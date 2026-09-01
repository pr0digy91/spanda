"""Stage 1 — per-file local extraction.

Opens one Python file, parses it with the stdlib AST, and writes down what is
defined in it and what names it uses. It resolves nothing: a reference here is
a name and a location, never a link to a definition. That is Stage 2's job,
and keeping the two apart is what makes this stage's output checkable by
reading it.

The one rule this module follows throughout: record what the parser sees,
refuse to record anything that requires a guess.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    # `.spanda` holds this tool's own index files. Reading them back would
    # make the tool part of what it measures.
    ".spanda",
    "__pycache__", ".git", ".hg", ".svn", ".venv", "venv", "env",
    "node_modules", "build", "dist", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "site-packages", ".eggs",
}

IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*\Z")

#: Call names whose targets cannot be known statically. Recorded as hints so
#: the gap is visible rather than absent.
DYNAMIC_BUILTINS = {"getattr", "setattr", "hasattr", "delattr"}

#: Imports performed by calling a function. Not import statements, so the
#: resolver's import audit cannot see them, and whatever they load has no
#: static importer and looks unreferenced. Recorded as their own hint kind.
DYNAMIC_IMPORTS = {"__import__", "importlib.import_module", "import_module"}


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _read_source(path: Path) -> str:
    """Read using Python's own encoding detection, so a coding cookie is honoured."""
    try:
        with tokenize.open(path) as fh:
            return fh.read()
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# module naming
# --------------------------------------------------------------------------

def module_name_for(path: Path, root: Path) -> str:
    """Dotted module name, derived by walking up while __init__.py exists.

    The package root is the first ancestor directory without an __init__.py,
    which is the same rule Python itself uses to decide where a package ends.
    """
    parts: list[str] = []
    stem = path.stem
    directory = path.parent
    # Stop at the root, never above it. The root is the directory that would
    # be on sys.path, so it is not itself part of any module name — even when
    # it contains an __init__.py, which many project roots do. Including it
    # prefixes every module with the checkout's directory name, and then no
    # import in the codebase resolves against anything.
    while directory != root and (directory / "__init__.py").exists():
        parts.append(directory.name)
        directory = directory.parent
    parts.reverse()
    if stem != "__init__":
        parts.append(stem)
    # A bare __init__.py at the scan root is the root package itself, whose
    # name is the empty string — not a module called "__init__".
    return ".".join(parts)


# --------------------------------------------------------------------------
# signatures
# --------------------------------------------------------------------------

def _unparse(node) -> str | None:
    return None if node is None else ast.unparse(node)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    """Full parameter list, preserving the distinction between parameter kinds."""
    args = node.args
    params: list[dict] = []

    def add(arg: ast.arg, kind: str, default) -> None:
        params.append({
            "name": arg.arg,
            "kind": kind,
            "annotation": _unparse(arg.annotation),
            "default": _unparse(default),
        })

    positional = list(args.posonlyargs) + list(args.args)
    # `defaults` fills the tail of the positional list, so align from the right.
    padding = [None] * (len(positional) - len(args.defaults))
    for arg, default in zip(positional, padding + list(args.defaults)):
        kind = "positional_only" if arg in args.posonlyargs else "positional"
        add(arg, kind, default)

    if args.vararg:
        add(args.vararg, "vararg", None)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        add(arg, "keyword_only", default)
    if args.kwarg:
        add(args.kwarg, "kwarg", None)

    return {"params": params, "returns": _unparse(node.returns)}


#: Decorators that change how a symbol is *called*, and therefore belong in
#: its shape. `@property` decides whether callers write `obj.total` or
#: `obj.total()`; `@staticmethod` and `@classmethod` decide what the first
#: argument means. Every other decorator — `@lru_cache`, `@wraps`, a route —
#: leaves the calling convention alone and is deliberately excluded, or
#: bumping `maxsize=128` to `256` would report as a breaking change.
CALLING_CONVENTION_DECORATORS = {
    "property", "cached_property", "staticmethod", "classmethod",
    "functools.cached_property", "abc.abstractproperty",
}


