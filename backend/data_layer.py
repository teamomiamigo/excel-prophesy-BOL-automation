"""
Integration layer for the four real data sources.

When USE_MOCK_DATA=False, these functions connect to live systems.
This file is the sole integration boundary — no SQL or parsing logic
lives anywhere else in the codebase.

Connection requirements (install when going live):
    pip install pyodbc sqlalchemy[mssql]

All connections go through AWP-SQL-PROD. TECH, SegGroup, and SQLAPPS3
are linked servers accessible from there — no separate connection strings needed.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _get_tech_prd1_connection():
    """Direct SQL auth connection to SG360-TECH-PRD1 (ShipperPlus host).
    Requires TECH_PRD1_USER and TECH_PRD1_PASSWORD in .env.
    """
    import pyodbc
    from backend.config import settings
    conn_str = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={settings.TECH_PRD1_SERVER};"
        f"DATABASE=ShipperPlus_Segerdahl;"
        f"UID={settings.TECH_PRD1_USER};"
        f"PWD={settings.TECH_PRD1_PASSWORD};"
    )
    return pyodbc.connect(conn_str, timeout=30)


def _get_connection(server: str = "AWP-SQL-PROD", database: str = "VisualMail"):
    """
    Return a pyodbc connection to the given SQL Server instance.
    Uses SQL auth when SQLSERVER_USER/SQLSERVER_PASSWORD are set in .env,
    otherwise falls back to Windows auth (Trusted_Connection=yes).
    """
    import pyodbc
    from backend.config import settings
    if settings.SQLSERVER_USER and settings.SQLSERVER_PASSWORD:
        conn_str = (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={server};"
            f"DATABASE={database};"
            f"UID={settings.SQLSERVER_USER};"
            f"PWD={settings.SQLSERVER_PASSWORD};"
        )
    else:
        conn_str = (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={server};"
            f"DATABASE={database};"
            f"Trusted_Connection=yes;"
        )
    return pyodbc.connect(conn_str, timeout=30)


# ---------------------------------------------------------------------------
# Query 1: Morning pull — trips and manifests for the last N days
# ---------------------------------------------------------------------------

# Full query as provided. Filters for ALG/LSC/ENRU/CHOICE destinations.
# Joins across linked servers: TECH (Live_Orders), SegGroup, SQLAPPS3 (ShipperPlus).
# Returns one row per manifest with trip, pieces, Prophecy pieces, customer, etc.
_TECHNIQUE_QUERY = """
SELECT e.Trip, e.ManifestNumber, e.DespatchDate, e.LocCode, e.Destination,
       e.Pallets, sum(e.Pieces) as VM_Pieces, e.pooled_to_load_id, e.load_id,
       sum(e.Proph_Pieces) as Proph_Pieces, e.Notes, e.TranType, e.Carrier,
       e.CustomerName, e.Notes_3PL
