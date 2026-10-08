import asyncio
import base64
import logging
import json
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
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = text.replace('<think>', '').replace('</think>', '')
    return text.strip()


# ==========================================
# ОСНОВНЫЕ ЗАПРОСЫ К ПРОВАЙДЕРАМ
# ==========================================

async def query_gemini(model_id: str, history: list, sys_prompt: str, image_bytes: bytes = None) -> str:
    contents = []
    for i, msg in enumerate(history):
        role = "model" if msg["role"] == "assistant" else "user"
        parts = [types.Part.from_text(text=msg["content"])]

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
    err_str = str(error).lower()
    if "timeout" in err_str or "timed out" in err_str:
        return "Таймаут (превышено время ожидания)"
    elif any(x in err_str for x in ["not a valid model", "model_not_found", "does not exist", "404"]):
        return "Модель не найдена, удалена или неверный ID"
    elif any(x in err_str for x in ["rate-limited", "429", "too many requests", "quota exceeded"]):
        return "Истрачены лимиты или сервер временно перегружен"
    elif any(x in err_str for x in ["tokens", "too large", "maximum context"]):
        return "Превышен лимит токенов"
    elif any(x in err_str for x in ["api_key", "401", "403", "unauthorized"]):
        return "Ошибка авторизации (проверьте API-ключ)"
    elif any(x in err_str for x in ["500", "502", "503"]):
        return "Сервер провайдера упал или высокая нагрузка на модель"
    elif "400" in err_str or "invalid_argument" in err_str:
        return "Неверный формат запроса (Bad Request)"
    return "Неизвестная ошибка API"


# ==========================================
# ИНТЕЛЛЕКТУАЛЬНЫЙ РОУТИНГ
# ==========================================

def get_capable_models(required_capability: str, current_model_id: str):
    """Возвращает список моделей, обладающих нужным навыком (text, image, audio).
       Сначала подставляет текущую модель (если она умеет), затем все остальные подходящие.
    """
    all_models = database.load_models_config()
    capable_models = [m for m in all_models if required_capability in m.get("capabilities", [])]

    current_model = next((m for m in capable_models if m["id"] == current_model_id), None)
    ordered = []

    if current_model:
        ordered.append(current_model)

    for m in capable_models:
        if m["id"] != current_model_id:
            ordered.append(m)

    return ordered


async def ask_llm(user_or_chat_id, prompt: str, chat_type: str, user_name: str = "", image_bytes: bytes = None):
    # Помечаем в истории базы данных, что к тексту была прикреплена картинка
    db_prompt = f"[Фото] {prompt}" if image_bytes else prompt
    await database.add_message_to_db(user_or_chat_id, "user", db_prompt)

    history = await database.get_history_from_db(user_or_chat_id, limit=12)
    sys_prompt = get_system_prompt(chat_type, user_name)

    current_model_id = await database.get_user_model(user_or_chat_id)

    # 1. Анализируем контент и определяем, какой навык нужен
    required_capability = "text"
    if image_bytes:
        required_capability = "image"
    elif "[Аудио]:" in prompt:
        required_capability = "audio"

    # 2. Получаем только те модели, которые физически способны обработать этот контент
    ordered_models = get_capable_models(required_capability, current_model_id)

    if not ordered_models:
        return f"⚠️ В списке доступных нейросетей нет ни одной модели, способной обработать этот тип данных (нужен навык: {required_capability})."

    # 3. Если автопереключение выключено, мы можем использовать только ПЕРВУЮ модель из подходящих.
    # Если юзер выбрал текстовую модель, а прислал картинку, первая подходящая модель будет ВРЕМЕННОЙ.
    auto_fallback = await database.get_auto_fallback(user_or_chat_id)
    if not auto_fallback:
        ordered_models = [ordered_models[0]]

    switched = False
    new_model_name = ""
    switch_reason = ""

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

                reply_text = sanitize_text(reply_text)

                # Записываем смену модели только если это обычный текстовый запрос
                # (Чтобы бот не переключился на vision-модель навсегда, если юзер просто скинул одну фотку)
                if model_id != current_model_id and required_capability == "text":
                    switched = True
                    new_model_name = model["name"]
                    await database.set_user_model(user_or_chat_id, model_id)
                elif model_id != current_model_id and required_capability != "text":
                    # Для медиа-запросов мы не сохраняем модель в БД (временная маршрутизация),
                    # но уведомляем юзера, что фото/аудио было обработано другой нейросетью.
                    switched = True
                    new_model_name = model["name"]
                    switch_reason = f"текущая модель не поддерживает {required_capability}"

                await database.add_message_to_db(user_or_chat_id, "assistant", reply_text)

                if switched:
                    show_tech = await database.get_show_tech_details(user_or_chat_id)
                    if show_tech:
                        reason_text = f" (причина: {switch_reason})" if switch_reason else ""
                        reply_text = f"<i>⚠️ Использована <b>{new_model_name}</b>{reason_text}.</i>\n\n" + reply_text
                    else:
                        logger.info(
                            f"Временное переключение для {user_or_chat_id} на {new_model_name} из-за {switch_reason}")

                return reply_text

            except Exception as e:
                switch_reason = parse_llm_error(e)
                logger.warning(f"Модель {model_id} ({provider}) пропущена. Причина: {switch_reason} | Ошибка: {e}")
                continue

    if not auto_fallback:
        return f"⚠️ Модель временно недоступна ({switch_reason}).\nАвто-переключение отключено."

    return f"⚠️ Все модели с поддержкой {required_capability} временно недоступны.\nПоследняя ошибка: {switch_reason}"


