"""Reuse the gateway's honest usage footer for fresh cron agents."""

from __future__ import annotations

import logging
from typing import Any

from gateway.runtime_footer import (
    _gateway_turn_runtime_metadata,
    build_footer_line,
    resolve_footer_config,
)

logger = logging.getLogger(__name__)


def append_runtime_footer(response: str, agent: Any, result: dict, cfg: dict, *, seconds: float) -> str:
    """Cron creates a fresh agent per fire, so all main-call counter baselines are zero."""
    if not resolve_footer_config(cfg, "cron").get("enabled"):
        return response
    try:
        metadata = _gateway_turn_runtime_metadata(
            agent,
            uncached_input_tokens_start=0,
            completion_tokens_start=0,
            cache_read_tokens_start=0,
            cache_write_tokens_start=0,
            usage_report_calls_start=0,
            cache_usage_report_calls_start=0,
            context_usage_report_calls_start=0,
            result_api_calls=result.get("api_calls"),
        )
        footer = build_footer_line(
            user_config=cfg,
            platform_key="cron",
            model=metadata.get("model_last"),
            context_tokens=metadata.get("last_prompt_tokens", 0),
            context_length=metadata.get("context_length"),
            turn_seconds=seconds,
            tokens_in=metadata.get("turn_input_tokens"),
            tokens_out=metadata.get("turn_output_tokens"),
            cache_read_tokens=metadata.get("turn_cache_read_tokens"),
            cache_write_tokens=metadata.get("turn_cache_write_tokens"),
            token_usage_status=metadata.get("token_usage_status"),
            cache_usage_status=metadata.get("cache_usage_status"),
            context_usage_status=metadata.get("context_usage_status"),
            reasoning_effort=metadata.get("reasoning_effort"),
        )
    except Exception:
        # Optional display metadata must never convert a completed job into a failed run.
        logger.warning("Cron runtime footer could not be built", exc_info=True)
        return response
    return f"{response}\n\n{footer}" if footer else response
