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
from spanda.store import (SCHEMA_VERSION, Index, IndexError_, db_path, ensure_index_dir,
                          merge_duplicate_definitions, path_module,
                          prepare_db_path, symbol_key)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def index_once(db: Path, source: Path) -> int:
    plan = plan_scan(source)
    patterns = load_patterns()
    with Index(db, codebase_root=source) as index:
        scan_id = index.begin_scan(plan.root, plan.skipped_count)
        for record in stream_records(plan):
            index.write_record(scan_id, record, patterns)
        # finish_scan commits: a scan is one transaction, all or nothing.
        index.finish_scan(scan_id)
    return scan_id


#: Never copy an existing index into a test workspace; the copy would claim
#: to describe the original directory.
IGNORE_INDEXES = shutil.ignore_patterns(".spanda")


def fresh_copy(destination: Path) -> Path:
    shutil.copytree(FIXTURES, destination, ignore=IGNORE_INDEXES)
    return destination


@pytest.fixture
def workspace(tmp_path):
    """A throwaway copy of the fixture codebase, safe to edit."""
    return fresh_copy(tmp_path / "code"), tmp_path / "index.db"


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
                "signature_hash": sig_hash, "docstring": None,
                "body_hash": f"sha256:b{line}", "loop_depth": 0}

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

    assert rows["total"] == 98
    assert rows["added"] == 0, "a re-index of unchanged code invented new symbols"
    assert rows["removed"] == 0, "a re-index of unchanged code lost symbols"
    assert rows["uuids"] == rows["keys"] == 98
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
        missing = {r["qualname"] for r in index.missing_at(2)}

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
        problem = index.connection.execute(
            "SELECT * FROM scan_problems WHERE scan_id = 1").fetchone()
        scan = index.scan(1)
    assert row["parse_status"] == "syntax_error"
    assert problem["line"] in (9, 10)  # varies by interpreter version
    assert scan["unparseable_files"] == 1
    assert scan["completed"] == 1


def test_unchanged_files_do_not_write_a_row_every_scan(tmp_path):
    """Storing the full file listing per scan cost 292,020 rows for 1,097
    files across 425 scans. Only changes are recorded now."""
    source = tmp_path / "code"
    fresh_copy(source)
    db = prepare_db_path(source)
    index_once(db, source)
    index_once(db, source)
    index_once(db, source)

    with Index(db) as index:
        files = index.connection.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
        versions = index.connection.execute(
            "SELECT COUNT(*) c FROM file_versions").fetchone()["c"]
        later = index.connection.execute(
            "SELECT COUNT(*) c FROM file_versions WHERE scan_id > 1").fetchone()["c"]

    assert files == 21, "one row per file, not per file per scan"
    assert versions == 21, "every file recorded once, at the scan that found it"
    assert later == 0, "three scans of unchanged code add nothing"


def test_a_changed_file_writes_one_new_version(tmp_path):
    source = tmp_path / "code"
    fresh_copy(source)
    db = prepare_db_path(source)
    index_once(db, source)
    helpers = source / "sample_pkg" / "helpers.py"
    helpers.write_text(helpers.read_text() + "\n\ndef added():\n    return 1\n")
    index_once(db, source)

    with Index(db) as index:
        changed = index.connection.execute(
            "SELECT file_path FROM file_versions WHERE scan_id = 2").fetchall()
    assert [r["file_path"] for r in changed] == ["sample_pkg/helpers.py"]


# -- where the index lives -------------------------------------------------

def test_index_lives_beside_the_code_it_describes(tmp_path):
    (tmp_path / "app.py").write_text("def f(): pass\n")
    db = prepare_db_path(tmp_path)
    assert db == tmp_path / ".spanda" / "index.db"


