"""Gateway runtime-metadata footer.

Renders a compact footer showing runtime state (model, context %, cwd) and
appends it to the FINAL message of an agent turn when enabled.  Off by default
to keep replies minimal.

Config (``~/.hermes/config.yaml``)::

    display:
      runtime_footer:
        enabled: true                       # off by default
        fields: [model, context_pct, cwd]   # order shown; drop any to hide

Available fields:
    model             — bare model id, vendor prefix dropped (``gpt-5.4``)
    model_last        — explicitly labelled final model (``model(last):gpt-5.4``)
    context_pct       — last-call context occupancy as a percent (``5%``)
    context_window    — last-call context used/total plus percent
                        (``ctx(last):123.0k/1.0M (12%)``)
    latency           — wall-clock duration of the turn (``22s``, ``1m05s``)
    cwd               — home-relative working dir (``~``)
    tokens_in         — this turn's summed non-cached provider input (``15.9k in``)
    tokens_out        — this turn's summed provider completion tokens (``1.2k out``)
    tokens_turn       — labelled non-cached turn usage
                        (``tokens(turn,uncached):15.9k in/1.2k out``)
    cache_hit         — this turn's provider-reported prompt cache hit ratio
                        (``cache(turn):87%``)
    reasoning_effort  — final model's request intent (``effort(req,last):max``)

``model_last``, ``latency``, ``tokens_in``, ``tokens_out``, ``tokens_turn``, and
``reasoning_effort`` are opt-in: they are NOT in the default field set, so a
footer whose ``fields`` are unset renders exactly as before.

``model_last`` and ``reasoning_effort`` are deliberately labelled ``last``:
fallback can change both during a turn. The effort is Hermes' request intent
for that final model, not a provider claim about how much reasoning was
ultimately performed. ``tokens_in`` and ``tokens_out`` are known
provider-reported non-cached deltas and can include calls made before a
fallback; they are not the final model's exclusive usage and are not the
cached agent's cumulative session counters. Cached input is excluded from
``tokens_turn`` but remains part of ``context_window`` because cached tokens
still occupy the model context. ``tokens_turn`` becomes
``tokens(turn,uncached,partial)`` when Hermes sees logical calls without usable usage.
``cache_hit`` uses cache-read tokens divided by all prompt-input buckets
(``uncached + cache-read + cache-write``); cache writes are prompt tokens but
are not cache hits. It becomes ``cache(turn,partial)`` under partial coverage.
Providers without trustworthy cache-read telemetry omit it instead of showing
a synthetic 0%. Context fields are likewise omitted unless the gateway proves
the value came from this turn's fully usage-covered final call.
When no usable positive-prompt usage exists, it is skipped rather than shown
as a synthetic zero.

Per-platform overrides live under ``display.platforms.<platform>.runtime_footer``.
Users can toggle the global setting with ``/footer on|off`` from both the CLI
and any gateway platform.

The footer is appended to the final response text in ``gateway/run.py`` right
before returning the response to the adapter send path — so it only lands on
the final message a user sees, not on tool-progress updates or streaming
partials.  When streaming is on and the final text has already been delivered
piecemeal, the delivery path sends the footer as a separate trailing message.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from gateway.session import SessionSource

logger = logging.getLogger(__name__)

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "


def _gateway_turn_runtime_metadata(
    agent: Any,
    *,
    uncached_input_tokens_start: Any,
    completion_tokens_start: Any,
    cache_read_tokens_start: Any,
    cache_write_tokens_start: Any,
    usage_report_calls_start: Any,
    cache_usage_report_calls_start: Any,
    context_usage_report_calls_start: Any,
    result_api_calls: Any,
) -> dict[str, Any]:
    """Snapshot honest per-turn runtime metadata from a gateway agent.

    Agent token counters are cumulative for the lifetime of a cached agent, so
    the visible turn usage is the delta from the snapshot taken immediately
    before ``run_conversation()``.  Model and effort describe the *final* model
    state after any fallback. Token deltas are explicitly provider-reported,
    not a claim about unreported retries or advisor fan-out. When Hermes sees
    logical calls without usable usage, the footer labels the known sum
    ``reported,partial`` rather than presenting it as a complete turn total.
    """
    if agent is None:
        return {}

    compressor = getattr(agent, "context_compressor", None)
    last_prompt_tokens = getattr(agent, "session_last_prompt_tokens", 0) or 0
    context_length = getattr(compressor, "context_length", 0) or 0
    input_tokens = getattr(agent, "session_prompt_tokens", 0) or 0
    uncached_input_tokens = getattr(agent, "session_input_tokens", 0) or 0
    output_tokens = getattr(agent, "session_completion_tokens", 0) or 0
    cache_read_tokens = getattr(agent, "session_cache_read_tokens", 0) or 0
    cache_write_tokens = getattr(agent, "session_cache_write_tokens", 0) or 0
    usage_report_calls = (
        getattr(agent, "session_usage_report_calls", 0) or 0
    )
    cache_usage_report_calls = (
        getattr(agent, "session_cache_usage_report_calls", 0) or 0
    )
    context_usage_report_calls = (
        getattr(agent, "session_context_usage_report_calls", 0) or 0
    )

    turn_input_tokens = turn_counter_delta(
        uncached_input_tokens,
        uncached_input_tokens_start,
    )
    turn_output_tokens = turn_counter_delta(
        output_tokens, completion_tokens_start
    )
    turn_cache_read_tokens = turn_counter_delta(
        cache_read_tokens, cache_read_tokens_start
    )
    turn_cache_write_tokens = turn_counter_delta(
        cache_write_tokens, cache_write_tokens_start
    )
    input_counter_valid = turn_input_tokens is not None
    output_counter_valid = turn_output_tokens is not None
    cache_read_counter_valid = turn_cache_read_tokens is not None
    cache_write_counter_valid = turn_cache_write_tokens is not None
    turn_usage_report_calls = turn_counter_delta(
        usage_report_calls, usage_report_calls_start
    )
    turn_cache_usage_report_calls = turn_counter_delta(
        cache_usage_report_calls,
        cache_usage_report_calls_start,
    )
    turn_context_usage_report_calls = turn_counter_delta(
        context_usage_report_calls,
        context_usage_report_calls_start,
    )
    try:
        expected_api_calls = int(result_api_calls)
    except (TypeError, ValueError):
        expected_api_calls = None
    token_usage_status = None
    if (
        isinstance(turn_usage_report_calls, int)
        and turn_usage_report_calls > 0
        and turn_input_tokens is not None
        and turn_output_tokens is not None
    ):
        token_usage_status = "reported"
        if (
            expected_api_calls is None
            or expected_api_calls < 0
            or turn_usage_report_calls != expected_api_calls
        ):
            token_usage_status = "reported_partial"
    else:
        logger.info(
            "Gateway runtime footer token usage unavailable: usable provider "
            "usage observed for %r of %r logical turn API calls",
            turn_usage_report_calls,
            result_api_calls,
        )
        turn_input_tokens = None
        turn_output_tokens = None
    cache_usage_status = None
    if (
        isinstance(turn_cache_usage_report_calls, int)
        and turn_cache_usage_report_calls > 0
        and turn_cache_read_tokens is not None
        and turn_cache_write_tokens is not None
    ):
        cache_usage_status = "reported"
        if (
            expected_api_calls is None
            or expected_api_calls < 0
            or turn_cache_usage_report_calls != expected_api_calls
        ):
            cache_usage_status = "reported_partial"
    else:
        turn_cache_read_tokens = None
        turn_cache_write_tokens = None
    context_usage_status = None
    if (
        isinstance(turn_context_usage_report_calls, int)
        and turn_context_usage_report_calls > 0
        and expected_api_calls is not None
        and expected_api_calls >= 0
        and turn_context_usage_report_calls == expected_api_calls
        and last_prompt_tokens > 0
    ):
        context_usage_status = "reported"
    if not input_counter_valid:
        logger.warning(
            "Gateway non-cached input-token counter moved backwards or became invalid "
            "during a turn (start=%r current=%r); footer usage unavailable",
            uncached_input_tokens_start,
            uncached_input_tokens,
        )
    if not output_counter_valid:
        logger.warning(
            "Gateway completion-token counter moved backwards or became "
            "invalid during a turn (start=%r current=%r); footer usage unavailable",
            completion_tokens_start,
            output_tokens,
        )
    if not cache_read_counter_valid:
        logger.warning(
            "Gateway cache-read-token counter moved backwards or became invalid "
            "during a turn (start=%r current=%r); footer cache ratio unavailable",
            cache_read_tokens_start,
            cache_read_tokens,
        )
    if not cache_write_counter_valid:
        logger.warning(
            "Gateway cache-write-token counter moved backwards or became invalid "
            "during a turn (start=%r current=%r); footer cache ratio unavailable",
            cache_write_tokens_start,
            cache_write_tokens,
        )

    model_last = getattr(agent, "model", None)
    return {
        "last_prompt_tokens": last_prompt_tokens,
        "input_tokens": input_tokens,
        "uncached_input_tokens": uncached_input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "usage_report_calls": usage_report_calls,
        "cache_usage_report_calls": cache_usage_report_calls,
        "context_usage_report_calls": context_usage_report_calls,
        "turn_input_tokens": turn_input_tokens,
        "turn_output_tokens": turn_output_tokens,
        "turn_cache_read_tokens": turn_cache_read_tokens,
        "turn_cache_write_tokens": turn_cache_write_tokens,
        "token_usage_status": token_usage_status,
        "cache_usage_status": cache_usage_status,
        "context_usage_status": context_usage_status,
        "reasoning_effort": resolved_reasoning_effort(
            getattr(agent, "reasoning_config", None)
        ),
        # Keep the existing key for hooks/session consumers while exposing the
        # explicit name to provenance-aware footer callers.
        "model": model_last,
        "model_last": model_last,
        "context_length": context_length,
    }


def _gateway_runtime_footer_line(
    agent_result: dict[str, Any],
    source: "SessionSource",
    *,
    turn_seconds: Optional[float] = None,
) -> str:
    """Build one turn's configured footer from its finalized result shape."""
    if not isinstance(agent_result, dict):
        return ""
    from gateway.run import _load_gateway_config, _platform_config_key, _terminal_scope_cwd

    result_seconds = agent_result.get("turn_seconds")
    if not isinstance(result_seconds, (int, float)) or isinstance(
        result_seconds, bool
    ):
        result_seconds = turn_seconds
    return build_footer_line(
        user_config=_load_gateway_config(),
        platform_key=_platform_config_key(source.platform),
        model=agent_result.get("model_last") or agent_result.get("model"),
        context_tokens=agent_result.get("last_prompt_tokens", 0) or 0,
        context_length=agent_result.get("context_length") or None,
        cwd=_terminal_scope_cwd(""),
        turn_seconds=result_seconds,
        tokens_in=agent_result.get("turn_input_tokens"),
        tokens_out=agent_result.get("turn_output_tokens"),
        cache_read_tokens=agent_result.get("turn_cache_read_tokens"),
        cache_write_tokens=agent_result.get("turn_cache_write_tokens"),
        token_usage_status=agent_result.get("token_usage_status"),
        cache_usage_status=agent_result.get("cache_usage_status"),
        context_usage_status=agent_result.get("context_usage_status"),
        reasoning_effort=agent_result.get("reasoning_effort"),
    )


