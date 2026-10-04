from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SessionState(str, enum.Enum):
    BROWSING = "BROWSING"
    ADDING_TO_CART = "ADDING_TO_CART"
    SELECTING_CARTS = "SELECTING_CARTS"
    EDITING_PROFILE = "EDITING_PROFILE"
    AWAITING_RECEIPT = "AWAITING_RECEIPT"
    COMPLETED = "COMPLETED"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Cart(Base):
    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

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
    availability_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    orders: Mapped[list[Order]] = relationship(back_populates="payment_batch", cascade="all, delete-orphan")


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
