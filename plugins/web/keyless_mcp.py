"""Keyless web search/extract via public free-tier endpoints (Exa, Parallel, Firecrawl, Keenable).
Resolved strictly LAST — after every keyed backend, the managed gateway, ddgs and custom plugin
providers — so it never pre-empts a deliberate setup. Privacy: no user identifiers are sent;
Parallel gets a random per-process ``session_id`` (rate limiting only) and its optional
``model_name`` analytics field is deliberately omitted. Disable with ``web.keyless_fallback: false``.
"""

from __future__ import annotations

import json
import logging
import inspect
import threading
import uuid
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Dict, List, Optional

from plugins.web._common import document as _page, page_error as _page_error, search_fail, search_ok, web_hit as _row

logger = logging.getLogger(__name__)

# asyncio.to_thread copies contextvars, but interrupt bits belong to the
# originating tool thread. Keep that identity across nested provider workers
# without changing the plugin ABC or sharing state across conversations.
_web_interrupt_thread = ContextVar("web_interrupt_thread", default=None)


def web_is_interrupted() -> bool:
    from tools.interrupt import is_interrupted, is_thread_interrupted

    return is_interrupted() or is_thread_interrupted(_web_interrupt_thread.get())


def web_interruptible(function):
    """Bind one web call's interrupt owner, including its executor workers."""
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def async_wrapper(*args, **kwargs):
            token = _web_interrupt_thread.set(
                _web_interrupt_thread.get() or threading.get_ident()
            )
            try:
                return await function(*args, **kwargs)
            finally:
                _web_interrupt_thread.reset(token)
        return async_wrapper

    @wraps(function)
    def sync_wrapper(*args, **kwargs):
        token = _web_interrupt_thread.set(
            _web_interrupt_thread.get() or threading.get_ident()
        )
        try:
            return function(*args, **kwargs)
        finally:
            _web_interrupt_thread.reset(token)
    return sync_wrapper


def is_extract_refusal(result: dict) -> bool:
    """User cancellation and security decisions must never trigger rescue."""
    if any(result.get(key) for key in (
        "interrupted", "blocked_by_policy", "blocked_by_security",
    )):
        return True
    error = str(result.get("error") or "").lower()
    return any(marker in error for marker in (
        "interrupted", "blocked by website policy",
        "blocked: url targets a private or internal", "ssrf",
    ))


def interrupted_extract_results(urls, results=()):
    """Keep completed pages and refusals; mark unfinished work interrupted."""
    from tools.web_tools_extract import _extract_url_identity, _reconcile_extract_results

    _, by_identity, _ = _reconcile_extract_results(urls, list(results))
    return [
        row if not row.get("error") or is_extract_refusal(row) else {
            "url": url, "title": "", "content": "",
            "error": "Interrupted", "interrupted": True,
        }
        for url in urls
        for row in [by_identity[_extract_url_identity(url)]]
    ]

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
KEENABLE_API_URL = "https://api.keenable.ai"
_KEENABLE_TITLE = "hermes-agent"

# Parallel free-tier rate-limit correlation id — random per process, never persisted.
_SESSION_ID = uuid.uuid4().hex

_TIMEOUT_SECONDS = 30

# Internal per-result handoff fields. The web_extract wrapper consumes these
# before returning its trimmed public envelope; they never enter cached page
# content or model-visible result rows.
EXTRACT_SERVED_BY_FIELD = "_hermes_extract_served_by"
EXTRACT_FALLBACK_ATTEMPTED_FIELD = "_hermes_extract_fallback_attempted"
EXTRACT_FALLBACK_USED_FIELD = "_hermes_extract_fallback_used"


class ExtractFailoverResults(list):
    """List-compatible extract batch with trusted routing outcome metadata.

    Per-row markers preserve which successful rows came from a fallback
    vendor.  Batch attributes also preserve an attempted-but-unsuccessful
    failover when no returned row can carry that fact (including an empty
    result list).
    """

    def __init__(
        self,
        values=(),
        *,
        fallback_attempted: bool = False,
        fallback_used: bool = False,
    ) -> None:
        super().__init__(values)
        self.fallback_attempted = bool(fallback_attempted)
        self.fallback_used = bool(fallback_used)


