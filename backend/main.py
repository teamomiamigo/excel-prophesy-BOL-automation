import csv
import io
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db, engine
from backend.models import (
    Base, BOLRecord, ApprovalHistory, BOLStatus, ActionType,
    BOLSummary, FlagRequest, ApproveRequest,
    ExportRequest, ExportResponse, HealthResponse,
)
from backend.mock_data import MOCK_BOLS
from backend.email_service import send_bol_export_email
from backend.csv_export import get_csv_filename, get_sid_filename, generate_sid_csv, generate_mock_sid_rows

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.USE_MOCK_DATA:
        Base.metadata.create_all(bind=engine)
        logger.info("DB tables verified/created.")
        with engine.connect() as _conn:
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS base_tariff NUMERIC(10,2)"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS fsc_pct NUMERIC(8,6)"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS is_third_party BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS is_ignored BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS invoice_sent_at TIMESTAMP WITH TIME ZONE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS alg_fsc_pct NUMERIC(8,6)"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS alg_fsc_cost NUMERIC(10,2)"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS tariff_zone_approximate BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS weight_source_fallback BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.commit()
        logger.info(
            "DB column migration for base_tariff/fsc_pct/is_third_party/is_ignored/invoice_sent_at/"
            "alg_fsc_pct/alg_fsc_cost/tariff_zone_approximate/weight_source_fallback complete."
        )
    logger.info(
        "SG360 BOL API started. Mock mode: %s | Version: %s",
        settings.USE_MOCK_DATA,
        settings.APP_VERSION,
    )
    yield
    logger.info("SG360 BOL API shutting down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - start) * 1000
    logger.info("%s %s — %d (%.1fms)", request.method, request.url.path, response.status_code, ms)
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s", request.url)
    detail = f"{type(exc).__name__}: {exc}" if settings.DEBUG else "Internal server error. Please contact your system administrator."
    return JSONResponse(status_code=500, content={"detail": detail})


# ---------------------------------------------------------------------------
# Mock state (in-memory; mutations survive process lifetime, reset on restart)
# ---------------------------------------------------------------------------

_mock_state: dict[str, dict] = {r["id"]: dict(r) for r in MOCK_BOLS}


def _find_mock(record_id: str) -> dict:
    """Lookup by UUID, invoice_number, or bol_number."""
    if record_id in _mock_state:
        return _mock_state[record_id]
    for rec in _mock_state.values():
        if rec["invoice_number"] == record_id:
            return rec
        if rec["bol_number"] is not None and str(rec["bol_number"]) == record_id:
            return rec
    raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")


def _record_to_summary(r: dict) -> dict:
    """Ensure UUID id is serialized as string."""
    out = dict(r)
    out["id"] = str(out["id"])
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["System"])
def health_check(db: Session = Depends(get_db)):
    db_ok = True
    if not settings.USE_MOCK_DATA:
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            db_ok = False
    return HealthResponse(
        status="ok",
        version=settings.APP_VERSION,
        db_online=db_ok,
        mock_mode=settings.USE_MOCK_DATA,
    )


@app.get("/api/bols", response_model=list[BOLSummary], tags=["BOLs"])
def list_pending_bols(db: Session = Depends(get_db)):
    """All pending and flagged records — what Katie sees each morning."""
    if settings.USE_MOCK_DATA:
        records = sorted(
            [_record_to_summary(r) for r in _mock_state.values() if r["status"] != "approved"],
            key=lambda r: (r.get("invoice_number") is None, r.get("created_at") or ""),
        )
        return records

    rows = (
        db.query(BOLRecord)
        .filter(BOLRecord.status != BOLStatus.APPROVED)
        .order_by(
            BOLRecord.invoice_number.is_(None),
            BOLRecord.created_at,
        )
        .all()
    )
    return rows


@app.get("/api/bols/approved", response_model=list[BOLSummary], tags=["BOLs"])
def list_approved_bols(
    export_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """
    Approved records not yet marked as sent to accounting (accounting_exported_at IS NULL).
    Pass export_date=YYYY-MM-DD to retrieve records approved on a specific date instead
    (used for historical CSV exports).
    """
    if settings.USE_MOCK_DATA:
        return [_record_to_summary(r) for r in _mock_state.values()
                if r["status"] == "approved" and r.get("accounting_exported_at") is None]

    if export_date:
        rows = (
            db.query(BOLRecord)
            .filter(
                BOLRecord.status == BOLStatus.APPROVED,
                BOLRecord.approved_at >= datetime(
                    export_date.year, export_date.month, export_date.day, tzinfo=timezone.utc
                ),
            )
            .all()
        )
    else:
        rows = (
            db.query(BOLRecord)
            .filter(
                BOLRecord.status == BOLStatus.APPROVED,
                BOLRecord.accounting_exported_at.is_(None),
            )
            .all()
        )
    return rows


@app.post("/api/bols/mark-accounting-sent", tags=["BOLs"])
def mark_accounting_sent(body: dict, db: Session = Depends(get_db)):
    """
    Mark a list of records as sent to accounting by setting accounting_exported_at = now().
    Called after Katie confirms she has sent the email from Outlook.
    """
    record_ids: list[str] = body.get("record_ids", [])
    if not record_ids:
        raise HTTPException(status_code=400, detail="record_ids is required")

    now_ts = datetime.now(timezone.utc)

    if settings.USE_MOCK_DATA:
        count = 0
        for rid in record_ids:
            if rid in _mock_state:
                _mock_state[rid]["accounting_exported_at"] = now_ts
                count += 1
        return {"marked": count, "timestamp": now_ts.isoformat()}

    rows = db.query(BOLRecord).filter(BOLRecord.id.in_(record_ids)).all()
    for row in rows:
        row.accounting_exported_at = now_ts
    db.commit()
    return {"marked": len(rows), "timestamp": now_ts.isoformat()}


@app.post(
    "/api/bols/{record_id}/approve",
    response_model=BOLSummary,
    status_code=status.HTTP_200_OK,
    tags=["BOLs"],
)
def approve_bol(
    record_id: str,
    body: ApproveRequest = ApproveRequest(),
    db: Session = Depends(get_db),
):
    """
    Approve a record. Idempotent — approving an already-approved record
    returns 200 without writing a duplicate history entry.
    """
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        if rec["status"] == "approved":
            return _record_to_summary(rec)
        rec["status"] = "approved"
        rec["approved_at"] = datetime.now(timezone.utc)
        rec["approved_by"] = body.approved_by
        rec["flag_reason"] = None
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    if row.status == BOLStatus.APPROVED:
        return row
    row.status = BOLStatus.APPROVED
    row.approved_at = datetime.now(timezone.utc)
    row.approved_by = body.approved_by
    row.flag_reason = None
    db.add(ApprovalHistory(
        bol_id=row.id,
        action=ActionType.APPROVED,
        performed_by=body.approved_by,
    ))
    db.commit()
    db.refresh(row)
    return row


@app.post(
    "/api/bols/{record_id}/unapprove",
    response_model=BOLSummary,
    status_code=status.HTTP_200_OK,
    tags=["BOLs"],
)
def unapprove_bol(
    record_id: str,
    db: Session = Depends(get_db),
):
    """Revert an approved record back to pending review."""
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        rec["status"] = "pending"
        rec["approved_at"] = None
        rec["approved_by"] = None
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    row.status = BOLStatus.PENDING
    row.approved_at = None
    row.approved_by = None
    db.add(ApprovalHistory(
        bol_id=row.id,
        action=ActionType.REOPENED,
        performed_by="coordinator",
    ))
    db.commit()
    db.refresh(row)
    return row


@app.post(
    "/api/bols/{record_id}/flag",
    response_model=BOLSummary,
    status_code=status.HTTP_200_OK,
    tags=["BOLs"],
)
def flag_bol(
    record_id: str,
    body: FlagRequest,
    db: Session = Depends(get_db),
):
    """Flag a record with a reason. Flagged records are excluded from exports."""
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        rec["status"] = "flagged"
        rec["flag_reason"] = body.reason
        rec["approved_at"] = None
        rec["approved_by"] = None
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    row.status = BOLStatus.FLAGGED
    row.flag_reason = body.reason
    row.approved_at = None
    row.approved_by = None
    db.add(ApprovalHistory(
        bol_id=row.id,
        action=ActionType.FLAGGED,
        performed_by="coordinator",
        reason=body.reason,
    ))
    db.commit()
    db.refresh(row)
    return row


@app.post(
    "/api/bols/{record_id}/unflag",
    response_model=BOLSummary,
    status_code=status.HTTP_200_OK,
    tags=["BOLs"],
)
def unflag_bol(
    record_id: str,
    db: Session = Depends(get_db),
):
    """Return a flagged record to pending review, clearing the flag reason."""
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        if rec["status"] != "flagged":
            raise HTTPException(status_code=400, detail="Record is not flagged.")
        rec["status"] = "pending"
        rec["flag_reason"] = None
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    if row.status != BOLStatus.FLAGGED:
        raise HTTPException(status_code=400, detail="Record is not flagged.")
    row.status = BOLStatus.PENDING
    row.flag_reason = None
    db.add(ApprovalHistory(
        bol_id=row.id,
        action=ActionType.REOPENED,
        performed_by="coordinator",
        reason="Unflagged — returned to pending",
    ))
    db.commit()
    db.refresh(row)
    return row


@app.post(
    "/api/bols/{record_id}/mark-third-party",
    response_model=BOLSummary,
    status_code=status.HTTP_200_OK,
    tags=["BOLs"],
)
def mark_third_party(
    record_id: str,
    db: Session = Depends(get_db),
):
    """Mark a record as third-party (customer pays freight directly).
    Only valid for records with no_invoice=True. Idempotent."""
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        if rec.get("amount") is not None or rec.get("bol_number") is not None:
            raise HTTPException(
                status_code=400,
                detail="Only records with no matched invoice and no BOL number can be marked as third-party.",
            )
        rec["is_third_party"] = True
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    if row.amount is not None or row.bol_number is not None:
        raise HTTPException(
            status_code=400,
            detail="Only records with no matched invoice and no BOL number can be marked as third-party.",
        )
    row.is_third_party = True
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


@app.post(
    "/api/bols/{record_id}/unmark-third-party",
    response_model=BOLSummary,
    status_code=status.HTTP_200_OK,
    tags=["BOLs"],
)
def unmark_third_party(
    record_id: str,
    db: Session = Depends(get_db),
):
    """Revert a third-party record back to the normal pending queue."""
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        rec["is_third_party"] = False
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    row.is_third_party = False
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/bols/{record_id}/ignore", response_model=BOLSummary, tags=["BOLs"])
def ignore_bol(record_id: str, db: Session = Depends(get_db)):
    """Mark a record as ignored — stays in log, excluded from exports, reversible."""
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        rec["is_ignored"] = True
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)
    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    row.is_ignored = True
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/bols/{record_id}/unignore", response_model=BOLSummary, tags=["BOLs"])
def unignore_bol(record_id: str, db: Session = Depends(get_db)):
    """Remove ignored flag from a record."""
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        rec["is_ignored"] = False
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)
    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    row.is_ignored = False
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/bols/{record_id}/reassign-invoice", tags=["BOLs"])
def reassign_invoice(record_id: str, body: dict, db: Session = Depends(get_db)):
    """
    Reassign the invoice on a record to a different trip/BOL/manifest.
    body: { "target": str, "action": "preview" | "merge" | "replace" }

    target search order:
    1. Pure integer → match bol_number
    2. Starts with TEC_T_ → match technique_trip
    3. Starts with TEC_M_ → match manifest
    4. Else → suffix match on trip (e.g. "110707" → TEC_T_0110707)
    """
    target_str = (body.get("target") or "").strip()
    action = body.get("action", "preview")

    if not target_str:
        raise HTTPException(status_code=400, detail="target is required")
    if action not in ("preview", "merge", "replace"):
        raise HTTPException(status_code=400, detail="action must be preview, merge, or replace")

    def _find_target_mock(t: str):
        """Return mock record dict matching target string, or None."""
        try:
            bol_int = int(t)
            for r in _mock_state.values():
                if r.get("bol_number") == bol_int:
                    return r
        except ValueError:
            pass
        if t.upper().startswith("TEC_T_"):
            for r in _mock_state.values():
                if (r.get("technique_trip") or "").upper() == t.upper():
                    return r
        if t.upper().startswith("TEC_M_"):
            for r in _mock_state.values():
                if (r.get("manifest") or "").upper() == t.upper():
                    return r
        # Suffix match: "110707" matches TEC_T_0110707
        for r in _mock_state.values():
            trip = r.get("technique_trip") or ""
            if trip and trip.split("_")[-1].lstrip("0") == t.lstrip("0"):
                return r
        return None

    def _clear_invoice_fields(rec: dict):
        rec["invoice_number"] = None
        rec["amount"] = None
        rec["cost_pct"] = None
        rec["alg_weight"] = None
        rec["alg_pallets"] = None
        rec["alg_pcs"] = None
        rec["weight_diff"] = None
        rec["pallet_diff"] = None
        rec["pcs_diff"] = None
        rec["inv_job_number"] = None
        rec["updated_at"] = datetime.now(timezone.utc)

    def _merge_invoice_numbers_util(existing, new):
        if not existing:
            return new
        parts = [p.strip() for p in existing.split(",")]
        if new not in parts:
            parts.append(new)
        return ", ".join(parts)

    if settings.USE_MOCK_DATA:
        source = _find_mock(record_id)
        if not source.get("invoice_number"):
            raise HTTPException(status_code=400, detail="Source record has no invoice to reassign")

        target_rec = _find_target_mock(target_str)
        target_found = target_rec is not None
        target_trip = target_rec.get("technique_trip") if target_rec else None
        target_inv = target_rec.get("invoice_number") if target_rec else None
        target_amount = float(target_rec.get("amount") or 0) if target_rec else None
        has_conflict = bool(target_inv) if target_rec else False

        if action == "preview":
            return {
                "target_found": target_found,
                "target_trip": target_trip,
                "target_invoice_number": target_inv,
                "target_amount": target_amount,
                "has_conflict": has_conflict,
            }

        if not target_found:
            raise HTTPException(status_code=404, detail=f"No record found matching '{target_str}'")

        src_inv = source.get("invoice_number")
        src_amount = source.get("amount")
        src_alg_weight = source.get("alg_weight")
        src_alg_pallets = source.get("alg_pallets")
        src_alg_pcs = source.get("alg_pcs")

        if action == "merge":
            target_rec["invoice_number"] = _merge_invoice_numbers_util(target_inv, src_inv)
            target_rec["amount"] = Decimal(str(round(
                float(target_rec.get("amount") or 0) + float(src_amount or 0), 2
            )))
            if not target_inv:
                target_rec["alg_weight"] = src_alg_weight
                target_rec["alg_pallets"] = src_alg_pallets
                target_rec["alg_pcs"] = src_alg_pcs
        elif action == "replace":
            target_rec["invoice_number"] = src_inv
            target_rec["amount"] = src_amount
            target_rec["alg_weight"] = src_alg_weight
            target_rec["alg_pallets"] = src_alg_pallets
            target_rec["alg_pcs"] = src_alg_pcs

        if target_rec.get("amount") and target_rec.get("access_prog"):
            target_rec["cost_pct"] = round(
                float(target_rec["amount"]) / float(target_rec["access_prog"]), 6
            )
        target_rec["updated_at"] = datetime.now(timezone.utc)

        # Clear invoice from source; delete stub if invoice-only
        is_stub = source.get("technique_trip") is None
        if is_stub:
            del _mock_state[source["id"]]
        else:
            _clear_invoice_fields(source)

        return {"success": True, "action": action, "target_trip": target_trip}

    # --- Live DB mode ---
    source_row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not source_row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    if not source_row.invoice_number:
        raise HTTPException(status_code=400, detail="Source record has no invoice to reassign")

    # Find target
    target_row = None
    try:
        bol_int = int(target_str)
        target_row = db.query(BOLRecord).filter(BOLRecord.bol_number == bol_int).first()
    except ValueError:
        pass
    if not target_row and target_str.upper().startswith("TEC_T_"):
        target_row = db.query(BOLRecord).filter(
            BOLRecord.technique_trip.ilike(target_str)
        ).first()
    if not target_row and target_str.upper().startswith("TEC_M_"):
        target_row = db.query(BOLRecord).filter(
            BOLRecord.manifest.ilike(target_str)
        ).first()
    if not target_row:
        # Suffix match
        for r in db.query(BOLRecord).all():
            trip = r.technique_trip or ""
            if trip and trip.split("_")[-1].lstrip("0") == target_str.lstrip("0"):
                target_row = r
                break

    target_found = target_row is not None
    target_trip = target_row.technique_trip if target_row else None
    target_inv = target_row.invoice_number if target_row else None
    target_amount = float(target_row.amount or 0) if target_row else None
    has_conflict = bool(target_inv) if target_row else False

    if action == "preview":
        return {
            "target_found": target_found,
            "target_trip": target_trip,
            "target_invoice_number": target_inv,
            "target_amount": target_amount,
            "has_conflict": has_conflict,
        }

    if not target_found:
        raise HTTPException(status_code=404, detail=f"No record found matching '{target_str}'")

    def _merge_nums_db(existing, new):
        if not existing:
            return new
        parts = [p.strip() for p in existing.split(",")]
        if new not in parts:
            parts.append(new)
        return ", ".join(parts)

    src_inv = source_row.invoice_number
    src_amount = source_row.amount
    src_alg_weight = source_row.alg_weight
    src_alg_pallets = source_row.alg_pallets
    src_alg_pcs = source_row.alg_pcs

    if action == "merge":
        target_row.invoice_number = _merge_nums_db(target_inv, src_inv)
        target_row.amount = Decimal(str(round(
            float(target_row.amount or 0) + float(src_amount or 0), 2
        )))
        if not target_inv:
            target_row.alg_weight = src_alg_weight
            target_row.alg_pallets = src_alg_pallets
            target_row.alg_pcs = src_alg_pcs
    elif action == "replace":
        target_row.invoice_number = src_inv
        target_row.amount = src_amount
        target_row.alg_weight = src_alg_weight
        target_row.alg_pallets = src_alg_pallets
        target_row.alg_pcs = src_alg_pcs

    if target_row.amount and target_row.access_prog:
        target_row.cost_pct = Decimal(str(round(
            float(target_row.amount) / float(target_row.access_prog), 6
        )))
    target_row.updated_at = datetime.now(timezone.utc)

    is_stub = source_row.technique_trip is None
    if is_stub:
        db.delete(source_row)
    else:
        source_row.invoice_number = None
        source_row.amount = None
        source_row.cost_pct = None
        source_row.alg_weight = None
        source_row.alg_pallets = None
        source_row.alg_pcs = None
        source_row.weight_diff = None
        source_row.pallet_diff = None
        source_row.pcs_diff = None
        source_row.inv_job_number = None
        source_row.updated_at = datetime.now(timezone.utc)

    db.commit()
    return {"success": True, "action": action, "target_trip": target_trip}


