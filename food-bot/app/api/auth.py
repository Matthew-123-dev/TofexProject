import hmac
import time
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.config import settings

router = APIRouter(tags=["auth"])
LOGIN_HTML_PATH = Path(__file__).resolve().parents[1] / "login.html"

MAX_FAILURES = 5
WINDOW_SECONDS = 10 * 60
_failures: dict[str, list[float]] = {}


def _client_key(request: Request) -> str:
    # Behind Railway's proxy the real client is the LAST hop it appended to X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _recent_failures(key: str) -> list[float]:
    cutoff = time.time() - WINDOW_SECONDS
    recent = [t for t in _failures.get(key, []) if t > cutoff]
    if recent:
        _failures[key] = recent
    else:
        _failures.pop(key, None)
    return recent


@router.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(LOGIN_HTML_PATH)


@router.post("/login", include_in_schema=False)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not settings.staff_password:
        return RedirectResponse("/login?error=not-configured", status_code=303)
    key = _client_key(request)
    if len(_recent_failures(key)) >= MAX_FAILURES:
        return RedirectResponse("/login?error=locked", status_code=303)
    # compare_digest avoids leaking how many characters matched.
    user_ok = hmac.compare_digest(username.encode(), settings.staff_username.encode())
    pass_ok = hmac.compare_digest(password.encode(), settings.staff_password.encode())
    if not (user_ok and pass_ok):
        _failures.setdefault(key, []).append(time.time())
        return RedirectResponse("/login?error=invalid", status_code=303)
    _failures.pop(key, None)
    request.session.clear()
    request.session["staff_authenticated"] = True
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
