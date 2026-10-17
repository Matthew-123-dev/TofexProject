from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session_factory
from app.messages import (
    M8C_QUOTE, M11_APPROVED, M12_REJECTED, M16_PICKUP_READY, M18B_COLLECTED, R6_REASSIGNED,
)
from app.models import (
    CartItem,
    DeliveryArea,
    Fulfillment,
    FulfillmentStage,
    MenuItem,
    OrderItem,
    OrderStatus,
    PaymentBatch,
    ReceiptImage,
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
RECEIPT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, max-age=3600",
}


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
        stock_before = (item.stock_quantity, item.low_stock_threshold)
        for key, value in payload.model_dump().items():
            setattr(item, key, value)
        if item.stock_quantity is not None and (item.stock_quantity, item.low_stock_threshold) != stock_before:
            # Changing the stock count or threshold re-checks availability (so restocking brings an item back).
            item.is_available = item.stock_quantity > item.low_stock_threshold
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
        used = await session.scalar(select(func.count()).select_from(OrderItem).where(OrderItem.item_id == item_id))
        if used:
            raise HTTPException(
                status_code=409,
                detail="This item appears in past orders, so it can't be deleted. Turn its availability off instead.",
            )
        # Clear any lingering cart references so in-progress carts don't point at a deleted item.
        await session.execute(delete(CartItem).where(CartItem.item_id == item_id))
        await session.delete(item)
        await session.commit()
        return {"status": "deleted", "id": item_id}


@router.get("/api/staff/orders")
async def list_orders(limit: int = 200) -> list[dict]:
    """Newest payment batches first. One query with eager loading (no per-order queries)."""
    limit = max(1, min(limit, 500))
    async with async_session_factory() as session:
        result = await session.execute(order_service._batch_query().order_by(PaymentBatch.id.desc()).limit(limit))
        return [_batch(batch) for batch in result.scalars().all() if batch.orders]


