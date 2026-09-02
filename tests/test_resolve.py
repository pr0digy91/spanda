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
                            build_scopes, resolve_record)

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
    scopes, _lost = build_scopes(records, table, index)
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


def test_a_call_through_a_function_local_import_resolves(resolved):
    """The audit already covered the import; this covers the call."""
    _table, references = resolved
    assert ("sample_pkg.lazy.run_later|function",
            "sample_pkg.helpers.slugify|function") in edges(references, "calls")


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


# -- re-export chains ------------------------------------------------------

def test_a_name_re_exported_twice_still_resolves(resolved):
    """`register_node` is defined in registry/impl.py, re-exported by
    registry/__init__.py, re-exported again by the package root, and used in
    consumer.py. Four files, three hops.

    A resolver that looks only for names a module *defines*, never for names
    it imported, loses the trail at the first __init__.py. On a real codebase
    that made 28 of 31 live handlers report as having no callers — and
    nothing complained, because the tool was not unsure, it simply never saw
    the reference.
    """
    _table, references = resolved
    found = edges(references)
    assert ("sample_pkg.consumer.NODES|variable",
            "sample_pkg.registry.impl.register_node|function") in found or \
           any(t == "sample_pkg.registry.impl.register_node|function"
               and s is None for s, t in found), \
        "the three-hop re-export did not resolve"


def test_the_re_exported_symbol_has_a_caller(resolved):
    """Stated the way the report states it: something references it."""
    _table, references = resolved
    targets = {r.target_symbol for r in references if r.target_symbol}
    assert "sample_pkg.registry.impl.register_node|function" in targets


def test_names_re_exported_through_the_package_root_resolve(resolved):
    """consumer.py imports Order and format_currency from the package root,
    which defines neither."""
    _table, references = resolved
    from_consumer = {(r.raw, r.target_symbol) for r in references
                     if r.source_file == "sample_pkg/consumer.py" and r.target_symbol}
    assert ("format_currency", "sample_pkg.helpers.format_currency|function") in from_consumer
    assert ("Order.empty", "sample_pkg.models.Order.empty|method") in from_consumer


def test_a_submodule_import_is_still_a_module_not_a_re_export(resolved):
    """`from . import handlers` must keep resolving to the submodule. The fix
    for re-exports has to not break this: both look like 'the target module
    does not define this name'."""
    _table, references = resolved
    reasons = {r.raw: r.reason for r in references
               if r.source_file == "sample_pkg/dynamic.py"}
    assert reasons.get("handlers") == "module_reference"


def test_a_closure_used_inside_its_parent_resolves(resolved):
    """`make_multiplier` returns `multiply`, and returning it is a reference.

    A nested function is a local name, so Stage 1 marks it resolved-locally
    and a naive Stage 2 skips it — leaving closures passed to `re.sub` as
    callbacks reported as having no callers at all.
    """
    _table, references = resolved
    assert ("sample_pkg.nested.make_multiplier|function",
            "sample_pkg.nested.make_multiplier.multiply|function") in edges(references)


# -- annotations -----------------------------------------------------------

def _from_checkout(references, raw):
    return [r for r in references
            if r.source_file == "sample_pkg/checkout.py" and r.raw == raw]


def test_an_annotated_parameter_resolves_its_attribute_access(resolved):
    """`method: PaymentMethod` then `method.charge(...)`. The code says what
    method is; ignoring that leaves 85% of such references unknowable."""
    _table, references = resolved
    [ref] = _from_checkout(references, "method.charge")[:1] or [None]
    assert ref and ref.target_symbol == "sample_pkg.base.PaymentMethod.charge|method"


def test_optional_is_unwrapped(resolved):
    _table, references = resolved
    refs = _from_checkout(references, "method.refund")
    assert any(r.target_symbol == "sample_pkg.base.PaymentMethod.refund|method"
               for r in refs)


def test_a_quoted_forward_reference_is_still_a_name(resolved):
    _table, references = resolved
    refs = _from_checkout(references, "method.provider_name")
    assert any(r.target_symbol == "sample_pkg.base.PaymentMethod.provider_name|variable"
               for r in refs)


