# Prompts for the remaining issues

Use **one prompt per session**, in the order below. Before each one run `git add -A` then `git commit -m "before <task>"`, and afterwards read `git diff`. `git checkout .` undoes a bad result.

## Paste this at the start of EVERY prompt

```
You are editing an existing FastAPI + async SQLAlchemy WhatsApp food-ordering bot (project folder: food-bot).
Rules:
- Only edit the files I name. Never open or print .env.
- Do not add dependencies unless I name them.
- Keep the existing code style. Do not rename functions or change message wording unless I say so.
- In async SQLAlchemy code NEVER read a relationship (batch.orders, user.saved_address, ...) on an object loaded with
  session.get(): it raises MissingGreenlet. Load it explicitly with select(...) / selectinload, or use order_service.get_batch().
- Never mark an order failed, cancelled or paid without checking its current stage first.
- When you finish, return ONLY the changed or new files in full, then a plain-language list of what you changed in each file and why
  (I am learning), then the exact commands I must run (migration, if any) and the manual test steps.
```

## Order and priority

| # | Task | Files | Priority |
|---|---|---|---|
| 1 | Automated tests for the critical paths | new `tests/`, new `requirements-dev.txt` | Do first: it proves the fixes work |
| 2 | Save the receipt image | `app/models.py`, new migration, `app/services/state_machine.py`, `app/api/dashboard.py` | High |
| 3 | Saved addresses (reuse) | `app/services/state_machine.py` | Medium |
| 4 | Paginate categories (more than 10) | `app/services/state_machine.py` | Medium |
| 5 | Expected minutes on "Other area" quotes | `app/api/dashboard.py`, `app/dashboard.html` | Low |
| 6 | Reserve stock at checkout | several | Optional: skip unless you actually see overselling |

---

## Prompt 1: Automated tests

```
You are editing an existing FastAPI + async SQLAlchemy WhatsApp food-ordering bot (project folder: food-bot).
Rules:
- Only edit the files I name. Never open or print .env.
- Do not add dependencies unless I name them.
- Keep the existing code style. Do not rename functions or change message wording unless I say so.
- In async SQLAlchemy code NEVER read a relationship (batch.orders, user.saved_address, ...) on an object loaded with
  session.get(): it raises MissingGreenlet. Load it explicitly with select(...) / selectinload, or use order_service.get_batch().
- Never mark an order failed, cancelled or paid without checking its current stage first.
- When you finish, return ONLY the changed or new files in full, then a plain-language list of what you changed in each file and why
  (I am learning), then the exact commands I must run (migration, if any) and the manual test steps.
Add automated tests so we can detect regressions. Files to create: requirements-dev.txt (pytest, pytest-asyncio, httpx), tests/conftest.py,
tests/test_scheduler.py, tests/test_checkout.py, tests/test_riders.py, tests/test_dashboard.py, tests/test_misc.py. Do not edit files in app/.

Setup:
- Use an in-memory SQLite database (set DATABASE_URL=sqlite+aiosqlite:///:memory: and STAFF_USERNAME/STAFF_PASSWORD/SESSION_SECRET in the test
  environment BEFORE importing app modules). Create tables with Base.metadata.create_all in a fixture.
- Replace the WhatsApp functions (send_text, send_buttons, send_list, send_location_request) with a fake that records (to, text, buttons)
  and returns True. Never call the network.
- Build IncomingMessage objects to drive state_machine.handle_incoming. Seed a user, two menu items (one with stock_quantity=5, threshold 1),
  one delivery area (fee 1500, 40 minutes), and one rider.

Tests to write (each one is a regression test for a bug that existed):
1. Scheduler: a fulfillment in AWAITING_APPROVAL whose timer_started_at is 2 hours old is NOT cancelled. One in AWAITING_PAYMENT that is
   31 minutes old IS cancelled exactly once (a second scheduler run does nothing and sends no second message). Reminders go out once at 10
   and once at 20 minutes. AWAITING_FEE_QUOTE never times out.
2. Checkout: full delivery flow through handle_incoming (menu, add item, checkout, Delivery, area, street, skip pin, landmark, skip, skip).
   The summary message shows the real delivery fee and total (not 0). Tapping Confirm does not raise and moves the stage to AWAITING_PAYMENT
   with total = subtotal + fee. A second Checkout tap does NOT create a second batch. Sending "menu" while awaiting payment and then a receipt
   image still stores the receipt, moves to AWAITING_APPROVAL, and sets timer_started_at to None.
3. Dashboard (use httpx.AsyncClient with ASGITransport and log in first): approving an order before the receipt returns 409; approving after
   the receipt returns 200, sets the batch PAID, and decrements stock; rejecting returns the stage to AWAITING_PAYMENT while the batch stays
   pending; deleting a paid order returns 409; an unauthenticated /api/staff/orders request returns 401.
4. Riders: dispatch to a rider who never messaged the bot is refused; after the rider sends "active" dispatch succeeds and the rider message
   contains the street but NO item names and NO prices. "picked N" (and the Picked up button id r:picked:N) sets OUT_FOR_DELIVERY and sends the
   customer the on-its-way message. Delivered then confirm sets DELIVERED and sends NO customer message until the scheduler runs after the delay.
   Undo works before the thank-you is sent and is refused after. A failure with reasons "1,3" confirmed with the failyes button sets FAILED,
   keeps the batch PAID, and the customer message contains the real item names.
5. Misc: running the seed twice leaves edited prices and stock unchanged; an unsigned POST to /webhook/whatsapp is rejected with 403 when
   DRY_RUN is false.

If a test fails because of a bug in the app code, do NOT change the test to make it pass. Report the failing test, the cause, and the smallest
fix, and wait for my answer.
```

