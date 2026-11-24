from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import (
    Cart, CartItem, Fulfillment, FulfillmentStage, MenuItem, Order, OrderItem, OrderStatus, PaymentBatch, User,
)
from app.services import menu_service
from app.utils import naira

PACKAGING_SMALL_FEE = Decimal("200.00")
PACKAGING_BIG_FEE = Decimal("300.00")
_PROTEIN_CATEGORIES = {"Protein 1", "Protein 2", "Protein 3"}
_CHICKEN_TURKEY_CATEGORIES = {"Protein 2", "Protein 3"}


def is_true_food_item(item: MenuItem) -> bool:
    """True for rice/spaghetti/porridge main food items (excluding moved Plantain/Moimoi extras)."""
    return item.category == "Food" and item.name not in menu_service.EXTRAS_MOVED_FROM_FOOD


def is_protein_item(item: MenuItem) -> bool:
    return item.category in _PROTEIN_CATEGORIES or item.name in menu_service.EXTRAS_MOVED_FROM_FOOD


def is_chicken_or_turkey(item: MenuItem) -> bool:
    return item.category in _CHICKEN_TURKEY_CATEGORIES or item.name.lower().startswith(("chicken", "turkey"))


def calculate_packaging_fee(lines: Sequence[tuple[Any, MenuItem]]) -> tuple[Decimal, str | None]:
    """Return `(packaging_fee, pack_size)` for one order/cart of `(line_with_qty, MenuItem)` pairs."""
    food_portions = 0
    has_food_or_protein = False
    has_poultry = False
    for line, item in lines:
        qty = getattr(line, "quantity", 0) or 0
        if qty <= 0 or item is None:
            continue
        if is_true_food_item(item):
            has_food_or_protein = True
            food_portions += qty
        elif is_protein_item(item):
            has_food_or_protein = True
        if is_chicken_or_turkey(item):
            has_food_or_protein = True
            has_poultry = True
    if not has_food_or_protein:
        return Decimal("0.00"), None
    if food_portions > 2 or (food_portions == 2 and has_poultry):
        return PACKAGING_BIG_FEE, "Big"
    return PACKAGING_SMALL_FEE, "Small"


class CheckoutError(Exception):
    """Raised when a cart cannot be checked out (item sold out, not enough stock)."""


async def get_or_create_cart(session: AsyncSession, user_id: int) -> Cart:
    user = await session.get(User, user_id)
    if user and user.active_cart_id:
        active = await session.get(Cart, user.active_cart_id)
        if active and active.user_id == user_id:
            return active
    result = await session.execute(
        select(Cart).where(Cart.user_id == user_id).order_by(Cart.id)
    )
    cart = result.scalars().first()
    if cart is None:
        cart = Cart(user_id=user_id, name="Cart 1")
        session.add(cart)
        await session.flush()
    if user:
        user.active_cart_id = cart.id
    return cart


async def create_cart(session: AsyncSession, user_id: int) -> Cart:
    count = await session.scalar(select(func.count()).select_from(Cart).where(Cart.user_id == user_id))
    cart = Cart(user_id=user_id, name=f"Cart {(count or 0) + 1}")
    session.add(cart)
    await session.flush()
    user = await session.get(User, user_id)
    if user:
        user.active_cart_id = cart.id
    return cart


async def get_carts(session: AsyncSession, user_id: int) -> list[Cart]:
    result = await session.execute(select(Cart).where(Cart.user_id == user_id).order_by(Cart.id))
    return list(result.scalars())


