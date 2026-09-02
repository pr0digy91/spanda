"""Called by a framework, marked by nothing the code names.

A human vetting of the "unreferenced" list found four live symbols in it,
all of these shapes: MCP handlers, an HTTP middleware function, and a
middleware class whose `dispatch` the framework calls by name. That last
one carries no decorator at all — only the override.
"""

from fastapi import FastAPI
from mcp.server import Server
from starlette.middleware.base import BaseHTTPMiddleware

app = FastAPI()
server = Server("sample")


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
