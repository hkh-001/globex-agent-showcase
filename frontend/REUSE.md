# 原前端复用记录

来源：`globex-agent-reference/frontend`，固定参考HEAD见主计划。参考目录只读。

| 来源相对路径 | 来源 SHA256 | 当前用途 |
| --- | --- | --- |
| `index.html` | `83851abd30b50c3de0f0c7b7f606707375110d0faa5a3abc0d48faa2435dd731` | 原样复用 |
| `package-lock.json` | `e70e5f58d977593900c0cf56ee25e05772533c57a2a8b71005eddf9c0754c86b` | 原样复用 |
| `package.json` | `c2e3247413945b6d2f602883f65bc30c59672bf11a176daf24be9f18a4ea6d45` | 原样复用 |
| `public/assets/globex-atmosphere.png` | `588ab23fc5e9efe4be39597f225ac8c0490994531497ee660bc39afe5a69a6d4` | 原样复用 |
| `src/App.tsx` | `70b8f75bc86e8f6e6e1eda4e4c6046f1ef1a3081c5b54c010f3eefc726f3a76a` | 适配/精简，见下文 |
| `src/components/CatalogMedia.tsx` | `65deedb47d9583bd11bc6893d6fc6b30be31fa8df257ceb42331742dfa472e26` | 原样复用 |
| `src/components/ConversationTurns.tsx` | `65426d22e4f4171e325bf577379b6b13d60eadbd90d902c9d2ee94d94f82b922` | 适配/精简，见下文 |
| `src/components/EventTimeline.tsx` | `c6bab5bcd909e89f3b3aeb97e285804d577db819656c2f4141229638c87cd001` | 原样复用 |
| `src/components/ExecutionTrace.tsx` | `cb8d63785a6f4ab68819c648399d69759bce23ec656be64cdec486fa8990962a` | 适配/精简，见下文 |
| `src/components/Icon.tsx` | `1ab3f9cc3357535587bd88cad1de21f71bf373833dadfc262783b176b2514f87` | 原样复用 |
| `src/components/Markdown.tsx` | `e18e1505d173436ffa6f8ff065a56568b8b7bdc91509110efa2c32b26a1a38c7` | 原样复用 |
| `src/components/Modal.tsx` | `fdcf76b8f1bff7d407103e1581658541e985005da28d39169db9a9c02351eb04` | 原样复用 |
| `src/components/ProductCards.tsx` | `edb56ddd3a8ddcba8fff6d180d403a411ccb222e0f5120545d2b3017cccea62b` | 适配/精简，见下文 |
| `src/components/ProductComparison.tsx` | `66af7b1570578c3486a3d3f99f3e86ebe2036899d162e6f7fd52c720c111df71` | 原样复用 |
| `src/components/ProductDetail.tsx` | `979b670a856a3860d3b92fdc048cfe92ecbafae6969334b621e83551200aca76` | 适配/精简，见下文 |
| `src/components/ToolProductResult.tsx` | `e37b57df55203fb340f13da19e25cb1dd8d24c5e597fe613cccb841159ee2620` | 原样复用 |
| `src/components/compactProductCards.css` | `a25903b376ea385584d4154d763e0006ba495f90c70d6a45c9c9a9d09f799ab2` | 原样复用 |
| `src/components/confirmations.css` | `4a713ea9d39b3c4e94902daf14464a911d2573b5fa3ba9af28bb3d9094586b85` | 原样复用 |
| `src/components/conversationDensity.css` | `20f014c68e814f380823ac904a7aa69387d4a3e87cd3d92a2d41c0bbbf3dbf6c` | 原样复用 |
| `src/components/executionTrace.css` | `a34fd67a90fb14fac1d81ca10541eb71e310175968944faa587aa82024f319aa` | 原样复用 |
| `src/components/productDetail.css` | `f0e7d8cd6f34aa83f53bc4d43169a8633e5ff5997c727cab42f447285aefbe8a` | 原样复用 |
| `src/hooks/useCommerceAgent.ts` | `d5a77d37854321aaf722a7f7ea3a7f3707103f610425c4247de9f56b51330884` | 适配/精简，见下文 |
| `src/lib/commerceClient.ts` | `8896acf1b28b29691b90c5f6f3f81c1be9ec6db505714d3513bb1b910ee94417` | 适配/精简，见下文 |
| `src/main.tsx` | `13accc51d148a755c6bc55fe5722683433296b0ae73178e065f5e0ed409090cb` | 适配/精简，见下文 |
| `src/styles.css` | `d3fb9861dcaf0f0a6006f1a4451dc66e0e57bce4fc179b90bb3d214516c80e30` | 适配/精简，见下文 |
| `src/types.ts` | `008ef257fe703dff3832022aa44a05fa2261aa865e704ab22a04532ca1ad2560` | 适配/精简，见下文 |
| `tsconfig.json` | `5ef18e1e733ca6998c62ab11be2e7a86e71b0e3f66024c9c0d0ed10c684b876b` | 原样复用 |
| `vite.config.ts` | `fd59f9656e47de378dfe38c7bda6bae8ee881ca44b67f406a3bda4733a57b249` | 原样复用 |

