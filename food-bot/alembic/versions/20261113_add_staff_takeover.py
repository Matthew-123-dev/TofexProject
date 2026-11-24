"""add staff takeover pause fields to users

Revision ID: 20261113_add_staff_takeover
Revises: 20261016_add_receipt_images
Create Date: 2026-11-13 15:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261113_add_staff_takeover"
down_revision: Union[str, None] = "20261016_add_receipt_images"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("bot_paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("bot_paused_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("escalation_requested_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "escalation_requested_at")
    op.drop_column("users", "bot_paused_until")
    op.drop_column("users", "bot_paused_at")
