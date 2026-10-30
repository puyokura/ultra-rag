"""Repository-local MCP entry point.

Ensures the monorepo package directories are importable even when this script is
started directly by a dsh MCP client, then delegates to the stdio server.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (
    ROOT,
    ROOT / "secret-core",
    ROOT / "key-use",
    ROOT / "password-input",
    ROOT / "cli",
):
    text = str(candidate)
    if text not in sys.path:
        sys.path.insert(0, text)

from rag.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
