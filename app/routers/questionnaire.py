from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(prefix="/questionnaire", tags=["questionnaire"])


@router.get("/", response_class=HTMLResponse)
async def questionnaire_start(request: Request):
    return templates.TemplateResponse(
        "questionnaire/start.html",
        {
            "request": request,
            "page_title": "Анкета заказа",
        },
    )
