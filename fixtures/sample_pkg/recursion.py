"""Recursion and mutual recursion. The call graph here is not a DAG.

Any traversal that assumes acyclicity will either loop forever or silently
truncate. countdown calls itself; is_even and is_odd call each other.
"""


def countdown(n: int) -> int:
    """Directly self-recursive."""
    if n <= 0:
        return 0
    return countdown(n - 1)


def is_even(n: int) -> bool:
    """Mutually recursive with is_odd."""
    if n == 0:
        return True
    return is_odd(n - 1)


def is_odd(n: int) -> bool:
    """Mutually recursive with is_even."""
    if n == 0:
        return False
    return is_even(n - 1)
