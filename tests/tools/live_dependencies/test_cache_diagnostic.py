"""Verify private diagnostic I/O with real files or explicit platform rejection."""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from agent.cache_diagnostic import _check_root, _new, _read


def test_diagnostic_file_ownership_is_required(tmp_path):
    if os.name != "posix":
        with pytest.raises(OSError, match="POSIX file ownership"):
            _check_root(tmp_path)
        assert list(tmp_path.iterdir()) == []
        return
    root = _check_root(tmp_path)
    document = (Path(__file__).resolve().parents[3] / "README.md").read_bytes()
    _new(root, "document", document)
    assert _read(root, "document", limit=len(document)) == document
    path = root / "document"
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="private_regular_file_required"):
        _read(root, "document", limit=len(document))
    path.chmod(0o600)
    assert _read(root, "document", limit=len(document)) == document


def test_website_policy_invalidation_applies_real_config_change(tmp_path):
    from hermes_cli.config import atomic_config_write
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.website_policy import check_website_access, invalidate_cache

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    url = "https://github.com/the3asic/hermes-agent"
    hostname = urlparse(url).hostname
    try:
        atomic_config_write(home / "config.yaml", {
            "security": {"website_blocklist": {"enabled": True, "domains": [hostname], "shared_files": []}},
        })
        invalidate_cache()
        assert check_website_access(url)["host"] == hostname
        atomic_config_write(home / "config.yaml", {
            "security": {"website_blocklist": {"enabled": False, "domains": [], "shared_files": []}},
        })
        assert check_website_access(url)["host"] == hostname
        invalidate_cache()
        assert check_website_access(url) is None
    finally:
        invalidate_cache()
        reset_hermes_home_override(token)
