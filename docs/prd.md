# WAJE BI v2 PRD 草案

状态：v0.1 signoff draft  
范围：第一版生产级 baseline，覆盖 `付费金额` 影响分析与回溯类问题  
来源：PRD 访谈与 [docs/product-decisions.md](/Users/luka/work/waje-bi-v2/docs/product-decisions.md:1)

## 0. 术语表

来源：PRD review P2-2。

| 术语 | 含义 |
| --- | --- |
| `accepted graph` | 经 WAJE 本地 compiler、policy、contract、permission 校验后允许执行的分析图。 |
| `capability card` | 给 LLM 和本地 compiler/verifier 共同使用的能力说明，描述能力用途、参数边界、证据输出、lint 和降级规则。 |
| `factor ledger` | 从 `付费金额影响因子分析.mm` 生成并经双 owner 审阅的因子支持状态层。 |
| `claim group` | Answer Package 中可被前端渲染和 verifier 检查的一组业务结论。 |
| `Answer Package` | 后端生成的完整答案包，包含意图、scope、baseline、证据、claim、visualization plan、降级路径和 verifier 结果。 |
| `evidence envelope` | capability 输出的统一证据外壳，承载 scope、claim、证据类型、强度、限制和引用。 |
| `business object impact` | 用户指定某个业务对象后，系统复盘该对象对目标指标的影响。业务对象可以是活动、投放、版本、外部事件、指标驱动、维度或分群。 |
| `question tool` | LangGraph 流程中的澄清交互节点。打开后可阻塞当前 run，用户选择后继续。 |
| `verifier` | 校验最终答案中数字、scope、baseline、证据强度、claim wording 和可视化表达的本地组件。 |

## 1. 产品目标

来源：PRD 访谈、product decisions 中 WAJE BI v2 baseline 决策。

WAJE BI v2 是 clean-slate SQL-first BI Agent，用于回答 `付费金额` 影响与回溯类业务问题。

第一版是生产级 baseline，需要覆盖核心分析能力、workflow、证据契约、verifier、用户可读解释和 launch acceptance。旧 WAJE 只作为业务口径、样例数据、失败案例、部分算法和测试经验参考。

## 2. 核心原则

来源：PRD 访谈、LangGraph/WAJE 边界讨论。

- SQL-first：分析真相来自语义合同、受控 SQL 编译、校验查询、查询执行、证据和 verifier。
- LLM 只提出意图、假设、candidate graph、澄清选项和叙述草案。
- WAJE-owned 本地系统负责语义合同、SQL 编译/校验、权限、证据、claim strength、Answer Package 和 verifier。
- LangGraph 负责 workflow 可视化、checkpoint、loop、branch、trace、runtime event 和节点进度。
- Dify 不进入第一版。
- `付费金额影响因子分析.mm` 是业务 SSOT，factor ledger 是可审阅、可版本化、可对账的运行支持状态层。
- recipe 是业务入口模板，accepted graph 可以在本地校验后偏离 recipe。
- 首条月初问题是 `pattern_explanation` 问题域的验收样例，禁止过拟合为单题专用逻辑。

## 3. 用户与任务

来源：PRD 访谈。

主要用户：

- 经营负责人：解释收入和付费金额变化。
- 运营团队：复盘活动、投放、版本、外部事件、异常。
- 数据/BI 负责人：判断结论是否可信，维护合同和证据边界。
- 管理层读者：阅读简洁结论、关键证据和限制。

主要任务：

- 解释 `付费金额` 为什么变化。
- 判断某个周期、窗口、事件相对模式是否存在。
- 复盘某个业务对象对目标指标的影响。
- 判断收入健康状态与风险来源。
- 找出最能解释变化的维度、因子或组合。
- 复盘某天、某月、某窗口、某维度的异常和黑天鹅候选。
- 与用户指定或系统推荐的 baseline 对比。
- 审查数据质量、证据充分性和 claim 边界。

## 4. 支持的问题族

来源：PRD 访谈，P1-2 命名确认。

| 问题族 | 用户问题 |
| --- | --- |
| `paid_amount_change_explanation` | 为什么付费金额上涨、下跌或变化？ |
| `pattern_explanation` | 为什么某个时间、窗口、事件相对或分群模式存在？ |
| `business_object_impact_review` | 某个业务对象对目标指标有什么影响？ |
| `revenue_health_review` | 最近收入/付费金额健康吗？风险在哪里？ |
| `segment_or_factor_attribution` | 哪些维度、因子或组合解释了变化/差异？ |
| `anomaly_or_black_swan_review` | 是否异常？异常集中在哪里？发生了什么？ |
| `custom_baseline_comparison` | 和指定或推断 baseline 相比，为什么变化？ |
| `data_quality_or_evidence_review` | 这个结论证据够不够？数据有没有问题？ |

一个用户问题可以命中多个问题族。系统输出一个 accepted graph，由多个 capability 组合执行。

## 5. UX 与 demo 边界

来源：PRD 访谈、P2-3。

产品体验是 Codex-like investigation thread，参考 21st Agent Elements 风格。

运行中展示业务化过程事件：

- 意图和 scope 理解
- accepted plan 和 todo 进度
- tool group 和 capability 进度
- question tool 澄清
- repair、degrade、block、skip
- 证据摘要和限制
- verifier 结果

完成后默认展示业务答案，过程流折叠为 process summary，并可展开。

本地 demo 只作为 UX pattern 参考：

- [app/page.tsx](../app/page.tsx)
- [Gateway 消息入口](../app/api/threads/%5BthreadId%5D/messages/route.ts)
- [Gateway 运行事件](../app/api/runs/%5BrunId%5D/events/route.ts)

demo 只证明会话创建、业务问题输入、澄清交互、运行进度和 Answer Package 展示的体验方向。demo 细节不定义生产协议、架构、graph contract、data contract 或实现承诺。

## 6. Question Tool

来源：PRD 访谈、P1-4。

question tool 用于会改变主结论、claim 边界、scope、baseline、时间语义、权限路径或执行成本的歧义。

规则：