def test_one_index_per_codebase(tmp_path):
    """Several files would each claim to describe the repository, with UUIDs
    and version history trapped inside whichever one you happened to open."""
    (tmp_path / "app.py").write_text("def f(): pass\n")
    first = prepare_db_path(tmp_path)
    index_once(first, tmp_path)
    second = prepare_db_path(tmp_path)
    index_once(second, tmp_path)
    assert first == second == db_path(tmp_path)
    assert [p.name for p in (tmp_path / ".spanda").glob("*.db")] == ["index.db"]
    with Index(first) as index:
        assert len(index.scans()) == 2, "both runs land in one history"


def test_index_dir_ignores_itself(tmp_path):
    """Indexes are derived data and must never reach a commit."""
    directory = ensure_index_dir(tmp_path)
    gitignore = directory / ".gitignore"
    assert gitignore.exists()
    assert "*" in gitignore.read_text().splitlines()


def test_history_accumulates_in_one_place(tmp_path):
    """Identity and history stay in one file, so neither can be stranded."""
    (tmp_path / "app.py").write_text("def kept(): pass\n")
    db = prepare_db_path(tmp_path)

    index_once(db, tmp_path)
    with Index(db) as index:
        before = {r["symbol_key"]: r["uuid"] for r in
                  index.connection.execute("SELECT symbol_key, uuid FROM symbols")}

    index_once(db, tmp_path)
    with Index(db) as index:
        after = {r["symbol_key"]: r["uuid"] for r in
                 index.connection.execute("SELECT symbol_key, uuid FROM symbols")}
        scans = index.scans()

    assert before == after
    assert [s["scan_id"] for s in scans] == [1, 2]


# -- the five robustness failures ------------------------------------------

def test_refuses_to_index_a_second_codebase_into_one_file(tmp_path):
    """Otherwise the first project's symbols all report as deleted."""
    a, b = tmp_path / "a", tmp_path / "b"
    for directory, name in ((a, "alpha"), (b, "beta")):
        directory.mkdir()
        (directory / f"{name}.py").write_text(f"def {name}_fn(): pass\n")

    db = prepare_db_path(a)
    index_once(db, a)
    with pytest.raises(IndexError_, match="indexes"):
        Index(db, codebase_root=b)


def test_an_interrupted_scan_leaves_no_trace(tmp_path):
    """A half-written scan is indistinguishable from a mass deletion."""
    source = tmp_path / "code"
    fresh_copy(source)
    db = prepare_db_path(source)
    index_once(db, source)

    plan, patterns = plan_scan(source), load_patterns()
    with pytest.raises(RuntimeError):
        with Index(db, codebase_root=source) as index:
            scan_id = index.begin_scan(plan.root, plan.skipped_count)
            for number, record in enumerate(stream_records(plan)):
                if number == 3:
                    raise RuntimeError("simulated crash")
                index.write_record(scan_id, record, patterns)

    with Index(db) as index:
        assert len(index.scans()) == 1, "the dead scan must not survive"
        assert index.missing_at(1) == [], "nothing may look deleted"


def test_refuses_to_compare_against_an_incomplete_scan(tmp_path):
    source = tmp_path / "code"
    fresh_copy(source)
    db = prepare_db_path(source)
    index_once(db, source)
    with Index(db) as index:
        index.connection.execute("UPDATE scans SET completed = 0 WHERE scan_id = 1")
        with pytest.raises(IndexError_, match="never completed"):
            index.require_complete(1)


def test_deletion_is_derived_not_stored(tmp_path):
    """An is_deleted column would be maintained wrongly once Stage 5 stops
    re-reading unchanged files, and a column that is always false is worse
    than no column because queries come to trust it."""
    source = tmp_path / "code"
    fresh_copy(source)
    db = prepare_db_path(source)
    index_once(db, source)
    with Index(db) as index:
        columns = [r["name"] for r in
                   index.connection.execute("PRAGMA table_info(symbols)")]
    assert "is_deleted" not in columns


