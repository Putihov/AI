# NOTE: Для Render Web Service нужна зависимость с вебхуками:
# requirements.txt → python-telegram-bot[webhooks]==20.3

import os
import base64
import logging
from datetime import datetime
from dotenv import load_dotenv
import json
from typing import Dict

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
# OCR (сохраняем рабочую логику)
# ============================
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
HEADERS = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
PROMPT_CONTAINER = (
    "На фото контейнер или документ. Верни ТОЛЬКО номер контейнера в формате ISO 6346: "
    "4 буквы + 7 цифр (пример: MSKU1234567). Если не найдено — верни: НЕ УДАЛОСЬ."
)
PROMPT_FLEX = (
    "На фото этикетка флекси-танка. Верни ТОЛЬКО номер в формате B3G########X-25Q/26Q "
    "(пример: B3G24071283B-26Q). Если не найдено — верни: НЕ УДАЛОСЬ."
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
        return r.json()["choices"][0]["message"]["content"].strip().upper()
    except Exception as e:
        logging.exception("OpenAI OCR error: %s", e)
        return "НЕ УДАЛОСЬ"

# ============================
# HELPERS
# ============================
# Сохранение фото отключено по требованию (оставляем заглушки).
# def save_photo(photo_bytes: bytes, folder: Path, filename: str) -> str:
#     ...
# def file_url(path: Path) -> str:
#     ...

def update_sheet_cell(row: int, col: int, value: str):
    try:
        containers_ws.update_cell(row, col, value)
    except Exception as e:
        logging.exception("Sheets update failed (row=%s col=%s): %s", row, col, e)

# Утилита: получить индекс строки последнего добавления
# (append_row сам не возвращает индекс; поэтому считаем до и после)

def append_and_get_row(values: list) -> int:
    pre = len(containers_ws.get_all_values())
    containers_ws.append_row(values)
    return pre + 1

# ============================
# HANDLERS
# ============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"step": "booking"}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Начать ввод данных", callback_data="start_entry")]])
    await update.message.reply_text(
        "Добро пожаловать! Введите номер букинга (или нажмите кнопку ниже).",
        reply_markup=kb,
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

    if step == "booking":
        booking = text
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        # корректно получаем индекс строки с новым букингом
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
            await update.message.reply_text("✅ Все данные сохранены.")
            user_state.pop(uid, None)
        else:
            await update.message.reply_text("⚠ Нужно ввести число.")
        return

    await update.message.reply_text("Нажмите /start и следуйте шагам.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state.get(uid)

    # Если фото пришло альбомом (media group), не ругаемся на каждое фото
    mgid = getattr(update.message, "media_group_id", None)
    if state and mgid and mgid == state.get("last_mgid"):
        # повторное фото из того же альбома — пропускаем молча
        return

    if not state or state.get("step") != "photo":
        # Если уже прошли шаг фото — напомним, что ждём число балок
        if state and state.get("row"):
            await update.message.reply_text("Фото уже обработано. Сколько балок?")
            return
        await update.message.reply_text("Сначала нажмите /start и введите букинг.")
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

    # запоминаем обработанный альбом, чтобы не дублировать
    user_state[uid]["last_mgid"] = mgid

    # Переходим к следующему шагу — ввод балок
    user_state[uid]["step"] = "beams"
    await update.message.reply_text("📸 Фото обработано. Сколько балок?")

# ============================
# RUN (WEBHOOK-ONLY for Render Web Service)
# ============================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # --- WEBHOOK-ONLY (без fallback на polling) ---
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
