import logging
import os
import sys
import asyncio
import html
import json
import re
import base64
from datetime import datetime, timezone, timedelta, time
import aiosqlite
import pymupdf
import io
from docx import Document
from dotenv import load_dotenv
from telegram import ReactionTypeEmoji
from google import genai
from google.genai import types
from openai import AsyncOpenAI
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, MessageEntity
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
    CommandHandler,
    CallbackQueryHandler,
)

from access import init_billing_db, consume_request_and_check, grant_lifetime_access, add_subscription_days, get_and_mark_expiring_subscriptions
import web_parser
from web_parser import process_message_for_urls

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))  # ID владельца бота
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Клиенты
openrouter_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
groq_client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=GROQ_URL)

AI_PROMPT_TGM = os.getenv("AI_PROMPT_TGM", "")
AI_PROMPT_PM = os.getenv("AI_PROMPT_PM", "")
AI_PROMPT_GM = os.getenv("AI_PROMPT_GM", "")
AI_PROMPT_IS_RELEVANT_QUESTION = os.getenv("AI_PROMPT_IS_RELEVANT_QUESTION", "")

# Настройки
SILENCE_MINUTES = int(os.getenv("SILENCE_MINUTES", "10")) #На сколько время ставить на паузу бота в минутах

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = "chat_history.db"
MODELS_CONFIG_PATH = "models.json"

user_selected_model = {}
last_request_time = {}
GLOBAL_SEMAPHORE = asyncio.Semaphore(1)
user_locks = {}
user_buffers = {} # Переменная для накопления быстрых сообщений
active_tasks = {}
silenced_chats = {}  # здесь храним, до какого времени чат молчит

MAX_MESSAGE_LENGTH = 4096
RATE_LIMIT_SECONDS_PER_USER = 1.0


# ==========================================
# РАБОТА С БАЗОЙ ДАННЫХ (SQLite)
# ==========================================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS messages
                         (
                             id
                             INTEGER
                             PRIMARY
                             KEY
                             AUTOINCREMENT,
                             chat_key
                             TEXT,
                             role
                             TEXT,
                             content
                             TEXT,
                             created_at
                             TIMESTAMP
                             DEFAULT
                             CURRENT_TIMESTAMP
                         )
                         """)
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS settings
                         (
                             chat_key
                             TEXT
                             PRIMARY
                             KEY,
                             auto_fallback
                             INTEGER
                             DEFAULT
                             1,
                             keep_context
                             INTEGER
                             DEFAULT
                             1
                         )
                         """)
        # Безопасно обновляем существующую таблицу, если колонки еще нет
        try:
            await db.execute("ALTER TABLE settings ADD COLUMN keep_context INTEGER DEFAULT 1")
        except Exception:
            pass  # Колонка уже существует, всё ок

        try: # Настройка отвечать на все
            await db.execute("ALTER TABLE settings ADD COLUMN respond_all INTEGER DEFAULT 0")
        except Exception:
            pass
        try: # Выбранная модель
            await db.execute("ALTER TABLE settings ADD COLUMN selected_model TEXT")
        except Exception:
            pass
        try: # тихий ответ
            await db.execute("ALTER TABLE settings ADD COLUMN silent_responses INTEGER DEFAULT 0")
        except Exception:
            pass


        await db.commit()


async def get_auto_fallback(chat_key):
    """Возвращает статус авто-переключения (по умолчанию True)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT auto_fallback FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else True


async def toggle_auto_fallback(chat_key):
    """Переключает статус авто-переключения и возвращает новое значение"""
    current = await get_auto_fallback(chat_key)
    new_val = 0 if current else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, auto_fallback)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET auto_fallback = excluded.auto_fallback
                         """, (str(chat_key), new_val))
        await db.commit()
    return bool(new_val)


async def add_message_to_db(chat_key, role: str, content: str):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO messages (chat_key, role, content) VALUES (?, ?, ?)",
                (str(chat_key), role, content)
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка записи в БД: {e}")


async def get_history_from_db(chat_key, limit: int = 10):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT role, content FROM messages WHERE chat_key = ? ORDER BY id DESC LIMIT ?",
                (str(chat_key), limit)
            )
            rows = await cursor.fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    except Exception as e:
        return []


async def clear_history_in_db(chat_key):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM messages WHERE chat_key = ?", (str(chat_key),))
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка очистки БД: {e}")

async def get_respond_all(chat_key):
    """Возвращает статус режима 'Отвечать на всё' (по умолчанию False)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT respond_all FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

async def toggle_respond_all(chat_key):
    """Переключает статус 'Отвечать на всё' и возвращает новое значение"""
    current = await get_respond_all(chat_key)
    new_val = 0 if current else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, respond_all)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET respond_all = excluded.respond_all
                         """, (str(chat_key), new_val))
        await db.commit()
    return bool(new_val)


async def get_user_model(chat_key):
    """Достает выбранную модель из БД. Если её нет - берет первую из конфига."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT selected_model FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return row[0]

    # Если в базе пусто, отдаем модель по умолчанию
    return load_models_config()[0]["id"]


async def set_user_model(chat_key, model_id):
    """Сохраняет выбранную модель в БД"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, selected_model)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET selected_model = excluded.selected_model
                         """, (str(chat_key), model_id))
        await db.commit()

async def get_silent_responses(chat_key):
    """Возвращает статус тихих ответов (по умолчанию False/со звуком)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT silent_responses FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

