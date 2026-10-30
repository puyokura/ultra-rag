from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from rag.direct_edit import DirectEditor
from rag.service import RagService
from tests.helpers import new_layout


class DirectEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        self.created = self.service.remember("元の好み", kind="preference")
        self.editor = DirectEditor(self.service)
        self.editor.export()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _file_for(self, memory_id: str) -> Path:
        for path in self.editor.workspace.glob("*.md"):
            front, _ = self.editor._parse(path)
            if front["memory_id"] == memory_id:
                return path
        raise AssertionError(f"no edit file for {memory_id}")

    def _rewrite(self, path: Path, statement: str | None = None, front_changes: dict | None = None) -> None:
        text = path.read_text(encoding="utf-8")
        _, rest = text.split("---\n", 1)
        front_text, body = rest.split("\n---\n", 1)
        front = yaml.safe_load(front_text) or {}
        front.update(front_changes or {})
        new_text = (
            "---\n"
            + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip()
            + "\n---\n"
            + (statement if statement is not None else body.strip())
            + "\n"
        )
        path.write_text(new_text, encoding="utf-8")

    def test_export_has_id_and_base_revision(self) -> None:
        path = self._file_for(self.created.memory_id)
        front, statement = self.editor._parse(path)
        self.assertEqual(front["memory_id"], self.created.memory_id)
        self.assertEqual(front["base_revision"], self.service.get_memory(self.created.memory_id)["revision"])
        self.assertEqual(statement, "元の好み")

    def test_edit_is_not_visible_until_scan_then_adopted(self) -> None:
        path = self._file_for(self.created.memory_id)
        self._rewrite(path, statement="新しい好み")
        self.assertNotIn("新しい好み", self.service.snapshot()["profile"])
        result = self.editor.scan()
        self.assertEqual(result.imported, 1)
        snapshot = self.service.snapshot()
        self.assertIn("新しい好み", snapshot["profile"])
        self.assertNotIn("元の好み", snapshot["profile"])
        updated = next(
            record for record in self.service.store.load().memories.values() if record.statement == "新しい好み"
        )
        front, _ = self.editor._parse(path)
        self.assertEqual(front["memory_id"], updated.id)
        self.assertEqual(front["base_revision"], updated.revision)

    def test_structural_id_change_is_rejected(self) -> None:
        path = self._file_for(self.created.memory_id)
        other = self.service.remember("別の記憶", kind="preference")
        self.editor.export()
        self._rewrite(path, front_changes={"memory_id": other.memory_id})
        result = self.editor.scan()
        self.assertEqual(result.imported, 0)
        self.assertGreaterEqual(result.failed, 1)
        self.assertCountEqual(
            [record.statement for record in self.service.store.load().memories.values() if record.status == "adopted"],
            ["元の好み", "別の記憶"],
        )

    def test_edit_file_deletion_is_not_a_record_deletion(self) -> None:
        path = self._file_for(self.created.memory_id)
        path.unlink()
        result = self.editor.scan()
        self.assertEqual(result.deletions_ignored, 1)
        self.assertIsNotNone(self.service.get_memory(self.created.memory_id))

    def test_conflicting_edit_is_held(self) -> None:
        path = self._file_for(self.created.memory_id)
        old_revision = self.service.get_memory(self.created.memory_id)["revision"]
        self.service.update(self.created.memory_id, statement="サーバー側の変更", target_revision=old_revision)
        self._rewrite(path, statement="ファイル側の変更", front_changes={"base_revision": old_revision})
        result = self.editor.scan()
        self.assertEqual(result.failed, 1)
        self.assertFalse(any(record.statement == "ファイル側の変更" for record in self.service.store.load().memories.values()))
        self.assertIn("サーバー側の変更", self.service.snapshot()["profile"])

    def test_unknown_new_file_is_not_adopted(self) -> None:
        (self.editor.workspace / "unknown.md").write_text(
            "---\nultra_rag_edit: 1\nmemory_id: mem_ffffffffffffffffffffffffffffffff\nbase_revision: 0\n---\n勝手な追加\n",
            encoding="utf-8",
        )
        result = self.editor.scan()
        self.assertEqual(result.failed, 1)
        self.assertFalse(any(record.statement == "勝手な追加" for record in self.service.store.load().memories.values()))

    def test_watch_process_imports_edit_automatically(self) -> None:
        path = self._file_for(self.created.memory_id)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "secret-core")]
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ultra_rag_cli.main",
                "--home",
                str(self.layout.home),
                "rag",
                "edit",
                "watch",
                "--interval",
                "0.2",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(1.0)
            self._rewrite(path, statement="監視プロセスが取り込む好み")
            deadline = time.time() + 20
            imported = False
            while time.time() < deadline:
                if any(
                    record.statement == "監視プロセスが取り込む好み"
                    for record in self.service.store.load().memories.values()
                ):
                    imported = True
                    break
                time.sleep(0.3)
            self.assertTrue(imported)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    unittest.main()
