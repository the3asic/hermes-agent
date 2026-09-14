"""web_extract helpers: URL validation, provider resolution, cache-aware dispatch.

Order of controls (each is a gate, never skipped by a cache hit): secret-URL
refusal -> SSRF filter (in web_tools.web_extract_tool) -> provider resolution
(strict selection) -> per-URL website policy -> disk cache -> vendor call with
one-shot keyless rescue. Logs under the origin (tools.web_tools) logger.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from tools.tool_backend_helpers import selection_error, selection_exists
from tools.url_safety import normalize_url_for_request
from tools.url_safety import async_is_safe_url, sensitive_query_param_name
from plugins.web.keyless_mcp import web_is_interrupted, is_extract_refusal, interrupted_extract_results
from tools.web_tools_provenance import (
    _EXTRACT_CACHE_HIT_FIELD, _EXTRACT_CACHE_SERVED_BY_FIELD, _EXTRACT_CACHE_RETRIEVED_AT_FIELD,
    _search_provenance_now, _extract_error_response,
)
from tools.web_tools_rescue import _rescue_eligible, _rescue_extract

logger = logging.getLogger("tools.web_tools")


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
            for source_key in ("sourceURL", "source_url"):
                source_url = metadata.get(source_key)
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

_NO_RESULT_ERROR = "Extract backend returned no result for this URL"
_EXTRACT_BACKENDS_HINT = "firecrawl, tavily, keenable, exa, or parallel."
_INVALID_ITEM_ERROR = (
    "Invalid URL item at index {}: expected a URL string or an object with a string 'url' or 'href' field"
)


def _web_extract_url(value: Any) -> Optional[str]:
    """URL from a model-supplied extract item (str, or dict with ``url``/``href``); None if unusable.

    Models sometimes forward a whole search result instead of its URL, hence the dict form. Never
    stringify arbitrary objects into misleading fetch targets.
    """
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    return (value.strip() or None) if isinstance(value, str) else None


def _disabled_plugin_error(capability: str, disabled_key: str) -> str:
    """Error text when the configured backend's bundled plugin is disabled in config."""
    vendor = disabled_key.split("/", 1)[-1]
    return (
        f"web.{capability}_backend is set to '{vendor}', but its plugin ('{disabled_key}') is disabled "
        f"in config. Re-enable it with `hermes plugins enable {disabled_key}` "
        "(or remove it from plugins.disabled)."
    )


def _no_provider_error(capability: str, fallback: str) -> str:
    """Error when no provider resolved: point at a disabled bundled plugin if that is the real cause."""
    from agent.web_search_registry import _disabled_web_plugin_for
    disabled_key = _disabled_web_plugin_for(capability=capability)
    return _disabled_plugin_error(capability, disabled_key) if disabled_key else fallback


def _strict_selection_error(capability: str, backend: str) -> str:
    """Error for a stored-but-unregistered backend: name the disabled plugin, else the bad selection.
    Strict selection never silently switches to whatever the availability walk finds."""
    failure = f"no registered web {capability} provider has that name"
    return _no_provider_error(capability, selection_error("web", f"'{backend}'", failure))


def _result_entry(url: str, error: Optional[str]) -> Dict[str, Any]:
    return {"url": url, "title": "", "content": "", "error": error}


def _extract_error_json(error: str) -> str:
    return json.dumps({"success": False, "error": error}, ensure_ascii=False)


def _refuse_all(error: str, requested_count: int):
    """Whole-call refusal with the same provenance contract as normal responses."""
    return None, None, None, _extract_error_response(error, requested_count=requested_count, success_false=True)


def _merge_in_order(
    total: int, fixed: Dict[int, dict], fetch_positions: List[int], fetch_urls: List[str], results: List[dict]
) -> List[dict]:
    """Merge already-reconciled safe results and local refusals by canonical request identity."""
    by_identity = {_extract_url_identity(row["url"]): row for row in results}
    merged = dict(fixed)
    for position, url in zip(fetch_positions, fetch_urls):
        merged[position] = {
            **by_identity.get(_extract_url_identity(url), _result_entry(url, _NO_RESULT_ERROR)),
            "url": url,
        }
    return [merged[index] for index in range(total)]


