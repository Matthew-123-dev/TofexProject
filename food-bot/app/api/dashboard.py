from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import async_session_factory
from app.models import DeliveryArea, MenuItem, OrderStatus, PaymentBatch, Rider, SessionState
from app.services import menu_service, order_service, whatsapp
from app.utils import normalize_phone

router = APIRouter(tags=["dashboard"])
HTML_PATH = Path(__file__).resolve().parents[1] / "dashboard.html"


class MenuItemInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    category: str = Field(min_length=1, max_length=60)
    price: Decimal = Field(ge=0)
    stock_quantity: int | None = Field(default=None, ge=0)
    low_stock_threshold: int = Field(default=0, ge=0)
    is_available: bool = True


class RiderInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone_number: str = Field(min_length=7, max_length=32)
    is_active: bool = False


class DeliveryAreaInput(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    fee: Decimal = Field(ge=0)
    expected_minutes: int = Field(gt=0, le=1440)
    sort_order: int = Field(ge=0, le=10000)
    is_active: bool = True


@router.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(HTML_PATH)


@router.get("/api/menu")
async def list_menu() -> list[dict]:
    async with async_session_factory() as session:
        result = await session.execute(select(MenuItem).order_by(MenuItem.category, MenuItem.name))
        return [_menu_item(item) for item in result.scalars()]


@router.post("/api/menu")
async def create_menu_item(payload: MenuItemInput) -> dict:
    async with async_session_factory() as session:
        item = MenuItem(**payload.model_dump())
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return _menu_item(item)


@router.patch("/api/menu/{item_id}")
async def update_menu_item(item_id: int, payload: MenuItemInput) -> dict:
    async with async_session_factory() as session:
        item = await session.get(MenuItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Menu item not found")
        for key, value in payload.model_dump().items():
            setattr(item, key, value)
        menu_service._touch(item)
        await session.commit()
        await session.refresh(item)
        return _menu_item(item)


@router.get("/api/orders")
async def list_orders() -> list[dict]:
    async with async_session_factory() as session:
        batches = []
        result = await session.execute(select(PaymentBatch.id).order_by(PaymentBatch.id.desc()))
        for batch_id in result.scalars():
            batch = await order_service.get_batch(session, batch_id)
            if batch:
                batches.append(_batch(batch))
        return batches


@router.post("/api/orders/{batch_id}/approve")
async def approve_order(batch_id: int) -> dict:
    return await _set_batch_status(batch_id, OrderStatus.PAID)


@router.post("/api/orders/{batch_id}/reject")
async def reject_order(batch_id: int) -> dict:
    return await _set_batch_status(batch_id, OrderStatus.CANCELLED)


@router.get("/api/riders")
async def list_riders() -> list[dict]:
    async with async_session_factory() as session:
        result = await session.execute(select(Rider).where(~Rider.is_archived).order_by(Rider.name))
        return [_rider(rider) for rider in result.scalars()]


@router.post("/api/riders")
async def create_rider(payload: RiderInput) -> dict:
    try:
        phone = normalize_phone(payload.phone_number)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with async_session_factory() as session:
        rider = Rider(name=payload.name.strip(), phone_number=phone, is_active=payload.is_active)
        session.add(rider)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="A rider with that phone number already exists.") from exc
        await session.refresh(rider)
        return _rider(rider)


@router.patch("/api/riders/{rider_id}")
async def update_rider(rider_id: int, payload: RiderInput) -> dict:
    try:
        phone = normalize_phone(payload.phone_number)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with async_session_factory() as session:
        rider = await session.get(Rider, rider_id)
        if rider is None or rider.is_archived:
            raise HTTPException(status_code=404, detail="Rider not found")
        rider.name = payload.name.strip()
        rider.phone_number = phone
        rider.is_active = payload.is_active
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="A rider with that phone number already exists.") from exc
        await session.refresh(rider)
        return _rider(rider)


@router.post("/api/riders/{rider_id}/archive")
async def archive_rider(rider_id: int) -> dict:
    async with async_session_factory() as session:
        rider = await session.get(Rider, rider_id)
        if rider is None:
            raise HTTPException(status_code=404, detail="Rider not found")
        rider.is_archived = True
        rider.is_active = False
        await session.commit()
        return {"status": "archived", "id": rider.id}


