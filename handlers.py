import asyncio
import re
import html
import logging
import io
from datetime import datetime, timezone, timedelta
import pymupdf
from docx import Document
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji
from telegram.ext import ContextTypes

import config
import database
import ai_core
import web_parser
from access import consume_request_and_check, grant_lifetime_access, add_subscription_days

logger = logging.getLogger(__name__)

async def check_admin_rights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, является ли пользователь админом чата или владельцем бота."""
    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type == "private":
        return True
    if config.ADMIN_ID and user_id == config.ADMIN_ID:
        return True

    try:
        member = await chat.get_member(user_id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False

def split_message(message: str):
    parts = []
    while len(message) > config.MAX_MESSAGE_LENGTH:
        split_index = message[:config.MAX_MESSAGE_LENGTH].rfind("\n")
        if split_index == -1:
            split_index = config.MAX_MESSAGE_LENGTH
        parts.append(message[:split_index])
        message = message[split_index:]
    parts.append(message)
    return parts

async def ensure_user_lock(key):
    if key not in config.user_locks:
        config.user_locks[key] = asyncio.Lock()
    return config.user_locks[key]

async def typing_sender(chat_id: int, context: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass

# ==========================================
# БИЛЛИНГ И АДМИН-ПАНЕЛЬ
# ==========================================

async def zombie_allowed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скрытая команда выдачи вечного доступа (только для админа)"""
    if update.effective_user.id != config.ADMIN_ID:
        return  # Если это не владелец, бот промолчит

    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    await grant_lifetime_access(chat_id, chat_type)
    await update.message.reply_text("🧟‍♂️ <b>Доступ разрешен!</b>\nЭтому чату выдан вечный VIP-пропуск.", parse_mode='HTML')


async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    # ❗️ ЗАМЕНИ номер телефона на свой реальный номер по СБП
    text = (
        "💳 <b>Оформление подписки</b>\n\n"
        "Переведите нужную сумму по СБП (Сбер/Т-Банк) на номер:\n"
        "<code>+7-922-720-12-55</code>\n\n"
        "После перевода нажмите кнопку ниже, чтобы я отправил запрос на проверку."
    )

    # Бот понимает, где его вызвали, и предлагает нужный тариф
    if chat_type != "private":
        kb = [[InlineKeyboardButton("💵 Я оплатил 400₽ (Группа 30 дней)", callback_data=f"paid:group:{chat_id}")]]
    else:
        kb = [[InlineKeyboardButton("💵 Я оплатил 100₽ (Личный 30 дней)", callback_data=f"paid:private:{chat_id}")]]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')


async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, tariff_type, chat_id_str = query.data.split(":")

    user = query.from_user
    user_name = user.full_name
    # Достаем @никнейм, если он установлен у юзера
    username_str = f" (@{user.username})" if user.username else ""
    user_id = user.id

    # Достаем название чата (если это группа)
    chat = query.message.chat
    chat_title_str = f"\nНазвание группы: <b>{chat.title}</b>" if chat.type != "private" else ""

    await query.edit_message_text("⏳ Заявка отправлена администратору. Ожидайте подтверждения (обычно 5-15 минут).")

    # Формируем расширенное сообщение ТЕБЕ В ЛИЧКУ
    admin_text = (
        f"💰 <b>НОВАЯ ЗАЯВКА НА ОПЛАТУ!</b>\n\n"
        f"От кого: {user_name}{username_str} (ID: <code>{user_id}</code>)\n"
        f"ID Чата: <code>{chat_id_str}</code>{chat_title_str}\n"
        f"Тип тарифа: <b>{tariff_type.upper()}</b>\n\n"
        f"Проверь баланс. Если деньги пришли, жми кнопку:"
    )

    admin_kb = [
        [InlineKeyboardButton("✅ Подтвердить (Выдать 30 дней)", callback_data=f"admin_confirm:{chat_id_str}:{tariff_type}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject:{chat_id_str}:{user_id}")]
    ]

    try:
        # Отправляем сообщение на твой ADMIN_ID
        await context.bot.send_message(chat_id=config.ADMIN_ID, text=admin_text, reply_markup=InlineKeyboardMarkup(admin_kb), parse_mode='HTML')
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление админу: {e}")


