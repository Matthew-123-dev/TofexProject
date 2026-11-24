import importlib
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app import dev_outbox, seed
from app.config import settings
from app.models import (
    DeliveryArea,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
    PaymentBatch,
    Rider,
    SavedAddress,
    User,
)
from app.schemas import IncomingMessage
from app.services import menu_service, order_parser, order_service, state_machine, whatsapp

AREA_FIXTURES = [
    ("Akute", "500", 1),
    ("Lambe Junction", "600", 2),
    ("Ajuwon", "700", 3),
    ("Alagbole", "800", 4),
    ("Berger", "1000", 5),
    ("Ishashi", "700", 6),
    ("Iboko", "800", 7),
    ("Denro", "900", 8),
    ("Dolland", "1000", 9),
    ("Pipeline", "900", 10),
]


class _DummyResponse:
    def __init__(self, status_code: int = 201, data: dict | None = None):
        self.status_code = status_code
        self._data = data or {}
        self.text = ""

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        return None


def _capture_whatsapp(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def _send_text(to: str, text: str, *args, **kwargs) -> bool:
        sent.append({"kind": "text", "to": to, "text": text})
        return True

    async def _send_buttons(to: str, body: str, buttons: list[tuple[str, str]], *args, **kwargs) -> bool:
        sent.append({"kind": "buttons", "to": to, "text": body, "buttons": list(buttons)})
        return True

    async def _send_list(to: str, body: str, button_label: str, section_title: str, rows: list[dict], *args, **kwargs) -> bool:
        sent.append({"kind": "list", "to": to, "text": body, "rows": list(rows)})
        return True

    async def _send_location_request(to: str, body: str, *args, **kwargs) -> bool:
        sent.append({"kind": "location", "to": to, "text": body})
        return True

    monkeypatch.setattr(whatsapp, "send_text", _send_text)
    monkeypatch.setattr(whatsapp, "send_buttons", _send_buttons)
    monkeypatch.setattr(whatsapp, "send_list", _send_list)
    monkeypatch.setattr(whatsapp, "send_location_request", _send_location_request)
    return sent


async def _seed_menu_and_areas(db) -> tuple[dict[str, MenuItem], dict[str, DeliveryArea]]:
    existing_items = (await db.execute(select(MenuItem))).scalars().all()
    items_by_name = {m.name: m for m in existing_items}
    if not existing_items:
        for name, description, price, category, stock, threshold in seed.MENU:
            item = MenuItem(
                name=name,
                description=description or None,
                price=Decimal(price),
                category=category,
                is_available=True,
                stock_quantity=stock,
                low_stock_threshold=threshold,
            )
            db.add(item)
            items_by_name[name] = item
        await db.flush()

    existing_areas = (await db.execute(select(DeliveryArea))).scalars().all()
    areas_by_name = {a.name: a for a in existing_areas}
    if not existing_areas:
        for name, fee, sort_order in AREA_FIXTURES:
            area = DeliveryArea(
                name=name,
                fee=Decimal(fee),
                expected_minutes=30,
                is_active=True,
                sort_order=sort_order,
            )
            db.add(area)
            areas_by_name[name] = area
        await db.flush()

    await db.commit()
    return items_by_name, areas_by_name


def _msg_text(sender: str, text: str, idx: int) -> IncomingMessage:
    return IncomingMessage(id=f"txt-{sender}-{idx}", sender=sender, type="text", text=text)


def _msg_reply(sender: str, reply_id: str, idx: int) -> IncomingMessage:
    return IncomingMessage(id=f"rep-{sender}-{idx}", sender=sender, type="interactive", reply_id=reply_id)


def _evo_text(phone: str, text: str, msg_id: str) -> list[IncomingMessage]:
    return whatsapp.parse_incoming({
        "event": "messages.upsert",
        "data": {
            "key": {"id": msg_id, "remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False},
            "message": {"conversation": text},
        },
    })


def _assert_evolution_poll(payload: dict) -> None:
    values = payload["values"]
    assert 2 <= len(values) <= 12, f"Poll option count out of Evolution bounds: {len(values)} -> {values}"
    assert len(payload["name"]) <= 255, f"Poll header exceeds 255 chars: {len(payload['name'])}"
    for val in values:
        assert len(val) <= 100, f"Poll option exceeds 100 chars: {len(val)} -> {val}"


@pytest.mark.asyncio
async def test_evolution_poll_constraints_and_step1_reduced_slots(db, monkeypatch):
    importlib.reload(whatsapp)
    whatsapp.LAST_MENU_MAP.clear()
    items, areas = await _seed_menu_and_areas(db)

    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "whatsapp_provider", "evolution")
    monkeypatch.setattr(settings, "evolution_api_url", "https://evolution.example.com")
    monkeypatch.setattr(settings, "evolution_api_key", "secret")
    monkeypatch.setattr(settings, "evolution_instance", "tofex-test")

    calls: list[dict] = []

    async def fake_post(self, url, *, headers=None, json=None, **kwargs):
        calls.append({"url": url, "headers": headers, "json": json})
        return _DummyResponse(201)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    phone = "2348011110001"
    await state_machine.handle_incoming(db, _msg_text(phone, "hi", 1))
    await db.commit()

    poll_calls = [c for c in calls if "/message/sendPoll/" in c["url"]]
    assert poll_calls
    step1_poll = poll_calls[-1]["json"]
    _assert_evolution_poll(step1_poll)
    assert len(step1_poll["values"]) == 5  # 3 heroes + Categories + Talk to staff
    menu_ids = set(whatsapp.LAST_MENU_MAP[phone].values())
    assert [whatsapp.LAST_MENU_MAP[phone][str(i)] for i in range(1, 6)] == [
        "bundle:jollof_fried_chicken",
        "bundle:spag_turkey",
        f"add:{items['Shawama (2000)'].id}",
        "menupg:1",
        "staff:escalate",
    ]
    assert not any(m.startswith("cat:") for m in menu_ids)

    # Categories screen via "📂 Categories" shows all 7 categories + Go back
    await state_machine.handle_incoming(db, _msg_reply(phone, "menupg:1", 2))
    await db.commit()
    cats_poll = [c for c in calls if "/message/sendPoll/" in c["url"]][-1]["json"]
    _assert_evolution_poll(cats_poll)
    cats_ids = set(whatsapp.LAST_MENU_MAP[phone].values())
    assert {
        "cat:Food",
        "cat:Snacks",
        "cat:Drinks",
        "cat:Protein 2",
        "cat:Protein 3",
        "cat:Protein 1",
        "cat:Icecream",
        "nav:back",
    } <= cats_ids

    # Drinks category pagination (9 items/page on Evolution)
    await state_machine.handle_incoming(db, _msg_reply(phone, "cat:Drinks", 3))
    await db.commit()
    drinks_poll = [c for c in calls if "/message/sendPoll/" in c["url"]][-1]["json"]
    _assert_evolution_poll(drinks_poll)
    add_ids = [v for v in set(whatsapp.LAST_MENU_MAP[phone].values()) if v.startswith("add:")]
    assert len(add_ids) <= 9

    # Optimistic add -> Add-On Hub
    await state_machine.handle_incoming(db, _msg_reply(phone, f"add:{items['Coke'].id}", 4))
    await db.commit()
    hub_poll = [c for c in calls if "/message/sendPoll/" in c["url"]][-1]["json"]
    _assert_evolution_poll(hub_poll)

    # Checkout Page 1 Choice
    await state_machine.handle_incoming(db, _msg_reply(phone, "checkout", 5))
    await db.commit()
    choice_poll = [c for c in calls if "/message/sendPoll/" in c["url"]][-1]["json"]
    _assert_evolution_poll(choice_poll)
    choice_ids = set(whatsapp.LAST_MENU_MAP[phone].values())
    assert {"fulfillment:delivery", "fulfillment:pickup", "area:other"} <= choice_ids

    # Area Page 0 (max 5 areas) -> Page 1 -> pick Akute -> Street -> Summary
    await state_machine.handle_incoming(db, _msg_reply(phone, "fulfillment:delivery", 6))
    await db.commit()
    area_p0 = [c for c in calls if "/message/sendPoll/" in c["url"]][-1]["json"]
    _assert_evolution_poll(area_p0)
    area_p0_ids = {v for v in whatsapp.LAST_MENU_MAP[phone].values() if v.startswith("area:") and v != "area:other"}
    assert len(area_p0_ids) == 5

    await state_machine.handle_incoming(db, _msg_reply(phone, "areas:1", 7))
    await db.commit()
    area_p1 = [c for c in calls if "/message/sendPoll/" in c["url"]][-1]["json"]
    _assert_evolution_poll(area_p1)
    area_p1_ids = {v for v in whatsapp.LAST_MENU_MAP[phone].values() if v.startswith("area:") and v != "area:other"}
    assert len(area_p1_ids) == 5

    await state_machine.handle_incoming(db, _msg_reply(phone, f"area:{areas['Akute'].id}", 8))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_text(phone, "12 Unity Road", 9))
    await db.commit()
    summary_poll = [c for c in calls if "/message/sendPoll/" in c["url"]][-1]["json"]
    _assert_evolution_poll(summary_poll)


