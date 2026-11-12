import base64
from collections import OrderedDict
import logging
import time
from typing import Any

import httpx

from app.config import settings
from app.dev_outbox import record
from app.schemas import IncomingMessage

logger = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com"
EVOLUTION_DELAY_MS = 1200


def _clean_phone(phone: str) -> str:
    """Normalize a WhatsApp JID or phone string to bare digits/identifier."""
    cleaned = (phone or "").strip()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    return cleaned.lstrip("+")


class _CaseInsensitiveMenuMap(dict[str, str]):
    """Per-user mapping from option number/title to Meta button_id or list_row_id."""

    def __getitem__(self, key: str) -> str:
        if super().__contains__(key):
            return super().__getitem__(key)
        if isinstance(key, str):
            norm = key.strip().lower()
            if super().__contains__(norm):
                return super().__getitem__(norm)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        if super().__contains__(key):
            return True
        if isinstance(key, str):
            return super().__contains__(key.strip().lower())
        return False


class _PhoneMenuCache(dict[str, _CaseInsensitiveMenuMap]):
    """Per-phone cache that normalizes phone numbers on access."""

    def __getitem__(self, phone: str) -> _CaseInsensitiveMenuMap:
        norm = _clean_phone(phone)
        if super().__contains__(norm):
            return super().__getitem__(norm)
        return super().__getitem__(phone)

    def __setitem__(self, phone: str, value: dict[str, str]) -> None:
        mapped = value if isinstance(value, _CaseInsensitiveMenuMap) else _CaseInsensitiveMenuMap(value)
        super().__setitem__(_clean_phone(phone), mapped)

    def get(self, phone: str, default: Any = None) -> Any:
        try:
            return self[phone]
        except KeyError:
            return default

    def __contains__(self, phone: object) -> bool:
        if isinstance(phone, str):
            return super().__contains__(_clean_phone(phone)) or super().__contains__(phone)
        return super().__contains__(phone)


class BoundedTTLCache:
    """Bounded in-memory TTL cache/set for webhook message ID deduplication."""

    def __init__(self, maxsize: int = 4096, ttl: float = 3600.0) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._store: OrderedDict[str, float] = OrderedDict()

    def _purge(self, now: float) -> None:
        cutoff = now - self.ttl
        while self._store:
            oldest_key, ts = next(iter(self._store.items()))
            if ts <= cutoff:
                self._store.popitem(last=False)
            else:
                break

    def add_or_seen(self, key: str) -> bool:
        """Return True if `key` is already in cache (duplicate); otherwise record it and return False."""
        if not key:
            return False
        now = time.monotonic()
        self._purge(now)
        if key in self._store:
            self._store.move_to_end(key)
            return True
        self._store[key] = now
        while len(self._store) > self.maxsize:
            self._store.popitem(last=False)
        return False

    def add(self, key: str) -> None:
        self.add_or_seen(key)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        self._purge(time.monotonic())
        return key in self._store

    def __len__(self) -> int:
        self._purge(time.monotonic())
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()


LAST_MENU_MAP: _PhoneMenuCache = _PhoneMenuCache()
_LAST_MENU_KIND: dict[str, str] = {}
SEEN_MESSAGE_IDS = BoundedTTLCache(maxsize=4096, ttl=3600.0)
_MEDIA_CACHE: OrderedDict[str, tuple[bytes, str]] = OrderedDict()


def _cache_inline_media(media_id: str, raw_b64: str, mime_type: str = "image/jpeg") -> None:
    if not media_id or not raw_b64:
        return
    try:
        cleaned = raw_b64.split(",", 1)[-1].strip()
        content = base64.b64decode(cleaned)
    except Exception:
        return
    _MEDIA_CACHE[media_id] = (content, mime_type or "image/jpeg")
    while len(_MEDIA_CACHE) > 128:
        _MEDIA_CACHE.popitem(last=False)