async def toggle_silent_responses(chat_key):
    """Переключает статус тихих ответов"""
    current = await get_silent_responses(chat_key)
    new_val = 0 if current else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, silent_responses)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET silent_responses = excluded.silent_responses
                         """, (str(chat_key), new_val))
        await db.commit()
    return bool(new_val)

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И КОНФИГ
# ==========================================

async def check_admin_rights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, является ли пользователь админом чата или владельцем бота."""
    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type == "private":
        return True
    if ADMIN_ID and user_id == ADMIN_ID:
        return True

    try:
        member = await chat.get_member(user_id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False


def load_models_config():
    if not os.path.exists(MODELS_CONFIG_PATH):
        raise FileNotFoundError(f"Файл {MODELS_CONFIG_PATH} не найден!")
    with open(MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
        models = json.load(f)
        if not models:
            raise ValueError(f"Файл {MODELS_CONFIG_PATH} пуст!")
        return models


def get_ordered_models(current_model_id: str):
    all_models = load_models_config()
    current_model = next((m for m in all_models if m["id"] == current_model_id), None)
    ordered = []
    if current_model:
        ordered.append(current_model)
    for m in all_models:
        if m["id"] != current_model_id:
            ordered.append(m)
    return ordered


def get_system_prompt(chat_type, user_name=""):
    if chat_type == "private":
        base_prompt = AI_PROMPT_PM or ""
        if user_name:
            base_prompt += f"\n\n[Системная информация: собеседника зовут {user_name}]"
    else:
        base_prompt = f"{AI_PROMPT_GM}\n{AI_PROMPT_TGM}"

    # Секретная инструкция с правильным списком Telegram-реакций и разрешением на молчание
    reaction_rule = (
        "\n\n[СЕКРЕТНАЯ ИНСТРУКЦИЯ (СТРОГО): Если уместно отреагировать эмоцией, начни ответ с тега [REACTION: эмодзи]. "
        "Разрешен ТОЛЬКО этот точный список эмодзи: "
        "❤️, 👌, 👍, 😁, 🔥, 🤡, 🤣, 👎, 🥰, 👏, 🤔, 🤯, 😱, 🤬, 😢, 🎉, 🤩, 🤮, 💩, 🙏, 🕊️, 🥱, 🥴, 😍, 🐳, ❤️‍🔥, 🌚, 🌭, 💯, ⚡, 🍌, 🏆, 💔, 🤨, 😐, 🍓, 🍾, 💋, 🖕, 😈, 😴, 😭, 🤓, 👻, 👨‍💻, 👀, 🎃, 🙈, 😇, 😨, 🤝, ✍️, 🤗, 🫡, 🎅, 🎄, ☃️, 💅, 🤪, 🗿, 🆒, 💘, 🙉, 🦄, 😘, 💊, 🙊, 😎, 👾, 🤷‍♂️, 🤷, 🤷‍♀️, 😡. "
        "\n❗️ ПРАВИЛО ВЫЖИВАНИЯ: Если идеального эмодзи нет в этом списке — НЕ ПИШИ ТЕГ ВООБЩЕ. "
        "Использование эмодзи вне списка ВЫЗЫВАЕТ КРИТИЧЕСКУЮ ОШИБКУ API ТЕЛЕГРАМА И ПОЛОМКУ БОТА. "
        "Если текст ответа не нужен, отвечай ТОЛЬКО одним тегом из списка.]"
    )

    return base_prompt + reaction_rule


def split_message(message: str):
    parts = []
    while len(message) > MAX_MESSAGE_LENGTH:
        split_index = message[:MAX_MESSAGE_LENGTH].rfind("\n")
        if split_index == -1:
            split_index = MAX_MESSAGE_LENGTH
        parts.append(message[:split_index])
        message = message[split_index:]
    parts.append(message)
    return parts

def sanitize_text(text: str) -> str:
    """Вырезает теги <think> от DeepSeek и очищает текст от невалидного HTML"""
    if not text:
        return ""
    # Вырезаем блок <think> ... </think> вместе с содержимым
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # На всякий случай удаляем висячие теги, если модель их не закрыла
    text = text.replace('<think>', '').replace('</think>', '')
    return text.strip()

async def ensure_user_lock(key):
    if key not in user_locks:
        user_locks[key] = asyncio.Lock()
    return user_locks[key]


async def typing_sender(chat_id: int, context: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


# ==========================================
# ОСНОВНАЯ ЛОГИКА ИИ
# ==========================================

async def query_gemini(model_id: str, history: list, sys_prompt: str, image_bytes: bytes = None) -> str:
    contents = []
    for i, msg in enumerate(history):
        role = "model" if msg["role"] == "assistant" else "user"
        parts = [types.Part.from_text(text=msg["content"])]

        # Если есть картинка, прикрепляем её к последнему сообщению пользователя
        if image_bytes and i == len(history) - 1 and role == "user":
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

        contents.append(types.Content(role=role, parts=parts))

    config = types.GenerateContentConfig(system_instruction=sys_prompt if sys_prompt else None, temperature=0.7)
    response = await asyncio.wait_for(
        asyncio.to_thread(gemini_client.models.generate_content, model=model_id, contents=contents, config=config),
        timeout=15.0
    )
    return response.text.strip()


async def query_groq(model_id: str, history: list, sys_prompt: str, image_bytes: bytes = None) -> str:
    # Магия Groq: Если прикреплена картинка, а текущая модель не поддерживает зрение,
    # временно и незаметно переключаем запрос на самую мощную vision-модель!
    if image_bytes and "vision" not in model_id.lower():
        model_id = "llama-3.2-90b-vision-preview"

    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})

    for i, msg in enumerate(history):
        if image_bytes and i == len(history) - 1 and msg["role"] == "user":
            b64_img = base64.b64encode(image_bytes).decode('utf-8')
            content = [
                {"type": "text", "text": msg["content"]},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
            ]
            messages.append({"role": msg["role"], "content": content})
        else:
            messages.append({"role": msg["role"], "content": msg["content"]})

    response = await asyncio.wait_for(
        groq_client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=0.7,
            max_tokens=1000
        ),
        timeout=15.0
    )
    return response.choices[0].message.content.strip()


async def query_openrouter(model_id: str, history: list, sys_prompt: str, image_bytes: bytes = None) -> str:
    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})

    for i, msg in enumerate(history):
        if image_bytes and i == len(history) - 1 and msg["role"] == "user":
            b64_img = base64.b64encode(image_bytes).decode('utf-8')
            content = [
                {"type": "text", "text": msg["content"]},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
            ]
            messages.append({"role": msg["role"], "content": content})
        else:
            messages.append({"role": msg["role"], "content": msg["content"]})

    response = await asyncio.wait_for(
        openrouter_client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=0.7,
        ),
        timeout=15.0,
    )
    return response.choices[0].message.content.strip()


