"""Stage 2 — global resolution.

Turns names into links. `total(order.items, 1.05)` in one file becomes an edge
to the definition of `total` in another, and `self.refund(0)` becomes an edge
to a method inherited from a base class in a third.

What it refuses to do is guess. Every reference it cannot place is recorded
with a reason, and the reasons are counted, because a resolver that quietly
drops what it cannot handle produces a call graph that looks complete and
is not. That is the failure this whole project exists to correct, and it
would be an easy one to reintroduce here of all places.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field

from spanda.modules import EXTERNAL, ModuleIndex, resolve_imports
from spanda.store import symbol_key

BUILTIN_NAMES = frozenset(dir(builtins))

# Why a reference could not be linked to a definition.
R_EXTERNAL = "external_module"          # lives in a library, not this codebase
R_BUILTIN = "builtin"                   # print, len, dict...
R_UNKNOWN_TYPE = "attribute_on_unknown_type"   # obj.method() on an unknown obj
R_NOT_FOUND = "not_found"               # a bare name nothing here defines
R_NO_SUCH_ATTR = "no_such_attribute"    # module or class known, member is not
R_STAR = "star_import_ambiguous"        # arrived via `import *`, source unclear
R_DYNAMIC = "dynamic_dispatch"          # the target is chosen at runtime
#: Not a failure: the name refers to a module in this codebase rather than to
#: a symbol in one. Counting it as unresolved would understate the resolver.
R_MODULE = "module_reference"


@dataclass(frozen=True)
class Target:
    """What a name in a file's scope refers to."""

    kind: str          # "symbol" | "module" | "external"
    symbol: str | None = None
    module: str | None = None
    via_star: bool = False


@dataclass(frozen=True)
class LostTrail:
    """A name the code imports from this codebase whose definition the
    resolver could not find.

    An import is proof of intent: someone wanted this symbol. Failing to place
    it is not an unused import, it is the resolver losing the trail — and
    unlike an ordinary unresolved reference, nothing downstream would flag
    it, because no reference is ever produced. Counted per run so a
    systematic blindness shows up as a number rather than as symbols that
    quietly look dead.
    """

    source_file: str
    line: int
    raw: str
    target_module: str
    name: str


@dataclass
class SymbolTable:
    """Every definition in the codebase, indexed the ways resolution needs."""

    #: module -> {top-level name -> symbol_key}
    module_names: dict[str, dict[str, str]] = field(default_factory=dict)
    #: symbol_key -> (kind, file, qualname)
    symbols: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    #: class symbol_key -> the base expressions written in the source
    class_bases: dict[str, list[str]] = field(default_factory=dict)
    #: class symbol_key -> {member name -> symbol_key}
    class_members: dict[str, dict[str, str]] = field(default_factory=dict)
    #: module -> __all__, when declared
    exports: dict[str, list[str]] = field(default_factory=dict)
    #: symbol_keys whose callers cannot be determined by reading the code
    dynamic: set[str] = field(default_factory=set)

    def add_record(self, record: dict, patterns: list[str]) -> None:
        from spanda.gaps import is_dynamic_dispatch

        module, file_path = record["module"], record["file"]
        self.module_names.setdefault(module, {})
        if record["dunder_all"]:
            self.exports[module] = record["dunder_all"]

        by_local: dict[str, str] = {}
        for definition in record["definitions"]:
            key = symbol_key(file_path, definition["qualname"], definition["kind"])
            by_local[definition["local_id"]] = key
            self.symbols[key] = (definition["kind"], file_path, definition["qualname"])

            if any(is_dynamic_dispatch(d["base"], patterns)
                   for d in definition["decorators"]):
                self.dynamic.add(key)

            parent = definition["parent"]
            if parent is None:
                self.module_names[module][definition["name"]] = key
            elif parent in by_local:
                self.class_members.setdefault(by_local[parent], {})[
                    definition["name"]] = key

            if definition["kind"] == "class":
                self.class_bases[key] = definition["bases"] or []

    def member(self, class_key: str, name: str, seen: set[str] | None = None,
               scopes: dict[str, dict[str, Target]] | None = None) -> str | None:
        """Find a member on a class or anything it inherits from.

        Bases are resolved through the scope of the file that declares them,
        which is what makes a base class in another module reachable. Cycles
        in an inheritance chain are expected rather than assumed away.
        """
        seen = seen if seen is not None else set()
        if class_key in seen:
            return None
        seen.add(class_key)

        direct = self.class_members.get(class_key, {}).get(name)
        if direct is not None:
            return direct

        _kind, file_path, _qualname = self.symbols.get(class_key, (None, None, None))
        if file_path is None or scopes is None:
            return None
        scope = scopes.get(file_path, {})
        for base in self.class_bases.get(class_key, []):
            root = base.partition("(")[0].partition("[")[0].strip()
            target = scope.get(root.rpartition(".")[2] if "." in root else root)
            if target and target.kind == "symbol":
                found = self.member(target.symbol, name, seen, scopes)
                if found is not None:
                    return found
        return None


