"""Authoritative readable memory store with atomic revisions.

Canonical data is plain Markdown under ``revisions/<n>/``: memory files with
YAML front matter, session notes, and tombstones. SQLite is only a rebuildable
search index. A revision becomes visible only after every file is written, the
manifest is published, and ``control/current.json`` is replaced atomically.
"""

from __future__ import annotations

import base64
import copy
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from contracts.models import (
    ContractError,
    Excerpt,
    MemoryRecord,
    Proposal,
    ProposalResult,
    SourceRef,
    SyncBundle,
    SyncFile,
)
from contracts.util import (
    atomic_write_text,
    append_jsonl,
    canonical_json,
    check_identifier,
    ensure_dir,
    new_id,
    read_json,
    read_jsonl,
    safe_relative,
    sha256_hex,
    utc_now,
    write_json,
    write_lock,
)
from rag.excerpts import MessageText, build_provided_excerpts, extractive_note, select_structural_excerpts
from rag.profile import (
    render_constraints,
    render_profile,
    render_project_overview,
)
from rag.redaction import redact_text

MEMORY_FILE_RE = re.compile(r"^mem_[0-9a-f]{32}\.md$")
SESSION_FILE_RE = re.compile(r"^sess_[0-9a-zA-Z._-]+\.md$|^[0-9a-zA-Z._-]+\.md$")


class StoreError(RuntimeError):
    pass


class StoreNotInitialised(StoreError):
    pass


class StoreCorruption(StoreError):
    pass


@dataclass
class TurnRecord:
    turn: int
    user_message_id: str | None
    assistant_message_id: str | None
    recorded_at: str
    note: str = ""
    note_method: str = "extractive"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TurnRecord":
        return cls(
            turn=int(data["turn"]),
            user_message_id=data.get("user_message_id"),
            assistant_message_id=data.get("assistant_message_id"),
            recorded_at=data.get("recorded_at", utc_now()),
            note=data.get("note", ""),
            note_method=data.get("note_method", "extractive"),
        )


@dataclass
class SessionRecord:
    id: str
    project_id: str | None
    created_at: str
    updated_at: str
    turns: list[TurnRecord] = field(default_factory=list)
    excerpts: list[Excerpt] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": [turn.to_dict() for turn in self.turns],
            "excerpts": [excerpt.to_dict() for excerpt in self.excerpts],
        }

    def to_markdown(self) -> str:
        front = yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False).strip()
        lines = [f"---\n{front}\n---", f"# セッション {self.id}", ""]
        lines.append(f"- プロジェクト: {self.project_id or '(個人共通)'}")
        lines.append(f"- 開始: {self.created_at}")
        lines.append(f"- 最終更新: {self.updated_at}")
        lines.append("")
        by_turn: dict[int | None, list[Excerpt]] = {}
        for excerpt in self.excerpts:
            by_turn.setdefault(excerpt.turn, []).append(excerpt)
        for turn in sorted(self.turns, key=lambda item: item.turn):
            lines.append(f"## ターン {turn.turn}")
            lines.append("")
            lines.append(f"- 利用者発言ID: {turn.user_message_id or '-'}")
            lines.append(f"- 応答ID: {turn.assistant_message_id or '-'}")
            lines.append(f"- 記録: {turn.recorded_at}")
            lines.append("")
            if turn.note:
                label = "整理メモ" if turn.note_method != "extractive" else "整理メモ（原文抜粋からの抽出）"
                lines.append(f"### {label}")
                lines.append("")
                for note_line in turn.note.splitlines():
                    lines.append(f"> {note_line}" if note_line else ">")
                lines.append("")
            turn_excerpts = by_turn.get(turn.turn, [])
            if turn_excerpts:
                lines.append("### 原文抜粋")
                lines.append("")
                for excerpt in sorted(turn_excerpts, key=lambda item: item.start):
                    lines.append(
                        f"- {excerpt.role} / メッセージ {excerpt.message_id or '-'} "
                        f"/ 範囲 {excerpt.start}:{excerpt.end} / `{excerpt.id}`"
                        + (" / 編集済み（秘密除外。原文一致とは区別）" if excerpt.edited else "")
                    )
                    lines.append("")
                    for excerpt_line in excerpt.text.splitlines() or [""]:
                        lines.append(f"> {excerpt_line}" if excerpt_line else ">")
                    lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "SessionRecord":
        if not text.startswith("---\n"):
            raise StoreCorruption("session file has no front matter")
        _, rest = text.split("---\n", 1)
        if "\n---\n" not in rest:
            raise StoreCorruption("session file has unterminated front matter")
        front, _body = rest.split("\n---\n", 1)
        data = yaml.safe_load(front) or {}
        session_id = str(data["id"])
        check_identifier(session_id, "session_id")
        project_id = data.get("project_id")
        if project_id:
            check_identifier(str(project_id), "project_id")
        return cls(
            id=session_id,
            project_id=project_id,
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
            turns=[TurnRecord.from_dict(item) for item in data.get("turns", [])],
            excerpts=[Excerpt.from_dict(item) for item in data.get("excerpts", [])],
        )


@dataclass
class RevisionState:
    revision: int
    parent: int | None
    directory: Path
    manifest: dict[str, Any]
    memories: dict[str, MemoryRecord]
    sessions: dict[str, SessionRecord]
    tombstones: list[dict[str, Any]]
    updates: dict[str, dict[str, Any]]

    @property
    def manifest_sha256(self) -> str:
        return self.manifest["digest"]


