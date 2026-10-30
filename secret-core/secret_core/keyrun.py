"""Foreground key launcher for systemd-managed services.

A unit can use this command as ``ExecStart``; the vault supplies only the selected
keys to the launched command and systemd owns start/stop/restart policy. The
process stays in the foreground, forwards stop requests to the manager, and
writes the same start/end audit records as ``key_exec``. It never registers or
enables a unit by itself.
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time

from contracts.config import ConfigError, Layout, load_config
from contracts.models import ExecRequest
from secret_core.runner import ExecManager, is_elevated


def _command_line(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ultra-rag-keyrun", description="run a systemd service with vault-supplied keys")
    parser.add_argument("--home", default=None)
    parser.add_argument("--key", action="append", required=True, help="registered key id (repeatable)")
    parser.add_argument("--host", default=None)
    parser.add_argument("--timeout", type=float, default=0.0, help="optional foreground safety timeout (0 = none)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command_parts = [part for part in args.command if part != "--"]
    if not command_parts:
        parser.error("a command after -- is required")
    layout = Layout.at(args.home)
    try:
        config = load_config(layout, create=False)
    except ConfigError as error:
        print(f"ultra-rag-keyrun: {error}", file=sys.stderr)
        return 2
    if is_elevated():
        print("ultra-rag-keyrun: key-supplied processes must not run with administrator privileges", file=sys.stderr)
        return 2
    manager = ExecManager(layout, config)
    request = ExecRequest(
        command=_command_line(command_parts),
        key_ids=list(args.key),
        device_id=config["device_id"],
        host=args.host or config.get("host", "server"),
        mode="resident",
        timeout_seconds=1.0,
    )
    state = manager.execute(request)
    if state.status != "running":
        print(f"ultra-rag-keyrun: {state.status}: {state.detail or state.stop_reason or ''}", file=sys.stderr)
        return 1
    stopping = {"requested": False}

    def stop(signum, _frame) -> None:
        if stopping["requested"]:
            return
        stopping["requested"] = True
        manager.stop(state.exec_id, reason=f"signal_{signum}")

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            try:
                signal.signal(getattr(signal, signal_name), stop)
            except (ValueError, OSError):
                pass
    deadline = time.time() + args.timeout if args.timeout > 0 else None
    final = state
    while deadline is None or time.time() < deadline:
        final = manager.status(state.exec_id)
        if final.status != "running":
            break
        time.sleep(0.4)
    if final.status == "running":
        manager.stop(state.exec_id, reason="foreground_timeout")
        time.sleep(1.0)
        final = manager.status(state.exec_id)
    if final.status == "running":
        print("ultra-rag-keyrun: the managed process is still running", file=sys.stderr)
        return 1
    if final.stdout:
        sys.stdout.write(final.stdout)
    if final.stderr:
        sys.stderr.write(final.stderr)
    if final.status == "finished" and final.exit_code == 0:
        return 0
    return int(final.exit_code) if isinstance(final.exit_code, int) and final.exit_code != 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
