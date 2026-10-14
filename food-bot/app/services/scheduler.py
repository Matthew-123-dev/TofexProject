import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session_factory
from app.messages import M13_REMINDER, M14_CANCELLED, M18_DELIVERED, R4_PICKUP_REMINDER, R7_WINDOW_WARNING
from app.models import (
    Fulfillment, FulfillmentStage, Order, OrderStatus, PaymentBatch, ProcessedMessage, Rider, SessionState, User,
)
from app.services import whatsapp

logger = logging.getLogger(__name__)

# Stages where the CUSTOMER still owes us something, so the checkout timer runs.
# Deliberately NOT timed:
#   AWAITING_FEE_QUOTE  - we are waiting for staff to quote a fee
#   AWAITING_APPROVAL   - the customer has paid and sent a receipt; only staff can act now
TIMED_STAGES = (
    FulfillmentStage.COLLECTING_DETAILS,
    FulfillmentStage.AWAITING_FEE_ACCEPT,
    FulfillmentStage.AWAITING_PAYMENT,
)
WHAT_WE_WAIT_FOR = {
    FulfillmentStage.COLLECTING_DETAILS: "your delivery details",
    FulfillmentStage.AWAITING_FEE_ACCEPT: "your decision on the delivery fee",
    FulfillmentStage.AWAITING_PAYMENT: "your payment receipt",
}
CHECKOUT_STATES = (
    SessionState.COLLECTING_DETAILS, SessionState.AWAITING_FEE_QUOTE, SessionState.AWAITING_FEE_ACCEPT,
    SessionState.AWAITING_PAYMENT, SessionState.AWAITING_APPROVAL, SessionState.AWAITING_RECEIPT,
)

_last_cleanup = 0.0


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


def _reminder_offsets() -> list[int]:
    """Reminder times in minutes, e.g. 10 and 20 for a 30-minute limit."""
    step = max(1, settings.checkout_reminder_minutes)
    return [m for m in (step, step * 2) if m < settings.checkout_cancel_minutes]


async def _ids(model, *conditions) -> list[int]:
    async with async_session_factory() as session:
        result = await session.execute(select(model.id).where(*conditions))
        return list(result.scalars())


