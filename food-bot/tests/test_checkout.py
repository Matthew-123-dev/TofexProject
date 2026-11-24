from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import DeliveryArea, FulfillmentStage, OrderStatus, SavedAddress, SessionState
from app.schemas import IncomingMessage
from app.services import order_service, state_machine


@pytest.mark.asyncio
async def test_checkout_flow(db, user, menu_item, delivery_area):
    # 1. Customer opens the menu and optimistically adds 1 portion of menu_item to cart
    await state_machine.handle_incoming(
        db, IncomingMessage(id="m1", sender=user.phone_number, type="text", text="menu")
    )
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="m2",
            sender=user.phone_number,
            type="interactive",
            reply_id=f"cat:{menu_item.category}",
        ),
    )
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="m3",
            sender=user.phone_number,
            type="interactive",
            reply_id=f"add:{menu_item.id}",
        ),
    )

    # 2. Customer starts checkout
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="m5",
            sender=user.phone_number,
            type="interactive",
            reply_id="checkout",
        ),
    )
    fulfillment = await state_machine.get_open_fulfillment(db, user.id)
    assert fulfillment is not None
    assert fulfillment.stage == FulfillmentStage.COLLECTING_DETAILS
    assert fulfillment.checkout_step == "choice"
    batch_id = fulfillment.payment_batch_id

    # 3. Choose Delivery -> select DeliveryArea -> enter street & landmark -> goes straight to "summary"
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="m6",
            sender=user.phone_number,
            type="interactive",
            reply_id="fulfillment:delivery",
        ),
    )
    assert fulfillment.checkout_step == "area"

    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="m7",
            sender=user.phone_number,
            type="interactive",
            reply_id=f"area:{delivery_area.id}",
        ),
    )
    assert fulfillment.checkout_step == "street"

    # Vague 1-word address is rejected and stays on "street"
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="m8-vague",
            sender=user.phone_number,
            type="text",
            text="Ikeja",
        ),
    )
    assert fulfillment.checkout_step == "street"

    # Combined street + landmark goes directly to "summary"
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="m8",
            sender=user.phone_number,
            type="text",
            text="12 Test Street, Near St. Leo Church",
        ),
    )
    assert fulfillment.checkout_step == "summary"
    assert fulfillment.street == "12 Test Street"
    assert fulfillment.landmark == "Near St. Leo Church"

    # 4. Confirm summary -> moves to AWAITING_PAYMENT with total = 1000 + 200 pack + 1500 delivery = 2700
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="m13",
            sender=user.phone_number,
            type="interactive",
            reply_id="address:confirm",
        ),
    )
    await db.commit()

    db.expunge_all()
    batch = await order_service.get_batch(db, batch_id)
    assert batch is not None
    assert batch.fulfillment is not None
    assert batch.fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    assert batch.packaging_fee == Decimal("200.00")
    assert batch.total_amount == Decimal("2700.00")
    assert batch.status == OrderStatus.PENDING

    # 5. Customer sends "menu" while awaiting payment, then sends receipt image
    user_phone = batch.orders[0].user.phone_number
    await state_machine.handle_incoming(
        db, IncomingMessage(id="m14", sender=user_phone, type="text", text="menu")
    )
    assert batch.orders[0].user.current_state == SessionState.BROWSING

    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="m15",
            sender=user_phone,
            type="image",
            media_id="receipt-photo-001",
        ),
    )
    await db.commit()

    db.expunge_all()
    batch = await order_service.get_batch(db, batch_id)
    assert batch is not None
    assert batch.receipt_media_id == "receipt-photo-001"
    assert batch.fulfillment is not None
    assert batch.fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL
    assert batch.fulfillment.timer_started_at is None


