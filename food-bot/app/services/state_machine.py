import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote_plus

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Cart, DeliveryArea, Fulfillment, FulfillmentStage, MenuItem, PaymentBatch, Rider, SessionState, User,
)
from app.messages import (
    M1_CHOICE, M2_STREET, M3_AREA, M3B_OTHER_AREA, M4_PIN, M5_LANDMARK, M6_SECOND_PHONE, M7_NOTES,
    M8_SUMMARY, M8B_OTHER_AREA, M8C_QUOTE, M9_PAYMENT, M9_PICKUP, M10_RECEIPT, M11_APPROVED,
    M17_ON_WAY, R1_DISPATCH, R2_PICKED_UP, R3_CONFIRM_DELIVERED, R3_DELIVERED, R4_PICKUP_REMINDER,
    R5_HELP, R6_REASSIGNED, R7_WINDOW_WARNING, R_ACTIVE, R_INACTIVE,
)
from app.schemas import IncomingMessage
from app.services import menu_service, order_service, whatsapp
from app.utils import naira, normalize_phone

logger = logging.getLogger(__name__)

GREETINGS = {"menu", "hi", "hello", "hey", "start"}
MENU_PAGE_SIZE = 10
QUANTITY_PAGE_SIZE = 10


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
    """Route one customer message based on what they sent and their current state."""
    print("INCOMING", msg.type, repr(msg.reply_id), repr(msg.text), flush=True)
    rider = await get_rider_by_phone(session, msg.sender)
    if rider is not None:
        rider.last_message_at = datetime.now(timezone.utc)
        await handle_rider_message(session, rider, msg)
        return

    user = await get_or_create_user(session, msg.sender)
    text = (msg.text or "").strip().lower()
    rid = msg.reply_id or ""

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
        return await toggle_cart(session, user, int(rid.removeprefix("cart:")))
    if rid == "checkout":
        return await handle_checkout(session, user)

    if user.current_state in (
        SessionState.COLLECTING_DETAILS, SessionState.AWAITING_FEE_QUOTE,
        SessionState.AWAITING_FEE_ACCEPT, SessionState.AWAITING_PAYMENT, SessionState.AWAITING_APPROVAL,
    ):
        fulfillment = await get_open_fulfillment(session, user.id)
        if fulfillment:
            return await handle_checkout_flow(session, user, fulfillment, msg)

    if user.current_state == SessionState.AWAITING_RECEIPT and msg.type == "image" and msg.media_id:
        return await handle_receipt_upload(session, user, msg.media_id)

    await whatsapp.send_text(user.phone_number, "Hi! Send *menu* to see what's available right now.")


