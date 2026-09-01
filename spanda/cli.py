"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from spanda.extract import (extract_codebase, extract_file, plan_scan,
                            stream_records)
from spanda.gaps import find_gaps, load_patterns, unreferenced_symbols
from spanda.guide import render as render_guide
from spanda.modules import (EXTERNAL, ModuleIndex, build_import_graph,
                            cycle_groups, processing_order, resolve_imports)
from spanda.resolve import SymbolTable, build_scopes, resolve_record
from spanda.drift import compare
from spanda.store import Index, IndexError_, db_path, prepare_db_path


#: Not this codebase's symbols, so not the resolver's to find.
EXTERNAL_REASONS = ("external_module", "builtin", "module_reference")

COLUMN_NAMES = ["defs", "fn", "cls", "meth", "var", "refs", "open", "hints"]


def _counts_for(record) -> dict:
    kinds = Counter(d["kind"] for d in record["definitions"])
    return {
        "defs": len(record["definitions"]),
        "fn": kinds["function"],
        "cls": kinds["class"],
        "meth": kinds["method"],
        "var": kinds["variable"],
        "refs": len(record["references"]),
        # references still unresolved after local scope: Stage 2's inbox
        "open": sum(1 for r in record["references"] if not r["local"]),
        "hints": len(record["dynamic_hints"]),
    }


def _summarise(scan) -> None:
    records = scan.records
    width = min(max((len(r["file"]) for r in records), default=20) + 2, 62)

    # Size each column to its largest value. Fixed widths silently run the
    # numbers together on a real codebase, which is where the summary is
    # needed most.
    rows = [_counts_for(r) for r in records if r["parse_status"] == "ok"]
    totals = Counter()
    for row in rows:
        totals.update(row)
    columns = [(name, max(len(name), len(f"{totals[name]:,}")) + 2)
               for name in COLUMN_NAMES]

    header = f"{'file':<{width}}" + "".join(f"{n:>{w}}" for n, w in columns)
    print(header)
    print("-" * len(header))

    unparseable = []
    for record in records:
        name = record["file"]
        if len(name) > width - 2:
            name = "..." + name[-(width - 5):]
        if record["parse_status"] != "ok":
            error = record["parse_error"]
            unparseable.append((record["file"], error["line"], error["message"]))
            print(f"{name:<{width}}{'UNPARSEABLE':>{sum(w for _, w in columns)}}")
            continue
        counts = _counts_for(record)
        print(f"{name:<{width}}"
              + "".join(f"{counts[n]:>{w},}" for n, w in columns))

    print("-" * len(header))
    print(f"{'TOTAL':<{width}}"
          + "".join(f"{totals[n]:>{w},}" for n, w in columns))

    print(f"\n{len(records)} files parsed, {len(unparseable)} unparseable")
    for name, line, message in unparseable:
        print(f"  ! {name}:{line}  {message}")
    if unparseable:
        # An interpreter older than the code it is reading rejects valid
        # syntax. Naming it turns a baffling failure into an obvious one.
        print(f"    (read by Python {platform.python_version()} — a file using "
              f"newer syntax than this\n     will fail here even though it is "
              f"valid; run spanda on a newer Python)")

    # A skipped file is a gap in what this tool knows. Say so out loud.
    if scan.skipped:
        print(f"\n{scan.skipped_count} files NOT looked at, excluded by directory name:")
        for reason, paths in sorted(scan.skipped.items(),
                                    key=lambda kv: -len(kv[1])):
            print(f"  {len(paths):>6}  {reason}/")


def _dynamic_dispatch_report(records: list[dict]) -> None:
    """Every decorator seen, so the dynamic-dispatch pattern list can be built
    from what is actually in the codebase rather than guessed at."""
    decorators = Counter()
    for record in records:
        for definition in record["definitions"]:
            for decorator in definition["decorators"]:
                decorators[decorator["base"] or decorator["raw"]] += 1
    if not decorators:
        return
    print("\ndecorators in use (Stage 2 decides which of these mean dynamic dispatch):")
    for base, count in decorators.most_common():
        print(f"  {count:>3}  @{base}")


