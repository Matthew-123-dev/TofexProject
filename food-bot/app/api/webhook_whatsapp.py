import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.database import async_session_factory
from app.models import ProcessedMessage
from app.services import state_machine, whatsapp

logger = logging.getLogger(__name__)
router = APIRouter()


def _signature_ok(raw: bytes, header: str | None) -> bool:
    if not settings.whatsapp_app_secret:
        return settings.app_env != "production"  # allow unsigned requests in local dev only
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.whatsapp_app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@router.get("/webhook/whatsapp")
async def verify(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> PlainTextResponse:
    if mode == "subscribe" and hmac.compare_digest(token, settings.whatsapp_verify_token):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook/whatsapp")
async def receive(
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, str]:
    raw = await request.body()
    if not _signature_ok(raw, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Bad signature")
    background.add_task(process_payload, json.loads(raw))
    return {"status": "received"}  # return 200 fast; Meta retries slow responses


async def process_payload(payload: dict) -> None:
    messages = whatsapp.parse_incoming(payload)
    async with async_session_factory() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        await session.execute(delete(ProcessedMessage).where(ProcessedMessage.created_at < cutoff))
        await session.commit()
        for msg in messages:
            if await session.get(ProcessedMessage, msg.id):
                continue  # duplicate delivery
            session.add(ProcessedMessage(message_id=msg.id))
            await session.commit()  # mark first so retries are ignored even if handling fails
            try:
                await state_machine.handle_incoming(session, msg)
                await session.commit()
            except Exception:
                logger.exception("Failed handling WhatsApp message %s", msg.id)
                await session.rollback()