async def add_to_cart(
    session: AsyncSession, user_id: int, item_id: int, quantity: int = 1, cart_id: int | None = None
) -> None:
    if cart_id is None:
        cart = await get_or_create_cart(session, user_id)
    else:
        cart = await session.get(Cart, cart_id)
        if cart is None or cart.user_id != user_id:
            raise ValueError("Cart does not belong to this user")
    cart.is_selected = True
    result = await session.execute(
        select(CartItem).where(CartItem.cart_id == cart.id, CartItem.item_id == item_id)
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.quantity += quantity
    else:
        session.add(CartItem(cart_id=cart.id, item_id=item_id, quantity=quantity))
    await session.flush()


async def set_cart_item_quantity(
    session: AsyncSession, user_id: int, item_id: int, quantity: int, cart_id: int | None = None
) -> None:
    """Set exact quantity for `item_id` in user's cart (or remove it when `quantity <= 0`)."""
    if cart_id is None:
        cart = await get_or_create_cart(session, user_id)
    else:
        cart = await session.get(Cart, cart_id)
        if cart is None or cart.user_id != user_id:
            raise ValueError("Cart does not belong to this user")
    result = await session.execute(
        select(CartItem).where(CartItem.cart_id == cart.id, CartItem.item_id == item_id)
    )
    existing = result.scalar_one_or_none()
    if quantity <= 0:
        if existing is not None:
            await session.delete(existing)
    else:
        cart.is_selected = True
        if existing is not None:
            existing.quantity = quantity
        else:
            session.add(CartItem(cart_id=cart.id, item_id=item_id, quantity=quantity))
    await session.flush()


async def remove_from_cart(session: AsyncSession, cart_id: int, item_id: int) -> None:
    await session.execute(
        delete(CartItem).where(CartItem.cart_id == cart_id, CartItem.item_id == item_id)
    )
    await session.flush()


async def clear_cart(session: AsyncSession, cart_id: int) -> None:
    await session.execute(delete(CartItem).where(CartItem.cart_id == cart_id))
    await session.flush()


async def clear_user_carts(session: AsyncSession, user_id: int) -> None:
    carts = await get_carts(session, user_id)
    if carts:
        cart_ids = [c.id for c in carts]
        await session.execute(delete(CartItem).where(CartItem.cart_id.in_(cart_ids)))
        for c in carts:
            c.is_selected = False
        await session.flush()


async def get_cart(session: AsyncSession, cart_id: int) -> list[tuple[CartItem, MenuItem]]:
    result = await session.execute(
        select(CartItem, MenuItem).join(MenuItem, MenuItem.id == CartItem.item_id).where(CartItem.cart_id == cart_id)
    )
    return [(c, m) for c, m in result.all()]


async def get_user_cart_totals(session: AsyncSession, user_id: int) -> tuple[int, Decimal, Decimal]:
    """Return `(total_items, items_subtotal, total_packaging_fee)` across selected (or active) carts."""
    carts = await get_carts(session, user_id)
    if not carts:
        return 0, Decimal("0"), Decimal("0")
    selected = [c for c in carts if c.is_selected]
    target_carts = selected if selected else carts
    total_qty = 0
    total_sub = Decimal("0")
    total_pack = Decimal("0")
    for c in target_carts:
        lines = await get_cart(session, c.id)
        total_qty += sum(ci.quantity for ci, _ in lines)
        total_sub += cart_subtotal(lines)
        pack_fee, _ = calculate_packaging_fee(lines)
        total_pack += pack_fee
    return total_qty, total_sub, total_pack


async def get_user_cart_summary(session: AsyncSession, user_id: int) -> tuple[int, Decimal]:
    """Return `(total_items, subtotal)` across the user's selected (or active) carts."""
    total_qty, total_sub, _ = await get_user_cart_totals(session, user_id)
    return total_qty, total_sub


async def get_selected_cart_ids(session: AsyncSession, user_id: int) -> list[int]:
    result = await session.execute(
        select(Cart.id).where(Cart.user_id == user_id, Cart.is_selected.is_(True)).order_by(Cart.id)
    )
    return list(result.scalars())


def cart_subtotal(lines: list[tuple[CartItem, MenuItem]]) -> Decimal:
    return sum((m.price * c.quantity for c, m in lines), Decimal("0"))


async def create_orders_from_carts(session: AsyncSession, user: User, cart_ids: list[int]) -> PaymentBatch | None:
    cart_lines = [(cart_id, await get_cart(session, cart_id)) for cart_id in cart_ids]
    cart_lines = [(cart_id, lines) for cart_id, lines in cart_lines if lines]
    if not cart_lines:
        return None
    _check_stock(cart_lines)
    orders_list: list[Order] = []
    subtotal = Decimal("0")
    total_pack = Decimal("0")
    for _, lines in cart_lines:
        order_sub = cart_subtotal(lines)
        order_pack, _ = calculate_packaging_fee(lines)
        subtotal += order_sub
        total_pack += order_pack
        orders_list.append(
            Order(
                user_id=user.id,
                subtotal=order_sub,
                delivery_fee=Decimal("0"),
                packaging_fee=order_pack,
                total_amount=order_sub + order_pack,
                status=OrderStatus.PENDING,
                items=[
                    OrderItem(
                        item_id=m.id,
                        item_name=m.name,
                        unit_price=m.price,
                        quantity=c.quantity,
                        subtotal=m.price * c.quantity,
                    )
                    for c, m in lines
                ],
            )
        )
    batch = PaymentBatch(
        user_id=user.id,
        delivery_fee=Decimal("0"),
        packaging_fee=total_pack,
        total_amount=subtotal + total_pack,
        status=OrderStatus.PENDING,
    )
    batch.orders = orders_list
    session.add(batch)
    await session.flush()
    batch.fulfillment = Fulfillment(
        fulfillment_type="delivery",
        stage=FulfillmentStage.COLLECTING_DETAILS,
        checkout_step="choice",
        timer_started_at=datetime.now(timezone.utc),
    )
    await session.execute(delete(CartItem).where(CartItem.cart_id.in_([cart_id for cart_id, _ in cart_lines])))
    await session.flush()
    return batch


async def restore_cart_from_batch(session: AsyncSession, user: User, batch: PaymentBatch) -> Cart:
    """Restore items from an unconfirmed checkout batch back into the user's active cart."""
    cart = await get_or_create_cart(session, user.id)
    await clear_cart(session, cart.id)
    for order in batch.orders:
        for oi in order.items:
            await add_to_cart(session, user.id, oi.item_id, quantity=oi.quantity, cart_id=cart.id)
    cart.is_selected = True
    await session.flush()
    return cart


async def get_last_order(session: AsyncSession, user_id: int) -> Order | None:
    """Fetch the customer's most recent order (preferring PAID, otherwise most recent order with items)."""
    paid_result = await session.execute(
        _order_query()
        .where(Order.user_id == user_id, Order.status == OrderStatus.PAID)
        .order_by(Order.id.desc())
        .limit(1)
    )
    paid = paid_result.scalar_one_or_none()
    if paid is not None and paid.items:
        return paid
    any_result = await session.execute(
        _order_query()
        .where(Order.user_id == user_id)
        .order_by(Order.id.desc())
        .limit(1)
    )
    order = any_result.scalar_one_or_none()
    return order if (order is not None and order.items) else None


def _check_stock(cart_lines: list[tuple[int, list[tuple[CartItem, MenuItem]]]]) -> None:
    """Re-check availability at checkout: items may have sold out since they were added to a cart."""
    wanted: dict[int, tuple[MenuItem, int]] = {}
    for _, lines in cart_lines:
        for cart_item, item in lines:
            _, qty = wanted.get(item.id, (item, 0))
            wanted[item.id] = (item, qty + cart_item.quantity)
    problems = []
    for item, qty in wanted.values():
        limit = item.sellable_units
        if not item.is_available or limit == 0:
            problems.append(f"{item.name} is no longer available")
        elif limit is not None and qty > limit:
            problems.append(f"only {limit} {item.name} left (you have {qty})")
    if problems:
        raise CheckoutError("; ".join(problems))


def _order_query():
    return (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.item), selectinload(Order.user))
        .execution_options(populate_existing=True)
    )


