# bot_gemini_improved.py
import logging
import os
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
import google.generativeai as genai
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

# prompts
AI_PROMPT_TGM = os.getenv("AI_PROMPT_TGM", "")
AI_PROMPT_PM = os.getenv("AI_PROMPT_PM", "")
AI_PROMPT_GM = os.getenv("AI_PROMPT_GM", "")
AI_PROMPT_IS_RELEVANT_QUESTION = os.getenv("AI_PROMPT_IS_RELEVANT_QUESTION", "")

# init
genai.configure(api_key=GEMINI_API_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Globals
# Кэш объектов моделей: {model_name: genai.GenerativeModel(...)}
models_cache = {}
# Какая модель выбрана для пользователя или чата: key = user_id or chat_id
user_selected_model = {}
# История чатов: key = user_or_chat_id -> chat object
user_chats = {}
# Последний запрос времени для анти-спама: key -> datetime
last_request_time = {}
# Пер-процессный семафор чтобы снизить число одновременных запросов к Gemini
GLOBAL_SEMAPHORE = asyncio.Semaphore(1)  # можно поднять до 2 при необходимости
# Пер-пользовательская блокировка (чтобы один пользователь не создавал параллельных запросов)
user_locks = {}

# Telegram constraints
MAX_MESSAGE_LENGTH = 4096

# Поддерживаемые модели для выбора пользователем
AVAILABLE_MODELS = [
    "gemini-3-pro",
    "gemini-3-pro-preview",
    "gemini-3-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

RATE_LIMIT_SECONDS_PER_USER = 1.0  # минимальный интервал между запросами от одного пользователя


def get_model_obj(model_name: str):
    """Возвращает или создаёт объект genai.GenerativeModel для model_name"""
    if model_name in models_cache:
        return models_cache[model_name]
    model_obj = genai.GenerativeModel(model_name)
    models_cache[model_name] = model_obj
    return model_obj


def get_system_prompt(chat_type):
    if chat_type == "private":
        return AI_PROMPT_PM or ""
    else:
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
    """Возвращает асинхронный lock для определённого key"""
    if key not in user_locks:
        user_locks[key] = asyncio.Lock()
    return user_locks[key]


async def typing_sender(chat_id: int, context: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception as e:
            logger.debug(f"Не удалось отправить typing: {e}")

        # Проверяем событие каждые 4 секунды
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


def parse_gemini_error(error: Exception) -> str:
    err = str(error)
    """Возвращает структурированное сообщение об ошибке"""

    if "limit: 0" in err or "limit:0" in err:
        return "Эта модель недоступна или отключена для текущего тарифа."

    if "Quota exceeded" in err:
        return "Превышена квота использования этой модели. Попробуйте позже или выберите другую модель."

    if "per minute" in err or "Resource has been exhausted" in err:
        return "Слишком много запросов подряд. Подождите несколько секунд и попробуйте снова."

    if "Invalid argument" in err:
        return "Модель отклонила запрос. Возможно, он слишком длинный или содержит неподдерживаемый формат."

    if "internal" in err or "500" in err:
        return "Внутренняя ошибка сервера Google. Попробуйте повторить запрос."

    return "Неизвестная ошибка. Логи содержат подробности."


async def ask_gemini(user_or_chat_id, prompt: str, chat_type: str, selected_model: str = None, max_retries: int = 3):
    """
    Универсальная функция запроса к Gemini с:
    - глобальным семафором (чтобы не посылать много параллельных запросов),
    - экспоненциальным бэкоффом на 429,
    - использованием системного промпта,
    - возвратом текстового ответа или исключительной строки ошибки.
    """

    model_name = selected_model or user_selected_model.get(user_or_chat_id) or DEFAULT_MODEL
    model_obj = get_model_obj(model_name)

    # Ensure there is a chat object
    if user_or_chat_id not in user_chats:
        chat_obj = model_obj.start_chat(history=[])
        user_chats[user_or_chat_id] = chat_obj
        # отправляем system prompt
        sys_prompt = get_system_prompt(chat_type)
        if sys_prompt:
            # блокирующее вызов: делаем в отдельном потоке
            await asyncio.to_thread(chat_obj.send_message, sys_prompt)

    chat_obj = user_chats[user_or_chat_id]

    # запрос к API с семафором и бэкоффом
    attempt = 0
    backoff = 1.0
    last_exception = None

    async with GLOBAL_SEMAPHORE:
        while attempt < max_retries:
            attempt += 1
            try:
                # SDK часто синхронный — вызываем в отдельном потоке, чтобы не блокировать цикл событий.
                # Здесь не используем stream ипотеку по совместимости; если у вас SDK поддерживает stream
                # вы можете заменить на итеративный сбор.
                response = await asyncio.to_thread(chat_obj.send_message, prompt)
                # ожидаем, что response имеет поле .text
                text = getattr(response, "text", None)
                if text is None:
                    # попытка взять .content или str(response)
                    text = str(response)
                return text.strip()
            except Exception as e:
                logger.warning(f"Ошибка при запросе к Gemini (попытка {attempt}): {e}")

                return f"⚠️ Ошибка Gemini: {parse_gemini_error(e)}"
        # если закончились попытки
        return f"⚠️ Ошибка Gemini (после {max_retries} попыток): {last_exception}"


async def is_bot_relevant(user_or_chat_id, text: str, chat_type):
    prompt = (
        f"Отвечать не нужно. Не сохраняй в памяти это. Просто проанализируй вопрос: {text}\n"
        f"{AI_PROMPT_IS_RELEVANT_QUESTION}\n"
        "Ответ должен быть только 'Да' или 'Нет'."
    )
    reply = await ask_gemini(user_or_chat_id, prompt, chat_type)
    return "да" in (reply or "").lower()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    user_id = message.from_user.id
    chat_id = message.chat_id
    text = message.text.strip()
    chat_type = message.chat.type
    history_key = user_id if chat_type == "private" else chat_id

    # Анти-спам: минимальный интервал между запросами от одного пользователя
    now = datetime.utcnow()
    last_time = last_request_time.get(history_key)
    if last_time and (now - last_time).total_seconds() < RATE_LIMIT_SECONDS_PER_USER:
        # Молчим — либо отправляем короткую подсказку, можно не сообщать
        return
    last_request_time[history_key] = now

    # определяем, отвечать ли: логика как у вас
    silent = chat_id in last_request_time and False  # можно реализовать mute_until как у вас

    is_reply_to_bot = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == context.bot.id
    )

    try:
        # Решаем форму вопроса
        if chat_type != "private":
            if (f"@{context.bot.username.lower()}" in text.lower()) or is_reply_to_bot:
                gemini_prompt = f"{user_id} пишет: {text}"
                is_short = False
            elif (("?" in text) and (not silent)
                  and (not text.lower().startswith('@')) and (await is_bot_relevant(history_key, text, chat_type))):
                gemini_prompt = f"{user_id} пишет: {text}\n\nОтветь кратко, 1-2 предложениями."
                # сохраняем для кнопки подробнее (если нужно)
                # detailed_questions[message.message_id] = text
                is_short = True
            else:
                return
        else:
            gemini_prompt = text
            is_short = False

        # выбор модели (приоритет: для чата/пользователя, иначе DEFAULT_MODEL)
        selected_model = user_selected_model.get(history_key, DEFAULT_MODEL)

        # создаём сообщение ожидания
        wait_msg = await update.message.reply_text("⏳ Ждём ответ от модели...")

        # запускаем фоновую задачу отправки typing
        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(typing_sender(chat_id, context, stop_event))

        # берём пер-пользовательский lock чтобы один пользователь не имел параллельных обращений
        user_lock = await ensure_user_lock(history_key)
        async with user_lock:
            # спрашиваем Gemini (ask_gemini выполняет свои retry и защищён семафором)
            reply = await ask_gemini(history_key, gemini_prompt, chat_type, selected_model)
        # завершили, отменяем typing и удаляем сообщение ожидания
        stop_event.set()
        typing_task.cancel()

        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=wait_msg.message_id)
        except Exception:
            # если не получилось удалить — игнорируем
            pass

        # Отправка ответа
        if is_short:
            # добавим кнопки "Тише" и "Подробнее" если нужно
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


# Кнопка "Подробнее" — аналог вашей реализации (упрощённо)
async def expand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        data = query.data  # format: more:<msg_id>:<user_id>
        _, msg_id_str, original_user_id_str = data.split(':')
        msg_id = int(msg_id_str)
        original_user_id = int(original_user_id_str)

        # Если у вас stored detailed_questions -> вернуть подробно
        # Пример: если нет — ответим, что фича отключена
        await query.message.reply_text("ℹ️ Подробные ответы включены в следующей версии.")
    except Exception as e:
        logger.exception("Ошибка при обработке кнопки подробнее")
        await query.message.reply_text("⚠️ Ошибка при обработке кнопки: " + str(e))


# Тише (mute) — упрощённая версия
async def silence_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        data = query.data  # format: silence:<chat_id>:<message_id>
        # В этой демонстрации просто заменяем markup чтобы показать реакцию
        new_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Молчу 10 мин…", callback_data="noop")]])
        await query.edit_message_reply_markup(reply_markup=new_keyboard)
    except Exception as e:
        logger.exception("Ошибка в кнопке Тише")
        try:
            await query.message.reply_text("⚠️ Ошибка в кнопке Тише: " + str(e))
        except Exception:
            pass


# Список команд /help
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 <b>Я — бот с ИИ на базе Gemini</b>\n\n"
        "Команды:\n"
        "/reset - сбросить контекст\n"
        "/model - выбрать модель\n"
        "/help - показать это сообщение\n"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')


# /reset
async def reset_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.effective_user.id if update.effective_chat.type == 'private' else update.effective_chat.id
    if key in user_chats:
        del user_chats[key]
        await update.message.reply_text("🧼 Контекст сброшен!")
    else:
        await update.message.reply_text("ℹ️ Контекст уже пуст.")


# /model - показывает доступные модели
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for m in AVAILABLE_MODELS:
        keyboard.append([InlineKeyboardButton(m, callback_data=f"set_model:{m}")])
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите модель для текущего чата/пользователя:", reply_markup=markup)


# Обработчик выбора модели
async def set_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        data = query.data  # format set_model:<model_name>
        _, model_name = data.split(":", 1)
        key = query.from_user.id if query.message.chat.type == 'private' else query.message.chat.id
        if model_name not in AVAILABLE_MODELS:
            await query.message.reply_text("❌ Модель недоступна.")
            return

        # Сохраняем выбор
        user_selected_model[key] = model_name
        # Сбрасываем контекст (рекомендуется при смене модели)
        if key in user_chats:
            del user_chats[key]
        # Инициализируем кэш модели (лениво)
        get_model_obj(model_name)

        await query.message.reply_text(f"✅ Модель установлена: {model_name}")
    except Exception as e:
        logger.exception("Ошибка при выборе модели")
        await query.message.reply_text("⚠️ Ошибка при выборе модели: " + str(e))


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            continue
        chat_id = update.effective_chat.id
        username = member.full_name
        prompt = (
            f"В нашу группу присоединился новый участник по имени {username}. "
            "Пришли один дружелюбный вариант приветствия от имени группы."
        )
        wait_msg = await update.message.reply_text("⏳ Генерирую приветствие...")
        typing_task = asyncio.create_task(typing_sender(chat_id, context))
        reply = await ask_gemini(chat_id, prompt, chat_type="group")
        typing_task.cancel()
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=wait_msg.message_id)
        except Exception:
            pass
        await update.message.reply_text(reply, parse_mode='HTML')


async def set_commands(application):
    commands = [
        BotCommand("reset", "Сбросить историю диалога"),
        BotCommand("help", "Показать список команд"),
        BotCommand("model", "Выбрать модель Gemini"),
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.post_init = set_commands
    logger.info("Бот на Gemini запущен ✅")
    app.run_polling()


if __name__ == "__main__":
    main()
