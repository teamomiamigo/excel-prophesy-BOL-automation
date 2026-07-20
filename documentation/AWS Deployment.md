# AWS Deployment

> What's actually running today, how it got deployed, and what's still needed before this can move to production. Written for a mixed audience — skip to the section that's relevant to you.

*Last reviewed: 2026-07-20.*

---

## 1. Summary

The BOL Reconciliation app has been live on real AWS infrastructure since 2026-07-09 — this is a **development/test environment**, not production. It runs as a serverless backend (AWS Lambda) behind a web address (CloudFront), with its own small database (Aurora) and a scoped, approved network path back to SG360's on-prem SQL Server for live data. Everything was built and deployed using the author's existing dev-AWS access. Moving this to production is a separate step that requires sign-off and access from Security and DevOps — that's covered in [Section 6](#6-path-to-production).

---

## 2. Architecture at a glance

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

One detail worth knowing: Lambda normally only "wakes up" when it's called (and is slow the first time after being idle — a "cold start"). Because a cold start plus a live on-prem SQL query together were pushing past a hard 30-second timeout on the pull endpoint, one Lambda instance is now kept permanently warm (provisioned concurrency). That fixed the timeout but means this now runs continuously instead of purely on-demand.

---

## 3. How it gets deployed

One script, `deploy.ps1`, with two independent halves:

**Backend** (code changes): build a new container image → push it to ECR → run `terraform plan` → **stop**. A person reviews the plan and runs `terraform apply` themselves. This pause is intentional — infrastructure changes always get a human look before they land, the same way this environment was originally built up.

**Frontend** (UI changes): build the React app → sync the built files to S3 → invalidate CloudFront's cache. Fully automatic, no review gate — this only replaces static files and is trivially safe to redo.

There is no CI/CD pipeline — every deploy is run manually, from the author's own machine.

---

## 4. Security & access posture

*(For the security team's read — facts, not judgments.)*

- **Public exposure**: the CloudFront web address is reachable by anyone who has the URL. The firewall (WAF) in front of it is currently set to **allow all traffic** — this was a deliberate dev-only choice because testers' IP addresses kept rotating faster than an IP allowlist could track. An IP-allowlist rule already exists in the config but isn't the active policy. The URL itself isn't published or indexed anywhere, so today's protection is obscurity, not access control.
- **What the backend is allowed to do (IAM)**: the Lambda's permissions are narrow and explicit — write logs, attach to the VPC, read exactly one named secret from Secrets Manager, and read/write only the one S3 bucket that holds invoice files. No broad or wildcard permissions.
- **Credentials**: nothing is hardcoded. All passwords (database, email, on-prem SQL, EIA weather/fuel API) live in one AWS Secrets Manager secret. That secret was created by hand rather than through Terraform, so unlike the rest of the infrastructure, its contents aren't version-controlled or diffable.
- **Network path to on-prem SQL Server**: Lambda can only reach one specific on-prem SQL Server, on one specific port, and nothing else — this path was explicitly approved through a CHG (change) ticket before it was turned on, and is scoped to exactly what that ticket described.
- **Data residency**: the app's own database (Aurora) and file storage (S3) are both private, not publicly reachable, and live only in this AWS account.

---

## 5. Known issues & things to prepare for

Things that are currently fragile, half-finished, or acceptable for dev but not for production:

- **DNS is patched, not fixed.** Name resolution inside this AWS network doesn't work normally, so the app has a hardcoded list of IP addresses for the few services it needs to reach. If AWS ever changes those addresses, connections would silently start failing until the list is updated by hand.
- **The credentials secret isn't self-healing.** The database's real password rotates automatically on AWS's side, but the copy the app reads doesn't rotate with it — someone has to manually resync it. This already caused one outage. A proper fix has been written but isn't turned on yet (see next point).
- **That fix needs one more AWS permission.** The permission to finish wiring up the self-healing credential fix hasn't been granted yet. Until it is, the manual-resync outage can recur.
- **No automated alerting.** If the backend starts failing in production right now, nobody gets notified — someone would have to notice manually or go looking in the logs.
- **The database is set up like a dev database.** No final backup snapshot is kept if it's ever deleted, and it's allowed to scale down to zero capacity when idle (which adds a delay on the next request after a quiet period).
- **Infrastructure changes only work from one laptop.** The record of what's been deployed (Terraform's "state") lives only on the author's machine, not somewhere shared. Nobody else can safely make infrastructure changes right now, and losing that laptop would be a real problem.

---

## 6. Path to production

Everything above was built and deployed using the author's own **existing development AWS access** — it didn't require separate sign-off. Production is different: production AWS access is controlled by Security and DevOps, not something the author can grant themselves. This section is meant to be the actual ask handed to those teams — it isn't a self-serve checklist.

**Open decision (not yet made):** should production be a brand-new, separate AWS environment (its own backend, database, and web address, fully isolated from this dev/test one), or should this environment itself be hardened and promoted to production? A separate environment is the safer default — it means testing never risks real production data — but it's more setup work and hasn't been decided yet.

**What Security/DevOps involvement would need to cover, once that decision is made:**

1. **Grant the AWS permission** needed to finish the self-healing-credentials fix described above, so the recurring outage stops for good.
2. **Move infrastructure tracking off the author's laptop** and onto shared, locked storage, so more than one person can safely make changes.
3. **Decide the real production access model** — should this be reachable from anywhere, only from the corporate network, or only with a login? Today's "allow everyone" firewall setting is dev-only and would need to change.
4. **Confirm the network path to on-prem SQL Server for production** — does a new environment need its own approved path, or can it reuse the one already approved for dev?
5. **Harden the production database** — keep a final backup on deletion, set a real backup retention window, and decide whether it should stay warm at all times instead of scaling to zero.
6. **Turn on alerting** so failures notify someone automatically instead of going unnoticed.
7. **Formalize how credentials are stored** — bring the hand-created secret under the same version-controlled management as everything else, or split it apart by purpose.
8. **Get a real domain name and certificate** instead of the default AWS-provided web address.
9. **Decide whether manual, laptop-run deploys are acceptable for production**, or whether a reviewed, automated deploy pipeline is wanted instead.
