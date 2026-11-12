import hashlib
import hmac
import json
import logging
from typing import Any

from sqlalchemy.exc import IntegrityError
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.database import async_session_factory
from app.models import ProcessedMessage
from app.services import state_machine, whatsapp
from app.services.whatsapp import BoundedTTLCache, LAST_MENU_MAP, SEEN_MESSAGE_IDS

logger = logging.getLogger(__name__)
router = APIRouter()


def _signature_ok(raw: bytes, header: str | None) -> bool:
    if not settings.whatsapp_app_secret:
        return settings.dry_run  # unsigned requests are accepted ONLY in dry-run (simulator) mode
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.whatsapp_app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _is_evolution_webhook(payload: Any) -> bool:
    if settings.whatsapp_provider.strip().lower() != "evolution":
        return False
    if not isinstance(payload, dict):
        return False
    return "event" in payload or ("data" in payload and "entry" not in payload)


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
@router.post("/webhook/whatsapp/{event_name}")
async def receive(
    request: Request,
    background: BackgroundTasks,
    event_name: str | None = None,
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, str]:
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    if isinstance(payload, dict) and event_name and "event" not in payload:
        payload["event"] = event_name.replace("-", ".")

    if not _is_evolution_webhook(payload) and not _signature_ok(raw, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Bad signature")

    background.add_task(process_payload, payload)
    return {"status": "received"}  # return 200 fast; webhook providers retry slow responses


FALLBACK_REPLY = "Sorry, something went wrong on our side. Please try again, or send *menu* to start over."


async def process_payload(payload: dict) -> None:
    messages = whatsapp.parse_incoming(payload)
    async with async_session_factory() as session:
        for msg in messages:
            try:
                session.add(ProcessedMessage(message_id=msg.id))
                await session.commit()  # mark first so webhook retries are ignored
            except IntegrityError:
                await session.rollback()
                continue  # duplicate delivery
            try:
                await state_machine.handle_incoming(session, msg)
                await session.commit()
            except Exception:
                logger.exception("Failed handling WhatsApp message %s", msg.id)
                await session.rollback()
                try:  # never leave the customer in silence
                    await whatsapp.send_text(msg.sender, FALLBACK_REPLY)
                except Exception:
                    logger.exception("Could not send the fallback reply to %s", msg.sender)
