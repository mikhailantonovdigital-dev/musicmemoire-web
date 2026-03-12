from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.storage import save_voice_file
from app.models import Order, OrderEvent, VoiceInput

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(prefix="/questionnaire", tags=["questionnaire"])

ALLOWED_STORY_SOURCES = {"text", "voice"}
ALLOWED_LYRICS_MODES = {"generate", "custom"}


def ensure_visitor_session(request: Request) -> str:
    visitor_id = request.session.get("visitor_id")
    if not visitor_id:
        visitor_id = str(uuid4())
        request.session["visitor_id"] = visitor_id
    return visitor_id


def get_current_draft(db: Session, request: Request) -> Order | None:
    visitor_id = ensure_visitor_session(request)
    draft_order_id = request.session.get("draft_order_id")

    query = db.query(Order).filter(
        Order.session_id == visitor_id,
        Order.status == "draft",
    )

    if draft_order_id:
        order = query.filter(Order.id == int(draft_order_id)).first()
        if order:
            return order

    return query.order_by(Order.id.desc()).first()


def get_latest_voice_input(db: Session, order_id: int) -> VoiceInput | None:
    return (
        db.query(VoiceInput)
        .filter(VoiceInput.order_id == order_id)
        .order_by(VoiceInput.id.desc())
        .first()
    )


def format_size(size_bytes: int | None) -> str | None:
    if size_bytes is None:
        return None
    return f"{size_bytes / (1024 * 1024):.2f} МБ"


@router.get("/", response_class=HTMLResponse)
async def questionnaire_start(request: Request, db: Session = Depends(get_db)):
    draft = get_current_draft(db, request)

    return templates.TemplateResponse(
        "questionnaire/start.html",
        {
            "request": request,
            "page_title": "Анкета заказа",
            "draft": draft,
            "error": None,
        },
    )


@router.post("/start", response_class=HTMLResponse)
async def questionnaire_start_submit(
    request: Request,
    story_source: str = Form(...),
    lyrics_mode: str = Form(...),
    db: Session = Depends(get_db),
):
    story_source = story_source.strip().lower()
    lyrics_mode = lyrics_mode.strip().lower()

    if story_source not in ALLOWED_STORY_SOURCES or lyrics_mode not in ALLOWED_LYRICS_MODES:
        draft = get_current_draft(db, request)
        return templates.TemplateResponse(
            "questionnaire/start.html",
            {
                "request": request,
                "page_title": "Анкета заказа",
                "draft": draft,
                "error": "Пожалуйста, выбери корректные варианты.",
                "form_story_source": story_source,
                "form_lyrics_mode": lyrics_mode,
            },
            status_code=400,
        )

    visitor_id = ensure_visitor_session(request)
    draft = get_current_draft(db, request)

    if draft is None:
        draft = Order(
            session_id=visitor_id,
            status="draft",
        )
        db.add(draft)
        db.flush()

    draft.story_source = story_source
    draft.lyrics_mode = lyrics_mode

    db.add(
        OrderEvent(
            order=draft,
            event_type="questionnaire_started",
            payload={
                "story_source": story_source,
                "lyrics_mode": lyrics_mode,
            },
        )
    )

    db.commit()
    db.refresh(draft)

    request.session["draft_order_id"] = draft.id

    return RedirectResponse(
        url=request.url_for("questionnaire_story"),
        status_code=303,
    )


@router.get("/story", response_class=HTMLResponse)
async def questionnaire_story(request: Request, db: Session = Depends(get_db)):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    latest_voice = get_latest_voice_input(db, draft.id)
    saved = request.query_params.get("saved") == "1"
    voice_uploaded = request.query_params.get("voice_uploaded") == "1"

    return templates.TemplateResponse(
        "questionnaire/story.html",
        {
            "request": request,
            "page_title": "Анкета — история",
            "draft": draft,
            "latest_voice": latest_voice,
            "latest_voice_size": format_size(latest_voice.size_bytes) if latest_voice else None,
            "saved": saved,
            "voice_uploaded": voice_uploaded,
            "error": None,
        },
    )