FROM (
    SELECT d.Trip, d.ManifestNumber, d.DespatchDate, d.LocCode, d.Destination,
           d.Pallets, d.UniqueContainerID, d.proph_pallet,
           case when d.Pieces is null then 0 else d.Pieces end as Pieces,
           case when s.pooled_to_load_id is null then 0 else pooled_to_load_id end as pooled_to_load_id,
           case when s.load_id is null then 0 else s.load_id end as load_id,
           case when oh.pieces is null then 0 else oh.pieces end as Proph_Pieces,
           d.Notes, d.TranType, d.Carrier, d.CustomerName,
           case when d.Notes like '%Third Party%' and d.Trantype = 'Prepaid' then '3rd Party' else '' end as Notes_3PL
    FROM (
        SELECT t.Trip, m.ManifestNumber, c.DespatchDate, c.LocCode, Destination,
               Pallets, p.UniqueContainerID, right(p.UniqueContainerID,20) as proph_pallet,
               p.NumberOfCopies as Pieces, c.Notes, c.TranType, c.Carrier, c.CustomerName
        FROM (
            SELECT b.DespatchID, b.DespatchDate, b.LocCode, b.Destination,
                   count(b.barcode) as Pallets,
                   case when len(b.DespatchID)=5 then concat('TEC_T_00',b.DespatchID)
                        else concat('TEC_T_0',b.DespatchID) end as Trip,
                   b.Notes, b.TranType, b.Carrier, b.CustomerName
            FROM (
                SELECT a.DespatchID, Max(a.DespatchDate) as DespatchDate, a.barcode,
                       Max(a.LocCode) as LocCode, MAX(a.Destination) as Destination,
                       a.Notes, a.TranType, a.Carrier, a.CustomerName
                FROM (
                    SELECT top 1000000 d.DespatchID, d.DespatchDate,
                           cast(d.Notes as varchar(max)) as Notes, p.barcode,
                           lsl.LocCode, CAST(l.Destination AS VARCHAR(MAX)) as Destination,
                           case when d.DSPIncoTermID = 1 then lt.IncoTermDesc
                                when d.DSPIncoTermID = 2 then lt.IncoTermDesc
                                when d.DSPIncoTermID = 3 then lt.IncoTermDesc end as TranType,
                           d.Haulier as Carrier, t.CustomerName
                    FROM TECH.Live_Orders.dbo.Despatch d
                    INNER JOIN TECH.Live_Orders.dbo.Pallets p ON d.DespatchID = p.DespatchID
                    INNER JOIN TECH.Live_Orders.dbo.OrderHeader oh ON p.OrderNo = oh.OrderNo
                    INNER JOIN TECH.Live_Orders.dbo.JobHeader j ON oh.OrderNo = j.OrderNo
                    INNER JOIN SegGroup.dbo.TECGraphJobCo AS t
                        ON j.JobNo COLLATE DATABASE_DEFAULT = t.JobNo COLLATE DATABASE_DEFAULT
                        AND t.SiteID = 'SC'
                    INNER JOIN TECH.Live_Orders.dbo.LoadDespatch ld ON d.DespatchID = ld.despatchID
                    INNER JOIN TECH.Live_Orders.dbo.LoadSplits ls
                        ON ld.LoadSplitID = ls.LoadSplitID AND ld.OrderNo = ls.OrderNo
                    INNER JOIN TECH.Live_Orders.dbo.Loads l
                        ON ls.LoadID = l.LoadID AND ls.OrderNo = l.OrderNo
                    INNER JOIN TECH.Live_Customers.dbo.LookupSiteLocations lsl ON l.FromLocID = lsl.LocID
                    LEFT JOIN TECH.Live_Estimating.dbo.LookUpIncoTerms lt ON d.DSPIncoTermID = lt.IncoTermID
                    WHERE (d.despatchdate > getdate() - ?)
                      AND (p.barcode is not null)
                      AND (l.Destination LIKE '%LSC%'
                        OR l.Destination LIKE '%ENRU%'
                        OR l.Destination LIKE '%ALG%'
                        OR l.Destination LIKE '%CHOICE%')
                    ORDER BY d.despatchID
                ) a
                GROUP BY a.DespatchID, a.barcode, a.Notes, a.TranType, a.Carrier, CustomerName
            ) b
            GROUP BY b.DespatchID, b.DespatchDate, b.LocCode, b.Destination,
                     b.Notes, b.TranType, b.Carrier, b.CustomerName
        ) c
        INNER JOIN VisualMail.dbo.Trip t ON c.trip = t.trip
        INNER JOIN VisualMail.dbo.Manifest m ON t.ID = m.TripID
        INNER JOIN VisualMail.dbo.Pallet p ON m.ManifestID = p.ID
    ) d
    LEFT JOIN SQLAPPS3.ShipperPlus_Segerdahl.dbo.order_headers oh ON d.proph_pallet = oh.order_id
    LEFT JOIN SQLAPPS3.ShipperPlus_Segerdahl.dbo.shipments s ON oh.shipment_id = s.shipment_id
) e
GROUP BY e.Trip, e.ManifestNumber, e.DespatchDate, e.LocCode, e.Destination,
         e.Pallets, e.pooled_to_load_id, e.load_id, e.Notes, e.TranType,
         e.Carrier, e.CustomerName, e.Notes_3PL