## Prompt 2: Save the receipt image

```
Receipt photos are currently stored only as a WhatsApp media id (payment_batches.receipt_media_id) and fetched live each time staff open them.
WhatsApp does not keep media forever. Store the image itself.

Files to edit: app/models.py, a NEW Alembic migration in alembic/versions/, app/services/state_machine.py, app/api/dashboard.py.

1. Model: add class ReceiptImage in app/models.py, table "receipt_images": id, payment_batch_id (FK payment_batches.id, ondelete CASCADE, indexed),
   media_id String(200), content LargeBinary, mime_type String(100), size_bytes Integer, created_at (same UTCDateTime pattern as the other models).
   Use a SEPARATE table, not a column on PaymentBatch, because get_batch() loads batches everywhere and must not load image bytes.
2. Migration: create the table. down_revision must be the current head: run `alembic heads` first (it should be "20261006_delivery_foundation").
   Give me the exact commands for a database managed by Alembic. Warn me if the database was created with create_all.
3. state_machine.py, handle_checkout_flow, the branch where a photo arrives in AWAITING_PAYMENT: after storing batch.receipt_media_id, call
   whatsapp.download_media(msg.media_id) (returns (bytes, mime)). If it returns non-empty bytes of at most 5 MB and the mime starts with "image/",
   add a ReceiptImage row. If the download fails or is empty, log a warning but STILL accept the receipt and continue exactly as today.
   Never let a download problem block the customer.
4. dashboard.py, get_receipt: first look for the newest ReceiptImage for the batch and return it. Only if there is none, fall back to the current
   live download. Add headers X-Content-Type-Options: nosniff and Cache-Control: private, max-age=3600. Refuse to serve any mime that does not start with "image/".
5. Rejecting a receipt must keep the old ReceiptImage rows (history).
Test: send a receipt, open "View receipt" in the dashboard, restart the app, open it again (it must still work without WhatsApp), then reject
and resend and confirm the newest image is the one shown.
```

## Prompt 3: Saved addresses (reuse the last address)

```
Returning customers should be able to reuse their last delivery address. The table already exists: SavedAddress in app/models.py
(one row per user: user_id unique, street, area_id, area_name, landmark, phone2, notes, latitude, longitude). No migration is needed.
File to edit: app/services/state_machine.py only.

1. In process_address_step, step "choice", when the customer taps fulfillment:delivery: look up their SavedAddress with an explicit
   select(SavedAddress).where(SavedAddress.user_id == user.id) (never user.saved_address). If one exists AND its area_id points to a DeliveryArea that is
   still active, set fulfillment.checkout_step = "saved_choice" and send buttons: text showing "street, area" and landmark, buttons
   saved:use "Use this address" and saved:new "New address". Otherwise continue exactly as today (area list).
2. Add step "saved_choice" to process_address_step: saved:use copies street, area_id, area_name, landmark, phone2, notes, latitude, longitude into the
   fulfillment, sets expected_minutes from the area, then calls confirm_address(...) (which sets the CURRENT fee and sends the payment message).
   saved:new starts the normal flow at the area step. Any other input resends the two buttons.
3. Add a helper save_address(session, user, fulfillment) that inserts or updates the user's single SavedAddress row from the fulfillment (only when
   fulfillment_type == "delivery" and street is set). Call it in confirm_address after a normal confirmation (both the known-area and the
   other-area branches) and in the address_editing branch.
4. Add a SHORT note in your answer about what happens if the saved area was disabled (it must fall back to the normal flow).
Test: first order saves the address; second order offers it; "Use this address" goes straight to the payment message with the correct fee; change the area's fee
in the dashboard and the next reuse shows the new fee; disable the area and the offer disappears.
```

