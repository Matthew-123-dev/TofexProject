# Agent prompt: delivery addresses, riders and fulfillment tracking

You are extending an existing FastAPI + async SQLAlchemy WhatsApp food-ordering bot that has a staff dashboard. Implement the feature set below. **Read the current code first** and adapt every name in this prompt to what actually exists. Some names here are examples.

---

## 0. How to work

1. **Work in the phases in section 12.** Finish one phase completely (app starts, migration applies, manual test passes) before starting the next. After each phase, summarize what changed, give the manual test steps, then **stop and wait for the user to confirm** before continuing.
2. **Back up first.** Tell the user to run `cp foodbot.db foodbot.db.bak` before any migration. Never run destructive commands against their database yourself.
3. **The user is learning.** For every file you change, explain in plain language what changed and why. Return only changed or new files, in full.
4. Keep all existing features working: menu management and archiving, order history and pagination, WhatsApp cart flow, stock decrement on approval, approve/reject.
5. Do not change unrelated wording, models or flows.

## 1. Existing system (verify by reading the code)

- Customer flow lives in `app/services/state_machine.py`: menu, carts (`Cart`, `CartItem`, `is_selected`), checkout creates a `PaymentBatch` containing one or more `Order`s (`OrderItem` stores `item_name` and `unit_price` snapshots). Routing uses `user.current_state` and interactive reply ids.
- WhatsApp client: `app/services/whatsapp.py` (`send_text`, `send_buttons`, `send_list`, `download_media`, `parse_incoming`). Note: `_send` currently logs failures and does not raise. You will change this (section 9).
- Dashboard: `app/dashboard.html` (single file, vanilla JS, inline CSS with CSS variables, maroon/cream theme) and `app/api/dashboard.py`.
- Approve/reject currently live in the dashboard API, and approval decrements stock for tracked items.
- Idempotency: `ProcessedMessage` table. Config: `app/config.py` (pydantic-settings, `.env`). Dev mode: `DRY_RUN=true` captures outgoing messages at `/dev/outbox`; `scripts/simulate.py` is a terminal chat simulator. Keep DRY_RUN forbidden in production.
- Migrations: Alembic (`render_as_batch` for SQLite). `scripts/init_db.py` uses `create_all`, which does not alter existing tables.
- Database: SQLite in development, PostgreSQL in production. Money is `Decimal`/`Numeric`.

## 2. Product decisions (final, from the owner)

**Checkout order is now: fulfillment choice -> address -> fee -> payment -> receipt -> staff approval.** The address is collected BEFORE payment because delivery fees depend on the area.

- Customers choose **Delivery** or **Pickup**. Pickup is at the outlet only, with no fee.
- Delivery fee is set per **delivery area** (a staff-managed list of area name, fee, expected delivery minutes). Customers choose their area from a list and may then optionally share a **location pin**. An **"Other area"** option sends a fee request to staff.
- All carts in one payment batch go to one address and one rider trip.
- Riders are several. **Staff choose which rider** gets each delivery. Only **active** riders can be chosen. Riders control their own active/inactive state over WhatsApp.
- Failed deliveries are never retried. They close as failed.
- Rider sees no item names and no prices, only an item count.
- All customer and rider text is English, professional but warm.

## 3. Customer flow

### 3.1 Starting checkout
When the customer taps Checkout with at least one selected cart, create the `PaymentBatch` and orders as today, plus a fulfillment record (section 8) in stage `collecting_details` with `delivery_fee = 0` and `total = subtotal` until the fee is known. Stock is still only decremented on approval.

A customer may have only one open checkout (stages `collecting_details`, `awaiting_fee_quote`, `awaiting_fee_accept`, `awaiting_payment`, `awaiting_approval`) at a time. If they start another, tell them to finish it or send `cancel`.

### 3.2 Step 1: fulfillment choice
Buttons: **Delivery**, **Pickup**, **Cancel order**.

- **Pickup:** type=pickup, fee 0, stage `awaiting_payment`, send the payment message (M9-pickup) which includes the outlet address.
- **Delivery with a saved address:** show it with buttons **Use this address** / **New address**. "Use" copies it to the fulfillment, recomputes the fee from the area's CURRENT fee (if that area was disabled or deleted, treat it as "New address"), then goes to payment (M9). Each customer has at most one saved address, overwritten whenever they confirm a new one.
- **Delivery without a saved address:** run the address steps below.

