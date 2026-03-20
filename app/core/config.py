from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: str = "development"
    APP_NAME: str = "Magic Music"
    BASE_URL: str = "https://magic-music.ru"
    SESSION_SECRET: str = "change-me"

    DATABASE_URL: str = "sqlite:///./dev.db"
    REDIS_URL: str = "redis://localhost:6379/0"

    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str | None = None
    OPENAI_TRANSCRIBE_MODEL: str = "gpt-4o-mini-transcribe"
    AUDIO_TRANSCRIBE_LANGUAGE: str = "ru"

    SUNO_API_KEY: str | None = None
    SUNO_API_BASE_URL: str = "https://api.sunoapi.org"
    SUNO_MODEL: str = "V5"
    SUNO_STUB_MODE: bool = True
    SUNO_STUB_DELAY_SECONDS: int = 12
    SUNO_STUB_AUDIO_URL: str | None = None
    SUNO_REQUEST_TIMEOUT_SECONDS: int = 60
    SUNO_CALLBACK_TOKEN: str | None = None

    YOOKASSA_SHOP_ID: str | None = None
    YOOKASSA_SECRET_KEY: str | None = None
    YOOKASSA_TAX_SYSTEM_CODE: str | None = None
    YOOKASSA_VAT_CODE: str | None = None
    YOOKASSA_RECEIPT_EMAIL: str | None = None

    METRICA_COUNTER_ID: str | None = None

    SUPPORT_TG_URL: str = "https://t.me/mikhailantonov19"
    SUPPORT_MAX_URL: str = "https://max.ru/u/f9LHodD0cOKg36L-baFKeBJquxSx5xydupa2AYOKdl7BUFipfVYS5FVVV80"

    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_REPORT_CHAT_ID: str | None = None

    TELEGRAM_BOT_WEBHOOK_SECRET: str | None = None

    PRICE_RUB: int = 990
    UPLOADS_DIR: str = "/var/data/musicmemoire/uploads"
    MAX_VOICE_FILE_MB: int = 25
    MAX_SONG_FILE_MB: int = 80
    MAX_SUPPORT_FILE_MB: int = 15

    OBJECT_STORAGE_BUCKET: str | None = None
    OBJECT_STORAGE_ENDPOINT_URL: str | None = None
    OBJECT_STORAGE_REGION: str | None = None
    OBJECT_STORAGE_ACCESS_KEY_ID: str | None = None
    OBJECT_STORAGE_SECRET_ACCESS_KEY: str | None = None
    OBJECT_STORAGE_PREFIX: str = "musicmemoire"
    OBJECT_STORAGE_FORCE_PATH_STYLE: bool = False

    SMTP_HOST: str | None = None
    SMTP_PORT: int = 465
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str | None = None
    SMTP_FROM_NAME: str = "Magic Music"
    SMTP_TIMEOUT_SECONDS: int = 20

    MAGIC_LINK_TTL_MINUTES: int = 30

    MAGIC_LINK_EMAIL_LIMIT_PER_HOUR: int = 5
    MAGIC_LINK_IP_LIMIT_PER_HOUR: int = 12
    QUESTIONNAIRE_MAGIC_LINK_ORDER_LIMIT_PER_HOUR: int = 3
    VOICE_UPLOAD_LIMIT_PER_ORDER_PER_DAY: int = 5
    VOICE_RETRANSCRIBE_LIMIT_PER_ORDER_PER_HOUR: int = 3
    SONG_START_LIMIT_PER_ORDER_PER_DAY: int = 6
    MAGIC_LINK_STUB_MODE: bool = True

    BACKGROUND_QUEUE_NAME: str = "default"
    BACKGROUND_JOBS_SYNC_MODE: bool = False

    ADMIN_TOKEN: str | None = None


settings = Settings()
