
*updated july 6 2026*
*created june 24 2026*

---
#### **Previous Walkthrough**
Every morning, Katie manually copies data from three systems into an Excel spreadsheet, calculates cost variance by hand, and emails a CSV to Mary in accounting. This tool replaces that with a web dashboard that pulls the same data automatically and presents it in a single view.
**Source file being replaced:** `Technique and BOL Numbers New June 2026.xlsx`

---
#### **New Daily Workflow**

**Step 1 — Load Manifests**
Katie clicks **Pull Manifests**. The system:
- Queries manifests and their relevant data from AWP-SQL-PROD (Technique/VisualMail)
- Pulls weight, pallets, and pieces from VisualMail via a second query
- Stores everything in PostgreSQL; records appear in the dashboard immediately

**Step 2 — Load Invoices (when Tania's invoice batch arrives)**
Tania drops ALG invoice CSVs into a named subfolder on the shared drive (e.g. `Tania 6-25-2026  4-16PM`). Katie clicks **Pull Invoices**. The system:
- Scans the shared drive for unprocessed subfolders and flat CSVs
- Parses the folder name to extract sender and timestamp (stored as `invoice_email_sender` / `invoice_sent_at`)
- Matches each invoice to the correct manifest using the Job Name field (trip suffix) or BOL number
- Populates invoice amount, Z-number, ALG weight/pallets/pcs
- Calculates **Cost %** = ALG invoice amount ÷ expected tariff rate

Katie can also upload CSVs manually via **Upload Invoices**, with an optional sender info panel (▼ Sender) to record name, date, and time.

**Step 3 — Reviewing**
Katie opens the dashboard. For each record she sees:

| Field                | Source                              | Purpose                            |
| -------------------- | ----------------------------------- | ---------------------------------- |
| Trip / Manifest      | Technique (SQL)                     | Identifies the shipment            |
| BOL Number           | Prophecy / auto-fill from Technique | Needed for accounting export       |
| Invoice Sender       | Folder name                         | Which batch this invoice came from |
| Invoice #            | ALG Invoice (Z-number)              | Links to original CSV              |
| Tech Wgt / Pal / PCS | VisualMail                          | What SG360 shipped                 |
| ALG Wgt / Pal / PCS  | ALG Invoice                         | What ALG recorded                  |
| Wgt / Pal / PCS Diff | Calculated                          | Discrepancy flags                  |
| Calculated Cost      | Tariff + FSC                        | What SG360 expected to pay         |
| Invoice Amount       | ALG Invoice                         | What ALG actually charged          |
| **Cost %**           | Calculated                          | **The key metric**                 |

Cost % color thresholds:
- 🟢 Green (97–103%) — looks right, approve
- 🟡 Yellow (94–97% or 103–106%) — worth checking
- 🔴 Red (<94% or >106%) — flag for investigation

Katie either **Approves** or **Flags** each record. Flagged records stay visible until resolved. Records can also be marked **3rd Party** (customer pays ALG directly, excluded from exports) or **Ignored** (excluded from exports but retained in log).

**Step 4 — Export**

The Approved section groups records by invoice batch (sender + date). Each batch has its own actions:

1. **Export to Prophecy** (top of section, shown if any records lack a BOL) — downloads the Prophecy SID import CSV. Katie imports it into Prophecy to create load numbers.

2. **Re-fetch BOLs** (per batch, live mode only) — after Katie imports the SID file and creates loads in Prophecy, clicking this re-queries Technique for those specific manifests and pulls the new BOL numbers back into the dashboard.

3. **Send to Accounting →** (per batch) — opens the Email Compose modal:
   - Shows a 4-column table: BOL | Invoice # | Sender | Amount + TOTAL
   - Editable To: and Subject: fields (subject pre-filled with sender + date)
   - **Copy Table** — copies an HTML table to clipboard; pastes as a formatted table in Outlook
   - **Open in Outlook** — opens a mailto: draft with plain-text table and pre-filled subject
   - Katie reviews, edits if needed, and sends from Outlook

4. After sending, Katie clicks **Mark as Sent ✓** in the modal. The system sets `accounting_exported_at` on all records in the batch; they move from Approved to the Log.

---
#### **Manifest Record Types**

**Type A — No BOL Yet (needs SID export)**
The Romeoville building hasn't created a load in Prophecy yet.
- Invoice Job Name matches the manifest trip number suffix (e.g. `TEC_T_0397246` → Job Name `397246`)
- Katie approves, then imports the SID file into Prophecy to create the BOL
- BOL number gets pulled back via Re-fetch BOLs after Prophecy import

**Type B — BOL Already Exists**
The Wolf building creates loads in Prophecy before shipping, so these arrive with a BOL number already attached.
- Invoice Job Name matches an existing BOL number
- Katie just approves — no SID export needed
- BOL auto-fills from the initial manifest pull

**Comingle Loads**
Some loads mix multiple customers (`CM_` prefix). These appear as invoice-only stubs with no Technique match. Labeled "Comingle — no Technique match" in the dashboard.

---
#### **Dashboard Layout**

```
┌────────────────────────────────────────────────────────────────────────┐
│  SG360 BOL Reconciliation                           Tuesday, July 1    │
│  DASHBOARD  |  LOG                                                     │
├────────────────────────────────────────────────────────────────────────┤
│  [50 Manifest Only]  [31 Invoice Only]  [13 Ready]  [41 Approved]      │
│  [↻ Pull Manifests]  [⤓ Pull Invoices]  [Upload Invoices]  [▼ Sender]  │
├────────────────────────────────────────────────────────────────────────┤
│  Pending Records                         [Search: trip / manifest...]  │
│  [Trip] [Manifest] [BOL] [Sender] [Invoice #] [Wgt] [$] [Cost %]       │
│  ...rows...                    [✓ Approve] [⚑ Flag] [3P] [Ignore]      │
├────────────────────────────────────────────────────────────────────────┤
│  Third Party (collapsible)                                             │
│  [records marked 3rd party]               [✓ Approve] [↩ Unmark]      │
├────────────────────────────────────────────────────────────────────────┤
│  Approved (41)                              [Export to Prophecy (N)]   │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Tania 6/25/2026 4:16PM  · 2 records · $6,841.71             ▾  │  │
│  │ [↺ Re-fetch BOLs]                      [Send to Accounting →]   │  │
│  │ [Trip] [Manifest] [BOL] [Invoice #] [Calc Cost] [$] [Cost %]    │  │
│  │ ...rows...                                          [↩ Revert]   │  │
│  ├──────────────────────────────────────────────────────────────────┤  │
│  │ No Sender  · 39 records · $107,019.28                       ▸  │  │
│  │                                        [Send to Accounting →]   │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

**Log tab** — all records across all dates, searchable by date range, status, and invoice sender. Shows `accounting_exported_at` timestamp once a batch has been confirmed sent.

---
#### **Calculated Cost Formula**

```
base_cost       = (cost_per_100lb × weight_lbs) / 100
base_tariff     = max(base_cost, minimum_freight)
fsc_pct         = fuel surcharge band for today's EIA diesel price
calculated_cost = base_tariff × (1 + fsc_pct)
```

- Tariff rate comes from the Access rate card (seeded into the database once)
- FSC comes from ALG's fuel surcharge matrix (seeded into the database once)
- Diesel price fetches live from the EIA API weekly (`EIA_API_KEY` required)
- Tariff lookup uses the 3-digit SCF ZIP zone from the manifest destination field

---
#### **Features**

| Built | Needs work |
| ----- | ---------- |
| Pull manifests from AWP-SQL-PROD | Comingle section (Module 2) |
| Pull weights/pallets/pcs from VisualMail | Scheduled morning pulls |
| Invoice CSV matching from shared drive subfolders | Prophecy BOL sync (needs Megha — schema) |
| Invoice sender + timestamp from folder name | VisualMail SELECT permission (AWD-SQL-WH4) |
| Manual invoice upload with optional sender info | |
| Cost % calculation with color coding | |
| Approve / Flag / Unflag / Ignore / 3rd Party | |
| Prophecy SID export (Type A records) | |
| Per-batch email compose with Outlook mailto: | |
| Copy Table (HTML → pastes formatted in Outlook) | |
| Mark as Sent → moves batch to Log | |
| Re-fetch BOLs after Prophecy import | |
| Audit log with date-range and sender filter | |
| Multi-invoice accumulation per trip | |
| Weight / pallet / PCS diff display | |
| Invoice reassignment between trips | |

---
