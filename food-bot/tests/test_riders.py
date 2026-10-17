import pytest

from app.models import FulfillmentStage, OrderStatus
from app.schemas import IncomingMessage
from app.services import order_service, state_machine


@pytest.mark.asyncio
async def test_rider_dispatch(db, user, menu_item, delivery_area, batch, rider):
    batch_id = batch.id
    rider_id = rider.id
    rider_phone = rider.phone_number

    # Prepare the order as PAID and READY for delivery
    batch.status = OrderStatus.PAID
    for o in batch.orders:
        o.status = OrderStatus.PAID
    batch.fulfillment.stage = FulfillmentStage.READY
    await db.commit()

    # 1. Dispatching to an inactive rider raises DispatchError
    with pytest.raises(state_machine.DispatchError):
        await state_machine.dispatch_fulfillment(db, batch_id, rider_id)

    # 2. Rider sends "active" to activate their 24h availability window
    await state_machine.handle_incoming(
        db,
        IncomingMessage(id="r1", sender=rider_phone, type="text", text="active"),
    )
    await db.commit()

    # 3. Dispatch now succeeds and transitions fulfillment to DISPATCHED
    result = await state_machine.dispatch_fulfillment(db, batch_id, rider_id)
    await db.commit()
    assert result["status"] == "dispatched"

    db.expunge_all()
    updated = await order_service.get_batch(db, batch_id)
    assert updated.fulfillment.stage == FulfillmentStage.DISPATCHED

    # 4. Rider marks batch as picked up -> OUT_FOR_DELIVERY
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="r2",
            sender=rider_phone,
            type="text",
            text=f"picked {batch_id}",
        ),
    )
    await db.commit()

    db.expunge_all()
    updated = await order_service.get_batch(db, batch_id)
    assert updated.fulfillment.stage == FulfillmentStage.OUT_FOR_DELIVERY

    # 5. Rider marks delivered and confirms -> DELIVERED
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="r3",
            sender=rider_phone,
            type="text",
            text=f"delivered {batch_id}",
        ),
    )
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="r4",
            sender=rider_phone,
            type="interactive",
            reply_id=f"r:yes:{batch_id}",
        ),
    )
    await db.commit()

    db.expunge_all()
    updated = await order_service.get_batch(db, batch_id)
    assert updated.fulfillment.stage == FulfillmentStage.DELIVERED

    # 6. Rider undoes delivery before thank-you is sent -> back to OUT_FOR_DELIVERY
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="r5",
            sender=rider_phone,
            type="text",
            text=f"undo {batch_id}",
        ),
    )
    await db.commit()

    db.expunge_all()
    updated = await order_service.get_batch(db, batch_id)
    assert updated.fulfillment.stage == FulfillmentStage.OUT_FOR_DELIVERY

    # 7. Rider marks delivery failed -> reason "1" -> confirms fail -> FAILED while batch remains PAID
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="r6",
            sender=rider_phone,
            type="text",
            text=f"fail {batch_id}",
        ),
    )
    await state_machine.handle_incoming(
        db,
        IncomingMessage(id="r7", sender=rider_phone, type="text", text="1"),
    )
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="r8",
            sender=rider_phone,
            type="interactive",
            reply_id=f"r:failyes:{batch_id}",
        ),
    )
    await db.commit()

    db.expunge_all()
    updated = await order_service.get_batch(db, batch_id)
    assert updated.fulfillment.stage == FulfillmentStage.FAILED
    assert updated.status == OrderStatus.PAID