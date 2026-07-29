*created 2026-07-01, rewritten 2026-07-23 to reflect what actually shipped 2026-07-22 (commits `352a16d`, `42413b8`) vs. what was only ever designed. Status tags per section follow `AI Development and Documentation Standard.md`'s Rule 2: `[PLANNED]` designed, not built · `[BUILT]` implemented, mock-mode verified · `[DEPLOYED]` verified live. Keep these accurate — update in the same commit that changes what's true, not later.*

---

## 1. Purpose `[BUILT — first task shipped, narrower scope than originally designed]`

This document designs an agentic AI layer for the SG360 BOL Reconciliation app — a system that can (a) automate operational work Katie currently does by hand, and (b) later produce analytical output (variance narratives, trend reports), without requiring a redesign each time a new automatable task is identified.

It is scoped to **extend the existing repo** (`backend/`, `frontend/`), not replace any part of it. It reuses existing conventions — `USE_MOCK_DATA`, the inline-migration pattern in `main.py`'s lifespan, the `approval_history` audit precedent, the approve/flag review pattern already in the dashboard — rather than introducing a parallel framework.

**What actually shipped 2026-07-22 is narrower and simpler than what §7/§9 originally designed**: one task — classify every pending record's review recommendation (approve / needs-review / flag) — built as a direct, non-pluggable pipeline rather than the registry-based multi-task system this document originally specified. See §7 and §9 for exactly what diverged and why that's an open decision, not a mistake to silently correct.

---

## 2. Design principles `[BUILT — the trust-boundary principles below all held; the extensibility principle did not]`

- **Human-in-the-loop by default.** `[BUILT]` Katie already reviews and approves/flags every record; the agent extends that pattern rather than bypassing it. It never mutates a `BOLRecord`, sends an external email, or triggers an export on its own — it produces a **Proposal** that a human accepts or rejects. Confirmed in the actual code: accepting a proposal calls the exact same `approve_bol()`/`flag_bol()` functions a human's manual click already uses — no parallel mutation path exists.
- **Reuse the data layer, don't bypass it.** `[BUILT]` The classifier reads the same `BOLRecord` fields (via `BOLSummary`-shaped dicts) the dashboard already uses; the pipeline calls the existing `poll_invoice_folder()` for intake. No agent-specific SQL connection or credential exists.
- **Read-only against production sources, same as today.** `[BUILT]` Nothing in the shipped code adds a new write path into company systems.
- **Everything auditable.** `[BUILT]` Every run and every proposal is a persisted row (`agent_runs`, `agent_proposals`) — not a log line that scrolls away. Gap: a run's `status` is currently hardcoded to succeeded regardless of what happened inside it — see §12.
- **Mock-mode parity.** `[BUILT]` Fully demoable under `USE_MOCK_DATA=True`; never verified against a real database yet (§13).
- **Additive extension — "adding a new task means writing one new file, never editing an engine file."** `[PLANNED, NOT BUILT — see §7]` This is the one design principle that did **not** survive into the actual implementation. Today, adding a second task type means directly editing `classifier.py`'s branch logic and `pipeline.py` — there is no registry, no decorator, no per-task file. Not a defect in what shipped (it correctly solves the one task it was built for), but a real, current limitation against this original goal.
- **Separate blast radius for code-modifying agents.** `[BUILT — by omission]` No code-modifying agent has been built or embedded here. See §11 — still correct as unstarted, separate-track guidance.

---

## 3. Core abstractions — as actually implemented `[BUILT, simplified from the original design]`