"""


def get_technique_data(days_back: int = 1) -> list[dict]:
    """
    Run Query 1 against AWP-SQL-PROD to get all ALG-destined manifests
    despatched in the last `days_back` days.

    Returns a list of dicts — one per manifest row — with keys:
        technique_trip      str   e.g. "TEC_T_0109878"
        manifest            str   e.g. "TEC_M_0228920"
        despatch_date       date
        loc_code            str
        destination         str
        technique_pallets   int
        technique_pcs       int   VM pieces (from VisualMail)
        pooled_to_load_id   int   from ShipperPlus (0 if null)
        load_id             int   from ShipperPlus (0 if null)
        prophecy_pcs        int   Proph_Pieces from ShipperPlus (0 if not yet linked)
        notes               str | None
        tran_type           str   "Prepaid", "Collect", etc.
        carrier             str
        customer_name       str
        notes_3pl           str   "3rd Party" or ""

    NOTE: Weight is NOT in this query — call get_manifest_weights() with the
    returned manifest numbers to get weight per manifest.
    """
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(_TECHNIQUE_QUERY, (days_back,))
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()
        conn.close()

        results = []
        for row in rows:
            r = dict(zip(columns, row))
            results.append({
                "technique_trip":    r["Trip"],
                "manifest":          r["ManifestNumber"],
                "despatch_date":     r["DespatchDate"],
                "loc_code":          r["LocCode"],
                "destination":       r["Destination"],
                "technique_pallets": int(r["Pallets"]) if r["Pallets"] else 0,
                "technique_pcs":     int(r["VM_Pieces"]) if r["VM_Pieces"] else 0,
                "pooled_to_load_id": int(r["pooled_to_load_id"]) if r["pooled_to_load_id"] else 0,
                "load_id":           int(r["load_id"]) if r["load_id"] else 0,
                "prophecy_pcs":      int(r["Proph_Pieces"]) if r["Proph_Pieces"] else 0,
                "notes":             r["Notes"] or None,
                "tran_type":         r["TranType"] or "",
                "carrier":           r["Carrier"] or "ALG Worldwide",
                "customer_name":     r["CustomerName"] or "",
                "notes_3pl":         r["Notes_3PL"] or "",
            })
        logger.info("[TECHNIQUE] Fetched %d manifests (days_back=%d)", len(results), days_back)
        return results

    except Exception as exc:
        logger.error("[TECHNIQUE] Query failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Query 2: Weight, pieces, pallets per manifest (batch)
# ---------------------------------------------------------------------------

# Original query is per-pallet (one row per pallet). We aggregate here so the
# caller gets one row per manifest: total weight, total pieces, pallet count.
# The WHERE clause accepts multiple manifest numbers via IN (?,...).
_MANIFEST_WEIGHT_QUERY = """
SELECT
    m.ManifestNumber,
    SUM(ROUND(p.Weight, 0))       AS total_weight,
    SUM(p.NumberOfCopies)         AS total_pcs,
    COUNT(p.UniqueContainerID)    AS total_pallets
FROM VisualMail.dbo.Manifest m
INNER JOIN VisualMail.dbo.Pallet p ON p.ID = m.ManifestID
WHERE p.Active = 1
  AND m.ManifestNumber IN ({placeholders})
GROUP BY m.ManifestNumber
"""


def get_manifest_weights(manifest_numbers: list[str]) -> dict[str, dict]:
    """
    Run Query 2 (aggregated) to get weight, pieces, and pallet count for a
    batch of manifests in a single round-trip.

    Returns a dict keyed by ManifestNumber:
        {
          "TEC_M_0228920": {
              "technique_weight":  Decimal("30074.00"),
              "technique_pcs":     343521,
              "technique_pallets": 42,
          },
          ...
        }

    Weight comes from VisualMail.dbo.Pallet.Weight (rounded to 0 decimals).
    This is the only source of weight — it is NOT in get_technique_data().
    """
    if not manifest_numbers:
        return {}

    try:
        conn = _get_connection()
        cursor = conn.cursor()
        placeholders = ",".join(["?"] * len(manifest_numbers))
        query = _MANIFEST_WEIGHT_QUERY.format(placeholders=placeholders)
        cursor.execute(query, manifest_numbers)
        rows = cursor.fetchall()
        conn.close()

        result = {}
        for row in rows:
            manifest_num, weight, pcs, pallets = row
            result[manifest_num] = {
                "technique_weight":  Decimal(str(weight)) if weight is not None else Decimal("0"),
                "technique_pcs":     int(pcs) if pcs else 0,
                "technique_pallets": int(pallets) if pallets else 0,
            }
        logger.info("[MANIFEST_WEIGHT] Fetched weights for %d/%d manifests",
                    len(result), len(manifest_numbers))
        return result

    except Exception as exc:
        logger.error("[MANIFEST_WEIGHT] Query failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Prophecy / ShipperPlus (future — load_id from Query 1 is the link)
# ---------------------------------------------------------------------------

# Via SQLAPPS3 linked server on AWP-SQL-PROD (used when TECH_PRD1 credentials not set)
_PROPHECY_BOL_QUERY = """
SELECT
    COALESCE(NULLIF(s.pooled_to_load_id, 0), s.load_id) AS bol_number,
    SUM(oh.pieces)  AS total_pieces,
    SUM(oh.weight)  AS total_weight,
    SUM(oh.pallets) AS total_pallets
