# AWS Deployment

> Status update on the AWS deployment: what's live, how it was built, and what's needed next. Mixed audience (leadership / security / DevOps) — skip to the section relevant to you.

*Last reviewed: 2026-07-22.*

---

## 1. What's Live Today

The BOL Reconciliation app has been running on real AWS infrastructure since 2026-07-09 — this is a **development/test environment**, not production. It runs as a serverless backend behind a public web address, with its own small database and a scoped, already-approved network path back to SG360's on-prem SQL Server for live data. Everything here was built and deployed using the author's existing dev-AWS access; moving to production is a separate step covered in [Section 3](#3-whats-next).

The pieces:

| Piece | AWS service | What it's for |
|---|---|---|
| Frontend | S3 + CloudFront | Serves the React dashboard as static files |
| Backend | Lambda (container image) | Runs the FastAPI app — all the `/api/*` logic |
| API entry point | API Gateway | Routes web requests into the Lambda |
| App's own database | Aurora Serverless (Postgres) | Stores approvals, flags, notes — the app's own data |
| Invoice PDFs | S3 (private bucket) | Stores uploaded ALG invoice files |
| Firewall | WAF | Sits in front of CloudFront, filters traffic |
| Credentials | Secrets Manager | Holds DB/SMTP/SQL passwords — nothing in code |
| Image storage | ECR | Holds the built backend container images |

**Two request paths, same web address:**
- **Loading the dashboard** → CloudFront serves cached static files straight from S3. Fast, no backend involved.
- **Clicking anything (approve, upload, pull data, etc.)** → CloudFront forwards `/api/*` to API Gateway → API Gateway invokes Lambda → Lambda runs the same FastAPI backend code that runs locally → depending on the action, it talks to Aurora (app data), the on-prem SQL Server (live Technique/Prophecy data), Secrets Manager (credentials), or S3 (invoice files).

One Lambda instance is currently kept permanently warm (provisioned concurrency) rather than purely on-demand — a cold start plus a live on-prem SQL query together were pushing past a hard 30-second platform timeout. That was originally diagnosed against the daily bulk "pull manifests" endpoint, which has since been removed entirely (2026-07-22) in favor of discovering manifest data per-invoice instead — but the same cold-start-plus-live-query risk still applies to the endpoints that replaced it (invoice retry-match, per-record BOL refresh), so provisioned concurrency is still doing real work, just for a different set of routes than originally. That fixed the timeout but means this now runs continuously instead of purely on-demand; see Section 3 for lower-cost alternatives worth evaluating.

A related fix (2026-07-21): the on-prem SQL connection itself now has an 8-second connect timeout and an opt-in per-query timeout, instead of relying on pyodbc's 30-second default — a slow/unreachable on-prem connection during a live invoice search could otherwise guarantee an ungraceful Lambda kill, since 30s alone exceeds the Lambda's own 29-second hard timeout.

---

## 2. How It Was Built & Deployed

**Deploy process** — one script, `deploy.ps1`, two independent halves:
- **Backend** (code changes): build a new container image → push to ECR → run `terraform plan` → **stop**. A person reviews the plan and runs `terraform apply` themselves — a deliberate pause, since infrastructure changes always get a human look before landing.
- **Frontend** (UI changes): build the React app → sync to S3 (in two passes — hashed assets first with a long, immutable cache lifetime, then `index.html` last with caching disabled entirely, added 2026-07-22 so a page load never gets a stale `index.html` pointing at assets that no longer exist) → invalidate CloudFront's cache. Fully automatic — this only replaces static files and is trivially safe to redo.

There is no CI/CD pipeline today — every deploy is run manually, from the author's own machine.

**Security & access posture** *(for the security team's read — facts, not judgments)*:
- **Public exposure**: the CloudFront address is reachable by anyone with the URL. The firewall (WAF) in front of it is currently set to **allow all traffic** — a deliberate dev-only choice, since testers' IP addresses kept rotating faster than an IP allowlist could track. An IP-allowlist rule exists in the config but isn't the active policy today. The URL isn't published or indexed anywhere, so today's protection is obscurity, not access control.
- **What the backend is allowed to do (IAM)**: narrow and explicit — write logs, attach to the VPC, read exactly one named secret from Secrets Manager, read/write only the one S3 bucket holding invoice files. No broad or wildcard permissions.
- **Credentials**: nothing hardcoded. All passwords (database, email, on-prem SQL, EIA API) live in one Secrets Manager secret. That secret was created by hand rather than through Terraform, so unlike the rest of the infrastructure, its contents aren't version-controlled or diffable.
- **Network path to on-prem SQL Server**: Lambda can reach exactly one on-prem SQL Server, on one port, nothing else — explicitly approved through a CHG ticket before being turned on, scoped to exactly what that ticket described.
- **Data residency**: the app's own database and file storage are both private, not publicly reachable, and live only in this AWS account.

---

## 3. What's Next

### Known issues to prepare for

Things currently fragile, half-finished, or acceptable for dev but not production:

- **DNS is patched, not fixed** — the app hardcodes IP addresses for the few services it needs, because normal name resolution doesn't work in this network. If AWS ever changes those addresses, connections would silently fail until the list is updated by hand.
- **The credentials secret isn't self-healing** — the database's real password auto-rotates, but the app's copy doesn't rotate with it; someone has to manually resync it. Already caused one outage. A proper fix is written but not turned on (see below).
- **No automated alerting** — if the backend fails right now, nobody is notified automatically.
- **The database is set up like a dev database** — no final backup snapshot kept if deleted, and it scales down to zero capacity when idle (adds delay on the next request after a quiet period).
- **Infrastructure changes only work from one laptop** — the record of what's deployed (Terraform's state) lives only on the author's machine. Nobody else can safely make infrastructure changes right now, and losing that laptop would be a real problem.

### Moving to production — the actual ask

Everything above was built using the author's own **existing development AWS access** — it didn't require separate sign-off. Production is different: production AWS access is controlled by Security and DevOps, not something the author can grant themselves. This is meant to be the ask handed to those teams, not a self-serve checklist.

**Open decision (not yet made):** brand-new, separate AWS environment for production (its own backend, database, web address — fully isolated from this dev/test one), or harden and promote this environment itself? Separate is the safer default — testing never risks real production data — but it's more setup work, and hasn't been decided yet.

**Proposed IAM permissions for the production Lambda role** (same shape as dev — least privilege, nothing broader):
- Write logs to CloudWatch (standard minimum for any Lambda)
- Manage network interfaces (required to attach to a VPC at all)
- Read exactly one named secret from Secrets Manager — not "read any secret"
- Read/write exactly one S3 bucket (invoices only)

Separately, whoever deploys production infrastructure will need `iam:PutRolePolicy` on the Lambda execution role — this is about *that person's* own AWS permissions (so Terraform can modify the role), not the Lambda's own grants above. This unblocks finishing the self-healing-credentials fix described below.

**Checklist:**

1. **Grant the AWS permission** (`iam:PutRolePolicy`) needed to finish the self-healing-credentials fix, so the recurring manual-resync outage stops for good.
2. **Move infrastructure tracking off the author's laptop** onto shared, locked storage, so more than one person can safely make changes.
3. **Decide the real production access model** — reachable from anywhere, only the corporate network/VPN, or only with a login? Today's "allow everyone" firewall is dev-only. (A `users` table already exists in the schema, unused so far, if a login wall is the direction chosen.)
4. **Confirm the network path to on-prem SQL Server for production** — does a new environment need its own approved path, or can it reuse the one already approved for dev?
5. **Harden the production database** — keep a final backup on deletion, set a real backup retention window, decide whether it should stay warm at all times instead of scaling to zero.
6. **Turn on alerting** so failures notify someone automatically.
7. **Formalize how credentials are stored** — bring the hand-created secret under the same version-controlled management as everything else, or split it apart by purpose.
8. **Get a real domain name and certificate** instead of the default AWS-provided web address.
9. **Decide whether manual, laptop-run deploys are acceptable for production**, or whether a reviewed, automated pipeline is wanted instead.
10. **Reconsider the always-on Lambda cost** — evaluate whether an async request pattern or a business-hours-only warm schedule can replace paying for a permanently warm instance 24/7.

**Likely CHG tickets** (a starting list — confirm actual granularity with security/DevOps):
- Network path from a production Lambda/VPC to on-prem AWP-SQL-PROD (new ticket if a separate environment; confirmation-only if reusing the dev-approved path)
- WAF policy change — moving off "allow everyone" to the real access model
- The `iam:PutRolePolicy` grant, if it goes through formal change management
- DNS/domain registration and certificate issuance
- Terraform remote-state backend creation (S3 bucket + locking)
- CloudWatch alarm + SNS topic for alerting
