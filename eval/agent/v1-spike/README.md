# AgentBench v1 Stage 0 Spike

This directory is an evaluation-infrastructure spike, not a model benchmark.
It validates execution ownership, graph scoring, per-case isolation and
accounting before any release/holdout task set is created.

## Frozen boundary

- No prompt, retrieval, recommendation or transaction-rule changes.
- No paid or external model calls.
- No Knowledge Agent claim: category knowledge is a Search Agent tool.
- Mock/scripted outcomes do not count as model capability metrics.
- Missing execution ownership is `evidence_invalid` (fail closed).

## Event ownership

The existing `TradeEvent.correlation` envelope carries `agent`,
`dispatch_id`, and `parent_dispatch_id`. Business payloads and the event
envelope remain unchanged. ContextVar scopes must always reset in `finally`.

## Edge grammar

An edge is `[agent, tool_or_agent]`. Agent-to-agent means dispatch;
agent-to-tool means invocation. Required capabilities map to one or more
tools through `capability-map.json`.

## Task success

End-to-end success is the conjunction of all deterministic P0/P1 checks.
Weighted scores are diagnostic only and cannot replace task success.

## Reproduce

```bash
uv run python scripts/eval/run_agentbench_spike.py \
  --output .globex-runtime/agentbench-stage0-$(date +%Y%m%dT%H%M%S)
```

The runner validates all three task documents but does not execute them as a
model benchmark (`tasks_executed=false`). It executes positive and negative
graph probes and deterministic local contract tests under a socket guard.
External and paid request counts are derived from the guard journal rather
than written as constants. This is an infrastructure result, not an Agent
quality score. `dispatch_isolation_groups` proves distinct dispatch ownership;
it does not by itself prove wall-clock overlap.
