"""The real definition, three hops from where it is used."""


def register_node(name: str, handler) -> dict:
    """Defined here, re-exported twice, called from a fourth file."""
    return {"name": name, "handler": handler}
