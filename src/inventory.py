"""Source inventory for the NISTA major-projects CSV releases in data/raw.

Reads every raw CSV as strings (no NA coercion, no cleaning) and writes:

* data/source_manifest.json      - file provenance; user-supplied fields come
                                   only from data/manual/source_metadata.csv
* outputs/source_inventory.md    - structural profile of the raw files
* data/manual/column_mapping.csv - proposed canonical field mapping for review

No cleaned analytical dataset is produced. Raw files are only read.

Usage:
    python src/inventory.py [--force-mapping]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw"
DEFAULT_METADATA = PROJECT_ROOT / "data" / "manual" / "source_metadata.csv"
DEFAULT_MANIFEST = PROJECT_ROOT / "data" / "source_manifest.json"
DEFAULT_INVENTORY = PROJECT_ROOT / "outputs" / "source_inventory.md"
DEFAULT_MAPPING = PROJECT_ROOT / "data" / "manual" / "column_mapping.csv"

UTF8_BOM = b"\xef\xbb\xbf"
CANDIDATE_DELIMITERS = (",", ";", "\t", "|")

# Fields the user supplies in the metadata file. Never derived from the files
# themselves or from filesystem timestamps.
METADATA_FIELDS = ("release_year", "snapshot_date", "retrieval_date", "source_page_url", "download_url")
ISO_DATE_FIELDS = ("snapshot_date", "retrieval_date")
URL_FIELDS = ("source_page_url", "download_url")
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
URL_RE = re.compile(r"https?://\S+")
# Official links checked by SHA256 against the raw files. Kept apart from the user-supplied fields so
# their provenance stays visible; verified_on is the date of that check, not a retrieval date.
DEFAULT_SOURCE_LINKS = PROJECT_ROOT / "data" / "reference" / "source_links.csv"
LINK_URL_FIELDS = ("source_page_url", "download_url", "report_url")
LINK_COLUMNS = ("file_name", "release_year", *LINK_URL_FIELDS, "sha256", "verified_on", "verification")

# Token patterns used only for profiling; nothing is converted.
NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?")
GROUPED_NUMERIC_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
DMY_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
PROJECT_ID_RE = re.compile(r"[A-Z]+_\d{4}_\d{4}-Q[1-4]")
CANDIDATE_SHARE = 0.5
TOKEN_LIST_LIMIT = 25
EXAMPLE_LIMIT = 5
RECURRING_MIN_COLUMNS = 3
# Unicode categories rendered as <U+XXXX> in the report so they stay visible.
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Co", "Cn", "Zs", "Zl", "Zp")

PROJECT_NAME_COLUMN = "Project Name"
DEPARTMENT_COLUMN = "Department"
CATEGORY_COLUMN = "Annual Report Category"
ID_COLUMNS = ("Major Projects ID", "GMPP ID")
DFT_CASEFOLD = "dft"

KNOWN_UNITS = ("£m", "%")
TRAILING_PAREN_RE = re.compile(r"\(([^()]*)\)\s*$")
PRICE_BASIS_PREFIX = "Presented in "

# Proposed canonical field -> exact source header per raw file stem, with a
# review note. Headers were copied from the raw files; the script fails if a
# proposed header is absent. Columns not listed here are reported as unmapped.
MONEY_NOTE = (
    "Price basis taken from the column label only. Do not compare across releases "
    "until a common price basis and scope are verified."
)
MAPPING_PROPOSALS: tuple[tuple[str, dict[str, str], str], ...] = (
    ("project_id", {"nista_2024_25": "GMPP ID", "nista_2025_26": "Major Projects ID"},
     "Header differs between releases. Treat as one identifier only after reviewing format, "
     "uniqueness and overlap evidence in outputs/source_inventory.md section 6."),
    ("project_name", {"nista_2024_25": "Project Name", "nista_2025_26": "Project Name"},
     "Free text; not a join key on its own."),
    ("department", {"nista_2024_25": "Department", "nista_2025_26": "Department"},
     "Compare value labels across releases (inventory section 4) before any recoding."),
    ("annual_report_category", {"nista_2024_25": "Annual Report Category", "nista_2025_26": "Annual Report Category"},
     "Compare value labels across releases (inventory section 4) before any recoding."),
    ("project_description", {"nista_2024_25": "Project Description", "nista_2025_26": "Project Description"},
     "Free text."),
    ("ipa_delivery_confidence", {"nista_2024_25": "IPA Delivery Confidence Assessment",
                                 "nista_2025_26": "IPA Delivery Confidence Assessment"},
     "Rating tokens kept raw; non-rating tokens are missingness reasons to preserve."),
    ("sro_delivery_confidence", {"nista_2024_25": "SRO Delivery Confidence Assessment",
                                 "nista_2025_26": "SRO Delivery Confidence Assessment"},
     "Rating tokens kept raw; non-rating tokens are missingness reasons to preserve."),
    ("delivery_confidence_commentary", {
        "nista_2024_25": "Departmental Commentary on Delivery Confidence Assessment Rating",
        "nista_2025_26": "Departmental Commentary on Delivery Confidence Assessment Rating"},
     "Free text."),
    ("start_date", {"nista_2024_25": "Start Date", "nista_2025_26": "Start Date"},
     "Raw date tokens; parsing deferred (see inventory section 8)."),
    ("end_date", {"nista_2024_25": "End Date", "nista_2025_26": "End Date"},
     "Raw date tokens; parsing deferred (see inventory section 8)."),
    ("schedule_narrative", {"nista_2024_25": "Schedule Narrative", "nista_2025_26": "Schedule Narrative"},
     "Free text."),
    ("fy_baseline_gbp_m", {"nista_2024_25": "Financial Year Baseline (£m)",
                           "nista_2025_26": "Financial Year Baseline (£m, Presented in 2024/25 Real Prices)"},
     MONEY_NOTE),
    ("fy_forecast_gbp_m", {"nista_2024_25": "Financial Year Forecast (£m)",
                           "nista_2025_26": "Financial Year Forecast (£m, Presented in 2024/25 Real Prices)"},
     MONEY_NOTE),
    ("fy_variance_pct", {"nista_2024_25": "Financial Year Variance (%)",
                         "nista_2025_26": "Financial Year Variance (%)"},
     "Percentage as published; derivation method not stated in the label."),
    ("in_year_variance_narrative", {"nista_2024_25": "In Year Variance Narrative",
                                    "nista_2025_26": "In Year Variance Narrative"},
     "Free text."),
    ("whole_life_cost_gbp_m", {"nista_2024_25": "Whole Life Cost (£m)",
                               "nista_2025_26": "Whole Life Cost (£m, Presented in 2024/25 Real Prices)"},
     MONEY_NOTE),
    ("costs_narrative", {"nista_2024_25": "Costs Narrative", "nista_2025_26": "Costs Narrative"},
     "Free text."),
    ("benefits_gbp_m", {"nista_2024_25": "Benefits (£m)",
                        "nista_2025_26": "Benefits (£m, Presented in 2024/25 Real Prices)"},
     MONEY_NOTE + " Check non-numeric token formats in inventory section 8."),
    ("benefits_narrative", {"nista_2024_25": "Benefits Narrative", "nista_2025_26": "Benefits Narrative"},
     "Free text."),
    ("evaluation_plan_self_reported", {"nista_2024_25": "Does the project have an evaluation plan? (self reported)"},
     "Release-specific question; not mapped to the other release's evaluation column because the questions differ."),
    ("evaluation_plan_registry_link", {"nista_2025_26": "Evaluation Plan and Registry Link"},
     "Release-specific question; not mapped to the other release's evaluation column because the questions differ."),
    ("sro_name", {"nista_2024_25": "Senior Responsible Owner (SRO) Name",
                  "nista_2025_26": "Senior Responsible Owner (SRO) Name"},
     "Personal name as published."),
)
MAPPING_COLUMNS = (
    "canonical_field", "source_file", "source_column_index", "source_column", "unit_in_label",
    "price_basis_in_label", "relationship", "observed_profile", "proposal_note", "review_status",
)


class InventoryError(Exception):
    """Raised when a raw file or the metadata file fails a structural check."""


@dataclass(frozen=True)
class Source:
    path: Path
    sha256: str
    byte_size: int
    has_bom: bool
    delimiter: str
    frame: pd.DataFrame
    embedded_newline_cells: int

    @property
    def name(self) -> str:
        return self.path.stem


@dataclass(frozen=True)
class FieldProfile:
    index: int
    name: str
    nonempty: int
    exact_empty: int
    whitespace_only: int
    whitespace_edges: int
    distinct_nonempty: int
    numeric: int
    grouped_numeric: int
    dmy_dates: int
    kind: str
    unit_in_label: str


def relpath(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def ranked(counter: Counter) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def csv_records(text: str, delimiter: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True))


def detect_delimiter(text: str, file_name: str) -> str:
    """Return the only candidate delimiter giving a constant record width above one."""
    viable, header_widths = [], {}
    for delimiter in CANDIDATE_DELIMITERS:
        try:
            records = csv_records(text, delimiter)
        except csv.Error:
            header_widths[delimiter] = "parse error"
            continue
        widths = {len(record) for record in records}
        header_widths[delimiter] = len(records[0]) if records else 0
        if len(widths) == 1 and widths.pop() > 1:
            viable.append(delimiter)
    if all(width in (0, 1, "parse error") for width in header_widths.values()):
        raise InventoryError(f"{file_name}: every candidate delimiter parses at most one column {header_widths!r}")
    if len(viable) != 1:
        raise InventoryError(
            f"{file_name}: delimiter not unambiguous; viable={viable!r} header widths={header_widths!r}"
        )
    return viable[0]


def read_source(path: Path) -> Source:
    raw = path.read_bytes()
    has_bom = raw.startswith(UTF8_BOM)
    try:
        text = raw[len(UTF8_BOM):].decode("utf-8") if has_bom else raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InventoryError(f"{path.name}: not valid UTF-8 ({exc})") from exc

    delimiter = detect_delimiter(text, path.name)
    records = csv_records(text, delimiter)
    header, body = records[0], records[1:]
    if len(header) == 1:
        raise InventoryError(f"{path.name}: only one column parsed with delimiter {delimiter!r}")
    if any(name.startswith("\ufeff") for name in header):
        raise InventoryError(f"{path.name}: byte order mark left in header after decoding")
    duplicates = sorted(name for name, count in Counter(header).items() if count > 1)
    if duplicates:
        raise InventoryError(f"{path.name}: duplicate header names {duplicates!r}")

    # keep_default_na=False keeps blank and "NA"-like tokens as literal strings;
    # na_filter=False additionally disables NA detection entirely.
    frame = pd.read_csv(
        io.StringIO(text, newline=""), sep=delimiter, dtype=str, keep_default_na=False, na_filter=False
    )
    if frame.shape[1] == 1:
        raise InventoryError(f"{path.name}: pandas parsed only one column")
    if list(frame.columns) != header:
        raise InventoryError(f"{path.name}: pandas header differs from csv module header")
    if frame.isna().any().any():
        raise InventoryError(f"{path.name}: NA values present after string read")
    reference = pd.DataFrame(body, columns=header, dtype=str)
    if frame.shape != reference.shape or not (frame.to_numpy() == reference.to_numpy()).all():
        raise InventoryError(f"{path.name}: pandas values differ from csv module values")

    return Source(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        has_bom=has_bom,
        delimiter=delimiter,
        frame=frame,
        embedded_newline_cells=sum(("\n" in v or "\r" in v) for row in body for v in row),
    )


def load_user_metadata(path: Path, file_names: list[str]) -> dict[str, dict[str, str | None]]:
    empty = {name: dict.fromkeys(METADATA_FIELDS) for name in file_names}
    if not path.exists():
        return empty
    table = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False, encoding="utf-8-sig")
    expected = ["file_name", *METADATA_FIELDS]
    if list(table.columns) != expected:
        raise InventoryError(f"{relpath(path)}: columns must be {expected!r}, got {list(table.columns)!r}")
    if table["file_name"].duplicated().any():
        raise InventoryError(f"{relpath(path)}: duplicate file_name rows")
    unknown = sorted(set(table["file_name"]) - set(file_names))
    if unknown:
        raise InventoryError(f"{relpath(path)}: rows for files not in data/raw {unknown!r}")

    metadata = empty
    for record in table.to_dict(orient="records"):
        name = record["file_name"]
        for field in METADATA_FIELDS:
            value = record[field].strip()
            if not value:
                continue
            if field in ISO_DATE_FIELDS:
                if not ISO_DATE_RE.fullmatch(value):
                    raise InventoryError(f"{relpath(path)}: {name} {field} must be YYYY-MM-DD, got {value!r}")
                date.fromisoformat(value)
            if field in URL_FIELDS and not URL_RE.fullmatch(value):
                raise InventoryError(f"{relpath(path)}: {name} {field} is not an http(s) URL: {value!r}")
            metadata[name][field] = value
    return metadata


def load_verified_links(path: Path, sources: list[Source], metadata) -> dict[str, dict[str, str] | None]:
    """Links whose recorded SHA256 equals the raw file's; None where no row exists."""
    links = {s.path.name: None for s in sources}
    if not path.exists():
        return links
    table = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False, encoding="utf-8-sig")
    if tuple(table.columns) != LINK_COLUMNS:
        raise InventoryError(f"{relpath(path)}: columns must be {list(LINK_COLUMNS)!r}, got {list(table.columns)!r}")
    if table["file_name"].duplicated().any():
        raise InventoryError(f"{relpath(path)}: duplicate file_name rows")
    hashes = {s.path.name: s.sha256 for s in sources}
    unknown = sorted(set(table["file_name"]) - set(hashes))
    if unknown:
        raise InventoryError(f"{relpath(path)}: rows for files not in data/raw {unknown!r}")
    for record in table.to_dict(orient="records"):
        name = record["file_name"]
        if record["sha256"] != hashes[name]:
            raise InventoryError(f"{relpath(path)}: {name} SHA256 does not match the raw file; link not verified")
        if any(not URL_RE.fullmatch(record[f]) for f in LINK_URL_FIELDS):
            raise InventoryError(f"{relpath(path)}: {name} has a field that is not an http(s) URL")
        if not ISO_DATE_RE.fullmatch(record["verified_on"]):
            raise InventoryError(f"{relpath(path)}: {name} verified_on must be YYYY-MM-DD")
        supplied_year = metadata[name]["release_year"]
        if supplied_year and supplied_year != record["release_year"]:
            raise InventoryError(f"{relpath(path)}: {name} release_year {record['release_year']!r} differs from "
                                 f"user metadata {supplied_year!r}")
        links[name] = {field: record[field] for field in LINK_COLUMNS if field != "file_name"}
    return links


