# Stage 2 Pilot Report (registry-backed drafts, not executable)

- 候选数：**3**（search / knowledge / routing）
- **候选数 ≠ 正式评测集规模**；正式 60 题未生成。
- 来源：`stage2/pilot/conversion-registry.json` 的显式 entry，逐条按 `record_id` 解析。
- 三题均为 Dev；`source_splits` 只允许 dev/空，Holdout/Release/Quarantined 一律拒绝。
- 每题包含：source payload/turn hash、adapter id+version、oracle id、题目级 checks、expected params、隔离与副作用。
- `automatic_scoring=false`、`execution_ready=false`：dry-run 尚未实现。
- 暂缺：memory（无合适 Dev 且明确“记住偏好”的源）、context（源问题引用缺失的“第一批”前置）、transaction（business-public 无用户 prompt）。宁缺勿凑。
- 功效：pilot 不用于模块级成功率或对外提升幅度。
