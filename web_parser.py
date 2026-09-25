import re
import os
import platform
import shutil
import tempfile
import asyncio
import aiohttp
import yt_dlp
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
import markdownify
import pymupdf  # PyMuPDF
import logging
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Критично: main.py импортирует этот модуль ДО своего собственного load_dotenv(),
# поэтому os.getenv() ниже иначе всегда возвращал бы None для GROQ_API_KEY и т.п.
# Подгружаем .env здесь же, на всякий случай — если уже загружен, это no-op.
load_dotenv()

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # Папка, где лежит сам скрипт

# Клиент для распознавания речи (Whisper через Groq).
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")

whisper_client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=GROQ_URL) if GROQ_API_KEY else None
if whisper_client:
    logger.info("Whisper-фолбэк (Groq) включён.")
else:
    logger.warning("GROQ_API_KEY не найден в окружении — Whisper-фолбэк выключен.")

# Файл с куками залогиненного YouTube-аккаунта (формат Netscape cookies.txt).
# Путь может быть относительным — тогда он считается от папки со скриптом (BASE_DIR),
# либо абсолютным (например "C:\MyApp\Bot\LZBot\cookies.txt").
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE")
if YTDLP_COOKIES_FILE:
    if not os.path.isabs(YTDLP_COOKIES_FILE):
        YTDLP_COOKIES_FILE = os.path.join(BASE_DIR, YTDLP_COOKIES_FILE)
    if os.path.isfile(YTDLP_COOKIES_FILE):
        logger.info(f"Используем cookies файл для yt-dlp: {YTDLP_COOKIES_FILE}")
    else:
        logger.warning(f"YTDLP_COOKIES_FILE указан ({YTDLP_COOKIES_FILE}), но файл не найден — работаем без кук.")
        YTDLP_COOKIES_FILE = None

# Порядок "клиентов", под которые yt-dlp маскирует запросы. Если один поймал бан/лимит,
# пробуем следующий — у каждого своя инфраструктура выдачи на стороне YouTube.
PLAYER_CLIENTS = [c.strip() for c in os.getenv("YTDLP_PLAYER_CLIENTS", "android,ios,web").split(",") if c.strip()]


def _locate_ffmpeg() -> str | None:
    """
    Ищем ffmpeg/ffprobe: сначала локально в BASE_DIR/lib (удобно для портативного деплоя
    на Windows без системной установки), затем в PATH. Если не нашли — не роняем бота:
    аудио-транскрипция через Whisper это последний резервный уровень, а не ядро парсера.
    Возвращает путь к папке с бинарниками (для yt-dlp 'ffmpeg_location') либо None.
    """
    if platform.system() == "Windows":
        candidate_dir = os.path.join(BASE_DIR, "lib")
        ffmpeg_exe = os.path.join(candidate_dir, "ffmpeg.exe")
        ffprobe_exe = os.path.join(candidate_dir, "ffprobe.exe")
        if os.path.isfile(ffmpeg_exe) and os.path.isfile(ffprobe_exe):
            return candidate_dir

    ffmpeg_in_path = shutil.which("ffmpeg")
    ffprobe_in_path = shutil.which("ffprobe")
    if ffmpeg_in_path and ffprobe_in_path:
        return os.path.dirname(ffmpeg_in_path) or None

    return None


FFMPEG_LOCATION = _locate_ffmpeg()
if FFMPEG_LOCATION:
    logger.info(f"ffmpeg найден, используем: {FFMPEG_LOCATION}")
else:
    logger.warning(
        f"ffmpeg/ffprobe не найдены (ни в {os.path.join(BASE_DIR, 'lib')}, ни в PATH) — "
        "Whisper-фолбэк по аудио отключён. Положите ffmpeg.exe и ffprobe.exe в папку lib "
        "рядом со скриптом либо установите ffmpeg в PATH."
    )



