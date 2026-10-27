"""Propose candidate project pairs between the two releases for manual review.

Inputs (read only):
    data/processed/observations.csv   from src/prepare.py; observation_id must be unique
    data/manual/scope_review.csv      every row must have include == "true" or "false"

Outputs:
    data/manual/project_crosswalk.csv   one row per candidate pair with decision "needs_review"; written only
                                        when absent (or with --force-crosswalk), otherwise validated and kept
    outputs/match_review.csv            review table: candidate and crosswalk rows with old/new names, dates,
                                        DCA tokens, scope and schedule narratives side by side
    outputs/match_review.md             readable review with rules, counts, cardinality and checks
    outputs/unmatched_observations.csv  in-scope observations without a confirmed pair, with neutral labels

Candidates come from exact project IDs and exact or normalised names; fuzzy name and description
similarity add suggestions only. Nothing is confirmed automatically: every proposed row starts as
needs_review and only the reviewer's decision counts. Confirmed pairs must be one-to-one, and the
join of confirmed pairs to observations is checked not to multiply rows. No change between releases
is calculated here.

Usage:
    python src/match.py [--force-crosswalk]
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections import Counter
from pathlib import Path

import duckdb
import pandas as pd

from inventory import PROJECT_ROOT, URL_RE, code, relpath, table, write_text
from prepare import DCA_RATINGS, PARSED, write_csv

DEFAULT_OBSERVATIONS = PROJECT_ROOT / "data" / "processed" / "observations.csv"
DEFAULT_SCOPE_REVIEW = PROJECT_ROOT / "data" / "manual" / "scope_review.csv"
DEFAULT_CROSSWALK = PROJECT_ROOT / "data" / "manual" / "project_crosswalk.csv"
DEFAULT_OUTPUTS_DIR = PROJECT_ROOT / "outputs"

CROSSWALK_COLUMNS = (
    "old_observation_id", "new_observation_id", "canonical_project_id", "decision", "match_method",
    "schedule_comparable", "dca_comparable", "monetary_comparable", "rationale", "evidence_url", "reviewer",
)
PAIR_KEY = ["old_observation_id", "new_observation_id"]
CONFIRMED, REJECTED, NEEDS_REVIEW, SPLIT_MERGE = "confirmed", "rejected", "needs_review", "split_merge"
DECISIONS = (CONFIRMED, REJECTED, NEEDS_REVIEW, SPLIT_MERGE)
TRUE, FALSE = "true", "false"
COMPARABILITY_COLUMNS = ("schedule_comparable", "dca_comparable")

# Candidate methods in reporting order; "manual" marks a pair the reviewer added.
MATCH_METHODS = ("exact_project_id", "exact_name", "normalised_name", "fuzzy_name", "fuzzy_description", "manual")
METHOD_SEPARATOR = "+"
# difflib ratios on normalised text, chosen after inspecting the DfT rows. Fuzzy candidates are
# suggestions for the reviewer and never set a decision.
FUZZY_NAME_MIN = 0.75
FUZZY_DESCRIPTION_MIN = 0.6

OBSERVATION_COLUMNS = (
    "observation_id", "release_year", "source_file", "source_row", "project_id_raw", "project_id",
    "project_name_raw", "department_raw", "annual_report_category_raw", "project_description_raw",
    "start_date_raw", "start_date_status", "end_date_raw", "end_date", "end_date_status",
    "external_dca_raw", "external_dca", "external_dca_status", "sro_dca_raw", "sro_dca", "sro_dca_status",
    "schedule_narrative_raw",
)
# Strict DCA comparison stays within one source field: external-to-external or SRO-to-SRO.
DCA_FIELDS = ("external_dca", "sro_dca")
# Review-table fields shown for both sides, old beside new: (output suffix, frame column).
SIDE_FIELDS = (
    ("release_year", "release_year"),
    ("source_row", "source_row"),
    ("project_id_raw", "project_id_raw"),
    ("project_name", "project_name_raw"),
    ("department", "department_raw"),
    ("category", "annual_report_category_raw"),
    ("scope_include", "scope_include"),
    ("start_date_raw", "start_date_raw"),
    ("start_date_status", "start_date_status"),
    ("end_date_raw", "end_date_raw"),
    ("end_date", "end_date"),
    ("end_date_status", "end_date_status"),
    ("external_dca_raw", "external_dca_raw"),
    ("external_dca_status", "external_dca_status"),
    ("sro_dca_raw", "sro_dca_raw"),
    ("sro_dca_status", "sro_dca_status"),
    ("description", "project_description_raw"),
    ("schedule_narrative", "schedule_narrative_raw"),
)
REVIEW_COLUMNS = (
    "component_id", "cardinality", "old_candidate_pairs", "new_candidate_pairs", *PAIR_KEY, "in_crosswalk",
    "decision", "proposed_by", "match_method", "name_similarity", "description_similarity",
    "proposed_canonical_project_id", *(f"{side}_{name}" for name, _ in SIDE_FIELDS for side in ("old", "new")),
)

NO_CANDIDATE, CANDIDATES_REJECTED, PENDING, SPLIT_MERGE_UNRESOLVED = (
    "no_candidate", "candidates_rejected", "pending_review", "split_merge_unresolved")
# Neutral wording: absence from a release is not evidence of completion, cancellation or a new start.
NEUTRAL_LABELS = {
    NO_CANDIDATE: "not observed in the other release (no candidate under the matching rules)",
    CANDIDATES_REJECTED: "not observed in the other release (all candidates rejected on review)",
    PENDING: "match not yet decided (candidates awaiting review)",
    SPLIT_MERGE_UNRESOLVED: "unresolved split/merge case; excluded from simple paired statistics",
}
UNMATCHED_COLUMNS = (
    "side", "observation_id", "release_year", "other_release_year", "source_row", "project_id_raw", "project_name",
    "category", "match_status", "neutral_label", "candidate_pairs", "candidate_observation_ids",
)


class MatchError(Exception):
    """Raised when an input fails a precondition for matching."""


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise MatchError(f"{relpath(path)} not found")
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False, encoding="utf-8-sig")


def text_key(text: str) -> str:
    """Casefold, turn punctuation into spaces and collapse whitespace; used for comparison only."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.casefold())).strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, text_key(a), text_key(b), autojunk=False).ratio()