def _ensure_poll_options(options: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Pad a 1-option menu with a context-appropriate second action so WhatsApp Polls (>=2 options) succeed."""
    if len(options) != 1:
        return list(options)
    first_id, first_title = options[0]
    if first_id.startswith("address:") or first_id == "area:other":
        second = ("cancel", "Cancel order")
    elif first_id.startswith("r:"):
        second = ("help", "Help")
    elif first_id == "menu" or first_title.strip().lower() in {"browse menu", "main menu"}:
        second = ("carts", "View carts")
    else:
        second = ("menu", "Browse menu")
    return [(first_id, first_title), second]


def _clear_numeric_shortcuts(to: str) -> None:
    """Remove bare number shortcuts ('1', '2', ...) when a plain-text prompt is sent."""
    phone = _clean_phone(to)
    menu = LAST_MENU_MAP.get(phone)
    if menu:
        for k in [k for k in list(menu.keys()) if k.isdigit()]:
            menu.pop(k, None)
    _LAST_MENU_KIND[phone] = "text"


def _populate_menu_map(
    to: str,
    options: list[tuple[str, str]],
    *,
    kind: str,
    extra_aliases: dict[str, str] | None = None,
) -> list[str]:
    """Build 1-based poll values and update LAST_MENU_MAP[phone]."""
    phone = _clean_phone(to)
    mapping = _CaseInsensitiveMenuMap()

    follows_list = kind == "buttons" and _LAST_MENU_KIND.get(phone) == "list" and phone in LAST_MENU_MAP
    is_pagination_buttons = follows_list and bool(options) and all(
        opt_id == "menu" or opt_id.startswith("catpg:") for opt_id, _ in options
    )

    # When send_list is immediately followed by send_buttons in the same turn
    # (e.g. show_carts or multi-page category browsing), retain non-numeric title
    # keys from the list so tapping an option on either poll resolves cleanly.
    # For pagination buttons (Previous / Categories / Next), also preserve the
    # list's numeric keys ("1".."10") so typing an item number still adds that item.
    if follows_list:
        for existing_key, existing_val in LAST_MENU_MAP[phone].items():
            if not existing_key.isdigit() or is_pagination_buttons:
                mapping[existing_key] = existing_val

    effective_options = _ensure_poll_options(options)
    poll_values: list[str] = []
    for idx, (opt_id, raw_title) in enumerate(effective_options, start=1):
        title = (raw_title or "").strip()
        numbered = f"{idx}. {title}"[:100]
        poll_values.append(numbered)

        if not is_pagination_buttons:
            mapping[str(idx)] = opt_id
        mapping[numbered] = opt_id
        mapping[numbered.lower()] = opt_id
        mapping[title] = opt_id
        mapping[title.lower()] = opt_id

    if extra_aliases:
        for alias, opt_id in extra_aliases.items():
            mapping[alias] = opt_id
            mapping[alias.lower()] = opt_id

    LAST_MENU_MAP[phone] = mapping
    _LAST_MENU_KIND[phone] = kind
    return poll_values


def resolve_menu_selection(phone: str, selection: str | None) -> str | None:
    """Resolve a user's poll vote or text reply against LAST_MENU_MAP[phone]."""
    if not selection:
        return None
    cleaned = selection.strip()
    if not cleaned:
        return None
    menu = LAST_MENU_MAP.get(phone)
    if not menu:
        return None
    return menu.get(cleaned)


def _is_evolution() -> bool:
    return settings.whatsapp_provider.strip().lower() == "evolution"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.whatsapp_access_token}"}


def _evolution_headers() -> dict[str, str]:
    return {"apikey": settings.evolution_api_key}


def _render(payload: dict) -> str:
    if payload["type"] == "text":
        return payload["text"]["body"]
    if payload["type"] in {"image", "video", "document", "audio"}:
        mtype = payload["type"]
        media_info = payload.get(mtype, {})
        caption = media_info.get("caption", "")
        link = media_info.get("link", "")
        return f"[{mtype}] {link}" + (f"\n{caption}" if caption else "")
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
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=_headers(), json=body)
    except httpx.HTTPError:
        logger.exception("WhatsApp send failed (network error)")
        return False
    if r.status_code >= 400:
        logger.error("WhatsApp send failed: %s %s", r.status_code, r.text)
        return False
    return True


