"""Tests for the systemd planned-stop preflight command."""

from gateway import planned_stop


def test_mark_planned_stop_rejects_invalid_pid(monkeypatch):
    called = []
    monkeypatch.setattr(
        planned_stop, "write_planned_stop_marker", lambda pid: called.append(pid) or True
    )

    assert planned_stop.mark_planned_stop(0) is False
    assert planned_stop.mark_planned_stop(1) is False
    assert called == []


def test_main_writes_pid_qualified_marker(monkeypatch):
    called = []
    monkeypatch.setattr(
        planned_stop, "write_planned_stop_marker", lambda pid: called.append(pid) or True
    )

    assert planned_stop.main(["4242"]) == 0
    assert called == [4242]
