"""Result caching for web_search / web_extract; both caches TTL-bounded (default 20 min,
``web.cache_ttl_minutes``; disable with ``web.cache_enabled: false``), only successful responses cache.
* Search memo: in-memory, single-flighted, keyed by provider, normalized query and bucketed limit.
  Retrieval time and cache age are retained; credential, locale and config omissions are disclosed.
* Extract cache: disk-backed under ``cache/web`` with a JSON index recording source retrieval time,
  cache write time, serving provider and final URL. Hits re-run the caller's truncate pipeline.
Lives here, not in tool dispatch, so hits sit after every safety check and skip only the vendor call.
"""

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Requested limits round UP to a bucket so cache keys collide on purpose.
_LIMIT_BUCKETS = (10, 20, 50, 100)

DEFAULT_TTL_MINUTES = 20

_INDEX_FILENAME = "extract-index.json"
_INDEX_MAX_ENTRIES = 500  # oldest entries evicted past this

# Persistent cache timestamps use wall time. Reject entries that are more than
# a small scheduling/clock-read tolerance into the future rather than letting
# them remain "fresh" indefinitely after a bad clock or sidecar corruption.
_MAX_WALL_CLOCK_SKEW_SECONDS = 5.0


def _web_config() -> dict:
    try:
        from tools.web_tools import _load_web_config
        return _load_web_config()
    except Exception:  # noqa: BLE001 — config problems must never break tools
        return {}


def cache_enabled() -> bool:
    """Both caches honor ``web.cache_enabled`` (default: on)."""
    return True if (val := _web_config().get("cache_enabled")) is None else bool(val)


def ttl_seconds() -> float:
    """TTL from ``web.cache_ttl_minutes`` (default 20, clamped 1–1440)."""
    raw = _web_config().get("cache_ttl_minutes")
    try:
        minutes = float(raw) if raw is not None else DEFAULT_TTL_MINUTES
    except (TypeError, ValueError):
        minutes = DEFAULT_TTL_MINUTES
    return max(1.0, min(minutes, 1440.0)) * 60.0


def bucket_limit(limit: int) -> int:
    """Round a requested result count up to the nearest bucket."""
    return next((b for b in _LIMIT_BUCKETS if limit <= b), _LIMIT_BUCKETS[-1])


def normalize_query(query: str) -> str:
    """Case-fold and collapse whitespace so trivial variants share an entry."""
    return re.sub(r"\s+", " ", (query or "").strip().lower())


def _host_slug(url: str) -> str:
    """Filesystem-safe hostname slug for cache filenames (``"page"`` when hostless). Shared with
    tools.web_tools_truncate."""
    host = (urlparse(url).hostname or "page").replace(":", "_")
    return re.sub(r"[^A-Za-z0-9._-]", "-", host)[:60].strip("-") or "page"


def _deep_copy(response: dict) -> dict:
    """Defensive copy so callers mutating a hit never corrupt the cached entry."""
    return json.loads(json.dumps(response))


# ─── Search memo (in-memory, single-flight) ───────────────────────────────────


_RUNTIME_PROVENANCE_FIELDS = frozenset(
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
        "upstream_cache_timestamp_status",
    }
)


