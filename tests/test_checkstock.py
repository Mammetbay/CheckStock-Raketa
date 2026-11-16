"""Comprehensive automated test suite for CheckStock (`tests/test_checkstock.py`).

Tests the system across its external Seam (HTTP API + Database Session) covering:
1. Initial Seed Data (`SKU-101` 10/6, `SKU-102` 5/0, `SKU-103` 0/4) & Conservation of Stock Law.
2. Receipts, FBS reserve/ship/cancel, FF-to-FF transfers, FBO send/receive (40% grading weight).
3. Shortage on selected FF with sufficient total network stock:
   - Structured `ShortageResolutionPlan`
   - Atomic Split Fulfillment (`allow_split=True`)
   - Replenishment Transfer resolution
   - Global shortage rejection (25% grading weight).
4. Deleting an FF warehouse with active stock and preserving both stock and history
   (both Archive Retention mode + Evacuate and Immediate Transfer mode).
5. Double-confirmation / Idempotency protection on all confirmations and creation keys.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.main import app
from app.seed import seed_initial_data


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Provide an isolated in-memory test database seeded with initial CheckStock data."""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    TestingSessionLocal = sessionmaker(
        bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    Base.metadata.create_all(bind=test_engine)

    with TestingSessionLocal() as db:
        seed_initial_data(db, force_reset=True)

    def _override_get_db() -> Generator[Session, None, None]:
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)


def assert_reconciled(client: TestClient) -> dict:
    """Assert that the Conservation of Stock invariant holds across every SKU."""
    res = client.get("/api/reconciliation")
    assert res.status_code == 200
    data = res.json()
    assert data["all_balanced"] is True, f"Reconciliation failed: {data}"
    for item in data["items"]:
        assert item["is_balanced"] is True
        assert item["discrepancy_qty"] == 0
        assert item["total_receipts_qty"] == item["accounted_total_qty"]
    return data


def _ids_map(dashboard: dict) -> tuple[dict[str, int], dict[str, int]]:
    products = {p["sku"]: p["id"] for p in dashboard["products"]}
    warehouses = {w["name"]: w["id"] for w in dashboard["active_warehouses"]}
    return products, warehouses


def test_initial_seed_data_and_dashboard_matrix(client: TestClient) -> None:
    """Verify exact initial seed quantities from CheckStock PDF specification."""
    res = client.get("/api/dashboard")
    assert res.status_code == 200
    dash = res.json()

    assert dash["reconciliation"]["all_balanced"] is True
    assert dash["kpi_summary"]["total_receipts_qty"] == 25  # (10+6) + 5 + 4 = 25
    assert dash["kpi_summary"]["active_ff_physical_qty"] == 25
    assert dash["kpi_summary"]["active_ff_available_qty"] == 25
    assert dash["kpi_summary"]["active_ff_reserved_fbs_qty"] == 0

    matrix = {row["sku"]: row for row in dash["sku_matrix"]}
    assert set(matrix.keys()) == {"SKU-101", "SKU-102", "SKU-103"}

    sku101_wh = {w["warehouse_name"]: w for w in matrix["SKU-101"]["warehouses"]}
    assert sku101_wh["Север"]["available_qty"] == 10
    assert sku101_wh["Юг"]["available_qty"] == 6

    sku102_wh = {w["warehouse_name"]: w for w in matrix["SKU-102"]["warehouses"]}
    assert sku102_wh["Север"]["available_qty"] == 5
    assert sku102_wh["Юг"]["available_qty"] == 0

    sku103_wh = {w["warehouse_name"]: w for w in matrix["SKU-103"]["warehouses"]}
    assert sku103_wh["Север"]["available_qty"] == 0
    assert sku103_wh["Юг"]["available_qty"] == 4


def test_receipt_and_idempotency_key_protection(client: TestClient) -> None:
    """Verify goods receipt increases stock and duplicate idempotency_key does not double-add."""
    dash = client.get("/api/dashboard").json()
    products, warehouses = _ids_map(dash)

    payload = {
        "product_id": products["SKU-102"],
        "warehouse_id": warehouses["Юг"],
        "qty": 7,
        "note": "Тестовый приход контейнеров",
        "idempotency_key": "receipt-unique-001",
    }
    r1 = client.post("/api/receipts", json=payload)
    assert r1.status_code == 200
    assert r1.json()["already_processed"] is False

    # Repeat exact same request (e.g. network retry or double form submit)
    r2 = client.post("/api/receipts", json=payload)
    assert r2.status_code == 200
    assert r2.json()["already_processed"] is True

    rec = assert_reconciled(client)
    sku102 = next(i for i in rec["items"] if i["sku"] == "SKU-102")
    assert sku102["total_receipts_qty"] == 5 + 7  # 12, NOT 19!
    assert sku102["active_ff_available_qty"] == 12


