import re
import time
import html
import logging
import aiohttp
from telegram import Bot, Message, InlineKeyboardMarkup
import config

logger = logging.getLogger(__name__)

RICH_MAX_LENGTH = 32000     # Лимит sendRichMessage — 32 768 символов
CLASSIC_MAX_LENGTH = 4096   # Лимит обычного sendMessage — 4 096 символов


def sanitize_gfm_markdown(text: str) -> str:
    r"""
    Очищает и форматирует текст для отправки в sendRichMessage (режим {"markdown": "..."}):
    1. Переносит блоки мыслей <think>...</think> в сворачиваемый спойлер <details>.
    2. Удаляет служебные теги <tg-thinking> (они разрешены только внутри черновика sendRichMessageDraft).
    3. Конвертирует случайно попавшие HTML-теги (<b>, <i>) в Markdown (**bold**, *italic*).
    4. Разбивает склеенные через ' • ' элементы и одиночные переносы строк, чтобы в GFM
       списки праздников и пунктов никогда не слипались в один сплошной абзац.
    5. Защищает одиночный знак валюты $ перед числами (\$10), чтобы не включался LaTeX.
    """
    if not text:
        return ""

    # 1. Превращаем закрытые блоки <think>...</think> (например, от DeepSeek/Qwen) в <details>
    def _think_to_details(match):
        thought = match.group(1).strip()
        if not thought:
            return ""
        return f"<details><summary>💭 Ход мыслей ИИ</summary>\n\n{thought}\n</details>\n\n"

    text = re.sub(r'<think>(.*?)</think>', _think_to_details, text, flags=re.DOTALL)
    text = text.replace('<think>', '').replace('</think>', '')
    text = re.sub(r'</?tg-thinking>', '', text)

    # 2. Защищаем блоки кода ```...``` и `...` от любых автозамен текста
    parts = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', text)
    for i in range(0, len(parts), 2):
        chunk = parts[i]

        # Заменяем случайные HTML-теги жирного/курсива на GFM Markdown
        chunk = re.sub(r'</?b>', '**', chunk)
        chunk = re.sub(r'</?strong>', '**', chunk)
        chunk = re.sub(r'</?i>', '*', chunk)
        chunk = re.sub(r'</?em>', '*', chunk)

        # Если нейросеть слепила пункты в одну строку через " • " — переносим каждый на новую строку списка
        chunk = re.sub(r'\s+•\s+', '\n- ', chunk)
        # Если строка начинается с буллита "• " — меняем на стандартный дефис списка "- "
        chunk = re.sub(r'^[ \t]*•[ \t]*', '- ', chunk, flags=re.MULTILINE)

        # В GFM одиночный перенос строки (\n) между двумя обычными строками склеивает их в один абзац.
        # Поэтому если строка не является частью таблицы (не начинается с '|') и не пустая,
        # превращаем одиночные переносы перед новыми пунктами/эмодзи в двойные (\n\n) или в список.
        lines = chunk.split('\n')
        formatted_lines = []
        for idx, line in enumerate(lines):
             stripped = line.strip()
             # Если это конец предыдущей обычной строки, а следующая начинается с эмодзи или жирного пункта,
             # но между ними нет пустой строки — добавляем пустую строку, чтобы GFM не слепил их в кучу
             if (
                 idx > 0
                 and stripped
                 and lines[idx - 1].strip()
                 and not stripped.startswith(('-', '*', '#', '>', '|', '<'))
                 and not lines[idx - 1].strip().startswith(('|', '<'))
                 and not re.match(r'^\d+\.', stripped)
             ):
                 formatted_lines.append("")
             formatted_lines.append(line)
        chunk = "\n".join(formatted_lines)

        # 3. Экранируем знак валюты $ перед цифрами ($10 -> \$10), чтобы парсер не считал это LaTeX-формулой
        chunk = re.sub(r'(?<![\\$])\$(\d)', r'\\$\1', chunk)
        parts[i] = chunk

    return "".join(parts).strip()


