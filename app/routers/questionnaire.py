from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import (
    generate_magic_token,
    get_session_user,
    hash_magic_token,
    is_valid_email,
    normalize_email,
    utcnow,
)
from app.core.storage import StorageError, ensure_voice_input_local_path, object_storage_enabled, save_voice_file
from app.core.templates import templates
from app.models import LyricsVersion, MagicLoginToken, Order, OrderEvent, User, VoiceInput
from app.models.order_payment import build_order_pricing_preview
from app.services.email_log_service import create_email_log
from app.services.email_service import EmailServiceError, magic_link_email_subject, send_magic_link_email
from app.services.lyrics_generation_service import (
    DualGenerationResult,
    LyricsGenerationError,
    generate_dual_lyrics_versions,
)
from app.services.transcription_service import (
    TranscriptionServiceError,
    transcribe_audio_file,
)
from app.services.rate_limit_service import RateLimitRule, enforce_rate_limit, get_client_ip
from app.services.background_jobs import BackgroundJobError, enqueue_background_job
from app.tasks import run_voice_transcription_task

router = APIRouter(prefix="/questionnaire", tags=["questionnaire"])

ALLOWED_STORY_SOURCES = {"text", "voice"}
ALLOWED_LYRICS_MODES = {"generate", "custom"}
ALLOWED_SONG_STYLES = {"pop", "rap", "rock", "chanson", "indie", "multi", "custom"}
ALLOWED_SONG_MOODS = {"romantic", "uplifting", "nostalgic", "dramatic", "party"}
ALLOWED_SINGER_GENDERS = {"male", "female"}
LYRICS_GENERATION_DAILY_LIMIT = 10


def style_requires_custom_text(song_style: str) -> bool:
    return song_style in {"multi", "custom"}


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
        Order.status.in_(["draft", "awaiting_payment", "payment_pending", "payment_canceled"]),
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


def get_lyrics_versions(db: Session, order_id: int) -> list[LyricsVersion]:
    return (
        db.query(LyricsVersion)
        .filter(LyricsVersion.order_id == order_id)
        .order_by(LyricsVersion.id.asc())
        .all()
    )

def get_lyrics_generation_scope(request: Request, db: Session, draft: Order) -> tuple[str, int | str]:
    session_user = get_session_user(request, db)
    user_id = draft.user_id or (session_user.id if session_user else None)
    if user_id is not None:
        return "user", user_id
    return "session", ensure_visitor_session(request)


def count_lyrics_generation_attempts_today(
    request: Request,
    db: Session,
    draft: Order,
) -> int:
    scope_kind, scope_value = get_lyrics_generation_scope(request, db, draft)
    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    query = (
        db.query(OrderEvent)
        .join(Order, OrderEvent.order_id == Order.id)
        .filter(
            OrderEvent.event_type == "lyrics_generation_started",
            OrderEvent.created_at >= day_start,
            OrderEvent.created_at < day_end,
        )
    )

    if scope_kind == "user":
        query = query.filter(Order.user_id == int(scope_value))
    else:
        query = query.filter(Order.session_id == str(scope_value))

    return query.count()


def get_lyrics_generation_limit_error() -> str:
    return (
        f"Новые 2 версии текста можно генерировать не более {LYRICS_GENERATION_DAILY_LIMIT} раз в день. "
        "Сегодня лимит уже исчерпан. Попробуй снова завтра."
    )


def format_size(size_bytes: int | None) -> str | None:
    if size_bytes is None:
        return None
    return f"{size_bytes / (1024 * 1024):.2f} МБ"


def humanize_transcription_status(status: str | None) -> str:
    mapping = {
        "uploaded": "Загружено",
        "queued": "В очереди",
        "transcribing": "Распознаём",
        "done": "Расшифровано",
        "failed": "Ошибка распознавания",
    }
    return mapping.get(status or "", "—")


def render_story_template(
    request: Request,
    draft: Order,
    latest_voice: VoiceInput | None,
    *,
    error: str | None = None,
    saved: bool = False,
    voice_uploaded: bool = False,
    transcribed: bool = False,
    transcription_queued: bool = False,
    transcription_failed: bool = False,
    transcription_error: str | None = None,
    status_code: int = 200,
):
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
            "transcription_queued": transcription_queued,
            "transcription_failed": transcription_failed,
            "transcription_error": transcription_error,
            "error": error,
        },
        status_code=status_code,
    )


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
        result = await transcribe_audio_file(str(ensure_voice_input_local_path(voice_input)))
    except (TranscriptionServiceError, StorageError) as exc:
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


