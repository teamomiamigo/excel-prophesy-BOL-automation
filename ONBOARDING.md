# SG360 BOL Reconciliation — Project Onboarding

*updated 2026-07-01*

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

- **Status: development, not yet in daily production use.** It runs locally on a developer machine today; there is no live deployment yet.
- Two processes: a Python backend (API + business logic) and a React frontend (the dashboard Katie would use), started together with one script (`start.ps1`).
- A `USE_MOCK_DATA` switch lets the whole app run against realistic fake data with no database or company network access at all — this is how most day-to-day development happens, and it's safe to run and demo anywhere.
- When pointed at real data (`USE_MOCK_DATA=False`), it connects to SG360's internal SQL Server systems and its own PostgreSQL database (see **Data sources & connections** below).
- No production deployment target (cloud vs. on-prem) has been finalized yet — the database layer is already written to work unmodified against AWS RDS Postgres if that's the direction chosen; that's the only infrastructure decision made so far.

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.13, FastAPI (web framework), SQLAlchemy 2.0 (database layer) |
| Frontend | React 18, built with Vite, no UI framework (hand-styled) |
| Database | PostgreSQL — the app's own data only (approval records, history, rate tables); it does not write to any company system |
| Email | Standard SMTP (Office 365) for sending reports; IMAP for reading the carrier's invoice emails |

## Data sources & connections

This is the part most relevant for "what does this touch and is it safe."

| Source | What it provides | Access type |
|---|---|---|
| **AWP-SQL-PROD** (SQL Server) | Shipping trip/manifest data from Technique and VisualMail, via linked servers | Read-only (SELECT-only), intended to use a dedicated service account rather than personal credentials |
| **SG360-TECH-PRD1 / SQLAPPS3** (SQL Server) | Prophecy/ShipperPlus data — BOL and load numbers | Read-only |
| **ALG Worldwide invoices** | Freight billing amounts, referenced by "Z-number" | Delivered as email attachments (CSV) or uploaded manually — not a live system connection |
| **SG360 internal tariff rate table** | The company's contracted freight rates, loaded once from a spreadsheet | One-time import into the app's own database, not a live connection |
| **EIA.gov (US Energy Information Administration)** | Weekly diesel fuel price, used to calculate the fuel surcharge on freight | Public API, free tier, requires a registered API key |
| **Office 365 mailbox** | Reads the carrier's invoice emails automatically; sends the daily billing summary to accounting | IMAP (read) + SMTP (send), app-specific password, not a personal account password |

**No production data ever lives in this code repository.** Real invoice/shipment exports used for testing are excluded from version control by design.

## Core features

- **Daily reconciliation dashboard** — pulls in the morning's shipping and invoice data automatically and shows it side by side
- **Automatic cost calculation** — computes what the freight *should* cost from the contracted tariff + current fuel surcharge, and flags anything more than ~3–6% off from what the carrier actually billed
- **One-click approve / flag / mark third-party / ignore** workflow per record, with a full audit history
- **Invoice matching** — automatically pairs incoming carrier invoices to the correct shipment, even when they arrive out of order
- **Export to accounting** — generates and emails the approved daily billing summary
- **Export to Prophecy** — generates the import file the coordinator uses to create official Bill of Lading numbers in the company's Prophecy system
- **Historical log** — a searchable record of every reconciled shipment, past and present

## Where to learn more

| Document | Purpose |
|---|---|
| [`README.md`](README.md) | Local setup instructions — installing and running the app on your own machine |
| [`CLAUDE.md`](CLAUDE.md) | Full technical reference: architecture, API routes, business rules, known issues — written for whoever (human or AI assistant) is actively developing this code |
| [`documentation/Developmental Documentation.md`](documentation/Developmental%20Documentation.md) | **Running changelog** — a dated entry for every fix or feature shipped, with what changed and why. This is the fastest way to see what's happened recently without reading commit history. |
| [`documentation/SECURITY.md`](documentation/SECURITY.md) | How credentials, database access, and production data are handled |
| `documentation/` (other files) | Original business requirements and workflow design notes |

## Recent changes

See [`documentation/Developmental Documentation.md`](documentation/Developmental%20Documentation.md) for the complete, continuously-updated changelog. Highlights so far:

- Restructured the project's internal documentation so every future fix gets a short, dated changelog entry — the goal of this file (and that one) is exactly this kind of continuity
- Fixed a dashboard bug where approving/flagging a shipment record would jump the page back to the top, disrupting review of a long list
