"""M1 gate: the extractor's output must match fixtures/README.md exactly.

These assertions are transcribed from the answer key, not from the tool's
output. If the two disagree, one of them is wrong and that is the point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spanda.extract import extract_codebase

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
GOLDEN = ROOT / "tests" / "golden"

# From fixtures/README.md, "Expected definition counts".
EXPECTED_COUNTS = {
    "sample_pkg/__init__.py":  (2, 0, 0, 0, 2),
    "sample_pkg/a.py":         (3, 2, 0, 0, 1),
    "sample_pkg/b.py":         (1, 1, 0, 0, 0),
    "sample_pkg/base.py":      (4, 0, 1, 2, 1),
    "sample_pkg/derived.py":   (4, 0, 1, 2, 1),
    "sample_pkg/dynamic.py":   (4, 3, 0, 0, 1),
    "sample_pkg/handlers.py":  (2, 2, 0, 0, 0),
    "sample_pkg/helpers.py":   (4, 3, 0, 0, 1),
    "sample_pkg/models.py":    (13, 2, 2, 5, 4),
    "sample_pkg/nested.py":    (8, 4, 2, 1, 1),
    "sample_pkg/recursion.py": (3, 3, 0, 0, 0),
    "sample_pkg/star.py":      (1, 1, 0, 0, 0),
}

# From fixtures/README.md, "Decorators — exactly 6 decorated definitions".
EXPECTED_DECORATORS = {
    ("sample_pkg/base.py", "PaymentMethod.charge"): "abstractmethod",
    ("sample_pkg/models.py", "Order.subtotal"): "property",
    ("sample_pkg/models.py", "Order.is_terminal"): "staticmethod",
    ("sample_pkg/models.py", "Order.empty"): "classmethod",
    ("sample_pkg/models.py", "_apply_rls_context"): "event.listens_for",
    ("sample_pkg/nested.py", "expensive_lookup"): "functools.lru_cache",
}


@pytest.fixture(scope="module")
def records() -> dict[str, dict]:
    return {r["file"]: r for r in extract_codebase(FIXTURES)}


def _by_qualname(record: dict) -> dict[str, dict]:
    return {d["qualname"]: d for d in record["definitions"]}


# -- counts ----------------------------------------------------------------

@pytest.mark.parametrize("path,expected", EXPECTED_COUNTS.items())
def test_definition_counts_match_answer_key(records, path, expected):
    kinds = {"function": 0, "class": 0, "method": 0, "variable": 0}
    for definition in records[path]["definitions"]:
        kinds[definition["kind"]] += 1
    total, fn, cls, meth, var = expected
    assert (len(records[path]["definitions"]), kinds["function"], kinds["class"],
            kinds["method"], kinds["variable"]) == (total, fn, cls, meth, var)


def test_totals(records):
    assert len(records) == 13
    assert sum(len(r["definitions"]) for r in records.values()) == 49
    parsed = [r for r in records.values() if r["parse_status"] == "ok"]
    assert len(parsed) == 12


# -- the hard edges --------------------------------------------------------

def test_syntax_error_is_recorded_not_fatal(records):
    broken = records["sample_pkg/broken.py"]
    assert broken["parse_status"] == "syntax_error"
    assert broken["parse_error"]["line"] == 10
    assert broken["definitions"] == []


def test_exactly_six_decorated_definitions(records):
    found = {
        (path, definition["qualname"]): definition["decorators"][0]["base"]
        for path, record in records.items()
        for definition in record["definitions"] if definition["decorators"]
    }
    assert found == EXPECTED_DECORATORS


def test_qualnames_carry_the_parent_chain(records):
    names = _by_qualname(records["sample_pkg/nested.py"])
    assert "Outer.Inner.ping" in names
    assert "make_multiplier.multiply" in names
    assert names["make_multiplier.multiply"]["parent"] == names["make_multiplier"]["local_id"]


def test_every_parameter_kind_survives(records):
    signature = _by_qualname(records["sample_pkg/nested.py"])["fetch_menu"]
    assert signature["is_async"] is True
    assert signature["signature"]["returns"] == "dict"
    kinds = {p["name"]: p["kind"] for p in signature["signature"]["params"]}
    assert kinds == {
        "restaurant_id": "positional_only",
        "sections": "vararg",
        "locale": "keyword_only",
        "options": "kwarg",
    }
    locale = next(p for p in signature["signature"]["params"] if p["name"] == "locale")
    assert locale["default"] == "'en'"


def test_signature_hash_ignores_formatting(records):
    """The shape signal must survive a reformat, or it is noise."""
    from spanda.extract import extract_file
    import tempfile, textwrap

    def hash_of(source: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.py"
            path.write_text(textwrap.dedent(source))
            return extract_file(path, Path(tmp))["definitions"][0]["signature_hash"]

    one_line = hash_of("def f(a: int, b: str = 'x') -> bool: return True\n")
    wrapped = hash_of("""
        def f(
            a: int,
            b: str = 'x',
        ) -> bool:
            return True
    """)
    changed = hash_of("def f(a: int, b: str = 'x', c: int = 0) -> bool: return True\n")
    assert one_line == wrapped, "reformatting must not read as a shape change"
    assert one_line != changed, "an added parameter must read as a shape change"


def test_cross_file_inheritance_base_is_recorded(records):
    derived = _by_qualname(records["sample_pkg/derived.py"])["UpiPayment"]
    assert derived["bases"] == ["PaymentMethod"]


def test_star_import_and_dunder_all(records):
    assert records["sample_pkg/helpers.py"]["dunder_all"] == ["format_currency", "slugify"]
    star = records["sample_pkg/star.py"]["imports"][0]
    assert star["is_star"] is True and star["module"] == ".helpers"


def test_conditional_import_flagged(records):
    imports = {i["raw"]: i["conditional"] for i in records["sample_pkg/dynamic.py"]["imports"]}
    assert imports["from . import handlers"] is False
    assert imports["import ujson as json"] is True
    assert imports["import json"] is True


def test_dynamic_hints_capture_getattr_sites(records):
    hints = records["sample_pkg/dynamic.py"]["dynamic_hints"]
    kinds = [h["kind"] for h in hints]
    assert kinds.count("getattr") == 2
    assert kinds.count("hasattr") == 1
    strings = {h["value"] for h in hints if h["kind"] == "identifier_string"}
    assert {"on_created", "on_paid"} <= strings


def test_references_carry_their_enclosing_definition(records):
    models = records["sample_pkg/models.py"]
    target = _by_qualname(models)["_apply_rls_context"]["local_id"]
    inside = {r["raw"] for r in models["references"] if r["enclosing"] == target}
    assert {"session.execute", "_current_tenant"} <= inside
    assert all(r["enclosing"] is not None or r["line"] < 20
               for r in models["references"])


def test_locals_are_resolved_in_stage_one(records):
    """Parameters and locals must not be handed to Stage 2 as open references,
    or the unresolved count stops meaning anything."""
    recursion = records["sample_pkg/recursion.py"]
    by_root = {(r["root"], r["local"]) for r in recursion["references"]}
    assert ("n", True) in by_root
    assert ("countdown", False) in by_root


# -- golden files ----------------------------------------------------------

def test_output_matches_golden(records):
    for path, record in records.items():
        golden_path = GOLDEN / (path + ".json")
        assert golden_path.exists(), f"missing golden file for {path}"
        assert record == json.loads(golden_path.read_text()), (
            f"{path} differs from its frozen output. If the change is intended, "
            f"regenerate with: python -m spanda.cli parse fixtures --out tests/golden")


def test_skipped_files_are_reported_not_silent():
    """A file the tool declined to read is a gap in what it knows. It must be
    counted and attributed, never dropped quietly."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "real.py").write_text("def kept(): pass\n")
        for hidden in ("node_modules", ".venv", "__pycache__"):
            (root / hidden).mkdir()
            (root / hidden / "ignored.py").write_text("def dropped(): pass\n")
        scan = extract_codebase(root)
        assert len(scan.records) == 1
        assert scan.skipped_count == 3
        assert set(scan.skipped) == {"node_modules", ".venv", "__pycache__"}


