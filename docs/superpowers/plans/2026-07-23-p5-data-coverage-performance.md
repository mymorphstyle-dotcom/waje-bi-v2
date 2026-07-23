# P5 执行计划：数据覆盖闭环与性能工程

状态：`completed`

目标：把当前可获得的新数据接入 WAJE 的不可变 snapshot/release、查询合同、证据和发布权威链；
同时把完整 BI 调查的时延拆成可观测、可归因、可回放的阶段预算，并在保持调查广度、证据闭合和
回答完整度的前提下缩短重复计算与验证输入。

## 继承的决议

- `IntentRevision -> DecisionLedger -> PlanRevision -> capability -> evidence -> claim ->
  AuthorityBundle -> publication -> delivery` 继续构成唯一权威链。
- PostgreSQL 继续承担 thread、task、checkpoint、artifact、release 和 trace identity 权威；
  ClickHouse 承担分析事实与聚合查询。
- P4 的十个因素域、宽进深出、受控调查 Agent、客户安全投影和 Workbench 完整审计继续生效。
- 回答质量审核保持 post-delivery advisory，不阻断首次发布，也不触发本地模板或自动改写。
- 当前数据以 2026-06-02 完整为阶段性验收边界；用户级注册首充漏斗本阶段不进入实现。
- 不使用关键词字典判断开放业务语义，不为单个问题或某次模型输出增加特例。

## 当前基线

P4 的三次完整真实调查耗时 741–935 秒。已持久化节点记录显示主要耗时集中在：

1. `settle_claim_authority`：208–324 秒；
2. `compose_claim_aware_narrative`：258–383 秒；
3. `execute_capability_dag`：97–122 秒；
4. 规划约 70 秒，意图绑定约 20–26 秒。

当前运行已经记录顶层节点耗时和 durable call 起止时间，但缺少统一阶段预算、输入规模、慢调用归因
和 capability materialization 子阶段观测。性能结论因此只能靠人工拼接 trace。

## Task 0：覆盖基线与 RED 测试

- 更新 P4 之后的真实覆盖表：Market Dashboard、外部事件、首充时区修复和新订单—下注关联源。
- 固化新数据的 grain、主键、时区、窗口、金额与关联完整性规则。
- 先增加失败测试，覆盖重复订单、错误时区边界、24h/7d 窗口倒置、展示比率误当权威、
  未与付费订单逐单对账就发布、合同缺字段和客户投影泄漏。

## Task 1：支付订单关联下注数据

源文件：`/Users/luka/Downloads/支付订单关联下注金额.csv`。

已审计事实：

- 299,530 行、299,530 个唯一订单、58,494 个用户；
- 原始时间范围 `2026-06-01 07:00:00` 至 `2026-06-03 06:59:59`，按
  `Asia/Shanghai` 解释后对应 `Africa/Lagos` 的 2026-06-01 至 2026-06-02；
- 充值金额合计 646,563,590 NGN；24h/7d 下注金额分别为
  4,165,547,141,440 和 15,015,128,192,321 NGN；
- 订单主键、金额非负、24h 不超过 7d、是否游戏标记均通过；
- 源比率列是两位小数展示值，最大舍入误差 0.005，分析统一由金额重算；
- 玩法占比包含长尾逐项舍入，3 条记录合计偏离 100% 超过 0.11 个百分点，当前只支持结构背景。

实施：

- 建立独立 source contract 和内容寻址 ClickHouse 物理表；
- 时区转换、主键和窗口规则在 loader 中 fail closed；
- 与 `paid_order_success` 按订单逐条核对用户、Lagos 业务日和充值金额；只有完全匹配才能发布
  `claim_ready` snapshot/release；
- 原始订单、用户和玩法字符串保持服务端受限，客户只消费聚合结果；
- 接入 24h/7d 下注金额、付费后 24h 游戏率、下注/充值比和充值档位交叉分析。

## Task 2：Market Dashboard 对账

- 以 `paid_order_success` 作为付费金额与付费订单主权威，Market Dashboard 作为经营漏斗和大盘背景。
- 对重叠日期逐日核对付费金额与付费人数，并保留差异率、推定截点和来源限制。
- 2026-06-01 完全匹配；2026-06-02 Dashboard 记录 185,469,962 NGN，付费订单记录
  338,323,281 NGN。Dashboard 数值接近当天 Lagos 14:26 的累计值，当前证据指向末日截取不完整。
- 该差异进入 source reconciliation 与 evidence ceiling：Dashboard 的 2026-06-02 付费金额和
  付费人数不得控制结论；注册、首充等字段可在明确 dashboard grain 和末日限制后用于漏斗背景。

## Task 3：统一性能观测与预算

- 定义 SDK-neutral `AnalysisPerformanceProfile`，从 checkpoint、durable call 和 capability
  materialization 生成阶段耗时、输入规模、调用数、预算状态和瓶颈排序。
- 阶段：意图、规划、查询准备、SQL、capability、claim、narrative、publication/delivery。
- profile 只进入 WAJE audit/Workbench，不进入客户回答和 DOM。
- 首版预算采用 audit-only：完整因素调查目标 p50 300 秒、p95 480 秒；预算超限继续完成高价值节点，
  记录可观测 breach，不截断回答。

## Task 4：不损失深度的性能优化

- block verifier 只接收待验证段落实际引用的 claim、fact、recommendation、limitation 和 boundary
  闭包；完整材料仍持久化在 Workbench，未引用材料不重复进入 verifier prompt。
- 保留完整 writer 输入、claim verifier、推荐验证和本地硬校验，避免以减少调查或删掉验证换时延。
- capability materializer 增加子阶段计时，确认 SQL、合同编译、结果校验、证据绑定各自成本后，
  再决定是否引入跨任务查询复用或预聚合。

## Task 5：验收

- loader、合同、snapshot/release、运行时 registry 和查询 compiler 测试通过；
- 真实 ClickHouse 逐单对账为 100%，真实聚合查询返回 6 月 1–2 日 24h/7d 行为指标；
- Dashboard 差异形成 typed reconciliation，不再被当作同口径可互换来源；
- performance profile 可从真实 run 生成并只写 WAJE 审计；
- verifier scoped payload 保持引用闭包和验证结果合同，测试证明未引用材料不会出站；
- Python、Phase 4/7、合同校验、release manifest 和静态检查通过。

## 后续数据 owner 输入

- 支付尝试/失败表：需要支付发起事件、终态、通道、失败码、发起与完成时间、订单关联键和 Lagos
  业务时间，才能发布支付成功率、失败原因和支付环节耗时结论。
- 内部运营与投放事件：需要 owner、事件时间窗、作用范围、活动/版本/渠道标识和可复核来源。
- 用户级注册首充漏斗仍按当前决议暂缓；Market Dashboard 继续承担阶段性的日级漏斗背景。