def build_scope(record: dict, table: SymbolTable, index: ModuleIndex
                ) -> tuple[dict[str, Target], list[tuple]]:
    """Every name visible at the top of one file, and what it refers to.

    Returns the scope plus the lookups it could not finish: names imported
    from a module that does not define them, which means that module imported
    them from somewhere else in turn. Those are resolved by `build_scopes`
    once every module has a scope to consult.
    """
    scope: dict[str, Target] = {}
    pending: list[tuple] = []

    # Names defined in this file — taken from the file's own record, never
    # from the symbol table's per-module dict. That dict is keyed by module
    # name, and twenty-six conftest.py files share the name "conftest": seeded
    # from it, a file's scope holds every same-named file's definitions, last
    # writer winning, and a bare call to `client()` resolves to a function in
    # a different directory. A wrong edge, not a missing one, and invisible to
    # the import audit because no import is involved.
    for definition in record["definitions"]:
        if definition["parent"] is None:
            scope[definition["name"]] = Target("symbol", symbol=symbol_key(
                record["file"], definition["qualname"], definition["kind"]))

    for edge in resolve_imports(record, index):
        target_module = (index.by_file.get(edge.target_file)
                         if edge.target_file else None)

        if edge.is_star:
            if target_module is not None:
                pending.append(("star", None, target_module, None, edge))
            continue

        for name, alias in edge.names:
            local = alias or name.partition(".")[0]
            if edge.target_file is None:
                scope[local] = Target("external", module=edge.target_module)
                continue
            if target_module is None:
                continue
            # `from . import handlers`, where the edge already points at the
            # submodule the name denotes. Distinguished from a re-export by
            # the module's own name: `flow_nodes.ai.ai_agent` is not what
            # `handle_ai_agent` denotes, so that one is a re-export to chase.
            # Conflating the two makes every symbol behind a package root
            # look uncalled.
            if target_module == name or target_module.endswith("." + name):
                scope[local] = Target("module", module=target_module)
                continue
            key = table.module_names.get(target_module, {}).get(name)
            if key is not None:
                scope[local] = Target("symbol", symbol=key)
            else:
                # The module does not define this name, so it re-exported it.
                # Following that is what makes a package root usable as an
                # import surface — and not following it makes every symbol
                # behind one look uncalled.
                scope[local] = Target("module", module=target_module)
                pending.append(("name", local, target_module, name, edge))
    return scope, pending


#: How many times to chase re-exports through further re-exports. Chains
#: longer than this are vanishingly rare, and a cap is what guarantees
#: termination when packages import each other in a circle.
REEXPORT_PASSES = 6


def build_scopes(collected, table: SymbolTable, index: ModuleIndex
                 ) -> tuple[dict[str, dict[str, Target]], list[LostTrail]]:
    """Build every module's scope, then chase re-exports to a fixed point.

    A single pass cannot do this: `from flow_nodes import handle_x` can only
    be resolved once `flow_nodes`'s own scope exists, and that scope may
    itself depend on a module not yet built. Iterating until nothing changes
    handles chains of any length, and circular imports, without recursion.
    """
    # Keyed by file path, never by module name. Twenty-six conftest.py files
    # in test directories without __init__.py all share the module name
    # "conftest"; keyed by name they overwrite each other, and every one of
    # them then resolves against whichever scope happened to be built last.
    scopes: dict[str, dict[str, Target]] = {}
    pending: dict[str, list[tuple]] = {}
    for record in collected:
        scope, unfinished = build_scope(record, table, index)
        scopes[record["file"]] = scope
        pending[record["file"]] = unfinished

    for _ in range(REEXPORT_PASSES):
        progressed = False
        for file_path, items in pending.items():
            remaining = []
            for kind, local, target_module, name, edge in items:
                source = scopes.get(index.file_for(target_module) or "", {})
                if kind == "star":
                    exported = table.exports.get(target_module)
                    names = (exported if exported is not None
                             else [n for n in source if not n.startswith("_")])
                    for exported_name in names:
                        found = source.get(exported_name)
                        if found is not None and found.kind == "symbol" \
                                and exported_name not in scopes[file_path]:
                            scopes[file_path][exported_name] = Target(
                                "symbol", symbol=found.symbol, via_star=True)
                            progressed = True
                    remaining.append((kind, local, target_module, name, edge))
                    continue
                found = source.get(name)
                # A re-export of something in this codebase is followed to
                # it; a re-export of an external name is external here too.
                # Both are answers. Only "still a module" is unfinished.
                if found is not None and found.kind in ("symbol", "external"):
                    scopes[file_path][local] = found
                    progressed = True
                else:
                    remaining.append((kind, local, target_module, name, edge))
            pending[file_path] = remaining
        if not progressed:
            break

    return scopes, audit_lost_trails(collected, scopes, table, index)