# -- what counts as a shape change -----------------------------------------

def _signature_hash(source: str, name: str = "total") -> tuple[str, str]:
    from spanda.extract import extract_file
    import tempfile, textwrap
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.py"
        path.write_text(textwrap.dedent(source))
        definition = next(d for d in extract_file(path, Path(tmp))["definitions"]
                          if d["name"] == name)
        return definition["signature_hash"], definition["canonical_signature"]


PLAIN = "def total(items, tax):\n    return 1\n"

SHAPE_CHANGES = {
    "parameter renamed":       "def total(lines, tax):\n    return 1\n",
    "parameter added":         "def total(items, tax, discount):\n    return 1\n",
    "parameter removed":       "def total(items):\n    return 1\n",
    "parameters reordered":    "def total(tax, items):\n    return 1\n",
    "default added":           "def total(items, tax=1.05):\n    return 1\n",
    "default changed":         "def total(items, tax=1.18):\n    return 1\n",
    "annotation added":        "def total(items: list, tax: float):\n    return 1\n",
    "return annotation added": "def total(items, tax) -> float:\n    return 1\n",
    "made keyword-only":       "def total(items, *, tax):\n    return 1\n",
    # Callers now receive a coroutine and must await it. Blunter than any
    # parameter change, and previously misclassified as internal.
    "made async":              "async def total(items, tax):\n    return 1\n",
}

