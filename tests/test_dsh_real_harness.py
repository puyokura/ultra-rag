from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.fake_llm_server import FakeModelServer
from tests.helpers import new_layout
from rag.service import RagService

ROOT = Path(__file__).resolve().parents[1]
DSH = shutil.which("dsh") or (str(Path.home() / "AppData" / "Roaming" / "npm" / "dsh.cmd") if os.name == "nt" else None)


@unittest.skipUnless(DSH, "dsh CLI is not available")
class RealDshHarnessTests(unittest.TestCase):
    """Boots the real dsh headless profile with a test-double model endpoint.

    This verifies dsh plugin loading, agent/pre-step injection, the MCP stdio
    tool path, and the turn-end hook. It does not verify a real external model.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.layout = new_layout(self.base)
        self.service = RagService(self.layout)
        self.service.remember("必ず実プロセスで検証する", kind="requirement")
        self.model = FakeModelServer().start()

    def tearDown(self) -> None:
        self.model.stop()
        self.temp.cleanup()

    def test_real_dsh_boot_injects_context_and_calls_mcp_tool(self) -> None:
        overlay = self.base / "overlay.yml"
        home = str(self.layout.home).replace("\\", "/")
        plugin = str(ROOT / "rag" / "dsh-plugin" / "index.mjs").replace("\\", "/")
        mcp_script = str(ROOT / "scripts" / "run_mcp.py").replace("\\", "/")
        overlay.write_text(
            f"""- insert:
    - id: ultra-rag-memory
      name: {plugin}
      config:
        home: {home}
    - id: mcp-ultra-rag
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: ultra-rag
        transport: stdio
        command: {sys.executable.replace(chr(92), '/')}
        args:
          - {mcp_script}
          - --home
          - {home}

- id: system-prompt
  config:
    personaPrefix: >-
      You are an autonomous engineer. Use the available memory tools.
    personaSuffix: >-
      Respond to the task.

- id: llm-pi-ai
  config:
    providers:
      fake:
        displayName: Fake
        baseURL: http://127.0.0.1:{self.model.port}/v1
        api: openai-completions
        apiKeyEnv: FAKE_LLM_KEY
        models:
          - id: fake-model
            name: Fake Model
            contextWindow: 32000
            maxTokens: 1024

- id: agent-default-model
  config:
    provider: fake
    model: fake-model
""",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["FAKE_LLM_KEY"] = "dummy-test-key"
        env["ULTRA_RAG_HOME"] = str(self.layout.home)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        result = subprocess.run(
            [
                str(DSH),
                "--profile",
                "headless",
                "--patch",
                str(overlay),
                "私の方針として『以後、回答は日本語で簡潔に書くこと』を memory_remember で登録して。",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            env=env,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout}\nstderr={result.stderr}")
        captured = [json.dumps(item, ensure_ascii=False) for item in self.model.requests]
        joined = "\n".join(captured)
        self.assertIn("[ultra-RAG memory", joined)
        self.assertIn("必ず実プロセスで検証する", joined)
        self.assertIn("memory_remember", joined)

        deadline = time.time() + 30
        memory = None
        session = None
        while time.time() < deadline:
            state = self.service.store.load()
            memory = next(
                (record for record in state.memories.values() if "以後、回答は日本語で簡潔に書くこと" == record.statement),
                None,
            )
            session = next((item for item in state.sessions.values() if item.excerpts), None)
            if memory and session:
                break
            time.sleep(0.4)
        self.assertIsNotNone(memory, "the scripted MCP tool call did not persist a memory")
        self.assertEqual(memory.status, "adopted")
        self.assertIsNotNone(session, "the turn-end hook did not persist excerpts")
        self.assertTrue(any("以後" in excerpt.text or "実プロセス" in excerpt.text for excerpt in session.excerpts))


if __name__ == "__main__":
    unittest.main()