def audit_lost_trails(collected, scopes: dict[str, dict[str, Target]],
                      table: SymbolTable, index: ModuleIndex) -> list[LostTrail]:
    """Check the finished scopes against what the code imported.

    Deliberately independent of how the scopes were built. A first version
    read the scope-builder's own unfinished-work list, and so inherited its
    blind spots: the branch that caused the original re-export bug skipped
    that list entirely, and the audit stayed silent on the exact failure it
    existed to catch. This version asks only the question that matters —
    for every `from X import name` where X is in this codebase and `X.name`
    is not itself a module, did `name` end up as something real?
    """
    lost: list[LostTrail] = []
    for record in collected:
        scope = scopes.get(record["file"], {})
        for edge in resolve_imports(record, index):
            if edge.is_star or edge.target_file is None:
                continue
            target_module = index.by_file.get(edge.target_file)
            if target_module is None:
                continue
            for name, alias in edge.names:
                # `from . import db` names a module, and that is its answer.
                joined = f"{target_module}.{name}" if target_module else name
                if index.file_for(joined) is not None or target_module == name \
                        or target_module.endswith("." + name):
                    continue
                local = alias or name
                final = scope.get(local)
                if final is None or final.kind == "module":
                    lost.append(LostTrail(
                        source_file=edge.source_file, line=edge.line,
                        raw=edge.raw, target_module=target_module, name=name))
    return lost


def _annotation_name(annotation: str) -> str | None:
    """The one class an annotation names, or None if it names something else.

    Handles what a resolver can honestly act on — a bare name, a forward
    reference in quotes, `X | None`, `Optional[X]` — and refuses the rest.
    `list[Order]` is a list; attribute access on it reaches list, not Order,
    and guessing otherwise would be exactly the kind of plausible wrong
    answer this project exists to avoid.
    """
    text = annotation.strip().strip("'\"")
    if text.startswith("Optional[") and text.endswith("]"):
        text = text[len("Optional["):-1].strip()
    elif "|" in text:
        parts = [p.strip() for p in text.split("|")]
        parts = [p for p in parts if p != "None"]
        if len(parts) != 1:
            return None
        text = parts[0]
    if not text or any(c in text for c in "[](), "):
        return None
    return text


def _class_for_annotation(annotation: str, scope: dict[str, Target],
                          table: SymbolTable) -> tuple[str | None, str | None]:
    """Resolve an annotation to a class in this codebase, or say why not."""
    name = _annotation_name(annotation)
    if name is None:
        return None, R_UNKNOWN_TYPE
    head, _dot, rest = name.partition(".")
    target = scope.get(head)
    if target is None:
        return None, R_BUILTIN if head in BUILTIN_NAMES else R_NOT_FOUND
    if target.kind == "external":
        return None, R_EXTERNAL
    if target.kind == "module":
        key = table.module_names.get(target.module, {}).get(rest) if rest else None
        return (key, None) if key and table.symbols[key][0] == "class" \
            else (None, R_NO_SUCH_ATTR)
    if rest:
        return None, R_UNKNOWN_TYPE
    if table.symbols.get(target.symbol, (None,))[0] == "class":
        return target.symbol, None
    return None, R_UNKNOWN_TYPE


@dataclass
class Reference:
    """One resolved — or explicitly unresolved — reference."""

    source_file: str
    source_symbol: str | None      # None means module-level code
    target_symbol: str | None
    edge_type: str                 # calls | inherits | uses
    raw: str
    line: int
    reason: str | None = None


def _edge_type(context: str) -> str:
    if context == "call":
        return "calls"
    if context == "base_class":
        return "inherits"
    return "uses"


