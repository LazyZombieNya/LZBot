import re
import asyncio
import aiohttp
import yt_dlp
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
import logging

logger = logging.getLogger(__name__)


def extract_urls(text: str) -> list:
    url_pattern = re.compile(r'https?://\S+')
    return url_pattern.findall(text)


async def parse_youtube(url: str) -> str:
    try:
        # === 1. ОЧИСТКА ССЫЛКИ ===
        # Вытаскиваем чистый ID видео, чтобы отрезать плейлисты, таймкоды и прочий мусор
        video_id = None
        if "v=" in url:
            video_id = url.split("v=")[1].split("&")[0]
        elif "youtu.be/" in url:
            video_id = url.split("youtu.be/")[1].split("?")[0]

        if not video_id:
            return f"[Видео на YouTube (не удалось извлечь ID из ссылки: {url})]"

        clean_url = f"https://www.youtube.com/watch?v={video_id}"

        # === 2. ПОЛУЧЕНИЕ МЕТАДАННЫХ ===
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'no_warnings': True,
            'noplaylist': True,
            'extract_flat': True
        }

        def get_info():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(clean_url, download=False)  # Используем чистую ссылку!

        # Ставим таймаут 10 секунд
        info = await asyncio.wait_for(asyncio.to_thread(get_info), timeout=10.0)

        if not info:
            return "[Видео на YouTube (ошибка получения данных)]"

        title = info.get('title', 'Неизвестное видео')
        channel = info.get('uploader', 'Неизвестный канал')
        description = info.get('description', '')
        if description:
            description = description[:300]
        else:
            description = ""

        video_info = f"[YouTube Видео]\nНазвание: {title}\nКанал: {channel}\nОписание: {description}...\n"

        # === 3. УМНЫЙ ПАРСИНГ СУБТИТРОВ ===
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)  # ID у нас уже есть!
            try:
                # Ищем русские или английские
                t = transcript_list.find_transcript(['ru', 'en'])
            except Exception:
                # Если их нет, забираем самую первую доступную дорожку
                t = next(iter(transcript_list))

            transcript_data = t.fetch()
            text = " ".join([entry['text'] for entry in transcript_data])
            subtitles = f"\n[Субтитры]: {text[:5000]}..." if len(text) > 5000 else f"\n[Субтитры]: {text}"
            return video_info + subtitles

        except Exception as e:
            return video_info + "\n[Субтитры недоступны (возможно, это музыка или шортс)]"

    except asyncio.TimeoutError:
        logger.warning(f"Таймаут парсинга YouTube: {url}")
        return f"[Видео на YouTube] (Таймаут получения метаданных)"
    except Exception as e:
        logger.warning(f"Не удалось получить данные YouTube: {e}")
        return f"[Видео на YouTube по ссылке {url}]"


async def fetch_url_content(url: str):
    if "youtube.com" in url or "youtu.be" in url:
        text = await parse_youtube(url)
        return text, None

    # Притворяемся настоящим Chrome-браузером, чтобы Cloudflare нас не блокировал
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.get(url, timeout=10) as response:
                content_type = response.headers.get('Content-Type', '').lower()

                if 'text/html' in content_type:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    for script in soup(["script", "style"]):
                        script.extract()
                    text = soup.get_text(separator=' ', strip=True)
                    return f"[Содержимое сайта]: {text[:5000]}...", None

                elif 'image' in content_type:
                    image_bytes = await response.read()
                    return f"[Пользователь прикрепил картинку]", image_bytes

                else:
                    return f"[Ссылка ведет на файл типа {content_type}. Скачивание пока не поддерживается.]", None

        except Exception as e:
            logger.warning(f"Ошибка при парсинге {url}: {e}")
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
        # Убираем сами URL из текста пользователя, чтобы ИИ не блокировал их по названиям доменов
        clean_text = re.sub(r'https?://\S+', '[ССЫЛКА ПРИКРЕПЛЕНА]', text).strip()
        return clean_text + appended_text, found_image_bytes

    return text, None