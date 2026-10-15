import aiohttp
import json
import os
import re
import logging
from telegram.ext import ContextTypes
from datetime import datetime

import config
from access import get_and_mark_expiring_subscriptions
import web_parser
import database
import holidays
import ai_core

logger = logging.getLogger(__name__)

# ==========================================
# 1. ПАРСЕР GOOGLE GEMINI
# ==========================================
# В API Google нет поля modalities, поэтому мы жестко исключаем служебные модели.
GEMINI_IGNORE_KEYWORDS = (
    "embed", "aqa", "bison", "gecko", "learnlm", "medlm", "robotics",
    "computer-use", "antigravity", "deep-research", "customtools", "veo", "omni"
)


def make_gemini_display_name(display_name: str) -> str:
    name = display_name.replace("Gemini", "").replace("Preview", "Prev").replace("preview", "prev").replace("Experimental", "Exp").strip()
    return f"✨ GEM: {name[:15]}"

def get_gemini_sort_score(model_id: str) -> float:
    """Оценивает модель для выстраивания идеальной очереди фолбэка и меню."""
    score = 0.0
    match = re.search(r'gemini-(\d+(?:\.\d+)?)', model_id)
    if match:
        score = float(match.group(1)) * 100

    # Приоритет внутри одного поколения: Argon -> Flash -> Pro -> Flash-Lite
    if "argon" in model_id:
        score += 15
    if "pro" in model_id:
        score -= 3
    if "lite" in model_id:
        score -= 6

    # Preview и экспериментальные версии опускаем ниже стабильных релизов
    if "preview" in model_id or "exp" in model_id:
        score -= 40

    return score


async def sync_gemini_models():
    if not config.GEMINI_API_KEY:
        return

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=100&key={config.GEMINI_API_KEY}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return
                models_data = (await resp.json()).get("models", [])

        parsed_models = []
        seen_base_ids = set()

        # Сначала сортируем сырой список так, чтобы стабильные версии шли перед preview
        models_data.sort(key=lambda x: ("preview" in x.get("name", ""), x.get("name", "")))

        for m in models_data:
            m_id = m.get("name", "").replace("models/", "")
            m_id_lower = m_id.lower()

            # 1. Берем ТОЛЬКО модели с поддержкой стандартного generateContent
            # (Чистые Live-модели с bidiGenerateContent требуют WebSocket и роняют обычный API-вызов)
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue

            # 2. Игнорируем спец-агентов, роботов и алиасы "-latest" (чтобы не плодить дубли в меню)
            if any(keyword in m_id_lower for keyword in GEMINI_IGNORE_KEYWORDS):
                continue
            if m_id_lower.endswith("-latest"):
                continue

            # 3. Защита от дублей: если уже есть стабильная "gemini-3.1-flash-lite",
            # то старую "gemini-3.1-flash-lite-preview" пропускаем
            base_id = m_id_lower.replace("-preview", "").replace("-exp", "")
            if base_id in seen_base_ids and ("preview" in m_id_lower or "exp" in m_id_lower):
                continue
            seen_base_ids.add(base_id)

            capabilities = []
            is_chat_candidate = False

            # 4. Распределяем возможности
            if "tts" in m_id_lower:
                capabilities = ["text", "audio_out"]
            elif "transcribe" in m_id_lower:
                capabilities = ["audio", "text"]
            elif "image" in m_id_lower or "nano-banana" in m_id_lower:
                capabilities = ["text", "image", "image_out"]
            elif "lyria" in m_id_lower:
                capabilities = ["audio_out"]
            elif m_id_lower.startswith("gemma-"):
                # Открытые модели Gemma оставляем в резерве (только текст/картинки)
                capabilities = ["text", "image"]
            else:
                # Все основные модели Gemini (4 Argon, 3.8..3.0 Flash/Pro, 2.5 Flash/Pro)
                capabilities = ["text", "image", "audio", "video"]
                is_chat_candidate = True

            parsed_models.append({
                "id": m_id,
                "name": make_gemini_display_name(m.get("displayName", m_id)),
                "provider": "gemini",
                "capabilities": capabilities,
                "selectable": False,  # Включим ниже для ТОП-10
                "is_chat_candidate": is_chat_candidate,
                "score": get_gemini_sort_score(m_id) if is_chat_candidate else -1000.0
            })

        # Сортируем кандидатов по нашему рейтингу (от 4 Argon до 2.5 Flash-Lite)
        chat_models = sorted(
            [m for m in parsed_models if m["is_chat_candidate"]],
            key=lambda x: x["score"],
            reverse=True
        )
        service_models = [m for m in parsed_models if not m["is_chat_candidate"]]

        # Первые 10 лучших моделей показываем в меню /model (туда войдут и 4 Argon, и 3.x, и 2.5 Flash),
        # а остальные чат-модели оставляем в базе как скрытый резерв (selectable=False)
        for i, m in enumerate(chat_models):
            if i < 10:
                m["selectable"] = True

        active_gemini_models = chat_models + service_models
        for m in active_gemini_models:
            m.pop("score", None)
            m.pop("is_chat_candidate", None)

        await _update_json_config("gemini", active_gemini_models)

    except Exception as e:
        logger.warning(f"Ошибка парсинга Gemini: {e}")


