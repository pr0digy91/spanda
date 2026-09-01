"""First hop: re-exports a name it does not define."""

from .impl import register_node

__all__ = ["register_node"]
