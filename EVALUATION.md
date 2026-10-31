# 评测与证据

| 层 | 回答的问题 | 当前公开证据 |
| --- | --- | --- |
| 单元与契约 | 业务规则、协议和安全门是否保持 | 939 collected；938 passed；1 个显式平台豁免 |
| 公开业务基线 | 精选检索与交易链是否实际执行 | 18 / 18 |
| 真实模型闭环 | 模型能否完成检索和准备订单，确定性批准后状态是否正确 | 4 次物理请求；闭环通过 |

## 公开业务基线

18 个场景覆盖部分约束满足、价格完整性、到手价连续性、授权、库存安全、事务原子性、幂等、所有权隔离、持久化和预算合同。每个场景必须由 pytest 实际执行一项并通过；退出码为零但未执行、skip 或多执行都不会被接受。

```bash
make baseline
```

这些场景是人工精选的回归集合，占全量测试的一小部分。18/18 不代表业务覆盖率、模型回答质量或 RL 收益。

## Harness 契约门

```bash
uv run python -m scripts.eval.harness run --suite eval/harness/v1/suite.json --profile contracts --output .globex-runtime/contracts
```

当前口径是 939 项被收集，其中 938 项执行并通过，1 项是带精确 nodeid、平台和原因的显式豁免。未声明 skip、原因变化或过期白名单都会失败。

## 真实模型闭环

公开摘要记录一条隔离 SQLite 上的 DeepSeek 自然语言链：检索商品、准备确认单、人工批准、持久化读取、准备取消和人工批准取消。4/12 次物理请求，单次输出上限 1024 token，零自动重试；库存序列为 `80 → 80 → 79 → 79 → 80`，价格为 `129 + 25 + 0 = 154 CNY`。

该结果只证明固定版本、固定合成场景的一次受控回归，不是统计意义上的模型效果比较。

### S01 单场景诊断驱动

S01 的真实模型重跑使用入库驱动 [`scripts/eval/run_s01_canary.py`](scripts/eval/run_s01_canary.py)。默认的 `preflight` 只使用 `httpx.MockTransport` 和本机回环，不读取模型密钥，也不连接供应商：

```bash
uv run python -m scripts.eval.run_s01_canary preflight \
  --output .globex-runtime/eval/s01-preflight-<new-id>
```

付费 `run` 模式必须额外提供一次性、权限为 `0600`、直接位于 `.globex-runtime/authorizations/` 的授权 receipt，并让 `--acknowledge-paid-run` 与其中 `receipt_id` 完全一致。receipt 还必须绑定当前 source commit、S01、`deepseek-chat`、温度 0、每请求 8192 输出 token、最多 4 次物理请求、零自动重试和空 fallback。驱动按 `receipt_id` 建立全局消费标记，复制或改名不能重复使用；随后启动全新账本和隔离数据目录。它还会强制使用本机回环的 bge-m3 1024 维 embedding，关闭 reranker、Web 搜索、模型恢复和其他外部旁路。任何启动、模型身份、cap、usage、账本、事件合同或副作用故障均保留首次结果并停止，不会自动调参重跑。

驱动明确不调用模型连接探针：现有连接探针固定使用 1024 输出 token，与本批冻结的 8192 合同不同。批前正向验证由 MockTransport 完成；真实供应商的第一次请求就是获得单独授权后的 S01，不额外消耗“连通性探针”调用。

## 证据完整性

[公开证据目录](eval/verification/public)只发布不含本机日志和绝对路径的结论文件。`manifest.json` 为这些公开文件记录 SHA-256。完整原始日志保留在私有源仓的固定 source commit/tree 中，公开仓不声称其完整披露。

## CI 与人工验收边界

| 结论 | 保证方式 |
| --- | --- |
| Harness 契约门 | `Harness regression` 对匹配路径的 push / pull request 自动执行 |
| 18 项公开业务基线 | `Harness regression` 自动执行并保留 report artifact |
| 9 份公开证据哈希 | `Harness regression` 自动调用 `scripts/verify_public_evidence.py` |
| 252 项前端测试与生产构建 | `Frontend regression` 对前端源码、测试与配置变更自动执行 |
| high / critical 依赖例外 | `Frontend regression` 同时运行负向合同与真实 `npm audit` |
| 真实模型效果 | 不由默认 CI 保证；必须手动选择、单独授权与单独报告 |

路径匹配合同保证守护文件会进入对应 workflow；是否适合公网部署、指标是否被如实表述、仓库是否可匿名访问，仍属于人工发布验收。
