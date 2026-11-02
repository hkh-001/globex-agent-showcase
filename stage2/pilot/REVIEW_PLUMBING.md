# Scorer-Plumbing v2 独立审查清单

> 本清单只审“真实工具 → 事件/ToolChunk → 双 scorer → 可重算校验”的管线。
> 不审 Agent 自主行为、RAG 召回、任务成功率。`physical_model_calls=0` 是零模型 replay 的模式定义，不是供应商 usage 证据。

## 1. 来源与工作区

```bash
git worktree add /tmp/plumbing-review 417869d
cd /tmp/plumbing-review
git status --porcelain
```

预期：干净；`source_commit=417869d`，`source_tree` 与 manifest 一致。

## 2. 重跑 runner

```bash
REPO=/Users/hkh/Desktop/实习准备/Globex-agent-showcase
PY="$REPO/.venv/bin/python"
$PY -m scripts.eval.agentbench.run_pilot_plumbing \
  --output /tmp/plumbing-review/out/evidence.json \
  --network-log /tmp/plumbing-review/out/network.jsonl \
  --manifest /tmp/plumbing-review/out/manifest.json
```

预期：`mode=scorer_plumbing_fixture_replay`、`agent_executed=false`、`3/3`、`external_connect_attempts=0`。

## 3. 语义对比（不要逐字比较）

`occurred_at / dispatch_id / elapsed_ms / started_at / finished_at` 等是动态字段，禁止要求字节一致：

```bash
$PY -m scripts.eval.agentbench.compare_plumbing_replay \
  --baseline "$REPO/stage2/pilot/evidence-plumbing-v2" \
  --candidate /tmp/plumbing-review/out
```

预期：`passed=true`。比较项为 task/scope/event 类型与工具/关键 payload/ToolChunk/双 scorer 结果。

## 4. 独立重算

```bash
$PY -m scripts.eval.agentbench.check_pilot_plumbing \
  --evidence "$REPO/stage2/pilot/evidence-plumbing-v2/evidence.json" \
  --network-log "$REPO/stage2/pilot/evidence-plumbing-v2/network.jsonl" \
  --manifest "$REPO/stage2/pilot/evidence-plumbing-v2/manifest.json"
```

预期：`passed=true`。注意：checker 只证明“自洽且可重算”，不证明执行真实性；执行真实性来自第 2/3 步的独立重放。

## 5. network guard 正向对照

```bash
$PY -m pytest -q -p no:cacheprovider \
  tests/test_agentbench_plumbing.py::test_network_guard_blocks_external_and_records
```

必须看到：外部连接抛阻断异常、日志出现 `{"kind":"external","operation":"connect"}`、外部计数为 1；loopback 不被阻断。

## 6. 协调篡改负例

必须拒绝：

- 删除 knowledge ToolChunk；
- 删除关键 event；
- 修改 payload；
- 修改保存的 `execution_score` / `deterministic_score`；
- 同步重算所有哈希但保留 `passed=true`。

## 7. 声明检查

报告必须写明：

- 只证明 scorer plumbing；
- `agent_executed=false`；
- `physical_model_calls=0` 是模式定义；
- 不证明 Agent 会自主选择工具；
- `automatic_scoring=false`、`execution_ready=false` 未被解锁。
