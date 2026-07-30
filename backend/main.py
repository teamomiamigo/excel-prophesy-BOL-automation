import csv
import io
import json
import logging
import multiprocessing
import os
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from fastapi import FastAPI, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# fail fast so a network hiccup doesn't eat lambda's whole 29s timeout
_S3_FAST_FAIL = BotoConfig(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1})


class _HardTimeout(Exception):
    """raised when the wrapped call runs past its deadline"""


def _call_with_timeout_target(conn, func, args, kwargs):
    try:
        result = ("ok", func(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001 -- reported back to the parent, not swallowed
        result = ("error", exc)
    try:
        conn.send(result)
    finally:
        conn.close()


def _call_with_timeout(func, seconds, *args, **kwargs):
    # Fixed 2026-07-30, three rounds -- see git history/commit message for the first two
    # (ThreadPoolExecutor, then a plain ProcessPoolExecutor/multiprocessing.Queue) and why each
    # didn't hold up under live testing against job 111427/Z559268's genuinely stuck AWP-SQL-PROD
    # query. The Queue-based process attempt failed a different, more basic way: creating a
    # multiprocessing.Queue needs a Lock (a POSIX named semaphore, sem_open() under /dev/shm),
    # and Lambda's execution environment doesn't reliably provide a writable /dev/shm -- it
    # failed immediately with "[Errno 2] No such file or directory", which this function's own
    # except-and-report-back design silently turned into a false "timed out" instead of a crash.
    # multiprocessing.Pipe() has no such dependency -- it's a plain os.pipe() under the hood, no
    # semaphore/shared-memory involved -- so it works the same regardless of /dev/shm.
    #
    # Known residual risk (found 2026-07-30 via timestamped diagnostic logging, since removed):
    # this backstop still occasionally misses its own deadline -- one request logged NOTHING for
    # 28s before Lambda's hard kill, not even this function's own very first log line, with only
    # trivial non-blocking Python between the prior DB fetch and here. That rules out anything in
    # our own code; it looks like an AWS Lambda infrastructure-level pause of the whole execution
    # environment, not a bug this function can detect or work around. Accepted as a rare residual
    # risk -- the frontend's own auto-retry (up to 3 attempts per stub) absorbs most occurrences.
    ctx = multiprocessing.get_context("fork")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_call_with_timeout_target, args=(child_conn, func, args, kwargs))
    proc.start()
    child_conn.close()  # the parent only needs its read end; the child's copy is closed there
    try:
        if parent_conn.poll(max(1, seconds)):
            status, payload = parent_conn.recv()
            proc.join(timeout=2)
            if status == "error":
                raise payload
            return payload
        # timed out -- kill the stuck worker so its socket to AWP-SQL-PROD actually closes,
        # instead of just declining to wait on it any longer (confirmed live: that alone doesn't
        # bound this call when the underlying query is genuinely stuck, not just slow)
        proc.terminate()
        proc.join(timeout=2)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2)
        raise _HardTimeout(f"exceeded {seconds}s hard deadline")
    finally:
        parent_conn.close()

