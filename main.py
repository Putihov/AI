# NOTE: Для Render Web Service нужна зависимость с вебхуками:
# requirements.txt → python-telegram-bot[webhooks]==20.3

import os
import base64
import logging
from datetime import datetime
from dotenv import load_dotenv
import json
from typing import Dict
import re

from google.oauth2.service_account import Credentials
import gspread
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import requests

# ============================
# ENV & CONFIG
# ============================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "FLEX")

logging.basicConfig(level=logging.INFO)

# ============================
# GOOGLE SHEETS
# ============================
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
client = gspread.authorize(creds)
ss = client.open(GOOGLE_SHEET_NAME)
containers_ws = ss.sheet1

# ============================
# STATE (пошаговый ввод)
# ============================
# chain: booking -> photo -> beams -> addons -> sheets
user_state: Dict[int, Dict] = {}

# ============================
# OCR (строгая валидация + повторная попытка + препроцессинг)
# ============================
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
HEADERS = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

PROMPT_CONTAINER = (
    "На фото контейнер или документ. Твоя задача — найти номер контейнера в формате ISO 6346. "
    "Верни СТРОГО один токен, который соответствует regex: ^[A-Z]{4}[0-9]{7}$. "
    "Если уверенности нет — верни ровно: НЕ УДАЛОСЬ."
)

PROMPT_FLEX = (
    "На фото этикетка флекси-танка. Ищи серийный номер, который ВСЕГДА начинается с B3G. "
    "Верни СТРОГО один токен, соответствующий regex: ^B3G[0-9]{8,10}[A-Z]-2[56]Q$ . "
    "Примеры: B3G24071283B-26Q, B3G24071254B-26Q. Если нет совпадения — верни: НЕ УДАЛОСЬ."
)

RE_CONTAINER = re.compile(r"^[A-Z]{4}[0-9]{7}$")
RE_FLEX = re.compile(r"^B3G[0-9]{8,10}[A-Z]-2[56]Q$")


def ocr_gpt_base64(img_b64: str, mode: str) -> str:
    """Распознаём номер через GPT, валидируем по regex. Две попытки; ищем match в тексте;
    пробуем оригинал и улучшенное изображение (контраст/резкость).
    """
    from PIL import Image, ImageOps, ImageFilter
    import io

    def enhance(b: bytes) -> str:
        im = Image.open(io.BytesIO(b)).convert("L")
        im = ImageOps.autocontrast(im)
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=150))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=95)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def extract_match(text: str) -> str | None:
        text = (text or "").upper()
        m = (RE_CONTAINER if mode == "container" else RE_FLEX).search(text)
        return m.group(0) if m else None

    def ask(prompt_text: str, img: str) -> str:
        payload = {
            "model": "gpt-4o",
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}},
                    ],
                }
            ],
            "max_tokens": 30,
        }
        r = requests.post(OPENAI_URL, headers=HEADERS, json=payload, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip().upper()

    try:
        imgs = [img_b64]
        try:
            imgs.append(enhance(base64.b64decode(img_b64)))
        except Exception:
            pass

        prompts = [
            PROMPT_CONTAINER if mode == "container" else PROMPT_FLEX,
            ("Выведи ТОЛЬКО одно совпадение с regex: "
             + ("^[A-Z]{4}[0-9]{7}$" if mode == "container" else "^B3G[0-9]{8,10}[A-Z]-2[56]Q$")
             + ". Не добавляй комментарии. Если совпадения нет — верни: НЕ УДАЛОСЬ."),
        ]

        for img in imgs:
            for p in prompts:
                out = ask(p, img)
                token = extract_match(out)
                if token:
                    return token
        return "НЕ УДАЛОСЬ"
    except Exception as e:
        logging.exception("OpenAI OCR error: %s", e)
        return "НЕ УДАЛОСЬ"

# ============================
# HELPERS
# ============================
# Сохранение фото временно отключено по требованию (оставлены заглушки).
# def save_photo(...)

def update_sheet_cell(row: int, col: int, value: str):
    try:
        containers_ws.update_cell(row, col, value)
    except Exception as e:
        logging.exception("Sheets update failed (row=%s col=%s): %s", row, col, e)


def append_and_get_row(values: list) -> int:
    containers_ws.append_row(values, value_input_option="USER_ENTERED")
    # Индекс последней занятой строки по колонке E (букинг)
    return len(containers_ws.col_values(5))  # 5 = столбец E

# ============================
# UI helpers
# ============================

def inline_start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Начать", callback_data="start_entry")]])


def reply_start_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("Начать")]], resize_keyboard=True, one_time_keyboard=False)

