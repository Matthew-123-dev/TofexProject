# Setup and simulation guide

This guide covers local development, the staff dashboard, the optional terminal
simulation, and connecting the bot to the WhatsApp Cloud API.

## Prerequisites

- Python 3.10 or newer.
- `pip` and `venv`.
- A terminal opened in the `food-bot` directory.
- SQLite for local development (included through the Python dependency
  `aiosqlite`). PostgreSQL is also supported when `DATABASE_URL` points to a
  PostgreSQL database.

On Linux or macOS, create and activate a virtual environment:

```bash
cd food-bot
python3 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell, use:

```powershell
cd food-bot
py -m venv .venv
.venv\Scripts\Activate.ps1
```

Install the dependencies and create the local environment file:

```bash
pip install -r requirements.txt
cp .env.example .env
```

For Windows, copy the file with `Copy-Item .env.example .env` instead.

## Configure the environment

The default `.env.example` values are suitable for local SQLite development.
Before starting the app, set at least:

```dotenv
APP_ENV=development
DATABASE_URL=sqlite+aiosqlite:///./foodbot.db
DRY_RUN=true
```

`DRY_RUN=true` prevents real WhatsApp API calls, prints outgoing messages to
the terminal, and enables `GET /dev/outbox` for local inspection and the
simulator. Do not use it in production.

The following values are optional for dry-run mode but are needed for a useful
ordering flow:

- `BRAND_NAME`, `OUTLET_ADDRESS`, `OUTLET_PHONE`, and `SUPPORT_PHONE`.
- `BANK_NAME`, `BANK_ACCOUNT_NUMBER`, and `BANK_ACCOUNT_NAME` for payment
  instructions.
- `DELIVERY_FEE` and the checkout/reminder timing settings.

Do not commit `.env`; it can contain access tokens and other deployment
secrets.

## Initialize the database

For a new local database, run this once from the `food-bot` directory:

```bash
python scripts/init_db.py
```

This creates the current tables and seeds the starter menu. It uses the
database configured by `DATABASE_URL`.

For an existing database managed by Alembic, do not use the initialization
script. Back up SQLite first, then apply the migrations:

```bash
cp foodbot.db foodbot.db.bak
alembic upgrade head
python -m app.seed
```

On Windows PowerShell, use `Copy-Item foodbot.db foodbot.db.bak`. If the
database is new but you need to apply migrations rather than create tables
directly, use `alembic upgrade head` and then `python -m app.seed`.

When the schema changes during development, generate and apply a migration:

```bash
alembic revision --autogenerate -m "describe the schema change"
alembic upgrade head
```

## Run the application locally

Start the API from the same directory:

```bash
uvicorn app.main:app --reload
```

The application is available at `http://127.0.0.1:8000`. Useful endpoints are:

- Staff dashboard: `http://127.0.0.1:8000/dashboard`
- Health check: `http://127.0.0.1:8000/health`
- Dry-run outbox: `http://127.0.0.1:8000/dev/outbox` (only when `DRY_RUN=true`)
- WhatsApp webhook: `POST /webhook/whatsapp`

The dashboard can edit menu prices, descriptions, categories, stock, and
availability; add menu items; review and approve or reject payment batches; and
manage riders and delivery areas. There is currently no staff authentication,
so do not expose the development server publicly.

## Run the terminal simulation (optional)

The simulator is useful when you want to exercise the customer conversation
without a Meta account, webhook registration, or real WhatsApp messages.

1. Leave the API running in the first terminal.
2. Set `DRY_RUN=true` in `.env` and restart the API if it was already running.
3. In a second terminal, activate the same virtual environment and run:

   ```bash
   python scripts/simulate.py
   ```

The simulator checks `/health`, sends signed requests to the local
`/webhook/whatsapp` endpoint, reads the dry-run outbox, and prints the bot
responses. It uses `2348012345678` as the simulated customer number by
default. Change it with either:

```bash
python scripts/simulate.py --phone 2348098765432
python scripts/simulate.py --base-url http://127.0.0.1:8000 --phone 2348098765432
```

Type `:help` to show commands. A typical flow is:

1. Type `menu`.
2. Type a reply ID printed by the bot, such as `cat:Food`.
3. Type an item action such as `add:1`, then follow the displayed reply IDs.
4. Type `:image` to simulate uploading a receipt image.
5. Type `:outbox` to read pending dry-run messages or `:quit` to exit.

The simulator also accepts normal text such as `carts`, `newcart`, and
`checkout`. It never contacts Meta and never sends a real WhatsApp message.
If it reports that it cannot connect, confirm that Uvicorn is still running
and that its URL matches `--base-url`.

## Connect a real WhatsApp webhook (optional)

Only do this after local dry-run testing. Set `DRY_RUN=false` and provide the
values supplied by Meta in `.env`:

```dotenv
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_VERIFY_TOKEN=use-a-long-random-value
WHATSAPP_APP_SECRET=...
WHATSAPP_API_VERSION=v21.0
```

Deploy the application behind HTTPS, then configure the Meta callback URL as:

```text
https://your-domain.example/webhook/whatsapp
```

Use the same `WHATSAPP_VERIFY_TOKEN` in Meta's webhook verification settings
and subscribe to the `messages` field. The `WHATSAPP_APP_SECRET` is used to
validate `X-Hub-Signature-256` on incoming requests. Do not use the local
Uvicorn server or SQLite file as a public production deployment.

## Known limitations

- The dashboard does not yet have staff authentication.
- Delivery address capture is not implemented.
- Menu lists are limited to the WhatsApp list-message limit.
- Receipt images are stored by WhatsApp media ID but are not previewed in the
  dashboard.
