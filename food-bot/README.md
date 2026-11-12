# WhatsApp Food Ordering Bot (`food-bot`)

A full-featured, production-ready WhatsApp commerce and delivery management platform built with **FastAPI** and **async SQLAlchemy**. Designed for restaurants, cloud kitchens, and food vendors.

Customers browse, type orders in plain English/pidgin, manage multiple packs (carts), check out for **Delivery** or **Pickup**, and upload bank transfer receipts—all inside WhatsApp. Staff manage the menu, stock levels, payment approvals, custom area fee quotes, and rider dispatch from a password-protected web dashboard, while riders receive and update deliveries directly on WhatsApp.

---

## Key Features & Functionality

### 1. Guided 6-Step Conversational Ordering UX
- **Clear Progress & Live Cart Status:** Every poll/menu displays `Step X of 6` and a live running cart summary (`🛒 Cart: 3 items (₦4,800)`).
- **Friendly Categories & Popularity Sorting:** Database categories are presented with customer-friendly names and emojis (e.g., `🍛 Rice, Spaghetti & Porridge`, `🌯 Shawarma & Snacks`, `🍗 Grilled Chicken`, `🦃 Grilled Turkey`, `🥩 Beef, Fish, Plantain & Extras`, `🥤 Cold Drinks, Yoghurt & Water`), sorted with the most popular items first.
- **Smart Quantity Selection:** 1-tap options for `1`, `2`, `3`, `4`, and `5+ portions (Type quantity)` with total prices calculated inside every option.
- **Sequential Add-Ons & Upsells:** After adding an item, customers can immediately tap `🚫 No extras — Checkout` or add grilled chicken, turkey, beef/fish/plantain, or a cold drink.
- **Fast Path for Returning Customers:** Automatically surfaces `🔁 Repeat my last order` on the Step 1 welcome menu when the customer has previous order history, re-checking live stock before loading the cart.
- **Universal Escape Routes:** Every screen after Step 1 includes `⬅ Go back` (backed by a per-user navigation stack and checkout step-back logic) and `❌ Cancel order`.

### 2. Typed (Free-Text) Natural-Language Ordering
Customers can tap `✍️ Type your order` on Step 1—or simply type their order at any point while browsing—to skip navigating multiple category menus:
- **Zero API Cost & Low RAM (`~5 MB`):** Powered by a deterministic rule-based parser + **RapidFuzz** (`token_sort_ratio`) fuzzy matching and a curated local dictionary—no external LLM fees or heavy PyTorch models required.
- **Multi-Item & Nigerian Slang Support:** Understands messages like:
  - `"2 jollof rice add stew, 1 fried rice, 2 chicken medium, 1 dodo, 1 moi moi, 2 coke"`
  - `"jollof of 800, shawarma of 1200 extra pepper no sausage, 1 eva water"`
- **Naira-Based Ordering:** Converts Naira amounts automatically (`"jollof of 800"` → `2 x Jollof rice` at `₦400` each; `"jollof of 700"` prompts a 1-tap `1 portion (₦400)` vs `2 portions (₦800)` check).
- **Smart Follow-Up Polls (Capped at Max 2):** If a customer types an ambiguous size (`"2 chicken"`) or a minor typo (`"1 jolof"`), the bot asks at most **2 quick follow-up polls** (`Step 2 of 6: Quick check 1 of 2`) with prices and a `⬅ Go back` undo option. Any extra ambiguous items are listed clearly under `⚠️ Please specify size/type` with an exact example to retype.
- **Sold-Out Alternatives & Stock Capping:** Automatically flags sold-out items (`⛔ Sold out`) with up to 2 in-stock alternatives from the same category as 1-tap buttons (`➕ Add <Item> instead`), and caps quantities that exceed remaining stock.
- **Special Kitchen Requests:** Extracts meal modifiers (`add stew` for Jollof/Fried Rice; `extra pepper`, `more pepper`, `less pepper`, `no pepper`, `no sausage` for Shawarma; plus `well done`, `no onions`, `no mayo`, `less/extra cream`).
  - **Staff vs. Rider Privacy:** Kitchen notes (`Kitchen: ...`) are saved on `Fulfillment.notes` so kitchen staff see them on the dashboard, but are automatically stripped from the rider dispatch message and from the customer's `SavedAddress`.

