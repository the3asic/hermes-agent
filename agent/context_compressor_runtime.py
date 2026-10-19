"""Bind compression summaries to the runtime of their owning session."""
import copy
from typing import Any


class CompressionRuntimeMixin:
    def _summary_main_runtime(self) -> dict[str, Any]:
        """Runtime snapshot for the next summary call: the owning session's attempt
        snapshot when it matches, else this compressor's construct/update snapshot.

        A reused compressor can outlive a session effort/model switch. The host scopes a
        fresh runtime (``scoped_runtime_main``) around each compression attempt, and that
        ContextVar also isolates a detached pool worker from a later attempt. The session-id
        match refuses a FOREIGN session's runtime even on the same deployment; standalone
        compressors (no bound session) always use their own snapshot.
        """
        from agent.auxiliary_client import get_scoped_runtime_main

        runtime = get_scoped_runtime_main()
        session_id = getattr(self, "_session_id", "")
        if session_id and runtime.get("session_id") == session_id:
            return runtime
        return {
            "model": self.model,
            "provider": self.provider,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "api_mode": self.api_mode,
            "reasoning_config": copy.deepcopy(getattr(self, "reasoning_config", None)),
        }

