*created 2026-07-22*

---

## 1. Purpose

The AI agent layer (`backend/agents/`, `POST /api/agents/run`, the Agent Activity tab) is built and demoed locally, triggered by a manual "Run AI Agent" button. This document describes what it would take to move the trigger to a real schedule running on AWS — so the daily invoice review runs unattended, on the already-deployed Lambda, without depending on anyone's laptop being on.

This is deliberately **documentation, not code** — the local build was scoped to ship this week; this is the next step, to be picked up separately. Nothing here is built yet.

**Target end-state:** an AWS EventBridge Scheduler rule invokes the deployed Lambda's `POST /api/agents/run` on a cron (matching the existing documented 7/8/9am pull cadence in `CLAUDE.md`'s daily workflow), running the **full pipeline** — pull new invoices, classify, draft reasoning, email Katie — with no button click. Katie still reviews and confirms every action, either in the Agent Activity tab or via the email's one-click link; the agent still never mutates a record without a human's accept.

---

## 2. A real blocker to resolve first — invoice intake won't work unattended yet

`POST /api/agents/run`'s first step calls `poll_invoice_folder()` (`backend/main.py`), which is what actually finds new invoice CSVs. In live mode, that function scans `settings.INVOICE_FOLDER` — a Windows UNC path (`\\sg360-wbapp-prd\Logistics\AgentsInvoices\Invoices to Process`) — exclusively. There is no S3-listing branch on that path today.

