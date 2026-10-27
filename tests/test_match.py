"""Tests for src/match.py candidate and crosswalk rules. Inputs are hand-built rows, not release data."""

import pandas as pd
import pytest

import match as m

OLD, NEW = "2024-25", "2025-26"
URL = "https://www.gov.uk/example"


def obs(observation_id, release_year, project_id="", name="", description="", department="DfT",
        category="Infrastructure and Construction", **extra):
    row = {column: "" for column in m.OBSERVATION_COLUMNS}
    row.update(observation_id=observation_id, release_year=release_year, source_file=f"{release_year}.csv",
               source_row=observation_id.split(":")[1], project_id_raw=project_id, project_id=project_id,
               project_name_raw=name, department_raw=department, annual_report_category_raw=category,
               project_description_raw=description)
    row.update(extra)
    return row


def make_frame(rows, include):
    """include maps observation_id -> 'true'/'false' for rows in the scope review."""
    observations = pd.DataFrame(rows, dtype=str)
    scope = pd.DataFrame({
        "observation_id": list(include),
        "project_name": [observations.set_index("observation_id").at[i, "project_name_raw"] for i in include],
        "include": list(include.values()),
    }, dtype=str)
    return m.build_frame(observations, scope)


def pairs(candidates):
    return dict(zip(zip(candidates["old_observation_id"], candidates["new_observation_id"]),
                    candidates["match_method"]))


def crosswalk_row(old_id, new_id, decision=m.NEEDS_REVIEW, **values):
    row = {column: "" for column in m.CROSSWALK_COLUMNS}
    row.update(old_observation_id=old_id, new_observation_id=new_id, decision=decision,
               match_method="exact_project_id", monetary_comparable=m.FALSE)
    if decision != m.NEEDS_REVIEW:
        row.update(rationale="checked descriptions", reviewer="tester")
    if decision == m.CONFIRMED:
        row.update(canonical_project_id=f"P-{old_id}", schedule_comparable=m.FALSE, dca_comparable=m.FALSE,
                   evidence_url=URL)
    row.update(values)
    return row


def crosswalk(*rows):
    return pd.DataFrame(list(rows), columns=list(m.CROSSWALK_COLUMNS), dtype=str)


@pytest.fixture
def frame():
    rows = [
        obs("old:0001", OLD, "DFT_0001_2021-Q1", "A1 UPGRADE", "Widen the A1 between two junctions."),
        obs("old:0002", OLD, "DFT_0002_2021-Q1", "RAIL HUB", "New chords into the city."),
        obs("old:0003", OLD, "DFT_0003_2021-Q1", "LONE PROJECT", "Something with no successor."),
        obs("new:0001", NEW, "DFT_0001_2021-Q1", "A1 Upgrade", "Widen the A1 between two junctions."),
        obs("new:0002", NEW, "DFT_0099_2526-Q1", "RAIL HUB", "New chords into the city centre."),
        obs("new:0003", NEW, "", "UNRELATED SCHEME", "Different work entirely."),
    ]
    include = {i: m.TRUE for i in ("old:0001", "old:0002", "old:0003", "new:0001", "new:0002", "new:0003")}
    return make_frame(rows, include)


# --- candidates are proposals, never decisions --------------------------------

def test_exact_id_and_name_propose_but_crosswalk_awaits_review(frame):
    candidates = m.propose_candidates(frame, OLD, NEW)
    found = pairs(candidates)
    assert found[("old:0001", "new:0001")] == "exact_project_id+normalised_name+fuzzy_description"
    assert found[("old:0002", "new:0002")] == "exact_name+fuzzy_description"
    xw = m.build_crosswalk(candidates)
    assert set(xw["decision"]) == {m.NEEDS_REVIEW}
    assert set(xw["monetary_comparable"]) == {m.FALSE}
    for column in ("canonical_project_id", "schedule_comparable", "dca_comparable", "rationale", "evidence_url",
                   "reviewer"):
        assert set(xw[column]) == {""}
    assert m.validate_crosswalk(xw, frame, OLD, NEW) == []
    assert m.confirmed_pairs(xw, frame).empty


