# SG360 BOL Reconciliation — Project Onboarding

*updated 2026-07-22*

This document is meant to let anyone — technical or not — pick up this project cold. If Nikhil is unavailable, this is where to start.

---

## What this is

An internal web app that replaces a manual daily Excel process at SG360 (commercial printing). Every morning, freight billing has to be reconciled across three separate sources: the shipping system (Technique/Visual Mail), the freight carrier's invoice (ALG Worldwide), and SG360's own tariff rate table. That reconciliation used to be done by hand in Excel. This app pulls all three sources together, calculates the expected cost automatically, flags discrepancies, and lets the logistics coordinator approve records with one click instead of manually cross-referencing spreadsheets.

## Who it's for

| Person | Role |
|---|---|
| **Katie** | SG360 logistics coordinator — reviews the dashboard every morning, approves or flags each freight record |
| **Mary** | SG360 accounting — receives the approved billing summary by email after Katie signs off |
| **Tanya** (ALG Worldwide) | External carrier contact — sends the daily invoice data referenced in reconciliation |
| **Marge** | Wrote the original SQL queries against the shipping system; source of truth on what data is accessible |
| **Megha** | Knows the Prophecy (BOL creation) system internals |
| **Phil** | Logistics lead; owns the relationship with ALG Worldwide |

## How it runs today

- **Status: live and deployed on AWS, actively being tested against real data — not yet Katie's official daily driver.** Since 2026-07-09, the backend has run as a Lambda function (behind API Gateway) with an Aurora Serverless v2 Postgres database, and the frontend is a static site on S3 behind CloudFront. It's been redeployed many times since as bugs were found and fixed against real invoices and real Technique data.
- The deployment target question is settled: AWS Lambda/API Gateway/Aurora/CloudFront/S3, all defined in Terraform (`terraform/main/`) and deployed via a `deploy.ps1` script (wrapped by the `/deploy` skill for anyone using Claude Code on this repo).
- It also still runs perfectly well locally on a developer machine — two processes (Python backend + React frontend) started together with one script (`start.ps1`) — which is how day-to-day development and testing happens before anything gets deployed.
- A `USE_MOCK_DATA` switch lets the whole app run against realistic fake data with no database or company network access at all — safe to run and demo anywhere, and how most feature work gets built before it's tried against real data.
- When pointed at real data (`USE_MOCK_DATA=False`), it connects to SG360's internal SQL Server systems and its own PostgreSQL database (see **Data sources & connections** below) — true both locally and in the live AWS deployment.

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.13, FastAPI (web framework), SQLAlchemy 2.0 (database layer); packaged as a Lambda container image in production |
| Frontend | React 18, built with Vite, no UI framework (hand-styled); served as a static build from S3/CloudFront in production |
| Database | PostgreSQL — the app's own data only (approval records, history, rate tables); it does not write to any company system. Local Postgres in dev, Aurora Serverless v2 in production |
| Email | Standard SMTP (Office 365) for sending reports; IMAP polling exists but manual upload/"poll folder" are the real daily intake path (see below) |

## Data sources & connections

This is the part most relevant for "what does this touch and is it safe."

