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
- validator 会从 registry 与源资产重建完整 canonical pilot；任务合同任一字段漂移都会失败。
- `automatic_scoring=false`、`execution_ready=false`：dry-run 尚未实现。
- 暂缺 memory / context / transaction：没有满足 Dev 且语义明确的源，宁缺勿凑。
