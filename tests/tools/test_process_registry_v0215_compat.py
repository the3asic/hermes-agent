"""Compatibility smoke for stable-v0.21.5 restart-safe worker dispatch."""
import tools.process_registry as pr


def test_scoped_spawn_lost_user_bus_is_importable_and_fail_closed_on_live_bus(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    assert pr.scoped_spawn_lost_user_bus({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}) is False