from backend.config import settings
from backend.database import get_db, engine
from backend.models import (
    Base, BOLRecord, ApprovalHistory, BOLStatus, ActionType,
    BOLSummary, FlagRequest, ApproveRequest,
    ExportRequest, ExportResponse, HealthResponse,
    ManifestCandidate, TripManifestsResponse,
    TariffRate, AlgTariffRate, FuelSurchargeRate,
)
from backend.mock_data import MOCK_BOLS
from backend.email_service import send_bol_export_email
from backend.csv_export import get_csv_filename, get_sid_filename, generate_sid_csv, generate_mock_sid_rows

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# Startup / shutdown

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.USE_MOCK_DATA:
        try:
            Base.metadata.create_all(bind=engine)
        except IntegrityError as exc:
            # concurrent cold starts can race creating the enum type; ignore if it already exists
            if "already exists" not in str(exc.orig):
                raise
            logger.info("DB schema already created by a concurrent cold start; continuing.")
        logger.info("DB tables verified/created.")
        # enum ADD VALUE can't share a transaction with the ADD COLUMN batch below, so its own connection
        with engine.connect() as _enum_conn:
            _enum_conn.execute(text("ALTER TYPE actiontype ADD VALUE IF NOT EXISTS 'DO_NOT_PAY'"))
            _enum_conn.commit()
        # when a column is removed from the model, switch its line below to DROP COLUMN IF EXISTS
        # in the same commit -- an orphaned NOT NULL column with no db-level default breaks every insert
        with engine.connect() as _conn:
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS base_tariff NUMERIC(10,2)"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS fsc_pct NUMERIC(8,6)"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS is_third_party BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.execute(text("ALTER TABLE bol_records DROP COLUMN IF EXISTS is_ignored"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS is_do_not_pay BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS invoice_sent_at TIMESTAMP WITH TIME ZONE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS alg_fsc_pct NUMERIC(8,6)"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS alg_fsc_cost NUMERIC(10,2)"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS tariff_zone_approximate BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS weight_source_fallback BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS is_ambiguous_trip BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS min_charge_uncertain BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS cost_calc_detail TEXT"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS is_dismissed BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.execute(text("ALTER TABLE bol_records ADD COLUMN IF NOT EXISTS mismatch_acknowledged BOOLEAN NOT NULL DEFAULT FALSE"))
            _conn.commit()
        logger.info("DB column migration complete.")
    logger.info(
        "SG360 BOL API started. Mock mode: %s | Version: %s",
        settings.USE_MOCK_DATA,
        settings.APP_VERSION,
    )
    yield
    logger.info("SG360 BOL API shutting down.")


# App
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)


# Middleware
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



# Mock state (in-memory; mutations survive process lifetime, reset on restart)
_mock_state: dict[str, dict] = {r["id"]: dict(r) for r in MOCK_BOLS}


def _find_mock(record_id: str) -> dict:
    # lookup by UUID, invoice_number, or bol_number
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



# Routes
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
        .filter(
            BOLRecord.status != BOLStatus.APPROVED,
            # excludes invoice-less sibling manifest stubs -- see "Ambiguous trips" in CLAUDE.md
            BOLRecord.invoice_number.isnot(None),
        )
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
    # Approved records not yet marked as sent to accounting (accounting_exported_at IS NULL).
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
    # Mark a list of records as sent to accounting by setting accounting_exported_at = now().
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
    # Approve a record. Idempotent — approving an already-approved record is a no-op.
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        if rec["status"] == "approved":
            return _record_to_summary(rec)
        if rec["status"] == "flagged":
            raise HTTPException(status_code=400, detail="Cannot approve a flagged record — unflag it first.")
        # third-party records never get a bol_number but still approve via this endpoint
        if not rec.get("bol_number") and not rec.get("is_third_party"):
            raise HTTPException(status_code=400, detail="Cannot approve a record with no BOL number.")
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
    if row.status == BOLStatus.FLAGGED:
        raise HTTPException(status_code=400, detail="Cannot approve a flagged record — unflag it first.")
    # third-party records never get a bol_number but still approve via this endpoint
    if row.bol_number is None and not row.is_third_party:
        raise HTTPException(status_code=400, detail="Cannot approve a record with no BOL number.")
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
    clear_accounting_export: bool = False,
    db: Session = Depends(get_db),
):
    # revert an approved record back to pending
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        rec["status"] = "pending"
        rec["approved_at"] = None
        rec["approved_by"] = None
        if clear_accounting_export:
            rec["accounting_exported_at"] = None
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    row.status = BOLStatus.PENDING
    row.approved_at = None
    row.approved_by = None
    if clear_accounting_export:
        row.accounting_exported_at = None
    db.add(ApprovalHistory(
        bol_id=row.id,
        action=ActionType.REOPENED,
        performed_by="coordinator",
        reason="Reverted from Log tab (previously sent to accounting)" if clear_accounting_export else None,
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
    # flag a record with reasoning, flagged records excluded from exports
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
    # returns flagged record to pending review, clearing the flag reason
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
    # mark a record third-party (customer pays freight directly)
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        if rec.get("bol_number") is not None or (rec.get("technique_trip") is not None and rec.get("amount") is not None):
            raise HTTPException(
                status_code=400,
                detail="Only records with no BOL number, and not both a Technique trip and an invoice amount, can be marked as third-party.",
            )
        rec["is_third_party"] = True
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    if row.bol_number is not None or (row.technique_trip is not None and row.amount is not None):
        raise HTTPException(
            status_code=400,
            detail="Only records with no BOL number, and not both a Technique trip and an invoice amount, can be marked as third-party.",
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
    # revert 3-party record back to normal pending queue
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


@app.post("/api/bols/{record_id}/dismiss", response_model=BOLSummary, tags=["BOLs"])
def dismiss_sibling(record_id: str, db: Session = Depends(get_db)):
    # hide a bad sibling manifest from the queue; reversible in principle, nothing deleted
    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Dismiss is disabled in mock mode.")

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    if row.invoice_number:
        raise HTTPException(
            status_code=400,
            detail="This record has a real invoice attached — dismiss is only for unmatched sibling manifests.",
        )
    row.is_dismissed = True
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/bols/{record_id}/acknowledge-mismatch", response_model=BOLSummary, tags=["BOLs"])
def acknowledge_mismatch(record_id: str, db: Session = Depends(get_db)):
    # clears the unverified badge for a mismatch with no ambiguous trip to compare against
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        rec["mismatch_acknowledged"] = True
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    row.mismatch_acknowledged = True
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/bols/{record_id}/mark-do-not-pay", response_model=BOLSummary, tags=["BOLs"])
def mark_do_not_pay(record_id: str, db: Session = Depends(get_db)):
    """approve an unmatched invoice as do-not-pay; renders as "DO NOT PAY" instead of an amount"""
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        if rec.get("technique_trip") is not None or not rec.get("invoice_number"):
            raise HTTPException(
                status_code=400,
                detail="Only unmatched invoice-only records (no Technique trip) can be marked Do Not Pay.",
            )
        if rec.get("is_do_not_pay"):
            return _record_to_summary(rec)
        rec["status"] = "approved"
        rec["approved_at"] = datetime.now(timezone.utc)
        rec["approved_by"] = "coordinator"
        rec["flag_reason"] = None
        rec["is_do_not_pay"] = True
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)

    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    if row.technique_trip is not None or not row.invoice_number:
        raise HTTPException(
            status_code=400,
            detail="Only unmatched invoice-only records (no Technique trip) can be marked Do Not Pay.",
        )
    if row.is_do_not_pay:
        return row
    row.status = BOLStatus.APPROVED
    row.approved_at = datetime.now(timezone.utc)
    row.approved_by = "coordinator"
    row.flag_reason = None
    row.is_do_not_pay = True
    db.add(ApprovalHistory(
        bol_id=row.id,
        action=ActionType.DO_NOT_PAY,
        performed_by="coordinator",
    ))
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/bols/{record_id}/unmark-do-not-pay", response_model=BOLSummary, tags=["BOLs"])
def unmark_do_not_pay(record_id: str, db: Session = Depends(get_db)):
    """Undo a do-not-pay marking — reverts to pending review, same as unapprove."""
    if settings.USE_MOCK_DATA:
        rec = _find_mock(record_id)
        rec["status"] = "pending"
        rec["approved_at"] = None
        rec["approved_by"] = None
        rec["is_do_not_pay"] = False
        rec["updated_at"] = datetime.now(timezone.utc)
        return _record_to_summary(rec)
    row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    row.status = BOLStatus.PENDING
    row.approved_at = None
    row.approved_by = None
    row.is_do_not_pay = False
    db.add(ApprovalHistory(
        bol_id=row.id,
        action=ActionType.REOPENED,
        performed_by="coordinator",
    ))
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/bols/{record_id}/reassign-invoice", tags=["BOLs"])
def reassign_invoice(record_id: str, body: dict, db: Session = Depends(get_db)):
    """reassign the invoice on a record to a different trip/bol/manifest, given { target, action }"""
    target_str = (body.get("target") or "").strip()
    action = body.get("action", "preview")

    if not target_str:
        raise HTTPException(status_code=400, detail="target is required")
    if action not in ("preview", "merge", "replace"):
        raise HTTPException(status_code=400, detail="action must be preview, merge, or replace")

    def _find_target_mock(t: str):
        """find the mock record matching target string, or None"""
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
        # Field list mirrors _REASSIGN_SOURCE_CLEAR_FIELDS (the live-DB path below) for
        # mock/live parity -- both now run this for every source, including a bare
        # invoice-only stub with no technique_trip (2026-07-23, see is_stub removal below).
        for field in _REASSIGN_SOURCE_CLEAR_FIELDS:
            rec[field] = None
        rec["tariff_zone_approximate"] = False
        rec["weight_source_fallback"] = False
        rec["min_charge_uncertain"] = False
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
            if target_inv:
                # log what's being discarded instead of silently overwriting it
                old_note = (
                    f"[Reassign] Replaced previous invoice {target_inv} "
                    f"(${float(target_rec.get('amount') or 0):.2f}, wt {target_rec.get('alg_weight') or 0}, "
                    f"pal {target_rec.get('alg_pallets') or 0}, pcs {target_rec.get('alg_pcs') or 0}) with {src_inv}."
                )
                target_rec["notes"] = f"{target_rec.get('notes')} {old_note}" if target_rec.get("notes") else old_note
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

        # clear the source's invoice fields but never delete the record itself
        _clear_invoice_fields(source)

        return {"success": True, "action": action, "target_trip": target_trip}

    # --- Live DB mode ---
    source_row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if not source_row:
        raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
    if not source_row.invoice_number:
        raise HTTPException(status_code=400, detail="Source record has no invoice to reassign")

    # find target -- never a dismissed sibling, those are marked bad/duplicate data
    target_row = None
    try:
        bol_int = int(target_str)
        target_row = db.query(BOLRecord).filter(
            BOLRecord.bol_number == bol_int, BOLRecord.is_dismissed.is_(False),
        ).first()
    except ValueError:
        pass
    if not target_row and target_str.upper().startswith("TEC_T_"):
        target_row = db.query(BOLRecord).filter(
            BOLRecord.technique_trip.ilike(target_str), BOLRecord.is_dismissed.is_(False),
        ).first()
    if not target_row and target_str.upper().startswith("TEC_M_"):
        target_row = db.query(BOLRecord).filter(
            BOLRecord.manifest.ilike(target_str), BOLRecord.is_dismissed.is_(False),
        ).first()
    if not target_row:
        # Suffix match
        for r in db.query(BOLRecord).filter(BOLRecord.is_dismissed.is_(False)).all():
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
    src_inv_job_number = source_row.inv_job_number
    src_invoice_email_sender = source_row.invoice_email_sender
    src_invoice_sent_at = source_row.invoice_sent_at

    if action == "merge":
        target_row.invoice_number = _merge_nums_db(target_inv, src_inv)
        target_row.amount = Decimal(str(round(
            float(target_row.amount or 0) + float(src_amount or 0), 2
        )))
        if not target_inv:
            target_row.alg_weight = src_alg_weight
            target_row.alg_pallets = src_alg_pallets
            target_row.alg_pcs = src_alg_pcs
            target_row.inv_job_number = src_inv_job_number
            target_row.invoice_email_sender = src_invoice_email_sender
            target_row.invoice_sent_at = src_invoice_sent_at
    elif action == "replace":
        if target_inv:
            # log what's being discarded instead of silently overwriting it
            old_note = (
                f"[Reassign] Replaced previous invoice {target_inv} "
                f"(${target_amount or 0:.2f}, wt {target_row.alg_weight or 0}, "
                f"pal {target_row.alg_pallets or 0}, pcs {target_row.alg_pcs or 0}) with {src_inv}."
            )
            target_row.notes = f"{target_row.notes} {old_note}" if target_row.notes else old_note
        target_row.invoice_number = src_inv
        target_row.amount = src_amount
        target_row.alg_weight = src_alg_weight
        target_row.alg_pallets = src_alg_pallets
        target_row.alg_pcs = src_alg_pcs
        target_row.inv_job_number = src_inv_job_number
        target_row.invoice_email_sender = src_invoice_email_sender
        target_row.invoice_sent_at = src_invoice_sent_at

    # recompute calculated cost + cost % now that the invoice lives on this record
    _recompute_access_prog_for_record(target_row, settings.INVOICE_FOLDER)
    # recompute diffs against the target's own weight/pallets/pcs
    _compute_diffs(target_row)
    target_row.updated_at = datetime.now(timezone.utc)

    # clears invoice-derived fields but leaves notes/flag_reason (Katie's own annotations) alone
    for field in _REASSIGN_SOURCE_CLEAR_FIELDS:
        setattr(source_row, field, None)
    source_row.tariff_zone_approximate = False
    source_row.weight_source_fallback = False
    source_row.min_charge_uncertain = False
    source_row.updated_at = datetime.now(timezone.utc)

    db.commit()
    return {"success": True, "action": action, "target_trip": target_trip}


@app.get("/api/bols/{record_id}/trip-manifests", response_model=TripManifestsResponse, tags=["BOLs"])
def get_trip_manifests(record_id: str, db: Session = Depends(get_db)):
    """every manifest sharing this record's trip, scored for manual sibling verification"""
    if settings.USE_MOCK_DATA:
        source = _find_mock(record_id)
        trip = source.get("technique_trip")
        if not trip:
            raise HTTPException(status_code=400, detail="This record has no technique_trip to compare siblings for")
        siblings = [r for r in _mock_state.values() if r.get("technique_trip") == trip]
    else:
        source_row = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
        if not source_row:
            raise HTTPException(status_code=404, detail=f"Record '{record_id}' not found")
        trip = source_row.technique_trip
        if not trip:
            raise HTTPException(status_code=400, detail="This record has no technique_trip to compare siblings for")
        siblings = db.query(BOLRecord).filter(
            BOLRecord.technique_trip == trip, BOLRecord.is_dismissed.is_(False),
        ).all()
        source = source_row

    reference = source if _cget(source, "invoice_number") else next(
        (s for s in siblings if _cget(s, "invoice_number")), None
    )

    score_by_id: dict[str, float] = {}
    best_id: Optional[str] = None
    if reference is not None:
        scored = _score_technique_candidates(
            siblings,
            _cget(reference, "alg_weight"),
            _cget(reference, "alg_pallets"),
            _cget(reference, "alg_pcs"),
        )
        score_by_id = {str(_cget(c, "id")): s for c, s in scored}
        best_id = str(_cget(scored[0][0], "id"))

    candidates = []
    for s in siblings:
        cand = ManifestCandidate.model_validate(s if not isinstance(s, dict) else _record_to_summary(s))
        sid = str(cand.id)
        cand.score = score_by_id.get(sid)
        cand.is_best_fit = sid == best_id if best_id is not None else False
        candidates.append(cand)
    if reference is not None:
        candidates.sort(key=lambda c: c.score if c.score is not None else float("inf"))

    return TripManifestsResponse(
        technique_trip=trip,
        reference_id=_cget(reference, "id") if reference is not None else None,
        invoice_number=_cget(reference, "invoice_number") if reference is not None else None,
        invoice_email_sender=_cget(reference, "invoice_email_sender") if reference is not None else None,
        inv_job_number=_cget(reference, "inv_job_number") if reference is not None else None,
        amount=_cget(reference, "amount") if reference is not None else None,
        alg_weight=_cget(reference, "alg_weight") if reference is not None else None,
        alg_pallets=_cget(reference, "alg_pallets") if reference is not None else None,
        alg_pcs=_cget(reference, "alg_pcs") if reference is not None else None,
        candidates=candidates,
    )


@app.patch("/api/bols/{record_id}/notes", response_model=BOLSummary, tags=["BOLs"])
def update_notes(
    record_id: str,
    body: dict,
    db: Session = Depends(get_db),
):
    """Update the notes field for a record. Called from the dashboard's Notes modal (Save button)."""
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


_INVOICE_FIELDS_TO_NULL = [
    "invoice_number", "invoice_email_sender", "invoice_sent_at", "inv_job_number", "carrier",
    "alg_weight", "alg_pallets", "alg_pcs",
    "access_prog", "amount", "cost_pct", "base_tariff", "fsc_pct", "alg_fsc_pct", "alg_fsc_cost",
    "match_strategy", "weight_diff", "pallet_diff", "pcs_diff", "notes", "flag_reason",
]
_INVOICE_FIELDS_TO_FALSE = ["tariff_zone_approximate", "weight_source_fallback", "min_charge_uncertain"]

# same as _INVOICE_FIELDS_TO_NULL but for reassign_invoice(); excludes notes/flag_reason (Katie's own annotations)
_REASSIGN_SOURCE_CLEAR_FIELDS = [
    "invoice_number", "invoice_email_sender", "invoice_sent_at", "inv_job_number", "carrier",
    "alg_weight", "alg_pallets", "alg_pcs",
    "access_prog", "amount", "cost_pct", "base_tariff", "fsc_pct", "alg_fsc_pct", "alg_fsc_cost",
    "match_strategy", "weight_diff", "pallet_diff", "pcs_diff", "cost_calc_detail",
]


@app.post("/api/admin/reset-invoices", tags=["Admin"])
def reset_all_invoices(confirm: bool = False, db: Session = Depends(get_db)):
    """dev-only: deletes invoice-only stubs and clears ALG-invoice-derived fields on every
    Technique record, even already-approved ones -- resets status to pending unconditionally,
    deliberately, since an approved record with its cost data wiped can't stay approved.
    never touches technique-side fields, is_third_party, sid_exported_at, or the rate tables"""
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass ?confirm=true to reset all invoice data")

    if settings.USE_MOCK_DATA:
        stub_ids = [k for k, v in _mock_state.items() if v.get("match_strategy") == "invoice_only"]
        for sid in stub_ids:
            del _mock_state[sid]
        for rec in _mock_state.values():
            for f in _INVOICE_FIELDS_TO_NULL:
                rec[f] = None
            for f in _INVOICE_FIELDS_TO_FALSE:
                rec[f] = False
            rec["status"] = "pending"
        return {"stubs_deleted": len(stub_ids), "records_cleared": len(_mock_state)}

    # partition in python, not SQL -- a technique_trip IS NOT NULL clause would miss Wolf/311 records too
    all_rows = db.query(BOLRecord).all()
    stubs = [r for r in all_rows if r.match_strategy == "invoice_only"]
    remaining = [r for r in all_rows if r.match_strategy != "invoice_only"]

    for s in stubs:
        db.delete(s)

    for row in remaining:
        for f in _INVOICE_FIELDS_TO_NULL:
            setattr(row, f, None)
        for f in _INVOICE_FIELDS_TO_FALSE:
            setattr(row, f, False)
        row.status = BOLStatus.PENDING

    db.commit()
    return {"stubs_deleted": len(stubs), "records_cleared": len(remaining)}


@app.post("/api/admin/wipe-test-data", tags=["Admin"])
def wipe_test_data(confirm: bool = False, db: Session = Depends(get_db)):
    """dev-only: deletes all bol_records + cascaded approval_history; leaves the rate tables alone"""
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass ?confirm=true to wipe all BOL records")

    if settings.USE_MOCK_DATA:
        count = len(_mock_state)
        _mock_state.clear()
        return {"records_deleted": count}

    rows = db.query(BOLRecord).all()
    count = len(rows)
    # per-row delete, not Query.delete(), so the approval_history cascade actually fires
    for row in rows:
        db.delete(row)
    db.commit()
    return {"records_deleted": count}


def _apply_bol_status(row: "BOLRecord", technique_row: dict) -> None:
    """set bol_number/needs_sid_export from a technique row's load_id/pooled_to_load_id"""
    load_id = technique_row.get("load_id") or 0
    pooled_id = technique_row.get("pooled_to_load_id") or 0
    if load_id > 0 or pooled_id > 0:
        row.needs_sid_export = False
        if load_id > 0 and not row.bol_number:
            row.bol_number = load_id
        elif pooled_id > 0 and not row.bol_number:
            row.bol_number = pooled_id
    elif not row.bol_number:
        row.needs_sid_export = True
    # else: already type B -- a later query with no load_id is a transient hiccup, not a vanished BOL


def _select_canonical_technique_row(rows: list[dict]) -> dict:
    """pick one canonical row per (trip, manifest) group -- prefer unpooled+prepaid, but
    take load_id/pooled_to_load_id as max() across the group so a real BOL is never lost"""
    if len(rows) == 1:
        return rows[0]

    def _sort_key(r: dict) -> tuple:
        tran_rank = 0 if (r.get("tran_type") or "").strip().lower() == "prepaid" else 1
        pooled_rank = 0 if not (r.get("pooled_to_load_id") or 0) else 1
        return (tran_rank, pooled_rank)

    ordered = sorted(rows, key=_sort_key)
    canonical = dict(ordered[0])
    canonical["load_id"] = max((r.get("load_id") or 0) for r in rows)
    canonical["pooled_to_load_id"] = max((r.get("pooled_to_load_id") or 0) for r in rows)
    logger.info(
        "[TECHNIQUE DEDUP] %s / %s: %d duplicate rows collapsed -> load_id=%s pooled_id=%s",
        canonical.get("technique_trip"), canonical.get("manifest"), len(rows),
        canonical["load_id"], canonical["pooled_to_load_id"],
    )
    return canonical


def _dedupe_technique_rows(rows: list[dict]) -> list[dict]:
    """collapse get_technique_data() rows to one per (trip, manifest) via _select_canonical_technique_row()"""
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for r in rows:
        key = (r.get("technique_trip"), r.get("manifest"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    return [_select_canonical_technique_row(groups[k]) for k in order]


def _compute_diffs(row: "BOLRecord") -> None:
    """weight/pallet/pcs diff = ALG qty - our own qty (prophecy baseline for Wolf/311, else technique).
    left null with no technique_trip or prophecy data -- there's no real baseline to diff against"""
    has_technique = bool(row.technique_trip)
    has_prophecy  = row.prophecy_weight is not None or row.prophecy_pallets is not None
    if not has_technique and not has_prophecy:
        row.weight_diff  = None
        row.pallet_diff  = None
        row.pcs_diff     = None
        return

    is_prophecy = not has_technique and has_prophecy
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


def _append_note_to(row: "BOLRecord", text: str) -> None:
    """idempotent notes-append -- skips if the same note is already there"""
    if text not in (row.notes or ""):
        row.notes = f"{row.notes} {text}".strip() if row.notes else text


_NO_ACTIVE_PALLET_DATA_NOTE = (
    "No active-pallet weight data in VisualMail for this manifest "
    "(Query B / Active=1 returned nothing) — weight/pallets/pcs left at 0."
)


def _trip_to_suffix(trip: str) -> str:
    """e.g. 'TEC_T_0110977' -> '110977'. Shared by invoice matching and stub re-matching."""
    parts = (trip or "").split("T_")
    if len(parts) < 2:
        return ""
    try:
        return str(int(parts[-1]))
    except ValueError:
        return ""


def _manifest_to_suffix(manifest: str) -> str:
    """e.g. 'TEC_M_0228920' -> '228920'. fallback key for invoices whose Job Name is the manifest, not the trip"""
    parts = (manifest or "").split("M_")
    if len(parts) < 2:
        return ""
    try:
        return str(int(parts[-1]))
    except ValueError:
        return ""


def _create_technique_record_from_fallback(db: Session, m: dict, weight_data: dict) -> "BOLRecord":
    """create a new BOLRecord for a manifest found only via the wide-window technique fallback query"""
    row = BOLRecord(status=BOLStatus.PENDING)
    db.add(row)
    row.technique_trip = m["technique_trip"]
    row.manifest = m["manifest"]
    row.technique_weight  = weight_data.get("technique_weight", 0)
    row.technique_pallets = weight_data.get("technique_pallets", 0)
    row.technique_pcs     = weight_data.get("technique_pcs", 0)
    if not weight_data:
        _append_note_to(row, _NO_ACTIVE_PALLET_DATA_NOTE)
    row.is_ambiguous_trip = (m.get("_trip_manifest_count") or 0) > 1
    _apply_bol_status(row, m)
    proph_pcs = m.get("prophecy_pcs") or 0
    if proph_pcs:
        row.prophecy_pcs = proph_pcs
    return row


@app.post("/api/admin/refetch-bols", tags=["Admin"])
def refetch_bols_for_manifests(body: dict, db: Session = Depends(get_db)):
    """re-query technique for specific manifests and update bol_number; use after Katie imports the SID file"""
    manifest_numbers: list[str] = body.get("manifest_numbers", [])
    if not manifest_numbers:
        raise HTTPException(status_code=400, detail="manifest_numbers is required")

    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Re-fetch BOLs is not available in mock mode.")

    from backend.data_layer import get_technique_data as _get_technique_data
    try:
        all_manifests = _dedupe_technique_rows(_get_technique_data(days_back=30))
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

_INVOICE_FOLDER_TIME_RE = re.compile(r"(\d{1,2})-?(\d{2})(AM|PM)", re.IGNORECASE)


def _parse_invoice_folder_name(name: str) -> "tuple[str, datetime] | None":
    """parse 'Tania 6-25-2026 4-16PM' into (display_string, datetime); last two words are
    always date + time, everything before is the sender name; None if it doesn't match"""
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
    # Parse time: '4-16PM', '11-30AM', or the no-dash '156PM'/'1230PM'
    m = _INVOICE_FOLDER_TIME_RE.fullmatch(time_part.strip())
    if not m:
        return None
    try:
        hour, minute = int(m.group(1)), int(m.group(2))
        ampm = m.group(3).upper()
        if not (1 <= hour <= 12) or not (0 <= minute <= 59):
            return None
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
    """search folder's root + one level of subfolders for a file whose name starts with
    z-number z; prefers .pdf over .csv (prefix match, newest wins on duplicates).
    require_csv=True skips the PDF preference -- for callers that need to re-parse line items"""
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


def _fetch_invoice_pdf_bytes(z: str) -> "bytes | None":
    """fetch a z-number's PDF bytes: S3, then local cache, then INVOICE_FOLDER; None if not found"""
    if not settings.USE_MOCK_DATA and settings.INVOICE_S3_BUCKET:
        try:
            resp = boto3.client("s3", config=_S3_FAST_FAIL).get_object(
                Bucket=settings.INVOICE_S3_BUCKET, Key=f"{z}.pdf"
            )
            return resp["Body"].read()
        except Exception:
            pass
    cache_path = os.path.join(_INVOICE_PDF_CACHE_DIR, f"{z}.pdf")
    if os.path.isfile(cache_path):
        with open(cache_path, "rb") as fh:
            return fh.read()
    folder = (
        settings.INVOICE_FOLDER
        if not settings.USE_MOCK_DATA
        else os.path.join(os.path.dirname(__file__), "test_data")
    )
    hit = _find_invoice_file(folder, z)  # prefers PDF over CSV by default
    if hit:
        path, _ = hit
        if path.lower().endswith(".pdf"):
            with open(path, "rb") as fh:
                return fh.read()
    return None


# local fallback cache for dev machines with no S3 bucket configured; mirrors the S3 key layout
_INVOICE_PDF_CACHE_DIR = os.path.join(os.path.dirname(__file__), "invoice_pdf_cache")


def _store_invoice_pdf_bytes(z: str, data: bytes) -> None:
    """persist one invoice's PDF: S3 if configured, else local cache. best-effort, logs on failure"""
    if settings.INVOICE_S3_BUCKET:
        try:
            boto3.client("s3", config=_S3_FAST_FAIL).put_object(
                Bucket=settings.INVOICE_S3_BUCKET,
                Key=f"{z}.pdf",
                Body=data,
                ContentType="application/pdf",
            )
        except Exception as exc:
            logger.error("[INVOICE-PDF] Failed to store PDF for %s in S3: %s", z, exc)
        return
    os.makedirs(_INVOICE_PDF_CACHE_DIR, exist_ok=True)
    with open(os.path.join(_INVOICE_PDF_CACHE_DIR, f"{z}.pdf"), "wb") as fh:
        fh.write(data)


def _store_batch_pdf_bytes(slug: str, data: bytes) -> None:
    """same S3-or-local-cache split as _store_invoice_pdf_bytes(), under a batches/ prefix"""
    if settings.INVOICE_S3_BUCKET:
        try:
            boto3.client("s3", config=_S3_FAST_FAIL).put_object(
                Bucket=settings.INVOICE_S3_BUCKET,
                Key=f"batches/{slug}.pdf",
                Body=data,
                ContentType="application/pdf",
            )
        except Exception as exc:
            logger.error("[INVOICE-PDF] Failed to store batch PDF '%s' in S3: %s", slug, exc)
        return
    batch_dir = os.path.join(_INVOICE_PDF_CACHE_DIR, "batches")
    os.makedirs(batch_dir, exist_ok=True)
    with open(os.path.join(batch_dir, f"{slug}.pdf"), "wb") as fh:
        fh.write(data)


def _fetch_batch_pdf_bytes(slug: str) -> "bytes | None":
    """fetch a previously-merged batch PDF: S3 if configured, else local cache; None if not found"""
    if not settings.USE_MOCK_DATA and settings.INVOICE_S3_BUCKET:
        try:
            resp = boto3.client("s3", config=_S3_FAST_FAIL).get_object(
                Bucket=settings.INVOICE_S3_BUCKET, Key=f"batches/{slug}.pdf"
            )
            return resp["Body"].read()
        except Exception:
            return None
    path = os.path.join(_INVOICE_PDF_CACHE_DIR, "batches", f"{slug}.pdf")
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            return fh.read()
    return None


def _slugify_sender(label: str) -> str:
    """turn a sender label into a filesystem/S3-safe storage key; see _readable_batch_name() for display"""
    import re
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", label.strip()).strip("_")
    return slug or "unassigned"


def _readable_batch_name(label: str) -> str:
    """human-readable download filename -- only swaps chars windows/macos forbid, unlike the opaque slug"""
    import re
    name = re.sub(r'[\\/:*?"<>|]+', '-', label.strip())
    return name or "batch"


def _collect_batch_invoice_numbers(sender: str, db: Session) -> "list[str]":
    """distinct z-numbers for this sender, splitting comma-joined invoice_number values"""
    if settings.USE_MOCK_DATA:
        raw = [
            v.get("invoice_number") for v in _mock_state.values()
            if v.get("invoice_email_sender") == sender and v.get("invoice_number")
        ]
    else:
        raw = [
            row[0] for row in db.query(BOLRecord.invoice_number)
                .filter(BOLRecord.invoice_email_sender == sender,
                        BOLRecord.invoice_number.isnot(None))
                .all()
        ]
    seen: list[str] = []
    for val in raw:
        for z in [p.strip() for p in val.split(",")]:
            if z and z not in seen:
                seen.append(z)
    return seen


def _merge_and_store_batch_pdf(sender: str, db: Session) -> dict:
    """merge every locatable PDF for one sender batch and cache it; safe to re-call, a
    missing PDF is skipped rather than failing the whole merge"""
    from pypdf import PdfWriter, PdfReader
    import io as _io

    z_list = _collect_batch_invoice_numbers(sender, db)
    writer = PdfWriter()
    missing: list[str] = []
    for z in z_list:
        pdf_bytes = _fetch_invoice_pdf_bytes(z)
        if pdf_bytes is None:
            missing.append(z)
            continue
        for page in PdfReader(_io.BytesIO(pdf_bytes)).pages:
            writer.add_page(page)

    if len(writer.pages) == 0:
        logger.warning(
            "[BATCH-PDF] No PDFs locatable for sender '%s' (%d invoice(s), all missing)",
            sender, len(z_list),
        )
        return {"merged": False, "pdf_count": 0, "invoice_count": len(z_list), "missing": missing}

    buf = _io.BytesIO()
    writer.write(buf)
    slug = _slugify_sender(sender)
    _store_batch_pdf_bytes(slug, buf.getvalue())
    logger.info(
        "[BATCH-PDF] Merged %d/%d PDF(s) for sender '%s' -> batches/%s.pdf",
        len(z_list) - len(missing), len(z_list), sender, slug,
    )
    return {
        "merged": True,
        "pdf_count": len(z_list) - len(missing),
        "invoice_count": len(z_list),
        "missing": missing,
    }


# zip3 tolerance for zone matching -- SCF zone codes and ALG's billed zip3 are usually a few digits apart, never exact
_ALG_ZONE_TOLERANCE = 5


def _lookup_alg_rate(alg_rate_by_zip3: dict, zip3: str) -> "float | None":
    """exact zip3 hit first, else nearest invoice zone within _ALG_ZONE_TOLERANCE"""
    rate = alg_rate_by_zip3.get(zip3)
    if rate is not None:
        return rate
    try:
        z = int(zip3)
    except (ValueError, TypeError):
        return None
    best_rate, best_dist = None, _ALG_ZONE_TOLERANCE + 1
    for key, key_rate in alg_rate_by_zip3.items():
        try:
            dist = abs(int(key) - z)
        except (ValueError, TypeError):
            continue
        if dist < best_dist:
            best_rate, best_dist = key_rate, dist
    return best_rate if best_dist <= _ALG_ZONE_TOLERANCE else None


# below this per-zone rated-weight fraction, fall back to the invoice's blended rate instead
# (effectively requires full coverage -- a lower threshold used to silently under-report partial loads)
_RATE_COVERAGE_THRESHOLD = 0.999999


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
    alg_blended_rate: "float | None" = None,
    alg_min_charge_by_zip3: "dict | None" = None,
    detail: "list | None" = None,
    learn: bool = True,
) -> None:
    """compute access_prog/base_tariff/fsc_pct from our own weight/pallet/piece data (never ALG's)
    against ALG's own per-zone rate -- using their rate structure is fine, using their quantities isn't.
    alg_blended_rate: whole-load $/cwt fallback when per-zone coverage is incomplete.
    alg_min_charge_by_zip3: per-zip3 minimum-charge floors actually billed on this invoice.
    detail: if a list, appended with one per-pallet pricing breakdown; never mutates the DB itself.
    learn: set False to skip the alg_tariff_rates reconciliation pass.
    see CLAUDE.md's "access_prog calculation" section for the full priority order."""
    from backend.data_layer import get_alg_tariff_rate, reconcile_alg_tariff_rates

    alg_min_charge_by_zip3 = alg_min_charge_by_zip3 or {}

    _effective_fsc_pct = Decimal(str(fsc_rate_val)) if fsc_rate_val is not None else _fsc_pct
    matched_rec.alg_fsc_pct = Decimal(str(fsc_rate_val)) if fsc_rate_val is not None else None
    matched_rec.alg_fsc_cost = Decimal(str(round(fsc_cost_val, 2))) if fsc_cost_val is not None else None

    own_pallets: list[tuple[str, float, "str | None"]] = []  # (zip3, weight, exact_dest_id)
    if effective_prophecy_bol:
        from backend.data_layer import get_prophecy_pallet_data as _get_prophecy_pallet_data
        # degrade gracefully -- a hung query here leaves own_pallets empty, not a failed upload
        try:
            prophecy_rows = _get_prophecy_pallet_data(int(effective_prophecy_bol))
        except Exception as exc:
            logger.error(
                "[ACCESS_PROG] get_prophecy_pallet_data failed for BOL %s: %s",
                effective_prophecy_bol, exc,
            )
            prophecy_rows = []
        for prow in prophecy_rows:
            dest_id = prow.get("destination_id")
            dest_zip = prow.get("destination_zip")
            # prefer the real zip3 over the SCF zone code -- ALG bills actual zip3s, see _ALG_ZONE_TOLERANCE
            zip3 = (dest_zip[:3] if dest_zip else None) or (dest_id[3:6] if dest_id and len(dest_id) >= 6 else None)
            weight = float(prow.get("weight") or 0)
            if zip3 and weight > 0:
                own_pallets.append((zip3, weight, dest_id))
    elif matched_rec.manifest:
        from backend.data_layer import get_pallet_data_for_manifests as _get_pallet_data_for_manifests
        # unlike its SID-export caller, degrade gracefully here instead of 500ing the whole request
        try:
            manifest_pallet_rows = _get_pallet_data_for_manifests([matched_rec.manifest])
        except Exception as exc:
            logger.error(
                "[ACCESS_PROG] get_pallet_data_for_manifests failed for manifest %s: %s",
                matched_rec.manifest, exc,
            )
            manifest_pallet_rows = []
        for prow in manifest_pallet_rows:
            dest_id = prow.get("Dest_ID") or ""
            dest_zip = prow.get("Dest_Zip")
            weight = float(prow.get("Wgt") or 0)
            # prefer the real ZIP over slicing the destination code -- the code's digits are a zone label, not always the real zip3
            zip3 = (str(dest_zip)[:3] if dest_zip else None) or (dest_id[3:6] if dest_id and len(dest_id) >= 6 else None)
            if zip3 and weight > 0:
                own_pallets.append((zip3, weight, dest_id))

    matched_rec.weight_source_fallback = not bool(own_pallets)
    if not own_pallets:
        # no independent weight data -- leave access_prog blank rather than use ALG's own weight
        matched_rec.tariff_zone_approximate = False
        matched_rec.min_charge_uncertain = False
        return

    new_tariff_sum = Decimal("0")
    new_base_sum = Decimal("0")
    any_approximate = False
    any_min_charge_uncertain = False
    # (dest_id, rate, floor $) for exact zip3 hits only -- fed to reconcile_alg_tariff_rates() after the loop
    to_learn: list[tuple[str, float, "float | None"]] = []
    total_weight = sum(w for _, w, _ in own_pallets)
    rated_weight = 0.0
    for zip3, weight, exact_dest_id in own_pallets:
        direct_rate = alg_rate_by_zip3.get(zip3)
        if exact_dest_id and direct_rate is not None:
            to_learn.append((exact_dest_id, direct_rate, alg_min_charge_by_zip3.get(zip3)))

        # use ALG's own invoiced rate for this zone first; our rate card is only a fallback
        alg_rate = _lookup_alg_rate(alg_rate_by_zip3, zip3)
        if alg_rate is not None:
            base = Decimal(str(round(alg_rate * weight / 100.0, 2)))
            # apply ALG's per-shipment minimum freight floor too, sourced from alg_tariff_rates.mc1
            # first (their complete export) and only the older, gappier tariff_rates card as fallback
            alg_min = get_alg_tariff_rate(exact_dest_id) if exact_dest_id else None
            mc1_used = None
            mc1_source = None
            if alg_min is not None:
                base = max(base, alg_min["mc1"])
                mc1_used, mc1_source = alg_min["mc1"], "alg_tariff_rates"
            else:
                # only flag min_charge_uncertain if the floor actually determined the price
                zone_info = _get_tariff_rate(zip3, weight, _diesel_price=_diesel_price, _fsc_pct=_effective_fsc_pct)
                if zone_info and zone_info.get("minimum_freight") is not None:
                    base = max(base, zone_info["minimum_freight"])
                    mc1_used, mc1_source = zone_info["minimum_freight"], "legacy_tariff_rates"
                    if base == mc1_used:
                        any_min_charge_uncertain = True
                else:
                    # no floor info anywhere -- silence isn't confirmation that no floor applies
                    any_min_charge_uncertain = True
            with_fsc = base * (Decimal("1") + _effective_fsc_pct) if _effective_fsc_pct is not None else base
            new_base_sum += base
            new_tariff_sum += with_fsc
            rated_weight += weight
            if detail is not None:
                detail.append({
                    "dest_id": exact_dest_id, "zip3": zip3, "weight": weight,
                    "rate_source": "invoice_own_rate", "rate_used": alg_rate,
                    "mc1_used": float(mc1_used) if mc1_used is not None else None,
                    "mc1_source": mc1_source,
                    "floored": mc1_used is not None and base == mc1_used,
                    "base": float(base), "with_fsc": float(with_fsc),
                })
            continue
        # this invoice didn't bill this zone -- next try an exact alg_tariff_rates match on dest_id
        alg_tariff = get_alg_tariff_rate(exact_dest_id) if exact_dest_id else None
        if alg_tariff is not None:
            base = Decimal(str(round(float(alg_tariff["rate1"]) * weight / 100.0, 2)))
            base = max(base, alg_tariff["mc1"])
            with_fsc = base * (Decimal("1") + _effective_fsc_pct) if _effective_fsc_pct is not None else base
            new_base_sum += base
            new_tariff_sum += with_fsc
            rated_weight += weight
            if detail is not None:
                detail.append({
                    "dest_id": exact_dest_id, "zip3": zip3, "weight": weight,
                    "rate_source": "alg_tariff_rates", "rate_used": float(alg_tariff["rate1"]),
                    "mc1_used": float(alg_tariff["mc1"]), "mc1_source": "alg_tariff_rates",
                    "floored": base == alg_tariff["mc1"],
                    "base": float(base), "with_fsc": float(with_fsc),
                })
            continue
        tariff = _get_tariff_rate(zip3, weight, _diesel_price=_diesel_price, _fsc_pct=_effective_fsc_pct)
        if tariff:
            new_tariff_sum += tariff["access_prog"]
            new_base_sum += tariff.get("base_tariff") or Decimal("0")
            rated_weight += weight
            if not tariff.get("is_exact_zone_match"):
                any_approximate = True
                logger.warning(
                    "[ZONE GAP] invoice=%s zip3=%s weight=%.2f — no exact tariff_rates match, "
                    "fell back to nearest zone (Phil needs a real rate for this zip3)",
                    getattr(matched_rec, "invoice_number", None), zip3, weight,
                )
            if detail is not None:
                detail.append({
                    "dest_id": exact_dest_id, "zip3": zip3, "weight": weight,
                    "rate_source": "legacy_tariff_rates", "rate_used": None,
                    "mc1_used": float(tariff["minimum_freight"]) if tariff.get("minimum_freight") is not None else None,
                    "mc1_source": "legacy_tariff_rates" if tariff.get("minimum_freight") is not None else None,
                    "floored": None,
                    "base": float(tariff.get("base_tariff") or 0), "with_fsc": float(tariff["access_prog"]),
                })
        else:
            any_approximate = True
            logger.warning(
                "[ZONE GAP] invoice=%s zip3=%s weight=%.2f — no tariff_rates entry at all, "
                "no ALG rate either, zone dropped from access_prog entirely (Phil needs a real rate for this zip3)",
                getattr(matched_rec, "invoice_number", None), zip3, weight,
            )
            if detail is not None:
                detail.append({
                    "dest_id": exact_dest_id, "zip3": zip3, "weight": weight,
                    "rate_source": "none", "rate_used": None, "mc1_used": None,
                    "mc1_source": None, "floored": None, "base": None, "with_fsc": None,
                })

    matched_rec.min_charge_uncertain = any_min_charge_uncertain
    if to_learn and learn:
        reconcile_alg_tariff_rates(to_learn)

    coverage = (rated_weight / total_weight) if total_weight > 0 else 0.0

    def _append_note(text: str) -> None:
        # idempotent -- a second invoice upload for the same trip must not duplicate the note
        if text not in (matched_rec.notes or ""):
            matched_rec.notes = f"{matched_rec.notes} {text}".strip() if matched_rec.notes else text

    if coverage >= _RATE_COVERAGE_THRESHOLD:
        matched_rec.tariff_zone_approximate = any_approximate
        if new_tariff_sum > 0:
            # recomputed fresh each time from our own data, not accumulated per-invoice like amount is
            matched_rec.access_prog = new_tariff_sum
            matched_rec.base_tariff = new_base_sum if new_base_sum > 0 else None
            matched_rec.fsc_pct = _effective_fsc_pct
    elif alg_blended_rate is not None and alg_blended_rate > 0:
        # not enough per-zone coverage -- price our whole weight at the invoice's blended $/cwt instead
        base = Decimal(str(round(alg_blended_rate * total_weight / 100.0, 2)))
        with_fsc = base * (Decimal("1") + _effective_fsc_pct) if _effective_fsc_pct is not None else base
        matched_rec.access_prog = with_fsc
        matched_rec.base_tariff = base
        matched_rec.fsc_pct = _effective_fsc_pct
        matched_rec.tariff_zone_approximate = True
        _append_note(
            f"Calc Cost uses the invoice's blended rate (${alg_blended_rate:.2f}/cwt) — "
            f"per-zone rates covered only {coverage:.0%} of our weight."
        )
        logger.info(
            "[RATE] invoice=%s blended-rate fallback used (coverage %.0f%%, blended $%.2f/cwt)",
            getattr(matched_rec, "invoice_number", None), coverage * 100, alg_blended_rate,
        )
    else:
        # no coverage and no usable blended rate -- an honest null beats a number from a sliver of the load
        matched_rec.access_prog = None
        matched_rec.base_tariff = None
        matched_rec.cost_pct = None
        matched_rec.tariff_zone_approximate = True
        _append_note(
            f"Calc Cost unavailable — rate data covered only {coverage:.0%} of our weight "
            f"({rated_weight:,.0f} of {total_weight:,.0f} lbs)."
        )
        logger.warning(
            "[RATE] invoice=%s access_prog left null (coverage %.0f%%, no blended rate available)",
            getattr(matched_rec, "invoice_number", None), coverage * 100,
        )


def _parse_alg_csv_context(reader: "csv.DictReader") -> dict:
    """walk an ALG invoice CSV once, extracting everything matching + _apply_access_prog_calc() need"""
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
        # Per-zip3, the $ actually billed on a line where ALG's own minimum-freight-charge
        # a floor fired on this zone this invoice; absent means "not observed", not "no minimum applies"
        "alg_min_charge_by_zip3": {},
        # freight-only total (excludes FSC footer); feeds the blended-rate fallback
        "alg_freight_total": 0.0,
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
        # first non-blank row wins -- a later blank Job Name shouldn't clear an already-correct key
        if not ctx["job_name"]:
            ctx["job_name"] = (row.get("Job Name") or "").strip()      # matching key
        if not ctx["alg_bol_no"]:
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
            billed = float(row.get("Billed$") or 0)
            rate_val = float(row.get("Rate") or 0)
            # prefer the printed Rate -- deriving Billed$/GrossWt instead would bake in the minimum-freight charge as a fake rate
            effective_rate = rate_val if rate_val > 0 else (
                round(billed / (gross_wt / 100.0), 4) if raw_zip and gross_wt > 0 and billed > 0 else None
            )
            if raw_zip and effective_rate:
                ctx["alg_rate_by_zip3"].setdefault(raw_zip[:3], effective_rate)
            # a printed Rate that computes less than Billed$ means ALG's minimum freight charge fired
            if raw_zip and rate_val > 0 and gross_wt > 0 and billed > 0:
                expected_charge = round(rate_val * gross_wt / 100.0, 2)
                if abs(expected_charge - billed) > 0.02:
                    ctx["alg_min_charge_by_zip3"].setdefault(raw_zip[:3], billed)
        except (ValueError, TypeError):
            pass
        try:
            ctx["alg_freight_total"] += float(row.get("Billed$") or 0)
        except (ValueError, TypeError):
            pass

    # unlike the per-zone Rate above, the printed FSC rate is rounded -- derive the precise value from the two dollar figures instead
    if ctx["fsc_cost_val"] and ctx["alg_freight_total"]:
        ctx["fsc_rate_val"] = round(ctx["fsc_cost_val"] / ctx["alg_freight_total"], 6)

    return ctx


def _finish_resolving_stub(
    rec: "BOLRecord",
    stub_sender: "str | None",
    stub_sent_at,
    folder: "str | None",
    _get_tariff_rate,
    _diesel_price,
    _fsc_pct,
) -> None:
    """cleanup after resolving a stub outside the main upload flow: copies invoice_email_sender/
    invoice_sent_at from the stub, then re-parses the invoice CSV to compute access_prog/cost_pct.
    no-ops silently (leaves cost fields null) if the folder or file can't be found"""
    if stub_sender:
        rec.invoice_email_sender = stub_sender
    if stub_sent_at:
        rec.invoice_sent_at = stub_sent_at

    if not folder or not os.path.isdir(folder) or not rec.invoice_number:
        return
    hit = _find_invoice_file(folder, rec.invoice_number, require_csv=True)
    if hit is None:
        return
    path, _media_type = hit
    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError:
        return

    reader = csv.DictReader(io.StringIO(content.decode("utf-8", errors="replace")))
    ctx = _parse_alg_csv_context(reader)
    # derive Wolf/311-ness structurally (no manifest, real bol_number) -- match_strategy can go stale
    effective_prophecy_bol = str(rec.bol_number) if not rec.manifest and rec.bol_number else None
    _blended = (
        round(ctx["alg_freight_total"] / (ctx["total_weight"] / 100.0), 4)
        if ctx.get("alg_freight_total") and ctx.get("total_weight") else None
    )
    _cost_detail: list = []
    _apply_access_prog_calc(
        rec, rec.match_strategy, effective_prophecy_bol,
        ctx["alg_rate_by_zip3"], ctx["fsc_rate_val"], ctx["fsc_cost_val"],
        _get_tariff_rate, _diesel_price, _fsc_pct,
        alg_blended_rate=_blended,
        alg_min_charge_by_zip3=ctx.get("alg_min_charge_by_zip3"),
        detail=_cost_detail,
    )
    if _cost_detail:
        rec.cost_calc_detail = json.dumps(_cost_detail)
    if rec.access_prog is not None and rec.amount:
        rec.cost_pct = Decimal(str(round(float(rec.amount) / float(rec.access_prog), 6)))


# above this combined relative-difference score, note the record for manual verification
# but still commit to the closest candidate
_CLOSE_MATCH_THRESHOLD = 0.15


def _cget(c, field):
    """read a field off a candidate that may be a BOLRecord (live) or a dict (mock)"""
    return c.get(field) if isinstance(c, dict) else getattr(c, field, None)


def _score_technique_candidates(candidates: list, total_weight, total_pallets, total_pcs):
    """score each candidate by combined relative diff vs invoice qty, sorted best-first.
    missing quantity data scores as a full mismatch rather than being skipped"""
    def _get(c, field):
        return c.get(field) if isinstance(c, dict) else getattr(c, field, None)

    def _rel_diff(actual, expected):
        if actual is None or not expected:
            return 1.0
        return abs(float(actual) - float(expected)) / float(expected)

    def _score(c):
        return (
            _rel_diff(_get(c, "technique_weight"), total_weight)
            + _rel_diff(_get(c, "technique_pallets"), total_pallets)
            + _rel_diff(_get(c, "technique_pcs"), total_pcs)
        )

    return sorted(((c, _score(c)) for c in candidates), key=lambda pair: pair[1])


def _closest_technique_match(candidates: list, total_weight, total_pallets, total_pcs):
    """returns (best_candidate, best_score); see _score_technique_candidates for the full ranked list"""
    return _score_technique_candidates(candidates, total_weight, total_pallets, total_pcs)[0]


def _partition_candidates_by_resolution(candidates: list):
    """excludes is_third_party candidates unless that empties the pool. returns (usable, resolved),
    where resolved is the subset already carrying a real bol_number -- a stronger signal than scoring"""
    non_tp = [c for c in candidates if not _cget(c, "is_third_party")]
    usable = non_tp if non_tp else candidates
    resolved = [c for c in usable if _cget(c, "bol_number")]
    return usable, resolved


def _flag_if_resolved_match_looks_wrong(
    matched_rec, total_weight, total_pallets, total_pcs, invoice_no: str, job_name: str, suffix_kind: str,
) -> None:
    """diagnostic-only: logs + notes when an already-resolved candidate's quantities don't
    actually fit this invoice, without ever overriding Katie's own resolution"""
    _, score = _closest_technique_match([matched_rec], total_weight, total_pallets, total_pcs)
    if score > _CLOSE_MATCH_THRESHOLD:
        note = (
            f"Invoice {invoice_no} attached to already-resolved manifest "
            f"{_cget(matched_rec, 'manifest')} (BOL {_cget(matched_rec, 'bol_number')}) via "
            f"{suffix_kind}-suffix '{job_name}', but its own Technique quantities differ "
            f"sharply from this invoice (discrepancy score {score:.2f}) — verify manually."
        )
        logger.warning(
            "[INVOICE] %s -> resolved match on %s suffix '%s' has large discrepancy "
            "(score=%.3f) despite skipping quantity scoring - flagging for review",
            invoice_no, suffix_kind, job_name, score,
        )
        if settings.USE_MOCK_DATA:
            existing = matched_rec.get("notes")
            matched_rec["notes"] = f"{existing} {note}" if existing else note
        else:
            matched_rec.notes = f"{matched_rec.notes} {note}" if matched_rec.notes else note


# tight deadline only under lambda (avoids an ungraceful 29s kill); local dev has no such ceiling
# 27 reverted back to 25 (2026-07-30, same day): pushing to 27 (+query_timeout=24) regressed to a
# raw Lambda hard-timeout crash (Status: timeout at 29000ms) on the very next test -- the query's
# real execution time isn't fixed, it varies run to run, and 27s left only ~2s of margin for
# terminate()/join() cleanup overhead before hitting Lambda's own 29s ceiling. A graceful
# {"timed_out": true} response (reliable at 25s) is a much better outcome than an occasional raw
# 500 (the risk introduced by cutting the margin this thin) -- don't push this deadline higher
# again without also solving the underlying query performance, see query_timeout's docstring below.
_RUNNING_ON_LAMBDA = bool(os.environ.get("AWS_SECRET_NAME"))
_WIDE_FALLBACK_DEADLINE = 25 if _RUNNING_ON_LAMBDA else 300  # seconds


def _wide_fallback_technique_search(
    job_name: str, alg_weight: "float | None", alg_pallets: "int | None", alg_pcs: "int | None",
    days_back: int = 90, query_timeout: "int | None" = 22,
) -> "tuple[dict | None, list[dict], bool]":
    """live technique search across `days_back` days for a trip/manifest suffix matching job_name.
    query_timeout raised, then partially reverted, same day (2026-07-30): 15 -> 22 -> 24 -> 22.
    Confirmed live that a 90-day scan routinely needs more than 15s (multiple different job
    suffixes all hit a clean HYT00 query-timeout-expired at exactly 15s, never once completing) --
    genuinely slow right now, not just occasionally unlucky. Pushing further to 24s (with the
    outer _WIDE_FALLBACK_DEADLINE also raised 25 -> 27) regressed on the very next live test to a
    raw Lambda hard-timeout crash instead of a graceful response -- the query's real execution
    time varies run to run, and 27s left too little margin under Lambda's 29s ceiling for
    terminate()/join() cleanup. Settled back on 22s/25s: reliable graceful degradation beats
    squeezing a few more seconds of query time at the cost of occasional raw crashes. If 22s isn't
    enough (it may still time out for the genuinely slowest queries), the fix is no longer "raise
    the timeout further" -- see the 2026-07-30 entry in project memory/CLAUDE.md for next steps
    (query optimization, most likely needing Marge's input on `_TECHNIQUE_QUERY`, since 90 days' worth of unfiltered manifests is a
    lot to pull and filter client-side for a single suffix lookup).
    returns (best, all_candidates, timed_out) -- timed_out distinguishes "ran to completion,
    found nothing" from "didn't finish", since a fixed deadline can clip a call that would've matched.
    all_candidates lets the caller persist ambiguous-trip siblings too, not just the winner."""
    from backend.data_layer import get_technique_data, get_manifest_weights

    start = time.monotonic()
    try:
        raw_manifests = _call_with_timeout(
            get_technique_data, _WIDE_FALLBACK_DEADLINE - (time.monotonic() - start),
            days_back=days_back, query_timeout=query_timeout,
        )
        wide_manifests = _dedupe_technique_rows(raw_manifests)

        # same trip-manifest count is_ambiguous_trip relies on everywhere else
        trip_manifest_counts: dict[str, int] = {}
        for m in wide_manifests:
            if m.get("technique_trip"):
                trip_manifest_counts[m["technique_trip"]] = trip_manifest_counts.get(m["technique_trip"], 0) + 1

        by_trip_suffix: dict[str, list[dict]] = {}
        by_manifest_suffix: dict[str, list[dict]] = {}
        for m in wide_manifests:
            if m.get("technique_trip"):
                by_trip_suffix.setdefault(_trip_to_suffix(m["technique_trip"]), []).append(m)
            if m.get("manifest"):
                by_manifest_suffix.setdefault(_manifest_to_suffix(m["manifest"]), []).append(m)

        candidates = by_trip_suffix.get(job_name) or by_manifest_suffix.get(job_name) or []
        if not candidates:
            return None, [], False
        if len(candidates) == 1:
            candidates[0]["_trip_manifest_count"] = trip_manifest_counts.get(candidates[0].get("technique_trip"), 0)
            return candidates[0], candidates, False

        # multiple manifests share this suffix -- score by closeness to the invoice's billed quantities
        # only if there's time left in the shared deadline; two live calls back-to-back can exceed lambda's ceiling
        remaining = _WIDE_FALLBACK_DEADLINE - (time.monotonic() - start)
        if remaining < 3:
            logger.warning(
                "[INVOICE WIDE FALLBACK] only %.1fs left in the deadline after the first "
                "live query -- skipping the scoring round-trip for suffix '%s' and picking "
                "a candidate without it",
                remaining, job_name,
            )
            best = next((c for c in candidates if c.get("bol_number")), candidates[0])
        else:
            score_weights = _call_with_timeout(
                get_manifest_weights, remaining,
                [c["manifest"] for c in candidates], query_timeout=query_timeout,
            )
            for c in candidates:
                wd = score_weights.get(c["manifest"], {})
                c["technique_weight"]  = wd.get("technique_weight", 0)
                c["technique_pallets"] = wd.get("technique_pallets", 0)
                c["technique_pcs"]     = wd.get("technique_pcs", 0)
            best, score = _closest_technique_match(candidates, float(alg_weight or 0), alg_pallets or 0, alg_pcs or 0)
            if score > _CLOSE_MATCH_THRESHOLD:
                logger.warning(
                    "[INVOICE WIDE FALLBACK] closest match among %d candidates on suffix '%s' "
                    "still has a large discrepancy (score=%.3f) - verify manually",
                    len(candidates), job_name, score,
                )
        trip_count = trip_manifest_counts.get(best.get("technique_trip"), 0)
        for c in candidates:
            c["_trip_manifest_count"] = trip_count
        return best, candidates, False
    except Exception as exc:
        # timed_out=True distinguishes "search didn't complete" from "completed, found nothing"
        logger.warning(
            "[INVOICE WIDE FALLBACK] live Technique search failed for suffix '%s' "
            "(days_back=%d): %s — treating as timed out, not a confirmed non-match.",
            job_name, days_back, exc,
        )
        return None, [], True


def _apply_invoice_match(
    matched_rec,
    match_strategy: str,
    effective_prophecy_bol: "Optional[str]",
    invoice_no: str,
    job_name: "Optional[str]",
    total_billed: "Optional[float]",
    total_weight: "Optional[float]",
    total_pallets: "Optional[int]",
    total_pcs: "Optional[int]",
    alg_rate_by_zip3: dict,
    fsc_rate_val: "Optional[float]",
    fsc_cost_val: "Optional[float]",
    invoice_email_sender: "Optional[str]",
    invoice_sent_at: "Optional[datetime]",
    _get_tariff_rate,
    _diesel_price,
    _fsc_pct,
    db: Session,
    alg_blended_rate: "Optional[float]" = None,
    alg_min_charge_by_zip3: "Optional[dict]" = None,
) -> dict:
    """apply one parsed invoice to one matched record: conflict detection, invoice-number
    merge, additive amount, diff computation. returns {matched_trip, matched_manifest, conflict}"""
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
                # additional invoice for the same trip: add amount only, quantities are per-trip totals
                matched_rec["amount"] = Decimal(str(round(
                    float(matched_rec.get("amount") or 0) + float(amount_dec), 2
                )))
            else:
                matched_rec["amount"] = amount_dec
                matched_rec["alg_weight"] = alg_weight_dec
                matched_rec["alg_pallets"] = total_pallets or None
                matched_rec["alg_pcs"] = total_pcs or None
            # only classify on a genuinely new match -- a duplicate re-upload shouldn't
            # erase a real prior classification like "prophecy_bol"
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
                # additional invoice for the same trip: add amount only, quantities are per-trip totals
                matched_rec.amount = Decimal(str(round(
                    float(matched_rec.amount or 0) + float(amount_dec), 2
                )))
            else:
                matched_rec.amount = amount_dec
                matched_rec.alg_weight = alg_weight_dec
                matched_rec.alg_pallets = total_pallets or None
                matched_rec.alg_pcs = total_pcs or None
            # only classify on a genuinely new match -- a duplicate re-upload shouldn't
            # erase a real prior classification like "prophecy_bol"
            matched_rec.match_strategy = match_strategy
        matched_rec.inv_job_number = job_name
        if invoice_email_sender:
            matched_rec.invoice_email_sender = invoice_email_sender
        if invoice_sent_at:
            matched_rec.invoice_sent_at = invoice_sent_at
        if not already_done:
            _cost_detail: list = []
            _apply_access_prog_calc(
                matched_rec, match_strategy, effective_prophecy_bol,
                alg_rate_by_zip3, fsc_rate_val, fsc_cost_val,
                _get_tariff_rate, _diesel_price, _fsc_pct,
                alg_blended_rate=alg_blended_rate,
                alg_min_charge_by_zip3=alg_min_charge_by_zip3,
                detail=_cost_detail,
            )
            if _cost_detail:
                matched_rec.cost_calc_detail = json.dumps(_cost_detail)
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

    return {"matched_trip": matched_trip, "matched_manifest": matched_manifest, "conflict": conflict_info}


def _process_invoice_csv(
    content: bytes,
    filename: str,
    db: Session,
    invoice_email_sender: "str | None" = None,
    invoice_sent_at: "datetime | None" = None,
) -> dict:
    """parse an ALG invoice CSV and match it to a BOLRecord; matching key is Job Name = the
    technique trip suffix (e.g. "110633" -> TEC_T_0110633), never BOL No (ALG's own internal ref).
    shared by the upload endpoint and the email-poll endpoint"""
    text_content = content.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text_content))
    ctx = _parse_alg_csv_context(reader)
    invoice_no: Optional[str]       = ctx["invoice_no"]
    job_name: Optional[str]         = ctx["job_name"]        # the real matching key
    alg_bol_no: Optional[str]       = ctx["alg_bol_no"]      # ALG's internal ref, stored for info only
    total_pcs                       = ctx["total_pcs"]
    total_weight                    = ctx["total_weight"]
    total_pallets                   = ctx["total_pallets"]
    fsc_rate_val: Optional[float]   = ctx["fsc_rate_val"]
    fsc_cost_val: Optional[float]   = ctx["fsc_cost_val"]
    total_billed: Optional[float]   = ctx["total_billed"]
    cust_job_no: Optional[str]      = ctx["cust_job_no"]
    alg_rate_by_zip3: dict[str, float] = ctx["alg_rate_by_zip3"]      # primary rate source; our own card is a fallback
    alg_min_charge_by_zip3: dict[str, float] = ctx["alg_min_charge_by_zip3"]  # feeds the alg_tariff_rates reconciliation
    # whole-invoice blended $/cwt, fallback when per-zone coverage is incomplete
    alg_blended_rate: Optional[float] = (
        round(ctx["alg_freight_total"] / (total_weight / 100.0), 4)
        if ctx.get("alg_freight_total") and total_weight else None
    )

    if not settings.USE_MOCK_DATA:
        from backend.data_layer import get_tariff_rate as _get_tariff_rate
        from backend.data_layer import get_current_diesel_price, get_fsc_rate as _get_fsc_rate
        if fsc_rate_val is not None:
            # the invoice's own FSC rate always wins -- skip the EIA fallback lookup entirely
            _diesel_price = None
            _fsc_pct = None
        else:
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
        """prophecy BOLs are 6 digits starting with '14' -- check the prefix, not just the magnitude"""
        try:
            return str(int(bol_no)).startswith("14") and len(str(int(bol_no))) == 6
        except (ValueError, TypeError):
            return False

    matched_rec = None
    match_strategy: Optional[str] = None
    effective_prophecy_bol: Optional[str] = None

    # exact matches always come before the "Job Name looks like a Prophecy BOL" guess (see _is_prophecy_bol)

    # 1. already uploaded: match by z-number
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

    # 2. job name as trip suffix -- one trip can have several manifests, resolve to the
    # closest one by comparing quantities against what the invoice actually billed
    loose_match_note: Optional[str] = None
    trip_sum_ctx: Optional[dict] = None
    if matched_rec is None and job_name:
        if settings.USE_MOCK_DATA:
            candidates = [
                rec for rec in _mock_state.values()
                if (rec.get("technique_trip") or "") and _trip_to_suffix(rec["technique_trip"]) == job_name
            ]
        else:
            candidates = [
                row_obj for row_obj in db.query(BOLRecord).filter(BOLRecord.technique_trip.isnot(None)).all()
                if _trip_to_suffix(row_obj.technique_trip or "") == job_name
            ]
        if len(candidates) == 1:
            matched_rec = candidates[0]
            match_strategy = "job_name"
        elif len(candidates) > 1:
            usable, resolved = _partition_candidates_by_resolution(candidates)

            if len(resolved) == 1:
                # Katie already resolved this by creating a real Prophecy BOL -- trust that over quantity-closeness
                matched_rec = resolved[0]
                match_strategy = "job_name"
                logger.info(
                    "[INVOICE] %s -> preferred already-resolved manifest %s over %d other "
                    "trip-suffix candidates on suffix '%s' (BOL %s already exists) — "
                    "skipped quantity-closeness scoring",
                    invoice_no, _cget(matched_rec, "manifest"), len(usable) - 1, job_name,
                    _cget(matched_rec, "bol_number"),
                )
                _flag_if_resolved_match_looks_wrong(
                    matched_rec, total_weight, total_pallets, total_pcs, invoice_no, job_name, "trip",
                )
            else:
                # some invoices bill the whole trip, not one manifest -- score the trip-sum as one more
                # candidate; if several are already resolved, score only among those
                scoring_pool = resolved if resolved else usable
                combined = {
                    "technique_weight": sum(float(_cget(c, "technique_weight") or 0) for c in usable),
                    "technique_pallets": sum(int(_cget(c, "technique_pallets") or 0) for c in usable),
                    "technique_pcs": sum(int(_cget(c, "technique_pcs") or 0) for c in usable),
                    "_is_trip_sum": True,
                }
                best, score = _closest_technique_match(scoring_pool + [combined], total_weight, total_pallets, total_pcs)
                match_strategy = "job_name"
                if isinstance(best, dict) and best.get("_is_trip_sum"):
                    # trip-level invoice: attach to the primary manifest (has a BOL, else heaviest)
                    primary = next((c for c in usable if _cget(c, "bol_number")), None)
                    if primary is None:
                        primary = max(usable, key=lambda c: float(_cget(c, "technique_weight") or 0))
                    matched_rec = primary
                    manifest_names = ", ".join(str(_cget(c, "manifest")) for c in usable)
                    trip_sum_ctx = {
                        "weight": combined["technique_weight"],
                        "pallets": combined["technique_pallets"],
                        "pcs": combined["technique_pcs"],
                        "siblings": [c for c in usable if c is not primary],
                        "manifest_names": manifest_names,
                    }
                    logger.info(
                        "[INVOICE] %s -> trip-level match on suffix '%s': invoice totals fit the "
                        "combined %d manifests (%s) better than any single one (score=%.3f)",
                        invoice_no, job_name, len(usable), manifest_names, score,
                    )
                else:
                    matched_rec = best
                if score > _CLOSE_MATCH_THRESHOLD:
                    loose_match_note = (
                        f"Matched via closest-quantity heuristic among {len(scoring_pool)} "
                        f"manifests on this trip (discrepancy score {score:.2f}) — verify manually."
                    )
                    logger.warning(
                        "[INVOICE] %s -> closest match among %d candidates on trip suffix '%s' "
                        "still has a large discrepancy (score=%.3f) - verify manually",
                        invoice_no, len(scoring_pool), job_name, score,
                    )
                else:
                    logger.info(
                        "[INVOICE] %s -> matched to closest of %d candidates on trip suffix '%s' (score=%.3f)",
                        invoice_no, len(scoring_pool), job_name, score,
                    )
                if loose_match_note:
                    if settings.USE_MOCK_DATA:
                        existing_notes = matched_rec.get("notes")
                        matched_rec["notes"] = f"{existing_notes} {loose_match_note}" if existing_notes else loose_match_note
                    else:
                        matched_rec.notes = f"{matched_rec.notes} {loose_match_note}" if matched_rec.notes else loose_match_note
        else:
            # 2b. no trip-suffix match -- try the job name as a manifest suffix instead (a trip
            # and its manifest are different numbers). no trip-sum candidate here, unrelated manifests don't sum
            if settings.USE_MOCK_DATA:
                manifest_candidates = [
                    rec for rec in _mock_state.values()
                    if (rec.get("manifest") or "") and _manifest_to_suffix(rec["manifest"]) == job_name
                ]
            else:
                manifest_candidates = [
                    row_obj for row_obj in db.query(BOLRecord).filter(BOLRecord.manifest.isnot(None)).all()
                    if _manifest_to_suffix(row_obj.manifest or "") == job_name
                ]
            if len(manifest_candidates) == 1:
                matched_rec = manifest_candidates[0]
                match_strategy = "job_name"
                logger.info(
                    "[INVOICE] %s -> matched via manifest suffix '%s' (no trip suffix match found)",
                    invoice_no, job_name,
                )
            elif len(manifest_candidates) > 1:
                usable, resolved = _partition_candidates_by_resolution(manifest_candidates)

                if len(resolved) == 1:
                    matched_rec = resolved[0]
                    match_strategy = "job_name"
                    logger.info(
                        "[INVOICE] %s -> preferred already-resolved manifest %s over %d other "
                        "manifest-suffix candidates '%s' (BOL %s already exists) — skipped "
                        "quantity-closeness scoring",
                        invoice_no, _cget(matched_rec, "manifest"), len(usable) - 1, job_name,
                        _cget(matched_rec, "bol_number"),
                    )
                    _flag_if_resolved_match_looks_wrong(
                        matched_rec, total_weight, total_pallets, total_pcs, invoice_no, job_name, "manifest",
                    )
                else:
                    scoring_pool = resolved if resolved else usable
                    best, score = _closest_technique_match(scoring_pool, total_weight, total_pallets, total_pcs)
                    matched_rec = best
                    match_strategy = "job_name"
                    if score > _CLOSE_MATCH_THRESHOLD:
                        loose_match_note = (
                            f"Matched via closest-quantity heuristic among {len(scoring_pool)} "
                            f"manifests sharing suffix '{job_name}' (discrepancy score {score:.2f}) — verify manually."
                        )
                        logger.warning(
                            "[INVOICE] %s -> closest match among %d manifest-suffix candidates '%s' "
                            "still has a large discrepancy (score=%.3f) - verify manually",
                            invoice_no, len(scoring_pool), job_name, score,
                        )
                    else:
                        logger.info(
                            "[INVOICE] %s -> matched to closest of %d manifest-suffix candidates '%s' (score=%.3f)",
                            invoice_no, len(scoring_pool), job_name, score,
                        )
                if loose_match_note:
                    if settings.USE_MOCK_DATA:
                        existing_notes = matched_rec.get("notes")
                        matched_rec["notes"] = f"{existing_notes} {loose_match_note}" if existing_notes else loose_match_note
                    else:
                        matched_rec.notes = f"{matched_rec.notes} {loose_match_note}" if matched_rec.notes else loose_match_note

    # 3. job name as a Prophecy BOL (Wolf/311, no technique trip); only after 1-2 rule out a real trip
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

    # a live wide-fallback search used to run inline here -- moved out to retry-match, called
    # automatically by the frontend right after upload, so it gets its own request budget

    if matched_rec is None:
        is_wolf_stub = bool(effective_prophecy_bol)
        stub_bol_number = int(effective_prophecy_bol) if is_wolf_stub else None
        stub_match_strategy = "prophecy_bol" if is_wolf_stub else "invoice_only"
        if is_wolf_stub:
            auto_note = f"Wolf/311 load — Prophecy BOL {effective_prophecy_bol}. New record created from this invoice."
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
                "technique_weight": 0,
                "technique_pallets": 0,
                "technique_pcs": 0,
                "weight_diff": None,
                "pallet_diff": None,
                "pcs_diff": None,
                "prophecy_weight": None,
                "prophecy_pallets": None,
                "prophecy_pcs": None,
                "invoice_email_sender": invoice_email_sender,
                "invoice_sent_at": invoice_sent_at,
                "notes": None,
                "status": "pending",
                "flag_reason": None,
                "match_strategy": stub_match_strategy,
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
                technique_weight      = 0,
                technique_pallets     = 0,
                technique_pcs         = 0,
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
                status                = BOLStatus.PENDING,
                match_strategy        = stub_match_strategy,
                needs_sid_export      = False,
            )
            db.add(stub)
            db.commit()
            # Wolf/311 stubs already have everything needed to compute Calculated Cost -- do it now
            if is_wolf_stub and stub_bol_number:
                from backend.data_layer import get_prophecy_data as _get_prophecy_data
                prop = _get_prophecy_data(stub_bol_number)
                if prop:
                    stub.prophecy_weight  = prop["prophecy_weight"]
                    stub.prophecy_pallets = prop["prophecy_pallets"]
                    stub.prophecy_pcs     = prop["prophecy_pcs"]
                    _compute_diffs(stub)
                if _get_tariff_rate is not None:
                    _cost_detail: list = []
                    _apply_access_prog_calc(
                        stub, "prophecy_bol", effective_prophecy_bol,
                        alg_rate_by_zip3, fsc_rate_val, fsc_cost_val,
                        _get_tariff_rate, _diesel_price, _fsc_pct,
                        alg_blended_rate=alg_blended_rate,
                        alg_min_charge_by_zip3=alg_min_charge_by_zip3,
                        detail=_cost_detail,
                    )
                    if _cost_detail:
                        stub.cost_calc_detail = json.dumps(_cost_detail)
                    if stub.amount and stub.access_prog:
                        stub.cost_pct = Decimal(str(round(float(stub.amount) / float(stub.access_prog), 6)))
                db.commit()
        logger.info(
            "[INVOICE] %s → no match, stub created (bol=%s, note=%s)",
            invoice_no, stub_bol_number, auto_note,
        )
        stub_record = _mock_state[stub_id] if settings.USE_MOCK_DATA else stub
        return {
            "matched": is_wolf_stub,
            "record_id": str(_cget(stub_record, "id")),
            "invoice_number": invoice_no,
            "job_name": job_name,
            "alg_bol_no": alg_bol_no,
            "matched_trip": None,
            "manifest": None,
            "match_strategy": stub_match_strategy,
            "alg_pcs": total_pcs,
            "alg_weight": round(total_weight, 2),
            "alg_pallets": total_pallets,
            "amount": total_billed,
            "fsc_pct": fsc_rate_val,
            "fsc_cost": fsc_cost_val,
            "message": (
                f"Invoice {invoice_no} matched Prophecy BOL {effective_prophecy_bol} (Wolf/311 load)."
                if is_wolf_stub
                else f"Invoice {invoice_no} has no match — stub record created. {auto_note}"
            ),
        }

    result = _apply_invoice_match(
        matched_rec, match_strategy, effective_prophecy_bol, invoice_no, job_name,
        total_billed, total_weight, total_pallets, total_pcs,
        alg_rate_by_zip3, fsc_rate_val, fsc_cost_val,
        invoice_email_sender, invoice_sent_at,
        _get_tariff_rate, _diesel_price, _fsc_pct, db,
        alg_blended_rate=alg_blended_rate,
        alg_min_charge_by_zip3=alg_min_charge_by_zip3,
    )
    matched_trip = result["matched_trip"]
    matched_manifest = result["matched_manifest"]
    conflict_info = result["conflict"]

    if trip_sum_ctx is not None:
        # trip-level invoice: recompute diffs against the trip's combined totals, not just the primary manifest
        primary_note = (
            f"Invoice {invoice_no} covers the entire trip "
            f"({len(trip_sum_ctx['siblings']) + 1} manifests: {trip_sum_ctx['manifest_names']}) — "
            f"quantity diffs are vs the trip's combined totals."
        )
        sibling_note = f"Billed under {invoice_no} — trip-level invoice attached to manifest {matched_manifest}."
        if settings.USE_MOCK_DATA:
            if matched_rec.get("alg_weight") is not None and trip_sum_ctx["weight"]:
                matched_rec["weight_diff"] = round(float(matched_rec["alg_weight"]) - trip_sum_ctx["weight"], 2)
            if matched_rec.get("alg_pallets") is not None:
                matched_rec["pallet_diff"] = matched_rec["alg_pallets"] - trip_sum_ctx["pallets"]
            if matched_rec.get("alg_pcs") is not None:
                matched_rec["pcs_diff"] = matched_rec["alg_pcs"] - trip_sum_ctx["pcs"]
            if primary_note not in (matched_rec.get("notes") or ""):
                existing = matched_rec.get("notes")
                matched_rec["notes"] = f"{existing} {primary_note}" if existing else primary_note
            for sib in trip_sum_ctx["siblings"]:
                if sibling_note not in (sib.get("notes") or ""):
                    existing = sib.get("notes")
                    sib["notes"] = f"{existing} {sibling_note}" if existing else sibling_note
        else:
            if matched_rec.alg_weight is not None and trip_sum_ctx["weight"]:
                matched_rec.weight_diff = Decimal(str(round(float(matched_rec.alg_weight) - trip_sum_ctx["weight"], 2)))
            if matched_rec.alg_pallets is not None:
                matched_rec.pallet_diff = matched_rec.alg_pallets - trip_sum_ctx["pallets"]
            if matched_rec.alg_pcs is not None:
                matched_rec.pcs_diff = matched_rec.alg_pcs - trip_sum_ctx["pcs"]
            if primary_note not in (matched_rec.notes or ""):
                matched_rec.notes = f"{matched_rec.notes} {primary_note}" if matched_rec.notes else primary_note
            for sib in trip_sum_ctx["siblings"]:
                if sibling_note not in (sib.notes or ""):
                    sib.notes = f"{sib.notes} {sibling_note}" if sib.notes else sibling_note
            db.commit()

    logger.info(
        "[INVOICE] Uploaded %s → matched trip %s (job_name=%s alg_bol=%s), amount=$%.2f",
        invoice_no, matched_trip, job_name, alg_bol_no, total_billed or 0,
    )
    return {
        "matched": True,
        "record_id": str(_cget(matched_rec, "id")),
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
        "trip_level": trip_sum_ctx is not None,
        "message": (
            f"Invoice {invoice_no} matched trip {matched_trip} as a trip-level invoice "
            f"(covers {len(trip_sum_ctx['siblings']) + 1} manifests)."
            if trip_sum_ctx is not None
            else f"Invoice {invoice_no} matched to trip {matched_trip} and updated."
        ),
    }


