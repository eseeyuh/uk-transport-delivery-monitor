# Power BI report: setup and model notes

The UK Transport Delivery Monitor is a three-page Power BI Desktop report built from the project's prepared CSV files. This guide covers opening the report, refreshing it on another computer and understanding its model. The analytical definitions and limitations are in [methodology.md](methodology.md).

## Open the report

Open `UK_Transport_Delivery_Monitor.pbix` in Power BI Desktop. The saved report includes imported data, so its existing pages can be viewed without rebuilding the Python pipeline or connecting to DuckDB. A PDF is available for viewing without Power BI.

The pages are:

| Page | Contents |
|---|---|
| Financials | Annual baseline, forecast and signed variance for one reporting release |
| Changes | Comparable published end dates and the single eligible DCA transition |
| Coverage | Field availability, unmatched projects and comparison limitations |

The reporting-year selector applies to Financials and Coverage. Changes compares both releases and does not use a single-year selection. In Power BI Desktop's editing view, use Ctrl + click to follow page-navigation buttons.

No Power BI Service deployment or scheduled refresh is configured as part of this project.

## Refresh on another computer

The report reads local CSV files from `data/processed/`. The folder path saved by the author needs to be changed before refreshing on another computer.

1. Download or clone the repository, keeping the `data/processed/` folder and its filenames intact.
2. Open the PBIX and select **Transform data**.
3. If the queries use the `RootPath` parameter, change its value to the local repository folder. Keep the trailing separator expected by the queries, for example `C:\Projects\uk-transport-delivery-monitor\`.
4. If the report uses individual file paths instead, update those connections through **Data source settings → Change Source**, or edit the affected query's Source step.
5. Select **Close & Apply**, then refresh the report and save a local copy.

There is no need to rebuild the queries or paste new DAX to view the completed report. The PBIX is the reference for the implemented Power Query steps, measures and display labels.

To regenerate the reporting CSVs, first follow the repository README to install dependencies and prepare the pipeline inputs. Once those inputs are ready, `python src/build.py` builds the analytical outputs. The build writes exports only when its SQL checks pass. Power BI imports those exports; it does not query `outputs/monitor.duckdb` directly.

If refresh reports a schema mismatch, compare the CSV headers with the query's expected columns. Do not remove the check or replace errors with zero to make the refresh succeed.

## Reporting tables

All eight exports are stored in `data/processed/`. They use UTF-8 CSV, ISO dates (`YYYY-MM-DD`), lowercase `true`/`false` and empty cells for nulls.

| File | Power BI table | One row represents |
|---|---|---|
| `pbi_observations.csv` | `Observations` | One included project in one release |
| `pbi_annual_variance.csv` | `AnnualVariance` | One observation, including financial inputs and calculation status |
| `pbi_variance_reconciliation.csv` | `VarianceReconciliation` | One observation's published-versus-calculated percentage check |
| `pbi_schedule_pairs.csv` | `SchedulePairs` | One confirmed project pair, with schedule eligibility |
| `pbi_dca_pairs.csv` | `DcaPairs` | One confirmed project pair, with DCA eligibility and origin |
| `pbi_unmatched.csv` | `Unmatched` | One included observation without a confirmed counterpart |
| `pbi_field_availability.csv` | `FieldAvailability` | One release, field and source-status combination |
| `pbi_coverage.csv` | `Coverage` | One reference coverage count |

`observation_id` identifies a project observation. `project_key` links observations of the same confirmed project across releases. Observation-level tables contain 29 rows, each pair table contains 13 rows, and Unmatched contains three rows in the recorded build. FieldAvailability contains aggregated counts: its rows are not individual projects.

## Model and filtering

The model design uses two dimensions derived from Observations:

- **DimRelease:** one row per reporting release, keyed by `release_year`; two rows in this sample.
- **DimProject:** one row per project, keyed by `project_key`; 16 rows in this sample. Display names are taken from the latest available observation.

Relationships filter in one direction, from dimension to reporting table. The dimension key is the unique `1` side, and the reporting table is the `*` side, even where its keys happen to be unique in this small sample.

| Dimension | Related reporting tables |
|---|---|
| `DimRelease[release_year]` | Observations, AnnualVariance, VarianceReconciliation, Unmatched, FieldAvailability |
| `DimProject[project_key]` | Observations, AnnualVariance, VarianceReconciliation, Unmatched, SchedulePairs, DcaPairs |

SchedulePairs and DcaPairs are not related to DimRelease because every row spans both releases. Coverage is a disconnected reference table. There are no fact-to-fact relationships or relationships between the dimensions in this design.

These notes describe the documented model design. Check the PBIX Model view when changing relationships; an automated pipeline check does not verify Power BI's relationship settings.

## Measures and display conventions

- **Financials:** use the numeric columns in AnnualVariance. Baseline and forecast totals use the same rows, filtered to `variance_status = "calculated"`. Monetary measures return a value only for one release at a time. The aggregate percentage is calculated from totals, not averaged from project percentages.
- **Percentage units:** the supplied variance calculation multiplies the ratio by 100. A stored value of `-3.136` therefore means `-3.136%`; applying Power BI's standard Percentage format to it would multiply the displayed value again. Check the measure expression and format together.
- **Schedule:** filter comparisons to `schedule_eligible = true`. An unavailable movement is not zero or unchanged.
- **DCA:** filter transitions to `dca_eligible = true`. The single eligible case is shown separately; no mean rating or sector-wide transition rate is calculated.
- **Availability:** sum `FieldAvailability[observations_with_status]`. Counting rows or distinct status names produces the wrong result. Totals across different reported fields are not used.
- **Missing values:** retain blanks. The status and reason fields distinguish missing, withheld and ineligible information.

Friendly labels can be used in visuals without renaming the source fields. Amounts are in £ million; published end dates can be displayed as day–month–year. Positive financial variance means forecast above baseline, not confirmed overspend.

## Reference values after refresh

The following values come from the recorded analytical build. Use them with no project selections or unintended visual filters. They are comparison targets, not a claim that every Power BI measure has been independently tested.

| Check | 2024–25 | 2025–26 |
|---|---:|---:|
| Included observations | 16 | 13 |
| Financially eligible observations | 16 | 12 |
| Eligible baseline (£m) | 6,987.73 | 5,939.15 |
| Eligible forecast (£m) | 6,637.30 | 5,752.90 |
| Aggregate forecast variance | −5.0149% | −3.1360% |
| Unmatched observations | 3 | 0 |
| End dates available | 11 | 9 |
| End dates not yet available | 1 | 1 |
| End dates withheld | 4 | 3 |

Between releases, there are 13 confirmed pairs: five eligible schedule comparisons, one later published end date, four unchanged published end dates and one eligible DCA comparison. East Coast Main Line moves 335 days later; East Coast Digital Programme changes from AMBER to RED.

Selecting either year should leave the confirmed pair count at 13. For each field in the availability matrix, status counts should sum to 16 or 13 for the selected release. With both releases in context, aggregate monetary measures should be blank.

The fuller SQL reference queries are in `powerbi_reference.sql` in this documentation folder. If a displayed value differs, check the imported export version, active filters, relationships and aggregation before changing the source data.

## Boundaries

The report is a local, static analysis of two published snapshots. It is not a live project-control system. Cross-release monetary comparisons remain disabled, and the DCA comparison describes one eligible case. Source retrieval dates are unknown; recorded verification dates are not substitutes.

Scope, matching and DCA-origin reviews were AI-assisted and are not independent human sign-off. Pipeline checks do not establish the correctness of every interpretation or visual interaction. See [methodology.md](methodology.md) for the full analytical rules and limitations.
