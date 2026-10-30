from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from contracts.config import api_token
from rag.http_server import RagHttpServer
from rag.service import RagService
from tests.helpers import new_layout


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        self.token = api_token(self.layout)
        self.server = RagHttpServer(self.layout, port=0)
        self.server.start_background()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.temp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_health_without_token_and_data_requires_token(self) -> None:
        status, payload = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, payload = self.request("GET", "/v1/revision")
        self.assertEqual(status, 401)
        status, payload = self.request("GET", "/v1/revision", token="wrong")
        self.assertEqual(status, 401)
        status, payload = self.request("GET", "/v1/revision", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["revision"], 1)

    def test_snapshot_and_remember_round_trip(self) -> None:
        status, payload = self.request(
            "POST",
            "/v1/remember",
            {"statement": "HTTP経由の記憶", "kind": "preference", "scope": "personal"},
            token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "adopted")
        status, payload = self.request("POST", "/v1/snapshot", {"query": "HTTP経由"}, token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("HTTP経由の記憶", payload["context_text"])

    def test_invalid_body_and_unknown_endpoint(self) -> None:
        request = urllib.request.Request(
            self.base + "/v1/snapshot",
            data=b"not-json",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("invalid body was accepted")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 400)
        status, payload = self.request("GET", "/v1/nope", token=self.token)
        self.assertEqual(status, 404)
        status, payload = self.request(
            "POST",
            "/v1/sync/apply",
            {
                "revision": 1,
                "files": [],
                "manifest_files": {},
                "deleted_paths": [],
                "tombstones": [],
                "manifest_sha256": "deadbeef",
                "full": True,
                "created_at": "2026-01-01T00:00:00Z",
            },
            token=self.token,
        )
        self.assertEqual(status, 409)

    def test_parallel_snapshots_are_consistent(self) -> None:
        self.service.remember("並列検索の対象", kind="preference")
        def call(_index: int):
            return self.request("POST", "/v1/snapshot", {"query": "並列検索"}, token=self.token)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(call, range(16)))
        self.assertTrue(all(status == 200 for status, _ in results))
        self.assertTrue(all("並列検索の対象" in payload["context_text"] for _, payload in results))


if __name__ == "__main__":
    unittest.main()
