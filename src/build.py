"""Build the analytical DuckDB database, run the SQL checks and write the Power BI CSV exports.

Inputs (read only):
    data/processed/observations.csv      from src/prepare.py
    data/processed/field_status.csv      from src/prepare.py
    data/manual/scope_review.csv         reviewed scope; include must be true/false
    data/manual/project_crosswalk.csv    reviewed crosswalk; must pass the src/match.py validation
    outputs/unmatched_observations.csv   from src/match.py; cross-checked against the SQL result
    data/reference/source_links.csv      download links whose files' SHA256 equals the manifest
    data/manual/dca_origin_review.csv    reviewed assessor attribution, with a verbatim commentary quote,
                                         for every sample observation with a parsed DCA rating
    data/source_manifest.json            raw file SHA256, release_year and snapshot_date from src/inventory.py
    data/manual/source_metadata.csv      user-supplied links, if any, must agree with source_links.csv
    sql/01_scope.sql .. sql/04_reconciliation.sql   analytical queries and checks, run in order

Outputs:
    outputs/monitor.duckdb          rebuilt from an empty file on every run (no appended rows)
    outputs/sql_checks.csv          every SQL check with actual, expected and pass/fail
    outputs/build_summary.md        coverage, comparisons and reconciliation findings
    data/processed/pbi_*.csv        Power BI exports, written only when every check passes; earlier
                                    exports are removed first so a failed build leaves none behind

Usage:
    python src/build.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

import match
from inventory import PROJECT_ROOT, code, relpath, table, write_text
from match import MatchError, read_csv
from prepare import write_csv

SQL_FILES = ("01_scope.sql", "02_quality.sql", "03_comparison.sql", "04_reconciliation.sql")
# Methodology section 4.3: initial tolerance for calculated vs published percentages. The epsilon only
# absorbs binary floating-point error in the comparison.
VARIANCE_TOLERANCE_PP = 0.1
COMPARISON_EPSILON = 1e-9
# (file stem in the processed directory, export view, sort order)
EXPORTS = (
    ("pbi_observations", "export_observations", "release_year, source_row"),
    ("pbi_annual_variance", "export_annual_variance", "release_year, source_row"),
    ("pbi_variance_reconciliation", "export_variance_reconciliation", "release_year, source_row"),
    ("pbi_schedule_pairs", "export_schedule_pairs", "old_observation_id, new_observation_id"),
    ("pbi_dca_pairs", "export_dca_pairs", "old_observation_id, new_observation_id"),
    ("pbi_unmatched", "export_unmatched", "release_year, source_row"),
    ("pbi_field_availability", "export_field_availability", "release_year, output_field, status"),
    ("pbi_coverage", "export_coverage", "sort_order"),
)
# Observation columns read by sql/; checked before loading so a missing column fails clearly.
AMOUNT_FIELDS = ("fy_baseline_gbp_m", "fy_forecast_gbp_m", "whole_life_cost_gbp_m", "benefits_gbp_m")
REQUIRED_OBSERVATION_COLUMNS = tuple(dict.fromkeys([
    *match.OBSERVATION_COLUMNS, "project_id_status", "start_date", "external_dca_source_column",
    "sro_dca_source_column", "delivery_confidence_commentary_raw",
    *(f"{field}{suffix}" for field in AMOUNT_FIELDS for suffix in ("_raw", "", "_status", "_price_basis_label")),
    "fy_variance_pct_raw", "fy_variance_pct", "fy_variance_pct_status",
]))
LINK_COLUMNS = ("file_name", "release_year", "source_page_url", "download_url", "report_url", "sha256",
                "verified_on", "verification")
URL_FIELDS = ("source_page_url", "download_url")
ORIGIN_COLUMNS = ("observation_id", "release_year", "rating_field", "rating", "commentary_attribution",
                  "evidence_quote", "reviewer", "note")
# Who the departmental commentary credits with the published rating; sql/03_comparison.sql derives the
# origin status from this and the field holding the rating.
COMMENTARY_ATTRIBUTIONS = ("IPA", "NISTA", "NISTA/SRO", "SRO", "none")
MANIFEST_FIELDS = ("file_name", "sha256", "release_year", "snapshot_date", "retrieval_date")


class BuildError(Exception):
    """Raised when an input fails a precondition for the build."""


@dataclass(frozen=True)
class Paths:
    observations: Path
    field_status: Path
    scope_review: Path
    crosswalk: Path
    dca_origin_review: Path
    match_unmatched: Path
    source_links: Path
    manifest: Path
    metadata: Path
    sql_dir: Path
    db: Path
    checks: Path
    summary: Path
    processed_dir: Path


def default_paths() -> Paths:
    root = PROJECT_ROOT
    return Paths(
        observations=root / "data" / "processed" / "observations.csv",
        field_status=root / "data" / "processed" / "field_status.csv",
        scope_review=root / "data" / "manual" / "scope_review.csv",
        crosswalk=root / "data" / "manual" / "project_crosswalk.csv",
        dca_origin_review=root / "data" / "manual" / "dca_origin_review.csv",
        match_unmatched=root / "outputs" / "unmatched_observations.csv",
        source_links=root / "data" / "reference" / "source_links.csv",
        manifest=root / "data" / "source_manifest.json",
        metadata=root / "data" / "manual" / "source_metadata.csv",
        sql_dir=root / "sql",
        db=root / "outputs" / "monitor.duckdb",
        checks=root / "outputs" / "sql_checks.csv",
        summary=root / "outputs" / "build_summary.md",
        processed_dir=root / "data" / "processed",
    )


# --- inputs -----------------------------------------------------------------

def load_manifest(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise BuildError(f"{relpath(path)} not found; run src/inventory.py first")
    sources = json.loads(path.read_text("utf-8"))["sources"]
    # null (not supplied) becomes an empty string, like a blank cell in the other inputs
    return pd.DataFrame([[s.get(field) or "" for field in MANIFEST_FIELDS] for s in sources],
                        columns=list(MANIFEST_FIELDS), dtype=str)


def load_dca_origin_review(path: Path) -> pd.DataFrame:
    """Reviewed assessor attribution; agreement with the observations is checked in sql/03_comparison.sql."""
    if not path.exists():
        raise BuildError(f"{relpath(path)} not found")
    review = read_csv(path)
    if tuple(review.columns) != ORIGIN_COLUMNS:
        raise BuildError(f"{relpath(path)}: columns must be {list(ORIGIN_COLUMNS)!r}")
    if review["observation_id"].duplicated().any():
        raise BuildError(f"{relpath(path)}: duplicate observation_id rows")
    bad = sorted(set(review["commentary_attribution"]) - set(COMMENTARY_ATTRIBUTIONS))
    if bad or not review["rating_field"].isin(["external", "sro"]).all():
        raise BuildError(f"{relpath(path)}: commentary_attribution must be one of {list(COMMENTARY_ATTRIBUTIONS)!r} "
                         f"(found {bad!r}) and rating_field external or sro")
    if (review["reviewer"] == "").any():
        raise BuildError(f"{relpath(path)}: every row needs a reviewer")
    return review


def load_source_links(path: Path, metadata_path: Path, source_files: set[str]) -> pd.DataFrame:
    """Verified links, one per source file; user-supplied URLs, where present, must agree."""
    links = read_csv(path)
    if tuple(links.columns) != LINK_COLUMNS:
        raise BuildError(f"{relpath(path)}: columns must be {list(LINK_COLUMNS)!r}")
    missing = sorted(source_files - set(links["file_name"]))
    if missing or links["file_name"].duplicated().any():
        raise BuildError(f"{relpath(path)}: need exactly one row per source file; missing {missing!r}")
    if metadata_path.exists():
        metadata = read_csv(metadata_path).set_index("file_name")
        for row in links.itertuples(index=False):
            for field in URL_FIELDS:
                supplied = metadata.at[row.file_name, field] if row.file_name in metadata.index else ""
                if supplied and supplied != getattr(row, field):
                    raise BuildError(f"{row.file_name}: {field} in {relpath(metadata_path)} ({supplied}) differs "
                                     f"from {relpath(path)} ({getattr(row, field)})")
    return links


def load_inputs(paths: Paths) -> dict[str, pd.DataFrame]:
    observations = match.load_observations(paths.observations)
    missing = [c for c in REQUIRED_OBSERVATION_COLUMNS if c not in observations.columns]
    if missing:
        raise BuildError(f"{relpath(paths.observations)}: missing columns {missing!r}; rerun src/prepare.py")
    scope = match.load_scope(paths.scope_review, observations)
    frame = match.build_frame(observations, scope)
    old_year, new_year = match.release_years(frame)
    crosswalk = match.load_crosswalk(paths.crosswalk)
    errors = match.validate_crosswalk(crosswalk, frame, old_year, new_year)
    if errors:
        raise BuildError(f"{relpath(paths.crosswalk)} failed validation:\n  " + "\n  ".join(errors))
    return {
        "src_observations": observations,
        "src_field_status": read_csv(paths.field_status),
        "src_scope_review": scope,
        "src_crosswalk": crosswalk,
        "src_dca_origin_review": load_dca_origin_review(paths.dca_origin_review),
        "src_match_unmatched": read_csv(paths.match_unmatched),
        "src_source_links": load_source_links(paths.source_links, paths.metadata, set(observations["source_file"])),
        "src_manifest": load_manifest(paths.manifest),
    }


# --- database ---------------------------------------------------------------

def sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def create_database(con: duckdb.DuckDBPyConnection, inputs: dict[str, pd.DataFrame], sql_dir: Path) -> pd.DataFrame:
    """Load inputs as VARCHAR tables, run the SQL files in order and return the checks."""
    for name, frame in inputs.items():
        con.register("input_frame", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM input_frame")
        con.unregister("input_frame")
    con.execute("CREATE TABLE params (variance_tolerance_pp DOUBLE, comparison_epsilon DOUBLE)")
    con.execute("INSERT INTO params VALUES (?, ?)", [VARIANCE_TOLERANCE_PP, COMPARISON_EPSILON])
    con.execute("CREATE TABLE sql_checks (check_id VARCHAR, stage VARCHAR, description VARCHAR, "
                "actual VARCHAR, expected VARCHAR, passed BOOLEAN)")
    for name in SQL_FILES:
        path = sql_dir / name
        if not path.exists():
            raise BuildError(f"{relpath(path)} not found")
        try:
            con.execute(path.read_text("utf-8"))
        except duckdb.Error as exc:
            raise BuildError(f"{relpath(path)} failed: {exc}") from exc
    return con.execute("SELECT * FROM sql_checks ORDER BY stage, check_id").df()


def export_paths(processed_dir: Path) -> list[Path]:
    return [processed_dir / f"{stem}.csv" for stem, _, _ in EXPORTS]


def write_exports(con: duckdb.DuckDBPyConnection, processed_dir: Path) -> dict[str, int]:
    processed_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for (stem, view, order), path in zip(EXPORTS, export_paths(processed_dir)):
        con.execute(f"COPY (SELECT * FROM {view} ORDER BY {order}) TO '{sql_path(path)}' (FORMAT csv, HEADER true)")
        counts[stem] = con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
    return counts


# --- summary ----------------------------------------------------------------

def rows(con: duckdb.DuckDBPyConnection, query: str) -> list[tuple]:
    return con.execute(query).fetchall()


def fmt(value, digits: int = 2) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def build_summary(con: duckdb.DuckDBPyConnection, checks: pd.DataFrame, exported: dict[str, int]) -> str:
    passed = int(checks["passed"].sum())
    lines = [
        "# Build summary", "",
        "Generated by `src/build.py` from the reviewed scope and crosswalk. Database: `outputs/monitor.duckdb`; "
        "checks: `outputs/sql_checks.csv`. Values are as published; movements are differences between "
        "published end dates and DCA labels describe published ratings, not causes or outcomes.", "",
        f"SQL checks passed: {passed} of {len(checks)}.", "",
    ]
    if passed < len(checks):
        failed = checks[~checks["passed"]]
        lines += ["## Failed checks", "", "Exports were not written.", ""]
        lines += table(["Check", "Description", "Actual", "Expected"],
                       [[r.check_id, r.description, r.actual, r.expected] for r in failed.itertuples(index=False)])

    lines += ["## Coverage", ""]
    lines += table(["Group", "Measure", "Release", "Count"], [list(r) for r in rows(
        con, "SELECT measure_group, measure, release_year, n FROM coverage ORDER BY sort_order")])

    lines += ["## End-date movement (schedule-comparable pairs)", "",
              "New minus old published end date, in days. Not a measure of delay, cause or responsibility.", ""]
    lines += table(["Canonical ID", "Project (new name)", "Old end date", "New end date", "Days", "Label"], [
        [code(r[0]), code(r[1]), r[2], r[3], r[4], r[5]] for r in rows(con, """
            SELECT c.canonical_project_id, n.project_name_raw, c.old_end_date, c.new_end_date,
                   c.end_date_movement_days, c.movement_label
            FROM schedule_comparison c JOIN sample_observations n ON n.observation_id = c.new_observation_id
            WHERE c.end_date_movement_days IS NOT NULL ORDER BY c.canonical_project_id""")
    ])
    lines += table(["Schedule eligibility", "Pairs"], [list(r) for r in rows(con, """
        SELECT coalesce(schedule_exclusion_reason, 'eligible (movement calculated)'), count(*)
        FROM schedule_comparison GROUP BY ALL ORDER BY 1""")])

    lines += ["## DCA transitions (strict comparison)", "",
              "Only pairs the reviewer marked DCA-comparable, using ratings from the same field in both releases "
              "whose commentary credits the same type of assessor as the field (data/manual/dca_origin_review.csv). "
              "Excluded pairs are not counted as unchanged.", ""]
    lines += table(["Canonical ID", "Field", "Old rating", "New rating", "Transition"], [
        [code(r[0]), r[1], r[2], r[3], r[4]] for r in rows(con, """
            SELECT canonical_project_id, same_field, old_rating_compared, new_rating_compared, dca_transition
            FROM dca_comparison WHERE dca_transition IS NOT NULL ORDER BY canonical_project_id""")
    ])
    lines += table(["DCA eligibility", "Pairs"], [list(r) for r in rows(con, """
        SELECT coalesce(dca_exclusion_reason, 'eligible (transition calculated)'), count(*)
        FROM dca_comparison GROUP BY ALL ORDER BY 1""")])
    lines += table(["Rating field changed between releases", "Pairs"], [list(r) for r in rows(con, """
        SELECT CAST(rating_field_changed AS VARCHAR), count(*) FROM dca_comparison GROUP BY ALL ORDER BY 1""")])

    lines += ["## Annual forecast variance against annual baseline (sample)", "",
              "Within each release only; annual figures from different releases cover different fiscal years "
              "and are never compared or summed.", ""]
    lines += table(["Release", "Variance status", "Percentage status", "Observations"], [list(r) for r in rows(con, """
        SELECT release_year, variance_status, variance_pct_status, count(*) FROM annual_variance
        GROUP BY ALL ORDER BY 1, 2, 3""")])

    lines += ["## Reconciliation with published variance", "",
              f"Published `Financial Year Variance (%)` is compared with the magnitude of the calculated percentage "
              f"(the published column is unsigned; see `sql/04_reconciliation.sql`). Tolerance "
              f"{VARIANCE_TOLERANCE_PP} percentage point.", "", "Sample:", ""]
    lines += table(["Release", "Status", "Observations"], [list(r) for r in rows(con, """
        SELECT release_year, reconciliation_status, count(*) FROM variance_reconciliation GROUP BY ALL ORDER BY 1, 2""")])
    outside = rows(con, """
        SELECT r.observation_id, s.project_name_raw, r.calculated_pct, r.published_pct, r.difference_pp
        FROM variance_reconciliation r JOIN sample_observations s ON s.observation_id = r.observation_id
        WHERE r.reconciliation_status = 'outside_tolerance_investigate' ORDER BY 1""")
    if outside:
        lines += table(["Observation", "Project", "Calculated %", "Published %", "Difference (pp)"],
                       [[r[0], code(r[1]), fmt(r[2], 4), fmt(r[3]), fmt(r[4], 4)] for r in outside])
    else:
        lines += ["No sample observation is outside the tolerance.", ""]
    lines += ["Whole portfolio (diagnostic only; not exported, outside the analytical scope):", ""]
    lines += table(["Release", "Status", "Observations"], [list(r) for r in rows(con, """
        SELECT release_year, reconciliation_status, count(*) FROM variance_reconciliation_all
        WHERE reconciliation_status IN ('within_tolerance', 'outside_tolerance_investigate')
        GROUP BY ALL ORDER BY 1, 2""")])
    portfolio_outside = rows(con, """
        SELECT r.observation_id, o.department_raw, r.calculated_pct, r.published_pct, r.difference_pp
        FROM variance_reconciliation_all r JOIN src_observations o ON o.observation_id = r.observation_id
        WHERE r.reconciliation_status = 'outside_tolerance_investigate' ORDER BY 1""")
    if portfolio_outside:
        lines += table(["Observation", "Department", "Calculated %", "Published %", "Difference (pp)"],
                       [[r[0], code(r[1]), fmt(r[2], 4), fmt(r[3]), fmt(r[4], 4)] for r in portfolio_outside])

    lines += ["## Included observations without a confirmed pair", ""]
    lines += table(["Observation", "Project", "Status", "Label"], [
        [r[0], code(r[1]), r[2], r[3]] for r in rows(con, """
            SELECT observation_id, project_name_raw, match_status, neutral_label FROM unmatched_observations
            ORDER BY observation_id""")
    ])

    lines += ["## Exports", ""]
    if exported:
        lines += table(["File", "Rows"], [[f"data/processed/{stem}.csv", n] for stem, n in exported.items()])
    else:
        lines += ["None written.", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


# --- driver -----------------------------------------------------------------

def run_build(paths: Paths) -> pd.DataFrame:
    """Rebuild everything; returns the checks. Exports are written only when every check passes."""
    inputs = load_inputs(paths)
    for path in export_paths(paths.processed_dir):
        path.unlink(missing_ok=True)
    paths.db.parent.mkdir(parents=True, exist_ok=True)
    temp_db = paths.db.with_name(paths.db.name + ".tmp")
    temp_db.unlink(missing_ok=True)
    with duckdb.connect(str(temp_db)) as con:
        checks = create_database(con, inputs, paths.sql_dir)
        exported = write_exports(con, paths.processed_dir) if checks["passed"].all() else {}
        summary = build_summary(con, checks, exported)
    os.replace(temp_db, paths.db)
    report = checks.assign(result=checks["passed"].map({True: "pass", False: "fail"})).drop(columns="passed")
    write_csv(report, paths.checks)
    write_text(paths.summary, summary)
    return checks


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    defaults = default_paths()
    for field in Paths.__dataclass_fields__:
        parser.add_argument(f"--{field.replace('_', '-')}", type=Path, default=getattr(defaults, field))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    paths = Paths(**{field: getattr(args, field) for field in Paths.__dataclass_fields__})
    try:
        checks = run_build(paths)
    except (BuildError, MatchError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    failed = checks[~checks["passed"]]
    print(f"wrote {relpath(paths.db)}, {relpath(paths.checks)}, {relpath(paths.summary)}")
    print(f"SQL checks: {len(checks) - len(failed)} passed, {len(failed)} failed")
    if not failed.empty:
        for row in failed.itertuples(index=False):
            print(f"  FAIL {row.check_id} {row.description}: actual {row.actual}, expected {row.expected}",
                  file=sys.stderr)
        print("exports not written", file=sys.stderr)
        return 1
    for path in export_paths(paths.processed_dir):
        print(f"  {relpath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
