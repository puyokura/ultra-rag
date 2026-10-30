"""Secret redaction used before any excerpt, note, candidate, or index write.

Pattern-based redaction cannot prove that a value is absent: it catches common
token shapes and credential assignments, and the caller must still treat the
session text as sensitive. Redacted excerpts are marked as edited so they are not
presented as original text.
"""

from __future__ import annotations

import re

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\b\s*[:=]\s*[^\s,;]{6,}"),
]

REDACTION = "[REDACTED]"


def redact_text(text: str) -> tuple[str, bool]:
    edited = False
    result = text
    for pattern in SECRET_PATTERNS:
        if pattern.search(result):
            edited = True
            result = pattern.sub(REDACTION, result)
    return result, edited


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)