FROM SQLAPPS3.ShipperPlus_Segerdahl.dbo.Shipments AS s
INNER JOIN SQLAPPS3.ShipperPlus_Segerdahl.dbo.order_headers AS oh
    ON s.shipment_id = oh.shipment_id
WHERE s.pooled_to_load_id = ?
   OR (ISNULL(s.pooled_to_load_id, 0) = 0 AND s.load_id = ?)
GROUP BY COALESCE(NULLIF(s.pooled_to_load_id, 0), s.load_id)
"""

# Direct query when connected to SG360-TECH-PRD1 (no 4-part name needed)
_PROPHECY_DIRECT_QUERY = """
SELECT
    COALESCE(NULLIF(s.pooled_to_load_id, 0), s.load_id) AS bol_number,
    SUM(oh.pieces)  AS total_pieces,
    SUM(oh.weight)  AS total_weight,
    SUM(oh.pallets) AS total_pallets
FROM dbo.Shipments AS s
INNER JOIN dbo.order_headers AS oh
    ON s.shipment_id = oh.shipment_id
WHERE s.pooled_to_load_id = ?
   OR (ISNULL(s.pooled_to_load_id, 0) = 0 AND s.load_id = ?)
GROUP BY COALESCE(NULLIF(s.pooled_to_load_id, 0), s.load_id)
"""


def get_prophecy_data(bol_number: int) -> Optional[dict]:
    """
    Fetch weight, pieces, and pallet count for a Prophecy BOL from ShipperPlus.

    Tries direct connection to SG360-TECH-PRD1 first (when TECH_PRD1_USER/PASSWORD
    are set in .env). Falls back to SQLAPPS3 linked server on AWP-SQL-PROD if the
    direct connection fails (e.g. Application user lacks DB access on PRD1).

    Returns:
        {
            "bol_number":       int,
            "prophecy_pcs":     int,
            "prophecy_weight":  Decimal,
            "prophecy_pallets": int,
        }
    Returns None if no ShipperPlus record found for this BOL.
    """
    from backend.config import settings

    def _run_query(conn, query):
        cursor = conn.cursor()
        cursor.execute(query, (bol_number, bol_number))
        row = cursor.fetchone()
        conn.close()
        return row

    def _to_result(row):
        logger.info("[PROPHECY] BOL %s → pcs=%s wt=%s pallets=%s", bol_number, row[1], row[2], row[3])
        return {
            "bol_number":       int(row[0]),
            "prophecy_pcs":     int(row[1]) if row[1] else 0,
            "prophecy_weight":  Decimal(str(row[2])) if row[2] else Decimal("0"),
            "prophecy_pallets": int(row[3]) if row[3] else 0,
        }

    # 1. Try direct connection to SG360-TECH-PRD1 if credentials are configured.
    if settings.TECH_PRD1_USER and settings.TECH_PRD1_PASSWORD:
        try:
            row = _run_query(_get_tech_prd1_connection(), _PROPHECY_DIRECT_QUERY)
            if row:
                return _to_result(row)
            logger.warning("[PROPHECY] No ShipperPlus record on PRD1 for BOL %s — trying SQLAPPS3", bol_number)
        except Exception as exc:
            logger.warning("[PROPHECY] PRD1 connection failed for BOL %s (%s) — falling back to SQLAPPS3", bol_number, exc)

    # 2. Fall back to SQLAPPS3 linked server on AWP-SQL-PROD (Windows auth).
    try:
        row = _run_query(_get_connection(), _PROPHECY_BOL_QUERY)
        if not row:
            logger.warning("[PROPHECY] No ShipperPlus record found for BOL %s", bol_number)
            return None
        return _to_result(row)
    except Exception as exc:
        logger.error("[PROPHECY] SQLAPPS3 query also failed for BOL %s: %s", bol_number, exc)
        return None


# Per-row (not aggregated) — used to independently calculate access_prog for Wolf/311
# loads the same way get_pallet_data_for_manifests() does for Technique trips.
_PROPHECY_PALLET_QUERY = """
SELECT oh.destination_id, oh.destination_zip, oh.weight
FROM SQLAPPS3.ShipperPlus_Segerdahl.dbo.Shipments AS s
INNER JOIN SQLAPPS3.ShipperPlus_Segerdahl.dbo.order_headers AS oh
    ON s.shipment_id = oh.shipment_id