def profile_field(index: int, name: str, values: list[str]) -> FieldProfile:
    nonempty = [v for v in values if v.strip()]
    numeric = sum(bool(NUMERIC_RE.fullmatch(v)) for v in nonempty)
    grouped_numeric = sum(bool(GROUPED_NUMERIC_RE.fullmatch(v)) for v in nonempty)
    dmy_dates = sum(bool(DMY_DATE_RE.fullmatch(v)) for v in nonempty)
    kind = "text"
    if nonempty and (numeric + grouped_numeric) / len(nonempty) >= CANDIDATE_SHARE:
        kind = "numeric_candidate"
    elif nonempty and dmy_dates / len(nonempty) >= CANDIDATE_SHARE:
        kind = "date_candidate"
    return FieldProfile(
        index=index,
        name=name,
        nonempty=len(nonempty),
        exact_empty=sum(v == "" for v in values),
        whitespace_only=sum(v != "" and not v.strip() for v in values),
        whitespace_edges=sum(bool(v.strip()) and v != v.strip() for v in values),
        distinct_nonempty=len(set(nonempty)),
        numeric=numeric,
        grouped_numeric=grouped_numeric,
        dmy_dates=dmy_dates,
        kind=kind,
        unit_in_label=label_unit_and_basis(name)[0],
    )