@app.patch("/api/bols/{record_id}/notes", response_model=BOLSummary, tags=["BOLs"])
def update_notes(
    record_id: str,
    body: dict,
    db: Session = Depends(get_db),
):
    """Update the notes field for a record. Called on auto-save from the dashboard."""
    notes = body.get("notes") or None

    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        rec["notes"] = notes
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    row.notes = notes
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/admin/reset-invoices", tags=["Admin"])
def reset_all_invoices(db: Session = Depends(get_db)):
    """
    Dev-only: delete stub records and clear invoice fields on all Technique records.
    Lets you do a clean invoice upload from scratch without re-running the pull.
    """
    if settings.USE_MOCK_DATA:
        stub_ids = [k for k, v in _mock_state.items() if v.get("match_strategy") == "invoice_only"]
        for sid in stub_ids:
            del _mock_state[sid]
        for rec in _mock_state.values():
            rec["invoice_number"] = None
            rec["amount"]         = None
            rec["alg_weight"]     = None
            rec["alg_pallets"]    = None
            rec["alg_pcs"]        = None
            rec["access_prog"]    = None
            rec["cost_pct"]       = None
            rec["match_strategy"] = None
            rec["inv_job_number"] = None
            rec["weight_diff"]    = None
            rec["pallet_diff"]    = None
            rec["pcs_diff"]       = None
            rec["notes"]          = None
            if rec["status"] != "approved":
                rec["status"]     = "pending"
                rec["flag_reason"] = None
        return {"stubs_deleted": len(stub_ids), "records_cleared": len(_mock_state)}

    stubs = db.query(BOLRecord).filter(BOLRecord.match_strategy == "invoice_only").all()
    stub_count = len(stubs)
    for s in stubs:
        db.delete(s)

    technique_rows = db.query(BOLRecord).filter(BOLRecord.technique_trip.isnot(None)).all()
    for row in technique_rows:
        row.invoice_number = None
        row.amount         = None
        row.alg_weight     = None
        row.alg_pallets    = None
        row.alg_pcs        = None
        row.access_prog    = None
        row.cost_pct       = None
        row.match_strategy = None
        row.inv_job_number = None
        row.weight_diff    = None
        row.pallet_diff    = None
        row.pcs_diff       = None
        row.notes          = None
        if row.status != BOLStatus.APPROVED:
            row.status      = BOLStatus.PENDING
            row.flag_reason = None

    db.commit()
    return {"stubs_deleted": stub_count, "records_cleared": len(technique_rows)}


def _apply_bol_status(row: "BOLRecord", technique_row: dict) -> None:
    """
    Set bol_number/needs_sid_export from a Technique/ShipperPlus query row's
    load_id/pooled_to_load_id. Type A (no BOL yet, needs_sid_export=True) vs
    Type B (load_id/pooled_to_load_id > 0 — a BOL already exists in Prophecy).
    Shared by the bulk pull (pull_technique_data) and the per-record BOL check
    (POST /api/bols/{id}/refresh-bol) so both apply identical logic.
    """
    load_id = technique_row.get("load_id") or 0
    pooled_id = technique_row.get("pooled_to_load_id") or 0
    if load_id > 0 or pooled_id > 0:
        row.needs_sid_export = False
        if load_id > 0 and not row.bol_number:
            row.bol_number = load_id
        elif pooled_id > 0 and not row.bol_number:
            row.bol_number = pooled_id
    else:
        row.needs_sid_export = True


def _compute_diffs(row: "BOLRecord") -> None:
    """
    weight_diff/pallet_diff/pcs_diff = ALG invoiced qty - our own recorded qty.
    Uses Prophecy quantities as the baseline for Wolf/311 records (no technique_trip,
    prophecy_* populated), Technique quantities otherwise — same "is this Prophecy-
    sourced" check BOLRow.jsx uses on the frontend. Single source of truth: call this
    anywhere a diff needs (re)computing instead of duplicating the formula.
    """
    is_prophecy = not row.technique_trip and (row.prophecy_weight is not None or row.prophecy_pallets is not None)
    ref_weight  = row.prophecy_weight  if is_prophecy else row.technique_weight
    ref_pallets = row.prophecy_pallets if is_prophecy else row.technique_pallets
    ref_pcs     = row.prophecy_pcs     if is_prophecy else row.technique_pcs

    row.weight_diff = (
        Decimal(str(round(float(row.alg_weight) - float(ref_weight), 2)))
        if row.alg_weight is not None and ref_weight is not None else None
    )
    row.pallet_diff = (
        row.alg_pallets - ref_pallets
        if row.alg_pallets is not None and ref_pallets is not None else None
    )
    row.pcs_diff = (
        row.alg_pcs - ref_pcs
        if row.alg_pcs is not None and ref_pcs is not None else None
    )


