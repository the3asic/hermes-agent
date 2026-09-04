#!/usr/bin/env python3
"""
Standalone Web Tools Module

This module provides generic web tools that work with multiple backend providers.
Backend is selected during ``hermes tools`` setup (web.backend in config.yaml).
When available, Hermes can route Firecrawl calls through a Nous-hosted tool-gateway
for Nous Subscribers only.

Available tools:
- web_search_tool: Search the web for information
- web_extract_tool: Extract content from specific web pages

Backend compatibility:
- Search + extract: Exa, Firecrawl, Parallel, Keenable
- Search only: Brave Search, DDGS, SearXNG, xAI

LLM Processing:
- Uses OpenRouter API with Gemini 3 Flash Preview for intelligent content extraction
- Extracts key excerpts and creates markdown summaries to reduce token usage

Debug Mode:
- Set WEB_TOOLS_DEBUG=true to enable detailed logging
- Creates web_tools_debug_UUID.json in ./logs directory
- Captures all tool calls, results, and compression metrics

Usage:
    from web_tools import web_search_tool, web_extract_tool
    
    # Search the web
    results = web_search_tool("Python machine learning libraries", limit=3)
    
    # Extract content from URLs  
    content = web_extract_tool(["https://example.com"], format="markdown")
"""

import json
import logging
import os
import re
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, TYPE_CHECKING
import httpx  # noqa: F401 — kept at module top so tests can patch tools.web_tools.httpx
# After the web-provider plugin migration (PR #25182), the Firecrawl SDK
# proxy, client construction, and response-shape normalizers all live in
# plugins.web.firecrawl.provider. We re-export the names that external
# code, integration tests, and unit-test patches reach for so the public
# surface stays stable.
if TYPE_CHECKING:
    from firecrawl import Firecrawl  # noqa: F401 — type hints only
from plugins.web.firecrawl.provider import (
    Firecrawl,  # noqa: F401  # re-exported for tests that mock.patch("tools.web_tools.Firecrawl")
    _firecrawl_backend_help_suffix,
    _get_firecrawl_client,  # noqa: F401  # re-exported for tests that `from tools.web_tools import _get_firecrawl_client`
    _get_firecrawl_gateway_url,
    _is_tool_gateway_ready,
    check_firecrawl_api_key,
)
# Parallel + Exa clients re-exported for backward-compat with existing
# unit tests (tests/tools/test_web_tools_config.py imports _get_parallel_client
# / _get_async_parallel_client / _get_exa_client directly).
from plugins.web.parallel.provider import (  # noqa: F401 — backward-compat names
    _get_async_parallel_client,
    _get_parallel_client,
)
from plugins.web.exa.provider import _get_exa_client  # noqa: F401

# Module-level cache slots for the per-vendor clients. The plugins read/write
# these via tools.web_tools so unit tests that reset
# ``tools.web_tools._<vendor>_client = None`` between cases keep working.
_firecrawl_client: Optional[Any] = None
_firecrawl_client_config: Optional[Any] = None
_parallel_client: Optional[Any] = None
_async_parallel_client: Optional[Any] = None
_exa_client: Optional[Any] = None

from tools.debug_helpers import DebugSession
# Imported solely so unit tests can monkeypatch these names on
# tools.web_tools (the firecrawl plugin reads them via its own import chain).
from tools.managed_tool_gateway import (  # noqa: F401 — backward-compat names for tests
    build_vendor_gateway_url,
    peek_nous_access_token as _peek_nous_access_token,
    read_nous_access_token as _read_nous_access_token,
    resolve_managed_tool_gateway,
)
from tools.tool_backend_helpers import (  # noqa: F401
    managed_nous_tools_enabled,
    nous_tool_gateway_unavailable_message,
    prefers_gateway,
)
from tools.url_safety import async_is_safe_url, normalize_url_for_request, sensitive_query_param_name
import sys

logger = logging.getLogger(__name__)


def _web_extract_url(value: Any) -> Optional[str]:
    """Return a usable URL from a model-supplied extract item.

    Models sometimes forward a complete web-search result instead of its URL.
    Accept the two common URL keys, but reject missing/non-string values rather
    than stringifying arbitrary objects into misleading fetch targets.
    """
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _extract_url_identity(url: str) -> str:
    """Return a deterministic HTTP URL identity for extract result matching.

    The identity normalizes scheme/host case, IDNs, an empty root path,
    default ports, and fragments. Path and query semantics remain untouched.
    It is used only to associate a provider row with the URL Hermes requested;
    the caller-visible row is rewritten to that original normalized URL.
    """
    from urllib.parse import urlsplit, urlunsplit

    normalized = normalize_url_for_request(url)
    try:
        parsed = urlsplit(normalized)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()
        if not scheme or not hostname:
            return normalized

        userinfo = ""
        if "@" in parsed.netloc:
            userinfo = parsed.netloc.rsplit("@", 1)[0] + "@"
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = parsed.port
        if port is not None and not (
            (scheme == "http" and port == 80)
            or (scheme == "https" and port == 443)
        ):
            host = f"{host}:{port}"
        return urlunsplit(
            (scheme, f"{userinfo}{host}", parsed.path or "/", parsed.query, "")
        )
    except (TypeError, ValueError):
        return normalized


_EXTRACT_RESULT_MAPPING_ERROR = (
    "Extract backend returned duplicate, unexpected, or ambiguous URL rows; "
    "no provider content was accepted for this batch"
)
_EXTRACT_RESULT_MISSING_ERROR = "Extract backend returned no result for this URL"


def _reconcile_extract_results(
    requested_urls: List[str],
    raw_results: Any,
) -> tuple[list, Dict[str, Dict[str, Any]], bool]:
    """Match provider rows to requested URLs without relying on list position.

    Missing URLs receive explicit error rows. Any non-object, URL-less,
    duplicate, unexpected, or ambiguously identified provider row fails the
    entire batch closed. The returned mapping is keyed by canonical request
    identity and is the only source used by cache writes and mixed-result
    assembly.
    """
    from plugins.web.keyless_mcp import ExtractFailoverResults

    fallback_attempted = bool(
        getattr(raw_results, "fallback_attempted", False)
    )
    fallback_used = bool(getattr(raw_results, "fallback_used", False))
    requested_by_identity: Dict[str, str] = {}
    duplicate_request = False
    for requested_url in requested_urls:
        identity = _extract_url_identity(requested_url)
        if identity in requested_by_identity:
            duplicate_request = True
        else:
            requested_by_identity[identity] = requested_url

    rows = list(raw_results) if isinstance(raw_results, (list, tuple)) else []
    matched: Dict[str, Dict[str, Any]] = {}
    invalid_mapping = duplicate_request or not isinstance(
        raw_results, (list, tuple)
    )
    for raw_result in rows:
        if not isinstance(raw_result, dict):
            invalid_mapping = True
            continue
        candidates = []
        result_url = raw_result.get("url")
        if isinstance(result_url, str) and result_url.strip():
            candidates.append(_extract_url_identity(result_url))
        metadata = raw_result.get("metadata")
        if isinstance(metadata, dict):
            source_url = metadata.get("sourceURL")
            if isinstance(source_url, str) and source_url.strip():
                candidates.append(_extract_url_identity(source_url))
        candidate_identities = set(candidates)
        if len(candidate_identities) != 1:
            invalid_mapping = True
            continue
        identity = next(iter(candidate_identities))
        if identity not in requested_by_identity or identity in matched:
            invalid_mapping = True
            continue
        accepted = dict(raw_result)
        accepted["url"] = requested_by_identity[identity]
        matched[identity] = accepted

    if invalid_mapping:
        matched = {
            identity: {
                "url": requested_url,
                "title": "",
                "content": "",
                "error": _EXTRACT_RESULT_MAPPING_ERROR,
            }
            for identity, requested_url in requested_by_identity.items()
        }
        ordered = [
            matched[_extract_url_identity(url)] for url in requested_urls
        ]
        return (
            ExtractFailoverResults(
                ordered,
                fallback_attempted=fallback_attempted,
                fallback_used=False,
            ),
            matched,
            False,
        )

    for identity, requested_url in requested_by_identity.items():
        if identity not in matched:
            matched[identity] = {
                "url": requested_url,
                "title": "",
                "content": "",
                "error": _EXTRACT_RESULT_MISSING_ERROR,
            }
    ordered = [matched[_extract_url_identity(url)] for url in requested_urls]
    fallback_used = fallback_used and any(
        not result.get("error") for result in ordered
    )
    return (
        ExtractFailoverResults(
            ordered,
            fallback_attempted=fallback_attempted,
            fallback_used=fallback_used,
        ),
        matched,
        True,
    )


# ─── Backend Selection ────────────────────────────────────────────────────────

def _env_value(name: str) -> str:
    """Resolve ``name`` via Hermes config-aware env, falling back to process env.

    Mirrors the SearXNG provider's ``_searxng_url()`` so that values set
    through Hermes' config/.env layer (``hermes config set``, ``hermes tools``)
    are honored here too — not just raw process-env exports. Without this,
    a config-only ``SEARXNG_URL`` (or any provider key) leaves the backend
    auto-detect cascade and ``check_web_api_key()`` blind to it. See #34290.
    """
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value(name)
    except Exception:
        val = None
    if val is None:
        val = os.getenv(name, "")
    return (val or "").strip()


def _has_env(name: str) -> bool:
    return bool(_env_value(name))

def _load_web_config() -> dict:
    """Load the ``web:`` section from ~/.hermes/config.yaml."""
    try:
        from hermes_cli.config import load_config
        # ``or {}``: a present-but-null ``web:`` section (YAML ``web:`` with no
        # body) makes ``.get("web", {})`` return None, which would break every
        # caller that does ``_load_web_config().get(...)``. Honor the ``-> dict``
        # contract so callers never see None.
        return load_config().get("web") or {}
    except (ImportError, Exception):
        return {}


# The built-in web backends whose availability is driven by hardcoded
# env-var / package / OAuth probes below. Any name NOT in this set is a
# candidate plugin-registered provider and must be resolved through the
# web_search_registry (``is_available()``) instead. Kept as a single named
# constant so the whitelist early-returns and the availability chokepoint
# stay in sync.
#
# NOTE: this intentionally includes ``xai``, which the registry's
# ``_LEGACY_PREFERENCE`` does NOT — xai availability is probed via
# ``has_xai_credentials()`` (env var OR auth.json OAuth), not a registered
# WebSearchProvider. Keep the two sets aligned by hand: if xai ever ships as
# a registered provider, drop it here so the registry path takes over.
_LEGACY_WEB_BACKENDS = frozenset(
    {"parallel", "firecrawl", "exa", "searxng", "brave-free", "ddgs", "xai", "keenable"}
)