def _convention_markers(is_async: bool, decorators: list[dict]) -> str:
    """The parts of a symbol's shape that are not its parameter list.

    `async` is here because it is a caller-visible contract change of the
    bluntest kind: every call site now receives a coroutine and must await it.
    Treating that as an internal edit would be exactly the false reassurance
    this project exists to eliminate.
    """
    markers = ["async"] if is_async else []
    markers += sorted(
        d["base"] for d in decorators
        if d["base"] in CALLING_CONVENTION_DECORATORS)
    return ("[" + ",".join(markers) + "]") if markers else ""


def _canonical_signature(kind: str, signature: dict | None, bases: list[str] | None,
                         annotation: str | None, is_async: bool = False,
                         decorators: list[dict] | None = None) -> str:
    """A normalised shape string, hashed to detect signature drift.

    Built from the AST rather than source text on purpose: reformatting must
    not read as a shape change. It covers everything a caller can be broken
    by — parameters, defaults, annotations, async, and the decorators that
    alter the calling convention — and nothing else.
    """
    markers = _convention_markers(is_async, decorators or [])
    if kind in ("function", "method") and signature is not None:
        rendered = []
        for param in signature["params"]:
            piece = f"{param['kind']}:{param['name']}"
            if param["annotation"]:
                piece += f":{param['annotation']}"
            if param["default"] is not None:
                piece += f"={param['default']}"
            rendered.append(piece)
        return (markers + "(" + ",".join(rendered) + ")->"
                + (signature["returns"] or ""))
    if kind == "class":
        # A class's shape is the bases it inherits from: change those and the
        # methods callers can reach change with them.
        return markers + "bases(" + ",".join(bases or []) + ")"
    return markers + ":" + (annotation or "")


# --------------------------------------------------------------------------
# the walk
# --------------------------------------------------------------------------

