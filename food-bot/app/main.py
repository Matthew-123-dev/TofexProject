import logging

from fastapi import FastAPI

from app.api import dashboard, dev, health, webhook_whatsapp
from app.config import settings

logging.basicConfig(level=logging.INFO)

if settings.dry_run and settings.app_env == "production":
    raise RuntimeError("DRY_RUN must not be enabled in production.")

app = FastAPI(title="WhatsApp Food Ordering Bot")
app.include_router(health.router)
app.include_router(webhook_whatsapp.router)
app.include_router(dashboard.router)
if settings.dry_run:
    app.include_router(dev.router)
