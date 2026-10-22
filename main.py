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
        BotCommand("events", "Дни рождения и особые даты")
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
    app.job_queue.run_daily(tasks.check_youtube_cookies_job, time=time(hour=9, minute=0, tzinfo=config.BOT_TIMEZONE))
    app.job_queue.run_daily(tasks.sync_api_models_job, time=time(hour=7, minute=0, tzinfo=config.BOT_TIMEZONE))
    # Ежедневная проверка ДР и особых дат в 09:00 по местному времени
    app.job_queue.run_daily(tasks.daily_custom_holidays_job, time=time(hour=9, minute=0, tzinfo=config.BOT_TIMEZONE))
    # Еженедельный дайджест по понедельникам (в PTB: 0=Вс, 1=Пн, ..., 5=Пт) в 09:05
    app.job_queue.run_daily(tasks.monday_weekly_digest_job, time=time(hour=9, minute=5, tzinfo=config.BOT_TIMEZONE),days=(1,))
    # Пятничный анонс событий следующей недели в 16:00 по пятницам (5=Пт)
    app.job_queue.run_daily(tasks.friday_upcoming_events_job, time=time(hour=16, minute=0, tzinfo=config.BOT_TIMEZONE),days=(5,))

    app.add_handler(CommandHandler("start", handlers.start_command))
    app.add_handler(CommandHandler("reset", handlers.reset_history))
    app.add_handler(CommandHandler("help", handlers.show_help))
    app.add_handler(CommandHandler("model", handlers.model_command))
    app.add_handler(CommandHandler("setting", handlers.settings_command))
    app.add_handler(CommandHandler("zombie_allowed", handlers.zombie_allowed_command))
    app.add_handler(CommandHandler("pay", handlers.pay_command))
    app.add_handler(CommandHandler("events", handlers.events_command))
    app.add_handler(CommandHandler("mute_bday", handlers.mute_bday_command))

    app.add_handler(CallbackQueryHandler(handlers.pay_callback, pattern="^paid:"))
    app.add_handler(CallbackQueryHandler(handlers.admin_confirm_callback, pattern="^admin_(confirm|reject):"))
    app.add_handler(CallbackQueryHandler(handlers.set_model_callback, pattern="^set_model:"))
    app.add_handler(CallbackQueryHandler(handlers.settings_callback, pattern="^setting:"))
    app.add_handler(CallbackQueryHandler(handlers.silence_callback, pattern="^(silence|unsilence):"))
    app.add_handler(CallbackQueryHandler(handlers.expand_callback, pattern="^more:"))
    app.add_handler(CallbackQueryHandler(handlers.cancel_callback, pattern="^cancel:", block=False))
    app.add_handler(CallbackQueryHandler(handlers.delete_event_callback, pattern="^del_event:"))
    app.add_handler(CallbackQueryHandler(handlers.bday_mute_callback, pattern="^bday_mute:"))

    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION) & ~filters.COMMAND,
        handlers.handle_message, block=False))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handlers.welcome_new_member))

    app.post_init = on_startup
    logger.info("Бот с динамической загрузкой моделей и настройками запущен ✅")

    # 2. drop_pending_updates=True заставляет бота ИГНОРИРОВАТЬ
    # все сообщения, которые были написаны, пока он был выключен.
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()