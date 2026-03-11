from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "public/home.html",
        {
            "request": request,
            "page_title": "Music Memoire — персональные песни в подарок",
        },
    )


@router.get("/health")
async def health():
    return {"ok": True, "service": "musicmemoire-web"}
