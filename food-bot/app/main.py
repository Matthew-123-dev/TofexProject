
import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.api import auth, dashboard, dev, health, webhook_whatsapp
from app.config import settings
from app.services.scheduler import run_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_PLACEHOLDER_SETTINGS = ("outlet_address", "outlet_phone", "support_phone", "refund_timeframe")
if settings.app_env == "production":
    missing = [
        name for name in _PLACEHOLDER_SETTINGS
        if "to be added" in str(getattr(settings, name)).lower()
    ]
    if missing:
        logger.warning("Production settings still contain placeholders: %s", ", ".join(missing))
    if not settings.session_secret or not settings.staff_password:
        raise RuntimeError("STAFF_PASSWORD and SESSION_SECRET must be configured in production.")

if settings.dry_run and settings.app_env == "production":
    raise RuntimeError("DRY_RUN must not be enabled in production.")

app = FastAPI(title="WhatsApp Food Ordering Bot")


class StaffPageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in {"/dashboard", "/manage-menu", "/orders", "/riders", "/delivery-areas"}:
            if not request.session.get("staff_authenticated"):
                return RedirectResponse("/login", status_code=303)
        return await call_next(request)


app.add_middleware(StaffPageMiddleware)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret or "development-only-session-secret")


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(run_scheduler())


app.include_router(health.router)
app.include_router(webhook_whatsapp.router)
app.include_router(auth.router)
app.include_router(dashboard.router)
if settings.dry_run:
    app.include_router(dev.router)
