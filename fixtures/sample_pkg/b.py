"""Half of a deliberate circular import pair: b imports a, a imports b."""

from .a import TABLE_PREFIX


def validate_table(table_id: str) -> bool:
    """Check the table id carries the expected prefix."""
    return table_id.startswith(TABLE_PREFIX)
