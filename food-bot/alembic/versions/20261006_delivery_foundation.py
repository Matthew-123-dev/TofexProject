"""add delivery areas, riders, saved addresses and fulfillments

Revision ID: 20261006_delivery_foundation
Revises: 843f5c2d7f76
"""
from alembic import op
import sqlalchemy as sa


revision = "20261006_delivery_foundation"
down_revision = "843f5c2d7f76"
branch_labels = None
depends_on = None


FULFILLMENT_STAGES = (
    "collecting_details", "awaiting_fee_quote", "awaiting_fee_accept",
    "awaiting_payment", "awaiting_approval", "preparing", "ready",
    "dispatched", "out_for_delivery", "delivered", "collected", "failed", "cancelled",
)


def upgrade() -> None:
    op.create_table(
        "delivery_areas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(24), nullable=False, unique=True),
        sa.Column("fee", sa.Numeric(10, 2), nullable=False),
        sa.Column("expected_minutes", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "riders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("phone_number", sa.String(32), nullable=False, unique=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("window_warning_sent_at", sa.DateTime(timezone=True)),
        sa.Column("pending_action", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_riders_phone_number", "riders", ["phone_number"], unique=True)
    op.create_table(
        "saved_addresses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("street", sa.String(150), nullable=False),
        sa.Column("area_id", sa.Integer()),
        sa.Column("area_name", sa.String(120), nullable=False),
        sa.Column("landmark", sa.String(100), nullable=False),
        sa.Column("phone2", sa.String(32)),
        sa.Column("notes", sa.String(200)),
        sa.Column("latitude", sa.Numeric(10, 7)),
        sa.Column("longitude", sa.Numeric(10, 7)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["area_id"], ["delivery_areas.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "fulfillments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payment_batch_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("fulfillment_type", sa.String(16), nullable=False),
        sa.Column("stage", sa.Enum(*FULFILLMENT_STAGES, name="fulfillmentstage", native_enum=False, length=32), nullable=False),
        sa.Column("checkout_step", sa.String(32)),
        sa.Column("street", sa.String(150)),
        sa.Column("area_id", sa.Integer()),
        sa.Column("area_name", sa.String(120)),
        sa.Column("landmark", sa.String(100)),
        sa.Column("phone2", sa.String(32)),
        sa.Column("notes", sa.String(200)),
        sa.Column("latitude", sa.Numeric(10, 7)),
        sa.Column("longitude", sa.Numeric(10, 7)),
        sa.Column("expected_minutes", sa.Integer()),
        sa.Column("rider_id", sa.Integer()),
        sa.Column("timer_started_at", sa.DateTime(timezone=True)),
        sa.Column("reminders_sent", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("address_editing", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("address_edit_started_at", sa.DateTime(timezone=True)),
        sa.Column("dispatch_error", sa.Text()),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("ready_at", sa.DateTime(timezone=True)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("picked_up_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("collected_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("thank_you_due_at", sa.DateTime(timezone=True)),
        sa.Column("thank_you_sent_at", sa.DateTime(timezone=True)),
        sa.Column("rider_reminder_sent_at", sa.DateTime(timezone=True)),
        sa.Column("failure_reasons", sa.Text()),
        sa.Column("failure_note", sa.Text()),
        sa.Column("failed_by", sa.String(16)),
        sa.Column("not_received_at", sa.DateTime(timezone=True)),
        sa.Column("failed_acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("not_received_acknowledged_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["payment_batch_id"], ["payment_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["area_id"], ["delivery_areas.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_fulfillments_stage", "fulfillments", ["stage"])
    op.create_index("ix_fulfillments_payment_batch_id", "fulfillments", ["payment_batch_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_fulfillments_payment_batch_id", table_name="fulfillments")
    op.drop_index("ix_fulfillments_stage", table_name="fulfillments")
    op.drop_table("fulfillments")
    op.drop_table("saved_addresses")
    op.drop_index("ix_riders_phone_number", table_name="riders")
    op.drop_table("riders")
    op.drop_table("delivery_areas")
