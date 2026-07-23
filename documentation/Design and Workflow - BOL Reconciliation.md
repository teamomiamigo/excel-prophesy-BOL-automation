
*updated 2026-07-22*
*created june 24 2026*

---
#### **Previous Walkthrough**
Every morning, Katie manually copies data from three systems into an Excel spreadsheet, calculates cost variance by hand, and emails a CSV to Mary in accounting. This tool replaces that with a web dashboard that pulls the same data automatically and presents it in a single view.
**Source file being replaced:** `Technique and BOL Numbers New June 2026.xlsx`

---
#### **New Daily Workflow**

**Step 1 — Invoices arrive, manifest data is discovered automatically**
There is no separate "load manifests" step anymore — the daily bulk Technique pull (previously a "Pull Manifests" button) was removed 2026-07-22 in favor of discovering trip/manifest data on demand, per invoice, at the moment it's actually needed:
- When an invoice is uploaded or polled in (Step 2 below), the system first checks its own database for an already-known matching trip/manifest
- If nothing matches yet, it automatically checks Technique/VisualMail directly (a wider, on-demand search — up to 90 days back), pulling weight, pallets, and pieces at the same time
- If a trip turns out to have split into more than one manifest (see "Ambiguous trips" below), every manifest on that trip gets persisted too, not just the one holding the invoice
- Nothing pre-populates an "awaiting invoice" bucket anymore — a record only exists once there's an invoice to reconcile it against (or it's a sibling manifest discovered alongside one)

**Step 2 — Load Invoices (when Tanya's invoice batch arrives)**
Tanya drops ALG invoice CSVs into a named subfolder on the shared drive (e.g. `Tanya 6-25-2026  4-16PM`). Katie clicks **⤓ Pull Invoices**. The system:
- Scans the shared drive for unprocessed subfolders and flat CSVs
- Parses the folder name to extract sender and timestamp (stored as `invoice_email_sender` / `invoice_sent_at`)
- Matches each invoice to the correct manifest using the Job Name field (trip suffix, then manifest suffix) or an existing Prophecy BOL number — instantly, from data already in the database
- Populates invoice amount, Z-number, ALG weight/pallets/pcs
- Calculates **Cost %** = ALG invoice amount ÷ our own calculated cost (over 100% means our calculation came in *lower* than what ALG billed)
- For anything that doesn't match instantly, the dashboard shows it as "checking Technique…" while an automatic follow-up search runs in the background (this is Step 1's on-demand discovery, triggered right after upload) — it patches in place once resolved, with no further clicks needed

Katie can also upload a folder of CSVs manually via **Upload Invoice Folder** — same matching logic, sender/date parsed from the folder name.

**Step 3 — Reviewing**
Katie opens the dashboard. For each record she sees:

| Field                | Source                              | Purpose                            |
| -------------------- | ------------------------------------ | ----------------------------------- |
| Trip / Manifest      | Technique (SQL)                     | Identifies the shipment            |
| BOL Number           | Prophecy / auto-synced from Technique | Needed for accounting export       |
| Invoice Sender       | Folder name                         | Which batch this invoice came from |
| Invoice #            | ALG Invoice (Z-number, clickable link to the PDF/CSV) | Links to original file |
| Tech Wgt / Pal / PCS | VisualMail                          | What SG360 shipped                 |
| ALG Wgt / Pal / PCS  | ALG Invoice                         | What ALG recorded                  |
| Wgt / Pal / PCS Diff | Calculated                          | Discrepancy flags                  |
| Calculated Cost      | ALG's own per-zone rate → internal rate table fallback, see formula below | What SG360 expected to pay, hover for a per-pallet breakdown |
| Invoice Amount       | ALG Invoice                         | What ALG actually charged          |
| **Cost %**           | Calculated                          | **The key metric**                 |

Cost % color thresholds:
- 🟢 Green (97–103%) — looks right, approve
- 🟡 Yellow (94–97% or 103–106%) — worth checking
- 🔴 Red (<94% or >106%) — flag for investigation

A record can also carry a few informational badges/actions:
- **`~EST`** next to Calculated Cost — a rate had to be approximated, our own weight data wasn't available so ALG's own weight was used instead, or a minimum-charge floor couldn't be confirmed
- **`~UNVERIFIED`** next to the weight cell — this manifest's trip split into more than one shipment and the system isn't sure which one the invoice actually belongs to. A **"⚖ Compare"** button appears in Actions, opening a side-by-side comparison of every manifest on the trip (with ΔWgt/ΔPal/ΔPcs per candidate) so Katie can reassign the invoice to the right one, or delete a manifest that's just bad/duplicate data. If there's no sibling to compare against (a lone manifest with a severe mismatch), a **"✓ Acknowledge"** button appears instead, just to clear the badge once Katie's checked it
- **"Do Not Pay"** — for an invoice that never matched any Technique/Prophecy record; approves it into its sender's batch and shows "DO NOT PAY" instead of a dollar amount (reversible)

