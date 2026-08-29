"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from spanda.extract import extract_codebase


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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
