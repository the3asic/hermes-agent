"""Native config/YAML/runtime checks using the release's shipped configuration data."""
from contextlib import contextmanager
import copy

import hermes_yaml
import pytest
from agent import auxiliary_client as aux, auxiliary_runtime_policy
from hermes_cli.config import load_config_readonly
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_constants import (
    parse_reasoning_effort,
    reset_hermes_home_override,
    resolve_reasoning_config,
    set_hermes_home_override,
)


@contextmanager
def _native_config(home):
    (home / "config.yaml").write_text(hermes_yaml.safe_dump(DEFAULT_CONFIG), encoding="utf-8")
    token = set_hermes_home_override(str(home))
    try:
        yield load_config_readonly()
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("task", [None, "voice_chat"])
def test_native_yaml_runtime_reasoning_is_isolated(tmp_path, task):
    with _native_config(tmp_path) as config:
        reasoning = (resolve_reasoning_config(config) if task is None
                     else parse_reasoning_effort(config["auxiliary"][task]["reasoning_effort"]))
        token = aux.set_runtime_main(
            provider=aux._read_main_provider(), model=aux._read_main_model(), reasoning_config=reasoning)
        try:
            normalized = aux._normalize_main_runtime(None)
            assert normalized["reasoning_config"] == reasoning
            if isinstance(reasoning, dict):
                assert normalized["reasoning_config"] is not reasoning
                normalized["reasoning_config"].clear()
                assert aux.get_scoped_runtime_main()["reasoning_config"] == reasoning
            with aux.scoped_runtime_main({**aux.get_scoped_runtime_main(), "reasoning_config": None}):
                assert "reasoning_config" in aux.get_scoped_runtime_main()
                assert aux.get_scoped_runtime_main()["reasoning_config"] is None
            assert aux.get_scoped_runtime_main()["reasoning_config"] == reasoning
        finally:
            aux.reset_runtime_main(token)


def test_native_yaml_policy_fingerprint_tracks_persisted_policy(tmp_path):
    with _native_config(tmp_path) as config:
        assert config["auxiliary"]["compression"] == DEFAULT_CONFIG["auxiliary"]["compression"]
        first = auxiliary_runtime_policy.fallback_policy_cache_fingerprint("compression")
        second = auxiliary_runtime_policy.fallback_policy_cache_fingerprint("compression")
        assert first == second
        assert isinstance(first, bytes)
        config["auxiliary"]["compression"] = copy.deepcopy(DEFAULT_CONFIG["auxiliary"]["voice_chat"])
        (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config), encoding="utf-8")
        changed = auxiliary_runtime_policy.fallback_policy_cache_fingerprint("compression")
        assert changed != first
        assert changed == auxiliary_runtime_policy.fallback_policy_cache_fingerprint("compression")
