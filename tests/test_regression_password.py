from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from password_input.service import PasswordBroker, PasswordError
from tests.helpers import new_secret_layout


class PasswordClaimTests(unittest.TestCase):
    """#28: one pending request accepts exactly one response."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_secret_layout(Path(self.temp.name))
        self.broker = PasswordBroker(self.layout)
        self.request = self.broker.create_request(
            "local", "user", "true", adapter="sudo", exec_id="exec_" + "a" * 32
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_remote_sudo_target_is_refused(self) -> None:
        with self.assertRaises(PasswordError):
            self.broker.create_request(
                "example.test", "user", "true", adapter="sudo", exec_id="exec_" + "b" * 32
            )

    def test_local_sudo_target_is_accepted(self) -> None:
        request = self.broker.create_request("local", "user", "true", adapter="sudo", exec_id="exec_" + "c" * 32)
        self.assertEqual(request.host, "local")

    def test_password_is_not_piped_when_sudo_does_not_need_it(self) -> None:
        import os

        helper = Path(self.temp.name) / "nopasswd_sudo.py"
        helper.write_text(
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "if '-n' in args:\n"
            "    sys.exit(0)\n"
            "data = sys.stdin.read()\n"
            "path = os.environ.get('LEAK_FILE')\n"
            "if path and data:\n"
            "    open(path, 'w').write(data)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        leak = Path(self.temp.name) / "leak.txt"
        request = self.broker.create_request("local", "user", "true", adapter="sudo", exec_id="exec_" + "d" * 32)
        with mock.patch.dict(os.environ, {"LEAK_FILE": str(leak)}):
            result = self.broker.run(request.id, "correct-password", executable=[sys.executable, str(helper)], timeout=10)
        self.assertTrue(result["ok"])
        self.assertFalse(leak.exists() and leak.read_text(encoding="utf-8").strip())

    def test_windows_askpass_does_not_expand_the_password_through_cmd(self) -> None:
        from password_input.service import _ASKPASS_CMD

        self.assertNotIn("%ULTRA_RAG_SSH_PASSWORD%", _ASKPASS_CMD)
        self.assertIn("[Console]::Out", _ASKPASS_CMD)

    def test_concurrent_responses_are_refused_after_the_first(self) -> None:
        outcomes: list[object] = []
        barrier = threading.Barrier(2)

        def fake_sudo(*args, **kwargs):
            # Both responses reach the command only when the claim is missing.
            # With the fix, the second caller fails before this point and the
            # barrier simply times out for the winner.
            try:
                barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
            return {"ok": True, "status": "consumed", "diagnostic": "authenticated", "exit_code": 0}

        def call() -> None:
            try:
                outcomes.append(
                    self.broker.run(
                        self.request.id, "correct-password", executable=[sys.executable, "-c", "pass"], timeout=10
                    )
                )
            except PasswordError as error:
                outcomes.append(type(error).__name__)

        with mock.patch.object(PasswordBroker, "_run_sudo", fake_sudo):
            threads = [threading.Thread(target=call) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len([item for item in outcomes if isinstance(item, dict)]), 1)

    def test_remote_sudo_target_is_refused(self) -> None:
        with self.assertRaises(PasswordError):
            self.broker.create_request(
                "example.test", "user", "true", adapter="sudo", exec_id="exec_" + "b" * 32
            )

    def test_local_sudo_target_is_accepted(self) -> None:
        request = self.broker.create_request("local", "user", "true", adapter="sudo", exec_id="exec_" + "c" * 32)
        self.assertEqual(request.host, "local")

    def test_password_is_not_piped_when_sudo_does_not_need_it(self) -> None:
        import os

        helper = Path(self.temp.name) / "nopasswd_sudo.py"
        helper.write_text(
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "if '-n' in args:\n"
            "    sys.exit(0)\n"
            "data = sys.stdin.read()\n"
            "path = os.environ.get('LEAK_FILE')\n"
            "if path and data:\n"
            "    open(path, 'w').write(data)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        leak = Path(self.temp.name) / "leak.txt"
        request = self.broker.create_request("local", "user", "true", adapter="sudo", exec_id="exec_" + "d" * 32)
        with mock.patch.dict(os.environ, {"LEAK_FILE": str(leak)}):
            result = self.broker.run(request.id, "correct-password", executable=[sys.executable, str(helper)], timeout=10)
        self.assertTrue(result["ok"])
        self.assertFalse(leak.exists() and leak.read_text(encoding="utf-8").strip())

    def test_windows_askpass_does_not_expand_the_password_through_cmd(self) -> None:
        from password_input.service import _ASKPASS_CMD

        self.assertNotIn("%ULTRA_RAG_SSH_PASSWORD%", _ASKPASS_CMD)
        self.assertIn("[Console]::Out", _ASKPASS_CMD)

    def test_concurrent_responses_are_refused_after_the_first(self) -> None:
        outcomes: list[object] = []

        def call() -> None:
            try:
                outcomes.append(
                    self.broker.run(
                        self.request.id, "correct-password", executable=[sys.executable, "-c", "pass"], timeout=10
                    )
                )
            except PasswordError as error:
                outcomes.append(type(error).__name__)

        barrier = threading.Barrier(2)
        original_find = PasswordBroker._find

        def synchronized_find(broker_self, request_id):
            request = original_find(broker_self, request_id)
            barrier.wait(timeout=10)
            return request

        with mock.patch.object(PasswordBroker, "_find", synchronized_find), mock.patch.object(
            PasswordBroker,
            "_run_sudo",
            lambda *args, **kwargs: {"ok": True, "status": "consumed", "diagnostic": "authenticated", "exit_code": 0},
        ):
            threads = [threading.Thread(target=call) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len([item for item in outcomes if isinstance(item, dict)]), 1)
        self.assertIn("PasswordError", outcomes)


if __name__ == "__main__":
    unittest.main()
