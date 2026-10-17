"""add receipt_images table

Revision ID: 20261016_add_receipt_images
Revises: 20261006_delivery_foundation
Create Date: 2026-10-16 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261016_add_receipt_images"
down_revision: Union[str, None] = "20261006_delivery_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "receipt_images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payment_batch_id", sa.Integer(), nullable=False),
        sa.Column("media_id", sa.String(length=200), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["payment_batch_id"], ["payment_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_receipt_images_payment_batch_id"),
        "receipt_images",
        ["payment_batch_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_receipt_images_payment_batch_id"), table_name="receipt_images")
    op.drop_table("receipt_images")