async def admin_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Двойная защита: никто кроме тебя не сможет нажать эти кнопки
    if query.from_user.id != config.ADMIN_ID:
        return

    parts = query.data.split(":")
    action = parts[0]
    target_chat_id = int(parts[1])

    # Сохраняем исходный текст заявки для истории и экранируем спецсимволы
    safe_original_text = html.escape(query.message.text)

    if action == "admin_confirm":
        tariff_type = parts[2]
        # Вызываем функцию начисления 30 дней!
        await add_subscription_days(target_chat_id, tariff_type, 30)

        # Оставляем заявку в истории как подтвержденную (reply_markup=None убирает кнопки)
        new_text = f"✅ <b>ОПЛАТА ПОДТВЕРЖДЕНА (30 дней)</b>\n\n<pre>{safe_original_text}</pre>"
        await query.edit_message_text(new_text, parse_mode='HTML', reply_markup=None)

        # Уведомляем клиента, что бот заработал
        try:
            await context.bot.send_message(chat_id=target_chat_id, text="🎉 <b>Оплата подтверждена!</b>\nВам начислено 30 дней доступа к ИИ. Приятного общения!", parse_mode='HTML')
        except Exception:
            pass

    elif action == "admin_reject":
        target_user_id = int(parts[2])  # Тот, кто нажал кнопку "Оплатил"

        # Оставляем заявку в истории как отклоненную
        new_text = f"❌ <b>ОПЛАТА ОТКЛОНЕНА</b>\n\n<pre>{safe_original_text}</pre>"
        await query.edit_message_text(new_text, parse_mode='HTML', reply_markup=None)

        try:
            await context.bot.send_message(chat_id=target_chat_id, text="❌ <b>Оплата не подтверждена.</b>\nЕсли вы перевели деньги, но заявка отклонена, свяжитесь с администратором @LazyZombie.", parse_mode='HTML')
        except Exception:
            pass


# ==========================================
# ОБРАБОТЧИКИ TELEGRAM
# ==========================================

"""Скрытая команда /setting"""
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin_rights(update, context):
        await update.message.reply_text("❌ У вас нет прав для изменения настроек.")
        return

    chat_key = update.effective_user.id if update.effective_chat.type == 'private' else update.effective_chat.id

    auto_fallback = await database.get_auto_fallback(chat_key)
    keep_context = await database.get_keep_context(chat_key)
    respond_all = await database.get_respond_all(chat_key)
    silent_responses = await database.get_silent_responses(chat_key)
    show_tech_details = await database.get_show_tech_details(chat_key)

    btn1 = "🟢 Авто-переключение ИИ: ВКЛ" if auto_fallback else "🔴 Авто-переключение ИИ: ВЫКЛ"
    btn2 = "🟢 Контекст при смене: СОХРАНЯТЬ" if keep_context else "🔴 Контекст при смене: УДАЛЯТЬ"
    btn3 = "🟢 Отвечать на всё: ВКЛ" if respond_all else "🔴 Отвечать на всё: ВЫКЛ"
    btn4 = "🟢 Тихие ответы: ВКЛ" if silent_responses else "🔴 Тихие ответы: ВЫКЛ"
    btn5 = "🟢 Тех. детали сбоев: ВКЛ" if show_tech_details else "🔴 Тех. детали сбоев: СКРЫТЫ"

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn1, callback_data="setting:auto_fallback")],
        [InlineKeyboardButton(btn2, callback_data="setting:keep_context")],
        [InlineKeyboardButton(btn3, callback_data="setting:respond_all")],
        [InlineKeyboardButton(btn4, callback_data="setting:silent_responses")],
        [InlineKeyboardButton(btn5, callback_data="setting:show_tech_details")]
    ])

    await update.message.reply_text("⚙️ <b>Настройки чата:</b>", reply_markup=markup, parse_mode='HTML')


