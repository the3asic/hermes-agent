"""User-supplied CDP endpoint resolution (browser.cdp_url / real-profile), dialog-policy config and the per-task CDP supervisor lifecycle.

Split out of ``tools/browser_tool.py``. Facade-owned state is read through ``_bt`` (``tools.browser_tool``, resolved per call) — no import cycle."""

import contextlib
import os
from typing import Optional, Tuple

from tools.browser_tool_origin import origin_module as _origin
from tools.browser_tool_origin import origin as _bt
from tools import browser_tool_lifecycle as _lifecycle
from tools import browser_tool_session as _session


def _pinned_cdp_target_id(task_id: str) -> Optional[str]:
    """Return agent-browser's active pinned page target for ``task_id``."""
    result = _session._run_browser_command(task_id, "tab", ["list"], timeout=10)
    if not result.get("success"):
        return None
    tabs = result.get("data", {}).get("tabs", [])
    for tab in tabs if isinstance(tabs, list) else []:
        if isinstance(tab, dict) and tab.get("active") and tab.get("type") == "page":
            target_id = str(tab.get("targetId") or "").strip()
            if target_id:
                return target_id
    return None


def _resolve_cdp_override(cdp_url: str) -> str:
    """Normalize a user-supplied CDP endpoint into a concrete websocket URL.

    Full ``ws://.../devtools/browser/...`` endpoints pass through; HTTP discovery roots and bare ``ws://host:port``
    resolve via ``/json/version`` → ``webSocketDebuggerUrl`` (falls back to the raw value with a warning).
    """
    _bt = _origin()
    raw = (cdp_url or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if "/devtools/browser/" in lowered:
        return raw

    discovery_url = raw
    if lowered.startswith(("ws://", "wss://")):
        if not (raw.count(":") == 2 and raw.rstrip("/").rsplit(":", 1)[-1].isdigit() and "/" not in raw.split(":", 2)[-1]):
            return raw
        discovery_url = ("http://" if lowered.startswith("ws://") else "https://") + raw.split("://", 1)[1]
    version_url = discovery_url if discovery_url.lower().endswith("/json/version") else discovery_url.rstrip("/") + "/json/version"

    san = _bt._sanitize_url_for_logs
    try:
        import requests  # lazy — shared module object, test patches still apply
        response = requests.get(version_url, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        _bt.logger.warning("Failed to resolve CDP endpoint %s via %s: %s", san(raw), san(version_url), san(exc))
        return raw
    ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
    if ws_url:
        _bt.logger.info("Resolved CDP endpoint %s -> %s", san(raw), san(ws_url))
        return ws_url
    _bt.logger.warning("CDP discovery at %s did not return webSocketDebuggerUrl; using raw endpoint", san(version_url))
    return raw


def _get_cdp_override_raw() -> str:
    """Return the *configured* CDP override without any network I/O.

    Precedence: ``BROWSER_CDP_URL`` env (live ``/browser connect``), then ``browser.cdp_url``. Is-it-configured
    gates (check_fns, ``_is_local_mode`` / ``_is_local_backend``, ``hermes doctor``) MUST use this, not
    :func:`_get_cdp_override`: its 10s HTTP discovery against a stale ``cdp_url`` would stall every startup's
    schema build with no error.
    """
    env_override = os.environ.get("BROWSER_CDP_URL", "").strip()
    return env_override or _origin()._browser_cfg("cdp_url", "", lambda v: str(v or "").strip(), "browser.cdp_url from config")


def _get_cdp_override() -> str:
    """Resolved CDP URL override, or "" (skips cloud AND local launch).

    May perform HTTP ``/json/version`` discovery — only call on paths about to *connect*; pure gates must use
    :func:`_get_cdp_override_raw`.
    """
    _bt = _origin()
    return _resolve_cdp_override(raw) if (raw := _get_cdp_override_raw()) else ""


def _get_dialog_policy_config() -> Tuple[str, float]:
    """Read ``browser.dialog_policy`` + ``browser.dialog_timeout_s``; supervisor defaults when absent/invalid."""
    _bt = _origin()
    # Deferred so browser_tool imports in minimal environments.
    from tools.browser_supervisor_dialogs import DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S, _VALID_POLICIES
    policy, timeout_s = DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {}) if isinstance(cfg, dict) else {}
        if not isinstance(browser_cfg, dict):
            return policy, timeout_s
        candidate = str(browser_cfg.get("dialog_policy") or DEFAULT_DIALOG_POLICY)
        if candidate in _VALID_POLICIES:
            policy = candidate
        else:
            _bt.logger.debug("Invalid browser.dialog_policy=%r; using default", candidate)
        timeout_raw = browser_cfg.get("dialog_timeout_s")
        try:
            timeout_s = float(timeout_raw) if timeout_raw is not None else DEFAULT_DIALOG_TIMEOUT_S
            if timeout_s <= 0:
                timeout_s = DEFAULT_DIALOG_TIMEOUT_S
        except (TypeError, ValueError):
            timeout_s = DEFAULT_DIALOG_TIMEOUT_S
        return policy, timeout_s
    except Exception:
        return DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S


def _ensure_cdp_supervisor(
    task_id: str,
    target_id: Optional[str] = None,
    *,
    expected_generation: Optional[int] = None,
) -> None:
    """Start a CDP supervisor for ``task_id`` if an endpoint is reachable.

    Idempotent — delegates to ``SupervisorRegistry.get_or_start`` which skips
    when a supervisor for this ``(task_id, cdp_url)`` already exists and
    tears down + restarts on URL change. Safe to call on every
    ``browser_navigate`` / ``/browser connect`` without worrying about
    double-attach.

    Resolves the CDP URL in this order:
      1. ``BROWSER_CDP_URL`` / ``browser.cdp_url`` — covers ``/browser connect``
         and config-set overrides.
      2. ``_active_sessions[task_id]["cdp_url"]`` — covers Browserbase + any
         other cloud provider whose ``create_session`` returns a raw CDP URL.

    A fixed shared-CDP endpoint deliberately waits for ``target_id`` instead
    of adopting its first page. The command path resolves the task-owned
    pinned target before calling again; if pinning never succeeds, supervisor
    features stay unavailable rather than observing another task's page.

    Swallows all errors — failing to attach the supervisor must not break
    the browser session itself.  The agent simply won't see
    ``pending_dialogs`` / ``frame_tree`` fields in snapshots.
    """
    with _bt._cleanup_lock:
        session_info = _bt._active_sessions.get(task_id, {})
        if expected_generation is None:
            raw_generation = session_info.get("_lifecycle_generation")
            if isinstance(raw_generation, int) and not isinstance(raw_generation, bool):
                expected_generation = raw_generation

    cdp_url = _get_cdp_override()
    # A fixed CDP endpoint can already contain unrelated user/task pages. Do
    # not let the supervisor attach to the endpoint's first page before
    # agent-browser has created and pinned this task's own target.
    if cdp_url and target_id is None:
        return
    if not cdp_url:
        # Fallback: active session may carry a per-session CDP URL from a
        # cloud provider (Browserbase sets this).
        maybe = str(session_info.get("cdp_url") or "")
        if maybe:
            cdp_url = _resolve_cdp_override(maybe)
    if not cdp_url:
        return
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]

        policy, timeout_s = _get_dialog_policy_config()

        def _publication_allowed() -> bool:
            bare_task_id = _bt._bare_task_id_for_session_key(task_id)
            with _bt._cleanup_lock:
                current = _bt._active_sessions.get(task_id)
                if current is None:
                    return False
                if _lifecycle._task_state_locked(bare_task_id) in {
                    _lifecycle.BrowserTaskState.RETIRING,
                    _lifecycle.BrowserTaskState.RETIRED,
                }:
                    return False
                if expected_generation is None:
                    return True
                return (
                    _lifecycle._task_generation_locked(bare_task_id) == expected_generation
                    and current.get("_lifecycle_generation") == expected_generation
                )

        SUPERVISOR_REGISTRY.get_or_start(
            task_id=task_id,
            cdp_url=cdp_url,
            target_id=target_id,
            dialog_policy=policy,
            dialog_timeout_s=timeout_s,
            publish_guard=_publication_allowed,
        )
    except Exception as exc:
        _bt.logger.debug(
            "CDP supervisor attach for task=%s failed (non-fatal): %s",
            task_id,
            exc,
        )


def _stop_cdp_supervisor(task_id: str) -> None:
    """Stop the CDP supervisor for ``task_id`` if one exists. No-op otherwise."""
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]
        SUPERVISOR_REGISTRY.stop(task_id)
    except Exception as exc:
        _origin().logger.debug("CDP supervisor stop for task=%s failed (non-fatal): %s", task_id, exc)
