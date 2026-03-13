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
from app.services.transcription_service import (
    TranscriptionServiceError,
    transcribe_audio_file,
)

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


def humanize_transcription_status(status: str | None) -> str:
    mapping = {
        "uploaded": "Загружено",
        "transcribing": "Распознаём",
        "done": "Расшифровано",
        "failed": "Ошибка распознавания",
    }
    return mapping.get(status or "", "—")


async def run_transcription_for_voice(
    db: Session,
    request: Request,
    draft: Order,
    voice_input: VoiceInput,
) -> bool:
    voice_input.transcription_status = "transcribing"

    db.add(
        OrderEvent(
            order=draft,
            event_type="voice_transcription_started",
            payload={"voice_input_id": voice_input.public_id},
        )
    )
    db.commit()

    try:
        result = await transcribe_audio_file(voice_input.storage_path)
    except TranscriptionServiceError as exc:
        voice_input.transcription_status = "failed"
        db.add(
            OrderEvent(
                order=draft,
                event_type="voice_transcription_failed",
                payload={
                    "voice_input_id": voice_input.public_id,
                    "error": str(exc),
                },
            )
        )
        db.commit()
        request.session["voice_transcription_error"] = str(exc)
        return False

    voice_input.transcription_status = "done"
    voice_input.transcript_text = result.text
    draft.transcript_text = result.text

    db.add(
        OrderEvent(
            order=draft,
            event_type="voice_transcription_done",
            payload={
                "voice_input_id": voice_input.public_id,
                "model": result.model,
                "language": result.language,
                "chars": len(result.text),
            },
        )
    )
    db.commit()
    return True


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
    lyrics_mode: str = Form(...),
    db: Session = Depends(get_db),
):
    lyrics_mode = lyrics_mode.strip().lower()

    if lyrics_mode not in ALLOWED_LYRICS_MODES:
        draft = get_current_draft(db, request)
        return templates.TemplateResponse(
            "questionnaire/start.html",
            {
                "request": request,
                "page_title": "Анкета заказа",
                "draft": draft,
                "error": "Пожалуйста, выбери один вариант.",
                "form_lyrics_mode": lyrics_mode,
            },
            status_code=400,
        )

    visitor_id = ensure_visitor_session(request)
    draft = get_current_draft(db, request)

    if draft is None:
        draft = Order(session_id=visitor_id, status="draft")
        db.add(draft)
        db.flush()

    draft.lyrics_mode = lyrics_mode

    db.add(
        OrderEvent(
            order=draft,
            event_type="lyrics_mode_selected",
            payload={"lyrics_mode": lyrics_mode},
        )
    )

    db.commit()
    db.refresh(draft)
    request.session["draft_order_id"] = draft.id

    if lyrics_mode == "custom":
        return RedirectResponse(
            url=request.url_for("questionnaire_custom_text"),
            status_code=303,
        )

    return RedirectResponse(
        url=request.url_for("questionnaire_story_source"),
        status_code=303,
    )


@router.get("/story-source", response_class=HTMLResponse)
async def questionnaire_story_source(request: Request, db: Session = Depends(get_db)):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if draft.lyrics_mode == "custom":
        return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)

    return templates.TemplateResponse(
        "questionnaire/story_source.html",
        {
            "request": request,
            "page_title": "Анкета — как рассказать историю",
            "draft": draft,
            "error": None,
        },
    )


