"""Optional local embedding backend.

Embeddings are a rebuildable derivative: the confirmed revision stays the source
of truth, and every failure falls back to literal search. Nothing here runs
unless the configuration explicitly enables it.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, Sequence


class EmbeddingUnavailable(RuntimeError):
    """The model or its runtime cannot be used on this host."""


class Embedder(Protocol):
    model_id: str
    prompt_id: str
    dim: int

    def encode_documents(self, texts: Sequence[str]) -> Any:
        ...

    def encode_query(self, text: str) -> Any:
        ...


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SentenceTransformerEmbedder:
    """ruri-v3 and compatible sentence-transformers checkpoints on local CPU."""

    prompt_id = "none"

    def __init__(self, model_id: str, device: str = "cpu", batch_size: int = 16) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:  # optional dependency
            raise EmbeddingUnavailable(
                "sentence-transformers is not installed; literal search stays active"
            ) from error
        try:
            self._model = SentenceTransformer(model_id, device=device)
            dimension = getattr(self._model, "get_embedding_dimension", None)
            self.dim = int(
                dimension() if callable(dimension) else self._model.get_sentence_embedding_dimension()
            )
        except Exception as error:  # model download, licence, memory, ...
            raise EmbeddingUnavailable(f"embedding model unavailable: {error}") from error
        self.model_id = model_id
        self.batch_size = max(1, int(batch_size))

    def _encode(self, texts: Sequence[str]) -> Any:
        import numpy as np

        vectors = self._model.encode(
            list(texts),
            normalize_embeddings=True,
            batch_size=self.batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        array = np.asarray(vectors, dtype=np.float32)
        if array.ndim == 1:
            array = array.reshape(1, -1)
        if array.shape[1] != self.dim:
            raise EmbeddingUnavailable("model returned an unexpected embedding dimension")
        return array

    def encode_documents(self, texts: Sequence[str]) -> Any:
        return self._encode(texts)

    def encode_query(self, text: str) -> Any:
        return self._encode([text])[0]