class KeylessMCPError(RuntimeError):
    """A keyless MCP call failed (transport, rate limit, or tool error)."""


_RATE_LIMIT_MARKERS = ("rate limit", "rate-limit", "ratelimit", "too many requests", "429", "quota exceeded", "slow down")

# vendor -> (display label, env key, signup URL) for the standard failure hint.
_VENDOR_HINTS = {
    "exa": ("Exa", "EXA_API_KEY", "https://exa.ai"), "parallel": ("Parallel", "PARALLEL_API_KEY", "https://parallel.ai"),
    "firecrawl": ("Firecrawl", "FIRECRAWL_API_KEY", "https://firecrawl.dev"), "keenable": ("Keenable", "KEENABLE_API_KEY", "https://keenable.ai"),
}


def _is_rate_limitish(message: str) -> bool:
    """Heuristic: does an error message look like free-tier throttling?"""
    return any(marker in (message or "").lower() for marker in _RATE_LIMIT_MARKERS)


def _fail_msg(vendor: str, kind: str, exc: Any, *, other_backends: bool = True) -> str:
    label, env_key, site = _VENDOR_HINTS[vendor]
    alt = " or another web backend via `hermes tools`" if other_backends else ""
    return f"Keyless {label} {kind} failed: {exc}. Set {env_key} ({site}){alt} for reliable service."


def _search(vendor: str, rows: Callable[[], List[Dict[str, Any]]], catch: Any = (), fmt: Optional[Callable[[Exception], str]] = None) -> Dict[str, Any]:
    """``search_ok(rows())``; :class:`KeylessMCPError` → standard vendor hint, exception
    types in ``catch`` → ``fmt(exc)``; anything else propagates."""
    try:
        if web_is_interrupted():
            return search_fail("Interrupted")
        results = rows()
        return search_fail("Interrupted") if web_is_interrupted() else search_ok(results)
    except KeylessMCPError as exc:
        return search_fail(_fail_msg(vendor, "search", exc))
    except catch as exc:
        return search_fail(fmt(exc))


def _per_url(urls: List[str], fetch: Callable[[str], Dict[str, Any]], vendor: str, catch: Any = Exception, hint: bool = False) -> List[Dict[str, Any]]:
    """Per-URL extract loop: a ``catch`` failure becomes an error entry (``hint`` adds the ``hermes tools`` hint)."""
    def _one(url: str) -> Dict[str, Any]:
        try:
            return fetch(url)
        except catch as exc:  # noqa: BLE001 — per-URL error entry
            return _page_error(url, _fail_msg(vendor, "extract", exc, other_backends=hint))

    results = []
    for index, url in enumerate(urls):
        if web_is_interrupted():
            results.extend(interrupted_extract_results(urls[index:]))
            break
        row = _one(url)
        if web_is_interrupted():
            results.extend(interrupted_extract_results(urls[index:], [row] if is_extract_refusal(row) else []))
            break
        results.append(row)
    return results


# --- Tier / config ------------------------------------------------------------
def keyless_enabled() -> bool:
    """Delegates to the registry so the ``web.keyless_fallback`` (default on) chokepoint lives with backend resolution."""
    try:
        from agent.web_search_registry import _keyless_tier_enabled
        return _keyless_tier_enabled()
    except Exception as exc:  # noqa: BLE001 — resolver optional in stripped envs
        logger.debug("keyless_enabled(): registry helper unavailable: %s", exc)
        return True


_BACKEND_KEYS = ("backend", "search_backend", "extract_backend")


def _web_config_selects(name: str) -> bool:
    """True when any ``web.backend`` / ``search_backend`` / ``extract_backend`` names *name*."""
    import tools.web_tools as _wt
    web_cfg = _wt._load_web_config()
    return any((web_cfg.get(key) or "").lower().strip() == name for key in _BACKEND_KEYS)


