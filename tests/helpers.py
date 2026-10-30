from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import zstandard
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from typing import Any

from contracts.config import Layout, api_token, ensure_initialised, load_config, save_config
from rag.service import RagService
from secret_core.devices import DeviceRegistry
from secret_core.lease import LeaseManager
from secret_core.vault import Vault

ROOT = Path(__file__).resolve().parents[1]


def new_layout(base: Path, role: str = "server", host: str | None = None) -> Layout:
    layout = Layout.at(base / "home")
    config = ensure_initialised(layout)
    config["role"] = role
    if host:
        config["host"] = host
    save_config(layout, config)
    api_token(layout, create=True)
    if role == "server":
        RagService(layout).initialise()
    return layout


def new_secret_layout(base: Path, role: str = "server") -> Layout:
    layout = new_layout(base, role)
    config = load_config(layout, create=False)
    Vault(layout).initialise()
    DeviceRegistry(layout).ensure_local(config["device_id"], "test-device")
    LeaseManager(layout, float(config["secret"]["lease_seconds"])).refresh("test")
    return layout


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    from ultra_rag_cli.main import main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as error:
            if isinstance(error.code, str):
                err.write(error.code + "\n")
                code = 2
            else:
                code = int(error.code or 0)
    return code, out.getvalue(), err.getvalue()


def build_session_file(
    base: Path,
    session_id: str,
    cwd: str,
    turns: list[dict[str, Any]],
    delegation_depth: int = 0,
) -> Path:
    directory = base / "sessions" / "encoded" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = [
        {
            "type": "session",
            "version": 4,
            "id": session_id,
            "createdAt": 1700000000000,
            "cwd": cwd,
            "isSeeded": False,
            "delegationDepth": delegation_depth,
            "agentPreset": "test",
        }
    ]
    sequence = 0
    for index, turn in enumerate(turns, start=1):
        events.append({"type": "turn/start", "seq": sequence, "time": 1700000000000 + index, "data": {"turn": index}})
        sequence += 1
        events.append(
            {
                "type": "user/message",
                "seq": sequence,
                "time": 1700000000000 + index,
                "data": {
                    "id": turn.get("user_id", f"u{index}"),
                    "role": "user",
                    "source": {"kind": "user"},
                    "content": [{"type": "text", "text": turn["user"]}],
                },
            }
        )
        sequence += 1
        events.append(
            {
                "type": "assistant/message",
                "seq": sequence,
                "time": 1700000000000 + index,
                "data": {
                    "turn": index,
                    "step": 1,
                    "message": {
                        "id": turn.get("assistant_id", f"a{index}"),
                        "role": "assistant",
                        "source": {"kind": "model", "provider": "test", "model": "test"},
                        "content": [{"type": "text", "text": turn["assistant"]}],
                    },
                },
            }
        )
        sequence += 1
        events.append(
            {
                "type": "turn/end",
                "seq": sequence,
                "time": 1700000000000 + index,
                "data": {"turn": index, "reason": {"kind": "completed"}},
            }
        )
        sequence += 1
    path = directory / "session.v4.jsonl.zstd"
    raw = "\n".join(json.dumps(event, ensure_ascii=False) for event in events).encode("utf-8") + b"\n"
    with open(path, "wb") as handle:
        with zstandard.ZstdCompressor().stream_writer(handle) as writer:
            writer.write(raw)
    return path
