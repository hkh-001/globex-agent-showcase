# 来源与策展说明

本公开作品集从私有工程的固定源码快照导出，并创建全新的 Git 历史；没有复制私有仓的 `.git`。

- source commit: `51c938cce2cb60b55f9953ab465fe3c3625b8b12`
- source tree: `0ceae88904f0a628cdb82a5f14ab723e62b82b6d`
- source tag: `v1.1.2-showcase-source`
- initial curated commit: `447380e70308dcd1aa50a43957ea75a36983e263`
- initial curated tree: `37bdb75db9c59e0d3e5c864a747fe7dc41bdb460`

源文件通过 `git archive` 导出。该方法只包含固定 tree 中已跟踪的文件，因此 `.env`、本地运行目录、构建产物和未跟踪 session 文件不会进入输出。

公开版排除了 282 个过程文档、1,179 个历史 verification 文件（其中旧 A3 实验约 24 MB）、Agent 工作指令、根目录阶段计划，以及含本机绝对路径、完整模型请求或授权原文的日志证据。

保留的是应用代码、测试、前端、配置、Docker、版本化数据集、评测 Harness，以及一组重新计算哈希的公开结论文件。完整原始证据留在私有 source tree；公开清单只对实际发布的文件负责。

公开仓的提交记录描述作品集导出、文档和 CI，不冒充私有研发过程。私有工程的历史提交、标签和 Actions 链接没有被复制或改写。
