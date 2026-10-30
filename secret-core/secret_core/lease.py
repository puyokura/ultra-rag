"""Permission lease shared by the execution manager and independent monitors.

A successful owner refresh records the current time. Supply and existing
executions require that record to stay within the configured lease (300 seconds
by default). Expiry stops managed executions even when the managing process is
unreachable; the monitor reads this file directly.
"""

from __future__ import annotations

import time

from contracts.config import Layout
from contracts.util import ensure_dir, read_json, utc_now, write_json, write_lock


class LeaseError(RuntimeError):
    pass


class LeaseManager:
    def __init__(self, layout: Layout, seconds: float = 300.0):
        self.layout = layout
        self.path = layout.secret / "control" / "lease.json"
        self.lock_file = layout.secret / "control" / "lease.lock"
        self.seconds = float(seconds)

    def refresh(self, source: str = "owner") -> dict:
        with write_lock(self.lock_file, 10):
            value = {"last_refresh": time.time(), "refreshed_at": utc_now(), "source": source}
            ensure_dir(self.path.parent)
            write_json(self.path, value)
            return value

    def age(self) -> float | None:
        data = read_json(self.path, None)
        if not isinstance(data, dict) or "last_refresh" not in data:
            return None
        return max(0.0, time.time() - float(data["last_refresh"]))

    def fresh(self) -> bool:
        age = self.age()
        return age is not None and age <= self.seconds

    def require_fresh(self) -> None:
        age = self.age()
        if age is None:
            raise LeaseError("no successful permission refresh has been recorded")
        if age > self.seconds:
            raise LeaseError(
                f"the last permission refresh is {int(age)}s old (limit {int(self.seconds)}s); new supply is refused"
            )
