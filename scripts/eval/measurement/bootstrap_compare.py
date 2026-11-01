"""只读分析：对两份 retrieval manifest 的逐 query 指标做配对 bootstrap。

用法：
  python -m scripts.eval.measurement.bootstrap_compare \
    --baseline-manifest <keyword>.manifest.json \
    --candidate-manifest <embedding>.manifest.json \
    --baseline-strategy keyword_2gram --candidate-strategy embedding_only \
    --out .globex-runtime/metrics-v1/retrieval/bootstrap.json

比值口径用 Σb/Σa（逐 query 比值在指标为 0 时分母无定义）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random

SEED = 20260918
ITERATIONS = 5000


def _per_query(manifest: dict, strategy: str) -> dict[str, dict]:
    metrics = ((manifest.get("execution") or {}).get("metrics") or {})
    if strategy not in metrics:
        raise SystemExit(f"manifest 中找不到策略 {strategy} 的指标：{sorted(metrics)}")
    rows = metrics[strategy].get("per_query") or []
    if not rows:
        raise SystemExit(f"策略 {strategy} 的 per_query 为空，拒绝用 0 条数据出结论")
    return {row["query"]: row for row in rows}


def _ratio(values_a: list[float], values_b: list[float]) -> float:
    total_a = sum(values_a)
    if total_a <= 0:
        return float("nan")
    return sum(values_b) / total_a


def _bootstrap(pairs: list[tuple[float, float]]) -> dict:
    rng = random.Random(SEED)
    if len(pairs) < 2:
        return {"ratio": _ratio([a for a, _ in pairs], [b for _, b in pairs]),
                "ci95": None, "iterations": 0, "pairs": len(pairs)}
    observed = _ratio([a for a, _ in pairs], [b for _, b in pairs])
    samples = []
    n = len(pairs)
    for _ in range(ITERATIONS):
        pick = [pairs[rng.randrange(n)] for _ in range(n)]
        value = _ratio([a for a, _ in pick], [b for _, b in pick])
        if value == value:
            samples.append(value)
    samples.sort()
    low = samples[int(0.025 * len(samples))]
    high = samples[int(0.975 * len(samples)) - 1]
    return {"ratio": observed, "ci95": [low, high], "iterations": ITERATIONS, "pairs": n}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--candidate-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--baseline-strategy", default="keyword_2gram")
    parser.add_argument("--candidate-strategy", default="embedding_only")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()

    base_manifest = json.loads(args.baseline_manifest.read_text(encoding="utf-8"))
    cand_manifest = json.loads(args.candidate_manifest.read_text(encoding="utf-8"))

    actual = (base_manifest.get("execution") or {}).get("actual_strategies")
    expected = {args.baseline_strategy: [args.baseline_strategy]}
    if actual != expected:
        raise SystemExit(f"基线档发生静默降级：actual_strategies={actual}，预期 {expected}")

    base = _per_query(base_manifest, args.baseline_strategy)
    cand = _per_query(cand_manifest, args.candidate_strategy)
    shared = sorted(set(base) & set(cand))
    if not shared:
        raise SystemExit("两份 manifest 没有共同 query，无法配对")

    result: dict = {
        "seed": SEED,
        "iterations": ITERATIONS,
        "k": ((base_manifest.get("parameters") or {}).get("top_k")
              or (cand_manifest.get("parameters") or {}).get("top_k")),
        "n_pairs": len(shared),
        "missing": {"baseline_only": len(set(base) - set(cand)), "candidate_only": len(set(cand) - set(base))},
        "overall": {},
        "by_kind": {},
        "per_query": [],
    }

    for metric in ("ndcg", "recall"):
        pairs = [(float(base[q][metric]), float(cand[q][metric])) for q in shared]
        result["overall"][metric] = {
            "baseline_mean": sum(a for a, _ in pairs) / len(pairs),
            "candidate_mean": sum(b for _, b in pairs) / len(pairs),
            **_bootstrap(pairs),
        }

    kinds = sorted({base[q].get("kind", "lexical") for q in shared})
    for kind in kinds:
        queries = [q for q in shared if base[q].get("kind", "lexical") == kind]
        pairs = [(float(base[q]["ndcg"]), float(cand[q]["ndcg"])) for q in queries]
        result["by_kind"][kind] = {"n": len(queries), **_bootstrap(pairs)}

    for q in shared:
        result["per_query"].append({
            "query": q,
            "kind": base[q].get("kind"),
            "baseline_ndcg": base[q].get("ndcg"),
            "candidate_ndcg": cand[q].get("ndcg"),
            "baseline_recall": base[q].get("recall"),
            "candidate_recall": cand[q].get("recall"),
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("n_pairs", "overall", "by_kind")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
