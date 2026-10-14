import asyncio
import logging
from contextlib import asynccontextmanager

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
_WEAK_PASSWORDS = {"change-this-password", "password", "admin", "123456"}

if settings.is_production:
    missing = [
        name for name in _PLACEHOLDER_SETTINGS
        if "to be added" in str(getattr(settings, name)).lower()
    ]
    if missing:
        logger.warning("Production settings still contain placeholders: %s", ", ".join(missing))
    problems = []
    if settings.dry_run:
        problems.append("DRY_RUN must not be enabled in production")
    if len(settings.staff_password) < 10 or settings.staff_password.lower() in _WEAK_PASSWORDS:
        problems.append("STAFF_PASSWORD must be set to a strong value (10+ characters)")
    if len(settings.session_secret) < 32 or settings.session_secret.startswith("replace-with"):
        problems.append("SESSION_SECRET must be a long random value (32+ characters)")
    if not settings.whatsapp_app_secret:
        problems.append("WHATSAPP_APP_SECRET must be set so webhook signatures can be verified")
    if settings.whatsapp_verify_token == "change-me":
        problems.append("WHATSAPP_VERIFY_TOKEN must not be the default value")
    if settings.database_url.startswith("sqlite"):
        logger.warning("Production is using SQLite. Use PostgreSQL: SQLite files can be lost when the app redeploys.")
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
elif settings.dry_run is False and not settings.whatsapp_app_secret:
    logger.warning("WHATSAPP_APP_SECRET is not set: incoming webhooks will be rejected (only DRY_RUN skips the check).")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Keep a reference to the task (an unreferenced asyncio task can be garbage-collected)
    # and cancel it cleanly on shutdown/reload so no database connection is left half-open.
    task = asyncio.create_task(run_scheduler())
    app.state.scheduler_task = task
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="WhatsApp Food Ordering Bot",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)


class StaffPageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in {"/dashboard", "/manage-menu", "/orders", "/riders", "/delivery-areas"}:
            if not request.session.get("staff_authenticated"):
                return RedirectResponse("/login", status_code=303)
        return await call_next(request)


app.add_middleware(StaffPageMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or "development-only-session-secret",
    https_only=settings.is_production,
    same_site="lax",
    max_age=settings.session_max_age_hours * 3600,
)

app.include_router(health.router)
app.include_router(webhook_whatsapp.router)
app.include_router(auth.router)
app.include_router(dashboard.router)
if settings.dry_run:
    app.include_router(dev.router)