def parse_llm_error(error: Exception) -> str:
    """Универсальный переводчик ошибок от любых API-провайдеров на человеческий язык"""
    err_str = str(error).lower()

    if "timeout" in err_str or "timed out" in err_str:
        return "Таймаут (превышено время ожидания)"
    elif any(x in err_str for x in ["not a valid model", "model_not_found", "does not exist", "404"]):
        return "Модель не найдена, удалена или неверный ID"
    elif any(x in err_str for x in ["rate-limited", "429", "too many requests", "quota exceeded"]):
        return "Истрачены лимиты или сервер временно перегружен"
    elif any(x in err_str for x in ["tokens", "too large", "maximum context"]):
        return "Превышен лимит токенов (слишком длинная история)"
    elif any(x in err_str for x in ["api_key", "401", "403", "unauthorized"]):
        return "Ошибка авторизации (проверьте API-ключ)"
    elif any(x in err_str for x in ["500", "502", "503"]):
        return "Сервер провайдера упал или высокая нагрузка на модель (ошибка 500+)"
    elif "400" in err_str or "invalid_argument" in err_str:
        return "Неверный формат запроса (Bad Request)"

    return "Неизвестная ошибка API"

async def ask_llm(user_or_chat_id, prompt: str, chat_type: str, user_name: str = "", image_bytes: bytes = None):
    # Помечаем в истории базы данных, что к тексту была прикреплена картинка
    db_prompt = f"[Фото] {prompt}" if image_bytes else prompt
    await add_message_to_db(user_or_chat_id, "user", db_prompt)

    history = await get_history_from_db(user_or_chat_id, limit=12)
    sys_prompt = get_system_prompt(chat_type, user_name)

    current_model_id = await get_user_model(user_or_chat_id)

    ordered_models = get_ordered_models(current_model_id)

    # ПРОВЕРКА НАСТРОЙКИ ПЕРЕКЛЮЧЕНИЯ
    auto_fallback = await get_auto_fallback(user_or_chat_id)
    if not auto_fallback:
        ordered_models = [ordered_models[0]]  # Оставляем только текущую модель

    switched = False
    new_model_name = ""
    switch_reason = ""  # Переменная для хранения причины сбоя

    async with GLOBAL_SEMAPHORE:
        for model in ordered_models:
            model_id = model["id"]
            provider = model["provider"]

            try:
                if provider == "gemini":
                    reply_text = await query_gemini(model_id, history, sys_prompt, image_bytes)
                elif provider == "groq":
                    reply_text = await query_groq(model_id, history, sys_prompt, image_bytes)
                elif provider == "openrouter":
                    reply_text = await query_openrouter(model_id, history, sys_prompt, image_bytes)
                else:
                    continue

                # Очищаем ответ от невалидных тегов ДО сохранения в базу и отправки
                reply_text = sanitize_text(reply_text)

                if model_id != current_model_id:
                    switched = True
                    new_model_name = model["name"]
                    await set_user_model(user_or_chat_id, model_id)

                await add_message_to_db(user_or_chat_id, "assistant", reply_text)

                if switched:
                    # Добавляем причину падения предыдущей модели прямо в сообщение!
                    reason_text = f" (причина: {switch_reason})" if switch_reason else ""
                    reply_text = f"<i>⚠️ Переключено на <b>{new_model_name}</b>{reason_text}.</i>\n\n" + reply_text

                return reply_text

            except Exception as e:
                # 1. Прогоняем сырую ошибку через наш парсер
                switch_reason = parse_llm_error(e)

                # 2. Выводим в консоль сервера и красивую причину, и сырую ошибку для дебага
                logger.warning(
                    f"Модель {model_id} ({provider}) пропущена. Причина: {switch_reason} | Сырая ошибка: {e}")
                continue

    if not auto_fallback:
        return f"⚠️ Модель <b>{current_model_id}</b> временно недоступна ({switch_reason}).\nАвто-переключение отключено."

    return f"⚠️ Все модели из списка временно недоступны.\nПоследняя ошибка: {switch_reason}"


async def is_bot_relevant(text: str, chat_id: int):
    # Базовая защита: не дергаем API из-за одного символа "?"
    if len(text.strip()) < 3:
        return False

    sys_prompt = AI_PROMPT_IS_RELEVANT_QUESTION
    user_prompt = f"Вопрос: {text}"

    current_model_id = await get_user_model(chat_id)

    ordered_models = get_ordered_models(current_model_id)
    auto_fallback = await get_auto_fallback(chat_id)
    if not auto_fallback:
        ordered_models = [ordered_models[0]]

    for model in ordered_models:
        model_id = model["id"]
        provider = model["provider"]

        try:
            if provider == "gemini":
                config = types.GenerateContentConfig(system_instruction=sys_prompt, temperature=0.1)
                response = await asyncio.wait_for(
                    asyncio.to_thread(gemini_client.models.generate_content, model=model_id, contents=user_prompt,
                                      config=config),
                    timeout=10.0
                )
                # Очищаем от возможных <think> и ищем слово "да"
                reply = sanitize_text(response.text)
                return "да" in reply.lower()

            elif provider == "groq":
                response = await asyncio.wait_for(
                    groq_client.chat.completions.create(
                        model=model_id,
                        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                        temperature=0.1
                    ),
                    timeout=10.0
                )
                reply = sanitize_text(response.choices[0].message.content or "")
                return "да" in reply.lower()

            elif provider == "openrouter":  # БЛОК ДЛЯ OPENROUTER
                response = await asyncio.wait_for(
                    openrouter_client.chat.completions.create(
                        model=model_id,
                        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                        temperature=0.1
                    ),
                    timeout=10.0
                )
                reply = sanitize_text(response.choices[0].message.content or "")
                return "да" in reply.lower()

        except Exception as e:
            logger.warning(f"Проверка релевантности: Модель {model_id} недоступна ({e}). Иду к следующей...")
            continue

    return False


# ==========================================
# БИЛЛИНГ И АДМИН-ПАНЕЛЬ
# ==========================================

