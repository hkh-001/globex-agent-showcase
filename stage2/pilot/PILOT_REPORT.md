# Stage 2 Pilot Report (registry + source-oracle drafts, not executable)

- 候选数：**3**（search / knowledge / routing）
- **候选数 ≠ 正式评测集规模**；正式 60 题未生成。
- 来源：`stage2/pilot/conversion-registry.json`，逐条按 `record_id` 解析。
- **题目级 oracle 继承源记录**：
  - Search：P1015 / CAN-LEGACY-014 / CNY / require_in_stock；
  - Knowledge：expected document `eval-digital-accessories-价格与预算.md`；
  - Routing：required_tools `product_search_tool`，完整路径 `main_agent → search_agent → product_search_tool`。
- 每题保存 `source_oracle`、`source_oracle_sha256`、`oracle_adapter_id/version`、adapter 输出 hash。
- `automatic_scoring=false`、`execution_ready=false`：dry-run 尚未实现。
- 暂缺 memory / context / transaction：没有满足 Dev 且语义明确的源，宁缺勿凑。