## Prompt 4: More than 10 categories

```
WhatsApp list messages hold at most 10 rows. state_machine.handle_menu_request sends every category in one list, so categories after the 10th are silently
unreachable. Items are already paginated; categories are not.
File to edit: app/services/state_machine.py only.

1. Change handle_menu_request(session, user) to handle_menu_request(session, user, page: int = 0). Show 9 categories per page. If more remain, add a final row
   with id "menupg:{page+1}" and title "More categories". Follow the pattern used by send_area_page.
2. In handle_incoming add a branch for rid starting with "menupg:" (validate that the rest is digits) that calls handle_menu_request with that page.
   Do not reuse the existing "catpg:" prefix (that one pages ITEMS inside a category).
3. Keep row titles at 24 characters or fewer. Keep the existing ids "cat:{name}" for the category rows.
Test with 12 categories: every category must be reachable, and "More categories" must lead to the rest.
```

## Prompt 5: Expected minutes on "Other area" quotes

```
When a customer types a custom area, staff send a fee quote, but no expected delivery time is recorded, so the Overdue badge never works for those orders.
Files to edit: app/api/dashboard.py and app/dashboard.html.

1. dashboard.py: in QuoteInput add expected_minutes: int = Field(ge=1, le=1440). In quote_order set fulfillment.expected_minutes = payload.expected_minutes.
2. dashboard.html: in the quoteModal function add a required number input "Expected delivery minutes" (id qMinutes, min 1, max 1440), prefill it with
   f.expected_minutes when editing a quote, and send expected_minutes in the POST body. Escape any user text you render.
3. Do not change anything else in the page. The page must still load with no errors in the browser console.
```

## Prompt 6 (optional): Reserve stock at checkout

Only do this if you actually see two customers buying the last portion. It touches many paths, so give it to a strong model, not a small one.

```
Today stock is decremented only when staff approve an order, so two customers can check out the last portion. Reserve stock at checkout instead.
Files to edit: app/services/order_service.py, app/services/menu_service.py, app/services/scheduler.py, app/services/state_machine.py, app/api/dashboard.py.

1. In order_service.create_orders_from_carts, after the existing _check_stock call, decrement stock_quantity for tracked items by the ordered quantity
   (inside the same transaction as the order creation).
2. Remove the decrement from the approval path (dashboard.approve_order). Keep the low-stock hiding logic and the low-stock messages.
3. Restore the stock when an order is cancelled before approval: state_machine.cancel_checkout and the scheduler's automatic cancel. Restoring must NOT
   re-enable an item that staff hid on purpose: only set is_available back to True if stock_quantity is above low_stock_threshold AND the item was
   hidden automatically. If you cannot tell the difference, add a boolean column auto_hidden to MenuItem (with a migration) and use it.
4. Failed deliveries do not restore stock (the food was prepared).
5. Add tests for: cancel restores stock; approval no longer decrements; two checkouts for the last item: the second one is refused.
```

---

## Manual steps (no prompt needed)

- Delete `init_db.py` in the project ROOT (keep `scripts/init_db.py`).
- Add `uploads/` and `venv/` to `.gitignore` if they are not there.

## Launch checklist (Railway)

1. Push the project to GitHub (no `.env`, no `.db`).
2. Railway: new project from the repo, then add a **PostgreSQL** service. Link its `DATABASE_URL` to the web service.
3. Set these variables on the web service: `APP_ENV=production`, `DRY_RUN=false`, `STAFF_USERNAME`, `STAFF_PASSWORD` (10+ characters), `SESSION_SECRET` (32+ random characters),
   `WHATSAPP_ACCESS_TOKEN` (permanent System User token), `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_API_VERSION`, your bank details,
   `DELIVERY_FEE`, `BRAND_NAME`, `OUTLET_ADDRESS`, `OUTLET_PHONE`, `SUPPORT_PHONE`, `REFUND_TIMEFRAME`, `MAPS_REGION_HINT`.
4. The Procfile already runs `alembic upgrade head` before starting. After the first deploy, open the Railway shell once and run `python -m app.seed` (safe: it only fills an empty menu).
5. Log in to the dashboard, add your delivery areas and riders.
6. In Meta: set the webhook callback URL to `https://<your-railway-domain>/webhook/whatsapp` with the same verify token, and subscribe to `messages`.
7. Have each rider message the bot `active`. Place a real test order end to end.
8. Turn on Railway's database backups (or schedule a nightly `pg_dump`), and start Meta business verification now.
