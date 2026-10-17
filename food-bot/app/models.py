from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime


class SessionState(str, enum.Enum):
    BROWSING = "BROWSING"
    ADDING_TO_CART = "ADDING_TO_CART"
    SELECTING_CARTS = "SELECTING_CARTS"
    EDITING_PROFILE = "EDITING_PROFILE"
    AWAITING_RECEIPT = "AWAITING_RECEIPT"
    COLLECTING_DETAILS = "COLLECTING_DETAILS"
    AWAITING_FEE_QUOTE = "AWAITING_FEE_QUOTE"
    AWAITING_FEE_ACCEPT = "AWAITING_FEE_ACCEPT"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"


class FulfillmentStage(str, enum.Enum):
    COLLECTING_DETAILS = "collecting_details"
    AWAITING_FEE_QUOTE = "awaiting_fee_quote"
    AWAITING_FEE_ACCEPT = "awaiting_fee_accept"
    AWAITING_PAYMENT = "awaiting_payment"
    AWAITING_APPROVAL = "awaiting_approval"
    PREPARING = "preparing"
    READY = "ready"
    DISPATCHED = "dispatched"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    COLLECTED = "collected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    active_cart_id: Mapped[int | None] = mapped_column(Integer)
    current_state: Mapped[SessionState] = mapped_column(
        Enum(SessionState, native_enum=False, length=32), default=SessionState.BROWSING
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())
    saved_address: Mapped["SavedAddress | None"] = relationship(back_populates="user", uselist=False)


class Cart(Base):
    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())

    items: Mapped[list[CartItem]] = relationship(back_populates="cart", cascade="all, delete-orphan")


class MenuItem(Base):
    __tablename__ = "menu"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    category: Mapped[str] = mapped_column(String(60), index=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    # Staff-managed stock. NULL means this item is binary: available or sold out.
    # Binary items use is_available and are never decremented automatically.
    stock_quantity: Mapped[int | None] = mapped_column(Integer)
    # Once stock_quantity <= low_stock_threshold the item is hidden and staff are alerted.
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=0)
    availability_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @property
    def sellable_units(self) -> int | None:
        """Units customers may still buy before the threshold is hit. None = untracked."""
        if self.stock_quantity is None:
            return None
        return max(0, self.stock_quantity - self.low_stock_threshold)


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("cart_id", "item_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("menu.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    cart: Mapped[Cart] = relationship(back_populates="items")


class PaymentBatch(Base):
    __tablename__ = "payment_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    receipt_media_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e]),
        default=OrderStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())

    orders: Mapped[list[Order]] = relationship(back_populates="payment_batch", cascade="all, delete-orphan")
    fulfillment: Mapped["Fulfillment | None"] = relationship(
        back_populates="payment_batch", uselist=False, cascade="all, delete-orphan"
    )


class ReceiptImage(Base):
    __tablename__ = "receipt_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_batch_id: Mapped[int] = mapped_column(
        ForeignKey("payment_batches.id", ondelete="CASCADE"), index=True
    )
    media_id: Mapped[str] = mapped_column(String(200))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    payment_batch_id: Mapped[int] = mapped_column(ForeignKey("payment_batches.id"), index=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e]),
        default=OrderStatus.PENDING,
    )
    delivery_address: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())

    user: Mapped[User] = relationship()
    payment_batch: Mapped[PaymentBatch] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("menu.id"))
    item_name: Mapped[str] = mapped_column(String(120))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    quantity: Mapped[int] = mapped_column(Integer)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    order: Mapped[Order] = relationship(back_populates="items")
    item: Mapped[MenuItem] = relationship()


class ProcessedMessage(Base):
    """WhatsApp message ids already handled, so Meta's webhook retries never duplicate work."""

    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())


class DeliveryArea(Base):
    __tablename__ = "delivery_areas"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(24), unique=True)
    fee: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    expected_minutes: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())


class Rider(Base):
    __tablename__ = "riders"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    window_warning_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    pending_action: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())


class SavedAddress(Base):
    __tablename__ = "saved_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    street: Mapped[str] = mapped_column(String(150))
    area_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_areas.id", ondelete="SET NULL"))
    area_name: Mapped[str] = mapped_column(String(120))
    landmark: Mapped[str] = mapped_column(String(100))
    phone2: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(String(200))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now(), onupdate=func.now())

    user: Mapped[User] = relationship(back_populates="saved_address")
    area: Mapped["DeliveryArea | None"] = relationship()


class Fulfillment(Base):
    __tablename__ = "fulfillments"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_batch_id: Mapped[int] = mapped_column(
        ForeignKey("payment_batches.id", ondelete="CASCADE"), unique=True, index=True
    )
    fulfillment_type: Mapped[str] = mapped_column(String(16))
    stage: Mapped[FulfillmentStage] = mapped_column(
        Enum(
            FulfillmentStage,
            native_enum=False,
            length=32,
            values_callable=lambda values: [item.value for item in values],
        ),
        index=True,
    )
    checkout_step: Mapped[str | None] = mapped_column(String(32))
    street: Mapped[str | None] = mapped_column(String(150))
    area_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_areas.id", ondelete="SET NULL"))
    area_name: Mapped[str | None] = mapped_column(String(120))
    landmark: Mapped[str | None] = mapped_column(String(100))
    phone2: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(String(200))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    expected_minutes: Mapped[int | None] = mapped_column(Integer)
    rider_id: Mapped[int | None] = mapped_column(ForeignKey("riders.id", ondelete="SET NULL"))
    timer_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reminders_sent: Mapped[str] = mapped_column(Text, default="[]")
    address_editing: Mapped[bool] = mapped_column(Boolean, default=False)
    address_edit_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    dispatch_error: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ready_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    picked_up_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    collected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    thank_you_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    thank_you_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    rider_reminder_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    failure_reasons: Mapped[str | None] = mapped_column(Text)
    failure_note: Mapped[str | None] = mapped_column(Text)
    failed_by: Mapped[str | None] = mapped_column(String(16))
    not_received_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    failed_acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    not_received_acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    payment_batch: Mapped[PaymentBatch] = relationship(back_populates="fulfillment")
    area: Mapped["DeliveryArea | None"] = relationship()
    rider: Mapped["Rider | None"] = relationship()