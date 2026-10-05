import asyncio
import base64
import logging
import re
from google import genai
from google.genai import types
from openai import AsyncOpenAI

import config
import database

logger = logging.getLogger(__name__)

# Клиенты
openrouter_client = AsyncOpenAI(
    api_key=config.OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)
gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
groq_client = AsyncOpenAI(api_key=config.GROQ_API_KEY, base_url=config.GROQ_URL)

def get_system_prompt(chat_type, user_name=""):
    if chat_type == "private":
        base_prompt = config.AI_PROMPT_PM or ""
        if user_name:
            base_prompt += f"\n\n[Системная информация: собеседника зовут {user_name}]"
    else:
        base_prompt = f"{config.AI_PROMPT_GM}\n{config.AI_PROMPT_TGM}"

    # Секретная инструкция с правильным списком Telegram-реакций и разрешением на молчание
    reaction_rule = (
        "\n\n[СЕКРЕТНАЯ ИНСТРУКЦИЯ (СТРОГО): Если уместно отреагировать эмоцией, начни ответ с тега [REACTION: эмодзи]. "
        "Разрешен ТОЛЬКО этот точный список эмодзи: "
        "❤️, 👌, 👍, 😁, 🔥, 🤡, 🤣, 👎, 🥰, 👏, 🤔, 🤯, 😱, 🤬, 😢, 🎉, 🤩, 🤮, 💩, 🙏, 🕊️, 🥱, 🥴, 😍, 🐳, ❤️‍🔥, 🌚, 🌭, 💯, ⚡, 🍌, 🏆, 💔, 🤨, 😐, 🍓, 🍾, 💋, 🖕, 😈, 😴, 😭, 🤓, 👻, 👨‍💻, 👀, 🎃, 🙈, 😇, 😨, 🤝, ✍️, 🤗, 🫡, 🎅, 🎄, ☃️, 💅, 🤪, 🗿, 🆒, 💘, 🙉, 🦄, 😘, 💊, 🙊, 😎, 👾, 🤷‍♂️, 🤷, 🤷‍♀️, 😡. "
        "\n❗️ ПРАВИЛО ВЫЖИВАНИЯ: Если идеального эмодзи нет в этом списке — НЕ ПИШИ ТЕГ ВООБЩЕ. "
        "Использование эмодзи вне списка ВЫЗЫВАЕТ КРИТИЧЕСКУЮ ОШИБКУ API ТЕЛЕГРАМА И ПОЛОМКУ БОТА. "
        "Если текст ответа не нужен, отвечай ТОЛЬКО одним тегом из списка.]"
    )

    return base_prompt + reaction_rule

def sanitize_text(text: str) -> str:
    """Вырезает теги <think> от DeepSeek и очищает текст от невалидного HTML"""
    if not text:
        return ""
    # Вырезаем блок <think> ... </think> вместе с содержимым
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # На всякий случай удаляем висячие теги, если модель их не закрыла
    text = text.replace('<think>', '').replace('</think>', '')
    return text.strip()

# ==========================================
# ОСНОВНАЯ ЛОГИКА ИИ
# ==========================================

async def query_gemini(model_id: str, history: list, sys_prompt: str, image_bytes: bytes = None) -> str:
    contents = []
    for i, msg in enumerate(history):
        role = "model" if msg["role"] == "assistant" else "user"
        parts = [types.Part.from_text(text=msg["content"])]

        # Если есть картинка, прикрепляем её к последнему сообщению пользователя
        if image_bytes and i == len(history) - 1 and role == "user":
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

        contents.append(types.Content(role=role, parts=parts))

    conf = types.GenerateContentConfig(system_instruction=sys_prompt if sys_prompt else None, temperature=0.7)
    response = await asyncio.wait_for(
        asyncio.to_thread(gemini_client.models.generate_content, model=model_id, contents=contents, config=conf),
        timeout=15.0
    )
    return response.text.strip()


async def query_groq(model_id: str, history: list, sys_prompt: str, image_bytes: bytes = None) -> str:
    # Магия Groq: Если прикреплена картинка, а текущая модель не поддерживает зрение,
    # временно и незаметно переключаем запрос на самую мощную vision-модель!
    if image_bytes and "vision" not in model_id.lower():
        model_id = "llama-3.2-90b-vision-preview"

    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})

    for i, msg in enumerate(history):
        if image_bytes and i == len(history) - 1 and msg["role"] == "user":
            b64_img = base64.b64encode(image_bytes).decode('utf-8')
            content = [
                {"type": "text", "text": msg["content"]},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
            ]
            messages.append({"role": msg["role"], "content": content})
        else:
            messages.append({"role": msg["role"], "content": msg["content"]})

    response = await asyncio.wait_for(
        groq_client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=0.7,
            max_tokens=1000
        ),
        timeout=15.0
    )
    return response.choices[0].message.content.strip()