# --- inputs -----------------------------------------------------------------

def load_observations(path: Path) -> pd.DataFrame:
    observations = read_csv(path)
    missing = [c for c in OBSERVATION_COLUMNS if c not in observations.columns]
    if missing:
        raise MatchError(f"{relpath(path)}: missing columns {missing!r}; rerun src/prepare.py")
    repeated = sorted(observations.loc[observations["observation_id"].duplicated(), "observation_id"])
    if repeated:
        raise MatchError(f"{relpath(path)}: observation_id not unique {repeated[:5]!r}")
    return observations


def load_scope(path: Path, observations: pd.DataFrame) -> pd.DataFrame:
    scope = read_csv(path)
    missing = [c for c in ("observation_id", "project_name", "include") if c not in scope.columns]
    if missing:
        raise MatchError(f"{relpath(path)}: missing columns {missing!r}")
    repeated = sorted(scope.loc[scope["observation_id"].duplicated(), "observation_id"])
    if repeated:
        raise MatchError(f"{relpath(path)}: observation_id not unique {repeated[:5]!r}")
    names = dict(zip(observations["observation_id"], observations["project_name_raw"]))
    unknown = sorted(set(scope["observation_id"]) - set(names))
    if unknown:
        raise MatchError(f"{relpath(path)}: observation_id not in observations {unknown[:5]!r}")
    changed = sorted(scope.loc[scope["project_name"] != scope["observation_id"].map(names), "observation_id"])
    if changed:
        raise MatchError(f"{relpath(path)}: project_name differs from observations for {changed[:5]!r}")
    unresolved = sorted(scope.loc[~scope["include"].isin((TRUE, FALSE)), "observation_id"])
    if unresolved:
        raise MatchError(f"{relpath(path)}: include is not true/false for {unresolved[:5]!r}; "
                         "complete the scope review first")
    return scope


def build_frame(observations: pd.DataFrame, scope: pd.DataFrame) -> pd.DataFrame:
    """Observations with scope decision; in_sample means the reviewer set include == true."""
    frame = observations[list(OBSERVATION_COLUMNS)].merge(
        scope[["observation_id", "include"]].rename(columns={"include": "scope_include"}),
        on="observation_id", how="left", validate="one_to_one")
    frame["in_scope_review"] = frame["scope_include"].notna()
    frame["scope_include"] = frame["scope_include"].fillna("")
    frame["in_sample"] = frame["scope_include"] == TRUE
    frame["name_exact"] = frame["project_name_raw"].str.strip()
    frame["name_key"] = frame["project_name_raw"].map(text_key)
    return frame


def release_years(frame: pd.DataFrame) -> tuple[str, str]:
    years = sorted(frame["release_year"].unique())
    if len(years) != 2:
        raise MatchError(f"expected exactly two releases, found {years!r}")
    return years[0], years[1]


# --- candidates -------------------------------------------------------------