async def handle_rider_message(session: AsyncSession, rider: Rider, msg: IncomingMessage) -> None:
    """Handle WhatsApp messages from riders: availability, status updates, and delivery actions."""
    text = (msg.text or "").strip()
    command = text.lower()

    if command in {"active", "activate", "on"}:
        rider.is_active = True
        rider.last_message_at = datetime.now(timezone.utc)
        await whatsapp.send_text(rider.phone_number, R_ACTIVE)
        return
    if command in {"inactive", "deactivate", "off"}:
        rider.is_active = False
        rider.last_message_at = datetime.now(timezone.utc)
        await whatsapp.send_text(rider.phone_number, R_INACTIVE)
        return
    if command in {"help", "?", "commands"}:
        await whatsapp.send_text(rider.phone_number, R5_HELP)
        return
    if command == "pending":
        result = await session.execute(
            select(Fulfillment)
            .where(Fulfillment.rider_id == rider.id)
            .where(Fulfillment.stage.in_([
                FulfillmentStage.READY,
                FulfillmentStage.DISPATCHED,
                FulfillmentStage.OUT_FOR_DELIVERY,
            ]))
            .order_by(Fulfillment.id.desc())
        )
        items = result.scalars().all()
        if not items:
            await whatsapp.send_text(rider.phone_number, "You have no active deliveries right now.")
            return
        body = "Your deliveries:\n" + "\n".join(
            f"• Batch #{f.payment_batch_id} — {f.stage.value}" for f in items
        )
        await whatsapp.send_text(rider.phone_number, body)
        return
    if command.startswith("picked "):
        try:
            batch_id = int(command.split(" ", 1)[1])
        except ValueError:
            await whatsapp.send_text(rider.phone_number, "Please send `picked <batch>`.")
            return
        fulfillment = await session.execute(
            select(Fulfillment).where(Fulfillment.payment_batch_id == batch_id, Fulfillment.rider_id == rider.id)
        )
        fulfillment = fulfillment.scalar_one_or_none()
        if fulfillment is None:
            await whatsapp.send_text(rider.phone_number, "I couldn't find that batch assigned to you.")
            return
        if fulfillment.stage != FulfillmentStage.DISPATCHED:
            await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not waiting to be picked up right now.")
            return
        fulfillment.stage = FulfillmentStage.OUT_FOR_DELIVERY
        fulfillment.picked_up_at = datetime.now(timezone.utc)
        fulfillment.rider_reminder_sent_at = None
        batch = await session.get(PaymentBatch, batch_id)
        if batch and batch.user:
            await whatsapp.send_text(batch.user.phone_number, M17_ON_WAY.format(
                batch=batch.id,
                rider_name=rider.name,
                rider_phone=rider.phone_number,
            ))
        await whatsapp.send_text(rider.phone_number, R2_PICKED_UP.format(batch=batch_id))
        return
    if command.startswith("delivered "):
        try:
            batch_id = int(command.split(" ", 1)[1])
        except ValueError:
            await whatsapp.send_text(rider.phone_number, "Please send `delivered <batch>`.")
            return
        fulfillment = await session.execute(
            select(Fulfillment).where(Fulfillment.payment_batch_id == batch_id, Fulfillment.rider_id == rider.id)
        )
        fulfillment = fulfillment.scalar_one_or_none()
        if fulfillment is None:
            await whatsapp.send_text(rider.phone_number, "I couldn't find that batch assigned to you.")
            return
        if fulfillment.stage != FulfillmentStage.OUT_FOR_DELIVERY:
            await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not out for delivery right now.")
            return
        await whatsapp.send_text(rider.phone_number, R3_CONFIRM_DELIVERED.format(batch=batch_id))
        return
    if command.startswith("fail "):
        try:
            batch_id = int(command.split(" ", 1)[1])
        except ValueError:
            await whatsapp.send_text(rider.phone_number, "Please send `fail <batch>`.")
            return
        rider.pending_action = json.dumps({"action": "fail", "batch_id": batch_id})
        await whatsapp.send_text(
            rider.phone_number,
            "Please reply with the reason numbers, for example: 1,3\n1 Customer unreachable\n2 Wrong address\n3 Customer refused\n4 Wrong number\n5 Other (add a note)",
        )
        return
    if command.startswith("undo "):
        try:
            batch_id = int(command.split(" ", 1)[1])
        except ValueError:
            await whatsapp.send_text(rider.phone_number, "Please send `undo <batch>`.")
            return
        fulfillment = await session.execute(
            select(Fulfillment).where(Fulfillment.payment_batch_id == batch_id, Fulfillment.rider_id == rider.id)
        )
        fulfillment = fulfillment.scalar_one_or_none()
        if fulfillment is None:
            await whatsapp.send_text(rider.phone_number, "I couldn't find that batch assigned to you.")
            return
        if fulfillment.stage != FulfillmentStage.DELIVERED:
            await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not marked delivered.")
            return
        fulfillment.stage = FulfillmentStage.OUT_FOR_DELIVERY
        fulfillment.delivered_at = None
        fulfillment.thank_you_due_at = None
        fulfillment.thank_you_sent_at = None
        await whatsapp.send_text(rider.phone_number, f"Delivery #{batch_id} was undone and put back on the route.")
        return

    if command[:1].isdigit():
        if rider.pending_action:
            payload = json.loads(rider.pending_action)
            if payload.get("action") == "fail":
                batch_id = int(payload.get("batch_id", 0))
                reasons = [part.strip() for part in command.split(",") if part.strip()]
                fulfillment = await session.execute(
                    select(Fulfillment).where(Fulfillment.payment_batch_id == batch_id, Fulfillment.rider_id == rider.id)
                )
                fulfillment = fulfillment.scalar_one_or_none()
                if fulfillment and fulfillment.stage in {FulfillmentStage.READY, FulfillmentStage.DISPATCHED, FulfillmentStage.OUT_FOR_DELIVERY}:
                    fulfillment.stage = FulfillmentStage.FAILED
                    fulfillment.failed_at = datetime.now(timezone.utc)
                    fulfillment.failure_reasons = ", ".join(reasons)
                    fulfillment.failed_by = "rider"
                    batch = await session.get(PaymentBatch, batch_id)
                    if batch:
                        batch.status = OrderStatus.CANCELLED
                        for order in batch.orders:
                            order.status = OrderStatus.CANCELLED
                        if batch.user:
                            await whatsapp.send_text(
                                batch.user.phone_number,
                                M20_FAILED.format(
                                    batch=batch.id,
                                    items="items",
                                    reasons=fulfillment.failure_reasons,
                                    refund_timeframe=settings.refund_timeframe,
                                    support_phone=settings.support_phone,
                                ),
                            )
                    rider.pending_action = None
                    await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} marked as failed. Thanks for the update.")
                    return
        await whatsapp.send_text(rider.phone_number, R5_HELP)
        return

    if msg.reply_id == "confirm-delivery" and rider.pending_action:
        payload = json.loads(rider.pending_action)
        if payload.get("action") == "confirm_delivered":
            batch_id = int(payload.get("batch_id", 0))
            fulfillment = await session.execute(
                select(Fulfillment).where(Fulfillment.payment_batch_id == batch_id, Fulfillment.rider_id == rider.id)
            )
            fulfillment = fulfillment.scalar_one_or_none()
            if fulfillment and fulfillment.stage == FulfillmentStage.OUT_FOR_DELIVERY:
                fulfillment.stage = FulfillmentStage.DELIVERED
                fulfillment.delivered_at = datetime.now(timezone.utc)
                fulfillment.thank_you_due_at = datetime.now(timezone.utc) + timedelta(minutes=settings.thank_you_delay_minutes)
                rider.pending_action = None
                batch = await session.get(PaymentBatch, batch_id)
                if batch and batch.user:
                    await whatsapp.send_text(
                        batch.user.phone_number,
                        M18_DELIVERED.format(batch=batch.id, brand_name=settings.brand_name),
                    )
                await whatsapp.send_text(rider.phone_number, R3_DELIVERED.format(minutes=settings.thank_you_delay_minutes))
                return

    await whatsapp.send_text(rider.phone_number, R5_HELP)


