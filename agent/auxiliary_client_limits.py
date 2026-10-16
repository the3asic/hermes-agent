"""Transport-specific auxiliary output limits, shared by request builders."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping, Optional

from utils import base_url_host_matches


def forwards_max_tokens(
    provider: str, provider_norm: str, model: str, effective_base: str, task: Optional[str],
    api_mode: Optional[str] = None,
) -> bool:
    """Whether an explicit max_tokens is forwarded on this route.

    No default cap elsewhere (omitted = provider default; avoids max_completion_tokens / ZAI-vision
    quirks). Forward only where mandatory or honored: Anthropic Messages wire (400 without it);
    NVIDIA NIM (empty choices[] when omitted); MoA reference slots; Gemini native (fixed 65,535
    ceiling otherwise); OpenRouter (budgets the FULL window when omitted → 402 on low credit);
    managed local llama-server (uncapped decode with no EOS burns the GPU to the context window).
    """
    from agent.auxiliary_client import (
        _is_anthropic_compat_endpoint, _nous_on_messages_wire,
        _NVIDIA_PROVIDER_NAMES, _is_gemini_native_route, _is_managed_local_endpoint,
    )
    return (
        _is_anthropic_compat_endpoint(provider, effective_base)
        or _nous_on_messages_wire(provider_norm, model)
        or provider_norm in _NVIDIA_PROVIDER_NAMES
        or base_url_host_matches(effective_base, "integrate.api.nvidia.com")
        or str(task) == "moa_reference"
        or (task == "compression" and api_mode == "codex_responses")
        or _is_gemini_native_route(provider_norm, effective_base)
        or provider_norm == "openrouter"
        or base_url_host_matches(effective_base, "openrouter.ai")
        or _is_managed_local_endpoint(effective_base)
    )


def responses_output_limits(kwargs: Mapping[str, Any], base_url: str) -> dict[str, Any]:
    """Keep explicit API-key Responses caps; the OAuth Codex backend rejects them."""
    from agent.codex_responses_adapter import classify_responses_route

    native = classify_responses_route(SimpleNamespace(base_url=base_url)).is_codex_backend
    limit = kwargs.get("max_completion_tokens", kwargs.get("max_tokens"))
    return {"max_output_tokens": limit} if limit is not None and not native else {}