# --- Оповещение админа о протухших куках / антибот-блокировке YouTube ---
# Не завязываемся на конкретное имя переменной окружения для токена бота — main.py
# уже логинит бота, так что просто передаёт сюда свой токен и ваш telegram id один раз
# при старте: web_parser.configure_admin_alerts(BOT_TOKEN, ADMIN_CHAT_ID)
_ADMIN_BOT_TOKEN: str | None = None
_ADMIN_CHAT_ID: str | None = None
_last_cookie_alert_ts: float = 0.0
COOKIE_ALERT_COOLDOWN_SEC = 6 * 3600  # не чаще раза в 6 часов — иначе при затяжной блокировке будет спам

# Эти фразы отдаёт именно YouTube/yt-dlp, когда сработала антибот-защита и куки
# нужны/протухли — сетевые обрывы, таймауты и 5xx текстово выглядят совсем иначе,
# так что ложных срабатываний на "сервер недоступен" быть не должно.
_COOKIE_ISSUE_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "please sign in",
    "this video is private",
    "members-only content",
    "cookies are no longer valid",
    "failed to parse cookies",
)


def configure_admin_alerts(bot_token: str, admin_chat_id: str) -> None:
    """Вызывается один раз из main.py, чтобы модуль умел писать вам в личку при проблеме с куками."""
    global _ADMIN_BOT_TOKEN, _ADMIN_CHAT_ID
    _ADMIN_BOT_TOKEN = bot_token
    _ADMIN_CHAT_ID = admin_chat_id


def _looks_like_cookie_issue(error_text: str) -> bool:
    low = (error_text or "").lower()
    return any(marker in low for marker in _COOKIE_ISSUE_MARKERS)


async def _maybe_alert_cookie_issue(error_text: str) -> None:
    global _last_cookie_alert_ts
    if not _looks_like_cookie_issue(error_text):
        return
    if not (_ADMIN_BOT_TOKEN and _ADMIN_CHAT_ID):
        logger.warning("Похоже, куки YouTube протухли, но configure_admin_alerts() не вызван — некому написать.")
        return

    now = asyncio.get_event_loop().time()
    if now - _last_cookie_alert_ts < COOKIE_ALERT_COOLDOWN_SEC:
        return  # уже предупреждали недавно, не дублируем
    _last_cookie_alert_ts = now

    text = (
        "⚠️ Похоже, куки YouTube протухли или включилась антибот-защита "
        "(\"Sign in to confirm you're not a bot\").\n"
        f"Обновите файл: {YTDLP_COOKIES_FILE or 'YTDLP_COOKIES_FILE'}\n\n"
        f"Исходная ошибка: {error_text[:300]}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{_ADMIN_BOT_TOKEN}/sendMessage",
                json={"chat_id": _ADMIN_CHAT_ID, "text": text},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:
        logger.warning(f"Не удалось отправить админу уведомление о куках: {e}")


TEST_VIDEO_ID = "jNQXAC9IVRw"  # "Me at the zoo" — первое видео на YouTube, гарантированно живо и публично


async def check_cookies_health() -> bool:
    """
    Плановая проверка (вызывайте раз в день из вашего планировщика в main.py):
    пробуем получить метаданные заведомо доступного видео теми же куками, которыми
    пользуется бот. Если YouTube ответит антибот-ошибкой — алерт админу уйдёт сразу,
    а не после первого реального сбоя у пользователя.
    Возвращает True, если куки (или анонимный доступ) в порядке.
    """
    base_opts = build_base_ydl_opts()
    client = PLAYER_CLIENTS[0] if PLAYER_CLIENTS else 'android'
    opts = {**base_opts, 'skip_download': True, 'extractor_args': {'youtube': [f'player_client={client}']}}

    def get_info():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"https://www.youtube.com/watch?v={TEST_VIDEO_ID}", download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(get_info), timeout=15.0)
        if info:
            logger.info("Плановая проверка кук YouTube: всё в порядке ✅")
            return True
        return False
    except Exception as e:
        logger.warning(f"Плановая проверка кук YouTube провалилась: {e}")
        await _maybe_alert_cookie_issue(str(e))
        return False


