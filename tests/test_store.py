"""M2 gate: storage that keeps a symbol's identity across scans.

The gate is deliberately blunt: indexing unchanged code twice must produce
exactly nothing. If it does not, one of the identity traps is live, and every
drift report built on top of this will be noise rather than signal.
"""

from __future__ import annotations

import shutil
import types
from pathlib import Path

import pytest

from spanda.extract import plan_scan, stream_records
from spanda.gaps import load_patterns
from spanda.store import (Index, merge_duplicate_definitions, path_module,
                          symbol_key)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def index_once(db: Path, source: Path) -> int:
    plan = plan_scan(source)
    patterns = load_patterns()
    with Index(db) as index:
        scan_id = index.begin_scan(plan.root, plan.skipped_count)
        for record in stream_records(plan):
            index.write_record(scan_id, record, patterns)
        index.commit()
        index.finish_scan(scan_id)
    return scan_id


@pytest.fixture
def workspace(tmp_path):
    """A throwaway copy of the fixture codebase, safe to edit."""
    source = tmp_path / "code"
    shutil.copytree(FIXTURES, source)
    return source, tmp_path / "index.db"


# -- identity --------------------------------------------------------------

def test_path_module_shapes():
    assert path_module("app/models/order.py") == "app.models.order"
    assert path_module("sample_pkg/__init__.py") == "sample_pkg"
    assert path_module("run.py") == "run"


def test_symbol_key_excludes_line_and_content():
    """A symbol that moves down the file is still the same symbol."""
    a = symbol_key("app/models.py", "Order.total", "method")
    b = symbol_key("app/models.py", "Order.total", "method")
    assert a == b == "app.models.Order.total|method"


def test_symbol_key_distinguishes_same_named_scripts():
    """Two run.py files in different directories are not one symbol."""
    assert (symbol_key("scripts/run.py", "main", "function")
            != symbol_key("tools/run.py", "main", "function"))


def test_duplicate_definitions_merge_rather_than_collide():
    """A name defined once per platform branch cannot be split into stable
    identities, so it becomes one symbol that says how many times it appears."""
    def definition(line, sig_hash):
        return {"qualname": "find_library", "kind": "function",
                "lines": [line, line + 2], "content_hash": f"sha256:c{line}",
                "signature_hash": sig_hash, "docstring": None}

    merged = merge_duplicate_definitions(
        [definition(10, "sha256:s1"), definition(20, "sha256:s1"),
         definition(30, "sha256:s2")])
    assert len(merged) == 1
    assert merged[0]["definition_count"] == 3
    assert merged[0]["signature_varies"] is True
    assert merged[0]["lines"] == [10, 32]


# -- the gate --------------------------------------------------------------

def test_indexing_unchanged_code_twice_produces_zero_drift(workspace):
    source, db = workspace
    index_once(db, source)
    index_once(db, source)

    with Index(db) as index:
        rows = index.connection.execute(
            "SELECT COUNT(*) total,"
            " SUM(first_seen_scan_id = 2) added,"
            " SUM(last_seen_scan_id < 2) removed,"
            " COUNT(DISTINCT uuid) uuids,"
            " COUNT(DISTINCT symbol_key) keys FROM symbols").fetchone()
        versions = index.connection.execute(
            "SELECT COUNT(*) c FROM symbol_versions WHERE scan_id = 2").fetchone()

    assert rows["total"] == 49
    assert rows["added"] == 0, "a re-index of unchanged code invented new symbols"
    assert rows["removed"] == 0, "a re-index of unchanged code lost symbols"
    assert rows["uuids"] == rows["keys"] == 49
    assert versions["c"] == 0, "unchanged symbols must not write version rows"


def test_uuids_survive_a_reindex(workspace):
    source, db = workspace
    index_once(db, source)
    with Index(db) as index:
        before = {r["symbol_key"]: r["uuid"] for r in
                  index.connection.execute("SELECT symbol_key, uuid FROM symbols")}
    index_once(db, source)
    with Index(db) as index:
        after = {r["symbol_key"]: r["uuid"] for r in
                 index.connection.execute("SELECT symbol_key, uuid FROM symbols")}
    assert before == after


