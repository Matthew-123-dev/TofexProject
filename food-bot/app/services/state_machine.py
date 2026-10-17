import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote_plus

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Cart,
    DeliveryArea,
    Fulfillment,
    FulfillmentStage,
    MenuItem,
    OrderStatus,
    PaymentBatch,
    ReceiptImage,
    Rider,
    SavedAddress,
    SessionState,
    User,
)
from app.messages import (
    M1_CHOICE, M2_STREET, M3_AREA, M3B_OTHER_AREA, M4_PIN, M5_LANDMARK, M6_SECOND_PHONE, M7_NOTES,
    M8_SUMMARY, M8B_OTHER_AREA, M8C_QUOTE, M9_PAYMENT, M9_PICKUP, M10_RECEIPT, M11_APPROVED,
    M15_LOCKED, M15B_AREA_LOCKED, M17_ON_WAY, M19_NOT_RECEIVED, M20_FAILED, R1_DISPATCH, R2_PICKED_UP,
    R3_CONFIRM_DELIVERED, R3_DELIVERED, R5_HELP, R_ACTIVE, R_INACTIVE,
)
from app.schemas import IncomingMessage
from app.services import menu_service, order_service, whatsapp
from app.utils import naira, normalize_phone

logger = logging.getLogger(__name__)

GREETINGS = {"menu", "hi", "hello", "hey", "start"}
MENU_PAGE_SIZE = 10
CATEGORY_PAGE_SIZE = 9
QUANTITY_PAGE_SIZE = 10
MAX_RECEIPT_BYTES = 5 * 1024 * 1024

# While a customer is in these stages the conversation belongs to the checkout:
# the only way out is `cancel`.
CHECKOUT_LOCKED_STAGES = (
    FulfillmentStage.COLLECTING_DETAILS,
    FulfillmentStage.AWAITING_FEE_QUOTE,
    FulfillmentStage.AWAITING_FEE_ACCEPT,
)
IN_PROGRESS_STAGES = (
    FulfillmentStage.PREPARING, FulfillmentStage.READY,
    FulfillmentStage.DISPATCHED, FulfillmentStage.OUT_FOR_DELIVERY,
)
OPEN_DELIVERY_STAGES = (FulfillmentStage.READY, FulfillmentStage.DISPATCHED, FulfillmentStage.OUT_FOR_DELIVERY)

# Rider failure reasons: code -> (internal label, wording for the customer)
FAIL_REASONS = {
    "1": ("customer unreachable", "we couldn't reach you by phone"),
    "2": ("wrong address", "the address couldn't be found"),
    "3": ("customer refused", "the order wasn't accepted on delivery"),
    "4": ("wrong number", "the phone number provided was incorrect"),
    "5": ("other", None),
}
FAIL_PROMPT = (
    "Please reply with the reason numbers, for example: 1,3\n1 Customer unreachable\n2 Wrong address\n"
    "3 Customer refused\n4 Wrong number\n5 Other (add a note, e.g. 5 gate was locked)"
)


class DispatchError(ValueError):
    """A dispatch was refused for a reason staff can fix (the message is safe to show them)."""


async def get_or_create_user(session: AsyncSession, phone: str) -> User:
    result = await session.execute(select(User).where(User.phone_number == phone))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(phone_number=phone, current_state=SessionState.BROWSING)
        session.add(user)
        await session.flush()
    return user


async def get_rider_by_phone(session: AsyncSession, phone: str) -> Rider | None:
    try:
        normalized = normalize_phone(phone)
    except ValueError:
        return None
    result = await session.execute(select(Rider).where(Rider.phone_number == normalized, Rider.is_archived.is_(False)))
    return result.scalar_one_or_none()


async def handle_incoming(session: AsyncSession, msg: IncomingMessage) -> None:
    """Route one WhatsApp message: riders first, then an open checkout, then normal browsing."""
    rider = await get_rider_by_phone(session, msg.sender)
    if rider is not None:
        rider.last_message_at = datetime.now(timezone.utc)
        await handle_rider_message(session, rider, msg)
        return

    user = await get_or_create_user(session, msg.sender)
    text = (msg.text or "").strip().lower()
    rid = msg.reply_id or ""

    # An open checkout owns the conversation. We route by the ORDER, not by user.current_state,
    # so browsing the menu can never make the bot ignore a receipt photo or an address answer.
    open_fulfillment = await get_open_fulfillment(session, user.id)
    if open_fulfillment is not None and (
        open_fulfillment.stage in CHECKOUT_LOCKED_STAGES
        or open_fulfillment.address_editing
        or msg.type in ("image", "location")
        or text in {"cancel", "address"}
        or rid == "cancel"
    ):
        return await handle_checkout_flow(session, user, open_fulfillment, msg)

    if text == "address":
        if await get_in_progress_fulfillment(session, user.id) is not None:
            await whatsapp.send_text(user.phone_number, M15_LOCKED.format(support_phone=settings.support_phone))
            return

    if text == "profile":
        user.current_state = SessionState.EDITING_PROFILE
        await whatsapp.send_text(
            user.phone_number,
            f"Your saved name is {user.name or 'not set'}. Reply with `name: Your Name` to update it.",
        )
        return
    if text.startswith("name:"):
        name = (msg.text or "").split(":", 1)[1].strip()
        if not name:
            await whatsapp.send_text(user.phone_number, "Please provide a name after `name:`.")
            return
        user.name = name[:120]
        user.current_state = SessionState.BROWSING
        await whatsapp.send_text(user.phone_number, f"Saved your name as {user.name}.")
        return

    if text in GREETINGS or rid == "menu":
        return await handle_menu_request(session, user)
    if rid.startswith("notreceived:"):
        batch_text = rid.removeprefix("notreceived:")
        return await handle_not_received(session, user, int(batch_text) if batch_text.isdigit() else 0)
    if rid.startswith("menupg:"):
        menu_page_text = rid.removeprefix("menupg:")
        if not menu_page_text.isdigit():
            await whatsapp.send_text(user.phone_number, "That menu page is invalid. Send *menu* to start again.")
            return
        return await handle_menu_request(session, user, int(menu_page_text))
    if rid.startswith("catpg:"):
        category_page = rid.removeprefix("catpg:")
        if ":" not in category_page:
            await whatsapp.send_text(user.phone_number, "That menu page is invalid. Send *menu* to start again.")
            return
        category, page_text = category_page.rsplit(":", 1)
        if not page_text.isdigit():
            await whatsapp.send_text(user.phone_number, "That menu page is invalid. Send *menu* to start again.")
            return
        return await handle_category_selected(session, user, category, int(page_text))
    if rid.startswith("cat:"):
        return await handle_category_selected(session, user, rid.removeprefix("cat:"))
    if rid.startswith("add:"):
        item_id_text = rid.removeprefix("add:")
        if not item_id_text.isdigit():
            await whatsapp.send_text(user.phone_number, "That menu item is invalid. Send *menu* to start again.")
            return
        return await show_quantity_selection(session, user, int(item_id_text))
    if rid.startswith("qty:"):
        quantity_parts = rid.removeprefix("qty:").split(":")
        if len(quantity_parts) != 2 or not all(part.isdigit() for part in quantity_parts):
            await whatsapp.send_text(user.phone_number, "That quantity is invalid. Send *menu* to start again.")
            return
        return await handle_add_item(session, user, int(quantity_parts[0]), int(quantity_parts[1]))
    if rid == "carts":
        return await show_carts(session, user)
    if rid == "newcart":
        await order_service.create_cart(session, user.id)
        await whatsapp.send_text(user.phone_number, "Created a new cart. Send *menu* to add items to it.")
        return
    if rid.startswith("cart:"):
        cart_text = rid.removeprefix("cart:")
        if not cart_text.isdigit():
            await whatsapp.send_text(user.phone_number, "That cart could not be found.")
            return
        return await toggle_cart(session, user, int(cart_text))
    if rid == "checkout":
        return await handle_checkout(session, user)

    if user.current_state == SessionState.AWAITING_RECEIPT and msg.type == "image" and msg.media_id:
        return await handle_receipt_upload(session, user, msg.media_id)

    await whatsapp.send_text(user.phone_number, "Hi! Send *menu* to see what's available right now.")


