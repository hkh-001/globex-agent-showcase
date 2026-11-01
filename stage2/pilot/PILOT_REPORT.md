# Stage 2 Pilot Report (draft, not executable)

- 候选数：5（search / knowledge / memory / context / routing）
- **候选数 ≠ 正式评测集规模**；本文件只是 source-backed pilot 草稿，正式 60 题尚未生成。
- 每题只绑定一个 verified capability 和一个 Dev leakage group；Holdout 未使用。
- 每题包含真实 `source_record_id`、`source_prompt_sha256`、`turns`（源资产 prompt）和转换依据。
- `automatic_scoring=false`、`execution_ready=false`：dry-run scorer 尚未实现，不能声称可自动评分。
- Transaction 暂缺：`trade.prepare` 的候选资产 `business-public` 是 case 元数据，没有用户 prompt；不伪造 turn。
- 评分边界：deterministic checks 直接引用 capability matrix 的 observation；仍需 dry-run 才能执行。
- 功效：pilot 不用于模块级成功率结论或对外提升幅度。