def test_a_dirty_tree_is_not_recorded_as_its_commit(tmp_path, monkeypatch):
    """Otherwise a scan of uncommitted work masquerades as a scan of that
    commit, and any later comparison across the two is nonsense."""
    import spanda.store as store
    source = tmp_path / "code"
    fresh_copy(source)
    db = prepare_db_path(source)

    monkeypatch.setattr(store, "git_state", lambda root: ("abc123", True))
    index_once(db, source)
    monkeypatch.setattr(store, "git_state", lambda root: ("abc123", False))
    index_once(db, source)

    with Index(db) as index:
        dirty, clean = index.scans()
    assert dirty["git_commit_hash"] is None
    assert dirty["git_base_commit"] == "abc123"
    assert clean["git_commit_hash"] == "abc123"


def test_refuses_an_index_from_a_different_schema_version(tmp_path):
    (tmp_path / "app.py").write_text("def f(): pass\n")
    db = prepare_db_path(tmp_path)
    index_once(db, tmp_path)
    with Index(db) as index:
        index.connection.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(IndexError_, match="schema version 99"):
        Index(db)


def test_identical_content_produces_an_identical_fingerprint(tmp_path):
    """A cheap, git-independent answer to 'did anything change at all?'"""
    source = tmp_path / "code"
    fresh_copy(source)
    db = prepare_db_path(source)
    index_once(db, source)
    index_once(db, source)
    with Index(db) as index:
        one, two = index.scans()
    assert one["content_fingerprint"] == two["content_fingerprint"]

    (source / "sample_pkg" / "extra.py").write_text("def added(): pass\n")
    index_once(db, source)
    with Index(db) as index:
        three = index.scans()[2]
    assert three["content_fingerprint"] != one["content_fingerprint"]


# -- migrations --------------------------------------------------------------

def _drop_body_hash(path: Path) -> None:
    """Make a schema-11 index look like a schema-10 one."""
    import sqlite3
    connection = sqlite3.connect(path)
    for table in ("symbols", "symbol_versions"):
        connection.execute(f"ALTER TABLE {table} DROP COLUMN body_hash")
    connection.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '10')")
    connection.commit()
    connection.close()


def test_an_older_index_is_brought_forward_not_refused(workspace):
    """Schema 10 to 11 adds one column. The rows written before it keep
    NULL there — a migration cannot hash source it never read — and the
    next scan fills it in for everything it re-reads."""
    workspace, _ = workspace
    from spanda.cli import main
    assert main(["index", str(workspace)]) == 0
    target = db_path(workspace)
    _drop_body_hash(target)

    with Index(target, codebase_root=workspace) as index:
        assert index.migrated_from == 10
        assert index.meta("schema_version") == str(SCHEMA_VERSION)
        # The index remembers it was brought forward; a later reader of a
        # NULL column can find out why without the terminal that saw it.
        steps = index.migrations()
        assert [(m["from"], m["to"]) for m in steps] == [
            (v - 1, v) for v in range(11, SCHEMA_VERSION + 1)]
        assert all(m["when"].startswith("20") for m in steps)
        empty = index.connection.execute(
            "SELECT COUNT(*) FROM symbols WHERE body_hash IS NULL").fetchone()[0]
        assert empty > 0

    assert main(["index", str(workspace)]) == 0
    with Index(target) as index:
        assert index.migrated_from is None
        empty = index.connection.execute(
            "SELECT COUNT(*) FROM symbols WHERE body_hash IS NULL"
            " AND last_seen_scan_id = (SELECT MAX(scan_id) FROM scans)").fetchone()[0]
        assert empty == 0


def test_a_newer_index_is_still_refused(workspace):
    workspace, _ = workspace
    from spanda.cli import main
    assert main(["index", str(workspace)]) == 0
    target = db_path(workspace)
    with Index(target) as index:
        index._set_meta("schema_version", "99")
    with pytest.raises(IndexError_, match="newer than"):
        Index(target)


