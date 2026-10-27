"""Tests for src/prepare.py parsing rules. Inputs are hand-built tokens and frames, not release data."""

import pandas as pd
import pytest

import prepare as p


def frame(**columns):
    return pd.DataFrame(columns, dtype=str)


# --- zero vs missing ---------------------------------------------------------

@pytest.mark.parametrize("raw", ["0", "0.0", "0.00"])
def test_zero_is_a_parsed_value(raw):
    result = p.parse_amount(raw)
    assert result.status == p.PARSED
    assert result.value == 0.0


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_blank_is_missing_not_zero(raw):
    result = p.parse_amount(raw)
    assert result.status == p.BLANK
    assert result.value is None


def test_zero_and_blank_stay_distinct_in_observations():
    raw = frame(ID=["DFT_0001_2425-Q1", "DFT_0002_2425-Q1"], WLC=["0", ""])
    specs = [p.FieldSpec("project_id", "ID", "none_in_label", "not_stated_in_label"),
             p.FieldSpec("whole_life_cost_gbp_m", "WLC", "£m", "not_stated_in_label")]
    obs, status, _ = p.prepare_source(raw, "x.csv", "2024-25", specs, ["project_id", "whole_life_cost_gbp_m"])
    assert obs["whole_life_cost_gbp_m"].iloc[0] == 0.0
    assert pd.isna(obs["whole_life_cost_gbp_m"].iloc[1])
    assert obs["whole_life_cost_gbp_m_status"].tolist() == [p.PARSED, p.BLANK]
    assert obs["whole_life_cost_gbp_m_raw"].tolist() == ["0", ""]


# --- exempt and range amounts --------------------------------------------------

@pytest.mark.parametrize("raw, sections", [
    ("Section 43 - Commercial interests", ("43",)),
    ("Exempt under Section 43 – Commercial interests​", ("43",)),
    ("Exempt under Section 22 – Information intended for future publication ​", ("22",)),
    ("Section 24 - National security, Section 26 - Defence", ("24", "26")),
    ("Section 23 - Information supplied by, or relating to, bodies dealing with security matters", ("23",)),
    ("Exempt under Section 35 - Formulation of government policy, Section 43 - Commercial interests", ("35", "43")),
])
def test_exemption_tokens_are_exempt_not_values(raw, sections):
    result = p.parse_amount(raw)
    assert result.status == p.EXEMPT
    assert result.value is None
    assert result.reason.startswith("FOI exemption cited: " + ", ".join(f"s{n}" for n in sections))


def test_planning_placeholder_with_exemption_keeps_both_reasons():
    raw = ("The GMPP project is still at the planning stage (Pre-SOC or equivalent) and is not yet in a "
           "position to provide costs/schedule information, Section 43 – Commercial interests​")
    result = p.parse_amount(raw)
    assert result.status == p.EXEMPT
    assert "s43" in result.reason and "planning-stage" in result.reason


def test_planning_placeholder_alone():
    raw = ("The GMPP project is still at the planning stage (Pre-SOC or equivalent) and is not yet in a "
           "position to provide cost/schedule information")
    assert p.parse_amount(raw).status == p.PLANNING


@pytest.mark.parametrize("raw", [
    "Section 43 - National security",        # title does not belong to the section number
    "Section 99 - Commercial interests",     # unknown section
    "Section 43 - Commercial interests and more text",
])
def test_lookalike_exemptions_are_unrecognised(raw):
    assert p.parse_amount(raw).status == p.UNRECOGNISED


@pytest.mark.parametrize("raw", [
    "Low: 1,876.30, Mid: 1,876.30, High: 1,876.30",
    "Mid: 20,300.00, High: 53,300.00",
    "Low: 797.93, Mid: 820.57",
    "Low: 0.00, Mid: 0.00, High: 0.00",
])
def test_ranges_have_no_scalar_value(raw):
    result = p.parse_amount(raw)
    assert result.status == p.RANGE
    assert result.value is None
    assert "no scalar value" in result.reason


@pytest.mark.parametrize("raw", ["High: 1.0, Low: 0.5", "Low: 1.0, Low: 2.0", "Low: 1.0; Mid: 2.0"])
def test_malformed_ranges_are_unrecognised(raw):
    assert p.parse_amount(raw).status == p.UNRECOGNISED


