from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from contracts.models import Proposal
from tests.helpers import new_layout, build_session_file
from rag.service import RagService
from rag.store import MemoryStore, StoreCorruption


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.layout = new_layout(self.base)
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_initial_revision_is_verifiable(self) -> None:
        info = self.service.verify()
        self.assertEqual(info["revision"], 1)
        self.assertEqual(info["memories"], 0)
        directory = self.layout.rag / "revisions" / "000001"
        self.assertTrue((directory / "profile" / "current.md").exists())
        self.assertTrue((directory / "profile" / "constraints.md").exists())

    def test_explicit_memory_appears_in_profile_and_constraints(self) -> None:
        principle = self.service.remember("実測を設計より優先する", kind="design_principle")
        requirement = self.service.remember("必ず実プロセスで検証する", kind="requirement")
        self.assertEqual(principle.status, "adopted")
        self.assertEqual(requirement.status, "adopted")
        snapshot = self.service.snapshot(query="実測")
        self.assertIn("実測を設計より優先する", snapshot["profile"])
        self.assertIn("必ず実プロセスで検証する", snapshot["constraints"])
        self.assertIn("実測を設計より優先する", snapshot["context_text"])
        self.assertNotIn("(適用される禁止・必須事項はない)", snapshot["constraints"])
        self.assertEqual(len(snapshot["related"]), 1)

    def test_inferred_memory_is_a_candidate_not_policy(self) -> None:
        result = self.service.remember("たぶん簡潔な回答が好き", evidence_kind="extracted")
        self.assertEqual(result.status, "pending")
        snapshot = self.service.snapshot()
        self.assertNotIn("たぶん簡潔な回答が好き", snapshot["profile"])
        self.assertNotIn("たぶん簡潔な回答が好き", snapshot["constraints"])
        self.assertIn("たぶん簡潔な回答が好き", snapshot["context_text"])
        confirmed = self.service.confirm(result.memory_id, "adopt")
        self.assertEqual(confirmed.status, "adopted")
        self.assertIn("たぶん簡潔な回答が好き", self.service.snapshot()["profile"])

    def test_update_keeps_superseded_version_and_returns_new_id(self) -> None:
        created = self.service.remember("回答は短くする", kind="preference")
        old = self.service.get_memory(created.memory_id)
        updated = self.service.update(created.memory_id, statement="回答は短く、条件は省略しない", target_revision=old["revision"])
        self.assertEqual(updated.status, "adopted")
        self.assertNotEqual(updated.memory_id, created.memory_id)
        new = self.service.get_memory(updated.memory_id)
        self.assertEqual(new["supersedes"], created.memory_id)
        old_after = self.service.get_memory(created.memory_id)
        self.assertEqual(old_after["status"], "superseded")
        profile = self.service.snapshot()["profile"]
        self.assertIn("回答は短く、条件は省略しない", profile)
        self.assertNotIn("回答は短くする", profile)

    def test_stale_target_change_is_held_as_conflict(self) -> None:
        created = self.service.remember("見た目より検証", kind="preference")
        first_revision = self.service.get_memory(created.memory_id)["revision"]
        self.service.update(created.memory_id, statement="検証を最優先する", target_revision=first_revision)
        revision_before = self.service.revision()
        result = self.service.propose(
            {
                "op": "update",
                "update_id": "upd_conflict_case_000001",
                "base_revision": revision_before,
                "device_id": self.service.store.device_id,
                "target_id": created.memory_id,
                "target_revision": first_revision,
                "statement": "見た目を最優先する",
                "evidence_kind": "explicit_statement",
            }
        )
        self.assertEqual(result.status, "conflict")
        self.assertEqual(self.service.revision(), revision_before)
        self.assertEqual(self.service.get_memory(created.memory_id)["status"], "superseded")

    def test_update_id_is_idempotent_and_content_bound(self) -> None:
        proposal = Proposal(
            op="create",
            update_id="upd_idempotent_000001",
            base_revision=self.service.revision(),
            device_id=self.service.store.device_id,
            kind="preference",
            scope="personal",
            statement="同じ更新IDは一度だけ適用する",
            evidence_kind="explicit_statement",
        )
        first = self.service.propose(proposal)
        revision = self.service.revision()
        replay = self.service.propose(Proposal.from_dict({**proposal.to_dict()}))
        self.assertEqual(replay.status, first.status)
        self.assertEqual(replay.memory_id, first.memory_id)
        self.assertEqual(self.service.revision(), revision)
        altered = Proposal.from_dict({**proposal.to_dict(), "statement": "異なる内容"})
        rejected = self.service.propose(altered)
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("different content", rejected.message)

    def test_forget_keeps_record_but_stops_automatic_reference(self) -> None:
        created = self.service.remember("古い好み", kind="preference")
        forgotten = self.service.forget(created.memory_id)
        self.assertEqual(forgotten.status, "forgotten")
        record = self.service.get_memory(created.memory_id)
        self.assertEqual(record["status"], "forgotten")
        self.assertFalse(record["autoload"])
        snapshot = self.service.snapshot(query="古い好み")
        self.assertNotIn("古い好み", snapshot["profile"])
        self.assertEqual(snapshot["related"], [])

    def test_delete_removes_body_and_excerpts_and_blocks_reimport(self) -> None:
        session_path = build_session_file(
            self.base,
            "sess_delete_case",
            str(self.base),
            [{"user": "以後、必ず朝に確認する方針です。", "assistant": "了解しました。"}],
        )
        from rag.importer import import_session

        stats = import_session(self.service, session_path, self.base / "sessions")
        self.assertGreaterEqual(stats["candidates"], 1)
        candidate = self.service.list_candidates()[0]
        memory = self.service.get_memory(candidate["id"])
        deleted = self.service.delete(candidate["id"], confirm=True)
        self.assertEqual(deleted.status, "deleted")
        self.assertIsNone(self.service.get_memory(candidate["id"]))
        sessions = self.service.store.load().sessions
        for session in sessions.values():
            for excerpt in session.excerpts:
                self.assertNotEqual(excerpt.text, memory["statement"])
        state = self.service.store.load()
        self.assertTrue(state.tombstones)
        replay = self.service.remember(
            memory["statement"],
            evidence_kind="imported_statement",
            source_refs=memory["source_refs"],
        )
        self.assertEqual(replay.status, "rejected")
        self.assertIn("deleted", replay.message)
        ref = memory["source_refs"][0]
        turn_replay = self.service.record_turn(
            session_id=ref["session_id"],
            project_id=None,
            turn=2,
            user_text=memory["statement"],
            assistant_text="再取り込みの試行です。",
            user_message_id=ref["message_id"],
            excerpts=[
                {
                    "role": "user",
                    "message_id": ref["message_id"],
                    "start": ref["start"],
                    "end": ref["end"],
                    "text": memory["statement"],
                }
            ],
        )
        self.assertFalse(turn_replay.get("saved"))
        state_after = self.service.store.load()
        for session in state_after.sessions.values():
            self.assertNotIn(memory["statement"], [excerpt.text for excerpt in session.excerpts])

    def test_publication_failure_keeps_previous_revision(self) -> None:
        before = self.service.revision()
        with mock.patch("rag.store.render_profile", side_effect=RuntimeError("simulated failure")):
            with self.assertRaises(RuntimeError):
                self.service.remember("失敗してはならない記憶")
        self.assertEqual(self.service.revision(), before)
        self.assertEqual(self.service.store.load().memories, {})
        success = self.service.remember("失敗後の正常な記憶")
        self.assertEqual(success.status, "adopted")
        self.assertEqual(self.service.revision(), before + 1)

    def test_manifest_tamper_is_detected(self) -> None:
        created = self.service.remember("改訂の完全性を検証する")
        state = self.service.store.load()
        path = None
        for candidate in state.directory.rglob("*.md"):
            if created.memory_id in candidate.name:
                path = candidate
                break
        self.assertIsNotNone(path)
        path.write_text(path.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")
        with self.assertRaises(StoreCorruption):
            self.service.store.load(verify=True)

    def test_turn_excerpt_must_match_source_exactly(self) -> None:
        with self.assertRaises(Exception):
            self.service.record_turn(
                session_id="sess_exact",
                project_id=None,
                turn=1,
                user_text="原文はこうです。",
                assistant_text="承知しました。",
                excerpts=[{"role": "user", "text": "原文はこうだ"}],
            )
        with self.assertRaises(Exception):
            self.service.record_turn(
                session_id="sess_exact",
                project_id=None,
                turn=1,
                user_text="原文はこうです。",
                assistant_text="承知しました。",
                excerpts=[{"role": "user", "text": "言い換えた文章", "start": 0, "end": 5}],
            )
        result = self.service.record_turn(
            session_id="sess_exact",
            project_id=None,
            turn=1,
            user_text="原文はこうです。",
            assistant_text="承知しました。",
            excerpts=[{"role": "user", "text": "原文はこうです。"}],
        )
        self.assertTrue(result["saved"])

    def test_structural_turn_records_only_selected_paragraphs(self) -> None:
        user_text = "今日は天気の話をします。\n\n設計より実測を優先する方針で進めます。\n\n雑談です。"
        assistant_text = "了解しました。以後、必ず実測します。"
        result = self.service.record_turn("sess_structural", None, 1, user_text, assistant_text)
        self.assertTrue(result["saved"])
        state = self.service.store.load()
        session = state.sessions["sess_structural"]
        texts = [excerpt.text for excerpt in session.excerpts]
        self.assertIn("設計より実測を優先する方針で進めます。", texts)
        self.assertNotIn("今日は天気の話をします。", texts)
        self.assertNotIn("雑談です。", texts)
        for excerpt in session.excerpts:
            self.assertIn(excerpt.text, user_text if excerpt.role == "user" else assistant_text)

    def test_same_turn_is_idempotent(self) -> None:
        first = self.service.record_turn("sess_idem", None, 1, "必ず検証する方針です。", "了解。")
        second = self.service.record_turn("sess_idem", None, 1, "必ず検証する方針です。", "了解。")
        self.assertTrue(second.get("already_recorded") or not second.get("saved"))
        self.assertEqual(second["revision"], first["revision"])


if __name__ == "__main__":
    unittest.main()
