"""Nesting, closures, async, and every parameter kind in one place.

Exists to check that qualnames and signatures survive the awkward cases:
a function defined inside a function, a class inside a class, and a
signature using positional-only, varargs, keyword-only and **kwargs.
"""

import functools


def make_multiplier(factor: int):
    """Returns a closure. `multiply` is a nested definition, not module-level."""

    def multiply(value: int) -> int:
        return value * factor

    return multiply


@functools.lru_cache(maxsize=128)
def expensive_lookup(key: str) -> str:
    """Call-form decorator from an imported module. Not dynamic dispatch."""
    return key.upper()


async def fetch_menu(
    restaurant_id: str, /, *sections: str, locale: str = "en", **options
) -> dict:
    """Async, positional-only, varargs, keyword-only and kwargs in one signature."""
    return {"id": restaurant_id, "sections": sections, "locale": locale, **options}


class Outer:
    """Contains a nested class."""

    class Inner:
        """Nested class. Its qualname must come out as Outer.Inner."""

        depth = 2

        def ping(self) -> str:
            """Qualname must be Outer.Inner.ping."""
            return "pong"
