import enum
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app import models
from app.services import menu_service

class DispatchError(ValueError):
    pass

async def get_or_create_user(session: AsyncSession, phone: str) -> models.User:
    user = await session.execute(select(models.User).where(models.User.phone == phone))
    user = user.scalar_one_or_none()
    if user is None:
        user = models.User(phone=phone)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user

async def get_rider_by_phone(session: AsyncSession, phone: str) -> Optional[models.Rider]:
    rider = await session.execute(select(models.Rider).where(models.Rider.phone == phone))
    return rider.scalar_one_or_none()

async def handle_incoming(session: AsyncSession, msg: models.IncomingMessage) -> None:
    user = await get_or_create_user(session, msg.phone)
    if msg.text.startswith("cat:"):
        category = msg.text[4:]
        await handle_category_selected(session, user, category)
    elif msg.text.startswith("menupg:"):
        try:
            page = int(msg.text[8:])
            await handle_menu_request(session, user, page)
        except ValueError:
            pass
    elif msg.text.startswith("item:"):
        item_id = int(msg.text[5:])
        await handle_add_item(session, user, item_id)
    elif msg.text.startswith("checkout"):
        await handle_checkout(session, user)
    elif msg.text.startswith("receipt:"):
        media_id = msg.text[8:]
        await handle_receipt_upload(session, user, media_id)
    elif msg.text.startswith("cart:"):
        cart_id = int(msg.text[5:])
        await toggle_cart(session, user, cart_id)
    elif msg.text.startswith("cancel"):
        await cancel_checkout(session, user, batch=None, fulfillment=None)
    elif msg.text.startswith("address:"):
        await start_address_edit(session, user, fulfillment=None)
    elif msg.text.startswith("pin:"):
        await ask_pin(session, user)
    elif msg.text.startswith("area:"):
        page = int(msg.text[5:])
        await send_area_page(session, user, fulfillment=None, page=page)
    elif msg.text.startswith("summary"):
        await send_summary(session, user, fulfillment=None)
    elif msg.text.startswith("confirm"):
        await confirm_address(session, user, fulfillment=None)
    elif msg.text.startswith("send"):
        await send_location_request(session, user)
    elif msg.text.startswith("sendtext"):
        await send_text(to=user.phone, body="Hello, this is a test message.")
    elif msg.text.startswith("sendbuttons"):
        await send_buttons(to=user.phone, body="Hello, this is a test message.", buttons=[("Button 1", "action1"), ("Button 2", "action2")])
    elif msg.text.startswith("sendlist"):
        await send_list(to=user.phone, body="Hello, this is a test message.", button_label="Button", section_title="Section", rows=[{"title": "Row 1", "description": "Description 1"}, {"title": "Row 2", "description": "Description 2"}])
    elif msg.text.startswith("download"):
        media_id = msg.text[8:]
        await download_media(media_id)
    elif msg.text.startswith("simulate"):
        await simulate(session, user)

async def handle_not_received(session: AsyncSession, user: models.User, batch_id: int) -> None:
    fulfillment = await get_in_progress_fulfillment(session, user.id)
    if fulfillment is not None:
        await fail_fulfillment(
            session,
            batch_id=batch_id,
            fulfillment=fulfillment,
            failed_by="user",
            internal_reasons="User did not receive the order",
            customer_reasons="User did not receive the order",
        )

def items_summary(batch: models.PaymentBatch) -> str:
    return "\n".join(f"Item: {item.name}, Quantity: {item.quantity}" for item in batch.items)

async def fail_fulfillment(
    session: AsyncSession,
    batch: models.PaymentBatch,
    fulfillment: models.Fulfillment,
    *,
    failed_by: str,
    internal_reasons: str,
    customer_reasons: str,
    note: Optional[str] = None,
) -> None:
    fulfillment.status = models.FulfillmentStatus.FAILED
    fulfillment.failed_by = failed_by
    fulfillment.internal_reasons = internal_reasons
    fulfillment.customer_reasons = customer_reasons
    fulfillment.note = note
    session.add(fulfillment)
    await session.commit()

async def _rider_batch(session: AsyncSession, rider: models.Rider, batch_id: int) -> None:
    batch = await get_batch(session, batch_id)
    if batch is not None:
        batch.rider_id = rider.id
        session.add(batch)
        await session.commit()

def _deliver_buttons(batch_id: int) -> List[tuple[str, str]]:
    return [("Deliver", f"deliv:{batch_id}"), ("Cancel", f"cancel:{batch_id}")]

async def rider_mark_picked(session: AsyncSession, rider: models.Rider, batch_id: int) -> None:
    await _rider_batch(session, rider, batch_id)

async def rider_ask_confirm_delivered(session: AsyncSession, rider: models.Rider, batch_id: int) -> None:
    await _rider_batch(session, rider, batch_id)

async def rider_confirm_delivered(session: AsyncSession, rider: models.Rider, batch_id: int) -> None:
    await _rider_batch(session, rider, batch_id)

async def rider_undo(session: AsyncSession, rider: models.Rider, batch_id: int) -> None:
    await _rider_batch(session, rider, batch_id)

async def rider_begin_fail(session: AsyncSession, rider: models.Rider, batch_id: int) -> None:
    await _rider_batch(session, rider, batch_id)

async def rider_submit_fail_reasons(session: AsyncSession, rider: models.Rider, text: str, payload: dict) -> None:
    await _rider_batch(session, rider, batch_id)