| Concept | As originally designed | As actually built |
|---|---|---|
| **Task Definition** | A declarative spec registered by ID, driving trigger/tools/output-schema generically. | **Not built.** One task exists, hardcoded as `classifier.py` + `llm.py` + `pipeline.py` — no registry, no spec object. |
| **Agent** | A stateless LLM executor bound to one Task Definition. | The "agent" is really two separate pieces: a deterministic rules engine (`classify_record()`, no LLM) that makes the actual recommendation, and an optional LLM call (`draft_reason()`) used **only** to write the explanatory sentence — never the decision. This split is a real strength of what shipped: the recommendation can never hallucinate, only the phrasing can, and even that has a template fallback. |
| **Tool** | A typed wrapper around one backend capability, giving an LLM tool-use loop scoped access. | **Not built.** There is no LLM tool-use loop at all — `llm.py` makes one single-turn, no-tools Anthropic API call per non-approve proposal, given a flat text block of already-computed signals. It cannot query anything itself. |
| **Run** | One persisted execution instance. | **Built, as designed.** `agent_runs` row per click of "Run AI Agent": counts, `email_sent`, `email_action_token`, timestamps. |
| **Proposal** | The output of any Task that would change state; requires human accept/edit/reject. | **Built, narrower than designed.** `agent_proposals` row per classified record: `recommended_action` (approve/needs_review/flag), `confidence`, `reasoning`, `reasoning_source` (llm/template), denormalized invoice/trip/amount fields, `status` (pending/accepted/rejected — **no "edited" status was built**, see §8). |
| **Trigger** | Cron schedule, DB-state condition, or manual button — generalized across tasks. | **Built: manual button only.** `POST /api/agents/run`, called from a dashboard toolbar button and a duplicate button inside the Agent Activity tab. No cron/DB-state trigger exists yet — see §13. |

---

## 4. System architecture — as actually implemented `[BUILT]`

```
 Trigger: "🤖 Run AI Agent" button (dashboard toolbar, or the Agent Activity tab's own copy)
                              │
                              ▼
 POST /api/agents/run  (backend/main.py, run_ai_agent())
   1. poll_invoice_folder(db)          — SAME function "Pull Invoices" already calls, no separate intake path
   2. _pending_records_for_agent(db)   — every currently-pending BOLRecord, minus invoice-less sibling stubs
   3. run_agent_pipeline(pending)      — backend/agents/pipeline.py
        for each record:
          classify_record(bol)         — backend/agents/classifier.py (deterministic, no LLM)
          if action != approve:
            draft_reason(bol, cls)     — backend/agents/llm.py (optional Anthropic call, template fallback)
   4. persist AgentRun + AgentProposal rows (or mock dicts under USE_MOCK_DATA)
   5. send_agent_summary_email(...)    — backend/email_service.py, soft-fails/logs with no SMTP configured
                              │
                              ▼
 Agent Activity dashboard tab (frontend/src/components/AgentActivitySection.jsx)
   - GET /api/agents/proposals lists every proposal
   - Accept  → POST /api/agents/proposals/{id}/accept  → _accept_proposal() → approve_bol()/flag_bol()
              (the EXACT same functions a manual dashboard click calls — no parallel mutation path)
   - Reject  → POST /api/agents/proposals/{id}/reject  → zero effect on the underlying BOLRecord
   - Batch accept: POST /api/agents/proposals/accept-batch
   - Guarded one-click email link: GET (preview, no mutation) + POST (the only mutating half)
     /api/agents/email-batch-approve — two-step specifically so an email client's automatic
     link-prefetching can never trigger a real approval by accident
```

No registry, no runner, no tool-use loop — the entire pipeline is one synchronous call chain inside a single FastAPI request. See §6.

---

## 5. Data model — as actually implemented `[BUILT, mock-mode verified only — see §13]`

**`agent_runs`** (`backend/models.py`)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `status` | Enum: `running`\|`succeeded`\|`failed` | **Gap:** every real run is hardcoded to `succeeded` (`main.py`); `failed` is defined but never actually assigned anywhere — a partial failure inside the pipeline or email send is currently invisible. |
| `started_at` / `finished_at` | DateTime | |
| `invoices_found` / `invoices_processed` / `records_classified` / `proposals_created` | Integer | |
| `email_sent` | Boolean | |
| `email_action_token` | String(64) | unguessable token for the one-click email-approve link |
| `error` | Text, nullable | **Unused** — nothing ever writes to this column |

No `task_id`, `trigger_type`, `input_summary`, `tokens_used`, or `cost_usd` columns were built — the original cost/budget-tracking design (§12) was never implemented.

