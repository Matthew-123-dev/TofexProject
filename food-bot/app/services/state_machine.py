import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Cart, MenuItem, SessionState, User
from app.schemas import IncomingMessage
from app.services import menu_service, order_service, whatsapp
from app.utils import naira

logger = logging.getLogger(__name__)

GREETINGS = {"menu", "hi", "hello", "hey", "start"}


async def get_or_create_user(session: AsyncSession, phone: str) -> User:
    result = await session.execute(select(User).where(User.phone_number == phone))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(phone_number=phone, current_state=SessionState.BROWSING)
        session.add(user)
        await session.flush()
    return user


async def handle_incoming(session: AsyncSession, msg: IncomingMessage) -> None:
    """Route one customer message based on what they sent and their current state."""
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
    if rid.startswith("cat:"):
        return await handle_category_selected(session, user, rid.removeprefix("cat:"))
    if rid.startswith("add:"):
        return await handle_add_item(session, user, int(rid.removeprefix("add:")))
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
    if user.current_state == SessionState.AWAITING_RECEIPT and msg.type == "image" and msg.media_id:
        return await handle_receipt_upload(session, user, msg.media_id)

    await whatsapp.send_text(user.phone_number, "Hi! Send *menu* to see what's available right now.")


async def handle_menu_request(session: AsyncSession, user: User) -> None:
    user.current_state = SessionState.BROWSING
    categories = await menu_service.get_categories(session)
    if not categories:
        await whatsapp.send_text(user.phone_number, "Sorry, nothing is available right now. Please check back soon.")
        return
    rows = [{"id": f"cat:{c}", "title": c[:24]} for c in categories]
    await whatsapp.send_list(user.phone_number, "What would you like to order?", "View menu", "Categories", rows)


async def handle_category_selected(session: AsyncSession, user: User, category: str) -> None:
    user.current_state = SessionState.ADDING_TO_CART
    items = await menu_service.get_available_items(session, category)
    if not items:
        await whatsapp.send_text(user.phone_number, f"Nothing in {category} is available right now.")
        return
    rows = [
        {"id": f"add:{i.id}", "title": i.name[:24], "description": naira(i.price)}
        for i in items  # TODO: paginate when a category has more than 10 items
    ]
    await whatsapp.send_list(user.phone_number, f"*{category}*: tap an item to add it", "Choose item", category, rows)


async def handle_add_item(session: AsyncSession, user: User, item_id: int) -> None:
    item: MenuItem | None = await menu_service.get_item(session, item_id)
    if item is None or not item.is_available:  # re-check: stock may have changed since the list was sent
        await whatsapp.send_text(user.phone_number, "Sorry, that item just ran out. Send *menu* to pick something else.")
        return
    cart = await order_service.get_or_create_cart(session, user.id)
    lines = await order_service.get_cart(session, cart.id)
    in_cart = next((c.quantity for c, m in lines if m.id == item_id), 0)
    limit = item.sellable_units  # None when stock isn't tracked
    if limit is not None and in_cart + 1 > limit:
        await whatsapp.send_buttons(
            user.phone_number,
            f"Sorry, only {limit} {item.name} available and you already have {in_cart} in your cart.",
            [("menu", "Browse menu"), ("checkout", "Checkout")],
        )
        return
    await order_service.add_to_cart(session, user.id, item_id, cart_id=cart.id)
    lines = await order_service.get_cart(session, cart.id)
    subtotal = order_service.cart_subtotal(lines)
    await whatsapp.send_buttons(
        user.phone_number,
        f"Added {item.name}. Cart: {sum(c.quantity for c, _ in lines)} item(s), {naira(subtotal)}.",
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
    user.current_state = SessionState.AWAITING_RECEIPT
    await whatsapp.send_text(
        user.phone_number,
        f"Payment batch #{batch.id} ({len(batch.orders)} orders)\n"
        f"*Total: {naira(batch.total_amount)}*\n\n"
        f"Please transfer to:\n{settings.bank_name}\n{settings.bank_account_number}\n{settings.bank_account_name}\n\n"
        "Then send a photo of your transfer receipt here.",
    )
    # TODO: collect delivery address before payment (add a state such as AWAITING_ADDRESS).


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
