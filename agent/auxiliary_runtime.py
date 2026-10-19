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
