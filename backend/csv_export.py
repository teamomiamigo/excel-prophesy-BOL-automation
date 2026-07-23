import io
import csv
from datetime import date
from decimal import Decimal
from typing import Optional


CSV_COLUMNS = [
    "Trip",
    "Manifest",
    "BOL",
    "Tech Weight",
    "Tech Pallets",
    "Tech PCS",
    "Invoice #",
    "Invoice Sender",
    "Calculated Cost",
    "Amount",
    "Cost %",
    "Proph Weight",
    "Weight Diff",
    "Proph Pallets",
    "Pallet Diff",
    "Proph PCS",
    "PCS Diff",
    "Notes",
]

# Exact 13-column format confirmed from real Prophecy import file:
# "New Import VM to Prophesy by manifest (10).csv"
SID_CSV_COLUMNS = [
    "Order_ID",
    "Dest_ID",
    "Wgt",
    "Pallets",
    "PCS",
    "Delv_Appt_From",
    "Delv_Appt_to",
    "JobNumber",
    "Earliest_Ship_Date",
    "Order_Comments",
    "Comments_2",
    "Version1",
    "Description",
]


def _fmt_decimal(val) -> str:
    if val is None:
        return ""
    return f"{Decimal(str(val)):.2f}"


def _fmt_cost_pct(val) -> str:
    if val is None:
        return ""
    pct = Decimal(str(val)) * 100
    return f"{pct:.2f}%"


def _fmt_int(val) -> str:
    if val is None:
        return ""
    return str(int(val))


def _fmt_diff(val) -> str:
    if val is None:
        return ""
    n = int(val) if isinstance(val, Decimal) else val
    return f"+{n}" if n > 0 else str(n)


def generate_csv_bytes(bol_records: list[dict]) -> bytes:
    #accpet list of BOL dicts and returns CSV bytes matching column order
    """
    Accept a list of BOL dicts and return UTF-8 CSV bytes matching
    the column order of the accounting export.
    """
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()

    for r in bol_records:
        writer.writerow({
            "Trip":             r.get("technique_trip") or "",
            "Manifest":         r.get("manifest") or "",
            "BOL":              _fmt_int(r.get("bol_number")),
            "Tech Weight":      _fmt_decimal(r.get("technique_weight")),
            "Tech Pallets":     _fmt_int(r.get("technique_pallets")),
            "Tech PCS":         _fmt_int(r.get("technique_pcs")),
            "Invoice #":        r.get("invoice_number") or "",
            "Invoice Sender":   r.get("invoice_email_sender") or "",
            "Calculated Cost":  _fmt_decimal(r.get("access_prog")),
            "Amount":           "DNP" if r.get("is_do_not_pay") else _fmt_decimal(r.get("amount")),
            "Cost %":           _fmt_cost_pct(r.get("cost_pct")),
            "Proph Weight":     _fmt_decimal(r.get("prophecy_weight")),
            "Weight Diff":      _fmt_diff(r.get("weight_diff")),
            "Proph Pallets":    _fmt_int(r.get("prophecy_pallets")),
            "Pallet Diff":      _fmt_diff(r.get("pallet_diff")),
            "Proph PCS":        _fmt_int(r.get("prophecy_pcs")),
            "PCS Diff":         _fmt_diff(r.get("pcs_diff")),
            "Notes":            r.get("notes") or "",
        })

    return output.getvalue().encode("utf-8")


def generate_mock_sid_rows(bol_records: list[dict]) -> list[dict]:
    # generate fake SID pallet rows from approved mock BOL records
    #one row per pallet using 13-column format prophecy expects
    rows = []
    for rec in bol_records:
        manifest = rec.get("manifest") or "MOCK_MANIFEST"
        manifest_suffix = manifest[-6:] if len(manifest) >= 6 else manifest
        n_pallets = int(rec.get("technique_pallets") or 1)
        total_weight = float(rec.get("technique_weight") or 0)
        total_pcs = int(rec.get("technique_pcs") or 0)
        pallet_wgt = round(total_weight / n_pallets, 0) if n_pallets else 0
        pallet_pcs = total_pcs // n_pallets if n_pallets else 0

        for i in range(1, n_pallets + 1):
            order_id = f"9M{manifest_suffix}{i:04d}"
            rows.append({
                "Order_ID":           order_id,
                "Dest_ID":            "SCF606",
                "Wgt":                int(pallet_wgt),
                "Pallets":            1,
                "PCS":                pallet_pcs,
                "Delv_Appt_From":     "",
                "Delv_Appt_to":       "",
                "JobNumber":          f"MOCK{i:05d}",
                "Earliest_Ship_Date": "",
                "Order_Comments":     rec.get("notes") or "",
                "Comments_2":         "",
                "Version1":           "",
                "Description":        "Shipped to USPS facility",
            })
    return rows


def generate_sid_csv(pallet_rows: list[dict]) -> bytes:
    # format pallet-level rows from get_pallet_data_for_manifests() into SID export
    # in live mode, SID_QUERY in data_layer.py aliasses columns to these exact names.
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=SID_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in pallet_rows:
        writer.writerow({col: (row.get(col) or "") for col in SID_CSV_COLUMNS})
    return output.getvalue().encode("utf-8")


def get_csv_filename(export_date: Optional[date] = None) -> str:
    d = export_date or date.today()
    return f"SG360_BOL_Export_{d.strftime('%Y%m%d')}.csv"


def get_sid_filename(export_date: Optional[date] = None) -> str:
    d = export_date or date.today()
    return f"SG360_Prophecy_SID_{d.strftime('%Y%m%d')}.csv"
