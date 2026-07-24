# P9 执行计划：Case B 当前基线与受控多 Agent 深度调查

状态：`proposed`

目标：在 P8 当前单权威运行链上重新建立 Case B 的真实生产基准，并把现有一次性只读调查
Agent 扩展为可持久化、可恢复、可审计的受控多 Agent 调查。多 Agent 必须增加机制解释、
交叉验证、替代解释和行动价值，同时保持 accepted Plan、Evidence、Claim、Publication 与
Delivery 的唯一权威。

基线提交：`46eff6d0 feat(runtime): close P8 first-answer performance`

## 1. 业务结果

P9 要让运营人员得到一份完整的调查结论：

- 指标发生了什么变化，变化有多大；
- 主驱动、次驱动和抵消因素分别是什么；
- 增长或下滑集中在哪些人群、档位、渠道、支付方式、地区或设备；
- 支付终态、市场漏斗、玩法行为和外部事件是否提供交叉信号；
- 哪些解释拥有较强证据，哪些仍是候选机制；
- 当前数据不能区分哪些解释；
- 下一步应优先验证或采取什么行动。

多 Agent 的价值由调查分工、共享证据、独立复核和统一收口产生。增加 Agent 数量本身不构成
业务价值。

## 2. 当前事实与边界

### 2.1 当前运行基线

- P8 真实 DeepSeek 首答为 318.835 秒，当前完整首答合同为 480 秒；
- 两轮已发布材料追问为 11.175 秒和 9.518 秒，追问合同为 20 秒；
- 首答保留 21 个 capability task、23 条 evidence 和 22 个 verified claim；
- 所有模型请求只经 `MainlandModelProvider` 发往 DeepSeek Chat Completions；
- 验收环境没有 `OPENAI_API_KEY`，OpenAI hosted request count 为 0；
- 线程摘要与回答质量审核已经退出客户首答关键路径。

### 2.2 当前多 Agent 能力

`bi_agent/runtime/controlled_subagent_tools.py` 已支持 1–3 个并行只读调查：

- 输入仅允许 customer-safe artifact；
- 子 Agent 不调用工具或 BI；
- 输出必须引用 allowlist source refs；
- 输出保存为结构化 `controlled_subagent_result` artifact；
- 子 Agent 没有 ThreadHead、Claim、Publication 或 Delivery 权限。

当前能力仍是一次性调用。P9 要补齐 durable child identity、恢复、幂等、失败隔离、父子 trace
和父级收口。

### 2.3 数据边界

- 当前跨来源完整边界为 Lagos 业务日 2026-06-02；
- 付费金额、付费人数和付费订单使用当前支付订单权威；
- Market Dashboard 可支持日级市场漏斗和经营背景；
- 支付订单—下注关联可支持观察性玩法行为证据；
- 外部事件工作簿可支持宏观、发薪日、赛事、电力、网络、媒体政策、天气、社会稳定和节假日
  的候选或背景证据；
- 内部运营与投放明细仍缺 owner、作用时间、范围、活动或版本身份、可复核来源；
- 支付失败原因、环节、重试链和处理耗时仍缺事件级来源；
- 用户级注册到首充漏斗继续按现有决议暂缓。

缺失来源形成局部 limitation，不得阻断其他已闭合结论，也不得被解释为“没有影响”。

## 3. 不可覆盖的既有决议

### 3.1 单权威

一个 accepted `IntentRevision` 和 `PlanRevision` 驱动能力执行、证据结算、claim、publication
和 delivery。子 Agent 只形成调查材料，不产生第二套计划、证据账本或发布链。

### 3.2 回答质量只记录

完整度、洞察深度、可读性、行动性、表达和潜在幻觉风险只进入交付后人工 advisory review：

- 不阻断客户回答；
- 不触发 writer retry、自动补写、自动改写或删段；
- 不产生客户 warning；
- 不改变 run、publication 或 delivery 状态；
- 不撤回已发布内容。

权限、固定敏感输出、SQL 与数据合同、证据引用、claim provenance、持久化完整性、叙事存在和
交付继续作为硬边界。

### 3.3 通用修复

