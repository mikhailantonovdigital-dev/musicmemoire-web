from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


def normalize_database_url(raw_url: str) -> str:
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql+psycopg://", 1)

    if raw_url.startswith("postgresql://") and not raw_url.startswith("postgresql+psycopg://"):
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return raw_url


DATABASE_URL = normalize_database_url(settings.DATABASE_URL)

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_bootstrap_migrations() -> None:
    inspector = inspect(engine)

    if "orders" in inspector.get_table_names():
        order_columns = {col["name"] for col in inspector.get_columns("orders")}

        with engine.begin() as conn:
            if "final_lyrics_text" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN final_lyrics_text TEXT"))

            if "title" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN title VARCHAR(255)"))

            if "song_style" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN song_style VARCHAR(32)"))

            if "song_style_custom" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN song_style_custom TEXT"))

            if "singer_gender" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN singer_gender VARCHAR(16)"))

            if "song_mood" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN song_mood VARCHAR(32)"))

    if "song_generations" in inspector.get_table_names():
        song_columns = {col["name"] for col in inspector.get_columns("song_generations")}

        with engine.begin() as conn:
            if "result_tracks" not in song_columns:
                conn.execute(text("ALTER TABLE song_generations ADD COLUMN result_tracks JSON"))

    if "voice_inputs" in inspector.get_table_names():
        voice_columns = {col["name"] for col in inspector.get_columns("voice_inputs")}

        with engine.begin() as conn:
            if "storage_backend" not in voice_columns:
                conn.execute(text("ALTER TABLE voice_inputs ADD COLUMN storage_backend VARCHAR(20)"))
            if "storage_bucket" not in voice_columns:
                conn.execute(text("ALTER TABLE voice_inputs ADD COLUMN storage_bucket VARCHAR(255)"))
            if "storage_key" not in voice_columns:
                conn.execute(text("ALTER TABLE voice_inputs ADD COLUMN storage_key VARCHAR(1024)"))


    if "support_messages" in inspector.get_table_names():
        support_columns = {col["name"] for col in inspector.get_columns("support_messages")}

        with engine.begin() as conn:
            if "attachment_original_filename" not in support_columns:
                conn.execute(text("ALTER TABLE support_messages ADD COLUMN attachment_original_filename VARCHAR(255)"))
            if "attachment_content_type" not in support_columns:
                conn.execute(text("ALTER TABLE support_messages ADD COLUMN attachment_content_type VARCHAR(255)"))
            if "attachment_size_bytes" not in support_columns:
                conn.execute(text("ALTER TABLE support_messages ADD COLUMN attachment_size_bytes INTEGER"))
            if "attachment_relative_path" not in support_columns:
                conn.execute(text("ALTER TABLE support_messages ADD COLUMN attachment_relative_path VARCHAR(1024)"))
            if "attachment_storage_backend" not in support_columns:
                conn.execute(text("ALTER TABLE support_messages ADD COLUMN attachment_storage_backend VARCHAR(20)"))
            if "attachment_storage_bucket" not in support_columns:
                conn.execute(text("ALTER TABLE support_messages ADD COLUMN attachment_storage_bucket VARCHAR(255)"))
            if "attachment_storage_key" not in support_columns:
                conn.execute(text("ALTER TABLE support_messages ADD COLUMN attachment_storage_key VARCHAR(1024)"))

    if "magic_login_tokens" not in inspector.get_table_names():
        pass


def init_db() -> None:
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    run_bootstrap_migrations()
