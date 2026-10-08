import asyncio
import logging

from fastapi import FastAPI

from app.api import dashboard, dev, health, webhook_whatsapp
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

if settings.dry_run and settings.app_env == "production":
    raise RuntimeError("DRY_RUN must not be enabled in production.")

app = FastAPI(title="WhatsApp Food Ordering Bot")


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(run_scheduler())


app.include_router(health.router)
app.include_router(webhook_whatsapp.router)
app.include_router(dashboard.router)
if settings.dry_run:
    app.include_router(dev.router)