### 3.3 Address steps (delivery)
In this order, one question at a time:

1. **Street** (text, required, 5 to 150 chars, include house or plot number).
2. **Area**: WhatsApp list of active areas. Rows hold at most 10, so show up to 8 areas per page, then a **More areas** row if more exist, and **Other area** as the last row. Row title = area name (max 24 chars), description = `₦fee · ~N min`. If **Other area**: ask the customer to type their area name; the fulfillment will need a staff quote.
3. **Location pin (optional):** send a `location_request_message` (Send location button), followed by a separate message with a **Skip** button (a location request message cannot hold a Skip button). Accept the word "skip" as well. Store latitude/longitude from the incoming `location` message.
4. **Landmark** (text, required, max 100 chars).
5. **Second phone number** (optional, **Skip** button). Validate and normalize Nigerian numbers (accept `080...`, `+234...`, `234...`). On invalid input, explain and ask again.
6. **Delivery notes** (optional, **Skip** button, max 200 chars).
7. **Summary** showing address, landmark, second phone, notes, order subtotal, delivery fee, total, with buttons **Confirm** / **Edit**. **Edit restarts the address questions.** Keep every message body under 1,024 characters (WhatsApp limit for interactive bodies).

On **Confirm**:
- Known area: set `delivery_fee`, `total_amount`, `expected_minutes` from the area; save/overwrite the customer's saved address; stage `awaiting_payment`; send payment message M9. The 30-minute timer keeps running from checkout start (see 4).
- Other area: save the address, stage `awaiting_fee_quote`, send M8b. The timer is **paused**. A dashboard alert "Fee quote needed" appears. When staff enter a fee and expected minutes, set the fee and total, stage `awaiting_fee_accept`, **restart the timer** (fresh 30 minutes, reminders reset), and send M8c with buttons **Proceed to pay** / **Cancel order**. Proceed moves to `awaiting_payment` and sends M9.

### 3.4 Locked-in behavior while collecting details
During `collecting_details`, `awaiting_fee_quote`, `awaiting_fee_accept`: the only exit is `cancel` (or the Cancel buttons). Any unrelated text or photo gets the current question repeated. In `awaiting_payment`: a photo is the receipt; other text gets a gentle reminder listing `cancel` and `address`.

### 3.5 Receipt
A photo in `awaiting_payment` stores the media id (existing behavior), moves to `awaiting_approval`, stops the timer, and sends M10. The dashboard raises a "new order to approve" alert.

### 3.6 Changing the address (`ADDRESS`)
- **Before the receipt is sent** (`awaiting_payment`): the customer may re-enter everything, including the area. Recompute the fee.
- **After the receipt, before approval** (`awaiting_approval`): allowed only **within the same area**. Skip the area step, keep the fee unchanged, and mention the rule (M15b). While the customer is mid-edit, set `address_editing = true`. **Approve is disabled while this is true.** If the edit is abandoned for 10 minutes, restore the previous address and set it false.
- **After approval:** refuse with M15.

### 3.7 Rejected receipt
Staff reject -> the batch is **not** cancelled. Clear the receipt, stage back to `awaiting_payment`, restart the 30-minute timer (reminders reset), keep the saved address, send M12. The customer may send a new receipt.

### 3.8 Cancelling
`cancel` in any pre-approval stage sets the batch status to cancelled and stage `cancelled`. After approval, the bot cannot cancel (tell them to call support).

## 4. Timers (background scheduler)

Add a small background loop started in the FastAPI lifespan (check every 30 seconds, wrapped in try/except with logging). Make every action idempotent using DB flags/counters so a duplicate run never double-sends.

| Timer | Rule |
|---|---|
| Customer reminder | Stages `collecting_details`, `awaiting_fee_accept`, `awaiting_payment`: reminders at 10 and 20 minutes after `timer_started_at`; message M13 states that the order will be cancelled automatically. |
| Customer auto-cancel | 30 minutes after `timer_started_at` -> cancel, send M14. `timer_started_at` is checkout start; it restarts after a rejected receipt and after a fee quote is sent. Paused during `awaiting_fee_quote`. |
| Address-edit timeout | 10 minutes of `address_editing` with no progress -> revert. |
| Thank-you | 10 minutes after `delivered_at` (if not undone) send M18. |
| Rider pickup reminder | 10 minutes after dispatch with no "Picked up" -> remind the rider once (R4). |
| Rider window warning | 23 hours after the rider's last message, if still marked active -> send R7 once. |
| Overdue flag | Computed at read time: stage in preparing/ready/dispatched/out_for_delivery and minutes since `approved_at` > `expected_minutes + 10` (delivery only). |