Katie either **Approves** or **Flags** each record. Flagged records stay visible until resolved (the flag reason shows as a hover tooltip on the flag icon). Records can also be marked **3rd Party** (customer pays ALG directly, excluded from exports) or **Do Not Pay** (included in the accounting export, but shown as "DO NOT PAY" instead of an amount — replaced the old "Ignore" label 2026-07-15).

**Step 4 — Export**

The Approved section groups records by invoice batch (sender + date). Each batch has its own actions:

1. **Export to Prophecy** (top of section, shown if any records lack a BOL) — downloads the Prophecy SID import CSV. Katie imports it into Prophecy to create load numbers. (A per-record "SID" button also exists for pushing one urgent record without waiting for the whole batch.)

2. **Re-fetch BOLs** (per batch, live mode only) — after Katie imports the SID file and creates loads in Prophecy, clicking this re-queries Technique for those specific manifests and pulls the new BOL numbers back into the dashboard. (A per-record "↻ BOL" button does the same for just one manifest.)

3. **Send to Accounting →** (per batch) — opens the Email Compose modal:
   - Shows a 3-column table: BOL | Invoice # | Sender | Amount
   - Editable To: and Subject: fields (subject pre-filled with sender + date)
   - **Copy Table** — copies an HTML table to clipboard; pastes as a formatted table in Outlook
   - **Open in Outlook** — opens a mailto: draft with plain-text table and pre-filled subject
   - **Download Invoice PDFs** — downloads a single merged PDF of every invoice in the batch, for Katie to attach
   - Katie reviews, edits if needed, and sends from Outlook

4. After sending, Katie clicks **Mark as Sent ✓** in the modal. The system sets `accounting_exported_at` on all records in the batch; they move from Approved to the Log. (If a batch gets sent by mistake, the Log tab has a "↩ Revert to Pending" action that undoes this and clears the export timestamp.)

---
#### **Manifest Record Types**

**Type A — No BOL Yet (needs SID export)**
The Romeoville building hasn't created a load in Prophecy yet.
- Invoice Job Name matches the manifest trip number suffix (e.g. `TEC_T_0397246` → Job Name `397246`)
- Katie approves, then imports the SID file into Prophecy to create the BOL
- BOL number gets pulled back automatically the next time Technique/Prophecy is checked for this manifest (via Re-fetch BOLs, or the per-record ↻ BOL button)

**Type B — BOL Already Exists**
The Wolf building creates loads in Prophecy before shipping, so these arrive with a BOL number already attached.
- Invoice Job Name matches an existing BOL number
- Katie just approves — no SID export needed
- BOL is picked up as soon as the manifest is discovered (Step 1), via the same Technique/ShipperPlus join used everywhere else

**Comingle Loads**
Some loads mix multiple customers (`CM_` prefix). These appear as invoice-only stubs with no Technique match. Labeled "Comingle — no Technique match" in the dashboard.

