---
sidebar_position: 12
title: "Web Search Provider Plugins"
description: "How to build a web-search/extract/crawl backend plugin for Hermes Agent"
---

# Building a Web Search Provider Plugin

Web-search provider plugins register a backend that services `web_search`, `web_extract`, and (optionally) deep-crawl tool calls. Built-in providers — Firecrawl, SearXNG, Exa, Parallel, Keenable, Brave Search (free tier), xAI, and DDGS — all ship as plugins under `plugins/web/<name>/`. You can add a new one, or override a bundled one, by dropping a directory next to them.

:::tip
Web search is one of several **backend plugins** Hermes supports. The others (with their own ABCs) are [Image Generation Provider Plugins](/developer-guide/image-gen-provider-plugin), [Video Generation Provider Plugins](/developer-guide/video-gen-provider-plugin), [Memory Provider Plugins](/developer-guide/memory-provider-plugin), [Context Engine Plugins](/developer-guide/context-engine-plugin), and [Model Provider Plugins](/developer-guide/model-provider-plugin). General tool/hook/CLI plugins live in [Build a Hermes Plugin](/developer-guide/plugins).
:::

## How discovery works

Hermes scans for web-search backends in three places:

1. **Bundled** — `<repo>/plugins/web/<name>/` (auto-loaded with `kind: backend`, always available)
2. **User** — `~/.hermes/plugins/web/<name>/` (opt-in via `plugins.enabled` or `hermes plugins enable <name>`)
3. **Pip** — packages declaring a `hermes_agent.plugins` entry point

Each plugin's `register(ctx)` function calls `ctx.register_web_search_provider(...)` — that puts the instance into the registry in `agent/web_search_registry.py`. The active provider for each capability is picked by config:

| Capability | Config key | Falls back to |
|---|---|---|
| `web_search` | `web.search_backend` | `web.backend` |
| `web_extract` | `web.extract_backend` | `web.backend` |
| Deep crawl modes inside `web_extract` | `web.extract_backend` | `web.backend` |

When neither key is set, Hermes auto-detects the backend from whichever API key/URL is present in the environment. `hermes tools` walks users through selection.

## Directory structure

```
plugins/web/my-backend/
├── __init__.py     # register() entry point
├── provider.py     # WebSearchProvider subclass
└── plugin.yaml     # Manifest with kind: backend and provides_web_providers
```

`brave_free/` and `ddgs/` are the smallest in-tree references — `brave_free` for an API-key-gated search-only provider, `ddgs` for a no-key provider that lazy-installs its SDK.

## The WebSearchProvider ABC

