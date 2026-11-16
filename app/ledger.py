"""Deep Domain Module for Warehouse Stock & Movement Accounting (`LedgerModule`).

Design vocabulary (`codebase-design`):
- **Module**: `LedgerModule` encapsulates all stock mutations, state machines, shortage resolution
  planning, warehouse soft-deletion, and reconciliation verification.
- **Interface**: Public functions/methods in this file plus domain exceptions (`DomainError`,
  `ShortageError`). HTTP routes and Pytest suites interact exclusively across this **Seam**.
- **Implementation**: Pessimistic row-level locks (`SELECT ... FOR UPDATE`), invariant checks,
  idempotency guards (`idempotency_key` + state machine), and immutable ledger recording.
"""

from __future__ import annotations

from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    FBOShipment,
    FBOShipmentStatus,
    FBSOrder,
    FBSOrderStatus,
    OperationType,
    Product,
    StockOperation,
    Transfer,
    TransferStatus,
    Warehouse,
    WarehouseStock,
    utcnow,
)
from app.schemas import (
    FBOShipmentCreateRequest,
    FBSReserveCreateRequest,
    ReceiptCreateRequest,
    ReconciliationSKUReport,
    ReconciliationSummary,
    ShortageAllocationItem,
    ShortageResolutionPlan,
    TransferCreateRequest,
    TransferReplenishmentSuggestion,
    WarehouseCreateRequest,
    WarehouseDeleteRequest,
    WarehouseRenameRequest,
)


