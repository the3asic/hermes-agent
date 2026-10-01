"""One-shot keyless-ring rescue for failed keyed/configured web calls.

Stateless by design: a rescue routes THIS call through the free-tier ring (plugins/web/keyless_mcp.py);
the next web_search/web_extract call attempts the chosen backend again. Callers must never cache a
rescue-served response, or the one-shot rescue becomes sticky for a whole TTL. Logs under the origin
(tools.web_tools) logger.
"""

import logging
from plugins.web.keyless_mcp import web_is_interrupted, is_extract_refusal, interrupted_extract_results

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
    except Exception as exc:  # noqa: BLE001 — registry optional
        logger.debug("keyless rescue tier check failed: %s", exc)
        return False


def _rescue_eligible(provider) -> bool:
    """True when a failed call on *provider* should get a one-shot rescue.

    Eligible: a keyed/configured path — any non-ring backend, or a ring vendor in keyed mode. A ring
    vendor already in keyless mode is NOT eligible: its failure means the ring was already walked.
    """
    if web_is_interrupted() or not _keyless_rescue_enabled() or provider is None:
        return False
    try:
        from plugins.web.keyless_mcp import _KEYLESS_RING, use_keyless
        name = getattr(provider, "name", "")
        if name not in _KEYLESS_RING:
            return True
        from agent.web_search_provider import get_provider_env
        if name == "firecrawl" and get_provider_env("FIRECRAWL_API_URL"):
            return True
        key_var = _RING_KEY_VARS.get(name, "")
        return not use_keyless(name, get_provider_env(key_var) if key_var else "")
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

    if web_is_interrupted() or is_extract_refusal({"error": original_error}):
        return {"success": False, "error": "Interrupted" if web_is_interrupted() else original_error}

    logger.warning(
        "web_search backend '%s' failed (%s); one-shot keyless rescue",
        provider_name, (original_error or "")[:200],
    )
    rescued = search_with_failover(provider_name, query, limit)
    if web_is_interrupted() or is_extract_refusal(rescued):
        return {
            "success": False,
            "error": "Interrupted" if web_is_interrupted() else rescued.get("error", "Request refused"),
        }
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
    """True for a website-policy refusal — intentional, never rescued (it would fetch blocked content)."""
    error = str(result.get("error") or "").lower()
    return bool(result.get("blocked_by_policy")) or "blocked by website policy" in error


def _rescue_extract(provider_name: str, urls: list, results: list) -> list:
    """One-shot keyless-ring rescue for a failed keyed/configured extract.

    Fires only when EVERY url failed (whole-backend failure); partial
    results are page problems and pass through untouched. Stateless —
    the next web_extract call attempts the chosen backend again.

    Cancellation, website-policy and security refusals are intentional, not
    outages. They are never re-fetched, and completed pages are preserved.
    """
    from plugins.web.keyless_mcp import ExtractFailoverResults, extract_with_failover
    from tools.web_tools_extract import _reconcile_extract_results, _extract_url_identity

    aligned, original_by_identity, mapping_valid = _reconcile_extract_results(
        urls, results
    )
    if not mapping_valid:
        # An invalid provider mapping is an integrity failure, not an outage.
        # Never route it elsewhere and risk accepting content for the wrong URL.
        return aligned

    if web_is_interrupted():
        return ExtractFailoverResults(interrupted_extract_results(urls, aligned))
    if any(
        result.get("interrupted")
        or "interrupted" in str(result.get("error") or "").lower()
        for result in aligned
    ):
        # A provider may observe cancellation before the originating thread's
        # bit is visible here. Its explicit stop still ends the whole batch.
        return ExtractFailoverResults(aligned)

    # Partition by canonical request identity. Rescue only genuine backend
    # failures; website-policy refusals remain untouched.
    rescue_urls = [
        url
        for url in urls
        if original_by_identity[_extract_url_identity(url)].get("error")
        and not is_extract_refusal(
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
        # selected provider's errors, except that a later refusal must survive.
        preserved = dict(original_by_identity)
        preserved.update({
            identity: row for identity, row in rescued_by_identity.items()
            if is_extract_refusal(row)
        })
        return ExtractFailoverResults(
            [preserved[_extract_url_identity(url)] for url in urls],
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
