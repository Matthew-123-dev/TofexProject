import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote_plus

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Cart,
    DeliveryArea,
    Fulfillment,
    FulfillmentStage,
    MenuItem,
    OrderStatus,
    PaymentBatch,
    ReceiptImage,
    Rider,
    SavedAddress,
    SessionState,
    User,
)
from app.messages import (
    M1_CHOICE, M2_STREET, M3_AREA, M3B_OTHER_AREA, M4_PIN, M5_LANDMARK, M6_SECOND_PHONE, M7_NOTES,
    M8_CONFIRM_POLL, M8_PICKUP_SUMMARY, M8_SUMMARY, M8B_OTHER_AREA, M8C_QUOTE, M9_PAYMENT, M9_PICKUP,
    M10_RECEIPT, M11_APPROVED, M15_LOCKED, M15B_AREA_LOCKED, M17_ON_WAY, M19_NOT_RECEIVED, M20_FAILED,
    R1_DISPATCH, R2_PICKED_UP, R3_CONFIRM_DELIVERED, R3_DELIVERED, R5_HELP, R_ACTIVE, R_INACTIVE,
)
from app.schemas import IncomingMessage
from app.services import menu_service, order_parser, order_service, whatsapp
from app.utils import naira, normalize_phone

logger = logging.getLogger(__name__)

GREETINGS = {"menu", "hi", "hello", "hey", "start"}
BACK_COMMANDS = {"back", "go back", "⬅ go back"}
CANCEL_COMMANDS = {"cancel", "cancel order", "❌ cancel order"}
TALK_TO_STAFF_COMMANDS = {
    "talk to staff",
    "speak to staff",
    "speak with staff",
    "talk to human",
    "speak to human",
    "talk to agent",
    "human",
    "agent",
    "staff",
    "help",
    "support",
    "customer care",
    "customer service",
}
CUSTOMER_RESUME_COMMANDS = {"resume bot", "use bot", "start bot"}
STAFF_RESUME_COMMANDS = {"!resume", "/resume", "#resume"}
MENU_PAGE_SIZE = 9
META_MENU_PAGE_SIZE = 7
CATEGORY_PAGE_SIZE = 9
AREA_PAGE_SIZE = 5
QUANTITY_PAGE_SIZE = 10
MAX_CUSTOM_QUANTITY = 50
MAX_RECEIPT_BYTES = 5 * 1024 * 1024
TYPED_ORDER_TTL_SECONDS = 1800.0

# Per-user navigation history, custom-quantity prompt state, and typed-order state (in-memory, keyed by phone)
_NAV_STACK: dict[str, list[dict]] = {}
_CUSTOM_QTY_ITEM: dict[str, tuple[int, str] | int] = {}
_TYPED_ORDER_DRAFTS: dict[str, tuple[float, order_parser.ParsedOrderDraft]] = {}
_TYPED_ORDER_PROMPTED: set[str] = set()
_KITCHEN_NOTES: dict[str, str] = {}
_JOURNEY_STATE: dict[str, dict] = {}
_PRESELECTED_AREA: dict[str, tuple[int, str]] = {}
_PREV_CART_SNAPSHOT: dict[str, list[tuple[int, int]]] = {}
_LAST_TYPED_ITEMS: dict[str, list[tuple[int, int, str | None]]] = {}
_USED_SAVED_SUMMARY: set[str] = set()


def _effective_menu_page_size() -> int:
    if not whatsapp.is_evolution_provider() and not settings.dry_run:
        return META_MENU_PAGE_SIZE
    return MENU_PAGE_SIZE


def _clear_user_ephemeral_state(phone: str, *, clear_typed: bool = True) -> None:
    _CUSTOM_QTY_ITEM.pop(phone, None)
    _NAV_STACK.pop(phone, None)
    _KITCHEN_NOTES.pop(phone, None)
    _JOURNEY_STATE.pop(phone, None)
    _PRESELECTED_AREA.pop(phone, None)
    _PREV_CART_SNAPSHOT.pop(phone, None)
    _LAST_TYPED_ITEMS.pop(phone, None)
    _USED_SAVED_SUMMARY.discard(phone)
    if clear_typed:
        _clear_typed_draft(phone)


def _journey_update(phone: str, **kwargs) -> dict:
    st = _JOURNEY_STATE.setdefault(
        phone,
        {"mode": "browse", "has_saved": False, "preselected_area": False, "fulfillment": None},
    )
    st.update(kwargs)
    return st


def _step_counts(phone: str, stage: str) -> tuple[int, int]:
    """Compute `(current_step, total_steps)` dynamically for the user's active path."""
    st = _JOURNEY_STATE.get(phone, {})
    mode = st.get("mode", "browse")
    has_saved = bool(st.get("has_saved", False))
    pre_area = bool(st.get("preselected_area", False))
    fulfill = st.get("fulfillment")

    if stage == "welcome":
        return 1, (3 if has_saved else 5)
    if stage in {"category", "quantity"}:
        return 2, (4 if has_saved else 6)
    if stage in {"hub", "carts", "typed_prompt", "typed_followup"}:
        if mode == "quick":
            total = 3 if has_saved else (4 if pre_area else 5)
            return 2, total
        return 3, (4 if has_saved else 6)
    if stage == "choice":
        base = 2 if mode == "quick" else 3
        return base + 1, base + 3
    if stage in {"area", "other_area"}:
        base = 2 if mode == "quick" else 3
        return base + 2, base + 4
    if stage == "street":
        if pre_area and not has_saved:
            return 3, 4
        base = 2 if mode == "quick" else 3
        return base + 3, base + 4
    if stage in {"extras", "pin", "landmark", "phone2", "notes"}:
        _, tot = _step_counts(phone, "summary")
        return max(1, tot - 1), tot
    # stage == "summary"
    if mode == "repeat" and has_saved and fulfill == "delivery":
        return 2, 2
    if has_saved and fulfill == "delivery":
        return (3, 3) if mode == "quick" else (4, 4)
    if pre_area and fulfill == "delivery":
        return 4, 4
    base = 2 if mode == "quick" else 3
    if fulfill == "pickup":
        return base + 2, base + 2
    return base + 4, base + 4


def _step_label(phone: str, stage: str) -> str:
    cur, tot = _step_counts(phone, stage)
    return f"Step {cur} of {tot}"


def _format_poll_header(title_line: str, context_line: str = "", *, include_hint: bool = True) -> str:
    parts = [title_line.strip()]
    if context_line.strip():
        parts.append(context_line.strip())
    if include_hint:
        parts.append("(0=cancel, HELP=staff)")
    text = "\n".join(parts)
    if len(text) <= 255:
        return text
    if include_hint and context_line.strip():
        short_ctx = context_line.strip()[: max(20, 255 - len(parts[0]) - len("(0=cancel, HELP=staff)") - 4)]
        text = f"{parts[0]}\n{short_ctx}\n(0=cancel, HELP=staff)"
    return text[:255]


def _pack_summary_line(pack_fee: Decimal) -> str:
    if pack_fee <= 0:
        return ""
    size = "Big" if pack_fee >= order_service.PACKAGING_BIG_FEE else "Small"
    return f"• Takeaway pack ({size}): {naira(pack_fee)}\n"


def _short_item_name(name: str, max_len: int = 18) -> str:
    friendly = menu_service.friendly_item_name(name)
    if len(friendly) <= max_len:
        return friendly
    return friendly[: max_len - 1].rstrip() + "…"


def _get_typed_draft(phone: str) -> order_parser.ParsedOrderDraft | None:
    """Return the active `ParsedOrderDraft` for `phone` if within TTL, else `None`."""
    entry = _TYPED_ORDER_DRAFTS.get(phone)
    if entry is None:
        return None
    ts, draft = entry
    if time.monotonic() - ts > TYPED_ORDER_TTL_SECONDS:
        _TYPED_ORDER_DRAFTS.pop(phone, None)
        return None
    return draft


def _set_typed_draft(phone: str, draft: order_parser.ParsedOrderDraft) -> None:
    """Store `draft` with a fresh timestamp."""
    _TYPED_ORDER_DRAFTS[phone] = (time.monotonic(), draft)


def _clear_typed_draft(phone: str) -> None:
    """Clear any typed-order draft and prompt flag for `phone`."""
    _TYPED_ORDER_DRAFTS.pop(phone, None)
    _TYPED_ORDER_PROMPTED.discard(phone)


def _extract_kitchen_prefix(notes: str | None) -> str | None:
    """Extract only the `Kitchen: ...` portion of a fulfillment notes string, if present."""
    if not notes:
        return None
    if notes.startswith("Kitchen: "):
        return notes.split(" | Delivery: ", 1)[0].strip() or None
    return None


def _extract_rider_notes(notes: str | None) -> str | None:
    """Extract only the rider/delivery portion of a fulfillment notes string (excluding `Kitchen: ...`)."""
    if not notes:
        return None
    if notes.startswith("Kitchen: "):
        if " | Delivery: " in notes:
            return notes.split(" | Delivery: ", 1)[1].strip() or None
        return None
    return notes


def _merge_kitchen_and_rider_notes(existing_notes: str | None, rider_note: str | None) -> str | None:
    """Combine any existing `Kitchen: ...` note with a delivery/rider note."""
    kitchen_part = _extract_kitchen_prefix(existing_notes)
    rider_clean = (rider_note or "").strip() or None
    if kitchen_part and rider_clean:
        return f"{kitchen_part} | Delivery: {rider_clean}"[:200]
    if kitchen_part:
        return kitchen_part[:200]
    return rider_clean[:200] if rider_clean else None

# While a customer is in these stages the conversation belongs to the checkout:
# the only way out is `cancel` or `back`.
CHECKOUT_LOCKED_STAGES = (
    FulfillmentStage.COLLECTING_DETAILS,
    FulfillmentStage.AWAITING_FEE_QUOTE,
    FulfillmentStage.AWAITING_FEE_ACCEPT,
)
IN_PROGRESS_STAGES = (
    FulfillmentStage.PREPARING, FulfillmentStage.READY,
    FulfillmentStage.DISPATCHED, FulfillmentStage.OUT_FOR_DELIVERY,
)
OPEN_DELIVERY_STAGES = (FulfillmentStage.READY, FulfillmentStage.DISPATCHED, FulfillmentStage.OUT_FOR_DELIVERY)

# Rider failure reasons: code -> (internal label, wording for the customer)
FAIL_REASONS = {
    "1": ("customer unreachable", "we couldn't reach you by phone"),
    "2": ("wrong address", "the address couldn't be found"),
    "3": ("customer refused", "the order wasn't accepted on delivery"),
    "4": ("wrong number", "the phone number provided was incorrect"),
    "5": ("other", None),
}
FAIL_PROMPT = (
    "Please reply with the reason numbers, for example: 1,3\n1 Customer unreachable\n2 Wrong address\n"
    "3 Customer refused\n4 Wrong number\n5 Other (add a note, e.g. 5 gate was locked)"
)


class DispatchError(ValueError):
    """A dispatch was refused for a reason staff can fix (the message is safe to show them)."""


def _push_nav(phone: str, screen: dict) -> None:
    """Record a screen in the user's navigation history stack for `⬅ Go back`."""
    stack = _NAV_STACK.setdefault(phone, [])
    if not stack or stack[-1] != screen:
        stack.append(screen)
        if len(stack) > 12:
            stack.pop(0)


def _pop_nav(phone: str) -> dict | None:
    """Pop the current screen and return the previous screen if available."""
    stack = _NAV_STACK.get(phone)
    if not stack:
        return None
    if len(stack) >= 2:
        stack.pop()
        return stack.pop()
    stack.clear()
    return None


async def _cart_status_line(session: AsyncSession | None, user_id: int) -> str:
    """Build a concise 1-line cart status summary for poll headers (Principle 2)."""
    if session is None:
        return "🛒 Cart: Empty"
    total_qty, subtotal, pack_fee = await order_service.get_user_cart_totals(session, user_id)
    if total_qty <= 0:
        return "🛒 Cart: Empty"
    total = subtotal + pack_fee
    if pack_fee > 0:
        return f"🛒 Cart: {total_qty} item{'s' if total_qty != 1 else ''} · {naira(total)} (incl. {naira(pack_fee)} pack)"
    return f"🛒 Cart: {total_qty} item{'s' if total_qty != 1 else ''} ({naira(total)})"


def _batch_items_block(batch: PaymentBatch) -> str:
    """Format all items in a payment batch for the Step 6 Order Summary."""
    lines = []
    for order in batch.orders:
        for oi in order.items:
            display = menu_service.friendly_item_name(oi.item_name)
            lines.append(f"• {oi.quantity} x {display} — {naira(oi.subtotal)}")
    return "\n".join(lines) if lines else "• Your selected items"


def _batch_totals(batch: PaymentBatch) -> tuple[int, Decimal]:
    """Return `(total_item_count, subtotal)` across a PaymentBatch's orders."""
    qty = sum(sum(oi.quantity for oi in o.items) for o in batch.orders)
    sub = sum((o.subtotal for o in batch.orders), Decimal("0"))
    return qty, sub


async def get_or_create_user(session: AsyncSession, phone: str) -> User:
    result = await session.execute(select(User).where(User.phone_number == phone))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(phone_number=phone, current_state=SessionState.BROWSING)
        session.add(user)
        await session.flush()
    return user


async def get_rider_by_phone(session: AsyncSession, phone: str) -> Rider | None:
    try:
        normalized = normalize_phone(phone)
    except ValueError:
        return None
    result = await session.execute(select(Rider).where(Rider.phone_number == normalized, Rider.is_archived.is_(False)))
    return result.scalar_one_or_none()


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def is_bot_paused(user: User, now: datetime | None = None) -> bool:
    """Return True when the bot is currently paused for staff takeover."""
    until = _utc(getattr(user, "bot_paused_until", None))
    if until is None:
        return False
    ref = _utc(now) or datetime.now(timezone.utc)
    return until > ref


async def pause_bot_for_staff(
    session: AsyncSession,
    user: User,
    *,
    minutes: int | None = None,
    escalated: bool = False,
    now: datetime | None = None,
) -> datetime:
    """Pause automated bot replies and checkout timers for `user` (defaults to 2 hours)."""
    ref = _utc(now) or datetime.now(timezone.utc)
    pause_mins = max(1, minutes if minutes is not None else settings.staff_takeover_minutes)
    if getattr(user, "bot_paused_at", None) is None or not is_bot_paused(user, ref):
        user.bot_paused_at = ref
    user.bot_paused_until = ref + timedelta(minutes=pause_mins)
    if escalated:
        user.escalation_requested_at = ref
    else:
        user.escalation_requested_at = None
    await session.flush()
    return user.bot_paused_until


async def _shift_open_fulfillment_timers(session: AsyncSession, user_id: int, paused_delta: timedelta) -> None:
    """Shift open checkout/address-edit timers forward so paused time does not count toward cancellation."""
    if paused_delta <= timedelta(0):
        return
    rows = (
        await session.execute(
            select(Fulfillment)
            .join(PaymentBatch, Fulfillment.payment_batch_id == PaymentBatch.id)
            .where(
                PaymentBatch.user_id == user_id,
                PaymentBatch.status == OrderStatus.PENDING,
                Fulfillment.stage.in_(
                    (
                        FulfillmentStage.COLLECTING_DETAILS,
                        FulfillmentStage.AWAITING_FEE_ACCEPT,
                        FulfillmentStage.AWAITING_PAYMENT,
                        FulfillmentStage.AWAITING_APPROVAL,
                    )
                ),
            )
        )
    ).scalars().all()
    for fulfillment in rows:
        if fulfillment.timer_started_at is not None:
            fulfillment.timer_started_at = _utc(fulfillment.timer_started_at) + paused_delta
        if fulfillment.address_editing and fulfillment.address_edit_started_at is not None:
            fulfillment.address_edit_started_at = _utc(fulfillment.address_edit_started_at) + paused_delta


async def resume_bot(session: AsyncSession, user: User, *, now: datetime | None = None) -> bool:
    """Clear staff takeover pause on `user` and restore shifted checkout timer headroom."""
    ref = _utc(now) or datetime.now(timezone.utc)
    paused_at = _utc(getattr(user, "bot_paused_at", None))
    paused_until = _utc(getattr(user, "bot_paused_until", None))
    was_active = paused_at is not None or paused_until is not None or getattr(user, "escalation_requested_at", None) is not None
    if paused_at is not None:
        effective_end = min(ref, paused_until) if paused_until is not None else ref
        if effective_end > paused_at:
            await _shift_open_fulfillment_timers(session, user.id, effective_end - paused_at)
    user.bot_paused_at = None
    user.bot_paused_until = None
    user.escalation_requested_at = None
    await session.flush()
    return was_active


async def handle_talk_to_staff(session: AsyncSession, user: User) -> None:
    """Escalate the conversation to human staff and pause the bot for 2 hours."""
    await pause_bot_for_staff(session, user, escalated=True)
    mins = max(1, settings.staff_takeover_minutes)
    duration_label = f"{mins // 60} hours" if mins >= 120 and mins % 60 == 0 else (
        "1 hour" if mins == 60 else f"{mins} minutes"
    )
    await whatsapp.send_text(
        user.phone_number,
        (
            f"🙋 I've paused the automated bot and alerted our team. A staff member will reply to you right here shortly "
            f"(or call {settings.support_phone} if urgent).\n\n"
            f"_(The bot will stay quiet for {duration_label} so you can chat freely. "
            f"Reply *resume bot* anytime if you'd like to return to the automated menu.)_"
        ),
    )