async def _send_evolution(endpoint: str, body: dict) -> bool:
    base_url = settings.evolution_api_url.rstrip("/")
    instance = settings.evolution_instance.strip("/")
    url = f"{base_url}/message/{endpoint}/{instance}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=_evolution_headers(), json=body)
    except httpx.HTTPError:
        logger.exception("Evolution API send failed (%s network error)", endpoint)
        return False
    if r.status_code >= 400:
        logger.error("Evolution API send failed (%s): %s %s", endpoint, r.status_code, r.text)
        return False
    return True


async def send_text(to: str, body: str) -> bool:
    _clear_numeric_shortcuts(to)
    if settings.dry_run:
        return await _send({"to": to, "type": "text", "text": {"body": body}})
    if _is_evolution():
        return await _send_evolution(
            "sendText",
            {"number": _clean_phone(to), "text": body, "delay": EVOLUTION_DELAY_MS},
        )
    return await _send({"to": to, "type": "text", "text": {"body": body}})


async def send_media(
    to: str,
    media: str,
    caption: str = "",
    media_type: str = "image",
    file_name: str | None = None,
) -> bool:
    """Send a media file or flyer via Evolution API sendMedia or Meta Cloud API."""
    if settings.dry_run:
        return await _send({
            "to": to,
            "type": media_type,
            media_type: {"link": media, **({"caption": caption} if caption else {})},
        })
    if _is_evolution():
        payload: dict[str, Any] = {
            "number": _clean_phone(to),
            "mediatype": media_type,
            "media": media,
            "caption": caption,
            "delay": EVOLUTION_DELAY_MS,
        }
        if file_name:
            payload["fileName"] = file_name
        return await _send_evolution("sendMedia", payload)
    return await _send({
        "to": to,
        "type": media_type,
        media_type: {"link": media, **({"caption": caption} if caption else {})},
    })


async def _send_evolution_poll_or_text(to: str, body: str, poll_values: list[str]) -> bool:
    """Send a 1-tap WhatsApp Poll when 2..12 options exist, or fall back to sendText otherwise."""
    if not poll_values:
        return await send_text(to, body)
    if len(poll_values) == 1:
        option_label = poll_values[0].removeprefix("1. ").strip()
        return await _send_evolution(
            "sendText",
            {"number": _clean_phone(to), "text": f"{body}\n\nReply 1 for {option_label}", "delay": EVOLUTION_DELAY_MS},
        )
    if len(poll_values) > 12:
        options_text = "\n".join(poll_values)
        return await _send_evolution(
            "sendText",
            {"number": _clean_phone(to), "text": f"{body}\n\n{options_text}", "delay": EVOLUTION_DELAY_MS},
        )
    return await _send_evolution(
        "sendPoll",
        {
            "number": _clean_phone(to),
            "name": body[:255],
            "selectableCount": 1,
            "values": [v[:100] for v in poll_values],
            "delay": EVOLUTION_DELAY_MS,
        },
    )


async def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> bool:
    """Reply buttons: WhatsApp allows at most 3 on Meta, or a 1-tap Poll on Evolution API."""
    sliced_buttons = [(i, t[:20]) for i, t in buttons[:3]]
    poll_options = [(i, t) for i, t in buttons]
    poll_values = _populate_menu_map(to, poll_options, kind="buttons")

    if settings.dry_run:
        return await _send({
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:1024]},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": i, "title": t}} for i, t in sliced_buttons
                ]},
            },
        })
    if _is_evolution():
        return await _send_evolution_poll_or_text(to, body, poll_values)
    return await _send({
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": i, "title": t}} for i, t in sliced_buttons
            ]},
        },
    })


