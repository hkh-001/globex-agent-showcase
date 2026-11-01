"""只读分析：两份 retrieval manifest 的配对比较。

输出：nDCG/Recall 的比值与 bootstrap CI、固定分母 P@5、平均返回条数、过滤准确率、
逐 query 胜负表与效应量。绝不改写输入 manifest。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics

SEED = 20260918
ITERATIONS = 5000


def _per_query(manifest: dict, strategy: str) -> dict[str, dict]:
    metrics = ((manifest.get("execution") or {}).get("metrics") or {})
    if strategy not in metrics:
        raise SystemExit(f"manifest 中找不到策略 {strategy}：{sorted(metrics)}")
    rows = metrics[strategy].get("per_query") or []
    if not rows:
        raise SystemExit(f"策略 {strategy} 的 per_query 为空，拒绝用 0 条数据出结论")
    return {row["query"]: row for row in rows}


def _actual_ok(manifest: dict, strategy: str) -> bool:
    values = (((manifest.get("execution") or {}).get("actual_strategies")) or {}).get(strategy) or []
    if strategy == "keyword_2gram":
        return values == ["keyword_2gram"]
    if strategy.startswith("hybrid"):
        # 无精排时合法结果是 hybrid_only；降到 keyword 才算失败。
        return bool(values) and "keyword_2gram" not in values
    return strategy in values


def _ratio(a: list[float], b: list[float]) -> float:
    total = sum(a)
    return float("nan") if total <= 0 else sum(b) / total


def _bootstrap(pairs: list[tuple[float, float]]) -> dict:
    if len(pairs) < 2:
        return {"ratio": _ratio([a for a, _ in pairs], [b for _, b in pairs]), "ci95": None, "pairs": len(pairs)}
    rng = random.Random(SEED)
    observed = _ratio([a for a, _ in pairs], [b for _, b in pairs])
    samples = []
    for _ in range(ITERATIONS):
        pick = [pairs[rng.randrange(len(pairs))] for _ in range(len(pairs))]
        value = _ratio([a for a, _ in pick], [b for _, b in pick])
        if value == value:
            samples.append(value)
    samples.sort()
    return {"ratio": observed, "ci95": [samples[int(0.025 * len(samples))], samples[int(0.975 * len(samples)) - 1]],
            "iterations": ITERATIONS, "pairs": len(pairs)}


def _side(rows: dict[str, dict], queries: list[str], metric: str) -> list[float]:
    return [float(rows[q].get(metric) or 0) for q in queries]


def _fixed_p5(row: dict) -> float:
    hits = len(set(row.get("retrieved") or []) & set(row.get("relevant") or []))
    return hits / 5.0


def _summary(base: dict, cand: dict, queries: list[str]) -> dict:
    deltas = [float(cand[q]["ndcg"]) - float(base[q]["ndcg"]) for q in queries]
    improved = sum(1 for d in deltas if d > 1e-9)
    regressed = sum(1 for d in deltas if d < -1e-9)
    return {
        "n": len(queries),
        "ndcg": {"baseline": statistics.mean(_side(base, queries, "ndcg")),
                 "candidate": statistics.mean(_side(cand, queries, "ndcg")),
                 **_bootstrap([(float(base[q]["ndcg"]), float(cand[q]["ndcg"])) for q in queries])},
        "recall": {"baseline": statistics.mean(_side(base, queries, "recall")),
                   "candidate": statistics.mean(_side(cand, queries, "recall")),
                   **_bootstrap([(float(base[q]["recall"]), float(cand[q]["recall"])) for q in queries])},
        "fixed_p5": {"baseline": statistics.mean([_fixed_p5(base[q]) for q in queries]),
                     "candidate": statistics.mean([_fixed_p5(cand[q]) for q in queries])},
        "avg_returned": {"baseline": statistics.mean([len(base[q].get("retrieved") or []) for q in queries]),
                         "candidate": statistics.mean([len(cand[q].get("retrieved") or []) for q in queries])},
        "filter": {"baseline": statistics.mean([1.0 if base[q].get("filter_ok") else 0.0
                                                for q in queries if base[q].get("filter_ok") is not None]) if any(
                       base[q].get("filter_ok") is not None for q in queries) else None},
        "win_loss": {"improved": improved, "regressed": regressed, "equal": len(queries) - improved - regressed,
                     "median_delta": statistics.median(deltas) if deltas else 0.0},
    }


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
    for manifest, strategy, label in (
        (base_manifest, args.baseline_strategy, "baseline"), (cand_manifest, args.candidate_strategy, "candidate"),
    ):
        if not _actual_ok(manifest, strategy):
            raise SystemExit(f"{label} 档发生静默降级：actual_strategies={manifest['execution'].get('actual_strategies')}")

    base = _per_query(base_manifest, args.baseline_strategy)
    cand = _per_query(cand_manifest, args.candidate_strategy)
    shared = sorted(set(base) & set(cand))
    if not shared:
        raise SystemExit("两份 manifest 没有共同 query，无法配对")

    result = {
        "seed": SEED, "iterations": ITERATIONS, "n_pairs": len(shared),
        "missing": {"baseline_only": sorted(set(base) - set(cand)), "candidate_only": sorted(set(cand) - set(base))},
        "overall": _summary(base, cand, shared),
        "by_kind": {},
        "per_query": [{"query": q, "kind": base[q].get("kind"),
                       "baseline_ndcg": base[q].get("ndcg"), "candidate_ndcg": cand[q].get("ndcg"),
                       "baseline_returned": len(base[q].get("retrieved") or []),
                       "candidate_returned": len(cand[q].get("retrieved") or [])} for q in shared],
    }
    for kind in sorted({base[q].get("kind", "lexical") for q in shared}):
        queries = [q for q in shared if base[q].get("kind", "lexical") == kind]
        result["by_kind"][kind] = _summary(base, cand, queries)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["overall"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
