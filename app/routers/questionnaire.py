from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models import Order, OrderEvent

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

    saved = request.query_params.get("saved") == "1"

    return templates.TemplateResponse(
        "questionnaire/story.html",
        {
            "request": request,
            "page_title": "Анкета — история",
            "draft": draft,
            "saved": saved,
            "error": None,
        },
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
            if not value:
                error = "Пока в этой версии вставь сюда историю или расшифровку вручную."
            else:
                draft.transcript_text = value
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
                "saved": False,
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
            },
        )
    )

    db.commit()

    return RedirectResponse(
        url=f"{request.url_for('questionnaire_story')}?saved=1",
        status_code=303,
    )
