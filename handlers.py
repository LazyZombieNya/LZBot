import asyncio
import re
import html
import logging
import holidays
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
import rich_utils
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
    return rich_utils.split_text(message, config.MAX_MESSAGE_LENGTH)

async def download_telegram_media_bytes(media_obj, progress_callback=None) -> bytes:
    """
    Скачивает любой медиафайл из сообщения Telegram:
    - Если файл <= 20 МБ — качает быстрым стандартным методом Bot API (get_file).
    - Если файл > 20 МБ (или публичный Bot API вернул 'File is too big') —
      автоматически качает его напрямую с дата-центра Telegram через MTProto / Local Bot API
      и передает живой прогресс скачивания в progress_callback!
    """
    import media_processor

    file_size = getattr(media_obj, "file_size", 0) or 0
    # 1. Если размер файла известен и он сразу больше 20 МБ — идем напрямую в обход лимита Bot API
    if file_size > 20 * 1024 * 1024:
        direct_bytes = await media_processor.download_large_telegram_file(
            media_obj.file_id, progress_callback=progress_callback
        )
        if direct_bytes:
            return direct_bytes

    # 2. Стандартный путь через Bot API (для файлов до 20 МБ)
    try:
        tg_file = await media_obj.get_file()
        return bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        # 3. Если стандартный Bot API всё равно ответил "File is too big" — пробуем прямой MTProto-загрузчик
        if "File is too big" in str(e):
            direct_bytes = await media_processor.download_large_telegram_file(
                media_obj.file_id, progress_callback=progress_callback
            )
            if direct_bytes:
                return direct_bytes
        raise e


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
        return

    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    await grant_lifetime_access(chat_id, chat_type)
    await update.message.reply_text("🧟‍♂️ <b>Доступ разрешен!</b>\nЭтому чату выдан вечный VIP-пропуск.", parse_mode='HTML')


