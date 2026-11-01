# AgentBench 已知问题

## KI-001：测试运行在仓库根目录生成 `:memory:.ses`

- **现象**：跑测试后仓库根目录出现 51 字节的 `:memory:.ses`，内容为时间戳 + UUID。
- **复现**：在仓库根目录运行 `pytest -q`（或 `tests/test_agentbench_stage0.py`）后执行 `git status --porcelain`。
- **路径**：`./:memory:.ses`（仓库根目录，非 `.globex-runtime/`）。
- **推测来源**：某个 session store 把 `:memory:` 当作相对文件名打开并落盘；未定位到具体调用点。
- **Git 状态**：未被 `.gitignore` 覆盖，会以未跟踪文件出现，污染“工作树干净”的证据判断。
- **当前处置**：每次运行后手动删除；**不提交该文件**，也不在本阶段修复（Stage 1 只做资产清点）。
- **后续**：在产品加固阶段定位并修复（优先检查会话存储的路径解析），并补一条“测试后工作树干净”的回归断言。