def _trip_to_suffix(trip: str) -> str:
    """e.g. 'TEC_T_0110977' -> '110977'. Shared by invoice matching and stub re-matching."""
    parts = (trip or "").split("T_")
    return str(int(parts[-1])) if len(parts) >= 2 else ""


def _create_technique_record_from_fallback(db: Session, m: dict, weight_data: dict) -> "BOLRecord":
    """
    Create a brand-new BOLRecord for a manifest found only via a wide-window Technique
    fallback query (see the invoice_only stub fallback in pull_technique_data() and
    POST /api/bols/{id}/retry-match) — deliberately separate from the main daily pull's
    per-manifest upsert, which has wipe-on-existing semantics that don't apply here:
    this only ever fires for a manifest we've never seen before.
    """
    row = BOLRecord(status=BOLStatus.PENDING)
    db.add(row)
    row.technique_trip = m["technique_trip"]
    row.manifest = m["manifest"]
    row.technique_weight  = weight_data.get("technique_weight", 0)
    row.technique_pallets = weight_data.get("technique_pallets", 0)
    row.technique_pcs     = weight_data.get("technique_pcs", 0)
    _apply_bol_status(row, m)
    proph_pcs = m.get("prophecy_pcs") or 0
    if proph_pcs:
        row.prophecy_pcs = proph_pcs
    return row


@app.post("/api/admin/pull", tags=["Admin"])
def pull_technique_data(db: Session = Depends(get_db)):
    """
    Morning data pull: fetch real Technique manifests from AWP-SQL-PROD and
    upsert into bol_records. Invoice fields are left null when MOCK_INVOICES=True.

    Auto-detects days_back: 21 days on first pull (empty DB), 1 day on subsequent
    pulls (incremental). Old data is persistent — daily pulls add new records only.

    Call this once each morning (or via the dashboard Pull Manifests button).
    Not available in mock mode.
    """
    if settings.USE_MOCK_DATA:
        raise HTTPException(
            status_code=400,
            detail="Pull endpoint is disabled in mock mode. Set USE_MOCK_DATA=False in .env.",
        )

    from backend.data_layer import get_technique_data, get_manifest_weights

    days_back = 1

    manifests = get_technique_data(days_back=days_back)

    # Deduplicate by (technique_trip, manifest) — the SQL query can return multiple
    # rows for the same manifest when pooled_to_load_id or other GROUP BY fields differ.
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for m in manifests:
        key = (m.get("technique_trip"), m.get("manifest"))
        if key not in seen:
            seen.add(key)
            deduped.append(m)
    manifests = deduped

    manifest_numbers = [m["manifest"] for m in manifests if m.get("manifest")]
    weights_by_manifest = get_manifest_weights(manifest_numbers) if manifest_numbers else {}

    loaded = 0
    for m in manifests:
        manifest_num = m.get("manifest")
        weight_data = weights_by_manifest.get(manifest_num, {})

        # Upsert: match on technique_trip + manifest to avoid duplicates on re-pull
        existing = (
            db.query(BOLRecord)
            .filter(
                BOLRecord.technique_trip == m["technique_trip"],
                BOLRecord.manifest == manifest_num,
            )
            .first()
        )

        if existing:
            row = existing
            # Clear all invoice-derived fields — fresh slate for today's CSV upload
            row.invoice_number = None
            row.amount         = None
            row.alg_weight     = None
            row.alg_pallets    = None
            row.alg_pcs        = None
            row.access_prog    = None
            row.cost_pct       = None
            row.match_strategy = None
            row.inv_job_number = None
            row.weight_diff    = None
            row.pallet_diff    = None
            row.pcs_diff       = None
            if row.status != BOLStatus.APPROVED:
                row.status      = BOLStatus.PENDING
                row.flag_reason = None
        else:
            row = BOLRecord()
            db.add(row)

        row.technique_trip = m["technique_trip"]
        row.manifest = manifest_num
        # Weight, pallets, and PCS come exclusively from Query B (VisualMail) — no fallback to Query A
        row.technique_weight   = weight_data.get("technique_weight", 0)
        row.technique_pallets  = weight_data.get("technique_pallets", 0)
        row.technique_pcs      = weight_data.get("technique_pcs", 0)
        # access_prog is computed at invoice upload time (per-pallet ZIP from ALG CSV) — not at pull time

        # Type A = no load yet (needs SID export to Prophecy to create BOL)
        # Type B = load_id > 0 (BOL already exists in Prophecy/ShipperPlus; store it)
        _apply_bol_status(row, m)

        # Prophecy pieces from Query A (ShipperPlus join) — save if non-zero
        proph_pcs = m.get("prophecy_pcs") or 0
        if proph_pcs:
            row.prophecy_pcs = proph_pcs

        if existing is None:
            row.status = BOLStatus.PENDING

        loaded += 1

    db.commit()
    logger.info("[PULL] Loaded %d records (days_back=%d)", loaded, days_back)

    # NOTE: stub re-matching and the wide fallback below intentionally run even when
    # `loaded == 0` (no new manifests today) — a day with nothing new despatched is
    # exactly when a stuck invoice_only stub from days ago most needs a chance to
    # resolve. Returning early here used to skip both unconditionally.

    # --- Re-match existing invoice_only stubs against newly loaded manifests ---
    stubs = db.query(BOLRecord).filter(BOLRecord.match_strategy == "invoice_only").all()
    rematched = 0
    for stub in stubs:
        job_name_s = stub.inv_job_number or ""
        if not job_name_s:
            continue

        technique_recs = db.query(BOLRecord).filter(
            BOLRecord.technique_trip.isnot(None),
            BOLRecord.match_strategy != "invoice_only",
            BOLRecord.invoice_number.is_(None),
        ).all()

        match_rec = None
        match_strat = None

        # Strategy 1: trip suffix → job name
        for c in technique_recs:
            if c.technique_trip and _trip_to_suffix(c.technique_trip) == job_name_s:
                match_rec = c
                match_strat = "job_name"
                break

        # Strategy 2: BOL number → job name (non-comingle only)
        if match_rec is None and "Comingle" not in (stub.notes or ""):
            try:
                bol_num = int(job_name_s)
                match_rec = db.query(BOLRecord).filter(
                    BOLRecord.bol_number == bol_num,
                    BOLRecord.match_strategy != "invoice_only",
                ).first()
                if match_rec:
                    match_strat = "bol_number"
            except (ValueError, TypeError):
                pass

        # Strategy 3: pallets + pieces (last resort, non-comingle only)
        if match_rec is None and "Comingle" not in (stub.notes or "") and stub.alg_pallets and stub.alg_pcs:
            candidates = [
                c for c in technique_recs
                if c.technique_pallets == stub.alg_pallets and c.technique_pcs == stub.alg_pcs
            ]
            if len(candidates) == 1:
                match_rec = candidates[0]
                match_strat = "pallets_pieces"
                logger.warning("[PULL RE-MATCH] Strategy pallets+pieces matched %s to %s — verify manually",
                               stub.invoice_number, match_rec.technique_trip)

        if match_rec is None:
            continue

        match_rec.invoice_number = stub.invoice_number
        match_rec.inv_job_number  = stub.inv_job_number
        match_rec.amount          = stub.amount
        match_rec.alg_weight      = stub.alg_weight
        match_rec.alg_pallets     = stub.alg_pallets
        match_rec.alg_pcs         = stub.alg_pcs
        match_rec.match_strategy  = match_strat
        _compute_diffs(match_rec)
        db.delete(stub)
        rematched += 1

    if rematched:
        db.commit()
        logger.info("[PULL] Re-matched %d invoice_only stub(s) to newly loaded manifests", rematched)

    # --- Wide fallback: stubs still unmatched may reference a real trip whose despatch
    # date just falls outside this pull's narrow days_back=1 window. Check a much wider
    # window once, rather than never re-checking Technique at all for these. ---
    remaining_stubs = db.query(BOLRecord).filter(
        BOLRecord.match_strategy == "invoice_only", BOLRecord.bol_number.is_(None)
    ).all()
    wide_resolved = 0
    if remaining_stubs:
        wide_manifests = get_technique_data(days_back=21)
        wide_by_suffix = {_trip_to_suffix(m["technique_trip"]): m for m in wide_manifests if m.get("technique_trip")}
        to_resolve = [
            (s, wide_by_suffix[s.inv_job_number])
            for s in remaining_stubs
            if s.inv_job_number and s.inv_job_number in wide_by_suffix
        ]
        if to_resolve:
            weights_by_manifest = get_manifest_weights([m["manifest"] for _, m in to_resolve])
            for stub, m in to_resolve:
                row = _create_technique_record_from_fallback(db, m, weights_by_manifest.get(m["manifest"], {}))
                row.invoice_number = stub.invoice_number
                row.inv_job_number = stub.inv_job_number
                row.amount         = stub.amount
                row.alg_weight     = stub.alg_weight
                row.alg_pallets    = stub.alg_pallets
                row.alg_pcs        = stub.alg_pcs
                row.match_strategy = "job_name"
                _compute_diffs(row)
                db.delete(stub)
                wide_resolved += 1
            db.commit()
            logger.info("[PULL] Wide fallback (21-day) resolved %d previously-stuck stub(s)", wide_resolved)

    msg = f"Loaded {loaded} manifest(s)."
    if rematched:
        msg += f" Re-matched {rematched} invoice stub(s)."
    if wide_resolved:
        msg += f" Resolved {wide_resolved} previously-stuck stub(s) via wide lookback."
    return {
        "records_loaded": loaded,
        "rematched": rematched,
        "wide_resolved": wide_resolved,
        "date": date.today().isoformat(),
        "message": msg,
    }


@app.post("/api/admin/refetch-bols", tags=["Admin"])
def refetch_bols_for_manifests(body: dict, db: Session = Depends(get_db)):
    """
    Re-query get_technique_data() filtered to specific manifest numbers and update
    bol_number on matching records. Use after Katie imports the SID file into Prophecy
    and creates load numbers — this pulls those new BOL numbers back into the app.
    Not available in mock mode.
    """
    manifest_numbers: list[str] = body.get("manifest_numbers", [])
    if not manifest_numbers:
        raise HTTPException(status_code=400, detail="manifest_numbers is required")

    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Re-fetch BOLs is not available in mock mode.")

    from backend.data_layer import get_technique_data as _get_technique_data
    try:
        all_manifests = _get_technique_data(days_back=30)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Technique query failed: {exc}")

    manifest_set = set(manifest_numbers)
    manifest_map = {m["manifest"]: m for m in all_manifests if m["manifest"] in manifest_set}

    updated = []
    unchanged = []
    for manifest_num in manifest_numbers:
        row = db.query(BOLRecord).filter(BOLRecord.manifest == manifest_num).first()
        if not row:
            unchanged.append({"manifest": manifest_num, "reason": "record not found in DB"})
            continue
        m = manifest_map.get(manifest_num)
        if not m:
            unchanged.append({"manifest": manifest_num, "reason": "not found in recent Technique pull"})
            continue
        load_id = m.get("load_id") or 0
        pooled_id = m.get("pooled_to_load_id") or 0
        new_bol = load_id if load_id > 0 else (pooled_id if pooled_id > 0 else None)
        if new_bol and row.bol_number != new_bol:
            row.bol_number = new_bol
            row.needs_sid_export = False
            updated.append({"manifest": manifest_num, "bol_number": new_bol})
        else:
            unchanged.append({"manifest": manifest_num, "bol_number": row.bol_number, "reason": "no change"})

    if updated:
        db.commit()
    logger.info("[REFETCH-BOLS] Updated %d BOL number(s) for %d manifest(s)", len(updated), len(manifest_numbers))
    return {"updated": updated, "unchanged": unchanged}


