import logging
import sys
from datetime import time
from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters

import config
import database
import tasks
import handlers
import web_parser
from access import init_billing_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def on_startup(application):
    try:
        database.load_models_config()
    except Exception as e:
        logger.critical(e)
        sys.exit(1)

    web_parser.configure_admin_alerts(bot_token=config.TELEGRAM_TOKEN, admin_chat_id=config.ADMIN_ID)
    await database.init_db()  # База истории сообщений
    await init_billing_db()  # База подписок покупки

    # СРАЗУ ОБНОВЛЯЕМ СПИСКИ ПРИ СТАРТЕ!
    await tasks.sync_gemini_models()  # Обновление баз моделей Gemini
    await tasks.sync_groq_models()  # Обновление баз моделей Groq
    await tasks.sync_openrouter_models()  # Обновление баз моделей Openrouter

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
        .token(config.TELEGRAM_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )

    # === ЗАПУСК ФОНОВОЙ ПРОВЕРКИ ===
    # Запускаем проверку каждые 3600 секунд (1 час).
    # first=10 означает, что первая проверка пройдет через 10 секунд после старта бота
    app.job_queue.run_repeating(tasks.check_expirations_job, interval=3600, first=10)
    app.job_queue.run_daily(tasks.check_youtube_cookies_job, time=time(hour=9, minute=0))
    app.job_queue.run_daily(tasks.sync_api_models_job, time=time(hour=7, minute=00))

    app.add_handler(CommandHandler("start", handlers.start_command))
    app.add_handler(CommandHandler("reset", handlers.reset_history))
    app.add_handler(CommandHandler("help", handlers.show_help))
    app.add_handler(CommandHandler("model", handlers.model_command))
    app.add_handler(CommandHandler("setting", handlers.settings_command))
    app.add_handler(CommandHandler("zombie_allowed", handlers.zombie_allowed_command))
    app.add_handler(CommandHandler("pay", handlers.pay_command))

    app.add_handler(CallbackQueryHandler(handlers.pay_callback, pattern="^paid:"))
    app.add_handler(CallbackQueryHandler(handlers.admin_confirm_callback, pattern="^admin_(confirm|reject):"))
    app.add_handler(CallbackQueryHandler(handlers.set_model_callback, pattern="^set_model:"))
    app.add_handler(CallbackQueryHandler(handlers.settings_callback, pattern="^setting:"))
    app.add_handler(CallbackQueryHandler(handlers.silence_callback, pattern="^(silence|unsilence):"))
    app.add_handler(CallbackQueryHandler(handlers.expand_callback, pattern="^more:"))
    app.add_handler(CallbackQueryHandler(handlers.cancel_callback, pattern="^cancel:", block=False))

    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO) & ~filters.COMMAND,
        handlers.handle_message, block=False))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handlers.welcome_new_member))

    app.post_init = on_startup
    logger.info("Бот с динамической загрузкой моделей и настройками запущен ✅")

    # 2. drop_pending_updates=True заставляет бота ИГНОРИРОВАТЬ
    # все сообщения, которые были написаны, пока он был выключен.
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()