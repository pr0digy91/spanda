"""An import written inside a function body, then called through.

Two real functions — a flow executor and a nightly job — were imported this
way in the codebase this tool was built against, and both reported zero
callers. The import was traced, so the audit passed; the call through it
was then discarded as a reference to a local name.
"""


def run_later(text: str) -> str:
    """Imports lazily, the way code does to sidestep an import cycle."""
    from .helpers import slugify
    return slugify(text)