### 3. Dual WhatsApp Provider Support (Evolution API + Meta Cloud API)
Switch providers with a single environment variable (`WHATSAPP_PROVIDER`):
- **`evolution` (Evolution API v2 / Baileys):** Sends native 1-tap WhatsApp Polls (`selectableCount: 1`), supports `@lid` / `@s.whatsapp.net` JID resolution, decrypts/fetches receipt photos via `getBase64FromMediaMessage`, and accepts numbered text fallbacks (`1`, `2`, `3`) when customers type instead of tapping a poll option.
- **`meta` (Official Meta WhatsApp Cloud API):** Sends native Interactive Reply Buttons (when `<= 3` options) and automatically upgrades to Interactive List Menus (when `4–10` options), splitting long labels into clean 24-character titles and 72-character descriptions.

### 4. Multi-Cart ("Separate Packs") & Payment Batches
Customers ordering for multiple people can create separate carts/packs (`🛍️ New cart (Separate pack)`), view/remove individual items (`➖ Remove <Item>`), and check out all selected carts together in one `PaymentBatch` with a single delivery fee and transfer receipt.

### 5. Delivery, Pickup & Saved Addresses
- **Pickup (`🏪 Pickup at Outlet`):** `₦0` delivery fee; skips address collection and goes straight to the itemized Step 6 Order Summary.
- **Delivery (`🛵 Delivery to my address`):**
  - Remembers the customer's last confirmed `SavedAddress` for 1-tap reuse (`📍 Use this address (+₦Fee)`), dynamically recalculating the current fee from `DeliveryArea`.
  - Collects delivery area (with fee and estimated minutes), street address, optional WhatsApp GPS location pin, nearest landmark, optional backup phone, and delivery notes.
  - Supports custom **"Other area"** requests: holds the order in `AWAITING_FEE_QUOTE` until staff send a custom delivery fee quote from the dashboard, which the customer can accept (`✅ Proceed to pay`) or decline.

### 6. Rider Dispatch & WhatsApp Delivery Tracking
Riders interact with the bot using their registered phone number:
- Receive dispatches (`R1_DISPATCH`) with customer contact info, address, landmark, delivery-only notes, and a clickable Google Maps link (GPS pin or text-search fallback).
- Tap `Picked up` → notifies the customer (`M17_ON_WAY`) with the rider's name and phone number.
- Tap `Delivered` → confirms delivery with a configurable undo window (`r:undo:<batch_id>`) before the automated thank-you message (`M18_DELIVERED`) is sent to the customer.
- Tap `Can't deliver` → guided 1–5 failure reason flow that closes the delivery, notifies the customer, and alerts staff on the dashboard.
- Send `ACTIVE` / `INACTIVE` / `PENDING` / `HELP` at any time.

### 7. Staff Web Dashboard & Background Scheduler
Password-protected web interface (`/login`) with 5 dedicated views:
- **`/dashboard`:** Live overview of menu availability, pending approvals, active deliveries, riders, and delivery areas.
- **`/manage-menu`:** Add, edit, delete, and toggle availability or stock quantities (`stock_quantity` and `low_stock_threshold`) for every item.
- **`/orders`:** View itemized orders and kitchen/delivery notes, preview uploaded bank transfer receipts, approve/reject payments, send delivery fee quotes for custom areas, mark orders ready, and dispatch to active riders.
- **`/riders`:** Register riders, view 24-hour WhatsApp window status, archive/unarchive, or delete riders.
- **`/delivery-areas`:** Manage delivery neighborhoods, fees (`₦`), and estimated delivery times.

---

## Tech Stack & Tools

