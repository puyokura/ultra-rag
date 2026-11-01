from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from rag.embeddings import EmbeddingUnavailable
from rag.search import SearchIndex
from rag.service import RagService
from tests.helpers import new_layout


class FakeEmbedder:
    prompt_id = "none"

    def __init__(
        self,
        model_id: str = "fake-v1",
        dim: int = 2,
        query_vector: list[float] | None = None,
    ) -> None:
        self.model_id = model_id
        self.dim = dim
        self.query_vector = np.asarray(query_vector or [1.0, 0.0], dtype=np.float32)
        self.document_calls: list[list[str]] = []

    def encode_documents(self, texts):
        self.document_calls.append(list(texts))
        vectors = []
        for text in texts:
            if "対象" in text:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return np.asarray(vectors, dtype=np.float32)

    def encode_query(self, text):
        return self.query_vector


class EmbeddingSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.layout = new_layout(Path(self.temp.name))
        self.base = RagService(self.layout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def service_with(self, embedder, enabled: bool = True) -> tuple[RagService, SearchIndex]:
        index = SearchIndex(
            self.layout.rag / "index" / "search.sqlite",
            self.base.store,
            embedding_enabled=enabled,
            embedder_factory=lambda: embedder,
        )
        return RagService(self.layout, index=index), index

    def test_disabled_path_never_loads_a_model(self) -> None:
        def explode():
            raise AssertionError("embedder must not load when disabled")

        index = SearchIndex(
            self.layout.rag / "index" / "search.sqlite",
            self.base.store,
            embedding_enabled=False,
            embedder_factory=explode,
        )
        service = RagService(self.layout, index=index)
        service.remember("再現してから修正する", kind="lesson")
        hits = service.search("再現")
        self.assertTrue(hits)
        self.assertEqual(index.embedding_status(), {"enabled": False})

    def test_semantic_only_hit_is_returned(self) -> None:
        service, _ = self.service_with(FakeEmbedder(query_vector=[1.0, 0.0]))
        target = service.remember("対象となる記憶です", kind="lesson")
        service.remember("別の記憶です", kind="lesson")
        hits = service.search("合図")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["memory_id"], target.memory_id)
        self.assertEqual(hits[0]["hit_type"], "semantic")

    def test_missing_model_falls_back_to_literal(self) -> None:
        def unavailable():
            raise EmbeddingUnavailable("model not installed")

        index = SearchIndex(
            self.layout.rag / "index" / "search.sqlite",
            self.base.store,
            embedding_enabled=True,
            embedder_factory=unavailable,
        )
        service = RagService(self.layout, index=index)
        target = service.remember("再現してから修正する", kind="lesson")
        hits = service.search("再現")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["memory_id"], target.memory_id)
        self.assertIsNotNone(index.embedding_status()["error"])

    def test_incremental_build_only_embeds_changed_rows(self) -> None:
        embedder = FakeEmbedder()
        service, _ = self.service_with(embedder)
        service.remember("対象となる記憶です", kind="lesson")
        first = service.build_embeddings()
        self.assertTrue(first["ok"])
        self.assertEqual(len(embedder.document_calls[-1]), 1)

        service.remember("別の記憶です", kind="lesson")
        service.search("対象")
        self.assertEqual(len(embedder.document_calls[-1]), 1)
        self.assertIn("別の記憶です", embedder.document_calls[-1][0])

    def test_model_change_rebuilds_every_vector(self) -> None:
        first = FakeEmbedder(model_id="fake-v1")
        service, _ = self.service_with(first)
        service.remember("対象となる記憶です", kind="lesson")
        service.remember("別の記憶です", kind="lesson")
        service.build_embeddings()
        self.assertEqual(len(first.document_calls[-1]), 2)

        second = FakeEmbedder(model_id="fake-v2")
        service2, _ = self.service_with(second)
        service2.search("合図")
        self.assertEqual(len(second.document_calls[-1]), 2)

    def test_forgotten_memory_is_removed_from_vectors(self) -> None:
        service, index = self.service_with(FakeEmbedder(query_vector=[1.0, 0.0]))
        target = service.remember("対象となる記憶です", kind="lesson")
        service.remember("別の記憶です", kind="lesson")
        service.build_embeddings()
        service.forget(target.memory_id)
        hits = service.search("合図")
        self.assertTrue(all(hit["memory_id"] != target.memory_id for hit in hits))
        connection = sqlite3.connect(index.path)
        try:
            rows = connection.execute(
                "SELECT source_id FROM embeddings WHERE source_kind='memory'"
            ).fetchall()
        finally:
            connection.close()
        self.assertNotIn(target.memory_id, {row[0] for row in rows})

    def test_index_stores_no_plaintext_column(self) -> None:
        service, index = self.service_with(FakeEmbedder())
        service.remember("秘密ではない対象の記憶", kind="lesson")
        service.build_embeddings()
        connection = sqlite3.connect(index.path)
        try:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(embeddings)")]
        finally:
            connection.close()
        self.assertIn("vec", columns)
        self.assertNotIn("text", columns)
        self.assertNotIn("statement", columns)


if __name__ == "__main__":
    unittest.main()