async def generate_versions_for_draft(
    db: Session,
    draft: Order,
) -> list[dict]:
    if draft.lyrics_mode != "generate":
        raise LyricsGenerationError("Генерация версий доступна только для режима без готового текста.")

    source_text = ""
    if draft.story_source == "voice":
        source_text = (draft.transcript_text or "").strip()
    else:
        source_text = (draft.story_text or "").strip()

    if not source_text:
        raise LyricsGenerationError("Сначала нужно заполнить историю для песни.")

    db.add(
        OrderEvent(
            order=draft,
            event_type="lyrics_generation_started",
            payload={"story_source": draft.story_source},
        )
    )
    db.commit()

    try:
        result: DualGenerationResult = await generate_dual_lyrics_versions(source_text)
    except LyricsGenerationError as exc:
        db.add(
            OrderEvent(
                order=draft,
                event_type="lyrics_generation_failed",
                payload={"error": str(exc)},
            )
        )
        db.commit()
        raise

    db.query(LyricsVersion).filter(
        LyricsVersion.order_id == draft.id
    ).delete(synchronize_session=False)

    for item in result.versions:
        db.add(
            LyricsVersion(
                order_id=draft.id,
                provider=item.provider,
                model_name=item.model_name,
                angle_label=item.angle_label,
                prompt_text=item.prompt_text,
                lyrics_text=item.lyrics_text,
                edited_lyrics_text=None,
                is_selected=False,
            )
        )

    variant_errors = [
        {
            "slot_label": err.slot_label,
            "user_message": err.user_message,
            "technical_message": err.technical_message,
        }
        for err in result.errors
    ]

    db.add(
        OrderEvent(
            order=draft,
            event_type="lyrics_generation_done",
            payload={
                "versions_count": len(result.versions),
                "model": result.versions[0].model_name if result.versions else None,
                "errors": variant_errors,
            },
        )
    )
    db.commit()

    return variant_errors


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
    draft.final_lyrics_text = value

    db.add(
        OrderEvent(
            order=draft,
            event_type="custom_lyrics_saved",
            payload={"chars": len(value)},
        )
    )
    db.commit()

    return RedirectResponse(
        url=f"{request.url_for('questionnaire_style')}?saved=1",
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
    transcription_queued = request.query_params.get("transcription_queued") == "1"
    transcription_failed = request.query_params.get("transcription_failed") == "1"
    transcription_error = request.session.pop("voice_transcription_error", None)

    return render_story_template(
        request,
        draft,
        latest_voice,
        saved=saved,
        voice_uploaded=voice_uploaded,
        transcribed=transcribed,
        transcription_queued=transcription_queued,
        transcription_failed=transcription_failed,
        transcription_error=transcription_error,
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

    limit_decision = enforce_rate_limit(
        db,
        request=request,
        action="questionnaire_voice_upload",
        user_message=f"Голосовое можно загружать не более {settings.VOICE_UPLOAD_LIMIT_PER_ORDER_PER_DAY} раз в день для одного заказа.",
        rules=[
            RateLimitRule("order", draft.public_id, settings.VOICE_UPLOAD_LIMIT_PER_ORDER_PER_DAY, 24 * 60 * 60),
        ],
        order=draft,
        extra_payload={
            "order_public_id": draft.public_id,
            "story_source": draft.story_source,
            "ip": get_client_ip(request),
        },
    )
    if not limit_decision.allowed:
        db.commit()
        return render_story_template(
            request,
            draft,
            latest_voice,
            error=limit_decision.message,
            status_code=429,
        )

    db.commit()

    try:
        stored = save_voice_file(voice_file)
    except (ValueError, StorageError) as exc:
        return render_story_template(
            request,
            draft,
            latest_voice,
            error=str(exc),
            status_code=400,
        )

    voice_input = VoiceInput(
        order_id=draft.id,
        original_filename=stored.original_filename,
        content_type=stored.content_type,
        storage_path=stored.absolute_path,
        relative_path=stored.relative_path,
        size_bytes=stored.size_bytes,
        storage_backend=stored.storage_backend,
        storage_bucket=stored.storage_bucket,
        storage_key=stored.storage_key,
        transcription_status="queued",
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

    transcription_ok = await run_transcription_for_voice(db, request, draft, voice_input)
    db.refresh(draft)
    db.refresh(voice_input)

    if transcription_ok:
        redirect_url = f"{request.url_for('questionnaire_story')}?voice_uploaded=1&transcribed=1"
    else:
        redirect_url = f"{request.url_for('questionnaire_story')}?voice_uploaded=1&transcription_failed=1"

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

    try:
        ensure_voice_input_local_path(latest_voice)
    except StorageError:
        request.session["voice_transcription_error"] = "Файл голосового не найден на сервере."
        return RedirectResponse(
            url=f"{request.url_for('questionnaire_story')}?transcription_failed=1",
            status_code=303,
        )

    limit_decision = enforce_rate_limit(
        db,
        request=request,
        action="questionnaire_voice_retranscribe",
        user_message=f"Перезапускать расшифровку можно не более {settings.VOICE_RETRANSCRIBE_LIMIT_PER_ORDER_PER_HOUR} раз в час для одного заказа.",
        rules=[
            RateLimitRule("order", draft.public_id, settings.VOICE_RETRANSCRIBE_LIMIT_PER_ORDER_PER_HOUR, 60 * 60),
        ],
        order=draft,
        extra_payload={"order_public_id": draft.public_id, "voice_input_id": latest_voice.public_id},
    )
    if not limit_decision.allowed:
        db.commit()
        request.session["voice_transcription_error"] = limit_decision.message
        return RedirectResponse(
            url=f"{request.url_for('questionnaire_story')}?transcription_failed=1",
            status_code=303,
        )

    latest_voice.transcription_status = "queued"
    db.add(latest_voice)
    db.commit()

    force_sync_transcription = not object_storage_enabled() and not settings.BACKGROUND_JOBS_SYNC_MODE

    try:
        enqueue_background_job(
            db,
            order=draft,
            job_type="voice_transcription",
            func=run_voice_transcription_task,
            payload={
                "order_public_id": draft.public_id,
                "voice_public_id": latest_voice.public_id,
                "apply_to_order": True,
                "started_event_type": "voice_transcription_started",
                "success_event_type": "voice_transcription_done",
                "failure_event_type": "voice_transcription_failed",
                "trigger": "questionnaire_voice_retranscribe",
            },
            force_sync=force_sync_transcription,
        )
        db.commit()
    except BackgroundJobError as exc:
        db.rollback()
        request.session["voice_transcription_error"] = f"Не удалось поставить расшифровку в очередь: {exc}"
        return RedirectResponse(
            url=f"{request.url_for('questionnaire_story')}?transcription_failed=1",
            status_code=303,
        )

    redirect_url = str(request.url_for("questionnaire_story")) + ("?transcription_done=1" if force_sync_transcription else "?transcription_queued=1")
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

    try:
        file_path = ensure_voice_input_local_path(voice_input)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return FileResponse(
        path=file_path,
        media_type=voice_input.content_type,
        filename=voice_input.original_filename or file_path.name,
    )


@router.post("/story", response_class=HTMLResponse)
async def questionnaire_story_submit(
    request: Request,
    action: str = Form(default="generate"),
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
                "action": action,
            },
        )
    )
    db.commit()

    if action == "generate":
        attempts_today = count_lyrics_generation_attempts_today(request, db, draft)
        if attempts_today >= LYRICS_GENERATION_DAILY_LIMIT:
            scope_kind, scope_value = get_lyrics_generation_scope(request, db, draft)
            db.add(
                OrderEvent(
                    order=draft,
                    event_type="lyrics_generation_limit_reached",
                    payload={
                        "scope": scope_kind,
                        "scope_value": str(scope_value),
                        "attempts_today": attempts_today,
                        "limit": LYRICS_GENERATION_DAILY_LIMIT,
                    },
                )
            )
            db.commit()

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
                    "error": get_lyrics_generation_limit_error(),
                },
                status_code=400,
            )

        try:
            variant_errors = await generate_versions_for_draft(db, draft)
        except LyricsGenerationError as exc:
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

        if variant_errors:
            request.session["lyrics_generation_warning"] = " ".join(
                item["user_message"] for item in variant_errors
            )

        redirect_url = f"{request.url_for('questionnaire_lyrics')}?generated=1"
        if variant_errors:
            redirect_url += "&partial=1"

        return RedirectResponse(
            url=redirect_url,
            status_code=303,
        )

    return RedirectResponse(
        url=request.url_for("questionnaire_story"),
        status_code=303,
    )