- LLM 可以提出澄清候选。
- 本地 policy 决定是否打开 question tool。
- 一次澄清最多包含 3-4 个短问题。
- 每个问题最多 3 个选项。
- 选项应尽量给出推荐推断。
- 必须包含固定出口：`tell the agent to do differently`。
- question tool 打开后可以阻塞当前 run，直到用户选择、接受推荐推断或给出新的指令。
- 低风险缺口不打开 question tool，直接按推荐推断继续。
- accepted graph 和 Answer Package 必须记录 `user_selected`、`recommended_inference_selected`、`agent_instructed_differently` 或 `system_inferred`。

`tell the agent to do differently` 处理流程：

1. 用户输入新的方向或约束。
2. LLM 进入 intent rebinding 或 targeted graph repair。
3. 本地 compiler 重新校验 capability、参数、权限、合同、budget 和 evidence 输出要求。
4. 可执行时继续，并在 accepted graph 记录 mutation reason。
5. 不可执行时给出业务化拒绝、repair 或 downgrade 说明。

## 7. Answer Shape

来源：PRD 访谈、P0-5。

答案由 verified `claim group` 和 validated `visualization_plan` 动态生成。

首屏稳定信息层级：

- 主结论
- 关键量化
- 主要驱动或解释
- 例外或分歧
- 证据边界和限制

卡片数量和顺序不固定。卡片跟随 accepted graph 和 verified evidence 生成，可以包含 baseline stability、formula contribution、attribution、business object impact、pattern evidence、anomaly、data quality、missing contract、permission limit 等。

主结论按业务解释力排序，每条结论必须带证据强度和 claim 边界。

### 7.1 Claim Group 最小产品契约

来源：P0-5。

每个 `claim group` 至少包含：

- conclusion text：业务结论文本
- scope：结论适用范围
- baseline：使用的比较基准
- target metric：目标指标
- evidence refs：支撑证据引用
- evidence type：证据类型
- strength：证据强度
- supported wording：允许使用的措辞
- disallowed wording：禁止使用的措辞
- limitations：限制、缺口、降级原因
- related visual blocks：相关可视化块
- verifier status：通过、降级、阻断或需修复

## 8. Evidence 与 Claim 边界

来源：PRD 访谈、verifier 讨论。

每个 claim 必须绑定 scope、baseline、evidence 和 allowed wording。

Evidence type：

- `accounting_contribution`：公式、bridge、decomposition、segment delta。
- `statistical_association`：周期、相关、lag、稳定性。
- `candidate_mechanism`：有业务合理性、时间或结构支持的候选机制。
- `causal_evidence`：有对照、反事实、实验、干预设计等更强证据。
- `insufficient`：路径存在，但数据、覆盖、稳定性或方法不足。

Strength：

- `high`
- `medium`
- `low`
- `insufficient`

规则：

- 贡献与因果影响必须分开。
- 候选机制可以进入答案，但要绑定证据强度和限制。
- 缺合同、unsupported grain、稀疏数据或权限限制必须降级或阻断 claim path。
- 弱证据不能只藏在展开详情里。
- verifier 检查最终 wording、数字、scope、evidence refs、disabled/degraded paths、allowed claim/evidence type 和 allowed strength/wording limit。

## 9. 问题族需求

来源：PRD 访谈。

### 9.1 `paid_amount_change_explanation`

目标：解释 `付费金额` 为什么变化、主要驱动是什么、影响多大、证据哪里有限。

默认 operating-review spine：

- 绑定 intent、scope、baseline、时间语义。
- 执行 data quality checks。
- 拆解 metric formula。
- 检查 pattern。
- 复盘 anomaly。
- 执行 attribution 和 segment bridge。
- 检查 business object evidence。
- 合成证据。
- verifier 校验答案。

行为：

- 识别异常定位、业务对象影响复盘等语义，并合并相关 graph branch。
- 用户未指定 baseline 时，LLM 推荐一个或多个业务合理 baseline。
- baseline binding 是显式 graph step。
- 多个 baseline 都有价值且成本可接受时，可以同时执行。
- baseline 分歧要约束最终结论。
- 执行深度由证据驱动。残差大、baseline 分歧、贡献不稳定、例外集中、事件窗口强重合、verifier 风险会触发深入。
- 弱且不改变主结论的信号进入 follow-up 或 limitation。

首屏：

- 动态业务结论
- 关键数字和 baseline 稳定性
- 主要驱动
- 例外或分歧
- 证据边界

验收风险：

- 正常经营复盘
- baseline 分歧
- 周期模式误判
- 异常主导变化
- business object 候选解释
- missing contract
- data quality issue
- 过强因果措辞
- permission-limited evidence

### 9.2 `pattern_explanation`

目标：验证、量化、解释并约束时间、窗口、事件相对、cohort、segment 或 baseline 模式。

支持的 pattern family：

- intra-period
- weekly、monthly、quarterly、yearly seasonality
- event-relative windows
- pre/post windows
- lag/recovery windows
- rolling windows
- custom baselines
- 合同支持的 cohort-related patterns
- grain 和权限支持的 segment-level pattern comparison

行为：

- 用户可以提出候选模式，系统验证、量化、找例外、解释候选机制。
- 允许受控 sibling-pattern exploration。用户问 pattern A，证据显示 pattern B 更解释数据时，答案先回应 A，再指出 B。
- 探索受用户意图、业务上下文、budget、candidate windows、SSOT、contracts、data quality 和 claim-strength policy 约束。
- 每次运行不全扫所有 pattern family。
- pattern 是否成立看 recurrence、业务显著幅度、稳定性、例外和数据质量。
- 可解释例外不直接否定整体模式。

首屏：

- 用户问的模式是否成立
- 是否存在更精确 sibling pattern
- 业务解释
- 例外边界
- 按 pattern family 选择 inline visual blocks

可视化示例：

- intra-period：phase comparison
- weekly：weekday profile
- event-relative：event timeline
- rolling：trend band
- lag/recovery：lag curve

验收风险：

- 候选模式成立
- 候选模式不成立
- sibling pattern 更强
- 可解释例外
- event candidate mechanism
- data quality issue
- missing contract
- 误判为 period-over-period 或 cumulative-value analysis
- 过强因果措辞

### 9.3 `business_object_impact_review`

目标：复盘用户指定的业务对象是否影响目标指标、影响多大、通过什么业务路径影响。

业务对象包括：

- 节假日、日历事件
- 活动、campaign
- 投放
- 产品版本
- 运营动作、push
- 外部事件
- 新增用户、付费用户、支付成功率、订单数、客单价等指标驱动
- 以 impact 方式提问的维度或分群

