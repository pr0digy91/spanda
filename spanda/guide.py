"""Render the index guide with numbers read from an actual index.

The prose is a template checked into this package; every figure and every
example symbol comes from the database being described. A guide with numbers
typed in by hand is a document that quietly goes wrong the moment the index is
rebuilt, and one placed next to a database is exactly the kind of thing people
trust without checking.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from spanda import verdicts as verdicts_module

#: Candidates the guide lists before pointing at `spanda vet` for the rest.
CANDIDATE_LIMIT = 20

TEMPLATE = Path(__file__).with_name("index_guide.md")
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _count(connection, table: str) -> int:
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0


def _example_symbol(index, latest: int) -> str:
    """A symbol from this codebase worth using in the example queries.

    Preferring one that actually has callers, so a reader who runs the query
    verbatim sees the shape of a real answer rather than an empty result.
    """
    row = index.connection.execute(
        "SELECT s.name FROM edges e JOIN symbols s ON s.uuid = e.target_symbol_uuid"
        " WHERE e.last_seen_scan_id = ? AND s.kind IN ('function', 'method')"
        " GROUP BY s.uuid ORDER BY COUNT(*) DESC LIMIT 1", (latest,)).fetchone()
    if row:
        return row["name"]
    row = index.connection.execute(
        "SELECT name FROM symbols WHERE last_seen_scan_id = ?"
        " AND kind = 'function' ORDER BY name LIMIT 1", (latest,)).fetchone()
    return row["name"] if row else "some_function"


def render(index, root: Path) -> str:
    """Fill the template from this index. Nothing here is hard-coded."""
    connection = index.connection
    latest_row = connection.execute(
        "SELECT * FROM scans WHERE completed = 1 ORDER BY scan_id DESC LIMIT 1"
    ).fetchone()
    if latest_row is None:
        raise ValueError(f"{index.path} holds no completed scan yet")
    latest = latest_row["scan_id"]

    total = _count(connection, "symbols")
    alive = connection.execute(
        "SELECT COUNT(*) FROM symbols WHERE last_seen_scan_id = ?",
        (latest,)).fetchone()[0]
    dynamic = connection.execute(
        "SELECT COUNT(*) FROM symbols"
        " WHERE last_seen_scan_id = ? AND has_dynamic_dispatch = 1",
        (latest,)).fetchone()[0]

    hint_unknown = connection.execute(
        "SELECT COUNT(*) FROM symbols WHERE last_seen_scan_id = ?"
        " AND dispatch_hint LIKE 'unknown\\_decorator:%' ESCAPE '\\'",
        (latest,)).fetchone()[0]
    hint_external = connection.execute(
        "SELECT COUNT(*) FROM symbols WHERE last_seen_scan_id = ?"
        " AND dispatch_hint LIKE 'external\\_base:%' ESCAPE '\\'",
        (latest,)).fetchone()[0]
    verdicts = _count(connection, "verdicts")
    schema = index.meta("schema_version") or "?"

    edges = {r["edge_type"]: r["c"] for r in connection.execute(
        "SELECT edge_type, COUNT(*) c FROM edges WHERE last_seen_scan_id = ?"
        " GROUP BY edge_type", (latest,))}
    reasons = {r["reason"]: r["c"] for r in connection.execute(
        "SELECT reason, COUNT(*) c FROM unresolved_refs GROUP BY reason")}

    span = connection.execute(
        "SELECT MIN(timestamp) a, MAX(timestamp) b FROM scans WHERE completed = 1"
    ).fetchone()
    scans = connection.execute(
        "SELECT COUNT(*) FROM scans WHERE completed = 1").fetchone()[0]

    edge_total = sum(edges.values())
    unresolved_total = sum(reasons.values())
    lines = [
        f"{scans} scan(s)      {span['a'][:10]} → {span['b'][:10]}"
        f"      {latest_row['total_files']} files at the newest scan",
        "",
        f"symbols          {total:>7,}   ({alive:,} alive"
        + (f", {total - alive:,} removed during the window)" if total > alive else ")"),
    ]
    if edge_total:
        detail = ", ".join(f"{n:,} {k}" for k, n in sorted(
            edges.items(), key=lambda kv: -kv[1]))
        lines.append(f"edges            {edge_total:>7,}   ({detail})")
    else:
        lines.append("edges                  0   (run `spanda index` to resolve references)")
    if unresolved_total:
        detail = ", ".join(f"{n:,} {k}" for k, n in sorted(
            reasons.items(), key=lambda kv: -kv[1]))
        lines.append(f"unresolved_refs  {unresolved_total:>7,}   ({detail})")
    lines.append(f"symbol_versions  {_count(connection, 'symbol_versions'):>7,}")
    # `.keys()` is not redundant here and `.get()` does not exist: this is a
    # sqlite3.Row, where `in` tests the values, not the column names.
    lost = latest_row["lost_trails"] if "lost_trails" in latest_row.keys() else 0  # noqa: SIM118
    lines.append(f"lost trails      {lost or 0:>7,}   imports the resolver could not place"
                 + ("" if not lost else " — DISTRUST 'no callers' UNTIL FIXED"))
    lines.append(f"dynamic dispatch {dynamic:>7,}   live symbols whose callers are not in "
                 f"the source")
    lines.append(f"unrecognised     {hint_unknown + hint_external:>7,}   live symbols the tool "
                 f"declines to call dead ({hint_unknown:,} unknown decorator, "
                 f"{hint_external:,} external base)")
    lines.append(f"verdicts         {verdicts:>7,}   human decisions on record")

    if edge_total:
        considered = edge_total + unresolved_total
        note = (
            f"{edge_total:,} of the {considered:,} references that could point at this "
            f"codebase\nresolved to a definition. The rest are attribute access on "
            f"unknown types and\nnames nothing here defines. That is the ceiling of "
            f"static analysis without type\ninference, and it is reported rather than "
            f"hidden — a call graph missing part of\nthe codebase while looking "
            f"complete is worse than one that admits it.")
    else:
        note = ("This index holds no reference edges yet. `spanda backfill` resolves "
                "them only\nfor the newest commit; run `spanda index` to fill them in.")

    # The verdicts loop, as it stands: the recorded decisions, checked
    # against this scan. Generated, like everything else here; the one thing
    # it never generates is the verdict itself.
    vetting = verdicts_module.vet(index, limit=CANDIDATE_LIMIT)
    known = vetting.verdicts
    verdict_lines = [
        f"{len(known)} verdict(s) recorded in the index"
        + (f" ({sum(1 for v in known if v.verdict == 'alive')} alive, "
           f"{sum(1 for v in known if v.verdict == 'dead')} dead)" if known else "")]
    if vetting.suggestions:
        verdict_lines.append(f"{len(vetting.suggestions)} pattern line(s) waiting to be "
                             f"added — alive verdicts on shapes the tool did not recognise")
    if vetting.contradicted:
        verdict_lines.append(f"{len(vetting.contradicted)} verdict(s) the code now "
                             f"contradicts")
    if vetting.blind_spots:
        verdict_lines.append(f"{len(vetting.blind_spots)} alive verdict(s) with nothing "
                             f"to explain them — blind spots to report")
    if vetting.candidates:
        candidate_lines = [f"?      {f}::{q}" for f, q, _l in vetting.candidates]
        if vetting.candidates_total > len(vetting.candidates):
            candidate_lines.append(
                f"# ... and {vetting.candidates_total - len(vetting.candidates)} more: "
                f"`spanda vet --limit {vetting.candidates_total}`")
    else:
        candidate_lines = ["# nothing left to vet outside tests"]

    commit = latest_row["git_commit_hash"] or latest_row["git_base_commit"]
    values = {
        "repo": root.name,
        "db_path": str(index.path),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "latest_scan": str(latest),
        "latest_commit": f", commit {commit[:8]}" if commit else "",
        "symbols_total": f"{total:,}",
        "symbols_alive": f"{alive:,}",
        "symbols_dead": f"{total - alive:,}",
        "dynamic": f"{dynamic:,}",
        "hint_unknown": f"{hint_unknown:,}",
        "hint_external": f"{hint_external:,}",
        "verdicts": f"{verdicts:,}",
        "verdict_summary": "\n".join(f"- {line}" for line in verdict_lines),
        "candidates_total": f"{vetting.candidates_total:,}",
        "candidates_block": "\n".join(candidate_lines),
        "schema": str(schema),
        "unknown_type": f"{reasons.get('attribute_on_unknown_type', 0):,}",
        "example_symbol": _example_symbol(index, latest),
        "stats_block": "\n".join(lines),
        "resolution_note": note,
    }

    text = TEMPLATE.read_text()
    missing = set(PLACEHOLDER.findall(text)) - set(values)
    if missing:
        raise ValueError(f"template has placeholders nothing fills: {sorted(missing)}")
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
