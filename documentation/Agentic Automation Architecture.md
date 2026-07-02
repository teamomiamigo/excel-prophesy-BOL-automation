*created 2026-07-01*

---

## 1. Purpose

This document designs an agentic AI layer for the SG360 BOL Reconciliation app — a system of LLM-backed agents that can (a) automate operational work Katie currently does by hand, and (b) later produce analytical output (variance narratives, trend reports), without requiring a redesign each time a new automatable task is identified.

It is scoped to **extend the existing repo** (`backend/`, `frontend/`), not replace any part of it. It reuses existing conventions — `USE_MOCK_DATA`, the inline-migration pattern in `main.py`'s lifespan, the `approval_history` audit table, the approve/flag review pattern already in the dashboard — rather than introducing a parallel framework.

First concrete target: **operational automation agents** (invoice-match proposals, flag-reason drafting, missing-invoice chasing, poll-health monitoring). Analytical agents and code-modifying agents are designed for, but deliberately sequenced later (see §10–11).

---

## 2. Design principles

These come directly from constraints already baked into this app — the architecture doesn't fight them, it inherits them.

- **Human-in-the-loop by default.** Katie already reviews and approves/flags every record; agents extend that pattern rather than bypassing it. An agent never mutates a `BOLRecord`, sends an external email, or triggers an export on its own — it produces a **Proposal** that a human accepts, edits, or rejects.
- **Reuse the data layer, don't bypass it.** Agents call the same `data_layer.py` functions and ORM models the rest of the app uses. No agent gets a raw SQL connection or an independent credential.
- **Read-only against production sources, same as today.** `AWP-SQL-PROD`, `SQLAPPS3`, `SG360-TECH-PRD1` stay SELECT-only. Nothing in this design adds a new write path into company systems — the only new writes are to this app's own PostgreSQL database, exactly as `SECURITY.md` already scopes it.
- **Everything auditable.** Every agent run and every human disposition of its output is a row in the database, extending the existing `approval_history` precedent — not a log line that scrolls away.
- **Mock-mode parity.** Agents must run against `mock_data.py` under `USE_MOCK_DATA=True` exactly like every other route, so they're demoable and developable without touching real systems or spending on LLM calls against production data.
- **Additive extension.** Adding a new automated task should mean writing one new file and registering it — never editing an orchestrator/engine file. This is the direct answer to "flexible and scalable as new tasks are integrated later."
- **Separate blast radius for code-modifying agents.** An agent that proposes a flag reason and an agent that edits `main.py` are not the same risk category and must not share a runtime, credentials, or review queue. See §11.

---

## 3. Core abstractions

| Concept | Definition | Closest existing precedent in this repo |
|---|---|---|
| **Task Definition** | A declarative spec: what triggers it, what tools it may call, what shape its output takes, whether its output needs human review. | Similar in spirit to a route in `main.py`, but declared as data, not a decorated function per feature. |
| **Agent** | An LLM-backed executor bound to exactly one Task Definition. Stateless between runs; only sees the tools and context its task grants it. | N/A — new concept. |
| **Tool** | A typed, narrow wrapper around one existing backend capability (`get_tariff_rate`, a read-only ORM query, `send_export_email`). Agents never get broader access than the tool grants. | `data_layer.py` functions themselves — this just adds a schema'd wrapper an LLM tool-call loop can invoke. |
| **Run** | One execution instance of a Task. Persisted: status, inputs, outputs, tokens/cost, timestamps, linked `BOLRecord`(s). | Same shape as a row in `approval_history`, one level up. |
| **Proposal** | The output of any Task that would change state. Requires explicit human accept / edit / reject before anything happens. Read-only analytical output skips this — it's just displayed. | Mirrors the existing Flag workflow: a flag is entered, then it sits until someone resolves it. |
| **Trigger** | What starts a Run — a cron schedule, a DB-state condition ("invoice stub unmatched for 2+ days"), or a manual button in the dashboard. | The existing "Pull Manifests" / "Poll Email" buttons are manual triggers already; this generalizes the concept. |

---

## 4. System architecture