主证据路径：

- action/intervention objects：event/intervention evidence
- metric drivers：formula decomposition 和 contribution evidence
- dimensions/segments：segment bridge 和 attribution evidence
- external context：contextual event evidence，并限制 claim wording

行为：

- LLM 推荐 comparison/control，compiler 校验。
- comparison choice 会改变结论时打开 question tool。
- 缺严格对照或完整干预数据时，可以降级为 association 或 candidate-impact。
- trend co-movement 或 temporal overlap 不能直接支持 confirmed net impact。

首屏：

- impact judgment
- impact magnitude
- likely business path
- comparison/control boundary

可视化：

- pre/post comparison
- exposure timeline
- formula contribution
- segment bridge
- attribution ranking
- control comparison

验收风险：

- activity/campaign/version impact
- ad spend 或 operation action impact
- metric-driver impact
- dimension/segment impact
- external context event
- missing controls
- missing exposure details
- wrong time window
- correlation 写成 causality
- no measurable impact
- local-segment-only impact

### 9.4 `revenue_health_review`

目标：判断收入或付费金额是否健康、风险在哪里、问题来自哪里。

默认检查：

- trend
- structure
- funnel/formula
- anomaly
- data quality

典型检查：

- paid amount trend
- paid users、paid order count、success rate、average amount decomposition
- channel、user、payment-method structure
- abnormal windows
- completeness 和 metric-contract issues

健康判断结合：

- 明确业务目标
- historical baseline
- year-over-year 或 period-over-period context
- volatility band
- structural risk
- data quality

首屏：

- health judgment
- risk sources
- key evidence
- suggested follow-up investigations

风险分层：

- high risk：影响大且证据强
- attention item：潜在影响大但证据中等或局部
- watch item：弱信号或局部信号
- data risk：数据质量限制判断

验收风险：

- healthy revenue
- unhealthy revenue
- worsening trend with healthy structure
- stable total with deteriorating structure
- payment-chain risk
- anomaly-dominated health issue
- data-quality-limited judgment
- target deviation

### 9.5 `segment_or_factor_attribution`

目标：找出最能解释变化、差异、模式、影响或例外的维度、因子或组合。

行为：

- 从一维候选筛选开始，建立业务直觉、候选池和残差模式。
- 单因子不稳定、残差大、因子内部差异明显、业务机制指向交互、verifier 风险需要更强证据时，进入组合归因。
- 进入组合归因后，二维组合是默认起点。
- 更高阶组合需要额外证据、业务相关性、稳定性和 budget 支持。

候选池：

- SSOT-registered candidates
- distribution shift、structure change、anomaly、new value、residual pattern 中发现的数据候选
- 经 ledger、contract、permission、evidence 检查后的 LLM/user hypotheses

首屏：

- actionable business explanation
- contribution magnitude
- stability
- coverage
- evidence boundary

规则：

- attribution 主答案不能变成 Top-N list。
- contribution 和 causal impact 必须分开。
- segment bridge、formula decomposition 或 attribution evidence 支持时，可以说 quantified contribution。
- causal wording 需要 intervention、control、mechanism 或更强 causal evidence。

验收风险：

- 一维解释充分
- 一维误导，需要二维归因
- 二维解释充分
- 需要高阶组合
- 局部组合不能泛化
- sparse sample risk
- permission-limited evidence
- contribution wording 被错误提升为 causal wording

### 9.6 `anomaly_or_black_swan_review`

目标：判断异常是否真实、异常在哪里、哪些业务或数据解释有证据。

行为：

- 先排 data quality、metric contract、time semantics、permission、cumulative-value issue。
- 再识别异常 time window、dimension、segment、metric component、event 或 combination。
- 再测试内部动作、结构变化、外部冲击和 black-swan candidates。
- anomaly 检测泛化到 ledger、contract、grain、permission、budget 支持的任何维度、因子、指标组件、分群、时间窗口、事件窗口或组合。
- black-swan 是异常解释候选的一类。大异常不能自动标为 black-swan。

首屏：

- anomaly conclusion
- affected scope
- likely explanations
- ruled-out paths

验收风险：

- true anomaly
- pseudo-anomaly or data issue
- local segment anomaly
- metric-chain anomaly
- internal-action explanation
- external black-swan candidate
- unsupported black-swan misclassification
- permission or grain-limited anomaly evidence

### 9.7 `custom_baseline_comparison`

目标：对比用户指定或系统推荐的 baseline，解释差异和业务原因。

行为：

- 用户指定 baseline 优先。
- baseline 不完整时，LLM 推荐 period-over-period、year-over-year、same weekday、event-relative、target value 或 similar-window。
- baseline 歧义会改变结论时打开 question tool。
- 用户无偏好时，按推荐 baseline 继续并记录。
- multiple-baseline disagreement 进入首屏结论边界。

首屏：

- target vs baseline conclusion
- baseline disagreement 是否改变解释
- difference magnitude
- main explanation paths
- evidence and data-quality boundaries

验收风险：

- user-specified baseline
- system-recommended baseline
- multiple-baseline disagreement
- event-relative baseline
- same-weekday or similar-window baseline
- target deviation
- cumulative-value misuse
- wrong time semantics
- unavailable comparable window

### 9.8 `data_quality_or_evidence_review`

目标：判断结论是否可信，数据、证据、合同、权限或 claim-strength 限制在哪里。

默认检查：

- data quality
- contract coverage
- permissions
- evidence strength
- claim wording

检查项：

- missing or duplicate data
- cumulative-value misuse
- time semantics
- metric contracts
- dimension/event contracts
- permission limits
- evidence sufficiency
- answer wording exceeding supported claim strength

首屏：

- trust judgment
- affected scope
- claims that need degradation
- recommended data or contract fixes

验收风险：

- trustworthy main conclusion
- local claim degradation
- missing contract
- permission limit
- cumulative-value misuse
- time-semantics error
- insufficient evidence
- over-strong wording
- upgrade after data or contract improvement

## 10. 首条 Vertical Slice Expectation Package

来源：P0-2。该包是 regression eval 起点，作为 `pattern_explanation` 泛化问题域样例。

### 10.1 输入

