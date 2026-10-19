"""Read the real repository through actual HTTP libraries and a loopback server."""

import asyncio
import socket
from pathlib import Path

import aiohttp
import pytest
import requests
from aiohttp import web
from tornado.httpclient import AsyncHTTPClient


@pytest.mark.asyncio
async def test_http_clients_preserve_repository_response_and_headers():
    document = Path(__file__).resolve().parents[3] / "README.md"
    expected = document.read_bytes()

    async def read_document(request):
        return web.FileResponse(document)

    app = web.Application()
    app.router.add_get("/document", read_document)
    runner = web.AppRunner(app)
    await runner.setup()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    site = web.SockSite(runner, listener)
    try:
        await site.start()
        url = f"http://127.0.0.1:{port}/document"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                assert response.status == 200 and await response.read() == expected
                etag = response.headers["ETag"]
                items = response.headers.items()
                assert items | items == set(items)
                assert items - items == set()
            async with session.get(url, headers={"If-None-Match": etag}) as response:
                assert response.status == 304 and await response.read() == b""
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        assert response.status_code == 200 and response.content == expected
        client = AsyncHTTPClient(force_instance=True)
        try:
            response = await client.fetch(url, request_timeout=10)
            assert response.code == 200 and response.body == expected
        finally:
            client.close()
    finally:
        await runner.cleanup()
        listener.close()
