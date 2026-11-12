from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import seed
from app.models import (
    DeliveryArea,
    Fulfillment,
    FulfillmentStage,
    MenuItem,
    Rider,
    SavedAddress,
)
from app.schemas import IncomingMessage
from app.services import menu_service, order_parser, order_service, state_machine, whatsapp


@pytest.fixture(autouse=True)
def clear_typed_order_state():
    state_machine._NAV_STACK.clear()
    state_machine._CUSTOM_QTY_ITEM.clear()
    state_machine._TYPED_ORDER_DRAFTS.clear()
    state_machine._TYPED_ORDER_PROMPTED.clear()
    state_machine._KITCHEN_NOTES.clear()
    yield
    state_machine._NAV_STACK.clear()
    state_machine._CUSTOM_QTY_ITEM.clear()
    state_machine._TYPED_ORDER_DRAFTS.clear()
    state_machine._TYPED_ORDER_PROMPTED.clear()
    state_machine._KITCHEN_NOTES.clear()


@pytest.mark.asyncio
async def test_parser_multi_item_slang_naira_and_special_requests(db):
    """Verify rule-based + RapidFuzz parser handles Nigerian slang, Naira amounts, and item modifiers."""
    await seed.main()
    catalog = await menu_service.get_all_items(db)

    draft = order_parser.parse_typed_order(
        "Please I want jollof of 800 add stew, 1 dodo, 1 moi moi, shawarma of 1200 extra pepper no sausage, and two coke",
        catalog,
    )
    assert len(draft.followups) == 0
    assert len(draft.unmatched) == 0

    by_name = {m.name: m for m in draft.matched}
    assert by_name["Jollof rice"].quantity == 2  # ₦800 / ₦400 = 2 portions
    assert by_name["Jollof rice"].notes == ["add stew"]
    assert by_name["Plantain (1)"].quantity == 1  # "dodo" -> Plantain (1)
    assert by_name["Moimoi"].quantity == 1
    assert by_name["Shawama (1200)"].quantity == 1
    assert set(by_name["Shawama (1200)"].notes) == {"extra pepper", "no sausage"}
    assert by_name["Coke"].quantity == 2

    # Non-multiple Naira amount triggers a `naira_mismatch` follow-up poll
    mismatch_draft = order_parser.parse_typed_order("jollof of 700", catalog)
    assert len(mismatch_draft.followups) == 1
    q = mismatch_draft.followups[0]
    assert q.kind == "naira_mismatch"
    assert q.naira_amount == 700
    assert len(q.options) == 2
    assert q.options[0][2] == Decimal("400")
    assert q.options[1][2] == Decimal("800")


