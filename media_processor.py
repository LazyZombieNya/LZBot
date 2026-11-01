"""
Media Processor - модуль подготовки медиафайлов для Telegram.

Модуль скачивает медиафайлы по URL и автоматически подготавливает их
для отправки в Telegram: сжимает изображения и видео, изменяет разрешение,
разделяет длинные изображения и конвертирует GIF в MP4.

Результатом обработки является путь или список путей к готовым файлам,
которые можно использовать для дальнейшей отправки.

Временные файлы хранятся в директории `temp_data` и могут быть удалены
после завершения работы с помощью функции `clear_data_folder()`.

Version: 1.3.0
add:Добавлено извлечение аудиодорожки и возможность создать из видеофайла один JPEG-коллаж 2x2
"""

import asyncio
import os
import shutil
import json
import platform
from io import BytesIO
from urllib.parse import urlparse

import aiohttp
import aiofiles
import ffmpeg
from PIL import Image
import math

# Ограничения Telegram
MAX_SIZE_IMG_MB = 10  # Максимальный размер фото в MB
MAX_SIZE_VIDEO_MB = 50  # Максимальный размер видео в MB
MAX_IMAGE_SIDE = 4096  # Максимальная длина одной стороны картинки
MAX_IMAGE_SIDES_SUM = 10000  # Telegram: сумма width+height не должна превышать это значение
MAX_IMAGE_RATIO = 19  # Telegram режет фото с соотношением сторон > 20:1, берём 19 с запасом

MAX_VIDEO_COMPRESS_ATTEMPTS = 4  # Сколько раз пробуем пересжать видео, если не влезло в лимит
MIN_VIDEO_BITRATE = 80_000  # Нижний предел битрейта видео (bps), ниже — совсем не смотрибельно

DATA_FOLDER = "temp_data"  # Папка где хранятся временно скачанные файлы
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) # Место положение скрипта

# Отключаем защиту Pillow от "DecompressionBomb", чтобы открывать огромные файлы
Image.MAX_IMAGE_PIXELS = None

# FFmpeg мультимедийный фреймворк для работы с медиафайлами
if platform.system() == "Windows":
    FFMPEG_PATH = os.path.join(BASE_DIR, "lib", "ffmpeg.exe")
    FFPROBE_PATH = os.path.join(BASE_DIR, "lib", "ffprobe.exe")

    if not os.path.exists(FFMPEG_PATH):
        raise FileNotFoundError(
            f"FFmpeg not found at path {FFMPEG_PATH}"
        )

    if not os.path.exists(FFPROBE_PATH):
        raise FileNotFoundError(
            f"FFprobe not found at path {FFPROBE_PATH}"
        )

else:
    if shutil.which("ffmpeg") is None:
        raise FileNotFoundError(
            "FFmpeg not found in PATH. Install it: sudo apt install ffmpeg -y"
        )

    if shutil.which("ffprobe") is None:
        raise FileNotFoundError(
            "FFprobe not found in PATH. Install it: sudo apt install ffmpeg -y"
        )

    FFMPEG_PATH = "ffmpeg"
    FFPROBE_PATH = "ffprobe"

# Узнаем какого разрешения файл по ссылке
def get_file_extension(url):
    parsed_url = urlparse(url)
    path = parsed_url.path.strip('/')  # Достаем путь из ссылки и убираем лишние слэши
    parts = path.rsplit('.', 1)  # Разделяем по последней точке на части

    if len(parts) == 2 and parts[1]:  # Если есть расширение (т.е. состоит из 2 частей) возвращаем в нижнем регистре
        return parts[1].lower()
    return ""  # Если расширения нет, возвращаем пустую строку

