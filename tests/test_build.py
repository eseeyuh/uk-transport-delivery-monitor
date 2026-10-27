"""Tests for src/build.py and the sql/ queries. Most inputs are a small hand-built fixture run through
src/match.py and src/build.py; the last test runs the real project inputs into a temporary directory."""

import csv
import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

import build as b
import match as m

OLD, NEW = "2024-25", "2025-26"
URL = "https://www.gov.uk/example"
OBS_COLUMNS = list(b.REQUIRED_OBSERVATION_COLUMNS)
FIELD_STATUS_FIELDS = ("end_date", "external_dca", "sro_dca")
DESCRIPTIONS = {"ALPHA ROAD": "Dual carriageway widening between two junctions.",
                "BETA RAIL": "Signalling renewal on a branch line.",
                "GAMMA BRIDGE": "Estuary crossing.",
                "DELTA OFFICE": "Headquarters relocation."}


COMMENTARY = {"a:0001": "Summary. The IPA rating is AMBER. More text.", "a:0002": "The SRO rating is GREEN.",
              "b:0001": "The NISTA rating is RED."}


def obs(observation_id, release_year, project_id, name, end=None, external=None, sro=None,
        baseline="", forecast="", published=""):
    """One prepared observation; None means a blank cell, 'exempt' a withheld one."""
    stem, row = observation_id.split(":")
    record = {column: "" for column in OBS_COLUMNS}
    record.update(observation_id=observation_id, release_year=release_year, source_file=f"{stem}.csv",
                  source_row=str(int(row)), project_id_raw=project_id, project_id=project_id,
                  project_id_status="present", project_name_raw=name, department_raw="DfT",
                  annual_report_category_raw="Infrastructure and Construction",
                  project_description_raw=DESCRIPTIONS[name], start_date_status="blank",
                  delivery_confidence_commentary_raw=COMMENTARY.get(observation_id, ""))
    for field, value in (("end_date", end), ("external_dca", external), ("sro_dca", sro)):
        if value is None:
            record[f"{field}_status"] = "blank"
        elif value == "exempt":
            record.update({f"{field}_raw": "Section 43 - Commercial interests", f"{field}_status": "exempt"})
        else:
            record.update({f"{field}_raw": value, field: value, f"{field}_status": "parsed"})
    for field, value in (("fy_baseline_gbp_m", baseline), ("fy_forecast_gbp_m", forecast),
                         ("fy_variance_pct", published)):
        record.update({f"{field}_raw": value, field: value, f"{field}_status": "parsed" if value else "blank"})
    for field in b.AMOUNT_FIELDS:
        record[f"{field}_price_basis_label"] = "not_stated_in_label"
    for field in ("whole_life_cost_gbp_m", "benefits_gbp_m"):
        record[f"{field}_status"] = "blank"
    return record


def base_observations():
    return [
        obs("a:0001", OLD, "DFT_0001_2021-Q1", "ALPHA ROAD", end="2030-01-01", external="AMBER",
            baseline="100.0", forecast="90.0", published="10.0"),
        obs("a:0002", OLD, "DFT_0002_2021-Q1", "BETA RAIL", end="2029-06-30", sro="GREEN",
            baseline="0.0", forecast="5.0"),
        obs("a:0003", OLD, "DFT_0003_2021-Q1", "GAMMA BRIDGE", end="2028-01-01", sro="AMBER",
            baseline="20.0", forecast="25.0", published="25.0"),
        obs("a:0004", OLD, "DFT_0004_2021-Q1", "DELTA OFFICE", end="2027-01-01", sro="RED",
            baseline="5.0", forecast="5.0", published="0.0"),
        obs("b:0001", NEW, "DFT_0001_2021-Q1", "ALPHA ROAD", end="2031-01-01", external="RED",
            baseline="50.0", forecast="55.0", published="10.0"),
        obs("b:0002", NEW, "DFT_0002_2021-Q1", "BETA RAIL", end="exempt", sro=None,
            baseline="8.0", forecast="8.0", published="0.0"),
        obs("b:0004", NEW, "DFT_0004_2021-Q1", "DELTA OFFICE", end="2027-01-01", sro="RED"),
    ]


INCLUDE = {"a:0001": "true", "a:0002": "true", "a:0003": "true", "a:0004": "false",
           "b:0001": "true", "b:0002": "true", "b:0004": "false"}
EXCLUDED = {"a:0004", "b:0004"}


