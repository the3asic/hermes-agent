"""Gateway runtime footer with model, usage, context, timing and working-directory fields.

Configure display.runtime_footer globally or display.platforms.<platform>.runtime_footer
per adapter. The default fields are model, context_pct and cwd; /footer toggles display.
Opt-in fields include model_last, served_model, context_window, latency, tokens_in,
tokens_out, tokens_turn, cache_hit and reasoning_effort.

Model and effort describe the final route. Token totals are this turn's provider-reported
uncached input and completion deltas; partial coverage is labelled explicitly. Cache hits
use cache-read / (uncached + cache-read + cache-write). Missing telemetry stays unknown.
Context fields require usage-covered final-call provenance. served_model shows the
requested alias and reported deployment only when they differ.

The final response carries the footer. Streaming sends it after the reply; a footer-only
reply can use the last delivered photo's caption when the adapter's UTF-16 limit permits.
FooteredReply preserves the footer through the adapter's media delivery path.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional
from urllib.parse import quote

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
        requested_model=agent_result.get("requested_model"),
        served_model=agent_result.get("served_model"),
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
    total = round(seconds)
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
    requested_model: Optional[str] = None,
    served_model: Optional[str] = None,
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
        elif field == "served_model":
            requested = requested_model or model
            alias = _model_short(requested)
            if served_model and served_model not in (alias, requested):
                parts.append(f"{alias} → {served_model}")
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
    requested_model: Optional[str] = None,
    served_model: Optional[str] = None,
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
        requested_model=requested_model,
        served_model=served_model,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
    )


class FooteredReply(str):
    """A final reply whose text ends with ``\n\n<runtime_footer>``. Plain text everywhere; any
    rewrite yields a plain ``str`` again, so the footer then simply stays in the text."""
    runtime_footer: str

    def __new__(cls, text: str, footer: str):
        instance = super().__new__(cls, f"{text}\n\n{footer}")
        instance.runtime_footer = footer
        return instance


class _FooterCaption(str):
    """A photo caption that carries the runtime footer (``caption_with_footer``)."""
    runtime_footer: str


def caption_with_footer(adapter: Any, images: list, footer: str) -> tuple[list, bool]:
    """``(images, True)`` with ``footer`` on the last ``(url, alt)`` photo's caption when the
    adapter declares ``media_caption_limit`` and the caption fits it (UTF-16 units). Never on an
    animation: a GIF goes out on its own path, whose failure the batch result does not list."""
    limit = int(getattr(adapter, "media_caption_limit", 0) or 0)
    target = next((i for i in range(len(images) - 1, -1, -1)
                   if not adapter._is_animation_url(images[i][0])), None) if footer and limit > 0 else None
    if target is None:
        return images, False
    url, alt = images[target]
    caption = _FooterCaption(f"{alt}\n\n{footer}" if alt else footer)
    if len(caption.encode("utf-16-le")) // 2 > limit:
        return images, False
    caption.runtime_footer = footer
    return [*images[:target], (url, caption), *images[target + 1:]], True


def caption_landed(result: Any, images: list) -> bool:
    """The photo whose caption carries the footer was delivered, or is queued for a re-upload that
    carries it (``raw_response["deferred"]``: flood control; a separate footer would repeat it).
    ``success`` means at least one photo landed, so that photo listed in ``raw_response["failed"]``
    (``(path_or_url, error)``) took the footer with it. False when no caption carries it."""
    url = next((u for u, alt in images if getattr(alt, "runtime_footer", "")), None)
    raw = getattr(result, "raw_response", None)
    raw = raw if isinstance(raw, dict) else {}
    if url is None or url in (raw.get("deferred") or ()):
        return url is not None
    if not getattr(result, "success", False):
        return False
    from urllib.parse import unquote
    lost = {str(item[0]) for item in raw.get("failed") or () if item}
    return url not in lost and unquote(url.removeprefix("file://")) not in lost


async def send_footer_unless_captioned(adapter: Any, chat_id: str, images: list, result: Any,
                                       metadata: Any, record_delivery: Any) -> None:
    """Non-streamed send path: the footer a photo caption carried, sent as its own message (as
    before #74547) when that photo did not land."""
    footer = next((alt.runtime_footer for _u, alt in images if getattr(alt, "runtime_footer", "")), "")
    if footer and not caption_landed(result, images):
        record_delivery(await adapter._send_with_retry(chat_id=chat_id, content=footer, metadata=metadata))


def footer_onto_photo_caption(adapter: Any, extracted: Any, image_exts: Iterable[str]) -> Any:
    """The adapter send path's ``_ExtractedResponse``, with a footer-only text moved onto the last
    photo's caption (MEDIA photos join the photo batch): otherwise the footer is its own message."""
    footer = getattr(extracted.pre_extract, "runtime_footer", "")
    if not footer or extracted.force_document_attachments or extracted.text_content.strip() != footer.strip():
        return extracted
    photos = [p for p, voice in extracted.media_files if not voice and Path(p).suffix.lower() in image_exts]
    images, attached = caption_with_footer(
        adapter, [*extracted.images, *((f"file://{quote(p)}", "") for p in photos)], footer)
    if not attached:
        return extracted
    return dataclasses.replace(extracted, text_content="", images=images,
                               media_files=[m for m in extracted.media_files if m[0] not in photos])