def gfm_to_fallback_html(text: str) -> str:
    """
    Конвертирует GFM Markdown и гибридные теги Rich HTML в классический безопасный Telegram HTML,
    если sendRichMessage вернул ошибку и нужен откат на обычный sendMessage.
    """
    if not text:
        return ""

    text = text.replace(r"\$", "$")

    # 1. Вырезаем блоки кода в плейсхолдеры, чтобы не испортить символы внутри них
    code_blocks = []

    def _save_fenced_code(m):
        lang = m.group(1).strip()
        code = html.escape(m.group(2))
        block = f'<pre><code class="language-{lang}">{code}</code></pre>' if lang else f'<pre>{code}</pre>'
        code_blocks.append(block)
        return f"@@CODEBLOCK_{len(code_blocks) - 1}@@"

    def _save_inline_code(m):
        code = html.escape(m.group(1))
        code_blocks.append(f"<code>{code}</code>")
        return f"@@CODEBLOCK_{len(code_blocks) - 1}@@"

    text = re.sub(r'```(\w*)\n?([\s\S]*?)```', _save_fenced_code, text)
    text = re.sub(r'`([^`\n]+)`', _save_inline_code, text)

    # 2. Превращаем Rich-теги в поддерживаемые стандартным Telegram HTML аналоги
    text = re.sub(
        r'<details[^>]*>\s*<summary>(.*?)</summary>([\s\S]*?)</details>',
        r'<b>▸ \1</b>\n<blockquote expandable>\2</blockquote>',
        text, flags=re.DOTALL
    )
    text = re.sub(r'<button[^>]*>(.*?)</button>', r'<code>\1</code>', text, flags=re.DOTALL)
    text = re.sub(r'</?(?:buttons|row|pullquote|footer|figure|caption|tg-map|tg-time|tg-thinking)[^>]*>', '', text)
    text = re.sub(r'<mark>(.*?)</mark>', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'<h[1-6][^>]*>(.*?)</h[1-6]>', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'<hr\s*/?>', '───────────────', text)

    # 3. Конвертируем базовый GFM Markdown в классический HTML
    text = re.sub(r'^(#{1,6})\s+(.+)$', r'<b>\2</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^---+$', '───────────────', text, flags=re.MULTILINE)
    text = re.sub(r'^[ \t]*[-*][ \t]+(.+)$', r'• \1', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'<a href="\2">\1</a>', text)

    # 4. Возвращаем блоки кода на место
    for idx, block in enumerate(code_blocks):
        text = text.replace(f"@@CODEBLOCK_{idx}@@", block)

    return text.strip()