@app.post("/api/invoices/upload", tags=["Invoices"])
async def upload_alg_invoice(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    pdf_file: Optional[UploadFile] = File(None),
    invoice_folder_name: Optional[str] = Form(None),
    invoice_sender: Optional[str] = Form(None),
    invoice_date: Optional[str] = Form(None),
    invoice_time: Optional[str] = Form(None),
):
    """upload an ALG invoice CSV (z-number format). invoice_folder_name is the sender's dated
    folder, parsed the same way as poll_invoice_folder. pdf_file is an optional companion PDF,
    stored in S3 keyed by z-number. invoice_sender/date/time are the manual-upload fallback."""
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
            # doesn't match the expected shape -- use the raw folder name as-is rather than leaving sender blank
            sender_str = invoice_folder_name.strip()[:200]
            logger.info("[UPLOAD] Folder name '%s' not parseable — using it as-is for sender", invoice_folder_name)
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

    if pdf_file is not None and result.get("invoice_number"):
        pdf_bytes = await pdf_file.read()
        _store_invoice_pdf_bytes(result["invoice_number"], pdf_bytes)

    return result


@app.post("/api/invoices/fix-sender", tags=["Invoices"])
def fix_invoice_sender(body: dict, db: Session = Depends(get_db)):
    """manual fix for a batch whose subfolder name failed to parse and was stored as-is;
    re-parses the corrected name and updates every row sharing the original raw sender string.
    body: {"raw_sender": "<as currently stored>", "corrected_folder_name": "Tania 7-22-2026 436PM"}"""
    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Not available in mock mode.")

    raw_sender = (body.get("raw_sender") or "").strip()
    corrected = (body.get("corrected_folder_name") or "").strip()
    if not raw_sender:
        raise HTTPException(status_code=400, detail="raw_sender is required")
    if not corrected:
        raise HTTPException(status_code=400, detail="corrected_folder_name is required")

    parsed = _parse_invoice_folder_name(corrected)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail=f'Could not parse "{corrected}" — expected the format '
                   f'"Name M-D-YYYY H-MMAM/PM" (e.g. "Tania 7-22-2026 436PM").',
        )
    display, sent_at = parsed

    rows = db.query(BOLRecord).filter(BOLRecord.invoice_email_sender == raw_sender).all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No records found with sender '{raw_sender}'.")

    for row in rows:
        row.invoice_email_sender = display
        row.invoice_sent_at = sent_at
    db.commit()
    logger.info("[FIX-INVOICE-SENDER] '%s' -> '%s' (%d record(s))", raw_sender, display, len(rows))

    # best-effort: refresh the merged batch-PDF cache under the corrected label
    try:
        _merge_and_store_batch_pdf(display, db)
    except Exception:
        logger.warning("[FIX-INVOICE-SENDER] batch-pdf refresh failed for '%s'", display, exc_info=True)

    return {"updated": len(rows), "invoice_email_sender": display, "invoice_sent_at": sent_at}


