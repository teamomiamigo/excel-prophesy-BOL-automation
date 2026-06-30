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

from fastapi import FastAPI, Depends, File, HTTPException, Request, UploadFile, status
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
            _conn.commit()
        logger.info("DB column migration for base_tariff/fsc_pct/is_third_party/is_ignored complete.")
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
    """Approved records for today (or export_date if provided)."""
    target = export_date or date.today()

    if settings.USE_MOCK_DATA:
        # In mock mode return all approved records regardless of date —
        # mock data doesn't represent real daily batches.
        return [_record_to_summary(r) for r in _mock_state.values()
                if r["status"] == "approved"]

    rows = (
        db.query(BOLRecord)
        .filter(
            BOLRecord.status == BOLStatus.APPROVED,
            BOLRecord.approved_at >= datetime(
                target.year, target.month, target.day, tzinfo=timezone.utc
            ),
        )
        .all()
    )
    return rows


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
    if not manifests:
        return {"records_loaded": 0, "date": date.today().isoformat(), "message": "No manifests found."}

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
        load_id = m.get("load_id") or 0
        pooled_id = m.get("pooled_to_load_id") or 0
        if load_id > 0 or pooled_id > 0:
            row.needs_sid_export = False
            if load_id > 0 and not row.bol_number:
                row.bol_number = load_id
            elif pooled_id > 0 and not row.bol_number:
                row.bol_number = pooled_id
        else:
            row.needs_sid_export = True

        # Prophecy pieces from Query A (ShipperPlus join) — save if non-zero
        proph_pcs = m.get("prophecy_pcs") or 0
        if proph_pcs:
            row.prophecy_pcs = proph_pcs

        if existing is None:
            row.status = BOLStatus.PENDING

        loaded += 1

    db.commit()
    logger.info("[PULL] Loaded %d records (days_back=%d)", loaded, days_back)

    # --- Re-match existing invoice_only stubs against newly loaded manifests ---
    def _trip_to_suffix_pull(trip: str) -> str:
        parts = trip.split("T_")
        return str(int(parts[-1])) if len(parts) >= 2 else ""

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
            if c.technique_trip and _trip_to_suffix_pull(c.technique_trip) == job_name_s:
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
        if match_rec.alg_weight is not None and match_rec.technique_weight:
            match_rec.weight_diff = Decimal(str(round(float(match_rec.alg_weight) - float(match_rec.technique_weight), 2)))
        if match_rec.alg_pallets is not None and match_rec.technique_pallets is not None:
            match_rec.pallet_diff = match_rec.alg_pallets - match_rec.technique_pallets
        if match_rec.alg_pcs is not None and match_rec.technique_pcs is not None:
            match_rec.pcs_diff = match_rec.alg_pcs - match_rec.technique_pcs
        db.delete(stub)
        rematched += 1

    if rematched:
        db.commit()
        logger.info("[PULL] Re-matched %d invoice_only stub(s) to newly loaded manifests", rematched)

    msg = f"Loaded {loaded} manifest(s)."
    if rematched:
        msg += f" Re-matched {rematched} invoice stub(s)."
    return {"records_loaded": loaded, "rematched": rematched, "date": date.today().isoformat(), "message": msg}


# ---------------------------------------------------------------------------
# Invoice CSV processing — shared by upload endpoint and email poller
# ---------------------------------------------------------------------------

