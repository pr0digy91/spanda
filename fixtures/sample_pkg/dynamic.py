"""Dynamic dispatch: none of the call targets here are statically knowable.

Also carries a conditional import, which is resolvable only at runtime and
must be recorded as conditional rather than silently treated as certain.
"""

from . import handlers

try:
    import ujson as json
except ImportError:
    import json

HANDLER_NAMES = ["on_created", "on_paid"]


def dispatch(event_name: str, payload):
    """Look up a handler by a name assembled at runtime, then call it."""
    fn = getattr(handlers, "on_" + event_name, None)
    if fn is None:
        return None
    return fn(payload)


def call_named(target, method_name: str = "on_paid"):
    """Attribute access through a variable, on an argument of unknown type."""
    if hasattr(target, method_name):
        return getattr(target, method_name)()
    return None


def serialise(payload) -> str:
    """Calls into whichever json module the conditional import bound."""
    return json.dumps(payload)


def load_plugin(name: str):
    """A module loaded by name at runtime. Not an import statement, so the
    import audit cannot see it; must surface as a gap instead."""
    import importlib
    return importlib.import_module(f"sample_pkg.{name}")