def _format_token_count(value: int) -> str:
    """Format a non-negative token count compactly without losing its unit."""
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k"
    return f"{value / 1_000_000:.1f}M"


def turn_counter_delta(current: Any, baseline: Any) -> Optional[int]:
    """Return this turn's non-negative counter delta.

    Cached agents keep cumulative token counters across turns. The counters are
    initialized once and only increment in the current runtime. A decrease is
    therefore an accounting anomaly, not a new generation we can safely infer;
    report the value as unavailable instead of fabricating a delta.
    """
    try:
        current_int = int(current)
        baseline_int = int(baseline)
    except (TypeError, ValueError):
        return None
    if current_int < 0 or baseline_int < 0:
        return None
    if current_int < baseline_int:
        return None
    return current_int - baseline_int


def resolved_reasoning_effort(reasoning_config: Any) -> str:
    """Return Hermes' resolved request effort for display.

    ``default`` means Hermes did not send an explicit level. This is request
    intent only; downstream routers/providers can still translate it.
    """
    if not isinstance(reasoning_config, dict):
        return "default"
    if reasoning_config.get("enabled") is False:
        return "none"
    effort = str(reasoning_config.get("effort") or "").strip().lower()
    return effort or "default"


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``.  Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    return model.rsplit("/", 1)[-1] if model else ""


