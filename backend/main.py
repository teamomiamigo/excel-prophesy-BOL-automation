import csv
import io
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from fastapi import FastAPI, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# Networking problems must degrade to a logged error in ~2s, never a hang that
# eats Lambda's whole 29s budget and surfaces as an opaque 500 (which is exactly
# what the S3 PDF route did while this VPC's DNS was broken).
_S3_FAST_FAIL = BotoConfig(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1})

from backend.config import settings
from backend.database import get_db, engine
from backend.models import (
    Base, BOLRecord, ApprovalHistory, BOLStatus, ActionType,
    BOLSummary, FlagRequest, ApproveRequest,
    ExportRequest, ExportResponse, HealthResponse,
    ManifestCandidate, TripManifestsResponse,
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
        try:
            Base.metadata.create_all(bind=engine)
        except IntegrityError as exc:
            # SQLAlchemy's create_all() checks "does this Postgres ENUM type
            # exist" and creates it in two separate, non-atomic steps. Under
            # Lambda's concurrent cold starts, two instances can both see
            # "not yet created" and race to CREATE TYPE — the loser hits this
            # UniqueViolation even though the schema is now in the desired
            # state. Safe to swallow only for that specific "already exists"
            # case; anything else is a real migration failure.
            if "already exists" not in str(exc.orig):
                raise
            logger.info("DB schema already created by a concurrent cold start; continuing.")
        logger.info("DB tables verified/created.")
        # Postgres native enums don't pick up new Python enum members automatically —
        # ADD VALUE must run as its own statement (can't share a transaction with the
        # ADD COLUMN batch below on older Postgres versions), hence its own connection.
        with engine.connect() as _enum_conn:
            _enum_conn.execute(text("ALTER TYPE actiontype ADD VALUE IF NOT EXISTS 'DO_NOT_PAY'"))
            _enum_conn.commit()
        # RULE: when a column is removed from an ORM model (backend/models.py),
        # its ADD COLUMN IF NOT EXISTS line below must be changed to a
        # DROP COLUMN IF EXISTS line in the SAME commit — never just left in
        # place. A Python-side SQLAlchemy `default=` is never a real Postgres
        # DEFAULT; an orphaned NOT NULL column with no DB-level default
        # rejects every future INSERT that omits it. This bit us on
        # 2026-07-16 (is_ignored removed from the model in #69/2026-07-15, but
        # left as ADD COLUMN IF NOT EXISTS here — the column already existed
        # live, so IF NOT EXISTS silently made this line a permanent no-op).
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
        logger.info(
            "DB column migration for base_tariff/fsc_pct/is_third_party/is_do_not_pay/"
            "invoice_sent_at/alg_fsc_pct/alg_fsc_cost/tariff_zone_approximate/weight_source_fallback/"
            "is_ambiguous_trip/min_charge_uncertain/cost_calc_detail/is_dismissed/mismatch_acknowledged complete "
            "(is_ignored dropped 2026-07-16 — see Developmental Documentation.md)."
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
        .filter(
            BOLRecord.status != BOLStatus.APPROVED,
            # Excludes sibling-manifest stubs (2026-07-22): retry_match_invoice() now
            # persists a trip's other manifests too when it resolves an ambiguous match
            # (see "Ambiguous trips" in CLAUDE.md) so Compare/reassign has real data to
            # work with — but those siblings have no invoice and aren't Katie's to review
            # individually; they'd just be redundant rows she'd have to dismiss one by
            # one. The trip's actual invoiced record (which does have invoice_number)
            # still shows, badged ~UNVERIFIED, with the Compare button as the one place
            # to see/act on its siblings. Nothing else creates an invoice-less record
            # post-Phase-4 (the old daily bulk pull's "Awaiting Invoice" pre-population
            # is gone), so this filter only ever excludes exactly these stubs.
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
    Covers two populations: pre-invoice Technique records (no amount/BOL yet),
    and invoice-only stubs that never matched any Technique/Prophecy record at
    all (no technique_trip, no BOL — the invoice may still carry an amount, since
    ALG billed something we just can't identify). Blocked once a record has BOTH
    a real technique_trip AND an amount (a normal matched/invoiced Corp record),
    or once a BOL number exists (already tied to a real Prophecy load). Idempotent."""
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


@app.post("/api/bols/{record_id}/dismiss", response_model=BOLSummary, tags=["BOLs"])
def dismiss_sibling(record_id: str, db: Session = Depends(get_db)):
    """
    Dismiss a bad/duplicate sibling manifest on an ambiguous trip — for the case
    CompareManifestsModal.jsx exists to handle: Technique split a trip into manifests
    that don't actually both need an invoice (human error making the manifest, a stray
    duplicate, etc.). Only ever called from that modal's "Delete" button, on a candidate
    that isn't the one holding the actual invoice.

    Reversible in principle (nothing is deleted, just hidden), but there's no undo route
    yet since nothing in the UI surfaces a dismissed record to undo from — added
    2026-07-22 alongside sibling-manifest persistence (see "Ambiguous trips" in
    CLAUDE.md); add one if that changes.
    """
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
    """
    Clear the ~UNVERIFIED badge for a severe weight/pallet/piece mismatch that has no
    ambiguous trip to compare against — the Compare modal only applies when
    is_ambiguous_trip is set (multiple manifests to actually choose between); a
    single-manifest mismatch had no available action at all before this (added
    2026-07-22, direct user feedback after the Compare-button work). Unlike dismiss,
    no guard on invoice presence — this doesn't hide or change any data, just
    acknowledges the discrepancy is expected/explained. No undo route — same reasoning
    as dismiss.
    """
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
    """
    Mark an unresolvable invoice-only record as do-not-pay: approves it (so it
    joins its sender's batch in the Approved section) and flags it to render
    "DO NOT PAY" instead of a dollar amount everywhere. Reversible via
    unmark-do-not-pay. Only valid for records with no Technique match at all
    (invoice-only / Wolf-311 stubs) — same population the old Ignore button targeted.
    """
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

    # Find target — never a dismissed sibling (2026-07-22): Katie explicitly said this
    # manifest is bad/duplicate data via the Compare modal's Delete button, so it
    # shouldn't be assignable as a reassign target either, even by BOL/trip/manifest
    # number typed directly into ReassignInvoiceModal.
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
        target_row.invoice_number = src_inv
        target_row.amount = src_amount
        target_row.alg_weight = src_alg_weight
        target_row.alg_pallets = src_alg_pallets
        target_row.alg_pcs = src_alg_pcs
        target_row.inv_job_number = src_inv_job_number
        target_row.invoice_email_sender = src_invoice_email_sender
        target_row.invoice_sent_at = src_invoice_sent_at

    # Recompute Calculated Cost + Cost % for the manifest the invoice now actually lives
    # on (2026-07-22 fix) — previously this only recomputed cost_pct from whatever
    # access_prog the target already had, which is null for an unmatched sibling (Calc
    # Cost is only ever computed once a manifest actually has an invoice), so a
    # reassigned record could show a blank/stale Calculated Cost and Cost % indefinitely.
    # Same re-parse-the-invoice-CSV approach POST /api/admin/recompute-access-prog uses.
    _recompute_access_prog_for_record(target_row, settings.INVOICE_FOLDER)
    # Diffs need to be against target_row's OWN technique_weight/pallets/pcs — never
    # recomputed here before this fix, so ΔWgt/ΔPal/ΔPcs on the dashboard stayed at
    # whatever the target had before the reassignment (usually null).
    _compute_diffs(target_row)
    target_row.updated_at = datetime.now(timezone.utc)

    is_stub = source_row.technique_trip is None
    if is_stub:
        db.delete(source_row)
    else:
        # Comprehensive clear (2026-07-22) — previously left access_prog/base_tariff/
        # fsc_pct/alg_fsc_pct/alg_fsc_cost/cost_calc_detail/match_strategy/
        # invoice_email_sender/invoice_sent_at/carrier stale on the source after its
        # invoice moved elsewhere, none of which made sense once the record had no
        # invoice. Does NOT touch notes/flag_reason — Katie's own annotations,
        # independent of which invoice happens to be attached.
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
    """
    Every manifest sharing this record's Technique trip, for manual verification
    of an is_ambiguous_trip row — one trip can split into several manifests, and
    nothing here guesses which one an invoice really belongs to; it just surfaces
    all of them, scored against whichever manifest the invoice actually landed on,
    so a human can decide. DB/mock only — no live Technique query.
    """
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

# Same idea as _INVOICE_FIELDS_TO_NULL, but scoped to reassign_invoice()'s source-clearing
# (2026-07-22) rather than the dev-only reset-invoices wipe -- deliberately excludes
# notes/flag_reason, which are Katie's own manual annotations, not derived from whichever
# invoice happens to be attached, and shouldn't be wiped just because one moved elsewhere.
_REASSIGN_SOURCE_CLEAR_FIELDS = [
    "invoice_number", "invoice_email_sender", "invoice_sent_at", "inv_job_number", "carrier",
    "alg_weight", "alg_pallets", "alg_pcs",
    "access_prog", "amount", "cost_pct", "base_tariff", "fsc_pct", "alg_fsc_pct", "alg_fsc_cost",
    "match_strategy", "weight_diff", "pallet_diff", "pcs_diff", "cost_calc_detail",
]


@app.post("/api/admin/reset-invoices", tags=["Admin"])
def reset_all_invoices(confirm: bool = False, db: Session = Depends(get_db)):
    """
    Dev-only: delete invoice-only stub records and clear every ALG-invoice-
    derived field on all Technique records -- including already-approved ones
    -- for a clean invoice upload from scratch without re-running the pull.

    Deliberately unconditional on status: a record whose invoice/cost data was
    just wiped can't sensibly stay "approved" with nothing left to show for it,
    so status resets to pending and flag_reason clears regardless of the
    record's current status. This means it WILL destroy real historical
    billing data on any already-approved/exported record if run against a live
    database with real data in it -- that's an explicit, deliberate choice, not
    an oversight; hence the confirm gate below.

    Never touches: Technique-side fields (technique_trip, manifest,
    technique_weight/pallets/pcs, bol_number, needs_sid_export), is_third_party
    (a manual categorization independent of any invoice), sid_exported_at (the
    Prophecy SID/BOL export lifecycle is independent of ALG invoice data), or
    the static tariff_rates/fuel_surcharge_rates/alg_tariff_rates rate-card
    tables -- only ALG-invoice-derived data is ever cleared.
    """
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

    # Partition in Python, not SQL: a `technique_trip IS NOT NULL` WHERE clause
    # misses Wolf/311 records (match_strategy="prophecy_bol"), which legitimately
    # have technique_trip=None too -- they match on Prophecy BOL number, not a
    # Technique trip. Every row that isn't an invoice_only stub needs clearing,
    # regardless of which side it matched through.
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
    """
    Dev-only: deletes ALL bol_records (pending/approved/flagged/logged) and their
    cascaded approval_history, for a clean invoice-by-invoice re-test.
    Does NOT touch tariff_rates, fuel_surcharge_rates, or users.
    """
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass ?confirm=true to wipe all BOL records")

    if settings.USE_MOCK_DATA:
        count = len(_mock_state)
        _mock_state.clear()
        return {"records_deleted": count}

    rows = db.query(BOLRecord).all()
    count = len(rows)
    for row in rows:
        db.delete(row)  # per-row delete (not Query.delete()) so the ORM
    db.commit()          # cascade="all, delete-orphan" to approval_history fires
    return {"records_deleted": count}


def _apply_bol_status(row: "BOLRecord", technique_row: dict) -> None:
    """
    Set bol_number/needs_sid_export from a Technique/ShipperPlus query row's
    load_id/pooled_to_load_id. Type A (no BOL yet, needs_sid_export=True) vs
    Type B (load_id/pooled_to_load_id > 0 — a BOL already exists in Prophecy).
    Used by the per-record BOL check (POST /api/bols/{id}/refresh-bol). Also used by
    the daily bulk pull (POST /api/admin/pull) until that route was removed 2026-07-22
    in favor of the per-invoice automatic matching in _process_invoice_csv()/retry-match.
    """
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
    # else: row already has a bol_number from a prior match (Type B). A later
    # query returning no load_id doesn't mean the BOL vanished from Prophecy —
    # it's far more likely a transient query/join hiccup. Leave needs_sid_export
    # (and bol_number) as-is instead of flip-flopping the record back to Type A.


def _select_canonical_technique_row(rows: list[dict]) -> dict:
    """
    Given all Technique rows sharing one (technique_trip, manifest) key — which can
    be more than one, since _TECHNIQUE_QUERY's GROUP BY includes pooled_to_load_id/
    load_id/TranType and those can legitimately differ pallet-to-pallet within one
    real manifest — deterministically pick the canonical row. Mirrors Katie's own
    manual process on the Technique report: sort by Pool to Load = 0, then Tran Type
    = Prepaid, then dedupe.

    load_id/pooled_to_load_id on the result are resolved independently via max()
    across ALL rows in the group, not just taken from the tie-break winner — a
    manifest that's already pooled into a load (Type B) must never be silently
    downgraded to Type A just because a sibling duplicate row happens to look
    "cleaner" (Prepaid + unpooled). 0 always means "no signal from this row"; any
    positive value is authoritative, so max() can only turn a would-be Type A into a
    correctly-detected Type B, never the reverse.
    """
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
    """
    Collapse a flat get_technique_data() result down to one row per
    (technique_trip, manifest), via _select_canonical_technique_row(). Every call
    site that resolves Technique-row duplicates should go through this — previously
    each did it differently (first-wins here, last-wins there, arbitrary DB row
    order), which is how the wrong duplicate ended up winning.
    """
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
    """
    weight_diff/pallet_diff/pcs_diff = ALG invoiced qty - our own recorded qty.
    Uses Prophecy quantities as the baseline for Wolf/311 records (no technique_trip,
    prophecy_* populated), Technique quantities otherwise — same "is this Prophecy-
    sourced" check BOLRow.jsx uses on the frontend. Single source of truth: call this
    anywhere a diff needs (re)computing instead of duplicating the formula.

    A record with neither a technique_trip nor Prophecy quantities (a genuinely
    unmatched invoice-only stub) has no independent baseline at all — technique_weight/
    pallets/pcs are 0 there only because the DB columns are non-nullable, not because
    we actually know our own quantity is zero. Diffing against that sentinel would
    just restate ALG's own number as a fake "difference", so leave diffs null instead.
    """
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
    """Idempotent notes-append for the pull/refresh paths — a manifest stuck without
    Query B weight data gets re-pulled/re-refreshed repeatedly, so this must not pile
    up duplicate copies of the same diagnostic note each time."""
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
    """e.g. 'TEC_M_0228920' -> '228920'. Fallback matching key (issue #65) for invoices
    whose Job Name reflects the manifest number rather than the trip number — a trip and
    its manifest are genuinely different numbers, so this cannot reuse _trip_to_suffix()
    (its 'T_' split finds nothing in a manifest string, e.g. 'TEC_M_...' has no 'T_').
    Comingle manifests (e.g. 'CM_052926A') coincidentally contain 'M_' too but end in a
    letter, not a pure number — caught and treated as no-suffix rather than raising."""
    parts = (manifest or "").split("M_")
    if len(parts) < 2:
        return ""
    try:
        return str(int(parts[-1]))
    except ValueError:
        return ""


def _create_technique_record_from_fallback(db: Session, m: dict, weight_data: dict) -> "BOLRecord":
    """
    Create a brand-new BOLRecord for a manifest found only via a wide-window Technique
    fallback query (see POST /api/bols/{id}/retry-match, called automatically by the
    frontend right after a new invoice_only stub is created — see _process_invoice_csv())
    — deliberately separate from the old daily bulk pull's per-manifest upsert (removed
    2026-07-22), which had wipe-on-existing semantics that don't apply here: this only
    ever fires for a manifest we've never seen before.
    """
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


def _fetch_invoice_pdf_bytes(z: str) -> "bytes | None":
    """Fetch raw PDF bytes for a Z-number: S3 first, then the local disk cache
    (whatever _store_invoice_pdf_bytes() wrote there), then INVOICE_FOLDER as a
    last resort for PDFs that never passed through this app at all (e.g. one
    poll_invoice_folder found sitting on the shared drive next to its CSV).
    Returns None if the PDF can't be found anywhere."""
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


# S3 (INVOICE_S3_BUCKET) is the only persistent store reachable from Lambda,
# but a local dev machine usually has no bucket configured at all — without
# this, a PDF uploaded through the frontend's multipart pdf_file field would
# be read into memory and then simply discarded, with nowhere to retrieve it
# from afterward. Individual PDFs are cached flat (mirrors the S3 key layout,
# "{z}.pdf"); merged batch PDFs live under a batches/ subfolder.
_INVOICE_PDF_CACHE_DIR = os.path.join(os.path.dirname(__file__), "invoice_pdf_cache")


def _store_invoice_pdf_bytes(z: str, data: bytes) -> None:
    """Persist one invoice's PDF bytes: S3 if INVOICE_S3_BUCKET is set, else the
    local disk cache. Best-effort — logs and swallows failures, matching the
    error handling already used at this function's one call site."""
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
    """Persist a merged batch PDF under the same S3-or-local-cache split as
    _store_invoice_pdf_bytes(), keyed under a batches/ prefix/subfolder."""
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
    """Fetch a previously-merged batch PDF: S3 if configured, else local cache.
    Returns None if no precomputed batch PDF exists yet for this slug."""
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
    """Turn an invoice_email_sender label (e.g. 'Tania 6/25/2026 4:16PM') into
    a filesystem/S3-key-safe slug for batch PDF storage — used as the storage
    key only. For a user-facing download filename, use _readable_batch_name()
    instead; this one is deliberately unreadable (every non-alphanumeric char
    becomes '_') so it can't collide across senders whose labels differ only
    in punctuation."""
    import re
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", label.strip()).strip("_")
    return slug or "unassigned"


def _readable_batch_name(label: str) -> str:
    """Human-readable version of a batch label (e.g. 'Tania 6/25/2026 4:16PM'
    -> 'Tania 6-25-2026 4-16PM') for use in a download filename. Unlike
    _slugify_sender() (the storage key), this only swaps the handful of
    characters Windows/macOS actually forbid in a filename, leaving the
    sender name/date/time readable as-is — the downloaded file is named
    after the batch itself, not an opaque slug."""
    import re
    name = re.sub(r'[\\/:*?"<>|]+', '-', label.strip())
    return name or "batch"


def _collect_batch_invoice_numbers(sender: str, db: Session) -> "list[str]":
    """All distinct Z-numbers on records sharing this invoice_email_sender —
    splits comma-joined invoice_number values (one record can carry more than
    one Z-number, see _merge_invoice_numbers) and preserves first-seen order."""
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
    """Merge every currently-locatable PDF for one invoice_email_sender batch
    into a single PDF and persist it under that sender's slug, so the
    "Download Invoices" button can serve a batch instantly instead of
    re-merging on every click. Called once after a folder upload finishes
    (POST /api/invoices/merge-batch-pdfs) and again, best-effort, at the end
    of poll_invoice_folder(). Safe to call repeatedly — always re-merges from
    current state, so a later-arriving PDF (e.g. a resolved stub) is picked up
    next time it's called. A Z-number with no locatable PDF is skipped, not
    fatal to the merge.
    """
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


# How far apart (numerically) a pallet's zip3 and an invoice's billed zip3 can
# be and still be treated as the same zone. Confirmed against real data
# (Z558429): Prophecy destination_ids carry SCF *zone* codes (e.g. "SCF350")
# while ALG bills the actual postal zip3 the SCF serves ("352") — the pairs
# are always within a few digits (085↔086, 350↔352, 890↔891) but almost never
# equal, so an exact join missed 33 of 33 zones on that invoice.
_ALG_ZONE_TOLERANCE = 5


def _lookup_alg_rate(alg_rate_by_zip3: dict, zip3: str) -> "float | None":
    """Exact zip3 hit first; otherwise the numerically nearest invoice zone
    within _ALG_ZONE_TOLERANCE. Adjacent-zone rates are close ($/cwt within
    ~10%), so a near miss is a far better estimate than dropping the zone."""
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


# Below this fraction of our load's weight successfully rated per-zone, the per-zone
# sum is discarded as unrepresentative in favor of the invoice's own blended rate, or
# an honest null when no blended rate is available either.
#
# Effectively requires full coverage (2026-07-15): a lower threshold (0.8 originally)
# let a partially-rated shipment silently report only the rated slice's dollars as if
# it were the whole shipment's cost — e.g. an 85%-covered load reported ~85% of its
# true cost, with no scaling and no fallback, because 85% cleared the old 80% bar.
# That single mechanism explained most of the systematic under-pricing this threshold
# was chasing. Requiring full coverage means ANY unrated zone now falls back to the
# invoice's own blended rate for the whole load instead of silently dropping dollars —
# matches the coverage-gap fallback this branch already trusted for the worse case.
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
    """
    Compute access_prog/base_tariff/fsc_pct from SG360's OWN weight/pallet/piece data —
    never ALG's — applied against ALG's own invoiced per-zone rate, since the tariff/zone
    rate structure is legitimately ALG's pricing (using it isn't a violation of
    independence; substituting their weight/pallet counts for ours would be). Sets
    alg_fsc_pct/alg_fsc_cost and the tariff_zone_approximate/weight_source_fallback/
    min_charge_uncertain flags. Called from every real invoice-processing site
    (_process_invoice_csv()'s upload/stub-resolution paths, Wolf/311 stub creation,
    and the POST /api/admin/recompute-access-prog backfill) — each of those call
    sites passes `detail=[]` and persists the result onto BOLRecord.cost_calc_detail
    (JSON), which GET /api/bols/{id}/cost-breakdown then simply reads back rather
    than ever calling this function itself (see 2026-07-21: that route used to
    re-parse the original invoice CSV on every call, which only worked in local dev
    — INVOICE_FOLDER is a UNC path the deployed Lambda can never reach).

    alg_blended_rate — the invoice's own freight-total / total-cwt ($/cwt, FSC excluded),
    used as a whole-load fallback when per-zone rating covers less than
    _RATE_COVERAGE_THRESHOLD of our weight.

    alg_min_charge_by_zip3 — per-zip3 $ actually billed on THIS invoice where a minimum-
    charge floor fired (see _parse_alg_csv_context()). Used both to flag
    min_charge_uncertain (only when the floor actually determined the price via the
    less-trustworthy legacy source, or when no floor info exists anywhere — fixed
    2026-07-21, previously fired on any alg_tariff_rates miss regardless of whether
    it mattered, flagging nearly every real record including several with a
    provably correct dollar amount) and to teach alg_tariff_rates
    (data_layer.reconcile_alg_tariff_rates) for any pallet whose zip3 this invoice
    billed directly.

    detail — when a list is passed, one dict per pallet is appended describing exactly how
    that pallet was priced (dest_id, zip3, weight, rate source, rate/mc1 used, whether the
    floor fired). Purely additive — never changes matched_rec or the DB directly; callers
    that want it persisted json.dumps() it onto cost_calc_detail themselves.

    learn — set False to suppress the alg_tariff_rates reconciliation pass. Every real
    call site leaves this True (default).

    See CLAUDE.md's "access_prog calculation" section for the full rationale/priority order.
    """
    from backend.data_layer import get_alg_tariff_rate, reconcile_alg_tariff_rates

    alg_min_charge_by_zip3 = alg_min_charge_by_zip3 or {}

    _effective_fsc_pct = Decimal(str(fsc_rate_val)) if fsc_rate_val is not None else _fsc_pct
    matched_rec.alg_fsc_pct = Decimal(str(fsc_rate_val)) if fsc_rate_val is not None else None
    matched_rec.alg_fsc_cost = Decimal(str(round(fsc_cost_val, 2))) if fsc_cost_val is not None else None

    own_pallets: list[tuple[str, float, "str | None"]] = []  # (zip3, weight, exact_dest_id)
    if effective_prophecy_bol:
        from backend.data_layer import get_prophecy_pallet_data as _get_prophecy_pallet_data
        # Same graceful-degradation contract as the get_pallet_data_for_manifests
        # branch below -- a slow/hung live query here should leave own_pallets empty
        # (weight_source_fallback=True), not fail the whole invoice upload.
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
            # Prefer the actual postal zip3 (destination_zip) over the SCF zone code
            # (destination_id "SCF350" → "350"): ALG's invoice bills actual zip3s, so
            # this is what joins against alg_rate_by_zip3 — the SCF code is the reason
            # exact joins used to miss on every zone (see _ALG_ZONE_TOLERANCE). The exact
            # dest_id is still carried alongside for the alg_tariff_rates lookup below,
            # which matches on it directly and needs no zip3 derivation.
            zip3 = (dest_zip[:3] if dest_zip else None) or (dest_id[3:6] if dest_id and len(dest_id) >= 6 else None)
            weight = float(prow.get("weight") or 0)
            if zip3 and weight > 0:
                own_pallets.append((zip3, weight, dest_id))
    elif matched_rec.manifest:
        from backend.data_layer import get_pallet_data_for_manifests as _get_pallet_data_for_manifests
        # get_pallet_data_for_manifests() intentionally re-raises on query failure (correct
        # for its other caller, the SID export routes, where fail-fast is right) — but this
        # access_prog path already has a documented graceful-null story for empty own_pallets
        # below, so a live query failure here should degrade the same way, not 500 the whole
        # invoice upload/recompute request.
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
            # Prefer the real ZIP (Locations.ZipCode, live from VisualMail) over slicing
            # the destination code — confirmed 2026-07-16 that code's own digits are ALG's
            # zone label, not always the real zip3 (e.g. "ASF140" labels a facility whose
            # real ZIP is 142xx). Same pattern as the Wolf/311 branch above.
            zip3 = (str(dest_zip)[:3] if dest_zip else None) or (dest_id[3:6] if dest_id and len(dest_id) >= 6 else None)
            if zip3 and weight > 0:
                own_pallets.append((zip3, weight, dest_id))

    matched_rec.weight_source_fallback = not bool(own_pallets)
    if not own_pallets:
        # No own weight/pallet data available at all (manifest/BOL not found, not yet
        # synced) — no independent data means no independent estimate. Leave access_prog
        # blank rather than substituting ALG's own invoiced weight.
        matched_rec.tariff_zone_approximate = False
        matched_rec.min_charge_uncertain = False
        return

    new_tariff_sum = Decimal("0")
    new_base_sum = Decimal("0")
    any_approximate = False
    any_min_charge_uncertain = False
    # (dest_id, this invoice's own directly-billed rate, observed floor $ or None) — only
    # for pallets whose exact zip3 this invoice billed directly (never a nearest-zone
    # tolerance guess); fed to reconcile_alg_tariff_rates() after the loop so a shared table
    # every future calculation depends on only ever learns from a genuine same-invoice hit.
    to_learn: list[tuple[str, float, "float | None"]] = []
    total_weight = sum(w for _, w, _ in own_pallets)
    rated_weight = 0.0
    for zip3, weight, exact_dest_id in own_pallets:
        direct_rate = alg_rate_by_zip3.get(zip3)
        if exact_dest_id and direct_rate is not None:
            to_learn.append((exact_dest_id, direct_rate, alg_min_charge_by_zip3.get(zip3)))

        # Rate/zone structure is ALG's own pricing — use their invoiced rate for this
        # zone first; our internal rate card is only a fallback for a zone this invoice
        # didn't happen to bill.
        alg_rate = _lookup_alg_rate(alg_rate_by_zip3, zip3)
        if alg_rate is not None:
            base = Decimal(str(round(alg_rate * weight / 100.0, 2)))
            # ALG applies a minimum freight charge per shipment; apply the same floor
            # here using our own weight — otherwise a pallet priced via ALG's rate has
            # no minimum-charge protection at all. Source the minimum from
            # alg_tariff_rates.mc1 (ALG's own complete rate export, ~0% zone coverage
            # gap, keyed by this pallet's exact dest_id) rather than the older zip3-keyed
            # tariff_rates card (confirmed 2026-07-16 to be missing ~64% of real zones,
            # which was silently skipping the minimum-charge floor on most pallets and
            # systematically under-pricing loads with several small/light shipments —
            # e.g. $556 of a $571 gap on one real invoice traced directly to this).
            # Only fall back to the old card if this exact dest_id isn't in alg_tariff_rates.
            alg_min = get_alg_tariff_rate(exact_dest_id) if exact_dest_id else None
            mc1_used = None
            mc1_source = None
            if alg_min is not None:
                base = max(base, alg_min["mc1"])
                mc1_used, mc1_source = alg_min["mc1"], "alg_tariff_rates"
            else:
                # alg_tariff_rates was independently confirmed ~100% accurate for every
                # destination checked by hand 2026-07-21, but a miss here only actually
                # matters if it changed the price or left us with zero floor information —
                # confirmed 2026-07-21 that flagging on every miss regardless (the previous
                # behavior) fired on nearly every real record, including ones whose dollar
                # amount was independently verified correct, since it only takes one such
                # pallet out of a hundred to flag the whole record.
                zone_info = _get_tariff_rate(zip3, weight, _diesel_price=_diesel_price, _fsc_pct=_effective_fsc_pct)
                if zone_info and zone_info.get("minimum_freight") is not None:
                    base = max(base, zone_info["minimum_freight"])
                    mc1_used, mc1_source = zone_info["minimum_freight"], "legacy_tariff_rates"
                    if base == mc1_used:
                        # The floor actually determined the price, using the less-
                        # trustworthy legacy source instead of alg_tariff_rates.
                        any_min_charge_uncertain = True
                else:
                    # No floor info anywhere — not alg_tariff_rates, not the legacy card.
                    # Can't rule out a real minimum charge we're simply missing; silence
                    # here isn't confirmation that no floor applies.
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
        # This invoice didn't bill this exact zone — next choice is an exact match
        # against ALG's own published rate table (alg_tariff_rates, keyed on the same
        # Dest_ID format our own pallet data already carries), which is far more
        # complete than the zip3-keyed internal card below (confirmed 2026-07-15: 0%
        # of a real invoice's zones missing here vs. 64% missing from the old card).
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
        # Live-only path (mock mode never calls this function), and idempotent —
        # a second invoice upload for the same trip must not duplicate the note.
        if text not in (matched_rec.notes or ""):
            matched_rec.notes = f"{matched_rec.notes} {text}".strip() if matched_rec.notes else text

    if coverage >= _RATE_COVERAGE_THRESHOLD:
        matched_rec.tariff_zone_approximate = any_approximate
        if new_tariff_sum > 0:
            # Recomputed fresh from our own manifest/BOL data each time (not accumulated
            # per-invoice) — our own weight doesn't change across multiple Z-invoices for the
            # same trip, unlike the old ALG-weight-based calc which needed to add each
            # invoice's own partial line items.
            matched_rec.access_prog = new_tariff_sum
            matched_rec.base_tariff = new_base_sum if new_base_sum > 0 else None
            matched_rec.fsc_pct = _effective_fsc_pct
    elif alg_blended_rate is not None and alg_blended_rate > 0:
        # Not enough per-zone coverage for a representative sum — price our whole
        # weight at the invoice's own blended $/cwt instead. Still our weight ×
        # their rate, just without per-zone resolution; Cost % then meaningfully
        # measures billed-weight variance rather than exploding on missing zones.
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
        # Neither per-zone coverage nor a usable blended rate — an honest null beats
        # publishing a number built from a sliver of the load.
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
        # Per-zip3, the $ actually billed on a line where ALG's own minimum-freight-charge
        # floor fired (printed Rate x GrossWt would have computed less than Billed$) — see
        # the detection below. Feeds the self-updating alg_tariff_rates reconciliation in
        # _apply_access_prog_calc(); a zip3 absent here means no floor was observed on this
        # invoice for that zone, not that no minimum applies.
        "alg_min_charge_by_zip3": {},
        # Freight-only dollar total (sum of freight-row Billed$, excludes the FSC
        # footer) — feeds the blended-rate fallback in _apply_access_prog_calc when
        # per-zone joins can't cover enough of the load.
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
        # First non-blank row wins (same convention as cust_job_no below) — a
        # multi-line invoice's Job Name is normally repeated on every line, but
        # if a later line item happens to leave it blank, blindly overwriting
        # would clear an already-correct matching key to '' and misclassify a
        # real trip as unmatched.
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
            # ALG's own printed Rate is the real per-cwt price — confirmed against 126
            # real historical invoices (7,290 freight lines) to always be populated and
            # to match our internal tariff card exactly, zone for zone. Reading it
            # directly avoids back-computing Billed$/GrossWt, which silently bakes in
            # ALG's per-shipment minimum-freight charge as if it were a flat rate (e.g.
            # a $70 minimum on a 216 lb parcel implies a fake $32/cwt "rate"). Only fall
            # back to the derived value if Rate is genuinely absent on some future format.
            effective_rate = rate_val if rate_val > 0 else (
                round(billed / (gross_wt / 100.0), 4) if raw_zip and gross_wt > 0 and billed > 0 else None
            )
            if raw_zip and effective_rate:
                ctx["alg_rate_by_zip3"].setdefault(raw_zip[:3], effective_rate)
            # Detect a real minimum-freight-charge floor on this line: when the printed
            # Rate would compute less than what was actually billed, ALG applied its own
            # per-destination minimum for this zone (confirmed 2026-07-21 against real
            # invoices — Traverse City MI, Abilene TX, etc. — that this observed $ figure
            # exactly matches ALG's real contracted minimum for that destination). Only
            # trustworthy when Rate was genuinely printed (rate_val > 0); a derived
            # effective_rate would trivially "match" billed and could never reveal a floor.
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

    # The CSV's Fuel Surcharge row prints its Rate rounded to 2 decimals (e.g. "0.41"),
    # but the invoice's own PDF and its two exact dollar figures agree on a more precise
    # value (e.g. 0.365, matching FSC$/freight$ to 4 decimals across every real invoice
    # checked 2026-07-15) — unlike the per-zone Rate above, this one IS worth deriving
    # from the exact dollars rather than trusting the printed label.
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
    """
    Common cleanup after a code path attaches a resolved invoice_only stub's data onto
    a real Technique record OUTSIDE the main upload flow (_apply_invoice_match() already
    does both of these inline, as part of parsing the invoice CSV for the first time).
    Used by POST /api/bols/{id}/retry-match (the only caller since the old daily bulk
    pull's DB-side stub re-match was removed 2026-07-22 along with the rest of that
    route) -- this used to leave invoice_email_sender/invoice_sent_at blank and never
    compute access_prog/cost_pct, since it only copied invoice_number/amount/alg_*
    onto the resolved record.

    1. Copies invoice_email_sender/invoice_sent_at from the stub being consumed (rec
       itself never had them -- it's either a brand-new fallback record or an existing
       Technique record that was never an invoice).
    2. Re-locates rec's own invoice CSV (_find_invoice_file, require_csv=True) and
       re-parses it to compute access_prog/base_tariff/fsc_pct/cost_pct, the same way
       POST /api/admin/recompute-access-prog does for a single record.

    No-ops silently, leaving rec's cost fields null, if `folder` isn't configured/found,
    the file can't be located, or our own pallet data isn't available -- same resilience
    contract as recompute_access_prog(), since none of these are truly exceptional here.
    """
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
    # Not rec.match_strategy == "prophecy_bol" -- that field is stored and can go stale
    # (a duplicate re-upload used to silently overwrite it to "invoice_number" before
    # 2026-07-20's fix; any record corrupted before that fix still carries the wrong
    # value today). "No Technique manifest, but a real BOL" is what a Wolf/311 load
    # structurally *is*, independent of whatever match_strategy currently says.
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


_CLOSE_MATCH_THRESHOLD = 0.15  # combined relative difference across weight/pallets/pcs;
                                # above this, log a warning and note the record for manual
                                # verification, but still commit to the closest candidate —
                                # tune against real invoices once this is live.


def _cget(c, field):
    """Read a field off a candidate that may be a BOLRecord (live mode) or a dict
    (mock mode) -- shared by the trip-suffix/manifest-suffix matching strategies."""
    return c.get(field) if isinstance(c, dict) else getattr(c, field, None)


def _score_technique_candidates(candidates: list, total_weight, total_pallets, total_pcs):
    """
    Given several BOLRecords/dicts sharing one trip suffix, score each by combined
    relative difference between its own technique_weight/pallets/pcs and the
    invoice's billed quantities, and return every (candidate, score) pair sorted
    best-first. Missing quantity data on a candidate scores as a full mismatch
    (1.0) on that axis rather than being skipped, so a record with no technique
    data never wins over one with real, closely-matching data.
    """
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
    """
    Reuses the same quantity-comparison idea as the pallets+pieces last-resort
    strategy below, just scoped to disambiguate within one trip instead of
    matching globally by exact equality only. Returns (best_candidate, best_score)
    — see _score_technique_candidates for the full ranked list (used by the
    trip-manifests comparison endpoint).
    """
    return _score_technique_candidates(candidates, total_weight, total_pallets, total_pcs)[0]


def _partition_candidates_by_resolution(candidates: list):
    """
    Given several BOLRecords/dicts sharing one trip or manifest suffix, filter out
    any candidate Katie has already marked is_third_party (she's told us it's not
    the billable-through-SG360 leg — never auto-attach a new invoice there), unless
    doing so would empty the pool entirely. Returns (usable, resolved):
      usable   - candidates still eligible for matching (3P ones excluded, unless
                 that would leave nothing)
      resolved - the subset of `usable` that already has a bol_number, i.e. Katie
                 has already created a real Prophecy BOL for it via her SID-export
                 flow. An empty `resolved` means nothing's been resolved yet.

    Resolution (bol_number / is_third_party) is a stronger, human-provided signal
    than quantity-closeness scoring — it comes from actions Katie already takes
    herself, not from guessing at Technique's own unreliable TranType/Notes fields.
    """
    non_tp = [c for c in candidates if not _cget(c, "is_third_party")]
    usable = non_tp if non_tp else candidates
    resolved = [c for c in usable if _cget(c, "bol_number")]
    return usable, resolved


def _flag_if_resolved_match_looks_wrong(
    matched_rec, total_weight, total_pallets, total_pcs, invoice_no: str, job_name: str, suffix_kind: str,
) -> None:
    """
    Diagnostic-only sanity check for the "exactly one resolved candidate" shortcut
    above: a bol_number means Katie already resolved this ambiguity, so it must
    keep winning the match regardless of how well its quantities fit — never
    override matched_rec here. But nothing else was ever checking whether that
    resolved candidate's own quantities are even a plausible fit for this invoice,
    so a stale/wrong bol_number on the wrong manifest could silently absorb an
    unrelated invoice with no signal anywhere. Log + note when the fit is bad,
    so it's visible (dashboard ~UNVERIFIED badge, Log tab, CSV exports) without
    ever second-guessing Katie's own resolution.
    """
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


def _wide_fallback_technique_search(
    job_name: str, alg_weight: "float | None", alg_pallets: "int | None", alg_pcs: "int | None",
    days_back: int = 90, query_timeout: "int | None" = 15,
) -> "tuple[dict | None, list[dict]]":
    """
    Live Technique search across `days_back` days (default 90) for a trip or manifest
    whose suffix matches job_name — the same two-tier trip-then-manifest suffix logic
    as the normal-window match in _process_invoice_csv() (strategies 2/2b), just
    against a much wider date range.

    Shared by two callers with different budget constraints (2026-07-22): the
    on-demand retry-match route (POST /api/bols/{id}/retry-match), which has this
    request's full ~29s ceiling to itself and passes query_timeout=None, and the
    frontend's automatic follow-up call to that same route right after an invoice
    upload creates a stub — also its own isolated request, also uncapped. Nothing
    calls this function with anything left over in the same request anymore (the
    upload-time inline call was removed 2026-07-22 — see _process_invoice_csv() —
    because sharing a request's budget with everything else already done in that
    request was the actual root cause of invoices that matched instantly on manual
    retry but not on upload). query_timeout stays parameterized (default 15) rather
    than deleted outright in case a future caller ever needs to share a budget again.

    Returns (best, all_candidates) — best is the winning manifest dict (as returned by
    get_technique_data(), with technique_weight/pallets/pcs populated), or None if
    nothing matches even in the wide window. all_candidates is every manifest that
    shared the search suffix (best included, weights populated on all of them, empty
    if best is None) — retry_match_invoice() uses this to persist the losing siblings
    of an ambiguous trip as their own records (2026-07-22), not just the winner, since
    nothing else populates them now that the old daily bulk pull is gone (see its
    removal note) — without this, GET /api/bols/{id}/trip-manifests and reassign-invoice
    would have no sibling data to compare/reassign against.
    """
    from backend.data_layer import get_technique_data, get_manifest_weights

    try:
        wide_manifests = _dedupe_technique_rows(
            get_technique_data(days_back=days_back, query_timeout=query_timeout)
        )

        # Same "how many manifests does this trip have" count is_ambiguous_trip is based
        # on everywhere else -- records created via this wide fallback previously never
        # set it at all (always defaulted False), so a genuinely ambiguous trip found only
        # here could never show the ~UNVERIFIED badge.
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
            return None, []
        if len(candidates) == 1:
            candidates[0]["_trip_manifest_count"] = trip_manifest_counts.get(candidates[0].get("technique_trip"), 0)
            return candidates[0], candidates

        # Multiple manifests share this suffix in the wide window — score by closeness
        # to the invoice's own billed quantities instead of taking an arbitrary one.
        # Weights are populated on every candidate here (not just the winner) so the
        # caller can persist siblings with real technique_weight/pallets/pcs too.
        score_weights = get_manifest_weights([c["manifest"] for c in candidates], query_timeout=query_timeout)
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
        return best, candidates
    except Exception as exc:
        # A hung/slow on-prem query here used to guarantee an ungraceful Lambda kill
        # (bare HTTP 500, no traceback -- confirmed 2026-07-21 via CloudWatch on a real
        # invoice upload) since nothing caught it. Treat any failure the same as "no
        # match in the wide window either" -- retry_match_invoice() already handles a
        # None return by reporting "not found" rather than propagating the failure, and
        # the stub it was called on is left untouched, so a later manual retry (or the
        # next automatic one, if this was called from the frontend's post-upload retry)
        # can simply try again.
        logger.warning(
            "[INVOICE WIDE FALLBACK] live Technique search failed for suffix '%s' "
            "(days_back=%d): %s — treating as no match.",
            job_name, days_back, exc,
        )
        return None, []


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
    """
    Apply one parsed invoice's data to one already-matched record (dict in mock
    mode, BOLRecord in live mode): conflict detection, invoice-number merge,
    amount additive across multiple Z-invoices per trip, access_prog recompute,
    diff computation. Extracted out of _process_invoice_csv() so it can be called
    once per record when Strategy 2 (Job Name as trip suffix) fans out to several
    manifests sharing one trip, instead of only ever handling a single match.
    Returns {"matched_trip", "matched_manifest", "conflict"}.
    """
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
            # Only classify the record on a genuinely new match -- a duplicate
            # re-upload of an already-recorded invoice (already_done) tells us
            # nothing new about what kind of record this is, and blindly
            # resetting match_strategy here would erase a real prior
            # classification like "prophecy_bol" in favor of "invoice_number"
            # (which just describes how THIS re-upload was looked up, not what
            # the record actually is) -- breaking anything that branches on it,
            # e.g. POST /api/admin/recompute-access-prog's Prophecy detection.
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
            # Only classify the record on a genuinely new match -- a duplicate
            # re-upload of an already-recorded invoice (already_done) tells us
            # nothing new about what kind of record this is, and blindly
            # resetting match_strategy here would erase a real prior
            # classification like "prophecy_bol" in favor of "invoice_number"
            # (which just describes how THIS re-upload was looked up, not what
            # the record actually is) -- breaking anything that branches on it,
            # e.g. POST /api/admin/recompute-access-prog's Prophecy detection.
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
    # Per-zip3 $ actually billed where this invoice's own minimum-charge floor fired — feeds
    # the self-updating alg_tariff_rates reconciliation in _apply_access_prog_calc().
    alg_min_charge_by_zip3: dict[str, float] = ctx["alg_min_charge_by_zip3"]
    # Whole-invoice blended $/cwt (freight only, FSC excluded) — the fallback rate when
    # per-zone joins can't cover enough of our load's weight.
    alg_blended_rate: Optional[float] = (
        round(ctx["alg_freight_total"] / (total_weight / 100.0), 4)
        if ctx.get("alg_freight_total") and total_weight else None
    )

    if not settings.USE_MOCK_DATA:
        from backend.data_layer import get_tariff_rate as _get_tariff_rate
        from backend.data_layer import get_current_diesel_price, get_fsc_rate as _get_fsc_rate
        if fsc_rate_val is not None:
            # The invoice carries its own FSC rate (every real ALG CSV does) — that
            # always wins, so don't burn time on the EIA fallback lookup at all.
            # This call used to cost ~20s per file while the VPC's DNS was broken.
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

    # 2. Job Name as trip suffix. One trip can have several distinct manifests —
    # collect every BOLRecord sharing this trip suffix, then resolve to the single
    # closest one by comparing quantities (weight/pallets/pcs) against the invoice's
    # own billed quantities. The invoice's order number keys to the trip, not any
    # one manifest, so when a trip has multiple manifests we can't tell which one
    # it's for from Job Name alone — but the manifest whose own numbers are closest
    # to what ALG billed is almost certainly the right one.
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
                # Katie has already resolved this ambiguity herself (created the real
                # Prophecy BOL for exactly one manifest on this trip via her SID-export
                # flow) — trust that over any quantity-closeness guess.
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
                # Some ALG invoices bill the whole trip, not one manifest (confirmed
                # live: Z558228 billed 19,076 lbs against manifests of 18,138 + 1,048).
                # Score the combined totals of every manifest on the trip as one more
                # candidate alongside each individual manifest — whichever fits the
                # invoice's quantities best wins. If more than one candidate is already
                # resolved (Katie's created real BOLs for two separate manifests on this
                # trip), score only among those — an unresolved manifest never outscores
                # one Katie has already confirmed.
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
                    # Trip-level invoice: attach to the primary manifest (prefer one
                    # that already has a BOL, else the heaviest), remember the trip
                    # totals so diffs get computed against them after the match applies.
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
            # 2b. No trip-suffix match at all — the invoice's Job Name may instead reflect
            # the MANIFEST number rather than the trip number (issue #65). A trip and its
            # manifest are genuinely different numbers (e.g. Trip TEC_T_0109878 vs Manifest
            # TEC_M_0228920), so an invoice keyed to the manifest would never match Strategy
            # 2's trip-suffix search above. No trip-sum synthetic candidate here — summing
            # several *different* manifests that only coincidentally share a matched suffix
            # doesn't mean the invoice covers all of them, unlike manifests of the same trip.
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

    # Note (2026-07-22): a live 90-day wide-fallback search used to run inline here
    # (step 4b) before giving up and creating a stub — removed because sharing this
    # request's budget with everything already done in it (CSV parsing, and for a
    # folder/email batch, every prior invoice in the same request) was the actual
    # root cause of invoices that matched instantly on a manual retry-match click but
    # not on upload. Every non-instant miss now becomes a stub immediately (below),
    # and the frontend fires an automatic POST /api/bols/{id}/retry-match — the same
    # wide search, in its own isolated request with the full budget to itself — right
    # after the upload/poll response comes back. See _wide_fallback_technique_search()
    # and retry_match_invoice().

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
        # Trip-level invoice: the quantity diffs _apply_invoice_match computed compare
        # against the primary manifest alone — recompute them against the trip's
        # combined totals, which is what this invoice actually bills, and leave an
        # explanatory note on every record involved.
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
    """
    Upload an ALG invoice CSV (Z-number format from Tanya).

    invoice_folder_name — the sender's dated folder name (e.g. "Tania 6-25-2026  4-16PM"),
      passed when the user selects a whole folder in the frontend. Parsed with the same
      _parse_invoice_folder_name() used by poll_invoice_folder, so sender metadata matches
      regardless of which path the CSV came in through.

    pdf_file — optional companion PDF (same Z-number stem as the CSV), when the frontend's
      folder walk finds one alongside it. Stored in S3 (INVOICE_S3_BUCKET) keyed by
      Z-number so GET /api/invoices/{z}/file can serve it back without needing
      INVOICE_FOLDER/UNC access — Lambda has no route to the on-prem file share, but
      S3 it can reach directly.

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
            # Doesn't match the expected "Name M-D-YYYY H-MMAM" shape — use the raw
            # folder name as-is rather than leaving sender blank (issue #67). Any CSV
            # uploaded from the same folder shares this identical string, so records
            # stay grouped/batched together and filterable via the existing sender
            # substring search even when the folder name doesn't parse.
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


@app.post("/api/invoices/merge-batch-pdfs", tags=["Invoices"])
def merge_batch_pdfs(body: dict, db: Session = Depends(get_db)):
    """
    Merge and store the combined invoice PDF for one upload batch — every
    record sharing the given invoice_email_sender. Called by the frontend once
    after a whole folder's worth of per-file /api/invoices/upload calls
    finishes, so the merge happens a single time per batch rather than being
    redone on every "Download Invoices" click. Safe to call again later (e.g.
    after a stub resolves and gains its own PDF) — always re-merges from
    whatever's currently locatable.
    """
    sender = (body.get("sender") or "").strip()
    if not sender:
        raise HTTPException(status_code=400, detail="sender is required")
    return _merge_and_store_batch_pdf(sender, db)


@app.get("/api/invoices/batch-pdf", tags=["Invoices"])
def get_batch_pdf(sender: str, db: Session = Depends(get_db)):
    """
    Serve the merged invoice PDF for one upload batch (every record sharing
    this invoice_email_sender). Serves the precomputed merge stored by
    POST /api/invoices/merge-batch-pdfs when one exists (fast path — no
    re-merging on every click); otherwise merges on the fly from whatever PDFs
    can currently be located and caches the result for next time — covers
    batches uploaded before this endpoint existed, or where the merge-on-
    upload step failed or was skipped (e.g. poll_invoice_folder-ingested
    invoices with no S3-stored companion PDF, only INVOICE_FOLDER's copy).
    """
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
    """
    Serve the original invoice file for a given Z-number, preferring the
    human-readable PDF ALG sends (falls back to CSV if no PDF exists, e.g.
    mock/test data).

    Checks S3 (INVOICE_S3_BUCKET) first — the PDF a companion upload stored there,
    which Lambda can actually reach (unlike the on-prem UNC share). Falls back to
    INVOICE_FOLDER (live) or backend/test_data/ (mock), including one level of dated
    sender subfolders (e.g. "Tania 6-25-2026  4-16PM/") created by poll_invoice_folder,
    for CSVs or any invoice uploaded before S3 storage existed.
    """
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
            if parsed:
                sender_str, sent_at = parsed
                logger.info("[POLL-FOLDER] Subfolder '%s' → sender='%s'", entry, sender_str)
            else:
                # Use the raw subfolder name as-is rather than leaving sender blank
                # (issue #67) — see matching comment in upload_alg_invoice().
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

    # Best-effort: refresh each affected sender's merged batch PDF so
    # "Download Invoices" doesn't need an on-the-fly merge next time. These
    # PDFs were never uploaded through this app (poll_invoice_folder only ever
    # reads the CSV, not a companion pdf_file), so this relies entirely on
    # _fetch_invoice_pdf_bytes()'s INVOICE_FOLDER fallback finding them still
    # sitting on the shared drive next to their CSV.
    senders_touched = {sender_str for _, _, sender_str, _ in file_queue if sender_str}
    for sender in senders_touched:
        try:
            _merge_and_store_batch_pdf(sender, db)
        except Exception as exc:
            logger.error("[POLL-FOLDER] Batch PDF merge failed for sender '%s': %s", sender, exc)

    return {"found": len(file_queue), "processed": results, "message": msg}


@app.post("/api/admin/fix-duplicate-invoice-matches", tags=["Admin"])
def fix_duplicate_invoice_matches(db: Session = Depends(get_db)):
    """
    One-time backfill for the old Strategy 2 bug: before _closest_technique_match()
    existed, an invoice matching several manifests on one trip suffix was applied to
    EVERY one of them with the same full amount/weight/pallets/pcs, instead of the
    single closest-matching manifest. The bug's signature is unambiguous — the exact
    same invoice_number on two or more separate records, which never happens
    otherwise (a second Z-invoice for the same manifest is comma-joined onto one
    record's invoice_number, not written to a second record).

    For each such group, re-scores every member against the group's own stored
    alg_weight/alg_pallets/alg_pcs (identical across the group, since that's exactly
    what the bug copied everywhere) using the same _closest_technique_match() logic
    the fixed matching code now uses. The best-scoring member is left untouched; every
    other member is reverted to a clean unmatched state (as if that invoice had never
    matched it) so it goes back into the normal pending queue for a real match later.
    """
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


def _recompute_access_prog_for_record(rec: "BOLRecord", folder: "str | None") -> str:
    """
    Re-locate and re-parse `rec`'s own invoice CSV to recompute access_prog/cost_pct/
    cost_calc_detail. ALG's per-zone rate/FSC context isn't stored anywhere else — only
    parsed transiently from the invoice CSV — so there's no way to redo this math without
    the original file. Extracted 2026-07-22 from recompute_access_prog() (its original,
    single-purpose caller) to share with reassign_invoice(), which has the exact same
    problem when an invoice moves to a different manifest: the new manifest's access_prog
    was never being computed at all, only cost_pct naively recomputed from whatever stale
    (usually null) access_prog the manifest already had.

    Diesel price is fetched lazily, only if this invoice's own CSV has no FSC rate — same
    "don't burn time on the EIA fallback lookup" pattern _process_invoice_csv() uses,
    important here since reassign_invoice() calls this inline in a user-facing request.

    Returns "ok", "no_file" (folder unset/missing, record has no invoice, or its CSV
    can't be found), or "no_own_data" (file found, but _apply_access_prog_calc() still
    couldn't compute a cost — e.g. no live pallet data for this manifest). Mutates `rec`
    in place; caller is responsible for db.commit().
    """
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

    # See _finish_resolving_stub()'s identical fix: stored match_strategy can go
    # stale (silently overwritten pre-2026-07-20), so route on manifest/bol_number
    # structurally instead of trusting the stored classification. Without this, a
    # corrupted row here doesn't just skip -- _apply_access_prog_calc() finds no
    # own pallet data via either path and sets rec.access_prog to None in place,
    # which the caller's db.commit() would persist, silently wiping a previously-
    # correct value (not merely a no-op skip).
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
    """
    Backfill Calculated Cost (access_prog) for existing matched records using the
    corrected formula: our own weight/pallets/pieces x ALG's own invoiced per-zone rate.
    Records whose original file can no longer be found, or for which we have no own
    pallet data available, are left untouched and reported separately rather than
    guessed at. See _recompute_access_prog_for_record() for the actual recompute logic.
    """
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


@app.get("/api/bols/{record_id}/cost-breakdown", tags=["Admin"])
def get_cost_breakdown(record_id: uuid.UUID, db: Session = Depends(get_db)):
    """
    Read-only per-pallet breakdown of how Calculated Cost was computed for one record —
    reads the `cost_calc_detail` JSON stored on the record at real-calculation time
    (see _apply_access_prog_calc()'s `detail` param, populated at every real call site:
    invoice upload, stub resolution, Wolf/311 stub creation, and the
    recompute-access-prog backfill).

    Rewritten 2026-07-21: this route used to re-locate and re-parse the record's own
    invoice CSV from INVOICE_FOLDER on every call, which only ever worked in local dev —
    INVOICE_FOLDER is a Windows UNC share the deployed Lambda has no env var for and can
    never mount regardless, so this route 404'd for every record, always, on the live app.
    Storing the breakdown once at calc time instead of re-deriving it on demand means this
    now works identically in local dev and on the deployed Lambda, and needs no live query
    or file access at all.
    """
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

    Weight/pallets/pieces (step 1) always refresh regardless of BOL status —
    a record can still need a weight correction after its BOL already exists.
    Only step 2 (the Prophecy BOL check) is skipped once needs_sid_export is
    already False, since there's nothing left to check for.

    Once a record has a bol_number, step 1 switches its weight source from
    get_manifest_weights() to get_manifest_weights_from_sid() — the same
    manifest-keyed pallet query behind the Prophecy SID export Katie's own
    process already relies on, so a post-BOL refresh matches her own numbers
    exactly. Before a BOL exists, get_manifest_weights() stays the source
    (unambiguous, cheaper); see CLAUDE.md for why this isn't a blanket swap.
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

    from backend.data_layer import get_technique_data, get_manifest_weights, get_manifest_weights_from_sid

    messages = []
    updated = False

    # (1) Refresh weight/pallets/pieces — always runs. Once a BOL exists, prefer
    # the SID-export query for exact consistency with what Katie's own Prophecy
    # import already used.
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

    # (2) Check BOL status from Technique/ShipperPlus (Query A) — only meaningful
    # for a record that doesn't have a BOL yet.
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
    """
    On-demand retry for one stuck invoice_only stub: check a wide (90-day) Technique
    window immediately. Shares _wide_fallback_technique_search() with the automatic
    post-upload retry the frontend now fires right after every new stub is created
    (see _process_invoice_csv()) — one search implementation instead of two that could
    silently drift apart. query_timeout=None here (unlike the 15s cap used when this
    search shares a request's budget with other work): this endpoint's live query isn't
    stacked onto anything else in the same request, so it has the full ~29s ceiling to
    itself and comfortably fits the 90-day query alone (measured ~13s).
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

    wide_match, all_candidates = _wide_fallback_technique_search(
        job_name_s, float(stub.alg_weight or 0), stub.alg_pallets, stub.alg_pcs,
        query_timeout=None,
    )
    if wide_match is None:
        return {"matched": False, "message": "Still not found in Technique (checked last 90 days)."}

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

    # Persist this trip's other manifests too, going forward (2026-07-22) — nothing
    # else creates them now that the old daily bulk pull is gone (removed in Phase 4),
    # so without this, GET /api/bols/{id}/trip-manifests and reassign-invoice would
    # have no sibling data to compare/reassign against on an ambiguous trip. Technique-
    # side data only, no invoice — same shape a genuinely un-invoiced manifest gets.
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


@app.get("/api/export/invoice-pdfs", tags=["Export"])
def export_invoice_pdfs(invoice_numbers: str):
    """
    Merge and download all invoice PDFs for the given comma-separated Z-numbers.
    Fetches from S3 (if configured) with INVOICE_FOLDER as fallback. Skips any
    Z-number whose PDF can't be located rather than failing the whole batch.
    Returns 404 if no PDFs were found at all.
    """
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
    """
    Generate CSV of approved records and email to Mary + Katie.
    Email failure is a soft failure — export still succeeds with email_sent=False.
    """
    target = body.export_date or date.today()

    if settings.USE_MOCK_DATA:
        # In mock mode return all approved records regardless of date —
        # mock data doesn't represent real daily batches.
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


# ---------------------------------------------------------------------------
# AWS Lambda entrypoint (Stage 1 — container image deployment)
# ---------------------------------------------------------------------------
from mangum import Mangum  # noqa: E402

handler = Mangum(app)