async def _process_checkout_timers(now: datetime) -> None:
    for fid in await _ids(Fulfillment, Fulfillment.stage.in_(TIMED_STAGES), Fulfillment.timer_started_at.is_not(None)):
        try:
            async with async_session_factory() as session:
                fulfillment = await session.get(Fulfillment, fid)
                if fulfillment is None or fulfillment.stage not in TIMED_STAGES or fulfillment.timer_started_at is None:
                    continue
                batch = await session.get(PaymentBatch, fulfillment.payment_batch_id)
                if batch is None or batch.status != OrderStatus.PENDING:
                    continue
                user = await session.get(User, batch.user_id)
                elapsed = now - fulfillment.timer_started_at
                what = WHAT_WE_WAIT_FOR[fulfillment.stage]

                if elapsed >= timedelta(minutes=settings.checkout_cancel_minutes):
                    fulfillment.stage = FulfillmentStage.CANCELLED
                    fulfillment.cancelled_at = now
                    fulfillment.timer_started_at = None
                    fulfillment.address_editing = False
                    batch.status = OrderStatus.CANCELLED
                    orders = (await session.execute(select(Order).where(Order.payment_batch_id == batch.id))).scalars()
                    for order in orders:
                        order.status = OrderStatus.CANCELLED
                    if user is not None:
                        if user.current_state in CHECKOUT_STATES:
                            user.current_state = SessionState.BROWSING
                        await whatsapp.send_text(user.phone_number, M14_CANCELLED.format(batch=batch.id, what=what))
                    await session.commit()
                    continue

                due = [i for i, minutes in enumerate(_reminder_offsets()) if elapsed >= timedelta(minutes=minutes)]
                if due and user is not None and f"reminder-{due[-1]}" not in _reminder_flags(fulfillment.reminders_sent):
                    remaining = max(1, settings.checkout_cancel_minutes - int(elapsed.total_seconds() // 60))
                    await whatsapp.send_text(
                        user.phone_number, M13_REMINDER.format(what=what, batch=batch.id, remaining=remaining)
                    )
                    for index in due:
                        _mark_reminder(fulfillment, f"reminder-{index}")
                    await session.commit()
        except Exception:
            logger.exception("Checkout timer failed for fulfillment %s", fid)


async def _process_address_edit_timeouts(now: datetime) -> None:
    cutoff = now - timedelta(minutes=settings.address_edit_timeout_minutes)
    ids = await _ids(
        Fulfillment, Fulfillment.address_editing.is_(True), Fulfillment.address_edit_started_at <= cutoff
    )
    for fid in ids:
        try:
            async with async_session_factory() as session:
                fulfillment = await session.get(Fulfillment, fid)
                if fulfillment is None or not fulfillment.address_editing:
                    continue
                fulfillment.address_editing = False
                fulfillment.address_edit_started_at = None
                fulfillment.checkout_step = None
                batch = await session.get(PaymentBatch, fulfillment.payment_batch_id)
                user = await session.get(User, batch.user_id) if batch else None
                if user is not None:
                    if fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL:
                        user.current_state = SessionState.AWAITING_APPROVAL
                    elif fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT:
                        user.current_state = SessionState.AWAITING_PAYMENT
                    await whatsapp.send_text(
                        user.phone_number, "We didn't hear back about your address change, so we kept your previous address."
                    )
                await session.commit()
        except Exception:
            logger.exception("Address-edit timeout failed for fulfillment %s", fid)


async def _process_thank_you(now: datetime) -> None:
    ids = await _ids(
        Fulfillment,
        Fulfillment.stage == FulfillmentStage.DELIVERED,
        Fulfillment.thank_you_due_at.is_not(None),
        Fulfillment.thank_you_sent_at.is_(None),
        Fulfillment.thank_you_due_at <= now,
    )
    for fid in ids:
        try:
            async with async_session_factory() as session:
                fulfillment = await session.get(Fulfillment, fid)
                if fulfillment is None or fulfillment.stage != FulfillmentStage.DELIVERED or fulfillment.thank_you_sent_at:
                    continue
                batch = await session.get(PaymentBatch, fulfillment.payment_batch_id)
                user = await session.get(User, batch.user_id) if batch else None
                if user is not None:
                    await whatsapp.send_buttons(
                        user.phone_number,
                        M18_DELIVERED.format(batch=batch.id, brand_name=settings.brand_name),
                        [("menu", "Order again"), (f"notreceived:{batch.id}", "Didn't receive it?")],
                    )
                fulfillment.thank_you_sent_at = now
                await session.commit()
        except Exception:
            logger.exception("Thank-you failed for fulfillment %s", fid)


async def _process_rider_pickup_reminders(now: datetime) -> None:
    cutoff = now - timedelta(minutes=settings.rider_pickup_reminder_minutes)
    ids = await _ids(
        Fulfillment,
        Fulfillment.stage == FulfillmentStage.DISPATCHED,
        Fulfillment.rider_id.is_not(None),
        Fulfillment.rider_reminder_sent_at.is_(None),
        Fulfillment.dispatched_at <= cutoff,
    )
    for fid in ids:
        try:
            async with async_session_factory() as session:
                fulfillment = await session.get(Fulfillment, fid)
                if fulfillment is None or fulfillment.stage != FulfillmentStage.DISPATCHED or fulfillment.rider_reminder_sent_at:
                    continue
                rider = await session.get(Rider, fulfillment.rider_id)
                if rider is not None:
                    batch_id = fulfillment.payment_batch_id
                    await whatsapp.send_buttons(
                        rider.phone_number,
                        R4_PICKUP_REMINDER.format(batch=batch_id),
                        [(f"r:picked:{batch_id}", "Picked up"), (f"r:fail:{batch_id}", "Can't deliver")],
                    )
                fulfillment.rider_reminder_sent_at = now
                await session.commit()
        except Exception:
            logger.exception("Rider reminder failed for fulfillment %s", fid)


async def _process_rider_windows(now: datetime) -> None:
    """Warn an active rider shortly before WhatsApp's 24-hour messaging window closes."""
    for rid in await _ids(Rider, Rider.is_active.is_(True), Rider.last_message_at.is_not(None)):
        try:
            async with async_session_factory() as session:
                rider = await session.get(Rider, rid)
                if rider is None or rider.last_message_at is None:
                    continue
                warn_at = rider.last_message_at + timedelta(hours=settings.rider_window_warning_hours)
                expires_at = rider.last_message_at + timedelta(hours=24)
                already_warned = (
                    rider.window_warning_sent_at is not None and rider.window_warning_sent_at >= rider.last_message_at
                )
                if warn_at <= now < expires_at and not already_warned:
                    await whatsapp.send_text(rider.phone_number, R7_WINDOW_WARNING)
                    rider.window_warning_sent_at = now
                    await session.commit()
        except Exception:
            logger.exception("Rider window warning failed for rider %s", rid)


async def _cleanup_processed_messages(now: datetime) -> None:
    """Housekeeping, at most once an hour: forget webhook message ids older than 90 days."""
    global _last_cleanup
    if time.monotonic() - _last_cleanup < 3600:
        return
    _last_cleanup = time.monotonic()
    async with async_session_factory() as session:
        await session.execute(delete(ProcessedMessage).where(ProcessedMessage.created_at < now - timedelta(days=90)))
        await session.commit()


async def run_once() -> None:
    now = datetime.now(timezone.utc)
    for step in (
        _process_checkout_timers,
        _process_address_edit_timeouts,
        _process_thank_you,
        _process_rider_pickup_reminders,
        _process_rider_windows,
        _cleanup_processed_messages,
    ):
        try:
            await step(now)
        except Exception:
            logger.exception("Scheduler step %s failed", step.__name__)


async def run_scheduler() -> None:
    while True:
        try:
            await run_once()
        except Exception:
            logger.exception("Fulfillment scheduler failed")
        await asyncio.sleep(30)
