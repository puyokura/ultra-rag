from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import run_cli

ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli(self, *args: str) -> tuple[int, str, str]:
        return run_cli(["--home", str(self.home), *args])

    def test_init_doctor_and_memory_flow(self) -> None:
        code, out, _ = self.cli("init")
        self.assertEqual(code, 0)
        info = json.loads(out)
        self.assertEqual(info["role"], "server")
        self.assertEqual(info["rag_revision"], 1)
        code, out, _ = self.cli("doctor")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["checks"]["rag"], "ok")

        code, out, _ = self.cli("rag", "remember", "実測を優先する", "--kind", "design_principle")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "adopted")
        code, out, _ = self.cli("rag", "search", "実測")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out))
        code, out, _ = self.cli("rag", "snapshot", "--query", "実測", "--text")
        self.assertEqual(code, 0)
        self.assertIn("実測を優先する", out)
        code, out, _ = self.cli("rag", "verify")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["memories"], 1)

    def test_forget_delete_and_delete_guard(self) -> None:
        self.cli("init")
        _, out, _ = self.cli("rag", "remember", "忘れる方針", "--kind", "preference")
        memory_id = json.loads(out)["memory_id"]
        code, out, _ = self.cli("rag", "forget", memory_id)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "forgotten")
        code, _, err = self.cli("rag", "delete", memory_id)
        self.assertNotEqual(code, 0)
        self.assertIn("--yes", err)
        code, out, _ = self.cli("rag", "delete", memory_id, "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "deleted")

    def test_secret_registration_exec_and_audit(self) -> None:
        self.cli("init")
        with mock.patch("sys.stdin", io.StringIO("cli-secret-value-123\n")):
            code, out, _ = self.cli(
                "secret", "register", "--name", "cli", "--service", "demo", "--purpose", "test", "--env-name", "CLI_KEY", "--value-stdin"
            )
        self.assertEqual(code, 0)
        meta = json.loads(out)
        self.assertNotIn("cli-secret-value-123", out)
        code, out, _ = self.cli("secret", "list")
        self.assertEqual(code, 0)
        self.assertNotIn("cli-secret-value-123", out)
        code, out, _ = self.cli(
            "secret",
            "exec",
            "--command",
            f'"{sys.executable}" -c "import os; print(os.environ.get(\'CLI_KEY\',\'\'))"',
            "--key",
            meta["id"],
            "--timeout",
            "30",
        )
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["status"], "finished")
        self.assertNotIn("cli-secret-value-123", out)
        self.assertIn("[REDACTED", result["stdout"])
        code, out, _ = self.cli("secret", "audit", "list")
        self.assertEqual(code, 0)
        self.assertNotIn("cli-secret-value-123", out)
        self.assertNotIn("echo", out)

    def test_secret_add_shortcut(self) -> None:
        self.cli("init")
        with mock.patch("sys.stdin", io.StringIO("ADDED_KEY=added-secret-456\n")):
            code, out, _ = self.cli("secret", "add", "ADDED_KEY", "--service", "demo", "--value-stdin")
        self.assertEqual(code, 0)
        meta = json.loads(out)
        self.assertEqual(meta["name"], "added-key")
        self.assertEqual(meta["service"], "demo")
        self.assertEqual(meta["env_name"], "ADDED_KEY")
        self.assertTrue(meta["env_line_prefix_stripped"])
        self.assertNotIn("added-secret-456", out)
        code, out, _ = self.cli("secret", "list")
        self.assertIn("ADDED_KEY", out)
        self.assertNotIn("added-secret-456", out)
        command = f'"{sys.executable}" -c "import os; print(len(os.environ[\'ADDED_KEY\']))"'
        code, out, _ = self.cli("secret", "exec", "--command", command, "--key", meta["id"], "--timeout", "30")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["stdout"].strip(), "16")

    def test_secret_import_env(self) -> None:
        self.cli("init")
        env_file = self.home / ".env"
        env_file.write_text(
            "# owner keys\n"
            "KEY_NAME=key-~~~\n"
            'SECOND_KEY="value # two"\n',
            encoding="utf-8",
        )
        code, out, _ = self.cli("secret", "import-env", str(env_file))
        self.assertEqual(code, 0)
        imported = json.loads(out)["registered"]
        self.assertEqual([item["name"] for item in imported], ["key-name", "second-key"])
        self.assertEqual([item["env_name"] for item in imported], ["KEY_NAME", "SECOND_KEY"])
        self.assertNotIn("key-~~~", out)
        self.assertNotIn("value # two", out)
        ids = [item["id"] for item in imported]
        command = (
            f'"{sys.executable}" -c "import os; '
            "print(len(os.environ['KEY_NAME']), len(os.environ['SECOND_KEY']))"
            '"'
        )
        code, out, _ = self.cli("secret", "exec", "--command", command, "--key", ids[0], "--key", ids[1], "--timeout", "30")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["stdout"].strip(), "7 11")

    def test_overlay_command_contains_resolved_paths(self) -> None:
        self.cli("init")
        code, out, _ = self.cli("rag", "overlay")
        self.assertEqual(code, 0)
        self.assertIn("ultra-rag-memory", out)
        self.assertIn("mcp-ultra-rag", out)
        self.assertIn(str(ROOT / "scripts" / "run_mcp.py").replace("\\", "/"), out)
        self.assertIn(str(self.home).replace("\\", "/"), out)

    def test_password_request_flow(self) -> None:
        self.cli("init")
        code, out, _ = self.cli(
            "password",
            "request",
            "--adapter",
            "sudo",
            "--host",
            "local",
            "--account",
            "u",
            "--command",
            "echo hi",
            "--exec-id",
            "exec_" + "a" * 32,
        )
        self.assertEqual(code, 0)
        request = json.loads(out)
        self.assertEqual(request["status"], "pending")
        code, out, _ = self.cli("password", "list")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), 1)
        with mock.patch("sys.stdin.isatty", return_value=False):
            code, _, err = self.cli("password", "provide", request["id"])
        self.assertNotEqual(code, 0)
        self.assertIn("terminal-only", err)
        code, out, _ = self.cli("password", "cancel", request["id"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "cancelled")

    def test_activity_summary_requires_structured_json(self) -> None:
        self.cli("init")
        payload = json.dumps(
            {
                "date": "2026-11-01",
                "overview": "CLIからの活動記録",
                "achievements": ["記録"],
                "decisions": [],
                "remaining": [],
            },
            ensure_ascii=False,
        )
        code, out, err = self.cli(
            "rag", "remember", "【活動記録】CLI", "--kind", "activity_summary", "--activity-json", payload
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "adopted")
        code, _, err = self.cli("rag", "remember", "活動", "--kind", "activity_summary")
        self.assertNotEqual(code, 0)
        self.assertIn("activity", err)

    @unittest.skipUnless(os.name == "nt", "Windows launchers are verified on Windows")
    def test_launchers_exist_and_run(self) -> None:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [str(ROOT / "bin" / "ultra-rag.cmd"), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ultra-rag", result.stdout)
        result = subprocess.run(
            [str(ROOT / "bin" / "ultra-rag-mcp.cmd"), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ultra-rag-mcp", result.stdout)


if __name__ == "__main__":
    unittest.main()