```
 ┌────────────────────────────────────────────────────────────────────┐
 │  Triggers                                                          │
 │  - cron schedule (reuses the 7/8/9am pull cadence already planned) │
 │  - DB-state watcher (e.g. "unmatched invoice stub, age > 2 days")  │
 │  - manual button in dashboard ("Run Agent" per task)                │
 └───────────────────────────┬──────────────────────────────────────┘
                              ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │  Orchestrator / Runner   (backend/agents/runner.py)                 │
 │  - loads a Task Definition from the registry                       │
 │  - opens a Run row, drives the LLM tool-use loop, closes the Run    │
 └───────────────────────────┬──────────────────────────────────────┘
                              ▼
 ┌───────────────────┐   ┌───────────────────────┐
 │  Task Registry     │   │  Tool Registry         │
 │ (backend/agents/    │   │ (backend/agents/tools.py)│
 │  registry.py +      │   │  thin wrappers around:  │
 │  tasks/*.py)         │   │  data_layer.py,         │
 │                      │   │  models.py ORM queries, │
 │                      │   │  email_service.py       │
 └───────────────────┘   └───────────┬───────────┘
                                      ▼
                          (existing backend, unchanged)
                          data_layer.py / models.py / email_service.py
                                      │
                                      ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │  Persistence — new tables: agent_runs, agent_proposals              │
 │  On accept: writes through to BOLRecord + approval_history, same   │
 │  path a human approval takes today.                                 │
 └───────────────────────────┬──────────────────────────────────────┘
                              ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │  Review surface — new "Agent Activity" dashboard tab                │
 │  Accept / Edit / Reject per proposal, same visual language as       │
 │  BOLRow's Approve / Flag buttons.                                    │
 └────────────────────────────────────────────────────────────────────┘
```

Nothing here replaces existing routes or components — it's a parallel slice that calls into the same backend functions and lands in a new tab next to Pending / Approved / Third-Party / Log.

---

## 5. Data model additions

Follows the existing convention: SQLAlchemy models in `models.py`, columns added via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in the `main.py` lifespan (no Alembic, matching how the rest of the schema evolves).

**`agent_runs`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `task_id` | String | matches a Task Definition's registry key |
| `trigger_type` | String | `"cron"` \| `"event"` \| `"manual"` |
| `status` | String | `"running"` \| `"succeeded"` \| `"failed"` |
| `started_at` / `finished_at` | DateTime | |
| `input_summary` | Text | short human-readable description of what was fed in |
| `tokens_used` / `cost_usd` | Numeric | for the budget guard in §12 |
| `error` | Text, nullable | |

**`agent_proposals`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `run_id` | UUID FK → `agent_runs` | |
| `bol_record_id` | UUID FK → `bol_records`, nullable | null for proposals not tied to one record (e.g. a poll-health alert) |
| `proposal_type` | String | e.g. `"flag_reason"`, `"invoice_match"`, `"chase_email"` |
| `payload` | JSONB | the actual proposed content/action, shape defined per `proposal_type` |
| `confidence` | Numeric, nullable | agent's self-reported confidence, shown to the reviewer |
| `status` | String | `"pending"` \| `"accepted"` \| `"edited"` \| `"rejected"` |
| `reviewed_by` / `reviewed_at` | String / DateTime, nullable | |

Accepting a proposal executes the underlying tool call (e.g. actually sets `flag_reason`, actually sends the chase email) through the **same functions the manual dashboard actions already use**, and writes an `approval_history` row — so the audit trail stays unified whether a human or an accepted-agent-proposal caused the change.

---

## 6. Execution model

- **Async via FastAPI `BackgroundTasks`** for the initial phase — matches current scale (one reviewer, small daily volume). No new infrastructure (queue, worker process) needed to ship Phase 1.
- **Upgrade path, not a requirement now:** if run volume or latency ever demands it, swap the runner's dispatch for RQ/Celery/Arq behind the same `runner.py` interface — nothing else in the design changes.
- **Idempotency:** a Run is keyed by `(task_id, target_id, date)` so a re-poll or re-trigger doesn't produce duplicate proposals — the same discipline already used for invoice dedup-by-`invoice_number` in `_process_invoice_csv()`.

---

## 7. Task Registry — the extension mechanism

