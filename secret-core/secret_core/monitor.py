"""Independent execution monitor.

Started as its own process per execution, without inherited key values. It kills
the managed process tree when the lease goes stale, a key/device is revoked, the
owner requests a stop, or the single-execution deadline passes. It exits when the
child exits. A failure of the managing process does not disable this monitor.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, timeout=20)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secret-core-monitor")
    parser.add_argument("--exec-dir", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--lease-file", required=True)
    parser.add_argument("--lease-seconds", type=float, required=True)
    parser.add_argument("--revoked-file", required=True)
    parser.add_argument("--keys", default="")
    parser.add_argument("--device", default="")
    parser.add_argument("--timeout", type=float, default=0.0)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--started-at", type=float, default=0.0)
    args = parser.parse_args(argv)
    exec_dir = Path(args.exec_dir)
    lease_file = Path(args.lease_file)
    revoked_file = Path(args.revoked_file)
    stop_file = Path(args.stop_file)
    started = time.time()
    keys = [item for item in args.keys.split(",") if item]
    reason: str | None = None
    while _pid_alive(args.pid):
        if stop_file.exists():
            stop_data = _read_json(stop_file)
            reason = str(stop_data.get("reason") or "owner_stop")
        else:
            revoked = _read_json(revoked_file)
            if any(key in revoked.get("keys", {}) for key in keys):
                reason = "key_revoked"
            elif any(
                float(revoked.get("rotated", {}).get(key, {}).get("epoch", 0)) > float(args.started_at)
                for key in keys
            ):
                reason = "key_rotated"
            elif args.device and args.device in revoked.get("devices", {}):
                reason = "device_stopped"
            else:
                lease = _read_json(lease_file)
                last = lease.get("last_refresh")
                if not isinstance(last, (int, float)) or time.time() - float(last) > args.lease_seconds:
                    reason = "lease_expired"
                elif args.timeout > 0 and time.time() - started > args.timeout:
                    reason = "timeout"
        if reason:
            break
        time.sleep(0.3)
    if reason:
        _kill_tree(args.pid)
        deadline = time.time() + 20
        while _pid_alive(args.pid) and time.time() < deadline:
            time.sleep(0.2)
        data = _read_json(exec_dir / "monitor.json")
        data.update({"killed": True, "reason": reason, "at": time.time()})
        try:
            _write_json(exec_dir / "monitor.json", data)
        except OSError:
            pass
        return 0
    data = _read_json(exec_dir / "monitor.json")
    data.update({"killed": False, "child_exited": True, "at": time.time()})
    try:
        _write_json(exec_dir / "monitor.json", data)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
