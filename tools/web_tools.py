#!/usr/bin/env python3
"""Generic web_search / web_extract tools over pluggable backends.

Backend is selected during ``hermes tools`` (``web.backend`` in config.yaml; per
capability via ``web.search_backend`` / ``web.extract_backend``). Every vendor
implementation lives in ``plugins/web/<vendor>/provider.py`` and registers with
``agent.web_search_registry``; this module owns selection, safety gates,
caching, keyless rescue, and the truncate-and-store result pipeline.
Debug: ``WEB_TOOLS_DEBUG=true`` writes ``logs/web_tools_debug_<UUID>.json``.
"""

import json
import logging
import os
from typing import List, Any, Optional
# Per-vendor client cache slots; plugins read/write these via tools.web_tools (tests reset them to None).
_firecrawl_client = _firecrawl_client_config = _parallel_client = _async_parallel_client = _exa_client = None

from plugins.web.firecrawl.provider import _is_tool_gateway_ready, check_firecrawl_api_key
from tools.debug_helpers import DebugSession
from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER, selection_exists
from tools.url_safety import async_is_safe_url
from plugins.web.keyless_mcp import web_interruptible, web_is_interrupted, is_extract_refusal
from tools.web_tools_provenance import (
    _build_extract_provenance, _extract_error_response, _inject_search_provenance,
    _search_web_count, _validate_search_provider_response,
)
from tools.web_tools_rescue import _rescue_eligible, _rescue_search
from tools.web_tools_truncate import _effective_char_limit, _trim_results, _truncate_results, convert_base64_images_to_links
from tools.web_tools_extract import (
    _extract_safe_urls, _merge_in_order, _no_provider_error, _resolve_extract_provider, _result_entry,
    _strict_selection_error, _validate_extract_urls,
)

logger = logging.getLogger(__name__)


# ─── Backend Selection ────────────────────────────────────────────────────────

def _env_value(name: str) -> str:
    """Resolve ``name`` via the config-aware env layer (``hermes config set`` values), then process env.

    Mirrors the SearXNG provider's ``_searxng_url()`` so that values set through Hermes' config/.env layer
    (``hermes config set``, ``hermes tools``) are honored here too — not just raw process-env exports.
    Without this, a config-only ``SEARXNG_URL`` (or any provider key) leaves the backend auto-detect cascade
    and ``check_web_api_key()`` blind to it. See #34290.
    """
    try:
        from hermes_cli.config import get_env_value
        val = get_env_value(name)
    except Exception:
        val = None
    return ((os.getenv(name, "") if val is None else val) or "").strip()


def _has_env(name: str) -> bool:
    return bool(_env_value(name))


def _load_web_config() -> dict:
    """Load the ``web:`` section from config.yaml; always a dict (a null section yields ``{}``)."""
    try:
        from hermes_cli.config import load_config
        return load_config().get("web") or {}
    except Exception:
        return {}


def _configured_backend(key: str = "backend") -> str:
    """Lower-cased, stripped ``web.<key>`` value ("" when unset/null)."""
    return (_load_web_config().get(key) or "").lower().strip()


def _registry_call(func_name: str, default, *args):
    """``agent.web_search_registry.<func_name>(*args)``, or *default* if it raised (registry never fatal)."""
    try:
        import agent.web_search_registry as registry_mod
        return getattr(registry_mod, func_name)(*args)
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry %s%r failed: %s", func_name, args, exc)
        return default


def _registered_web_provider(backend: str):
    """Plugin-registered web provider by name, or ``None``."""
    return _registry_call("get_provider", None, backend) if backend else None


def _list_registered_web_providers():
    """All plugin-registered web providers (empty list on failure)."""
    return _registry_call("list_providers", [])


def _probe(provider, method: str, context: str = "") -> Optional[bool]:
    """``bool(provider.<method>())``, or ``None`` if it raised (a broken provider is unavailable; *context* is
    appended to the debug log line, e.g. " during readiness check")."""
    try:
        return bool(getattr(provider, method)())
    except Exception as exc:  # noqa: BLE001 — a broken provider is "unavailable"
        name = getattr(provider, "name", provider)
        logger.debug("web provider %r.%s() raised%s: %s", name, method, context, exc)
        return None


