"""Seed the menu. Run after migrations: python -m app.seed"""
import asyncio
from decimal import Decimal

from sqlalchemy import func, select

from app.database import async_session_factory
from app.models import MenuItem

# name, description, price, category, stock (None = binary availability), hide-at threshold
MENU = [
    ("Spaghetti", "Seasoned spaghetti.", "600", "Food", None, 0),
    ("Jollof rice", "Classic Nigerian jollof rice.", "400", "Food", None, 0),
    ("Fried rice", "Savory fried rice.", "400", "Food", None, 0),
    ("Porridge", "Warm, hearty porridge.", "600", "Food", None, 0),
    ("Moimoi", "Steamed bean pudding.", "700", "Food", None, 0),
    ("Plantain (1)", "One serving of plantain.", "200", "Food", None, 0),
    ("Beef", "Tender beef serving.", "500", "Protein 1", None, 0),
    ("Egg", "One boiled egg.", "400", "Protein 1", None, 0),
    ("Ponmo", "Seasoned cow skin.", "500", "Protein 1", None, 0),
    ("Fish (Big)", "Large fish serving.", "2000", "Protein 1", None, 0),
    ("Fish (Medium)", "Medium fish serving.", "1500", "Protein 1", None, 0),
    ("Salad", "Fresh mixed salad.", "800", "Protein 1", None, 0),
    ("Chicken (1)", "Small chicken.", "2500", "Protein 2", None, 0),
    ("Chicken (2)", "Medium chicken.", "3000", "Protein 2", None, 0),
    ("Chicken (3)", "Large chicken.", "3500", "Protein 2", None, 0),
    ("Chicken (4)", "Xtralarge chicken.", "4000", "Protein 2", None, 0),
    ("Chicken (5)", "XXtralarge chicken.", "4500", "Protein 2", None, 0),
    ("Turkey (1)", "Small turkey.", "3000", "Protein 3", None, 0),
    ("Turkey (2)", "Medium turkey.", "3500", "Protein 3", None, 0),
    ("Turkey (3)", "Large turkey.", "4000", "Protein 3", None, 0),
    ("Turkey (4)", "Xtralarge turkey.", "4500", "Protein 3", None, 0),
    ("Turkey (5)", "XXtralarge turkey.", "5000", "Protein 3", None, 0),
    ("Small cup", "Small cup of ice cream.", "400", "Icecream", None, 0),
    ("Medium", "Medium cup of ice cream.", "600", "Icecream", None, 0),
    ("Big cup", "Large cup of ice cream.", "800", "Icecream", None, 0),
    ("1/2 liter", "Half-litre ice cream.", "2000", "Icecream", None, 0),
    ("Meatpie", "Savory meat pie.", "1000", "Snacks", None, 0),
    ("Eggroll", "Fried egg roll.", "600", "Snacks", None, 0),
    ("Shawama (1200)", "Regular shawarma.", "1200", "Snacks", None, 0),
    ("Shawama (2000)", "Large shawarma.", "2000", "Snacks", None, 0),
    ("Popcorn (200)", "Small popcorn serving.", "200", "Snacks", None, 0),
    ("Popcorn (300)", "Large popcorn serving.", "300", "Snacks", None, 0),
    ("Berryblast", "Berry-flavored drink.", "1500", "Drinks", None, 0),
    ("Pulpy orange", "Orange-flavored drink.", "1700", "Drinks", None, 0),
    ("Exotic", "Exotic fruit drink.", "2000", "Drinks", None, 0),
    ("Hollandia", "Hollandia dairy drink.", "2000", "Drinks", None, 0),
    ("Active", "Chivita Active fruit drink.", "2000", "Drinks", None, 0),
    ("Lucozade", "Lucozade energy drink.", "1000", "Drinks", None, 0),
    ("Fearless", "Fearless energy drink.", "1000", "Drinks", None, 0),
    ("Fanta", "Orange soft drink.", "500", "Drinks", None, 0),
    ("Coke", "Coca-Cola soft drink.", "500", "Drinks", None, 0),
    ("Cola", "American Cola soft drink.", "500", "Drinks", None, 0),
    ("Pepsi", "Pepsi soft drink.", "500", "Drinks", None, 0),
    ("Sprite", "Lemon-lime soft drink.", "500", "Drinks", None, 0),
    ("Nutrichoco", "Chocolate dairy drink.", "1000", "Drinks", None, 0),
    ("Nutrimilk", "Milk drink.", "700", "Drinks", None, 0),
    ("Can malt", "Malt drink in a can.", "800", "Drinks", None, 0),
    ("Plastic malt", "Malt drink in a bottle.", "600", "Drinks", None, 0),
    ("Freshyo", "Fresh yoghurt drink.", "700", "Drinks", None, 0),
    ("Vijuchoco", "Chocolate yoghurt drink.", "1200", "Drinks", None, 0),
    ("Bottle water", "Bottled drinking water.", "200", "Drinks", None, 0),
]


async def main() -> None:
    """Load the starter menu ONLY into an empty menu table.

    It never updates prices/stock or deletes items, so running it again can never wipe
    staff changes made from the dashboard.
    """
    async with async_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(MenuItem))
        if count:
            print(f"Menu already has {count} items; nothing was changed.")
            return
        for name, description, price, category, stock, threshold in MENU:
            session.add(MenuItem(
                name=name, description=description or None, price=Decimal(price), category=category,
                is_available=True, stock_quantity=stock, low_stock_threshold=threshold,
            ))
        await session.commit()
        print(f"Seeded {len(MENU)} menu items.")


if __name__ == "__main__":
    asyncio.run(main())