def label_unit_and_basis(header: str) -> tuple[str, str]:
    match = TRAILING_PAREN_RE.search(header)
    if not match:
        return "none_in_label", "not_stated_in_label"
    parts = [part.strip() for part in match.group(1).split(",")]
    unit = parts[0] if parts[0] in KNOWN_UNITS else "none_in_label"
    basis = next(
        (part.removeprefix(PRICE_BASIS_PREFIX) for part in parts[1:] if part.startswith(PRICE_BASIS_PREFIX)),
        "not_stated_in_label",
    )
    return unit, basis


# --- markdown helpers -------------------------------------------------------

def invisible_chars(value: str) -> set[str]:
    return {ch for ch in value if ch not in " \r\n" and unicodedata.category(ch) in INVISIBLE_CATEGORIES}


def code(value: str) -> str:
    """Render a raw token in a code span with invisible characters made visible.

    Newlines become \\n, other invisible or format characters become <U+XXXX>,
    and leading/trailing ordinary spaces become ␠. Display only; data is untouched.
    """
    if value == "":
        return "*(empty string)*"
    text = value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")
    text = "".join(f"<U+{ord(ch):04X}>" if ch in invisible_chars(ch) else ch for ch in text)
    core = text.strip(" ")
    if core:
        start = text.index(core)
        text = "␠" * start + core + "␠" * (len(text) - start - len(core))
    else:
        text = "␠" * len(text)
    text = text.replace("|", "\\|")
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines + [""]