@router.post("/story-source", response_class=HTMLResponse)
async def questionnaire_story_source_submit(
    request: Request,
    story_source: str = Form(...),
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    story_source = story_source.strip().lower()

    if story_source not in ALLOWED_STORY_SOURCES:
        return templates.TemplateResponse(
            "questionnaire/story_source.html",
            {
                "request": request,
                "page_title": "Анкета — как рассказать историю",
                "draft": draft,
                "error": "Пожалуйста, выбери один вариант.",
                "form_story_source": story_source,
            },
            status_code=400,
        )

    draft.story_source = story_source

    db.add(
        OrderEvent(
            order=draft,
            event_type="story_source_selected",
            payload={"story_source": story_source},
        )
    )
    db.commit()

    return RedirectResponse(
        url=request.url_for("questionnaire_story"),
        status_code=303,
    )


@router.get("/custom-text", response_class=HTMLResponse)
async def questionnaire_custom_text(request: Request, db: Session = Depends(get_db)):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if draft.lyrics_mode != "custom":
        return RedirectResponse(url=request.url_for("questionnaire_story_source"), status_code=303)

    saved = request.query_params.get("saved") == "1"

    return templates.TemplateResponse(
        "questionnaire/custom_text.html",
        {
            "request": request,
            "page_title": "Анкета — готовый текст",
            "draft": draft,
            "saved": saved,
            "error": None,
        },
    )


@router.post("/custom-text", response_class=HTMLResponse)
async def questionnaire_custom_text_submit(
    request: Request,
    custom_lyrics_text: str = Form(default=""),
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if draft.lyrics_mode != "custom":
        return RedirectResponse(url=request.url_for("questionnaire_story_source"), status_code=303)

    value = custom_lyrics_text.strip()
    if not value:
        return templates.TemplateResponse(
            "questionnaire/custom_text.html",
            {
                "request": request,
                "page_title": "Анкета — готовый текст",
                "draft": draft,
                "saved": False,
                "error": "Вставь готовый текст песни.",
            },
            status_code=400,
        )

    draft.custom_lyrics_text = value

    db.add(
        OrderEvent(
            order=draft,
            event_type="custom_lyrics_saved",
            payload={"chars": len(value)},
        )
    )
    db.commit()

    return RedirectResponse(
        url=f"{request.url_for('questionnaire_custom_text')}?saved=1",
        status_code=303,
    )


@router.get("/story", response_class=HTMLResponse)
async def questionnaire_story(request: Request, db: Session = Depends(get_db)):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if draft.lyrics_mode == "custom":
        return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)

    if draft.story_source not in ALLOWED_STORY_SOURCES:
        return RedirectResponse(url=request.url_for("questionnaire_story_source"), status_code=303)

    latest_voice = get_latest_voice_input(db, draft.id)
    saved = request.query_params.get("saved") == "1"
    voice_uploaded = request.query_params.get("voice_uploaded") == "1"
    transcribed = request.query_params.get("transcribed") == "1"
    transcription_failed = request.query_params.get("transcription_failed") == "1"
    transcription_error = request.session.pop("voice_transcription_error", None)

    return templates.TemplateResponse(
        "questionnaire/story.html",
        {
            "request": request,
            "page_title": "Анкета — история",
            "draft": draft,
            "latest_voice": latest_voice,
            "latest_voice_size": format_size(latest_voice.size_bytes) if latest_voice else None,
            "latest_voice_status_label": humanize_transcription_status(
                latest_voice.transcription_status if latest_voice else None
            ),
            "saved": saved,
            "voice_uploaded": voice_uploaded,
            "transcribed": transcribed,
            "transcription_failed": transcription_failed,
            "transcription_error": transcription_error,
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
                "latest_voice_status_label": humanize_transcription_status(
                    latest_voice.transcription_status if latest_voice else None
                ),
                "saved": False,
                "voice_uploaded": False,
                "transcribed": False,
                "transcription_failed": False,
                "transcription_error": None,
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
    db.refresh(voice_input)

    success = await run_transcription_for_voice(db, request, draft, voice_input)

    redirect_url = f"{request.url_for('questionnaire_story')}?voice_uploaded=1"
    if success:
        redirect_url += "&transcribed=1"
    else:
        redirect_url += "&transcription_failed=1"

    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/voice-retranscribe", response_class=HTMLResponse)
async def questionnaire_voice_retranscribe(
    request: Request,
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    latest_voice = get_latest_voice_input(db, draft.id)
    if latest_voice is None:
        request.session["voice_transcription_error"] = "Нет записанного голосового для расшифровки."
        return RedirectResponse(
            url=f"{request.url_for('questionnaire_story')}?transcription_failed=1",
            status_code=303,
        )

    file_path = Path(latest_voice.storage_path)
    if not file_path.exists():
        request.session["voice_transcription_error"] = "Файл голосового не найден на сервере."
        return RedirectResponse(
            url=f"{request.url_for('questionnaire_story')}?transcription_failed=1",
            status_code=303,
        )

    success = await run_transcription_for_voice(db, request, draft, latest_voice)

    redirect_url = str(request.url_for("questionnaire_story"))
    if success:
        redirect_url += "?transcribed=1"
    else:
        redirect_url += "?transcription_failed=1"

    return RedirectResponse(url=redirect_url, status_code=303)


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
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if draft.lyrics_mode == "custom":
        return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)

    latest_voice = get_latest_voice_input(db, draft.id)
    error = None

    if draft.story_source == "voice":
        value = transcript_text.strip()
        if not value and latest_voice is None:
            error = "Сначала запиши голосовое сообщение."
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
                "latest_voice_status_label": humanize_transcription_status(
                    latest_voice.transcription_status if latest_voice else None
                ),
                "saved": False,
                "voice_uploaded": False,
                "transcribed": False,
                "transcription_failed": False,
                "transcription_error": None,
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
                "has_story_text": bool((draft.story_text or "").strip()),
            },
        )
    )
    db.commit()

    return RedirectResponse(
        url=f"{request.url_for('questionnaire_story')}?saved=1",
        status_code=303,
    )