def crosswalk_rows():
    def row(old_id, new_id, canonical, schedule, dca):
        return {"old_observation_id": old_id, "new_observation_id": new_id, "canonical_project_id": canonical,
                "decision": "confirmed", "match_method": "exact_project_id+exact_name+fuzzy_description",
                "schedule_comparable": schedule, "dca_comparable": dca, "monetary_comparable": "false",
                "rationale": "same ID, name and description", "evidence_url": URL, "reviewer": "tester"}
    return [row("a:0001", "b:0001", "DFT_0001_2021-Q1", "true", "true"),
            row("a:0002", "b:0002", "DFT_0002_2021-Q1", "false", "false")]


def origin_rows():
    def row(observation_id, release_year, field, rating, attribution, quote):
        return {"observation_id": observation_id, "release_year": release_year, "rating_field": field,
                "rating": rating, "commentary_attribution": attribution, "evidence_quote": quote,
                "reviewer": "tester", "note": ""}
    return [row("a:0001", OLD, "external", "AMBER", "IPA", "The IPA rating is AMBER"),
            row("a:0002", OLD, "sro", "GREEN", "SRO", "The SRO rating is GREEN"),
            row("a:0003", OLD, "sro", "AMBER", "none", ""),
            row("b:0001", NEW, "external", "RED", "NISTA", "The NISTA rating is RED")]


def write(path: Path, frame: pd.DataFrame) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, lineterminator="\n")
    return path


def make_project(tmp_path, observations=None, field_status_extra=(), origins=None):
    """Write fixture inputs, run src/match.py on them and return build Paths."""
    observations = pd.DataFrame(observations or base_observations(), columns=OBS_COLUMNS, dtype=str)
    status_rows = [
        {"observation_id": r.observation_id, "release_year": r.release_year, "source_file": r.source_file,
         "source_row": r.source_row, "canonical_field": field, "output_field": field, "source_column": field,
         "raw_token": getattr(r, f"{field}_raw"), "parsed_value": getattr(r, field),
         "status": getattr(r, f"{field}_status"), "reason": ""}
        for r in observations.itertuples(index=False) for field in FIELD_STATUS_FIELDS
    ] + list(field_status_extra)
    names = dict(zip(observations["observation_id"], observations["project_name_raw"]))
    scope = pd.DataFrame([{"observation_id": i, "project_name": names[i], "include": inc, "reason": "fixture",
                           "reviewer": "tester"} for i, inc in INCLUDE.items()])
    links = pd.DataFrame([
        {"file_name": f"{stem}.csv", "release_year": year, "source_page_url": f"{URL}/{stem}",
         "download_url": f"{URL}/{stem}.csv", "report_url": f"{URL}/{stem}/report", "sha256": f"sha-{stem}",
         "verified_on": "2026-10-22", "verification": "fixture"} for stem, year in (("a", OLD), ("b", NEW))])
    paths = b.Paths(
        observations=write(tmp_path / "observations.csv", observations),
        field_status=write(tmp_path / "field_status.csv", pd.DataFrame(status_rows)),
        scope_review=write(tmp_path / "scope_review.csv", scope),
        crosswalk=write(tmp_path / "crosswalk.csv", pd.DataFrame(crosswalk_rows())),
        dca_origin_review=write(tmp_path / "dca_origin_review.csv",
                                pd.DataFrame(origins or origin_rows(), columns=list(b.ORIGIN_COLUMNS))),
        match_unmatched=tmp_path / "out" / "unmatched_observations.csv",
        source_links=write(tmp_path / "source_links.csv", links),
        manifest=tmp_path / "manifest.json",
        metadata=tmp_path / "no_metadata.csv",
        sql_dir=b.PROJECT_ROOT / "sql",
        db=tmp_path / "out" / "monitor.duckdb",
        checks=tmp_path / "out" / "sql_checks.csv",
        summary=tmp_path / "out" / "build_summary.md",
        processed_dir=tmp_path / "processed",
    )
    paths.manifest.write_text(json.dumps({"sources": [
        {"file_name": "a.csv", "sha256": "sha-a", "release_year": OLD, "snapshot_date": "2025-03-31",
         "retrieval_date": None},
        {"file_name": "b.csv", "sha256": "sha-b", "release_year": NEW, "snapshot_date": "2026-03-31",
         "retrieval_date": None}]}), encoding="utf-8")
    assert m.main(["--observations", str(paths.observations), "--scope-review", str(paths.scope_review),
                   "--crosswalk", str(paths.crosswalk), "--outputs-dir", str(tmp_path / "out")]) == 0
    return paths