def provider_tier(name: str) -> str:
    """``web.provider_tier.<name>`` (``hermes tools`` Free/Paid rows): ``free``, ``paid``, or ``auto`` (anything else/unset)."""
    try:
        from hermes_cli.config import load_config
        tiers = (load_config().get("web") or {}).get("provider_tier") or {}
        value = str(tiers.get(name, "") or "").lower().strip()
        return value if value in ("free", "paid") else "auto"
    except Exception as exc:  # noqa: BLE001 — config layer optional
        logger.debug("provider_tier(%r) config read failed: %s", name, exc)
        return "auto"


def use_keyless(name: str, api_key: str) -> bool:
    """Single chokepoint for search + extract: ``free`` → keyless even with a key; ``paid`` → keyed even
    without one (the keyed path raises its usual missing-key error); ``auto`` → keyless only when no key + tier enabled."""
    tier = provider_tier(name)
    if tier in ("free", "paid"):
        return tier == "free"
    return not api_key and keyless_enabled()


# --- MCP transport ------------------------------------------------------------
def _parse_mcp_body(body: str) -> str:
    """First text content item from an MCP tools/call response — plain-JSON bodies
    (Parallel) or SSE ``data: {...}`` lines (Exa). Raises :class:`KeylessMCPError` for
    JSON-RPC errors and ``isError`` tool results (e.g. Exa's free-tier rate limit)."""

    def _from_payload(payload: str) -> Optional[str]:
        payload = payload.strip()
        if not payload.startswith("{"):
            return None
        data = json.loads(payload)
        err = data.get("error")
        if err:
            raise KeylessMCPError(str(err.get("message") or err))
        result = data.get("result") or {}
        texts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)]
        if result.get("isError"):
            raise KeylessMCPError(" ".join(t for t in texts if t) or "MCP tool call failed")
        return next((str(t) for t in texts if t), None)

    stripped = body.strip()
    candidates = [stripped] if stripped.startswith("{") else []
    candidates += [line[len("data: "):] for line in body.splitlines() if line.startswith("data: ")]
    for candidate in candidates:
        try:
            text = _from_payload(candidate)
        except json.JSONDecodeError:
            continue
        if text is not None:
            return text
    raise KeylessMCPError("Unrecognized MCP response shape")


def mcp_call(url: str, tool: str, arguments: Dict[str, Any], timeout: int = _TIMEOUT_SECONDS) -> str:
    """POST a JSON-RPC ``tools/call`` and return the text payload. Raises
    :class:`KeylessMCPError` on transport failures, non-2xx, JSON-RPC and tool errors."""
    import requests
    if web_is_interrupted():
        raise KeylessMCPError("Interrupted")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": "hermes-agent"}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise KeylessMCPError(f"request failed: {exc}") from exc
    if web_is_interrupted():
        raise KeylessMCPError("Interrupted")
    if response.status_code >= 400:
        raise KeylessMCPError(f"HTTP {response.status_code}: {response.text[:300]}")
    return _parse_mcp_body(response.text)


# --- Parallel (search.parallel.ai) — JSON text payloads -----------------------
def parallel_search_keyless(query: str, limit: int = 5) -> Dict[str, Any]:
    def _rows() -> List[Dict[str, Any]]:
        text = mcp_call(PARALLEL_MCP_URL, "web_search", {"objective": query, "search_queries": [query], "session_id": _SESSION_ID})
        results = json.loads(text).get("results") or []
        return [
            _row(r.get("url") or "", r.get("title") or "", " ".join(r.get("excerpts") or []), i + 1)
            for i, r in enumerate(results[:max(limit, 0)] if limit else results)
        ]

    return _search("parallel", _rows, (json.JSONDecodeError, TypeError, KeyError), lambda exc: f"Keyless Parallel search returned an unexpected payload: {exc}")


