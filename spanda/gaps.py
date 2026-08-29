"""What the extractor cannot see, made explicit.

This module resolves nothing. It reads Stage 1 output and reports the places
where a call graph built from that output would be wrong if believed — the
symbols something calls that no reference names. Producing this list is the
whole reason for recording decorators verbatim and hint sites at all.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

DEFAULT_PATTERNS = Path(__file__).with_name("dynamic_dispatch.txt")


def load_patterns(path: Path | None = None) -> list[str]:
    source = path or DEFAULT_PATTERNS
    return [
        line.strip() for line in source.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def is_dynamic_dispatch(decorator_base: str | None, patterns: list[str]) -> bool:
    if not decorator_base:
        return False
    return any(fnmatch(decorator_base, pattern) for pattern in patterns)


def referenced_names(scan) -> set[str]:
    """Every bare name the codebase mentions, from any construct that names a
    symbol: a reference, an import, or an `__all__` entry.

    Names only — this is not resolution, and two different symbols sharing a
    name are indistinguishable here. That imprecision is deliberate and runs
    one way only: a name in this set is definitely mentioned somewhere, so
    anything absent from it is safe to treat as genuinely unmentioned.
    """
    named: set[str] = set()
    for record in scan.records:
        for reference in record["references"]:
            if reference["chain"]:
                named.update(reference["chain"])
        # An import names a symbol just as surely as a call does. Counting
        # only references would report every re-exported symbol as orphaned.
        for statement in record["imports"]:
            for alias in statement["names"]:
                named.add(alias["name"].split(".")[0])
                if alias["alias"]:
                    named.add(alias["alias"])
        named.update(record["dunder_all"])
    return named


@dataclass
class Gap:
    kind: str
    file: str
    line: int
    symbol: str
    detail: str


def find_gaps(scan, patterns: list[str]) -> list[Gap]:
    """Three kinds of gap, in descending order of confidence."""
    gaps: list[Gap] = []

    # 1. Decorated with something that dispatches at runtime. High confidence:
    #    the decorator is written in the source, we are only reading it.
    for record in scan.records:
        for definition in record["definitions"]:
            for decorator in definition["decorators"]:
                if is_dynamic_dispatch(decorator["base"], patterns):
                    gaps.append(Gap(
                        "dynamic_dispatch_decorator", record["file"],
                        definition["lines"][0], definition["qualname"],
                        "@" + decorator["raw"]))

    # 2. Call sites that choose their target at runtime. The *target* is
    #    unknown; the site itself is certain.
    for record in scan.records:
        by_id = {d["local_id"]: d["qualname"] for d in record["definitions"]}
        for hint in record["dynamic_hints"]:
            if hint["kind"] in ("getattr", "setattr", "hasattr", "delattr"):
                gaps.append(Gap(
                    "runtime_attribute_access", record["file"], hint["line"],
                    by_id.get(hint["enclosing"], "<module>"), hint["raw"]))

    # 3. A string literal that spells the name of a symbol nothing references.
    #    Restricted to otherwise-unreferenced symbols on purpose: if a symbol
    #    is referenced normally somewhere, its name appearing in a string adds
    #    nothing, and reporting it anyway buries the real finding. On the
    #    Python stdlib that restriction is the difference between 6091 hits
    #    and a list someone will actually read.
    #
    #    This stays a heuristic and is labelled as one: a name match is not a
    #    call, and it must never be turned into an edge.
    referenced = referenced_names(scan)
    defined: dict[str, list[str]] = defaultdict(list)
    for record in scan.records:
        for definition in record["definitions"]:
            defined[definition["name"]].append(
                f"{record['file']}:{definition['lines'][0]}")

    for record in scan.records:
        by_id = {d["local_id"]: d["qualname"] for d in record["definitions"]}
        own = {d["name"] for d in record["definitions"]}
        # __all__ entries name re-exports, which are a resolvable construct
        # (Stage 2 handles them). Reporting them here would pad the list with
        # gaps that are not gaps, and a padded list stops being read.
        exported = set(record["dunder_all"])
        for hint in record["dynamic_hints"]:
            if hint["kind"] != "identifier_string":
                continue
            value = hint["value"]
            if value in exported or value in referenced:
                continue
            if value in defined and value not in own:
                where = ", ".join(defined[value][:3])
                gaps.append(Gap(
                    "name_in_string_literal", record["file"], hint["line"],
                    by_id.get(hint["enclosing"], "<module>"),
                    f'"{value}" names a symbol defined at {where}'))

    return sorted(gaps, key=lambda g: (g.kind, g.file, g.line))


def unreferenced_symbols(scan) -> list[tuple[str, int, str]]:
    """Definitions whose name appears in no reference anywhere in the codebase.

    Deliberately a name match, not a resolution: this over-reports (any
    same-named symbol counts) and so is safe in the one direction that
    matters. A symbol listed here is *at most* unreferenced; a symbol absent
    from it is definitely referenced somewhere.
    """
    named = referenced_names(scan)
    orphans = []
    for record in scan.records:
        for definition in record["definitions"]:
            if definition["name"] not in named:
                orphans.append((record["file"], definition["lines"][0],
                                definition["qualname"]))
    return sorted(orphans)
