"""M4 gate: pointing an import at a file, and finding the cycles.

Nothing about symbols here. This stage answers one question — which file does
this import statement mean — and reports the circular groups that make a
naive ordering wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spanda.extract import module_name_for, plan_scan, stream_records
from spanda.modules import (EXTERNAL, UNRESOLVED, ModuleIndex, absolute_module,
                            build_import_graph, cycle_groups,
                            processing_order, resolve_imports)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="module")
def survey():
    plan = plan_scan(FIXTURES)
    index, records = ModuleIndex(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        records.append(record)
    edges = [e for r in records for e in resolve_imports(r, index)]
    return index, edges, [r["file"] for r in records]


# -- module naming ---------------------------------------------------------

def test_module_name_stops_at_the_root(tmp_path):
    """A project root often has its own __init__.py. It is still the
    directory on sys.path, not a package inside one — including it prefixes
    every module with the checkout's directory name, and then nothing
    resolves."""
    (tmp_path / "__init__.py").write_text("")
    (tmp_path / "db.py").write_text("")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "thing.py").write_text("")

    assert module_name_for(tmp_path / "db.py", tmp_path) == "db"
    assert module_name_for(tmp_path / "pkg" / "thing.py", tmp_path) == "pkg.thing"
    assert module_name_for(tmp_path / "pkg" / "__init__.py", tmp_path) == "pkg"


# -- relative imports ------------------------------------------------------

@pytest.mark.parametrize("module,is_package,name,level,expected", [
    ("pkg.sub.mod", False, "base", 1, "pkg.sub.base"),
    ("pkg.sub.mod", False, "base", 2, "pkg.base"),
    ("pkg.sub.mod", False, None, 1, "pkg.sub"),
    # Inside a package's __init__, one dot means that package itself.
    ("pkg.sub", True, "base", 1, "pkg.sub.base"),
    ("pkg.sub", True, None, 1, "pkg.sub"),
    ("pkg.mod", False, "x", 4, None),      # climbs past the top
    ("pkg.sub.mod", False, "x", 0, "x"),   # absolute, level 0
])
def test_relative_imports_become_absolute(module, is_package, name, level, expected):
    assert absolute_module(module, is_package, name, level) == expected


# -- resolution ------------------------------------------------------------

def test_imports_resolve_to_the_right_files(survey):
    _index, edges, _files = survey
    resolved = {(e.source_file, e.target_file) for e in edges if e.target_file}
    assert ("sample_pkg/a.py", "sample_pkg/b.py") in resolved
    assert ("sample_pkg/derived.py", "sample_pkg/base.py") in resolved
    assert ("sample_pkg/star.py", "sample_pkg/helpers.py") in resolved


def test_importing_a_submodule_points_at_it_not_at_the_package(survey):
    """`from . import handlers` depends on handlers.py, not on __init__.py.
    Resolving to the package loses the real edge."""
    _index, edges, _files = survey
    targets = {e.target_file for e in edges
               if e.source_file == "sample_pkg/dynamic.py"}
    assert "sample_pkg/handlers.py" in targets


def test_third_party_imports_are_external_not_a_failure(survey):
    """sqlalchemy is not installed and is not this codebase's problem."""
    _index, edges, _files = survey
    external = {e.target_module for e in edges if e.reason == EXTERNAL}
    assert {"sqlalchemy", "decimal", "abc"} <= external
    assert not [e for e in edges if e.reason == UNRESOLVED], (
        "every import that looks internal should resolve on the fixture")


# -- cycles ----------------------------------------------------------------

def test_the_deliberate_cycle_is_found(survey):
    _index, edges, files = survey
    groups = cycle_groups(build_import_graph(edges, files))
    assert groups == [["sample_pkg/a.py", "sample_pkg/b.py"]]


def test_processing_order_puts_dependencies_first(survey):
    _index, edges, files = survey
    units = processing_order(build_import_graph(edges, files))

    position = {f: n for n, unit in enumerate(units) for f in unit}
    assert position["sample_pkg/helpers.py"] < position["sample_pkg/star.py"]
    assert position["sample_pkg/base.py"] < position["sample_pkg/derived.py"]
    assert position["sample_pkg/handlers.py"] < position["sample_pkg/dynamic.py"]
    assert position["sample_pkg/models.py"] < position["sample_pkg/__init__.py"]
    # The cycle is one unit, and it precedes the package root that imports it.
    assert position["sample_pkg/a.py"] == position["sample_pkg/b.py"]
    assert position["sample_pkg/a.py"] < position["sample_pkg/__init__.py"]


def test_every_file_appears_exactly_once(survey):
    """A file missing from the order would be silently skipped later."""
    _index, edges, files = survey
    units = processing_order(build_import_graph(edges, files))
    ordered = [f for unit in units for f in unit]
    assert sorted(ordered) == sorted(files)
    assert len(ordered) == len(set(ordered))


def test_a_file_that_imports_itself_is_a_cycle(tmp_path):
    graph = {"a.py": {"a.py"}, "b.py": set()}
    assert cycle_groups(graph) == [["a.py"]]
