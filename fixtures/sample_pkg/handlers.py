"""Handlers reached only through getattr in dynamic.py.

Neither function is referenced by name anywhere in this package. Both are
called at runtime. This is the second shape of the same trap as
models._apply_rls_context, arriving via getattr instead of a decorator.
"""


def on_created(payload):
    """Invoked as getattr(handlers, "on_created")."""
    return {"created": payload}


def on_paid(payload):
    """Invoked as getattr(handlers, "on_paid")."""
    return {"paid": payload}