class _BindingCollector(ast.NodeVisitor):
    """Names bound inside one function body, without descending into nested
    scopes. Used to mark references that are plainly local, so they are not
    handed to Stage 2 as if they were unresolvable cross-file references."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.declared_elsewhere: set[str] = set()

    def visit_FunctionDef(self, node) -> None:
        self.names.add(node.name)  # bound here; its body is a separate scope

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node) -> None:
        pass

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add((alias.asname or alias.name).split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name)

    def visit_Global(self, node: ast.Global) -> None:
        self.declared_elsewhere.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.declared_elsewhere.update(node.names)


def _local_bindings(node) -> set[str]:
    collector = _BindingCollector()
    for statement in node.body:
        collector.visit(statement)
    args = node.args
    for arg in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                + [a for a in (args.vararg, args.kwarg) if a]):
        collector.names.add(arg.arg)
    return collector.names - collector.declared_elsewhere


@dataclass
class _Scope:
    local_id: str | None
    qualname: str
    is_class: bool
    is_function: bool
    bindings: set[str] = field(default_factory=set)


@dataclass
class _FileResult:
    definitions: list[dict] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)
    dynamic_hints: list[dict] = field(default_factory=list)
    imports: list[dict] = field(default_factory=list)


def _dotted_chain(node) -> list[str] | None:
    """Flatten `a.b.c` to ['a','b','c']; None if it does not bottom out in a name."""
    chain: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        chain.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    chain.append(current.id)
    chain.reverse()
    return chain


def _body_hash(node) -> str:
    """Hash of what a definition *does*, ignoring what it says.

    `content_hash` moves when a docstring is added or an error message is
    reworded, which makes two functions with identical logic count as two
    implementations. This hash walks the syntax tree instead: docstrings are
    left out, every string literal is reduced to "a string", and the
    definition's own name is dropped, so `_enforce_role` in fourteen files
    with fourteen differently worded messages hashes the same fourteen times
    — and differently the moment one of them raises a different exception.
    """
    parts: list[str] = []

    def emit(value, drop_name: bool = False) -> None:
        if isinstance(value, ast.Constant):
            # Identifier text (`id`, `attr`, `arg`) never arrives here — it
            # is a plain str field on another node — so this neutralises
            # only literal strings.
            parts.append("<str>" if isinstance(value.value, str)
                         else repr(value.value))
        elif isinstance(value, ast.AST):
            parts.append(type(value).__name__ + "(")
            for field, child in ast.iter_fields(value):
                if drop_name and field == "name":
                    continue
                if field == "body" and isinstance(child, list) \
                        and _docstring_of(value) is not None:
                    child = child[1:]  # a lambda's body is one expression
                parts.append(field + "=")
                emit(child)
                parts.append(",")
            parts.append(")")
        elif isinstance(value, list):
            parts.append("[")
            for item in value:
                emit(item)
                parts.append(",")
            parts.append("]")
        else:
            parts.append(repr(value))

    emit(node, drop_name=isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    return _sha256("".join(parts))


def _docstring_of(node) -> int | None:
    """The id of a body's leading string constant, if it has one.

    Noted as each definition is entered rather than by a separate walk over
    the whole tree: that second walk cost as much as everything else the
    extractor does for a file, to find nodes the main walk passes anyway.
    """
    body = getattr(node, "body", None)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return id(body[0].value)
    return None


class _Walker(ast.NodeVisitor):
    def __init__(self, source_lines: list[str], dunder_all: list[str],
                 module_docstring: int | None) -> None:
        self.lines = source_lines
        self.dunder_all = dunder_all
        self.docstrings: set[int] = {module_docstring} if module_docstring else set()
        self.out = _FileResult()
        self.scopes: list[_Scope] = [_Scope(None, "", False, False)]
        self.context = "name"
        self.conditional = 0
        self._counter = 0

    # -- helpers ----------------------------------------------------------
    @property
    def scope(self) -> _Scope:
        return self.scopes[-1]

    def _next_id(self) -> str:
        self._counter += 1
        return f"d{self._counter}"

    def _source_of(self, start: int, end: int) -> str:
        segment = self.lines[start - 1:end]
        return "\n".join(line.rstrip() for line in segment)

    def _is_local(self, root: str | None) -> bool:
        """True if the root name is bound in an enclosing function scope."""
        if root is None:
            return False
        return any(scope.is_function and root in scope.bindings
                   for scope in reversed(self.scopes))

    def _record_reference(self, node, raw: str, chain: list[str] | None) -> None:
        root = chain[0] if chain else None
        self.out.references.append({
            "raw": raw,
            "root": root,
            "chain": chain,
            "context": self.context,
            "line": node.lineno,
            "enclosing": self.scope.local_id,
            # Resolved here, within the file's own scope. Everything else is
            # handed to Stage 2 still unresolved.
            "local": self._is_local(root),
        })

    #: Contexts that describe *where* a reference sits structurally. A call
    #: nested inside one of these keeps the outer label, so that
    #: `@event.listens_for(...)` is not counted as an ordinary call site.
    STRUCTURAL = {"decorator", "base_class", "annotation"}

    def _in_context(self, context: str, visit):
        if context == "call" and self.context in self.STRUCTURAL:
            visit()
            return
        previous, self.context = self.context, context
        visit()
        self.context = previous

    # -- definitions ------------------------------------------------------
    def _add_definition(self, node, kind: str, name: str, *, signature=None,
                        bases=None, annotation=None, lines=None,
                        decorators=None, docstring=None) -> str:
        local_id = self._next_id()
        qualname = f"{self.scope.qualname}.{name}" if self.scope.qualname else name
        start, end = lines
        decorator_lines = None
        if getattr(node, "decorator_list", None):
            decorator_lines = [
                min(d.lineno for d in node.decorator_list),
                max(d.end_lineno for d in node.decorator_list),
            ]
            start = min(start, decorator_lines[0])

        canonical = _canonical_signature(
            kind, signature, bases, annotation,
            is_async=isinstance(node, ast.AsyncFunctionDef),
            decorators=decorators or [])
        self.out.definitions.append({
            "local_id": local_id,
            "kind": kind,
            "name": name,
            "qualname": qualname,
            "parent": self.scope.local_id,
            "lines": [start, end],
            "decorator_lines": decorator_lines,
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "is_private": name.startswith("_") and not (
                name.startswith("__") and name.endswith("__")),
            "in_dunder_all": name in self.dunder_all,
            "signature": signature,
            "bases": bases,
            "annotation": annotation,
            "decorators": decorators or [],
            "docstring": docstring,
            "canonical_signature": canonical,
            "signature_hash": _sha256(canonical),
            "content_hash": _sha256(self._source_of(start, end)),
            "body_hash": _body_hash(node),
        })
        return local_id

    def _decorators(self, node) -> list[dict]:
        rendered = []
        for decorator in getattr(node, "decorator_list", []):
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            chain = _dotted_chain(target)
            rendered.append({
                "raw": ast.unparse(decorator),
                "base": ".".join(chain) if chain else None,
            })
        return rendered

    def _visit_def(self, node) -> None:
        doc = _docstring_of(node)
        if doc is not None:
            self.docstrings.add(doc)
        kind = "method" if self.scope.is_class else "function"
        local_id = self._add_definition(
            node, kind, node.name,
            signature=_signature(node),
            lines=[node.lineno, node.end_lineno],
            decorators=self._decorators(node),
            docstring=ast.get_docstring(node),
        )
        qualname = f"{self.scope.qualname}.{node.name}" if self.scope.qualname else node.name
        self.scopes.append(_Scope(local_id, qualname, is_class=False, is_function=True,
                                  bindings=_local_bindings(node)))
        # Decorators evaluate in the enclosing scope, but are attributed to the
        # symbol they decorate: that is where they need to be findable later.
        self._in_context("decorator", lambda: [self.visit(d) for d in node.decorator_list])
        self._in_context("annotation", lambda: self._visit_signature(node))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def _visit_signature(self, node) -> None:
        args = node.args
        every = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        every += [a for a in (args.vararg, args.kwarg) if a]
        for arg in every:
            if arg.annotation:
                self.visit(arg.annotation)
        for default in list(args.defaults) + [d for d in args.kw_defaults if d]:
            self.visit(default)
        if node.returns:
            self.visit(node.returns)

    visit_FunctionDef = _visit_def
    visit_AsyncFunctionDef = _visit_def

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        doc = _docstring_of(node)
        if doc is not None:
            self.docstrings.add(doc)
        bases = [ast.unparse(b) for b in node.bases]
        local_id = self._add_definition(
            node, "class", node.name,
            bases=bases,
            lines=[node.lineno, node.end_lineno],
            decorators=self._decorators(node),
            docstring=ast.get_docstring(node),
        )
        qualname = f"{self.scope.qualname}.{node.name}" if self.scope.qualname else node.name
        self.scopes.append(_Scope(local_id, qualname, is_class=True, is_function=False))
        self._in_context("decorator", lambda: [self.visit(d) for d in node.decorator_list])
        self._in_context("base_class", lambda: [self.visit(b) for b in node.bases])
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    # -- assignments ------------------------------------------------------
    def _assignment_names(self, target) -> list[ast.Name]:
        if isinstance(target, ast.Name):
            return [target]
        if isinstance(target, (ast.Tuple, ast.List)):
            return [n for element in target.elts for n in self._assignment_names(element)]
        return []

    def visit_Assign(self, node: ast.Assign) -> None:
        # Module and class scope only. Function locals are not callable or
        # importable from elsewhere, so they have no place in a call graph.
        if not self.scope.is_function:
            for target in node.targets:
                for name in self._assignment_names(target):
                    self._add_definition(node, "variable", name.id,
                                         lines=[node.lineno, node.end_lineno])
        self._in_context("assign_target",
                         lambda: [self.visit(t) for t in node.targets])
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self.scope.is_function and isinstance(node.target, ast.Name):
            self._add_definition(node, "variable", node.target.id,
                                 annotation=_unparse(node.annotation),
                                 lines=[node.lineno, node.end_lineno])
        self._in_context("annotation", lambda: self.visit(node.annotation))
        if node.value:
            self.visit(node.value)

    # -- imports ----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        self.out.imports.append({
            "raw": ast.unparse(node),
            "module": None,
            "names": [{"name": a.name, "alias": a.asname} for a in node.names],
            "level": 0,
            "is_star": False,
            "line": node.lineno,
            "conditional": self.conditional > 0,
        })

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        star = any(a.name == "*" for a in node.names)
        self.out.imports.append({
            "raw": ast.unparse(node),
            "module": ("." * node.level) + (node.module or ""),
            "names": [{"name": a.name, "alias": a.asname} for a in node.names],
            "level": node.level,
            "is_star": star,
            "line": node.lineno,
            "conditional": self.conditional > 0,
        })

    def _visit_conditional(self, node) -> None:
        self.conditional += 1
        self.generic_visit(node)
        self.conditional -= 1

    visit_Try = _visit_conditional
    visit_If = _visit_conditional

    # -- references and hints ---------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        chain = _dotted_chain(node.func)
        if chain and chain[-1] in DYNAMIC_BUILTINS and len(chain) == 1:
            self.out.dynamic_hints.append({
                "kind": chain[0],
                "raw": ast.unparse(node),
                "line": node.lineno,
                "enclosing": self.scope.local_id,
            })
        elif chain and ".".join(chain) in DYNAMIC_IMPORTS:
            self.out.dynamic_hints.append({
                "kind": "dynamic_import",
                "raw": ast.unparse(node),
                "line": node.lineno,
                "enclosing": self.scope.local_id,
            })
        self._in_context("call", lambda: self.visit(node.func))
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        chain = _dotted_chain(node)
        if chain is not None:
            # Identical to ast.unparse for a pure dotted chain, without the
            # cost of re-rendering a node whose parts are already in hand.
            self._record_reference(node, ".".join(chain), chain)
            return  # whole chain recorded once; do not also record its parts
        self._record_reference(node, ast.unparse(node), None)
        self.visit(node.value)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._record_reference(node, node.id, [node.id])

    def visit_Constant(self, node: ast.Constant) -> None:
        if (isinstance(node.value, str) and id(node) not in self.docstrings
                and IDENTIFIER_RE.match(node.value)):
            self.out.dynamic_hints.append({
                "kind": "identifier_string",
                "value": node.value,
                "line": node.lineno,
                "enclosing": self.scope.local_id,
            })


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def _extract_dunder_all(tree: ast.Module) -> list[str]:
    for statement in tree.body:
        targets = statement.targets if isinstance(statement, ast.Assign) else []
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                value = statement.value
                if isinstance(value, (ast.List, ast.Tuple)):
                    return [e.value for e in value.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def extract_file(path: Path, root: Path) -> dict:
    """Extract one file. Never raises for bad input — unparseable is a result."""
    relative = path.relative_to(root).as_posix()
    source = _read_source(path)
    record = {
        "file": relative,
        "module": module_name_for(path, root),
        "parse_status": "ok",
        "parse_error": None,
        "file_hash": _sha256(source),
        "module_docstring": None,
        "dunder_all": [],
        "imports": [],
        "definitions": [],
        "references": [],
        "dynamic_hints": [],
    }

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        record["parse_status"] = "syntax_error"
        record["parse_error"] = {"message": error.msg, "line": error.lineno}
        return record

    dunder_all = _extract_dunder_all(tree)
    walker = _Walker(source.splitlines(), dunder_all, _docstring_of(tree))
    for statement in tree.body:
        walker.visit(statement)

    record["module_docstring"] = ast.get_docstring(tree)
    record["dunder_all"] = dunder_all
    record["imports"] = walker.out.imports
    record["definitions"] = walker.out.definitions
    record["references"] = walker.out.references
    record["dynamic_hints"] = walker.out.dynamic_hints
    return record


@dataclass
class ScanPlan:
    """What a run intends to look at, decided before any file is opened.

    Cheap to build — it walks filenames only — so the caller can report the
    shape of a run, and what it is skipping, before committing to the work.
    """

    root: Path
    files: list[Path]
    skipped: dict[str, list[str]] = field(default_factory=dict)
    #: Python files on disk that git ignores, set aside by the CLI. A scan
    #: labelled "at commit X" must not contain files that commit does not.
    ignored: list[str] = field(default_factory=list)

    @property
    def skipped_count(self) -> int:
        return sum(len(paths) for paths in self.skipped.values()) + len(self.ignored)


@dataclass
class CodebaseScan:
    """Every record from one pass, held in memory at once.

    Only for consumers that genuinely need the whole codebase in view — the
    gap report needs a table of every name defined anywhere before it can say
    which ones nothing mentions. Everything else should use `stream_records`,
    which holds one file at a time.
    """

    root: Path
    records: list[dict]
    skipped: dict[str, list[str]] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    @property
    def skipped_count(self) -> int:
        return sum(len(paths) for paths in self.skipped.values())


def _count_python_files(directory: str) -> list[str]:
    """Paths of .py files under a directory that will not be parsed.

    Still counted, because a file the tool declined to read is a gap in its
    knowledge and the count is what makes that gap visible. Counted with
    scandir rather than rglob: no Path objects, no per-file relative_to, so
    the 19,000 files of a virtualenv cost a fraction of what they did.
    """
    found: list[str] = []
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.name.endswith(".py"):
                        found.append(entry.path)
        except OSError:
            continue
    return found


def partition_python_files(root: Path) -> tuple[list[Path], dict[str, list[str]]]:
    """Split every .py file under root into those to parse and those skipped.

    Returns the skipped ones grouped by which rule excluded them, so the
    exclusion can be reported rather than assumed harmless. Skipped
    directories are pruned before being descended into for parsing; they
    are only counted.
    """
    keep: list[Path] = []
    skipped: dict[str, list[str]] = {}
    root_str = str(root)
    prefix = len(root_str) + 1

    for current, dirnames, filenames in os.walk(root_str):
        pruned = [d for d in dirnames if d in SKIP_DIRS]
        for name in pruned:
            full = os.path.join(current, name)
            skipped.setdefault(name, []).extend(
                p[prefix:].replace(os.sep, "/") for p in _count_python_files(full))
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            if name.endswith(".py"):
                keep.append(Path(current) / name)

    keep.sort()
    for reason in skipped:
        skipped[reason].sort()
    return keep, skipped


def iter_python_files(root: Path):
    """Every .py file under root, excluding virtualenvs, caches and build output."""
    keep, _ = partition_python_files(root)
    yield from keep


def plan_scan(root: Path) -> ScanPlan:
    """Decide what to look at. Opens nothing."""
    root = root.resolve()
    keep, skipped = partition_python_files(root)
    return ScanPlan(root=root, files=keep, skipped=skipped)


def stream_records(plan: ScanPlan):
    """Yield one record per file, holding exactly one in memory at a time.

    This is the shape every consumer should use unless it truly needs the
    whole codebase at once. Accumulating all records first makes peak memory
    scale with codebase size — measured at 187 MB for 680 files, which
    extrapolates to roughly 2.7 GB at 10,000 files. Streaming makes peak
    memory a function of the largest single file instead, and therefore flat
    no matter how large the codebase grows.
    """
    for path in plan.files:
        yield extract_file(path, plan.root)


def extract_codebase(root: Path, plan: ScanPlan | None = None) -> CodebaseScan:
    """Materialise every record. Prefer `stream_records` unless the consumer
    needs a whole-codebase view; see CodebaseScan.

    Pass a plan to read exactly what another command would — the CLI hands
    in one with git-ignored files set aside, so `parse` and `index` never
    disagree about what the codebase is."""
    plan = plan or plan_scan(root)
    return CodebaseScan(
        root=plan.root,
        records=list(stream_records(plan)),
        skipped=plan.skipped,
        ignored=plan.ignored,
    )