def token_table(counter: Counter, limit: int = TOKEN_LIST_LIMIT) -> list[str]:
    items = ranked(counter)
    lines = table(["Raw token", "Rows"], [[code(token), count] for token, count in items[:limit]])
    if len(items) > limit:
        lines.insert(-1, f"\n*{len(items) - limit} more distinct tokens not shown.*")
    return lines


# --- inventory sections -----------------------------------------------------

def section_files(sources: list[Source], metadata: dict[str, dict[str, str | None]], links) -> list[str]:
    lines = ["## 1. Files", ""]
    rows = []
    for s in sources:
        by_codepoint = Counter()
        for v in s.frame.to_numpy().ravel().tolist():
            by_codepoint.update(invisible_chars(v))
        invisible_summary = "; ".join(
            f"U+{ord(ch):04X} {unicodedata.name(ch, 'UNNAMED')}: {n}"
            for ch, n in sorted(by_codepoint.items(), key=lambda kv: ord(kv[0]))
        ) or "none"
        rows.append([
            code(relpath(s.path)), code(s.sha256), s.byte_size, "yes" if s.has_bom else "no", code(s.delimiter),
            s.frame.shape[0], s.frame.shape[1], s.embedded_newline_cells, invisible_summary,
        ])
    lines += table(
        ["File", "SHA256", "Bytes", "UTF-8 BOM", "Delimiter", "Data rows", "Columns",
         "Cells with embedded newlines", "Cells containing invisible/format characters (by code point)"],
        rows,
    )
    lines += ["User-supplied provenance (from `data/manual/source_metadata.csv`):", ""]
    lines += table(
        ["File", *METADATA_FIELDS],
        [[s.path.name, *(code(v) if v else "**not supplied**" for v in metadata[s.path.name].values())]
         for s in sources],
    )
    lines += ["Verified official links (from `data/reference/source_links.csv`; the file served at "
              "`download_url` had the same SHA256 as the raw file on `verified_on`, which is not a retrieval "
              "date):", ""]
    lines += table(
        ["File", *LINK_URL_FIELDS, "verified_on"],
        [[s.path.name, *(code(links[s.path.name][f]) for f in (*LINK_URL_FIELDS, "verified_on"))]
         if links[s.path.name] else [s.path.name, *["**not verified**"] * (len(LINK_URL_FIELDS) + 1)]
         for s in sources],
    )
    no_retrieval = [s.path.name for s in sources if not metadata[s.path.name]["retrieval_date"]]
    if no_retrieval:
        lines += [f"Limitation: the retrieval date of {', '.join(no_retrieval)} is unknown and left blank. It is not "
                  "inferred from filesystem timestamps or from `verified_on`; the hash match shows only that the "
                  "official file was unchanged on that date.", ""]
    return lines