@pytest.mark.asyncio
async def test_typed_order_direct_flow_and_kitchen_notes_separation(db, user):
    """Verify Step 1 `✍️ Type your order`, confirmation, kitchen notes on Fulfillment, and stripping from Rider/SavedAddress."""
    await seed.main()
    area = DeliveryArea(name="Akute", fee=Decimal("700.00"), expected_minutes=30, is_active=True, sort_order=1)
    rider = Rider(
        name="Musa",
        phone_number="2348099887766",
        is_active=True,
        is_archived=False,
        last_message_at=datetime.now(timezone.utc),
    )
    db.add_all([area, rider])
    await db.commit()

    # Step 1: Menu shows `✍️ Type your order`
    await state_machine.handle_incoming(
        db, IncomingMessage(id="to-1", sender=user.phone_number, type="text", text="menu")
    )
    step1_rows = whatsapp.send_list.call_args[0][4]
    assert any(r["id"] == "typeorder:start" and r["title"] == "✍️ Type your order" for r in step1_rows)

    # Customer taps `✍️ Type your order`
    whatsapp.send_text.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="to-2", sender=user.phone_number, type="interactive", reply_id="typeorder:start")
    )
    assert whatsapp.send_text.called
    assert "TYPE YOUR ORDER BELOW (Step 2 of 6)" in whatsapp.send_text.call_args[0][1]

    # Customer sends typed order with Jollof Rice ("add stew") and Large Shawarma ("less pepper no sausage")
    whatsapp.send_text.reset_mock()
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="to-3",
            sender=user.phone_number,
            type="text",
            text="2 jollof rice add stew, 1 large shawarma less pepper no sausage, 1 coke",
        ),
    )
    assert whatsapp.send_text.called
    summary_msg = whatsapp.send_text.call_args[0][1]
    assert "WE UNDERSTOOD YOUR ORDER (Step 3 of 6)" in summary_msg
    assert "2 x Jollof rice — ₦800" in summary_msg
    assert "1 x Shawarma (Large) — ₦2,000" in summary_msg
    assert "1 x Coke — ₦500" in summary_msg
    assert "📝 *Kitchen note:*" in summary_msg
    assert "add stew" in summary_msg
    assert "less pepper" in summary_msg
    assert "no sausage" in summary_msg

    assert whatsapp.send_buttons.called
    confirm_buttons = whatsapp.send_buttons.call_args[0][2]
    assert confirm_buttons[0] == ("typeorder:confirm", "✅ Add all to cart (₦3,300)")

    # Customer confirms typed order -> moves to Step 4 of 6 add-ons / checkout poll
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="to-4", sender=user.phone_number, type="interactive", reply_id="typeorder:confirm")
    )
    step4_body = whatsapp.send_buttons.call_args[0][1]
    assert "Step 4 of 6" in step4_body
    assert "₦3,300" in step4_body

    # Proceed to checkout -> Delivery -> Area -> Combined Street & Landmark -> Optional Rider Note via address:extras -> Confirm
    await state_machine.handle_incoming(
        db, IncomingMessage(id="to-5", sender=user.phone_number, type="interactive", reply_id="checkout")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="to-6", sender=user.phone_number, type="interactive", reply_id="fulfillment:delivery")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="to-7", sender=user.phone_number, type="interactive", reply_id=f"area:{area.id}")
    )
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="to-8",
            sender=user.phone_number,
            type="text",
            text="14 Denro Road, Opposite St Leo Church",
        ),
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="to-9", sender=user.phone_number, type="interactive", reply_id="address:extras")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="to-10", sender=user.phone_number, type="interactive", reply_id="extras:notes")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="to-12", sender=user.phone_number, type="text", text="Call at the estate gate")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="to-13", sender=user.phone_number, type="interactive", reply_id="address:confirm")
    )

    # Verify Fulfillment.notes contains BOTH Kitchen note (for dashboard staff) and Delivery note
    fulfillment = (await db.execute(select(Fulfillment).order_by(Fulfillment.id.desc()))).scalars().first()
    assert fulfillment is not None
    assert fulfillment.notes is not None
    assert fulfillment.notes.startswith("Kitchen: ")
    assert "add stew" in fulfillment.notes
    assert "no sausage" in fulfillment.notes
    assert "Delivery: Call at the estate gate" in fulfillment.notes

    # Verify SavedAddress.notes contains ONLY the delivery note (no kitchen prep notes!)
    saved = (await db.execute(select(SavedAddress).where(SavedAddress.user_id == user.id))).scalar_one()
    assert saved.notes == "Call at the estate gate"

    # Mark order Ready and dispatch to rider: verify rider message excludes kitchen notes
    fulfillment.stage = FulfillmentStage.READY
    await db.commit()
    whatsapp.send_buttons.reset_mock()
    res = await state_machine.dispatch_fulfillment(db, fulfillment.payment_batch_id, rider.id)
    assert res["status"] == "dispatched"
    rider_msg = whatsapp.send_buttons.call_args[0][1]
    assert "Notes: Call at the estate gate" in rider_msg
    assert "add stew" not in rider_msg
    assert "no sausage" not in rider_msg


@pytest.mark.asyncio
async def test_typed_order_max_two_followup_polls_and_go_back(db, user):
    """Verify at most 2 follow-up polls are asked, overflow moves to `⚠️ Please specify`, and `⬅ Go back` undoes selection."""
    await seed.main()

    # Customer sends 1 exact item + 4 ambiguous size-family items ("2 chicken, 1 turkey, 1 fish, 1 shawarma")
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db,
        IncomingMessage(
            id="fu-1",
            sender=user.phone_number,
            type="text",
            text="1 jollof rice, 2 chicken, 1 turkey, 1 fish, 1 shawarma",
        ),
    )
    # Follow-up 1 of 2: Chicken size
    assert whatsapp.send_buttons.called
    q1_body, q1_buttons = whatsapp.send_buttons.call_args[0][1], whatsapp.send_buttons.call_args[0][2]
    assert "Step 2 of 6: Quick check 1 of 2" in q1_body
    assert "Grilled Chicken" in q1_body
    assert len(q1_body) <= 255
    assert 2 <= len(q1_buttons) <= 12

    # Pick first chicken option (2x Grilled Chicken — Small)
    first_chicken_id = q1_buttons[0][0]
    assert first_chicken_id.startswith("typeorder:pick:")
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="fu-2", sender=user.phone_number, type="interactive", reply_id=first_chicken_id)
    )

    # Follow-up 2 of 2: Turkey size
    q2_body, q2_buttons = whatsapp.send_buttons.call_args[0][1], whatsapp.send_buttons.call_args[0][2]
    assert "Step 2 of 6: Quick check 2 of 2" in q2_body
    assert "Grilled Turkey" in q2_body

    # Test `⬅ Go back` from Follow-up 2 -> returns to Follow-up 1 and undoes the first chicken selection
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="fu-back", sender=user.phone_number, type="interactive", reply_id="nav:back")
    )
    assert "Step 2 of 6: Quick check 1 of 2" in whatsapp.send_buttons.call_args[0][1]

    # Pick second chicken option this time, then skip turkey on Follow-up 2
    second_chicken_id = q1_buttons[1][0]
    await state_machine.handle_incoming(
        db, IncomingMessage(id="fu-3", sender=user.phone_number, type="interactive", reply_id=second_chicken_id)
    )
    whatsapp.send_text.reset_mock()
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="fu-4", sender=user.phone_number, type="interactive", reply_id="typeorder:skip")
    )

    # Now on Confirmation screen (no 3rd or 4th follow-up poll!): `fish` and `shawarma` are in `⚠️ Please specify`
    assert whatsapp.send_text.called
    summary_text = whatsapp.send_text.call_args[0][1]
    assert "WE UNDERSTOOD YOUR ORDER (Step 3 of 6)" in summary_text
    assert "1 x Jollof rice" in summary_text
    assert "2 x Chicken Medium (2)" in summary_text
    assert "Chicken Small (1)" not in summary_text  # undone by `⬅ Go back`!
    assert "⚠️ *Please specify size/type:*" in summary_text
    assert "fish → type *1 fish big*" in summary_text
    assert "shawarma → type *1 shawarma large*" in summary_text