# ---------------------------------------------------------------------------
# Invoice CSV processing — shared by upload endpoint and email poller
# ---------------------------------------------------------------------------

def _parse_invoice_folder_name(name: str) -> "tuple[str, datetime] | None":
    """
    Parse a subfolder name like 'Tania 6-25-2026  4-16PM' into
    (display_string, datetime).  Returns None if the name doesn't match.

    The last two whitespace-separated parts are always date + time; everything
    before that is the sender name, so multi-word senders (e.g. "Tania Smith
    6-25-2026 4-16PM") work the same as single-word ones:
        [:-2] sender name   e.g. 'Tania'
        [-2]  date          e.g. '6-25-2026'  (M-D-YYYY)
        [-1]  time          e.g. '4-16PM'     (H-MMAM/PM)
    """
    parts = [p for p in name.split() if p]
    if len(parts) < 3:
        return None
    sender = " ".join(parts[:-2])
    date_part = parts[-2]
    time_part = parts[-1]
    try:
        dt_date = datetime.strptime(date_part, "%m-%d-%Y")
    except ValueError:
        return None
    # Parse time: '4-16PM' or '11-30AM'
    try:
        ampm = time_part[-2:].upper()
        hm = time_part[:-2].split("-")
        hour, minute = int(hm[0]), int(hm[1])
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        dt = dt_date.replace(hour=hour, minute=minute, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None
    # Display string: "Tania 6/25/2026 4:16PM" — built manually for cross-platform compatibility
    h12 = hour % 12 or 12
    display = f"{sender} {dt_date.month}/{dt_date.day}/{dt_date.year} {h12}:{minute:02d}{ampm}"
    return display, dt


def _find_invoice_file(folder: str, z: str, require_csv: bool = False) -> "tuple[str, str] | None":
    """
    Search `folder`'s root plus one level of subfolders (same layout
    poll_invoice_folder scans) for a file whose name starts with Z-number `z`.
    Prefers a .pdf match (ALG's human-readable invoice) over .csv — real ALG
    PDFs are named like "Z557948- Segerdahl Graphics, Inc_.pdf", a prefix
    match rather than an exact one. Most-recently-modified file wins on
    duplicate matches (e.g. a resend). Returns (path, media_type) or None.

    require_csv=True skips the PDF preference and only returns a .csv match —
    used by the recompute-access-prog backfill, which needs to re-parse the
    invoice's line items, not just view/serve the file (GET /api/invoices/{z}/file
    uses the default PDF-preferred behavior).
    """
    if not os.path.isdir(folder):
        return None

    z_upper = z.strip().upper()
    pdf_hits: list[str] = []
    csv_hits: list[str] = []

    def _scan(dir_path: str):
        try:
            entries = os.listdir(dir_path)
        except OSError:
            return
        for entry in entries:
            entry_path = os.path.join(dir_path, entry)
            if not os.path.isfile(entry_path):
                continue
            stem, ext = os.path.splitext(entry)
            if not stem.upper().startswith(z_upper):
                continue
            ext = ext.lower()
            if ext == ".pdf":
                pdf_hits.append(entry_path)
            elif ext == ".csv":
                csv_hits.append(entry_path)

    _scan(folder)
    for entry in os.listdir(folder):
        entry_path = os.path.join(folder, entry)
        if os.path.isdir(entry_path):
            _scan(entry_path)

    if not require_csv and pdf_hits:
        return max(pdf_hits, key=os.path.getmtime), "application/pdf"
    if csv_hits:
        return max(csv_hits, key=os.path.getmtime), "text/csv"
    return None


def _apply_access_prog_calc(
    matched_rec: "BOLRecord",
    match_strategy: "str | None",
    effective_prophecy_bol: "str | None",
    alg_rate_by_zip3: dict,
    fsc_rate_val: "float | None",
    fsc_cost_val: "float | None",
    _get_tariff_rate,
    _diesel_price,
    _fsc_pct,
) -> None:
    """
    Compute access_prog/base_tariff/fsc_pct from SG360's OWN weight/pallet/piece data —
    never ALG's — applied against ALG's own invoiced per-zone rate, since the tariff/zone
    rate structure is legitimately ALG's pricing (using it isn't a violation of
    independence; substituting their weight/pallet counts for ours would be). Sets
    alg_fsc_pct/alg_fsc_cost and the tariff_zone_approximate/weight_source_fallback flags.
    Shared by _process_invoice_csv() (every invoice upload) and the
    POST /api/admin/recompute-access-prog backfill.

    See CLAUDE.md's "access_prog calculation" section for the full rationale/priority order.
    """
    _effective_fsc_pct = Decimal(str(fsc_rate_val)) if fsc_rate_val is not None else _fsc_pct
    matched_rec.alg_fsc_pct = Decimal(str(fsc_rate_val)) if fsc_rate_val is not None else None
    matched_rec.alg_fsc_cost = Decimal(str(round(fsc_cost_val, 2))) if fsc_cost_val is not None else None

    own_pallets: list[tuple[str, float]] = []  # (zip3, weight)
    if match_strategy == "prophecy_bol" and effective_prophecy_bol:
        from backend.data_layer import get_prophecy_pallet_data as _get_prophecy_pallet_data
        for prow in _get_prophecy_pallet_data(int(effective_prophecy_bol)):
            dest_id = prow.get("destination_id")
            dest_zip = prow.get("destination_zip")
            zip3 = dest_id[3:6] if dest_id and len(dest_id) >= 6 else (dest_zip[:3] if dest_zip else None)
            weight = float(prow.get("weight") or 0)
            if zip3 and weight > 0:
                own_pallets.append((zip3, weight))
    elif matched_rec.manifest:
        from backend.data_layer import get_pallet_data_for_manifests as _get_pallet_data_for_manifests
        for prow in _get_pallet_data_for_manifests([matched_rec.manifest]):
            dest_id = prow.get("Dest_ID") or ""
            weight = float(prow.get("Wgt") or 0)
            if len(dest_id) >= 6 and weight > 0:
                own_pallets.append((dest_id[3:6], weight))

    matched_rec.weight_source_fallback = not bool(own_pallets)
    if not own_pallets:
        # No own weight/pallet data available at all (manifest/BOL not found, not yet
        # synced) — no independent data means no independent estimate. Leave access_prog
        # blank rather than substituting ALG's own invoiced weight.
        matched_rec.tariff_zone_approximate = False
        return

    new_tariff_sum = Decimal("0")
    new_base_sum = Decimal("0")
    any_approximate = False
    for zip3, weight in own_pallets:
        # Rate/zone structure is ALG's own pricing — use their invoiced rate for this
        # zone first; our internal rate card is only a fallback for a zone this invoice
        # didn't happen to bill.
        alg_rate = alg_rate_by_zip3.get(zip3)
        if alg_rate is not None:
            base = Decimal(str(round(alg_rate * weight / 100.0, 2)))
            with_fsc = base * (Decimal("1") + _effective_fsc_pct) if _effective_fsc_pct is not None else base
            new_base_sum += base
            new_tariff_sum += with_fsc
            continue
        tariff = _get_tariff_rate(zip3, weight, _diesel_price=_diesel_price, _fsc_pct=_effective_fsc_pct)
        if tariff:
            new_tariff_sum += tariff["access_prog"]
            new_base_sum += tariff.get("base_tariff") or Decimal("0")
            if not tariff.get("is_exact_zone_match"):
                any_approximate = True
        else:
            any_approximate = True

    matched_rec.tariff_zone_approximate = any_approximate
    if new_tariff_sum > 0:
        # Recomputed fresh from our own manifest/BOL data each time (not accumulated
        # per-invoice) — our own weight doesn't change across multiple Z-invoices for the
        # same trip, unlike the old ALG-weight-based calc which needed to add each
        # invoice's own partial line items.
        matched_rec.access_prog = new_tariff_sum
        matched_rec.base_tariff = new_base_sum if new_base_sum > 0 else None
        matched_rec.fsc_pct = _effective_fsc_pct


def _parse_alg_csv_context(reader: "csv.DictReader") -> dict:
    """
    Walk an ALG invoice CSV's rows once, extracting everything invoice-matching and
    _apply_access_prog_calc() need: invoice number, job name, ALG's own per-zone rate,
    total weight/pallets/pieces, FSC rate/cost, and total billed amount. Shared by the
    live upload path (_process_invoice_csv) and the POST /api/admin/recompute-access-prog
    backfill (re-parsing a historical invoice file located via _find_invoice_file()).
    """
    ctx = {
        "invoice_no": None,
        "job_name": None,
        "alg_bol_no": None,
        "cust_job_no": None,
        "total_pcs": 0,
        "total_weight": 0.0,
        "total_pallets": 0,
        "fsc_rate_val": None,
        "fsc_cost_val": None,
        "total_billed": None,
        "alg_rate_by_zip3": {},
    }
    for row in reader:
        inv = (row.get("Invoice No") or "").strip()
        post_office = (row.get("Post Office") or "").strip()

        if "Fuel Surcharge" in post_office:
            try:
                ctx["fsc_rate_val"] = float(row.get("Rate") or 0)
                ctx["fsc_cost_val"] = float(row.get("Billed$") or 0)
            except (ValueError, TypeError):
                pass
            continue

        if "Total Billed Amount" in post_office:
            # The total is in the last populated column
            vals = [v.strip() for v in row.values() if (v or "").strip()]
            try:
                ctx["total_billed"] = float(vals[-1])
            except (ValueError, IndexError):
                pass
            continue

        if not inv or not inv.startswith("Z"):
            continue

        ctx["invoice_no"] = inv
        ctx["job_name"] = (row.get("Job Name") or "").strip()      # matching key
        ctx["alg_bol_no"] = (row.get("BOL No") or "").strip()      # ALG reference, not used for matching
        try:
            ctx["total_pcs"] += int(float(row.get("Pcs") or 0))
            ctx["total_weight"] += float(row.get("GrossWt") or 0)
            ctx["total_pallets"] += int(float(row.get("PalletCount") or 0))
        except (ValueError, TypeError):
            pass
        if ctx["cust_job_no"] is None:
            ctx["cust_job_no"] = (row.get("Cust Job No") or "").strip()
        raw_zip = (row.get("Zip") or "").strip()
        try:
            gross_wt = float(row.get("GrossWt") or 0)
            rate_val = float(row.get("Rate") or 0)
            if raw_zip and gross_wt > 0 and rate_val > 0:
                ctx["alg_rate_by_zip3"].setdefault(raw_zip[:3], rate_val)
        except (ValueError, TypeError):
            pass
    return ctx


def _process_invoice_csv(
    content: bytes,
    filename: str,
    db: Session,
    invoice_email_sender: "str | None" = None,
    invoice_sent_at: "datetime | None" = None,
) -> dict:
    """
    Parse an ALG invoice CSV and match it to a BOLRecord.

    Matching key: "Job Name" field = Technique DespatchID suffix
    (e.g. "110633" → TEC_T_0110633). "BOL No" is ALG's internal ref and
    is NOT used for matching.

    Called by both the manual upload endpoint and the email-poll endpoint so
    both paths apply identical matching and calculation logic.

    invoice_email_sender / invoice_sent_at: populated from the subfolder name
    (e.g. 'Tania 6/25/2026 4:16PM' and datetime(2026,6,25,16,16,tzinfo=utc)).
    Left null when invoked from a flat-file scan or without metadata.
    """
    text_content = content.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text_content))
    ctx = _parse_alg_csv_context(reader)
    invoice_no: Optional[str]       = ctx["invoice_no"]
    job_name: Optional[str]         = ctx["job_name"]        # Technique DespatchID suffix — the real matching key
    alg_bol_no: Optional[str]       = ctx["alg_bol_no"]      # ALG's internal BOL reference (stored for info only)
    total_pcs                       = ctx["total_pcs"]
    total_weight                    = ctx["total_weight"]
    total_pallets                   = ctx["total_pallets"]
    fsc_rate_val: Optional[float]   = ctx["fsc_rate_val"]
    fsc_cost_val: Optional[float]   = ctx["fsc_cost_val"]
    total_billed: Optional[float]   = ctx["total_billed"]
    cust_job_no: Optional[str]      = ctx["cust_job_no"]
    # ALG's own per-zone rate, used as the primary rate source in _apply_access_prog_calc()
    # (the tariff/zone structure is legitimately ALG's pricing) — our internal rate card is
    # only a fallback for a zone this invoice didn't happen to bill.
    alg_rate_by_zip3: dict[str, float] = ctx["alg_rate_by_zip3"]

    if not settings.USE_MOCK_DATA:
        from backend.data_layer import get_tariff_rate as _get_tariff_rate
        from backend.data_layer import get_current_diesel_price, get_fsc_rate as _get_fsc_rate
        _diesel_price = get_current_diesel_price()
        _fsc_pct = _get_fsc_rate(_diesel_price) if _diesel_price is not None else None
        logger.info("[INVOICE] diesel=$%.3f fsc_pct=%s", _diesel_price or 0, _fsc_pct)
    else:
        _get_tariff_rate = None
        _diesel_price = None
        _fsc_pct = None

    if not invoice_no:
        raise HTTPException(
            status_code=422,
            detail="Could not parse Invoice No from the CSV. Check file format.",
        )

    def _is_prophecy_bol(bol_no: str) -> bool:
        """Prophecy BOLs are 6-digit numbers starting with '14' (140000–149999).
        The ALG CSV 'BOL No' column contains Post Office permit numbers (e.g. 401212)
        which also exceed 140000, so we must check the prefix, not just the magnitude.
        Only the Job Name column carries the actual Prophecy BOL.
        """
        try:
            return str(int(bol_no)).startswith("14") and len(str(int(bol_no))) == 6
        except (ValueError, TypeError):
            return False

    matched_rec = None
    match_strategy: Optional[str] = None
    effective_prophecy_bol: Optional[str] = None

    # Try exact, reliable matches first — a real match always beats a "Job Name looks like
    # a Prophecy BOL number" guess, since ordinary trip suffixes can coincidentally fall in
    # the same 140000-149999 numeric range as real Prophecy BOLs (see _is_prophecy_bol).
    # Job Name normally carries the trip suffix (e.g. "110810" → TEC_T_0110810); for a
    # genuine Wolf/311 load with no Technique trip, it instead carries the Prophecy BOL
    # itself — that's only checked below, after ruling out a real trip match.

    # 1. Already uploaded: match by Z-number.
    if settings.USE_MOCK_DATA:
        for rec in _mock_state.values():
            if rec.get("invoice_number") == invoice_no:
                matched_rec = rec
                match_strategy = "invoice_number"
                break
    else:
        matched_rec = (
            db.query(BOLRecord)
            .filter(BOLRecord.invoice_number == invoice_no)
            .first()
        )
        if matched_rec is not None:
            match_strategy = "invoice_number"

    # 2. Job Name as trip suffix.
    if matched_rec is None and job_name:
        if settings.USE_MOCK_DATA:
            for rec in _mock_state.values():
                trip = rec.get("technique_trip") or ""
                if trip and _trip_to_suffix(trip) == job_name:
                    matched_rec = rec
                    match_strategy = "job_name"
                    break
        else:
            for row_obj in db.query(BOLRecord).filter(
                BOLRecord.technique_trip.isnot(None)
            ).all():
                if _trip_to_suffix(row_obj.technique_trip or "") == job_name:
                    matched_rec = row_obj
                    match_strategy = "job_name"
                    break

    # 3. Job Name as a Prophecy BOL (Wolf/311 — no Technique trip for this load). Only
    # reached once steps 1-2 have ruled out this being an ordinary trip suffix.
    if matched_rec is None and job_name and _is_prophecy_bol(job_name):
        effective_prophecy_bol = job_name
        bol_num = int(effective_prophecy_bol)
        if settings.USE_MOCK_DATA:
            for rec in _mock_state.values():
                if rec.get("bol_number") == bol_num:
                    matched_rec = rec
                    match_strategy = "prophecy_bol"
                    break
        else:
            matched_rec = (
                db.query(BOLRecord)
                .filter(BOLRecord.bol_number == bol_num)
                .first()
            )
            if matched_rec is not None:
                match_strategy = "prophecy_bol"

    # 4. Pallets + pieces (last resort, non-comingle only).
    if matched_rec is None and not (cust_job_no or "").upper().startswith("CM") \
            and total_pallets and total_pcs:
        if settings.USE_MOCK_DATA:
            candidates = [
                rec for rec in _mock_state.values()
                if rec.get("technique_pallets") == total_pallets
                and rec.get("technique_pcs") == total_pcs
                and not rec.get("invoice_number")
                and rec.get("technique_trip") is not None
            ]
            if len(candidates) == 1:
                matched_rec = candidates[0]
                match_strategy = "pallets_pieces"
                logger.warning("[INVOICE] pallets+pieces matched %s to %s — verify manually",
                               invoice_no, matched_rec.get("technique_trip"))
        else:
            candidates = db.query(BOLRecord).filter(
                BOLRecord.technique_pallets == total_pallets,
                BOLRecord.technique_pcs == total_pcs,
                BOLRecord.invoice_number.is_(None),
                BOLRecord.technique_trip.isnot(None),
            ).all()
            if len(candidates) == 1:
                matched_rec = candidates[0]
                match_strategy = "pallets_pieces"
                logger.warning("[INVOICE] pallets+pieces matched %s to %s — verify manually",
                               invoice_no, matched_rec.technique_trip)

    if matched_rec is None:
        is_wolf_stub = bool(effective_prophecy_bol)
        stub_bol_number = int(effective_prophecy_bol) if is_wolf_stub else None
        if is_wolf_stub:
            auto_note = f"Wolf/311 load — Prophecy BOL {alg_bol_no}. No matching morning record found."
        elif (cust_job_no or "").upper().startswith("CM"):
            auto_note = f"Comingle — no Technique match. Cust Job No: {cust_job_no}"
        else:
            auto_note = f"No Technique trip for job name '{job_name}'. Validate manually."

        amount_dec_s = Decimal(str(round(total_billed, 2))) if total_billed is not None else None
        alg_weight_dec_s = Decimal(str(round(total_weight, 2))) if total_weight else None
        # access_prog requires Technique weight/ZIP data — not available for unmatched stubs.
        access_prog_s = None
        cost_pct_s = None
        if settings.USE_MOCK_DATA:
            stub_id = str(uuid.uuid4())
            _mock_state[stub_id] = {
                "id": stub_id,
                "technique_trip": None,
                "manifest": None,
                "bol_number": stub_bol_number,
                "inv_job_number": job_name,
                "invoice_number": invoice_no,
                "amount": amount_dec_s,
                "alg_weight": alg_weight_dec_s,
                "alg_pallets": total_pallets or None,
                "alg_pcs": total_pcs or None,
                "access_prog": access_prog_s,
                "cost_pct": cost_pct_s,
                "technique_weight": None,
                "technique_pallets": None,
                "technique_pcs": None,
                "weight_diff": None,
                "pallet_diff": None,
                "pcs_diff": None,
                "prophecy_weight": None,
                "prophecy_pallets": None,
                "prophecy_pcs": None,
                "invoice_email_sender": invoice_email_sender,
                "invoice_sent_at": invoice_sent_at,
                "notes": auto_note,
                "status": "pending",
                "flag_reason": None,
                "match_strategy": "prophecy_bol" if is_wolf_stub else "invoice_only",
                "needs_sid_export": False,
                "no_invoice": False,
                "is_third_party": False,
                "approved_at": None,
                "approved_by": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        else:
            stub = BOLRecord(
                technique_weight      = None,
                technique_pallets     = None,
                technique_pcs         = None,
                bol_number            = stub_bol_number,
                inv_job_number        = job_name,
                invoice_number        = invoice_no,
                invoice_email_sender  = invoice_email_sender,
                invoice_sent_at       = invoice_sent_at,
                amount                = amount_dec_s,
                alg_weight            = alg_weight_dec_s,
                alg_pallets           = total_pallets or None,
                alg_pcs               = total_pcs or None,
                access_prog           = access_prog_s,
                cost_pct              = cost_pct_s,
                notes                 = auto_note,
                status                = BOLStatus.PENDING,
                match_strategy        = "prophecy_bol" if is_wolf_stub else "invoice_only",
                needs_sid_export      = False,
            )
            db.add(stub)
            db.commit()
            # For Wolf/311 stubs: try to fill Prophecy quantities immediately, and — since
            # we already have everything _apply_access_prog_calc() needs (a Prophecy BOL
            # number) — compute Calculated Cost here too, instead of leaving it null until
            # some future invoice happens to re-touch this record.
            if is_wolf_stub and stub_bol_number:
                from backend.data_layer import get_prophecy_data as _get_prophecy_data
                prop = _get_prophecy_data(stub_bol_number)
                if prop:
                    stub.prophecy_weight  = prop["prophecy_weight"]
                    stub.prophecy_pallets = prop["prophecy_pallets"]
                    stub.prophecy_pcs     = prop["prophecy_pcs"]
                    _compute_diffs(stub)
                if _get_tariff_rate is not None:
                    _apply_access_prog_calc(
                        stub, "prophecy_bol", effective_prophecy_bol,
                        alg_rate_by_zip3, fsc_rate_val, fsc_cost_val,
                        _get_tariff_rate, _diesel_price, _fsc_pct,
                    )
                    if stub.amount and stub.access_prog:
                        stub.cost_pct = Decimal(str(round(float(stub.amount) / float(stub.access_prog), 6)))
                db.commit()
        logger.info(
            "[INVOICE] %s → no match, stub created (bol=%s, note=%s)",
            invoice_no, stub_bol_number, auto_note,
        )
        return {
            "matched": False,
            "invoice_number": invoice_no,
            "job_name": job_name,
            "alg_bol_no": alg_bol_no,
            "matched_trip": None,
            "manifest": None,
            "match_strategy": "invoice_only",
            "alg_pcs": total_pcs,
            "alg_weight": round(total_weight, 2),
            "alg_pallets": total_pallets,
            "amount": total_billed,
            "fsc_pct": fsc_rate_val,
            "fsc_cost": fsc_cost_val,
            "message": f"Invoice {invoice_no} has no match — stub record created. {auto_note}",
        }

    amount_dec = Decimal(str(round(total_billed, 2))) if total_billed is not None else None
    alg_weight_dec = Decimal(str(round(total_weight, 2))) if total_weight else None

    def _merge_invoice_numbers(existing: Optional[str], new: str) -> str:
        """Comma-join invoice numbers; skip if already present."""
        if not existing:
            return new
        parts = [p.strip() for p in existing.split(",")]
        if new not in parts:
            parts.append(new)
        return ", ".join(parts)

    def _already_uploaded(existing: Optional[str], new: str) -> bool:
        if not existing:
            return False
        return new in [p.strip() for p in existing.split(",")]

    conflict_info = None

    if settings.USE_MOCK_DATA:
        existing_inv = matched_rec.get("invoice_number")
        already_done = _already_uploaded(existing_inv, invoice_no)
        if existing_inv and not already_done:
            conflict_info = {
                "invoice_number": invoice_no,
                "record_id": matched_rec.get("id"),
                "matched_trip": matched_rec.get("technique_trip"),
                "existing_invoice": existing_inv,
                "existing_amount": float(matched_rec.get("amount") or 0),
                "new_amount": total_billed or 0,
            }
        matched_rec["invoice_number"] = _merge_invoice_numbers(existing_inv, invoice_no)
        if not already_done:
            if existing_inv and amount_dec:
                # Additional invoice for same trip: add billing amount only.
                # Quantities (weight/pallets/pcs) are per-trip totals shared across Z-invoices — don't double-count.
                matched_rec["amount"] = Decimal(str(round(
                    float(matched_rec.get("amount") or 0) + float(amount_dec), 2
                )))
            else:
                matched_rec["amount"] = amount_dec
                matched_rec["alg_weight"] = alg_weight_dec
                matched_rec["alg_pallets"] = total_pallets or None
                matched_rec["alg_pcs"] = total_pcs or None
        matched_rec["match_strategy"] = match_strategy
        matched_rec["inv_job_number"] = job_name
        if invoice_email_sender:
            matched_rec["invoice_email_sender"] = invoice_email_sender
        if invoice_sent_at:
            matched_rec["invoice_sent_at"] = invoice_sent_at
        if matched_rec.get("amount") and matched_rec.get("access_prog"):
            matched_rec["cost_pct"] = round(
                float(matched_rec["amount"]) / float(matched_rec["access_prog"]), 6
            )
        # Diffs: ALG vs Prophecy for Wolf/311, ALG vs Technique for Corp.
        alg_w   = matched_rec.get("alg_weight")
        alg_pal = matched_rec.get("alg_pallets")
        alg_p   = matched_rec.get("alg_pcs")
        if match_strategy == "prophecy_bol":
            ref_w   = matched_rec.get("prophecy_weight")
            ref_pal = matched_rec.get("prophecy_pallets")
            ref_p   = matched_rec.get("prophecy_pcs")
        else:
            ref_w   = matched_rec.get("technique_weight")
            ref_pal = matched_rec.get("technique_pallets")
            ref_p   = matched_rec.get("technique_pcs")
        if alg_w is not None and ref_w:
            matched_rec["weight_diff"] = round(float(alg_w) - float(ref_w), 2)
        if alg_pal is not None and ref_pal is not None:
            matched_rec["pallet_diff"] = alg_pal - ref_pal
        if alg_p is not None and ref_p is not None:
            matched_rec["pcs_diff"] = alg_p - ref_p
        matched_rec["updated_at"] = datetime.now(timezone.utc)
        matched_trip = matched_rec.get("technique_trip")
        matched_manifest = matched_rec.get("manifest")
    else:
        existing_inv = matched_rec.invoice_number
        already_done = _already_uploaded(existing_inv, invoice_no)
        if existing_inv and not already_done:
            conflict_info = {
                "invoice_number": invoice_no,
                "record_id": str(matched_rec.id),
                "matched_trip": matched_rec.technique_trip,
                "existing_invoice": existing_inv,
                "existing_amount": float(matched_rec.amount or 0),
                "new_amount": total_billed or 0,
            }
        matched_rec.invoice_number = _merge_invoice_numbers(existing_inv, invoice_no)
        if not already_done:
            if existing_inv and amount_dec:
                # Additional invoice for same trip: add billing amount only.
                # Quantities (weight/pallets/pcs) are per-trip totals shared across Z-invoices — don't double-count.
                matched_rec.amount = Decimal(str(round(
                    float(matched_rec.amount or 0) + float(amount_dec), 2
                )))
            else:
                matched_rec.amount = amount_dec
                matched_rec.alg_weight = alg_weight_dec
                matched_rec.alg_pallets = total_pallets or None
                matched_rec.alg_pcs = total_pcs or None
        matched_rec.match_strategy = match_strategy
        matched_rec.inv_job_number = job_name
        if invoice_email_sender:
            matched_rec.invoice_email_sender = invoice_email_sender
        if invoice_sent_at:
            matched_rec.invoice_sent_at = invoice_sent_at
        if not already_done:
            _apply_access_prog_calc(
                matched_rec, match_strategy, effective_prophecy_bol,
                alg_rate_by_zip3, fsc_rate_val, fsc_cost_val,
                _get_tariff_rate, _diesel_price, _fsc_pct,
            )
        if matched_rec.amount and matched_rec.access_prog:
            matched_rec.cost_pct = Decimal(
                str(round(float(matched_rec.amount) / float(matched_rec.access_prog), 6))
            )
        # Wolf/311: refresh Prophecy weight/pallets/pcs from ShipperPlus when this invoice
        # matched via a Prophecy BOL number this time around.
        if match_strategy == "prophecy_bol" and effective_prophecy_bol:
            from backend.data_layer import get_prophecy_data as _get_prophecy_data
            prop = _get_prophecy_data(int(effective_prophecy_bol))
            if prop:
                matched_rec.prophecy_weight  = prop["prophecy_weight"]
                matched_rec.prophecy_pallets = prop["prophecy_pallets"]
                matched_rec.prophecy_pcs     = prop["prophecy_pcs"]
        _compute_diffs(matched_rec)
        db.commit()
        db.refresh(matched_rec)
        matched_trip = matched_rec.technique_trip
        matched_manifest = matched_rec.manifest

    logger.info(
        "[INVOICE] Uploaded %s → matched trip %s (job_name=%s alg_bol=%s), amount=$%.2f",
        invoice_no, matched_trip, job_name, alg_bol_no, total_billed or 0,
    )
    return {
        "matched": True,
        "invoice_number": invoice_no,
        "job_name": job_name,
        "alg_bol_no": alg_bol_no,
        "matched_trip": matched_trip,
        "manifest": matched_manifest,
        "match_strategy": match_strategy,
        "alg_pcs": total_pcs,
        "alg_weight": round(total_weight, 2),
        "alg_pallets": total_pallets,
        "amount": total_billed,
        "fsc_pct": fsc_rate_val,
        "fsc_cost": fsc_cost_val,
        "conflict": conflict_info,
        "message": f"Invoice {invoice_no} matched to trip {matched_trip} and updated.",
    }


@app.post("/api/invoices/upload", tags=["Invoices"])
async def upload_alg_invoice(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    invoice_folder_name: Optional[str] = Form(None),
    invoice_sender: Optional[str] = Form(None),
    invoice_date: Optional[str] = Form(None),
    invoice_time: Optional[str] = Form(None),
):
    """
    Upload an ALG invoice CSV (Z-number format from Tanya).

    invoice_folder_name — the sender's dated folder name (e.g. "Tania 6-25-2026  4-16PM"),
      passed when the user selects a whole folder in the frontend. Parsed with the same
      _parse_invoice_folder_name() used by poll_invoice_folder, so sender metadata matches
      regardless of which path the CSV came in through.

    Optional form fields for manual uploads (fallback when invoice_folder_name is absent
    or doesn't parse):
      invoice_sender  — sender name, e.g. "Tania"
      invoice_date    — ISO date string, e.g. "2026-06-25"
      invoice_time    — 24h time string, e.g. "16:16"
    """
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted.")
    content = await file.read()

    # Build sender metadata — prefer the folder name (automatic), fall back to manual fields.
    sender_str: Optional[str] = None
    sent_at: Optional[datetime] = None
    if invoice_folder_name:
        parsed = _parse_invoice_folder_name(invoice_folder_name)
        if parsed:
            sender_str, sent_at = parsed
            logger.info("[UPLOAD] Folder name '%s' → sender='%s'", invoice_folder_name, sender_str)
        else:
            logger.info("[UPLOAD] Folder name '%s' — not parseable, no sender metadata from it", invoice_folder_name)
    if sender_str is None and invoice_sender and invoice_date:
        try:
            d = datetime.strptime(invoice_date, "%Y-%m-%d")
            if invoice_time:
                t = datetime.strptime(invoice_time, "%H:%M")
                sent_dt = d.replace(hour=t.hour, minute=t.minute, tzinfo=timezone.utc)
                h12 = t.hour % 12 or 12
                ampm = "AM" if t.hour < 12 else "PM"
                time_display = f"{h12}:{t.minute:02d}{ampm}"
            else:
                sent_dt = d.replace(tzinfo=timezone.utc)
                time_display = ""
            sent_at = sent_dt
            time_part = f" {time_display}" if time_display else ""
            sender_str = f"{invoice_sender.strip()} {d.month}/{d.day}/{d.year}{time_part}"
        except ValueError:
            pass  # Bad date/time format — proceed without metadata

    result = _process_invoice_csv(content, file.filename or "upload.csv", db,
                                   invoice_email_sender=sender_str,
                                   invoice_sent_at=sent_at)
    result["invoice_email_sender"] = sender_str
    return result


@app.get("/api/invoices/{invoice_number}/file", tags=["Invoices"])
def get_invoice_file(invoice_number: str):
    """
    Serve the original invoice file for a given Z-number, preferring the
    human-readable PDF ALG sends (falls back to CSV if no PDF exists, e.g.
    mock/test data). Searches INVOICE_FOLDER (live) or backend/test_data/
    (mock), including one level of dated sender subfolders
    (e.g. "Tania 6-25-2026  4-16PM/") created by poll_invoice_folder.
    """
    if settings.USE_MOCK_DATA:
        folder = os.path.join(os.path.dirname(__file__), "test_data")
    else:
        folder = settings.INVOICE_FOLDER

    if not folder:
        raise HTTPException(status_code=404, detail="No invoice folder configured (set INVOICE_FOLDER in .env)")

    z = invoice_number.strip().upper()
    hit = _find_invoice_file(folder, z)
    if hit is None:
        raise HTTPException(status_code=404, detail=f"File not found for {invoice_number}. Checked: {folder}")

    path, media_type = hit
    filename = os.path.basename(path)
    disposition = "inline" if media_type == "application/pdf" else "attachment"
    return FileResponse(path, media_type=media_type, filename=filename,
                         headers={"Content-Disposition": f'{disposition}; filename="{filename}"'})


@app.post("/api/invoices/poll-folder", tags=["Invoices"])
def poll_invoice_folder(db: Session = Depends(get_db)):
    """
    Scan INVOICE_FOLDER for unprocessed ALG invoice CSVs and process each.
    Files are NOT moved — "already processed" is tracked by checking invoice_number
    against existing BOLRecord rows (or _mock_state in mock mode).

    In mock mode uses backend/test_data/ as the folder.
    Set INVOICE_FOLDER in .env for live mode.
    """
    if settings.USE_MOCK_DATA:
        folder = os.path.join(os.path.dirname(__file__), "test_data")
    else:
        folder = settings.INVOICE_FOLDER
        # If the process started before INVOICE_FOLDER was added to .env, read the
        # file directly so a restart isn't required.
        if not folder:
            from dotenv import dotenv_values
            _env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
            folder = dotenv_values(_env_path).get("INVOICE_FOLDER", "")

    if not folder:
        raise HTTPException(
            status_code=503,
            detail="INVOICE_FOLDER is not configured. Set INVOICE_FOLDER in .env.",
        )
    if not os.path.isdir(folder):
        raise HTTPException(
            status_code=503,
            detail=f"INVOICE_FOLDER path does not exist: {folder}",
        )

    # Build set of Z-numbers already imported so we skip files already in the DB.
    # ALG CSV filenames are named after their Z-number (e.g. Z557707.CSV).
    if settings.USE_MOCK_DATA:
        existing_invoices = {
            v.get("invoice_number", "").upper()
            for v in _mock_state.values()
            if v.get("invoice_number")
        }
    else:
        existing_invoices = {
            row[0].upper()
            for row in db.query(BOLRecord.invoice_number)
                          .filter(BOLRecord.invoice_number.isnot(None))
                          .all()
        }

    # Build a list of (csv_path, fname, sender_str, sent_at) tuples to process.
    # Priority: named subfolders (sender metadata parsed from folder name) then
    # flat CSVs in root (no metadata — backwards-compat for test_data/ and emergencies).
    file_queue: list[tuple[str, str, "str | None", "datetime | None"]] = []

    for entry in os.listdir(folder):
        entry_path = os.path.join(folder, entry)
        if os.path.isdir(entry_path):
            parsed = _parse_invoice_folder_name(entry)
            sender_str, sent_at = (parsed[0], parsed[1]) if parsed else (None, None)
            if parsed:
                logger.info("[POLL-FOLDER] Subfolder '%s' → sender='%s'", entry, sender_str)
            else:
                logger.info("[POLL-FOLDER] Subfolder '%s' — name not parseable, no sender metadata", entry)
            for fname in os.listdir(entry_path):
                if fname.lower().endswith(".csv") and os.path.splitext(fname)[0].upper() not in existing_invoices:
                    file_queue.append((os.path.join(entry_path, fname), fname, sender_str, sent_at))
        elif entry.lower().endswith(".csv") and os.path.isfile(entry_path):
            if os.path.splitext(entry)[0].upper() not in existing_invoices:
                file_queue.append((entry_path, entry, None, None))

    if not file_queue:
        return {"found": 0, "processed": [], "message": "No new invoice CSV files found in folder."}

    results = []
    for fpath, fname, sender_str, sent_at in file_queue:
        try:
            with open(fpath, "rb") as fh:
                content = fh.read()
            result = _process_invoice_csv(content, fname, db,
                                          invoice_email_sender=sender_str,
                                          invoice_sent_at=sent_at)
            results.append(result)
            logger.info("[POLL-FOLDER] Processed: %s (sender=%s)", fname, sender_str)
        except HTTPException as exc:
            results.append({"error": exc.detail, "filename": fname, "matched": False})
            logger.warning("[POLL-FOLDER] HTTPException processing %s: %s", fname, exc.detail)
        except Exception as exc:
            results.append({"error": str(exc), "filename": fname, "matched": False})
            logger.error("[POLL-FOLDER] Failed to process %s: %s", fname, exc)

    matched = sum(1 for r in results if r.get("matched") and r.get("match_strategy") != "invoice_only")
    stubbed = sum(1 for r in results if not r.get("matched") and not r.get("error"))
    errors  = sum(1 for r in results if r.get("error"))
    msg = f"Processed {len(file_queue)} file(s): {matched} matched, {stubbed} stubbed."
    if errors:
        msg += f" {errors} error(s)."
    return {"found": len(file_queue), "processed": results, "message": msg}


@app.post("/api/admin/recompute-diffs", tags=["Admin"])
def recompute_diffs(db: Session = Depends(get_db)):
    """
    One-time backfill for records whose weight_diff/pallet_diff/pcs_diff were computed
    incorrectly (or not at all) before the Wolf/311 diff bug fix. Pure DB read/recompute/
    write — technique_*/prophecy_*/alg_* values are already stored correctly, only the
    diff math and match_strategy label were wrong, so no live Technique/Prophecy query
    is needed here.
    """
    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Not available in mock mode.")
    checked = 0
    for row in db.query(BOLRecord).filter(BOLRecord.alg_weight.isnot(None)).all():
        # Repair the historical mislabel: a record with a BOL, no technique_trip, and
        # Prophecy quantities is a Wolf/311 load regardless of what match_strategy says.
        if row.bol_number and not row.technique_trip and row.prophecy_weight is not None:
            row.match_strategy = "prophecy_bol"
        _compute_diffs(row)
        checked += 1
    db.commit()
    logger.info("[RECOMPUTE-DIFFS] Checked %d record(s) with an invoice matched", checked)
    return {"records_checked": checked}


@app.post("/api/admin/recompute-access-prog", tags=["Admin"])
def recompute_access_prog(db: Session = Depends(get_db)):
    """
    Backfill Calculated Cost (access_prog) for existing matched records using the
    corrected formula: our own weight/pallets/pieces x ALG's own invoiced per-zone rate.
    ALG's per-zone rate/FSC context isn't stored anywhere — only parsed transiently from
    each invoice's CSV on upload — so this re-locates each record's original file
    (_find_invoice_file, require_csv=True) and re-parses it, then re-runs
    _apply_access_prog_calc() with a fresh live query for our own pallet data. Records
    whose original file can no longer be found, or for which we have no own pallet data
    available, are left untouched and reported separately rather than guessed at.
    """
    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Not available in mock mode.")

    folder = settings.INVOICE_FOLDER
    if not folder or not os.path.isdir(folder):
        raise HTTPException(status_code=404, detail="INVOICE_FOLDER is not configured or does not exist.")

    from backend.data_layer import get_tariff_rate as _get_tariff_rate
    from backend.data_layer import get_current_diesel_price, get_fsc_rate as _get_fsc_rate
    _diesel_price = get_current_diesel_price()
    _fsc_pct = _get_fsc_rate(_diesel_price) if _diesel_price is not None else None

    fixed = 0
    skipped_no_file = 0
    skipped_no_own_data = 0

    for rec in db.query(BOLRecord).filter(BOLRecord.invoice_number.isnot(None)).all():
        hit = _find_invoice_file(folder, rec.invoice_number, require_csv=True)
        if hit is None:
            skipped_no_file += 1
            continue
        path, _media_type = hit
        try:
            with open(path, "rb") as f:
                content = f.read()
        except OSError:
            skipped_no_file += 1
            continue

        reader = csv.DictReader(io.StringIO(content.decode("utf-8", errors="replace")))
        ctx = _parse_alg_csv_context(reader)

        effective_prophecy_bol = (
            str(rec.bol_number) if rec.match_strategy == "prophecy_bol" and rec.bol_number else None
        )
        _apply_access_prog_calc(
            rec, rec.match_strategy, effective_prophecy_bol,
            ctx["alg_rate_by_zip3"], ctx["fsc_rate_val"], ctx["fsc_cost_val"],
            _get_tariff_rate, _diesel_price, _fsc_pct,
        )
        if rec.access_prog is None:
            skipped_no_own_data += 1
            continue
        if rec.amount and rec.access_prog:
            rec.cost_pct = Decimal(str(round(float(rec.amount) / float(rec.access_prog), 6)))
        fixed += 1

    db.commit()
    logger.info(
        "[RECOMPUTE-ACCESS-PROG] fixed=%d skipped_no_file=%d skipped_no_own_data=%d",
        fixed, skipped_no_file, skipped_no_own_data,
    )
    return {"fixed": fixed, "skipped_no_file": skipped_no_file, "skipped_no_own_data": skipped_no_own_data}


@app.post("/api/admin/poll-email", tags=["Admin"])
def poll_alg_email(db: Session = Depends(get_db)):
    """
    Poll the O365 inbox for unread ALG invoice emails from Tanya.

    Finds unread emails matching ALG_SENDER_EMAIL (if set), extracts .csv
    attachments, processes each through the same invoice pipeline as the
    manual upload, and marks the emails as read.

    Requires SMTP_USER + SMTP_PASSWORD in .env (same O365 credentials as
    outbound email). Set ALG_SENDER_EMAIL to Tanya's address to avoid
    processing unrelated emails. In mock mode this endpoint is disabled.
    """
    if settings.USE_MOCK_DATA:
        raise HTTPException(
            status_code=501,
            detail="Email polling is not available in mock mode.",
        )
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="IMAP credentials not configured. Set SMTP_USER and SMTP_PASSWORD in .env.",
        )

    from backend.email_parser import poll_alg_invoice_emails

    try:
        attachments = poll_alg_invoice_emails(
            imap_host=settings.IMAP_HOST,
            imap_port=settings.IMAP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            sender_filter=settings.ALG_SENDER_EMAIL,
            mailbox=settings.IMAP_MAILBOX,
        )
    except Exception as exc:
        logger.error("[EMAIL POLL] IMAP error: %s", exc)
        raise HTTPException(status_code=503, detail=f"IMAP connection failed: {exc}")

    if not attachments:
        return {
            "found": 0,
            "processed": [],
            "message": "No new ALG invoice emails found.",
        }

    results = []
    for fname, csv_bytes in attachments:
        try:
            result = _process_invoice_csv(csv_bytes, fname, db)
            results.append(result)
        except HTTPException as exc:
            results.append({"error": exc.detail, "filename": fname, "matched": False})
        except Exception as exc:
            logger.error("[EMAIL POLL] Failed to process %s: %s", fname, exc)
            results.append({"error": str(exc), "filename": fname, "matched": False})

    matched = sum(1 for r in results if r.get("matched") and r.get("match_strategy") != "invoice_only")
    stubbed = sum(1 for r in results if not r.get("matched") and not r.get("error"))
    errors = sum(1 for r in results if r.get("error"))
    msg = f"Processed {len(attachments)} attachment(s): {matched} matched, {stubbed} stubbed."
    if errors:
        msg += f" {errors} error(s)."
    return {"found": len(attachments), "processed": results, "message": msg}


