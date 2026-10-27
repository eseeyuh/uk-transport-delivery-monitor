# Source inventory

This document summarises the two raw CSV files used by the UK Transport Delivery Monitor. It records their structure, identifiers and formatting issues before cleaning or matching.

The figures below come from the supplied output of `src/inventory.py`. This is an edited summary, not a new inventory run. Counts in this document describe the full government portfolio unless explicitly labelled DfT; they are not the dashboard's analytical sample.

## Source files

Both files are stored in `data/raw/`. They are comma-delimited UTF-8 CSVs with a byte-order mark (BOM), quoted multiline cells and 21 columns.

| File | Reporting snapshot | Data rows | Size (bytes) | Cells with embedded newlines |
|---|---|---:|---:|---:|
| `nista_2024_25.csv` | 31 March 2025 | 213 | 629,559 | 947 |
| `nista_2025_26.csv` | 31 March 2026 | 189 | 521,941 | 263 |

### Official sources

| Release | Publication | CSV download | HTML report |
|---|---|---|---|
| 2024–25 | [Publication page](https://www.gov.uk/government/publications/nista-annual-report-2024-2025) | [Source CSV](https://assets.publishing.service.gov.uk/media/6895fbb63080e72710b2e2ef/nista_annual_report_data_2425.csv) | [Report](https://www.gov.uk/government/publications/nista-annual-report-2024-2025/nista-annual-report-2024-25) |
| 2025–26 | [Publication page](https://www.gov.uk/government/publications/nista-major-projects-annual-report-2025-26) | [Source CSV](https://assets.publishing.service.gov.uk/media/6a4fb28da6586e258d371bfd/NISTA_Major_Projects_Annual_Report_2025-2026_Data.csv) | [Report](https://www.gov.uk/government/publications/nista-major-projects-annual-report-2025-26/nista-major-projects-annual-report-2025-26) |

The original retrieval dates are unknown. Release years and snapshot dates are recorded in `data/manual/source_metadata.csv`; verified links are recorded separately in `data/reference/source_links.csv`.

The source record states that both official downloads matched the stored files by SHA256 on **22 October 2026**. That is a verification date, not a retrieval date. The links above are taken from that record; they have not been rechecked for this editorial revision.

### File fingerprints

```text
nista_2024_25.csv
SHA256: cac14ed4e6a90a81486bdc24295ea40996e7fd6fcd3834e3ffad8a2685f8c8b3

nista_2025_26.csv
SHA256: 434e3667308157be0539f4ce85f208e7b69af373a2638df457f40c1197d8524e
```

## Column structure

Fifteen column names are identical across the files. Six differ: four monetary headings, the evaluation-plan heading and the identifier heading. A renamed field is not automatically equivalent in meaning.

### Columns shared by exact name

- `Project Name`
- `Department`
- `Annual Report Category`
- `Project Description`
- `IPA Delivery Confidence Assessment`
- `SRO Delivery Confidence Assessment`
- `Departmental Commentary on Delivery Confidence Assessment Rating`
- `Start Date`
- `End Date`
- `Schedule Narrative`
- `Financial Year Variance (%)`
- `In Year Variance Narrative`
- `Costs Narrative`
- `Benefits Narrative`
- `Senior Responsible Owner (SRO) Name`

### Changed headings

| 2024–25 | 2025–26 |
|---|---|
| `Financial Year Baseline (£m)` | `Financial Year Baseline (£m, Presented in 2024/25 Real Prices)` |
| `Financial Year Forecast (£m)` | `Financial Year Forecast (£m, Presented in 2024/25 Real Prices)` |
| `Whole Life Cost (£m)` | `Whole Life Cost (£m, Presented in 2024/25 Real Prices)` |
| `Benefits (£m)` | `Benefits (£m, Presented in 2024/25 Real Prices)` |
| `Does the project have an evaluation plan? (self reported)` | `Evaluation Plan and Registry Link` |
| `GMPP ID` | `Major Projects ID` |

The 2025–26 monetary headings explicitly specify 2024/25 real prices; the earlier headings do not specify a price basis. Cross-release monetary comparisons are disabled. The evaluation-plan change also needs care: a registry-link field is not simply a renamed yes/no response.

Both files retain the external DCA heading `IPA Delivery Confidence Assessment`. Its presence does not establish the assessor for every row; the project records assessment-origin evidence separately.

The approved field mapping is in `data/manual/column_mapping.csv`.

## Department for Transport records

The source uses `DfT` in 2024–25 and `DFT` in 2025–26. The inventory identifies these records by trimming and case-folding the department label for comparison, while retaining the published labels.

| Category | 2024–25 | 2025–26 |
|---|---:|---:|
| Infrastructure and Construction | 16 | 13 |
| Government Transformation and Service Delivery | 3 | 3 |
| **All DfT records** | **19** | **16** |

The dashboard's reviewed infrastructure sample contains 16 and 13 observations respectively. Scope decisions are stored in `data/manual/scope_review.csv`; source category counts alone do not establish project matches or explain why records disappear.

For context, the full files each contain 20 department labels and four categories:

| Category | 2024–25 | 2025–26 |
|---|---:|---:|
| Infrastructure and Construction | 68 | 68 |
| Government Transformation and Service Delivery | 76 | 61 |
| Military Capability | 45 | 38 |
| ICT / Information and Communications Technology (ICT) | 24 | 22 |

The ICT label is abbreviated in the earlier file and expanded in the later one. Other department labels also change case, such as `MoD` to `MOD` and `MoJ` to `MOJ`.

## Identifiers and duplicate checks

| Check | 2024–25 | 2025–26 |
|---|---:|---:|
| Data rows | 213 | 189 |
| Non-blank project IDs | 213 | 187 |
| Distinct non-blank project IDs | 213 | 187 |
| Blank project IDs | 0 | 2 |
| Duplicate non-blank ID values | 0 | 0 |
| Fully identical rows | 0 | 0 |

All non-blank IDs match the inventory's expected pattern, `[A-Z]+_\d{4}_\d{4}-Q[1-4]`. Project names and project-name-plus-department combinations are unique within each file, both as exact strings and after trimming and case-folding.

The two blank IDs in 2025–26 belong to:

- Flood and Coastal Risk Management (FCRM) Investment Programme 2026–2029;
- Children's Homes Capital Programme.

There are **170 distinct non-blank IDs shared exactly across the full files**. This is candidate overlap, not a validated matched cohort. The transport analysis uses its separately reviewed project crosswalk.

## Values that need careful parsing

The inventory reads every cell as text with `keep_default_na=False`. It does not clean values, assign analytical availability classes or match projects. A non-empty value can be a disclosure statement rather than a usable number or date.

### Dates

| Field | 2024–25 single-date pattern | 2025–26 single-date pattern |
|---|---:|---:|
| Start Date | 201 | 167 |
| End Date | 155 | 139 |

These counts refer to whole-cell `dd/mm/yyyy` patterns, not every date mentioned in a cell. None of the pattern-matching values failed the inventory's calendar-validity check. Other entries include exemption text, planning-stage statements and Low/Mid/High date scenarios. They are not automatically converted into a single date.

### Financial fields

The counts below combine plain numeric tokens and comma-grouped numeric tokens; they do not imply that every record qualifies for an analytical calculation.

| Field | 2024–25 numeric tokens | 2025–26 numeric tokens |
|---|---:|---:|
| Annual baseline | 178 | 145 |
| Annual forecast | 178 | 145 |
| Published annual variance | 177 | 144 |
| Whole-life cost | 148 | 132 |
| Benefits | 168 | 84 |

The later file includes thousands separators such as `1,007.93`. Both releases also contain scenario strings such as `Low: 2,750.00, Mid: 2,820.00, High: 3,100.00`. These must remain distinguishable from single amounts; the analysis does not substitute a midpoint.

### Blanks and disclosure text

Blank cells, exemption statements and planning-stage statements occur throughout the files. Common examples include commercial-interest exemptions, information intended for future publication and statements that cost or schedule information is not yet available.

The wording varies between releases: `Section 43 - Commercial interests` may appear as `Exempt under Section 43 – Commercial interests`, sometimes with an invisible trailing character. Some cells combine more than one reason. These differences matter when classifying availability; a non-numeric token is not necessarily a processing error.

For example, the full 2025–26 file has 72 blank benefits cells and 117 blank evaluation-plan cells. These counts are separate from the narrower DfT availability matrix.

### Whitespace and multiline text

Quoted narrative cells contain embedded line breaks. The files also contain tabs, non-breaking spaces, narrow non-breaking spaces and zero-width spaces:

| Character | Code point | Cells containing it, 2024–25 | Cells containing it, 2025–26 |
|---|---|---:|---:|
| Tab | U+0009 | 34 | 18 |
| Non-breaking space | U+00A0 | 6 | 22 |
| Zero-width space | U+200B | 3 | 150 |
| Narrow non-breaking space | U+202F | 10 | 15 |

Counts are per character type and may overlap within a cell. These formatting details can break exact text comparisons even when values look identical on screen. Raw text is retained; any normalisation belongs in the preparation stage.

## How to use this inventory

This summary explains what is present in the source files. It does not establish analytical comparability or validate published figures. Use [methodology.md](../docs/methodology.md) for calculation and eligibility rules, and the recorded mapping, scope and crosswalk files for project decisions.

`src/inventory.py` can regenerate the detailed machine-produced inventory. This edited summary omits repetitive full-portfolio frequency tables and long lists of raw tokens; it does not replace the raw CSVs or the recorded metadata. If the source files change, regenerate the inventory and update this document's counts and fingerprints.
