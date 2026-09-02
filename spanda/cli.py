"""Command line entry point: parse arguments, run one thing, print.

The scan engine the commands share lives in `spanda.scan`; nothing here
reads a source file itself."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from spanda.drift import compare
from spanda.extract import extract_codebase
from spanda.gaps import find_gaps, load_patterns, unreferenced_symbols
from spanda.guide import render as render_guide
from spanda.loops import build as build_loops, render as render_loops
from spanda.modules import (EXTERNAL, build_import_graph, cycle_groups,
                            processing_order)
from spanda.profile import build as build_profile, render as render_profile
from spanda import verdicts as verdicts_module
from spanda.scan import (changed_python_files, cycles_for, full_scan, git,
                         git_failure, import_survey, incremental_scan,
                         override_hints, plan_for, resolve_codebase,
                         resolve_collected)
from spanda.store import SCHEMA_VERSION, Index, IndexError_, db_path, prepare_db_path


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
    if scan.ignored:
        print(f"\n{len(scan.ignored)} .py files NOT looked at because git ignores them:")
        for path in scan.ignored[:10]:
            print(f"  {path}")
        if len(scan.ignored) > 10:
            print(f"  ... and {len(scan.ignored) - 10} more")


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

    scan = extract_codebase(root, plan_for(root))

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
    "framework_method_override":
        "Methods a framework calls by name on a subclass of its own base — no "
        "decorator\n  marks them, nothing here calls them, and the base is outside "
        "this codebase:",
    "framework_owned_class":
        "Classes a framework owns by inheritance — a mapped table, a model the "
        "framework\n  registers. Alive whether or not Python names them:",
    "unknown_decorator":
        "Decorated with something on neither list, and nothing names them. Not "
        "a claim\n  that a framework calls these — a statement that spanda does "
        "not know. Vet, then\n  add a line to dynamic_dispatch.txt either way:",
    "override_on_external_base":
        "Public methods nothing names, on classes whose base is outside this "
        "codebase.\n  Whatever the base's framework is, this is the shape it "
        "calls through (lower\n  confidence than a pattern match):",
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

    scan = extract_codebase(root, plan_for(root))
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


def _run_scan(db_path, root, plan, patterns):
    """Open the index, run one full scan, resolve it, and say what happened."""
    with Index(db_path, codebase_root=root) as index:
        if index.migrated_from:
            print(f"index brought forward from schema {index.migrated_from} "
                  f"to {SCHEMA_VERSION}; this scan fills in what the new "
                  f"columns need")
        scan_id = index.begin_scan(plan.root, plan.skipped_count)
        print(f"scan {scan_id}: {len(plan.files)} files under {plan.root}")

        run = full_scan(index, scan_id, plan, patterns,
                        progress=lambda done, total, symbols: print(
                            f"  {done}/{total} files, {symbols} symbols"))
        # Resolved from what was just read, not by reading it again.
        _scopes, references, lost = resolve_collected(
            run.collected, run.module_index, run.table)
        counts = index.write_references(
            scan_id, references, index.symbol_uuids_by_key())
        index.record_lost_trails(scan_id, lost)
        counts["lost"] = lost
        index.set_external_base_hints(
            scan_id, override_hints(run.collected, run.module_index))

        totals = index.finish_scan(scan_id)
        missing = index.gone_since_previous(scan_id)
    return totals, missing, scan_id, counts


def cmd_index(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    plan = plan_for(root)
    patterns = load_patterns(Path(args.patterns) if args.patterns else None)

    target = Path(args.db) if args.db else prepare_db_path(root)
    print(f"index: {target}")

    totals, missing, scan_id, counts = _run_scan(target, root, plan, patterns)

    print(f"\nscan {scan_id} complete")
    print(f"  {totals['parsed_files']} files parsed, "
          f"{totals['unparseable_files']} unparseable, "
          f"{totals['skipped_files']} not looked at"
          + (f" ({len(plan.ignored)} of them .py files git ignores: "
             f"{', '.join(plan.ignored[:3])}"
             f"{', ...' if len(plan.ignored) > 3 else ''})" if plan.ignored else ""))
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
        print(f"\n  {len(missing)} symbols seen by the previous scan are gone "
              f"in this one:")
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
        history = index.migrations()
    print(f"schema {SCHEMA_VERSION}"
          + ("; brought forward " + ", ".join(
              f"from {m['from']} on {m['when'][:10]}" for m in history)
             if history else ""))
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
        if report.edges_added or report.edges_removed:
            headline.append(f"{len(report.edges_added)} references gained, "
                            f"{len(report.edges_removed)} lost")
        if report.cycles_appeared or report.cycles_gone:
            headline.append(f"{len(report.cycles_appeared)} circular-import "
                            f"group(s) appeared, {len(report.cycles_gone)} dissolved")
        if report.loops_deeper or report.loops_shallower:
            headline.append(f"{len(report.loops_deeper)} nest loops deeper, "
                            f"{len(report.loops_shallower)} shallower")
        if report.decorators_appeared:
            unknown = sum(1 for _b, k, _c in report.decorators_appeared if k == "unknown")
            headline.append(f"{len(report.decorators_appeared)} decorator(s) seen for "
                            f"the first time" + (f", {unknown} on no list" if unknown else ""))
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

    if report.decorators_appeared:
        print("DECORATORS SEEN FOR THE FIRST TIME — a new framework, or a new way of "
              "calling\n")
        for base, kind, count in report.decorators_appeared:
            note = {"dispatch": "on the dispatch list",
                    "harmless": "known harmless",
                    "unknown": "ON NEITHER LIST — vet, then add a line"}[kind]
            print(f"    @{base:<40} {count:>4} symbol(s)   {note}")
        print()
    if report.decorators_gone:
        print("DECORATORS NO LONGER USED\n")
        for base, _kind, count in report.decorators_gone:
            print(f"    @{base:<40} was on {count} symbol(s)")
        print()

    if report.loops_deeper:
        print("LOOPS DEEPER — more nesting in the body than before (own loops only;\n"
              "depth reached through calls is not compared)\n")
        for change in report.loops_deeper:
            print(_bullet(change))
            print(f"        was {change.before} deep, now {change.after}")
        print()
    if report.loops_shallower:
        print("LOOPS SHALLOWER\n")
        for change in report.loops_shallower:
            print(_bullet(change))
            print(f"        was {change.before} deep, now {change.after}")
        print()

    if report.cycles_appeared:
        print("CIRCULAR IMPORTS APPEARED — these files now import each other\n")
        for group in report.cycles_appeared:
            for member in group:
                print(f"    {member}")
            print()
    if report.cycles_gone:
        print("CIRCULAR IMPORTS DISSOLVED\n")
        for group in report.cycles_gone:
            for member in group:
                print(f"    {member}")
            print()

    if (report.edges_added or report.edges_removed) and not args.brief:
        if report.edges_removed:
            print("REFERENCES LOST — something no longer calls, uses or inherits this\n")
            for label in report.edges_removed:
                print(f"    {label}")
            print()
        if report.edges_added:
            print("REFERENCES GAINED\n")
            for label in report.edges_added:
                print(f"    {label}")
            print()
    elif report.edges_added or report.edges_removed:
        print(f"({len(report.edges_added)} references gained and "
              f"{len(report.edges_removed)} lost hidden; drop --brief to list them)\n")

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


def cmd_backfill(args: argparse.Namespace) -> int:
    """Index past commits, so drift can be tested against real history today
    rather than after weeks of accumulating scans."""
    root = Path(args.path).resolve()
    if git(root, "rev-parse", "HEAD") is None:
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

    log = git(root, "log", "-n", str(args.last), "--format=%H %ct %s")
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
        if git(root, "worktree", "add", "--detach", str(worktree),
                commits[0][0]) is None:
            print("could not create a worktree", file=sys.stderr)
            return 2
        try:
            with Index(target, codebase_root=root) as index:
                previous_commit, previous_scan = None, None
                for number, (commit, when, subject) in enumerate(commits, start=1):
                    if git(worktree, "checkout", "--detach", "--force",
                            commit) is None:
                        print(f"  ! could not check out {commit[:8]}, skipping",
                              file=sys.stderr)
                        continue
                    committed = datetime.fromtimestamp(
                        int(when), timezone.utc).isoformat(timespec="seconds")
                    plan = plan_for(worktree)
                    scan_id = index.begin_scan(
                        worktree, plan.skipped_count, record_root=root,
                        commit_override=commit, dirty_override=False,
                        timestamp_override=committed)

                    changed = (changed_python_files(worktree, previous_commit, commit)
                               if previous_commit else None)
                    if changed is None and previous_commit:
                        # Falling back is right; doing it quietly is not.
                        # A run that read everything for a reason nobody
                        # saw cannot be told from one that worked as meant.
                        why = git_failure(worktree, "diff", "--name-only",
                                          previous_commit, commit)
                        print(f"  ! could not diff {previous_commit[:8]}.."
                              f"{commit[:8]} ({why}); read every file instead",
                              file=sys.stderr)
                    if changed is None or previous_scan is None:
                        full_scan(index, scan_id, plan, patterns)
                        touched = len(plan.files)
                    else:
                        result = incremental_scan(
                            index, worktree, scan_id, plan, patterns, changed)
                        touched = result["reparsed"]

                    # Only the newest commit gets its references resolved.
                    # Doing it for all of them would dominate the run for
                    # edges nobody asks about at a historical commit.
                    if number == len(commits):
                        _p, _t, _s, references, lost, hints = resolve_codebase(
                            worktree, patterns)
                        index.write_references(
                            scan_id, references, index.symbol_uuids_by_key())
                        index.record_lost_trails(scan_id, lost)
                        index.set_external_base_hints(scan_id, hints)
                        if not index.scan(scan_id)["cycles_recorded"]:
                            index.record_cycles(scan_id, cycles_for(plan))

                    totals = index.finish_scan(scan_id)
                    previous_commit, previous_scan = commit, scan_id
                    if number % 25 == 0 or number == len(commits):
                        print(f"  {number:>4}/{len(commits)}  {commit[:8]}  "
                              f"{totals['total_symbols']:>6} symbols  "
                              f"{touched:>4} files re-read  {subject[:36]}")
        finally:
            git(root, "worktree", "remove", "--force", str(worktree))

    print(f"\nDone. Compare any two with:  spanda drift {args.path} A B")
    return 0


def _report_lost_trails(lost) -> None:
    """The resolver's self-audit, printed where it cannot be missed.

    Zero is the expected reading. Anything else means the tool imported a
    name and then could not say what it was — and every "no callers" answer
    from this run deserves suspicion until the cause is found.
    """
    if not lost:
        print("  self-audit: every name brought in by an import statement, at the "
              "top of a file or\n  inside a function, was traced to its definition "
              "(imports done by calling importlib\n  are not statements; `spanda "
              "gaps` lists those)")
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
    plan, table, _scopes, references, lost, _hints = resolve_codebase(root, patterns)

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
    print("  The rest are listed above with a reason — none are dropped.")

    edges = Counter(r.edge_type for r in resolved)
    print("\n  edges by type:")
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

    plan, index, edges = import_survey(root)
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
        latest = max((s["scan_id"] for s in index.scans(complete_only=True)),
                     default=0)

        for symbol in matches:
            print(f"\n{symbol['file_path']}:{symbol['line_start']}  "
                  f"{symbol['kind']} {symbol['qualname']}")
            print(f"    {symbol['canonical_signature']}")

            # A symbol the newest scan did not see is gone, not unused. Saying
            # "nothing references it" about a deleted function invites exactly
            # the wrong conclusion.
            if symbol["last_seen_scan_id"] < latest:
                print(f"\n  This symbol no longer exists: last seen in scan "
                      f"{symbol['last_seen_scan_id']}, the newest is {latest}.\n"
                      f"  Nothing can call it now. To see what called it while it "
                      f"existed, compare\n  scans with `spanda drift`.")
                continue

            callers = index.callers_of(symbol["uuid"])
            maybe = index.possible_callers_of(symbol["name"])

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
            elif symbol["dispatch_hint"] \
                    and symbol["dispatch_hint"].startswith("unknown_decorator:"):
                print(f"\n  ...but it is decorated with @{symbol['dispatch_hint'][18:]}, "
                      f"which spanda does not know.\n     A framework may call it. "
                      f"Vet it, then add the decorator to dynamic_dispatch.txt\n"
                      f"     as dispatching or as harmless, so the next reader is told.")
            elif symbol["dispatch_hint"] and symbol["dispatch_hint"].startswith("external_base:"):
                print(f"\n  ...but it is a public method on a subclass of "
                      f"{symbol['dispatch_hint'][14:]}, which is outside this\n"
                      f"     codebase, and nothing here names it. Frameworks call "
                      f"such methods by name.")

            if maybe:
                print(f"\n  plus {len(maybe)} place(s) reaching for the name "
                      f"'{symbol['name']}' on an object\n     whose type could "
                      f"not be determined — each may or may not be this symbol:")
                for row in maybe[:args.limit]:
                    print(f"      {row['source_file']}:{row['line']}  {row['raw']}")
                if len(maybe) > args.limit:
                    print(f"      ... and {len(maybe) - args.limit} more")

            verdict = index.verdict_for(symbol["file_path"], symbol["qualname"])
            if verdict:
                print(f"\n  Vetted {verdict['verdict'].upper()} by a person on "
                      f"{verdict['date']}" + (f": {verdict['note']}" if verdict["note"] else "")
                      + "\n     (recorded in the index; `spanda vet` checks it "
                        "against each scan)")
            elif not callers and not symbol["has_dynamic_dispatch"] and not maybe \
                    and not symbol["dispatch_hint"]:
                print("\n  Nothing references it, and nothing explains the "
                      "silence. Possibly unused —\n     though entry points and "
                      "callers outside this codebase are invisible here. If a "
                      "person\n     decides, record it: `spanda vet` prints the line.")
    return 0


def cmd_vet(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    target = resolve_db(args)
    if target is None:
        print(f"no index yet at {db_path(root)} — run `spanda index` first",
              file=sys.stderr)
        return 1
    status = 0
    with Index(target) as index:
        # -- record, forget, import: writes first, then the report -----------
        recorded = 0
        for verdict, targets in (("alive", args.alive or []), ("dead", args.dead or [])):
            for spec in targets:
                parsed = verdicts_module.parse_target(spec)
                if parsed is None:
                    print(f"  ! {spec!r} is not file_path::qualname", file=sys.stderr)
                    status = 1
                    continue
                if index.symbol_by_path(*parsed) is None:
                    print(f"  ! no symbol {spec} in this index; recorded anyway — "
                          f"`spanda vet` will report it as contradicted", file=sys.stderr)
                index.record_verdict(*parsed, verdict, note=args.note or "")
                recorded += 1
        for spec in args.forget or []:
            parsed = verdicts_module.parse_target(spec)
            if parsed is None or not index.forget_verdict(*parsed):
                print(f"  ! no verdict recorded for {spec}", file=sys.stderr)
                status = 1
            else:
                recorded += 1
        if args.from_file:
            source = Path(args.from_file)
            imported, problems = verdicts_module.parse(source.read_text())
            for problem in problems:
                print(f"  ! {source.name} line {problem.line}: {problem.why}\n"
                      f"      {problem.text}", file=sys.stderr)
                status = 1
            for v in imported:
                index.record_verdict(v.file_path, v.qualname, v.verdict, v.note, v.date)
            recorded += len(imported)
        if recorded:
            print(f"{recorded} verdict(s) written to {target}\n")

        if args.export:
            for v in index.verdicts():
                print(verdicts_module.as_line(v))
            return status

        report = verdicts_module.vet(index, include_tests=args.include_tests,
                                     limit=args.limit)
    print(verdicts_module.render(report, root.name))
    if args.append_to and report.suggestions:
        destination = Path(args.append_to)
        existing = destination.read_text() if destination.exists() else ""
        new = [s for s in report.suggestions if s.line not in existing.splitlines()]
        if new:
            with destination.open("a") as handle:
                handle.write(f"\n# From `spanda vet {root.name}` on "
                             f"{verdicts_module.today()}:\n")
                for s in new:
                    handle.write(f"# {s.because}\n{s.line}\n")
        print(f"appended {len(new)} pattern line(s) to {destination}")
    return status


def cmd_profile(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    target = resolve_db(args)
    if target is None:
        print(f"no index yet at {db_path(root)} — run `spanda index` first",
              file=sys.stderr)
        return 1
    with Index(target) as index:
        profile = build_profile(index, include_tests=args.include_tests,
                                min_files=args.min_files)
    print(render_profile(profile, root.name))
    return 0


def cmd_loops(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    target = resolve_db(args)
    if target is None:
        print(f"no index yet at {db_path(root)} — run `spanda index` first",
              file=sys.stderr)
        return 1
    with Index(target) as index:
        report = build_loops(index, include_tests=args.include_tests)
    print(render_loops(report, root.name, limit=args.limit))
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

    profile_cmd = subparsers.add_parser(
        "profile", help="what the code keeps doing: reuse, naming, annotations, churn")
    profile_cmd.add_argument("path", help="root of the indexed codebase")
    profile_cmd.add_argument("--include-tests", action="store_true",
                             help="count symbols under tests/ as well")
    profile_cmd.add_argument("--min-files", type=int, default=3, metavar="N",
                             help="report a name as reused when defined in N+ files (default 3)")
    profile_cmd.add_argument("--db", default=None, help="pin a specific index file")
    profile_cmd.set_defaults(func=cmd_profile)

    loops_cmd = subparsers.add_parser(
        "loops", help="where the loops are: nested in a body, nested across "
                      "calls, recursive, and database calls inside them")
    loops_cmd.add_argument("path", help="root of the indexed codebase")
    loops_cmd.add_argument("--include-tests", action="store_true",
                           help="count symbols under tests/ too")
    loops_cmd.add_argument("--limit", type=int, default=15, metavar="N",
                           help="rows per section (default 15)")
    loops_cmd.add_argument("--db", default=None, help="pin a specific index file")
    loops_cmd.set_defaults(func=cmd_loops)

    vet_cmd = subparsers.add_parser(
        "vet", help="the verdicts loop: record a person's decision in the index, "
                    "check recorded decisions against the newest scan, print the "
                    "pattern lines they imply and the next candidates")
    vet_cmd.add_argument("path", help="root of the indexed codebase")
    vet_cmd.add_argument("--alive", action="append", metavar="FILE::QUALNAME",
                         help="record that this symbol is alive (repeatable)")
    vet_cmd.add_argument("--dead", action="append", metavar="FILE::QUALNAME",
                         help="record that this symbol is dead (repeatable)")
    vet_cmd.add_argument("--note", default="", help="why, for the verdicts recorded now")
    vet_cmd.add_argument("--forget", action="append", metavar="FILE::QUALNAME",
                         help="remove a recorded verdict")
    vet_cmd.add_argument("--from", dest="from_file", metavar="FILE",
                         help="record every verdict line in this file")
    vet_cmd.add_argument("--export", action="store_true",
                         help="print every recorded verdict as a line --from can read")
    vet_cmd.add_argument("--include-tests", action="store_true",
                         help="list candidates under tests/ too")
    vet_cmd.add_argument("--limit", type=int, default=30, metavar="N",
                         help="candidates to print (default 30)")
    vet_cmd.add_argument("--append-to", metavar="FILE",
                         help="append the suggested pattern lines to this pattern file")
    vet_cmd.add_argument("--db", default=None, help="pin a specific index file")
    vet_cmd.set_defaults(func=cmd_vet)

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