**`agent_proposals`** (`backend/models.py`)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `run_id` | UUID FK → `agent_runs.id` | |
| `bol_record_id` | UUID, **not FK-constrained** | deliberate — mock-mode IDs don't point at a real `bol_records` row |
| `recommended_action` | Enum: `approve`\|`needs_review`\|`flag` | the classifier's output |
| `confidence` | Numeric(4,3) | a fixed constant per rule branch (0.5–0.9) — **not a calibrated probability** |
| `reasoning` | Text | LLM-drafted or template, per `reasoning_source` |
| `reasoning_source` | String(20): `llm`\|`template` | audit field — lets you tell which reasoning strings were ever real LLM output |
| `signal_summary` | Text (JSON-encoded) | the exact numbers behind the classification, for a UI tooltip that was never built (see §8) |
| `invoice_number` / `technique_trip` / `manifest` / `amount` / `cost_pct` | denormalized | so reviewing a proposal never requires joining back to `bol_records` |
| `status` | Enum: `pending`\|`accepted`\|`rejected` | **no `"edited"` status** — the original §8 "Edit" action was never built; today it's Accept-as-is or Reject only |
| `reviewed_by` | String | **hardcoded literal** — always `"coordinator"` or `"katie (via email link)"`, not a real authenticated identity (this app has no auth system at all yet) |
| `reviewed_at` / `reject_reason` / `created_at` | | |

No `proposal_type` or JSONB `payload` column — there is only one proposal shape today (a record review recommendation), so the originally-designed generic polymorphic shape wasn't needed yet. Revisit this the day a second task type is actually added (§7).

---

## 6. Execution model — as actually implemented `[BUILT, diverges from the original design]`

- **Fully synchronous, no `BackgroundTasks`, no queue.** The entire chain (poll → classify → draft reasoning → persist → email) runs inline inside the `POST /api/agents/run` request. This matches the original "Phase 1, current scale" reasoning, but is a real latency risk once this runs against the cloud deployment — see §13's Lambda-timeout gap.
- **No idempotency key.** The original design proposed keying a Run by `(task_id, target_id, date)` to prevent duplicate proposals from a re-trigger. **This was not built** — clicking "Run AI Agent" twice in a row will classify (and propose against) the same still-pending records twice, creating duplicate proposal rows. Not yet a demonstrated problem (a human triggers this manually and would notice), but a real gap against the original design intent.

---

## 7. Extension mechanism — original design vs. actual reality `[PLANNED design not built; direct-edit approach used instead]`

**Originally designed:** a `TASK_REGISTRY` + `@register_task` decorator (`backend/agents/registry.py`) so a new task is one new file under `backend/agents/tasks/` plus a prompt file — never touching a shared engine file.

**What actually exists:** none of `registry.py`, `runner.py`, `tools.py`, or `tasks/*.py` were built. The one task that exists is direct, hardcoded Python across three files (`classifier.py`, `llm.py`, `pipeline.py`) with no registration mechanism, no per-task trigger config, and no generic tool-use loop an LLM could drive itself.

**This is an open decision, not a bug to silently fix.** Before adding a second task type (`draft-flag-reason`, `chase-missing-invoice`, `poll-health-monitor` — see §9 — or anything new), decide explicitly:
1. Retrofit the original registry pattern now, before a second task makes the direct-edit approach painful, or
2. Keep extending `classifier.py`/`pipeline.py` directly for now, and only build the registry once a second task actually exists to justify the abstraction (avoids designing for a hypothetical).

Either way, follow the extensibility pattern in `AI Development and Documentation Standard.md`'s companion workflow guidance: read-only data access, deterministic decision logic with LLM reserved for phrasing only, its own reviewable Proposal shape, and reuse of an existing human-equivalent mutation function for Accept — never a new parallel write path.

---

## 8. Human-in-the-loop UI `[BUILT, close to the original design]`

**Agent Activity** dashboard tab (`frontend/src/components/AgentActivitySection.jsx`), alongside Pending / Approved / Third-Party / Log — matches the originally-designed tab pattern well.