@pytest.mark.asyncio
async def test_hero_combos_and_cheapest_first_fallback(db):
    items, _areas = await _seed_menu_and_areas(db)
    catalog = list((await db.execute(select(MenuItem))).scalars().all())

    # 1. Default heroes pick Chicken (2) @ 3000, Turkey (2) @ 3500, Shawama (2000) @ 2000
    heroes = {h["id"]: h for h in menu_service.resolve_hero_options(catalog)}
    assert heroes["bundle:jollof_fried_chicken"]["total_price"] == Decimal("4200")
    assert any(it.id == items["Chicken (2)"].id for it, _qty in heroes["bundle:jollof_fried_chicken"]["items"])
    assert heroes["bundle:spag_turkey"]["total_price"] == Decimal("4700")
    assert any(it.id == items["Turkey (2)"].id for it, _qty in heroes["bundle:spag_turkey"]["items"])
    assert f"add:{items['Shawama (2000)'].id}" in heroes

    # 2. Hide Chicken (2) & Turkey (2) -> cheapest-first fallback picks Chicken (1) & Turkey (1)
    items["Chicken (2)"].is_available = False
    items["Turkey (2)"].is_available = False
    await db.flush()
    catalog2 = list((await db.execute(select(MenuItem))).scalars().all())
    heroes2 = {h["id"]: h for h in menu_service.resolve_hero_options(catalog2)}
    assert any(it.id == items["Chicken (1)"].id for it, _qty in heroes2["bundle:jollof_fried_chicken"]["items"])
    assert any(it.id == items["Turkey (1)"].id for it, _qty in heroes2["bundle:spag_turkey"]["items"])

    # 3. Also hide Chicken (1) & Turkey (1) -> falls back to Chicken (3) & Turkey (3)
    items["Chicken (1)"].is_available = False
    items["Turkey (1)"].is_available = False
    await db.flush()
    catalog3 = list((await db.execute(select(MenuItem))).scalars().all())
    heroes3 = {h["id"]: h for h in menu_service.resolve_hero_options(catalog3)}
    assert any(it.id == items["Chicken (3)"].id for it, _qty in heroes3["bundle:jollof_fried_chicken"]["items"])
    assert any(it.id == items["Turkey (3)"].id for it, _qty in heroes3["bundle:spag_turkey"]["items"])
    await db.commit()

    # 4. Customer taps bundle:jollof_fried_chicken -> inserts 2x Jollof, 1x Fried, 1x Chicken (3) + N300 pack
    phone = "2348011110002"
    await state_machine.handle_incoming(db, _msg_reply(phone, "bundle:jollof_fried_chicken", 1))
    await db.commit()
    user = (await db.execute(select(User).where(User.phone_number == phone))).scalar_one()
    _cnt, subtotal, pack_fee = await order_service.get_user_cart_totals(db, user.id)
    assert subtotal == Decimal("4700.00")  # 800 + 400 + 3500
    assert pack_fee == Decimal("300.00")