@app.post("/api/invoices/merge-batch-pdfs", tags=["Invoices"])
def merge_batch_pdfs(body: dict, db: Session = Depends(get_db)):
    """merge and store the combined invoice PDF for one sender batch; safe to re-call"""
    sender = (body.get("sender") or "").strip()
    if not sender:
        raise HTTPException(status_code=400, detail="sender is required")
    return _merge_and_store_batch_pdf(sender, db)


@app.get("/api/invoices/batch-pdf", tags=["Invoices"])
def get_batch_pdf(sender: str, db: Session = Depends(get_db)):
    """serve the merged batch PDF; fast path reads the precomputed cache, else merges on the fly"""
    sender = sender.strip()
    if not sender:
        raise HTTPException(status_code=400, detail="sender is required")
    slug = _slugify_sender(sender)

    cached = _fetch_batch_pdf_bytes(slug)
    if cached is None:
        result = _merge_and_store_batch_pdf(sender, db)
        if not result["merged"]:
            raise HTTPException(status_code=404, detail=f"No invoice PDFs found for sender '{sender}'")
        cached = _fetch_batch_pdf_bytes(slug)

    return StreamingResponse(
        io.BytesIO(cached),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="SG360 Invoices - {_readable_batch_name(sender)}.pdf"'},
    )


