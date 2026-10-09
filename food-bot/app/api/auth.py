from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.config import settings

router = APIRouter(tags=["auth"])
LOGIN_HTML_PATH = Path(__file__).resolve().parents[1] / "login.html"


@router.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(LOGIN_HTML_PATH)


@router.post("/login", include_in_schema=False)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not settings.staff_password:
        return RedirectResponse("/login?error=not-configured", status_code=303)
    if username != settings.staff_username or password != settings.staff_password:
        return RedirectResponse("/login?error=invalid", status_code=303)
    request.session["staff_authenticated"] = True
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
