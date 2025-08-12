import os
import re
import io
import base64
import logging
from datetime import datetime

from google.oauth2.service_account import Credentials
import gspread
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import requests

# ============================
# CONFIG
# ============================
BOT_TOKEN = "8053566580:AAFW7Y6WOFUsWew_H9uHCv9sVnrvz7C8dmU"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEET_NAME = "FLEX"
AUTHORIZED_USERS_FILE = "authorized_users.txt"
SERVICE_ACCOUNT_FILE = "credentials.json"

logging.basicConfig(level=logging.INFO)

# ============================
# GOOGLE SHEETS
# ============================
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
client = gspread.authorize(creds)
ss = client.open(GOOGLE_SHEET_NAME)
containers_ws = ss.sheet1
try:
    flex_ws = ss.worksheet("FLEX_TANKS")
except gspread.WorksheetNotFound:
    flex_ws = ss.add_worksheet(title="FLEX_TANKS", rows=1000, cols=10)
    flex_ws.append_row(["timestamp", "username", "user_id", "flex_number", "status"])

# ============================
# AUTH HELPERS
# ============================
def load_authorized_users():
    if not os.path.exists(AUTHORIZED_USERS_FILE):
        return set()
    with open(AUTHORIZED_USERS_FILE, "r", encoding="utf-8") as f:
        return set(map(int, filter(None, f.read().splitlines())))

def add_authorized_user(uid: int):
    with open(AUTHORIZED_USERS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{uid}\n")

AUTHORIZED_USERS = load_authorized_users()

# ============================
# ISO 6346 CHECK DIGIT
# ============================
CHAR_MAP = {
    'A': 10, 'B': 12, 'C': 13, 'D': 14, 'E': 15, 'F': 16,
    'G': 17, 'H': 18, 'I': 19, 'J': 20, 'K': 21, 'L': 23,
    'M': 24, 'N': 25, 'O': 26, 'P': 27, 'Q': 28, 'R': 29,
    'S': 30, 'T': 31, 'U': 32, 'V': 34, 'W': 35, 'X': 36,
    'Y': 37, 'Z': 38
}
WEIGHTS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]

def calc_check_digit(code10: str) -> str:
    total = 0
    for i, ch in enumerate(code10):
        if ch.isdigit():
            val = int(ch)
        else:
            val = CHAR_MAP.get(ch.upper(), 0)
        total += val * WEIGHTS[i]
    rem = total % 11
    return '0' if rem == 10 else str(rem)

# ============================
# OPENAI VISION HELPER (gpt-4o)
# ============================
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
HEADERS = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

PROMPT_CONTAINER = (
    "На фото контейнер или документ. Тебе нужно вернуть ТОЛЬКО номер контейнера "
    "в формате ISO 6346: 4 буквы и 7 цифр (пример: MSKU1234567). "
    "Если номера нет или он нечитаем, верни строго слово: НЕ УДАЛОСЬ."
)

PROMPT_FLEX = (
    "На фото этикетка флекси-танка. Номер ВСЕГДА начинается с B3G. Верни ТОЛЬКО серийный номер в формате: "
    "B3G########X-25Q или B3G########X-26Q (пример: B3G24071283B-26Q). "
    "Если номера нет или он нечитаем, верни строго: НЕ УДАЛОСЬ."
)

