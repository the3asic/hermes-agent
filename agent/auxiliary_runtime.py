"""Session-owned runtime snapshots for auxiliary work."""
import copy
from typing import Any


def current_main_runtime(agent) -> dict[str, Any]:
    """Freeze the live session identity and effort before starting auxiliary work."""
    runtime = {
        key: getattr(agent, key, '') or ''
        for key in ('model', 'provider', 'base_url', 'api_key', 'api_mode', 'auth_mode', 'session_id')
    }
    runtime['reasoning_config'] = copy.deepcopy(getattr(agent, 'reasoning_config', None))
    return runtime


def _aux_route_pins_target(resolved_provider: Optional[str], resolved_model: Optional[str]) -> bool:
    """Whether task config pinned an explicit provider/model for this call.

    A pinned target (``auxiliary.<task>.provider/model``, a summary_model override, or a
    per-call ``model=``) owns its own reasoning policy; only a route left to ``auto``
    inherits the session's.
    """
    return (
        str(resolved_provider or "").strip().lower() not in {"", "auto", "main"}
        or bool(str(resolved_model or "").strip())
    )


def _aux_route_matches_main_runtime(
    request_provider: Optional[str], final_model: Optional[str], base_url: str,
    main_runtime: Dict[str, Any],
) -> bool:
    """Whether the concrete auxiliary route IS the session's main deployment.

    Provider and model must match; base_url only disambiguates when both sides carry one
    (default-OpenAI sessions have no explicit URL on either side). Deployment identity is
    compared from normalized route fields — anything stricter would silently disable
    inheritance for default routes.
    """
    def _norm(value: Any) -> str:
        return str(value or "").strip().rstrip("/").lower()

    from agent.auxiliary_client import _normalize_aux_provider, _fallback_provider_from_label

    route_provider = _normalize_aux_provider(
        _fallback_provider_from_label(str(request_provider or "")))
    if route_provider != _normalize_aux_provider(str(main_runtime.get("provider") or "")):
        return False
    if _norm(final_model) != _norm(main_runtime.get("model")):
        return False
    main_base, route_base = _norm(main_runtime.get("base_url")), _norm(base_url)
    return not (main_base and route_base and main_base != route_base)


def inherited_compression_reasoning(
    task, reasoning_config, effective_extra_body, resolved_provider, resolved_model,
    request_provider, final_model, base_url, main_runtime,
):
    """Inherit effort only for an unpinned compression call on its owning route."""
    if (
        task == "compression"
        and reasoning_config is None
        and "reasoning" not in effective_extra_body
        and not _aux_route_pins_target(resolved_provider, resolved_model)
        and _aux_route_matches_main_runtime(
            request_provider, final_model, base_url, main_runtime)
    ):
        inherited = main_runtime.get("reasoning_config")
        if isinstance(inherited, dict):
            reasoning_config = copy.deepcopy(inherited)
    return reasoning_config


def normalize_runtime_fields(main_runtime, fields) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field in fields:
        value = main_runtime.get(field)
        if field == "reasoning_config" and isinstance(value, dict):
            # Deep copy: the snapshot must not mutate under a concurrent in-session effort change.
            normalized[field] = copy.deepcopy(value)
        elif field == "api_key" and callable(value) and not isinstance(value, str):
            normalized[field] = value
        elif isinstance(value, str) and value.strip():
            normalized[field] = value.strip()
    for identity_field in ("provider", "requested_provider"):
        identity = normalized.get(identity_field)
        if isinstance(identity, str):
            normalized[identity_field] = identity.lower()
    return normalized


def responses_reasoning_intent(kwargs, extra_body):
    """Preserve an explicit Responses policy before the chat effort spelling."""
    reasoning = extra_body.get("reasoning")
    if isinstance(reasoning, dict):
        return reasoning
    effort = kwargs.get("reasoning_effort")
    return {"effort": effort} if effort is not None else None
