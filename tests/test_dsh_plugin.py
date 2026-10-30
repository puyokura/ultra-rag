from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import new_layout
from rag.service import RagService

ROOT = Path(__file__).resolve().parents[1]
NODE = "node"


class DshPluginHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        self.service.remember("必ず実プロセスで検証する", kind="requirement")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plugin_injects_memory_and_triggers_turn_save(self) -> None:
        env = os.environ.copy()
        env["ULTRA_RAG_REPO"] = str(ROOT)
        env["ULTRA_RAG_HOME"] = str(self.layout.home)
        env["ULTRA_RAG_PYTHON"] = sys.executable
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT), str(ROOT / "secret-core"), str(ROOT / "key-use"), str(ROOT / "password-input"), str(ROOT / "cli")]
        )
        result = subprocess.run(
            [NODE, str(ROOT / "tests" / "node" / "plugin_harness.mjs")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(data["ok"])
        self.assertIn("必ず実プロセスで検証する", data["injected"])
        self.assertIn("ultra-RAG", data["summary"])
        self.assertFalse([warning for warning in data["warnings"] if "failed" in warning])
        deadline = time.time() + 20
        while time.time() < deadline:
            state = self.service.store.load()
            session = state.sessions.get("sess_plugin_0001")
            if session and session.excerpts:
                break
            time.sleep(0.3)
        session = self.service.store.load().sessions.get("sess_plugin_0001")
        self.assertIsNotNone(session)
        self.assertTrue(session.excerpts)
        self.assertTrue(any("実測を優先" in excerpt.text for excerpt in session.excerpts))


if __name__ == "__main__":
    unittest.main()