def ocr_gpt_base64(img_b64: str, mode: str) -> str:
    prompt = PROMPT_CONTAINER if mode == "container" else PROMPT_FLEX
    payload = {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                ],
            }
        ],
        "max_tokens": 50,
    }
    try:
        r = requests.post(OPENAI_URL, headers=HEADERS, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip().upper()
    except Exception as e:
        logging.exception("OpenAI request failed: %s", e)
        return "НЕ УДАЛОСЬ"

# ============================
# REGEX PARSERS
# ============================
CONTAINER_FULL = re.compile(r"\b([A-Z]{4})(?:\s|-)?(\d{6})(\d)\b")
FLEX_FULL = re.compile(r"\b(B3G\d{8}[A-Z])[-\s]?(25Q|26Q)\b")

def parse_container(text: str) -> tuple[str, str]:
    m = CONTAINER_FULL.search(text.replace(" ", "")) or CONTAINER_FULL.search(text)
    if not m:
        return "", "Ошибка OCR"
    owner = m.group(1)
    six = m.group(2)
    check = m.group(3)
    code10 = owner + six
    if calc_check_digit(code10) != check:
        return owner + six + check, "Требуется проверка (контрольная не сходится)"
    return owner + six + check, "Успешно"

def parse_flex(text: str) -> tuple[str, str]:
    t = text.replace(" ", "").replace("—", "-")
    m = FLEX_FULL.search(t)
    if not m:
        return "", "Ошибка OCR"
    number = f"{m.group(1)}-{m.group(2)}"
    return number, "Успешно"

# ============================
# TELEGRAM BOT
# ============================
scan_mode: dict[int, str] = {}
user_bucket: dict[int, list] = {}

def kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📦 Сканировать контейнер"), KeyboardButton("🧪 Сканировать флекс")],
         [KeyboardButton("Готово")]],
        resize_keyboard=True,
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Выберите режим сканирования и отправляйте фото. Когда закончите — нажмите ‘Готово’.",
        reply_markup=kb(),
    )

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in AUTHORIZED_USERS:
        await update.message.reply_text("✅ Вы уже зарегистрированы.")
        return
    AUTHORIZED_USERS.add(uid)
    add_authorized_user(uid)
    await update.message.reply_text("🎉 Регистрация успешна. Теперь можно отправлять фото.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip().lower()
    if uid not in AUTHORIZED_USERS:
        await update.message.reply_text("⛔ Нет доступа. Отправьте /register для регистрации.")
        return

    if txt.startswith("📦"):
        scan_mode[uid] = "container"
        user_bucket[uid] = []
        await update.message.reply_text("Режим: контейнер. Отправьте фото, затем нажмите ‘Готово’.")
    elif txt.startswith("🧪"):
        scan_mode[uid] = "flex"
        user_bucket[uid] = []
        await update.message.reply_text("Режим: флекс. Отправьте фото, затем нажмите ‘Готово’.")
    elif txt == "готово":
        await finalize(update, context)
    else:
        await update.message.reply_text("Выберите режим и отправьте фото.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in AUTHORIZED_USERS:
        await update.message.reply_text("⛔ Нет доступа. /register")
        return
    if uid not in scan_mode:
        await update.message.reply_text("Сначала выберите режим сканирования.")
        return
    user_bucket.setdefault(uid, []).append(update.message)
    await update.message.reply_text("✅ Фото получено. Ещё фото или ‘Готово’.")

async def finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or "Без ника"

    if uid not in scan_mode or not user_bucket.get(uid):
        await update.message.reply_text("⚠ Нет фото для обработки.")
        return

    mode = scan_mode[uid]
    msgs = user_bucket.pop(uid, [])
    scan_mode.pop(uid, None)

    for m in msgs:
        photo = m.photo[-1]
        f = await context.bot.get_file(photo.file_id)
        byts = await f.download_as_bytearray()
        img_b64 = base64.b64encode(byts).decode("utf-8")

        raw = ocr_gpt_base64(img_b64, mode)
        if mode == "container":
            number, status = parse_container(raw)
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            containers_ws.append_row([now, uname, uid, number or "Не удалось", status])
            await update.message.reply_text(f"Результат: {number or 'не удалось'} ({status})")
        else:
            number, status = parse_flex(raw)
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            flex_ws.append_row([now, uname, uid, number or "Не удалось", status])
            await update.message.reply_text(f"Результат: {number or 'не удалось'} ({status})")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logging.info("✅ Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
