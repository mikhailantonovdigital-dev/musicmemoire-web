from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/", response_class=HTMLResponse)
async def account_dashboard(request: Request):
    return templates.TemplateResponse(
        "account/dashboard.html",
        {
            "request": request,
            "page_title": "Личный кабинет",
        },
    )
