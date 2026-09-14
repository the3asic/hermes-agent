"""Web Search Provider ABC.

The single plugin-facing surface every web provider (brave-free, ddgs, searxng,
exa, parallel, tavily, keenable, firecrawl) implements; registered via
``PluginContext.register_web_search_provider()`` and selected by
``web.search_backend`` / ``web.extract_backend`` / ``web.backend``.

Response shapes (legacy contract, the tool wrapper does not translate)::

This ABC is the SINGLE plugin-facing surface for web providers — built-in
providers in the tree (brave-free, ddgs, searxng, exa, parallel, keenable,
firecrawl, xai) implement it. The legacy in-tree ``tools.web_providers.base``
ABCs were deleted in PR #25182 along with the per-vendor inline helpers
in ``tools/web_tools.py``. Providers keep the compact response contract
documented below; the tool wrapper adds Hermes-owned request provenance to
every successful client-function ``web_search`` response it handles before it
reaches the model. xAI's provider-executed native Responses tool is a separate
surface and does not emit this client-function envelope.

Provider response shape:

Search results::

    {
        "success": True,
        "data": {
            "provenance": {                 # optional provider contribution
                "engine": str,
                "limitations": list[str],
                "transformations": list[str],
            },
            "web": [
                {"title": str, "url": str, "description": str, "position": int},
                ...
            ]
        }
    }

``data.provenance`` is optional at the provider boundary. Providers may put
only facts they can obtain directly from the upstream response there, such as
the engine identity, source-date semantics, an exact upstream cache timestamp,
and provider-side transformations or limitations. An
``upstream_cache_timestamp`` must be a timezone-qualified RFC 3339 date-time.
Hermes does not validate leap-second occurrence tables: any ``time-second=60``
notation is explicitly reported as unsupported rather than accepted or called
invalid.
Hermes validates this boundary and overwrites routing, timestamps,
process-cache state, scope, and result counts at the wrapper. Bare
``confidence``, ``fresh``, ``current``, ``verified``, or ``authoritative``
keys are recursively removed from the successful provider payload; if an
upstream response explicitly reports a related metric, preserve it under a
provider-namespaced key and document its exact semantics.

Extract results::

    {
        "success": True,
        "data": [
            {"url": str, "title": str, "content": str,
             "raw_content": str, "metadata": dict},
            ...
        ]
    }

On failure (either capability)::

    {"success": False, "error": str}

Failed ``web_search`` responses intentionally keep this existing envelope and
do not receive ``data.provenance``.
"""

from __future__ import annotations

import abc
import os
from typing import Any, Dict, List

from agent.provider_base import ProviderBase


def get_provider_env(name: str) -> str:
    """Config-aware env lookup (``os.environ`` first, then ``~/.hermes/.env``) so
    credentials set through the config layer are visible in gateway sessions /
    delegate children / subprocess runs. Stripped value, or ``""`` when unset.

    Falls back to a bare ``os.getenv`` when the config module is unavailable (stripped installs, early
    import contexts). See #40190.
    """
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value(name)
    except Exception:  # noqa: BLE001 — config layer optional here
        val = None
    if val is None:
        val = os.getenv(name, "")
    return (val or "").strip()


class WebSearchProvider(ProviderBase):
    """Abstract base class for a web search/extract backend: implement :meth:`is_available`
    and at least one of :meth:`search` / :meth:`extract`; the ``supports_*`` flags route each capability."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """True when this provider can service calls. Cheap check only (env var, importable
        dep, instance URL) — NO network; runs at tool registration and on every ``hermes tools`` paint."""

    def supports_search(self) -> bool:
        """True if this provider implements :meth:`search`."""
        return True

    def is_keyless_available(self) -> bool:
        """True when this provider can serve calls WITHOUT credentials (public anonymous
        free tiers such as Exa / Parallel MCP); used only when NO provider is configured or
        keyed. Must never make :meth:`is_available` True, or the legacy preference walk would
        route keyed users onto a higher-priority backend's free tier. Cheap, no network."""
        return False

    def supports_extract(self) -> bool:
        """True if this provider implements :meth:`extract` (sync or ``async def`` —
        the dispatcher awaits coroutine functions)."""
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a web search.

        Override when :meth:`supports_search` returns True. The default
        raises NotImplementedError; callers should gate on
        :meth:`supports_search` before calling.

        Every successful client-function ``web_search`` provider response must contain
        ``data.web``. It may also contain provider-owned static facts under
        ``data.provenance``. The wrapper injects the complete request-time
        truth contract into every successful response it handles and gives
        its dynamic fields precedence. Provider ``limitations`` and
        ``transformations`` are merged with wrapper-owned values without
        duplicates; other non-core fields such as ``engine`` and explicit
        source-date semantics are preserved. A provider may supply an RFC 3339
        ``upstream_cache_timestamp`` when the upstream response reports one;
        Hermes validates its supported non-leap-second form and alone derives
        ``upstream_cache_timestamp_status``.

        Providers must not guess freshness or authority. Hermes recursively removes bare
        ``confidence``, ``fresh``, ``current``, ``verified``, or
        ``authoritative`` keys from the successful payload. An explicitly reported upstream metric
        belongs under a provider-namespaced key whose semantics the plugin
        documents.
        """
        raise NotImplementedError(
            f"{self.name} does not support search (override supports_search)"
        )

    def extract(self, urls: List[str], **kwargs: Any) -> Any:
        """Extract content from URLs (callers gate on :meth:`supports_extract`); may be ``async def``.
        Returns ``[{"url", "title", "content", "raw_content", "metadata"?, "error"?}, ...]`` (``error``
        only on per-URL failure). Ignore unknown ``kwargs`` (``format``, ``include_raw``, ``max_chars``)."""
        raise NotImplementedError(
            f"{self.name} does not support extract (override supports_extract)"
        )


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Optional  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
