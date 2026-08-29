"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from spanda.extract import extract_codebase, plan_scan, stream_records
from spanda.gaps import find_gaps, load_patterns, unreferenced_symbols
from spanda.store import (Index, IndexError_, existing_dbs, latest_db,
                          prepare_db_path)


COLUMNS = [("defs", 6), ("fn", 5), ("cls", 5), ("meth", 6),
           ("var", 5), ("refs", 8), ("open", 8), ("hints", 7)]


def _summarise(scan) -> None:
    records = scan.records
    width = min(max((len(r["file"]) for r in records), default=20) + 2, 62)

    header = f"{'file':<{width}}" + "".join(f"{n:>{w}}" for n, w in COLUMNS)
    print(header)
    print("-" * len(header))

    totals = Counter()
    unparseable = []
    for record in records:
        name = record["file"]
        if len(name) > width - 2:
            name = "..." + name[-(width - 5):]
        if record["parse_status"] != "ok":
            error = record["parse_error"]
            unparseable.append((record["file"], error["line"], error["message"]))
            print(f"{name:<{width}}{'UNPARSEABLE':>{sum(w for _, w in COLUMNS)}}")
            continue
        kinds = Counter(d["kind"] for d in record["definitions"])
        counts = {
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
        totals.update(counts)
        print(f"{name:<{width}}" + "".join(f"{counts[n]:>{w}}" for n, w in COLUMNS))

    print("-" * len(header))
    print(f"{'TOTAL':<{width}}" + "".join(f"{totals[n]:>{w}}" for n, w in COLUMNS))

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

    if args.db:
        db_path, seeded_from = Path(args.db), None
    else:
        db_path, seeded_from = prepare_db_path(root)

    print(f"index: {db_path}")
    if seeded_from:
        print(f"  carried forward from {seeded_from.name}, so symbols keep "
              f"their identity")

    try:
        totals, missing, scan_id = _run_scan(db_path, root, plan, patterns)
    except BaseException:
        # A run that never produced a scan leaves behind only the copy of the
        # previous index it was seeded from. Remove it rather than littering
        # .spanda/ with files that duplicate an existing state.
        if seeded_from is not None and db_path.exists():
            with Index(db_path) as probe:
                empty = not probe.scans()
            if empty:
                db_path.unlink()
                print(f"  discarded empty {db_path.name}", file=sys.stderr)
        raise

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
    """Explicit --db wins; otherwise the newest index for this codebase."""
    if args.db:
        return Path(args.db)
    return latest_db(Path(args.path).resolve())


def cmd_scans(args: argparse.Namespace) -> int:
    db_path = resolve_db(args)
    if db_path is None:
        print(f"no index yet under {Path(args.path).resolve()}/.spanda/ — "
              f"run `spanda index` first")
        return 1
    others = existing_dbs(Path(args.path).resolve())
    print(f"index: {db_path}"
          + (f"   ({len(others)} indexes present, newest shown)" if len(others) > 1 else ""))
    with Index(db_path) as index:
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
    db_path = resolve_db(args)
    if db_path is None:
        print(f"no index yet under {Path(args.path).resolve()}/.spanda/ — "
              f"run `spanda index` first")
        return 1
    with Index(db_path) as index:
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
                           help="pin a specific index file (default: "
                                "<codebase>/.spanda/YYYY-MM-DD-HHMMSS.db)")
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

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except IndexError_ as refusal:
        print(f"refusing: {refusal}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
