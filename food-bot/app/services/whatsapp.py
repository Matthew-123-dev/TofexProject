import logging

import httpx

from app.config import settings
from app.dev_outbox import record
from app.schemas import IncomingMessage

logger = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.whatsapp_access_token}"}


def _render(payload: dict) -> str:
    if payload["type"] == "text":
        return payload["text"]["body"]
    inter = payload["interactive"]
    if inter["type"] == "button":
        opts = [f'  [{b["reply"]["id"]}] {b["reply"]["title"]}' for b in inter["action"]["buttons"]]
    elif inter["type"] == "list":
        opts = [
            f'  [{r["id"]}] {r["title"]}' + (f' - {r["description"]}' if r.get("description") else "")
            for sec in inter["action"]["sections"] for r in sec["rows"]
        ]
    else:
        # location_request_message and any other action-only interactive types have no selectable rows/buttons.
        opts = ["  (reply with your location)"] if inter["type"] == "location_request_message" else []
    return inter["body"]["text"] + "\n" + "\n".join(opts)


async def _send(payload: dict) -> bool:
    if settings.dry_run:
        record("whatsapp", payload["to"], _render(payload))
        return True
    url = f"{GRAPH}/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
    body = {"messaging_product": "whatsapp", **payload}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=_headers(), json=body)
    if r.status_code >= 400:
        logger.error("WhatsApp send failed: %s %s", r.status_code, r.text)
        return False
    return True


async def send_text(to: str, body: str) -> bool:
    return await _send({"to": to, "type": "text", "text": {"body": body}})


async def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> bool:
    """Reply buttons: WhatsApp allows at most 3, titles up to 20 chars."""
    return await _send({
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in buttons[:3]
            ]},
        },
    })


async def send_list(to: str, body: str, button_label: str, section_title: str, rows: list[dict]) -> bool:
    """List message: up to 10 rows. Row = {id, title (<=24), description (<=72, optional)}."""
    return await _send({
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_label[:20],
                "sections": [{"title": section_title[:24], "rows": rows[:10]}],
            },
        },
    })


async def send_location_request(to: str, body: str) -> bool:
    return await _send({
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "location_request_message",
            "body": {"text": body[:1024]},
            "action": {"name": "send_location"},
        },
    })


async def download_media(media_id: str) -> bytes:
    """Two-step fetch: media id -> temporary URL -> bytes (both need the access token)."""
    if settings.dry_run:
        return b"dry-run-image"
    async with httpx.AsyncClient(timeout=30) as client:
        meta = await client.get(f"{GRAPH}/{settings.whatsapp_api_version}/{media_id}", headers=_headers())
        meta.raise_for_status()
        file = await client.get(meta.json()["url"], headers=_headers())
        file.raise_for_status()
        return file.content


def parse_incoming(payload: dict) -> list[IncomingMessage]:
    """Flatten Meta's nested webhook payload into IncomingMessage objects (ignores status updates)."""
    out: list[IncomingMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                mtype = m.get("type", "")
                text = reply_id = media_id = None
                latitude = longitude = location_name = location_address = None
                if mtype == "text":
                    text = m["text"]["body"]
                elif mtype == "interactive":
                    inter = m["interactive"]
                    reply_id = inter.get(inter.get("type", ""), {}).get("id")
                elif mtype == "image":
                    media_id = m["image"]["id"]
                elif mtype == "location":
                    location = m["location"]
                    latitude = float(location["latitude"])
                    longitude = float(location["longitude"])
                    location_name = location.get("name")
                    location_address = location.get("address")
                out.append(IncomingMessage(
                    id=m["id"], sender=m["from"], type=mtype,
                    text=text, reply_id=reply_id, media_id=media_id,
                    latitude=latitude, longitude=longitude,
                    location_name=location_name, location_address=location_address,
                ))
    return out