EXACT_CANDIDATES_SQL = """
SELECT o.observation_id AS old_observation_id, n.observation_id AS new_observation_id, 'exact_project_id' AS method
FROM old_obs o JOIN new_obs n ON o.project_id = n.project_id
WHERE o.project_id <> '' AND (o.in_sample OR n.in_sample)
UNION ALL
SELECT o.observation_id, n.observation_id, 'exact_name'
FROM old_obs o JOIN new_obs n ON o.name_exact = n.name_exact
WHERE o.name_exact <> '' AND (o.in_sample OR n.in_sample)
UNION ALL
SELECT o.observation_id, n.observation_id, 'normalised_name'
FROM old_obs o JOIN new_obs n ON o.name_key = n.name_key
WHERE o.name_key <> '' AND o.name_exact <> n.name_exact AND (o.in_sample OR n.in_sample)
"""


def propose_candidates(frame: pd.DataFrame, old_year: str, new_year: str) -> pd.DataFrame:
    """Candidate pairs with at least one in-sample side; returns PAIR_KEY + match_method.

    Exact project ID and exact/normalised name are searched across every row of the other release,
    so a project that changed department or category is still proposed. Fuzzy suggestions are
    limited to rows in the scope review (the inventory-confirmed DfT rows).
    """
    columns = ["observation_id", "project_id", "name_exact", "name_key", "in_sample"]
    old = frame.loc[frame["release_year"] == old_year]
    new = frame.loc[frame["release_year"] == new_year]
    with duckdb.connect() as con:
        con.register("old_obs", old[columns])
        con.register("new_obs", new[columns])
        found = con.execute(EXACT_CANDIDATES_SQL).df()
    rows = list(found.itertuples(index=False, name=None))

    for a in old[old["in_scope_review"]].itertuples(index=False):
        for b in new[new["in_scope_review"]].itertuples(index=False):
            if not (a.in_sample or b.in_sample):
                continue
            if a.name_key != b.name_key and similarity(a.project_name_raw, b.project_name_raw) >= FUZZY_NAME_MIN:
                rows.append((a.observation_id, b.observation_id, "fuzzy_name"))
            if similarity(a.project_description_raw, b.project_description_raw) >= FUZZY_DESCRIPTION_MIN:
                rows.append((a.observation_id, b.observation_id, "fuzzy_description"))

    methods: dict[tuple[str, str], set[str]] = {}
    for old_id, new_id, method in rows:
        methods.setdefault((old_id, new_id), set()).add(method)
    candidates = pd.DataFrame(
        [(old_id, new_id, METHOD_SEPARATOR.join(m for m in MATCH_METHODS if m in found_methods))
         for (old_id, new_id), found_methods in sorted(methods.items())],
        columns=[*PAIR_KEY, "match_method"], dtype=str)
    return candidates


def build_crosswalk(candidates: pd.DataFrame) -> pd.DataFrame:
    """Initial crosswalk: every candidate awaits review; monetary comparison is off in this version."""
    crosswalk = pd.DataFrame({column: "" for column in CROSSWALK_COLUMNS}, index=candidates.index)
    crosswalk[PAIR_KEY] = candidates[PAIR_KEY]
    crosswalk["decision"] = NEEDS_REVIEW
    crosswalk["match_method"] = candidates["match_method"]
    crosswalk["monetary_comparable"] = FALSE
    return crosswalk[list(CROSSWALK_COLUMNS)].reset_index(drop=True)


# --- crosswalk checks -------------------------------------------------------

def load_crosswalk(path: Path) -> pd.DataFrame:
    crosswalk = read_csv(path)
    if tuple(crosswalk.columns) != CROSSWALK_COLUMNS:
        raise MatchError(f"{relpath(path)}: columns must be exactly {list(CROSSWALK_COLUMNS)!r}, "
                         f"found {list(crosswalk.columns)!r}")
    return crosswalk


def comparability_errors(where: str, row, old: pd.Series, new: pd.Series) -> list[str]:
    """Data preconditions for a confirmed pair flagged comparable. Same date concept, comparable scope and
    verified assessment origin are reviewer judgements recorded in the rationale; these are the checks
    the data can support."""
    errors = []
    flagged = TRUE in (row.schedule_comparable, row.dca_comparable)
    if flagged and not (old["in_sample"] and new["in_sample"]):
        errors.append(f"{where}: comparability can be 'true' only when both observations have scope include = true")
    if row.schedule_comparable == TRUE and not (old["end_date_status"] == new["end_date_status"] == PARSED):
        errors.append(f"{where}: schedule_comparable is 'true' but an end date is not a parsed date")
    same_field = any(
        old[f"{field}_status"] == new[f"{field}_status"] == PARSED
        and old[field] in DCA_RATINGS and new[field] in DCA_RATINGS
        for field in DCA_FIELDS
    )
    if row.dca_comparable == TRUE and not same_field:
        errors.append(f"{where}: dca_comparable is 'true' but no DCA field (external or SRO) has a parsed "
                      "Red/Amber/Green rating in both releases")
    return errors


