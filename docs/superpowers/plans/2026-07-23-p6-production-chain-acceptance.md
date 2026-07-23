# P6 执行计划：完整 BI 链路验收与性能收敛

状态：`completed`

目标：把支付订单状态源落实为可查询、可对账、可审计的最终结果权威，随后以真实 DeepSeek、真实
ClickHouse/PostgreSQL 和真实前端完成标准 BI 问题族验收；在保留因素覆盖、证据、claim、verifier、
publication 和 delivery 权威的前提下收敛端到端时延。

## 继承的决议

- `IntentRevision -> DecisionLedger -> PlanRevision -> capability -> evidence -> claim ->
  AuthorityBundle -> publication -> delivery` 继续构成唯一权威链。
- PostgreSQL 继续承担 thread、task、checkpoint、artifact、release 和 trace identity 权威；
  ClickHouse 承担分析事实与聚合查询。
- P4 的十个因素域和 P5 的数据覆盖、对账、阶段性能 profile 继续生效。
- 回答必须完成已接受计划中的 required/disclosure 调查并发布明确边界；交付后回答质量审核保持
  advisory，不阻断首次发布，也不自动转成 runtime guardrail。
- 当前阶段跨来源完整验收截止 Lagos 业务日 2026-06-02。
- 内部运营与投放事件继续记录为 source-unbound 局部边界，后续接入；外部事件源保持已发布状态。
- 用户级注册—首充漏斗仍按现有决议暂缓，Market Dashboard 提供日级漏斗背景。
- 不引入关键词字典、单例回答规则、本地高价值模板或第二套分析权威。

## Task 0：真实支付状态审计与 RED 测试

- 确认源 grain、状态枚举、订单重复、状态重叠、时间字段、金额字段和 snapshot 截止语义。
- 固化 owner 已接受规则：`pay_success` 优先于同订单的 `order_success`；重复成功记录取最新完成时间。
- 为未知状态、重复终局、状态日期缺失、成功对账差异、原始 ID 泄漏和未发布 snapshot 先增加失败测试。

## Task 1：支付最终结果权威

- 用 `payment_final_outcome` 取代没有真实数据支持的 `payment_attempt` 合同。
- 固定结果枚举：`successful` 与 `not_paid_as_of_snapshot`；未知源状态 fail closed。
- 成功订单采用规范化成功记录；未成功订单采用只存在 `order_success` 且在 snapshot 中没有
  `pay_success` 的订单。
- 结果按支付发起 Lagos 业务日归属；成功记录缺少发起时间时允许回退到完成业务日，并单独审计覆盖率。
- 对账必须证明成功订单数和成功金额与 `paid_order_success` 完全一致；结果总订单按订单 ID 唯一。
- 发布支付最终结果分布、成功订单占比及支付方式/渠道聚合切片。

## Task 2：证据和回答边界

- 支持窗口级成功订单、未成功订单、总终局订单、成功占比及同期变化。
- `not_paid_as_of_snapshot` 只表示当前冻结数据中未形成 `pay_success`，不得解释为已知失败原因。
- 当前源不能发布失败码、失败环节、重试次数、支付链路漏斗、支付处理耗时或通道事故归因。
- 原始订单 ID、用户 ID、原始 Provider payload 和技术错误只进入 Workbench/审计。

## Task 3：完整 BI 纵向验收

- 标准集覆盖：变化解释、公式拆解、充值档位/高价值、市场漏斗、玩法与付费后行为、周期与异常、
  外部事件、支付最终结果、数据质量边界、追问和质疑修订。
- 每个案例保存自然语言问题、结构化期望、运行状态、因素覆盖、查询/证据/claim 引用、客户答案、
  Workbench trace、前端截图和人工点评。
- 硬验收检查流程完整、权威闭合、证据强度、客户安全和恢复；完整度、深度、可读性、行动性作为
  post-delivery review 字段，不改变 publication 结果。
- 真实环境显式清除 `OPENAI_API_KEY`，模型请求只允许进入 MainlandModelProvider 的 DeepSeek
  Chat Completions endpoint，并运行无 `api.openai.com` 出站断言。

## Task 4：性能收敛

- 从 P5 `analysis-performance-profile.v1` 读取真实阶段耗时和输入规模，先处理重复查询、重复材料化、
  无效 source-unbound 分支调用和 verifier 重复输入。
- 保留 required/disclosure 因素覆盖、完整 writer 材料、claim verifier、推荐验证和本地硬校验。
- 完整调查目标仍为 p50 300 秒、p95 480 秒，超限行为继续是 `record_and_continue`。
- 在同一标准案例与同一数据 release 上比较优化前后时延，禁止用减少调查深度换取达标。

## Task 5：验收标准

- 支付最终结果 snapshot/release 可重复构建，未知状态和对账差异 fail closed。
- 支付成功订单及金额与 `paid_order_success` 精确一致，查询只返回聚合结果。
- 运行时不再把不存在的支付尝试明细宣传为已接入能力。
- 至少完成一组真实 DeepSeek 端到端标准案例，保存前端截图和 Workbench 证据。
- 无 `OPENAI_API_KEY`、无 OpenAI 托管模型/Responses/Conversations/Trace/Evals、无
  `api.openai.com` 出站。
- Python、Provider、Phase 4/7、前端静态合同、release manifest 和 diff 检查通过。

## 后续阶段边界

- 支付尝试事件、失败码、重试链和处理时延需要新的事件级来源，不能从本阶段终局状态反推。
- 内部运营与投放事件等待 owner 数据后进入新 source snapshot/release。
- 多 Agent 编排只在 P6 单 Agent 基线形成后做受控实验，共享同一 Plan/Evidence/Claim/Publication 权威。