def _validate_extract_urls(urls: List[Any]):
    """Normalize model-supplied items and block URLs carrying secrets (percent-encoded forms are unquoted
    and checked too). Returns ``(normalized_urls, normalized_indices, invalid_urls, blocked_json)``;
    ``blocked_json`` is a whole-call refusal (exfiltration prevention) or None."""
    from agent.redact import _PREFIX_RE
    from urllib.parse import unquote

    normalized_urls, normalized_indices, invalid_urls = [], [], {}
    for index, item in enumerate(urls):
        _url = _web_extract_url(item)
        if _url is None:
            invalid_urls[index] = _result_entry("", _INVALID_ITEM_ERROR.format(index))
            continue
        normalized_url = normalize_url_for_request(_url)
        if any(_PREFIX_RE.search(c) for c in (_url, unquote(_url), normalized_url, unquote(normalized_url))):
            return _refuse_all(
                "Blocked: URL contains what appears to be an API key or token. "
                "Secrets must not be sent in URLs.",
                len(urls),
            )
        sensitive_query_key = sensitive_query_param_name(normalized_url)
        if sensitive_query_key:
            return _refuse_all(
                "Blocked: URL contains a credential-like query parameter "
                f"({sensitive_query_key}). Web extract backends are third-party "
                "readers; remove the sensitive query parameter or use a local "
                "browser session when this access is explicitly required.",
                len(urls),
            )
        normalized_urls.append(normalized_url)
        normalized_indices.append(index)
    return normalized_urls, normalized_indices, invalid_urls, None


def _resolve_extract_provider(backend: str):
    """Resolve the extract provider for *backend*; returns ``(provider, error_json)``.

    A registered search-only backend is a typed error (never a silent switch). An unregistered name with
    a stored web selection is a strict-selection error; with no selection, fall through to the walk.
    """
    from agent.web_search_registry import get_active_extract_provider, get_provider as _wsp_get_provider
    from tools.web_tools import _has_explicit_capability_backend
    provider = _wsp_get_provider(backend) if backend else None
    if provider is not None and provider.supports_extract():
        return provider, None
    if provider is not None and _has_explicit_capability_backend("extract"):
        return None, _extract_error_json(
            f"{provider.display_name} is a search-only backend and cannot extract URL content. "
            "Set web.extract_backend to " + _EXTRACT_BACKENDS_HINT
        )
    if provider is None and backend and selection_exists("web"):
        return None, _extract_error_json(_strict_selection_error("extract", backend))
    provider = get_active_extract_provider()
    if provider is None:
        fallback = "No web extract provider configured. Set web.extract_backend to " + _EXTRACT_BACKENDS_HINT
        return None, _extract_error_json(_no_provider_error("extract", fallback))
    return provider, None


