---
description: Wipe all ALG-invoice-derived data (weights, amounts, rates, cost %, invoice numbers) from the live SG360 BOL app for a clean re-test, without touching Technique/manifest data, BOL numbers, or the static tariff rate tables.
---

# /cleanout — Reset Invoice Data for Re-Testing

## What this skill does

Wraps `POST /api/admin/reset-invoices?confirm=true` against the **live deployed app** (not local dev) so you can repeatedly re-test the invoice upload/matching/cost-calculation pipeline from a clean slate.

**Clears:** every ALG-invoice-derived field — `invoice_number`, `invoice_email_sender`, `invoice_sent_at`, `inv_job_number`, `carrier`, `alg_weight`/`alg_pallets`/`alg_pcs`, `access_prog`, `amount`, `cost_pct`, `base_tariff`, `fsc_pct`, `alg_fsc_pct`, `alg_fsc_cost`, `tariff_zone_approximate`, `weight_source_fallback`, `match_strategy`, `weight_diff`/`pallet_diff`/`pcs_diff`, `notes`, `flag_reason` — and resets `status` to pending, on **every** Technique-matched record. Also deletes every invoice-only stub record entirely (there's nothing to "clear" on those — the whole record only existed because of the invoice).

This is deliberately unconditional on approval status: a record whose invoice/cost data was just wiped can't sensibly stay "approved" with nothing left to show for it. **If there's ever real, permanent financial history in the live app you need to keep, do not run this against it** — this was an explicit choice made when this skill was designed (see `backend/main.py`'s `reset_all_invoices()` docstring), not a safety default.

**Never touches:**
- Technique-side fields (`technique_trip`, `manifest`, `technique_weight`/`pallets`/`pcs`, `bol_number`, `needs_sid_export`) — so you don't need to re-run "Pull Manifests" after cleanout, the Technique data is still there
- `is_third_party` (a manual categorization independent of any invoice)
- `sid_exported_at` (the Prophecy SID/BOL export lifecycle is independent of ALG invoice data)
- The static `tariff_rates`, `fuel_surcharge_rates`, `alg_tariff_rates` rate-card tables — these took real effort to seed and are never touched by this or any other admin-reset endpoint (Step 1b below checks their health; `POST /api/admin/seed-rate-tables` is the separate route that (re-)seeds them, added 2026-07-22)

**Re-testing past or future invoices works cleanly afterward:** invoice matching dedup (both manual upload and `poll-folder`) checks only the *current* `invoice_number` values in the database. Since this skill clears that field everywhere, re-uploading an invoice you tested before — or a brand new one that's never been seen — both process exactly like the first time.

---

## Steps

### Step 1 — Resolve the live API URL and confirm it's healthy

```powershell
cd C:\nikhilm\excel-prophesy-BOL-automation\terraform\main
$apiUrl = (terraform output -raw api_invoke_url).TrimEnd('/')
cd C:\nikhilm\excel-prophesy-BOL-automation

$health = Invoke-WebRequest "$apiUrl/health" -UseBasicParsing | ConvertFrom-Json
```

Confirm `mock_mode: false` and `db_online: true` before proceeding — if either is off, stop and report it rather than running the reset (an unhealthy or mock-mode app means the wipe wouldn't be touching real live data, or would fail outright).

### Step 1b — Check the static rate-table row counts (visibility, not a gate)

```powershell
$rateCounts = Invoke-WebRequest "$apiUrl/api/admin/rate-table-counts" -UseBasicParsing | ConvertFrom-Json
```

Added 2026-07-22 after discovering live Aurora's `tariff_rates`/`fuel_surcharge_rates` tables were **completely empty** (0 rows) and `alg_tariff_rates` only ~58% seeded (305/528) — this had been silently causing every zone lookup that missed to set the `~EST` badge on real invoices, for weeks, with no visibility into it. These tables are never touched by cleanout itself (see "Never touches" above), but reporting their counts alongside the reset makes a future seeding gap like this one obvious immediately instead of surfacing only as a confusing live-vs-local cost-calculation mismatch.

Expected (matching local's seed, `python -m backend.seed_rates`): `tariff_rates: 253`, `fuel_surcharge_rates: 135`, `alg_tariff_rates: >= 527` (this one only grows over time via the self-learning `reconcile_alg_tariff_rates()` mechanism, so higher than 527 is healthy — lower, or either of the other two off their fixed count, is the gap to flag).

### Step 2 — Snapshot the before-state (for the report, not a gate)

```powershell
$beforePending  = Invoke-WebRequest "$apiUrl/api/bols" -UseBasicParsing | ConvertFrom-Json
$beforeApproved = Invoke-WebRequest "$apiUrl/api/bols/approved" -UseBasicParsing | ConvertFrom-Json
$allBefore = @($beforePending) + @($beforeApproved)
$stubsBefore = ($allBefore | Where-Object { $_.match_strategy -eq "invoice_only" }).Count
$withInvoiceBefore = ($allBefore | Where-Object { $_.invoice_number }).Count
```

### Step 3 — Run the reset

```powershell
$result = Invoke-WebRequest "$apiUrl/api/admin/reset-invoices?confirm=true" -Method POST -UseBasicParsing | ConvertFrom-Json
# {"stubs_deleted": N, "records_cleared": M}
```

### Step 4 — Verify and report

```powershell
$afterPending = Invoke-WebRequest "$apiUrl/api/bols" -UseBasicParsing | ConvertFrom-Json
$stillHasInvoiceData = ($afterPending | Where-Object { $_.invoice_number -or $_.cost_pct -or $_.base_tariff }).Count
```

`$stillHasInvoiceData` should be `0` — if it's not, something in the clear list is incomplete; report this rather than declaring success.

Report to the user:
- Stub records deleted (`$result.stubs_deleted`)
- Records with invoice data cleared (`$result.records_cleared`)
- Confirmation that `$stillHasInvoiceData` is 0
- The rate-table counts from Step 1b, with a flag if any look short of the expected values
- A one-line reminder: Technique/manifest data and BOL numbers are untouched — no need to re-pull before testing invoices again

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `mock_mode: true` at Step 1 | Hitting the wrong URL, or the Lambda somehow fell back to mock settings | Re-check `terraform output api_invoke_url`; don't proceed until `mock_mode: false` |
| `db_online: false` at Step 1 | Live app is currently broken (see the `deploy` skill's troubleshooting table) | Fix connectivity first — running the reset won't work against a down DB anyway |
| 400 `"Pass ?confirm=true..."` | The URL is missing the query param | Should never happen if following Step 3 exactly; double-check the URL string |
| `$stillHasInvoiceData` > 0 after running | The field-clear list in `reset_all_invoices()` (`backend/main.py`) is missing a field that got added to the model since this skill was written | Read the current `_INVOICE_FIELDS_TO_NULL`/`_INVOICE_FIELDS_TO_FALSE` lists in `backend/main.py`, compare against all invoice-derived columns in `backend/models.py`, and extend the list |
| `$rateCounts` shows `tariff_rates`/`fuel_surcharge_rates` at 0, or `alg_tariff_rates` well under 527 | The static rate tables were never seeded against this environment's database (confirmed 2026-07-22 — this exact gap existed silently on live Aurora and caused every zone-lookup miss to set the `~EST` badge on real invoices) | Upload the 3 source files to `s3://sg360-bol-invoices/rate-seed/` (`tariff_rates.csv`, `fsc_matrix.xlsx`, `alg_tariff_rates.csv` — see `backend/seed_rates.py`'s `DEFAULT_*` paths for the originals), then `POST /api/admin/seed-rate-tables`; re-check `rate-table-counts` after |