@app.get("/api/invoices/{invoice_number}/file", tags=["Invoices"])
def get_invoice_file(invoice_number: str):
    """serve the original invoice file for a z-number, preferring PDF over CSV.
    checks S3 first (reachable from lambda), then INVOICE_FOLDER/test_data as a fallback"""
    z = invoice_number.strip().upper()

    if not settings.USE_MOCK_DATA and settings.INVOICE_S3_BUCKET:
        from botocore.exceptions import ClientError
        s3 = boto3.client("s3", config=_S3_FAST_FAIL)
        key = f"{z}.pdf"
        try:
            s3.head_object(Bucket=settings.INVOICE_S3_BUCKET, Key=key)
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.INVOICE_S3_BUCKET, "Key": key},
                ExpiresIn=300,
            )
            return RedirectResponse(url)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "404":
                logger.error("[INVOICE FILE] S3 lookup failed for %s: %s", z, exc)

    if settings.USE_MOCK_DATA:
        folder = os.path.join(os.path.dirname(__file__), "test_data")
    else:
        folder = settings.INVOICE_FOLDER

    if not folder:
        raise HTTPException(status_code=404, detail="No invoice folder configured (set INVOICE_FOLDER in .env)")

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
    """scan INVOICE_FOLDER for unprocessed invoice CSVs; files stay in place, "already
    processed" is tracked via existing invoice_number rows. mock mode uses test_data/"""
    if settings.USE_MOCK_DATA:
        folder = os.path.join(os.path.dirname(__file__), "test_data")
    else:
        folder = settings.INVOICE_FOLDER
        # re-read .env directly in case INVOICE_FOLDER was added after this process started
        if not folder:
            from dotenv import dotenv_values
            _env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
            folder = dotenv_values(_env_path).get("INVOICE_FOLDER", "")

    if not folder:
        raise HTTPException(
            status_code=503,
            detail=(
                "Folder-based invoice polling isn't available in this environment "
                "(no network path to the shared drive) — use Upload Invoice CSV instead."
            ),
        )
    if not os.path.isdir(folder):
        raise HTTPException(
            status_code=503,
            detail=f"INVOICE_FOLDER path does not exist: {folder}",
        )

    # skip Z-numbers already imported (ALG CSV filenames are named after their Z-number)
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

    # named subfolders first (sender metadata from the folder name), then flat CSVs in root
    file_queue: list[tuple[str, str, "str | None", "datetime | None"]] = []

    for entry in os.listdir(folder):
        entry_path = os.path.join(folder, entry)
        if os.path.isdir(entry_path):
            parsed = _parse_invoice_folder_name(entry)
            if parsed:
                sender_str, sent_at = parsed
                logger.info("[POLL-FOLDER] Subfolder '%s' → sender='%s'", entry, sender_str)
            else:
                # use the raw subfolder name as-is rather than leaving sender blank
                sender_str, sent_at = entry.strip()[:200], None
                logger.info("[POLL-FOLDER] Subfolder '%s' not parseable — using it as-is for sender", entry)
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

    # best-effort: refresh each affected sender's merged batch PDF cache
    senders_touched = {sender_str for _, _, sender_str, _ in file_queue if sender_str}
    for sender in senders_touched:
        try:
            _merge_and_store_batch_pdf(sender, db)
        except Exception as exc:
            logger.error("[POLL-FOLDER] Batch PDF merge failed for sender '%s': %s", sender, exc)

    return {"found": len(file_queue), "processed": results, "message": msg}


