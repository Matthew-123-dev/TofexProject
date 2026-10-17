import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Ensure the project root (`food-bot/`) is on sys.path before importing `app`
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.api.dashboard as dashboard_api
import app.api.webhook_whatsapp as webhook_api
import app.database as app_database
import app.main as app_main
import app.seed as app_seed
import app.services.scheduler as scheduler_module
from app.config import settings
from app.database import Base, get_session
from app.main import app, get_db
from app.models import (
    DeliveryArea,
    Fulfillment,
    FulfillmentStage,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
    PaymentBatch,
    Rider,
    SessionState,
    User,
)
from app.services import order_service, whatsapp

DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionFactory = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def fake_whatsapp(monkeypatch):
    """Mock outgoing WhatsApp API calls and set test staff credentials safely in memory."""
    monkeypatch.setattr(whatsapp, "send_text", AsyncMock(return_value=True))
    monkeypatch.setattr(whatsapp, "send_buttons", AsyncMock(return_value=True))
    monkeypatch.setattr(whatsapp, "send_list", AsyncMock(return_value=True))
    monkeypatch.setattr(whatsapp, "send_location_request", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "staff_username", "admin")
    monkeypatch.setattr(settings, "staff_password", "test-staff-password-123")
    yield


@pytest_asyncio.fixture
async def db(monkeypatch):
    """Create an isolated in-memory SQLite schema for each test and redirect all session factories to it."""
    monkeypatch.setattr(app_database, "engine", test_engine)
    monkeypatch.setattr(app_database, "async_session_factory", TestingSessionFactory)
    monkeypatch.setattr(app_main, "async_session_factory", TestingSessionFactory)
    monkeypatch.setattr(dashboard_api, "async_session_factory", TestingSessionFactory)
    monkeypatch.setattr(webhook_api, "async_session_factory", TestingSessionFactory)
    monkeypatch.setattr(scheduler_module, "async_session_factory", TestingSessionFactory)
    monkeypatch.setattr(app_seed, "async_session_factory", TestingSessionFactory)

    async def override_get_db():
        async with TestingSessionFactory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session] = override_get_db

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with TestingSessionFactory() as session:
        yield session

    app.dependency_overrides.clear()
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client(db):
    """Async HTTP client bound to the FastAPI ASGI app."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def user(db: AsyncSession) -> User:
    u = User(
        phone_number="2348012345678",
        name="Test User",
        current_state=SessionState.BROWSING,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@pytest_asyncio.fixture
async def menu_item(db: AsyncSession) -> MenuItem:
    item = MenuItem(
        name="Test Item",
        description="Tasty test meal",
        price=Decimal("1000.00"),
        category="Food",
        is_available=True,
        stock_quantity=5,
        low_stock_threshold=1,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@pytest_asyncio.fixture
async def delivery_area(db: AsyncSession) -> DeliveryArea:
    area = DeliveryArea(
        name="Test Area",
        fee=Decimal("1500.00"),
        expected_minutes=40,
        is_active=True,
        sort_order=1,
    )
    db.add(area)
    await db.commit()
    await db.refresh(area)
    return area


@pytest_asyncio.fixture
async def rider(db: AsyncSession) -> Rider:
    r = Rider(
        name="Test Rider",
        phone_number="2348098765432",
        is_active=False,
        is_archived=False,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return r


@pytest_asyncio.fixture
async def batch(db: AsyncSession, user: User, menu_item: MenuItem, delivery_area: DeliveryArea) -> PaymentBatch:
    b = PaymentBatch(
        user_id=user.id,
        delivery_fee=delivery_area.fee,
        total_amount=menu_item.price + delivery_area.fee,
        receipt_media_id="receipt-media-1",
        status=OrderStatus.PENDING,
    )
    b.orders = [
        Order(
            user_id=user.id,
            subtotal=menu_item.price,
            delivery_fee=Decimal("0.00"),
            total_amount=menu_item.price,
            status=OrderStatus.PENDING,
            items=[
                OrderItem(
                    item_id=menu_item.id,
                    item_name=menu_item.name,
                    unit_price=menu_item.price,
                    quantity=1,
                    subtotal=menu_item.price,
                )
            ],
        )
    ]
    db.add(b)
    await db.flush()
    b.fulfillment = Fulfillment(
        payment_batch_id=b.id,
        fulfillment_type="delivery",
        stage=FulfillmentStage.AWAITING_APPROVAL,
        street="12 Test Street",
        area_id=delivery_area.id,
        area_name=delivery_area.name,
        landmark="Opposite City Hall",
        expected_minutes=delivery_area.expected_minutes,
    )
    await db.commit()
    loaded = await order_service.get_batch(db, b.id)
    assert loaded is not None
    return loaded


@pytest_asyncio.fixture
async def order(batch: PaymentBatch) -> Order:
    return batch.orders[0]