# -- change detection ------------------------------------------------------

def test_addition_and_removal_are_detected(workspace):
    source, db = workspace
    index_once(db, source)
    (source / "sample_pkg" / "helpers.py").write_text(
        '__all__ = ["kept"]\n\n\ndef kept():\n    return 1\n')
    (source / "sample_pkg" / "added.py").write_text("def brand_new():\n    return 2\n")
    index_once(db, source)

    with Index(db) as index:
        added = {r["qualname"] for r in index.connection.execute(
            "SELECT qualname FROM symbols WHERE first_seen_scan_id = 2")}
        missing = {r["qualname"] for r in index.missing_since(2)}

    assert "brand_new" in added
    assert {"format_currency", "slugify", "_internal_only"} <= missing


def test_shape_change_is_distinguishable_from_internal_change(workspace):
    source, db = workspace
    index_once(db, source)

    helpers = source / "sample_pkg" / "helpers.py"
    text = helpers.read_text()
    text = text.replace('def format_currency(amount, currency: str = "INR") -> str:',
                        'def format_currency(amount, currency: str = "INR", *, pad: bool = False) -> str:')
    text = text.replace('return text.strip().lower().replace(" ", "-")',
                        'return text.strip().casefold().replace(" ", "-")')
    helpers.write_text(text)
    index_once(db, source)

    with Index(db) as index:
        rows = {r["qualname"]: r for r in index.connection.execute("""
            SELECT s.qualname, v1.signature_hash s1, v2.signature_hash s2,
                   v1.content_hash c1, v2.content_hash c2
            FROM symbols s
            JOIN symbol_versions v1 ON v1.symbol_uuid = s.uuid AND v1.scan_id = 1
            JOIN symbol_versions v2 ON v2.symbol_uuid = s.uuid AND v2.scan_id = 2
        """)}

    shape = rows["format_currency"]
    assert shape["s1"] != shape["s2"], "an added parameter must be a shape change"

    internal = rows["slugify"]
    assert internal["s1"] == internal["s2"], "a body edit must not read as a shape change"
    assert internal["c1"] != internal["c2"], "a body edit must still register"


def test_history_answers_questions_about_older_scans(workspace):
    """Stage 6 diffs arbitrary scan pairs, which the current-values-only
    schema could not do: scan 2 would overwrite scan 1's hashes."""
    source, db = workspace
    index_once(db, source)
    helpers = source / "sample_pkg" / "helpers.py"
    helpers.write_text(helpers.read_text().replace(
        "def slugify(text: str) -> str:", "def slugify(text: str, sep: str = '-') -> str:"))
    index_once(db, source)
    index_once(db, source)  # third scan, unchanged again

    with Index(db) as index:
        row = index.connection.execute(
            "SELECT uuid FROM symbols WHERE qualname = 'slugify'").fetchone()
        at_one = index.version_at(row["uuid"], 1)
        at_three = index.version_at(row["uuid"], 3)

    assert at_one["scan_id"] == 1
    assert "sep" not in at_one["canonical_signature"]
    # No row was written for scan 3; the scan-2 version is still what is true.
    assert at_three["scan_id"] == 2
    assert "sep" in at_three["canonical_signature"]


# -- streaming -------------------------------------------------------------

def test_extraction_streams_rather_than_accumulating():
    """Peak memory must depend on the largest file, not the codebase size."""
    plan = plan_scan(FIXTURES)
    stream = stream_records(plan)
    assert isinstance(stream, types.GeneratorType)
    first = next(stream)
    assert first["file"], "a record is available before the codebase is fully parsed"


def test_unparseable_file_is_recorded_in_the_index(workspace):
    source, db = workspace
    index_once(db, source)
    with Index(db) as index:
        row = index.connection.execute(
            "SELECT * FROM files WHERE file_path LIKE '%broken.py'").fetchone()
        scan = index.scan(1)
    assert row["parse_status"] == "syntax_error"
    assert row["parse_error_line"] == 10
    assert scan["unparseable_files"] == 1
    assert scan["completed"] == 1
