"""Prepare typed observations from the raw NISTA releases using the approved column mapping.

Inputs (read only):
    data/raw/*.csv                    raw releases; SHA256 must match data/source_manifest.json
    data/manual/column_mapping.csv    every row must have review_status == "approved"
    data/manual/source_metadata.csv   release_year must be supplied for every file

Outputs:
    data/processed/observations.csv   one row per source record; raw tokens kept beside parsed values
    data/processed/field_status.csv   one row per mapped source cell with status and reason
    outputs/flagged_records.csv       cells unavailable for scalar use or needing review, plus key issues
    outputs/field_status_counts.csv   counts by release, field and status
    outputs/prepare_summary.md        record counts, date-order evidence, DfT selection
    data/manual/scope_review.csv      every DfT row for manual scope classification

Nothing is imputed or dropped. Ranges, FOI exemptions, planning-stage placeholders and
unrecognised tokens stay unavailable for scalar arithmetic with an explicit status and reason.

Usage:
    python src/prepare.py [--force-scope]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from inventory import (
    DMY_DATE_RE,
    GROUPED_NUMERIC_RE,
    NUMERIC_RE,
    PROJECT_ID_RE,
    PROJECT_ROOT,
    InventoryError,
    code,
    load_user_metadata,
    read_source,
    relpath,
    table,
    write_text,
)

DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw"
DEFAULT_MAPPING = PROJECT_ROOT / "data" / "manual" / "column_mapping.csv"
DEFAULT_METADATA = PROJECT_ROOT / "data" / "manual" / "source_metadata.csv"
DEFAULT_MANIFEST = PROJECT_ROOT / "data" / "source_manifest.json"
DEFAULT_PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
DEFAULT_OUTPUTS_DIR = PROJECT_ROOT / "outputs"
DEFAULT_SCOPE_REVIEW = PROJECT_ROOT / "data" / "manual" / "scope_review.csv"

# Field statuses.
PARSED = "parsed"
PRESENT = "present"
BLANK = "blank"
EXEMPT = "exempt"
PLANNING = "planning_stage"
RANGE = "range"
AMBIGUOUS = "ambiguous_day_month"
PARTIAL = "partial_date"
INVALID = "invalid_date"
UNVERIFIED = "unverified_date_format"
UNRECOGNISED = "unrecognised"
NOT_COLLECTED = "not_collected_in_release"
UNAVAILABLE_BY_SOURCE = (EXEMPT, PLANNING, RANGE)
NEEDS_REVIEW = (AMBIGUOUS, PARTIAL, INVALID, UNVERIFIED, UNRECOGNISED)

# Field kinds by canonical name; any other mapped field is free text.
AMOUNT_UNITS = {
    "fy_baseline_gbp_m": "£m",
    "fy_forecast_gbp_m": "£m",
    "whole_life_cost_gbp_m": "£m",
    "benefits_gbp_m": "£m",
    "fy_variance_pct": "%",
}
DATE_FIELDS = ("start_date", "end_date")
# External (IPA, per the source column label) and SRO assessments stay separate outputs.
DCA_OUTPUT_NAMES = {"ipa_delivery_confidence": "external_dca", "sro_delivery_confidence": "sro_dca"}
ID_FIELD = "project_id"
REQUIRED_FIELDS = (
    ID_FIELD, "project_name", "department", "annual_report_category", "project_description",
    *AMOUNT_UNITS, *DATE_FIELDS, *DCA_OUTPUT_NAMES,
)
MAPPING_COLUMNS = ("canonical_field", "source_file", "source_column_index", "source_column",
                   "unit_in_label", "price_basis_in_label", "review_status")

# The rating scale observed in both releases (outputs/source_inventory.md).
DCA_RATINGS = ("RED", "AMBER", "GREEN")

# FOI Act section titles exactly as cited in the releases (outputs/source_inventory.md section 9).
FOI_SECTION_TITLES = {
    "22": "Information intended for future publication",
    "23": "Information supplied by, or relating to, bodies dealing with security matters",
    "24": "National security",
    "26": "Defence",
    "27": "International relations",
    "35": "Formulation of government policy",
    "43": "Commercial interests",
}
PLANNING_PATTERN = (
    r"The GMPP project is still at the planning stage \(Pre-SOC or equivalent\) "
    r"and is not yet in a position to provide costs?/schedule information"
)
_TITLE_ALTERNATIVES = "|".join(
    re.escape(title) for title in sorted(FOI_SECTION_TITLES.values(), key=len, reverse=True)
)
WITHHELD_PIECE_RE = re.compile(
    rf"(?:Exempt under )?Section (?P<num>\d+) - (?P<title>{_TITLE_ALTERNATIVES})|(?P<planning>{PLANNING_PATTERN})",
    re.IGNORECASE,
)
# Characters folded only when classifying withheld text; raw tokens are never changed.
CLASSIFICATION_FOLDS = {"​": "", " ": " ", " ": " ", "\t": " ", "–": "-"}

RANGE_LABELS = ("Low", "Mid", "High")
AMOUNT_COMPONENT = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
DATE_COMPONENT = r"\d{2}/\d{2}/\d{4}"
PARTIAL_DATE_RES = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\d{1,2}/\d{4}",                    # mm/yyyy
    r"\d{4}",                            # yyyy
    r"\d{4}-\d{2}",                      # yyyy-mm
    r"(?:Q[1-4]|H[12])[ /-]?\d{2,4}",    # quarter or half year
    r"[A-Za-z]{3,9}[ -]\d{2,4}",         # month name and year
    r"(?:FY ?)?\d{2,4}/\d{2}",           # financial year
))
OTHER_DATE_FORMAT_RES = tuple(re.compile(p) for p in (
    r"\d{1,2}/\d{1,2}/\d{2,4}",
    r"\d{4}-\d{2}-\d{2}",
    r"\d{1,2}[.-]\d{1,2}[.-]\d{2,4}",
))
ORDER_VERIFIED = "day_first_verified"
ORDER_CONFLICT = "conflicting_order_evidence"
ORDER_MONTH_FIRST = "month_first_evidence"
ORDER_NO_EVIDENCE = "no_day_first_evidence"

# Exact Department labels for DfT per file, confirmed in outputs/source_inventory.md section 5.
DFT_LABELS = {"nista_2024_25.csv": "DfT", "nista_2025_26.csv": "DFT"}
INFRASTRUCTURE_CATEGORY = "Infrastructure and Construction"
NON_INFRASTRUCTURE_CATEGORIES = (
    "Government Transformation and Service Delivery",
    "ICT",
    "Information and Communications Technology (ICT)",
    "Military Capability",
)
# Name terms that send an Infrastructure and Construction row to manual review.
SCOPE_REVIEW_TERMS = ("digital", "fund", "transformation", "reform", "ownership", "technology", "systems")
SCOPE_COLUMNS = (
    "observation_id", "release_year", "project_name", "source_category", "include", "reason", "reviewer",
    "proposed_include", "proposed_basis", "source_file", "source_row", "project_id_raw", "department_raw",
    "project_description_raw",
)


class PrepareError(Exception):
    """Raised when an input fails a precondition for preparation."""


@dataclass(frozen=True)
class FieldResult:
    status: str
    value: float | str | None = None
    reason: str = ""


@dataclass(frozen=True)
class Withheld:
    sections: tuple[str, ...]
    planning: bool


@dataclass(frozen=True)
class OrderEvidence:
    first_part_over_12: int
    second_part_over_12: int
    state: str


@dataclass(frozen=True)
class FieldSpec:
    canonical_field: str
    source_column: str
    unit_in_label: str
    price_basis_in_label: str


def field_kind(canonical_field: str) -> str:
    if canonical_field in AMOUNT_UNITS:
        return "amount"
    if canonical_field in DATE_FIELDS:
        return "date"
    if canonical_field in DCA_OUTPUT_NAMES:
        return "dca"
    if canonical_field == ID_FIELD:
        return "identifier"
    return "text"


def output_name(canonical_field: str) -> str:
    return DCA_OUTPUT_NAMES.get(canonical_field, canonical_field)


def join_notes(*notes: str) -> str:
    return "; ".join(note for note in notes if note)


# --- token classification ---------------------------------------------------

def classify_withheld(raw: str) -> Withheld | None:
    """Return FOI sections / planning flag if the whole token is withheld text, else None."""
    text = raw
    for char, replacement in CLASSIFICATION_FOLDS.items():
        text = text.replace(char, replacement)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    sections: list[str] = []
    planning = False
    pos = 0
    while pos < len(text):
        match = WITHHELD_PIECE_RE.match(text, pos)
        if not match:
            return None
        if match.group("planning"):
            planning = True
        else:
            number = match.group("num")
            if FOI_SECTION_TITLES.get(number, "").casefold() != match.group("title").casefold():
                return None
            sections.append(number)
        pos = match.end()
        if pos < len(text):
            if not text.startswith(", ", pos):
                return None
            pos += 2
    return Withheld(tuple(sections), planning)


def missing_or_withheld(raw: str) -> FieldResult | None:
    if not raw.strip():
        return FieldResult(BLANK, None, "empty or whitespace-only cell in source")
    withheld = classify_withheld(raw)
    if withheld is None:
        return None
    if withheld.sections:
        reason = "FOI exemption cited: " + ", ".join(f"s{n}" for n in withheld.sections)
        if withheld.planning:
            reason += "; planning-stage placeholder also present"
        return FieldResult(EXEMPT, None, reason)
    return FieldResult(PLANNING, None, "planning-stage placeholder: information not yet available")


def parse_range(text: str, component: str) -> tuple[tuple[str, str], ...] | None:
    """Return Low/Mid/High parts if the whole token is a labelled range, else None."""
    parts = tuple(re.findall(rf"(Low|Mid|High): ({component})", text))
    if not parts or ", ".join(f"{label}: {value}" for label, value in parts) != text:
        return None
    labels = [label for label, _ in parts]
    if labels != sorted(set(labels), key=RANGE_LABELS.index):
        return None
    return parts


def range_result(parts: tuple[tuple[str, str], ...]) -> FieldResult:
    listed = ", ".join(f"{label}={value}" for label, value in parts)
    return FieldResult(RANGE, None, f"Low/Mid/High range ({listed}); no scalar value")


def parse_amount(raw: str, unit: str = "£m") -> FieldResult:
    early = missing_or_withheld(raw)
    if early:
        return early
    text = raw.strip()
    note = "surrounding whitespace removed" if text != raw else ""
    if NUMERIC_RE.fullmatch(text):
        return FieldResult(PARSED, float(text), note)
    if GROUPED_NUMERIC_RE.fullmatch(text):
        return FieldResult(PARSED, float(text.replace(",", "")), join_notes(note, "thousands separators removed"))
    parts = parse_range(text, AMOUNT_COMPONENT)
    if parts:
        return range_result(parts)
    return FieldResult(UNRECOGNISED, None,
                       f"not a plain {unit} number, Low/Mid/High range, FOI exemption or planning placeholder")


def day_month_order(values) -> OrderEvidence:
    """Evidence for dd/mm vs mm/dd from exact dd/mm/yyyy tokens in one source column."""
    first = second = 0
    for value in values:
        match = DMY_DATE_RE.fullmatch(value.strip())
        if match:
            first += int(match.group(1)) > 12
            second += int(match.group(2)) > 12
    if first and not second:
        state = ORDER_VERIFIED
    elif first and second:
        state = ORDER_CONFLICT
    elif second:
        state = ORDER_MONTH_FIRST
    else:
        state = ORDER_NO_EVIDENCE
    return OrderEvidence(first, second, state)


def parse_date(raw: str, order_state: str) -> FieldResult:
    early = missing_or_withheld(raw)
    if early:
        return early
    text = raw.strip()
    note = "surrounding whitespace removed" if text != raw else ""
    match = DMY_DATE_RE.fullmatch(text)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        if order_state != ORDER_VERIFIED and day != month:
            return FieldResult(AMBIGUOUS, None, f"day/month order not verified for this column ({order_state})")
        try:
            parsed = datetime.strptime(text, "%d/%m/%Y").date()
        except ValueError:
            return FieldResult(INVALID, None, "not a valid calendar date as dd/mm/yyyy")
        return FieldResult(PARSED, parsed.isoformat(), note)
    parts = parse_range(text, DATE_COMPONENT)
    if parts:
        return range_result(parts)
    if any(pattern.fullmatch(text) for pattern in PARTIAL_DATE_RES):
        return FieldResult(PARTIAL, None, "date lacks day and/or month; not completed")
    if any(pattern.fullmatch(text) for pattern in OTHER_DATE_FORMAT_RES):
        return FieldResult(UNVERIFIED, None, "date-like token in a format not verified for this source")
    return FieldResult(UNRECOGNISED, None, "not a date, Low/Mid/High range, FOI exemption or planning placeholder")


def parse_dca(raw: str) -> FieldResult:
    early = missing_or_withheld(raw)
    if early:
        return early
    rating = raw.strip().upper()
    if rating in DCA_RATINGS:
        return FieldResult(PARSED, rating, "" if rating == raw else "case/whitespace normalised")
    return FieldResult(UNRECOGNISED, None,
                       f"not one of {'/'.join(DCA_RATINGS)}, an FOI exemption or planning placeholder")


def parse_identifier(raw: str) -> FieldResult:
    early = missing_or_withheld(raw)
    if early:
        return early
    text = raw.strip()
    if PROJECT_ID_RE.fullmatch(text):
        return FieldResult(PRESENT, text, "surrounding whitespace removed" if text != raw else "")
    return FieldResult(UNRECOGNISED, None, f"does not match identifier pattern {PROJECT_ID_RE.pattern}")


def parse_text(raw: str) -> FieldResult:
    return missing_or_withheld(raw) or FieldResult(PRESENT)


def parse_cell(spec: FieldSpec, raw: str, order_state: str | None) -> FieldResult:
    kind = field_kind(spec.canonical_field)
    if kind == "amount":
        return parse_amount(raw, AMOUNT_UNITS[spec.canonical_field])
    if kind == "date":
        return parse_date(raw, order_state)
    if kind == "dca":
        return parse_dca(raw)
    if kind == "identifier":
        return parse_identifier(raw)
    return parse_text(raw)


def flag_keys(keys: pd.Series) -> pd.DataFrame:
    """Flag blank keys and non-blank keys that occur more than once (exact match after strip)."""
    stripped = keys.str.strip()
    missing = stripped == ""
    counts = stripped[~missing].value_counts()
    duplicate = ~missing & stripped.map(counts).fillna(0).gt(1)
    return pd.DataFrame({"missing": missing, "duplicate": duplicate}, index=keys.index)


# --- inputs -----------------------------------------------------------------

def load_mapping(path: Path, headers: dict[str, list[str]]) -> tuple[dict[str, list[FieldSpec]], list[str]]:
    """Validate the approved mapping against the raw headers; return specs per file and field order."""
    mapping = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False, encoding="utf-8-sig")
    missing_columns = [c for c in MAPPING_COLUMNS if c not in mapping.columns]
    if missing_columns:
        raise PrepareError(f"{relpath(path)}: missing columns {missing_columns!r}")
    not_approved = mapping[mapping["review_status"] != "approved"]
    if not not_approved.empty:
        pairs = list(zip(not_approved["canonical_field"], not_approved["source_file"]))
        raise PrepareError(f"{relpath(path)}: {len(pairs)} rows not approved, e.g. {pairs[:5]!r}")
    if (mapping["canonical_field"].str.strip() == "").any():
        raise PrepareError(f"{relpath(path)}: rows with blank canonical_field")
    unknown_files = sorted(set(mapping["source_file"]) - set(headers))
    if unknown_files:
        raise PrepareError(f"{relpath(path)}: rows for files not in data/raw {unknown_files!r}")

    specs: dict[str, list[FieldSpec]] = {}
    for file_name, columns in headers.items():
        rows = mapping[mapping["source_file"] == file_name]
        for row in rows.itertuples(index=False):
            index = int(row.source_column_index)
            if index >= len(columns) or columns[index] != row.source_column:
                raise PrepareError(f"{file_name}: mapped column {row.source_column!r} not at index {index}")
            unit = AMOUNT_UNITS.get(row.canonical_field)
            if unit and row.unit_in_label != unit:
                raise PrepareError(f"{file_name}: {row.canonical_field} expects unit {unit!r} in label, "
                                   f"mapping says {row.unit_in_label!r}")
        for column in ("source_column", "canonical_field"):
            repeated = sorted(rows[column][rows[column].duplicated()])
            if repeated:
                raise PrepareError(f"{file_name}: {column} mapped more than once {repeated!r}")
        unmapped = [c for c in columns if c not in set(rows["source_column"])]
        if unmapped:
            raise PrepareError(f"{file_name}: source columns without approved mapping {unmapped!r}")
        absent = [f for f in REQUIRED_FIELDS if f not in set(rows["canonical_field"])]
        if absent:
            raise PrepareError(f"{file_name}: required canonical fields not mapped {absent!r}")
        specs[file_name] = [
            FieldSpec(r.canonical_field, r.source_column, r.unit_in_label, r.price_basis_in_label)
            for r in rows.itertuples(index=False)
        ]
    field_order = list(dict.fromkeys(mapping["canonical_field"]))
    return specs, field_order


def check_manifest(path: Path, sources) -> None:
    if not path.exists():
        raise PrepareError(f"{relpath(path)} not found; run src/inventory.py first")
    recorded = {entry["file_name"]: entry["sha256"] for entry in json.loads(path.read_text("utf-8"))["sources"]}
    for source in sources:
        if recorded.get(source.path.name) != source.sha256:
            raise PrepareError(f"{source.path.name}: SHA256 differs from {relpath(path)}; rerun the inventory")


# --- preparation ------------------------------------------------------------

def prepare_source(frame: pd.DataFrame, source_file: str, release_year: str, specs: list[FieldSpec],
                   field_order: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Parse one raw release. Returns observations, field status rows and date-order evidence."""
    stem = Path(source_file).stem
    spec_by_field = {spec.canonical_field: spec for spec in specs}
    evidence = {
        spec.canonical_field: day_month_order(frame[spec.source_column])
        for spec in specs if field_kind(spec.canonical_field) == "date"
    }
    observations, statuses = [], []
    for source_row, record in enumerate(frame.to_dict(orient="records"), start=1):
        observation_id = f"{stem}:{source_row:04d}"
        base = {"observation_id": observation_id, "release_year": release_year,
                "source_file": source_file, "source_row": source_row}
        observation = dict(base)
        for field in field_order:
            kind, name = field_kind(field), output_name(field)
            spec = spec_by_field.get(field)
            raw = record[spec.source_column] if spec else None
            result = (parse_cell(spec, raw, evidence[field].state if field in evidence else None)
                      if spec else FieldResult(NOT_COLLECTED))
            observation[f"{name}_raw"] = raw
            if kind != "text":
                observation[name] = result.value
            observation[f"{name}_status"] = result.status
            if kind == "amount":
                observation[f"{name}_price_basis_label"] = spec.price_basis_in_label if spec else None
            if kind == "dca":
                observation[f"{name}_source_column"] = spec.source_column if spec else None
            if spec:
                statuses.append({
                    **base, "canonical_field": field, "output_field": name, "source_column": spec.source_column,
                    "raw_token": raw, "parsed_value": "" if result.value is None else str(result.value),
                    "status": result.status, "reason": result.reason,
                })
        observations.append(observation)

    obs = pd.DataFrame(observations)
    if ID_FIELD in spec_by_field:
        flags = flag_keys(obs[f"{ID_FIELD}_raw"])
        obs["project_id_missing"] = flags["missing"]
        obs["project_id_duplicate_in_release"] = flags["duplicate"]
    for name in (output_name(f) for f in field_order if field_kind(f) == "amount"):
        obs[name] = pd.to_numeric(obs[name])
    order_rows = [
        {"release_year": release_year, "source_file": source_file, "canonical_field": field,
         "source_column": spec_by_field[field].source_column, "first_part_over_12": ev.first_part_over_12,
         "second_part_over_12": ev.second_part_over_12, "state": ev.state}
        for field, ev in evidence.items()
    ]
    return obs, pd.DataFrame(statuses), order_rows


