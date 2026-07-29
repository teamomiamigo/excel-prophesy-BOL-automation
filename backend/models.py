import uuid
import enum
from datetime import datetime, date
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    String, Integer, Numeric, Date, DateTime, Boolean,
    ForeignKey, Text, Enum as SAEnum, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pydantic import BaseModel, Field, ConfigDict

from backend.database import Base


# ---------------------------------------------------------------------------
# Static rate tables (seeded via backend/seed_rates.py — not mock-gated)
# ---------------------------------------------------------------------------

class TariffRate(Base):
    """drop-ship tariff rates, one row per USPS SCF. seeded via `python -m backend.seed_rates`.
    lookup key: ep_zip3 (3-digit SCF zone); last-resort fallback below alg_tariff_rates"""
    __tablename__ = "tariff_rates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ep_zip: Mapped[str] = mapped_column(String(30), nullable=False)          # "3-d 060"
    ep_zip3: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # "060" — query key
    ep_text: Mapped[Optional[str]] = mapped_column(String(200))              # "SPRINGFIELD LDC..."
    origin_zip: Mapped[str] = mapped_column(String(10), default="60095")     # Romeoville origin
    ignore_flag: Mapped[bool] = mapped_column(Boolean, default=False)        # Y=excluded from tariff
    distance_miles: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    cost_per_100lb: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    minimum_freight: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    drop_ship_site_key: Mapped[Optional[str]] = mapped_column(String(30))
    expiration_date: Mapped[Optional[date]] = mapped_column(Date)
    effective_date: Mapped[Optional[date]] = mapped_column(Date)


class AlgTariffRate(Base):
    """ALG's own per-destination rate table (tariff_id ALG5_2026), one-time exported from
    ShipperPlus. keyed by the exact destination code (e.g. "SCF606") -- exact match, no zip3
    slicing needed, unlike the older TariffRate card. seeded via `python -m backend.seed_rates`"""
    __tablename__ = "alg_tariff_rates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tariff_id: Mapped[str] = mapped_column(String(30), nullable=False, default="ALG5_2026")
    dest_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    rate1: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    mc1: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)


class FuelSurchargeRate(Base):
    """ALG's fuel surcharge matrix, 135 diesel-price bands -> FSC amount (decimal fraction,
    NOT the /100 the docstring used to say). seeded via `python -m backend.seed_rates`"""
    __tablename__ = "fuel_surcharge_rates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fuel_price_min: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    fuel_price_max: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    fsc_amount: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    carrier: Mapped[str] = mapped_column(String(100), default="ALG Worldwide Logistics")
    effective_date: Mapped[Optional[date]] = mapped_column(Date)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class BOLStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    FLAGGED = "flagged"


class ActionType(str, enum.Enum):
    APPROVED = "approved"
    FLAGGED = "flagged"
    REOPENED = "reopened"
    DO_NOT_PAY = "do_not_pay"


# ---------------------------------------------------------------------------
# SQLAlchemy ORM models
# ---------------------------------------------------------------------------

class BOLRecord(Base):
    """one row per freight reconciliation record. no bol_number until Katie creates one in
    Prophecy; identified before then by technique_trip + manifest + invoice_number"""
    __tablename__ = "bol_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Prophecy — nullable because BOL is created after data loads
    bol_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    # Visual Mail / Technique (AWP-SQL-PROD)
    # technique_trip is nullable: blank rows in the Excel belong to the trip above
    technique_trip: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # CM_ prefix = comingle manifest (future Module 2)
    manifest: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    technique_weight: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    technique_pallets: Mapped[int] = mapped_column(Integer, nullable=False)
    technique_pcs: Mapped[int] = mapped_column(Integer, nullable=False)

    # ALG invoice email (from Tanya each morning)
    invoice_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    invoice_email_sender: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    invoice_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    inv_job_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    carrier: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # ALG invoice quantities — populated on CSV upload; null until then
    alg_weight: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    alg_pallets: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    alg_pcs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Cost columns (three sources)
    prop_reship: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    access_prog: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    # Stored as ratio (0.9881 = 98.81%) for fast queries; computed: amount / access_prog
    # (reverted 2026-07-21 — was access_prog/amount 2026-07-16 to 2026-07-21)
    cost_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6), nullable=True)
    # Rate breakdown for tooltip: base_tariff × (1 + fsc_pct) = access_prog
    base_tariff: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    fsc_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6), nullable=True)
    # ALG's own reported FSC for this invoice, used instead of an EIA-diesel-derived guess
    alg_fsc_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6), nullable=True)
    alg_fsc_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    # true when a pallet's rate had to fall back to nearest-zone matching
    tariff_zone_approximate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # true when our own weight data was unavailable and access_prog fell back to ALG's self-reported weight
    weight_source_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # true when a pallet's minimum-freight floor couldn't be confirmed against alg_tariff_rates.mc1
    min_charge_uncertain: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # JSON list of per-pallet cost-calc detail dicts, stored at calc time -- powers the cost-breakdown route
    cost_calc_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # true when this manifest's trip had more than one manifest in the live technique search that matched it
    is_ambiguous_trip: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Quantity comparisons — Technique vs ALG invoice (populated on upload)
    prophecy_weight: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    weight_diff: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    prophecy_pallets: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pallet_diff: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prophecy_pcs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pcs_diff: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Workflow state
    status: Mapped[BOLStatus] = mapped_column(
        SAEnum(BOLStatus), nullable=False, default=BOLStatus.PENDING
    )
    flag_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Record type and export tracking
    # needs_sid_export: True = Type A (no BOL yet, must import SID into Prophecy)
    #                   False = Type B (load_id > 0, BOL already exists)
    needs_sid_export: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    no_invoice: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_third_party: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_do_not_pay: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # manually dismissed as a bad/duplicate sibling manifest; never set on a record with an invoice_number.
    # reversible in principle but no undo route exists yet
    is_dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # clears the ~UNVERIFIED badge for a mismatch with no ambiguous trip to compare against. no undo route
    mismatch_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    match_strategy: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    sid_exported_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    accounting_exported_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    approval_history: Mapped[list["ApprovalHistory"]] = relationship(
        "ApprovalHistory", back_populates="bol", cascade="all, delete-orphan"
    )


