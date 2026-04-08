from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import settings
from app.core.db import init_db
from app.core.storage import ensure_storage_dirs
from app.core.templates import templates
from app.routers import public, questionnaire, account, admin, songs, checkout, telegram
from app.services.telegram_report_service import register_telegram_webhook


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_storage_dirs()
    init_db()

    webhook_result = register_telegram_webhook()
    if webhook_result.ok:
        logger.info(webhook_result.detail)
    else:
        logger.warning("Telegram webhook registration skipped/failed: %s", webhook_result.detail)

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
app.mount("/uploads", StaticFiles(directory=settings.UPLOADS_DIR), name="uploads")

app.include_router(public.router)
app.include_router(questionnaire.router)
app.include_router(account.router)
app.include_router(checkout.router)
app.include_router(songs.router)
app.include_router(admin.router)
app.include_router(telegram.router)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    base_url = settings.BASE_URL.rstrip("/")
    canonical_url = f"{base_url}{request.url.path}"
    meta_description = (
        "Страница не найдена. Вернитесь на главную Magic Music, в портфолио "
        "или сразу к созданию персональной песни."
    )

    return templates.TemplateResponse(
        "public/404.html",
        {
            "request": request,
            "page_title": "Страница не найдена — Magic Music",
            "meta_description": meta_description,
            "meta_robots": "noindex,follow",
            "canonical_url": canonical_url,
            "og_title": "Страница не найдена — Magic Music",
            "og_description": meta_description,
            "og_type": "website",
            "og_url": canonical_url,
            "og_image": f"{base_url}/static/img/hero-gift-song.jpg",
            "twitter_card": "summary_large_image",
            "body_class": "page-not-found",
        },
        status_code=404,
    )
