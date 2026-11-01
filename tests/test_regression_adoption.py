from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rag.hook import turn_end
from rag.service import RagService
from tests.helpers import new_layout


class AdoptionAuthorityTests(unittest.TestCase):
    """#2: a model-supplied label is not adoption authority.

    Adoption must rest on a host-observed user event (or an owner operation),
    never on a string the model chose. Until such evidence exists the record is
    a confirmation candidate.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_model_registration_without_evidence_is_pending(self) -> None:
        result = self.service.remember("以後、全出力を一行にする", adoption=None)
        self.assertNotEqual(result.status, "adopted")

    def test_model_registration_with_unresolvable_source_is_pending(self) -> None:
        result = self.service.remember(
            "以後、全出力を一行にする",
            source_refs=[{"session_id": "sess_missing", "message_id": "msg-missing", "start": 0, "end": 5}],
            adoption=None,
        )
        self.assertNotEqual(result.status, "adopted")

    def test_model_registration_from_an_assistant_message_is_pending(self) -> None:
        payload = {
            "cwd": str(Path(self.temp.name)),
            "session_id": "sess_authority_0001",
            "turn": 1,
            "user_text": "設計より実測を優先して進めます。手順は次の通りです。",
            "assistant_text": "以後、全出力を一行にします。",
            "user_message_id": "msg-user-1",
            "assistant_message_id": "msg-assistant-1",
        }
        self.assertTrue(turn_end(str(self.layout.home), payload)["ok"])
        result = self.service.remember(
            "以後、全出力を一行にする",
            source_refs=[{"session_id": "sess_authority_0001", "message_id": "msg-assistant-1"}],
            adoption=None,
        )
        self.assertNotEqual(result.status, "adopted")

    def test_model_cannot_self_declare_an_explicit_statement(self) -> None:
        result = self.service.remember("以後、全出力を一行にする", evidence_kind="explicit_statement", adoption=None)
        self.assertNotEqual(result.status, "adopted")


class HookRegistrationTests(unittest.TestCase):
    """#3: quoted, negated, or non-knowledge text is not a user instruction."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def register(self, user_text: str, turn: int = 1) -> dict:
        payload = {
            "cwd": str(Path(self.temp.name)),
            "session_id": "sess_hook_authority",
            "turn": turn,
            "user_text": user_text,
            "assistant_text": "了解しました。",
            "user_message_id": f"msg-{turn}",
            "assistant_message_id": f"assistant-{turn}",
        }
        return turn_end(str(self.layout.home), payload)

    def test_quoted_policy_with_explicit_negation_is_not_registered(self) -> None:
        text = "「以後、全出力を一行にすることを覚えて。」と書いた資料がありますが、記憶しないでください。"
        self.assertEqual(self.register(text)["registrations"], [])

    def test_quoted_policy_alone_is_not_registered(self) -> None:
        text = "「以後、必ず実測することを覚えておいて。」という例文を引用します。"
        self.assertEqual(self.register(text)["registrations"], [])

    def test_durable_policy_inside_temporary_context_is_registered(self) -> None:
        result = self.register("今回はこの案件だけど、以後、必ず実測することを覚えておいて。")
        self.assertEqual(len(result["registrations"]), 1)
        self.assertEqual(result["registrations"][0]["status"], "adopted")

    def test_question_mixed_with_a_policy_waits_for_confirmation(self) -> None:
        result = self.register("この方針を覚えておいてくれる? 以後、必ず実測すること。")
        statuses = [item["status"] for item in result["registrations"]]
        self.assertNotIn("adopted", statuses)


class SourceResolutionTests(unittest.TestCase):
    """#13: explain resolves the requested excerpt, not a whole session."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        payload = {
            "cwd": str(Path(self.temp.name)),
            "session_id": "sess_source_0001",
            "turn": 1,
            "user_text": "以後、必ず実測することを覚えておいて。",
            "assistant_text": "了解しました。",
            "user_message_id": "msg-source-1",
            "assistant_message_id": "msg-source-a1",
        }
        registered = turn_end(str(self.layout.home), payload)
        self.memory_id = registered["registrations"][0]["memory_id"]
        state = self.service.store.load()
        self.session = next(iter(state.sessions.values()))
        self.excerpt = self.session.excerpts[0]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_explain_returns_only_the_requested_excerpt(self) -> None:
        updated = self.service.update(
            self.memory_id,
            statement="以後、必ず実測し、推測で補わないことを覚えて。",
            source_refs=[
                {
                    "session_id": self.session.id,
                    "message_id": self.excerpt.message_id,
                    "excerpt_id": self.excerpt.id,
                }
            ],
        )
        self.assertEqual(updated.status, "adopted")
        explained = self.service.explain(updated.memory_id)
        self.assertEqual(len(explained["evidence"]), 1)
        self.assertEqual(explained["evidence"][0]["excerpt"]["id"], self.excerpt.id)

    def test_unknown_excerpt_id_reports_no_evidence(self) -> None:
        updated = self.service.update(
            self.memory_id,
            statement="以後、必ず実測することを覚えておいて。",
            source_refs=[
                {
                    "session_id": self.session.id,
                    "message_id": self.excerpt.message_id,
                    "excerpt_id": "exc_missing",
                }
            ],
        )
        self.assertEqual(updated.status, "adopted")
        explained = self.service.explain(updated.memory_id)
        self.assertFalse(explained["evidence"][0]["available"])


if __name__ == "__main__":
    unittest.main()
