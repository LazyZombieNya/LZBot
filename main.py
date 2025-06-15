import logging
import os
import asyncio
import google.generativeai as genai
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler, \
    CallbackQueryHandler, CallbackContext

load_dotenv()  # Загружаем переменные из .env
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL")
#Промты для задач
AI_PROMPT_TGM = os.getenv("AI_PROMPT_TGM") #Основной промт при запуске нового чата, который объясняет что нейронка находится в телеграмм боте
AI_PROMPT_PM = os.getenv("AI_PROMPT_PM") #Объясняет что это чат личный
AI_PROMPT_GM = os.getenv("AI_PROMPT_GM") #Объясняет что это чат групповой
AI_PROMPT_IS_RELEVANT_QUESTION  = os.getenv("AI_PROMPT_IS_RELEVANT_QUESTION") #  Технический вопрос для решения ложноположительных "да" в неуверенных или расплывчатых ответах от модели.


# Инициализация Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL)

# Включаем логирование
logging.basicConfig(level=logging.INFO)

# Хранилище сообщений для кнопки "Подробнее"
detailed_questions = {}
# История чатов по user_id или chat_id
user_chats = {}
# Сюда будем сохранять время окончания молчания по chat_id
mute_until = {}

# Ограничения Telegram
MAX_MESSAGE_LENGTH = 4096

# Ответ от Gemini
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message #Получает само сообщение, будь то из чата или группы.
    user_id = message.from_user.id
    chat_id = message.chat_id
    text = message.text
    # Ключ истории: отдельно для каждого пользователя или группы.
    history_key = user_id if message.chat.type == 'private' else chat_id

    silent = chat_id in mute_until and datetime.utcnow() < mute_until[chat_id]


    is_reply_to_bot = ( #Если боту нажали ответить
            message.reply_to_message and
            message.reply_to_message.from_user and
            message.reply_to_message.from_user.id == context.bot.id
    )

    try:
        # Определяем, что спросить у нейросети
        if message.chat.type != 'private': #True — если это ЛС. False — если группа или супергруппа.
            # прямое обращение или replay на бота — отвечаем полно
            if (f"@{context.bot.username.lower()}" in text.lower()) or (is_reply_to_bot):
                gemini_prompt = f"{user_id} пишет: {text}"
                is_short = False
            # Если вопрос и не обращение напрямую — ответ кратко
            elif (("?" in text) and (not silent)
                  and(not text.lower().startswith('@')) and (await is_bot_relevant(history_key,text,message.chat.type))):
                gemini_prompt = f"{user_id} пишет: {text}\n\nОтветь кратко, 1-2 предложениями."
                # Сохраняем вопрос по message_id
                detailed_questions[message.message_id] = text
                is_short = True
            else:
                return  # в остальных случаях в группе игнорируем
        else:
            gemini_prompt = text  # в личке всегда отвечаем полно
            is_short = False

        # Запрос к ИИ
        reply = await ask_gemini(history_key, gemini_prompt, message.chat.type)

        if is_short:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔇 Тише", callback_data=f"silence:{chat_id}:{message.message_id}"),
                InlineKeyboardButton("📖 Подробнее", callback_data=f"more:{message.message_id}:{user_id}")]
            ])
            await update.message.reply_text(reply,parse_mode='HTML', reply_markup=keyboard)
        else:
            for part in split_message(reply):
                await update.message.reply_text(part,parse_mode='HTML')
    except Exception as e:
        logging.error(f"Ошибка: {e}")


# Универсальная функция запроса к Gemini
async def ask_gemini(user_or_chat_id, prompt: str, chat_type):
    # Если чата ещё нет — создаём
    if user_or_chat_id not in user_chats:
        chat = model.start_chat(history=[])
        user_chats[user_or_chat_id] = chat
        chat.send_message(get_system_prompt(chat_type)) #В зависимости от типа чата говорим чату как надо себя вести
    chat = user_chats[user_or_chat_id]

    try:
        response = chat.send_message(prompt)
        return response.text.strip()
    except Exception as e:
        error_text = str(e)

        if "429" in error_text:
            return (
                f"⚠️ <b>Слишком много запросов для {GEMINI_MODEL}</b>\n"
                "Подожди немного и попробуй снова через минуту.\n\n"
                "<a href='https://ai.google.dev/gemini-api/docs/rate-limits'>Документация по лимитам</a>"
            )
        else:
            logging.error(f"Ошибка запроса к Gemini: {e}")
            return f"⚠️ Ошибка Gemini: {e}"

def get_system_prompt(chat_type):
    if chat_type == 'private':
        return AI_PROMPT_PM
    else:
        return (
            AI_PROMPT_GM
        )

# Проверка нейронки на возможность дать корректный ответ, чтобы избежать ложноположительных "да" в неуверенных или расплывчатых ответах от модели.
async def is_bot_relevant(user_or_chat_id,text: str,chat_type) -> bool:
    prompt = (
        f"Отвечать не нужно. Не сохраняй в памяти это. Просто проанализируй вопрос: {text}\n"
        f"{AI_PROMPT_IS_RELEVANT_QUESTION}"
        "Ответ должен быть только 'Да' или 'Нет'."
    )
    reply = await ask_gemini(user_or_chat_id, prompt, chat_type)
    return "да" in reply.lower()