async def zombie_allowed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скрытая команда выдачи вечного доступа (только для админа)"""
    if update.effective_user.id != ADMIN_ID:
        return  # Если это не владелец, бот промолчит

    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    await grant_lifetime_access(chat_id, chat_type)
    await update.message.reply_text("🧟‍♂️ <b>Доступ разрешен!</b>\nЭтому чату выдан вечный VIP-пропуск.",
                                    parse_mode='HTML')


async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    # ❗️ ЗАМЕНИ номер телефона на свой реальный номер по СБП
    text = (
        "💳 <b>Оформление подписки</b>\n\n"
        "Переведите нужную сумму по СБП (Сбер/Т-Банк) на номер:\n"
        "<code>+7-922-720-12-55</code>\n\n"
        "После перевода нажмите кнопку ниже, чтобы я отправил запрос на проверку."
    )

    # Бот понимает, где его вызвали, и предлагает нужный тариф
    if chat_type != "private":
        kb = [[InlineKeyboardButton("💵 Я оплатил 400₽ (Группа 30 дней)", callback_data=f"paid:group:{chat_id}")]]
    else:
        kb = [[InlineKeyboardButton("💵 Я оплатил 100₽ (Личный 30 дней)", callback_data=f"paid:private:{chat_id}")]]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')


async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, tariff_type, chat_id_str = query.data.split(":")

    user = query.from_user
    user_name = user.full_name
    # Достаем @никнейм, если он установлен у юзера
    username_str = f" (@{user.username})" if user.username else ""
    user_id = user.id

    # Достаем название чата (если это группа)
    chat = query.message.chat
    chat_title_str = f"\nНазвание группы: <b>{chat.title}</b>" if chat.type != "private" else ""

    await query.edit_message_text("⏳ Заявка отправлена администратору. Ожидайте подтверждения (обычно 5-15 минут).")

    # Формируем расширенное сообщение ТЕБЕ В ЛИЧКУ
    admin_text = (
        f"💰 <b>НОВАЯ ЗАЯВКА НА ОПЛАТУ!</b>\n\n"
        f"От кого: {user_name}{username_str} (ID: <code>{user_id}</code>)\n"
        f"ID Чата: <code>{chat_id_str}</code>{chat_title_str}\n"
        f"Тип тарифа: <b>{tariff_type.upper()}</b>\n\n"
        f"Проверь баланс. Если деньги пришли, жми кнопку:"
    )

    admin_kb = [
        [InlineKeyboardButton("✅ Подтвердить (Выдать 30 дней)",
                              callback_data=f"admin_confirm:{chat_id_str}:{tariff_type}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject:{chat_id_str}:{user_id}")]
    ]

    try:
        # Отправляем сообщение на твой ADMIN_ID
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=InlineKeyboardMarkup(admin_kb),
                                       parse_mode='HTML')
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление админу: {e}")


async def admin_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Двойная защита: никто кроме тебя не сможет нажать эти кнопки
    if query.from_user.id != ADMIN_ID:
        return

    parts = query.data.split(":")
    action = parts[0]
    target_chat_id = int(parts[1])

    # Сохраняем исходный текст заявки для истории и экранируем спецсимволы
    safe_original_text = html.escape(query.message.text)

    if action == "admin_confirm":
        tariff_type = parts[2]
        # Вызываем функцию начисления 30 дней!
        await add_subscription_days(target_chat_id, tariff_type, 30)

        # Оставляем заявку в истории как подтвержденную (reply_markup=None убирает кнопки)
        new_text = f"✅ <b>ОПЛАТА ПОДТВЕРЖДЕНА (30 дней)</b>\n\n<pre>{safe_original_text}</pre>"
        await query.edit_message_text(new_text, parse_mode='HTML', reply_markup=None)

        # Уведомляем клиента, что бот заработал
        try:
            await context.bot.send_message(chat_id=target_chat_id,
                                           text="🎉 <b>Оплата подтверждена!</b>\nВам начислено 30 дней доступа к ИИ. Приятного общения!",
                                           parse_mode='HTML')
        except Exception:
            pass

    elif action == "admin_reject":
        target_user_id = int(parts[2])  # Тот, кто нажал кнопку "Оплатил"

        # Оставляем заявку в истории как отклоненную
        new_text = f"❌ <b>ОПЛАТА ОТКЛОНЕНА</b>\n\n<pre>{safe_original_text}</pre>"
        await query.edit_message_text(new_text, parse_mode='HTML', reply_markup=None)

        try:
            await context.bot.send_message(chat_id=target_chat_id,
                                           text="❌ <b>Оплата не подтверждена.</b>\nЕсли вы перевели деньги, но заявка отклонена, свяжитесь с администратором @LazyZombie.",
                                           parse_mode='HTML')
        except Exception:
            pass


async def check_expirations_job(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки истекающих подписок"""
    # Ищем тех, кому осталось 24 часа
    expiring_chats = await get_and_mark_expiring_subscriptions(hours_left=24)

    for chat_id_str in expiring_chats:
        try:
            await context.bot.send_message(
                chat_id=int(chat_id_str),
                text="⚠️ <b>Внимание!</b>\nСрок вашей подписки на ИИ-бота истекает менее чем через 24 часа. Чтобы не потерять доступ, вы можете заранее продлить его командой /pay",
                parse_mode='HTML'
            )
            logger.info(f"Отправлено предупреждение об истечении подписки в чат {chat_id_str}")
        except Exception as e:
            logger.error(f"Не удалось отправить предупреждение {chat_id_str} (возможно, бот заблокирован): {e}")

# ==========================================
# ОБРАБОТЧИКИ TELEGRAM
# ==========================================

"""Скрытая команда /setting"""
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin_rights(update, context):
        await update.message.reply_text("❌ У вас нет прав для изменения настроек.")
        return

    chat_key = update.effective_user.id if update.effective_chat.type == 'private' else update.effective_chat.id

    auto_fallback = await get_auto_fallback(chat_key)
    keep_context = await get_keep_context(chat_key)
    respond_all = await get_respond_all(chat_key)
    silent_responses = await get_silent_responses(chat_key)  # Новое

    btn1 = "🟢 Авто-переключение ИИ: ВКЛ" if auto_fallback else "🔴 Авто-переключение ИИ: ВЫКЛ"
    btn2 = "🟢 Контекст при смене: СОХРАНЯТЬ" if keep_context else "🔴 Контекст при смене: УДАЛЯТЬ"
    btn3 = "🟢 Отвечать на всё: ВКЛ" if respond_all else "🔴 Отвечать на всё: ВЫКЛ"
    btn4 = "🟢 Тихие ответы: ВКЛ" if silent_responses else "🔴 Тихие ответы: ВЫКЛ"

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn1, callback_data="setting:auto_fallback")],
        [InlineKeyboardButton(btn2, callback_data="setting:keep_context")],
        [InlineKeyboardButton(btn3, callback_data="setting:respond_all")],
        [InlineKeyboardButton(btn4, callback_data="setting:silent_responses")]  # Новое
    ])

    await update.message.reply_text("⚙️ <b>Настройки чата:</b>", reply_markup=markup, parse_mode='HTML')