async def query_openrouter(model_id: str, history: list, sys_prompt: str, image_bytes: bytes = None) -> str:
    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})

    for i, msg in enumerate(history):
        if image_bytes and i == len(history) - 1 and msg["role"] == "user":
            b64_img = base64.b64encode(image_bytes).decode('utf-8')
            content = [
                {"type": "text", "text": msg["content"]},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
            ]
            messages.append({"role": msg["role"], "content": content})
        else:
            messages.append({"role": msg["role"], "content": msg["content"]})

    response = await asyncio.wait_for(
        openrouter_client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=0.7,
        ),
        timeout=15.0,
    )
    return response.choices[0].message.content.strip()


def parse_llm_error(error: Exception) -> str:
    """Универсальный переводчик ошибок от любых API-провайдеров на человеческий язык"""
    err_str = str(error).lower()

    if "timeout" in err_str or "timed out" in err_str:
        return "Таймаут (превышено время ожидания)"
    elif any(x in err_str for x in ["not a valid model", "model_not_found", "does not exist", "404"]):
        return "Модель не найдена, удалена или неверный ID"
    elif any(x in err_str for x in ["rate-limited", "429", "too many requests", "quota exceeded"]):
        return "Истрачены лимиты или сервер временно перегружен"
    elif any(x in err_str for x in ["tokens", "too large", "maximum context"]):
        return "Превышен лимит токенов (слишком длинная история)"
    elif any(x in err_str for x in ["api_key", "401", "403", "unauthorized"]):
        return "Ошибка авторизации (проверьте API-ключ)"
    elif any(x in err_str for x in ["500", "502", "503"]):
        return "Сервер провайдера упал или высокая нагрузка на модель (ошибка 500+)"
    elif "400" in err_str or "invalid_argument" in err_str:
        return "Неверный формат запроса (Bad Request)"

    return "Неизвестная ошибка API"

async def ask_llm(user_or_chat_id, prompt: str, chat_type: str, user_name: str = "", image_bytes: bytes = None):
    # Помечаем в истории базы данных, что к тексту была прикреплена картинка
    db_prompt = f"[Фото] {prompt}" if image_bytes else prompt
    await database.add_message_to_db(user_or_chat_id, "user", db_prompt)

    history = await database.get_history_from_db(user_or_chat_id, limit=12)
    sys_prompt = get_system_prompt(chat_type, user_name)

    current_model_id = await database.get_user_model(user_or_chat_id)

    ordered_models = database.get_ordered_models(current_model_id)

    # ПРОВЕРКА НАСТРОЙКИ ПЕРЕКЛЮЧЕНИЯ
    auto_fallback = await database.get_auto_fallback(user_or_chat_id)
    if not auto_fallback:
        ordered_models = [ordered_models[0]]  # Оставляем только текущую модель

    switched = False
    new_model_name = ""
    switch_reason = ""  # Переменная для хранения причины сбоя

    async with config.GLOBAL_SEMAPHORE:
        for model in ordered_models:
            model_id = model["id"]
            provider = model["provider"]

            try:
                if provider == "gemini":
                    reply_text = await query_gemini(model_id, history, sys_prompt, image_bytes)
                elif provider == "groq":
                    reply_text = await query_groq(model_id, history, sys_prompt, image_bytes)
                elif provider == "openrouter":
                    reply_text = await query_openrouter(model_id, history, sys_prompt, image_bytes)
                else:
                    continue

                # Очищаем ответ от невалидных тегов ДО сохранения в базу и отправки
                reply_text = sanitize_text(reply_text)

                if model_id != current_model_id:
                    switched = True
                    new_model_name = model["name"]
                    await database.set_user_model(user_or_chat_id, model_id)

                await database.add_message_to_db(user_or_chat_id, "assistant", reply_text)

                if switched:
                    # Проверяем, хочет ли пользователь видеть технические детали сбоя
                    show_tech = await database.get_show_tech_details(user_or_chat_id)
                    if show_tech:
                        # Добавляем причину падения предыдущей модели прямо в сообщение!
                        reason_text = f" (причина: {switch_reason})" if switch_reason else ""
                        reply_text = f"<i>⚠️ Переключено на <b>{new_model_name}</b>{reason_text}.</i>\n\n" + reply_text
                    else:
                        # Если скрыто - просто логируем в консоль для админа
                        logger.info(f"Тихое авто-переключение для {user_or_chat_id} на {new_model_name} из-за {switch_reason}")

                return reply_text

            except Exception as e:
                # 1. Прогоняем сырую ошибку через наш парсер
                switch_reason = parse_llm_error(e)

                # 2. Выводим в консоль сервера и красивую причину, и сырую ошибку для дебага
                logger.warning(
                    f"Модель {model_id} ({provider}) пропущена. Причина: {switch_reason} | Сырая ошибка: {e}")
                continue

    if not auto_fallback:
        return f"⚠️ Модель <b>{current_model_id}</b> временно недоступна ({switch_reason}).\nАвто-переключение отключено."

    return f"⚠️️ Все модели из списка временно недоступны.\nПоследняя ошибка: {switch_reason}"


