"""Independent spot checks of five cases against the raw NISTA files.

Each expected value is recalculated here from data/raw with the Python standard library only (csv,
decimal, datetime, re). Nothing is read from data/processed/observations.csv, the DuckDB database or
sql/. Raw rows are located by project ID, not by the pipeline's row numbers. The recalculated value is
then compared with the value in the Power BI export (data/processed/pbi_*.csv).

Writes docs/manual_checks.csv. Reviewer: Claude, AI-assisted verification. No human or spreadsheet
review is involved.

Usage:
    python src/manual_checks.py --checked-on YYYY-MM-DD
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW = PROJECT_ROOT / "data" / "raw"
PROCESSED = PROJECT_ROOT / "data" / "processed"
OUTPUT = PROJECT_ROOT / "docs" / "manual_checks.csv"
OLD_FILE, NEW_FILE = "nista_2024_25.csv", "nista_2025_26.csv"
ID_COLUMN = {OLD_FILE: "GMPP ID", NEW_FILE: "Major Projects ID"}
BASELINE = {OLD_FILE: "Financial Year Baseline (£m)",
            NEW_FILE: "Financial Year Baseline (£m, Presented in 2024/25 Real Prices)"}
FORECAST = {OLD_FILE: "Financial Year Forecast (£m)",
            NEW_FILE: "Financial Year Forecast (£m, Presented in 2024/25 Real Prices)"}
PUBLISHED = "Financial Year Variance (%)"
END_DATE = "End Date"
IPA_DCA = "IPA Delivery Confidence Assessment"
SRO_DCA = "SRO Delivery Confidence Assessment"
COMMENTARY = "Departmental Commentary on Delivery Confidence Assessment Rating"
TOLERANCE_PP = Decimal("0.1")  # methodology section 4.3
CONCERN = {"GREEN": 1, "AMBER": 2, "RED": 3}
REVIEWER = "Claude — AI-assisted verification"
METHOD = ("Recalculated with Python stdlib (csv, decimal, datetime, re) from data/raw; raw row found by "
          "project ID; independent of sql/ and DuckDB; no human or spreadsheet review")
COLUMNS = ["check_id", "case", "project_id", "source_file", "source_row", "source_column", "raw_value",
           "expected_value", "expected_derivation", "observed_value", "observed_in", "outcome", "reviewer",
           "method", "checked_on"]


class CheckError(Exception):
    pass


def read_raw(name: str) -> tuple[list[str], list[list[str]]]:
    with open(RAW / name, encoding="utf-8-sig", newline="") as fh:
        records = list(csv.reader(fh))
    return records[0], records[1:]


def find_row(name: str, project_id: str) -> tuple[int, dict[str, str]]:
    """1-based data row number (header excluded) and the record of the only row with this project ID."""
    header, records = read_raw(name)
    position = header.index(ID_COLUMN[name])
    hits = [(i, r) for i, r in enumerate(records, start=1) if r[position] == project_id]
    if len(hits) != 1:
        raise CheckError(f"{name}: {len(hits)} rows with {ID_COLUMN[name]} {project_id!r}; expected exactly one")
    number, record = hits[0]
    return number, dict(zip(header, record))


def day_first(name: str) -> bool:
    """True when the End Date column shows day-first order: some first part > 12, no second part > 12."""
    header, records = read_raw(name)
    position = header.index(END_DATE)
    parts = [m.groups() for r in records if (m := re.fullmatch(r"(\d{2})/(\d{2})/\d{4}", r[position]))]
    return any(int(a) > 12 for a, _ in parts) and not any(int(b) > 12 for _, b in parts)


def parse_date(token: str) -> date:
    day, month, year = (int(p) for p in token.split("/"))
    return date(year, month, day)


def export_rows(stem: str) -> list[dict[str, str]]:
    with open(PROCESSED / f"{stem}.csv", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def export_row(stem: str, **key) -> dict[str, str]:
    hits = [r for r in export_rows(stem) if all(r[k] == v for k, v in key.items())]
    if len(hits) != 1:
        raise CheckError(f"{stem}: {len(hits)} rows for {key!r}; expected exactly one")
    return hits[0]


def observation_id(name: str, row: int) -> str:
    return f"{Path(name).stem}:{row:04d}"


def same(expected: str, observed: str) -> bool:
    """Exact text match, or numeric equality when both sides are numbers."""
    if expected == observed:
        return True
    try:
        return Decimal(expected) == Decimal(observed)
    except ArithmeticError:
        return False
    except ValueError:
        return False


class Recorder:
    def __init__(self, checked_on: str):
        self.rows: list[dict[str, str]] = []
        self.checked_on = checked_on

    def add(self, case, project_id, source_file, source_row, source_column, raw_value, expected, derivation,
            observed, observed_in):
        self.rows.append({
            "check_id": f"MC{len(self.rows) + 1:02d}", "case": case, "project_id": project_id,
            "source_file": f"data/raw/{source_file}" if source_file else "", "source_row": str(source_row),
            "source_column": source_column, "raw_value": raw_value, "expected_value": expected,
            "expected_derivation": derivation, "observed_value": observed, "observed_in": observed_in,
            "outcome": "match" if same(expected, observed) else "mismatch", "reviewer": REVIEWER,
            "method": METHOD, "checked_on": self.checked_on,
        })


def pct6(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


# --- cases ---------------------------------------------------------------------------------

def schedule_case(rec: Recorder, case: str, project_id: str) -> None:
    for name in (OLD_FILE, NEW_FILE):
        if not day_first(name):
            raise CheckError(f"{name}: End Date order is not demonstrably day-first")
    old_row, old = find_row(OLD_FILE, project_id)
    new_row, new = find_row(NEW_FILE, project_id)
    old_date, new_date = parse_date(old[END_DATE]), parse_date(new[END_DATE])
    days = (new_date - old_date).days
    pair = export_row("pbi_schedule_pairs", old_observation_id=observation_id(OLD_FILE, old_row),
                      new_observation_id=observation_id(NEW_FILE, new_row))
    where = f"pbi_schedule_pairs.csv [{pair['old_observation_id']} -> {pair['new_observation_id']}]"
    day_first_note = "dd/mm/yyyy (column has first parts > 12 and no second part > 12)"
    rec.add(case, project_id, OLD_FILE, old_row, END_DATE, old[END_DATE], old_date.isoformat(), day_first_note,
            pair["old_end_date"], f"{where}.old_end_date")
    rec.add(case, project_id, NEW_FILE, new_row, END_DATE, new[END_DATE], new_date.isoformat(), day_first_note,
            pair["new_end_date"], f"{where}.new_end_date")
    rec.add(case, project_id, "", "", "", "", str(days), "new end date minus old end date, in days",
            pair["end_date_movement_days"], f"{where}.end_date_movement_days")
    label = ("later published end date" if days > 0 else "earlier published end date" if days < 0
             else "unchanged published end date")
    rec.add(case, project_id, "", "", "", "", label, "sign of the day difference", pair["movement_label"],
            f"{where}.movement_label")
    rec.add(case, project_id, "", "", "", "", "true", "two parsed end dates and pair marked schedule-comparable",
            pair["schedule_eligible"], f"{where}.schedule_eligible")


def missing_baseline_case(rec: Recorder, case: str, project_id: str) -> None:
    row, record = find_row(NEW_FILE, project_id)
    oid = observation_id(NEW_FILE, row)
    baseline, forecast, published = record[BASELINE[NEW_FILE]], record[FORECAST[NEW_FILE]], record[PUBLISHED]
    if baseline.strip() != "":
        raise CheckError(f"{oid}: baseline is not blank in the raw file ({baseline!r})")
    variance = export_row("pbi_annual_variance", observation_id=oid)
    reconciliation = export_row("pbi_variance_reconciliation", observation_id=oid)
    rec.add(case, project_id, NEW_FILE, row, BASELINE[NEW_FILE], baseline, "baseline_blank",
            "blank raw baseline: no variance; blank is not zero", variance["variance_status"],
            f"pbi_annual_variance.csv [{oid}].variance_status")
    rec.add(case, project_id, NEW_FILE, row, FORECAST[NEW_FILE], forecast, forecast,
            "forecast kept as published although no variance is calculated", variance["forecast_gbp_m"],
            f"pbi_annual_variance.csv [{oid}].forecast_gbp_m")
    rec.add(case, project_id, "", "", "", "", "", "no variance amount without a baseline (empty)",
            variance["variance_gbp_m"], f"pbi_annual_variance.csv [{oid}].variance_gbp_m")
    rec.add(case, project_id, "", "", "", "", "", "no variance percentage without a baseline (empty)",
            variance["variance_pct"], f"pbi_annual_variance.csv [{oid}].variance_pct")
    rec.add(case, project_id, NEW_FILE, row, PUBLISHED, published, "not_compared_calculated_unavailable",
            "nothing to reconcile when the calculated value is unavailable",
            reconciliation["reconciliation_status"], f"pbi_variance_reconciliation.csv [{oid}].reconciliation_status")


def positive_variance_case(rec: Recorder, case: str, project_id: str) -> None:
    row, record = find_row(NEW_FILE, project_id)
    oid = observation_id(NEW_FILE, row)
    baseline, forecast = Decimal(record[BASELINE[NEW_FILE]]), Decimal(record[FORECAST[NEW_FILE]])
    published = Decimal(record[PUBLISHED])
    amount = forecast - baseline
    if amount <= 0:
        raise CheckError(f"{oid}: forecast is not above baseline")
    pct = amount / baseline * 100
    difference = abs(pct) - published
    status = "within_tolerance" if abs(difference) <= TOLERANCE_PP else "outside_tolerance_investigate"
    variance = export_row("pbi_annual_variance", observation_id=oid)
    reconciliation = export_row("pbi_variance_reconciliation", observation_id=oid)
    rec.add(case, project_id, NEW_FILE, row, f"{FORECAST[NEW_FILE]} minus {BASELINE[NEW_FILE]}",
            f"{record[FORECAST[NEW_FILE]]} - {record[BASELINE[NEW_FILE]]}", str(amount),
            "forecast minus baseline, same row, same price-basis label (2024/25 Real Prices)",
            variance["variance_gbp_m"], f"pbi_annual_variance.csv [{oid}].variance_gbp_m")
    rec.add(case, project_id, "", "", "", "", pct6(pct), "(forecast - baseline) / baseline x 100, signed, 6 dp",
            variance["variance_pct"], f"pbi_annual_variance.csv [{oid}].variance_pct")
    rec.add(case, project_id, "", "", "", "", "forecast_above_baseline", "sign of the amount",
            variance["variance_direction"], f"pbi_annual_variance.csv [{oid}].variance_direction")
    rec.add(case, project_id, NEW_FILE, row, PUBLISHED, record[PUBLISHED], str(published), "published value as is",
            reconciliation["published_pct"], f"pbi_variance_reconciliation.csv [{oid}].published_pct")
    rec.add(case, project_id, "", "", "", "", pct6(difference), "|calculated| - published, percentage points",
            reconciliation["difference_pp"], f"pbi_variance_reconciliation.csv [{oid}].difference_pp")
    rec.add(case, project_id, "", "", "", "", status, f"|difference| <= {TOLERANCE_PP} pp",
            reconciliation["reconciliation_status"], f"pbi_variance_reconciliation.csv [{oid}].reconciliation_status")


# Attribution sentences as worded in each release; the captured word is the rating the commentary states.
ATTRIBUTION = {
    OLD_FILE: ("IPA", r"Infrastructure Project Authority's Delivery Confidence Assessment rating at 24/25-Q4 "
                      r"remained at (\w+)"),
    NEW_FILE: ("NISTA", r"National Infrastructure & Service Transformation Authority's Delivery Confidence "
                        r"Assessment rating at 25/26-Q4 is at (\w+)"),
}
CONTRARY_ATTRIBUTION = r"NISTA\s*/\s*SRO|SRO Delivery Confidence|SRO's Delivery Confidence"


def dca_case(rec: Recorder, case: str, project_id: str) -> None:
    old_row, old = find_row(OLD_FILE, project_id)
    new_row, new = find_row(NEW_FILE, project_id)
    oid_old, oid_new = observation_id(OLD_FILE, old_row), observation_id(NEW_FILE, new_row)
    pair = export_row("pbi_dca_pairs", old_observation_id=oid_old, new_observation_id=oid_new)
    where = f"pbi_dca_pairs.csv [{oid_old} -> {oid_new}]"
    ratings = {}
    for name, row, record, side in ((OLD_FILE, old_row, old, "old"), (NEW_FILE, new_row, new, "new")):
        if record[SRO_DCA].strip():
            raise CheckError(f"{name} row {row}: SRO column is not blank")
        assessor, pattern = ATTRIBUTION[name]
        text = record[COMMENTARY]
        match = re.search(pattern, text)
        if not match or re.search(CONTRARY_ATTRIBUTION, text):
            raise CheckError(f"{name} row {row}: commentary does not attribute the rating to the {assessor} alone")
        if match.group(1).upper() != record[IPA_DCA]:
            raise CheckError(f"{name} row {row}: commentary rating {match.group(1)} differs from the cell")
        ratings[side] = record[IPA_DCA]
        rec.add(case, project_id, name, row, IPA_DCA, record[IPA_DCA], record[IPA_DCA],
                "rating cell; SRO cell blank", pair[f"{side}_rating_compared"], f"{where}.{side}_rating_compared")
        rec.add(case, project_id, name, row, COMMENTARY, match.group(0), assessor,
                f"commentary credits the {assessor} (independent assessor) with this rating; column label alone "
                f"not relied on", pair[f"{side}_commentary_attribution"], f"{where}.{side}_commentary_attribution")
    transition = ("Improved" if CONCERN[ratings["new"]] < CONCERN[ratings["old"]]
                  else "Unchanged" if CONCERN[ratings["new"]] == CONCERN[ratings["old"]] else "Deteriorated")
    rec.add(case, project_id, "", "", "", "", "true",
            "both ratings in the external field, each credited to an independent assessor (IPA, then NISTA)",
            pair["origin_verified_both"], f"{where}.origin_verified_both")
    rec.add(case, project_id, "", "", "", "", transition,
            f"{ratings['old']} -> {ratings['new']} on GREEN < AMBER < RED (order of concern)",
            pair["dca_transition"], f"{where}.dca_transition")


CASES = (
    ("ECML end-date movement", schedule_case, "DFT_0033_1819-Q1"),
    ("Unchanged schedule pair (Midlands Rail Hub)", schedule_case, "DFT_0043_2021-Q2"),
    ("ECML 2025-26 missing baseline", missing_baseline_case, "DFT_0033_1819-Q1"),
    ("Positive annual forecast variance (EWR Phase 1, 2025-26)", positive_variance_case, "DFT_0046_2021-Q2"),
    ("ECDP DCA transition and assessment origin", dca_case, "DFT_0038_1920-Q4"),
)


def run(checked_on: str) -> list[dict[str, str]]:
    rec = Recorder(checked_on)
    for case, check, project_id in CASES:
        check(rec, case, project_id)
    return rec.rows


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checked-on", required=True, type=date.fromisoformat)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    try:
        rows = run(args.checked_on.isoformat())
    except CheckError as exc:
        print(f"manual checks failed: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    mismatches = [r for r in rows if r["outcome"] != "match"]
    print(f"wrote {args.output.relative_to(PROJECT_ROOT).as_posix()}: {len(rows)} checks, "
          f"{len(rows) - len(mismatches)} match, {len(mismatches)} mismatch")
    for r in mismatches:
        print(f"  MISMATCH {r['check_id']} {r['case']}: expected {r['expected_value']!r}, "
              f"observed {r['observed_value']!r} in {r['observed_in']}", file=sys.stderr)
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
