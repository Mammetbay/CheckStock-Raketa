"""Pydantic v2 request/response schemas and domain contracts for CheckStock."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WarehouseCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Название склада ФФ")

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Название склада не может быть пустым")
        return cleaned


class WarehouseRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Новое название склада ФФ")

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Название склада не может быть пустым")
        return cleaned


class WarehouseDeleteRequest(BaseModel):
    """Options when deleting (archiving) a fulfillment warehouse with or without stock."""

    transfer_to_warehouse_id: int | None = Field(
        default=None,
        description=(
            "Опциональный ID действующего ФФ, куда нужно сразу перевести весь доступный "
            "остаток при удалении. Если не указан — остаток сохраняется на архивном балансе "
            "удалённого ФФ и явно отображается на экране остатков."
        ),
    )
    note: str | None = Field(default=None, max_length=255)


class ReceiptCreateRequest(BaseModel):
    product_id: int = Field(..., gt=0)
    warehouse_id: int = Field(..., gt=0)
    qty: int = Field(..., gt=0, le=1_000_000, description="Количество прихода (> 0)")
    supplier_source: str = Field(default="Поставщик / Производство", max_length=128)
    note: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=128)


class TransferCreateRequest(BaseModel):
    product_id: int = Field(..., gt=0)
    from_warehouse_id: int = Field(..., gt=0)
    to_warehouse_id: int = Field(..., gt=0)
    qty: int = Field(..., gt=0, le=1_000_000, description="Количество для перемещения (> 0)")
    note: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=128)


class FBSReserveCreateRequest(BaseModel):
    product_id: int = Field(..., gt=0)
    warehouse_id: int = Field(..., gt=0)
    qty: int = Field(..., gt=0, le=1_000_000)
    note: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=128)
    allow_split: bool = Field(
        default=False,
        description=(
            "Если True и на выбранном ФФ не хватает товара, но общего доступного остатка "
            "на всех активных ФФ достаточно — система атомарно разделит резерв по складам."
        ),
    )


class FBOShipmentCreateRequest(BaseModel):
    product_id: int = Field(..., gt=0)
    warehouse_id: int = Field(..., gt=0)
    qty: int = Field(..., gt=0, le=1_000_000)
    marketplace_name: str = Field(default="Склад маркетплейса (WB FBO)", max_length=128)
    note: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=128)
    allow_split: bool = Field(
        default=False,
        description=(
            "Если True и на выбранном ФФ не хватает товара, но общего доступного остатка "
            "на всех активных ФФ достаточно — система атомарно разделит поставку FBO по складам."
        ),
    )


class ShortageAllocationItem(BaseModel):
    warehouse_id: int
    warehouse_name: str
    available_qty: int
    allocate_qty: int
    is_primary: bool


class TransferReplenishmentSuggestion(BaseModel):
    from_warehouse_id: int
    from_warehouse_name: str
    to_warehouse_id: int
    to_warehouse_name: str
    transfer_qty: int


class ShortageResolutionPlan(BaseModel):
    """Structured plan returned when selected FF lacks stock, or global stock is insufficient."""

    code: Literal["SHORTAGE_ON_SELECTED_FF", "INSUFFICIENT_TOTAL_STOCK"]
    operation_kind: Literal["FBS", "FBO", "TRANSFER"]
    product_id: int
    sku: str
    product_name: str
    requested_warehouse_id: int
    requested_warehouse_name: str
    requested_qty: int
    available_on_selected_qty: int
    deficit_on_selected_qty: int
    total_available_across_network_qty: int
    can_resolve_across_network: bool
    message: str
    split_allocations: list[ShortageAllocationItem] = Field(default_factory=list)
    replenishment_transfers: list[TransferReplenishmentSuggestion] = Field(default_factory=list)


class ReconciliationSKUReport(BaseModel):
    product_id: int
    sku: str
    product_name: str
    total_receipts_qty: int
    active_ff_physical_qty: int
    archived_ff_physical_qty: int
    total_ff_physical_qty: int
    active_ff_available_qty: int
    active_ff_reserved_fbs_qty: int
    archived_ff_available_qty: int
    archived_ff_reserved_fbs_qty: int
    in_transit_ff_qty: int
    in_transit_fbo_qty: int
    received_fbo_qty: int
    shipped_fbs_qty: int
    accounted_total_qty: int
    discrepancy_qty: int
    is_balanced: bool


class ReconciliationSummary(BaseModel):
    all_balanced: bool
    formula: str
    checked_at: datetime
    items: list[ReconciliationSKUReport]
