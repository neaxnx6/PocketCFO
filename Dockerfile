FROM python:3.12-slim

# Настройка переменных окружения Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# Установка системных зависимостей (если нужны для компиляции некоторых пакетов)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Копирование файла зависимостей
COPY requirements.txt .

# Установка зависимостей (если нет requirements.txt, используем pip freeze или pyproject.toml)
# Поскольку у нас pyproject.toml:
COPY pyproject.toml .
RUN pip install --upgrade pip && pip install setuptools wheel && pip install .

# Копирование исходного кода проекта
COPY . .

# Команда запуска
CMD ["python", "app/main.py"]