def validate_crosswalk(crosswalk: pd.DataFrame, frame: pd.DataFrame, old_year: str, new_year: str) -> list[str]:
    """Return every rule violation; an empty list means the crosswalk is usable."""
    errors: list[str] = []
    release = dict(zip(frame["observation_id"], frame["release_year"]))
    info = frame.set_index("observation_id")
    for line, row in enumerate(crosswalk.itertuples(index=False), start=2):
        where = f"line {line} ({row.old_observation_id} -> {row.new_observation_id})"
        for column, year in (("old_observation_id", old_year), ("new_observation_id", new_year)):
            observation_id = getattr(row, column)
            if observation_id not in release:
                errors.append(f"{where}: {column} not in observations")
            elif release[observation_id] != year:
                errors.append(f"{where}: {column} is from {release[observation_id]}, expected {year}")
        if row.decision not in DECISIONS:
            errors.append(f"{where}: decision {row.decision!r} not one of {'/'.join(DECISIONS)}")
        methods = row.match_method.split(METHOD_SEPARATOR)
        if not all(m in MATCH_METHODS for m in methods) or len(methods) != len(set(methods)):
            errors.append(f"{where}: match_method {row.match_method!r} is not a '+'-joined list of "
                          f"{'/'.join(MATCH_METHODS)}")
        if row.monetary_comparable != FALSE:
            errors.append(f"{where}: monetary_comparable must be 'false' in this version "
                          "(methodology sections 4.2 and 5.7)")
        for column in COMPARABILITY_COLUMNS:
            value = getattr(row, column)
            if value not in (TRUE, FALSE, ""):
                errors.append(f"{where}: {column} {value!r} must be 'true', 'false' or blank")
            elif row.decision == CONFIRMED and value == "":
                errors.append(f"{where}: {column} must be 'true' or 'false' for a confirmed pair")
            elif row.decision != CONFIRMED and value == TRUE:
                errors.append(f"{where}: {column} can be 'true' only for a confirmed pair")
        if row.decision in (CONFIRMED, REJECTED, SPLIT_MERGE):
            if not row.rationale.strip():
                errors.append(f"{where}: rationale required for decision {row.decision!r}")
            if not row.reviewer.strip():
                errors.append(f"{where}: reviewer required for decision {row.decision!r}")
        if row.decision == CONFIRMED:
            if not row.canonical_project_id.strip():
                errors.append(f"{where}: canonical_project_id required for a confirmed pair")
            urls = [u for u in re.split(r"[\s;]+", row.evidence_url) if u]
            if not urls or not all(URL_RE.fullmatch(u) for u in urls):
                errors.append(f"{where}: evidence_url must give one or more http(s) URLs for a confirmed pair")
            if row.old_observation_id in release and row.new_observation_id in release:
                errors += comparability_errors(where, row, info.loc[row.old_observation_id],
                                               info.loc[row.new_observation_id])

    repeated = crosswalk[crosswalk.duplicated(PAIR_KEY, keep=False)]
    for old_id, new_id in sorted(set(zip(repeated["old_observation_id"], repeated["new_observation_id"]))):
        errors.append(f"pair {old_id} -> {new_id} appears more than once")
    confirmed = crosswalk[crosswalk["decision"] == CONFIRMED]
    for column in (*PAIR_KEY, "canonical_project_id"):
        for value, n in sorted(Counter(confirmed[column]).items()):
            if n > 1 and value.strip():
                errors.append(f"{column} {value!r} is in {n} confirmed pairs; confirmed pairs must be one-to-one "
                              "(use split_merge for splits and merges)")
    split = crosswalk[crosswalk["decision"] == SPLIT_MERGE]
    for column in PAIR_KEY:
        for value in sorted(set(confirmed[column]) & set(split[column])):
            errors.append(f"{column} {value!r} is in both a confirmed and a split_merge row")
    return errors


CONFIRMED_PAIRS_SQL = """
SELECT x.old_observation_id, x.new_observation_id, x.canonical_project_id, x.match_method,
       x.schedule_comparable, x.dca_comparable, x.monetary_comparable
FROM crosswalk x
JOIN observations o ON o.observation_id = x.old_observation_id
JOIN observations n ON n.observation_id = x.new_observation_id
WHERE x.decision = 'confirmed'
ORDER BY x.old_observation_id, x.new_observation_id
"""