def resolve_record(record: dict, table: SymbolTable,
                   scopes: dict[str, dict[str, Target]]) -> list[Reference]:
    """Resolve every reference in one file against the whole-codebase table."""
    scope = scopes.get(record["file"], {})
    enclosing_class: dict[str, str] = {}
    owner: dict[str, str] = {}

    by_local = {d["local_id"]: d for d in record["definitions"]}
    # Annotated parameters, per definition: the type the code itself declares
    # for a name, which is the single largest source of otherwise-unknowable
    # attribute access. On the target codebase 85% of `param.method()` references sit
    # under an annotation the resolver was ignoring.
    annotated: dict[str, dict[str, str]] = {}
    for definition in record["definitions"]:
        signature = definition.get("signature")
        if signature:
            annotated[definition["local_id"]] = {
                p["name"]: p["annotation"] for p in signature["params"]
                if p.get("annotation")}
    # Functions defined inside another function. A closure passed to re.sub
    # as a callback is a local name, so Stage 1 marks it resolved-locally and
    # nothing here would look further — leaving a function that is plainly
    # used reported as having no callers.
    nested: dict[tuple[str, str], str] = {}
    for definition in record["definitions"]:
        parent = definition["parent"]
        if parent is not None and by_local.get(parent, {}).get("kind") != "class":
            nested[(parent, definition["name"])] = symbol_key(
                record["file"], definition["qualname"], definition["kind"])

    for definition in record["definitions"]:
        key = symbol_key(record["file"], definition["qualname"], definition["kind"])
        owner[definition["local_id"]] = key
        parent = definition["parent"]
        while parent is not None and by_local.get(parent, {}).get("kind") != "class":
            parent = by_local.get(parent, {}).get("parent")
        if parent is not None:
            enclosing_class[definition["local_id"]] = symbol_key(
                record["file"], by_local[parent]["qualname"], "class")

    out: list[Reference] = []
    for reference in record["references"]:
        chain = reference["chain"]
        if not chain:
            continue
        source = owner.get(reference["enclosing"])
        edge_type = _edge_type(reference["context"])

        def emit(target: str | None, reason: str | None = None) -> None:
            out.append(Reference(
                source_file=record["file"], source_symbol=source,
                target_symbol=target, edge_type=edge_type,
                raw=reference["raw"], line=reference["line"], reason=reason))

        root = chain[0]

        # `self.method()` — resolvable through the enclosing class and its
        # bases, and the single most common intra-class call there is.
        if root in ("self", "cls") and len(chain) > 1:
            owning = enclosing_class.get(reference["enclosing"])
            found = table.member(owning, chain[1], scopes=scopes) if owning else None
            emit(found, None if found else R_UNKNOWN_TYPE)
            continue

        if reference["local"]:
            # A local name used bare is already accounted for — unless it
            # names a function defined in this same scope, which is a real
            # reference to a real definition.
            if len(chain) == 1:
                inner = nested.get((reference["enclosing"], root))
                if inner is not None:
                    emit(inner)
                continue
            # `param.member` where the signature says what `param` is.
            annotation = annotated.get(reference["enclosing"], {}).get(root)
            if annotation is None:
                emit(None, R_UNKNOWN_TYPE)
                continue
            class_key, reason = _class_for_annotation(annotation, scope, table)
            if class_key is None:
                emit(None, reason)
                continue
            found = table.member(class_key, chain[1], scopes=scopes)
            emit(found, None if found else R_NO_SUCH_ATTR)
            continue

        target = scope.get(root)
        if target is None:
            emit(None, R_BUILTIN if root in BUILTIN_NAMES else R_NOT_FOUND)
            continue

        if target.kind == "external":
            emit(None, R_EXTERNAL)
            continue

        if target.kind == "module":
            if len(chain) == 1:
                emit(None, R_MODULE)
                continue
            key = table.module_names.get(target.module, {}).get(chain[1])
            emit(key, None if key else R_NO_SUCH_ATTR)
            continue

        # A symbol in this codebase.
        if len(chain) == 1:
            emit(target.symbol, R_STAR if target.via_star else None)
            continue

        kind = table.symbols.get(target.symbol, (None,))[0]
        if kind == "class":
            found = table.member(target.symbol, chain[1], scopes=scopes)
            emit(found, None if found else R_NO_SUCH_ATTR)
        else:
            emit(None, R_UNKNOWN_TYPE)
    return out
