from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MenuItem, Order

# Items moved from 'Food' to 'Protein 1' (Beef, Fish, Plantain & Extras) even on already-seeded DBs
EXTRAS_MOVED_FROM_FOOD: set[str] = {"Plantain (1)", "Moimoi"}

# Customer-facing category labels (Principle 3: Recognition over recall)
CATEGORY_DISPLAY_NAMES: dict[str, str] = {
    "Food": "🍛 Rice, Spaghetti & Porridge",
    "Snacks": "🌯 Shawarma & Snacks",
    "Protein 2": "🍗 Grilled Chicken",
    "Protein 3": "🦃 Grilled Turkey",
    "Protein 1": "🥩 Beef, Fish, Plantain & Extras",
    "Drinks": "🥤 Cold Drinks",
    "Icecream": "🍦 Ice Cream",
}

CATEGORY_SORT_ORDER: list[str] = [
    "Food",
    "Snacks",
    "Protein 2",
    "Protein 3",
    "Protein 1",
    "Drinks",
    "Icecream",
]

# Customer-facing item labels for abbreviated database names
ITEM_DISPLAY_NAMES: dict[str, str] = {
    "Plantain (1)": "Plantain (1 portion)",
    "Chicken (1)": "Chicken Small (1)",
    "Chicken (2)": "Chicken Medium (2)",
    "Chicken (3)": "Chicken Large (3)",
    "Chicken (4)": "Chicken XL (4)",
    "Chicken (5)": "Chicken XXL (5)",
    "Turkey (1)": "Turkey Small (1)",
    "Turkey (2)": "Turkey Medium (2)",
    "Turkey (3)": "Turkey Large (3)",
    "Turkey (4)": "Turkey XL (4)",
    "Turkey (5)": "Turkey XXL (5)",
    "Shawama (1200)": "Shawarma (Regular)",
    "Shawama (2000)": "Shawarma (Large)",
    "Popcorn (200)": "Popcorn (Small)",
    "Popcorn (300)": "Popcorn (Large)",
    "Medium": "Medium cup",
    "1/2 liter": "Half-liter tub",
}

# Popularity order per category so the most popular items appear first in each poll
ITEM_POPULARITY_ORDER: dict[str, list[str]] = {
    "Food": ["Jollof rice", "Fried rice", "Spaghetti", "Porridge"],
    "Snacks": ["Shawama (1200)", "Shawama (2000)", "Meatpie", "Eggroll", "Popcorn (200)", "Popcorn (300)"],
    "Protein 2": ["Chicken (1)", "Chicken (2)", "Chicken (3)", "Chicken (4)", "Chicken (5)"],
    "Protein 3": ["Turkey (1)", "Turkey (2)", "Turkey (3)", "Turkey (4)", "Turkey (5)"],
    "Protein 1": ["Beef", "Plantain (1)", "Egg", "Moimoi", "Salad", "Fish (Medium)", "Fish (Big)", "Ponmo"],
    "Drinks": [
        "Coke",
        "Fanta",
        "Sprite",
        "Pepsi",
        "Bottle water",
        "Hollandia",
        "Exotic",
        "Active",
        "Pulpy orange",
        "Berryblast",
        "Can malt",
        "Plastic malt",
        "Freshyo",
        "Vijuchoco",
        "Nutrichoco",
        "Nutrimilk",
        "Fearless",
        "Lucozade",
        "Cola",
    ],
    "Icecream": ["Small cup", "Medium", "Big cup", "1/2 liter"],
}


def friendly_category_name(category: str) -> str:
    """Return a customer-friendly category label with emoji."""
    return CATEGORY_DISPLAY_NAMES.get(category, category)


def friendly_item_name(item_name: str) -> str:
    """Return a customer-friendly item label."""
    return ITEM_DISPLAY_NAMES.get(item_name, item_name)


def sort_categories(categories: list[str]) -> list[str]:
    """Order known categories by meal popularity first, followed by any custom categories alphabetically."""
    order_map = {name: idx for idx, name in enumerate(CATEGORY_SORT_ORDER)}
    return sorted(categories, key=lambda c: (order_map.get(c, 999), c.lower()))


async def get_categories(session: AsyncSession) -> list[str]:
    """Only categories that still have at least one available item."""
    result = await session.execute(
        select(MenuItem.category).where(MenuItem.is_available.is_(True)).distinct().order_by(MenuItem.category)
    )
    raw = list(result.scalars())
    return sort_categories(raw)


async def get_available_items(session: AsyncSession, category: str) -> list[MenuItem]:
    if category == "Food":
        stmt = (
            select(MenuItem)
            .where(
                MenuItem.category == "Food",
                MenuItem.is_available.is_(True),
                MenuItem.name.not_in(EXTRAS_MOVED_FROM_FOOD),
            )
            .order_by(MenuItem.name)
        )
    elif category == "Protein 1":
        stmt = (
            select(MenuItem)
            .where(
                MenuItem.is_available.is_(True),
                or_(
                    MenuItem.category == "Protein 1",
                    MenuItem.name.in_(EXTRAS_MOVED_FROM_FOOD),
                ),
            )
            .order_by(MenuItem.name)
        )
    else:
        stmt = (
            select(MenuItem)
            .where(MenuItem.category == category, MenuItem.is_available.is_(True))
            .order_by(MenuItem.name)
        )
    result = await session.execute(stmt)
    items = list(result.scalars())
    pop_list = ITEM_POPULARITY_ORDER.get(category)
    if not pop_list:
        return items
    rank = {name: idx for idx, name in enumerate(pop_list)}
    return sorted(items, key=lambda item: (rank.get(item.name, 999), item.name.lower()))


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