def _get_backend() -> str:
    """Shared web backend name. A stored ``web.backend`` is returned as-is — no availability probe, no
    fallback — so a broken selection surfaces the vendor's honest error rather than silently rerouting.
    Autodetect runs ONLY when no web selection has ever been stored."""
    configured = _configured_backend()
    if configured:
        # "nous" (managed subscription) is serviced by firecrawl, routed through the managed Tool Gateway.
        return "firecrawl" if configured == NOUS_MANAGED_PROVIDER else configured
    if selection_exists("web"):
        # Selection exists (use_gateway / per-capability keys) but no shared name: firecrawl, no ladder.
        return "firecrawl"

    # Never-configured install. Explicit user credentials beat the managed-gateway probe (a Nous OAuth
    # token's tier may not grant web access; the gateway then fails at runtime with no fallback).
    # Free tiers trail paid.
    backend_candidates = (
        ("tavily", _has_env("TAVILY_API_KEY")), ("perplexity", _has_env("PERPLEXITY_API_KEY")),
        ("exa", _has_env("EXA_API_KEY")),
        ("parallel", _has_env("PARALLEL_API_KEY")), ("keenable", _has_env("KEENABLE_API_KEY")),
        ("firecrawl", _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")),
        ("firecrawl", _is_tool_gateway_ready()), ("searxng", _has_env("SEARXNG_URL")),
        ("brave-free", _has_env("BRAVE_SEARCH_API_KEY")), ("ddgs", _ddgs_package_importable()),
    )
    for backend, available in backend_candidates:
        if available:
            return backend

    # Plugin-contributed providers (built-ins are covered above); probe the held object directly.
    for provider in _list_registered_web_providers():
        if provider.name not in _LEGACY_WEB_BACKENDS and _probe(provider, "is_available"):
            return provider.name

    # Keyless free tier — strictly last so it never pre-empts a keyed backend. Discovery must run
    # first: reachable from contexts that haven't loaded plugins (subprocess runs, delegate children).
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import _keyless_preference, _keyless_tier_enabled
        if _keyless_tier_enabled():
            for name in _keyless_preference():
                provider = _registered_web_provider(name)
                if provider is not None and _probe(provider, "is_keyless_available"):
                    return name
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("keyless fallback walk failed: %s", exc)

    return "firecrawl"  # default (backward compat)


def _has_explicit_capability_backend(capability: str) -> bool:
    """Whether this capability has a direct or shared backend selection.

    Per-capability selection must not be inferred from another capability's
    key: ``web.extract_backend`` alone does not make an autodetected search
    candidate explicit. This distinction lets a never-configured capability
    continue through the registry's capability-aware autodetect walk.
    """
    cfg = _load_web_config()
    return bool(
        str(cfg.get(f"{capability}_backend") or "").strip()
        or str(cfg.get("backend") or "").strip()
    )



def _get_search_backend() -> str:
    """Backend for web_search: ``web.search_backend`` (strict, no probe) > ``web.backend`` > autodetect."""
    return _configured_backend("search_backend") or _get_backend()


def _get_extract_backend() -> str:
    """Backend for web_extract: ``web.extract_backend`` (strict, no probe) > ``web.backend`` > autodetect."""
    return _configured_backend("extract_backend") or _get_backend()


def _ddgs_package_importable() -> bool:
    """ddgs is the only backend gated on package presence; single symbol so tests can patch it."""
    try:
        import ddgs  # noqa: F401
        return True
    except ImportError:
        return False


def _xai_available() -> bool:
    # Cheap probe only (env var OR auth.json OAuth): resolve_xai_http_credentials() may hit the network.
    try:
        from tools.xai_http import has_xai_credentials
        return has_xai_credentials()
    except Exception:
        return False


# Built-in backends -> cheap availability probes; any other name is a plugin provider resolved via the
# registry's ``is_available()``. Lambdas so test patches of module-level helpers (_ddgs_package_importable,
# check_firecrawl_api_key) are honored at call time. ``xai`` is probed via has_xai_credentials(), not a
# registered provider, though the registry's _LEGACY_PREFERENCE omits it — drop it if xai ever registers.
_BUILTIN_AVAILABILITY = {
    "exa": lambda: _has_env("EXA_API_KEY"),
    "parallel": lambda: _has_env("PARALLEL_API_KEY"),
    "keenable": lambda: _has_env("KEENABLE_API_KEY"),
    "firecrawl": lambda: check_firecrawl_api_key(),
    "tavily": lambda: _has_env("TAVILY_API_KEY")
    or any(_configured_backend(k) == "tavily" for k in ("backend", "search_backend", "extract_backend")),
    "perplexity": lambda: _has_env("PERPLEXITY_API_KEY"),
    "searxng": lambda: _has_env("SEARXNG_URL"),
    "brave-free": lambda: _has_env("BRAVE_SEARCH_API_KEY"),
    "ddgs": lambda: _ddgs_package_importable(),
    "xai": _xai_available,
}
_LEGACY_WEB_BACKENDS = frozenset(_BUILTIN_AVAILABILITY)


def _is_backend_available(backend: str) -> bool:
    """True when *backend* is usable — the single availability chokepoint. Non-legacy names delegate to the
    registered provider's ``is_available()`` (unregistered names fall through); built-ins use cheap probes.

    For plugin-registered backends (any name outside :data:`_LEGACY_WEB_BACKENDS`), availability is
    delegated to the provider's ``is_available()`` via the web_search_registry. This is the single
    chokepoint through which ``_get_backend``, ``_get_capability_backend``, and ``check_web_api_key`` all
    resolve availability — fixing custom-provider discovery for every caller at once (issues #28651, #31873,
    #32698). Built-in backends keep their cheap hardcoded probes below.
    """
    backend = (backend or "").lower().strip()
    provider = None if backend in _LEGACY_WEB_BACKENDS else _registered_web_provider(backend)
    if provider is not None:
        return _probe(provider, "is_available") or False
    probe = _BUILTIN_AVAILABILITY.get(backend)
    return probe() if probe else False


# ─── Firecrawl Client ──────────────────────────────────────────────────────── After PR #25182, the
# firecrawl client, lazy SDK proxy, dual-auth config resolution, response normalizers, and
# check_firecrawl_api_key() all live in plugins.web.firecrawl.provider.
def _web_requires_env() -> list[str]:
    """Tool-registry metadata env vars for the web backends. Gateway vars are always listed: gating them
    on ``managed_nous_tools_enabled()`` cost a synchronous portal HTTP refresh at every CLI startup.
    Contract: set var -> tool sees it; extras are harmless for the not-logged-in."""
    return [
        "EXA_API_KEY", "PARALLEL_API_KEY", "TAVILY_API_KEY", "PERPLEXITY_API_KEY", "KEENABLE_API_KEY", "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL", "TOOL_GATEWAY_DOMAIN", "TOOL_GATEWAY_SCHEME",
        "TOOL_GATEWAY_USER_TOKEN",
    ]

_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


# ─── Dispatch ─────────────────────────────────────────────────────────────────

# ─── Exa / Parallel inline helpers — moved into plugins ────────────────────── After PR #25182, the exa
# client + search/extract and parallel client + search/extract helpers all live in their respective plugins:
# - plugins/web/exa/provider.py - plugins/web/parallel/provider.py Both plugins register through
# agent.web_search_registry and the dispatchers in this file resolve them via get_active_*_provider().
def _ensure_web_plugins_loaded() -> None:
    """Idempotently run plugin discovery so the web registry is populated. Dispatch is reachable from contexts
    that never triggered discovery (subprocess agent runs, delegate children, scripts); without it a
    configured backend yields a misleading "No web ... provider" error.

    Every bundled web provider (brave-free, ddgs, searxng, exa, parallel, tavily, firecrawl, keenable)
    registers itself via ``plugins/web/<vendor>/__init__.py`` during plugin discovery. Tool dispatch can be
    reached from contexts that haven't already triggered discovery — subprocess agent runs, delegate
    children, standalone scripts, certain test paths — and without it the registry is empty and
    ``get_provider('firecrawl')`` returns ``None`` even when the user has ``web.extract_backend: firecrawl``
    configured and ``FIRECRAWL_API_KEY`` set. See #27580.
    """
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
    except Exception as exc:  # noqa: BLE001
        # Warning, not debug: a broken plugin import is otherwise invisible.
        logger.warning("Web plugin discovery failed (non-fatal): %s", exc)


def _finish_debug(call_name: str, debug_call_data: dict, error_msg: Optional[str] = None) -> Optional[str]:
    """Log the call into the debug session; with *error_msg*, record it and return its ``tool_error`` envelope."""
    if error_msg is not None:
        logger.debug("%s", error_msg)
        debug_call_data["error"] = error_msg
    _debug.log_call(call_name, debug_call_data)
    _debug.save()
    return None if error_msg is None else tool_error(error_msg)


@web_interruptible
def web_search_tool(query: str, limit: int = 5) -> str:
    """Search the web via the configured backend.

    Returns a JSON string ``{"success": bool, "data": {"web": [{"title", "url", "description", "position"},
    ...]}}`` (metadata only — use web_extract_tool for page content) or ``{"success": false, "error": ...}``.
    """
    try:
        limit = min(max(int(limit), 1), 100)
    except (TypeError, ValueError):
        limit = 5
    debug_call_data = {
        "parameters": {"query": query, "limit": limit}, "error": None, "results_count": 0,
        "original_response_size": 0, "final_response_size": 0,
    }

    try:
        if web_is_interrupted():
            return tool_error("Interrupted", success=False)
        # Sync only — every provider's search() is sync.
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import get_active_search_provider, get_provider as _wsp_get_provider
        backend = _get_search_backend()
        provider = _wsp_get_provider(backend) if backend else None
        if provider is None or not provider.supports_search():
            if provider is not None and _has_explicit_capability_backend("search"):
                error = f"{provider.display_name} does not support web search. Set web.search_backend to a search-capable provider."
                _finish_debug("web_search_tool", debug_call_data, error)
                return tool_error(error, success=False)
            if provider is None and backend and selection_exists("web"):
                error_text = debug_call_data["error"] = _strict_selection_error("search", backend)
                _finish_debug("web_search_tool", debug_call_data)
                return json.dumps({"success": False, "error": error_text}, indent=2, ensure_ascii=False)
            # Never-configured install: legacy availability-walked autodetect.
            provider = get_active_search_provider()

        if provider is None:
            fallback = "No web search provider configured. Run `hermes tools` to set one up."
            response_data = {"success": False, "error": _no_provider_error("search", fallback)}
        else:
            logger.info("Web search via %s: '%s' (limit: %d)", provider.name, query, limit)
            response_data = _memoized_search(provider, query, limit)

        debug_call_data["results_count"] = _search_web_count(response_data)
        result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
        debug_call_data["final_response_size"] = len(result_json)
        _finish_debug("web_search_tool", debug_call_data)
        return result_json
    except Exception as e:
        return _finish_debug("web_search_tool", debug_call_data, f"Error searching web: {str(e)}")


def _memoized_search(provider, query: str, limit: int) -> dict:
    """Single-flight cache with actual retrieval time and non-sticky fallback provenance."""
    from tools.web_result_cache import (
        bucket_limit as _bucket_limit,
        cache_enabled as _search_cache_enabled,
        search_memo as _search_memo,
        slice_search_response as _slice_search_response,
        utc_now_iso as _utc_now_iso,
    )

    def _paid_search() -> tuple[dict, bool, str]:
        _fetch_limit = _bucket_limit(limit)
        _rescued = False
        if web_is_interrupted():
            return {"success": False, "error": "Interrupted"}, False, ""
        try:
            _resp = provider.search(query, _fetch_limit)
        except Exception as exc:  # noqa: BLE001 — candidate for rescue
            if web_is_interrupted():
                _resp = {"success": False, "error": "Interrupted"}
            elif not is_extract_refusal({"error": str(exc)}) and _rescue_eligible(provider):
                _rescued = True
                _resp = _rescue_search(
                    provider.name, str(exc), query, _fetch_limit
                )
            else:
                raise
        else:
            if web_is_interrupted():
                _resp = {"success": False, "error": "Interrupted"}
            if (
                isinstance(_resp, dict)
                and _resp.get("success") is False
                and not is_extract_refusal(_resp)
                and _rescue_eligible(provider)
            ):
                # One-shot keyless rescue: THIS call rides the
                # free-tier ring; the next call attempts the chosen
                # backend again.
                _rescued = True
                _resp = _rescue_search(
                    provider.name,
                    str(_resp.get("error", "")),
                    query,
                    _fetch_limit,
                )
        return (
            _validate_search_provider_response(_resp),
            _rescued,
            _utc_now_iso(),
        )

    response_data = None
    _cache_metadata = None
    _cache_status = "bypass"
    _was_rescued = False
    _retrieved_at = ""
    if _search_cache_enabled():
        response_data, _cache_metadata = (
            _search_memo.lookup_with_metadata(
                provider.name, query, limit
            )
        )
        if response_data is not None:
            _cache_status = "hit"
        else:
            with _search_memo.flight_lock(provider.name, query, limit):
                # Re-check inside the lock: a concurrent identical call
                # may have stored while this one waited. That waiter is
                # truthfully a cache hit even though its first lookup
                # missed before the single-flight lock.
                response_data, _cache_metadata = (
                    _search_memo.lookup_with_metadata(
                        provider.name, query, limit
                    )
                )
                if response_data is not None:
                    _cache_status = "hit"
                else:
                    response_data, _was_rescued, _retrieved_at = (
                        _paid_search()
                    )
                    # Never cache a rescue-served response: it came
                    # from a ring vendor, not the chosen backend (wrong
                    # key), and caching it would make the one-shot
                    # rescue sticky for a whole TTL.
                    if not _was_rescued:
                        _cache_metadata = (
                            _search_memo.store_with_metadata(
                                provider.name,
                                query,
                                limit,
                                response_data,
                                retrieved_at=_retrieved_at,
                            )
                        )
                        _cache_status = (
                            "miss" if _cache_metadata else "bypass"
                        )
    else:
        response_data, _was_rescued, _retrieved_at = _paid_search()

    if _cache_status == "hit" and _cache_metadata:
        _retrieved_at = str(_cache_metadata["retrieved_at"])
    elif not _retrieved_at:
        _retrieved_at = _utc_now_iso()

    response_data = _validate_search_provider_response(response_data)
    _fetched_result_count = _search_web_count(response_data)
    response_data = _slice_search_response(response_data, limit)
    if response_data.get("success") is True:
        response_data = _inject_search_provenance(
            response_data,
            requested_backend=provider.name,
            requested_limit=limit,
            fetched_result_count=_fetched_result_count,
            retrieved_at=_retrieved_at,
            cache_status=_cache_status,
            cache_age_seconds=(
                _cache_metadata.get("age_seconds")
                if _cache_metadata
                else None
            ),
            cache_ttl_seconds=(
                _cache_metadata.get("ttl_seconds")
                if _cache_metadata
                else None
            ),
            fallback_used=_was_rescued,
        )
    return response_data


@web_interruptible
async def web_extract_tool(urls: List[Any], format: str = None, char_limit: Optional[int] = None) -> str:
    """Extract clean page content (no LLM) from URLs via the configured backend.

    Pages over ``char_limit`` (default web.extract_char_limit or 15000) are head+tail truncated with a footer
    pointing at the stored full text; inline base64 images become ``[IMAGE: alt]``. URLs carrying secrets are
    refused before any fetch; private-network URLs are blocked per entry. Returns JSON ``{"results": [...]}``.
    """
    facts = {
        "requested_count": len(urls), "requested_backend": None, "cache_status": "bypass",
        "provider_call_attempted": False, "fetch_succeeded": False, "network_retrieved_at": "",
        "fallback_attempted": False, "fallback_used": False,
    }
    if web_is_interrupted():
        return _extract_error_response("Interrupted", success_false=True, **facts)
    normalized_urls, normalized_indices, invalid_urls, blocked = _validate_extract_urls(urls)
    if blocked is not None:
        return blocked
    debug_call_data = {
        "parameters": {"urls": normalized_urls, "format": format, "char_limit": char_limit}, "error": None,
        "pages_extracted": 0, "pages_truncated": 0, "original_response_size": 0, "final_response_size": 0,
        "truncation_metrics": [], "processing_applied": [],
    }

    try:
        logger.info("Extracting content from %d URL(s)", len(normalized_urls))
        # SSRF protection — filter private/internal URLs before any backend.
        safe_urls, safe_indices, ssrf_blocked = [], [], {}
        for index, url in zip(normalized_indices, normalized_urls):
            if await async_is_safe_url(url):
                safe_urls.append(url)
                safe_indices.append(index)
            else:
                ssrf_blocked[index] = _result_entry(
                    url, "Blocked: URL targets a private or internal network address"
                )

        results = []
        if safe_urls:
            backend = _get_extract_backend()
            facts["requested_backend"] = backend or None
            _ensure_web_plugins_loaded()
            provider, error_json = _resolve_extract_provider(backend)
            if error_json is not None:
                return _extract_error_response(json.loads(error_json)["error"], success_false=True, **facts)
            facts["requested_backend"] = provider.name
            results = await _extract_safe_urls(provider, safe_urls, format, facts=facts)
        # Safe results were reconciled by URL; list position is not a provider identity.
        if invalid_urls or ssrf_blocked:
            fixed = {**ssrf_blocked, **invalid_urls}
            results = _merge_in_order(len(urls), fixed, safe_indices, safe_urls, results)

        logger.info("Extracted content from %d pages", len(results))
        debug_call_data["pages_extracted"] = len(results)
        debug_call_data["original_response_size"] = len(json.dumps({"results": results}))
        debug_call_data["processing_applied"].append("truncate_and_store")
        _truncate_results(results, _effective_char_limit(char_limit), debug_call_data)
        trimmed = _trim_results(results)
        result_json = (
            json.dumps({"provenance": _build_extract_provenance(results, **facts), "results": trimmed}, indent=2, ensure_ascii=False) if trimmed
            else _extract_error_response("Content was inaccessible or not found", **facts)
        )
        # Belt-and-suspenders sweep of the serialized JSON: a provider may tuck a base64 blob in metadata.
        cleaned_result = convert_base64_images_to_links(result_json)
        debug_call_data["final_response_size"] = len(cleaned_result)
        debug_call_data["processing_applied"].append("base64_image_conversion")
        _finish_debug("web_extract_tool", debug_call_data)
        return cleaned_result
    except Exception as e:
        error = f"Error extracting content: {str(e)}"
        _finish_debug("web_extract_tool", debug_call_data, error)
        return _extract_error_response(error, **facts)


def _provider_is_ready(provider) -> bool:
    """True when *provider* is keyed-available OR keyless-capable, without raising.

    ``get_active_*_provider()`` returns an explicitly configured backend even when ``is_available()`` is
    False (so dispatch can emit a precise error), so readiness gates (tool check_fn, ``hermes doctor``)
    must probe for real. Keyless mode (Exa/Parallel free tier) is a working state, not a misconfig.

    See #78412.
    """
    if provider is None:
        return False
    ready = _probe(provider, "is_available", " during readiness check")
    if ready is None:  # broken provider == not ready; don't try the keyless probe
        return False
    return bool(ready or _probe(provider, "is_keyless_available", " during readiness check"))


def check_web_api_key() -> bool:
    """``check_fn`` gate for web_search / web_extract: is any web backend available?

    A plugin-registered provider reporting ``is_available()`` must light the tools up even with no
    built-in credentials; resolution funnels through :func:`_is_backend_available`.

    See #28651, #31873.
    """
    # Boolean OR over configured + built-ins — probe order is irrelevant here.
    candidates = [c for c in (_configured_backend(),) if c] + list(_LEGACY_WEB_BACKENDS)
    if any(_is_backend_available(backend) for backend in candidates):
        return True
    # Plugin path. Discovery must run first: check_fn fires at tool-registration time, before any dispatch.
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import get_active_search_provider, get_active_extract_provider
        return _provider_is_ready(get_active_search_provider()) or _provider_is_ready(
            get_active_extract_provider()
        )
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry availability check failed: %s", exc)
        return False


# ─── Registry ─────────────────────────────────────────────────────────────────
from tools.registry import registry, tool_error

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": (
        "Search through the configured backend and return result metadata: "
        "titles, URLs, and upstream description snippets. This does not fetch "
        "result pages. Every successful response generated by this wrapper adds "
        "data.provenance with the "
        "requested and serving backends, fallback use, Hermes process-memory "
        "cache status and age, retrieval/serve times, evidence scope, result "
        "counts, transformations, and limitations. The search-cache key omits "
        "credential identity, locale, and provider configuration; "
        "result_set_truncated reports only Hermes' limit slice. Do not treat snippets as "
        "confidence, freshness, "
        "currentness, verification, or primary-source evidence. Trusted tool "
        "execution middleware or result-transform hooks can replace the wrapper "
        "output; Hermes logs when they change a wrapper success or make a "
        "non-success/short-circuit claim success. Use web_extract "
        "on relevant URLs and inspect the source. Returns up to 5 results by "
        "default. Backend-supported operators such as site:domain, filetype:pdf, "
        "intitle:word, -term, and \"exact phrase\" may work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or \"exact phrase\"."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5
            }
        },
        "required": ["query"]
    }
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract content from web page URLs. Returns clean page content in markdown/text (no LLM summarization — fast). Every normal or safety-error response begins with mandatory provenance naming the requested/serving backend, fallback attempt/use, cache state, whether a provider call was attempted, whether uncached content was fetched successfully, successful retrieval/serve times, and requested/returned/success/failure counts. Provenance never contains page URLs, content, or error text. Also works with PDF URLs (arxiv papers, documents) — pass the PDF link directly. Pages within the char budget (default 15000) return whole; larger pages return a head+tail window with a footer telling you the full text's saved file path and the read_file call to page through the omitted middle. Inline images appear as [IMAGE: alt] placeholders; real image URLs are kept as links. If a URL fails or times out, use the browser tool instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5
            },
            "char_limit": {
                "type": "integer",
                "description": "Optional per-page character budget sent back (default 15000). Pages larger than this are head+tail truncated with the full text stored to disk. Raise it when you need more of a long page inline.",
                "minimum": 2000
            }
        },
        "required": ["urls"]
    }
}

