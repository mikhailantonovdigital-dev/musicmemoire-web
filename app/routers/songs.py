from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import get_session_user, utcnow
from app.core.templates import templates
from app.models import Order, OrderEvent, SongGeneration
from app.services.song_workflow import (
    RUNNING_SONG_STATUSES,
    create_song_job,
    get_latest_song,
    humanize_song_status,
    process_song_callback,
    sync_song_job_state,
)
from app.services.suno_service import SunoServiceError

router = APIRouter(prefix="/songs", tags=["songs"])


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


@router.post("/callback/suno")
async def suno_callback(request: Request, db: Session = Depends(get_db)):
    expected_token = (settings.SUNO_CALLBACK_TOKEN or "").strip()
    actual_token = (request.query_params.get("token") or "").strip()
    if expected_token and actual_token != expected_token:
        raise HTTPException(status_code=403, detail="Некорректный callback token.")

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался JSON-объект.")

    song = process_song_callback(db, payload)
    db.commit()

    if song is None:
        return JSONResponse({"status": "ignored"})

    return JSONResponse(
        {
            "status": "ok",
            "song_job_id": song.public_id,
            "song_status": song.status,
        }
    )


@router.post("/start/{order_public_id}")
async def song_start(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    order = get_song_order(request, db, order_public_id)
    if order is None:
        raise HTTPException(status_code=403, detail="Нет доступа к заказу.")

    try:
        song = create_song_job(db, order)
        db.commit()
        db.refresh(song)
    except SunoServiceError as exc:
        db.rollback()
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

    return RedirectResponse(url=f"/songs/status?job={song.public_id}", status_code=303)


@router.get("/status", response_class=HTMLResponse)
async def song_status(job: str, request: Request, db: Session = Depends(get_db)):
    song = get_song_job(request, db, job)
    if song is None:
        raise HTTPException(status_code=404, detail="Задача генерации не найдена.")

    error = None
    if song.status in RUNNING_SONG_STATUSES:
        try:
            song = sync_song_job_state(db, song)
            db.commit()
            db.refresh(song)
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
        db.commit()
        db.refresh(new_song)
    except SunoServiceError as exc:
        db.rollback()
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
