import aiohttp
import json
import os
import re
import logging
from telegram.ext import ContextTypes

import config
from ai_core import groq_client
from access import get_and_mark_expiring_subscriptions
import web_parser

logger = logging.getLogger(__name__)

# ==========================================
# 1. ПАРСЕР GOOGLE GEMINI
# ==========================================
# В API Google нет поля modalities, поэтому мы жестко исключаем служебные модели.
GEMINI_IGNORE_KEYWORDS = (
    "embed", "aqa", "bison", "gecko", "learnlm", "medlm", "robotics"
)


def make_gemini_display_name(display_name: str) -> str:
    name = display_name.replace("Gemini", "").replace("preview", "prev").replace("Experimental", "exp").strip()
    return f"✨ GEM: {name[:14]}"


def get_gemini_sort_score(model_id: str) -> float:
    score = 0.0
    match = re.search(r'gemini-(\d+(?:\.\d+)?)', model_id)
    if match: score = float(match.group(1)) * 100
    if "lite" in model_id: score -= 2
    if "pro" in model_id: score -= 5
    if "preview" in model_id or "exp" in model_id: score -= 10
    return score


async def sync_gemini_models():
    if not config.GEMINI_API_KEY: return

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=100&key={config.GEMINI_API_KEY}"
            async with session.get(url) as resp:
                if resp.status != 200: return
                models_data = (await resp.json()).get("models", [])

        parsed_models = []
        for m in models_data:
            m_id = m.get("name", "").replace("models/", "")

            # Пропускаем модели, которые ничего не генерируют
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods and "bidiGenerateContent" not in methods:
                continue

            m_id_lower = m_id.lower()
            if any(keyword in m_id_lower for keyword in GEMINI_IGNORE_KEYWORDS):
                continue

            capabilities = []
            selectable = False

            # Определяем возможности по названию
            if "gemini-" in m_id_lower and ("flash" in m_id_lower or "pro" in m_id_lower):
                if "tts" in m_id_lower:
                    capabilities = ["text", "audio_out"]
                elif "live" in m_id_lower or "transcribe" in m_id_lower:
                    capabilities = ["audio", "text"]
                elif "image" in m_id_lower:
                    capabilities = ["text", "image", "image_out"]
                else:
                    # Обычные флагманы умеют всё
                    capabilities = ["text", "image", "audio", "video"]
                    selectable = True
            elif "nano-banana" in m_id_lower:
                capabilities = ["text", "image_out"]
            elif "lyria" in m_id_lower:
                capabilities = ["audio_out"]
            else:
                continue

            parsed_models.append({
                "id": m_id,
                "name": make_gemini_display_name(m.get("displayName", m_id)),
                "provider": "gemini",
                "capabilities": capabilities,
                "selectable": selectable,
                "score": get_gemini_sort_score(m_id) if selectable else 0.0
            })

        # Оставляем только ТОП-8 для меню (selectable=True), остальные (служебные) просто храним в базе
        selectable_models = sorted([m for m in parsed_models if m["selectable"]], key=lambda x: x["score"],
                                   reverse=True)[:8]
        service_models = [m for m in parsed_models if not m["selectable"]]

        active_gemini_models = selectable_models + service_models
        for m in active_gemini_models: m.pop("score", None)

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