| Layer | Technology | Purpose |
| :--- | :--- | :--- |
| **Backend Framework** | [FastAPI](https://fastapi.tiangolo.com/) + Uvicorn | Async webhook handling, REST endpoints, and server-rendered staff pages |
| **Database & ORM** | [SQLAlchemy 2.0 (Async)](https://docs.sqlalchemy.org/) + Alembic | Async ORM supporting SQLite (`aiosqlite` for local dev) and PostgreSQL (`asyncpg` for Railway/production) |
| **Natural-Language Parser** | [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) + Rule Engine | Fast, zero-cost (`₦0/msg`), low-memory (`~5 MB`) free-text order & modifier parsing |
| **WhatsApp Integration** | [HTTPX](https://www.python-httpx.org/) | Async client for Evolution API v2 (Polls/Media) and Meta Graph API v21.0 (Buttons/Lists/Media) |
| **Configuration** | Pydantic Settings (`pydantic-settings`) | Typed `.env` configuration with automatic Railway production detection |
| **Testing** | Pytest + `pytest-asyncio` | `66` automated unit and end-to-end conversational flow tests |

---

## Project Structure

```text
food-bot/
├── app/
│   ├── main.py                  # FastAPI app, lifespan startup, StaffPageMiddleware, and router registration
│   ├── config.py                # Typed environment settings (.env)
│   ├── database.py              # Async SQLAlchemy engine and session factory
│   ├── models.py                # Database models (User, Cart, MenuItem, PaymentBatch, Order, Fulfillment, Rider, etc.)
│   ├── messages.py              # All customer and rider WhatsApp message templates (M1..M20, R1..R7)
│   ├── schemas.py               # Normalized IncomingMessage schema
│   ├── seed.py                  # Starter menu catalog (51 items across 6 categories)
│   ├── scheduler.py             # Background loop for checkout reminders, timeouts, and thank-you messages
│   ├── dev_outbox.py            # In-memory message capture for local dry-run testing
│   ├── utils.py                 # Naira formatting (₦) and Nigerian phone number normalization (234...)
│   ├── dashboard.html           # Staff web dashboard template
│   ├── login.html               # Staff login page template
│   ├── api/
│   │   ├── webhook_whatsapp.py  # Inbound Meta & Evolution API webhook normalization, deduplication & routing
│   │   ├── dashboard.py         # Staff dashboard endpoints (menu CRUD, order approval, quotes, rider dispatch)
│   │   ├── auth.py              # Staff session login/logout and HMAC cookie signing
│   │   ├── dev.py               # Dry-run simulator outbox endpoints
│   │   └── health.py            # Healthcheck endpoint (/health)
│   └── services/
│       ├── state_machine.py     # Core 6-step conversational ordering, typed-order, checkout & rider state machine
│       ├── order_parser.py      # Rule-based + RapidFuzz free-text order parser, aliases, and size families
│       ├── menu_service.py      # Category/item queries, friendly display names, and popularity sorting
│       ├── order_service.py     # Cart management, stock reservation, and PaymentBatch creation
│       └── whatsapp.py          # Dual-provider outbound messaging (Evolution Polls & Meta Buttons/Lists)
├── alembic/                     # Database migrations
├── scripts/
│   └── simulate.py              # Interactive terminal chat simulator for local dry-run testing
├── tests/                       # 66 automated tests (UX flow, typed orders, checkout, riders, providers, dashboard)
├── requirements.txt             # Python dependencies
└── SETUP_GUIDE.md               # Additional walkthrough notes
```

---

## Step-by-Step Setup & Implementation Guide (For Personal or Production Use)

### Step 1: Clone & Install Dependencies

1. Ensure you have **Python 3.11+** installed.
2. Clone the repository and create a virtual environment:

```bash
git clone <your-repo-url>
cd food-bot

# Create and activate a virtual environment (Windows PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Or on Linux / macOS:
# python3 -m venv .venv
# source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Step 2: Configure Environment Variables (`.env`)

Create a `.env` file in the project root directory with the required configuration keys:

```ini
# --- Environment & Database ---
APP_ENV=development
DATABASE_URL=sqlite+aiosqlite:///./foodbot.db
DRY_RUN=true

# --- Staff Dashboard Credentials ---
STAFF_USERNAME=
STAFF_PASSWORD=
SESSION_SECRET=

# --- Business Branding & Bank Transfer Details ---
BRAND_NAME=
OUTLET_ADDRESS=
OUTLET_PHONE=
SUPPORT_PHONE=
REFUND_TIMEFRAME=
MAPS_REGION_HINT=

BANK_NAME=
BANK_ACCOUNT_NUMBER=
BANK_ACCOUNT_NAME=

# --- WhatsApp Provider Selection ("evolution" or "meta") ---
WHATSAPP_PROVIDER=evolution

# Option A: Evolution API settings (when WHATSAPP_PROVIDER=evolution)
EVOLUTION_API_URL=
EVOLUTION_API_KEY=
EVOLUTION_INSTANCE=

# Option B: Meta WhatsApp Cloud API settings (when WHATSAPP_PROVIDER=meta)
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_VERIFY_TOKEN=
WHATSAPP_APP_SECRET=
WHATSAPP_API_VERSION=v21.0

# --- Timers (in minutes / hours) ---
CHECKOUT_REMINDER_MINUTES=10
CHECKOUT_CANCEL_MINUTES=30
ADDRESS_EDIT_TIMEOUT_MINUTES=10
THANK_YOU_DELAY_MINUTES=10
RIDER_PICKUP_REMINDER_MINUTES=10
RIDER_WINDOW_WARNING_HOURS=23
```

### Step 3: Run Database Migrations & Seed the Menu

Run Alembic migrations to create the tables, then seed the starter menu:

```bash
alembic upgrade head
python -m app.seed
```

### Step 4: Test Locally (Zero External Accounts Needed)

With `DRY_RUN=true` in `.env`:

1. Start the FastAPI server:
   ```bash
   uvicorn app.main:app --reload
   ```
2. Open the **Staff Dashboard** at `http://127.0.0.1:8000/login` and sign in with your `STAFF_USERNAME` and `STAFF_PASSWORD`.
   - Go to `/delivery-areas` to add a delivery area, fee, and estimated time.
   - Go to `/riders` to add a test rider.
3. In a second terminal (with your virtual environment activated), launch the interactive chat simulator:
   ```bash
   python scripts/simulate.py
   ```
   - Type `menu` to see the Step 1 poll.
   - Type an option ID (such as `cat:Food`, `add:1`, `qty:1:2`, or `typeorder:start`) or type a natural-language order directly like:
     `2 jollof rice add stew, 1 large shawarma less pepper no sausage, 1 coke`
   - Type `:image` when prompted for a bank transfer receipt photo.

### Step 5: Connect to a Live WhatsApp Number

Once you are ready to go live, set `DRY_RUN=false` in `.env` and configure your chosen provider:

#### Option A: Using Evolution API (`WHATSAPP_PROVIDER=evolution`)
1. Deploy or open your **Evolution API v2** instance, create an instance, and connect your WhatsApp number.
2. Configure the Evolution webhook URL to point to your deployed bot:
   - **Webhook URL:** `https://<your-bot-domain>/webhook/whatsapp`
   - **Webhook Events:** Enable `MESSAGES_UPSERT`.
3. Set `WHATSAPP_PROVIDER=evolution`, `EVOLUTION_API_URL`, `EVOLUTION_API_KEY`, and `EVOLUTION_INSTANCE` in `.env`.

#### Option B: Using Official Meta Cloud API (`WHATSAPP_PROVIDER=meta`)
1. In the [Meta Developer Portal](https://developers.facebook.com/), create a Business App and add the **WhatsApp** product.
2. Copy your **Phone Number ID**, **Permanent Access Token**, and **App Secret** into `.env`.
3. Under **WhatsApp → Configuration → Webhook**:
   - **Callback URL:** `https://<your-bot-domain>/webhook/whatsapp`
   - **Verify Token:** Match `WHATSAPP_VERIFY_TOKEN` in `.env`.
   - Subscribe to the `messages` webhook field.
4. Set `WHATSAPP_PROVIDER=meta` in `.env`.

### Step 6: Deploy to Railway (or Render / Fly.io)

1. Push your repository to GitHub and create a new project on [Railway](https://railway.app/).
2. Add a **PostgreSQL** database service in Railway and link its `DATABASE_URL` to your web service (the app automatically converts `postgres://` or `postgresql://` to `postgresql+asyncpg://`).
3. Set `APP_ENV=production` and add your `.env` variables in Railway's **Variables** tab.
   > **Note:** In production (`APP_ENV=production` or on Railway), the app enforces security checks at startup: `DRY_RUN` must be `false`, `STAFF_PASSWORD` and `SESSION_SECRET` must be set, and provider credentials must be present.
4. Set your start command to:
   ```bash
   alembic upgrade head && python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```
   *(Note: `python -m app.seed` is idempotent—it only inserts menu items that do not already exist).*

---

## How to Customize & Edit the Bot for Your Own Business

### 1. Changing Menu Items, Prices & Stock (No Code Needed)
For day-to-day updates, log in to **`/manage-menu`** on the web dashboard:
- Add new meals, edit prices, toggle availability on/off, or set `stock_quantity` and `low_stock_threshold`.
- If you want to change the initial starter catalog for fresh installations, edit the `SEED_ITEMS` list in `app/seed.py`.

### 2. Customizing Friendly Category & Item Names and Popularity Order
Open `app/services/menu_service.py`:
- **`CATEGORY_DISPLAY_NAMES` & `CATEGORY_SORT_ORDER`:** Map raw database category names (e.g., `"Food"`, `"Snacks"`) to friendly display titles with emojis (`"🍛 Rice, Spaghetti & Porridge"`) and control the order in which categories appear in Step 1.
- **`ITEM_DISPLAY_NAMES`:** Map raw database item names (e.g., `"Shawama (1200)"`, `"Chicken 2"`) to clear customer-facing labels (`"Shawarma (Regular)"`, `"Chicken Medium (2)"`).
- **`ITEM_POPULARITY_ORDER`:** List items in the exact order they should appear inside each category poll (Principle 3: list the most popular options first).
- **`EXTRAS_MOVED_FROM_FOOD`:** Move specific side items (such as `Plantain (1)` and `Moimoi`) out of one DB category so they surface inside another category (`Protein 1`).

### 3. Customizing the Typed-Order Parser (Aliases, Sizes & Special Requests)
Open `app/services/order_parser.py`:
- **`ITEM_ALIASES`:** Add local slang, abbreviations, or common spellings mapped to exact database `MenuItem.name` values:
  ```python
  ITEM_ALIASES = {
      "dodo": "Plantain (1)",
      "moi moi": "Moimoi",
      "spag": "Spaghetti",
      "asaro": "Porridge",
      # Add your own aliases here:
      "ofada": "Ofada rice",
  }
  ```
- **`SIZE_FAMILIES`:** When a menu item comes in multiple sizes (e.g., `"chicken"`, `"turkey"`, `"shawarma"`, `"fish"`), register its family key, display label, list of DB item names, and retype hint so typing bare `"2 chicken"` automatically triggers a size selection poll.
- **`SPECIAL_REQUEST_PATTERNS`:** Add regex patterns for any kitchen preparation notes your restaurant supports:
  ```python
  SPECIAL_REQUEST_PATTERNS = [
      (re.compile(r"\b(?:add|extra|more|with|plenty)\s+stew\b", re.IGNORECASE), "add stew"),
      (re.compile(r"\b(?:no|without|remove|minus)\s+sausages?\b", re.IGNORECASE), "no sausage"),
      # Add custom kitchen modifiers here
  ]
  ```
- **`MAX_FOLLOWUP_POLLS`:** Currently set to `2` so customers are never asked more than 2 clarifying questions before seeing their order confirmation.

### 4. Editing WhatsApp Message Copy & Templates
- **Checkout & Rider Templates:** Edit `app/messages.py` to customize the wording for address prompts (`M1`–`M7`), order summaries (`M8_SUMMARY`, `M8_PICKUP_SUMMARY`), bank payment instructions (`M9_PAYMENT`, `M9_PICKUP`), status updates (`M10`–`M20`), and rider dispatches (`R1`–`R7`).
- **Browsing & Typed-Order Poll Copy:** Edit `app/services/state_machine.py` to adjust the Step 1–4 poll headers or the Step 4 add-on upsell buttons (in `handle_add_item` and `handle_typed_order_confirm`).
  > **WhatsApp Platform Limits to Keep in Mind:**
  > - Poll questions: max `255` characters.
  > - Poll option labels: `2` to `12` options per poll, max `100` characters per option (Evolution) / `24` characters for interactive row titles + `72` characters for descriptions (Meta).

---

## Running the Automated Test Suite

The project includes `66` automated tests covering the 6-step UX flow, typed-order parsing, multi-cart checkout, saved addresses, Evolution & Meta providers, rider dispatch, scheduler timeouts, and dashboard actions.

```bash
pytest -v
```