# ============================
# HANDLERS
# ============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"step": "booking"}
    await update.message.reply_text(
        "Добро пожаловать! Введите номер букинга или нажмите кнопку \"Начать\".",
        reply_markup=reply_start_kb(),
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_state[uid] = {"step": "booking"}
    await q.edit_message_text("Введите номер букинга:")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or "Без ника"
    text = (update.message.text or "").strip()
    state = user_state.get(uid, {})
    step = state.get("step")

    # Нажата кнопка ReplyKeyboard «Начать»
    if text.lower() == "начать":
        user_state[uid] = {"step": "booking"}
        await update.message.reply_text("Введите номер букинга:")
        return

    if step == "booking":
        booking = text
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        row = append_and_get_row([now, '', '', '', booking])  # E — букинг
        update_sheet_cell(row, 18, uname)  # R — username (установщик)
        user_state[uid] = {"row": row, "booking": booking, "step": "photo"}
        await update.message.reply_text("📌 Букинг сохранён. Теперь загрузите фото контейнера и флекса.")
        return

    if step == "beams":
        if text.isdigit():
            update_sheet_cell(state["row"], 14, text)  # N — Балки
            user_state[uid]["step"] = "addons"
            await update.message.reply_text("📌 Сколько дополнительных? Введите число:")
        else:
            await update.message.reply_text("⚠ Нужно ввести число.")
        return

    if step == "addons":
        if text.isdigit():
            update_sheet_cell(state["row"], 15, text)  # O — Допы
            user_state[uid]["step"] = "sheets"
            await update.message.reply_text("📌 Сколько листов? Введите число:")
        else:
            await update.message.reply_text("⚠ Нужно ввести число.")
        return

    if step == "sheets":
        if text.isdigit():
            update_sheet_cell(state["row"], 16, text)  # P — Листы
            user_state.pop(uid, None)
            await update.message.reply_text("✅ Все данные сохранены.", reply_markup=reply_start_kb())
        else:
            await update.message.reply_text("⚠ Нужно ввести число.")
        return

    # Если шаг не установлен — показываем кнопку Начать
    await update.message.reply_text("Нажмите «Начать», чтобы пройти шаги.", reply_markup=reply_start_kb())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state.get(uid)

    mgid = getattr(update.message, "media_group_id", None)
    if state and mgid and mgid == state.get("last_mgid"):
        return

    if not state or state.get("step") != "photo":
        if state and state.get("row"):
            await update.message.reply_text("Фото уже обработано. Сколько балок?")
            return
        await update.message.reply_text("Сначала нажмите «Начать» и введите букинг.", reply_markup=reply_start_kb())
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    bytes_data = await file.download_as_bytearray()
    img_b64 = base64.b64encode(bytes_data).decode("utf-8")

    container_number = ocr_gpt_base64(img_b64, "container")
    flex_number = ocr_gpt_base64(img_b64, "flex")

    row = state["row"]
    if container_number != "НЕ УДАЛОСЬ":
        update_sheet_cell(row, 6, container_number)  # F — контейнер
    if flex_number != "НЕ УДАЛОСЬ":
        update_sheet_cell(row, 11, flex_number)      # K — флекс

    user_state[uid]["last_mgid"] = mgid
    user_state[uid]["step"] = "beams"
    await update.message.reply_text("📸 Фото обработано. Сколько балок?")

# ============================
# RUN (WEBHOOK-ONLY for Render Web Service)
# ============================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.Regex(r"^(?i)начать$"), handle_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # --- WEBHOOK-ONLY ---
    port = int(os.environ.get("PORT", 8443))
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not host:
        raise RuntimeError(
            "RENDER_EXTERNAL_HOSTNAME не задан. Запускайте как Render Web Service или укажите переменную окружения."
        )

    webhook_url = f"https://{host}/{BOT_TOKEN}"
    logging.info(f"✅ Запускаем webhook на {webhook_url}, порт={port}")

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=BOT_TOKEN,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
