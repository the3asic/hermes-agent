"""Content-free lifecycle snapshots for the external process observer."""
from __future__ import annotations

import logging
import math
import re
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, List, Optional

from hermes_constants import get_hermes_home

if TYPE_CHECKING:
    from tools.process_registry import ProcessSession

logger = logging.getLogger(__name__)

AGENT_RUNS_OBSERVABILITY_PATH = get_hermes_home() / "runtime" / "agent-runs-observability.json"
AGENT_RUNS_OBSERVABILITY_SCHEMA = "hermes.agent_runs_observability.v1"
AGENT_RUNS_OBSERVATION_SCOPE = "orchestration"
MAX_OBSERVABILITY_RUNS = 64
OBSERVABILITY_OUTPUT_WRITE_INTERVAL_SECONDS = 1.0
_OBSERVABILITY_COMPLETION_REASONS = frozenset({
    "exited", "killed", "lost", "failed_start", "already_exited", "unknown",
})
_OBSERVABILITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_OBSERVABILITY_HOST_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _safe_observability_timestamp(value: Any) -> Optional[float]:
    """Return a finite positive timestamp, never a synthetic zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _safe_observability_exit_code(value: Any) -> Optional[int]:
    """Return a bounded integer exit code, preserving unknown as null."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < -(2 ** 31) or value > (2 ** 31 - 1):
        return None
    return value


def _safe_observability_host_alias(value: Any) -> str:
    """Validate the non-secret orchestration host label used for grouping."""
    if not isinstance(value, str):
        return "unknown"
    alias = value.strip()
    if not _OBSERVABILITY_HOST_ALIAS_RE.fullmatch(alias):
        return "unknown"
    return alias


def _default_orchestration_host_alias() -> str:
    try:
        return _safe_observability_host_alias(socket.gethostname())
    except Exception:
        return "unknown"


class ProcessObservabilityMixin:
    def _init_observability(self) -> None:
        self._snapshot_write_lock = threading.Lock()
        self._snapshot_rate_limit_lock = threading.Lock()
        self._snapshot_last_write_monotonic = 0.0
        self._orchestration_host_alias = _default_orchestration_host_alias()

    def _record_output_observation(self, session: ProcessSession, chunk: str) -> None:
        """Record content-free output availability and refresh the snapshot."""
        if not chunk:
            return
        with session._lock:
            first_output = session.last_output_at is None
            session.last_output_at = time.time()
            session.output_history_available = True
        # First output is useful immediately. After that, cap snapshot writes so
        # a noisy process cannot turn every pipe chunk into a filesystem write.
        # Terminal transitions always force a final snapshot separately.
        should_write = False
        with self._snapshot_rate_limit_lock:
            now = time.monotonic()
            if (
                first_output
                or now - self._snapshot_last_write_monotonic
                >= OBSERVABILITY_OUTPUT_WRITE_INTERVAL_SECONDS
            ):
                # Reserve this interval before releasing the lock. Without the
                # reservation, concurrent reader threads can all observe the
                # same stale timestamp and serialize a burst of redundant
                # snapshot writes through _snapshot_write_lock.
                self._snapshot_last_write_monotonic = now
                should_write = True
        if should_write:
            self._write_observability_snapshot()


    @staticmethod
    def _observability_run_entry(session: ProcessSession) -> dict:
        """Build one bounded, content-free orchestration lifecycle record."""
        proc_id = session.id if (
            isinstance(session.id, str)
            and _OBSERVABILITY_ID_RE.fullmatch(session.id)
        ) else "unknown"
        state = "finished" if session.exited else "running"
        reason = str(session.completion_reason or "unknown").lower()
        if not session.exited or reason not in _OBSERVABILITY_COMPLETION_REASONS:
            reason = None if not session.exited else "unknown"

        with session._lock:
            started_at = _safe_observability_timestamp(session.started_at)
            ended_at = (
                _safe_observability_timestamp(session.ended_at)
                if session.exited else None
            )
            last_output_at = _safe_observability_timestamp(session.last_output_at)
            history_available = session.output_history_available
            if not isinstance(history_available, bool):
                history_available = None
            output_available = None
            if history_available is True:
                output_available = bool(session.output_buffer)

        return {
            "proc_id": proc_id,
            "orchestration_state": state,
            "started_at": started_at,
            "ended_at": ended_at,
            "exit_code": (
                _safe_observability_exit_code(session.exit_code)
                if session.exited else None
            ),
            "completion_reason": reason,
            "last_output_at": last_output_at,
            "last_output_available": output_available,
            "output_history_available": history_available,
        }


    def _write_observability_snapshot(
        self,
        *,
        extra_sessions: Optional[List[ProcessSession]] = None,
    ) -> None:
        """Atomically publish the ProcessRegistry-owned read-only snapshot.

        The snapshot describes orchestration only.  It intentionally carries
        neither a PID nor resource counters, because a local PID may be an SSH
        wrapper while execution happens on another machine.  A separate remote
        observer joins on the opaque ``proc_id`` and remains authoritative for
        remote execution identity and resource use.
        """
        try:
            with self._snapshot_write_lock:
                with self._lock:
                    tracked = list(self._running.values()) + list(self._finished.values())
                by_id = {session.id: session for session in tracked}
                for session in extra_sessions or ():
                    by_id.setdefault(session.id, session)
                sessions = sorted(
                    by_id.values(),
                    key=lambda item: (
                        _safe_observability_timestamp(item.started_at) or 0.0,
                        str(item.id),
                    ),
                )[-MAX_OBSERVABILITY_RUNS:]

                generated_at = time.time()
                snapshot = {
                    "schema_version": AGENT_RUNS_OBSERVABILITY_SCHEMA,
                    "observation_scope": AGENT_RUNS_OBSERVATION_SCOPE,
                    "orchestration_host_alias": _safe_observability_host_alias(
                        self._orchestration_host_alias
                    ),
                    "generated_at": generated_at,
                    "freshness": {
                        "observed_at": generated_at,
                        "max_age_seconds": None,
                    },
                    "runs": [self._observability_run_entry(session) for session in sessions],
                }

                path = AGENT_RUNS_OBSERVABILITY_PATH
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    path.parent.chmod(0o700)
                except OSError:
                    pass
                from utils import atomic_json_write
                atomic_json_write(path, snapshot, mode=0o600, sort_keys=True)
                self._snapshot_last_write_monotonic = time.monotonic()
        except Exception as exc:
            logger.debug(
                "Failed to write agent-runs observability snapshot: %s",
                exc,
                exc_info=True,
            )
