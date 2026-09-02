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


# -- edges and cycles (M8) --------------------------------------------------

def index_via_cli(source: Path) -> None:
    from spanda.cli import main
    assert main(["index", str(source)]) == 0


def test_a_lost_reference_is_reported(tmp_path):
    """Remove the one call to a function. Symbol drift sees nothing — the
    callee is unchanged — but the reference graph lost an edge, and that is
    the thing a reader deleting the callee would want to know."""
    source = tmp_path / "code"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    index_via_cli(source)
    derived = source / "sample_pkg" / "derived.py"
    derived.write_text(derived.read_text().replace("        self.refund(0)", "        return None"))
    index_via_cli(source)

    report = drift_between(prepare_db_path(source), 1, 2)
    assert any("UpiPayment.settle -> PaymentMethod.refund (calls)" in e
               for e in report.edges_removed), report.edges_removed
    assert not any("refund" in c.qualname for c in report.shape + report.removed)


def test_a_new_circular_import_is_reported(tmp_path):
    """Make base.py import derived.py: base <-> derived is now a cycle."""
    source = tmp_path / "code"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    index_via_cli(source)
    base = source / "sample_pkg" / "base.py"
    base.write_text("from .derived import UpiPayment\n" + base.read_text())
    index_via_cli(source)

    report = drift_between(prepare_db_path(source), 1, 2)
    assert ["sample_pkg/base.py", "sample_pkg/derived.py"] in report.cycles_appeared
    assert report.cycles_gone == []


def test_a_dissolved_cycle_is_reported(tmp_path):
    """Break the fixture's deliberate a <-> b cycle."""
    source = tmp_path / "code"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    index_via_cli(source)
    (source / "sample_pkg" / "b.py").write_text(
        'def validate_table(table_id: str) -> bool:\n    return table_id.startswith("tbl_")\n')
    index_via_cli(source)

    report = drift_between(prepare_db_path(source), 1, 2)
    assert ["sample_pkg/a.py", "sample_pkg/b.py"] in report.cycles_gone


def test_missing_edge_data_is_a_caveat_not_a_zero(tmp_path):
    """A scan with no resolved references must not read as 'no edges changed'."""
    source = tmp_path / "code"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    db = prepare_db_path(source)
    index_once(db, source)          # symbols only: no references, no cycles
    index_via_cli(source)           # full: references and cycles

    report = drift_between(db, 1, 2)
    assert report.edges_added == [] and report.edges_removed == []
    assert any("no reference data" in c for c in report.caveats)
    assert any("import graph was never computed" in c for c in report.caveats)



# -- loop depth --------------------------------------------------------------

def test_a_loop_added_inside_a_loop_reads_as_deeper(project):
    """Depth is a property of the body, so it is also an internal change;
    it is reported on its own because it is a different question."""
    source, db = project
    index_once(db, source)
    (source / "billing.py").write_text(
        "def total(items, tax):\n"
        "    out = 0\n"
        "    for i in items:\n"
        "        for extra in i.extras:\n"
        "            out += extra\n"
        "    return out * tax\n\n\n"
        "def receipt(order):\n"
        "    return str(total(order.items, 1.05))\n")
    index_once(db, source)
    report = drift_between(db, 1, 2)
    assert [(c.qualname, c.before, c.after) for c in report.loops_deeper] == [("total", "1", "2")]
    assert report.loops_shallower == []
    assert [c.qualname for c in report.internal] == ["total"]

    (source / "billing.py").write_text(
        "def total(items, tax):\n"
        "    return sum(items) * tax\n\n\n"
        "def receipt(order):\n"
        "    return str(total(order.items, 1.05))\n")
    index_once(db, source)
    report = drift_between(db, 2, 3)
    assert [(c.qualname, c.before, c.after) for c in report.loops_shallower] == [("total", "2", "0")]


def test_a_version_without_loop_depth_is_a_caveat_not_a_zero(project):
    """Rows written before schema 13 carry NULL. If that body is still the
    current one the next scan fills it exactly; if it was replaced, nothing
    can, and the report says so instead of comparing against zero."""
    source, db = project
    index_once(db, source)
    with Index(db) as index:
        index.connection.execute("UPDATE symbol_versions SET loop_depth = NULL")
    # An unchanged scan: the current bodies get their depth back.
    index_once(db, source)
    with Index(db) as index:
        missing = index.connection.execute(
            "SELECT COUNT(*) FROM symbol_versions WHERE loop_depth IS NULL").fetchone()[0]
    assert missing == 0

    with Index(db) as index:
        index.connection.execute(
            "UPDATE symbol_versions SET loop_depth = NULL WHERE scan_id = 1")
    (source / "billing.py").write_text(
        "def total(items, tax):\n"
        "    return sum(i.price for i in items) * tax * 2\n\n\n"
        "def receipt(order):\n"
        "    return str(total(order.items, 1.05))\n")
    index_once(db, source)
    report = drift_between(db, 1, 3)
    assert report.loops_deeper == [] and report.loops_shallower == []
    assert any("no loop depth recorded" in c for c in report.caveats)
