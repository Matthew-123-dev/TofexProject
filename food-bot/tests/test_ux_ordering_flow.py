from decimal import Decimal

import pytest

from app import seed
from app.models import FulfillmentStage, MenuItem, Order, OrderItem, OrderStatus, PaymentBatch, SessionState
from app.schemas import IncomingMessage
from app.services import menu_service, order_service, state_machine, whatsapp


@pytest.fixture(autouse=True)
def clear_ux_state():
    state_machine._NAV_STACK.clear()
    state_machine._CUSTOM_QTY_ITEM.clear()
    yield
    state_machine._NAV_STACK.clear()
    state_machine._CUSTOM_QTY_ITEM.clear()


@pytest.mark.asyncio
async def test_ux_step1_friendly_categories_and_single_poll_pagination(db, user):
    """Verify Step 1 friendly category names, popularity sorting, and single-poll category browsing."""
    await seed.main()

    # Step 1: Welcome & Category Poll
    await state_machine.handle_incoming(
        db, IncomingMessage(id="ux-1", sender=user.phone_number, type="text", text="hi")
    )
    assert whatsapp.send_list.called
    step1_args = whatsapp.send_list.call_args[0]
    step1_body = step1_args[1]
    step1_rows = step1_args[4]

    assert "Step 1 of 6" in step1_body
    assert len(step1_body) <= 255
    assert 2 <= len(step1_rows) <= 12
    # Popularity order: Food ("🍛 Rice, Spaghetti & Porridge") first, Snacks ("🌯 Shawarma & Snacks") second
    assert step1_rows[0]["id"] == "cat:Food"
    assert step1_rows[0]["title"] == "🍛 Rice, Spaghetti & Porridge"
    assert step1_rows[1]["id"] == "cat:Snacks"
    assert step1_rows[1]["title"] == "🌯 Shawarma & Snacks"
    assert step1_rows[2]["id"] == "cat:Protein 2"
    assert step1_rows[2]["title"] == "🍗 Grilled Chicken"
    assert step1_rows[3]["id"] == "cat:Protein 3"
    assert step1_rows[3]["title"] == "🦃 Grilled Turkey"
    assert step1_rows[4]["id"] == "cat:Protein 1"
    assert step1_rows[4]["title"] == "🥩 Beef, Fish & Extras"

    # Step 2: Multi-page category (Drinks has 19 items) sends ONLY ONE poll per turn (never send_buttons after send_list)
    whatsapp.send_list.reset_mock()
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="ux-2", sender=user.phone_number, type="interactive", reply_id="cat:Drinks")
    )
    assert whatsapp.send_list.call_count == 1
    assert whatsapp.send_buttons.call_count == 0
    drinks_body = whatsapp.send_list.call_args[0][1]
    drinks_rows = whatsapp.send_list.call_args[0][4]
    assert "Step 2 of 6" in drinks_body
    assert len(drinks_body) <= 255
    assert 2 <= len(drinks_rows) <= 12
    # Popular drinks first (Coke, Fanta, Sprite)
    assert drinks_rows[0]["title"] == "Coke"
    assert drinks_rows[0]["description"] == "₦500"
    row_ids = [r["id"] for r in drinks_rows]
    assert "catpg:Drinks:1" in row_ids
    assert "nav:back" in row_ids
    assert "cancel" in row_ids


@pytest.mark.asyncio
async def test_ux_smart_quantity_5plus_and_sequential_addons(db, user, menu_item):
    """Verify Step 3 smart quantity (1..4 + 5+ typed fallback) and Step 4 sequential add-on poll."""
    menu_item.stock_quantity = 20
    await db.commit()

    # Select item -> Step 3 Quantity Poll
    await state_machine.handle_incoming(
        db, IncomingMessage(id="q-1", sender=user.phone_number, type="interactive", reply_id=f"add:{menu_item.id}")
    )
    qty_body = whatsapp.send_list.call_args[0][1]
    qty_rows = whatsapp.send_list.call_args[0][4]
    assert "Step 3 of 6" in qty_body
    assert len(qty_body) <= 255
    qty_ids = [r["id"] for r in qty_rows]
    assert qty_ids == [
        f"qty:{menu_item.id}:1",
        f"qty:{menu_item.id}:2",
        f"qty:{menu_item.id}:3",
        f"qty:{menu_item.id}:4",
        f"qtycustom:{menu_item.id}",
        "nav:back",
        "cancel",
    ]
    assert qty_rows[0]["description"] == "₦1,000"
    assert qty_rows[3]["description"] == "₦4,000"

    # Tap "5+ portions (Type quantity)" -> receives explicit free-text prompt
    await state_machine.handle_incoming(
        db, IncomingMessage(id="q-2", sender=user.phone_number, type="interactive", reply_id=f"qtycustom:{menu_item.id}")
    )
    prompt_text = whatsapp.send_text.call_args[0][1]
    assert "TYPE QUANTITY BELOW" in prompt_text
    assert "Step 3 of 6" in prompt_text

    # Invalid typed input is rejected with helpful guidance
    await state_machine.handle_incoming(
        db, IncomingMessage(id="q-3", sender=user.phone_number, type="text", text="abc")
    )
    assert "whole number" in whatsapp.send_text.call_args[0][1]

    # Valid typed input "6" adds 6 portions and shows Step 4 add-ons poll
    await state_machine.handle_incoming(
        db, IncomingMessage(id="q-4", sender=user.phone_number, type="text", text="6")
    )
    step4_body = whatsapp.send_buttons.call_args[0][1]
    step4_buttons = whatsapp.send_buttons.call_args[0][2]
    assert "Step 4 of 6" in step4_body
    assert "6 x Test Item" in step4_body
    assert "₦6,000" in step4_body
    assert step4_buttons[0][0] == "checkout"
    assert "No extras" in step4_buttons[0][1]