def select_dft(observations: pd.DataFrame) -> pd.DataFrame:
    """Rows whose Department equals the inventory-confirmed DfT label for their file."""
    selected = []
    for source_file, group in observations.groupby("source_file", sort=True):
        if source_file not in DFT_LABELS:
            raise PrepareError(f"{source_file}: no inventory-confirmed DfT label")
        label = DFT_LABELS[source_file]
        exact = group["department_raw"] == label
        folded = group["department_raw"].str.strip().str.casefold() == label.casefold()
        if not exact.equals(folded):
            variants = sorted(set(group.loc[folded & ~exact, "department_raw"]))
            raise PrepareError(f"{source_file}: DfT label variants not confirmed by inventory {variants!r}")
        if not exact.any():
            raise PrepareError(f"{source_file}: no rows with Department == {label!r}")
        selected.append(group[exact])
    return pd.concat(selected).sort_values(["release_year", "source_row"]).reset_index(drop=True)


def propose_scope(category: str, project_name: str) -> tuple[str, str]:
    """Mechanical proposal for the reviewer; the reviewer's include column is authoritative."""
    terms = [term for term in SCOPE_REVIEW_TERMS if term in project_name.casefold()]
    if category == INFRASTRUCTURE_CATEGORY:
        if terms:
            return "review", (f"source category '{category}', but project name contains {terms}; "
                              "confirm physical transport infrastructure scope")
        return "yes", f"source category '{category}'"
    if category in NON_INFRASTRUCTURE_CATEGORIES:
        return "no", f"source category '{category}' is not infrastructure/construction; include only with justification"
    return "review", f"no proposal rule for source category '{category}'"


