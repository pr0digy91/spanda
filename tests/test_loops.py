"""`spanda loops`: where the loops are, read from the index.

The fixture's batch.py holds one case of each shape; recursion.py holds
the recursive ones. What is pinned is the reading, not a complexity —
the report never states one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from spanda.cli import main
from spanda.loops import build, is_database_call, load_database_patterns, render
from spanda.store import Index, prepare_db_path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture
def indexed(tmp_path):
    source = tmp_path / "proj"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    assert main(["index", str(source)]) == 0
    return source


def test_depth_in_one_body_and_across_a_call(indexed):
    with Index(prepare_db_path(indexed)) as index:
        report = build(index)
    deepest = {s.qualname: s.own for s in report.deepest}
    assert deepest["pair_up"] == 2
    across = {s.qualname: s for s in report.across_calls}
    assert across["pair_all_groups"].own == 1
    assert across["pair_all_groups"].reach == 3
    assert across["pair_all_groups"].via == "pair_up"
    assert "normalise_all" not in across, "slugify has no loops to add"


def test_recursion_is_read_off_the_call_graph(indexed):
    with Index(prepare_db_path(indexed)) as index:
        report = build(index)
    assert ["countdown"] in report.recursion
    assert ["is_even", "is_odd"] in report.recursion


def test_a_database_call_inside_a_loop_is_kept_though_unresolved(indexed):
    with Index(prepare_db_path(indexed)) as index:
        report = build(index)
    hits = [(c.enclosing, c.raw, c.depth) for c in report.database_in_loops]
    assert ("load_each", "session.get", 1) in hits
    assert report.unseen_in_loops.get("attribute_on_unknown_type", 0) >= 1


def test_the_report_never_states_a_complexity(indexed):
    with Index(prepare_db_path(indexed)) as index:
        text = render(build(index), "proj")
    assert "O(" not in text
    assert "does not say" in text and "how anything scales" in text
    assert "3 deep (own 1)" in text and "via pair_up" in text
    assert "countdown calls itself" in text
    assert "session.get" in text


def test_database_patterns_are_names_not_types():
    patterns = load_database_patterns()
    assert is_database_call("session.execute", patterns)
    assert is_database_call("self.db.scalars", patterns)
    assert is_database_call("conn.fetchall", patterns)
    assert not is_database_call("cache.get", patterns), "dict-like .get is not a query"
    assert not is_database_call("logger.info", patterns)


def test_a_call_moved_out_of_a_loop_reads_as_out_of_it(tmp_path):
    src = tmp_path / "p"
    src.mkdir()
    (src / "m.py").write_text("def work():\n    return 1\n\n\ndef run(xs):\n"
                              "    for x in xs:\n        work()\n")
    assert main(["index", str(src)]) == 0
    (src / "m.py").write_text("def work():\n    return 1\n\n\ndef run(xs):\n"
                              "    work()\n    for x in xs:\n        pass\n")
    assert main(["index", str(src)]) == 0
    with Index(prepare_db_path(src)) as index:
        (depth,) = index.connection.execute(
            "SELECT loop_depth FROM edges WHERE edge_type = 'calls'").fetchone()
    assert depth == 0
