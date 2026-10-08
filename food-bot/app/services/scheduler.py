import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.messages import M13_REMINDER, M14_CANCELLED, M18_DELIVERED, M19_NOT_RECEIVED, R4_PICKUP_REMINDER, R7_WINDOW_WARNING
from app.models import Fulfillment, FulfillmentStage, Order, OrderStatus, PaymentBatch, Rider, SessionState, User
from app.services import whatsapp

logger = logging.getLogger(__name__)


def _reminder_flags(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except (TypeError, ValueError):
        pass
    return []


def _mark_reminder(fulfillment: Fulfillment, key: str) -> None:
    flags = _reminder_flags(fulfillment.reminders_sent)
    if key not in flags:
        flags.append(key)
        fulfillment.reminders_sent = json.dumps(flags)


async def run_once() -> None:
    async with async_session_factory() as session:
        now = datetime.now(timezone.utc)

        result = await session.execute(
            select(Fulfillment)
            .where(Fulfillment.stage == FulfillmentStage.DISPATCHED)
            .where(Fulfillment.rider_id.is_not(None))
            .where(Fulfillment.rider_reminder_sent_at.is_(None))
        )
        for fulfillment in result.scalars():
            if fulfillment.dispatched_at is not None and (now - fulfillment.dispatched_at) >= timedelta(minutes=settings.rider_pickup_reminder_minutes):
                rider = await session.get(Rider, fulfillment.rider_id)
                if rider is not None:
                    await whatsapp.send_text(rider.phone_number, R4_PICKUP_REMINDER.format(batch=fulfillment.payment_batch_id))
                    fulfillment.rider_reminder_sent_at = now

        result = await session.execute(
            select(Fulfillment)
            .where(Fulfillment.stage == FulfillmentStage.DELIVERED)
            .where(Fulfillment.thank_you_due_at.is_not(None))
            .where(Fulfillment.thank_you_sent_at.is_(None))
        )
        for fulfillment in result.scalars():
            if fulfillment.thank_you_due_at is not None and now >= fulfillment.thank_you_due_at:
                batch = await session.get(PaymentBatch, fulfillment.payment_batch_id)
                if batch is not None:
                    user = await session.get(User, batch.user_id)
                    if user is not None:
                        await whatsapp.send_text(user.phone_number, M18_DELIVERED.format(batch=batch.id, brand_name=settings.brand_name))
                    fulfillment.thank_you_sent_at = now

        result = await session.execute(
            select(Fulfillment)
            .where(Fulfillment.stage.in_([
                FulfillmentStage.COLLECTING_DETAILS,
                FulfillmentStage.AWAITING_FEE_QUOTE,
                FulfillmentStage.AWAITING_FEE_ACCEPT,
                FulfillmentStage.AWAITING_PAYMENT,
                FulfillmentStage.AWAITING_APPROVAL,
            ]))
            .where(Fulfillment.timer_started_at.is_not(None))
        )
        for fulfillment in result.scalars():
            batch = await session.get(PaymentBatch, fulfillment.payment_batch_id)
            if batch is None or batch.status != OrderStatus.PENDING:
                continue
            if fulfillment.timer_started_at is None:
                continue
            elapsed = now - fulfillment.timer_started_at
            reminder_key = f"checkout-reminder:{fulfillment.id}"
            cancel_key = f"checkout-cancel:{fulfillment.id}"

            if elapsed >= timedelta(minutes=settings.checkout_cancel_minutes):
                if fulfillment.stage not in {FulfillmentStage.CANCELLED, FulfillmentStage.FAILED}:
                    user = await session.get(User, batch.user_id)
                    if user is not None:
                        what = "your delivery details"
                        if fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT:
                            what = "your payment receipt"
                        elif fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL:
                            what = "payment confirmation"
                        elif fulfillment.stage == FulfillmentStage.AWAITING_FEE_ACCEPT:
                            what = "your delivery fee confirmation"
                        await whatsapp.send_text(user.phone_number, M14_CANCELLED.format(batch=batch.id, what=what))
                        user.current_state = SessionState.BROWSING
                    fulfillment.stage = FulfillmentStage.CANCELLED
                    fulfillment.cancelled_at = now
                    batch.status = OrderStatus.CANCELLED
                    orders = (
                        await session.execute(
                            select(Order).where(Order.payment_batch_id == batch.id)
                        )
                    ).scalars()
                    for order in orders:
                        order.status = OrderStatus.CANCELLED
                    _mark_reminder(fulfillment, cancel_key)
                    continue

            if elapsed >= timedelta(minutes=settings.checkout_reminder_minutes):
                what = "your delivery details"
                if fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT:
                    what = "your payment receipt"
                elif fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL:
                    what = "payment confirmation"
                elif fulfillment.stage == FulfillmentStage.AWAITING_FEE_ACCEPT:
                    what = "your delivery fee confirmation"

                remaining = max(0, settings.checkout_cancel_minutes - int(elapsed.total_seconds() // 60))
                user = await session.get(User, batch.user_id)
                if user is not None and reminder_key not in _reminder_flags(fulfillment.reminders_sent):
                    await whatsapp.send_text(
                        user.phone_number,
                        M13_REMINDER.format(what=what, batch=batch.id, remaining=remaining),
                    )
                    _mark_reminder(fulfillment, reminder_key)

        result = await session.execute(
            select(Fulfillment)
            .where(Fulfillment.stage == FulfillmentStage.FAILED)
            .where(Fulfillment.not_received_at.is_not(None))
            .where(Fulfillment.not_received_acknowledged_at.is_(None))
        )
        for fulfillment in result.scalars():
            batch = await session.get(PaymentBatch, fulfillment.payment_batch_id)
            if batch is None:
                continue
            user = await session.get(User, batch.user_id)
            if user is not None:
                await whatsapp.send_text(user.phone_number, M19_NOT_RECEIVED)
            fulfillment.not_received_acknowledged_at = now

        result = await session.execute(select(Rider).where(Rider.is_active.is_(True)))
        for rider in result.scalars():
            if rider.last_message_at is None:
                continue
            if rider.window_warning_sent_at is None and now >= rider.last_message_at + timedelta(hours=settings.rider_window_warning_hours):
                await whatsapp.send_text(rider.phone_number, R7_WINDOW_WARNING)
                rider.window_warning_sent_at = now

        await session.commit()


async def run_scheduler() -> None:
    while True:
        try:
            await run_once()
        except Exception:
            logger.exception("Fulfillment scheduler failed")
        await asyncio.sleep(30)