async def send_list(to: str, body: str, button_label: str, section_title: str, rows: list[dict]) -> bool:
    """List message: up to 10 rows on Meta, or a 1-tap Poll on Evolution API."""
    sliced_rows = [
        {"id": r["id"], "title": str(r["title"])[:24], **({"description": r["description"]} if r.get("description") else {})}
        for r in rows[:10]
    ]
    poll_rows = rows if _is_evolution() else sliced_rows
    poll_options: list[tuple[str, str]] = []
    extra_aliases: dict[str, str] = {}
    for idx, r in enumerate(poll_rows, start=1):
        row_id = str(r["id"])
        title = str(r["title"]).strip()
        desc = (r.get("description") or "").strip()
        display_title = f"{title} - {desc}" if desc else title
        poll_options.append((row_id, display_title))
        if desc:
            extra_aliases[title] = row_id
            extra_aliases[f"{idx}. {title}"] = row_id
        for alias in r.get("aliases") or ():
            clean_alias = str(alias).strip()
            if clean_alias:
                extra_aliases[clean_alias] = row_id
                extra_aliases[f"{idx}. {clean_alias}"] = row_id
                if desc:
                    extra_aliases[f"{clean_alias} - {desc}"] = row_id
                    extra_aliases[f"{idx}. {clean_alias} - {desc}"] = row_id
    poll_values = _populate_menu_map(to, poll_options, kind="list", extra_aliases=extra_aliases)

    if settings.dry_run:
        return await _send({
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body},
                "action": {
                    "button": button_label[:20],
                    "sections": [{"title": section_title[:24], "rows": sliced_rows}],
                },
            },
        })
    if _is_evolution():
        return await _send_evolution_poll_or_text(to, body, poll_values)
    return await _send({
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_label[:20],
                "sections": [{"title": section_title[:24], "rows": sliced_rows}],
            },
        },
    })


async def send_location_request(to: str, body: str) -> bool:
    if settings.dry_run:
        return await _send({
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "location_request_message",
                "body": {"text": body[:1024]},
                "action": {"name": "send_location"},
            },
        })
    if _is_evolution():
        return await send_text(to, body)
    return await _send({
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "location_request_message",
            "body": {"text": body[:1024]},
            "action": {"name": "send_location"},
        },
    })


async def download_media(media_id: str) -> tuple[bytes, str]:
    """Fetch receipt media bytes and mime_type for either Evolution API or Meta Cloud API."""
    if settings.dry_run:
        return b"", "application/octet-stream"
    if _is_evolution():
        cached = _MEDIA_CACHE.pop(media_id, None)
        if cached is not None:
            return cached
        base_url = settings.evolution_api_url.rstrip("/")
        instance = settings.evolution_instance.strip("/")
        url = f"{base_url}/chat/getBase64FromMediaMessage/{instance}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers=_evolution_headers(),
                json={"message": {"key": {"id": media_id}}, "convertToMp4": False},
            )
            resp.raise_for_status()
            data = resp.json()
            raw_b64 = (data.get("base64") or "").split(",", 1)[-1].strip()
            mime_type = data.get("mimetype") or data.get("mimeType") or "image/jpeg"
            return (base64.b64decode(raw_b64) if raw_b64 else b""), mime_type
    async with httpx.AsyncClient(timeout=30) as client:
        meta = await client.get(f"{GRAPH}/{settings.whatsapp_api_version}/{media_id}", headers=_headers())
        meta.raise_for_status()
        meta_json = meta.json()
        file = await client.get(meta_json["url"], headers=_headers())
        file.raise_for_status()
        return file.content, meta_json.get("mime_type", "application/octet-stream")


def _extract_poll_selections(item: dict) -> list[tuple[str, str | None, str | None, bool | None]]:
    """Extract `(selected_option, vote_key_id, voter_jid, from_me)` tuples from an Evolution update/upsert item."""
    results: list[tuple[str, str | None, str | None, bool | None]] = []

    def _collect_from_poll_updates(poll_updates: Any) -> None:
        if not isinstance(poll_updates, list):
            return
        for pu in poll_updates:
            if not isinstance(pu, dict):
                continue
            pu_key = pu.get("pollUpdateMessageKey") if isinstance(pu.get("pollUpdateMessageKey"), dict) else {}
            pu_id = pu_key.get("id")
            pu_jid = pu_key.get("remoteJid") or pu.get("voter")
            pu_from_me = pu_key.get("fromMe") if "fromMe" in pu_key else None
            vote = pu.get("vote")
            selected: list[Any] = []
            if isinstance(vote, dict):
                selected = vote.get("selectedOptions") or vote.get("options") or []
            elif isinstance(vote, list):
                selected = vote
            elif isinstance(pu.get("selectedOptions"), list):
                selected = pu["selectedOptions"]
            for opt in selected:
                opt_str = opt.get("name") if isinstance(opt, dict) else str(opt)
                if opt_str and opt_str.strip():
                    results.append((opt_str.strip(), pu_id, pu_jid, pu_from_me))

    update_obj = item.get("update") if isinstance(item.get("update"), dict) else {}
    msg_obj = item.get("message") if isinstance(item.get("message"), dict) else {}

    _collect_from_poll_updates(update_obj.get("pollUpdates"))
    _collect_from_poll_updates(item.get("pollUpdates"))

    for container in (
        update_obj.get("pollUpdateMessage"),
        msg_obj.get("pollUpdateMessage"),
        update_obj,
        item,
    ):
        if not isinstance(container, dict):
            continue
        vote = container.get("vote")
        selected = (
            vote.get("selectedOptions")
            if isinstance(vote, dict)
            else container.get("selectedOptions")
        )
        if isinstance(selected, list) and not results:
            for opt in selected:
                opt_str = opt.get("name") if isinstance(opt, dict) else str(opt)
                if opt_str and opt_str.strip():
                    results.append((opt_str.strip(), None, None, None))

    return results


