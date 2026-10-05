import aiohttp
import json
import os
import re
import logging
from telegram.ext import ContextTypes

import config
import web_parser
from ai_core import groq_client
from access import get_and_mark_expiring_subscriptions

logger = logging.getLogger(__name__)

# Черный список для Gemini
GEMINI_IGNORE_KEYWORDS = (
    "vision", "image", "embed", "aqa", "tts", "live", "transcribe",
    "bison", "gecko", "learnlm", "medlm", "veo", "lyria", "robotics"
)

def make_gemini_display_name(model_id: str, display_name: str) -> str:
    """Генерирует аккуратное имя для кнопок меню Telegram"""
    # Очищаем от мусора: "Gemini 3.5 Flash-Lite" -> "✨ GEM: 3.5 Flash-Lite"
    name = display_name.replace("Gemini", "").replace("preview", "prev").replace("Experimental", "exp").strip()
    return f"✨ GEM: {name[:14]}"

def get_gemini_sort_score(model_id: str) -> float:
    """Умная функция для оценки крутости модели (чем выше балл, тем выше в списке)"""
    score = 0.0

    # 1. Извлекаем номер версии (3.8, 3.5, 3, 2.5)
    match = re.search(r'gemini-(\d+(?:\.\d+)?)', model_id)
    if match:
        score = float(match.group(1)) * 100  # Например, 3.8 -> 380 баллов

    # 2. Штрафуем специфичные версии, чтобы выстроить идеальный порядок
    if "lite" in model_id:
        score -= 2  # Lite чуть ниже флагмана той же версии
    if "pro" in model_id:
        score -= 5  # Pro тяжелее и лимиты меньше, ставим после Flash
    if "preview" in model_id or "exp" in model_id:
        score -= 10  # Экспериментальные ставим ниже стабильных

    return score

