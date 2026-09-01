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
from spanda.store import path_module, symbol_key

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
        scope = scopes.get(path_module(file_path), {})
        for base in self.class_bases.get(class_key, []):
            root = base.partition("(")[0].partition("[")[0].strip()
            target = scope.get(root.rpartition(".")[2] if "." in root else root)
            if target and target.kind == "symbol":
                found = self.member(target.symbol, name, seen, scopes)
                if found is not None:
                    return found
        return None


def build_scope(record: dict, table: SymbolTable, index: ModuleIndex) -> dict[str, Target]:
    """Every name visible at the top of one file, and what it refers to."""
    module = record["module"]
    scope: dict[str, Target] = {}

    # Names defined in this file.
    for name, key in table.module_names.get(module, {}).items():
        scope[name] = Target("symbol", symbol=key)

    for edge in resolve_imports(record, index):
        target_module = (index.by_file.get(edge.target_file)
                         if edge.target_file else None)

        if edge.is_star:
            if target_module is None:
                continue
            # `import *` honours __all__ when it is declared; without one,
            # everything not underscore-prefixed.
            exported = table.exports.get(target_module)
            available = table.module_names.get(target_module, {})
            names = exported if exported is not None else [
                n for n in available if not n.startswith("_")]
            for name in names:
                key = available.get(name)
                if key is not None:
                    scope[name] = Target("symbol", symbol=key, via_star=True)
            continue

        for name, alias in edge.names:
            local = alias or name.partition(".")[0]
            if edge.target_file is None:
                scope[local] = Target("external", module=edge.target_module)
                continue
            if target_module is None:
                continue
            # `from . import handlers` resolved to the submodule itself.
            if edge.target_module == target_module and name not in \
                    table.module_names.get(target_module, {}):
                scope[local] = Target("module", module=target_module)
                continue
            key = table.module_names.get(target_module, {}).get(name)
            if key is not None:
                scope[local] = Target("symbol", symbol=key)
            else:
                scope[local] = Target("module", module=target_module)
    return scope


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
    scope = scopes.get(record["module"], {})
    enclosing_class: dict[str, str] = {}
    owner: dict[str, str] = {}

    by_local = {d["local_id"]: d for d in record["definitions"]}
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
            # A local name used bare is already accounted for. Used as
            # `thing.method()`, the type of `thing` is not knowable here.
            if len(chain) > 1:
                emit(None, R_UNKNOWN_TYPE)
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