def test_grouped_thousands_parse_only_in_exact_format():
    assert p.parse_amount("1,007.93").value == 1007.93
    assert p.parse_amount("1,007.93").reason == "thousands separators removed"
    for raw in ("1,00.5", "1007,93", "£1,007.93", "1,007.93m", "approx 5"):
        assert p.parse_amount(raw).status == p.UNRECOGNISED, raw


def test_negative_amount_parses():
    assert p.parse_amount("-12.5").value == -12.5


# --- date ambiguity ------------------------------------------------------------

def test_day_month_order_evidence_states():
    assert p.day_month_order(["13/01/2020", "01/04/2021"]).state == p.ORDER_VERIFIED
    assert p.day_month_order(["01/04/2021", "05/06/2022"]).state == p.ORDER_NO_EVIDENCE
    assert p.day_month_order(["01/13/2021"]).state == p.ORDER_MONTH_FIRST
    assert p.day_month_order(["13/01/2020", "01/13/2021"]).state == p.ORDER_CONFLICT


def test_date_parses_only_when_day_first_verified():
    assert p.parse_date("01/04/2022", p.ORDER_VERIFIED) == p.FieldResult(p.PARSED, "2022-04-01", "")
    unverified = p.parse_date("01/04/2022", p.ORDER_NO_EVIDENCE)
    assert unverified.status == p.AMBIGUOUS and unverified.value is None
    assert p.parse_date("01/04/2022", p.ORDER_CONFLICT).status == p.AMBIGUOUS


def test_same_day_and_month_is_unambiguous_without_evidence():
    assert p.parse_date("05/05/2022", p.ORDER_NO_EVIDENCE).value == "2022-05-05"


@pytest.mark.parametrize("raw", ["03/2027", "2027", "2027-03", "Q1 2027", "March 2027", "2026/27"])
def test_partial_dates_are_flagged_not_completed(raw):
    result = p.parse_date(raw, p.ORDER_VERIFIED)
    assert result.status == p.PARTIAL
    assert result.value is None


@pytest.mark.parametrize("raw, status", [
    ("31/02/2025", p.INVALID),
    ("2025-03-31", p.UNVERIFIED),
    ("1/4/2022", p.UNVERIFIED),
    ("Low: 31/03/2025, Mid: 30/04/2025, High: 31/05/2025", p.RANGE),
    ("No", p.UNRECOGNISED),
])
def test_other_date_tokens(raw, status):
    result = p.parse_date(raw, p.ORDER_VERIFIED)
    assert result.status == status
    assert result.value is None


def test_date_column_without_evidence_is_flagged_in_pipeline():
    raw = frame(ID=["DFT_0001_2425-Q1", "DFT_0002_2425-Q1"], S=["01/04/2022", "05/06/2023"])
    specs = [p.FieldSpec("project_id", "ID", "none_in_label", "not_stated_in_label"),
             p.FieldSpec("start_date", "S", "none_in_label", "not_stated_in_label")]
    obs, _, evidence = p.prepare_source(raw, "x.csv", "2024-25", specs, ["project_id", "start_date"])
    assert evidence[0]["state"] == p.ORDER_NO_EVIDENCE
    assert obs["start_date_status"].tolist() == [p.AMBIGUOUS, p.AMBIGUOUS]
    assert obs["start_date"].isna().all()


# --- cancellation / exemption tokens and DCA -------------------------------------

@pytest.mark.parametrize("raw", ["Cancelled", "Project closed", "Paused", "N/A", "TBC"])
def test_status_words_are_never_read_as_values_or_blanks(raw):
    for result in (p.parse_amount(raw), p.parse_dca(raw), p.parse_date(raw, p.ORDER_VERIFIED)):
        assert result.status == p.UNRECOGNISED
        assert result.value is None


@pytest.mark.parametrize("raw, expected, note", [
    ("AMBER", "AMBER", ""),
    ("Amber", "AMBER", "case/whitespace normalised"),
    (" green ", "GREEN", "case/whitespace normalised"),
])
def test_dca_case_normalised(raw, expected, note):
    assert p.parse_dca(raw) == p.FieldResult(p.PARSED, expected, note)


@pytest.mark.parametrize("raw", ["Amber/Red", "AMBER-GREEN", "Ambr"])
def test_dca_unknown_spellings_not_guessed(raw):
    assert p.parse_dca(raw).status == p.UNRECOGNISED