This is the direct answer to "flexible... as new features, tasks, and parts that need to be automated are integrated later on." Adding a task never touches the runner.

```python
# backend/agents/registry.py
TASK_REGISTRY: dict[str, "TaskDefinition"] = {}

def register_task(task_id: str, **spec):
    def decorator(fn):
        TASK_REGISTRY[task_id] = TaskDefinition(id=task_id, handler=fn, **spec)
        return fn
    return decorator
```

```python
# backend/agents/tasks/propose_invoice_match.py
from backend.agents.registry import register_task

@register_task(
    "propose-invoice-match",
    trigger="event:unmatched_invoice_stub",
    tools=["query_unmatched_stubs", "query_recent_manifests"],
    output_schema=InvoiceMatchProposal,
    review_required=True,
)
def run(context):
    ...
```

A new task = one new file in `backend/agents/tasks/`, a prompt file in `backend/agents/prompts/`, and the decorator — no changes to `runner.py`, no changes to the dashboard shell (the "Agent Activity" tab renders any `proposal_type` generically off its schema). Schedules for cron-triggered tasks are declared the same place as everything else config-driven — `config.py`, via `pydantic-settings`.

---

## 8. Human-in-the-loop UI

New dashboard tab: **Agent Activity**, sitting alongside Pending / Approved / Third-Party / Log — same tab pattern already in `App.jsx`.

- Lists pending Proposals grouped by `proposal_type`.
- Each row: the proposed content, the agent's confidence, and Accept / Edit / Reject buttons — visually consistent with `BOLRow.jsx`'s Approve / Flag buttons.
- **Accept** commits the proposal through the existing mutation path (e.g. the same function `POST /api/bols/{id}/flag` calls internally) and stamps `approval_history`.
- **Edit** lets Katie adjust the drafted text/match before accepting — the edited version is what gets committed and logged.
- **Reject** just closes the proposal with a reason; optionally feeds back into future prompts as a "don't repeat this mistake" example (Phase 3+, not required to ship Phase 1).

---

## 9. Initial tasks (operational automation — the selected first phase)