async def handle_menu_request(session: AsyncSession, user: User) -> None:
    user.current_state = SessionState.BROWSING
    categories = await menu_service.get_categories(session)
    if not categories:
        await whatsapp.send_text(user.phone_number, "Sorry, nothing is available right now. Please check back soon.")
        return
    rows = [{"id": f"cat:{c}", "title": c[:24]} for c in categories]
    await whatsapp.send_list(user.phone_number, "What would you like to order?", "View menu", "Categories", rows)


async def handle_menu_request(session: AsyncSession, user: User) -> None:
    user.current_state = SessionState.BROWSING
    categories = await menu_service.get_categories(session)
    if not categories:
        await whatsapp.send_text(user.phone_number, "Sorry, nothing is available right now. Please check back soon.")
        return
    rows = [{"id": f"cat:{c}", "title": c[:24]} for c in categories]
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
    cart_ids = await order_service.get_selected_cart_ids(session, user.id)
    if not cart_ids:
        await whatsapp.send_text(
            user.phone_number,
            "No cart is selected yet. Tap a cart below to select it (✓ means selected), then press Checkout.",
        )
        await show_carts(session, user)
        return
    batch = await order_service.create_orders_from_carts(session, user, cart_ids)
    if batch is None:
        await whatsapp.send_text(user.phone_number, "Your selected carts are empty. Send *menu* to start.")
        return
    for cart_id in cart_ids:
        cart = await session.get(Cart, cart_id)
        if cart:
            cart.is_selected = False

    if batch.fulfillment is None:
        batch.fulfillment = Fulfillment(
            payment_batch_id=batch.id,
            fulfillment_type="delivery",
            stage=FulfillmentStage.COLLECTING_DETAILS,
            checkout_step="choice",
            timer_started_at=datetime.now(timezone.utc),
        )
    else:
        batch.fulfillment.fulfillment_type = "delivery"
        batch.fulfillment.stage = FulfillmentStage.COLLECTING_DETAILS
        batch.fulfillment.checkout_step = "choice"
        batch.fulfillment.timer_started_at = datetime.now(timezone.utc)
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


