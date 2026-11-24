from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MenuItem, Order
from app.utils import naira

# Items moved from 'Food' to 'Protein 1' (Beef, Fish, Plantain & Extras) even on already-seeded DBs
EXTRAS_MOVED_FROM_FOOD: set[str] = {"Plantain (1)", "Moimoi"}
STEP1_PRIMARY_CATEGORIES: list[str] = ["Food", "Snacks", "Drinks"]

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


async def get_all_items(session: AsyncSession) -> list[MenuItem]:
    """Return all menu items (available and sold out) so the typed-order parser can flag sold-out items."""
    result = await session.execute(select(MenuItem).order_by(MenuItem.id))
    return list(result.scalars())


async def get_item(session: AsyncSession, item_id: int) -> MenuItem | None:
    return await session.get(MenuItem, item_id)


async def find_items(session: AsyncSession, name: str) -> list[MenuItem]:
    result = await session.execute(select(MenuItem).where(MenuItem.name.ilike(f"%{name}%")).order_by(MenuItem.name))
    return list(result.scalars())


def default_tap_quantity(item: MenuItem) -> int:
    """Optimistic default quantity inserted on a 1-tap item selection (Plantain defaults to 2)."""
    if item.name == "Plantain (1)":
        limit = item.sellable_units
        return 1 if limit is not None and limit < 2 else 2
    return 1


def _is_sellable(item: MenuItem | None, needed_qty: int = 1) -> bool:
    return bool(
        item is not None
        and item.is_available
        and (item.sellable_units is None or item.sellable_units >= needed_qty)
    )


def _pick_protein_with_fallback(
    catalog: Sequence[MenuItem], preferred_name: str, category_name: str
) -> MenuItem | None:
    """Return `preferred_name` if sellable, otherwise the cheapest available item in `category_name`."""
    by_name = {m.name: m for m in catalog}
    preferred = by_name.get(preferred_name)
    if _is_sellable(preferred, 1):
        return preferred
    candidates = [m for m in catalog if m.category == category_name and _is_sellable(m, 1)]
    if not candidates:
        return None
    candidates.sort(key=lambda m: (m.price, m.name.lower()))
    return candidates[0]


def resolve_hero_options(catalog: Sequence[MenuItem]) -> list[dict]:
    """Build Step 1 hero combos & bestsellers from the live catalog with cheapest-first protein fallback."""
    by_name = {m.name: m for m in catalog}
    heroes: list[dict] = []

    jollof = by_name.get("Jollof rice")
    fried = by_name.get("Fried rice")
    chicken = _pick_protein_with_fallback(catalog, "Chicken (2)", "Protein 2")
    if _is_sellable(jollof, 2) and _is_sellable(fried, 1) and chicken is not None:
        assert jollof is not None and fried is not None
        total = jollof.price * 2 + fried.price + chicken.price
        heroes.append({
            "id": "bundle:jollof_fried_chicken",
            "title": "🔥 Jollof, Fried & Chicken",
            "description": f"{naira(total)} · 2 Jollof, 1 Fried, {friendly_item_name(chicken.name)}",
            "items": [(jollof, 2), (fried, 1), (chicken, 1)],
            "total_price": total,
        })

    spag = by_name.get("Spaghetti")
    turkey = _pick_protein_with_fallback(catalog, "Turkey (2)", "Protein 3")
    if _is_sellable(spag, 2) and turkey is not None:
        assert spag is not None
        total = spag.price * 2 + turkey.price
        heroes.append({
            "id": "bundle:spag_turkey",
            "title": "🔥 Spaghetti & Turkey",
            "description": f"{naira(total)} · 2 Spag, {friendly_item_name(turkey.name)}",
            "items": [(spag, 2), (turkey, 1)],
            "total_price": total,
        })

    shawarma = by_name.get("Shawama (2000)")
    if _is_sellable(shawarma, 1):
        assert shawarma is not None
        heroes.append({
            "id": f"add:{shawarma.id}",
            "title": "🌯 Shawarma (Large)",
            "description": naira(shawarma.price),
            "items": [(shawarma, 1)],
            "total_price": shawarma.price,
        })

    return heroes


_ADDON_BUCKETS: dict[str, list[str]] = {
    "combo": ["Coke", "Can malt", "Plantain (1)", "Big cup", "Fanta", "Bottle water", "Moimoi"],
    "protein": ["Jollof rice", "Fried rice", "Plantain (1)", "Coke", "Spaghetti", "Beef", "Fanta", "Bottle water"],
    "food": ["Chicken (1)", "Beef", "Plantain (1)", "Coke", "Turkey (1)", "Moimoi", "Fanta", "Bottle water"],
    "snacks": ["Coke", "Can malt", "Popcorn (200)", "Big cup", "Fanta", "Bottle water", "Small cup", "Meatpie"],
    "drinks": ["Shawama (1200)", "Meatpie", "Jollof rice", "Popcorn (200)", "Eggroll", "Fried rice", "Shawama (2000)", "Chicken (1)"],
}


def get_contextual_addons(
    catalog: Sequence[MenuItem],
    last_item: MenuItem | None,
    cart_item_ids: set[int],
    limit: int = 4,
) -> list[tuple[MenuItem, int]]:
    """Return up to `limit` direct 1-tap `(item, default_qty)` cross-sells tailored to `last_item`."""
    if last_item is None:
        bucket = "combo"
    elif last_item.name in EXTRAS_MOVED_FROM_FOOD or last_item.category in {"Protein 1", "Protein 2", "Protein 3"}:
        bucket = "protein"
    elif last_item.category == "Food":
        bucket = "food"
    elif last_item.category == "Snacks":
        bucket = "snacks"
    else:
        bucket = "drinks"

    by_name = {m.name: m for m in catalog}
    picked: list[tuple[MenuItem, int]] = []
    for name in _ADDON_BUCKETS[bucket]:
        item = by_name.get(name)
        if item is None or item.id in cart_item_ids:
            continue
        qty = default_tap_quantity(item)
        if not _is_sellable(item, qty):
            continue
        picked.append((item, qty))
        if len(picked) >= limit:
            break
    return picked


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
