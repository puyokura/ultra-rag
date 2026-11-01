"""Owner-only password request broker for supported CLIs.

The value is read from the operating terminal (or an injected provider in tests),
used once, and never written to the request record, RAG, audit log, or vault. The
request TTL is five minutes by default. Adapters are limited to sudo and ssh;
arbitrary ``Password:`` prompts and web/GUI logins are out of scope.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from contracts.config import Layout, load_config
from contracts.models import HostKeyRecord, PasswordRequest
from contracts.util import (
    atomic_write_text,
    check_identifier,
    ensure_dir,
    new_id,
    read_json,
    utc_now,
    write_json,
    write_lock,
)
from secret_core.devices import DeviceRegistry

ALLOWED_ADAPTERS = ("sudo", "ssh")


class PasswordError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fingerprint_of_public_key(key_type: str, key_b64: str) -> str:
    blob = base64.b64decode(key_b64)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


# The askpass helper must not pass the password through cmd's expansion:
# metacharacters (& | < > ^) would be reinterpreted. PowerShell reads the
# environment variable directly, so the value is written verbatim.
_ASKPASS_CMD = (
    "@echo off\r\n"
    'powershell -NoProfile -Command "[Console]::Out.WriteLine($env:ULTRA_RAG_SSH_PASSWORD)"\r\n'
)


class PasswordBroker:
    def __init__(self, layout: Layout, config: dict | None = None):
        self.layout = layout
        self.config = config or load_config(layout, create=False)
        self.root = layout.password
        self.state_file = self.root / "requests.json"
        self.hostkeys_file = self.root / "hostkeys.json"
        self.known_hosts = self.root / "known_hosts"
        self.lock_file = self.root / "requests.lock"
        self.ttl = float(self.config.get("password", {}).get("request_ttl_seconds", 300))
        self.default_port = int(self.config.get("password", {}).get("ssh_port", 22))
        self.devices = DeviceRegistry(layout)
        self.device_id = str(self.config["device_id"])

    def _authenticate_terminal(self) -> None:
        if not self.devices.authenticate_local(self.device_id):
            raise PasswordError("the terminal device is not authenticated; re-register or stop/restart it from the owner CLI")

    def _find_for_terminal(self, request: PasswordRequest) -> PasswordRequest:
        if request.device_id != self.device_id:
            raise PasswordError("the password request belongs to another terminal")
        return request

    # ------------------------------------------------------------------ state
    def _read(self) -> dict:
        data = read_json(self.state_file, {}) or {}
        data.setdefault("requests", {})
        return data

    def _write(self, data: dict) -> None:
        ensure_dir(self.root)
        write_json(self.state_file, data)
        try:
            os.chmod(self.state_file, 0o600)
        except OSError:
            pass

    def _expire(self, request: PasswordRequest) -> PasswordRequest:
        if request.status == "pending" and _parse(request.expires_at) <= _now():
            request.status = "expired"
        return request

    def _find(self, request_id: str) -> PasswordRequest:
        if not re.match(r"^pw_[0-9a-f]{32}$", request_id):
            raise PasswordError("invalid password request id")
        data = self._read()
        row = data["requests"].get(request_id)
        if not row:
            raise PasswordError("password request not found")
        return PasswordRequest.from_dict(row)

    def _save(self, request: PasswordRequest) -> None:
        with write_lock(self.lock_file, 10):
            data = self._read()
            data["requests"][request.id] = request.to_dict()
            self._write(data)

    def _claim(self, request_id: str, new_status: str) -> PasswordRequest:
        """Atomically move a pending request to its consuming status.

        Two terminals (or two retries) must not both answer the same request;
        the check and the write happen under one lock.
        """
        with write_lock(self.lock_file, 10):
            data = self._read()
            row = data["requests"].get(request_id)
            if not row:
                raise PasswordError("password request not found")
            request = self._expire(PasswordRequest.from_dict(row))
            if request.status != "pending":
                data["requests"][request.id] = request.to_dict()
                self._write(data)
                raise PasswordError(f"request is {request.status}; a new request is required")
            request.status = new_status
            data["requests"][request.id] = request.to_dict()
            self._write(data)
            return request

    def create_request(
        self,
        host: str,
        account: str,
        command: str,
        adapter: str = "ssh",
        session_id: str | None = None,
        exec_id: str | None = None,
        host_key_fingerprint: str | None = None,
        port: int | None = None,
    ) -> PasswordRequest:
        self._authenticate_terminal()
        if adapter not in ALLOWED_ADAPTERS:
            raise PasswordError("adapter must be sudo or ssh")
        if not host.strip() or not account.strip() or not command.strip():
            raise PasswordError("host, account, and command are required")
        if adapter == "sudo" and host.strip().lower() not in ("local", "localhost", "127.0.0.1"):
            raise PasswordError("the sudo adapter runs on this machine; host must be local")
        if not exec_id:
            raise PasswordError("exec_id is required to bind the request to one terminal execution")
        check_identifier(exec_id, "exec_id")
        if host_key_fingerprint is not None and not re.match(r"^SHA256:[A-Za-z0-9+/]+$", host_key_fingerprint):
            raise PasswordError("host_key_fingerprint must have the SHA256:... form")
        request = PasswordRequest(
            id=new_id("pw"),
            host=self._normalize_host(host),
            account=account.strip(),
            command=command,
            adapter=adapter,
            created_at=_iso(_now()),
            expires_at=_iso(_now() + timedelta(seconds=self.ttl)),
            status="pending",
            session_id=session_id,
            device_id=self.device_id,
            exec_id=exec_id,
            host_key_fingerprint=host_key_fingerprint,
            port=int(port or self.default_port),
        )
        self._save(request)
        return request

    def list_requests(self, include_finished: bool = False) -> list[dict]:
        data = self._read()
        result = []
        for row in data["requests"].values():
            request = self._expire(PasswordRequest.from_dict(row))
            if request.device_id != self.device_id:
                continue
            if not include_finished and request.status not in ("pending",):
                continue
            result.append(request.to_dict())
        return sorted(result, key=lambda item: item["created_at"])

    def status(self, request_id: str) -> dict:
        self._authenticate_terminal()
        request = self._find_for_terminal(self._expire(self._find(request_id)))
        if request.status != "pending":
            self._save(request)
        return request.to_dict()

    def status_by_exec(self, exec_id: str) -> dict:
        self._authenticate_terminal()
        check_identifier(exec_id, "exec_id")
        rows = [
            self._expire(PasswordRequest.from_dict(row))
            for row in self._read()["requests"].values()
            if row.get("exec_id") == exec_id and row.get("device_id") == self.device_id
        ]
        if not rows:
            raise PasswordError("no password request is bound to that exec_id on this terminal")
        rows.sort(key=lambda item: item.created_at)
        request = rows[-1]
        if request.status != "pending":
            self._save(request)
        return request.to_dict()

    def cancel(self, request_id: str) -> dict:
        self._authenticate_terminal()
        request = self._find_for_terminal(self._find(request_id))
        if request.status == "pending":
            request.status = "cancelled"
            self._save(request)
        return request.to_dict()

    # --------------------------------------------------------------- host keys
    @staticmethod
    def _normalize_host(host: str) -> str:
        value = host.strip().lower()
        if value.startswith("[") and "]" in value:
            value = value[1 : value.index("]")]
        return value

    @staticmethod
    def _host_field(host: str, port: int) -> str:
        return host if port == 22 else f"[{host}]:{port}"

    def _hostkeys(self) -> list[dict]:
        data = read_json(self.hostkeys_file, {}) or {}
        if isinstance(data, dict) and "records" not in data:
            rows = list(data.values())
        elif isinstance(data, dict):
            rows = list(data.get("records", []))
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        records: list[dict] = []
        seen: set[tuple] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (row.get("host"), row.get("port"), row.get("key_type"), row.get("fingerprint"))
            if key in seen:
                continue
            seen.add(key)
            records.append(row)
        return records

    def _save_hostkeys(self, records: list[dict]) -> None:
        write_json(self.hostkeys_file, {"version": 2, "records": records})

    def scan_host_key(self, host: str, port: int | None = None) -> list[dict]:
        host = self._normalize_host(host)
        port = port or self.default_port
        ssh_keyscan = shutil.which("ssh-keyscan")
        if not ssh_keyscan:
            raise PasswordError("ssh-keyscan is not available; cannot verify the host key")
        result = subprocess.run(
            [ssh_keyscan, "-T", "5", "-p", str(port), host],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        keys: list[dict] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            key_type, key_b64 = parts[1], parts[2]
            keys.append(
                {
                    "host": host,
                    "port": port,
                    "key_type": key_type,
                    "public_key": key_b64,
                    "fingerprint": fingerprint_of_public_key(key_type, key_b64),
                }
            )
        if not keys:
            raise PasswordError(f"no host key could be scanned for {host}:{port}")
        return keys

    def accept_host_key(self, host: str, port: int, fingerprint: str, accepted_by: str = "owner") -> HostKeyRecord:
        host = self._normalize_host(host)
        keys = self.scan_host_key(host, port)
        match = next((item for item in keys if item["fingerprint"] == fingerprint), None)
        if match is None:
            raise PasswordError("the fingerprint does not match any currently scanned host key")
        ensure_dir(self.root)
        host_field = self._host_field(host, port)
        line = f"{host_field} {match['key_type']} {match['public_key']}"
        existing_lines = []
        if self.known_hosts.exists():
            existing_lines = [item for item in self.known_hosts.read_text(encoding="utf-8").splitlines() if item.strip()]
        if line not in existing_lines:
            existing_lines.append(line)
            atomic_write_text(self.known_hosts, "\n".join(existing_lines) + "\n")
        records = self._hostkeys()
        record = HostKeyRecord(
            host=host,
            port=port,
            key_type=match["key_type"],
            fingerprint=fingerprint,
            public_key=match["public_key"],
            accepted_at=utc_now(),
            accepted_by=accepted_by,
        )
        if not any(
            item.get("host") == host
            and item.get("port") == port
            and item.get("key_type") == match["key_type"]
            and item.get("fingerprint") == fingerprint
            for item in records
        ):
            records.append(record.to_dict())
            self._save_hostkeys(records)
        return record

    def known_host_keys(self) -> list[dict]:
        return self._hostkeys()

    def check_host_key(self, host: str, port: int | None = None) -> dict:
        host = self._normalize_host(host)
        port = port or self.default_port
        records = [item for item in self._hostkeys() if item.get("host") == host and int(item.get("port", 0)) == port]
        scanned = self.scan_host_key(host, port)
        current_fingerprints = {item["fingerprint"] for item in scanned}
        previous_fingerprints = {item.get("fingerprint") for item in records}
        if not records:
            status = "unknown"
        elif previous_fingerprints & current_fingerprints:
            status = "unchanged"
        else:
            status = "changed"
        return {
            "host": host,
            "port": port,
            "status": status,
            "records": records,
            "scanned": scanned,
        }

    def select_host_key(self, host: str, port: int, fingerprint: str | None) -> dict:
        host = self._normalize_host(host)
        records = [item for item in self._hostkeys() if item.get("host") == host and int(item.get("port", 0)) == port]
        if not records:
            raise PasswordError("host_key_not_accepted")
        if fingerprint:
            selected = next((item for item in records if item.get("fingerprint") == fingerprint), None)
            if selected is None:
                raise PasswordError("host_key_selection_not_accepted")
        elif len(records) > 1:
            raise PasswordError("host_key_selection_required: several keys are accepted for this host; specify a fingerprint")
        else:
            selected = records[0]
        # Do not run ssh-keyscan again here. Accept already recorded the key and
        # OpenSSH verifies the selected key against the temporary known_hosts at
        # connect time; a second scan is redundant and rate-limited by sshd.
        return selected

    def export_hostkeys(self) -> dict:
        lines = []
        if self.known_hosts.exists():
            lines = [line for line in self.known_hosts.read_text(encoding="utf-8").splitlines() if line.strip()]
        return {"version": 2, "records": self._hostkeys(), "known_hosts": lines}

    def import_hostkeys(self, data: dict) -> dict:
        records = self._hostkeys()
        incoming = data.get("records", []) if isinstance(data, dict) else []
        added = 0
        for row in incoming:
            if not isinstance(row, dict):
                continue
            key = (row.get("host"), row.get("port"), row.get("key_type"), row.get("fingerprint"))
            if any(
                (item.get("host"), item.get("port"), item.get("key_type"), item.get("fingerprint")) == key
                for item in records
            ):
                continue
            records.append(row)
            added += 1
        if added:
            self._save_hostkeys(records)
        lines = []
        if self.known_hosts.exists():
            lines = [line for line in self.known_hosts.read_text(encoding="utf-8").splitlines() if line.strip()]
        added_lines = 0
        for line in data.get("known_hosts", []) if isinstance(data, dict) else []:
            if isinstance(line, str) and line.strip() and line.strip() not in lines:
                lines.append(line.strip())
                added_lines += 1
        if added_lines:
            atomic_write_text(self.known_hosts, "\n".join(lines) + "\n")
        return {"records_added": added, "known_hosts_added": added_lines, "records_total": len(records)}

    # ---------------------------------------------------------------- execution
    def run(
        self,
        request_id: str,
        password: str,
        executable: list[str] | None = None,
        timeout: float = 120.0,
    ) -> dict:
        if not password:
            raise PasswordError("the password must not be empty")
        self._authenticate_terminal()
        request = self._find_for_terminal(self._expire(self._find(request_id)))
        if request.status != "pending":
            self._save(request)
            raise PasswordError(f"request is {request.status}; a new request is required")
        selected_host_key = None
        if request.adapter == "ssh":
            selected_host_key = self.select_host_key(request.host, request.port, request.host_key_fingerprint)
        # One-time binding: claim before invoking so a crash or a second
        # terminal cannot permit replay.
        request = self._claim(request.id, "provided")
        try:
            if request.adapter == "sudo":
                result = self._run_sudo(request, password, executable, timeout)
            else:
                result = self._run_ssh(request, password, executable, timeout, selected_host_key)
        except KeyboardInterrupt:
            request.status = "cancelled"
            self._save(request)
            raise
        except Exception:
            request.status = "failed"
            self._save(request)
            raise
        request.status = "consumed" if result["ok"] else "failed"
        self._save(request)
        return result

    def _run_sudo(self, request: PasswordRequest, password: str, executable: list[str] | None, timeout: float) -> dict:
        base = executable or ([shutil.which("sudo") or "sudo"])
        command = list(base) + ["-S", "-p", "", "/bin/sh", "-c", request.command]
        try:
            # If sudo would not ask for a password (cached credential or
            # NOPASSWD), sending it on stdin hands the value to the command.
            probe = subprocess.run(
                list(base) + ["-n", "true"],
                capture_output=True,
                text=True,
                timeout=min(timeout, 10.0),
                check=False,
            )
            if probe.returncode == 0:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            else:
                result = subprocess.run(
                    command,
                    input=password + "\n",
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            return {"ok": False, "status": "failed", "diagnostic": "timeout", "exit_code": None}
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        diagnostic = self._sudo_diagnostic(result.returncode, text)
        return {
            "ok": result.returncode == 0,
            "status": "consumed" if result.returncode == 0 else "failed",
            "diagnostic": diagnostic,
            "exit_code": result.returncode,
        }

    @staticmethod
    def _sudo_diagnostic(returncode: int, text: str) -> str:
        lowered = text.lower()
        if "incorrect password" in lowered or "sorry, try again" in lowered or "authentication failure" in lowered:
            return "authentication_failed"
        if returncode == 0:
            return "authenticated"
        return "command_failed"

    def _run_ssh(
        self,
        request: PasswordRequest,
        password: str,
        executable: list[str] | None,
        timeout: float,
        selected_host_key: dict | None = None,
    ) -> dict:
        port = int(request.port or self.default_port)
        if not self.known_hosts.exists():
            return {"ok": False, "status": "failed", "diagnostic": "host_key_not_accepted", "exit_code": None}
        helper_dir = Path(tempfile.mkdtemp(prefix="ultra-rag-askpass-", dir=str(self.layout.run)))
        helper = helper_dir / ("askpass.cmd" if os.name == "nt" else "askpass.sh")
        try:
            if os.name == "nt":
                helper.write_text(_ASKPASS_CMD, encoding="utf-8")
            else:
                helper.write_text('#!/bin/sh\nprintf \'%s\\n\' "$ULTRA_RAG_SSH_PASSWORD"\n', encoding="utf-8")
                os.chmod(helper, 0o700)
            base = executable or ([shutil.which("ssh") or "ssh"])
            host_key_options: list[str] = []
            known_hosts_path = self.known_hosts
            empty_known_hosts = helper_dir / "known_hosts.empty"
            empty_known_hosts.write_text("", encoding="utf-8")
            if selected_host_key and selected_host_key.get("key_type"):
                host_key_options = ["-o", f"HostKeyAlgorithms={selected_host_key['key_type']}"]
                selected_hosts = helper_dir / "known_hosts.selected"
                selected_hosts.write_text(
                    f"{self._host_field(request.host, port)} "
                    f"{selected_host_key['key_type']} {selected_host_key['public_key']}\n",
                    encoding="utf-8",
                )
                known_hosts_path = selected_hosts
            command = list(base) + [
                "-o",
                "BatchMode=no",
                "-o",
                "NumberOfPasswordPrompts=1",
                "-o",
                "PreferredAuthentications=password,keyboard-interactive",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "StrictHostKeyChecking=yes",
                *host_key_options,
                "-o",
                f"UserKnownHostsFile={known_hosts_path}",
                "-o",
                f"GlobalKnownHostsFile={empty_known_hosts}",
                "-o",
                "UpdateHostKeys=no",
                "-o",
                "ConnectTimeout=10",
                "-p",
                str(port),
                f"{request.account}@{request.host}",
                request.command,
            ]
            env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT", "HOME", "USERPROFILE", "TEMP", "TMP") if key in os.environ}
            env.update(
                {
                    "SSH_ASKPASS": str(helper),
                    "SSH_ASKPASS_REQUIRE": "force",
                    "DISPLAY": os.environ.get("DISPLAY", ":0"),
                    "ULTRA_RAG_SSH_PASSWORD": password,
                }
            )
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    env=env,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "status": "failed", "diagnostic": "timeout", "exit_code": None}
            text = (result.stdout or "") + "\n" + (result.stderr or "")
            lowered = text.lower()
            if "host key verification failed" in lowered or "remote host identification has changed" in lowered:
                return {"ok": False, "status": "failed", "diagnostic": "host_key_verification_failed", "exit_code": result.returncode}
            if "permission denied" in lowered or "authentication failed" in lowered:
                return {"ok": False, "status": "failed", "diagnostic": "authentication_failed", "exit_code": result.returncode}
            return {
                "ok": result.returncode == 0,
                "status": "consumed" if result.returncode == 0 else "failed",
                "diagnostic": "authenticated" if result.returncode == 0 else "command_failed",
                "exit_code": result.returncode,
            }
        finally:
            shutil.rmtree(helper_dir, ignore_errors=True)

    def run_with_prompt(
        self,
        request_id: str,
        prompt: Callable[[str], str],
        executable: list[str] | None = None,
        timeout: float = 120.0,
    ) -> dict:
        self._authenticate_terminal()
        request = self._find_for_terminal(self._expire(self._find(request_id)))
        if request.status != "pending":
            raise PasswordError(f"request is {request.status}; a new request is required")
        label = f"{request.host} / {request.account} / {request.command}\nPassword: "
        password = prompt(label)
        return self.run(request_id, password, executable=executable, timeout=timeout)