def test_inter_warehouse_transfer_and_double_receive_idempotency(client: TestClient) -> None:
    """Verify FF-to-FF transfer lifecycle and double-click protection on receive."""
    dash = client.get("/api/dashboard").json()
    products, warehouses = _ids_map(dash)

    # Send 4 units of SKU-101 from Север (10 -> 6) to Юг (currently 6)
    send_res = client.post(
        "/api/transfers",
        json={
            "product_id": products["SKU-101"],
            "from_warehouse_id": warehouses["Север"],
            "to_warehouse_id": warehouses["Юг"],
            "qty": 4,
        },
    )
    assert send_res.status_code == 200
    transfer_id = send_res.json()["transfer"]["id"]

    rec_mid = assert_reconciled(client)
    sku101_mid = next(i for i in rec_mid["items"] if i["sku"] == "SKU-101")
    assert sku101_mid["active_ff_physical_qty"] == 12  # 6 on Север + 6 on Юг
    assert sku101_mid["in_transit_ff_qty"] == 4

    # First confirmation of receipt -> adds 4 units to Юг (6 -> 10)
    rec1 = client.post(f"/api/transfers/{transfer_id}/receive")
    assert rec1.status_code == 200
    assert rec1.json()["already_processed"] is False
    assert rec1.json()["transfer"]["status"] == "RECEIVED"

    # Second confirmation (double-click / repeat request) -> must NOT add 4 units again
    rec2 = client.post(f"/api/transfers/{transfer_id}/receive")
    assert rec2.status_code == 200
    assert rec2.json()["already_processed"] is True

    rec_final = assert_reconciled(client)
    sku101_final = next(i for i in rec_final["items"] if i["sku"] == "SKU-101")
    assert sku101_final["in_transit_ff_qty"] == 0
    assert sku101_final["active_ff_physical_qty"] == 16
    assert sku101_final["active_ff_available_qty"] == 16


def test_fbs_reserve_ship_cancel_and_double_confirmation(client: TestClient) -> None:
    """Verify FBS reservation, shipment, cancellation, and protection from double deduction."""
    dash = client.get("/api/dashboard").json()
    products, warehouses = _ids_map(dash)

    # 1. Reserve 4 units of SKU-101 on Север (available 10 -> 6, reserved 0 -> 4, physical stays 10)
    res_create = client.post(
        "/api/fbs/reserve",
        json={
            "product_id": products["SKU-101"],
            "warehouse_id": warehouses["Север"],
            "qty": 4,
        },
    )
    assert res_create.status_code == 200
    order1_id = res_create.json()["orders"][0]["id"]

    rec1 = assert_reconciled(client)
    sku101 = next(i for i in rec1["items"] if i["sku"] == "SKU-101")
    assert sku101["active_ff_physical_qty"] == 16
    assert sku101["active_ff_available_qty"] == 12
    assert sku101["active_ff_reserved_fbs_qty"] == 4

    # 2. Ship order1 -> deducts from reserved and physical on Север
    ship1 = client.post(f"/api/fbs/{order1_id}/ship")
    assert ship1.status_code == 200
    assert ship1.json()["already_processed"] is False

    # 3. Repeat ship on order1 -> idempotent no-op, must NOT deduct twice!
    ship2 = client.post(f"/api/fbs/{order1_id}/ship")
    assert ship2.status_code == 200
    assert ship2.json()["already_processed"] is True

    rec2 = assert_reconciled(client)
    sku101_after_ship = next(i for i in rec2["items"] if i["sku"] == "SKU-101")
    assert sku101_after_ship["active_ff_physical_qty"] == 12
    assert sku101_after_ship["active_ff_available_qty"] == 12
    assert sku101_after_ship["active_ff_reserved_fbs_qty"] == 0
    assert sku101_after_ship["shipped_fbs_qty"] == 4

    # Cannot cancel an already shipped order
    bad_cancel = client.post(f"/api/fbs/{order1_id}/cancel")
    assert bad_cancel.status_code == 409

    # 4. Create second reservation and cancel it -> returns stock to available
    res_create2 = client.post(
        "/api/fbs/reserve",
        json={
            "product_id": products["SKU-101"],
            "warehouse_id": warehouses["Север"],
            "qty": 3,
        },
    )
    order2_id = res_create2.json()["orders"][0]["id"]

    cancel1 = client.post(f"/api/fbs/{order2_id}/cancel")
    assert cancel1.status_code == 200
    assert cancel1.json()["already_processed"] is False

    # Repeat cancel -> idempotent no-op
    cancel2 = client.post(f"/api/fbs/{order2_id}/cancel")
    assert cancel2.status_code == 200
    assert cancel2.json()["already_processed"] is True

    rec3 = assert_reconciled(client)
    sku101_after_cancel = next(i for i in rec3["items"] if i["sku"] == "SKU-101")
    assert sku101_after_cancel["active_ff_available_qty"] == 12
    assert sku101_after_cancel["active_ff_reserved_fbs_qty"] == 0


