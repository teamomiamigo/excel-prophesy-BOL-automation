# Security Practices — SG360 BOL Reconciliation

> This document exists because the security team is watching and you should be able to show them exactly what you're doing and why it's safe.

---

## The Short Version (for showing to someone quickly)

- No production data in the repo — ever
- No credentials in code — ever
- All database access is read-only (goal)
- App runs locally or on internal infrastructure only
- Nothing goes to the cloud unless IT approves it

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
- `DATABASE_URL` — includes username and password
- `SMTP_USER` / `SMTP_PASSWORD` — O365 app password
- Any future API keys

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

When you get SQL access from Raj/IT:
- Request **SELECT-only** permissions on production databases
- Never ask for INSERT, UPDATE, DELETE on production tables
- The app only needs to read from Technique, Prophecy, and VisualMail
- The app writes only to its own PostgreSQL database (approval records, history)

### Service accounts vs personal credentials

- Do not use your personal Windows/AD credentials in the app's database connection string
- Ask Raj to create a service account (e.g. `sg360_app_readonly`) with SELECT-only permissions
- This account's credentials go in `.env` only — never in code

### SSMS (SQL Server Management Studio)

- You can use your personal credentials in SSMS for development/exploration — that's fine
- Those credentials never touch the application code or the repo
- When a query is ready, it goes into `/sql/` in the repo as a `.sql` file — no credentials embedded

---

## Database credentials in AWS

*(Added 2026-07-16, after a production outage caused by exactly this gap not being written down anywhere.)*

The app's own PostgreSQL database (Aurora Serverless v2, `sg360-bol-aurora`) currently gets its password from **`sg360-bol-live-credentials`**, an out-of-band secret (created manually, not tracked by Terraform — Terraform only knows its ARN/name as a literal string in `iam.tf`/`lambda.tf`) that also holds SMTP, on-prem SQL Server, and EIA API credentials.

**The actual root cause of the 2026-07-16 outage:** Aurora's real master password is separately AWS-managed and auto-rotated (`manage_master_user_password = true` in `terraform/main/aurora.tf`, name pattern `rds!cluster-...`), and nothing keeps `sg360-bol-live-credentials`'s copy in sync with it — so every auto-rotation (roughly weekly) silently breaks the DB connection until someone manually resyncs it (as was done that day; next rotation is due ~2026-07-23).

**The intended fix, built but not yet deployed:** `backend/config.py` supports reading the DB password directly from Aurora's own auto-managed secret at every cold start (no copy to go stale), via `RDS_MASTER_SECRET_ARN`/`DB_HOST`/`DB_PORT`/`DB_NAME`. Enabling it requires granting the deploying AWS user `iam:PutRolePolicy` on the `sg360-bol-lambda-exec` role — not currently available. Until that permission is granted and this is wired up in `terraform/main/lambda.tf`/`iam.tf`, **expect this outage to recur on the next auto-rotation** unless someone manually resyncs the secret first.

**Known gap:** `sg360-bol-live-credentials` being untracked by Terraform is a separate, non-urgent weakness — its contents can't be diffed/reviewed like the rest of the infrastructure. Bringing it under Terraform management (or splitting it into per-purpose secrets) is a reasonable future improvement, not yet done.

*Last reviewed: 2026-07-16.*

---

## The Repository

### What to commit
- Source code (`.py`, `.jsx`, `.js`, `.ts`)
- SQL query files (`/sql/*.sql`) — no embedded credentials
- Config templates (`.env.example`)
- Documentation (`README.md`, `CLAUDE.md`, `STATUS.md`, this file)
- Test files

### What never gets committed
- `.env` (real credentials)
- Any file containing a password, API key, or connection string with real credentials
- Production data exports (no CSV dumps of real BOL data)
- SSMS query results saved as files

### Branch strategy (keep it simple)
```
main          ← stable, working code
dev           ← active development
feature/xxx   ← individual features (optional)
```

Don't push directly to `main` once the app is in use by Katie.

---

## Running the App Safely

### During development (mock data)

```
USE_MOCK_DATA=True
```

This mode uses hardcoded test records. No database connection is made. No emails are sent — they log to console. Safe to run anywhere, share screenshots, demo to stakeholders.

### When connecting to real data

- Run only on your work machine or on internal company infrastructure
- Do not expose the app to the internet (no public URLs, no ngrok tunnels without IT approval)
- `DEBUG=False` in any non-local environment
- Email sending is only active when SMTP credentials are set — until then it logs

---

## What to Tell the Security Team

If asked, here's the one-paragraph answer:

*"I'm building an internal tool that automates a manual daily reconciliation process. During development it runs entirely on mock data — no production systems are touched. When we connect to real data, it will use read-only service accounts approved by IT on internal SQL Server databases (AWD-SQL-WH4, SQLAPPS3). No data leaves the corporate network. Credentials are stored in environment variables, not in code, and the repository contains no secrets. The deployment target is internal infrastructure, to be determined with Raj."*

---

## Checklist Before Sharing the Repo With Anyone

- [ ] `git log --all --full-diff -p -- .env` — confirms no `.env` in history
- [ ] `grep -r "password" --include="*.py" .` — no hardcoded passwords in Python
- [ ] `grep -r "password" --include="*.env" .` — only `.env.example` (with placeholder values) shows up
- [ ] `USE_MOCK_DATA=True` confirmed as the default in `.env.example`
- [ ] No production CSV files committed (check `/data/` or any exports)

---

*Last reviewed: 2026-07-16 (added "Database credentials in AWS" section)*