def test_blank_ids_never_match_each_other():
    rows = [obs("old:0001", OLD, "", "ALPHA", "x"), obs("new:0001", NEW, "", "OMEGA", "y")]
    frame = make_frame(rows, {"old:0001": m.TRUE, "new:0001": m.TRUE})
    assert m.propose_candidates(frame, OLD, NEW).empty


def test_fuzzy_name_suggests_above_threshold_only():
    rows = [
        obs("old:0001", OLD, "", "EAST WEST RAIL CONNECTION STAGE 1", "a"),
        obs("new:0001", NEW, "", "EAST WEST RAIL CONNECTION STAGE 2", "b"),
        obs("new:0002", NEW, "", "MIDLANDS ROAD FUND", "c"),
    ]
    frame = make_frame(rows, {r["observation_id"]: m.TRUE for r in rows})
    found = pairs(m.propose_candidates(frame, OLD, NEW))
    assert found == {("old:0001", "new:0001"): "fuzzy_name"}


def test_pairs_with_no_in_sample_side_are_not_proposed():
    rows = [obs("old:0001", OLD, "DFT_0001_2021-Q1", "REFORM", "a"), obs("new:0001", NEW, "DFT_0001_2021-Q1",
                                                                          "REFORM", "a")]
    frame = make_frame(rows, {"old:0001": m.FALSE, "new:0001": m.FALSE})
    assert m.propose_candidates(frame, OLD, NEW).empty


def test_exact_id_found_outside_scope_review_and_flagged_as_boundary():
    rows = [obs("old:0001", OLD, "DFT_0001_2021-Q1", "BRIDGE", "a"),
            obs("new:0001", NEW, "DFT_0001_2021-Q1", "BRIDGE", "a", department="DLUHC")]
    frame = make_frame(rows, {"old:0001": m.TRUE})
    candidates = m.propose_candidates(frame, OLD, NEW)
    assert pairs(candidates) == {("old:0001", "new:0001"): "exact_project_id+exact_name"}
    review = m.build_review(candidates, m.build_crosswalk(candidates), frame)
    assert review.loc[0, "new_scope_include"] == ""
    assert review.loc[0, "new_department"] == "DLUHC"


# --- cardinality and join multiplication -----------------------------------------

def many_to_many_frame():
    rows = [
        obs("old:0001", OLD, "", "EAST WEST RAIL CONNECTION STAGE 1", "a"),
        obs("old:0002", OLD, "", "EAST WEST RAIL CONNECTION STAGE 2 AND 3", "b"),
        obs("new:0001", NEW, "", "EAST WEST RAIL CONNECTION STAGE 1", "c"),
        obs("new:0002", NEW, "", "EAST WEST RAIL CONNECTION STAGE 2 AND 3", "d"),
    ]
    return make_frame(rows, {r["observation_id"]: m.TRUE for r in rows})


def test_overlapping_candidates_form_one_many_to_many_component():
    frame = many_to_many_frame()
    candidates = m.propose_candidates(frame, OLD, NEW)
    review = m.build_review(candidates, m.build_crosswalk(candidates), frame)
    assert len(review) == 4
    assert set(review["cardinality"]) == {"many_to_many"}
    assert set(review["component_id"]) == {"C001"}


def test_pair_structure_labels():
    links = [("o1", "n1"), ("o1", "n2"), ("o2", "n3"), ("o3", "n3"), ("o4", "n4"), ("o5", "n5"),
             ("o6", "n6"), ("o6", "n7"), ("o7", "n6")]
    review = pd.DataFrame(links, columns=m.PAIR_KEY)
    review["decision"] = [m.NEEDS_REVIEW] * 5 + [m.REJECTED] + [m.NEEDS_REVIEW] * 3
    structure = m.pair_structure(review)
    assert list(structure["cardinality"]) == [
        "one_to_many", "one_to_many", "many_to_one", "many_to_one", "one_to_one", "rejected",
        "many_to_many", "one_to_many", "many_to_one"]
    assert list(structure["component_id"]) == ["C001", "C001", "C002", "C002", "C003", "", "C004", "C004", "C004"]
    assert list(structure["old_candidate_pairs"]) == ["2", "2", "1", "1", "1", "", "2", "2", "1"]


