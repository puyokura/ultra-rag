#!/usr/bin/env python3
"""Measure a local embedding model against the existing literal search path.

This is an evaluation harness, not runtime code. It builds an isolated temporary
memory store (never the production home), indexes a small Japanese dataset, and
compares Recall@1, Recall@5 and MRR@5 with cosine similarity over local vectors.

Run manually on the machine that would host the model:

    python scripts/evaluate_embeddings.py --model google/embeddinggemma-2

The default search path stays literal; this script never enables embeddings.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for entry in ("", "secret-core", "key-use", "password-input", "cli"):
    candidate = str(ROOT / entry) if entry else str(ROOT)
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from contracts.config import Layout, api_token, ensure_initialised, save_config
from rag.service import RagService

DATASET_PATH = Path(__file__).with_name("embedding_eval_dataset.json")


def load_dataset(path: Path) -> tuple[list[dict[str, str]], dict[str, list[dict[str, str]]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    documents = payload["documents"]
    query_sets = {"natural": payload["queries"], "keyword": payload["queries_keyword"]}
    identifiers = [document["id"] for document in documents]
    if len(identifiers) != len(set(identifiers)):
        raise SystemExit("dataset has duplicate document ids")
    known = set(identifiers)
    for name, queries in query_sets.items():
        if not queries:
            raise SystemExit(f"query set is empty: {name}")
        for item in queries:
            if item["target"] not in known:
                raise SystemExit(f"query target does not exist: {item['target']}")
    return documents, query_sets


def rank_metrics(rankings: list[list[str]], targets: list[str], k: int = 5) -> dict[str, float]:
    reciprocal: list[float] = []
    hits_at_1 = 0
    hits_at_k = 0
    for ranking, target in zip(rankings, targets, strict=True):
        try:
            rank = ranking.index(target) + 1
        except ValueError:
            rank = None
        if rank == 1:
            hits_at_1 += 1
        if rank is not None and rank <= k:
            hits_at_k += 1
        reciprocal.append(1.0 / rank if rank else 0.0)
    total = len(targets)
    return {
        "recall_at_1": hits_at_1 / total if total else 0.0,
        f"recall_at_{k}": hits_at_k / total if total else 0.0,
        f"mrr_at_{k}": statistics.fmean(reciprocal) if reciprocal else 0.0,
    }


class PeakRss:
    """Sample resident memory while a model loads and encodes."""

    def __init__(self) -> None:
        try:
            import psutil
        except ImportError:  # pragma: no cover - optional dependency
            self.process = None
        else:
            self.process = psutil.Process()
        self.peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "PeakRss":
        if self.process is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, self.process.memory_info().rss)
            self._stop.wait(0.05)

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def literal_evaluation(
    documents: list[dict[str, str]], query_sets: dict[str, list[dict[str, str]]], limit: int
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ultra-rag-eval-") as temporary:
        layout = Layout.at(Path(temporary) / "home")
        config = ensure_initialised(layout)
        config["role"] = "server"
        save_config(layout, config)
        api_token(layout, create=True)
        service = RagService(layout)
        service.initialise()
        mapping: dict[str, str] = {}
        started = time.perf_counter()
        for document in documents:
            result = service.remember(f"{document['title']}。{document['text']}", kind="lesson")
            mapping[result.memory_id] = document["id"]
        index_seconds = time.perf_counter() - started
        rankings: dict[str, list[list[str]]] = {}
        search_seconds: dict[str, float] = {}
        for name, queries in query_sets.items():
            started = time.perf_counter()
            rows: list[list[str]] = []
            for item in queries:
                hits = service.search(item["query"], limit=limit)
                rows.append([mapping[hit["memory_id"]] for hit in hits if hit["memory_id"] in mapping])
            rankings[name] = rows
            search_seconds[name] = time.perf_counter() - started
        storage_bytes = directory_bytes(Path(temporary))
    return {
        "rankings": rankings,
        "index_seconds": index_seconds,
        "search_seconds": search_seconds,
        "storage_bytes": storage_bytes,
    }


def model_family(model_id: str) -> str:
    lowered = model_id.lower()
    if "embeddinggemma" in lowered:
        return "gemma"
    if "multilingual-e5" in lowered or lowered.endswith("/e5-small") or "e5-" in lowered:
        return "e5"
    return "plain"


def document_text(document: dict[str, str], family: str) -> str:
    if family == "gemma":
        return f"title: {document['title']} | text: {document['text']}"
    return f"{document['title']}。{document['text']}"


def encode(
    model: Any, texts: list[str], *, family: str, is_query: bool, batch_size: int
) -> "np.ndarray":
    import numpy as np

    options = {
        "normalize_embeddings": True,
        "batch_size": batch_size,
        "convert_to_numpy": True,
        "show_progress_bar": False,
    }
    if family == "gemma":
        prompt = "SearchQuery" if is_query else "Document"
        try:
            return np.asarray(model.encode(texts, prompt_name=prompt, **options), dtype=np.float32)
        except Exception:  # older sentence-transformers without the prompt config
            pass
    if family == "e5":
        prefix = "query: " if is_query else "passage: "
        return np.asarray(model.encode([prefix + text for text in texts], **options), dtype=np.float32)
    return np.asarray(model.encode(texts, **options), dtype=np.float32)


def embedding_evaluation(
    model_id: str,
    documents: list[dict[str, str]],
    query_sets: dict[str, list[dict[str, str]]],
    *,
    device: str,
    batch_size: int,
    limit: int,
) -> dict[str, Any]:
    try:
        from huggingface_hub import snapshot_download
        from sentence_transformers import SentenceTransformer
    except ImportError as error:
        raise SystemExit(
            "sentence-transformers and huggingface_hub are required for this harness: " + str(error)
        ) from error

    import numpy as np

    family = model_family(model_id)
    baseline_rss = 0
    try:
        import psutil

        baseline_rss = psutil.Process().memory_info().rss
    except ImportError:
        pass

    started = time.perf_counter()
    snapshot = Path(snapshot_download(model_id))
    download_seconds = time.perf_counter() - started
    model_bytes = directory_bytes(snapshot)

    with PeakRss() as memory:
        started = time.perf_counter()
        model = SentenceTransformer(model_id, device=device)
        load_seconds = time.perf_counter() - started
        document_vectors = encode(
            model,
            [document_text(document, family) for document in documents],
            family=family,
            is_query=False,
            batch_size=batch_size,
        )
        index_seconds = time.perf_counter() - started - load_seconds

        rankings: dict[str, list[list[str]]] = {}
        query_seconds: dict[str, float] = {}
        for name, queries in query_sets.items():
            started = time.perf_counter()
            rows: list[list[str]] = []
            for item in queries:
                query_vector = encode(
                    model, [item["query"]], family=family, is_query=True, batch_size=1
                )[0]
                scores = document_vectors @ query_vector
                order = np.argsort(-scores)[:limit]
                rows.append([documents[int(index)]["id"] for index in order])
            rankings[name] = rows
            query_seconds[name] = time.perf_counter() - started

    vector_bytes = int(document_vectors.astype(np.float32).nbytes)
    return {
        "rankings": rankings,
        "download_seconds": download_seconds,
        "load_seconds": load_seconds,
        "index_seconds": index_seconds,
        "query_seconds": query_seconds,
        "query_ms_per_query": {
            name: (seconds / len(query_sets[name])) * 1000
            for name, seconds in query_seconds.items()
        },
        "vector_bytes": vector_bytes,
        "model_bytes": model_bytes,
        "dimension": int(document_vectors.shape[1]),
        "peak_rss_bytes": memory.peak,
        "baseline_rss_bytes": baseline_rss,
        "prompts": sorted(getattr(model, "prompts", {}) or {}),
    }


def summarize(
    literal: dict[str, Any],
    embedding: dict[str, Any],
    query_sets: dict[str, list[dict[str, str]]],
    k: int,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    literal_ranks: dict[str, list[int]] = {}
    embedding_ranks: dict[str, list[int]] = {}
    for name, queries in query_sets.items():
        targets = [item["target"] for item in queries]
        metrics[name] = {
            "literal": rank_metrics(literal["rankings"][name], targets, k),
            "embedding": rank_metrics(embedding["rankings"][name], targets, k),
        }
        literal_ranks[name] = [
            0 if target not in ranking else ranking.index(target) + 1
            for ranking, target in zip(literal["rankings"][name], targets, strict=True)
        ]
        embedding_ranks[name] = [
            0 if target not in ranking else ranking.index(target) + 1
            for ranking, target in zip(embedding["rankings"][name], targets, strict=True)
        ]
    return {"metrics": metrics, "literal_ranks": literal_ranks, "embedding_ranks": embedding_ranks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Hugging Face model id")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--output", type=Path, help="write the full report as JSON")
    args = parser.parse_args(argv)

    documents, query_sets = load_dataset(args.dataset)
    literal = literal_evaluation(documents, query_sets, args.limit)
    embedding = embedding_evaluation(
        args.model,
        documents,
        query_sets,
        device=args.device,
        batch_size=args.batch_size,
        limit=args.limit,
    )
    metrics = summarize(literal, embedding, query_sets, args.limit)

    per_query: dict[str, list[dict[str, object]]] = {}
    for name, queries in query_sets.items():
        per_query[name] = [
            {
                "query": item["query"],
                "target": item["target"],
                "literal_rank": literal_rank,
                "embedding_rank": embedding_rank,
            }
            for item, literal_rank, embedding_rank in zip(
                queries,
                metrics["literal_ranks"][name],
                metrics["embedding_ranks"][name],
                strict=True,
            )
        ]

    report = {
        "model": args.model,
        "model_family": model_family(args.model),
        "dataset": {
            "path": str(args.dataset),
            "documents": len(documents),
            "queries": {name: len(queries) for name, queries in query_sets.items()},
            "limit": args.limit,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "device": args.device,
        },
        "literal": {key: value for key, value in literal.items() if key != "rankings"},
        "embedding": {key: value for key, value in embedding.items() if key != "rankings"},
        "metrics": metrics["metrics"],
        "per_query": per_query,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"model: {args.model} (dim={embedding['dimension']}, family={report['model_family']})")
    print(
        f"documents={len(documents)} "
        + " ".join(f"{name}={len(queries)}" for name, queries in query_sets.items())
        + f" limit={args.limit}"
    )
    print("set       method      R@1     R@5     MRR@5")
    for name in query_sets:
        lit = report["metrics"][name]["literal"]
        emb = report["metrics"][name]["embedding"]
        print(f"{name:<9} literal   {lit['recall_at_1']:.3f}   {lit['recall_at_5']:.3f}   {lit['mrr_at_5']:.3f}")
        print(f"{name:<9} embedding {emb['recall_at_1']:.3f}   {emb['recall_at_5']:.3f}   {emb['mrr_at_5']:.3f}")
    query_ms = ", ".join(
        f"{name}={milliseconds:.1f}ms"
        for name, milliseconds in embedding["query_ms_per_query"].items()
    )
    print(
        "cost: "
        f"vectors={embedding['vector_bytes'] / 1024:.1f} KiB "
        f"model={embedding['model_bytes'] / 1048576:.1f} MiB "
        f"load={embedding['load_seconds']:.1f}s "
        f"index={embedding['index_seconds']:.1f}s "
        f"query=[{query_ms}] "
        f"peakRSS={embedding['peak_rss_bytes'] / 1048576:.0f}MiB "
        f"literal_index={literal['index_seconds']:.2f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
