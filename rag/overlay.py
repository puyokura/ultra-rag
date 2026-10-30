"""Generate a dsh overlay that wires the memory bridge and MCP server.

The overlay never modifies the user's profile. Paths are resolved from the
current repository and the selected ultra-RAG home.
"""

from __future__ import annotations

import sys
from pathlib import Path

from contracts.config import Layout, load_config


def render_overlay(layout: Layout, repo_root: Path | None = None) -> str:
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    home = layout.home.resolve()
    python = sys.executable
    sync_before_turn = bool(load_config(layout, create=False).get("sync_remote"))

    def posix(path: Path | str) -> str:
        return str(path).replace("\\", "/")

    return (
        "- insert:\n"
        "    - id: ultra-rag-memory\n"
        f"      name: {posix(root / 'rag' / 'dsh-plugin' / 'index.mjs')}\n"
        "      config:\n"
        f"        home: {posix(home)}\n"
        f"        python: {posix(python)}\n"
        f"        syncBeforeTurn: {'true' if sync_before_turn else 'false'}\n"
        "    - id: mcp-ultra-rag\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: ultra-rag\n"
        "        transport: stdio\n"
        f"        command: {posix(python)}\n"
        "        args:\n"
        f"          - {posix(root / 'scripts' / 'run_mcp.py')}\n"
        "          - --home\n"
        f"          - {posix(home)}\n"
    )
