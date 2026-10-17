from datetime import datetime, timedelta, timezone

import pytest

from app.models import FulfillmentStage, OrderStatus
from app.services import order_service, scheduler


@pytest.mark.asyncio
async def test_scheduler_fulfillment(db, batch):
    batch_id = batch.id
    now = datetime.now(timezone.utc)

    # 1. AWAITING_APPROVAL is not timed: even after 2 hours, scheduler does not cancel it
    batch.fulfillment.stage = FulfillmentStage.AWAITING_APPROVAL
    batch.fulfillment.timer_started_at = now - timedelta(hours=2)
    await db.commit()

    await scheduler.run_once()
    db.expunge_all()
    updated = await order_service.get_batch(db, batch_id)
    assert updated.fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL
    assert updated.status == OrderStatus.PENDING

    # 2. AWAITING_FEE_QUOTE is also not timed (waiting on staff, not customer)
    updated.fulfillment.stage = FulfillmentStage.AWAITING_FEE_QUOTE
    updated.fulfillment.timer_started_at = now - timedelta(hours=2)
    await db.commit()

    await scheduler.run_once()
    db.expunge_all()
    updated = await order_service.get_batch(db, batch_id)
    assert updated.fulfillment.stage == FulfillmentStage.AWAITING_FEE_QUOTE

    # 3. AWAITING_PAYMENT at 10 minutes sends first reminder and stays AWAITING_PAYMENT
    updated.fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
    updated.fulfillment.reminders_sent = "[]"
    updated.fulfillment.timer_started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await db.commit()

    await scheduler.run_once()
    db.expunge_all()
    updated = await order_service.get_batch(db, batch_id)
    assert updated.fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    assert "reminder-0" in updated.fulfillment.reminders_sent

    # 4. AWAITING_PAYMENT at 20 minutes sends second reminder and stays AWAITING_PAYMENT
    updated.fulfillment.timer_started_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    await db.commit()

    await scheduler.run_once()
    db.expunge_all()
    updated = await order_service.get_batch(db, batch_id)
    assert updated.fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    assert "reminder-1" in updated.fulfillment.reminders_sent

    # 5. AWAITING_PAYMENT past 30 minutes (31 min) auto-cancels the fulfillment and batch
    updated.fulfillment.timer_started_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    await db.commit()

    await scheduler.run_once()
    db.expunge_all()
    updated = await order_service.get_batch(db, batch_id)
    assert updated.fulfillment.stage == FulfillmentStage.CANCELLED
    assert updated.fulfillment.timer_started_at is None
    assert updated.status == OrderStatus.CANCELLED