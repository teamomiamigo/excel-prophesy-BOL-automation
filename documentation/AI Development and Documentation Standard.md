*created 2026-07-23*

## Purpose

This repo now has AI-agent-authored functionality (the classifier/LLM/pipeline layer under `backend/agents/`, first committed 2026-07-22). This standard exists because that first round shipped completely undocumented — zero changelog entries, and two docs (`CLAUDE.md`, `Agentic Automation Architecture.md`) left silently claiming it doesn't exist, weeks after it did. This is the fix, going forward.

## Rule 1 — Every AI-related change gets a changelog entry, same bar as anything else

`documentation/Developmental Documentation.md` already has a working convention: "One entry per closed issue. Newest on top," each with What/Why/Files/Gotcha. AI-authored or AI-agent-touching work follows this identical format — no separate track, no lighter bar.

Concretely: the commit/PR that adds or changes anything under `backend/agents/`, any `/api/agents/*` route, or any agent-related frontend component must include its changelog entry in that same commit, not as a follow-up. If a change adds a genuinely new LLM call site, say so in the entry — the code already tracks LLM-vs-template per proposal (`AgentProposal.reasoning_source`); the changelog doesn't need to re-derive that, just flag when a new integration point is introduced.

## Rule 2 — Every architecture/design doc carries an explicit status tag per section

Root cause of this round's staleness: `Agentic Automation Architecture.md` was written before the code existed, said so accurately at the time, and was never revisited once code shipped against it. Fix: every section of a forward-looking design doc gets one of three tags, updated in the same commit that changes its truth value:

- `[PLANNED]` — designed, not built.
- `[BUILT]` — implemented, exercised at least in mock mode.
- `[DEPLOYED]` — implemented and verified against the live/production environment.

A document's own header claim must reflect the least-mature tag of any section still under it — never leave a document-level claim ("this is unimplemented") that a single shipped section has already falsified.

## Rule 3 — Where AI-related content lives (extends the existing 3-way split, doesn't add a 4th)

This repo already splits documentation three ways; AI-related content slots into the same split rather than inventing a new location:

- **`CLAUDE.md`** — architecture/business rules, kept current. Update the relevant section (e.g. the `backend/agents/` directory description) in the same commit that changes what's true.
- **`documentation/Developmental Documentation.md`** — the changelog. See Rule 1.
- **`documentation/<Subsystem>.md` deep-dives** (e.g. `Agentic Automation Architecture.md`) — the detailed spec for one subsystem. Apply Rule 2's status tags here specifically, since this is where the staleness actually happened.

## Rule 4 — Ownership: the same person/PR that ships the code owns the doc update

No separate "docs" pass, no backlog item to "clean this up later" — the PR that changes behavior updates `CLAUDE.md`/the changelog/the relevant status tags as part of itself. This mirrors how this repo's `/commit` workflow already appends a changelog entry automatically; extend that same discipline to doc-status-tag updates.

## Rule 5 — Pre-merge checklist for any agentic-layer change

Before merging a change touching `backend/agents/`, any `/api/agents/*` route, or agent-facing frontend components:

- [ ] Changelog entry written in `documentation/Developmental Documentation.md`
- [ ] `CLAUDE.md`'s `backend/agents/` description updated if behavior changed
- [ ] Status tags in `documentation/Agentic Automation Architecture.md` updated to match reality
- [ ] If a new LLM call site was added: confirmed it never decides a mutation, only drafts explanatory text, and has a non-LLM fallback
- [ ] If a new mutation path was added: confirmed it reuses an existing, human-click-equivalent function rather than introducing a parallel write path
