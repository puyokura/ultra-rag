"""Import important excerpts from saved dsh sessions.

Only the selected excerpts and organized notes are written to the new session
notes; full transcripts are never copied. Parsing is resumable and idempotent:
turn writes are keyed by message ids and imported candidates use deterministic
update ids. Secrets are redacted before selection, and redacted excerpts are
marked as edited rather than presented as original text.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import zstandard

from contracts.project import resolve_project_id
from contracts.util import append_jsonl, read_jsonl, sha256_hex, utc_now
from rag.redaction import redact_text
from rag.excerpts import MessageText, select_structural_excerpts, split_paragraphs, score_paragraph
from rag.service import RagService

IMPORT_MARKERS = (
    "覚えて",
    "方針",
    "禁止",
    "必須",
    "必ず",
    "絶対",
    "好み",
    "設計",
    "思想",
    "理念",
    "判断軸",
    "教訓",
    "ルール",
    "してはいけない",
    "must",
    "never",
    "always",
    "prefer",
    "decide",
)


class ImportError_(RuntimeError):
    pass


@dataclass
class ImportStats:
    files: int = 0
    sessions: int = 0
    skipped: int = 0
    turns: int = 0
    excerpts: int = 0
    candidates: int = 0
    blocked: int = 0
    errors: list[str] = field(default_factory=list)
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "sessions": self.sessions,
            "skipped": self.skipped,
            "turns": self.turns,
            "excerpts": self.excerpts,
            "candidates": self.candidates,
            "blocked": self.blocked,
            "errors": self.errors,
            "skip_reasons": self.skip_reasons,
        }


def _blocks_text(content: Iterable[dict[str, Any]] | None) -> str:
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def read_session_events(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    if raw[:4] == b"\x28\xb5\x2f\xfd" or path.suffix in (".zstd", ".zst"):
        with open(path, "rb") as handle:
            with zstandard.ZstdDecompressor().stream_reader(handle) as reader:
                data = reader.read()
    else:
        data = raw
    events: list[dict[str, Any]] = []
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def discover_session_files(dsh_home: Path) -> list[Path]:
    sessions_dir = dsh_home / "sessions"
    if not sessions_dir.is_dir():
        return []
    files: list[Path] = []
    for path in sessions_dir.rglob("session*.jsonl*"):
        if path.is_file():
            files.append(path)
    return sorted(files)


@dataclass
class ParsedTurn:
    turn: int
    user_id: str | None
    user_text: str
    assistant_id: str | None
    assistant_text: str


def parse_session(events: list[dict[str, Any]]) -> tuple[dict[str, Any], list[ParsedTurn], str | None]:
    header: dict[str, Any] = {}
    turns: list[ParsedTurn] = []
    version: str | None = None
    current_turn: int | None = None
    user_id: str | None = None
    user_text = ""
    assistant_id: str | None = None
    assistant_text = ""
    sequence = 0

    def flush() -> None:
        nonlocal user_id, user_text, assistant_id, assistant_text
        if user_text or assistant_text:
            turns.append(
                ParsedTurn(
                    turn=current_turn if current_turn is not None else len(turns) + 1,
                    user_id=user_id,
                    user_text=user_text,
                    assistant_id=assistant_id,
                    assistant_text=assistant_text,
                )
            )
        user_id, user_text, assistant_id, assistant_text = None, "", None, ""

    for event in events:
        kind = event.get("type")
        if kind == "session":
            header = event
            version = str(event.get("version"))
            continue
        if kind == "turn/start":
            current_turn = int(event.get("data", {}).get("turn", len(turns) + 1))
            continue
        if kind == "user/message":
            source = event.get("data", {}).get("source", {}) or {}
            if source.get("kind") != "user":
                continue
            text = _blocks_text(event.get("data", {}).get("content"))
            if not text:
                continue
            if user_text:
                user_text += "\n\n" + text
            else:
                user_id = event.get("data", {}).get("id")
                user_text = text
            continue
        if kind == "assistant/message":
            message = event.get("data", {}).get("message", {}) or {}
            text = _blocks_text(message.get("content"))
            if text:
                assistant_id = message.get("id")
                assistant_text = text
            continue
        if kind == "turn/end":
            flush()
            current_turn = None
            continue
        sequence += 1
    flush()
    return header, turns, version


def _deterministic_update_id(*parts: str) -> str:
    return "upd_" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _classify_kind(paragraph: str) -> str:
    lowered = paragraph.lower()
    if any(marker in lowered for marker in ("禁止", "してはいけない", "never", "must not", "don't", "避け")):
        return "prohibition"
    if any(marker in lowered for marker in ("必須", "必ず", "絶対", "must", "always", "required")):
        return "requirement"
    return "preference"


def _paragraph_candidates(text: str, session_id: str, message_id: str | None, turn: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for start, end, paragraph in split_paragraphs(text):
        if len(paragraph) < 8:
            continue
        if not any(marker in paragraph.lower() for marker in IMPORT_MARKERS):
            continue
        candidates.append(
            {
                "statement": paragraph,
                "kind": _classify_kind(paragraph),
                "start": start,
                "end": end,
                "source_refs": [
                    {
                        "session_id": session_id,
                        "message_id": message_id,
                        "start": start,
                        "end": end,
                        "note": "imported candidate from a saved session",
                    }
                ],
            }
        )
    candidates.sort(key=lambda item: (-score_paragraph(item["statement"]), item["start"]))
    return candidates[:3]


def import_session(
    service: RagService,
    path: Path,
    dsh_home: Path,
    dry_run: bool = False,
    max_turns: int | None = None,
    project_override: str | None = None,
) -> dict[str, Any]:
    stats = ImportStats(files=1)
    try:
        events = read_session_events(path)
    except Exception as error:
        stats.errors.append(f"{path.name}: cannot read session ({type(error).__name__})")
        return stats.to_dict()
    header, turns, version = parse_session(events)
    if version not in (None, "4"):
        stats.skip(f"unsupported session format version {version}")
        return stats.to_dict()
    if int(header.get("delegationDepth", 0) or 0) > 0 or header.get("origin") == "subagent":
        stats.skip("subagent session")
        return stats.to_dict()
    if not turns:
        stats.skip("no user/assistant turns")
        return stats.to_dict()
    session_id = str(header.get("id") or path.parent.name)
    project_id = project_override
    if not project_id:
        cwd = header.get("cwd")
        project_id = resolve_project_id(cwd) if cwd else None
    stats.sessions += 1
    if dry_run:
        stats.turns = len(turns)
        stats.excerpts = sum(1 for turn in turns for _ in select_structural_excerpts(
            [MessageText("user", turn.user_id, redact_text(turn.user_text)[0]),
             MessageText("assistant", turn.assistant_id, redact_text(turn.assistant_text)[0])],
            session_id, turn.turn))
        return stats.to_dict()
    for turn in turns:
        if max_turns is not None and stats.turns >= max_turns:
            break
        user_text, user_edited = redact_text(turn.user_text)
        assistant_text, assistant_edited = redact_text(turn.assistant_text)
        messages = [
            MessageText("user", turn.user_id, user_text),
            MessageText("assistant", turn.assistant_id, assistant_text),
        ]
        selected = select_structural_excerpts(messages, session_id, turn.turn)
        provided = [
            {
                "role": excerpt.role,
                "message_id": excerpt.message_id,
                "start": excerpt.start,
                "end": excerpt.end,
                "text": excerpt.text,
                "edited": user_edited if excerpt.role == "user" else assistant_edited,
            }
            for excerpt in selected
        ]
        if provided:
            result = service.record_turn(
                session_id=session_id,
                project_id=project_id,
                turn=turn.turn,
                user_text=user_text,
                assistant_text=assistant_text,
                user_message_id=turn.user_id,
                assistant_message_id=turn.assistant_id,
                excerpts=provided,
            )
            if result.get("saved"):
                stats.turns += 1
                stats.excerpts += len(provided)
        for candidate in _paragraph_candidates(user_text, session_id, turn.user_id, turn.turn):
            update_id = _deterministic_update_id(
                session_id,
                turn.user_id or "-",
                str(candidate["start"]),
                str(candidate["end"]),
                "candidate",
            )
            proposal = {
                "op": "create",
                "update_id": update_id,
                "base_revision": service.revision(),
                "device_id": service.store.device_id,
                "session_id": session_id if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", session_id) else None,
                "kind": candidate["kind"],
                "scope": "personal",
                "statement": candidate["statement"],
                "evidence_kind": "imported_statement",
                "source_refs": candidate["source_refs"],
                "intent": "imported from a saved session; requires confirmation",
            }
            result = service.propose(proposal)
            if result.status == "pending":
                stats.candidates += 1
            elif result.status == "rejected" and "deleted" in result.message:
                stats.blocked += 1
    return stats.to_dict()


def import_all(
    service: RagService,
    dsh_home: Path,
    limit: int | None = None,
    dry_run: bool = False,
    project_override: str | None = None,
) -> dict[str, Any]:
    ledger_path = service.layout.rag / "import" / "ledger.jsonl"
    ledger = read_jsonl(ledger_path)
    completed: dict[str, str] = {
        str(row.get("path")): str(row.get("file_sha256", ""))
        for row in ledger
        if row.get("status") == "complete"
    }
    files = discover_session_files(dsh_home)
    if limit is not None:
        files = files[:limit]
    total = ImportStats()
    total.files = len(files)
    for path in files:
        relative = str(path)
        digest = sha256_hex(path.read_bytes())
        if completed.get(relative) == digest and not dry_run:
            total.skip("already imported")
            continue
        result = import_session(service, path, dsh_home, dry_run=dry_run, project_override=project_override)
        total.sessions += result.get("sessions", 0)
        total.skipped += result.get("skipped", 0)
        total.turns += result.get("turns", 0)
        total.excerpts += result.get("excerpts", 0)
        total.candidates += result.get("candidates", 0)
        total.blocked += result.get("blocked", 0)
        total.errors.extend(result.get("errors", []))
        for reason, count in result.get("skip_reasons", {}).items():
            total.skip_reasons[reason] = total.skip_reasons.get(reason, 0) + count
        if not dry_run and not result.get("errors"):
            append_jsonl(
                ledger_path,
                {
                    "path": relative,
                    "session_id": path.parent.name,
                    "status": "complete",
                    "imported_at": utc_now(),
                    "stats": result,
                    "file_sha256": digest,
                },
            )
    return total.to_dict()