async def handle_incoming(session: AsyncSession, msg: IncomingMessage) -> None:
    """Route one WhatsApp message: riders first, staff takeover, then open checkout, then browsing."""
    rider = await get_rider_by_phone(session, msg.sender)
    if msg.from_me:
        if rider is not None:
            return
        user = await get_or_create_user(session, msg.sender)
        staff_cmd = (msg.text or "").strip().lower()
        if staff_cmd in STAFF_RESUME_COMMANDS:
            await resume_bot(session, user)
        else:
            await pause_bot_for_staff(session, user, escalated=False)
        return

    if rider is not None:
        rider.last_message_at = datetime.now(timezone.utc)
        await handle_rider_message(session, rider, msg)
        return

    user = await get_or_create_user(session, msg.sender)
    raw_text = (msg.text or "").strip()
    text = raw_text.lower()
    rid = msg.reply_id or ""
    now = datetime.now(timezone.utc)

    if getattr(user, "bot_paused_until", None) is not None:
        if is_bot_paused(user, now):
            if rid == "staff:resume" or text in CUSTOMER_RESUME_COMMANDS:
                await resume_bot(session, user, now=now)
                await whatsapp.send_text(
                    user.phone_number,
                    "🤖 Automated bot resumed! Send *menu* to browse or continue your order.",
                )
            return
        await resume_bot(session, user, now=now)

    if rid == "staff:escalate" or text in TALK_TO_STAFF_COMMANDS:
        return await handle_talk_to_staff(session, user)

    # An open checkout owns the conversation. We route by the ORDER, not by user.current_state,
    # so browsing the menu can never make the bot ignore a receipt photo or an address answer.
    open_fulfillment = await get_open_fulfillment(session, user.id)
    if open_fulfillment is not None and (
        open_fulfillment.stage in CHECKOUT_LOCKED_STAGES
        or open_fulfillment.address_editing
        or msg.type in ("image", "location")
        or text in CANCEL_COMMANDS
        or text in BACK_COMMANDS
        or text == "address"
        or rid in {"cancel", "nav:back", "checkout:edit_items", "cart:view"}
    ):
        return await handle_checkout_flow(session, user, open_fulfillment, msg)

    if text == "address":
        if await get_in_progress_fulfillment(session, user.id) is not None:
            await whatsapp.send_text(user.phone_number, M15_LOCKED.format(support_phone=settings.support_phone))
            return

    if text == "profile":
        user.current_state = SessionState.EDITING_PROFILE
        await whatsapp.send_text(
            user.phone_number,
            f"Your saved name is {user.name or 'not set'}. Reply with `name: Your Name` to update it.",
        )
        return
    if text.startswith("name:"):
        name = raw_text.split(":", 1)[1].strip()
        if not name:
            await whatsapp.send_text(user.phone_number, "Please provide a name after `name:`.")
            return
        user.name = name[:120]
        user.current_state = SessionState.BROWSING
        await whatsapp.send_text(user.phone_number, f"Saved your name as {user.name}.")
        return

    # Escape routes during browsing: Cancel or Go Back
    if rid == "cancel" or text in CANCEL_COMMANDS:
        _clear_user_ephemeral_state(user.phone_number)
        if session is not None:
            await order_service.clear_user_carts(session, user.id)
        user.current_state = SessionState.BROWSING
        await whatsapp.send_text(
            user.phone_number,
            "❌ Your current order has been cancelled and your cart has been cleared. Send *menu* whenever you're ready to start fresh!",
        )
        return

    if rid == "nav:back" or text in BACK_COMMANDS:
        return await handle_go_back_browsing(session, user)

    # Custom 5+ quantity free-text input when active
    if user.phone_number in _CUSTOM_QTY_ITEM and not rid and text not in GREETINGS:
        entry = _CUSTOM_QTY_ITEM[user.phone_number]
        item_id, qty_mode = entry if isinstance(entry, tuple) else (entry, "add")
        if raw_text.isdecimal():
            qty_val = int(raw_text)
            if 1 <= qty_val <= MAX_CUSTOM_QUANTITY:
                _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
                if qty_mode == "set":
                    return await handle_set_item_quantity(session, user, item_id, qty_val)
                return await handle_add_item(session, user, item_id, qty_val)
        await whatsapp.send_text(
            user.phone_number,
            f"⚠️ Please type a whole number between 1 and {MAX_CUSTOM_QUANTITY} (for example: *6*), or reply *BACK* to go back.",
        )
        return

    if text in GREETINGS or rid in {"menu", "nav:categories"}:
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        _clear_typed_draft(user.phone_number)
        return await handle_menu_request(session, user)
    if rid in {"typeorder:start", "typeorder:retype"}:
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        return await prompt_typed_order(user)
    if rid.startswith("typeorder:mode:"):
        return await handle_typed_order_mode(session, user, rid.removeprefix("typeorder:mode:"))
    if rid.startswith("typeorder:pick:"):
        return await handle_typed_order_pick(session, user, rid.removeprefix("typeorder:pick:"))
    if rid == "typeorder:skip":
        return await handle_typed_order_skip(session, user)
    if rid.startswith("typeorder:alt:"):
        return await handle_typed_order_alt(session, user, rid.removeprefix("typeorder:alt:"))
    if rid == "typeorder:confirm":
        return await handle_typed_order_confirm(session, user)
    if rid == "typeorder:replace_prev":
        return await handle_typed_order_replace_prev(session, user)
    if rid == "repeat:last" or text in {"repeat", "repeat last order", "repeat order"}:
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        _clear_typed_draft(user.phone_number)
        return await handle_repeat_last_order(session, user)
    if rid.startswith("notreceived:"):
        batch_text = rid.removeprefix("notreceived:")
        return await handle_not_received(session, user, int(batch_text) if batch_text.isdecimal() else 0)
    if rid.startswith("menupg:"):
        menu_page_text = rid.removeprefix("menupg:")
        if not menu_page_text.isdecimal():
            await whatsapp.send_text(user.phone_number, "That menu page is invalid. Send *menu* to start again.")
            return
        return await handle_menu_request(session, user, int(menu_page_text))
    if rid.startswith("catpg:"):
        category_page = rid.removeprefix("catpg:")
        if ":" not in category_page:
            await whatsapp.send_text(user.phone_number, "That menu page is invalid. Send *menu* to start again.")
            return
        category, page_text = category_page.rsplit(":", 1)
        if not page_text.isdecimal():
            await whatsapp.send_text(user.phone_number, "That menu page is invalid. Send *menu* to start again.")
            return
        return await handle_category_selected(session, user, category, int(page_text))
    if rid.startswith("cat:"):
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        _clear_typed_draft(user.phone_number)
        return await handle_category_selected(session, user, rid.removeprefix("cat:"))
    if rid.startswith("bundle:"):
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        return await handle_bundle_add(session, user, rid)
    if rid.startswith("add:"):
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        item_id_text = rid.removeprefix("add:")
        if not item_id_text.isdecimal():
            await whatsapp.send_text(user.phone_number, "That menu item is invalid. Send *menu* to start again.")
            return
        return await handle_optimistic_add(session, user, int(item_id_text))
    if rid.startswith("quick_add:"):
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        item_id_text = rid.removeprefix("quick_add:")
        if not item_id_text.isdecimal():
            await whatsapp.send_text(user.phone_number, "That menu item is invalid. Send *menu* to start again.")
            return
        return await handle_optimistic_add(session, user, int(item_id_text))
    if rid.startswith("qty_inc:"):
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        inc_parts = rid.removeprefix("qty_inc:").split(":")
        if len(inc_parts) != 2 or not all(part.isdecimal() for part in inc_parts):
            await whatsapp.send_text(user.phone_number, "That quantity is invalid. Send *menu* to start again.")
            return
        return await handle_add_item(session, user, int(inc_parts[0]), int(inc_parts[1]))
    if rid.startswith("qty_menu:"):
        item_id_text = rid.removeprefix("qty_menu:")
        if not item_id_text.isdecimal():
            await whatsapp.send_text(user.phone_number, "That menu item is invalid. Send *menu* to start again.")
            return
        return await show_quantity_selection(session, user, int(item_id_text), mode="set")
    if rid.startswith("qtyset:"):
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        qty_parts = rid.removeprefix("qtyset:").split(":")
        if len(qty_parts) != 2 or not all(part.isdecimal() for part in qty_parts):
            await whatsapp.send_text(user.phone_number, "That quantity is invalid. Send *menu* to start again.")
            return
        return await handle_set_item_quantity(session, user, int(qty_parts[0]), int(qty_parts[1]))
    if rid.startswith("qtycustomset:"):
        item_id_text = rid.removeprefix("qtycustomset:")
        if not item_id_text.isdecimal():
            await whatsapp.send_text(user.phone_number, "That menu item is invalid. Send *menu* to start again.")
            return
        return await prompt_custom_quantity(session, user, int(item_id_text), mode="set")
    if rid.startswith("qtycustom:"):
        item_id_text = rid.removeprefix("qtycustom:")
        if not item_id_text.isdecimal():
            await whatsapp.send_text(user.phone_number, "That menu item is invalid. Send *menu* to start again.")
            return
        return await prompt_custom_quantity(session, user, int(item_id_text), mode="add")
    if rid.startswith("qty:"):
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        quantity_parts = rid.removeprefix("qty:").split(":")
        if len(quantity_parts) != 2 or not all(part.isdecimal() for part in quantity_parts):
            await whatsapp.send_text(user.phone_number, "That quantity is invalid. Send *menu* to start again.")
            return
        return await handle_add_item(session, user, int(quantity_parts[0]), int(quantity_parts[1]))
    if rid in {"carts", "cart:view"} or text in {"cart", "carts"}:
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        _clear_typed_draft(user.phone_number)
        return await show_carts(session, user)
    if rid.startswith("rmitem:"):
        item_id_text = rid.removeprefix("rmitem:")
        if item_id_text.isdecimal():
            cart = await order_service.get_or_create_cart(session, user.id)
            await order_service.remove_from_cart(session, cart.id, int(item_id_text))
        return await show_carts(session, user)
    if rid == "clearcart" or text in {"clear cart", "clearcart"}:
        _clear_typed_draft(user.phone_number)
        _KITCHEN_NOTES.pop(user.phone_number, None)
        _PRESELECTED_AREA.pop(user.phone_number, None)
        if session is not None:
            await order_service.clear_user_carts(session, user.id)
        await whatsapp.send_text(user.phone_number, "🗑️ Your cart has been cleared. Send *menu* to pick new items.")
        return
    if rid == "newcart" or text in {"newcart", "new cart"}:
        await order_service.create_cart(session, user.id)
        await whatsapp.send_text(user.phone_number, "Created a new cart. Send *menu* to add items to it.")
        return
    if rid.startswith("cart:"):
        cart_text = rid.removeprefix("cart:")
        if not cart_text.isdecimal():
            await whatsapp.send_text(user.phone_number, "That cart could not be found.")
            return
        return await toggle_cart(session, user, int(cart_text))
    if rid in {"checkout", "checkout:start"} or text in {"checkout", "check out"}:
        _CUSTOM_QTY_ITEM.pop(user.phone_number, None)
        _clear_typed_draft(user.phone_number)
        return await handle_checkout(session, user)

    if user.current_state == SessionState.AWAITING_RECEIPT and msg.type == "image" and msg.media_id:
        return await handle_receipt_upload(session, user, msg.media_id)

    if open_fulfillment is not None:
        return await handle_checkout_flow(session, user, open_fulfillment, msg)

    # Free-text typed order auto-detection directly from anywhere during browsing / cart building
    if not rid and raw_text and session is not None:
        catalog = await menu_service.get_all_items(session)
        areas = list((await session.execute(select(DeliveryArea).where(DeliveryArea.is_active.is_(True)))).scalars())
        draft = order_parser.parse_typed_order(raw_text, catalog, areas=areas)
        if draft.has_any_recognition:
            return await start_typed_order_draft(session, user, draft)
        if user.phone_number in _TYPED_ORDER_PROMPTED:
            _TYPED_ORDER_PROMPTED.discard(user.phone_number)
            await whatsapp.send_text(
                user.phone_number,
                (
                    "💡 I couldn't match any menu items in that message.\n"
                    "Try typing like: *2 jollof rice, 1 chicken medium, 1 coke* — pick from the menu below, or reply *staff* to chat with us:"
                ),
            )
            return await handle_menu_request(session, user, 0)

    await whatsapp.send_text(
        user.phone_number,
        "Hi! Send *menu* to pick an item, or just type what you'd like (e.g. *2 jollof rice, 1 coke*). Reply *HELP* for staff.",
    )


async def handle_go_back_browsing(session: AsyncSession, user: User) -> None:
    """Handle `⬅ Go back` (`nav:back` or `back`) during browsing, typed orders, and cart building."""
    if user.phone_number in _CUSTOM_QTY_ITEM:
        entry = _CUSTOM_QTY_ITEM.pop(user.phone_number)
        item_id, qty_mode = entry if isinstance(entry, tuple) else (entry, "add")
        return await show_quantity_selection(session, user, item_id, mode=qty_mode)

    draft = _get_typed_draft(user.phone_number)
    if draft is not None:
        if draft.awaiting_cart_mode:
            _clear_typed_draft(user.phone_number)
            return await handle_menu_request(session, user, 0)
        if draft.followup_index > 0:
            draft.followup_index -= 1
            q = draft.followups[draft.followup_index]
            order_parser.undo_followup_choice(draft, q)
            _set_typed_draft(user.phone_number, draft)
            return await _advance_typed_order(session, user, draft)
        _clear_typed_draft(user.phone_number)
        return await prompt_typed_order(user)

    if user.phone_number in _TYPED_ORDER_PROMPTED:
        _TYPED_ORDER_PROMPTED.discard(user.phone_number)
        return await handle_menu_request(session, user, 0)

    prev = _pop_nav(user.phone_number)
    if prev:
        kind = prev.get("kind")
        if kind == "category":
            return await handle_category_selected(session, user, prev["category"], prev.get("page", 0))
        if kind == "quantity":
            return await show_quantity_selection(session, user, prev["item_id"], mode=prev.get("mode", "set"))
        if kind == "hub":
            item = await menu_service.get_item(session, prev["item_id"]) if prev.get("item_id") else None
            return await send_addon_hub(session, user, last_item=item, added_qty=prev.get("qty", 1))
        if kind == "carts":
            return await show_carts(session, user)
        if kind == "menu":
            return await handle_menu_request(session, user, prev.get("page", 0))

    return await handle_menu_request(session, user, 0)