async def handle_checkout_flow(
    session: AsyncSession, user: User, fulfillment: Fulfillment, msg: IncomingMessage
) -> None:
    """Process a message arriving while customer is in a checkout stage."""
    text = (msg.text or "").strip()
    lowered = text.lower()
    batch = await session.get(PaymentBatch, fulfillment.payment_batch_id)
    if batch is None:
        user.current_state = SessionState.BROWSING
        await whatsapp.send_text(user.phone_number, "I couldn't find that order. Send *menu* to start again.")
        return

    if lowered == "cancel" or msg.reply_id == "cancel":
        fulfillment.stage = FulfillmentStage.CANCELLED
        fulfillment.cancelled_at = datetime.now(timezone.utc)
        batch.status = "cancelled"
        user.current_state = SessionState.BROWSING
        await whatsapp.send_text(user.phone_number, f"Order #{batch.id} has been cancelled. Send *menu* whenever you'd like to start again.")
        return

    if fulfillment.stage == FulfillmentStage.AWAITING_FEE_QUOTE:
        await whatsapp.send_text(user.phone_number, M8B_OTHER_AREA)
        return

    if fulfillment.stage == FulfillmentStage.AWAITING_FEE_ACCEPT:
        if msg.reply_id == "quote:accept":
            fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
            fulfillment.timer_started_at = datetime.now(timezone.utc)
            user.current_state = SessionState.AWAITING_PAYMENT
            await send_payment_message(user, batch, fulfillment)
        elif msg.reply_id == "quote:cancel":
            fulfillment.stage = FulfillmentStage.CANCELLED
            fulfillment.cancelled_at = datetime.now(timezone.utc)
            batch.status = "cancelled"
            user.current_state = SessionState.BROWSING
            await whatsapp.send_text(user.phone_number, f"Order #{batch.id} has been cancelled. Send *menu* whenever you'd like to start again.")
        else:
            fee = batch.delivery_fee or Decimal("0")
            await whatsapp.send_buttons(user.phone_number, M8C_QUOTE.format(
                area=fulfillment.area_name, fee=naira(fee), total=naira(batch.total_amount)
            ), [("quote:accept", "Proceed to pay"), ("quote:cancel", "Cancel order")])
        return

    if fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT:
        if msg.type == "image" and msg.media_id:
            batch.receipt_media_id = msg.media_id
            fulfillment.stage = FulfillmentStage.AWAITING_APPROVAL
            user.current_state = SessionState.AWAITING_APPROVAL
            await whatsapp.send_text(user.phone_number, M10_RECEIPT)
            return
        elif lowered == "address":
            fulfillment.address_editing = True
            fulfillment.address_edit_started_at = datetime.now(timezone.utc)
            await whatsapp.send_text(user.phone_number, "You may edit your street, landmark, second phone and notes. The area must stay the same.")
            await begin_address(session, user, fulfillment, keep_area=True)
            return
        else:
            await whatsapp.send_text(user.phone_number, "Please send your payment receipt photo, reply ADDRESS to update your address, or send cancel.")
            return

    if fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL:
        if lowered == "address":
            await begin_address(session, user, fulfillment, keep_area=True)
            return
        await whatsapp.send_text(user.phone_number, "Your order is being reviewed. You'll hear from us shortly.")
        return

    if fulfillment.stage in {FulfillmentStage.PREPARING, FulfillmentStage.READY, FulfillmentStage.DISPATCHED, FulfillmentStage.OUT_FOR_DELIVERY}:
        if lowered == "cancel":
            await whatsapp.send_text(
                user.phone_number,
                "Your order is already in progress and cannot be cancelled from here. Please contact support.",
            )
            return

    await process_address_step(session, user, fulfillment, msg)


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
            fulfillment.checkout_step = "street"
            await whatsapp.send_text(user.phone_number, M2_STREET)
        else:
            await whatsapp.send_buttons(user.phone_number, M1_CHOICE, [
                ("fulfillment:delivery", "Delivery"), ("fulfillment:pickup", "Pickup"), ("cancel", "Cancel order")
            ])
        return

    if step == "street":
        if len(text) < 5 or len(text) > 150:
            await whatsapp.send_text(user.phone_number, "Please send a street address between 5 and 150 characters, including your house or plot number.")
            return
        fulfillment.street = text
        fulfillment.checkout_step = "area"
        await send_area_page(session, user, fulfillment, 0)
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
            fulfillment._quoted_fee = area.fee
            fulfillment.checkout_step = "pin"
            await ask_pin(session, user)
            return
        await send_area_page(session, user, fulfillment, 0)
        return

    if step == "other_area":
        if not text or len(text) > 120:
            await whatsapp.send_text(user.phone_number, "Please type your area name (up to 120 characters).")
            return
        fulfillment.area_id = None
        fulfillment.area_name = text
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
            fulfillment.checkout_step = "street"
            fulfillment.street = None
            fulfillment.landmark = None
            fulfillment.phone2 = None
            fulfillment.notes = None
            await whatsapp.send_text(user.phone_number, M2_STREET)
        elif msg.reply_id == "address:confirm":
            await confirm_address(session, user, fulfillment)
        else:
            await send_summary(session, user, fulfillment)
        return


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
    """Show a summary of the collected address for confirmation."""
    batch = await session.get(PaymentBatch, fulfillment.payment_batch_id)
    if batch is None:
        return
    fee = getattr(fulfillment, "_quoted_fee", batch.delivery_fee or Decimal("0"))
    body = M8_SUMMARY.format(
        street=fulfillment.street, area=fulfillment.area_name, landmark=fulfillment.landmark,
        second_phone_line=f"Second phone: {fulfillment.phone2}\n" if fulfillment.phone2 else "",
        notes_line=f"Notes: {fulfillment.notes}\n" if fulfillment.notes else "",
        subtotal=naira(batch.total_amount - (batch.delivery_fee or Decimal("0"))),
        fee=naira(fee), total=naira(batch.total_amount + (fee if fulfillment.area_id else Decimal("0"))),
    )
    await whatsapp.send_buttons(user.phone_number, body, [("address:confirm", "Confirm"), ("address:edit", "Edit")])


