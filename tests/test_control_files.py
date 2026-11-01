from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contracts.util import write_once_json


class ControlFileTests(unittest.TestCase):
    """Control files that another process polls are immutable once written."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_write_wins(self) -> None:
        target = self.base / "run" / "stop"
        self.assertTrue(write_once_json(target, {"reason": "first"}))
        self.assertFalse(write_once_json(target, {"reason": "second"}))
        stored = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(stored["reason"], "first")

    def test_no_temporary_file_is_left_behind(self) -> None:
        target = self.base / "run" / "stop"
        write_once_json(target, {"reason": "first"})
        self.assertEqual(sorted(path.name for path in target.parent.iterdir()), ["stop"])


if __name__ == "__main__":
    unittest.main()