"""Обработчик кнопок меню настроек"""
async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_admin_rights(update, context):
        await query.answer("❌ У вас нет прав!", show_alert=True)
        return

    chat_key = query.from_user.id if query.message.chat.type == 'private' else query.message.chat.id

    _, setting_type = query.data.split(":")

    if setting_type == "auto_fallback":
        await database.toggle_auto_fallback(chat_key)
    elif setting_type == "keep_context":
        await database.toggle_keep_context(chat_key)
    elif setting_type == "respond_all":
        await database.toggle_respond_all(chat_key)
    elif setting_type == "silent_responses":
        await database.toggle_silent_responses(chat_key)
    elif setting_type == "show_tech_details":
        await database.toggle_show_tech_details(chat_key)

    auto_fallback = await database.get_auto_fallback(chat_key)
    keep_context = await database.get_keep_context(chat_key)
    respond_all = await database.get_respond_all(chat_key)
    silent_responses = await database.get_silent_responses(chat_key)
    show_tech_details = await database.get_show_tech_details(chat_key)

    btn1 = "🟢 Авто-переключение ИИ: ВКЛ" if auto_fallback else "🔴 Авто-переключение ИИ: ВЫКЛ"
    btn2 = "🟢 Контекст при смене: СОХРАНЯТЬ" if keep_context else "🔴 Контекст при смене: УДАЛЯТЬ"
    btn3 = "🟢 Отвечать на всё: ВКЛ" if respond_all else "🔴 Отвечать на всё: ВЫКЛ"
    btn4 = "🟢 Тихие ответы: ВКЛ" if silent_responses else "🔴 Тихие ответы: ВЫКЛ"
    btn5 = "🟢 Тех. детали сбоев: ВКЛ" if show_tech_details else "🔴 Тех. детали сбоев: СКРЫТЫ"

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn1, callback_data="setting:auto_fallback")],
        [InlineKeyboardButton(btn2, callback_data="setting:keep_context")],
        [InlineKeyboardButton(btn3, callback_data="setting:respond_all")],
        [InlineKeyboardButton(btn4, callback_data="setting:silent_responses")],
        [InlineKeyboardButton(btn5, callback_data="setting:show_tech_details")]
    ])

    await query.edit_message_reply_markup(reply_markup=markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "Пользователь"
    chat_id = message.chat_id
    chat_type = message.chat.type
    history_key = user_id if chat_type == "private" else chat_id

    # --- НАЧАЛО: Группировка (буферизация) быстрых сообщений ---
    if history_key not in config.user_buffers:
        config.user_buffers[history_key] = []

    config.user_buffers[history_key].append(message)
    current_len = len(config.user_buffers[history_key])

    # Ждем 1.5 секунды, чтобы собрать все сообщения из серии (пересылка + текст или фотоальбом)
    await asyncio.sleep(1.5)

    # Если список вырос за время ожидания, значит текущий поток не последний, уступаем выполнение
    if len(config.user_buffers.get(history_key, [])) != current_len:
        return

    # Забираем все накопленные сообщения
    messages = config.user_buffers.pop(history_key, [])

    user_text_only = ""
    trigger_msg = messages[-1]
    media_msg = trigger_msg
    is_reply_to_bot = False
    is_reply_to_other = False
    replied_human_name = ""

    # Проверяем, является ли последнее сообщение реплаем
    if trigger_msg.reply_to_message and trigger_msg.reply_to_message.from_user:
        if trigger_msg.reply_to_message.from_user.id == context.bot.id:
            is_reply_to_bot = True
        else:
            is_reply_to_other = True
            replied_human_name = trigger_msg.reply_to_message.from_user.first_name or "пользователя"

    # Склеиваем текст и ищем медиа со всех полученных сообщений
    for msg in messages:
        part_text = msg.text or msg.caption or ""
        if part_text:
            user_text_only += part_text + "\n\n"

        if msg.photo or msg.document or msg.voice or msg.audio:
            media_msg = msg

    # Отделяем чистый текст юзера от будущего контекста с цитатами
    user_text_only = user_text_only.strip()
    text = user_text_only

    # === МАГИЯ РЕПЛАЕВ И ПЕРЕСЫЛОК ===
    if trigger_msg.reply_to_message:
        replied_text = trigger_msg.reply_to_message.text or trigger_msg.reply_to_message.caption or ""
        if replied_text:
            if is_reply_to_bot:
                prefix = "[В ответ на твое сообщение]"
            else:
                prefix = f"[В ответ на сообщение от {replied_human_name}]"

            if text:
                text = f"{text}\n\n{prefix}:\n«{replied_text}»"
            else:
                text = f"{prefix}:\n«{replied_text}»"

    # Умный поиск медиа в реплаях
    if not (
            media_msg.photo or media_msg.document or media_msg.voice or media_msg.audio) and trigger_msg.reply_to_message:
        media_msg = trigger_msg.reply_to_message

    # === ОБРАБОТКА ГОЛОСОВЫХ И АУДИО (Через отказоустойчивый каскад) ===
    if media_msg.voice or media_msg.audio:
        audio_obj = media_msg.voice if media_msg.voice else media_msg.audio

        if audio_obj.file_size and audio_obj.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("⚠️ Аудио слишком большое (лимит 20 МБ).")
            return

        file = await audio_obj.get_file()
        audio_bytes = bytes(await file.download_as_bytearray())

        # Определяем MIME-тип файла (для голоса в Telegram это audio/ogg)
        mime_type = getattr(audio_obj, 'mime_type', None) or 'audio/ogg'

        wait_msg = await update.message.reply_text("🎧 Слушаю аудио/голосовое...", disable_notification=True)
        try:
            # Вызываем каскад моделей STT из ai_core
            transcript = await ai_core.transcribe_audio(audio_bytes, mime_type=mime_type)

            # Добавляем явный контекст, чтобы ИИ понимал формат исходного сообщения
            text = (text + f"\n\n[Пользователь отправил голосовое сообщение. Расшифровка]:\n{transcript}").strip()
            print(text)
            await wait_msg.delete()
        except Exception as e:
            logger.error(f"Ошибка каскада распознавания аудио: {e}")
            await wait_msg.edit_text("⚠️ Ошибка распознавания голоса (все аудио-сервисы временно недоступны).")
            return

    # === ОБРАБОТКА ПРИКРЕПЛЕННЫХ ФАЙЛОВ ===
    elif media_msg.document:
        doc = media_msg.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("⚠️ Файл слишком большой (лимит 20 МБ).")
            return

        wait_msg = await update.message.reply_text(f"📄 Читаю файл {doc.file_name}...", disable_notification=True)
        try:
            file = await doc.get_file()
            file_bytes = bytes(await file.download_as_bytearray())

            # Если это PDF
            if doc.mime_type == 'application/pdf':
                pdf = pymupdf.open(stream=file_bytes, filetype="pdf")
                doc_text = "".join([page.get_text() for page in pdf])
                text += f"\n\n[Содержимое прикрепленного PDF файла {doc.file_name}]:\n{doc_text[:6000]}..."

            # Если это Word документ (DOCX)
            elif doc.file_name.lower().endswith('.docx'):
                docx_file = io.BytesIO(file_bytes)
                document = Document(docx_file)
                doc_text = "\n".join([para.text for para in document.paragraphs])
                text += f"\n\n[Содержимое прикрепленного Word документа {doc.file_name}]:\n{doc_text[:6000]}..."

                # Если это текстовый файл, код или логи (.txt, .py, .log, .json и т.д.)
            else:
                try:
                    doc_text = file_bytes.decode('utf-8')
                    text += f"\n\n[Содержимое прикрепленного файла {doc.file_name}]:\n{doc_text[:6000]}..."
                except UnicodeDecodeError:
                    # Вместо pass добавляем системное сообщение для ИИ!
                    text += f"\n\n[Системное сообщение: Пользователь прикрепил файл {doc.file_name}, но это неизвестный бинарный формат. Бот не смог извлечь из него текст. Сообщи об этом пользователю.]"
                await wait_msg.delete()
        except Exception as e:
            logger.error(f"Ошибка чтения файла: {e}")
            await wait_msg.edit_text("⚠️ Ошибка при чтении файла.")
            return

    # Получаем настройку тихих ответов перед отправкой
    is_silent = await database.get_silent_responses(history_key)

    # ПРОВЕРКА ДОСТУПА (БИЛЛИНГ)
    has_access = await consume_request_and_check(history_key, chat_type)
    if not has_access:
        # Если это личка - показываем меню оплаты. Если группа - просим админов оплатить.
        tariff_msg = (
            "⭐️ <b>Доступ ограничен</b>\n\n"
            "Ваши пробные запросы закончились, или срок подписки истек. "
            "Чтобы бот снова начал отвечать, необходимо оформить подписку. Введите /pay для просмотра тарифов."
        )
        # Отвечаем юзеру только если он напрямую тегнул бота или это личка,
        # Проверяем ник бота только в чисто тексте юзера!
        if is_reply_to_bot or chat_type == "private" or (
                context.bot.username and f"@{context.bot.username.lower()}" in user_text_only.lower()):
            await update.message.reply_text(tariff_msg, parse_mode='HTML')
        return


    now = datetime.now(timezone.utc)
    last_time = config.last_request_time.get(history_key)
    if last_time and (now - last_time).total_seconds() < config.RATE_LIMIT_SECONDS_PER_USER:
        return
    config.last_request_time[history_key] = now

    # СКАЧИВАНИЕ КАРТИНКИ
    image_bytes = None
    if media_msg.photo:
        # Берем самую большую версию картинки [-1]
        photo_file = await media_msg.photo[-1].get_file()  # <--- ИМЕННО MEDIA_MSG!
        image_bytes = bytes(await photo_file.download_as_bytearray())
        # Если юзер скинул просто фото без текста, даем ИИ скрытую системную инструкцию
        if not text:
            text = (
                "[Системная пометка: Пользователь отправил картинку без текста. "
                "Изучи её и отреагируй как живой участник чата. Обязательно учитывай наш предыдущий контекст диалога. "
                "Если это мем или шутка — посмейся или ответь встречной шуткой. "
                "Если текст или интерфейс на иностранном языке — помоги перевести или объясни суть. "
                "Если это просто фото — прокомментируй его по-человечески. "
                "СТРОГО ЗАПРЕЩЕНО использовать фразы вроде 'На картинке изображено', 'Я вижу', 'Здесь показано'. Отвечай естественно.]"
            )
        else:
            text += "\n\n[СИСТЕМНОЕ УВЕДОМЛЕНИЕ: К этому запросу прикреплено ИЗОБРАЖЕНИЕ. Обязательно проанализируй его!]"

    # Финальная защита: если нет ни текста (включая извлеченный из файлов/голоса), ни картинки - выходим
    if not text and not image_bytes:
        return

    try:
        # --- ПРОВЕРКА СОСТОЯНИЯ ТИШИНЫ ---
        is_silenced = False
        if chat_id in config.silenced_chats:
            if datetime.now(timezone.utc) < config.silenced_chats[chat_id]:
                is_silenced = True
            else:
                # Если 10 минут прошло, удаляем чат из списка молчащих
                del config.silenced_chats[chat_id]

        # ВАЖНО: Вырезаем ссылки и ищем вопрос ТОЛЬКО в чистом тексте пользователя!
        text_without_links = re.sub(r'https?://\S+', '', user_text_only).strip()
        has_real_question = "?" in text_without_links

        is_mentioned = False
        if context.bot.username:
            # Ищем тег бота тоже ТОЛЬКО в тексте пользователя!
            is_mentioned = f"@{context.bot.username.lower()}" in user_text_only.lower()

        if chat_type != "private":
            if is_mentioned or is_reply_to_bot:
                # Прямые упоминания и реплаи игнорируют тишину!
                prompt_text = f"{user_name} (ID: {user_id}) пишет: {text}"
                is_short = False
            elif is_silenced:
                # Если чат на паузе, игнорируем всё остальное
                return
            elif await database.get_respond_all(history_key):
                # Если включен режим "Отвечать на всё", бот реагирует на каждое сообщение
                prompt_text = f"{user_name} (ID: {user_id}) пишет: {text}"
                is_short = False
            # Проверяем наличие вопроса ТОЛЬКО в очищенном от ссылок тексте!
            # И ЖЕЛЕЗНОЕ ПРАВИЛО: не отвечаем на вопросы, адресованные другим людям (is_reply_to_other)
            elif has_real_question and not is_reply_to_other and (
            await ai_core.is_bot_relevant(user_text_only, history_key, user_name)):
                prompt_text = f"{user_name} (ID: {user_id}) пишет: {text}\n\nОтветь кратко, 1-2 предложениями."
                is_short = True
            else:
                return
        else:
            prompt_text = text
            is_short = False

        user_lock = await ensure_user_lock(history_key)

        # 🛑 ЗАЩИТА: Если мы уже генерируем ответ этому юзеру/чату - просим подождать
        if user_lock.locked():
            await update.message.reply_text("⏳ Я еще думаю над прошлым вопросом, подождите немного...")
            return

        async with user_lock:
            cancel_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🛑 Отмена", callback_data=f"cancel:{history_key}")]])
            wait_msg = await update.message.reply_text("⏳ Ждём ответ...", reply_markup=cancel_kb, disable_notification=True)

            stop_event = asyncio.Event()
            typing_task = asyncio.create_task(typing_sender(chat_id, context, stop_event))

            # === МАГИЯ WEB-ПАРСЕРА ===
            # Прогоняем текст через парсер ссылок перед отправкой в ИИ
            enriched_prompt, parsed_image_bytes = await web_parser.process_message_for_urls(prompt_text)

            # Если юзер не прикрепил картинку напрямую в телегу, но кинул ссылку на неё - берем картинку по ссылке
            final_image_bytes = image_bytes if image_bytes else parsed_image_bytes

            # Передаем обогащенный промпт (с текстом сайтов) и картинку в LLM
            gen_task = asyncio.create_task(
                ai_core.ask_llm(history_key, enriched_prompt, chat_type, user_name, final_image_bytes))
            config.active_tasks[history_key] = gen_task

            try:
                reply = await gen_task
            except asyncio.CancelledError:
                # Если задачу принудительно отменили кнопкой
                stop_event.set()
                typing_task.cancel()
                return
            finally:
                config.active_tasks.pop(history_key, None)

        stop_event.set()
        typing_task.cancel()

        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        try:
            await wait_msg.delete()
        except Exception:
            pass

        # ПАРСИНГ РЕАКЦИИ ОТ ИИ
        match = re.search(r'\[REACTION:\s*(.*?)\]', reply)
        if match:
            # Очищаем эмодзи от случайных пробелов
            reaction_emoji = match.group(1).strip()
            # Вырезаем тег из текста
            reply = re.sub(r'\[REACTION:\s*.*?\]\s*', '', reply).strip()

            try:
                # Ставим реакцию
                await message.set_reaction(reaction=[ReactionTypeEmoji(reaction_emoji)])
            except Exception as e:
                logger.warning(f"Не удалось поставить реакцию {reaction_emoji}: {e}")

        if not reply:
            return

        if is_short:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔇 Тише", callback_data=f"silence:{chat_id}:{message.message_id}"),
                InlineKeyboardButton("📖 Подробнее", callback_data=f"more:{message.message_id}:{user_id}")
            ]])
            await update.message.reply_text(reply, parse_mode='HTML', reply_markup=keyboard,
                                            disable_notification=is_silent)
        else:
            for part in split_message(reply):
                await update.message.reply_text(part, parse_mode='HTML', disable_notification=is_silent)

    except Exception as e:
        logger.exception("Ошибка в handle_message")


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # Обязательно! Убирает анимацию загрузки на кнопке

    try:
        _, target_key = query.data.split(":")
        target_key = int(target_key)
        if target_key in config.active_tasks:
            config.active_tasks[target_key].cancel()
            await query.edit_message_text("🛑 Генерация отменена пользователем.")
        else:
            await query.edit_message_text("🛑 Запрос уже завершен или отменен.")
    except Exception as e:
        logger.exception("Ошибка в кнопке Отмена")