async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    text = (
        "💳 <b>Оформление подписки</b>\n\n"
        "Переведите нужную сумму по СБП (Сбер/Т-Банк) на номер:\n"
        "<code>+7-922-720-12-55</code>\n\n"
        "После перевода нажмите кнопку ниже, чтобы я отправил запрос на проверку."
    )

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
        # Отправляем сообщение на ADMIN_ID
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

    safe_original_text = html.escape(query.message.text or "")

    if action == "admin_confirm":
        tariff_type = parts[2]
        # Вызываем функцию начисления 30 дней!
        await add_subscription_days(target_chat_id, tariff_type, 30)

        new_text = f"✅ <b>ОПЛАТА ПОДТВЕРЖДЕНА (30 дней)</b>\n\n<pre>{safe_original_text}</pre>"
        await query.edit_message_text(new_text, parse_mode='HTML', reply_markup=None)

        # Уведомляем клиента, что бот заработал
        try:
            await context.bot.send_message(chat_id=target_chat_id, text="🎉 <b>Оплата подтверждена!</b>\nВам начислено 30 дней доступа к ИИ. Приятного общения!", parse_mode='HTML')
        except Exception:
            pass

    elif action == "admin_reject":
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
    holidays_mode = await database.get_holidays_mode(chat_key)

    btn1 = "🟢 Авто-переключение ИИ: ВКЛ" if auto_fallback else "🔴 Авто-переключение ИИ: ВЫКЛ"
    btn2 = "🟢 Контекст при смене: СОХРАНЯТЬ" if keep_context else "🔴 Контекст при смене: УДАЛЯТЬ"
    btn3 = "🟢 Отвечать на всё: ВКЛ" if respond_all else "🔴 Отвечать на всё: ВЫКЛ"
    btn4 = "🟢 Тихие ответы: ВКЛ" if silent_responses else "🔴 Тихие ответы: ВЫКЛ"
    btn5 = "🟢 Тех. детали сбоев: ВКЛ" if show_tech_details else "🔴 Тех. детали сбоев: СКРЫТЫ"
    if holidays_mode == 1:
        btn6 = "🟡 Праздники: ТОЛЬКО СВОИ (ДР)"
    elif holidays_mode == 2:
        btn6 = "🟢 Праздники: ВСЕ (КАЖДЫЙ ДЕНЬ)"
    else:
        btn6 = "🔴 Праздники и ДР: ВЫКЛ"

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn1, callback_data="setting:auto_fallback")],
        [InlineKeyboardButton(btn2, callback_data="setting:keep_context")],
        [InlineKeyboardButton(btn3, callback_data="setting:respond_all")],
        [InlineKeyboardButton(btn4, callback_data="setting:silent_responses")],
        [InlineKeyboardButton(btn5, callback_data="setting:show_tech_details")],
        [InlineKeyboardButton(btn6, callback_data="setting:holidays")]
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
    elif setting_type == "holidays":
        await database.toggle_holidays_enabled(chat_key)

    auto_fallback = await database.get_auto_fallback(chat_key)
    keep_context = await database.get_keep_context(chat_key)
    respond_all = await database.get_respond_all(chat_key)
    silent_responses = await database.get_silent_responses(chat_key)
    show_tech_details = await database.get_show_tech_details(chat_key)
    holidays_mode = await database.get_holidays_mode(chat_key)

    btn1 = "🟢 Авто-переключение ИИ: ВКЛ" if auto_fallback else "🔴 Авто-переключение ИИ: ВЫКЛ"
    btn2 = "🟢 Контекст при смене: СОХРАНЯТЬ" if keep_context else "🔴 Контекст при смене: УДАЛЯТЬ"
    btn3 = "🟢 Отвечать на всё: ВКЛ" if respond_all else "🔴 Отвечать на всё: ВЫКЛ"
    btn4 = "🟢 Тихие ответы: ВКЛ" if silent_responses else "🔴 Тихие ответы: ВЫКЛ"
    btn5 = "🟢 Тех. детали сбоев: ВКЛ" if show_tech_details else "🔴 Тех. детали сбоев: СКРЫТЫ"
    if holidays_mode == 1:
        btn6 = "🟡 Праздники: ТОЛЬКО СВОИ (ДР)"
    elif holidays_mode == 2:
        btn6 = "🟢 Праздники: ВСЕ (КАЖДЫЙ ДЕНЬ)"
    else:
        btn6 = "🔴 Праздники и ДР: ВЫКЛ"

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn1, callback_data="setting:auto_fallback")],
        [InlineKeyboardButton(btn2, callback_data="setting:keep_context")],
        [InlineKeyboardButton(btn3, callback_data="setting:respond_all")],
        [InlineKeyboardButton(btn4, callback_data="setting:silent_responses")],
        [InlineKeyboardButton(btn5, callback_data="setting:show_tech_details")],
        [InlineKeyboardButton(btn6, callback_data="setting:holidays")]
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

    buffer_key = (history_key, user_id)
    # Группировка (буферизация) быстрых сообщений
    if buffer_key not in config.user_buffers:
        config.user_buffers[buffer_key] = []

    config.user_buffers[buffer_key].append(message)
    current_len = len(config.user_buffers[buffer_key])

    # Ждем 1.5 секунды, чтобы собрать все сообщения из серии (пересылка + текст или фотоальбом)
    await asyncio.sleep(1.5)

    # Если список вырос за время ожидания, значит текущий поток не последний, уступаем выполнение
    if len(config.user_buffers.get(buffer_key, [])) != current_len:
        return

    # Забираем все накопленные сообщения
    messages = config.user_buffers.pop(buffer_key, [])

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
        part_text = rich_utils.extract_any_message_text(msg)
        if part_text:
            user_text_only += part_text + "\n\n"

        if msg.photo or msg.video or msg.video_note or msg.animation or msg.document or msg.voice or msg.audio:
            media_msg = msg

    # Отделяем чистый текст юзера от будущего контекста с цитатами
    user_text_only = user_text_only.strip()
    text = user_text_only

    # === МАГИЯ РЕПЛАЕВ И ПЕРЕСЫЛОК (С поддержкой RichMessage) ===
    if trigger_msg.reply_to_message:
        replied_text = rich_utils.extract_any_message_text(trigger_msg.reply_to_message)
        if replied_text:
            if is_reply_to_bot:
                prefix = "[В ответ на твое сообщение]"
            else:
                prefix = f"[В ответ на сообщение от {replied_human_name}]"

            if text:
                text = f"{text}\n\n{prefix}:\n«{replied_text}»"
            else:
                text = f"{prefix}:\n«{replied_text}»"

    # Поиск медиа в реплаях
    if not (
        media_msg.photo or media_msg.video or media_msg.video_note or
        media_msg.animation or media_msg.document or media_msg.voice or media_msg.audio
    ) and trigger_msg.reply_to_message:
        media_msg = trigger_msg.reply_to_message

    # Решение принимается до скачивания медиа, списания запроса и любых сообщений ожидания.
    prepared_reply = None
    is_mentioned = bool(context.bot.username and re.search(
        rf"(?<!\w)@{re.escape(context.bot.username)}(?!\w)", user_text_only, re.IGNORECASE))

    # === БЫСТРЫЙ ТРИГГЕР ПО СЛОВУ "ПРАЗДНИКИ" ===
    clean_word = re.sub(r'[^\wа-яА-ЯёЁ]', '', user_text_only).strip().lower()
    if clean_word in ("праздники", "праздник"):
        is_silent = await database.get_silent_responses(history_key)
        tracker = rich_utils.RichStatusTracker(context.bot, chat_id, chat_type, trigger_msg.message_id)
        await tracker.step("🎉 Смотрю календарь праздников и особые даты чата...", force=True)
        try:
            holiday_reply = await holidays.build_today_holidays_message(history_key)
            await tracker.cleanup()
            await rich_utils.send_rich_safe(
                context.bot, chat_id, holiday_reply,
                reply_to_message_id=trigger_msg.message_id,
                disable_notification=is_silent
            )
        except Exception as e:
            logger.error(f"Ошибка вывода праздников: {e}")
            await tracker.cleanup()
            await trigger_msg.reply_text("⚠️ Не удалось загрузить календарь праздников.")
        return

    if chat_type != "private" and not (is_mentioned or is_reply_to_bot):
        silence_until = config.silenced_chats.get(chat_id)
        if silence_until and datetime.now(timezone.utc) < silence_until:
            return
        config.silenced_chats.pop(chat_id, None)
        if not await database.get_respond_all(history_key):
            text_without_links = re.sub(r'https?://\S+', '', user_text_only).strip()
            if is_reply_to_other or "?" not in text_without_links:
                return
             # Без запроса к боту не анализируем вложения и не гадаем по их подписи.
            if (
                media_msg.photo or media_msg.video or media_msg.video_note or
                media_msg.animation or media_msg.document or media_msg.voice or media_msg.audio
            ):
                return
            last_time = config.last_request_time.get(history_key)
            if last_time and (datetime.now(timezone.utc) - last_time).total_seconds() < config.RATE_LIMIT_SECONDS_PER_USER:
                return
            user_lock = await ensure_user_lock(history_key)
            if user_lock.locked():
                return
            async with user_lock:
                prepared_reply = await ai_core.prepare_group_reply(user_text_only, history_key, user_name)
            if prepared_reply is None:
                return

    is_silent = await database.get_silent_responses(history_key)

    # ПРОВЕРКА ДОСТУПА (БИЛЛИНГ)
    has_access = await consume_request_and_check(history_key, chat_type)
    if not has_access:
        tariff_msg = (
            "⭐️ <b>Доступ ограничен</b>\n\n"
            "Ваши пробные запросы закончились, или срок подписки истек. "
            "Чтобы бот снова начал отвечать, необходимо оформить подписку. Введите /pay для просмотра тарифов."
        )
        if is_reply_to_bot or chat_type == "private" or (
                context.bot.username and f"@{context.bot.username.lower()}" in user_text_only.lower()):
            await update.message.reply_text(tariff_msg, parse_mode='HTML')
        return

    now = datetime.now(timezone.utc)
    last_time = config.last_request_time.get(history_key)
    if last_time and (now - last_time).total_seconds() < config.RATE_LIMIT_SECONDS_PER_USER:
        return
    config.last_request_time[history_key] = now

    try:
        is_short = prepared_reply is not None
        if is_short:
            prompt_text = text if chat_type == "private" else f"{user_name} (ID: {user_id}) пишет: {text}"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔇 Тише", callback_data=f"silence:{chat_id}:{trigger_msg.message_id}"),
                InlineKeyboardButton("📖 Подробнее", callback_data=f"more:{trigger_msg.message_id}:{user_id}")
            ]])
            await rich_utils.send_rich_safe(
                context.bot, chat_id, prepared_reply,
                reply_to_message_id=trigger_msg.message_id,
                reply_markup=keyboard,
                disable_notification=is_silent
            )
            await database.add_message_to_db(history_key, "user", prompt_text)
            await database.add_message_to_db(history_key, "assistant", prepared_reply)
            return

        user_lock = await ensure_user_lock(history_key)
        if user_lock.locked():
            await update.message.reply_text("⏳ Я еще думаю над прошлым вопросом, подождите немного...")
            return

        cancel_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛑 Отмена", callback_data=f"cancel:{history_key}")]]
        )
        tracker = rich_utils.RichStatusTracker(
            context.bot, chat_id, chat_type,
            reply_to_message_id=trigger_msg.message_id,
            cancel_kb=cancel_kb
        )

        video_grid_bytes = None
        image_bytes = None

        async with user_lock:
            stop_event = asyncio.Event()
            typing_task = asyncio.create_task(typing_sender(chat_id, context, stop_event))

            try:
                # 1. ГОЛОСОВЫЕ И АУДИО (С поддержкой сжатия больших аудиофайлов через ffmpeg)
                if media_msg.voice or media_msg.audio:
                    audio_obj = media_msg.voice if media_msg.voice else media_msg.audio
                    size_mb = (audio_obj.file_size or 0) / (1024 * 1024)

                    if size_mb > 20:
                        await tracker.step(
                            f"📦 Аудиофайл большой ({size_mb:.1f} МБ) — скачиваю и сжимаю, придётся подождать чуть дольше...",
                            force=True
                        )
                    else:
                        await tracker.step("🎧 Слушаю и расшифровываю голосовое сообщение...", force=True)

                    try:
                        audio_bytes = await download_telegram_media_bytes(audio_obj, progress_callback=tracker.step)
                        mime_type = getattr(audio_obj, 'mime_type', None) or 'audio/ogg'

                        # Если аудиофайл большой (> 15 МБ) — сжимаем его локально через media_processor в легкий моно-MP3
                        if len(audio_bytes) > 15 * 1024 * 1024:
                            await tracker.step("🗜 Сжимаю большой аудиофайл через FFmpeg для нейросети...", force=True)
                            import tempfile, os, media_processor
                            with tempfile.TemporaryDirectory() as tmpdir:
                                raw_p = os.path.join(tmpdir, "big_audio")
                                with open(raw_p, "wb") as f:
                                    f.write(audio_bytes)
                                comp_p = await media_processor.compress_audio_for_ai(raw_p)
                                if comp_p and os.path.exists(comp_p):
                                    with open(comp_p, "rb") as f:
                                        audio_bytes = f.read()
                                    mime_type = "audio/mpeg"

                        transcript = await ai_core.transcribe_audio(audio_bytes, mime_type=mime_type)
                        text = (
                                    text + f"\n\n[Пользователь отправил голосовое сообщение. Расшифровка]:\n{transcript}").strip()
                    except Exception as e:
                        if "File is too big" in str(e):
                            await tracker.cleanup()
                            await update.message.reply_text(
                                f"⚠️ Аудиофайл весит <b>{size_mb:.1f} МБ</b>. Стандартный сервер Telegram не отдаёт ботам файлы тяжелее 20 МБ без подключенного Local Bot API Server.",
                                parse_mode="HTML"
                            )
                            return
                        logger.error(f"Ошибка каскада распознавания аудио: {e}")
                        await tracker.cleanup()
                        await update.message.reply_text(
                            "⚠️ Ошибка распознавания голоса (все аудио-сервисы временно недоступны).")
                        return

                # 2. ВИДЕОФАЙЛЫ, КРУЖКИ И GIF (С поддержкой больших файлов и фолбэком на превью)
                elif media_msg.video or media_msg.video_note or media_msg.animation:
                    vid_obj = media_msg.video or media_msg.video_note or media_msg.animation
                    is_gif = bool(media_msg.animation)
                    size_mb = (vid_obj.file_size or 0) / (1024 * 1024)

                    if size_mb > 20:
                        await tracker.step(
                            f"📦 Видео большое ({size_mb:.1f} МБ) — пробую скачать и сжать локально, подождите...",
                            force=True
                        )
                    else:
                        await tracker.step(
                            "🎬 Анализирую кадры и текст на GIF..." if is_gif else "🎬 Слушаю звук и смотрю субтитры на видео...",
                            force=True
                        )

                    try:
                        video_bytes = await download_telegram_media_bytes(vid_obj, progress_callback=tracker.step)
                        transcript, video_grid_bytes = await web_parser.extract_video_file_context(video_bytes,
                                                                                                   is_gif=is_gif)
                        media_label = "GIF-анимацию" if is_gif else "видеофайл"
                        if transcript:
                            text = (
                                        text + f"\n\n[Пользователь отправил {media_label}. Распознанная речь из видео]:\n{transcript}").strip()
                        else:
                            text = (
                                        text + f"\n\n[Пользователь отправил {media_label} без устной речи (или только с музыкой/текстом на экране). "
                                               f"Проанализируй прикрепленную раскадровку (4 кадра из видео), прочитай вшитые субтитры/надписи и ответь по сути.]").strip()
                    except Exception as e:
                        # Если стандартный Bot API заблокировал скачивание (> 20 МБ), достаем встроенный кадр-превью (thumbnail)!
                        thumb = getattr(vid_obj, "thumbnail", None)
                        if "File is too big" in str(e) and thumb:
                            await tracker.step(
                                f"⚠️ Видео ({size_mb:.1f} МБ) превышает лимит облака ТГ 20 МБ — извлекаю кадр превью...",
                                force=True
                            )
                            t_file = await thumb.get_file()
                            video_grid_bytes = bytes(await t_file.download_as_bytearray())
                            text = (
                                        text + f"\n\n[Системная пометка: Пользователь отправил большое видео ({size_mb:.1f} МБ). "
                                               f"Из-за лимита стандартного Telegram API в 20 МБ звук скачать не удалось, но прикреплен кадр превью этого видео. Проанализируй его и подпись пользователя.]").strip()
                        else:
                            logger.error(f"Ошибка анализа видео/GIF: {e}")
                            await tracker.cleanup()
                            await update.message.reply_text(
                                "⚠️ Не удалось обработать видеофайл (возможно, превышен лимит 20 МБ).")
                            return

                # 3. ПРИКРЕПЛЕННЫЕ ДОКУМЕНТЫ (Видео-документы, GIF, PDF, DOCX, код)
                elif media_msg.document:
                    doc = media_msg.document
                    size_mb = (doc.file_size or 0) / (1024 * 1024)
                    mime = (doc.mime_type or "").lower()
                    fname_lower = (doc.file_name or "").lower()
                    is_video_doc = mime.startswith("video/") or fname_lower.endswith(
                        (".mp4", ".mov", ".mkv", ".webm", ".avi"))
                    is_gif_doc = mime == "image/gif" or fname_lower.endswith(".gif")

                    if is_video_doc or is_gif_doc:
                        if size_mb > 20:
                            await tracker.step(f"📦 Медиа-документ большой ({size_mb:.1f} МБ) — скачиваю и сжимаю...",
                                               force=True)
                        else:
                            await tracker.step(f"🎬 Анализирую медиа-документ {doc.file_name}...", force=True)
                        try:
                            file_bytes = await download_telegram_media_bytes(doc, progress_callback=tracker.step)
                            transcript, video_grid_bytes = await web_parser.extract_video_file_context(
                                file_bytes, is_gif=is_gif_doc
                            )
                            if transcript:
                                text = (
                                            text + f"\n\n[Пользователь отправил видео-документ {doc.file_name}. Распознанная речь]:\n{transcript}").strip()
                            else:
                                text = (
                                            text + f"\n\n[Пользователь отправил медиа-документ {doc.file_name} без устной речи. "
                                                   f"Проанализируй прикрепленную раскадровку (4 кадра), прочитай субтитры/надписи и ответь по сути.]").strip()
                        except Exception as e:
                            thumb = getattr(doc, "thumbnail", None)
                            if "File is too big" in str(e) and thumb:
                                t_file = await thumb.get_file()
                                video_grid_bytes = bytes(await t_file.download_as_bytearray())
                                text = (
                                            text + f"\n\n[Пользователь отправил большой видео-документ {doc.file_name} ({size_mb:.1f} МБ). Прикреплен кадр превью.]").strip()
                            else:
                                logger.error(f"Ошибка анализа видео-документа: {e}")
                                await tracker.cleanup()
                                await update.message.reply_text(
                                    "⚠️ Ошибка при обработке видео-документа (лимит Telegram Bot API — 20 МБ).")
                                return
                    else:
                        if size_mb > 20:
                            await tracker.step(
                                f"📦 Документ {doc.file_name} большой ({size_mb:.1f} МБ) — пробую загрузить...",
                                force=True)
                        else:
                            await tracker.step(f"📄 Читаю файл {doc.file_name}...", force=True)
                        try:
                            file = await doc.get_file()
                            file_bytes = bytes(await file.download_as_bytearray())

                            if mime == 'application/pdf' or fname_lower.endswith('.pdf'):
                                pdf = pymupdf.open(stream=file_bytes, filetype="pdf")
                                doc_text = "".join([page.get_text() for page in pdf])
                                text += f"\n\n[Содержимое прикрепленного PDF файла {doc.file_name}]:\n{doc_text[:6000]}..."
                            elif fname_lower.endswith('.docx'):
                                docx_file = io.BytesIO(file_bytes)
                                document = Document(docx_file)
                                doc_text = "\n".join([para.text for para in document.paragraphs])
                                text += f"\n\n[Содержимое прикрепленного Word документа {doc.file_name}]:\n{doc_text[:6000]}..."
                            else:
                                try:
                                    doc_text = file_bytes.decode('utf-8')
                                    text += f"\n\n[Содержимое прикрепленного файла {doc.file_name}]:\n{doc_text[:6000]}..."
                                except UnicodeDecodeError:
                                    text += f"\n\n[Системное сообщение: Пользователь прикрепил файл {doc.file_name}, но это неизвестный бинарный формат. Бот не смог извлечь из него текст. Сообщи об этом пользователю.]"
                        except Exception as e:
                            logger.error(f"Ошибка чтения файла: {e}")
                            await tracker.cleanup()
                            await update.message.reply_text(
                                "⚠️ Ошибка при чтении файла (возможно, он превышает лимит Telegram 20 МБ).")
                            return

                # 4. ФОТО
                if media_msg.photo:
                    await tracker.step("🖼 Загружаю и анализирую изображение...", force=True)
                    photo_file = await media_msg.photo[-1].get_file()
                    image_bytes = bytes(await photo_file.download_as_bytearray())
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

                if not text and not image_bytes and not video_grid_bytes:
                    await tracker.cleanup()
                    return

                prompt_text = text if chat_type == "private" else f"{user_name} (ID: {user_id}) пишет: {text}"

                # 5. ПАРСИНГ ССЫЛОК (WEB / YOUTUBE / INSTAGRAM / TIKTOK)
                if web_parser.extract_urls(prompt_text):
                    await tracker.step("🌐 Загружаю данные и субтитры по ссылке...", force=True)
                enriched_prompt, parsed_image_bytes = await web_parser.process_message_for_urls(prompt_text)
                final_image_bytes = image_bytes or video_grid_bytes or parsed_image_bytes

                # 6. ЗАПРОС К LLM С ОБНОВЛЕНИЕМ <tg-thinking>
                gen_task = asyncio.create_task(
                    ai_core.ask_llm(
                        history_key, enriched_prompt, chat_type, user_name,
                        final_image_bytes, status_callback=tracker.step
                    )
                )
                config.active_tasks[history_key] = gen_task

                try:
                    reply = await gen_task
                except asyncio.CancelledError:
                    return
                finally:
                    config.active_tasks.pop(history_key, None)

            finally:
                stop_event.set()
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
                await tracker.cleanup()

        # ПАРСИНГ РЕАКЦИИ ОТ ИИ
        match = re.search(r'\[REACTION:\s*(.*?)\]', reply or "")
        if match:
            reaction_emoji = match.group(1).strip()
            reply = re.sub(r'\[REACTION:\s*.*?\]\s*', '', reply).strip()

            if reaction_emoji in ai_core.ALLOWED_REACTIONS:
                try:
                    await trigger_msg.set_reaction(reaction=[ReactionTypeEmoji(reaction_emoji)])
                except Exception as e:
                    logger.warning(f"Не удалось поставить реакцию {reaction_emoji}: {e}")
            else:
                logger.debug(f"ИИ попытался поставить неподдерживаемую реакцию: {reaction_emoji} (проигнорировано)")

        if not reply:
            return

        await rich_utils.send_rich_safe(
            context.bot,
            chat_id=chat_id,
            text=reply,
            reply_to_message_id=trigger_msg.message_id,
            disable_notification=is_silent,
        )

    except Exception:
        logger.exception("Ошибка в handle_message")


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, target_key = query.data.split(":")
        target_key = int(target_key)
        if target_key in config.active_tasks:
            config.active_tasks[target_key].cancel()
            await query.edit_message_text("🛑 Генерация отменена пользователем.")
        else:
            await query.edit_message_text("🛑 Запрос уже завершен или отменен.")
    except Exception:
        logger.exception("Ошибка в кнопке Отмена")


