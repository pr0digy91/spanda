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
    assert orphans >= MAKE_OR_BREAK, "these genuinely have no static references"

    explained = {g.symbol for g in gaps if g.kind == "dynamic_dispatch_decorator"}
    explained |= {g.detail.split('"')[1] for g in gaps
                  if g.kind == "name_in_string_literal"}
    assert explained >= MAKE_OR_BREAK, (
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


def _alembic_tree(tmp_path):
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    (tmp_path / "alembic" / "env.py").write_text(
        "from alembic import context\n\n\ndef run():\n    context.run_migrations()\n")
    (versions / "0001_first.py").write_text(
        "from alembic import op\n\n\ndef upgrade():\n    op.add_column('t', None)\n\n\n"
        "def downgrade():\n    op.drop_column('t', 'c')\n\n\ndef helper():\n    pass\n")
    (versions / "0002_multidb.py").write_text(
        "def upgrade_engine1():\n    pass\n\n\ndef downgrade_engine1():\n    pass\n")
    (tmp_path / "seeds.py").write_text("def upgrade():\n    pass\n")


def test_alembic_migrations_are_framework_called_by_file_and_name(tmp_path):
    """Alembic runs upgrade() and downgrade() in every file under versions/
    itself. Nothing at the definition says so, and nothing in the codebase
    names them, so on a migrations repository 844 of 845 candidates were
    these — a list long enough that a reader stops reading. The `file:`
    pattern kind exists for this shape."""
    from spanda.gaps import framework_convention
    _alembic_tree(tmp_path)
    scan = extract_codebase(tmp_path)
    patterns = load_patterns()
    by_symbol = {(g.file, g.symbol): g for g in find_gaps(scan, patterns)
                 if g.kind == "framework_convention"}
    assert set(by_symbol) == {
        ("alembic/versions/0001_first.py", "upgrade"),
        ("alembic/versions/0001_first.py", "downgrade"),
        ("alembic/versions/0002_multidb.py", "upgrade_engine1"),
        ("alembic/versions/0002_multidb.py", "downgrade_engine1"),
    }, "helper() in a migration and upgrade() outside versions/ are ordinary functions"
    assert by_symbol[("alembic/versions/0001_first.py", "upgrade")].detail \
        == "matches *versions/*.py::upgrade*"
    # The match is on the written path and on module level, never on a method.
    definition = {"kind": "method", "parent": 1, "name": "upgrade"}
    assert framework_convention("alembic/versions/x.py", definition, patterns) is None
    assert framework_convention(None, {"kind": "function", "parent": None,
                                       "name": "upgrade"}, patterns) is None


def test_a_file_pattern_without_a_separator_matches_nothing():
    """A malformed line must not become a wildcard that flags every function."""
    from spanda.gaps import framework_convention
    definition = {"kind": "function", "parent": None, "name": "upgrade"}
    assert framework_convention("alembic/versions/x.py", definition,
                                ["file:*versions/*.py"]) is None


def test_task_queue_decorators_are_dispatch():
    """`@app.task` is Celery and procrastinate; the worker calls what it
    decorates. Two export tasks reported as a decorator spanda did not know."""
    patterns = load_patterns()
    for base in ("app.task", "celery.shared_task", "shared_task",
                 "app.periodic", "dramatiq.actor"):
        assert is_dynamic_dispatch(base, patterns), base
    assert not is_dynamic_dispatch("scheduler.scheduled_job", patterns), \
        "still unknown, still reported as such"


def test_a_codebase_extends_the_built_in_patterns_from_its_own_file(tmp_path):
    """`.spanda/dynamic_dispatch.txt` is read after the built-in list on
    every run, so a framework the tool has never heard of is a one-line fix
    in the repository rather than an edit inside an installed package.
    `--patterns` still replaces the list outright."""
    from spanda.gaps import local_patterns_path
    from spanda.store import ensure_index_dir
    ensure_index_dir(tmp_path)
    local = local_patterns_path(tmp_path)
    assert local.exists(), "spanda index writes the file so a message can name it"
    assert load_patterns(root=tmp_path) == load_patterns(), \
        "the stub is comments only: it documents the format and adds nothing"
    local.write_text(local.read_text() + "*.every\n")
    with_local = load_patterns(root=tmp_path)
    assert is_dynamic_dispatch("scheduler.every", with_local)
    assert with_local[:-1] == load_patterns()
    override = tmp_path / "only.txt"
    override.write_text("*.only\n")
    assert load_patterns(override, root=tmp_path) == ["*.only"]
    # Written once; a person's edits are theirs.
    ensure_index_dir(tmp_path)
    assert local.read_text().endswith("*.every\n")
