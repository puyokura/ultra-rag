"""Direct Markdown editing with validated update proposals.

The editable workspace is deliberately outside the confirmed revision tree, the
search index, and every model-facing snapshot. A scan turns a valid edit into a
normal update proposal, so the existing verification, revision, and conflict
paths still decide whether it is published. Structural changes, unknown ids,
and file deletions never become memory changes.

Each file carries ``memory_id`` and ``base_revision`` in its front matter. After
an adopted edit the file keeps its path but is rewritten with the new memory id
and revision, so the user keeps one stable editing handle.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from contracts.models import MemoryRecord, Proposal
from contracts.util import atomic_write_text, ensure_dir, read_json, utc_now, write_json, write_lock
from rag.service import RagService
from rag.store import StoreError, normalized_statement

EDIT_MARKER = 1
ALLOWED_FRONT_KEYS = {
    "ultra_rag_edit",
    "memory_id",
    "base_revision",
    "kind",
    "scope",
    "project_id",
    "conditions",
    "exceptions",
}


class EditError(RuntimeError):
    pass


@dataclass
class EditScanResult:
    examined: int
    imported: int
    skipped: int
    failed: int
    deletions_ignored: int
    events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "imported": self.imported,
            "skipped": self.skipped,
            "failed": self.failed,
            "deletions_ignored": self.deletions_ignored,
            "events": self.events,
        }


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DirectEditor:
    def __init__(self, service: RagService, workspace: Path | None = None):
        self.service = service
        self.root = service.layout.rag / "edit"
        self.workspace = workspace or self.root / "workspace"
        self.state_path = self.root / "control" / "state.json"
        self.events_path = self.root / "control" / "events.jsonl"
        self.lock_file = self.root / "control" / "edit.lock"
        ensure_dir(self.workspace)
        ensure_dir(self.state_path.parent)
        for path in (self.root, self.workspace, self.state_path.parent):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass

    # ------------------------------------------------------------------ state
    def _state(self) -> dict[str, Any]:
        data = read_json(self.state_path, {}) or {}
        data.setdefault("files", {})
        return data

    def _save(self, state: dict[str, Any]) -> None:
        write_json(self.state_path, state)

    def _record_event(self, event: dict[str, Any]) -> None:
        ensure_dir(self.events_path.parent)
        with open(self.events_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({**event, "at": utc_now()}, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(self.events_path, 0o600)
        except OSError:
            pass

    # ----------------------------------------------------------------- export
    @staticmethod
    def _render(record: MemoryRecord) -> str:
        front = {
            "ultra_rag_edit": EDIT_MARKER,
            "memory_id": record.id,
            "base_revision": record.revision,
            "kind": record.kind,
            "scope": record.scope,
            "project_id": record.project_id,
            "conditions": list(record.conditions),
            "exceptions": list(record.exceptions),
        }
        return (
            "---\n"
            + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip()
            + "\n---\n"
            + record.statement.strip()
            + "\n"
        )

    def export(self) -> dict[str, Any]:
        with write_lock(self.lock_file, 30):
            state = self._state()
            records = sorted(
                self.service.store.load().memories.values(),
                key=lambda record: record.id,
            )
            live_ids = {record.id for record in records if record.status in ("adopted", "pending", "conflict")}
            by_id = {entry["memory_id"]: name for name, entry in state["files"].items() if entry.get("memory_id")}
            exported = 0
            created = 0
            for record in records:
                if record.status not in ("adopted", "pending", "conflict"):
                    continue
                name = by_id.get(record.id)
                path = self.workspace / (name or f"{record.id}.md")
                if path.exists():
                    continue
                text = self._render(record)
                atomic_write_text(path, text)
                state["files"][path.name] = {
                    "memory_id": record.id,
                    "base_revision": record.revision,
                    "imported_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "last_failed_hash": None,
                    "last_attempt": None,
                    "missing_reported": False,
                }
                exported += 1
                created += 1
            self._save(state)
            return {"exported": exported, "tracked": len(state["files"]), "live_memories": len(live_ids)}

    # ------------------------------------------------------------------- parse
    def _parse(self, path: Path) -> tuple[dict[str, Any], str]:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise EditError("front matter is missing")
        _, rest = text.split("---\n", 1)
        if "\n---\n" not in rest:
            raise EditError("front matter is not terminated")
        front_text, body = rest.split("\n---\n", 1)
        front = yaml.safe_load(front_text) or {}
        if not isinstance(front, dict):
            raise EditError("front matter must be a mapping")
        unknown = set(front) - ALLOWED_FRONT_KEYS
        if unknown:
            raise EditError(f"unsupported front matter fields: {sorted(unknown)}")
        if front.get("ultra_rag_edit") != EDIT_MARKER:
            raise EditError("unsupported edit format")
        memory_id = front.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id.startswith("mem_"):
            raise EditError("memory_id is missing or invalid")
        base_revision = front.get("base_revision")
        if not isinstance(base_revision, int) or base_revision < 0:
            raise EditError("base_revision must be a non-negative integer")
        conditions = front.get("conditions") or []
        exceptions = front.get("exceptions") or []
        if not isinstance(conditions, list) or not all(isinstance(item, str) for item in conditions):
            raise EditError("conditions must be a list of strings")
        if not isinstance(exceptions, list) or not all(isinstance(item, str) for item in exceptions):
            raise EditError("exceptions must be a list of strings")
        statement = body.strip()
        if not statement:
            raise EditError("statement body is empty")
        return front, statement

    # -------------------------------------------------------------------- scan
    def _failure(
        self,
        state: dict[str, Any],
        name: str,
        digest: str,
        reason: str,
        events: list[dict[str, Any]],
    ) -> None:
        entry = state["files"].setdefault(name, {})
        entry.update({"last_failed_hash": digest, "last_attempt": utc_now(), "last_error": reason})
        events.append({"file": name, "status": "rejected", "reason": reason})
        self._record_event({"file": name, "status": "rejected", "reason": reason})

    def scan(self, force: bool = False) -> EditScanResult:
        with write_lock(self.lock_file, 60):
            state = self._state()
            revision_state = self.service.store.load()
            workspace_files = {
                path.name: path
                for path in self.workspace.glob("*.md")
                if path.is_file() and not path.name.startswith(".")
            }
            examined = imported = skipped = failed = deletions = 0
            events: list[dict[str, Any]] = []
            for name, entry in list(state["files"].items()):
                if name not in workspace_files:
                    if not entry.get("missing_reported"):
                        deletions += 1
                        entry["missing_reported"] = True
                        events.append(
                            {
                                "file": name,
                                "status": "deletion_ignored",
                                "reason": "deleting an edit file is not a delete request; the memory is kept",
                            }
                        )
                        self._record_event(events[-1])
                    continue
                entry["missing_reported"] = False
            for name, path in sorted(workspace_files.items()):
                examined += 1
                digest = _file_hash(path)
                entry = state["files"].get(name)
                if entry is None:
                    entry = {"memory_id": None, "base_revision": None, "imported_hash": None}
                    state["files"][name] = entry
                if entry.get("imported_hash") == digest and not force:
                    skipped += 1
                    continue
                if not force and entry.get("last_failed_hash") == digest:
                    skipped += 1
                    continue
                try:
                    front, statement = self._parse(path)
                except EditError as error:
                    failed += 1
                    self._failure(state, name, digest, str(error), events)
                    continue
                memory_id = front["memory_id"]
                record = revision_state.memories.get(memory_id)
                if record is None:
                    failed += 1
                    self._failure(state, name, digest, "unknown or deleted memory_id", events)
                    continue
                previous_id = entry.get("memory_id")
                if previous_id and previous_id != memory_id:
                    failed += 1
                    self._failure(state, name, digest, "memory_id change is structural and is not auto-adopted", events)
                    continue
                if int(front["base_revision"]) > self.service.revision():
                    failed += 1
                    self._failure(state, name, digest, "base_revision is newer than the confirmed revision", events)
                    continue
                structural_error = None
                if front.get("kind") not in (None, record.kind):
                    structural_error = "kind change is structural and is not auto-adopted"
                elif front.get("scope") not in (None, record.scope):
                    structural_error = "scope change is structural and is not auto-adopted"
                elif front.get("project_id") not in (None, record.project_id):
                    structural_error = "project_id change is structural and is not auto-adopted"
                if structural_error:
                    failed += 1
                    self._failure(state, name, digest, structural_error, events)
                    continue
                if record.status in ("forgotten", "deleted"):
                    failed += 1
                    self._failure(state, name, digest, "the memory is not editable in its current state", events)
                    continue
                changes = (
                    normalized_statement(statement) != normalized_statement(record.statement)
                    or list(front.get("conditions") or []) != list(record.conditions)
                    or list(front.get("exceptions") or []) != list(record.exceptions)
                )
                if not changes:
                    entry.update(
                        {
                            "memory_id": record.id,
                            "base_revision": record.revision,
                            "imported_hash": digest,
                            "last_failed_hash": None,
                            "last_attempt": utc_now(),
                        }
                    )
                    self._save(state)
                    skipped += 1
                    continue
                proposal = Proposal(
                    op="update",
                    base_revision=self.service.revision(),
                    device_id=self.service.store.device_id,
                    target_id=record.id,
                    target_revision=int(front["base_revision"]),
                    statement=statement,
                    conditions=list(front.get("conditions") or []),
                    exceptions=list(front.get("exceptions") or []),
                    evidence_kind="direct_edit",
                    intent="direct markdown edit",
                    adoption="owner_operation",
                )
                try:
                    result = self.service.propose(proposal)
                except Exception as error:
                    failed += 1
                    self._failure(state, name, digest, f"proposal failed: {type(error).__name__}", events)
                    continue
                if result.status in ("adopted", "merged"):
                    new_record = revision_state.memories.get(result.memory_id or "")
                    if new_record is None:
                        new_record = self.service.store.load().memories.get(result.memory_id or "")
                    if new_record is not None:
                        text = self._render(new_record)
                        atomic_write_text(path, text)
                        entry.update(
                            {
                                "memory_id": new_record.id,
                                "base_revision": new_record.revision,
                                "imported_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                "last_failed_hash": None,
                                "last_attempt": utc_now(),
                            }
                        )
                    imported += 1
                    events.append(
                        {
                            "file": name,
                            "status": result.status,
                            "memory_id": result.memory_id,
                            "revision": result.revision,
                        }
                    )
                    self._record_event(events[-1])
                else:
                    failed += 1
                    self._failure(state, name, digest, f"proposal {result.status}: {result.message}", events)
                self._save(state)
            self._save(state)
            return EditScanResult(examined, imported, skipped, failed, deletions, events)

    # ------------------------------------------------------------------- watch
    def watch(self, interval: float = 2.0, stop_event: threading.Event | None = None, export_first: bool = False) -> None:
        stop = stop_event or threading.Event()
        if export_first:
            self.export()
        while not stop.is_set():
            try:
                self.scan()
            except Exception as error:
                self._record_event({"status": "scan_error", "reason": type(error).__name__})
            stop.wait(max(0.2, float(interval)))