INTERNAL_CHANGES = {
    "body rewritten":     "def total(items, tax):\n    return 2\n",
    "docstring added":    'def total(items, tax):\n    """Doc."""\n    return 1\n',
    "comment added":      "def total(items, tax):\n    # note\n    return 1\n",
    "reformatted":        "def total(\n    items,\n    tax,\n):\n    return 1\n",
    "unrelated decorator": "import functools\n@functools.lru_cache(maxsize=128)\ndef total(items, tax):\n    return 1\n",
}


@pytest.mark.parametrize("label,source", SHAPE_CHANGES.items())
def test_caller_visible_changes_move_the_signature_hash(label, source):
    assert _signature_hash(source)[0] != _signature_hash(PLAIN)[0], label


@pytest.mark.parametrize("label,source", INTERNAL_CHANGES.items())
def test_internal_changes_leave_the_signature_hash_alone(label, source):
    assert _signature_hash(source)[0] == _signature_hash(PLAIN)[0], label


def test_property_is_part_of_a_methods_shape():
    """@property decides whether callers write obj.total or obj.total()."""
    plain = "class C:\n    def total(self):\n        return 1\n"
    prop = "class C:\n    @property\n    def total(self):\n        return 1\n"
    assert _signature_hash(plain)[0] != _signature_hash(prop)[0]


def test_tuning_an_unrelated_decorator_is_not_a_shape_change():
    """Otherwise bumping a cache size reports as a breaking change."""
    small = "import functools\n@functools.lru_cache(maxsize=128)\ndef total(a):\n    return 1\n"
    large = "import functools\n@functools.lru_cache(maxsize=256)\ndef total(a):\n    return 1\n"
    assert _signature_hash(small)[0] == _signature_hash(large)[0]


def test_moving_a_function_changes_neither_hash():
    """Line numbers are not part of identity or of either hash."""
    moved = "\n\n\n" + PLAIN
    from spanda.extract import extract_file
    import tempfile
    def both(source):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.py"
            path.write_text(source)
            d = extract_file(path, Path(tmp))["definitions"][0]
            return d["signature_hash"], d["content_hash"]
    assert both(PLAIN) == both(moved)