```text
全量样本看，为什么从 2024 年 1 月开始到 2026 年 5 月结束，为什么每个月月初的付费金额都比月中/月末高一些
```

### 10.2 期望识别

- expected family：`pattern_explanation`
- merged families：可合并 `paid_amount_change_explanation`、`segment_or_factor_attribution`、`business_object_impact_review`、`data_quality_or_evidence_review`
- scope：full sample
- time range：2024-01 到 2026-05
- pattern family：intra-period
- target metric：`付费金额`

### 10.3 必需证据路径

以下路径适用于同类 `pattern_explanation` launch case。首条 slice 使用月初 / 月中 / 月末窗口；其他 pattern family 复用“识别、证明、量化、解释候选机制、找例外、验证答案”的结构，并替换对应窗口、候选机制和 visual blocks。

首条 slice 默认要求这些 evidence paths 出现在 accepted graph 中，或记录可审计的降级、跳过、阻断原因。

- `data_quality_check`
- `pattern_scan`
- `formula_decompose`
- `event_evidence`
- `segment_bridge` 或 `joint_attribution`
- `outlier_scan`
- `answer_verify`

### 10.4 禁止路径

- 不得识别为普通环比/同比变化归因。
- 不得识别为成本期变化问题。
- 不得把累计值当作窗口值。
- 不得只用 pooled average 证明“每个月都更高”。
- 不得把 payday、活动、节假日写成已证明因果，除非证据达到对应强度。

### 10.5 默认窗口

- 月初：1-10 日
- 月中：11-20 日
- 月末：21 日到自然月月底

`pattern_scan` 仍需支持自定义窗口、事件相对窗口、rolling、lag/recovery 等泛化场景。

### 10.6 Pattern 判断标准

首条 slice 的 regression eval 默认按 2024-01 到 2026-05 共 29 个自然月计算。数据不完整月份不进入强度分母，但必须进入 exception list 或 data quality limitation。

系统可以说“月初模式成立”时，需要同时满足：

- 覆盖：complete comparable month >= 24；低于 24 时最多表达弱信号或数据不足。
- 方向一致：月初日均 `付费金额` 同时高于月中和月末的月份 >= comparable months 的 70%。
- 幅度：月初相对月中、月末中较高者的 median uplift >= 3%，或达到 metric contract 声明的 materiality threshold。
- 稳定性：标注异常月份后，剩余 comparable months 仍满足方向一致阈值。
- 例外：所有未满足方向一致、数据缺失、口径异常或 outlier 主导的月份必须出现在 exception list。
- 降级：方向一致在 60%-70% 时只能说“存在倾向”；低于 60% 时不能发布 broad recurring pattern claim。
- 阻断：data quality check 发现会破坏主结论的问题时，主结论进入 data-limited 或 blocked。

这些阈值只定义首条 slice 的 launch regression 默认。通用 `pattern_scan` 需要支持按 pattern family、metric contract 和业务 materiality 配置不同阈值。

### 10.7 候选机制

候选机制从 SSOT 和 ledger 出发，可包含：

- payday 一致性事件维度
- weekday mix
- holiday
- activity/campaign
- channel structure
- new/returning users
- payment success rate
- payment method
- device/region 等合同支持维度
- outlier 或 black-swan candidate

缺合同机制可以作为 candidate mechanism，但必须说明缺口和 claim 限制。

### 10.8 期望首屏

- 回答月初模式是否成立。
- 给出关键量化，例如方向一致月份、幅度、例外。
- 说明更精确 sibling pattern 是否存在。
- 说明公式拆解与主要业务解释。
- 区分 supported explanation、local/exception explanation、insufficient/ruled-out path。
- 给出证据强度和主要限制。

### 10.9 期望 visual blocks

- phase comparison：月初 / 月中 / 月末对比。
- exception list：异常月份和异常原因候选。
- formula contribution：付费金额公式拆解。
- candidate explanation ranking：候选机制及证据强度。
- evidence boundary：缺合同、unsupported grain、数据质量限制。

### 10.10 Verifier pass/fail

Pass：

- 问题族正确。
- required evidence paths 出现在 accepted graph 或有合理降级记录。
- 禁止路径未出现。
- 关键数字有 evidence refs。
- 候选机制措辞未越过证据强度。
- visual blocks 绑定已验证 evidence。

Fail：

- 误判为环比/同比变化分析。
- 使用累计值支持窗口结论。
- 隐藏 data quality 或 missing contract。
- 把 candidate mechanism 写成 confirmed cause。
- 没有说明例外月份或 pattern 稳定性。

## 11. SSOT 与 Factor Ledger

来源：P1-1、P0-1。

`付费金额影响因子分析.mm` 是业务来源树。factor ledger 是可审阅、可版本化、可对账的运行支持层。PRD 只定义概念与运行影响，最终表结构进入技术设计。

### 11.1 Ledger 概念字段

- business meaning
- data status
- supported question families
- supported capabilities
- supported grain
- allowed claim/evidence type
- allowed strength / wording limit
- known gaps
- owner/review status
- upgrade path

支持状态按 factor、question family、capability、supported grain、claim type 表达。

Ledger statuses 与 launch acceptance 的 `data_contract_state` 保持一致：

- `contract_backed`
- `evidence_linked`
- `static_assumption`
- `missing_contract`
- `permission_limited`
- `unsupported_grain`
- `out_of_scope_for_now`

### 11.2 `.mm` 到 ledger 审阅流水线

1. 从 `.mm` 抽取 metric、factor、dimension、event、formula、missing contract。
2. 生成 review artifact。
3. business owner 审 business meaning、解释有效性、claim boundary。
4. data/engineering owner 审 data contract、grain、permission、可执行 capability。
5. 为每个节点分配 ledger status。
6. 自动检测 missing node、status conflict、unsupported claim/evidence type、unsupported wording limit、缺 backlog 项。
7. 版本化 accepted ledger source。
8. 发布 runtime mirror 给 graph compiler、capability、verifier 使用。

Launch acceptance 要求每个 relevant SSOT node 都有明确 ledger status。核心问题族不能带 invisible SSOT gaps 上线。

### 11.3 Payday 建模

payday 是一致性事件维度，默认窗口为每月 `25..30`，适用于 WAJE 初始业务 framing 的相关分析。Phase 1 只维护这一条 payday 维度；claim strength 由窗口证据、稳定性和 verifier 决定。

