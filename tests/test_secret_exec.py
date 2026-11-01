from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from contracts.config import Layout, load_config
from key_use.service import KeyService
from tests.helpers import new_secret_layout
from secret_core.audit import AuditError, AuditLog
from secret_core.devices import DeviceRegistry
from secret_core.lease import LeaseManager
from secret_core.runner import ExecManager, is_elevated
from secret_core.vault import Vault, VaultError


def py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def echo_env(name: str) -> str:
    return py(f"import os; print(os.environ.get('{name}',''))")


def write_file(name: str, content: str, exit_code: int = 0) -> str:
    code = (
        f"from pathlib import Path; import sys; "
        f"Path('{name}').write_text('{content}', encoding='utf-8'); sys.exit({exit_code})"
    )
    return py(code)


def list_workspace() -> str:
    return py("import os; print(chr(10).join(sorted(os.listdir('.'))))")


@unittest.skipIf(os.name == "nt" and is_elevated(), "key_exec correctly refuses administrator tokens")
class KeyExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.temp.name)
        self.layout = new_secret_layout(self.base)
        self.config = load_config(self.layout, create=False)
        self.vault = Vault(self.layout)
        self.key = self.vault.register("test", "demo", "tests", "value-abc-123", env_name="DEMO_KEY")
        self.service = KeyService(self.layout)

    def tearDown(self) -> None:
        from secret_core.runner import kill_process_tree

        for pid_file in (self.layout.run).glob("*/pid.json"):
            try:
                pid = int(json.loads(pid_file.read_text(encoding="utf-8")).get("pid", 0))
                if pid:
                    kill_process_tree(pid)
            except Exception:
                pass
        time.sleep(0.8)
        self.temp.cleanup()

    def test_vault_lists_metadata_only_and_encrypts_value(self) -> None:
        metas = self.vault.list_keys()
        self.assertEqual(len(metas), 1)
        payload = json.dumps([meta.to_dict() for meta in metas])
        self.assertNotIn("value-abc-123", payload)
        entry_files = list((self.layout.secret / "vault" / "entries").glob("*.json"))
        self.assertEqual(len(entry_files), 1)
        raw = entry_files[0].read_bytes()
        self.assertNotIn(b"value-abc-123", raw)
        resolved = self.vault.resolve([self.key.id])
        self.assertEqual(resolved["DEMO_KEY"], "value-abc-123")

    def test_rotate_and_delete(self) -> None:
        self.vault.rotate(self.key.id, "new-value-456")
        self.assertEqual(self.vault.resolve([self.key.id])["DEMO_KEY"], "new-value-456")
        self.vault.delete(self.key.id)
        self.assertEqual(self.vault.list_keys(), [])
        with self.assertRaises(VaultError):
            self.vault.resolve([self.key.id])

    def test_register_many_is_prevalidated(self) -> None:
        with self.assertRaises(VaultError):
            self.vault.register_many(
                [
                    {"name": "dup", "service": "demo", "purpose": "tests", "value": "x", "env_name": "DEMO_KEY"},
                    {"name": "new", "service": "demo", "purpose": "tests", "value": "y", "env_name": "NEW_KEY"},
                ]
            )
        self.assertEqual([meta.env_name for meta in self.vault.list_keys()], ["DEMO_KEY"])

    def test_rename_preserves_sealed_value(self) -> None:
        entry = self.layout.secret / "vault" / "entries" / f"{self.key.id}.json"
        before = json.loads(entry.read_text(encoding="utf-8"))["sealed"]
        meta = self.vault.rename(self.key.id, "renamed-key", "renamed-service")
        self.assertEqual(meta.name, "renamed-key")
        self.assertEqual(meta.service, "renamed-service")
        after = json.loads(entry.read_text(encoding="utf-8"))["sealed"]
        self.assertEqual(before, after)
        self.assertEqual(self.vault.resolve([self.key.id])["DEMO_KEY"], "value-abc-123")
        self.assertEqual(self.vault.get_meta(self.key.id).name, "renamed-key")

    def test_device_authentication_and_stop(self) -> None:
        registry = DeviceRegistry(self.layout)
        self.assertFalse(registry.authenticate(self.config["device_id"], "wrong"))
        self.assertTrue(registry.authenticate_local(self.config["device_id"]))
        registry.stop(self.config["device_id"])
        self.assertFalse(registry.authenticate_local(self.config["device_id"]))
        result = self.service.key_exec("echo hi", [self.key.id], timeout_seconds=10)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("device", result["detail"])

    def test_exec_masks_secret_and_audit_stays_minimal(self) -> None:
        result = self.service.key_exec(echo_env("DEMO_KEY"), [self.key.id], timeout_seconds=30)
        self.assertEqual(result["status"], "finished")
        self.assertNotIn("value-abc-123", json.dumps(result))
        self.assertIn("[REDACTED:", result["stdout"])
        self.assertIn("既知値の完全一致", result["masking_note"])
        self.assertGreaterEqual(result["masking_detections"]["total"], 1)
        self.assertIn("exact", result["masking_detections"]["types"])
        audit = json.dumps(AuditLog(self.layout).read())
        self.assertNotIn("value-abc-123", audit)
        self.assertNotIn("echo", audit)
        self.assertNotIn("stdout", audit)
        self.assertNotIn("DEMO_KEY", os.environ)

    def test_base64_masking_is_detected_and_reported(self) -> None:
        code = (
            "import base64, os; "
            "print(base64.b64encode(os.environ['DEMO_KEY'].encode()).decode())"
        )
        result = self.service.key_exec(py(code), [self.key.id], timeout_seconds=30)
        self.assertEqual(result["status"], "finished")
        self.assertNotIn("dmFsdWUtYWJjLTEyMw==", result["stdout"])
        self.assertIn("base64", result["masking_detections"]["types"])
        self.assertIn("[REDACTED", result["stdout"])

    def test_masking_failure_returns_no_output_and_no_artifacts(self) -> None:
        destination = self.base / "masking-failure"
        destination.mkdir()
        (destination / "out.txt").write_text("old", encoding="utf-8")
        command = write_file("out.txt", "artifact-content")
        with mock.patch("secret_core.runner.mask_known_values", side_effect=RuntimeError("mask failure")):
            result = self.service.key_exec(
                command,
                [self.key.id],
                timeout_seconds=30,
                deliverables=["out.txt"],
                deliver_to=str(destination),
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "masking_failed")
        self.assertEqual(result["masking_status"], "failed")
        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["artifacts"]["imported"], [])
        self.assertTrue(result["artifacts"]["kept_workspace"])
        self.assertEqual((destination / "out.txt").read_text(encoding="utf-8"), "old")
        self.assertIn("masking_failed", json.dumps(result))

    def test_audit_start_failure_refuses_execution(self) -> None:
        with mock.patch.object(AuditLog, "start", side_effect=AuditError("disk full")):
            result = self.service.key_exec("echo should-not-run", [self.key.id], timeout_seconds=10)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("audit_start_failed", result["detail"])
        exec_dir = self.layout.run / result["exec_id"]
        self.assertFalse((exec_dir / "pid.json").exists())
        self.assertFalse((exec_dir / "workspace").exists())

    def test_command_containing_a_selected_value_is_rejected(self) -> None:
        result = self.service.key_exec("echo value-abc-123", [self.key.id], timeout_seconds=10)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("secret_in_command", result["detail"])
        self.assertNotIn("value-abc-123", json.dumps(result))

    def test_stale_lease_refuses_new_supply(self) -> None:
        lease_path = self.layout.secret / "control" / "lease.json"
        lease_path.write_text(json.dumps({"last_refresh": time.time() - 1000}), encoding="utf-8")
        result = self.service.key_exec("echo hi", [self.key.id], timeout_seconds=10)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("lease", result["detail"])

    def test_timeout_stops_process(self) -> None:
        command = f'"{sys.executable}" -c "import time; time.sleep(10)"'
        result = self.service.key_exec(command, [self.key.id], timeout_seconds=1)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_reason"], "timeout")

    def test_duplicate_id_and_generation_change(self) -> None:
        exec_id = "exec_11111111111111111111111111111111"
        from contracts.models import ExecRequest

        request = ExecRequest(
            command="echo one",
            key_ids=[self.key.id],
            device_id=self.config["device_id"],
            host="local",
            timeout_seconds=30,
            exec_id=exec_id,
        )
        first = self.service.manager.execute(request)
        self.assertEqual(first.status, "finished")
        again = self.service.manager.execute(request)
        self.assertEqual(again.status, first.status)
        different = ExecRequest(
            command="echo two",
            key_ids=[self.key.id],
            device_id=self.config["device_id"],
            host="local",
            timeout_seconds=30,
            exec_id=exec_id,
        )
        rejected = self.service.manager.execute(different)
        self.assertEqual(rejected.status, "rejected")
        # A separate manager process (same boot) must still see the state.
        manager2 = ExecManager(self.layout)
        self.assertEqual(manager2.status(exec_id).status, "finished")
        # Simulate a reboot: the boot marker changes and old ids are no longer queryable.
        epoch_path = self.layout.run / "epoch.json"
        epoch = json.loads(epoch_path.read_text(encoding="utf-8"))
        epoch["boot_marker"] = "simulated-reboot"
        epoch_path.write_text(json.dumps(epoch), encoding="utf-8")
        manager3 = ExecManager(self.layout)
        self.assertEqual(manager3.status(exec_id).status, "unknown")

    def test_host_mismatch_is_not_started(self) -> None:
        other_host = "pc" if self.config.get("host") == "server" else "server"
        result = self.service.key_exec("echo hi", [self.key.id], host=other_host, timeout_seconds=10)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("host_mismatch", result["detail"])

    def test_revocation_kills_resident_execution(self) -> None:
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        result = self.service.key_exec(command, [self.key.id], mode="resident")
        self.assertEqual(result["status"], "running")
        time.sleep(1.0)
        self.vault.delete(self.key.id)
        deadline = time.time() + 15
        status = result
        while time.time() < deadline:
            status = self.service.exec_status(result["exec_id"])
            if status["status"] != "running":
                break
            time.sleep(0.5)
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["stop_reason"], "key_revoked")

    def test_rotation_stops_old_execution_but_allows_new_supply(self) -> None:
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        old = self.service.key_exec(command, [self.key.id], mode="resident")
        self.assertEqual(old["status"], "running")
        time.sleep(0.6)
        self.vault.rotate(self.key.id, "rotated-value-789")
        deadline = time.time() + 15
        status = old
        while time.time() < deadline:
            status = self.service.exec_status(old["exec_id"])
            if status["status"] != "running":
                break
            time.sleep(0.4)
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["stop_reason"], "key_rotated")
        new = self.service.key_exec(echo_env("DEMO_KEY"), [self.key.id], timeout_seconds=30)
        self.assertEqual(new["status"], "finished")
        self.assertIn("[REDACTED", new["stdout"])

    def test_lease_expiry_stops_existing_execution(self) -> None:
        layout = new_secret_layout(self.base / "lease")
        config = load_config(layout, create=False)
        config["secret"]["lease_seconds"] = 1
        from contracts.config import save_config

        save_config(layout, config)
        vault = Vault(layout)
        key = vault.register("lease", "demo", "test", "lease-secret", env_name="LEASE_KEY")
        service = KeyService(layout)
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        result = service.key_exec(command, [key.id], mode="resident")
        self.assertEqual(result["status"], "running")
        deadline = time.time() + 15
        status = result
        while time.time() < deadline:
            status = service.exec_status(result["exec_id"])
            if status["status"] != "running":
                break
            time.sleep(0.5)
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["stop_reason"], "lease_expired")

    def test_artifacts_import_only_on_success_and_overwrite(self) -> None:
        destination = self.base / "deliver"
        destination.mkdir()
        (destination / "out.txt").write_text("old content", encoding="utf-8")
        success = self.service.key_exec(
            write_file("out.txt", "new-content"),
            [self.key.id],
            timeout_seconds=30,
            deliverables=["out.txt"],
            deliver_to=str(destination),
        )
        self.assertEqual(success["status"], "finished")
        self.assertEqual(success["artifacts"]["imported"], ["out.txt"])
        self.assertEqual((destination / "out.txt").read_text(encoding="utf-8").strip(), "new-content")

        failed = self.service.key_exec(
            write_file("out.txt", "partial", exit_code=3),
            [self.key.id],
            timeout_seconds=30,
            deliverables=["out.txt"],
            deliver_to=str(destination),
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["artifacts"]["imported"], [])
        self.assertTrue(failed["artifacts"]["kept_workspace"])
        self.assertTrue(Path(failed["artifacts"]["workspace"]).is_dir())
        self.assertEqual((destination / "out.txt").read_text(encoding="utf-8").strip(), "new-content")

    def test_execution_workspaces_are_separate(self) -> None:
        first = self.service.key_exec(write_file("only-first.txt", "first"), [self.key.id], timeout_seconds=30)
        second = self.service.key_exec(list_workspace(), [self.key.id], timeout_seconds=30)
        self.assertEqual(first["status"], "finished")
        self.assertEqual(second["status"], "finished")
        self.assertNotIn("only-first.txt", second["stdout"])
        self.assertNotEqual(first["workspace"], second["workspace"])

    def test_partial_artifact_import_reports_failures(self) -> None:
        destination = self.base / "partial"
        destination.mkdir()
        result = self.service.key_exec(
            write_file("one.txt", "one"),
            [self.key.id],
            timeout_seconds=30,
            deliverables=["one.txt", "missing.txt"],
            deliver_to=str(destination),
        )
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["artifacts"]["imported"], ["one.txt"])
        self.assertEqual(result["artifacts"]["failed"][0]["path"], "missing.txt")
        self.assertTrue((destination / "one.txt").exists())

    def test_owner_stop_kills_execution(self) -> None:
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        result = self.service.key_exec(command, [self.key.id], mode="resident")
        self.assertEqual(result["status"], "running")
        self.service.stop(result["exec_id"], reason="owner_stop")
        deadline = time.time() + 15
        status = result
        while time.time() < deadline:
            status = self.service.exec_status(result["exec_id"])
            if status["status"] != "running":
                break
            time.sleep(0.4)
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["stop_reason"], "owner_stop")

    def test_monitor_death_stops_execution(self) -> None:
        from secret_core.runner import kill_process_tree

        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        result = self.service.key_exec(command, [self.key.id], mode="resident")
        self.assertEqual(result["status"], "running")
        time.sleep(0.8)
        monitor_data = json.loads(
            (self.layout.run / result["exec_id"] / "monitor.json").read_text(encoding="utf-8")
        )
        kill_process_tree(int(monitor_data["pid"]))
        deadline = time.time() + 15
        status = result
        while time.time() < deadline:
            status = self.service.exec_status(result["exec_id"])
            if status["status"] != "running":
                break
            time.sleep(0.3)
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["stop_reason"], "monitor_stopped")

    def test_keyrun_foreground_supplies_environment_and_masks(self) -> None:
        script = self.base / "keyrun_child.py"
        script.write_text(
            "import os, sys\n"
            "print('child-key=' + os.environ.get('DEMO_KEY', ''))\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "secret-core")])
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "secret_core.keyrun",
                "--home",
                str(self.layout.home),
                "--key",
                self.key.id,
                "--",
                sys.executable,
                str(script),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
            cwd=str(self.base),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("value-abc-123", result.stdout)
        self.assertIn("[REDACTED", result.stdout)
        audit = AuditLog(self.layout).read()
        self.assertTrue(any(row.get("phase") == "start" for row in audit))
        self.assertTrue(any(row.get("phase") == "end" for row in audit))

    def test_lease_daemon_keeps_permission_fresh(self) -> None:
        from secret_core.lease import LeaseManager

        old = time.time() - 10_000
        lease_path = self.layout.secret / "control" / "lease.json"
        lease_path.write_text(json.dumps({"last_refresh": old}), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "secret-core")]
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "secret_core.lease_daemon",
                "--home",
                str(self.layout.home),
                "--interval",
                "0.2",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.7)
            manager = LeaseManager(self.layout, 300)
            self.assertTrue(manager.fresh())
        finally:
            process.terminate()
            code = process.wait(timeout=10)
            if os.name != "nt":
                self.assertEqual(code, 0)

    def test_directory_input_and_symlink_not_followed(self) -> None:
        source = self.base / "input-dir"
        (source / "sub").mkdir(parents=True)
        (source / "a.txt").write_text("a", encoding="utf-8")
        (source / "sub" / "b.txt").write_text("b", encoding="utf-8")
        link = source / "link.txt"
        try:
            os.symlink(source / "a.txt", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not permitted")
        command = py(
            "import os; print('\\n'.join(sorted(p for p in os.listdir('input-dir') if p != 'sub'))); "
            "print('sub-b=' + os.path.exists('input-dir/sub/b.txt').__str__())"
        )
        result = self.service.key_exec(command, [self.key.id], timeout_seconds=30, input_paths=[str(source)])
        self.assertEqual(result["status"], "finished")
        self.assertIn("a.txt", result["stdout"])
        self.assertNotIn("link.txt", result["stdout"])
        self.assertIn("sub-b=True", result["stdout"])
        self.assertTrue(result["inputs"]["skipped"])

    def test_directory_artifact_is_recursive_and_atomic(self) -> None:
        destination = self.base / "deliver-dir"
        destination.mkdir()
        code = (
            "from pathlib import Path; "
            "Path('outdir/sub').mkdir(parents=True, exist_ok=True); "
            "Path('outdir/a.txt').write_text('a'); "
            "Path('outdir/sub/b.txt').write_text('b')"
        )
        result = self.service.key_exec(
            py(code),
            [self.key.id],
            timeout_seconds=30,
            deliverables=["outdir"],
            deliver_to=str(destination),
        )
        self.assertEqual(result["status"], "finished")
        self.assertIn("outdir/a.txt", result["artifacts"]["imported"])
        self.assertIn("outdir/sub/b.txt", result["artifacts"]["imported"])
        self.assertEqual((destination / "outdir" / "sub" / "b.txt").read_text(encoding="utf-8"), "b")
        self.assertFalse(list(destination.rglob(".*tmp*")))

    def test_resident_artifact_can_be_imported_explicitly(self) -> None:
        destination = self.base / "deliver-resident"
        destination.mkdir()
        code = (
            "from pathlib import Path; import time; "
            "Path('made.txt').write_text('resident'); time.sleep(30)"
        )
        result = self.service.key_exec(
            py(code),
            [self.key.id],
            mode="resident",
            deliverables=["made.txt"],
            deliver_to=str(destination),
        )
        self.assertEqual(result["status"], "running")
        workspace = Path(result["workspace"])
        deadline = time.time() + 10
        while time.time() < deadline and not (workspace / "made.txt").exists():
            time.sleep(0.2)
        imported = self.service.exec_artifacts(result["exec_id"])
        self.assertIn("made.txt", imported["imported"])
        self.assertEqual((destination / "made.txt").read_text(encoding="utf-8"), "resident")
        self.service.stop(result["exec_id"], reason="test_cleanup")

    def test_status_and_stop_are_limited_to_issuing_terminal(self) -> None:
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        result = self.service.key_exec(command, [self.key.id], mode="resident")
        self.assertEqual(result["status"], "running")
        other = "dev_" + "f" * 32
        denied_status = self.service.manager.status(result["exec_id"], caller_device=other)
        self.assertEqual(denied_status.status, "rejected")
        self.assertIn("caller_device_mismatch", denied_status.detail)
        allowed_status = self.service.manager.status(result["exec_id"], caller_device=other, allow_other=True)
        self.assertEqual(allowed_status.status, "running")
        denied_stop = self.service.manager.stop(result["exec_id"], caller_device=other)
        self.assertEqual(denied_stop.status, "rejected")
        self.service.stop(result["exec_id"], reason="test_cleanup")

    def test_multiple_stop_requests_are_idempotent(self) -> None:
        import threading

        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        result = self.service.key_exec(command, [self.key.id], mode="resident")
        self.assertEqual(result["status"], "running")
        outcomes: list[str] = []

        def stop() -> None:
            outcomes.append(self.service.stop(result["exec_id"], reason="parallel_stop")["status"])

        threads = [threading.Thread(target=stop) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(outcomes), 5)
        deadline = time.time() + 15
        status = result
        while time.time() < deadline:
            status = self.service.exec_status(result["exec_id"])
            if status["status"] != "running":
                break
            time.sleep(0.3)
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["stop_reason"], "parallel_stop")

    def test_first_stop_reason_is_kept(self) -> None:
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        result = self.service.key_exec(command, [self.key.id], mode="resident")
        self.assertEqual(result["status"], "running")
        self.service.stop(result["exec_id"], reason="first_reason")
        self.service.stop(result["exec_id"], reason="second_reason")
        deadline = time.time() + 15
        status = self.service.exec_status(result["exec_id"])
        while time.time() < deadline and status["status"] == "running":
            time.sleep(0.3)
            status = self.service.exec_status(result["exec_id"])
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["stop_reason"], "first_reason")

    def test_vault_recreate_archives_and_invalidates_old_identities(self) -> None:
        old_device = self.config["device_id"]
        old_token = (self.layout.secret / "devices" / f"{old_device}.token").read_text(encoding="utf-8").strip()
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        execution = self.service.key_exec(command, [self.key.id], mode="resident")
        self.assertEqual(execution["status"], "running")
        result = Vault(self.layout).recreate()
        archive = Path(result["archive"])
        self.assertTrue((archive / "manifest.json").exists())
        self.assertTrue((archive / "vault-old" / "master.key.sealed").exists())
        manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("vault-old/master.key.sealed", manifest["files"])
        self.assertIn(execution["exec_id"], result["invalidated_exec_ids"])
        self.assertIn(old_device, result["invalidated_device_ids"])
        self.assertEqual(Vault(self.layout).initialized, True)
        with self.assertRaises(VaultError):
            Vault(self.layout).resolve([self.key.id])
        self.assertEqual(self.service.exec_status(execution["exec_id"])["status"], "invalid")
        self.assertEqual(DeviceRegistry(self.layout).status(old_device)["status"], "invalid")
        self.assertFalse(DeviceRegistry(self.layout).authenticate(old_device, old_token))
        self.assertNotEqual(load_config(self.layout)["device_id"], old_device)
        self.assertTrue(
            any(row.get("id") == old_device and row.get("status") == "invalid" for row in DeviceRegistry(self.layout).list_devices())
        )
        audit = AuditLog(self.layout).read()
        self.assertTrue(any(row.get("phase") == "vault_recreate" for row in audit))

    def test_concurrency_limit_rejects_extra_execution(self) -> None:
        from contracts.config import save_config

        layout = new_secret_layout(self.base / "concurrency")
        config = load_config(layout, create=False)
        config["secret"]["max_concurrent"] = 1
        save_config(layout, config)
        vault = Vault(layout)
        key = vault.register("concurrency", "demo", "test", "concurrency-secret", env_name="CONCURRENCY_KEY")
        service = KeyService(layout)
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        first = service.key_exec(command, [key.id], mode="resident")
        self.assertEqual(first["status"], "running")
        second = service.key_exec("echo second", [key.id], timeout_seconds=10)
        self.assertEqual(second["status"], "rejected")
        self.assertIn("concurrency_limit", second["detail"])
        service.stop(first["exec_id"], reason="test_cleanup")
        deadline = time.time() + 15
        while time.time() < deadline:
            if service.exec_status(first["exec_id"])["status"] != "running":
                break
            time.sleep(0.3)
        third = service.key_exec("echo third", [key.id], timeout_seconds=10)
        self.assertEqual(third["status"], "finished")

    @unittest.skipUnless(os.name == "nt", "DPAPI sealed-key tampering test is Windows-specific")
    def test_tampered_sealed_key_cannot_unseal(self) -> None:
        sealed = self.layout.secret / "vault" / "master.key.sealed"
        payload = json.loads(sealed.read_text(encoding="utf-8"))
        payload["blob"] = payload["blob"][:-8] + "AAAAAAAA"
        sealed.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(Exception):
            Vault(self.layout).resolve([self.key.id])


if __name__ == "__main__":
    unittest.main()
