"""Utility helpers with a deliberately narrow __all__.

`_internal_only` is excluded, so `from .helpers import *` in star.py must
not pull it in. A resolver that ignores __all__ will wrongly believe it did.
"""

__all__ = ["format_currency", "slugify"]


def format_currency(amount, currency: str = "INR") -> str:
    """Render an amount for display."""
    return f"{currency} {amount:.2f}"


def slugify(text: str) -> str:
    """Lowercase and hyphenate a string."""
    return text.strip().lower().replace(" ", "-")


def _internal_only(value):
    """Excluded from __all__ and from the star import in star.py."""
    return value