#Функция обрезки сообщения на части
def split_message(message):
    parts = []
    while len(message) > MAX_MESSAGE_LENGTH:
        split_index = message[:MAX_MESSAGE_LENGTH].rfind("\n")
        if split_index == -1:
            split_index = MAX_MESSAGE_LENGTH
        parts.append(message[:split_index])
        message = message[split_index:]
    parts.append(message)
    return parts

# Приветствие новых участников через нейросеть
async def welcome_new_member(update: Update, context: CallbackContext):
    for member in update.message.new_chat_members:
        # Не приветствовать самого бота
        if member.id == context.bot.id:
            continue

        chat_id = update.effective_chat.id
        username = member.full_name  # Можно использовать member.mention_html() для HTML-отметки
        prompt = (
            f"В нашу группу  присоединился новый участник по имени {username}. "
            "Приветствуй его дружелюбно и с юмором от имени группы, используй контекст наших последних сообщений"
        )
        reply = await ask_gemini(chat_id, prompt, chat_type="group")

        await update.message.reply_text(
            reply,
            parse_mode='HTML'
        )

#Кнопка подробнее
async def expand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        data = query.data  # format: more:<msg_id>:<user_id>
        _, msg_id_str, original_user_id_str = data.split(':')
        msg_id = int(msg_id_str)
        original_user_id = int(original_user_id_str)

        requester_id = query.from_user.id
        question = detailed_questions.get(msg_id)

        if not question:
            await query.message.reply_text("❌ Оригинальный вопрос не найден.")
            return

        # Можно сделать ограничение: только автор вопроса может нажимать
        # if requester_id != original_user_id:
        #     await query.message.reply_text("⛔ Только автор вопроса может запросить подробности.")
        #     return

        # Отправляем тот же вопрос с уточнением
        detailed_reply = await ask_gemini(original_user_id, f"Объясни подробнее: {question}", chat_type="private")

        for part in split_message(detailed_reply):
            await query.message.reply_text(part, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Ошибка при обработке кнопки: {e}")
        await query.message.reply_text("⚠️ Ошибка при обработке кнопки: " + str(e))

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 <b>Я — бот с ИИ на базе Gemini-2.0-flash</b>\n\n"
        "Вот что я умею:\n"
        "• 📩 Отвечаю кратко на сообщения, содержащие <b>вопросительный знак '?'</b> (в группе) если знаю ответ\n"
        "• 🔎 Поддержка кнопки <b>«Подробнее»</b> к краткому ответу\n"
        "• 👤 Отвечаю, если вы обратились <b>ко мне напрямую</b>\n"
        "• 🔁 Команда <b>/reset</b> — сбрасывает историю диалога\n"
        "• ℹ️ Команда <b>/help</b> — показать это сообщение\n"
        "• 👋 Приветствую новых участников\n\n"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')


# 🔁 Команда /reset — сброс истории
async def reset_history(update: Update, context: CallbackContext):
    key = update.effective_user.id if update.effective_chat.type == 'private' else update.effective_chat.id
    if key in user_chats:
        del user_chats[key]
        await update.message.reply_text("🧼 Контекст сброшен!")
    else:
        await update.message.reply_text("ℹ️ Контекст уже пуст.")

# Обработка команды /start
async def start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    prompt = ("Поздоровайся с пользователем телеграмм бота, представься и кратко расскажи, что ты умеешь."
              f"Это тебе в помощь: подключена модель {GEMINI_MODEL} и есть кнопка /help")

    reply = await ask_gemini(user_id, prompt, chat_type)
    await update.message.reply_text(reply)

# Замолчать и не отвечать на вопросы 10 мин.
async def silence_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        data = query.data  # format: silence:<chat_id>:<message_id>
        _, chat_id_str, msg_id_str = data.split(':')
        chat_id = int(chat_id_str)
        msg_id = int(msg_id_str)

        # Устанавливаем молчание на 10 минут
        mute_until[chat_id] = datetime.utcnow() + timedelta(minutes=10)

        # Меняем кнопку на ⏳
        new_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Молчу 10 мин…", callback_data="noop")]])
        await query.edit_message_reply_markup(reply_markup=new_keyboard)

        # Возврат кнопок через 10 минут
       #await asyncio.sleep(600)

        # Возвращаем старые кнопки
        # original_question = detailed_questions.get(msg_id)
        # if original_question:
        #     keyboard = InlineKeyboardMarkup([[
        #         InlineKeyboardButton("🔇 Тише", callback_data=f"silence:{chat_id}:{msg_id}"),
        #         InlineKeyboardButton("📖 Подробнее", callback_data=f"more:{msg_id}:{query.from_user.id}")
        #     ]])
        #     try:
        #         await query.message.edit_reply_markup(reply_markup=keyboard)
        #         mute_until.pop(chat_id, None)  # Удаляем "молчание"
        #     except Exception as e:
        #         logging.warning(f"Не удалось вернуть кнопки: {e}")

    except Exception as e:
        logging.error(f"Ошибка в кнопке Тише: {e}")


#Установка команд в бота
async def set_commands(application):
    commands = [
        BotCommand("reset", "Сбросить историю диалога"),
        BotCommand("help", "Показать список команд"),
    ]
    await application.bot.set_my_commands(commands)

# Запуск бота
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("reset", reset_history))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CallbackQueryHandler(silence_callback, pattern="^silence:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(expand_callback, pattern="^more:"))
    app.post_init = set_commands # Устанавливаем команды
    print("Бот на Gemini запущен ✅")
    app.run_polling()

if __name__ == "__main__":
    main()
