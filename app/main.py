"""FastAPI Transport Adapter over `LedgerModule` (`app/main.py`)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine, get_db
from app.ledger import (
    DomainError,
    ShortageError,
    cancel_fbs_order,
    check_reconciliation,
    create_fbo_shipment,
    create_fbs_reserve,
    create_transfer,
    create_warehouse,
    delete_warehouse,
    evacuate_archived_warehouse_stock,
    get_dashboard_state,
    receive_fbo_shipment,
    receive_transfer,
    record_receipt,
    rename_warehouse,
    ship_fbs_order,
)
from app.schemas import (
    FBOShipmentCreateRequest,
    FBSReserveCreateRequest,
    ReceiptCreateRequest,
    TransferCreateRequest,
    WarehouseCreateRequest,
    WarehouseDeleteRequest,
    WarehouseRenameRequest,
)
from app.seed import seed_initial_data

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize schema and seed initial data on startup (unless overridden by test fixture)."""
    if get_db not in _app.dependency_overrides:
        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            seed_initial_data(db, force_reset=False)
    yield
    if get_db not in _app.dependency_overrides:
        engine.dispose()


app = FastAPI(
    title="CheckStock — Складской учёт ФФ / FBS / FBO (Ракета)",
    description=(
        "Прототип системы учёта товарных остатков на складах фулфилмента (ФФ), "
        "перемещений между ФФ, резервов/отгрузок FBS и поставок FBO с гарантией "
        "сходимости баланса и защитой от повторного подтверждения."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(ShortageError)
async def shortage_error_handler(_request: Request, exc: ShortageError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "code": exc.code,
            "message": exc.message,
            "shortage_plan": exc.plan.model_dump(mode="json"),
        },
    )


@app.exception_handler(DomainError)
async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


class EvacuateStockRequest(BaseModel):
    target_warehouse_id: int = Field(..., gt=0, description="ID действующего ФФ для перевода остатка")


@app.get("/", response_class=HTMLResponse)
def index_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Render the single-screen CheckStock manager dashboard."""
    state = get_dashboard_state(db)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"initial_state": state},
    )


@app.get("/api/health")
def health_check(db: Session = Depends(get_db)) -> dict[str, Any]:
    rec = check_reconciliation(db)
    return {
        "status": "ok",
        "all_balanced": rec.all_balanced,
        "sku_count": len(rec.items),
    }


@app.get("/api/dashboard")
def api_get_dashboard(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return full dashboard state + reconciliation report."""
    return get_dashboard_state(db)


@app.get("/api/reconciliation")
def api_get_reconciliation(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Verify Conservation of Stock across all SKUs and locations."""
    return check_reconciliation(db).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Warehouses (ФФ)
# ---------------------------------------------------------------------------


@app.post("/api/warehouses")
def api_create_warehouse(req: WarehouseCreateRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = create_warehouse(db, req)
    return {**result, "dashboard": get_dashboard_state(db)}


@app.patch("/api/warehouses/{warehouse_id}")
def api_rename_warehouse(
    warehouse_id: int,
    req: WarehouseRenameRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = rename_warehouse(db, warehouse_id, req)
    return {**result, "dashboard": get_dashboard_state(db)}


@app.post("/api/warehouses/{warehouse_id}/delete")
def api_delete_warehouse_post(
    warehouse_id: int,
    req: WarehouseDeleteRequest | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = delete_warehouse(db, warehouse_id, req)
    return {**result, "dashboard": get_dashboard_state(db)}


@app.delete("/api/warehouses/{warehouse_id}")
def api_delete_warehouse(
    warehouse_id: int,
    transfer_to_warehouse_id: int | None = Query(default=None),
    note: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    req = WarehouseDeleteRequest(transfer_to_warehouse_id=transfer_to_warehouse_id, note=note)
    result = delete_warehouse(db, warehouse_id, req)
    return {**result, "dashboard": get_dashboard_state(db)}


@app.post("/api/warehouses/{warehouse_id}/evacuate")
def api_evacuate_archived_warehouse(
    warehouse_id: int,
    req: EvacuateStockRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = evacuate_archived_warehouse_stock(db, warehouse_id, req.target_warehouse_id)
    return {**result, "dashboard": get_dashboard_state(db)}


# ---------------------------------------------------------------------------
# Receipts & Inter-Warehouse Transfers (Приход и Перемещения между ФФ)
# ---------------------------------------------------------------------------


@app.post("/api/receipts")
def api_record_receipt(req: ReceiptCreateRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = record_receipt(db, req)
    return {**result, "dashboard": get_dashboard_state(db)}


@app.post("/api/transfers")
def api_create_transfer(req: TransferCreateRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = create_transfer(db, req)
    return {**result, "dashboard": get_dashboard_state(db)}


@app.post("/api/transfers/{transfer_id}/receive")
def api_receive_transfer(transfer_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = receive_transfer(db, transfer_id)
    return {**result, "dashboard": get_dashboard_state(db)}


# ---------------------------------------------------------------------------
# FBS Reservations, Shipments, and Cancellations
# ---------------------------------------------------------------------------


@app.post("/api/fbs/reserve")
def api_create_fbs_reserve(req: FBSReserveCreateRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = create_fbs_reserve(db, req)
    return {**result, "dashboard": get_dashboard_state(db)}


@app.post("/api/fbs/{order_id}/ship")
def api_ship_fbs_order(order_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = ship_fbs_order(db, order_id)
    return {**result, "dashboard": get_dashboard_state(db)}


@app.post("/api/fbs/{order_id}/cancel")
def api_cancel_fbs_order(order_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = cancel_fbs_order(db, order_id)
    return {**result, "dashboard": get_dashboard_state(db)}


# ---------------------------------------------------------------------------
# FBO Shipments and Marketplace Acceptance
# ---------------------------------------------------------------------------


@app.post("/api/fbo/send")
def api_create_fbo_shipment(req: FBOShipmentCreateRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = create_fbo_shipment(db, req)
    return {**result, "dashboard": get_dashboard_state(db)}


@app.post("/api/fbo/{shipment_id}/receive")
def api_receive_fbo_shipment(shipment_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = receive_fbo_shipment(db, shipment_id)
    return {**result, "dashboard": get_dashboard_state(db)}


# ---------------------------------------------------------------------------
# Demo Reset (Сброс к начальным тестовым данным ТЗ)
# ---------------------------------------------------------------------------


@app.post("/api/demo/reset")
@app.post("/api/reset-demo")
def api_reset_demo_data(db: Session = Depends(get_db)) -> dict[str, Any]:
    seed_initial_data(db, force_reset=True)
    return {
        "message": "База данных сброшена к начальным тестовым данным ТЗ (SKU-101: 10/6, SKU-102: 5/0, SKU-103: 0/4).",
        "dashboard": get_dashboard_state(db),
    }