- 不为 Case B 的日期、问题文本、固定数字或某次模型输出写特例；
- 不使用关键词字典猜测开放业务语义；
- 不恢复已删除的旧 planner、answer package、mutable artifact 或兼容读取路径；
- Provider timeout、retry 和 circuit breaker 继续集中在 Provider 层；
- 真实失败先归纳为可复用的节点、合同、证据或运行时失败类型，再修改实现。

## 4. 多 Agent 业务设计

### 4.1 总分析师

父 Agent 负责：

- 识别用户要支持的经营决策；
- 确认目标指标、目标窗口、基线和业务范围；
- 生成并接受唯一 Plan；
- 选择可以独立并行的调查任务；
- 对数字、证据和解释冲突进行收口；
- 形成唯一客户答案。

### 4.2 专项调查

调查任务从 accepted Plan 的独立分析轴和已发布材料动态产生，不建立 Case B 固定角色。
常见任务族包括：

1. **增长机制调查**
   - 付费人数、频次、单笔金额；
   - 新老用户或日级漏斗背景；
   - 充值档位迁移；
   - 主驱动与抵消项。

2. **结构与集中度调查**
   - 渠道、支付方式、地区、城市、设备和网络；
   - 规模贡献、份额变化和超额偏差；
   - 新进入、退出、稀疏和集中度风险；
   - 各维度不可加总的解释边界。

3. **背景、替代解释与反向证据调查**
   - 支付终态和成功率；
   - 市场漏斗、玩法与下注关联；
   - 发薪日、赛事、节假日等外部事件；
   - 相反信号、混杂因素和当前无法区分的解释。

每个专项任务必须有清晰问题、输入 artifact allowlist、预期输出类型和最大成本。没有独立价值的
任务由父 Agent直接完成，不为了使用多 Agent 而拆分。

### 4.3 父级收口

父 Agent 合并专项结果时：

- 数字冲突回到数据合同、来源和对账记录；
- 解释冲突保留为竞争解释，并标记各自证据强度；
- 数据不足明确写成当前不能区分，同时说明需要补充的来源；
- 子 Agent 文本不能直接升级为 claim；
- 最终 narrative 只消费已有 verified claim、合法 limitation 和引用闭合的候选调查材料。

## 5. 实施任务

### Task 0：当前状态审计与 P9 RED 契约

开始实现前完整检查：

- `AGENTS.md`；
- `docs/specs/general-agent-runtime/target-architecture.md`；
- `docs/implementation-roadmap.md`；
- `docs/adr/2026-07-17-single-authority-agent-workflow.md`；
- `docs/adr/2026-07-20-advisory-publication-human-review.md`；
- `docs/reviews/2026-07-24-p8-first-answer-performance.md`；
- `bi_agent/runtime/controlled_subagent_tools.py`；
- `bi_agent/runtime/agents_sdk_adapter.py`；
- `bi_agent/runtime/agent_turn_runtime.py`；
- `bi_agent/runtime/durable_call_journal.py`；
- `bi_agent/runtime/post_execution_workflow.py`；
- 当前 Case B acceptance runner、expectation package 和相关测试。

先写失败测试，覆盖：

- 子 Agent 无查询、claim、publication 和线程权威；
- 父子身份、输入 digest 和 source allowlist 可持久化；
- 重启后不重复已完成子任务；
- 一个子任务失败不抹除其他结果；
- 提示注入和伪造 source ref 不能进入输出；
- 回答质量发现不能进入交付依赖图。

### Task 1：建立当前 Case B 单 Agent 基准

标准问题：

> 全量样本看，2026 年 6 月 1 日付费金额为什么上涨？

要求：

- 使用新线程和当前 snapshot/release；
- 基线存在实质歧义时，通过现有 typed clarification 让用户选择；
- 保存 intent、plan、task、query、evidence、claim、publication、delivery 和 Provider profile；
- 覆盖公式、充值档位、支付终态、市场漏斗、玩法、结构维度、外部事件和数据质量；
- 明确内部运营、投放与支付失败过程数据边界；
- 完成两轮已发布材料追问，禁止重新运行完整 BI；
- 保存客户页面、WorkBench 和必要 trace 截图；
- 人工评价内容深度与可读性，只记录，不改变 hard status。

