from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session_factory
from app.messages import M8C_QUOTE, M19_NOT_RECEIVED, M20_FAILED
from app.models import (
    CartItem,
    DeliveryArea,
    Fulfillment,
    FulfillmentStage,
    MenuItem,
    OrderStatus,
    PaymentBatch,
    Rider,
    SessionState,
)
from app.services import menu_service, order_service, state_machine, whatsapp
from app.utils import naira, normalize_phone

async def require_staff(request: Request) -> None:
    if not request.session.get("staff_authenticated"):
        raise HTTPException(status_code=401, detail="Staff authentication required")


router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_staff)])
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


class DispatchInput(BaseModel):
    rider_id: int = Field(gt=0)


class FailureInput(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


@router.get("/{page}", include_in_schema=False)
async def dashboard(page: str) -> FileResponse:
    if page not in {"dashboard", "manage-menu", "orders", "riders", "delivery-areas"}:
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(HTML_PATH)


@router.get("/api/staff/menu")
async def list_menu() -> list[dict]:
    async with async_session_factory() as session:
        result = await session.execute(select(MenuItem).order_by(MenuItem.category, MenuItem.name))
        return [_menu_item(item) for item in result.scalars()]


@router.post("/api/staff/menu")
async def create_menu_item(payload: MenuItemInput) -> dict:
    async with async_session_factory() as session:
        item = MenuItem(**payload.model_dump())
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return _menu_item(item)


@router.patch("/api/staff/menu/{item_id}")
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


@router.delete("/api/staff/menu/{item_id}")
async def delete_menu_item(item_id: int) -> dict:
    async with async_session_factory() as session:
        item = await session.get(MenuItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Menu item not found")
        # Clear any lingering cart references so in-progress carts don't point at a deleted item.
        await session.execute(delete(CartItem).where(CartItem.item_id == item_id))
        await session.delete(item)
        await session.commit()
        return {"status": "deleted", "id": item_id}


@router.get("/api/staff/orders")
async def list_orders() -> list[dict]:
    async with async_session_factory() as session:
        batches = []
        result = await session.execute(select(PaymentBatch.id).order_by(PaymentBatch.id.desc()))
        for batch_id in result.scalars():
            batch = await order_service.get_batch(session, batch_id)
            if batch:
                batches.append(_batch(batch))
        return batches


@router.get("/api/staff/orders/{batch_id}/receipt")
async def get_receipt(batch_id: int) -> Response:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Payment batch not found")
        if not batch.receipt_media_id:
            raise HTTPException(status_code=404, detail="No receipt has been uploaded for this order")
        media_id = batch.receipt_media_id
    if settings.dry_run:
        raise HTTPException(
            status_code=409,
            detail="Receipt previews aren't available in dry-run mode (no real image was uploaded).",
        )
    content, mime_type = await whatsapp.download_media(media_id)
    if not content:
        raise HTTPException(status_code=502, detail="Could not fetch the receipt image from WhatsApp.")
    return Response(content=content, media_type=mime_type)


@router.delete("/api/staff/orders/{batch_id}")
async def delete_order(batch_id: int) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Payment batch not found")
        # Cascades to the batch's orders, order items, and fulfillment record.
        await session.delete(batch)
        await session.commit()
        return {"status": "deleted", "id": batch_id}


@router.post("/api/staff/orders/{batch_id}/approve")
async def approve_order(batch_id: int) -> dict:
    return await _set_batch_status(batch_id, OrderStatus.PAID)


@router.post("/api/staff/orders/{batch_id}/reject")
async def reject_order(batch_id: int) -> dict:
    return await _set_batch_status(batch_id, OrderStatus.CANCELLED)


class QuoteInput(BaseModel):
    fee: Decimal = Field(ge=0)


@router.post("/api/staff/orders/{batch_id}/quote")
async def quote_order(batch_id: int, payload: QuoteInput) -> dict:
    """Staff submits (or edits) a delivery fee quote for a custom ("Other") area, (re)notifying the customer."""
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Payment batch not found")
        fulfillment = batch.fulfillment
        if fulfillment is None or fulfillment.stage not in {FulfillmentStage.AWAITING_FEE_QUOTE, FulfillmentStage.AWAITING_FEE_ACCEPT}:
            raise HTTPException(status_code=409, detail="Order is not awaiting a fee quote")
        if not batch.orders:
            raise HTTPException(status_code=404, detail="Payment batch has no orders")
        subtotal = sum((order.subtotal for order in batch.orders), Decimal("0"))
        batch.delivery_fee = payload.fee
        batch.total_amount = subtotal + payload.fee
        fulfillment.stage = FulfillmentStage.AWAITING_FEE_ACCEPT
        user = batch.orders[0].user
        if user:
            user.current_state = SessionState.AWAITING_FEE_ACCEPT
            await whatsapp.send_buttons(user.phone_number, M8C_QUOTE.format(
                area=fulfillment.area_name, fee=naira(payload.fee), total=naira(batch.total_amount)
            ), [("quote:accept", "Proceed to pay"), ("quote:cancel", "Cancel order")])
        await session.commit()
        return _batch(await order_service.get_batch(session, batch_id))


@router.post("/api/staff/orders/{batch_id}/ready")
async def mark_order_ready(batch_id: int) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Payment batch not found")
        fulfillment = batch.fulfillment
        if fulfillment is None:
            raise HTTPException(status_code=404, detail="Fulfillment record not found")
        if fulfillment.stage != FulfillmentStage.PREPARING:
            raise HTTPException(status_code=409, detail="Order is not in the preparing stage")
        fulfillment.stage = FulfillmentStage.READY
        fulfillment.ready_at = datetime.now(timezone.utc)
        if fulfillment.fulfillment_type == "pickup" and batch.orders:
            user = batch.orders[0].user
            if user:
                await whatsapp.send_text(user.phone_number, f"Good news! Order #{batch.id} is ready for pickup at {settings.outlet_address}.")
        await session.commit()
        return {"status": "ready", "batch_id": batch.id}


@router.post("/api/staff/orders/{batch_id}/dispatch")
async def dispatch_order(batch_id: int, payload: DispatchInput) -> dict:
    async with async_session_factory() as session:
        result = await state_machine.dispatch_fulfillment(session, batch_id, payload.rider_id)
        await session.commit()
        return result


@router.post("/api/staff/orders/{batch_id}/collected")
async def mark_order_collected(batch_id: int) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None or batch.fulfillment is None:
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        fulfillment = batch.fulfillment
        if fulfillment.stage not in {FulfillmentStage.READY, FulfillmentStage.OUT_FOR_DELIVERY}:
            raise HTTPException(status_code=409, detail="Order is not ready to be marked collected")
        fulfillment.stage = FulfillmentStage.COLLECTED
        fulfillment.collected_at = datetime.now(timezone.utc)
        if batch.orders:
            user = batch.orders[0].user
            if user:
                await whatsapp.send_text(user.phone_number, f"Thank you for collecting order #{batch.id}. We appreciate you choosing {settings.brand_name}!")
        await session.commit()
        return {"status": "collected", "batch_id": batch.id}


@router.post("/api/staff/orders/{batch_id}/reassign")
async def reassign_order(batch_id: int, payload: DispatchInput) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None or batch.fulfillment is None:
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        current_rider_id = batch.fulfillment.rider_id
        if current_rider_id is not None and current_rider_id != payload.rider_id:
            rider = await session.get(Rider, current_rider_id)
            if rider:
                await whatsapp.send_text(rider.phone_number, f"Delivery #{batch.id} was reassigned. No action needed from you.")
        result = await state_machine.dispatch_fulfillment(session, batch_id, payload.rider_id, resend=True)
        await session.commit()
        return result


@router.post("/api/staff/orders/{batch_id}/failed")
async def mark_order_failed(batch_id: int, payload: FailureInput | None = None) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None or batch.fulfillment is None:
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        fulfillment = batch.fulfillment
        if fulfillment.stage not in {FulfillmentStage.READY, FulfillmentStage.DISPATCHED, FulfillmentStage.OUT_FOR_DELIVERY}:
            raise HTTPException(status_code=409, detail="Order is not eligible to be marked failed")
        reason = (payload.reason if payload and payload.reason else fulfillment.failure_reasons or "Operational issue").strip()
        fulfillment.stage = FulfillmentStage.FAILED
        fulfillment.failed_at = datetime.now(timezone.utc)
        fulfillment.failure_reasons = reason
        fulfillment.failed_by = "staff"
        batch.status = OrderStatus.CANCELLED
        for order in batch.orders:
            order.status = OrderStatus.CANCELLED
        user = batch.orders[0].user if batch.orders else None
        if user is not None:
            await whatsapp.send_text(
                user.phone_number,
                M20_FAILED.format(
                    batch=batch.id,
                    items="items",
                    reasons=reason,
                    refund_timeframe=settings.refund_timeframe,
                    support_phone=settings.support_phone,
                ),
            )
        await session.commit()
        return {"status": "failed", "batch_id": batch.id, "reason": reason}


@router.post("/api/staff/orders/{batch_id}/not-received")
async def mark_order_not_received(batch_id: int) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None or batch.fulfillment is None:
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        fulfillment = batch.fulfillment
        if fulfillment.stage not in {FulfillmentStage.DELIVERED, FulfillmentStage.FAILED}:
            fulfillment.stage = FulfillmentStage.FAILED
        fulfillment.not_received_at = datetime.now(timezone.utc)
        fulfillment.not_received_acknowledged_at = datetime.now(timezone.utc)
        fulfillment.failed_by = "customer"
        fulfillment.failure_reasons = fulfillment.failure_reasons or "Customer reported order not received"
        user = batch.orders[0].user if batch.orders else None
        if user is not None:
            await whatsapp.send_text(user.phone_number, M19_NOT_RECEIVED)
        await session.commit()
        return {"status": "not_received", "batch_id": batch.id}


@router.get("/api/staff/riders")
async def list_riders() -> list[dict]:
    async with async_session_factory() as session:
        result = await session.execute(select(Rider).where(~Rider.is_archived).order_by(Rider.name))
        return [_rider(rider) for rider in result.scalars()]


@router.get("/api/staff/riders/archived")
async def list_archived_riders() -> list[dict]:
    async with async_session_factory() as session:
        result = await session.execute(select(Rider).where(Rider.is_archived).order_by(Rider.name))
        return [_rider(rider) for rider in result.scalars()]


@router.post("/api/staff/riders")
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


@router.patch("/api/staff/riders/{rider_id}")
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


@router.post("/api/staff/riders/{rider_id}/archive")
async def archive_rider(rider_id: int) -> dict:
    async with async_session_factory() as session:
        rider = await session.get(Rider, rider_id)
        if rider is None:
            raise HTTPException(status_code=404, detail="Rider not found")
        rider.is_archived = True
        rider.is_active = False
        await session.commit()
        return {"status": "archived", "id": rider.id}


@router.post("/api/staff/riders/{rider_id}/unarchive")
async def unarchive_rider(rider_id: int) -> dict:
    async with async_session_factory() as session:
        rider = await session.get(Rider, rider_id)
        if rider is None:
            raise HTTPException(status_code=404, detail="Rider not found")
        rider.is_archived = False
        await session.commit()
        return {"status": "unarchived", "id": rider.id}


@router.delete("/api/staff/riders/{rider_id}")
async def delete_rider(rider_id: int) -> dict:
    async with async_session_factory() as session:
        rider = await session.get(Rider, rider_id)
        if rider is None:
            raise HTTPException(status_code=404, detail="Rider not found")
        # Detach the rider from any fulfillments referencing them before deleting.
        await session.execute(
            update(Fulfillment).where(Fulfillment.rider_id == rider_id).values(rider_id=None)
        )
        await session.delete(rider)
        await session.commit()
        return {"status": "deleted", "id": rider_id}


@router.get("/api/staff/delivery-areas")
async def list_delivery_areas() -> list[dict]:
    async with async_session_factory() as session:
        result = await session.execute(select(DeliveryArea).order_by(DeliveryArea.sort_order, DeliveryArea.name))
        return [_area(area) for area in result.scalars()]


@router.post("/api/staff/delivery-areas")
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


@router.patch("/api/staff/delivery-areas/{area_id}")
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
        if batch.fulfillment is not None:
            if status == OrderStatus.PAID:
                batch.fulfillment.stage = FulfillmentStage.PREPARING
                batch.fulfillment.approved_at = datetime.now(timezone.utc)
            else:
                batch.fulfillment.stage = FulfillmentStage.CANCELLED
                batch.fulfillment.cancelled_at = datetime.now(timezone.utc)
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
    fulfillment = batch.fulfillment
    return {
        "id": batch.id,
        "status": batch.status.value,
        "total_amount": str(batch.total_amount),
        "delivery_fee": str(batch.delivery_fee),
        "receipt_media_id": batch.receipt_media_id,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "customer": batch.orders[0].user.name or batch.orders[0].user.phone_number,
        "phone_number": batch.orders[0].user.phone_number,
        "fulfillment": {
            "type": fulfillment.fulfillment_type if fulfillment else None,
            "stage": fulfillment.stage.value if fulfillment else None,
            "rider_id": fulfillment.rider_id if fulfillment else None,
            "rider_name": fulfillment.rider.name if fulfillment and fulfillment.rider else None,
            "area_name": fulfillment.area_name if fulfillment else None,
            "street": fulfillment.street if fulfillment else None,
            "landmark": fulfillment.landmark if fulfillment else None,
            "phone2": fulfillment.phone2 if fulfillment else None,
            "notes": fulfillment.notes if fulfillment else None,
        } if fulfillment else None,
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