@app.post("/api/admin/fix-duplicate-invoice-matches", tags=["Admin"])
def fix_duplicate_invoice_matches(db: Session = Depends(get_db)):
    """one-time backfill for the old strategy-2 bug: an invoice matching several manifests
    on one trip used to apply to every one of them instead of just the closest. finds
    records sharing an identical invoice_number, keeps the best-scoring one, reverts the rest"""
    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Not available in mock mode.")

    rows = db.query(BOLRecord).filter(BOLRecord.invoice_number.isnot(None)).all()
    groups: dict[str, list] = {}
    for row in rows:
        for inv in [p.strip() for p in row.invoice_number.split(",")]:
            groups.setdefault(inv, []).append(row)

    fixed = []
    for inv, members in groups.items():
        if len(members) < 2:
            continue
        ref = members[0]
        total_weight = float(ref.alg_weight) if ref.alg_weight is not None else None
        total_pallets = ref.alg_pallets
        total_pcs = ref.alg_pcs
        winner, score = _closest_technique_match(members, total_weight, total_pallets, total_pcs)
        losers = [m for m in members if m.id != winner.id]
        reverted = []
        for loser in losers:
            reverted.append({"manifest": loser.manifest, "technique_trip": loser.technique_trip})
            loser.invoice_number = None
            loser.amount         = None
            loser.alg_weight     = None
            loser.alg_pallets    = None
            loser.alg_pcs        = None
            loser.access_prog    = None
            loser.cost_pct       = None
            loser.match_strategy = None
            loser.inv_job_number = None
            loser.weight_diff    = None
            loser.pallet_diff    = None
            loser.pcs_diff       = None
            loser.tariff_zone_approximate = False
            loser.weight_source_fallback  = False
            loser.min_charge_uncertain    = False
            loser.notes = None
            if loser.status != BOLStatus.APPROVED:
                loser.status = BOLStatus.PENDING
                loser.flag_reason = None
        fixed.append({
            "invoice_number": inv,
            "kept": {"manifest": winner.manifest, "technique_trip": winner.technique_trip, "score": round(score, 4)},
            "reverted": reverted,
        })
        logger.info(
            "[FIX-DUP-INVOICE] %s: kept manifest=%s (score=%.3f), reverted %d other match(es)",
            inv, winner.manifest, score, len(reverted),
        )

    db.commit()
    return {"groups_fixed": len(fixed), "details": fixed}