def build_args(paths):
    return [arg for field in b.Paths.__dataclass_fields__
            for arg in (f"--{field.replace('_', '-')}", str(getattr(paths, field)))]


def export(paths, stem):
    with open(paths.processed_dir / f"{stem}.csv", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def built(tmp_path):
    paths = make_project(tmp_path)
    assert b.main(build_args(paths)) == 0
    return paths


# --- unmatched observations stay unmatched, never "completed" ---------------------------

def test_unmatched_records_do_not_become_completed(built):
    unmatched = export(built, "pbi_unmatched")
    assert [(r["observation_id"], r["match_status"]) for r in unmatched] == [("a:0003", "no_candidate")]
    assert unmatched[0]["neutral_label"].startswith("not observed in the other release")
    paired = {r[k] for stem in ("pbi_schedule_pairs", "pbi_dca_pairs") for r in export(built, stem)
              for k in ("old_observation_id", "new_observation_id")}
    assert "a:0003" not in paired
    observation = next(r for r in export(built, "pbi_observations") if r["observation_id"] == "a:0003")
    assert observation["canonical_project_id"] == "" and observation["match_status"] == "no_candidate"
    for stem, _, _ in b.EXPORTS:
        text = (built.processed_dir / f"{stem}.csv").read_text("utf-8").casefold()
        for word in ("complet", "cancel", "closed", "exited", "new project"):
            assert word not in text, (stem, word)


# --- joins preserve row counts ------------------------------------------------------------

def test_joins_preserve_row_counts(built):
    sample = [i for i, inc in INCLUDE.items() if inc == "true"]
    for stem in ("pbi_observations", "pbi_annual_variance", "pbi_variance_reconciliation"):
        assert sorted(r["observation_id"] for r in export(built, stem)) == sorted(sample), stem
    for stem in ("pbi_schedule_pairs", "pbi_dca_pairs"):
        assert len(export(built, stem)) == len(crosswalk_rows()), stem
    checks = pd.read_csv(built.checks, dtype=str, keep_default_na=False)
    assert set(checks["result"]) == {"pass"}
    for check in ("S02", "S06", "S07", "S09", "C01", "C06", "E01", "E03", "R02"):
        assert check in set(checks["check_id"])


def test_duplicated_status_rows_stop_the_build_without_exports(tmp_path):
    extra = [{"observation_id": "a:0001", "release_year": OLD, "source_file": "a.csv", "source_row": "1",
              "canonical_field": "end_date", "output_field": "end_date", "source_column": "end_date",
              "raw_token": "2030-01-01", "parsed_value": "2030-01-01", "status": "parsed", "reason": ""}]
    paths = make_project(tmp_path, field_status_extra=extra)
    assert b.main(build_args(paths)) == 1
    checks = pd.read_csv(paths.checks, dtype=str, keep_default_na=False).set_index("check_id")
    assert checks.at["Q01", "result"] == "fail"
    assert not list(paths.processed_dir.glob("pbi_*.csv"))


# --- exclusions are respected -------------------------------------------------------------

def test_excluded_observations_never_reach_exports(built):
    for stem, _, _ in b.EXPORTS:
        text = (built.processed_dir / f"{stem}.csv").read_text("utf-8")
        for observation_id in EXCLUDED:
            assert observation_id not in text, (stem, observation_id)
    coverage = {(r["measure"], r["release_year"]): r["n"] for r in export(built, "pbi_coverage")}
    assert coverage[("included_observations", OLD)] == "3"
    assert coverage[("included_observations", NEW)] == "2"


# --- repeated runs do not duplicate rows ----------------------------------------------------

def test_repeated_runs_do_not_duplicate_rows(built):
    first = {p.name: p.read_bytes() for p in built.processed_dir.glob("pbi_*.csv")}
    checks_first = built.checks.read_bytes()
    assert b.main(build_args(built)) == 0
    assert b.main(build_args(built)) == 0
    assert {p.name: p.read_bytes() for p in built.processed_dir.glob("pbi_*.csv")} == first
    assert built.checks.read_bytes() == checks_first
    with duckdb.connect(str(built.db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM sql_checks").fetchone()[0] == len(pd.read_csv(built.checks))
        assert con.execute("SELECT count(*) FROM src_observations").fetchone()[0] == len(base_observations())
        assert con.execute("SELECT count(*) FROM confirmed_pairs").fetchone()[0] == 2
    assert not list(built.db.parent.glob("*.tmp"))


# --- calculation rules ----------------------------------------------------------------------

def test_annual_variance_and_zero_baseline(built):
    rows = {r["observation_id"]: r for r in export(built, "pbi_annual_variance")}
    assert float(rows["a:0001"]["variance_gbp_m"]) == -10.0
    assert float(rows["a:0001"]["variance_pct"]) == -10.0
    zero = rows["a:0002"]
    assert float(zero["variance_gbp_m"]) == 5.0
    assert zero["variance_pct"] == ""
    assert zero["variance_pct_status"] == "zero_baseline_no_percentage"
    assert zero["zero_baseline_flag"] == "true"
    for row in rows.values():
        assert row["variance_pct"].casefold() not in ("inf", "-inf", "nan", "infinity")


def test_reconciliation_uses_unsigned_published_values(built):
    rows = {r["observation_id"]: r for r in export(built, "pbi_variance_reconciliation")}
    assert rows["a:0001"]["reconciliation_status"] == "within_tolerance"
    assert rows["a:0002"]["reconciliation_status"] == "not_compared_calculated_unavailable"
    assert rows["a:0001"]["tolerance_pp"] == "0.1"


def test_reconciliation_difference_is_flagged_not_overwritten(tmp_path):
    observations = base_observations()
    observations[0].update(fy_variance_pct_raw="12.0", fy_variance_pct="12.0")
    paths = make_project(tmp_path, observations)
    assert b.main(build_args(paths)) == 0
    row = next(r for r in export(paths, "pbi_variance_reconciliation") if r["observation_id"] == "a:0001")
    assert row["reconciliation_status"] == "outside_tolerance_investigate"
    assert float(row["published_pct"]) == 12.0
    assert float(row["calculated_pct"]) == -10.0
    assert abs(float(row["difference_pp"]) - (-2.0)) < 1e-9


def test_negative_published_variance_stops_the_build(tmp_path):
    observations = base_observations()
    observations[0].update(fy_variance_pct_raw="-10.0", fy_variance_pct="-10.0")
    paths = make_project(tmp_path, observations)
    assert b.main(build_args(paths)) == 1
    checks = pd.read_csv(paths.checks, dtype=str, keep_default_na=False).set_index("check_id")
    assert checks.at["R01", "result"] == "fail"


def test_end_date_movement_only_for_schedule_comparable_pairs(built):
    rows = {r["old_observation_id"]: r for r in export(built, "pbi_schedule_pairs")}
    assert rows["a:0001"]["end_date_movement_days"] == "365"
    assert rows["a:0001"]["movement_label"] == "later published end date"
    assert rows["a:0002"]["end_date_movement_days"] == ""
    assert rows["a:0002"]["schedule_status"] == "not_schedule_comparable"
    assert rows["a:0002"]["old_end_date_raw"] == "2029-06-30"


def test_missing_dca_is_never_an_improvement(built):
    rows = {r["old_observation_id"]: r for r in export(built, "pbi_dca_pairs")}
    assert rows["a:0001"]["dca_transition"] == "Deteriorated"
    assert (rows["a:0001"]["old_concern_rank"], rows["a:0001"]["new_concern_rank"]) == ("2", "3")
    missing = rows["a:0002"]
    assert missing["old_sro_dca_raw"] == "GREEN" and missing["new_sro_dca_status"] == "blank"
    assert missing["dca_transition"] == ""
    assert missing["old_rating_compared"] == "" and missing["new_rating_compared"] == ""
    assert missing["dca_status"] == "not_dca_comparable"


def test_historical_mixed_rating_is_kept_raw_and_excluded(tmp_path):
    observations = base_observations()
    # prepare.py leaves a rating outside GREEN/AMBER/RED unparsed with status "unrecognised"
    observations[5].update(sro_dca_raw="AMBER/RED", sro_dca="", sro_dca_status="unrecognised")
    paths = make_project(tmp_path, observations)
    assert b.main(build_args(paths)) == 0
    row = next(r for r in export(paths, "pbi_dca_pairs") if r["new_observation_id"] == "b:0002")
    assert row["new_sro_dca_raw"] == "AMBER/RED" and row["new_sro_dca_status"] == "unrecognised"
    assert row["dca_transition"] == "" and row["new_rating_compared"] == ""
    with duckdb.connect(str(paths.db), read_only=True) as con:
        assert dict(con.execute("SELECT rating, concern_rank FROM dca_scale").fetchall()) == {
            "GREEN": 1, "AMBER": 2, "RED": 3}


# --- eligibility, exclusion reasons and assessment origin -----------------------------------

def coverage(paths):
    return {(r["measure"], r["release_year"]): int(r["n"]) for r in export(paths, "pbi_coverage")}


def test_ineligible_pairs_are_excluded_not_unchanged(built):
    dca = {r["old_observation_id"]: r for r in export(built, "pbi_dca_pairs")}
    assert dca["a:0001"]["dca_eligible"] == "true" and dca["a:0001"]["dca_exclusion_reason"] == ""
    assert dca["a:0002"]["dca_eligible"] == "false"
    assert dca["a:0002"]["dca_exclusion_reason"] == "rating_unavailable_in_a_release"
    schedule = {r["old_observation_id"]: r for r in export(built, "pbi_schedule_pairs")}
    assert schedule["a:0002"]["schedule_exclusion_reason"] == "end_date_unavailable_in_a_release"
    c = coverage(built)
    assert (c[("dca_eligible_pairs", "both")], c[("dca_unchanged", "both")], c[("dca_improved", "both")],
            c[("dca_deteriorated", "both")], c[("dca_excluded", "both")]) == (1, 0, 0, 1, 1)
    assert (c[("schedule_eligible_pairs", "both")], c[("schedule_later", "both")],
            c[("schedule_unchanged", "both")], c[("schedule_excluded", "both")]) == (1, 1, 0, 1)


def test_origin_is_verified_from_commentary_not_field_name(built):
    row = next(r for r in export(built, "pbi_dca_pairs") if r["old_observation_id"] == "a:0001")
    assert (row["old_origin_status"], row["new_origin_status"]) == ("independent_attributed",) * 2
    assert row["origin_verified_both"] == "true"
    observation = next(r for r in export(built, "pbi_observations") if r["observation_id"] == "a:0003")
    assert observation["dca_origin_status"] == "assessor_not_stated"


def test_dca_comparable_pair_with_unverified_origin_stops_the_build(tmp_path):
    origins = origin_rows()
    origins[3].update(commentary_attribution="NISTA/SRO")
    paths = make_project(tmp_path, origins=origins)
    assert b.main(build_args(paths)) == 1
    checks = pd.read_csv(paths.checks, dtype=str, keep_default_na=False).set_index("check_id")
    assert checks.at["C16", "result"] == "fail"
    assert not list(paths.processed_dir.glob("pbi_*.csv"))


def test_origin_quote_must_occur_in_the_commentary(tmp_path):
    origins = origin_rows()
    origins[0].update(evidence_quote="The IPA rating is RED")
    paths = make_project(tmp_path, origins=origins)
    assert b.main(build_args(paths)) == 1
    checks = pd.read_csv(paths.checks, dtype=str, keep_default_na=False).set_index("check_id")
    assert checks.at["C15", "result"] == "fail"


def test_snapshot_date_and_project_key_reach_the_observation_export(built):
    rows = {r["observation_id"]: r for r in export(built, "pbi_observations")}
    assert rows["a:0001"]["snapshot_date"] == "2025-03-31" and rows["b:0001"]["snapshot_date"] == "2026-03-31"
    assert rows["a:0001"]["project_key"] == rows["b:0001"]["project_key"] == "DFT_0001_2021-Q1"
    assert rows["a:0003"]["project_key"] == "DFT_0003_2021-Q1"
    assert "retrieval_date" not in rows["a:0001"]


# --- project data ------------------------------------------------------------------------

def test_project_build_passes_all_checks(tmp_path):
    defaults = b.default_paths()
    if not defaults.observations.exists() or not defaults.crosswalk.exists():
        pytest.skip("project inputs not prepared")
    paths = b.Paths(**{**{f: getattr(defaults, f) for f in b.Paths.__dataclass_fields__},
                       "db": tmp_path / "monitor.duckdb", "checks": tmp_path / "sql_checks.csv",
                       "summary": tmp_path / "build_summary.md", "processed_dir": tmp_path / "processed"})
    checks = b.run_build(paths)
    assert checks["passed"].all(), checks.loc[~checks["passed"], ["check_id", "description"]]
    assert len(export(paths, "pbi_observations")) == 29
    assert len(export(paths, "pbi_schedule_pairs")) == 13
    c = coverage(paths)
    assert (c[("dca_eligible_pairs", "both")], c[("dca_unchanged", "both")], c[("dca_improved", "both")],
            c[("dca_deteriorated", "both")], c[("dca_excluded", "both")]) == (1, 0, 0, 1, 12)
    assert {r["match_status"] for r in export(paths, "pbi_unmatched")} <= {
        "no_candidate", "candidates_rejected", "pending_review", "split_merge_unresolved"}
