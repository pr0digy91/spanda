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


METHOD_PREFIX = "method:"


def is_dynamic_dispatch(decorator_base: str | None, patterns: list[str]) -> bool:
    if not decorator_base:
        return False
    return any(fnmatch(decorator_base, pattern) for pattern in patterns
               if not pattern.startswith(METHOD_PREFIX))


def is_framework_method(bases: list[str] | None, name: str, patterns: list[str]) -> bool:
    """A method the framework calls by name on a subclass of its base.

    `class RequestLogger(BaseHTTPMiddleware)` with a `dispatch` method: no
    decorator, no call anywhere in the codebase, and the base is external
    so the resolver never sees what invokes it. Matched on the base names
    as written, both in full and by their last component.
    """
    if not bases:
        return False
    written = set()
    for base in bases:
        # `Generic[T]`, `Base[Model]` — the subscript is not part of the name.
        plain = base.split("[", 1)[0]
        written.add(plain)
        written.add(plain.rpartition(".")[2])
    for pattern in patterns:
        if not pattern.startswith(METHOD_PREFIX):
            continue
        base_glob, _, name_glob = pattern[len(METHOD_PREFIX):].rpartition(".")
        if fnmatch(name, name_glob) and any(fnmatch(b, base_glob) for b in written):
            return True
    return False


def is_framework_called(definition: dict, bases_by_local: dict, patterns: list[str]) -> bool:
    """Either way a framework can own the call: a decorator, or an override."""
    if any(is_dynamic_dispatch(d["base"], patterns) for d in definition["decorators"]):
        return True
    return definition["kind"] == "method" and is_framework_method(
        bases_by_local.get(definition["parent"]), definition["name"], patterns)


def class_bases_by_local(definitions: list[dict]) -> dict:
    return {d["local_id"]: d["bases"] for d in definitions if d["kind"] == "class"}


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

    # 1b. An override the framework calls by name. Same confidence as a
    #     decorator — the base and the method name are both written — and
    #     the one shape a decorator list could never express.
    for record in scan.records:
        bases = class_bases_by_local(record["definitions"])
        for definition in record["definitions"]:
            if definition["kind"] == "method" and is_framework_method(
                    bases.get(definition["parent"]), definition["name"], patterns):
                gaps.append(Gap(
                    "framework_method_override", record["file"],
                    definition["lines"][0], definition["qualname"],
                    f"overrides {definition['name']} on "
                    f"{', '.join(bases.get(definition['parent']) or [])}"))

    # 2. Call sites that choose their target at runtime. The *target* is
    #    unknown; the site itself is certain.
    for record in scan.records:
        by_id = {d["local_id"]: d["qualname"] for d in record["definitions"]}
        for hint in record["dynamic_hints"]:
            if hint["kind"] in ("getattr", "setattr", "hasattr", "delattr"):
                gaps.append(Gap(
                    "runtime_attribute_access", record["file"], hint["line"],
                    by_id.get(hint["enclosing"], "<module>"), hint["raw"]))
            elif hint["kind"] == "dynamic_import":
                gaps.append(Gap(
                    "dynamic_import", record["file"], hint["line"],
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
    # Only things that can be *called*. A string matching a variable name is
    # not a hidden call site: a Pydantic field named `to`, or a dict key
    # spelling a model attribute, has no callers to be unable to find. On
    # the target codebase that distinction removes 451 findings of pure noise, all of
    # them class attributes, without losing a single real one.
    defined: dict[str, list[str]] = defaultdict(list)
    for record in scan.records:
        for definition in record["definitions"]:
            if definition["kind"] not in ("function", "method", "class"):
                continue
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