async def confirm_address(session: AsyncSession, user: User, fulfillment: Fulfillment) -> None:
    """Confirm the address: set fee for known areas or move to awaiting quote for others."""
    batch = await session.get(PaymentBatch, fulfillment.payment_batch_id)
    if batch is None:
        return
    area = await session.get(DeliveryArea, fulfillment.area_id) if fulfillment.area_id else None
    if area:
        batch.delivery_fee = area.fee
        batch.total_amount = sum((order.subtotal for order in batch.orders), Decimal("0")) + area.fee
        fulfillment.expected_minutes = area.expected_minutes
        fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
        fulfillment.timer_started_at = datetime.now(timezone.utc)
        fulfillment.address_editing = False
        fulfillment.checkout_step = None
        user.current_state = SessionState.AWAITING_PAYMENT
        await send_payment_message(user, batch, fulfillment)
    else:
        fulfillment.stage = FulfillmentStage.AWAITING_FEE_QUOTE
        fulfillment.timer_started_at = None
        fulfillment.address_editing = False
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
    """Dispatch a batch to a rider and send the rider message. Returns status and optional error."""
    batch = await order_service.get_batch(session, batch_id)
    if batch is None:
        raise ValueError("Batch not found")
    fulfillment = batch.fulfillment
    if fulfillment is None:
        raise ValueError("Fulfillment record not found")

    rider = await session.get(Rider, rider_id)
    if rider is None or rider.is_archived or not rider.is_active:
        raise ValueError("Rider is not available for dispatch")
    if rider.last_message_at is not None:
        if datetime.now(timezone.utc) - rider.last_message_at > timedelta(hours=24):
            raise ValueError("Rider is inactive for dispatch")

    if fulfillment.stage not in {FulfillmentStage.READY, FulfillmentStage.DISPATCHED} and not resend:
        raise ValueError("Batch is not ready for dispatch")

    order_numbers = ", ".join(str(order.id) for order in batch.orders)
    n_items = sum(sum(item.quantity for item in order.items) for order in batch.orders)
    second_phone_line = f"Second phone: {fulfillment.phone2}\n" if fulfillment.phone2 else ""
    notes_line = f"Notes: {fulfillment.notes}\n" if fulfillment.notes else ""
    rider_message = R1_DISPATCH.format(
        batch=batch.id,
        order_numbers=order_numbers,
        name=batch.orders[0].user.name or batch.orders[0].user.phone_number,
        phone=batch.orders[0].user.phone_number,
        street=fulfillment.street or "",
        area=fulfillment.area_name or "",
        landmark=fulfillment.landmark or "",
        second_phone_line=second_phone_line,
        notes_line=notes_line,
        map_link=build_map_link(fulfillment),
        n=n_items,
        m=len(batch.orders),
    )

    sent = await whatsapp.send_text(rider.phone_number, rider_message)
    if not sent:
        fulfillment.dispatch_error = "WhatsApp send failed"
        if fulfillment.stage == FulfillmentStage.DISPATCHED:
            fulfillment.stage = FulfillmentStage.READY
        return {"status": "failed", "batch_id": batch.id, "rider_id": rider.id, "error": "send failed"}

    fulfillment.rider_id = rider.id
    fulfillment.dispatch_error = None
    fulfillment.stage = FulfillmentStage.DISPATCHED
    fulfillment.dispatched_at = datetime.now(timezone.utc)
    if fulfillment.approved_at is None:
        fulfillment.approved_at = datetime.now(timezone.utc)
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
