# P4 执行计划：全因子覆盖、宽进深出与受控 BI 调查编排

状态：`completed`

目标：让每个已接受的 BI 变化解释在同一 pinned snapshot/release 上完成可审计的一级因素域
覆盖，先低成本筛查，再对有信号的因素做深度调查；拉新注册首充漏斗、充值档位、支付链路、
内部运营事件和外部事件进入同一调查拓扑。独立分支可以并行，最终仍由现有 evidence、claim、
`AuthorityBundle`、publication 和 delivery 单权威链结算。

## 继承的决议

- `IntentRevision`、`DecisionLedger`、accepted `PlanRevision`、能力执行、证据、claim、
  `AuthorityBundle`、publication 和 delivery 继续构成唯一 BI 权威链。
- 顶层 OpenAI Agents SDK 只承担单个应用轮次内的模型—工具循环。长任务、checkpoint、恢复、
  幂等和终局继续由 `AgentTurnRuntime`、PostgreSQL 和 BI LangGraph 管理。
- 大陆模型 Provider 是唯一模型出口；Provider timeout、retry 和熔断策略保持集中。
- 业务质量、完整性、解释深度、新颖性、行动建议和视觉评分进入 Workbench 与人工审核，
  不改变首次交付状态，不触发自动 writer retry、自动改写或撤回。
- 权限、固定敏感输出、SQL 安全、数据合同、snapshot/release、证据来源、claim provenance、
  持久化完整性和客户安全投影继续作为硬边界。
- 推荐承诺继续受 claim ceiling 硬校验；越界的可选建议进入 Workbench policy rejection 并被
  丢弃，已验证分析继续交付，不触发自动改写。
- 开放业务语义由 typed LLM binding 处理；运行时不使用关键词字典，不增加本地高价值回答模板，
  不为单个 eval 或偶发模型输出增加特例。
- 多 Agent 是 BI 工具内部的受控调查策略。主 Agent 保持唯一 thread 和最终回答权威；分支不能
  直接发布、修改 ThreadHead 或绕过 evidence/claim settlement。

## 第一性原理

### 因素覆盖先于文案生成

回答深度取决于调查输入。每个变化解释必须对 SSOT 中与目标指标有关的一级因素域形成明确
结算状态，避免模型只解释已经碰巧查到的几张结果表。

### 宽进深出

第一遍只做低成本、合同允许的候选筛查；第二遍只对有信号、用户明确要求或竞争解释需要的
因素下钻。客户回答突出主要结论、证据、竞争解释、局部限制和行动，不罗列全部后台筛查项。

### 多条公式分别对账

交易公式、拉新漏斗、支付漏斗、充值档位和聚合维度属于不同 reconciliation group。每组内部
必须闭合，组间只做交叉解释，禁止把不同分解路径的贡献率直接相加。

### 缺口也是调查结果

数据缺失、合同缺失、来源过期、粒度不支持和本次预算未覆盖都以 typed coverage outcome
结算。它们可以形成局部 boundary，不能伪装成无影响或系统故障。

## P4 合同

### FactorCoveragePlan

每个已接受 BI 调查创建内容寻址的 coverage plan，至少包含：

- accepted intent/plan revision refs；
- target metric、目标窗口、主基线、snapshot/release；
- 从 reviewed factor master 与 active capability support 解析出的一级因素域；
- 每个因素域的 applicability、screening capability、deep-dive capability、依赖、预算和停止条件；
- 必需、条件、辅助和 disclosure 角色；
- plan digest 与 active runtime contract digest。

开放语义 applicability 由 typed 模型 binding 建议；本地 compiler 只接受 reviewed catalog ID，
并校验 capability、dataset、grain、evidence 和 claim ceiling。

### FactorCoverageOutcome

每个计划因素域必须结算为以下一种状态：

- `analyzed`：深度调查完成；
- `screened_no_signal`：筛查完成，未达到深挖阈值；
- `unavailable_data`：当前 release 没有必需数据；
- `missing_contract`：来源存在但合同不允许当前分析；
- `unsupported_grain`：请求粒度不受支持；
- `not_applicable`：typed applicability 明确排除；
- `deferred_by_budget`：已记录价值与停止原因，本次预算未继续；
- `failed`：真实执行失败，附 retryability 和受影响义务。

Outcome 保存 query/result/artifact/evidence refs、信号摘要、限制、成本、时间和 digest。客户投影
只消费业务安全 summary、主要发现和相关限制。

### InvestigationBranch

可并行分支至少包含：

