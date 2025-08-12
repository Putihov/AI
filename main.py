import os
import re
import io
import base64
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import json

from google.oauth2.service_account import Credentials
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# LOAD ENV
# ============================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "FLEX")
PHOTO_ROOT = Path("/tmp/flex_photos")

# ============================
# CONFIG
# ============================
logging.basicConfig(level=logging.INFO)

# ============================
# GOOGLE SHEETS
# ============================
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
client = gspread.authorize(creds)
ss = client.open(GOOGLE_SHEET_NAME)
containers_ws = ss.sheet1

# ============================
# STATE
# ============================
user_state = {}  # uid: {"row": int, "step": str, "booking": str}

# ============================
# OCR GPT
# ============================
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
HEADERS = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
PROMPT_CONTAINER = "На фото контейнер или документ. Верни ТОЛЬКО номер контейнера ISO 6346."
PROMPT_FLEX = "На фото этикетка флекси-танка. Верни ТОЛЬКО номер вида B3G########X-26Q."

def ocr_gpt_base64(img_b64: str, mode: str) -> str:
    prompt = PROMPT_CONTAINER if mode == "container" else PROMPT_FLEX
    payload = {
        "model": "gpt-4o",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            ]}
        ],
        "max_tokens": 50
    }
    try:
        r = requests.post(OPENAI_URL, headers=HEADERS, json=payload, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip().upper()
    except Exception:
        return "НЕ УДАЛОСЬ"

# ============================
# HELPERS
# ============================
def save_photo(photo_bytes: bytes, folder: Path, filename: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    with open(path, "wb") as f:
        f.write(photo_bytes)
    return str(path)

def file_url(path: Path) -> str:
    return f"file:///{str(path).replace('\\', '/')}"

def update_sheet_cell(row: int, col: int, value: str):
    containers_ws.update_cell(row, col, value)

# ============================
# TELEGRAM BOT
# ============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Начать ввод данных", callback_data="start_entry")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Добро пожаловать! Нажмите кнопку ниже, чтобы начать.", reply_markup=reply_markup)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    user_state[uid] = {"step": "booking"}
    await query.edit_message_text("Введите номер букинга:")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or "Без ника"
    text = update.message.text.strip()
    state = user_state.get(uid, {})
    step = state.get("step")

    if step == "booking":
        booking = text
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        row_data = [now, '', '', '', booking]
        containers_ws.append_row(row_data)
        row = containers_ws.row_count
        update_sheet_cell(row, 18, uname)  # R
        user_state[uid] = {"row": row, "booking": booking, "step": "photo"}
        await update.message.reply_text("📌 Букинг сохранён. Теперь загрузите фото контейнера и флекса.")
    elif step == "beams":
        if text.isdigit():
            update_sheet_cell(user_state[uid]["row"], 14, text)  # N
            user_state[uid]["step"] = "addons"
            await update.message.reply_text("📌 Сколько дополнительных? Введите число:")
        else:
            await update.message.reply_text("⚠ Нужно ввести число.")
    elif step == "addons":
        if text.isdigit():
            update_sheet_cell(user_state[uid]["row"], 15, text)  # O
            user_state[uid]["step"] = "sheets"
            await update.message.reply_text("📌 Сколько листов? Введите число:")
        else:
            await update.message.reply_text("⚠ Нужно ввести число.")
    elif step == "sheets":
        if text.isdigit():
            update_sheet_cell(user_state[uid]["row"], 16, text)  # P
            await update.message.reply_text("✅ Все данные сохранены.")
            del user_state[uid]  # сброс состояния
        else:
            await update.message.reply_text("⚠ Нужно ввести число.")
    else:
        await update.message.reply_text("Нажмите /start для начала.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_state or user_state[uid].get("step") != "photo":
        await update.message.reply_text("Сначала нажмите /start и введите букинг.")
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    bytes_data = await file.download_as_bytearray()
    img_b64 = base64.b64encode(bytes_data).decode("utf-8")

    container_number = ocr_gpt_base64(img_b64, "container")
    flex_number = ocr_gpt_base64(img_b64, "flex")

    folder = PHOTO_ROOT / user_state[uid]['booking']
    save_photo(bytes_data, folder, f"{datetime.now().strftime('%H%M%S')}.jpg")
    f_url = file_url(folder)

    row = user_state[uid]['row']
    if container_number != "НЕ УДАЛОСЬ":
        update_sheet_cell(row, 6, container_number)  # F
        update_sheet_cell(row, 8, f_url)             # H
    if flex_number != "НЕ УДАЛОСЬ":
        update_sheet_cell(row, 11, flex_number)      # K
        update_sheet_cell(row, 13, f_url)            # M

    user_state[uid]["step"] = "beams"
    await update.message.reply_text("📸 Фото обработано. Сколько балок?")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logging.info("✅ Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
