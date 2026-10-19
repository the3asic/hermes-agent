"""Verify private diagnostic I/O with real files or explicit platform rejection."""

import os
from pathlib import Path

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