@router.get("/lyrics", response_class=HTMLResponse)
async def questionnaire_lyrics(request: Request, db: Session = Depends(get_db)):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if draft.lyrics_mode != "generate":
        return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)

    versions = get_lyrics_versions(db, draft.id)
    if not versions:
        return RedirectResponse(url=request.url_for("questionnaire_story"), status_code=303)

    generated = request.query_params.get("generated") == "1"
    saved = request.query_params.get("saved") == "1"
    partial = request.query_params.get("partial") == "1"
    generation_warning = request.session.pop("lyrics_generation_warning", None)

    selected_version = next((item for item in versions if item.is_selected), versions[0])
    final_lyrics_text = selected_version.edited_lyrics_text or selected_version.lyrics_text

    return templates.TemplateResponse(
        "questionnaire/lyrics.html",
        {
            "request": request,
            "page_title": "Анкета — версии текста",
            "draft": draft,
            "versions": versions,
            "selected_version": selected_version,
            "final_lyrics_text": final_lyrics_text,
            "generated": generated,
            "saved": saved,
            "partial": partial,
            "generation_warning": generation_warning,
            "error": None,
        },
    )


@router.post("/lyrics", response_class=HTMLResponse)
async def questionnaire_lyrics_submit(
    request: Request,
    selected_version_public_id: str = Form(...),
    final_lyrics_text: str = Form(default=""),
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if draft.lyrics_mode != "generate":
        return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)

    versions = get_lyrics_versions(db, draft.id)
    if not versions:
        return RedirectResponse(url=request.url_for("questionnaire_story"), status_code=303)

    selected_version = next(
        (item for item in versions if item.public_id == selected_version_public_id),
        None,
    )

    value = final_lyrics_text.strip()
    if selected_version is None or not value:
        fallback_selected = next((item for item in versions if item.is_selected), versions[0])

        return templates.TemplateResponse(
            "questionnaire/lyrics.html",
            {
                "request": request,
                "page_title": "Анкета — версии текста",
                "draft": draft,
                "versions": versions,
                "selected_version": fallback_selected,
                "final_lyrics_text": value or (fallback_selected.edited_lyrics_text or fallback_selected.lyrics_text),
                "generated": False,
                "saved": False,
                "partial": False,
                "generation_warning": None,
                "error": "Выбери одну версию и оставь непустой финальный текст.",
            },
            status_code=400,
        )

    for version in versions:
        version.is_selected = version.public_id == selected_version.public_id

    selected_version.edited_lyrics_text = value
    draft.final_lyrics_text = value

    db.add(
        OrderEvent(
            order=draft,
            event_type="lyrics_version_selected",
            payload={
                "variant": selected_version.angle_label,
                "version_id": selected_version.public_id,
                "chars": len(value),
            },
        )
    )
    db.commit()

    return RedirectResponse(
        url=f"{request.url_for('questionnaire_style')}?saved=1",
        status_code=303,
    )

