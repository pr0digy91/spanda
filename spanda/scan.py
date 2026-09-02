"""One scan of a codebase into an open index — full or incremental.

`spanda index` and `spanda backfill` both need the same thing: read a plan
of files, write each record, resolve what was read, record the cycles. For
a long time that loop lived inside the command-line module, once for
`index` and twice more inline in `backfill`, and the tests that wanted the
engine had to import it from the CLI. This module is the engine and nothing
else. It never prints; a caller that wants progress passes a callback.

Everything here that talks to git says so in its name, and the one function
that can silently fall back — `changed_python_files` returning None — is
paired with `git_failure`, so the caller can say *why* it read everything
instead of quietly doing so.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from spanda.extract import ScanPlan, extract_file, plan_scan, stream_records
from spanda.gaps import external_base_overrides
from spanda.modules import (ModuleIndex, build_import_graph, cycle_groups,
                            resolve_imports)
from spanda.resolve import SymbolTable, build_scopes, resolve_record

#: How often a full scan reports progress, in files.
PROGRESS_EVERY = 200

Progress = Callable[[int, int, int], None]  # (files_done, files_total, symbols)


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def git(root: Path, *args: str, strip: bool = True) -> str | None:
    """Run git and return stdout, or None if it failed.

    `strip=False` matters for `status --porcelain`, whose status field is
    fixed-width and begins with a space for an unstaged change. Stripping the
    output eats that space on the first line only, which shifts the path by
    one character — a bug that hides until the first line happens to be the
    one you care about.
    """
    result = subprocess.run(("git", "-C", str(root)) + args,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() if strip else result.stdout


def git_failure(root: Path, *args: str) -> str:
    """What git said when it refused. Only worth calling on the failure path,
    so the happy path pays nothing for the explanation."""
    result = subprocess.run(("git", "-C", str(root)) + args,
                            capture_output=True, text=True)
    if result.returncode == 0:
        return "it succeeded on retry"
    return (result.stderr.strip() or f"exit status {result.returncode}").splitlines()[0]


def plan_for(root: Path) -> ScanPlan:
    """What a scan will read: everything on disk, minus what git ignores.

    A full scan walks the filesystem and a backfill follows git, and they
    disagree on exactly one thing: a `.py` file that is on disk but ignored.
    A helper script under a gitignored `scripts/` is enough: the full scan
    records it as *added* in a scan labelled "at commit 7e4ae187, clean
    tree", which that commit does not contain. So a git repository is
    scanned as git sees it, and the files set aside are
    counted and reported, not dropped. Outside git nothing changes.

    git is asked about the planned files and only those — `check-ignore`
    over the plan — rather than for a listing of everything it ignores,
    which on a repository with a virtualenv is twenty thousand lines to
    answer a question about one thousand.
    """
    plan = plan_scan(root)
    if not plan.files:
        return plan
    relative = [p.relative_to(plan.root).as_posix() for p in plan.files]
    result = subprocess.run(
        ("git", "-C", str(plan.root), "check-ignore", "--stdin", "-z"),
        input="\0".join(relative), capture_output=True, text=True)
    # 0: some ignored; 1: none ignored; anything else: not a repository, or
    # no git — and then the plan stands as the filesystem gave it.
    if result.returncode not in (0, 1):
        return plan
    ignored = {p for p in result.stdout.split("\0") if p}
    if not ignored:
        return plan
    keep = []
    for path, name in zip(plan.files, relative):
        if name in ignored:
            plan.ignored.append(name)
        else:
            keep.append(path)
    plan.files = keep
    return plan


def changed_python_files(root: Path, since_commit: str,
                         to_commit: str = "HEAD") -> set[str] | None:
    """Which .py files differ between two commits, plus uncommitted edits.

    None means "cannot tell" — no git, or a commit this checkout does not
    have — and the caller must fall back to reading everything. Guessing that
    nothing changed would be the worst possible answer.
    """
    diff = git(root, "diff", "--name-only", since_commit, to_commit)
    if diff is None:
        return None
    # git reports paths relative to the repository root. The scan root may
    # sit below it — `spanda backfill ~/repo/services` — and then every
    # changed path misses the scan's own file list, gets treated as "deleted
    # or not ours", and is carried forward with stale content. Silently.
    top = git(root, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    prefix = Path(root).resolve().relative_to(Path(top).resolve()).as_posix()
    prefix = "" if prefix == "." else prefix + "/"

    def ours(path: str) -> str | None:
        if not path.endswith(".py"):
            return None
        if prefix and not path.startswith(prefix):
            return None  # changed, but outside the scan root
        return path[len(prefix):]

    changed = {p for p in (ours(line) for line in diff.splitlines()) if p}

    # Uncommitted work is a difference too, and git status is the only thing
    # that sees it.
    status = git(root, "status", "--porcelain", "--untracked-files=all",
                 strip=False)
    if status:
        for line in status.splitlines():
            # "XY path", or "XY old -> new" for a rename.
            path = ours(line[3:].split(" -> ")[-1].strip())
            if path:
                changed.add(path)
    return changed


# --------------------------------------------------------------------------
# what resolution needs, and resolution itself
# --------------------------------------------------------------------------

def for_resolution(record: dict) -> dict:
    """What resolution needs from a record, once the symbol table has been fed.

    Keeping this instead of the whole record is what lets the codebase be
    parsed once rather than three times: the full records for 1,097 files run
    to hundreds of megabytes, this to a few tens.
    """
    return {
        "file": record["file"],
        "module": record["module"],
        "dunder_all": record["dunder_all"],
        "imports": record["imports"],
        "references": record["references"],
        "definitions": [{"local_id": d["local_id"], "name": d["name"],
                         "qualname": d["qualname"], "kind": d["kind"],
                         "parent": d["parent"], "bases": d["bases"],
                         "lines": d["lines"],
                         "instance_attributes": d.get("instance_attributes", []),
                         "signature": ({"params": [
                             {"name": p["name"], "annotation": p["annotation"]}
                             for p in d["signature"]["params"]]}
                             if d["signature"] else None)}
                        for d in record["definitions"]],
    }


def cycles_from(collected, module_index) -> list[list[str]]:
    """Circular-import groups from records already in hand."""
    edges = [e for r in collected for e in resolve_imports(r, module_index)]
    return cycle_groups(build_import_graph(edges, list(module_index.by_file)))


def cycles_for(plan: ScanPlan) -> list[list[str]]:
    """Circular-import groups for a plan nobody has read yet — a second pass
    over the files, used only when an incremental scan left cycles
    unrecorded and the caller wants them anyway."""
    module_index, collected = ModuleIndex(), []
    for record in stream_records(plan):
        module_index.add(record["file"], record["module"])
        collected.append(for_resolution(record))
    return cycles_from(collected, module_index)


def override_hints(collected, module_index) -> dict[str, str]:
    """symbol_key -> external base, for the overrides nothing names."""
    from spanda.store import symbol_key
    return {symbol_key(file, qualname, "method"): base
            for file, qualname, _line, base in external_base_overrides(collected, module_index)}


def resolve_collected(collected, module_index, table):
    """Resolve, given records already gathered by whoever parsed them.

    Still two logical passes — the symbol table has to be complete before any
    reference is resolved, since a reference can point at a definition in a
    file read later — but only one pass over the source.
    """
    scopes, lost = build_scopes(collected, table, module_index)
    references = []
    for record in collected:
        references.extend(resolve_record(record, table, scopes))
    return scopes, references, lost


def resolve_codebase(root: Path, patterns):
    """Parse a codebase and resolve it, for callers that have not already
    parsed it themselves."""
    plan = plan_for(root)
    module_index, table, collected = ModuleIndex(), SymbolTable(), []
    for record in stream_records(plan):
        module_index.add(record["file"], record["module"])
        table.add_record(record, patterns)
        collected.append(for_resolution(record))
    scopes, references, lost = resolve_collected(collected, module_index, table)
    return plan, table, scopes, references, lost, override_hints(collected, module_index)


def import_survey(root: Path):
    """Resolve every import in a codebase. Keeps only what the graph needs,
    so memory stays flat rather than holding every record."""
    plan = plan_for(root)
    index, statements = ModuleIndex(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        statements.append({"file": record["file"], "module": record["module"],
                           "imports": record["imports"]})
    edges = []
    for record in statements:
        edges.extend(resolve_imports(record, index))
    return plan, index, edges


# --------------------------------------------------------------------------
# the two ways to run a scan
# --------------------------------------------------------------------------

@dataclass
class FullScan:
    """What a full pass leaves in hand, so nothing has to be read twice."""

    symbols: int
    module_index: ModuleIndex
    table: SymbolTable
    collected: list[dict] = field(default_factory=list)


def full_scan(index, scan_id: int, plan: ScanPlan, patterns,
              progress: Progress | None = None) -> FullScan:
    """Read every planned file into an open scan, and record its cycles.

    One file in memory at a time. The whole scan is a single transaction, so
    an interrupted run leaves the index untouched rather than half-written —
    a half-written scan is indistinguishable from a mass deletion.
    """
    index.record_unread(scan_id, plan)
    run = FullScan(0, ModuleIndex(), SymbolTable())
    for number, record in enumerate(stream_records(plan), start=1):
        run.symbols += index.write_record(scan_id, record, patterns)
        run.module_index.add(record["file"], record["module"])
        run.table.add_record(record, patterns)
        run.collected.append(for_resolution(record))
        if progress and number % PROGRESS_EVERY == 0:
            progress(number, len(plan.files), run.symbols)
    index.record_cycles(scan_id, cycles_from(run.collected, run.module_index))
    return run


def incremental_scan(index, root: Path, scan_id: int, plan: ScanPlan, patterns,
                     changed: set[str]) -> dict:
    """Re-read only what changed, and carry the rest forward.

    Consecutive commits in a large repository differ by a handful of files
    out of a thousand. Re-reading all of them produces identical records for
    almost all of the work, at a cost that grows with the size of the
    codebase rather than with the size of the change.
    """
    index.record_unread(scan_id, plan)
    present = {p.relative_to(plan.root).as_posix(): p for p in plan.files}
    reparsed, symbols = set(), 0

    for relative in sorted(changed):
        path = present.get(relative)
        if path is None:
            continue  # deleted, or not a file this scan would look at anyway
        symbols += index.write_record(scan_id, extract_file(path, plan.root), patterns)
        reparsed.add(relative)

    # A file present now but never indexed before is new to this index, not a
    # carry-forward, so it has to be read even if git did not name it.
    known = {r["file_path"] for r in index.connection.execute(
        "SELECT file_path FROM files")}
    for relative, path in present.items():
        if relative not in known and relative not in reparsed:
            symbols += index.write_record(
                scan_id, extract_file(path, plan.root), patterns)
            reparsed.add(relative)

    carried = index.carry_forward(scan_id, set(present), reparsed)
    return {"reparsed": len(reparsed), "symbols": symbols, **carried}
