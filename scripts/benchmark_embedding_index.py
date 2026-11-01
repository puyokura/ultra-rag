#!/usr/bin/env python3
"""Measure index build cost for a local embedding model at a given scale.

This is a cost benchmark only: it repeats the evaluation documents to reach the
requested count. Quality must be measured with the labelled dataset instead.

    python scripts/benchmark_embedding_index.py --model cl-nagoya/ruri-v3-310m --documents 1000
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate_embeddings import (  # noqa: E402
    DATASET_PATH,
    PeakRss,
    directory_bytes,
    document_text,
    encode,
    load_dataset,
    model_family,
)


def build_documents(base: list[dict[str, str]], count: int) -> list[dict[str, str]]:
    documents: list[dict[str, str]] = []
    for index in range(count):
        source = base[index % len(base)]
        documents.append(
            {
                "id": f"scale-{index:05d}",
                "title": source["title"],
                "text": f"{source['text']}（参考{index}）",
            }
        )
    return documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--documents", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    from huggingface_hub import snapshot_download
    from sentence_transformers import SentenceTransformer

    base, _ = load_dataset(args.dataset)
    documents = build_documents(base, args.documents)
    family = model_family(args.model)
    texts = [document_text(document, family) for document in documents]

    snapshot = Path(snapshot_download(args.model))
    model_bytes = directory_bytes(snapshot)
    with PeakRss() as memory:
        started = time.perf_counter()
        model = SentenceTransformer(args.model, device=args.device)
        load_seconds = time.perf_counter() - started
        started = time.perf_counter()
        vectors = encode(
            model,
            texts,
            family=family,
            is_query=False,
            batch_size=args.batch_size,
        )
        index_seconds = time.perf_counter() - started
    vector_bytes = int(np.asarray(vectors, dtype=np.float32).nbytes)
    report = {
        "model": args.model,
        "documents": len(documents),
        "dimension": int(vectors.shape[1]),
        "load_seconds": load_seconds,
        "index_seconds": index_seconds,
        "per_document_ms": (index_seconds / len(documents)) * 1000,
        "vector_bytes": vector_bytes,
        "model_bytes": model_bytes,
        "peak_rss_bytes": memory.peak,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "device": args.device,
            "batch_size": args.batch_size,
        },
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"{args.model}: docs={len(documents)} dim={report['dimension']} "
        f"load={load_seconds:.1f}s index={index_seconds:.1f}s "
        f"per_doc={report['per_document_ms']:.1f}ms "
        f"vectors={vector_bytes / 1048576:.2f}MiB "
        f"peakRSS={memory.peak / 1048576:.0f}MiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
