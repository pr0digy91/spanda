"""M7 gate: re-read only what changed, and get the same answer.

The whole value of an incremental index rests on one property — that it is
indistinguishable from a full one. A faster index that is subtly different is
not an optimisation, it is a second source of truth.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from spanda.cli import _incremental_scan, changed_python_files
from spanda.extract import plan_scan, stream_records
from spanda.gaps import load_patterns
from spanda.store import Index, prepare_db_path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def git(root: Path, *args: str) -> str:
    return subprocess.run(("git", "-C", str(root)) + args,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A git repository holding a copy of the fixture codebase."""
    source = tmp_path / "code"
    shutil.copytree(FIXTURES, source, ignore=shutil.ignore_patterns(".spanda"))
    git(source, "init", "-q")
    git(source, "config", "user.email", "t@t")
    git(source, "config", "user.name", "t")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "first")
    return source


def full_scan(index, root, patterns) -> int:
    plan = plan_scan(root)
    scan_id = index.begin_scan(plan.root, plan.skipped_count)
    for record in stream_records(plan):
        index.write_record(scan_id, record, patterns)
    index.finish_scan(scan_id)
    return scan_id


def incremental(index, root, patterns, changed) -> int:
    plan = plan_scan(root)
    scan_id = index.begin_scan(plan.root, plan.skipped_count)
    _incremental_scan(index, root, scan_id, plan, patterns, changed)
    index.finish_scan(scan_id)
    return scan_id


def snapshot(index, scan_id) -> tuple:
    row = index.scan(scan_id)
    symbols = {r["symbol_key"]: r["content_hash"] for r in
               index.connection.execute(
                   "SELECT symbol_key, content_hash FROM symbols"
                   " WHERE last_seen_scan_id = ?", (scan_id,))}
    return (row["total_files"], row["total_symbols"],
            row["content_fingerprint"], symbols)


# -- the gate --------------------------------------------------------------

def test_incremental_matches_a_full_scan_exactly(repo, tmp_path):
    """Same commit, two routes to it, one answer."""
    patterns = load_patterns()
    helpers = repo / "sample_pkg" / "helpers.py"

    with Index(prepare_db_path(repo), codebase_root=repo) as index:
        full_scan(index, repo, patterns)
        before = git(repo, "rev-parse", "HEAD")

        helpers.write_text(helpers.read_text().replace(
            "def slugify(text: str) -> str:",
            "def slugify(text: str, sep: str = '-') -> str:"))
        (repo / "sample_pkg" / "added.py").write_text("def fresh():\n    return 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "second")

        changed = changed_python_files(repo, before)
        incremental_id = incremental(index, repo, patterns, changed)
        incremental_state = snapshot(index, incremental_id)

    with Index(tmp_path / "fresh.db", codebase_root=repo) as index:
        full_id = full_scan(index, repo, patterns)
        full_state = snapshot(index, full_id)

    assert incremental_state == full_state


def test_unchanged_files_are_carried_forward_not_deleted(repo):
    """Without this the first incremental run reports the whole codebase as
    deleted — the single most likely way this design fails silently."""
    patterns = load_patterns()
    with Index(prepare_db_path(repo), codebase_root=repo) as index:
        full_scan(index, repo, patterns)
        before = git(repo, "rev-parse", "HEAD")
        helpers = repo / "sample_pkg" / "helpers.py"
        helpers.write_text(helpers.read_text() + "\n\ndef extra():\n    return 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "second")

        scan_id = incremental(index, repo, patterns,
                              changed_python_files(repo, before))
        assert index.missing_at(scan_id) == []
        assert index.scan(scan_id)["total_files"] == 17


def test_a_commit_changing_nothing_still_carries_everything(repo):
    """An empty diff is the case a naive `NOT IN (...)` silently gets wrong,
    and one stranded file never recovers on later scans."""
    patterns = load_patterns()
    with Index(prepare_db_path(repo), codebase_root=repo) as index:
        first = full_scan(index, repo, patterns)
        before = git(repo, "rev-parse", "HEAD")
        git(repo, "commit", "-q", "--allow-empty", "-m", "nothing")

        second = incremental(index, repo, patterns,
                             changed_python_files(repo, before))
        assert index.scan(second)["total_symbols"] == index.scan(first)["total_symbols"]
        assert index.missing_at(second) == []

        # ...and a third, to prove the carry-forward chain does not decay.
        third = incremental(index, repo, patterns, set())
        assert index.scan(third)["total_symbols"] == index.scan(first)["total_symbols"]


def test_a_deleted_file_stops_being_carried_forward(repo):
    patterns = load_patterns()
    with Index(prepare_db_path(repo), codebase_root=repo) as index:
        full_scan(index, repo, patterns)
        before = git(repo, "rev-parse", "HEAD")
        (repo / "sample_pkg" / "handlers.py").unlink()
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "remove handlers")

        scan_id = incremental(index, repo, patterns,
                              changed_python_files(repo, before))
        gone = {r["qualname"] for r in index.missing_at(scan_id)}
    assert {"on_created", "on_paid"} <= gone


def test_a_symbol_deleted_earlier_is_not_resurrected(repo):
    """A file that is carried forward without being re-read must not bring
    back a symbol that was already removed from it."""
    patterns = load_patterns()
    helpers = repo / "sample_pkg" / "helpers.py"
    with Index(prepare_db_path(repo), codebase_root=repo) as index:
        full_scan(index, repo, patterns)
        before = git(repo, "rev-parse", "HEAD")

        helpers.write_text('__all__ = ["slugify"]\n\n\ndef slugify(t):\n    return t\n')
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "drop format_currency")
        second = incremental(index, repo, patterns,
                             changed_python_files(repo, before))

        git(repo, "commit", "-q", "--allow-empty", "-m", "nothing")
        third = incremental(index, repo, patterns, set())

        alive = {r["qualname"] for r in index.connection.execute(
            "SELECT qualname FROM symbols WHERE last_seen_scan_id = ?", (third,))}
    assert "format_currency" not in alive, "a removed symbol came back"
    assert "slugify" in alive


