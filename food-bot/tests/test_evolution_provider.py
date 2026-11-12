from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.models import FulfillmentStage, SessionState
from app.services import state_machine, whatsapp


class _DummyResponse:
    def __init__(self, status_code: int = 201, text: str = "{}", json_data: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._json = json_data or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)

    def json(self) -> dict:
        return self._json


@pytest.fixture(autouse=True)
def reset_evolution_caches():
    whatsapp.LAST_MENU_MAP.clear()
    whatsapp._LAST_MENU_KIND.clear()
    whatsapp.SEEN_MESSAGE_IDS.clear()
    whatsapp._MEDIA_CACHE.clear()
    yield
    whatsapp.LAST_MENU_MAP.clear()
    whatsapp._LAST_MENU_KIND.clear()
    whatsapp.SEEN_MESSAGE_IDS.clear()
    whatsapp._MEDIA_CACHE.clear()


@pytest.mark.asyncio
async def test_evolution_outbound_text_media_and_polls(monkeypatch):
    """Verify Evolution API v2 outbound endpoints, headers, payloads, and LAST_MENU_MAP."""
    # Restore real whatsapp functions (conftest.py mocks them by default)
    import importlib
    importlib.reload(whatsapp)

    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "whatsapp_provider", "evolution")
    monkeypatch.setattr(settings, "evolution_api_url", "https://evolution.railway.app/")
    monkeypatch.setattr(settings, "evolution_api_key", "evo-secret-key")
    monkeypatch.setattr(settings, "evolution_instance", "tofex-main")

    calls: list[dict] = []

    async def fake_post(self, url, *, headers=None, json=None, **kwargs):
        calls.append({"url": url, "headers": headers, "json": json})
        return _DummyResponse(status_code=201)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    phone = "2348012345678"

    # 1. send_text -> /message/sendText/{instance}
    assert await whatsapp.send_text(phone, "Welcome to Tofex!") is True
    assert calls[-1] == {
        "url": "https://evolution.railway.app/message/sendText/tofex-main",
        "headers": {"apikey": "evo-secret-key"},
        "json": {"number": phone, "text": "Welcome to Tofex!", "delay": 1200},
    }

    # 2. send_media -> /message/sendMedia/{instance}
    assert await whatsapp.send_media(phone, "https://example.com/flyer.jpg", caption="Weekend promo") is True
    assert calls[-1] == {
        "url": "https://evolution.railway.app/message/sendMedia/tofex-main",
        "headers": {"apikey": "evo-secret-key"},
        "json": {
            "number": phone,
            "mediatype": "image",
            "media": "https://example.com/flyer.jpg",
            "caption": "Weekend promo",
            "delay": 1200,
        },
    }

    # 3. send_buttons -> /message/sendPoll/{instance} + LAST_MENU_MAP
    assert await whatsapp.send_buttons(
        phone,
        "How would you like your order?",
        [("fulfillment:delivery", "Delivery"), ("fulfillment:pickup", "Pickup"), ("cancel", "Cancel order")],
    ) is True
    assert calls[-1] == {
        "url": "https://evolution.railway.app/message/sendPoll/tofex-main",
        "headers": {"apikey": "evo-secret-key"},
        "json": {
            "number": phone,
            "name": "How would you like your order?",
            "selectableCount": 1,
            "values": ["1. Delivery", "2. Pickup", "3. Cancel order"],
            "delay": 1200,
        },
    }

    menu_map = whatsapp.LAST_MENU_MAP[phone]
    assert menu_map["1"] == "fulfillment:delivery"
    assert menu_map["1. Delivery"] == "fulfillment:delivery"
    assert menu_map["1. delivery"] == "fulfillment:delivery"
    assert menu_map["Delivery"] == "fulfillment:delivery"
    assert menu_map["DELIVERY"] == "fulfillment:delivery"
    assert menu_map["2"] == "fulfillment:pickup"
    assert menu_map["2. Pickup"] == "fulfillment:pickup"
    assert menu_map["pickup"] == "fulfillment:pickup"
    assert menu_map["3"] == "cancel"
    assert menu_map["3. Cancel order"] == "cancel"
    assert menu_map["cancel order"] == "cancel"

    # 4. send_list -> /message/sendPoll/{instance} + LAST_MENU_MAP
    rows = [
        {"id": "cat:Grills", "title": "Grills"},
        {"id": "cat:Drinks", "title": "Drinks"},
    ]
    assert await whatsapp.send_list(phone, "What would you like to order?", "View menu", "Categories", rows) is True
    assert calls[-1] == {
        "url": "https://evolution.railway.app/message/sendPoll/tofex-main",
        "headers": {"apikey": "evo-secret-key"},
        "json": {
            "number": phone,
            "name": "What would you like to order?",
            "selectableCount": 1,
            "values": ["1. Grills", "2. Drinks"],
            "delay": 1200,
        },
    }
    assert whatsapp.LAST_MENU_MAP[phone]["1"] == "cat:Grills"
    assert whatsapp.LAST_MENU_MAP[phone]["1. Grills"] == "cat:Grills"
    assert whatsapp.LAST_MENU_MAP[phone]["grills"] == "cat:Grills"
    assert whatsapp.LAST_MENU_MAP[phone]["2"] == "cat:Drinks"
    assert whatsapp.LAST_MENU_MAP[phone]["2. drinks"] == "cat:Drinks"
    assert whatsapp.LAST_MENU_MAP[phone]["DRINKS"] == "cat:Drinks"


