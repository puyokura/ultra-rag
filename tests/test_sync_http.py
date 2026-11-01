from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from contracts.config import Layout, api_token, ensure_initialised, load_config, save_config
from contracts.models import Proposal
from rag.http_server import RagHttpServer
from rag.service import RagService
from rag.sync import sync_once, sync_pull, sync_push, sync_status
from tests.helpers import new_layout


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.server_layout = new_layout(self.base / "server")
        self.server_service = RagService(self.server_layout)
        self.token = api_token(self.server_layout)
        self.server = RagHttpServer(self.server_layout, port=0)
        self.server.start_background()
        self.url = f"http://127.0.0.1:{self.server.port}"

        self.client_layout = new_layout(self.base / "client", role="client")
        self.client_service = RagService(self.client_layout)

    def tearDown(self) -> None:
        try:
            self.server.stop()
        except Exception:
            pass
        self.temp.cleanup()

    def test_initial_pull_materializes_full_replica(self) -> None:
        self.server_service.remember("サーバー正本の記憶", kind="preference")
        result = sync_pull(self.client_layout, self.url, self.token)
        self.assertTrue(result["ok"])
        self.assertEqual(result["revision"], self.server_service.revision())
        self.assertEqual(self.client_service.verify()["revision"], self.server_service.revision())
        hits = self.client_service.search("サーバー正本")
        self.assertTrue(hits)

    def test_client_proposal_is_unsynced_then_pushed_and_pulled_back(self) -> None:
        sync_pull(self.client_layout, self.url, self.token)
        result = self.client_service.remember("クライアントで作った記憶", kind="preference")
        self.assertEqual(result.status, "unsynced")
        self.assertEqual(sync_status(self.client_layout)["unsynced"], 1)
        pushed = sync_push(self.client_layout, self.url, self.token)
        self.assertTrue(pushed["ok"])
        self.assertEqual(pushed["pushed"], 1)
        self.assertEqual(sync_status(self.client_layout)["unsynced"], 0)
        self.assertTrue(self.server_service.search("クライアントで作った記憶"))
        pull = sync_pull(self.client_layout, self.url, self.token)
        self.assertTrue(pull["ok"])
        self.assertTrue(self.client_service.search("クライアントで作った記憶"))

    def test_client_turn_payload_is_pushed_to_server(self) -> None:
        sync_pull(self.client_layout, self.url, self.token)
        result = self.client_service.record_turn(
            session_id="sess_sync_turn_0001",
            project_id=None,
            turn=1,
            user_text="今後の方針として、実測を優先する。",
            assistant_text="了解しました。",
            user_message_id="msg-user-1",
            assistant_message_id="msg-assistant-1",
        )
        self.assertFalse(result["saved"])
        self.assertEqual(sync_status(self.client_layout)["unsynced"], 1)
        pushed = sync_push(self.client_layout, self.url, self.token)
        self.assertTrue(pushed["ok"])
        self.assertEqual(sync_status(self.client_layout)["unsynced"], 0)
        reference = self.server_service.reference(session_id="sess_sync_turn_0001")
        self.assertTrue(reference["available"])
        self.assertEqual(reference["count"], 1)

    def test_offline_keeps_queue_and_then_recovers(self) -> None:
        sync_pull(self.client_layout, self.url, self.token)
        self.server.stop()
        queued = self.client_service.remember("通信断中に記録した方針", kind="requirement")
        self.assertEqual(queued.status, "unsynced")
        offline = sync_pull(self.client_layout, self.url, self.token)
        self.assertFalse(offline["ok"])
        self.assertTrue(offline["offline"])
        self.assertEqual(sync_status(self.client_layout)["unsynced"], 1)
        # Restart the authoritative store on a fresh port and recover.
        server2 = RagHttpServer(self.server_layout, port=0)
        server2.start_background()
        try:
            url2 = f"http://127.0.0.1:{server2.port}"
            result = sync_once(self.client_layout, url2, self.token)
            self.assertTrue(result["ok"], result)
            self.assertEqual(sync_status(self.client_layout)["unsynced"], 0)
            self.assertTrue(self.server_service.search("通信断中に記録した方針"))
        finally:
            server2.stop()

    def test_stale_update_is_kept_as_conflict(self) -> None:
        created = self.server_service.remember("競合させる方針", kind="preference")
        sync_pull(self.client_layout, self.url, self.token)
        revision = self.client_service.revision()
        proposal = Proposal(
            op="update",
            base_revision=revision,
            target_revision=revision,
            device_id=self.client_service.store.device_id,
            target_id=created.memory_id,
            statement="クライアント側の変更",
            evidence_kind="explicit_statement",
            adoption="owner_operation",
        )
        queued = self.client_service.propose(proposal)
        self.assertEqual(queued.status, "unsynced")
        self.server_service.update(
            created.memory_id,
            statement="サーバー側の変更",
            target_revision=revision,
        )
        pushed = sync_push(self.client_layout, self.url, self.token)
        self.assertFalse(pushed["ok"])
        self.assertEqual(pushed["conflicts"], 1)
        self.assertEqual(sync_status(self.client_layout)["unsynced"], 1)
        self.assertTrue(self.server_service.search("サーバー側の変更"))
        self.assertFalse(self.server_service.search("クライアント側の変更"))

    def test_deletion_propagates_and_blocks_queued_reimport(self) -> None:
        created = self.server_service.remember(
            "削除される記憶",
            kind="preference",
            source_refs=[{"session_id": "sess_sync_delete", "message_id": "msg1", "start": 0, "end": 12}],
        )
        sync_pull(self.client_layout, self.url, self.token)
        self.server_service.delete(created.memory_id, confirm=True)
        pull = sync_pull(self.client_layout, self.url, self.token)
        self.assertTrue(pull["ok"])
        self.assertIsNone(self.client_service.get_memory(created.memory_id))
        self.assertTrue(any(row["memory_id"] == created.memory_id for row in self.client_service.store.load().tombstones))
        replay = self.client_service.remember(
            "削除された記憶",
            evidence_kind="imported_statement",
            source_refs=[{"session_id": "sess_sync_delete", "message_id": "msg1", "start": 0, "end": 12}],
        )
        self.assertEqual(replay.status, "unsynced")
        pushed = sync_push(self.client_layout, self.url, self.token)
        self.assertEqual(pushed["rejected"], 1)
        self.assertEqual(sync_status(self.client_layout)["unsynced"], 0)


if __name__ == "__main__":
    unittest.main()
