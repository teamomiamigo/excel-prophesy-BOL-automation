Requirements & SQL Field Mapping 

ALG Freight Billing Reconciliation | SG360 | Phase 1 | June 2026 

Status key: Confirmed = done | Open = needs confirmation | Blocked = access issue | Manual = user enters 

|                                     |                                         |                                                                     |           |                                                                   |
| ----------------------------------- | --------------------------------------- | ------------------------------------------------------------------- | --------- | ----------------------------------------------------------------- |
| Field                               | Source                                  | DB Field                                                            | Status    | Notes                                                             |
| Technique Trip#                     | AWP-SQL-PROD (VisualMail)               | Technique_trip                                                      | Confirmed |                                                                   |
| Manifest #                          | AWP-SQL-PROD (VisualMail)               | Manifest                                                            | Confirmed |                                                                   |
| BOL  / Load #                       | AWP-SQL-PROD (VisualMail)               | Pooled_in_laod                                                      | Confirmed |                                                                   |
| Z-Number                            | Invoice CSV                             | Invoice number                                                      | Confirmed |                                                                   |
| Job Number                          | Invoice CSV                             | Inv_Job_number                                                      | Confirmed |                                                                   |
| Carrier                             | Invoice CSV                             | Invoice                                                             | Confirmed |                                                                   |
| Weight, Pallets, Pieces by Manifest | AWP-SQL-PROD (VisualMail)               | Technique_weights, <br><br>Technique_pallets, <br><br>Technique_pcs | Confirmed |                                                                   |
| Weights, Pallets, Pieces by Invoice | Invoice CSV                             |                                                                     | Confirmed | These are parsed but not fully stored and displayed in the UI yet |
| Access Program Price                | Tariff_rates * FSC matrix + EIA API key | Access_prog                                                         | Confirmed | Data has been collected, EIA key has not been fully integrated    |
| Invoice Amount                      | Invoice CSV                             | Amount                                                              | Confirmed |                                                                   |
| Cost Difference                     |                                         |                                                                     |           |                                                                   |
| Weights, Pallets, Pieces Difference |                                         |                                                                     |           |                                                                   |