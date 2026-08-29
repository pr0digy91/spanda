"""Cross-file inheritance: the base class lives in another module.

`settle` calls `self.refund`, which is defined in base.py. Resolving that
call requires following the inheritance edge across the file boundary.
"""

from .base import PaymentMethod


class UpiPayment(PaymentMethod):
    """UPI payment. Overrides charge, inherits refund."""

    provider_name = "upi"

    def charge(self, amount, *, idempotency_key: str | None = None) -> bool:
        """Concrete implementation of the abstract base method."""
        return amount > 0

    def settle(self) -> None:
        """Calls a method defined only on the cross-file base class."""
        self.refund(0)