# ==========================================
# 2. ПАРСЕР OPENROUTER
# ==========================================
def make_or_display_name(name: str) -> str:
    clean = name.replace("(free)", "").replace("Instruct", "").strip()
    if ":" in clean: clean = clean.split(":", 1)[1].strip()
    return f"🔋 OR: {clean[:15]}"


async def sync_openrouter_models():
    if not config.OPENROUTER_API_KEY: return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://openrouter.ai/api/v1/models?sort=most-popular") as resp:
                if resp.status != 200: return
                models_data = (await resp.json()).get("data", [])

        active_or_models = []
        selectable_count = 0

        for m in models_data:
            m_id = m.get("id", "")
            pricing = m.get("pricing", {})
            arch = m.get("architecture", {})

            # Исключаем платные модели
            is_free = pricing.get("prompt") == "0" and pricing.get("completion") == "0"
            if not is_free and not m_id.endswith(":free"):
                continue

            m_id_lower = m_id.lower()
            if m_id_lower.startswith("google/") or m_id_lower.startswith("groq/"):
                continue

            # Читаем модальности напрямую из API
            in_mods = arch.get("input_modalities", ["text"])
            out_mods = arch.get("output_modalities", ["text"])

            capabilities = []
            if "text" in in_mods: capabilities.append("text")
            if "image" in in_mods: capabilities.append("image")
            if "audio" in in_mods: capabilities.append("audio")

            if "image" in out_mods: capabilities.append("image_out")
            if "audio" in out_mods: capabilities.append("audio_out")

            # Модель показывается в меню, только если она умеет генерировать текст
            selectable = ("text" in out_mods) and ("text" in in_mods)

            active_or_models.append({
                "id": m_id,
                "name": make_or_display_name(m.get("name", "")),
                "provider": "openrouter",
                "capabilities": capabilities,
                "selectable": selectable
            })

            if selectable:
                selectable_count += 1
            if selectable_count >= 8:  # Ограничиваем ТОП-8 в меню
                break

        await _update_json_config("openrouter", active_or_models)

    except Exception as e:
        logger.warning(f"Ошибка парсинга OpenRouter: {e}")


# ==========================================
# 3. ПАРСЕР GROQ
# ==========================================
# Исключаем только Guard-модели, так как аудио мы теперь поддерживаем
GROQ_IGNORE_KEYWORDS = ("guard", "safeguard")


def make_groq_display_name(model_id: str) -> str:
    clean = model_id.split("/")[-1].replace("-versatile", "").replace("-instant", "")
    short = " ".join([p.capitalize() for p in clean.split("-")])
    return f"⚡ GRQ: {short[:14]}"


async def sync_groq_models():
    if not config.GROQ_API_KEY: return

    try:
        # Groq API отдает модальности, но python SDK groq_client.models.list()
        # их не возвращает, поэтому делаем прямой aiohttp запрос к API.
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
            async with session.get("https://api.groq.com/openai/v1/models", headers=headers) as resp:
                if resp.status != 200: return
                models_data = (await resp.json()).get("data", [])

        active_groq_models = []

        for m in models_data:
            m_id = m.get("id", "")
            if any(k in m_id.lower() for k in GROQ_IGNORE_KEYWORDS):
                continue
            if not m.get("active", True):
                continue

            in_mods = m.get("input_modalities", ["text"])
            out_mods = m.get("output_modalities", ["text"])

            capabilities = []
            if "text" in in_mods: capabilities.append("text")
            if "image" in in_mods: capabilities.append("image")
            if "audio" in in_mods: capabilities.append("audio")

            if "speech" in out_mods: capabilities.append("audio_out")
            if "transcription" in out_mods: capabilities.append("text")  # Whisper выдает текст

            # В меню выводим только чат-модели
            selectable = ("text" in in_mods) and ("text" in out_mods)

            active_groq_models.append({
                "id": m_id,
                "name": make_groq_display_name(m_id),
                "provider": "groq",
                "capabilities": capabilities,
                "selectable": selectable
            })

        await _update_json_config("groq", active_groq_models)

    except Exception as e:
        logger.warning(f"Ошибка парсинга Groq: {e}")


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ДЖОБЫ
# ==========================================

