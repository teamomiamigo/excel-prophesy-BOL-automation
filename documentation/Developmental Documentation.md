*updated 2026-07-02*

Running log of development work on this branch — what changed, why, and anything non-obvious for the next person (human dev or Claude Code) touching this code. Pairs with `CLAUDE.md` (architecture/business rules, kept current) and the GitHub issue backlog (what's queued up next).

## Reference

Stable technical notes that don't belong to one changelog entry — add here when something is worth knowing on its own. Keep this short; if it's about the codebase's architecture rather than something learned while fixing a bug, it probably belongs in `CLAUDE.md` instead.

- **Known data-integrity bug (found 2026-07-01, not yet fixed):** 10 pairs of duplicate `bol_records` rows exist in production for the same `technique_trip` (e.g. two rows for `TEC_T_0110814` with the identical `created_at` timestamp down to the microsecond). Root cause suspected in `pull_technique_data()`'s upsert/matching logic — a real trip is getting inserted twice in one pull instead of matched to its existing row. Not caused by anything in this changelog; discovered incidentally while verifying the table-merge below. Tracked as a follow-up, not yet fixed.

## Changelog

One entry per closed issue. Newest on top.

### 2026-07-02 — #34 / #35 Per-record Prophecy export, per-record BOL check, top-level Refresh (docs fix for #22)
**What:** Added two per-record actions on pending Type A rows (Actions column, `BOLRow.jsx`): "SID" exports that one record's Prophecy SID CSV without waiting for a batch approval (`POST /api/bols/{id}/export-prophecy-sid`, reusing the exact same `get_pallet_data_for_manifests()`/`generate_sid_csv()` logic as the bulk export — verified byte-identical output for the same manifest), and "↻ BOL" checks Prophecy for a BOL number on just that manifest (`POST /api/bols/{id}/refresh-bol`). Added a top-level "⟳ Refresh" button that re-fetches pending/approved records from our own DB only (`fetchPending()` + `fetchApproved()`) — no live Technique/AWP-SQL-PROD hit, unlike "Pull Manifests". Also implemented `sid_exported_at`, which existed as a column since early on but was never actually written anywhere — now stamped by both the bulk and per-record export routes.
**Why:** Katie needed to push one urgent record to Prophecy without batching, and check whether a BOL came back without re-running the full morning pull. Investigating this surfaced that CLAUDE.md's Open Question #8 ("how do we get BOL numbers back from Prophecy?") was stale — the mechanism (`get_technique_data()`'s ShipperPlus join) already existed via `pull_technique_data()`, just undocumented and not available per-record.
**Files:** backend/main.py (`_apply_bol_status()` extracted as a shared helper, two new routes, `sid_exported_at` write added to the bulk route), frontend/src/components/BOLRow.jsx, frontend/src/App.jsx, CLAUDE.md (resolved Q8)
**Gotcha:** `refresh-bol`'s live-query path (record has no BOL yet) takes ~10-11s in practice — it reuses `get_technique_data(days_back=21)` unchanged rather than a new single-manifest-scoped SQL query, trading speed for zero risk of a subtly wrong new query. Short-circuits near-instantly when the record already has a BOL. Issue #22 ("verify end-to-end") is NOT closed by this entry — the actual live round-trip (real SID export → user imports into Prophecy → "↻ BOL" confirms it) is being verified by the user directly, not by an automated test.

### 2026-07-01 — #31 / #36 Remove incorrect Prophecy badges + merge Invoice Only into main table
**What:** Reordered invoice-matching priority in `_process_invoice_csv()` so exact Technique-trip matches (Z-number, then Job-Name-as-trip-suffix) are always tried before treating Job Name as a Prophecy BOL number — a real trip whose numeric suffix happens to start with "14" (e.g. `140237`) was being misclassified as a Wolf/311 Prophecy load, incorrectly showing the indigo "P" marker. Also removed the separate "Invoice Only" and "Comingle" collapsible sections in `BOLTable.jsx` — all pending records (manifest-only, invoice-only, comingle) now render in one flat table, sorted by effective date (`invoice_sent_at` if known, else `created_at`).
**Why:** Katie wants one unified review queue, not split by category; the Prophecy-BOL heuristic had no way to distinguish "looks like a BOL number" from "is actually one" without first ruling out a real trip match.
**Files:** backend/main.py (`_process_invoice_csv` matching order), frontend/src/components/BOLTable.jsx
**Gotcha:** The date-based sort is a placeholder, not the real sortable-columns feature (issue #33) — many records have no `invoice_sent_at` yet and fall back to `created_at`, so ordering isn't very meaningful until #33 lands. Verified the reorder fix with a throwaway record (`TEC_T_0140237`) proving the collision case now matches correctly via `job_name` instead of `prophecy_bol`.

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