WHERE s.pooled_to_load_id = ?
   OR (ISNULL(s.pooled_to_load_id, 0) = 0 AND s.load_id = ?)
"""

_PROPHECY_PALLET_DIRECT_QUERY = """
SELECT oh.destination_id, oh.destination_zip, oh.weight
FROM dbo.Shipments AS s
INNER JOIN dbo.order_headers AS oh
    ON s.shipment_id = oh.shipment_id
WHERE s.pooled_to_load_id = ?
   OR (ISNULL(s.pooled_to_load_id, 0) = 0 AND s.load_id = ?)
"""


def get_prophecy_pallet_data(bol_number: int) -> list[dict]:
    """
    Fetch per-row destination + weight for a Prophecy BOL from ShipperPlus order_headers —
    the Wolf/311 equivalent of get_pallet_data_for_manifests(), used to independently
    calculate access_prog from SG360's own data instead of ALG's invoiced weight.

    destination_id is SCF/NDC-prefixed (e.g. "SCF080") like VisualMail's Locations.AccountNumber —
    confirmed live to hold that format, though it's sometimes NULL; destination_zip (raw 5-digit
    ZIP) is always populated and used as a fallback for the zip3.

    Returns a list of {"destination_id": str|None, "destination_zip": str, "weight": Decimal}.
    Returns [] if no rows found or the query fails (caller should fall back to ALG's own weight).
    """
    from backend.config import settings

    def _run_query(conn, query):
        cursor = conn.cursor()
        cursor.execute(query, (bol_number, bol_number))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def _to_result(rows):
        return [
            {
                "destination_id": row[0],
                "destination_zip": row[1],
                "weight": Decimal(str(row[2])) if row[2] else Decimal("0"),
            }
            for row in rows
        ]

    if settings.TECH_PRD1_USER and settings.TECH_PRD1_PASSWORD:
        try:
            rows = _run_query(_get_tech_prd1_connection(), _PROPHECY_PALLET_DIRECT_QUERY)
            if rows:
                return _to_result(rows)
            logger.warning("[PROPHECY] No pallet rows on PRD1 for BOL %s — trying SQLAPPS3", bol_number)
        except Exception as exc:
            logger.warning("[PROPHECY] PRD1 pallet query failed for BOL %s (%s) — falling back to SQLAPPS3", bol_number, exc)

    try:
        rows = _run_query(_get_connection(), _PROPHECY_PALLET_QUERY)
        if not rows:
            logger.warning("[PROPHECY] No pallet rows found for BOL %s", bol_number)
            return []
        return _to_result(rows)
    except Exception as exc:
        logger.error("[PROPHECY] SQLAPPS3 pallet query also failed for BOL %s: %s", bol_number, exc)
        return []


# ---------------------------------------------------------------------------
# Tariff / Access rates  (seeded from CSV via backend/seed_rates.py)
# ---------------------------------------------------------------------------

def get_current_diesel_price() -> Optional[float]:
    """
    Fetch the most recent weekly US on-highway diesel retail price from the EIA API.
    Series: EMD_EPD2D_PTE_NUS_DPG (EIA Weekly Retail On-Highway Diesel Prices, US avg).

    Returns the price in $/gal, or None if EIA_API_KEY is not set or the call fails.
    The price is used to look up the FSC band in fuel_surcharge_rates via get_fsc_rate().
    """
    import json
    import urllib.request
    from backend.config import settings

    if not settings.EIA_API_KEY:
        logger.warning(
            "[EIA] EIA_API_KEY not set — FSC skipped, Calc Cost = base tariff only "
            "(understated by ~36%%). Register free at eia.gov/developer and add EIA_API_KEY to .env"
        )
        return None

    url = (
        "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
        f"?api_key={settings.EIA_API_KEY}"
        "&frequency=weekly"
        "&data[0]=value"
        "&facets[series][]=EMD_EPD2D_PTE_NUS_DPG"
        "&sort[0][column]=period"
        "&sort[0][direction]=desc"
        "&length=1"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read())
        rows = payload.get("response", {}).get("data", [])
        if not rows:
            logger.warning("[EIA] No data rows returned from EIA API")
            return None
        price = float(rows[0]["value"])
        logger.info("[EIA] Diesel $%.3f/gal (period: %s)", price, rows[0].get("period"))
        return price
    except Exception as exc:
        logger.error("[EIA] Failed to fetch diesel price: %s", exc)
        return None


def get_fsc_rate(fuel_price_per_gallon: float) -> Optional[Decimal]:
    """
    Look up the ALG Worldwide FSC for the given diesel price.

    fsc_amount in the DB is already the decimal multiplier (e.g., 0.365 = 36.5% surcharge).
    The Excel source stores 0.365 for the 36.5% band — NOT 36.5.

    Apply to base tariff: access_prog = base_tariff × (1 + fsc_pct)
    Confirmed June 22 meeting: FSC is a % of base freight, sourced from EIA weekly diesel.
    """
    from backend.database import SessionLocal
    from backend.models import FuelSurchargeRate

    db = SessionLocal()
    try:
        price = Decimal(str(fuel_price_per_gallon))
        rate = (
            db.query(FuelSurchargeRate)
            .filter(
                FuelSurchargeRate.fuel_price_min <= price,
                FuelSurchargeRate.fuel_price_max >= price,
            )
            .first()
        )
        if not rate:
            logger.warning("[FSC] No rate found for fuel price $%.2f/gal", fuel_price_per_gallon)
            return None
        fsc_pct = rate.fsc_amount  # already a decimal multiplier (0.365 = 36.5%)
        logger.info("[FSC] fuel=$%.2f → fsc_pct=%s (%.1f%%)", fuel_price_per_gallon, fsc_pct, float(fsc_pct) * 100)
        return fsc_pct
    finally:
        db.close()


def get_tariff_rate(destination_zip3: str, weight: float,
                    _diesel_price: Optional[float] = None,
                    _fsc_pct: Optional[Decimal] = None) -> Optional[dict]:
    """
    Look up the FSC-inclusive Access program rate for a delivery ZIP3.

    destination_zip3: first 3 digits of the delivery ZIP from the ALG invoice Zip column.
    weight: pallet weight in lbs.

    Lookup strategy:
      1. Exact match on ep_zip3 (e.g., "606" → Chicago SCF zone 606).
      2. If no exact match, use nearest zone: largest ep_zip3 ≤ delivery_zip3.
         Example: delivery ZIP "80266xxxx" → zip3="802" → nearest zone "800" (Denver).
         This handles ALG invoices that include the physical facility ZIP rather than
         the SCF zone number (e.g., "802660001" instead of the zone code "800").

    _diesel_price / _fsc_pct: pass pre-fetched values to avoid one EIA API call per
    pallet row. If omitted, fetched from EIA on each call.

    Returns: {"access_prog": Decimal, "base_tariff": Decimal, "fsc_pct": Decimal,
              "is_exact_zone_match": bool}
    Returns None if no active tariff row found even with nearest-zone fallback.
    """
    from backend.database import SessionLocal
    from backend.models import TariffRate

    db = SessionLocal()
    try:
        zip3 = destination_zip3.zfill(3)

        # 1. Exact match
        rates = (
            db.query(TariffRate)
            .filter(TariffRate.ep_zip3 == zip3, TariffRate.ignore_flag.is_(False))
            .all()
        )

        matched_zone = zip3
        if not rates:
            # 2. Nearest zone: largest ep_zip3 ≤ zip3
            candidates = (
                db.query(TariffRate)
                .filter(TariffRate.ep_zip3 <= zip3, TariffRate.ignore_flag.is_(False))
                .order_by(TariffRate.ep_zip3.desc())
                .limit(10)
                .all()
            )
            if candidates:
                matched_zone = candidates[0].ep_zip3
                rates = [r for r in candidates if r.ep_zip3 == matched_zone]
                logger.info("[TARIFF] zip3=%s no exact match → nearest zone %s", zip3, matched_zone)

        if not rates:
            logger.warning("[TARIFF] No rate found for zip3=%s (nearest zone also missing)", zip3)
            return None

        # When a zone has multiple facility rows, use the lowest rate (conservative)
        rate = min(rates, key=lambda r: float(r.cost_per_100lb))

        base_cost = float(rate.cost_per_100lb) * weight / 100.0
        base_tariff = max(base_cost, float(rate.minimum_freight))

        diesel_price = _diesel_price if _diesel_price is not None else get_current_diesel_price()
        fsc_pct = _fsc_pct if _fsc_pct is not None else (
            get_fsc_rate(diesel_price) if diesel_price is not None else None
        )

        if fsc_pct is not None:
            access_prog = Decimal(str(round(base_tariff, 2))) * (Decimal("1") + fsc_pct)
        else:
            access_prog = Decimal(str(round(base_tariff, 2)))

        logger.info(
            "[TARIFF] zip3=%s zone=%s weight=%.0f base=%.2f fsc_pct=%s → access_prog=%.2f",
            zip3, matched_zone, weight, base_tariff, fsc_pct, access_prog,
        )
        return {
            "access_prog": Decimal(str(round(float(access_prog), 2))),
            "base_tariff": Decimal(str(round(base_tariff, 2))),
            "fsc_pct":     fsc_pct,
            "is_exact_zone_match": matched_zone == zip3,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Prophecy SID export — pallet-level data from VisualMail for SID import file
# ---------------------------------------------------------------------------

# Adapted from "Created From Create Import from VM to Prophesy by Manifest.sql".
# Accepts a list of full manifest numbers (e.g., TEC_M_0228920).
# Locations.AccountNumber is the destination code (e.g., 'SCF606' → zone '606').
# Column aliases match SID_CSV_COLUMNS exactly. Extra columns (ManifestNumber etc.)
# are ignored by csv.DictWriter(extrasaction="ignore") in generate_sid_csv().
_SID_QUERY = """
SELECT
    VisualMail.dbo.Manifest.ManifestNumber,
    CASE
        WHEN LEFT(Pallet.UniqueContainerID, 3) = '99M'
        THEN RIGHT(Pallet.UniqueContainerID, 20)
        ELSE Pallet.UniqueContainerID
    END AS Order_ID,
    VisualMail.dbo.Locations.AccountNumber AS Dest_ID,
    ROUND(VisualMail.dbo.Pallet.Weight, 0) AS Wgt,
    1 AS Pallets,
    VisualMail.dbo.Pallet.NumberOfCopies AS PCS,
    CASE
        WHEN LEFT(Locations.AccountNumber, 3) = 'SCF'
        THEN Pallet.SCF_DateStart
        ELSE Pallet.NDC_DateStart
    END AS Delv_Appt_From,
    CASE
        WHEN LEFT(Locations.AccountNumber, 3) = 'SCF'
        THEN Pallet.SCF_DateEnd
        ELSE Pallet.NDC_DateEnd
    END AS Delv_Appt_to,
    VisualMail.dbo.Pallet.JobNumber,
    VisualMail.dbo.Pallet.DueDate AS Earliest_Ship_Date,
    VisualMail.dbo.Pallet.JobName AS Order_Comments,
    'IHD From: ' + CONVERT(VARCHAR(12), VisualMail.dbo.Pallet.InHomeDateFrom, 101)
        + ' to ' + CONVERT(VARCHAR(12), VisualMail.dbo.Pallet.InHomeDateTo, 101) AS Comments_2,
    VisualMail.dbo.PalletStatusCodes.Description,
    VisualMail.dbo.Pallet.Version AS Version1
FROM VisualMail.dbo.WarehouseLocation
    INNER JOIN VisualMail.dbo.PalletLoc
        ON VisualMail.dbo.WarehouseLocation.ID = VisualMail.dbo.PalletLoc.LocationID
    RIGHT OUTER JOIN VisualMail.dbo.Pallet
        INNER JOIN VisualMail.dbo.Locations
            ON VisualMail.dbo.Pallet.Consignee = VisualMail.dbo.Locations.ID
        INNER JOIN VisualMail.dbo.PalletStatusCodes
            ON VisualMail.dbo.Pallet.PalletStatus = VisualMail.dbo.PalletStatusCodes.StatusCode
        INNER JOIN VisualMail.dbo.Manifest
            ON VisualMail.dbo.Pallet.ID = VisualMail.dbo.Manifest.ManifestID
        ON VisualMail.dbo.PalletLoc.PalletNumber = VisualMail.dbo.Pallet.UniqueContainerID
WHERE (VisualMail.dbo.Pallet.Active = 1)
  AND (VisualMail.dbo.Manifest.ManifestNumber IN ({placeholders}))
ORDER BY VisualMail.dbo.Pallet.MotherPalletID, Order_ID
"""


def get_pallet_data_for_manifests(manifest_numbers: list[str]) -> list[dict]:
    """
    Fetch pallet-level rows from VisualMail for each manifest in the list.
    Used to generate the Prophecy SID import file.

    Returns a list of dicts with keys matching SID_CSV_COLUMNS in csv_export.py:
        ManifestNumber, OrderNumber, UniqueContainerID, Order_ID, MotherPalletID,
        Dest ID, Wgt, Pallets, PCS, Delv Appt From, Delv Appt to, JobNumber,
        Earliest Ship Date, Order Comments, Comments 2, Description, JobID,
        Version, VerifiedDate, Location

    Dest ID = Locations.AccountNumber, e.g. 'SCF606' (parse first 3 digits after
    prefix to get the ep_zip3 for tariff lookup: 'SCF606' → '606').
    """
    if not manifest_numbers:
        return []

    try:
        conn = _get_connection()
        cursor = conn.cursor()
        placeholders = ",".join(["?"] * len(manifest_numbers))
        query = _SID_QUERY.format(placeholders=placeholders)
        cursor.execute(query, manifest_numbers)
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()
        conn.close()

        result = [dict(zip(columns, row)) for row in rows]
        logger.info("[SID] Fetched %d pallet rows for %d manifests", len(result), len(manifest_numbers))
        return result

    except Exception as exc:
        logger.error("[SID] Query failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# ALG invoice CSV (from Tanya via email or direct upload)
# ---------------------------------------------------------------------------

def get_alg_invoice(invoice_number: str) -> Optional[dict]:
    """
    TODO: Parse the daily ALG invoice from Tanya (CSV format confirmed).

    CSV join key: BOL No field = last 6 significant digits of technique_trip.
    Example: trip TEC_T_0397246 → invoice BOL No = '397246' (str(int('0397246'))).

    Invoice CSV columns (one row per pallet/destination):
        Invoice No, Invoice Date, Cust Job No, Job Name, Pro 8125, Ver, IB/Date,
        SiteKey, BOL No, Post Office, Type, Zip, State, Pcs, GrossWt, PalletCount,
        Container, Trays, Rate, Billed$
    Footer rows:
        "Fuel Surcharge" row: col[16]=base_freight_total, col[17]=fsc_rate, col[18]=fsc_cost
        "Total Billed Amount:" row: col[19]=total_amount

    Aggregate across pallet rows for per-invoice totals.

    Expected return shape:
    {
        "invoice_number":       str,     -- e.g. "Z556229"
        "amount":               Decimal, -- Total Billed Amount from footer
        "invoice_email_sender": str,     -- e.g. "Tanya 6/10/2026 4:21PM"
        "alg_weight":           Decimal, -- sum of GrossWt
        "alg_pallets":          int,     -- sum of PalletCount
        "alg_pcs":              int,     -- sum of Pcs
    }

    For now, use POST /api/invoices/upload to import CSVs manually.
    """
    pass
