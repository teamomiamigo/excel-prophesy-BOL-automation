# Security Practices — SG360 BOL Reconciliation

> This document exists because the security team is watching and you should be able to show them exactly what you're doing and why it's safe.

---

## The Short Version (for showing to someone quickly)

- No production data in the repo — ever
- No credentials in code — ever
- All production database access is read-only (SELECT-only service account)
- The backend runs on AWS (Lambda + API Gateway + Aurora Serverless v2); the frontend is on S3 + CloudFront — both public endpoints, not internal-only
- The AWS deployment was built using the author's existing development AWS access — there is no separate IT/security sign-off process for it today

---

## Credentials and Secrets

### The rule: `.env` stays off GitHub

Your `.env` file holds database URLs, SMTP passwords, and any API keys. It must never be committed.

Verify your `.gitignore` contains:
```
.env
*.env
.env.*
```

**Check before every commit:**
```bash
git status
# .env should never appear in "Changes to be committed"
```

If you accidentally committed a secret, rotate it immediately — don't just delete the file from the next commit. The secret is in git history.

### What goes in `.env` (and nowhere else)

See `CLAUDE.md`'s ".env quick-start" section for the current, full list of keys (it covers a lot more than it used to — `SQLSERVER_*`, `EIA_API_KEY`, IMAP/`ALG_SENDER_EMAIL`, `INVOICE_S3_BUCKET`, etc.) rather than duplicating that list here where it will drift. In production (AWS Lambda), none of these come from `.env` at all — see "Database credentials in AWS" below.

### What is safe to commit
- `.env.example` — a template with placeholder values, no real secrets:
  ```
  USE_MOCK_DATA=True
  DATABASE_URL=postgresql://user:password@localhost:5432/sg360_bol
  SMTP_USER=
  SMTP_PASSWORD=
  EMAIL_TO_MARY=["mary@sg360.com"]
  ```

---

## Database Access

### Principle: read-only where possible

- Production database access is **SELECT-only** — confirmed live and working against AWP-SQL-PROD (VisualMail/Technique) and the SQLAPPS3 linked server (ShipperPlus/Prophecy)
- Never request INSERT, UPDATE, DELETE on production tables
- The app only reads from Technique, Prophecy, and VisualMail
- The app writes only to its own PostgreSQL database (approval records, history) — Aurora Serverless v2 in production, local Postgres in dev

### Service accounts vs personal credentials

- Do not use your personal Windows/AD credentials in the app's database connection string
- Locally, `SQLSERVER_USER`/`SQLSERVER_PASSWORD` blank means Windows auth to AWP-SQL-PROD; in production the Lambda uses whatever service credentials are in the Secrets Manager secret (see below)
- These credentials go in `.env` (local) or Secrets Manager (production) only — never in code

### SSMS (SQL Server Management Studio)

- You can use your personal credentials in SSMS for development/exploration — that's fine
- Those credentials never touch the application code or the repo
- There's no `/sql/` directory convention in this repo — ad hoc queries developed in SSMS get folded directly into `data_layer.py` once confirmed working, not checked in separately

---

## Database credentials in AWS

*(Added 2026-07-16, after a production outage caused by exactly this gap not being written down anywhere.)*

The app's own PostgreSQL database (Aurora Serverless v2, `sg360-bol-aurora`) currently gets its password from **`sg360-bol-live-credentials`**, an out-of-band secret (created manually, not tracked by Terraform — Terraform only knows its ARN/name as a literal string in `iam.tf`/`lambda.tf`) that also holds SMTP, on-prem SQL Server, and EIA API credentials.

**The actual root cause of the 2026-07-16 outage:** Aurora's real master password is separately AWS-managed and auto-rotated (`manage_master_user_password = true` in `terraform/main/aurora.tf`, name pattern `rds!cluster-...`), and nothing keeps `sg360-bol-live-credentials`'s copy in sync with it — so every auto-rotation (roughly weekly) silently breaks the DB connection until someone manually resyncs it (as was done that day; next rotation is due ~2026-07-23).

**The intended fix, built but not yet deployed:** `backend/config.py` supports reading the DB password directly from Aurora's own auto-managed secret at every cold start (no copy to go stale), via `RDS_MASTER_SECRET_ARN`/`DB_HOST`/`DB_PORT`/`DB_NAME`. Enabling it requires granting the deploying AWS user `iam:PutRolePolicy` on the `sg360-bol-lambda-exec` role — not currently available. Until that permission is granted and this is wired up in `terraform/main/lambda.tf`/`iam.tf`, **expect this outage to recur on the next auto-rotation** unless someone manually resyncs the secret first.

