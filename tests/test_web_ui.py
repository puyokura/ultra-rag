from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from rag.http_server import RagHttpServer
from rag.service import RagService
from tests.helpers import new_layout

LONG = "長い方針の本文です。" * 40


class MemoryBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.service = RagService(self.layout)
        self.adopted = self.service.remember(LONG, kind="design_principle", conditions=["案件A"])
        self.candidate = self.service.remember(
            "確認待ちの候補", kind="preference", evidence_kind="extracted", adoption=None
        )
        self.server = RagHttpServer(self.layout, host="127.0.0.1", port=0).start_background()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.temp.cleanup()

    def fetch(self, path: str, method: str = "GET") -> tuple[int, dict, bytes]:
        request = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_page_is_self_contained_and_read_only(self) -> None:
        status, headers, body = self.fetch("/ui")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))
        text = body.decode("utf-8")
        self.assertIn("ultra-RAG memory", text)
        self.assertIn("navigator.clipboard", text)
        self.assertIn("word-break", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("<script src=", text)

    def test_data_has_full_text_and_no_write_surface(self) -> None:
        status, headers, body = self.fetch("/ui/data")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["revision"], self.service.revision())
        self.assertIn(LONG, json.dumps(data, ensure_ascii=False))
        ids = {item["id"] for item in data["memories"]}
        self.assertIn(self.adopted.memory_id, ids)
        self.assertIn(self.candidate.memory_id, ids)
        self.assertEqual(data["counts"]["candidates"], 1)
        self.assertIn("個人プロフィール", data["profile"])
        self.assertIn("禁止・必須事項", data["constraints"])

    def test_api_still_requires_the_bearer_token(self) -> None:
        status, _, _ = self.fetch("/v1/revision")
        self.assertEqual(status, 401)
        status, _, _ = self.fetch("/ui/data", method="POST")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
