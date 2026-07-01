*updated 2026-07-01*

Running log of development work on this branch — what changed, why, and anything non-obvious for the next person (human dev or Claude Code) touching this code. Pairs with `CLAUDE.md` (architecture/business rules, kept current) and the GitHub issue backlog (what's queued up next).

## Reference

Stable technical notes that don't belong to one changelog entry — add here when something is worth knowing on its own. Keep this short; if it's about the codebase's architecture rather than something learned while fixing a bug, it probably belongs in `CLAUDE.md` instead.

_(none yet)_

## Changelog

One entry per closed issue. Newest on top.

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