def _resolve_whatsapp_jid(*candidates: Any) -> str:
    """Return the first valid `@s.whatsapp.net` JID from candidates (handling `@lid` / `remoteJidAlt` / `senderPn`)."""
    for cand in candidates:
        if not isinstance(cand, str):
            continue
        cleaned = cand.strip()
        if cleaned.endswith("@s.whatsapp.net"):
            return cleaned
    return ""


def _parse_evolution_incoming(payload: dict) -> list[IncomingMessage]:
    """Parse Evolution API v2 `messages.upsert` and `messages.update` webhook payloads."""
    raw_event = str(payload.get("event") or "").strip().lower().replace("_", ".")
    if raw_event not in {"messages.upsert", "messages.update"}:
        return []

    raw_data = payload.get("data")
    if isinstance(raw_data, list):
        items = [d for d in raw_data if isinstance(d, dict)]
    elif isinstance(raw_data, dict):
        if isinstance(raw_data.get("messages"), list):
            items = [d for d in raw_data["messages"] if isinstance(d, dict)]
        else:
            items = [raw_data]
    else:
        return []

    out: list[IncomingMessage] = []

    for item in items:
        key = item.get("key") if isinstance(item.get("key"), dict) else {}
        from_me = key.get("fromMe") if "fromMe" in key else item.get("fromMe", False)
        if from_me is True:
            continue

        remote_jid = _resolve_whatsapp_jid(
            key.get("remoteJid"),
            key.get("remoteJidAlt"),
            item.get("remoteJid"),
            item.get("remoteJidAlt"),
            item.get("senderPn"),
        )
        key_id = str(key.get("id") or item.get("keyId") or "").strip()

        if raw_event == "messages.update":
            poll_selections = _extract_poll_selections(item)
            if not poll_selections:
                continue
            for selected_option, vote_key_id, voter_jid, vote_from_me in poll_selections:
                if vote_from_me is True:
                    continue
                effective_jid = _resolve_whatsapp_jid(remote_jid, voter_jid)
                if not effective_jid:
                    continue
                base_id = key_id or (vote_key_id or "")
                if not base_id:
                    continue
                if vote_key_id and vote_key_id != key_id:
                    dedup_id = f"{base_id}:{vote_key_id}:{selected_option}"[:200]
                else:
                    dedup_id = f"{base_id}:{selected_option}"[:200]
                if SEEN_MESSAGE_IDS.add_or_seen(dedup_id):
                    continue
                phone = _clean_phone(effective_jid)
                resolved_id = resolve_menu_selection(phone, selected_option)
                if resolved_id is not None:
                    out.append(
                        IncomingMessage(
                            id=dedup_id,
                            sender=phone,
                            type="interactive",
                            text=selected_option,
                            reply_id=resolved_id,
                        )
                    )
                else:
                    out.append(
                        IncomingMessage(
                            id=dedup_id,
                            sender=phone,
                            type="text",
                            text=selected_option,
                            reply_id=None,
                        )
                    )
            continue

        # messages.upsert
        if not remote_jid:
            continue
        phone = _clean_phone(remote_jid)
        msg_obj = item.get("message") if isinstance(item.get("message"), dict) else {}

        # Poll vote arriving via messages.upsert
        poll_selections = _extract_poll_selections(item)
        if poll_selections:
            for selected_option, vote_key_id, _, vote_from_me in poll_selections:
                if vote_from_me is True:
                    continue
                dedup_id = key_id or (vote_key_id or "")
                if not dedup_id or SEEN_MESSAGE_IDS.add_or_seen(dedup_id):
                    continue
                resolved_id = resolve_menu_selection(phone, selected_option)
                out.append(
                    IncomingMessage(
                        id=dedup_id,
                        sender=phone,
                        type="interactive" if resolved_id is not None else "text",
                        text=selected_option,
                        reply_id=resolved_id,
                    )
                )
            continue

        # Image message (payment receipt upload)
        if "imageMessage" in msg_obj or item.get("messageType") == "imageMessage":
            if not key_id or SEEN_MESSAGE_IDS.add_or_seen(key_id):
                continue
            img_obj = msg_obj.get("imageMessage") if isinstance(msg_obj.get("imageMessage"), dict) else {}
            inline_b64 = msg_obj.get("base64") or item.get("base64") or img_obj.get("base64")
            if isinstance(inline_b64, str) and inline_b64:
                _cache_inline_media(key_id, inline_b64, img_obj.get("mimetype") or "image/jpeg")
            out.append(IncomingMessage(id=key_id, sender=phone, type="image", media_id=key_id))
            continue

        # Location message (delivery pin)
        loc_obj = msg_obj.get("locationMessage") or msg_obj.get("liveLocationMessage")
        if isinstance(loc_obj, dict) and "degreesLatitude" in loc_obj and "degreesLongitude" in loc_obj:
            if not key_id or SEEN_MESSAGE_IDS.add_or_seen(key_id):
                continue
            out.append(
                IncomingMessage(
                    id=key_id,
                    sender=phone,
                    type="location",
                    latitude=float(loc_obj["degreesLatitude"]),
                    longitude=float(loc_obj["degreesLongitude"]),
                    location_name=loc_obj.get("name"),
                    location_address=loc_obj.get("address"),
                )
            )
            continue

        # Text / button / list reply extraction
        text: str | None = None
        explicit_reply_id: str | None = None

        if isinstance(msg_obj.get("conversation"), str):
            text = msg_obj["conversation"]
        elif isinstance(msg_obj.get("extendedTextMessage"), dict) and isinstance(
            msg_obj["extendedTextMessage"].get("text"), str
        ):
            text = msg_obj["extendedTextMessage"]["text"]
        elif isinstance(msg_obj.get("buttonsResponseMessage"), dict):
            btn_resp = msg_obj["buttonsResponseMessage"]
            explicit_reply_id = btn_resp.get("selectedButtonId")
            text = btn_resp.get("selectedDisplayText")
        elif isinstance(msg_obj.get("listResponseMessage"), dict):
            list_resp = msg_obj["listResponseMessage"]
            single = list_resp.get("singleSelectReply") if isinstance(list_resp.get("singleSelectReply"), dict) else {}
            explicit_reply_id = single.get("selectedRowId")
            text = list_resp.get("title")
        elif isinstance(item.get("text"), str):
            text = item["text"]

        if text is None and explicit_reply_id is None:
            continue

        if not key_id or SEEN_MESSAGE_IDS.add_or_seen(key_id):
            continue

        resolved_id = explicit_reply_id or resolve_menu_selection(phone, text)
        if resolved_id is not None:
            out.append(
                IncomingMessage(
                    id=key_id,
                    sender=phone,
                    type="interactive",
                    text=text,
                    reply_id=resolved_id,
                )
            )
        else:
            out.append(
                IncomingMessage(
                    id=key_id,
                    sender=phone,
                    type="text",
                    text=text,
                    reply_id=None,
                )
            )

    return out


def parse_incoming(payload: dict) -> list[IncomingMessage]:
    """Flatten either Evolution API v2 or Meta Cloud API webhook payloads into IncomingMessage objects."""
    if not isinstance(payload, dict):
        return []
    if "event" in payload or ("data" in payload and "entry" not in payload):
        return _parse_evolution_incoming(payload)

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