@pytest.mark.asyncio
async def test_optimistic_add_plantain_default_and_dry_run_outbox(db, monkeypatch):
    importlib.reload(whatsapp)
    whatsapp.LAST_MENU_MAP.clear()
    dev_outbox.OUTBOX.clear()
    items, _areas = await _seed_menu_and_areas(db)

    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "whatsapp_provider", "evolution")

    jollof = items["Jollof rice"]
    plantain = items["Plantain (1)"]
    assert menu_service.default_tap_quantity(jollof) == 1
    assert menu_service.default_tap_quantity(plantain) == 2

    phone = "2348011110003"
    await state_machine.handle_incoming(db, _msg_text(phone, "hi", 1))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone, f"add:{jollof.id}", 2))
    await db.commit()

    user = (await db.execute(select(User).where(User.phone_number == phone))).scalar_one()
    _cnt, subtotal, pack = await order_service.get_user_cart_totals(db, user.id)
    assert subtotal == Decimal("400.00")
    assert pack == Decimal("200.00")

    # +1 stepper -> 2x Jollof (still Small pack N200)
    await state_machine.handle_incoming(db, _msg_reply(phone, f"qty_inc:{jollof.id}:1", 3))
    await db.commit()
    _cnt, subtotal, pack = await order_service.get_user_cart_totals(db, user.id)
    assert subtotal == Decimal("800.00")
    assert pack == Decimal("200.00")

    # Change quantity -> 3x Jollof -> triggers Big pack N300
    await state_machine.handle_incoming(db, _msg_reply(phone, f"qty_menu:{jollof.id}", 4))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone, f"qtyset:{jollof.id}:3", 5))
    await db.commit()
    _cnt, subtotal, pack = await order_service.get_user_cart_totals(db, user.id)
    assert subtotal == Decimal("1200.00")
    assert pack == Decimal("300.00")

    # Tapping Plantain defaults to 2 portions
    await state_machine.handle_incoming(db, _msg_reply(phone, f"add:{plantain.id}", 6))
    await db.commit()
    _cnt, subtotal, pack = await order_service.get_user_cart_totals(db, user.id)
    assert subtotal == Decimal("1600.00")  # 1200 + 2*200
    assert pack == Decimal("300.00")
    assert len(dev_outbox.OUTBOX) >= 6

    # Stock-capped Plantain (1 unit sellable above threshold=0 -> caps default to 1)
    plantain.stock_quantity = 1
    plantain.low_stock_threshold = 0
    assert menu_service.default_tap_quantity(plantain) == 1