def section_columns(sources: list[Source]) -> list[str]:
    lines = ["## 2. Exact column names", ""]
    for s in sources:
        lines += [f"### {s.path.name}", ""]
        lines += table(["#", "Column name"], [[i, code(c)] for i, c in enumerate(s.frame.columns)])
    if len(sources) > 1:
        sets = {s.name: set(s.frame.columns) for s in sources}
        common = set.intersection(*sets.values())
        lines += ["### Header comparison (exact string equality)", "",
                  f"Names present in every file: {len(common)}", ""]
        for s in sources:
            only = [c for c in s.frame.columns if c not in common]
            lines.append(f"Only in {s.path.name} ({len(only)}):")
            lines += [f"- {code(c)}" for c in only] or ["- none"]
            lines.append("")
    return lines


def section_fields(sources: list[Source], profiles: dict[str, list[FieldProfile]]) -> list[str]:
    lines = [
        "## 3. Field profiles", "",
        "Non-empty means the value contains at least one non-whitespace character. "
        f"Strict numeric means the whole token matches `{NUMERIC_RE.pattern}`; comma-grouped numeric "
        f"means it matches `{GROUPED_NUMERIC_RE.pattern}`; dd/mm/yyyy means it matches "
        f"`{DMY_DATE_RE.pattern}`. A field is a numeric candidate when at least {CANDIDATE_SHARE:.0%} of its "
        "non-empty tokens are strict or comma-grouped numeric, and a date candidate on the same rule for "
        "dd/mm/yyyy. Nothing is converted.", "",
    ]
    for s in sources:
        lines += [f"### {s.path.name} ({s.frame.shape[0]} rows)", ""]
        lines += table(
            ["#", "Column", "Non-empty", "Empty string", "Whitespace only", "Leading/trailing whitespace",
             "Distinct non-empty", "Strict numeric", "Comma-grouped numeric", "dd/mm/yyyy", "Unit in label",
             "Profile"],
            [[p.index, code(p.name), p.nonempty, p.exact_empty, p.whitespace_only, p.whitespace_edges,
              p.distinct_nonempty, p.numeric, p.grouped_numeric, p.dmy_dates, p.unit_in_label, p.kind]
             for p in profiles[s.name]],
        )
    return lines


def section_frequencies(sources: list[Source]) -> list[str]:
    lines = ["## 4. Department and category frequencies (exact labels)", ""]
    for column in (DEPARTMENT_COLUMN, CATEGORY_COLUMN):
        lines += [f"### {code(column)}", ""]
        for s in sources:
            lines.append(f"**{s.path.name}**")
            lines.append("")
            if column not in s.frame.columns:
                lines += ["Column not present.", ""]
                continue
            lines += token_table(Counter(s.frame[column]), limit=10_000)
    if all(DEPARTMENT_COLUMN in s.frame.columns for s in sources):
        lines += [
            f"### {code(DEPARTMENT_COLUMN)} labels grouped by case-insensitive equality across files", "",
            "Grouping is for review only; no recoding is applied.", "",
        ]
        groups: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
        for s in sources:
            for label in s.frame[DEPARTMENT_COLUMN]:
                groups[label.strip().casefold()][s.name][label] += 1
        rows = []
        for key in sorted(groups):
            row = [code(key)]
            for s in sources:
                labels = groups[key].get(s.name, Counter())
                row.append(", ".join(f"{code(lbl)} ({n})" for lbl, n in ranked(labels)) or "—")
            rows.append(row)
        lines += table(["Case-folded key", *(s.path.name for s in sources)], rows)
    return lines


def section_dft(sources: list[Source]) -> list[str]:
    lines = [
        "## 5. DfT department and category labels", "",
        f"Rule: rows whose {code(DEPARTMENT_COLUMN)} value, stripped and case-folded, equals "
        f"{code(DFT_CASEFOLD)}. Labels are shown exactly as published.", "",
    ]
    rows = []
    for s in sources:
        if DEPARTMENT_COLUMN not in s.frame.columns or CATEGORY_COLUMN not in s.frame.columns:
            rows.append([s.path.name, "column not present", "", 0])
            continue
        subset = s.frame[s.frame[DEPARTMENT_COLUMN].str.strip().str.casefold() == DFT_CASEFOLD]
        pairs = Counter(zip(subset[DEPARTMENT_COLUMN], subset[CATEGORY_COLUMN]))
        if not pairs:
            rows.append([s.path.name, "no matching rows", "", 0])
        for (dept, category), count in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0])):
            rows.append([s.path.name, code(dept), code(category), count])
    lines += table(["File", "Department label", "Annual Report Category label", "Rows"], rows)
    return lines