async def get_in_progress_fulfillment(session: AsyncSession, user_id: int) -> Fulfillment | None:
    result = await session.execute(
        select(Fulfillment)
        .join(PaymentBatch, PaymentBatch.id == Fulfillment.payment_batch_id)
        .where(PaymentBatch.user_id == user_id, Fulfillment.stage.in_(IN_PROGRESS_STAGES))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def handle_not_received(session: AsyncSession, user: User, batch_id: int) -> None:
    """The customer tapped "Didn't receive it?" on the delivered message."""
    batch = await order_service.get_batch(session, batch_id)
    fulfillment = batch.fulfillment if batch else None
    if batch is None or batch.user_id != user.id or fulfillment is None or fulfillment.stage != FulfillmentStage.DELIVERED:
        await whatsapp.send_text(user.phone_number, "Sorry, we couldn't find that delivery. Please call " + settings.support_phone + ".")
        return
    if fulfillment.not_received_at is None:
        fulfillment.not_received_at = datetime.now(timezone.utc)
        fulfillment.not_received_acknowledged_at = None
    await whatsapp.send_text(user.phone_number, M19_NOT_RECEIVED)


def items_summary(batch: PaymentBatch) -> str:
    """"2 x Jollof rice, 1 x Chapman": for the customer's own failed-delivery message only (never sent to riders)."""
    parts = [f"{item.quantity} x {item.item_name}" for order in batch.orders for item in order.items]
    return ", ".join(parts) or "your order"


async def fail_fulfillment(
    session: AsyncSession, batch: PaymentBatch, fulfillment: Fulfillment, *,
    failed_by: str, internal_reasons: str, customer_reasons: str, note: str | None = None,
) -> None:
    """Close a delivery as failed and tell the customer. Used by both riders and staff.

    The batch stays PAID on purpose: the customer did pay, and staff need that record to handle a refund.
    """
    fulfillment.stage = FulfillmentStage.FAILED
    fulfillment.failed_at = datetime.now(timezone.utc)
    fulfillment.failure_reasons = internal_reasons
    fulfillment.failure_note = note
    fulfillment.failed_by = failed_by
    fulfillment.failed_acknowledged_at = None
    customer = batch.orders[0].user if batch.orders else None
    if customer is not None:
        await whatsapp.send_text(customer.phone_number, M20_FAILED.format(
            batch=batch.id, items=items_summary(batch), reasons=customer_reasons,
            refund_timeframe=settings.refund_timeframe, support_phone=settings.support_phone,
        ))


async def _rider_batch(
    session: AsyncSession, rider: Rider, batch_id: int
) -> tuple[PaymentBatch | None, Fulfillment | None]:
    """Load a batch (with orders, items and customer) and make sure it is assigned to this rider."""
    batch = await order_service.get_batch(session, batch_id)
    fulfillment = batch.fulfillment if batch else None
    if batch is None or fulfillment is None or fulfillment.rider_id != rider.id:
        await whatsapp.send_text(rider.phone_number, f"I couldn't find batch #{batch_id} assigned to you.")
        return None, None
    return batch, fulfillment


def _deliver_buttons(batch_id: int) -> list[tuple[str, str]]:
    return [(f"r:deliv:{batch_id}", "Delivered"), (f"r:fail:{batch_id}", "Can't deliver")]


async def rider_mark_picked(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    batch, fulfillment = await _rider_batch(session, rider, batch_id)
    if fulfillment is None:
        return
    if fulfillment.stage != FulfillmentStage.DISPATCHED:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not waiting to be picked up right now.")
        return
    fulfillment.stage = FulfillmentStage.OUT_FOR_DELIVERY
    fulfillment.picked_up_at = datetime.now(timezone.utc)
    fulfillment.rider_reminder_sent_at = None
    customer = batch.orders[0].user if batch.orders else None
    if customer is not None:
        await whatsapp.send_text(customer.phone_number, M17_ON_WAY.format(
            batch=batch.id, rider_name=rider.name, rider_phone=f"+{rider.phone_number}",
        ))
    await whatsapp.send_buttons(rider.phone_number, R2_PICKED_UP.format(batch=batch.id), _deliver_buttons(batch.id))


async def rider_ask_confirm_delivered(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    _, fulfillment = await _rider_batch(session, rider, batch_id)
    if fulfillment is None:
        return
    if fulfillment.stage != FulfillmentStage.OUT_FOR_DELIVERY:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not out for delivery right now.")
        return
    await whatsapp.send_buttons(
        rider.phone_number, R3_CONFIRM_DELIVERED.format(batch=batch_id),
        [(f"r:yes:{batch_id}", "Yes, delivered"), (f"r:no:{batch_id}", "Not yet")],
    )


async def rider_confirm_delivered(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    _, fulfillment = await _rider_batch(session, rider, batch_id)
    if fulfillment is None:
        return
    if fulfillment.stage != FulfillmentStage.OUT_FOR_DELIVERY:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not out for delivery right now.")
        return
    now = datetime.now(timezone.utc)
    fulfillment.stage = FulfillmentStage.DELIVERED
    fulfillment.delivered_at = now
    # The customer is thanked by the scheduler after this delay, so the rider can still undo a mistake.
    fulfillment.thank_you_due_at = now + timedelta(minutes=settings.thank_you_delay_minutes)
    fulfillment.thank_you_sent_at = None
    await whatsapp.send_buttons(
        rider.phone_number, R3_DELIVERED.format(minutes=settings.thank_you_delay_minutes),
        [(f"r:undo:{batch_id}", "Undo")],
    )


async def rider_undo(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    _, fulfillment = await _rider_batch(session, rider, batch_id)
    if fulfillment is None:
        return
    if fulfillment.stage != FulfillmentStage.DELIVERED:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} is not marked as delivered.")
        return
    if fulfillment.thank_you_sent_at is not None:
        await whatsapp.send_text(
            rider.phone_number, "It's too late to undo this: the customer has already been thanked. Please call the office."
        )
        return
    fulfillment.stage = FulfillmentStage.OUT_FOR_DELIVERY
    fulfillment.delivered_at = None
    fulfillment.thank_you_due_at = None
    await whatsapp.send_buttons(
        rider.phone_number, f"Done. Batch #{batch_id} is back on your route.", _deliver_buttons(batch_id)
    )


async def rider_begin_fail(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    _, fulfillment = await _rider_batch(session, rider, batch_id)
    if fulfillment is None:
        return
    if fulfillment.stage not in OPEN_DELIVERY_STAGES:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} can't be marked as failed right now.")
        return
    rider.pending_action = json.dumps({"action": "fail_reasons", "batch_id": batch_id})
    await whatsapp.send_text(rider.phone_number, FAIL_PROMPT)


async def rider_submit_fail_reasons(session: AsyncSession, rider: Rider, text: str, payload: dict) -> None:
    """The rider answered the numbered list, e.g. "1,3" or "5 gate was locked"."""
    batch_id = int(payload.get("batch_id", 0))
    match = re.match(r"^\s*([1-5](?:\s*,\s*[1-5])*)\s*(.*)$", text, re.S)
    if not match:
        await whatsapp.send_text(rider.phone_number, FAIL_PROMPT)
        return
    codes = list(dict.fromkeys(re.findall(r"[1-5]", match.group(1))))
    note = match.group(2).strip()[:200] or None
    labels = ", ".join(FAIL_REASONS[c][0] for c in codes)
    rider.pending_action = json.dumps({
        "action": "confirm_fail", "batch_id": batch_id, "codes": codes, "note": note,
    })
    await whatsapp.send_buttons(
        rider.phone_number,
        f"Mark batch #{batch_id} as failed?\nReason: {labels}" + (f" ({note})" if note else ""),
        [(f"r:failyes:{batch_id}", "Yes, mark failed"), (f"r:failno:{batch_id}", "Cancel")],
    )


async def rider_confirm_fail(session: AsyncSession, rider: Rider, batch_id: int) -> None:
    try:
        payload = json.loads(rider.pending_action or "{}")
    except ValueError:
        payload = {}
    if payload.get("action") != "confirm_fail" or payload.get("batch_id") != batch_id:
        await whatsapp.send_text(rider.phone_number, f"Nothing to confirm. Send `fail {batch_id}` to start again.")
        return
    batch, fulfillment = await _rider_batch(session, rider, batch_id)
    rider.pending_action = None
    if fulfillment is None:
        return
    if fulfillment.stage not in OPEN_DELIVERY_STAGES:
        await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} can't be marked as failed right now.")
        return
    codes = payload.get("codes", [])
    note = payload.get("note")
    customer_phrases = [FAIL_REASONS[c][1] for c in codes if FAIL_REASONS[c][1]]
    if "5" in codes:
        customer_phrases.append(note or "an unexpected problem came up")
    await fail_fulfillment(
        session, batch, fulfillment, failed_by="rider",
        internal_reasons=", ".join(FAIL_REASONS[c][0] for c in codes),
        customer_reasons="; ".join(customer_phrases) or "we could not complete the delivery", note=note,
    )
    await whatsapp.send_text(rider.phone_number, f"Batch #{batch_id} marked as failed. Thanks for the update.")


async def handle_rider_message(session: AsyncSession, rider: Rider, msg: IncomingMessage) -> None:
    """Riders talk to the bot with buttons (preferred) or typed commands. They never see customer messages."""
    text = (msg.text or "").strip()
    command = text.lower()
    rid = msg.reply_id or ""

    # ---- button taps: ids look like "r:picked:12" ----
    if rid.startswith("r:"):
        parts = rid.split(":")
        action = parts[1] if len(parts) > 1 else ""
        batch_id = int(parts[2]) if len(parts) > 2 and parts[2].isdecimal() else 0
        if action == "picked" and batch_id:
            return await rider_mark_picked(session, rider, batch_id)
        if action == "deliv" and batch_id:
            return await rider_ask_confirm_delivered(session, rider, batch_id)
        if action == "yes" and batch_id:
            return await rider_confirm_delivered(session, rider, batch_id)
        if action == "no":
            await whatsapp.send_text(rider.phone_number, "No problem. Tap Delivered once you've handed it over.")
            return
        if action == "fail" and batch_id:
            return await rider_begin_fail(session, rider, batch_id)
        if action == "failyes" and batch_id:
            return await rider_confirm_fail(session, rider, batch_id)
        if action == "failno":
            rider.pending_action = None
            await whatsapp.send_text(rider.phone_number, "Okay, nothing was changed.")
            return
        if action == "undo" and batch_id:
            return await rider_undo(session, rider, batch_id)
        await whatsapp.send_text(rider.phone_number, R5_HELP)
        return

    # ---- simple commands ----
    if command in {"active", "activate", "on"}:
        rider.is_active = True
        await whatsapp.send_text(rider.phone_number, R_ACTIVE)
        return
    if command in {"inactive", "deactivate", "off"}:
        rider.is_active = False
        await whatsapp.send_text(rider.phone_number, R_INACTIVE)
        return
    if command in {"help", "?", "commands"}:
        await whatsapp.send_text(rider.phone_number, R5_HELP)
        return
    if command == "pending":
        result = await session.execute(
            select(Fulfillment)
            .where(Fulfillment.rider_id == rider.id, Fulfillment.stage.in_(OPEN_DELIVERY_STAGES))
            .order_by(Fulfillment.id.desc())
        )
        items = result.scalars().all()
        if not items:
            await whatsapp.send_text(rider.phone_number, "You have no active deliveries right now.")
            return
        await whatsapp.send_text(
            rider.phone_number,
            "Your deliveries:\n" + "\n".join(f"• Batch #{f.payment_batch_id}: {f.stage.value}" for f in items),
        )
        return

    typed = re.match(r"^(picked|delivered|fail|undo)\s+#?(\d+)$", command)
    if typed:
        verb, batch_id = typed.group(1), int(typed.group(2))
        if verb == "picked":
            return await rider_mark_picked(session, rider, batch_id)
        if verb == "delivered":
            return await rider_ask_confirm_delivered(session, rider, batch_id)
        if verb == "fail":
            return await rider_begin_fail(session, rider, batch_id)
        return await rider_undo(session, rider, batch_id)

    # ---- the rider is answering the "failure reasons" question ----
    if rider.pending_action and text:
        try:
            payload = json.loads(rider.pending_action)
        except ValueError:
            payload = {}
        if payload.get("action") == "fail_reasons":
            return await rider_submit_fail_reasons(session, rider, text, payload)

    await whatsapp.send_text(rider.phone_number, R5_HELP)


async def handle_menu_request(session: AsyncSession | None, user: User, page: int = 0) -> None:
    user.current_state = SessionState.BROWSING
    categories = await menu_service.get_categories(session)
    if not categories:
        await whatsapp.send_text(user.phone_number, "Sorry, nothing is available right now. Please check back soon.")
        return

    # Unit-test fallback when called with `session=None` (e.g. tests/test_menu_pagination.py)
    if session is None:
        total_pages = (len(categories) + CATEGORY_PAGE_SIZE - 1) // CATEGORY_PAGE_SIZE
        page = min(max(page, 0), total_pages - 1)
        start = page * CATEGORY_PAGE_SIZE
        rows = [{"id": f"cat:{c}", "title": c[:24]} for c in categories[start:start + CATEGORY_PAGE_SIZE]]
        if start + CATEGORY_PAGE_SIZE < len(categories):
            rows.append({"id": f"menupg:{page + 1}", "title": "More categories"})
        await whatsapp.send_list(user.phone_number, "What would you like to order?", "View menu", "Categories", rows)
        return

    _push_nav(user.phone_number, {"kind": "menu", "page": page})
    total_qty, subtotal, pack_fee = await order_service.get_user_cart_totals(session, user.id)
    last_order = await order_service.get_last_order(session, user.id) if total_qty == 0 else None
    saved, saved_area = await _active_saved_address(session, user.id)
    has_saved = saved is not None and saved_area is not None
    _journey_update(
        user.phone_number,
        mode="browse",
        has_saved=has_saved,
        preselected_area=user.phone_number in _PRESELECTED_AREA,
        fulfillment=None,
    )

    page0_cats = [c for c in menu_service.STEP1_PRIMARY_CATEGORIES if c in categories]
    if not page0_cats:
        page0_cats = categories[:3]
    more_cats = [c for c in categories if c not in page0_cats]

    if page > 0 and more_cats:
        per_page = _effective_menu_page_size()
        extra_pages = max(1, (len(more_cats) + per_page - 1) // per_page)
        extra_page = min(page - 1, extra_pages - 1)
        start = extra_page * per_page
        slice_cats = more_cats[start:start + per_page]
        rows: list[dict] = [
            {
                "id": f"cat:{c}",
                "title": menu_service.friendly_category_name(c),
                "aliases": [c],
            }
            for c in slice_cats
        ]
        if start + per_page < len(more_cats):
            rows.append({"id": f"menupg:{extra_page + 2}", "title": "More categories"})
        if extra_page > 0:
            rows.append({"id": f"menupg:{extra_page}", "title": "⬅ Previous categories"})
        else:
            rows.append({"id": "menu", "title": "⬅ Main menu"})
        rows.append({"id": "nav:back", "title": "⬅ Go back"})
        cart_line = await _cart_status_line(session, user.id)
        body = _format_poll_header(
            f"{_step_label(user.phone_number, 'welcome')} · More Categories ({extra_page + 1}/{extra_pages})",
            cart_line if total_qty > 0 else "Pick a category below:",
        )
        await whatsapp.send_list(user.phone_number, body, "More options", "Categories", rows)
        return

    catalog = await menu_service.get_all_items(session)
    heroes = menu_service.resolve_hero_options(catalog)

    rows = []
    if total_qty > 0:
        rows.append({
            "id": "checkout",
            "title": "✅ Checkout Now",
            "description": f"{total_qty} item(s), {naira(subtotal + pack_fee)}",
        })
    elif last_order is not None:
        last_items = ", ".join(
            f"{oi.quantity}x {menu_service.friendly_item_name(oi.item_name)}" for oi in last_order.items[:2]
        )
        if len(last_order.items) > 2:
            last_items += f" +{len(last_order.items) - 2} more"
        rows.append({
            "id": "repeat:last",
            "title": "⚡ Repeat Last Order",
            "description": f"{last_items} · {naira(last_order.total_amount)}"[:72],
        })

    for hero in heroes:
        rows.append({
            "id": hero["id"],
            "title": hero["title"],
            "description": hero["description"],
        })

    for c in page0_cats:
        rows.append({
            "id": f"cat:{c}",
            "title": menu_service.friendly_category_name(c),
            "aliases": [c],
        })

    if more_cats:
        more_preview = ", ".join(menu_service.friendly_category_name(c).split(" ", 1)[-1] for c in more_cats[:3])
        if len(more_cats) > 3:
            more_preview += "…"
        rows.append({
            "id": "menupg:1",
            "title": "📂 More Categories",
            "description": more_preview[:72],
        })

    rows.append({
        "id": "staff:escalate",
        "title": "💬 Talk to staff",
        "description": "Pause bot & chat with our team",
    })

    step_lbl = _step_label(user.phone_number, "welcome")
    if total_qty > 0:
        cart_total = subtotal + pack_fee
        title_line = f"{step_lbl} · 🛒 Cart {naira(cart_total)}"
        ctx = "Tap a combo, pick a category, or type your order:"
    elif last_order is not None:
        title_line = f"{step_lbl} · 👋 Welcome back to {settings.brand_name}!"
        ctx = "Tap Repeat, pick a combo/category, or type your order:"
    else:
        title_line = f"{step_lbl} · 👋 {settings.brand_name}"
        ctx = "Tap a combo, pick a category, or just type your order (e.g. 2 jollof, 1 coke):"

    body = _format_poll_header(title_line, ctx)
    await whatsapp.send_list(user.phone_number, body, "View menu", "Quick Menu", rows)


async def handle_repeat_last_order(session: AsyncSession, user: User) -> None:
    """Fast path for repeat customers (Principle 9): load last order and jump to Summary when saved address exists."""
    last_order = await order_service.get_last_order(session, user.id)
    if last_order is None or not last_order.items:
        await whatsapp.send_text(user.phone_number, "We couldn't find a previous order to repeat. Let's start a new one!")
        return await handle_menu_request(session, user, 0)

    cart = await order_service.get_or_create_cart(session, user.id)
    await order_service.clear_cart(session, cart.id)

    added_names: list[str] = []
    skipped_names: list[str] = []

    for oi in last_order.items:
        item = await menu_service.get_item(session, oi.item_id)
        if item is None or not item.is_available:
            skipped_names.append(menu_service.friendly_item_name(oi.item_name))
            continue
        limit = item.sellable_units
        if limit is not None and limit <= 0:
            skipped_names.append(menu_service.friendly_item_name(item.name))
            continue
        qty = oi.quantity if limit is None else min(oi.quantity, limit)
        await order_service.add_to_cart(session, user.id, item.id, quantity=qty, cart_id=cart.id)
        added_names.append(f"{qty} x {menu_service.friendly_item_name(item.name)}")

    if not added_names:
        await whatsapp.send_text(
            user.phone_number,
            "⚠️ Sorry, the items from your last order are currently sold out. Please pick something fresh from our menu:",
        )
        return await handle_menu_request(session, user, 0)

    if skipped_names:
        await whatsapp.send_text(
            user.phone_number,
            f"⚠️ Note: *{', '.join(skipped_names)}* from your last order is currently unavailable, "
            f"so we added the available items ({', '.join(added_names)}).",
        )

    saved, saved_area = await _active_saved_address(session, user.id)
    if saved is not None and saved_area is not None:
        _journey_update(
            user.phone_number,
            mode="repeat",
            has_saved=True,
            preselected_area=False,
            fulfillment="delivery",
        )
        return await handle_checkout(session, user)

    lines = await order_service.get_cart(session, cart.id)
    total_qty = sum(c.quantity for c, _ in lines)
    _journey_update(
        user.phone_number,
        mode="quick",
        has_saved=False,
        preselected_area=user.phone_number in _PRESELECTED_AREA,
        fulfillment=None,
    )
    user.current_state = SessionState.ADDING_TO_CART
    _push_nav(user.phone_number, {"kind": "carts"})
    await send_addon_hub(
        session,
        user,
        last_item=None,
        added_qty=total_qty,
        hub_title=f"⚡ Repeat order loaded ({', '.join(added_names)})",
    )


# ============================================================================
# TYPED ORDER FLOW (Rule-Based + RapidFuzz Free-Text Order Parsing)
# ============================================================================


async def prompt_typed_order(user: User) -> None:
    """Explicit handoff to free text when customer requests `typeorder:retype` / `typeorder:start`."""
    _TYPED_ORDER_DRAFTS.pop(user.phone_number, None)
    _TYPED_ORDER_PROMPTED.add(user.phone_number)
    user.current_state = SessionState.ADDING_TO_CART
    step_lbl = _step_label(user.phone_number, "typed_prompt")
    await whatsapp.send_text(
        user.phone_number,
        (
            f"✍️ *TYPE YOUR ORDER BELOW ({step_lbl})*\n"
            "Send all the items you want in one message.\n\n"
            "💡 *Examples:*\n"
            "• _2 jollof rice, 1 fried rice, 2 chicken medium, 1 plantain, 2 coke_\n"
            "• _jollof 2 fried 2 turkey 1 plantain 800_\n"
            "• _2 jollof rice, 1 coke to Akute_\n\n"
            "_(Or reply *BACK* to pick from the menu, or *CANCEL* to cancel)_"
        ),
    )


async def _send_fast_typed_cart_summary(
    session: AsyncSession,
    user: User,
    *,
    area_name: str | None = None,
    had_prior_items: bool = False,
    notes_summary: str = "",
) -> None:
    """Send itemized Cart Summary Card + single Action Poll immediately for high-confidence typed orders."""
    cart = await order_service.get_or_create_cart(session, user.id)
    lines = await order_service.get_cart(session, cart.id)
    total_qty, items_sub, pack_fee = await order_service.get_user_cart_totals(session, user.id)
    total = items_sub + pack_fee

    item_bullets = "\n".join(
        f"• {c.quantity} x {menu_service.friendly_item_name(m.name)} — {naira(m.price * c.quantity)}"
        for c, m in lines
    )
    sections: list[str] = [
        f"📝 *ADDED TO YOUR CART ({_step_label(user.phone_number, 'carts')})*",
        item_bullets or "• _(Cart empty)_",
    ]
    if area_name:
        sections.append(f"📍 *Delivery area matched:* {area_name}")
    if notes_summary:
        sections.append(f"📝 *Kitchen note:* {notes_summary}")

    pack_line = _pack_summary_line(pack_fee)
    breakdown = f"• Items subtotal: {naira(items_sub)}\n{pack_line}━━━━━━━━━━━━━━━━━━\n*Cart Total:* {naira(total)}"
    sections.append(breakdown)
    await whatsapp.send_text(user.phone_number, "\n\n".join(sections))

    buttons: list[tuple[str, str]] = [
        ("checkout", f"✅ Confirm & Checkout ({naira(total)})"),
    ]
    if had_prior_items:
        buttons.append(("typeorder:replace_prev", "🔄 Replace previous cart instead"))
    buttons.extend([
        ("carts", "✏️ Edit Cart"),
        ("menu", "➕ Add More Items"),
        ("nav:back", "⬅ Go back"),
    ])
    header = _format_poll_header(
        f"{_step_label(user.phone_number, 'carts')} · 🛒 Cart {naira(total)}",
        f"Got {total_qty} item(s). Ready to checkout?",
    )
    await whatsapp.send_buttons(user.phone_number, header, buttons)


async def start_typed_order_draft(
    session: AsyncSession, user: User, draft: order_parser.ParsedOrderDraft
) -> None:
    """Fast-forward unambiguous typed orders directly into cart; ask <=2 checks only when ambiguous."""
    _TYPED_ORDER_PROMPTED.discard(user.phone_number)
    user.current_state = SessionState.ADDING_TO_CART
    saved, saved_area = await _active_saved_address(session, user.id)
    has_saved = saved is not None and saved_area is not None

    if draft.is_high_confidence:
        _clear_typed_draft(user.phone_number)
        cart = await order_service.get_or_create_cart(session, user.id)
        existing_lines = await order_service.get_cart(session, cart.id)
        had_prior_items = bool(existing_lines)
        in_cart_map = {m.id: c.quantity for c, m in existing_lines}

        typed_snapshot: list[tuple[int, int, str | None]] = []
        for m in draft.matched:
            item = await menu_service.get_item(session, m.item_id)
            if item is None or not item.is_available:
                continue
            limit = item.sellable_units
            already = in_cart_map.get(item.id, 0)
            qty_to_add = m.quantity if limit is None else max(0, min(m.quantity, limit - already))
            if qty_to_add <= 0:
                continue
            await order_service.add_to_cart(session, user.id, item.id, quantity=qty_to_add, cart_id=cart.id)
            in_cart_map[item.id] = already + qty_to_add
            typed_snapshot.append((item.id, qty_to_add, ", ".join(m.notes) if m.notes else None))

        if not typed_snapshot:
            return await prompt_typed_order(user)

        _LAST_TYPED_ITEMS[user.phone_number] = typed_snapshot
        notes_summary = draft.kitchen_notes_summary()
        if notes_summary:
            prev_notes = _KITCHEN_NOTES.get(user.phone_number)
            _KITCHEN_NOTES[user.phone_number] = f"{prev_notes}; {notes_summary}" if prev_notes else notes_summary

        if draft.matched_area_id and draft.matched_area_name:
            _PRESELECTED_AREA[user.phone_number] = (draft.matched_area_id, draft.matched_area_name)

        _journey_update(
            user.phone_number,
            mode="quick",
            has_saved=has_saved,
            preselected_area=bool(draft.matched_area_id),
            fulfillment=None,
        )
        _push_nav(user.phone_number, {"kind": "carts"})
        await _send_fast_typed_cart_summary(
            session,
            user,
            area_name=draft.matched_area_name,
            had_prior_items=had_prior_items,
            notes_summary=_KITCHEN_NOTES.get(user.phone_number, ""),
        )
        return

    _journey_update(
        user.phone_number,
        mode="quick",
        has_saved=has_saved,
        preselected_area=bool(draft.matched_area_id),
        fulfillment=None,
    )
    total_qty, subtotal, pack_fee = await order_service.get_user_cart_totals(session, user.id)
    if total_qty > 0:
        draft.awaiting_cart_mode = True
        _set_typed_draft(user.phone_number, draft)
        await whatsapp.send_buttons(
            user.phone_number,
            _format_poll_header(
                f"{_step_label(user.phone_number, 'typed_followup')} · Cart {naira(subtotal + pack_fee)}",
                f"You already have {total_qty} item(s). How should we handle your typed order?",
            ),
            [
                ("typeorder:mode:add", f"➕ Add to my cart ({total_qty} item{'s' if total_qty != 1 else ''})"),
                ("typeorder:mode:replace", "🔄 Replace my cart with this order"),
                ("nav:back", "⬅ Go back"),
            ],
        )
        return

    draft.awaiting_cart_mode = False
    draft.cart_mode = "add"
    _set_typed_draft(user.phone_number, draft)
    await _advance_typed_order(session, user, draft)


async def handle_typed_order_replace_prev(session: AsyncSession, user: User) -> None:
    """Replace the previous cart items with only the most recently fast-forwarded typed order."""
    snap = _LAST_TYPED_ITEMS.get(user.phone_number)
    if not snap:
        return await handle_menu_request(session, user, 0)
    cart = await order_service.get_or_create_cart(session, user.id)
    await order_service.clear_cart(session, cart.id)
    note_parts: list[str] = []
    for item_id, qty, item_notes in snap:
        item = await menu_service.get_item(session, item_id)
        if item is not None and item.is_available:
            await order_service.add_to_cart(session, user.id, item.id, quantity=qty, cart_id=cart.id)
            if item_notes:
                note_parts.append(f"{menu_service.friendly_item_name(item.name)} ({item_notes})")
    notes_summary = "; ".join(note_parts)
    if notes_summary:
        _KITCHEN_NOTES[user.phone_number] = notes_summary
    else:
        _KITCHEN_NOTES.pop(user.phone_number, None)
    pre_area = _PRESELECTED_AREA.get(user.phone_number)
    await _send_fast_typed_cart_summary(
        session,
        user,
        area_name=pre_area[1] if pre_area else None,
        had_prior_items=False,
        notes_summary=notes_summary,
    )


async def handle_typed_order_mode(session: AsyncSession, user: User, mode: str) -> None:
    """Handle `➕ Add to my cart` or `🔄 Replace my cart` poll answer."""
    draft = _get_typed_draft(user.phone_number)
    if draft is None:
        return await prompt_typed_order(user)
    draft.cart_mode = "replace" if mode == "replace" else "add"
    draft.awaiting_cart_mode = False
    _set_typed_draft(user.phone_number, draft)
    await _advance_typed_order(session, user, draft)


async def _advance_typed_order(
    session: AsyncSession, user: User, draft: order_parser.ParsedOrderDraft
) -> None:
    """Ask the next follow-up question (up to 2 max), or show the confirmation summary & poll."""
    if draft.followup_index < len(draft.followups):
        idx = draft.followup_index
        total_q = len(draft.followups)
        q = draft.followups[idx]
        buttons: list[tuple[str, str]] = []
        step_lbl = _step_label(user.phone_number, "typed_followup")

        if q.kind == "size":
            body = _format_poll_header(
                f"{step_lbl} · Quick check {idx + 1} of {total_q}",
                f"Which size of *{q.family_label}* would you like for *\"{q.raw_phrase}\"*?",
            )
            for item_id, display_name, unit_price in q.options:
                opt_qty = q.quantity
                label = (
                    f"{opt_qty}x {display_name} ({naira(unit_price * opt_qty)})"
                    if opt_qty > 1
                    else f"{display_name} ({naira(unit_price)})"
                )
                buttons.append((f"typeorder:pick:{item_id}:{opt_qty}", label))
        elif q.kind == "naira_mismatch":
            body = _format_poll_header(
                f"{step_lbl} · Quick check {idx + 1} of {total_q}",
                f"*{q.family_label}* doesn't divide into {naira(Decimal(q.naira_amount or 0))}. Pick one:",
            )
            for opt_i, (item_id, display_name, opt_total) in enumerate(q.options):
                opt_qty = q.quantity + opt_i
                buttons.append((f"typeorder:pick:{item_id}:{opt_qty}", f"{display_name} ({naira(opt_total)})"))
        else:
            body = _format_poll_header(
                f"{step_lbl} · Quick check {idx + 1} of {total_q}",
                f"We couldn't find *\"{q.raw_phrase}\"*. Did you mean:",
            )
            for item_id, display_name, unit_price in q.options:
                opt_qty = q.quantity
                buttons.append(
                    (f"typeorder:pick:{item_id}:{opt_qty}", f"✅ Yes, {opt_qty}x {display_name} ({naira(unit_price * opt_qty)})")
                )

        buttons.append(("typeorder:skip", f"❌ Remove \"{q.raw_phrase[:20]}\""))
        buttons.append(("nav:back", "⬅ Go back"))
        await whatsapp.send_buttons(user.phone_number, body, buttons)
        return

    await _send_typed_order_confirmation(session, user, draft)


async def handle_typed_order_pick(session: AsyncSession, user: User, payload: str) -> None:
    """Resolve the current follow-up question with the customer's selected item and quantity."""
    draft = _get_typed_draft(user.phone_number)
    if draft is None:
        return await prompt_typed_order(user)
    parts = payload.split(":")
    if len(parts) != 2 or not all(p.isdecimal() for p in parts):
        return await _advance_typed_order(session, user, draft)
    item_id, qty = int(parts[0]), int(parts[1])
    item = await menu_service.get_item(session, item_id)
    if draft.followup_index < len(draft.followups):
        q = draft.followups[draft.followup_index]
        if item is not None:
            order_parser.resolve_followup_choice(draft, q, item, qty)
        draft.followup_index += 1
        _set_typed_draft(user.phone_number, draft)
    await _advance_typed_order(session, user, draft)


async def handle_typed_order_skip(session: AsyncSession, user: User) -> None:
    """Skip/remove the ambiguous item in the current follow-up question."""
    draft = _get_typed_draft(user.phone_number)
    if draft is None:
        return await prompt_typed_order(user)
    if draft.followup_index < len(draft.followups):
        q = draft.followups[draft.followup_index]
        q.resolved_item_id = None
        q.resolved_qty = 0
        draft.followup_index += 1
        _set_typed_draft(user.phone_number, draft)
    await _advance_typed_order(session, user, draft)


async def _send_typed_order_confirmation(
    session: AsyncSession, user: User, draft: order_parser.ParsedOrderDraft
) -> None:
    """Send the itemized plain-text breakdown of an ambiguous/partial typed order followed by action poll."""
    step_lbl = _step_label(user.phone_number, "carts")
    sections: list[str] = [f"📝 *WE UNDERSTOOD YOUR ORDER ({step_lbl})*"]

    if draft.matched:
        item_lines: list[str] = []
        for m in draft.matched:
            line = f"• {m.quantity} x {m.display_name} — {naira(m.subtotal)}"
            if m.stock_capped_from is not None:
                line += f" _(⚠️ Stock adjusted: only {m.quantity} left, from {m.stock_capped_from})_"
            item_lines.append(line)
        sections.append("\n".join(item_lines))
    else:
        sections.append("• _(No available items matched yet)_")

    if draft.sold_out:
        so_lines: list[str] = []
        for so in draft.sold_out:
            if so.alternatives:
                alt_str = ", ".join(f"{a_name} ({naira(a_price)})" for _, a_name, a_price in so.alternatives)
                so_lines.append(f"⛔ *Sold out:* {so.display_name} _(Try: {alt_str})_")
            else:
                so_lines.append(f"⛔ *Sold out:* {so.display_name}")
        sections.append("\n".join(so_lines))

    if draft.please_specify:
        spec_lines = "\n".join(f"• {hint}" for hint in draft.please_specify)
        sections.append(f"⚠️ *Please specify size/type:*\n{spec_lines}")

    if draft.unmatched:
        unmatched_str = ", ".join(f'"{u}"' for u in draft.unmatched)
        sections.append(f"⚠️ *Couldn't find on our menu:* {unmatched_str}")

    if draft.matched_area_name:
        sections.append(f"📍 *Delivery area matched:* {draft.matched_area_name}")

    notes_summary = draft.kitchen_notes_summary()
    if notes_summary:
        sections.append(f"📝 *Kitchen note:* {notes_summary}")

    sections.append(f"━━━━━━━━━━━━━━━━━━\n*Items Subtotal:* {naira(draft.subtotal)}")
    await whatsapp.send_text(user.phone_number, "\n\n".join(sections))

    buttons: list[tuple[str, str]] = []
    if draft.matched:
        buttons.append(("typeorder:confirm", f"✅ Add all to cart ({naira(draft.subtotal)})"))

    seen_alt_ids: set[int] = set()
    for so in draft.sold_out:
        for alt_id, alt_name, alt_price in so.alternatives:
            if alt_id not in seen_alt_ids and len(seen_alt_ids) < 2:
                seen_alt_ids.add(alt_id)
                buttons.append((f"typeorder:alt:{alt_id}", f"➕ Add {alt_name} instead ({naira(alt_price)})"))

    buttons.append(("typeorder:retype", "✏️ Retype my order"))
    buttons.append(("menu", "🍛 Pick from menu instead"))
    buttons.append(("staff:escalate", "💬 Talk to staff"))
    buttons.append(("nav:back", "⬅ Go back"))

    await whatsapp.send_buttons(
        user.phone_number,
        _format_poll_header(
            f"{step_lbl} · Confirm your typed order",
            "Tap below to add these items to your cart or make changes:",
        ),
        buttons,
    )


async def handle_typed_order_alt(session: AsyncSession, user: User, alt_id_str: str) -> None:
    """Add an in-stock category alternative for a sold-out item and refresh the confirmation screen."""
    draft = _get_typed_draft(user.phone_number)
    if draft is None or not alt_id_str.isdecimal():
        return await prompt_typed_order(user)
    alt_id = int(alt_id_str)
    alt_item = await menu_service.get_item(session, alt_id)
    if alt_item is not None and alt_item.is_available:
        order_parser._add_or_merge_matched(draft, alt_item, 1, [])
        for so in list(draft.sold_out):
            if any(a_id == alt_id for a_id, _, _ in so.alternatives):
                draft.sold_out.remove(so)
                break
        _set_typed_draft(user.phone_number, draft)
    await _send_typed_order_confirmation(session, user, draft)


async def handle_typed_order_confirm(session: AsyncSession, user: User) -> None:
    """Commit confirmed items from a disambiguated typed order draft into cart and open Add-On Hub."""
    draft = _get_typed_draft(user.phone_number)
    if draft is None or not draft.matched:
        return await prompt_typed_order(user)

    _clear_typed_draft(user.phone_number)
    if draft.cart_mode == "replace":
        await order_service.clear_user_carts(session, user.id)
        _KITCHEN_NOTES.pop(user.phone_number, None)

    cart = await order_service.get_or_create_cart(session, user.id)
    existing_lines = await order_service.get_cart(session, cart.id)
    in_cart_map = {m.id: c.quantity for c, m in existing_lines}

    added_labels: list[str] = []
    added_qty = 0
    for m in draft.matched:
        item = await menu_service.get_item(session, m.item_id)
        if item is None or not item.is_available:
            continue
        limit = item.sellable_units
        already = in_cart_map.get(item.id, 0)
        qty_to_add = m.quantity if limit is None else max(0, min(m.quantity, limit - already))
        if qty_to_add <= 0:
            continue
        await order_service.add_to_cart(session, user.id, item.id, quantity=qty_to_add, cart_id=cart.id)
        in_cart_map[item.id] = already + qty_to_add
        added_qty += qty_to_add
        added_labels.append(f"{qty_to_add}x {m.display_name}")

    notes_summary = draft.kitchen_notes_summary()
    if notes_summary:
        prev_notes = _KITCHEN_NOTES.get(user.phone_number)
        _KITCHEN_NOTES[user.phone_number] = f"{prev_notes}; {notes_summary}" if prev_notes else notes_summary

    if draft.matched_area_id and draft.matched_area_name:
        _PRESELECTED_AREA[user.phone_number] = (draft.matched_area_id, draft.matched_area_name)

    user.current_state = SessionState.ADDING_TO_CART
    _push_nav(user.phone_number, {"kind": "carts"})

    added_preview = ", ".join(added_labels[:3])
    if len(added_labels) > 3:
        added_preview += f" +{len(added_labels) - 3} more"

    await send_addon_hub(
        session,
        user,
        last_item=None,
        added_qty=added_qty,
        hub_title=f"✅ Added: {added_preview or 'your items'}",
    )


async def handle_category_selected(
    session: AsyncSession, user: User, category: str, page: int = 0
) -> None:
    user.current_state = SessionState.ADDING_TO_CART
    items = await menu_service.get_available_items(session, category)
    if not items:
        await whatsapp.send_text(user.phone_number, f"Nothing in {menu_service.friendly_category_name(category)} is available right now.")
        return

    saved, saved_area = await _active_saved_address(session, user.id)
    _journey_update(
        user.phone_number,
        mode="browse",
        has_saved=saved is not None and saved_area is not None,
    )
    _push_nav(user.phone_number, {"kind": "category", "category": category, "page": page})
    page_size = _effective_menu_page_size()
    total_pages = (len(items) + page_size - 1) // page_size
    page = min(max(page, 0), total_pages - 1)
    start = page * page_size
    end = start + page_size

    rows: list[dict] = [
        {
            "id": f"add:{i.id}",
            "title": menu_service.friendly_item_name(i.name),
            "description": naira(i.price),
            "aliases": [i.name],
        }
        for i in items[start:end]
    ]

    # Single-poll pagination & nav back (0=cancel, HELP=staff via hidden global shortcuts)
    if page < total_pages - 1:
        rows.append({"id": f"catpg:{category}:{page + 1}", "title": f"➕ See more (Page {page + 2}/{total_pages})"})
    if page > 0:
        rows.append({"id": f"catpg:{category}:{page - 1}", "title": f"⬅ Previous (Page {page}/{total_pages})"})
    rows.append({"id": "nav:back", "title": "⬅ Go back"})

    friendly_cat = menu_service.friendly_category_name(category)
    cart_line = await _cart_status_line(session, user.id)
    step_lbl = _step_label(user.phone_number, "category")
    page_tag = f" · Page {page + 1}/{total_pages}" if total_pages > 1 else ""
    heading = _format_poll_header(
        f"{step_lbl} · {friendly_cat}{page_tag}",
        f"{cart_line} · Tap any item to add:",
    )
    await whatsapp.send_list(user.phone_number, heading, "Choose item", category, rows)


async def send_addon_hub(
    session: AsyncSession,
    user: User,
    *,
    last_item: MenuItem | None,
    added_qty: int = 1,
    hub_title: str | None = None,
) -> None:
    """Optimistic Add-On & Checkout Hub Poll (inline +1 / Change Qty + 4 direct 1-tap add-ons)."""
    cart = await order_service.get_or_create_cart(session, user.id)
    lines = await order_service.get_cart(session, cart.id)
    total_qty, items_sub, pack_fee = await order_service.get_user_cart_totals(session, user.id)
    cart_total = items_sub + pack_fee
    cart_item_ids = {m.id for _, m in lines}

    catalog = await menu_service.get_all_items(session)
    addons = menu_service.get_contextual_addons(catalog, last_item, cart_item_ids, limit=4)

    user.current_state = SessionState.ADDING_TO_CART
    _push_nav(
        user.phone_number,
        {"kind": "hub", "item_id": last_item.id if last_item else None, "qty": added_qty},
    )

    step_lbl = _step_label(user.phone_number, "hub")
    if hub_title:
        lead = hub_title
    elif last_item is not None:
        in_cart_qty = next((c.quantity for c, m in lines if m.id == last_item.id), added_qty)
        lead = f"✅ Added {added_qty}x {menu_service.friendly_item_name(last_item.name)} ({in_cart_qty} in cart)"
    else:
        lead = f"🛒 {total_qty} item(s) in cart"

    pack_note = f" (incl. {naira(pack_fee)} pack)" if pack_fee > 0 else ""
    title_line = f"{step_lbl} · 🛒 {naira(cart_total)}{pack_note}"
    body = _format_poll_header(title_line, f"{lead}. Add extras or checkout?")

    buttons: list[tuple[str, str]] = [
        ("checkout", f"💳 Checkout Now ({naira(cart_total)})"),
    ]
    if last_item is not None:
        short_name = _short_item_name(last_item.name, 16)
        buttons.append((f"qty_inc:{last_item.id}:1", f"➕ Add 1 more {short_name} (+{naira(last_item.price)})"))
        buttons.append((f"qty_menu:{last_item.id}", f"🔢 Change {short_name} Qty"))

    for addon, a_qty in addons:
        addon_name = _short_item_name(addon.name, 16)
        qty_tag = f" ×{a_qty}" if a_qty > 1 else ""
        buttons.append((f"quick_add:{addon.id}", f"➕ {addon_name}{qty_tag} ({naira(addon.price * a_qty)})"))

    buttons.append(("menu", "📂 Browse Full Menu"))
    buttons.append(("carts", "🛒 View / Edit Cart"))
    buttons.append(("nav:back", "⬅ Go back"))

    await whatsapp.send_buttons(user.phone_number, body, buttons)


async def handle_bundle_add(session: AsyncSession, user: User, bundle_id: str) -> None:
    """Resolve and optimistically add a Step 1 hero combo bundle (with live protein fallback)."""
    catalog = await menu_service.get_all_items(session)
    heroes = menu_service.resolve_hero_options(catalog)
    hero = next((h for h in heroes if h["id"] == bundle_id), None)
    if hero is None:
        await whatsapp.send_text(
            user.phone_number,
            "Sorry, part of that combo just ran out. Here is what's currently available:",
        )
        return await handle_menu_request(session, user, 0)

    cart = await order_service.get_or_create_cart(session, user.id)
    lines = await order_service.get_cart(session, cart.id)
    in_cart_map = {m.id: c.quantity for c, m in lines}

    for item, needed in hero["items"]:
        limit = item.sellable_units
        already = in_cart_map.get(item.id, 0)
        if limit is not None and already + needed > limit:
            await whatsapp.send_buttons(
                user.phone_number,
                f"⚠️ Only {limit} {menu_service.friendly_item_name(item.name)} available (you have {already} in cart).",
                [("menu", "📂 Browse Full Menu"), ("checkout", "✅ Checkout"), ("nav:back", "⬅ Go back")],
            )
            return

    added_total = 0
    for item, needed in hero["items"]:
        await order_service.add_to_cart(session, user.id, item.id, quantity=needed, cart_id=cart.id)
        added_total += needed

    saved, saved_area = await _active_saved_address(session, user.id)
    _journey_update(
        user.phone_number,
        mode="quick",
        has_saved=saved is not None and saved_area is not None,
    )
    await send_addon_hub(
        session,
        user,
        last_item=None,
        added_qty=added_total,
        hub_title=f"✅ Added {hero['title']}",
    )


async def handle_optimistic_add(session: AsyncSession, user: User, item_id: int) -> None:
    """Immediately insert default portion (2 for Plantain, 1 otherwise) and show Add-On Hub."""
    item = await menu_service.get_item(session, item_id)
    if item is None or not item.is_available:
        await whatsapp.send_text(user.phone_number, "Sorry, that item just ran out. Send *menu* to pick something else.")
        return
    qty = menu_service.default_tap_quantity(item)
    await handle_add_item(session, user, item_id, qty)


async def show_quantity_selection(session: AsyncSession, user: User, item_id: int, *, mode: str = "set") -> None:
    item: MenuItem | None = await menu_service.get_item(session, item_id)
    if item is None or not item.is_available:
        await whatsapp.send_text(
            user.phone_number,
            "Sorry, that item is no longer available. Send *menu* to pick something else.",
        )
        return
    available_quantity = item.sellable_units
    if mode != "set" and available_quantity is not None:
        in_cart = 0
        if user.active_cart_id is not None:
            cart = await session.get(Cart, user.active_cart_id)
            if cart is not None and cart.user_id == user.id:
                lines = await order_service.get_cart(session, cart.id)
                in_cart = next((c.quantity for c, m in lines if m.id == item_id), 0)
        available_quantity = max(0, available_quantity - in_cart)
    max_quantity = QUANTITY_PAGE_SIZE if available_quantity is None else min(
        QUANTITY_PAGE_SIZE, available_quantity
    )
    if max_quantity < 1:
        await whatsapp.send_text(user.phone_number, f"Sorry, {menu_service.friendly_item_name(item.name)} is sold out.")
        return

    _push_nav(user.phone_number, {"kind": "quantity", "item_id": item.id, "category": item.category, "mode": mode})

    direct_max = min(4, max_quantity)
    prefix = "qtyset" if mode == "set" else "qty"
    custom_prefix = "qtycustomset" if mode == "set" else "qtycustom"
    rows: list[dict] = [
        {
            "id": f"{prefix}:{item.id}:{quantity}",
            "title": f"{quantity} portion{'s' if quantity != 1 else ''}",
            "description": naira(item.price * quantity),
        }
        for quantity in range(1, direct_max + 1)
    ]
    if available_quantity is None or available_quantity >= 5:
        rows.append({
            "id": f"{custom_prefix}:{item.id}",
            "title": "5+ portions (Type quantity)",
        })
    rows.append({"id": "nav:back", "title": "⬅ Go back"})

    display_name = menu_service.friendly_item_name(item.name)
    item_label = f"*{display_name}*" if display_name == item.name else f"*{display_name}* (*{item.name}*)"
    cart_line = await _cart_status_line(session, user.id)
    body = _format_poll_header(
        f"{_step_label(user.phone_number, 'quantity')} · Set quantity",
        f"{item_label} ({naira(item.price)} each) · {cart_line}",
    )
    await whatsapp.send_list(
        user.phone_number,
        body,
        "Choose quantity",
        item.name,
        rows,
    )


async def prompt_custom_quantity(session: AsyncSession, user: User, item_id: int, *, mode: str = "set") -> None:
    """Explicit free-text handoff when customer selects `5+ portions (Type quantity)`."""
    item: MenuItem | None = await menu_service.get_item(session, item_id)
    if item is None or not item.is_available:
        await whatsapp.send_text(user.phone_number, "Sorry, that item is no longer available. Send *menu* to pick something else.")
        return
    available = item.sellable_units
    if available is not None and available < 5:
        await whatsapp.send_text(user.phone_number, f"Only {available} portion(s) of {menu_service.friendly_item_name(item.name)} left.")
        return await show_quantity_selection(session, user, item_id, mode=mode)

    max_allowed = MAX_CUSTOM_QUANTITY if available is None else min(MAX_CUSTOM_QUANTITY, available)
    _CUSTOM_QTY_ITEM[user.phone_number] = (item.id, mode)
    display_name = menu_service.friendly_item_name(item.name)
    await whatsapp.send_text(
        user.phone_number,
        (
            f"✍️ *TYPE QUANTITY BELOW ({_step_label(user.phone_number, 'quantity')})*\n"
            f"How many portions of *{display_name}* ({naira(item.price)} each) would you like?\n\n"
            f"👉 Type a number between *5* and *{max_allowed}* (for example: *6*)\n"
            f"_(Or reply *BACK* to go back, or *CANCEL* to cancel)_"
        ),
    )


async def handle_set_item_quantity(session: AsyncSession, user: User, item_id: int, quantity: int) -> None:
    """Set exact quantity for `item_id` when updating via Change Quantity, then refresh Add-On Hub."""
    item: MenuItem | None = await menu_service.get_item(session, item_id)
    if item is None or not item.is_available:
        await whatsapp.send_text(user.phone_number, "Sorry, that item just ran out. Send *menu* to pick something else.")
        return
    if quantity < 1 or quantity > MAX_CUSTOM_QUANTITY:
        await whatsapp.send_text(user.phone_number, "Please choose a valid quantity.")
        return
    limit = item.sellable_units
    if limit is not None and quantity > limit:
        await whatsapp.send_buttons(
            user.phone_number,
            f"⚠️ Sorry, only {limit} {menu_service.friendly_item_name(item.name)} available.",
            [("menu", "📂 Browse Full Menu"), ("checkout", "✅ Checkout"), ("nav:back", "⬅ Go back")],
        )
        return
    cart = await order_service.get_or_create_cart(session, user.id)
    await order_service.set_cart_item_quantity(session, user.id, item_id, quantity=quantity, cart_id=cart.id)
    await send_addon_hub(
        session,
        user,
        last_item=item,
        added_qty=quantity,
        hub_title=f"✅ Updated {menu_service.friendly_item_name(item.name)} to {quantity} portion{'s' if quantity != 1 else ''}",
    )


async def handle_add_item(
    session: AsyncSession, user: User, item_id: int, quantity: int
) -> None:
    item: MenuItem | None = await menu_service.get_item(session, item_id)
    if item is None or not item.is_available:  # re-check: stock may have changed since the list was sent
        await whatsapp.send_text(user.phone_number, "Sorry, that item just ran out. Send *menu* to pick something else.")
        return
    if quantity < 1 or quantity > MAX_CUSTOM_QUANTITY:
        await whatsapp.send_text(user.phone_number, "Please choose a valid quantity.")
        return
    cart = await order_service.get_or_create_cart(session, user.id)
    lines = await order_service.get_cart(session, cart.id)
    in_cart = next((c.quantity for c, m in lines if m.id == item_id), 0)
    limit = item.sellable_units  # None when stock isn't tracked
    if limit is not None and in_cart + quantity > limit:
        await whatsapp.send_buttons(
            user.phone_number,
            f"⚠️ Sorry, only {limit} {menu_service.friendly_item_name(item.name)} available and you already have {in_cart} in your cart.",
            [("menu", "📂 Browse Full Menu"), ("checkout", "✅ Checkout"), ("nav:back", "⬅ Go back")],
        )
        return
    await order_service.add_to_cart(session, user.id, item_id, quantity=quantity, cart_id=cart.id)
    await send_addon_hub(session, user, last_item=item, added_qty=quantity)


async def handle_checkout(session: AsyncSession, user: User) -> None:
    # One open checkout at a time. Without this, tapping an old "Checkout" button created a
    # second order and sent the customer back to the Delivery/Pickup question.
    open_fulfillment = await get_open_fulfillment(session, user.id)
    if open_fulfillment is not None:
        if open_fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL:
            text = (
                f"Your order #{open_fulfillment.payment_batch_id} is waiting for payment confirmation. "
                "You can place another order once it has been approved."
            )
        else:
            text = (
                f"You already have order #{open_fulfillment.payment_batch_id} in progress. "
                "Please finish it first, or send *cancel* to cancel it and start again."
            )
        await whatsapp.send_text(user.phone_number, text)
        return

    cart_ids = await order_service.get_selected_cart_ids(session, user.id)
    if not cart_ids:
        await whatsapp.send_text(
            user.phone_number,
            "No cart is selected yet. Tap a cart below to select it (✓ means selected), then choose Checkout.",
        )
        await show_carts(session, user)
        return
    try:
        batch = await order_service.create_orders_from_carts(session, user, cart_ids)
    except order_service.CheckoutError as exc:
        await whatsapp.send_text(
            user.phone_number, f"Sorry, we can't check out yet: {exc}. Please send *menu* to update your cart."
        )
        return
    if batch is None:
        await whatsapp.send_text(user.phone_number, "Your selected carts are empty. Send *menu* to start.")
        return
    for cart_id in cart_ids:
        cart = await session.get(Cart, cart_id)
        if cart:
            cart.is_selected = False

    now = datetime.now(timezone.utc)
    if batch.fulfillment is None:
        batch.fulfillment = Fulfillment(payment_batch_id=batch.id)
    batch.fulfillment.fulfillment_type = "delivery"
    batch.fulfillment.stage = FulfillmentStage.COLLECTING_DETAILS
    batch.fulfillment.timer_started_at = now
    batch.fulfillment.reminders_sent = "[]"
    kitchen_note = _KITCHEN_NOTES.pop(user.phone_number, None)
    if kitchen_note:
        batch.fulfillment.notes = f"Kitchen: {kitchen_note}"[:200]
    await session.flush()

    user.current_state = SessionState.COLLECTING_DETAILS
    pre_area = _PRESELECTED_AREA.pop(user.phone_number, None)
    saved, saved_area = await _active_saved_address(session, user.id)

    # Pre-selected area from free-text ("... to Akute") takes priority if saved address is absent or different
    if pre_area is not None and (saved_area is None or pre_area[0] != saved_area.id):
        area = await session.get(DeliveryArea, pre_area[0])
        if area is not None and area.is_active:
            batch.fulfillment.fulfillment_type = "delivery"
            batch.fulfillment.area_id = area.id
            batch.fulfillment.area_name = area.name
            batch.fulfillment.expected_minutes = area.expected_minutes
            batch.fulfillment.checkout_step = "street"
            _journey_update(user.phone_number, preselected_area=True, has_saved=False, fulfillment="delivery")
            await whatsapp.send_text(
                user.phone_number,
                M2_STREET.format(step_label=_step_label(user.phone_number, "street")),
            )
            return

    # 1-Tap Checkout Fast-Forward for Returning Customers with SavedAddress
    if saved is not None and saved_area is not None:
        batch.fulfillment.fulfillment_type = "delivery"
        batch.fulfillment.street = saved.street
        batch.fulfillment.area_id = saved_area.id
        batch.fulfillment.area_name = saved_area.name
        batch.fulfillment.landmark = saved.landmark
        batch.fulfillment.phone2 = saved.phone2
        batch.fulfillment.notes = _merge_kitchen_and_rider_notes(batch.fulfillment.notes, saved.notes)
        batch.fulfillment.latitude = saved.latitude
        batch.fulfillment.longitude = saved.longitude
        batch.fulfillment.expected_minutes = saved_area.expected_minutes
        batch.fulfillment.checkout_step = "summary"
        _USED_SAVED_SUMMARY.add(user.phone_number)
        _journey_update(user.phone_number, has_saved=True, preselected_area=False, fulfillment="delivery")
        await send_summary(session, user, batch.fulfillment, batch=batch, using_saved_address=True)
        return

    # New Customer: 2-Step Checkout Page 1 (Select Area / Pickup / Other Area)
    batch.fulfillment.checkout_step = "choice"
    _journey_update(user.phone_number, has_saved=False, preselected_area=False, fulfillment=None)
    await _send_fulfillment_choice_poll(user, batch)


async def _send_fulfillment_choice_poll(user: User, batch: PaymentBatch) -> None:
    total_qty, subtotal = _batch_totals(batch)
    pack_fee = getattr(batch, "packaging_fee", Decimal("0")) or Decimal("0")
    base_total = subtotal + pack_fee
    cart_line = f"🛒 Order: {total_qty} item{'s' if total_qty != 1 else ''} · {naira(base_total)}"
    await whatsapp.send_buttons(
        user.phone_number,
        M1_CHOICE.format(step_label=_step_label(user.phone_number, "choice"), cart_line=cart_line),
        [
            ("fulfillment:delivery", "📍 Select Delivery Area"),
            ("fulfillment:pickup", f"🏃 Pickup at {settings.outlet_address} (₦0 Fee)"),
            ("area:other", "✍️ Add Other Area (Custom quote)"),
            ("nav:back", "⬅ Back to Cart"),
        ],
    )


async def handle_receipt_upload(session: AsyncSession, user: User, media_id: str) -> None:
    order = await order_service.get_latest_pending_order(session, user.id)
    if order is None:
        await whatsapp.send_text(user.phone_number, "I couldn't find a pending order. Send *menu* to start a new one.")
        return
    batch = await order_service.get_batch(session, order.payment_batch_id)
    if batch is None:
        await whatsapp.send_text(user.phone_number, "I couldn't find the payment batch. Please contact us.")
        return
    batch.receipt_media_id = media_id
    await whatsapp.send_text(user.phone_number, "Receipt received. We're confirming your payment and will message you shortly.")


async def show_carts(session: AsyncSession, user: User) -> None:
    carts = await order_service.get_carts(session, user.id)
    if not carts:
        await whatsapp.send_text(user.phone_number, "You have no carts yet. Send *menu* to start.")
        return

    _push_nav(user.phone_number, {"kind": "carts"})
    active_cart = await order_service.get_or_create_cart(session, user.id)
    active_lines = await order_service.get_cart(session, active_cart.id)
    total_qty, total_sub, pack_fee = await order_service.get_user_cart_totals(session, user.id)
    cart_total = total_sub + pack_fee
    step_lbl = _step_label(user.phone_number, "carts")

    # Send itemized cart summary text if the active cart has items
    if active_lines:
        item_bullets = "\n".join(
            f"• {c.quantity} x {menu_service.friendly_item_name(m.name)} — {naira(m.price * c.quantity)}"
            for c, m in active_lines
        )
        pack_line = _pack_summary_line(pack_fee)
        await whatsapp.send_text(
            user.phone_number,
            (
                f"🛒 *YOUR CART ({step_lbl})*\n{item_bullets}\n"
                f"• Items subtotal: {naira(total_sub)}\n{pack_line}"
                f"━━━━━━━━━━━━━━━━━━\n*Total:* {naira(cart_total)}"
            ),
        )

    rows: list[dict] = []
    if total_qty > 0:
        rows.append({
            "id": "checkout",
            "title": "✅ Proceed to checkout",
            "description": f"{total_qty} item(s), {naira(cart_total)}",
        })
    rows.append({"id": "menu", "title": "➕ Add more items"})

    for c_item, m_item in active_lines[:3]:
        rows.append({
            "id": f"rmitem:{m_item.id}",
            "title": f"➖ Remove {menu_service.friendly_item_name(m_item.name)}"[:24],
            "description": f"Remove {c_item.quantity}x ({naira(m_item.price * c_item.quantity)})",
        })

    if len(carts) > 1:
        for cart in carts:
            lines = await order_service.get_cart(session, cart.id)
            subtotal = order_service.cart_subtotal(lines)
            marker = "✓ " if cart.is_selected else ""
            rows.append({
                "id": f"cart:{cart.id}",
                "title": f"{marker}{cart.name}"[:24],
                "description": f"{sum(c.quantity for c, _ in lines)} item(s), {naira(subtotal)}",
            })

    if total_qty > 0:
        rows.append({"id": "clearcart", "title": "🗑️ Clear cart"})
    rows.append({"id": "newcart", "title": "🛍️ New cart (Separate pack)"})
    rows.append({"id": "staff:escalate", "title": "💬 Talk to staff"})
    rows.append({"id": "nav:back", "title": "⬅ Go back"})

    max_rows = 12 if (whatsapp.is_evolution_provider() or settings.dry_run) else 10
    if len(rows) > max_rows:
        # Preserve trailing Clear / Staff / Go back when slicing
        tail = rows[-3:]
        rows = rows[: max_rows - 3] + tail

    user.current_state = SessionState.SELECTING_CARTS
    await whatsapp.send_list(
        user.phone_number,
        _format_poll_header(
            f"{step_lbl} · Review or edit cart",
            f"🛒 {total_qty} item(s) · Total: {naira(cart_total)} · Tap Checkout when ready:",
        ),
        "Manage cart",
        "Your carts",
        rows,
    )


async def toggle_cart(session: AsyncSession, user: User, cart_id: int) -> None:
    cart = await session.get(Cart, cart_id)
    if cart is None or cart.user_id != user.id:
        await whatsapp.send_text(user.phone_number, "That cart could not be found.")
        return
    cart.is_selected = not cart.is_selected
    await show_carts(session, user)


# ============================================================================
# PHASE 2: CHECKOUT FLOW (Fulfillment, Address Collection, Fee Quotes)
# ============================================================================


async def get_open_fulfillment(session: AsyncSession, user_id: int) -> Fulfillment | None:
    """Get the customer's open checkout fulfillment if one exists."""
    open_stages = (
        FulfillmentStage.COLLECTING_DETAILS, FulfillmentStage.AWAITING_FEE_QUOTE,
        FulfillmentStage.AWAITING_FEE_ACCEPT, FulfillmentStage.AWAITING_PAYMENT,
        FulfillmentStage.AWAITING_APPROVAL,
    )
    result = await session.execute(
        select(Fulfillment)
        .join(PaymentBatch)
        .where(PaymentBatch.user_id == user_id, Fulfillment.stage.in_(open_stages))
        .order_by(Fulfillment.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def cancel_checkout(session: AsyncSession, user: User, batch: PaymentBatch, fulfillment: Fulfillment) -> None:
    """Cancel an order that has not been approved yet (batch.orders must already be loaded)."""
    fulfillment.stage = FulfillmentStage.CANCELLED
    fulfillment.cancelled_at = datetime.now(timezone.utc)
    fulfillment.timer_started_at = None
    fulfillment.address_editing = False
    fulfillment.checkout_step = None
    batch.status = OrderStatus.CANCELLED
    for order in batch.orders:
        order.status = OrderStatus.CANCELLED
    _clear_user_ephemeral_state(user.phone_number)
    user.current_state = SessionState.BROWSING
    await whatsapp.send_text(
        user.phone_number, f"Order #{batch.id} has been cancelled. Send *menu* whenever you'd like to start again."
    )


async def return_checkout_to_cart(session: AsyncSession, user: User, batch: PaymentBatch, fulfillment: Fulfillment) -> None:
    """Restore items from an unconfirmed checkout batch back into the cart and open cart view."""
    kitchen_prefix = _extract_kitchen_prefix(fulfillment.notes)
    if kitchen_prefix:
        _KITCHEN_NOTES[user.phone_number] = kitchen_prefix.removeprefix("Kitchen: ").strip()
    await order_service.restore_cart_from_batch(session, user, batch)
    fulfillment.stage = FulfillmentStage.CANCELLED
    fulfillment.cancelled_at = datetime.now(timezone.utc)
    fulfillment.timer_started_at = None
    fulfillment.address_editing = False
    fulfillment.checkout_step = None
    batch.status = OrderStatus.CANCELLED
    for order in batch.orders:
        order.status = OrderStatus.CANCELLED
    _USED_SAVED_SUMMARY.discard(user.phone_number)
    user.current_state = SessionState.SELECTING_CARTS
    await show_carts(session, user)


async def start_address_edit(session: AsyncSession, user: User, fulfillment: Fulfillment) -> None:
    """The customer replied ADDRESS: let them re-enter everything except the area."""
    if fulfillment.fulfillment_type == "pickup":
        await whatsapp.send_text(user.phone_number, "This is a pickup order, so there is no delivery address to change.")
        return
    fulfillment.address_editing = True
    fulfillment.address_edit_started_at = datetime.now(timezone.utc)
    if fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL:
        await whatsapp.send_text(user.phone_number, M15B_AREA_LOCKED.format(support_phone=settings.support_phone))
    else:
        await whatsapp.send_text(
            user.phone_number,
            "You can change your street, landmark, second phone and notes, but your area must stay the same. "
            f"To change your area, please call {settings.support_phone}.",
        )
    await begin_address(session, user, fulfillment, keep_area=True)


async def handle_checkout_flow(
    session: AsyncSession, user: User, fulfillment: Fulfillment, msg: IncomingMessage
) -> None:
    """Process a message arriving while the customer has an open checkout."""
    text = (msg.text or "").strip()
    lowered = text.lower()
    rid = msg.reply_id or ""
    batch = await order_service.get_batch(session, fulfillment.payment_batch_id)
    if batch is None:
        user.current_state = SessionState.BROWSING
        await whatsapp.send_text(user.phone_number, "I couldn't find that order. Send *menu* to start again.")
        return
    stage = fulfillment.stage
    cancelling = lowered in CANCEL_COMMANDS or rid == "cancel"
    going_back = lowered in BACK_COMMANDS or rid == "nav:back"

    # Changing the address after checkout: keep answering the address questions.
    if fulfillment.address_editing and fulfillment.checkout_step:
        if cancelling:
            fulfillment.address_editing = False
            fulfillment.address_edit_started_at = None
            fulfillment.checkout_step = None
            user.current_state = (
                SessionState.AWAITING_APPROVAL if stage == FulfillmentStage.AWAITING_APPROVAL
                else SessionState.AWAITING_PAYMENT
            )
            await whatsapp.send_text(user.phone_number, "Okay, we kept your current delivery address.")
            return
        await process_address_step(session, user, fulfillment, batch, msg)
        return

    if rid in {"checkout:edit_items", "cart:view"} and stage in (FulfillmentStage.COLLECTING_DETAILS, FulfillmentStage.AWAITING_FEE_ACCEPT):
        await return_checkout_to_cart(session, user, batch, fulfillment)
        return

    if cancelling:
        if stage == FulfillmentStage.AWAITING_APPROVAL:
            await whatsapp.send_text(
                user.phone_number,
                f"Your payment for order #{batch.id} is already being reviewed, so it can't be cancelled here. "
                f"Please call {settings.support_phone}.",
            )
            return
        await cancel_checkout(session, user, batch, fulfillment)
        return

    if stage == FulfillmentStage.AWAITING_FEE_QUOTE:
        await whatsapp.send_text(user.phone_number, M8B_OTHER_AREA)
        return

    if stage == FulfillmentStage.AWAITING_FEE_ACCEPT:
        if rid == "quote:accept":
            fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
            fulfillment.timer_started_at = datetime.now(timezone.utc)
            fulfillment.reminders_sent = "[]"
            user.current_state = SessionState.AWAITING_PAYMENT
            await send_payment_message(user, batch, fulfillment)
        elif rid == "quote:cancel":
            await cancel_checkout(session, user, batch, fulfillment)
        elif rid == "address:edit" or going_back:
            fulfillment.stage = FulfillmentStage.COLLECTING_DETAILS
            fulfillment.checkout_step = "area"
            user.current_state = SessionState.COLLECTING_DETAILS
            await send_area_page(session, user, fulfillment, 0, batch=batch)
        else:
            fee = batch.delivery_fee or Decimal("0")
            await whatsapp.send_buttons(
                user.phone_number,
                M8C_QUOTE.format(area=fulfillment.area_name, fee=naira(fee), total=naira(batch.total_amount)),
                [
                    ("quote:accept", f"✅ Proceed to pay ({naira(batch.total_amount)})"),
                    ("address:edit", "📍 Change delivery area"),
                    ("quote:cancel", "❌ Cancel order"),
                ],
            )
        return

    if stage == FulfillmentStage.AWAITING_PAYMENT:
        if msg.type == "image" and msg.media_id:
            batch.receipt_media_id = msg.media_id
            try:
                content, mime = await whatsapp.download_media(msg.media_id)
                mime_clean = (mime or "").strip().lower()
                if content and len(content) <= MAX_RECEIPT_BYTES and mime_clean.startswith("image/"):
                    session.add(
                        ReceiptImage(
                            payment_batch_id=batch.id,
                            media_id=msg.media_id,
                            content=content,
                            mime_type=mime_clean[:100],
                            size_bytes=len(content),
                        )
                    )
                else:
                    logger.warning(
                        "Receipt media %s for batch %s not stored (size=%s, mime=%r)",
                        msg.media_id,
                        batch.id,
                        len(content) if content else 0,
                        mime,
                    )
            except Exception:
                logger.warning(
                    "Could not download receipt media %s for batch %s",
                    msg.media_id,
                    batch.id,
                    exc_info=True,
                )
            fulfillment.stage = FulfillmentStage.AWAITING_APPROVAL
            fulfillment.timer_started_at = None  # the customer has paid; from here only staff can act
            user.current_state = SessionState.AWAITING_APPROVAL
            await whatsapp.send_text(user.phone_number, M10_RECEIPT)
            return
        if lowered == "address":
            await start_address_edit(session, user, fulfillment)
            return
        if going_back:
            fulfillment.stage = FulfillmentStage.COLLECTING_DETAILS
            fulfillment.checkout_step = "summary"
            user.current_state = SessionState.COLLECTING_DETAILS
            await send_summary(session, user, fulfillment, batch=batch)
            return
        await whatsapp.send_text(
            user.phone_number,
            "📸 Please send your payment receipt photo here, reply *ADDRESS* to update your address, or reply *CANCEL* to cancel.",
        )
        return

    if stage == FulfillmentStage.AWAITING_APPROVAL:
        if lowered == "address":
            await start_address_edit(session, user, fulfillment)
            return
        await whatsapp.send_text(user.phone_number, "⏳ Your order is being reviewed by our team. You'll hear from us shortly!")
        return

    await process_address_step(session, user, fulfillment, batch, msg)


async def _active_saved_address(
    session: AsyncSession, user_id: int
) -> tuple[SavedAddress | None, DeliveryArea | None]:
    """Load the user's SavedAddress explicitly and verify its DeliveryArea is still active."""
    saved = (
        await session.execute(select(SavedAddress).where(SavedAddress.user_id == user_id))
    ).scalar_one_or_none()
    if saved is None or saved.area_id is None:
        return None, None
    area = await session.get(DeliveryArea, saved.area_id)
    if area is None or not area.is_active:
        return None, None
    return saved, area


async def _send_saved_address_prompt(
    user: User, saved: SavedAddress, area: DeliveryArea, batch: PaymentBatch | None = None
) -> None:
    subtotal = _batch_totals(batch)[1] if batch is not None else Decimal("0")
    total = subtotal + area.fee
    fee_line = f"🛵 Delivery fee: {naira(area.fee)} · Total: {naira(total)}\n" if batch is not None else ""
    await whatsapp.send_buttons(
        user.phone_number,
        (
            f"Step 5 of 6: Confirm delivery address\n"
            f"📍 {saved.street}, {area.name}\n"
            f"Landmark: {saved.landmark}\n"
            f"{fee_line}"
            f"Deliver to your saved address?"
        ),
        [
            ("saved:use", f"📍 Use this address (+{naira(area.fee)})"),
            ("saved:new", "✏️ Enter a new address"),
            ("nav:back", "⬅ Go back"),
            ("cancel", "❌ Cancel order"),
        ],
    )


async def save_address(session: AsyncSession, user: User, fulfillment: Fulfillment) -> None:
    """Insert or update the customer's single SavedAddress row from a confirmed delivery fulfillment."""
    if fulfillment.fulfillment_type != "delivery" or not fulfillment.street:
        return
    saved = (
        await session.execute(select(SavedAddress).where(SavedAddress.user_id == user.id))
    ).scalar_one_or_none()
    area_name = fulfillment.area_name or ""
    landmark = fulfillment.landmark or ""
    rider_notes = _extract_rider_notes(fulfillment.notes)
    if saved is None:
        session.add(
            SavedAddress(
                user_id=user.id,
                street=fulfillment.street,
                area_id=fulfillment.area_id,
                area_name=area_name,
                landmark=landmark,
                phone2=fulfillment.phone2,
                notes=rider_notes,
                latitude=fulfillment.latitude,
                longitude=fulfillment.longitude,
            )
        )
    else:
        saved.street = fulfillment.street
        saved.area_id = fulfillment.area_id
        saved.area_name = area_name
        saved.landmark = landmark
        saved.phone2 = fulfillment.phone2
        saved.notes = rider_notes
        saved.latitude = fulfillment.latitude
        saved.longitude = fulfillment.longitude
    await session.flush()


def _parse_combined_address(raw: str) -> tuple[str, str] | None:
    """Validate and split a combined street + landmark message into `(street, landmark)`.

    Returns `None` if the input is too short or vague (< 8 chars or < 2 words).
    """
    cleaned = re.sub(r"\s+", " ", (raw or "").strip())
    if len(cleaned) < 8 or len(cleaned) > 220 or len(cleaned.split()) < 2:
        return None

    # 1. Explicit landmark preposition phrase after street text
    m = re.match(
        r"^(?P<street>.{4,}?)(?:[,;\n-]+\s*|\s+)(?P<landmark>(?:opposite|opp\.?|near|beside|behind|by|after|before|adjacent|close\s+to|next\s+to|off|around|at)\b.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m:
        street = m.group("street").strip(" ,;-")[:150]
        landmark = m.group("landmark").strip(" ,;-")[:100]
        if street and landmark:
            return street, landmark

    # 2. Comma / semicolon / dash separator
    parts = [p.strip() for p in re.split(r"[,;\n]+|\s+-\s+", cleaned) if p.strip()]
    if len(parts) >= 2:
        street = parts[0][:150]
        landmark = ", ".join(parts[1:])[:100]
        if street and landmark:
            return street, landmark

    # 3. Single multi-word address phrase
    return cleaned[:150], cleaned[:100]


async def _send_extras_menu(user: User, fulfillment: Fulfillment) -> None:
    """Send the optional delivery details picker from the Summary screen."""
    status_parts: list[str] = []
    if fulfillment.latitude is not None and fulfillment.longitude is not None:
        status_parts.append("📌 GPS Pin: Added")
    if fulfillment.phone2:
        status_parts.append(f"📞 Phone 2: {fulfillment.phone2}")
    rider_note = _extract_rider_notes(fulfillment.notes)
    if rider_note:
        status_parts.append(f"📝 Note: {rider_note[:30]}")
    status_line = " · ".join(status_parts) if status_parts else "No optional details added yet"

    await whatsapp.send_buttons(
        user.phone_number,
        _format_poll_header(
            f"{_step_label(user.phone_number, 'extras')} · Optional delivery details",
            status_line,
        ),
        [
            ("extras:done", "✅ Done — Back to Order Summary"),
            ("extras:pin", "📍 Share GPS location pin"),
            ("extras:phone2", "📞 Add backup phone number"),
            ("extras:notes", "📝 Add delivery / gate note"),
            ("nav:back", "⬅ Go back"),
        ],
    )


async def _handle_checkout_go_back(
    session: AsyncSession, user: User, fulfillment: Fulfillment, batch: PaymentBatch
) -> None:
    """Step backward one screen inside `process_address_step` when customer taps `⬅ Go back`."""
    step = fulfillment.checkout_step or "choice"
    if step == "choice":
        await return_checkout_to_cart(session, user, batch, fulfillment)
        return
    if step in ("area", "saved_choice"):
        if fulfillment.address_editing:
            fulfillment.address_editing = False
            fulfillment.address_edit_started_at = None
            fulfillment.checkout_step = None
            await whatsapp.send_text(user.phone_number, "Okay, we kept your current delivery address.")
            return
        fulfillment.checkout_step = "choice"
        await _send_fulfillment_choice_poll(user, batch)
        return
    if step in ("street", "other_area"):
        if fulfillment.address_editing:
            fulfillment.address_editing = False
            fulfillment.address_edit_started_at = None
            fulfillment.checkout_step = None
            await whatsapp.send_text(user.phone_number, "Okay, we kept your current delivery address.")
            return
        fulfillment.checkout_step = "area"
        await send_area_page(session, user, fulfillment, 0, batch=batch)
        return
    if step == "extras":
        fulfillment.checkout_step = "summary"
        await send_summary(session, user, fulfillment, batch=batch)
        return
    if step in ("pin", "phone2", "notes"):
        if fulfillment.street:
            fulfillment.checkout_step = "extras"
            await _send_extras_menu(user, fulfillment)
        else:
            fulfillment.checkout_step = "street"
            await whatsapp.send_text(
                user.phone_number,
                M2_STREET.format(step_label=_step_label(user.phone_number, "street")),
            )
        return
    if step == "landmark":
        fulfillment.checkout_step = "street"
        await whatsapp.send_text(
            user.phone_number,
            M2_STREET.format(step_label=_step_label(user.phone_number, "street")),
        )
        return
    if step == "summary":
        if user.phone_number in _USED_SAVED_SUMMARY:
            await return_checkout_to_cart(session, user, batch, fulfillment)
        elif fulfillment.fulfillment_type == "pickup":
            fulfillment.checkout_step = "choice"
            await _send_fulfillment_choice_poll(user, batch)
        else:
            fulfillment.checkout_step = "street"
            await whatsapp.send_text(
                user.phone_number,
                M2_STREET.format(step_label=_step_label(user.phone_number, "street")),
            )
        return


async def _send_phone2_prompt(user: User) -> None:
    await whatsapp.send_buttons(
        user.phone_number,
        M6_SECOND_PHONE.format(step_label=_step_label(user.phone_number, "phone2")),
        [
            ("address:skip_phone", "⏭️ Skip backup phone"),
            ("address:fast_summary", "⚡ Back to Order Summary"),
            ("nav:back", "⬅ Go back"),
        ],
    )


async def _send_notes_prompt(user: User) -> None:
    await whatsapp.send_buttons(
        user.phone_number,
        M7_NOTES.format(step_label=_step_label(user.phone_number, "notes")),
        [
            ("address:skip_notes", "🚫 No extra notes — Review order"),
            ("nav:back", "⬅ Go back"),
        ],
    )


async def process_address_step(
    session: AsyncSession, user: User, fulfillment: Fulfillment, batch: PaymentBatch, msg: IncomingMessage
) -> None:
    """Handle customer input while collecting address details step-by-step."""
    text = (msg.text or "").strip()
    lowered = text.lower()
    rid = msg.reply_id or ""
    step = fulfillment.checkout_step or ""

    if rid == "nav:back" or lowered in BACK_COMMANDS:
        await _handle_checkout_go_back(session, user, fulfillment, batch)
        return

    if step == "choice":
        if rid in {"fulfillment:pickup", "fulfill:pickup"}:
            fulfillment.fulfillment_type = "pickup"
            fulfillment.checkout_step = "summary"
            _USED_SAVED_SUMMARY.discard(user.phone_number)
            _journey_update(user.phone_number, fulfillment="pickup")
            await send_summary(session, user, fulfillment, batch=batch, using_saved_address=False)
        elif rid in {"fulfillment:delivery", "fulfill:delivery"}:
            fulfillment.fulfillment_type = "delivery"
            fulfillment.checkout_step = "area"
            _USED_SAVED_SUMMARY.discard(user.phone_number)
            _journey_update(user.phone_number, fulfillment="delivery")
            await send_area_page(session, user, fulfillment, 0, batch=batch)
        elif rid == "area:other":
            fulfillment.fulfillment_type = "delivery"
            fulfillment.checkout_step = "other_area"
            _USED_SAVED_SUMMARY.discard(user.phone_number)
            _journey_update(user.phone_number, fulfillment="other_area")
            await whatsapp.send_text(
                user.phone_number,
                M3B_OTHER_AREA.format(step_label=_step_label(user.phone_number, "other_area")),
            )
        elif rid.startswith("area:"):
            # Also accept direct area:{id} from Page 1 if sent
            area_id_str = rid.split(":", 1)[1]
            area = await session.get(DeliveryArea, int(area_id_str)) if area_id_str.isdecimal() else None
            if area is not None and area.is_active:
                fulfillment.fulfillment_type = "delivery"
                fulfillment.area_id = area.id
                fulfillment.area_name = area.name
                fulfillment.expected_minutes = area.expected_minutes
                fulfillment.checkout_step = "street"
                _journey_update(user.phone_number, fulfillment="delivery")
                await whatsapp.send_text(
                    user.phone_number,
                    M2_STREET.format(step_label=_step_label(user.phone_number, "street")),
                )
            else:
                await _send_fulfillment_choice_poll(user, batch)
        else:
            await _send_fulfillment_choice_poll(user, batch)
        return

    if step == "saved_choice":
        if rid == "saved:new":
            fulfillment.checkout_step = "area"
            await send_area_page(session, user, fulfillment, 0, batch=batch)
            return
        saved, area = await _active_saved_address(session, user.id)
        if saved is None or area is None:
            fulfillment.checkout_step = "area"
            await send_area_page(session, user, fulfillment, 0, batch=batch)
            return
        if rid == "saved:use":
            fulfillment.street = saved.street
            fulfillment.area_id = area.id
            fulfillment.area_name = area.name
            fulfillment.landmark = saved.landmark
            fulfillment.phone2 = saved.phone2
            fulfillment.notes = _merge_kitchen_and_rider_notes(fulfillment.notes, saved.notes)
            fulfillment.latitude = saved.latitude
            fulfillment.longitude = saved.longitude
            fulfillment.expected_minutes = area.expected_minutes
            await confirm_address(session, user, fulfillment)
            return
        await _send_saved_address_prompt(user, saved, area, batch=batch)
        return

    if step == "area":
        if rid.startswith("areas:") or rid.startswith("areapg:"):
            page_str = rid.split(":", 1)[1]
            page = int(page_str) if page_str.isdecimal() else 0
            await send_area_page(session, user, fulfillment, page, batch=batch)
            return
        if rid == "area:other":
            fulfillment.checkout_step = "other_area"
            _journey_update(user.phone_number, fulfillment="other_area")
            await whatsapp.send_text(
                user.phone_number,
                M3B_OTHER_AREA.format(step_label=_step_label(user.phone_number, "other_area")),
            )
            return
        if rid.startswith("area:"):
            area_id_str = rid.split(":", 1)[1]
            if not area_id_str.isdecimal():
                await send_area_page(session, user, fulfillment, 0, batch=batch)
                return
            area_id = int(area_id_str)
            area = await session.get(DeliveryArea, area_id)
            if area is None or not area.is_active:
                await send_area_page(session, user, fulfillment, 0, batch=batch)
                return
            fulfillment.area_id = area.id
            fulfillment.area_name = area.name
            fulfillment.expected_minutes = area.expected_minutes
            fulfillment.checkout_step = "street"
            _journey_update(user.phone_number, fulfillment="delivery")
            await whatsapp.send_text(
                user.phone_number,
                M2_STREET.format(step_label=_step_label(user.phone_number, "street")),
            )
            return
        await send_area_page(session, user, fulfillment, 0, batch=batch)
        return

    if step == "other_area":
        if not text or len(text) > 120:
            await whatsapp.send_text(
                user.phone_number,
                "⚠️ Please type your area name (up to 120 characters), or reply *BACK* to pick from our standard areas.",
            )
            return
        fulfillment.area_id = None
        fulfillment.area_name = text
        fulfillment.checkout_step = "street"
        _journey_update(user.phone_number, fulfillment="delivery")
        await whatsapp.send_text(
            user.phone_number,
            M2_STREET.format(step_label=_step_label(user.phone_number, "street")),
        )
        return

    if step == "street":
        if msg.type == "location" and msg.latitude is not None and msg.longitude is not None:
            fulfillment.latitude = msg.latitude
            fulfillment.longitude = msg.longitude
            if fulfillment.street:
                fulfillment.checkout_step = "summary"
                await send_summary(session, user, fulfillment, batch=batch)
            else:
                step_lbl = _step_label(user.phone_number, "street")
                await whatsapp.send_text(
                    user.phone_number,
                    (
                        f"📍 *GPS Location Pin saved! ({step_lbl})*\n"
                        "Now please type your *house/gate number, street name, and nearest landmark* in one message so our rider knows your exact door.\n"
                        "👉 Example: *14 Denro Road, yellow gate opposite St. Leo Church*\n\n"
                        "_(Or reply *BACK* to change your area, or *CANCEL* to cancel)_"
                    ),
                )
            return

        parsed = _parse_combined_address(text)
        if parsed is None:
            await whatsapp.send_text(
                user.phone_number,
                "⚠️ Please type both your *house/street address* and a *nearby landmark* in one message (at least 8 characters, e.g. *14 Denro Road, opposite St. Leo Church*), or reply *BACK*.",
            )
            return
        fulfillment.street, fulfillment.landmark = parsed
        fulfillment.checkout_step = "summary"
        await send_summary(session, user, fulfillment, batch=batch)
        return

    if step == "extras":
        if rid == "extras:done":
            fulfillment.checkout_step = "summary"
            await send_summary(session, user, fulfillment, batch=batch)
            return
        if rid == "extras:pin":
            fulfillment.checkout_step = "pin"
            await ask_pin(session, user)
            return
        if rid == "extras:phone2":
            fulfillment.checkout_step = "phone2"
            await _send_phone2_prompt(user)
            return
        if rid == "extras:notes":
            fulfillment.checkout_step = "notes"
            await _send_notes_prompt(user)
            return
        if msg.type == "location" and msg.latitude is not None and msg.longitude is not None:
            fulfillment.latitude = msg.latitude
            fulfillment.longitude = msg.longitude
            fulfillment.checkout_step = "summary"
            await send_summary(session, user, fulfillment, batch=batch)
            return
        await _send_extras_menu(user, fulfillment)
        return

    if step == "pin":
        if msg.type == "location" and msg.latitude is not None and msg.longitude is not None:
            fulfillment.latitude = msg.latitude
            fulfillment.longitude = msg.longitude
            fulfillment.checkout_step = "summary"
            await send_summary(session, user, fulfillment, batch=batch)
        elif lowered == "skip" or rid == "address:skip_pin":
            fulfillment.checkout_step = "summary"
            await send_summary(session, user, fulfillment, batch=batch)
        else:
            await ask_pin(session, user)
        return

    if step == "landmark":
        if not text or len(text) > 100:
            await whatsapp.send_text(
                user.phone_number,
                "⚠️ Please send a landmark near your address (up to 100 characters, e.g. *Opposite St. Leo Church*), or reply *BACK*.",
            )
            return
        fulfillment.landmark = text
        fulfillment.checkout_step = "summary"
        await send_summary(session, user, fulfillment, batch=batch)
        return

    if step == "phone2":
        if rid == "address:fast_summary":
            fulfillment.phone2 = None
            fulfillment.notes = _extract_kitchen_prefix(fulfillment.notes)
            fulfillment.checkout_step = "summary"
            await send_summary(session, user, fulfillment, batch=batch)
            return
        if lowered == "skip" or rid == "address:skip_phone":
            fulfillment.phone2 = None
            fulfillment.checkout_step = "summary"
            await send_summary(session, user, fulfillment, batch=batch)
            return
        try:
            fulfillment.phone2 = normalize_phone(text)
        except ValueError:
            await whatsapp.send_buttons(
                user.phone_number,
                _format_poll_header(
                    f"{_step_label(user.phone_number, 'phone2')} · Invalid phone number",
                    "⚠️ Enter a number like *08031234567*, or tap Skip:",
                ),
                [
                    ("address:skip_phone", "⏭️ Skip backup phone"),
                    ("address:fast_summary", "⚡ Back to Order Summary"),
                    ("nav:back", "⬅ Go back"),
                ],
            )
            return
        fulfillment.checkout_step = "summary"
        await send_summary(session, user, fulfillment, batch=batch)
        return

    if step == "notes":
        is_skip = lowered == "skip" or rid == "address:skip_notes"
        rider_note = None if is_skip else text[:200]
        fulfillment.notes = _merge_kitchen_and_rider_notes(fulfillment.notes, rider_note)
        fulfillment.checkout_step = "summary"
        await send_summary(session, user, fulfillment, batch=batch)
        return

    if step == "summary":
        if msg.type == "location" and msg.latitude is not None and msg.longitude is not None:
            fulfillment.latitude = msg.latitude
            fulfillment.longitude = msg.longitude
            await send_summary(session, user, fulfillment, batch=batch)
            return
        if rid in {"address:extras", "checkout:extras"}:
            fulfillment.checkout_step = "extras"
            await _send_extras_menu(user, fulfillment)
            return
        if rid == "checkout:switch_pickup":
            fulfillment.fulfillment_type = "pickup"
            fulfillment.street = None
            fulfillment.area_id = None
            fulfillment.area_name = None
            fulfillment.landmark = None
            fulfillment.latitude = None
            fulfillment.longitude = None
            fulfillment.expected_minutes = None
            _USED_SAVED_SUMMARY.discard(user.phone_number)
            _journey_update(user.phone_number, fulfillment="pickup")
            await send_summary(session, user, fulfillment, batch=batch, using_saved_address=False)
            return
        if rid == "checkout:new_address":
            fulfillment.fulfillment_type = "delivery"
            fulfillment.street = None
            fulfillment.area_id = None
            fulfillment.area_name = None
            fulfillment.landmark = None
            fulfillment.phone2 = None
            fulfillment.notes = _extract_kitchen_prefix(fulfillment.notes)
            fulfillment.latitude = None
            fulfillment.longitude = None
            fulfillment.expected_minutes = None
            fulfillment.checkout_step = "area"
            _USED_SAVED_SUMMARY.discard(user.phone_number)
            _journey_update(user.phone_number, has_saved=False, preselected_area=False, fulfillment="delivery")
            await send_area_page(session, user, fulfillment, 0, batch=batch)
            return
        if rid == "address:edit":
            if fulfillment.fulfillment_type == "pickup":
                fulfillment.checkout_step = "choice"
                await _send_fulfillment_choice_poll(user, batch)
                return
            fulfillment.street = None
            fulfillment.landmark = None
            fulfillment.phone2 = None
            fulfillment.notes = _extract_kitchen_prefix(fulfillment.notes)
            _USED_SAVED_SUMMARY.discard(user.phone_number)
            if fulfillment.address_editing:  # after checkout the area is locked, so restart at the street
                fulfillment.checkout_step = "street"
                await whatsapp.send_text(
                    user.phone_number,
                    M2_STREET.format(step_label=_step_label(user.phone_number, "street")),
                )
            else:  # still checking out: start over from the area so a wrong area can be corrected
                fulfillment.area_id = None
                fulfillment.area_name = None
                fulfillment.latitude = None
                fulfillment.longitude = None
                fulfillment.checkout_step = "area"
                await send_area_page(session, user, fulfillment, 0, batch=batch)
        elif rid in {"address:confirm", "checkout:confirm_saved"}:
            await confirm_address(session, user, fulfillment)
        else:
            await send_summary(session, user, fulfillment, batch=batch)
        return

    # Unknown step (should never happen): never leave the customer in silence.
    await whatsapp.send_text(
        user.phone_number, "Sorry, I lost track of where we were. Please send *cancel* and start your order again."
    )


async def begin_address(session: AsyncSession, user: User, fulfillment: Fulfillment, keep_area: bool = False) -> None:
    """Start or restart address capture from street step."""
    if not keep_area:
        fulfillment.street = None
        fulfillment.area_id = None
        fulfillment.area_name = None
        fulfillment.landmark = None
        fulfillment.phone2 = None
        fulfillment.notes = _extract_kitchen_prefix(fulfillment.notes)
    fulfillment.checkout_step = "street"
    user.current_state = SessionState.COLLECTING_DETAILS
    await whatsapp.send_text(
        user.phone_number,
        M2_STREET.format(step_label=_step_label(user.phone_number, "street")),
    )


async def ask_pin(session: AsyncSession, user: User) -> None:
    """Send a location request and skip/back poll for the customer."""
    step_lbl = _step_label(user.phone_number, "pin")
    await whatsapp.send_location_request(user.phone_number, M4_PIN.format(step_label=step_lbl))
    await whatsapp.send_buttons(
        user.phone_number,
        _format_poll_header(
            f"{step_lbl} · Share GPS location pin or skip",
            "Tap below to skip sharing your GPS pin:",
        ),
        [
            ("address:skip_pin", "⏭️ Skip location pin"),
            ("nav:back", "⬅ Go back"),
        ],
    )


async def send_area_page(
    session: AsyncSession, user: User, fulfillment: Fulfillment, page: int, *, batch: PaymentBatch | None = None
) -> None:
    """Send a paginated poll of 5 active delivery areas with fees, ETA, More/Previous, Other area, and Go back."""
    areas = list((await session.execute(
        select(DeliveryArea).where(DeliveryArea.is_active.is_(True)).order_by(DeliveryArea.sort_order, DeliveryArea.name)
    )).scalars())
    total_pages = max(1, (len(areas) + AREA_PAGE_SIZE - 1) // AREA_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * AREA_PAGE_SIZE
    rows = [
        {"id": f"area:{area.id}", "title": area.name[:24], "description": f"{naira(area.fee)} · ~{area.expected_minutes} min"}
        for area in areas[start:start + AREA_PAGE_SIZE]
    ]
    if start + AREA_PAGE_SIZE < len(areas):
        rows.append({"id": f"areas:{page + 1}", "title": "More areas"})
    if page > 0:
        rows.append({"id": f"areas:{page - 1}", "title": "⬅ Previous areas"})
    rows.append({"id": "area:other", "title": "Other area"})
    rows.append({"id": "nav:back", "title": "⬅ Go back"})

    if batch is None:
        batch = await order_service.get_batch(session, fulfillment.payment_batch_id)
    subtotal = _batch_totals(batch)[1] if batch is not None else Decimal("0")
    pack_fee = getattr(batch, "packaging_fee", Decimal("0")) or Decimal("0")
    cart_line = f"🛒 Order subtotal: {naira(subtotal + pack_fee)}"
    await whatsapp.send_list(
        user.phone_number,
        M3_AREA.format(step_label=_step_label(user.phone_number, "area"), cart_line=cart_line),
        "Choose area",
        "Delivery areas",
        rows,
    )


async def send_summary(
    session: AsyncSession,
    user: User,
    fulfillment: Fulfillment,
    *,
    batch: PaymentBatch | None = None,
    using_saved_address: bool | None = None,
) -> None:
    """Send the full itemized order summary message followed by the dynamic Summary Action Poll."""
    if batch is None:
        batch = await order_service.get_batch(session, fulfillment.payment_batch_id)
    if batch is None:
        return
    subtotal = sum((order.subtotal for order in batch.orders), Decimal("0"))
    pack_fee = getattr(batch, "packaging_fee", Decimal("0")) or Decimal("0")
    base_total = subtotal + pack_fee
    packaging_line = _pack_summary_line(pack_fee)
    items_block = _batch_items_block(batch)
    step_lbl = _step_label(user.phone_number, "summary")
    is_saved_mode = (
        using_saved_address
        if using_saved_address is not None
        else (user.phone_number in _USED_SAVED_SUMMARY and fulfillment.fulfillment_type == "delivery")
    )

    if fulfillment.fulfillment_type == "pickup":
        total_text = naira(base_total)
        pickup_items = f"{items_block}\n📝 Notes: {fulfillment.notes}" if fulfillment.notes else items_block
        summary_text = M8_PICKUP_SUMMARY.format(
            step_label=step_lbl,
            items_block=pickup_items,
            outlet_address=settings.outlet_address,
            subtotal=naira(subtotal),
            packaging_line=packaging_line,
            total=total_text,
        )
        confirm_buttons = [
            ("address:confirm", f"✅ Confirm & Pay ({total_text})"[:48]),
            ("address:edit", "📍 Edit delivery / pickup"),
            ("checkout:edit_items", "🛒 Edit Cart"),
            ("nav:back", "⬅ Go back"),
        ]
    else:
        area = await session.get(DeliveryArea, fulfillment.area_id) if fulfillment.area_id else None
        if area is not None:
            fee_text, total_text = naira(area.fee), naira(base_total + area.fee)
        else:  # "Other area": staff will quote the fee after the customer confirms
            fee_text, total_text = "to be confirmed", f"{naira(base_total)} + delivery fee"
        pin_line = "📌 GPS Pin: Shared ✅\n" if fulfillment.latitude is not None and fulfillment.longitude is not None else ""
        phone_line = f"📞 Second phone: {fulfillment.phone2}\n" if fulfillment.phone2 else ""
        summary_text = M8_SUMMARY.format(
            step_label=step_lbl,
            items_block=items_block,
            street=fulfillment.street or "",
            area=fulfillment.area_name or "",
            landmark=fulfillment.landmark or "",
            second_phone_line=f"{pin_line}{phone_line}",
            notes_line=f"📝 Notes: {fulfillment.notes}\n" if fulfillment.notes else "",
            subtotal=naira(subtotal),
            packaging_line=packaging_line,
            fee=fee_text,
            total=total_text,
        )
        if is_saved_mode and area is not None:
            short_addr = f"{(fulfillment.street or '')[:18]}, {(fulfillment.area_name or '')[:14]}".strip(" ,")
            confirm_buttons = [
                ("address:confirm", f"✅ Confirm & Pay {total_text} ({short_addr})"[:96]),
                ("checkout:switch_pickup", f"🏃 Switch to Pickup (Pay {naira(base_total)} · Save {naira(area.fee)})"[:96]),
                ("checkout:new_address", "📍 Deliver to a Different Address"),
                ("address:extras", "📝 Add Pin / Phone / Note"),
                ("checkout:edit_items", "🛒 Edit Cart"),
                ("nav:back", "⬅ Go back"),
            ]
        else:
            confirm_buttons = [
                ("address:confirm", f"✅ Confirm & Pay ({total_text})"[:48]),
                ("address:extras", "➕ Add Pin / Phone / Note"),
                ("address:edit", "📍 Edit delivery / pickup"),
                ("checkout:edit_items", "🛒 Edit Cart"),
                ("nav:back", "⬅ Go back"),
            ]

    await whatsapp.send_text(user.phone_number, summary_text)
    await whatsapp.send_buttons(
        user.phone_number,
        M8_CONFIRM_POLL.format(step_label=step_lbl, total=total_text)[:255],
        confirm_buttons,
    )


async def confirm_address(session: AsyncSession, user: User, fulfillment: Fulfillment) -> None:
    """Confirm the order & fulfillment details: set the fee for known areas/pickup, or request a quote for other areas."""
    # get_batch loads batch.orders up front. (Reading batch.orders on a batch fetched with
    # session.get() raises MissingGreenlet in async code.)
    batch = await order_service.get_batch(session, fulfillment.payment_batch_id)
    if batch is None:
        return
    now = datetime.now(timezone.utc)
    _USED_SAVED_SUMMARY.discard(user.phone_number)

    if fulfillment.address_editing:  # a change after checkout: the order stays exactly where it was
        fulfillment.address_editing = False
        fulfillment.address_edit_started_at = None
        fulfillment.checkout_step = None
        await save_address(session, user, fulfillment)
        user.current_state = (
            SessionState.AWAITING_APPROVAL if fulfillment.stage == FulfillmentStage.AWAITING_APPROVAL
            else SessionState.AWAITING_PAYMENT
        )
        await whatsapp.send_text(user.phone_number, "Your delivery address has been updated ✅")
        return

    subtotal = sum((order.subtotal for order in batch.orders), Decimal("0"))
    pack_fee = getattr(batch, "packaging_fee", Decimal("0")) or Decimal("0")
    base_total = subtotal + pack_fee

    if fulfillment.fulfillment_type == "pickup":
        batch.delivery_fee = Decimal("0")
        batch.total_amount = base_total
        fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
        fulfillment.timer_started_at = now
        fulfillment.reminders_sent = "[]"
        fulfillment.checkout_step = None
        user.current_state = SessionState.AWAITING_PAYMENT
        await send_payment_message(user, batch, fulfillment)
        return

    await save_address(session, user, fulfillment)
    area = await session.get(DeliveryArea, fulfillment.area_id) if fulfillment.area_id else None
    if area:
        batch.delivery_fee = area.fee
        batch.total_amount = base_total + area.fee
        fulfillment.expected_minutes = area.expected_minutes
        fulfillment.stage = FulfillmentStage.AWAITING_PAYMENT
        fulfillment.timer_started_at = now
        fulfillment.reminders_sent = "[]"
        fulfillment.checkout_step = None
        user.current_state = SessionState.AWAITING_PAYMENT
        await send_payment_message(user, batch, fulfillment)
    else:
        fulfillment.stage = FulfillmentStage.AWAITING_FEE_QUOTE
        fulfillment.timer_started_at = None  # paused while staff work out the fee
        fulfillment.checkout_step = None
        user.current_state = SessionState.AWAITING_FEE_QUOTE
        await whatsapp.send_text(user.phone_number, M8B_OTHER_AREA)


def build_map_link(fulfillment: Fulfillment) -> str:
    """Build a rider map link from a pin if present, or a text-search fallback."""
    if fulfillment.latitude is not None and fulfillment.longitude is not None:
        return f"https://www.google.com/maps?q={fulfillment.latitude},{fulfillment.longitude}"
    parts = [
        fulfillment.street or "",
        fulfillment.area_name or "",
        settings.maps_region_hint,
    ]
    query = ", ".join(p for p in parts if p).strip()
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"


async def dispatch_fulfillment(session: AsyncSession, batch_id: int, rider_id: int, *, resend: bool = False) -> dict:
    """Send a ready delivery to a rider. Raises DispatchError for problems staff can fix."""
    batch = await order_service.get_batch(session, batch_id)
    if batch is None:
        raise DispatchError("Order not found")
    fulfillment = batch.fulfillment
    if fulfillment is None or not batch.orders:
        raise DispatchError("This order has no delivery record")
    if fulfillment.fulfillment_type != "delivery":
        raise DispatchError("Pickup orders are not sent to riders")
    if resend and fulfillment.stage != FulfillmentStage.DISPATCHED:
        raise DispatchError("Only a dispatched delivery can be reassigned")
    if fulfillment.stage not in {FulfillmentStage.READY, FulfillmentStage.DISPATCHED}:
        raise DispatchError("Mark the order Ready before dispatching it")

    rider = await session.get(Rider, rider_id)
    if rider is None or rider.is_archived:
        raise DispatchError("Rider not found")
    now = datetime.now(timezone.utc)
    if (
        not rider.is_active
        or rider.last_message_at is None
        or now - rider.last_message_at > timedelta(hours=24)
    ):
        raise DispatchError("That rider isn't available. Ask them to send ACTIVE to the bot, then try again.")

    customer = batch.orders[0].user
    order_numbers = ", ".join(str(order.id) for order in batch.orders)
    n_items = sum(sum(item.quantity for item in order.items) for order in batch.orders)
    rider_notes = _extract_rider_notes(fulfillment.notes)
    rider_message = R1_DISPATCH.format(
        batch=batch.id,
        order_numbers=order_numbers,
        name=customer.name or customer.phone_number,
        phone=customer.phone_number,
        street=fulfillment.street or "",
        area=fulfillment.area_name or "",
        landmark=fulfillment.landmark or "",
        second_phone_line=f"Second phone: {fulfillment.phone2}\n" if fulfillment.phone2 else "",
        notes_line=f"Notes: {rider_notes}\n" if rider_notes else "",
        map_link=build_map_link(fulfillment),
        n=n_items,
        m=len(batch.orders),
    )
    sent = await whatsapp.send_buttons(
        rider.phone_number, rider_message,
        [(f"r:picked:{batch.id}", "Picked up"), (f"r:fail:{batch_id}", "Can't deliver")],
    )
    if not sent:
        fulfillment.dispatch_error = "WhatsApp could not deliver the message to the rider"
        return {"status": "failed", "batch_id": batch.id, "rider_id": rider.id, "error": fulfillment.dispatch_error}

    fulfillment.rider_id = rider.id
    fulfillment.dispatch_error = None
    fulfillment.stage = FulfillmentStage.DISPATCHED
    fulfillment.dispatched_at = now
    fulfillment.rider_reminder_sent_at = None
    if fulfillment.approved_at is None:
        fulfillment.approved_at = now
    return {"status": "dispatched", "batch_id": batch.id, "rider_id": rider.id}


async def send_payment_message(user: User, batch: PaymentBatch | None, fulfillment: Fulfillment) -> None:
    """Send the payment instructions and receipt collection message."""
    if batch is None:
        return
    if fulfillment.fulfillment_type == "pickup":
        body = M9_PICKUP.format(
            outlet_address=settings.outlet_address, total=naira(batch.total_amount),
            bank_name=settings.bank_name, account_number=settings.bank_account_number,
            account_name=settings.bank_account_name, minutes=settings.checkout_cancel_minutes
        )
    else:
        body = M9_PAYMENT.format(
            total=naira(batch.total_amount), bank_name=settings.bank_name,
            account_number=settings.bank_account_number, account_name=settings.bank_account_name,
            minutes=settings.checkout_cancel_minutes
        )
    await whatsapp.send_text(user.phone_number, body)
