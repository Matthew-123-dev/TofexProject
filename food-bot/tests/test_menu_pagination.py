"""Tests for the paginated category menu.

Covers handle_menu_request(session, user, page) and the "menupg:" branch of handle_incoming.

These tests need no database and send no real WhatsApp messages: the menu query, the
WhatsApp senders and the user lookup are replaced with small fakes (see the fixtures below).
They do not use the shared fixtures from conftest.py.
"""
from types import SimpleNamespace

import pytest

from app.models import FulfillmentStage, SessionState
from app.schemas import IncomingMessage
from app.services import state_machine

PHONE = "2348012345678"
INVALID_PAGE_TEXT = "That menu page is invalid. Send *menu* to start again."
WHATSAPP_MAX_ROWS = 10
WHATSAPP_MAX_TITLE = 24


# ---------------------------------------------------------------- fixtures and helpers


@pytest.fixture
def outbox(monkeypatch):
    """Record what the bot would send, instead of calling WhatsApp."""
    sent = SimpleNamespace(lists=[], texts=[], buttons=[])

    async def fake_send_list(to, body, button_label, section_title, rows):
        sent.lists.append(
            {"to": to, "body": body, "button_label": button_label, "section_title": section_title, "rows": rows}
        )
        return True

    async def fake_send_text(to, body):
        sent.texts.append(body)
        return True

    async def fake_send_buttons(to, body, buttons):
        sent.buttons.append(buttons)
        return True

    monkeypatch.setattr(state_machine.whatsapp, "send_list", fake_send_list)
    monkeypatch.setattr(state_machine.whatsapp, "send_text", fake_send_text)
    monkeypatch.setattr(state_machine.whatsapp, "send_buttons", fake_send_buttons)
    return sent


@pytest.fixture
def set_categories(monkeypatch):
    """Call set_categories([...names...]) to choose what menu_service.get_categories returns."""

    def _set(categories):
        async def fake_get_categories(session):
            return list(categories)

        monkeypatch.setattr(state_machine.menu_service, "get_categories", fake_get_categories)

    return _set


@pytest.fixture
def menu_user():
    """A stand-in customer. handle_menu_request only reads phone_number and sets current_state."""
    return SimpleNamespace(id=1, phone_number=PHONE, current_state=SessionState.ADDING_TO_CART)


@pytest.fixture
def route(monkeypatch, menu_user):
    """Make handle_incoming treat PHONE as a normal customer, with no database.

    Set route.open_fulfillment to simulate a customer who has an order open.
    """
    state = SimpleNamespace(open_fulfillment=None)

    async def fake_rider(session, phone):
        return None

    async def fake_user(session, phone):
        return menu_user

    async def fake_open_fulfillment(session, user_id):
        return state.open_fulfillment

    monkeypatch.setattr(state_machine, "get_rider_by_phone", fake_rider)
    monkeypatch.setattr(state_machine, "get_or_create_user", fake_user)
    monkeypatch.setattr(state_machine, "get_open_fulfillment", fake_open_fulfillment)
    return state


@pytest.fixture
def spies(monkeypatch):
    """Replace the handlers handle_incoming dispatches to and record how they were called."""
    calls = SimpleNamespace(menu=[], category=[], checkout=[])

    async def fake_menu(session, user, page=0):
        calls.menu.append(page)

    async def fake_category(session, user, category, page=0):
        calls.category.append((category, page))

    async def fake_checkout(session, user, fulfillment, msg):
        calls.checkout.append(msg.reply_id)

    monkeypatch.setattr(state_machine, "handle_menu_request", fake_menu)
    monkeypatch.setattr(state_machine, "handle_category_selected", fake_category)
    monkeypatch.setattr(state_machine, "handle_checkout_flow", fake_checkout)
    return calls


def make_categories(count):
    """Category names that already sort in the order menu_service.get_categories returns them."""
    return [f"Category {n:02d}" for n in range(1, count + 1)]


def tap(reply_id):
    """A customer tapping a list row or button."""
    return IncomingMessage(id="wamid.test", sender=PHONE, type="interactive", reply_id=reply_id)


def say(text):
    """A customer typing a text message."""
    return IncomingMessage(id="wamid.test", sender=PHONE, type="text", text=text)


def cat_ids(rows):
    return [row["id"] for row in rows if row["id"].startswith("cat:")]


def more_rows(rows):
    return [row for row in rows if row["id"].startswith("menupg:")]


# ---------------------------------------------------------------- handle_menu_request


@pytest.mark.asyncio
async def test_menu_first_page_has_nine_categories_and_more_row(outbox, set_categories, menu_user):
    categories = make_categories(12)
    set_categories(categories)

    await state_machine.handle_menu_request(None, menu_user, 0)

    rows = outbox.lists[0]["rows"]
    assert len(rows) == 10
    assert cat_ids(rows) == [f"cat:{name}" for name in categories[:9]]
    assert rows[-1] == {"id": "menupg:1", "title": "More categories"}


