import aiosqlite
import json
import os
import logging
import config

logger = logging.getLogger(__name__)


# ==========================================
# РАБОТА С БАЗОЙ ДАННЫХ (SQLite)
# ==========================================

async def init_db():
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Таблица сообщений
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

        # Таблица настроек (теперь всё создается сразу, без ALTER TABLE костылей)
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
                             1,
                             respond_all
                             INTEGER
                             DEFAULT
                             0,
                             silent_responses
                             INTEGER
                             DEFAULT
                             0,
                             show_tech_details
                             INTEGER
                             DEFAULT
                             0,
                             selected_model
                             TEXT
                         )
                         """)
        await db.commit()


# ==========================================
# БАЗОВЫЕ ФУНКЦИИ ДЛЯ ЛЮБЫХ НАСТРОЕК (DRY)
# ==========================================

async def _get_setting(chat_key: str, column_name: str, default_value: int):
    """Универсальная функция для получения любой настройки из базы"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        # В SQLite нельзя передавать имена колонок через '?', поэтому используем f-строку.
        # Это безопасно, так как column_name задаем мы сами в коде, а не юзер.
        query = f"SELECT {column_name} FROM settings WHERE chat_key = ?"
        async with db.execute(query, (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else bool(default_value)


async def _toggle_setting(chat_key: str, column_name: str):
    """Универсальная функция для переключения любой настройки (0 -> 1 -> 0)"""
    current_value = await _get_setting(chat_key, column_name, default_value=0)
    new_val = 0 if current_value else 1

    async with aiosqlite.connect(config.DB_PATH) as db:
        query = f"""
            INSERT INTO settings (chat_key, {column_name})
            VALUES (?, ?) ON CONFLICT(chat_key) DO
            UPDATE SET {column_name} = excluded.{column_name}
        """
        await db.execute(query, (str(chat_key), new_val))
        await db.commit()
    return bool(new_val)


# ==========================================
# ОБЕРТКИ НАСТРОЕК (Короткие и понятные)
# ==========================================

async def get_auto_fallback(chat_key):      return await _get_setting(chat_key, "auto_fallback", 1)


async def toggle_auto_fallback(chat_key):   return await _toggle_setting(chat_key, "auto_fallback")


async def get_keep_context(chat_key):       return await _get_setting(chat_key, "keep_context", 1)


async def toggle_keep_context(chat_key):    return await _toggle_setting(chat_key, "keep_context")


async def get_respond_all(chat_key):        return await _get_setting(chat_key, "respond_all", 0)


async def toggle_respond_all(chat_key):     return await _toggle_setting(chat_key, "respond_all")


async def get_silent_responses(chat_key):   return await _get_setting(chat_key, "silent_responses", 0)


async def toggle_silent_responses(chat_key): return await _toggle_setting(chat_key, "silent_responses")


async def get_show_tech_details(chat_key):  return await _get_setting(chat_key, "show_tech_details", 0)


async def toggle_show_tech_details(chat_key): return await _toggle_setting(chat_key, "show_tech_details")


# ==========================================
# ИСТОРИЯ ДИАЛОГОВ И МОДЕЛИ
# ==========================================

async def add_message_to_db(chat_key, role: str, content: str):
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute("INSERT INTO messages (chat_key, role, content) VALUES (?, ?, ?)",
                             (str(chat_key), role, content))
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка записи в БД: {e}")


async def get_history_from_db(chat_key, limit: int = 10):
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT role, content FROM messages WHERE chat_key = ? ORDER BY id DESC LIMIT ?",
                                      (str(chat_key), limit))
            rows = await cursor.fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    except Exception as e:
        return []


async def clear_history_in_db(chat_key):
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute("DELETE FROM messages WHERE chat_key = ?", (str(chat_key),))
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка очистки БД: {e}")


def load_models_config():
    if not os.path.exists(config.MODELS_CONFIG_PATH):
        raise FileNotFoundError(f"Файл {config.MODELS_CONFIG_PATH} не найден!")
    with open(config.MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
        models = json.load(f)
        if not models:
            raise ValueError(f"Файл {config.MODELS_CONFIG_PATH} пуст!")
        return models


async def get_user_model(chat_key):
    """Достает выбранную модель из БД. Если её нет - берет первую из конфига."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT selected_model FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return row[0]

    # Если в базе пусто, отдаем модель по умолчанию
    return load_models_config()[0]["id"]


async def set_user_model(chat_key, model_id):
    """Сохраняет выбранную модель в БД"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, selected_model)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET selected_model = excluded.selected_model
                         """, (str(chat_key), model_id))
        await db.commit()


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