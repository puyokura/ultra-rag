from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import build_session_file, new_layout
from rag.importer import import_all, import_session, redact_text
from rag.service import RagService


class ImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.layout = new_layout(self.base)
        self.service = RagService(self.layout)
        self.dsh_home = self.base / "dsh"
        self.session = build_session_file(
            self.dsh_home,
            "sess_import_0001",
            str(self.base / "project"),
            [
                {
                    "user": "設計より実測を優先する方針で進めます。\n\n今日の天気は晴れです。",
                    "assistant": "了解しました。以後、必ず実測します。",
                },
                {
                    "user": "APIキーは sk-abcdefghijklmnopqrstuvwxyz123456 を使っています。これを覚えて。",
                    "assistant": "秘密は保存しません。",
                },
            ],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_redaction_removes_common_secrets(self) -> None:
        text, edited = redact_text("token=supersecretvalue and sk-abcdefghijklmnopqrstuvwxyz")
        self.assertTrue(edited)
        self.assertNotIn("supersecretvalue", text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", text)
        self.assertIn("[REDACTED]", text)

    def test_dry_run_does_not_write(self) -> None:
        result = import_all(self.service, self.dsh_home, dry_run=True)
        self.assertEqual(result["turns"], 2)
        self.assertEqual(self.service.revision(), 1)
        self.assertFalse((self.layout.rag / "import" / "ledger.jsonl").exists())

    def test_import_records_excerpts_and_pending_candidates(self) -> None:
        result = import_all(self.service, self.dsh_home)
        self.assertEqual(result["sessions"], 1)
        self.assertGreaterEqual(result["turns"], 1)
        state = self.service.store.load()
        self.assertIn("sess_import_0001", state.sessions)
        session = state.sessions["sess_import_0001"]
        self.assertTrue(session.excerpts)
        for excerpt in session.excerpts:
            self.assertTrue(excerpt.text)
        candidates = self.service.list_candidates()
        self.assertTrue(candidates)
        self.assertTrue(all(candidate["status"] == "pending" for candidate in candidates))
        statements = [candidate["statement"] for candidate in candidates]
        self.assertTrue(any("実測を優先する" in statement for statement in statements))
        for statement in statements:
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", statement)

    def test_ledger_skips_unchanged_session(self) -> None:
        first = import_all(self.service, self.dsh_home)
        revision = self.service.revision()
        second = import_all(self.service, self.dsh_home)
        self.assertEqual(second["turns"], 0)
        self.assertEqual(self.service.revision(), revision)
        self.assertGreaterEqual(second["skip_reasons"].get("already imported", 0), 1)
        self.assertEqual(first["sessions"], 1)

    def test_imported_candidate_does_not_change_profile_before_confirmation(self) -> None:
        import_all(self.service, self.dsh_home)
        profile = self.service.snapshot()["profile"]
        self.assertNotIn("実測を優先する", profile)
        candidate = self.service.list_candidates()[0]
        self.service.confirm(candidate["id"], "adopt")
        self.assertIn(candidate["statement"], self.service.snapshot()["profile"])

    def test_redacted_excerpt_is_marked_edited(self) -> None:
        import_all(self.service, self.dsh_home)
        state = self.service.store.load()
        session = state.sessions["sess_import_0001"]
        edited = [excerpt for excerpt in session.excerpts if excerpt.edited]
        self.assertTrue(edited)
        self.assertTrue(any("[REDACTED]" in excerpt.text for excerpt in edited))

    def test_explicit_reference_and_explain_expose_stored_excerpts(self) -> None:
        import_all(self.service, self.dsh_home)
        candidate = self.service.list_candidates()[0]
        explained = self.service.explain(candidate["id"])
        self.assertTrue(explained["available"])
        self.assertTrue(explained["evidence"])
        self.assertTrue(any(item.get("available") for item in explained["evidence"]))
        reference = self.service.reference(session_id="sess_import_0001")
        self.assertTrue(reference["available"])
        self.assertEqual(reference["sessions"][0]["session_id"], "sess_import_0001")
        self.assertTrue(reference["sessions"][0]["excerpts"])
        filtered = self.service.reference(session_id="sess_import_0001", query="実測")
        self.assertTrue(filtered["sessions"][0]["excerpts"])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", json.dumps(reference, ensure_ascii=False))
        # Forgotten memories remain available only through explicit session reference.
        self.service.confirm(candidate["id"], "adopt")
        self.service.forget(candidate["id"])
        self.assertEqual(self.service.search(candidate["statement"]), [])
        self.assertTrue(self.service.reference(session_id="sess_import_0001")["sessions"][0]["excerpts"])

    def test_subagent_session_is_skipped_with_reason(self) -> None:
        hide = self.dsh_home / "sessions" / "sub"
        file = build_session_file(
            hide,
            "sess_subagent_0001",
            str(self.base),
            [{"user": "内部の作業です。", "assistant": "完了。"}],
            delegation_depth=1,
        )
        # build_session_file already places it under sessions/encoded; use the returned path directly.
        result = import_session(self.service, file, self.dsh_home)
        self.assertEqual(result["skipped"], 1)
        self.assertIn("subagent", next(iter(result["skip_reasons"])))


if __name__ == "__main__":
    unittest.main()