## 12. Capability Cards

来源：P0-4。

capability card 是给 LLM、graph compiler 和 verifier 共同使用的产品契约。写法使用业务语言优先，并附结构化约束。不向 LLM 暴露物理数据库 schema。

每张 card 至少包含：

- business use
- non-use
- key parameters
- evidence output
- lint rules
- degradation rules
- typical question families

每张 card 还要给 compiler/verifier 一个最小结构化 spec：

- required parameters：缺少时进入 targeted repair 或 block。
- optional parameters：可由 LLM 推荐或 compiler 默认补齐。
- evidence payload：typed payload 名称和 required evidence fields。
- lint severity：`block`、`repair`、`degrade`、`warn`。
- degradation output：降级后允许的 evidence type、strength 和 wording limit。
- verifier hooks：最终答案必须检查的 claim、数字、scope 或 visual block。

Baseline card spec：

| Capability | Required parameters | Evidence payload | Hard lints | Degraded output |
| --- | --- | --- | --- | --- |
| `pattern_scan` | target metric, scope, time range, grain, pattern family, window definition | `pattern_evidence_package` | illegal window/grain, cumulative-value misuse | weak association or insufficient with exception list |
| `formula_decompose` | metric, contract version, scope, baseline, time range | `formula_decompose_result` | missing metric contract, failed reconciliation | formula claim degraded or blocked |
| `joint_attribution` | target claim, candidate pool, scope, baseline, metric, budget | `joint_attribution_result` | unsupported candidate, sparse-cell violation, forbidden grain | local contribution or insufficient |
| `event_evidence` | business object, event window, scope, target metric, baseline/control | `event_evidence_result` | missing event timing for strong claim, forbidden data | contextual evidence or candidate mechanism |
| `outlier_scan` | metric, scope, time range, grain, expected baseline | `outlier_scan_result` | missing baseline, cumulative-value risk | local anomaly or data-limited finding |
| `segment_bridge` | metric, scope, segment, baseline, time range, grain | `segment_bridge_result` | unsupported segment grain, permission failure | local segment claim or permission-limited claim |
| `data_quality_check` | scope, metric, time range, grain, contracts, permission context | `data_quality_result` | invalid metric identity, destructive completeness issue | data-limited or blocked claim path |
| `answer_verify` | draft answer, claim groups, evidence refs, visualization plan | `answer_verifier_result` | unsupported main claim, numeric mismatch, over-strong wording | targeted repair, downgraded claim, or blocked answer |

### 12.1 `pattern_scan`

- business use：验证和量化周期、窗口、事件相对、rolling、lag/recovery、custom baseline 等模式。
- non-use：不直接解释业务原因，不替代 attribution 或 event evidence。
- key parameters：target metric、scope、time range、grain、pattern family、window definition、baseline、filters、quality checks。
- evidence output：pattern identity、existence、quantification、comparison、exceptions、quality flags、downstream hints。
- lint rules：不能只用 pooled average 支持 recurring claim；必须检查 time semantics、completeness、cumulative-value risk。
- degradation rules：样本不足、窗口缺失、unsupported grain、弱稳定性时降级为 weak association 或 insufficient。
- typical families：`pattern_explanation`、`paid_amount_change_explanation`、`custom_baseline_comparison`、`business_object_impact_review`。

### 12.2 `formula_decompose`

- business use：按 metric contract 拆解 `付费金额` 及相关指标，解释变化来自订单数、成功率、客单价、用户数等组件。
- non-use：不能发明合同外公式，不能替代 segment attribution。
- key parameters：metric、decomposition path、scope、baseline、time range、grain、filters。
- evidence output：component levels、delta、contribution、residual、reconciliation status。
- lint rules：必须使用声明过的 metric contract；必须做 reconciliation；累计值和期间值必须区分。
- degradation rules：缺字段、unsupported grain、无法 reconcile 时降级或阻断对应 formula claim。
- typical families：`paid_amount_change_explanation`、`revenue_health_review`、`business_object_impact_review`、`custom_baseline_comparison`。

### 12.3 `joint_attribution`

- business use：筛选和升维组合因子，解释变化、差异、模式、impact 或 exception。
- non-use：不能把 contribution 排名直接写成 causality。
- key parameters：target claim、candidate pool、scope、baseline、metric、time range、max depth/budget、sparse limits。
- evidence output：candidate ranking、组合贡献、residual reduction、stability、coverage、sparsity warnings、tested candidates。
- lint rules：进入组合归因后从二维起步；高阶组合需要业务相关性、稳定性和 budget 支持。
- degradation rules：稀疏样本、权限受限、局部组合无法泛化时限制 claim。
- typical families：`segment_or_factor_attribution`、`paid_amount_change_explanation`、`pattern_explanation`。

### 12.4 `event_evidence`

- business use：评估业务对象、事件窗口、外部上下文、payday 等候选机制与指标变化的关系。
- non-use：不能仅凭时间重合生成 confirmed impact 或 causality。
- key parameters：business object、event window、scope、target metric、baseline/control、lead/lag/recovery window。
- evidence output：window alignment、pre/post comparison、lag/recovery、overlap/stability、limitations。
- lint rules：无对照时默认 candidate impact 或 contextual evidence；强影响需要更强合同或对照证据。
- degradation rules：缺 event timing、缺 exposure、缺 control 时降级。
- typical families：`business_object_impact_review`、`pattern_explanation`、`anomaly_or_black_swan_review`。

### 12.5 `outlier_scan`

- business use：发现异常时间、维度、指标链路、事件窗口或组合，支持异常和例外解释。
- non-use：不能自动把大异常写成 black swan。
- key parameters：metric、scope、time range、dimension candidates、grain、expected baseline、sensitivity/budget。
- evidence output：outlier periods、outlier segments、affected metrics、candidate explanations、ruled-out paths。
- lint rules：必须先排 data quality、time semantics、cumulative-value risk。
- degradation rules：异常只在局部或权限受限时限制 claim。
- typical families：`anomaly_or_black_swan_review`、`paid_amount_change_explanation`、`revenue_health_review`。

### 12.6 `segment_bridge`