@pytest.mark.asyncio
async def test_returning_customer_repeat_order_2_taps_and_switch_pickup(db, monkeypatch):
    sent = _capture_whatsapp(monkeypatch)
    items, areas = await _seed_menu_and_areas(db)
    akute = areas["Akute"]

    async def _seed_returning_user(phone: str) -> User:
        u = User(phone_number=phone, name="Tolu")
        db.add(u)
        await db.flush()
        batch = PaymentBatch(
            user_id=u.id,
            total_amount=Decimal("1600.00"),
            delivery_fee=Decimal("500.00"),
            packaging_fee=Decimal("200.00"),
            status=OrderStatus.PAID,
        )
        db.add(batch)
        await db.flush()
        ord_row = Order(
            user_id=u.id,
            payment_batch_id=batch.id,
            subtotal=Decimal("900.00"),
            delivery_fee=Decimal("500.00"),
            packaging_fee=Decimal("200.00"),
            total_amount=Decimal("1600.00"),
            status=OrderStatus.PAID,
        )
        db.add(ord_row)
        await db.flush()
        jollof = items["Jollof rice"]
        coke = items["Coke"]
        db.add(OrderItem(order_id=ord_row.id, item_id=jollof.id, item_name=jollof.name, unit_price=jollof.price, quantity=1, subtotal=jollof.price))
        db.add(OrderItem(order_id=ord_row.id, item_id=coke.id, item_name=coke.name, unit_price=coke.price, quantity=1, subtotal=coke.price))
        db.add(SavedAddress(user_id=u.id, area_id=akute.id, area_name=akute.name, street="12 Balogun St", landmark="Near market"))
        await db.commit()
        return u

    # Path A: 2 taps from repeat:last -> address:confirm -> Bank transfer details
    phone_a = "2348011110004"
    user_a = await _seed_returning_user(phone_a)
    sent.clear()
    await state_machine.handle_incoming(db, _msg_text(phone_a, "hi", 1))
    await db.commit()
    greeting_rows = [r["id"] for r in sent[-1]["rows"]]
    assert greeting_rows == [
        "repeat:last",
        "bundle:jollof_fried_chicken",
        "bundle:spag_turkey",
        f"add:{items['Shawama (2000)'].id}",
        "menupg:1",
        "staff:escalate",
    ]

    # Tap 1: repeat:last -> straight to Checkout Summary (with Step 2 of 2 header)
    await state_machine.handle_incoming(db, _msg_reply(phone_a, "repeat:last", 2))
    await db.commit()
    combined = "\n".join(m["text"] for m in sent[-2:])
    assert "12 Balogun St" in combined
    assert "Step 2 of 2" in combined
    assert "1,600" in combined

    # Tap 2: address:confirm -> creates PENDING batch & sends bank details
    await state_machine.handle_incoming(db, _msg_reply(phone_a, "address:confirm", 3))
    await db.commit()
    pending_batches = (
        await db.execute(
            select(PaymentBatch)
            .join(Order)
            .where(Order.user_id == user_a.id, PaymentBatch.status == OrderStatus.PENDING)
        )
    ).scalars().all()
    assert len(pending_batches) == 1
    assert pending_batches[0].packaging_fee == Decimal("200.00")
    assert pending_batches[0].delivery_fee == Decimal("500.00")
    assert pending_batches[0].total_amount == Decimal("1600.00")
    assert any("account" in (m.get("text") or "").lower() or "transfer" in (m.get("text") or "").lower() for m in sent)

    # Path B: repeat:last -> switch to Pickup (N0 fee) -> confirm
    phone_b = "2348011110005"
    user_b = await _seed_returning_user(phone_b)
    await state_machine.handle_incoming(db, _msg_reply(phone_b, "repeat:last", 4))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone_b, "checkout:switch_pickup", 5))
    await db.commit()
    combined_pickup = "\n".join(m["text"] for m in sent[-2:])
    assert settings.outlet_address in combined_pickup
    await state_machine.handle_incoming(db, _msg_reply(phone_b, "address:confirm", 6))
    await db.commit()

    pickup_batch = (
        await db.execute(
            select(PaymentBatch)
            .join(Order)
            .where(Order.user_id == user_b.id, PaymentBatch.status == OrderStatus.PENDING)
        )
    ).scalars().one()
    assert pickup_batch.delivery_fee == Decimal("0.00")
    assert pickup_batch.packaging_fee == Decimal("200.00")
    assert pickup_batch.total_amount == Decimal("1100.00")

    # Path C: repeat:last -> checkout:new_address opens area/choice screen
    phone_c = "2348011110006"
    await _seed_returning_user(phone_c)
    await state_machine.handle_incoming(db, _msg_reply(phone_c, "repeat:last", 7))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone_c, "checkout:new_address", 8))
    await db.commit()
    new_addr_ids = {r["id"] for r in sent[-1].get("rows", [])} | {b[0] for b in sent[-1].get("buttons", [])}
    assert "area:other" in new_addr_ids or any(i.startswith("area:") for i in new_addr_ids)


