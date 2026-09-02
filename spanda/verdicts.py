"""Human verdicts, kept in the index, and the loop that turns a miss into a
pattern line.

The tool can say "no callers" and it can say "I do not know this shape". It
cannot say "dead". Only a person who has looked can, and that decision was
being made in a chat window and lost. The `verdicts` table keeps it, in the
one authoritative store this project has: the index.

    spanda vet <repo> --alive server.py::RequestLoggingMiddleware.dispatch \\
        --note "Starlette calls dispatch by name"
    spanda vet <repo> --dead services/legacy.py::old_helper --note "removed in PR 412"

`spanda vet` with no flags reads the verdicts against the newest scan and
does four things: turns every *alive* verdict on an unrecognised shape into
the pattern line that would have recognised it; reports verdicts the code
has since contradicted — a "dead" that now has callers, an "alive" that no
longer exists; flags an alive verdict nothing explains as a blind spot in
the tool; and prints the next candidates to vet. A miss costs one line once
it is known, and this is where the line comes from.

The index is not committed, so verdicts live with the index on the machine
that holds it. `--export` prints them as lines and `--from FILE` reads such
lines back, for moving them between machines or surviving a rebuild.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone


@dataclass(frozen=True)
class Verdict:
    file_path: str
    qualname: str
    verdict: str        # alive | dead
    date: str
    note: str
    line: int = 0       # source line, when read from a file


@dataclass
class ParseProblem:
    line: int
    text: str
    why: str


def parse_target(target: str) -> tuple[str, str] | None:
    """`file_path::qualname`, or None if it is not that shape."""
    if "::" not in target:
        return None
    file_path, _, qualname = target.partition("::")
    if not file_path or not qualname:
        return None
    return file_path, qualname


def parse(text: str) -> tuple[list[Verdict], list[ParseProblem]]:
    """Read verdict lines — the format `--export` writes and `--from` reads.
    A bad line is reported, never silently skipped."""
    verdicts, problems = [], []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            problems.append(ParseProblem(number, raw, "expected: verdict  file::qualname  date  note"))
            continue
        verdict, target, when = parts[0].lower(), parts[1], parts[2]
        note = parts[3] if len(parts) > 3 else ""
        if verdict not in ("alive", "dead"):
            problems.append(ParseProblem(number, raw, f"verdict must be alive or dead, not {parts[0]!r}"))
            continue
        parsed = parse_target(target)
        if parsed is None:
            problems.append(ParseProblem(number, raw, "target must be file_path::qualname"))
            continue
        try:
            date.fromisoformat(when)
        except ValueError:
            problems.append(ParseProblem(number, raw, f"date must be YYYY-MM-DD, not {when!r}"))
            continue
        verdicts.append(Verdict(parsed[0], parsed[1], verdict, when, note, number))
    return verdicts, problems


def as_line(verdict: Verdict) -> str:
    return (f"{verdict.verdict:<5}  {verdict.file_path}::{verdict.qualname}  "
            f"{verdict.date}  {verdict.note}").rstrip()


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

@dataclass
class Suggestion:
    """A pattern line that would have recognised a vetted-alive symbol."""
    line: str
    because: str


@dataclass
class VetReport:
    scan_id: int
    verdicts: list[Verdict]
    suggestions: list[Suggestion] = field(default_factory=list)
    #: (verdict, what the code says now)
    contradicted: list[tuple[Verdict, str]] = field(default_factory=list)
    #: alive verdicts already explained by a pattern — fine, just redundant
    explained: list[tuple[Verdict, str]] = field(default_factory=list)
    #: alive verdicts on symbols with no hint at all: a blind spot to report
    blind_spots: list[Verdict] = field(default_factory=list)
    #: symbols with no edge, no hint, no verdict — the next list to vet
    candidates: list[tuple[str, str, int]] = field(default_factory=list)
    candidates_total: int = 0


def suggestion_for(hint: str | None, qualname: str) -> Suggestion | None:
    """The pattern line an alive verdict implies, from the symbol's hint."""
    if not hint:
        return None
    kind, _, detail = hint.partition(":")
    if kind == "unknown_decorator":
        return Suggestion(detail, f"@{detail} was vetted as called by a framework")
    if kind == "external_base":
        name = qualname.rpartition(".")[2]
        return Suggestion(f"method:{detail}.{name}",
                          f"{name} on a {detail} subclass was vetted as framework-called")
    return None


