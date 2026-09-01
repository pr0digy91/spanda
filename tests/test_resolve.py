"""M5 gate: linking names to definitions, and refusing to guess.

The fixture's hard cases are here on purpose — a circular import, a base
class in another file, a star import bounded by __all__, mutual recursion.
Each is a way a resolver can quietly produce a wrong answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spanda.extract import plan_scan, stream_records
from spanda.gaps import load_patterns
from spanda.modules import ModuleIndex
from spanda.resolve import (R_BUILTIN, R_EXTERNAL, R_UNKNOWN_TYPE, SymbolTable,
                            build_scope, resolve_record)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="module")
def resolved():
    plan, patterns = plan_scan(FIXTURES), load_patterns()
    index, table, records = ModuleIndex(), SymbolTable(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        table.add_record(record, patterns)
        records.append(record)
    scopes = {r["module"]: build_scope(r, table, index) for r in records}
    references = [ref for r in records for ref in resolve_record(r, table, scopes)]
    return table, references


def edges(references, edge_type=None):
    return {(r.source_symbol, r.target_symbol) for r in references
            if r.target_symbol and (edge_type is None or r.edge_type == edge_type)}


# -- the hard cases --------------------------------------------------------

def test_a_call_across_a_circular_import_resolves(resolved):
    _table, references = resolved
    assert ("sample_pkg.a.reserve_table|function",
            "sample_pkg.b.validate_table|function") in edges(references, "calls")
    assert ("sample_pkg.b.validate_table|function",
            "sample_pkg.a.TABLE_PREFIX|variable") in edges(references)


def test_inheritance_across_files_resolves(resolved):
    _table, references = resolved
    assert ("sample_pkg.derived.UpiPayment|class",
            "sample_pkg.base.PaymentMethod|class") in edges(references, "inherits")


def test_a_method_inherited_from_another_file_resolves(resolved):
    """self.refund() in derived.py reaches a method defined in base.py.
    Getting here requires following the inheritance edge across files."""
    _table, references = resolved
    assert ("sample_pkg.derived.UpiPayment.settle|method",
            "sample_pkg.base.PaymentMethod.refund|method") in edges(references, "calls")


def test_star_imports_resolve_through_dunder_all(resolved):
    _table, references = resolved
    found = edges(references, "calls")
    assert ("sample_pkg.star.build_receipt_line|function",
            "sample_pkg.helpers.slugify|function") in found
    assert ("sample_pkg.star.build_receipt_line|function",
            "sample_pkg.helpers.format_currency|function") in found


def test_star_import_does_not_pull_in_what_dunder_all_excludes(resolved):
    """helpers.__all__ omits _internal_only, so star.py never sees it."""
    _table, references = resolved
    targets = {t for _s, t in edges(references)}
    assert "sample_pkg.helpers._internal_only|function" not in targets


def test_recursion_and_mutual_recursion_resolve(resolved):
    _table, references = resolved
    found = edges(references, "calls")
    assert ("sample_pkg.recursion.countdown|function",
            "sample_pkg.recursion.countdown|function") in found
    assert ("sample_pkg.recursion.is_even|function",
            "sample_pkg.recursion.is_odd|function") in found
    assert ("sample_pkg.recursion.is_odd|function",
            "sample_pkg.recursion.is_even|function") in found


def test_self_dot_method_resolves_within_a_class(resolved):
    _table, references = resolved
    assert ("sample_pkg.models.Order.total|method",
            "sample_pkg.models.Order.subtotal|method") in edges(references)


# -- refusing to guess -----------------------------------------------------

def test_third_party_names_are_external_not_missing(resolved):
    _table, references = resolved
    external = {r.raw for r in references if r.reason == R_EXTERNAL}
    assert "event.listens_for" in external or "Session" in external


def test_builtins_are_labelled_as_such(resolved):
    _table, references = resolved
    builtin = {r.raw for r in references if r.reason == R_BUILTIN}
    assert "sum" in builtin


def test_an_attribute_on_an_unknown_object_is_not_guessed(resolved):
    """`session.execute(...)` where session is an unannotated parameter.
    There is a plausible match to reach for, and reaching for it is exactly
    the failure this project exists to correct."""
    _table, references = resolved
    unknown = {r.raw for r in references if r.reason == R_UNKNOWN_TYPE}
    assert "session.execute" in unknown
    assert "item.price" in unknown
    assert all(r.target_symbol is None
               for r in references if r.reason == R_UNKNOWN_TYPE)


def test_every_reference_is_either_resolved_or_has_a_reason(resolved):
    """The guarantee the whole design rests on: nothing is silently dropped."""
    _table, references = resolved
    for reference in references:
        assert reference.target_symbol or reference.reason, reference


def test_the_runtime_dispatched_hook_has_no_callers_and_is_flagged(resolved):
    """_apply_rls_context genuinely has no static callers. What matters is
    that the index also knows why, so the zero is never read as 'unused'."""
    table, references = resolved
    key = "sample_pkg.models._apply_rls_context|function"
    assert key in table.dynamic
    assert not [r for r in references if r.target_symbol == key]