- business use：解释 segment mix、结构变化和分群贡献，支持从总体变化落到可行动分群。
- non-use：不能替代高阶组合搜索，也不能绕过 unsupported grain。
- key parameters：metric、scope、segments、baseline、time range、grain、filters。
- evidence output：segment contribution、mix shift、coverage、local vs broad boundary。
- lint rules：分群证据不能支持超出 scope 的全局 claim。
- degradation rules：低覆盖、权限受限、unsupported grain 时降级。
- typical families：`segment_or_factor_attribution`、`business_object_impact_review`、`revenue_health_review`。

### 12.7 `data_quality_check`

- business use：检查结论能否被数据和合同支持。
- non-use：不能替代业务解释。
- key parameters：scope、metric、time range、grain、contracts、permission context、expected output paths。
- evidence output：completeness、duplicates、time semantics、metric identity、cumulative-value guard、permission coverage、sample size。
- lint rules：高风险查询和强 claim 必须有 data quality check。
- degradation rules：发现关键质量问题时阻断或降级相关 claim。
- typical families：全部问题族。

### 12.8 `answer_verify`

- business use：校验最终答案的数字、scope、baseline、证据、wording 和 visual blocks。
- non-use：不能生成新的业务证据，不能替代 capability 输出。
- key parameters：draft answer、claim groups、evidence refs、disabled/degraded paths、visualization plan。
- evidence output：verifier status、failed claims、required wording changes、blocked claims、accepted answer package.
- lint rules：所有 first-screen claim 必须可追溯到 evidence refs；candidate mechanism 不能写成 confirmed cause。
- degradation rules：verifier 失败时禁止发布强结论，需修复、降级或阻断。
- typical families：全部问题族。

## 13. Accepted Graph Lifecycle

来源：P0-3。

Graph/node 状态：

- `proposed`：LLM 提出的 candidate node 或 graph。
- `accepted`：本地 compiler 校验后接受。
- `auto_added`：compiler 自动加入 deterministic guardrail。
- `repair_requested`：compiler 要求 LLM targeted repair。
- `repaired`：LLM 修复后再次通过 compiler。
- `running`：LangGraph 正在执行。
- `completed`：节点完成并产出证据。
- `degraded`：节点可执行但证据、合同、权限或质量限制了 claim。
- `blocked`：安全、权限、合同、SQL safety 或非法 claim path 阻断。
- `skipped`：因 budget、用户选择、scope 不适用或弱信号不影响主结论而跳过。
- `verified`：相关 claim 被 verifier 接受。

Clarification outcome：

- `user_selected`
- `recommended_inference_selected`
- `agent_instructed_differently`
- `system_inferred`

用户可见过程事件使用业务语言展示，不展示技术 id、raw payload、provider metadata 或 raw SQL。

## 14. Graph Compiler

来源：P0-3、P1-4。

LLM 输出 candidate capability graph。local compiler 负责 validate、repair、degrade、block 或 accept。

Candidate graph node 产品字段：

- `node_id`
- `capability`
- `params`
- `purpose`
- `target_claim`
- `scope`
- `depends_on`
- `expected_evidence`
- `fallback_or_degrade_rule`
- optional：`priority`、`budget_hint`、`parallel_group`、`recipe_origin`、`mutation_reason`

动作边界：

- block：安全和合法性问题。
- degrade：证据或合同缺口。
- targeted repair：业务规划缺口。
- auto-add：确定性 guardrail。

动作判定表：

| Condition | Default action | Owner | User-visible result |
| --- | --- | --- | --- |
| permission failure、forbidden data、SQL safety risk | block | local policy/compiler | 说明权限或安全边界，相关 claim 不发布 |
| invalid metric contract、illegal grain/filter/window | block | semantic compiler | 说明口径或粒度不支持 |
| missing deterministic guardrail | auto-add | graph compiler | process event 记录自动补齐 |
| missing contract、unsupported grain、weak evidence、sparse data | degrade | capability/verifier | claim 降级，进入 limitation 或 follow-up |
| 缺关键节点、target claim 不清楚、graph path 未回答问题 | targeted repair | LLM reasoner + graph compiler | 重新生成局部 graph，记录 mutation reason |
| recipe/subgraph expectation conflict | targeted repair | graph compiler | 要求 LLM 给出保留、替换或跳过理由 |
| budget risk 或 low-value branch | skip or degrade | local controller | 记录 skipped path 和业务影响 |
| verifier failed strong claim | targeted repair first, then degrade or block | answer verifier | 强结论不发布，展示修复或降级结果 |

自动补齐只允许用于确定性 guardrail 和默认元信息，例如 data quality check、permission check、contract version pinning、timezone guard、completeness check、evidence normalization。会改变业务问题、baseline、窗口语义、claim strength 或候选机制的内容，必须进入 targeted repair 或 question tool。

Failure 进入优化循环前需要人工介入：eval failure 先归因到 business failure type 和 system responsibility point，再由 business/engineering owner 判断 severity、frequency 和 generalizability。通过评审后才能升级为 runtime guardrail、capability lint 或 prompt/recipe 变更。

Block 示例：

- permission failure
- SQL safety risk
- invalid metric contract
- illegal grain/filter/window
- claim path requiring forbidden data

Degrade 示例：

- missing contract
- unsupported grain
- weak evidence
- sparse data
- insufficient coverage
- contextual-only event evidence

Targeted repair 示例：

- 缺关键分析节点
- recipe expectations 冲突
- target claim 不清楚
- graph path 没有回答用户问题

Auto-add 示例：

- data quality check
- cumulative-value guard
- timezone guard
- contract version pinning
- permission check
- completeness check
- evidence normalization

## 15. Answer Package 与 Verifier

来源：P0-5。

后端必须生成完整 Answer Package。

Answer Package 至少包含：

- user intent
- scope
- baseline
- accepted graph summary
- metrics and contracts used
- evidence refs
- missing/degraded/blocked/skipped paths
- visualization plan
- claim groups
- verifier result
- assumptions and clarification outcomes
- graph lifecycle summary

Verifier 负责：

- 校验答案数字。
- 校验 scope 和 baseline。
- 校验证据类型和强度。
- 阻止 unsupported broadening。
- 阻止 candidate mechanism 写成 confirmed cause。
- 确保 degraded/blocked paths 约束 wording。
- 确保 visual blocks 不夸大证据。

verifier 失败时，强结论不得发布。系统必须修复、降级或阻断对应 claim。