def build_scope_review(dft: pd.DataFrame) -> pd.DataFrame:
    proposals = [propose_scope(c, n) for c, n in zip(dft["annual_report_category_raw"], dft["project_name_raw"])]
    scope = pd.DataFrame({
        "observation_id": dft["observation_id"],
        "release_year": dft["release_year"],
        "project_name": dft["project_name_raw"],
        "source_category": dft["annual_report_category_raw"],
        "include": "",
        "reason": "",
        "reviewer": "",
        "proposed_include": [p[0] for p in proposals],
        "proposed_basis": [p[1] for p in proposals],
        "source_file": dft["source_file"],
        "source_row": dft["source_row"],
        "project_id_raw": dft["project_id_raw"],
        "department_raw": dft["department_raw"],
        "project_description_raw": dft["project_description_raw"],
    })
    return scope[list(SCOPE_COLUMNS)]


def build_flagged(observations: pd.DataFrame, field_status: pd.DataFrame) -> pd.DataFrame:
    cells = field_status[field_status["status"].isin(UNAVAILABLE_BY_SOURCE + NEEDS_REVIEW)].copy()
    cells["flag_class"] = cells["status"].map(
        lambda s: "unavailable_by_source" if s in UNAVAILABLE_BY_SOURCE else "needs_review")
    key_rows = []
    for column, status, reason in (
        ("project_id_missing", "missing_key", "project identifier blank in source"),
        ("project_id_duplicate_in_release", "duplicate_key", "project identifier occurs more than once in release"),
    ):
        for row in observations[observations[column]].itertuples(index=False):
            key_rows.append({
                "observation_id": row.observation_id, "release_year": row.release_year,
                "source_file": row.source_file, "source_row": row.source_row, "canonical_field": ID_FIELD,
                "output_field": ID_FIELD, "source_column": "", "raw_token": row.project_id_raw,
                "parsed_value": "", "status": status, "reason": reason, "flag_class": "record_key_issue",
            })
    flagged = pd.concat([cells, pd.DataFrame(key_rows, columns=cells.columns)], ignore_index=True)
    return flagged.sort_values(["source_file", "source_row", "canonical_field", "status"]).reset_index(drop=True)


