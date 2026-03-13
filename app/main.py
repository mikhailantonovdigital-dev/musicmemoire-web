from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import settings
from app.core.db import init_db
from app.core.storage import ensure_storage_dirs
from app.routers import public, questionnaire, account, admin, songs


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_storage_dirs()
    init_db()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    lifespan=lifespan,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET,
    same_site="lax",
    https_only=settings.APP_ENV == "production",
    max_age=60 * 60 * 24 * 30,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(public.router)
app.include_router(questionnaire.router)
app.include_router(account.router)
app.include_router(songs.router)
app.include_router(admin.router)