@app.post("/api/admin/recompute-diffs", tags=["Admin"])
def recompute_diffs(db: Session = Depends(get_db)):
    """one-time backfill for records with incorrect/missing diffs; pure DB recompute, no live query"""
    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Not available in mock mode.")
    checked = 0
    for row in db.query(BOLRecord).filter(BOLRecord.alg_weight.isnot(None)).all():
        # a bol_number + no technique_trip + prophecy quantities is structurally a Wolf/311 load
        if row.bol_number and not row.technique_trip and row.prophecy_weight is not None:
            row.match_strategy = "prophecy_bol"
        _compute_diffs(row)
        checked += 1
    db.commit()
    logger.info("[RECOMPUTE-DIFFS] Checked %d record(s) with an invoice matched", checked)
    return {"records_checked": checked}


def _recompute_access_prog_for_record(rec: "BOLRecord", folder: "str | None") -> str:
    """re-locate and re-parse rec's invoice CSV to recompute access_prog/cost_pct/cost_calc_detail --
    ALG's rate/FSC context only exists in the CSV, there's no other way to redo this math.
    returns "ok", "no_file", or "no_own_data". mutates rec in place; caller commits."""
    if not rec.invoice_number or not folder or not os.path.isdir(folder):
        return "no_file"
    hit = _find_invoice_file(folder, rec.invoice_number, require_csv=True)
    if hit is None:
        return "no_file"
    path, _media_type = hit
    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError:
        return "no_file"

    reader = csv.DictReader(io.StringIO(content.decode("utf-8", errors="replace")))
    ctx = _parse_alg_csv_context(reader)

    from backend.data_layer import get_tariff_rate as _get_tariff_rate
    from backend.data_layer import get_current_diesel_price, get_fsc_rate as _get_fsc_rate
    if ctx["fsc_rate_val"] is not None:
        _diesel_price = None
        _fsc_pct = None
    else:
        _diesel_price = get_current_diesel_price()
        _fsc_pct = _get_fsc_rate(_diesel_price) if _diesel_price is not None else None

    # route on manifest/bol_number structurally -- stored match_strategy can go stale
    effective_prophecy_bol = str(rec.bol_number) if not rec.manifest and rec.bol_number else None
    _blended = (
        round(ctx["alg_freight_total"] / (ctx["total_weight"] / 100.0), 4)
        if ctx.get("alg_freight_total") and ctx.get("total_weight") else None
    )
    _cost_detail: list = []
    _apply_access_prog_calc(
        rec, rec.match_strategy, effective_prophecy_bol,
        ctx["alg_rate_by_zip3"], ctx["fsc_rate_val"], ctx["fsc_cost_val"],
        _get_tariff_rate, _diesel_price, _fsc_pct,
        alg_blended_rate=_blended,
        alg_min_charge_by_zip3=ctx.get("alg_min_charge_by_zip3"),
        detail=_cost_detail,
    )
    if rec.access_prog is None:
        return "no_own_data"
    if _cost_detail:
        rec.cost_calc_detail = json.dumps(_cost_detail)
    if rec.amount and rec.access_prog:
        rec.cost_pct = Decimal(str(round(float(rec.amount) / float(rec.access_prog), 6)))
    return "ok"


@app.post("/api/admin/recompute-access-prog", tags=["Admin"])
def recompute_access_prog(db: Session = Depends(get_db)):
    """backfill Calculated Cost for existing matched records; records with no locatable
    file or no own pallet data are left untouched and reported separately, not guessed at"""
    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Not available in mock mode.")

    folder = settings.INVOICE_FOLDER
    if not folder or not os.path.isdir(folder):
        raise HTTPException(status_code=404, detail="INVOICE_FOLDER is not configured or does not exist.")

    fixed = 0
    skipped_no_file = 0
    skipped_no_own_data = 0

    for rec in db.query(BOLRecord).filter(BOLRecord.invoice_number.isnot(None)).all():
        result = _recompute_access_prog_for_record(rec, folder)
        if result == "no_file":
            skipped_no_file += 1
        elif result == "no_own_data":
            skipped_no_own_data += 1
        else:
            fixed += 1

    db.commit()
    logger.info(
        "[RECOMPUTE-ACCESS-PROG] fixed=%d skipped_no_file=%d skipped_no_own_data=%d",
        fixed, skipped_no_file, skipped_no_own_data,
    )
    return {"fixed": fixed, "skipped_no_file": skipped_no_file, "skipped_no_own_data": skipped_no_own_data}


@app.post("/api/admin/recompute-invoice-senders", tags=["Admin"])
def recompute_invoice_senders(db: Session = Depends(get_db)):
    """safe-to-rerun backfill: re-parses invoice_email_sender for rows still holding a raw,
    unparsed folder name, wherever today's parser now succeeds. idempotent -- only rows
    with invoice_sent_at IS NULL are touched, and a fixed row always gets it set"""
    if settings.USE_MOCK_DATA:
        raise HTTPException(status_code=400, detail="Not available in mock mode.")

    rows = (
        db.query(BOLRecord)
        .filter(BOLRecord.invoice_email_sender.isnot(None))
        .filter(BOLRecord.invoice_sent_at.is_(None))
        .all()
    )
    fixed = 0
    unparseable: set[str] = set()
    for row in rows:
        parsed = _parse_invoice_folder_name(row.invoice_email_sender)
        if parsed is None:
            unparseable.add(row.invoice_email_sender)
            continue
        row.invoice_email_sender, row.invoice_sent_at = parsed
        fixed += 1

    db.commit()
    logger.info(
        "[RECOMPUTE-INVOICE-SENDERS] checked=%d fixed=%d unparseable=%d",
        len(rows), fixed, len(unparseable),
    )
    return {
        "records_checked": len(rows),
        "records_fixed": fixed,
        "still_unparseable_senders": sorted(unparseable),
    }


