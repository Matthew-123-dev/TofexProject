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
    """Verify actionable Step 1 welcome poll (<=9 slots), More Categories pagination, and single-poll category browsing."""
    await seed.main()

    # Step 1: Actionable Welcome Poll (3 Hero Slots + 3 Primary Categories + More Categories + Talk to staff = 8 slots)
    await state_machine.handle_incoming(
        db, IncomingMessage(id="ux-1", sender=user.phone_number, type="text", text="hi")
    )
    assert whatsapp.send_list.called
    step1_args = whatsapp.send_list.call_args[0]
    step1_body = step1_args[1]
    step1_rows = step1_args[4]

    assert "Step 1 of 5" in step1_body
    assert len(step1_body) <= 255
    assert 2 <= len(step1_rows) <= 9
    row_ids = [r["id"] for r in step1_rows]
    assert "bundle:jollof_fried_chicken" in row_ids
    assert "bundle:spag_turkey" in row_ids
    assert any(r_id.startswith("add:") for r_id in row_ids)  # Shawarma Large
    assert "cat:Food" in row_ids
    assert "cat:Snacks" in row_ids
    assert "cat:Drinks" in row_ids
    assert "menupg:1" in row_ids
    assert "staff:escalate" in row_ids

    # Page 2 of Categories shows Protein 2, Protein 3, Protein 1, Icecream
    whatsapp.send_list.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="ux-pg2", sender=user.phone_number, type="interactive", reply_id="menupg:1")
    )
    pg2_ids = [r["id"] for r in whatsapp.send_list.call_args[0][4]]
    assert "cat:Protein 2" in pg2_ids
    assert "cat:Protein 3" in pg2_ids
    assert "cat:Protein 1" in pg2_ids
    assert "cat:Icecream" in pg2_ids

    # Verify Food contains only the 4 main meals (no Plantain/Moimoi)
    whatsapp.send_list.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="ux-food", sender=user.phone_number, type="interactive", reply_id="cat:Food")
    )
    food_rows = whatsapp.send_list.call_args[0][4]
    food_titles = [r["title"] for r in food_rows if r["id"].startswith("add:")]
    assert food_titles == ["Jollof rice", "Fried rice", "Spaghetti", "Porridge"]

    # Verify Protein 1 surfaces Plantain and Moimoi alongside Beef/Egg/Salad
    whatsapp.send_list.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="ux-prot1", sender=user.phone_number, type="interactive", reply_id="cat:Protein 1")
    )
    prot1_rows = whatsapp.send_list.call_args[0][4]
    prot1_titles = [r["title"] for r in prot1_rows if r["id"].startswith("add:")]
    assert "Plantain (1 portion)" in prot1_titles
    assert "Moimoi" in prot1_titles
    assert "Beef" in prot1_titles

    # Step 2: Multi-page category (Drinks has 19 items) shows 9 items per page + single poll per turn
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
    assert drinks_rows[0]["title"] == "Coke"
    assert drinks_rows[0]["description"] == "₦500"
    drinks_ids = [r["id"] for r in drinks_rows]
    assert len([i for i in drinks_ids if i.startswith("add:")]) == 9
    assert "catpg:Drinks:1" in drinks_ids
    assert "nav:back" in drinks_ids
    assert "cancel" not in drinks_ids  # hidden 0 shortcut instead of clutter button


@pytest.mark.asyncio
async def test_ux_smart_quantity_5plus_and_sequential_addons(db, user, menu_item):
    """Verify optimistic add -> Add-On Hub -> Change Qty ('qtyset' / 5+ typed fallback)."""
    menu_item.stock_quantity = 20
    await db.commit()

    # Tapping item optimistically adds 1x and shows Add-On Hub (NOT standalone Quantity poll)
    await state_machine.handle_incoming(
        db, IncomingMessage(id="q-1", sender=user.phone_number, type="interactive", reply_id=f"add:{menu_item.id}")
    )
    hub_body = whatsapp.send_buttons.call_args[0][1]
    hub_buttons = whatsapp.send_buttons.call_args[0][2]
    assert "1x Test Item" in hub_body
    assert "₦1,200" in hub_body  # 1000 item + 200 Small pack
    hub_ids = [b[0] for b in hub_buttons]
    assert hub_ids[0] == "checkout"
    assert f"qty_inc:{menu_item.id}:1" in hub_ids
    assert f"qty_menu:{menu_item.id}" in hub_ids

    # Tap "Change Qty" -> explicit Quantity poll (1..4 + 5+ custom)
    await state_machine.handle_incoming(
        db, IncomingMessage(id="q-menu", sender=user.phone_number, type="interactive", reply_id=f"qty_menu:{menu_item.id}")
    )
    qty_body = whatsapp.send_list.call_args[0][1]
    qty_rows = whatsapp.send_list.call_args[0][4]
    assert "Set quantity" in qty_body
    assert len(qty_body) <= 255
    qty_ids = [r["id"] for r in qty_rows]
    assert qty_ids == [
        f"qtyset:{menu_item.id}:1",
        f"qtyset:{menu_item.id}:2",
        f"qtyset:{menu_item.id}:3",
        f"qtyset:{menu_item.id}:4",
        f"qtycustomset:{menu_item.id}",
        "nav:back",
    ]
    assert qty_rows[0]["description"] == "₦1,000"
    assert qty_rows[3]["description"] == "₦4,000"

    # Tap "5+ portions (Type quantity)" -> receives explicit free-text prompt
    await state_machine.handle_incoming(
        db, IncomingMessage(id="q-2", sender=user.phone_number, type="interactive", reply_id=f"qtycustomset:{menu_item.id}")
    )
    prompt_text = whatsapp.send_text.call_args[0][1]
    assert "TYPE QUANTITY BELOW" in prompt_text

    # Invalid typed input is rejected with helpful guidance
    await state_machine.handle_incoming(
        db, IncomingMessage(id="q-3", sender=user.phone_number, type="text", text="abc")
    )
    assert "whole number" in whatsapp.send_text.call_args[0][1]

    # Valid typed input "6" SETS quantity to 6 (not 7!) -> 6000 + 300 Big pack = ₦6,300
    await state_machine.handle_incoming(
        db, IncomingMessage(id="q-4", sender=user.phone_number, type="text", text="6")
    )
    updated_body = whatsapp.send_buttons.call_args[0][1]
    assert "Updated Test Item to 6 portions" in updated_body
    assert "₦6,300" in updated_body


