"""Initial test data seeding (Тестовые данные из ТЗ CheckStock).

Creates:
- Два склада ФФ: «Север» и «Юг»
- Три артикула и начальный тестовый приход:
  * SKU-101 Органайзер: Север = 10, Юг = 6
  * SKU-102 Контейнер:  Север = 5,  Юг = 0
  * SKU-103 Корзина:    Север = 0,  Юг = 4
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ledger import record_receipt
from app.models import (
    FBOShipment,
    FBSOrder,
    Product,
    StockOperation,
    Transfer,
    Warehouse,
    WarehouseStock,
)
from app.schemas import ReceiptCreateRequest


SEED_WAREHOUSES = ["Север", "Юг"]

SEED_PRODUCTS = [
    {"sku": "SKU-101", "name": "Органайзер", "initial_stock": {"Север": 10, "Юг": 6}},
    {"sku": "SKU-102", "name": "Контейнер", "initial_stock": {"Север": 5, "Юг": 0}},
    {"sku": "SKU-103", "name": "Корзина", "initial_stock": {"Север": 0, "Юг": 4}},
]


def seed_initial_data(db: Session, *, force_reset: bool = False) -> bool:
    """Seed the database with the exact initial dataset required by the CheckStock specification.

    Returns True if seed data was inserted (or reset), False if database was already populated.
    """
    if force_reset:
        db.execute(delete(StockOperation))
        db.execute(delete(Transfer))
        db.execute(delete(FBSOrder))
        db.execute(delete(FBOShipment))
        db.execute(delete(WarehouseStock))
        db.execute(delete(Warehouse))
        db.execute(delete(Product))
        db.commit()
    else:
        existing_product = db.scalars(select(Product).limit(1)).first()
        if existing_product is not None:
            return False

    warehouses_by_name: dict[str, Warehouse] = {}
    for wh_name in SEED_WAREHOUSES:
        wh = Warehouse(name=wh_name, is_deleted=False)
        db.add(wh)
        db.flush()
        warehouses_by_name[wh_name] = wh

    products_by_sku: dict[str, Product] = {}
    for item in SEED_PRODUCTS:
        product = Product(sku=item["sku"], name=item["name"])
        db.add(product)
        db.flush()
        products_by_sku[item["sku"]] = product
        for wh in warehouses_by_name.values():
            db.add(
                WarehouseStock(
                    warehouse_id=wh.id,
                    product_id=product.id,
                    physical_qty=0,
                    available_qty=0,
                    reserved_fbs_qty=0,
                )
            )

    db.commit()

    # Record initial receipts through `LedgerModule.record_receipt` so every unit has an audit trail
    for item in SEED_PRODUCTS:
        product = products_by_sku[item["sku"]]
        stock_spec: dict[str, int] = item["initial_stock"]  # type: ignore[assignment]
        for wh_name, qty in stock_spec.items():
            if qty > 0:
                wh = warehouses_by_name[wh_name]
                record_receipt(
                    db,
                    ReceiptCreateRequest(
                        product_id=product.id,
                        warehouse_id=wh.id,
                        qty=qty,
                        supplier_source="Начальный тестовый приход (ТЗ CheckStock)",
                        note=f"Стартовый приход по ТЗ: {product.sku} ({product.name}) на ФФ «{wh.name}» ({qty} шт.)",
                        idempotency_key=f"seed:{product.sku}:{wh.name}",
                    ),
                )

    return True
