from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import new_layout
from rag.search import SearchIndex
from rag.service import RagService


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        self.project_a = "proj_aaaaaaaaaaaaaaaa"
        self.project_b = "proj_bbbbbbbbbbbbbbbb"
        self.service.remember("実測を設計より優先する", kind="design_principle")
        self.service.remember("案件AではSQLiteを使う", kind="project_decision", scope="project", project_id=self.project_a)
        self.service.remember("案件BではPostgreSQLを使う", kind="project_decision", scope="project", project_id=self.project_b)
        self.service.remember("共通の教訓として境界を先に固める", kind="lesson", scope="shared_lesson")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_project_scope_isolation(self) -> None:
        for_project_a = self.service.search("SQLiteを使う", project_id=self.project_a)
        self.assertTrue(for_project_a)
        self.assertTrue(all(hit["project_id"] in (None, self.project_a) for hit in for_project_a))
        for_project_b = self.service.search("SQLiteを使う", project_id=self.project_b)
        self.assertFalse(for_project_b)
        personal_only = self.service.search("SQLiteを使う", project_id=None)
        self.assertFalse([hit for hit in personal_only if hit["scope"] == "project"])

    def test_short_japanese_query_falls_back_to_substring(self) -> None:
        hits = self.service.search("実測")
        self.assertTrue(any(hit["statement"] == "実測を設計より優先する" for hit in hits))

    def test_multi_character_japanese_query(self) -> None:
        hits = self.service.search("設計より優先")
        self.assertTrue(hits)

    def test_pending_is_excluded_by_default(self) -> None:
        pending = self.service.remember("推測された好みの候補", evidence_kind="extracted")
        self.assertEqual(pending.status, "pending")
        default_hits = self.service.search("推測された好み")
        self.assertFalse(default_hits)
        with_pending = self.service.search("推測された好み", include_pending=True)
        self.assertTrue(any(hit["memory_id"] == pending.memory_id for hit in with_pending))

    def test_forgotten_and_deleted_are_not_searchable(self) -> None:
        created = self.service.remember("忘却対象の検索語", kind="preference")
        self.service.forget(created.memory_id)
        self.assertFalse(self.service.search("忘却対象の検索語"))
        other = self.service.remember("削除対象の検索語", kind="preference")
        self.service.delete(other.memory_id, confirm=True)
        self.assertFalse(self.service.search("削除対象の検索語"))

    def test_index_rebuilds_from_readable_revision(self) -> None:
        index_path = self.layout.rag / "index" / "search.sqlite"
        self.assertTrue(index_path.exists())
        index_path.unlink()
        index = SearchIndex(index_path, self.service.store)
        hits = index.search("実測", None)
        self.assertTrue(hits)
        self.assertTrue(index_path.exists())

    def test_stale_index_revision_is_rebuilt(self) -> None:
        hits_before = self.service.search("新しい記憶")
        self.assertFalse(hits_before)
        self.service.remember("新しい記憶を追加した", kind="preference")
        hits_after = self.service.search("新しい記憶")
        self.assertTrue(hits_after)


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_notifications_are_reported_once_per_session(self) -> None:
        session = "sess_notify_0001"
        self.service.remember("最初の方針", kind="preference")
        first = self.service.snapshot(session_id=session)
        self.assertEqual(first["notifications"], [])
        self.service.remember("追加された方針", kind="requirement")
        second = self.service.snapshot(session_id=session)
        self.assertTrue(any("追加された方針" in line for line in second["notifications"]))
        third = self.service.snapshot(session_id=session)
        self.assertEqual(third["notifications"], [])

    def test_notifications_do_not_leak_other_projects(self) -> None:
        project_a = "proj_eeeeeeeeeeeeeeee"
        project_b = "proj_ffffffffffffffff"
        self.service.remember("案件Aの初期方針", kind="project_decision", scope="project", project_id=project_a)
        self.service.snapshot(project_id=project_a, session_id="sess_notify_a")
        self.service.snapshot(project_id=project_b, session_id="sess_notify_b")
        self.service.remember("案件Aの追加方針", kind="project_decision", scope="project", project_id=project_a)
        snapshot_b = self.service.snapshot(project_id=project_b, session_id="sess_notify_b")
        self.assertFalse(any("案件Aの追加方針" in line for line in snapshot_b["notifications"]))
        snapshot_a = self.service.snapshot(project_id=project_a, session_id="sess_notify_a")
        self.assertTrue(any("案件Aの追加方針" in line for line in snapshot_a["notifications"]))

    def test_project_constraints_only_apply_to_their_project(self) -> None:
        project_a = "proj_cccccccccccccccc"
        project_b = "proj_dddddddddddddddd"
        self.service.remember("全案件の必須条件", kind="requirement")
        self.service.remember("案件Aだけの禁止", kind="prohibition", scope="project", project_id=project_a)
        self.service.remember("案件Aだけの設計判断", kind="project_decision", scope="project", project_id=project_a)
        snapshot_a = self.service.snapshot(project_id=project_a)
        snapshot_b = self.service.snapshot(project_id=project_b)
        self.assertIn("全案件の必須条件", snapshot_a["constraints"])
        self.assertIn("案件Aだけの禁止", snapshot_a["constraints"])
        self.assertIn("全案件の必須条件", snapshot_b["constraints"])
        self.assertNotIn("案件Aだけの禁止", snapshot_b["constraints"])
        self.assertNotIn("案件Aだけの設計判断", snapshot_a["profile"])
        self.assertNotIn("案件Aだけの設計判断", snapshot_b["profile"])

    def test_pending_candidates_are_visible_as_confirmation_material_only(self) -> None:
        pending = self.service.remember("確認待ちの候補", evidence_kind="extracted")
        snapshot = self.service.snapshot(include_pending=True)
        self.assertNotIn("確認待ちの候補", snapshot["profile"])
        self.assertNotIn("確認待ちの候補", snapshot["constraints"])
        self.assertIn("確認待ちの候補", snapshot["context_text"])
        self.assertTrue(any(item["id"] == pending.memory_id for item in snapshot["pending"]))