def test_an_unannotated_parameter_stays_honestly_unknown(resolved):
    """cannot_know(method, amount) has no annotation. There is a plausible
    class named PaymentMethod in scope, and reaching for it is the guess
    this project exists to refuse."""
    _table, references = resolved
    refs = _from_checkout(references, "method.charge")
    unknown = [r for r in refs if r.target_symbol is None]
    assert unknown and all(r.reason == R_UNKNOWN_TYPE for r in unknown)


def test_a_generic_annotation_is_not_guessed_at():
    from spanda.resolve import _annotation_name
    assert _annotation_name("PaymentMethod") == "PaymentMethod"
    assert _annotation_name("'PaymentMethod'") == "PaymentMethod"
    assert _annotation_name("Optional[PaymentMethod]") == "PaymentMethod"
    assert _annotation_name("PaymentMethod | None") == "PaymentMethod"
    assert _annotation_name("list[PaymentMethod]") is None
    assert _annotation_name("Dict[str, Any]") is None
    assert _annotation_name("int | str") is None


# -- the self-audit --------------------------------------------------------

def test_a_clean_fixture_loses_no_trails(resolved):
    """Zero is the expected reading. The fixture imports through re-export
    chains three deep and every one must be traced."""
    from spanda.extract import plan_scan, stream_records
    from spanda.modules import ModuleIndex
    plan, patterns = plan_scan(FIXTURES), load_patterns()
    index, table, records = ModuleIndex(), SymbolTable(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        table.add_record(record, patterns)
        records.append(record)
    _scopes, lost = build_scopes(records, table, index)
    assert lost == []


def test_an_import_the_resolver_cannot_place_is_counted_not_dropped(tmp_path):
    """`from pkg import thing` where pkg neither defines nor imports `thing`.

    Nothing downstream would ever flag this: no reference is produced, so no
    reason code is attached. The audit is the only thing that says it.
    """
    from spanda.extract import plan_scan, stream_records
    from spanda.modules import ModuleIndex
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("VERSION = 1\n")
    (tmp_path / "user.py").write_text(
        "from pkg import thing\n\n\ndef go():\n    return thing()\n")
    plan, patterns = plan_scan(tmp_path), load_patterns()
    index, table, records = ModuleIndex(), SymbolTable(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        table.add_record(record, patterns)
        records.append(record)
    _scopes, lost = build_scopes(records, table, index)
    assert len(lost) == 1
    assert lost[0].name == "thing"
    assert lost[0].source_file == "user.py"
    assert lost[0].target_module == "pkg"


def test_a_re_exported_external_name_is_not_a_lost_trail(tmp_path):
    """orm/base.py does `from sqlalchemy import UUID as PGUUID`; every model
    then does `from .base import PGUUID`. The trail is not lost — it leads
    outside the codebase, and the audit must say external, not missing."""
    from spanda.extract import plan_scan, stream_records
    from spanda.modules import ModuleIndex
    (tmp_path / "orm").mkdir()
    (tmp_path / "orm" / "__init__.py").write_text("")
    (tmp_path / "orm" / "base.py").write_text(
        "from sqlalchemy.dialects.postgresql import UUID as PGUUID\n")
    (tmp_path / "orm" / "user.py").write_text(
        "from .base import PGUUID\n\nclass User:\n    id = PGUUID\n")
    plan, patterns = plan_scan(tmp_path), load_patterns()
    index, table, records = ModuleIndex(), SymbolTable(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        table.add_record(record, patterns)
        records.append(record)
    scopes, lost = build_scopes(records, table, index)
    assert lost == []
    assert scopes["orm/user.py"]["PGUUID"].kind == "external"  # keyed by file


def test_the_audit_does_not_share_the_scope_builders_blind_spots(monkeypatch):
    """Reintroduce the original re-export bug and the audit must fire.

    The first audit read the scope builder's own unfinished-work list and
    inherited its blindness — the buggy branch skipped that list, so the
    audit stayed silent on the exact failure it was built for.
    """
    import spanda.resolve as resolve_module
    from spanda.extract import plan_scan, stream_records
    from spanda.modules import ModuleIndex

    original = resolve_module.build_scope

    def buggy_build_scope(record, table, index):
        scope, pending = original(record, table, index)
        # The bug: names imported from a package that only re-exports them
        # are left as module targets and never chased.
        for kind, local, target_module, _name, _edge in list(pending):
            if kind == "name":
                scope[local] = resolve_module.Target("module", module=target_module)
        return scope, [p for p in pending if p[0] != "name"]

    monkeypatch.setattr(resolve_module, "build_scope", buggy_build_scope)
    plan, patterns = plan_scan(FIXTURES), load_patterns()
    index, table, records = ModuleIndex(), SymbolTable(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        table.add_record(record, patterns)
        records.append(record)
    _scopes, lost = resolve_module.build_scopes(records, table, index)
    assert {t.name for t in lost} >= {"register_node", "Order", "format_currency"}


def test_files_sharing_a_module_name_get_their_own_scopes(tmp_path):
    """Two conftest.py files in test directories without __init__.py both have
    the module name "conftest". Keyed by name, one scope overwrites the
    other and both files resolve against whichever was built last."""
    from spanda.extract import plan_scan, stream_records
    from spanda.modules import ModuleIndex
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "__init__.py").write_text("")
    (tmp_path / "lib" / "alpha.py").write_text("def a():\n    return 1\n")
    (tmp_path / "lib" / "beta.py").write_text("def b():\n    return 2\n")
    for sub, mod, fn in (("t1", "alpha", "a"), ("t2", "beta", "b")):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "conftest.py").write_text(
            f"from lib.{mod} import {fn}\n\n\ndef use():\n    return {fn}()\n")
    plan, patterns = plan_scan(tmp_path), load_patterns()
    index, table, records = ModuleIndex(), SymbolTable(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        table.add_record(record, patterns)
        records.append(record)
    assert [r["module"] for r in records if r["file"].endswith("conftest.py")] \
        == ["conftest", "conftest"]
    scopes, lost = build_scopes(records, table, index)
    assert lost == []
    calls = {(r.source_file, r.target_symbol) for r in
             (ref for rec in records for ref in resolve_record(rec, table, scopes))
             if r.edge_type == "calls" and r.target_symbol}
    assert ("t1/conftest.py", "lib.alpha.a|function") in calls
    assert ("t2/conftest.py", "lib.beta.b|function") in calls


def test_a_function_local_import_is_audited_too(tmp_path):
    """Imports written inside a function body are recorded like any other
    and must be checked. A zero that only covered top-of-file imports would
    be a zero that sounds bigger than it is."""
    from spanda.extract import plan_scan, stream_records
    from spanda.modules import ModuleIndex
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "user.py").write_text(
        "def go():\n    from pkg import thing\n    return thing()\n")
    plan, patterns = plan_scan(tmp_path), load_patterns()
    index, table, records = ModuleIndex(), SymbolTable(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        table.add_record(record, patterns)
        records.append(record)
    _scopes, lost = build_scopes(records, table, index)
    assert [t.name for t in lost] == ["thing"]


def test_same_named_modules_do_not_share_definitions(tmp_path):
    """Two conftest.py files each define client(). A bare call to client()
    in one must resolve to its own, not to whichever was indexed last.

    The scope-by-file fix covered imports. This is the other half: the symbol
    table's per-module dict merges same-named files, and seeding a scope from
    it produces a *wrong* edge that no audit sees, because no import is
    involved.
    """
    from spanda.extract import plan_scan, stream_records
    from spanda.modules import ModuleIndex
    for sub in ("t1", "t2"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "conftest.py").write_text(
            f"def client():\n    return '{sub}'\n\n\ndef use():\n    return client()\n")
    plan, patterns = plan_scan(tmp_path), load_patterns()
    index, table, records = ModuleIndex(), SymbolTable(), []
    for record in stream_records(plan):
        index.add(record["file"], record["module"])
        table.add_record(record, patterns)
        records.append(record)
    scopes, _lost = build_scopes(records, table, index)
    for record in records:
        calls = [r.target_symbol for r in resolve_record(record, table, scopes)
                 if r.edge_type == "calls" and r.target_symbol]
        own = record["file"].split("/")[0]
        assert calls == [f"{own}.conftest.client|function"], record["file"]


def test_an_assignment_target_is_not_a_possible_caller(resolved):
    """`self.order_id = order_id` writes an attribute. It can never be a call,
    so it must not be stored as a place that might be calling a symbol named
    order_id. Its own reason, so it is counted rather than dropped."""
    from spanda.resolve import R_ASSIGNMENT
    from spanda.store import KEEPABLE_REASONS
    _table, references = resolved
    # Only the writes in __init__ (lines 32-35); `self.items` is also *read*
    # later in the file, and a read is legitimately an unknown-type reference.
    stores = [r for r in references
              if r.raw in ("self.order_id", "self.items", "self.status")
              and r.source_file == "sample_pkg/models.py" and r.line <= 35]
    assert stores, "the fixture's __init__ writes these"
    assert all(r.reason == R_ASSIGNMENT for r in stores)
    assert R_ASSIGNMENT not in KEEPABLE_REASONS



# -- three labelling faults a reviewer found in the live index ---------------

def _scoping(resolved):
    _table, references = resolved
    return [r for r in references if r.source_file == "sample_pkg/scoping.py"]


def test_a_member_of_a_class_with_an_external_base_is_maybe_inherited(resolved):
    """`a.model_dump()` on a Pydantic model: the class is known, its base is
    not, so the member is unknown — not absent. 112 of 112 were mislabelled."""
    (ref,) = [r for r in _scoping(resolved) if r.raw == "a.model_dump"]
    assert ref.reason == "attribute_maybe_inherited"
    assert not any(r.reason == "no_such_attribute" for r in _scoping(resolved))


def test_names_the_language_binds_are_not_reported_as_unfound(resolved):
    unfound = [r.raw for r in _scoping(resolved) if r.reason == "not_found"]
    assert unfound == []
    (ref,) = [r for r in _scoping(resolved) if r.raw == "__file__"]
    assert ref.reason == "builtin"


def test_a_class_body_reference_resolves_to_the_attribute(resolved):
    _table, references = resolved
    assert ("sample_pkg.scoping.Ledger|class",
            "sample_pkg.scoping.Ledger.rate|variable") in edges(references, "uses")


def test_a_module_alias_reaches_its_definitions_and_re_exports(resolved):
    _table, references = resolved
    assert ("sample_pkg.scoping.via_alias|function",
            "sample_pkg.helpers.slugify|function") in edges(references, "calls")
    assert ("sample_pkg.scoping.via_alias|function",
            "sample_pkg.models.Order|class") in edges(references, "uses"), \
        "Order is re-exported by the package root, not defined there"


def test_reading_a_member_inside_a_call_is_a_use_not_a_call(resolved):
    _table, references = resolved
    key = ("sample_pkg.scoping.filtered|function", "sample_pkg.models.Order.total|method")
    assert key in edges(references, "uses")
    assert key not in edges(references, "calls")



def test_an_attribute_set_on_self_is_an_instance_attribute_not_absent(resolved):
    reads = [r for r in _scoping(resolved)
             if r.raw == "self.seconds" and r.reason != "assignment_target"]
    assert [r.reason for r in reads] == ["instance_attribute"]


def test_a_call_further_along_the_chain_is_not_a_call_on_the_member(resolved):
    """`Order.total.__doc__.strip()` reads total and calls strip."""
    _table, references = resolved
    key = ("sample_pkg.scoping.described|function", "sample_pkg.models.Order.total|method")
    assert key in edges(references, "uses") and key not in edges(references, "calls")


def test_a_module_level_loop_target_resolves_as_a_module_name(resolved):
    _table, references = resolved
    # The read sits in module-level code, so its source is the module.
    assert (None, "sample_pkg.scoping._spec|variable") in edges(references, "uses")


def test_a_chain_through_a_submodule_reaches_its_member(resolved):
    _table, references = resolved
    assert ("sample_pkg.scoping.deep|function",
            "sample_pkg.helpers.slugify|function") in edges(references, "calls")



def test_dunder_attributes_every_class_and_module_has_are_builtins(resolved):
    reasons = {r.raw: r.reason for r in _scoping(resolved)
               if r.raw in ("Order.__name__", "h.__file__")}
    assert reasons == {"Order.__name__": "builtin", "h.__file__": "builtin"}