- branch ID、factor domain、hypothesis 与问题；
- accepted plan task 和 coverage item refs；
- 只读 capability allowlist；
- 固定 snapshot/release 和 context refs；
- source closure、预算、依赖与停止条件；
- 强类型 `InvestigationResult` 输出。

分支没有 ThreadHead、客户消息、publication、approval 和任意 SQL 权限。查询只能通过既有
capability/query compiler，恢复时严格重放原 branch spec 与工具选择。

### InvestigationSynthesis

统一综合只消费已结算 outcome 和闭合 artifact，输出：

- 已验证的方向与幅度；
- 各 reconciliation group 内部贡献；
- 排序后的主要因素与局部范围；
- 竞争解释及其状态；
- 不可相加、不可因果化和不可跨粒度外推的边界；
- 可执行下一步；
- claim/evidence/limitation refs。

综合不生成新事实。随后仍进入现有 claim settlement、narrative、publication 和 delivery。

## 因素域基线

P4 首批必须覆盖以下 SSOT 域，并允许后续通过 reviewed registry 增加新域：

1. `payment_order_metric_chain`：付费人数、次数、频次、单笔金额、支付发起与成功率；
2. `user_acquisition_and_first_payment`：新增、注册、注册率、首充、首次付费率、新增首日付费率；
3. `amount_tier_and_user_value`：充值档位、档位人数/金额/频次、高价值用户、首充与复充结构；
4. `payment_channel_and_method`：支付方式、渠道、失败、耗时和通道候选；
5. `marketing_channel_and_growth_ops`：渠道、大盘、投放背景和拉新结构；
6. `gameplay_and_betting`：玩法人数、频次、金额、返奖和同期关联；
7. `calendar_time_and_payday`：时间趋势、发薪、节假日和日内分布；
8. `product_operation_events`：产品、活动、首充礼包、支付流程、服务和运营事件；
9. `external_context_events`：赛事、天气、网络、电力、宏观、政策、社会事件和竞品候选；
10. `data_quality_and_evidence`：数据覆盖、来源新鲜度、对账和证据边界。

## 实施任务

### Task 0：基线、执行计划和 RED 测试

- 固化 active runtime contract、因子域、维度和 capability 快照。
- 新增 P4 合同测试，证明当前公式树遗漏漏斗、运行时维度遗漏充值档位、普通变化解释不会稳定
  结算外部/内部事件域。
- 保存当前单 Agent 运行时、查询数、模型调用数和客户回答作为比较基线。

### Task 1：FactorCoverage 合同与注册表

- 新增 SDK-neutral coverage plan/outcome/synthesis dataclass 与严格序列化校验。
- 从 reviewed factor ledger、metric/dimension/event contract 与 runtime registry 生成 active 因素域。
- 为每个 analysis goal 定义 coverage requirement；`explain_change`、`revenue_health_review`、
  `segment_or_factor_attribution` 和 `anomaly_or_black_swan_review` 必须结算适用一级域。
- coverage item、branch spec、branch outcome 与 synthesis 进入现有 append-only task/artifact/trace 身份链，
  不建立第二套历史。

### Task 2：漏斗、充值档位和支付链路

- 将 `new_user_funnel_dashboard` 接入公式轴，保持 daily dashboard grain 与 lifetime first-pay 边界。
- 把 `amount_bucket` 注册到 active runtime dimension catalog，使用已接受 NGN bucket policy。
- 增加档位结构 capability：目标/基线的金额、订单、用户、频次、组内均值和 mix contribution；
  source closure 与总额必须对账。
- 让支付发起次数、成功率、支付方式、金额档位和可用的失败/耗时证据形成独立 reconciliation group。
- 对来源缺失的 retention、lifetime first-pay、incident 和 payment-flow change 形成局部 typed boundary。

### Task 3：外部和内部事件

- 对变化解释、健康度、模式和异常问题执行低成本 event overlap screening；存在候选后才运行事件窗口
  深挖。
- 外部事件保留 context/candidate ceiling，校验 freshness、scope、authority 和 source digest。
- 内部运营事件只有在 owner-published snapshot 注册并进入 active release 后可执行；source unbound
  明确结算为 `unavailable_data`。
- 增加事件重叠、重复事件窗口、对照窗口和区域/渠道暴露差异能力；没有足够对照时禁止因果措辞。

### Task 4：受控 BI 调查编排

- 在 BI LangGraph 内增加 coverage compile → breadth screen → signal rank → deep branches → synthesis
  的可恢复拓扑。