def parse_group_reply(raw: str):
    """Неоднозначный/невалидный результат никогда не разрешает вмешательство."""
    try:
        result = json.loads(sanitize_text(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(result, dict) or set(result) != {"action", "answer"}:
        return None
    if result["action"] != "answer" or not isinstance(result["answer"], str):
        return None
    answer = result["answer"].strip()
    if not answer or len(answer) > 1000 or "[REACTION:" in answer:
        return None
    return answer


async def prepare_group_reply(text: str, chat_id: int, user_name: str = "Пользователь"):
    """Проверяет полезность и возвращает тот же готовый ответ, без второй генерации.

    Отказ не запускает каскад: запасная модель нужна только при сбое API/протокола.
    Ничего не записывает в историю и не меняет выбранную модель.
    """
    if len(text.strip()) < 3:
        return None
    current_model_id = await database.get_user_model(chat_id)
    ordered_models = get_capable_models("text", current_model_id)
    if not await database.get_auto_fallback(chat_id):
        ordered_models = ordered_models[:1]
    if not ordered_models:
        return None

    history = await database.get_history_from_db(chat_id, limit=6)
    user_prompt = json.dumps({
        "history": [{"role": m["role"], "content": m["content"][:1500]} for m in history],
        "sender": user_name, "message": text[:6000],
    }, ensure_ascii=False)
    sys_prompt = config.AI_PROMPT_GROUP_REPLY

    async with config.GLOBAL_SEMAPHORE:
        for model in ordered_models:
            model_id, provider = model["id"], model["provider"]
            try:
                if provider == "gemini":
                    conf = types.GenerateContentConfig(system_instruction=sys_prompt, temperature=0.1)
                    response = await asyncio.wait_for(
                        asyncio.to_thread(gemini_client.models.generate_content,
                                          model=model_id, contents=user_prompt, config=conf), timeout=10.0)
                    raw = response.text
                elif provider in ("groq", "openrouter"):
                    client = groq_client if provider == "groq" else openrouter_client
                    response = await asyncio.wait_for(client.chat.completions.create(
                        model=model_id,
                        messages=[{"role": "system", "content": sys_prompt},
                                  {"role": "user", "content": user_prompt}],
                        temperature=0.1), timeout=10.0)
                    raw = response.choices[0].message.content
                else:
                    continue
                # Строгое чтение: слово «да» внутри другого слова больше не считается согласием.
                result = json.loads(sanitize_text(raw))
                if (isinstance(result, dict) and set(result) == {"action", "answer"}
                        and result["action"] == "skip" and result["answer"] == ""):
                    return None
                answer = parse_group_reply(raw)
                if answer is not None:
                    return answer
                logger.warning("Невалидный ответ маршрутизатора %s", model_id)
            except Exception:
                logger.warning("Ошибка проверки полезности: модель %s", model_id, exc_info=True)
    return None


async def is_bot_relevant(text: str, chat_id: int, user_name: str = "Пользователь"):
    """Совместимость для старых вызовов. Обработчик использует prepare_group_reply."""
    return await prepare_group_reply(text, chat_id, user_name) is not None


# ==========================================
# ТРАНСКРИБАЦИЯ АУДИО (ДИАЛОГОВЫЙ ПАРСЕР)
# ==========================================

async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Универсальная транскрибация аудио с авто-переключением по доступным аудио-моделям."""
    last_err = None

    # 1. Загружаем все модели, у которых в capabilities есть "audio" (это и STT типа Whisper, и мультимодалки)
    all_models = database.load_models_config()
    audio_models = [m for m in all_models if "audio" in m.get("capabilities", [])]

    if not audio_models:
        raise RuntimeError("В списке моделей нет ни одной с поддержкой аудио (STT).")

    # 2. Сортируем: сначала быстрые специализированные STT модели от Groq (Whisper), затем остальные
    whisper_models = [m for m in audio_models if "whisper" in m["id"].lower()]
    other_models = [m for m in audio_models if "whisper" not in m["id"].lower()]
    ordered_audio_models = whisper_models + other_models

    for item in ordered_audio_models:
        provider = item["provider"]
        model_name = item["id"]

        try:
            # Сценарий STT через Groq Whisper
            if provider == "groq" and "whisper" in model_name.lower() and config.GROQ_API_KEY:
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
                if text: return text

            # Сценарий STT через мультимодальные модели Google Gemini
            elif provider == "gemini" and config.GEMINI_API_KEY:
                prompt = "Сделай точную дословную расшифровку этого аудиосообщения на языке оригинала. Выведи ТОЛЬКО распознанный текст без каких-либо вводных слов, пояснений и кавычек."
                contents = [types.Part.from_bytes(data=audio_bytes, mime_type=mime_type), prompt]
                conf = types.GenerateContentConfig(temperature=0.1)

                response = await asyncio.wait_for(
                    asyncio.to_thread(gemini_client.models.generate_content, model=model_name, contents=contents,
                                      config=conf),
                    timeout=25.0
                )
                if (response.text or "").strip():
                    logger.info(f"Аудио успешно распознано через Gemini ({model_name}) ✅")
                    return response.text.strip()

            # Сценарий STT через мультимодальные модели OpenRouter (если в будущем понадобится)
            # elif provider == "openrouter": pass

        except Exception as e:
            last_err = e
            logger.warning(f"Транскрибатор {provider}:{model_name} не сработал ({e}). Пробую следующую модель...")
            continue

    raise RuntimeError(f"Все аудио-модели из списка недоступны. Последняя ошибка: {last_err}")