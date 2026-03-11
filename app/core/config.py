from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: str = "development"
    APP_NAME: str = "Music Memoire"
    APP_DOMAIN: str = "http://127.0.0.1:8000"
    BASE_URL: str = "http://127.0.0.1:8000"
    SESSION_SECRET: str = "change-me"

    DATABASE_URL: str = "sqlite:///./dev.db"
    REDIS_URL: str = "redis://localhost:6379/0"

    YOOKASSA_SHOP_ID: str | None = None
    YOOKASSA_SECRET_KEY: str | None = None
    YOOKASSA_TAX_SYSTEM_CODE: str | None = None
    YOOKASSA_VAT_CODE: str | None = None
    YOOKASSA_RECEIPT_EMAIL: str | None = None

    SUNO_API_KEY: str | None = None
    SUNO_MODEL: str | None = None

    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str | None = None

    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL_PRIMARY: str | None = None
    GEMINI_MODEL_FALLBACK: str | None = None

    METRICA_COUNTER_ID: str | None = None
    METRICA_TOKEN: str | None = None

    SUPPORT_TG_URL: str = "https://t.me/mikhailantonov19"
    SUPPORT_MAX_URL: str = "https://max.ru/u/f9LHodD0cOKg36L-baFKeBJquxSx5xydupa2AYOKdl7BUFipfVYS5FVVV80"

    PRICE_RUB: int = 4900


settings = Settings()
