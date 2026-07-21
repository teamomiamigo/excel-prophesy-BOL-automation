# ALG minimum-charge (`alg_tariff_rates.mc1`) audit log

Living document. Started 2026-07-21 while investigating why Calculated Cost (`access_prog`)
was running significantly below ALG's actual invoiced `amount` on several real records. See
`CLAUDE.md`'s "access_prog calculation" section and `_apply_access_prog_calc()` in
`backend/main.py` for the code this checks against.

## How to read this

Each row was verified by hand against a real invoice CSV (`test_invoices_0622/`), cross-checked
against two local reference files: `VM_Locations.xlsx` (VisualMail's full destination table —
`AccountNumber`/`ZipCode`/`DropSiteKey`) and `ALG5_2026_tariff_rates.csv` (the source of the
`alg_tariff_rates` table, format `tariff_id,dest_id,rate1,mc1`, no header). "Stored mc1" is what
`alg_tariff_rates` had at the time it was checked; "ALG actually billed" is the real $ figure
from the invoice's own `Rate`/`GrossWt`/`Billed$` columns where a minimum-charge floor visibly
fired (`Billed$` != `round(Rate * GrossWt / 100, 2)`).

As of 2026-07-21, `data_layer.reconcile_alg_tariff_rates()` (called from
`_apply_access_prog_calc()`) automatically writes ALG's own directly-billed rate/minimum back
into `alg_tariff_rates` on every invoice upload/recompute — new entries below this point should
mostly reflect the table healing itself rather than manual spot-checks. Append new findings
(manual or from `GET /api/bols/{id}/cost-breakdown`) as more invoices get checked.

## Verified findings (2026-07-21 initial pass)

| Destination | dest_id | Stored mc1 | ALG actually billed | Status |
|---|---|---|---|---|
| Traverse City, MI | SCF496 | $70 | $70 | correct |
| Abilene, TX | SCF795 | $70 | $70 | correct |
| Amarillo, TX | SCF790 | $70 | $70 | correct |
| Corpus Christi, TX | SCF783 | $70 | $70 | correct |
| El Paso, TX | SCF798 | $70 | $70 | correct |
| Bismarck, ND | SCF585 | $70 | $70 | correct |
| Grand Forks, ND | SCF582 | $70 | $70 | correct |
| Essex Junction, VT | SCF054 | $70 | $70 | correct |
| Easton, MD | SCF216 | $70 | $70 | correct |
| Charleston, WV | SCF250 | $70 | $70 | correct |
| Tallahassee, FL | SCF323 | $70 | $70 | correct |
| Fayetteville, AR | SCF727 | $70 | $70 | correct |
| Yakima, WA | SCF989 | $70 | $70 | correct |
| Grand Junction, CO | SCF814 | $70 | $70 | correct |
| Kingsford, MI | SCF498 | $70 | $70 | correct |
| Lubbock, TX | SCF793 | $70 | $70 | correct |
| McAllen, TX | SCF785 | $70 | $70 | correct |
| Missoula, MT | SCF598 | $70 | $70 | correct |
| North Platte, NE | SCF691 | $70 | $70 | correct |
| Wenatchee, WA | SCF988 | $70 | $70 | correct |
| Midland, TX | SCF797 | $70 | $70 | correct |
| Casper, WY | SCF826 | $70 | $70 | correct |
| Cheyenne, WY | SCF820 | $70 | $70 | correct |
| Huron, SD | SCF572 | $70 | $70 | correct |
| Springfield, OR | SCF974 | $70 | $70 | correct |
| Medford, OR | SCF975 | $70 | $70 | correct |
| ~25 standard destinations (Albany SCF120, Brockton SCF023, Denver NDC800, Cedar Rapids SCF522, Champaign SCF618, Buffalo ASF140, Providence SCF028, Pittsburgh SCF150, Philadelphia SCF190, Baltimore SCF212, Orlando SCF328, and more — see git history for the full per-invoice trace) | various | $10 | $10 | correct |
| **Waite Park, MN** | **SCF563** | **$10** | **$70** | **wrong — one-row data error, not a coverage gap** |
| Nashville, TN (one line, BOL 146477 / Z557838) | SCF370 | $70 | $0 | looks like a credit/adjustment line, not a real floor case — worth one more look, not urgent |

**Bottom line as of this pass:** `alg_tariff_rates`'s 527 rows exactly match VisualMail's own
527-row `Locations` table (zero missing either direction) — this is not a coverage gap. Only
one genuine data error was found (Waite Park). Yet several real records — including ones
created the same day as this audit — still showed Calculated Cost running well below ALG's
actual bill, with `tariff_zone_approximate=false` (the rate matched fine) and no other flag
raised. That's a runtime/execution disconnect between correct stored data and the final
result, not a data problem — see `GET /api/bols/{id}/cost-breakdown`'s docstring in
`backend/main.py` for how to keep investigating it, and the `min_charge_uncertain` flag added
this same session for what visibility exists so far.

## Open items

- Confirm what the Waite Park (SCF563) fix actually was — the self-updating reconciliation
  pass should auto-correct this the next time that invoice (or manifest going to that
  destination) is processed; verify it did.
- Confirm/dismiss the Nashville $0 line (BOL 146477 / Z557838) — likely a credit or partial
  reversal, not evidence `alg_tariff_rates` is wrong for SCF370.
- Root-cause the runtime disconnect described above (see `GET /api/bols/{id}/cost-breakdown`).