@router.get("/style", response_class=HTMLResponse)
async def questionnaire_style(request: Request, db: Session = Depends(get_db)):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if not (draft.final_lyrics_text or "").strip():
        if draft.lyrics_mode == "custom":
            return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)
        return RedirectResponse(url=request.url_for("questionnaire_lyrics"), status_code=303)

    saved = request.query_params.get("saved") == "1"

    return templates.TemplateResponse(
        "questionnaire/style.html",
        {
            "request": request,
            "page_title": "Анкета — стиль песни",
            "draft": draft,
            "saved": saved,
            "error": None,
        },
    )


@router.post("/style", response_class=HTMLResponse)
async def questionnaire_style_submit(
    request: Request,
    song_style: str | None = Form(default=None),
    song_style_custom: str = Form(default=""),
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if not (draft.final_lyrics_text or "").strip():
        if draft.lyrics_mode == "custom":
            return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)
        return RedirectResponse(url=request.url_for("questionnaire_lyrics"), status_code=303)

    song_style = (song_style or "").strip().lower()
    song_style_custom = song_style_custom.strip()

    if song_style not in ALLOWED_SONG_STYLES:
        return templates.TemplateResponse(
            "questionnaire/style.html",
            {
                "request": request,
                "page_title": "Анкета — стиль песни",
                "draft": draft,
                "saved": False,
                "error": "Выбери стиль песни.",
                "form_song_style": song_style,
                "form_song_style_custom": song_style_custom,
            },
            status_code=400,
        )

    if style_requires_custom_text(song_style) and not song_style_custom:
        return templates.TemplateResponse(
            "questionnaire/style.html",
            {
                "request": request,
                "page_title": "Анкета — стиль песни",
                "draft": draft,
                "saved": False,
                "error": "Уточни стиль песни.",
                "form_song_style": song_style,
                "form_song_style_custom": song_style_custom,
            },
            status_code=400,
        )

    draft.song_style = song_style
    draft.song_style_custom = song_style_custom if style_requires_custom_text(song_style) else None

    db.add(
        OrderEvent(
            order=draft,
            event_type="song_style_selected",
            payload={
                "song_style": draft.song_style,
                "song_style_custom": draft.song_style_custom,
            },
        )
    )
    db.commit()

    return RedirectResponse(
        url=request.url_for("questionnaire_mood"),
        status_code=303,
    )