async def _update_json_config(provider_name: str, new_provider_models: list):
    """Безопасно перезаписывает блок конкретного провайдера в models.json"""
    if not os.path.exists(config.MODELS_CONFIG_PATH):
        current_models = []
    else:
        with open(config.MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
            current_models = json.load(f)

    # Собираем модели других провайдеров
    other_models = [m for m in current_models if m.get("provider") != provider_name]

    # Чтобы соблюдать красивый порядок (Сначала Gemini, потом Groq, потом OpenRouter)
    if provider_name == "gemini":
        final_list = new_provider_models + [m for m in other_models if m["provider"] == "groq"] + [m for m in
                                                                                                   other_models if m[
                                                                                                       "provider"] == "openrouter"]
    elif provider_name == "groq":
        final_list = [m for m in other_models if m["provider"] == "gemini"] + new_provider_models + [m for m in
                                                                                                     other_models if m[
                                                                                                         "provider"] == "openrouter"]
    else:
        final_list = [m for m in other_models if m["provider"] == "gemini"] + [m for m in other_models if m[
            "provider"] == "groq"] + new_provider_models

    if final_list != current_models:
        with open(config.MODELS_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(final_list, f, ensure_ascii=False, indent=2)
        logger.info(f"models.json успешно обновлен для провайдера: {provider_name} ✅")


async def sync_api_models_job(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача: обновляет списки моделей всех провайдеров"""
    await sync_gemini_models()
    await sync_groq_models()
    await sync_openrouter_models()


async def check_youtube_cookies_job(context: ContextTypes.DEFAULT_TYPE):
    await web_parser.check_cookies_health()


async def check_expirations_job(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки истекающих подписок"""
    expiring_chats = await get_and_mark_expiring_subscriptions(hours_left=24)

    for chat_id_str in expiring_chats:
        try:
            await context.bot.send_message(
                chat_id=int(chat_id_str),
                text="⚠️ <b>Внимание!</b>\nСрок вашей подписки на ИИ-бота истекает менее чем через 24 часа. Чтобы не потерять доступ, вы можете заранее продлить его командой /pay",
                parse_mode='HTML'
            )
        except Exception:
            pass

async def daily_custom_holidays_job(context: ContextTypes.DEFAULT_TYPE):
    """Каждое утро проверяет: есть ли сегодня в чатах чьи-то ДР или особые даты, и поздравляет!"""
    now = datetime.now()
    enabled_chats = await database.get_all_holiday_enabled_chats()

    for chat_id_str in enabled_chats:
        try:
            chat_id = int(chat_id_str)
            todays_events = await database.get_custom_events_by_date(chat_id, now.day, now.month)
            if not todays_events:
                continue

            events_desc = "\n".join(
                [f"- {'День рождения' if e['category']=='birthday' else 'Праздник чата'}: {e['title']}" for e in todays_events]
            )
            prompt = (
                f"[Системное событие: сегодня ({now.day:02d}.{now.month:02d}) в нашем чате особенный день!]\n"
                f"Список событий:\n{events_desc}\n\n"
                "Напиши теплое, душевное и креативное поздравление от лица бота для участников чата! "
                "Используй праздничные эмодзи, но не пиши тег [REACTION]."
            )
            reply = await ai_core.ask_llm(chat_id, prompt, chat_type="group", save_to_history=False)
            if reply:
                await context.bot.send_message(chat_id=chat_id, text=reply, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Ошибка поздравления в чате {chat_id_str}: {e}")


async def monday_weekly_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """Каждый понедельник утром отправляет расписание праздников и ДР на неделю"""
    enabled_chats = await database.get_all_holiday_enabled_chats()
    for chat_id_str in enabled_chats:
        try:
            chat_id = int(chat_id_str)
            digest = await holidays.build_monday_digest(chat_id)
            if digest:
                await context.bot.send_message(chat_id=chat_id, text=digest, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Ошибка еженедельного дайджеста в чате {chat_id_str}: {e}")