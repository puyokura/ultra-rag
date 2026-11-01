from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contracts.models import ContractError, Proposal
from contracts.util import read_jsonl
from rag.hook import turn_end
from rag.service import RagService
from tests.helpers import new_layout

SECRET = "sk-" + "a" * 30


class SecretGateTests(unittest.TestCase):
    """#4: every write path passes the same redaction gate."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_statement_secret_is_redacted_before_storage(self) -> None:
        created = self.service.remember(f"api_key={SECRET} を保存する", kind="preference")
        stored = self.service.get_memory(created.memory_id)
        self.assertNotIn(SECRET, stored["statement"])
        self.assertIn("[REDACTED]", stored["statement"])

    def test_turn_note_secret_is_redacted_before_storage(self) -> None:
        self.service.record_turn(
            session_id="sess_note_secret",
            project_id=None,
            turn=1,
            user_text="作業メモを残す。",
            assistant_text="了解しました。",
            user_message_id="msg-1",
            assistant_message_id="msg-2",
            excerpts=[
                {
                    "role": "user",
                    "message_id": "msg-1",
                    "start": 0,
                    "end": len("作業メモを残す。"),
                    "text": "作業メモを残す。",
                }
            ],
            note=f"token={SECRET}",
        )
        session = self.service.store.load().sessions["sess_note_secret"]
        self.assertNotIn(SECRET, session.turns[0].note or "")


class KindScopeInvariantTests(unittest.TestCase):
    """#6: a project decision cannot live in the personal profile."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_project_decision_requires_project_scope(self) -> None:
        with self.assertRaises(ContractError):
            self.service.remember("案件AではSQLiteを使う", kind="project_decision", scope="personal")

    def test_project_decision_is_absent_from_the_personal_profile(self) -> None:
        with self.assertRaises(ContractError):
            self.service.remember("案件BではPostgreSQLを使う", kind="project_decision", scope="shared_lesson")
        self.assertNotIn("PostgreSQLを使う", self.service.snapshot()["profile"])


class SessionOwnershipTests(unittest.TestCase):
    """#7: a session memory belongs to the session that created it."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        self.created = self.service.remember(
            "セッションAだけの作業メモ",
            scope="session",
            session_id="sess_owner_A",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_other_session_cannot_search_it(self) -> None:
        self.assertEqual(self.service.search("作業メモ", scopes=["session"], session_id="sess_owner_B"), [])

    def test_owner_session_can_search_it(self) -> None:
        hits = self.service.search("作業メモ", scopes=["session"], session_id="sess_owner_A")
        self.assertEqual([hit["memory_id"] for hit in hits], [self.created.memory_id])

    def test_other_session_does_not_see_it_in_candidates(self) -> None:
        self.service.remember("セッションAの保留", scope="session", session_id="sess_owner_A", evidence_kind="extracted", adoption=None)
        page = self.service.candidate_page(session_id="sess_owner_B")
        self.assertEqual(page["items"], [])

    def test_session_memory_requires_an_owner(self) -> None:
        with self.assertRaises(ContractError):
            self.service.remember("所有者のないセッション記憶", scope="session")


class LineageHeadTests(unittest.TestCase):
    """#8: one lineage keeps exactly one adopted head."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_updating_a_superseded_version_is_rejected(self) -> None:
        first = self.service.remember("元の方針", kind="preference")
        base = self.service.get_memory(first.memory_id)["revision"]
        second = self.service.update(first.memory_id, statement="改訂した方針", target_revision=base)
        self.assertEqual(second.status, "adopted")
        superseded_revision = self.service.get_memory(first.memory_id)["revision"]
        result = self.service.update(first.memory_id, statement="古い版からの分岐", target_revision=superseded_revision)
        self.assertEqual(result.status, "rejected")
        heads = [
            record.id
            for record in self.service.store.load().memories.values()
            if record.status == "adopted"
            and (record.id == second.memory_id or record.supersedes == first.memory_id)
        ]
        self.assertEqual(heads, [second.memory_id])


