"""Half of a deliberate circular import pair: a imports b, b imports a.

This cycle is genuine — importing this package at runtime would raise
ImportError. That is intentional. The fixture is a parsing target, never
an import target.
"""

from .b import validate_table

TABLE_PREFIX = "tbl_"


def reserve_table(table_id: str, party_size: int = 2) -> bool:
    """Reserve a table after validating it."""
    if not validate_table(table_id):
        return False
    return True


def describe_table(table_id: str) -> str:
    """Prefix a bare table id for display."""
    return TABLE_PREFIX + table_id
