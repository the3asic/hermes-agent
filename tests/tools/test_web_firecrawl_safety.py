"""Firecrawl redirect identity, cache policy, and cancellation contracts."""

import asyncio
import ipaddress
import json
import os
import socket
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

from plugins.web import keyless_mcp
from plugins.web.firecrawl import provider as firecrawl
from tools import interrupt, web_result_cache, web_tools, website_policy


class SDKMetadata(BaseModel):
    """Frozen Firecrawl SDK model_dump contract: snake_case request fields."""

    source_url: str
    url: str | None = None
    title: str = "Page"
    status_code: int = 200


class SDKDocument(BaseModel):
    markdown: str
    metadata: SDKMetadata


@pytest.fixture
def configured(monkeypatch):
    config_path = Path(os.environ["HERMES_HOME"]) / "config.yaml"

    def configure(*, blocked=(), cache=True):
        config_path.write_text(json.dumps({
            "web": {"extract_backend": "firecrawl", "cache_enabled": cache},
            "security": {"website_blocklist": {
                "enabled": True, "domains": list(blocked), "shared_files": [],
            }},
        }), encoding="utf-8")
        website_policy.invalidate_cache()

    configure()
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:3002")

    def resolve(host, *args, **kwargs):
        try:
            address = str(ipaddress.ip_address(host))
        except ValueError:
            address = "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(web_tools._debug, "log_call", lambda *args: None)
    monkeypatch.setattr(web_tools._debug, "save", lambda: None)
    # Discover and resolve the real provider through the actual config loader.
    web_tools._ensure_web_plugins_loaded()
    sdk = Mock()
    monkeypatch.setattr(firecrawl, "_get_firecrawl_client", lambda: sdk)
    rescue = Mock(side_effect=AssertionError("Refusal must not enter cloud rescue"))
    monkeypatch.setattr(keyless_mcp, "extract_with_failover", rescue)
    interrupt.set_interrupt(False)
    yield configure, sdk, rescue
    interrupt.set_interrupt(False)
    website_policy.invalidate_cache()


