"""One-shot keyless-ring rescue for failed keyed/configured web calls.

Stateless by design: a rescue routes THIS call through the free-tier ring (plugins/web/keyless_mcp.py);
the next web_search/web_extract call attempts the chosen backend again. Callers must never cache a
rescue-served response, or the one-shot rescue becomes sticky for a whole TTL. Logs under the origin
(tools.web_tools) logger.
"""

import logging

logger = logging.getLogger("tools.web_tools")

# Ring vendor -> env var holding its paid key (keyed mode ⇒ eligible for rescue).
_RING_KEY_VARS = {
    "exa": "EXA_API_KEY", "parallel": "PARALLEL_API_KEY",
    "firecrawl": "FIRECRAWL_API_KEY", "keenable": "KEENABLE_API_KEY",
}


def _keyless_rescue_enabled() -> bool:
    """``web.keyless_rescue`` (default on), implicitly off when the keyless tier is disabled."""
    from tools.web_tools import _load_web_config
    if not _load_web_config().get("keyless_rescue", True):
        return False
    try:
        from agent.web_search_registry import _keyless_tier_enabled
        return _keyless_tier_enabled()
    except Exception as exc:
        logger.debug("keyless rescue tier check failed: %s", exc)
        return False


def _ring_vendor_keyless(name: str, capability: str | None = None) -> bool:
    """Did *name*'s own provider route this call through the anonymous keyless ring?

    Mirrors the predicate each ring provider evaluates before calling, so eligibility reflects what
    actually happened. Firecrawl owns extra routes that bypass the ring without a key — the managed
    Nous Tool Gateway (persisted ``nous`` selection, or the legacy never-configured fallback when the
    gateway is ready) and a self-hosted ``FIRECRAWL_API_URL`` — so it is asked directly.
    """
    if name == "firecrawl":
        from plugins.web.firecrawl.provider import _use_keyless_ring
        return _use_keyless_ring(capability)
    from agent.web_search_provider import get_provider_env
    from plugins.web.keyless_mcp import use_keyless
    key_var = _RING_KEY_VARS.get(name, "")
    return use_keyless(name, get_provider_env(key_var) if key_var else "")


def _managed_search_fallback(provider, original_error: str, query: str, limit: int):
    """Try managed Firecrawl for this call only; None leaves the original error for keyless rescue.
    Managed Firecrawl is billed, so a caller on free fast search alone never reaches it."""
    from agent.web_search_provider import get_provider_env
    from tools import web_tools as _wt
    if (getattr(provider, "name", "") != "perplexity"
            or get_provider_env("PERPLEXITY_API_KEY") or not _wt._managed_web_search() or not _wt._is_tool_gateway_ready()):
        return None
    logger.warning("web_search managed Perplexity failed (%s); serving this call from managed Firecrawl", (original_error or "")[:200])
    try:
        from agent.web_search_registry import get_provider
        resp = get_provider("firecrawl").search(query, limit)
    except Exception as exc:
        resp = {"success": False, "error": str(exc)}
    if not resp.get("success"):
        logger.warning("managed Firecrawl fallback failed too: %s", str(resp.get("error", ""))[:200])
        return None
    resp.setdefault("data", {}).update(
        fallback_from="managed_primary",
        backend_error=f"Primary managed search failed this call ({(original_error or 'unknown error')[:300]}); "
                      "result served by the managed fallback. The next call will use the primary again.",
    )
    return resp


def _rescue_eligible(provider, capability: str | None = None) -> bool:
    """True when a failed call on *provider* should get a one-shot rescue.

    Eligible: any call that did NOT go through the keyless ring — a non-ring backend, a ring vendor
    in keyed mode, or a ring vendor routed through the managed gateway / a self-hosted instance. A
    ring vendor that walked the ring is NOT eligible: its failure means the ring already failed.
    """
    if not _keyless_rescue_enabled() or provider is None:
        return False
    try:
        from plugins.web.keyless_mcp import _KEYLESS_RING
        name = getattr(provider, "name", "")
        return name not in _KEYLESS_RING or not _ring_vendor_keyless(name, capability)
    except Exception as exc:
        logger.debug("rescue eligibility check failed: %s", exc)
        return False


def _rescue_search(provider_name: str, original_error: str, query: str, limit: int) -> dict:
    """Rescue a failed search via the ring; annotate the result with the original failure."""
    from plugins.web.keyless_mcp import search_with_failover
    logger.warning(
        "web_search backend '%s' failed (%s); one-shot keyless rescue",
        provider_name, (original_error or "")[:200],
    )
    rescued = search_with_failover(provider_name, query, limit)
    if rescued.get("success"):
        rescued.setdefault("data", {}).update(
            rescued_from=provider_name,
            backend_error=(
                f"Configured backend '{provider_name}' failed this call "
                f"({(original_error or 'unknown error')[:300]}); result served by the keyless free tier. "
                f"The next call will use '{provider_name}' again."
            ),
        )
        return rescued
    # Ring also failed: the ORIGINAL error names the user's setup, so lead with it.
    return {
        "success": False,
        "error": (
            f"{original_error or 'search failed'} "
            f"(keyless rescue also failed: {rescued.get('error', 'unknown')})"
        ),
    }


def _rescue_extract(provider_name: str, urls: list, results: list) -> list:
    """Retry failed request identities once, preserving completed pages and safety refusals."""
    from plugins.web._common import is_origin_http_error
    from plugins.web.keyless_mcp import ExtractFailoverResults, extract_with_failover, is_extract_refusal, web_is_interrupted
    from tools.web_tools_extract import _extract_url_identity, _reconcile_extract_results

    ordered, by_identity, valid = _reconcile_extract_results(urls, results)
    if not valid or web_is_interrupted() or any(str(row.get("error", "")).casefold() == "interrupted" for row in ordered):
        return ordered
    rescue_urls = [url for url in urls if (
        (row := by_identity[_extract_url_identity(url)]).get("error")
        and not is_extract_refusal(row) and not is_origin_http_error(row.get("error"))
    )]
    if not rescue_urls:
        return ordered
    original_error = str(by_identity[_extract_url_identity(rescue_urls[0])].get("error") or "extract failed")
    logger.warning("web_extract backend '%s' failed %d URL(s) (%s); one-shot keyless rescue",
                   provider_name, len(rescue_urls), original_error[:200])
    _rescued, recovered, valid = _reconcile_extract_results(
        rescue_urls, extract_with_failover(provider_name, rescue_urls),
    )
    used = False
    partial_success = any(not row.get("error") for row in recovered.values())
    for url in rescue_urls:
        identity = _extract_url_identity(url)
        row = recovered[identity]
        if valid and (not row.get("error") or is_extract_refusal(row) or partial_success):
            if not row.get("error"):
                meta = row.setdefault("metadata", {})
                if isinstance(meta, dict):
                    meta.update(rescued_from=provider_name, backend_error=original_error[:300])
                used = True
            by_identity[identity] = row
        elif not valid:
            by_identity[identity] = row
    return ExtractFailoverResults(
        [by_identity[_extract_url_identity(url)] for url in urls], fallback_attempted=True, fallback_used=used,
    )