**Known gap:** `sg360-bol-live-credentials` being untracked by Terraform is a separate, non-urgent weakness — its contents can't be diffed/reviewed like the rest of the infrastructure. Bringing it under Terraform management (or splitting it into per-purpose secrets) is a reasonable future improvement, not yet done.

*Last reviewed: 2026-07-16.*

---

## Public exposure (live deployment)

*(Added 2026-07-22 — this section previously didn't exist, and other parts of this doc still claimed the app was internal-only even after the deployment described here went live 2026-07-09.)*

The app has a real public footprint on the internet, not just internal infrastructure:

- **Frontend**: S3 bucket (`sg360-bol-frontend`) behind CloudFront — a public HTTPS URL.
- **Backend**: API Gateway HTTP API in front of the Lambda function — also a public HTTPS URL.
- **WAF**: a CloudFront-scoped WAFv2 web ACL currently has `default_action = allow{}` (opened 2026-07-14 for testing — testers' egress IPs rotate through a NAT/VPN faster than an IP allowlist could track). **This is obscurity, not access control** — the CloudFront URL isn't linked or indexed anywhere, but anyone with the URL can reach it. The IP-allowlist rule is left intact in `terraform/main/waf.tf` and should be flipped back to `block{}` before any real production rollout.
- Everything on-prem (AWP-SQL-PROD, SQLAPPS3) is still reached read-only, and no production data lives in this repo or in front-end code — only in Aurora and in the on-prem systems themselves.

See `documentation/AWS Deployment.md` for the full infrastructure writeup, including the security-group/IAM details.

---

## The Repository

### What to commit
- Source code (`.py`, `.jsx`, `.js`, `.ts`)
- Config templates (`.env.example`)
- Documentation (`README.md`, `CLAUDE.md`, this file, and the rest of `documentation/`)
- Test files

### What never gets committed
- `.env` (real credentials)
- Any file containing a password, API key, or connection string with real credentials
- Production data exports (no CSV dumps of real BOL data) — `test_invoices_*/` is gitignored for this reason
- SSMS query results saved as files

### Branch strategy

In practice, work lands on feature/iteration branches (e.g. `development-iteration-5`) that get merged into `main` via PR — there's no separate long-lived `dev` branch. Don't push directly to `main` once Katie is using the app day-to-day.

---

## Running the App Safely

### During development (mock data)

```
USE_MOCK_DATA=True
```

This mode uses hardcoded test records. No database connection is made. No emails are sent — they log to console. Safe to run anywhere, share screenshots, demo to stakeholders.

### When connecting to real data

- Locally: this still only touches production systems read-only (SELECT-only service account), same as the live deployment
- `DEBUG=False` in any non-local environment
- Email sending is only active when SMTP credentials are set — until then it logs
- The live AWS deployment (see "Public exposure" above) is already internet-reachable — there's no separate "should I expose this" decision left to make for that environment; the open question is tightening the WAF allowlist before real production rollout, not whether to go public at all

---

## What to Tell the Security Team

If asked, here's the one-paragraph answer:

*"This is an internal tool that automates a manual daily reconciliation process. It's deployed on AWS — a Lambda-based API (behind API Gateway) with an Aurora Serverless v2 Postgres database, and a static frontend on S3/CloudFront — built using the developer's existing AWS access, without a separate infrastructure decision or IT sign-off process yet. It reads real data read-only from internal SQL Server systems (AWP-SQL-PROD, and ShipperPlus/Prophecy via the SQLAPPS3 linked server) using a SELECT-only service account; no data leaves the corporate network except into this AWS account. Credentials are stored in environment variables locally and in AWS Secrets Manager in production — never in code — and the repository contains no secrets. The CloudFront/API Gateway endpoints are public HTTPS URLs; a WAF is in place but currently set to allow all traffic (obscurity, not access control) while in active testing, with an IP-allowlist rule ready to enable before a real production rollout."*

---

## Checklist Before Sharing the Repo With Anyone

- [ ] `git log --all --full-diff -p -- .env` — confirms no `.env` in history
- [ ] `grep -r "password" --include="*.py" .` — no hardcoded passwords in Python
- [ ] `grep -r "password" --include="*.env" .` — only `.env.example` (with placeholder values) shows up
- [ ] `USE_MOCK_DATA=True` confirmed as the default in `.env.example`
- [ ] No production CSV files committed (check `test_invoices_*/` is actually gitignored, not just present)

---

*Last reviewed: 2026-07-22 (corrected "internal infrastructure only" / "no public URLs" claims — both contradicted by the live AWS deployment since 2026-07-09; added "Public exposure" section, fixed AWD-SQL-WH4 → AWP-SQL-PROD, removed the non-existent `/sql/` directory convention, updated branch strategy to match actual practice)*