def section_identifiers(sources: list[Source]) -> list[str]:
    lines = ["## 6. Identifier columns", "", "### Presence", ""]
    lines += table(
        ["Column", *(s.path.name for s in sources)],
        [[code(col), *("present" if col in s.frame.columns else "**absent**" for s in sources)]
         for col in ID_COLUMNS],
    )
    present_everywhere = [col for col in ID_COLUMNS if all(col in s.frame.columns for s in sources)]
    lines += [
        "Identifier columns present in every file under the same name: "
        + (", ".join(code(c) for c in present_everywhere) if present_everywhere else "**none**"), "",
        f"Format check pattern (whole token): `{PROJECT_ID_RE.pattern}`", "",
    ]
    found: list[tuple[Source, str, set[str]]] = []
    for s in sources:
        for col in ID_COLUMNS:
            if col not in s.frame.columns:
                continue
            values = s.frame[col].tolist()
            nonempty = [v for v in values if v.strip()]
            counts = Counter(nonempty)
            duplicated = {v: n for v, n in counts.items() if n > 1}
            nonconforming = Counter(v for v in nonempty if not PROJECT_ID_RE.fullmatch(v))
            found.append((s, col, set(nonempty)))
            lines += [f"### {code(col)} in {s.path.name}", ""]
            lines += table(["Measure", "Value"], [
                ["Rows", len(values)],
                ["Non-empty", len(nonempty)],
                ["Empty or whitespace only", len(values) - len(nonempty)],
                ["Distinct non-empty", len(counts)],
                ["Values occurring more than once", len(duplicated)],
                ["Rows carrying a duplicated value", sum(duplicated.values())],
                ["Values with leading/trailing whitespace", sum(v != v.strip() for v in nonempty)],
                ["Non-empty values not matching the format pattern", sum(nonconforming.values())],
            ])
            if duplicated:
                lines += ["Duplicated values:", ""] + token_table(Counter(duplicated))
            if nonconforming:
                lines += ["Non-conforming values:", ""] + token_table(nonconforming)
            blank_rows = s.frame[s.frame[col].str.strip() == ""]
            if not blank_rows.empty and PROJECT_NAME_COLUMN in s.frame.columns:
                lines += [f"Rows with an empty {code(col)} (0-based data row, project name):", ""]
                lines += table(["Row", "Project Name"],
                               [[i, code(name)] for i, name in blank_rows[PROJECT_NAME_COLUMN].items()])
    if len({s.name for s, _, _ in found}) > 1:
        lines += [
            "### Exact-string overlap of identifier values between files", "",
            "Counts distinct non-empty values that are byte-identical in both columns. "
            "This is not a validated project match.", "",
        ]
        rows = []
        for i, (sa, ca, va) in enumerate(found):
            for sb, cb, vb in found[i + 1:]:
                if sa.name != sb.name:
                    rows.append([f"{sa.path.name} {code(ca)}", len(va), f"{sb.path.name} {code(cb)}", len(vb),
                                 len(va & vb)])
        lines += table(["Column A", "Distinct A", "Column B", "Distinct B", "Shared exact values"], rows)
    return lines


def section_keys(sources: list[Source]) -> list[str]:
    lines = [
        "## 7. Duplicate candidate keys", "",
        "A key value is blank when any part is empty or whitespace only. Blank key values are excluded "
        "from duplicate counts. The normalised variant strips whitespace and case-folds each part, for review only.",
        "",
    ]
    for s in sources:
        frame = s.frame
        candidates = [[c] for c in ID_COLUMNS if c in frame.columns]
        candidates += [[c] for c in (PROJECT_NAME_COLUMN,) if c in frame.columns]
        if PROJECT_NAME_COLUMN in frame.columns and DEPARTMENT_COLUMN in frame.columns:
            candidates.append([PROJECT_NAME_COLUMN, DEPARTMENT_COLUMN])
        rows, details = [], []
        for key in candidates:
            for variant in ("exact", "normalised"):
                tuples = [tuple(parts) for parts in frame[key].itertuples(index=False, name=None)]
                if variant == "normalised":
                    tuples = [tuple(p.strip().casefold() for p in t) for t in tuples]
                usable = [t for t in tuples if all(p.strip() for p in t)]
                counts = Counter(usable)
                duplicated = {t: n for t, n in counts.items() if n > 1}
                label = " + ".join(key)
                rows.append([code(label), variant, len(tuples) - len(usable), len(counts), len(duplicated),
                             sum(duplicated.values())])
                if duplicated:
                    details += [f"Duplicated {variant} values of {code(label)}:", ""]
                    details += token_table(Counter({" ‖ ".join(t): n for t, n in duplicated.items()}))
        full_row_duplicates = int(frame.duplicated(keep=False).sum())
        lines += [f"### {s.path.name}", ""]
        lines += table(["Key", "Variant", "Blank key rows", "Distinct key values", "Duplicated key values",
                        "Rows carrying a duplicated key"], rows)
        lines += details
        lines += [f"Fully identical rows (all columns): {full_row_duplicates}", ""]
    return lines


