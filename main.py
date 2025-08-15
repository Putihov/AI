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
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
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
# OCR (VISION): используем только поддерживаемые модели
#   Основная: gpt-4o  (поддерживает изображения)
#   Fallback: gpt-4.1  (как текстовый пост-проход, может вернуть кандидаты)
# ============================
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
HEADERS = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

RE_CONTAINER = re.compile(r"^[A-Z]{4}[0-9]{7}$")
RE_FLEX = re.compile(r"^B3G[0-9]{8,10}[A-Z]-2[56]Q$")


def _extract_match(text: str, mode: str) -> str | None:
    text = (text or "").upper()
    regex = RE_CONTAINER if mode == "container" else RE_FLEX
    m = regex.search(text)
    return m.group(0) if m else None


def ocr_gpt_base64(img_b64: str, mode: str) -> str:
    """Распознаём номер через gpt-4o. Если не найден — пробуем второй запрос с жёстким regex."""
    try:
        base_prompt = (
            "На фото контейнер. Найди номер ISO 6346. Верни только номер." if mode == "container" else
            "На фото этикетка флекси‑танка. Номер начинается с B3G и заканчивается -25Q или -26Q. Верни только номер."
        )

        def ask(prompt_text: str) -> str:
            payload = {
                "model": "gpt-4o",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "Ты — OCR-система. Отвечай ТОЛЬКО найденным номером, без комментариев."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                        ],
                    },
                ],
                "max_tokens": 30,
            }
            r = requests.post(OPENAI_CHAT_URL, headers=HEADERS, json=payload, timeout=60)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()

        # Первая попытка — базовый промпт
        out1 = ask(base_prompt)
        token = _extract_match(out1, mode)
        if token:
            return token

        # Вторая попытка — строго просим один матч по regex
        strict_regex = "^[A-Z]{4}[0-9]{7}$" if mode == "container" else "^B3G[0-9]{8,10}[A-Z]-2[56]Q$"
        out2 = ask(f"Найди и выведи ОДИН номер, соответствующий regex: {strict_regex}. Никаких пояснений.")
        token = _extract_match(out2, mode)
        if token:
            return token

    except Exception as e:
        logging.exception("OpenAI OCR error: %s", e)

    return "НЕ УДАЛОСЬ"

# ============================
# HELPERS
# ============================
# Сохранение фото отключено (по ТЗ), работаем только с OCR и ссылками на фото позже.

def update_sheet_cell(row: int, col: int, value: str):
    try:
        containers_ws.update_cell(row, col, value)
    except Exception as e:
        logging.exception("Sheets update failed (row=%s col=%s): %s", row, col, e)


def append_and_get_row(values: list) -> int:
    containers_ws.append_row(values, value_input_option="USER_ENTERED")
    # индекс рассчитываем по длине колонки E (букинг)
    return len(containers_ws.col_values(5))

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
    user_state[uid] = {"step": "booking", "have_container": False, "have_flex": False}
    await update.message.reply_text(
        "Введите номер букинга (или нажмите кнопку внизу).",
        reply_markup=reply_start_kb(),
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_state[uid] = {"step": "booking", "have_container": False, "have_flex": False}
    await q.edit_message_text("Введите номер букинга:")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or "Без ника"
    text = (update.message.text or "").strip()
    state = user_state.get(uid, {})
    step = state.get("step")

    # Кнопка «Начать» из нижнего меню
    if re.fullmatch(r"начать", text, flags=re.IGNORECASE):
        user_state[uid] = {"step": "booking", "have_container": False, "have_flex": False}
        await update.message.reply_text("Введите номер букинга:")
        return

    if step == "booking":
        booking = text
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        row = append_and_get_row([now, '', '', '', booking])  # Запись в E — букинг
        update_sheet_cell(row, 18, uname)  # R — логин установщика
        user_state[uid] = {"row": row, "booking": booking, "step": "photo", "have_container": False, "have_flex": False}
        await update.message.reply_text("📌 Букинг сохранён. Теперь загрузите фото контейнера и флекса (можно одним альбомом).")
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

    # fallback
    await update.message.reply_text("Нажмите «Начать», чтобы начать ввод.", reply_markup=reply_start_kb())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state.get(uid)

    # Принимаем фото на шагах photo ИЛИ beams (чтобы не мешал альбом)
    if not state or state.get("step") not in {"photo", "beams"}:
        await update.message.reply_text("Сначала нажмите «Начать» и введите букинг.", reply_markup=reply_start_kb())
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    bytes_data = await file.download_as_bytearray()
    img_b64 = base64.b64encode(bytes_data).decode("utf-8")

    # Пробуем достать оба номера с каждого фото. Записываем только если ещё не записано.
    row = state.get("row")
    if row:
        cont = ocr_gpt_base64(img_b64, "container")
        flex = ocr_gpt_base64(img_b64, "flex")
        if cont != "НЕ УДАЛОСЬ" and not state.get("have_container"):
            update_sheet_cell(row, 6, cont)
            state["have_container"] = True
        if flex != "НЕ УДАЛОСЬ" and not state.get("have_flex"):
            update_sheet_cell(row, 11, flex)
            state["have_flex"] = True

    # После первого фото переводим на шаг beams, но продолжаем принимать фото без ошибок
    if state.get("step") == "photo":
        state["step"] = "beams"
        await update.message.reply_text("📸 Фото обработано. Сколько балок?")
    else:
        # Если пользователь прислал ещё одно фото уже на шаге "beams" — молча пытаемся дополнить номера.
        pass

# ============================
# RUN (WEBHOOK-ONLY for Render Web Service)
# ============================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    # Исправление "global flags not at the start": используем компилированный regex с IGNORECASE
    app.add_handler(MessageHandler(filters.Regex(re.compile(r"^начать$", re.IGNORECASE)), handle_text))
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
