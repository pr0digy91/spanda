"""What the code keeps doing.

`gaps` says what the tool cannot see. This says what the codebase repeats:
names re-implemented file after file, how parameters are named and annotated,
where docstrings are and are not, which symbols never settle. Read straight
from the index, over the newest completed scan, live symbols only.

Every figure here describes the corpus, not intent. A helper defined in
twenty-one files is a fact; whether that is discipline or drift is the
reader's call, and the report stops short of making it.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

SNAKE = re.compile(r"^_*[a-z][a-z0-9_]*$")
CAPWORDS = re.compile(r"^_*[A-Z][A-Za-z0-9]*$")


@dataclass
class Reuse:
    name: str
    kind: str
    files: int
    #: Distinct `body_hash` values — code with docstrings and string wording
    #: removed. One body across many files is copying; many is divergence.
    distinct_bodies: int
    #: Distinct `signature_hash` values: how many different parameter lists.
    distinct_shapes: int
    #: Definitions with no body hash yet (recorded before schema 11 and not
    #: re-read since). Reported rather than counted as one body each.
    unhashed: int
    examples: list[str]


@dataclass
class Profile:
    scan_id: int
    symbols: int
    files: int
    tests_excluded: int
    reused: list[Reuse] = field(default_factory=list)
    params: list[tuple[str, int, float]] = field(default_factory=list)
    param_annotation_rate: float = 0.0
    return_annotation_rate: float = 0.0
    docstrings: dict[str, tuple[int, int]] = field(default_factory=dict)
    naming: dict[str, tuple[int, int]] = field(default_factory=dict)
    async_share: float = 0.0
    mean_params: float = 0.0
    decorators: list[tuple[str, int]] = field(default_factory=list)
    churn: list[tuple[str, str, int]] = field(default_factory=list)
    scans: int = 1


def build(index, include_tests: bool = False, min_files: int = 3) -> Profile:
    connection = index.connection
    latest = connection.execute(
        "SELECT MAX(scan_id) AS s FROM scans WHERE completed = 1").fetchone()["s"]
    if latest is None:
        raise ValueError("no completed scan to profile")

    rows = connection.execute(
        "SELECT name, qualname, kind, file_path, signature, canonical_signature,"
        "       docstring, decorators, body_hash, signature_hash, uuid"
        " FROM symbols WHERE last_seen_scan_id = ?", (latest,)).fetchall()

    excluded = 0
    kept = []
    for r in rows:
        if not include_tests and (r["file_path"].startswith("tests/")
                                  or "/tests/" in r["file_path"]):
            excluded += 1
            continue
        kept.append(r)

    profile = Profile(
        scan_id=latest, symbols=len(kept),
        files=len({r["file_path"] for r in kept}), tests_excluded=excluded,
        scans=connection.execute(
            "SELECT COUNT(*) FROM scans WHERE completed = 1").fetchone()[0])

    # -- names re-implemented across files ------------------------------------
    by_name: dict[tuple[str, str], list] = defaultdict(list)
    for r in kept:
        # Dunder methods are expected to repeat — every class has __init__ —
        # so they say nothing about reuse and are left out.
        if r["kind"] in ("function", "method", "class") and not (
                r["name"].startswith("__") and r["name"].endswith("__")):
            by_name[(r["name"], r["kind"])].append(r)
    for (name, kind), group in by_name.items():
        files = {r["file_path"] for r in group}
        if len(files) >= min_files:
            bodies = {r["body_hash"] for r in group if r["body_hash"]}
            profile.reused.append(Reuse(
                name=name, kind=kind, files=len(files),
                distinct_bodies=len(bodies),
                distinct_shapes=len({r["signature_hash"] for r in group}),
                unhashed=sum(1 for r in group if not r["body_hash"]),
                examples=sorted(files)[:3]))
    profile.reused.sort(key=lambda x: (-x.files, x.name))

    # -- parameters and annotations ------------------------------------------
    names, annotated = Counter(), Counter()
    total_params = annotated_params = 0
    returns_total = returns_annotated = 0
    callables = 0
    async_count = 0
    for r in kept:
        if r["kind"] not in ("function", "method"):
            continue
        callables += 1
        if (r["canonical_signature"] or "").startswith("[async"):
            async_count += 1
        if not r["signature"]:
            continue
        signature = json.loads(r["signature"])
        returns_total += 1
        returns_annotated += bool(signature.get("returns"))
        for p in signature["params"]:
            if p["name"] in ("self", "cls"):
                continue
            total_params += 1
            names[p["name"]] += 1
            if p["annotation"]:
                annotated[p["name"]] += 1
                annotated_params += 1
    profile.params = [(n, c, annotated[n] / c) for n, c in names.most_common(10)]
    profile.param_annotation_rate = annotated_params / total_params if total_params else 0.0
    profile.return_annotation_rate = returns_annotated / returns_total if returns_total else 0.0
    profile.async_share = async_count / callables if callables else 0.0
    profile.mean_params = total_params / returns_total if returns_total else 0.0

    # -- docstrings and naming conventions -----------------------------------
    for kind in ("class", "function", "method"):
        group = [r for r in kept if r["kind"] == kind]
        profile.docstrings[kind] = (sum(1 for r in group if r["docstring"]), len(group))
    functions = [r for r in kept if r["kind"] in ("function", "method")]
    classes = [r for r in kept if r["kind"] == "class"]
    profile.naming["snake_case functions"] = (
        sum(1 for r in functions if SNAKE.match(r["name"])), len(functions))
    profile.naming["CapWords classes"] = (
        sum(1 for r in classes if CAPWORDS.match(r["name"])), len(classes))
    profile.naming["underscore-private callables"] = (
        sum(1 for r in functions if r["name"].startswith("_")
            and not r["name"].startswith("__")), len(functions))

    # -- decorators ---------------------------------------------------------
    decorators = Counter()
    for r in kept:
        for d in json.loads(r["decorators"] or "[]"):
            decorators[d["base"] or d["raw"]] += 1
    profile.decorators = decorators.most_common(10)

    # -- churn ------------------------------------------------------------------
    if profile.scans > 1:
        keep_ids = {r["uuid"] for r in kept}
        for r in connection.execute(
                "SELECT s.uuid, s.qualname, s.file_path,"
                "       COUNT(DISTINCT v.signature_hash) AS shapes"
                " FROM symbols s JOIN symbol_versions v ON v.symbol_uuid = s.uuid"
                " GROUP BY s.uuid HAVING shapes > 2 ORDER BY shapes DESC LIMIT 40"):
            if r["uuid"] in keep_ids:
                profile.churn.append((r["qualname"], r["file_path"], r["shapes"]))
            if len(profile.churn) == 10:
                break
    return profile


def render(profile: Profile, repo: str) -> str:
    out = []
    p = out.append
    p(f"{repo}: what the code keeps doing")
    p(f"scan {profile.scan_id} · {profile.symbols:,} live symbols in {profile.files:,} files"
      + (f" · {profile.tests_excluded:,} test symbols excluded (--include-tests)"
         if profile.tests_excluded else ""))
    p("")

    p("NAMES DEFINED AGAIN AND AGAIN — same name, separate definition, in many files")
    p("  'bodies' counts distinct code once docstrings and string wording are set aside:")
    p("  a reworded error message is the same body, a different exception is not.")
    p("  21 files / 1 body is copying; 21 files / 21 bodies is the same name doing")
    p("  different things everywhere. 'shapes' counts distinct parameter lists.")
    p("")
    if not profile.reused:
        p("  none defined in three or more files")
    unhashed = 0
    for r in profile.reused[:15]:
        unhashed += r.unhashed
        bodies = f"{r.distinct_bodies:>3}" if not r.unhashed else "  ?"
        note = " ← identical copies" if r.distinct_bodies == 1 and not r.unhashed \
            and r.files > 1 else ""
        p(f"  {r.files:>3} files  {bodies} bodies  {r.distinct_shapes:>3} shapes   "
          f"{r.kind:<8} {r.name}{note}")
        p(f"             e.g. {', '.join(r.examples)}")
    if unhashed:
        p("")
        p("  '?' — some of these definitions predate the body hash and have not been")
        p("  re-read since; run `spanda index` once to fill it in.")
    p("")

    p("PARAMETERS — how they are named, and whether the code says what they are")
    p(f"  {profile.param_annotation_rate:.0%} of parameters annotated · "
      f"{profile.return_annotation_rate:.0%} of callables annotate their return · "
      f"{profile.mean_params:.1f} parameters per callable on average")
    p("")
    for name, count, rate in profile.params:
        p(f"  {count:>6}  {name:<20} annotated {rate:>4.0%}")
    p("")

    p("DOCSTRINGS")
    for kind, (have, total) in profile.docstrings.items():
        p(f"  {kind:<9} {have / total if total else 0:>4.0%} of {total:,}")
    p("")

    p("NAMING")
    for label, (have, total) in profile.naming.items():
        p(f"  {label:<32} {have / total if total else 0:>4.0%} of {total:,}")
    p(f"  {'async callables':<32} {profile.async_share:>4.0%}")
    p("")

    if profile.decorators:
        p("DECORATORS MOST USED")
        for base, count in profile.decorators:
            p(f"  {count:>6}  @{base}")
        p("")

    if profile.scans > 1:
        p(f"CHURN — symbols whose shape changed most across {profile.scans} scans")
        if not profile.churn:
            p("  none changed shape more than twice")
        for qualname, file_path, shapes in profile.churn:
            p(f"  {shapes} shapes   {qualname:<40} {file_path}")
        p("")

    p("Every figure describes the corpus as it is now. None of them says whether")
    p("a pattern is good; that is the reader's call, and this report stops short of it.")
    return "\n".join(out)