@pytest.mark.asyncio
async def test_new_customer_checkout_choice_and_5_area_pagination(db, monkeypatch):
    sent = _capture_whatsapp(monkeypatch)
    items, _areas = await _seed_menu_and_areas(db)
    phone = "2348011110007"

    await state_machine.handle_incoming(db, _msg_reply(phone, f"add:{items['Coke'].id}", 1))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone, "checkout", 2))
    await db.commit()

    choice_msg = sent[-1]
    assert choice_msg["kind"] == "buttons"
    choice_ids = [b[0] for b in choice_msg["buttons"]]
    choice_titles = " ".join(b[1] for b in choice_msg["buttons"])
    assert choice_ids[:3] == ["fulfillment:delivery", "fulfillment:pickup", "area:other"]
    assert "Akute Branch (No 8, Ishashi Rd)" in choice_titles

    # Page 0 shows only first 5 areas
    await state_machine.handle_incoming(db, _msg_reply(phone, "fulfillment:delivery", 3))
    await db.commit()
    area_p0 = sent[-1]
    area_rows_p0 = [r for r in area_p0["rows"] if r["id"].startswith("area:") and r["id"] != "area:other"]
    assert len(area_rows_p0) == 5
    assert [r["title"].split(" —")[0] for r in area_rows_p0] == ["Akute", "Lambe Junction", "Ajuwon", "Alagbole", "Berger"]
    assert any(r["id"] == "areas:1" for r in area_p0["rows"])

    # Page 1 shows next 5 areas + previous link
    await state_machine.handle_incoming(db, _msg_reply(phone, "areas:1", 4))
    await db.commit()
    area_p1 = sent[-1]
    area_rows_p1 = [r for r in area_p1["rows"] if r["id"].startswith("area:") and r["id"] != "area:other"]
    assert len(area_rows_p1) == 5
    assert [r["title"].split(" —")[0] for r in area_rows_p1] == ["Ishashi", "Iboko", "Denro", "Dolland", "Pipeline"]
    assert any(r["id"] == "areas:0" for r in area_p1["rows"])

    # Pickup branch finishes cleanly with outlet address & N0 fee
    await state_machine.handle_incoming(db, _msg_reply(phone, "nav:back", 5))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone, "fulfillment:pickup", 6))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone, "address:confirm", 7))
    await db.commit()
    user = (await db.execute(select(User).where(User.phone_number == phone))).scalar_one()
    batch = (
        await db.execute(select(PaymentBatch).join(Order).where(Order.user_id == user.id))
    ).scalar_one()
    assert batch.delivery_fee == Decimal("0.00")
    assert batch.packaging_fee == Decimal("0.00")  # Coke only
    assert batch.total_amount == Decimal("500.00")