@app.get("/api/export/prophecy-sid", tags=["Export"])
def export_prophecy_sid(db: Session = Depends(get_db)):
    """
    Generate a Prophecy SID import CSV for all of today's approved manifests.

    Katie imports this file into Prophecy (via the SID import process) to
    create load numbers. The file contains one row per pallet from VisualMail.

    In mock mode: generates synthetic pallet rows from approved mock records
    so the full download flow can be tested without a SQL Server connection.
    """
    filename = get_sid_filename()

    if settings.USE_MOCK_DATA:
        approved = [
            r for r in _mock_state.values()
            if r["status"] == "approved"
            and r.get("needs_sid_export", True)
            and not r.get("is_third_party", False)
            and not r.get("is_ignored", False)
        ]
        if not approved:
            raise HTTPException(
                status_code=422,
                detail="No approved Type-A records to export. Only records that need a Prophecy BOL are included in the SID file.",
            )
        pallet_rows = generate_mock_sid_rows(approved)
        csv_bytes = generate_sid_csv(pallet_rows)
        now = datetime.now(timezone.utc)
        for r in approved:
            r["sid_exported_at"] = now
        logger.info("[SID] Mock export: %d pallet rows for %d Type-A records → %s",
                    len(pallet_rows), len(approved), filename)
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    from backend.data_layer import get_pallet_data_for_manifests

    approved_rows = (
        db.query(BOLRecord)
        .filter(
            BOLRecord.status == BOLStatus.APPROVED,
            BOLRecord.needs_sid_export == True,
            BOLRecord.is_third_party == False,
            BOLRecord.is_ignored == False,
        )
        .all()
    )
    manifests = [r.manifest for r in approved_rows if r.manifest]
    if not manifests:
        raise HTTPException(
            status_code=422,
            detail="No approved records with manifest numbers found. Approve records before exporting.",
        )

    pallet_rows = get_pallet_data_for_manifests(manifests)
    if not pallet_rows:
        raise HTTPException(
            status_code=404,
            detail=f"No pallet data found in VisualMail for {len(manifests)} manifest(s).",
        )

    csv_bytes = generate_sid_csv(pallet_rows)
    now = datetime.now(timezone.utc)
    for r in approved_rows:
        r.sid_exported_at = now
    db.commit()
    logger.info("[SID] Exported %d pallet rows for %d manifests → %s", len(pallet_rows), len(manifests), filename)

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/bols/{record_id}/export-prophecy-sid", tags=["Export"])
def export_prophecy_sid_for_record(record_id: uuid.UUID, db: Session = Depends(get_db)):
    """
    Generate a Prophecy SID import CSV for a single record's manifest —
    the per-record equivalent of GET /api/export/prophecy-sid, for pushing
    one urgent Type A record to Prophecy without waiting to batch-approve
    and export everything at once. Available on pending (not-yet-approved)
    Type A records, per Katie's workflow: check it as soon as she's reviewed
    one record, rather than only after a full batch approval.
    """
    filename_suffix = datetime.now(timezone.utc).strftime("%Y%m%d")

    if settings.USE_MOCK_DATA:
        rec = _mock_state.get(str(record_id))
        if rec is None:
            raise HTTPException(status_code=404, detail="Record not found.")
        if not rec.get("needs_sid_export", True):
            raise HTTPException(status_code=422, detail="This record already has a BOL — nothing to export.")
        pallet_rows = generate_mock_sid_rows([rec])
        csv_bytes = generate_sid_csv(pallet_rows)
        rec["sid_exported_at"] = datetime.now(timezone.utc)
        trip = rec.get("technique_trip") or "record"
        filename = f"SG360_Prophecy_SID_{trip}_{filename_suffix}.csv"
        logger.info("[SID] Mock per-record export: %s → %s", trip, filename)
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    from backend.data_layer import get_pallet_data_for_manifests

    rec = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if rec is None:
        raise HTTPException(status_code=404, detail="Record not found.")
    if not rec.needs_sid_export:
        raise HTTPException(status_code=422, detail="This record already has a BOL — nothing to export.")
    if not rec.manifest:
        raise HTTPException(status_code=422, detail="This record has no manifest number to export.")

    pallet_rows = get_pallet_data_for_manifests([rec.manifest])
    if not pallet_rows:
        raise HTTPException(
            status_code=404,
            detail=f"No pallet data found in VisualMail for manifest {rec.manifest}.",
        )

    csv_bytes = generate_sid_csv(pallet_rows)
    rec.sid_exported_at = datetime.now(timezone.utc)
    db.commit()

    filename = f"SG360_Prophecy_SID_{rec.technique_trip or rec.manifest}_{filename_suffix}.csv"
    logger.info("[SID] Exported %d pallet rows for manifest %s → %s", len(pallet_rows), rec.manifest, filename)

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/bols/{record_id}/refresh-bol", tags=["Admin"])
def refresh_bol_for_record(record_id: uuid.UUID, db: Session = Depends(get_db)):
    """
    Refresh one record's manifest-side data from Technique without re-running
    the full pull across every manifest: (1) re-check VisualMail for updated
    weight/pallets/pieces (Query B — the only source of these fields), and
    (2) check whether Prophecy now has a BOL (load_id/pooled_to_load_id) for
    this manifest (Query A). Meant for the round-trip: Katie exports a SID
    file for one record, imports it into Prophecy, then uses this to confirm
    the BOL number came back — instead of waiting for tomorrow's full pull.

    Does NOT touch invoice-side fields (invoice_number/amount/alg_*/
    access_prog/cost_pct) — those are only recomputed by invoice upload
    (_process_invoice_csv), not by this manifest refresh.

    Reuses get_technique_data()/get_manifest_weights() unchanged (proven,
    already-working queries — the same two the morning pull uses) rather
    than new hand-written single-manifest SQL — heavier than strictly
    necessary, but zero risk of a subtly wrong new query. Revisit if this
    proves too slow in practice.
    """
    if settings.USE_MOCK_DATA:
        raise HTTPException(
            status_code=400,
            detail="Refresh-BOL is disabled in mock mode. Set USE_MOCK_DATA=False in .env.",
        )

    rec = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if rec is None:
        raise HTTPException(status_code=404, detail="Record not found.")
    if not rec.manifest:
        raise HTTPException(status_code=422, detail="This record has no manifest number to check.")
    if not rec.needs_sid_export:
        return {"updated": False, "bol_number": rec.bol_number, "message": "This record already has a BOL."}

    from backend.data_layer import get_technique_data, get_manifest_weights

    messages = []
    updated = False

    # (1) Refresh weight/pallets/pieces from VisualMail (Query B)
    weight_data = get_manifest_weights([rec.manifest]).get(rec.manifest)
    if weight_data:
        new_weight  = weight_data["technique_weight"]
        new_pallets = weight_data["technique_pallets"]
        new_pcs     = weight_data["technique_pcs"]
        if (new_weight, new_pallets, new_pcs) != (rec.technique_weight, rec.technique_pallets, rec.technique_pcs):
            rec.technique_weight  = new_weight
            rec.technique_pallets = new_pallets
            rec.technique_pcs     = new_pcs
            _compute_diffs(rec)
            updated = True
            messages.append("Weight/pallets/pieces updated.")

    # (2) Check BOL status from Technique/ShipperPlus (Query A)
    manifests = get_technique_data(days_back=21)
    match = next((m for m in manifests if m.get("manifest") == rec.manifest), None)
    if match is None:
        messages.append("Manifest not found in Technique for BOL check — try again later.")
    else:
        before = rec.bol_number
        _apply_bol_status(rec, match)
        if rec.bol_number and rec.bol_number != before:
            updated = True
            messages.append(f"BOL {rec.bol_number} found.")

    db.commit()

    if updated:
        logger.info("[REFRESH-BOL] %s → bol=%s weight=%s pallets=%s pcs=%s",
                    rec.manifest, rec.bol_number, rec.technique_weight, rec.technique_pallets, rec.technique_pcs)
    else:
        messages.append("No changes.")

    return {"updated": updated, "bol_number": rec.bol_number, "message": " ".join(messages)}