def split_text(text: str, max_len: int = CLASSIC_MAX_LENGTH) -> list[str]:
    """Разбивает длинный текст на части по переносам строк, не превышая max_len."""
    parts = []
    while len(text) > max_len:
        idx = text[:max_len].rfind("\n")
        if idx == -1:
            idx = max_len
        parts.append(text[:idx])
        text = text[idx:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


def extract_any_message_text(msg: Message | None) -> str:
    """
    Извлекает текст как из обычного сообщения (msg.text / msg.caption),
    так и из RichMessage (где msg.text == None, а дерево блоков лежит в api_kwargs['rich_message']).
    Нужно для работы реплаев и кнопки '📖 Подробнее'.
    """
    if not msg:
        return ""
    if msg.text:
        return msg.text
    if msg.caption:
        return msg.caption

    rich_data = getattr(msg, "api_kwargs", {}).get("rich_message")
    if not rich_data:
        return ""

    collected = []

    def _walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("text", "markdown", "html", "title", "summary") and isinstance(v, str):
                    collected.append(v)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(rich_data)
    return "\n".join(collected).strip()


async def _call_bot_api(method: str, payload: dict) -> dict | bool | None:
    """Прямой HTTP-вызов методов Bot API 10.1–10.3 (sendRichMessage / sendRichMessageDraft)."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/{method}"
    clean_payload = {k: v for k, v in payload.items() if v is not None}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=clean_payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json(content_type=None)
            if resp.status == 200 and data.get("ok"):
                return data.get("result", True)
            raise RuntimeError(f"Telegram API {method} ({resp.status}): {data.get('description')}")


async def send_rich_safe(
    bot: Bot,
    chat_id: int | str,
    text: str,
    reply_to_message_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | dict | None = None,
    disable_notification: bool | None = None,
    prefer_html: bool = False,
):
    """
    Отправляет ответ через sendRichMessage (до 32 768 символов).
    При любой ошибке автоматически откатывается на классический bot.send_message.
    """
    if not text or not text.strip():
        return None

    markup_dict = reply_markup.to_dict() if isinstance(reply_markup, InlineKeyboardMarkup) else reply_markup
    reply_params = {"message_id": reply_to_message_id} if reply_to_message_id else None

    # 1. Основной путь: отправка через sendRichMessage
    try:
        rich_chunks = split_text(text, RICH_MAX_LENGTH)
        last_res = None
        for i, chunk in enumerate(rich_chunks):
            current_markup = markup_dict if i == len(rich_chunks) - 1 else None
            rich_body = {"html": chunk} if prefer_html else {"markdown": sanitize_gfm_markdown(chunk)}
            payload = {
                "chat_id": chat_id,
                "rich_message": rich_body,
                "reply_parameters": reply_params if i == 0 else None,
                "reply_markup": current_markup,
                "disable_notification": disable_notification,
            }
            last_res = await _call_bot_api("sendRichMessage", payload)
        return last_res
    except Exception as e:
        logger.debug(f"Откат с sendRichMessage на обычный sendMessage: {e}")

    # 2. Фолбэк: конвертируем в классический HTML и отправляем через send_message (по 4096 символов)
    fallback_html = text if prefer_html else gfm_to_fallback_html(text)
    chunks = split_text(fallback_html, CLASSIC_MAX_LENGTH)
    last_msg = None
    for i, part in enumerate(chunks):
        if not part.strip():
            continue
        current_kb = reply_markup if i == len(chunks) - 1 else None
        cur_reply_id = reply_to_message_id if i == 0 else None
        try:
            last_msg = await bot.send_message(
                chat_id=chat_id,
                text=part,
                parse_mode="HTML",
                reply_to_message_id=cur_reply_id,
                reply_markup=current_kb,
                disable_notification=disable_notification,
            )
        except Exception:
            # Если даже HTML не распарсился — отправляем чистым текстом без потери ответа
            plain_part = re.sub(r'<[^>]+>', '', part)
            last_msg = await bot.send_message(
                chat_id=chat_id,
                text=plain_part[:CLASSIC_MAX_LENGTH],
                reply_to_message_id=cur_reply_id,
                reply_markup=current_kb,
                disable_notification=disable_notification,
            )
    return last_msg


class RichStatusTracker:
    """
    Отображает живой процесс размышлений и действий бота перед ответом:
    - В ЛС (private): использует нативный sendRichMessageDraft с тегом <tg-thinking>
      и встроенной кнопкой остановки (can_stop=True).
    - Автоматически поддерживает жизнь черновика (keep-alive каждые 15 сек),
      чтобы плашка <tg-thinking> не исчезала по 30-секундному таймауту Telegram во время долгих закачек!
    - В группах (где Telegram API запрещает вызывать sendRichMessageDraft):
      рисует красивую многошаговую панель размышлений (выполненные шаги с ✅, активный шаг с ⏳
      и сворачиваемая цитата с мыслями ИИ) + инлайн-кнопка "🛑 Отмена".
    """
    def __init__(self, bot: Bot, chat_id: int, chat_type: str, reply_to_message_id: int = None, cancel_kb=None):
        self.bot = bot
        self.chat_id = chat_id
        self.chat_type = chat_type
        self.reply_to_message_id = reply_to_message_id
        self.cancel_kb = cancel_kb
        self.draft_id = int(time.time() * 1000) % 1_000_000_000 or 1
        self.steps: list[str] = []
        self.last_thought: str = ""
        self.wait_msg: Message | None = None
        # В Telegram Bot API метод sendRichMessageDraft работает ТОЛЬКО в личных чатах (private)
        self.draft_supported = (chat_type == "private")
        self._last_update_ts = 0.0
        self._keepalive_task = None
        self._closed = False

    def _ensure_keepalive(self):
        """Запускает фоновый таймер, который не дает черновику sendRichMessageDraft протухнуть через 30 сек."""
        if self.draft_supported and self._keepalive_task is None and not self._closed:
            import asyncio
            self._keepalive_task = asyncio.create_task(self._keep_alive_loop())

    async def _keep_alive_loop(self):
        """Каждые 15 секунд обновляет черновик в ЛС, пока идет долгая операция (скачивание/сжатие)."""
        import asyncio
        while not self._closed and self.draft_supported:
            await asyncio.sleep(15.0)
            if self._closed or not self.draft_supported or not self.steps:
                break
            # Если с последнего обновления прошло больше 12 секунд — переотправляем текущий статус
            if time.monotonic() - self._last_update_ts >= 12.0:
                try:
                    await self._render(force=True)
                except Exception:
                    pass

    async def step(self, step_text: str, thought_preview: str = "", force: bool = False):
        """Добавляет новый шаг (или обновляет проценты скачивания на месте) и отрисовывает статус."""
        if self._closed:
            return

        if step_text:
            # Если это обновление процентов скачивания ("📥 Качаю...") — заменяем предыдущую строку процентов на месте,
            # чтобы не плодить десятки строк "10%... 20%... 30%..."
            if (
                self.steps
                and step_text.startswith("📥 Качаю большой файл")
                and (self.steps[-1].startswith("📥 Качаю большой файл") or self.steps[-1].startswith("📦 "))
            ):
                self.steps[-1] = step_text
            elif not self.steps or self.steps[-1] != step_text:
                self.steps.append(step_text)

        if thought_preview:
            self.last_thought = thought_preview.strip()

        self._ensure_keepalive()
        await self._render(force=force)

    async def _render(self, force: bool = False):
        now = time.monotonic()

        # 1. В личных чатах (private) стримим <tg-thinking> через sendRichMessageDraft
        if self.draft_supported:
            if not force and (now - self._last_update_ts < 0.7):
                return
            thinking_lines = "\n".join(f"• {s}" for s in self.steps)
            if self.last_thought:
                thinking_lines += f"\n\n{self.last_thought}"

            draft_md = f"<tg-thinking>{thinking_lines}</tg-thinking>"
            try:
                await _call_bot_api("sendRichMessageDraft", {
                    "chat_id": self.chat_id,
                    "draft_id": self.draft_id,
                    "rich_message": {"markdown": draft_md},
                    "reply_parameters": {"message_id": self.reply_to_message_id} if self.reply_to_message_id else None,
                    "can_stop": True,
                })
                self._last_update_ts = now
                return
            except Exception as e:
                logger.debug(f"sendRichMessageDraft не сработал, переключаюсь на групповой виджет: {e}")
                self.draft_supported = False

        # 2. Для групповых чатов: формируем красивую многошаговую карточку размышлений
        step_lines = []
        for idx, s in enumerate(self.steps):
            if idx < len(self.steps) - 1:
                # Прошлые завершенные этапы помечаем галочкой
                step_lines.append(f"✅ <i>{html.escape(s)}</i>")
            else:
                # Текущий активный этап выделяем жирным и песочными часами
                step_lines.append(f"⏳ <b>{html.escape(s)}</b>")

        steps_block = "\n".join(step_lines) if step_lines else "⏳ <b>Думаю...</b>"
        display_text = f"💭 <b>Ход работы бота:</b>\n<blockquote expandable>{steps_block}"
        if self.last_thought:
            display_text += f"\n\n🧠 <i>{html.escape(self.last_thought)}</i>"
        display_text += "</blockquote>"

        try:
            if not self.wait_msg:
                self.wait_msg = await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=display_text,
                    parse_mode="HTML",
                    reply_to_message_id=self.reply_to_message_id,
                    reply_markup=self.cancel_kb,
                    disable_notification=True,
                )
                self._last_update_ts = now
            elif force or (now - self._last_update_ts >= 1.2):
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.wait_msg.message_id,
                    text=display_text,
                    parse_mode="HTML",
                    reply_markup=self.cancel_kb,
                )
                self._last_update_ts = now
        except Exception:
            pass

    async def cleanup(self):
        """Останавливает фоновый таймер и удаляет временное окно статуса в группе перед отправкой ответа."""
        self._closed = True
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self.wait_msg:
            try:
                await self.wait_msg.delete()
            except Exception:
                pass
            self.wait_msg = None