async def sync_gemini_models():
    """Синхронизирует актуальные текстовые модели Google Gemini"""
    if not config.GEMINI_API_KEY:
        return

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=100&key={config.GEMINI_API_KEY}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"Ошибка API Gemini при получении моделей: {resp.status}")
                    return
                data = await resp.json()
                models_data = data.get("models", [])

        active_gemini_models = []
        for m in models_data:
            full_name = m.get("name", "")
            m_id = full_name.replace("models/", "")

            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue

            m_id_lower = m_id.lower()

            # ЖЕСТКИЙ БЕЛЫЙ СПИСОК: берем только модели gemini, в которых есть flash или pro
            if not m_id_lower.startswith("gemini-"):
                continue
            if "flash" not in m_id_lower and "pro" not in m_id_lower:
                continue

            # Исключаем служебные, аудио и робо-песочницы
            if any(keyword in m_id_lower for keyword in GEMINI_IGNORE_KEYWORDS):
                continue

            display_name = m.get("displayName", m_id)
            active_gemini_models.append({
                "id": m_id,
                "name": make_gemini_display_name(m_id, display_name),
                "provider": "gemini",
                "score": get_gemini_sort_score(m_id)  # Временное поле для сортировки
            })

        # Сортируем по нашему баллу (по убыванию)
        active_gemini_models.sort(key=lambda x: x["score"], reverse=True)

        # Убираем временное поле score и берем ТОП-8 лучших моделей
        for m in active_gemini_models:
            m.pop("score", None)

        active_gemini_models = active_gemini_models[:10]

        if not active_gemini_models:
            return

        if not os.path.exists(config.MODELS_CONFIG_PATH):
            return

        with open(config.MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
            current_models = json.load(f)

        # Вытаскиваем модели других провайдеров (Groq, OpenRouter), чтобы не затереть их
        other_models = [m for m in current_models if m.get("provider") != "gemini"]

        # Склеиваем: сначала новые правильные Gemini, потом всё остальное
        new_models_list = active_gemini_models + other_models

        # Сохраняем, только если есть реальные изменения
        if new_models_list != current_models:
            with open(config.MODELS_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(new_models_list, f, ensure_ascii=False, indent=2)
            logger.info("models.json успешно обновлен: список моделей Gemini актуализирован ✅")

    except Exception as e:
        logger.warning(f"Не удалось обновить список моделей Gemini: {e}")

def make_or_display_name(model_id: str, name: str) -> str:
    """Очищает имя модели OpenRouter от мусора, чтобы оно красиво влезло в кнопку"""
    clean = name.replace("(free)", "").replace("Instruct", "").strip()
    # Удаляем названия компаний в начале (например "Meta: Llama 3" -> "Llama 3")
    if ":" in clean:
        clean = clean.split(":", 1)[1].strip()
    # Обрезаем длинные названия
    return f"🔋 OR: {clean[:15]}"

async def sync_openrouter_models():
    """Синхронизирует ТОП-8 самых популярных бесплатных моделей OpenRouter"""
    if not config.OPENROUTER_API_KEY:
        return

    try:
        # Запрашиваем модели, отсортированные по популярности в мире
        async with aiohttp.ClientSession() as session:
            async with session.get("https://openrouter.ai/api/v1/models?sort=most-popular") as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                models_data = data.get("data", [])

        active_or_models = []
        for m in models_data:
            m_id = m.get("id", "")
            pricing = m.get("pricing", {})

            # Берем только 100% бесплатные модели
            is_free = pricing.get("prompt") == "0" and pricing.get("completion") == "0"
            if not is_free and not m_id.endswith(":free"):
                continue

            # Исключаем модели, которые мы уже используем напрямую,
            # а также не-текстовые генераторы (например, картинки)
            m_id_lower = m_id.lower()
            if m_id_lower.startswith("google/") or m_id_lower.startswith("groq/"):
                continue
            if "vision" in m_id_lower or "image" in m_id_lower or "embed" in m_id_lower:
                continue

            active_or_models.append({
                "id": m_id,
                "name": make_or_display_name(m_id, m.get("name", "")),
                "provider": "openrouter"
            })

            # Ограничиваемся ТОП-8 моделями, чтобы не разорвать экран телефона кнопками
            if len(active_or_models) >= 8:
                break

        if not active_or_models:
            return

        if not os.path.exists(config.MODELS_CONFIG_PATH):
            return

        with open(config.MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
            current_models = json.load(f)

        # Оставляем блоки Gemini и Groq как есть, а блок OpenRouter заменяем новым
        before_or = [m for m in current_models if m.get("provider") != "openrouter"]
        new_models_list = before_or + active_or_models

        if new_models_list != current_models:
            with open(config.MODELS_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(new_models_list, f, ensure_ascii=False, indent=2)
            logger.info("models.json успешно обновлен: ТОП-8 моделей OpenRouter актуализирован ✅")

    except Exception as e:
        logger.warning(f"Не удалось обновить список OpenRouter: {e}")

# Черный список подстрок: всё, что не является обычной текстовой/мультимодальной LLM
GROQ_IGNORE_KEYWORDS = (
    "whisper",
    "guard",
    "safeguard",
    "orpheus",
    "tts",
    "embed",
    "vision-preview",  # vision-модель вызывается ботом скрыто под капотом
)

def make_groq_display_name(model_id: str) -> str:
    """Генерирует аккуратное имя для кнопок меню Telegram"""
    clean = model_id.split("/")[-1]
    # Убираем технические суффиксы
    clean = clean.replace("-versatile", "").replace("-instant", "")
    parts = clean.split("-")
    short = " ".join([p.capitalize() for p in parts])
    return f"⚡ GRQ: {short[:14]}"

async def sync_groq_models():
    """Синхронизирует активные модели Groq, сохраняя порядок Gemini и OpenRouter"""
    if not config.GROQ_API_KEY:
        return

    try:
        # 1. Получаем список живых моделей прямо от Groq
        response = await groq_client.models.list()
        active_groq_ids = []

        for m in response.data:
            m_id = m.id.lower()
            # Пропускаем служебные модели, транскрибаторы и фильтры безопасности
            if any(keyword in m_id for keyword in GROQ_IGNORE_KEYWORDS):
                continue
            # Если у модели есть признак активности — проверяем его
            if getattr(m, "active", True):
                active_groq_ids.append(m.id)

        if not active_groq_ids:
            return

        # 2. Читаем текущий models.json
        if not os.path.exists(config.MODELS_CONFIG_PATH):
            return

        with open(config.MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
            current_models = json.load(f)

        # 3. Разделяем модели на категории: до Groq (Gemini), сам Groq, и после (OpenRouter)
        before_groq = []
        existing_groq = {}
        after_groq = []
        groq_seen = False

        for item in current_models:
            if item.get("provider") == "groq":
                groq_seen = True
                existing_groq[item["id"]] = item
            elif not groq_seen:
                before_groq.append(item)
            else:
                after_groq.append(item)

        # 4. Собираем актуальный список моделей Groq
        updated_groq = []
        for g_id in active_groq_ids:
            if g_id in existing_groq:
                # Если модель уже была в конфиге — сохраняем её привычное имя
                updated_groq.append(existing_groq[g_id])
            else:
                # Если появилась новая модель — формируем для неё запись
                updated_groq.append({
                    "id": g_id,
                    "name": make_groq_display_name(g_id),
                    "provider": "groq"
                })

        # 5. Склеиваем всё обратно: [Gemini...] + [Актуальный Groq] + [OpenRouter...]
        new_models_list = before_groq + updated_groq + after_groq

        # Перезаписываем models.json только если состав реально изменился
        if new_models_list != current_models:
            with open(config.MODELS_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(new_models_list, f, ensure_ascii=False, indent=2)
            logger.info("models.json успешно обновлен: список моделей Groq актуализирован ✅")

    except Exception as e:
        logger.warning(f"Не удалось обновить список моделей Groq: {e}")

async def sync_api_models_job(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача: обновляет списки моделей всех провайдеров"""
    await sync_gemini_models()     # Сначала обновляем Google
    await sync_groq_models()       # Затем Groq
    await sync_openrouter_models() # И в конце ТОП бесплатных из OpenRouter

async def check_youtube_cookies_job(context: ContextTypes.DEFAULT_TYPE):
    await web_parser.check_cookies_health()

async def check_expirations_job(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки истекающих подписок"""
    # Ищем тех, кому осталось 24 часа
    expiring_chats = await get_and_mark_expiring_subscriptions(hours_left=24)

    for chat_id_str in expiring_chats:
        try:
            await context.bot.send_message(
                chat_id=int(chat_id_str),
                text="⚠️ <b>Внимание!</b>\nСрок вашей подписки на ИИ-бота истекает менее чем через 24 часа. Чтобы не потерять доступ, вы можете заранее продлить его командой /pay",
                parse_mode='HTML'
            )
            logger.info(f"Отправлено предупреждение об истечении подписки в чат {chat_id_str}")
        except Exception as e:
            logger.error(f"Не удалось отправить предупреждение {chat_id_str} (возможно, бот заблокирован): {e}")