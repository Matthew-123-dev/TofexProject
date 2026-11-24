from datetime import datetime, timedelta, timezone

import pytest

from app import seed
from app.config import settings
from app.models import FulfillmentStage, OrderStatus, User
from app.schemas import IncomingMessage
from app.services import scheduler, state_machine, whatsapp


@pytest.mark.asyncio
async def test_staff_from_me_takeover_pauses_bot_for_two_hours_and_resumes(db, client, user):
    """Manual staff WhatsApp replies pause the bot for 2 hours, stay silent for customers, and resume cleanly."""
    # 1. Staff sends a manual message from WhatsApp (`from_me=True`)
    await state_machine.handle_incoming(
        db,
        IncomingMessage(id="staff-1", sender=user.phone_number, type="text", text="Hi, I'll handle this!", from_me=True),
    )
    await db.commit()
    await db.refresh(user)

    assert user.bot_paused_until is not None
    assert state_machine.is_bot_paused(user) is True
    remaining = user.bot_paused_until - datetime.now(timezone.utc)
    assert timedelta(minutes=118) <= remaining <= timedelta(minutes=121)

    # 2. Customer replies while paused -> bot stays silent (no text/list/buttons sent)
    whatsapp.send_text.reset_mock()
    whatsapp.send_list.reset_mock()
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db,
        IncomingMessage(id="cust-while-paused", sender=user.phone_number, type="text", text="menu"),
    )
    assert not whatsapp.send_text.called
    assert not whatsapp.send_list.called
    assert not whatsapp.send_buttons.called

    # 3. Dashboard takeover API lists the paused chat and lets staff resume immediately
    await client.post(
        "/login",
        data={"username": settings.staff_username, "password": settings.staff_password},
        follow_redirects=False,
    )
    takeovers_res = await client.get("/api/staff/takeovers")
    assert takeovers_res.status_code == 200
    takeovers = takeovers_res.json()
    assert len(takeovers) == 1
    assert takeovers[0]["user_id"] == user.id

    resume_res = await client.post(f"/api/staff/customers/{user.id}/resume-bot")
    assert resume_res.status_code == 200
    db.expunge_all()
    refreshed = await db.get(User, user.id)
    assert state_machine.is_bot_paused(refreshed) is False

    # 4. Now customer sending "menu" gets normal automated menu
    await seed.main()
    await state_machine.handle_incoming(
        db,
        IncomingMessage(id="cust-after-resume", sender=user.phone_number, type="text", text="menu"),
    )
    assert whatsapp.send_list.called


@pytest.mark.asyncio
async def test_customer_talk_to_staff_escalation_and_customer_resume(db, client, user):
    """Tapping 'Talk to staff' or typing 'talk to staff' escalates, pauses bot, surfaces alert, and supports resume."""
    await state_machine.handle_incoming(
        db,
        IncomingMessage(id="esc-1", sender=user.phone_number, type="interactive", reply_id="staff:escalate"),
    )
    await db.commit()
    await db.refresh(user)

    assert state_machine.is_bot_paused(user) is True
    assert user.escalation_requested_at is not None
    assert whatsapp.send_text.called
    assert "paused the automated bot" in whatsapp.send_text.call_args[0][1]

    # Dashboard shows escalation_requested_at set
    await client.post(
        "/login",
        data={"username": settings.staff_username, "password": settings.staff_password},
        follow_redirects=False,
    )
    takeovers = (await client.get("/api/staff/takeovers")).json()
    assert len(takeovers) == 1
    assert takeovers[0]["escalation_requested_at"] is not None

    # Customer can also type "resume bot" to exit pause early
    whatsapp.send_text.reset_mock()
    await state_machine.handle_incoming(
        db,
        IncomingMessage(id="esc-resume", sender=user.phone_number, type="text", text="resume bot"),
    )
    await db.commit()
    await db.refresh(user)
    assert state_machine.is_bot_paused(user) is False
    assert "Automated bot resumed" in whatsapp.send_text.call_args[0][1]


@pytest.mark.asyncio
async def test_scheduler_skips_and_preserves_checkout_timer_while_paused(db, user, batch):
    """Checkout reminder & auto-cancel timers must pause during the 2-hour takeover and keep remaining headroom."""
    now = datetime.now(timezone.utc)
    batch.fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
    batch.fulfillment.timer_started_at = now - timedelta(minutes=25)
    await db.commit()

    # Pause bot for 2 hours (customer only used 25 mins, 5 mins left on timer)
    await state_machine.pause_bot_for_staff(db, user, minutes=120, now=now)
    await db.commit()

    # Even if we advance pause start 2 hours back (simulating 1h55m into takeover), timer is still paused
    user.bot_paused_at = now - timedelta(minutes=115)
    user.bot_paused_until = now + timedelta(minutes=5)
    batch.fulfillment.timer_started_at = now - timedelta(minutes=140)
    await db.commit()

    whatsapp.send_text.reset_mock()
    await scheduler.run_once()
    db.expunge_all()
    still_pending = await state_machine.order_service.get_batch(db, batch.id)
    assert still_pending.status == OrderStatus.PENDING
    assert still_pending.fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    assert not whatsapp.send_text.called

    # Once the 2-hour pause has expired, scheduler auto-resumes and shifts timer forward (keeping ~5 mins left!)
    u = await db.get(User, user.id)
    u.bot_paused_at = now - timedelta(minutes=125)
    u.bot_paused_until = now - timedelta(minutes=5)
    await db.commit()

    await scheduler.run_once()
    db.expunge_all()
    after_expiry = await state_machine.order_service.get_batch(db, batch.id)
    u_after = await db.get(User, user.id)
    assert state_machine.is_bot_paused(u_after) is False
    # Because paused_delta was 120m, timer_started_at shifted from -140m to -20m -> NOT cancelled yet!
    assert after_expiry.status == OrderStatus.PENDING
    assert after_expiry.fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
