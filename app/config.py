from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    BOT_TOKEN: str = "placeholder_token"
    # Telegram Bot token

    # Основной LLM (можно менять в .env без правки кода)
    LLM_API_KEY: str = "sk-placeholder"
    LLM_API_BASE: str = "https://api.proxyapi.ru/openai/v1"
    LLM_MODEL_NAME: str = "openai/gpt-4.1-mini"

    # ProxyAPI (оставляем для Whisper голосовых сообщений)
    PROXY_API_KEY: str = "sk-placeholder"
    PROXY_API_BASE: str = "https://api.proxyapi.ru/openai/v1"

    DB_URL: str = "sqlite+aiosqlite:///pocket_cfo.db"

    model_config = SettingsConfigDict(env_file=".env")

settings = Settings()
