"""Excerpt selection and validation.

Two paths exist:

* an AI (or the user) supplies excerpts. Every excerpt is validated as an exact
  contiguous substring of the source message; a range that does not match is
  rejected instead of being stored as if it were original text.
* no excerpts are supplied. A deterministic structural selector keeps whole
  paragraphs that mention decisions, policies, prohibitions, or preferences.
  It never cuts a paragraph mid-sentence and labels its note as extractive.

The structural path is not presented as an AI summary. Its note is built from
complete first sentences of the selected paragraphs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from contracts.models import ContractError, Excerpt
from contracts.util import new_id, utc_now

MARKERS = (
    "覚えて",
    "忘れて",
    "方針",
    "決定",
    "決めた",
    "禁止",
    "必須",
    "必ず",
    "絶対",
    "好み",
    "好き",
    "嫌い",
    "設計",
    "思想",
    "理念",
    "判断軸",
    "教訓",
    "ルール",
    "should",
    "must",
    "never",
    "always",
    "prefer",
    "decide",
    "decided",
    "requirement",
    "policy",
    "禁止事項",
    "してはいけない",
    "しないこと",
    "避け",
)


@dataclass
class MessageText:
    role: str
    message_id: str | None
    text: str


def split_paragraphs(text: str) -> list[tuple[int, int, str]]:
    """Return (start, end, paragraph) with exact offsets and surrounding blank lines excluded."""
    paragraphs: list[tuple[int, int, str]] = []
    for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", text):
        start, end = match.span()
        raw = text[start:end]
        left = len(raw) - len(raw.lstrip())
        right = len(raw) - len(raw.rstrip())
        para = raw.strip()
        if para:
            paragraphs.append((start + left, end - right, para))
    return paragraphs


def score_paragraph(paragraph: str) -> int:
    lowered = paragraph.lower()
    return sum(1 for marker in MARKERS if marker in lowered)


def _first_sentence(paragraph: str) -> str:
    match = re.search(r"[^。．.!?\n]+[。．.!?]?", paragraph)
    return (match.group(0) if match else paragraph).strip()


def select_structural_excerpts(
    messages: list[MessageText],
    session_id: str,
    turn: int | None,
    max_excerpts: int = 4,
) -> list[Excerpt]:
    candidates: list[tuple[int, int, str, str, str | None]] = []
    for message in messages:
        for start, end, paragraph in split_paragraphs(message.text):
            score = score_paragraph(paragraph)
            if score > 0 and len(paragraph) >= 8:
                candidates.append((score, start, paragraph, message.role, message.message_id))
    if not candidates:
        return []
    candidates.sort(key=lambda item: (-item[0], item[1]))
    chosen = candidates[:max_excerpts]
    chosen.sort(key=lambda item: item[1])
    excerpts: list[Excerpt] = []
    for _, start, paragraph, role, message_id in chosen:
        excerpts.append(
            Excerpt(
                id=new_id("exc"),
                session_id=session_id,
                role=role,
                message_id=message_id,
                start=start,
                end=start + len(paragraph),
                text=paragraph,
                recorded_at=utc_now(),
                turn=turn,
            )
        )
    return excerpts


def build_provided_excerpts(
    provided: list[dict],
    messages: list[MessageText],
    session_id: str,
    turn: int | None,
    max_chars: int = 4000,
) -> list[Excerpt]:
    by_role = {message.role: message for message in messages}
    excerpts: list[Excerpt] = []
    for item in provided:
        role = item.get("role")
        text = item.get("text")
        message_id = item.get("message_id")
        if role not in by_role:
            raise ContractError(f"excerpt role {role!r} has no source message in this turn")
        if not isinstance(text, str) or not text:
            raise ContractError("excerpt text must not be empty")
        if len(text) > max_chars:
            raise ContractError(
                f"excerpt exceeds {max_chars} characters; split it into multiple excerpts "
                "instead of truncating the middle"
            )
        source = by_role[role]
        if message_id and source.message_id and message_id != source.message_id:
            raise ContractError("excerpt message_id does not match the turn's source message")
        start = item.get("start")
        end = item.get("end")
        if start is not None and end is not None:
            if not (0 <= start <= end <= len(source.text)) or source.text[start:end] != text:
                raise ContractError("excerpt range does not match the source message text")
        else:
            start = source.text.find(text)
            if start < 0:
                raise ContractError("excerpt text is not an exact substring of the source message")
            end = start + len(text)
        excerpt = Excerpt(
            id=item.get("id") or new_id("exc"),
            session_id=session_id,
            role=role,
            message_id=source.message_id,
            start=int(start),
            end=int(end),
            text=text,
            recorded_at=utc_now(),
            turn=turn,
            edited=bool(item.get("edited", False)),
        ).validate_against(source.text)
        excerpts.append(excerpt)
    seen: set[tuple] = set()
    unique: list[Excerpt] = []
    for excerpt in excerpts:
        key = (excerpt.role, excerpt.start, excerpt.end, excerpt.text)
        if key in seen:
            continue
        seen.add(key)
        unique.append(excerpt)
    return unique


def extractive_note(excerpts: list[Excerpt]) -> str:
    if not excerpts:
        return ""
    parts = [_first_sentence(excerpt.text) for excerpt in excerpts]
    return " / ".join(parts)