@router.get("/mood", response_class=HTMLResponse)
async def questionnaire_mood(request: Request, db: Session = Depends(get_db)):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if not (draft.final_lyrics_text or "").strip():
        if draft.lyrics_mode == "custom":
            return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)
        return RedirectResponse(url=request.url_for("questionnaire_lyrics"), status_code=303)

    if not (draft.song_style or "").strip():
        return RedirectResponse(url=request.url_for("questionnaire_style"), status_code=303)

    return templates.TemplateResponse(
        "questionnaire/mood.html",
        {
            "request": request,
            "page_title": "Анкета — настроение песни",
            "draft": draft,
            "error": None,
        },
    )


@router.post("/mood", response_class=HTMLResponse)
async def questionnaire_mood_submit(
    request: Request,
    song_mood: str = Form(...),
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if not (draft.final_lyrics_text or "").strip():
        if draft.lyrics_mode == "custom":
            return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)
        return RedirectResponse(url=request.url_for("questionnaire_lyrics"), status_code=303)

    if not (draft.song_style or "").strip():
        return RedirectResponse(url=request.url_for("questionnaire_style"), status_code=303)

    song_mood = song_mood.strip().lower()
    if song_mood not in ALLOWED_SONG_MOODS:
        return templates.TemplateResponse(
            "questionnaire/mood.html",
            {
                "request": request,
                "page_title": "Анкета — настроение песни",
                "draft": draft,
                "error": "Выбери настроение песни.",
                "form_song_mood": song_mood,
            },
            status_code=400,
        )

    draft.song_mood = song_mood
    db.add(
        OrderEvent(
            order=draft,
            event_type="song_mood_selected",
            payload={"song_mood": song_mood},
        )
    )
    db.commit()

    return RedirectResponse(
        url=request.url_for("questionnaire_singer"),
        status_code=303,
    )


@router.get("/singer", response_class=HTMLResponse)
async def questionnaire_singer(request: Request, db: Session = Depends(get_db)):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if not (draft.final_lyrics_text or "").strip():
        if draft.lyrics_mode == "custom":
            return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)
        return RedirectResponse(url=request.url_for("questionnaire_lyrics"), status_code=303)

    if not (draft.song_style or "").strip():
        return RedirectResponse(url=request.url_for("questionnaire_style"), status_code=303)

    return templates.TemplateResponse(
        "questionnaire/singer.html",
        {
            "request": request,
            "page_title": "Анкета — кто поёт",
            "draft": draft,
            "error": None,
        },
    )


