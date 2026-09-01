"""Stage 6 — what changed between two scans.

Everything before this stage exists to make this comparison possible and
honest. The report's value rests entirely on one distinction: a change to a
symbol's shape can break the code that calls it, and a change to its insides
cannot. Blur those together and the report becomes a changelog nobody reads.

Nothing here resolves references, so the report says "this contract moved",
never "and here are the twelve callers". Claiming the second without the
reference graph would be the precise failure this project exists to correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Change:
    symbol_uuid: str
    symbol_key: str
    qualname: str
    kind: str
    file_path: str
    line_start: int | None
    #: added | removed | shape | internal
    category: str
    before: str | None = None
    after: str | None = None
    #: True when static analysis cannot see who calls this, so a shape change
    #: here cannot be checked by reading the code.
    callers_unknowable: bool = False

    def sort_key(self) -> tuple:
        return (self.file_path or "", self.line_start or 0, self.qualname)


@dataclass
class DriftReport:
    scan_a: dict
    scan_b: dict
    added: list[Change] = field(default_factory=list)
    removed: list[Change] = field(default_factory=list)
    shape: list[Change] = field(default_factory=list)
    internal: list[Change] = field(default_factory=list)
    unchanged_count: int = 0
    #: Reference edges gained and lost, as "source -> target (type)". Empty
    #: with a caveat, never silently empty, when a scan holds no edge data.
    edges_added: list[str] = field(default_factory=list)
    edges_removed: list[str] = field(default_factory=list)
    #: Circular-import groups that appeared or dissolved, as sorted file lists.
    cycles_appeared: list[list[str]] = field(default_factory=list)
    cycles_gone: list[list[str]] = field(default_factory=list)
    #: Reasons the comparison itself may be incomplete, e.g. files that failed
    #: to parse in either scan. Surfaced rather than quietly tolerated.
    caveats: list[str] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not (self.added or self.removed or self.shape or self.internal
                    or self.edges_added or self.edges_removed
                    or self.cycles_appeared or self.cycles_gone)

    @property
    def total_changes(self) -> int:
        return (len(self.added) + len(self.removed) + len(self.shape)
                + len(self.internal) + len(self.edges_added)
                + len(self.edges_removed) + len(self.cycles_appeared)
                + len(self.cycles_gone))


def _describe(row) -> str:
    return row["canonical_signature"] or ""


def compare(index, scan_a: int, scan_b: int) -> DriftReport:
    """Diff two scans. Both must have completed, or the answer is a fiction."""
    a = index.require_complete(scan_a)
    b = index.require_complete(scan_b)
    if scan_a >= scan_b:
        raise ValueError(
            f"scan {scan_a} is not earlier than scan {scan_b}; "
            f"pass the older scan first")

    report = DriftReport(scan_a=dict(a), scan_b=dict(b))

    at_a = index.present_at(scan_a)
    at_b = index.present_at(scan_b)

    rows = {r["uuid"]: r for r in index.connection.execute(
        "SELECT * FROM symbols WHERE uuid IN "
        "(SELECT symbol_uuid FROM symbol_spans WHERE"
        " (from_scan <= ? AND to_scan >= ?) OR (from_scan <= ? AND to_scan >= ?))",
        (scan_a, scan_a, scan_b, scan_b))}

    def change(uuid: str, category: str, **kwargs) -> Change:
        row = rows[uuid]
        return Change(
            symbol_uuid=uuid, symbol_key=row["symbol_key"],
            qualname=row["qualname"], kind=row["kind"],
            file_path=row["file_path"], line_start=row["line_start"],
            category=category,
            callers_unknowable=bool(row["has_dynamic_dispatch"]),
            **kwargs)

    for uuid in at_b - at_a:
        version = index.version_at(uuid, scan_b)
        report.added.append(change(uuid, "added",
                                   after=_describe(version) if version else None))

    for uuid in at_a - at_b:
        version = index.version_at(uuid, scan_a)
        report.removed.append(change(uuid, "removed",
                                     before=_describe(version) if version else None))

    for uuid in at_a & at_b:
        was = index.version_at(uuid, scan_a)
        now = index.version_at(uuid, scan_b)
        if was is None or now is None:
            continue
        if was["signature_hash"] != now["signature_hash"]:
            report.shape.append(change(uuid, "shape",
                                       before=_describe(was), after=_describe(now)))
        elif was["content_hash"] != now["content_hash"]:
            report.internal.append(change(uuid, "internal"))
        else:
            report.unchanged_count += 1

    for bucket in (report.added, report.removed, report.shape, report.internal):
        bucket.sort(key=Change.sort_key)

    report.caveats = _caveats(index, a, b)
    _compare_edges(index, scan_a, scan_b, report)
    _compare_cycles(index, scan_a, scan_b, report)
    return report


def _edge_label(row) -> str:
    source = row["source_name"] or f"{row['source_file']} <module>"
    return f"{source} -> {row['target_name']} ({row['edge_type']})"


def _compare_edges(index, scan_a: int, scan_b: int, report: DriftReport) -> None:
    """What now calls, uses or inherits from what, versus before.

    Only when both scans hold reference data. Backfill resolves references at
    its newest commit alone, so against an earlier backfilled scan every edge
    would read as added — a fact about the tool, and one the reader is told.
    """
    for scan_id in (scan_a, scan_b):
        if not index.has_edge_data_at(scan_id):
            report.caveats.append(
                f"scan {scan_id} holds no reference data (references are resolved "
                f"by `spanda index`, and by `backfill` only at its newest commit); "
                f"edge changes cannot be reported for this pair")
            return
    before = index.edges_at(scan_a)
    after = index.edges_at(scan_b)
    report.edges_added = sorted(_edge_label(after[k]) for k in after.keys() - before.keys())
    report.edges_removed = sorted(_edge_label(before[k]) for k in before.keys() - after.keys())


def _compare_cycles(index, scan_a: int, scan_b: int, report: DriftReport) -> None:
    """Circular-import groups that appeared or dissolved."""
    before = index.cycles_at(scan_a)
    after = index.cycles_at(scan_b)
    for scan_id, groups in ((scan_a, before), (scan_b, after)):
        if groups is None:
            report.caveats.append(
                f"scan {scan_id} read only the files that changed, so its import "
                f"graph was never computed; circular-import changes cannot be "
                f"reported for this pair")
            return
    report.cycles_appeared = sorted(sorted(g) for g in after - before)
    report.cycles_gone = sorted(sorted(g) for g in before - after)


def _caveats(index, a, b) -> list[str]:
    """What would make this comparison less than complete.

    A file that failed to parse contributes no symbols, so everything it
    contained reads as removed — a fact about the tool, not about the code,
    and one the reader has to be told before trusting a removal count.
    """
    notes = []
    for scan in (a, b):
        if scan["unparseable_files"]:
            unreadable = index.connection.execute(
                "SELECT file_path, line FROM scan_problems"
                " WHERE scan_id = ? ORDER BY file_path",
                (scan["scan_id"],)).fetchall()
            listed = ", ".join(f"{r['file_path']}:{r['line']}"
                               for r in unreadable[:3])
            more = f" and {len(unreadable) - 3} more" if len(unreadable) > 3 else ""
            notes.append(
                f"scan {scan['scan_id']} could not parse {scan['unparseable_files']} "
                f"file(s) ({listed}{more}); anything defined in them is absent from "
                f"that scan and will read as added or removed here")
        if scan["skipped_files"]:
            notes.append(
                f"scan {scan['scan_id']} skipped {scan['skipped_files']} file(s) by "
                f"directory name; nothing in them is compared")
    if a["content_fingerprint"] == b["content_fingerprint"]:
        notes.append(
            "both scans indexed byte-identical code, so any difference below "
            "would indicate a bug in the tool rather than a change in the codebase")
    return notes
