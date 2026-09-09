import logging
import os
import sys
import asyncio
import json
import re
import base64
from datetime import datetime, timezone, timedelta
import aiosqlite
from dotenv import load_dotenv
from telegram import ReactionTypeEmoji

from google import genai
from google.genai import types
from openai import AsyncOpenAI
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
    CommandHandler,
    CallbackQueryHandler,
)

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
        "\n\n[СЕКРЕТНАЯ ИНСТРУКЦИЯ: Если ты считаешь уместным отреагировать на сообщение пользователя эмоцией, "
        "начни свой ответ с тега [REACTION: эмодзи]. Безопасные разрешенные эмодзи: "
        "👍, 👎, ❤️, 🔥, 👏, 😁, 🤔, 🤯, 😱, 🤬, 😢, 🎉, 🤩, 🤮, 💩, 🙏, 👌, 🤡, 🤣, ⚡, 🏆, 💔, 🤨, 😐, 😴, 😭, 🤓, 👻, 👀, 🤝, 🫡, 🗿. "
        "\n❗️ ВАЖНО: Если сообщение пользователя не требует текстового ответа (например, это просто смешной мем, "
        "фотография без контекста или подтверждение 'ок'/'понял'), ты ДОЛЖЕН ответить ТОЛЬКО тегом реакции (например, '[REACTION: 🤣]') "
        "и больше ничего не писать. Не комментируй мемы текстом, если достаточно просто посмеяться реакцией!"
        "Если реакция не нужна, просто пиши ответ без тега.]"
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

    current_model_id = user_selected_model.get(user_or_chat_id)
    if not current_model_id:
        current_model_id = load_models_config()[0]["id"]

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
                    user_selected_model[user_or_chat_id] = model_id

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

    current_model_id = user_selected_model.get(chat_id)
    if not current_model_id:
        current_model_id = load_models_config()[0]["id"]

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

    btn1 = "🟢 Авто-переключение ИИ: ВКЛ" if auto_fallback else "🔴 Авто-переключение ИИ: ВЫКЛ"
    btn2 = "🟢 Контекст при смене: СОХРАНЯТЬ" if keep_context else "🔴 Контекст при смене: УДАЛЯТЬ"

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn1, callback_data="setting:auto_fallback")],
        [InlineKeyboardButton(btn2, callback_data="setting:keep_context")]
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

    # Определяем, какую именно кнопку нажали
    _, setting_type = query.data.split(":")

    if setting_type == "auto_fallback":
        await toggle_auto_fallback(chat_key)
    elif setting_type == "keep_context":
        await toggle_keep_context(chat_key)

    # Получаем актуальные статусы
    auto_fallback = await get_auto_fallback(chat_key)
    keep_context = await get_keep_context(chat_key)

    btn1 = "🟢 Авто-переключение ИИ: ВКЛ" if auto_fallback else "🔴 Авто-переключение ИИ: ВЫКЛ"
    btn2 = "🟢 Контекст при смене: СОХРАНЯТЬ" if keep_context else "🔴 Контекст при смене: УДАЛЯТЬ"

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn1, callback_data="setting:auto_fallback")],
        [InlineKeyboardButton(btn2, callback_data="setting:keep_context")]
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
    # Разрешаем работу, если есть текст ИЛИ фото (caption)
    if not message or (not message.text and not message.photo):
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "Пользователь"
    chat_id = message.chat_id

    # Текст теперь может быть либо в .text, либо в .caption (подпись к фото)
    text = message.text or message.caption or ""
    text = text.strip()

    chat_type = message.chat.type
    history_key = user_id if chat_type == "private" else chat_id

    now = datetime.now(timezone.utc)
    last_time = last_request_time.get(history_key)
    if last_time and (now - last_time).total_seconds() < RATE_LIMIT_SECONDS_PER_USER:
        return
    last_request_time[history_key] = now

    # СКАЧИВАНИЕ КАРТИНКИ
    image_bytes = None
    if message.photo:
        # Берем самую большую версию картинки [-1]
        photo_file = await message.photo[-1].get_file()
        image_bytes = bytes(await photo_file.download_as_bytearray())
        # Если юзер скинул просто фото без текста, даем ИИ базовую команду
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

    is_reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == context.bot.id
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

        if chat_type != "private":
            if (f"@{context.bot.username.lower()}" in text.lower()) or is_reply_to_bot:
                # Прямые упоминания и реплаи игнорируют тишину!
                prompt_text = f"{user_name} (ID: {user_id}) пишет: {text}"
                is_short = False
                # Если это фотка в группу без упоминания бота - игнорируем
            elif ("?" in text) and (not text.lower().startswith('@')) and (not is_silenced) and (
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
            wait_msg = await update.message.reply_text("⏳ Ждём ответ...", reply_markup=cancel_kb)

            stop_event = asyncio.Event()
            typing_task = asyncio.create_task(typing_sender(chat_id, context, stop_event))

            # ПЕРЕДАЕМ КАРТИНКУ В ask_llm
            gen_task = asyncio.create_task(ask_llm(history_key, prompt_text, chat_type, user_name, image_bytes))
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
            await update.message.reply_text(reply, parse_mode='HTML', reply_markup=keyboard)
        else:
            for part in split_message(reply):
                await update.message.reply_text(part, parse_mode='HTML')

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
            reply_to_message_id=original_msg_id
        )

        # 2. Явно передаем этот текст нейросети, чтобы она знала, о чем речь
        prompt = (
            "Пожалуйста, распиши вот этот свой краткий ответ максимально подробно "
            f"и развернуто. Дай больше деталей:\n\n«{bot_short_answer}»"
        )

        reply = await ask_llm(history_key, prompt, query.message.chat.type)

        await wait_msg.delete()
        for part in split_message(reply):
            await context.bot.send_message(chat_id=chat_id, text=part, parse_mode='HTML')

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
    help_text = "🤖 <b>Я - бот с ИИ на базе Gemini, Groq, Openrouter</b>\n\nКоманды:\n/reset - сбросить контекст\n/model - выбрать модель\n/help - помощь\n"
    await update.message.reply_text(help_text, parse_mode='HTML')


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
    # Сохраняем выбор пользователя
    current_model_id = user_selected_model.get(key)
    user_selected_model[key] = model_id

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
        prompt = f"В группу вступил {member.full_name}. Напиши короткое дружелюбное приветствие."
        wait_msg = await update.message.reply_text("⏳ Генерирую приветствие...")
        reply = await ask_llm(update.effective_chat.id, prompt, chat_type="group")
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await update.message.reply_text(reply, parse_mode='HTML')


async def on_startup(application):
    try:
        load_models_config()
    except Exception as e:
        logger.critical(e)
        sys.exit(1)
    await init_db()
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

    app.add_handler(CommandHandler("reset", reset_history))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("setting", settings_command))
    app.add_handler(CallbackQueryHandler(set_model_callback, pattern="^set_model:"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^setting:"))
    app.add_handler(CallbackQueryHandler(silence_callback, pattern="^(silence|unsilence):"))
    app.add_handler(CallbackQueryHandler(expand_callback, pattern="^more:"))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern="^cancel:", block=False))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_message, block=False))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    app.post_init = on_startup
    logger.info("Бот с динамической загрузкой моделей и настройками запущен ✅")

    # 2. drop_pending_updates=True заставляет бота ИГНОРИРОВАТЬ
    # все сообщения, которые были написаны, пока он был выключен.
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()