**Ambiguous trips**
Occasionally a single Technique trip splits into more than one manifest (usually because a job on the trip should have been billed third-party and wasn't marked that way). Since there's no daily bulk pull anymore to pre-populate every manifest on a trip, the system now persists all of a trip's manifests as soon as it discovers the ambiguity — the invoiced one stays the one visible row (badged `~UNVERIFIED`), the others are only seen inside the "⚖ Compare" tool. See Step 3 above.

---
#### **Dashboard Layout**

```
┌────────────────────────────────────────────────────────────────────────┐
│  SG360 BOL Reconciliation                           Tuesday, July 22   │
│  DASHBOARD  |  LOG                                                     │
├────────────────────────────────────────────────────────────────────────┤
│  ┌───────────────┐  ┌──────────────────────┐  ┌───────────────────┐    │
│  │      12       │  │         13           │  │        41         │    │
│  │ Awaiting       │  │ Ready to Review      │  │ Approved Today     │    │
│  │ Invoice        │  │ 9 Type A · 4 Type B  │  │                    │    │
│  └───────────────┘  └──────────────────────┘  └───────────────────┘    │
│  [⤓ Pull Invoices]  [Upload Invoice Folder]                            │
├────────────────────────────────────────────────────────────────────────┤
│  Pending Records                         [Search: trip / manifest...]  │
│  [Trip] [Manifest] [BOL] [Sender] [Invoice #] [Wgt] [$] [Cost %] [Notes]│
│  ...rows...          [✓ Approve] [⚑] [SID] [↻ BOL] | [3P] [DNP/Compare] │
├────────────────────────────────────────────────────────────────────────┤
│  Third Party (collapsible)                                             │
│  [records marked 3rd party]      [Move All to Log (N)]  [↩ Unmark]     │
├────────────────────────────────────────────────────────────────────────┤
│  Approved (41)                              [Export to Prophecy (N)]   │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Tanya 6/25/2026 4:16PM  · 2 records · $6,841.71             ▾  │  │
│  │ [↺ Re-fetch BOLs]      [Send to Accounting →]  [Download PDFs]   │  │
│  │ [Trip] [Manifest] [BOL] [Invoice #] [Calc Cost] [$] [Cost %]    │  │
│  │ ...rows...                                          [↩ Revert]   │  │
│  ├──────────────────────────────────────────────────────────────────┤  │
│  │ No Sender  · 39 records · $107,019.28                       ▸  │  │
│  │                                        [Send to Accounting →]   │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

**Log tab** — all records across all dates, searchable by date range, status, and invoice sender. Shows `accounting_exported_at` timestamp once a batch has been confirmed sent, with a "↩ Revert to Pending" action for anything sent by mistake.

---
#### **Calculated Cost Formula**

The real formula is more layered than a single flat lookup — it prioritizes ALG's own invoiced rate over SG360's internal card, since the rate/zone structure is legitimately ALG's pricing (only the weight/pallet/piece counts have to be SG360's own independent numbers):

```
For each pallet, resolve a per-cwt rate in priority order:
  (a) ALG's own rate for this exact zone, from THIS invoice's own CSV (its Rate column, exact zip3 match, else nearest zone within ±5)
  (b) exact match against alg_tariff_rates (a much more complete ALG-sourced rate table, keyed by destination code — no zip3 involved)
  (c) SG360's internal zip3-keyed tariff_rates card, as a last resort (flags the record `~EST`)

base_cost   = rate × (weight_lbs / 100)
base_tariff = max(base_cost, that zone's own minimum freight charge)
fsc_pct     = parsed from THIS invoice's own "Fuel Surcharge" footer row (dollar-derived, more precise than
              its printed Rate label); EIA's weekly diesel price is only a fallback if that can't be parsed
calculated_cost = base_tariff × (1 + fsc_pct)
```

If even one pallet on the load can't be priced by any of the three rate sources above, the whole load falls back to the invoice's own blended $/cwt rate instead (or is left blank if that's unavailable too) — no partial per-zone total is ever reported as if it were the whole load's cost.

Weight/pallet/piece counts always come from SG360's own Technique/Prophecy data, never from ALG's invoice — if that data isn't available yet, Calculated Cost is left blank rather than guessed from ALG's own numbers.

---
#### **Features**

| Built | Needs work |
| ----- | ---------- |
| Automatic per-invoice manifest/trip discovery (no more manual "Pull Manifests") | Comingle section (Module 2) |
| Pull weights/pallets/pcs from VisualMail | Scheduled/automated daily invoice email polling (manual upload/poll-folder is the real daily path today) |
| Invoice CSV matching from shared drive subfolders, with automatic wide-search follow-up | |
| Invoice sender + timestamp from folder name | |
| Manual invoice-folder upload | |
| Cost % calculation with color coding, 3-tier rate resolution, and a per-pallet cost breakdown tooltip | |
| Approve / Flag / Unflag / Do Not Pay / 3rd Party | |
| Ambiguous-trip detection with a side-by-side Compare tool, plus dismiss/acknowledge actions | |
| Prophecy SID export (Type A records), per-record or per-batch | |
| Prophecy BOL sync (reads `load_id` back automatically — resolved 2026-07-01, no longer blocked on schema questions) | |
| Per-batch email compose with Outlook mailto:, plus merged invoice PDF download | |
| Copy Table (HTML → pastes formatted in Outlook) | |
| Mark as Sent → moves batch to Log, with a Log-tab revert action | |
| Re-fetch BOLs after Prophecy import, per-batch or per-record | |
| Audit log with date-range and sender filter | |
| Multi-invoice accumulation per trip | |
| Weight / pallet / PCS diff display | |
| Invoice reassignment between trips (now also recomputes cost/diffs on both sides) | |
| VisualMail SELECT permission (confirmed granted and working) | |

---
