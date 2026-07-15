"""
Seed static rate tables: tariff_rates and fuel_surcharge_rates.

Run once (or re-run to refresh after a rate update):
    python -m backend.seed_rates

Override file paths:
    python -m backend.seed_rates --tariff PATH --fsc PATH

Requires a live PostgreSQL connection (USE_MOCK_DATA does not affect this
script — it writes directly to the database regardless).
"""
import argparse
import csv
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import text

from backend.database import engine, SessionLocal
from backend.models import Base, TariffRate, FuelSurchargeRate, AlgTariffRate

DEFAULT_TARIFF_CSV = Path(
    r"c:\nikhilm\billing-freight-automation"
    r"\SG360_Romeoville Letters-Flats Tariff_Inbounds Included_Effective 04-01-2026.csv"
)
DEFAULT_FSC_XLSX = Path(
    r"c:\nikhilm\billing-freight-automation"
    r"\SG360_ALG Worldwide Logistics FSC Matrix_06.01.2026.xlsx"
)
DEFAULT_ALG_TARIFF_CSV = Path(
    r"c:\nikhilm\billing-freight-automation\ALG5_2026_tariff_rates.csv"
)

TARIFF_EFFECTIVE = date(2026, 4, 1)
FSC_EFFECTIVE = date(2026, 6, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_dec(value, default=None):
    if value is None or str(value).strip() == "":
        return default
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        return default


def _parse_yyyymmdd(value):
    s = str(value).strip() if value else ""
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return None


def _extract_zip3(ep_zip: str) -> str:
    """'3-d 060' → '060'"""
    return ep_zip.replace("3-d", "").replace("3-D", "").strip()


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_tariff_rates(tariff_path: Path, db) -> int:
    """
    Parse the SG360 Letters-Flats Tariff CSV and replace all tariff_rates rows.

    CSV layout:
      Row 1: 18 column headers
      Rows 2-5: metadata (Name=, OriginZIP=, OriginText=, DropShipFileDate=)
      Rows 6-258: one SCF facility per row (Facility Type = "SCF")

    Only rows with Facility Type == "SCF" and Ignore flag == "N" are active
    rate rows. Rows marked "Y" are stored but excluded from lookups.
    """
    db.execute(text("DELETE FROM tariff_rates"))

    records = []
    with open(tariff_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            facility = row.get("Facility Type", "").strip()
            # Metadata rows (Name=, OriginZIP=, etc.) have no valid Facility Type
            if facility != "SCF":
                continue

            ep_zip = row.get("EP ZIP", "").strip()
            records.append(TariffRate(
                ep_zip=ep_zip,
                ep_zip3=_extract_zip3(ep_zip),
                ep_text=row.get("EP Text", "").strip() or None,
                origin_zip="60095",
                ignore_flag=(row.get("Ignore (Y / N)", "N").strip().upper() == "Y"),
                distance_miles=_to_dec(row.get("Distance (miles)")),
                cost_per_100lb=_to_dec(row.get("Cost per 100 lb ($)"), default=Decimal("0")),
                minimum_freight=_to_dec(row.get("Minimum Freight ($)"), default=Decimal("0")),
                drop_ship_site_key=row.get("Drop Ship Site Key", "").strip() or None,
                expiration_date=_parse_yyyymmdd(row.get("Drop Ship Expiration Date")),
                effective_date=TARIFF_EFFECTIVE,
            ))

    db.bulk_save_objects(records)
    db.commit()
    return len(records)


def load_fsc_rates(fsc_path: Path, db) -> int:
    """
    Parse the ALG Worldwide FSC Matrix Excel and replace all fuel_surcharge_rates rows.

    Sheet "Direct FSC" layout:
      Rows 1-8: headers / branding (ALG WORLDWIDE LOGISTICS, FUEL SURCHARGE MATRIX)
      Row 9: column headers (Fuel Price - At Least, Fuel Price - Up to, FSC)
      Rows 10-144: 135 price-band rows

    FSC unit: decimal multiplier (e.g., fsc_amount=0.365 → 36.5% surcharge).
    The Excel stores 0.365, not 36.5. Applied as: access_prog = base_tariff × (1 + fsc_amount).
    """
    try:
        import openpyxl
    except ImportError:
        raise SystemExit(
            "openpyxl is required to seed FSC rates.\n"
            "Install it: pip install openpyxl>=3.1.0"
        )

    db.execute(text("DELETE FROM fuel_surcharge_rates"))

    wb = openpyxl.load_workbook(fsc_path, read_only=True, data_only=True)
    ws = wb["Direct FSC"]

    HEADER_ROW = 9  # 1-indexed; data starts at row 10

    records = []
    for row in ws.iter_rows(min_row=HEADER_ROW + 1, values_only=True):
        fuel_min, fuel_max, fsc = row[0], row[1], row[2]
        if fuel_min is None or fsc is None:
            continue
        records.append(FuelSurchargeRate(
            fuel_price_min=Decimal(str(fuel_min)),
            fuel_price_max=Decimal(str(fuel_max)),
            fsc_amount=Decimal(str(fsc)),
            carrier="ALG Worldwide Logistics",
            effective_date=FSC_EFFECTIVE,
        ))

    wb.close()
    db.bulk_save_objects(records)
    db.commit()
    return len(records)


def load_alg_tariff_rates(alg_tariff_path: Path, db) -> int:
    """
    Parse the Access-sourced ALG5_2026 rate export (columns: tariff_id, destination,
    rate1, mc1 — no header row) and replace all alg_tariff_rates rows.

    destination is the exact Locations.AccountNumber-format code (e.g. "SCF606"),
    confirmed 2026-07-15 to match our own pallet data's Dest_ID/destination_id exactly —
    no zip3 derivation needed for this table, unlike tariff_rates.
    """
    db.execute(text("DELETE FROM alg_tariff_rates"))

    records = []
    with open(alg_tariff_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 4 or not row[0].strip():
                continue
            tariff_id, dest_id, rate1, mc1 = (row[0].strip(), row[1].strip().upper(),
                                               _to_dec(row[2]), _to_dec(row[3]))
            if dest_id and rate1 is not None and mc1 is not None:
                records.append(AlgTariffRate(
                    tariff_id=tariff_id,
                    dest_id=dest_id,
                    rate1=rate1,
                    mc1=mc1,
                ))

    db.bulk_save_objects(records)
    db.commit()
    return len(records)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Seed tariff_rates and fuel_surcharge_rates tables from source files."
    )
    parser.add_argument(
        "--tariff",
        default=str(DEFAULT_TARIFF_CSV),
        metavar="PATH",
        help="Path to the Letters-Flats Tariff CSV (default: %(default)s)",
    )
    parser.add_argument(
        "--fsc",
        default=str(DEFAULT_FSC_XLSX),
        metavar="PATH",
        help="Path to the ALG FSC Matrix Excel (default: %(default)s)",
    )
    parser.add_argument(
        "--alg-tariff",
        default=str(DEFAULT_ALG_TARIFF_CSV),
        metavar="PATH",
        help="Path to the Access-sourced ALG5_2026 rate export (default: %(default)s)",
    )
    args = parser.parse_args()

    tariff_path = Path(args.tariff)
    fsc_path = Path(args.fsc)
    alg_tariff_path = Path(args.alg_tariff)

    missing = [p for p in (tariff_path, fsc_path, alg_tariff_path) if not p.exists()]
    if missing:
        for p in missing:
            print(f"ERROR: file not found: {p}")
        raise SystemExit(1)

    print("Creating / verifying database tables...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        print(f"Loading tariff rates from {tariff_path.name} ...")
        n = load_tariff_rates(tariff_path, db)
        print(f"  {n} rows inserted into tariff_rates.")

        print(f"Loading FSC rates from {fsc_path.name} ...")
        n = load_fsc_rates(fsc_path, db)
        print(f"  {n} rows inserted into fuel_surcharge_rates.")

        print(f"Loading ALG tariff rates from {alg_tariff_path.name} ...")
        n = load_alg_tariff_rates(alg_tariff_path, db)
        print(f"  {n} rows inserted into alg_tariff_rates.")

        print("Done.")
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
