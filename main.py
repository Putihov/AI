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
import requests
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
# STATE
# ============================
user_state: Dict[int, Dict] = {}

# ============================
# OCR via GPT-5o → fallback GPT-4o
# ============================
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
HEADERS = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

RE_CONTAINER = re.compile(r"^[A-Z]{4}[0-9]{7}$")
RE_FLEX = re.compile(r"^B3G[0-9]{8,10}[A-Z]-2[56]Q$")

def ocr_gpt_base64(img_b64: str, mode: str) -> str:
    models = ["gpt-5o", "gpt-4o"]
    prompt = (
        "Извлеки номер контейнера формата ISO 6346" if mode == "container" else
        "Извлеки серийный номер флекситанка, начинающийся с B3G"
    )

    for model in models:
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "Ты — OCR-система. Отвечай только найденным номером."},
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                    ]}
                ],
                "max_tokens": 30,
            }
            r = requests.post(OPENAI_URL, headers=HEADERS, json=payload, timeout=60)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip().upper()
            regex = RE_CONTAINER if mode == "container" else RE_FLEX
            m = regex.search(text)
            if m:
                return m.group(0)
        except Exception as e:
            logging.error(f"OCR error on {model}: {e}")
    return "НЕ УДАЛОСЬ"

# ============================
# HELPERS
# ============================
def update_sheet_cell(row: int, col: int, value: str):
    try:
        containers_ws.update_cell(row, col, value)
    except Exception as e:
        logging.exception("Sheets update failed (row=%s col=%s): %s", row, col, e)

def append_and_get_row(values: list) -> int:
    containers_ws.append_row(values, value_input_option="USER_ENTERED")
    return len(containers_ws.col_values(5))

def reply_start_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("Начать")]], resize_keyboard=True)

# ============================
# HANDLERS
# ============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"step": "booking"}
    await update.message.reply_text(
        "Добро пожаловать! Введите номер букинга.",
        reply_markup=reply_start_kb(),
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or "Без ника"
    text = (update.message.text or "").strip()
    state = user_state.get(uid, {})
    step = state.get("step")

    if text.lower() == "начать":
        user_state[uid] = {"step": "booking"}
        await update.message.reply_text("Введите номер букинга:")
        return

    if step == "booking":
        booking = text
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        row = append_and_get_row([now, '', '', '', booking])
        update_sheet_cell(row, 18, uname)
        user_state[uid] = {"row": row, "booking": booking, "step": "photo"}
        await update.message.reply_text("📌 Букинг сохранён. Теперь загрузите фото контейнера и флекса.")
        return

    if step == "beams":
        if text.isdigit():
            update_sheet_cell(state["row"], 14, text)
            user_state[uid]["step"] = "addons"
            await update.message.reply_text("📌 Сколько дополнительных?")
        else:
            await update.message.reply_text("⚠ Нужно ввести число.")
        return

    if step == "addons":
        if text.isdigit():
            update_sheet_cell(state["row"], 15, text)
            user_state[uid]["step"] = "sheets"
            await update.message.reply_text("📌 Сколько листов?")
        else:
            await update.message.reply_text("⚠ Нужно ввести число.")
        return

    if step == "sheets":
        if text.isdigit():
            update_sheet_cell(state["row"], 16, text)
            user_state.pop(uid, None)
            await update.message.reply_text("✅ Все данные сохранены.", reply_markup=reply_start_kb())
        else:
            await update.message.reply_text("⚠ Нужно ввести число.")
        return

    await update.message.reply_text("Нажмите «Начать», чтобы пройти шаги.", reply_markup=reply_start_kb())

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state.get(uid)
    if not state or state.get("step") != "photo":
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
        update_sheet_cell(row, 6, container_number)
    if flex_number != "НЕ УДАЛОСЬ":
        update_sheet_cell(row, 11, flex_number)

    user_state[uid]["step"] = "beams"
    await update.message.reply_text("📸 Фото обработано. Сколько балок?")

# ============================
# RUN WEBHOOK
# ============================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^начать$"), handle_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    port = int(os.environ.get("PORT", 8443))
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not host:
        raise RuntimeError("RENDER_EXTERNAL_HOSTNAME не задан")

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
