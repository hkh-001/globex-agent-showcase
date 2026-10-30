"""Build and query the category KB, preserving independently replayable evidence."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.infrastructure.rag.category_knowledge import (
    bootstrap_category_knowledge,
    build_category_knowledge_base,
)
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.embedding_identity import (
    embedding_identity,
    normalized_embedding_endpoint,
)


async def verify(output: Path, query: str) -> dict:
    settings = load_settings()
    knowledge = build_category_knowledge_base(settings)
    inserted = await bootstrap_category_knowledge(knowledge)
    documents = await knowledge.list_documents()
    response = await knowledge.embedding_model([query])
    vector = response.embeddings[0]
    hits = await knowledge.search(queries=[query], top_k=3)
    result = {
        "query": query,
        "logical_collection": settings.category_kb_collection,
        "physical_collection": knowledge.collection,
        "configured_dimensions": settings.embedding_dim,
        "actual_embedding_dimensions": len(vector),
        "embedding_model": settings.embedding_model,
        "embedding_version": settings.embedding_version,
        "embedding_identity": embedding_identity(settings),
        "normalized_endpoint_sha256": hashlib.sha256(
            normalized_embedding_endpoint(settings.embedding_base_url).encode()
        ).hexdigest(),
        "inserted_documents": inserted,
        "document_count": len(documents),
        "document_ids": sorted(doc.document_id for doc in documents),
        "document_ids_sha256": hashlib.sha256(
            "\n".join(sorted(doc.document_id for doc in documents)).encode()
        ).hexdigest(),
        "hits": [
            {
                "score": float(hit.score),
                "source": hit.chunk.metadata.get("source"),
                "content_sha256": hashlib.sha256(
                    hit.chunk.content.text.encode("utf-8")
                ).hexdigest(),
                "content_excerpt": hit.chunk.content.text[:240],
            }
            for hit in hits
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--query", default="旅行背包价格和自重怎么判断")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.output, args.query)), ensure_ascii=False))


if __name__ == "__main__":
    main()
