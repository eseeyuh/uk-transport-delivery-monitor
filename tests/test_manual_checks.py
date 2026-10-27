"""The independent spot checks in src/manual_checks.py agree with the current Power BI exports."""

import pytest

import manual_checks as mc


def test_independent_checks_match_the_exports():
    if not (mc.PROCESSED / "pbi_dca_pairs.csv").exists():
        pytest.skip("project exports not built")
    rows = mc.run("2026-10-24")
    assert len({r["case"] for r in rows}) == 5
    assert [r["check_id"] for r in rows if r["outcome"] != "match"] == []
    assert {r["reviewer"] for r in rows} == {"Claude — AI-assisted verification"}


def test_blank_is_not_zero():
    assert mc.same("", "") and not mc.same("", "0") and mc.same("2.750000", "2.75")