class DomainError(Exception):
    """Structured domain exception surfaced cleanly at the HTTP/Test seam."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "DOMAIN_ERROR",
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}


class ShortageError(DomainError):
    """Raised when the selected FF warehouse does not have enough available stock."""

    def __init__(self, plan: ShortageResolutionPlan) -> None:
        super().__init__(
            plan.message,
            code=plan.code,
            status_code=409,
            details={"shortage_plan": plan.model_dump(mode="json")},
        )
        self.plan = plan


# ---------------------------------------------------------------------------
# Internal helpers (Private to the Deep Module Implementation)
# ---------------------------------------------------------------------------


def _get_product_or_raise(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if not product:
        raise DomainError(f"Товар с ID={product_id} не найден", code="PRODUCT_NOT_FOUND", status_code=404)
    return product


def _get_warehouse_or_raise(db: Session, warehouse_id: int, *, allow_deleted: bool = False) -> Warehouse:
    warehouse = db.get(Warehouse, warehouse_id)
    if not warehouse:
        raise DomainError(f"Склад ФФ с ID={warehouse_id} не найден", code="WAREHOUSE_NOT_FOUND", status_code=404)
    if warehouse.is_deleted and not allow_deleted:
        raise DomainError(
            f"Склад ФФ «{warehouse.name}» удалён (в архиве) и недоступен для новых операций прихода/отгрузки.",
            code="WAREHOUSE_ARCHIVED",
            status_code=409,
        )
    return warehouse


def _lock_or_create_stock(db: Session, warehouse_id: int, product_id: int) -> WarehouseStock:
    """Acquire a row-level lock (`SELECT ... FOR UPDATE`) on `(warehouse_id, product_id)` stock."""
    stmt = (
        select(WarehouseStock)
        .where(
            WarehouseStock.warehouse_id == warehouse_id,
            WarehouseStock.product_id == product_id,
        )
        .with_for_update()
    )
    stock = db.scalars(stmt).first()
    if stock is None:
        stock = WarehouseStock(
            warehouse_id=warehouse_id,
            product_id=product_id,
            physical_qty=0,
            available_qty=0,
            reserved_fbs_qty=0,
        )
        db.add(stock)
        db.flush()
    return stock


def _assert_stock_invariant(stock: WarehouseStock) -> None:
    """Verify node-level stock accounting invariant before flushing to DB."""
    if stock.available_qty < 0 or stock.reserved_fbs_qty < 0 or stock.physical_qty < 0:
        raise DomainError(
            "Нарушение инварианта: количество на складе не может стать отрицательным",
            code="NEGATIVE_STOCK_FORBIDDEN",
            status_code=409,
        )
    if stock.physical_qty != stock.available_qty + stock.reserved_fbs_qty:
        raise DomainError(
            "Нарушение инварианта узла: physical_qty != available_qty + reserved_fbs_qty",
            code="STOCK_INVARIANT_VIOLATION",
            status_code=500,
        )


def _find_existing_ops_by_idempotency_key(
    db: Session,
    key: str | None,
    expected_type: OperationType,
) -> list[StockOperation]:
    if not key:
        return []
    cleaned = key.strip()
    if not cleaned:
        return []
    stmt = (
        select(StockOperation)
        .options(joinedload(StockOperation.product))
        .where(
            (StockOperation.idempotency_key == cleaned)
            | (StockOperation.idempotency_key.like(f"{cleaned}:part%"))
        )
        .order_by(StockOperation.id.asc())
    )
    ops = list(db.scalars(stmt).all())
    if ops and any(op.operation_type != expected_type.value for op in ops):
        raise DomainError(
            f"Ключ идемпотентности «{cleaned}» уже был использован для другого типа операции.",
            code="IDEMPOTENCY_KEY_CONFLICT",
            status_code=409,
        )
    return ops


def _find_warehouse_by_name_casefold(
    db: Session,
    name: str,
    *,
    exclude_id: int | None = None,
) -> Warehouse | None:
    """Case-insensitive Unicode/Cyrillic warehouse lookup across SQLite and PostgreSQL."""
    target = name.strip().casefold()
    for wh in db.scalars(select(Warehouse).order_by(Warehouse.id.asc())).all():
        if exclude_id is not None and wh.id == exclude_id:
            continue
        if wh.name.casefold() == target:
            return wh
    return None


def _next_document_number(db: Session, model: Any, prefix: str) -> str:
    max_id = db.scalar(select(func.coalesce(func.max(model.id), 0))) or 0
    return f"{prefix}-{max_id + 1:04d}"


def _build_shortage_plan(
    db: Session,
    *,
    operation_kind: str,
    product: Product,
    selected_warehouse: Warehouse,
    requested_qty: int,
) -> ShortageResolutionPlan:
    """Compute a deterministic shortage resolution plan across all active FF warehouses."""
    active_warehouses = list(
        db.scalars(
            select(Warehouse).where(Warehouse.is_deleted.is_(False)).order_by(Warehouse.id.asc())
        ).all()
    )
    stock_rows = {
        row.warehouse_id: row
        for row in db.scalars(
            select(WarehouseStock).where(WarehouseStock.product_id == product.id)
        ).all()
    }

    selected_avail = stock_rows.get(selected_warehouse.id).available_qty if selected_warehouse.id in stock_rows else 0
    deficit = max(0, requested_qty - selected_avail)

    total_network_avail = sum(
        (stock_rows[w.id].available_qty if w.id in stock_rows else 0) for w in active_warehouses
    )
    can_resolve = total_network_avail >= requested_qty

    split_allocations: list[ShortageAllocationItem] = []
    replenishment_transfers: list[TransferReplenishmentSuggestion] = []

    if can_resolve:
        remaining_for_split = requested_qty
        if selected_avail > 0:
            take_primary = min(selected_avail, remaining_for_split)
            split_allocations.append(
                ShortageAllocationItem(
                    warehouse_id=selected_warehouse.id,
                    warehouse_name=selected_warehouse.name,
                    available_qty=selected_avail,
                    allocate_qty=take_primary,
                    is_primary=True,
                )
            )
            remaining_for_split -= take_primary

        remaining_for_transfer = deficit
        # Sort other active warehouses by available stock descending so we take from richest donor first
        donor_warehouses = [w for w in active_warehouses if w.id != selected_warehouse.id]
        donor_warehouses.sort(
            key=lambda w: stock_rows[w.id].available_qty if w.id in stock_rows else 0,
            reverse=True,
        )

        for donor in donor_warehouses:
            donor_avail = stock_rows[donor.id].available_qty if donor.id in stock_rows else 0
            if donor_avail <= 0:
                continue
            if remaining_for_split > 0:
                take_donor = min(donor_avail, remaining_for_split)
                split_allocations.append(
                    ShortageAllocationItem(
                        warehouse_id=donor.id,
                        warehouse_name=donor.name,
                        available_qty=donor_avail,
                        allocate_qty=take_donor,
                        is_primary=False,
                    )
                )
                remaining_for_split -= take_donor

            if remaining_for_transfer > 0:
                move_qty = min(donor_avail, remaining_for_transfer)
                replenishment_transfers.append(
                    TransferReplenishmentSuggestion(
                        from_warehouse_id=donor.id,
                        from_warehouse_name=donor.name,
                        to_warehouse_id=selected_warehouse.id,
                        to_warehouse_name=selected_warehouse.name,
                        transfer_qty=move_qty,
                    )
                )
                remaining_for_transfer -= move_qty

        donor_summary = ", ".join(
            f"ФФ «{item.from_warehouse_name}» ({item.transfer_qty} шт.)"
            for item in replenishment_transfers
        )
        message = (
            f"На выбранном ФФ «{selected_warehouse.name}» доступно только {selected_avail} шт. "
            f"{product.sku} ({product.name}), не хватает {deficit} шт. для запроса на {requested_qty} шт. "
            f"Однако общий доступный остаток по всем активным ФФ достаточен ({total_network_avail} шт.). "
            f"Доступно разрешение: (1) Авто-разделение по складам или "
            f"(2) Перемещение недостающих {deficit} шт. с {donor_summary}."
        )
        code = "SHORTAGE_ON_SELECTED_FF"
    else:
        global_deficit = requested_qty - total_network_avail
        message = (
            f"Недостаточно товара даже с учётом всех активных ФФ: запрошено {requested_qty} шт. "
            f"{product.sku} ({product.name}), на ФФ «{selected_warehouse.name}» доступно {selected_avail} шт., "
            f"суммарно на всех активных ФФ доступно {total_network_avail} шт. (глобальный дефицит: {global_deficit} шт.). "
            f"Оформите приход товара."
        )
        code = "INSUFFICIENT_TOTAL_STOCK"

    return ShortageResolutionPlan(
        code=code,  # type: ignore[arg-type]
        operation_kind=operation_kind,  # type: ignore[arg-type]
        product_id=product.id,
        sku=product.sku,
        product_name=product.name,
        requested_warehouse_id=selected_warehouse.id,
        requested_warehouse_name=selected_warehouse.name,
        requested_qty=requested_qty,
        available_on_selected_qty=selected_avail,
        deficit_on_selected_qty=deficit,
        total_available_across_network_qty=total_network_avail,
        can_resolve_across_network=can_resolve,
        message=message,
        split_allocations=split_allocations,
        replenishment_transfers=replenishment_transfers,
    )


# ---------------------------------------------------------------------------
# Public Deep Module Interface: Warehouse Management
# ---------------------------------------------------------------------------


def create_warehouse(db: Session, req: WarehouseCreateRequest) -> dict[str, Any]:
    """Create a new active FF warehouse and initialize zero stock rows for all products."""
    existing = _find_warehouse_by_name_casefold(db, req.name)
    if existing:
        if existing.is_deleted:
            raise DomainError(
                f"Склад с именем «{existing.name}» уже существует в архиве удалённых ФФ. "
                f"Используйте другое название, чтобы сохранить прозрачность истории.",
                code="WAREHOUSE_EXISTS_IN_ARCHIVE",
                status_code=409,
            )
        raise DomainError(
            f"Склад ФФ с названием «{existing.name}» уже существует.",
            code="WAREHOUSE_NAME_DUPLICATE",
            status_code=409,
        )

    warehouse = Warehouse(name=req.name, is_deleted=False)
    db.add(warehouse)
    db.flush()

    products = list(db.scalars(select(Product).order_by(Product.id.asc())).all())
    for product in products:
        db.add(
            WarehouseStock(
                warehouse_id=warehouse.id,
                product_id=product.id,
                physical_qty=0,
                available_qty=0,
                reserved_fbs_qty=0,
            )
        )

    db.commit()
    db.refresh(warehouse)
    return {
        "warehouse": _serialize_warehouse(warehouse),
        "message": f"Склад ФФ «{warehouse.name}» успешно добавлен.",
    }


def rename_warehouse(db: Session, warehouse_id: int, req: WarehouseRenameRequest) -> dict[str, Any]:
    """Rename an existing FF warehouse without breaking any stock or operation links."""
    warehouse = _get_warehouse_or_raise(db, warehouse_id, allow_deleted=False)
    if warehouse.name == req.name:
        return {
            "warehouse": _serialize_warehouse(warehouse),
            "already_processed": True,
            "message": f"Название склада ФФ «{warehouse.name}» не изменилось.",
        }

    conflict = _find_warehouse_by_name_casefold(db, req.name, exclude_id=warehouse.id)
    if conflict:
        raise DomainError(
            f"Склад ФФ с названием «{req.name}» уже существует.",
            code="WAREHOUSE_NAME_DUPLICATE",
            status_code=409,
        )

    old_name = warehouse.name
    warehouse.name = req.name
    db.commit()
    db.refresh(warehouse)
    return {
        "warehouse": _serialize_warehouse(warehouse),
        "already_processed": False,
        "message": f"Склад ФФ переименован: «{old_name}» -> «{warehouse.name}».",
    }


def delete_warehouse(
    db: Session,
    warehouse_id: int,
    req: WarehouseDeleteRequest | None = None,
) -> dict[str, Any]:
    """Soft-delete (archive) an FF warehouse while preserving 100% of stock and operation history.

    Supports two transparent modes:
    1. If `transfer_to_warehouse_id` is specified: immediately moves all `available_qty > 0`
       to the target active FF warehouse and logs `WAREHOUSE_ARCHIVE_TRANSFER` operations.
    2. If `transfer_to_warehouse_id` is None: keeps current stock (`physical_qty`, `available_qty`,
       `reserved_fbs_qty`) explicitly accounted on the archived FF balance («Архив ФФ: <Название>»),
       visible in the dedicated dashboard block, from where it can be transferred at any time.
    """
    req = req or WarehouseDeleteRequest()
    warehouse = _get_warehouse_or_raise(db, warehouse_id, allow_deleted=True)
    if warehouse.is_deleted:
        return {
            "warehouse": _serialize_warehouse(warehouse),
            "already_processed": True,
            "transferred_items": [],
            "retained_items": _get_warehouse_stock_breakdown(db, warehouse.id),
            "message": f"Повторное удаление проигнорировано: ФФ «{warehouse.name}» уже находится в архиве.",
        }

    # Check if there are incoming unreceived transfers heading INTO this warehouse
    incoming_transfers = list(
        db.scalars(
            select(Transfer).where(
                Transfer.to_warehouse_id == warehouse.id,
                Transfer.status == TransferStatus.IN_TRANSIT.value,
            )
        ).all()
    )
    if incoming_transfers:
        raise DomainError(
            f"Нельзя удалить ФФ «{warehouse.name}», пока в его адрес числятся непринятые перемещения "
            f"в пути ({len(incoming_transfers)} шт.). Сначала примите перемещение.",
            code="WAREHOUSE_HAS_INCOMING_TRANSFERS",
            status_code=409,
        )

    target_warehouse: Warehouse | None = None
    if req.transfer_to_warehouse_id is not None:
        if req.transfer_to_warehouse_id == warehouse.id:
            raise DomainError(
                "Склад-получатель остатка должен отличаться от удаляемого склада.",
                code="INVALID_TARGET_WAREHOUSE",
                status_code=400,
            )
        target_warehouse = _get_warehouse_or_raise(db, req.transfer_to_warehouse_id, allow_deleted=False)

    stocks = list(
        db.scalars(
            select(WarehouseStock)
            .options(joinedload(WarehouseStock.product))
            .where(WarehouseStock.warehouse_id == warehouse.id)
            .with_for_update()
        ).all()
    )

    transferred_items: list[dict[str, Any]] = []
    if target_warehouse is not None:
        for src_stock in stocks:
            if src_stock.available_qty > 0:
                move_qty = src_stock.available_qty
                dst_stock = _lock_or_create_stock(db, target_warehouse.id, src_stock.product_id)

                src_stock.available_qty -= move_qty
                src_stock.physical_qty -= move_qty
                _assert_stock_invariant(src_stock)

                dst_stock.available_qty += move_qty
                dst_stock.physical_qty += move_qty
                _assert_stock_invariant(dst_stock)

                op = StockOperation(
                    operation_type=OperationType.WAREHOUSE_ARCHIVE_TRANSFER.value,
                    product_id=src_stock.product_id,
                    qty=move_qty,
                    from_location=f"ФФ {warehouse.name} (удаление)",
                    to_location=f"ФФ {target_warehouse.name}",
                    reference_id=f"DEL-FF-{warehouse.id}",
                    note=req.note
                    or f"Автоматический перевод доступного остатка при удалении ФФ «{warehouse.name}» на ФФ «{target_warehouse.name}»",
                )
                db.add(op)
                transferred_items.append(
                    {
                        "sku": src_stock.product.sku,
                        "product_name": src_stock.product.name,
                        "qty": move_qty,
                        "to_warehouse_name": target_warehouse.name,
                    }
                )

    warehouse.is_deleted = True
    warehouse.deleted_at = utcnow()
    retained_after = _get_warehouse_stock_breakdown(db, warehouse.id)

    if target_warehouse is not None and transferred_items:
        if retained_after:
            warehouse.archive_note = (
                f"Свободный остаток переведён на ФФ «{target_warehouse.name}»; "
                f"резерв FBS сохранён за архивным ФФ «{warehouse.name}» до завершения заказов."
            )
        else:
            warehouse.archive_note = f"Удалён с полным переводом остатка на ФФ «{target_warehouse.name}»."
    elif retained_after:
        warehouse.archive_note = (
            req.note
            or f"Склад удалён. Товар сохранён и числится на архивном балансе «Архив ФФ: {warehouse.name}»."
        )
    else:
        warehouse.archive_note = req.note or "Склад удалён (остатков на момент удаления не было)."

    db.commit()
    db.refresh(warehouse)

    return {
        "warehouse": _serialize_warehouse(warehouse),
        "already_processed": False,
        "transferred_items": transferred_items,
        "retained_items": retained_after,
        "message": (
            f"ФФ «{warehouse.name}» переведён в архив (история и остатки сохранены). "
            + (
                f"Переведено позиций на ФФ «{target_warehouse.name}»: {len(transferred_items)}."
                if target_warehouse and transferred_items
                else (
                    f"За архивным балансом «Архив ФФ: {warehouse.name}» числится позиций: {len(retained_after)}."
                    if retained_after
                    else "Остатков на складе не было."
                )
            )
        ),
    }


def evacuate_archived_warehouse_stock(
    db: Session,
    archived_warehouse_id: int,
    target_warehouse_id: int,
) -> dict[str, Any]:
    """Transfer all remaining available stock from a deleted (archived) FF to an active FF."""
    src_warehouse = _get_warehouse_or_raise(db, archived_warehouse_id, allow_deleted=True)
    if not src_warehouse.is_deleted:
        raise DomainError(
            f"Склад «{src_warehouse.name}» активен. Для активных складов используйте стандартное перемещение между ФФ.",
            code="WAREHOUSE_NOT_ARCHIVED",
            status_code=400,
        )
    dst_warehouse = _get_warehouse_or_raise(db, target_warehouse_id, allow_deleted=False)

    stocks = list(
        db.scalars(
            select(WarehouseStock)
            .options(joinedload(WarehouseStock.product))
            .where(WarehouseStock.warehouse_id == src_warehouse.id)
            .with_for_update()
        ).all()
    )

    moved: list[dict[str, Any]] = []
    for src_stock in stocks:
        if src_stock.available_qty > 0:
            qty = src_stock.available_qty
            dst_stock = _lock_or_create_stock(db, dst_warehouse.id, src_stock.product_id)

            src_stock.available_qty -= qty
            src_stock.physical_qty -= qty
            _assert_stock_invariant(src_stock)

            dst_stock.available_qty += qty
            dst_stock.physical_qty += qty
            _assert_stock_invariant(dst_stock)

            db.add(
                StockOperation(
                    operation_type=OperationType.WAREHOUSE_ARCHIVE_TRANSFER.value,
                    product_id=src_stock.product_id,
                    qty=qty,
                    from_location=f"Архив ФФ: {src_warehouse.name}",
                    to_location=f"ФФ {dst_warehouse.name}",
                    reference_id=f"EVAC-FF-{src_warehouse.id}",
                    note=f"Перевод остатка с удалённого (архивного) ФФ «{src_warehouse.name}» на действующий ФФ «{dst_warehouse.name}»",
                )
            )
            moved.append(
                {
                    "sku": src_stock.product.sku,
                    "product_name": src_stock.product.name,
                    "qty": qty,
                    "to_warehouse_name": dst_warehouse.name,
                }
            )

    if not moved:
        return {
            "already_processed": True,
            "moved_items": [],
            "message": f"На архивном ФФ «{src_warehouse.name}» нет свободного остатка для перевода.",
        }

    retained_after = _get_warehouse_stock_breakdown(db, src_warehouse.id)
    if retained_after:
        src_warehouse.archive_note = (
            f"Доступный остаток переведён на действующий ФФ «{dst_warehouse.name}»; "
            f"резерв FBS сохранён за архивным ФФ «{src_warehouse.name}» до завершения заказов."
        )
    else:
        src_warehouse.archive_note = f"Доступный остаток переведён на действующий ФФ «{dst_warehouse.name}»."
    db.commit()
    return {
        "already_processed": False,
        "moved_items": moved,
        "message": f"Остаток с архивного ФФ «{src_warehouse.name}» успешно переведён на ФФ «{dst_warehouse.name}».",
    }


# ---------------------------------------------------------------------------
# Public Deep Module Interface: Stock Receipts & Movements
# ---------------------------------------------------------------------------


def record_receipt(db: Session, req: ReceiptCreateRequest) -> dict[str, Any]:
    """Record incoming goods receipt (Приход товара на ФФ) with idempotency protection."""
    existing_ops = _find_existing_ops_by_idempotency_key(db, req.idempotency_key, OperationType.RECEIPT)
    if existing_ops:
        return {
            "operation": _serialize_operation(existing_ops[0]),
            "already_processed": True,
            "message": "Повторный запрос прихода проигнорирован по ключу идемпотентности (остаток не задвоен).",
        }

    product = _get_product_or_raise(db, req.product_id)
    warehouse = _get_warehouse_or_raise(db, req.warehouse_id, allow_deleted=False)
    stock = _lock_or_create_stock(db, warehouse.id, product.id)

    stock.physical_qty += req.qty
    stock.available_qty += req.qty
    _assert_stock_invariant(stock)

    ref_id = _next_document_number(db, StockOperation, "IN")
    op = StockOperation(
        idempotency_key=req.idempotency_key.strip() if req.idempotency_key and req.idempotency_key.strip() else None,
        operation_type=OperationType.RECEIPT.value,
        product_id=product.id,
        qty=req.qty,
        from_location=req.supplier_source.strip() or "Поставщик",
        to_location=f"ФФ {warehouse.name}",
        reference_id=ref_id,
        note=req.note or f"Приход товара на склад ФФ «{warehouse.name}»",
    )
    db.add(op)
    db.commit()
    db.refresh(op)

    return {
        "operation": _serialize_operation(op),
        "already_processed": False,
        "message": f"Оформлен приход: {product.sku} ({product.name}) — {req.qty} шт. на ФФ «{warehouse.name}».",
    }


def create_transfer(db: Session, req: TransferCreateRequest) -> dict[str, Any]:
    """Send an inter-warehouse transfer (`IN_TRANSIT` between FF warehouses)."""
    existing_ops = _find_existing_ops_by_idempotency_key(db, req.idempotency_key, OperationType.TRANSFER_SENT)
    if existing_ops:
        existing_transfer = db.scalars(
            select(Transfer)
            .options(
                joinedload(Transfer.product),
                joinedload(Transfer.from_warehouse),
                joinedload(Transfer.to_warehouse),
            )
            .where(Transfer.transfer_number == existing_ops[0].reference_id)
        ).first()
        return {
            "transfer": _serialize_transfer(existing_transfer) if existing_transfer else None,
            "already_processed": True,
            "message": "Повторная отправка перемещения проигнорирована по ключу идемпотентности.",
        }

    if req.from_warehouse_id == req.to_warehouse_id:
        raise DomainError(
            "Склад-отправитель и склад-получатель перемещения должны различаться.",
            code="SAME_WAREHOUSE_TRANSFER",
            status_code=400,
        )

    product = _get_product_or_raise(db, req.product_id)
    from_wh = _get_warehouse_or_raise(db, req.from_warehouse_id, allow_deleted=False)
    to_wh = _get_warehouse_or_raise(db, req.to_warehouse_id, allow_deleted=False)

    src_stock = _lock_or_create_stock(db, from_wh.id, product.id)
    if src_stock.available_qty < req.qty:
        plan = _build_shortage_plan(
            db,
            operation_kind="TRANSFER",
            product=product,
            selected_warehouse=from_wh,
            requested_qty=req.qty,
        )
        raise ShortageError(plan)

    src_stock.available_qty -= req.qty
    src_stock.physical_qty -= req.qty
    _assert_stock_invariant(src_stock)

    # Ensure destination stock row exists
    _lock_or_create_stock(db, to_wh.id, product.id)

    transfer_number = _next_document_number(db, Transfer, "TR")
    transfer = Transfer(
        transfer_number=transfer_number,
        product_id=product.id,
        from_warehouse_id=from_wh.id,
        to_warehouse_id=to_wh.id,
        qty=req.qty,
        status=TransferStatus.IN_TRANSIT.value,
        note=req.note or f"Перемещение между ФФ «{from_wh.name}» -> «{to_wh.name}»",
    )
    db.add(transfer)
    db.flush()

    op = StockOperation(
        idempotency_key=req.idempotency_key.strip() if req.idempotency_key and req.idempotency_key.strip() else None,
        operation_type=OperationType.TRANSFER_SENT.value,
        product_id=product.id,
        qty=req.qty,
        from_location=f"ФФ {from_wh.name}",
        to_location=f"В пути между ФФ (-> ФФ {to_wh.name})",
        reference_id=transfer.transfer_number,
        note=transfer.note,
    )
    db.add(op)
    db.commit()
    db.refresh(transfer)

    return {
        "transfer": _serialize_transfer(transfer),
        "already_processed": False,
        "message": (
            f"Перемещение {transfer.transfer_number} отправлено: {product.sku} ({req.qty} шт.) "
            f"списано с ФФ «{from_wh.name}» и числится в пути на ФФ «{to_wh.name}»."
        ),
    }


def receive_transfer(db: Session, transfer_id: int) -> dict[str, Any]:
    """Confirm receipt of an inter-warehouse transfer (`IN_TRANSIT` -> `RECEIVED`).

    Idempotent: a repeated confirmation on an already `RECEIVED` transfer returns
    `already_processed=True` without adding stock a second time.
    """
    stmt = (
        select(Transfer)
        .options(
            joinedload(Transfer.product),
            joinedload(Transfer.from_warehouse),
            joinedload(Transfer.to_warehouse),
        )
        .where(Transfer.id == transfer_id)
        .with_for_update()
    )
    transfer = db.scalars(stmt).first()
    if not transfer:
        raise DomainError(f"Перемещение с ID={transfer_id} не найдено", code="TRANSFER_NOT_FOUND", status_code=404)

    if transfer.status == TransferStatus.RECEIVED.value:
        return {
            "transfer": _serialize_transfer(transfer),
            "already_processed": True,
            "message": (
                f"Повторное подтверждение проигнорировано: перемещение {transfer.transfer_number} "
                f"уже было принято ранее. Товар повторно не начислен."
            ),
        }

    dst_stock = _lock_or_create_stock(db, transfer.to_warehouse_id, transfer.product_id)
    dst_stock.physical_qty += transfer.qty
    dst_stock.available_qty += transfer.qty
    _assert_stock_invariant(dst_stock)

    transfer.status = TransferStatus.RECEIVED.value
    transfer.received_at = utcnow()

    op = StockOperation(
        operation_type=OperationType.TRANSFER_RECEIVED.value,
        product_id=transfer.product_id,
        qty=transfer.qty,
        from_location=f"В пути между ФФ (из ФФ {transfer.from_warehouse.name})",
        to_location=f"ФФ {transfer.to_warehouse.name}",
        reference_id=transfer.transfer_number,
        note=f"Приёмка перемещения {transfer.transfer_number} на склад ФФ «{transfer.to_warehouse.name}»",
    )
    db.add(op)
    db.commit()
    db.refresh(transfer)

    return {
        "transfer": _serialize_transfer(transfer),
        "already_processed": False,
        "message": (
            f"Перемещение {transfer.transfer_number} принято: {transfer.product.sku} ({transfer.qty} шт.) "
            f"зачислено в доступный остаток ФФ «{transfer.to_warehouse.name}»."
        ),
    }


# ---------------------------------------------------------------------------
# Public Deep Module Interface: FBS Reservations, Shipments, and Cancellations
# ---------------------------------------------------------------------------


def create_fbs_reserve(db: Session, req: FBSReserveCreateRequest) -> dict[str, Any]:
    """Reserve stock on FF for an FBS order (`available_qty` -> `reserved_fbs_qty`).

    If `available_qty < req.qty` on the selected warehouse:
    - When `allow_split=False`: raises `ShortageError` with a `ShortageResolutionPlan`
      (showing whether network stock is sufficient, exact split allocation, and transfer option).
    - When `allow_split=True` and network stock is sufficient: atomically creates split FBS
      reservations across the primary and donor warehouses in one transaction.
    """
    existing_ops = _find_existing_ops_by_idempotency_key(db, req.idempotency_key, OperationType.FBS_RESERVED)
    if existing_ops:
        ref_ids = [op.reference_id for op in existing_ops if op.reference_id]
        existing_orders = list(
            db.scalars(
                select(FBSOrder)
                .options(joinedload(FBSOrder.product), joinedload(FBSOrder.warehouse))
                .where(FBSOrder.order_number.in_(ref_ids))
                .order_by(FBSOrder.id.asc())
            ).all()
        )
        return {
            "orders": [_serialize_fbs_order(o) for o in existing_orders],
            "already_processed": True,
            "split_applied": len(existing_orders) > 1,
            "message": "Повторное создание резерва FBS проигнорировано по ключу идемпотентности.",
        }

    product = _get_product_or_raise(db, req.product_id)
    warehouse = _get_warehouse_or_raise(db, req.warehouse_id, allow_deleted=False)
    stock = _lock_or_create_stock(db, warehouse.id, product.id)

    if stock.available_qty >= req.qty:
        stock.available_qty -= req.qty
        stock.reserved_fbs_qty += req.qty
        _assert_stock_invariant(stock)

        order_num = _next_document_number(db, FBSOrder, "FBS")
        order = FBSOrder(
            order_number=order_num,
            product_id=product.id,
            warehouse_id=warehouse.id,
            qty=req.qty,
            status=FBSOrderStatus.RESERVED.value,
            note=req.note or f"Резерв FBS на складе ФФ «{warehouse.name}»",
        )
        db.add(order)
        db.flush()

        op = StockOperation(
            idempotency_key=req.idempotency_key.strip() if req.idempotency_key and req.idempotency_key.strip() else None,
            operation_type=OperationType.FBS_RESERVED.value,
            product_id=product.id,
            qty=req.qty,
            from_location=f"ФФ {warehouse.name} (Доступно)",
            to_location=f"ФФ {warehouse.name} (Резерв FBS)",
            reference_id=order.order_number,
            note=order.note,
        )
        db.add(op)
        db.commit()
        db.refresh(order)

        return {
            "orders": [_serialize_fbs_order(order)],
            "already_processed": False,
            "split_applied": False,
            "message": (
                f"Создан резерв {order.order_number}: {product.sku} ({req.qty} шт.) "
                f"заблокирован в резерве FBS на ФФ «{warehouse.name}»."
            ),
        }

    # Shortage on selected FF warehouse
    plan = _build_shortage_plan(
        db,
        operation_kind="FBS",
        product=product,
        selected_warehouse=warehouse,
        requested_qty=req.qty,
    )
    if not req.allow_split or not plan.can_resolve_across_network:
        raise ShortageError(plan)

    # Atomic Split Fulfillment across warehouses
    created_orders: list[FBSOrder] = []
    base_id = db.scalar(select(func.coalesce(func.max(FBSOrder.id), 0))) or 0
    for idx, alloc in enumerate(plan.split_allocations, start=1):
        alloc_stock = _lock_or_create_stock(db, alloc.warehouse_id, product.id)
        if alloc_stock.available_qty < alloc.allocate_qty:
            raise DomainError(
                f"Конкурентное изменение остатка на ФФ «{alloc.warehouse_name}», повторите операцию.",
                code="CONCURRENT_STOCK_CHANGE",
                status_code=409,
            )
        alloc_stock.available_qty -= alloc.allocate_qty
        alloc_stock.reserved_fbs_qty += alloc.allocate_qty
        _assert_stock_invariant(alloc_stock)

        order_num = f"FBS-{base_id + idx:04d}"
        split_note = (
            f"Авто-разделение резерва FBS ({req.qty} шт. суммарно): "
            f"{alloc.allocate_qty} шт. с ФФ «{alloc.warehouse_name}»"
        )
        order = FBSOrder(
            order_number=order_num,
            product_id=product.id,
            warehouse_id=alloc.warehouse_id,
            qty=alloc.allocate_qty,
            status=FBSOrderStatus.RESERVED.value,
            note=req.note or split_note,
        )
        db.add(order)
        db.flush()

        idem_key = (
            f"{req.idempotency_key.strip()}:part{idx}"
            if idx > 1 and req.idempotency_key and req.idempotency_key.strip()
            else (req.idempotency_key.strip() if req.idempotency_key and req.idempotency_key.strip() else None)
        )
        db.add(
            StockOperation(
                idempotency_key=idem_key,
                operation_type=OperationType.FBS_RESERVED.value,
                product_id=product.id,
                qty=alloc.allocate_qty,
                from_location=f"ФФ {alloc.warehouse_name} (Доступно)",
                to_location=f"ФФ {alloc.warehouse_name} (Резерв FBS)",
                reference_id=order.order_number,
                note=split_note,
            )
        )
        created_orders.append(order)

    db.commit()
    for o in created_orders:
        db.refresh(o)

    breakdown_str = " + ".join(f"{a.allocate_qty} шт. (ФФ «{a.warehouse_name}»)" for a in plan.split_allocations)
    return {
        "orders": [_serialize_fbs_order(o) for o in created_orders],
        "already_processed": False,
        "split_applied": True,
        "message": f"Нехватка на одном ФФ разрешена авто-разделением резерва FBS ({req.qty} шт.): {breakdown_str}.",
    }


def ship_fbs_order(db: Session, order_id: int) -> dict[str, Any]:
    """Ship a reserved FBS order to customer (`RESERVED` -> `SHIPPED`).

    Decrements both `reserved_fbs_qty` and `physical_qty` on the FF warehouse.
    Idempotent: repeated calls on a `SHIPPED` order do not deduct stock twice.
    """
    stmt = (
        select(FBSOrder)
        .options(joinedload(FBSOrder.product), joinedload(FBSOrder.warehouse))
        .where(FBSOrder.id == order_id)
        .with_for_update()
    )
    order = db.scalars(stmt).first()
    if not order:
        raise DomainError(f"Заказ FBS с ID={order_id} не найден", code="FBS_ORDER_NOT_FOUND", status_code=404)

    if order.status == FBSOrderStatus.SHIPPED.value:
        return {
            "order": _serialize_fbs_order(order),
            "already_processed": True,
            "message": (
                f"Повторное подтверждение проигнорировано: заказ {order.order_number} "
                f"уже отправлен клиенту. Повторного списания товара не произошло."
            ),
        }

    if order.status == FBSOrderStatus.CANCELLED.value:
        raise DomainError(
            f"Нельзя отправить заказ {order.order_number}, так как его резерв уже был отменён.",
            code="FBS_ORDER_ALREADY_CANCELLED",
            status_code=409,
        )

    stock = _lock_or_create_stock(db, order.warehouse_id, order.product_id)
    if stock.reserved_fbs_qty < order.qty or stock.physical_qty < order.qty:
        raise DomainError(
            f"Нарушение резерва на ФФ «{order.warehouse.name}» для заказа {order.order_number}.",
            code="FBS_RESERVE_MISMATCH",
            status_code=409,
        )

    stock.reserved_fbs_qty -= order.qty
    stock.physical_qty -= order.qty
    _assert_stock_invariant(stock)

    now = utcnow()
    order.status = FBSOrderStatus.SHIPPED.value
    order.updated_at = now
    order.shipped_at = now

    db.add(
        StockOperation(
            operation_type=OperationType.FBS_SHIPPED.value,
            product_id=order.product_id,
            qty=order.qty,
            from_location=f"ФФ {order.warehouse.name} (Резерв FBS)",
            to_location="В пути к клиенту по FBS",
            reference_id=order.order_number,
            note=f"Отправка заказа {order.order_number} клиенту по FBS со склада ФФ «{order.warehouse.name}»",
        )
    )
    db.commit()
    db.refresh(order)

    return {
        "order": _serialize_fbs_order(order),
        "already_processed": False,
        "message": (
            f"Заказ {order.order_number} отправлен клиенту по FBS: {order.product.sku} ({order.qty} шт.) "
            f"списан с физического остатка и резерва ФФ «{order.warehouse.name}»."
        ),
    }


def cancel_fbs_order(db: Session, order_id: int) -> dict[str, Any]:
    """Cancel an FBS reservation (`RESERVED` -> `CANCELLED`), releasing stock back to `available_qty`.

    Idempotent: repeated cancellation of an already `CANCELLED` order is a safe no-op.
    """
    stmt = (
        select(FBSOrder)
        .options(joinedload(FBSOrder.product), joinedload(FBSOrder.warehouse))
        .where(FBSOrder.id == order_id)
        .with_for_update()
    )
    order = db.scalars(stmt).first()
    if not order:
        raise DomainError(f"Заказ FBS с ID={order_id} не найден", code="FBS_ORDER_NOT_FOUND", status_code=404)

    if order.status == FBSOrderStatus.CANCELLED.value:
        return {
            "order": _serialize_fbs_order(order),
            "already_processed": True,
            "message": (
                f"Повторная отмена проигнорирована: резерв {order.order_number} "
                f"уже был отменён ранее. Остаток повторно не изменён."
            ),
        }

    if order.status == FBSOrderStatus.SHIPPED.value:
        raise DomainError(
            f"Нельзя отменить резерв заказа {order.order_number}, так как товар уже отправлен клиенту.",
            code="FBS_ORDER_ALREADY_SHIPPED",
            status_code=409,
        )

    stock = _lock_or_create_stock(db, order.warehouse_id, order.product_id)
    if stock.reserved_fbs_qty < order.qty:
        raise DomainError(
            f"Некорректный резерв на ФФ «{order.warehouse.name}» для отмены заказа {order.order_number}.",
            code="FBS_RESERVE_MISMATCH",
            status_code=409,
        )

    stock.reserved_fbs_qty -= order.qty
    stock.available_qty += order.qty
    _assert_stock_invariant(stock)

    now = utcnow()
    order.status = FBSOrderStatus.CANCELLED.value
    order.updated_at = now
    order.cancelled_at = now

    db.add(
        StockOperation(
            operation_type=OperationType.FBS_CANCELLED.value,
            product_id=order.product_id,
            qty=order.qty,
            from_location=f"ФФ {order.warehouse.name} (Резерв FBS)",
            to_location=f"ФФ {order.warehouse.name} (Доступно)",
            reference_id=order.order_number,
            note=f"Отмена резерва FBS {order.order_number}: товар возвращён в доступный остаток ФФ «{order.warehouse.name}»",
        )
    )
    db.commit()
    db.refresh(order)

    return {
        "order": _serialize_fbs_order(order),
        "already_processed": False,
        "message": (
            f"Резерв {order.order_number} отменён: {order.product.sku} ({order.qty} шт.) "
            f"разблокирован и возвращён в доступный остаток ФФ «{order.warehouse.name}»."
        ),
    }


# ---------------------------------------------------------------------------
# Public Deep Module Interface: FBO Shipments and Receipts
# ---------------------------------------------------------------------------


def create_fbo_shipment(db: Session, req: FBOShipmentCreateRequest) -> dict[str, Any]:
    """Send an FBO shipment from FF to marketplace warehouse (`IN_TRANSIT` to FBO).

    Supports shortage detection (`ShortageError` with `ShortageResolutionPlan`) and
    atomic multi-warehouse split fulfillment when `allow_split=True`.
    """
    existing_ops = _find_existing_ops_by_idempotency_key(db, req.idempotency_key, OperationType.FBO_SENT)
    if existing_ops:
        ref_ids = [op.reference_id for op in existing_ops if op.reference_id]
        existing_shipments = list(
            db.scalars(
                select(FBOShipment)
                .options(joinedload(FBOShipment.product), joinedload(FBOShipment.warehouse))
                .where(FBOShipment.shipment_number.in_(ref_ids))
                .order_by(FBOShipment.id.asc())
            ).all()
        )
        return {
            "shipments": [_serialize_fbo_shipment(s) for s in existing_shipments],
            "already_processed": True,
            "split_applied": len(existing_shipments) > 1,
            "message": "Повторная отправка поставки FBO проигнорирована по ключу идемпотентности.",
        }

    product = _get_product_or_raise(db, req.product_id)
    warehouse = _get_warehouse_or_raise(db, req.warehouse_id, allow_deleted=False)
    stock = _lock_or_create_stock(db, warehouse.id, product.id)

    marketplace_label = req.marketplace_name.strip() or "Склад маркетплейса (WB FBO)"

    if stock.available_qty >= req.qty:
        stock.available_qty -= req.qty
        stock.physical_qty -= req.qty
        _assert_stock_invariant(stock)

        ship_num = _next_document_number(db, FBOShipment, "FBO")
        shipment = FBOShipment(
            shipment_number=ship_num,
            product_id=product.id,
            warehouse_id=warehouse.id,
            marketplace_name=marketplace_label,
            qty=req.qty,
            status=FBOShipmentStatus.IN_TRANSIT.value,
            note=req.note or f"Поставка по FBO со склада ФФ «{warehouse.name}» на «{marketplace_label}»",
        )
        db.add(shipment)
        db.flush()

        db.add(
            StockOperation(
                idempotency_key=req.idempotency_key.strip() if req.idempotency_key and req.idempotency_key.strip() else None,
                operation_type=OperationType.FBO_SENT.value,
                product_id=product.id,
                qty=req.qty,
                from_location=f"ФФ {warehouse.name}",
                to_location=f"В пути по FBO ({marketplace_label})",
                reference_id=shipment.shipment_number,
                note=shipment.note,
            )
        )
        db.commit()
        db.refresh(shipment)

        return {
            "shipments": [_serialize_fbo_shipment(shipment)],
            "already_processed": False,
            "split_applied": False,
            "message": (
                f"Поставка {shipment.shipment_number} отправлена по FBO: {product.sku} ({req.qty} шт.) "
                f"покинул ФФ «{warehouse.name}» и числится в пути на «{marketplace_label}»."
            ),
        }

    # Shortage on selected FF warehouse
    plan = _build_shortage_plan(
        db,
        operation_kind="FBO",
        product=product,
        selected_warehouse=warehouse,
        requested_qty=req.qty,
    )
    if not req.allow_split or not plan.can_resolve_across_network:
        raise ShortageError(plan)

    # Atomic Split FBO Shipment across warehouses
    created_shipments: list[FBOShipment] = []
    base_id = db.scalar(select(func.coalesce(func.max(FBOShipment.id), 0))) or 0
    for idx, alloc in enumerate(plan.split_allocations, start=1):
        alloc_stock = _lock_or_create_stock(db, alloc.warehouse_id, product.id)
        if alloc_stock.available_qty < alloc.allocate_qty:
            raise DomainError(
                f"Конкурентное изменение остатка на ФФ «{alloc.warehouse_name}», повторите операцию.",
                code="CONCURRENT_STOCK_CHANGE",
                status_code=409,
            )
        alloc_stock.available_qty -= alloc.allocate_qty
        alloc_stock.physical_qty -= alloc.allocate_qty
        _assert_stock_invariant(alloc_stock)

        ship_num = f"FBO-{base_id + idx:04d}"
        split_note = (
            f"Авто-разделение поставки FBO ({req.qty} шт. суммарно): "
            f"{alloc.allocate_qty} шт. с ФФ «{alloc.warehouse_name}»"
        )
        shipment = FBOShipment(
            shipment_number=ship_num,
            product_id=product.id,
            warehouse_id=alloc.warehouse_id,
            marketplace_name=marketplace_label,
            qty=alloc.allocate_qty,
            status=FBOShipmentStatus.IN_TRANSIT.value,
            note=req.note or split_note,
        )
        db.add(shipment)
        db.flush()

        idem_key = (
            f"{req.idempotency_key.strip()}:part{idx}"
            if idx > 1 and req.idempotency_key and req.idempotency_key.strip()
            else (req.idempotency_key.strip() if req.idempotency_key and req.idempotency_key.strip() else None)
        )
        db.add(
            StockOperation(
                idempotency_key=idem_key,
                operation_type=OperationType.FBO_SENT.value,
                product_id=product.id,
                qty=alloc.allocate_qty,
                from_location=f"ФФ {alloc.warehouse_name}",
                to_location=f"В пути по FBO ({marketplace_label})",
                reference_id=shipment.shipment_number,
                note=split_note,
            )
        )
        created_shipments.append(shipment)

    db.commit()
    for s in created_shipments:
        db.refresh(s)

    breakdown_str = " + ".join(f"{a.allocate_qty} шт. (ФФ «{a.warehouse_name}»)" for a in plan.split_allocations)
    return {
        "shipments": [_serialize_fbo_shipment(s) for s in created_shipments],
        "already_processed": False,
        "split_applied": True,
        "message": f"Нехватка на одном ФФ разрешена авто-разделением поставки FBO ({req.qty} шт.): {breakdown_str}.",
    }


def receive_fbo_shipment(db: Session, shipment_id: int) -> dict[str, Any]:
    """Confirm marketplace acceptance of an FBO shipment (`IN_TRANSIT` -> `RECEIVED`).

    Idempotent: repeated clicks on «Принять FBO» return `already_processed=True` without duplicate ledger entries.
    """
    stmt = (
        select(FBOShipment)
        .options(joinedload(FBOShipment.product), joinedload(FBOShipment.warehouse))
        .where(FBOShipment.id == shipment_id)
        .with_for_update()
    )
    shipment = db.scalars(stmt).first()
    if not shipment:
        raise DomainError(f"Поставка FBO с ID={shipment_id} не найдена", code="FBO_SHIPMENT_NOT_FOUND", status_code=404)

    if shipment.status == FBOShipmentStatus.RECEIVED.value:
        return {
            "shipment": _serialize_fbo_shipment(shipment),
            "already_processed": True,
            "message": (
                f"Повторное подтверждение проигнорировано: поставка {shipment.shipment_number} "
                f"уже была принята на складе маркетплейса ранее."
            ),
        }

    shipment.status = FBOShipmentStatus.RECEIVED.value
    shipment.received_at = utcnow()

    db.add(
        StockOperation(
            operation_type=OperationType.FBO_RECEIVED.value,
            product_id=shipment.product_id,
            qty=shipment.qty,
            from_location=f"В пути по FBO ({shipment.marketplace_name})",
            to_location=f"Принято на складе маркетплейса FBO ({shipment.marketplace_name})",
            reference_id=shipment.shipment_number,
            note=f"Подтверждена приёмка поставки {shipment.shipment_number} на складе маркетплейса FBO",
        )
    )
    db.commit()
    db.refresh(shipment)

    return {
        "shipment": _serialize_fbo_shipment(shipment),
        "already_processed": False,
        "message": (
            f"Поставка {shipment.shipment_number} принята маркетплейсом по FBO: "
            f"{shipment.product.sku} ({shipment.qty} шт.)."
        ),
    }


# ---------------------------------------------------------------------------
# Public Deep Module Interface: Reconciliation & Dashboard State
# ---------------------------------------------------------------------------


def check_reconciliation(db: Session) -> ReconciliationSummary:
    """Verify the Conservation of Stock Law for every SKU in the database.

    Formula:
        Total Receipts (RECEIPT) ==
            Physical Stock on Active FF +
            Physical Stock on Archived FF +
            In-Transit between FF (`Transfer.status == IN_TRANSIT`) +
            In-Transit FBO (`FBOShipment.status == IN_TRANSIT`) +
            Received on FBO (`FBOShipment.status == RECEIVED`) +
            Shipped to Customers via FBS (`FBSOrder.status == SHIPPED`)
    """
    products = list(db.scalars(select(Product).order_by(Product.sku.asc())).all())
    warehouses = {w.id: w for w in db.scalars(select(Warehouse)).all()}
    stocks = list(db.scalars(select(WarehouseStock)).all())
    transfers = list(db.scalars(select(Transfer)).all())
    fbs_orders = list(db.scalars(select(FBSOrder)).all())
    fbo_shipments = list(db.scalars(select(FBOShipment)).all())
    receipt_ops = list(
        db.scalars(
            select(StockOperation).where(StockOperation.operation_type == OperationType.RECEIPT.value)
        ).all()
    )

    items: list[ReconciliationSKUReport] = []
    all_balanced = True

    for product in products:
        total_receipts = sum(op.qty for op in receipt_ops if op.product_id == product.id)

        prod_stocks = [s for s in stocks if s.product_id == product.id]
        active_phys = sum(
            s.physical_qty for s in prod_stocks if s.warehouse_id in warehouses and not warehouses[s.warehouse_id].is_deleted
        )
        active_avail = sum(
            s.available_qty for s in prod_stocks if s.warehouse_id in warehouses and not warehouses[s.warehouse_id].is_deleted
        )
        active_res = sum(
            s.reserved_fbs_qty for s in prod_stocks if s.warehouse_id in warehouses and not warehouses[s.warehouse_id].is_deleted
        )

        arch_phys = sum(
            s.physical_qty for s in prod_stocks if s.warehouse_id in warehouses and warehouses[s.warehouse_id].is_deleted
        )
        arch_avail = sum(
            s.available_qty for s in prod_stocks if s.warehouse_id in warehouses and warehouses[s.warehouse_id].is_deleted
        )
        arch_res = sum(
            s.reserved_fbs_qty for s in prod_stocks if s.warehouse_id in warehouses and warehouses[s.warehouse_id].is_deleted
        )

        # Node invariant check: physical == available + reserved
        node_invariant_ok = all(
            s.physical_qty == s.available_qty + s.reserved_fbs_qty
            and s.physical_qty >= 0
            and s.available_qty >= 0
            and s.reserved_fbs_qty >= 0
            for s in prod_stocks
        )

        in_transit_ff = sum(
            t.qty
            for t in transfers
            if t.product_id == product.id and t.status == TransferStatus.IN_TRANSIT.value
        )
        in_transit_fbo = sum(
            f.qty
            for f in fbo_shipments
            if f.product_id == product.id and f.status == FBOShipmentStatus.IN_TRANSIT.value
        )
        received_fbo = sum(
            f.qty
            for f in fbo_shipments
            if f.product_id == product.id and f.status == FBOShipmentStatus.RECEIVED.value
        )
        shipped_fbs = sum(
            o.qty
            for o in fbs_orders
            if o.product_id == product.id and o.status == FBSOrderStatus.SHIPPED.value
        )

        total_ff_phys = active_phys + arch_phys
        accounted_total = total_ff_phys + in_transit_ff + in_transit_fbo + received_fbo + shipped_fbs
        discrepancy = total_receipts - accounted_total
        is_balanced = (discrepancy == 0) and node_invariant_ok
        if not is_balanced:
            all_balanced = False

        items.append(
            ReconciliationSKUReport(
                product_id=product.id,
                sku=product.sku,
                product_name=product.name,
                total_receipts_qty=total_receipts,
                active_ff_physical_qty=active_phys,
                archived_ff_physical_qty=arch_phys,
                total_ff_physical_qty=total_ff_phys,
                active_ff_available_qty=active_avail,
                active_ff_reserved_fbs_qty=active_res,
                archived_ff_available_qty=arch_avail,
                archived_ff_reserved_fbs_qty=arch_res,
                in_transit_ff_qty=in_transit_ff,
                in_transit_fbo_qty=in_transit_fbo,
                received_fbo_qty=received_fbo,
                shipped_fbs_qty=shipped_fbs,
                accounted_total_qty=accounted_total,
                discrepancy_qty=discrepancy,
                is_balanced=is_balanced,
            )
        )

    return ReconciliationSummary(
        all_balanced=all_balanced,
        formula=(
            "Сумма приходов = На складах ФФ (активные + архив) + В пути между ФФ "
            "+ В пути по FBO + Принято на FBO + Отправлено клиентам по FBS"
        ),
        checked_at=utcnow(),
        items=items,
    )


def get_dashboard_state(db: Session) -> dict[str, Any]:
    """Build the complete single-screen operational state for the manager UI and API."""
    products = list(db.scalars(select(Product).order_by(Product.sku.asc())).all())
    all_warehouses = list(db.scalars(select(Warehouse).order_by(Warehouse.id.asc())).all())
    active_warehouses = [w for w in all_warehouses if not w.is_deleted]
    archived_warehouses = [w for w in all_warehouses if w.is_deleted]

    stock_map: dict[tuple[int, int], WarehouseStock] = {
        (s.warehouse_id, s.product_id): s
        for s in db.scalars(select(WarehouseStock)).all()
    }

    transfers = list(
        db.scalars(
            select(Transfer)
            .options(
                joinedload(Transfer.product),
                joinedload(Transfer.from_warehouse),
                joinedload(Transfer.to_warehouse),
            )
            .order_by(Transfer.id.desc())
        ).all()
    )
    fbs_orders = list(
        db.scalars(
            select(FBSOrder)
            .options(joinedload(FBSOrder.product), joinedload(FBSOrder.warehouse))
            .order_by(FBSOrder.id.desc())
        ).all()
    )
    fbo_shipments = list(
        db.scalars(
            select(FBOShipment)
            .options(joinedload(FBOShipment.product), joinedload(FBOShipment.warehouse))
            .order_by(FBOShipment.id.desc())
        ).all()
    )
    operations = list(
        db.scalars(
            select(StockOperation)
            .options(joinedload(StockOperation.product))
            .order_by(StockOperation.id.desc())
            .limit(200)
        ).all()
    )

    reconciliation = check_reconciliation(db)
    rec_by_product = {item.product_id: item for item in reconciliation.items}

    # Build SKU Matrix rows across active FF warehouses + transit/shipped totals
    sku_matrix: list[dict[str, Any]] = []
    for product in products:
        rec = rec_by_product[product.id]
        per_warehouse: list[dict[str, Any]] = []
        for wh in active_warehouses:
            st = stock_map.get((wh.id, product.id))
            per_warehouse.append(
                {
                    "warehouse_id": wh.id,
                    "warehouse_name": wh.name,
                    "physical_qty": st.physical_qty if st else 0,
                    "available_qty": st.available_qty if st else 0,
                    "reserved_fbs_qty": st.reserved_fbs_qty if st else 0,
                }
            )

        archived_breakdown: list[dict[str, Any]] = []
        for wh in archived_warehouses:
            st = stock_map.get((wh.id, product.id))
            if st and (st.physical_qty > 0 or st.available_qty > 0 or st.reserved_fbs_qty > 0):
                archived_breakdown.append(
                    {
                        "warehouse_id": wh.id,
                        "warehouse_name": wh.name,
                        "physical_qty": st.physical_qty,
                        "available_qty": st.available_qty,
                        "reserved_fbs_qty": st.reserved_fbs_qty,
                    }
                )

        sku_matrix.append(
            {
                "product_id": product.id,
                "sku": product.sku,
                "name": product.name,
                "warehouses": per_warehouse,
                "archived_warehouses": archived_breakdown,
                "totals": {
                    "active_physical_qty": rec.active_ff_physical_qty,
                    "active_available_qty": rec.active_ff_available_qty,
                    "active_reserved_fbs_qty": rec.active_ff_reserved_fbs_qty,
                    "archived_physical_qty": rec.archived_ff_physical_qty,
                    "archived_available_qty": rec.archived_ff_available_qty,
                    "archived_reserved_fbs_qty": rec.archived_ff_reserved_fbs_qty,
                    "all_ff_physical_qty": rec.total_ff_physical_qty,
                    "in_transit_ff_qty": rec.in_transit_ff_qty,
                    "in_transit_fbo_qty": rec.in_transit_fbo_qty,
                    "received_fbo_qty": rec.received_fbo_qty,
                    "shipped_fbs_qty": rec.shipped_fbs_qty,
                    "total_receipts_qty": rec.total_receipts_qty,
                    "accounted_total_qty": rec.accounted_total_qty,
                    "is_balanced": rec.is_balanced,
                },
            }
        )

    # Dedicated summary of archived warehouses & where their stock is accounted
    archived_holdings: list[dict[str, Any]] = []
    for wh in archived_warehouses:
        items_held = _get_warehouse_stock_breakdown(db, wh.id)
        archived_holdings.append(
            {
                "warehouse": _serialize_warehouse(wh),
                "items": items_held,
                "total_physical_qty": sum(i["physical_qty"] for i in items_held),
                "total_available_qty": sum(i["available_qty"] for i in items_held),
                "total_reserved_fbs_qty": sum(i["reserved_fbs_qty"] for i in items_held),
            }
        )

    # Grand KPI cards
    kpi_summary = {
        "total_receipts_qty": sum(i.total_receipts_qty for i in reconciliation.items),
        "active_ff_physical_qty": sum(i.active_ff_physical_qty for i in reconciliation.items),
        "active_ff_available_qty": sum(i.active_ff_available_qty for i in reconciliation.items),
        "active_ff_reserved_fbs_qty": sum(i.active_ff_reserved_fbs_qty for i in reconciliation.items),
        "archived_ff_physical_qty": sum(i.archived_ff_physical_qty for i in reconciliation.items),
        "in_transit_ff_qty": sum(i.in_transit_ff_qty for i in reconciliation.items),
        "in_transit_fbo_qty": sum(i.in_transit_fbo_qty for i in reconciliation.items),
        "received_fbo_qty": sum(i.received_fbo_qty for i in reconciliation.items),
        "shipped_fbs_qty": sum(i.shipped_fbs_qty for i in reconciliation.items),
    }

    return {
        "shop_name": "Основной магазин CheckStock",
        "products": [{"id": p.id, "sku": p.sku, "name": p.name} for p in products],
        "active_warehouses": [_serialize_warehouse(w) for w in active_warehouses],
        "archived_warehouses": archived_holdings,
        "sku_matrix": sku_matrix,
        "kpi_summary": kpi_summary,
        "reconciliation": reconciliation.model_dump(mode="json"),
        "transfers": [_serialize_transfer(t) for t in transfers],
        "fbs_orders": [_serialize_fbs_order(o) for o in fbs_orders],
        "fbo_shipments": [_serialize_fbo_shipment(s) for s in fbo_shipments],
        "operations": [_serialize_operation(op) for op in operations],
    }


# ---------------------------------------------------------------------------
# Serialization Helpers
# ---------------------------------------------------------------------------


def _get_warehouse_stock_breakdown(db: Session, warehouse_id: int) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(WarehouseStock)
            .options(joinedload(WarehouseStock.product))
            .where(WarehouseStock.warehouse_id == warehouse_id)
            .order_by(WarehouseStock.product_id.asc())
        ).all()
    )
    result: list[dict[str, Any]] = []
    for r in rows:
        if r.physical_qty > 0 or r.available_qty > 0 or r.reserved_fbs_qty > 0:
            result.append(
                {
                    "product_id": r.product_id,
                    "sku": r.product.sku,
                    "product_name": r.product.name,
                    "physical_qty": r.physical_qty,
                    "available_qty": r.available_qty,
                    "reserved_fbs_qty": r.reserved_fbs_qty,
                }
            )
    return result


def _serialize_warehouse(w: Warehouse) -> dict[str, Any]:
    return {
        "id": w.id,
        "name": w.name,
        "is_deleted": w.is_deleted,
        "deleted_at": w.deleted_at.isoformat() if w.deleted_at else None,
        "archive_note": w.archive_note,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


def _serialize_transfer(t: Transfer) -> dict[str, Any]:
    return {
        "id": t.id,
        "transfer_number": t.transfer_number,
        "product_id": t.product_id,
        "sku": t.product.sku if t.product else None,
        "product_name": t.product.name if t.product else None,
        "from_warehouse_id": t.from_warehouse_id,
        "from_warehouse_name": t.from_warehouse.name if t.from_warehouse else None,
        "to_warehouse_id": t.to_warehouse_id,
        "to_warehouse_name": t.to_warehouse.name if t.to_warehouse else None,
        "qty": t.qty,
        "status": t.status,
        "note": t.note,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "received_at": t.received_at.isoformat() if t.received_at else None,
    }


def _serialize_fbs_order(o: FBSOrder) -> dict[str, Any]:
    return {
        "id": o.id,
        "order_number": o.order_number,
        "product_id": o.product_id,
        "sku": o.product.sku if o.product else None,
        "product_name": o.product.name if o.product else None,
        "warehouse_id": o.warehouse_id,
        "warehouse_name": o.warehouse.name if o.warehouse else None,
        "warehouse_is_deleted": o.warehouse.is_deleted if o.warehouse else False,
        "qty": o.qty,
        "status": o.status,
        "note": o.note,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "shipped_at": o.shipped_at.isoformat() if o.shipped_at else None,
        "cancelled_at": o.cancelled_at.isoformat() if o.cancelled_at else None,
    }


def _serialize_fbo_shipment(s: FBOShipment) -> dict[str, Any]:
    return {
        "id": s.id,
        "shipment_number": s.shipment_number,
        "product_id": s.product_id,
        "sku": s.product.sku if s.product else None,
        "product_name": s.product.name if s.product else None,
        "warehouse_id": s.warehouse_id,
        "warehouse_name": s.warehouse.name if s.warehouse else None,
        "marketplace_name": s.marketplace_name,
        "qty": s.qty,
        "status": s.status,
        "note": s.note,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "received_at": s.received_at.isoformat() if s.received_at else None,
    }


def _serialize_operation(op: StockOperation) -> dict[str, Any]:
    return {
        "id": op.id,
        "idempotency_key": op.idempotency_key,
        "operation_type": op.operation_type,
        "product_id": op.product_id,
        "sku": op.product.sku if op.product else None,
        "product_name": op.product.name if op.product else None,
        "qty": op.qty,
        "from_location": op.from_location,
        "to_location": op.to_location,
        "reference_id": op.reference_id,
        "note": op.note,
        "created_at": op.created_at.isoformat() if op.created_at else None,
    }