- 独立分支并行，依赖分支串行；同一 run 默认遵守现有并发、Provider 和 exploration budget。
- 分支可以使用受控模型—工具循环，但 task/checkpoint/lease/recovery 仍由 WAJE 持久化运行时拥有。
- branch retry 只使用 Provider 统一策略和现有 durable task recovery，不增加业务节点重试循环。
- 一个分支失败只影响其 coverage outcome 和绑定义务；其他分支继续结算。

### Task 5：综合、连续追问和客户投影

- `InvestigationSynthesis` 进入 narrative material，但不能越过 AuthorityBundle 或新增事实。
- 主回答前部直接回应主问题；随后呈现驱动链、交叉证据、竞争解释、限制和行动。
- 用户追问证据时读取既有 synthesis/claim/evidence；material revision 只重跑受影响分支。
- 客户页面不展示 Agent 名称、branch ID、SDK 类型、内部 digest、Provider payload 或技术错误。
- Workbench 保存 coverage topology、每个分支工具/模型/错误 trace 和人工质量审核。

### Task 6：评测与真实环境验收

标准包增加：

- 拉新、注册、首充漏斗拆解；
- 充值档位 mix shift；
- 支付成功率、方式和金额档位交叉；
- 有外部事件、无外部事件、外部源过期；
- 内部事件源未绑定和已绑定；
- 多因素同时变化与 reconciliation group 防重复贡献；
- 单分支失败、恢复、重复 dispatch 和 catalog/snapshot 漂移；
- 连续追问和 material revision 的定向重跑。

硬门禁检查 route、coverage closure、query/result/artifact refs、数字日期单位、公式对账、证据上限、
幂等恢复、客户安全和大陆模型出站。人工评分继续只进入 Workbench review queue。

真实验收显式清除 `OPENAI_API_KEY`，验证 DeepSeek Chat Completions、WAJE-only trace、
`api.openai.com` 请求数为 0，并记录单 Agent 与 P4 编排的查询数、模型调用、延迟和人工评价。

### Task 7：版本与交付

- 更新 runtime contract version、schema migration、release manifest、coverage audit 和静态合同测试。
- 提供 P4 交付报告：实际数据覆盖、仍缺来源、所有测试、真实 Provider 结果、性能和后续 owner 待办。
- 不新建或重跑 Case B；P4 使用独立标准包和真实测试 thread。

## 完成标准

- 普通付费金额变化解释会结算适用一级因素域，不再只依赖核心交易公式和常见维度。
- 注册率、首次付费率、新增首日付费率进入可执行漏斗路径，且与 lifetime first-pay 明确区分。
- 充值档位进入 runtime 维度和贡献分析，并与总付费金额完成对账。
- 外部事件在相关问题中自动执行候选筛查；无事件时不生成事件解释，来源过期时明确边界。
- 内部运营事件未绑定时形成局部数据缺口；绑定后可以按同一事件合同执行。
- 独立 BI 调查可以并行、可恢复、可幂等重放，任何子分支都不能直接获得 claim 或 publication 权威。
- 最终综合保留每条结论的 evidence/claim/limitation refs，不跨 reconciliation group 重复相加。
- 客户得到一份完整、可读、面向运营决策的业务参考；人工质量评分不改变交付状态。
- 运行链仅访问大陆 Provider，OpenAI 托管请求和默认 trace exporter 使用数为 0。

## 完成证据

- runtime contract v17 已注册十个 reviewed 因素域，coverage plan、outcome、branch 与 synthesis 均以
  内容寻址身份进入现有权威链。
- 三组真实 DeepSeek BI 用例和一组双受控调查 Agent 用例通过；所有运行均清除
  `OPENAI_API_KEY`，出站为 `https://api.deepseek.com/chat/completions`，OpenAI 托管请求为 0。
- 最终充值档位运行 `run-5cc0e7ff183b652ceb12ae9e` 持久化了 10 个因素域的完整结算：6 个
  `analyzed`、4 个 `unavailable_data`，且主要因素排序引用 evidence-ledger identity。
- P4 标准包 13 个场景通过目录校验，10 个 deterministic 场景通过；Phase 7 全量在排除 Case B
  后为 1548 passed、40 skipped、13 deselected，Phase 8 为 16 passed。
- release manifest v45 已按最终合同、运行时、Gateway 和 eval artifacts 的内容摘要冻结。
- 交付细节见 `docs/reviews/2026-07-23-p4-full-factor-delivery.md`。
