"""The verdicts loop: a person decides, the index keeps it and checks it.

What is pinned: verdicts survive scans; an alive verdict on an unrecognised
shape becomes the pattern line that would have caught it; a verdict the
code later contradicts is reported; the candidate list excludes anything
vetted; import and export round-trip; and the verdicts file that an
earlier build let through the directory's .gitignore is not let through
any more.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from spanda.cli import main
from spanda.store import GITIGNORE_BODY, Index, ensure_index_dir, prepare_db_path
from spanda.verdicts import as_line, parse, render, suggestion_for, vet

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


def test_a_verdict_is_recorded_in_the_index_and_survives_a_scan(indexed, capsys):
    assert main(["vet", str(indexed), "--alive", "sample_pkg/handlers.py::on_paid",
                 "--note", "dispatched from HANDLER_NAMES"]) == 0
    assert main(["index", str(indexed)]) == 0
    capsys.readouterr()
    assert main(["callers", str(indexed), "on_paid"]) == 0
    out = capsys.readouterr().out
    assert "Vetted ALIVE by a person on " in out
    assert "dispatched from HANDLER_NAMES" in out and "recorded in the index" in out
    with Index(prepare_db_path(indexed)) as index:
        (verdict,) = index.verdicts()
    assert (verdict.verdict, verdict.qualname) == ("alive", "on_paid")


def test_an_alive_verdict_on_an_unknown_shape_becomes_a_pattern_line(indexed):
    with Index(prepare_db_path(indexed)) as index:
        index.record_verdict("sample_pkg/middleware.py", "nightly_cleanup", "alive", "apscheduler")
        index.record_verdict("sample_pkg/middleware.py", "Auditor.on_validate", "alive", "pydantic hook")
        index.record_verdict("sample_pkg/middleware.py", "RequestLogger.dispatch", "alive", "already a pattern")
        report = vet(index)
    assert [s.line for s in report.suggestions] == [
        "method:BaseModel.on_validate", "scheduler.scheduled_job"]
    assert [v.qualname for v, _why in report.explained] == ["RequestLogger.dispatch"]
    assert report.contradicted == [] and report.blind_spots == []
    text = render(report, "proj")
    assert "PATTERN LINES TO ADD" in text and "scheduler.scheduled_job" in text


def test_a_contradicted_verdict_is_reported(indexed):
    with Index(prepare_db_path(indexed)) as index:
        index.record_verdict("sample_pkg/helpers.py", "slugify", "dead", "thought unused")
        index.record_verdict("sample_pkg/helpers.py", "vanished", "alive", "never existed")
        index.record_verdict("sample_pkg/handlers.py", "on_paid", "alive", "called by name")
        report = vet(index)
    why = {v.qualname: reason for v, reason in report.contradicted}
    assert "caller(s) now" in why["slugify"]
    assert why["vanished"] == "no such symbol in the index"
    assert [v.qualname for v in report.blind_spots] == ["on_paid"], \
        "alive, no caller, no hint: the tool has a blind spot and says so"


def test_candidates_exclude_the_vetted_and_the_hinted(indexed):
    with Index(prepare_db_path(indexed)) as index:
        before = vet(index)
        assert before.candidates_total > 0
        names = {q for _f, q, _l in before.candidates}
        assert "nightly_cleanup" not in names, "an unknown decorator is a hint, not a candidate"
        assert "Auditor.on_validate" not in names
        first_file, first_name, _line = before.candidates[0]
        index.record_verdict(first_file, first_name, "dead", "vetted")
        after = vet(index)
    assert after.candidates_total == before.candidates_total - 1
    assert (first_file, first_name) not in {(f, q) for f, q, _l in after.candidates}


def test_export_and_import_round_trip(indexed, tmp_path, capsys):
    assert main(["vet", str(indexed), "--dead", "sample_pkg/helpers.py::_internal_only",
                 "--alive", "sample_pkg/handlers.py::on_paid", "--note", "by name"]) == 0
    capsys.readouterr()
    assert main(["vet", str(indexed), "--export"]) == 0
    exported = capsys.readouterr().out
    lines = [l for l in exported.splitlines() if l.startswith(("alive", "dead"))]
    assert len(lines) == 2 and any("on_paid" in l and "by name" in l for l in lines)

    fresh = tmp_path / "again"
    shutil.copytree(FIXTURES, fresh, ignore=shutil.ignore_patterns(".spanda"))
    assert main(["index", str(fresh)]) == 0
    saved = tmp_path / "verdicts.txt"
    saved.write_text(exported)
    assert main(["vet", str(fresh), "--from", str(saved)]) == 0
    with Index(prepare_db_path(fresh)) as index:
        assert sorted(as_line(v) for v in index.verdicts()) == sorted(lines)


def test_forget_removes_a_verdict_and_says_so_when_there_is_none(indexed, capsys):
    assert main(["vet", str(indexed), "--dead", "sample_pkg/helpers.py::_internal_only"]) == 0
    assert main(["vet", str(indexed), "--forget", "sample_pkg/helpers.py::_internal_only"]) == 0
    assert main(["vet", str(indexed), "--forget", "sample_pkg/helpers.py::_internal_only"]) == 1
    assert "no verdict recorded" in capsys.readouterr().err


def test_append_to_writes_the_lines_once(indexed, tmp_path):
    with Index(prepare_db_path(indexed)) as index:
        index.record_verdict("sample_pkg/middleware.py", "nightly_cleanup", "alive", "apscheduler")
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("*.get\n")
    assert main(["vet", str(indexed), "--append-to", str(patterns)]) == 0
    assert main(["vet", str(indexed), "--append-to", str(patterns)]) == 0
    lines = patterns.read_text().splitlines()
    assert lines.count("scheduler.scheduled_job") == 1
    assert lines[0] == "*.get"


def test_the_whole_index_directory_is_ignored_again(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    ensure_index_dir(repo)
    (repo / ".spanda" / "index.db").write_bytes(b"x")
    (repo / ".spanda" / "verdicts.txt").write_text("# a leftover from an earlier build\n")
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=True).stdout
    assert ".spanda" not in result


def test_a_superseded_gitignore_is_brought_back_but_an_edited_one_is_kept(tmp_path):
    from spanda.store import _SUPERSEDED_GITIGNORE_BODIES
    for number, body in enumerate(_SUPERSEDED_GITIGNORE_BODIES):
        old = tmp_path / f"old{number}"
        (old / ".spanda").mkdir(parents=True)
        (old / ".spanda" / ".gitignore").write_text(body)
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