"""Обработчик кнопок меню настроек"""


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_admin_rights(update, context):
        await query.answer("❌ У вас нет прав!", show_alert=True)
        return

    chat_key = query.from_user.id if query.message.chat.type == 'private' else query.message.chat.id

    _, setting_type = query.data.split(":")

    if setting_type == "auto_fallback":
        await toggle_auto_fallback(chat_key)
    elif setting_type == "keep_context":
        await toggle_keep_context(chat_key)
    elif setting_type == "respond_all":
        await toggle_respond_all(chat_key)
    elif setting_type == "silent_responses":  # Новое
        await toggle_silent_responses(chat_key)

    auto_fallback = await get_auto_fallback(chat_key)
    keep_context = await get_keep_context(chat_key)
    respond_all = await get_respond_all(chat_key)
    silent_responses = await get_silent_responses(chat_key)  # Новое

    btn1 = "🟢 Авто-переключение ИИ: ВКЛ" if auto_fallback else "🔴 Авто-переключение ИИ: ВЫКЛ"
    btn2 = "🟢 Контекст при смене: СОХРАНЯТЬ" if keep_context else "🔴 Контекст при смене: УДАЛЯТЬ"
    btn3 = "🟢 Отвечать на всё: ВКЛ" if respond_all else "🔴 Отвечать на всё: ВЫКЛ"
    btn4 = "🟢 Тихие ответы: ВКЛ" if silent_responses else "🔴 Тихие ответы: ВЫКЛ"  # Новое

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn1, callback_data="setting:auto_fallback")],
        [InlineKeyboardButton(btn2, callback_data="setting:keep_context")],
        [InlineKeyboardButton(btn3, callback_data="setting:respond_all")],
        [InlineKeyboardButton(btn4, callback_data="setting:silent_responses")]  # Новое
    ])

    await query.edit_message_reply_markup(reply_markup=markup)