## 必要变更

- App.tsx：从原侧栏/顶栏、欢迎布局、商品区域、composer与弹窗组合中裁剪skills/收藏/自动化/检索评测/长期偏好/模型选择等未支持导航。保留原class与文案骨架、组件和CSS；会话来自服务端。原App非常耦合外围服务，故重组核心组合，未复制774行后保留无效请求。
- hooks/useCommerceAgent.ts：保留useSyncExternalStore与CommerceClient生命周期；删除skills初始化，仅初始化服务端会话。
- lib/commerceClient.ts：重组原HttpAgent订阅、message/state/event投影与服务端快照读取路径。合同只发最后user/state空/tools空/context空/forwardedProps空，不信客户端历史。localStorage只存选中会话ID。断线查询事实，不自动重跑模型或交易；恢复running会话仅GET轮询，每秒一次最多120次，终态停止，epoch切换/重置/卸载清理，失败给手动刷新提示。
- ProductCards：删除未支持收藏按钮及props；无运税时明确未提供。商品金额直接消费后端major投影；无伪造评分、图像与运税。
- ConversationTurns：移除未支持购物表单消息解析，直接展示用户文本；执行轨迹仍用原ExecutionTrace。
- 确认区域：原ConfirmationCards包含本期不支持撤单与完整地址展示，沿用原confirmation-card样式并裁成显式批准/拒绝与本地订单事实，不采集虚假地址。详情准备通过可信HTTP /confirmations/prepare提交实际SKU/evidence/quantity，不能模型批准；自然对话AgentScope prepare由后端单测覆盖，离线不冒充自由理解。
- CatalogMedia原媒体解析/占位组件保留，但不挂载发起/product-media请求的Provider；无媒体注册表时只显示目录原图/占位，不复制参考商品示意图冒充本目录实物。
- main.tsx只挂载购物App，移除独立检索评测页面；package-lock沿用原锁并npm ci验证。
- 首屏通过/health读取服务端mode并醒目标注离线录制；欢迎快捷例子仅目录支持的背包/水杯并提供配送国家。
- 执行过程移除未支持Think/模型管理提示，只呈现真实工具活动。
- 商品区标题消费服务端view_kind，详情/比较标为只读快照，不误称当前搜索候选。
- I3详情按钮采用`详情 product_id`明确触发已支持的get_product_details；比较弹窗标注只读快照，不冒充工具调用。
- ProductDetail新增无evidence_id禁prepare，详情/比较快照不扩授权。types新增服务端证据/库存快照字段。
- ProductDetail、ProductComparison、ConversationTurns、ExecutionTrace、Modal、Markdown、Icon和原CSS用于实际页面，无iframe/旧HTML。

## 验证

`npm ci --ignore-scripts`、`npm test`、`npm run build`。测试使用真实HttpAgent处理SSE，不mock整个客户端；验证服务端快照恢复无重跑、旧卡清理与localStorage不成为事实源。最终浏览器联调由统一I2/I3验收记录固定代码版本。

## Native entry label wiring

顶部协议标签仅投影服务端 `/health.model_protocol` 与 `STATE_SNAPSHOT/state.model_protocol`，支持 `native-tools-v1`、`legacy-json-v1`；缺失或未知值显示 `unknown`。协议与离线/DeepSeek运行模式分别展示，不从模式猜协议、不添加模型管理或调用。历史会话显示实际绑定协议，新选购使用已有health或snapshot.configured_model_protocol的服务端配置事实。仅复用已有health与会话请求，没有增加模型POST或切换上下文策略。

## native-tools-v2 bounded-detail entry

仅将现有协议联合类型/服务端字段解析白名单扩展为native-tools-v2。继续使用原协议标签、四卡详情快照、Modal/比较组件及CSS，无新按钮/布局/模型请求。v2会话恢复、旧v1会话标签、缺字段unknown和新会话采用server configured协议事实有离线GET-only回归；聚合详情/SKU顺序仍由服务端真实products数组决定，前端不补造卡/证据。