def _registered_web_provider(backend: str):
    """Return a plugin-registered web provider by name, or ``None``.

    Consults ``agent.web_search_registry`` so backends contributed by the
    plugin system (which are absent from :data:`_LEGACY_WEB_BACKENDS`) are
    discoverable during availability/selection resolution. Returns ``None``
    on any lookup failure so callers can fall through to legacy checks.
    """
    if not backend:
        return None
    try:
        from agent.web_search_registry import get_provider

        return get_provider(backend)
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry lookup failed for %r: %s", backend, exc)
        return None


def _registered_web_provider_available(backend: str):
    """Availability of a *registered* web provider, or ``None`` if unregistered.

    Returns ``True``/``False`` when *backend* names a registered provider
    (calling its ``is_available()``), or ``None`` when it isn't registered —
    letting the caller fall through to the legacy built-in probes.
    """
    provider = _registered_web_provider(backend)
    if provider is None:
        return None
    try:
        return bool(provider.is_available())
    except Exception as exc:  # noqa: BLE001 — a broken provider is "unavailable"
        logger.debug("web provider %r.is_available() raised: %s", backend, exc)
        return False


def _list_registered_web_providers():
    """Return all plugin-registered web providers (empty list on failure)."""
    try:
        from agent.web_search_registry import list_providers

        return list_providers()
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry list failed: %s", exc)
        return []