async def expand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, msg_id_str, user_id_str = query.data.split(':')
        original_msg_id = int(msg_id_str)
        chat_id = query.message.chat_id
        history_key = int(user_id_str) if query.message.chat.type == 'private' else chat_id

        # 1. Захватываем текст короткого ответа прямо из сообщения с кнопкой
        bot_short_answer = query.message.text or ""

        await query.edit_message_reply_markup(reply_markup=None)
        wait_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Собираю подробный ответ...",
            reply_to_message_id=original_msg_id,
            disable_notification=True
        )

        # 2. Явно передаем этот текст нейросети, чтобы она знала, о чем речь
        prompt = (
            "Пожалуйста, распиши вот этот свой краткий ответ максимально подробно "
            f"и развернуто. Дай больше деталей:\n\n«{bot_short_answer}»"
        )

        reply = await ai_core.ask_llm(history_key, prompt, query.message.chat.type)

        is_silent = await database.get_silent_responses(history_key)

        await wait_msg.delete()
        for part in split_message(reply):
            await context.bot.send_message(chat_id=chat_id, text=part, parse_mode='HTML',
                                           disable_notification=is_silent)

    except Exception as e:
        logger.exception("Ошибка при обработке кнопки подробнее")
        await context.bot.send_message(chat_id=query.message.chat.id, text="⚠ Ошибка при формировании ответа.")


