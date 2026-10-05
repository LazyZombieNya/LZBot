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
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS messages
                         (
                             id INTEGER PRIMARY KEY AUTOINCREMENT,
                             chat_key TEXT,
                             role TEXT,
                             content TEXT,
                             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                         )
                         """)
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS settings
                         (
                             chat_key TEXT PRIMARY KEY,
                             auto_fallback INTEGER DEFAULT 1,
                             keep_context INTEGER DEFAULT 1
                         )
                         """)
        # Безопасно обновляем существующую таблицу, если колонки еще нет
        try:
            await db.execute("ALTER TABLE settings ADD COLUMN keep_context INTEGER DEFAULT 1")
        except Exception:
            pass  # Колонка уже существует, всё ок

        try: # Настройка отвечать на все
            await db.execute("ALTER TABLE settings ADD COLUMN respond_all INTEGER DEFAULT 0")
        except Exception:
            pass
        try: # Выбранная модель
            await db.execute("ALTER TABLE settings ADD COLUMN selected_model TEXT")
        except Exception:
            pass
        try: # тихий ответ
            await db.execute("ALTER TABLE settings ADD COLUMN silent_responses INTEGER DEFAULT 0")
        except Exception:
            pass
        try: # показ технических деталей (переключение моделей)
            await db.execute("ALTER TABLE settings ADD COLUMN show_tech_details INTEGER DEFAULT 0")
        except Exception:
            pass

        await db.commit()

async def get_show_tech_details(chat_key):
    """Возвращает статус показа технических деталей (по умолчанию False/скрыто)"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT show_tech_details FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

async def toggle_show_tech_details(chat_key):
    """Переключает статус показа технических деталей"""
    current = await get_show_tech_details(chat_key)
    new_val = 0 if current else 1
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, show_tech_details)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET show_tech_details = excluded.show_tech_details
                         """, (str(chat_key), new_val))
        await db.commit()
    return bool(new_val)

async def get_auto_fallback(chat_key):
    """Возвращает статус авто-переключения (по умолчанию True)"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT auto_fallback FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else True

async def toggle_auto_fallback(chat_key):
    """Переключает статус авто-переключения и возвращает новое значение"""
    current = await get_auto_fallback(chat_key)
    new_val = 0 if current else 1
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, auto_fallback)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET auto_fallback = excluded.auto_fallback
                         """, (str(chat_key), new_val))
        await db.commit()
    return bool(new_val)

async def add_message_to_db(chat_key, role: str, content: str):
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "INSERT INTO messages (chat_key, role, content) VALUES (?, ?, ?)",
                (str(chat_key), role, content)
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка записи в БД: {e}")

async def get_history_from_db(chat_key, limit: int = 10):
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT role, content FROM messages WHERE chat_key = ? ORDER BY id DESC LIMIT ?",
                (str(chat_key), limit)
            )
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

async def get_respond_all(chat_key):
    """Возвращает статус режима 'Отвечать на всё' (по умолчанию False)"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT respond_all FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

async def toggle_respond_all(chat_key):
    """Переключает статус 'Отвечать на всё' и возвращает новое значение"""
    current = await get_respond_all(chat_key)
    new_val = 0 if current else 1
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, respond_all)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET respond_all = excluded.respond_all
                         """, (str(chat_key), new_val))
        await db.commit()
    return bool(new_val)

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

async def get_silent_responses(chat_key):
    """Возвращает статус тихих ответов (по умолчанию False/со звуком)"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT silent_responses FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

async def toggle_silent_responses(chat_key):
    """Переключает статус тихих ответов"""
    current = await get_silent_responses(chat_key)
    new_val = 0 if current else 1
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, silent_responses)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET silent_responses = excluded.silent_responses
                         """, (str(chat_key), new_val))
        await db.commit()
    return bool(new_val)

async def get_keep_context(chat_key):
    """Возвращает статус передачи контекста (по умолчанию True)"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT keep_context FROM settings WHERE chat_key = ?", (str(chat_key),)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else True

async def toggle_keep_context(chat_key):
    """Переключает статус передачи контекста и возвращает новое значение"""
    current = await get_keep_context(chat_key)
    new_val = 0 if current else 1
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
                         INSERT INTO settings (chat_key, keep_context)
                         VALUES (?, ?) ON CONFLICT(chat_key) DO
                         UPDATE SET keep_context = excluded.keep_context
                         """, (str(chat_key), new_val))
        await db.commit()
    return bool(new_val)

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