async def get_keep_context(chat_key):
    """Возвращает статус передачи контекста (по умолчанию True)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT keep_context FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else True

async def toggle_keep_context(chat_key):
    """Переключает статус передачи контекста и возвращает новое значение"""
    current = await get_keep_context(chat_key)
    new_val = 0 if current else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, keep_context)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET keep_context = excluded.keep_context
                         """, (str(chat_key), new_val))
        await db.commit()
    return bool(new_val)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "Пользователь"
    chat_id = message.chat_id
    chat_type = message.chat.type
    history_key = user_id if chat_type == "private" else chat_id

    # --- НАЧАЛО: Группировка (буферизация) быстрых сообщений ---
    if history_key not in user_buffers:
        user_buffers[history_key] = []

    user_buffers[history_key].append(message)
    current_len = len(user_buffers[history_key])

    # Ждем 1.5 секунды, чтобы собрать все сообщения из серии (пересылка + текст или фотоальбом)
    await asyncio.sleep(1.5)

    # Если список вырос за время ожидания, значит текущий поток не последний, уступаем выполнение
    if len(user_buffers.get(history_key, [])) != current_len:
        return

    # Забираем все накопленные сообщения
    messages = user_buffers.pop(history_key, [])

    text = ""
    media_msg = messages[-1]
    is_reply_to_bot = False

    # Склеиваем текст и ищем медиа со всех полученных сообщений
    for msg in messages:
        part_text = msg.text or msg.caption or ""
        if part_text:
            text += part_text + "\n\n"

        if msg.photo or msg.document or msg.voice or msg.audio:
            media_msg = msg

        if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == context.bot.id:
            is_reply_to_bot = True

    text = text.strip()

    # === МАГИЯ РЕПЛАЕВ И ПЕРЕСЫЛОК ===
    if media_msg.reply_to_message:
        replied_text = media_msg.reply_to_message.text or media_msg.reply_to_message.caption or ""
        if replied_text:
            if text:
                text = f"{text}\n\n[Контекст из пересланного/отвеченного сообщения]:\n{replied_text}"
            else:
                text = f"[Пересланное/отвеченное сообщение]:\n{replied_text}"

    # Умный поиск медиа в реплаях
    if not (media_msg.photo or media_msg.document or media_msg.voice or media_msg.audio) and media_msg.reply_to_message:
        media_msg = media_msg.reply_to_message

    # === ОБРАБОТКА ГОЛОСОВЫХ И АУДИО (Через Groq Whisper) ===
    if media_msg.voice or media_msg.audio:
        audio_obj = media_msg.voice if media_msg.voice else media_msg.audio

        if audio_obj.file_size and audio_obj.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("⚠️ Аудио слишком большое (лимит 20 МБ).")
            return

        file = await audio_obj.get_file()
        audio_bytes = bytes(await file.download_as_bytearray())

        wait_msg = await update.message.reply_text("🎧 Слушаю голосовое сообщение...", disable_notification=True)
        try:
            transcript = await groq_client.audio.transcriptions.create(
                file=('audio.ogg', audio_bytes, 'audio/ogg'),
                model="whisper-large-v3",
                response_format="text"
            )
            # Добавляем явный контекст, чтобы ИИ понимал формат исходного сообщения
            text = (
                        text + f"\n\n[Пользователь отправил голосовое сообщение. Расшифровка]:\n{transcript.strip()}").strip()
            print(text)
            await wait_msg.delete()
        except Exception as e:
            logger.error(f"Ошибка Whisper: {e}")
            await wait_msg.edit_text("⚠️ Ошибка распознавания голоса.")
            return

    # === ОБРАБОТКА ПРИКРЕПЛЕННЫХ ФАЙЛОВ ===
    elif media_msg.document:
        doc = media_msg.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("⚠️ Файл слишком большой (лимит 20 МБ).")
            return

        wait_msg = await update.message.reply_text(f"📄 Читаю файл {doc.file_name}...", disable_notification=True)
        try:
            file = await doc.get_file()
            file_bytes = bytes(await file.download_as_bytearray())

            # Если это PDF
            if doc.mime_type == 'application/pdf':
                pdf = pymupdf.open(stream=file_bytes, filetype="pdf")
                doc_text = "".join([page.get_text() for page in pdf])
                text += f"\n\n[Содержимое прикрепленного PDF файла {doc.file_name}]:\n{doc_text[:6000]}..."

            # Если это Word документ (DOCX)
            elif doc.file_name.lower().endswith('.docx'):
                docx_file = io.BytesIO(file_bytes)
                document = Document(docx_file)
                doc_text = "\n".join([para.text for para in document.paragraphs])
                text += f"\n\n[Содержимое прикрепленного Word документа {doc.file_name}]:\n{doc_text[:6000]}..."

                # Если это текстовый файл, код или логи (.txt, .py, .log, .json и т.д.)
            else:
                try:
                    doc_text = file_bytes.decode('utf-8')
                    text += f"\n\n[Содержимое прикрепленного файла {doc.file_name}]:\n{doc_text[:6000]}..."
                except UnicodeDecodeError:
                    # Вместо pass добавляем системное сообщение для ИИ!
                    text += f"\n\n[Системное сообщение: Пользователь прикрепил файл {doc.file_name}, но это неизвестный бинарный формат. Бот не смог извлечь из него текст. Сообщи об этом пользователю.]"
                await wait_msg.delete()
        except Exception as e:
            logger.error(f"Ошибка чтения файла: {e}")
            await wait_msg.edit_text("⚠️ Ошибка при чтении файла.")
            return

    text = text.strip()

    # Финальная защита: если нет ни текста (включая извлеченный из файлов/голоса), ни картинки - выходим
    if not text and not media_msg.photo:
        return

    # Получаем настройку тихих ответов перед отправкой
    is_silent = await get_silent_responses(history_key)

    # ПРОВЕРКА ДОСТУПА (БИЛЛИНГ)
    has_access = await consume_request_and_check(history_key, chat_type)
    if not has_access:
        # Если это личка - показываем меню оплаты. Если группа - просим админов оплатить.
        tariff_msg = (
            "⭐️ <b>Доступ ограничен</b>\n\n"
            "Ваши пробные запросы закончились, или срок подписки истек. "
            "Чтобы бот снова начал отвечать, необходимо оформить подписку. Введите /pay для просмотра тарифов."
        )
        # Отвечаем юзеру только если он напрямую тегнул бота или это личка,
        # чтобы бот не спамил об оплате на каждое сообщение в группе
        if is_reply_to_bot or chat_type == "private" or (f"@{context.bot.username.lower()}" in text.lower()):
            await update.message.reply_text(tariff_msg, parse_mode='HTML')
        return


    now = datetime.now(timezone.utc)
    last_time = last_request_time.get(history_key)
    if last_time and (now - last_time).total_seconds() < RATE_LIMIT_SECONDS_PER_USER:
        return
    last_request_time[history_key] = now

    # СКАЧИВАНИЕ КАРТИНКИ
    image_bytes = None
    if media_msg.photo:
        # Берем самую большую версию картинки [-1]
        photo_file = await media_msg.photo[-1].get_file()  # <--- ИМЕННО MEDIA_MSG!
        image_bytes = bytes(await photo_file.download_as_bytearray())
        # Если юзер скинул просто фото без текста, даем ИИ скрытую системную инструкцию
        if not text:
            text = (
                "[Системная пометка: Пользователь отправил картинку без текста. "
                "Изучи её и отреагируй как живой участник чата. Обязательно учитывай наш предыдущий контекст диалога. "
                "Если это мем или шутка — посмейся или ответь встречной шуткой. "
                "Если текст или интерфейс на иностранном языке — помоги перевести или объясни суть. "
                "Если это просто фото — прокомментируй его по-человечески. "
                "СТРОГО ЗАПРЕЩЕНО использовать фразы вроде 'На картинке изображено', 'Я вижу', 'Здесь показано'. Отвечай естественно.]"
            )

    try:
        # --- ПРОВЕРКА СОСТОЯНИЯ ТИШИНЫ ---
        is_silenced = False
        if chat_id in silenced_chats:
            if datetime.now(timezone.utc) < silenced_chats[chat_id]:
                is_silenced = True
            else:
                # Если 10 минут прошло, удаляем чат из списка молчащих
                del silenced_chats[chat_id]

        # Вырезаем все ссылки из текста, чтобы проверить, есть ли там реальный вопрос
        text_without_links = re.sub(r'https?://\S+', '', text).strip()
        has_real_question = "?" in text_without_links

        if chat_type != "private":
            if (f"@{context.bot.username.lower()}" in text.lower()) or is_reply_to_bot:
                # Прямые упоминания и реплаи игнорируют тишину!
                prompt_text = f"{user_name} (ID: {user_id}) пишет: {text}"
                is_short = False
            elif is_silenced:
                # Если чат на паузе, игнорируем всё остальное
                return
            elif await get_respond_all(history_key):
                # Если включен режим "Отвечать на всё", бот реагирует на каждое сообщение
                prompt_text = f"{user_name} (ID: {user_id}) пишет: {text}"
                is_short = False
            # Проверяем наличие вопроса ТОЛЬКО в очищенном от ссылок тексте!
            elif has_real_question and (not text.lower().startswith('@')) and (
                await is_bot_relevant(text, history_key)):
                prompt_text = f"{user_name} (ID: {user_id}) пишет: {text}\n\nОтветь кратко, 1-2 предложениями."
                is_short = True
            else:
                return
        else:
            prompt_text = text
            is_short = False

        user_lock = await ensure_user_lock(history_key)

        # 🛑 ЗАЩИТА: Если мы уже генерируем ответ этому юзеру/чату - просим подождать
        if user_lock.locked():
            await update.message.reply_text("⏳ Я еще думаю над прошлым вопросом, подождите немного...")
            return

        async with user_lock:
            cancel_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🛑 Отмена", callback_data=f"cancel:{history_key}")]])
            wait_msg = await update.message.reply_text("⏳ Ждём ответ...", reply_markup=cancel_kb, disable_notification=True)

            stop_event = asyncio.Event()
            typing_task = asyncio.create_task(typing_sender(chat_id, context, stop_event))

            # === МАГИЯ WEB-ПАРСЕРА ===
            # Прогоняем текст через парсер ссылок перед отправкой в ИИ
            enriched_prompt, parsed_image_bytes = await process_message_for_urls(prompt_text)

            # Если юзер не прикрепил картинку напрямую в телегу, но кинул ссылку на неё - берем картинку по ссылке
            final_image_bytes = image_bytes if image_bytes else parsed_image_bytes

            # Передаем обогащенный промпт (с текстом сайтов) и картинку в LLM
            gen_task = asyncio.create_task(
                ask_llm(history_key, enriched_prompt, chat_type, user_name, final_image_bytes))
            active_tasks[history_key] = gen_task

            try:
                reply = await gen_task
            except asyncio.CancelledError:
                # Если задачу принудительно отменили кнопкой
                stop_event.set()
                typing_task.cancel()
                return
            finally:
                active_tasks.pop(history_key, None)

        stop_event.set()
        typing_task.cancel()

        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        try:
            await wait_msg.delete()
        except Exception:
            pass

        # ПАРСИНГ РЕАКЦИИ ОТ ИИ
        match = re.search(r'\[REACTION:\s*(.*?)\]', reply)
        if match:
            # Очищаем эмодзи от случайных пробелов
            reaction_emoji = match.group(1).strip()
            # Вырезаем тег из текста
            reply = re.sub(r'\[REACTION:\s*.*?\]\s*', '', reply).strip()

            try:
                # Ставим реакцию
                await message.set_reaction(reaction=[ReactionTypeEmoji(reaction_emoji)])
            except Exception as e:
                logger.warning(f"Не удалось поставить реакцию {reaction_emoji}: {e}")

        if not reply:
            return

        if is_short:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔇 Тише", callback_data=f"silence:{chat_id}:{message.message_id}"),
                InlineKeyboardButton("📖 Подробнее", callback_data=f"more:{message.message_id}:{user_id}")
            ]])
            await update.message.reply_text(reply, parse_mode='HTML', reply_markup=keyboard,
                                            disable_notification=is_silent)
        else:
            for part in split_message(reply):
                await update.message.reply_text(part, parse_mode='HTML', disable_notification=is_silent)

    except Exception as e:
        logger.exception("Ошибка в handle_message")


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # Обязательно! Убирает анимацию загрузки на кнопке

    try:
        _, target_key = query.data.split(":")
        target_key = int(target_key)
        if target_key in active_tasks:
            active_tasks[target_key].cancel()
            await query.edit_message_text("🛑 Генерация отменена пользователем.")
        else:
            await query.edit_message_text("🛑 Запрос уже завершен или отменен.")
    except Exception as e:
        logger.exception("Ошибка в кнопке Отмена")


