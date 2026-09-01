"""Sample package root.

Exists to test re-export resolution: names below are importable as
`sample_pkg.Order` even though they are *defined* in submodules. A resolver
that only looks at where a name is imported will point here; a correct one
points at the real definition in models.py / helpers.py.
"""

from .models import Order, OrderStatus
from .helpers import format_currency
from .registry import register_node
from . import a

__all__ = ["Order", "OrderStatus", "format_currency", "register_node"]

VERSION = "0.1.0"