def section_tokens(sources: list[Source], profiles: dict[str, list[FieldProfile]]) -> list[str]:
    lines = [
        "## 8. Representative non-numeric and non-date tokens", "",
        "Covers date candidate fields and every field that is a numeric candidate or carries a unit "
        f"({', '.join(KNOWN_UNITS)}) in its label (section 3). Every distinct non-conforming non-empty token "
        f"is counted; the {TOKEN_LIST_LIMIT} most frequent are shown verbatim.", "",
    ]
    for s in sources:
        lines += [f"### {s.path.name}", ""]
        for p in profiles[s.name]:
            numeric_field = p.kind == "numeric_candidate" or p.unit_in_label in KNOWN_UNITS
            if not numeric_field and p.kind != "date_candidate":
                continue
            values = [v for v in s.frame[p.name] if v.strip()]
            lines += [f"#### {code(p.name)} ({p.kind}; unit in label: {p.unit_in_label})", ""]
            if numeric_field:
                grouped = Counter(v for v in values if GROUPED_NUMERIC_RE.fullmatch(v))
                other = Counter(v for v in values if not NUMERIC_RE.fullmatch(v) and v not in grouped)
                lines += [f"Non-empty {p.nonempty}; strict numeric {p.numeric}; comma-grouped numeric "
                          f"{p.grouped_numeric}; other {sum(other.values())} in {len(other)} distinct tokens.", ""]
                if grouped:
                    examples = ", ".join(code(t) for t, _ in ranked(grouped)[:EXAMPLE_LIMIT])
                    lines += [f"Comma-grouped examples: {examples}", ""]
            else:
                matches = [DMY_DATE_RE.fullmatch(v) for v in values]
                matched = [m for m in matches if m]
                invalid = Counter()
                for m in matched:
                    try:
                        datetime.strptime(m.group(0), "%d/%m/%Y")
                    except ValueError:
                        invalid[m.group(0)] += 1
                other = Counter(v for v, m in zip(values, matches) if not m)
                lines += [
                    f"Non-empty {p.nonempty}; dd/mm/yyyy pattern {len(matched)}; "
                    f"first part > 12: {sum(int(m.group(1)) > 12 for m in matched)}; "
                    f"second part > 12: {sum(int(m.group(2)) > 12 for m in matched)}; "
                    f"not a valid dd/mm/yyyy calendar date: {sum(invalid.values())}; "
                    f"non-date {sum(other.values())} in {len(other)} distinct tokens.", "",
                ]
                if invalid:
                    lines += ["Pattern matches that are not valid dd/mm/yyyy dates:", ""] + token_table(invalid)
            if other:
                lines += token_table(other)
    return lines


def section_recurring(sources: list[Source]) -> list[str]:
    lines = [
        "## 9. Recurring non-data tokens", "",
        f"Non-empty tokens that are neither strict numeric nor dd/mm/yyyy and appear verbatim in at least "
        f"{RECURRING_MIN_COLUMNS} different columns of the same file. These are candidate missingness "
        "reasons (for example exemption or status text); they are listed, not classified.", "",
    ]
    for s in sources:
        columns_by_token: dict[str, set[str]] = defaultdict(set)
        cells = Counter()
        for column in s.frame.columns:
            for v in s.frame[column]:
                if v.strip() and not NUMERIC_RE.fullmatch(v) and not DMY_DATE_RE.fullmatch(v):
                    columns_by_token[v].add(column)
                    cells[v] += 1
        recurring = sorted(
            (t for t, cols in columns_by_token.items() if len(cols) >= RECURRING_MIN_COLUMNS),
            key=lambda t: (-len(columns_by_token[t]), -cells[t], t),
        )
        lines += [f"### {s.path.name}", ""]
        lines += table(["Raw token", "Columns", "Cells"],
                       [[code(t), len(columns_by_token[t]), cells[t]] for t in recurring])
    return lines


def build_inventory(sources, metadata, profiles, links) -> str:
    lines = [
        "# Source inventory", "",
        "Generated by `src/inventory.py` from the files in `data/raw`. Every value is read as a string "
        "with `keep_default_na=False`; nothing is converted, cleaned or matched. "
        "Raw tokens are shown in code spans with display-only escapes: newlines as `\\n`, invisible or "
        "format characters as `<U+XXXX>`, leading/trailing spaces as `␠`, pipes as `\\|`.", "",
    ]
    for section in (
        section_files(sources, metadata, links),
        section_columns(sources),
        section_fields(sources, profiles),
        section_frequencies(sources),
        section_dft(sources),
        section_identifiers(sources),
        section_keys(sources),
        section_tokens(sources, profiles),
        section_recurring(sources),
    ):
        lines += section
    return "\n".join(lines).rstrip("\n") + "\n"


# --- manifest and mapping ---------------------------------------------------