def _process_invoice_csv(content: bytes, filename: str, db: Session) -> dict:
    """
    Parse an ALG invoice CSV and match it to a BOLRecord.

    Matching key: "Job Name" field = Technique DespatchID suffix
    (e.g. "110633" → TEC_T_0110633). "BOL No" is ALG's internal ref and
    is NOT used for matching.

    Called by both the manual upload endpoint and the email-poll endpoint so
    both paths apply identical matching and calculation logic.
    """
    text_content = content.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text_content))
    invoice_no: Optional[str] = None
    job_name: Optional[str] = None   # Technique DespatchID suffix — the real matching key
    alg_bol_no: Optional[str] = None  # ALG's internal BOL reference (stored for info only)
    total_pcs = 0
    total_weight = 0.0
    total_pallets = 0
    fsc_rate_val: Optional[float] = None
    fsc_cost_val: Optional[float] = None
    total_billed: Optional[float] = None
    cust_job_no: Optional[str] = None
    pallet_tariff_sum = Decimal("0")
    pallet_base_tariff_sum = Decimal("0")
    pallet_fsc_pct_last: Optional[Decimal] = None

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

    for row in reader:
        inv = (row.get("Invoice No") or "").strip()
        post_office = (row.get("Post Office") or "").strip()

        if "Fuel Surcharge" in post_office:
            try:
                fsc_rate_val = float(row.get("Rate") or 0)
                fsc_cost_val = float(row.get("Billed$") or 0)
            except (ValueError, TypeError):
                pass
            continue

        if "Total Billed Amount" in post_office:
            # The total is in the last populated column
            vals = [v.strip() for v in row.values() if (v or "").strip()]
            try:
                total_billed = float(vals[-1])
            except (ValueError, IndexError):
                pass
            continue

        if not inv or not inv.startswith("Z"):
            continue

        invoice_no = inv
        job_name = (row.get("Job Name") or "").strip()      # matching key
        alg_bol_no = (row.get("BOL No") or "").strip()      # ALG reference, not used for matching
        try:
            total_pcs += int(float(row.get("Pcs") or 0))
            total_weight += float(row.get("GrossWt") or 0)
            total_pallets += int(float(row.get("PalletCount") or 0))
        except (ValueError, TypeError):
            pass
        if cust_job_no is None:
            cust_job_no = (row.get("Cust Job No") or "").strip()
        if _get_tariff_rate is not None:
            raw_zip = (row.get("Zip") or "").strip()
            try:
                gross_wt = float(row.get("GrossWt") or 0)
                if raw_zip and gross_wt > 0:
                    tariff = _get_tariff_rate(raw_zip[:3], gross_wt,
                                              _diesel_price=_diesel_price, _fsc_pct=_fsc_pct)
                    if tariff:
                        pallet_tariff_sum += tariff["access_prog"]
                        pallet_base_tariff_sum += tariff.get("base_tariff") or Decimal("0")
                        if pallet_fsc_pct_last is None and tariff.get("fsc_pct") is not None:
                            pallet_fsc_pct_last = tariff["fsc_pct"]
            except (ValueError, TypeError):
                pass

    if not invoice_no:
        raise HTTPException(
            status_code=422,
            detail="Could not parse Invoice No from the CSV. Check file format.",
        )

    def _trip_to_suffix(trip: str) -> str:
        """TEC_T_0110633 → '110633'"""
        parts = trip.split("T_")
        return str(int(parts[-1])) if len(parts) >= 2 else ""

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

    # Prophecy BOL lives in Job Name only — the BOL No column is always a permit number.
    effective_prophecy_bol = job_name if job_name and _is_prophecy_bol(job_name) else None

    if effective_prophecy_bol:
        # ── Wolf/311 path ─────────────────────────────────────────────────────
        # Job Name is a Prophecy BOL (14xxxx). No Technique trip for this load.
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
    else:
        # ── Corp / Technique path ─────────────────────────────────────────────
        # Job Name is the trip suffix (e.g. "110810" → TEC_T_0110810).

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

        # 3. Pallets + pieces (last resort, non-comingle only).
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
                "notes": auto_note,
                "status": "pending",
                "flag_reason": None,
                "match_strategy": "invoice_only",
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
                technique_weight  = Decimal("0"),
                technique_pallets = 0,
                technique_pcs     = 0,
                bol_number        = stub_bol_number,
                inv_job_number    = job_name,
                invoice_number    = invoice_no,
                amount            = amount_dec_s,
                alg_weight        = alg_weight_dec_s,
                alg_pallets       = total_pallets or None,
                alg_pcs           = total_pcs or None,
                access_prog       = access_prog_s,
                cost_pct          = cost_pct_s,
                notes             = auto_note,
                status            = BOLStatus.PENDING,
                match_strategy    = "invoice_only",
                needs_sid_export  = False,
            )
            db.add(stub)
            db.commit()
            # For Wolf/311 stubs: try to fill Prophecy quantities immediately.
            if is_wolf_stub and stub_bol_number:
                from backend.data_layer import get_prophecy_data as _get_prophecy_data
                prop = _get_prophecy_data(stub_bol_number)
                if prop:
                    stub.prophecy_weight  = prop["prophecy_weight"]
                    stub.prophecy_pallets = prop["prophecy_pallets"]
                    stub.prophecy_pcs     = prop["prophecy_pcs"]
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
        if pallet_tariff_sum > 0 and not already_done:
            if existing_inv:
                matched_rec.access_prog  = (matched_rec.access_prog  or Decimal("0")) + pallet_tariff_sum
                matched_rec.base_tariff  = (matched_rec.base_tariff  or Decimal("0")) + pallet_base_tariff_sum
            else:
                matched_rec.access_prog = pallet_tariff_sum
                matched_rec.base_tariff = pallet_base_tariff_sum if pallet_base_tariff_sum > 0 else None
            if pallet_fsc_pct_last is not None:
                matched_rec.fsc_pct = pallet_fsc_pct_last
        if matched_rec.amount and matched_rec.access_prog:
            matched_rec.cost_pct = Decimal(
                str(round(float(matched_rec.amount) / float(matched_rec.access_prog), 6))
            )
        # Wolf/311: fill Prophecy weight/pallets/pcs from ShipperPlus, then diff against Prophecy.
        if match_strategy == "prophecy_bol" and effective_prophecy_bol:
            from backend.data_layer import get_prophecy_data as _get_prophecy_data
            prop = _get_prophecy_data(int(effective_prophecy_bol))
            if prop:
                matched_rec.prophecy_weight  = prop["prophecy_weight"]
                matched_rec.prophecy_pallets = prop["prophecy_pallets"]
                matched_rec.prophecy_pcs     = prop["prophecy_pcs"]
                if matched_rec.alg_weight is not None and prop["prophecy_weight"]:
                    matched_rec.weight_diff = Decimal(str(round(
                        float(matched_rec.alg_weight) - float(prop["prophecy_weight"]), 2
                    )))
                if matched_rec.alg_pallets is not None and prop["prophecy_pallets"] is not None:
                    matched_rec.pallet_diff = matched_rec.alg_pallets - prop["prophecy_pallets"]
                if matched_rec.alg_pcs is not None and prop["prophecy_pcs"] is not None:
                    matched_rec.pcs_diff = matched_rec.alg_pcs - prop["prophecy_pcs"]
        else:
            # Corp/Technique: diff against Technique quantities.
            if matched_rec.alg_weight is not None and matched_rec.technique_weight:
                matched_rec.weight_diff = Decimal(str(round(
                    float(matched_rec.alg_weight) - float(matched_rec.technique_weight), 2
                )))
            if matched_rec.alg_pallets is not None and matched_rec.technique_pallets is not None:
                matched_rec.pallet_diff = matched_rec.alg_pallets - matched_rec.technique_pallets
            if matched_rec.alg_pcs is not None and matched_rec.technique_pcs is not None:
                matched_rec.pcs_diff = matched_rec.alg_pcs - matched_rec.technique_pcs
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
):
    """Upload an ALG invoice CSV (Z-number format from Tanya)."""
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted.")
    content = await file.read()
    return _process_invoice_csv(content, file.filename or "upload.csv", db)


