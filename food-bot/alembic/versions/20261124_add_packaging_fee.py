"""add packaging_fee to payment_batches and orders

Revision ID: 20261124_add_packaging_fee
Revises: 20261113_add_staff_takeover
Create Date: 2026-11-24 12:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261124_add_packaging_fee"
down_revision: Union[str, None] = "20261113_add_staff_takeover"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payment_batches",
        sa.Column("packaging_fee", sa.Numeric(10, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "orders",
        sa.Column("packaging_fee", sa.Numeric(10, 2), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("orders", "packaging_fee")
    op.drop_column("payment_batches", "packaging_fee")