async def silence_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        parts = query.data.split(":")
        action = parts[0]
        chat_id = int(parts[1])

        # Читаем текущую клавиатуру
        existing_markup = query.message.reply_markup
        new_inline_keyboard = []

        if action == "silence":
            # 1. Устанавливаем тишину, используя константу SILENCE_MINUTES
            config.silenced_chats[chat_id] = datetime.now(timezone.utc) + timedelta(minutes=config.SILENCE_MINUTES)

            # 2. Меняем кнопку на информативную с крестиком отмены
            for row in existing_markup.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data and btn.callback_data.startswith("silence:"):
                        new_text = f"🔊 Молчу {config.SILENCE_MINUTES} мин. (❌)"
                        new_row.append(InlineKeyboardButton(new_text,
                                                            callback_data=btn.callback_data.replace("silence:",
                                                                                                    "unsilence:")))
                    else:
                        new_row.append(btn)
                new_inline_keyboard.append(new_row)

        elif action == "unsilence":
            # 1. Досрочно снимаем тишину
            if chat_id in config.silenced_chats:
                del config.silenced_chats[chat_id]

            # 2. Возвращаем исходную кнопку
            for row in existing_markup.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data and btn.callback_data.startswith("unsilence:"):
                        new_row.append(InlineKeyboardButton("🔇 Тише",
                                                            callback_data=btn.callback_data.replace("unsilence:",
                                                                                                    "silence:")))
                    else:
                        new_row.append(btn)
                new_inline_keyboard.append(new_row)

        if new_inline_keyboard:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_inline_keyboard))

    except Exception as e:
        logger.error(f"Ошибка в silence_callback: {e}")


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Проверяем права на управление настройками (в ЛС всегда True, в группе - только админы)
    can_change_settings = await check_admin_rights(update, context)

    # 1. Базовая справка для всех
    help_text = (
        "🤖 <b>Умный ИИ-ассистент</b>\n\n"
        "Я умею поддерживать диалог, запоминать контекст и <b>видеть картинки</b> (отправь фото с текстом или мем, и я пойму, что там изображено).\n\n"
        "В группах я не влезаю в каждую беседу. Я отвечаю только если меня тегнуть, ответить на мое сообщение или задать осмысленный вопрос со знаком «?». А еще я умею реагировать эмодзи!\n\n"
        "<b>Команды:</b>\n"
        "🔹 /model — выбрать нейросеть (Gemini, Groq, OpenRouter)\n"
        "🔹 /reset — начать диалог с чистого листа (сбросить память)\n"
        "🔹 /pay — оформить или продлить подписку\n"
        "🔹 /help — показать эту справку"
    )

    # 2. Блок настроек (видят пользователи в ЛС и админы в группах)
    if can_change_settings:
        help_text += (
            "\n\n⚙️ <b>Настройки чата:</b>\n"
            "🔹 /setting — управление режимами ИИ и памятью"
        )

    # 3. Секретный блок ВЛАДЕЛЬЦА (видишь только ТЫ)
    if config.ADMIN_ID and user_id == config.ADMIN_ID:
        help_text += (
            "\n\n👑 <b>Управление биллингом (только для владельца):</b>\n"
            "🧟‍♂️ /zombie_allowed — выдать текущему чату вечный VIP-доступ"
        )

    await update.message.reply_text(help_text, parse_mode='HTML')


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name

    welcome_text = (
        f"Привет, <b>{user_name}</b>! 👋\n\n"
        "Я — твой умный ИИ-помощник. Я умею поддерживать диалог, запоминать контекст нашей беседы и <b>видеть картинки</b> (просто скинь мне фото или мем!).\n\n"
        "Напиши свой первый вопрос или отправь /help, чтобы посмотреть список команд."
    )

    # Добавляем инлайн-кнопку, чтобы сразу вовлечь пользователя
    keyboard = [[InlineKeyboardButton("⚙️ Выбрать нейросеть", callback_data="setting:models_placeholder")]]

    await update.message.reply_text(welcome_text, parse_mode='HTML')