@router.get("/api/delivery-areas")
async def list_delivery_areas() -> list[dict]:
    async with async_session_factory() as session:
        result = await session.execute(select(DeliveryArea).order_by(DeliveryArea.sort_order, DeliveryArea.name))
        return [_area(area) for area in result.scalars()]


@router.post("/api/delivery-areas")
async def create_delivery_area(payload: DeliveryAreaInput) -> dict:
    async with async_session_factory() as session:
        values = payload.model_dump()
        values["name"] = payload.name.strip()
        area = DeliveryArea(**values)
        session.add(area)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="A delivery area with that name already exists.") from exc
        await session.refresh(area)
        return _area(area)


@router.patch("/api/delivery-areas/{area_id}")
async def update_delivery_area(area_id: int, payload: DeliveryAreaInput) -> dict:
    async with async_session_factory() as session:
        area = await session.get(DeliveryArea, area_id)
        if area is None:
            raise HTTPException(status_code=404, detail="Delivery area not found")
        for key, value in payload.model_dump().items():
            setattr(area, key, value)
        area.name = area.name.strip()
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="A delivery area with that name already exists.") from exc
        await session.refresh(area)
        return _area(area)


async def _set_batch_status(batch_id: int, status: OrderStatus) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Payment batch not found")
        if batch.status != OrderStatus.PENDING:
            raise HTTPException(status_code=409, detail="Payment batch has already been handled")
        batch.status = status
        for order in batch.orders:
            order.status = status
            if status == OrderStatus.PAID:
                order.user.current_state = SessionState.COMPLETED
                await menu_service.decrement_stock(session, order)
            else:
                order.user.current_state = SessionState.BROWSING
        phone = batch.orders[0].user.phone_number
        if status == OrderStatus.PAID:
            await whatsapp.send_text(phone, f"Payment confirmed for batch #{batch.id}. Your orders are being prepared. Thank you!")
        else:
            await whatsapp.send_text(phone, f"We couldn't verify payment batch #{batch.id}. Please contact us or send *menu* to try again.")
        await session.commit()
        return {"status": status.value, "batch_id": batch.id}


def _menu_item(item: MenuItem) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "category": item.category,
        "price": str(item.price),
        "stock_quantity": item.stock_quantity,
        "low_stock_threshold": item.low_stock_threshold,
        "is_available": item.is_available,
    }


def _batch(batch) -> dict:
    return {
        "id": batch.id,
        "status": batch.status.value,
        "total_amount": str(batch.total_amount),
        "delivery_fee": str(batch.delivery_fee),
        "receipt_media_id": batch.receipt_media_id,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "customer": batch.orders[0].user.name or batch.orders[0].user.phone_number,
        "phone_number": batch.orders[0].user.phone_number,
        "orders": [
            {
                "id": order.id,
                "subtotal": str(order.subtotal),
                "items": [
                    {"name": item.item_name, "quantity": item.quantity, "subtotal": str(item.subtotal)}
                    for item in order.items
                ],
            }
            for order in batch.orders
        ],
    }


def _rider(rider: Rider) -> dict:
    last_message_at = rider.last_message_at
    if last_message_at and last_message_at.tzinfo is None:
        last_message_at = last_message_at.replace(tzinfo=timezone.utc)
    available = bool(
        rider.is_active
        and last_message_at
        and last_message_at >= datetime.now(timezone.utc) - timedelta(hours=24)
    )
    return {
        "id": rider.id,
        "name": rider.name,
        "phone_number": rider.phone_number,
        "is_active": rider.is_active,
        "is_archived": rider.is_archived,
        "last_message_at": rider.last_message_at.isoformat() if rider.last_message_at else None,
        "available": available,
    }


def _area(area: DeliveryArea) -> dict:
    return {
        "id": area.id,
        "name": area.name,
        "fee": str(area.fee),
        "expected_minutes": area.expected_minutes,
        "is_active": area.is_active,
        "sort_order": area.sort_order,
    }