All durations come from settings with the defaults in section 11.

## 5. Fulfillment lifecycle

Stages (enum values lowercase): `collecting_details`, `awaiting_fee_quote`, `awaiting_fee_accept`, `awaiting_payment`, `awaiting_approval`, `preparing`, `ready`, `dispatched`, `out_for_delivery`, `delivered`, `collected`, `failed`, `cancelled`. Keep the existing `PaymentBatch.status` (pending/paid/cancelled): approval sets it to paid, cancellation to cancelled.

**Delivery path**
1. Staff **Approve** (requires `awaiting_approval` and not `address_editing`) -> `preparing`, set `approved_at`, decrement stock (existing), send M11.
2. Staff **Mark ready** -> `ready` (no message to the customer).
3. Staff choose a rider from a dropdown of **currently active** riders and press **Dispatch** -> the rider gets R1. Move to `dispatched` **only if WhatsApp accepts the message**. If it fails, stay in `ready`, record `dispatch_error`, and show a red warning with a **Resend** button.
4. Rider taps **Picked up** -> `out_for_delivery`, `picked_up_at`; the customer gets M17 (with the rider's name and phone).
5. Rider taps **Delivered** and confirms -> `delivered`, `delivered_at`, `thank_you_due_at = now + 10 min`. The rider may **Undo** until the thank-you is sent, which restores `out_for_delivery` and cancels the thank-you. The delivered stage is never set by staff by hand.
6. After 10 minutes the scheduler sends M18.

**Reassign:** while `dispatched` (not yet picked up) staff may pick a different active rider. The old rider is told it was reassigned (R6), the new rider gets R1, and the pickup reminder timer resets.

**Failure:** from `ready`, `dispatched` or `out_for_delivery`, either the rider (see 6.4) or staff (dashboard **Mark failed** with a reasons checklist and a confirm step) can fail the delivery. Reasons: customer unreachable, wrong address, customer refused, wrong number, other (with a note). Several may be selected. Result: stage `failed`, `failed_at`, reasons stored, customer gets M20, the order is closed (no retry), and a dashboard alert is raised until staff acknowledge it. Refunds are handled manually by staff outside the system.

**Pickup path:** approve -> `preparing` -> staff **Mark ready** -> `ready` and the customer gets M16 -> staff **Collected** (with confirm) -> `collected`, thank-you M18b sent immediately.

**"Didn't receive it?"** (button on M18): record `not_received_at`, reply M19, raise a dashboard alert until acknowledged.

## 6. Rider interface over WhatsApp

### 6.1 Identification
On every incoming message, check whether the sender's number belongs to a non-archived rider **before** any customer logic. Rider numbers are rider-only: they never receive menu or customer messages. Update `last_message_at` on every rider message, including button taps. Unknown text -> the command list (R5).

### 6.2 Commands (typed, case-insensitive) and equivalent buttons
`active`, `inactive`, `help`, `pending` (lists the rider's open deliveries with batch number, address and status), `picked <batch>`, `delivered <batch>`, `fail <batch>`, `undo <batch>`. A rider can act only on batches assigned to him and only in the correct stage; otherwise reply politely. Button ids should carry the batch number so several simultaneous deliveries are unambiguous.

### 6.3 Availability
`active` marks the rider active, `inactive` marks him inactive. A rider counts as **available** only if `is_active` is true AND less than 24 hours have passed since his last message (WhatsApp only allows free-form messages within 24 hours of the person's last message to the bot, and we deliberately use no templates in this version). Inactive or expired riders cannot be selected for dispatch (enforce this server-side, not just in the UI). A rider who is mid-delivery when he goes inactive can still finish it.

### 6.4 Failing a delivery
`Can't deliver` / `fail <batch>` -> the bot sends a numbered text list (1 Customer unreachable, 2 Wrong address, 3 Customer refused, 4 Wrong number, 5 Other, add a note after the number) and asks the rider to reply with numbers like `1,3`. Then a mandatory confirm step. Keep this small per-rider conversation state on the rider row (for example `pending_action` as JSON text).

### 6.5 Confirm steps
Delivered always asks "Confirm delivery of batch #X?" with **Yes, delivered** / **Not yet**.

### 6.6 Privacy rule
Rider messages contain: batch number, individual order numbers, customer name, phone, address, landmark, second phone, notes, map link, and an item **count** ("3 items across 2 orders"). They must NEVER contain item names or prices. Add a test asserting this.

## 7. Dashboard

Keep the existing constraints: no external scripts, fonts or CDNs; inline SVG only; CSS variables; escape all user-supplied text; responsive on phones; accessible controls.

### 7.1 Navigation
Manage Menu, **Orders**, **Riders**, **Delivery areas**.

### 7.2 Orders view (replace the empty/placeholder content, merge with any existing order history and keep its pagination and search)
Tabs: **Awaiting approval** (`awaiting_approval`), **In progress** (`preparing`, `ready`, `dispatched`, `out_for_delivery`), **Completed** (`delivered`, `collected`), **Cancelled & failed** (`cancelled`, `failed`), plus search by name, phone or batch number. Batches still in the customer's own checkout stages are hidden, except fee-quote requests (below).

A **"Needs attention"** strip at the top lists: fee quotes needed, failed deliveries not yet acknowledged, "Didn't receive it?" reports not yet acknowledged, dispatch failures, and overdue deliveries. Each alert has an **Acknowledge** button (except fee quotes, which clear when a fee is entered).

Each order card shows: batch number and order numbers; customer name, customer ID and phone; fulfillment type; for delivery the address (with a **Copy** button), landmark, second phone, notes, a **map link** (pin: `https://www.google.com/maps?q=LAT,LON`; no pin: a Google Maps search URL built from the typed address + area + `MAPS_REGION_HINT`, URL-encoded); the assigned rider; the stage badge; a small timeline with timestamps (approved, dispatched, picked up, delivered/failed/collected); **minutes elapsed counted from approval and stopped at delivered, failed or collected**; an **Overdue** badge when applicable; receipt status; items and totals.

Actions by stage: Approve/Reject (awaiting approval; Approve disabled while `address_editing`, with an explanatory tooltip), Mark ready (preparing), rider dropdown + Dispatch (ready, delivery), Resend (dispatch failed), Reassign (dispatched), Mark failed with reasons (ready/dispatched/out for delivery), Collected (ready, pickup), and the fee-quote form (fee + expected minutes). Every state-changing button has a confirm step, disables itself after a click, and the server must re-check the stage so double clicks and stale pages cannot corrupt state.

### 7.3 Live updates and alerts
Refresh the Awaiting approval, In progress and alerts data **every 15 seconds** (Completed and Cancelled load when opened). New alerts trigger a **sound and a visual alert** for: a new order awaiting approval, a failed delivery, a "Didn't receive it?" report, and a new fee-quote request. Visual: toast, flashing badge on the nav item, and a page title like `(2) New order`. Sound: a short beep generated with the Web Audio API (no audio files). Browsers block sound until the user interacts, so add an **Enable sound** button (remember the choice in `localStorage`). Track which alerts were already announced so refreshes do not repeat them.

### 7.4 Riders page
A list with: name, phone, **status badge** (Active / Inactive; for Active show roughly when the 24-hour window ends), number of open deliveries, and actions. Staff can **add** a rider (name + phone, validated and normalized to the format WhatsApp uses: digits only with country code, e.g. `2348012345678`), **edit** name or phone, and **remove** a rider. Removal is an archive (never a hard delete) and is blocked, with a clear message, if the rider has open deliveries. Staff cannot force a rider active; the rider does that over WhatsApp.

### 7.5 Delivery areas page
Add, edit, disable areas: name (max 24 characters, unique), fee, expected delivery minutes, sort order. Disabling an area keeps existing orders intact.

## 8. Data model (proposed; adjust to project conventions and keep money as Numeric)

- `delivery_areas`: id, name (unique), fee, expected_minutes, is_active, sort_order.
- `riders`: id, name, phone_number (unique), is_archived, is_active, last_message_at, window_warning_sent_at, pending_action (nullable text/JSON), created_at.
- `saved_addresses`: id, user_id (unique FK), street, area_id (nullable FK), area_name, landmark, phone2, notes, latitude, longitude, updated_at.
- `fulfillments` (one per `PaymentBatch`, unique FK): fulfillment_type (delivery/pickup), stage, checkout_step (address sub-step), street, area_id, area_name, landmark, phone2, notes, latitude, longitude, expected_minutes, rider_id (nullable FK), timer_started_at, reminders_sent, address_editing, address_edit_started_at, dispatch_error, approved_at, ready_at, dispatched_at, picked_up_at, delivered_at, collected_at, failed_at, cancelled_at, thank_you_due_at, thank_you_sent_at, rider_reminder_sent_at, failure_reasons (JSON text), failure_note, failed_by (rider/staff), not_received_at, plus `*_acknowledged_at` fields for failed / not-received alerts.
- Existing `PaymentBatch.delivery_fee` and `total_amount` are updated when the fee becomes known.
- Add indexes on `fulfillments(stage)` and `riders(phone_number)`.
- **Legacy rows:** batches created before this feature have no fulfillment record. Display them exactly as before and never crash on them. Do not backfill.

## 9. WhatsApp client changes

- `_send` and the public send functions must **report success or failure** (return a bool or raise a specific exception) so dispatch can detect failures. Customer-facing sends should still never crash the webhook handler.
- Add `send_location_request(to, body)` using an interactive message of type `location_request_message` with action name `send_location` (body up to 1,024 characters).
- Parse incoming `location` messages (latitude, longitude, name, address) into `IncomingMessage`.
- Support the list pagination described in 3.3. Respect limits: button title 20 chars, list row title 24, row description 72, list button label 20, 3 reply buttons max.
- Do not add template-message support in this version.

## 10. Messages

Put **every** customer-facing and rider-facing string in one module, `app/messages.py`, as functions or `.format` templates, so the owner can edit wording in one place. Use these drafts (placeholders in braces are filled from data or settings). Write any further small prompts in the same warm, professional tone and list them in your summary for review.

**Customer**
- **M1 choice:** "Thank you! How would you like to receive your order?" [Delivery] [Pickup] [Cancel order]
- **M2 street:** "Great, delivery it is. 🛵 What is your street address? Please include your house or plot number."
- **M3 area:** "Which area are you in?" (list, button "Choose area"). **M3b:** "No problem. Please type the name of your area."
- **M4 pin:** "To help our rider find you faster, you can share your location pin. Tap the button below, or choose Skip." [Skip]
- **M5 landmark:** "What is a landmark near your address? (For example, a church, school or bus stop.)"
- **M6 second phone:** "Is there a second phone number we can reach you on during delivery? Send it now, or tap Skip."
- **M7 notes:** "Any notes for the rider? (For example, "call when you reach the gate".) Send them now, or tap Skip."
- **M8 summary:** "Please check your delivery details:\n\n📍 {street}, {area}\nLandmark: {landmark}\n{second phone line}{notes line}\nOrder: ₦{subtotal}\nDelivery fee: ₦{fee}\nTotal: ₦{total}" [Confirm] [Edit]
- **M8b other area:** "Thank you! Your area isn't on our standard list, so our team is confirming your delivery fee. We'll message you shortly."
- **M8c quote:** "The delivery fee to {area} is ₦{fee}, so your total is ₦{total}. Would you like to continue?" [Proceed to pay] [Cancel order]
- **M9 address confirmed and payment (message b):** "Thank you, your delivery address is confirmed ✅\n\nTotal to pay: ₦{total}\n\nPlease transfer to:\n{bank_name}\n{account_number}\n{account_name}\n\nThen send a photo of your transfer receipt here within {minutes} minutes. To change your address, reply ADDRESS."
- **M9-pickup:** "Pickup selected. You can collect your order at {OUTLET_ADDRESS}.\n\nTotal to pay: ₦{total}\n\nPlease transfer to:\n{bank_name}\n{account_number}\n{account_name}\n\nThen send a photo of your transfer receipt here within {minutes} minutes."
- **M10 receipt received:** "Receipt received. Thank you! 🙏 Our team is confirming your payment. Until we approve it, you can still change your delivery address within the same area by replying ADDRESS."
- **M11 payment approved (message c):** "Your payment is confirmed ✅ Thank you! Order #{batch} is now being prepared. We'll message you as soon as it's on its way." (pickup: "...We'll let you know when it's ready for pickup.")
- **M12 receipt rejected:** "We couldn't verify your payment for order #{batch}. Please send a clear photo of your transfer receipt again, or call {SUPPORT_PHONE} if you need help. Your delivery details are saved. You have {minutes} minutes to resend it."
- **M13 reminder:** "A friendly reminder: we're still waiting for {what} for order #{batch}. Please send it within {remaining} minutes, otherwise the order will be cancelled automatically." (`what` = "your delivery details" / "your payment receipt" / "your decision on the delivery fee")
- **M14 auto-cancel:** "Order #{batch} has been cancelled because we didn't receive {what} in time. You're welcome to start again whenever you're ready. Just send *menu*."
- **M15 locked:** "Your order has already been approved, so the delivery address can no longer be changed. Please call {SUPPORT_PHONE} and we'll help."
- **M15b area locked:** "You can change your street, landmark and notes, but your area must stay the same now that your receipt has been sent. To change your area, please call {SUPPORT_PHONE}."
- **M16 pickup ready:** "Good news! Your order #{batch} is ready for pickup 🎉\nCollect it at: {OUTLET_ADDRESS}\nNeed directions? Call {OUTLET_PHONE}."
- **M17 on its way (message d):** "Your order #{batch} is on its way! 🛵\nYour rider is {rider_name}. You can reach them on {rider_phone}.\nPlease keep your phone nearby."
- **M18 delivered thank-you (message e; the only message that names the brand):** "Your order #{batch} has been delivered. Thank you for choosing {BRAND_NAME}. We hope you enjoy every bite! 🍽️\nIf anything isn't right, tap the button below." [Order again] [Didn't receive it?]
- **M18b pickup collected:** "Thank you for collecting order #{batch}. We appreciate you choosing {BRAND_NAME}! 🍽️" [Order again]
- **M19 not received:** "Thank you for telling us. We're sorry for the trouble. Our team will contact you shortly."
- **M20 failed delivery (message f):** "We're very sorry, we couldn't deliver order #{batch} ({items}).\nReason: {reasons}.\nThis order is now closed. Our team will review your payment and contact you within {REFUND_TIMEFRAME} to confirm any refund due. To speak to us right away, call {SUPPORT_PHONE}." Customer-facing reason wording: unreachable -> "we couldn't reach you by phone"; wrong address -> "the address couldn't be found"; refused -> "the order wasn't accepted on delivery"; wrong number -> "the phone number provided was incorrect"; other -> the rider's or staff's note.

**Rider**
- **R1 dispatch:** "New delivery 🛵\nBatch #{batch} (orders {order_numbers})\nCustomer: {name}, {phone}\nAddress: {street}, {area}\nLandmark: {landmark}\n{second phone line}{notes line}Map: {map_link}\n{n} items across {m} order(s)" [Picked up] [Can't deliver]
- **R2 after pickup:** "Thanks! Tap Delivered once you've handed over batch #{batch}." [Delivered] [Can't deliver]
- **R3 confirm delivered:** "Confirm delivery of batch #{batch}?" [Yes, delivered] [Not yet]. After yes: "Marked as delivered ✅ If this was a mistake, tap Undo within {minutes} minutes." [Undo]
- **R4 pickup reminder:** "Reminder: batch #{batch} is waiting for pickup. Tap Picked up once you have collected it." [Picked up] [Can't deliver]
- **R5 help:** list of commands (`active`, `inactive`, `pending`, `picked <batch>`, `delivered <batch>`, `fail <batch>`, `undo <batch>`, `help`).
- **R6 reassigned:** "Delivery #{batch} was reassigned. No action needed from you."
- **R7 window warning:** "Your availability expires in about an hour. Reply ACTIVE to stay available for deliveries."
- Active/inactive confirmations: "You're now active ✅ You'll receive deliveries until you send INACTIVE." / "You're now inactive. You won't receive new deliveries."

## 11. Configuration (add to `app/config.py` and `.env.example`; no secrets)

```
BRAND_NAME=Tofex Grill and More
OUTLET_ADDRESS=[Outlet address - to be added]
OUTLET_PHONE=[Outlet phone - to be added]
SUPPORT_PHONE=[Support phone - to be added]
REFUND_TIMEFRAME=[timeframe - to be added]
MAPS_REGION_HINT=Lagos, Nigeria
CHECKOUT_REMINDER_MINUTES=10
CHECKOUT_CANCEL_MINUTES=30
ADDRESS_EDIT_TIMEOUT_MINUTES=10
THANK_YOU_DELAY_MINUTES=10
RIDER_PICKUP_REMINDER_MINUTES=10
RIDER_WINDOW_WARNING_HOURS=23
OVERDUE_GRACE_MINUTES=10
```
If `APP_ENV=production` and any value still contains a "[... to be added]" placeholder, log a clear startup warning.

## 12. Phases (stop after each for the user's confirmation)

1. **Foundation:** models, one Alembic migration, areas and riders API, **Riders** and **Delivery areas** dashboard pages, phone normalization, `app/messages.py`, new settings.
2. **Customer checkout:** fulfillment choice, address steps, saved addresses, fee calculation, other-area quotes (with the staff fee form), payment messages, receipt, changes of address, reject/resend, cancel, scheduler for reminders and auto-cancel.
3. **Fulfillment and riders:** statuses, approve/ready/dispatch/reassign/collected/fail, rider WhatsApp interface, customer notifications, thank-you scheduler, undo, "Didn't receive it?".
4. **Dashboard Orders view:** tabs, cards, alerts strip, 15-second refresh, sound and visual alerts, overdue logic, merged with existing history/pagination.
5. **Testing and simulator:** see section 14.

## 13. Migration (important)

- One Alembic migration per phase is fine. `down_revision` must be the current head: run `alembic heads`; do not guess.
- The user's database may have been created with `scripts/init_db.py` (no `alembic_version` table). Give exact commands for both cases: (a) Alembic-managed: `alembic upgrade head`; (b) created by `create_all`: `alembic stamp <previous revision>` then `alembic upgrade head`, or equivalent SQLite `ALTER TABLE`/`CREATE TABLE` statements. Test the commands on a copy of a database.
- Existing rows must keep working (see "Legacy rows" in section 8).

## 14. Testing and development simulator

- Extend `scripts/simulate.py` (DRY_RUN only) so one terminal can act as a customer, as a **rider** (send messages from a rider's phone number, tap rider buttons, share a location pin), and as staff via the dashboard. Show captured outgoing messages for any recipient, labeled by who they went to.
- The user has also added a real second WhatsApp number as a test rider. Remind them that on Meta's test number each recipient must be added to the allowed list, that the rider must be added in the Riders page in the normalized format, and that the rider must send `active` to the bot before staff can dispatch to him.
- Add automated tests where practical (pytest + an in-memory SQLite): phone normalization, fee/total calculation, stage transition guards (for example approving while `address_editing`), rider availability (active flag and the 24-hour rule), the rider-message privacy rule (no item names or prices), scheduler idempotency (reminders and cancel fire once), undo before and after the thank-you.
- Finish with a manual test checklist covering: delivery with a known area; "Other area" quote; saved-address reuse; pickup; address change before and after the receipt and after approval; rejected receipt then resend; 10/20/30-minute reminder and cancel (use shortened settings for the test); dispatch to an inactive rider is refused; dispatch failure shows a warning and Resend works; reassign; Picked up -> Delivered -> Undo and thank-you timing; failed delivery with multiple reasons; "Didn't receive it?"; alert sounds; overdue flag; legacy order still displays.

## 15. Do not

- Do not use external scripts, fonts, CDNs or audio files in the dashboard.
- Do not add hard-delete endpoints for riders, areas, menu items or orders.
- Do not retry failed deliveries or build refund automation.
- Do not log full customer addresses or phone numbers at INFO level in production paths beyond what already exists.
- Do not send item names or prices to riders.
- Do not enable `DRY_RUN` or the simulator in production.
- Authentication for the dashboard is a separate task and is out of scope here, but keep every new endpoint under the same router and prefix so a login can be added in one place. Mention this in your summary.

## 16. What to return

For each phase: the changed or new files in full, a plain-language explanation of each change, the exact migration commands, and the manual test steps. At the very end, a list of every message string in `app/messages.py` for the owner to review.
