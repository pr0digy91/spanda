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
    lost = latest_row["lost_trails"] if "lost_trails" in latest_row.keys() else 0
    lines.append(f"lost trails      {lost or 0:>7,}   imports the resolver could not place"
                 + ("" if not lost else " — DISTRUST 'no callers' UNTIL FIXED"))
    lines.append(f"dynamic dispatch {dynamic:>7,}   live symbols whose callers are not in the source")

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
        "unknown_type": f"{reasons.get('attribute_on_unknown_type', 0):,}",
        "example_symbol": _example_symbol(index, latest),
        "stats_block": "\n".join(lines),
        "resolution_note": note,
    }

    text = TEMPLATE.read_text()
    missing = {m for m in PLACEHOLDER.findall(text)} - set(values)
    if missing:
        raise ValueError(f"template has placeholders nothing fills: {sorted(missing)}")
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
