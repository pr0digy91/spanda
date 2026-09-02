"""The verdicts loop: a person decides, the tool keeps it and checks it.

What is pinned: the file format refuses bad lines out loud; an alive verdict
on an unrecognised shape becomes the pattern line that would have caught
it; a verdict the code later contradicts is reported; the candidate list
excludes anything vetted; and the verdicts file survives the directory's
own .gitignore.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from spanda.cli import main
from spanda.gaps import load_patterns
from spanda.store import Index, ensure_index_dir, prepare_db_path
from spanda.verdicts import load, parse, render, suggestion_for, vet, verdicts_path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture
def indexed(tmp_path):
    source = tmp_path / "proj"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    assert main(["index", str(source)]) == 0
    return source


def test_bad_lines_are_reported_not_skipped():
    verdicts, problems = parse(
        "# comment\n"
        "alive  a.py::f  2026-09-02  fine\n"
        "maybe  a.py::g  2026-09-02  not a verdict\n"
        "dead   a.py/g   2026-09-02  no double colon\n"
        "dead   a.py::h  yesterday   bad date\n"
        "dead   a.py::i  2026-09-02\n")
    assert [(v.verdict, v.qualname, v.note) for v in verdicts] == [
        ("alive", "f", "fine"), ("dead", "i", "")]
    assert [p.line for p in problems] == [3, 4, 5]


def test_an_alive_verdict_on_an_unknown_shape_becomes_a_pattern_line(indexed):
    verdicts_path(indexed).write_text(
        "alive  sample_pkg/middleware.py::nightly_cleanup      2026-09-02  apscheduler runs it\n"
        "alive  sample_pkg/middleware.py::Auditor.on_validate  2026-09-02  pydantic hook\n"
        "alive  sample_pkg/middleware.py::RequestLogger.dispatch  2026-09-02  already a pattern\n")
    known, problems, exists = load(indexed)
    assert exists and not problems
    with Index(prepare_db_path(indexed)) as index:
        report = vet(index, known, problems, exists)
    assert [s.line for s in report.suggestions] == [
        "scheduler.scheduled_job", "method:BaseModel.on_validate"]
    assert [v.qualname for v, _why in report.explained] == ["RequestLogger.dispatch"]
    assert report.contradicted == [] and report.blind_spots == []
    text = render(report, "proj", verdicts_path(indexed))
    assert "PATTERN LINES TO ADD" in text and "scheduler.scheduled_job" in text


def test_a_contradicted_verdict_is_reported(indexed):
    verdicts_path(indexed).write_text(
        "dead   sample_pkg/helpers.py::slugify   2026-09-02  thought unused\n"
        "alive  sample_pkg/helpers.py::vanished  2026-09-02  never existed\n"
        "alive  sample_pkg/handlers.py::on_paid  2026-09-02  called by name from a string\n")
    known, problems, exists = load(indexed)
    with Index(prepare_db_path(indexed)) as index:
        report = vet(index, known, problems, exists)
    why = {v.qualname: reason for v, reason in report.contradicted}
    assert "caller(s) now" in why["slugify"]
    assert why["vanished"] == "no such symbol in the index"
    assert [v.qualname for v in report.blind_spots] == ["on_paid"], \
        "alive, no caller, no hint: the tool has a blind spot and says so"


def test_candidates_exclude_the_vetted_and_the_hinted(indexed):
    with Index(prepare_db_path(indexed)) as index:
        before = vet(index, [], [], False)
    assert before.candidates_total > 0
    names = {q for _f, q, _l in before.candidates}
    assert "nightly_cleanup" not in names, "an unknown decorator is a hint, not a candidate"
    assert "Auditor.on_validate" not in names
    first_file, first_name, _line = before.candidates[0]
    verdicts_path(indexed).write_text(f"dead  {first_file}::{first_name}  2026-09-02  vetted\n")
    known, problems, exists = load(indexed)
    with Index(prepare_db_path(indexed)) as index:
        after = vet(index, known, problems, exists)
    assert after.candidates_total == before.candidates_total - 1
    assert (first_file, first_name) not in {(f, q) for f, q, _l in after.candidates}


def test_index_loads_verdicts_and_callers_shows_them(indexed, capsys):
    verdicts_path(indexed).write_text(
        "alive  sample_pkg/handlers.py::on_paid  2026-09-02  dispatched from HANDLER_NAMES\n")
    assert main(["index", str(indexed)]) == 0
    capsys.readouterr()
    assert main(["callers", str(indexed), "on_paid"]) == 0
    out = capsys.readouterr().out
    assert "Vetted ALIVE by a person on 2026-09-02: dispatched from HANDLER_NAMES" in out


def test_append_to_writes_the_lines_once(indexed, tmp_path, capsys):
    verdicts_path(indexed).write_text(
        "alive  sample_pkg/middleware.py::nightly_cleanup  2026-09-02  apscheduler\n")
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("*.get\n")
    assert main(["vet", str(indexed), "--append-to", str(patterns)]) == 0
    assert main(["vet", str(indexed), "--append-to", str(patterns)]) == 0
    lines = patterns.read_text().splitlines()
    assert lines.count("scheduler.scheduled_job") == 1
    assert lines[0] == "*.get"


def test_the_verdicts_file_survives_the_directory_gitignore(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    ensure_index_dir(repo)
    (repo / ".spanda" / "index.db").write_bytes(b"x")
    verdicts_path(repo).write_text("# verdicts\n")
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=True).stdout
    assert ".spanda/verdicts.txt" in result
    assert ".gitignore" not in result, "regenerated wherever spanda runs; not for git"
    assert "index.db" not in result


def test_an_old_gitignore_is_brought_forward_but_an_edited_one_is_kept(tmp_path):
    from spanda.store import GITIGNORE_BODY, OLD_GITIGNORE_BODY
    old = tmp_path / "old"
    (old / ".spanda").mkdir(parents=True)
    (old / ".spanda" / ".gitignore").write_text(OLD_GITIGNORE_BODY)
    ensure_index_dir(old)
    assert (old / ".spanda" / ".gitignore").read_text() == GITIGNORE_BODY

    edited = tmp_path / "edited"
    (edited / ".spanda").mkdir(parents=True)
    (edited / ".spanda" / ".gitignore").write_text("*\n!notes.md\n")
    ensure_index_dir(edited)
    assert (edited / ".spanda" / ".gitignore").read_text() == "*\n!notes.md\n"


def test_suggestions_come_only_from_unrecognised_hints():
    assert suggestion_for("unknown_decorator:x.y", "f").line == "x.y"
    assert suggestion_for("external_base:Base", "Cls.run").line == "method:Base.run"
    assert suggestion_for("dispatch:router.get", "f") is None
    assert suggestion_for(None, "f") is None
