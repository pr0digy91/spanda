"""Uses names the package root only re-exports, never defines.

This is the shape that made 28 of 31 real handlers look uncalled: a name
defined in one file, re-exported by its package, re-exported again by the
package above it, and used from a fourth. Every hop is somewhere the trail
can be lost, and losing it reports a busy function as dead.
"""

from sample_pkg import Order, format_currency, register_node

NODES = register_node("summary", None)


def summarise(order_id: str) -> str:
    """Calls two names that arrive here through re-export chains."""
    return format_currency(Order.empty(order_id).total())