class ApprovalHistory(Base):
    """Full audit log — one row per approve/flag action on a record."""
    __tablename__ = "approval_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bol_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bol_records.id"), nullable=False, index=True
    )
    action: Mapped[ActionType] = mapped_column(SAEnum(ActionType), nullable=False)
    performed_by: Mapped[str] = mapped_column(String(100), nullable=False, default="coordinator")
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    performed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    bol: Mapped["BOLRecord"] = relationship("BOLRecord", back_populates="approval_history")


class User(Base):
    """Stubbed for future authentication module."""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    role: Mapped[str] = mapped_column(String(50), default="coordinator")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class BOLSummary(BaseModel):
    """All fields needed by the dashboard table and mutation responses."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bol_number: Optional[int] = None
    technique_trip: Optional[str] = None
    manifest: Optional[str] = None
    technique_weight: Decimal
    technique_pallets: int
    technique_pcs: int
    invoice_number: Optional[str] = None
    inv_job_number: Optional[str] = None
    invoice_email_sender: Optional[str] = None
    invoice_sent_at: Optional[datetime] = None
    prop_reship: Optional[Decimal] = None
    access_prog: Optional[Decimal] = None
    amount: Optional[Decimal] = None
    cost_pct: Optional[Decimal] = None
    base_tariff: Optional[Decimal] = None
    fsc_pct: Optional[Decimal] = None
    alg_fsc_pct: Optional[Decimal] = None
    alg_fsc_cost: Optional[Decimal] = None
    tariff_zone_approximate: bool = False
    weight_source_fallback: bool = False
    min_charge_uncertain: bool = False
    is_ambiguous_trip: bool = False
    prophecy_weight: Optional[Decimal] = None
    weight_diff: Optional[Decimal] = None
    prophecy_pallets: Optional[int] = None
    pallet_diff: Optional[int] = None
    prophecy_pcs: Optional[int] = None
    pcs_diff: Optional[int] = None
    alg_weight: Optional[Decimal] = None
    alg_pallets: Optional[int] = None
    alg_pcs: Optional[int] = None
    notes: Optional[str] = None
    status: BOLStatus
    flag_reason: Optional[str] = None
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    needs_sid_export: bool = True
    no_invoice: bool = False
    is_third_party: bool = False
    is_do_not_pay: bool = False
    is_dismissed: bool = False
    mismatch_acknowledged: bool = False
    match_strategy: Optional[str] = None
    sid_exported_at: Optional[datetime] = None
    accounting_exported_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ManifestCandidate(BOLSummary):
    """One manifest sharing a trip with an ambiguous-match record, scored against
    the invoice actually attached to that trip — see GET /trip-manifests."""
    score: Optional[float] = None
    is_best_fit: bool = False


class TripManifestsResponse(BaseModel):
    """All manifests sharing one Technique trip, for manual verification of which
    one an ALG invoice really belongs to when is_ambiguous_trip is set."""
    technique_trip: Optional[str] = None
    reference_id: Optional[uuid.UUID] = None
    invoice_number: Optional[str] = None
    invoice_email_sender: Optional[str] = None
    inv_job_number: Optional[str] = None
    amount: Optional[Decimal] = None
    alg_weight: Optional[Decimal] = None
    alg_pallets: Optional[int] = None
    alg_pcs: Optional[int] = None
    candidates: list[ManifestCandidate]


class FlagRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


class ApproveRequest(BaseModel):
    approved_by: str = Field(default="coordinator", max_length=100)


class ExportRequest(BaseModel):
    export_date: Optional[date] = None
    email_recipients: Optional[list[str]] = None


class ExportResponse(BaseModel):
    success: bool
    records_exported: int
    csv_filename: str
    email_sent: bool
    email_recipients: list[str]
    message: str


class HealthResponse(BaseModel):
    status: str
    version: str
    db_online: bool
    mock_mode: bool