def parallel_extract_keyless(urls: List[str]) -> List[Dict[str, Any]]:
    try:
        data = json.loads(mcp_call(PARALLEL_MCP_URL, "web_fetch", {"urls": list(urls), "objective": "Full page content", "session_id": _SESSION_ID}))
    except (KeylessMCPError, json.JSONDecodeError, TypeError) as exc:
        message = _fail_msg("parallel", "extract", exc)
        return [_page_error(u, message) for u in urls]
    results = [
        _page(r.get("url") or "", r.get("title") or "", r.get("full_content") or r.get("content") or "\n\n".join(r.get("excerpts") or []))
        for r in data.get("results") or []
    ]
    for error in data.get("errors") or []:
        url = error.get("url") or ""
        results.append({**_page_error(url, str(error.get("content") or error.get("error_type") or "extraction failed")), "metadata": {"sourceURL": url}})
    # URLs the endpoint silently dropped still get an error entry (per-URL contract).
    seen = {r["url"] for r in results}
    results.extend(_page_error(u, "no content returned") for u in urls if u not in seen)
    return results


# --- Exa (mcp.exa.ai) — formatted plain-text payloads -------------------------
def _after(line: str, prefix: str) -> str:
    return line[len(prefix):].strip()


_EXA_LABELS = ("Title:", "URL:", "Highlights:", "Published:", "Author:")


def _parse_exa_search_text(text: str, limit: int) -> List[Dict[str, Any]]:
    """Parse Exa's ``---``-separated ``Title:/URL:/Published:/Author:/Highlights:`` blocks."""
    results: List[Dict[str, Any]] = []
    for block in text.split("\n---\n"):
        title = url = ""
        highlight_lines: List[str] = []
        in_highlights = False
        for stripped in map(str.strip, block.splitlines()):
            if stripped.startswith("Title:"):
                title = _after(stripped, "Title:")
            elif stripped.startswith("URL:"):
                url = _after(stripped, "URL:")
            elif in_highlights and stripped and not stripped.startswith(_EXA_LABELS):
                highlight_lines.append(stripped)
            # Highlights run until the next labelled field.
            if stripped.startswith(_EXA_LABELS):
                in_highlights = stripped.startswith("Highlights:")
        if url:
            results.append(_row(url, title, " ".join(highlight_lines), len(results) + 1))
        if limit and len(results) >= limit:
            break
    return results


def exa_search_keyless(query: str, limit: int = 5) -> Dict[str, Any]:
    return _search("exa", lambda: _parse_exa_search_text(mcp_call(EXA_MCP_URL, "web_search_exa", {"query": query, "numResults": max(1, int(limit))}), limit))


def exa_extract_keyless(urls: List[str]) -> List[Dict[str, Any]]:
    """Called per-URL; the tool returns one combined text payload."""
    def _fetch(url: str) -> Dict[str, Any]:
        text = mcp_call(EXA_MCP_URL, "web_fetch_exa", {"urls": [url]})
        # Title: first markdown H1 or ``Title:`` line, whichever comes first.
        titles = (_after(s, "# " if s.startswith("# ") else "Title:") for s in map(str.strip, text.splitlines()) if s.startswith(("# ", "Title:")))
        return _page(url, next(titles, ""), text)

    return _per_url(urls, _fetch, "exa", catch=KeylessMCPError, hint=True)


# --- Firecrawl keyless (public cloud API, no auth header) ---------------------
def firecrawl_search_keyless(query: str, limit: int = 5) -> Dict[str, Any]:
    from plugins.web.firecrawl.provider import _KeylessFirecrawlClient, _extract_web_search_results
    rows = lambda: _extract_web_search_results(_KeylessFirecrawlClient().search(query=query, limit=limit))  # noqa: E731
    return _search("firecrawl", rows, Exception, lambda exc: _fail_msg("firecrawl", "search", exc))


def firecrawl_extract_keyless(urls: List[str]) -> List[Dict[str, Any]]:
    """Keyless Firecrawl cloud scrape → legacy extract result list."""
    from plugins.web.firecrawl.provider import (
        _KeylessFirecrawlClient,
        _scrape_result_for_request,
        _website_refusal,
    )
    client = _KeylessFirecrawlClient()

    def _fetch(url: str) -> Dict[str, Any]:
        refusal = _website_refusal(url)
        if refusal is not None:
            return refusal
        response = client.scrape(url=url, formats=["markdown"])
        return _scrape_result_for_request(url, response)

    return _per_url(urls, _fetch, "firecrawl")