@app.get("/api/invoices/{invoice_number}/file", tags=["Invoices"])
def get_invoice_file(invoice_number: str):
    """
    Serve the original invoice CSV file for a given Z-number.
    Looks in INVOICE_FOLDER (live) or backend/test_data/ (mock).
    Also checks a 'processed/' subfolder in case the file was moved after import.
    """
    if settings.USE_MOCK_DATA:
        folder = os.path.join(os.path.dirname(__file__), "test_data")
    else:
        folder = settings.INVOICE_FOLDER

    if not folder:
        raise HTTPException(status_code=404, detail="No invoice folder configured (set INVOICE_FOLDER in .env)")

    z = invoice_number.strip().upper()
    candidates = [
        os.path.join(folder, f"{z}.CSV"),
        os.path.join(folder, f"{z}.csv"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            filename = os.path.basename(path)
            return FileResponse(path, media_type="text/csv", filename=filename,
                                headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    raise HTTPException(status_code=404, detail=f"File not found for {invoice_number}. Checked: {folder}")


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

    candidates = [
        f for f in os.listdir(folder)
        if f.lower().endswith(".csv")
        and os.path.isfile(os.path.join(folder, f))
        and os.path.splitext(f)[0].upper() not in existing_invoices
    ]

    if not candidates:
        return {"found": 0, "processed": [], "message": "No new invoice CSV files found in folder."}

    results = []
    for fname in candidates:
        fpath = os.path.join(folder, fname)
        try:
            with open(fpath, "rb") as fh:
                content = fh.read()
            result = _process_invoice_csv(content, fname, db)
            results.append(result)
            logger.info("[POLL-FOLDER] Processed: %s", fname)
        except HTTPException as exc:
            results.append({"error": exc.detail, "filename": fname, "matched": False})
            logger.warning("[POLL-FOLDER] HTTPException processing %s: %s", fname, exc.detail)
        except Exception as exc:
            results.append({"error": str(exc), "filename": fname, "matched": False})
            logger.error("[POLL-FOLDER] Failed to process %s: %s", fname, exc)

    matched = sum(1 for r in results if r.get("matched") and r.get("match_strategy") != "invoice_only")
    stubbed = sum(1 for r in results if not r.get("matched") and not r.get("error"))
    errors  = sum(1 for r in results if r.get("error"))
    msg = f"Processed {len(candidates)} file(s): {matched} matched, {stubbed} stubbed."
    if errors:
        msg += f" {errors} error(s)."
    return {"found": len(candidates), "processed": results, "message": msg}


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
    logger.info("[SID] Exported %d pallet rows for %d manifests → %s", len(pallet_rows), len(manifests), filename)

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/logs", response_model=list[BOLSummary], tags=["Logs"])
def get_logs(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    status: Optional[str] = "approved",
    db: Session = Depends(get_db),
):
    """
    Historical log of approved records (default). Pass ?status=all to include pending/flagged.
    """
    if settings.USE_MOCK_DATA:
        all_records = list(_mock_state.values())
        if status and status != "all":
            all_records = [r for r in all_records if r.get("status") == status]
        if start_date:
            all_records = [
                r for r in all_records
                if r.get("created_at") and r["created_at"].date() >= start_date
            ]
        if end_date:
            all_records = [
                r for r in all_records
                if r.get("created_at") and r["created_at"].date() <= end_date
            ]
        return [_record_to_summary(r) for r in all_records]

    query = db.query(BOLRecord)
    if status and status != "all":
        try:
            query = query.filter(BOLRecord.status == BOLStatus(status))
        except ValueError:
            pass
    if start_date:
        query = query.filter(BOLRecord.created_at >= datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc))
    if end_date:
        query = query.filter(BOLRecord.created_at <= datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc))
    return query.order_by(BOLRecord.created_at.desc()).all()


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
