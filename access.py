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
                trial_left INTEGER DEFAULT {TRIAL_REQUESTS},
                warning_sent INTEGER DEFAULT 0
            )
        """)
        # На случай, если таблица уже создана, безопасно добавим колонку
        try:
            await db.execute("ALTER TABLE subscriptions ADD COLUMN warning_sent INTEGER DEFAULT 0")
        except Exception:
            pass
        await db.commit()


async def get_subscription_info(chat_id: int):
    """Возвращает полную информацию о подписке чата"""
    async with aiosqlite.connect(BILLING_DB_PATH) as db:
        async with db.execute("SELECT expires_at, is_lifetime, trial_left FROM subscriptions WHERE chat_id = ?",
                              (str(chat_id),)) as cursor:
            return await cursor.fetchone()


async def grant_lifetime_access(chat_id: int, chat_type: str):
    """Выдает вечный доступ (команда /zombie_allowed)"""
    async with aiosqlite.connect(BILLING_DB_PATH) as db:
        await db.execute("""
                         INSERT INTO subscriptions (chat_id, chat_type, is_lifetime, trial_left)
                         VALUES (?, ?, 1, 0) ON CONFLICT(chat_id) DO
                         UPDATE SET is_lifetime = 1
                         """, (str(chat_id), chat_type))
        await db.commit()


async def add_subscription_days(chat_id: int, chat_type: str, days: int):
    """Продлевает подписку, суммируя дни, если она еще активна"""
    now = datetime.now(timezone.utc)

    info = await get_subscription_info(chat_id)

    if info and info[0]:  # Если уже есть дата окончания
        current_expires_at = datetime.fromisoformat(info[0])
        # Если подписка еще жива, плюсуем к ней. Если уже истекла, плюсуем к текущему времени
        base_date = max(current_expires_at, now)
    else:
        base_date = now

    new_expires_at = base_date + timedelta(days=days)

    async with aiosqlite.connect(BILLING_DB_PATH) as db:
        await db.execute("""
                         INSERT INTO subscriptions (chat_id, chat_type, expires_at, trial_left)
                         VALUES (?, ?, ?, 0) ON CONFLICT(chat_id) DO
                         UPDATE SET expires_at = excluded.expires_at, trial_left = 0
                         """, (str(chat_id), chat_type, new_expires_at.isoformat()))
        await db.commit()


async def consume_request_and_check(chat_id: int, chat_type: str) -> bool:
    """
    Списывает триальный запрос или проверяет подписку.
    Возвращает True, если бот может ответить, и False, если нужно платить.
    """
    info = await get_subscription_info(chat_id)

    if not info:
        async with aiosqlite.connect(BILLING_DB_PATH) as db:
            await db.execute(
                "INSERT INTO subscriptions (chat_id, chat_type, trial_left) VALUES (?, ?, ?)",
                (str(chat_id), chat_type, TRIAL_REQUESTS - 1)
            )
            await db.commit()
        return True  # Разрешаем первый запрос

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


"""Проверяет, истекает ли подписка в течение 3 дней и не отправлялось ли предупреждение"""
async def check_subscription_expiring(chat_id: int) -> bool:

    async with aiosqlite.connect(BILLING_DB_PATH) as db:
        async with db.execute("SELECT expires_at, warning_sent, is_lifetime FROM subscriptions WHERE chat_id = ?",
                              (str(chat_id),)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False

            expires_at_str, warning_sent, is_lifetime = row
            if is_lifetime == 1 or not expires_at_str:
                return False

            expires_at = datetime.fromisoformat(expires_at_str)
            now = datetime.now(timezone.utc)

            # Если до конца осталось меньше 3 дней и предупреждение еще не шло
            if timedelta(0) < (expires_at - now) <= timedelta(days=3) and warning_sent == 0:
                # Ставим флаг, что предупредили
                await db.execute("UPDATE subscriptions SET warning_sent = 1 WHERE chat_id = ?", (str(chat_id),))
                await db.commit()
                return True

    return False