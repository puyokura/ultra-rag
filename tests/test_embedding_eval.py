from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_embeddings.py"


def load_module():
    spec = importlib.util.spec_from_file_location("evaluate_embeddings", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DatasetTests(unittest.TestCase):
    def test_dataset_is_labelled_and_complete(self) -> None:
        module = load_module()
        documents, query_sets = module.load_dataset(ROOT / "scripts" / "embedding_eval_dataset.json")
        identifiers = {document["id"] for document in documents}
        self.assertEqual(len(documents), 30)
        self.assertEqual(len(identifiers), 30)
        self.assertEqual(set(query_sets), {"natural", "keyword"})
        for name, queries in query_sets.items():
            self.assertEqual(len(queries), 30, name)
            self.assertEqual({item["target"] for item in queries}, identifiers, name)
            self.assertEqual(len({item["query"] for item in queries}), len(queries), name)
            self.assertTrue(all(item["query"].strip() for item in queries), name)

    def test_natural_and_keyword_queries_differ(self) -> None:
        payload = json.loads((ROOT / "scripts" / "embedding_eval_dataset.json").read_text(encoding="utf-8"))
        natural = {item["query"] for item in payload["queries"]}
        keyword = {item["query"] for item in payload["queries_keyword"]}
        self.assertTrue(natural.isdisjoint(keyword))


class MetricTests(unittest.TestCase):
    def test_rank_metrics_counts_misses(self) -> None:
        module = load_module()
        rankings = [["a", "b", "c"], ["b", "a"], ["c", "b", "a"]]
        metrics = module.rank_metrics(rankings, ["a", "a", "a"], k=5)
        self.assertAlmostEqual(metrics["recall_at_1"], 1 / 3)
        self.assertAlmostEqual(metrics["recall_at_5"], 1.0)
        self.assertAlmostEqual(metrics["mrr_at_5"], (1 + 0.5 + 1 / 3) / 3)

    def test_missing_target_scores_zero(self) -> None:
        module = load_module()
        metrics = module.rank_metrics([["b", "c"]], ["a"], k=5)
        self.assertEqual(metrics["recall_at_1"], 0.0)
        self.assertEqual(metrics["recall_at_5"], 0.0)
        self.assertEqual(metrics["mrr_at_5"], 0.0)


class FallbackTests(unittest.TestCase):
    def test_literal_search_runs_without_embedding_packages(self) -> None:
        script = textwrap.dedent(
            """
            import os
            import sys

            class Blocker:
                def find_spec(self, name, path=None, target=None):
                    root = name.split(".")[0]
                    if root in {"sentence_transformers", "torch", "transformers"}:
                        raise ImportError("blocked for fallback test: " + name)
                    return None

            sys.meta_path.insert(0, Blocker())
            root = sys.argv[1]
            sys.path[:0] = [root] + [
                os.path.join(root, name)
                for name in ("secret-core", "key-use", "password-input", "cli")
            ]

            import pathlib
            import tempfile

            from contracts.config import Layout, api_token, ensure_initialised, save_config
            from rag.service import RagService

            with tempfile.TemporaryDirectory() as temporary:
                layout = Layout.at(pathlib.Path(temporary) / "home")
                config = ensure_initialised(layout)
                config["role"] = "server"
                save_config(layout, config)
                api_token(layout, create=True)
                service = RagService(layout)
                service.initialise()
                service.remember("再現してから修正する", kind="lesson")
                hits = service.search("再現")
                if not hits:
                    raise SystemExit("literal search returned no hits")
            print("OK")
            """
        )
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            cwd=ROOT,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