def document(request, final, *, sdk=False, content="PAGE_BODY"):
    if sdk:
        return SDKDocument(
            markdown=content,
            metadata=SDKMetadata(source_url=request, url=final),
        )
    return {"markdown": content, "metadata": {
        "sourceURL": request, "url": final, "title": "Page", "statusCode": 200,
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk_payload", [False, True])
async def test_real_provider_redirect_keeps_request_identity_and_cache(configured, sdk_payload):
    _, sdk, rescue = configured
    urls = ["https://allowed.example/first", "https://allowed.example/second"]
    final = "https://destination.example/redirected"
    sdk.scrape.side_effect = lambda *, url, **kw: document(url, final, sdk=sdk_payload)
    result = json.loads(await web_tools.web_extract_tool(urls))
    assert [row["url"] for row in result["results"]] == urls
    assert [row["content"] for row in result["results"]] == ["PAGE_BODY"] * 2
    assert result["provenance"]["success_count"] == 2
    assert result["provenance"]["fallback_attempted"] is False
    for url in urls:
        hit = web_result_cache.extract_cache_get(url, provider="firecrawl")
        assert hit["final_url"] == final
    cached = json.loads(await web_tools.web_extract_tool(urls))
    assert cached["provenance"]["cache_status"] == "hit"
    assert sdk.scrape.call_count == 2
    rescue.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk_payload", [False, True])
@pytest.mark.parametrize("private", [False, True])
async def test_final_address_refusal_never_returns_content_or_rescues(configured, sdk_payload, private):
    configure, sdk, rescue = configured
    configure(blocked=["blocked.example"])
    request = "https://allowed.example/redirect"
    final = "http://127.0.0.1/private" if private else "https://blocked.example/page"
    sdk.scrape.return_value = document(request, final, sdk=sdk_payload)
    result = json.loads(await web_tools.web_extract_tool([request]))
    row = result["results"][0]
    assert row["url"] == request
    assert row["content"] == ""
    assert row["blocked_by_security"] if private else row["blocked_by_policy"]
    assert result["provenance"]["success_count"] == 0
    assert result["provenance"]["fallback_attempted"] is False
    assert web_result_cache.extract_cache_get(request, provider="firecrawl") is None
    rescue.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_key", ["sourceURL", "source_url"])
async def test_mismatched_source_is_rejected_without_poisoning_sibling(configured, source_key):
    _, sdk, rescue = configured
    first, second = "https://allowed.example/first", "https://allowed.example/second"
    sdk.scrape.side_effect = [
        {"markdown": "WRONG_BODY", "metadata": {source_key: second, "url": first}},
        document(second, second),
    ]
    result = json.loads(await web_tools.web_extract_tool([first, second]))
    assert result["results"][0]["error"] == web_tools._EXTRACT_RESULT_MAPPING_ERROR
    assert result["results"][0]["content"] == ""
    assert result["results"][1]["content"] == "PAGE_BODY"
    assert result["provenance"]["success_count"] == 1
    rescue.assert_not_called()


@pytest.mark.asyncio
async def test_cached_redirect_is_rechecked_after_policy_change(configured):
    configure, sdk, rescue = configured
    request, final = "https://allowed.example/source", "https://blocked.example/final"
    sdk.scrape.return_value = document(request, final, sdk=True)
    initial = json.loads(await web_tools.web_extract_tool([request]))
    assert initial["results"][0]["content"] == "PAGE_BODY"
    configure(blocked=["blocked.example"])
    blocked = json.loads(await web_tools.web_extract_tool([request]))
    assert blocked["results"][0]["blocked_by_policy"]
    assert blocked["results"][0]["content"] == ""
    assert blocked["provenance"]["provider_call_attempted"] is False
    assert sdk.scrape.call_count == 1
    rescue.assert_not_called()


def test_old_firecrawl_cache_without_final_address_misses(configured):
    url = "https://allowed.example/cached"
    web_result_cache.extract_cache_put(url, "BODY", provider="firecrawl")
    index = web_result_cache._load_index()
    entry = index[web_result_cache._url_digest(url, None, "firecrawl")]
    entry.pop("final_url")
    web_result_cache._save_index(index)
    assert web_result_cache.extract_cache_get(url, provider="firecrawl") is None


@pytest.mark.parametrize("private", [False, True])
def test_keyless_firecrawl_uses_same_final_address_checks(configured, monkeypatch, private):
    configure, _, _ = configured
    configure(blocked=["blocked.example"])
    request = "https://allowed.example/redirect"
    final = "http://127.0.0.1/private" if private else "https://blocked.example/page"
    monkeypatch.setattr(
        firecrawl._KeylessFirecrawlClient, "scrape",
        lambda *args, **kwargs: document(request, final, sdk=True),
    )
    row = keyless_mcp.firecrawl_extract_keyless([request])[0]
    assert row["url"] == request
    assert row["content"] == ""
    assert keyless_mcp.is_extract_refusal(row)


@pytest.mark.asyncio
async def test_interrupt_before_dispatch_makes_no_provider_or_cloud_call(configured):
    _, sdk, rescue = configured
    interrupt.set_interrupt(True)
    result = json.loads(await web_tools.web_extract_tool(["https://allowed.example/page"]))
    assert result["error"] == "Interrupted"
    assert result["provenance"]["provider_call_attempted"] is False
    sdk.scrape.assert_not_called()
    rescue.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_during_scrape_preserves_completed_pages_and_stops_batch(configured):
    _, sdk, rescue = configured
    owner_thread = threading.get_ident()
    urls = [f"https://allowed.example/page{number}" for number in range(3)]

    def scrape(*, url, **kwargs):
        if url == urls[1]:
            assert threading.get_ident() != owner_thread
            interrupt.set_interrupt(True, owner_thread)
        return document(url, url)

    sdk.scrape.side_effect = scrape
    result = json.loads(await web_tools.web_extract_tool(urls))
    assert result["results"][0]["content"] == "PAGE_BODY"
    assert all(row["error"] == "Interrupted" for row in result["results"][1:])
    assert sdk.scrape.call_count == 2
    assert result["provenance"]["fallback_attempted"] is False
    rescue.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_during_failed_provider_never_starts_rescue(configured):
    _, sdk, rescue = configured
    owner_thread = threading.get_ident()

    def scrape(**kwargs):
        interrupt.set_interrupt(True, owner_thread)
        raise RuntimeError("HTTP 429")

    sdk.scrape.side_effect = scrape
    result = json.loads(await web_tools.web_extract_tool(["https://allowed.example/page"]))
    assert result["provenance"]["fallback_attempted"] is False
    assert result["provenance"]["success_count"] == 0
    rescue.assert_not_called()


@pytest.mark.asyncio
async def test_keyless_ring_worker_observes_origin_thread_interrupt(monkeypatch):
    owner_thread = threading.get_ident()
    url = "https://allowed.example/page"
    second = Mock(side_effect=AssertionError("Must not start the next vendor"))

    def first(urls):
        assert threading.get_ident() != owner_thread
        interrupt.set_interrupt(True, owner_thread)
        return [{"url": url, "error": "429 rate limit"}]

    monkeypatch.setattr(keyless_mcp, "_ring_order", lambda name: ["firecrawl", "exa"])
    monkeypatch.setitem(keyless_mcp._KEYLESS_EXTRACTORS, "firecrawl", first)
    monkeypatch.setitem(keyless_mcp._KEYLESS_EXTRACTORS, "exa", second)

    @keyless_mcp.web_interruptible
    async def dispatch():
        return await asyncio.to_thread(keyless_mcp.extract_with_failover, "firecrawl", [url])

    try:
        rows = await dispatch()
        assert rows[0]["error"] == "Interrupted"
        assert rows.fallback_attempted is False
        second.assert_not_called()
    finally:
        interrupt.set_interrupt(False, owner_thread)
    assert keyless_mcp.web_is_interrupted() is False


def test_keyless_refusal_that_mentions_rate_limit_never_advances_ring(monkeypatch):
    url = "https://allowed.example/page"
    refused = {"url": url, "error": "SSRF rejection: 429", "blocked_by_security": True}
    second = Mock()
    monkeypatch.setattr(keyless_mcp, "_ring_order", lambda name: ["firecrawl", "exa"])
    monkeypatch.setitem(keyless_mcp._KEYLESS_EXTRACTORS, "firecrawl", lambda urls: [refused])
    monkeypatch.setitem(keyless_mcp._KEYLESS_EXTRACTORS, "exa", second)
    rows = keyless_mcp.extract_with_failover("firecrawl", [url])
    assert rows[0]["error"] == refused["error"]
    second.assert_not_called()


def test_rescue_preserves_security_refusals_and_completed_rows(monkeypatch):
    urls = [f"https://allowed.example/{index}" for index in range(3)]
    refused = {"url": urls[0], "error": "SSRF rejection", "blocked_by_security": True}
    completed = {"url": urls[1], "content": "COMPLETE"}
    failed = {"url": urls[2], "error": "Backend unavailable"}
    rescue = Mock(return_value=[{"url": urls[2], "content": "RECOVERED"}])
    monkeypatch.setattr(keyless_mcp, "extract_with_failover", rescue)
    rows = web_tools._rescue_extract("firecrawl", urls, [refused, completed, failed])
    rescue.assert_called_once_with("firecrawl", [urls[2]])
    assert rows[0] == refused
    assert rows[1] == completed
    assert rows[2]["content"] == "RECOVERED"


def test_explicit_interrupted_row_stops_rescue_even_without_thread_signal(monkeypatch):
    urls = ["https://allowed.example/first", "https://allowed.example/second"]
    rows = [{"url": urls[0], "error": "Interrupted"}, {"url": urls[1], "error": "429"}]
    rescue = Mock()
    monkeypatch.setattr(keyless_mcp, "extract_with_failover", rescue)
    result = web_tools._rescue_extract("firecrawl", urls, rows)
    assert result == rows
    assert result.fallback_attempted is False
    rescue.assert_not_called()


def test_rescue_failure_preserves_new_security_refusal(monkeypatch):
    url = "https://allowed.example/page"
    refusal = {"url": url, "error": "SSRF rejection", "blocked_by_security": True}
    monkeypatch.setattr(keyless_mcp, "extract_with_failover", lambda *args: [refusal])
    result = web_tools._rescue_extract("firecrawl", [url], [{"url": url, "error": "500"}])
    assert result[0] == refusal
    assert result.fallback_attempted is True
    assert result.fallback_used is False


@pytest.mark.parametrize("raises", [False, True])
def test_search_interrupt_during_provider_never_rescues(configured, monkeypatch, raises):
    _, sdk, _ = configured
    rescue = Mock()
    monkeypatch.setattr(keyless_mcp, "search_with_failover", rescue)

    def search(**kwargs):
        interrupt.set_interrupt(True)
        if raises:
            raise RuntimeError("HTTP 429")
        return {"data": [{"url": "https://allowed.example/found"}]}

    sdk.search.side_effect = search
    result = json.loads(web_tools.web_search_tool("query"))
    assert result["success"] is False
    assert result["error"] == "Interrupted"
    rescue.assert_not_called()


def test_keyless_search_stops_before_next_vendor(monkeypatch):
    second = Mock()

    def first(query, limit):
        interrupt.set_interrupt(True)
        return {"success": False, "error": "429"}

    monkeypatch.setattr(keyless_mcp, "_ring_order", lambda name: ["firecrawl", "exa"])
    monkeypatch.setitem(keyless_mcp._KEYLESS_SEARCHERS, "firecrawl", first)
    monkeypatch.setitem(keyless_mcp._KEYLESS_SEARCHERS, "exa", second)
    try:
        result = keyless_mcp.search_with_failover("firecrawl", "query")
        assert result["error"] == "Interrupted"
        second.assert_not_called()
    finally:
        interrupt.set_interrupt(False)
