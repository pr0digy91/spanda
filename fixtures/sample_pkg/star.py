"""Star import: the names used below have no explicit import statement.

Resolving `slugify` and `format_currency` here requires expanding
`from .helpers import *` through helpers.__all__.
"""

from .helpers import *


def build_receipt_line(item_name: str, price) -> str:
    """Uses two names that exist here only via the star import."""
    return f"{slugify(item_name)}: {format_currency(price)}"