该运行形成 P9 的单 Agent 对照组。旧 Case B artifact 只用于历史比较，不作为当前通过证据。

### Task 2：持久化子任务生命周期

在现有 WAJE runtime 与 PostgreSQL 权威内补齐：

- parent operation、child run、investigation 和 attempt 的稳定 identity；
- content-addressed input、allowed source refs 和输出 artifact；
- `planned`、`running`、`completed`、`limited`、`failed` 等 typed 状态；
- lease、恢复、幂等和取消；
- 同一逻辑输入只有一个 accepted child result；
- Provider retry 只发生在 Provider 层；
- 父子 trace 进入 Workbench；
- 客户投影不暴露内部 child identity、Provider payload 或技术错误。

优先复用现有 ThreadItemLedger、durable call journal、task recovery 和 artifact persistence。
只有现有记录无法表达必要生命周期时才扩展 schema，禁止建立平行历史系统。

### Task 3：从 accepted Plan 生成独立调查

建立 typed investigation proposal 和 deterministic admission：

- LLM 可以提出调查分工；
- admission 验证任务是否对应 accepted axis、是否独立、是否有合法材料、是否超出预算；
- 单次最多并行 3 个任务；
- 重复或依赖关系强的任务合并或留给父 Agent；
- 任务只读取已经形成的 customer-safe artifact；
- 子任务不得触发 `run_bi_analysis`、`continue_bi_analysis` 或其他数据查询；
- 不使用本地关键词表选择业务调查。

### Task 4：父级综合与冲突处理

实现父级综合输入：

- 子结果引用闭合后注册为候选调查 artifact；
- 父级按 source refs 映射回已有 evidence、claim 和 limitation；
- 数字或来源冲突形成 typed hard error；
- 开放解释分歧形成可并列的候选机制；
- 某个子结果失败时保留其他结果和合法主回答；
- 子 Agent 没有成功返回时，父级仍可以使用原单 Agent 材料完成回答；
- 多 Agent 不增加第二个 narrative、publication 或 customer payload。

### Task 5：同源 A/B 真实验收

在相同问题、clarification、snapshot、accepted Plan 和模型配置下运行：

1. 单 Agent Case B；
2. 受控多 Agent Case B；
3. 一个子任务失败的多 Agent Case B；
4. 父 worker 在子任务完成前后重启；
5. 两轮已发布材料追问。

比较：

- accepted obligation、axis、task、evidence 和 verified claim；
- 主驱动、抵消项、结构集中点、交叉信号、替代解释和行动优先级；
- 子 Agent 是否发现单 Agent 遗漏的合法材料；
- Provider 调用数、token、时延和错误；
- 是否新增 BI 查询；
- publication 和 delivery identity；
- Workbench 父子 trace 完整性；
- 客户页面可读性。

人工洞察评价可以影响多 Agent 功能是否默认启用，但不能影响任何已形成 publication 的交付。

### Task 6：标准测试包、文档与发布

建立 `evals/general_agent_runtime/p9-cases.jsonl`，至少覆盖：

1. 当前 Case B 单 Agent 完整链；
2. 两个独立调查并行完成；
3. 三个调查中一个 Provider 失败；
4. child completion 后父 worker 重启；
5. 重复 dispatch 和 child input 幂等；
6. artifact 提示注入；
7. 伪造 source ref；
8. 子 Agent 权限越界；
9. 父级 claim/provenance 闭环；
10. 已发布材料追问不重跑 BI；
11. 无 `OPENAI_API_KEY` 和无 `api.openai.com` 出站；
12. Workbench parent-child trace；
13. 客户页面桌面和移动端截图。

最终更新：

- target architecture；
- implementation roadmap；
- P9 验收报告；
- eval README；
- release manifest；
- 数据库迁移与恢复 runbook（仅在 schema 发生变化时）。

## 6. 本窗口经验与防回归清单

### 6.1 回答质量曾被错误放进交付路径

曾出现 narrative normalization 因覆盖度或引用判断删除 block 的实现方向。该方向违反人工
advisory 决议。P9 禁止恢复 `block_excluded`、质量驱动 retry、自动补写、warning、状态降级或
publication veto。