async def get_in_progress_fulfillment(session: AsyncSession, user_id: int) -> Fulfillment | None:
    result = await session.execute(
        select(Fulfillment)
        .join(PaymentBatch, PaymentBatch.id == Fulfillment.payment_batch_id)
        .where(PaymentBatch.user_id == user_id, Fulfillment.stage.in_(IN_PROGRESS_STAGES))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def handle_not_received(session: AsyncSession, user: User, batch_id: int) -> None:
    """The customer tapped "Didn't receive it?" on the delivered message."""
    batch = await order_service.get_batch(session, batch_id)
    fulfillment = batch.fulfillment if batch else None
    if batch is None or batch.user_id != user.id or fulfillment is None or fulfillment.stage != FulfillmentStage.DELIVERED:
        await whatsapp.send_text(user.phone_number, "Sorry, we couldn't find that delivery. Please call " + settings.support_phone + ".")
        return
    if fulfillment.not_received_at is None:
        fulfillment.not_received_at = datetime.now(timezone.utc)
        fulfillment.not_received_acknowledged_at = None
    await whatsapp.send_text(user.phone_number, M19_NOT_RECEIVED)


def items_summary(batch: PaymentBatch) -> str:
    """"2 x Jollof rice, 1 x Chapman": for the customer's own failed-delivery message only (never sent to riders)."""
    parts = [f"{item.quantity} x {item.item_name}" for order in batch.orders for item in order.items]
    return ", ".join(parts) or "your order"


async def fail_fulfillment(
    session: AsyncSession, batch: PaymentBatch, fulfillment: Fulfillment, *,
    failed_by: str, internal_reasons: str, customer_reasons: str, note: str | None = None,
) -> None:
    """Close a delivery as failed and tell the customer. Used by both riders and staff.

    The batch stays PAID on purpose: the customer did pay, and staff need that record to handle a refund.
    """
    fulfillment.stage = FulfillmentStage.FAILED
    fulfillment.failed_at = datetime.now(timezone.utc)
    fulfillment.failure_reasons = internal_reasons
    fulfillment.failure_note = note
    fulfillment.failed_by = failed_by
    fulfillment.failed_acknowledged_at = None
    customer = batch.orders[0].user if batch.orders else None
    if customer is not None:
        await whatsapp.send_text(customer.phone_number, M20_FAILED.format(
            batch=batch.id, items=items_summary(batch), reasons=customer_reasons,
            refund_timeframe=settings.refund_timeframe, support_phone=settings.support_phone,
        ))


async def _rider_batch(
    session: AsyncSession, rider: Rider, batch_id: int
) -> tuple[PaymentBatch | None, Fulfillment | None]:
    """Load a batch (with orders, items and customer) and make sure it is assigned to this rider."""
    batch = await order_service.get_batch(session, batch_id)
    fulfillment = batch.fulfillment if batch else None
    if batch is None or fulfillment is None or fulfillment.rider_id != rider.id:
        await whatsapp.send_text(rider.phone_number, f"I couldn't find batch #{batch_id} assigned to you.")
        return None, None
    return batch, fulfillment


def _deliver_buttons(batch_id: int) -> list[tuple[str, str]]:
    return [(f"r:deliv:{batch_id}", "Delivered"), (f"r:fail:{batch_id}", "Can't deliver")]


async def rider_mark_picked(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    batch, fulfillment = await _rider_batch(session, rider, batch_id)
    if fulfillment is None:
        return
    if fulfillment.stage != FulfillmentStage.DISPATCHED:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not waiting to be picked up right now.")
        return
    fulfillment.stage = FulfillmentStage.OUT_FOR_DELIVERY
    fulfillment.picked_up_at = datetime.now(timezone.utc)
    fulfillment.rider_reminder_sent_at = None
    customer = batch.orders[0].user if batch.orders else None
    if customer is not None:
        await whatsapp.send_text(customer.phone_number, M17_ON_WAY.format(
            batch=batch.id, rider_name=rider.name, rider_phone=f"+{rider.phone_number}",
        ))
    await whatsapp.send_buttons(rider.phone_number, R2_PICKED_UP.format(batch=batch.id), _deliver_buttons(batch.id))


async def rider_ask_confirm_delivered(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    _, fulfillment = await _rider_batch(session, rider, batch_id)
    if fulfillment is None:
        return
    if fulfillment.stage != FulfillmentStage.OUT_FOR_DELIVERY:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not out for delivery right now.")
        return
    await whatsapp.send_buttons(
        rider.phone_number, R3_CONFIRM_DELIVERED.format(batch=batch_id),
        [(f"r:yes:{batch_id}", "Yes, delivered"), (f"r:no:{batch_id}", "Not yet")],
    )


async def rider_confirm_delivered(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    _, fulfillment = await _rider_batch(session, rider, batch_id)
    if fulfillment is None:
        return
    if fulfillment.stage != FulfillmentStage.OUT_FOR_DELIVERY:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not out for delivery right now.")
        return
    now = datetime.now(timezone.utc)
    fulfillment.stage = FulfillmentStage.DELIVERED
    fulfillment.delivered_at = now
    # The customer is thanked by the scheduler after this delay, so the rider can still undo a mistake.
    fulfillment.thank_you_due_at = now + timedelta(minutes=settings.thank_you_delay_minutes)
    fulfillment.thank_you_sent_at = None
    await whatsapp.send_buttons(
        rider.phone_number, R3_DELIVERED.format(minutes=settings.thank_you_delay_minutes),
        [(f"r:undo:{batch_id}", "Undo")],
    )


async def rider_undo(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    _, fulfillment = await _rider_batch(session, rider, batch_id)
    if fulfillment is None:
        return
    if fulfillment.stage != FulfillmentStage.DELIVERED:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not marked as delivered.")
        return
    if fulfillment.thank_you_sent_at is not None:
        await whatsapp.send_text(
            rider.phone_number, "It's too late to undo this: the customer has already been thanked. Please call the office."
        )
        return
    fulfillment.stage = FulfillmentStage.OUT_FOR_DELIVERY
    fulfillment.delivered_at = None
    fulfillment.thank_you_due_at = None
    await whatsapp.send_buttons(
        rider.phone_number, f"Done. Batch #{batch_id} is back on your route.", _deliver_buttons(batch_id)
    )


async def rider_begin_fail(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    _, fulfillment = await _rider_batch(session, rider, batch_id)
    if fulfillment is None:
        return
    if fulfillment.stage not in OPEN_DELIVERY_STAGES:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} can't be marked as failed right now.")
        return
    rider.pending_action = json.dumps({"action": "fail_reasons", "batch_id": batch_id})
    await whatsapp.send_text(rider.phone_number, FAIL_PROMPT)


async def rider_submit_fail_reasons(session: AsyncSession, rider: Rider, text: str, payload: dict) -> None:
    """The rider answered the numbered list, e.g. "1,3" or "5 gate was locked"."""
    batch_id = int(payload.get("batch_id", 0))
    match = re.match(r"^\s*([1-5](?:\s*,\s*[1-5])*)\s*(.*)$", text, re.S)
    if not match:
        await whatsapp.send_text(rider.phone_number, FAIL_PROMPT)
        return
    codes = list(dict.fromkeys(re.findall(r"[1-5]", match.group(1))))
    note = match.group(2).strip()[:200] or None
    labels = ", ".join(FAIL_REASONS[c][0] for c in codes)
    rider.pending_action = json.dumps({
        "action": "confirm_fail", "batch_id": batch_id, "codes": codes, "note": note,
    })
    await whatsapp.send_buttons(
        rider.phone_number,
        f"Mark batch #{batch_id} as failed?\nReason: {labels}" + (f" ({note})" if note else ""),
        [(f"r:failyes:{batch_id}", "Yes, mark failed"), (f"r:failno:{batch_id}", "Cancel")],
    )


async def rider_confirm_fail(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    try:
        payload = json.loads(rider.pending_action or "{}")
    except ValueError:
        payload = {}
    if payload.get("action") != "confirm_fail" or payload.get("batch_id") != batch_id:
        await whatsapp.send_text(rider.phone_number, f"Nothing to confirm. Send `fail {batch_id}` to start again.")
        return
    batch, fulfillment = await _rider_batch(session, rider, batch_id)
    rider.pending_action = None
    if fulfillment is None:
        return
    if fulfillment.stage not in OPEN_DELIVERY_STAGES:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} can't be marked as failed right now.")
        return
    codes = payload.get("codes", [])
    note = payload.get("note")
    customer_phrases = [FAIL_REASONS[c][1] for c in codes if FAIL_REASONS[c][1]]
    if "5" in codes:
        customer_phrases.append(note or "an unexpected problem came up")
    await fail_fulfillment(
        session, batch, fulfillment, failed_by="rider",
        internal_reasons=", ".join(FAIL_REASONS[c][0] for c in codes),
        customer_reasons="; ".join(customer_phrases) or "we could not complete the delivery", note=note,
    )
    await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} marked as failed. Thanks for the update.")


async def handle_rider_message(session: AsyncSession, rider: Rider, msg: IncomingMessage) -> None:
    """Riders talk to the bot with buttons (preferred) or typed commands. They never see customer messages."""
    text = (msg.text or "").strip()
    command = text.lower()
    rid = msg.reply_id or ""

    # ---- button taps: ids look like "r:picked:12" ----
    if rid.startswith("r:"):
        parts = rid.split(":")
        action = parts[1] if len(parts) > 1 else ""
        batch_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        if action == "picked" and batch_id:
            return await rider_mark_picked(session, rider, batch_id)
        if action == "deliv" and batch_id:
            return await rider_ask_confirm_delivered(session, rider, batch_id)
        if action == "yes" and batch_id:
            return await rider_confirm_delivered(session, rider, batch_id)
        if action == "no":
            await whatsapp.send_text(rider.phone_number, "No problem. Tap Delivered once you've handed it over.")
            return
        if action == "fail" and batch_id:
            return await rider_begin_fail(session, rider, batch_id)
        if action == "failyes" and batch_id:
            return await rider_confirm_fail(session, rider, batch_id)
        if action == "failno":
            rider.pending_action = None
            await whatsapp.send_text(rider.phone_number, "Okay, nothing was changed.")
            return
        if action == "undo" and batch_id:
            return await rider_undo(session, rider, batch_id)
        await whatsapp.send_text(rider.phone_number, R5_HELP)
        return

    # ---- simple commands ----
    if command in {"active", "activate", "on"}:
        rider.is_active = True
        await whatsapp.send_text(rider.phone_number, R_ACTIVE)
        return
    if command in {"inactive", "deactivate", "off"}:
        rider.is_active = False
        await whatsapp.send_text(rider.phone_number, R_INACTIVE)
        return
    if command in {"help", "?", "commands"}:
        await whatsapp.send_text(rider.phone_number, R5_HELP)
        return
    if command == "pending":
        result = await session.execute(
            select(Fulfillment)
            .where(Fulfillment.rider_id == rider.id, Fulfillment.stage.in_(OPEN_DELIVERY_STAGES))
            .order_by(Fulfillment.id.desc())
        )
        items = result.scalars().all()
        if not items:
            await whatsapp.send_text(rider.phone_number, "You have no active deliveries right now.")
            return
        await whatsapp.send_text(
            rider.phone_number,
            "Your deliveries:\n" + "\n".join(f"• Batch #{f.payment_batch_id}: {f.stage.value}" for f in items),
        )
        return

    typed = re.match(r"^(picked|delivered|fail|undo)\s+#?(\d+)$", command)
    if typed:
        verb, batch_id = typed.group(1), int(typed.group(2))
        if verb == "picked":
            return await rider_mark_picked(session, rider, batch_id)
        if verb == "delivered":
            return await rider_ask_confirm_delivered(session, rider, batch_id)
        if verb == "fail":
            return await rider_begin_fail(session, rider, batch_id)
        return await rider_undo(session, rider, batch_id)

    # ---- the rider is answering the "failure reasons" question ----
    if rider.pending_action and text:
        try:
            payload = json.loads(rider.pending_action)
        except ValueError:
            payload = {}
        if payload.get("action") == "fail_reasons":
            return await rider_submit_fail_reasons(session, rider, text, payload)

    await whatsapp.send_text(rider.phone_number, R5_HELP)


async def handle_menu_request(session: AsyncSession, user: User, page: int = 0) -> None:
    user.current_state = SessionState.BROWSING
    categories = await menu_service.get_categories(session)
    if not categories:
        await whatsapp.send_text(user.phone_number, "Sorry, nothing is available right now. Please check back soon.")
        return
    # A WhatsApp list holds at most 10 rows: 9 categories + 1 "More categories" row.
    total_pages = (len(categories) + CATEGORY_PAGE_SIZE - 1) // CATEGORY_PAGE_SIZE
    page = min(max(page, 0), total_pages - 1)
    start = page * CATEGORY_PAGE_SIZE
    rows = [{"id": f"cat:{c}", "title": c[:24]} for c in categories[start:start + CATEGORY_PAGE_SIZE]]
    if start + CATEGORY_PAGE_SIZE < len(categories):
        rows.append({"id": f"menupg:{page + 1}", "title": "More categories"})
    await whatsapp.send_list(user.phone_number, "What would you like to order?", "View menu", "Categories", rows)


async def handle_category_selected(
    session: AsyncSession, user: User, category: str, page: int = 0
) -> None:
    user.current_state = SessionState.ADDING_TO_CART
    items = await menu_service.get_available_items(session, category)
    if not items:
        await whatsapp.send_text(user.phone_number, f"Nothing in {category} is available right now.")
        return
    total_pages = (len(items) + MENU_PAGE_SIZE - 1) // MENU_PAGE_SIZE
    page = min(max(page, 0), total_pages - 1)
    start = page * MENU_PAGE_SIZE
    end = start + MENU_PAGE_SIZE
    rows = [
        {"id": f"add:{i.id}", "title": i.name[:24], "description": naira(i.price)}
        for i in items[start:end]
    ]
    heading = f"*{category}*: tap an item to add it"
    if total_pages > 1:
        heading = f"{heading}\nPage {page + 1}/{total_pages}"
    await whatsapp.send_list(user.phone_number, heading, "Choose item", category, rows)
    if total_pages > 1:
        buttons: list[tuple[str, str]] = []
        if page > 0:
            buttons.append((f"catpg:{category}:{page - 1}", "Previous"))
        buttons.append(("menu", "Categories"))
        if page < total_pages - 1:
            buttons.append((f"catpg:{category}:{page + 1}", "Next"))
        await whatsapp.send_buttons(
            user.phone_number,
            f"Browsing {category}: page {page + 1} of {total_pages}.",
            buttons,
        )


async def show_quantity_selection(session: AsyncSession, user: User, item_id: int) -> None:
    item: MenuItem | None = await menu_service.get_item(session, item_id)
    if item is None or not item.is_available:
        await whatsapp.send_text(
            user.phone_number,
            "Sorry, that item is no longer available. Send *menu* to pick something else.",
        )
        return
    available_quantity = item.sellable_units
    if available_quantity is not None:
        in_cart = 0
        if user.active_cart_id is not None:
            cart = await session.get(Cart, user.active_cart_id)
            if cart is not None and cart.user_id == user.id:
                lines = await order_service.get_cart(session, cart.id)
                in_cart = next((c.quantity for c, m in lines if m.id == item_id), 0)
        available_quantity = max(0, available_quantity - in_cart)
    max_quantity = QUANTITY_PAGE_SIZE if available_quantity is None else min(
        QUANTITY_PAGE_SIZE, available_quantity
    )
    if max_quantity < 1:
        await whatsapp.send_text(user.phone_number, f"Sorry, {item.name} is sold out.")
        return
    rows = [
        {
            "id": f"qty:{item.id}:{quantity}",
            "title": f"{quantity} portion{'s' if quantity != 1 else ''}",
        }
        for quantity in range(1, max_quantity + 1)
    ]
    await whatsapp.send_list(
        user.phone_number,
        f"How many portions of *{item.name}* would you like?",
        "Choose quantity",
        item.name,
        rows,
    )


async def handle_add_item(
    session: AsyncSession, user: User, item_id: int, quantity: int
) -> None:
    item: MenuItem | None = await menu_service.get_item(session, item_id)
    if item is None or not item.is_available:  # re-check: stock may have changed since the list was sent
        await whatsapp.send_text(user.phone_number, "Sorry, that item just ran out. Send *menu* to pick something else.")
        return
    if quantity < 1 or quantity > QUANTITY_PAGE_SIZE:
        await whatsapp.send_text(user.phone_number, "Please choose a quantity from the list.")
        return
    cart = await order_service.get_or_create_cart(session, user.id)
    lines = await order_service.get_cart(session, cart.id)
    in_cart = next((c.quantity for c, m in lines if m.id == item_id), 0)
    limit = item.sellable_units  # None when stock isn't tracked
    if limit is not None and in_cart + quantity > limit:
        await whatsapp.send_buttons(
            user.phone_number,
            f"Sorry, only {limit} {item.name} available and you already have {in_cart} in your cart.",
            [("menu", "Browse menu"), ("checkout", "Checkout")],
        )
        return
    await order_service.add_to_cart(session, user.id, item_id, quantity=quantity, cart_id=cart.id)
    lines = await order_service.get_cart(session, cart.id)
    subtotal = order_service.cart_subtotal(lines)
    await whatsapp.send_buttons(
        user.phone_number,
        f"Added {quantity} x {item.name}. Cart: {sum(c.quantity for c, _ in lines)} item(s), {naira(subtotal)}.",
        [("menu", "Add more"), ("newcart", "New cart"), ("carts", "Choose carts")],
    )


async def handle_checkout(session: AsyncSession, user: User) -> None:
    # One open checkout at a time. Without this, tapping an old "Checkout" button created a
    # second order and sent the customer back to the Delivery/Pickup question.
    open_fulfillment = await get_open_fulfillment(session, user.id)
    if open_fulfillment is not None:
        if open_fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL:
            text = (
                f"Your order #{open_fulfillment.payment_batch_id} is waiting for payment confirmation. "
                "You can place another order once it has been approved."
            )
        else:
            text = (
                f"You already have order #{open_fulfillment.payment_batch_id} in progress. "
                "Please finish it first, or send *cancel* to cancel it and start again."
            )
        await whatsapp.send_text(user.phone_number, text)
        return

    cart_ids = await order_service.get_selected_cart_ids(session, user.id)
    if not cart_ids:
        await whatsapp.send_text(
            user.phone_number,
            "No cart is selected yet. Tap a cart below to select it (✓ means selected), then press Checkout.",
        )
        await show_carts(session, user)
        return
    try:
        batch = await order_service.create_orders_from_carts(session, user, cart_ids)
    except order_service.CheckoutError as exc:
        await whatsapp.send_text(
            user.phone_number, f"Sorry, we can't check out yet: {exc}. Please send *menu* to update your cart."
        )
        return
    if batch is None:
        await whatsapp.send_text(user.phone_number, "Your selected carts are empty. Send *menu* to start.")
        return
    for cart_id in cart_ids:
        cart = await session.get(Cart, cart_id)
        if cart:
            cart.is_selected = False

    now = datetime.now(timezone.utc)
    if batch.fulfillment is None:
        batch.fulfillment = Fulfillment(payment_batch_id=batch.id)
    batch.fulfillment.fulfillment_type = "delivery"
    batch.fulfillment.stage = FulfillmentStage.COLLECTING_DETAILS
    batch.fulfillment.checkout_step = "choice"
    batch.fulfillment.timer_started_at = now
    batch.fulfillment.reminders_sent = "[]"
    await session.flush()

    user.current_state = SessionState.COLLECTING_DETAILS
    await whatsapp.send_buttons(user.phone_number, M1_CHOICE, [
        ("fulfillment:delivery", "Delivery"), ("fulfillment:pickup", "Pickup"), ("cancel", "Cancel order")
    ])


async def handle_receipt_upload(session: AsyncSession, user: User, media_id: str) -> None:
    order = await order_service.get_latest_pending_order(session, user.id)
    if order is None:
        await whatsapp.send_text(user.phone_number, "I couldn't find a pending order. Send *menu* to start a new one.")
        return
    batch = await order_service.get_batch(session, order.payment_batch_id)
    if batch is None:
        await whatsapp.send_text(user.phone_number, "I couldn't find the payment batch. Please contact us.")
        return
    batch.receipt_media_id = media_id
    await whatsapp.send_text(user.phone_number, "Receipt received. We're confirming your payment and will message you shortly.")


async def show_carts(session: AsyncSession, user: User) -> None:
    carts = await order_service.get_carts(session, user.id)
    rows = []
    for cart in carts:
        lines = await order_service.get_cart(session, cart.id)
        subtotal = order_service.cart_subtotal(lines)
        marker = "✓ " if cart.is_selected else ""
        rows.append({"id": f"cart:{cart.id}", "title": f"{marker}{cart.name}"[:24],
                     "description": f"{sum(c.quantity for c, _ in lines)} item(s), {naira(subtotal)}"})
    if not rows:
        await whatsapp.send_text(user.phone_number, "You have no carts yet. Send *menu* to start.")
        return
    user.current_state = SessionState.SELECTING_CARTS
    await whatsapp.send_list(
        user.phone_number,
        "A ✓ means the cart is selected for checkout. Tap a cart to select or unselect it, then choose Checkout.",
        "Select carts",
        "Your carts",
        rows,
    )
    await whatsapp.send_buttons(user.phone_number, "When ready, check out the selected carts.", [("checkout", "Checkout")])


async def toggle_cart(session: AsyncSession, user: User, cart_id: int) -> None:
    cart = await session.get(Cart, cart_id)
    if cart is None or cart.user_id != user.id:
        await whatsapp.send_text(user.phone_number, "That cart could not be found.")
        return
    cart.is_selected = not cart.is_selected
    await show_carts(session, user)


# ============================================================================
# PHASE 2: CHECKOUT FLOW (Fulfillment, Address Collection, Fee Quotes)
# ============================================================================


async def get_open_fulfillment(session: AsyncSession, user_id: int) -> Fulfillment | None:
    """Get the customer's open checkout fulfillment if one exists."""
    open_stages = (
        FulfillmentStage.COLLECTING_DETAILS, FulfillmentStage.AWAITING_FEE_QUOTE,
        FulfillmentStage.AWAITING_FEE_ACCEPT, FulfillmentStage.AWAITING_PAYMENT,
        FulfillmentStage.AWAITING_APPROVAL,
    )
    result = await session.execute(
        select(Fulfillment)
        .join(PaymentBatch)
        .where(PaymentBatch.user_id == user_id, Fulfillment.stage.in_(open_stages))
        .order_by(Fulfillment.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def cancel_checkout(session: AsyncSession, user: User, batch: PaymentBatch, fulfillment: Fulfillment) -> None:
    """Cancel an order that has not been approved yet (batch.orders must already be loaded)."""
    fulfillment.stage = FulfillmentStage.CANCELLED
    fulfillment.cancelled_at = datetime.now(timezone.utc)
    fulfillment.timer_started_at = None
    fulfillment.address_editing = False
    fulfillment.checkout_step = None
    batch.status = OrderStatus.CANCELLED
    for order in batch.orders:
        order.status = OrderStatus.CANCELLED
    user.current_state = SessionState.BROWSING
    await whatsapp.send_text(
        user.phone_number, f"Order #{batch.id} has been cancelled. Send *menu* whenever you'd like to start again."
    )


async def start_address_edit(session: AsyncSession, user: User, fulfillment: Fulfillment) -> None:
    """The customer replied ADDRESS: let them re-enter everything except the area."""
    if fulfillment.fulfillment_type == "pickup":
        await whatsapp.send_text(user.phone_number, "This is a pickup order, so there is no delivery address to change.")
        return
    fulfillment.address_editing = True
    fulfillment.address_edit_started_at = datetime.now(timezone.utc)
    if fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL:
        await whatsapp.send_text(user.phone_number, M15B_AREA_LOCKED.format(support_phone=settings.support_phone))
    else:
        await whatsapp.send_text(
            user.phone_number,
            "You can change your street, landmark, second phone and notes, but your area must stay the same. "
            f"To change your area, please call {settings.support_phone}.",
        )
    await begin_address(session, user, fulfillment, keep_area=True)


async def handle_checkout_flow(
    session: AsyncSession, user: User, fulfillment: Fulfillment, msg: IncomingMessage
) -> None:
    """Process a message arriving while the customer has an open checkout."""
    text = (msg.text or "").strip()
    lowered = text.lower()
    batch = await order_service.get_batch(session, fulfillment.payment_batch_id)
    if batch is None:
        user.current_state = SessionState.BROWSING
        await whatsapp.send_text(user.phone_number, "I couldn't find that order. Send *menu* to start again.")
        return
    stage = fulfillment.stage
    cancelling = lowered == "cancel" or msg.reply_id == "cancel"

    # Changing the address after checkout: keep answering the address questions.
    if fulfillment.address_editing and fulfillment.checkout_step:
        if cancelling:
            fulfillment.address_editing = False
            fulfillment.address_edit_started_at = None
            fulfillment.checkout_step = None
            user.current_state = (
                SessionState.AWAITING_APPROVAL if stage == FulfillmentStage.AWAITING_APPROVAL
                else SessionState.AWAITING_PAYMENT
            )
            await whatsapp.send_text(user.phone_number, "Okay, we kept your current delivery address.")
            return
        await process_address_step(session, user, fulfillment, msg)
        return

    if cancelling:
        if stage == FulfillmentStage.AWAITING_APPROVAL:
            await whatsapp.send_text(
                user.phone_number,
                f"Your payment for order #{batch.id} is already being reviewed, so it can't be cancelled here. "
                f"Please call {settings.support_phone}.",
            )
            return
        await cancel_checkout(session, user, batch, fulfillment)
        return

    if stage == FulfillmentStage.AWAITING_FEE_QUOTE:
        await whatsapp.send_text(user.phone_number, M8B_OTHER_AREA)
        return

    if stage == FulfillmentStage.AWAITING_FEE_ACCEPT:
        if msg.reply_id == "quote:accept":
            fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
            fulfillment.timer_started_at = datetime.now(timezone.utc)
            fulfillment.reminders_sent = "[]"
            user.current_state = SessionState.AWAITING_PAYMENT
            await send_payment_message(user, batch, fulfillment)
        elif msg.reply_id == "quote:cancel":
            await cancel_checkout(session, user, batch, fulfillment)
        else:
            fee = batch.delivery_fee or Decimal("0")
            await whatsapp.send_buttons(user.phone_number, M8C_QUOTE.format(
                area=fulfillment.area_name, fee=naira(fee), total=naira(batch.total_amount)
            ), [("quote:accept", "Proceed to pay"), ("quote:cancel", "Cancel order")])
        return

    if stage == FulfillmentStage.AWAITING_PAYMENT:
        if msg.type == "image" and msg.media_id:
            batch.receipt_media_id = msg.media_id
            try:
                content, mime = await whatsapp.download_media(msg.media_id)
                mime_clean = (mime or "").strip().lower()
                if content and len(content) <= MAX_RECEIPT_BYTES and mime_clean.startswith("image/"):
                    session.add(
                        ReceiptImage(
                            payment_batch_id=batch.id,
                            media_id=msg.media_id,
                            content=content,
                            mime_type=mime_clean[:100],
                            size_bytes=len(content),
                        )
                    )
                else:
                    logger.warning(
                        "Receipt media %s for batch %s not stored (size=%s, mime=%r)",
                        msg.media_id,
                        batch.id,
                        len(content) if content else 0,
                        mime,
                    )
            except Exception:
                logger.warning(
                    "Could not download receipt media %s for batch %s",
                    msg.media_id,
                    batch.id,
                    exc_info=True,
                )
            fulfillment.stage = FulfillmentStage.AWAITING_APPROVAL
            fulfillment.timer_started_at = None  # the customer has paid; from here only staff can act
            user.current_state = SessionState.AWAITING_APPROVAL
            await whatsapp.send_text(user.phone_number, M10_RECEIPT)
            return
        if lowered == "address":
            await start_address_edit(session, user, fulfillment)
            return
        await whatsapp.send_text(
            user.phone_number,
            "Please send your payment receipt photo, reply ADDRESS to update your address, or send cancel.",
        )
        return

    if stage == FulfillmentStage.AWAITING_APPROVAL:
        if lowered == "address":
            await start_address_edit(session, user, fulfillment)
            return
        await whatsapp.send_text(user.phone_number, "Your order is being reviewed. You'll hear from us shortly.")
        return

    await process_address_step(session, user, fulfillment, msg)


async def _active_saved_address(
    session: AsyncSession, user_id: int
) -> tuple[SavedAddress | None, DeliveryArea | None]:
    """Load the user's SavedAddress explicitly and verify its DeliveryArea is still active."""
    saved = (
        await session.execute(select(SavedAddress).where(SavedAddress.user_id == user_id))
    ).scalar_one_or_none()
    if saved is None or saved.area_id is None:
        return None, None
    area = await session.get(DeliveryArea, saved.area_id)
    if area is None or not area.is_active:
        return None, None
    return saved, area


async def _send_saved_address_prompt(user: User, saved: SavedAddress, area: DeliveryArea) -> None:
    await whatsapp.send_buttons(
        user.phone_number,
        f"Deliver to your saved address?\n\n📍 {saved.street}, {area.name}\nLandmark: {saved.landmark}",
        [("saved:use", "Use this address"), ("saved:new", "New address")],
    )


async def save_address(session: AsyncSession, user: User, fulfillment: Fulfillment) -> None:
    """Insert or update the customer's single SavedAddress row from a confirmed delivery fulfillment."""
    if fulfillment.fulfillment_type != "delivery" or not fulfillment.street:
        return
    saved = (
        await session.execute(select(SavedAddress).where(SavedAddress.user_id == user.id))
    ).scalar_one_or_none()
    area_name = fulfillment.area_name or ""
    landmark = fulfillment.landmark or ""
    if saved is None:
        session.add(
            SavedAddress(
                user_id=user.id,
                street=fulfillment.street,
                area_id=fulfillment.area_id,
                area_name=area_name,
                landmark=landmark,
                phone2=fulfillment.phone2,
                notes=fulfillment.notes,
                latitude=fulfillment.latitude,
                longitude=fulfillment.longitude,
            )
        )
    else:
        saved.street = fulfillment.street
        saved.area_id = fulfillment.area_id
        saved.area_name = area_name
        saved.landmark = landmark
        saved.phone2 = fulfillment.phone2
        saved.notes = fulfillment.notes
        saved.latitude = fulfillment.latitude
        saved.longitude = fulfillment.longitude
    await session.flush()


async def process_address_step(
    session: AsyncSession, user: User, fulfillment: Fulfillment, msg: IncomingMessage
) -> None:
    """Handle customer input while collecting address details step-by-step."""
    text = (msg.text or "").strip()
    step = fulfillment.checkout_step or ""

    if step == "choice":
        if msg.reply_id == "fulfillment:pickup":
            fulfillment.fulfillment_type = "pickup"
            fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
            fulfillment.checkout_step = None
            user.current_state = SessionState.AWAITING_PAYMENT
            batch = await session.get(PaymentBatch, fulfillment.payment_batch_id)
            if batch:
                await send_payment_message(user, batch, fulfillment)
        elif msg.reply_id == "fulfillment:delivery":
            fulfillment.fulfillment_type = "delivery"
            saved, area = await _active_saved_address(session, user.id)
            if saved is not None and area is not None:
                fulfillment.checkout_step = "saved_choice"
                await _send_saved_address_prompt(user, saved, area)
            else:
                fulfillment.checkout_step = "area"
                await send_area_page(session, user, fulfillment, 0)
        else:
            await whatsapp.send_buttons(user.phone_number, M1_CHOICE, [
                ("fulfillment:delivery", "Delivery"), ("fulfillment:pickup", "Pickup"), ("cancel", "Cancel order")
            ])
        return

    if step == "saved_choice":
        if msg.reply_id == "saved:new":
            fulfillment.checkout_step = "area"
            await send_area_page(session, user, fulfillment, 0)
            return
        saved, area = await _active_saved_address(session, user.id)
        if saved is None or area is None:
            fulfillment.checkout_step = "area"
            await send_area_page(session, user, fulfillment, 0)
            return
        if msg.reply_id == "saved:use":
            fulfillment.street = saved.street
            fulfillment.area_id = area.id
            fulfillment.area_name = area.name
            fulfillment.landmark = saved.landmark
            fulfillment.phone2 = saved.phone2
            fulfillment.notes = saved.notes
            fulfillment.latitude = saved.latitude
            fulfillment.longitude = saved.longitude
            fulfillment.expected_minutes = area.expected_minutes
            await confirm_address(session, user, fulfillment)
            return
        await _send_saved_address_prompt(user, saved, area)
        return

    if step == "area":
        if msg.reply_id and msg.reply_id.startswith("areas:"):
            page = int(msg.reply_id.split(":", 1)[1])
            await send_area_page(session, user, fulfillment, page)
            return
        if msg.reply_id == "area:other":
            fulfillment.checkout_step = "other_area"
            await whatsapp.send_text(user.phone_number, M3B_OTHER_AREA)
            return
        if msg.reply_id and msg.reply_id.startswith("area:"):
            area_id = int(msg.reply_id.split(":", 1)[1])
            area = await session.get(DeliveryArea, area_id)
            if area is None or not area.is_active:
                await send_area_page(session, user, fulfillment, 0)
                return
            fulfillment.area_id = area.id
            fulfillment.area_name = area.name
            fulfillment.expected_minutes = area.expected_minutes
            fulfillment.checkout_step = "street"
            await whatsapp.send_text(user.phone_number, M2_STREET)
            return
        await send_area_page(session, user, fulfillment, 0)
        return

    if step == "other_area":
        if not text or len(text) > 120:
            await whatsapp.send_text(user.phone_number, "Please type your area name (up to 120 characters).")
            return
        fulfillment.area_id = None
        fulfillment.area_name = text
        fulfillment.checkout_step = "street"
        await whatsapp.send_text(user.phone_number, M2_STREET)
        return

    if step == "street":
        if len(text) < 5 or len(text) > 150:
            await whatsapp.send_text(user.phone_number, "Please send a street address between 5 and 150 characters, including your house or plot number.")
            return
        fulfillment.street = text
        fulfillment.checkout_step = "pin"
        await ask_pin(session, user)
        return

    if step == "pin":
        if msg.type == "location" and msg.latitude is not None and msg.longitude is not None:
            fulfillment.latitude = msg.latitude
            fulfillment.longitude = msg.longitude
            fulfillment.checkout_step = "landmark"
            await whatsapp.send_text(user.phone_number, M5_LANDMARK)
        elif text.lower() == "skip" or msg.reply_id == "address:skip_pin":
            fulfillment.checkout_step = "landmark"
            await whatsapp.send_text(user.phone_number, M5_LANDMARK)
        else:
            await ask_pin(session, user)
        return

    if step == "landmark":
        if not text or len(text) > 100:
            await whatsapp.send_text(user.phone_number, "Please send a landmark up to 100 characters.")
            return
        fulfillment.landmark = text
        fulfillment.checkout_step = "phone2"
        await whatsapp.send_buttons(user.phone_number, M6_SECOND_PHONE, [("address:skip_phone", "Skip")])
        return

    if step == "phone2":
        if text.lower() == "skip" or msg.reply_id == "address:skip_phone":
            fulfillment.phone2 = None
            fulfillment.checkout_step = "notes"
            await whatsapp.send_buttons(user.phone_number, M7_NOTES, [("address:skip_notes", "Skip")])
            return
        try:
            fulfillment.phone2 = normalize_phone(text)
        except ValueError:
            await whatsapp.send_buttons(user.phone_number, "That second number is not valid. Please try again or tap Skip.", [("address:skip_phone", "Skip")])
            return
        fulfillment.checkout_step = "notes"
        await whatsapp.send_buttons(user.phone_number, M7_NOTES, [("address:skip_notes", "Skip")])
        return

    if step == "notes":
        is_skip = text.lower() == "skip" or msg.reply_id == "address:skip_notes"
        fulfillment.notes = None if is_skip else text[:200]
        fulfillment.checkout_step = "summary"
        await send_summary(session, user, fulfillment)
        return

    if step == "summary":
        if msg.reply_id == "address:edit":
            fulfillment.street = None
            fulfillment.landmark = None
            fulfillment.phone2 = None
            fulfillment.notes = None
            if fulfillment.address_editing:  # after checkout the area is locked, so restart at the street
                fulfillment.checkout_step = "street"
                await whatsapp.send_text(user.phone_number, M2_STREET)
            else:  # still checking out: start over from the area so a wrong area can be corrected
                fulfillment.area_id = None
                fulfillment.area_name = None
                fulfillment.latitude = None
                fulfillment.longitude = None
                fulfillment.checkout_step = "area"
                await send_area_page(session, user, fulfillment, 0)
        elif msg.reply_id == "address:confirm":
            await confirm_address(session, user, fulfillment)
        else:
            await send_summary(session, user, fulfillment)
        return

    # Unknown step (should never happen): never leave the customer in silence.
    await whatsapp.send_text(
        user.phone_number, "Sorry, I lost track of where we were. Please send *cancel* and start your order again."
    )


async def begin_address(session: AsyncSession, user: User, fulfillment: Fulfillment, keep_area: bool = False) -> None:
    """Start or restart address capture from street step."""
    if not keep_area:
        fulfillment.street = None
        fulfillment.area_id = None
        fulfillment.area_name = None
        fulfillment.landmark = None
        fulfillment.phone2 = None
        fulfillment.notes = None
    fulfillment.checkout_step = "street"
    user.current_state = SessionState.COLLECTING_DETAILS
    await whatsapp.send_text(user.phone_number, M2_STREET)


async def ask_pin(session: AsyncSession, user: User) -> None:
    """Send a location request and skip button for the customer."""
    await whatsapp.send_location_request(user.phone_number, M4_PIN)
    await whatsapp.send_buttons(user.phone_number, "You can share your location now, or skip this step.", [("address:skip_pin", "Skip")])


async def send_area_page(session: AsyncSession, user: User, fulfillment: Fulfillment, page: int) -> None:
    """Send a paginated list of active delivery areas."""
    areas = list((await session.execute(
        select(DeliveryArea).where(DeliveryArea.is_active.is_(True)).order_by(DeliveryArea.sort_order, DeliveryArea.name)
    )).scalars())
    start, page_size = page * 8, 8
    rows = [
        {"id": f"area:{area.id}", "title": area.name[:24], "description": f"{naira(area.fee)} · ~{area.expected_minutes} min"}
        for area in areas[start:start + page_size]
    ]
    if start + page_size < len(areas):
        rows.append({"id": f"areas:{page + 1}", "title": "More areas"})
    rows.append({"id": "area:other", "title": "Other area"})
    await whatsapp.send_list(user.phone_number, M3_AREA, "Choose area", "Delivery areas", rows)


async def send_summary(session: AsyncSession, user: User, fulfillment: Fulfillment) -> None:
    """Show the collected address, the delivery fee and the total for confirmation."""
    batch = await order_service.get_batch(session, fulfillment.payment_batch_id)
    if batch is None:
        return
    subtotal = sum((order.subtotal for order in batch.orders), Decimal("0"))
    area = await session.get(DeliveryArea, fulfillment.area_id) if fulfillment.area_id else None
    if area is not None:
        fee_text, total_text = naira(area.fee), naira(subtotal + area.fee)
    else:  # "Other area": staff will quote the fee after the customer confirms
        fee_text, total_text = "to be confirmed", f"{naira(subtotal)} + delivery fee"
    body = M8_SUMMARY.format(
        street=fulfillment.street, area=fulfillment.area_name, landmark=fulfillment.landmark,
        second_phone_line=f"Second phone: {fulfillment.phone2}\n" if fulfillment.notes or fulfillment.phone2 else "",
        notes_line=f"Notes: {fulfillment.notes}\n" if fulfillment.notes else "",
        subtotal=naira(subtotal), fee=fee_text, total=total_text,
    )
    if not fulfillment.phone2:
        body = M8_SUMMARY.format(
            street=fulfillment.street, area=fulfillment.area_name, landmark=fulfillment.landmark,
            second_phone_line="",
            notes_line=f"Notes: {fulfillment.notes}\n" if fulfillment.notes else "",
            subtotal=naira(subtotal), fee=fee_text, total=total_text,
        )
    await whatsapp.send_buttons(user.phone_number, body, [("address:confirm", "Confirm"), ("address:edit", "Edit")])


async def confirm_address(session: AsyncSession, user: User, fulfillment: Fulfillment) -> None:
    """Confirm the address: set the fee for known areas, or ask staff for a quote for other areas."""
    # get_batch loads batch.orders up front. (Reading batch.orders on a batch fetched with
    # session.get() raises MissingGreenlet in async code.)
    batch = await order_service.get_batch(session, fulfillment.payment_batch_id)
    if batch is None:
        return
    now = datetime.now(timezone.utc)

    if fulfillment.address_editing:  # a change after checkout: the order stays exactly where it was
        fulfillment.address_editing = False
        fulfillment.address_edit_started_at = None
        fulfillment.checkout_step = None
        await save_address(session, user, fulfillment)
        user.current_state = (
            SessionState.AWAITING_APPROVAL if fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL
            else SessionState.AWAITING_PAYMENT
        )
        await whatsapp.send_text(user.phone_number, "Your delivery address has been updated ✅")
        return

    await save_address(session, user, fulfillment)
    area = await session.get(DeliveryArea, fulfillment.area_id) if fulfillment.area_id else None
    if area:
        batch.delivery_fee = area.fee
        batch.total_amount = sum((order.subtotal for order in batch.orders), Decimal("0")) + area.fee
        fulfillment.expected_minutes = area.expected_minutes
        fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
        fulfillment.timer_started_at = now
        fulfillment.reminders_sent = "[]"
        fulfillment.checkout_step = None
        user.current_state = SessionState.AWAITING_PAYMENT
        await send_payment_message(user, batch, fulfillment)
    else:
        fulfillment.stage = FulfillmentStage.AWAITING_FEE_QUOTE
        fulfillment.timer_started_at = None  # paused while staff work out the fee
        fulfillment.checkout_step = None
        user.current_state = SessionState.AWAITING_FEE_QUOTE
        await whatsapp.send_text(user.phone_number, M8B_OTHER_AREA)


def build_map_link(fulfillment: Fulfillment) -> str:
    """Build a rider map link from a pin if present, or a text-search fallback."""
    if fulfillment.latitude is not None and fulfillment.longitude is not None:
        return f"https://www.google.com/maps?q={fulfillment.latitude},{fulfillment.longitude}"
    parts = [
        fulfillment.street or "",
        fulfillment.area_name or "",
        settings.maps_region_hint,
    ]
    query = ", ".join(p for p in parts if p).strip()
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"


async def dispatch_fulfillment(session: AsyncSession, batch_id: int, rider_id: int, *, resend: bool = False) -> dict:
    """Send a ready delivery to a rider. Raises DispatchError for problems staff can fix."""
    batch = await order_service.get_batch(session, batch_id)
    if batch is None:
        raise DispatchError("Order not found")
    fulfillment = batch.fulfillment
    if fulfillment is None or not batch.orders:
        raise DispatchError("This order has no delivery record")
    if fulfillment.fulfillment_type != "delivery":
        raise DispatchError("Pickup orders are not sent to riders")
    if resend and fulfillment.stage != FulfillmentStage.DISPATCHED:
        raise DispatchError("Only a dispatched delivery can be reassigned")
    if fulfillment.stage not in {FulfillmentStage.READY, FulfillmentStage.DISPATCHED}:
        raise DispatchError("Mark the order Ready before dispatching it")

    rider = await session.get(Rider, rider_id)
    if rider is None or rider.is_archived:
        raise DispatchError("Rider not found")
    now = datetime.now(timezone.utc)
    if (
        not rider.is_active
        or rider.last_message_at is None
        or now - rider.last_message_at > timedelta(hours=24)
    ):
        raise DispatchError("That rider isn't available. Ask them to send ACTIVE to the bot, then try again.")

    customer = batch.orders[0].user
    order_numbers = ", ".join(str(order.id) for order in batch.orders)
    n_items = sum(sum(item.quantity for item in order.items) for order in batch.orders)
    rider_message = R1_DISPATCH.format(
        batch=batch.id,
        order_numbers=order_numbers,
        name=customer.name or customer.phone_number,
        phone=customer.phone_number,
        street=fulfillment.street or "",
        area=fulfillment.area_name or "",
        landmark=fulfillment.landmark or "",
        second_phone_line=f"Second phone: {fulfillment.phone2}\n" if fulfillment.phone2 else "",
        notes_line=f"Notes: {fulfillment.notes}\n" if fulfillment.notes else "",
        map_link=build_map_link(fulfillment),
        n=n_items,
        m=len(batch.orders),
    )
    sent = await whatsapp.send_buttons(
        rider.phone_number, rider_message,
        [(f"r:picked:{batch.id}", "Picked up"), (f"r:fail:{batch_id}", "Can't deliver")],
    )
    if not sent:
        fulfillment.dispatch_error = "WhatsApp could not deliver the message to the rider"
        return {"status": "failed", "batch_id": batch.id, "rider_id": rider.id, "error": fulfillment.dispatch_error}

    fulfillment.rider_id = rider.id
    fulfillment.dispatch_error = None
    fulfillment.stage = FulfillmentStage.DISPATCHED
    fulfillment.dispatched_at = now
    fulfillment.rider_reminder_sent_at = None
    if fulfillment.approved_at is None:
        fulfillment.approved_at = now
    return {"status": "dispatched", "batch_id": batch.id, "rider_id": rider.id}


async def send_payment_message(user: User, batch: PaymentBatch | None, fulfillment: Fulfillment) -> None:
    """Send the payment instructions and receipt collection message."""
    if batch is None:
        return
    if fulfillment.fulfillment_type == "pickup":
        body = M9_PICKUP.format(
            outlet_address=settings.outlet_address, total=naira(batch.total_amount),
            bank_name=settings.bank_name, account_number=settings.bank_account_number,
            account_name=settings.bank_account_name, minutes=settings.checkout_cancel_minutes
        )
    else:
        body = M9_PAYMENT.format(
            total=naira(batch.total_amount), bank_name=settings.bank_name,
            account_number=settings.bank_account_number, account_name=settings.bank_account_name,
            minutes=settings.checkout_cancel_minutes
        )
    await whatsapp.send_text(user.phone_number, body)
