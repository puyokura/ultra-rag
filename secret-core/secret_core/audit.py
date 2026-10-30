"""Secret-free audit log.

Only ids, times, states, and stop reasons are recorded. The command text,
environment, raw output, and secret values are never written. The owner CLI can
delete audit history; no model-facing tool can. Losing the ability to append the
start record refuses new supply; an end-record failure does not fabricate a
successful history.
"""

from __future__ import annotations

from typing import Any

from contracts.config import Layout
from contracts.models import AuditEntry
from contracts.util import append_jsonl, ensure_dir, read_jsonl, utc_now


class AuditError(RuntimeError):
    pass


class AuditLog:
    def __init__(self, layout: Layout):
        self.directory = layout.secret / "audit"
        self.path = self.directory / "audit.jsonl"

    def start(self, entry: AuditEntry) -> None:
        ensure_dir(self.directory)
        try:
            append_jsonl(self.path, {"phase": "start", **entry.to_dict()})
        except OSError as error:
            raise AuditError(f"cannot write the start audit record: {error}") from error

    def finish(self, exec_id: str, state: str, stop_reason: str | None, ended_at: str | None = None) -> None:
        ensure_dir(self.directory)
        try:
            append_jsonl(
                self.path,
                {
                    "phase": "end",
                    "exec_id": exec_id,
                    "state": state,
                    "stop_reason": stop_reason,
                    "ended_at": ended_at or utc_now(),
                },
            )
        except OSError as error:
            raise AuditError(f"cannot write the end audit record: {error}") from error

    def read(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    def delete_all(self) -> int:
        if self.path.exists():
            rows = len(read_jsonl(self.path))
            self.path.unlink()
            return rows
        return 0
