"""Registered-device authentication and stop control.

Every registered device may use every registered key (no per-key assignment), but
each device authenticates with its own token. Only the owner CLI can register,
list, or stop devices. Tokens are stored hashed in the registry; the local token
file exists so an unattended local execution can authenticate without a prompt,
and is protected by filesystem permissions rather than a recovery mechanism.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from contracts.config import Layout
from contracts.util import (
    atomic_write_text,
    ensure_dir,
    new_id,
    read_json,
    secure_token,
    utc_now,
    write_json,
    write_lock,
)
from secret_core.revocation import Revocations

DEVICE_RE = re.compile(r"^dev_[0-9a-f]{32}$")


class DeviceError(RuntimeError):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DeviceRegistry:
    def __init__(self, layout: Layout):
        self.layout = layout
        self.path = layout.secret / "devices.json"
        self.token_dir = layout.secret / "devices"
        self.lock_file = layout.secret / "control" / "devices.lock"
        self.revocations = Revocations(layout)

    def _read(self) -> dict:
        data = read_json(self.path, {}) or {}
        data.setdefault("devices", {})
        return data

    def _write(self, data: dict) -> None:
        write_json(self.path, data)

    def register(self, name: str) -> tuple[dict, str]:
        if not name.strip():
            raise DeviceError("device name is required")
        token = secure_token(32)
        with write_lock(self.lock_file, 10):
            data = self._read()
            device_id = new_id("dev")
            record = {
                "id": device_id,
                "name": name.strip(),
                "token_sha256": _hash(token),
                "status": "active",
                "created_at": utc_now(),
                "last_seen_at": None,
            }
            data["devices"][device_id] = record
            self._write(data)
        ensure_dir(self.token_dir)
        atomic_write_text(self.token_dir / f"{device_id}.token", token + "\n")
        return {key: value for key, value in record.items() if key != "token_sha256"}, token

    def ensure_local(self, device_id: str, name: str) -> dict:
        if not DEVICE_RE.match(device_id):
            raise DeviceError("configured device_id is invalid")
        if not self.path.exists():
            ensure_dir(self.path.parent)
            write_json(self.path, {"devices": {}})
        with write_lock(self.lock_file, 10):
            data = self._read()
            record = data["devices"].get(device_id)
            token_file = self.token_dir / f"{device_id}.token"
            if record is None:
                token = secure_token(32)
                record = {
                    "id": device_id,
                    "name": name,
                    "token_sha256": _hash(token),
                    "status": "active",
                    "created_at": utc_now(),
                    "last_seen_at": None,
                }
                data["devices"][device_id] = record
                self._write(data)
                ensure_dir(self.token_dir)
                atomic_write_text(token_file, token + "\n")
            elif not token_file.exists():
                raise DeviceError(
                    "this device is registered but its local token file is missing; "
                    "stop and register the device again from the owner CLI"
                )
            return {key: value for key, value in record.items() if key != "token_sha256"}

    def authenticate(self, device_id: str, token: str) -> bool:
        data = self._read()
        record = data["devices"].get(device_id)
        if not record or record.get("status") != "active":
            return False
        if self.revocations.device_revoked(device_id):
            return False
        if not hmac.compare_digest(record["token_sha256"], _hash(token)):
            return False
        with write_lock(self.lock_file, 10):
            data = self._read()
            if device_id in data["devices"]:
                data["devices"][device_id]["last_seen_at"] = utc_now()
                self._write(data)
        return True

    def authenticate_local(self, device_id: str) -> bool:
        token_file = self.token_dir / f"{device_id}.token"
        if not token_file.exists():
            return False
        return self.authenticate(device_id, token_file.read_text(encoding="utf-8").strip())

    def list_devices(self) -> list[dict]:
        data = self._read()
        rows = [
            {key: value for key, value in record.items() if key != "token_sha256"}
            for record in sorted(data["devices"].values(), key=lambda item: item.get("created_at", ""))
        ]
        invalidated = read_json(self.layout.secret / "invalidated.json", {}) or {}
        known = {row.get("id") for row in rows}
        for device_id in invalidated.get("device_ids") or []:
            if device_id not in known:
                rows.append(
                    {
                        "id": device_id,
                        "name": "(invalidated by vault recreate)",
                        "status": "invalid",
                        "created_at": None,
                    }
                )
        return rows

    def status(self, device_id: str) -> dict:
        if not DEVICE_RE.match(device_id):
            raise DeviceError("invalid device id")
        data = self._read()
        record = data["devices"].get(device_id)
        if record is not None:
            return {key: value for key, value in record.items() if key != "token_sha256"}
        invalidated = read_json(self.layout.secret / "invalidated.json", {}) or {}
        if device_id in (invalidated.get("device_ids") or []):
            return {"id": device_id, "name": "(invalidated by vault recreate)", "status": "invalid"}
        raise DeviceError("device is not registered")

    def stop(self, device_id: str) -> dict:
        if not DEVICE_RE.match(device_id):
            raise DeviceError("invalid device id")
        with write_lock(self.lock_file, 10):
            data = self._read()
            record = data["devices"].get(device_id)
            if record is None:
                raise DeviceError("device is not registered")
            record["status"] = "stopped"
            record["stopped_at"] = utc_now()
            self._write(data)
        self.revocations.revoke_device(device_id, "owner_stop")
        return {key: value for key, value in record.items() if key != "token_sha256"}
