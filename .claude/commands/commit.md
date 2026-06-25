Run the full commit-and-push workflow for this project. The user may optionally pass a short description as $ARGUMENTS.

## Step 1 — Read what changed

Run `git status` and `git diff HEAD` (include staged and unstaged changes). Understand:
- Which files changed and in which area (frontend/, backend/, documentation/, CLAUDE.md, config)
- What the changes actually do — read relevant hunks carefully, don't just list filenames

## Step 2 — Determine which docs to timestamp

Apply these rules to decide which documentation files need an `*updated [date]*` refresh:

| Files changed | Doc to timestamp |
|---|---|
| Anything in `frontend/` | `documentation/SG360 BOL Reconciliation — Design Walkthrough.md` |
| Anything in `backend/` | `documentation/Developmental Documentation.md` |
| A `[ ]` checklist item in Developmental Documentation is now resolved | Check it off `[x]` in that file |
| New API route, new .env key, known bug fixed or added | Update `CLAUDE.md` with a minimal inline change only |

Only update the `*updated [date]*` line at the top of the doc — do not add content, do not add a changelog section. Today's date is available from the system.

If no mapping applies clearly, skip doc updates and note that in the proposal.

## Step 3 — Build the proposal

Present a single block to the user before doing anything. Format it exactly like this:

---
**Ready to commit**

**What changed:** [2–4 plain English bullets describing the code changes]

**Docs to update:** [list each doc and why, or "none"]

**CLAUDE.md update:** [one line describing the change, or "none"]

**Commit message:** `[type]: [short description]`
*(types: feat / fix / chore / refactor / docs)*

**PR description:**
```
## What changed
- [bullet]
- [bullet]

## Why
[one sentence]

## Notes / follow-ups
[omit this section if nothing is open]
```

Say **yes** to proceed, or tell me what to change.

---

If $ARGUMENTS was provided, use it to improve the commit message and PR description.

## Step 4 — Wait for approval

Do not touch any files until the user says yes (or a clear equivalent). If they give corrections, revise the proposal and show it again.

## Step 5 — Execute

Once approved:
1. Update `*updated [date]*` in each doc identified in Step 2
2. Apply any CLAUDE.md changes
3. Stage all changed files: both code and docs
4. Commit with the agreed message
5. Push to the current branch: `git push`

After pushing, output one line: the commit hash and the branch it was pushed to.