def cmd_parse(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    scan = extract_codebase(root)

    if args.out:
        out_root = Path(args.out).resolve()
        for record in scan.records:
            destination = out_root / (record["file"] + ".json")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(record, indent=2, sort_keys=False) + "\n")
        print(f"wrote {len(scan.records)} JSON files to {out_root}\n")

    if not args.quiet:
        _summarise(scan)
        _dynamic_dispatch_report(scan.records)
    return 0


GAP_HEADINGS = {
    "dynamic_dispatch_decorator":
        "Decorated with something that dispatches at runtime — the framework "
        "calls these,\n  and no reference in this codebase names them:",
    "runtime_attribute_access":
        "Call sites that pick their target at runtime — the site is certain, "
        "the target is not:",
    "dynamic_import":
        "Modules loaded by calling importlib rather than by an import statement. "
        "Whatever\n  they load has no static importer, so it will look unreferenced:",
    "name_in_string_literal":
        "String literals that spell the name of a symbol defined elsewhere "
        "(heuristic —\n  a name match is not a call, and must never become an edge):",
}


def cmd_gaps(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    scan = extract_codebase(root)
    patterns = load_patterns(Path(args.patterns) if args.patterns else None)
    gaps = find_gaps(scan, patterns)

    total_symbols = sum(len(r["definitions"]) for r in scan.records)
    print(f"{len(scan.records)} files, {total_symbols} symbols, "
          f"{scan.skipped_count} files not looked at\n")

    if not gaps:
        print("No gaps found. Treat that with suspicion rather than relief: it "
              "more likely\nmeans the pattern list needs extending than that "
              "the codebase has none.")
    for kind, heading in GAP_HEADINGS.items():
        found = [g for g in gaps if g.kind == kind]
        if not found:
            continue
        print(f"{heading}\n")
        for gap in found:
            print(f"  {gap.file}:{gap.line}")
            print(f"      {gap.symbol}")
            print(f"      {gap.detail}")
        print(f"  ({len(found)})\n")

    if args.unreferenced:
        orphans = unreferenced_symbols(scan)
        # A symbol with no references is only dead if nothing else explains
        # the silence. Cross-referencing the gap list is what separates
        # "probably unused" from "called by something this tool cannot see" —
        # and reporting the second as the first is the failure this whole
        # project exists to avoid.
        explained = {(g.file, g.symbol): g for g in gaps
                     if g.kind == "dynamic_dispatch_decorator"}
        by_name = {g.detail.split('"')[1]: g for g in gaps
                   if g.kind == "name_in_string_literal"}

        unexplained = []
        accounted = []
        for file, line, qualname in orphans:
            gap = explained.get((file, qualname)) or by_name.get(qualname.split(".")[-1])
            (accounted if gap else unexplained).append((file, line, qualname, gap))

        print(f"Symbols with no reference anywhere: {len(orphans)}\n")

        if accounted:
            print(f"  Silence explained ({len(accounted)}) — something calls these that "
                  "this tool\n  cannot see. Do NOT read these as unused:\n")
            for file, line, qualname, gap in accounted:
                print(f"    {file}:{line}  {qualname}")
                print(f"        {gap.detail}")
            print()

        print(f"  No explanation found ({len(unexplained)}) — possibly unused, "
              "but this is a\n  name match over one codebase, not proof. "
              "Entry points, tests and callers\n  outside this tree are all "
              "invisible from here:\n")
        for file, line, qualname, _ in unexplained:
            print(f"    {file}:{line}  {qualname}")

    return 0


PROGRESS_EVERY = 200


def _run_scan(db_path, root, plan, patterns):
    with Index(db_path, codebase_root=root) as index:
        scan_id = index.begin_scan(plan.root, plan.skipped_count)
        print(f"scan {scan_id}: {len(plan.files)} files under {plan.root}")

        symbols = 0
        module_index, table, collected = ModuleIndex(), SymbolTable(), []
        # One file in memory at a time. The whole scan is a single
        # transaction, so an interrupted run leaves the index untouched
        # rather than half-written — a half-written scan is indistinguishable
        # from a mass deletion.
        for number, record in enumerate(stream_records(plan), start=1):
            symbols += index.write_record(scan_id, record, patterns)
            module_index.add(record["file"], record["module"])
            table.add_record(record, patterns)
            collected.append(_for_resolution(record))
            if number % PROGRESS_EVERY == 0:
                print(f"  {number}/{len(plan.files)} files, {symbols} symbols")

        # Resolved from what was just read, not by reading it again.
        _scopes, references, lost = _resolve_collected(collected, module_index, table)
        counts = index.write_references(
            scan_id, references, index.symbol_uuids_by_key())
        index.record_lost_trails(scan_id, lost)
        counts["lost"] = lost

        totals = index.finish_scan(scan_id)
        missing = index.missing_at(scan_id)
    return totals, missing, scan_id, counts



def cmd_index(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    plan = plan_scan(root)
    patterns = load_patterns(Path(args.patterns) if args.patterns else None)

    target = Path(args.db) if args.db else prepare_db_path(root)
    print(f"index: {target}")

    totals, missing, scan_id, counts = _run_scan(target, root, plan, patterns)

    print(f"\nscan {scan_id} complete")
    print(f"  {totals['parsed_files']} files parsed, "
          f"{totals['unparseable_files']} unparseable, "
          f"{totals['skipped_files']} not looked at")
    print(f"  {totals['total_symbols']} symbols "
          f"({totals['ambiguous_symbols']} defined more than once in their file)")
    print(f"  {counts['edges']} references resolved to a definition, "
          f"{counts['unresolved']} could not be")
    _report_lost_trails(counts.get("lost", []))
    print(f"  content fingerprint {totals['content_fingerprint']}")
    if totals["git_commit_hash"]:
        print(f"  at commit {totals['git_commit_hash'][:12]} (clean tree)")
    elif totals["git_base_commit"]:
        print(f"  based on {totals['git_base_commit'][:12]} PLUS uncommitted "
              f"changes — recorded as its own state, not as that commit")
    else:
        print("  not a git repository — incremental re-index will not be available")

    if missing:
        print(f"\n  {len(missing)} symbols known to the index were not seen "
              f"in this scan:")
        for row in missing[:10]:
            print(f"    {row['file_path']}:{row['line_start']}  {row['qualname']}"
                  f"  (last seen in scan {row['last_seen_scan_id']})")
        if len(missing) > 10:
            print(f"    ... and {len(missing) - 10} more")
    return 0


def resolve_db(args: argparse.Namespace) -> Path | None:
    """Explicit --db wins; otherwise this codebase's one index."""
    target = Path(args.db) if args.db else db_path(Path(args.path).resolve())
    return target if target.exists() else None


def cmd_scans(args: argparse.Namespace) -> int:
    target = resolve_db(args)
    if target is None:
        print(f"no index yet at {db_path(Path(args.path).resolve())} — "
              f"run `spanda index` first")
        return 1
    print(f"index: {target}")
    with Index(target) as index:
        rows = index.scans()
    if not rows:
        print("no scans in this index yet")
        return 0
    header = (f"{'id':>4}  {'when':<26} {'commit':<10} {'files':>7} "
              f"{'symbols':>8}  status")
    print(header)
    print("-" * len(header))
    for row in rows:
        commit = (row["git_commit_hash"] or "-")[:8]
        if row["git_dirty"]:
            commit += "*"
        status = "complete" if row["completed"] else "INTERRUPTED — partial"
        print(f"{row['scan_id']:>4}  {row['timestamp']:<26} {commit:<10} "
              f"{row['total_files']:>7} {row['total_symbols']:>8}  {status}")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    target = resolve_db(args)
    if target is None:
        print(f"no index yet at {db_path(Path(args.path).resolve())} — "
              f"run `spanda index` first")
        return 1
    with Index(target) as index:
        rows = index.find(args.pattern.replace("*", "%"))
        scans = index.scans()
    latest = max((s["scan_id"] for s in scans), default=0)
    if not rows:
        print(f"nothing matching {args.pattern!r}")
        return 0
    for row in rows:
        flags = []
        if row["has_dynamic_dispatch"]:
            flags.append("dynamic-dispatch")
        if row["definition_count"] > 1:
            flags.append(f"defined x{row['definition_count']}")
        if row["last_seen_scan_id"] < latest:
            flags.append(f"NOT SEEN since scan {row['last_seen_scan_id']}")
        suffix = ("  [" + ", ".join(flags) + "]") if flags else ""
        print(f"{row['file_path']}:{row['line_start']}  {row['kind']:<9} "
              f"{row['qualname']}{suffix}")
        print(f"    {row['symbol_key']}")
    return 0


def _bullet(change) -> str:
    warning = "  [callers not statically knowable]" if change.callers_unknowable else ""
    return f"    {change.file_path}:{change.line_start}  {change.qualname}{warning}"


def cmd_drift(args: argparse.Namespace) -> int:
    target = resolve_db(args)
    if target is None:
        print(f"no index yet at {db_path(Path(args.path).resolve())} — "
              f"run `spanda index` first")
        return 1

    with Index(target) as index:
        scans = index.scans(complete_only=True)
        if len(scans) < 2:
            print(f"only {len(scans)} completed scan(s) in this index; "
                  f"drift needs two.\nRun `spanda index` again after a change, "
                  f"or `spanda backfill` to index past commits.")
            return 1
        first = args.scan_a if args.scan_a is not None else scans[-2]["scan_id"]
        second = args.scan_b if args.scan_b is not None else scans[-1]["scan_id"]
        report = compare(index, first, second)

    a, b = report.scan_a, report.scan_b

    def label(scan) -> str:
        if scan["git_commit_hash"]:
            return f"scan {scan['scan_id']} ({scan['git_commit_hash'][:8]})"
        if scan["git_base_commit"]:
            return f"scan {scan['scan_id']} ({scan['git_base_commit'][:8]}+dirty)"
        return f"scan {scan['scan_id']}"

    print(f"{label(a)}  →  {label(b)}")
    print(f"{a['timestamp']}  →  {b['timestamp']}\n")

    if report.identical:
        print("Nothing changed.")
    else:
        headline = []
        if report.shape:
            headline.append(f"{len(report.shape)} changed shape")
        if report.removed:
            headline.append(f"{len(report.removed)} removed")
        if report.added:
            headline.append(f"{len(report.added)} added")
        if report.internal:
            headline.append(f"{len(report.internal)} changed internally")
        print(", ".join(headline).capitalize() + ".")
        print(f"{report.unchanged_count} symbols untouched.\n")

    if report.shape:
        print("SHAPE CHANGED — callers of these may break\n")
        for change in report.shape:
            print(_bullet(change))
            print(f"        was  {change.before}")
            print(f"        now  {change.after}")
        print()

    if report.removed:
        print("REMOVED\n")
        for change in report.removed:
            print(_bullet(change))
        print()

    if report.added:
        print("ADDED\n")
        for change in report.added:
            print(_bullet(change))
        print()

    if report.internal and not args.brief:
        print("CHANGED INTERNALLY — callers unaffected\n")
        for change in report.internal:
            print(_bullet(change))
        print()
    elif report.internal:
        print(f"({len(report.internal)} internal-only changes hidden; "
              f"drop --brief to list them)\n")

    if report.caveats:
        print("Caveats\n")
        for note in report.caveats:
            print(f"  · {note}")
        print()

    unknowable = [c for c in report.shape if c.callers_unknowable]
    if unknowable:
        print(f"{len(unknowable)} of the shape changes are on symbols whose "
              f"callers cannot be\ndetermined by reading the code. Reading the "
              f"call sites will not find them.")
    return 0


def changed_python_files(root: Path, since_commit: str,
                         to_commit: str = "HEAD") -> set[str] | None:
    """Which .py files differ between two commits, plus uncommitted edits.

    None means "cannot tell" — no git, or a commit this checkout does not
    have — and the caller must fall back to reading everything. Guessing that
    nothing changed would be the worst possible answer.
    """
    diff = _git(root, "diff", "--name-only", since_commit, to_commit)
    if diff is None:
        return None
    changed = {line for line in diff.splitlines() if line.endswith(".py")}

    # Uncommitted work is a difference too, and git status is the only thing
    # that sees it.
    status = _git(root, "status", "--porcelain", "--untracked-files=all",
                  strip=False)
    if status:
        for line in status.splitlines():
            # "XY path", or "XY old -> new" for a rename.
            path = line[3:].split(" -> ")[-1].strip()
            if path.endswith(".py"):
                changed.add(path)
    return changed


def _incremental_scan(index, root: Path, scan_id: int, plan, patterns,
                      changed: set[str]) -> dict:
    """Re-read only what changed, and carry the rest forward.

    On the target codebase consecutive commits differ by a median of three files out of
    1,098. Re-reading all of them costs 1.5 seconds a commit and produces
    identical results for 99.7% of the work.
    """
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


def _git(root: Path, *args: str, strip: bool = True) -> str | None:
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


def cmd_backfill(args: argparse.Namespace) -> int:
    """Index past commits, so drift can be tested against real history today
    rather than after weeks of accumulating scans."""
    root = Path(args.path).resolve()
    if _git(root, "rev-parse", "HEAD") is None:
        print(f"{root} is not a git repository; backfill has no history to read",
              file=sys.stderr)
        return 2

    target = Path(args.db) if args.db else prepare_db_path(root)
    with Index(target, codebase_root=root) as index:
        if index.scans():
            print(f"{target} already holds scans. Backfill writes history in "
                  f"chronological order,\nso it needs an empty index — later "
                  f"scans would otherwise carry earlier commits.\nDelete it and "
                  f"re-run, or pass --db to a new file.", file=sys.stderr)
            return 2

    log = _git(root, "log", "-n", str(args.last), "--format=%H %ct %s")
    commits = [line.split(" ", 2) for line in (log or "").splitlines()]
    commits.reverse()  # oldest first, so scan_id order matches time order
    if not commits:
        print("no commits found", file=sys.stderr)
        return 2

    patterns = load_patterns(Path(args.patterns) if args.patterns else None)
    print(f"backfilling {len(commits)} commits into {target}\n")

    # One worktree for the whole run, checked out from commit to commit.
    # Adding and removing a worktree per commit rewrites all 1,098 files each
    # time; checking out between two neighbouring commits writes only the
    # handful that differ.
    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp) / "tree"
        if _git(root, "worktree", "add", "--detach", str(worktree),
                commits[0][0]) is None:
            print(f"could not create a worktree", file=sys.stderr)
            return 2
        try:
            with Index(target, codebase_root=root) as index:
                previous_commit, previous_scan = None, None
                for number, (commit, when, subject) in enumerate(commits, start=1):
                    if _git(worktree, "checkout", "--detach", "--force",
                            commit) is None:
                        print(f"  ! could not check out {commit[:8]}, skipping",
                              file=sys.stderr)
                        continue
                    committed = datetime.fromtimestamp(
                        int(when), timezone.utc).isoformat(timespec="seconds")
                    plan = plan_scan(worktree)
                    scan_id = index.begin_scan(
                        worktree, plan.skipped_count, record_root=root,
                        commit_override=commit, dirty_override=False,
                        timestamp_override=committed)

                    changed = (changed_python_files(worktree, previous_commit, commit)
                               if previous_commit else None)
                    if changed is None or previous_scan is None:
                        symbols = sum(index.write_record(scan_id, record, patterns)
                                      for record in stream_records(plan))
                        touched = len(plan.files)
                    else:
                        result = _incremental_scan(
                            index, worktree, scan_id, plan, patterns, changed)
                        symbols, touched = result["symbols"], result["reparsed"]

                    # Only the newest commit gets its references resolved.
                    # Doing it for all of them would dominate the run for
                    # edges nobody asks about at a historical commit.
                    if number == len(commits):
                        _p, _t, _s, references, lost = _resolve_codebase(worktree, patterns)
                        index.write_references(
                            scan_id, references, index.symbol_uuids_by_key())
                        index.record_lost_trails(scan_id, lost)

                    totals = index.finish_scan(scan_id)
                    previous_commit, previous_scan = commit, scan_id
                    if number % 25 == 0 or number == len(commits):
                        print(f"  {number:>4}/{len(commits)}  {commit[:8]}  "
                              f"{totals['total_symbols']:>6} symbols  "
                              f"{touched:>4} files re-read  {subject[:36]}")
        finally:
            _git(root, "worktree", "remove", "--force", str(worktree))

    print(f"\nDone. Compare any two with:  spanda drift {args.path} A B")
    return 0


def _import_survey(root: Path):
    """Resolve every import in a codebase. Keeps only what the graph needs,
    so memory stays flat rather than holding every record."""
    plan = plan_scan(root)
    index, statements = ModuleIndex(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        statements.append({"file": record["file"], "module": record["module"],
                           "imports": record["imports"]})
    edges = []
    for record in statements:
        edges.extend(resolve_imports(record, index))
    return plan, index, edges


#: What resolution needs from a record, once the symbol table has been fed.
#: Keeping this instead of the whole record is what lets the codebase be
#: parsed once rather than three times: the full records for 1,097 files run
#: to hundreds of megabytes, this to a few tens.
def _for_resolution(record: dict) -> dict:
    return {
        "file": record["file"],
        "module": record["module"],
        "dunder_all": record["dunder_all"],
        "imports": record["imports"],
        "references": record["references"],
        "definitions": [{"local_id": d["local_id"], "name": d["name"],
                         "qualname": d["qualname"], "kind": d["kind"],
                         "parent": d["parent"],
                         "signature": ({"params": [
                             {"name": p["name"], "annotation": p["annotation"]}
                             for p in d["signature"]["params"]]}
                             if d["signature"] else None)}
                        for d in record["definitions"]],
    }


def _resolve_collected(collected, module_index, table):
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


def _resolve_codebase(root: Path, patterns):
    """Parse a codebase and resolve it, for callers that have not already
    parsed it themselves."""
    plan = plan_scan(root)
    module_index, table, collected = ModuleIndex(), SymbolTable(), []
    for record in stream_records(plan):
        module_index.add(record["file"], record["module"])
        table.add_record(record, patterns)
        collected.append(_for_resolution(record))
    scopes, references, lost = _resolve_collected(collected, module_index, table)
    return plan, table, scopes, references, lost


def _report_lost_trails(lost) -> None:
    """The resolver's self-audit, printed where it cannot be missed.

    Zero is the expected reading. Anything else means the tool imported a
    name and then could not say what it was — and every "no callers" answer
    from this run deserves suspicion until the cause is found.
    """
    if not lost:
        print("  self-audit: every name brought in by an import statement was "
              "traced to its definition\n  (imports done by calling importlib "
              "are not statements; `spanda gaps` lists those)")
        return
    print(f"\n  SELF-AUDIT: {len(lost)} imported name(s) whose definition could "
          f"not be found.")
    print("  These are trails the resolver lost, not unused imports. Symbols "
          "behind them\n  will wrongly look uncalled until this is fixed:")
    for trail in lost[:8]:
        print(f"      {trail.source_file}:{trail.line}  {trail.raw}")
    if len(lost) > 8:
        print(f"      ... and {len(lost) - 8} more")


def cmd_resolve(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    patterns = load_patterns(Path(args.patterns) if args.patterns else None)
    plan, table, _scopes, references, lost = _resolve_codebase(root, patterns)

    resolved = [r for r in references if r.target_symbol]
    unresolved = [r for r in references if not r.target_symbol]
    by_reason = Counter(r.reason for r in unresolved)

    print(f"{len(plan.files)} files, {len(table.symbols)} symbols, "
          f"{len(references)} references\n")
    print(f"  {len(resolved)} resolved to a definition in this codebase")
    print(f"  {len(unresolved)} not resolved, by reason:")
    for reason, count in by_reason.most_common():
        print(f"      {count:>7}  {reason}")

    share = len(resolved) / max(len(references) - sum(
        by_reason[x] for x in EXTERNAL_REASONS), 1)
    print(f"\n  Of references that could point at this codebase, "
          f"{share:.0%} were resolved.")
    print(f"  The rest are listed above with a reason — none are dropped.")

    edges = Counter(r.edge_type for r in resolved)
    print(f"\n  edges by type:")
    for kind, count in edges.most_common():
        print(f"      {count:>7}  {kind}")
    print()
    _report_lost_trails(lost)

    if args.reasons:
        print()
        for reason in by_reason:
            examples = [r for r in unresolved if r.reason == reason][:args.reasons]
            print(f"  {reason}:")
            for r in examples:
                print(f"      {r.source_file}:{r.line}  {r.raw}")
    return 0


def cmd_imports(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    plan, index, edges = _import_survey(root)
    graph = build_import_graph(edges, list(index.by_file))
    cycles = cycle_groups(graph)

    internal = [e for e in edges if e.target_file]
    external = [e for e in edges if e.reason == EXTERNAL]
    unresolved = [e for e in edges if e.reason and e.reason != EXTERNAL]

    print(f"{len(plan.files)} files, {len(edges)} import statements")
    print(f"  {len(internal)} resolved to a file in this codebase")
    print(f"  {len(external)} point outside it (stdlib or installed packages)")
    if unresolved:
        print(f"  {len(unresolved)} look internal but match no file — "
              f"a gap in this tool, not in the code")
        for edge in unresolved[:8]:
            print(f"      {edge.source_file}:{edge.line}  {edge.raw}  [{edge.reason}]")

    print(f"\n{len(cycles)} circular import group(s)")
    for group in cycles:
        print(f"\n  {len(group)} files importing each other:")
        for member in group:
            print(f"      {member}")

    if args.order:
        units = processing_order(graph)
        print(f"\n\nprocessing order — {len(units)} units, dependencies first\n")
        for number, unit in enumerate(units, start=1):
            if len(unit) == 1:
                print(f"  {number:>4}  {unit[0]}")
            else:
                print(f"  {number:>4}  [cycle group, order within is arbitrary]")
                for member in unit:
                    print(f"        {member}")
    return 0


def cmd_callers(args: argparse.Namespace) -> int:
    target = resolve_db(args)
    if target is None:
        print(f"no index yet at {db_path(Path(args.path).resolve())} — "
              f"run `spanda index` first")
        return 1

    with Index(target) as index:
        matches = index.find(args.name.replace("*", "%"), limit=20)
        if not matches:
            print(f"no symbol named {args.name!r} in this index")
            return 1

        for symbol in matches:
            callers = index.callers_of(symbol["uuid"])
            maybe = index.possible_callers_of(symbol["name"])

            print(f"\n{symbol['file_path']}:{symbol['line_start']}  "
                  f"{symbol['kind']} {symbol['qualname']}")
            print(f"    {symbol['canonical_signature']}")

            if callers:
                print(f"\n  {len(callers)} static caller(s):")
                for row in callers:
                    where = (f"{row['file_path']}:{row['line_start']}  {row['qualname']}"
                             if row["qualname"] else
                             f"{row['source_file']}  <module-level code>")
                    print(f"      {row['edge_type']:<9} {where}")
            else:
                print("\n  no static callers found")

            if symbol["has_dynamic_dispatch"]:
                print("\n  ...but this symbol is dispatched at runtime. Whatever "
                      "calls it is not\n     visible in the source, so the count "
                      "above is not the whole story.")

            if maybe:
                print(f"\n  plus {len(maybe)} place(s) reaching for the name "
                      f"'{symbol['name']}' on an object\n     whose type could "
                      f"not be determined — each may or may not be this symbol:")
                for row in maybe[:args.limit]:
                    print(f"      {row['source_file']}:{row['line']}  {row['raw']}")
                if len(maybe) > args.limit:
                    print(f"      ... and {len(maybe) - args.limit} more")

            if not callers and not symbol["has_dynamic_dispatch"] and not maybe:
                print("\n  Nothing references it, and nothing explains the "
                      "silence. Possibly unused —\n     though entry points and "
                      "callers outside this codebase are invisible here.")
    return 0


def cmd_guide(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    target = resolve_db(args)
    if target is None:
        print(f"no index yet at {db_path(root)} — run `spanda index` first",
              file=sys.stderr)
        return 1

    with Index(target) as index:
        text = render_guide(index, root)

    if args.write:
        destination = target.parent / "README.md"
        destination.write_text(text)
        print(f"wrote {destination}")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spanda",
        description="Deterministic static-analysis indexing engine for Python.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse = subparsers.add_parser(
        "parse", help="Stage 1: extract definitions and references, one file at a time")
    parse.add_argument("path", help="root of the codebase to parse")
    parse.add_argument("--out", help="directory to write one JSON record per source file")
    parse.add_argument("--quiet", action="store_true", help="suppress the summary")
    parse.set_defaults(func=cmd_parse)

    gaps = subparsers.add_parser(
        "gaps", help="what static analysis cannot see in this codebase")
    gaps.add_argument("path", help="root of the codebase to inspect")
    gaps.add_argument("--patterns", help="override the dynamic-dispatch pattern file")
    gaps.add_argument("--unreferenced", action="store_true",
                      help="also list symbols whose name appears in no reference")
    gaps.set_defaults(func=cmd_gaps)

    index_cmd = subparsers.add_parser(
        "index", help="Stage 4: parse a codebase and store it in SQLite")
    index_cmd.add_argument("path", help="root of the codebase to index")
    index_cmd.add_argument("--db", default=None,
                           help="override the index location "
                                "(default: <codebase>/.spanda/index.db)")
    index_cmd.add_argument("--patterns", help="override the dynamic-dispatch pattern file")
    index_cmd.set_defaults(func=cmd_index)

    scans_cmd = subparsers.add_parser("scans", help="list the scans in an index")
    scans_cmd.add_argument("path", help="root of the indexed codebase")
    scans_cmd.add_argument("--db", default=None, help="pin a specific index file")
    scans_cmd.set_defaults(func=cmd_scans)

    find_cmd = subparsers.add_parser("find", help="look up symbols by name")
    find_cmd.add_argument("path", help="root of the indexed codebase")
    find_cmd.add_argument("pattern", help="name or qualname, * allowed as a wildcard")
    find_cmd.add_argument("--db", default=None, help="pin a specific index file")
    find_cmd.set_defaults(func=cmd_find)

    imports_cmd = subparsers.add_parser(
        "imports", help="Stage 0: resolve imports, find circular import groups")
    imports_cmd.add_argument("path", help="root of the codebase")
    imports_cmd.add_argument("--order", action="store_true",
                             help="also print the full processing order")
    imports_cmd.set_defaults(func=cmd_imports)

    resolve_cmd = subparsers.add_parser(
        "resolve", help="Stage 2: link references to the definitions they name")
    resolve_cmd.add_argument("path", help="root of the codebase")
    resolve_cmd.add_argument("--patterns", help="override the dynamic-dispatch pattern file")
    resolve_cmd.add_argument("--reasons", type=int, default=0, metavar="N",
                             help="show N examples of each unresolved reason")
    resolve_cmd.set_defaults(func=cmd_resolve)

    guide_cmd = subparsers.add_parser(
        "guide", help="a note on reading this index, with its own numbers filled in")
    guide_cmd.add_argument("path", help="root of the indexed codebase")
    guide_cmd.add_argument("--write", action="store_true",
                           help="write it to .spanda/README.md beside the index")
    guide_cmd.add_argument("--db", default=None, help="pin a specific index file")
    guide_cmd.set_defaults(func=cmd_guide)

    callers_cmd = subparsers.add_parser(
        "callers", help="what calls a symbol, and what might but cannot be proven to")
    callers_cmd.add_argument("path", help="root of the indexed codebase")
    callers_cmd.add_argument("name", help="symbol name, * allowed as a wildcard")
    callers_cmd.add_argument("--limit", type=int, default=10,
                             help="how many unprovable call sites to list")
    callers_cmd.add_argument("--db", default=None, help="pin a specific index file")
    callers_cmd.set_defaults(func=cmd_callers)

    drift_cmd = subparsers.add_parser(
        "drift", help="what changed between two scans")
    drift_cmd.add_argument("path", help="root of the indexed codebase")
    drift_cmd.add_argument("scan_a", nargs="?", type=int, default=None,
                           help="older scan (default: second-newest)")
    drift_cmd.add_argument("scan_b", nargs="?", type=int, default=None,
                           help="newer scan (default: newest)")
    drift_cmd.add_argument("--brief", action="store_true",
                           help="hide the internal-only changes")
    drift_cmd.add_argument("--db", default=None, help="pin a specific index file")
    drift_cmd.set_defaults(func=cmd_drift)

    backfill_cmd = subparsers.add_parser(
        "backfill", help="index past git commits into a fresh index")
    backfill_cmd.add_argument("path", help="root of the codebase (a git repository)")
    backfill_cmd.add_argument("--last", type=int, default=10,
                              help="how many commits back to index (default: 10)")
    backfill_cmd.add_argument("--patterns", help="override the dynamic-dispatch pattern file")
    backfill_cmd.add_argument("--db", default=None, help="pin a specific index file")
    backfill_cmd.set_defaults(func=cmd_backfill)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except IndexError_ as refusal:
        print(f"refusing: {refusal}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