@pytest.mark.asyncio
async def test_saved_address_reuse_fee_update_and_disabled_fallback(db, user, menu_item, delivery_area):
    user_id = user.id
    user_phone = user.phone_number
    item_id = menu_item.id
    area_id = delivery_area.id

    async def start_new_checkout(prefix: str):
        await state_machine.handle_incoming(
            db,
            IncomingMessage(id=f"{prefix}-add", sender=user_phone, type="interactive", reply_id=f"add:{item_id}"),
        )
        await state_machine.handle_incoming(
            db,
            IncomingMessage(id=f"{prefix}-co", sender=user_phone, type="interactive", reply_id="checkout"),
        )
        return await state_machine.get_open_fulfillment(db, user_id)

    # 1. First order (no saved address): goes to "choice" -> select delivery -> area -> street -> summary -> confirm
    f1 = await start_new_checkout("o1")
    assert f1 is not None
    assert f1.checkout_step == "choice"

    await state_machine.handle_incoming(
        db, IncomingMessage(id="o1-deliv", sender=user_phone, type="interactive", reply_id="fulfillment:delivery")
    )
    assert f1.checkout_step == "area"

    await state_machine.handle_incoming(
        db, IncomingMessage(id="o1-area", sender=user_phone, type="interactive", reply_id=f"area:{area_id}")
    )
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="o1-street",
            sender=user_phone,
            type="text",
            text="45 Marina Road, Opposite General Hospital",
        ),
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="o1-ok", sender=user_phone, type="interactive", reply_id="address:confirm")
    )
    await db.commit()

    saved = (await db.execute(select(SavedAddress).where(SavedAddress.user_id == user_id))).scalar_one_or_none()
    assert saved is not None
    assert saved.street == "45 Marina Road"
    assert saved.landmark == "Opposite General Hospital"
    assert saved.area_id == area_id

    # Cancel order 1 so we can start a second order
    await state_machine.handle_incoming(
        db, IncomingMessage(id="o1-cancel", sender=user_phone, type="text", text="cancel")
    )
    await db.commit()

    # 2. Second order: fast-forwards directly to "summary"; confirming reaches AWAITING_PAYMENT (1000 + 200 pack + 1500 deliv)
    f2 = await start_new_checkout("o2")
    assert f2 is not None
    assert f2.checkout_step == "summary"
    b2_id = f2.payment_batch_id

    await state_machine.handle_incoming(
        db, IncomingMessage(id="o2-use", sender=user_phone, type="interactive", reply_id="address:confirm")
    )
    await db.commit()

    db.expunge_all()
    b2 = await order_service.get_batch(db, b2_id)
    assert b2 is not None
    assert b2.fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    assert b2.packaging_fee == Decimal("200.00")
    assert b2.delivery_fee == Decimal("1500.00")
    assert b2.total_amount == Decimal("2700.00")

    # Cancel order 2 and update area fee to 2200
    await state_machine.handle_incoming(
        db, IncomingMessage(id="o2-cancel", sender=user_phone, type="text", text="cancel")
    )
    area = await db.get(DeliveryArea, area_id)
    area.fee = Decimal("2200.00")
    await db.commit()

    # 3. Third order: fast-forward summary picks up the updated 2200 fee
    f3 = await start_new_checkout("o3")
    assert f3 is not None
    assert f3.checkout_step == "summary"
    b3_id = f3.payment_batch_id

    await state_machine.handle_incoming(
        db, IncomingMessage(id="o3-use", sender=user_phone, type="interactive", reply_id="address:confirm")
    )
    await db.commit()

    db.expunge_all()
    b3 = await order_service.get_batch(db, b3_id)
    assert b3 is not None
    assert b3.fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    assert b3.delivery_fee == Decimal("2200.00")
    assert b3.total_amount == Decimal("3400.00")

    # Cancel order 3 and disable the area
    await state_machine.handle_incoming(
        db, IncomingMessage(id="o3-cancel", sender=user_phone, type="text", text="cancel")
    )
    area = await db.get(DeliveryArea, area_id)
    area.is_active = False
    await db.commit()

    # 4. Fourth order: disabled saved area falls back cleanly to "choice" -> delivery -> "area"
    f4 = await start_new_checkout("o4")
    assert f4 is not None
    assert f4.checkout_step == "choice"
    await state_machine.handle_incoming(
        db, IncomingMessage(id="o4-deliv", sender=user_phone, type="interactive", reply_id="fulfillment:delivery")
    )
    assert f4.checkout_step == "area"