### 6.2 确定性引用问题曾触发无价值模型重试

合法 fact owner 歧义应依据 accepted authority 机械装配，同时保留模型原文。未知或伪造引用形成
provenance error。相同输入的确定性错误不得重复调用模型。

### 6.3 Provider payload 曾导致输入和输出预算失败

完整权威材料留在 Workbench，Provider 使用无损 typed projection、列式材料和可逆短引用。
压缩不能删除 required evidence、claim、limitation 或 publication requirement。

### 6.4 线程摘要和质量核验曾占用客户关键路径

摘要维护和质量审核保持后台、持久化驱动。多 Agent 也不能把审阅或汇总维护塞回首答交付路径。

### 6.5 已发布材料追问曾有重跑和串行读取风险

追问优先使用 `agent-turn-action-binding.v2` 和一次有界 artifact 读取。禁止为了回答现有材料
问题重新执行 Case B。

### 6.6 真实失败需要定位责任节点

出现重复失败时：

1. 保存失败 run、Provider 输入输出、节点耗时和 typed error；
2. 判断属于 intent、plan、capability、evidence、claim、narrative、persistence、delivery 或
   Provider 哪个责任点；
3. 归纳为可复用失败类型；
4. 修改对应节点的提示词、typed contract、projection 或运行时策略；
5. 增加问题家族测试；
6. 再跑同组真实用例。

禁止通过 Case B 日期、固定问句、单个数字或一次偶发模型输出增加特例。

### 6.7 历史计划包含已删除路径

2026-07-15 的 Case B 计划属于历史资料，包含当前已经删除或替换的模块。实现前以当前源码、
当前 ADR 和 P8 后合同为准，不恢复旧模块或双轨兼容。

### 6.8 外部事件来源曾被误判为不存在

当前外部事件工作簿可以提供候选或背景证据。内部运营、投放明细和支付失败过程仍是独立缺口。
计划、回答和验收需要分别表达，不能把它们合并成“事件数据不可用”。

## 7. 验收门槛

### 硬门槛

- 新 Case B 单 Agent 与多 Agent 均完成完整权威闭环；
- 多 Agent 与单 Agent 使用同一个 accepted Plan 和 snapshot/release；
- 子 Agent 不产生 BI query、claim、publication 或 delivery；
- 所有子结果引用闭合；
- 子任务失败和重启不破坏主回答、幂等或其他子任务；
- 首答满足 480 秒合同，追问满足 20 秒合同；
- 真实环境清除 `OPENAI_API_KEY`，唯一模型出站为 DeepSeek；
- OpenAI hosted request count 为 0，trace 只进入 WAJE；
- Phase 7/8/P9、静态合同、TypeScript、production build 和 release manifest 全部通过。

### 多 Agent 默认启用条件

- 硬门槛无回归；
- 人工业务评价确认多 Agent 稳定增加机制解释、抵消项、细分集中点、交叉信号、替代解释或
  行动优先级中的至少一类实质价值；
- 增加的延迟和 Provider 成本可观测且可接受；
- 仅增加篇幅、重复证据或产生更多未闭合假设时，保持按需调用或继续实验。

人工评价用于功能发布决策，不改变已经形成的客户 publication。

## 8. 本阶段不做

- 不授予子 Agent 独立 BI 查询权；
- 不授予子 Agent ThreadHead、Claim、Publication 或 Delivery 权限；
- 不使用 OpenAI Hosted Multi-Agent、Hosted Trace 或 Hosted Evals；
- 不新增前端产品功能；
- 不补造内部运营、投放或支付失败过程数据；
- 不实现用户级注册到首充漏斗；
- 不建立 Case B 专属业务规则；
- 不用回答质量评判阻断客户交付。

## 9. 完成交付物

- 当前合同下的新 Case B 单 Agent 真实报告；
- 当前合同下的多 Agent A/B 与失败恢复报告；
- P9 deterministic、live 和 browser 标准包；
- 客户页面与 Workbench 截图；
- parent-child durable trace 和幂等证据；
- P9 实施与验收报告；
- 更新后的路线图、target architecture、eval README 和 release manifest。
