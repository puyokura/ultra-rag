"""Internal hook entry points used by the dsh Cordis plugin.

The plugin sends the last user message for context injection and the completed
turn for excerpt selection. Full turn text passes through process memory only;
only validated excerpts and the organized note are persisted, after redaction.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from contracts.config import ConfigError, Layout, load_config
from contracts.project import resolve_project_id
from contracts.util import check_identifier
from rag.excerpts import MessageText, select_structural_excerpts, split_paragraphs
from rag.redaction import redact_text

MEMORY_REQUEST_RE = re.compile(
    r"(?:覚えて|記憶して)(?:おいて|おき|ください|下さい|ほしい|くれ|ね|もらえ|$|[。．.!！、,])"
)


def _is_memory_request(paragraph: str) -> bool:
    """Accept an explicit request, not a question that merely contains 覚えて."""
    if "?" in paragraph or "？" in paragraph:
        return False
    return bool(MEMORY_REQUEST_RE.search(paragraph))
from rag.service import RagService
from rag.store import StoreError


def _registration_kind(paragraph: str) -> str:
    lowered = paragraph.lower()
    if any(marker in lowered for marker in ("禁止", "してはいけない", "never", "must not", "避け")):
        return "prohibition"
    if any(marker in lowered for marker in ("必須", "必ず", "絶対", "must", "always", "required")):
        return "requirement"
    return "preference"


def _explicit_registrations(
    user_text: str, user_message_id: str | None, session_id: str, project_id: str | None
) -> list[dict[str, Any]]:
    """User-explicit "覚えて" paragraphs are adopted without waiting for the model.

    Only the explicit registration marker triggers this path. Inferred policy
    statements still require the model's memory_remember call or confirmation.
    A paragraph explicitly limited to this project is kept in project scope; a
    one-off "今回は" statement is not promoted into a lasting policy.
    """
    registrations: list[dict[str, Any]] = []
    for start, end, paragraph in split_paragraphs(user_text):
        if not _is_memory_request(paragraph):
            continue
        if len(paragraph) < 8:
            continue
        if "今回は" in paragraph or "今回だけ" in paragraph:
            continue
        project_specific = any(marker in paragraph for marker in ("この案件", "このプロジェクト", "このリポジトリ"))
        registrations.append(
            {
                "statement": paragraph,
                "kind": _registration_kind(paragraph),
                "scope": "project" if project_specific and project_id else "personal",
                "project_id": project_id if project_specific and project_id else None,
                "start": start,
                "end": end,
                "source_refs": [
                    {
                        "session_id": session_id,
                        "message_id": user_message_id,
                        "start": start,
                        "end": end,
                        "note": "explicit registration in the conversation",
                    }
                ],
            }
        )
    return registrations[:3]


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("hook payload must be a JSON object")
    return value


def _layout(home: str | None) -> Layout:
    return Layout.at(home)


def pre_step(home: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    layout = _layout(home)
    config = load_config(layout, create=False)
    cwd = payload.get("cwd") or str(Path.cwd())
    project_id = payload.get("project_id") or resolve_project_id(cwd)
    session_id = payload.get("session_id")
    if session_id:
        check_identifier(str(session_id), "session_id")
    service = RagService(layout)
    if not service.store.exists():
        return {"ok": False, "error": "rag_not_initialised"}
    result = service.snapshot(
        project_id=project_id,
        query=str(payload.get("query") or ""),
        session_id=str(session_id) if session_id else None,
        related_limit=int(payload.get("related_limit", 8)),
    )
    related = result.get("related", [])
    pending = result.get("pending", [])
    summary = (
        f"ultra-RAG revision {result['revision']} · related {len(related)} · "
        f"pending {len(pending)} · role {config.get('role')}"
    )
    return {
        "ok": True,
        "revision": result["revision"],
        "project_id": project_id,
        "summary": summary,
        "context_text": result["context_text"],
        "related_count": len(related),
        "pending_count": len(pending),
        "notifications": result.get("notifications", []),
    }


def turn_end(home: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    layout = _layout(home)
    load_config(layout, create=False)
    service = RagService(layout)
    if not service.store.exists():
        return {"ok": False, "error": "rag_not_initialised"}
    session_id = str(payload["session_id"])
    check_identifier(session_id, "session_id")
    cwd = payload.get("cwd") or str(Path.cwd())
    project_id = payload.get("project_id") or resolve_project_id(cwd)
    user_text, user_edited = redact_text(str(payload.get("user_text") or ""))
    assistant_text, assistant_edited = redact_text(str(payload.get("assistant_text") or ""))
    messages = [
        MessageText("user", payload.get("user_message_id"), user_text),
        MessageText("assistant", payload.get("assistant_message_id"), assistant_text),
    ]
    selected = select_structural_excerpts(messages, session_id, int(payload.get("turn", 1)))
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
    if not provided:
        return {"ok": True, "saved": False, "reason": "no important content selected", "revision": service.revision()}
    result = service.record_turn(
        session_id=session_id,
        project_id=project_id,
        turn=int(payload.get("turn", 1)),
        user_text=user_text,
        assistant_text=assistant_text,
        user_message_id=payload.get("user_message_id"),
        assistant_message_id=payload.get("assistant_message_id"),
        excerpts=provided,
    )
    registrations = []
    for candidate in _explicit_registrations(user_text, payload.get("user_message_id"), session_id, project_id):
        adopted = service.remember(
            statement=candidate["statement"],
            kind=candidate["kind"],
            scope=candidate["scope"],
            project_id=candidate["project_id"],
            evidence_kind="explicit_registration",
            session_id=session_id,
            source_refs=candidate["source_refs"],
            intent="explicit registration marker in the user message",
        )
        registrations.append(adopted.to_dict())
    return {"ok": True, **result, "registrations": registrations}


def status(home: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    return pre_step(home, {**payload, "query": ""})


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="ultra-rag-hook")
    parser.add_argument("action", choices=["pre-step", "turn-end", "status"])
    parser.add_argument("--home", default=None)
    args = parser.parse_args(argv)
    try:
        payload = _read_payload()
        if args.action == "pre-step":
            result = pre_step(args.home, payload)
        elif args.action == "turn-end":
            result = turn_end(args.home, payload)
        else:
            result = status(args.home, payload)
    except (ConfigError, StoreError, KeyError, ValueError) as error:
        result = {"ok": False, "error": type(error).__name__}
    except Exception as error:  # no message text or payload in the response
        result = {"ok": False, "error": type(error).__name__}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
