from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import seed
from app.api import webhook_whatsapp
from app.config import settings
from app.models import MenuItem
from app.services import menu_service


@pytest.mark.asyncio
async def test_misc_edit_prices_and_stock(db, menu_item):
    menu_service.set_price(menu_item, Decimal("1500.00"))
    menu_service.set_stock(menu_item, 10)
    await db.commit()

    refreshed = await menu_service.get_item(db, menu_item.id)
    assert refreshed is not None
    assert refreshed.price == Decimal("1500.00")
    assert refreshed.stock_quantity == 10
    assert refreshed.is_available is True

    # Dropping stock to or below threshold automatically hides the item
    menu_service.set_stock(refreshed, 1)
    await db.commit()
    assert refreshed.is_available is False


@pytest.mark.asyncio
async def test_misc_webhook_signature(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "whatsapp_app_secret", "top-secret-key")
    assert webhook_whatsapp._signature_ok(b'{"entry":[]}', "sha256=invalid") is False
    assert webhook_whatsapp._signature_ok(b'{"entry":[]}', None) is False


@pytest.mark.asyncio
async def test_misc_seed_twice(db):
    # First seed populates the empty menu table
    await seed.main()
    count_first = await db.scalar(select(func.count()).select_from(MenuItem))
    assert count_first == len(seed.MENU)

    # Modify an item and verify running seed.main() a second time does not overwrite it
    first_item = (await db.execute(select(MenuItem).order_by(MenuItem.id).limit(1))).scalar_one()
    first_item_id = first_item.id
    first_item.price = Decimal("9999.00")
    await db.commit()

    await seed.main()
    count_second = await db.scalar(select(func.count()).select_from(MenuItem))
    assert count_second == count_first

    db.expunge_all()
    reloaded = await db.get(MenuItem, first_item_id)
    assert reloaded is not None
    assert reloaded.price == Decimal("9999.00")


@pytest.mark.asyncio
async def test_misc_webhook_dry_run(client, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "whatsapp_app_secret", "")
    response = await client.post("/webhook/whatsapp", json={"message": "test"})
    assert response.status_code == 403