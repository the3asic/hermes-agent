"""Exercise Hermes MCP HTTP transports against the real SDK and repository data."""

import asyncio
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from mcp.server import MCPServer
from mcp.types import TextContent
from starlette.middleware.gzip import GZipMiddleware


@asynccontextmanager
async def repository_server(transport):
    readme = Path(__file__).resolve().parents[3] / "README.md"
    sdk = MCPServer("Hermes dependency compatibility")

    @sdk.tool(structured_output=False)
    def read_repository_readme() -> str:
        """Read the actual README of this maintained Hermes checkout."""
        return readme.read_text(encoding="utf-8")

    if transport == "sse":
        app = sdk.sse_app()
        endpoint = "/sse"
    else:
        app = sdk.streamable_http_app(json_response=True, stateless_http=True)
        endpoint = "/mcp"
    app.add_middleware(GZipMiddleware, minimum_size=1)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    running = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if running.done():
                    running.result()
                    raise RuntimeError("The real MCP server stopped before becoming ready")
                await asyncio.sleep(0.02)
        yield f"http://127.0.0.1:{port}{endpoint}", readme
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(running, timeout=15)
        finally:
            listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ("sse", "streamable-http"))
async def test_mcp_http_transport_reads_repository_document(tmp_path, transport):
    home = tmp_path / "home"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        from tools.mcp_tool_discovery import _connect_server

        async with repository_server(transport) as (url, readme):
            task = await _connect_server(
                "dependency-readme",
                {"url": url, "transport": transport, "connect_timeout": 15, "tool_timeout": 15},
            )
            try:
                assert task.session is not None
                tools = await task.session.list_tools()
                assert "read_repository_readme" in {tool.name for tool in tools.tools}
                result = await task.session.call_tool("read_repository_readme", {})
                assert not result.is_error
                text = "".join(block.text for block in result.content if isinstance(block, TextContent))
                expected = readme.read_text(encoding="utf-8")
                assert text == expected
            finally:
                await task.shutdown()
    finally:
        reset_hermes_home_override(token)