## 16. Visualization Plan

来源：P1-2。

Answer Package 包含 validated `visualization_plan`。

Visual block 声明：

- block type
- placement
- purpose
- metric and scope
- supporting evidence ref
- supported claim
- limitations

Semantic views：

- `pattern_view`
- `formula_view`
- `attribution_view`
- `business_object_view`
- `event_timeline_view`：仅作为 business object 的事件/时间线子类型
- `anomaly_view`
- `evidence_view`
- `data_quality_view`

可视化系统由证据驱动，不做通用拖拽式 chart builder。前端渲染 validated plan，不从 raw evidence 自行判断业务重点。

## 17. Artifact

来源：P1-5。

Artifact 进入第一版 baseline，但保持简单。

Artifact 保存：

- verified answer
- visualization plan
- rendered visual blocks
- process summary
- evidence boundaries
- limitations
- data/contract snapshot info

Artifact 能力：

- read-only sharing
- permission-filtered access
- permission-filtered static export
- 从 artifact 继续追问或继续 investigation

权限变化后再次访问 artifact 时，需要按当前用户权限过滤答案 section、visual block、dimension、metric、segment、evidence 和 follow-up context。

## 18. Runtime 与服务边界

来源：LangGraph/WAJE 边界讨论。

TypeScript frontend/gateway 负责：

- frontend
- thread UI
- SDK integration
- auth/session boundary
- streaming gateway
- frontend-facing APIs

Python BI Agent Core 负责：

- LangGraph execution
- BI capability APIs
- semantic compiler
- analytical query access
- statistical analysis
- evidence reducer
- answer verifier

WAJE-owned 系统负责 BI semantics、validation、SQL compilation、evidence strength、permissions、answer verification 和 accepted graph state。

LangGraph 负责可见执行机制、checkpoint、branch/loop display、runtime progress、trace 和 retry。

## 19. Production Launch Gates

来源：P1-3。

第一版上线门槛：

- 权限阻断：权限不足的数据不得支持 claim；用户可见 permission-limited 说明。
- 审计可追溯：答案能追溯到 run、contract version、evidence、claim、verifier result。
- 快照与复跑：分析 pin contract version 和 data freshness；复跑时说明是否可比。
- 结果复用：thread-scoped result reuse 必须校验 scope、filters、grain、metric、window、baseline、contract version 和 freshness。
- Budget 留痕：因 budget 跳过的路径必须记录，并进入 Answer Package 限制或 follow-up。
- 失败可观测：slow、failed、degraded、blocked、verifier-failed runs 必须可定位。
- verifier gate：verifier 失败时不得发布强结论。
- 性能预算：每个 capability 声明默认 timeout、row/result budget 和降级策略；超时后记录 skipped/degraded path。
- 部署门槛：frontend/gateway、Python BI Agent Core、Postgres runtime mirror、ClickHouse query access 和 LangGraph adapter 有独立健康检查。
- 回滚门槛：contract、ledger、capability card、prompt/recipe、verifier policy 变更可按版本回退。
- 观测字段：每个 run 至少记录 `run_id`、`thread_id`、`user_id`、contract versions、graph version、node status、capability duration、query refs、evidence refs、verifier status、degrade/block reason。
- 告警范围：slow run、capability error、compiler block、verifier failure、permission spike、contract mismatch、ledger mismatch 进入 launch dashboard。

## 20. Launch Evaluation

来源：P1-6。

Launch eval 使用真实用户表达 + structured expectation package。

每个 eval case 包含：

- natural-language question
- expected question family
- expected intent and scope
- required capabilities
- forbidden capabilities
- expected `business_evidence_state`
- expected `data_contract_state`
- allowed claim/evidence type
- allowed strength / wording limit
- expected visual blocks
- verifier pass/fail rules

样本池：

- real user questions
- historical failure cases
- matrix-generated boundary cases

运行节奏：

- prompt、compiler、synthesizer、verifier、orchestration 变更跑 smoke eval。
- capability、contract、ledger、semantic-query 变更跑 affected slice。
- release candidate、model/provider change、major prompt change 跑 full acceptance eval。

### 20.1 Failure Attribution

业务失败类型：

- wrong question family
- wrong scope
- wrong baseline
- missed key factor
- over-strong weak evidence
- hidden data gap
- misleading visualization
- unsupported main conclusion

系统责任点：

- LLM reasoner
- graph compiler
- semantic compiler
- capability API
- evidence reducer
- answer synthesizer
- answer verifier
- visualization planner

Eval failure 不能自动进入 runtime guardrail 或 optimization loop。promotion 需要人工验证、业务/工程双 owner、severity/frequency/generalizability 评估，并在修改后重跑 affected eval slice。

## 21. Launch Acceptance Matrix

来源：P0-1。

矩阵按业务问题族组织，覆盖 capability tags、代表性 SSOT factor groups、ledger states、allowed claim/evidence type、allowed strength/wording limit、visual blocks、verifier checks。完整 factor-by-factor 矩阵在 `.mm` 完成 factor ledger reconciliation 后展开。

### 21.1 状态定义

`business_evidence_state`：

- `quantifiable`
- `candidate_mechanism`
- `contextual_evidence`
- `insufficient`
- `permission_limited`
- `unsupported_grain`
- `out_of_scope`

`data_contract_state`：

- `contract_backed`
- `evidence_linked`
- `static_assumption`
- `missing_contract`
- `permission_limited`
- `unsupported_grain`
- `out_of_scope_for_now`

### 21.2 可执行骨架