def status_counts(field_status: pd.DataFrame) -> pd.DataFrame:
    return (field_status.groupby(["release_year", "source_file", "canonical_field", "output_field", "status"])
            .size().rename("n").reset_index()
            .sort_values(["release_year", "canonical_field", "status"]).reset_index(drop=True))


def build_summary(observations, field_status, flagged, order_evidence, scope, sources, mapping_rows) -> str:
    lines = [
        "# Preparation summary", "",
        "Generated by `src/prepare.py`. No records were dropped and no values were imputed. "
        "Monetary values are not comparable across releases until a common price basis and scope are verified.", "",
        "## Inputs", "",
    ]
    lines += table(["File", "SHA256 (matches manifest)", "Release year", "Records"], [
        [s.path.name, code(s.sha256), observations.loc[observations.source_file == s.path.name, "release_year"].iat[0],
         int((observations.source_file == s.path.name).sum())] for s in sources
    ])
    lines += [f"Approved mapping rows used: {mapping_rows}", ""]

    lines += ["## Record keys", ""]
    lines += table(["Release", "Records", "Blank project_id", "Duplicate project_id within release"], [
        [ry, len(g), int(g["project_id_missing"].sum()), int(g["project_id_duplicate_in_release"].sum())]
        for ry, g in observations.groupby("release_year", sort=True)
    ])

    lines += ["## Date day/month order evidence", "",
              "Dates are parsed only where the column is `day_first_verified`: some exact dd/mm/yyyy token has a "
              "first part above 12 and none has a second part above 12.", ""]
    lines += table(["Release", "Field", "Source column", "First part > 12", "Second part > 12", "State"],
                   [[r["release_year"], r["canonical_field"], code(r["source_column"]), r["first_part_over_12"],
                     r["second_part_over_12"], r["state"]] for r in order_evidence])

    lines += ["## Field status counts", ""]
    for ry, group in field_status.groupby("release_year", sort=True):
        pivot = pd.crosstab(group["output_field"], group["status"])
        order = list(dict.fromkeys(group["output_field"]))
        pivot = pivot.reindex(order)
        lines += [f"### {ry}", ""]
        lines += table(["Field", *pivot.columns], [[f, *pivot.loc[f].tolist()] for f in pivot.index])

    lines += ["## Flagged cells and records", ""]
    counts = flagged.groupby(["release_year", "flag_class", "status"]).size().rename("n").reset_index()
    lines += table(["Release", "Flag class", "Status", "Count"], counts.values.tolist())

    lines += ["## DfT selection for scope review", "",
              "Selected by exact Department label per file: "
              + ", ".join(f"{f} {code(lbl)}" for f, lbl in sorted(DFT_LABELS.items())) + ".", ""]
    proposal_counts = scope.groupby(["release_year", "source_category", "proposed_include"]).size()
    lines += table(["Release", "Source category", "Proposed include", "Rows"],
                   [[*idx, n] for idx, n in proposal_counts.items()])
    lines += ["Proposals are mechanical (source category plus name terms "
              f"{', '.join(code(t) for t in SCOPE_REVIEW_TERMS)}); the reviewer's `include` column decides.", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    write_text(path, frame.to_csv(index=False, lineterminator="\n"))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED_DIR)
    parser.add_argument("--outputs-dir", type=Path, default=DEFAULT_OUTPUTS_DIR)
    parser.add_argument("--scope-review", type=Path, default=DEFAULT_SCOPE_REVIEW)
    parser.add_argument("--force-scope", action="store_true",
                        help="overwrite an existing scope review file (it may contain reviewer decisions)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    try:
        raw_paths = sorted(args.raw_dir.glob("*.csv"))
        if not raw_paths:
            raise PrepareError(f"no CSV files in {relpath(args.raw_dir)}")
        sources = [read_source(path) for path in raw_paths]
        check_manifest(args.manifest, sources)
        metadata = load_user_metadata(args.metadata, [s.path.name for s in sources])
        release_years = {s.path.name: metadata[s.path.name]["release_year"] for s in sources}
        missing = sorted(name for name, year in release_years.items() if not year)
        if missing:
            raise PrepareError(f"release_year not supplied in {relpath(args.metadata)} for {missing!r}")
        if len(set(release_years.values())) != len(release_years):
            raise PrepareError(f"release_year values are not unique {release_years!r}")
        specs, field_order = load_mapping(args.mapping, {s.path.name: list(s.frame.columns) for s in sources})

        obs_parts, status_parts, order_evidence = [], [], []
        for source in sources:
            obs, status, evidence = prepare_source(
                source.frame, source.path.name, release_years[source.path.name], specs[source.path.name], field_order)
            obs_parts.append(obs)
            status_parts.append(status)
            order_evidence += evidence
        observations = pd.concat(obs_parts, ignore_index=True)
        field_status = pd.concat(status_parts, ignore_index=True)
        dft = select_dft(observations)
        scope = build_scope_review(dft)
        flagged = build_flagged(observations, field_status)
    except (InventoryError, PrepareError) as exc:
        print(f"prepare failed: {exc}", file=sys.stderr)
        return 1

    write_csv(observations, args.processed_dir / "observations.csv")
    write_csv(field_status, args.processed_dir / "field_status.csv")
    write_csv(flagged, args.outputs_dir / "flagged_records.csv")
    write_csv(status_counts(field_status), args.outputs_dir / "field_status_counts.csv")
    write_text(args.outputs_dir / "prepare_summary.md", build_summary(
        observations, field_status, flagged, order_evidence, scope, sources,
        sum(len(v) for v in specs.values())))
    if args.scope_review.exists() and not args.force_scope:
        print(f"kept existing {relpath(args.scope_review)} (pass --force-scope to overwrite)")
    else:
        write_csv(scope, args.scope_review)
        print(f"wrote {relpath(args.scope_review)} ({len(scope)} rows)")

    print(f"observations: {len(observations)} rows; field status: {len(field_status)} rows; "
          f"flagged: {len(flagged)} rows")
    for name, n in Counter(field_status["status"]).most_common():
        print(f"  {name}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