- Lists every proposal: recommendation pill (Approve/Needs Review/Flag, colored), confidence %, reasoning text, status.
- **Accept** and **Reject** buttons per row, plus a batch-accept action. **The originally-designed "Edit" action was never built** — a reviewer can only accept the proposal exactly as drafted or reject it outright; there's no way to correct a flag reason's wording before accepting it, for example.
- **Gap found in the shipped UI, not originally anticipated:** the `signal_summary` field (the exact numbers behind a classification) is persisted and fetched by the frontend but **never displayed** — no tooltip, no expandable detail exists to show it, despite the backend model comment explicitly saying it exists "to ground the UI tooltip." Worth building before treating this as finished.
- **Cosmetic:** the "Run AI Agent" trigger button is duplicated (dashboard toolbar + inside this tab) — harmless, not yet cleaned up.

---

## 9. Initial tasks — what was designed vs. what shipped

| Task | Status | Notes |
|---|---|---|
| **Record-review classification** (approve / needs-review / flag every pending record) | `[BUILT]` | **Not exactly any of the four originally-planned tasks below** — this is a broader, simpler task that emerged instead: classify every currently-pending record's overall disposition, using the same thresholds the dashboard already color-codes. Arguably more immediately useful than the narrower `propose-invoice-match` idea below, but shipped without the original Phase-1 gate (§12/§13) being resolved first — it currently only runs against mock data, which is what keeps it inside that gate today. |
| `propose-invoice-match` (as originally scoped: best-candidate match for a still-unmatched invoice stub) | `[PLANNED, NOT BUILT]` | Largely superseded in practice — the deterministic `_wide_fallback_technique_search()` (unrelated to this agent layer) now auto-resolves most stubs without any LLM involvement. What's left for an LLM agent to add here (a fuzzier guess for stubs that survive that automatic search) is still a real, unbuilt idea. |
| `draft-flag-reason` | `[PLANNED, NOT BUILT]` | Not started. |
| `chase-missing-invoice` | `[PLANNED, NOT BUILT]` | Not started. |
| `poll-health-monitor` | `[PLANNED, NOT BUILT]` | Not started. |

---

## 10. Analytical agents `[PLANNED, NOT BUILT — unchanged from original design]`

Read-only tasks that would never produce a Proposal — display artifacts or emails, lighter review policy:

- **Weekly variance narrative** — aggregate `approval_history` + `BOLRecord` cost-variance data into a short written summary.
- **Carrier trend watch** — flag if a carrier's average `cost_pct` drifts over a rolling window.

Still a reasonable later-phase idea; nothing about the actual Phase 0/1 build changes this design.

---

## 11. Coding / dev-work agents — explicitly out-of-band `[unchanged, non-negotiable]`

**Do not embed a code-modifying agent inside this runtime.** Different trust boundary (source code and git history, not billing data), different reviewer (a developer, not Katie). Reuse the same conceptual model — Task → Trigger → Proposal → human review — but let the "runtime" be Claude Code itself and the "Proposal" be a pull request. Independent of everything else in this document; has no dependency on `backend/agents/`.

---

## 12. Security & cost constraints `[PARTIALLY BUILT — see gaps]`

- Agent reads inherit the app's existing access — no new write path into `AWP-SQL-PROD`/`SQLAPPS3` was introduced. `[BUILT]`
- **Still open, unresolved, and now more urgent than when this was written**: sending billing/shipment data to Anthropic's hosted API needs IT/Legal sign-off on data handling. **Not answered.** The shipped code currently only runs against mock data, which is the only thing keeping this compliant today — this must be resolved before pointing the agent at real (`USE_MOCK_DATA=False`) records. `[OPEN — see §15]`
- LLM API key lives in `.env` (`ANTHROPIC_API_KEY`), matching every other secret's convention. `[BUILT]` **Gap:** unlike `DATABASE_URL`, this key has no path into the AWS Secrets Manager mechanism the deployed Lambda otherwise uses — deploying this for real would need that wired up manually first.
- **Cost guard: designed, not built.** No `tokens_used`/`cost_usd` tracking exists on `agent_runs`, and there is no daily budget ceiling enforced anywhere. `[PLANNED, NOT BUILT]` Low real risk today only because `llm.py` short-circuits to a template with no API key configured — revisit before ever provisioning a real key.

