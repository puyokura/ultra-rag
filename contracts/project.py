"""Stable project identity shared by both hosts.

A device-local absolute path is never used as the identifier. Preference order:

1. an explicit id (CLI flag or stored mapping),
2. ``.ultra-rag-project`` at the repository root,
3. a normalized git ``remote.origin.url`` hash,
4. the repository directory name hash (documented as *not* portable when two
   unrelated projects share a name and no remote or marker exists).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

PROJECT_ID_RE = re.compile(r"^proj_[0-9a-f]{16}$")
MARKER = ".ultra-rag-project"


class ProjectError(RuntimeError):
    pass


def _hash(text: str) -> str:
    return "proj_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_remote(url: str) -> str:
    text = url.strip()
    text = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
    text = text.replace("\\", "/")
    text = re.sub(r"^[^@/]+@", "", text)  # user@
    text = re.sub(r"/+", "/", text)
    text = re.sub(r"\.git$", "", text, flags=re.IGNORECASE)
    return text.lower().rstrip("/")


def validate_project_id(value: str) -> str:
    if not PROJECT_ID_RE.match(value):
        raise ProjectError(f"invalid project id: {value!r} (expected proj_<16 hex>)")
    return value


def _read_marker(root: Path) -> str | None:
    marker = root / MARKER
    if not marker.exists():
        return None
    text = marker.read_text(encoding="utf-8").strip()
    if not text:
        return None
    if text.startswith("{"):
        data = json.loads(text)
        text = str(data.get("project_id", ""))
    return validate_project_id(text)


def _git_toplevel(path: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def _git_remote(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def resolve_project_id(path: Path | str, explicit: str | None = None) -> str:
    if explicit:
        return validate_project_id(explicit)
    start = Path(path).expanduser().resolve()
    if start.is_file():
        start = start.parent
    current = start
    while True:
        found = _read_marker(current)
        if found:
            return found
        if (current / ".git").exists():
            break
        if current.parent == current:
            break
        current = current.parent
    top = _git_toplevel(start) or start
    remote = _git_remote(top)
    if remote:
        return _hash("remote:" + normalize_remote(remote))
    name = top.name or "root"
    return _hash("name:" + name)


def write_marker(root: Path | str, project_id: str | None = None) -> str:
    root_path = Path(root).resolve()
    identifier = validate_project_id(project_id) if project_id else resolve_project_id(root_path)
    (root_path / MARKER).write_text(json.dumps({"project_id": identifier}, indent=2) + "\n", encoding="utf-8")
    return identifier
