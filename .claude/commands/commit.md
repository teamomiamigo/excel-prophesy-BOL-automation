Run the full commit-and-push workflow for this project. The user may optionally pass a short description as $ARGUMENTS.

## Step 1 — Read what changed

Run `git status` and `git diff HEAD` (include staged and unstaged changes). Understand:
- Which files changed and in which area (frontend/, backend/, documentation/, CLAUDE.md, config)
- What the changes actually do — read relevant hunks carefully, don't just list filenames

## Step 2 — Determine which docs to update

Apply these rules:

| Situation | Action |
|---|---|
| A GitHub issue from the backlog is being closed out (bug fix or feature done) | Append a changelog entry to `documentation/Developmental Documentation.md`, using the template embedded in that file (`### YYYY-MM-DD — #NN short title` + **What** / **Why** / **Files** / **Gotcha**). Newest entry goes at the top of the Changelog section. Pull the issue number and title from the backlog/conversation context. |
| Something worth knowing surfaces that isn't tied to closing one issue (a constraint, a non-obvious fact, a gotcha) | Add a short line under the Reference section of `documentation/Developmental Documentation.md` instead of a changelog entry |
| Anything in `frontend/` changed but no issue is being closed this commit | Bump `*updated [date]*` at the top of `documentation/Design and Workflow - BOL Reconciliation.md` |
| New API route, new `.env` key, known bug fixed or added | Update `CLAUDE.md` with a minimal inline change only |

Changelog entries are 3–5 lines, not essays — one fact per line, no restating the diff. If no mapping applies clearly, skip doc updates and note that in the proposal.

## Step 3 — Build the proposal

Present a single block to the user before doing anything. Format it exactly like this:

---
**Ready to commit**

**What changed:** [2–4 plain English bullets describing the code changes]

**Docs to update:** [for a changelog entry, show the full entry text as it will be written; for other docs, doc name + why, or "none"]

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
1. Write the docs identified in Step 2 (changelog entry, reference note, and/or date bump)
2. Apply any CLAUDE.md changes
3. Stage all changed files: both code and docs
4. Commit with the agreed message
5. Push to the current branch: `git push`

After pushing, output one line: the commit hash and the branch it was pushed to.