def test_rejections_resolve_cardinality():
    frame = many_to_many_frame()
    candidates = m.propose_candidates(frame, OLD, NEW)
    keep = {("old:0001", "new:0001"), ("old:0002", "new:0002")}
    xw = crosswalk(*[crosswalk_row(a, b, m.CONFIRMED if (a, b) in keep else m.REJECTED,
                                   match_method=method)
                     for (a, b), method in pairs(candidates).items()])
    assert m.validate_crosswalk(xw, frame, OLD, NEW) == []
    review = m.build_review(candidates, xw, frame)
    kept = review[review["decision"] == m.CONFIRMED]
    assert set(kept["cardinality"]) == {"one_to_one"}
    assert set(review.loc[review["decision"] == m.REJECTED, "cardinality"]) == {"rejected"}
    joined = m.confirmed_pairs(xw, frame)
    assert len(joined) == 2


def test_confirmed_one_to_many_is_rejected_by_validation():
    frame = many_to_many_frame()
    xw = crosswalk(crosswalk_row("old:0002", "new:0001", m.CONFIRMED, canonical_project_id="EWR-A"),
                   crosswalk_row("old:0002", "new:0002", m.CONFIRMED, canonical_project_id="EWR-B"))
    errors = m.validate_crosswalk(xw, frame, OLD, NEW)
    assert any("old_observation_id 'old:0002' is in 2 confirmed pairs" in e for e in errors)
    with pytest.raises(m.MatchError, match="multiply"):
        m.confirmed_pairs(xw, frame)


def test_split_merge_is_allowed_but_not_with_confirmed_on_same_observation():
    frame = many_to_many_frame()
    split = crosswalk(crosswalk_row("old:0002", "new:0001", m.SPLIT_MERGE),
                      crosswalk_row("old:0002", "new:0002", m.SPLIT_MERGE))
    assert m.validate_crosswalk(split, frame, OLD, NEW) == []
    mixed = crosswalk(crosswalk_row("old:0002", "new:0001", m.CONFIRMED),
                      crosswalk_row("old:0002", "new:0002", m.SPLIT_MERGE))
    assert any("both a confirmed and a split_merge row" in e for e in m.validate_crosswalk(mixed, frame, OLD, NEW))


def test_duplicate_observation_ids_are_refused(tmp_path):
    path = tmp_path / "observations.csv"
    rows = [obs("old:0001", OLD), obs("old:0001", OLD)]
    pd.DataFrame(rows, dtype=str).to_csv(path, index=False)
    with pytest.raises(m.MatchError, match="not unique"):
        m.load_observations(path)


# --- crosswalk validation rules ---------------------------------------------------

@pytest.mark.parametrize("values, message", [
    ({"decision": "accepted"}, "not one of"),
    ({"decision": "Confirmed"}, "not one of"),
    ({"monetary_comparable": m.TRUE}, "monetary_comparable must be 'false'"),
    ({"monetary_comparable": ""}, "monetary_comparable must be 'false'"),
    ({"schedule_comparable": m.TRUE}, "only for a confirmed pair"),
    ({"dca_comparable": "yes"}, "must be 'true', 'false' or blank"),
    ({"match_method": "fuzzy_guess"}, "match_method"),
    ({"match_method": "exact_name+exact_name"}, "match_method"),
])
def test_invalid_values_are_reported(frame, values, message):
    errors = m.validate_crosswalk(crosswalk(crosswalk_row("old:0001", "new:0001", **values)), frame, OLD, NEW)
    assert any(message in e for e in errors), errors


def test_blank_canonical_ids_are_not_reported_as_shared(frame):
    xw = crosswalk(crosswalk_row("old:0001", "new:0001", m.CONFIRMED, canonical_project_id=""),
                   crosswalk_row("old:0002", "new:0002", m.CONFIRMED, canonical_project_id=""))
    errors = m.validate_crosswalk(xw, frame, OLD, NEW)
    assert sum("canonical_project_id required" in e for e in errors) == 2
    assert not any("confirmed pairs must be one-to-one" in e for e in errors)


# --- comparability preconditions ------------------------------------------------------

DATED = {"end_date_status": "parsed", "end_date": "2030-01-01"}


