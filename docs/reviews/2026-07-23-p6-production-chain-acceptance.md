# P6 生产链路验收记录

日期：2026-07-23
状态：硬验收通过，回答质量与性能保留人工审阅项

## 交付结论

P6 已建立支付订单最终状态权威，并将其接入现有单权威 BI 调查链。真实 DeepSeek 标准案例
完成了 `IntentRevision -> PlanRevision -> capability -> evidence -> claim -> AuthorityBundle ->
publication -> delivery`，随后两轮追问复用了同一已发布材料，没有重新执行完整分析。

真实验收环境显式清除了 `OPENAI_API_KEY`。报告记录的唯一模型出站为
`https://api.deepseek.com`，模型为 `deepseek-v4-flash` / `deepseek-v4-pro`，
`openAiHostedRequestCount=0`。

## 支付最终状态权威

- 源行数：75,984,922。
- 规范化终态订单：75,984,873 个唯一订单。
- `successful`：41,234,677；`not_paid_as_of_snapshot`：34,750,196。
- 成功状态重叠 48 个订单，按已接受的 `pay_success` 权威解决。
- 重复成功记录 1 条，按最新完成时间去重。
- 4,463 个成功订单缺少发起时间，使用完成业务日回退，并进入审计。
- 金额对账：88,881,490,051，与 `paid_order_success` 成功订单权威完全一致。
- 覆盖范围：2024-01-01 至 2026-07-04；跨来源当前阶段完整截止为 Lagos 业务日
  2026-06-02。

权威引用：

- `source-load-manifest:sha256:05677892b65ce442098c6e9c56779396232e1363a4523ebadc7b0ccc28ee7117`
- `dataset-snapshot:sha256:1ea81b42ae17e7fab0c3474b541a977dfdbcf834ffb34390ce7f72e4179f27c1`
- `dataset-release:sha256:610c0c0b714199619b5f8b50c6a1718e4a3e9df6dfa354250764688d055ca43f`
- `dataset-release-authority:sha256:733c51cf39441ff87528bb5ba35ef35c2789bcf2808e1b647964efe7ece7b7b6`
- `payment-final-outcome-reconciliation:sha256:69928c27035c39cc60ca0db47a343dda6e32b1f523f72734736f546d98cffb32`

`not_paid_as_of_snapshot` 只表示冻结快照内没有观察到 `pay_success`。当前来源不能发布失败原因、
失败环节、重试次数、处理耗时或通道故障归因。

## 真实 DeepSeek 纵向验收

通过运行：

- thread：`thread-eval-p6_payment_final_outcome_live-20260723022107a41c83-r1`
- run：`run-7530cc625cc86edcbf95501a`
- 初始轮：`completed_with_limits / analysis_publication`
- 方法追问：`completed_with_limits / tool_response`，调用 `inspect_analysis_artifact`
- 证据挑战：`completed_with_limits / context_response`
- publication：1；初始轮 authority ref：22；硬断言失败：0。
- 报告：`evals/general_agent_runtime/results/p6-latest-live.json`

验收过程中保留了三类失败审计：

1. 第一轮在多来源 metric 混入 `formula_decompose` 后触发 slot mismatch。支付终态指标已改由
   `payment_outcome_health` 单独所有。
2. 第二轮完成能力、claim 和 recommendation 验证后，因 2.86 MB 原始叙事材料触发
   `narrative_input_budget_exceeded`。
3. 第三轮发现事件原始载荷令单条证据超过 64 KiB，触发
   `capability_evidence_observation_budget_exceeded`。

最终修复形成两个通用合同：

- 每个可执行 capability ID 只能属于一个分析轴。外部事件与内部运营事件使用独立 capability
  身份，防止计划按 ID 合并后串用来源。
- 完整查询结果和源内容身份进入 Workbench；叙事层只消费 capability 声明的
  `public-fact-projection.v1`。事件证据保存事件 ID、审阅字段和源内容 digest，原始查询材料由
  result ref 追溯。

## 人工回答质量审阅

本轮 publication 合法且证据闭合，回答质量保持 post-delivery advisory。

- 结论直接性：不通过。用户明确询问支付终态，首答没有给出终态订单、成功订单、未支付订单和
  成功率总体结论。
- 分析完整度：不通过。`payment_outcome_compare` 已成功生成 2 个维度、93 个观察值，回答却没有
  使用这些材料；追问还把已存在的终态证据描述为未提供。
- 机制、替代解释：部分通过。付费金额公式和维度诊断较完整，支付成功率变化没有进入主叙事。
- 证据边界：通过。回答没有把未支付终态解释为失败原因，也明确拒绝失败环节、重试和耗时归因。
- 可读性：部分通过。段落和输入区遮挡验收通过，真实答案的章节标题和列表视觉层级仍可加强。
- 行动性：部分通过。给出了付费频次和维度调查建议，缺少针对支付终态变化的运营优先级。

该审阅不会撤回首次 publication，也不会自动提升为 runtime guardrail。下一阶段应从 obligation
到 narrative block 的完整性合同解决，而非针对本题添加文案模板。

## 性能

成功运行的 profile：
`analysis-performance-profile:sha256:bc99dc193dd39b37eae682ead7282819609d0cded291d8058e0e45b94afc9bc7`。

| 阶段 | 耗时 |
|---|---:|
| intent | 47.8 秒 |
| plan | 94.0 秒 |
| evidence | 102.0 秒 |
| coverage | 1.7 秒 |
| claim authority | 299.9 秒 |
| narrative | 455.1 秒 |
| delivery | 21.1 秒 |
| 初始轮端到端 | 1045.0 秒 |

目标 p50 300 秒、p95 480 秒仍未达到；profile 状态为 `breached`，执行策略保持
`audit_only / record_and_continue`。本轮删除了重复事件调度并将持久化叙事材料从约 2.86 MB
降到约 1.03 MB，解决了输入上限失败。后续性能工作应集中于 claim/narrative Provider 输入和
阶段编排，保留 required/disclosure 因素、claim verifier、recommendation verifier 与本地硬校验。

## 前端证据

- 真实 DeepSeek thread：`output/playwright/p6-real-deepseek/payment-final-outcome-thread.png`
- 标准桌面截图：
  `output/playwright/p6-standard/p6_answer_readability_browser/answer-readability-desktop.png`
- 标准移动端截图：
  `output/playwright/p6-standard/p6_answer_readability_browser/answer-readability-mobile.png`

浏览器硬验收通过：结构化段落有间距，长答案滚动区域没有被固定输入框遮挡，桌面和移动端均可
继续追问。

## 后续边界

- 支付过程事件、失败码、失败环节、重试链和耗时需要新事件级来源。
- 内部运营和投放事件等待 owner 数据后发布 source snapshot/release。
- 用户级注册到首充漏斗仍按已接受决议暂缓。
- 多 Agent 编排留到单 Agent 基线后的受控实验，继续共享 Plan、Evidence、Claim、Publication
  权威。

## 最终验证

- Phase 4：403 passed。
- Phase 7 + release manifest：1591 passed，40 skipped。
- P6 确定性标准包：4/4 passed。
- P6 真实 DeepSeek 标准案例：1/1 hard passed。
- P6 Playwright 标准案例：1/1 passed，桌面和移动端截图均已保存。
- TypeScript：`npx tsc --noEmit` passed。
- Next.js 生产构建：`npm run build` passed。
- 静态合同：28 个 YAML、10 个 capability card、25 个 support record passed。
- release manifest/deployment：14 passed，manifest version
  `single-authority.final.release.2026-07-23-v51`。
