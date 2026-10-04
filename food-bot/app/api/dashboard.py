from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.database import async_session_factory
from app.models import MenuItem, OrderStatus, PaymentBatch, SessionState
from app.services import menu_service, order_service, whatsapp

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
