from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import new_layout
from rag.hook import turn_end
from rag.service import RagService


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self, user: str, turn: int = 1) -> dict:
        return {
            "cwd": str(Path(self.temp.name)),
            "session_id": "sess_hook_0001",
            "turn": turn,
            "user_text": user,
            "assistant_text": "了解しました。",
            "user_message_id": f"msg-{turn}",
            "assistant_message_id": f"assistant-{turn}",
        }

    def test_explicit_registration_is_adopted_without_a_model_tool_call(self) -> None:
        result = turn_end(str(self.layout.home), self.payload("以後、必ず実プロセスで検証することを覚えて。"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["registrations"])
        adopted = result["registrations"][0]
        self.assertEqual(adopted["status"], "adopted")
        snapshot = self.service.snapshot(query="実プロセス")
        self.assertNotIn("以後、必ず実プロセスで検証することを覚えて。", snapshot["profile"])
        self.assertIn("以後、必ず実プロセスで検証することを覚えて。", snapshot["constraints"])

    def test_reprocessing_the_same_turn_does_not_duplicate_the_memory(self) -> None:
        first = turn_end(str(self.layout.home), self.payload("以後、必ず実測することを覚えて。"))
        revision = self.service.revision()
        second = turn_end(str(self.layout.home), self.payload("以後、必ず実測することを覚えて。"))
        self.assertEqual(second["revision"], revision)
        self.assertEqual(len([m for m in self.service.store.load().memories.values() if "必ず実測" in m.statement]), 1)
        self.assertEqual(first["registrations"][0]["memory_id"], second["registrations"][0]["memory_id"])

    def test_question_containing_remember_word_is_not_registered(self) -> None:
        result = turn_end(str(self.layout.home), self.payload("今覚えてることは?"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["registrations"], [])
        self.assertFalse(any("今覚えてる" in memory.statement for memory in self.service.store.load().memories.values()))

    def test_non_explicit_statement_is_not_auto_adopted(self) -> None:
        result = turn_end(str(self.layout.home), self.payload("設計より実測を優先する方針で進めます。"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["registrations"], [])
        self.assertNotIn("設計より実測を優先する方針で進めます。", self.service.snapshot()["profile"])


if __name__ == "__main__":
    unittest.main()