@pytest.mark.asyncio
async def test_hidden_cancel_and_help_shortcuts_and_freetext_safety(db, monkeypatch):
    importlib.reload(whatsapp)
    whatsapp.LAST_MENU_MAP.clear()
    dev_outbox.OUTBOX.clear()
    items, areas = await _seed_menu_and_areas(db)

    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "whatsapp_provider", "evolution")

    # 1. Hidden "0" cancels while browsing poll (resolved via webhook parser)
    phone1 = "2348011110008"
    await state_machine.handle_incoming(db, _msg_text(phone1, "hi", 1))
    await db.commit()
    assert whatsapp.resolve_menu_selection(phone1, "0") == "cancel"
    assert whatsapp.resolve_menu_selection(phone1, "help") == "staff:escalate"
    for parsed_msg in _evo_text(phone1, "0", "evo-cancel-0"):
        await state_machine.handle_incoming(db, parsed_msg)
    await db.commit()
    assert "cancel" in dev_outbox.OUTBOX[-1]["text"].lower() or "start again" in dev_outbox.OUTBOX[-1]["text"].lower()

    # 2. Hidden "help" triggers 2h staff takeover
    phone2 = "2348011110009"
    await state_machine.handle_incoming(db, _msg_text(phone2, "hi", 3))
    await db.commit()
    for parsed_msg in _evo_text(phone2, "help", "evo-help-1"):
        await state_machine.handle_incoming(db, parsed_msg)
    await db.commit()
    u2 = (await db.execute(select(User).where(User.phone_number == phone2))).scalar_one()
    assert u2.bot_paused_until is not None

    # 3. _clear_numeric_shortcuts ensures typing "0" in free-text street entry does NOT cancel
    phone3 = "2348011110010"
    await state_machine.handle_incoming(db, _msg_reply(phone3, f"add:{items['Jollof rice'].id}", 5))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone3, "checkout", 6))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone3, "fulfillment:delivery", 7))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone3, f"area:{areas['Akute'].id}", 8))
    await db.commit()
    assert whatsapp.resolve_menu_selection(phone3, "0") is None  # cleared by send_text!
    for parsed_msg in _evo_text(phone3, "0", "evo-street-0"):
        assert parsed_msg.type == "text"
        assert parsed_msg.reply_id is None
        await state_machine.handle_incoming(db, parsed_msg)
    await db.commit()
    u3 = (await db.execute(select(User).where(User.phone_number == phone3))).scalar_one()
    batch3 = (await db.execute(select(PaymentBatch).join(Order).where(Order.user_id == u3.id))).scalar_one()
    assert batch3.status == OrderStatus.PENDING  # order was NOT cancelled
    assert "street address" in dev_outbox.OUTBOX[-1]["text"].lower()

    # 4. Rider menu excludes customer "help" -> staff escalation; rider "help" still returns R5_HELP
    rider_phone = "2348099990001"
    db.add(Rider(name="Kola", phone_number=rider_phone, is_active=True))
    await db.commit()
    await whatsapp.send_buttons(rider_phone, "Delivery?", [("r:acc:1", "Accept"), ("r:rej:1", "Reject")])
    assert "help" not in whatsapp.LAST_MENU_MAP.get(rider_phone, {})
    dev_outbox.OUTBOX.clear()
    for parsed_msg in _evo_text(rider_phone, "help", "evo-rider-help"):
        assert parsed_msg.reply_id is None
        await state_machine.handle_incoming(db, parsed_msg)
    await db.commit()
    assert "active" in dev_outbox.OUTBOX[-1]["text"].lower()


