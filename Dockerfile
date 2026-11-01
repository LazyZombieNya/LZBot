FROM python:3.12-slim
WORKDIR /app
# Установка ffmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Копируем зависимости, ставим gcc для сборки tgcrypto и сразу удаляем gcc после установки
COPY requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y --auto-remove gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

COPY . .

# Запускаем скрипт c отключением буферизации логов
CMD ["python", "-u", "main.py"]