@pytest.mark.asyncio
async def test_typed_order_non_empty_cart_add_or_replace(db, user):
    """Verify non-empty cart prompts `➕ Add to my cart` vs `🔄 Replace my cart`."""
    await seed.main()
    fried_rice = (await db.execute(select(MenuItem).where(MenuItem.name == "Fried rice"))).scalar_one()
    await order_service.add_to_cart(db, user.id, fried_rice.id, quantity=1)
    await db.commit()

    # Customer types a new order while cart already has 1 x Fried rice
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="cm-1", sender=user.phone_number, type="text", text="2 beef, 1 plantain")
    )
    assert whatsapp.send_buttons.called
    mode_body, mode_buttons = whatsapp.send_buttons.call_args[0][1], whatsapp.send_buttons.call_args[0][2]
    assert "already have 1 item" in mode_body
    mode_ids = [b[0] for b in mode_buttons]
    assert "typeorder:mode:add" in mode_ids
    assert "typeorder:mode:replace" in mode_ids

    # Choose `🔄 Replace my cart` -> shows confirmation -> confirm
    await state_machine.handle_incoming(
        db, IncomingMessage(id="cm-2", sender=user.phone_number, type="interactive", reply_id="typeorder:mode:replace")
    )
    await state_machine.handle_incoming(
        db, IncomingMessage(id="cm-3", sender=user.phone_number, type="interactive", reply_id="typeorder:confirm")
    )

    cart = await order_service.get_or_create_cart(db, user.id)
    lines = await order_service.get_cart(db, cart.id)
    cart_items = {m.name: c.quantity for c, m in lines}
    assert "Fried rice" not in cart_items
    assert cart_items == {"Beef": 2, "Plantain (1)": 1}


@pytest.mark.asyncio
async def test_typed_order_sold_out_alternatives_and_stock_capping(db, user):
    """Verify sold-out items offer in-stock category alternatives and quantities exceeding stock are capped."""
    await seed.main()
    beef = (await db.execute(select(MenuItem).where(MenuItem.name == "Beef"))).scalar_one()
    plantain = (await db.execute(select(MenuItem).where(MenuItem.name == "Plantain (1)"))).scalar_one()
    beef.is_available = False
    plantain.stock_quantity = 2
    await db.commit()

    whatsapp.send_text.reset_mock()
    whatsapp.send_buttons.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="so-1", sender=user.phone_number, type="text", text="1 beef, 5 dodo")
    )
    summary_text = whatsapp.send_text.call_args[0][1]
    assert "⛔ *Sold out:* Beef" in summary_text
    assert "2 x Plantain (1 portion)" in summary_text
    assert "Stock adjusted: only 2 left, from 5" in summary_text

    # Confirmation poll offers up to 2 in-stock alternatives from Protein 1
    buttons = whatsapp.send_buttons.call_args[0][2]
    alt_buttons = [b for b in buttons if b[0].startswith("typeorder:alt:")]
    assert 1 <= len(alt_buttons) <= 2

    # Tap the first alternative -> adds it to the draft and refreshes confirmation
    whatsapp.send_text.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="so-2", sender=user.phone_number, type="interactive", reply_id=alt_buttons[0][0])
    )
    updated_summary = whatsapp.send_text.call_args[0][1]
    assert "⛔ *Sold out:* Beef" not in updated_summary


@pytest.mark.asyncio
async def test_typed_order_unmatched_text_tip(db, user):
    """Verify unmatched text after tapping `✍️ Type your order` shows a helpful example tip and re-opens the menu."""
    await seed.main()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="un-1", sender=user.phone_number, type="interactive", reply_id="typeorder:start")
    )
    whatsapp.send_text.reset_mock()
    whatsapp.send_list.reset_mock()
    await state_machine.handle_incoming(
        db, IncomingMessage(id="un-2", sender=user.phone_number, type="text", text="sushi and pepperoni pizza")
    )
    assert whatsapp.send_text.called
    assert "couldn't match any menu items" in whatsapp.send_text.call_args[0][1]
    assert whatsapp.send_list.called