@pytest.mark.asyncio
async def test_menu_last_page_has_the_rest_and_no_more_row(outbox, set_categories, menu_user):
    categories = make_categories(12)
    set_categories(categories)

    await state_machine.handle_menu_request(None, menu_user, 1)

    rows = outbox.lists[0]["rows"]
    assert cat_ids(rows) == [f"cat:{name}" for name in categories[9:]]
    assert len(rows) == 3
    assert more_rows(rows) == []


@pytest.mark.asyncio
async def test_menu_default_page_is_the_first_page(outbox, set_categories, menu_user):
    set_categories(make_categories(12))

    await state_machine.handle_menu_request(None, menu_user)
    await state_machine.handle_menu_request(None, menu_user, 0)

    assert outbox.lists[0]["rows"] == outbox.lists[1]["rows"]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 8, 9])
async def test_menu_nine_or_fewer_categories_have_no_more_row(outbox, set_categories, menu_user, count):
    categories = make_categories(count)
    set_categories(categories)

    await state_machine.handle_menu_request(None, menu_user, 0)

    rows = outbox.lists[0]["rows"]
    assert cat_ids(rows) == [f"cat:{name}" for name in categories]
    assert more_rows(rows) == []


@pytest.mark.asyncio
async def test_menu_exactly_ten_categories_needs_a_second_page(outbox, set_categories, menu_user):
    # Boundary case: 10 is one more than a page holds (9), so the 10th goes to page 2.
    categories = make_categories(10)
    set_categories(categories)

    await state_machine.handle_menu_request(None, menu_user, 0)
    await state_machine.handle_menu_request(None, menu_user, 1)

    first, second = outbox.lists[0]["rows"], outbox.lists[1]["rows"]
    assert len(cat_ids(first)) == 9
    assert first[-1]["id"] == "menupg:1"
    assert cat_ids(second) == [f"cat:{categories[9]}"]
    assert more_rows(second) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 9, 10, 12, 18, 19, 27, 28, 50])
async def test_menu_every_category_is_reachable(outbox, set_categories, menu_user, count):
    categories = make_categories(count)
    set_categories(categories)

    reached = []
    page = 0
    while True:
        outbox.lists.clear()
        await state_machine.handle_menu_request(None, menu_user, page)
        rows = outbox.lists[0]["rows"]

        assert 1 <= len(rows) <= WHATSAPP_MAX_ROWS
        reached += cat_ids(rows)

        more = more_rows(rows)
        if not more:
            break
        assert len(more) == 1
        assert rows[-1] == more[0]  # "More categories" is always the final row
        assert more[0]["id"] == f"menupg:{page + 1}"
        page += 1
        assert page < 20, "paging never ended"

    assert reached == [f"cat:{name}" for name in categories]  # all of them, in order, none twice


@pytest.mark.asyncio
async def test_menu_row_titles_fit_the_whatsapp_limit(outbox, set_categories, menu_user):
    categories = [f"{n:02d} An extremely long category name that overflows" for n in range(1, 13)]
    set_categories(categories)

    for page in (0, 1):
        await state_machine.handle_menu_request(None, menu_user, page)

    for sent in outbox.lists:
        for row in sent["rows"]:
            assert len(row["title"]) <= WHATSAPP_MAX_TITLE
    # the title is shortened, but the id still carries the full name so the tap can be matched
    assert outbox.lists[0]["rows"][0]["id"] == f"cat:{categories[0]}"
    assert outbox.lists[0]["rows"][0]["title"] == categories[0][:WHATSAPP_MAX_TITLE]


@pytest.mark.asyncio
async def test_menu_category_ids_keep_the_cat_prefix_and_full_name(outbox, set_categories, menu_user):
    categories = ["Rice: Jollof & Fried", "Drinks (cold)", "Swallow"]
    set_categories(categories)

    await state_machine.handle_menu_request(None, menu_user)

    assert cat_ids(outbox.lists[0]["rows"]) == [f"cat:{name}" for name in categories]


@pytest.mark.asyncio
async def test_menu_out_of_range_page_is_clamped(outbox, set_categories, menu_user):
    set_categories(make_categories(12))

    await state_machine.handle_menu_request(None, menu_user, 1)
    await state_machine.handle_menu_request(None, menu_user, 99)
    await state_machine.handle_menu_request(None, menu_user, 0)
    await state_machine.handle_menu_request(None, menu_user, -5)

    last, too_high, first, too_low = (sent["rows"] for sent in outbox.lists)
    assert too_high == last
    assert too_low == first


@pytest.mark.asyncio
async def test_menu_request_puts_the_customer_in_browsing_state(outbox, set_categories, menu_user):
    set_categories(make_categories(12))
    assert menu_user.current_state == SessionState.ADDING_TO_CART

    await state_machine.handle_menu_request(None, menu_user, 1)

    assert menu_user.current_state == SessionState.BROWSING