def build_base_ydl_opts() -> dict:
    """Базовые опции yt-dlp, общие для извлечения метаданных и для скачивания аудио."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    if YTDLP_COOKIES_FILE:
        opts['cookiefile'] = YTDLP_COOKIES_FILE
    return opts


def extract_urls(text: str) -> list:
    url_pattern = re.compile(r'https?://\S+')
    return url_pattern.findall(text)


async def transcribe_via_audio(clean_url: str, base_ydl_opts: dict) -> str | None:
    """
    Резервный (третий) уровень: если субтитры недоступны (429 / нет речи в разметке /
    защита YouTube), скачиваем минимальный по весу аудиопоток и прогоняем его через
    Whisper (Groq). Groq лимитирует файл ~25MB, поэтому берём самое лёгкое аудио
    и сразу конвертируем в mp3 с низким битрейтом через ffmpeg (обязательно должен
    быть установлен в системе, куда деплоится бот).
    """
    if not whisper_client:
        logger.warning("GROQ_API_KEY не задан — Whisper-фолбэк недоступен.")
        return None

    if not FFMPEG_LOCATION:
        logger.warning("ffmpeg недоступен — пропускаем аудио-фолбэк для этого видео.")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        out_template = os.path.join(tmpdir, "%(id)s.%(ext)s")
        audio_opts = {
            **base_ydl_opts,
            'format': 'worstaudio/bestaudio',  # самый лёгкий поток — экономим трафик и время
            'outtmpl': out_template,
            'ffmpeg_location': FFMPEG_LOCATION,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '64',  # 64kbps достаточно для распознавания речи
            }],
        }
        audio_opts.pop('skip_download', None)

        def download() -> str | None:
            with yt_dlp.YoutubeDL(audio_opts) as ydl:
                ydl.download([clean_url])
            for fname in os.listdir(tmpdir):
                return os.path.join(tmpdir, fname)
            return None

        try:
            audio_path = await asyncio.wait_for(asyncio.to_thread(download), timeout=90.0)
        except asyncio.TimeoutError:
            logger.warning("Таймаут скачивания аудио для Whisper.")
            return None
        except Exception as e:
            logger.warning(f"Ошибка скачивания аудио для Whisper: {e}")
            # Это последний рубеж фолбэков — если и тут ошибка похожа на куки/антибот, стоит написать
            await _maybe_alert_cookie_issue(str(e))
            return None

        if not audio_path or not os.path.exists(audio_path):
            return None

        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > 24:
            logger.warning(f"Аудио слишком большое для Whisper ({size_mb:.1f}MB) — пропускаем видео целиком.")
            return None

        try:
            with open(audio_path, "rb") as f:
                transcript = await whisper_client.audio.transcriptions.create(
                    model=WHISPER_MODEL,
                    file=f,
                    response_format="text",
                )
            text = transcript if isinstance(transcript, str) else getattr(transcript, "text", "")
            text = text.strip()
            if text:
                logger.info(f"✅ УСПЕШНО ТРАНСКРИБИРОВАНО ЧЕРЕЗ WHISPER: {text[:100]}...")
                return text
            return None
        except Exception as e:
            logger.warning(f"Ошибка транскрибации через Whisper (Groq): {e}")
            return None


async def parse_youtube(url: str) -> str:
    try:
        # 1. Извлекаем чистый ID
        video_id = None
        if "v=" in url:
            video_id = url.split("v=")[1].split("&")[0]
        elif "youtu.be/" in url:
            video_id = url.split("youtu.be/")[1].split("?")[0]
        elif "shorts/" in url:
            video_id = url.split("shorts/")[1].split("?")[0].split("&")[0]
        elif "/live/" in url:
            video_id = url.split("/live/")[1].split("?")[0].split("&")[0]

        if video_id:
            video_id = video_id.split("/")[0]  # на случай хвостовых /что-то в пути

        if not video_id:
            return f"[Видео на YouTube (не удалось извлечь ID: {url})]"

        clean_url = f"https://www.youtube.com/watch?v={video_id}"

        # 2. Настраиваем yt-dlp и пробуем клиентов по очереди, пока один не сработает
        base_opts = build_base_ydl_opts()
        ydl_opts = {**base_opts, 'skip_download': True}

        def get_info(client: str):
            opts = {**ydl_opts, 'extractor_args': {'youtube': [f'player_client={client}']}}
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(clean_url, download=False)

        info = None
        working_client = PLAYER_CLIENTS[0] if PLAYER_CLIENTS else 'android'
        last_error_text = ""
        for client in (PLAYER_CLIENTS or ['android']):
            try:
                info = await asyncio.wait_for(asyncio.to_thread(get_info, client), timeout=12.0)
                if info:
                    working_client = client
                    break
            except asyncio.TimeoutError:
                logger.warning(f"Таймаут получения метаданных (клиент={client}).")
            except Exception as e:
                last_error_text = str(e)
                logger.warning(f"Клиент {client} не смог получить метаданные: {e}")

        if not info:
            # Все клиенты отвалились — если причина похожа на протухшие куки/антибот, пишем админу
            await _maybe_alert_cookie_issue(last_error_text)
            return "[Видео на YouTube (ошибка получения данных)]"

        # Дальше используем именно тот клиент, который сработал — он же, скорее всего,
        # успешнее отдаст и субтитры/аудио, раз именно его YouTube сейчас пропускает.
        ydl_opts['extractor_args'] = {'youtube': [f'player_client={working_client}']}

        title = info.get('title', 'Неизвестное видео')
        channel = info.get('uploader', 'Неизвестный канал')
        description = (info.get('description') or '')[:300]
        video_info = f"[YouTube Видео]\nНазвание: {title}\nКанал: {channel}\nОписание: {description}...\n"

        # 3. Вытягиваем ссылки на авто-субтитры
        subtitles_dict = info.get('subtitles') or {}
        auto_dict = info.get('automatic_captions') or {}

        all_subs = {**auto_dict, **subtitles_dict}
        target_tracks = None

        for lang in ['ru', 'ru-RU', 'en', 'en-US', 'en-GB']:
            if lang in all_subs:
                target_tracks = all_subs[lang]
                break

        if not target_tracks and all_subs:
            target_tracks = list(all_subs.values())[0]

        sub_url = None
        if target_tracks:
            for fmt in target_tracks:
                if fmt.get('ext') == 'json3':
                    sub_url = fmt.get('url')
                    break
            if not sub_url:
                sub_url = target_tracks[0].get('url')

        # 4. Скачиваем текст субтитров (ОСНОВНОЙ ПУТЬ)
        extracted_subtitles = None
        if sub_url:
            try:
                headers = info.get('http_headers', {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                })

                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(sub_url, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            lines = []
                            for event in data.get('events', []):
                                for seg in event.get('segs', []):
                                    chunk = seg.get('utf8', '').strip()
                                    if chunk and chunk != '\n':
                                        lines.append(chunk)
                            raw_text = " ".join(lines)
                            extracted_subtitles = f"\n[Субтитры]: {raw_text[:6000]}..." if len(
                                raw_text) > 6000 else f"\n[Субтитры]: {raw_text}"
                            print(f"✅ УСПЕШНО ИЗВЛЕЧЕНЫ СУБТИТРЫ (через yt-dlp):\n{extracted_subtitles[:100]}...")
                        else:
                            logger.warning(f"YouTube вернул код {resp.status} при попытке скачать субтитры (yt-dlp).")
            except Exception as e:
                logger.warning(f"Ошибка загрузки дорожки субтитров yt-dlp: {e}")

        # 5. РЕЗЕРВНЫЙ ПЛАН (Если yt-dlp словил 429 или вообще не нашел ссылку)
        if not extracted_subtitles:
            try:
                def fetch_backup():
                    # В youtube-transcript-api >= 1.0 статический get_transcript() убрали,
                    # нужен инстанс и .fetch(). Пробуем новый API, а если вдруг на машине
                    # стоит старая версия библиотеки — откатываемся на старый вызов.
                    try:
                        ytt_api = YouTubeTranscriptApi()
                        fetched = ytt_api.fetch(video_id, languages=['ru', 'en'])
                        return " ".join(snippet.text for snippet in fetched)
                    except AttributeError:
                        transcript_data = YouTubeTranscriptApi.get_transcript(video_id, languages=['ru', 'en'])
                        return " ".join(entry['text'] for entry in transcript_data)

                text = await asyncio.wait_for(asyncio.to_thread(fetch_backup), timeout=7.0)
                extracted_subtitles = f"\n[Субтитры]: {text[:6000]}..." if len(text) > 6000 else f"\n[Субтитры]: {text}"
                print(f"✅ УСПЕШНО ИЗВЛЕЧЕНЫ СУБТИТРЫ (через резервный API):\n{extracted_subtitles[:100]}...")
            except Exception as e:
                logger.warning(f"Резервный парсер тоже не справился: {e}")

        # 6. ПОСЛЕДНИЙ РУБЕЖ: оба текстовых способа отдали 429 / ничего не нашли —
        # скачиваем аудио и распознаём его сами через Whisper, не завися от YouTube.
        if not extracted_subtitles:
            whisper_text = await transcribe_via_audio(clean_url, base_opts)
            if whisper_text:
                extracted_subtitles = f"\n[Субтитры (распознано через Whisper)]: {whisper_text[:6000]}..." \
                    if len(whisper_text) > 6000 else f"\n[Субтитры (распознано через Whisper)]: {whisper_text}"
            else:
                extracted_subtitles = "\n[Субтитры недоступны (YouTube заблокировал запрос, в видео нет речи, " \
                                       "или не настроен GROQ_API_KEY для Whisper-фолбэка)]"

        return video_info + extracted_subtitles

    except asyncio.TimeoutError:
        logger.warning(f"Таймаут парсинга YouTube: {url}")
        return f"[Видео на YouTube] (Таймаут получения данных)"
    except Exception as e:
        logger.warning(f"Ошибка yt-dlp: {e}")
        return f"[Видео на YouTube по ссылке {url}]"

# Другие платформы, которые понимает yt-dlp и где имеет смысл пытаться вытащить видео.
# У них, в отличие от YouTube, обычно нет готовой текстовой дорожки субтитров —
# поэтому единственный реалистичный путь понять содержание — аудио + Whisper.
OTHER_VIDEO_HOSTS = ("tiktok.com", "instagram.com", "x.com", "twitter.com")


async def parse_generic_video(url: str) -> str:
    """
    Универсальный путь для TikTok/Instagram/X и т.п.: metadata через yt-dlp +
    аудио-фолбэк через Whisper. Instagram и X часто отдают видео только залогиненным —
    так что для них YTDLP_COOKIES_FILE (с куками именно под тот сайт) особенно важен.
    TikTok обычно отдаёт публичные ролики и без кук.
    """
    base_opts = build_base_ydl_opts()
    opts = {**base_opts, 'skip_download': True}

    def get_info():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(get_info), timeout=15.0)
    except asyncio.TimeoutError:
        return f"[Видео по ссылке {url}] (таймаут получения данных)"
    except Exception as e:
        logger.warning(f"Не удалось получить метаданные {url}: {e}")
        await _maybe_alert_cookie_issue(str(e))
        return f"[Видео по ссылке {url} (не удалось получить данные — возможно, нужны куки для этого сайта)]"

    if not info:
        return f"[Видео по ссылке {url} (пусто)]"

    title = info.get('title', 'Без названия')
    uploader = info.get('uploader', 'Неизвестный автор')
    description = (info.get('description') or '')[:300]
    platform_name = info.get('extractor_key', 'видео')
    video_info = f"[{platform_name}]\nНазвание: {title}\nАвтор: {uploader}\nОписание: {description}...\n"

    whisper_text = await transcribe_via_audio(url, base_opts)
    if whisper_text:
        body = f"\n[Распознано через Whisper]: {whisper_text[:6000]}..." \
            if len(whisper_text) > 6000 else f"\n[Распознано через Whisper]: {whisper_text}"
    else:
        body = "\n[Аудио/речь распознать не удалось]"

    return video_info + body


async def fetch_url_content(url: str):
    if "youtube.com" in url or "youtu.be" in url:
        text = await parse_youtube(url)
        return text, None

    if any(host in url for host in OTHER_VIDEO_HOSTS):
        text = await parse_generic_video(url)
        return text, None

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.get(url, timeout=15) as response:
                content_type = response.headers.get('Content-Type', '').lower()
                # Скачиваем файл целиком в память
                file_bytes = await response.read()

                # 1. Если это HTML-страница (Сайт)
                if 'text/html' in content_type:
                    soup = BeautifulSoup(file_bytes.decode('utf-8', errors='ignore'), 'html.parser')
                    for script in soup(["script", "style"]):
                        script.extract()
                    # Превращаем HTML в красивый Markdown
                    html_content = str(soup)
                    text = markdownify.markdownify(html_content, heading_style="ATX").strip()
                    text = re.sub(r'\n{3,}', '\n\n', text)  # Убираем лишние пустые строки
                    return f"[Содержимое сайта]:\n{text[:6000]}...", None

                # 2. Если это картинка
                elif 'image' in content_type:
                    return f"[Пользователь прикрепил картинку]", file_bytes

                # 3. Если это PDF-документ
                elif 'application/pdf' in content_type:
                    try:
                        doc = pymupdf.open(stream=file_bytes, filetype="pdf")
                        text = "".join([page.get_text() for page in doc])
                        return f"[Содержимое PDF документа]:\n{text[:6000]}...", None
                    except Exception as e:
                        logger.error(f"Ошибка парсинга PDF: {e}")
                        return "[Не удалось прочитать PDF документ]", None

                # 4. Неизвестные файлы (Попытка прочитать как текст: логи, код, конфиги)
                else:
                    try:
                        text = file_bytes.decode('utf-8')
                        return f"[Содержимое текстового файла]:\n{text[:6000]}...", None
                    except UnicodeDecodeError:
                        # Если выдало ошибку кодировки, значит это бинарник (.exe, .zip и т.д.)
                        return f"[Ссылка ведет на бинарный файл типа {content_type}. Анализ не поддерживается.]", None

        except Exception as e:
            logger.warning(f"Ошибка при загрузке {url}: {e}")
            return "", None


async def process_message_for_urls(text: str):
    urls = extract_urls(text)
    if not urls:
        return text, None

    appended_text = "\n\n--- ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ ДЛЯ ИИ (СОДЕРЖИМОЕ ССЫЛОК) ---\n"
    found_image_bytes = None

    for url in urls[:3]:
        content_text, img_bytes = await fetch_url_content(url)
        if content_text:
            appended_text += content_text + "\n"
        if img_bytes and not found_image_bytes:
            found_image_bytes = img_bytes

    if appended_text.strip() != "--- ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ ДЛЯ ИИ (СОДЕРЖИМОЕ ССЫЛОК) ---":
        clean_text = re.sub(r'https?://\S+', '[ССЫЛКА ПРИКРЕПЛЕНА]', text).strip()
        return clean_text + appended_text, found_image_bytes

    return text, None