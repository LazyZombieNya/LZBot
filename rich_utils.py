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
    Очищает текст для RichMessage:
    1. Переносит блоки <think>...</think> в <details>.
    2. Удаляет теги <tg-thinking>.
    3. Защищает знак $ перед числами (\$10).
    """
    if not text:
        return ""

    def _think_to_details(match):
        thought = match.group(1).strip()
        if not thought:
            return ""
        return f"<details><summary>💭 Ход мыслей ИИ</summary>\n\n{thought}\n</details>\n\n"

    text = re.sub(r'<think>(.*?)</think>', _think_to_details, text, flags=re.DOTALL)
    text = text.replace('<think>', '').replace('</think>', '')
    text = re.sub(r'</?tg-thinking>', '', text)

    # Защищаем блоки кода от экранирования знака $
    parts = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', text)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r'(?<![\\$])\$(\d)', r'\\$\1', parts[i])

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

    # 3. Конвертируем базовый GFM Markdown в HTML
    text = re.sub(r'^(#{1,6})\s+(.+)$', r'<b>\2</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^---+$', '───────────────', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'<a href="\2">\1</a>', text)

    # 4. Возвращаем блоки кода на место
    for idx, block in enumerate(code_blocks):
        text = text.replace(f"@@CODEBLOCK_{idx}@@", block)

    return text.strip()


def split_text(text: str, max_len: int = CLASSIC_MAX_LENGTH) -> list[str]:
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
    так и из RichMessage (где msg.text == None, а данные лежат в api_kwargs['rich_message']).
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
    Отображает процесс размышлений и действий бота перед ответом:
    - В ЛС (private) использует sendRichMessageDraft с тегом <tg-thinking> и кнопкой остановки (can_stop=True).
    - В группах (или при недоступности Draft API) плавно обновляет обычное сообщение ожидания.
    """
    def __init__(self, bot: Bot, chat_id: int, chat_type: str, reply_to_message_id: int = None, cancel_kb=None):
        self.bot = bot
        self.chat_id = chat_id
        self.chat_type = chat_type
        self.reply_to_message_id = reply_to_message_id
        self.cancel_kb = cancel_kb
        self.draft_id = int(time.time() * 1000) % 1_000_000_000 or 1
        self.steps: list[str] = []
        self.wait_msg: Message | None = None
        self.draft_supported = (chat_type == "private")
        self._last_update_ts = 0.0

    async def step(self, step_text: str, thought_preview: str = "", force: bool = False):
        if step_text and (not self.steps or self.steps[-1] != step_text):
            self.steps.append(step_text)

        thinking_lines = "\n".join(f"• {s}" for s in self.steps)
        if thought_preview:
            thinking_lines += f"\n\n{thought_preview.strip()}"

        now = time.monotonic()

        # 1. В личных чатах стримим <tg-thinking> через sendRichMessageDraft
        if self.draft_supported:
            if not force and (now - self._last_update_ts < 0.7):
                return
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
                logger.debug(f"sendRichMessageDraft не сработал, переключаюсь на обычное сообщение ожидания: {e}")
                self.draft_supported = False

        # 2. Фолбэк (для групп или если Draft API недоступен)
        current_label = self.steps[-1] if self.steps else "⏳ Ждём ответ..."
        display_text = f"⏳ <i>{html.escape(current_label)}</i>"
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
        if self.wait_msg:
            try:
                await self.wait_msg.delete()
            except Exception:
                pass
            self.wait_msg = None