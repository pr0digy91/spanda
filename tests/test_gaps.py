"""The visible-gap guarantee, tested against the answer key.

fixtures/README.md names three symbols that are called at runtime and
referenced by name nowhere. Reporting any of them as plainly unused is the
exact failure this project exists to correct, so it is tested directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spanda.extract import extract_codebase
from spanda.gaps import (find_gaps, is_dynamic_dispatch, load_patterns,
                         unreferenced_symbols)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

# From fixtures/README.md, "The three symbols that make or break this project".
MAKE_OR_BREAK = {"_apply_rls_context", "on_created", "on_paid"}


@pytest.fixture(scope="module")
def scan():
    return extract_codebase(FIXTURES)


@pytest.fixture(scope="module")
def gaps(scan):
    return find_gaps(scan, load_patterns())


def test_the_decorated_hook_is_flagged(gaps):
    flagged = {g.symbol for g in gaps if g.kind == "dynamic_dispatch_decorator"}
    assert flagged == {"_apply_rls_context", "security_headers", "list_tools",
                       "Auditor.name_present"}


def test_an_override_the_framework_calls_by_name_is_flagged(gaps):
    """No decorator, no caller: `dispatch` on a BaseHTTPMiddleware subclass.
    A human vetting found this one alive on the dead list."""
    flagged = {g.symbol: g.detail for g in gaps if g.kind == "framework_method_override"}
    assert flagged == {"RequestLogger.dispatch": "overrides dispatch on BaseHTTPMiddleware"}


def test_a_decorator_on_neither_list_is_reported_as_unknown(gaps):
    """Not dead, not known: the reader is told the tool does not know."""
    unknown = {g.symbol: g.detail for g in gaps if g.kind == "unknown_decorator"}
    assert unknown == {"nightly_cleanup": '@scheduler.scheduled_job(\'cron\', hour=3)'}


def test_harmless_decorators_are_neither_dispatch_nor_unknown():
    from spanda.gaps import classify_decorator
    patterns = load_patterns()
    assert classify_decorator("functools.lru_cache", patterns) == "harmless"
    assert classify_decorator("property", patterns) == "harmless"
    assert classify_decorator("pytest.mark.parametrize", patterns) == "harmless"
    assert classify_decorator("event.listens_for", patterns) == "dispatch"
    assert classify_decorator("scheduler.scheduled_job", patterns) == "unknown"


def test_a_public_method_on_an_external_base_is_a_candidate(gaps):
    found = {g.symbol: g.detail for g in gaps if g.kind == "override_on_external_base"}
    assert set(found) == {"Auditor.on_validate"}, \
        "name_present is a validator, explained by its decorator, and not listed twice"
    assert "BaseModel" in found["Auditor.on_validate"]
    # dispatch is explained by its pattern line and not listed twice;
    # _helper is private; methods on internal bases (derived.py) resolve.


def test_a_class_a_framework_owns_by_inheritance_is_flagged(gaps):
    """Eleven SQLAlchemy models sat on a dead list: nothing in Python named
    them, and every one was a table Alembic owns."""
    owned = {g.symbol: g.detail for g in gaps if g.kind == "framework_owned_class"}
    assert owned == {"AuditLog": "inherits from Base"}
    from spanda.gaps import framework_class_base
    patterns = load_patterns()
    assert framework_class_base(["Base", "TimestampMixin"], patterns) == "Base"
    assert framework_class_base(["db.Model"], patterns) == "db.Model"
    assert framework_class_base(["BaseModel"], patterns) is None, \
        "a Pydantic schema is used by reference, not registered by inheritance"


def test_framework_method_matching_is_by_written_base_name():
    from spanda.gaps import is_framework_method
    patterns = load_patterns()
    assert is_framework_method(["BaseHTTPMiddleware"], "dispatch", patterns)
    assert is_framework_method(["starlette.middleware.base.BaseHTTPMiddleware"],
                               "dispatch", patterns)
    assert is_framework_method(["HTTPEndpoint"], "post", patterns)
    assert not is_framework_method(["BaseHTTPMiddleware"], "helper", patterns)
    assert not is_framework_method(["PaymentMethod"], "dispatch", patterns), \
        "an internal base with a method called dispatch is resolvable, not framework-called"
    assert not is_framework_method(None, "dispatch", patterns)


def test_ordinary_decorators_are_not_flagged():
    """Precision: flagging every decorated symbol is the same as flagging none."""
    patterns = load_patterns()
    for benign in ("property", "staticmethod", "classmethod", "abstractmethod",
                   "functools.lru_cache", "functools.wraps", "dataclass"):
        assert not is_dynamic_dispatch(benign, patterns), benign
    for dynamic in ("event.listens_for", "Depends", "signals.receiver",
                    "dispatch.register"):
        assert is_dynamic_dispatch(dynamic, patterns), dynamic


def test_runtime_attribute_access_sites_are_found(gaps):
    sites = [g for g in gaps if g.kind == "runtime_attribute_access"]
    assert len(sites) == 3
    assert all(g.file == "sample_pkg/dynamic.py" for g in sites)


def test_handlers_reached_only_by_string_are_found(gaps):
    named = {g.detail.split('"')[1] for g in gaps
             if g.kind == "name_in_string_literal"}
    assert named == {"on_created", "on_paid"}


def test_dunder_all_entries_are_not_reported_as_gaps(gaps):
    """Re-exports are a resolvable construct, not a gap. Padding the list with
    non-gaps is how a report stops being read."""
    details = " ".join(g.detail for g in gaps)
    for reexport in ("format_currency", "OrderStatus"):
        assert f'"{reexport}"' not in details


def test_imports_count_as_references(scan):
    """`from .models import Order` names Order as surely as a call does."""
    orphans = {qualname for _, _, qualname in unreferenced_symbols(scan)}
    assert "Order" not in orphans


def test_the_three_untraceable_symbols_are_never_reported_as_unused(scan, gaps):
    """The whole point. Each is unreferenced *and* carries a reason why."""
    orphans = {qualname for _, _, qualname in unreferenced_symbols(scan)}
    assert MAKE_OR_BREAK <= orphans, "these genuinely have no static references"

    explained = {g.symbol for g in gaps if g.kind == "dynamic_dispatch_decorator"}
    explained |= {g.detail.split('"')[1] for g in gaps
                  if g.kind == "name_in_string_literal"}
    assert MAKE_OR_BREAK <= explained, (
        "every symbol with no static callers must carry an explicit reason; "
        "silence without a reason is the CodeGraph failure")


def test_a_dynamic_import_is_a_visible_gap(gaps):
    """importlib.import_module(...) is not an import statement, so the
    resolver's import audit cannot see it. It has to surface here instead,
    or the modules it loads look unreferenced with nothing to explain why."""
    dynamic = [g for g in gaps if g.kind == "dynamic_import"]
    assert len(dynamic) == 1
    assert dynamic[0].file == "sample_pkg/dynamic.py"
    assert "import_module" in dynamic[0].detail
