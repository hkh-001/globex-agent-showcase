# Stage 2 Pilot Report

- 候选数：6（search / knowledge / memory / context / routing / transaction 各 1）
- **候选数 ≠ 正式评测集规模**：本文件只是 pilot，用于验证生成管线与评分衔接；正式 60 题尚未生成。
- 来源：`eval/agent/v1/template-catalog.json` 中 `recommended_split=dev` 的 leakage group；Holdout 未使用。
- 每题只绑定一个 leakage group；同组不得跨 split。
- 能力来源：`capability-matrix.json` 中 18 项 `supported + verified`；未使用 4 项 instrumentation 与 1 项 not_supported。
- 评分：`oracle_type=deterministic`，要求现有 execution-graph scorer 可判定；交易题显式要求独立数据库、买家、会话和前后快照。
- 功效：pilot 不用于模块级成功率结论，也不用于对外宣称提升幅度。
