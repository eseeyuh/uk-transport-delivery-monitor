"""Tests for the verified-link provenance in src/inventory.py."""

import json
from pathlib import Path

import pandas as pd
import pytest

import inventory as inv

URL = "https://www.gov.uk/example"


def source(name: str, sha256: str) -> inv.Source:
    return inv.Source(path=Path(name), sha256=sha256, byte_size=0, has_bom=False, delimiter=",",
                      frame=pd.DataFrame(), embedded_newline_cells=0)


def write_links(path: Path, **overrides) -> Path:
    row = {"file_name": "a.csv", "release_year": "2024-25", "source_page_url": URL, "download_url": f"{URL}/a.csv",
           "report_url": f"{URL}/report", "sha256": "abc", "verified_on": "2026-10-22", "verification": "test"}
    row.update(overrides)
    pd.DataFrame([row], columns=list(inv.LINK_COLUMNS)).to_csv(path, index=False, lineterminator="\n")
    return path


def metadata(release_year="2024-25"):
    return {"a.csv": {"release_year": release_year, "snapshot_date": "2025-03-31", "retrieval_date": None,
                      "source_page_url": None, "download_url": None}}


def test_links_are_kept_when_the_hash_matches(tmp_path):
    links = inv.load_verified_links(write_links(tmp_path / "links.csv"), [source("a.csv", "abc")], metadata())
    assert links["a.csv"]["download_url"] == f"{URL}/a.csv"
    assert links["a.csv"]["verified_on"] == "2026-10-22"


def test_a_hash_mismatch_is_rejected(tmp_path):
    with pytest.raises(inv.InventoryError, match="SHA256"):
        inv.load_verified_links(write_links(tmp_path / "links.csv"), [source("a.csv", "other")], metadata())


def test_a_release_year_conflict_is_rejected(tmp_path):
    with pytest.raises(inv.InventoryError, match="release_year"):
        inv.load_verified_links(write_links(tmp_path / "links.csv"), [source("a.csv", "abc")], metadata("2025-26"))


def test_verified_links_do_not_fill_user_metadata(tmp_path):
    sources = [source("a.csv", "abc")]
    links = inv.load_verified_links(write_links(tmp_path / "links.csv"), sources, metadata())
    manifest = inv.build_manifest(sources, metadata(), tmp_path / "meta.csv", links, tmp_path / "links.csv")
    entry = json.loads(manifest)["sources"][0]
    assert entry["download_url"] is None and entry["retrieval_date"] is None
    assert entry["verified_links"]["download_url"] == f"{URL}/a.csv"
    assert entry["snapshot_date"] == "2025-03-31"


def test_missing_links_file_leaves_links_unset(tmp_path):
    assert inv.load_verified_links(tmp_path / "absent.csv", [source("a.csv", "abc")], metadata()) == {"a.csv": None}