def confirmed_pairs(crosswalk: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    """Confirmed one-to-one pairs joined to both observations, guarded against join multiplication."""
    with duckdb.connect() as con:
        con.register("crosswalk", crosswalk[list(CROSSWALK_COLUMNS)])
        con.register("observations", frame[["observation_id"]])
        pairs = con.execute(CONFIRMED_PAIRS_SQL).df()
    expected = int((crosswalk["decision"] == CONFIRMED).sum())
    if len(pairs) != expected:
        raise MatchError(f"confirmed pairs joined to observations give {len(pairs)} rows, expected {expected}")
    for column in PAIR_KEY:
        if pairs[column].duplicated().any():
            raise MatchError(f"confirmed pairs repeat {column}; join would multiply rows")
    return pairs


# --- review outputs ---------------------------------------------------------

def pair_structure(review: pd.DataFrame) -> pd.DataFrame:
    """Cardinality and connected components over pairs not rejected (candidates and crosswalk rows)."""
    active = review["decision"] != REJECTED
    pairs = review.loc[active, PAIR_KEY]
    old_counts = pairs["old_observation_id"].value_counts()
    new_counts = pairs["new_observation_id"].value_counts()

    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent.setdefault(node, node) != node:
            node = parent[node]
        return node

    for old_id, new_id in zip(pairs["old_observation_id"], pairs["new_observation_id"]):
        parent[find("old:" + old_id)] = find("new:" + new_id)
    roots = [find("old:" + old_id) for old_id in pairs["old_observation_id"]]
    order = {root: f"C{n:03d}" for n, root in enumerate(dict.fromkeys(
        root for _, root in sorted(zip(map(tuple, pairs.values.tolist()), roots))), start=1)}

    structure = pd.DataFrame(index=review.index, columns=[
        "component_id", "cardinality", "old_candidate_pairs", "new_candidate_pairs"], dtype=str).fillna("")
    for index, root in zip(pairs.index, roots):
        n_new = int(old_counts[review.at[index, "old_observation_id"]])
        n_old = int(new_counts[review.at[index, "new_observation_id"]])
        shape = {(True, True): "one_to_one", (False, True): "one_to_many",
                 (True, False): "many_to_one", (False, False): "many_to_many"}[(n_new == 1, n_old == 1)]
        structure.loc[index] = [order[root], shape, str(n_new), str(n_old)]
    structure.loc[~active, "cardinality"] = "rejected"
    return structure


def build_review(candidates: pd.DataFrame, crosswalk: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    merged = candidates.rename(columns={"match_method": "proposed_by"}).merge(
        crosswalk, on=PAIR_KEY, how="outer", validate="one_to_one", indicator=True)
    review = pd.DataFrame({
        "old_observation_id": merged["old_observation_id"],
        "new_observation_id": merged["new_observation_id"],
        "in_crosswalk": merged["_merge"].isin(["both", "right_only"]).map({True: TRUE, False: FALSE}),
        "decision": merged["decision"].fillna(""),
        "proposed_by": merged["proposed_by"].fillna(""),
        "match_method": merged["match_method"].fillna(merged["proposed_by"]),
    })
    sides = {}
    for side in ("old", "new"):
        part = frame.set_index("observation_id").loc[review[f"{side}_observation_id"]].reset_index(drop=True)
        sides[side] = part
    review["name_similarity"] = [f"{similarity(a, b):.3f}" for a, b in zip(
        sides["old"]["project_name_raw"], sides["new"]["project_name_raw"])]
    review["description_similarity"] = [f"{similarity(a, b):.3f}" for a, b in zip(
        sides["old"]["project_description_raw"], sides["new"]["project_description_raw"])]
    same_id = (sides["old"]["project_id"] == sides["new"]["project_id"]) & (sides["old"]["project_id"] != "")
    review["proposed_canonical_project_id"] = sides["old"]["project_id"].where(same_id, "")
    for name, column in SIDE_FIELDS:
        for side in ("old", "new"):
            review[f"{side}_{name}"] = sides[side][column].astype(str)
    review = pd.concat([pair_structure(review), review], axis=1)
    review["_sort"] = review["component_id"].eq("")
    review = review.sort_values(["_sort", "component_id", *PAIR_KEY]).reset_index(drop=True)
    return review[list(REVIEW_COLUMNS)]


def build_unmatched(frame: pd.DataFrame, review: pd.DataFrame, old_year: str, new_year: str) -> pd.DataFrame:
    """In-sample observations with no confirmed pair, labelled without implying why they are absent."""
    rows = []
    for side, year, other_year in (("old", old_year, new_year), ("new", new_year, old_year)):
        other = "new" if side == "old" else "old"
        sample = frame[frame["in_sample"] & (frame["release_year"] == year)].sort_values("observation_id")
        for obs in sample.itertuples(index=False):
            linked = review[review[f"{side}_observation_id"] == obs.observation_id]
            decisions = set(linked["decision"].replace("", NEEDS_REVIEW))
            if CONFIRMED in decisions:
                continue
            if SPLIT_MERGE in decisions:
                status = SPLIT_MERGE_UNRESOLVED
            elif NEEDS_REVIEW in decisions:
                status = PENDING
            elif decisions:
                status = CANDIDATES_REJECTED
            else:
                status = NO_CANDIDATE
            rows.append({
                "side": side, "observation_id": obs.observation_id, "release_year": year,
                "other_release_year": other_year, "source_row": obs.source_row, "project_id_raw": obs.project_id_raw,
                "project_name": obs.project_name_raw, "category": obs.annual_report_category_raw,
                "match_status": status, "neutral_label": NEUTRAL_LABELS[status], "candidate_pairs": str(len(linked)),
                "candidate_observation_ids": ";".join(sorted(linked[f"{other}_observation_id"])),
            })
    return pd.DataFrame(rows, columns=list(UNMATCHED_COLUMNS), dtype=str)


def quote(text: str) -> list[str]:
    if not text.strip():
        return ["> *(blank in source)*"]
    return ["> " + line if line.strip() else ">" for line in text.splitlines()]


def build_report(frame, candidates, crosswalk, pairs, review, unmatched, old_year, new_year) -> str:
    lines = [
        "# Candidate matching review", "",
        "Generated by `src/match.py`. Candidates are proposals only: no pair is confirmed automatically, and "
        "decisions are taken solely from `data/manual/project_crosswalk.csv`. No change between releases "
        "(dates, DCA or money) is calculated at this stage.", "",
        "## Sample", "",
    ]
    lines += table(["Release", "Role", "Observations", "In scope review (DfT)", "In sample (include = true)"], [
        [year, role, int((frame["release_year"] == year).sum()),
         int((frame["in_scope_review"] & (frame["release_year"] == year)).sum()),
         int((frame["in_sample"] & (frame["release_year"] == year)).sum())]
        for year, role in ((old_year, "old"), (new_year, "new"))
    ])

    lines += ["## Candidate rules", "",
              "A pair is proposed only if at least one side is in the sample.", "",
              "- `exact_project_id`: identical published project ID (`GMPP ID` / `Major Projects ID`), "
              "searched across every row of the other release.",
              "- `exact_name`: identical project name after trimming surrounding whitespace, every row.",
              "- `normalised_name`: names equal after casefolding and replacing punctuation and repeated "
              "whitespace, but not exactly equal; every row.",
              f"- `fuzzy_name`: difflib ratio of normalised names >= {FUZZY_NAME_MIN} (names not already equal), "
              "scope-review rows only. Suggestion only.",
              f"- `fuzzy_description`: difflib ratio of normalised descriptions >= {FUZZY_DESCRIPTION_MIN}, "
              "scope-review rows only. Suggestion only.",
              "", "Identical IDs or names do not show unchanged scope (methodology section 5.2).", ""]

    lines += ["## Crosswalk", "",
              "`data/manual/project_crosswalk.csv` is written only when absent (or with `--force-crosswalk`); "
              "new rows start as `needs_review` with `monetary_comparable` = `false` and blank decision fields. "
              "An existing file is validated and never changed.", ""]
    lines += table(["Decision", "Rows"], [[d, int((crosswalk["decision"] == d).sum())] for d in DECISIONS])
    lines += ["Validation: passed (allowed decisions, release sides, unique pairs, `monetary_comparable` = "
              "`false`, comparability only on confirmed pairs, rationale/reviewer/evidence on decided rows, "
              "one-to-one confirmed pairs).",
              f"Confirmed pairs joined to observations: {len(pairs)} rows for "
              f"{int((crosswalk['decision'] == CONFIRMED).sum())} confirmed crosswalk rows (no multiplication).", ""]
    in_crosswalk = set(zip(crosswalk["old_observation_id"], crosswalk["new_observation_id"]))
    proposed = set(zip(candidates["old_observation_id"], candidates["new_observation_id"]))
    lines += [f"Current candidates missing from the crosswalk: {len(proposed - in_crosswalk)}"
              + "".join(f"\n- {a} -> {b}" for a, b in sorted(proposed - in_crosswalk)),
              f"Crosswalk rows that are not current candidates (manual or stale): {len(in_crosswalk - proposed)}"
              + "".join(f"\n- {a} -> {b}" for a, b in sorted(in_crosswalk - proposed)), ""]

    confirmed = crosswalk[crosswalk["decision"] == CONFIRMED]
    schedule = confirmed["schedule_comparable"] == TRUE
    dca = confirmed["dca_comparable"] == TRUE
    lines += ["## Coverage", "",
              "Confirmed pairs are one-to-one, so each pair is one project observed in both releases. "
              "Candidate-row counts above are not project counts.", ""]
    lines += table(["Measure", "Count"], [
        [f"Included observations, {old_year}", int((frame["in_sample"] & (frame["release_year"] == old_year)).sum())],
        [f"Included observations, {new_year}", int((frame["in_sample"] & (frame["release_year"] == new_year)).sum())],
        ["Confirmed identity pairs", len(confirmed)],
        ["Schedule-comparable pairs", int(schedule.sum())],
        ["Strict DCA-comparable pairs", int(dca.sum())],
        ["Pairs comparable on both schedule and DCA", int((schedule & dca).sum())],
        [f"Included {old_year} observations without a confirmed pair", int((unmatched["side"] == "old").sum())],
        [f"Included {new_year} observations without a confirmed pair", int((unmatched["side"] == "new").sum())],
        ["Monetary-comparable pairs (disabled in this version)", int((crosswalk["monetary_comparable"] == TRUE).sum())],
    ])

    lines += ["## Candidates by method", "",
              "A pair can be proposed by several methods, so the counts overlap.", ""]
    lines += table(["Method", "Pairs"], [
        [code(m), int(candidates["match_method"].str.split(METHOD_SEPARATOR).map(lambda ms, m=m: m in ms).sum())]
        for m in MATCH_METHODS if m != "manual"
    ])
    lines += [f"Total candidate pairs: {len(candidates)}", ""]

    lines += ["## Cardinality", "",
              "Counted over pairs not rejected. A component is a set of observations connected by such pairs; "
              "any component with more than one pair needs a one-to-one decision, a rejection or `split_merge`.", ""]
    lines += table(["Cardinality", "Pairs"], [[c, n] for c, n in sorted(Counter(review["cardinality"]).items())])
    multi = review[review["component_id"].ne("")].groupby("component_id").filter(lambda g: len(g) > 1)
    if multi.empty:
        lines += ["No component has more than one pair.", ""]
    else:
        lines += table(["Component", "Pair", "Cardinality", "Method", "Old name", "New name"], [
            [r.component_id, f"{r.old_observation_id} -> {r.new_observation_id}", r.cardinality,
             code(r.match_method), code(r.old_project_name), code(r.new_project_name)]
            for r in multi.itertuples(index=False)
        ])

    lines += ["## Review table", "",
              "Full columns are in `outputs/match_review.csv`. Similarities are difflib ratios of normalised text.", ""]
    lines += table(["Component", "Cardinality", "Old", "New", "Method", "Name sim.", "Desc. sim.", "Decision",
                    "Old name", "New name"], [
        [r.component_id or "-", r.cardinality, r.old_observation_id, r.new_observation_id, code(r.match_method),
         r.name_similarity, r.description_similarity, r.decision or "*(not in crosswalk)*",
         code(r.old_project_name), code(r.new_project_name)]
        for r in review.itertuples(index=False)
    ])

    boundary = review[(review["old_scope_include"] != TRUE) | (review["new_scope_include"] != TRUE)]
    lines += ["## Pairs crossing the scope boundary", ""]
    if boundary.empty:
        lines += ["None: every pair has both sides in the sample.", ""]
    else:
        lines += table(["Old", "New", "Old include", "New include", "Old category", "New category"], [
            [r.old_observation_id, r.new_observation_id, r.old_scope_include or "*(not reviewed)*",
             r.new_scope_include or "*(not reviewed)*", code(r.old_category), code(r.new_category)]
            for r in boundary.itertuples(index=False)
        ])

    lines += ["## In-sample observations without a confirmed pair", "",
              "Listed in `outputs/unmatched_observations.csv`. Absence from a release does not show completion, "
              "cancellation or a new start (methodology sections 3 and 5.6).", ""]
    lines += table(["Side", "Status", "Neutral label", "Observations"], [
        [side, status, NEUTRAL_LABELS[status], n]
        for (side, status), n in sorted(Counter(zip(unmatched["side"], unmatched["match_status"])).items())
    ])
    rest = unmatched[unmatched["match_status"] != PENDING]
    if not rest.empty:
        lines += table(["Side", "Observation", "Project ID", "Name", "Status"], [
            [r.side, r.observation_id, code(r.project_id_raw), code(r.project_name), r.match_status]
            for r in rest.itertuples(index=False)
        ])

    lines += ["## Pair details", "",
              "Raw tokens are shown as published; parsed values come from `src/prepare.py`. "
              "Dates are shown side by side only; no movement is calculated.", ""]
    for r in review.itertuples(index=False):
        lines += [f"### {r.component_id or 'rejected'}: {r.old_observation_id} -> {r.new_observation_id}", "",
                  f"Method {code(r.match_method)}; cardinality {r.cardinality}; "
                  f"decision {r.decision or '*(not in crosswalk)*'}; name similarity {r.name_similarity}; "
                  f"description similarity {r.description_similarity}.", ""]
        lines += table(["Field", f"Old ({r.old_release_year}, row {r.old_source_row})",
                        f"New ({r.new_release_year}, row {r.new_source_row})"], [
            ["Project ID", code(r.old_project_id_raw), code(r.new_project_id_raw)],
            ["Project name", code(r.old_project_name), code(r.new_project_name)],
            ["Department", code(r.old_department), code(r.new_department)],
            ["Category", code(r.old_category), code(r.new_category)],
            ["Scope include", r.old_scope_include or "*(not reviewed)*", r.new_scope_include or "*(not reviewed)*"],
            ["Start date", f"{code(r.old_start_date_raw)} ({r.old_start_date_status})",
             f"{code(r.new_start_date_raw)} ({r.new_start_date_status})"],
            ["End date", f"{code(r.old_end_date_raw)} ({r.old_end_date_status})",
             f"{code(r.new_end_date_raw)} ({r.new_end_date_status})"],
            ["External (IPA) DCA", f"{code(r.old_external_dca_raw)} ({r.old_external_dca_status})",
             f"{code(r.new_external_dca_raw)} ({r.new_external_dca_status})"],
            ["SRO DCA", f"{code(r.old_sro_dca_raw)} ({r.old_sro_dca_status})",
             f"{code(r.new_sro_dca_raw)} ({r.new_sro_dca_status})"],
        ])
        for label, name in (("Description", "description"), ("Schedule narrative", "schedule_narrative")):
            for side, year in (("old", r.old_release_year), ("new", r.new_release_year)):
                lines += [f"**{label}, {side} ({year}):**", "", *quote(getattr(r, f"{side}_{name}")), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--observations", type=Path, default=DEFAULT_OBSERVATIONS)
    parser.add_argument("--scope-review", type=Path, default=DEFAULT_SCOPE_REVIEW)
    parser.add_argument("--crosswalk", type=Path, default=DEFAULT_CROSSWALK)
    parser.add_argument("--outputs-dir", type=Path, default=DEFAULT_OUTPUTS_DIR)
    parser.add_argument("--force-crosswalk", action="store_true",
                        help="overwrite an existing crosswalk (it may contain reviewer decisions)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    try:
        observations = load_observations(args.observations)
        frame = build_frame(observations, load_scope(args.scope_review, observations))
        old_year, new_year = release_years(frame)
        candidates = propose_candidates(frame, old_year, new_year)
        write_crosswalk = args.force_crosswalk or not args.crosswalk.exists()
        crosswalk = build_crosswalk(candidates) if write_crosswalk else load_crosswalk(args.crosswalk)
        errors = validate_crosswalk(crosswalk, frame, old_year, new_year)
        if errors:
            raise MatchError(f"{relpath(args.crosswalk)} failed validation:\n  " + "\n  ".join(errors))
        pairs = confirmed_pairs(crosswalk, frame)
        review = build_review(candidates, crosswalk, frame)
        unmatched = build_unmatched(frame, review, old_year, new_year)
    except MatchError as exc:
        print(f"match failed: {exc}", file=sys.stderr)
        return 1

    if write_crosswalk:
        write_csv(crosswalk, args.crosswalk)
        print(f"wrote {relpath(args.crosswalk)} ({len(crosswalk)} rows)")
    else:
        print(f"kept existing {relpath(args.crosswalk)} (pass --force-crosswalk to overwrite)")
    write_csv(review, args.outputs_dir / "match_review.csv")
    write_csv(unmatched, args.outputs_dir / "unmatched_observations.csv")
    write_text(args.outputs_dir / "match_review.md", build_report(
        frame, candidates, crosswalk, pairs, review, unmatched, old_year, new_year))
    print(f"candidates: {len(candidates)}; review rows: {len(review)}; confirmed pairs: {len(pairs)}; "
          f"unmatched in-sample observations: {len(unmatched)}")
    for name, n in sorted(Counter(review["cardinality"]).items()):
        print(f"  {name}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