async def is_bot_relevant(text: str, chat_id: int, user_name: str = "Пользователь"):
    # Базовая защита: не дергаем API из-за одного символа "?"
    if len(text.strip()) < 3:
        return False

    sys_prompt = config.AI_PROMPT_IS_RELEVANT_QUESTION
    user_prompt = f"Сообщение от пользователя {user_name}: {text}"

    current_model_id = await database.get_user_model(chat_id)

    ordered_models = database.get_ordered_models(current_model_id)
    auto_fallback = await database.get_auto_fallback(chat_id)
    if not auto_fallback:
        ordered_models = [ordered_models[0]]

    for model in ordered_models:
        model_id = model["id"]
        provider = model["provider"]

        try:
            if provider == "gemini":
                conf = types.GenerateContentConfig(system_instruction=sys_prompt, temperature=0.1)
                response = await asyncio.wait_for(
                    asyncio.to_thread(gemini_client.models.generate_content, model=model_id, contents=user_prompt,
                                      config=conf),
                    timeout=10.0
                )
                # Очищаем от возможных <think> и ищем слово "да"
                reply = sanitize_text(response.text)
                return "да" in reply.lower()

            elif provider == "groq":
                response = await asyncio.wait_for(
                    groq_client.chat.completions.create(
                        model=model_id,
                        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                        temperature=0.1
                    ),
                    timeout=10.0
                )
                reply = sanitize_text(response.choices[0].message.content or "")
                return "да" in reply.lower()

            elif provider == "openrouter":  # БЛОК ДЛЯ OPENROUTER
                response = await asyncio.wait_for(
                    openrouter_client.chat.completions.create(
                        model=model_id,
                        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                        temperature=0.1
                    ),
                    timeout=10.0
                )
                reply = sanitize_text(response.choices[0].message.content or "")
                return "да" in reply.lower()

        except Exception as e:
            logger.warning(f"Проверка релевантности: Модель {model_id} недоступна ({e}). Иду к следующей...")
            continue

    return False

# ==========================================
# КАСКАД МОДЕЛЕЙ ТРАНСКРИБАЦИИ АУДИО
# ==========================================
AUDIO_TRANSCRIBE_MODELS = [
    {"provider": "groq", "model": "whisper-large-v3"},
    {"provider": "groq", "model": "whisper-large-v3-turbo"},
    {"provider": "gemini", "model": "gemini-2.5-flash"},
    {"provider": "gemini", "model": "gemini-3.8-flash"},
]

async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Универсальная транскрибация аудио с авто-переключением между Groq и Gemini."""
    last_err = None

    for item in AUDIO_TRANSCRIBE_MODELS:
        provider = item["provider"]
        model_name = item["model"]

        try:
            # 1. Попытка через Groq Whisper
            if provider == "groq" and config.GROQ_API_KEY:
                # Нормализуем расширение для заголовка Groq
                ext = "ogg" if "ogg" in mime_type else "mp3"
                transcript = await asyncio.wait_for(
                    groq_client.audio.transcriptions.create(
                        file=(f'audio.{ext}', audio_bytes, mime_type),
                        model=model_name,
                        response_format="text"
                    ),
                    timeout=20.0
                )
                text = transcript.strip() if isinstance(transcript, str) else getattr(transcript, "text", "").strip()
                if text:
                    return text

            # 2. Резервная попытка через Google Gemini (нативное распознавание)
            elif provider == "gemini" and config.GEMINI_API_KEY:
                prompt = (
                    "Сделай точную дословную расшифровку этого аудиосообщения на языке оригинала. "
                    "Выведи ТОЛЬКО распознанный текст без каких-либо вводных слов, пояснений и кавычек."
                )
                contents = [
                    types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                    prompt
                ]
                conf = types.GenerateContentConfig(temperature=0.1)

                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        gemini_client.models.generate_content,
                        model=model_name,
                        contents=contents,
                        config=conf
                    ),
                    timeout=25.0
                )
                text = (response.text or "").strip()
                if text:
                    logger.info(f"Аудио успешно распознано через резерв Gemini ({model_name}) ✅")
                    return text

        except Exception as e:
            last_err = e
            logger.warning(f"Транскрибатор {provider}:{model_name} не сработал ({e}). Пробую следующую модель...")
            continue

    raise RuntimeError(f"Все аудио-модели из списка недоступны. Последняя ошибка: {last_err}")