@app.post("/api/bols/{record_id}/retry-match", tags=["Admin"])
def retry_match_invoice(record_id: uuid.UUID, db: Session = Depends(get_db)):
    """
    On-demand retry for one stuck invoice_only stub: check a wide (21-day) Technique
    window immediately instead of waiting for the next "Pull Manifests" click. Reuses
    _create_technique_record_from_fallback()/_compute_diffs() so this stays in sync
    with the automatic wide fallback in pull_technique_data().
    """
    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Retry-match is disabled in mock mode.")

    stub = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if stub is None:
        raise HTTPException(status_code=404, detail="Record not found.")
    if stub.match_strategy != "invoice_only" or stub.bol_number:
        raise HTTPException(status_code=422, detail="This record isn't a pending unmatched invoice.")

    job_name_s = stub.inv_job_number or ""
    if not job_name_s:
        return {"matched": False, "message": "No job name on this invoice to match against."}

    from backend.data_layer import get_technique_data, get_manifest_weights

    manifests = get_technique_data(days_back=21)
    match = next((m for m in manifests if _trip_to_suffix(m.get("technique_trip") or "") == job_name_s), None)
    if match is None:
        return {"matched": False, "message": "Still not found in Technique (checked last 21 days)."}

    weight_data = get_manifest_weights([match["manifest"]]).get(match["manifest"], {})
    row = _create_technique_record_from_fallback(db, match, weight_data)
    row.invoice_number = stub.invoice_number
    row.inv_job_number  = stub.inv_job_number
    row.amount          = stub.amount
    row.alg_weight      = stub.alg_weight
    row.alg_pallets     = stub.alg_pallets
    row.alg_pcs         = stub.alg_pcs
    row.match_strategy  = "job_name"
    _compute_diffs(row)
    db.delete(stub)
    db.commit()
    logger.info("[RETRY-MATCH] Resolved stub %s → %s", stub.invoice_number, row.technique_trip)
    return {"matched": True, "message": f"Matched to {row.technique_trip}."}


