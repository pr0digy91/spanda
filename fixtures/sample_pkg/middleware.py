"""Called by a framework, marked by nothing the code names.

A human vetting of the "unreferenced" list found four live symbols in it,
all of these shapes: MCP handlers, an HTTP middleware function, and a
middleware class whose `dispatch` the framework calls by name. That last
one carries no decorator at all — only the override.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from mcp.server import Server
from pydantic import BaseModel, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

app = FastAPI()
server = Server("sample")
scheduler = AsyncIOScheduler()


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    return response


@server.list_tools()
async def list_tools():
    return []


class RequestLogger(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        return await call_next(request)


@scheduler.scheduled_job("cron", hour=3)
async def nightly_cleanup():
    """A decorator on neither list. Not dead, not known: unrecognised."""
    return None


class Auditor(BaseModel):
    """An external base with no pattern line. A public method nothing names
    is a candidate the framework may call; a private one is the class's own."""

    def on_validate(self):
        return True

    def _helper(self):
        return False

    @field_validator("name")
    def name_present(cls, value):
        """Explained by its decorator; not a second, vaguer finding."""
        return value
