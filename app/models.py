"""SQLAlchemy 2.0 ORM models and database-level constraints for CheckStock."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc)


class TransferStatus(StrEnum):
    IN_TRANSIT = "IN_TRANSIT"
    RECEIVED = "RECEIVED"


class FBSOrderStatus(StrEnum):
    RESERVED = "RESERVED"
    SHIPPED = "SHIPPED"
    CANCELLED = "CANCELLED"


class FBOShipmentStatus(StrEnum):
    IN_TRANSIT = "IN_TRANSIT"
    RECEIVED = "RECEIVED"


class OperationType(StrEnum):
    RECEIPT = "RECEIPT"
    TRANSFER_SENT = "TRANSFER_SENT"
    TRANSFER_RECEIVED = "TRANSFER_RECEIVED"
    FBS_RESERVED = "FBS_RESERVED"
    FBS_SHIPPED = "FBS_SHIPPED"
    FBS_CANCELLED = "FBS_CANCELLED"
    FBO_SENT = "FBO_SENT"
    FBO_RECEIVED = "FBO_RECEIVED"
    WAREHOUSE_ARCHIVE_TRANSFER = "WAREHOUSE_ARCHIVE_TRANSFER"


class Product(Base):
    """SKU catalog item (Артикул и наименование товара)."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    stocks: Mapped[list[WarehouseStock]] = relationship("WarehouseStock", back_populates="product")


class Warehouse(Base):
    """Fulfillment warehouse (Склад фулфилмента — ФФ).

    Supports soft deletion (`is_deleted=True`) so deleting a warehouse never destroys
    historical operations or strands physical inventory without traceability.
    """

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archive_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    stocks: Mapped[list[WarehouseStock]] = relationship("WarehouseStock", back_populates="warehouse")


class WarehouseStock(Base):
    """Materialized inventory state for a (warehouse, product) pair.

    Database CHECK constraints guarantee:
    - `physical_qty >= 0`
    - `available_qty >= 0`
    - `reserved_fbs_qty >= 0`
    - `physical_qty = available_qty + reserved_fbs_qty`
    """

    __tablename__ = "warehouse_stocks"
    __table_args__ = (
        UniqueConstraint("warehouse_id", "product_id", name="uq_warehouse_product_stock"),
        CheckConstraint("physical_qty >= 0", name="ck_stock_physical_non_negative"),
        CheckConstraint("available_qty >= 0", name="ck_stock_available_non_negative"),
        CheckConstraint("reserved_fbs_qty >= 0", name="ck_stock_reserved_non_negative"),
        CheckConstraint(
            "physical_qty = available_qty + reserved_fbs_qty",
            name="ck_stock_balance_invariant",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    physical_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_fbs_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    warehouse: Mapped[Warehouse] = relationship("Warehouse", back_populates="stocks")
    product: Mapped[Product] = relationship("Product", back_populates="stocks")


class Transfer(Base):
    """Inter-warehouse transfer (Перемещение между ФФ)."""

    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_transfer_qty_positive"),
        CheckConstraint("from_warehouse_id != to_warehouse_id", name="ck_transfer_distinct_warehouses"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transfer_number: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    from_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    to_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=TransferStatus.IN_TRANSIT.value, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped[Product] = relationship("Product")
    from_warehouse: Mapped[Warehouse] = relationship("Warehouse", foreign_keys=[from_warehouse_id])
    to_warehouse: Mapped[Warehouse] = relationship("Warehouse", foreign_keys=[to_warehouse_id])


class FBSOrder(Base):
    """FBS reservation and customer shipment (Резерв и отправка FBS)."""

    __tablename__ = "fbs_orders"
    __table_args__ = (CheckConstraint("qty > 0", name="ck_fbs_qty_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=FBSOrderStatus.RESERVED.value, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped[Product] = relationship("Product")
    warehouse: Mapped[Warehouse] = relationship("Warehouse")


class FBOShipment(Base):
    """FBO shipment to marketplace warehouse (Отправка на склад маркетплейса по FBO)."""

    __tablename__ = "fbo_shipments"
    __table_args__ = (CheckConstraint("qty > 0", name="ck_fbo_qty_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shipment_number: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    marketplace_name: Mapped[str] = mapped_column(
        String(128), default="Склад маркетплейса (FBO)", nullable=False
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=FBOShipmentStatus.IN_TRANSIT.value, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped[Product] = relationship("Product")
    warehouse: Mapped[Warehouse] = relationship("Warehouse")


class StockOperation(Base):
    """Immutable audit log of every stock movement (История операций)."""

    __tablename__ = "stock_operations"
    __table_args__ = (CheckConstraint("qty > 0", name="ck_operation_qty_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    from_location: Mapped[str] = mapped_column(String(128), nullable=False)
    to_location: Mapped[str] = mapped_column(String(128), nullable=False)
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    product: Mapped[Product] = relationship("Product")