def _get_backend() -> str:
    """Determine which web backend to use (shared fallback).

    Reads ``web.backend`` from config.yaml (set by ``hermes tools``). A
    stored backend name is returned as-is — no availability probe, no
    fallback — so the vendor path can raise its own honest error when the
    selection is broken. The credential/entitlement autodetect ladder runs
    ONLY when no web selection has ever been stored.
    """
    configured = (_load_web_config().get("backend") or "").lower().strip()
    if configured:
        # Strict: the stored selection is final, known name or not — an
        # unknown/typoed name surfaces as the vendor path's honest error
        # rather than silently rerouting through the credential ladder.
        # The managed "Nous Subscription" selection ("nous") is serviced by
        # the firecrawl provider, whose client resolver routes it through
        # the managed Tool Gateway.
        from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER

        if configured == NOUS_MANAGED_PROVIDER:
            return "firecrawl"
        return configured

    from tools.tool_backend_helpers import selection_exists

    if selection_exists("web"):
        # A web selection exists (e.g. use_gateway key or per-capability
        # backends) but the shared backend name is empty — keep the
        # firecrawl default rather than credential-laddering.
        return "firecrawl"

    # Never-configured install — pick the highest-priority available
    # backend. Explicit user credentials (EXA_API_KEY etc.)
    # beat the managed-tool-gateway probe so a deliberate setup is not
    # pre-empted by a Nous OAuth token whose subscription tier may not
    # actually grant web-search access (the gateway then fails at runtime
    # with "no subscription" and the tool returns an error to the agent
    # without falling back). Free-tier backends trail the paid ones.
    backend_candidates = (
        ("exa", _has_env("EXA_API_KEY")),
        ("parallel", _has_env("PARALLEL_API_KEY")),
        ("keenable", _has_env("KEENABLE_API_KEY")),
        ("firecrawl", _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")),
        ("firecrawl", _is_tool_gateway_ready()),
        ("searxng", _has_env("SEARXNG_URL")),
        ("brave-free", _has_env("BRAVE_SEARCH_API_KEY")),
        ("ddgs", _ddgs_package_importable()),
    )
    for backend, available in backend_candidates:
        if available:
            return backend

    # Final fallback: walk plugin-registered providers so a custom backend
    # (with no built-in creds present) still resolves. Built-in names are
    # already covered above, so this only surfaces plugin-contributed
    # providers via their own is_available() gate. We hold the provider
    # object already, so probe it directly rather than round-tripping through
    # _is_backend_available() (which would re-do the registry lookup).
    for provider in _list_registered_web_providers():
        if provider.name in _LEGACY_WEB_BACKENDS:
            continue
        try:
            if provider.is_available():
                return provider.name
        except Exception as exc:  # noqa: BLE001 — a broken provider is skipped
            logger.debug("web provider %r.is_available() raised: %s", provider.name, exc)

    # Keyless free-tier walk — zero credentials anywhere. Providers with a
    # public anonymous endpoint (Parallel, Exa — see
    # plugins/web/keyless_mcp.py) can still serve, unless the user disabled
    # the tier via ``web.keyless_fallback: false``. Strictly last so it
    # never pre-empts any keyed/importable backend above. Discovery must
    # run first — this path is reachable from contexts that haven't loaded
    # plugins yet (subprocess agent runs, delegate children, scripts).
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import _keyless_preference, _keyless_tier_enabled

        if _keyless_tier_enabled():
            for name in _keyless_preference():
                provider = _registered_web_provider(name)
                if provider is None:
                    continue
                try:
                    if provider.is_keyless_available():
                        return name
                except Exception as exc:  # noqa: BLE001 — skip broken provider
                    logger.debug(
                        "web provider %r.is_keyless_available() raised: %s", name, exc
                    )
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("keyless fallback walk failed: %s", exc)

    return "firecrawl"  # default (backward compat)


def _get_search_backend() -> str:
    """Determine which backend to use for web_search specifically.

    Selection priority:
    1. ``web.search_backend`` (per-capability override)
    2. ``web.backend`` (shared fallback — existing behavior)
    3. Auto-detect from env vars

    This enables using different providers for search vs extract
    (e.g. SearXNG for search + Firecrawl for extract).
    """
    return _get_capability_backend("search")


def _get_extract_backend() -> str:
    """Determine which backend to use for web_extract specifically.

    Selection priority:
    1. ``web.extract_backend`` (per-capability override)
    2. ``web.backend`` (shared fallback — existing behavior)
    3. Auto-detect from env vars
    """
    return _get_capability_backend("extract")


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


def _get_capability_backend(capability: str) -> str:
    """Shared helper for per-capability backend selection.

    Reads ``web.{capability}_backend`` from config; a stored value is
    returned unconditionally (strict selection — no availability probe).
    A selected-but-broken backend surfaces the vendor path's honest error
    instead of being silently replaced by whatever the credential ladder
    finds. Falls through to the shared ``_get_backend()`` only when no
    per-capability override is stored.
    """
    cfg = _load_web_config()
    specific = (cfg.get(f"{capability}_backend") or "").lower().strip()
    if specific:
        return specific
    return _get_backend()



def _is_backend_available(backend: str) -> bool:
    """Return True when the selected backend is currently usable.

    For plugin-registered backends (any name outside
    :data:`_LEGACY_WEB_BACKENDS`), availability is delegated to the
    provider's ``is_available()`` via the web_search_registry. This is the
    single chokepoint through which ``_get_backend``,
    ``_get_capability_backend``, and ``check_web_api_key`` all resolve
    availability — fixing custom-provider discovery for every caller at once
    (issues #28651, #31873, #32698). Built-in backends keep their cheap
    hardcoded probes below.
    """
    backend = (backend or "").lower().strip()
    if backend not in _LEGACY_WEB_BACKENDS:
        registered = _registered_web_provider_available(backend)
        if registered is not None:
            return registered
    if backend == "exa":
        return _has_env("EXA_API_KEY")
    if backend == "parallel":
        return _has_env("PARALLEL_API_KEY")
    if backend == "keenable":
        return _has_env("KEENABLE_API_KEY")
    if backend == "firecrawl":
        return check_firecrawl_api_key()
    if backend == "searxng":
        return _has_env("SEARXNG_URL")
    if backend == "brave-free":
        return _has_env("BRAVE_SEARCH_API_KEY")
    if backend == "ddgs":
        return _ddgs_package_importable()
    if backend == "xai":
        # Cheap probe — env var OR auth.json has OAuth tokens. Must not
        # call resolve_xai_http_credentials() here because the OAuth path
        # can trigger a network token refresh, and _is_backend_available
        # runs on every web_search dispatch + every `hermes tools` repaint.
        try:
            from tools.xai_http import has_xai_credentials
            return has_xai_credentials()
        except Exception:
            return False
    return False


def _ddgs_package_importable() -> bool:
    """Return True when the ``ddgs`` Python package can be imported.

    ddgs is the only backend whose availability is driven by a package
    presence rather than an env var / config entry.  Wrapped in a helper
    so auto-detect and ``_is_backend_available`` share the same check
    (and tests can monkeypatch a single symbol).
    """
    try:
        import ddgs  # noqa: F401
        return True
    except ImportError:
        return False



# ─── One-shot keyless rescue (keyed/configured backend failed) ───────────────

def _keyless_rescue_enabled() -> bool:
    """Read ``web.keyless_rescue`` from config (default: enabled).

    Also implicitly off whenever the keyless tier itself is disabled
    (``web.keyless_fallback: false``).
    """
    cfg = _load_web_config()
    if not cfg.get("keyless_rescue", True):
        return False
    try:
        from agent.web_search_registry import _keyless_tier_enabled

        return _keyless_tier_enabled()
    except Exception as exc:  # noqa: BLE001 — registry optional
        logger.debug("keyless rescue tier check failed: %s", exc)
        return False


def _rescue_eligible(provider) -> bool:
    """True when a failed call on *provider* should get a one-shot rescue.

    Eligible: the call ran a keyed/configured path — either a non-ring
    backend (searxng, brave-free, xai, custom plugins, managed gateway) or
    a ring vendor operating in keyed mode. NOT eligible: the call already
    went through the keyless ring (its failure means the ring was walked;
    re-walking would just repeat it).
    """
    if not _keyless_rescue_enabled():
        return False
    if provider is None:
        return False
    try:
        from plugins.web.keyless_mcp import _KEYLESS_RING, use_keyless

        name = getattr(provider, "name", "")
        if name in _KEYLESS_RING:
            key_var = {
                "exa": "EXA_API_KEY",
                "parallel": "PARALLEL_API_KEY",
                "firecrawl": "FIRECRAWL_API_KEY",
                "keenable": "KEENABLE_API_KEY",
            }.get(name, "")
            from agent.web_search_provider import get_provider_env

            api_key = get_provider_env(key_var) if key_var else ""
            # Keyless-mode ring vendors already walked the ring on failure.
            return not use_keyless(name, api_key)
        return True
    except Exception as exc:  # noqa: BLE001 — rescue is best-effort
        logger.debug("rescue eligibility check failed: %s", exc)
        return False


def _rescue_search(provider_name: str, original_error: str, query: str, limit: int) -> dict:
    """One-shot keyless-ring rescue for a failed keyed/configured search.

    Stateless by design: this call alone routes to the free-tier ring; the
    NEXT web_search call attempts the chosen backend again. The result is
    annotated with the original backend failure so the model (and the
    user) can see the configured backend needs attention.
    """
    from plugins.web.keyless_mcp import search_with_failover

    logger.warning(
        "web_search backend '%s' failed (%s); one-shot keyless rescue",
        provider_name, (original_error or "")[:200],
    )
    rescued = search_with_failover(provider_name, query, limit)
    if rescued.get("success"):
        data = rescued.setdefault("data", {})
        data["rescued_from"] = provider_name
        data["backend_error"] = (
            f"Configured backend '{provider_name}' failed this call "
            f"({(original_error or 'unknown error')[:300]}); result served "
            "by the keyless free tier. The next call will use "
            f"'{provider_name}' again."
        )
        return rescued
    # Ring also failed: surface the ORIGINAL backend error (it names the
    # user's configured setup) with the rescue note appended.
    return {
        "success": False,
        "error": (
            f"{original_error or 'search failed'} "
            f"(keyless rescue also failed: {rescued.get('error', 'unknown')})"
        ),
    }


def _policy_blocked_result(result: dict) -> bool:
    """True when an extract result failed because of the user's website
    policy — an intentional refusal, never a backend outage. Policy blocks
    must NOT be rescued: routing the same URL through the keyless ring
    would fetch content the user explicitly blocked."""
    if result.get("blocked_by_policy"):
        return True
    return "blocked by website policy" in str(result.get("error") or "").lower()


def _rescue_extract(provider_name: str, urls: list, results: list) -> list:
    """One-shot keyless-ring rescue for a failed keyed/configured extract.

    Fires only when EVERY url failed (whole-backend failure); partial
    results are page problems and pass through untouched. Stateless —
    the next web_extract call attempts the chosen backend again.

    Website-policy refusals are intentional, not failures: entries flagged
    by ``_policy_blocked_result`` are never re-fetched through the ring and
    their original (blocked) results are preserved verbatim.
    """
    from plugins.web.keyless_mcp import ExtractFailoverResults, extract_with_failover

    aligned, original_by_identity, mapping_valid = _reconcile_extract_results(
        urls, results
    )
    if not mapping_valid:
        # An invalid provider mapping is an integrity failure, not an outage.
        # Never route it elsewhere and risk accepting content for the wrong URL.
        return aligned

    # Partition by canonical request identity. Rescue only genuine backend
    # failures; website-policy refusals remain untouched.
    rescue_urls = [
        url
        for url in urls
        if not _policy_blocked_result(
            original_by_identity[_extract_url_identity(url)]
        )
    ]
    if not rescue_urls:
        # Every failure is an intentional policy block.  Preserve the list
        # contract while explicitly recording that no fallback call occurred.
        return ExtractFailoverResults(
            aligned,
            fallback_attempted=False,
            fallback_used=False,
        )

    original_error = next(
        (
            original_by_identity[_extract_url_identity(url)].get("error")
            for url in rescue_urls
            if original_by_identity[_extract_url_identity(url)].get("error")
        ),
        "extract failed",
    )
    logger.warning(
        "web_extract backend '%s' failed all %d URL(s) (%s); one-shot keyless rescue",
        provider_name, len(rescue_urls), (original_error or "")[:200],
    )
    rescued_raw = extract_with_failover(provider_name, list(rescue_urls))
    rescued, rescued_by_identity, rescue_mapping_valid = (
        _reconcile_extract_results(rescue_urls, rescued_raw)
    )
    if not rescue_mapping_valid:
        merged_by_identity = dict(original_by_identity)
        merged_by_identity.update(rescued_by_identity)
        merged = [
            merged_by_identity[_extract_url_identity(url)] for url in urls
        ]
        return ExtractFailoverResults(
            merged,
            fallback_attempted=True,
            fallback_used=False,
        )
    rescued_errors = [r.get("error", "") for r in rescued]
    if rescued and all(e for e in rescued_errors):
        # Rescue was genuinely attempted but failed everywhere. Keep the
        # selected provider's more useful errors without losing attempt state.
        return ExtractFailoverResults(
            aligned,
            fallback_attempted=True,
            fallback_used=False,
        )
    fallback_used = any(not r.get("error") for r in rescued)
    for r in rescued:
        if not r.get("error"):
            meta = r.setdefault("metadata", {})
            if isinstance(meta, dict):
                meta["rescued_from"] = provider_name
                meta["backend_error"] = (original_error or "")[:300]
    merged_by_identity = dict(original_by_identity)
    merged_by_identity.update(rescued_by_identity)
    merged = [merged_by_identity[_extract_url_identity(url)] for url in urls]
    return ExtractFailoverResults(
        merged,
        fallback_attempted=True,
        fallback_used=fallback_used,
    )


# ─── Firecrawl Client ────────────────────────────────────────────────────────

# ─── Firecrawl Client ────────────────────────────────────────────────────────
# After PR #25182, the firecrawl client, lazy SDK proxy, dual-auth config
# resolution, response normalizers, and check_firecrawl_api_key() all live
# in plugins.web.firecrawl.provider and are re-exported at the top of this
# module so external callers (integration tests, tool-registry gating) and
# unit tests that patch tools.web_tools.<name> continue to work.


def _web_requires_env() -> list[str]:
    """Return tool metadata env vars for the currently enabled web backends.

    The gateway env vars are always reported — they're metadata strings
    used by the tool registry to light up the tool when the variable is
    set.  Gating them on ``managed_nous_tools_enabled()`` only saved
    string noise in the metadata list, but cost a synchronous HTTP
    refresh against the Nous portal on every CLI startup (invoked at
    tool-registration time).  The behavioral contract is: if the env var
    is set, the tool sees it; if not, it doesn't.  Not-logged-in users
    simply don't have the vars set, so the extra entries are harmless.
    """
    return [
        "EXA_API_KEY",
        "PARALLEL_API_KEY",
        "KEENABLE_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "FIRECRAWL_GATEWAY_URL",
        "TOOL_GATEWAY_DOMAIN",
        "TOOL_GATEWAY_SCHEME",
        "TOOL_GATEWAY_USER_TOKEN",
    ]


# ─── Parallel / Firecrawl helpers — moved into plugins ───────────────────────
# After PR #25182, the per-vendor client construction, request helpers, and
# response normalizers all live in plugins.web.<vendor>.provider:
#   - parallel: plugins/web/parallel/provider.py
#   - firecrawl: plugins/web/firecrawl/provider.py
# The names from the firecrawl plugin (Firecrawl proxy, _get_firecrawl_client,
# _to_plain_object, _normalize_result_list, _extract_web_search_results,
# _extract_scrape_payload, _is_tool_gateway_ready, etc.) are re-exported at
# the top of this module for backward-compat with integration tests and
# unit-test patches.


# Default budget (characters) of clean page text sent to the model. Pages at
# or under this size are returned whole; larger pages are head+tail truncated
# and the full text is stored on disk (see _store_full_text). Spending context,
# not API dollars — so this is generous relative to the old 5k summary cap.
# Override via web.extract_char_limit in config.yaml.
DEFAULT_EXTRACT_CHAR_LIMIT = 15000

# Hard ceiling on the full-text file written to cache/web. The truncate-store
# path otherwise calls path.write_text(content, encoding="utf-8") with no upper bound, so a
# multi-MB page (some backends return very large markdown) writes unbounded
# bytes to disk on every extract. Cap the stored copy; the model only ever
# sees char_limit anyway, and a 2MB page is already far more than any single
# read_file paging session needs. Mirrors the pre-truncate-store era's 2MB
# refusal ceiling, but stores (capped) instead of refusing.
MAX_STORED_TEXT_CHARS = 2_000_000

_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


def _get_extract_char_limit() -> int:
    """Resolve the per-page char budget from config, clamped to a sane range."""
    try:
        configured = _load_web_config().get("extract_char_limit")
        if configured is not None:
            value = int(configured)
            # Floor at 2k (below that the footer dominates), no hard ceiling
            # beyond a generous guard so a typo can't blow up context.
            return max(2000, min(value, 500_000))
    except (TypeError, ValueError):
        pass
    return DEFAULT_EXTRACT_CHAR_LIMIT


def convert_base64_images_to_links(text: str) -> str:
    """Replace inline base64 image blobs with labeled markdown links.

    base64 image payloads are token bombs (a single inline PNG can be tens of
    thousands of characters), so we never send the raw bytes to the model. But
    we preserve the fact that an image was there, and its alt text, as an
    inspectable placeholder. Real (http/https) markdown image links are left
    untouched so the agent can ``web_extract`` / ``vision_analyze`` them.

    Transformations:
      ``![alt](data:image/png;base64,AAAA...)``  -> ``[IMAGE: alt](base64 image omitted)``
      ``(data:image/png;base64,AAAA...)``        -> ``[IMAGE]``
      bare ``data:image/...;base64,AAAA...``     -> ``[IMAGE]``
    """
    # 1. Markdown image with base64 source -> keep alt text, drop the blob.
    def _md_repl(m: "re.Match[str]") -> str:
        alt = (m.group("alt") or "").strip()
        return f"[IMAGE: {alt}]" if alt else "[IMAGE]"

    md_b64 = re.compile(
        r"!\[(?P<alt>[^\]]*)\]\(\s*data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+\)"
    )
    out = md_b64.sub(_md_repl, text)

    # 2. Parenthesised base64 (non-markdown) and 3. bare base64 -> [IMAGE].
    out = re.sub(r"\(\s*data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+\)", "[IMAGE]", out)
    out = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "[IMAGE]", out)
    return out