def test_fbo_send_receive_and_double_confirmation(client: TestClient) -> None:
    """Verify FBO shipment leaves FF immediately, tracks in transit, and receives idempotently."""
    dash = client.get("/api/dashboard").json()
    products, warehouses = _ids_map(dash)

    send_res = client.post(
        "/api/fbo/send",
        json={
            "product_id": products["SKU-103"],
            "warehouse_id": warehouses["Юг"],
            "qty": 3,
            "marketplace_name": "WB Коледино FBO",
        },
    )
    assert send_res.status_code == 200
    shipment_id = send_res.json()["shipments"][0]["id"]

    rec_mid = assert_reconciled(client)
    sku103_mid = next(i for i in rec_mid["items"] if i["sku"] == "SKU-103")
    assert sku103_mid["active_ff_physical_qty"] == 1
    assert sku103_mid["in_transit_fbo_qty"] == 3
    assert sku103_mid["received_fbo_qty"] == 0

    # Confirm acceptance on FBO
    r1 = client.post(f"/api/fbo/{shipment_id}/receive")
    assert r1.status_code == 200
    assert r1.json()["already_processed"] is False

    # Confirm a second time (double confirmation) -> idempotent
    r2 = client.post(f"/api/fbo/{shipment_id}/receive")
    assert r2.status_code == 200
    assert r2.json()["already_processed"] is True

    rec_final = assert_reconciled(client)
    sku103_final = next(i for i in rec_final["items"] if i["sku"] == "SKU-103")
    assert sku103_final["in_transit_fbo_qty"] == 0
    assert sku103_final["received_fbo_qty"] == 3


def test_shortage_on_selected_ff_when_total_network_stock_sufficient(client: TestClient) -> None:
    """Verify shortage handling when selected FF has 10 units, another FF has 6, and 12 are requested."""
    dash = client.get("/api/dashboard").json()
    products, warehouses = _ids_map(dash)

    # 1. Request 12 units of SKU-101 from Север (has 10, Юг has 6 -> total 16) without split
    res_shortage = client.post(
        "/api/fbs/reserve",
        json={
            "product_id": products["SKU-101"],
            "warehouse_id": warehouses["Север"],
            "qty": 12,
            "allow_split": False,
        },
    )
    assert res_shortage.status_code == 409
    err = res_shortage.json()
    assert err["code"] == "SHORTAGE_ON_SELECTED_FF"
    plan = err["shortage_plan"]
    assert plan["can_resolve_across_network"] is True
    assert plan["available_on_selected_qty"] == 10
    assert plan["deficit_on_selected_qty"] == 2
    assert plan["total_available_across_network_qty"] == 16
    assert len(plan["split_allocations"]) == 2
    assert plan["split_allocations"][0]["allocate_qty"] == 10
    assert plan["split_allocations"][1]["allocate_qty"] == 2
    assert len(plan["replenishment_transfers"]) == 1
    assert plan["replenishment_transfers"][0]["transfer_qty"] == 2

    # 2. Resolve via Option A: Atomic Split Fulfillment (`allow_split=True`)
    res_split = client.post(
        "/api/fbs/reserve",
        json={
            "product_id": products["SKU-101"],
            "warehouse_id": warehouses["Север"],
            "qty": 12,
            "allow_split": True,
        },
    )
    assert res_split.status_code == 200
    split_data = res_split.json()
    assert split_data["split_applied"] is True
    assert len(split_data["orders"]) == 2

    rec = assert_reconciled(client)
    sku101 = next(i for i in rec["items"] if i["sku"] == "SKU-101")
    assert sku101["active_ff_reserved_fbs_qty"] == 12
    assert sku101["active_ff_available_qty"] == 4  # 0 on Север + 4 on Юг

    # 3. Requesting 50 units (exceeds total network stock) must fail even with allow_split=True
    res_global = client.post(
        "/api/fbo/send",
        json={
            "product_id": products["SKU-101"],
            "warehouse_id": warehouses["Юг"],
            "qty": 50,
            "allow_split": True,
        },
    )
    assert res_global.status_code == 409
    assert res_global.json()["code"] == "INSUFFICIENT_TOTAL_STOCK"
    assert res_global.json()["shortage_plan"]["can_resolve_across_network"] is False


