import aiosqlite
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger(__name__)

BILLING_DB_PATH = "billing.db"
TRIAL_REQUESTS = 5


async def init_billing_db():
    async with aiosqlite.connect(BILLING_DB_PATH) as db:
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id TEXT PRIMARY KEY,
                chat_type TEXT,
                expires_at TIMESTAMP,
                is_lifetime INTEGER DEFAULT 0,
                trial_left INTEGER DEFAULT {TRIAL_REQUESTS}
            )
        """)

        # Безопасно добавляем колонку для тех, кто обновился со старой версии БД
        try:
            await db.execute("ALTER TABLE subscriptions ADD COLUMN warning_sent INTEGER DEFAULT 0")
        except Exception:
            pass

        await db.commit()


async def get_subscription_info(chat_id: int):
    async with aiosqlite.connect(BILLING_DB_PATH) as db:
        async with db.execute("SELECT expires_at, is_lifetime, trial_left FROM subscriptions WHERE chat_id = ?",
                              (str(chat_id),)) as cursor:
            return await cursor.fetchone()


async def grant_lifetime_access(chat_id: int, chat_type: str):
    async with aiosqlite.connect(BILLING_DB_PATH) as db:
        await db.execute("""
                         INSERT INTO subscriptions (chat_id, chat_type, is_lifetime, trial_left, warning_sent)
                         VALUES (?, ?, 1, 0, 0) ON CONFLICT(chat_id) DO
                         UPDATE SET is_lifetime = 1
                         """, (str(chat_id), chat_type))
        await db.commit()


async def add_subscription_days(chat_id: int, chat_type: str, days: int):
    now = datetime.now(timezone.utc)
    info = await get_subscription_info(chat_id)

    if info and info[0]:
        current_expires_at = datetime.fromisoformat(info[0])
        base_date = max(current_expires_at, now)
    else:
        base_date = now

    new_expires_at = base_date + timedelta(days=days)

    async with aiosqlite.connect(BILLING_DB_PATH) as db:
        # При начислении дней обязательно сбрасываем warning_sent в 0
        await db.execute("""
                         INSERT INTO subscriptions (chat_id, chat_type, expires_at, trial_left, warning_sent)
                         VALUES (?, ?, ?, 0, 0) ON CONFLICT(chat_id) DO
                         UPDATE SET expires_at = excluded.expires_at, trial_left = 0, warning_sent = 0
                         """, (str(chat_id), chat_type, new_expires_at.isoformat()))
        await db.commit()


async def consume_request_and_check(chat_id: int, chat_type: str) -> bool:
    info = await get_subscription_info(chat_id)

    if not info:
        async with aiosqlite.connect(BILLING_DB_PATH) as db:
            await db.execute(
                "INSERT INTO subscriptions (chat_id, chat_type, trial_left, warning_sent) VALUES (?, ?, ?, 0)",
                (str(chat_id), chat_type, TRIAL_REQUESTS - 1)
            )
            await db.commit()
        return True

    expires_at_str, is_lifetime, trial_left = info

    if is_lifetime == 1:
        return True

    if expires_at_str:
        expires_at = datetime.fromisoformat(expires_at_str)
        if datetime.now(timezone.utc) < expires_at:
            return True

    if trial_left > 0:
        async with aiosqlite.connect(BILLING_DB_PATH) as db:
            await db.execute("UPDATE subscriptions SET trial_left = trial_left - 1 WHERE chat_id = ?", (str(chat_id),))
            await db.commit()
        return True

    return False


async def get_and_mark_expiring_subscriptions(hours_left: int = 24):
    """Находит подписки, истекающие менее чем через hours_left, помечает их и возвращает список ID"""
    now = datetime.now(timezone.utc)
    target_time = now + timedelta(hours=hours_left)
    expiring_chats = []

    async with aiosqlite.connect(BILLING_DB_PATH) as db:
        async with db.execute("""
                              SELECT chat_id, expires_at
                              FROM subscriptions
                              WHERE is_lifetime = 0
                                AND warning_sent = 0
                                AND expires_at IS NOT NULL
                              """) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            chat_id, expires_at_str = row
            expires_at = datetime.fromisoformat(expires_at_str)

            # Если время окончания наступит менее чем через 24 часа, но еще не наступило в прошлом
            if now < expires_at <= target_time:
                expiring_chats.append(chat_id)

        # Помечаем найденные чаты, чтобы не отправить им уведомление дважды
        for chat_id in expiring_chats:
            await db.execute("UPDATE subscriptions SET warning_sent = 1 WHERE chat_id = ?", (chat_id,))
        await db.commit()

    return expiring_chats