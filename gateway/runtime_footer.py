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

import os
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "


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
    """Drop ``vendor/`` prefix for readability (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    if not model:
        return ""
    return model.rsplit("/", 1)[-1]


def resolve_footer_config(
    user_config: dict[str, Any] | None,
    platform_key: str | None = None,
) -> dict[str, Any]:
    """Resolve effective runtime-footer config for *platform_key*.

    Merge order (later wins):
        1. Built-in defaults (enabled=False)
        2. ``display.runtime_footer``
        3. ``display.platforms.<platform_key>.runtime_footer``
    """
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}

    global_cfg = cfg.get("runtime_footer")
    if isinstance(global_cfg, dict):
        if "enabled" in global_cfg:
            resolved["enabled"] = bool(global_cfg.get("enabled"))
        if isinstance(global_cfg.get("fields"), list) and global_cfg["fields"]:
            resolved["fields"] = [str(f) for f in global_cfg["fields"]]

    if platform_key:
        platforms = cfg.get("platforms") or {}
        plat_cfg = platforms.get(platform_key)
        if isinstance(plat_cfg, dict):
            plat_footer = plat_cfg.get("runtime_footer")
            if isinstance(plat_footer, dict):
                if "enabled" in plat_footer:
                    resolved["enabled"] = bool(plat_footer.get("enabled"))
                if isinstance(plat_footer.get("fields"), list) and plat_footer["fields"]:
                    resolved["fields"] = [str(f) for f in plat_footer["fields"]]

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
            if context_length and context_length > 0 and context_tokens >= 0:
                pct = max(0, min(100, round((context_tokens / context_length) * 100)))
                parts.append(f"{pct}%")
        elif field == "context_window":
            if context_length and context_length > 0 and context_tokens >= 0:
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
            rel = _home_relative_cwd(cwd or os.environ.get("TERMINAL_CWD", ""))
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
                token_usage_status in {"reported", "reported_partial"}
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
                        if token_usage_status == "reported_partial"
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
        reasoning_effort=reasoning_effort,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
    )
