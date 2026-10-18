from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_policy() -> HTMLResponse:
    brand = escape(settings.brand_name)
    support = escape(settings.support_phone)
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Privacy Policy · {brand}</title>
  <style>
    body {{
      margin: 0;
      padding: 40px 20px;
      background: #f7f3ed;
      color: #292321;
      font: 16px/1.6 system-ui, sans-serif;
    }}
    main {{
      max-width: 720px;
      margin: 0 auto;
      padding: 32px;
      background: #fffdf9;
      border: 1px solid #e8dfd5;
      border-radius: 16px;
    }}
    h1, h2 {{ font-family: Georgia, serif; }}
    h1 {{ margin-top: 0; }}
  </style>
</head>
<body>
  <main>
    <h1>Privacy Policy for {brand}</h1>
    <p>This Privacy Policy explains how <strong>{brand}</strong> collects, uses, and protects information when you place orders through our WhatsApp ordering service.</p>

    <h2>1. Information We Collect</h2>
    <ul>
      <li><strong>Contact Information:</strong> Your WhatsApp phone number, profile name, and any optional secondary phone number you provide for delivery.</li>
      <li><strong>Delivery Details:</strong> Your delivery street address, area, landmark, delivery notes, and optional GPS location pin.</li>
      <li><strong>Order &amp; Payment Information:</strong> Items in your cart, order history, and images of payment transfer receipts you send to confirm your order.</li>
    </ul>

    <h2>2. How We Use Your Information</h2>
    <p>We use this information solely to process, prepare, and deliver your food orders, verify bank transfer payments, assign orders to our delivery riders, and provide customer support.</p>

    <h2>3. Data Sharing &amp; Storage</h2>
    <p>We do not sell or rent your personal information to third parties. Your delivery address and phone number are shared only with our authorized delivery riders assigned to fulfill your order.</p>

    <h2>4. Data Deletion &amp; Contact Us</h2>
    <p>You may request to update or delete your saved delivery address and order data at any time by contacting our support team at <strong>{support}</strong>.</p>
  </main>
</body>
</html>"""
    return HTMLResponse(content=html)