def _rate_table_counts(db: Session) -> dict:
    return {
        "tariff_rates": db.query(TariffRate).count(),
        "alg_tariff_rates": db.query(AlgTariffRate).count(),
        "fuel_surcharge_rates": db.query(FuelSurchargeRate).count(),
    }


@app.get("/api/admin/rate-table-counts", tags=["Admin"])
def rate_table_counts(db: Session = Depends(get_db)):
    """read-only row counts for the three static rate-card tables; makes a seeding
    gap visible at a glance instead of silently missing every zone lookup"""
    return _rate_table_counts(db)


# fixed S3 keys seed_rate_tables() reads; upload the same source files here first
_RATE_SEED_S3_KEYS = {
    "tariff_rates": "rate-seed/tariff_rates.csv",
    "fuel_surcharge_rates": "rate-seed/fsc_matrix.xlsx",
    "alg_tariff_rates": "rate-seed/alg_tariff_rates.csv",
}


@app.post("/api/admin/seed-rate-tables", tags=["Admin"])
def seed_rate_tables(db: Session = Depends(get_db)):
    """(re-)seed the three rate tables from S3 -- for lambda, which can't reach the local
    disk paths seed_rates.py's CLI normally reads from and has no other way to run it against
    VPC-private Aurora. requires the source files already uploaded under _RATE_SEED_S3_KEYS. safe to re-run"""
    if not settings.INVOICE_S3_BUCKET:
        raise HTTPException(status_code=400, detail="INVOICE_S3_BUCKET is not configured.")

    from backend.seed_rates import load_tariff_rates, load_fsc_rates, load_alg_tariff_rates

    before = _rate_table_counts(db)

    s3 = boto3.client("s3", config=_S3_FAST_FAIL)
    tmp_dir = Path(tempfile.gettempdir()) / "sg360_rate_seed"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    local_paths = {}
    fetch_errors = {}
    for table, key in _RATE_SEED_S3_KEYS.items():
        dest = tmp_dir / Path(key).name
        try:
            s3.download_file(settings.INVOICE_S3_BUCKET, key, str(dest))
            local_paths[table] = dest
        except Exception as exc:
            fetch_errors[table] = str(exc)

    if fetch_errors:
        raise HTTPException(
            status_code=404,
            detail=f"Could not fetch source file(s) from s3://{settings.INVOICE_S3_BUCKET}/rate-seed/: "
                   f"{fetch_errors}. Upload them first -- see backend/seed_rates.py for the expected "
                   f"file formats.",
        )

    inserted = {}
    load_errors = {}
    try:
        inserted["tariff_rates"] = load_tariff_rates(local_paths["tariff_rates"], db)
    except Exception as exc:
        db.rollback()
        load_errors["tariff_rates"] = str(exc)
    try:
        inserted["fuel_surcharge_rates"] = load_fsc_rates(local_paths["fuel_surcharge_rates"], db)
    except Exception as exc:
        db.rollback()
        load_errors["fuel_surcharge_rates"] = str(exc)
    try:
        inserted["alg_tariff_rates"] = load_alg_tariff_rates(local_paths["alg_tariff_rates"], db)
    except Exception as exc:
        db.rollback()
        load_errors["alg_tariff_rates"] = str(exc)

    after = _rate_table_counts(db)
    logger.info(
        "[SEED-RATE-TABLES] before=%s inserted=%s errors=%s after=%s",
        before, inserted, load_errors, after,
    )
    return {"before": before, "inserted": inserted, "errors": load_errors, "after": after}


@app.get("/api/bols/{record_id}/cost-breakdown", tags=["Admin"])
def get_cost_breakdown(record_id: uuid.UUID, db: Session = Depends(get_db)):
    """read-only per-pallet Calculated Cost breakdown, reading the cost_calc_detail JSON
    stored on the record at calc time -- no live query or file access needed"""
    rec = db.query(BOLRecord).filter(BOLRecord.id == record_id).first()
    if rec is None:
        raise HTTPException(status_code=404, detail="Record not found.")
    if not rec.invoice_number:
        raise HTTPException(status_code=422, detail="This record has no invoice to break down.")
    if not rec.cost_calc_detail:
        raise HTTPException(
            status_code=404,
            detail="This record hasn't been recomputed since cost-breakdown storage was "
                   "added — run recompute-access-prog to backfill it.",
        )
    try:
        detail = json.loads(rec.cost_calc_detail)
    except (ValueError, TypeError):
        raise HTTPException(status_code=500, detail="Stored cost-breakdown detail is corrupted.")

    return {
        "record_id": str(rec.id),
        "invoice_number": rec.invoice_number,
        "match_strategy": rec.match_strategy,
        "access_prog": float(rec.access_prog) if rec.access_prog is not None else None,
        "amount": float(rec.amount) if rec.amount is not None else None,
        "cost_pct": float(rec.cost_pct) if rec.cost_pct is not None else None,
        "min_charge_uncertain": rec.min_charge_uncertain,
        "tariff_zone_approximate": rec.tariff_zone_approximate,
        "weight_source_fallback": rec.weight_source_fallback,
        "pallets": detail,
    }


@app.post("/api/admin/poll-email", tags=["Admin"])
def poll_alg_email(db: Session = Depends(get_db)):
    """poll the O365 inbox for unread ALG invoice emails, extract CSVs, and process each
    through the same pipeline as manual upload. requires SMTP_USER/SMTP_PASSWORD in .env"""
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
    """generate a Prophecy SID import CSV for today's approved manifests, one row per pallet;
    Katie imports this into Prophecy to create load numbers"""
    filename = get_sid_filename()

    if settings.USE_MOCK_DATA:
        approved = [
            r for r in _mock_state.values()
            if r["status"] == "approved"
            and r.get("needs_sid_export", True)
            and not r.get("is_third_party", False)
            and not r.get("is_do_not_pay", False)
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
            BOLRecord.is_do_not_pay == False,
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
    """per-record SID export -- pushes one urgent Type A record to Prophecy without
    waiting for a full batch approval"""
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
    """refresh one record's manifest-side data without a full pull: re-check weight/pallets/pieces,
    and check whether Prophecy now has a BOL. does not touch invoice-side fields -- those only
    recompute via invoice upload. weight source switches to get_manifest_weights_from_sid() once
    a bol_number exists, matching what Katie's own Prophecy import already used"""
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

    from backend.data_layer import get_technique_data, get_manifest_weights, get_manifest_weights_from_sid

    messages = []
    updated = False

    # (1) always refresh weight/pallets/pieces; prefer the SID-export query once a BOL exists
    if rec.bol_number:
        weight_data = get_manifest_weights_from_sid([rec.manifest]).get(rec.manifest)
    else:
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
        else:
            messages.append("Weight/pallets/pieces unchanged.")
    else:
        messages.append(_NO_ACTIVE_PALLET_DATA_NOTE)

    # (2) check BOL status -- only meaningful if the record doesn't have one yet
    if rec.needs_sid_export:
        manifests = _dedupe_technique_rows(get_technique_data(days_back=21))
        match = next((m for m in manifests if m.get("manifest") == rec.manifest), None)
        if match is None:
            messages.append("Manifest not found in Technique for BOL check — try again later.")
        else:
            before = rec.bol_number
            _apply_bol_status(rec, match)
            if rec.bol_number and rec.bol_number != before:
                updated = True
                messages.append(f"BOL {rec.bol_number} found.")
    else:
        messages.append("BOL already exists — skipped Prophecy BOL check.")

    db.commit()

    if updated:
        logger.info("[REFRESH-BOL] %s → bol=%s weight=%s pallets=%s pcs=%s",
                    rec.manifest, rec.bol_number, rec.technique_weight, rec.technique_pallets, rec.technique_pcs)

    return {"updated": updated, "bol_number": rec.bol_number, "message": " ".join(messages)}


@app.post("/api/bols/{record_id}/retry-match", tags=["Admin"])
def retry_match_invoice(record_id: uuid.UUID, db: Session = Depends(get_db)):
    """on-demand retry for one stuck invoice_only stub against a wide 90-day technique window.

    Uses _wide_fallback_technique_search()'s default query_timeout=15 (fixed 2026-07-30,
    was query_timeout=None here on the theory that this route has the full request budget to
    itself so no query-level cap was needed) -- confirmed live that the outer _WIDE_FALLBACK_DEADLINE
    (a ThreadPoolExecutor + future.result(timeout=...) wrapper) is NOT a reliable backstop on its own:
    pyodbc's blocking call doesn't release the GIL while waiting on AWP-SQL-PROD (the same documented
    characteristic that made _get_connection() add a raw-socket pre-check for the connect phase), so
    when the live query itself runs long, the wrapper's own timeout can't fire on schedule either --
    the request just runs until Lambda's hard 29s function limit kills it outright (an ungraceful 500,
    not this endpoint's intended "timed_out": true response). pyodbc's own query_timeout is enforced by
    the ODBC driver itself, independent of the GIL, so it can actually cut a stuck query off in time for
    the graceful degrade below to run."""
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

    wide_match, all_candidates, timed_out = _wide_fallback_technique_search(
        job_name_s, float(stub.alg_weight or 0), stub.alg_pallets, stub.alg_pcs,
    )
    if wide_match is None:
        if timed_out:
            # search didn't finish -- distinct from a confirmed miss, retryable not final
            return {
                "matched": False,
                "timed_out": True,
                "message": "Technique search timed out before finishing -- please retry.",
            }
        return {"matched": False, "timed_out": False, "message": "Still not found in Technique (checked last 90 days)."}

    from backend.data_layer import get_manifest_weights
    weight_data = get_manifest_weights([wide_match["manifest"]]).get(wide_match["manifest"], {})
    row = _create_technique_record_from_fallback(db, wide_match, weight_data)
    row.invoice_number = stub.invoice_number
    row.inv_job_number  = stub.inv_job_number
    row.amount          = stub.amount
    row.alg_weight      = stub.alg_weight
    row.alg_pallets     = stub.alg_pallets
    row.alg_pcs         = stub.alg_pcs
    row.match_strategy  = "job_name"
    _compute_diffs(row)

    # persist this trip's other manifests too -- nothing else creates them since the daily pull was removed
    siblings = [
        c for c in all_candidates
        if c.get("technique_trip") == wide_match.get("technique_trip")
        and c.get("manifest") != wide_match.get("manifest")
    ]
    if siblings:
        existing_manifests = {
            m for (m,) in db.query(BOLRecord.manifest)
            .filter(BOLRecord.technique_trip == wide_match["technique_trip"])
            .all()
        }
        for sib in siblings:
            if sib.get("manifest") in existing_manifests:
                continue
            sib_weight_data = {
                "technique_weight": sib.get("technique_weight", 0),
                "technique_pallets": sib.get("technique_pallets", 0),
                "technique_pcs": sib.get("technique_pcs", 0),
            }
            _create_technique_record_from_fallback(db, sib, sib_weight_data)

    from backend.data_layer import get_tariff_rate as _get_tariff_rate
    from backend.data_layer import get_current_diesel_price, get_fsc_rate as _get_fsc_rate
    _diesel_price = get_current_diesel_price()
    _fsc_pct = _get_fsc_rate(_diesel_price) if _diesel_price is not None else None
    _finish_resolving_stub(
        row, stub.invoice_email_sender, stub.invoice_sent_at, settings.INVOICE_FOLDER,
        _get_tariff_rate, _diesel_price, _fsc_pct,
    )

    db.delete(stub)
    db.commit()
    logger.info("[RETRY-MATCH] Resolved stub %s → %s", stub.invoice_number, row.technique_trip)
    return {"matched": True, "matched_trip": row.technique_trip, "message": f"Matched to {row.technique_trip}."}


@app.get("/api/logs", response_model=list[BOLSummary], tags=["Logs"])
def get_logs(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    status: Optional[str] = "approved",
    invoice_sender: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """historical log of approved records by default; ?status=all includes pending/flagged.
    sorted by invoice_sent_at desc, then created_at desc"""
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
        # invoice_sent_at desc (nulls last), then created_at desc
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


@app.get("/api/export/invoice-pdfs", tags=["Export"])
def export_invoice_pdfs(invoice_numbers: str):
    """merge and download invoice PDFs for the given comma-separated z-numbers; skips
    any that can't be located rather than failing the whole batch"""
    from pypdf import PdfWriter, PdfReader
    import io as _io

    z_list = [z.strip().upper() for z in invoice_numbers.split(",") if z.strip()]
    writer = PdfWriter()
    missing: list[str] = []

    for z in z_list:
        pdf_bytes = _fetch_invoice_pdf_bytes(z)
        if pdf_bytes is None:
            missing.append(z)
            logger.warning("[INVOICE-PDF] PDF not found for %s", z)
            continue
        for page in PdfReader(_io.BytesIO(pdf_bytes)).pages:
            writer.add_page(page)

    if len(writer.pages) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No invoice PDFs found for: {', '.join(missing or z_list)}",
        )

    buf = _io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    if missing:
        logger.info("[INVOICE-PDF] Merged %d page(s); skipped %d Z-numbers with no PDF: %s",
                    len(writer.pages), len(missing), ", ".join(missing))
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="SG360_Invoices_{date_str}.pdf"'},
    )


@app.post("/api/export", response_model=ExportResponse, tags=["Export"])
def export_approved_bols(
    body: ExportRequest = ExportRequest(),
    db: Session = Depends(get_db),
):
    """generate CSV of approved records and email to Mary + Katie; email failure is a soft failure"""
    target = body.export_date or date.today()

    if settings.USE_MOCK_DATA:
        # mock data doesn't represent real daily batches -- ignore the date filter
        approved = [r for r in _mock_state.values() if r["status"] == "approved" and not r.get("is_do_not_pay", False)]
    else:
        rows = (
            db.query(BOLRecord)
            .filter(
                BOLRecord.status == BOLStatus.APPROVED,
                BOLRecord.is_do_not_pay == False,
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

    recipients = body.email_recipients or settings.EMAIL_TO_ACCOUNTING
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


# ---------------------------------------------------------------------------
# AWS Lambda entrypoint (Stage 1 — container image deployment)
# ---------------------------------------------------------------------------
from mangum import Mangum  # noqa: E402

handler = Mangum(app)
