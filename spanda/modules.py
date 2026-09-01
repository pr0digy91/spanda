"""Stage 0 — import graph, module resolution, and cycle detection.

Resolving a name across files starts here: `from .base import PaymentMethod`
has to become an actual file before anything can be said about what
`PaymentMethod` refers to. This module answers that, and nothing else — it
does not look at a single symbol.

Its second output is the cycle groups. Python codebases have circular
imports, and a resolver that assumes otherwise either loops or quietly
truncates. Cycles are found and reported, not worked around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: Why an import could not be pointed at a file in this codebase.
EXTERNAL = "external"          # stdlib or an installed package
UNRESOLVED = "unresolved"      # looks internal, but no file matches
RELATIVE_ESCAPE = "relative_escape"  # `from ...x import y` climbing past the top


@dataclass
class ImportEdge:
    """One import statement, resolved as far as it can be."""

    source_file: str
    raw: str
    line: int
    #: The dotted module the statement names, made absolute.
    target_module: str | None
    #: The file it resolves to, or None with `reason` explaining why not.
    target_file: str | None = None
    reason: str | None = None
    is_star: bool = False
    conditional: bool = False
    #: Names the statement brings in, as (name, alias).
    names: list[tuple[str, str | None]] = field(default_factory=list)


@dataclass
class ModuleIndex:
    """Which dotted module name lives in which file, and the reverse.

    Built from what the extractor already recorded, so no file is read twice.
    """

    by_module: dict[str, str] = field(default_factory=dict)
    by_file: dict[str, str] = field(default_factory=dict)
    packages: set[str] = field(default_factory=set)
    #: Top-level package names present in this codebase. Maintained as
    #: modules are added rather than rebuilt per lookup: it was being
    #: recomputed from every module for every one of thousands of imports.
    roots: set[str] = field(default_factory=set)

    def add(self, file_path: str, module: str) -> None:
        self.by_module[module] = file_path
        self.by_file[file_path] = module
        self.roots.add(module.partition(".")[0])
        if file_path.endswith("__init__.py"):
            self.packages.add(module)

    def is_package(self, module: str) -> bool:
        return module in self.packages

    def file_for(self, module: str) -> str | None:
        return self.by_module.get(module)


def absolute_module(importing_module: str, is_package: bool,
                    module: str | None, level: int) -> str | None:
    """Turn a possibly-relative import into an absolute dotted name.

    Python's rule, which is easy to get subtly wrong: inside a package's
    `__init__.py`, a single dot means *that package*; inside an ordinary
    module it means the package containing it.
    """
    if level == 0:
        return module or None

    base = importing_module if is_package else importing_module.rpartition(".")[0]
    for _ in range(level - 1):
        if not base:
            return None  # climbed past the top of the tree
        base = base.rpartition(".")[0]

    if level > 1 and not base:
        return None
    if module:
        return f"{base}.{module}" if base else module
    return base or None


def resolve_imports(record: dict, index: ModuleIndex) -> list[ImportEdge]:
    """Point every import in one file at a file, or say why it cannot be."""
    importing = record["module"]
    is_package = record["file"].endswith("__init__.py")
    edges: list[ImportEdge] = []

    for statement in record["imports"]:
        raw_module = statement["module"]
        level = statement["level"]
        # `from ..db import Session` arrives as module="..db"; the dots are
        # already counted in `level`, so strip them before joining.
        cleaned = (raw_module or "").lstrip(".") or None

        if level == 0 and raw_module is None:
            # `import a.b.c` / `import a.b as x` — the module is in `names`.
            for name, alias in [(n["name"], n["alias"]) for n in statement["names"]]:
                edges.append(_edge(record, statement, name, index,
                                   names=[(name, alias)]))
            continue

        target = absolute_module(importing, is_package, cleaned, level)
        names = [(n["name"], n["alias"]) for n in statement["names"]]
        if target is None:
            edges.append(ImportEdge(
                source_file=record["file"], raw=statement["raw"],
                line=statement["line"], target_module=None,
                reason=RELATIVE_ESCAPE, is_star=statement["is_star"],
                conditional=statement["conditional"], names=names))
            continue

        # `from . import handlers` depends on the *submodule*, not on the
        # package's __init__. Resolving it to the package instead loses the
        # real edge — and in a package root that imports its own submodules,
        # loses it in exactly the place the dependency order matters most.
        submodules = [(n, a) for n, a in names
                      if not statement["is_star"]
                      and index.file_for(f"{target}.{n}") is not None]
        plain = [(n, a) for n, a in names if (n, a) not in submodules]

        for name, alias in submodules:
            edges.append(_edge(record, statement, f"{target}.{name}", index,
                               names=[(name, alias)]))
        if plain or not submodules:
            edges.append(_edge(record, statement, target, index, names=plain))
    return edges


def _edge(record: dict, statement: dict, target: str, index: ModuleIndex,
          names: list[tuple[str, str | None]]) -> ImportEdge:
    edge = ImportEdge(
        source_file=record["file"], raw=statement["raw"], line=statement["line"],
        target_module=target, is_star=statement["is_star"],
        conditional=statement["conditional"], names=names)

    resolved = index.file_for(target)
    if resolved is None and names and not statement["is_star"]:
        # `from pkg import submodule` names a module, not a symbol in one.
        for name, _alias in names:
            candidate = index.file_for(f"{target}.{name}")
            if candidate is not None:
                resolved = candidate
                edge.target_module = f"{target}.{name}"
                break

    if resolved is not None:
        edge.target_file = resolved
    else:
        # A dotted root that exists here but with no matching file is a gap in
        # this tool. A root that does not exist here is simply someone else's
        # package, which is not.
        root = target.partition(".")[0]
        edge.reason = UNRESOLVED if root in index.roots else EXTERNAL
    return edge


# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------

def build_import_graph(edges: list[ImportEdge],
                       all_files: list[str] | None = None) -> dict[str, set[str]]:
    """file -> the files it imports from. Only edges inside this codebase.

    Every file is a node, including ones that neither import nor are imported.
    A file missing from the graph would be missing from the processing order
    too, and silently skipping a file is how a tool comes to describe a
    codebase it has not fully read.
    """
    graph: dict[str, set[str]] = {f: set() for f in (all_files or [])}
    for edge in edges:
        graph.setdefault(edge.source_file, set())
        if edge.target_file and edge.target_file != edge.source_file:
            graph[edge.source_file].add(edge.target_file)
            graph.setdefault(edge.target_file, set())
    return graph


def strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative.

    Recursion would overflow the stack on a large codebase, and the whole
    point of this function is the case where the graph is not a tree.
    Components come back in reverse topological order.
    """
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for root in graph:
        if root in index_of:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(graph[root]))]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, pending = work[-1]
            if pending:
                child = pending.pop()
                if child not in index_of:
                    index_of[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, sorted(graph.get(child, ()))))
                elif child in on_stack:
                    low[node] = min(low[node], index_of[child])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index_of[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    components.append(sorted(component))
    return components


def processing_order(graph: dict[str, set[str]]) -> list[list[str]]:
    """Files in dependency order, cyclic groups kept together as one unit.

    Each unit is a list: one file, or a group whose members import each other
    and therefore have no correct order among themselves. Saying so is the
    point — a flat list would imply an ordering that does not exist.
    """
    components = strongly_connected_components(graph)
    where: dict[str, int] = {}
    for number, component in enumerate(components):
        for member in component:
            where[member] = number

    # Condense to a DAG of components, then Kahn's algorithm over that.
    outgoing: dict[int, set[int]] = {n: set() for n in range(len(components))}
    indegree: dict[int, int] = {n: 0 for n in range(len(components))}
    for source, targets in graph.items():
        for target in targets:
            a, b = where[source], where[target]
            if a != b and b not in outgoing[a]:
                outgoing[a].add(b)
                indegree[b] += 1

    # A file must come after what it imports, so walk dependencies first.
    ready = sorted(n for n, d in indegree.items() if d == 0)
    order: list[int] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for target in sorted(outgoing[node]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()

    assert len(order) == len(components), "condensation must be acyclic"
    return [components[n] for n in reversed(order)]


def cycle_groups(graph: dict[str, set[str]]) -> list[list[str]]:
    """Only the units that genuinely contain a cycle."""
    return [c for c in strongly_connected_components(graph)
            if len(c) > 1 or any(m in graph.get(m, ()) for m in c)]
