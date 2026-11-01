"""Isolated key-supplied execution manager.

A single request runs in a dedicated workspace with only the selected keys in
its environment. The manager validates the device and lease, writes the start
audit record before any supply, spawns the child and an independent monitor, and
returns masked output. A restarted runtime does not adopt or re-run an old id.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from contracts.config import Layout, load_config
from contracts.models import AuditEntry, ExecRequest, ExecState
from contracts.util import (
    check_identifier,
    ensure_dir,
    mask_known_values,
    new_id,
    read_json,
    sha256_hex,
    utc_now,
    write_json,
    write_lock,
    write_once_json,
)
from secret_core.audit import AuditError, AuditLog
from secret_core.devices import DeviceError, DeviceRegistry
from secret_core.lease import LeaseError, LeaseManager
from secret_core.revocation import Revocations
from secret_core.vault import Vault, VaultError

SAFE_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "SYSTEMROOT",
    "windir",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "TERM",
    "TZ",
    "PYTHONPATH",
)


class ExecError(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not process:
            return False
        try:
            code = ctypes.c_ulong(0)
            if ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def is_elevated() -> bool:
    if os.name == "nt":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return True  # fail closed
    try:
        return os.geteuid() == 0
    except AttributeError:
        return True


def kill_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        return
    try:
        os.killpg(os.getpgid(pid), 9)
    except ProcessLookupError:
        return
    except Exception:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _is_link(path: Path) -> bool:
    """Symlinks and Windows junctions are not followed."""
    try:
        if path.is_symlink():
            return True
        checker = getattr(path, "is_junction", None)
        return bool(checker and checker())
    except OSError:
        return True


def _safe_member(root: Path, relative: str) -> Path | None:
    """Resolve a deliverable path without escaping ``root`` or crossing a link.

    Absolute paths, drive-relative paths, and ``..`` are refused. Every path
    component is checked for a symlink or junction before the final path is
    resolved, because a link in the middle would otherwise copy a file from
    outside the execution workspace.
    """
    raw = str(relative).replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return None
    candidate = root
    for part in Path(raw).parts:
        if part in ("", ".", ".."):
            return None
        candidate = candidate / part
        if _is_link(candidate):
            return None
    try:
        resolved_root = root.resolve()
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved != resolved_root and resolved_root not in resolved.parents:
        return None
    return candidate


def _copy_file_atomic(source: Path, destination: Path) -> None:
    ensure_dir(destination.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.tmp-", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as output, open(source, "rb") as input_handle:
            shutil.copyfileobj(input_handle, output)
            output.flush()
            os.fsync(output.fileno())
        try:
            shutil.copystat(source, temporary)
        except OSError:
            pass
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _merge_type_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for key, value in list(left.items()) + list(right.items()):
        merged[key] = merged.get(key, 0) + int(value)
    return merged


def _copy_tree_no_links(source: Path, destination: Path, report: dict[str, Any]) -> None:
    ensure_dir(destination)
    source_root = source
    for root, directories, files in os.walk(source_root, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source_root)
        for name in list(directories):
            candidate = root_path / name
            if _is_link(candidate):
                directories.remove(name)
                report["skipped"].append(
                    {"path": (relative_root / name).as_posix(), "reason": "symbolic link or junction is not followed"}
                )
                continue
            ensure_dir(destination / relative_root / name)
        for name in files:
            candidate = root_path / name
            relative = (relative_root / name).as_posix()
            if _is_link(candidate):
                report["skipped"].append({"path": relative, "reason": "symbolic link or junction is not followed"})
                continue
            try:
                _copy_file_atomic(candidate, destination / relative_root / name)
                report["copied"].append(relative)
            except OSError as error:
                report["errors"].append({"path": relative, "reason": type(error).__name__})


def _boot_marker() -> str:
    """Identify the host boot, not one CLI process.

    A resident execution starts from a short-lived CLI process. Tying the
    runtime generation to that PID made every later `exec-status` call look like
    a previous generation. The boot marker is stable across processes and only
    changes after a reboot.
    """
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        if value:
            return "linux:" + value
    except OSError:
        pass
    if os.name == "nt":
        # The registry value is not readable for every account, and falling back
        # to the PID would make each process (including the resident supervisor)
        # look like a new generation. GetTickCount64 is stable for one boot.
        try:
            import ctypes

            ticks = ctypes.windll.kernel32.GetTickCount64()
            return "windows-boot:" + str(int(time.time() - float(ticks) / 1000.0))
        except Exception:
            pass
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\SystemInformation") as key:
                value, _ = winreg.QueryValueEx(key, "SystemBootTime")
            return "windows:" + str(value)
        except Exception:
            pass
    return "pid:" + str(os.getpid())


class RuntimeEpoch:
    def __init__(self, layout: Layout):
        self.path = layout.run / "epoch.json"
        self.lock_file = layout.run / "epoch.lock"

    def acquire(self) -> int:
        marker = _boot_marker()
        with write_lock(self.lock_file, 20):
            data = read_json(self.path, None)
            if isinstance(data, dict) and data.get("boot_marker") == marker:
                return int(data["epoch"])
            epoch = int(data.get("epoch", 0)) + 1 if isinstance(data, dict) else 1
            write_json(
                self.path,
                {"epoch": epoch, "boot_marker": marker, "pid": os.getpid(), "started_at": utc_now()},
            )
            return epoch


class ExecManager:
    def __init__(self, layout: Layout, config: dict[str, Any] | None = None):
        self.layout = layout
        self.config = config or load_config(layout, create=False)
        self.vault = Vault(layout)
        self.devices = DeviceRegistry(layout)
        self.audit = AuditLog(layout)
        self.revocations = Revocations(layout)
        secret_config = self.config.get("secret", {})
        self.lease = LeaseManager(layout, float(secret_config.get("lease_seconds", 300)))
        self.max_exec_seconds = float(secret_config.get("max_exec_seconds", 3600))
        self.max_concurrent = int(secret_config.get("max_concurrent", 4))
        self.capacity_lock = layout.run / "capacity.lock"
        self.host = str(self.config.get("host", "pc"))
        self.epoch = RuntimeEpoch(layout).acquire()
        self.run_root = layout.run
        self._claim_guard = threading.Lock()
        self._claim_locks: dict[str, threading.Lock] = {}
        ensure_dir(self.run_root)

    # ----------------------------------------------------------------- helpers
    def _exec_dir(self, exec_id: str) -> Path:
        check_identifier(exec_id, "exec_id")
        return self.run_root / exec_id

    def _state(self, exec_id: str) -> ExecState | None:
        path = self._exec_dir(exec_id) / "state.json"
        data = read_json(path, None)
        return ExecState.from_dict(data) if isinstance(data, dict) else None

    def _write_state(self, exec_id: str, state: ExecState) -> None:
        write_json(self._exec_dir(exec_id) / "state.json", state.to_dict())

    def _reject(self, request: ExecRequest, detail: str, persist_command: bool = True) -> ExecState:
        state = ExecState(
            exec_id=request.exec_id,
            status="rejected",
            detail=detail,
            generation=self.epoch,
            ended_at=utc_now(),
        )
        directory = self._exec_dir(request.exec_id)
        ensure_dir(directory)
        if not (directory / "request.json").exists():
            payload = request.to_dict()
            if not persist_command:
                # A request rejected for containing a secret must not be kept
                # verbatim on disk; keep only opaque digests.
                payload["command"] = f"[REDACTED:{sha256_hex(request.command)[:12]}]"
                payload["deliverables"] = [f"[REDACTED:{sha256_hex(item)[:12]}]" for item in request.deliverables]
                payload["input_paths"] = [f"[REDACTED:{sha256_hex(item)[:12]}]" for item in request.input_paths]
            write_json(directory / "request.json", payload)
        self._write_state(request.exec_id, state)
        return state

    def _host_ok(self, host: str) -> bool:
        return host == "local" or host == self.host

    def _active_executions(self) -> int:
        """Best-effort cross-process count of live, managed executions."""
        count = 0
        for directory in self.run_root.iterdir() if self.run_root.is_dir() else []:
            if not directory.is_dir() or not directory.name.startswith("exec_"):
                continue
            state_data = read_json(directory / "state.json", None)
            if not isinstance(state_data, dict) or state_data.get("status") != "running":
                continue
            pid_data = read_json(directory / "pid.json", {}) or {}
            pid = int(pid_data.get("pid", 0) or 0)
            if pid and not _pid_alive(pid):
                continue
            if not pid:
                try:
                    from contracts.util import parse_iso

                    started = parse_iso(str(state_data.get("started_at"))).timestamp()
                    if time.time() - started > 60:
                        # A crash between reservation and pid publication must
                        # not consume a concurrency slot forever.
                        continue
                except Exception:
                    continue
            count += 1
        return count

    # ---------------------------------------------------------------- execute
    def execute(self, request: ExecRequest | dict[str, Any]) -> ExecState:
        if isinstance(request, dict):
            request = ExecRequest(**{key: request[key] for key in ExecRequest.__dataclass_fields__ if key in request})
        request.validate()
        # One claim per execution id: the reservation, state write, and spawn
        # must be one critical section, or two callers can start the same id.
        claim = self.run_root / "claims" / f"{request.exec_id}.lock"
        ensure_dir(claim.parent)
        # filelock alone is per process, so two threads in one process can both
        # enter. Pair it with an in-process mutex for the same execution id.
        with self._claim_guard:
            claim_mutex = self._claim_locks.setdefault(request.exec_id, threading.Lock())
        with claim_mutex:
            with write_lock(claim, 60):
                return self._execute_locked(request)

    def _execute_locked(self, request: ExecRequest) -> ExecState:
        invalidated = read_json(self.layout.secret / "invalidated.json", {}) or {}
        if request.exec_id in (invalidated.get("exec_ids") or []):
            return ExecState(
                exec_id=request.exec_id,
                status="invalid",
                detail="invalidated by vault recreation",
                generation=self.epoch,
            )
        directory = self._exec_dir(request.exec_id)
        if directory.exists():
            existing = self._state(request.exec_id)
            previous = read_json(directory / "request.json", None)
            if existing is None:
                return self._reject(request, "an incomplete execution directory already exists")
            if existing.generation != self.epoch:
                return ExecState(
                    exec_id=request.exec_id,
                    status="unknown",
                    detail="the id belongs to a previous runtime generation; it is not re-executed",
                    generation=existing.generation,
                )
            if isinstance(previous, dict) and ExecRequest(**{k: previous[k] for k in ExecRequest.__dataclass_fields__ if k in previous}).fingerprint() != request.fingerprint():
                return ExecState(
                    exec_id=request.exec_id,
                    status="rejected",
                    detail="the same exec id was already used with different content",
                    generation=self.epoch,
                )
            return existing
        if not self._host_ok(request.host):
            return self._reject(request, f"host_mismatch: this runtime is {self.host}, request targets {request.host}")
        if request.device_id != self.config.get("device_id"):
            return self._reject(request, "device_mismatch: the request device is not this runtime's registered device")
        try:
            if not self.devices.authenticate_local(request.device_id):
                return self._reject(request, "device_authentication_failed")
        except DeviceError as error:
            return self._reject(request, f"device_authentication_failed: {error}")
        blocked = self.revocations.blocked(request.key_ids, request.device_id)
        if blocked:
            return self._reject(request, f"revoked: {blocked}")
        try:
            self.lease.require_fresh()
        except LeaseError as error:
            return self._reject(request, f"lease: {error}")
        if request.timeout_seconds > self.max_exec_seconds:
            return self._reject(request, f"timeout_exceeds_limit: {int(self.max_exec_seconds)}s")
        if is_elevated():
            return self._reject(request, "key_exec must not run with administrator privileges")
        audit_entry = AuditEntry(
            audit_id=new_id("aud"),
            exec_id=request.exec_id,
            key_ids=list(request.key_ids),
            device_id=request.device_id,
            started_at=utc_now(),
            ended_at=None,
            state="starting",
        )
        try:
            self.audit.start(audit_entry)
        except AuditError as error:
            return self._reject(request, f"audit_start_failed: {error}")
        started_epoch = time.time()
        rotation_before = read_json(self.revocations.path, {}) or {}
        try:
            values = self.vault.resolve(request.key_ids)
        except (VaultError, Exception) as error:
            self.audit.finish(request.exec_id, "rejected", "key_supply_failed")
            return self._reject(request, f"key_supply_failed: {type(error).__name__}")
        rotation_after = read_json(self.revocations.path, {}) or {}
        before = rotation_before.get("rotated", {}) if isinstance(rotation_before, dict) else {}
        after = rotation_after.get("rotated", {}) if isinstance(rotation_after, dict) else {}
        if any(
            float(after.get(key_id, {}).get("epoch", 0)) > float(before.get(key_id, {}).get("epoch", 0))
            for key_id in request.key_ids
        ):
            self.audit.finish(request.exec_id, "rejected", "key_rotated_before_start")
            return self._reject(
                request,
                "key_rotated_before_start: the key was rotated while the execution was starting; retry with the new generation",
            )
        if any(len(value) >= 8 and value in request.command for value in values.values()):
            self.audit.finish(request.exec_id, "rejected", "secret_in_command")
            return self._reject(
                request,
                "secret_in_command: the command text contains a selected secret value; "
                "reference the environment variable instead",
                persist_command=False,
            )
        paths = list(request.deliverables) + list(request.input_paths)
        if any(len(value) >= 8 and value in item for value in values.values() for item in paths):
            self.audit.finish(request.exec_id, "rejected", "secret_in_path")
            return self._reject(
                request,
                "secret_in_path: a selected secret value appears in a path; use a plain file name",
                persist_command=False,
            )
        directory = self._exec_dir(request.exec_id)
        workspace = directory / "workspace"
        ensure_dir(workspace)
        try:
            input_report = self._copy_inputs(request, workspace)
        except ExecError as error:
            self.audit.finish(request.exec_id, "rejected", "input_rejected")
            return self._reject(request, str(error))
        write_json(directory / "request.json", request.to_dict())
        env = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
        env.update(values)
        env["ULTRA_RAG_EXEC_ID"] = request.exec_id
        with write_lock(self.capacity_lock, 20):
            if self.max_concurrent > 0 and self._active_executions() >= self.max_concurrent:
                self.audit.finish(request.exec_id, "rejected", "concurrency_limit")
                return self._reject(
                    request,
                    f"concurrency_limit: {self.max_concurrent} managed executions are already running",
                )
            state = ExecState(
                exec_id=request.exec_id,
                status="running",
                started_at=utc_now(),
                generation=self.epoch,
                workspace=str(workspace),
                inputs=input_report,
            )
            self._write_state(request.exec_id, state)
        if request.mode == "resident":
            # The CLI must not own the pipes or the finalizer: it exits while
            # the command keeps running. A detached supervisor spawns the child,
            # owns its streams, and writes the final state and audit record.
            self._launch_supervisor(request, state, env, started_epoch)
            return state
        try:
            process = self._spawn(request, env, workspace)
        except Exception as error:
            state.status = "failed"
            state.ended_at = utc_now()
            state.detail = f"spawn_failed: {type(error).__name__}"
            self._write_state(request.exec_id, state)
            self.audit.finish(request.exec_id, "failed", "spawn_failed")
            return state
        try:
            write_json(directory / "pid.json", {"pid": process.pid, "at": utc_now()})
            monitor = self._start_monitor(request, process.pid, started_epoch)
        except Exception as error:
            # The child already holds the supplied values. A monitor that never
            # started would leave it unmanaged, so stop the tree and record it.
            kill_process_tree(process.pid)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            state.status = "failed"
            state.ended_at = utc_now()
            state.detail = f"monitor_start_failed: {type(error).__name__}"
            self._write_state(request.exec_id, state)
            self.audit.finish(request.exec_id, "failed", "monitor_start_failed")
            return state
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        threads = [
            threading.Thread(target=self._read_stream, args=(process.stdout, stdout_chunks), daemon=True),
            threading.Thread(target=self._read_stream, args=(process.stderr, stderr_chunks), daemon=True),
        ]
        for thread in threads:
            thread.start()
        return self._finalize(
            request,
            state,
            process,
            monitor,
            stdout_chunks,
            stderr_chunks,
            threads,
            request.timeout_seconds,
            list(values.values()),
            started_epoch,
        )

    def _launch_supervisor(
        self, request: ExecRequest, state: ExecState, env: dict[str, str], started_epoch: float
    ) -> None:
        """Hand a resident execution to a detached process and wait for its pid."""
        directory = self._exec_dir(request.exec_id)
        repo_root = Path(__file__).resolve().parents[2]
        supervisor_env = dict(env)
        supervisor_env["PYTHONPATH"] = os.pathsep.join(
            [str(repo_root), str(repo_root / "secret-core")]
        )
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        supervisor = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "secret_core.supervisor",
                "--home",
                str(self.layout.home),
                "--exec-id",
                request.exec_id,
                "--started-at",
                str(started_epoch),
            ],
            cwd=str(repo_root),
            env=supervisor_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if (directory / "pid.json").exists():
                return
            if supervisor.poll() is not None:
                break
            time.sleep(0.05)
        kill_process_tree(supervisor.pid)
        state.status = "failed"
        state.ended_at = utc_now()
        state.detail = "supervisor_start_failed"
        self._write_state(request.exec_id, state)
        self.audit.finish(request.exec_id, "failed", "supervisor_start_failed")

    def supervise_resident(self, exec_id: str, started_epoch: float) -> ExecState:
        """Run in the detached supervisor: own the child, its streams, and state."""
        directory = self._exec_dir(exec_id)
        data = read_json(directory / "request.json", None)
        if not isinstance(data, dict):
            raise ExecError("the execution request is not available")
        request = ExecRequest(**{key: data[key] for key in ExecRequest.__dataclass_fields__ if key in data})
        state = self._state(exec_id) or ExecState(exec_id=exec_id, status="running", generation=self.epoch)
        workspace = Path(state.workspace or (directory / "workspace"))
        env = dict(os.environ)
        values = self._supplied_values(request, env)
        try:
            process = self._spawn(request, env, workspace)
            write_json(directory / "pid.json", {"pid": process.pid, "at": utc_now()})
            monitor = self._start_monitor(request, process.pid, started_epoch)
        except Exception as error:
            state.status = "failed"
            state.ended_at = utc_now()
            state.detail = f"supervisor_spawn_failed: {type(error).__name__}"
            self._write_state(exec_id, state)
            self.audit.finish(exec_id, "failed", "supervisor_spawn_failed")
            return state
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        threads = [
            threading.Thread(target=self._read_stream, args=(process.stdout, stdout_chunks), daemon=True),
            threading.Thread(target=self._read_stream, args=(process.stderr, stderr_chunks), daemon=True),
        ]
        for thread in threads:
            thread.start()
        return self._finalize(
            request, state, process, monitor, stdout_chunks, stderr_chunks, threads, None, values, started_epoch
        )

    def _supplied_values(self, request: ExecRequest, env: dict[str, str]) -> list[str]:
        names = {meta.id: meta.env_name for meta in self.vault.list_keys()}
        return [env[name] for name in (names.get(key_id) for key_id in request.key_ids) if name and name in env]

    @staticmethod
    def _group_alive(pid: int) -> bool:
        if os.name == "nt":
            return False
        try:
            os.killpg(pid, 0)
            return True
        except OSError:
            return False

    def _spawn(self, request: ExecRequest, env: dict[str, str], workspace: Path) -> subprocess.Popen:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            return subprocess.Popen(
                request.command,
                shell=True,
                cwd=str(workspace),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
        return subprocess.Popen(
            ["/bin/sh", "-c", request.command],
            cwd=str(workspace),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _copy_inputs(self, request: ExecRequest, workspace: Path) -> dict[str, Any]:
        secret_root = self.layout.secret.resolve()
        run_root = self.run_root.resolve()
        report: dict[str, Any] = {"copied": [], "skipped": [], "errors": []}
        for raw in request.input_paths:
            source = Path(raw).expanduser().resolve()
            if not source.exists():
                raise ExecError(f"input_not_found: {source.name}")
            if secret_root in source.parents or source == secret_root:
                raise ExecError("input from the secret area is refused")
            if run_root in source.parents or source == run_root:
                raise ExecError("input from another execution workspace is refused")
            destination = workspace / source.name
            if _is_link(source):
                report["skipped"].append({"path": source.name, "reason": "symbolic link or junction is not followed"})
                continue
            if source.is_dir():
                _copy_tree_no_links(source, destination, report)
            else:
                try:
                    _copy_file_atomic(source, destination)
                    report["copied"].append(source.name)
                except OSError as error:
                    report["errors"].append({"path": source.name, "reason": type(error).__name__})
        return report

    def _start_monitor(self, request: ExecRequest, pid: int, started_epoch: float) -> subprocess.Popen:
        stop_file = self._exec_dir(request.exec_id) / "stop"
        timeout = request.timeout_seconds if request.mode == "single" else 0
        args = [
            sys.executable,
            "-m",
            "secret_core.monitor",
            "--exec-dir",
            str(self._exec_dir(request.exec_id)),
            "--pid",
            str(pid),
            "--lease-file",
            str(self.lease.path),
            "--lease-seconds",
            str(self.lease.seconds),
            "--revoked-file",
            str(self.revocations.path),
            "--keys",
            ",".join(request.key_ids),
            "--device",
            request.device_id,
            "--timeout",
            str(timeout),
            "--stop-file",
            str(stop_file),
            "--started-at",
            str(started_epoch),
        ]
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT", "PYTHONPATH", "TEMP", "TMP") if key in os.environ}
        package_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(
            [package_root, env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)
        process = subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            start_new_session=(os.name != "nt"),
        )
        write_json(self._exec_dir(request.exec_id) / "monitor.json", {"pid": process.pid, "at": utc_now()})
        return process

    @staticmethod
    def _read_stream(stream: Any, sink: list[bytes]) -> None:
        try:
            sink.append(stream.read())
        except Exception:
            sink.append(b"")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _finalize(
        self,
        request: ExecRequest,
        state: ExecState,
        process: subprocess.Popen,
        monitor: subprocess.Popen,
        stdout_chunks: list[bytes],
        stderr_chunks: list[bytes],
        threads: list[threading.Thread],
        timeout: float | None,
        supplied_values: list[str],
        started_epoch: float,
    ) -> ExecState:
        timed_out = False
        monitor_stopped = False
        deadline = None if timeout is None else time.time() + timeout
        while True:
            try:
                exit_code = process.wait(timeout=0.3)
                break
            except subprocess.TimeoutExpired:
                if process.poll() is not None:
                    exit_code = process.returncode
                    break
                if monitor.poll() is not None:
                    # The independent monitor died before the child: stop the
                    # child from this side instead of leaving it unmanaged.
                    monitor_stopped = True
                    kill_process_tree(process.pid)
                    try:
                        exit_code = process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        exit_code = None
                    break
                if deadline is not None and time.time() > deadline:
                    timed_out = True
                    kill_process_tree(process.pid)
                    try:
                        exit_code = process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        exit_code = None
                    break
        descendants_left = False
        if os.name != "nt" and self._group_alive(process.pid):
            # The shell can exit while a background child still holds the
            # supplied environment; finished must wait for the whole group.
            wait_until = time.time() + (timeout if timeout else 5.0)
            while time.time() < wait_until and self._group_alive(process.pid):
                time.sleep(0.2)
            if self._group_alive(process.pid):
                descendants_left = True
                kill_process_tree(process.pid)
        for thread in threads:
            thread.join(timeout=10)
        # The monitor writes its reason immediately after killing; wait briefly
        # so a lease/revocation kill is not misreported as an ordinary failure.
        try:
            monitor.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        monitor_data = read_json(self._exec_dir(request.exec_id) / "monitor.json", {}) or {}
        monitor_reason = monitor_data.get("reason") if monitor_data.get("killed") else None
        if monitor_reason is None:
            # Monitor output can race the child exit. Classify from the same
            # durable control inputs the monitor reads instead of reporting a
            # stopped execution as a command failure.
            if (self._exec_dir(request.exec_id) / "stop").exists():
                monitor_reason = str(
                    (read_json(self._exec_dir(request.exec_id) / "stop", {}) or {}).get("reason")
                    or "owner_stop"
                )
            else:
                blocked = self.revocations.blocked(request.key_ids, request.device_id)
                if blocked:
                    monitor_reason = "key_revoked" if blocked.startswith("key_revoked") else "device_stopped"
                else:
                    rotation = read_json(self.revocations.path, {}).get("rotated", {})
                    if any(float(rotation.get(key, {}).get("epoch", 0)) > started_epoch for key in request.key_ids):
                        monitor_reason = "key_rotated"
                    elif self.lease.age() is not None and self.lease.age() > self.lease.seconds:
                        monitor_reason = "lease_expired"
        stdout = b"".join(stdout_chunks).decode("utf-8", "replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
        masking_failed = False
        try:
            masked_out, report_out = mask_known_values(stdout, supplied_values)
            masked_err, report_err = mask_known_values(stderr, supplied_values)
            state.masking_detections = {
                "total": int(report_out["total"]) + int(report_err["total"]),
                "types": _merge_type_counts(report_out["types"], report_err["types"]),
            }
            state.masked_hits = sorted(set(report_out["markers"] + report_err["markers"]))
        except Exception as error:
            masking_failed = True
            masked_out = ""
            masked_err = ""
            state.masking_status = "failed"
            state.masking_error = type(error).__name__
            state.masking_detections = {
                "total": 0,
                "types": {},
                "values_considered": len(supplied_values),
            }
            state.masked_hits = []
        state.masking_note = (
            "既知値の完全一致・base64・JSONエスケープのみを置換します。"
            "分割・暗号化・変換・外部送信された値は検出しません。"
        )
        state.stdout = masked_out
        state.stderr = masked_err
        state.exit_code = exit_code
        state.ended_at = utc_now()
        state.duration_seconds = max(0.0, time.time() - started_epoch)
        if masking_failed:
            state.status = "failed"
            state.stop_reason = "masking_failed"
        elif timed_out:
            state.status = "stopped"
            state.stop_reason = "timeout"
        elif monitor_stopped:
            state.status = "stopped"
            state.stop_reason = "monitor_stopped"
        elif descendants_left:
            state.status = "stopped"
            state.stop_reason = "descendants_timeout"
        elif monitor_reason:
            state.status = "stopped"
            state.stop_reason = str(monitor_reason)
        elif exit_code == 0:
            state.status = "finished"
        else:
            state.status = "failed"
        output_dir = self._exec_dir(request.exec_id)
        write_json(output_dir / "state.json", state.to_dict())
        try:
            (output_dir / "stdout.log").write_text(masked_out, encoding="utf-8")
            (output_dir / "stderr.log").write_text(masked_err, encoding="utf-8")
        except OSError:
            pass
        if state.status == "finished":
            state.artifacts = self._mask_metadata(
                self._import_artifacts(request, Path(state.workspace or output_dir / "workspace")),
                list(supplied_values),
            )
            write_json(output_dir / "state.json", state.to_dict())
        elif request.deliverables:
            state.artifacts = {
                "imported": [],
                "failed": [],
                "skipped": [],
                "workspace": str(state.workspace or output_dir / "workspace"),
                "reason": state.stop_reason or state.status,
                "kept_workspace": True,
            }
            write_json(output_dir / "state.json", state.to_dict())
        try:
            self.audit.finish(request.exec_id, state.status, state.stop_reason)
        except AuditError:
            state.detail = (state.detail or "") + "; audit_end_write_failed"
            write_json(output_dir / "state.json", state.to_dict())
        try:
            monitor.terminate()
        except Exception:
            pass
        return state

    @classmethod
    def _mask_metadata(cls, value: Any, supplied: list[str]) -> Any:
        if isinstance(value, str):
            return mask_known_values(value, supplied)[0]
        if isinstance(value, list):
            return [cls._mask_metadata(item, supplied) for item in value]
        if isinstance(value, dict):
            return {key: cls._mask_metadata(item, supplied) for key, item in value.items()}
        return value

    def _import_artifacts(self, request: ExecRequest, workspace: Path) -> dict[str, Any]:
        if not request.deliverables:
            return {}
        if not request.deliver_to:
            return {"imported": [], "failed": [], "skipped": [], "error": "deliver_to was not specified"}
        destination_root = Path(request.deliver_to).expanduser().resolve()
        if not destination_root.is_dir():
            return {"imported": [], "failed": [], "skipped": [], "error": "deliver_to is not an existing directory"}
        secret_root = self.layout.secret.resolve()
        if destination_root == secret_root or secret_root in destination_root.parents:
            return {"imported": [], "failed": [], "skipped": [], "error": "deliver_to must not be inside the secret area"}
        imported: list[str] = []
        failed: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        for relative in request.deliverables:
            source = _safe_member(workspace, relative)
            destination = _safe_member(destination_root, relative)
            if source is None or destination is None:
                failed.append(
                    {"path": relative, "reason": "path escapes the execution root or crosses a link"}
                )
                continue
            if not source.exists():
                failed.append({"path": relative, "reason": "missing in the execution workspace"})
                continue
            try:
                if source.is_dir():
                    report: dict[str, Any] = {"copied": [], "skipped": [], "errors": []}
                    _copy_tree_no_links(source, destination, report)
                    skipped.extend(report["skipped"])
                    for error in report["errors"]:
                        failed.append({"path": relative + "/" + error["path"], "reason": error["reason"]})
                    imported.extend(relative + "/" + item for item in report["copied"])
                else:
                    _copy_file_atomic(source, destination)
                    imported.append(relative)
            except OSError as error:
                failed.append({"path": relative, "reason": type(error).__name__})
        return {
            "imported": imported,
            "failed": failed,
            "skipped": skipped,
            "workspace": str(workspace),
        }

    def import_artifacts(self, exec_id: str) -> dict[str, Any]:
        """Explicitly import selected deliverables, including while resident."""
        state = self._state(exec_id)
        if state is None:
            raise ExecError("unknown execution id")
        if state.generation != self.epoch:
            raise ExecError("the execution id belongs to a previous runtime generation")
        if state.masking_status == "failed" or state.stop_reason == "masking_failed":
            raise ExecError("artifacts are unavailable because masking failed")
        request_data = read_json(self._exec_dir(exec_id) / "request.json", None)
        if not isinstance(request_data, dict):
            raise ExecError("the execution request is not available")
        request = ExecRequest(**{key: request_data[key] for key in ExecRequest.__dataclass_fields__ if key in request_data})
        workspace = Path(state.workspace or (self._exec_dir(exec_id) / "workspace"))
        try:
            supplied = list(self.vault.resolve(request.key_ids).values())
        except Exception:
            supplied = []
        result = self._mask_metadata(self._import_artifacts(request, workspace), supplied)
        state.artifacts = result
        self._write_state(exec_id, state)
        return result

    # ------------------------------------------------------------------ status
    def status(self, exec_id: str, caller_device: str | None = None, allow_other: bool = False) -> ExecState:
        request_data = read_json(self._exec_dir(exec_id) / "request.json", None)
        if caller_device and isinstance(request_data, dict):
            owner = request_data.get("device_id")
            if owner and owner != caller_device and not allow_other:
                return ExecState(
                    exec_id=exec_id,
                    status="rejected",
                    detail="caller_device_mismatch: only the issuing terminal may query this execution",
                    generation=self.epoch,
                )
        invalidated = read_json(self.layout.secret / "invalidated.json", {}) or {}
        if exec_id in (invalidated.get("exec_ids") or []):
            return ExecState(exec_id=exec_id, status="invalid", detail="invalidated by vault recreation", generation=self.epoch)
        state = self._state(exec_id)
        if state is None:
            return ExecState(exec_id=exec_id, status="unknown", detail="no state for this execution id", generation=self.epoch)
        if state.generation != self.epoch:
            return ExecState(
                exec_id=exec_id,
                status="unknown",
                detail="the id belongs to a previous runtime generation; state is not queryable",
                generation=state.generation,
            )
        if state.status == "running":
            pid_data = read_json(self._exec_dir(exec_id) / "pid.json", {}) or {}
            pid = int(pid_data.get("pid", 0) or 0)
            if pid and not _pid_alive(pid):
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    fresh = self._state(exec_id)
                    if fresh is not None and fresh.status != "running":
                        return fresh
                    monitor_data = read_json(self._exec_dir(exec_id) / "monitor.json", {}) or {}
                    if monitor_data.get("killed"):
                        break
                    time.sleep(0.1)
                monitor_data = read_json(self._exec_dir(exec_id) / "monitor.json", {}) or {}
                if monitor_data.get("killed"):
                    state.status = "stopped"
                    state.stop_reason = str(monitor_data.get("reason") or "stopped")
                else:
                    state.status = "unknown"
                    state.detail = "the child process ended without a queryable exit state"
                state.ended_at = state.ended_at or utc_now()
                self._write_state(exec_id, state)
        return state

    def stop(
        self,
        exec_id: str,
        reason: str = "owner_stop",
        caller_device: str | None = None,
        allow_other: bool = False,
    ) -> ExecState:
        directory = self._exec_dir(exec_id)
        if not directory.exists():
            return ExecState(exec_id=exec_id, status="unknown", detail="no such execution", generation=self.epoch)
        request_data = read_json(directory / "request.json", None)
        if caller_device and isinstance(request_data, dict):
            owner = request_data.get("device_id")
            if owner and owner != caller_device and not allow_other:
                return ExecState(
                    exec_id=exec_id,
                    status="rejected",
                    detail="caller_device_mismatch: only the issuing terminal may stop this execution",
                    generation=self.epoch,
                )
        with write_lock(directory / "stop.lock", 10):
            # A stop is an event, not a mutable setting: the monitor polls this
            # file while the child runs, so replacing it can fail on Windows and
            # a later caller must not rewrite the recorded reason.
            write_once_json(directory / "stop", {"reason": reason, "at": utc_now()})
        state = self._state(exec_id) or ExecState(exec_id=exec_id, status="unknown", generation=self.epoch)
        return state
