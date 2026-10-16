from decimal import Decimal
import re


def naira(amount: Decimal | int | float) -> str:
    return f"₦{Decimal(amount):,.0f}"


def normalize_phone(phone: str) -> str:
    """Return a Nigerian WhatsApp number as country-code digits without a plus sign."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0") and len(digits) == 11:
        digits = "234" + digits[1:]
    elif digits.startswith("234") and len(digits) == 13:
        pass
    else:
        raise ValueError("Enter a Nigerian number such as 08012345678 or +2348012345678.")
    if not re.fullmatch(r"234[789]\d{9}", digits):
        raise ValueError("Enter a valid Nigerian mobile number.")
    return digits


def fake_whatsapp_function(to: str, body: str) -> bool:
    """Fake WhatsApp function to simulate sending a message."""
    # Simulate sending a message and return True if successful
    return True
