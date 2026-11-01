"""Runtime configuration and directory resolution.

Deployment paths are deliberately *not* fixed in source. Every process resolves
one home directory:

1. the explicit ``home=`` argument (tests, CLI option), then
2. ``ULTRA_RAG_HOME``, then
3. a per-OS application-data directory.

Nothing here creates production data implicitly inside the repository.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contracts.util import (
    atomic_write_text,
    ensure_dir,
    new_id,
    read_json,
    secure_token,
    utc_now,
    write_json,
)

APP_DIR_NAME = "ultra-rag"


class ConfigError(RuntimeError):
    pass


def default_home() -> Path:
    override = os.environ.get("ULTRA_RAG_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return (Path(base) / APP_DIR_NAME).resolve()


@dataclass(frozen=True)
class Layout:
    home: Path
    rag: Path
    secret: Path
    password: Path
    run: Path
    control: Path
    config_file: Path

    @classmethod
    def at(cls, home: Path | str | None = None) -> "Layout":
        root = Path(home).expanduser().resolve() if home not in (None, "") else default_home()
        if not str(root).strip():
            root = default_home()
        return cls(
            home=root,
            rag=root / "rag",
            secret=root / "secret",
            password=root / "password",
            run=root / "run",
            control=root / "control",
            config_file=root / "config.json",
        )

    def ensure(self) -> "Layout":
        for path in (self.home, self.rag, self.secret, self.password, self.run, self.control):
            ensure_dir(path)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        return self


DEFAULT_CONFIG: dict[str, Any] = {
    "role": "server",
    "host": "pc" if os.name == "nt" else "server",
    "device_id": None,
    "project_id": None,
    "sync_remote": None,
    "rag": {
        "max_excerpt_chars": 4000,
        "auto_candidate_scope": "personal",
        "embedding_enabled": False,
        "embedding_model": "cl-nagoya/ruri-v3-310m",
        "embedding_device": "cpu",
        "embedding_batch_size": 16,
    },
    "secret": {
        "lease_seconds": 300,
        "max_exec_seconds": 3600,
        "default_exec_seconds": 120,
        "max_concurrent": 4,
    },
    "password": {
        "request_ttl_seconds": 300,
        "ssh_port": 22,
    },
}


def load_config(layout: Layout, create: bool = False) -> dict[str, Any]:
    """Load configuration, optionally initialising a brand-new home.

    Initialisation is explicit: callers that only read must pass ``create=False``.
    """
    if create:
        layout.ensure()
    if not layout.config_file.exists():
        if not create:
            raise ConfigError(f"ultra-RAG is not initialised at {layout.home}")
        config = dict(DEFAULT_CONFIG)
        config["device_id"] = new_id("dev")
        config["created_at"] = utc_now()
        write_json(layout.config_file, config)
        ensure_dir(layout.control)
        token_file = layout.control / "api-token"
        if not token_file.exists():
            atomic_write_text(token_file, secure_token() + "\n")
        return config
    config = read_json(layout.config_file, default=None)
    if not isinstance(config, dict):
        raise ConfigError(f"invalid config file: {layout.config_file}")
    merged = dict(DEFAULT_CONFIG)
    for key, value in config.items():
        if key in ("rag", "secret", "password") and isinstance(value, dict):
            merged[key] = {**DEFAULT_CONFIG[key], **value}
        else:
            merged[key] = value
    if not merged.get("device_id"):
        raise ConfigError("config is missing device_id")
    return merged


def save_config(layout: Layout, config: dict[str, Any]) -> None:
    write_json(layout.config_file, config)


def api_token(layout: Layout, create: bool = False) -> str:
    token_file = layout.control / "api-token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    if not create:
        raise ConfigError(f"API token file is missing: {token_file}")
    ensure_dir(token_file.parent)
    token = secure_token()
    atomic_write_text(token_file, token + "\n")
    return token


def ensure_initialised(layout: Layout) -> dict[str, Any]:
    return load_config(layout, create=True)
