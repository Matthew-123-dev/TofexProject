from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import DeliveryArea, FulfillmentStage, OrderStatus, SavedAddress, SessionState
from app.schemas import IncomingMessage
from app.services import order_service, state_machine


@pytest.mark.asyncio
async def test_checkout_flow(db, user, menu_item, delivery_area):
    # 1. Customer opens the menu and adds 1 portion of menu_item to cart
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
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="m4",
            sender=user.phone_number,
            type="interactive",
            reply_id=f"qty:{menu_item.id}:1",
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

    # 3. Choose Delivery -> select DeliveryArea -> enter street -> skip pin -> landmark -> skip phone2 -> skip notes
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

    # 4. Confirm summary -> moves to AWAITING_PAYMENT with total = 1000 + 1500 = 2500
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
    assert batch.total_amount == Decimal("2500.00")
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

    async def start_new_delivery_checkout(prefix: str):
        await state_machine.handle_incoming(
            db,
            IncomingMessage(id=f"{prefix}-add", sender=user_phone, type="interactive", reply_id=f"qty:{item_id}:1"),
        )
        await state_machine.handle_incoming(
            db,
            IncomingMessage(id=f"{prefix}-co", sender=user_phone, type="interactive", reply_id="checkout"),
        )
        await state_machine.handle_incoming(
            db,
            IncomingMessage(
                id=f"{prefix}-deliv", sender=user_phone, type="interactive", reply_id="fulfillment:delivery"
            ),
        )
        return await state_machine.get_open_fulfillment(db, user_id)

    # 1. First order: complete normal address steps and confirm -> saves address
    f1 = await start_new_delivery_checkout("o1")
    assert f1 is not None
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

    # 2. Second order: offers saved_choice; "saved:use" goes straight to AWAITING_PAYMENT with 1500 fee
    f2 = await start_new_delivery_checkout("o2")
    assert f2 is not None
    assert f2.checkout_step == "saved_choice"
    b2_id = f2.payment_batch_id

    await state_machine.handle_incoming(
        db, IncomingMessage(id="o2-use", sender=user_phone, type="interactive", reply_id="saved:use")
    )
    await db.commit()

    db.expunge_all()
    b2 = await order_service.get_batch(db, b2_id)
    assert b2 is not None
    assert b2.fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    assert b2.delivery_fee == Decimal("1500.00")
    assert b2.total_amount == Decimal("2500.00")

    # Cancel order 2 and update area fee to 2200
    await state_machine.handle_incoming(
        db, IncomingMessage(id="o2-cancel", sender=user_phone, type="text", text="cancel")
    )
    area = await db.get(DeliveryArea, area_id)
    area.fee = Decimal("2200.00")
    await db.commit()

    # 3. Third order: reusing saved address picks up the updated 2200 fee
    f3 = await start_new_delivery_checkout("o3")
    assert f3 is not None
    assert f3.checkout_step == "saved_choice"
    b3_id = f3.payment_batch_id

    await state_machine.handle_incoming(
        db, IncomingMessage(id="o3-use", sender=user_phone, type="interactive", reply_id="saved:use")
    )
    await db.commit()

    db.expunge_all()
    b3 = await order_service.get_batch(db, b3_id)
    assert b3 is not None
    assert b3.fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    assert b3.delivery_fee == Decimal("2200.00")
    assert b3.total_amount == Decimal("3200.00")

    # Cancel order 3 and disable the area
    await state_machine.handle_incoming(
        db, IncomingMessage(id="o3-cancel", sender=user_phone, type="text", text="cancel")
    )
    area = await db.get(DeliveryArea, area_id)
    area.is_active = False
    await db.commit()

    # 4. Fourth order: disabled area skips saved_choice and falls back to "area" step
    f4 = await start_new_delivery_checkout("o4")
    assert f4 is not None
    assert f4.checkout_step == "area"