# Возвращает размер файла
def format_size(size_bytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024

# получение информации о видео
async def get_video_info(path):
    command = [
        FFPROBE_PATH,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        path
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, _ = await process.communicate()
    data = json.loads(stdout)

    video_stream = next(
        s for s in data["streams"]
        if s["codec_type"] == "video"
    )

    return {
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "duration": float(data["format"]["duration"]),
        "bitrate": int(data["format"].get("bit_rate", 0))
    }

# Удаляет все файлы в папке DATA_FOLDER, если они есть.
async def clear_data_folder():
    if os.path.exists(DATA_FOLDER):
        for file in os.listdir(DATA_FOLDER):
            file_path = os.path.join(DATA_FOLDER, file)
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Error deleting {file_path}: {e}")

def _fit_telegram_photo_dimensions(img):
    width, height = img.size

    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        ratio = min(MAX_IMAGE_SIDE / width, MAX_IMAGE_SIDE / height)
        width, height = max(1, int(width * ratio)), max(1, int(height * ratio))
        img = img.resize((width, height), Image.LANCZOS)

    long_side, short_side = max(width, height), min(width, height)
    if short_side > 0 and long_side / short_side > MAX_IMAGE_RATIO:
        target_long = max(1, int(short_side * MAX_IMAGE_RATIO))
        if width >= height:
            left = (width - target_long) // 2
            img = img.crop((left, 0, left + target_long, height))
            width = target_long
        else:
            top = (height - target_long) // 2
            img = img.crop((0, top, width, top + target_long))
            height = target_long

    if width + height > MAX_IMAGE_SIDES_SUM:
        ratio = MAX_IMAGE_SIDES_SUM / (width + height)
        width, height = max(1, int(width * ratio)), max(1, int(height * ratio))
        img = img.resize((width, height), Image.LANCZOS)

    return img

"""
    Разрезает длинное изображение на равные части по вертикали.
    Режет только "вытянутые" картинки (комиксы/свитки).
    """


def split_long_image(image_path, max_height=4000):
    img = Image.open(image_path)
    width, height = img.size

    ratio = height / width if width > 0 else 0
    filename = os.path.basename(image_path)

    print(f"[АЛИЗ КАРТИНКИ] {filename} | Размер: {width}x{height} | Отношение (Высота/Ширина): {ratio:.2f}")

    # Не режем, если это обычный арт (соотношение меньше 2.5)
    # Если при этом он выше 4000px, compress_image сожмет его пропорционально.
    if ratio < 2.5:
        print(f"[АЛИЗ КАРТИНКИ] -> Пропускаем обрезку. Причина: Отношение ({ratio:.2f}) < 2.5 (не комикс)")
        return [image_path]

    # ДИНАМИЧЕСКИЙ РАСЧЕТ ВЫСОТЫ
    # Чтобы было удобно читать, высота куска не должна превышать ширину более чем в 3 раза.
    comfortable_chunk_height = width * 3.0

    # Итоговая целевая высота: берем комфортную, но не позволяем ей перевалить за лимит ТГ (4000)
    target_height = min(max_height, comfortable_chunk_height)

    # Вычисляем количество частей на основе нашей целевой высоты
    num_parts = math.ceil(height / target_height)
    part_height = math.ceil(height / num_parts)

    print(f"[АЛИЗ КАРТИНКИ] -> Вытянутый формат! Режем на {num_parts} частей (высота каждой ~{part_height}px)")

    parts = []
    base_dir = os.path.dirname(image_path)
    base_name, ext = os.path.splitext(os.path.basename(image_path))

    for i in range(num_parts):
        top = i * part_height
        bottom = min((i + 1) * part_height, height)

        box = (0, top, width, bottom)
        cropped_img = img.crop(box)

        if cropped_img.mode in ("RGBA", "P") and ext.lower() in (".jpg", ".jpeg"):
            cropped_img = cropped_img.convert("RGB")

        part_path = os.path.join(base_dir, f"{base_name}_part_{i}{ext}")
        cropped_img.save(part_path)
        parts.append(part_path)

    return parts

#Сжатие картинок если они больше MAX_SIZE_IMG_MB
async def compress_image(image_path_or_bytes, max_size=MAX_SIZE_IMG_MB * 1024 * 1024):
    if isinstance(image_path_or_bytes, bytes):
        img = Image.open(BytesIO(image_path_or_bytes))
        file_name = "bytes_image"
    else:
        img = Image.open(image_path_or_bytes)
        file_name = os.path.basename(image_path_or_bytes)

    orig_w, orig_h = img.size
    img = img.convert("RGB")

    # Подгоняем под лимиты Telegram
    img = _fit_telegram_photo_dimensions(img)
    fit_w, fit_h = img.size

    if orig_w != fit_w or orig_h != fit_h:
        print(f"[СЖАТИЕ] {file_name} | Обрезан лимитами ТГ: {orig_w}x{orig_h} -> {fit_w}x{fit_h}")

    quality = 90
    while True:
        output = BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)
        size = output.tell()

        if size <= max_size:
            print(f"[СЖАТИЕ] {file_name} | Успех: {fit_w}x{fit_h}, качество {quality}%, вес {size / 1024:.2f} KB")
            return output.getvalue()

        if quality > 50:
            print(
                f"[СЖАТИЕ] {file_name} | Вес {size / 1024 / 1024:.2f} MB > {max_size / 1024 / 1024:.0f} MB. Снижаем качество до {quality - 5}%")
            quality -= 5
            continue

        width, height = img.size
        if width < 1000 or height < 1000:
            print(
                f"[СЖАТИЕ] {file_name} | Достигнут предел (ширина/высота < 1000). Оставляем {width}x{height}, {size / 1024:.2f} KB")
            return output.getvalue()

        # Уменьшаем разрешение, если не влезли по весу
        new_w, new_h = int(width * 0.9), int(height * 0.9)
        print(f"[СЖАТИЕ] {file_name} | Уменьшаем разрешение: {width}x{height} -> {new_w}x{new_h}")
        img = img.resize((new_w, new_h), Image.LANCZOS)
        fit_w, fit_h = new_w, new_h  # Обновляем для логов

def _pick_scale(video_bitrate):
    if video_bitrate >= 2_500_000:
        return 1280
    elif video_bitrate >= 1_200_000:
        return 720
    elif video_bitrate >= 700_000:
        return 640
    elif video_bitrate >= 450_000:
        return 480
    elif video_bitrate >= 250_000:
        return 360
    else:
        return 256

async def _encode_video(input_path, temp_output, video_bitrate, audio_bitrate, scale):
    command = [
        FFMPEG_PATH,
        "-y",
        "-i", input_path,
        "-vf",
        f"scale='min(iw,{scale})':-2",
        "-c:v", "libx264",
        "-preset", "fast",
        "-b:v", str(video_bitrate),
        "-maxrate", str(int(video_bitrate * 1.2)),
        "-bufsize", str(int(video_bitrate * 2)),
        "-c:a", "aac",
        "-b:a", f"{audio_bitrate // 1000}k",
        "-movflags", "+faststart",
        temp_output
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        print("FFmpeg command:")
        print(" ".join(command))
        print(stderr.decode(errors="ignore"))
        return False

    return True

async def mux_video_audio(video_path, audio_path, output_path):
    """Склеивает видео и аудио без пережатия (прямое копирование потоков)."""
    command = [
        FFMPEG_PATH,
        "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",  # Конвертируем аудио в AAC для идеальной совместимости с Telegram
        output_path
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()
    return process.returncode == 0

#Сжатие видео с помощью FFMPEG. Если после прохода файл всё ещё не влезает
#в лимит — пересчитываем битрейт по фактическому результату и повторяем.
async def compress_video(input_path, output_path):
    info = await get_video_info(input_path)

    duration = info["duration"]
    width = info["width"]
    height = info["height"]

    size_mb = os.path.getsize(input_path) / (1024 * 1024)

    print(
        f"Video info: "
        f"{width}x{height}, "
        f"{duration:.1f}s, "
        f"{size_mb:.2f} MB"
    )

    target_size_mb = MAX_SIZE_VIDEO_MB * 0.92

    target_total_bitrate = int(
        target_size_mb * 8 * 1024 * 1024 / duration
    )

    if duration > 1800:
        audio_bitrate = 64_000
    elif duration > 600:
        audio_bitrate = 96_000
    else:
        audio_bitrate = 128_000

    # Раньше здесь стоял жёсткий пол в 300 kbps, который на длинных видео
    # (20+ минут) был в разы больше реально нужного битрейта и не позволял
    # уложиться в лимит ни при каких условиях. Теперь пол ниже, а нехватку
    # размера ловим ниже через повторные попытки с коррекцией битрейта.
    video_bitrate = max(
        MIN_VIDEO_BITRATE,
        target_total_bitrate - audio_bitrate
    )

    temp_output = output_path + ".tmp.mp4"

    for attempt in range(1, MAX_VIDEO_COMPRESS_ATTEMPTS + 1):
        scale = _pick_scale(video_bitrate)

        print(
            f"Attempt {attempt}/{MAX_VIDEO_COMPRESS_ATTEMPTS}: "
            f"target video bitrate {video_bitrate / 1000:.0f} kbps, "
            f"audio {audio_bitrate / 1000:.0f} kbps, scale {scale}p"
        )

        ok = await _encode_video(input_path, temp_output, video_bitrate, audio_bitrate, scale)
        if not ok:
            if os.path.exists(temp_output):
                os.remove(temp_output)
            return False

        new_size_mb = os.path.getsize(temp_output) / (1024 * 1024)

        print(
            f"Compressed (attempt {attempt}): "
            f"{size_mb:.2f} MB -> "
            f"{new_size_mb:.2f} MB"
        )

        if new_size_mb <= MAX_SIZE_VIDEO_MB:
            if os.path.exists(output_path):
                os.remove(output_path)
            shutil.move(temp_output, output_path)
            return True

        # Не влезли — считаем, во сколько раз фактический результат больше
        # цели, и пропорционально режем битрейт с запасом 10%, вместо того
        # чтобы гадать заново с фиксированным шагом.
        achieved_total_bitrate = (new_size_mb * 8 * 1024 * 1024) / duration
        os.remove(temp_output)

        if achieved_total_bitrate <= 0:
            break

        correction = (target_total_bitrate / achieved_total_bitrate) * 0.9
        new_video_bitrate = max(MIN_VIDEO_BITRATE, int(video_bitrate * correction))

        if new_video_bitrate >= video_bitrate:
            break  # коррекция ничего не меняет — дальше пытаться бессмысленно

        video_bitrate = new_video_bitrate

    print(
        f"Failed to compress {input_path} below {MAX_SIZE_VIDEO_MB} MB "
        f"after {MAX_VIDEO_COMPRESS_ATTEMPTS} attempts, size {format_size(os.path.getsize(input_path))}"
    )
    return False

#Функция конвертации Gif в Mp4
async def gif_to_mp4(input_path, output_path):
    command = [
        FFMPEG_PATH,
        "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-crf", "28",
        "-preset", "fast",
        output_path
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()
    return output_path

# Скачивает медиафайлы на диск со сжатием и склейкой звука (для Reddit DASH)
async def download_media(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/122.0.0.0",
        "Referer": url,
    }

    ext = get_file_extension(url)
    clean_url = url.split('?')[0]
    name_only = os.path.splitext(clean_url.split("/")[-1])[0].lower()
    filename = f"temp_{name_only[:40]}.{ext}"

    os.makedirs(DATA_FOLDER, exist_ok=True)
    file_path = os.path.join(DATA_FOLDER, filename)

    timeout = aiohttp.ClientTimeout(total=900, sock_read=45, sock_connect=30)
    is_reddit_video = "v.redd.it" in clean_url and ext == 'mp4'

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            temp_path = file_path + "_temp"

            # --- ЭТАП СКАЧИВАНИЯ ---
            if is_reddit_video:
                base_url = clean_url.rsplit('/', 1)[0]

                # РАСШИРЕННЫЙ СПИСОК: Добавлены новые варианты расположения звука для формата CMAF
                audio_urls = [
                    f"{base_url}/DASH_AUDIO_128.mp4",
                    f"{base_url}/DASH_AUDIO_64.mp4",
                    f"{base_url}/DASH_audio.mp4",
                    f"{base_url}/CMAF_AUDIO_128.mp4",
                    f"{base_url}/CMAF_AUDIO_64.mp4",
                    f"{base_url}/audio",
                    f"{base_url}/audio.mp4"
                ]

                # 1. Качаем немое видео
                async with session.get(url, headers=headers) as resp_v:
                    if resp_v.status != 200: return None
                    async with aiofiles.open(temp_path, 'wb') as f:
                        async for chunk in resp_v.content.iter_chunked(1024 * 1024): await f.write(chunk)

                # 2. Ищем и качаем звук (перебираем все варианты из списка)
                temp_aud = file_path + "_aud.mp4"
                has_audio = False
                for aud_url in audio_urls:
                    async with session.get(aud_url, headers=headers) as resp_a:
                        if resp_a.status == 200:
                            async with aiofiles.open(temp_aud, 'wb') as f:
                                async for chunk in resp_a.content.iter_chunked(1024 * 1024): await f.write(chunk)
                            has_audio = True
                            break  # Если звук нашли - прерываем поиск

                # 3. Склеиваем
                if has_audio and os.path.getsize(temp_aud) > 0:
                    print(f"[{name_only}] Найден звук, склеиваем...")
                    temp_mux = file_path + "_mux.mp4"
                    mux_ok = await mux_video_audio(temp_path, temp_aud, temp_mux)
                    os.remove(temp_aud)
                    if mux_ok:
                        os.remove(temp_path)
                        temp_path = temp_mux
                else:
                    if os.path.exists(temp_aud): os.remove(temp_aud)
                    print(f"[{name_only}] Звука нет (немое видео или GIF).")
            else:
                # Стандартное скачивание (картинки, сторонние видео)
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        async with aiofiles.open(temp_path, 'wb') as f:
                            async for chunk in response.content.iter_chunked(1024 * 1024):
                                await f.write(chunk)
                    else:
                        print(f"Ошибка HTTP: {response.status} для {url}")
                        return None

            # --- ЭТАП ПРОВЕРКИ И СЖАТИЯ ---
            size_file = os.path.getsize(temp_path)
            print(f"Downloaded {temp_path} size {format_size(size_file)}")

            if not os.path.exists(temp_path) or size_file == 0:
                print(f"Ошибка: Скачанный файл пустой: {url}")
                if os.path.exists(temp_path): os.remove(temp_path)
                return None

            if ext in ['jpeg', 'jpg', 'png', 'bmp', 'webp']:
                shutil.move(temp_path, file_path)
                image_parts = split_long_image(file_path, max_height=4000)
                final_paths = []
                for i, part in enumerate(image_parts):
                    compressed_bytes = await compress_image(part)
                    part_final_path = f"{file_path}_part{i}.{ext}" if len(image_parts) > 1 else file_path
                    async with aiofiles.open(part_final_path, 'wb') as img_file:
                        await img_file.write(compressed_bytes)
                    final_paths.append(part_final_path)
                    if part != file_path: os.remove(part)
                if len(image_parts) > 1 and os.path.exists(file_path): os.remove(file_path)
                return final_paths

            elif ext in ['mp4', 'avi', 'mov', 'mkv', 'webm', 'gif']:
                if ext == "gif":
                    compressed_path = file_path.replace(".gif", ".mp4")
                    await gif_to_mp4(temp_path, compressed_path)
                    file_path = compressed_path
                elif size_file < MAX_SIZE_VIDEO_MB * 1024 * 1024:
                    shutil.move(temp_path, file_path)
                else:
                    success = await compress_video(temp_path, file_path)
                    if not success:
                        print(f"Failed to compress {temp_path} below limit.")
                        if os.path.exists(temp_path): os.remove(temp_path)
                        return None
                    if os.path.exists(temp_path): os.remove(temp_path)
                return [file_path]
            else:
                os.remove(temp_path)
                return None

    except asyncio.TimeoutError:
        print(f"Timeout while downloading: {url}")
        return None
    except Exception as e:
        print(f"Error processing {url}: {e}")
        return None

# =====================================================================
# ДОПОЛНЕНИЕ ДЛЯ ИИ-БОТА: СЖАТИЕ БОЛЬШИХ АУДИО И СОЗДАНИЕ РАСКАДРОВКИ 2x2
# =====================================================================

async def compress_audio_for_ai(input_path: str, output_path: str = None, max_size_mb: float = 19.0) -> str | None:
    """
    Извлекает аудиодорожку из любого большого аудио- или видеофайла и сжимает её в легкий моно-MP3
    (16 kHz, 32 kbps — оптимальный формат для распознавания речи Whisper и Gemini).
    Позволяет ужать даже 60-минутный файл весом 500 МБ в ~13 МБ без потери разборчивости речи!
    """
    if not output_path:
        output_path = input_path + "_ai_compressed.mp3"

    # 16000 Гц, 1 канал (моно), битрейт 32k — стандарт для Speech-to-Text моделей
    command = [
        FFMPEG_PATH,
        "-y",
        "-i", input_path,
        "-vn",                  # Отбрасываем видеопоток (если на вход подали видеофайл)
        "-ac", "1",             # Переводим в моно (уменьшает вес в 2 раза)
        "-ar", "16000",         # Частота дискретизации 16 кГц (родная частота для Whisper)
        "-c:a", "libmp3lame",
        "-b:a", "32k",          # Сверхлегкий битрейт 32 кбит/с (1 минута весит всего ~230 КБ)
        output_path
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()

    if process.returncode != 0 or not os.path.exists(output_path):
        return None

    # Если файл длится больше 1.5 часов и даже при 32k превысил лимит — обрезаем по лимиту байт
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    if size_mb > max_size_mb:
        print(f"[АУДИО СЖАТИЕ] Файл всё ещё {size_mb:.1f} МБ > {max_size_mb} МБ, обрезаем до лимита API...")
        with open(output_path, "rb") as f:
            trimmed = f.read(int(max_size_mb * 1024 * 1024))
        with open(output_path, "wb") as f:
            f.write(trimmed)

    return output_path


async def make_video_storyboard_grid(video_path: str, output_jpg_path: str = None) -> bytes | None:
    """
    Создает из видеофайла один JPEG-коллаж 2x2 (4 ключевых кадра из разных моментов ролика).
    Равномерно берет кадры по всей длительности видео (даже если ролик идет 20 минут или 5 секунд),
    чтобы нейросеть видела суть видео и могла прочитать вшитые субтитры/надписи на экране.
    """
    if not output_jpg_path:
        output_jpg_path = video_path + "_grid.jpg"

    # Узнаем длительность видео через ffprobe, чтобы шаг между 4 кадрами был пропорционален длине ролика
    step_sec = 1.5
    try:
        info = await get_video_info(video_path)
        duration = info.get("duration", 0)
        if duration > 2.0:
            # Делим длительность на 4 части, чтобы захватить начало, середину и конец
            step_sec = max(0.5, round(duration / 4.2, 2))
    except Exception:
        pass

    vf_filter = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{step_sec})',scale=640:-1,tile=2x2"
    command = [
        FFMPEG_PATH,
        "-y",
        "-i", video_path,
        "-vf", vf_filter,
        "-frames:v", "1",
        "-q:v", "5",
        output_jpg_path
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(process.wait(), timeout=20.0)
        if os.path.exists(output_jpg_path) and os.path.getsize(output_jpg_path) > 0:
            async with aiofiles.open(output_jpg_path, "rb") as f:
                raw_bytes = await f.read()
            # Прогоняем через наш компрессор картинок на случай нестандартных пропорций
            return await compress_image(raw_bytes)
    except Exception as e:
        print(f"[КОЛЛАЖ ВИДЕО] Ошибка создания раскадровки 2x2: {e}")
    finally:
        if os.path.exists(output_jpg_path):
            try:
                os.remove(output_jpg_path)
            except Exception:
                pass

    return None

# Глобальный экземпляр MTProto-клиента для прямого скачивания больших файлов (> 20 МБ)
_mtproto_client = None
_mtproto_lock = asyncio.Lock()


async def download_large_telegram_file(file_id: str, dest_path: str = None, progress_callback=None) -> bytes | None:
    """
    Обходит жесткий лимит публичного Telegram Bot API (20 МБ) и скачивает файлы до 2000 МБ (2 ГБ)
    напрямую на сервер бота двумя способами:
    1) Если в .env задан LOCAL_BOT_API_URL (контейнер aiogram/telegram-bot-api в Portainer) —
       запрашивает getFile у локального сервера и забирает файл напрямую с общего тома (или по HTTP).
    2) Если локального контейнера нет, но заданы TELEGRAM_API_ID и TELEGRAM_API_HASH —
       подключается напрямую к дата-центру Telegram по протоколу MTProto (через kurigram/pyrogram)
       и скачивает файл по его file_id прямо внутрь контейнера бота с отображением прогресса (% и МБ)!
    """
    import time
    import config

    os.makedirs(DATA_FOLDER, exist_ok=True)
    if not dest_path:
        dest_path = os.path.join(DATA_FOLDER, f"large_{file_id[-16:]}.bin")

    # --- СПОСОБ А: Через локальный контейнер telegram-bot-api (если поднят в Portainer) ---
    if getattr(config, "LOCAL_BOT_API_URL", ""):
        try:
            api_url = f"{config.LOCAL_BOT_API_URL}/bot{config.TELEGRAM_TOKEN}/getFile"
            timeout = aiohttp.ClientTimeout(total=600)  # До 10 минут на скачивание гигабайтного файла
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, json={"file_id": file_id}) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 200 and data.get("ok"):
                        file_path = data["result"].get("file_path", "")
                        # 1. Если у контейнеров общая папка (Volume), файл уже лежит прямо на диске!
                        if file_path and os.path.exists(file_path):
                            print(f"[LOCAL BOT API] Читаю большой файл напрямую с диска: {file_path}")
                            async with aiofiles.open(file_path, "rb") as f:
                                return await f.read()
                        # 2. Иначе скачиваем его по локальной сети из контейнера telegram-bot-api
                        dl_url = f"{config.LOCAL_BOT_API_URL}/file/bot{config.TELEGRAM_TOKEN}/{file_path.lstrip('/')}"
                        async with session.get(dl_url) as dl_resp:
                            if dl_resp.status == 200:
                                return await dl_resp.read()
        except Exception as e:
            print(f"[LOCAL BOT API] Ошибка скачивания через локальный сервер: {e}")

    # --- СПОСОБ Б: Напрямую внутри Python через MTProto (kurigram / pyrogram) без второго контейнера ---
    api_id = getattr(config, "TELEGRAM_API_ID", 0)
    api_hash = getattr(config, "TELEGRAM_API_HASH", "")
    if not (api_id and api_hash):
        print("[MTPROTO] TELEGRAM_API_ID и TELEGRAM_API_HASH не заданы в .env — прямое скачивание >20 МБ недоступно.")
        return None

    global _mtproto_client
    try:
        from pyrogram import Client
    except ImportError:
        print("[MTPROTO] Библиотека kurigram/pyrogram не установлена (pip install kurigram).")
        return None

    async with _mtproto_lock:
        try:
            if _mtproto_client is None:
                session_path = os.path.join(DATA_FOLDER, "mtproto_downloader_bot")
                _mtproto_client = Client(
                    name=session_path,
                    api_id=api_id,
                    api_hash=api_hash,
                    bot_token=config.TELEGRAM_TOKEN,
                    no_updates=True,  # Не перехватываем сообщения у основного бота, работаем только как скачиватель!
                )
            if not _mtproto_client.is_connected:
                await _mtproto_client.start()

            print(f"[MTPROTO] Качаю большой файл напрямую с дата-центра Telegram в {dest_path}...")
            last_report_ts = 0.0

            # Колбэк прогресса скачивания для живого обновления плашки <tg-thinking> (каждые 3 секунды)
            async def _on_progress(current: int, total: int):
                nonlocal last_report_ts
                if not progress_callback or total <= 0:
                    return
                now = time.monotonic()
                if now - last_report_ts < 3.0 and current < total:
                    return
                last_report_ts = now
                pct = int(current * 100 / total)
                cur_mb = current / (1024 * 1024)
                tot_mb = total / (1024 * 1024)
                try:
                    await progress_callback(
                        f"📥 Качаю большой файл напрямую: {pct}% ({cur_mb:.1f} из {tot_mb:.1f} МБ)..."
                    )
                except Exception:
                    pass

            downloaded_path = await _mtproto_client.download_media(
                file_id, file_name=dest_path, progress=_on_progress
            )
            if downloaded_path and os.path.exists(downloaded_path):
                async with aiofiles.open(downloaded_path, "rb") as f:
                    file_bytes = await f.read()
                try:
                    os.remove(downloaded_path)
                except Exception:
                    pass
                print(f"[MTPROTO] Успешно скачан файл размером {format_size(len(file_bytes))} ✅")
                return file_bytes
        except Exception as e:
            print(f"[MTPROTO] Ошибка прямого скачивания большого файла: {e}")

    return None