async def _dispatch_extract(provider, fetch_urls: List[str], format: Optional[str], facts: dict) -> List[dict]:
    """Reconcile fetched rows by URL identity before fallback, cache writes or provenance."""
    from tools.web_result_cache import extract_cache_put as _extract_cache_put
    import inspect
    facts["provider_call_attempted"] = not web_is_interrupted()
    _extract_result_mapping_valid = True
    try:
        if not facts["provider_call_attempted"]:
            results = interrupted_extract_results(fetch_urls)
        elif inspect.iscoroutinefunction(provider.extract):
            results = await provider.extract(fetch_urls, format=format)
        else:
            # Run sync extract() in a thread so we don't block the
            # event loop on network I/O.
            results = await asyncio.to_thread(
                provider.extract, fetch_urls, format=format
            )
    except Exception as exc:  # noqa: BLE001 — candidate for rescue
        if web_is_interrupted():
            results = interrupted_extract_results(fetch_urls)
        elif not is_extract_refusal({"error": str(exc)}) and _rescue_eligible(provider):
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
                not is_extract_refusal(result)
                for result in results
            )
        ):
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
    facts["fallback_attempted"] = bool(
        facts["fallback_attempted"]
        or getattr(results, "fallback_attempted", False)
        or any(
            bool(result.get(EXTRACT_FALLBACK_ATTEMPTED_FIELD))
            or bool(result.get(EXTRACT_FALLBACK_USED_FIELD))
            for result in results
        )
    )
    facts["fallback_used"] = bool(
        getattr(results, "fallback_used", False)
        or any(
            not result.get("error")
            and bool(result.get(EXTRACT_FALLBACK_USED_FIELD))
            for result in results
        )
    )
    facts["fetch_succeeded"] = any(
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
    facts["fallback_used"] = bool(
        facts["fallback_used"] or _extract_vendor_drift
    )
    facts["fallback_attempted"] = bool(
        facts["fallback_attempted"] or facts["fallback_used"]
    )
    if facts["fetch_succeeded"]:
        facts["network_retrieved_at"] = _search_provenance_now()

    # Cache each successful fetch's full clean text for TTL reuse
    # (best-effort; oversized pages are skipped by the cache).
    # NEVER cache a rescue-served batch: it came from a ring
    # vendor, not the chosen backend, and caching it would make
    # the one-shot rescue sticky for a whole TTL — the next call
    # must attempt the chosen backend again.
    if not (facts["fallback_attempted"] or facts["fallback_used"] or web_is_interrupted()):
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
                metadata = fetched.get("metadata")
                final_url = (
                    metadata.get("url")
                    if isinstance(metadata, dict) else None
                )
                _extract_cache_put(
                    requested_url,
                    _content,
                    title=fetched.get("title", ""),
                    format=format,
                    provider=provider.name,
                    served_by=provider.name,
                    retrieved_at=facts["network_retrieved_at"],
                    final_url=final_url or requested_url,
                )
    return results


async def _extract_safe_urls(provider, safe_urls: List[str], format: Optional[str], *, facts: dict) -> List[dict]:
    """Apply website and redirect policy to every cache hit, then fetch unique uncached URLs."""
    from tools.web_result_cache import (
        cache_enabled as _extract_cache_enabled,
        extract_cache_get as _extract_cache_get,
        extract_cache_put as _extract_cache_put,
    )
    from tools.website_policy import check_website_access as _check_site
    cached_results: Dict[str, Dict[str, Any]] = {}
    refused_results: Dict[str, Dict[str, Any]] = {}
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
        except Exception as exc:
            refused_results[identity] = {
                "url": url, "title": "", "content": "",
                "error": f"Website policy check failed: {exc}",
                "blocked_by_security": True,
            }
            continue
        if _policy_block is not None:
            refused_results[identity] = {
                "url": url, "title": "", "content": "",
                "error": _policy_block["message"],
                "blocked_by_policy": {
                    key: _policy_block[key]
                    for key in ("host", "rule", "source")
                },
            }
            continue
        hit = _extract_cache_get(
            url, format=format, provider=provider.name
        )
        if hit is not None:
            final_url = hit.get("final_url", url)
            if not await async_is_safe_url(final_url):
                refused_results[identity] = {
                    "url": url, "title": "", "content": "",
                    "error": "Blocked: URL targets a private or internal network address",
                    "blocked_by_security": True,
                }
                continue
            try:
                final_block = _check_site(final_url)
            except Exception as exc:
                refused_results[identity] = {
                    "url": url, "title": "", "content": "",
                    "error": f"Website policy check failed: {exc}",
                    "blocked_by_security": True,
                }
                continue
            if final_block is not None:
                refused_results[identity] = {
                    "url": url, "title": "", "content": "",
                    "error": final_block["message"],
                    "blocked_by_policy": {
                        key: final_block[key]
                        for key in ("host", "rule", "source")
                    },
                }
                continue
            hit[_EXTRACT_CACHE_HIT_FIELD] = True
            hit[_EXTRACT_CACHE_SERVED_BY_FIELD] = hit.get("served_by")
            hit[_EXTRACT_CACHE_RETRIEVED_AT_FIELD] = hit.get(
                "retrieved_at"
            )
            cached_results[identity] = hit
        else:
            fetch_urls.append(url)

    if not fetch_urls:
        facts["cache_status"] = "hit" if cached_results else "bypass"
    elif cached_results:
        facts["cache_status"] = "mixed"
    elif _extract_cache_enabled():
        facts["cache_status"] = "miss"
    else:
        facts["cache_status"] = "bypass"
    fetched_results_by_identity = {}
    if fetch_urls:
        logger.info("Web extract via %s: %d URL(s)", provider.name, len(fetch_urls))
        fetched = await _dispatch_extract(provider, fetch_urls, format, facts)
        _, fetched_results_by_identity, _ = _reconcile_extract_results(fetch_urls, fetched)
    safe_result_by_identity = {**refused_results, **cached_results, **fetched_results_by_identity}
    return [
        {**safe_result_by_identity[identity], "url": url}
        for identity, url in zip(safe_identity_order, safe_urls)
    ]