def comparability_frame(old_extra, new_extra, new_include=m.TRUE):
    rows = [obs("old:0001", OLD, "DFT_0001_2021-Q1", "A1", "a", **old_extra),
            obs("new:0001", NEW, "DFT_0001_2021-Q1", "A1", "a", **new_extra)]
    return make_frame(rows, {"old:0001": m.TRUE, "new:0001": new_include})


def comparable_row(**values):
    return crosswalk(crosswalk_row("old:0001", "new:0001", m.CONFIRMED, **values))


def test_schedule_comparable_needs_two_parsed_end_dates():
    frame = comparability_frame(DATED, {"end_date_status": "exempt"})
    errors = m.validate_crosswalk(comparable_row(schedule_comparable=m.TRUE), frame, OLD, NEW)
    assert any("end date is not a parsed date" in e for e in errors)
    frame = comparability_frame(DATED, DATED)
    assert m.validate_crosswalk(comparable_row(schedule_comparable=m.TRUE), frame, OLD, NEW) == []


@pytest.mark.parametrize("old_extra, new_extra, ok", [
    ({"external_dca": "AMBER", "external_dca_status": "parsed"},
     {"external_dca": "RED", "external_dca_status": "parsed"}, True),
    ({"sro_dca": "GREEN", "sro_dca_status": "parsed"}, {"sro_dca": "GREEN", "sro_dca_status": "parsed"}, True),
    # assessor change between fields is never a strict comparison, and SRO never stands in for external
    ({"external_dca": "AMBER", "external_dca_status": "parsed"},
     {"sro_dca": "AMBER", "sro_dca_status": "parsed"}, False),
    ({"external_dca": "AMBER", "external_dca_status": "parsed"}, {"external_dca_status": "exempt"}, False),
])
def test_dca_comparable_needs_ratings_in_the_same_field(old_extra, new_extra, ok):
    frame = comparability_frame(old_extra, new_extra)
    errors = m.validate_crosswalk(comparable_row(dca_comparable=m.TRUE), frame, OLD, NEW)
    assert (errors == []) is ok, errors


def test_comparability_needs_both_sides_in_scope():
    frame = comparability_frame(DATED, DATED, new_include=m.FALSE)
    errors = m.validate_crosswalk(comparable_row(schedule_comparable=m.TRUE), frame, OLD, NEW)
    assert any("scope include = true" in e for e in errors)
    assert m.validate_crosswalk(comparable_row(), frame, OLD, NEW) == []


@pytest.mark.parametrize("blank, message", [
    ("rationale", "rationale required"),
    ("reviewer", "reviewer required"),
    ("canonical_project_id", "canonical_project_id required"),
    ("evidence_url", "evidence_url"),
    ("schedule_comparable", "must be 'true' or 'false' for a confirmed pair"),
])
def test_confirmed_rows_need_complete_review(frame, blank, message):
    errors = m.validate_crosswalk(crosswalk(crosswalk_row("old:0001", "new:0001", m.CONFIRMED, **{blank: ""})),
                                  frame, OLD, NEW)
    assert any(message in e for e in errors), errors


def test_rejection_needs_rationale_and_reviewer(frame):
    row = crosswalk_row("old:0001", "new:0001", m.REJECTED, rationale=" ", reviewer="")
    errors = m.validate_crosswalk(crosswalk(row), frame, OLD, NEW)
    assert any("rationale required" in e for e in errors)
    assert any("reviewer required" in e for e in errors)


def test_sides_ids_and_duplicates_checked(frame):
    xw = crosswalk(crosswalk_row("new:0001", "old:0001"), crosswalk_row("old:0009", "new:0001"),
                   crosswalk_row("old:0002", "new:0002"), crosswalk_row("old:0002", "new:0002"))
    errors = m.validate_crosswalk(xw, frame, OLD, NEW)
    assert any("old_observation_id is from 2025-26" in e for e in errors)
    assert any("old_observation_id not in observations" in e for e in errors)
    assert any("pair old:0002 -> new:0002 appears more than once" in e for e in errors)


# --- unmatched observations -------------------------------------------------------

