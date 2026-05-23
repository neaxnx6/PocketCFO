FROM python:3.12-slim

# Настройка переменных окружения Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# Установка системных зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Копирование файла зависимостей
COPY pyproject.toml .

# Установка зависимостей
RUN pip install --upgrade pip && pip install setuptools wheel && pip install .

# Копирование исходного кода проекта
COPY . .

# Команда запуска
CMD ["python", "app/main.py"]
