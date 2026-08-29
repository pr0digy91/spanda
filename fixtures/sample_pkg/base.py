"""Base class for the cross-file inheritance case."""

from abc import ABC, abstractmethod


class PaymentMethod(ABC):
    """Abstract payment method."""

    provider_name = "unknown"

    @abstractmethod
    def charge(self, amount, *, idempotency_key: str | None = None) -> bool:
        """Charge the given amount."""

    def refund(self, amount) -> bool:
        """Default refund behaviour. Subclasses may inherit this unchanged."""
        return False
