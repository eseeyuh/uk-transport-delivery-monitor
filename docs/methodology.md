# Methodology

## About this analysis

The UK Transport Delivery Monitor uses two annual government publications to examine transport infrastructure reporting. It covers annual financial forecasts, changes in published project end dates, delivery confidence and the availability of supporting information.

The Power BI report has three pages: **Financials**, **Changes** and **Coverage**. This document explains how their measures are calculated and where comparison is limited. It describes an independent portfolio project, not an assessment commissioned by NISTA, the Department for Transport (DfT) or VVB Engineering.

## 1. Sources and scope

| Release | Reporting snapshot | Source |
|---|---|---|
| 2024–25 | 31 March 2025 | [NISTA Annual Report 2024–25](https://www.gov.uk/government/publications/nista-annual-report-2024-2025) |
| 2025–26 | 31 March 2026 | [NISTA Major Projects Annual Report 2025–26](https://www.gov.uk/government/publications/nista-major-projects-annual-report-2025-26) |

The analysis uses the accompanying project-level CSV files, stored locally as `nista_2024_25.csv` and `nista_2025_26.csv`. Together they contain 402 records across the full government portfolio: 213 in the earlier file and 189 in the later one, with 21 columns in each.

The report includes DfT projects classified as **Infrastructure and Construction**, subject to the recorded scope review. Other DfT programmes remain outside the headline sample. The resulting sample contains 29 project observations: 16 in 2024–25 and 13 in 2025–26.

An **observation** is one project in one release. These 29 observations are not 29 distinct projects: the model contains 16 project keys, including 13 projects matched across both releases.

The original download dates are unknown. They have not been inferred from file timestamps or later checks. The source record states that, on 22 October 2026, files served by the official download links had SHA256 hashes identical to the stored raw files. This verifies file identity on that date, not the original retrieval date.

The snapshot date is also distinct from publication dates and dates mentioned in commentary. Some departmental narratives describe developments after the reporting snapshot.

## 2. Data preparation and reporting model

The workflow keeps source records separate from prepared data and calculations:

1. **Inventory:** record source columns, raw values, file hashes and metadata.
2. **Preparation:** map fields, parse values and retain disclosure statuses without changing the raw files.
3. **Scope and matching:** apply recorded inclusion decisions and the reviewed project crosswalk.
4. **Analysis:** use Python and DuckDB SQL to build reporting tables and run validation checks.
5. **Presentation:** load eight CSV exports through Power Query into Power BI and calculate report measures in DAX.

The Power BI model separates project and release dimensions (`DimProject` and `DimRelease`) from reporting tables. Those tables cover observations, annual variance, reconciliation, schedule pairs, DCA pairs, unmatched observations, field availability and coverage. Pair tables describe comparisons between releases; their cohort is not redefined by selecting a single reporting year.

Exports retain identifiers, source file and row references, raw values, calculation statuses, eligibility flags and reasons for exclusion. These make it possible to trace a displayed result back to its source.

## 3. Annual financial variance

### What the measure means

Financial figures are expressed in **£ million**. Annual forecast variance compares the forecast with the annual baseline in the same project observation:

```text
Variance (£m) = Annual forecast − Annual baseline
Variance (%)  = 100 × (Annual forecast − Annual baseline) / Annual baseline
```

A positive result means the forecast is above the baseline; a negative result means it is below. These are forecasts, not actual expenditure. A negative figure does not by itself demonstrate savings, and a positive figure does not establish a final project cost overrun.

Amounts are calculated only when baseline and forecast are both usable single numbers with matching price-basis labels in the same release. Percentages additionally require a baseline above zero. A blank baseline is not treated as zero. A zero baseline retains its amount but has no percentage result. Ranges are not replaced with midpoints, and missing or withheld amounts are not imputed. Unusual or negative source amounts require review rather than silent deletion.

For 2024–25, the source labels do not establish a price basis. Calculations within that release are retained with this limitation; matching unspecified labels do not independently prove a common economic basis.

### Aggregate variance

The headline percentage uses the same eligible observations in both sums:

```text
Aggregate variance (%) =
    100 × (SUM(eligible forecasts) − SUM(eligible baselines))
        / SUM(eligible baselines)
```

The denominator must be positive. This is not the average of individual project percentages. Financial measures are shown for one release at a time, alongside coverage.

| Release | Included observations | Eligible for annual variance |
|---|---:|---:|
| 2024–25 | 16 | 16 |
| 2025–26 | 13 | 12 |

East Coast Main Line in 2025–26 has no published baseline, so its variance is unavailable and it is excluded from both aggregate sums.

### Why monetary values are not compared across releases

Section 5 of the 2025–26 report introduces real-terms reporting with a 2024/25 base year and warns that previous years' monetary values are not directly comparable. A universal price basis for the earlier project-level figures was not established from the 2024–25 report.

Cross-release monetary comparisons are therefore disabled, including growth in whole-life cost, benefits, baseline and forecast. Annual figures also refer to different financial years. A change between portfolio totals must not be described as a saving or cost increase.

No inflation conversion is applied. Any future comparison would need evidence about project-specific prices, scope and cost coverage; applying a generic inflation factor would not resolve these issues.

### Checking against the published percentage

The calculated variance retains its sign. In the supplied files, the published `Financial Year Variance (%)` behaves as a magnitude: none of its parsed values is negative, including records whose forecast is below baseline. This is an observed pattern in these files, not a confirmed official definition.

Reconciliation therefore compares:

```text
Difference (percentage points) = ABS(calculated variance %) − published variance %
```

A result is within tolerance when the absolute difference is no greater than **0.1 percentage point**, plus `1e-9` for floating-point precision. The tolerance is a project rule, not NISTA guidance. Published and calculated values remain separate.

Across the full source portfolio, 321 rows have a positive baseline and all values needed for this comparison. Of those, 320 are within tolerance, including all 217 negative calculated variances. Within the analytical sample, all 28 eligible observations are within tolerance.

One out-of-scope record, DCMS EURO 2028 (`nista_2025_26:0012`), differs by approximately −0.147 percentage point: the displayed £4.73m baseline and £4.82m forecast imply +1.9027%, compared with a published 2.05%. Rounded source amounts are a possible explanation, but the cause is unconfirmed. The record remains flagged and neither value is changed.

There is also one zero baseline outside the sample (`nista_2024_25:0002`, 0 → 0). It has no calculated percentage. No negative baselines were found.

Check R01 stops the build if a negative published percentage appears. This is a prompt to review the observed sign convention, not a declaration that negative source values are invalid.

## 4. Matching projects between releases

Identifiers and names are used to find candidate matches. A similar name alone is insufficient: descriptions and scope must also support a link. The crosswalk records the observation IDs, matching decision, rationale and reviewer attribution.

There are 13 confirmed one-to-one pairs. Four candidate crosswalk rows were rejected. Unresolved splits, mergers or scope changes are not forced into simple like-for-like comparisons. A confirmed identity match does not automatically make its schedule or DCA comparable; each has a separate eligibility decision.

Three included observations in 2024–25 have no counterpart in the later sample:

- Midland Main Line Electrification Programme;
- Rapid Charging Fund;
- A12 Chelmsford to A120 Widening.

They are labelled **Not observed in the other release**. Absence does not establish completion or cancellation. Differences between release counts do not show how many projects were completed.

## 5. Published end-date movements

A schedule comparison requires a confirmed project pair, two valid end dates, comparable scope and the same date concept. Partial or ambiguous dates are not converted into invented exact dates.

```text
Movement (days) = End date in later release − End date in earlier release
```

Positive means a later published end date; negative means an earlier one; zero means no change in the published date.

Five of the 13 confirmed pairs qualify. East Coast Main Line moves from 2 June 2026 to 3 May 2027, **335 days later**. Transpennine Route Upgrade, East Coast Digital Programme, Midlands Rail Hub and A417 Air Balloon have unchanged published end dates.

The other eight pairs are excluded: five lack a usable end date and three have scope or re-sequencing issues. Excluded pairs are not counted as unchanged. Any share of later end dates uses the five eligible pairs as its denominator.

A published date movement does not establish a realised delay, a missed contractual milestone, its cause or responsibility. Commentary can provide context, but later updates are not assumed to have been known at the snapshot.

## 6. Delivery Confidence Assessment (DCA)

DCA describes delivery confidence; it is not a probability of success or a percentage complete. For eligible comparisons, GREEN, AMBER and RED are ordered from lower to higher concern. Transitions are labelled Improved, Unchanged or Deteriorated. Missing, withheld, Reset and historical mixed ratings such as AMBER/RED are not assigned positions on that scale. No mean DCA score is calculated.

### Assessment origin and eligibility

The files contain external and SRO rating fields, prepared as `external_dca` and `sro_dca`. No automatic fallback or precedence rule combines them into `published_dca`. No included observation has a rating in both fields.

The external source heading still refers to the IPA in 2025–26, although commentary can credit NISTA. This project therefore uses a conservative rule: the column name alone does not verify who supplied a rating.

For the 25 included observations with a parsed rating, `data/manual/dca_origin_review.csv` records the commentary attribution and an evidence quote. Origin is verified only when the commentary explicitly credits the IPA or NISTA for an external-field rating, or the SRO for an SRO-field rating. Joint NISTA/SRO attribution, no named assessor or a different assessor is treated as unverified.

A pair qualifies only when both ratings:

- are GREEN, AMBER or RED in the same rating field;
- have verified origins of the same class (external-to-external or SRO-to-SRO);
- relate to comparable project scope.

IPA and NISTA are treated as the external assessment class, while their different names are retained. A change in assessor means a transition cannot be attributed solely to a change in underlying delivery.

### Coverage and interpretation

Only **one of 13 pairs** qualifies: East Coast Digital Programme moves from AMBER, credited to the IPA, to RED, credited to NISTA. It is labelled Deteriorated and presented as an individual case, not a sector trend. No headline rate or average is derived from it.

| Reason for exclusion | Pairs |
|---|---:|
| Rating unavailable in a release | 2 |
| Rating field changed | 2 |
| Assessment origin unverified | 6 |
| Scope changed despite verified origin | 2 |
| **Total excluded** | **12** |

Accepting column labels alone could allow up to seven comparable pairs. That wider interpretation is not adopted. Ratings remain available as within-release context, with origin status and evidence; excluded comparisons are never labelled unchanged.

The attribution and crosswalk reviews were AI-assisted. They have not been independently confirmed by a human reviewer. Matching an evidence quote to raw commentary verifies that the quote exists, not that its interpretation is correct.

## 7. Information availability

The Coverage page counts project observations by reported field and availability class. It distinguishes **Available**, **Blank in source**, **Not yet available** and **Withheld**, using the classifications in the prepared exports.

Counts refer to the selected release: for each field, the displayed status counts should sum to 16 in 2024–25 or 13 in 2025–26. Totals across different fields are not meaningful and are not used as a project count.

Raw tokens and detailed processing statuses are retained separately. Disclosure restrictions, missing source values, parsing problems and unresolved comparability are different issues. Withheld information is not zero, and an unavailable comparison is not an unchanged result.

This page measures **public information availability**, not the quality of a project's internal controls. No composite project data-quality score is assigned.

## 8. Validation and limitations

The recorded pipeline run passed 52 SQL checks and 124 automated tests. A separate standard-library script, sharing no calculation code with the build SQL, matched 27 checks across five source cases. A second full run produced byte-identical exports and unchanged raw-file hashes.

Checks cover unique keys, one-to-one matching, join row counts, scope exclusions, eligibility rules, missing values, finite numerical results and preservation of source values. The independent cases include the East Coast Main Line date movement and missing baseline, Midlands Rail Hub's unchanged date, an East West Rail financial calculation and the East Coast Digital DCA transition.

These results validate the recorded pipeline run. They do not constitute an external audit, human sign-off or exhaustive verification of Power BI filter behaviour. The Power BI report has since been built; the earlier statement that no dashboard existed is no longer current.

The main limits are:

- **Small, selected sample:** findings describe the included DfT observations, not all UK infrastructure or contractor performance.
- **Restricted comparisons:** five schedule pairs and one strict DCA pair qualify; monetary comparisons across releases are disabled.
- **Judgement in reviews:** scope, matching and DCA origin depend on recorded interpretations, including AI-assisted review.
- **Unknown retrieval dates:** later hash verification does not establish when the raw files were downloaded.
- **Unresolved source discrepancy:** the out-of-scope DCMS percentage difference remains unexplained.
- **Snapshot reporting:** published dates, forecasts and assessments are not a continuous record of actual delivery.

## 9. Supporting records and attribution

The scope review, column mapping, project crosswalk, source metadata and DCA evidence are retained in `data/manual/`. Verified source links are recorded in `data/reference/source_links.csv`. SQL check results are in `outputs/sql_checks.csv`; independent check results are in `docs/manual_checks.csv`.

Contains public sector information licensed under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/), except where otherwise stated in the source publications. Source-data licensing is separate from the licence for this project's code.