def utc_now_iso() -> str:
    """Return the current wall time as an explicit UTC ISO-8601 string."""
    return (
        datetime.fromtimestamp(time.time(), timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _normalized_utc_timestamp(value: Any) -> Optional[tuple[str, float]]:
    """Return ``(UTC RFC3339 seconds, epoch)`` for an aware finite timestamp."""
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        epoch = parsed.timestamp()
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(epoch):
        return None
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        epoch,
    )


def _cacheable_search_response(response: dict) -> dict:
    """Copy a successful provider response without request-time provenance.

    Search providers may contribute stable provenance such as engine identity,
    source-date semantics, upstream cache reporting, limitations, and
    transformations.  Hermes-owned request facts are recomputed for every
    caller.  In particular, a cached ``served_at`` or ``cache.status=miss``
    would lie to the next caller, so those fields never enter the memo.
    """
    copied = json.loads(json.dumps(response))
    data = copied.get("data")
    if not isinstance(data, dict):
        return copied
    provenance = data.get("provenance")
    if not isinstance(provenance, dict):
        return copied
    cleaned = {
        key: value
        for key, value in provenance.items()
        if key not in _RUNTIME_PROVENANCE_FIELDS
    }
    if cleaned:
        data["provenance"] = cleaned
    else:
        data.pop("provenance", None)
    return copied


@dataclass(frozen=True)
class _SearchMemoEntry:
    response: dict
    retrieved_at: str
    stored_wall_time: float
    stored_monotonic: float
    effective_ttl_seconds: float
    expires_monotonic: float


class SearchMemo:
    """TTL memo + single-flight coalescer for search responses. Thread-safe: the parallel tool-dispatch pool
    and subagents share this process, so identical queries genuinely race; per-key locks make the losers
    wait for (and share) the winner's response."""

    def __init__(self) -> None:
        self._store: Dict[tuple, _SearchMemoEntry] = {}
        self._store_lock = threading.Lock()
        self._key_locks: Dict[tuple, threading.Lock] = {}

    @staticmethod
    def _key(provider: str, query: str, limit: int) -> tuple:
        return (provider, normalize_query(query), bucket_limit(limit))

    def lookup(self, provider: str, query: str, limit: int) -> Optional[dict]:
        """Return a defensive response copy, preserving the legacy API."""
        response, _metadata = self.lookup_with_metadata(provider, query, limit)
        return response

    def lookup_with_metadata(
        self, provider: str, query: str, limit: int
    ) -> tuple[Optional[dict], Optional[dict]]:
        """Return a response plus truthful, dynamically calculated hit data.

        ``age_seconds`` uses the monotonic clock, so wall-clock adjustments
        cannot make a hit younger or older. ``retrieved_at`` remains the wall
        time of the original provider response, not the time of this lookup.
        """
        if not cache_enabled():
            return None, None
        key = self._key(provider, query, limit)
        now = time.monotonic()
        with self._store_lock:
            entry = self._store.get(key)
            if entry is None:
                return None, None
            if now >= entry.expires_monotonic:
                del self._store[key]
                return None, None
            response = json.loads(json.dumps(entry.response))
            metadata = {
                "retrieved_at": entry.retrieved_at,
                "stored_wall_time": entry.stored_wall_time,
                "age_seconds": round(
                    max(0.0, now - entry.stored_monotonic), 3
                ),
                "ttl_seconds": entry.effective_ttl_seconds,
            }
        logger.info("web_search cache hit: %r via %s", query, provider)
        return response, metadata

    def store(self, provider: str, query: str, limit: int, response: dict) -> None:
        """Cache a SUCCESSFUL response, preserving the legacy ``None`` return."""
        self.store_with_metadata(provider, query, limit, response)

    def store_with_metadata(
        self,
        provider: str,
        query: str,
        limit: int,
        response: dict,
        *,
        retrieved_at: Optional[str] = None,
    ) -> Optional[dict]:
        """Cache a successful response and return its immutable timing facts."""
        if not cache_enabled():
            return None
        if not isinstance(response, dict) or response.get("success") is not True:
            return None
        data = response.get("data")
        web = data.get("web") if isinstance(data, dict) else None
        if not isinstance(web, list) or any(
            not isinstance(item, dict) for item in web
        ):
            return None
        key = self._key(provider, query, limit)
        stored_monotonic = time.monotonic()
        stored_wall_time = time.time()
        effective_ttl = ttl_seconds()
        effective_retrieved_at = (
            retrieved_at
            if isinstance(retrieved_at, str) and retrieved_at.strip()
            else datetime.fromtimestamp(stored_wall_time, timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        entry = _SearchMemoEntry(
            response=_cacheable_search_response(response),
            retrieved_at=effective_retrieved_at,
            stored_wall_time=stored_wall_time,
            stored_monotonic=stored_monotonic,
            effective_ttl_seconds=effective_ttl,
            expires_monotonic=stored_monotonic + effective_ttl,
        )
        with self._store_lock:
            # Opportunistic expiry sweep to bound memory.
            for k in [
                k
                for k, existing in self._store.items()
                if stored_monotonic >= existing.expires_monotonic
            ]:
                del self._store[k]
            self._store[key] = entry
        return {
            "retrieved_at": entry.retrieved_at,
            "stored_wall_time": entry.stored_wall_time,
            "age_seconds": 0.0,
            "ttl_seconds": entry.effective_ttl_seconds,
        }

    def flight_lock(self, provider: str, query: str, limit: int) -> threading.Lock:
        """Per-key lock held around lookup-miss → paid request → store."""
        key = self._key(provider, query, limit)
        with self._store_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                # Bound the lock table, but never evict a HELD lock: dropping one lets a concurrent
                # identical request mint a fresh lock and issue a duplicate paid call. locked() is a
                # safe snapshot under _store_lock because holders already have their reference.
                # See #94618.
                if len(self._key_locks) > 256:
                    self._key_locks = {k: v for k, v in self._key_locks.items() if v.locked()}
                lock = self._key_locks[key] = threading.Lock()
            return lock

    def clear(self) -> None:
        """Drop all cached entries (tests; config changes)."""
        with self._store_lock:
            self._store.clear()
            self._key_locks.clear()


search_memo = SearchMemo()


def slice_search_response(response: dict, limit: int) -> dict:
    """Trim a bucketed response's result list down to the caller's limit."""
    try:
        web = response.get("data", {}).get("web")
        if isinstance(web, list) and len(web) > limit:
            out = _deep_copy(response)
            out["data"]["web"] = out["data"]["web"][:limit]
            return out
    except Exception:  # noqa: BLE001
        pass
    return response


# ─── Extract cache (disk-backed, reuses cache/web) ────────────────────────────

_index_lock = threading.Lock()


def _cache_dir() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_dir
        d = get_hermes_dir("cache/web", "web_cache")
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:  # noqa: BLE001
        return None


def _load_index() -> dict:
    try:
        data = json.loads((_cache_dir() / _INDEX_FILENAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt index == empty cache
        return {}


def _save_index(index: dict) -> None:
    if (d := _cache_dir()) is None:
        return
    path = d / _INDEX_FILENAME
    try:
        if len(index) > _INDEX_MAX_ENTRIES:
            newest = sorted(index.items(), key=lambda kv: kv[1].get("fetched_at", 0), reverse=True)
            index = dict(newest[:_INDEX_MAX_ENTRIES])
        # Per-process tmp name: CLI, gateway, cron, and subagents all write this index; a shared tmp name
        # would let concurrent writers truncate each other. os.replace is atomic: worst case is a lost insert.
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(index), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to save web extract cache index: %s", exc)


def _url_digest(url: str, format: Optional[str], provider: str = "") -> str:
    # format AND provider are part of the key: html != markdown, and one backend's rendering is not another's.
    raw = f"{url}\n{format or 'markdown'}\n{provider or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _entry_file_path(url: str, format: Optional[str], provider: str) -> Optional[Path]:
    """Dedicated cache file per (url, format, provider) — deliberately NOT the truncate-store file
    (keyed on URL alone), which html/markdown or two providers' copies of one URL would overwrite.

    The truncate-store file keeps its role for read_file paging; these files exist only for cache reuse and
    carry the full key in their name. See #94618.
    """
    if (d := _cache_dir()) is None:
        return None
    slug = "page"
    with suppress(Exception):
        slug = _host_slug(url)
    return d / f"{slug}-{_url_digest(url, format, provider)}.cache.md"


def _host_matches_pattern(host: str, pattern: str) -> bool:
    """Case-insensitive: exact, ``*.wildcard``, or bare-domain suffix
    (``mysite.dev`` also matches ``preview.mysite.dev``)."""
    host = host.lower().strip(".")
    pattern = (pattern or "").lower().strip().strip(".").removeprefix("*.")
    return bool(pattern) and (host == pattern or host.endswith("." + pattern))


def _is_cache_exempt_host(url: str) -> bool:
    """True when the host matches ``web.cache_exempt_hosts`` — sites the user develops over public DNS
    (staging, tunnels, previews) that must fetch live."""
    try:
        patterns = _web_config().get("cache_exempt_hosts") or []
        host = (urlparse(url).hostname or "").strip("[]")
        if not isinstance(patterns, (list, tuple)) or not host:
            return False
        return any(_host_matches_pattern(host, str(p)) for p in patterns)
    except Exception:  # noqa: BLE001 — config problems never break tools
        return False


def _is_local_dev_url(url: str) -> bool:
    """True for loopback/private/LAN URLs — never cached: they are the user's own fast-changing dev servers.
    Hostname heuristics only, no DNS: this is a freshness decision, not a security boundary (SSRF enforcement
    lives in tools/url_safety.py, which blocks these by default anyway)."""
    try:
        host = (urlparse(url).hostname or "").strip("[]").lower()
        # Unparseable → don't cache; single-label (no "." / ":") == LAN name, not public DNS.
        if not host or host == "localhost" or host.endswith((".localhost", ".local")):
            return True
        if "." not in host and ":" not in host:
            return True
        import ipaddress
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False  # public DNS name
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified
    except Exception:  # noqa: BLE001 — on doubt, don't cache
        return True


def _cacheable(url: str) -> bool:
    """Extract-cache gate: enabled, not a local-dev host, not user-exempted."""
    return cache_enabled() and not (_is_local_dev_url(url) or _is_cache_exempt_host(url))


def extract_cache_get(
    url: str,
    format: Optional[str] = None,
    provider: str = "",
) -> Optional[dict]:
    """Return a fresh cached page plus source provenance, else None.

    Provider-specific entries written before serving-vendor/retrieval-time
    provenance was added are deliberately treated as misses: an old key cannot
    prove that a keyless failover did not populate it under the requested
    provider's name or preserve the source-retrieval time.
    """
    if not _cacheable(url):
        return None
    with _index_lock:
        index = _load_index()
        entry = index.get(_url_digest(url, format, provider))
    if not entry:
        return None
    try:
        fetched_at = float(entry.get("fetched_at", 0))
    except (TypeError, ValueError):
        return None
    now = time.time()
    age_seconds = now - fetched_at
    if (
        not math.isfinite(fetched_at)
        or not math.isfinite(age_seconds)
        or age_seconds < -_MAX_WALL_CLOCK_SKEW_SECONDS
        or age_seconds >= ttl_seconds()
    ):
        return None
    served_by = entry.get("served_by")
    if provider:
        if (
            not isinstance(served_by, str)
            or served_by.strip() != provider.strip()
        ):
            return None
    final_url = entry.get("final_url")
    # Old Firecrawl entries cannot prove which redirect destination supplied
    # their content, so they cannot safely survive a website-policy change.
    if provider == "firecrawl" and (
        not isinstance(final_url, str) or not final_url.strip()
    ):
        return None
    retrieved = _normalized_utc_timestamp(entry.get("retrieved_at"))
    if retrieved is None:
        return None
    retrieved_at, retrieved_epoch = retrieved
    if (
        retrieved_epoch > now + _MAX_WALL_CLOCK_SKEW_SECONDS
        or retrieved_epoch > fetched_at + _MAX_WALL_CLOCK_SKEW_SECONDS
    ):
        return None
    try:
        file_path, cache_root = Path(entry["file"]), _cache_dir()
        # The index is plain JSON on disk; never let a tampered entry read outside cache/web.
        if cache_root.resolve() not in file_path.resolve().parents:
            return None
        content = file_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — evicted/pruned file == miss (or no cache dir)
        return None
    logger.info("web_extract cache hit: %s", url)
    return {
        "url": url,
        "title": entry.get("title", ""),
        "content": content,
        "error": None,
        "cached": True,
        "served_by": served_by or None,
        "retrieved_at": retrieved_at,
        "final_url": final_url or url,
    }


def extract_cache_put(
    url: str,
    content: str,
    title: str = "",
    format: Optional[str] = None,
    provider: str = "",
    served_by: str = "",
    retrieved_at: str = "",
    final_url: Optional[str] = None,
) -> None:
    """Store one successful extraction's full clean text for TTL reuse; pages over the truncate-store
    ceiling are not cached (serving a capped copy back as if whole would silently lose the tail)."""
    if not content or not _cacheable(url):
        return
    try:
        stored_at = time.time()
        if not math.isfinite(stored_at):
            return
        serving_provider = (served_by or provider or "").strip()
        if provider and serving_provider != provider.strip():
            return
        if retrieved_at:
            retrieved = _normalized_utc_timestamp(retrieved_at)
            if retrieved is None:
                return
            normalized_retrieved_at, retrieved_epoch = retrieved
            if retrieved_epoch > stored_at + _MAX_WALL_CLOCK_SKEW_SECONDS:
                return
        else:
            normalized_retrieved_at = (
                datetime.fromtimestamp(stored_at, timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        from tools.web_tools_truncate import MAX_STORED_TEXT_CHARS
        file_path = _entry_file_path(url, format, provider)
        if len(content) > MAX_STORED_TEXT_CHARS or file_path is None:
            return
        from tools.spill_safety import write_text_exclusive
        write_text_exclusive(file_path, content, private=False, overwrite=True)
        with _index_lock:
            index = _load_index()
            index[_url_digest(url, format, provider)] = {
                "url": url,
                "file": str(file_path),
                "title": title or "",
                "fetched_at": stored_at,
                "retrieved_at": normalized_retrieved_at,
                "served_by": serving_provider or None,
                "final_url": final_url or url,
            }
            _save_index(index)
    except Exception as exc:  # noqa: BLE001 — cache writes are best-effort
        logger.debug("Failed to cache web extract for %s: %s", url, exc)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Any  # noqa: F401,E402
from typing import List  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
