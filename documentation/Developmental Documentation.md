*updated 2026-07-01*

Running log of development work on this branch — what changed, why, and anything non-obvious for the next person (human dev or Claude Code) touching this code. Pairs with `CLAUDE.md` (architecture/business rules, kept current) and the GitHub issue backlog (what's queued up next).

## Reference

Stable technical notes that don't belong to one changelog entry — add here when something is worth knowing on its own. Keep this short; if it's about the codebase's architecture rather than something learned while fixing a bug, it probably belongs in `CLAUDE.md` instead.

_(none yet)_

## Changelog

One entry per closed issue. Newest on top.

### 2026-07-01 — #21 access_prog diverges from ALG's invoice amount
**What:** `access_prog` now uses SG360's own pulled pallet data (`get_pallet_data_for_manifests()` for Technique trips, new `get_prophecy_pallet_data()` for Wolf/311 Prophecy loads) instead of ALG's self-reported per-pallet weight, and uses the invoice's own parsed FSC rate (`alg_fsc_pct`/`alg_fsc_cost`) instead of an EIA-diesel-derived guess. Tariff lookup now tries an exact zone match, then this same invoice's own rate for a gap zone, then a nearest-zone guess as a last resort — the latter two cases set `tariff_zone_approximate`; no own pallet data at all sets `weight_source_fallback`. Both surface as a `~EST` badge in the dashboard.
**Why:** `access_prog` was silently replaying ALG's own weight/rate back through our rate card, so it could never catch a real weight discrepancy — defeating its purpose as an independent Cost % variance check. Verified live: with a deliberately mismatched ALG weight, the old approach gave a deceptive ~100% Cost %, the new one correctly flagged 226%.
**Files:** backend/main.py (`_process_invoice_csv`), backend/data_layer.py (`get_tariff_rate`, new `get_prophecy_pallet_data`), backend/models.py, frontend/src/components/BOLRow.jsx
**Gotcha:** `access_prog`/`base_tariff`/`fsc_pct` are now recomputed fresh from our own data on every invoice upload for a trip, not accumulated per-invoice like `amount` is — our own weight doesn't change just because a second Z-invoice arrived. Also resolved two stale CLAUDE.md open questions in the process: VisualMail SELECT permission (was blocking, now confirmed granted) and the destination/ZIP field (`Locations.AccountNumber` confirmed correct). `tariff_rates` still has real coverage gaps (e.g. zones 253/231/235 absent from the source card) — the invoice-rate fallback covers the common case but the rate card itself should still be completed with Marge/Phil.

### 2026-07-01 — #28 Page scrolls to top after approving a record
**What:** Approve/flag/unflag/etc. no longer reset scroll position — the loading skeleton now only appears on the true initial load, not on background refetches triggered by action buttons.
**Why:** `fetchPending()`/`fetchApproved()` set `loading=true` on every call; the table components replaced their entire content with a small placeholder whenever that flag was true, collapsing page height and losing scroll position.
**Files:** frontend/src/App.jsx
**Gotcha:** Fixed at the shared fetch-function level — every current and future button that calls `fetchPending`/`fetchApproved` inherits this fix automatically.

<!-- Template for new entries:
### YYYY-MM-DD — #NN short title
**What:** one or two sentences on the change
**Why:** root cause / reason, one sentence
**Files:** path/to/file.py, path/to/File.jsx
**Gotcha:** anything non-obvious a future dev needs to know (omit if none)
-->
