from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from contracts.config import load_config, save_config
from contracts.models import MemoryRecord
from rag.profile import render_profile
from rag.service import RagService
from tests.helpers import new_layout


class CandidatePagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_candidates_are_paged_by_fifty_and_rejected_are_retained(self) -> None:
        ids: list[str] = []
        for index in range(55):
            result = self.service.remember(f"候補 {index}", kind="preference", evidence_kind="extracted")
            ids.append(result.memory_id or result.candidate_id)
        rejected = ids[0]
        self.service.confirm(rejected, decision="reject")
        first = self.service.candidate_page()
        self.assertEqual(first["total"], 55)
        self.assertEqual(first["page_size"], 50)
        self.assertEqual(len(first["items"]), 50)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_page"], 2)
        second = self.service.candidate_page(page=2)
        self.assertEqual(len(second["items"]), 5)
        self.assertFalse(second["has_more"])
        self.assertEqual(second["pages"], 2)
        combined = first["items"] + second["items"]
        self.assertEqual({item["id"] for item in combined}, set(ids))
        self.assertTrue(any(item["id"] == rejected and item["status"] == "rejected" for item in combined))
        third = self.service.candidate_page(page=3)
        self.assertEqual(third["items"], [])

    def test_profile_deduplicates_but_does_not_cap(self) -> None:
        records = []
        for index in range(2):
            records.append(
                MemoryRecord(
                    id="mem_" + str(index) * 32,
                    kind="preference",
                    scope="personal",
                    statement="同じ好み",
                    project_id=None,
                    status="adopted",
                    evidence_kind="explicit_statement",
                    source_refs=[],
                    revision=1,
                    recorded_at="2026-01-01T00:00:00Z",
                    effective_at="2026-01-01T00:00:00Z",
                )
            )
        profile = render_profile(records, 1)
        self.assertEqual(profile.count("同じ好み"), 1)

    def test_snapshot_reports_overflow_without_truncating(self) -> None:
        config = load_config(self.layout)
        config["rag"]["max_context_chars"] = 20
        save_config(self.layout, config)
        service = RagService(self.layout)
        service.remember("長い文脈でも省略しない", kind="preference")
        snapshot = service.snapshot()
        self.assertTrue(snapshot["overflow"])
        self.assertTrue(snapshot["split_required"])
        self.assertIn("長い文脈でも省略しない", snapshot["context_text"])
        self.assertGreater(snapshot["context_chars"], 20)
        self.assertIn("Split", snapshot["overflow_hint"])

    def test_excerpt_overflow_is_explicit(self) -> None:
        excerpts = [
            SimpleNamespace(text=f"exact excerpt {index}", to_dict=lambda index=index: {"text": f"exact excerpt {index}"})
            for index in range(60)
        ]
        session = SimpleNamespace(
            id="sess_" + "a" * 32,
            project_id=None,
            updated_at="2026-01-01T00:00:00Z",
            turns=[],
            excerpts=excerpts,
        )
        with mock.patch.object(self.service.store, "load", return_value=SimpleNamespace(sessions={session.id: session})):
            result = self.service.reference(session_id=session.id, limit=50)
        self.assertTrue(result["split_required"])
        self.assertEqual(result["excerpt_overflow"], 10)
        self.assertEqual(result["sessions"][0]["excerpts_total"], 60)
        self.assertTrue(result["sessions"][0]["excerpts_truncated"])
        self.assertEqual(result["sessions"][0]["excerpts"][0]["text"], "exact excerpt 0")
