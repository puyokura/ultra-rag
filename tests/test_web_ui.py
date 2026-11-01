from __future__ import annotations

import json
import shutil
import subprocess
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

    def test_page_is_self_contained(self) -> None:
        status, headers, body = self.fetch("/ui")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))
        text = body.decode("utf-8")
        self.assertIn("記憶台帳", text)
        self.assertIn("navigator.clipboard", text)
        self.assertIn("活動記録", text)
        self.assertIn("overflow-wrap", text)
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

    def post(self, path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_decision_requires_the_ui_header(self) -> None:
        status, _ = self.post("/ui/decide", {"candidate_id": self.candidate.memory_id, "decision": "adopt"})
        self.assertEqual(status, 403)

    def test_pending_candidates_can_be_adopted_or_rejected(self) -> None:
        other = self.service.remember("却下する候補", adoption=None)
        status, body = self.post(
            "/ui/decide",
            {"candidate_id": self.candidate.memory_id, "decision": "adopt"},
            {"X-Ultra-RAG-UI": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "adopted")
        status, body = self.post(
            "/ui/decide",
            {"candidate_id": other.memory_id, "decision": "reject"},
            {"X-Ultra-RAG-UI": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "rejected")
        _, headers, raw = self.fetch("/ui/data")
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["counts"]["candidates"], 0)

    def test_activity_summary_is_visible_in_its_own_view(self) -> None:
        activity = self.service.remember(
            "【活動記録】UI確認",
            kind="activity_summary",
            activity={
                "date": "2026-11-01",
                "overview": "UI確認をした",
                "achievements": ["活動タブを追加"],
                "decisions": [],
                "remaining": ["ページング"],
            },
            adoption="activity_summary",
        )
        _, _, raw = self.fetch("/ui/data")
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["counts"]["activities"], 1)
        self.assertEqual(data["activities"][0]["id"], activity.memory_id)
        self.assertEqual(data["activities"][0]["activity"]["remaining"], ["ページング"])

    @unittest.skipUnless(shutil.which("node"), "node is required to syntax-check the inline script")
    def test_inline_script_parses(self) -> None:
        _, _, body = self.fetch("/ui")
        text = body.decode("utf-8")
        script = text.split("<script>", 1)[1].split("</script>", 1)[0]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(script)
            path = handle.name
        try:
            result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_api_still_requires_the_bearer_token(self) -> None:
        status, _, _ = self.fetch("/v1/revision")
        self.assertEqual(status, 401)
        status, _, _ = self.fetch("/ui/data", method="POST")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