**This matters because:** the deployed Lambda cannot reach a Windows UNC path at all (this is already a known, documented constraint elsewhere in this app — see the `cost-breakdown` route's history in `CLAUDE.md`, which hit the exact same wall). So a scheduled cloud run of `POST /api/agents/run` would call `poll_invoice_folder()`, find nothing (not an error — it would just silently report `{"found": 0, ...}`), and only ever classify whatever records were already pending from some other intake path. It would not actually replace the manual "pull invoices" step.

**What needs to happen before scheduling is worth turning on:**
- Add an S3-listing branch to `poll_invoice_folder()`, mirroring the pattern already used elsewhere in `main.py` for invoice PDFs (`settings.INVOICE_S3_BUCKET` — search `main.py` for the existing `if not settings.USE_MOCK_DATA and settings.INVOICE_S3_BUCKET:` blocks around the PDF-storage functions for the pattern to copy: list objects in the bucket, dedupe against already-known `invoice_number`s the same way the UNC-folder branch already does, and feed each new CSV through the same `_process_invoice_csv()` call).
- Something needs to actually put new invoice CSVs into that S3 bucket in the first place — today nothing does; CSVs only ever arrive via a human's manual `POST /api/invoices/upload` (a direct HTTP upload, not a folder drop). Either Tanya's invoice delivery mechanism changes to land files in S3 directly, or an intermediate step (e.g. a small Lambda/script watching the UNC share and mirroring new files to S3) is needed. This is a business-process question as much as a code one — worth a conversation with Phil/IT before assuming either direction.

**Until this is resolved, a reasonable interim schedule** is to invoke the pipeline in "classify what's already there" mode only — i.e., still on a cron, but understand it won't discover new invoices on its own; someone (or some other automated step) still has to get invoices into the system first via the existing upload path. This is a smaller, still-useful win (removes the manual "click Run AI Agent" step for review/email, even if intake stays manual) and doesn't require the S3 work above.

---

## 3. Infrastructure work — genuine Terraform, not app code

This is new work in `terraform/main/`, following the same pattern as everything else deployed there (`lambda.tf`, `apigateway.tf`, etc.) — **not** a change to `backend/` application code beyond the S3-listing fix in §2.

- **New resource:** an `aws_scheduler_schedule` (EventBridge Scheduler, the newer AWS API — prefer this over the older `aws_cloudwatch_event_rule` unless there's a reason to match an existing pattern elsewhere in this account) with a cron expression matching the 7/8/9am cadence, targeting the deployed API Gateway endpoint's `POST /api/agents/run` route (an `aws_scheduler_schedule` can target API Gateway directly via an EventBridge API destination, or target the Lambda function directly and have it construct its own internal request — the API Gateway path is simpler and exercises the exact same code path a manual click does, so prefer that unless a direct-Lambda-invoke turns out cleaner).
- **IAM:** the scheduler needs its own execution role with permission to invoke the API Gateway route (`execute-api:Invoke` scoped to the specific API/stage/route) or the Lambda function directly (`lambda:InvokeFunction`), depending on which target is chosen above. This is a new, narrowly-scoped role — it should **not** reuse `sg360-bol-lambda-exec` (the Lambda's own execution role), since the scheduler is a different principal invoking the function, not the function's own runtime identity.
- **Human-reviewed `terraform apply` gate stays in place.** Per `CLAUDE.md`'s existing deployment process and the `/deploy` skill, this change goes through the same `deploy.ps1` → human-reviewed `terraform apply` flow as every other infra change — it is not something to auto-apply.

---

## 4. Interaction with existing Lambda constraints

`CLAUDE.md` already documents several real, hard-won constraints on this Lambda that a scheduled `POST /api/agents/run` would inherit — worth re-reading in full before wiring this up, summarized here:

- **API Gateway's 30s integration timeout / Lambda's 29s function timeout.** The existing bulk `POST /api/admin/pull` route already had to have its own live wide-fallback search moved out (`_wide_fallback_technique_search()`) after repeatedly blowing this budget when stacked on top of the main Technique pull. `POST /api/agents/run` calls `poll_invoice_folder()` (which itself calls `_process_invoice_csv()` per file, which can trigger that same wide-fallback search for any newly-unmatched invoice) **and then** classifies every pending record **and then** sends an email — all synchronously, in one request. If invoice volume or an unlucky wide-fallback search pushes this past 29s on a cold day, the whole scheduled run fails ungracefully (a bare Lambda timeout, not a clean error). This is the single most likely real-world failure mode once this is scheduled — worth load-testing against a realistic invoice count before turning the schedule on, and worth considering whether the pipeline should be split into two scheduled steps (pull, then classify+email) if it proves too slow to do in one request.
- **Provisioned concurrency** (`lambda.tf`) already keeps one execution environment warm to avoid the ~13–23s cold-start cost stacking on top of live query latency. A scheduled invocation benefits from this exactly the same way a manual click does — no additional change needed here, just confirming it stays enabled.
- **The `lambda_sql_access` security group's `description` field is frozen** (see `terraform/main/lambda_sql_security_group.tf`'s incident writeup) — nothing about this scheduling work should touch that resource at all; it's unrelated, just noted so nobody conflates "add a scheduler" with "touch the security group" during a future Terraform diff review.

---

## 5. Data model — should just work, but hasn't been verified against real Aurora

`AgentRun`/`AgentProposal` are new SQLAlchemy tables (`backend/models.py`). Because they're brand-new (not columns added to an existing live table), `Base.metadata.create_all(bind=engine)` — already the first line of `lifespan()` — should create them automatically on the next cold start against Aurora, with no inline `ALTER TABLE` migration needed (that pattern is only for columns added to a table that's already live). This is the same mechanism every other table in this app relies on, so it's expected to just work — but it has only been exercised in mock mode so far in this pass. Confirm it against a real (non-production, ideally) Postgres instance before relying on it in the cloud, the same way any other schema change here would be spot-checked.

---

## 6. Suggested order of work

1. Resolve the invoice-intake question in §2 first — either build the S3-listing branch, or explicitly decide the schedule will only classify/email, not discover new invoices, and document that choice.
2. Load-test `POST /api/agents/run` locally against a realistic invoice/record count to get a real sense of its latency before assuming it fits in 29s on AWS.
3. Write the Terraform for the scheduler + its IAM role (§3), scoped as narrowly as possible.
4. Deploy via the normal `/deploy` flow, with the schedule initially pointed at a harmless time (e.g. an off-hours test run) before switching it to the real 7/8/9am cadence.
5. Verify one real scheduled run end-to-end against Aurora (§5) before trusting it to replace the manual button for good.
