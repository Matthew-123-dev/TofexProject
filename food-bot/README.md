# WhatsApp Food Ordering Bot

FastAPI + async SQLAlchemy. Customers use WhatsApp only; staff log in to a
password-protected web dashboard to manage the menu, orders, riders, and
delivery areas.

**Start with `SETUP_GUIDE.md`.** Part 1 lets you test the whole flow with no accounts.

## Structure

```
app/
  main.py                  App, routers, and StaffPageMiddleware (login gate for staff pages)
  config.py                Settings from .env
  database.py              Async engine and sessions
  models.py                users, carts, menu, cart_items, payment_batches, orders, order_items,
                            delivery_areas, riders, fulfillments, saved_addresses, processed_messages
  schemas.py                IncomingMessage
  seed.py                  Starter menu with stock levels and thresholds
  dev_outbox.py            Dry-run message capture
  api/                     webhook_whatsapp, dashboard, auth (staff login/logout), health, dev
  services/                state_machine, menu_service (stock logic), order_service, whatsapp
  dashboard.html           Staff dashboard, orders, riders, and delivery-areas pages
  login.html               Staff login page
alembic/                   Migrations
```

## Staff dashboard

Staff log in at `/login` with credentials hardcoded in `.env`
(`STAFF_USERNAME` / `STAFF_PASSWORD`, not stored in the database). Once
logged in, the dashboard is split into separate pages:

- **`/dashboard`** – summary (menu item counts, available-now count, pending
  orders, last sync) plus condensed previews (top categories/items, the 5
  most recent active orders, up to 5 riders with the rider form, up to 5
  delivery areas), each with a "More" link to its full page.
- **`/manage-menu`** – full menu management: add, edit, delete, and toggle
  availability/stock for every item, grouped by category.
- **`/orders`** – every order, active orders first and completed ones last;
  approve/reject payment batches, send or edit a delivery-fee quote for
  custom ("Other") delivery areas, preview an uploaded payment receipt, and
  delete an order.
- **`/riders`** – the rider form plus the full rider list, with
  archive/unarchive and delete actions.
- **`/delivery-areas`** – add, edit, and delete delivery areas and their fees.

Stock, pricing, descriptions, categories, and availability are all managed
from the dashboard forms (there are no WhatsApp text commands for this
anymore). Each menu item has `stock_quantity` (empty = availability-only)
and `low_stock_threshold`; when a paid order brings stock down to the
threshold or below, the item is automatically hidden from the customer
menu. Customers can't add more than the sellable units (stock minus
threshold) to a cart.

See `SETUP_GUIDE.md` for a full walkthrough of every dashboard feature.

## Delivery areas and quotes

Customers pick a delivery area as the first step of checkout. If they choose
a listed area, its fee is applied automatically. If they choose "Other" and
type a custom area, the order is held until a staff member sends a delivery
fee quote from the `/orders` page; the customer then confirms the quote
before the order proceeds.

## Multiple carts and payment batches

Customers can create automatically numbered carts. The active cart is used while
adding items; the cart-selection screen lets them select several carts for
checkout. Each selected cart becomes its own order, while one payment batch
contains the shared receipt, payment status, and one delivery fee (or quote).

## Test locally without webhooks

You can test the app locally without configuring any Meta callback:

1. Set `DRY_RUN=true` in `.env`.
2. Use the local SQLite database (`DATABASE_URL=sqlite+aiosqlite:///./foodbot.db`).
3. Set `STAFF_USERNAME` / `STAFF_PASSWORD` in `.env` for dashboard login.
4. Start the app with `uvicorn app.main:app --reload`.
5. Open `http://127.0.0.1:8000/login`, sign in, and you'll land on `/dashboard`.
6. Read the messages the bot would have sent from `GET /dev/outbox`.

In dry-run mode, outbound WhatsApp calls are printed to the terminal and stored in
the in-memory outbox. No webhook registration is needed for this workflow. Note
that the `:image` simulator command sends a fake media id, so the "View receipt"
preview on `/orders` will always show an "unavailable in dry-run" message locally
— real image previews only appear once connected to a live WhatsApp webhook with
`DRY_RUN=false`.

To simulate customer messages from a terminal, start the API and run:

```bash
uvicorn app.main:app --reload
python scripts/simulate.py
```

Type `menu`, then type the reply IDs printed by the bot, such as `cat:Food` or
`add:1`. Use `:image` to simulate a receipt upload, and `:help` for all commands.

## Not done yet
Cart editing, customer-side order cancellation, and automated tests.