async def expand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, msg_id_str, user_id_str = query.data.split(':')
        original_msg_id = int(msg_id_str)
        chat_id = query.message.chat_id
        history_key = int(user_id_str) if query.message.chat.type == 'private' else chat_id

        # 1. Захватываем текст короткого ответа прямо из сообщения с кнопкой
        bot_short_answer = query.message.text or ""

        await query.edit_message_reply_markup(reply_markup=None)
        wait_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Собираю подробный ответ...",
            reply_to_message_id=original_msg_id,
            disable_notification=True
        )

        # 2. Явно передаем этот текст нейросети, чтобы она знала, о чем речь
        prompt = (
            "Пожалуйста, распиши вот этот свой краткий ответ максимально подробно "
            f"и развернуто. Дай больше деталей:\n\n«{bot_short_answer}»"
        )

        reply = await ask_llm(history_key, prompt, query.message.chat.type)

        is_silent = await get_silent_responses(history_key)

        await wait_msg.delete()
        for part in split_message(reply):
            await context.bot.send_message(chat_id=chat_id, text=part, parse_mode='HTML',
                                           disable_notification=is_silent)

    except Exception as e:
        logger.exception("Ошибка при обработке кнопки подробнее")
        await context.bot.send_message(chat_id=query.message.chat.id, text="⚠️ Ошибка при формировании ответа.")


async def silence_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        parts = query.data.split(":")
        action = parts[0]
        chat_id = int(parts[1])

        # Читаем текущую клавиатуру
        existing_markup = query.message.reply_markup
        new_inline_keyboard = []

        if action == "silence":
            # 1. Устанавливаем тишину, используя константу SILENCE_MINUTES
            silenced_chats[chat_id] = datetime.now(timezone.utc) + timedelta(minutes=SILENCE_MINUTES)

            # 2. Меняем кнопку на информативную с крестиком отмены
            for row in existing_markup.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data and btn.callback_data.startswith("silence:"):
                        new_text = f"🔊 Молчу {SILENCE_MINUTES} мин. (❌)"
                        new_row.append(InlineKeyboardButton(new_text,
                                                            callback_data=btn.callback_data.replace("silence:",
                                                                                                    "unsilence:")))
                    else:
                        new_row.append(btn)
                new_inline_keyboard.append(new_row)

        elif action == "unsilence":
            # 1. Досрочно снимаем тишину
            if chat_id in silenced_chats:
                del silenced_chats[chat_id]

            # 2. Возвращаем исходную кнопку
            for row in existing_markup.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data and btn.callback_data.startswith("unsilence:"):
                        new_row.append(InlineKeyboardButton("🔇 Тише",
                                                            callback_data=btn.callback_data.replace("unsilence:",
                                                                                                    "silence:")))
                    else:
                        new_row.append(btn)
                new_inline_keyboard.append(new_row)

        if new_inline_keyboard:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_inline_keyboard))

    except Exception as e:
        logger.error(f"Ошибка в silence_callback: {e}")


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Проверяем права на управление настройками (в ЛС всегда True, в группе - только админы)
    can_change_settings = await check_admin_rights(update, context)

    # 1. Базовая справка для всех
    help_text = (
        "🤖 <b>Умный ИИ-ассистент</b>\n\n"
        "Я умею поддерживать диалог, запоминать контекст и <b>видеть картинки</b> (отправь фото с текстом или мем, и я пойму, что там изображено).\n\n"
        "В группах я не влезаю в каждую беседу. Я отвечаю только если меня тегнуть, ответить на мое сообщение или задать осмысленный вопрос со знаком «?». А еще я умею реагировать эмодзи!\n\n"
        "<b>Команды:</b>\n"
        "🔹 /model — выбрать нейросеть (Gemini, Groq, OpenRouter)\n"
        "🔹 /reset — начать диалог с чистого листа (сбросить память)\n"
        "🔹 /pay — оформить или продлить подписку\n"
        "🔹 /help — показать эту справку"
    )

    # 2. Блок настроек (видят пользователи в ЛС и админы в группах)
    if can_change_settings:
        help_text += (
            "\n\n⚙️ <b>Настройки чата:</b>\n"
            "🔹 /setting — управление режимами ИИ и памятью"
        )

    # 3. Секретный блок ВЛАДЕЛЬЦА (видишь только ТЫ)
    if ADMIN_ID and user_id == ADMIN_ID:
        help_text += (
            "\n\n👑 <b>Управление биллингом (только для владельца):</b>\n"
            "🧟‍♂️ /zombie_allowed — выдать текущему чату вечный VIP-доступ"
        )

    await update.message.reply_text(help_text, parse_mode='HTML')


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name

    welcome_text = (
        f"Привет, <b>{user_name}</b>! 👋\n\n"
        "Я — твой умный ИИ-помощник. Я умею поддерживать диалог, запоминать контекст нашей беседы и <b>видеть картинки</b> (просто скинь мне фото или мем!).\n\n"
        "Напиши свой первый вопрос или отправь /help, чтобы посмотреть список команд."
    )

    # Добавляем инлайн-кнопку, чтобы сразу вовлечь пользователя
    keyboard = [[InlineKeyboardButton("⚙️ Выбрать нейросеть",
                                      callback_data="setting:models_placeholder")]]  # Можно просто направить на команду /model

    await update.message.reply_text(welcome_text, parse_mode='HTML')

