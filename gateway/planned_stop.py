"""Systemd ExecStop preflight for clean gateway shutdown classification."""

from __future__ import annotations

import argparse

from gateway.status import write_planned_stop_marker


def mark_planned_stop(target_pid: int) -> bool:
    """Write a PID/start-time-qualified marker for a live gateway process."""
    if target_pid <= 1:
        return False
    return write_planned_stop_marker(target_pid)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mark an intentional gateway stop")
    parser.add_argument("pid", type=int, help="systemd gateway MainPID")
    args = parser.parse_args(argv)
    if mark_planned_stop(args.pid):
        return 0
    parser.error("could not write the planned-stop marker for the gateway PID")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
