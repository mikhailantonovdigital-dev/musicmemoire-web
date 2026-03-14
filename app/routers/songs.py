from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import utcnow
from app.models import Order, OrderEvent, SongGeneration, User
from app.services.suno_service import SunoServiceError, start_song_generation, sync_song_generation

router = APIRouter(prefix="/songs", tags=["songs"])
templates = Jinja2Templates(directory="app/templates")

RUNNING_SONG_STATUSES = {"queued", "processing"}


def get_session_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("account_user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == int(user_id)).first()


def get_song_order(request: Request, db: Session, order_public_id: str) -> Order | None:
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        return None

    draft_order_id = request.session.get("draft_order_id")
    if draft_order_id and int(draft_order_id) == order.id:
        return order

    user = get_session_user(request, db)
    if user and order.user_id == user.id:
        return order

    return None


def get_song_job(request: Request, db: Session, job_public_id: str) -> SongGeneration | None:
    job = db.query(SongGeneration).filter(SongGeneration.public_id == job_public_id).first()
    if job is None:
        return None

    order = get_song_order(request, db, job.order.public_id)
    if order is None:
        return None

    return job


def humanize_song_status(status: str | None) -> str:
    mapping = {
        "queued": "В очереди",
        "processing": "Генерируется",
        "succeeded": "Готово",
        "failed": "Ошибка",
        "canceled": "Отменено",
    }
    return mapping.get(status or "", "—")


def get_latest_song(order: Order) -> SongGeneration | None:
    if not order.song_generations:
        return None
    return sorted(order.song_generations, key=lambda item: item.id or 0, reverse=True)[0]


def has_successful_payment(order: Order) -> bool:
    return any(payment.status == "succeeded" for payment in order.payments)


def can_start_song(order: Order) -> bool:
    return has_successful_payment(order)


def create_song_job(db: Session, order: Order) -> SongGeneration:
    latest_song = get_latest_song(order)
    if latest_song and latest_song.status in RUNNING_SONG_STATUSES:
        return latest_song

    lyrics_text = (order.final_lyrics_text or "").strip()
    if not lyrics_text:
        raise SunoServiceError("Сначала нужен финальный текст песни.")

    if order.user_id is None:
        raise SunoServiceError("Сначала нужно привязать заказ к email и кабинету.")

    if not can_start_song(order):
        raise SunoServiceError("Генерация песни станет доступна после оплаты.")

    attempt_no = (latest_song.attempt_no + 1) if latest_song else 1

    song = SongGeneration(
        order_id=order.id,
        user_id=order.user_id,
        provider="suno",
        status="queued",
        attempt_no=attempt_no,
        lyrics_text_snapshot=lyrics_text,
    )
    db.add(song)
    db.flush()

    result = start_song_generation(order_number=order.order_number, lyrics_text=lyrics_text)

    song.external_job_id = result.external_job_id
    song.status = result.status
    song.started_at = utcnow()
    song.raw_payload = result.raw
    song.error_message = None

    order.status = "song_pending"

    db.add(
        OrderEvent(
            order=order,
            event_type="song_generation_started",
            payload={
                "song_job_id": song.public_id,
                "attempt_no": song.attempt_no,
                "provider": song.provider,
                "external_job_id": song.external_job_id,
            },
        )
    )
    db.commit()
    db.refresh(song)
    return song


def sync_song_job_state(db: Session, song: SongGeneration) -> SongGeneration:
    if song.status not in RUNNING_SONG_STATUSES:
        return song

    result = sync_song_generation(
        external_job_id=song.external_job_id,
        started_at=song.started_at,
    )

    previous_status = song.status
    song.status = result.status
    song.audio_url = result.audio_url
    song.error_message = result.error_message
    song.raw_payload = result.raw

    if result.status == "succeeded":
        if song.finished_at is None:
            song.finished_at = utcnow()
        song.order.status = "song_ready"
    elif result.status == "failed":
        if song.finished_at is None:
            song.finished_at = utcnow()
        song.order.status = "song_failed"
    else:
        song.order.status = "song_pending"

    if previous_status != song.status:
        db.add(
            OrderEvent(
                order=song.order,
                event_type="song_generation_status_changed",
                payload={
                    "song_job_id": song.public_id,
                    "status_from": previous_status,
                    "status_to": song.status,
                },
            )
        )

    db.commit()
    db.refresh(song)
    return song


@router.post("/start/{order_public_id}")
async def song_start(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    order = get_song_order(request, db, order_public_id)
    if order is None:
        raise HTTPException(status_code=403, detail="Нет доступа к заказу.")

    try:
        song = create_song_job(db, order)
    except SunoServiceError as exc:
        latest_song = get_latest_song(order)
        return templates.TemplateResponse(
            "songs/status.html",
            {
                "request": request,
                "page_title": "Генерация песни",
                "order": order,
                "song": latest_song,
                "song_status_label": humanize_song_status(latest_song.status if latest_song else None),
                "is_ready": False,
                "is_running": False,
                "is_failed": True,
                "error": str(exc),
                "suno_stub_mode": settings.SUNO_STUB_MODE,
            },
            status_code=400,
        )

    return RedirectResponse(
        url=f"/songs/status?job={song.public_id}",
        status_code=303,
    )


@router.get("/status", response_class=HTMLResponse)
async def song_status(job: str, request: Request, db: Session = Depends(get_db)):
    song = get_song_job(request, db, job)
    if song is None:
        raise HTTPException(status_code=404, detail="Задача генерации не найдена.")

    error = None
    if song.status in RUNNING_SONG_STATUSES:
        try:
            song = sync_song_job_state(db, song)
        except SunoServiceError as exc:
            error = str(exc)
            song.status = "failed"
            song.error_message = str(exc)
            song.finished_at = utcnow()
            song.order.status = "song_failed"
            db.add(
                OrderEvent(
                    order=song.order,
                    event_type="song_generation_failed",
                    payload={
                        "song_job_id": song.public_id,
                        "error": str(exc),
                    },
                )
            )
            db.commit()
            db.refresh(song)

    return templates.TemplateResponse(
        "songs/status.html",
        {
            "request": request,
            "page_title": "Генерация песни",
            "order": song.order,
            "song": song,
            "song_status_label": humanize_song_status(song.status),
            "is_ready": song.status == "succeeded",
            "is_running": song.status in RUNNING_SONG_STATUSES,
            "is_failed": song.status == "failed",
            "error": error,
            "suno_stub_mode": settings.SUNO_STUB_MODE,
        },
    )


@router.post("/retry/{job_public_id}")
async def song_retry(job_public_id: str, request: Request, db: Session = Depends(get_db)):
    song = get_song_job(request, db, job_public_id)
    if song is None:
        raise HTTPException(status_code=404, detail="Задача генерации не найдена.")

    if song.status in RUNNING_SONG_STATUSES:
        return RedirectResponse(url=f"/songs/status?job={song.public_id}", status_code=303)

    try:
        new_song = create_song_job(db, song.order)
    except SunoServiceError as exc:
        return templates.TemplateResponse(
            "songs/status.html",
            {
                "request": request,
                "page_title": "Генерация песни",
                "order": song.order,
                "song": song,
                "song_status_label": humanize_song_status(song.status),
                "is_ready": False,
                "is_running": False,
                "is_failed": True,
                "error": str(exc),
                "suno_stub_mode": settings.SUNO_STUB_MODE,
            },
            status_code=400,
        )

    return RedirectResponse(url=f"/songs/status?job={new_song.public_id}", status_code=303)