def test_delete_warehouse_with_stock_preserves_stock_and_history(client: TestClient) -> None:
    """Verify deleting an FF warehouse with stock preserves both stock and operation history."""
    dash = client.get("/api/dashboard").json()
    _products, warehouses = _ids_map(dash)

    # Mode 1: Delete ФФ «Север» (holds 10 SKU-101 and 5 SKU-102) keeping stock on archived balance
    del_res = client.post(
        f"/api/warehouses/{warehouses['Север']}/delete",
        json={"transfer_to_warehouse_id": None},
    )
    assert del_res.status_code == 200
    del_data = del_res.json()
    assert del_data["warehouse"]["is_deleted"] is True
    assert len(del_data["retained_items"]) == 2

    # Verify reconciliation still holds and dashboard explicitly shows where stock is accounted
    rec = assert_reconciled(client)
    sku101 = next(i for i in rec["items"] if i["sku"] == "SKU-101")
    assert sku101["active_ff_physical_qty"] == 6  # on Юг
    assert sku101["archived_ff_physical_qty"] == 10  # on Архив ФФ: Север
    assert sku101["total_ff_physical_qty"] == 16

    dash_after = del_res.json()["dashboard"]
    assert len(dash_after["archived_warehouses"]) == 1
    assert dash_after["archived_warehouses"][0]["warehouse"]["name"] == "Север"
    assert dash_after["archived_warehouses"][0]["total_physical_qty"] == 15  # 10 + 5
    # Verify operation history was NOT deleted
    assert len(dash_after["operations"]) >= 4

    # Now evacuate the archived stock from «Архив ФФ: Север» to active «ФФ Юг»
    evac_res = client.post(
        f"/api/warehouses/{warehouses['Север']}/evacuate",
        json={"target_warehouse_id": warehouses["Юг"]},
    )
    assert evac_res.status_code == 200
    assert len(evac_res.json()["moved_items"]) == 2

    rec_after_evac = assert_reconciled(client)
    sku101_evac = next(i for i in rec_after_evac["items"] if i["sku"] == "SKU-101")
    assert sku101_evac["active_ff_physical_qty"] == 16
    assert sku101_evac["archived_ff_physical_qty"] == 0


def test_warehouse_create_rename_and_delete_with_immediate_transfer(client: TestClient) -> None:
    """Verify creating, renaming, and deleting an FF warehouse with immediate stock transfer."""
    # 1. Create a new FF warehouse «Запад»
    create_res = client.post("/api/warehouses", json={"name": "Запад"})
    assert create_res.status_code == 200
    zapad_id = create_res.json()["warehouse"]["id"]

    # 2. Rename «Запад» -> «Центр»
    rename_res = client.patch(f"/api/warehouses/{zapad_id}", json={"name": "Центр"})
    assert rename_res.status_code == 200
    assert rename_res.json()["warehouse"]["name"] == "Центр"

    dash = client.get("/api/dashboard").json()
    products, warehouses = _ids_map(dash)

    # 3. Guard: cannot delete «Центр» while an unreceived transfer is IN_TRANSIT into it
    tr_res = client.post(
        "/api/transfers",
        json={
            "product_id": products["SKU-101"],
            "from_warehouse_id": warehouses["Север"],
            "to_warehouse_id": warehouses["Центр"],
            "qty": 3,
        },
    )
    assert tr_res.status_code == 200
    tr_id = tr_res.json()["transfer"]["id"]

    blocked_del = client.post(
        f"/api/warehouses/{warehouses['Центр']}/delete",
        json={"transfer_to_warehouse_id": warehouses["Юг"]},
    )
    assert blocked_del.status_code == 409
    assert blocked_del.json()["code"] == "WAREHOUSE_HAS_INCOMING_TRANSFERS"

    # Receive the transfer onto «Центр» (now «Центр» has 3 units of SKU-101)
    client.post(f"/api/transfers/{tr_id}/receive")

    # 4. Delete «Центр» with immediate stock transfer to «Юг»
    del_immediate = client.post(
        f"/api/warehouses/{warehouses['Центр']}/delete",
        json={"transfer_to_warehouse_id": warehouses["Юг"]},
    )
    assert del_immediate.status_code == 200
    assert len(del_immediate.json()["transferred_items"]) == 1
    assert del_immediate.json()["transferred_items"][0]["qty"] == 3

    rec = assert_reconciled(client)
    sku101 = next(i for i in rec["items"] if i["sku"] == "SKU-101")
    assert sku101["active_ff_physical_qty"] == 16  # 7 on Север + 9 on Юг
    assert sku101["archived_ff_physical_qty"] == 0


