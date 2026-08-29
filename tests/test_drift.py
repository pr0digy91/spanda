"""M3 gate: the drift report, and the distinction it exists to draw.

A report that lumps shape changes together with body edits is a changelog.
The whole value is in separating "this can break your callers" from "this
cannot", so that is what these tests are about.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from spanda.drift import compare
from spanda.extract import plan_scan, stream_records
from spanda.gaps import load_patterns
from spanda.store import Index, IndexError_, prepare_db_path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def index_once(db: Path, source: Path) -> int:
    plan, patterns = plan_scan(source), load_patterns()
    with Index(db, codebase_root=source) as index:
        scan_id = index.begin_scan(plan.root, plan.skipped_count)
        for record in stream_records(plan):
            index.write_record(scan_id, record, patterns)
        index.finish_scan(scan_id)
    return scan_id


@pytest.fixture
def project(tmp_path):
    """A tiny codebase and its index, ready to be edited between scans."""
    source = tmp_path / "code"
    source.mkdir()
    (source / "billing.py").write_text(
        "def total(items, tax):\n"
        "    return sum(i.price for i in items) * tax\n\n\n"
        "def receipt(order):\n"
        "    return str(total(order.items, 1.05))\n")
    return source, prepare_db_path(source)


def drift_between(db, a, b):
    with Index(db) as index:
        return compare(index, a, b)


# -- the central distinction ------------------------------------------------

def test_a_shape_change_is_reported_separately_from_a_body_edit(project):
    source, db = project
    index_once(db, source)
    (source / "billing.py").write_text(
        # total gains a keyword-only parameter: callers break.
        "def total(items, *, tax):\n"
        "    return sum(i.price for i in items) * tax\n\n\n"
        # receipt's body changes only: callers are fine.
        "def receipt(order):\n"
        "    return f'{total(order.items, tax=1.05)}'\n")
    index_once(db, source)

    report = drift_between(db, 1, 2)
    assert [c.qualname for c in report.shape] == ["total"]
    assert [c.qualname for c in report.internal] == ["receipt"]
    assert report.shape[0].before == "(positional:items,positional:tax)->"
    assert report.shape[0].after == "(positional:items,keyword_only:tax)->"


def test_reformatting_produces_no_shape_change(project):
    """The report must survive a formatter run without crying wolf."""
    source, db = project
    index_once(db, source)
    (source / "billing.py").write_text(
        "def total(\n    items,\n    tax,\n):\n"
        "    return sum(i.price for i in items) * tax\n\n\n"
        "def receipt(order):\n"
        "    return str(total(order.items, 1.05))\n")
    index_once(db, source)

    report = drift_between(db, 1, 2)
    assert report.shape == []
    assert [c.qualname for c in report.internal] == ["total"]


def test_identical_code_produces_an_empty_report(project):
    source, db = project
    index_once(db, source)
    index_once(db, source)
    report = drift_between(db, 1, 2)
    assert report.identical
    assert report.total_changes == 0


# -- additions and removals -------------------------------------------------

def test_additions_and_removals(project):
    source, db = project
    index_once(db, source)
    (source / "billing.py").write_text("def receipt(order):\n    return '0'\n")
    (source / "extra.py").write_text("def brand_new():\n    return 1\n")
    index_once(db, source)

    report = drift_between(db, 1, 2)
    assert [c.qualname for c in report.removed] == ["total"]
    assert [c.qualname for c in report.added] == ["brand_new"]


def test_presence_is_tracked_per_scan_not_inferred(project):
    """A symbol deleted and restored must not look present throughout.

    first_seen/last_seen alone would say it existed the whole time, which is
    exactly the kind of confidently wrong answer this project exists to avoid.
    """
    source, db = project
    original = (source / "billing.py").read_text()
    index_once(db, source)                                    # scan 1: present
    (source / "billing.py").write_text("def receipt(order):\n    return '0'\n")
    index_once(db, source)                                    # scan 2: gone
    (source / "billing.py").write_text(original)
    index_once(db, source)                                    # scan 3: back

    with Index(db) as index:
        rows = {r["qualname"]: r["uuid"] for r in
                index.connection.execute("SELECT qualname, uuid FROM symbols")}
        at_one = index.present_at(1)
        at_two = index.present_at(2)
        at_three = index.present_at(3)

    total = rows["total"]
    assert total in at_one
    assert total not in at_two, "it was genuinely absent at scan 2"
    assert total in at_three

    # And the report for 1 -> 2 must call it removed, not unchanged.
    assert [c.qualname for c in drift_between(db, 1, 2).removed] == ["total"]
    assert [c.qualname for c in drift_between(db, 2, 3).added] == ["total"]
    # Comparing the endpoints directly: it is present at both, so unchanged.
    assert drift_between(db, 1, 3).identical


# -- honesty ----------------------------------------------------------------

def test_shape_changes_on_undeterminable_symbols_are_flagged(tmp_path):
    """A shape change is actionable only if you can find the callers. When
    you cannot, the report has to say so rather than imply a code review
    would catch it."""
    source = tmp_path / "code"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    db = prepare_db_path(source)
    index_once(db, source)

    models = source / "sample_pkg" / "models.py"
    models.write_text(models.read_text().replace(
        "def _apply_rls_context(session, flush_context, instances) -> None:",
        "def _apply_rls_context(session, flush_context) -> None:"))
    index_once(db, source)

    report = drift_between(db, 1, 2)
    changed = {c.qualname: c for c in report.shape}
    assert "_apply_rls_context" in changed
    assert changed["_apply_rls_context"].callers_unknowable is True


def test_unparseable_files_are_declared_as_a_caveat(tmp_path):
    """A file that failed to parse contributes no symbols, so its contents
    read as removed — a fact about the tool, not about the code."""
    source = tmp_path / "code"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    db = prepare_db_path(source)
    index_once(db, source)
    index_once(db, source)

    report = drift_between(db, 1, 2)
    assert any("could not parse" in note for note in report.caveats)


def test_refuses_to_compare_scans_in_the_wrong_order(project):
    source, db = project
    index_once(db, source)
    index_once(db, source)
    with Index(db) as index:
        with pytest.raises(ValueError, match="not earlier"):
            compare(index, 2, 1)


def test_refuses_to_compare_against_an_incomplete_scan(project):
    source, db = project
    index_once(db, source)
    index_once(db, source)
    with Index(db) as index:
        index.connection.execute("UPDATE scans SET completed = 0 WHERE scan_id = 2")
        with pytest.raises(IndexError_, match="never completed"):
            compare(index, 1, 2)
