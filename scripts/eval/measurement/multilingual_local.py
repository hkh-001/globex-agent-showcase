"""本地多语言检索评测：用 bge-m3 跑 v3 的 dense / hybrid，不改冻结 runner。

复用 v3 的池隔离（platform + corpus_language）、金标（relevant_canonical_ids）与
v2 的 score；不调用任何付费模型，reranker 不参与。
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import math
import pathlib
import statistics
from types import SimpleNamespace

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.seed_products import _product_from_record
from app.infrastructure.settings import load_settings
from scripts.eval.hard_constraints import find_hit_constraint_violations
from scripts.eval.retrieval_upgrade import paired_interval
from scripts.eval.retrieval_v2 import score
from scripts.generate_retrieval_multilingual import build_cases

ROOT = pathlib.Path(__file__).resolve().parents[3]


class StaticIndex:
    """内存余弦索引；search_filtered 模拟 qdrant 的 product_ids 预过滤。"""

    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors

    @staticmethod
    def _rank(embedding, vectors, top_n, allowed):
        import numpy as np
        query = np.asarray(embedding, dtype=np.float32)
        query_norm = float(np.linalg.norm(query)) or 1.0
        ids, matrix = [], []
        for pid, vec in vectors.items():
            if allowed is not None and pid not in allowed:
                continue
            ids.append(pid)
            matrix.append(vec)
        if not ids:
            return []
        scores = (np.asarray(matrix, dtype=np.float32) @ query) / (
            np.linalg.norm(np.asarray(matrix, dtype=np.float32), axis=1) * query_norm + 1e-12
        )
        order = np.argsort(-scores)[:top_n]
        return [SimpleNamespace(product_id=ids[i], score=float(scores[i])) for i in order]

    async def search(self, embedding, top_n):
        return self._rank(embedding, self._vectors, top_n, None)

    async def search_filtered(self, embedding, top_n, product_ids=None):
        return self._rank(embedding, self._vectors, top_n, set(product_ids) if product_ids else None)


class QueryEmbedder:
    def __init__(self, table: dict[str, list[float]]):
        self._table = table

    async def embed(self, text: str):
        return self._table[text]

    async def embed_batch(self, texts):
        return [self._table[text] for text in texts]


def _summarize(rows):
    return {"cases": len(rows), "recall": statistics.mean(r["recall"] for r in rows),
            "mrr": statistics.mean(r["mrr"] for r in rows), "ndcg": statistics.mean(r["ndcg"] for r in rows)}


async def evaluate(output: pathlib.Path, *, split: str) -> None:
    settings = load_settings()
    records = [json.loads(line) for line in (ROOT / "data/catalog-v3.jsonl").read_text().splitlines()]
    cases = [json.loads(line) for line in (ROOT / "eval/v3/multilingual_retrieval.jsonl").read_text().splitlines()]
    if cases != build_cases(records):
        raise SystemExit("评测集与冻结构建规则不一致，停止")
    products = [_product_from_record(record) for record in records]
    by_id = {p.product_id: p for p in products}

    embedder = OpenAIEmbeddingClient(settings)
    print(json.dumps({"stage": "embed_products", "n": len(products)}), flush=True)
    product_vectors = {}
    for start in range(0, len(products), 32):
        batch = products[start:start + 32]
        vectors = await embedder.embed_batch([p.searchable_text() for p in batch])
        product_vectors.update({p.product_id: v for p, v in zip(batch, vectors)})
        if (start // 32) % 20 == 0:
            print(json.dumps({"embedded": start + len(batch)}), flush=True)

    queries = sorted({case["query"] for case in cases})
    query_vectors = {}
    for start in range(0, len(queries), 32):
        batch = queries[start:start + 32]
        vectors = await embedder.embed_batch(batch)
        query_vectors.update(dict(zip(batch, vectors)))
    print(json.dumps({"stage": "embed_queries", "n": len(queries)}), flush=True)

    index = StaticIndex(product_vectors)
    fake = QueryEmbedder(query_vectors)
    selected = [c for c in cases if split == "all" or c["mode"] == split]
    samples = []
    for i, case in enumerate(selected, 1):
        pool = [p for p in products
                if p.source_platform == case["platform"] and p.source_language == case["corpus_language"]]
        repo = InMemoryProductRepository(pool)
        variants = {
            "dense": CatalogSearchUseCase(repo, fake, index, hybrid_enabled=True, hybrid_lexical_weight=0),
            "hybrid": CatalogSearchUseCase(repo, fake, index, hybrid_enabled=True),
        }
        for name, usecase in variants.items():
            result = await usecase.execute(ProductSearchSpec(
                normalized_query=case["query"], ship_to=case["ship_to"], top_k=5,
            ))
            violations = find_hit_constraint_violations(result["hits"], by_id, ship_to=case["ship_to"])
            samples.append({
                "id": case["id"], "variant": name, "mode": case["mode"], "family": case["family"],
                "corpus_language": case["corpus_language"], "query_language": case["query_language"],
                **score(result["hits"], case["relevant_canonical_ids"], 5),
                "violations": {k: sorted(v) for k, v in violations.items()},
                "ids": [h["product_id"] for h in result["hits"]],
            })
        if i % 100 == 0:
            print(json.dumps({"completed": i, "total": len(selected)}), flush=True)

    grouped = collections.defaultdict(list)
    for row in samples:
        grouped[(row["variant"], row["mode"])].append(row)
    report = {
        "scope": "本地 bge-m3 dense/hybrid；复用 v3 池隔离与金标；不含 reranker",
        "embedding": {"model": settings.embedding_model, "dim": settings.embedding_dim},
        "frozen": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
            ROOT / "data/catalog-v3.jsonl", ROOT / "eval/v3/multilingual_retrieval.jsonl",
            ROOT / "scripts/eval/measurement/multilingual_local.py",
        )},
        "summary": {f"{v}/{m}": _summarize(rows) for (v, m), rows in grouped.items()},
        "samples": samples,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--split", choices=("all", "monolingual", "cross_language"), default="all")
    args = parser.parse_args()
    asyncio.run(evaluate(args.output, split=args.split))


if __name__ == "__main__":
    main()
