"""Where the loops are, and what runs inside them.

Not Big O. Complexity depends on what a loop walks and on what the called
code does at runtime, and neither is knowable from source. What *is*
knowable is where the loops sit: how deep they nest inside one body, how
deep they nest across a call — a one-loop function calling a two-loop
function is three deep, and no single file shows it — which calls are
recursive, and which calls inside a loop reach a database, the shape that
turns one request into a thousand queries.

Everything here is a place to look, read from the index over the newest
completed scan. The report says where the loops are; it never says how
anything scales.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from spanda.modules import strongly_connected_components

DEFAULT_DATABASE_PATTERNS = Path(__file__).with_name("database_calls.txt")


def load_database_patterns(path: Path | None = None) -> list[str]:
    source = path or DEFAULT_DATABASE_PATTERNS
    return [line.strip() for line in source.read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def is_database_call(raw: str, patterns: list[str]) -> bool:
    return any(fnmatch(raw, pattern) for pattern in patterns)


@dataclass
class Site:
    """One symbol and how deep its loops go."""
    qualname: str
    kind: str
    file: str
    line: int
    own: int            # loops nested in its own body
    reach: int = 0      # own, or deeper through the functions it calls
    via: str | None = None  # the call that makes `reach` exceed `own`
    recursive: bool = False


@dataclass
class DatabaseCall:
    file: str
    line: int
    enclosing: str
    raw: str
    depth: int
    resolved_to: str | None = None


@dataclass
class Loops:
    scan_id: int
    symbols: int
    tests_excluded: int
    edges: int
    #: Deepest bodies, by loops nested in one function.
    deepest: list[Site] = field(default_factory=list)
    #: Bodies whose depth comes from what they call, not from their own loops.
    across_calls: list[Site] = field(default_factory=list)
    #: Recursive groups: one symbol calling itself, or several calling round.
    recursion: list[list[str]] = field(default_factory=list)
    #: Calls inside loops whose name says "database".
    database_in_loops: list[DatabaseCall] = field(default_factory=list)
    #: Calls inside loops that could not be resolved, by reason — the part
    #: of the loop bodies this report cannot see into.
    unseen_in_loops: dict[str, int] = field(default_factory=dict)
    edge_depth_missing: int = 0


def _is_test(path: str) -> bool:
    return path.startswith("tests/") or "/tests/" in path


def build(index, include_tests: bool = False,
          database_patterns: list[str] | None = None) -> Loops:
    connection = index.connection
    latest = connection.execute(
        "SELECT MAX(scan_id) AS s FROM scans WHERE completed = 1").fetchone()["s"]
    if latest is None:
        raise ValueError("no completed scan to read loops from")
    patterns = database_patterns or load_database_patterns()

    rows = connection.execute(
        "SELECT uuid, qualname, kind, file_path, line_start, loop_depth"
        " FROM symbols WHERE last_seen_scan_id = ?", (latest,)).fetchall()
    excluded = sum(1 for r in rows if not include_tests and _is_test(r["file_path"]))
    symbols = {r["uuid"]: r for r in rows
               if include_tests or not _is_test(r["file_path"])}

    edges = connection.execute(
        "SELECT source_symbol_uuid AS src, target_symbol_uuid AS dst, loop_depth"
        " FROM edges WHERE edge_type = 'calls' AND last_seen_scan_id = ?"
        " AND source_symbol_uuid IS NOT NULL", (latest,)).fetchall()
    calls: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for e in edges:
        if e["src"] in symbols and e["dst"] in symbols:
            calls[e["src"]].append((e["dst"], e["loop_depth"] or 0))

    report = Loops(scan_id=latest, symbols=len(symbols), tests_excluded=excluded,
                   edges=sum(len(v) for v in calls.values()))

    # -- recursion: strongly connected components of the call graph ----------
    graph = {u: {dst for dst, _ in calls.get(u, ())} for u in symbols}
    in_cycle: set[str] = set()
    for component in strongly_connected_components(graph):
        if len(component) > 1 or component[0] in graph.get(component[0], ()):
            in_cycle.update(component)
            report.recursion.append(sorted(symbols[u]["qualname"] for u in component))
    report.recursion.sort(key=lambda g: (-len(g), g))

    # -- depth across calls -------------------------------------------------
    # reach(f) = max(own loops, call-site depth + reach(callee)). A callee in
    # the same recursive group contributes nothing: its depth is unbounded
    # in a way loops do not describe, and it is reported as recursion.
    reach: dict[str, tuple[int, str | None]] = {}

    def compute(u: str) -> tuple[int, str | None]:
        # Iterative post-order, so a deep call chain cannot blow the stack.
        stack = [(u, False)]
        while stack:
            node, done = stack.pop()
            if node in reach:
                continue
            if not done:
                stack.append((node, True))
                for dst, _ in calls.get(node, ()):
                    if dst not in reach and not (node in in_cycle and dst in in_cycle):
                        stack.append((dst, False))
                continue
            best, via = symbols[node]["loop_depth"] or 0, None
            for dst, at in calls.get(node, ()):
                if node in in_cycle and dst in in_cycle:
                    continue
                through, _ = reach.get(dst, (0, None))
                if at + through > best:
                    best, via = at + through, symbols[dst]["qualname"]
            reach[node] = (best, via)
        return reach[u]

    sites = []
    for u, r in symbols.items():
        total, via = compute(u)
        sites.append(Site(qualname=r["qualname"], kind=r["kind"], file=r["file_path"],
                          line=r["line_start"], own=r["loop_depth"] or 0,
                          reach=total, via=via, recursive=u in in_cycle))
    report.deepest = sorted((s for s in sites if s.own >= 2),
                            key=lambda s: (-s.own, s.file, s.line))
    report.across_calls = sorted((s for s in sites if s.reach > s.own and s.reach >= 2),
                                 key=lambda s: (-s.reach, s.file, s.line))

    # -- what runs inside loops that the resolver could not see into ----------
    by_uuid = {u: r["qualname"] for u, r in symbols.items()}
    for row in connection.execute(
            "SELECT source_file, source_symbol_uuid, raw, line, loop_depth, reason"
            " FROM loop_calls WHERE scan_id = ? ORDER BY source_file, line", (latest,)):
        if not include_tests and _is_test(row["source_file"]):
            continue
        report.unseen_in_loops[row["reason"]] = report.unseen_in_loops.get(row["reason"], 0) + 1
        if is_database_call(row["raw"], patterns):
            report.database_in_loops.append(DatabaseCall(
                file=row["source_file"], line=row["line"],
                enclosing=by_uuid.get(row["source_symbol_uuid"], "<module>"),
                raw=row["raw"], depth=row["loop_depth"]))
    # A resolved call can reach a database too, through a helper whose name
    # says so — `repo.fetch_orders` is not in the pattern file, but
    # `session.execute` inside it is. That is the across-calls case above;
    # only direct, name-matched calls are listed here.
    report.edge_depth_missing = connection.execute(
        "SELECT COUNT(*) FROM edges WHERE last_seen_scan_id = ? AND loop_depth IS NULL",
        (latest,)).fetchone()[0]
    return report


def render(report: Loops, repo: str, limit: int = 15) -> str:
    out: list[str] = []
    p = out.append
    p(f"{repo}: where the loops are")
    p(f"scan {report.scan_id} · {report.symbols:,} live symbols · {report.edges:,} call edges"
      + (f" · {report.tests_excluded:,} test symbols excluded (--include-tests)"
         if report.tests_excluded else ""))
    p("")

    p("LOOPS NESTED IN ONE BODY — for/while/comprehensions, counted syntactically")
    if not report.deepest:
        p("  no function nests two or more loops")
    for s in report.deepest[:limit]:
        p(f"  {s.own} deep   {s.file}:{s.line}  {s.qualname}")
    if len(report.deepest) > limit:
        p(f"  ... and {len(report.deepest) - limit} more at 2 or deeper")
    p("")

    p("LOOPS NESTED ACROSS CALLS — a loop calling a function that loops")
    p("  'reach' adds the callee's loops to the depth of the call site. No single")
    p("  file shows this; it is read off the call graph.")
    if not report.across_calls:
        p("  none deeper through calls than in their own body")
    for s in report.across_calls[:limit]:
        p(f"  {s.reach} deep (own {s.own})   {s.file}:{s.line}  {s.qualname}"
          f"   via {s.via}")
    if len(report.across_calls) > limit:
        p(f"  ... and {len(report.across_calls) - limit} more")
    p("")

    p("RECURSION — a function that reaches itself through the call graph")
    if not report.recursion:
        p("  none")
    for group in report.recursion[:limit]:
        p("  " + (group[0] + " calls itself" if len(group) == 1
                  else " ↔ ".join(group)))
    if len(report.recursion) > limit:
        p(f"  ... and {len(report.recursion) - limit} more groups")
    p("")

    p("DATABASE CALLS INSIDE LOOPS — matched by name against database_calls.txt")
    p("  The session is an external type and never resolves, so the name is all")
    p("  there is. Each line is one place to look, not a measurement.")
    if not report.database_in_loops:
        p("  none matched")
    for c in report.database_in_loops[:limit * 2]:
        p(f"  {c.depth} deep   {c.file}:{c.line}  in {c.enclosing}:  {c.raw}")
    if len(report.database_in_loops) > limit * 2:
        p(f"  ... and {len(report.database_in_loops) - limit * 2} more")
    p("")

    if report.unseen_in_loops:
        total = sum(report.unseen_in_loops.values())
        p(f"NOT SEEN INTO — {total:,} calls inside loops the resolver could not follow:")
        for reason, count in sorted(report.unseen_in_loops.items(), key=lambda kv: -kv[1]):
            p(f"  {count:>6}  {reason}")
        p("  Whatever those do per iteration is outside this report.")
        p("")
    if report.edge_depth_missing:
        p(f"  {report.edge_depth_missing} call edges predate loop depth and have not been"
          f" re-read; run `spanda index`.")
        p("")

    p("This says where the loops are and what runs inside them. It does not say")
    p("what any loop walks, how large that is, or how anything scales.")
    return "\n".join(out)