def _store_full_text(url: str, content: str) -> Optional[str]:
    """Write the full extracted page to cache/web and return its absolute path.

    The file is mounted read-only into remote backends (Docker/Modal/SSH) via
    credential_files._CACHE_DIRS, so the agent's terminal/read_file tools can
    page through the complete text on any backend. Returns None on failure
    (storage is best-effort; truncated content is still returned to the model).
    """
    try:
        import hashlib
        from urllib.parse import urlparse
        from hermes_constants import get_hermes_dir

        cache_dir = get_hermes_dir("cache/web", "web_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)

        host = (urlparse(url).hostname or "page").replace(":", "_")
        slug = re.sub(r"[^A-Za-z0-9._-]", "-", host)[:60].strip("-") or "page"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        path = cache_dir / f"{slug}-{digest}.md"
        # Bound the stored copy so a pathologically large page can't write
        # unbounded bytes to disk. If capped, append a marker so a reader of
        # the file knows it isn't the literal complete page.
        if len(content) > MAX_STORED_TEXT_CHARS:
            content = (
                content[:MAX_STORED_TEXT_CHARS]
                + f"\n\n[... stored copy truncated at {MAX_STORED_TEXT_CHARS:,} chars "
                f"of {len(content):,}; re-extract a more specific URL for the rest ...]"
            )
        from tools.spill_safety import write_text_exclusive

        # Deterministic filename in a well-known dir: refuse symlinks via
        # lstat-unlink + exclusive create. Re-extraction of the same URL
        # legitimately overwrites (same slug-digest name). Not private:
        # cache/web is bind-mounted into remote backends whose container UID
        # must be able to read it, and content is fetched public text.
        write_text_exclusive(path, content, private=False, overwrite=True)
        return str(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to store full web_extract text for %s: %s", url, exc)
        return None


def _truncate_with_footer(
    content: str,
    url: str,
    char_limit: int,
) -> tuple[str, bool]:
    """Return (model_text, was_truncated) for one page's clean content.

    Pages at or under ``char_limit`` are returned whole. Larger pages get a
    head+tail window (~75% head / ~25% tail) cut on a markdown line boundary
    where possible, plus an explicit footer telling the model exactly how much
    it is seeing, where the full text is stored, and which read_file call pages
    in the omitted middle. Deterministic — no model involvement.
    """
    if len(content) <= char_limit:
        return content, False

    head_budget = int(char_limit * 0.75)
    tail_budget = char_limit - head_budget

    head = content[:head_budget]
    tail = content[-tail_budget:]
    # Snap the head cut back to the last newline so we don't slice mid-line.
    nl = head.rfind("\n")
    if nl > head_budget * 0.5:
        head = head[:nl]
    # Snap the tail cut forward to the next newline for the same reason.
    nl = tail.find("\n")
    if 0 <= nl < tail_budget * 0.5:
        tail = tail[nl + 1:]

    total = len(content)
    stored_path = _store_full_text(url, content)

    footer_lines = [
        "",
        "─" * 8 + " [TRUNCATED] " + "─" * 8,
        f"Showing {len(head):,} chars (head) + {len(tail):,} chars (tail) "
        f"of {total:,} total clean characters.",
    ]
    if stored_path:
        # The omitted middle begins right after the head we're showing. Give
        # the model a concrete starting line (head line count + 1) so its first
        # read_file lands in the gap instead of guessing <line>. read_file is
        # 1-indexed; +1 moves past the last head line we already showed.
        middle_start_line = head.count("\n") + 2
        footer_lines.append(f"Full text saved to: {stored_path}")
        footer_lines.append(
            f'To read the omitted middle: read_file path="{stored_path}" '
            f"offset={middle_start_line} limit=200  (the file is the complete page; "
            f"raise/lower offset to page through it)."
        )
    else:
        footer_lines.append(
            "Full text could not be stored; re-run web_extract on a more "
            "specific URL or use browser_navigate for the complete page."
        )
    footer_lines.append("─" * 29)

    model_text = head + "\n\n[... middle omitted — see footer ...]\n\n" + tail
    model_text += "\n" + "\n".join(footer_lines)
    return model_text, True



# ─── Exa / Parallel inline helpers — moved into plugins ──────────────────────
# After PR #25182, the exa client + search/extract and parallel client +
# search/extract helpers all live in their respective plugins:
#   - plugins/web/exa/provider.py
#   - plugins/web/parallel/provider.py
# Both plugins register through agent.web_search_registry and the
# dispatchers in this file resolve them via get_active_*_provider().


def _ensure_web_plugins_loaded() -> None:
    """Idempotently trigger plugin discovery so the web registry is populated.

    Every bundled web provider (brave-free, ddgs, searxng, exa, parallel,
    firecrawl, keenable, xai) registers itself via ``plugins/web/<vendor>/__init__.py``
    during plugin discovery. Tool dispatch can be reached from contexts that
    haven't already triggered discovery — subprocess agent runs, delegate
    children, standalone scripts, certain test paths — and without it the
    registry is empty and ``get_provider('firecrawl')`` returns ``None`` even
    when the user has ``web.extract_backend: firecrawl`` configured and
    ``FIRECRAWL_API_KEY`` set. The symptom is a misleading "No web extract
    provider configured" error (issue #27580).

    Mirrors :func:`tools.browser_tool._ensure_browser_plugins_loaded` exactly:
    the underlying discovery call is idempotent and cheap on subsequent
    invocations.
    """
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
    except Exception as exc:  # noqa: BLE001
        # Warning, not debug: if a plugin import is genuinely broken the
        # user otherwise hits the misleading "No web extract provider
        # configured" error this helper is meant to eliminate, with no
        # clue in normal logs about the real cause.
        logger.warning("Web plugin discovery failed (non-fatal): %s", exc)


_CORE_SEARCH_PROVENANCE_FIELDS = frozenset(
    {
        "requested_backend",
        "served_by",
        "served_by_source",
        "fallback_used",
        "retrieved_at",
        "served_at",
        "cache",
        "evidence_scope",
        "page_fetched",
        "result_scope",
        "requested_limit",
        "fetched_result_count",
        "returned_count",
        "result_set_truncated",
        "result_set_truncation_scope",
        "upstream_cache_timestamp",
        "upstream_cache_timestamp_status",
        "limitations",
        "transformations",
    }
)

_PROVIDER_SELF_CERTIFICATION_FIELDS = frozenset(
    {"confidence", "fresh", "current", "verified", "authoritative"}
)

_MAX_PROVIDER_RESPONSE_NESTING = 100

_RFC3339_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


def _provenance_string_list(value: Any) -> list[str]:
    """Return only non-empty strings from a provider-owned list field."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _merge_provenance_lists(*values: Any) -> list[str]:
    """Stable, duplicate-free merge for limitations and transformations."""
    merged = []
    seen = set()
    for value in values:
        for item in _provenance_string_list(value):
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def _search_web_count(response: dict) -> int:
    data = response.get("data") if isinstance(response, dict) else None
    web = data.get("web") if isinstance(data, dict) else None
    return len(web) if isinstance(web, list) else 0


def _invalid_search_provider_response(reason: str) -> dict:
    """Return a bounded failure without reflecting an untrusted payload."""
    return {
        "success": False,
        "error": f"Invalid web search provider response: {reason}",
    }


def _validate_search_provider_response(response: Any) -> dict:
    """Fail closed unless a provider response satisfies the search envelope.

    Failure envelopes retain their legacy provider-defined shape. Successful
    envelopes must use the literal JSON boolean ``true`` and contain a list of
    result objects at ``data.web`` before they can be cached or receive the
    Hermes-owned truth contract.
    """
    if not isinstance(response, dict):
        return _invalid_search_provider_response("expected an object")
    if type(response.get("success")) is not bool:
        return _invalid_search_provider_response(
            "'success' must be a JSON boolean"
        )
    if response["success"] is False:
        return response
    data = response.get("data")
    if not isinstance(data, dict):
        return _invalid_search_provider_response(
            "successful response must contain an object at 'data'"
        )
    web = data.get("web")
    if not isinstance(web, list):
        return _invalid_search_provider_response(
            "successful response must contain a list at 'data.web'"
        )
    if any(not isinstance(item, dict) for item in web):
        return _invalid_search_provider_response(
            "every 'data.web' item must be an object"
        )
    try:
        # Normalize exactly what the JSON tool boundary can represent. This
        # converts nested tuples to arrays and rejects cycles, unsupported
        # objects, and NaN/Infinity before caching or provenance injection.
        return json.loads(
            json.dumps(response, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError, RecursionError):
        return _invalid_search_provider_response(
            "successful response must be JSON-compatible"
        )


def _is_rfc3339_timestamp(value: str) -> bool:
    """Validate Hermes' RFC 3339 subset (leap-second notation excluded)."""
    if not _RFC3339_TIMESTAMP_RE.fullmatch(value):
        return False
    if value[17:19] == "60":
        return False
    candidate = value[:10] + "T" + value[11:]
    if candidate[-1:] in {"Z", "z"}:
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _strip_provider_self_certification_fields(
    value: Any,
    *,
    _depth: int = 0,
) -> tuple[Any, bool]:
    """Copy a provider payload while removing ambiguous bare truth claims.

    Exact, case-insensitive keys are removed at every level. Namespaced fields
    such as ``provider_confidence`` remain available for explicitly documented
    upstream metrics. Excessively nested or cyclic provider payloads fail
    closed instead of consuming unbounded recursion.
    """
    if _depth > _MAX_PROVIDER_RESPONSE_NESTING:
        raise ValueError("web search provider response nesting exceeds safety limit")
    if isinstance(value, dict):
        cleaned = {}
        omitted = False
        for key, item in value.items():
            if (
                isinstance(key, str)
                and key.strip().casefold()
                in _PROVIDER_SELF_CERTIFICATION_FIELDS
            ):
                omitted = True
                continue
            cleaned_item, item_omitted = _strip_provider_self_certification_fields(
                item,
                _depth=_depth + 1,
            )
            cleaned[key] = cleaned_item
            omitted = omitted or item_omitted
        return cleaned, omitted
    if isinstance(value, (list, tuple)):
        cleaned_list = []
        omitted = False
        for item in value:
            cleaned_item, item_omitted = _strip_provider_self_certification_fields(
                item,
                _depth=_depth + 1,
            )
            cleaned_list.append(cleaned_item)
            omitted = omitted or item_omitted
        return cleaned_list, omitted
    return value, False


def _inject_search_provenance(
    response: dict,
    *,
    requested_backend: str,
    requested_limit: int,
    fetched_result_count: int,
    retrieved_at: str,
    cache_status: str,
    cache_age_seconds: Optional[float] = None,
    cache_ttl_seconds: Optional[float] = None,
    fallback_used: bool = False,
) -> dict:
    """Add the core-owned truth contract to a successful search response.

    Providers may contribute stable fields (for example ``engine``, source
    date semantics, upstream cache reporting, limitations, and
    transformations). Hermes owns request-time routing, cache, timestamps,
    scope, and counts, and overwrites those fields on every call. Bare provider
    ``confidence``, ``current``, ``fresh``, ``verified``, and
    ``authoritative`` self-certification fields are removed.
    """
    if not isinstance(response, dict) or response.get("success") is not True:
        return response

    response, omitted_self_certification = (
        _strip_provider_self_certification_fields(response)
    )
    data = response.get("data")
    if not isinstance(data, dict):
        data = {}
    provider_provenance = data.get("provenance")
    if not isinstance(provider_provenance, dict):
        provider_provenance = {}

    legacy_served_by = data.get("served_by")
    served_by = (
        legacy_served_by.strip()
        if isinstance(legacy_served_by, str) and legacy_served_by.strip()
        else requested_backend
    )
    served_by_source = (
        "provider_reported"
        if isinstance(legacy_served_by, str) and legacy_served_by.strip()
        else "requested_backend_default"
    )
    raw_upstream_cache_timestamp = provider_provenance.get(
        "upstream_cache_timestamp"
    )
    if raw_upstream_cache_timestamp is None:
        upstream_cache_timestamp = None
        upstream_cache_timestamp_status = "not_reported_in_response"
    elif isinstance(raw_upstream_cache_timestamp, str):
        candidate_timestamp = raw_upstream_cache_timestamp.strip()
        if (
            _RFC3339_TIMESTAMP_RE.fullmatch(candidate_timestamp)
            and candidate_timestamp[17:19] == "60"
        ):
            upstream_cache_timestamp = None
            upstream_cache_timestamp_status = (
                "reported_unsupported_second_60"
            )
        elif _is_rfc3339_timestamp(candidate_timestamp):
            upstream_cache_timestamp = candidate_timestamp
            upstream_cache_timestamp_status = "reported_in_response"
        else:
            upstream_cache_timestamp = None
            upstream_cache_timestamp_status = "reported_invalid_rfc3339"
    else:
        upstream_cache_timestamp = None
        upstream_cache_timestamp_status = "reported_invalid_rfc3339"

    returned_count = _search_web_count(response)
    result_set_truncated = fetched_result_count > returned_count
    limitations = _merge_provenance_lists(
        provider_provenance.get("limitations"),
        ["page_not_fetched", "not_exhaustive"],
        ["upstream_cache_time_not_reported"]
        if upstream_cache_timestamp_status == "not_reported_in_response"
        else [],
        ["upstream_cache_timestamp_invalid_rfc3339"]
        if upstream_cache_timestamp_status == "reported_invalid_rfc3339"
        else [],
        ["upstream_cache_timestamp_second_60_unsupported"]
        if upstream_cache_timestamp_status
        == "reported_unsupported_second_60"
        else [],
    )
    transformations = _merge_provenance_lists(
        provider_provenance.get("transformations"),
        ["limit_slice"] if result_set_truncated else [],
        ["provider_self_certification_fields_omitted"]
        if omitted_self_certification
        else [],
    )

    cache_age = (
        float(cache_age_seconds)
        if cache_status == "hit" and cache_age_seconds is not None
        else None
    )
    cache_ttl = (
        float(cache_ttl_seconds)
        if cache_status in {"hit", "miss"} and cache_ttl_seconds is not None
        else None
    )
    provenance = {
        "requested_backend": requested_backend,
        "served_by": served_by,
        "served_by_source": served_by_source,
        "fallback_used": bool(fallback_used or served_by != requested_backend),
        "retrieved_at": retrieved_at,
        "served_at": _search_provenance_now(),
        "cache": {
            "layer": "hermes_process_memory",
            "status": cache_status,
            "age_seconds": cache_age,
            "ttl_seconds": cache_ttl,
            "key_dimensions": [
                "provider_name",
                "normalized_query",
                "bucketed_limit",
            ],
            "credential_identity_in_key": False,
            "locale_in_key": False,
            "provider_configuration_in_key": False,
        },
        "evidence_scope": "search_result_metadata_only",
        "page_fetched": False,
        "result_scope": "top_n",
        "requested_limit": requested_limit,
        "fetched_result_count": fetched_result_count,
        "returned_count": returned_count,
        "result_set_truncated": result_set_truncated,
        "result_set_truncation_scope": "hermes_bucket_slice_only",
        "upstream_cache_timestamp": upstream_cache_timestamp,
        "upstream_cache_timestamp_status": upstream_cache_timestamp_status,
        "limitations": limitations,
        "transformations": transformations,
    }
    for key, value in provider_provenance.items():
        if (
            key not in _CORE_SEARCH_PROVENANCE_FIELDS
            and not (
                isinstance(key, str)
                and key.strip().casefold() in _PROVIDER_SELF_CERTIFICATION_FIELDS
            )
        ):
            provenance[key] = value

    # Rebuild ``data`` so provenance stays ahead of potentially large result
    # lists when downstream context storage has to keep only a prefix.
    response["data"] = {
        "provenance": provenance,
        **{key: value for key, value in data.items() if key != "provenance"},
    }
    return response


def _search_provenance_now() -> str:
    """Late import avoids coupling web provider discovery to cache config."""
    from tools.web_result_cache import utc_now_iso

    return utc_now_iso()


_EXTRACT_CACHE_HIT_FIELD = "_hermes_extract_cache_hit"
_EXTRACT_CACHE_SERVED_BY_FIELD = "_hermes_extract_cache_served_by"
_EXTRACT_CACHE_RETRIEVED_AT_FIELD = "_hermes_extract_cache_retrieved_at"


def _build_extract_provenance(
    results: List[Dict[str, Any]],
    *,
    requested_backend: Optional[str],
    requested_count: int,
    cache_status: str,
    provider_call_attempted: bool,
    fetch_succeeded: bool,
    network_retrieved_at: str,
    fallback_attempted: bool,
    fallback_used: bool,
) -> Dict[str, Any]:
    """Build model-visible extract routing facts without copying page data.

    ``retrieved_at`` is the oldest successful source-retrieval time represented
    in this response, or ``None`` when no content was retrieved. That is
    conservative for mixed cache/fetch batches: cached pages may be older than
    pages fetched during the current call. ``served_by`` is a string for one
    serving vendor, a sorted list when successful rows came from multiple
    vendors, and ``None`` when no row succeeded.
    """
    from plugins.web.keyless_mcp import (
        EXTRACT_FALLBACK_ATTEMPTED_FIELD,
        EXTRACT_FALLBACK_USED_FIELD,
        EXTRACT_SERVED_BY_FIELD,
    )

    successful = [result for result in results if not result.get("error")]
    vendors = set()
    retrieval_times = []
    for result in successful:
        serving_vendor = result.get(EXTRACT_SERVED_BY_FIELD)
        if not isinstance(serving_vendor, str) or not serving_vendor.strip():
            serving_vendor = (
                result.get(_EXTRACT_CACHE_SERVED_BY_FIELD)
                if result.get(_EXTRACT_CACHE_HIT_FIELD)
                else None
            )
        if not isinstance(serving_vendor, str) or not serving_vendor.strip():
            serving_vendor = (
                requested_backend
                if isinstance(requested_backend, str)
                and requested_backend.strip()
                else None
            )
        if isinstance(serving_vendor, str) and serving_vendor.strip():
            vendors.add(serving_vendor.strip())

        cached_retrieved_at = result.get(_EXTRACT_CACHE_RETRIEVED_AT_FIELD)
        if (
            result.get(_EXTRACT_CACHE_HIT_FIELD)
            and isinstance(cached_retrieved_at, str)
            and cached_retrieved_at.strip()
        ):
            retrieval_times.append(cached_retrieved_at.strip())

    if fetch_succeeded and network_retrieved_at:
        retrieval_times.append(network_retrieved_at)

    served_by: Any
    if not vendors:
        served_by = None
    elif len(vendors) == 1:
        served_by = next(iter(vendors))
    else:
        served_by = sorted(vendors)

    fallback_attempt_observed = any(
        bool(result.get(EXTRACT_FALLBACK_ATTEMPTED_FIELD))
        or bool(result.get(EXTRACT_FALLBACK_USED_FIELD))
        for result in results
    )
    fallback_success_observed = (
        any(
            not result.get("error")
            and bool(result.get(EXTRACT_FALLBACK_USED_FIELD))
            for result in results
        )
        or (
            isinstance(requested_backend, str)
            and bool(requested_backend.strip())
            and any(vendor != requested_backend for vendor in vendors)
        )
    )
    fallback_used = bool(fallback_used or fallback_success_observed)
    fallback_attempted = bool(
        fallback_attempted or fallback_attempt_observed or fallback_used
    )
    served_at = _search_provenance_now()
    return {
        "requested_backend": requested_backend,
        "served_by": served_by,
        "fallback_attempted": fallback_attempted,
        "fallback_used": bool(fallback_used),
        "cache_status": cache_status,
        "provider_call_attempted": bool(provider_call_attempted),
        "fetch_succeeded": bool(fetch_succeeded),
        "retrieved_at": min(retrieval_times) if retrieval_times else None,
        "served_at": served_at,
        "requested_count": max(0, int(requested_count)),
        "returned_count": len(results),
        "success_count": len(successful),
        "failure_count": len(results) - len(successful),
    }


def _extract_error_response(
    error: str,
    *,
    requested_count: int,
    requested_backend: Optional[str] = None,
    cache_status: str = "bypass",
    provider_call_attempted: bool = False,
    fetch_succeeded: bool = False,
    network_retrieved_at: str = "",
    fallback_attempted: bool = False,
    fallback_used: bool = False,
    success_false: bool = False,
) -> str:
    """Return a legacy-compatible error envelope with mandatory provenance."""
    payload: Dict[str, Any] = {
        "provenance": _build_extract_provenance(
            [],
            requested_backend=requested_backend,
            requested_count=requested_count,
            cache_status=cache_status,
            provider_call_attempted=provider_call_attempted,
            fetch_succeeded=fetch_succeeded,
            network_retrieved_at=network_retrieved_at,
            fallback_attempted=fallback_attempted,
            fallback_used=fallback_used,
        )
    }
    bounded_error = json.loads(tool_error(error))["error"]
    if success_false:
        payload["success"] = False
    payload["error"] = bounded_error
    return json.dumps(payload, ensure_ascii=False)


def web_search_tool(query: str, limit: int = 5) -> str:
    """
    Search through the configured backend and return result metadata.

    Search descriptions are upstream snippets, not page content. This function
    does not fetch result pages or prove that a statement is current, accurate,
    or authoritative. Use :func:`web_extract_tool` and inspect the primary
    source when those distinctions matter. The success envelope always carries
    ``data.provenance`` before ``data.web`` so routing, cache layer, timestamps,
    evidence scope, counts, transformations, and limitations survive bounded
    output previews. Malformed provider success envelopes fail closed, and bare
    provider ``confidence``, ``current``, ``fresh``, ``verified``, or
    ``authoritative`` self-certification fields are removed.
    
    Args:
        query (str): The search query to look up
        limit (int): Maximum number of results to return (default: 5)
    
    Returns:
        str: JSON string containing search results with the following structure:
             {
                 "success": bool,
                 "data": {
                     "provenance": {
                         "requested_backend": str,
                         "served_by": str,
                         "served_by_source": (
                             "provider_reported" |
                             "requested_backend_default"
                         ),
                         "fallback_used": bool,
                         "retrieved_at": str,
                         "served_at": str,
                         "cache": {
                             "layer": "hermes_process_memory",
                             "status": "hit" | "miss" | "bypass",
                             "age_seconds": float | null,
                             "ttl_seconds": float | null,
                             "key_dimensions": list[str],
                             "credential_identity_in_key": false,
                             "locale_in_key": false,
                             "provider_configuration_in_key": false
                         },
                         "evidence_scope": "search_result_metadata_only",
                         "page_fetched": false,
                         "result_scope": "top_n",
                         "requested_limit": int,
                         "fetched_result_count": int,
                         "returned_count": int,
                         "result_set_truncated": bool,
                         "result_set_truncation_scope": (
                             "hermes_bucket_slice_only"
                         ),
                         "upstream_cache_timestamp": str | null,
                         "upstream_cache_timestamp_status": (
                             "reported_in_response" |
                             "not_reported_in_response" |
                             "reported_invalid_rfc3339" |
                             "reported_unsupported_second_60"
                         ),
                         "limitations": list[str],
                         "transformations": list[str]
                     },
                     "web": [
                         {
                             "title": str,
                             "url": str,
                             "description": str,
                             "position": int
                         },
                         ...
                     ]
                 }
             }
    
    Raises:
        Exception: If search fails or API key is not set
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = min(max(limit, 1), 100)

    debug_call_data = {
        "parameters": {
            "query": query,
            "limit": limit
        },
        "error": None,
        "results_count": 0,
        "original_response_size": 0,
        "final_response_size": 0
    }
    
    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)

        # Dispatch through the web search registry. All 8 providers
        # (brave-free, ddgs, searxng, exa, parallel, firecrawl, keenable, xai)
        # now live as plugins; the dispatcher is just a registry lookup +
        # delegation. Sync only — every provider's search() is sync.
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import (
            get_active_search_provider,
            get_provider as _wsp_get_provider,
            _disabled_web_plugin_for,
        )

        backend = _get_search_backend()
        provider = _wsp_get_provider(backend) if backend else None
        if provider is None or not provider.supports_search():
            if (
                provider is not None
                and not provider.supports_search()
                and _has_explicit_capability_backend("search")
            ):
                error_text = (
                    f"{provider.display_name} does not support web search. "
                    "Set web.search_backend to a search-capable provider."
                )
                response_data = {"success": False, "error": error_text}
                result_json = json.dumps(
                    response_data, indent=2, ensure_ascii=False
                )
                debug_call_data["error"] = error_text
                _debug.log_call("web_search_tool", debug_call_data)
                _debug.save()
                return result_json
            from tools.tool_backend_helpers import (
                selection_error,
                selection_exists,
            )

            if provider is None and backend and selection_exists("web"):
                disabled_key = _disabled_web_plugin_for(capability="search")
                if disabled_key:
                    _vendor = disabled_key.split("/", 1)[-1]
                    error_text = (
                        f"web.search_backend is set to '{_vendor}', but its "
                        f"plugin ('{disabled_key}') is disabled in config. "
                        f"Re-enable it with `hermes plugins enable {disabled_key}` "
                        "(or remove it from plugins.disabled)."
                    )
                else:
                    error_text = selection_error(
                        "web",
                        f"'{backend}'",
                        "no registered web search provider has that name",
                    )
                response_data = {"success": False, "error": error_text}
                result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
                debug_call_data["error"] = error_text
                _debug.log_call("web_search_tool", debug_call_data)
                _debug.save()
                return result_json
            # Never-configured install: fall back to the availability-walked
            # active provider (legacy autodetect behavior).
            provider = get_active_search_provider()

        if provider is None:
            # A bundled web plugin the user explicitly disabled looks
            # identical to "no provider" here — point at the real cause
            # (re-enable the plugin) rather than a generic setup hint.
            disabled_key = _disabled_web_plugin_for(capability="search")
            if disabled_key:
                _vendor = disabled_key.split("/", 1)[-1]
                response_data = {
                    "success": False,
                    "error": (
                        f"web.search_backend is set to '{_vendor}', but its "
                        f"plugin ('{disabled_key}') is disabled in config. "
                        f"Re-enable it with `hermes plugins enable {disabled_key}` "
                        "(or remove it from plugins.disabled)."
                    ),
                }
            else:
                response_data = {
                    "success": False,
                    "error": (
                        "No web search provider configured. "
                        "Run `hermes tools` to set one up."
                    ),
                }
        else:
            logger.info(
                "Web search via %s: '%s' (limit: %d)",
                provider.name, query, limit,
            )
            # ── TTL memo + single-flight (tools/web_result_cache.py) ──
            # Sits after every safety/config check and directly around the
            # paid vendor call. Identical queries within the TTL (subagent
            # fan-outs, repeat lookups) are served from memory; concurrent
            # identical queries share one request via the flight lock. The
            # provider is asked for the BUCKETED count (10/20/50/100) so
            # near-identical limits share an entry; the caller's requested
            # count is sliced out below. Only successful responses cache.
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
                try:
                    _resp = provider.search(query, _fetch_limit)
                except Exception as exc:  # noqa: BLE001 — candidate for rescue
                    if _rescue_eligible(provider):
                        _rescued = True
                        _resp = _rescue_search(
                            provider.name, str(exc), query, _fetch_limit
                        )
                    else:
                        raise
                else:
                    if (
                        isinstance(_resp, dict)
                        and _resp.get("success") is False
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

        debug_call_data["results_count"] = _search_web_count(response_data)
        result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
        debug_call_data["final_response_size"] = len(result_json)
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()
        return result_json

    except Exception as e:
        error_msg = f"Error searching web: {str(e)}"
        logger.debug("%s", error_msg)

        debug_call_data["error"] = error_msg
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()

        return tool_error(error_msg)


async def web_extract_tool(
    urls: List[Any],
    format: str = None,
    char_limit: Optional[int] = None,
) -> str:
    """
    Extract content from specific web pages using available extraction API backend.

    Returns clean page content (markdown/text) with NO LLM summarization. The
    extract backends (Firecrawl, Exa, Parallel, Keenable) already return clean,
    boilerplate-stripped content, so we return it directly and fast. Pages over
    ``char_limit`` are head+tail truncated with an explicit footer; the full
    text is stored under cache/web and the footer tells the model how to
    read_file the omitted middle. Inline base64 images are replaced with
    ``[IMAGE: alt]`` placeholders (real image URLs are preserved as links).

    Args:
        urls (List[Any]): URL strings or search-result objects containing a
            string ``url`` or ``href`` field
        format (str): Desired output format ("markdown" or "html", optional)
        char_limit (Optional[int]): Per-page char budget sent to the model
            (default: web.extract_char_limit or 15000). Larger pages truncate.

    Security: URLs are checked for embedded secrets before fetching.

    Returns:
        str: JSON string with a top-level ``provenance`` object followed by a
             ``results`` list; each result keeps the existing ``url``,
             ``title``, ``content``, ``error`` shape. Provenance contains only
             routing, fallback attempt/use, cache, provider-call, successful
             fetch timing, and aggregate-count facts — never page URLs,
             content, or error text. Error envelopes retain their legacy keys
             and also begin with the same mandatory provenance object.

    Raises:
        Exception: If extraction fails or API key is not set
    """
    _extract_requested_count = len(urls)
    _extract_requested_backend: Optional[str] = None
    _extract_cache_status = "bypass"
    _extract_provider_call_attempted = False
    _extract_fetch_succeeded = False
    _extract_network_retrieved_at = ""
    _extract_fallback_attempted = False
    _extract_fallback_used = False

    # Block URLs containing embedded secrets (exfiltration prevention).
    # URL-decode first so percent-encoded secrets (%73k- = sk-) are caught.
    from agent.redact import _PREFIX_RE
    from urllib.parse import unquote
    normalized_urls: List[str] = []
    normalized_indices: List[int] = []
    invalid_urls: Dict[int, Dict[str, Any]] = {}
    for index, item in enumerate(urls):
        _url = _web_extract_url(item)
        if _url is None:
            invalid_urls[index] = {
                "url": "",
                "title": "",
                "content": "",
                "error": (
                    f"Invalid URL item at index {index}: expected a URL string "
                    "or an object with a string 'url' or 'href' field"
                ),
            }
            continue
        normalized_url = normalize_url_for_request(_url)
        if (
            _PREFIX_RE.search(_url)
            or _PREFIX_RE.search(unquote(_url))
            or _PREFIX_RE.search(normalized_url)
            or _PREFIX_RE.search(unquote(normalized_url))
        ):
            return _extract_error_response(
                "Blocked: URL contains what appears to be an API key or token. "
                "Secrets must not be sent in URLs.",
                requested_count=_extract_requested_count,
                success_false=True,
            )
        sensitive_query_key = sensitive_query_param_name(normalized_url)
        if sensitive_query_key:
            return _extract_error_response(
                (
                    "Blocked: URL contains a credential-like query parameter "
                    f"({sensitive_query_key}). Web extract backends are third-party "
                    "readers; remove the sensitive query parameter or use a local "
                    "browser session when this access is explicitly required."
                ),
                requested_count=_extract_requested_count,
                success_false=True,
            )
        normalized_urls.append(normalized_url)
        normalized_indices.append(index)

    debug_call_data = {
        "parameters": {
            "urls": normalized_urls,
            "format": format,
            "char_limit": char_limit,
        },
        "error": None,
        "pages_extracted": 0,
        "pages_truncated": 0,
        "original_response_size": 0,
        "final_response_size": 0,
        "truncation_metrics": [],
        "processing_applied": []
    }

    try:
        logger.info("Extracting content from %d URL(s)", len(normalized_urls))

        # ── SSRF protection — filter out private/internal URLs before any backend ──
        safe_urls = []
        safe_indices = []
        ssrf_blocked: Dict[int, Dict[str, Any]] = {}
        for index, url in zip(normalized_indices, normalized_urls):
            if not await async_is_safe_url(url):
                ssrf_blocked[index] = {
                    "url": url, "title": "", "content": "",
                    "error": "Blocked: URL targets a private or internal network address",
                }
            else:
                safe_urls.append(url)
                safe_indices.append(index)
        safe_result_by_identity: Dict[str, Dict[str, Any]] = {}

        # Dispatch only safe URLs to the configured backend
        if not safe_urls:
            results = []
        else:
            backend = _get_extract_backend()
            _extract_requested_backend = backend or None

            # All eight providers (brave-free, ddgs, searxng, exa, parallel,
            # firecrawl, keenable, xai) now live as plugins. The dispatcher is a
            # registry lookup + delegation. Some providers' extract() is
            # async (parallel, firecrawl), others sync (exa, keenable) — we
            # detect coroutine functions and await; sync functions run
            # inline (the policy gate, SSRF re-check, etc. live inside the
            # provider itself for the firecrawl per-URL loop).
            _ensure_web_plugins_loaded()
            from agent.web_search_registry import (
                get_active_extract_provider,
                get_provider as _wsp_get_provider,
                _disabled_web_plugin_for,
            )

            provider = _wsp_get_provider(backend) if backend else None
            if provider is None or not provider.supports_extract():
                # When an explicitly configured name is registered but does
                # not support extract (search-only providers like brave-free /
                # ddgs / searxng), surface a typed error. An autodetected
                # capability mismatch instead continues through the registry's
                # capability-aware provider walk. An unregistered name (typo /
                # unloaded plugin) is handled by the strict-selection check
                # below.
                if (
                    provider is not None
                    and not provider.supports_extract()
                    and _has_explicit_capability_backend("extract")
                ):
                    return _extract_error_response(
                        (
                            f"{provider.display_name} is a search-only "
                            "backend and cannot extract URL content. "
                            "Set web.extract_backend to firecrawl, "
                            "keenable, exa, or parallel."
                        ),
                        requested_count=_extract_requested_count,
                        requested_backend=_extract_requested_backend,
                        success_false=True,
                    )
                from tools.tool_backend_helpers import (
                    selection_error,
                    selection_exists,
                )

                if provider is None and backend and selection_exists("web"):
                    # Strict selection: a stored-but-unregistered backend
                    # errors by name instead of silently switching to
                    # whatever the availability walk finds.
                    disabled_key = _disabled_web_plugin_for(capability="extract")
                    if disabled_key:
                        _vendor = disabled_key.split("/", 1)[-1]
                        error_text = (
                            f"web.extract_backend is set to '{_vendor}', but "
                            f"its plugin ('{disabled_key}') is disabled in "
                            f"config. Re-enable it with `hermes plugins "
                            f"enable {disabled_key}` (or remove it from "
                            "plugins.disabled)."
                        )
                    else:
                        error_text = selection_error(
                            "web",
                            f"'{backend}'",
                            "no registered web extract provider has that name",
                        )
                    return _extract_error_response(
                        error_text,
                        requested_count=_extract_requested_count,
                        requested_backend=_extract_requested_backend,
                        success_false=True,
                    )
                provider = get_active_extract_provider()
                if provider is None:
                    # If the configured backend is a bundled web plugin the
                    # user explicitly disabled, the backend is set correctly
                    # and the real fix is to re-enable the plugin — say so
                    # instead of telling them to set web.extract_backend
                    # (which they already did). #40190 follow-up.
                    disabled_key = _disabled_web_plugin_for(capability="extract")
                    if disabled_key:
                        _vendor = disabled_key.split("/", 1)[-1]
                        return _extract_error_response(
                            (
                                f"web.extract_backend is set to '{_vendor}', "
                                f"but its plugin ('{disabled_key}') is disabled "
                                "in config. Re-enable it with "
                                f"`hermes plugins enable {disabled_key}` "
                                "(or remove it from plugins.disabled)."
                            ),
                            requested_count=_extract_requested_count,
                            requested_backend=_extract_requested_backend,
                            success_false=True,
                        )
                    return _extract_error_response(
                        (
                            "No web extract provider configured. "
                            "Set web.extract_backend to firecrawl, "
                            "keenable, exa, or parallel."
                        ),
                        requested_count=_extract_requested_count,
                        requested_backend=_extract_requested_backend,
                        success_false=True,
                    )


            # ── Extract cache (tools/web_result_cache.py) ─────────────────
            _extract_requested_backend = provider.name

            # Disk-backed via cache/web: a URL extracted within the TTL is
            # served from disk instead of re-scraped. Deliberately placed
            # AFTER the secret-URL gate, SSRF gate, provider resolution, and
            # strict-selection validation, and gated per-URL on the website
            # blocklist policy — a hit skips only the vendor call, never a
            # control. Policy-blocked URLs are treated as cache misses so
            # dispatch handles them exactly as it would without a cache.
            # Keys include the provider and format, so switching backends or
            # formats within the TTL never serves the other's content.
            from tools.web_result_cache import (
                cache_enabled as _extract_cache_enabled,
                extract_cache_get as _extract_cache_get,
                extract_cache_put as _extract_cache_put,
            )
            from tools.website_policy import check_website_access as _check_site
            cached_results: Dict[str, Dict[str, Any]] = {}
            fetch_urls: List[str] = []
            safe_identity_order = [
                _extract_url_identity(url) for url in safe_urls
            ]
            representative_url_by_identity: Dict[str, str] = {}
            for identity, url in zip(safe_identity_order, safe_urls):
                representative_url_by_identity.setdefault(identity, url)
            for identity, url in representative_url_by_identity.items():
                hit = None
                try:
                    _policy_block = _check_site(url)
                except Exception:  # noqa: BLE001 — policy errors fail open like dispatch
                    _policy_block = None
                if _policy_block is None:
                    hit = _extract_cache_get(
                        url, format=format, provider=provider.name
                    )
                if hit is not None:
                    hit[_EXTRACT_CACHE_HIT_FIELD] = True
                    hit[_EXTRACT_CACHE_SERVED_BY_FIELD] = hit.get("served_by")
                    hit[_EXTRACT_CACHE_RETRIEVED_AT_FIELD] = hit.get(
                        "retrieved_at"
                    )
                    cached_results[identity] = hit
                else:
                    fetch_urls.append(url)

            if not fetch_urls:
                _extract_cache_status = "hit"
            elif cached_results:
                _extract_cache_status = "mixed"
            elif _extract_cache_enabled():
                _extract_cache_status = "miss"
            else:
                _extract_cache_status = "bypass"

            if not fetch_urls:
                safe_result_by_identity = dict(cached_results)
                results = [
                    {
                        **cached_results[identity],
                        "url": url,
                    }
                    for identity, url in zip(safe_identity_order, safe_urls)
                ]
            else:
                logger.info(
                    "Web extract via %s: %d URL(s)", provider.name, len(fetch_urls)
                )

                # Async-or-sync dispatch: parallel + firecrawl have async
                # extract(); exa + keenable are sync.
                import inspect
                _extract_provider_call_attempted = True
                _extract_result_mapping_valid = True
                try:
                    if inspect.iscoroutinefunction(provider.extract):
                        results = await provider.extract(fetch_urls, format=format)
                    else:
                        # Run sync extract() in a thread so we don't block the
                        # event loop on network I/O.
                        results = await asyncio.to_thread(
                            provider.extract, fetch_urls, format=format
                        )
                except Exception as exc:  # noqa: BLE001 — candidate for rescue
                    if _rescue_eligible(provider):
                        _extract_fallback_attempted = True
                        failed = [
                            {"url": u, "title": "", "content": "", "error": str(exc)}
                            for u in fetch_urls
                        ]
                        results = await asyncio.to_thread(
                            _rescue_extract, provider.name, fetch_urls, failed
                        )
                    else:
                        raise
                else:
                    results, _, _extract_result_mapping_valid = (
                        _reconcile_extract_results(fetch_urls, results)
                    )
                    # One-shot keyless rescue when the WHOLE batch failed
                    # (backend-level outage, not per-page problems). Stateless:
                    # the next web_extract call uses the chosen backend again.
                    if (
                        results
                        and all(r.get("error") for r in results)
                        and _extract_result_mapping_valid
                        and _rescue_eligible(provider)
                        and any(
                            not _policy_blocked_result(result)
                            for result in results
                        )
                    ):
                        _extract_fallback_attempted = True
                        results = await asyncio.to_thread(
                            _rescue_extract, provider.name, fetch_urls, results
                        )

                results, fetched_results_by_identity, _ = (
                    _reconcile_extract_results(fetch_urls, results)
                )

                from plugins.web.keyless_mcp import (
                    EXTRACT_FALLBACK_ATTEMPTED_FIELD,
                    EXTRACT_FALLBACK_USED_FIELD,
                    EXTRACT_SERVED_BY_FIELD,
                    ExtractFailoverResults,
                )
                _extract_fallback_attempted = bool(
                    _extract_fallback_attempted
                    or getattr(results, "fallback_attempted", False)
                    or any(
                        bool(result.get(EXTRACT_FALLBACK_ATTEMPTED_FIELD))
                        or bool(result.get(EXTRACT_FALLBACK_USED_FIELD))
                        for result in results
                    )
                )
                _extract_fallback_used = bool(
                    getattr(results, "fallback_used", False)
                    or any(
                        not result.get("error")
                        and bool(result.get(EXTRACT_FALLBACK_USED_FIELD))
                        for result in results
                    )
                )
                _extract_fetch_succeeded = any(
                    not result.get("error")
                    and bool(
                        result.get("raw_content", "")
                        or result.get("content", "")
                    )
                    for result in results
                )
                _extract_vendor_drift = any(
                    not result.get("error")
                    and isinstance(result.get(EXTRACT_SERVED_BY_FIELD), str)
                    and result[EXTRACT_SERVED_BY_FIELD] != provider.name
                    for result in results
                )
                _extract_fallback_used = bool(
                    _extract_fallback_used or _extract_vendor_drift
                )
                _extract_fallback_attempted = bool(
                    _extract_fallback_attempted or _extract_fallback_used
                )
                if _extract_fetch_succeeded:
                    _extract_network_retrieved_at = _search_provenance_now()

                # Cache each successful fetch's full clean text for TTL reuse
                # (best-effort; oversized pages are skipped by the cache).
                # NEVER cache a rescue-served batch: it came from a ring
                # vendor, not the chosen backend, and caching it would make
                # the one-shot rescue sticky for a whole TTL — the next call
                # must attempt the chosen backend again.
                if not (_extract_fallback_attempted or _extract_fallback_used):
                    for requested_url in fetch_urls:
                        fetched = fetched_results_by_identity[
                            _extract_url_identity(requested_url)
                        ]
                        if fetched.get("error"):
                            continue
                        _content = (
                            fetched.get("raw_content", "") or fetched.get("content", "")
                        )
                        if _content:
                            _extract_cache_put(
                                requested_url,
                                _content,
                                title=fetched.get("title", ""),
                                format=format,
                                provider=provider.name,
                                served_by=provider.name,
                                retrieved_at=_extract_network_retrieved_at,
                            )

                # Merge by canonical request identity only. Provider result
                # order is not a contract (Parallel appends failures after
                # successes), so positional assembly can cross-wire content.
                safe_result_by_identity = dict(cached_results)
                safe_result_by_identity.update(fetched_results_by_identity)
                results = ExtractFailoverResults(
                    [
                        {
                            **safe_result_by_identity[identity],
                            "url": url,
                        }
                        for identity, url in zip(
                            safe_identity_order, safe_urls
                        )
                    ],
                    fallback_attempted=_extract_fallback_attempted,
                    fallback_used=_extract_fallback_used,
                )

        # Reconstruct original input order across invalid, blocked, and
        # provider-processed entries from the canonical URL map. Provider list
        # position is never used as identity.
        if invalid_urls or ssrf_blocked:
            safe_results = {
                index: {
                    **safe_result_by_identity[_extract_url_identity(url)],
                    "url": url,
                }
                for index, url in zip(safe_indices, safe_urls)
            }
            by_index = {**safe_results, **ssrf_blocked, **invalid_urls}
            results = [by_index[index] for index in range(len(urls))]

        response = {"results": results}
        
        pages_extracted = len(response.get('results', []))
        logger.info("Extracted content from %d pages", pages_extracted)
        
        debug_call_data["pages_extracted"] = pages_extracted
        debug_call_data["original_response_size"] = len(json.dumps(response))

        effective_char_limit = char_limit if char_limit is not None else _get_extract_char_limit()
        try:
            effective_char_limit = max(2000, min(int(effective_char_limit), 500_000))
        except (TypeError, ValueError):
            effective_char_limit = DEFAULT_EXTRACT_CHAR_LIMIT

        # Truncate-and-store: no LLM. For each result, convert inline base64
        # images to labeled placeholders (keeping alt text + real image URLs),
        # then return the clean content directly if within budget, or a
        # head+tail window plus a footer pointing at the stored full text.
        debug_call_data["processing_applied"].append("truncate_and_store")
        for result in response.get("results", []):
            if result.get("error"):
                continue
            url = result.get("url", "")
            raw_content = result.get("raw_content", "") or result.get("content", "")
            if not raw_content:
                continue
            clean = convert_base64_images_to_links(raw_content)
            model_text, truncated = _truncate_with_footer(clean, url, effective_char_limit)
            result["content"] = model_text
            if truncated:
                debug_call_data["pages_truncated"] += 1
                debug_call_data["truncation_metrics"].append({
                    "url": url,
                    "original_size": len(clean),
                    "sent_size": len(model_text),
                })
                logger.info("%s (truncated %d -> %d chars)", url, len(clean), len(model_text))
            else:
                logger.info("%s (%d chars, whole)", url, len(clean))

        # Trim output to minimal fields per entry: title, content, error
        trimmed_results = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "error": r.get("error"),
                **({  "blocked_by_policy": r["blocked_by_policy"]} if "blocked_by_policy" in r else {}),
            }
            for r in response.get("results", [])
        ]
        trimmed_response: Dict[str, Any] = {
            "provenance": _build_extract_provenance(
                response["results"],
                requested_backend=_extract_requested_backend,
                requested_count=_extract_requested_count,
                cache_status=_extract_cache_status,
                provider_call_attempted=_extract_provider_call_attempted,
                fetch_succeeded=_extract_fetch_succeeded,
                network_retrieved_at=_extract_network_retrieved_at,
                fallback_attempted=_extract_fallback_attempted,
                fallback_used=_extract_fallback_used,
            ),
            "results": trimmed_results,
        }

        if trimmed_response.get("results") == []:
            result_json = _extract_error_response(
                "Content was inaccessible or not found",
                requested_count=_extract_requested_count,
                requested_backend=_extract_requested_backend,
                cache_status=_extract_cache_status,
                provider_call_attempted=_extract_provider_call_attempted,
                fetch_succeeded=_extract_fetch_succeeded,
                network_retrieved_at=_extract_network_retrieved_at,
                fallback_attempted=_extract_fallback_attempted,
                fallback_used=_extract_fallback_used,
            )
        else:
            result_json = json.dumps(trimmed_response, indent=2, ensure_ascii=False)

        # base64 images were already converted to placeholders per-result above;
        # this is a belt-and-suspenders sweep over the serialized JSON in case a
        # provider tucked a blob somewhere unexpected (e.g. metadata).
        cleaned_result = convert_base64_images_to_links(result_json)

        debug_call_data["final_response_size"] = len(cleaned_result)
        debug_call_data["processing_applied"].append("base64_image_conversion")
        
        # Log debug information
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()
        
        return cleaned_result
            
    except Exception as e:
        error_msg = f"Error extracting content: {str(e)}"
        logger.debug("%s", error_msg)
        
        debug_call_data["error"] = error_msg
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()
        
        return _extract_error_response(
            error_msg,
            requested_count=_extract_requested_count,
            requested_backend=_extract_requested_backend,
            cache_status=_extract_cache_status,
            provider_call_attempted=_extract_provider_call_attempted,
            fetch_succeeded=_extract_fetch_succeeded,
            network_retrieved_at=_extract_network_retrieved_at,
            fallback_attempted=_extract_fallback_attempted,
            fallback_used=_extract_fallback_used,
        )


# Convenience function to check Firecrawl credentials
def _provider_is_ready(provider) -> bool:
    """Return True when *provider* reports readiness without raising.

    ``get_active_*_provider()`` intentionally returns an explicitly configured
    backend even when ``is_available()`` is False so the dispatcher can emit a
    precise missing-credential error. Tool/doctor readiness gates must still
    require a true availability probe — otherwise ``hermes doctor`` paints a
    green ✓ for a backend that cannot run (issue #78412).

    A provider that can serve anonymously (``is_keyless_available()`` — the
    Exa/Parallel free tier) IS ready: keyless mode is a working state, not a
    misconfiguration.
    """
    if provider is None:
        return False
    try:
        if provider.is_available():
            return True
    except Exception as exc:  # noqa: BLE001 — broken provider == not ready
        logger.debug(
            "web provider %r.is_available() raised during readiness check: %s",
            getattr(provider, "name", provider),
            exc,
        )
        return False
    try:
        return bool(provider.is_keyless_available())
    except Exception as exc:  # noqa: BLE001 — broken provider == not ready
        logger.debug(
            "web provider %r.is_keyless_available() raised during readiness check: %s",
            getattr(provider, "name", provider),
            exc,
        )
        return False


def check_web_api_key() -> bool:
    """Check whether the configured web backend is available.

    Used as the ``check_fn`` gate for the ``web_search`` and ``web_extract``
    tool registry entries — so a plugin-registered provider that reports
    ``is_available()`` must light the tools up even when no built-in backend
    has credentials (issues #28651, #31873). Resolution funnels through
    :func:`_is_backend_available`, which delegates non-legacy names to the
    registry.
    """
    # ``or ""``: a null ``web.backend`` value yields None from ``.get``, and
    # ``None.lower()`` would raise. Mirrors ``_get_backend``.
    configured = (_load_web_config().get("backend") or "").lower().strip()
    if configured and _is_backend_available(configured):
        return True
    # Any built-in backend with credentials present. This is a boolean OR, so
    # unlike _get_backend() the probe order is irrelevant.
    if any(_is_backend_available(backend) for backend in _LEGACY_WEB_BACKENDS):
        return True
    # Plugin-registered path: the active-provider resolvers return an explicit
    # config hit even when credentials are missing (so the tool can print a
    # precise "set FOO_API_KEY" error). Readiness still requires a true
    # availability probe — keyed (is_available) OR keyless-capable
    # (is_keyless_available; the Exa/Parallel anonymous free tier serves
    # zero-credential installs, so those count as ready). Discovery must run
    # first — check_fn fires at tool-registration time, before any dispatch
    # has populated the registry.
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import (
            get_active_search_provider,
            get_active_extract_provider,
        )

        return (
            _provider_is_ready(get_active_search_provider())
            or _provider_is_ready(get_active_extract_provider())
        )
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry availability check failed: %s", exc)
        return False

if __name__ == "__main__":
    """
    Simple test/demo when run directly
    """
    print("🌐 Standalone Web Tools Module")
    print("=" * 40)

    # Check if API keys are available
    web_available = check_web_api_key()
    tool_gateway_available = _is_tool_gateway_ready()
    from hermes_cli.config import get_env_value as _gev
    firecrawl_key_available = bool((_gev("FIRECRAWL_API_KEY") or "").strip())
    firecrawl_url_available = bool((_gev("FIRECRAWL_API_URL") or "").strip())

    if web_available:
        backend = _get_backend()
        print(f"✅ Web backend: {backend}")
        if backend == "exa":
            print("   Using Exa API (https://exa.ai)")
        elif backend == "parallel":
            print("   Using Parallel API (https://parallel.ai)")
        elif backend == "searxng":
            print(f"   Using SearXNG (search only): {_env_value('SEARXNG_URL')}")
        elif backend == "brave-free":
            print("   Using Brave Search free tier (search only)")
        elif backend == "ddgs":
            print("   Using DuckDuckGo via ddgs package (search only)")
        elif firecrawl_url_available:
            print(f"   Using self-hosted Firecrawl: {(_gev('FIRECRAWL_API_URL') or '').strip().rstrip('/')}")
        elif firecrawl_key_available:
            print("   Using direct Firecrawl cloud API")
        elif tool_gateway_available:
            print(f"   Using Firecrawl tool-gateway: {_get_firecrawl_gateway_url()}")
        else:
            print("   Firecrawl backend selected but not configured")
    else:
        print("❌ No web search backend configured")
        print(
            "Set EXA_API_KEY, PARALLEL_API_KEY, KEENABLE_API_KEY, FIRECRAWL_API_KEY, FIRECRAWL_API_URL"
            f"{_firecrawl_backend_help_suffix()}"
        )

    if not web_available:
        sys.exit(1)

    print("🛠️  Web tools ready for use!")
    print(f"   Extract char limit: {_get_extract_char_limit()} chars "
          "(pages over this are truncated; full text stored in cache/web)")

    # Show debug mode status
    if _debug.active:
        print(f"🐛 Debug mode ENABLED - Session ID: {_debug.session_id}")
        print(f"   Debug logs will be saved to: {_debug.log_dir}/web_tools_debug_{_debug.session_id}.json")
    else:
        print("🐛 Debug mode disabled (set WEB_TOOLS_DEBUG=true to enable)")

    print("\nBasic usage:")
    print("  from web_tools import web_search_tool, web_extract_tool")
    print("  import asyncio")
    print("")
    print("  # Search (synchronous)")
    print("  results = web_search_tool('Python tutorials')")
    print("")
    print("  # Extract (asynchronous, no LLM — truncate-and-store)")
    print("  async def main():")
    print("      content = await web_extract_tool(['https://example.com'])")
    print("      # bigger budget for one call:")
    print("      content = await web_extract_tool(['https://docs.python.org'], char_limit=40000)")
    print("  asyncio.run(main())")

    print("\nDebug mode:")
    print("  export WEB_TOOLS_DEBUG=true")
    print("  # Logs saved to: ./logs/web_tools_debug_UUID.json")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
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
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=args.get("limit", 5)),
    check_fn=check_web_api_key,
    requires_env=_web_requires_env(),
    emoji="🔍",
    max_result_size_chars=100_000,
)
registry.register(
    name="web_extract",
    toolset="web",
    schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [],
        "markdown",
        char_limit=args.get("char_limit"),
    ),
    check_fn=check_web_api_key,
    requires_env=_web_requires_env(),
    is_async=True,
    emoji="📄",
    max_result_size_chars=100_000,
)
