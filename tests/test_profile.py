"""`spanda profile`: what the code keeps doing, read from the index.

The report describes the corpus and stops short of judging it. These tests
pin the arithmetic, and the one distinction that makes the reuse section
worth reading — N files with one body is copying, N files with N bodies is
the same name doing N different things.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from spanda.cli import main
from spanda.profile import build, render
from spanda.store import Index, prepare_db_path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture
def indexed(tmp_path):
    source = tmp_path / "proj"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    assert main(["index", str(source)]) == 0
    return source


def test_reuse_counts_files_and_distinct_bodies(indexed):
    """`charge` is defined in base.py and derived.py with different bodies."""
    with Index(prepare_db_path(indexed)) as index:
        profile = build(index, min_files=2)
    charge = next(r for r in profile.reused if r.name == "charge")
    assert charge.kind == "method"
    assert charge.files == 2
    assert charge.distinct_bodies == 2
    assert charge.examples == ["sample_pkg/base.py", "sample_pkg/derived.py"]


def test_verbatim_copies_are_told_apart_from_same_named_variants(tmp_path):
    """Three files, one body: copying. Three files, three bodies: not."""
    src = tmp_path / "p"
    src.mkdir()
    for n in range(3):
        (src / f"m{n}.py").write_text(
            "def copied():\n    return 1\n\n\n"
            f"def variant():\n    return {n}\n")
    assert main(["index", str(src)]) == 0
    with Index(prepare_db_path(src)) as index:
        profile = build(index, min_files=3)
    by_name = {r.name: r for r in profile.reused}
    assert by_name["copied"].distinct_bodies == 1
    assert by_name["variant"].distinct_bodies == 3
    text = render(profile, "p")
    assert "identical copies" in text.split("copied")[1].split("\n")[0]


def test_dunder_methods_do_not_count_as_reuse(tmp_path):
    src = tmp_path / "p"
    src.mkdir()
    for n in range(3):
        (src / f"m{n}.py").write_text(
            f"class C{n}:\n    def __init__(self):\n        self.n = {n}\n")
    assert main(["index", str(src)]) == 0
    with Index(prepare_db_path(src)) as index:
        profile = build(index, min_files=2)
    assert not [r for r in profile.reused if r.name == "__init__"]


def test_annotation_and_docstring_arithmetic(indexed):
    with Index(prepare_db_path(indexed)) as index:
        profile = build(index)
    # checkout.py: take_payment(method: PaymentMethod, amount) -> annotated 1 of 2
    assert 0.0 < profile.param_annotation_rate < 1.0
    assert 0.0 < profile.return_annotation_rate < 1.0
    have, total = profile.docstrings["function"]
    assert 0 < have <= total
    assert profile.naming["snake_case functions"][0] == profile.naming["snake_case functions"][1]


def test_tests_are_excluded_by_default_and_counted(tmp_path):
    src = tmp_path / "p"
    (src / "tests").mkdir(parents=True)
    (src / "app.py").write_text("def real():\n    return 1\n")
    (src / "tests" / "test_app.py").write_text("def test_real():\n    return 1\n")
    assert main(["index", str(src)]) == 0
    with Index(prepare_db_path(src)) as index:
        default = build(index)
        everything = build(index, include_tests=True)
    assert default.symbols == 1 and default.tests_excluded == 1
    assert everything.symbols == 2 and everything.tests_excluded == 0


def test_churn_appears_only_with_history(indexed):
    helpers = indexed / "sample_pkg" / "helpers.py"
    with Index(prepare_db_path(indexed)) as index:
        assert build(index).churn == []
    for sig in ("def slugify(text: str, a=1) -> str:", "def slugify(text: str, a=1, b=2) -> str:"):
        helpers.write_text(helpers.read_text().replace(
            helpers.read_text().split("\n")[helpers.read_text().split("\n").index(
                next(l for l in helpers.read_text().split("\n") if l.startswith("def slugify")))],
            sig))
        assert main(["index", str(indexed)]) == 0
    with Index(prepare_db_path(indexed)) as index:
        profile = build(index)
    assert profile.scans == 3
    assert any(q == "slugify" and shapes == 3 for q, _f, shapes in profile.churn)


def test_the_command_runs_and_names_the_repo(indexed, capsys):
    assert main(["profile", str(indexed)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("proj: what the code keeps doing")
    assert "PARAMETERS" in out and "DOCSTRINGS" in out and "NAMING" in out