def _env_cwd() -> str:
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.environ.get("TERMINAL_CWD", "")
    return terminal_env("TERMINAL_CWD", "")


def resolve_footer_config(user_config: dict[str, Any] | None, platform_key: str | None = None) -> dict[str, Any]:
    """Resolve effective footer config: defaults (enabled=False) <
    ``display.runtime_footer`` < ``display.platforms.<platform_key>.runtime_footer``."""
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}
    plat_cfg = (cfg.get("platforms") or {}).get(platform_key) if platform_key else None
    sections = [cfg.get("runtime_footer"), plat_cfg.get("runtime_footer") if isinstance(plat_cfg, dict) else None]
    for section in sections:
        if not isinstance(section, dict):
            continue
        if "enabled" in section:
            resolved["enabled"] = bool(section.get("enabled"))
        if isinstance(section.get("fields"), list) and section["fields"]:
            resolved["fields"] = [str(f) for f in section["fields"]]
    return resolved


def _format_latency(seconds: float) -> str:
    """Humanize a turn duration: ``<1s``, ``22s``, ``1m05s``."""
    if seconds < 1:
        return "<1s"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    m, sec = divmod(total, 60)
    return f"{m}m{sec:02d}s"


def format_runtime_footer(
    *,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    turn_seconds: Optional[float] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    token_usage_status: Optional[str] = None,
    cache_usage_status: Optional[str] = None,
    context_usage_status: Optional[str] = "reported",
    reasoning_effort: Optional[str] = None,
    fields: Iterable[str] = _DEFAULT_FIELDS,
) -> str:
    """Render the footer line, or return "" if no fields have data.

    Fields are skipped silently when their underlying data is missing — a
    partially-populated footer is better than a line with ``?%`` or empty slots.
    """
    parts: list[str] = []
    for field in fields:
        if field == "model":
            m = _model_short(model)
            if m:
                parts.append(m)
        elif field == "model_last":
            m = _model_short(model)
            if m:
                parts.append(f"model(last):{m}")
        elif field == "context_pct":
            if (
                context_usage_status == "reported"
                and context_length
                and context_length > 0
                and context_tokens >= 0
            ):
                pct = max(0, min(100, round((context_tokens / context_length) * 100)))
                parts.append(f"{pct}%")
        elif field == "context_window":
            if (
                context_usage_status == "reported"
                and context_length
                and context_length > 0
                and context_tokens >= 0
            ):
                pct = max(0, min(100, round((context_tokens / context_length) * 100)))
                parts.append(
                    "ctx(last):"
                    f"{_format_token_count(context_tokens)}/"
                    f"{_format_token_count(context_length)} ({pct}%)"
                )
        elif field == "latency":
            # Wall-clock turn duration. Skipped when the caller supplied no
            # timing (call sites that don't measure) or the value is negative.
            if turn_seconds is not None and turn_seconds >= 0:
                parts.append(_format_latency(turn_seconds))
        elif field == "cwd":
            rel = _home_relative_cwd(cwd or _env_cwd())
            if rel:
                parts.append(rel)
        elif field == "tokens_in":
            if (
                token_usage_status == "reported"
                and isinstance(tokens_in, int)
                and not isinstance(tokens_in, bool)
                and tokens_in >= 0
            ):
                parts.append(f"{_format_token_count(tokens_in)} in")
        elif field == "tokens_out":
            if (
                token_usage_status == "reported"
                and isinstance(tokens_out, int)
                and not isinstance(tokens_out, bool)
                and tokens_out >= 0
            ):
                parts.append(f"{_format_token_count(tokens_out)} out")
        elif field == "tokens_turn":
            reported: list[str] = []
            if (
                isinstance(tokens_in, int)
                and not isinstance(tokens_in, bool)
                and tokens_in >= 0
            ):
                reported.append(f"{_format_token_count(tokens_in)} in")
            if (
                isinstance(tokens_out, int)
                and not isinstance(tokens_out, bool)
                and tokens_out >= 0
            ):
                reported.append(f"{_format_token_count(tokens_out)} out")
            if reported and token_usage_status in {
                "reported",
                "reported_partial",
            }:
                label = (
                    "tokens(turn,uncached,partial)"
                    if token_usage_status == "reported_partial"
                    else "tokens(turn,uncached)"
                )
                parts.append(f"{label}:{'/'.join(reported)}")
        elif field == "cache_hit":
            cache_buckets = (tokens_in, cache_read_tokens, cache_write_tokens)
            if (
                cache_usage_status in {"reported", "reported_partial"}
                and all(
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                    for value in cache_buckets
                )
            ):
                prompt_tokens = sum(cache_buckets)
                if prompt_tokens > 0:
                    cache_pct = round((cache_read_tokens / prompt_tokens) * 100)
                    label = (
                        "cache(turn,partial)"
                        if cache_usage_status == "reported_partial"
                        else "cache(turn)"
                    )
                    parts.append(f"{label}:{cache_pct}%")
        elif field == "reasoning_effort":
            if reasoning_effort:
                parts.append(f"effort(req,last):{reasoning_effort}")
        # Unknown field names are silently ignored.

    if not parts:
        return ""

    return _SEP.join(parts)


def build_footer_line(
    *,
    user_config: dict[str, Any] | None,
    platform_key: str | None,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    turn_seconds: Optional[float] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    token_usage_status: Optional[str] = None,
    cache_usage_status: Optional[str] = None,
    context_usage_status: Optional[str] = "reported",
    reasoning_effort: Optional[str] = None,
) -> str:
    """Top-level entry point used by gateway/run.py.

    Returns the footer text (empty string when disabled or no data).  Callers
    append this to the final response themselves, preserving a single blank
    line of separation.

    ``turn_seconds`` is the wall-clock duration of the agent run, measured by
    the caller with ``time.monotonic()``.  Callers that don't measure it leave
    it ``None`` and the ``latency`` field is skipped.
    """
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    return format_runtime_footer(
        model=model,
        context_tokens=context_tokens,
        context_length=context_length,
        cwd=cwd,
        turn_seconds=turn_seconds,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        token_usage_status=token_usage_status,
        cache_usage_status=cache_usage_status,
        context_usage_status=context_usage_status,
        reasoning_effort=reasoning_effort,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
    )