async def reset_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.effective_user.id if update.effective_chat.type == 'private' else update.effective_chat.id
    await database.clear_history_in_db(key)
    await update.message.reply_text("🧼 Контекст диалога очищен!")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_models = database.load_models_config()

    # Оставляем только те модели, которые предназначены для выбора юзером (текстовые флагманы)
    selectable_models = [m for m in all_models if m.get("selectable", True)]

    # Задаем количество столбцов (2)
    COLUMNS = 2

    # Создаем плоский список всех кнопок
    buttons = [
        InlineKeyboardButton(m["name"], callback_data=f"set_model:{m['id']}")
        for m in selectable_models
    ]

    # Разбиваем список на строки по COLUMNS кнопок в каждой
    keyboard = [buttons[i:i + COLUMNS] for i in range(0, len(buttons), COLUMNS)]

    await update.message.reply_text(
        "Выберите активную нейросеть:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def set_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, model_id = query.data.split(":", 1)
    key = query.from_user.id if query.message.chat.type == 'private' else query.message.chat.id
    # Сохраняем выбор пользователя в БД
    current_model_id = await database.get_user_model(key)
    await database.set_user_model(key, model_id)

    # Проверяем настройку сохранения контекста
    keep_context = await database.get_keep_context(key)
    context_msg = ""

    # Если сохранение выключено И модель реально изменилась - сбрасываем базу
    if not keep_context and current_model_id != model_id:
        await database.clear_history_in_db(key)
        context_msg = "\n🧼 <i>(Контекст прошлого диалога удален)</i>"
    # Ищем полную информацию о выбранной модели в конфиге
    all_models = database.load_models_config()
    selected_model = next((m for m in all_models if m["id"] == model_id), None)

    if selected_model:
        name = selected_model["name"]
        provider = selected_model["provider"].upper()
    else:
        name = model_id
        provider = "UNKNOWN"

    await query.message.reply_text(
        f"✅ Установлена модель: <b>{name}</b>\n({provider}: <code>{model_id}</code>){context_msg}",
        parse_mode='HTML'
    )


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.id == context.bot.id: continue
        prompt = (
            f"В нашу группу только что вступил пользователь по имени {member.full_name}. "
            "Напиши для него очень короткое, креативное и теплое приветствие. "
            "Обязательно используй 1-2 эмодзи. СТРОГОЕ ПРАВИЛО: твой ответ должен состоять максимум из 1 или 2 предложений. "
            "Не задавай ему лишних вопросов и не пиши 'Привет, я искусственный интеллект'."
        )
        wait_msg = await update.message.reply_text("⏳ Генерирую приветствие...")
        reply = await ai_core.ask_llm(update.effective_chat.id, prompt, chat_type="group")
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await update.message.reply_text(reply, parse_mode='HTML')