@router.post("/singer", response_class=HTMLResponse)
async def questionnaire_singer_submit(
    request: Request,
    singer_gender: str = Form(...),
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if not (draft.final_lyrics_text or "").strip():
        if draft.lyrics_mode == "custom":
            return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)
        return RedirectResponse(url=request.url_for("questionnaire_lyrics"), status_code=303)

    if not (draft.song_style or "").strip():
        return RedirectResponse(url=request.url_for("questionnaire_style"), status_code=303)

    if not (draft.song_mood or "").strip():
        return RedirectResponse(url=request.url_for("questionnaire_mood"), status_code=303)

    singer_gender = singer_gender.strip().lower()

    if singer_gender not in ALLOWED_SINGER_GENDERS:
        return templates.TemplateResponse(
            "questionnaire/singer.html",
            {
                "request": request,
                "page_title": "Анкета — кто поёт",
                "draft": draft,
                "error": "Выбери, кто должен петь песню.",
                "form_singer_gender": singer_gender,
            },
            status_code=400,
        )

    draft.singer_gender = singer_gender

    db.add(
        OrderEvent(
            order=draft,
            event_type="singer_gender_selected",
            payload={"singer_gender": singer_gender, "song_mood": draft.song_mood},
        )
    )
    db.commit()

    return RedirectResponse(
        url=request.url_for("questionnaire_access"),
        status_code=303,
    )

@router.get("/access", response_class=HTMLResponse)
async def questionnaire_access(request: Request, db: Session = Depends(get_db)):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if not (draft.final_lyrics_text or "").strip():
        if draft.lyrics_mode == "custom":
            return RedirectResponse(url=request.url_for("questionnaire_custom_text"), status_code=303)
        return RedirectResponse(url=request.url_for("questionnaire_lyrics"), status_code=303)

    if not (draft.song_style or "").strip():
        return RedirectResponse(url=request.url_for("questionnaire_style"), status_code=303)

    if draft.singer_gender not in ALLOWED_SINGER_GENDERS:
        return RedirectResponse(url=request.url_for("questionnaire_singer"), status_code=303)

    session_user = get_session_user(request, db)
    if session_user is not None:
        if draft.user_id is None:
            draft.user_id = session_user.id
            db.add(
                OrderEvent(
                    order=draft,
                    event_type="account_linked_from_session",
                    payload={
                        "email": session_user.email,
                        "user_id": session_user.public_id,
                    },
                )
            )
            db.commit()

        if draft.user_id == session_user.id:
            return RedirectResponse(
                url=request.url_for("account_order_detail", order_public_id=draft.public_id),
                status_code=303,
            )

    saved = request.query_params.get("saved") == "1"
    sent = request.query_params.get("sent") == "1"
    stub_login_url = request.session.get("stub_questionnaire_login_url")
    pricing = build_order_pricing_preview(db, draft)

    return templates.TemplateResponse(
        "questionnaire/access.html",
        {
            "request": request,
            "page_title": "Анкета — доступ к кабинету",
            "draft": draft,
            "saved": saved,
            "sent": sent,
            "stub_mode": settings.MAGIC_LINK_STUB_MODE,
            "stub_login_url": stub_login_url,
            "price_rub": int(pricing["final_price_rub"]),
            "base_price_rub": int(pricing["base_price_rub"]),
            "discount_rub": int(pricing["discount_rub"]),
            "has_discount": bool(pricing["has_discount"]),
            "error": None,
            "form_email": draft.user.email if draft.user else "",
        },
    )


