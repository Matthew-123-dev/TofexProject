from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MenuItem, Order


async def get_categories(session: AsyncSession) -> list[str]:
    """Only categories that still have at least one available item."""
    result = await session.execute(
        select(MenuItem.category).where(MenuItem.is_available.is_(True)).distinct().order_by(MenuItem.category)
    )
    return list(result.scalars())


async def get_available_items(session: AsyncSession, category: str) -> list[MenuItem]:
    result = await session.execute(
        select(MenuItem)
        .where(MenuItem.category == category, MenuItem.is_available.is_(True))
        .order_by(MenuItem.name)
    )
    return list(result.scalars())


async def get_item(session: AsyncSession, item_id: int) -> MenuItem | None:
    return await session.get(MenuItem, item_id)


async def find_items(session: AsyncSession, name: str) -> list[MenuItem]:
    result = await session.execute(select(MenuItem).where(MenuItem.name.ilike(f"%{name}%")).order_by(MenuItem.name))
    return list(result.scalars())


def _touch(item: MenuItem) -> None:
    item.availability_updated_at = datetime.now(timezone.utc)


def _refresh_from_stock(item: MenuItem) -> None:
    if item.stock_quantity is not None:
        item.is_available = item.stock_quantity > item.low_stock_threshold
    _touch(item)


# ---- staff actions used by the admin dashboard ----

def set_stock(item: MenuItem, quantity: int) -> None:
    """Staff set the quantity on hand. Availability is recalculated against the threshold."""
    item.stock_quantity = quantity
    _refresh_from_stock(item)


def set_threshold(item: MenuItem, threshold: int) -> None:
    item.low_stock_threshold = threshold
    _refresh_from_stock(item)


def set_available(item: MenuItem, available: bool) -> None:
    """Set availability for binary items, or manually override tracked stock."""
    item.is_available = available
    _touch(item)


def set_price(item: MenuItem, price: Decimal) -> None:
    if price < 0:
        raise ValueError("Price cannot be negative")
    item.price = price


def set_name(item: MenuItem, name: str) -> None:
    if not name.strip():
        raise ValueError("Name cannot be empty")
    item.name = name.strip()


def set_description(item: MenuItem, description: str) -> None:
    item.description = description.strip() or None


def set_category(item: MenuItem, category: str) -> None:
    if not category.strip():
        raise ValueError("Category cannot be empty")
    item.category = category.strip()


async def stock_report(session: AsyncSession) -> str:
    result = await session.execute(select(MenuItem).order_by(MenuItem.category, MenuItem.name))
    lines = []
    for i in result.scalars():
        icon = "✅" if i.is_available else "⛔"
        stock = "not tracked" if i.stock_quantity is None else f"{i.stock_quantity} in stock, hides at {i.low_stock_threshold}"
        lines.append(f"{icon} {i.name}: {stock}")
    return "\n".join(lines) or "Menu is empty."


# ---- called when an order is paid ----

async def decrement_stock(session: AsyncSession, order: Order) -> list[str]:
    """Deduct a paid order from tracked items. Returns alert texts for items that hit their threshold.

    Only ever hides items here; re-enabling is a deliberate staff action.
    """
    alerts: list[str] = []
    for oi in order.items:
        item = oi.item
        if item is None or item.stock_quantity is None:
            continue
        item.stock_quantity = max(0, item.stock_quantity - oi.quantity)
        if item.stock_quantity <= item.low_stock_threshold and item.is_available:
            item.is_available = False
            _touch(item)
            alerts.append(
                f"⚠️ {item.name} is down to {item.stock_quantity} (threshold {item.low_stock_threshold}) "
                f"and is now hidden from the menu. Update its stock in Manage Menu to show it again."
            )
    return alerts