def build_manifest(sources: list[Source], metadata, metadata_path: Path, links, links_path: Path) -> str:
    entries = []
    for s in sources:
        supplied = metadata[s.path.name]
        entries.append({
            "file_name": s.path.name,
            "path": relpath(s.path),
            "sha256": s.sha256,
            "byte_size": s.byte_size,
            "encoding": "utf-8",
            "utf8_bom": s.has_bom,
            "delimiter": s.delimiter,
            "header_columns": int(s.frame.shape[1]),
            "data_rows": int(s.frame.shape[0]),
            **supplied,
            "user_metadata_missing": [field for field in METADATA_FIELDS if supplied[field] is None],
            "verified_links": links[s.path.name],
        })
    manifest = {
        "manifest_version": 1,
        "user_metadata_file": relpath(metadata_path),
        "user_metadata_file_found": metadata_path.exists(),
        "provenance_rules": (
            "release_year, snapshot_date, retrieval_date, source_page_url and download_url are copied "
            "from the user metadata file only; null means not supplied. They are never inferred from "
            "file names, file contents or filesystem timestamps."
        ),
        "verified_links_file": relpath(links_path),
        "verified_links_rule": (
            "verified_links are copied from the verified links file only when its recorded SHA256 equals the "
            "raw file's SHA256 (the file served at download_url was byte-identical on verified_on). verified_on "
            "is the date of that check, not the date the raw file was retrieved; an unknown retrieval_date "
            "stays null."
        ),
        "sources": entries,
    }
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def build_mapping(sources: list[Source], profiles: dict[str, list[FieldProfile]]) -> pd.DataFrame:
    by_name = {s.name: s for s in sources}
    profile_lookup = {(s.name, p.name): p for s in sources for p in profiles[s.name]}
    used: set[tuple[str, str]] = set()
    rows = []

    def observed(source_name: str, column: str) -> str:
        p = profile_lookup[(source_name, column)]
        rows_total = by_name[source_name].frame.shape[0]
        return (f"non-empty {p.nonempty}/{rows_total}; strict numeric {p.numeric}; "
                f"comma-grouped numeric {p.grouped_numeric}; dd/mm/yyyy {p.dmy_dates}; {p.kind}")

    for canonical, headers, note in MAPPING_PROPOSALS:
        present = {name: header for name, header in headers.items() if name in by_name}
        for name, header in present.items():
            if header not in by_name[name].frame.columns:
                raise InventoryError(f"mapping proposal {canonical!r}: {header!r} not found in {name}")
        if len(present) == 1:
            relationship = "single_release_only"
        elif len(set(present.values())) == 1:
            relationship = "same_label_all_releases"
        else:
            relationship = "label_differs_between_releases"
        for name in sorted(present):
            header = present[name]
            unit, basis = label_unit_and_basis(header)
            used.add((name, header))
            rows.append([canonical, by_name[name].path.name, list(by_name[name].frame.columns).index(header),
                         header, unit, basis, relationship, observed(name, header), note, "pending_review"])

    for s in sources:
        for index, header in enumerate(s.frame.columns):
            if (s.name, header) not in used:
                unit, basis = label_unit_and_basis(header)
                rows.append(["", s.path.name, index, header, unit, basis, "unmapped",
                             observed(s.name, header), "No proposal; review required.", "pending_review"])
    return pd.DataFrame(rows, columns=list(MAPPING_COLUMNS))


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--source-links", type=Path, default=DEFAULT_SOURCE_LINKS)
    parser.add_argument("--force-mapping", action="store_true",
                        help="overwrite an existing column mapping file (it may contain review edits)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    try:
        raw_paths = sorted(args.raw_dir.glob("*.csv"))
        if not raw_paths:
            raise InventoryError(f"no CSV files in {relpath(args.raw_dir)}")
        sources = [read_source(path) for path in raw_paths]
        metadata = load_user_metadata(args.metadata, [s.path.name for s in sources])
        links = load_verified_links(args.source_links, sources, metadata)
        profiles = {
            s.name: [profile_field(i, col, s.frame[col].tolist()) for i, col in enumerate(s.frame.columns)]
            for s in sources
        }
        mapping = build_mapping(sources, profiles)
    except InventoryError as exc:
        print(f"inventory failed: {exc}", file=sys.stderr)
        return 1

    write_text(args.manifest, build_manifest(sources, metadata, args.metadata, links, args.source_links))
    write_text(args.inventory, build_inventory(sources, metadata, profiles, links))
    if args.mapping.exists() and not args.force_mapping:
        print(f"kept existing {relpath(args.mapping)} (pass --force-mapping to overwrite)")
    else:
        write_text(args.mapping, mapping.to_csv(index=False, lineterminator="\n"))
        print(f"wrote {relpath(args.mapping)} ({len(mapping)} rows)")

    for s in sources:
        missing = [f for f in METADATA_FIELDS if metadata[s.path.name][f] is None]
        print(f"{s.path.name}: {s.frame.shape[0]} rows x {s.frame.shape[1]} columns, "
              f"delimiter {s.delimiter!r}, BOM {s.has_bom}, sha256 {s.sha256}")
        if missing:
            print(f"  user metadata not supplied: {', '.join(missing)}")
    print(f"wrote {relpath(args.manifest)}")
    print(f"wrote {relpath(args.inventory)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