@router.post("/access", response_class=HTMLResponse)
async def questionnaire_access_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    draft = get_current_draft(db, request)
    if draft is None:
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if not (draft.final_lyrics_text or "").strip():
        return RedirectResponse(url=request.url_for("questionnaire_start"), status_code=303)

    if not (draft.song_style or "").strip():
        return RedirectResponse(url=request.url_for("questionnaire_style"), status_code=303)

    if draft.singer_gender not in ALLOWED_SINGER_GENDERS:
        return RedirectResponse(url=request.url_for("questionnaire_singer"), status_code=303)

    email = normalize_email(email)

    if not is_valid_email(email):
        pricing = build_order_pricing_preview(db, draft)
        return templates.TemplateResponse(
            "questionnaire/access.html",
            {
                "request": request,
                "page_title": "Анкета — доступ к кабинету",
                "draft": draft,
                "saved": False,
                "sent": False,
                "stub_mode": settings.MAGIC_LINK_STUB_MODE,
                "stub_login_url": None,
                "price_rub": int(pricing["final_price_rub"]),
                "base_price_rub": int(pricing["base_price_rub"]),
                "discount_rub": int(pricing["discount_rub"]),
                "has_discount": bool(pricing["has_discount"]),
                "error": "Укажи корректный email.",
                "form_email": email,
            },
            status_code=400,
        )

    limit_decision = enforce_rate_limit(
        db,
        request=request,
        action="questionnaire_magic_link_send",
        user_message="Ссылка для входа уже отправлялась слишком часто. Подождите немного и попробуйте снова.",
        rules=[
            RateLimitRule("order", draft.public_id, settings.QUESTIONNAIRE_MAGIC_LINK_ORDER_LIMIT_PER_HOUR, 60 * 60),
            RateLimitRule("email", email, settings.MAGIC_LINK_EMAIL_LIMIT_PER_HOUR, 60 * 60),
            RateLimitRule("ip", get_client_ip(request), settings.MAGIC_LINK_IP_LIMIT_PER_HOUR, 60 * 60),
        ],
        order=draft,
        extra_payload={
            "order_public_id": draft.public_id,
            "email": email,
            "ip": get_client_ip(request),
        },
    )
    if not limit_decision.allowed:
        db.commit()
        pricing = build_order_pricing_preview(db, draft)
        return templates.TemplateResponse(
            "questionnaire/access.html",
            {
                "request": request,
                "page_title": "Анкета — доступ к кабинету",
                "draft": draft,
                "saved": False,
                "sent": False,
                "stub_mode": settings.MAGIC_LINK_STUB_MODE,
                "stub_login_url": None,
                "price_rub": int(pricing["final_price_rub"]),
                "base_price_rub": int(pricing["base_price_rub"]),
                "discount_rub": int(pricing["discount_rub"]),
                "has_discount": bool(pricing["has_discount"]),
                "error": limit_decision.message,
                "form_email": email,
            },
            status_code=429,
        )

    db.commit()

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(email=email)
        db.add(user)
        db.flush()

    draft.user_id = user.id

    raw_token = generate_magic_token()
    token_hash = hash_magic_token(raw_token)
    expires_at = utcnow() + timedelta(minutes=settings.MAGIC_LINK_TTL_MINUTES)

    db.add(
        MagicLoginToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )

    db.add(
        OrderEvent(
            order=draft,
            event_type="account_linked",
            payload={
                "email": email,
                "user_id": user.public_id,
                "song_style": draft.song_style,
                "song_style_custom": draft.song_style_custom,
                "singer_gender": draft.singer_gender,
            },
        )
    )
    db.commit()

    login_url = f"{settings.BASE_URL.rstrip('/')}/account/magic-login?token={raw_token}"

    try:
        delivery = send_magic_link_email(
            recipient_email=email,
            login_url=login_url,
        )
        create_email_log(
            db,
            email_type="magic_link",
            recipient_email=email,
            subject=magic_link_email_subject(),
            status="stub" if delivery.mode == "stub" else "sent",
            delivery_mode=delivery.mode,
            order=draft,
            user=user,
            payload={"login_url": delivery.login_url, "source": "questionnaire_access"},
        )
        db.commit()
    except EmailServiceError as exc:
        create_email_log(
            db,
            email_type="magic_link",
            recipient_email=email,
            subject=magic_link_email_subject(),
            status="failed",
            delivery_mode="email",
            order=draft,
            user=user,
            error_message=str(exc),
            payload={"login_url": login_url, "source": "questionnaire_access"},
        )
        db.commit()
        pricing = build_order_pricing_preview(db, draft)
        return templates.TemplateResponse(
            "questionnaire/access.html",
            {
                "request": request,
                "page_title": "Анкета — доступ к кабинету",
                "draft": draft,
                "saved": False,
                "sent": False,
                "stub_mode": settings.MAGIC_LINK_STUB_MODE,
                "stub_login_url": None,
                "price_rub": int(pricing["final_price_rub"]),
                "base_price_rub": int(pricing["base_price_rub"]),
                "discount_rub": int(pricing["discount_rub"]),
                "has_discount": bool(pricing["has_discount"]),
                "error": str(exc),
                "form_email": email,
            },
            status_code=400,
        )

    request.session["account_user_id"] = user.id

    if delivery.mode == "stub":
        request.session["stub_questionnaire_login_url"] = delivery.login_url
    else:
        request.session.pop("stub_questionnaire_login_url", None)

    return RedirectResponse(
        url=f"/checkout/start/{draft.public_id}",
        status_code=303,
    )
