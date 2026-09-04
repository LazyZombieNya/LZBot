import logging
import os
import asyncio
from datetime import datetime, timezone
import aiosqlite
from dotenv import load_dotenv
import google.generativeai as genai
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
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1")

# Клиент Groq
groq_client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=GROQ_URL)

# Prompts
AI_PROMPT_TGM = os.getenv("AI_PROMPT_TGM", "")
AI_PROMPT_PM = os.getenv("AI_PROMPT_PM", "")
AI_PROMPT_GM = os.getenv("AI_PROMPT_GM", "")
AI_PROMPT_IS_RELEVANT_QUESTION = os.getenv("AI_PROMPT_IS_RELEVANT_QUESTION", "")

# Init Gemini
genai.configure(api_key=GEMINI_API_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Путь к файлу базы данных SQLite
DB_PATH = "chat_history.db"

# Globals
models_cache = {}
user_selected_model = {}
user_chats = {}
last_request_time = {}
GLOBAL_SEMAPHORE = asyncio.Semaphore(1)
user_locks = {}
active_tasks = {}

MAX_MESSAGE_LENGTH = 4096

AVAILABLE_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

RATE_LIMIT_SECONDS_PER_USER = 1.0


# ==========================================
# РАБОТА С БАЗОЙ ДАННЫХ (SQLite)
# ==========================================

async def init_db():
    """Создание таблицы для хранения истории диалогов"""
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
        await db.commit()


async def add_message_to_db(chat_key, role: str, content: str):
    """Сохранение реплики пользователя или ассистента"""
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
    """Чтение последних N сообщений для контекста"""
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
        logger.error(f"Ошибка чтения из БД: {e}")
        return []


async def clear_history_in_db(chat_key):
    """Очистка контекста пользователя в БД"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM messages WHERE chat_key = ?", (str(chat_key),))
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка очистки БД: {e}")


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def get_model_obj(model_name: str):
    if model_name in models_cache:
        return models_cache[model_name]
    model_obj = genai.GenerativeModel(model_name)
    models_cache[model_name] = model_obj
    return model_obj


def get_system_prompt(chat_type):
    if chat_type == "private":
        return AI_PROMPT_PM or ""
    return AI_PROMPT_GM or ""


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


async def ensure_user_lock(key):
    if key not in user_locks:
        user_locks[key] = asyncio.Lock()
    return user_locks[key]


async def typing_sender(chat_id: int, context: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception as e:
            logger.debug(f"Не удалось отправить typing: {e}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


def parse_gemini_error(error: Exception) -> str:
    err = str(error)
    if "limit: 0" in err or "limit:0" in err:
        return "Эта модель недоступна или отключена для текущего тарифа."
    if "Quota exceeded" in err:
        return "Превышена квота использования этой модели. Попробуйте позже."
    if "per minute" in err or "Resource has been exhausted" in err:
        return "Слишком много запросов подряд. Подождите несколько секунд."
    if "Invalid argument" in err:
        return "Модель отклонила запрос."
    if "internal" in err or "500" in err:
        return "Внутренняя ошибка сервера Google."
    return "Неизвестная ошибка Gemini."


# ==========================================
# ОСНОВНАЯ ЛОГИКА ИИ И FALLBACK
# ==========================================

async def ask_gemini(user_or_chat_id, prompt: str, chat_type: str, selected_model: str = None, max_retries: int = 3):
    model_name = selected_model or user_selected_model.get(user_or_chat_id) or DEFAULT_MODEL
    model_obj = get_model_obj(model_name)

    # 1. Если сессия не в памяти — восстанавливаем её из SQLite
    if user_or_chat_id not in user_chats:
        db_history = await get_history_from_db(user_or_chat_id, limit=10)
        gemini_history = []
        for msg in db_history:
            role = "model" if msg["role"] == "assistant" else "user"
            gemini_history.append({"role": role, "parts": [msg["content"]]})

        chat_obj = model_obj.start_chat(history=gemini_history)
        user_chats[user_or_chat_id] = chat_obj

        # Если истории не было, инициализируем системным промптом
        if not gemini_history:
            sys_prompt = get_system_prompt(chat_type)
            if sys_prompt:
                try:
                    await asyncio.to_thread(chat_obj.send_message, sys_prompt)
                except Exception:
                    pass

    chat_obj = user_chats[user_or_chat_id]

    # Сохраняем запрос пользователя в SQLite
    await add_message_to_db(user_or_chat_id, "user", prompt)

    attempt = 0
    backoff = 1.0
    last_exception = None

    async with GLOBAL_SEMAPHORE:
        # Попытки обращения к Gemini
        while attempt < max_retries:
            attempt += 1
            try:
                response = await asyncio.to_thread(chat_obj.send_message, prompt)
                text = getattr(response, "text", None) or str(response)
                reply_text = text.strip()

                # Сохраняем ответ модели в SQLite
                await add_message_to_db(user_or_chat_id, "assistant", reply_text)
                return reply_text
            except Exception as e:
                last_exception = e
                logger.warning(f"Ошибка Gemini (попытка {attempt}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(backoff)
                    backoff *= 2

        # 2. Если все попытки Gemini исчерпаны — срабатывает Fallback на Groq
        logger.info(f"Gemini недоступен. Переключаюсь на Groq для {user_or_chat_id}")
        try:
            db_history = await get_history_from_db(user_or_chat_id, limit=8)
            groq_messages = []
            sys_prompt = get_system_prompt(chat_type)
            if sys_prompt:
                groq_messages.append({"role": "system", "content": sys_prompt})

            for msg in db_history:
                groq_messages.append({"role": msg["role"], "content": msg["content"]})

            groq_response = await groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=groq_messages,
                temperature=0.7
            )
            reply_text = groq_response.choices[0].message.content.strip()

            # Сохраняем ответ Groq в историю базы
            await add_message_to_db(user_or_chat_id, "assistant", reply_text)
            return reply_text + "\n\n<i>(Ответил Groq, так как Gemini временно недоступен)</i>"
        except Exception as groq_err:
            logger.error(f"Ошибка резервного API Groq: {groq_err}")
            return f"⚠️ Ошибка Gemini: {parse_gemini_error(last_exception)}\nРезервный API также недоступен."


async def is_bot_relevant(text: str):
    """Изолированная проверка релевантности без засорения истории сессии"""
    prompt = (
        f"Проанализируй вопрос: {text}\n"
        f"{AI_PROMPT_IS_RELEVANT_QUESTION}\n"
        "Ответ должен быть только 'Да' или 'Нет'."
    )
    try:
        model_obj = get_model_obj("gemini-2.5-flash")
        response = await asyncio.to_thread(model_obj.generate_content, prompt)
        reply = getattr(response, "text", "") or ""
        return "да" in reply.lower()
    except Exception as e:
        logger.warning(f"Ошибка при проверке релевантности: {e}")
        return False


# ==========================================
# ОБРАБОТЧИКИ TELEGRAM
# ==========================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    user_id = message.from_user.id
    chat_id = message.chat_id
    text = message.text.strip()
    chat_type = message.chat.type
    history_key = user_id if chat_type == "private" else chat_id

    # Анти-спам с поддержкой timezone-aware UTC
    now = datetime.now(timezone.utc)
    last_time = last_request_time.get(history_key)
    if last_time and (now - last_time).total_seconds() < RATE_LIMIT_SECONDS_PER_USER:
        return
    last_request_time[history_key] = now

    is_reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == context.bot.id
    )

    try:
        if chat_type != "private":
            if (f"@{context.bot.username.lower()}" in text.lower()) or is_reply_to_bot:
                gemini_prompt = f"{user_id} пишет: {text}"
                is_short = False
            elif ("?" in text) and (not text.lower().startswith('@')) and (await is_bot_relevant(text)):
                gemini_prompt = f"{user_id} пишет: {text}\n\nОтветь кратко, 1-2 предложениями."
                is_short = True
            else:
                return
        else:
            gemini_prompt = text
            is_short = False

        selected_model = user_selected_model.get(history_key, DEFAULT_MODEL)

        cancel_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛑 Отмена", callback_data=f"cancel:{history_key}")
        ]])
        wait_msg = await update.message.reply_text("⏳ Ждём ответ от модели...", reply_markup=cancel_kb)

        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(typing_sender(chat_id, context, stop_event))

        user_lock = await ensure_user_lock(history_key)
        gen_task = asyncio.create_task(ask_gemini(history_key, gemini_prompt, chat_type, selected_model))
        active_tasks[history_key] = gen_task

        try:
            async with user_lock:
                reply = await gen_task
        except asyncio.CancelledError:
            stop_event.set()
            await wait_msg.edit_text("🛑 Генерация была отменена пользователем.")
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
            await context.bot.delete_message(chat_id=chat_id, message_id=wait_msg.message_id)
        except Exception:
            pass

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
        try:
            await update.message.reply_text(f"⚠️ Внутренняя ошибка: {e}")
        except Exception:
            pass


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, target_key = query.data.split(":")
        target_key = int(target_key)
        if target_key in active_tasks:
            active_tasks[target_key].cancel()
            await query.answer("Отменяю запрос...")
        else:
            await query.answer("Нет активной генерации.")
    except Exception as e:
        logger.exception("Ошибка в кнопке Отмена")


async def expand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("ℹ️ Подробные ответы включены в следующей версии.")


async def silence_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        new_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Молчу 10 мин…", callback_data="noop")]])
        await query.edit_message_reply_markup(reply_markup=new_keyboard)
    except Exception as e:
        logger.exception("Ошибка в кнопке Тише")


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 <b>Я — бот с ИИ на базе Gemini и Groq</b>\n\n"
        "Команды:\n"
        "/reset - сбросить контекст\n"
        "/model - выбрать модель\n"
        "/help - помощь\n"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')


async def reset_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.effective_user.id if update.effective_chat.type == 'private' else update.effective_chat.id

    # Очищаем базу данных и оперативную память
    await clear_history_in_db(key)
    if key in user_chats:
        del user_chats[key]
    await update.message.reply_text("🧼 Контекст сброшен из памяти и базы данных!")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(m, callback_data=f"set_model:{m}")] for m in AVAILABLE_MODELS]
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите модель:", reply_markup=markup)


async def set_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, model_name = query.data.split(":", 1)
        key = query.from_user.id if query.message.chat.type == 'private' else query.message.chat.id
        if model_name not in AVAILABLE_MODELS:
            await query.message.reply_text("❌ Модель недоступна.")
            return

        user_selected_model[key] = model_name
        if key in user_chats:
            del user_chats[key]
        get_model_obj(model_name)
        await query.message.reply_text(f"✅ Модель установлена: {model_name}")
    except Exception as e:
        logger.exception("Ошибка при выборе модели")


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            continue
        chat_id = update.effective_chat.id
        prompt = f"В группу вступил {member.full_name}. Напиши короткое дружелюбное приветствие."
        wait_msg = await update.message.reply_text("⏳ Генерирую приветствие...")
        reply = await ask_gemini(chat_id, prompt, chat_type="group")
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=wait_msg.message_id)
        except Exception:
            pass
        await update.message.reply_text(reply, parse_mode='HTML')


async def on_startup(application):
    """Инициализация базы данных и команд бота при запуске"""
    await init_db()
    commands = [
        BotCommand("reset", "Сбросить историю диалога"),
        BotCommand("help", "Показать список команд"),
        BotCommand("model", "Выбрать модель"),
    ]
    await application.bot.set_my_commands(commands)


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("reset", reset_history))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CallbackQueryHandler(set_model_callback, pattern="^set_model:"))
    app.add_handler(CallbackQueryHandler(silence_callback, pattern="^silence:"))
    app.add_handler(CallbackQueryHandler(expand_callback, pattern="^more:"))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern="^cancel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    app.post_init = on_startup
    logger.info("Бот запущен ✅")
    app.run_polling()


if __name__ == "__main__":
    main()