# --- Keenable keyless (api.keenable.ai public endpoints) ----------------------
def _keenable_request(method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
    """Call a Keenable public endpoint with the mandatory X-Keenable-Title app id."""
    import requests
    if web_is_interrupted():
        raise KeylessMCPError("Interrupted")
    headers = {"X-Keenable-Title": _KEENABLE_TITLE}
    if method == "post":
        headers["Content-Type"] = "application/json"
    response = getattr(requests, method)(f"{KEENABLE_API_URL}{path}", headers=headers, timeout=_TIMEOUT_SECONDS, **kwargs)
    if web_is_interrupted():
        raise KeylessMCPError("Interrupted")
    if response.status_code >= 400:
        raise KeylessMCPError((response.text or "").strip() or f"HTTP {response.status_code}")
    return response.json()


def keenable_search_keyless(query: str, limit: int = 5) -> Dict[str, Any]:
    def _rows() -> List[Dict[str, Any]]:
        data = _keenable_request("post", "/v1/search/public", json={"query": query, "max_results": max(1, int(limit))})
        return [
            _row(r.get("url") or "", r.get("title") or "", r.get("snippet") or r.get("description") or "", i + 1)
            for i, r in enumerate(data.get("results") or [])
        ]

    return _search("keenable", _rows, Exception, lambda exc: f"Keyless Keenable search failed: {exc}.")


def keenable_extract_keyless(urls: List[str]) -> List[Dict[str, Any]]:
    def _fetch(url: str) -> Dict[str, Any]:
        data = _keenable_request("get", "/v1/fetch/public", params={"url": url})
        return _page(data.get("url") or url, data.get("title") or "", data.get("content") or "", source_url=url)

    return _per_url(urls, _fetch, "keenable")


# --- Round-robin ring + next-in-line failover (rate-limited free tiers) -------
_KEYLESS_RING = ("exa", "parallel", "firecrawl", "keenable")

# Late-bound lookups (not bare references) so ``patch.object(keyless_mcp, "<vendor>_search_keyless")``
# is honored at call time. Tests also ``setitem`` these dicts directly.
_KEYLESS_SEARCHERS: Dict[str, Callable[[str, int], Dict[str, Any]]] = {v: (lambda query, limit, _v=v: globals()[f"{_v}_search_keyless"](query, limit)) for v in _KEYLESS_RING}
_KEYLESS_EXTRACTORS: Dict[str, Callable[[List[str]], List[Dict[str, Any]]]] = {v: (lambda urls, _v=v: globals()[f"{_v}_extract_keyless"](urls)) for v in _KEYLESS_RING}

# Per-process round-robin cursor, seeded by the random session id so the fleet
# spreads across vendors; advances once per unpinned keyless request.
_ring_lock = threading.Lock()
_ring_cursor = int(_SESSION_ID, 16) % len(_KEYLESS_RING)


def _vendor_pinned(name: str) -> bool:
    """True when config explicitly routes web traffic to *name* (backend keys or a
    ``free`` tier pin). A pinned vendor starts every keyless request."""
    if provider_tier(name) == "free":
        return True
    try:
        return _web_config_selects(name)
    except Exception as exc:  # noqa: BLE001 — config layer optional
        logger.debug("_vendor_pinned(%r) config read failed: %s", name, exc)
        return False


def _ring_order(name: str) -> List[str]:
    """Vendor walk order: pinned → start at *name* (its ring position fixes the failover
    succession); else round-robin from the cursor, advancing it per request. Vendors
    pinned ``paid`` are excluded (explicit paid opts their free endpoint out)."""
    global _ring_cursor
    if _vendor_pinned(name):
        start = _KEYLESS_RING.index(name) if name in _KEYLESS_RING else 0
    else:
        with _ring_lock:
            start = _ring_cursor
            _ring_cursor = (_ring_cursor + 1) % len(_KEYLESS_RING)
    ordered = _KEYLESS_RING[start:] + _KEYLESS_RING[:start]
    return [v for v in ordered if provider_tier(v) != "paid"]


_ALL_PAID_MSG = "All keyless web providers are pinned to paid tiers."


def _walk_ring(name: str, kind: str, call, throttled) -> tuple:
    """Call each vendor from :func:`_ring_order` until a result is not ``throttled``.
    Returns ``(order, vendor, result, exhausted)``; ``order`` is empty (result None)
    when every vendor is pinned paid."""
    order = _ring_order(name)
    vendor, result = None, None
    for i, vendor in enumerate(order):
        result = call(vendor)
        if not throttled(result):
            return order, vendor, result, False
        if i + 1 < len(order):
            logger.info("keyless %s %s throttled; failing over to %s", vendor, kind, order[i + 1])
    return order, vendor, result, True


@web_interruptible
def search_with_failover(name: str, query: str, limit: int = 5) -> Dict[str, Any]:
    """Rate-limit-shaped errors advance to the next vendor, other errors stop the walk
    (a malformed query fails everywhere). ``data.served_by`` is set when the serving
    vendor differs from *name*."""

    def _throttled(result: Dict[str, Any]) -> bool:
        return not result.get("success") and not is_extract_refusal(result) and _is_rate_limitish(result.get("error", ""))

    def _call(vendor):
        if web_is_interrupted():
            return search_fail("Interrupted")
        result = _KEYLESS_SEARCHERS[vendor](query, limit)
        return search_fail("Interrupted") if web_is_interrupted() else result

    if web_is_interrupted():
        return search_fail("Interrupted")
    order, vendor, result, exhausted = _walk_ring(name, "search", _call, _throttled)
    if not order:
        return search_fail(_ALL_PAID_MSG)
    if exhausted:
        result["error"] = f"{result.get('error', '')} (all keyless vendors throttled: {', '.join(order)})"
    elif result.get("success") and vendor != name:
        result.setdefault("data", {})["served_by"] = vendor
    return result


@web_interruptible
def extract_with_failover(name: str, urls: List[str]) -> List[Dict[str, Any]]:
    """Fails over only when EVERY url in a batch is rate-limit-shaped (partial failures
    are page problems, returned as-is)."""

    if web_is_interrupted():
        return ExtractFailoverResults(interrupted_extract_results(urls))
    order = _ring_order(name)
    if not order:
        return ExtractFailoverResults(
            [
                {"url": u, "title": "", "content": "",
                 "error": "All keyless web providers are pinned to paid tiers."}
                for u in urls
            ]
        )
    last: List[Dict[str, Any]] = []
    fallback_attempted = False
    for i, vendor in enumerate(order):
        if web_is_interrupted():
            return ExtractFailoverResults(
                interrupted_extract_results(urls, last),
                fallback_attempted=fallback_attempted,
            )
        results = _KEYLESS_EXTRACTORS[vendor](list(urls))
        used_fallback_route = i > 0 or vendor != name
        fallback_attempted = fallback_attempted or used_fallback_route
        fallback_used = False
        for result in results:
            if not isinstance(result, dict):
                continue
            if not result.get("error"):
                result[EXTRACT_SERVED_BY_FIELD] = vendor
                if used_fallback_route:
                    result[EXTRACT_FALLBACK_USED_FIELD] = True
                    fallback_used = True
            if used_fallback_route:
                result[EXTRACT_FALLBACK_ATTEMPTED_FIELD] = True
        batch = ExtractFailoverResults(
            results,
            fallback_attempted=fallback_attempted,
            fallback_used=fallback_used,
        )
        if web_is_interrupted():
            return ExtractFailoverResults(
                interrupted_extract_results(urls, results),
                fallback_attempted=fallback_attempted,
                fallback_used=fallback_used,
            )
        errors = [r.get("error", "") for r in results]
        all_throttled = bool(results) and all(
            e and _is_rate_limitish(e) for e in errors
        ) and not any(is_extract_refusal(row) for row in results)
        if not all_throttled:
            return batch
        last = batch
        nxt = order[i + 1] if i + 1 < len(order) else None
        if nxt:
            logger.info(
                "keyless %s extract throttled; failing over to %s", vendor, nxt
            )
    return ExtractFailoverResults(
        last,
        fallback_attempted=fallback_attempted,
        fallback_used=False,
    )