registry.register(
    name="web_search", toolset="web", schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=args.get("limit", 5)),
    check_fn=check_web_api_key, requires_env=_web_requires_env(), emoji="🔍",
    max_result_size_chars=100_000,
)
registry.register(
    name="web_extract", toolset="web", schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [], "markdown",
        char_limit=args.get("char_limit"),
    ),
    check_fn=check_web_api_key, requires_env=_web_requires_env(), is_async=True, emoji="📄",
    max_result_size_chars=100_000,
)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Dict  # noqa: F401,E402
from typing import TYPE_CHECKING  # noqa: F401,E402
import asyncio  # noqa: F401,E402
import httpx  # noqa: F401,E402
import re  # noqa: F401,E402
import sys  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'DEFAULT_EXTRACT_CHAR_LIMIT': ('tools.web_tools_truncate', 'DEFAULT_EXTRACT_CHAR_LIMIT'),
    'Firecrawl': ('plugins.web.firecrawl.provider', 'Firecrawl'),
    'MAX_STORED_TEXT_CHARS': ('tools.web_tools_truncate', 'MAX_STORED_TEXT_CHARS'),
    'build_vendor_gateway_url': ('tools.managed_tool_gateway', 'build_vendor_gateway_url'),
    'managed_nous_tools_enabled': ('tools.tool_backend_helpers', 'managed_nous_tools_enabled'),
    'normalize_url_for_request': ('tools.url_safety', 'normalize_url_for_request'),
    'nous_tool_gateway_unavailable_message': ('tools.tool_backend_helpers', 'nous_tool_gateway_unavailable_message'),
    'prefers_gateway': ('tools.tool_backend_helpers', 'prefers_gateway'),
    'resolve_managed_tool_gateway': ('tools.managed_tool_gateway', 'resolve_managed_tool_gateway'),
    'sensitive_query_param_name': ('tools.url_safety', 'sensitive_query_param_name'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
