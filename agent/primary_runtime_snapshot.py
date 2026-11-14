"""The ``_primary_runtime`` snapshot: the route a fallback turn restores on the next turn.

Both builders (agent construction, and a deliberate model switch) record the custom entry that owns
the route's credential, resolved against the config the route runs on. Restoring scopes the
credential pool by that record, never by a live ``config.yaml`` lookup, so an entry edited or
removed mid-session can't hand the session another account's key.
"""

from __future__ import annotations

import copy
from typing import Any

from agent.custom_entry_pool import SNAPSHOT_OWNER_KEY, runtime_entry_owner


def _snapshot_primary_runtime(agent):
    """Capture the preferred route and policy for restoration on the next turn."""
    agent._primary_runtime = _build_primary_runtime_snapshot(agent, agent.api_mode)


def _build_primary_runtime_snapshot(agent, api_mode) -> dict[str, Any]:
    """The ``_primary_runtime`` record that persists a switch across turns."""
    cc = getattr(agent, "context_compressor", None) or None
    reasoning = getattr(agent, "reasoning_config", None)
    policy_entry = getattr(agent, "_runtime_reasoning_entry", None)
    rt = {
        "model": agent.model,
        "provider": agent.provider,
        "requested_provider": agent.requested_provider,
        SNAPSHOT_OWNER_KEY: runtime_entry_owner(agent.provider, agent.requested_provider, agent.base_url),
        "base_url": agent.base_url,
        "api_mode": agent.api_mode,
        "api_key": getattr(agent, "api_key", ""),
        "client_kwargs": dict(agent._client_kwargs),
        "use_prompt_caching": agent._use_prompt_caching,
        "use_native_cache_layout": agent._use_native_cache_layout,
        "reasoning_config": copy.deepcopy(reasoning) if isinstance(reasoning, dict) else None,
        "reasoning_policy_entry": copy.deepcopy(policy_entry) if isinstance(policy_entry, dict) else None,
        "reasoning_echo_flag": getattr(agent, "_reasoning_echo_flag", False),
        # Overrides must travel with the switched-to identity or a later recovery/restore resurrects
        # PRE-switch overrides from the stale init snapshot.
        # See #75091.
        "request_overrides": copy.deepcopy(getattr(agent, "request_overrides", {}) or {}),
        "runtime_capabilities": dict(getattr(agent, "runtime_capabilities", {}) or {}),
        "compressor_model": getattr(cc, "model", agent.model),
        "compressor_base_url": getattr(cc, "base_url", agent.base_url),
        "compressor_api_key": getattr(cc, "api_key", ""),
        "compressor_provider": getattr(cc, "provider", agent.provider),
        "compressor_context_length": cc.context_length if cc else 0,
        "compressor_api_mode": getattr(cc, "api_mode", agent.api_mode),
        "compressor_threshold_tokens": cc.threshold_tokens if cc else 0,
    }
    if api_mode == "anthropic_messages":
        rt.update({
            "anthropic_api_key": agent._anthropic_api_key,
            "anthropic_base_url": agent._anthropic_base_url,
            "is_anthropic_oauth": agent._is_anthropic_oauth,
        })
    return rt
