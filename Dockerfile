FROM python:3.12-slim
WORKDIR /app
# Копируем зависимости и устанавливаем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Запускаем скрипт c отключением буферизации логов
CMD ["python", "-u", "main.py"]