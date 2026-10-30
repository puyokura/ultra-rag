"""Small, dependency-free .env parser for owner-side key import.

The parser intentionally accepts only assignments, not command substitution or
variable expansion. Values are returned to the caller and never logged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


class DotenvError(ValueError):
    pass


@dataclass(frozen=True)
class DotenvEntry:
    key: str
    value: str
    line: int


def _unquote(value: str, line: int) -> str:
    if not value or value[0] not in ("'", '"'):
        return value
    quote = value[0]
    escaped = False
    end = -1
    for index in range(1, len(value)):
        char = value[index]
        if quote == '"' and char == "\\" and not escaped:
            escaped = True
            continue
        if char == quote and not escaped:
            end = index
            break
        escaped = False
    if end < 0:
        raise DotenvError(f"line {line}: unterminated quoted value")
    trailer = value[end + 1 :].strip()
    if trailer and not trailer.startswith("#"):
        raise DotenvError(f"line {line}: unexpected text after quoted value")
    inner = value[1:end]
    if quote == "'":
        return inner
    out: list[str] = []
    index = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
    while index < len(inner):
        char = inner[index]
        if char == "\\" and index + 1 < len(inner) and inner[index + 1] in escapes:
            out.append(escapes[inner[index + 1]])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_dotenv(text: str) -> list[DotenvEntry]:
    entries: list[DotenvEntry] = []
    seen: dict[str, int] = {}
    for line_number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise DotenvError(f"line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            raise DotenvError(f"line {line_number}: key must match [A-Z_][A-Z0-9_]*")
        if key in seen:
            raise DotenvError(f"line {line_number}: duplicate key {key} (first on line {seen[key]})")
        value = value.strip()
        if value[:1] in ("'", '"'):
            value = _unquote(value, line_number)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if not value:
            raise DotenvError(f"line {line_number}: empty value for {key}")
        seen[key] = line_number
        entries.append(DotenvEntry(key=key, value=value, line=line_number))
    if not entries:
        raise DotenvError("no key assignments found")
    return entries