@router.get("/api/staff/orders/{batch_id}/receipt")
async def get_receipt(batch_id: int) -> Response:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Payment batch not found")
        stored = (
            await session.execute(
                select(ReceiptImage)
                .where(ReceiptImage.payment_batch_id == batch_id)
                .order_by(ReceiptImage.created_at.desc(), ReceiptImage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if stored is not None:
            mime_clean = (stored.mime_type or "").strip().lower()
            if not mime_clean.startswith("image/"):
                raise HTTPException(status_code=415, detail="Stored receipt is not a valid image.")
            return Response(content=stored.content, media_type=mime_clean, headers=RECEIPT_HEADERS)
        if not batch.receipt_media_id:
            raise HTTPException(status_code=404, detail="No receipt has been uploaded for this order")
        media_id = batch.receipt_media_id
    if settings.dry_run:
        raise HTTPException(
            status_code=409,
            detail="Receipt previews aren't available in dry-run mode (no real image was uploaded).",
        )
    try:
        content, mime_type = await whatsapp.download_media(media_id)
    except Exception as exc:  # expired media id, expired token, network error
        raise HTTPException(
            status_code=502,
            detail="Could not fetch the receipt from WhatsApp (it may have expired). Ask the customer to resend it.",
        ) from exc
    if not content:
        raise HTTPException(status_code=502, detail="Could not fetch the receipt image from WhatsApp.")
    mime_clean = (mime_type or "").strip().lower()
    if not mime_clean.startswith("image/"):
        raise HTTPException(status_code=415, detail="Receipt media is not a valid image.")
    return Response(content=content, media_type=mime_clean, headers=RECEIPT_HEADERS)


@router.delete("/api/staff/orders/{batch_id}")
async def delete_order(batch_id: int) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Payment batch not found")
        if batch.status == OrderStatus.PAID:
            raise HTTPException(
                status_code=409,
                detail="Paid orders are kept for your records and can't be deleted.",
            )
        # Cascades to the batch's orders, order items, and fulfillment record.
        await session.delete(batch)
        await session.commit()
        return {"status": "deleted", "id": batch_id}


@router.post("/api/staff/orders/{batch_id}/approve")
async def approve_order(batch_id: int) -> dict:
    async with async_session_factory() as session:
        batch = await _reviewable_batch(session, batch_id)
        fulfillment = batch.fulfillment
        batch.status = OrderStatus.PAID
        low_stock: list[str] = []
        for order in batch.orders:
            order.status = OrderStatus.PAID
            order.user.current_state = SessionState.COMPLETED
            low_stock += await menu_service.decrement_stock(session, order)
        if fulfillment is not None:
            fulfillment.stage = FulfillmentStage.PREPARING
            fulfillment.approved_at = datetime.now(timezone.utc)
            fulfillment.timer_started_at = None
        phone = batch.orders[0].user.phone_number
        pickup = fulfillment is not None and fulfillment.fulfillment_type == "pickup"
        await session.commit()  # save the decision first, then tell the customer
        text = M11_APPROVED.format(batch=batch.id)
        if pickup:
            text = text.replace("as soon as it's on its way", "when it's ready for pickup")
        await whatsapp.send_text(phone, text)
        return {"status": "paid", "batch_id": batch.id, "low_stock": low_stock}


@router.post("/api/staff/orders/{batch_id}/reject")
async def reject_order(batch_id: int) -> dict:
    """Reject a receipt. The customer can send a new one: the order is NOT cancelled."""
    async with async_session_factory() as session:
        batch = await _reviewable_batch(session, batch_id)
        fulfillment = batch.fulfillment
        user = batch.orders[0].user
        phone = user.phone_number
        if fulfillment is None:  # legacy order from before delivery tracking: reject = cancel
            batch.status = OrderStatus.CANCELLED
            for order in batch.orders:
                order.status = OrderStatus.CANCELLED
            user.current_state = SessionState.BROWSING
            text = f"We couldn't verify payment batch #{batch.id}. Please contact us or send *menu* to try again."
        else:
            batch.receipt_media_id = None
            fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
            fulfillment.timer_started_at = datetime.now(timezone.utc)
            fulfillment.reminders_sent = "[]"
            fulfillment.address_editing = False
            user.current_state = SessionState.AWAITING_PAYMENT
            text = M12_REJECTED.format(
                batch=batch.id, support_phone=settings.support_phone, minutes=settings.checkout_cancel_minutes,
            )
        await session.commit()
        await whatsapp.send_text(phone, text)
        return {"status": "receipt_rejected", "batch_id": batch.id}


async def _reviewable_batch(session, batch_id: int) -> PaymentBatch:
    """Load a batch that is genuinely waiting for approval; otherwise raise a clear HTTP error."""
    batch = await order_service.get_batch(session, batch_id)
    if batch is None or not batch.orders:
        raise HTTPException(status_code=404, detail="Payment batch not found")
    if batch.status != OrderStatus.PENDING:
        raise HTTPException(status_code=409, detail="This order has already been handled")
    fulfillment = batch.fulfillment
    if fulfillment is not None:
        if fulfillment.stage != FulfillmentStage.AWAITING_APPROVAL:
            raise HTTPException(
                status_code=409, detail="This order isn't waiting for approval (no receipt has been sent yet)."
            )
        if fulfillment.address_editing:
            raise HTTPException(
                status_code=409, detail="The customer is changing their delivery address. Try again in a minute."
            )
    elif not batch.receipt_media_id:
        raise HTTPException(status_code=409, detail="No receipt has been sent for this order yet.")
    return batch


class QuoteInput(BaseModel):
    fee: Decimal = Field(ge=0)
    expected_minutes: int = Field(ge=1, le=1440)


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
        fulfillment.expected_minutes = payload.expected_minutes
        fulfillment.stage = FulfillmentStage.AWAITING_FEE_ACCEPT
        fulfillment.timer_started_at = datetime.now(timezone.utc)  # the customer gets a fresh window to reply
        fulfillment.reminders_sent = "[]"
        user = batch.orders[0].user
        if user:
            user.current_state = SessionState.AWAITING_FEE_ACCEPT
        quote_text = M8C_QUOTE.format(
            area=fulfillment.area_name, fee=naira(payload.fee), total=naira(batch.total_amount)
        )
        phone = user.phone_number if user else None
        await session.commit()
        if phone:
            await whatsapp.send_buttons(
                phone, quote_text, [("quote:accept", "Proceed to pay"), ("quote:cancel", "Cancel order")]
            )
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
        pickup_phone = batch.orders[0].user.phone_number if fulfillment.fulfillment_type == "pickup" and batch.orders else None
        await session.commit()
        if pickup_phone:
            await whatsapp.send_text(pickup_phone, M16_PICKUP_READY.format(
                batch=batch.id, outlet_address=settings.outlet_address, outlet_phone=settings.outlet_phone,
            ))
        return {"status": "ready", "batch_id": batch.id}


@router.post("/api/staff/orders/{batch_id}/dispatch")
async def dispatch_order(batch_id: int, payload: DispatchInput) -> dict:
    async with async_session_factory() as session:
        try:
            result = await state_machine.dispatch_fulfillment(session, batch_id, payload.rider_id)
        except state_machine.DispatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()  # also saves dispatch_error when WhatsApp refused the message
        if result["status"] == "failed":
            raise HTTPException(status_code=502, detail=result["error"] + ". Check the rider's number and try Dispatch again.")
        return result


@router.post("/api/staff/orders/{batch_id}/collected")
async def mark_order_collected(batch_id: int) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None or batch.fulfillment is None:
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        fulfillment = batch.fulfillment
        if fulfillment.fulfillment_type != "pickup":
            raise HTTPException(status_code=409, detail="Only pickup orders can be marked as collected")
        if fulfillment.stage != FulfillmentStage.READY:
            raise HTTPException(status_code=409, detail="Order is not ready to be marked collected")
        fulfillment.stage = FulfillmentStage.COLLECTED
        fulfillment.collected_at = datetime.now(timezone.utc)
        phone = batch.orders[0].user.phone_number if batch.orders else None
        await session.commit()
        if phone:
            await whatsapp.send_buttons(
                phone, M18B_COLLECTED.format(batch=batch.id, brand_name=settings.brand_name), [("menu", "Order again")]
            )
        return {"status": "collected", "batch_id": batch.id}


@router.post("/api/staff/orders/{batch_id}/reassign")
async def reassign_order(batch_id: int, payload: DispatchInput) -> dict:
    """Give a dispatched delivery (not yet picked up) to a different rider."""
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None or batch.fulfillment is None:
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        old_rider_id = batch.fulfillment.rider_id
        try:
            result = await state_machine.dispatch_fulfillment(session, batch_id, payload.rider_id, resend=True)
        except state_machine.DispatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()
        if result["status"] == "failed":
            raise HTTPException(status_code=502, detail=result["error"] + ". The previous rider still has this delivery.")
        if old_rider_id is not None and old_rider_id != payload.rider_id:
            old_rider = await session.get(Rider, old_rider_id)
            if old_rider is not None:
                await whatsapp.send_text(old_rider.phone_number, R6_REASSIGNED.format(batch=batch_id))
        return result


@router.post("/api/staff/orders/{batch_id}/failed")
async def mark_order_failed(batch_id: int, payload: FailureInput | None = None) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None or batch.fulfillment is None or not batch.orders:
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        fulfillment = batch.fulfillment
        if fulfillment.stage not in {FulfillmentStage.READY, FulfillmentStage.DISPATCHED, FulfillmentStage.OUT_FOR_DELIVERY}:
            raise HTTPException(status_code=409, detail="Order is not eligible to be marked failed")
        reason = (payload.reason if payload and payload.reason else "").strip() or "an operational problem"
        await state_machine.fail_fulfillment(
            session, batch, fulfillment, failed_by="staff", internal_reasons=reason, customer_reasons=reason,
        )
        await session.commit()
        return {"status": "failed", "batch_id": batch.id, "reason": reason}


@router.post("/api/staff/orders/{batch_id}/ack-failed")
async def acknowledge_failed(batch_id: int) -> dict:
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None or batch.fulfillment is None:
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        batch.fulfillment.failed_acknowledged_at = datetime.now(timezone.utc)
        await session.commit()
        return {"status": "acknowledged", "batch_id": batch.id}


@router.post("/api/staff/orders/{batch_id}/not-received")
async def mark_order_not_received(batch_id: int) -> dict:
    """Staff acknowledge a customer's "Didn't receive it?" report (the customer sends it from WhatsApp)."""
    async with async_session_factory() as session:
        batch = await order_service.get_batch(session, batch_id)
        if batch is None or batch.fulfillment is None:
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        if batch.fulfillment.not_received_at is None:
            raise HTTPException(status_code=409, detail="The customer hasn't reported this order as not received")
        batch.fulfillment.not_received_acknowledged_at = datetime.now(timezone.utc)
        await session.commit()
        return {"status": "acknowledged", "batch_id": batch.id}


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
        rider = Rider(name=payload.name.strip(), phone_number=phone, is_active=False)
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
        # is_active is NOT editable here: a rider switches it himself by messaging ACTIVE / INACTIVE.
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
        open_deliveries = await session.scalar(
            select(func.count()).select_from(Fulfillment).where(
                Fulfillment.rider_id == rider_id,
                Fulfillment.stage.in_([FulfillmentStage.DISPATCHED, FulfillmentStage.OUT_FOR_DELIVERY]),
            )
        )
        if open_deliveries:
            raise HTTPException(
                status_code=409,
                detail="This rider still has deliveries in progress. Reassign or finish them first.",
            )
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
        history = await session.scalar(
            select(func.count()).select_from(Fulfillment).where(Fulfillment.rider_id == rider_id)
        )
        if history:
            raise HTTPException(
                status_code=409,
                detail="This rider has delivery history, so they can't be deleted. Archive them instead.",
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
        "customer_id": batch.orders[0].user.id,
        "fulfillment": {
            "type": fulfillment.fulfillment_type,
            "stage": fulfillment.stage.value,
            "rider_id": fulfillment.rider_id,
            "rider_name": fulfillment.rider.name if fulfillment.rider else None,
            "rider_phone": fulfillment.rider.phone_number if fulfillment.rider else None,
            "area_name": fulfillment.area_name,
            "street": fulfillment.street,
            "landmark": fulfillment.landmark,
            "phone2": fulfillment.phone2,
            "notes": fulfillment.notes,
            "map_link": state_machine.build_map_link(fulfillment) if fulfillment.fulfillment_type == "delivery" and fulfillment.street else None,
            "expected_minutes": fulfillment.expected_minutes,
            "address_editing": bool(fulfillment.address_editing),
            "dispatch_error": fulfillment.dispatch_error,
            "failure_reasons": fulfillment.failure_reasons,
            "failed_acknowledged": fulfillment.failed_acknowledged_at is not None,
            "not_received_at": fulfillment.not_received_at.isoformat() if fulfillment.not_received_at else None,
            "not_received_acknowledged": fulfillment.not_received_acknowledged_at is not None,
            "approved_at": fulfillment.approved_at.isoformat() if fulfillment.approved_at else None,
            "dispatched_at": fulfillment.dispatched_at.isoformat() if fulfillment.dispatched_at else None,
            "picked_up_at": fulfillment.picked_up_at.isoformat() if fulfillment.picked_up_at else None,
            "delivered_at": fulfillment.delivered_at.isoformat() if fulfillment.delivered_at else None,
            "collected_at": fulfillment.collected_at.isoformat() if fulfillment.collected_at else None,
            "failed_at": fulfillment.failed_at.isoformat() if fulfillment.failed_at else None,
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