async def reset_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.effective_user.id if update.effective_chat.type == 'private' else update.effective_chat.id
    await clear_history_in_db(key)
    await update.message.reply_text("🧼 Контекст диалога очищен!")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    models = load_models_config()

    # Задайте нужное количество столбцов (2 или 3)
    COLUMNS = 2

    # Создаем плоский список всех кнопок
    buttons = [
        InlineKeyboardButton(m["name"], callback_data=f"set_model:{m['id']}")
        for m in models
    ]

    # Разбиваем список на строки по COLUMNS кнопок в каждой
    keyboard = [buttons[i:i + COLUMNS] for i in range(0, len(buttons), COLUMNS)]

    await update.message.reply_text(
        "Выберите активную модель:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def set_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, model_id = query.data.split(":", 1)
    key = query.from_user.id if query.message.chat.type == 'private' else query.message.chat.id
    # Сохраняем выбор пользователя в БД
    current_model_id = await get_user_model(key)
    await set_user_model(key, model_id)

    # Проверяем настройку сохранения контекста
    keep_context = await get_keep_context(key)
    context_msg = ""

    # Если сохранение выключено И модель реально изменилась - сбрасываем базу
    if not keep_context and current_model_id != model_id:
        await clear_history_in_db(key)
        context_msg = "\n🧼 <i>(Контекст прошлого диалога удален)</i>"
    # Ищем полную информацию о выбранной модели в конфиге
    all_models = load_models_config()
    selected_model = next((m for m in all_models if m["id"] == model_id), None)

    if selected_model:
        name = selected_model["name"]
        provider = selected_model["provider"].upper()
    else:
        name = model_id
        provider = "UNKNOWN"

    await query.message.reply_text(
        f"✅ Установлена модель: <b>{name}</b>\n({provider}: <code>{model_id}</code>){context_msg}",
        parse_mode='HTML'
    )


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.id == context.bot.id: continue
        prompt = (
            f"В нашу группу только что вступил пользователь по имени {member.full_name}. "
            "Напиши для него очень короткое, креативное и теплое приветствие. "
            "Обязательно используй 1-2 эмодзи. СТРОГОЕ ПРАВИЛО: твой ответ должен состоять максимум из 1 или 2 предложений. "
            "Не задавай ему лишних вопросов и не пиши 'Привет, я искусственный интеллект'."
        )
        wait_msg = await update.message.reply_text("⏳ Генерирую приветствие...")
        reply = await ask_llm(update.effective_chat.id, prompt, chat_type="group")
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await update.message.reply_text(reply, parse_mode='HTML')

async def check_youtube_cookies_job(context: ContextTypes.DEFAULT_TYPE):
    await web_parser.check_cookies_health()

async def on_startup(application):
    try:
        load_models_config()
    except Exception as e:
        logger.critical(e)
        sys.exit(1)
    web_parser.configure_admin_alerts(bot_token=TELEGRAM_TOKEN, admin_chat_id=ADMIN_ID)
    await init_db() # База истории сообщений
    await init_billing_db() # База подписок покупки
    commands = [
        BotCommand("reset", "Сбросить историю диалога"),
        BotCommand("help", "Показать список команд"),
        BotCommand("model", "Выбрать модель"),
    ]
    await application.bot.set_my_commands(commands)


def main():
    # 1. Увеличиваем таймауты ожидания ответа от Telegram,
    # чтобы бот не падал из-за кратковременных лагов сети
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )

    # === ЗАПУСК ФОНОВОЙ ПРОВЕРКИ ===
    # Запускаем проверку каждые 3600 секунд (1 час).
    # first=10 означает, что первая проверка пройдет через 10 секунд после старта бота
    app.job_queue.run_repeating(check_expirations_job, interval=3600, first=10)
    app.job_queue.run_daily(check_youtube_cookies_job, time=time(hour=9, minute=0))

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("reset", reset_history))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("setting", settings_command))
    app.add_handler(CommandHandler("zombie_allowed", zombie_allowed_command))
    app.add_handler(CommandHandler("pay", pay_command))
    app.add_handler(CallbackQueryHandler(pay_callback, pattern="^paid:"))
    app.add_handler(CallbackQueryHandler(admin_confirm_callback, pattern="^admin_(confirm|reject):"))
    app.add_handler(CallbackQueryHandler(set_model_callback, pattern="^set_model:"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^setting:"))
    app.add_handler(CallbackQueryHandler(silence_callback, pattern="^(silence|unsilence):"))
    app.add_handler(CallbackQueryHandler(expand_callback, pattern="^more:"))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern="^cancel:", block=False))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO) & ~filters.COMMAND, handle_message, block=False))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    app.post_init = on_startup
    logger.info("Бот с динамической загрузкой моделей и настройками запущен ✅")

    # 2. drop_pending_updates=True заставляет бота ИГНОРИРОВАТЬ
    # все сообщения, которые были написаны, пока он был выключен.
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()