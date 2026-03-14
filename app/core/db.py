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

            if "song_style" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN song_style VARCHAR(32)"))

            if "song_style_custom" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN song_style_custom TEXT"))

            if "singer_gender" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN singer_gender VARCHAR(16)"))

            if "song_style" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN song_style VARCHAR(32)"))

            if "song_style_custom" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN song_style_custom TEXT"))

            if "singer_gender" not in order_columns:
                conn.execute(text("ALTER TABLE orders ADD COLUMN singer_gender VARCHAR(16)"))

    if "magic_login_tokens" not in inspector.get_table_names():
        # table creation will be handled by create_all below, this branch is only for clarity
        pass


def init_db() -> None:
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    run_bootstrap_migrations()
