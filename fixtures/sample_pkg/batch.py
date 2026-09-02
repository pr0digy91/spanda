"""Loops: nested in one body, nested across a call, and a database call
inside one.

The index records where loops are and what runs inside them. It never says
how anything scales — `pair_all_groups` is three loops deep across a call,
which is a place to look, not a complexity.
"""

from .helpers import slugify


def normalise_all(rows: list[str]) -> list[str]:
    """One loop, calling a function that has none."""
    out = []
    for row in rows:
        out.append(slugify(row))
    return out


def pair_up(rows: list[str]) -> list[tuple[str, str]]:
    """Two loops in one body."""
    pairs = []
    for left in rows:
        for right in rows:
            if left != right:
                pairs.append((left, right))
    return pairs


def pair_all_groups(groups: list[list[str]]) -> int:
    """One loop that calls a two-loop function: three deep across the call."""
    total = 0
    for group in groups:
        total += len(pair_up(group))
    return total


def load_each(session, ids: list[int]) -> list:
    """A database call inside a loop — the shape that makes one request
    into a thousand queries. The session never resolves; its name is all
    the index has."""
    found = []
    for identifier in ids:
        found.append(session.get(identifier))
    return found


SQUARES = [n * n for n in range(10)]