def test_changed_files_includes_uncommitted_work(repo):
    patterns = load_patterns()
    before = git(repo, "rev-parse", "HEAD")
    (repo / "sample_pkg" / "helpers.py").write_text("def only():\n    return 1\n")
    changed = changed_python_files(repo, before)
    assert "sample_pkg/helpers.py" in changed


def test_no_git_means_no_incremental_rather_than_a_guess(tmp_path):
    """'Cannot tell' has to be distinguishable from 'nothing changed'."""
    (tmp_path / "a.py").write_text("def f(): pass\n")
    assert changed_python_files(tmp_path, "HEAD~1") is None


def test_the_index_command_resolves_references_end_to_end(repo):
    """Through the CLI, not the resolver directly.

    The index path hands resolution a slimmed record; the resolver tests hand
    it a full one. A field the resolver needs but the slimming drops passes
    every resolver test and breaks `spanda index` — which is what happened.
    """
    from spanda.cli import main
    assert main(["index", str(repo)]) == 0
    with Index(prepare_db_path(repo)) as index:
        edges = index.connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        closure = index.connection.execute(
            "SELECT COUNT(*) FROM edges e JOIN symbols s ON s.uuid = e.target_symbol_uuid"
            " WHERE s.qualname = 'make_multiplier.multiply'").fetchone()[0]
    assert edges > 0
    assert closure == 1, "the closure edge must survive the slimmed record"


def test_changed_files_are_relative_to_the_scan_root_not_the_git_root(repo):
    """`spanda backfill ~/repo/services` where ~/repo is the git root. git
    names changes relative to ~/repo; the scan wants them relative to
    services/. Mismatched, every change misses the lookup and is carried
    forward stale — silently."""
    services = repo / "services"
    services.mkdir()
    (services / "billing.py").write_text("def total():\n    return 1\n")
    (repo / "elsewhere.py").write_text("def other():\n    return 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add services")
    before = git(repo, "rev-parse", "HEAD")

    (services / "billing.py").write_text("def total():\n    return 2\n")
    (repo / "elsewhere.py").write_text("def other():\n    return 3\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "change both")

    changed = changed_python_files(services, before)
    assert changed == {"billing.py"}, "relative to services/, and only what is under it"


def test_callers_on_a_deleted_symbol_says_gone_not_unused(repo, capsys):
    """Rename a function, re-index, ask about the old name. "Nothing
    references it — possibly unused" would be the wrong conclusion offered
    with confidence. The honest answer is that it no longer exists."""
    from spanda.cli import main
    assert main(["index", str(repo)]) == 0
    helpers = repo / "sample_pkg" / "helpers.py"
    helpers.write_text(helpers.read_text().replace("def slugify(", "def slug_of("))
    assert main(["index", str(repo)]) == 0
    capsys.readouterr()

    assert main(["callers", str(repo), "slugify"]) == 0
    out = capsys.readouterr().out
    assert "no longer exists" in out
    assert "possibly unused" not in out.lower()


def test_backfill_end_to_end_matches_a_full_scan_of_head(repo, tmp_path):
    """Three commits through the real command — worktree, checkout, git diff,
    incremental carry-forward, final resolution — and the newest scan must
    equal a plain full index of the same commit. The wrapper around
    _incremental_scan was verified only by hand before this."""
    from spanda.cli import main
    helpers = repo / "sample_pkg" / "helpers.py"
    helpers.write_text(helpers.read_text() + "\n\ndef second():\n    return 2\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "second")
    (repo / "sample_pkg" / "handlers.py").unlink()
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "third: drop handlers")

    assert main(["backfill", str(repo), "--last", "3"]) == 0
    with Index(prepare_db_path(repo)) as index:
        scans = index.scans(complete_only=True)
        assert [s["scan_id"] for s in scans] == [1, 2, 3]
        newest = scans[-1]
        backfilled = snapshot(index, newest["scan_id"])
        assert newest["cycles_recorded"] == 1
        assert index.connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0] > 0
        gone = {r["qualname"] for r in index.missing_at(3)}
        assert {"on_created", "on_paid"} <= gone

    fresh = tmp_path / "fresh.db"
    with Index(fresh, codebase_root=repo) as index:
        full_state = snapshot(index, full_scan(index, repo, load_patterns()))
    assert backfilled == full_state