def test_fbo_split_fulfillment_and_html_dashboard_render(client: TestClient) -> None:
    """Verify FBO split fulfillment across warehouses and HTML index rendering."""
    html_res = client.get("/")
    assert html_res.status_code == 200
    assert "CheckStock" in html_res.text

    dash = client.get("/api/dashboard").json()
    products, warehouses = _ids_map(dash)

    # Send 14 units of SKU-101 to FBO with allow_split=True (Север has 10, Юг has 6)
    fbo_split = client.post(
        "/api/fbo/send",
        json={
            "product_id": products["SKU-101"],
            "warehouse_id": warehouses["Север"],
            "qty": 14,
            "allow_split": True,
        },
    )
    assert fbo_split.status_code == 200
    assert fbo_split.json()["split_applied"] is True
    assert len(fbo_split.json()["shipments"]) == 2

    rec = assert_reconciled(client)
    sku101 = next(i for i in rec["items"] if i["sku"] == "SKU-101")
    assert sku101["in_transit_fbo_qty"] == 14
    assert sku101["active_ff_available_qty"] == 2


def test_delete_warehouse_with_reserved_fbs_and_ship_after_archive(client: TestClient) -> None:
    """Verify deleting a warehouse that holds both available stock and reserved FBS orders."""
    dash = client.get("/api/dashboard").json()
    products, warehouses = _ids_map(dash)

    # 1. Reserve 3 units of SKU-101 on Север (physical=10, available=7, reserved=3)
    res_fbs = client.post(
        "/api/fbs/reserve",
        json={
            "product_id": products["SKU-101"],
            "warehouse_id": warehouses["Север"],
            "qty": 3,
        },
    )
    assert res_fbs.status_code == 200
    order_id = res_fbs.json()["orders"][0]["id"]

    # 2. Delete Север with immediate transfer of available stock (7 SKU-101, 5 SKU-102) to Юг
    del_res = client.post(
        f"/api/warehouses/{warehouses['Север']}/delete",
        json={"transfer_to_warehouse_id": warehouses["Юг"]},
    )
    assert del_res.status_code == 200
    # Reserved 3 units remain on archived Север until the FBS order is shipped or cancelled
    rec_mid = assert_reconciled(client)
    sku101_mid = next(i for i in rec_mid["items"] if i["sku"] == "SKU-101")
    assert sku101_mid["active_ff_available_qty"] == 13  # 6 + 7 on Юг
    assert sku101_mid["archived_ff_reserved_fbs_qty"] == 3
    assert sku101_mid["archived_ff_physical_qty"] == 3

    # 3. Standard transfer from archived warehouse is blocked (must use /evacuate for available stock)
    blocked_tr = client.post(
        "/api/transfers",
        json={
            "product_id": products["SKU-101"],
            "from_warehouse_id": warehouses["Север"],
            "to_warehouse_id": warehouses["Юг"],
            "qty": 1,
        },
    )
    assert blocked_tr.status_code == 409
    assert blocked_tr.json()["code"] == "WAREHOUSE_ARCHIVED"

    # 4. Ship the reserved FBS order from archived Север -> completes cleanly and reconciles
    ship_res = client.post(f"/api/fbs/{order_id}/ship")
    assert ship_res.status_code == 200

    rec_final = assert_reconciled(client)
    sku101_final = next(i for i in rec_final["items"] if i["sku"] == "SKU-101")
    assert sku101_final["archived_ff_physical_qty"] == 0
    assert sku101_final["shipped_fbs_qty"] == 3