async def handle_generation_stopped(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия нативной кнопки '⏹ Остановить' в sendRichMessageDraft (Bot API 10.3)"""
    msg = update.effective_message
    if not msg:
        return
    history_key = msg.from_user.id if msg.chat.type == "private" else msg.chat_id
    if history_key in config.active_tasks:
        config.active_tasks[history_key].cancel()
        await msg.reply_text("🛑 Генерация ответа остановлена.")


async def expand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, msg_id_str, user_id_str = query.data.split(':')
        original_msg_id = int(msg_id_str)
        chat_id = query.message.chat_id
        history_key = int(user_id_str) if query.message.chat.type == 'private' else chat_id

        # Извлекаем текст короткого ответа (работает и для RichMessage, и для обычного текста)
        bot_short_answer = rich_utils.extract_any_message_text(query.message)
        if not bot_short_answer:
            hist = await database.get_history_from_db(history_key, limit=2)
            for m in reversed(hist):
                if m["role"] == "assistant":
                    bot_short_answer = m["content"]
                    break

        await query.edit_message_reply_markup(reply_markup=None)
        tracker = rich_utils.RichStatusTracker(
            context.bot, chat_id, query.message.chat.type, reply_to_message_id=original_msg_id
        )
        await tracker.step("📖 Собираю развернутый ответ...", force=True)

        prompt = (
            "Пожалуйста, распиши вот этот свой краткий ответ максимально подробно "
            f"и развернуто. Дай больше деталей:\n\n«{bot_short_answer}»"
        )

        reply = await ai_core.ask_llm(
            history_key, prompt, query.message.chat.type, status_callback=tracker.step
        )
        is_silent = await database.get_silent_responses(history_key)
        await tracker.cleanup()

        await rich_utils.send_rich_safe(
            context.bot,
            chat_id=chat_id,
            text=reply,
            reply_to_message_id=original_msg_id,
            disable_notification=is_silent,
        )

    except Exception:
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
                        new_row.append(InlineKeyboardButton(
                            new_text, callback_data=btn.callback_data.replace("silence:", "unsilence:")
                        ))
                    else:
                        new_row.append(btn)
                new_inline_keyboard.append(new_row)

        elif action == "unsilence":
            # 1. Досрочно снимаем тишину
            config.silenced_chats.pop(chat_id, None)
            # 2. Возвращаем исходную кнопку
            for row in existing_markup.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data and btn.callback_data.startswith("unsilence:"):
                        new_row.append(InlineKeyboardButton(
                            "🔇 Тише", callback_data=btn.callback_data.replace("unsilence:", "silence:")
                        ))
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
        "Я умею поддерживать диалог, запоминать контекст, <b>видеть картинки и видео</b>, слушать голосовые и переводить видео по ссылкам.\n\n"
        "В группах я не влезаю в каждую беседу. Я отвечаю только если меня тегнуть, ответить на мое сообщение или задать вопрос со знаком «?», на который у меня есть полезный ответ. Если данных не хватает, я молчу. А еще я умею реагировать эмодзи!\n\n"
        "<b>Команды:</b>\n"
        "🔹 /model — выбрать нейросеть (Gemini, Groq, OpenRouter)\n"
        "🔹 /events — дни рождения и особые даты чата\n"
        "🔹 /reset — начать диалог с чистого листа (сбросить память)\n"
        "🔹 /pay — оформить или продлить подписку\n"
        "🔹 /help — показать эту справку"
    )

    # 2. Блок настроек (видят пользователи в ЛС и админы в группах)
    if can_change_settings:
        help_text += (
            "\n\n⚙️ <b>Настройки чата:</b>\n"
            "🔹 /setting — управление режимами ИИ, памятью и праздниками"
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
        "Я — твой умный ИИ-помощник. Я умею поддерживать диалог, запоминать контекст нашей беседы и <b>видеть картинки и видео</b> (просто скинь мне фото, видео или мем!).\n\n"
        "Напиши свой первый вопрос или отправь /help, чтобы посмотреть список команд."
    )
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
    chat_id = update.effective_chat.id
    # 1. Достаем последние 6 сообщений из базы для контекста
    history = await database.get_history_from_db(chat_id, limit=6)
    history_text = "\n".join([f"{msg['role']}: {msg['content'][:100]}" for msg in history])
    if not history_text:
        history_text = "В чате пока тихо."

    is_silent = await database.get_silent_responses(chat_id)

    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            continue

        prompt = (
            f"[Системное событие: в группу вступил новый участник {member.full_name}].\n"
            "Напиши для него короткое, креативное и теплое приветствие на 1-2 предложения (можно со смайликами в тексте). "
            "Не используй тег [REACTION] в этом ответе.\n"
            f"Для контекста — вот о чем общались в чате прямо перед его приходом:\n<chat_history>\n{history_text}\n</chat_history>\n"
            "Если в истории обсуждается какая-то тема, забавно и кратко упомяни её в приветствии, чтобы сразу ввести человека в курс дела. "
            "Не пиши 'Привет, я ИИ' и не задавай скучных вопросов."
        )

        wait_msg = await update.message.reply_text("⏳ Генерирую приветствие...", disable_notification=True)
        # save_to_history=False не дает техническому промпту записаться в память как реплика юзера
        reply = await ai_core.ask_llm(chat_id, prompt, chat_type="group", save_to_history=False)

        try:
            await wait_msg.delete()
        except Exception:
            pass

        if not reply:
            continue

        # 3. Если модель всё равно добавила тег [REACTION], аккуратно вырезаем его
        match = re.search(r'\[REACTION:\s*(.*?)\]', reply)
        if match:
            reaction_emoji = match.group(1).strip()
            reply = re.sub(r'\[REACTION:\s*.*?\]\s*', '', reply).strip()
            # Ставим реакцию ТОЛЬКО если эмодзи реально существует в белом списке Telegram
            if reaction_emoji in ai_core.ALLOWED_REACTIONS:
                try:
                    await update.message.set_reaction(reaction=[ReactionTypeEmoji(reaction_emoji)])
                except Exception as e:
                    logger.warning(f"Не удалось поставить реакцию {reaction_emoji}: {e}")

        if reply:
            await rich_utils.send_rich_safe(
                context.bot, chat_id, reply,
                reply_to_message_id=update.message.message_id,
                disable_notification=is_silent
            )


# ==========================================
# УПРАВЛЕНИЕ ПРАЗДНИКАМИ И ДР (/events)
# ==========================================

async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_key = update.effective_user.id if update.effective_chat.type == 'private' else update.effective_chat.id

    # --- БЫСТРЫЙ ТЕСТ РАССЫЛОК ДЛЯ АДМИНА ---
    if context.args and len(context.args) == 1 and context.args[0].lower() in ("test_mon", "test_fri", "test_day", "test_bday"):
        if not await check_admin_rights(update, context):
            return
        import tasks
        cmd = context.args[0].lower()
        now = datetime.now(config.BOT_TIMEZONE)
        is_silent = await database.get_silent_responses(chat_key)
        wait_msg = await update.message.reply_text(
            f"🧪 Тестирую задачу <code>{cmd}</code>...", parse_mode='HTML', disable_notification=True
        )
        try:
            if cmd == "test_bday":
                sent = await tasks.check_and_send_upcoming_bday_alerts(context, target_chat_id=chat_key)
                res = (
                    f"✅ Проверка ДР (на 1–3 дня вперед) завершена!\n"
                    f"Отправлено уведомлений админам в ЛС: **{sent}** шт."
                )
            elif cmd == "test_mon":
                res = await holidays.build_monday_digest(chat_key, start_dt=now)
                if not res:
                    res = "ℹ️ На этой неделе нет запланированных ДР, особых событий чата или праздничных выходных среди недели, поэтому в понедельник бот промолчит."
            elif cmd == "test_fri":
                res = await holidays.build_friday_preview(chat_key, friday_dt=now)
                if not res:
                    res = "ℹ️ На следующей неделе (Пн–Вс от текущей даты) в этом чате нет своих ДР или особых дат, поэтому пятничное сообщение не отправляется."
            else:
                res = await holidays.build_today_holidays_message(chat_key, dt=now)
                # При тесте дня заодно проверяем отправку ДР в личку админам!
                await tasks.check_and_send_upcoming_bday_alerts(context, target_chat_id=chat_key)
            await wait_msg.delete()
            await rich_utils.send_rich_safe(
                context.bot, update.effective_chat.id, res,
                reply_to_message_id=update.message.message_id,
                disable_notification=is_silent
            )
        except Exception as e:
            await wait_msg.edit_text(f"⚠️ Ошибка теста: {e}")
        return

    # Если переданы аргументы: /events 15.10 др День рождения Ползователя
    if context.args and len(context.args) >= 2:
        if not await check_admin_rights(update, context):
            await update.message.reply_text("❌ Добавлять даты в группе могут только администраторы.")
            return

        date_arg = context.args[0]
        match = re.match(r'^(\d{1,2})[./-](\d{1,2})$', date_arg)
        if not match:
            await update.message.reply_text("⚠️ Неверный формат даты. Используйте: <code>/events ДД.ММ Название</code>", parse_mode='HTML')
            return

        day, month = int(match.group(1)), int(match.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            await update.message.reply_text("⚠️ Проверьте правильность дня и месяца.")
            return

        # Проверяем, указан ли тип "др"
        second_word = context.args[1].lower()
        if second_word in ("др", "bd", "birthday") and len(context.args) >= 3:
            category = "birthday"
            title = " ".join(context.args[2:])
        else:
            category = "holiday"
            title = " ".join(context.args[1:])

        await database.add_custom_event(chat_key, day, month, title, category)
        icon = "🎂" if category == "birthday" else "⭐"
        await update.message.reply_text(f"✅ Добавлено: {icon} <b>{day:02d}.{month:02d}</b> — {html.escape(title)}", parse_mode='HTML')
        return

    # Если вызвано просто /events — выводим список и кнопки удаления
    holidays_mode = await database.get_holidays_mode(chat_key)
    mode_status = {
        0: "🔴 <b>Уведомления о праздниках сейчас ВЫКЛЮЧЕНЫ!</b> (Включите их в /setting)",
        1: "🟡 Режим уведомлений: <b>Только свои даты и ДР</b>",
        2: "🟢 Режим уведомлений: <b>Все праздники ежедневно</b>"
    }.get(holidays_mode, "")

    events = await database.get_custom_events(chat_key)
    text = (
        "📅 <b>Особые даты и Дни рождения этого чата:</b>\n"
        f"{mode_status}\n\n"
        "Чтобы добавить дату, напишите:\n"
        "• <code>/events 15.10 др Имя</code> — добавить День рождения 🎂\n"
        "• <code>/events 25.12 Новогодний корпоратив</code> — особый день ⭐\n\n"
    )

    kb = []
    if not events:
        text += "<i>Список пока пуст.</i>"
    else:
        for ev in events:
            icon = "🎂" if ev["category"] == "birthday" else "⭐"
            line = f"{icon} <b>{ev['day']:02d}.{ev['month']:02d}</b> — {html.escape(ev['title'])}"
            text += line + "\n"
            kb.append([InlineKeyboardButton(
                f"🗑 Удалить: {ev['day']:02d}.{ev['month']:02d} {ev['title'][:20]}",
                callback_data=f"del_event:{ev['id']}"
            )])

    await update.message.reply_text(
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(kb) if kb else None
    )


async def delete_event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_admin_rights(update, context):
        await query.answer("❌ Удалять даты могут только админы!", show_alert=True)
        return

    await query.answer("Удалено!")
    chat_key = query.from_user.id if query.message.chat.type == 'private' else query.message.chat.id
    event_id = int(query.data.split(":")[1])

    await database.delete_custom_event(event_id, chat_key)
    events = await database.get_custom_events(chat_key)

    kb = []
    for ev in events:
        kb.append([InlineKeyboardButton(
            f"🗑 Удалить: {ev['day']:02d}.{ev['month']:02d} {ev['title'][:20]}",
            callback_data=f"del_event:{ev['id']}"
        )])

    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb) if kb else None)


async def mute_bday_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Позволяет админу отключить или включить личные уведомления о ДР из конкретной группы"""
    user_id = update.effective_user.id

    # Если команда вызвана прямо в группе без аргументов — берем ID этой группы
    if not context.args and update.effective_chat.type != "private":
        target_chat_id = str(update.effective_chat.id)
    elif context.args:
        target_chat_id = context.args[0].strip()
    else:
        await update.message.reply_text("ℹ️ Укажите ID группы: <code>/mute_bday -100123456789</code>", parse_mode='HTML')
        return

    currently_muted = await database.is_admin_bday_muted(target_chat_id, user_id)
    new_state = not currently_muted
    await database.set_admin_bday_mute(target_chat_id, user_id, mute=new_state)

    if new_state:
        await update.message.reply_text(
            f"🔕 Вы отключили личные напоминания о ДР из группы <code>{html.escape(target_chat_id)}</code>.\n"
            f"Чтобы включить обратно, введите <code>/mute_bday {html.escape(target_chat_id)}</code> ещё раз.",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            f"🔔 Личные напоминания о ДР из группы <code>{html.escape(target_chat_id)}</code> снова <b>включены</b>!",
            parse_mode='HTML'
        )


async def bday_mute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Не присылать ДР из этой группы' прямо в ЛС у админа"""
    query = update.callback_query
    await query.answer()

    _, action, target_chat_id = query.data.split(":", 2)
    user_id = query.from_user.id

    if action == "off":
        await database.set_admin_bday_mute(target_chat_id, user_id, mute=True)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔔 Включить обратно", callback_data=f"bday_mute:on:{target_chat_id}")
        ]])
        await query.edit_message_reply_markup(reply_markup=kb)
        await query.message.reply_text("🔕 Уведомления о ДР из этой группы отключены.")
    else:
        await database.set_admin_bday_mute(target_chat_id, user_id, mute=False)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔕 Не присылать ДР из этой группы", callback_data=f"bday_mute:off:{target_chat_id}")
        ]])
        await query.edit_message_reply_markup(reply_markup=kb)
        await query.message.reply_text("🔔 Уведомления о ДР из этой группы снова включены!")

async def sync_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительно обновляет списки моделей всех провайдеров (только для владельца)."""
    if update.effective_user.id != config.ADMIN_ID:
        return
    import tasks
    wait_msg = await update.message.reply_text("🔄 Синхронизирую списки моделей (Gemini, Groq, OpenRouter)...")
    gem_total, gem_menu = await tasks.sync_gemini_models()
    await tasks.sync_groq_models()
    await tasks.sync_openrouter_models()

    all_models = database.load_models_config()
    gem_names = [f"• {m['name']} (`{m['id']}`)" for m in all_models if m["provider"] == "gemini" and m.get("selectable")]
    text = (
        f"✅ **Синхронизация завершена!**\n\n"
        f"✨ **Gemini** (найдено {gem_total}, в меню {gem_menu}):\n" + "\n".join(gem_names)
    )
    await wait_msg.edit_text(text, parse_mode="Markdown")