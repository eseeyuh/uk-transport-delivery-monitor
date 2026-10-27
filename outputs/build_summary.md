# Build summary

The recorded build passed **all 52 SQL checks** and produced eight CSV tables for Power BI. The analytical sample contains 29 project observations across the 2024–25 and 2025–26 releases, with 13 confirmed project pairs.

This is an edited summary of the supplied build output, not a new pipeline run. The database is `outputs/monitor.duckdb`, and detailed check results are in `outputs/sql_checks.csv`. Calculation rules and limitations are explained in [methodology.md](../docs/methodology.md).

## Coverage at a glance

An observation is one project in one release. A confirmed pair links the same project across the two releases; eligibility for each comparison is assessed separately.

| Measure | 2024–25 | 2025–26 |
|---|---:|---:|
| Included observations | 16 | 13 |
| Annual financial variance calculated | 16 | 12 |
| Financial variance unavailable | 0 | 1 |
| Observations without a confirmed counterpart | 3 | 0 |

| Between-release comparison | Eligible pairs | Excluded pairs | Confirmed pairs |
|---|---:|---:|---:|
| Published end dates | 5 | 8 | 13 |
| Delivery confidence (strict rules) | 1 | 12 | 13 |
| Monetary values | 0 | 13 | 13 |

One pair qualifies for both the schedule and DCA comparisons. Missing or excluded comparisons are not counted as unchanged.

## Published end dates

Of the five comparable schedules, one published end date moved later and four were unchanged. None moved earlier.

| Project | End date reported in 2024–25 | End date reported in 2025–26 | Movement (days) |
|---|---|---|---:|
| East Coast Main Line Programme | 2 June 2026 | 3 May 2027 | +335 |
| Transpennine Route Upgrade | 17 October 2041 | 17 October 2041 | 0 |
| East Coast Digital Programme | 14 October 2030 | 14 October 2030 | 0 |
| Midlands Rail Hub | 31 December 2040 | 31 December 2040 | 0 |
| A417 Air Balloon | 31 August 2027 | 31 August 2027 | 0 |

Movement is the later release's end date minus the earlier release's end date. It describes a change in published information, not proof of a realised delay or its cause.

Eight pairs were excluded: five lacked an end date in one release, and three had scope or re-sequencing issues.

## Delivery confidence

East Coast Digital Programme is the only pair that meets the strict DCA comparison rules. Its external rating changed from **AMBER to RED**, labelled **Deteriorated**. The earlier commentary credits the IPA and the later commentary credits NISTA.

Eligibility requires comparable scope, ratings from the same field and verified assessment origin of the same class in both releases. The origin review is AI-assisted and is not independent human sign-off. This single case does not establish a sector-wide trend.

| Reason a DCA comparison was excluded | Pairs |
|---|---:|
| Assessment origin unverified | 6 |
| Rating field changed | 2 |
| Rating unavailable in a release | 2 |
| Scope changed | 2 |
| **Total excluded** | **12** |

Two of the 13 pairs changed rating field between releases; the other 11 did not. No eligible pair was labelled Improved or Unchanged.

## Annual financial variance

Annual forecast variance was calculated for **28 of the 29 observations**. East Coast Main Line in 2025–26 has a blank baseline, so its variance is unavailable. That blank is not treated as zero.

```text
Variance (£m) = Annual forecast − Annual baseline
Variance (%)  = 100 × (Annual forecast − Annual baseline) / Annual baseline
```

The calculation is signed: positive means forecast above baseline, and negative means forecast below baseline. Figures are compared only within the same release. No cross-release monetary comparison or combined monetary total is produced.

### Reconciliation with published percentages

All 28 eligible sample observations agree with the published variance within **0.1 percentage point**:

| Release | Within tolerance | Outside tolerance | Not compared |
|---|---:|---:|---:|
| 2024–25 | 16 | 0 | 0 |
| 2025–26 | 12 | 0 | 1 |

The published percentage behaves as an unsigned magnitude in these files. Reconciliation therefore compares it with the absolute value of the calculated percentage, while exports retain the signed calculation. This is an observed source pattern, not a confirmed official definition.

### Wider source-file diagnostic

A separate diagnostic checked 321 eligible records across the full government portfolio: 320 were within tolerance and one was outside. These counts include the analytical sample; they are not an additional sample or the basis for the transport findings.

| Release | Within tolerance | Outside tolerance |
|---|---:|---:|
| 2024–25 | 177 | 0 |
| 2025–26 | 143 | 1 |

The exception is the out-of-scope DCMS EURO 2028 record (`nista_2025_26:0012`): calculated +1.9027%, published 2.05%, a difference of approximately −0.1473 percentage point. Rounded source amounts are a possible explanation, but the cause is unconfirmed. Source values were not changed, and the record is absent from the analytical exports.

## Projects without a counterpart

Three observations from 2024–25 have no confirmed counterpart in the later release under the matching rules:

| Project | Source observation |
|---|---|
| Midland Main Line Electrification (MMLE) Programme | `nista_2024_25:0093` |
| Rapid Charging Fund | `nista_2024_25:0094` |
| A12 Chelmsford to A120 Widening | `nista_2024_25:0095` |

Their report label is **Not observed in the other release**. This does not establish completion or cancellation. There are no unmatched included observations in 2025–26.

## Power BI exports

All files below are in `data/processed/`. Row counts describe the recorded build and should be updated if its inputs change.

| File | Rows |
|---|---:|
| `pbi_observations.csv` | 29 |
| `pbi_annual_variance.csv` | 29 |
| `pbi_variance_reconciliation.csv` | 29 |
| `pbi_schedule_pairs.csv` | 13 |
| `pbi_dca_pairs.csv` | 13 |
| `pbi_unmatched.csv` | 3 |
| `pbi_field_availability.csv` | 78 |
| `pbi_coverage.csv` | 30 |

Passing the SQL checks confirms the recorded build's processing rules. It does not independently validate every review judgement or Power BI visual interaction.
