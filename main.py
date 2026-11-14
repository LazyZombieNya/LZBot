import logging
import sys
from datetime import time
from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters

import config
import database
import tasks
import handlers
import re
import web_parser
from access import init_billing_db

logging.basicConfig(level=logging.INFO)


class SecretMaskFilter(logging.Filter):
    """Автоматически скрывает токен Telegram-бота во всех логах (включая httpx и traceback)."""
    def __init__(self, token: str | None):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self.token:
            # Формируем готовое сообщение с подставленными аргументами и заменяем токен
            msg = record.getMessage()
            if self.token in msg:
                record.msg = msg.replace(self.token, "***TOKEN***")
                record.args = None
        return True


# Вешаем фильтр на все обработчики корневого логгера
_mask_filter = SecretMaskFilter(config.TELEGRAM_TOKEN)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_mask_filter)

logger = logging.getLogger(__name__)

class GenerationStoppedFilter(filters.MessageFilter):
    def filter(self, message) -> bool:
        return bool(getattr(message, "api_kwargs", {}).get("message_generation_stopped"))

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
    # 1. Настраиваем таймауты как для обычных запросов, так и отдельно для getUpdates (поллинга).
    # Критично при работе через HTTP/HTTPS прокси: если прокси-туннель "моргнет" и оставит сокет
    # полуоткрытым (half-open), get_updates_read_timeout принудительно разорвет зависшее соединение
    # и переподключится заново, не давая боту "уснуть".
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_TOKEN)
        .connect_timeout(20.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(20.0)
        .get_updates_connect_timeout(15.0)
        .get_updates_read_timeout(15.0)
        .get_updates_write_timeout(15.0)
        .get_updates_pool_timeout(15.0)
        .get_updates_connection_pool_size(4)
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
    # Сторожевой таймер связи с Telegram через прокси (проверка каждые 2 минуты)
    app.job_queue.run_repeating(tasks.telegram_watchdog_job, interval=120, first=60)

    app.add_handler(CommandHandler("start", handlers.start_command))
    app.add_handler(CommandHandler("reset", handlers.reset_history))
    app.add_handler(CommandHandler("help", handlers.show_help))
    app.add_handler(CommandHandler("model", handlers.model_command))
    app.add_handler(CommandHandler("setting", handlers.settings_command))
    app.add_handler(CommandHandler("zombie_allowed", handlers.zombie_allowed_command))
    app.add_handler(CommandHandler("pay", handlers.pay_command))
    app.add_handler(CommandHandler("events", handlers.events_command))
    app.add_handler(CommandHandler("mute_bday", handlers.mute_bday_command))
    app.add_handler(CommandHandler("sync_models", handlers.sync_models_command))

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
    app.add_handler(MessageHandler(GenerationStoppedFilter(), handlers.handle_generation_stopped, block=False))

    app.post_init = on_startup
    logger.info("Бот с динамической загрузкой моделей и настройками запущен ✅")

    # 2. drop_pending_updates=True заставляет бота ИГНОРИРОВАТЬ
    # все сообщения, которые были написаны, пока он был выключен.
    # timeout=10 и bootstrap_retries=-1 гарантируют мгновенный перезапуск опроса при сбое прокси.
    app.run_polling(
        drop_pending_updates=True,
        timeout=10,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()