@pytest.mark.asyncio
async def test_ux_repeat_last_order_and_pickup_confirmation(db, user, menu_item):
    """Verify '⚡ Repeat Last Order' fast path (no saved address -> Add-On Hub) and Pickup confirmation."""
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

    # Step 1 shows "⚡ Repeat Last Order" as first option
    await state_machine.handle_incoming(
        db, IncomingMessage(id="rep-1", sender=user.phone_number, type="text", text="menu")
    )
    rows = whatsapp.send_list.call_args[0][4]
    assert rows[0]["id"] == "repeat:last"
    assert "Repeat Last Order" in rows[0]["title"]

    # Tapping "repeat:last" (without saved address) loads 2 x menu_item and opens Add-On Hub
    await state_machine.handle_incoming(
        db, IncomingMessage(id="rep-2", sender=user.phone_number, type="interactive", reply_id="repeat:last")
    )
    hub_body = whatsapp.send_buttons.call_args[0][1]
    assert "Repeat order loaded" in hub_body
    assert "2 x Test Item" in hub_body

    # Proceed to checkout -> Page 1 choice
    await state_machine.handle_incoming(
        db, IncomingMessage(id="rep-3", sender=user.phone_number, type="interactive", reply_id="checkout")
    )
    fulfillment = await state_machine.get_open_fulfillment(db, user.id)
    assert fulfillment is not None
    assert fulfillment.checkout_step == "choice"

    # Choose Pickup -> Order Summary with Small pack (₦200) -> Confirm -> AWAITING_PAYMENT
    whatsapp.send_text.reset_mock()
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="rep-4", sender=user.phone_number, type="interactive", reply_id="fulfillment:pickup")
    )
    assert fulfillment.checkout_step == "summary"
    summary_msg = whatsapp.send_text.call_args[0][1]
    assert "ORDER SUMMARY" in summary_msg
    assert "Akute Branch (No 8, Ishashi Rd)" in summary_msg
    assert "2 x Test Item — ₦2,000" in summary_msg
    assert "Takeaway pack (Small): ₦200" in summary_msg
    assert "TOTAL TO PAY: ₦2,200" in summary_msg
    confirm_poll_body = whatsapp.send_buttons.call_args[0][1]
    assert "Confirm & Pay ₦2,200" in confirm_poll_body

    await state_machine.handle_incoming(
        db, IncomingMessage(id="rep-5", sender=user.phone_number, type="interactive", reply_id="address:confirm")
    )
    assert fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    payment_msg = whatsapp.send_text.call_args[0][1]
    assert "Pickup Order Confirmed" in payment_msg
    assert "₦2,200" in payment_msg


@pytest.mark.asyncio
async def test_ux_go_back_restores_cart_and_steps_backward(db, user, menu_item, delivery_area):
    """Verify '⬅ Go back' navigation across browsing, Add-On Hub, and checkout, preserving cart items."""
    # Browse -> Category -> Optimistic add -> Hub -> Go back to Category
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-1", sender=user.phone_number, type="text", text="menu")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-2", sender=user.phone_number, type="interactive", reply_id=f"cat:{menu_item.category}")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-3", sender=user.phone_number, type="interactive", reply_id=f"add:{menu_item.id}")
    )
    assert whatsapp.send_buttons.called

    # Increment +1 in cart via qty_inc (now 2 portions in cart), then go back to Category and back to Hub
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-inc", sender=user.phone_number, type="interactive", reply_id=f"qty_inc:{menu_item.id}:1")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-4", sender=user.phone_number, type="interactive", reply_id="checkout")
    )
    fulfillment = await state_machine.get_open_fulfillment(db, user.id)
    assert fulfillment is not None
    assert fulfillment.checkout_step == "choice"

    # Choose Delivery -> Area -> tap "⬅ Go back" -> returns to Choice
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-7", sender=user.phone_number, type="interactive", reply_id="fulfillment:delivery")
    )
    assert fulfillment.checkout_step == "area"
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-8", sender=user.phone_number, type="interactive", reply_id="nav:back")
    )
    assert fulfillment.checkout_step == "choice"

    # Tap "⬅ Go back" again from Choice -> cancels draft fulfillment and restores 2 items into cart!
    await state_machine.handle_incoming(
        db, IncomingMessage(id="gb-9", sender=user.phone_number, type="interactive", reply_id="nav:back")
    )
    assert await state_machine.get_open_fulfillment(db, user.id) is None
    total_qty, subtotal = await order_service.get_user_cart_summary(db, user.id)
    assert total_qty == 2
    assert subtotal == Decimal("2000.00")