@pytest.mark.asyncio
async def test_menu_list_message_wrapper_is_unchanged(outbox, set_categories, menu_user):
    set_categories(make_categories(12))

    await state_machine.handle_menu_request(None, menu_user, 0)

    sent = outbox.lists[0]
    assert sent["to"] == PHONE
    assert sent["body"] == "What would you like to order?"
    assert sent["button_label"] == "View menu"
    assert sent["section_title"] == "Categories"


@pytest.mark.asyncio
async def test_menu_with_no_categories_sends_text_and_no_list(outbox, set_categories, menu_user):
    set_categories([])

    await state_machine.handle_menu_request(None, menu_user, 0)

    assert outbox.lists == []
    assert len(outbox.texts) == 1
    assert "nothing is available" in outbox.texts[0]


# ---------------------------------------------------------------- handle_incoming routing


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply_id, expected_page",
    [("menupg:0", 0), ("menupg:1", 1), ("menupg:12", 12), ("menupg:01", 1)],
)
async def test_menupg_route_calls_the_menu_with_that_page(route, spies, outbox, reply_id, expected_page):
    await state_machine.handle_incoming(None, tap(reply_id))

    assert spies.menu == [expected_page]
    assert spies.category == []  # it must not be mistaken for an item/category tap
    assert outbox.texts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply_id",
    ["menupg:", "menupg:abc", "menupg:-1", "menupg:1.5", "menupg: 1", "menupg:1 ", "menupg:1:2"],
)
async def test_menupg_route_rejects_a_page_that_is_not_digits(route, spies, outbox, reply_id):
    await state_machine.handle_incoming(None, tap(reply_id))

    assert spies.menu == []
    assert spies.category == []
    assert outbox.texts == [INVALID_PAGE_TEXT]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_id", ["menupg:²", "menupg:①"])
async def test_menupg_route_rejects_digit_lookalike_characters(route, spies, outbox, reply_id):
    await state_machine.handle_incoming(None, tap(reply_id))

    assert spies.menu == []
    assert outbox.texts == [INVALID_PAGE_TEXT]


@pytest.mark.asyncio
async def test_menupg_route_shows_the_next_page_of_categories(route, outbox, set_categories):
    categories = make_categories(12)
    set_categories(categories)

    await state_machine.handle_incoming(None, tap("menupg:1"))

    rows = outbox.lists[0]["rows"]
    assert cat_ids(rows) == [f"cat:{name}" for name in categories[9:]]
    assert more_rows(rows) == []


@pytest.mark.asyncio
async def test_menupg_end_to_end_with_twelve_categories(route, outbox, set_categories):
    """The scenario from the task: type 'menu', then follow 'More categories' until the end."""
    categories = make_categories(12)
    set_categories(categories)

    await state_machine.handle_incoming(None, say("menu"))
    reached = []
    for _ in range(5):
        rows = outbox.lists[-1]["rows"]
        reached += cat_ids(rows)
        more = more_rows(rows)
        if not more:
            break
        await state_machine.handle_incoming(None, tap(more[0]["id"]))

    assert reached == [f"cat:{name}" for name in categories]
    assert len(outbox.lists) == 2  # page 1 and page 2, nothing extra


@pytest.mark.asyncio
@pytest.mark.parametrize("make_message", [lambda: say("hi"), lambda: say("Menu"), lambda: tap("menu")])
async def test_menu_greetings_and_the_menu_button_show_the_first_page(route, outbox, set_categories, make_message):
    categories = make_categories(12)
    set_categories(categories)

    await state_machine.handle_incoming(None, make_message())

    rows = outbox.lists[0]["rows"]
    assert cat_ids(rows) == [f"cat:{name}" for name in categories[:9]]
    assert rows[-1]["id"] == "menupg:1"


@pytest.mark.asyncio
async def test_catpg_route_still_pages_items_not_categories(route, spies):
    await state_machine.handle_incoming(None, tap("catpg:Burgers & Fries:2"))

    assert spies.category == [("Burgers & Fries", 2)]
    assert spies.menu == []


@pytest.mark.asyncio
async def test_cat_route_still_opens_a_category(route, spies):
    await state_machine.handle_incoming(None, tap("cat:Burgers"))

    assert spies.category == [("Burgers", 0)]
    assert spies.menu == []


@pytest.mark.asyncio
async def test_menupg_is_handled_by_checkout_while_details_are_being_collected(route, spies):
    route.open_fulfillment = SimpleNamespace(stage=FulfillmentStage.COLLECTING_DETAILS, address_editing=False)

    await state_machine.handle_incoming(None, tap("menupg:1"))

    assert spies.checkout == ["menupg:1"]
    assert spies.menu == []


@pytest.mark.asyncio
async def test_menupg_works_when_the_open_order_is_waiting_for_payment(route, spies):
    route.open_fulfillment = SimpleNamespace(stage=FulfillmentStage.AWAITING_PAYMENT, address_editing=False)

    await state_machine.handle_incoming(None, tap("menupg:1"))

    assert spies.menu == [1]
    assert spies.checkout == []
