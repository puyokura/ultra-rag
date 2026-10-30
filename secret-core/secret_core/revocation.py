"""Cross-process revocation control for keys and devices.

A CLI deletion, rotation, or device stop must stop an execution managed by a
different process. Monitors poll this file; it contains ids and timestamps only,
never a key value or command body.
"""

from __future__ import annotations

from contracts.config import Layout
from contracts.util import read_json, write_json, write_lock


class Revocations:
    def __init__(self, layout: Layout):
        self.path = layout.secret / "control" / "revoked.json"
        self.lock_file = layout.secret / "control" / "revoked.lock"

    def _read(self) -> dict:
        data = read_json(self.path, {}) or {}
        data.setdefault("keys", {})
        data.setdefault("devices", {})
        data.setdefault("rotated", {})
        return data

    def revoke_key(self, key_id: str, reason: str) -> None:
        with write_lock(self.lock_file, 10):
            data = self._read()
            data["keys"][key_id] = {"at": _now(), "reason": reason}
            write_json(self.path, data)

    def rotate_key(self, key_id: str) -> None:
        """Signal existing executions to stop without blocking new supply."""
        with write_lock(self.lock_file, 10):
            data = self._read()
            data["rotated"][key_id] = {"at": _now(), "epoch": _time()}
            write_json(self.path, data)

    def revoke_device(self, device_id: str, reason: str) -> None:
        with write_lock(self.lock_file, 10):
            data = self._read()
            data["devices"][device_id] = {"at": _now(), "reason": reason}
            write_json(self.path, data)

    def key_revoked(self, key_id: str) -> bool:
        return key_id in self._read()["keys"]

    def device_revoked(self, device_id: str) -> bool:
        return device_id in self._read()["devices"]

    def blocked(self, key_ids: list[str], device_id: str) -> str | None:
        data = self._read()
        for key_id in key_ids:
            if key_id in data["keys"]:
                return f"key_revoked:{key_id}"
        if device_id in data["devices"]:
            return "device_stopped"
        return None


def _now() -> str:
    from contracts.util import utc_now

    return utc_now()


def _time() -> float:
    import time

    return time.time()