def normalized_statement(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def manifest_digest(revision: int, files: dict[str, str]) -> str:
    return sha256_hex(canonical_json({"revision": revision, "files": files}))


class MemoryStore:
    """One authoritative store (home server role) or a read replica (client role)."""

    def __init__(
        self,
        root: Path | str,
        device_id: str,
        role: str = "server",
        max_excerpt_chars: int = 4000,
        lock_timeout: float = 30.0,
    ):
        self.root = Path(root)
        self.device_id = device_id
        self.role = role
        self.max_excerpt_chars = max_excerpt_chars
        self.lock_timeout = lock_timeout
        self.revisions_dir = self.root / "revisions"
        self.control_dir = self.root / "control"
        self.manifests_dir = self.control_dir / "manifests"
        self.current_file = self.control_dir / "current.json"
        self.lock_file = self.control_dir / "write.lock"
        self.queue_file = self.control_dir / "unsynced.jsonl"
        self.results_file = self.control_dir / "update-results.jsonl"
        self.session_state_file = self.control_dir / "session-state.json"

    # ------------------------------------------------------------------ basics
    def exists(self) -> bool:
        return self.current_file.exists()

    def initialize(self) -> RevisionState:
        with write_lock(self.lock_file, self.lock_timeout):
            if self.current_file.exists():
                return self.load()
            ensure_dir(self.root)
            ensure_dir(self.revisions_dir)
            ensure_dir(self.manifests_dir)
            self._render_revision(1, None, {}, {}, [], [])
            return self.load()

    def _revision_dir(self, revision: int) -> Path:
        return self.revisions_dir / f"{revision:06d}"

    def _manifest_path(self, revision: int) -> Path:
        return self.manifests_dir / f"{revision:06d}.json"

    def current_revision(self) -> int:
        data = read_json(self.current_file, None)
        if not isinstance(data, dict) or "revision" not in data:
            raise StoreNotInitialised(f"store is not initialised at {self.root}")
        return int(data["revision"])

    def current_manifest(self) -> dict[str, Any]:
        revision = self.current_revision()
        manifest = read_json(self._manifest_path(revision), None)
        if not isinstance(manifest, dict):
            raise StoreCorruption(f"manifest for revision {revision} is missing")
        return manifest

    def load(self, verify: bool = True) -> RevisionState:
        data = read_json(self.current_file, None)
        if not isinstance(data, dict):
            raise StoreNotInitialised(f"store is not initialised at {self.root}")
        revision = int(data["revision"])
        manifest = read_json(self._manifest_path(revision), None)
        if not isinstance(manifest, dict):
            raise StoreCorruption(f"manifest for revision {revision} is missing")
        directory = self._revision_dir(revision)
        if not directory.is_dir():
            raise StoreCorruption(f"revision directory is missing: {directory}")
        if verify:
            self._verify_directory(directory, manifest)
        memories: dict[str, MemoryRecord] = {}
        sessions: dict[str, SessionRecord] = {}
        for path in sorted((directory / "memories").glob("*.md")):
            record = MemoryRecord.from_markdown(path.read_text(encoding="utf-8"))
            memories[record.id] = record
        for path in sorted((directory / "candidates").glob("*.md")):
            record = MemoryRecord.from_markdown(path.read_text(encoding="utf-8"))
            memories[record.id] = record
        projects_dir = directory / "projects"
        if projects_dir.is_dir():
            for project_dir in sorted(path for path in projects_dir.iterdir() if path.is_dir()):
                for path in sorted((project_dir / "memories").glob("*.md")):
                    record = MemoryRecord.from_markdown(path.read_text(encoding="utf-8"))
                    memories[record.id] = record
        for path in sorted((directory / "sessions").glob("*.md")):
            record = SessionRecord.from_markdown(path.read_text(encoding="utf-8"))
            sessions[record.id] = record
        if projects_dir.is_dir():
            for project_dir in sorted(path for path in projects_dir.iterdir() if path.is_dir()):
                for path in sorted((project_dir / "sessions").glob("*.md")):
                    record = SessionRecord.from_markdown(path.read_text(encoding="utf-8"))
                    sessions[record.id] = record
        tombstones = read_jsonl(directory / "control" / "tombstones.jsonl")
        updates: dict[str, dict[str, Any]] = {}
        for row in read_jsonl(directory / "control" / "updates.jsonl"):
            updates[row["update_id"]] = row
        return RevisionState(
            revision=revision,
            parent=manifest.get("parent"),
            directory=directory,
            manifest=manifest,
            memories=memories,
            sessions=sessions,
            tombstones=tombstones,
            updates=updates,
        )

    def _verify_directory(self, directory: Path, manifest: dict[str, Any]) -> None:
        files = manifest.get("files", {})
        actual: dict[str, str] = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                relative = path.relative_to(directory).as_posix()
                actual[relative] = sha256_hex(path.read_bytes())
        if actual != files:
            raise StoreCorruption("revision file digests do not match the manifest")
        if manifest_digest(int(manifest["revision"]), files) != manifest.get("digest"):
            raise StoreCorruption("revision manifest digest is invalid")

    # --------------------------------------------------------------- rendering
    def _render_revision(
        self,
        revision: int,
        parent: int | None,
        memories: dict[str, MemoryRecord],
        sessions: dict[str, SessionRecord],
        tombstones: list[dict[str, Any]],
        updates: list[dict[str, Any]],
    ) -> None:
        directory = self._revision_dir(revision)
        if directory.exists():
            shutil.rmtree(directory)
        ensure_dir(directory)
        for folder in ("memories", "candidates", "profile", "sessions", "projects", "control"):
            ensure_dir(directory / folder)
        project_memories: dict[str, list[MemoryRecord]] = {}
        project_sessions: dict[str, list[str]] = {}
        for record in memories.values():
            if record.status in ("pending", "conflict"):
                atomic_write_text(directory / "candidates" / f"{record.id}.md", record.to_markdown())
            elif record.scope == "project" and record.project_id:
                target = directory / "projects" / record.project_id / "memories"
                ensure_dir(target)
                atomic_write_text(target / f"{record.id}.md", record.to_markdown())
                project_memories.setdefault(record.project_id, []).append(record)
            else:
                atomic_write_text(directory / "memories" / f"{record.id}.md", record.to_markdown())
        for record in sessions.values():
            text = record.to_markdown()
            if record.project_id:
                target = directory / "projects" / record.project_id / "sessions"
                ensure_dir(target)
                atomic_write_text(target / f"{record.id}.md", text)
                project_sessions.setdefault(record.project_id, []).append(record.id)
            else:
                atomic_write_text(directory / "sessions" / f"{record.id}.md", text)
        for project_id in sorted(set(project_memories) | set(project_sessions)):
            overview = render_project_overview(
                project_id,
                project_memories.get(project_id, []),
                project_sessions.get(project_id, []),
            )
            atomic_write_text(directory / "projects" / project_id / "overview.md", overview)
        atomic_write_text(directory / "profile" / "current.md", render_profile(list(memories.values()), revision))
        atomic_write_text(directory / "profile" / "constraints.md", render_constraints(list(memories.values()), revision))
        for tombstone in tombstones:
            append_jsonl(directory / "control" / "tombstones.jsonl", tombstone)
        for row in updates:
            append_jsonl(directory / "control" / "updates.jsonl", row)
        files: dict[str, str] = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                files[path.relative_to(directory).as_posix()] = sha256_hex(path.read_bytes())
        manifest = {
            "revision": revision,
            "parent": parent,
            "created_at": utc_now(),
            "files": files,
            "digest": manifest_digest(revision, files),
        }
        write_json(self._manifest_path(revision), manifest)
        write_json(self.current_file, {"revision": revision, "manifest": self._manifest_path(revision).name})

    # ---------------------------------------------------------------- proposals
    def _result(self, proposal: Proposal, status: str, memory_id: str | None, revision: int, message: str) -> ProposalResult:
        return ProposalResult(
            update_id=proposal.update_id,
            status=status,
            memory_id=memory_id,
            revision=revision,
            message=message,
        )

    def propose(self, proposal: Proposal) -> ProposalResult:
        proposal.validate()
        if self.role != "server":
            return self._queue_proposal(proposal)
        with write_lock(self.lock_file, self.lock_timeout):
            base = self.load()
            recorded = self._recorded_result(proposal.update_id)
            if recorded is not None:
                if recorded.get("fingerprint") != proposal.fingerprint():
                    return self._result(
                        proposal,
                        "rejected",
                        (recorded.get("result") or {}).get("memory_id"),
                        base.revision,
                        "the same update_id was already used with different content",
                    )
                return ProposalResult(**recorded["result"])
            previous = base.updates.get(proposal.update_id)
            if previous is not None:
                if previous.get("fingerprint") != proposal.fingerprint():
                    return self._result(
                        proposal,
                        "rejected",
                        previous.get("memory_id"),
                        base.revision,
                        "the same update_id was already used with different content",
                    )
                return ProposalResult(**previous["result"])
            if proposal.base_revision > base.revision:
                return self._result(
                    proposal,
                    "rejected",
                    None,
                    base.revision,
                    f"base_revision {proposal.base_revision} is newer than the store revision {base.revision}",
                )
            memories = copy.deepcopy(base.memories)
            sessions = copy.deepcopy(base.sessions)
            tombstones = copy.deepcopy(base.tombstones)
            updates = list(base.updates.values())
            if proposal.op == "create":
                result, changed = self._apply_create(proposal, base, memories, sessions, tombstones)
            elif proposal.op == "update":
                result, changed = self._apply_update(proposal, base, memories, sessions, tombstones)
            elif proposal.op == "forget":
                result, changed = self._apply_forget(proposal, base, memories, sessions, tombstones)
            elif proposal.op == "delete":
                result, changed = self._apply_delete(proposal, base, memories, sessions, tombstones)
            elif proposal.op == "confirm":
                result, changed = self._apply_confirm(proposal, base, memories, sessions, tombstones)
            else:  # pragma: no cover - validate() already restricts
                raise ContractError(f"unsupported operation: {proposal.op}")
            if not changed:
                self._remember_result(proposal, result)
                return result
            new_revision = base.revision + 1
            result.revision = new_revision
            self._stamp_new_revision(memories, sessions, new_revision)
            record_for_scope = memories.get(result.memory_id or "") or base.memories.get(proposal.target_id or "")
            entry = {
                "update_id": proposal.update_id,
                "fingerprint": proposal.fingerprint(),
                "status": result.status,
                "memory_id": result.memory_id,
                "scope": record_for_scope.scope if record_for_scope else None,
                "project_id": record_for_scope.project_id if record_for_scope else None,
                "revision": new_revision,
                "message": result.message,
                "at": utc_now(),
                "session_id": proposal.session_id,
                "result": result.to_dict(),
            }
            updates.append(entry)
            self._render_revision(new_revision, base.revision, memories, sessions, tombstones, updates)
            self._remember_result(proposal, result)
            return result

    @staticmethod
    def _stamp_new_revision(
        memories: dict[str, MemoryRecord], sessions: dict[str, SessionRecord], revision: int
    ) -> None:
        """Records changed in this proposal are stamped by the apply methods."""
        return None

    def _attested(self, proposal: Proposal, sessions: dict[str, Any]) -> bool:
        """Check a host/owner adoption claim before a record may be adopted.

        ``owner_operation`` is the owner CLI or an authenticated sync peer.
        ``user_message`` additionally has to match a stored user excerpt: the
        statement digest and the range are checked, so a caller that merely
        labels its own text cannot adopt it.
        """
        if proposal.adoption == "owner_operation":
            return True
        if proposal.adoption == "activity_summary":
            # The owner explicitly allows activity records to be saved without
            # confirmation. The record is excluded from profile injection.
            return proposal.kind == "activity_summary"
        if proposal.adoption != "user_message":
            return False
        data = proposal.attestation or {}
        if data.get("role") != "user":
            return False
        statement = (proposal.statement or "").strip()
        if not statement or sha256_hex(statement) != data.get("statement_digest"):
            return False
        session = sessions.get(str(data.get("session_id") or ""))
        if session is None:
            return False
        message_id = data.get("message_id")
        start, end = data.get("start"), data.get("end")
        if not message_id or not isinstance(start, int) or not isinstance(end, int):
            return False
        return any(
            excerpt.message_id == message_id and excerpt.start <= start and end <= excerpt.end
            for excerpt in session.excerpts
        )

    def _apply_create(
        self,
        proposal: Proposal,
        base: RevisionState,
        memories: dict[str, MemoryRecord],
        sessions: dict[str, SessionRecord],
        tombstones: list[dict[str, Any]],
    ) -> tuple[ProposalResult, bool]:
        statement_text, statement_edited = redact_text((proposal.statement or "").strip())
        statement = normalized_statement(statement_text)
        fingerprints = self._source_fingerprints(proposal.source_refs)
        forgotten = [
            existing
            for existing in memories.values()
            if existing.status == "forgotten"
            and set(self._source_fingerprints(existing.source_refs)) & set(fingerprints)
        ]
        if forgotten:
            return (
                self._result(
                    proposal,
                    "rejected",
                    forgotten[0].id,
                    base.revision,
                    "the source belongs to a forgotten memory; a new explicit statement is required",
                ),
                False,
            )
        for existing in memories.values():
            if (
                existing.status in ("adopted", "pending", "conflict")
                and existing.kind == proposal.kind
                and existing.scope == proposal.scope
                and existing.project_id == proposal.project_id
                and normalized_statement(existing.statement) == statement
                and tuple(existing.conditions) == tuple(proposal.conditions or [])
                and tuple(existing.exceptions) == tuple(proposal.exceptions or [])
            ):
                return (
                    self._result(proposal, "merged", existing.id, base.revision, f"an identical memory already exists with status {existing.status}"),
                    False,
                )
        evidence = proposal.evidence_kind or "extracted"
        blocked = [row for row in tombstones if set(row.get("source_fingerprints", [])) & set(fingerprints)]
        if blocked:
            return (
                self._result(
                    proposal,
                    "rejected",
                    None,
                    base.revision,
                    "the source was deleted; re-import does not revive the record",
                ),
                False,
            )
        adopted = evidence in ("explicit_statement", "explicit_registration", "direct_edit") and self._attested(
            proposal, sessions
        )
        status = "adopted" if adopted else "pending"
        conditions = [redact_text(item)[0] for item in (proposal.conditions or [])]
        exceptions = [redact_text(item)[0] for item in (proposal.exceptions or [])]
        refs = copy.deepcopy(proposal.source_refs)
        for ref in refs:
            if ref.note:
                ref.note = redact_text(ref.note)[0]
        record = MemoryRecord(
            id=new_id("mem"),
            kind=proposal.kind or "preference",
            scope=proposal.scope or "personal",
            statement=statement_text,
            project_id=proposal.project_id,
            status=status,
            evidence_kind=evidence,
            source_refs=refs,
            revision=base.revision + 1,
            recorded_at=utc_now(),
            effective_at=utc_now(),
            conditions=conditions,
            exceptions=exceptions,
            origin="conversation",
            note_method=None,
            owner_session_id=proposal.session_id if (proposal.scope == "session") else None,
            autoload=False if (proposal.kind == "activity_summary") else True,
        ).validate()
        memories[record.id] = record
        message = "adopted" if adopted else "stored as a confirmation candidate"
        return self._result(proposal, status, record.id, base.revision + 1, message), True

    def _explicit(self, proposal: Proposal) -> bool:
        return (proposal.evidence_kind or "extracted") in (
            "explicit_statement",
            "explicit_registration",
            "direct_edit",
        )

    def _apply_update(
        self,
        proposal: Proposal,
        base: RevisionState,
        memories: dict[str, MemoryRecord],
        sessions: dict[str, SessionRecord],
        tombstones: list[dict[str, Any]],
    ) -> tuple[ProposalResult, bool]:
        target = memories.get(proposal.target_id or "")
        if target is None:
            return self._result(proposal, "rejected", None, base.revision, "target memory does not exist"), False
        if target.status == "forgotten":
            return (
                self._result(
                    proposal,
                    "rejected",
                    target.id,
                    base.revision,
                    "target is forgotten; use create for an explicit new registration",
                ),
                False,
            )
        if target.status in ("deleted",):
            return self._result(proposal, "rejected", target.id, base.revision, "target memory is deleted"), False
        if target.scope == "session" and target.owner_session_id != proposal.session_id:
            return (
                self._result(
                    proposal,
                    "rejected",
                    target.id,
                    base.revision,
                    "session memory belongs to another session",
                ),
                False,
            )
        if not self._attested(proposal, sessions):
            return (
                self._result(
                    proposal,
                    "rejected",
                    target.id,
                    base.revision,
                    "an update of an existing memory requires the owner or an observed user message; no change was applied",
                ),
                False,
            )
        expected = proposal.target_revision if proposal.target_revision is not None else proposal.base_revision
        if target.revision > expected:
            same = proposal.statement is not None and normalized_statement(proposal.statement) == normalized_statement(target.statement)
            if same and proposal.conditions is None and proposal.exceptions is None:
                return self._result(proposal, "merged", target.id, base.revision, "the target already has the proposed content"), False
            return (
                self._result(
                    proposal,
                    "conflict",
                    target.id,
                    base.revision,
                    "the target changed after the proposal base revision; the last confirmed content is kept",
                ),
                False,
            )
        if target.status == "superseded":
            return (
                self._result(
                    proposal,
                    "rejected",
                    target.id,
                    base.revision,
                    "target is a superseded version; update the current adopted version",
                ),
                False,
            )
        new_statement = redact_text((proposal.statement or target.statement).strip())[0]
        new_record = MemoryRecord(
            id=new_id("mem"),
            kind=proposal.kind or target.kind,
            scope=proposal.scope or target.scope,
            statement=new_statement,
            project_id=proposal.project_id if proposal.scope is not None else target.project_id,
            status="adopted",
            evidence_kind=proposal.evidence_kind or "explicit_statement",
            source_refs=copy.deepcopy(proposal.source_refs) or copy.deepcopy(target.source_refs),
            revision=base.revision + 1,
            recorded_at=utc_now(),
            effective_at=utc_now(),
            conditions=[redact_text(item)[0] for item in proposal.conditions] if proposal.conditions is not None else list(target.conditions),
            exceptions=[redact_text(item)[0] for item in proposal.exceptions] if proposal.exceptions is not None else list(target.exceptions),
            supersedes=target.id,
            origin="conversation",
            note_method=None,
            owner_session_id=(proposal.session_id if (proposal.scope or target.scope) == "session" else None),
        ).validate()
        target.status = "superseded"
        target.revision = base.revision + 1
        memories[target.id] = target
        memories[new_record.id] = new_record
        return self._result(proposal, "adopted", new_record.id, base.revision + 1, "adopted a new version; the old version is kept as superseded"), True

    def _apply_forget(
        self,
        proposal: Proposal,
        base: RevisionState,
        memories: dict[str, MemoryRecord],
        sessions: dict[str, SessionRecord],
        tombstones: list[dict[str, Any]],
    ) -> tuple[ProposalResult, bool]:
        target = memories.get(proposal.target_id or "")
        if target is None:
            return self._result(proposal, "rejected", None, base.revision, "target memory does not exist"), False
        if target.status == "forgotten":
            return self._result(proposal, "forgotten", target.id, base.revision, "already forgotten"), False
        target.status = "forgotten"
        target.autoload = False
        target.revision = base.revision + 1
        memories[target.id] = target
        return self._result(proposal, "forgotten", target.id, base.revision + 1, "automatic reference stopped; the record remains for explicit reference"), True

    def _connected_ids(self, target_id: str, memories: dict[str, MemoryRecord]) -> set[str]:
        connected = {target_id}
        changed = True
        while changed:
            changed = False
            for record in memories.values():
                if record.id in connected:
                    if record.supersedes and record.supersedes not in connected:
                        connected.add(record.supersedes)
                        changed = True
                elif record.supersedes in connected:
                    connected.add(record.id)
                    changed = True
        return connected

    @staticmethod
    def _ref_fingerprint(ref: SourceRef) -> str:
        return sha256_hex(
            canonical_json(
                {
                    "session_id": ref.session_id,
                    "message_id": ref.message_id,
                    "start": ref.start,
                    "end": ref.end,
                }
            )
        )

    @classmethod
    def _source_fingerprints(cls, refs: list[SourceRef]) -> list[str]:
        return sorted(cls._ref_fingerprint(ref) for ref in refs)

    def _apply_delete(
        self,
        proposal: Proposal,
        base: RevisionState,
        memories: dict[str, MemoryRecord],
        sessions: dict[str, SessionRecord],
        tombstones: list[dict[str, Any]],
    ) -> tuple[ProposalResult, bool]:
        target = memories.get(proposal.target_id or "")
        if target is None:
            return self._result(proposal, "rejected", None, base.revision, "target memory does not exist"), False
        connected = self._connected_ids(target.id, memories)
        statements = [memories[item].statement for item in connected if item in memories]
        references = [ref for item in connected if item in memories for ref in memories[item].source_refs]
        deleted_fingerprints = {
            fingerprint
            for item in connected
            if item in memories
            for fingerprint in self._source_fingerprints(memories[item].source_refs)
        }
        closed_candidates = 0
        for memory_id, record in list(memories.items()):
            if memory_id in connected or record.status not in ("pending", "conflict"):
                continue
            if deleted_fingerprints & set(self._source_fingerprints(record.source_refs)):
                record.status = "rejected"
                record.autoload = False
                record.revision = base.revision + 1
                memories[memory_id] = record
                closed_candidates += 1
        for memory_id in connected:
            record = memories.pop(memory_id, None)
            if record is None:
                continue
            tombstones.append(
                {
                    "memory_id": memory_id,
                    "deleted_at": utc_now(),
                    "revision": base.revision + 1,
                    "scope": record.scope,
                    "project_id": record.project_id,
                    "reason": "user_delete",
                    "source_fingerprints": self._source_fingerprints(record.source_refs),
                }
            )
        self._redact_sessions(sessions, connected, statements, references)
        message = "blocked and removed the memory and its connected versions, excerpts, and candidates"
        if closed_candidates:
            message += f"; closed {closed_candidates} candidate(s) that cited the same evidence"
        return (
            self._result(
                proposal,
                "deleted",
                target.id,
                base.revision + 1,
                message,
            ),
            True,
        )

    def _redact_sessions(
        self,
        sessions: dict[str, SessionRecord],
        memory_ids: set[str],
        statements: list[str],
        references: list[SourceRef],
    ) -> None:
        ref_keys = {(ref.session_id, ref.message_id) for ref in references if ref.session_id and ref.message_id}
        normalized_statements = {normalized_statement(item) for item in statements if item}
        for session in sessions.values():
            kept: list[Excerpt] = []
            removed_turns: set[int] = set()
            for excerpt in session.excerpts:
                removed = False
                if normalized_statement(excerpt.text) in normalized_statements:
                    removed = True
                for session_id, message_id in ref_keys:
                    if session.id == session_id and excerpt.message_id == message_id:
                        removed = True
                if removed:
                    if excerpt.turn is not None:
                        removed_turns.add(excerpt.turn)
                    continue
                kept.append(excerpt)
            session.excerpts = kept
            for turn in session.turns:
                note_matches = bool(turn.note) and normalized_statement(turn.note) in normalized_statements
                if not note_matches and turn.turn not in removed_turns:
                    continue
                kept_for_turn = [item for item in kept if item.turn == turn.turn]
                turn.note = extractive_note(kept_for_turn) if kept_for_turn else ""
                turn.note_method = "extractive" if kept_for_turn else "deleted"

    def _apply_confirm(
        self,
        proposal: Proposal,
        base: RevisionState,
        memories: dict[str, MemoryRecord],
        sessions: dict[str, SessionRecord],
        tombstones: list[dict[str, Any]],
    ) -> tuple[ProposalResult, bool]:
        target = memories.get(proposal.target_id or "")
        if target is None:
            return self._result(proposal, "rejected", None, base.revision, "candidate does not exist"), False
        if target.status not in ("pending", "conflict"):
            return self._result(proposal, "rejected", target.id, base.revision, "target is not a pending candidate"), False
        if target.scope == "session" and target.owner_session_id != proposal.session_id:
            return self._result(proposal, "rejected", target.id, base.revision, "session memory belongs to another session"), False
        fingerprints = set(self._source_fingerprints(target.source_refs))
        if fingerprints and any(fingerprints & set(row.get("source_fingerprints", [])) for row in tombstones):
            return (
                self._result(
                    proposal,
                    "rejected",
                    target.id,
                    base.revision,
                    "the candidate cites deleted evidence; confirmation is refused",
                ),
                False,
            )
        if proposal.note == "reject":
            target.status = "rejected"
            target.autoload = False
            target.revision = base.revision + 1
            memories[target.id] = target
            return self._result(proposal, "rejected", target.id, base.revision + 1, "candidate rejected and retained"), True
        target.status = "adopted"
        target.autoload = True
        target.evidence_kind = "confirmed_inference" if target.evidence_kind == "extracted" else target.evidence_kind
        target.revision = base.revision + 1
        memories[target.id] = target
        return self._result(proposal, "adopted", target.id, base.revision + 1, "candidate confirmed and adopted"), True

    def _recorded_result(self, update_id: str) -> dict[str, Any] | None:
        if not self.results_file.exists():
            return None
        for row in read_jsonl(self.results_file):
            if row.get("update_id") == update_id:
                return row
        return None

    def _remember_result(self, proposal: Proposal, result: ProposalResult) -> None:
        """Record every terminal result independently of the revision tree.

        merged / conflict / rejected do not produce a revision, but the caller
        still needs the outcome for idempotent retries.
        """
        ensure_dir(self.control_dir)
        append_jsonl(
            self.results_file,
            {
                "update_id": proposal.update_id,
                "fingerprint": proposal.fingerprint(),
                "result": result.to_dict(),
                "at": utc_now(),
            },
        )

    def _queue_proposal(self, proposal: Proposal) -> ProposalResult:
        append_jsonl(
            self.queue_file,
            {
                "kind": "proposal",
                "queued_at": utc_now(),
                "payload": proposal.to_dict(),
                "status": "unsynced",
            },
        )
        return self._result(
            proposal,
            "unsynced",
            None,
            self.current_revision() if self.exists() else 0,
            "the home server is authoritative; the proposal is stored in the unsynced queue",
        )

    # ------------------------------------------------------------------- turns
    def record_turn(
        self,
        session_id: str,
        project_id: str | None,
        turn: int,
        user: MessageText,
        assistant: MessageText,
        provided_excerpts: list[dict] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        check_identifier(session_id, "session_id")
        if project_id:
            check_identifier(project_id, "project_id")
        if self.role != "server":
            # Validate before queueing and never persist the full transcript:
            # only the excerpts that will actually be stored leave this process.
            messages = [user, assistant]
            if provided_excerpts is not None:
                queued_excerpts = build_provided_excerpts(
                    provided_excerpts, messages, session_id, turn, max_chars=self.max_excerpt_chars
                )
            else:
                queued_excerpts = select_structural_excerpts(messages, session_id, turn)
            if not queued_excerpts:
                return {
                    "saved": False,
                    "unsynced": True,
                    "reason": "no important content selected",
                    "revision": self.current_revision(),
                }
            append_jsonl(
                self.queue_file,
                {
                    "kind": "turn",
                    "queued_at": utc_now(),
                    "payload": {
                        "session_id": session_id,
                        "project_id": project_id,
                        "turn": turn,
                        "user_message_id": user.message_id,
                        "assistant_message_id": assistant.message_id,
                        "excerpts": [item.to_dict() for item in queued_excerpts],
                        "note": redact_text(note)[0] if note else None,
                    },
                    "status": "unsynced",
                },
            )
            return {"saved": False, "unsynced": True, "revision": self.current_revision()}
        with write_lock(self.lock_file, self.lock_timeout):
            base = self.load()
            sessions = copy.deepcopy(base.sessions)
            session = sessions.get(session_id)
            if session is None:
                session = SessionRecord(
                    id=session_id,
                    project_id=project_id,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            existing = next((item for item in session.turns if item.turn == turn), None)
            if existing is not None:
                if existing.user_message_id == user.message_id and existing.assistant_message_id == assistant.message_id:
                    return {
                        "saved": True,
                        "already_recorded": True,
                        "revision": base.revision,
                        "excerpts": len([item for item in session.excerpts if item.turn == turn]),
                    }
                raise StoreError(f"turn {turn} is already recorded with different message ids")
            messages = [user, assistant]
            if provided_excerpts is not None:
                excerpts = build_provided_excerpts(
                    provided_excerpts, messages, session_id, turn, max_chars=self.max_excerpt_chars
                )
            else:
                excerpts = select_structural_excerpts(messages, session_id, turn)
            blocked_fingerprints = {
                fingerprint
                for tombstone in base.tombstones
                for fingerprint in tombstone.get("source_fingerprints", [])
            }
            if blocked_fingerprints:
                allowed: list[Excerpt] = []
                for excerpt in excerpts:
                    ref = SourceRef(
                        session_id=excerpt.session_id,
                        message_id=excerpt.message_id,
                        start=excerpt.start,
                        end=excerpt.end,
                    )
                    if self._ref_fingerprint(ref) in blocked_fingerprints:
                        continue
                    allowed.append(excerpt)
                excerpts = allowed
            if not excerpts:
                return {"saved": False, "revision": base.revision, "reason": "no important content selected"}
            note_text = redact_text(note.strip())[0] if note else extractive_note(excerpts)
            note_method = "provided" if note else "extractive"
            session.excerpts.extend(excerpts)
            session.turns.append(
                TurnRecord(
                    turn=turn,
                    user_message_id=user.message_id,
                    assistant_message_id=assistant.message_id,
                    recorded_at=utc_now(),
                    note=note_text,
                    note_method=note_method,
                )
            )
            session.updated_at = utc_now()
            sessions[session_id] = session
            new_revision = base.revision + 1
            self._render_revision(
                new_revision,
                base.revision,
                copy.deepcopy(base.memories),
                sessions,
                copy.deepcopy(base.tombstones),
                list(base.updates.values()),
            )
            return {
                "saved": True,
                "revision": new_revision,
                "excerpts": len(excerpts),
                "note_method": note_method,
            }

    # ----------------------------------------------------------------- queries
    def list_memories(self, include_pending: bool = False) -> list[MemoryRecord]:
        state = self.load()
        records = [
            record
            for record in state.memories.values()
            if record.status == "adopted" or include_pending
        ]
        return sorted(records, key=lambda item: (item.scope, item.project_id or "", item.kind, item.recorded_at, item.id))

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        return self.load().memories.get(memory_id)

    def updates_after(self, revision: int) -> list[dict[str, Any]]:
        rows = [row for row in self.load().updates.values() if int(row.get("revision", 0)) > revision]
        return sorted(rows, key=lambda row: (row.get("revision", 0), row.get("at", "")))

    def tombstone_matches(self, source_fingerprints: set[str]) -> list[dict[str, Any]]:
        state = self.load()
        return [
            row
            for row in state.tombstones
            if set(row.get("source_fingerprints", [])) & source_fingerprints
        ]

    # -------------------------------------------------------------------- sync
    def sync_bundle(self, since: int) -> SyncBundle:
        state = self.load()
        if since > state.revision:
            raise StoreError(f"since={since} is newer than the current revision {state.revision}")
        current_files = state.manifest["files"]
        if since == state.revision:
            return SyncBundle(
                revision=state.revision,
                files=[],
                manifest_files=dict(current_files),
                deleted_paths=[],
                tombstones=copy.deepcopy(state.tombstones),
                manifest_sha256=state.manifest["digest"],
                full=False,
                created_at=utc_now(),
            )
        base_manifest = read_json(self._manifest_path(since), None) if since > 0 else None
        if not isinstance(base_manifest, dict):
            base_files: dict[str, str] = {}
            full = True
        else:
            base_files = base_manifest.get("files", {})
            full = False
        changed: list[SyncFile] = []
        for path, digest in current_files.items():
            if base_files.get(path) != digest:
                content = (state.directory / Path(path)).read_bytes()
                changed.append(SyncFile(path=path, sha256=digest, content_b64=base64.b64encode(content).decode("ascii")))
        deleted = sorted(path for path in base_files if path not in current_files)
        return SyncBundle(
            revision=state.revision,
            files=changed,
            manifest_files=dict(current_files),
            deleted_paths=deleted,
            tombstones=copy.deepcopy(state.tombstones),
            manifest_sha256=state.manifest["digest"],
            full=full,
            created_at=utc_now(),
        )

    def record_synced_turn(
        self,
        session_id: str,
        project_id: str | None,
        turn: int,
        user_message_id: str | None,
        assistant_message_id: str | None,
        excerpts: list[dict[str, Any]],
        note: str | None = None,
    ) -> dict[str, Any]:
        """Append a turn whose excerpts were validated by the peer replica."""
        check_identifier(session_id, "session_id")
        if project_id:
            check_identifier(project_id, "project_id")
        parsed = [Excerpt.from_dict(item) for item in excerpts]
        if not parsed:
            return {"saved": False, "reason": "no important content selected", "revision": self.current_revision()}
        for item in parsed:
            item.session_id = session_id
            item.turn = int(turn)
        with write_lock(self.lock_file, self.lock_timeout):
            base = self.load()
            sessions = copy.deepcopy(base.sessions)
            session = sessions.get(session_id)
            if session is None:
                session = SessionRecord(
                    id=session_id,
                    project_id=project_id,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            existing = next((item for item in session.turns if item.turn == turn), None)
            if existing is not None:
                if existing.user_message_id == user_message_id and existing.assistant_message_id == assistant_message_id:
                    return {
                        "saved": True,
                        "already_recorded": True,
                        "revision": base.revision,
                        "excerpts": len([item for item in session.excerpts if item.turn == turn]),
                    }
                raise StoreError(f"turn {turn} is already recorded with different message ids")
            blocked = {
                fingerprint
                for tombstone in base.tombstones
                for fingerprint in tombstone.get("source_fingerprints", [])
            }
            if blocked:
                allowed = []
                for excerpt in parsed:
                    ref = SourceRef(
                        session_id=excerpt.session_id,
                        message_id=excerpt.message_id,
                        start=excerpt.start,
                        end=excerpt.end,
                    )
                    if self._ref_fingerprint(ref) in blocked:
                        continue
                    allowed.append(excerpt)
                parsed = allowed
            if not parsed:
                return {"saved": False, "revision": base.revision, "reason": "no important content selected"}
            note_text = redact_text(note.strip())[0] if note else extractive_note(parsed)
            note_method = "provided" if note else "extractive"
            session.excerpts.extend(parsed)
            session.turns.append(
                TurnRecord(
                    turn=turn,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    recorded_at=utc_now(),
                    note=note_text,
                    note_method=note_method,
                )
            )
            session.updated_at = utc_now()
            sessions[session_id] = session
            new_revision = base.revision + 1
            self._render_revision(
                new_revision,
                base.revision,
                copy.deepcopy(base.memories),
                sessions,
                copy.deepcopy(base.tombstones),
                list(base.updates.values()),
            )
            return {
                "saved": True,
                "revision": new_revision,
                "excerpts": len(parsed),
                "note_method": note_method,
            }

    def apply_bundle(self, bundle: SyncBundle) -> int:
        if self.role == "server":
            raise StoreError("apply_bundle is only valid for a client replica")
        ensure_dir(self.revisions_dir)
        ensure_dir(self.manifests_dir)
        with write_lock(self.lock_file, self.lock_timeout):
            if bundle.revision <= (self.current_revision() if self.exists() else 0):
                return self.current_revision() if self.exists() else 0
            files = dict(bundle.manifest_files)
            if manifest_digest(bundle.revision, files) != bundle.manifest_sha256:
                raise StoreCorruption("sync bundle manifest digest is invalid")
            base_revision = self.current_revision() if self.exists() else 0
            base_dir = self._revision_dir(base_revision) if base_revision else None
            directory = self._revision_dir(bundle.revision)
            temporary = directory.with_name(f".tmp-{bundle.revision:06d}-{os.getpid()}")
            if temporary.exists():
                shutil.rmtree(temporary)
            if bundle.full or base_dir is None:
                ensure_dir(temporary)
            else:
                shutil.copytree(base_dir, temporary)
            for path in bundle.deleted_paths:
                target = temporary / Path(safe_relative(path))
                if target.exists():
                    target.unlink()
            for item in bundle.files:
                relative = safe_relative(item.path)
                target = temporary / Path(relative)
                ensure_dir(target.parent)
                content = base64.b64decode(item.content_b64 or "")
                if sha256_hex(content) != item.sha256:
                    raise StoreCorruption(f"sync file digest mismatch: {relative}")
                target.write_bytes(content)
            actual: dict[str, str] = {}
            for path in sorted(temporary.rglob("*")):
                if path.is_file():
                    actual[path.relative_to(temporary).as_posix()] = sha256_hex(path.read_bytes())
            if actual != files:
                raise StoreCorruption("materialized replica does not match the bundle manifest")
            if directory.exists():
                shutil.rmtree(directory)
            os.replace(temporary, directory)
            manifest = {
                "revision": bundle.revision,
                "parent": base_revision or None,
                "created_at": utc_now(),
                "files": files,
                "digest": bundle.manifest_sha256,
            }
            write_json(self._manifest_path(bundle.revision), manifest)
            write_json(self.current_file, {"revision": bundle.revision, "manifest": self._manifest_path(bundle.revision).name})
            return bundle.revision