| Source | What it provides | Access type |
|---|---|---|
| **AWP-SQL-PROD** (SQL Server) | Shipping trip/manifest data from Technique and VisualMail directly, plus Prophecy/ShipperPlus BOL and load numbers via its `SQLAPPS3` linked server (there is no separate direct connection to a Prophecy-hosting server — an earlier attempt at one was removed 2026-07-20 once it was confirmed the server it targeted doesn't actually host that data) | Read-only (SELECT-only service account) |
| **ALG Worldwide invoices** | Freight billing amounts, referenced by "Z-number" | Delivered as email attachments (CSV) or uploaded manually — matched against Technique data automatically, not a live query to ALG |
| **SG360 internal tariff rate table + a more complete ALG-sourced rate table** | The company's contracted freight rates, loaded once from a spreadsheet/CSV export | One-time import into the app's own database, not a live connection |
| **EIA.gov (US Energy Information Administration)** | Weekly diesel fuel price — used only as a fallback fuel-surcharge source when an invoice's own fuel-surcharge line can't be parsed | Public API, free tier, requires a registered API key |
| **Office 365 mailbox** | Can read the carrier's invoice emails automatically (IMAP) and sends the daily billing summary to accounting (SMTP); in practice, the live workflow uses manual "Upload Invoices" / "Poll Folder" as the real daily intake, not automated email polling | IMAP (read) + SMTP (send), app-specific password, not a personal account password |

**No production data ever lives in this code repository.** Real invoice/shipment exports used for testing are excluded from version control by design.

## Core features

- **Daily reconciliation dashboard** — surfaces new invoices matched against shipping data automatically, side by side
- **Automatic cost calculation** — computes what the freight *should* cost from ALG's own per-zone rates (with SG360's contracted rate table as a fallback) plus the invoice's own fuel surcharge, and flags anything more than ~3–6% off from what the carrier actually billed
- **One-click approve / flag / mark third-party / "Do Not Pay"** workflow per record, with a full audit history (renamed from "Ignore" 2026-07-15 — do-not-pay records stay visible in their sender's batch rather than being hidden away)
- **Invoice matching** — automatically pairs incoming carrier invoices to the correct shipment as soon as they're uploaded, including an automatic wider search for older/harder-to-match trips, with a manual "⚖ Compare" tool for the rare ambiguous case (a trip that split into more than one shipment manifest)
- **Export to accounting** — generates and emails the approved daily billing summary
- **Export to Prophecy** — generates the import file the coordinator uses to create official Bill of Lading numbers in the company's Prophecy system
- **Historical log** — a searchable record of every reconciled shipment, past and present

## Where to learn more

| Document | Purpose |
|---|---|
| [`README.md`](README.md) | Local setup instructions — installing and running the app on your own machine |
| [`CLAUDE.md`](CLAUDE.md) | Full technical reference: architecture, API routes, business rules, known issues, live AWS deployment details — written for whoever (human or AI assistant) is actively developing this code |
| [`documentation/Developmental Documentation.md`](documentation/Developmental%20Documentation.md) | **Running changelog** — a dated entry for every fix or feature shipped, with what changed and why. This is the fastest way to see what's happened recently without reading commit history. |
| [`documentation/SECURITY.md`](documentation/SECURITY.md) | How credentials, database access, and production data are handled — including the live AWS deployment's public exposure |
| [`documentation/AWS Deployment.md`](documentation/AWS%20Deployment.md) | What's actually running in AWS today, how it was built, and what's still rough around the edges |
| `documentation/` (other files) | Original business requirements and workflow design notes |

## Recent changes

See [`documentation/Developmental Documentation.md`](documentation/Developmental%20Documentation.md) for the complete, continuously-updated changelog. Highlights since this file was last updated (2026-07-01):

- **Went live on AWS** (2026-07-09) — Lambda + API Gateway + Aurora Serverless v2 backend, S3 + CloudFront frontend; redeployed repeatedly since as real invoices and real Technique data surfaced bugs
- **Invoice-matching and cost-calculation accuracy overhaul** — a much more complete ALG-sourced rate table, a corrected minimum-freight-charge floor, precise fuel-surcharge parsing, and the Cost % variance formula itself settled on `amount / access_prog` after some back-and-forth
- **Removed the daily bulk "Pull Manifests" step entirely** (2026-07-22) — new trip/manifest data is now discovered automatically per-invoice at match time instead of a scheduled pull
- **Ambiguous-trip handling** — when one shipping trip splits into more than one manifest, the dashboard now flags it and gives Katie a side-by-side "⚖ Compare" tool to pick the right one, instead of silently guessing
- **"Ignore" renamed to "Do Not Pay"** (2026-07-15) — a do-not-pay invoice now stays visible in its sender's batch rather than being tucked away in a separate list
- Ongoing dashboard polish (a 7-phase improvement plan) — inline note editing, bulk actions, sortable columns, per-record Prophecy export/BOL refresh, and more
