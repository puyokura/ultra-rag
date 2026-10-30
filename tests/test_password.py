from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from password_input.service import PasswordBroker, PasswordError, fingerprint_of_public_key
from tests.helpers import new_secret_layout

FAKE_SUDO = r'''
import sys
password = sys.stdin.readline().rstrip("\n")
args = sys.argv[1:]
print("attempted:" + " ".join(args), file=sys.stderr)
if password == "correct-password":
    print("sudo-ok")
    sys.exit(0)
print("Sorry, try again.", file=sys.stderr)
sys.exit(1)
'''

FAKE_SSH = r'''
import os
import sys
helper = os.environ.get("SSH_ASKPASS", "")
password = os.environ.get("ULTRA_RAG_SSH_PASSWORD", "")
print("helper=" + helper, file=sys.stderr)
print("args=" + " ".join(sys.argv[1:]), file=sys.stderr)
if password == "correct-password" and helper:
    print("ssh-ok")
    sys.exit(0)
print("Permission denied (publickey,password).", file=sys.stderr)
sys.exit(255)
'''


class PasswordBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.layout = new_secret_layout(self.base)
        self.exec_id = "exec_" + "a" * 32
        self.broker = PasswordBroker(self.layout)
        self.helper = self.base / "fake_sudo.py"
        self.helper.write_text(FAKE_SUDO, encoding="utf-8")
        self.ssh_helper = self.base / "fake_ssh.py"
        self.ssh_helper.write_text(FAKE_SSH, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_request_expires_after_ttl(self) -> None:
        request = self.broker.create_request("host", "user", "echo hi", adapter="sudo", exec_id=self.exec_id)
        data = json.loads((self.layout.password / "requests.json").read_text(encoding="utf-8"))
        data["requests"][request.id]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        (self.layout.password / "requests.json").write_text(json.dumps(data), encoding="utf-8")
        status = self.broker.status(request.id)
        self.assertEqual(status["status"], "expired")
        with self.assertRaises(PasswordError):
            self.broker.run(request.id, "correct-password", executable=[sys.executable, str(self.helper)])

    def test_sudo_adapter_uses_one_time_password_without_returning_raw_output(self) -> None:
        request = self.broker.create_request("host", "user", "echo hi", adapter="sudo", exec_id=self.exec_id)
        result = self.broker.run(
            request.id,
            "correct-password",
            executable=[sys.executable, str(self.helper)],
            timeout=30,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["diagnostic"], "authenticated")
        self.assertNotIn("attempted", json.dumps(result))
        self.assertEqual(self.broker.status(request.id)["status"], "consumed")
        raw = (self.layout.password / "requests.json").read_text(encoding="utf-8")
        self.assertNotIn("correct-password", raw)
        with self.assertRaises(PasswordError):
            self.broker.run(request.id, "correct-password", executable=[sys.executable, str(self.helper)])

    def test_sudo_authentication_failure_is_classified(self) -> None:
        request = self.broker.create_request("host", "user", "echo hi", adapter="sudo", exec_id=self.exec_id)
        result = self.broker.run(
            request.id,
            "wrong-password",
            executable=[sys.executable, str(self.helper)],
            timeout=30,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostic"], "authentication_failed")
        self.assertEqual(self.broker.status(request.id)["status"], "failed")

    def test_ssh_requires_accepted_host_key(self) -> None:
        request = self.broker.create_request("example.test", "user", "true", adapter="ssh", exec_id=self.exec_id)
        with self.assertRaises(PasswordError):
            self.broker.run(
                request.id,
                "correct-password",
                executable=[sys.executable, str(self.ssh_helper)],
                timeout=30,
            )
        self.assertEqual(self.broker.status(request.id)["status"], "pending")

    def test_ssh_adapter_delivers_password_via_askpass_environment(self) -> None:
        key = "AAAAC3NzaC1lZDI1NTE5AAAAIGZha2U="
        scanned = {
            "host": "example.test",
            "port": 22,
            "key_type": "ssh-ed25519",
            "public_key": key,
            "fingerprint": fingerprint_of_public_key("ssh-ed25519", key),
        }
        self.broker.import_hostkeys(
            {
                "records": [
                    {
                        **scanned,
                        "accepted_at": "2026-01-01T00:00:00Z",
                        "accepted_by": "owner",
                    }
                ],
                "known_hosts": [f"example.test ssh-ed25519 {key}"],
            }
        )
        request = self.broker.create_request(
            "example.test", "user", "true", adapter="ssh", exec_id=self.exec_id, host_key_fingerprint=scanned["fingerprint"]
        )
        with mock.patch.object(PasswordBroker, "scan_host_key", return_value=[scanned]):
            result = self.broker.run(
                request.id,
                "correct-password",
                executable=[sys.executable, str(self.ssh_helper)],
                timeout=30,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["diagnostic"], "authenticated")
        self.assertNotIn("correct-password", json.dumps(result))
        self.assertFalse(list((self.layout.run).glob("ultra-rag-askpass-*")))

    def test_host_key_accept_records_dedicated_known_hosts(self) -> None:
        key = "AAAAC3NzaC1lZDI1NTE5AAAAIGZha2U="
        scanned = {
            "host": "example.test",
            "port": 22,
            "key_type": "ssh-ed25519",
            "public_key": key,
            "fingerprint": fingerprint_of_public_key("ssh-ed25519", key),
        }
        with mock.patch.object(PasswordBroker, "scan_host_key", return_value=[scanned]):
            record = self.broker.accept_host_key("example.test", 22, scanned["fingerprint"])
        self.assertEqual(record.fingerprint, scanned["fingerprint"])
        known = (self.layout.password / "known_hosts").read_text(encoding="utf-8")
        self.assertIn("example.test ssh-ed25519", known)
        self.assertEqual(len(self.broker.known_host_keys()), 1)

    def test_cancel_prevents_input(self) -> None:
        request = self.broker.create_request("host", "user", "echo hi", adapter="sudo", exec_id=self.exec_id)
        self.broker.cancel(request.id)
        self.assertEqual(self.broker.status(request.id)["status"], "cancelled")
        with self.assertRaises(PasswordError):
            self.broker.run(request.id, "whatever", executable=[sys.executable, str(self.helper)])

    def test_request_is_bound_to_terminal_and_exec_id(self) -> None:
        request = self.broker.create_request(
            "host", "user", "echo hi", adapter="sudo", exec_id=self.exec_id
        )
        self.assertEqual(request.device_id, self.broker.device_id)
        self.assertEqual(request.exec_id, self.exec_id)
        by_exec = self.broker.status_by_exec(self.exec_id)
        self.assertEqual(by_exec["id"], request.id)
        with self.assertRaises(PasswordError):
            self.broker.status_by_exec("exec_" + "b" * 32)
        data = json.loads((self.layout.password / "requests.json").read_text(encoding="utf-8"))
        data["requests"][request.id]["device_id"] = "dev_" + "f" * 32
        (self.layout.password / "requests.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(PasswordError):
            self.broker.status(request.id)

    def test_stopped_terminal_cannot_create_or_provide(self) -> None:
        from secret_core.devices import DeviceRegistry

        registry = DeviceRegistry(self.layout)
        registry.stop(self.broker.device_id)
        with self.assertRaises(PasswordError):
            self.broker.create_request("host", "user", "echo hi", adapter="sudo", exec_id=self.exec_id)

    def test_multiple_host_keys_are_kept_and_selection_is_explicit(self) -> None:
        first = "AAAAC3NzaC1lZDI1NTE5AAAAIGZha2U="
        second = "AAAAC3NzaC1lZDI1NTE5AAAAIEFub3RoZXI="
        scanned_first = {
            "host": "example.test",
            "port": 2222,
            "key_type": "ssh-ed25519",
            "public_key": first,
            "fingerprint": fingerprint_of_public_key("ssh-ed25519", first),
        }
        scanned_second = {
            "host": "example.test",
            "port": 2222,
            "key_type": "ssh-ed25519",
            "public_key": second,
            "fingerprint": fingerprint_of_public_key("ssh-ed25519", second),
        }
        with mock.patch.object(PasswordBroker, "scan_host_key", return_value=[scanned_first, scanned_second]):
            self.broker.accept_host_key("example.test", 2222, scanned_first["fingerprint"])
            self.broker.accept_host_key("example.test", 2222, scanned_second["fingerprint"])
            self.broker.accept_host_key("example.test", 2222, scanned_second["fingerprint"])
        records = self.broker.known_host_keys()
        self.assertEqual(len(records), 2)
        lines = (self.layout.password / "known_hosts").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len({line for line in lines if line.startswith("[example.test]:2222 ")}), 2)
        with mock.patch.object(PasswordBroker, "scan_host_key", return_value=[scanned_first, scanned_second]):
            with self.assertRaises(PasswordError):
                self.broker.select_host_key("example.test", 2222, None)
            selected = self.broker.select_host_key("example.test", 2222, scanned_second["fingerprint"])
        self.assertEqual(selected["fingerprint"], scanned_second["fingerprint"])
        bundle = self.broker.export_hostkeys()
        self.assertEqual(len(bundle["records"]), 2)
        imported = self.broker.import_hostkeys(bundle)
        self.assertEqual(imported["records_added"], 0)
        self.assertEqual(imported["known_hosts_added"], 0)

    def test_host_key_check_reports_change(self) -> None:
        first = "AAAAC3NzaC1lZDI1NTE5AAAAIGZha2U="
        second = "AAAAC3NzaC1lZDI1NTE5AAAAIEFub3RoZXI="
        scanned_first = {
            "host": "example.test",
            "port": 22,
            "key_type": "ssh-ed25519",
            "public_key": first,
            "fingerprint": fingerprint_of_public_key("ssh-ed25519", first),
        }
        scanned_second = {**scanned_first, "public_key": second, "fingerprint": fingerprint_of_public_key("ssh-ed25519", second)}
        with mock.patch.object(PasswordBroker, "scan_host_key", return_value=[scanned_first]):
            self.broker.accept_host_key("example.test", 22, scanned_first["fingerprint"])
            result = self.broker.check_host_key("example.test", 22)
            self.assertEqual(result["status"], "unchanged")
        with mock.patch.object(PasswordBroker, "scan_host_key", return_value=[scanned_second]):
            result = self.broker.check_host_key("example.test", 22)
            self.assertEqual(result["status"], "changed")

    def test_fingerprint_format(self) -> None:
        value = fingerprint_of_public_key("ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAIGZha2U=")
        self.assertTrue(value.startswith("SHA256:"))
        self.assertNotIn("=", value.split(":", 1)[1])


if __name__ == "__main__":
    unittest.main()

