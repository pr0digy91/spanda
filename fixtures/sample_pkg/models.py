"""Domain models, plus the exact failure case this whole project exists for.

`_apply_rls_context` at the bottom is invoked by SQLAlchemy on every flush.
Nothing in this codebase references it by name. A call graph that reports
"0 callers" for it is not merely incomplete — it is actively misleading,
because "0 callers" reads as "safe to change".

The sqlalchemy imports are unresolvable on purpose: the library is not
installed, so a correct resolver marks them external rather than guessing.
"""

from decimal import Decimal
from enum import Enum

from sqlalchemy import event
from sqlalchemy.orm import Session


class OrderStatus(Enum):
    """Lifecycle states for an order."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class Order:
    """A single restaurant order."""

    tax_rate = Decimal("0.05")

    def __init__(self, order_id: str, items: list | None = None) -> None:
        self.order_id = order_id
        self.items = items or []
        self.status = OrderStatus.PENDING

    @property
    def subtotal(self) -> Decimal:
        """Sum of line items before tax."""
        return sum((item.price for item in self.items), Decimal("0"))

    def total(self, *, currency: str = "INR") -> Decimal:
        """Order total including tax."""
        return self.subtotal * (1 + self.tax_rate)

    @staticmethod
    def is_terminal(status: OrderStatus) -> bool:
        """Whether an order in this state can still change."""
        return status in (OrderStatus.CONFIRMED, OrderStatus.CANCELLED)

    @classmethod
    def empty(cls, order_id: str) -> "Order":
        """Construct an order with no line items."""
        return cls(order_id, [])


@event.listens_for(Session, "before_flush")
def _apply_rls_context(session, flush_context, instances) -> None:
    """Apply row-level security context before every flush.

    Called by SQLAlchemy, never by name from this codebase.
    """
    session.execute("SET LOCAL app.tenant_id = :tid", {"tid": _current_tenant()})


def _current_tenant() -> str:
    """Reached only from _apply_rls_context, which itself has no static callers."""
    return "tenant-default"
