import logging
import os

import google.generativeai as genai
from dotenv import load_dotenv
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler, CallbackQueryHandler

load_dotenv()  # Загружаем переменные из .env
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
AI_PROMPT = os.getenv("AI_PROMPT")

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

# Включаем логирование
logging.basicConfig(level=logging.INFO)

# хранилище оригинальных вопросов
detailed_questions = {}

# Хранилище индивидуальных чатов: user_id -> chat
user_chats = {}

# Ответ от Gemini
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #user_id = update.message.from_user.id
    is_short = False
    message = update.effective_message #Получает само сообщение, будь то из чата или группы.
    user_id = message.from_user.id
    chat_id = message.chat_id
    text = message.text
    is_private = message.chat.type == 'private' #Определяем группа или личка. True — если это ЛС. False — если группа или супергруппа.
    is_reply_to_bot = ( #Если боту нажали ответить
            message.reply_to_message and
            message.reply_to_message.from_user and
            message.reply_to_message.from_user.id == context.bot.id
    )

    # Ключ истории: отдельно для каждого пользователя или группы
    history_key = user_id if is_private else chat_id

    # Создаём чат, если ещё не было
    if history_key not in user_chats:
        new_chat = model.start_chat(history=[])
        new_chat.send_message(AI_PROMPT)
        user_chats[history_key] = new_chat

    chat = user_chats[history_key]

    try:
        # Определяем, что спросить у нейросети
        if not is_private:
            # прямое обращение или replay на бота — отвечаем полно
            if (f"@{context.bot.username.lower()}" in text.lower()) or (is_reply_to_bot):
                gemini_prompt = text
            # Если вопрос и не обращение напрямую — ответ кратко
            elif "?" in text and not message.text.lower().startswith('@'):
                gemini_prompt = f"{text}\n\nОтветь кратко, 1-2 предложениями."
                # Сохраняем вопрос по message_id
                detailed_questions[message.message_id] = text
                is_short = True
            else:
                return  # в остальных случаях в группе игнорируем
        else:
            gemini_prompt = text  # в личке всегда отвечаем полно


        response = chat.send_message(gemini_prompt)
        reply = response.text
        if is_short:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 Подробнее", callback_data=f"more:{message.message_id}:{user_id}")]
            ])
            await update.message.reply_text(reply, reply_markup=keyboard)
        else:
            for part in split_message(reply):
                await update.message.reply_text(part)
    except Exception as e:
        logging.error(f"Ошибка Gemini: {e}")
        await update.message.reply_text("⚠️ Ошибка Gemini: " + str(e))

#Функция обрезки сообщения на части
def split_message(text, max_length=4000):
    parts = []
    while len(text) > max_length:
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        parts.append(text[:split_at].strip())
        text = text[split_at:].strip()
    parts.append(text)
    return parts

# Приветствие новых участников
async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        # Не приветствовать самого бота
        if member.id == context.bot.id:
            continue

        await update.message.reply_text(
            f"👋 Добро пожаловать, {member.mention_html()}!",
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
        chat = user_chats.get(original_user_id)
        if chat is None:
            chat = model.start_chat(history=[])
            chat.send_message(AI_PROMPT)
            user_chats[original_user_id] = chat

        # Добавляем уточнение к вопросу
        full_question = f"Объясни подробнее: {question}"
        response = chat.send_message(full_question)
        detailed_reply = response.text.strip()

        for part in split_message(detailed_reply):
            await query.message.reply_text(part)

    except Exception as e:
        logging.error(f"Ошибка при обработке кнопки: {e}")
        await query.message.reply_text("⚠️ Ошибка при обработке кнопки: " + str(e))

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 <b>Я — бот с ИИ на базе Gemini</b>\n\n"
        "Вот что я умею:\n"
        "• 📩 Отвечаю кратко на сообщения, содержащие <b>вопросительный знак '?'</b> (в группе)\n"
        "• 🔎 Поддержка кнопки <b>«Подробнее»</b> к краткому ответу\n"
        "• 👤 Отвечаю, если вы обратились <b>ко мне напрямую</b>\n"
        "• 🔁 Команда <b>/reset</b> — сбрасывает историю диалога\n"
        "• ℹ️ Команда <b>/help</b> — показать это сообщение\n"
        "• 👋 Приветствую новых участников\n\n"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')


# 🔁 Команда /reset — сброс истории
async def reset_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    new_chat = model.start_chat(history=[])
    new_chat.send_message(AI_PROMPT)
    user_chats[user_id] = new_chat
    await update.message.reply_text("🧹 История диалога сброшена! Начнём с чистого листа.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Привет! Я работаю через gemini-2.0-flash. Напиши мне что-нибудь!')

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(expand_callback, pattern="^more:"))
    app.post_init = set_commands # Устанавливаем команды
    print("Бот на Gemini запущен ✅")
    app.run_polling()

if __name__ == "__main__":
    main()