async def rider_confirm_fail(session: AsyncSession, rider: models.Rider, batch_id: int) -> None:
    await _rider_batch(session, rider, batch_id)

async def handle_rider_message(session: AsyncSession, rider: models.Rider, msg: models.IncomingMessage) -> None:
    if msg.text.startswith("deliv:"):
        batch_id = int(msg.text[6:])
        await rider_confirm_delivered(session, rider, batch_id)
    elif msg.text.startswith("cancel:"):
        batch_id = int(msg.text[7:])
        await rider_undo(session, rider, batch_id)

async def handle_menu_request(session: AsyncSession, user: models.User, page: int = 0) -> None:
    categories = await menu_service.get_categories(session)
    start = page * 9
    end = start + 9
    paginated_categories = categories[start:end]
    if len(categories) > end:
        paginated_categories.append({"id": f"menupg:{page + 1}", "title": "More categories"})

    for category in paginated_categories:
        await send_area_page(session, user, fulfillment=None, page=page, title=category["title"], rows=[{"title": category["id"], "description": category["title"][:24]}])

async def handle_category_selected(session: AsyncSession, user: models.User, category: str) -> None:
    items = await menu_service.get_available_items(session, category)
    for item in items:
        await send_area_page(session, user, fulfillment=None, page=0, title=item.name, rows=[{"title": f"item:{item.id}", "description": item.description[:24]}])

async def show_quantity_selection(session: AsyncSession, user: models.User, item_id: int) -> None:
    item = await menu_service.get_item(session, item_id)
    if item is not None:
        await send_area_page(session, user, fulfillment=None, page=0, title=f"Select quantity for {item.name}", rows=[{"title": f"item:{item_id}:1", "description": "1"}, {"title": f"item:{item_id}:2", "description": "2"}, {"title": f"item:{item_id}:3", "description": "3"}])

async def handle_add_item(session: AsyncSession, user: models.User, item_id: int, quantity: int = 1) -> None:
    cart = await get_or_create_cart(session, user.id)
    await add_to_cart(session, user.id, item_id, quantity, cart_id=cart.id)

async def handle_checkout(session: AsyncSession, user: models.User) -> None:
    cart = await get_cart(session, cart_id=None)
    if cart is not None:
        await create_orders_from_carts(session, user, [cart.id])

async def handle_receipt_upload(session: AsyncSession, user: models.User, media_id: str) -> None:
    pass

async def show_carts(session: AsyncSession, user: models.User) -> None:
    carts = await get_carts(session, user_id=user.id)
    for cart in carts:
        await send_area_page(session, user, fulfillment=None, page=0, title=f"Cart {cart.id}", rows=[{"title": f"cart:{cart.id}", "description": "View cart"}])

async def toggle_cart(session: AsyncSession, user: models.User, cart_id: int) -> None:
    pass

async def get_open_fulfillment(session: AsyncSession, user_id: int) -> Optional[models.Fulfillment]:
    fulfillment = await session.execute(select(models.Fulfillment).where(models.Fulfillment.user_id == user_id).order_by(models.Fulfillment.created_at.desc()).limit(1))
    return fulfillment.scalar_one_or_none()

async def cancel_checkout(session: AsyncSession, user: models.User, batch: models.PaymentBatch, fulfillment: models.Fulfillment) -> None:
    pass

async def start_address_edit(session: AsyncSession, user: models.User, fulfillment: models.Fulfillment) -> None:
    pass

async def handle_checkout_flow(session: AsyncSession, user: models.User, fulfillment: models.Fulfillment, msg: models.IncomingMessage) -> None:
    pass

async def _active_saved_address(session: AsyncSession, user_id: int) -> Optional[models.SavedAddress]:
    saved_address = await session.execute(select(models.SavedAddress).where(models.SavedAddress.user_id == user_id).order_by(models.SavedAddress.created_at.desc()).limit(1))
    return saved_address.scalar_one_or_none()

async def _send_saved_address_prompt(user: models.User, saved: models.SavedAddress, area: models.DeliveryArea) -> None:
    pass

async def save_address(session: AsyncSession, user: models.User, fulfillment: models.Fulfillment) -> None:
    pass

async def process_address_step(session: AsyncSession, user: models.User, fulfillment: models.Fulfillment, msg: models.IncomingMessage) -> None:
    pass

async def begin_address(session: AsyncSession, user: models.User, fulfillment: models.Fulfillment, keep_area: bool = False) -> None:
    pass

async def ask_pin(session: AsyncSession, user: models.User) -> None:
    pass

async def send_area_page(session: AsyncSession, user: models.User, fulfillment: models.Fulfillment, page: int, title: str, rows: List[Dict[str, str]]) -> None:
    pass

async def send_summary(session: AsyncSession, user: models.User, fulfillment: models.Fulfillment) -> None:
    pass

async def confirm_address(session: AsyncSession, user: models.User, fulfillment: models.Fulfillment) -> None:
    pass

def build_map_link(fulfillment: models.Fulfillment) -> str:
    return f"https://maps.google.com/maps?q={fulfillment.location.latitude},{fulfillment.location.longitude}"

async def dispatch_fulfillment(session: AsyncSession, batch_id: int, rider_id: int, *, resend: bool = False) -> None:
    pass

async def send_payment_message(user: models.User, batch: models.PaymentBatch, fulfillment: models.Fulfillment) -> None:
    pass

async def simulate(session: AsyncSession, user: models.User) -> None:
    pass