| Question family | Representative SSOT factor groups | Required capabilities | Ledger states to cover | Allowed claim/evidence type | Allowed strength / wording limit | Expected visual blocks | Verifier checks |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `paid_amount_change_explanation` | payment/order metrics, user/payment/channel structure, events, anomalies | `formula_decompose`, `joint_attribution`, `segment_bridge`, `outlier_scan`, `data_quality_check`, `answer_verify`; 按需 `pattern_scan`, `event_evidence` | `contract_backed`, `missing_contract`, `permission_limited` | accounting contribution, candidate mechanism, contextual evidence | quantified claim 需要可复算证据；candidate/contextual 不写 confirmed cause | formula contribution, attribution, anomaly, baseline stability, limitation | 数字、baseline、scope、缺口、降级路径 |
| `pattern_explanation` | calendar/time windows, payday, holidays, events, segment structure | `pattern_scan`, `data_quality_check`, `answer_verify`; 按需 `event_evidence`, `joint_attribution`, `outlier_scan` | `contract_backed`, `static_assumption`, `evidence_linked`, `unsupported_grain` | statistical association, candidate mechanism | high/medium/low 跟随 recurrence、幅度、稳定性；弱信号只写 tendency | phase/profile/timeline/rolling/lag view, exceptions | pattern family、窗口、稳定性、例外、wording |
| `business_object_impact_review` | campaigns, ad spend, product versions, metric drivers, dimensions/segments, external context | object type 决定 `event_evidence`、`formula_decompose` 或 `segment_bridge`; `answer_verify`; 首屏/强结论要求 `data_quality_check`; 按需 `joint_attribution` | `contract_backed`, `evidence_linked`, `missing_contract`, `unsupported_grain` | accounting contribution, statistical association, candidate mechanism, causal evidence when supported | net impact 需要 control/causal evidence；无对照时限制为 candidate/contextual | pre/post, exposure timeline, contribution, control comparison | 对照、窗口、证据强度、impact wording |
| `revenue_health_review` | target metrics, payment funnel, structure, anomalies, data quality | `formula_decompose`, `outlier_scan`, `segment_bridge`, `data_quality_check`, `answer_verify`; 按需 `joint_attribution` | `contract_backed`, `permission_limited`, `missing_contract` | health judgment, accounting contribution, anomaly supported | risk wording 跟随影响大小和 evidence strength；data issue 单独标注 | trend, formula, structure, anomaly, data quality | 目标/历史 baseline、风险分层、数据限制 |
| `segment_or_factor_attribution` | channel, user type, payment method, device/geo, metric components | `segment_bridge`, `joint_attribution`, `data_quality_check`, `answer_verify`; 按需 `formula_decompose` | `contract_backed`, `unsupported_grain`, `permission_limited` | quantified contribution, explained difference, candidate mechanism | 局部组合只写局部 scope；稀疏或权限限制时降级 | attribution ranking, combination path, stability/coverage | scope、稀疏、权限、贡献/因果分离 |
| `anomaly_or_black_swan_review` | time windows, metric chain, dimensions/segments, internal actions, external events | `outlier_scan`, `data_quality_check`, `event_evidence`, `answer_verify`; 按需 `segment_bridge`, `joint_attribution` | `contract_backed`, `evidence_linked`, `missing_contract`, `out_of_scope_for_now` | anomaly supported, candidate explanation, insufficient | black-swan 只能作为候选解释；data issue 优先降级 | anomaly scope, ruled-out paths, event/context timeline | 伪异常、scope、候选解释、黑天鹅 wording |
| `custom_baseline_comparison` | time baselines, event-relative baselines, target values, similar windows, metric components | `pattern_scan`, `formula_decompose`, `data_quality_check`, `answer_verify`; 按需 `joint_attribution`, `event_evidence` | `contract_backed`, `unsupported_grain`, `missing_contract` | quantified delta, accounting contribution, candidate mechanism | baseline 不可比时降级；分歧 baseline 限制主结论 | baseline comparison, formula, attribution, limitation | baseline、可比性、时间语义、claim boundary |
| `data_quality_or_evidence_review` | contracts, permissions, metric identity, data freshness, evidence refs | `data_quality_check`, `answer_verify`; 按需回查相关 evidence | 全部 ledger states | trust judgment, degraded claim, insufficient | 可信/不可信要绑定受影响 claim 和 scope | trust summary, data quality, evidence boundary | 数据质量、合同、权限、wording、升级条件 |

### 21.3 代表性单元格示例

| Cell | business_evidence_state | data_contract_state | Pass condition |
| --- | --- | --- | --- |
| `pattern_explanation` × payday × `event_evidence` × full sample month phase × candidate mechanism | `candidate_mechanism` | `static_assumption` / `evidence_linked` | 可以表达 payday window 与月初模式存在业务相关候选机制；强因果 wording 需要额外证据。 |
| `business_object_impact_review` × ad spend × exposure impact × recent week | `contextual_evidence` or `insufficient` | depends on exposure/control contracts | 缺 exposure/control 时只可表达候选 impact 或限制；不能写 confirmed net impact。 |
| `segment_or_factor_attribution` × channel × user type × two-dimensional attribution | `quantifiable` when contracts and grain support | `contract_backed` | 可表达组合贡献、覆盖和稳定性；稀疏或权限限制时降级。 |

## 22. First Baseline Acceptance

第一版生产 baseline 通过条件：

- 八个问题族都有端到端代表 case。
- 首条 vertical slice 通过 pattern-domain expectation package。
- 每个 relevant SSOT node 有明确 ledger status。
- 八个 foundational capability 有 product-contract card。
- accepted graph 生命周期和 compiler 动作有记录。
- Answer Package、claim group、verifier 能约束每个 final claim。
- 过程事件使用业务语言。
- 技术 artifact 不进入普通 UI。
- artifact 支持简单保存、权限过滤分享和继续追问。
- eval 覆盖真实问题、历史失败和边界 case。
- 权限、审计、快照、复跑、budget、部署、observability launch gates 满足要求。

## 23. 后续技术设计范围

以下内容不在 PRD 中定最终形态：

- ledger、evidence、run state、contract 的最终表结构。
- capability call、Answer Package、event stream、verifier 的最终 API schema。
- 21st Agent Elements / SDK 最终采用方案。
- 具体语义合同文件和 ClickHouse/Postgres query planning。
- 具体 eval case 文件和完整 factor-by-factor acceptance matrix。
- PRD 到实现计划的任务拆解。

## 24. 轻量来源追溯

来源：P2-4。

- 产品目标、原则、问题族：来自 PRD interview 和 `docs/product-decisions.md` 的 WAJE BI v2 product alignment。
- question tool、accepted graph、Answer Package：来自 PRD review P0/P1 确认。
- 首条 vertical slice：来自 pattern domain 访谈和月初问题讨论。
- factor ledger：来自 `.mm` SSOT 和 ledger review 流水线确认。
- launch eval、guardrail promotion：来自 eval/guardrail 讨论，failure 进入优化循环必须人工介入。
