# Fixture codebase — the answer key

`sample_pkg/` is a small Python package that exists only to be parsed. Every
file in it is there to break one specific thing. Because it is small enough to
read in full, we know all the correct answers in advance — which is the whole
point. Output from the extractor is graded against this document, not eyeballed
for plausibility.

**This package is never imported, only parsed.** The circular import in
`a.py`/`b.py` is genuine and would raise `ImportError` at runtime; the
`sqlalchemy` imports in `models.py` refer to a library that is not installed.
Both are deliberate.

## Recording rules assumed by the counts below

- Every `def`, `async def` and `class` is a definition, at any nesting depth.
- Assignments are recorded at **module scope and class scope only**. Locals
  inside function bodies are not recorded, including `self.x = ...`.
- `__all__` and `VERSION` are ordinary module-level variables and are counted.

## Expected definition counts

| File | Defs | fn | class | method | var |
|---|---:|---:|---:|---:|---:|
| `__init__.py` | 2 | 0 | 0 | 0 | 2 |
| `consumer.py` | 2 | 1 | 0 | 0 | 1 |
| `lazy.py` | 1 | 1 | 0 | 0 | 0 |
| `batch.py` | 5 | 4 | 0 | 0 | 1 |
| `a.py` | 3 | 2 | 0 | 0 | 1 |
| `b.py` | 1 | 1 | 0 | 0 | 0 |
| `checkout.py` | 4 | 4 | 0 | 0 | 0 |
| `base.py` | 4 | 0 | 1 | 2 | 1 |
| `derived.py` | 4 | 0 | 1 | 2 | 1 |
| `dynamic.py` | 5 | 4 | 0 | 0 | 1 |
| `handlers.py` | 2 | 2 | 0 | 0 | 0 |
| `helpers.py` | 4 | 3 | 0 | 0 | 1 |
| `models.py` | 13 | 2 | 2 | 5 | 4 |
| `nested.py` | 8 | 4 | 2 | 1 | 1 |
| `recursion.py` | 3 | 3 | 0 | 0 | 0 |
| `registry/__init__.py` | 1 | 0 | 0 | 0 | 1 |
| `registry/impl.py` | 1 | 1 | 0 | 0 | 0 |
| `star.py` | 1 | 1 | 0 | 0 | 0 |
| `broken.py` | — | unparseable; the reported line and wording vary by interpreter |
| **Total** | **64** | 33 | 6 | 10 | 15 |

19 files: 18 parse, 1 does not.

## What each file proves

| File | The thing it breaks |
|---|---|
| `__init__.py` | Re-exports. `Order` is importable here but *defined* in `models.py`. |
| `checkout.py` | Attribute access on an annotated parameter — `method: PaymentMethod` then `method.charge()`. Three resolvable forms and one unannotated control. |
| `registry/` → `consumer.py` | A three-hop re-export: defined in `registry/impl.py`, re-exported by `registry/__init__.py`, re-exported again by the package root, used from `consumer.py`. |
| `a.py` ↔ `b.py` | A real circular import. Cycle detection must group them, not hang. |
| `models.py` | The core case: `@event.listens_for` dispatch, plus unresolvable external imports. |
| `base.py` → `derived.py` | Inheritance across a file boundary. |
| `helpers.py` | `__all__` that deliberately excludes `_internal_only`. |
| `star.py` | `from .helpers import *` — names used with no import statement naming them. |
| `handlers.py` | Two functions with zero static references that are called at runtime. |
| `dynamic.py` | `getattr`/`hasattr` dispatch, runtime-assembled names, conditional import, and an `importlib.import_module` call the import audit cannot see. |
| `recursion.py` | Self- and mutual recursion. The graph is not a DAG. |
| `lazy.py` | An import inside a function body, called through. The import audit passes on it; the call must still become an edge. |
| `batch.py` | Loops: two nested in one body, one calling a two-loop function (three deep across the call), and a database call inside a loop. |
| `nested.py` | Nested function, nested class, async, and every parameter kind. |
| `broken.py` | A syntax error must be recorded, not fatal. |

## Specific assertions the extractor must satisfy

**Decorators — exactly 6 decorated definitions, recorded verbatim:**

| Symbol | Decorator |
|---|---|
| `base.PaymentMethod.charge` | `abstractmethod` |
| `models.Order.subtotal` | `property` |
| `models.Order.is_terminal` | `staticmethod` |
| `models.Order.empty` | `classmethod` |
| `models._apply_rls_context` | `event.listens_for(Session, 'before_flush')` |
| `nested.expensive_lookup` | `functools.lru_cache(maxsize=128)` |

Of these, **exactly one** is dynamic dispatch: `event.listens_for`. Flagging
`lru_cache` or `property` too would make the flag meaningless — precision here
matters as much as recall.

**Qualnames.** `Outer.Inner.ping`, not `ping`. `make_multiplier.multiply`
carries its parent. `Order.total`, not `total`.

**Signatures.** `nested.fetch_menu` must come out with `restaurant_id` as
positional-only, `*sections` as varargs, `locale` as keyword-only with default
`'en'`, `**options` as kwargs, return annotation `dict`, and `is_async: true`.

**Dynamic hints.** At minimum: two `getattr` sites and one `hasattr` site in
`dynamic.py`, and the identifier-shaped string literals `"on_created"` and
`"on_paid"` in `HANDLER_NAMES` — which happen to name two real functions in
`handlers.py` that nothing references.

**Unparseable file.** `broken.py` recorded with `parse_status: "syntax_error"`,
a message, a line, and zero definitions. The run completes.

The message and line are the interpreter's, and differ between versions —
3.9 reports "invalid syntax" at line 10, 3.14 "\'(\' was never closed" at line
9. Everything else in this answer key is identical across both, hashes
included, which is what makes an index portable between them.

## The re-export chain, and why it has its own file

`register_node` is defined in `registry/impl.py`, re-exported by
`registry/__init__.py`, re-exported again by `sample_pkg/__init__.py`, and
used in `consumer.py`. Four files, three hops.

This shape is here because it broke on real code. A resolver that looks only
for names a module *defines* — never for names it *imported* — loses the trail
at the first `__init__.py`, and everything behind that package root reports as
having no callers. On the target codebase it made 28 of 31 live flow handlers look dead.

The trap is that nothing complains. The tool's own honesty machinery cannot
help: it is not unsure, it simply never saw the reference. Any change to
scope building must keep `consumer.py` resolving all the way through to
`registry/impl.py`.

## The three symbols that make or break this project

`models._apply_rls_context`, `handlers.on_created`, `handlers.on_paid`.

All three are called at runtime. None is referenced by name anywhere in the
package. A tool that reports "0 callers" for any of them has reproduced the
exact failure this project exists to fix. The correct output is an explicit
"cannot determine callers", with the reason attached.