Subclass `agent.web_search_provider.WebSearchProvider`. The only required members are `name`, `is_available()`, and whichever of `search()` / `extract()` you implement. (Deep crawling is not a separate method — it's a mode of `extract()`.)

```python
# plugins/web/my-backend/provider.py
from __future__ import annotations

import os
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider


class MyBackendWebSearchProvider(WebSearchProvider):
    """Minimal search-only provider against the My Backend HTTP API."""

    @property
    def name(self) -> str:
        # Stable id used in web.search_backend / web.extract_backend / web.backend
        # config keys. Lowercase, no spaces; hyphens permitted.
        return "my-backend"

    @property
    def display_name(self) -> str:
        # Human label shown in `hermes tools`. Defaults to `name`.
        return "My Backend"

    def is_available(self) -> bool:
        # Cheap check — env var present, optional dep importable, etc.
        # MUST NOT make network calls (runs on every `hermes tools` paint).
        return bool(os.getenv("MY_BACKEND_API_KEY", "").strip())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        import httpx

        api_key = os.environ["MY_BACKEND_API_KEY"]
        try:
            resp = httpx.get(
                "https://api.example.com/search",
                params={"q": query, "count": max(1, min(int(limit), 20))},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            return {"success": False, "error": str(exc)}

        # The provider envelope stays compact. Hermes adds request-time
        # provenance after this method returns; see "Response shape" below.
        return {
            "success": True,
            "data": {
                # Optional: only upstream/provider facts you can prove.
                "provenance": {
                    "engine": "my-backend",
                    "upstream_cache_timestamp": data.get("cacheTimestamp"),
                    "limitations": ["provider_snippets_only"],
                    "transformations": [],
                },
                "web": [
                    {
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "description": item.get("snippet", ""),
                        "position": idx + 1,
                    }
                    for idx, item in enumerate(data.get("results", []))
                ],
            },
        }
```

```python
# plugins/web/my-backend/__init__.py
from plugins.web.my_backend.provider import MyBackendWebSearchProvider


def register(ctx) -> None:
    """Plugin entry point — called once at load time."""
    ctx.register_web_search_provider(MyBackendWebSearchProvider())
```

## plugin.yaml

```yaml
name: web-my-backend
version: 1.0.0
description: "My Backend web search — Bearer-auth REST API"
author: Your Name
kind: backend
provides_web_providers:
  - my-backend
requires_env:
  - MY_BACKEND_API_KEY
```

| Key | Purpose |
|---|---|
| `kind: backend` | Routes the plugin through the backend-loading path |
| `provides_web_providers` | List of provider `name`s this plugin registers — used by the loader to advertise the plugin in `hermes tools` even before `register()` runs |
| `requires_env` | Interactive credential prompt during `hermes plugins install` (see [Build a Hermes Plugin](/developer-guide/plugins#gate-on-environment-variables) for the rich format) |

## ABC reference

Full contract in `agent/web_search_provider.py`. Methods you may override:

| Member | Required | Default | Purpose |
|---|---|---|---|
| `name` | ✅ | — | Stable id used in `web.*_backend` config |
| `display_name` | — | `name` | Label shown in `hermes tools` |
| `is_available()` | ✅ | — | Cheap availability gate — env vars, optional deps |
| `supports_search()` | — | `True` | Capability flag for `web_search` routing |
| `supports_extract()` | — | `False` | Capability flag for `web_extract` routing |
| `search(query, limit)` | conditional | raises | Required when `supports_search()` returns `True` |
| `extract(urls, **kwargs)` | conditional | raises | Required when `supports_extract()` returns `True` |

Providers can advertise multiple capabilities from a single class — Firecrawl, Keenable, Exa, and Parallel all implement both search and extract. Brave Search and DDGS are search-only; SearXNG is search-only with a documented "pair me with an extract provider" workflow.

## Response shape

Providers return a compact, fixed envelope so the tool wrapper does not have to translate between backends. The client-function wrapper adds a mandatory `data.provenance` block to every successful `web_search` response it handles. A provider does not need to construct the wrapper-owned fields itself.

This contract belongs specifically to `tools.web_tools.web_search_tool`. When an xAI Responses inference session selects xAI search, the transport swaps that client function for xAI's provider-executed native `web_search`; no Hermes function-result envelope exists on that separate surface, so it does not carry this provenance block.

The boundary is fail-closed: `success` must be the literal JSON boolean `true` or `false`. A successful response must contain an object at `data`, a list at `data.web`, and only result objects inside that list. A malformed “success” is returned as a bounded failure, is not cached, and never receives provenance.

**Provider search success:**

```python
{
    "success": True,
    "data": {
        "provenance": {                 # optional at the provider boundary
            "engine": str,
            "limitations": list[str],
            "transformations": list[str],
        },
        "web": [
            {"title": str, "url": str, "description": str, "position": int},
            ...
        ],
    },
}
```

**Final `web_search` success returned by Hermes:**

```python
{
    "success": True,
    "data": {
        "provenance": {
            "requested_backend": str,
            "served_by": str,
            "served_by_source": (
                "provider_reported" | "requested_backend_default"
            ),
            "fallback_used": bool,
            "retrieved_at": str,        # UTC ISO-8601; original retrieval time
            "served_at": str,           # UTC ISO-8601; this response time
            "cache": {
                "layer": "hermes_process_memory",
                "status": "hit" | "miss" | "bypass",
                "age_seconds": float | None,
                "ttl_seconds": float | None,
                "key_dimensions": [
                    "provider_name", "normalized_query", "bucketed_limit"
                ],
                "credential_identity_in_key": False,
                "locale_in_key": False,
                "provider_configuration_in_key": False,
            },
            "evidence_scope": "search_result_metadata_only",
            "page_fetched": False,
            "result_scope": "top_n",
            "requested_limit": int,
            "fetched_result_count": int,
            "returned_count": int,
            "result_set_truncated": bool,
            "result_set_truncation_scope": "hermes_bucket_slice_only",
            "upstream_cache_timestamp": str | None,
            "upstream_cache_timestamp_status": (
                "reported_in_response" |
                "not_reported_in_response" |
                "reported_invalid_rfc3339" |
                "reported_unsupported_rfc3339_leap_second"
            ),
            "limitations": list[str],
            "transformations": list[str],
            # Provider-owned, non-core facts such as `engine` may follow.
        },
        "web": [...],
    },
}
```

The wrapper owns routing, timing, Hermes' process-memory cache state, evidence scope, and result counts. It overwrites those fields even if a provider supplies values for them. `served_by_source` says whether `served_by` came from the provider's legacy `data.served_by` field or defaulted to the selected provider name. On a cache hit, `retrieved_at` remains the time of the original provider response while `served_at` records the current tool response; `cache.age_seconds` is calculated with a monotonic clock.

The search memo key contains only provider name, normalized query, and bucketed limit. Credential identity, locale, and provider configuration are not key dimensions; the fixed boolean fields in `cache` disclose those omissions on every success. `fetched_result_count` is the number returned for Hermes' bucketed provider request, while `returned_count` is the caller-visible count after the requested limit is applied. `result_set_truncated` and `result_set_truncation_scope="hermes_bucket_slice_only"` report only that Hermes slice; they do not claim the upstream result set was exhaustive.

Providers may add stable facts that come directly from their own configuration, documented API semantics, or the upstream response:

- Engine identity, such as `engine`.
- Explicit source-date semantics, such as `source_date_kind` and `source_date_kind_status`. Document the provider-specific values you use.
- An exact `upstream_cache_timestamp`, but only if the upstream response reports a timezone-qualified RFC 3339 date-time. Providers must not set `upstream_cache_timestamp_status`; Hermes validates the value and derives the status. A supported non-leap-second timestamp produces `"reported_in_response"`; a missing or null value becomes `None` with `"not_reported_in_response"`; malformed values become `None` with `"reported_invalid_rfc3339"`. RFC 3339 leap-second notation is valid but not supported by Hermes' parser, so it becomes `None` with `"reported_unsupported_rfc3339_leap_second"` rather than being called invalid. Rejected values include an explicit limitation.
- `limitations` and `transformations`. Hermes merges these lists with its own values in stable order and removes duplicates.

Other provider-owned, non-core provenance keys are preserved. Provider values never override the core fields shown above.

:::warning
Do not infer freshness or authority. Hermes recursively removes case-insensitive bare `confidence`, `fresh`, `current`, `verified`, and `authoritative` keys throughout the provider-owned successful payload and records that omission in `transformations`. If the upstream explicitly reports a related metric, keep the exact fact under a provider-namespaced key (for example, `my_backend_relevance_score`) and document its semantics instead of normalizing it into one of those labels. This fixed guard matches exact keys only; it is not a general semantic judge for arbitrary extension names. Do not turn a relative result date into a guessed publication timestamp, invent an upstream crawl/cache time, or describe a top-N response as complete. `description` remains search-result metadata, not fetched page text; use `web_extract` when page content is required.
:::

The generic `transform_tool_result` hook remains trusted, high-privilege middleware for every tool. If it returns a non-equivalent replacement for a successful structured `web_search`, Hermes accepts the replacement to preserve redaction and plugin compatibility, and logs that the wrapper truth contract no longer describes the model-bound output. A transforming plugin must preserve or recompute provenance for the result it emits.

**Extract success:**

```python
{
    "success": True,
    "data": [
        {
            "url": str,
            "title": str,
            "content": str,
            "raw_content": str,
            "metadata": dict,    # optional
            "error": str,        # optional, only on per-URL failure
        },
        ...
    ],
}
```

**Either capability, on failure:**

```python
{"success": False, "error": "human-readable message"}
```

Failed `web_search` responses intentionally retain this existing envelope and do not receive `data.provenance`.

Both `search()` and `extract()` may be `async def` — the dispatcher detects coroutine functions via `inspect.iscoroutinefunction` and awaits accordingly. Sync implementations that do blocking I/O (HTTP, SDK calls) are fine for small backends; the dispatcher handles threading. The mandatory provenance contract applies to every well-formed success emitted by Hermes' client-function wrapper; `web_extract` keeps the extract envelope above.

## Capability flags

Hermes routes calls to the right provider based on the `supports_*` flags. A common multi-provider setup:

```yaml
# ~/.hermes/config.yaml
web:
  search_backend: "brave-free"     # search-only, fast, free 2k/mo
  extract_backend: "firecrawl"     # extract + crawl, paid quota
```

When `web.search_backend` or `web.extract_backend` aren't set, both fall through to `web.backend`. When that's also unset, Hermes picks the first available provider that supports the requested capability based on env-var presence.

If your provider only supports one capability, leave the other flags at their default (`False`) and the registry will skip it for that tool — users won't see misleading "provider X failed" errors when they're using X only for search and asking the agent to extract.

## How Hermes wires it into the tools

The `web_search` and `web_extract` tools live in `tools/web_tools.py`. At call time they:

1. Read the relevant config key (`web.search_backend` for `web_search`, `web.extract_backend` for `web_extract`)
2. Ask the registry for the provider with that `name`
3. Check `is_available()` and the matching `supports_*()` flag
4. Dispatch to `search()` / `extract()` (deep crawl runs as a mode inside `extract()`), awaiting if the method is a coroutine
5. Validate the strict success/data/web envelope and cache only valid successes
6. Add the mandatory provenance contract to every valid successful `web_search` response
7. JSON-serialize the response envelope; the generic transform hook cannot rewrite a successful search into a different value

Errors surface as the tool result; the LLM decides how to explain them. If no provider is registered (or every available one fails the capability gate), the tool returns a helpful error pointing at `hermes tools`.

## Lazy-installing optional dependencies

If your provider wraps a third-party SDK (like DDGS does with the `ddgs` package), don't `import` it at module top level. Use `tools.lazy_deps.ensure(...)` inside `is_available()` or `search()` — Hermes will install the package on first use, gated by `security.allow_lazy_installs`. See [Build a Hermes Plugin → Lazy-install](/developer-guide/plugins#lazy-install-optional-python-dependencies) for the security model.

## Reference implementations

- **`plugins/web/brave_free/`** — small, API-key-gated, search-only HTTP provider. Good starting template.
- **`plugins/web/ddgs/`** — no-key provider that lazy-installs its SDK. Useful pattern for backends that wrap a Python package.
- **`plugins/web/firecrawl/`** — full multi-capability provider (search + extract + crawl) with multiple format modes.
- **`plugins/web/searxng/`** — self-hosted, URL-configured backend with no auth.
- **`plugins/web/xai/`** — LLM-backed search via Grok's server-side `web_search` tool. Shows how to reuse an existing OAuth/env-var credential surface (`tools/xai_http.py`) without adding new env vars, and how to write a cheap `is_available()` that honors the no-network contract.

## Distribute via pip

```toml
# pyproject.toml
[project.entry-points."hermes_agent.plugins"]
my-backend-web = "my_backend_web_package"
```

`my_backend_web_package` must expose a top-level `register` function. See [Distribute via pip](/developer-guide/plugins#distribute-via-pip) in the general plugin guide for the full setup.

## Related pages

- [Web Search](/user-guide/features/web-search) — user-facing feature documentation and per-backend configuration
- [Plugins overview](/user-guide/features/plugins) — all plugin types at a glance
- [Build a Hermes Plugin](/developer-guide/plugins) — general tools/hooks/slash commands guide