@router.post("/voice-upload", response_class=HTMLResponse)
async def questionnaire_voice_upload(
    request: Request,
    voice_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if draft.story_source != "voice" or draft.lyrics_mode == "custom":
        return RedirectResponse(url=request.url_for("questionnaire_story"), status_code=303)

    latest_voice = get_latest_voice_input(db, draft.id)

    try:
        stored = save_voice_file(voice_file)
    except ValueError as exc:
        return templates.TemplateResponse(
            "questionnaire/story.html",
            {
                "request": request,
                "page_title": "Анкета — история",
                "draft": draft,
                "latest_voice": latest_voice,
                "latest_voice_size": format_size(latest_voice.size_bytes) if latest_voice else None,
                "saved": False,
                "voice_uploaded": False,
                "error": str(exc),
            },
            status_code=400,
        )

    voice_input = VoiceInput(
        order_id=draft.id,
        original_filename=stored.original_filename,
        content_type=stored.content_type,
        storage_path=stored.absolute_path,
        relative_path=stored.relative_path,
        size_bytes=stored.size_bytes,
        transcription_status="uploaded",
    )
    db.add(voice_input)
    db.flush()

    db.add(
        OrderEvent(
            order=draft,
            event_type="voice_uploaded",
            payload={
                "voice_input_id": voice_input.public_id,
                "size_bytes": voice_input.size_bytes,
                "content_type": voice_input.content_type,
            },
        )
    )

    db.commit()

    return RedirectResponse(
        url=f"{request.url_for('questionnaire_story')}?voice_uploaded=1",
        status_code=303,
    )


@router.get("/voice/{voice_public_id}")
async def questionnaire_voice_stream(
    voice_public_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    visitor_id = ensure_visitor_session(request)

    voice_input = (
        db.query(VoiceInput)
        .join(Order, VoiceInput.order_id == Order.id)
        .filter(
            VoiceInput.public_id == voice_public_id,
            Order.session_id == visitor_id,
        )
        .first()
    )

    if voice_input is None:
        raise HTTPException(status_code=404, detail="Файл не найден.")

    file_path = Path(voice_input.storage_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден на диске.")

    return FileResponse(
        path=file_path,
        media_type=voice_input.content_type,
        filename=voice_input.original_filename or file_path.name,
    )


@router.post("/story", response_class=HTMLResponse)
async def questionnaire_story_submit(
    request: Request,
    story_text: str = Form(default=""),
    transcript_text: str = Form(default=""),
    custom_lyrics_text: str = Form(default=""),
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    latest_voice = get_latest_voice_input(db, draft.id)
    error = None

    if draft.lyrics_mode == "custom":
        value = custom_lyrics_text.strip()
        if not value:
            error = "Вставь готовый текст песни."
        else:
            draft.custom_lyrics_text = value

    else:
        if draft.story_source == "voice":
            value = transcript_text.strip()
            if not value and latest_voice is None:
                error = "Загрузи голосовое или вставь расшифровку вручную."
            else:
                draft.transcript_text = value or draft.transcript_text
        else:
            value = story_text.strip()
            if not value:
                error = "Напиши историю для песни."
            else:
                draft.story_text = value

    if error:
        return templates.TemplateResponse(
            "questionnaire/story.html",
            {
                "request": request,
                "page_title": "Анкета — история",
                "draft": draft,
                "latest_voice": latest_voice,
                "latest_voice_size": format_size(latest_voice.size_bytes) if latest_voice else None,
                "saved": False,
                "voice_uploaded": False,
                "error": error,
            },
            status_code=400,
        )

    db.add(
        OrderEvent(
            order=draft,
            event_type="story_saved",
            payload={
                "story_source": draft.story_source,
                "lyrics_mode": draft.lyrics_mode,
                "has_voice_upload": latest_voice is not None,
                "has_transcript_text": bool((draft.transcript_text or "").strip()),
            },
        )
    )

    db.commit()

    return RedirectResponse(
        url=f"{request.url_for('questionnaire_story')}?saved=1",
        status_code=303,
    )
