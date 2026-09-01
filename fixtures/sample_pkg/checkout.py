"""Attribute access on a parameter whose type the signature declares.

`method` is unannotated in the wild often enough, but here the code says what
it is. That declaration is the only thing standing between `method.charge()`
and "attribute on unknown type" — and it points at a class in another file,
whose `charge` is abstract and whose `refund` is inherited.
"""

from typing import Optional

from .base import PaymentMethod


def take_payment(method: PaymentMethod, amount) -> bool:
    """Resolves through the annotation to the class in base.py."""
    return method.charge(amount, idempotency_key=None)


def maybe_refund(method: Optional[PaymentMethod], amount) -> bool:
    """Optional[...] is unwrapped; the call still resolves."""
    return method.refund(amount) if method else False


def forward_ref(method: "PaymentMethod") -> str:
    """A quoted forward reference is still a name."""
    return method.provider_name


def cannot_know(method, amount) -> bool:
    """No annotation, so this stays honestly unknown."""
    return method.charge(amount)