def test_unmatched_labels_are_neutral_and_reflect_decisions(frame):
    candidates = m.propose_candidates(frame, OLD, NEW)
    xw = crosswalk(crosswalk_row("old:0001", "new:0001", m.CONFIRMED,
                                 match_method="exact_project_id+normalised_name+fuzzy_description"),
                   crosswalk_row("old:0002", "new:0002", m.REJECTED, match_method="exact_name+fuzzy_description"))
    assert m.validate_crosswalk(xw, frame, OLD, NEW) == []
    review = m.build_review(candidates, xw, frame)
    unmatched = m.build_unmatched(frame, review, OLD, NEW)
    status = dict(zip(unmatched["observation_id"], unmatched["match_status"]))
    assert status == {"old:0002": m.CANDIDATES_REJECTED, "old:0003": m.NO_CANDIDATE,
                      "new:0002": m.CANDIDATES_REJECTED, "new:0003": m.NO_CANDIDATE}
    labels = " ".join(unmatched["neutral_label"]).casefold()
    for word in ("complete", "cancel", "new project", "exit", "closed"):
        assert word not in labels


def test_pending_candidates_are_not_reported_as_absent(frame):
    candidates = m.propose_candidates(frame, OLD, NEW)
    review = m.build_review(candidates, m.build_crosswalk(candidates), frame)
    unmatched = m.build_unmatched(frame, review, OLD, NEW)
    status = dict(zip(unmatched["observation_id"], unmatched["match_status"]))
    assert status["old:0001"] == m.PENDING
    assert status["old:0003"] == m.NO_CANDIDATE


def test_excluded_observations_are_not_listed_as_unmatched():
    rows = [obs("old:0001", OLD, "", "IN", "a"), obs("old:0002", OLD, "", "OUT", "b")]
    frame = make_frame(rows, {"old:0001": m.TRUE, "old:0002": m.FALSE})
    candidates = m.propose_candidates(frame, OLD, NEW)
    review = m.build_review(candidates, m.build_crosswalk(candidates), frame)
    assert list(m.build_unmatched(frame, review, OLD, NEW)["observation_id"]) == ["old:0001"]


# --- scope prerequisite and crosswalk preservation ----------------------------------

def test_unresolved_scope_review_blocks_matching(tmp_path):
    observations = pd.DataFrame([obs("old:0001", OLD, name="A")], dtype=str)
    path = tmp_path / "scope_review.csv"
    pd.DataFrame({"observation_id": ["old:0001"], "project_name": ["A"], "include": [""]}).to_csv(path, index=False)
    with pytest.raises(m.MatchError, match="complete the scope review"):
        m.load_scope(path, observations)


def test_existing_crosswalk_is_kept(tmp_path):
    rows = [obs("old:0001", OLD, "DFT_0001_2021-Q1", "A1", "a"), obs("new:0001", NEW, "DFT_0001_2021-Q1", "A1", "a")]
    pd.DataFrame(rows, dtype=str).to_csv(tmp_path / "observations.csv", index=False)
    pd.DataFrame({"observation_id": ["old:0001", "new:0001"], "project_name": ["A1", "A1"],
                  "include": [m.TRUE, m.TRUE]}).to_csv(tmp_path / "scope.csv", index=False)
    args = ["--observations", str(tmp_path / "observations.csv"), "--scope-review", str(tmp_path / "scope.csv"),
            "--crosswalk", str(tmp_path / "crosswalk.csv"), "--outputs-dir", str(tmp_path / "out")]
    assert m.main(args) == 0
    xw = m.load_crosswalk(tmp_path / "crosswalk.csv")
    assert list(xw["decision"]) == [m.NEEDS_REVIEW]
    reviewed = crosswalk(crosswalk_row("old:0001", "new:0001", m.REJECTED,
                                       match_method="exact_project_id+exact_name+fuzzy_description"))
    reviewed.to_csv(tmp_path / "crosswalk.csv", index=False, lineterminator="\n")
    before = (tmp_path / "crosswalk.csv").read_bytes()
    assert m.main(args) == 0
    assert (tmp_path / "crosswalk.csv").read_bytes() == before
    unmatched = pd.read_csv(tmp_path / "out" / "unmatched_observations.csv", dtype=str, keep_default_na=False)
    assert set(unmatched["match_status"]) == {m.CANDIDATES_REJECTED}
