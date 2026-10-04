from decimal import Decimal


def naira(amount: Decimal | int | float) -> str:
    return f"₦{Decimal(amount):,.0f}"
