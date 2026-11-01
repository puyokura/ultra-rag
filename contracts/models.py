"""Cross-feature contracts: memory, execution, authentication requests, audit.

These dataclasses are the single definition used by the RAG, key-use,
password-input, CLI, HTTP, and MCP surfaces. They deliberately contain no
secret-value fields: no contract in this project can carry an API key or a
password value back to a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Iterable, Literal

import yaml

from contracts.util import check_identifier, content_hash, new_id, utc_now

MEMORY_KINDS = (
    "philosophy",
    "design_principle",
    "preference",
    "prohibition",
    "requirement",
    "lesson",
    "project_decision",
)
MEMORY_SCOPES = ("personal", "shared_lesson", "project", "session")
MEMORY_STATUSES = ("adopted", "pending", "conflict", "superseded", "forgotten", "deleted", "rejected")
EVIDENCE_KINDS = (
    "explicit_statement",
    "explicit_registration",
    "confirmed_inference",
    "direct_edit",
    "imported_statement",
    "extracted",
)
ADOPTION_KINDS = ("owner_operation", "user_message")
KIND_SCOPE_RULES = {"project_decision": ("project",)}
PROPOSAL_OPS = ("create", "update", "forget", "delete", "confirm")
PROPOSAL_STATUSES = (
    "adopted",
    "pending",
    "conflict",
    "merged",
    "forgotten",
    "deleted",
    "rejected",
    "duplicate",
    "unsynced",
    "pending_confirmation",
)
EXEC_STATUSES = ("accepted", "running", "finished", "failed", "stopped", "unknown", "rejected")
PASSWORD_STATUSES = ("pending", "provided", "expired", "cancelled", "consumed", "failed")


class ContractError(ValueError):
    """A value violates the published contract."""


def _require_choice(value: str, choices: Iterable[str], field_name: str) -> str:
    allowed = tuple(choices)
    if value not in allowed:
        raise ContractError(f"{field_name} must be one of {allowed}, got {value!r}")
    return value


@dataclass
class SourceRef:
    session_id: str | None = None
    message_id: str | None = None
    excerpt_id: str | None = None
    start: int | None = None
    end: int | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceRef":
        return cls(
            session_id=data.get("session_id"),
            message_id=data.get("message_id"),
            excerpt_id=data.get("excerpt_id"),
            start=data.get("start"),
            end=data.get("end"),
            note=data.get("note"),
        )


@dataclass
class MemoryRecord:
    id: str
    kind: str
    scope: str
    statement: str
    project_id: str | None
    status: str
    evidence_kind: str
    source_refs: list[SourceRef]
    revision: int
    recorded_at: str
    effective_at: str
    owner_session_id: str | None = None
    conditions: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    supersedes: str | None = None
    autoload: bool = True
    origin: str = "conversation"
    note_method: str | None = None

    def validate(self) -> "MemoryRecord":
        _require_choice(self.kind, MEMORY_KINDS, "kind")
        _require_choice(self.scope, MEMORY_SCOPES, "scope")
        _require_choice(self.status, MEMORY_STATUSES, "status")
        _require_choice(self.evidence_kind, EVIDENCE_KINDS, "evidence_kind")
        if not self.statement or not self.statement.strip():
            raise ContractError("statement must not be empty")
        if self.scope == "project" and not self.project_id:
            raise ContractError("project scope requires project_id")
        allowed_scopes = KIND_SCOPE_RULES.get(self.kind)
        if allowed_scopes and self.scope not in allowed_scopes:
            raise ContractError(f"{self.kind} requires scope in {allowed_scopes}, got {self.scope!r}")
        if self.scope == "session" and not self.owner_session_id:
            raise ContractError("session scope requires owner_session_id")
        if self.scope != "session" and self.owner_session_id:
            raise ContractError("only session memories may carry an owner_session_id")
        if self.project_id is not None:
            check_identifier(self.project_id, "project_id")
            if not self.project_id.startswith("proj_"):
                raise ContractError("project_id must start with proj_")
        if self.scope != "project" and self.scope != "session" and self.project_id:
            raise ContractError("personal/shared memories must not carry a project_id")
        if self.revision < 0:
            raise ContractError("revision must be non-negative")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "scope": self.scope,
            "statement": self.statement,
            "project_id": self.project_id,
            "status": self.status,
            "evidence_kind": self.evidence_kind,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
            "revision": self.revision,
            "recorded_at": self.recorded_at,
            "effective_at": self.effective_at,
            "owner_session_id": self.owner_session_id,
            "conditions": list(self.conditions),
            "exceptions": list(self.exceptions),
            "supersedes": self.supersedes,
            "autoload": self.autoload,
            "origin": self.origin,
            "note_method": self.note_method,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        record = cls(
            id=data["id"],
            kind=data["kind"],
            scope=data["scope"],
            statement=data["statement"],
            project_id=data.get("project_id"),
            status=data["status"],
            evidence_kind=data["evidence_kind"],
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            revision=int(data.get("revision", 0)),
            recorded_at=data["recorded_at"],
            effective_at=data.get("effective_at", data["recorded_at"]),
            owner_session_id=data.get("owner_session_id"),
            conditions=list(data.get("conditions", [])),
            exceptions=list(data.get("exceptions", [])),
            supersedes=data.get("supersedes"),
            autoload=bool(data.get("autoload", True)),
            origin=data.get("origin", "conversation"),
            note_method=data.get("note_method"),
        )
        return record.validate()

    def to_markdown(self) -> str:
        meta = self.to_dict()
        meta.pop("statement")
        front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{front}\n---\n{self.statement.strip()}\n"

    @classmethod
    def from_markdown(cls, text: str) -> "MemoryRecord":
        if not text.startswith("---\n"):
            raise ContractError("memory file has no front matter")
        _, rest = text.split("---\n", 1)
        if "\n---\n" not in rest:
            raise ContractError("memory file has unterminated front matter")
        front, body = rest.split("\n---\n", 1)
        data = yaml.safe_load(front) or {}
        if not isinstance(data, dict):
            raise ContractError("memory front matter must be a mapping")
        data["statement"] = body.strip()
        return cls.from_dict(data)


@dataclass
class Proposal:
    op: str
    base_revision: int
    device_id: str
    session_id: str | None = None
    update_id: str = field(default_factory=lambda: new_id("upd"))
    target_id: str | None = None
    kind: str | None = None
    scope: str | None = None
    project_id: str | None = None
    statement: str | None = None
    conditions: list[str] | None = None
    exceptions: list[str] | None = None
    evidence_kind: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)
    intent: str | None = None
    target_revision: int | None = None
    confirm: bool = False
    note: str | None = None
    adoption: str | None = None
    attestation: dict[str, Any] | None = None

    def validate(self) -> "Proposal":
        _require_choice(self.op, PROPOSAL_OPS, "op")
        if self.base_revision < 0:
            raise ContractError("base_revision must be non-negative")
        if not self.device_id:
            raise ContractError("device_id is required")
        if self.project_id is not None:
            check_identifier(self.project_id, "project_id")
            if not self.project_id.startswith("proj_"):
                raise ContractError("project_id must start with proj_")
        if self.session_id is not None:
            check_identifier(self.session_id, "session_id")
        if self.target_id is not None:
            check_identifier(self.target_id, "target_id")
        if self.op in ("update", "forget", "delete", "confirm") and not self.target_id:
            raise ContractError(f"{self.op} requires target_id")
        if self.op == "create":
            if not self.kind or not self.scope or not self.statement:
                raise ContractError("create requires kind, scope, and statement")
            _require_choice(self.kind, MEMORY_KINDS, "kind")
            _require_choice(self.scope, MEMORY_SCOPES, "scope")
            if self.evidence_kind:
                _require_choice(self.evidence_kind, EVIDENCE_KINDS, "evidence_kind")
            if self.scope == "project" and not self.project_id:
                raise ContractError("project scope requires project_id")
            allowed_scopes = KIND_SCOPE_RULES.get(self.kind)
            if allowed_scopes and self.scope not in allowed_scopes:
                raise ContractError(f"{self.kind} requires scope in {allowed_scopes}, got {self.scope!r}")
            if self.scope == "session" and not self.session_id:
                raise ContractError("session scope requires session_id")
        if self.op == "update":
            if not (self.statement or self.kind or self.scope or self.conditions is not None or self.exceptions is not None):
                raise ContractError("update requires at least one changed field")
        if self.op == "delete" and not self.confirm:
            raise ContractError("delete requires explicit confirmation")
        if self.op == "confirm" and self.note not in (None, "adopt", "reject"):
            raise ContractError("confirm note must be 'adopt', 'reject', or omitted")
        self._validate_adoption()
        return self

    def _validate_adoption(self) -> None:
        """Adoption is a host/owner claim, not a model-supplied label.

        A user-message claim must name the observed session message and carry a
        digest of the exact statement, so the store can check it against the
        stored excerpts instead of trusting a classification string.
        """
        if self.adoption is None:
            return
        _require_choice(self.adoption, ADOPTION_KINDS, "adoption")
        if self.adoption != "user_message":
            return
        data = self.attestation or {}
        if not isinstance(data, dict):
            raise ContractError("attestation must be an object")
        if data.get("role") != "user":
            raise ContractError("user_message adoption must attest a user role")
        if not data.get("session_id") or not data.get("message_id"):
            raise ContractError("user_message adoption requires session_id and message_id")
        if not isinstance(data.get("start"), int) or not isinstance(data.get("end"), int):
            raise ContractError("user_message adoption requires an integer range")
        if not data.get("statement_digest"):
            raise ContractError("user_message adoption requires statement_digest")

    def fingerprint(self) -> str:
        payload = self.to_dict()
        payload.pop("update_id", None)
        return content_hash(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "base_revision": self.base_revision,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "update_id": self.update_id,
            "target_id": self.target_id,
            "kind": self.kind,
            "scope": self.scope,
            "project_id": self.project_id,
            "statement": self.statement,
            "conditions": list(self.conditions) if self.conditions is not None else None,
            "exceptions": list(self.exceptions) if self.exceptions is not None else None,
            "evidence_kind": self.evidence_kind,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
            "intent": self.intent,
            "target_revision": self.target_revision,
            "confirm": self.confirm,
            "note": self.note,
            "adoption": self.adoption,
            "attestation": self.attestation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Proposal":
        return cls(
            op=data["op"],
            base_revision=int(data["base_revision"]),
            device_id=data["device_id"],
            session_id=data.get("session_id"),
            update_id=data.get("update_id") or new_id("upd"),
            target_id=data.get("target_id"),
            kind=data.get("kind"),
            scope=data.get("scope"),
            project_id=data.get("project_id"),
            statement=data.get("statement"),
            conditions=list(data["conditions"]) if data.get("conditions") is not None else None,
            exceptions=list(data["exceptions"]) if data.get("exceptions") is not None else None,
            evidence_kind=data.get("evidence_kind"),
            source_refs=[SourceRef.from_dict(item) for item in data.get("source_refs", [])],
            intent=data.get("intent"),
            target_revision=data.get("target_revision"),
            confirm=bool(data.get("confirm", False)),
            note=data.get("note"),
            adoption=data.get("adoption"),
            attestation=data.get("attestation"),
        ).validate()


@dataclass
class ProposalResult:
    update_id: str
    status: str
    memory_id: str | None
    revision: int
    message: str
    candidate_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "update_id": self.update_id,
            "status": self.status,
            "memory_id": self.memory_id,
            "revision": self.revision,
            "message": self.message,
            "candidate_id": self.candidate_id,
        }


@dataclass
class SearchHit:
    memory_id: str
    statement: str
    kind: str
    scope: str
    project_id: str | None
    status: str
    revision: int
    score: float
    conditions: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    source_refs: list[SourceRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "statement": self.statement,
            "kind": self.kind,
            "scope": self.scope,
            "project_id": self.project_id,
            "status": self.status,
            "revision": self.revision,
            "score": self.score,
            "conditions": self.conditions,
            "exceptions": self.exceptions,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
        }


@dataclass
class Excerpt:
    id: str
    session_id: str
    role: str
    message_id: str | None
    start: int
    end: int
    text: str
    recorded_at: str = field(default_factory=utc_now)
    turn: int | None = None
    edited: bool = False

    def validate_against(self, source_text: str) -> "Excerpt":
        if self.role not in ("user", "assistant"):
            raise ContractError("excerpt role must be user or assistant")
        if not (0 <= self.start <= self.end <= len(source_text)):
            raise ContractError("excerpt range is outside the source message")
        if source_text[self.start : self.end] != self.text:
            raise ContractError("excerpt text does not match the source range")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Excerpt":
        return cls(
            id=data["id"],
            session_id=data["session_id"],
            role=data["role"],
            message_id=data.get("message_id"),
            start=int(data["start"]),
            end=int(data["end"]),
            text=data["text"],
            recorded_at=data.get("recorded_at", utc_now()),
            turn=data.get("turn"),
            edited=bool(data.get("edited", False)),
        )


@dataclass
class KeyMeta:
    id: str
    name: str
    service: str
    purpose: str
    env_name: str
    status: str = "active"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_used_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ExecRequest:
    command: str
    key_ids: list[str]
    device_id: str
    host: str
    mode: Literal["single", "resident"] = "single"
    timeout_seconds: float = 120.0
    exec_id: str = field(default_factory=lambda: new_id("exec"))
    session_id: str | None = None
    cwd: str | None = None
    deliverables: list[str] = field(default_factory=list)
    deliver_to: str | None = None
    input_paths: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def validate(self) -> "ExecRequest":
        if not self.command.strip():
            raise ContractError("command must not be empty")
        if not self.key_ids:
            raise ContractError("key_exec requires at least one selected key id")
        if self.mode not in ("single", "resident"):
            raise ContractError("mode must be single or resident")
        if self.mode == "single" and not (0 < self.timeout_seconds <= 24 * 3600):
            raise ContractError("single execution requires a positive finite timeout")
        if self.host not in ("local", "pc", "server"):
            raise ContractError("host must be local, pc, or server")
        for path in self.deliverables:
            if not path or path.startswith("/") or (len(path) > 1 and path[1] == ":") or ".." in path.replace("\\", "/").split("/"):
                raise ContractError(f"deliverable path must be relative and must not escape the workspace: {path!r}")
        return self

    def fingerprint(self) -> str:
        payload = self.to_dict()
        payload.pop("created_at", None)
        return content_hash(payload)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ExecState:
    exec_id: str
    status: str
    exit_code: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    stop_reason: str | None = None
    stdout: str = ""
    stderr: str = ""
    masked_hits: list[str] = field(default_factory=list)
    output_path: str | None = None
    workspace: str | None = None
    generation: int = 0
    artifacts: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    masking_status: str = "ok"
    masking_detections: dict[str, Any] = field(default_factory=dict)
    masking_note: str = ""
    masking_error: str | None = None
    duration_seconds: float | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecState":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class AuditEntry:
    audit_id: str
    exec_id: str
    key_ids: list[str]
    device_id: str
    started_at: str
    ended_at: str | None
    state: str
    stop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class PasswordRequest:
    id: str
    host: str
    account: str
    command: str
    adapter: str
    created_at: str
    expires_at: str
    status: str = "pending"
    session_id: str | None = None
    device_id: str | None = None
    exec_id: str | None = None
    host_key_fingerprint: str | None = None
    port: int = 22

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PasswordRequest":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class HostKeyRecord:
    host: str
    port: int
    key_type: str
    fingerprint: str
    public_key: str
    accepted_at: str
    accepted_by: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SyncFile:
    path: str
    sha256: str
    content_b64: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SyncBundle:
    revision: int
    files: list[SyncFile]
    manifest_files: dict[str, str]
    deleted_paths: list[str]
    tombstones: list[dict[str, Any]]
    manifest_sha256: str
    full: bool
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "files": [item.to_dict() for item in self.files],
            "manifest_files": self.manifest_files,
            "deleted_paths": self.deleted_paths,
            "tombstones": self.tombstones,
            "manifest_sha256": self.manifest_sha256,
            "full": self.full,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyncBundle":
        return cls(
            revision=int(data["revision"]),
            files=[SyncFile(**item) for item in data.get("files", [])],
            manifest_files=dict(data.get("manifest_files", {})),
            deleted_paths=list(data.get("deleted_paths", [])),
            tombstones=list(data.get("tombstones", [])),
            manifest_sha256=data["manifest_sha256"],
            full=bool(data.get("full", False)),
            created_at=data.get("created_at", utc_now()),
        )