@pytest.mark.asyncio
async def test_high_confidence_freetext_trailing_area_fast_forward(db, monkeypatch):
    sent = _capture_whatsapp(monkeypatch)
    items, areas = await _seed_menu_and_areas(db)
    catalog = list((await db.execute(select(MenuItem))).scalars().all())
    all_areas = list(areas.values())

    # 1. Verify parser extracts matched area and marks high confidence
    draft = order_parser.parse_typed_order("2 jollof rice, 1 coke to Akute", catalog, areas=all_areas)
    assert draft.is_high_confidence is True
    assert draft.matched_area_id == areas["Akute"].id
    assert draft.matched_area_name == "Akute"

    alias_draft = order_parser.parse_typed_order("1 shawama 2000 to Ojodu Berger", catalog, areas=all_areas)
    assert alias_draft.is_high_confidence is True
    assert alias_draft.matched_area_id == areas["Berger"].id

    # 2. High-confidence order skips confirmation poll, adds straight to cart, skips Area poll on checkout
    phone = "2348011110011"
    await state_machine.handle_incoming(db, _msg_text(phone, "2 jollof rice, 1 coke to Akute", 1))
    await db.commit()

    hub_msg = sent[-1]
    assert any(btn[0] == "checkout" for btn in hub_msg.get("buttons", []))
    user = (await db.execute(select(User).where(User.phone_number == phone))).scalar_one()
    _cnt, subtotal, pack = await order_service.get_user_cart_totals(db, user.id)
    assert subtotal == Decimal("1300.00")  # 2*400 + 500
    assert pack == Decimal("200.00")

    # Tapping checkout goes straight to street prompt (skips choice & area poll!)
    await state_machine.handle_incoming(db, _msg_reply(phone, "checkout", 2))
    await db.commit()
    assert sent[-1]["kind"] == "text"
    assert "street" in sent[-1]["text"].lower()

    await state_machine.handle_incoming(db, _msg_text(phone, "14 Unity Estate", 3))
    await db.commit()
    combined = "\n".join(m["text"] for m in sent[-2:])
    assert "Akute" in combined
    assert "2,000" in combined  # 1300 + 200 pack + 500 delivery
    await state_machine.handle_incoming(db, _msg_reply(phone, "address:confirm", 4))
    await db.commit()

    batch = (await db.execute(select(PaymentBatch).join(Order).where(Order.user_id == user.id))).scalar_one()
    assert batch.delivery_fee == Decimal("500.00")
    assert batch.packaging_fee == Decimal("200.00")
    assert batch.total_amount == Decimal("2000.00")