| Task | Trigger | What it proposes | Why this one |
|---|---|---|---|
| `propose-invoice-match` | Event: invoice-only stub exists with no Technique match | Best-candidate trip/manifest match with a confidence score, using signals beyond the current exact Job-Name match in `_process_invoice_csv()` (date proximity, weight/pallet similarity) | Highest current manual burden — stubs currently sit unmatched until someone manually reassigns via `reassign-invoice` |
| `draft-flag-reason` | Event: record's `cost_pct` is orange/red and still unflagged after the pull that surfaced it | A candidate flag reason, grounded in the variance and similar historical flags | Removes the blank-page problem of writing a flag reason from scratch |
| `chase-missing-invoice` | Cron: nightly, records N business days old with no ALG invoice | A drafted follow-up email to Tanya/Phil — **queued, never sent** without explicit Accept, since this is external-facing | Currently no automated nudge exists; someone has to notice the gap manually |
| `poll-health-monitor` | Cron: after each scheduled email/folder poll | A dashboard banner (not a Proposal — it's informational) if poll results look anomalous (e.g. zero results N days running) | Silent failures in `email_parser.py` / folder polling are currently only visible in logs |

All four reuse existing tools (`data_layer.py`, ORM queries, `email_service.py`) — no new integrations required to start.

---

## 10. Analytical agents (designed for, not first phase)

Read-only Tasks that never produce a Proposal — their output is a display artifact or an email, so they use a lighter review policy (no accept/reject queue, just a "send" confirmation where relevant):

- **Weekly variance narrative** — aggregates `approval_history` + `BOLRecord` cost-variance data into a short written summary for Katie/Mary.
- **Carrier trend watch** — flags if a carrier's average `cost_pct` drifts over a rolling window, independent of any single flagged record.

These slot into the same Task Registry (`review_required=False`, `output_schema` = a narrative/report shape instead of a mutation) — no architectural change needed when this phase starts, just new files under `backend/agents/tasks/`.

---

## 11. Coding / dev-work agents — explicitly out-of-band

**Recommendation: do not embed a code-modifying agent inside this runtime.** It's a different trust boundary (touches source code and git history, not billing data), a different reviewer (a developer, not Katie), and mixing it with agents that read production billing data multiplies the blast radius of both for no benefit.

Instead, reuse the same *conceptual* model — Task → Trigger → Proposal → human review — but let the "runtime" be Claude Code itself (this session, or a GitHub Action using the Claude Code SDK/Action), and let the "Proposal" be a pull request instead of a database row. A cron-triggered task like "flag TODOs older than 90 days" or "run the code-review skill on the latest merge to main" fits this pattern without ever sharing credentials, a database connection, or a review queue with the operational agents in §9. This can be started independently of Phases 0–3 below — it has no dependency on the `backend/agents/` package.

---

## 12. Security & cost constraints (carried over from `SECURITY.md`)

- Agents inherit the existing read-only DB service account — no new write path into `AWP-SQL-PROD` / `SG360-TECH-PRD1` / `SQLAPPS3` is introduced by this design.
- **Open question, must resolve before Phase 1 ships:** sending billing/shipment data to an external LLM API needs IT/legal sign-off on data handling, the same class of question already tracked in `CLAUDE.md`'s Open Questions table. Do not send real customer or invoice data to an LLM provider until this is answered — build and test Phase 1 entirely under `USE_MOCK_DATA=True` in the meantime.
- LLM API key lives in `.env` like every other secret (`ANTHROPIC_API_KEY` or equivalent) — never hardcoded.
- **Cost guard:** each `agent_runs` row logs tokens/cost; the runner enforces a daily budget ceiling (config value in `config.py`) and refuses to start new Runs once it's hit, failing loudly rather than silently degrading.

---

## 13. Phased rollout

| Phase | Scope | Ships when |
|---|---|---|
| **0 — Infra** | `backend/agents/` package, registry, `agent_runs`/`agent_proposals` tables, "Agent Activity" tab, one no-op "echo" task to prove the pipe end-to-end in mock mode | The end-to-end plumbing works with zero real LLM data exposure |
| **1** | `propose-invoice-match` | IT/legal data-handling question in §12 is answered |
| **2** | `draft-flag-reason` + `chase-missing-invoice` | Phase 1 has run for a few days without surprises |
| **3** | `poll-health-monitor` + analytical tasks (§10) | Whenever — these are read-only and lower-risk, can move earlier if useful |
| **4 (independent track)** | Coding/dev-work agents via CI (§11) | Can start any time — no dependency on Phases 0–3 |

---

## 14. Proposed file layout

```
backend/agents/
  __init__.py
  registry.py          — TASK_REGISTRY, @register_task decorator, TaskDefinition dataclass
  runner.py            — executes a Run: loads task, drives LLM tool-use loop, persists Run + Proposals
  tools.py             — typed wrappers around data_layer.py / models.py / email_service.py exposed to agents
  tasks/
    propose_invoice_match.py
    draft_flag_reason.py
    chase_missing_invoice.py
    poll_health_monitor.py
  prompts/             — one file per task, plain text/markdown, editable without touching Python

frontend/src/components/
  AgentActivitySection.jsx   — mirrors ApprovedSection.jsx's list + action-button pattern

New routes (in main.py, or split into backend/agents_routes.py once it grows):
  GET  /api/agents/proposals            — pending proposals for the Agent Activity tab
  POST /api/agents/proposals/{id}/accept
  POST /api/agents/proposals/{id}/edit
  POST /api/agents/proposals/{id}/reject
  POST /api/agents/tasks/{task_id}/run  — manual trigger, mirrors the existing "Pull Manifests" button pattern
```

New tables added via the existing inline `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migration block in `main.py`'s lifespan — no Alembic introduced.

---

## 15. Open questions before Phase 1

| # | Question | Who to ask |
|---|---|---|
| 1 | Is sending billing/invoice data to an external LLM API acceptable under SG360's data handling policy, and does it need a specific vendor agreement? | IT / Legal |
| 2 | Who reviews Proposals day-to-day — Katie only (operational tasks), or also Nikhil for anything dev-adjacent? | Nikhil / Katie |
| 3 | Budget ownership for LLM API usage — same cost center as the rest of this app's infra, or separate? | Nikhil |