@pytest.mark.asyncio
async def test_ux_repeat_last_order_and_pickup_confirmation(db, user, menu_item):
    """Verify '🔁 Repeat my last order' fast path and Step 6 Pickup itemized confirmation."""
    # Seed a previous PAID order for this user
    past_batch = PaymentBatch(
        user_id=user.id,
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("2000.00"),
        status=OrderStatus.PAID,
    )
    past_batch.orders = [
        Order(
            user_id=user.id,
            subtotal=Decimal("2000.00"),
            delivery_fee=Decimal("0.00"),
            total_amount=Decimal("2000.00"),
            status=OrderStatus.PAID,
            items=[
                OrderItem(
                    item_id=menu_item.id,
                    item_name=menu_item.name,
                    unit_price=menu_item.price,
                    quantity=2,
                    subtotal=Decimal("2000.00"),
                )
            ],
        )
    ]
    db.add(past_batch)
    await db.commit()

    # Step 1 shows "🔁 Repeat my last order" as first option
    await state_machine.handle_incoming(
        db, IncomingMessage(id="rep-1", sender=user.phone_number, type="text", text="menu")
    )
    rows = whatsapp.send_list.call_args[0][4]
    assert rows[0]["id"] == "repeat:last"
    assert "Repeat my last order" in rows[0]["title"]

    # Tapping "repeat:last" loads 2 x menu_item into cart and shows Step 4 poll
    await state_machine.handle_incoming(
        db, IncomingMessage(id="rep-2", sender=user.phone_number, type="interactive", reply_id="repeat:last")
    )
    step4_body = whatsapp.send_buttons.call_args[0][1]
    assert "Step 4 of 6: Repeat order loaded!" in step4_body
    assert "2 x Test Item" in step4_body

    # Proceed to checkout -> Step 5A Delivery or Pickup
    await state_machine.handle_incoming(
        db, IncomingMessage(id="rep-3", sender=user.phone_number, type="interactive", reply_id="checkout")
    )
    fulfillment = await state_machine.get_open_fulfillment(db, user.id)
    assert fulfillment is not None
    assert fulfillment.checkout_step == "choice"

    # Choose Pickup -> goes to Step 6 Summary first (instead of skipping confirmation!)
    whatsapp.send_text.reset_mock()
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="rep-4", sender=user.phone_number, type="interactive", reply_id="fulfillment:pickup")
    )
    assert fulfillment.checkout_step == "summary"
    summary_msg = whatsapp.send_text.call_args[0][1]
    assert "ORDER SUMMARY (Step 6 of 6)" in summary_msg
    assert "2 x Test Item — ₦2,000" in summary_msg
    confirm_poll_body = whatsapp.send_buttons.call_args[0][1]
    assert "Step 6 of 6: Confirm your order" in confirm_poll_body

    # Confirm Pickup order -> transitions to AWAITING_PAYMENT and sends bank details
    await state_machine.handle_incoming(
        db, IncomingMessage(id="rep-5", sender=user.phone_number, type="interactive", reply_id="address:confirm")
    )
    assert fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    payment_msg = whatsapp.send_text.call_args[0][1]
    assert "Pickup Order Confirmed" in payment_msg


@pytest.mark.asyncio
async def test_ux_go_back_restores_cart_and_steps_backward(db, user, menu_item, delivery_area):
    """Verify '⬅ Go back' navigation across browsing and checkout, preserving cart items."""
    # Browse -> Category -> Quantity -> Go back to Category
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-1", sender=user.phone_number, type="text", text="menu")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-2", sender=user.phone_number, type="interactive", reply_id=f"cat:{menu_item.category}")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-3", sender=user.phone_number, type="interactive", reply_id=f"add:{menu_item.id}")
    )
    assert "Step 3 of 6" in whatsapp.send_list.call_args[0][1]

    # Tap "⬅ Go back" on Step 3 -> returns to Step 2 Category items
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-4", sender=user.phone_number, type="interactive", reply_id="nav:back")
    )
    assert "Step 2 of 6" in whatsapp.send_list.call_args[0][1]

    # Add 2 portions and start checkout -> Step 5A (choice)
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-5", sender=user.phone_number, type="interactive", reply_id=f"qty:{menu_item.id}:2")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-6", sender=user.phone_number, type="interactive", reply_id="checkout")
    )
    fulfillment = await state_machine.get_open_fulfillment(db, user.id)
    assert fulfillment is not None
    assert fulfillment.checkout_step == "choice"

    # Choose Delivery -> Step 5C (area) -> tap "⬅ Go back" -> returns to Step 5A (choice)
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-7", sender=user.phone_number, type="interactive", reply_id="fulfillment:delivery")
    )
    assert fulfillment.checkout_step == "area"
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-8", sender=user.phone_number, type="interactive", reply_id="nav:back")
    )
    assert fulfillment.checkout_step == "choice"

    # Tap "⬅ Go back" again from Step 5A -> cancels draft fulfillment and restores 2 items into cart!
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-9", sender=user.phone_number, type="interactive", reply_id="nav:back")
    )
    assert await state_machine.get_open_fulfillment(db, user.id) is None
    total_qty, subtotal = await order_service.get_user_cart_summary(db, user.id)
    assert total_qty == 2
    assert subtotal == Decimal("2000.00")
