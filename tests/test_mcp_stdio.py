from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import new_secret_layout
from secret_core.vault import Vault

ROOT = Path(__file__).resolve().parents[1]


class MCPClient:
    def __init__(self, home: Path):
        env = os.environ.copy()
        env["ULTRA_RAG_HOME"] = str(home)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT), str(ROOT / "secret-core"), str(ROOT / "key-use"), str(ROOT / "password-input"), str(ROOT / "cli")]
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "rag.mcp_server", "--home", str(home)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.next_id = 0
        self.transcript = []

    def send(self, method: str, params: dict | None = None, notification: bool = False) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            self.next_id += 1
            message["id"] = self.next_id
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def read(self, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(f"MCP server exited: {self.process.stderr.read()}")
            self.transcript.append(line)
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        raise TimeoutError("no MCP response")

    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.send("tools/call", {"name": name, "arguments": arguments or {}})
        response = self.read()
        if "error" in response:
            raise AssertionError(response["error"])
        return json.loads(response["result"]["content"][0]["text"])

    def stop(self) -> int:
        try:
            self.process.stdin.close()
        except Exception:
            pass
        try:
            code = self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except Exception:
                pass
        return code


class MCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.layout = new_secret_layout(self.base)
        self.vault = Vault(self.layout)
        self.key = self.vault.register("mcp-test", "demo", "test", "super-secret-value", env_name="MCP_DEMO_KEY")
        self.client = MCPClient(self.layout.home)

    def tearDown(self) -> None:
        try:
            self.client.stop()
        except Exception:
            pass
        self.temp.cleanup()

    def initialise(self) -> None:
        self.client.send(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
        )
        init = self.client.read()
        self.assertIn("result", init)
        self.client.send("notifications/initialized", {}, notification=True)
        self.client.send("tools/list", {})
        tools = self.client.read()
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("memory_snapshot", names)
        self.assertIn("key_list", names)
        self.assertIn("key_exec", names)
        self.assertIn("exec_status", names)

    def test_memory_tools_over_real_stdio(self) -> None:
        self.initialise()
        proposed = self.client.call(
            "memory_remember",
            {"statement": "実測を最優先する", "kind": "design_principle", "scope": "personal"},
        )
        # The model-facing tool has no adoption authority: the record waits for
        # the owner or for a hook that observed the user's own message.
        self.assertEqual(proposed["status"], "pending")
        adopted = self.client.call("memory_confirm", {"candidate_id": proposed["memory_id"], "decision": "adopt"})
        self.assertEqual(adopted["status"], "adopted")
        snapshot = self.client.call("memory_snapshot", {"query": "実測"})
        self.assertIn("実測を最優先する", snapshot["context_text"])
        search = self.client.call("memory_search", {"query": "実測"})
        self.assertTrue(search["results"])
        activity = self.client.call(
            "memory_remember",
            {
                "statement": "【活動記録】MCP確認",
                "kind": "activity_summary",
                "activity": {
                    "date": "2026-11-01",
                    "overview": "MCP経由の活動記録",
                    "achievements": ["構造化保存"],
                    "decisions": [],
                    "remaining": [],
                },
            },
        )
        self.assertEqual(activity["status"], "adopted")
        stored = self.client.call("memory_get", {"memory_id": activity["memory_id"]})
        self.assertEqual(stored["activity"]["overview"], "MCP経由の活動記録")
        snapshot = self.client.call("memory_snapshot", {})
        self.assertNotIn("MCP経由の活動記録", snapshot["context_text"])

    def test_key_tools_never_return_values(self) -> None:
        self.initialise()
        metas = self.client.call("key_list")["keys"]
        self.assertEqual(len(metas), 1)
        self.assertNotIn("super-secret-value", json.dumps(metas))
        result = self.client.call(
            "key_exec",
            {
                "command": f'"{sys.executable}" -c "import os; print(os.environ.get(\'MCP_DEMO_KEY\',\'\'))"',
                "key_ids": [self.key.id],
                "timeout_seconds": 30,
            },
        )
        self.assertEqual(result["status"], "finished")
        self.assertNotIn("super-secret-value", json.dumps(result))
        self.assertIn("[REDACTED", result["stdout"])
        status = self.client.call("exec_status", {"exec_id": result["exec_id"]})
        self.assertEqual(status["status"], "finished")
        raw = "".join(self.client.transcript)
        self.assertNotIn("super-secret-value", raw)

    def test_server_exits_cleanly_on_eof(self) -> None:
        self.initialise()
        code = self.client.stop()
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