@pytest.mark.asyncio
async def test_meta_outbound_kept_intact(monkeypatch):
    """Verify Meta Cloud API outbound behavior remains unchanged when WHATSAPP_PROVIDER == 'meta'."""
    import importlib
    importlib.reload(whatsapp)

    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "whatsapp_provider", "meta")
    monkeypatch.setattr(settings, "whatsapp_access_token", "meta-token-123")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "99887766")
    monkeypatch.setattr(settings, "whatsapp_api_version", "v21.0")

    calls: list[dict] = []

    async def fake_post(self, url, *, headers=None, json=None, **kwargs):
        calls.append({"url": url, "headers": headers, "json": json})
        return _DummyResponse(status_code=200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    phone = "2348012345678"
    assert await whatsapp.send_buttons(phone, "Confirm?", [("address:confirm", "Confirm")]) is True
    assert calls[-1]["url"] == "https://graph.facebook.com/v21.0/99887766/messages"
    assert calls[-1]["headers"] == {"Authorization": "Bearer meta-token-123"}
    assert calls[-1]["json"]["messaging_product"] == "whatsapp"
    assert calls[-1]["json"]["type"] == "interactive"
    assert calls[-1]["json"]["interactive"]["type"] == "button"


@pytest.mark.asyncio
async def test_inbound_evolution_filtering_dedup_and_resolution(client, db, user, menu_item, delivery_area, monkeypatch):
    """Verify messages.upsert, messages.update poll votes, JID/fromMe filtering, and TTLCache deduplication."""
    monkeypatch.setattr(settings, "whatsapp_provider", "evolution")
    monkeypatch.setattr(settings, "dry_run", False)

    phone = user.phone_number
    jid = f"{phone}@s.whatsapp.net"

    # Seed LAST_MENU_MAP as if the category menu poll was sent
    whatsapp._populate_menu_map(
        phone,
        [(f"cat:{menu_item.category}", menu_item.category)],
        kind="list",
    )

    # 1. Ignore fromMe == True
    from_me_payload = {
        "event": "messages.upsert",
        "instance": "tofex-main",
        "data": {
            "key": {"remoteJid": jid, "fromMe": True, "id": "evo-from-me-1"},
            "message": {"conversation": "1"},
        },
    }
    assert whatsapp.parse_incoming(from_me_payload) == []

    # 2. Ignore non-@s.whatsapp.net JIDs (groups, broadcast)
    group_payload = {
        "event": "messages.upsert",
        "instance": "tofex-main",
        "data": {
            "key": {"remoteJid": "12036304@g.us", "fromMe": False, "id": "evo-group-1"},
            "message": {"conversation": "1"},
        },
    }
    assert whatsapp.parse_incoming(group_payload) == []

    # 3. Resolve text reply ("1") in messages.upsert against LAST_MENU_MAP and deduplicate retries
    upsert_payload = {
        "event": "messages.upsert",
        "instance": "tofex-main",
        "data": {
            "key": {"remoteJid": jid, "fromMe": False, "id": "evo-msg-100"},
            "message": {"conversation": "1"},
        },
    }
    parsed_first = whatsapp.parse_incoming(upsert_payload)
    assert len(parsed_first) == 1
    assert parsed_first[0].id == "evo-msg-100"
    assert parsed_first[0].sender == phone
    assert parsed_first[0].type == "interactive"
    assert parsed_first[0].reply_id == f"cat:{menu_item.category}"

    # Duplicate key.id is ignored by BoundedTTLCache
    assert whatsapp.parse_incoming(upsert_payload) == []

    # 4. Resolve poll vote in messages.update via webhook endpoint and drive state_machine
    whatsapp._populate_menu_map(
        phone,
        [(f"add:{menu_item.id}", menu_item.name)],
        kind="list",
    )
    poll_vote_payload = {
        "event": "messages.update",
        "instance": "tofex-main",
        "data": {
            "key": {"remoteJid": jid, "fromMe": False, "id": "evo-poll-vote-200"},
            "update": {
                "pollUpdates": [
                    {
                        "pollUpdateMessageKey": {"remoteJid": jid, "fromMe": False, "id": "vote-sub-key"},
                        "vote": {"selectedOptions": [f"1. {menu_item.name}"]},
                    }
                ]
            },
        },
    }
    resp = await client.post("/webhook/whatsapp", json=poll_vote_payload)
    assert resp.status_code == 200

    # Because show_quantity_selection was triggered for menu_item.id, send_list was called with quantity rows
    assert state_machine.whatsapp.send_list.called
    call_args = state_machine.whatsapp.send_list.call_args[0]
    assert call_args[0] == phone
    assert f"*{menu_item.name}*" in call_args[1]
    assert call_args[4][0]["id"] == f"qty:{menu_item.id}:1"