@app.get("/api/logs", response_model=list[BOLSummary], tags=["Logs"])
def get_logs(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    status: Optional[str] = "approved",
    invoice_sender: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Historical log of approved records (default). Pass ?status=all to include pending/flagged.
    Optional ?invoice_sender= filters by partial match on invoice_email_sender.
    Sorted by invoice received date (invoice_sent_at) newest first, then by record creation date.
    """
    if settings.USE_MOCK_DATA:
        all_records = list(_mock_state.values())
        if status and status != "all":
            all_records = [r for r in all_records if r.get("status") == status]
        if start_date:
            all_records = [
                r for r in all_records
                if r.get("invoice_sent_at") and r["invoice_sent_at"].date() >= start_date
                or r.get("created_at") and r["created_at"].date() >= start_date
            ]
        if end_date:
            all_records = [
                r for r in all_records
                if r.get("invoice_sent_at") and r["invoice_sent_at"].date() <= end_date
                or r.get("created_at") and r["created_at"].date() <= end_date
            ]
        if invoice_sender:
            s = invoice_sender.lower()
            all_records = [r for r in all_records if s in (r.get("invoice_email_sender") or "").lower()]
        # Sort: invoice_sent_at desc (nulls last), then created_at desc
        all_records.sort(
            key=lambda r: (
                r.get("invoice_sent_at") is None,
                -(r["invoice_sent_at"].timestamp() if r.get("invoice_sent_at") else 0),
                -(r["created_at"].timestamp() if r.get("created_at") else 0),
            )
        )
        return [_record_to_summary(r) for r in all_records]

    from sqlalchemy import nullslast
    query = db.query(BOLRecord)
    if status and status != "all":
        try:
            query = query.filter(BOLRecord.status == BOLStatus(status))
        except ValueError:
            pass
    if start_date:
        start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
        query = query.filter(
            (BOLRecord.invoice_sent_at >= start_dt) | (BOLRecord.invoice_sent_at.is_(None) & (BOLRecord.created_at >= start_dt))
        )
    if end_date:
        end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc)
        query = query.filter(
            (BOLRecord.invoice_sent_at <= end_dt) | (BOLRecord.invoice_sent_at.is_(None) & (BOLRecord.created_at <= end_dt))
        )
    if invoice_sender:
        query = query.filter(BOLRecord.invoice_email_sender.ilike(f"%{invoice_sender}%"))
    return query.order_by(
        nullslast(BOLRecord.invoice_sent_at.desc()),
        BOLRecord.created_at.desc(),
    ).all()


@app.get("/api/logs/export", tags=["Logs"])
def export_logs(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """Download full log as CSV, optionally filtered by date range."""
    if settings.USE_MOCK_DATA:
        records = list(_mock_state.values())
    else:
        query = db.query(BOLRecord)
        if start_date:
            query = query.filter(BOLRecord.created_at >= datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc))
        if end_date:
            query = query.filter(BOLRecord.created_at <= datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc))
        records = [
            {col.name: getattr(r, col.name) for col in r.__table__.columns}
            for r in query.order_by(BOLRecord.created_at.desc()).all()
        ]

    from backend.csv_export import generate_csv_bytes, get_csv_filename
    csv_bytes = generate_csv_bytes(records)
    filename = f"SG360_BOL_Log_{date.today().strftime('%Y%m%d')}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/export", response_model=ExportResponse, tags=["Export"])
def export_approved_bols(
    body: ExportRequest = ExportRequest(),
    db: Session = Depends(get_db),
):
    """
    Generate CSV of approved records and email to Mary + Katie.
    Email failure is a soft failure — export still succeeds with email_sent=False.
    """
    target = body.export_date or date.today()

    if settings.USE_MOCK_DATA:
        # In mock mode return all approved records regardless of date —
        # mock data doesn't represent real daily batches.
        approved = [r for r in _mock_state.values() if r["status"] == "approved" and not r.get("is_ignored", False)]
    else:
        rows = (
            db.query(BOLRecord)
            .filter(
                BOLRecord.status == BOLStatus.APPROVED,
                BOLRecord.is_ignored == False,
                BOLRecord.approved_at >= datetime(
                    target.year, target.month, target.day, tzinfo=timezone.utc
                ),
            )
            .all()
        )
        approved = [
            {col.name: getattr(r, col.name) for col in r.__table__.columns}
            for r in rows
        ]

    if not approved:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No approved records found for {target.isoformat()}. Approve at least one record before exporting.",
        )

    recipients = body.email_recipients or (settings.EMAIL_TO_MARY + settings.EMAIL_TO_KATIE)
    email_sent = send_bol_export_email(approved, target, recipients)

    return ExportResponse(
        success=True,
        records_exported=len(approved),
        csv_filename=get_csv_filename(target),
        email_sent=email_sent,
        email_recipients=recipients,
        message=(
            f"Exported {len(approved)} record(s). "
            f"Email {'sent to Mary and Katie' if email_sent else 'not sent — SMTP not configured, check logs'}."
        ),
    )
