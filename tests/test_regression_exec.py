from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from contracts.models import ExecRequest
from key_use.service import KeyService
from password_input.service import PasswordBroker, fingerprint_of_public_key
from secret_core.runner import ExecError, ExecManager, is_elevated
from secret_core.vault import Vault
from tests.helpers import new_secret_layout


def py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def alive(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@unittest.skipIf(os.name == "nt" and is_elevated(), "administrator token")
class ExecutionAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.temp.name)
        self.layout = new_secret_layout(self.base)
        from contracts.config import load_config

        self.config = load_config(self.layout, create=False)
        self.vault = Vault(self.layout)
        self.key = self.vault.register("test", "demo", "tests", "value-abc-123", env_name="DEMO_KEY")
        self.service = KeyService(self.layout)
        self.device = self.config["device_id"]

    def tearDown(self) -> None:
        from secret_core.runner import kill_process_tree

        for pid_file in (self.layout.run).glob("*/pid.json"):
            try:
                import json

                pid = int(json.loads(pid_file.read_text(encoding="utf-8")).get("pid", 0))
                if pid:
                    kill_process_tree(pid)
            except Exception:
                pass
        time.sleep(0.8)
        self.temp.cleanup()

    def request(self, exec_id: str, command: str, mode: str = "resident", **kwargs) -> ExecRequest:
        return ExecRequest(
            exec_id=exec_id,
            command=command,
            key_ids=[self.key.id],
            device_id=self.device,
            host="local",
            mode=mode,
            timeout_seconds=kwargs.pop("timeout_seconds", 30),
            **kwargs,
        )

    def test_concurrent_same_exec_id_starts_one_process(self) -> None:
        exec_id = "exec_" + "a" * 32
        request = self.request(exec_id, py("import time; time.sleep(20)"))
        calls: list[str] = []
        original = ExecManager._launch_supervisor

        def slow_launch(manager_self, req, state, env, started_epoch):
            calls.append(req.exec_id)
            time.sleep(0.4)
            return original(manager_self, req, state, env, started_epoch)

        results = []
        with mock.patch.object(ExecManager, "_launch_supervisor", slow_launch):
            threads = [
                threading.Thread(target=lambda: results.append(self.service.manager.execute(request)))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(calls, [exec_id])
        self.assertTrue(all(state.status == "running" for state in results))
        self.service.stop(exec_id, reason="test_cleanup")

    def test_resident_execution_survives_the_starting_process(self) -> None:
        script = (
            "import sys\n"
            "from contracts.config import Layout\n"
            "from key_use.service import KeyService\n"
            "svc = KeyService(Layout.at(None))\n"
            "print(svc.key_exec(sys.argv[1], [sys.argv[2]], mode='resident', timeout_seconds=30)['exec_id'])\n"
        )
        env = dict(os.environ)
        repo = Path(__file__).resolve().parents[1]
        env["ULTRA_RAG_HOME"] = str(self.layout.home)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(repo), str(repo / "secret-core"), str(repo / "key-use"), str(repo / "password-input"), str(repo / "cli")]
        )
        started = subprocess.run(
            [sys.executable, "-c", script, py("print('hello-from-resident')"), self.key.id],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        exec_id = started.stdout.strip().splitlines()[-1]
        deadline = time.time() + 40
        status = self.service.exec_status(exec_id)
        while time.time() < deadline and status["status"] == "running":
            time.sleep(0.5)
            status = self.service.exec_status(exec_id)
        self.assertEqual(status["status"], "finished")
        self.assertIn("hello-from-resident", status.get("stdout") or "")
        self.assertEqual(status.get("exit_code"), 0)

    def test_old_id_is_not_replayed_after_vault_recreation(self) -> None:
        finished = self.service.key_exec(py("print('done')"), [self.key.id], timeout_seconds=30)
        self.assertEqual(finished["status"], "finished")
        Vault(self.layout).recreate()
        replay = self.service.manager.execute(
            ExecRequest(
                exec_id=finished["exec_id"],
                command=py("print('done')"),
                key_ids=[self.key.id],
                device_id=self.device,
                host="local",
                timeout_seconds=30,
            )
        )
        self.assertNotEqual(replay.status, "finished")

    def test_deliverable_through_a_linked_directory_is_refused(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        workspace = self.base / "workspace"
        workspace.mkdir()
        try:
            os.symlink(outside, workspace / "sub", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        destination = self.base / "delivered"
        destination.mkdir()
        request = self.request(
            "exec_" + "b" * 32,
            "true",
            mode="single",
            deliverables=["sub/secret.txt"],
            deliver_to=str(destination),
        )
        result = self.service.manager._import_artifacts(request, workspace)
        self.assertEqual(result["imported"], [])
        self.assertFalse((destination / "sub" / "secret.txt").exists())

    def test_absolute_deliverable_is_refused(self) -> None:
        workspace = self.base / "workspace2"
        workspace.mkdir()
        destination = self.base / "delivered2"
        destination.mkdir()
        request = self.request(
            "exec_" + "c" * 32,
            "true",
            mode="single",
            deliverables=[str(workspace / "x.txt")],
            deliver_to=str(destination),
        )
        result = self.service.manager._import_artifacts(request, workspace)
        self.assertEqual(result["imported"], [])

    def test_monitor_start_failure_stops_the_child(self) -> None:
        exec_id = "exec_" + "d" * 32
        request = self.request(exec_id, py("import time; time.sleep(30)"), mode="single")
        with mock.patch.object(ExecManager, "_start_monitor", side_effect=OSError("boom")):
            state = self.service.manager.execute(request)
        self.assertEqual(state.status, "failed")
        self.assertIn("monitor_start_failed", state.detail or "")
        import json

        pid = int(json.loads((self.layout.run / exec_id / "pid.json").read_text(encoding="utf-8"))["pid"])
        deadline = time.time() + 10
        while time.time() < deadline and alive(pid):
            time.sleep(0.2)
        self.assertFalse(alive(pid))


    def test_artifacts_are_refused_when_masking_failed(self) -> None:
        exec_id = "exec_" + "f" * 32
        destination = self.base / "masked_out"
        destination.mkdir()
        request = self.request(
            exec_id,
            py("print('x')"),
            mode="single",
            deliverables=["out.txt"],
            deliver_to=str(destination),
        )
        with mock.patch("secret_core.runner.mask_known_values", side_effect=RuntimeError("boom")):
            state = self.service.manager.execute(request)
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.stop_reason, "masking_failed")
        with self.assertRaises(ExecError):
            self.service.manager.import_artifacts(exec_id)

    def test_secret_in_a_deliverable_path_is_rejected_without_persisting_it(self) -> None:
        destination = self.base / "path_out"
        destination.mkdir()
        result = self.service.key_exec(
            py("print(1)"),
            [self.key.id],
            timeout_seconds=30,
            deliverables=["value-abc-123.txt"],
            deliver_to=str(destination),
        )
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("value-abc-123", json.dumps(result))
        saved = json.loads((self.layout.run / result["exec_id"] / "request.json").read_text(encoding="utf-8"))
        self.assertNotIn("value-abc-123", json.dumps(saved))

    def test_key_rotation_during_supply_is_detected(self) -> None:
        request = self.request("exec_" + "9" * 32, py("print(1)"), mode="single")
        original = Vault.resolve
        rotations = self.service.manager.revocations

        def resolving(vault_self, key_ids):
            values = original(vault_self, key_ids)
            rotations.rotate_key(self.key.id)
            return values

        with mock.patch.object(Vault, "resolve", resolving):
            state = self.service.manager.execute(request)
        self.assertEqual(state.status, "rejected")
        self.assertIn("key_rotated_before_start", state.detail or "")

    @unittest.skipIf(os.name == "nt", "process groups are POSIX only")
    def test_background_descendants_block_completion(self) -> None:
        request = self.request(
            "exec_" + "8" * 32,
            "sleep 30 >/dev/null 2>&1 & exit 0",
            mode="single",
            timeout_seconds=2,
        )
        state = self.service.manager.execute(request)
        self.assertNotEqual(state.status, "finished")
        self.assertEqual(state.stop_reason, "descendants_timeout")

class KnownHostsExclusivityTests(unittest.TestCase):
    """#32: the global known_hosts must not satisfy host verification."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.temp.name)
        self.layout = new_secret_layout(self.base)
        self.broker = PasswordBroker(self.layout)
        key = "AAAAC3NzaC1lZDI1NTE5AAAAIGZha2U="
        self.scanned = {
            "host": "example.test",
            "port": 22,
            "key_type": "ssh-ed25519",
            "public_key": key,
            "fingerprint": fingerprint_of_public_key("ssh-ed25519", key),
        }
        self.broker.import_hostkeys(
            {
                "records": [{**self.scanned, "accepted_at": "2026-01-01T00:00:00Z", "accepted_by": "owner"}],
                "known_hosts": [f"example.test ssh-ed25519 {key}"],
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_ssh_command_ignores_global_known_hosts_and_host_key_updates(self) -> None:
        request = self.broker.create_request(
            "example.test",
            "user",
            "true",
            adapter="ssh",
            exec_id="exec_" + "e" * 32,
            host_key_fingerprint=self.scanned["fingerprint"],
        )
        captured: list[list[str]] = []

        def fake_run(command, **kwargs):
            captured.append(list(command))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("password_input.service.subprocess.run", side_effect=fake_run):
            self.broker.run(request.id, "correct-password", executable=[sys.executable, "-c", "pass"], timeout=5)
        ssh_commands = [command for command in captured if any("UserKnownHostsFile=" in part for part in command)]
        self.assertTrue(ssh_commands, "the ssh command was not captured")
        options = " ".join(ssh_commands[0])
        self.assertIn("GlobalKnownHostsFile=", options)
        self.assertIn("UpdateHostKeys=no", options)
        self.assertIn("UserKnownHostsFile=", options)


if __name__ == "__main__":
    unittest.main()