class ForgetSuppressionTests(unittest.TestCase):
    """#9: a forgotten policy is not resurrected by replaying the same payload."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        self.payload = {
            "cwd": str(Path(self.temp.name)),
            "session_id": "sess_forget_0001",
            "turn": 1,
            "user_text": "以後、必ず実測することを覚えておいて。",
            "assistant_text": "了解しました。",
            "user_message_id": "msg-forget-1",
            "assistant_message_id": "msg-forget-2",
        }
        first = turn_end(str(self.layout.home), self.payload)
        self.memory_id = first["registrations"][0]["memory_id"]
        self.service.forget(self.memory_id)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_replaying_the_same_payload_does_not_adopt_it_again(self) -> None:
        replay = turn_end(str(self.layout.home), self.payload)
        self.assertNotEqual(replay["registrations"][0]["status"], "adopted")
        adopted = [
            record
            for record in self.service.store.load().memories.values()
            if record.statement == "以後、必ず実測することを覚えておいて。" and record.status == "adopted"
        ]
        self.assertEqual(adopted, [])


class DeleteCascadeTests(unittest.TestCase):
    """#10: deleting a memory invalidates candidates and notes that cite it."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        self.refs = [{"session_id": "sess_cascade", "message_id": "msg-cascade", "start": 0, "end": 4}]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_candidate_sharing_deleted_evidence_cannot_be_confirmed(self) -> None:
        adopted = self.service.remember("共有証拠の記憶", source_refs=self.refs)
        candidate = self.service.remember(
            "共有証拠の候補",
            source_refs=self.refs,
            evidence_kind="extracted",
            adoption=None,
        )
        self.assertEqual(candidate.status, "pending")
        self.service.delete(adopted.memory_id, confirm=True)
        self.assertEqual(self.service.confirm(candidate.memory_id).status, "rejected")


class ConditionStateTests(unittest.TestCase):
    """#12/#11: unspecified and explicitly empty are different, and the
    duplicate key includes conditions/exceptions."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        self.created = self.service.remember(
            "条件付きの方針",
            kind="preference",
            conditions=["案件A"],
            exceptions=["緊急時"],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_explicit_empty_clears_conditions(self) -> None:
        updated = self.service.update(self.created.memory_id, conditions=[], exceptions=[])
        self.assertEqual(updated.status, "adopted")
        stored = self.service.get_memory(updated.memory_id)
        self.assertEqual(stored["conditions"], [])
        self.assertEqual(stored["exceptions"], [])

    def test_unspecified_keeps_conditions(self) -> None:
        updated = self.service.update(self.created.memory_id, statement="改訂した方針")
        stored = self.service.get_memory(updated.memory_id)
        self.assertEqual(stored["conditions"], ["案件A"])
        self.assertEqual(stored["exceptions"], ["緊急時"])

    def test_different_conditions_are_not_merged(self) -> None:
        other = self.service.remember("条件付きの方針", kind="preference", conditions=["案件B"])
        self.assertNotEqual(other.status, "merged")
        statements = [
            record for record in self.service.store.load().memories.values() if record.statement == "条件付きの方針"
        ]
        self.assertEqual(len(statements), 2)

    def test_merged_reports_the_existing_status(self) -> None:
        pending = self.service.remember("推測の方針", evidence_kind="extracted", adoption=None)
        self.assertEqual(pending.status, "pending")
        replay = self.service.remember("推測の方針", evidence_kind="extracted", adoption=None)
        self.assertEqual(replay.status, "merged")
        self.assertIn("pending", replay.message)


class ClientQueuePrivacyTests(unittest.TestCase):
    """#5: a client replica queues validated excerpts, not the full transcript."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name), role="client")
        self.service = RagService(self.layout)
        self.service.initialise()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_queue_contains_only_the_selected_excerpt(self) -> None:
        user_text = "前置きの説明です。以後、必ず実測する。"
        excerpt = "以後、必ず実測する。"
        self.service.record_turn(
            session_id="sess_client_queue",
            project_id=None,
            turn=1,
            user_text=user_text,
            assistant_text="了解しました。",
            user_message_id="msg-1",
            assistant_message_id="msg-2",
            excerpts=[
                {
                    "role": "user",
                    "message_id": "msg-1",
                    "start": user_text.index(excerpt),
                    "end": user_text.index(excerpt) + len(excerpt),
                    "text": excerpt,
                }
            ],
        )
        payload = read_jsonl(self.service.store.queue_file)[-1]["payload"]
        self.assertNotIn("user", payload)
        self.assertNotIn("assistant", payload)
        self.assertNotIn("前置きの説明です", json.dumps(payload, ensure_ascii=False))
        self.assertIn(excerpt, json.dumps(payload, ensure_ascii=False))

    def test_mismatched_excerpt_is_rejected_before_queueing(self) -> None:
        before = len(read_jsonl(self.service.store.queue_file))
        with self.assertRaises(Exception):
            self.service.record_turn(
                session_id="sess_client_queue",
                project_id=None,
                turn=1,
                user_text="本文です。",
                assistant_text="了解しました。",
                user_message_id="msg-1",
                assistant_message_id="msg-2",
                excerpts=[
                    {"role": "user", "message_id": "msg-1", "start": 0, "end": 4, "text": "一致しない"}
                ],
            )
        self.assertEqual(len(read_jsonl(self.service.store.queue_file)), before)