@pytest.mark.asyncio
async def test_meta_cloud_api_payload_limits(db, monkeypatch):
    importlib.reload(whatsapp)
    whatsapp.LAST_MENU_MAP.clear()
    items, _areas = await _seed_menu_and_areas(db)

    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "whatsapp_provider", "meta")
    monkeypatch.setattr(settings, "whatsapp_access_token", "meta-token-test")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "123456789")
    monkeypatch.setattr(settings, "whatsapp_api_version", "v21.0")

    calls: list[dict] = []

    async def fake_post(self, url, *, headers=None, json=None, **kwargs):
        calls.append({"url": url, "headers": headers, "json": json})
        return _DummyResponse(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    def _assert_meta_limits(payload: dict) -> None:
        if payload.get("type") != "interactive":
            return
        inter = payload["interactive"]
        if inter["type"] == "button":
            buttons = inter["action"]["buttons"]
            assert 1 <= len(buttons) <= 3
            for btn in buttons:
                assert len(btn["reply"]["title"]) <= 20
        elif inter["type"] == "list":
            rows = [r for sec in inter["action"]["sections"] for r in sec["rows"]]
            assert 1 <= len(rows) <= 10
            for r in rows:
                assert len(r["id"]) > 0
                assert len(r["title"]) <= 24
                if "description" in r:
                    assert len(r["description"]) <= 72

    phone = "2348011110012"
    for idx, msg in enumerate([
        _msg_text(phone, "hi", 1),
        _msg_reply(phone, "menupg:1", 2),
        _msg_reply(phone, "cat:Drinks", 3),
        _msg_reply(phone, f"add:{items['Jollof rice'].id}", 4),
        _msg_reply(phone, "checkout", 5),
        _msg_reply(phone, "fulfillment:delivery", 6),
    ]):
        await state_machine.handle_incoming(db, msg)
        await db.commit()
        _assert_meta_limits(calls[-1]["json"])

    # Verify Drinks category on Meta uses META_MENU_PAGE_SIZE = 7 items max on page 0
    drinks_payload = calls[2]["json"]["interactive"]
    drinks_rows = [r for sec in drinks_payload["action"]["sections"] for r in sec["rows"]]
    assert len([r for r in drinks_rows if r["id"].startswith("add:")]) <= 7


@pytest.mark.asyncio
async def test_takeaway_packaging_fee_matrix_and_custom_area_quote(db, client):
    items, _areas = await _seed_menu_and_areas(db)

    def _dummy_line(qty: int):
        return type("Line", (), {"quantity": qty})()

    # Pure matrix checks
    assert order_service.calculate_packaging_fee([(_dummy_line(2), items["Coke"]), (_dummy_line(1), items["Meatpie"]), (_dummy_line(1), items["Small cup"])]) == (Decimal("0.00"), None)
    assert order_service.calculate_packaging_fee([(_dummy_line(1), items["Jollof rice"])]) == (Decimal("200.00"), "Small")
    assert order_service.calculate_packaging_fee([(_dummy_line(2), items["Jollof rice"])]) == (Decimal("200.00"), "Small")
    assert order_service.calculate_packaging_fee([(_dummy_line(1), items["Jollof rice"]), (_dummy_line(1), items["Fried rice"])]) == (Decimal("200.00"), "Small")
    assert order_service.calculate_packaging_fee([(_dummy_line(2), items["Jollof rice"]), (_dummy_line(1), items["Beef"]), (_dummy_line(2), items["Plantain (1)"])]) == (Decimal("200.00"), "Small")
    assert order_service.calculate_packaging_fee([(_dummy_line(2), items["Jollof rice"]), (_dummy_line(1), items["Chicken (1)"])]) == (Decimal("300.00"), "Big")
    assert order_service.calculate_packaging_fee([(_dummy_line(1), items["Jollof rice"]), (_dummy_line(1), items["Fried rice"]), (_dummy_line(1), items["Turkey (2)"])]) == (Decimal("300.00"), "Big")
    assert order_service.calculate_packaging_fee([(_dummy_line(2), items["Jollof rice"]), (_dummy_line(1), items["Spaghetti"])]) == (Decimal("300.00"), "Big")
    assert order_service.calculate_packaging_fee([(_dummy_line(1), items["Chicken (2)"])]) == (Decimal("200.00"), "Small")
    assert order_service.calculate_packaging_fee([(_dummy_line(2), items["Beef"]), (_dummy_line(1), items["Plantain (1)"])]) == (Decimal("200.00"), "Small")

    # End-to-end custom area quote preserves N300 Big pack + adds staff quote fee
    phone = "2348011110013"
    await state_machine.handle_incoming(db, _msg_reply(phone, f"add:{items['Jollof rice'].id}", 1))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone, f"qty_inc:{items['Jollof rice'].id}:1", 2))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone, f"quick_add:{items['Chicken (1)'].id}", 3))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone, "checkout", 4))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone, "area:other", 5))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_text(phone, "Magboro", 6))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_text(phone, "44 Custom Lane, Magboro", 7))
    await db.commit()
    await state_machine.handle_incoming(db, _msg_reply(phone, "address:confirm", 8))
    await db.commit()

    user = (await db.execute(select(User).where(User.phone_number == phone))).scalar_one()
    batch = (await db.execute(select(PaymentBatch).join(Order).where(Order.user_id == user.id))).scalar_one()
    assert batch.packaging_fee == Decimal("300.00")
    assert batch.delivery_fee == Decimal("0.00")
    assert batch.total_amount == Decimal("3600.00")  # 800 + 2500 + 300 pack

    login_resp = await client.post(
        "/login",
        data={"username": settings.staff_username, "password": settings.staff_password},
        follow_redirects=False,
    )
    assert login_resp.status_code == 303

    quote_resp = await client.post(
        f"/api/staff/orders/{batch.id}/quote",
        json={"fee": 1200, "expected_minutes": 45},
    )
    assert quote_resp.status_code == 200

    orders_resp = await client.get("/api/staff/orders")
    assert orders_resp.status_code == 200
    matching = [o for o in orders_resp.json() if o["id"] == batch.id]
    assert len(matching) == 1
    assert Decimal(str(matching[0]["packaging_fee"])) == Decimal("300.00")
    assert Decimal(str(matching[0]["delivery_fee"])) == Decimal("1200.00")
    assert Decimal(str(matching[0]["total_amount"])) == Decimal("4800.00")  # 3300 items + 300 pack + 1200 delivery
