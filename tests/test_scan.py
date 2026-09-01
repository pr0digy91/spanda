"""The scan engine, apart from the commands that call it.

Most of it is exercised through `spanda index` and `spanda backfill`; these
pin the parts that are only visible from outside: what a scan writes down
about itself, and that a fallback explains itself.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from spanda.extract import plan_scan
from spanda.gaps import load_patterns
from spanda.scan import full_scan, git, git_failure, plan_for
from spanda.store import Index

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def _git(root, *args):
    return subprocess.run(("git", "-C", str(root)) + args,
                          capture_output=True, text=True).stdout.strip()


def test_a_scan_records_what_it_chose_not_to_read(tmp_path):
    src = tmp_path / "code"
    shutil.copytree(FIXTURES, src, ignore=shutil.ignore_patterns(".spanda"))
    (src / ".venv" / "lib").mkdir(parents=True)
    (src / ".venv" / "lib" / "six.py").write_text("x = 1\n")
    (src / ".venv" / "lib" / "seven.py").write_text("x = 1\n")

    with Index(tmp_path / "i.db", codebase_root=src) as index:
        plan = plan_scan(src)
        scan_id = index.begin_scan(plan.root, plan.skipped_count)
        full_scan(index, scan_id, plan, load_patterns())
        index.finish_scan(scan_id)
        unread = {r["path"]: (r["reason"], r["files"]) for r in index.unread_at(scan_id)}
        assert unread[".venv/"] == ("directory_excluded", 2)
        assert index.scan(scan_id)["skipped_files"] == 2


def test_git_failure_says_what_git_said(tmp_path):
    src = tmp_path / "r"
    src.mkdir()
    _git(src, "init", "-q")
    assert git(src, "diff", "--name-only", "nope", "HEAD") is None
    why = git_failure(src, "diff", "--name-only", "nope", "HEAD")
    assert "nope" in why or "bad revision" in why or "ambiguous" in why
    assert "\n" not in why, "one line, for one report line"


def test_plan_for_asks_only_about_planned_files(tmp_path):
    """A tracked file that matches an ignore pattern is still in the commit,
    so it is still read — `check-ignore` without --no-index agrees."""
    src = tmp_path / "r"
    (src / "gen").mkdir(parents=True)
    (src / "gen" / "kept.py").write_text("a = 1\n")
    (src / "gen" / "dropped.py").write_text("b = 1\n")
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    _git(src, "add", "-f", "gen/kept.py")
    (src / ".gitignore").write_text("gen/\n")
    _git(src, "add", ".gitignore")
    _git(src, "commit", "-qm", "one")

    plan = plan_for(src)
    assert [p.name for p in plan.files] == ["kept.py"]
    assert plan.ignored == ["gen/dropped.py"]
