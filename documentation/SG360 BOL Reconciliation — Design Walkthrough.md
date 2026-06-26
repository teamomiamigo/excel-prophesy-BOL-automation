
*updated june 26 2026*
*created june 24 2026*

---
#### **Previous Walkthrough**
Every morning, Katie manually copies data from three systems into an Excel spreadsheet, calculates cost variance by hand, and emails a CSV to Mary in accounting. This tool replaces that with a web dashboard that pulls the same data automatically and presents it in a single view.
**Source file being replaced:** `Technique and BOL Numbers New June 2026.xlsx`

---
#### **New Daily Workflow**
**Step 1 — Load Manifests**
clicks **Pull Manifests**. The system:
- Queries Manifests and their relevant data from the past N days
- Pulls weight, pallets, and pieces from VisualMail
- Stores everything in the local database (as of now)
Records appear in the dashboard as soon as the pull completes.

**Step 2 — Load Invoices (when Tanya's email arrives)**
Katie clicks **Pull Invoices** (or uploads a CSV manually). The system:
- Reads invoice CSV files from a shared folder (or click on manual upload)
- Matches each invoice to the correct manifest using the Job Name field
- Populates invoice amount, Z-number, ALG weight/pallets/pcs
- Calculates **Cost %** = ALG invoice amount ÷ expected tariff rate

**Step 3 — Reviewing**
Katie opens the dashboard. For each record she sees:

| Field                          | Source               | Purpose                                    |
| ------------------------------ | -------------------- | ------------------------------------------ |
| Trip / Manifest                | Technique (SQL)      | Identifies the shipment                    |
| BOL Number                     | Prophecy / auto-fill | Needed for accounting export               |
| Job Number                     | ALG Invoice          | Verification: should match manifest suffix |
| Tech Wgt / Pal / PCS           | VisualMail           | What SG360 shipped                         |
| ALG Wgt / Pal / PCS            | ALG Invoice          | What ALG recorded                          |
| Wgt Diff / Pal Diff / PCS Diff | Calculated           | Discrepancy flags                          |
| Calculated Cost                | Tariff + FSC         | What SG360 expected to pay                 |
| Invoice Amount                 | ALG Invoice          | What ALG actually charged                  |
| **Cost %**                     | Calculated           | **The key metric**                         |

Cost % color thresholds:
- 🟢 Green (95–105%) — looks right, approve
- 🟡 Yellow (90–95% or 105–110%) — worth checking
- 🔴 Red (<90% or >110%) — flag for investigation
this will be changed to 3%, I had just set it as this for earlier
Katie either **Approves** or **Flags** each record. Flagged records stay visible until resolved.

**Step 4 — Export**
When done reviewing, click **Finalize**. The system:
1. For records without a BOL → provides a button to generate the **Prophecy SID import file** so Katie can create the BOL in Prophecy
2. For all approved records → emails a summary CSV to Mary and Katie
After Katie imports the SID file and creates loads in Prophecy, the BOL numbers sync back into the dashboard automatically (once that connection is live).
This will then get uploaded to a log, and the data saved without a BOL will pull for a manifest number BOL the next day when manifests are being pulled

---
#### **Manifest Record Types**
**Type A — No BOL Yet (needs SID export)**
 warehouse hasn't created a load in Prophecy yet.
- Invoice Job Name matches the manifest trip number suffix (e.g., `TEC_T_0397246` → Job Name `397246)
- reviews, approves, then imports the SID file into Prophecy to create the BOL
- BOL number gets pulled back into the dashboard after creation

**Type B — BOL Already Exists**
The Wolf building creates loads in Prophecy before shipping, so these arrive with a BOL number already attached.
- Invoice Job Name matches an existing BOL number
- Katie just approves — no SID export needed
- BOL auto-fills from the initial query

**Comingle/other Loads**
Some loads mix multiple customers (`CM_` prefix). These appear in a **separate collapsible section** at the bottom of the dashboard — they don't have a direct Technique match and are reviewed separately.

#### **Dashboard Layout**

```
┌─────────────────────────────────────────────────────────────────┐
│  SG360 BOL Reconciliation                    [Pull Manifests]   │
│                                              [Pull Invoices]    │
│  📋 12 Manifests   📄 9 Invoices   ✅ 9 Matched   ⏳ 3 Pending  │
├─────────────────────────────────────────────────────────────────┤
│  [Search: trip / manifest / invoice / BOL / job number...]      │
├─────────────────────────────────────────────────────────────────┤
│  DASHBOARD  |  LOG                                              │
├─────────────────────────────────────────────────────────────────┤
│  ▼ Records Ready for Review (matched manifest + invoice)        │
│  [Trip] [Manifest] [BOL] [Job#] [Wgt] [Pal] [PCS] [Calc] [$]  │
│  ...rows...                                [Approve] [Flag]     │
├─────────────────────────────────────────────────────────────────┤
│  ▼ Manifests Waiting for Invoice                                │
│  [records with no invoice yet]                                  │
├─────────────────────────────────────────────────────────────────┤
│  ▼ Comingle (separate review)                                   │
│  [CM_ records]                                                  │
├─────────────────────────────────────────────────────────────────┤
│  ▼ Approved Today                                               │
│  [approved records]              [Export SID] [Send to Mary]    │
└─────────────────────────────────────────────────────────────────┘
```

**Log tab** shows only fully completed records (approved + sent to accounting). Date-range filterable.

#### **Calculated Cost Formula**

```
base_cost      = (cost_per_100lb × weight_lbs) / 100
base_tariff    = max(base_cost, minimum_freight)
fsc_pct        = fuel surcharge band for today's EIA diesel price
calculated_cost = base_tariff × (1 + fsc_pct)
```

- Tariff rate comes from the Access rate card (seeded into the database)
- FSC comes from ALG's fuel surcharge matrix (seeded into the database)
- Diesel price fetches live from the EIA API weekly
- Hovering over the calculated cost shows the full breakdown with real numbers

**Note:** Tariff lookup requires the 3-digit SCF ZIP zone for each manifest's destination. Currently pulling this from the ALG invoice CSV (`Destination` or `Zip` field). Need to confirm this is reliable.

---

#### **Features**

| Built                                    | Needs work                         |
| ---------------------------------------- | ---------------------------------- |
| Pull manifests from AWP-SQL-PROD         | Manifest filtering                 |
| Pull weights/pallets/pcs from VisualMail | Exporting format                   |
| Invoice CSV upload and matching          | Shared drive connection and format |
| Cost % calculation with color coding     |                                    |
| Approve / Flag / Unflag records          |                                    |
| Prophecy SID export                      |                                    |
| Email summary to Mary + Katie            |                                    |
| Audit log of all actions                 |                                    |
| Multi-invoice accumulation per trip      |                                    |
| Weight / pallet / PCS diff display       |                                    |
|                                          |                                    |

---

#### **Open Questions Regarding Documentation**
1. How far back should manifests go? current window is 10 days. Invoices can come in 11–18 days after despatch. Should we pull 20–30 days to make sure nothing gets missed?
2. How long does a record stay "pending" before it's considered stale? If a manifest has no invoice after 2 weeks, should it stay visible or be archived
3. Comingle loads -- right now these appear at the bottom in their own section with no calculated cost. Is there a separate process for reviewing these, or...
4. Date filtering for organization — should the default view show today only, or the last N days? What's most useful for the review?
5. Accounting export format — is the current 18-column CSV format what Mary expects, or does she need it in a different layout?
6. Any other information or design changes that you would like? Any processes that seem confusing?
7. What would happen with the PDF files, do those still need to be moved around?
8. Am I missing anything in your process that isn't a part of this method or any issues that you would probably encounter?
9. Can you define again how the matching situation works and how that is moved over?
