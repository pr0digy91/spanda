"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from spanda.extract import extract_codebase, plan_scan, stream_records
from spanda.gaps import find_gaps, load_patterns, unreferenced_symbols
from spanda.drift import compare
from spanda.store import Index, IndexError_, db_path, prepare_db_path


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
        # One file in memory at a time. Peak memory is a function of the
        # largest file, not of the codebase size. The whole scan is a single
        # transaction, so an interrupted run leaves the index untouched
        # rather than half-written — a half-written scan is indistinguishable
        # from a mass deletion.
        for number, record in enumerate(stream_records(plan), start=1):
            symbols += index.write_record(scan_id, record, patterns)
            if number % PROGRESS_EVERY == 0:
                print(f"  {number}/{len(plan.files)} files, {symbols} symbols")

        totals = index.finish_scan(scan_id)
        missing = index.missing_at(scan_id)
    return totals, missing, scan_id



def cmd_index(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    plan = plan_scan(root)
    patterns = load_patterns(Path(args.patterns) if args.patterns else None)

    target = Path(args.db) if args.db else prepare_db_path(root)
    print(f"index: {target}")

    totals, missing, scan_id = _run_scan(target, root, plan, patterns)

    print(f"\nscan {scan_id} complete")
    print(f"  {totals['parsed_files']} files parsed, "
          f"{totals['unparseable_files']} unparseable, "
          f"{totals['skipped_files']} not looked at")
    print(f"  {totals['total_symbols']} symbols "
          f"({totals['ambiguous_symbols']} defined more than once in their file)")
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


def _git(root: Path, *args: str) -> str | None:
    result = subprocess.run(("git", "-C", str(root)) + args,
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


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

    with Index(target, codebase_root=root) as index:
        for number, (commit, when, subject) in enumerate(commits, start=1):
            committed = datetime.fromtimestamp(
                int(when), timezone.utc).isoformat(timespec="seconds")
            with tempfile.TemporaryDirectory() as tmp:
                worktree = Path(tmp) / "tree"
                if _git(root, "worktree", "add", "--detach",
                        str(worktree), commit) is None:
                    print(f"  ! could not check out {commit[:8]}, skipping",
                          file=sys.stderr)
                    continue
                try:
                    plan = plan_scan(worktree)
                    scan_id = index.begin_scan(
                        worktree, plan.skipped_count, record_root=root,
                        commit_override=commit, dirty_override=False,
                        timestamp_override=committed)
                    symbols = 0
                    for record in stream_records(plan):
                        symbols += index.write_record(scan_id, record, patterns)
                    index.finish_scan(scan_id)
                    print(f"  {number:>3}/{len(commits)}  scan {scan_id}  "
                          f"{commit[:8]}  {symbols:>6} symbols  {subject[:44]}")
                finally:
                    _git(root, "worktree", "remove", "--force", str(worktree))

    print(f"\nDone. Compare any two with:  spanda drift {args.path} A B")
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
