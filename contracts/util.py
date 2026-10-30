"""Shared, dependency-light helpers for every ultra-RAG feature project.

Nothing in this module reads secrets. Runtime paths are resolved from an
explicit home directory or from ``ULTRA_RAG_HOME`` only; no repository path is
treated as production data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from filelock import FileLock

_UMASK_LOCK = threading.Lock()
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string with a trailing ``Z``."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str) -> str:
    """Stable-shape identifier; the prefix keeps kinds distinguishable."""
    return f"{prefix}_{uuid.uuid4().hex}"


def check_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.match(value):
        raise ValueError(f"{field_name} contains unsafe characters: {value!r}")
    return value


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        # chmod on Windows only toggles the read-only bit. ACL tightening is
        # attempted once; a failure is not reported as proof of protection.
        try:
            import subprocess

            user = os.environ.get("USERNAME")
            if not user:
                return
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                check=False,
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _restrict(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode=mode)


def append_jsonl(path: Path, value: Any) -> None:
    ensure_dir(path.parent)
    line = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    _restrict(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


@contextmanager
def write_lock(lock_path: Path, timeout: float = 30.0) -> Iterator[None]:
    ensure_dir(lock_path.parent)
    with FileLock(str(lock_path), timeout=timeout):
        yield


def secure_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def mask_known_values(text: str, values: list[str]) -> tuple[str, dict[str, Any]]:
    """Mask exact known values and encoded variants, with a non-secret report."""
    markers: list[str] = []
    types: dict[str, int] = {}
    if not text:
        return text, {"total": 0, "types": {}, "markers": []}
    candidates: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if not value or value in seen:
            continue
        seen.add(value)
        marker = f"[REDACTED:{index + 1}]"
        candidates.append((value, marker, "exact"))
        variants = {
            "base64": base64.b64encode(value.encode("utf-8")).decode("ascii"),
            "json": json.dumps(value)[1:-1],
        }
        for kind, variant in variants.items():
            if len(variant) >= 6 and variant != value:
                candidates.append((variant, marker, kind))
    for needle, marker, kind in sorted(candidates, key=lambda pair: len(pair[0]), reverse=True):
        count = text.count(needle)
        if not count:
            continue
        text = text.replace(needle, marker)
        types[kind] = types.get(kind, 0) + count
        if marker not in markers:
            markers.append(marker)
    return text, {"total": sum(types.values()), "types": types, "markers": markers}


def redact_known_values(text: str, values: list[str]) -> tuple[str, list[str]]:
    """Mask exact known values and their common encoded forms.

    This is a mistake-leak mitigation, not a guarantee: transformed, encrypted,
    split, or externally streamed values cannot all be recognized. Callers must
    report that limit instead of claiming complete leakage prevention.
    """
    masked, report = mask_known_values(text, values)
    return masked, report["markers"]


def tail_lines(text: str, limit: int) -> str:
    """Return the last ``limit`` lines without inserting an ellipsis."""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[-limit:]) + "\n"


def safe_relative(path: str) -> str:
    """Reject absolute, parent-escaping, or drive-qualified relative paths."""
    text = path.replace("\\", "/")
    if not text or text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise ValueError(f"unsafe relative path: {path!r}")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe relative path: {path!r}")
    return "/".join(parts)