def test_cyrillic_case_insensitive_warehouse_duplicates_and_idempotent_rename(client: TestClient) -> None:
    """Verify Cyrillic case-insensitive duplicate warehouse guards on SQLite/PostgreSQL."""
    dash = client.get("/api/dashboard").json()
    _products, warehouses = _ids_map(dash)

    # Exact duplicate Cyrillic name «Север» -> 409 (must not crash with 500 IntegrityError)
    dup_exact = client.post("/api/warehouses", json={"name": "Север"})
    assert dup_exact.status_code == 409
    assert dup_exact.json()["code"] == "WAREHOUSE_NAME_DUPLICATE"

    # Lowercase Cyrillic duplicate «север» -> 409
    dup_lower = client.post("/api/warehouses", json={"name": "север"})
    assert dup_lower.status_code == 409
    assert dup_lower.json()["code"] == "WAREHOUSE_NAME_DUPLICATE"

    # Renaming «Юг» to «север» -> 409
    rename_conflict = client.patch(f"/api/warehouses/{warehouses['Юг']}", json={"name": "север"})
    assert rename_conflict.status_code == 409
    assert rename_conflict.json()["code"] == "WAREHOUSE_NAME_DUPLICATE"

    # Renaming «Север» to «Север» (no-op) -> 200 with already_processed=True
    rename_same = client.patch(f"/api/warehouses/{warehouses['Север']}", json={"name": "Север"})
    assert rename_same.status_code == 200
    assert rename_same.json()["already_processed"] is True
    assert_reconciled(client)


def test_split_fulfillment_idempotency_key_and_cross_operation_conflict(client: TestClient) -> None:
    """Verify split FBS/FBO idempotency returns all split parts and blocks cross-operation key reuse."""
    dash = client.get("/api/dashboard").json()
    products, warehouses = _ids_map(dash)

    split_payload = {
        "product_id": products["SKU-101"],
        "warehouse_id": warehouses["Север"],
        "qty": 12,
        "allow_split": True,
        "idempotency_key": "split-fbs-key-001",
    }
    r1 = client.post("/api/fbs/reserve", json=split_payload)
    assert r1.status_code == 200
    assert r1.json()["already_processed"] is False
    assert r1.json()["split_applied"] is True
    assert len(r1.json()["orders"]) == 2

    # Retry with same idempotency_key -> must return all 2 split orders and split_applied=True
    r2 = client.post("/api/fbs/reserve", json=split_payload)
    assert r2.status_code == 200
    assert r2.json()["already_processed"] is True
    assert r2.json()["split_applied"] is True
    assert len(r2.json()["orders"]) == 2

    # Attempting to reuse an FBS idempotency_key for a receipt must fail with 409
    bad_reuse = client.post(
        "/api/receipts",
        json={
            "product_id": products["SKU-101"],
            "warehouse_id": warehouses["Север"],
            "qty": 5,
            "idempotency_key": "split-fbs-key-001",
        },
    )
    assert bad_reuse.status_code == 409
    assert bad_reuse.json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    # Verify /api/reset-demo alias works and restores initial 25 units
    reset_res = client.post("/api/reset-demo")
    assert reset_res.status_code == 200
    rec = assert_reconciled(client)
    assert sum(i["total_receipts_qty"] for i in rec["items"]) == 25


def test_ui_palette_apple_design_and_static_assets(client: TestClient) -> None:
    """Verify HTML/JS UI contract: CheckStock/Raketa palette, apple-design media queries, and static assets."""
    html_res = client.get("/")
    assert html_res.status_code == 200
    html = html_res.text

    # Exact CheckStock / Raketa v0.5 color palette tokens
    for hex_token in ("#F6F5F1", "#EDF3EE", "#1E5638", "#F4EFEA", "#D95338", "#EEF0FF", "#4F46E5", "#1D2421", "#E4E7E4"):
        assert hex_token in html, f"Missing reference color token {hex_token} in index.html"

    # Apple design accessibility media queries and no invalid Tailwind v4 classes
    assert "prefers-reduced-motion: reduce" in html
    assert "prefers-reduced-transparency: reduce" in html
    assert "prefers-contrast: more" in html
    assert "shadow-2xs" not in html

    js_res = client.get("/static/app.js")
    assert js_res.status_code == 200
    js = js_res.text
    assert "animateSurfaceShow" in js
    assert "animateSurfaceHide" in js
    assert "В пути FBO" in js
    assert "Принято FBO" in js