def test_a_gap_no_migration_covers_is_refused(workspace):
    """Version 3 onward has no recorded steps: say so, do not guess."""
    workspace, _ = workspace
    from spanda.cli import main
    assert main(["index", str(workspace)]) == 0
    target = db_path(workspace)
    with Index(target) as index:
        index._set_meta("schema_version", "3")
    with pytest.raises(IndexError_, match="no migration covers"):
        Index(target)


def test_scan_report_names_only_what_went_missing_this_time(workspace, capsys):
    """After a long history `missing_at` is every symbol that ever went; the
    report after a scan should name what *this* scan is the first not to see,
    and nothing from earlier scans."""
    from spanda.cli import main
    workspace, _ = workspace
    helpers = workspace / "sample_pkg" / "helpers.py"
    original = helpers.read_text()
    helpers.write_text(original + "\ndef first_extra():\n    return 1\n\n\ndef second_extra():\n    return 2\n")
    assert main(["index", str(workspace)]) == 0

    helpers.write_text(original + "\ndef second_extra():\n    return 2\n")
    assert main(["index", str(workspace)]) == 0
    report = capsys.readouterr().out
    assert "1 symbols seen by the previous scan are gone" in report
    assert "first_extra" in report

    helpers.write_text(original)
    assert main(["index", str(workspace)]) == 0
    report = capsys.readouterr().out
    assert "1 symbols seen by the previous scan are gone" in report
    assert "second_extra" in report and "first_extra" not in report

    assert main(["index", str(workspace)]) == 0
    assert "gone" not in capsys.readouterr().out
    with Index(db_path(workspace)) as index:
        assert len(index.missing_at(4)) == 2, "history keeps both"



def test_framework_called_symbols_carry_the_flag_in_the_index(workspace):
    """The flag is what keeps a symbol off the dead list. All three shapes
    from the vetting — decorator on a function, MCP handler, and an
    override with no decorator — must carry it."""
    from spanda.cli import main
    workspace, _ = workspace
    assert main(["index", str(workspace)]) == 0
    with Index(db_path(workspace)) as index:
        flagged = {r["qualname"] for r in index.connection.execute(
            "SELECT qualname FROM symbols WHERE file_path = 'sample_pkg/middleware.py'"
            " AND has_dynamic_dispatch = 1")}
    assert flagged == {"security_headers", "list_tools", "RequestLogger.dispatch",
                       "Auditor.name_present", "AuditLog"}
    with Index(db_path(workspace)) as index:
        hints = {r["qualname"]: r["dispatch_hint"] for r in index.connection.execute(
            "SELECT qualname, dispatch_hint FROM symbols"
            " WHERE file_path = 'sample_pkg/middleware.py'")}
    assert hints["security_headers"] == "dispatch:app.middleware"
    assert hints["RequestLogger.dispatch"] == "override:BaseHTTPMiddleware.dispatch"
    assert hints["nightly_cleanup"] == "unknown_decorator:scheduler.scheduled_job"
    assert hints["Auditor.on_validate"] == "external_base:BaseModel"
    assert hints["Auditor.name_present"] == "dispatch:field_validator", \
        "a decorator explanation outranks the vaguer external-base one"
    assert hints["Auditor._helper"] is None, "private: the class's own"
    assert hints["AuditLog"] == "inherits:Base", "a mapped table is alive by inheritance"
    assert hints["app"] is None



def test_an_external_base_hint_goes_away_when_a_call_appears(workspace):
    """A hint must describe the newest scan, not the first one that set it."""
    from spanda.cli import main
    workspace, _ = workspace
    assert main(["index", str(workspace)]) == 0
    (workspace / "sample_pkg" / "consumer.py").write_text(
        (workspace / "sample_pkg" / "consumer.py").read_text()
        + "\n\ndef audit(a):\n    return a.on_validate()\n")
    assert main(["index", str(workspace)]) == 0
    with Index(db_path(workspace)) as index:
        hint = index.connection.execute(
            "SELECT dispatch_hint FROM symbols WHERE qualname = 'Auditor.on_validate'"
        ).fetchone()[0]
    assert hint is None
