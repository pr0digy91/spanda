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
    assert flagged == {"_apply_rls_context"}


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
