"""The index guide must describe the index it is placed next to.

A document sitting beside a database is trusted without checking, so it must
not be able to carry numbers from somewhere else, or from last month.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from spanda.extract import plan_scan, stream_records
from spanda.gaps import load_patterns
from spanda.guide import render
from spanda.store import Index, prepare_db_path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture
def indexed(tmp_path):
    source = tmp_path / "some_project"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    plan, patterns = plan_scan(source), load_patterns()
    with Index(prepare_db_path(source), codebase_root=source) as index:
        scan_id = index.begin_scan(plan.root, plan.skipped_count)
        for record in stream_records(plan):
            index.write_record(scan_id, record, patterns)
        index.finish_scan(scan_id)
    return source


def test_every_placeholder_is_filled(indexed):
    with Index(prepare_db_path(indexed)) as index:
        text = render(index, indexed)
    assert not re.search(r"\{\{\w+\}\}", text), "an unfilled placeholder shipped"


def test_the_numbers_come_from_the_index(indexed):
    with Index(prepare_db_path(indexed)) as index:
        text = render(index, indexed)
        alive = index.connection.execute(
            "SELECT COUNT(*) c FROM symbols WHERE last_seen_scan_id = 1").fetchone()["c"]
    assert f"symbols               {alive}" in text or f"({alive} alive)" in text


def test_it_names_the_repository_it_describes(indexed):
    with Index(prepare_db_path(indexed)) as index:
        text = render(index, indexed)
    assert "# Reading the some_project index" in text
    assert str(prepare_db_path(indexed)) in text


def test_the_example_symbol_exists_in_this_codebase(indexed):
    """A reader who runs the example query verbatim should get a result."""
    with Index(prepare_db_path(indexed)) as index:
        text = render(index, indexed)
        names = {r["name"] for r in index.connection.execute(
            "SELECT name FROM symbols")}
    example = re.search(r"WHERE name = '(\w+)'", text).group(1)
    assert example in names


def test_it_refuses_to_describe_an_index_with_no_completed_scan(tmp_path):
    (tmp_path / "a.py").write_text("def f(): pass\n")
    with Index(prepare_db_path(tmp_path)) as index:
        with pytest.raises(ValueError, match="no completed scan"):
            render(index, tmp_path)


def test_an_index_without_edges_says_so_rather_than_claiming_none_exist(indexed):
    """Backfill resolves references only for the newest commit. An index in
    that state must not read as 'this codebase has no calls in it'."""
    with Index(prepare_db_path(indexed)) as index:
        index.connection.execute("DELETE FROM edges")
        text = render(index, indexed)
    assert "no reference edges yet" in text



def test_the_guide_lists_candidates_but_decides_nothing(indexed):
    """The candidates come from the index; the verdict never does. Every
    line is a suggestion in the file's format, and a recorded verdict
    removes its symbol from the list."""
    with Index(prepare_db_path(indexed)) as index:
        text = render(index, indexed)
    assert "0 verdict(s) recorded in the index" in text
    block = text.split("Waiting for a verdict", 1)[1]
    assert "dead   sample_pkg/handlers.py::on_paid" not in block, \
        "on_paid is named in a string literal: reachable through unresolved_refs? no — " \
        "it is on the candidate list only if nothing explains it"
    first = next(line for line in block.splitlines() if line.startswith("dead   "))
    target = first.split()[1]
    file_path, qualname = target.split("::")

    with Index(prepare_db_path(indexed)) as index:
        index.record_verdict(file_path, qualname, "alive", "checked by hand")
        text = render(index, indexed)
    assert "1 verdict(s) recorded in the index (1 alive, 0 dead)" in text
    assert target not in text.split("Waiting for a verdict", 1)[1]
    assert "1 alive verdict(s) with nothing to explain them" in text
