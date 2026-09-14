"""Route-owned reasoning policy for gateway agents."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

logger = logging.getLogger(__name__)


def _install_gateway_turn_reasoning_resolver(
    runner: "GatewayRunner",
    agent: Any,
    *,
    source: "SessionSource",
    session_key: Optional[str],
) -> None:
    """Install the active-runtime effort resolver for this gateway turn.

    A cached agent can remain on its previous fallback while the primary is in
    cooldown. The gateway initially resolves routing from the requested primary,
    so the canonical turn prologue invokes this resolver *after* its single
    ``_restore_primary_runtime()`` call. Primary turns honor session > model >
    global precedence. A fallback kept active by cooldown instead uses its own
    entry pin > target-model > global policy, never the primary session effort.
    """

    def _resolve(active_model: str = "") -> dict | None:
        active_fallback = getattr(agent, "_runtime_reasoning_entry", None)
        resolved = _resolve_gateway_reasoning_for_route(
            runner,
            source=source,
            session_key=session_key,
            model=active_model,
            fallback_entry=active_fallback,
        )
        runner._reasoning_config = resolved
        return resolved

    # Refreshed every turn on cached agents, so it cannot retain a stale
    # session/source binding.
    agent._gateway_reasoning_config_resolver = _resolve


def _resolve_gateway_reasoning_for_route(
    runner: "GatewayRunner",
    *,
    source: Optional["SessionSource"],
    session_key: Optional[str],
    model: str,
    fallback_entry: Any = None,
) -> dict | None:
    """Resolve primary/session or target-owned fallback effort for one route."""
    if isinstance(fallback_entry, dict):
        from gateway.run import _load_gateway_runtime_config
        from hermes_constants import resolve_fallback_reasoning_config

        try:
            config = _load_gateway_runtime_config()
        except Exception:
            # A fallback must never inherit the failed primary/session effort
            # merely because config reload failed. The entry pin can still be
            # resolved against an empty config; if that also fails, use the
            # provider default rather than crossing the policy boundary.
            logger.debug(
                "Fallback reasoning config reload failed; resolving entry "
                "against provider defaults",
                exc_info=True,
            )
            config = {}
        try:
            return resolve_fallback_reasoning_config(
                config,
                model,
                fallback_entry,
            )
        except Exception:
            logger.debug(
                "Fallback reasoning policy failed for model %s; using provider default",
                model,
                exc_info=True,
            )
            return None
    return runner._resolve_session_reasoning_config(
        source=source,
        session_key=session_key,
        model=model,
    )


def _apply_route_reasoning_policy_to_agent(
    agent: Any,
    fallback_entry: Any,
    reasoning_config: Any,
) -> None:
    """Align one route agent's durable reasoning-policy provenance.

    A live, cooldown-held in-turn fallback keeps its active entry when the
    requested route still describes the primary. Otherwise a non-fallback
    route clears stale provenance so a same-runtime session/model override
    cannot accidentally retain the previous fallback's effort.
    """
    primary_runtime = getattr(agent, "_primary_runtime", None)
    if isinstance(fallback_entry, dict):
        agent._runtime_reasoning_entry = dict(fallback_entry)
        if isinstance(primary_runtime, dict):
            primary_runtime["reasoning_policy_entry"] = dict(fallback_entry)
            primary_runtime["reasoning_config"] = (
                dict(reasoning_config)
                if isinstance(reasoning_config, dict)
                else None
            )
        return

    if (
        getattr(agent, "_fallback_activated", False)
        and isinstance(getattr(agent, "_active_fallback_entry", None), dict)
    ):
        # restore_primary_runtime() has not yet decided whether the primary is
        # available this turn. Preserve the active fallback until that single
        # canonical restore attempt settles in build_turn_context().
        return

    agent._runtime_reasoning_entry = None
    if isinstance(primary_runtime, dict):
        primary_runtime["reasoning_policy_entry"] = None
        primary_runtime["reasoning_config"] = (
            dict(reasoning_config)
            if isinstance(reasoning_config, dict)
            else None
        )
