from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import get_session_user
from app.core.templates import templates
from app.models import Order, OrderEvent, SongGeneration
from app.services.song_workflow import (
    RUNNING_SONG_STATUSES,
    create_song_job,
    ensure_song_track_cached,
    get_latest_ready_song,
    get_latest_song,
    get_song_track_entry,
    get_song_track_storage_path,
    humanize_song_status,
    process_song_callback,
    sync_song_job_state,
)
from app.services.suno_service import SunoServiceError

router = APIRouter(prefix="/songs", tags=["songs"])


def has_admin_access(request: Request) -> bool:
    return bool(request.session.get("admin_access"))


def get_song_order(request: Request, db: Session, order_public_id: str) -> Order | None:
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        return None

    if has_admin_access(request):
        return order

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


@router.get("/file/{job_public_id}/{track_index}", name="song_track_file")
async def song_track_file(job_public_id: str, track_index: int, request: Request, download: int = 0, db: Session = Depends(get_db)):
    song = get_song_job(request, db, job_public_id)
    if song is None:
        raise HTTPException(status_code=404, detail="Аудиофайл не найден.")

    if track_index < 0:
        raise HTTPException(status_code=404, detail="Аудиофайл не найден.")

    try:
        ensure_song_track_cached(db, song, track_index, source="song_file_route")
        db.commit()
        db.refresh(song)
    except Exception:
        db.rollback()
        song = get_song_job(request, db, job_public_id)
        if song is None:
            raise HTTPException(status_code=404, detail="Аудиофайл не найден.")

    track = get_song_track_entry(song, track_index)
    if track is None:
        raise HTTPException(status_code=404, detail="Аудиофайл не найден.")

    stored_path = get_song_track_storage_path(song, track_index)
    if stored_path is not None:
        media_type = (track.get("stored_content_type") or "audio/mpeg").strip() or "audio/mpeg"
        filename = track.get("stored_original_filename") or f"{song.order.order_number}-track-{track_index + 1}.mp3"
        if download:
            return FileResponse(stored_path, media_type=media_type, filename=filename)
        return FileResponse(stored_path, media_type=media_type)

    remote_url = (track.get("audio_url") or track.get("stream_audio_url") or "").strip()
    if remote_url:
        return RedirectResponse(url=remote_url, status_code=307)

    raise HTTPException(status_code=404, detail="Аудиофайл не найден.")


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
    auto_refresh_enabled = song.status in RUNNING_SONG_STATUSES
    if song.status in RUNNING_SONG_STATUSES:
        try:
            song = sync_song_job_state(db, song)
            db.commit()
            db.refresh(song)
        except SunoServiceError as exc:
            error = str(exc)
            auto_refresh_enabled = False
            db.rollback()

            song = get_song_job(request, db, job)
            if song is None:
                raise HTTPException(status_code=404, detail="Задача генерации не найдена.")

            db.add(
                OrderEvent(
                    order=song.order,
                    event_type="song_generation_status_sync_failed",
                    payload={
                        "song_job_id": song.public_id,
                        "external_job_id": song.external_job_id,
                        "error": error,
                        "source": "status_page",
                    },
                )
            )
            db.commit()
            db.refresh(song)

    latest_ready_song = get_latest_ready_song(song.order)
    fallback_ready_song = None
    if latest_ready_song is not None and latest_ready_song.public_id != song.public_id:
        fallback_ready_song = latest_ready_song

    return templates.TemplateResponse(
        "songs/status.html",
        {
            "request": request,
            "page_title": "Генерация песни",
            "order": song.order,
            "song": song,
            "fallback_ready_song": fallback_ready_song,
            "song_status_label": humanize_song_status(song.status),
            "fallback_song_status_label": humanize_song_status(fallback_ready_song.status if fallback_ready_song else None),
            "is_ready": song.status == "succeeded",
            "is_running": song.status in RUNNING_SONG_STATUSES,
            "is_failed": song.status == "failed",
            "error": error,
            "auto_refresh_enabled": auto_refresh_enabled and song.status in RUNNING_SONG_STATUSES,
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