class OperationLedgerTests(unittest.TestCase):
    """#15: merged/conflict/rejected outcomes are queryable and idempotent."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def base_proposal(self, update_id: str, **overrides):
        payload = {
            "op": "update",
            "update_id": update_id,
            "base_revision": self.service.revision(),
            "device_id": self.service.store.device_id,
            "statement": "台帳テスト",
            "evidence_kind": "explicit_statement",
            "adoption": "owner_operation",
        }
        payload.update(overrides)
        return Proposal.from_dict(payload)

    def test_conflict_is_recorded(self) -> None:
        created = self.service.remember("台帳の方針", kind="preference")
        proposal = self.base_proposal("upd_ledger_conflict_01", target_id=created.memory_id, target_revision=0)
        result = self.service.propose(proposal)
        self.assertEqual(result.status, "conflict")
        self.assertEqual(self.service.status("upd_ledger_conflict_01")["status"], "conflict")

    def test_same_id_with_different_content_is_rejected(self) -> None:
        created = self.service.remember("台帳の方針", kind="preference")
        proposal = self.base_proposal("upd_ledger_reuse_01", target_id=created.memory_id, target_revision=0)
        self.service.propose(proposal)
        changed = Proposal.from_dict({**proposal.to_dict(), "statement": "別の内容"})
        self.assertEqual(self.service.propose(changed).status, "rejected")

    def test_merged_is_recorded(self) -> None:
        self.service.remember("同一の方針", kind="preference")
        merged = self.service.remember("同一の方針", kind="preference")
        self.assertEqual(merged.status, "merged")
        self.assertEqual(self.service.status(merged.update_id)["status"], "merged")


class ActivitySummaryTests(unittest.TestCase):
    """Activity records are saved without confirmation and never injected."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_activity_summary_is_adopted_and_not_injected(self) -> None:
        result = self.service.remember(
            "【活動記録】今日はUIを追加した。",
            kind="activity_summary",
            adoption="activity_summary",
        )
        self.assertEqual(result.status, "adopted")
        stored = self.service.get_memory(result.memory_id)
        self.assertFalse(stored["autoload"])
        snapshot = self.service.snapshot()
        self.assertNotIn("今日はUIを追加した", snapshot["profile"])
        self.assertNotIn("今日はUIを追加した", snapshot["constraints"])
        self.assertEqual(self.service.search("UIを追加"), [])

    def test_activity_claim_does_not_adopt_other_kinds(self) -> None:
        result = self.service.remember(
            "通常の方針", kind="preference", adoption="activity_summary"
        )
        self.assertNotEqual(result.status, "adopted")


class ProfileCompositionTests(unittest.TestCase):
    """The injected profile is composed from records, not from a Markdown file."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        self.created = self.service.remember("記録から描画する方針", kind="design_principle")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_snapshot_never_reads_the_rendered_markdown_artifact(self) -> None:
        from unittest import mock

        original = Path.read_text

        def guarded(path, *args, **kwargs):
            if path.name == "current.md" and path.parent.name == "profile":
                raise AssertionError("the profile artifact must not be an injection source")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", guarded):
            snapshot = self.service.snapshot()
        self.assertIn("記録から描画する方針", snapshot["profile"])


if __name__ == "__main__":
    unittest.main()
