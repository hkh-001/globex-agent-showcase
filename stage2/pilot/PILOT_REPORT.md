# Stage 2 Pilot Report (registry + source-oracle drafts, not executable)

- 候选数：**3**（search / knowledge / routing）
- **候选数 ≠ 正式评测集规模**；正式 60 题未生成。
- 来源：`stage2/pilot/conversion-registry.json`，逐条按 `record_id` 解析。
- **题目级 oracle 继承源记录**：
  - Search：P1015 / CAN-LEGACY-014 / CNY / require_in_stock；
  - Knowledge：expected document `eval-digital-accessories-价格与预算.md`；
  - Routing：required_tools `product_search_tool`，完整路径 `main_agent → search_agent → product_search_tool`。
- 每题保存 `source_oracle`、`source_oracle_sha256`、`oracle_adapter_id/version`、adapter 输出 hash。
- deterministic checks 使用显式 selector 与量词（`contains_any/contains_all/all_equal/any_gt`），
  并由 `scripts/eval/agentbench/deterministic_checks.py` fail-closed 执行；合成正负轨迹已纳入契约测试。
- Knowledge 的文档来源只从真实 ToolChunk 的 `tool_result` 读取；bus event 仅判
  `hit_count/abstained/policy_fact_statuses`。对应源文档必须存在于正式 `knowledge/manifest.jsonl`；
  scorer 强制按 `shopping_session_id + run_id` scope 过滤 event 与 ToolChunk。
- validator 会从 registry 与源资产重建完整 canonical pilot；任务合同任一字段漂移都会失败。
- `automatic_scoring=false`、`execution_ready=false`：scorer plumbing runner 已实现，但在独立审查前不解锁。
- 暂缺 memory / context / transaction：没有满足 Dev 且语义明确的源，宁缺勿凑。

## Scorer plumbing dry-run 边界

`scripts/eval/agentbench/run_pilot_plumbing.py` 用 fixture 按已冻结计划调用真实工具，采集真实
bus event 与 ToolChunk，并同时运行 execution graph 与 deterministic-check scorer。它不执行
LLM Agent，只能证明评分管线，不是任务通过率或 Agent 能力证据。每题强制独立 session/run scope，
模型调用为 0，外部网络由守卫阻断；任务中的 `automatic_scoring` 与 `execution_ready` 保持 false。
`physical_model_calls=0` 是该 fixture 模式没有模型执行路径的定义值，不是供应商用量埋点测量。

Knowledge 使用正式 `KNOWLEDGE_DIR` 上的确定性 keyword fallback，仅验证真实文件、工具合同和
ToolChunk 采集，不证明向量召回效果。
