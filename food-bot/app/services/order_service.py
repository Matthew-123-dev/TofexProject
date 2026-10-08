from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import (
    Cart, CartItem, Fulfillment, FulfillmentStage, MenuItem, Order, OrderItem, OrderStatus, PaymentBatch, User,
)
from app.utils import naira


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


async def get_cart(session: AsyncSession, cart_id: int) -> list[tuple[CartItem, MenuItem]]:
    result = await session.execute(
        select(CartItem, MenuItem).join(MenuItem, MenuItem.id == CartItem.item_id).where(CartItem.cart_id == cart_id)
    )
    return [(c, m) for c, m in result.all()]


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
        for cart_id, _ in cart_lines:
            if await session.get(Cart, cart_id) is None:
                raise ValueError("One or more carts could not be found")
    subtotal = sum((cart_subtotal(lines) for _, lines in cart_lines), Decimal("0"))
    batch = PaymentBatch(
        user_id=user.id,
        delivery_fee=Decimal("0"),
        total_amount=subtotal,
        status=OrderStatus.PENDING,
    )
    batch.orders = [
        Order(
            user_id=user.id,
            subtotal=cart_subtotal(lines),
            delivery_fee=Decimal("0"),
            total_amount=cart_subtotal(lines),
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
        for _, lines in cart_lines
    ]
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
    lines += ["", f"Items: {naira(order.subtotal)}", f"Delivery: {naira(order.delivery_fee)}",
              f"Total: {naira(order.total_amount)}"]
    return "\n".join(lines)


def format_batch_summary(batch: PaymentBatch) -> str:
    lines = [f"Payment batch #{batch.id}", f"Customer: +{batch.orders[0].user.phone_number}", ""]
    for order in batch.orders:
        lines.append(f"Order #{order.id}")
        lines.extend(f"{oi.quantity} x {oi.item_name}" for oi in order.items)
    lines += ["", f"Delivery: {naira(batch.delivery_fee)}", f"Total: {naira(batch.total_amount)}"]
    return "\n".join(lines)