---

## 13. Phased rollout — updated status

| Phase | Scope | Status |
|---|---|---|
| **0 — Infra** | `backend/agents/` package, `agent_runs`/`agent_proposals` tables, Agent Activity tab, manual trigger | `[BUILT]` — shipped, but as a direct pipeline, not the registry-based plumbing originally scoped for this phase |
| **1 — First operational task** | Originally: `propose-invoice-match`, gated on IT/legal sign-off | `[BUILT, narrower scope, gate not yet satisfied]` — the record-review classification task shipped instead of the originally-planned one, and only runs in mock mode; the IT/legal gate was never actually resolved, just deferred by staying mock-only |
| **2** | `draft-flag-reason` + `chase-missing-invoice` | `[PLANNED, NOT BUILT]` |
| **3** | `poll-health-monitor` + analytical tasks (§10) | `[PLANNED, NOT BUILT]` |
| **4 (independent track)** | Coding/dev-work agents via CI (§11) | `[PLANNED, NOT BUILT]` — no dependency on 0–3 |

**Additional concrete blockers found during the 2026-07-23 audit, not part of the original phased plan:**

| Item | Current state | Specific blocker |
|---|---|---|
| Scheduled/unattended trigger | Manual only | Invoice intake (`poll_invoice_folder()`) can't reach its source in the cloud deployment |
| Cloud invoice intake | Local/network path only | No S3-based alternative built |
| Load/latency testing | Never done | Full pull+classify+email chain is synchronous in one request against a deployment with a hard request-timeout ceiling |
| Schema/production verification | Mock-mode only | `agent_runs`/`agent_proposals` never exercised against the real production database |
| Run-history visibility | Not exposed | No route lists past runs; failures are silently recorded as successes (§5) |
| Automated tests | None exist | `classifier.py` must be hand-kept in sync with the dashboard's own thresholds, no shared source of truth |
| Authentication | None in this app | `reviewed_by` is always a hardcoded string |

---

## 14. Actual file layout (supersedes the originally-proposed layout below)

```
backend/agents/
  __init__.py
  classifier.py   — classify_record(bol): deterministic rules, mirrors dashboard thresholds exactly
  llm.py          — draft_reason(bol, classification): optional Anthropic call, template fallback
  pipeline.py     — run_agent_pipeline(pending_records): pure orchestration, no I/O

backend/main.py (not split out; all routes live here, tagged "Agents"):
  POST /api/agents/run
  GET  /api/agents/proposals
  POST /api/agents/proposals/{id}/accept
  POST /api/agents/proposals/{id}/reject
  POST /api/agents/proposals/accept-batch
  GET  /api/agents/email-batch-approve   (preview, no mutation)
  POST /api/agents/email-batch-approve   (the only mutating half)

frontend/src/components/
  AgentActivitySection.jsx   — proposal table, Accept/Reject, matches the originally-designed tab pattern
```

**Originally proposed (not built — preserved here for reference if the registry pattern is retrofitted later, see §7):**

```
backend/agents/
  registry.py          — TASK_REGISTRY, @register_task decorator, TaskDefinition dataclass
  runner.py            — executes a Run: loads task, drives LLM tool-use loop, persists Run + Proposals
  tools.py             — typed wrappers around data_layer.py / models.py / email_service.py exposed to agents
  tasks/
    propose_invoice_match.py
    draft_flag_reason.py
    chase_missing_invoice.py
    poll_health_monitor.py
  prompts/             — one file per task, plain text/markdown, editable without touching Python
```

---

## 15. Open questions before real (non-mock) data flows through this system

| # | Question | Who to ask | Status |
|---|---|---|---|
| 1 | Is sending billing/invoice data to Anthropic's hosted API acceptable under SG360's data handling policy, and does it need a specific vendor agreement? | IT / Legal | **Still open** — confirmed unresolved as of the 2026-07-23 audit |
| 2 | Who reviews Proposals day-to-day — Katie only, or someone else? | Nikhil / Katie | Still open |
| 3 | Budget ownership for LLM API usage | Nikhil | Still open — moot until §12's cost guard is actually built and a real API key is provisioned |
