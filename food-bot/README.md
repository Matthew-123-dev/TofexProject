# WhatsApp Food Ordering Bot

FastAPI + async SQLAlchemy. Customers use WhatsApp only; staff approvals, menu management, stock control, and order review use the web dashboard.

**Start with `SETUP_GUIDE.md`.** Part 1 lets you test the whole flow with no accounts.

## Structure

```
app/
  main.py                  App and routers (dev router only when DRY_RUN=true)
  config.py                Settings from .env
  database.py              Async engine and sessions
  models.py        users, carts, menu, cart_items, payment_batches, orders, order_items, processed_messages
  schemas.py               IncomingMessage
  seed.py                  Starter menu with stock levels and thresholds
  dev_outbox.py            Dry-run message capture
  api/                     webhook_whatsapp, dashboard, health, dev
  services/                state_machine, menu_service (stock logic), order_service, whatsapp
alembic/                   Migrations
scripts/simulate.py        Terminal chat for testing (customer + admin)
```

## Staff-managed stock

Each menu item has `stock_quantity` (empty = availability-only) and `low_stock_threshold`. When a paid order brings stock to the threshold or below, the item is hidden from the menu. Staff manage menu, stock, and orders at `/dashboard`:

```
/stock                    list everything
/stock <name> <qty>       set quantity on hand (re-shows the item if above threshold)
/threshold <name> <n>     hide the item once stock reaches n
/soldout <name>           hide now
/available <name>         show again
```
Customers can't add more than the sellable units (stock minus threshold) to a cart.

## Multiple carts and payment batches

Customers can create automatically numbered carts. The active cart is used while
adding items; the cart-selection screen lets them select several carts for
checkout. Each selected cart becomes its own order, while one payment batch
contains the shared receipt, payment status, and one delivery fee.

The dashboard supports editing prices, descriptions, categories, stock, and availability, plus adding menu items and approving or rejecting payment batches.

## Not done yet
Delivery address capture, menu pagination over 10 items, cart editing, customer-side order cancellation, dashboard authentication, automated tests, and receipt-image preview.
