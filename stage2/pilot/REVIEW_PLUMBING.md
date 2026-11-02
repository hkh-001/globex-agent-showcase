# Scorer-Plumbing v2 独立审查清单

> 本清单只审“真实工具 → 事件/ToolChunk → 双 scorer → 可重算校验”的管线。
> 不审 Agent 自主行为、RAG 召回、任务成功率。`physical_model_calls=0` 是零模型 replay 的模式定义，不是供应商 usage 证据。

## 1. 来源与工作区

```bash
SOURCE_REPO=/tmp/plumbing-source-417869d
REVIEW_REPO=/Users/hkh/Desktop/实习准备/Globex-agent-showcase
REVIEW_TOOLING_COMMIT=3a0704169a2786b7e6db69a2e6b72873b807e2c5
PY="$REVIEW_REPO/.venv/bin/python"
git -C "$REVIEW_REPO" worktree add "$SOURCE_REPO" 417869d
cd "$SOURCE_REPO"
git status --porcelain
```

预期：干净；`source_commit=417869d`，`source_tree` 与 manifest 一致。

审查工具不存在于 `417869d`，必须从单独的已绑定提交执行：

```bash
git -C "$REVIEW_REPO" merge-base --is-ancestor "$REVIEW_TOOLING_COMMIT" HEAD
git -C "$REVIEW_REPO" show "$REVIEW_TOOLING_COMMIT":scripts/eval/agentbench/check_pilot_plumbing.py | shasum -a 256
git -C "$REVIEW_REPO" show "$REVIEW_TOOLING_COMMIT":scripts/eval/agentbench/compare_plumbing_replay.py | shasum -a 256
git -C "$REVIEW_REPO" show "$REVIEW_TOOLING_COMMIT":tests/test_agentbench_plumbing.py | shasum -a 256
```

预期分别为：

- `278dbbf8e67ac814f894d16b8ffa2aa8f3ffa026f02e1d5df7f4e35d93ec1bdc`
- `7180f60e97c72d9e6c44cbae15968d0a89a51e89ddcdaed1bfb8e2522a94616b`
- `0319a4af17ea4a02ded5968f7f4d587483b96844aa71e6ff29287cdd11b108b7`

## 2. 重跑 runner

```bash
cd "$SOURCE_REPO"
$PY -m scripts.eval.agentbench.run_pilot_plumbing \
  --output "$SOURCE_REPO/out/evidence.json" \
  --network-log "$SOURCE_REPO/out/network.jsonl" \
  --manifest "$SOURCE_REPO/out/manifest.json"
```

预期：`mode=scorer_plumbing_fixture_replay`、`agent_executed=false`、`3/3`、`external_connect_attempts=0`。

## 3. 语义对比（不要逐字比较）

`occurred_at / dispatch_id / elapsed_ms / started_at / finished_at` 等是动态字段，禁止要求字节一致：

```bash
cd "$REVIEW_REPO"
$PY -m scripts.eval.agentbench.compare_plumbing_replay \
  --baseline "$REVIEW_REPO/stage2/pilot/evidence-plumbing-v2" \
  --candidate "$SOURCE_REPO/out"
```

预期：`passed=true`。比较项为 task/scope/event 类型与工具/关键 payload/ToolChunk/双 scorer 结果。

## 4. 独立重算

```bash
cd "$REVIEW_REPO"
$PY -m scripts.eval.agentbench.check_pilot_plumbing \
  --evidence "$REVIEW_REPO/stage2/pilot/evidence-plumbing-v2/evidence.json" \
  --network-log "$REVIEW_REPO/stage2/pilot/evidence-plumbing-v2/network.jsonl" \
  --manifest "$REVIEW_REPO/stage2/pilot/evidence-plumbing-v2/manifest.json"
```

预期：`passed=true`。注意：checker 只证明“自洽且可重算”，不证明执行真实性；执行真实性来自第 2/3 步的独立重放。

## 5. network guard 正向对照

```bash
cd "$REVIEW_REPO"
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