def _batch_query():
    return select(PaymentBatch).options(
        selectinload(PaymentBatch.orders).selectinload(Order.items).selectinload(OrderItem.item),
        selectinload(PaymentBatch.orders).selectinload(Order.user),
        selectinload(PaymentBatch.fulfillment).selectinload(Fulfillment.rider),
    )


async def get_batch(session: AsyncSession, batch_id: int) -> PaymentBatch | None:
    result = await session.execute(_batch_query().where(PaymentBatch.id == batch_id))
    return result.scalar_one_or_none()


async def get_order(session: AsyncSession, order_id: int) -> Order | None:
    result = await session.execute(_order_query().where(Order.id == order_id))
    return result.scalar_one_or_none()


async def get_latest_pending_order(session: AsyncSession, user_id: int) -> Order | None:
    result = await session.execute(
        _order_query()
        .where(Order.user_id == user_id, Order.status == OrderStatus.PENDING)
        .order_by(Order.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def format_order_summary(order: Order) -> str:
    """Plain-text summary used for the admin alert and the kitchen ticket. Needs items loaded."""
    lines = [f"Order #{order.id}", f"Customer: +{order.user.phone_number}", ""]
    lines += [f"{oi.quantity} x {oi.item_name}" for oi in order.items]
    lines += ["", f"Items: {naira(order.subtotal)}"]
    if getattr(order, "packaging_fee", Decimal("0")) > 0:
        lines.append(f"Packaging: {naira(order.packaging_fee)}")
    lines += [f"Delivery: {naira(order.delivery_fee)}", f"Total: {naira(order.total_amount)}"]
    return "\n".join(lines)


def format_batch_summary(batch: PaymentBatch) -> str:
    lines = [f"Payment batch #{batch.id}", f"Customer: +{batch.orders[0].user.phone_number}", ""]
    for order in batch.orders:
        lines.append(f"Order #{order.id}")
        lines.extend(f"{oi.quantity} x {oi.item_name}" for oi in order.items)
    lines.append("")
    if getattr(batch, "packaging_fee", Decimal("0")) > 0:
        lines.append(f"Packaging: {naira(batch.packaging_fee)}")
    lines += [f"Delivery: {naira(batch.delivery_fee)}", f"Total: {naira(batch.total_amount)}"]
    return "\n".join(lines)
