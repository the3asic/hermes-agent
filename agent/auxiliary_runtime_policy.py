"""Route-owned reasoning and immutable fallback policy for auxiliary requests."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


def without_route_reasoning(body: dict[str, Any]) -> dict[str, Any]:
    """Remove the failed route's provider-specific reasoning controls before projecting the target."""
    from agent.auxiliary_profile_gates import PROFILE_REASONING_KEYS

    return {key: value for key, value in body.items() if str(key).strip().lower() not in PROFILE_REASONING_KEYS}


def fallback_request_controls(
    task: str | None, destination: Any, label: str, *, max_tokens: int | None,
    effective_extra_body: dict[str, Any], reasoning_config: dict[str, Any] | None,
    fallback_route_ref: Any = None, apply_fast_lane: bool = True,
    reasoning_is_pinned: bool = False,
) -> tuple[int | None, dict[str, Any], dict[str, Any] | None]:
    """Build target-owned compression controls for initial, retry and pinned requests."""
    from agent import auxiliary_client as aux

    task_config = aux._get_auxiliary_task_config(task) if task == "compression" else {}
    entry = (dict(fallback_route_ref.entry) if isinstance(fallback_route_ref, aux._FallbackRouteRef)
             else (aux._fallback_chain_entry(task, label) or {}))
    body = dict(effective_extra_body)
    if task == "compression":
        if not reasoning_is_pinned and (fallback_route_ref is not None or label):
            reasoning_config = aux._resolve_auxiliary_fallback_reasoning(
                task, label, model=destination.model, fallback_route_ref=fallback_route_ref)
        body = without_route_reasoning(body)
    if apply_fast_lane:
        max_tokens, body = aux._compression_fast_lane_controls(
            task, actual_provider=destination.provider, actual_model=destination.model,
            requested_provider=entry.get("provider"), requested_model=entry.get("model"),
            route_config=entry, leak_guard_config=task_config, max_tokens=max_tokens, extra_body=body)
    return max_tokens, body, copy.deepcopy(reasoning_config)


def fallback_policy_cache_fingerprint(task: str | None) -> bytes:
    """Invalidate auto clients when fallback entries, credentials or reasoning defaults change."""
    from agent import auxiliary_client as aux
    from hermes_cli.config import load_config_readonly
    from hermes_cli.fallback_config import get_fallback_chain

    # A failed read cannot validate a previously cached route snapshot.
    config = load_config_readonly()
    task_config = aux._get_auxiliary_task_config(task) if task else {}
    main_chain = get_fallback_chain(config)
    task_chain = task_config.get("fallback_chain") or []
    credentials = [aux._fallback_entry_api_key(entry)
                   for chain in (main_chain, task_chain) for entry in chain if isinstance(entry, dict)]
    policy = {"task": task_config, "main_chain": main_chain, "credentials": credentials,
              "agent": config.get("agent"), "model": config.get("model"),
              "providers": config.get("providers"), "custom_providers": config.get("custom_providers"),
              "model_overrides": config.get("model_overrides")}
    return hashlib.blake2b(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode(),
                           digest_size=16).digest()


def compression_fallback_route(index: int, entry: dict[str, Any]) -> dict[str, Any]:
    """Freeze a host-stall candidate before its retry worker or plugin leaf calls are scheduled."""
    from agent import auxiliary_client as aux

    ref = aux._fallback_route_ref("auxiliary", index, entry)
    snapshot = ref.entry
    provider = str(snapshot.get("provider") or "").strip()
    model = str(snapshot.get("model") or "").strip()
    if not provider or not model:
        raise ValueError("A compression fallback requires a provider and model")
    label = f"fallback_chain[{index}]({provider})"
    return {
        "label": label, "provider": provider, "model": model,
        "base_url": str(snapshot.get("base_url") or "").strip() or None,
        "api_key": aux._fallback_entry_api_key(dict(snapshot)) or None,
        "api_mode": str(snapshot.get("api_mode") or snapshot.get("transport") or "").strip() or None,
        "timeout": aux._coerce_positive_timeout(snapshot.get("timeout")),
        "fallback_route_ref": ref,
        "reasoning_config": aux._resolve_auxiliary_fallback_reasoning("compression", label, model=model, fallback_route_ref=ref),
    }