class SearchScopeTests(unittest.TestCase):
    """#36: unadopted memories and excerpts are searchable, never injected."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_activity_summary_is_searchable_but_not_injected(self) -> None:
        activity = self.service.remember(
            "固有の活動記録キーワードを残した",
            kind="activity_summary",
            activity={
                "date": "2026-11-01",
                "overview": "検索対象の確認",
                "achievements": ["検索を広げた"],
                "decisions": [],
                "remaining": [],
            },
            adoption="activity_summary",
        )
        self.assertEqual(activity.status, "adopted")
        hits = self.service.search("固有の活動記録キーワード")
        self.assertTrue(any(hit["memory_id"] == activity.memory_id for hit in hits))
        snapshot = self.service.snapshot(query="固有の活動記録キーワード")
        self.assertNotIn("固有の活動記録キーワード", snapshot["context_text"])

    def record_excerpt(self, session_id: str, text: str) -> None:
        self.service.record_turn(
            session_id=session_id,
            project_id=None,
            turn=1,
            user_text=text,
            assistant_text="了解しました。",
            user_message_id=f"{session_id}-u1",
            assistant_message_id=f"{session_id}-a1",
            excerpts=[
                {"role": "user", "message_id": f"{session_id}-u1", "start": 0, "end": len(text), "text": text}
            ],
        )

    def test_excerpt_is_searchable_only_in_its_own_session(self) -> None:
        self.record_excerpt("sess_scope_a", "固有の抜粋キーワードを含む本文です。")
        hits = self.service.search("固有の抜粋キーワード", session_id="sess_scope_a")
        self.assertTrue(any(hit.get("hit_type") == "excerpt" for hit in hits))
        other = self.service.search("固有の抜粋キーワード", session_id="sess_scope_b")
        self.assertEqual(other, [])
        snapshot = self.service.snapshot(session_id="sess_scope_a")
        self.assertNotIn("固有の抜粋キーワード", snapshot["context_text"])

    def test_excerpt_is_excluded_when_scopes_excludes_session(self) -> None:
        self.record_excerpt("sess_scope_c", "範囲指定で除外する抜粋キーワード。")
        hits = self.service.search("範囲指定で除外する抜粋キーワード", scopes=["personal"], session_id="sess_scope_c")
        self.assertFalse(any(hit.get("hit_type") == "excerpt" for hit in hits))

    def test_deleted_source_excerpt_is_not_searchable(self) -> None:
        self.record_excerpt("sess_scope_d", "削除対象の抜粋キーワード。")
        memory = self.service.remember(
            "削除対象の記憶",
            source_refs=[{"session_id": "sess_scope_d", "message_id": "sess_scope_d-u1", "start": 0, "end": 10}],
        )
        self.service.delete(memory.memory_id, confirm=True)
        hits = self.service.search("削除対象の抜粋キーワード", session_id="sess_scope_d")
        self.assertFalse(any(hit.get("hit_type") == "excerpt" for hit in hits))


if __name__ == "__main__":
    unittest.main()
