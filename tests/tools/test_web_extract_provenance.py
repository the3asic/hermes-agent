"""Behavior contract for model-visible ``web_extract`` provenance."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from unittest.mock import patch

import pytest

from plugins.web import keyless_mcp
from tools import web_tools


class _ExtractProvider:
    display_name = "Test Extract"

    def __init__(self, name, handler):
        self.name = name
        self._handler = handler
        self.calls = []

    def is_available(self):
        return True

    def supports_extract(self):
        return True

    async def extract(self, urls, **kwargs):
        self.calls.append(list(urls))
        return copy.deepcopy(self._handler(list(urls)))


def _successful(urls, *, content="page-body"):
    return [
        {"url": url, "title": "Title", "content": content}
        for url in urls
    ]


def _failed(urls, *, error="upstream exploded"):
    return [
        {"url": url, "title": "", "content": "", "error": error}
        for url in urls
    ]


def _assert_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.utcoffset() is not None


def _assert_provenance_has_no_page_payload(provenance, *raw_values):
    serialized = json.dumps(provenance, ensure_ascii=False)
    for value in raw_values:
        assert value not in serialized
    assert "url" not in provenance
    assert "content" not in provenance
    assert "error" not in provenance


def _install(
    monkeypatch,
    provider,
    *,
    cache_hits=None,
    cache_enabled=True,
    rescue_eligible=False,
):
    cache_hits = cache_hits or {}
    cache_writes = []
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: provider.name)
    monkeypatch.setattr(
        "agent.web_search_registry.get_provider", lambda name: provider
    )
    monkeypatch.setattr(web_tools, "_rescue_eligible", lambda selected: rescue_eligible)

    async def _safe(_url):
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)
    monkeypatch.setattr(
        "tools.website_policy.check_website_access", lambda url: None
    )
    monkeypatch.setattr(
        "tools.web_result_cache.cache_enabled", lambda: cache_enabled
    )
    monkeypatch.setattr(
        "tools.web_result_cache.extract_cache_get",
        lambda url, **kwargs: copy.deepcopy(cache_hits.get(url)),
    )
    monkeypatch.setattr(
        "tools.web_result_cache.extract_cache_put",
        lambda *args, **kwargs: cache_writes.append((args, kwargs)),
    )
    monkeypatch.setattr(web_tools._debug, "log_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_tools._debug, "save", lambda: None)
    return cache_writes


@pytest.mark.asyncio
async def test_direct_extract_provenance_and_cache_write(monkeypatch):
    url = "https://direct.example/private-path"
    content = "raw-direct-content"

    def _direct(urls):
        rows = _successful(urls, content=content)
        # Provider-owned lookalikes must not impersonate Hermes cache facts.
        rows[0].update({
            "cached": True,
            "served_by": "spoofed-vendor",
            "retrieved_at": "2000-01-01T00:00:00Z",
        })
        return rows

    provider = _ExtractProvider(
        "firecrawl", _direct
    )
    cache_writes = _install(monkeypatch, provider)

    result = json.loads(await web_tools.web_extract_tool([url]))

    assert list(result)[:2] == ["provenance", "results"]
    provenance = result["provenance"]
    assert provenance["requested_backend"] == "firecrawl"
    assert provenance["served_by"] == "firecrawl"
    assert provenance["fallback_attempted"] is False
    assert provenance["fallback_used"] is False
    assert provenance["cache_status"] == "miss"
    assert provenance["provider_call_attempted"] is True
    assert provenance["fetch_succeeded"] is True
    assert provenance["success_count"] == 1
    assert provenance["failure_count"] == 0
    assert provenance["requested_count"] == 1
    assert provenance["returned_count"] == 1
    _assert_timestamp(provenance["retrieved_at"])
    assert provenance["retrieved_at"] != "2000-01-01T00:00:00Z"
    _assert_timestamp(provenance["served_at"])
    _assert_provenance_has_no_page_payload(provenance, url, content)
    assert cache_writes[0][1]["served_by"] == "firecrawl"
    assert cache_writes[0][1]["retrieved_at"] == provenance["retrieved_at"]


@pytest.mark.asyncio
async def test_partial_success_counts_without_copying_error(monkeypatch):
    urls = ["https://partial.example/ok", "https://partial.example/fail"]
    error = "private provider diagnostic"

    def _partial(requested_urls):
        return [
            {"url": requested_urls[0], "title": "OK", "content": "body"},
            {
                "url": requested_urls[1],
                "title": "",
                "content": "",
                "error": error,
            },
        ]

    provider = _ExtractProvider("firecrawl", _partial)
    cache_writes = _install(monkeypatch, provider)

    result = json.loads(await web_tools.web_extract_tool(urls))
    provenance = result["provenance"]

    assert provenance["served_by"] == "firecrawl"
    assert provenance["success_count"] == 1
    assert provenance["failure_count"] == 1
    assert provenance["requested_count"] == 2
    assert provenance["returned_count"] == 2
    assert len(cache_writes) == 1
    _assert_provenance_has_no_page_payload(provenance, *urls, error)


@pytest.mark.asyncio
async def test_full_cache_hit_reports_original_retrieval(monkeypatch):
    urls = ["https://cache.example/a", "https://cache.example/b"]
    oldest = "2026-09-01T01:02:03Z"
    cache_hits = {
        urls[0]: {
            "url": urls[0], "title": "A", "content": "cached-a",
            "error": None, "cached": True, "served_by": "firecrawl",
            "retrieved_at": oldest,
        },
        urls[1]: {
            "url": urls[1], "title": "B", "content": "cached-b",
            "error": None, "cached": True, "served_by": "firecrawl",
            "retrieved_at": "2026-09-02T01:02:03Z",
        },
    }
    provider = _ExtractProvider("firecrawl", _successful)
    cache_writes = _install(monkeypatch, provider, cache_hits=cache_hits)

    result = json.loads(await web_tools.web_extract_tool(urls))
    provenance = result["provenance"]

    assert provenance["served_by"] == "firecrawl"
    assert provenance["fallback_attempted"] is False
    assert provenance["fallback_used"] is False
    assert provenance["cache_status"] == "hit"
    assert provenance["provider_call_attempted"] is False
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] == oldest
    assert provenance["success_count"] == 2
    assert provenance["failure_count"] == 0
    assert provenance["requested_count"] == 2
    assert provenance["returned_count"] == 2
    assert provider.calls == []
    assert cache_writes == []
    _assert_provenance_has_no_page_payload(provenance, *urls, "cached-a")


@pytest.mark.asyncio
async def test_mixed_cache_and_fetch_reports_both_truthfully(monkeypatch):
    urls = ["https://mixed.example/cached", "https://mixed.example/live"]
    cached_at = "2026-09-01T01:02:03Z"
    cache_hits = {
        urls[0]: {
            "url": urls[0], "title": "Cached", "content": "old-body",
            "error": None, "cached": True, "served_by": "exa",
            "retrieved_at": cached_at,
        }
    }
    provider = _ExtractProvider("exa", _successful)
    cache_writes = _install(monkeypatch, provider, cache_hits=cache_hits)

    result = json.loads(await web_tools.web_extract_tool(urls))
    provenance = result["provenance"]

    assert provenance["served_by"] == "exa"
    assert provenance["cache_status"] == "mixed"
    assert provenance["provider_call_attempted"] is True
    assert provenance["fetch_succeeded"] is True
    assert provenance["retrieved_at"] == cached_at
    assert provenance["success_count"] == 2
    assert provider.calls == [[urls[1]]]
    assert len(cache_writes) == 1
    _assert_provenance_has_no_page_payload(provenance, *urls, "old-body")


@pytest.mark.asyncio
async def test_mixed_cache_success_and_failed_rescue_does_not_claim_fallback_used(
    monkeypatch,
):
    urls = ["https://mixed-fail.example/cached", "https://mixed-fail.example/live"]
    cached_at = "2026-09-01T01:02:03Z"
    cache_hits = {
        urls[0]: {
            "url": urls[0],
            "title": "Cached",
            "content": "cached-success",
            "error": None,
            "served_by": "keenable",
            "retrieved_at": cached_at,
        }
    }
    provider = _ExtractProvider("keenable", _failed)
    cache_writes = _install(
        monkeypatch,
        provider,
        cache_hits=cache_hits,
        rescue_eligible=True,
    )

    with patch.object(
        keyless_mcp,
        "extract_with_failover",
        return_value=_failed([urls[1]], error="ring dead"),
    ):
        result = json.loads(await web_tools.web_extract_tool(urls))
    provenance = result["provenance"]

    assert provenance["served_by"] == "keenable"
    assert provenance["fallback_attempted"] is True
    assert provenance["fallback_used"] is False
    assert provenance["provider_call_attempted"] is True
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] == cached_at
    assert provenance["success_count"] == 1
    assert provenance["failure_count"] == 1
    assert cache_writes == []


@pytest.mark.asyncio
async def test_policy_only_failure_does_not_claim_fetch_or_fallback(monkeypatch):
    url = "https://policy.example/page"

    def _blocked(urls):
        return [
            {
                "url": item,
                "title": "",
                "content": "",
                "error": "Blocked by website policy",
                "blocked_by_policy": {"rule": "test"},
            }
            for item in urls
        ]

    provider = _ExtractProvider("keenable", _blocked)
    cache_writes = _install(monkeypatch, provider, rescue_eligible=True)

    with patch.object(
        keyless_mcp,
        "extract_with_failover",
        side_effect=AssertionError("policy refusal must not enter keyless fallback"),
    ):
        result = json.loads(await web_tools.web_extract_tool([url]))
    provenance = result["provenance"]

    assert provenance["fallback_attempted"] is False
    assert provenance["fallback_used"] is False
    assert provenance["provider_call_attempted"] is True
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] is None
    assert provenance["served_by"] is None
    assert cache_writes == []
    _assert_provenance_has_no_page_payload(
        provenance, url, "Blocked by website policy"
    )


@pytest.mark.asyncio
async def test_one_shot_rescue_reports_vendor_and_is_not_cached(monkeypatch):
    url = "https://rescue.example/page"
    provider = _ExtractProvider("keenable", _failed)
    cache_writes = _install(monkeypatch, provider, rescue_eligible=True)
    rescued = _successful([url], content="rescued-content")
    rescued[0][keyless_mcp.EXTRACT_SERVED_BY_FIELD] = "exa"

    with patch.object(
        keyless_mcp, "extract_with_failover", return_value=rescued
    ):
        result = json.loads(await web_tools.web_extract_tool([url]))
    provenance = result["provenance"]

    assert provenance["requested_backend"] == "keenable"
    assert provenance["served_by"] == "exa"
    assert provenance["fallback_attempted"] is True
    assert provenance["fallback_used"] is True
    assert provenance["provider_call_attempted"] is True
    assert provenance["fetch_succeeded"] is True
    assert provenance["success_count"] == 1
    assert cache_writes == []
    assert keyless_mcp.EXTRACT_SERVED_BY_FIELD not in result["results"][0]
    assert keyless_mcp.EXTRACT_FALLBACK_USED_FIELD not in result["results"][0]
    _assert_provenance_has_no_page_payload(
        provenance, url, "rescued-content", "upstream exploded"
    )


@pytest.mark.asyncio
async def test_keyless_vendor_failover_is_visible_and_not_cached(monkeypatch):
    url = "https://ring.example/page"

    def _ring_extract(urls):
        return keyless_mcp.extract_with_failover("exa", urls)

    provider = _ExtractProvider("exa", _ring_extract)
    cache_writes = _install(monkeypatch, provider)
    with patch.object(keyless_mcp, "_ring_order", return_value=["exa", "parallel"]), \
         patch.dict(
             keyless_mcp._KEYLESS_EXTRACTORS,
             {
                 "exa": lambda urls: _failed(urls, error="HTTP 429"),
                 "parallel": lambda urls: _successful(
                     urls, content="parallel-content"
                 ),
             },
         ):
        result = json.loads(await web_tools.web_extract_tool([url]))
    provenance = result["provenance"]

    assert provenance["requested_backend"] == "exa"
    assert provenance["served_by"] == "parallel"
    assert provenance["fallback_attempted"] is True
    assert provenance["fallback_used"] is True
    assert provenance["provider_call_attempted"] is True
    assert provenance["fetch_succeeded"] is True
    assert cache_writes == []
    assert keyless_mcp.EXTRACT_SERVED_BY_FIELD not in result["results"][0]
    assert keyless_mcp.EXTRACT_FALLBACK_USED_FIELD not in result["results"][0]
    _assert_provenance_has_no_page_payload(
        provenance, url, "parallel-content", "HTTP 429"
    )


@pytest.mark.asyncio
async def test_keyless_all_failed_preserves_attempt_without_claiming_use(
    monkeypatch,
):
    url = "https://ring-failed.example/page"

    def _ring_extract(urls):
        return keyless_mcp.extract_with_failover("exa", urls)

    provider = _ExtractProvider("exa", _ring_extract)
    cache_writes = _install(monkeypatch, provider)
    with patch.object(keyless_mcp, "_ring_order", return_value=["exa", "parallel"]), \
         patch.dict(
             keyless_mcp._KEYLESS_EXTRACTORS,
             {
                 "exa": lambda urls: _failed(urls, error="HTTP 429 exa"),
                 "parallel": lambda urls: _failed(
                     urls, error="HTTP 429 parallel"
                 ),
             },
         ):
        result = json.loads(await web_tools.web_extract_tool([url]))
    provenance = result["provenance"]

    assert provenance["served_by"] is None
    assert provenance["fallback_attempted"] is True
    assert provenance["fallback_used"] is False
    assert provenance["provider_call_attempted"] is True
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] is None
    assert cache_writes == []
    assert keyless_mcp.EXTRACT_FALLBACK_ATTEMPTED_FIELD not in result["results"][0]
    assert keyless_mcp.EXTRACT_FALLBACK_USED_FIELD not in result["results"][0]


@pytest.mark.asyncio
async def test_all_failed_has_null_served_by(monkeypatch):
    url = "https://failed.example/page"
    provider = _ExtractProvider("keenable", _failed)
    cache_writes = _install(monkeypatch, provider, rescue_eligible=True)
    with patch.object(
        keyless_mcp,
        "extract_with_failover",
        return_value=_failed([url], error="ring dead"),
    ):
        result = json.loads(await web_tools.web_extract_tool([url]))
    provenance = result["provenance"]

    assert provenance["served_by"] is None
    assert provenance["fallback_attempted"] is True
    assert provenance["fallback_used"] is False
    assert provenance["provider_call_attempted"] is True
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] is None
    assert provenance["success_count"] == 0
    assert provenance["failure_count"] == 1
    assert cache_writes == []
    _assert_provenance_has_no_page_payload(
        provenance, url, "upstream exploded", "ring dead"
    )


@pytest.mark.asyncio
async def test_invalid_only_result_still_has_mandatory_provenance(monkeypatch):
    provider = _ExtractProvider("firecrawl", _successful)
    _install(monkeypatch, provider)

    result = json.loads(await web_tools.web_extract_tool([{}]))
    provenance = result["provenance"]

    assert list(result)[:2] == ["provenance", "results"]
    assert provenance["requested_backend"] is None
    assert provenance["served_by"] is None
    assert provenance["provider_call_attempted"] is False
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] is None
    assert provenance["requested_count"] == 1
    assert provenance["returned_count"] == 1
    assert provenance["success_count"] == 0
    assert provenance["failure_count"] == 1


@pytest.mark.asyncio
async def test_secret_url_error_has_mandatory_payload_free_provenance():
    secret_url = "https://example.com/?token=sk-secret-value"

    result = json.loads(await web_tools.web_extract_tool([secret_url]))
    provenance = result["provenance"]

    assert list(result) == ["provenance", "success", "error"]
    assert result["success"] is False
    assert provenance["requested_backend"] is None
    assert provenance["provider_call_attempted"] is False
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] is None
    assert provenance["requested_count"] == 1
    assert provenance["returned_count"] == 0
    _assert_provenance_has_no_page_payload(provenance, secret_url, "sk-secret-value")


@pytest.mark.asyncio
async def test_ssrf_only_result_has_mandatory_provenance(monkeypatch):
    url = "https://blocked.example/page"
    provider = _ExtractProvider("firecrawl", _successful)
    _install(monkeypatch, provider)

    async def _blocked(_url):
        return False

    monkeypatch.setattr(web_tools, "async_is_safe_url", _blocked)
    result = json.loads(await web_tools.web_extract_tool([url]))
    provenance = result["provenance"]

    assert list(result)[:2] == ["provenance", "results"]
    assert provenance["requested_backend"] is None
    assert provenance["provider_call_attempted"] is False
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] is None
    assert provenance["requested_count"] == 1
    assert provenance["returned_count"] == 1
    assert provider.calls == []
    _assert_provenance_has_no_page_payload(provenance, url)


@pytest.mark.asyncio
async def test_provider_selection_error_has_mandatory_provenance(monkeypatch):
    url = "https://selection.example/page"
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "missing")
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda name: None)
    monkeypatch.setattr(
        "agent.web_search_registry._disabled_web_plugin_for",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "tools.tool_backend_helpers.selection_exists", lambda section: True
    )

    async def _safe(_url):
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)
    monkeypatch.setattr(web_tools._debug, "log_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_tools._debug, "save", lambda: None)

    result = json.loads(await web_tools.web_extract_tool([url]))
    provenance = result["provenance"]

    assert list(result) == ["provenance", "success", "error"]
    assert result["success"] is False
    assert provenance["requested_backend"] == "missing"
    assert provenance["served_by"] is None
    assert provenance["provider_call_attempted"] is False
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] is None
    assert provenance["requested_count"] == 1
    assert provenance["returned_count"] == 0
    _assert_provenance_has_no_page_payload(provenance, url, result["error"])


@pytest.mark.asyncio
async def test_provider_exception_has_mandatory_provenance(monkeypatch):
    url = "https://exception.example/page"

    def _raise(_urls):
        raise RuntimeError("private provider failure")

    provider = _ExtractProvider("firecrawl", _raise)
    _install(monkeypatch, provider, rescue_eligible=False)

    result = json.loads(await web_tools.web_extract_tool([url]))
    provenance = result["provenance"]

    assert list(result) == ["provenance", "error"]
    assert provenance["requested_backend"] == "firecrawl"
    assert provenance["served_by"] is None
    assert provenance["provider_call_attempted"] is True
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] is None
    assert provenance["requested_count"] == 1
    assert provenance["returned_count"] == 0
    _assert_provenance_has_no_page_payload(
        provenance, url, "private provider failure"
    )


@pytest.mark.asyncio
async def test_empty_provider_result_keeps_provenance_on_error(monkeypatch):
    url = "https://empty.example/page"
    provider = _ExtractProvider("firecrawl", lambda _urls: [])
    _install(monkeypatch, provider)

    result = json.loads(await web_tools.web_extract_tool([url]))
    provenance = result["provenance"]

    assert list(result) == ["provenance", "results"]
    assert provenance["requested_backend"] == "firecrawl"
    assert provenance["provider_call_attempted"] is True
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] is None
    assert provenance["requested_count"] == 1
    assert provenance["returned_count"] == 1
    assert provenance["failure_count"] == 1
    assert result["results"][0]["url"] == url
    assert web_tools._EXTRACT_RESULT_MISSING_ERROR in result["results"][0]["error"]


@pytest.mark.asyncio
async def test_only_second_provider_row_is_matched_by_url_not_position(
    monkeypatch,
):
    urls = ["https://count.example/a", "https://count.example/b"]

    def _only_second(requested):
        # Parallel/keyless shape: successful rows first, then a synthesized
        # error for an endpoint-dropped URL. The list order is B, A.
        return [
            _successful([requested[1]], content="body-b")[0],
            {
                "url": requested[0],
                "title": "",
                "content": "",
                "error": "no content returned",
            },
        ]

    provider = _ExtractProvider(
        "firecrawl", _only_second
    )
    cache_writes = _install(monkeypatch, provider)

    result = json.loads(await web_tools.web_extract_tool(urls))
    provenance = result["provenance"]

    assert provenance["requested_count"] == 2
    assert provenance["returned_count"] == 2
    assert provenance["success_count"] == 1
    assert provenance["failure_count"] == 1
    assert result["results"][0]["url"] == urls[0]
    assert result["results"][0]["error"] == "no content returned"
    assert result["results"][1]["url"] == urls[1]
    assert result["results"][1]["content"] == "body-b"
    assert cache_writes == [
        ((urls[1], "body-b"), {
            "title": "Title",
            "format": None,
            "provider": "firecrawl",
            "served_by": "firecrawl",
            "retrieved_at": provenance["retrieved_at"],
        })
    ]


@pytest.mark.asyncio
async def test_middle_missing_row_is_filled_without_cross_url_content(
    monkeypatch,
):
    urls = [
        "https://middle.example/a",
        "https://middle.example/b",
        "https://middle.example/c",
    ]

    def _out_of_order_without_middle(requested):
        return [
            _successful([requested[2]], content="body-c")[0],
            _successful([requested[0]], content="body-a")[0],
        ]

    provider = _ExtractProvider("firecrawl", _out_of_order_without_middle)
    cache_writes = _install(monkeypatch, provider)

    result = json.loads(await web_tools.web_extract_tool(urls))

    assert [row["url"] for row in result["results"]] == urls
    assert result["results"][0]["content"] == "body-a"
    assert web_tools._EXTRACT_RESULT_MISSING_ERROR in result["results"][1]["error"]
    assert result["results"][2]["content"] == "body-c"
    assert result["provenance"]["success_count"] == 2
    assert result["provenance"]["failure_count"] == 1
    assert [args[0] for args, _kwargs in cache_writes] == [urls[0], urls[2]]
    assert [args[1] for args, _kwargs in cache_writes] == ["body-a", "body-c"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_mapping", ["duplicate", "unexpected", "ambiguous"])
async def test_duplicate_unexpected_or_ambiguous_rows_fail_closed(
    monkeypatch,
    bad_mapping,
):
    urls = ["https://mapping.example/a", "https://mapping.example/b"]

    def _bad(_requested):
        if bad_mapping == "duplicate":
            return [
                _successful([urls[0]], content="first-a")[0],
                _successful([urls[0]], content="second-a")[0],
            ]
        if bad_mapping == "unexpected":
            return [
                _successful([urls[0]], content="body-a")[0],
                _successful(["https://mapping.example/other"], content="other")[0],
            ]
        row = _successful([urls[0]], content="ambiguous")[0]
        row["metadata"] = {"sourceURL": urls[1]}
        return [row]

    provider = _ExtractProvider("firecrawl", _bad)
    cache_writes = _install(monkeypatch, provider)

    result = json.loads(await web_tools.web_extract_tool(urls))
    provenance = result["provenance"]

    assert [row["url"] for row in result["results"]] == urls
    assert all(
        web_tools._EXTRACT_RESULT_MAPPING_ERROR in row["error"]
        for row in result["results"]
    )
    assert provenance["success_count"] == 0
    assert provenance["failure_count"] == 2
    assert provenance["fetch_succeeded"] is False
    assert provenance["retrieved_at"] is None
    assert cache_writes == []


@pytest.mark.asyncio
async def test_fallback_only_second_row_maps_by_url_and_fills_missing(
    monkeypatch,
):
    urls = ["https://fallback-map.example/a", "https://fallback-map.example/b"]
    provider = _ExtractProvider("keenable", _failed)
    cache_writes = _install(monkeypatch, provider, rescue_eligible=True)
    rescued = _successful([urls[1]], content="fallback-b")
    rescued[0][keyless_mcp.EXTRACT_SERVED_BY_FIELD] = "parallel"

    with patch.object(keyless_mcp, "extract_with_failover", return_value=rescued):
        result = json.loads(await web_tools.web_extract_tool(urls))
    provenance = result["provenance"]

    assert [row["url"] for row in result["results"]] == urls
    assert web_tools._EXTRACT_RESULT_MISSING_ERROR in result["results"][0]["error"]
    assert result["results"][1]["content"] == "fallback-b"
    assert provenance["served_by"] == "parallel"
    assert provenance["fallback_attempted"] is True
    assert provenance["fallback_used"] is True
    assert provenance["success_count"] == 1
    assert provenance["failure_count"] == 1
    assert cache_writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_fallback", ["duplicate", "unexpected"])
async def test_fallback_duplicate_or_unexpected_rows_fail_closed(
    monkeypatch,
    bad_fallback,
):
    urls = ["https://fallback-bad.example/a", "https://fallback-bad.example/b"]
    provider = _ExtractProvider("keenable", _failed)
    cache_writes = _install(monkeypatch, provider, rescue_eligible=True)
    if bad_fallback == "duplicate":
        rescued = [
            _successful([urls[0]], content="one")[0],
            _successful([urls[0]], content="two")[0],
        ]
    else:
        rescued = _successful(
            [urls[0], "https://fallback-bad.example/other"],
            content="unexpected",
        )

    with patch.object(keyless_mcp, "extract_with_failover", return_value=rescued):
        result = json.loads(await web_tools.web_extract_tool(urls))
    provenance = result["provenance"]

    assert [row["url"] for row in result["results"]] == urls
    assert all(row.get("error") for row in result["results"])
    assert provenance["fallback_attempted"] is True
    assert provenance["fallback_used"] is False
    assert provenance["success_count"] == 0
    assert provenance["failure_count"] == 2
    assert cache_writes == []