def test_dca_exemption_is_exempt():
    assert p.parse_dca("Exempt under Section 24 - National security").status == p.EXEMPT


def test_external_dca_never_filled_from_sro():
    raw = frame(ID=["DFT_0001_2425-Q1", "DFT_0002_2425-Q1"], IPA=["", "red"], SRO=["GREEN", "AMBER"])
    specs = [p.FieldSpec("project_id", "ID", "none_in_label", "not_stated_in_label"),
             p.FieldSpec("ipa_delivery_confidence", "IPA", "none_in_label", "not_stated_in_label"),
             p.FieldSpec("sro_delivery_confidence", "SRO", "none_in_label", "not_stated_in_label")]
    fields = ["project_id", "ipa_delivery_confidence", "sro_delivery_confidence"]
    obs, _, _ = p.prepare_source(raw, "x.csv", "2024-25", specs, fields)
    assert pd.isna(obs["external_dca"].iloc[0])
    assert obs["external_dca"].iloc[1] == "RED"
    assert obs["external_dca_status"].tolist() == [p.BLANK, p.PARSED]
    assert obs["external_dca_raw"].tolist() == ["", "red"]
    assert obs["external_dca_source_column"].tolist() == ["IPA", "IPA"]
    assert obs["sro_dca"].tolist() == ["GREEN", "AMBER"]


def test_field_missing_from_release_is_not_collected():
    raw = frame(ID=["DFT_0001_2425-Q1"])
    specs = [p.FieldSpec("project_id", "ID", "none_in_label", "not_stated_in_label")]
    obs, status, _ = p.prepare_source(raw, "x.csv", "2024-25", specs, ["project_id", "evaluation_plan_registry_link"])
    assert obs["evaluation_plan_registry_link_status"].tolist() == [p.NOT_COLLECTED]
    assert set(status["canonical_field"]) == {"project_id"}


# --- duplicate keys ------------------------------------------------------------

def test_duplicate_and_missing_keys_flagged():
    flags = p.flag_keys(pd.Series(["A", "A ", "B", "", "  "], dtype=str))
    assert flags["duplicate"].tolist() == [True, True, False, False, False]
    assert flags["missing"].tolist() == [False, False, False, True, True]


def test_duplicate_keys_flagged_within_release_and_rows_kept():
    raw = frame(ID=["DFT_0001_2425-Q1", "DFT_0001_2425-Q1", "DFT_0002_2425-Q1"])
    specs = [p.FieldSpec("project_id", "ID", "none_in_label", "not_stated_in_label")]
    obs, _, _ = p.prepare_source(raw, "x.csv", "2024-25", specs, ["project_id"])
    assert len(obs) == 3
    assert obs["project_id_duplicate_in_release"].tolist() == [True, True, False]
    assert obs["source_row"].tolist() == [1, 2, 3]


# --- mapping approval and DfT selection ---------------------------------------------

def test_unapproved_mapping_is_rejected(tmp_path):
    path = tmp_path / "mapping.csv"
    pd.DataFrame([{"canonical_field": "project_id", "source_file": "x.csv", "source_column_index": "0",
                   "source_column": "ID", "unit_in_label": "none_in_label",
                   "price_basis_in_label": "not_stated_in_label", "review_status": "pending_review"}]).to_csv(
        path, index=False)
    with pytest.raises(p.PrepareError, match="not approved"):
        p.load_mapping(path, {"x.csv": ["ID"]})


def test_dft_selection_rejects_unconfirmed_label_variant():
    obs = pd.DataFrame({"source_file": ["nista_2024_25.csv"] * 2, "release_year": ["2024-25"] * 2,
                        "source_row": [1, 2], "department_raw": ["DfT", "DFT"]})
    with pytest.raises(p.PrepareError, match="variants"):
        p.select_dft(obs)


def test_scope_proposal_does_not_assume_dft_is_infrastructure():
    assert p.propose_scope("Infrastructure and Construction", "A66")[0] == "yes"
    assert p.propose_scope("Infrastructure and Construction", "EAST COAST DIGITAL PROGRAMME")[0] == "review"
    assert p.propose_scope("Government Transformation and Service Delivery", "Rail Reform")[0] == "no"
    assert p.propose_scope("Some new category", "Anything")[0] == "review"