def vet(index, include_tests: bool = False, limit: int = 30) -> VetReport:
    """Read the recorded verdicts against the newest scan."""
    connection = index.connection
    latest = connection.execute(
        "SELECT MAX(scan_id) FROM scans WHERE completed = 1").fetchone()[0]
    if latest is None:
        raise ValueError("no completed scan to vet against")
    verdicts = index.verdicts()
    report = VetReport(latest, verdicts)

    seen_lines: set[str] = set()
    for verdict in verdicts:
        row = connection.execute(
            "SELECT * FROM symbols WHERE file_path = ? AND qualname = ?"
            " ORDER BY last_seen_scan_id DESC LIMIT 1",
            (verdict.file_path, verdict.qualname)).fetchone()
        if row is None:
            report.contradicted.append((verdict, "no such symbol in the index"))
            continue
        gone = row["last_seen_scan_id"] < latest
        callers = connection.execute(
            "SELECT COUNT(*) FROM edges WHERE target_symbol_uuid = ?"
            " AND last_seen_scan_id = ?", (row["uuid"], latest)).fetchone()[0]

        if verdict.verdict == "dead":
            if gone:
                continue  # dead and now removed: the verdict was acted on
            if callers:
                report.contradicted.append(
                    (verdict, f"{callers} caller(s) now — vetted dead, but called"))
            continue

        # alive
        if gone:
            report.contradicted.append(
                (verdict, f"no longer exists (last seen in scan {row['last_seen_scan_id']})"))
            continue
        hint = row["dispatch_hint"]
        if row["has_dynamic_dispatch"]:
            report.explained.append((verdict, hint or "dispatch"))
            continue
        suggestion = suggestion_for(hint, verdict.qualname)
        if suggestion is not None:
            if suggestion.line not in seen_lines:
                seen_lines.add(suggestion.line)
                report.suggestions.append(suggestion)
            continue
        if callers:
            report.explained.append((verdict, f"{callers} static caller(s)"))
            continue
        report.blind_spots.append(verdict)

    # the next list to vet
    rows = connection.execute(
        "SELECT s.qualname, s.file_path, s.line_start FROM symbols s"
        " WHERE s.last_seen_scan_id = ?"
        "  AND s.kind IN ('function', 'method', 'class')"
        "  AND s.has_dynamic_dispatch = 0 AND s.dispatch_hint IS NULL"
        "  AND NOT (s.name LIKE '\\_\\_%\\_\\_' ESCAPE '\\')"
        "  AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.target_symbol_uuid = s.uuid"
        "                  AND e.last_seen_scan_id = s.last_seen_scan_id)"
        "  AND NOT EXISTS (SELECT 1 FROM unresolved_refs u WHERE u.attr_name = s.name)"
        "  AND NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.file_path = s.file_path"
        "                  AND v.qualname = s.qualname)"
        " ORDER BY s.file_path, s.line_start", (latest,)).fetchall()
    if not include_tests:
        rows = [r for r in rows if not (r["file_path"].startswith("tests/")
                                        or "/tests/" in r["file_path"])]
    report.candidates_total = len(rows)
    report.candidates = [(r["file_path"], r["qualname"], r["line_start"]) for r in rows[:limit]]
    return report


def render(report: VetReport, repo: str) -> str:
    out: list[str] = []
    p = out.append
    p(f"{repo}: vetting, against scan {report.scan_id}")
    alive = sum(1 for v in report.verdicts if v.verdict == "alive")
    p(f"{len(report.verdicts)} verdict(s) recorded in the index: "
      f"{alive} alive, {len(report.verdicts) - alive} dead")
    p("")

    if report.suggestions:
        p("PATTERN LINES TO ADD — each alive verdict on an unrecognised shape, as a rule")
        p("  Append to spanda/dynamic_dispatch.txt (or the file passed with --patterns),")
        p("  or run `spanda vet --append-to <file>`:")
        for s in report.suggestions:
            p(f"    {s.line:<44} # {s.because}")
        p("")
    if report.blind_spots:
        p("ALIVE WITH NOTHING TO EXPLAIN IT — a blind spot in the tool; report the shape")
        for v in report.blind_spots:
            p(f"    {v.file_path}::{v.qualname}  ({v.date}: {v.note or 'no note'})")
        p("")
    if report.contradicted:
        p("VERDICTS THE CODE NOW CONTRADICTS")
        for v, why in report.contradicted:
            p(f"    {v.verdict:<5} {v.file_path}::{v.qualname}  ({v.date}) — {why}")
        p("")
    if report.explained:
        p(f"{len(report.explained)} alive verdict(s) already explained by the tool "
          f"(a pattern, or a caller); kept as record")
        p("")

    p(f"TO VET — {report.candidates_total} symbol(s) with no caller, no hint, no verdict"
      + (f"; first {len(report.candidates)}" if report.candidates_total > len(report.candidates) else ""))
    p(f"  Record a decision with  spanda vet {repo} --dead <target>  or  --alive <target>")
    p(f"  --note \"what calls it\";  or save the lines you agree with to a file and run")
    p(f"  spanda vet {repo} --from <file>:")
    for file_path, qualname, line in report.candidates:
        p(f"    dead   {file_path}::{qualname}  {today()}  ")
    if not report.candidates:
        p("    nothing left to vet")
    p("")
    p("A verdict is a person's decision, dated. The index records it and checks it")
    p("against later scans; the tool does not make it.")
    return "\n".join(out)
