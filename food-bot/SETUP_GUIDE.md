# Setup and testing guide

## Local setup

From the `food-bot` directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `DRY_RUN=true` while testing locally, then add the WhatsApp and bank settings
when you are ready to connect the real client.

For a new development database:

```bash
python scripts/init_db.py
```

For an existing database managed by Alembic, generate and apply a migration
instead of using `create_all`:

```bash
alembic revision --autogenerate -m "update current schema"
alembic upgrade head
python -m app.seed
```

## Run the app

```bash
uvicorn app.main:app --reload
```

Open the staff dashboard at:

```text
http://127.0.0.1:8000/dashboard
```

The dashboard supports:

- Editing prices, descriptions, categories, stock, and availability.
- Adding new menu items.
- Reviewing payment batches.
- Approving or rejecting orders.

Approved or rejected payments still notify the customer through WhatsApp.

## WhatsApp webhook

The only external webhook is:

```text
POST /webhook/whatsapp
```

Configure this callback in Meta WhatsApp Cloud API and subscribe to the
`messages` field. The dashboard is a normal web page and does not require a
separate webhook.

## Current development limitations

- The dashboard does not yet have staff authentication.
- Delivery address capture is not implemented.
- Menu lists are limited to the WhatsApp list-message limit.
- Receipt images are stored by WhatsApp media ID but are not previewed in the dashboard yet.
