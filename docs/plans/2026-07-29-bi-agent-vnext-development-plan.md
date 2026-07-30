# WAJE BI Agent vNext 0→1 开发计划

## 0. 文档控制

| 项目 | 内容 |
|---|---|
| 状态 | Active |
| 日期 | 2026-07-29 |
| 实现根目录 | `vnext/` |
| 适用阶段 | Gate 0–Gate 7 |
| 产品阶段 | 无线上用户、无 production artifact、无兼容义务 |
| 当前 Gate | Gate 2 complete + durable async amendment；G3.E0 formal admission 仍为 `deny_g3_1`；G3.1 local implementation 已按用户 development override 完成 |
| 计划权威 | 本文负责开发顺序、Gate 验收和范围控制；各 Gate 接受后的合同、ADR、schema 与 eval package 负责对应实现细节 |

本文是 WAJE BI Agent vNext 的持久化执行计划。旧 `bi_agent/`、`app/`、`components/`、
`lib/`、`contracts/`、`tests/`、`tools/`、`evals/`、根级 runtime SQL 和历史文档只作为
参考材料。vNext 应用代码、合同、迁移、测试、eval 与运行入口全部位于 `vnext/`。GitHub
要求 workflow 位于仓库根 `.github/`；该目录只允许 vNext 的最小 deployment projection，
由 `vnext/ops/github/workflow-authority-policy.json` 逐文件 hash 约束并进入删除独立性验收。

### 0.1 计划更新规则

- 每个 Gate 开始前执行访谈判断并写入 Gate 记录。
- 仓库、数据、工具可以查明的事实直接调查。
- 只有会改变产品、架构、业务定义、范围、风险或验收标准的判断题才询问用户。
- 需要询问时一次只问一个问题，同时写出推荐答案、备选答案和影响，等待回复。
- 无需用户决策时记录“本 Gate 无需用户决策”并继续。
- Gate 内出现新的重大决策时，只暂停受影响分支；不受影响的验证可以继续。
- 接受后的决定进入本计划的 Decision Log，并由对应 Gate 合同或 ADR 固化。

## 1. 产品使命与边界

### 1.1 使命

把专业 Business Analysis 做成模型原生、证据约束的 BI Agent，交付完整闭环：

```text
用户经营问题
  → 测量设计
  → 动态调查
  → 可信证据
  → 可追溯回答
```

Primary Business Analysis Agent 持续拥有开放业务语义。它通过 typed actions 修订测量设计、
组织调查、检查语义、调用能力、记录解释、触发敏感性分析、询问用户并提出答案。确定性系统
守住数据、执行、证据和发布的硬边界。

### 1.2 当前产品范围

- 开放经营问题的语义理解与连续追问。
- estimand、观察单位、分子、分母、exposure、comparison 与假设的显式测量设计。
- 可动态修订、可恢复、可回放的调查过程。
- 数据合同约束的探查、分析 capability 与受治理 SQL escape hatch。
- 不可变证据、稳定 result handle、逐 claim 证据绑定和适用边界。
- provisional / settled 回答与风险触发 Reviewer。
- Chat + Analysis Workspace 双栏 UI，以及 accepted WorkPlan 与真实 event journal 的
  Workflow 只读业务投影。
- 核心业务问题家族的生产完整覆盖和可执行 launch acceptance。

### 1.3 当前非目标

- 持续监控、告警或定时巡检。
- 预测、情景模拟或预测性决策。
- 自动执行业务动作。
- 邮件、日历、文档、IM 等办公协同。
- general enterprise agent。
- 旧系统数据、API、artifact、UI、runtime、测试或调用方的迁移兼容。

任何扩展都需要单独的产品决定和 Gate 变更，不得借 capability、工具或 UI 小改动进入当前
范围。

## 2. Day 0 隔离合同

### 2.1 隔离规则

1. vNext 应用生产代码只能位于 `vnext/`；provider 强制要求的根级 `.github/` 文件属于
   exact-hash-bound deployment projection，不得承载业务逻辑、runtime 包或旧系统依赖。
2. vNext 不 import、调用、继承、链接或运行旧生产实现。
3. vNext 不把旧测试、fixture、contract、SQL migration 或前端构建产物纳入运行依赖。
4. 从旧代码提取通用模式时，先记录来源与抽象理由，再在 `vnext/` 内形成新实现、新测试和
   新内部引用。
5. 旧代码不能通过路径注入、动态 import、shell 调用、HTTP loopback、数据库 view 或
   shared migration 形成隐性依赖。
6. vNext 使用独立包名、数据库 schema、migration ledger、环境变量前缀、运行入口、测试
   入口和发布清单。
7. vNext 的 CI 必须支持只复制 `vnext/` 与 policy 列出的最小 `.github/` deployment
   projection 到空临时目录后执行构建、测试、workflow 校验和 smoke run。
8. Gate 7 必须在模拟删除全部历史实现的环境中完成 build、test、run、package 和 release
   manifest 验收。
9. `vnext/tools/isolation-policy.json` 维护 machine-readable forbidden paths、package
   names、schema names、commands 和 environment variables。verifier 扫描 executable
   source、dependency manifest、SQL、shell、generated import graph 和 release artifact。
10. 文档可以引用历史路径用于审计；可执行 surface 和 release artifact 中禁止出现未批准
    legacy reference。allowlist 必须包含理由、owner 和最晚删除 Gate。
11. Python baseline 为 `requires-python >=3.12`。本地与 CI 使用锁定 toolchain 创建项目内
    virtualenv；禁止因宿主机自带解释器降低版本要求。

### 2.2 旧组件隔离清单

| 旧区域 | 参考价值 | vNext 规则 | 最早整体删除时点 |
|---|---|---|---|
| `bi_agent/` | 失败模式、算法思路、provider 与数据访问经验 | 禁止 runtime/import 依赖 | Gate 7 删除演练通过后 |
| `app/`、`components/`、`lib/` | UI 交互经验、customer-safe projection 经验 | 禁止复用旧组件与 API route | Gate 6 新 UI 验收后 |
| `contracts/` | 指标、维度、因子和数据源参考 | 经人工/自动重建进入 vNext contract；禁止运行时读取旧目录 | Gate 4 contract coverage 通过后 |
| `tools/runtime/*.sql` | 持久化经验和失败样本 | vNext 使用独立 schema 与 migration ledger | Gate 5 recovery 验收后 |
| `tests/`、`evals/` | 历史失败案例和自然语言样本 | 只转写为当前合同的 expectation package；禁止继承断言 | Gate 7 新矩阵完整后 |
| 根级 `package.json`、`requirements.txt` | 版本与工具参考 | vNext 使用独立依赖清单和 lockfile | Gate 6/7 独立构建后 |
| `docs/superpowers/`、旧 ADR/spec | 决策和事故背景 | 只读参考；不形成 vNext 实现权威 | Gate 7 后可归档 |
| `ops/`、根级 compose | 本地数据基础设施经验 | vNext 自带可复现 ops 与环境合同 | Gate 4 数据链验收后 |

不建立兼容 adapter、双写、dual read、旧 ID translation 或旧 event replay。

## 3. 权威模型

### 3.1 五类权威对象

| 权威对象 | 职责 | 核心不变量 |
|---|---|---|
| `InvestigationCase` | 稳定承载一个经营问题及其连续调查，只保存 identity、生命周期与 CAS accepted head pointers | accepted Question/Frame/Plan/Answer 都指向 immutable revision/version；身份、业务结论与用户角色解耦 |
| `AnalysisFrameRevision` | 测量设计唯一权威 | source-grounded measurement algebra 以显式 `EstimandSpec` 定义 population、variable/event、观察单位、时间、window、estimator、分子分母、unit/exposure aggregation、contrast、eligibility、identification、alternatives、falsification、reversal 与 epistemic completion |
| `WorkPlanRevision` | 当前 accepted 业务调查任务图 | 任务可动态修订；业务口径变化必须引用新 FrameRevision；工具重试保持同一 plan revision |
| `EvidenceRecord` | capability 原生返回的不可变证据 | 绑定 semantic/authority/resolution-outcome identity、封闭 execution provenance（conformance 或 production QuerySpec）、contract、snapshot/release、typed scope、grain、exposure、结果摘要或稳定 result handle |
| `AnswerVersion` | 用户可见答案及逐结论绑定 | `provisional \| settled`；每个 claim 绑定 EstimandSpec、obligation、EvidenceUseBinding、typed applicability、限制、反例状态和 Reviewer disposition |

### 3.2 派生与从属记录

- `ContextPacket`：从 accepted heads、最近相关事件、可见证据索引、未决异议和用户消息构造的
  有界、可哈希、可重放输入；它是投影，不独立改写权威。
- `EventJournalEntry`：追加式运行事实，覆盖 action 请求、admission、执行、结果、失败、
  checkpoint、resume、revision acceptance、answer publication 和 delivery。
- `InterpretationRecord`：Primary Agent 对证据的结构化解释，绑定 FrameRevision 与
  EvidenceRecord；它不能提升证据强度。
- `MeasurementObjection` / `ReviewerObjection`：独立 Reviewer 的结构化异议，分别约束
  Frame acceptance 与 Answer publication；绑定风险、对象、证据缺口、建议动作和
  disposition，不生成平行 Frame 或答案。
- `DecisionRecord`：用户决定或低风险推荐推断，绑定相关 FrameRevision/WorkPlanRevision。
- `QuestionRevision`：属于 `InvestigationCase` authority family 的 immutable input lineage；
  保存原始用户问题、纠正和 accepted clarification source refs，不定义 estimand。
- `SemanticBinding`：Primary Agent 对 accepted QuestionRevision 的 source-grounded typed
  解释；每个 material assertion 都接受独立 semantic consistency pass。
- `MessageIngressRecord` / `MessageImpactBinding`：跨阶段 follow-up 的 durable typed saga；
  Primary Agent 判断开放 message impact，controller 只执行持久化、CAS 与 fencing。
- `CaseMailbox` / `OperationIdentity`：所有用户 command 和异步 job 的 durable ingress 与
  causation/correlation 身份；mailbox authority epoch 在 correction 到达时立即 fence 旧工作。
- `OutboxMessage` / `JobLease`：跨进程 at-least-once job、expected head/epoch fence、
  heartbeat、lease expiry 与 effectively-once authority admission。
- `FrameCandidateBundle`：未接受 proposal 的 durable review saga，绑定 candidate hash、
  question head、review request/result 与 disposition。
- `ResolvedMeasurementInstance`：从 accepted Frame、calendar、contract 与 snapshot/release
  确定性派生、内容寻址、无独立 accepted head 的执行实例；accepted Plan 是唯一 adoption
  point。
- `MeasurementResolutionOutcome`：system-derived `resolved_instance |
  typed_resolution_boundary`；boundary 也保持 requirement/obligation/claim 权威链。
- `EvidenceRequirementSpec`：Frame 拥有的 claim/evidence closure requirement。
- `ResolvedEvidenceObligation`：deterministic compiler 从 requirement 与 resolution outcome 单向
  派生的执行闭环对象。
- `QueryBindingEnvelope`：Gate 3 的 logical query authority contract；Gate 4 的 physical
  QuerySpec 只能消费它。
- `EvidenceUseBinding`：新 claim/Frame 使用已有 Evidence 时的 scope、identity、strength
  compatibility proof。
- `EvidenceValidityRecord` / `ObligationSatisfactionRecord`：system-owned append-only
  disposition/projection，禁止修改 EvidenceRecord 或 obligation definition。
- `SettlementPreconditionReport`：系统派生的 settlement 前置报告；Agent、capability 和
  test harness 无权写入。
- `ConformanceExecutionProvenance | PhysicalQueryExecutionProvenance`：按可信 realm 封闭
  execution proof；Gate 3 test Evidence 不使用 future QuerySpec 占位。
- `QuerySpec`、`CapabilityInvocation`、`ToolAttempt`、`ResultHandle`：Gate 4 生产执行与
  恢复记录；它们不定义业务口径。

`InvestigationCase` 的 head 更新只移动指针。旧 revision/version 保持可寻址，head 移动
不能原地改写其内容。event journal 记录 head acceptance 事实，但 journal 本身不能决定或
修复 head。

### 3.3 Revision 规则

| 变化 | 必须创建的对象 | 例子 |
|---|---|---|
| 用户改变问题目标、scope、约束或明确纠正语义 | `QuestionRevision`，并失效当前 Frame/Plan/Answer heads | 从“解释本月下降”改为“判断上季度活动是否有效” |
| estimand、时间含义、baseline、观察单位、分子分母、exposure 或 comparison 变化 | `AnalysisFrameRevision`，随后创建引用它的 `WorkPlanRevision` | 从“月初金额占比”改为“月初用户人均金额” |
| assumptions、alternatives、falsification、reversal、evidence requirement 或 epistemic completion 变化 | `AnalysisFrameRevision` | 加入“发薪日暴露”替代解释 |
| 调查任务、依赖、优先级、能力路线或 execution budget/stop 变化，业务口径不变 | `WorkPlanRevision` | 补充 channel bridge 与 sensitivity |
| provider timeout、网络重试、幂等重放、同参数重试 | 保持当前 revision；新增 `ToolAttempt` / event | ClickHouse 短暂不可达 |
| capability 参数修正导致目标 population、grain 或 comparison 变化 | 先创建 `AnalysisFrameRevision` | 查询发现用户定义粒度无法支撑原观察单位 |
| capability 参数修正只恢复同一已接受业务任务 | 保持 revision；新增 attempt | 临时连接失败后重试 |
| claim 内容、证据绑定或适用边界变化 | `AnswerVersion` | 局部 claim 降级为 provisional |

settled `AnswerVersion` 必须绑定 exact accepted QuestionRevision、FrameRevision、
WorkPlanRevision、EstimandSpec set、semantic/authority/resolution-outcome/execution identities、
EvidenceRequirementSpec/ResolvedEvidenceObligation set、EvidenceUseBinding set、
Reviewer disposition set、SettlementPreconditionReport 和 verifier policy version。
任一 accepted head 变化时，旧 settled version 保持历史可寻址；它不再代表当前 case head。
当前答案需要新建 AnswerVersion 并重新完成受影响 claim 的 settlement。

## 4. Agent 与系统边界

### 4.1 Primary Agent typed actions

```text
revise_frame
revise_plan
inspect_semantics
run_probe
call_capability
run_sensitivity
record_interpretation
ask_user
propose_answer
stop
```

每个 action 使用带版本的输入/输出 schema，包含 `case_id`、`action_id`、expected heads、
幂等键、operation/causation/correlation identity、authority revision、payload hash、业务目的、
目标 claim 或 task、参数和预期证据。controller 只接受合法 action，校验 expected heads 与
mailbox authority epoch，并把动作结果写入 event journal。

开放业务意图、纠正、挑战与澄清文本由 typed LLM binding 处理。本地系统不使用关键词字典
猜测开放语义。

### 4.2 确定性系统职责

- SQL 安全、允许的数据源和只读限制。
- 身份、线程归属、权限、隐私、固定敏感输出与稀疏样本规则。
- metric/dimension/data contract、snapshot/release、grain 与 exposure 可执行性。
- question/frame/measurement/binding/instance identity、source span 与 downstream
  compatibility。
- typed state、revision CAS、幂等、lease、checkpoint、retry、resume 与 outbox。
- QuerySpec 编译、参数校验、result handle 完整性与 provenance。
- EvidenceRecord 不可变性、claim/evidence/frame 兼容性。
- 数字绑定、单位、分母、文字方向、比较方向和展示一致性。
- Reviewer 风险触发、异议 disposition 与 settled 发布门禁。
- 所有正常用户使用同一套数据集解析、capability、调查路线、证据强度和结论发布强度。
  用户身份只用于 thread/case 归属、性能安全、审计和限流。

### 4.3 模型职责

- 开放业务语义、source-grounded binding 和可组合测量设计候选。
- 自主提出主 estimand、合理 alternatives 与 sensitivity；缺少不可检验的 material 业务决定
  时执行 ask_user。
- 动态调查路线、替代解释、证伪与反转条件。
- 基于已接受证据的解释、洞察和叙事。
- 发现无法从 source、contract、data availability 或低成本 investigation 查明的高影响歧义
  时提出业务化选项。
- 基于硬边界反馈局部修订 frame、plan、interpretation 或 answer。

模型不能直接写权威 heads、绕过 capability、升级证据强度、改写 snapshot/release 或覆盖
Reviewer objection。

高价值 LLM 节点默认等待真实回答。只有显式配置正数 timeout 时 provider 层可以中止并按
统一策略重试。业务节点不实现分散 retry，也不使用本地模板补写测量设计、洞察或最终回答。

### 4.4 决策与澄清协议

- Gate 访谈遵循本文 0.1：一次一个重大决定，等待用户确认。
- runtime 选择会实质改变业务结论、baseline、时间语义、敏感输出、数据访问、claim 强度
  或显著执行成本，且无法从 source、contract、data availability 或低成本 investigation
  查明时，Primary Agent 生成 2–3 个业务选项、推荐解释、接受推荐继续的选项和
  `tell the agent to do differently` 出口。
- 可检验的设计分歧由 Primary Agent 选择主设计，记录 DecisionRecord，并把合理替代纳入
  alternatives/sensitivity。
- 低风险缺口采用推荐推断继续，写入 `DecisionRecord`，并由 accepted Frame/Plan 引用。
- 用户角色和数据能力等级不得成为业务澄清项。

## 5. 目标目录与服务边界

```text
vnext/
├── README.md
├── .python-version
├── pyproject.toml
├── uv.lock
├── package.json
├── package-lock.json
├── apps/
│   └── workbench/                 # Next.js Chat + Analysis + Workflow 与 TS gateway
├── services/
│   ├── command_api/               # 短事务 ingress，立即返回 runId/cursor
│   ├── agent_worker/              # case mailbox 与 Primary Agent controller worker
│   ├── job_workers/               # LLM/capability/reviewer effect workers
│   ├── projection_stream/         # journal projection 与 SSE/WebSocket transport
│   └── analysis_core/
│       └── src/waje_vnext/
│           ├── domain/            # 五类权威对象、typed state、纯不变量
│           ├── agent/             # Primary Agent binding、ContextPacket、typed actions
│           ├── controller/        # durable async state machine、admission、fencing、恢复
│           ├── capabilities/      # capability fabric 与 EvidenceRecord 构造
│           ├── semantics/         # metric/dimension/factor/data contract 解析
│           ├── query/             # QuerySpec、SQL compiler 与 governed escape hatch
│           ├── trust/             # claim binding、数字/文字 verifier、Reviewer
│           ├── projection/        # customer-safe Answer/Analysis/Workflow projection
│           ├── storage/           # repository ports 与 PostgreSQL adapters
│           └── providers/         # LLM、ClickHouse、PostgreSQL adapter
├── contracts/
│   ├── api/                       # OpenAPI / transport schemas
│   ├── domain/                    # language-neutral versioned schemas
│   ├── semantics/                 # metric/dimension/factor/capability contracts
│   └── events/                    # event journal schemas
├── storage/
│   └── migrations/                # vNext 独立 PostgreSQL schema 与 ledger
├── evals/
│   ├── cases/
│   ├── expectations/
│   └── runners/
├── tests/
│   ├── contract/
│   ├── unit/
│   ├── integration/
│   ├── replay/
│   ├── e2e/
│   └── deletion_independence/
├── ops/                            # 独立本地与发布配置
└── tools/                          # build/test/run/package/isolation commands
```

### 5.1 服务职责

| 边界 | 所有权 |
|---|---|
| Command API | 鉴权、用户消息持久化、case 创建/唤醒、短事务 mailbox + journal + outbox；立即返回 runId/cursor |
| Primary Agent Controller Worker | 按 case 串行消费 durable mailbox，调度异步 job，并在短事务内提交 accepted authority |
| LLM/Capability/Reviewer Workers | 事务外执行耗时 effect，持有可续租 job lease，返回 immutable receipt/result |
| Projection/Streaming | 从 journal 和 accepted heads 重建 customer-safe projection，通过 cursor SSE/WebSocket 推送 |
| TypeScript Workbench/Gateway | 页面、会话接入、流式 transport、customer-safe projection 展示；不持有编排或 BI 权威 |
| Python Analysis Core | Primary Agent、controller、authority repositories、capabilities、semantic/query、trust、projection |
| PostgreSQL | mailbox、authority objects、accepted heads、event journal、checkpoint、outbox、job lease、result metadata、projection |
| ClickHouse | 受 QuerySpec 与 snapshot/release 约束的分析查询 |
| Object/result storage | 大结果内容寻址与稳定 handle；PostgreSQL 保存 hash、schema、location 与生命周期 |
| LLM provider | typed reasoning；timeout/retry/circuit breaker 统一位于 provider 层 |

TypeScript 与 Python 通过版本化 API/event schema 通信。共享合同以 `vnext/contracts/` 为源，
生成物进入各自 build output，禁止手工维护两套含义。

### 5.2 并发与提交原则

WAJE 采用 case-scoped durable async runtime。耗时 LLM、semantic inspection、probe、
capability、sensitivity 和 Reviewer job 可以跨进程并发；每个 InvestigationCase 的业务
authority admission 沿单一串行通道提交。journal append、authority mutation 与 outbox
enqueue 位于同一短数据库事务。

delivery 采用 at-least-once。idempotency key、unique constraint、payload hash、accepted-head
CAS、mailbox authority epoch、job fencing token 和 immutable receipt 共同形成
effectively-once 状态变更。分布式 exactly-once 不进入设计假设。

## 6. 数据与迁移策略

### 6.1 Day 0 策略

- 新建独立 PostgreSQL schema `waje_vnext` 和独立 migration ledger。
- 新 migration 从版本 1 开始，不读取或升级 `waje_runtime`。
- 新对象使用 vNext ID namespace；不翻译旧 run、thread、plan、evidence 或 publication ID。
- ClickHouse 可访问同一受治理业务数据源，dataset contract、snapshot/release binding 和
  QuerySpec 均由 vNext 重新定义。
- 语义合同从业务 SSOT 和真实数据重新建立。旧 `contracts/` 只能作为核对输入，不能在
  runtime 中加载。
- `contracts/ssot/付费金额影响因子分析.mm` 作为 factor SSOT 的重建输入。Gate 1–4 把需要
  的业务定义复制为 vNext 自有、版本化且带来源 hash 的 semantic contracts；vNext runtime
  不读取旧路径。
- 本地开发使用独立环境变量前缀 `WAJE_VNEXT_`。密钥不进入日志、artifact 或仓库。
- Python 使用 3.12.13 toolchain 创建 `vnext/.venv`，project contract 要求 Python
  `>=3.12`；Python dependency graph 由 `uv.lock` 固定。

### 6.2 迁移验收

- clean database 可按顺序 apply、rollback development-only change、re-apply。
- 并发 migration 有 advisory lock，失败保持 ledger 与 schema 一致。
- repository contract test 在 PostgreSQL 上验证 CAS、immutability、head acceptance、
  mailbox ordering、authority epoch、event ordering、checkpoint/resume、outbox fence、
  job lease/heartbeat 和原子 rollback。
- 旧 schema 缺失时 vNext 运行正常。
- vNext schema 缺失时进程 fail closed 并给出可操作诊断。

## 7. Gate 计划

### Gate 0：现状解剖与 Day 0 隔离

**入口访谈判断**

- 状态：已执行。
- 结论：本 Gate 无需用户决策。
- 理由：实现根目录、零兼容、参考边界和产品范围已在任务中明确；仓库结构与依赖可自行调查。

**交付物**

- 本开发计划与对抗式自审记录。
- 旧目录、构建入口、数据入口、运行入口和测试入口现状清单。
- `vnext/` 独立根目录、包 namespace、最小 build/test/run 骨架。
- command、case controller worker、job worker、journal/projection 和 streaming 的逻辑部署
  边界；它们只通过 versioned contract 与 durable records 协作。
- 隔离策略与 forbidden dependency manifest。
- 独立 root Python/Node package manifests；Gate 0 验证 dependency graph 不指向仓库根级
  package 或旧目录，完整 Workbench build 在 Gate 6 验收。
- deletion-independence verifier：只复制 `vnext/` 与 machine-readable policy
  明示的根级 `.github/` deployment projection 到临时目录后执行 compile、test、
  workflow validation 与 smoke run。
- Gate 0 验收报告。

**Exit criteria**

- [x] `vnext/` 内不存在对旧生产目录、旧包名、旧 runtime 命令或旧 schema 的依赖。
- [x] machine-readable isolation policy 覆盖 import、dynamic path、shell、SQL、dependency
  manifest 和 artifact 扫描。
- [x] 隔离 verifier 在仓库内和临时空目录各通过；provider-required `.github/`
  projection 在 clean copy 中逐文件验证且不依赖历史目录。
- [x] 最小 Python runtime 可 compile、test、run。
- [x] clean copy 使用 Python 3.12 virtualenv 完成 wheel build，低版本解释器 fail closed。
- [x] 新根目录、服务边界、环境变量和数据库 namespace 已文档化。
- [x] 服务边界已按 durable command/worker/journal/projection/streaming 模型回审，未把
  `asyncio` 或长 HTTP 请求当作跨进程运行基础。
- [x] 旧系统删除顺序与最终删除演练可执行。
- [x] 对抗式自审中的 Gate 0 blocking finding 已清零。

### Gate 1：权威对象与存储合同

**入口**

- 先评估第五类权威对象、AnswerVersion settled 语义、Frame acceptance 权限是否仍需用户决定。
- 默认推荐：保留 `InvestigationCase` 为稳定 head 容器；Reviewer objection 作为从属 Trust
  record；settled 表示当前 Frame 下逐 claim 验证和风险 disposition 完成。
- 2026-07-29 用户已确认 `InvestigationCase` 为第五类权威对象。它只保存 identity、
  lifecycle 和 accepted heads 的 CAS 指针。
- 其余 admission 与澄清边界已有项目合同：低风险推断可继续并记录；影响业务结论、
  baseline、时间语义、数据安全、claim 强度或显著执行成本的歧义必须询问用户。本 Gate
  无需第二项用户决策。

**交付物**

- 五类权威对象 schema 与 invariants。
- typed actions、admission result、ContextPacket 和 event journal contract。
- CaseMailbox、OperationIdentity、authority epoch、controller wake、异步 job 与 expected
  head/epoch fence contract。
- PostgreSQL v1 migrations、repository ports/adapters、CAS 与 append-only 写入。
- 状态机、revision supersession、idempotency、checkpoint、outbox contract。
- journal append、authority mutation 与 outbox enqueue 的同事务 contract；worker job
  lease、heartbeat、expiry 和 fencing token。
- generated TypeScript contract bindings、Python typed domain 与双向 schema contract tests；
  只支持当前 schema version，避免维护第二套生成式 Python runtime。

**Exit criteria**

- [x] 所有 authority mutation 经过 CAS/admission。
- [x] EvidenceRecord 与 accepted revision 不可原地修改。
- [x] 工具 retry 不创建 Frame/Plan revision。
- [x] 口径变化缺少新 FrameRevision 时 admission 拒绝。
- [x] ContextPacket 可由持久化状态确定性重建并 hash 相同。
- [x] event journal 支持幂等 append、case 内单调 cursor 和 customer-safe projection。
- [x] command/event/action/job 带 operation、idempotency、causation、correlation、
  authority revision 与 payload hash。
- [ ] storage authority mutation event 显式接收 causal operation，并移除 production path
  的静默缺省派生；该项纳入 G3.2 operation-lineage blocker。
- [x] duplicate ingress 只产生一个 mailbox authority；mailbox + journal + outbox 失败整体
  rollback。
- [x] outbox effect job 绑定 admitted action、accepted head 和 mailbox epoch。

### Gate 2：单主 Agent runtime

**入口**

- 评估 controller 框架、provider 约束和人工中断语义。
- 默认推荐：WAJE-owned controller 掌握 typed action admission 与持久化；模型 provider 通过
  窄 adapter 接入；高价值节点默认等待真实回答。
- 2026-07-29 用户确认 WAJE-owned controller 是唯一 runtime 权威；LangGraph 不进入
  authoritative action loop。
- provider timeout、高价值节点等待与 `ask_user` 阻塞边界已有项目合同。本 Gate
  无需追加用户决策。

**交付物**

- Primary Business Analysis Agent binding。
- durable async controller、case lease、job lease/heartbeat storage contract、
  checkpoint/resume、outbox、provider retry；periodic heartbeat supervisor 与旧
  fencing-token rejection 在 G3.2 关闭。
- Primary Agent LLM job、`WAITING_FOR_LLM`、`WAITING_FOR_EFFECT`、`WAITING_FOR_REVIEW`、
  `WAITING_FOR_USER` 与多 pending job state。
- 任意运行阶段的 message ingress、correction authority epoch 与 stale-result rejection。
- frame/plan 动态 revision、局部失败恢复、ask_user interruption。
- deterministic fake provider 与真实 provider acceptance harness。
- concurrent resume、duplicate delivery、stale head 和 crash boundary tests。

**Exit criteria**

- [x] 单一 Primary Agent 持有开放业务语义。
- [x] crash/restart 后从 accepted heads 与 event cursor 恢复。
- [x] stale action 不能覆盖新 head。
- [x] 内容问题局部修订，口径变化生成 FrameRevision。
- [x] timeout/retry 只在 provider 或 tool supervision 层发生。
- [x] 用户 command 在短事务后返回 durable cursor；Primary Agent provider 不在 command
  request 调用栈执行。
- [x] LLM/effect 在数据库事务外执行，提交前重检 accepted head 与 authority epoch。
- [x] correction-vs-LLM/effect、duplicate ingress、atomic outbox failure、concurrent worker
  和 crash/resume 测试通过。
- [x] at-least-once job delivery 通过幂等、CAS、唯一约束和 receipt 达到 effectively-once
  authority mutation。

Exit evidence：
`docs/reviews/2026-07-29-bi-agent-vnext-gate-2.md`；
`docs/reviews/2026-07-30-bi-agent-vnext-gate-0-2-durable-async-realignment.md`。

### Gate 3：Universal Measurement Authority

**入口访谈判断**

- 状态：已执行。
- 结论：本 Gate 无需用户决策。
- 理由：用户已明确 Primary Agent 自主提出开放测量设计，确定性系统验证结构、权威连续性、
  日历、data contract、证据和发布边界。
- Gate 0–2 realignment audit：
  `docs/reviews/2026-07-30-bi-agent-vnext-gate-0-2-realignment-audit.md`。
- Gate 0–2 durable async amendment：
  `docs/reviews/2026-07-30-bi-agent-vnext-gate-0-2-durable-async-realignment.md`。
- focused implementation plan：
  `docs/plans/2026-07-30-bi-agent-vnext-gate-3-universal-measurement-authority.md`。
- plan adversarial review：
  `docs/reviews/2026-07-30-bi-agent-vnext-gate-3-plan-adversarial-review.md`。
- behavior-eval adversarial review：
  `docs/reviews/2026-07-30-bi-agent-vnext-gate-3-behavior-eval-adversarial-review.md`；
  authoring draft 打开 10 个 Blocking、11 个 Major。
- G3.E0 trust implementation：
  `docs/reviews/2026-07-30-bi-agent-vnext-gate-3-e0-trust-implementation.md`；
  Episode v2、Source/Review/Corpus/Grader registries、双视图、三层 verdict、cross-Gate
  profiles、review packages 与 derived readiness 已落地；本地 verifier 拒绝全部
  repository-local self-signing，claim/boundary ceiling、truth support、counterfactual
  materialization 和 manifest history 均 fail closed；public GitHub remote、
  GitHub/Sigstore verification contract、request/provider-state schema、privilege-separated
  workflow、唯一签发 job、complete admission-authority binding、双 predecessor provenance
  与攻击测试已落地；candidate runtime measurement 已 hash-bound，尚未形成 hermetic runtime
  closure；protected environment activation、trusted workflow pin、首个真实 bundle、
  trusted canonical connector、monotonic provider-state/admission CAS、digest-pinned builder、
  来源、双审、
  truth/per-claim review、calibration、held-out、promotion/run freeze 仍阻止 formal
  admission 与 gold promotion。
- durable async adversarial review：
  `docs/reviews/2026-07-30-bi-agent-vnext-durable-async-gate3-adversarial-review.md`；
  periodic heartbeat、terminal JobDisposition、obligation scheduler、Reviewer worker 与
  operation lineage 是 G3.2 blocking work。
- 组合审查第一轮 20 个 Blocking、8 个 Major；closure verification 再打开 6 个 Blocking、
  6 个 Major。两轮 finding 均已写入设计；G3.1 实现对抗式收口见
  `docs/reviews/2026-07-30-bi-agent-vnext-gate-3-1-implementation.md`。
- Gate 1/2 历史验收保持；G3.1/G3.2 是任何新 Gate 3 业务 Evidence、Answer 和 Workflow
  实现的硬前置。
- G3.E0 formal admission 仍要求冻结与实现无关的 EvaluationEpisode 合同、候选集、
  反事实关系、review/calibration 流程与 coverage ledger。用户已明确授权 G3.1 local
  implementation development override；代码完成不改变 `deny_g3_1`。

**交付物**

- QuestionRevision、跨阶段 correction、source-grounded SemanticBinding 与 accepted question
  head。
- case mailbox 上的 durable MessageImpactBinding saga；LLM、capability、sensitivity 和
  Reviewer 通过 async job 运行，authority admission 按 case 串行。
- obligation-aware fan-out/fan-in、乱序完成、duplicate delivery、worker lease/heartbeat、
  crash/resume 和 stale-result rejection。
- durable FrameCandidateBundle review saga 与独立 MeasurementObjection。
- 显式 EstimandSpec、条件完备的 AnalysisFrame measurement algebra、
  EvidenceRequirementSpec 与 ResolvedEvidenceObligation。
- semantic/authority/resolution-outcome/logical identity、tagged conformance/production
  execution provenance、typed scope 和 cross-language canonical codec。
- TemporalSemanticSpec、WindowRuleSpec、ResolutionContext、DataVersionSpec 与
  unit/exposure aggregation algebra。
- ResolvedMeasurementInstance 的无 head 确定性派生，以及 accepted Plan 的唯一 adoption。
- Gate 3 QueryBindingEnvelope 与 Gate 4 physical QuerySpec compiler 的所有权边界。
- capability-result/Evidence atomic admission、EvidenceUseBinding 与 crash/resume。
- ModelInvocationRecord、RunTraceManifest、SettlementPreconditionReport。
- provisional Answer 与 execution/obligation/publication/delivery 四轴 Workflow。
- machine-frozen eval manifest、real-provider semantic/frame lane、full-authority conformance
  lane 与 independent Reviewer lane。
- behavior-first EvaluationEpisode corpus：真实/专家措辞、业务世界、决策风险、可接受
  结果空间、禁止结果、反事实 siblings 与分层 grader。
- 用户提供的八类付费金额真实问题形成八个独立 candidate Episode：变化解释、规律、事件
  影响、健康度、维度/因子归因、异常、多基准和证据质量；它们不收窄其他问题家族。

**Exit criteria**

- [ ] contract-supported 问题形成 executable design；clarification/boundary 符合 case
  `required_disposition` 与 `allowed_dispositions`。
- [ ] material assertions 全部有 grounding 和独立 semantic consistency pass。
- [ ] Frame 条件完备地表达 definition、data quality、scalar/rate/distribution/time series、
  contrast、cohort、funnel、decomposition、association、causal challenge 与 diagnostic set。
- [ ] calendar、actual range、data version、expected/observed/valid exposure、eligibility 与
  aggregation algebra可审计。
- [ ] requirement → obligation、Frame → resolution outcome → Plan adoption 保持单向可证明。
- [ ] typed scope 与 identity 贯穿 Gate 3 conformance execution、Evidence、claim 和
  Workflow；Gate 4 的 physical execution 必须消费同一封闭合同。
- [ ] correction、review、effect、Evidence admission 并发与 crash/resume 全部 fail safe。
- [ ] independent obligations 可并行，乱序结果逐个重检 accepted heads/epoch；旧 result
  只能进入 superseded 审计。
- [ ] QueryBindingEnvelope/capability 无平行业务口径入口，Gate 3 不生成生产物理 QuerySpec。
- [ ] technical retry 与 Frame/Plan revision 的 identity 边界通过。
- [ ] provisional Answer 不触发 settled、completed 或 delivered Workflow。
- [ ] real-provider 两条 lane、独立 Reviewer 与 complete WAJE trace 通过。
- [ ] 旧 Gate 3 实现、artifact、fixture 不进入当前依赖或 acceptance。
- [ ] 对抗式审计 blocking findings 为 0。
- [ ] G3.E0 corpus 达到 Gate3EvalPolicy，真实用户来源和 business/measurement review
  均可验证；合成措辞不得冒充真实用户样本。

### Gate 4：完整 capability fabric

**入口**

- 评估 capability 边界、通用 SQL escape hatch 的批准条件和核心问题家族 coverage。
- 任何 capability 划分需先验证可跨多个问题家族复用。

**交付物**

- semantic inspection、coverage/profile probe、pattern/comparison、formula decomposition、
  segment bridge、distribution/outlier、cohort/funnel/retention、event/context evidence、
  sensitivity/falsification 等能力族。
- 消费 Gate 3 QueryBindingEnvelope 的 physical QuerySpec compiler、cost/row/grain/privacy
  guard、governed SQL escape hatch。
- capability registry 与 support records。
- 每个 capability 通过 typed CapabilityResultEnvelope 原生返回 immutable EvidenceRecord；
  system-owned EvidenceAdmissionRecord 决定它能否关闭 obligation；大结果返回稳定 handle。
- ClickHouse/PostgreSQL integration 与 contract/data gap classification。

**Exit criteria**

- [ ] capability 按业务分析模式定义，未出现单题专用 API。
- [ ] 所有查询绑定 metric/dimension/data contract 与 snapshot/release。
- [ ] raw SQL 只能由受治理 escape hatch 执行并产生完整 provenance。
- [ ] missing contract、unsupported grain、permission/privacy blocked 与 no signal 分离。
- [ ] result handle 内容寻址、可验证、可过期且 EvidenceRecord 保持不可变。

### Gate 5：Answer / Trust Plane

**入口**

- 评估 settled 门槛、风险触发矩阵、局部降级策略和 Reviewer independence。
- claim strength 采用问题家族 × claim type 阈值，数据缺口与证据强度分别建模。

**交付物**

- claim graph、typed scope/evidence compatibility、scope/limitation binding。
- 数字、单位、分母、比较方向和文字方向 verifier。
- 消费 Gate 3 SettlementPreconditionReport 的 provisional/settled transition、局部 claim
  降级与 answer versioning。
- 风险触发 Reviewer 与结构化 objection/disposition。
- answer projection、technical audit projection 与 publication outbox。

**Exit criteria**

- [ ] 每个业务 claim 可追溯至 FrameRevision 和 EvidenceRecord 或明确 boundary。
- [ ] Reviewer 只产出异议，不创建平行答案。
- [ ] 局部问题只影响相关 claim/block。
- [ ] settled answer 无 unresolved blocking objection。
- [ ] 证据不足、missing contract、unsupported grain 或 permission blocked 不产生过强主结论。

### Gate 6：双栏工作台

**入口**

- 评估 v0 clone 视觉基线、移动端行为、evidence 展开深度和 Workflow 信息密度。
- UI 不暴露 prompt、内部 verifier、SQL retry、模型节点或敏感内部字段。

**交付物**

- 左侧 Chat，右侧 Analysis Workspace。
- Analysis Workspace 与 Workflow View 切换。
- Workflow 由 accepted WorkPlan + event journal 生成只读业务节点。
- Answer、claim、evidence、limitation、frame/plan revision 和 pending decision 可展开。
- 持久化 projection、SSE cursor、刷新恢复、静态 fallback。
- 视觉回归、accessibility、responsive 与真实浏览器 e2e。

**Exit criteria**

- [ ] UI 不拥有编排状态或权威。
- [ ] 刷新、断线重连和跨设备读取保持同一持久化 projection。
- [ ] event chronology 不完整时显示静态状态，不合成计时或虚构进度。
- [ ] customer-safe projection 通过敏感字段和稀疏样本检查。
- [ ] Workflow 节点只显示业务任务、真实状态、证据和限制。

### Gate 7：全覆盖、eval 与删除独立性

**入口**

- 评估核心问题家族 launch matrix、通过阈值、真实 provider 运行预算和发布环境。
- 不以 Gate 3 单题通过替代完整 launch coverage。

**交付物**

- 核心问题家族 × factor/capability × claim type acceptance matrix。
- 三类样本池：真实用户问题、历史失败案例、矩阵生成边界案例。
- business gold → system draft → human review expectation maintenance。
- deterministic replay、真实数据 eval、真实 provider eval、UI e2e。
- 删除旧目录后的 build/test/run/package/publish dry-run。
- release manifest、SBOM、migration manifest、rollback/incident runbook。

**Exit criteria**

- [ ] 所有 launch-required matrix cells 达到规定 evidence/data contract state。
- [ ] 历史失败案例依据当前合同重新表达并通过。
- [ ] full acceptance、replay determinism、recovery、security 与 UI 验收通过。
- [ ] 只保留 `vnext/`、policy 列出的最小 `.github/` deployment projection 和发布所需
  仓库元数据时可构建、测试、运行和打包。
- [ ] release artifact 不含旧路径、旧包、旧 schema、旧 fixture 或旧 contract。
- [ ] 旧实现可整体删除；删除不会改变 vNext release hash 中的运行依赖集合。

## 8. 测试与 eval 矩阵

| 层 | 核心对象 | 主要测试 | 失败分类 |
|---|---|---|---|
| Domain contract | 五类权威对象、typed actions | schema、property、immutability、revision rules | invalid authority / illegal transition |
| Question/measurement | QuestionRevision、SemanticBinding、measurement graph/identity | source grounding、graph completeness、metamorphic、mutation | authority drift / wrong estimand |
| Storage | mailbox、repositories、journal、checkpoint、outbox、job lease | PostgreSQL integration、atomic commit、concurrency、heartbeat takeover、crash recovery | persistence / ordering / idempotency / fence loss |
| Context | ContextPacket | reconstruction、boundedness、redaction、hash stability | missing context / leakage / stale head |
| Controller | durable async state machine | correction-vs-job、parallel obligations、乱序完成、duplicate delivery、stale action/result、retry/resume | orchestration / recovery / authority race |
| Semantic/data | metric、dimension、snapshot、grain | contract fixtures + live schema profile | missing contract / unsupported grain / data absent |
| Query/capability | QuerySpec、capability | compile golden、SQL safety、live ClickHouse | query / capability / provenance |
| Evidence | EvidenceRecord、result handle | immutability、hash、scope compatibility | evidence mismatch / missing provenance |
| Trust | claims、AnswerVersion、Reviewer | numeric binding、direction、scope、risk matrix | overclaim / contradiction / unresolved objection |
| Replay | authority + journal | deterministic projection、partial chronology | replay divergence / synthetic state |
| UI | Chat/Analysis/Workflow | component、browser e2e、visual、a11y | projection / usability / leakage |
| Business acceptance | 问题家族 × factor/capability | real wording + structured expectation | business failure + responsibility point |
| Isolation/release | `vnext/` + policy-bound `.github/` projection | clean-copy build/test/workflow validation/run/package | forbidden dependency / packaging |

### 8.1 核心问题家族初始集合

Gate 1–4 可以扩充，Gate 7 前不得缩减为证明切片：

- metric definition / measurement design；
- trend、周期、同比/环比和基线比较；
- composition、denominator 与 mix shift；
- segment bridge 与 contribution attribution；
- formula decomposition 与 accounting explanation；
- distribution、outlier 与 concentration；
- cohort、retention 与 funnel；
- event/context association；
- data quality、coverage 与 contract challenge；
- sensitivity、alternative explanation、falsification 与 reversal；
- follow-up、scope revision、challenge 与 evidence explanation。

2026-07-30 用户提供的付费金额问题集作为真实用户 slice，覆盖变化解释、规律、事件影响、
收入健康、维度/因子归因、异常/黑天鹅、多基准和数据质量/证据检查。它们用于检验上述家族
的组合行为，不能成为 question router、固定 capability 路线或 launch 范围边界。

### 8.2 EvaluationEpisode 与 expectation envelope

业务验收的基本单位是实现无关的 `EvaluationEpisode`，每个 Episode 至少包含：

- stable episode ID、catalog/policy version 与可核验 source provenance；
- 自然用户对话，可包含 clarification、correction、challenge 与 scope revision；
- 独立于 WAJE 实现的 business world、数据条件、隐藏 evaluator truth 与决策风险；
- 必须保持的业务含义、必须调查的问题和多个可接受 measurement design family；
- allowed dispositions、allowed boundary codes、claim ceiling 与 clarification policy；
- 明确的 forbidden outcomes；
- 至少三个最小反事实 sibling，分别检测 meaning preservation、measurement change 和
  boundary/interaction change；
- deterministic hard checks、calibrated semantic rubric、trace obligations 与 human-review
  要求。

Episode 禁止规定 action 顺序、工具序列、SQL 形状、唯一 Frame 或内部 Workflow 节点。
authority、identity、calendar、persistence、Evidence admission 和 publication 另由
conformance suites 逐项验证；codec、repository、provider 和 projection 由 implementation
tests 验证。三层结果分别记账，业务 Episode 失败不能被局部测试分数抵消。

Gate 3 的 checked-in `Gate3EvalPolicy`、episode catalog、grader rubric 与 manifest validator
同样适用于 Gate 7。run manifest 只能扩展 policy，不能改写 acceptable outcome、forbidden
outcome、claim ceiling、boundary code 或最低覆盖 floor。

eval 失败不能自动升级为 runtime guardrail。升级需要人工确认、产品与数据/平台双 owner、
可复用模式和相关 eval slice 回归。

## 9. UI 里程碑

| Gate | UI 能力 |
|---|---|
| Gate 0 | 目录与 transport/projection 边界 |
| Gate 1 | authority/event schema 的 TypeScript bindings |
| Gate 2 | cursor-based headless event contract、durable runId 与 pending user decision contract |
| Gate 3 | question/frame/measurement/evidence/claim identity 与 Workflow 状态 projection fixture |
| Gate 4 | capability evidence/result handle 展开合同 |
| Gate 5 | claim scope、limitation、provisional/settled、Reviewer disposition |
| Gate 6 | 完整双栏工作台与真实浏览器验收 |
| Gate 7 | 全问题家族 replay、性能、安全、可访问性和发布验收 |

## 10. 风险与访谈触发点

| 风险 | 早期信号 | 默认处理 | 必须访谈的条件 |
|---|---|---|---|
| source-to-measurement authority drift | Frame 无法逐字段回指 QuestionRevision，或 downstream identity 改变 | 阻止 Frame/Evidence/publication acceptance，触发 MeasurementObjection 或 FrameRevision | 缺少的业务目标/政策无法从 source、contract、data 查明 |
| 测量设计含义不唯一 | 两个合理 estimand 会得出不同结论 | ask_user 前先跑低成本 semantic inspection | 选择会改变业务结论或 baseline |
| Frame/Plan revision 混淆 | 参数修正改变 population/grain | admission 强制新 Frame | revision 规则需要放宽 |
| 单题过拟合 | capability 名称或代码出现题目常量 | 抽象为问题家族能力并补跨 case 测试 | 通用化会显著扩大范围或成本 |
| Reviewer 演化成第二答案 | objection 含完整替代 narrative | schema 拒绝并只保留结构化异议 | 需要改变 Reviewer 产品职责 |
| UI 获取编排权威 | 前端本地推进节点或推断状态 | 只读 projection，服务端 accepted heads | 需要离线编辑/人工改 plan |
| 证据 hash 稳定但语义漂移 | contract/snapshot 未入 fingerprint | 扩大 provenance fingerprint | 会改变证据身份或 retention |
| result handle 过期 | answer 仍引用已清理大结果 | EvidenceRecord 保存必要摘要和 retention class | retention 成本与审计要求冲突 |
| LLM provider 能力不足 | typed action/schema 频繁失败 | provider adapter 统一重试与评测 | 需要换 provider 或改变产品质量门槛 |
| 数据覆盖不足 | matrix cell 为 missing contract | 明确 boundary 与升级路径 | launch-required family 无法达到门槛 |
| 性能成本失控 | 高成本 capability 反复运行 | plan budget、probe-first、cache by immutable key | 预算会改变默认调查深度 |
| correction 与旧 job 竞态 | 新消息到达后旧 LLM/effect 仍尝试提交 | mailbox epoch 立即 fence；result 保留审计，authority admission 拒绝 | 产品希望旧 scope 结果自动合并到新问题 |
| 并发 obligation 乱序 | 较晚计划的 job 先完成 | 每个 result 按 accepted head/epoch/obligation identity 独立 admission | 业务要求跨 obligation 原子成组发布 |
| worker 丢失或重复投递 | heartbeat 过期、provider 调用重复 | job lease/fencing + at-least-once + 幂等 receipt/CAS | 外部 provider 无法支持安全重试且成本边界会改变 |
| 删除独立性失真 | isolation test 依赖 repo root 或旧数据库 | 临时目录 + clean schema 验证 | 需要共享基础设施作为生产依赖 |

## 11. 提交与审查策略

### 11.1 提交

- 每个 Gate 使用独立分支或连续、可 review 的提交序列。
- 提交按合同、实现、测试、文档组织；每个提交保持 build/test 可解释。
- 禁止把旧实现修改与 vNext 生产实现混在同一提交。
- 允许从旧材料复制后重写；提交说明记录抽象来源和已消除的旧依赖。
- migration、contract breaking change 和 eval expectation change 单独提交。
- 生成物只有在可复现且发布需要时提交。

### 11.2 Gate 审查

每个 Gate 至少完成：

1. entry interview record；
2. architecture/invariant review；
3. implementation diff review；
4. deterministic tests；
5. 需要时执行 live data/provider/browser acceptance；
6. adversarial review；
7. exit evidence manifest；
8. Gate 状态与后续风险更新。

Gate evidence manifest 至少包含 source revision、环境指纹、执行命令、开始/结束时间、
exit code、测试摘要、artifact path 与 SHA-256、已知限制和 reviewer disposition。缺少
evidence manifest、存在未处置 blocking finding 或关键命令失败时，Gate 状态保持
`In progress`。

Reviewer 检查重点：

- 是否引入旧实现依赖；
- 是否把开放业务语义硬编码进本地逻辑；
- 是否把业务口径变化伪装成 retry；
- 是否让 UI、event journal、runtime framework 或 Reviewer 获取业务权威；
- 是否存在无证据 fallback、高价值模板回答或单题特例；
- 是否通过局部降级保留无争议 claim；
- 是否达到问题家族级验收。

## 12. 删除独立性最终验收协议

Gate 0 建立 verifier，Gate 7 执行完整协议：

1. 生成 clean temporary workspace。
2. 只复制 `vnext/`、policy 列出的 `.github/` deployment projection、license 和发布所需
   最小仓库元数据。
3. 清除 repo root `PYTHONPATH`、Node resolution path、旧环境变量和旧 build cache。
4. 使用 clean PostgreSQL 创建 `waje_vnext`，确认 `waje_runtime` 不存在。
5. 使用允许的独立 ClickHouse test dataset 或 hermetic fixture。
6. 安装 lockfile 依赖。
7. 运行 format/lint/typecheck/compile/unit/contract/integration/replay/UI/eval smoke。
8. 启动 Python service、worker、TS gateway/workbench，完成 health 与一条 hermetic analysis。
9. 构建 release package、SBOM、migration manifest 与 static assets。
10. 扫描 artifact、source map、import graph、SQL 和 manifest 中的 forbidden legacy references。
11. 在 package-only 环境执行 smoke run。
12. 对比 clean-copy build 与正常 build 的依赖 manifest；差异必须为零或有非运行时解释。
13. verifier 的扫描规则、allowlist 和输出本身进入 release evidence，防止通过修改 scanner
    隐藏 legacy dependency。

## 13. Gate 状态表

| Gate | 状态 | 入口访谈 | Exit evidence |
|---|---|---|---|
| Gate 0 | Complete | 本 Gate 无需用户决策 | `docs/reviews/2026-07-29-bi-agent-vnext-gate-0.md` |
| Gate 1 | Complete | 已确认 `InvestigationCase`；无其他用户决策 | `docs/reviews/2026-07-29-bi-agent-vnext-gate-1.md` |
| Gate 2 | Complete + durable async amendment | 已确认 WAJE-owned controller；本 amendment 无需用户决策 | `docs/reviews/2026-07-30-bi-agent-vnext-gate-0-2-durable-async-realignment.md` |
| Gate 3 | G3.1 local implementation complete under development override; G3.E0 formal admission remains `deny_g3_1`; G3.2+ pending | 已确认 public GitHub Artifact Attestations/Sigstore；protected review、trusted workflow revision、首个 bundle、canonical provider entry、receipt CAS、authority roots、显式 estimand、可执行 counterfactual、真实来源与独立双审待关闭 | `docs/reviews/2026-07-30-bi-agent-vnext-gate-3-1-implementation.md` |
| Gate 4 | Pending | 待执行 | — |
| Gate 5 | Pending | 待执行 | — |
| Gate 6 | Pending | 待执行 | — |
| Gate 7 | Pending | 待执行 | — |

## 14. Decision Log

| 日期 | 决定 | 来源 | 影响 |
|---|---|---|---|
| 2026-07-29 | vNext 为全新实现，零兼容 | 用户任务 | 旧实现全量隔离，不设计迁移兼容层 |
| 2026-07-29 | 当前产品闭环止于可追溯回答 | 用户任务 | 排除监控、预测、自动行动、办公协同和 general agent |
| 2026-07-29 | 一个 Primary Business Analysis Agent 持有开放业务语义 | 用户任务 | 无平行业务答案 Agent |
| 2026-07-29 | `AnalysisFrameRevision` 是测量设计唯一权威 | 用户任务 | 口径变化强制 Frame revision |
| 2026-07-29 | Workflow 是 accepted plan + event journal 的只读业务投影 | 用户任务 | UI 与 workflow view 不拥有编排 |
| 2026-07-29 | vNext 根目录为 `vnext/` | Gate 0 仓库调查 | 与全部旧生产目录形成可机械验证边界 |
| 2026-07-29 | 第五类权威对象暂定 `InvestigationCase` | Gate 0 架构归纳 | Gate 1 入口复核；不影响 Gate 0 隔离 |
| 2026-07-29 | Python 最低版本为 3.12，Gate 0 使用 3.12.13 virtualenv | 用户补充 | 宿主 Python 不影响 vNext baseline；clean-copy 验收重建 venv |
| 2026-07-29 | 确认 `InvestigationCase` 为第五类权威对象 | 用户确认 | Gate 1 以稳定 case root + 四类 immutable content authority 建模 |
| 2026-07-29 | WAJE-owned controller 为唯一 runtime 权威 | 用户确认 | LangGraph 不进入 authoritative action loop；typed state、CAS、journal 与 checkpoint 保持单一来源 |
| 2026-07-30 | 撤销旧 Gate 3 | 用户要求 + authority-drift 复核 | 删除错误 capability、Evidence、Answer、Workflow、fixture 与 artifact，不保留兼容 |
| 2026-07-30 | Gate 3 重定义为 Universal Measurement Authority | 用户要求 + Gate 0–2 audit | 先建立问题到测量的通用权威连续性，再进入完整 capability fabric |
| 2026-07-30 | QuestionRevision 归 `InvestigationCase` authority family | Gate 0–2 audit | 保存 immutable 用户输入 lineage；测量设计仍只属于 AnalysisFrameRevision |
| 2026-07-30 | Gate 3 只产出 provisional Answer | Gate 分层审计 | identity preconditions 在 Gate 3 fail closed；完整 settled publication 由 Gate 5 实现 |
| 2026-07-30 | G3.E0 采用 behavior-first EvaluationEpisode | 用户确认 + eval-first review | 测试先定义业务世界、可接受结果空间、禁止结果和反事实关系；G3.1 等待 readiness hard gate |
| 2026-07-30 | 合成措辞不计入真实用户来源 | 组合对抗审查 | 真实用户池、双审、held-out 和 calibration 缺口保持显式，禁止用生成样本补数量 |
| 2026-07-30 | runtime 采用整体异步、authority commit 局部同步 | 用户顶层架构要求 | command 短事务、durable mailbox、跨进程 job、case 串行 admission、head/epoch fence 与 cursor projection |
| 2026-07-30 | at-least-once 是 delivery 基础假设 | 用户顶层架构要求 | 依靠幂等、CAS、唯一约束、receipt 和 fencing 达到 effectively-once mutation |
| 2026-07-30 | 八类付费金额问题进入真实用户 candidate pool | 用户原始问题集 | 形成 8 个独立 Episode；真实措辞有 source trace，拟合 world/expectation 仍待双审 |
| 2026-07-30 | Gate 3 外部 authority 采用 public GitHub Actions + Artifact Attestations/Sigstore | 用户确认 | 使用 immutable repository IDs、protected ref/environment、exact workflow/source SHA、run/attempt、release/trust epoch、predecessor、完整 evaluator/runtime 与授权 hash；runner 无长期 signing key |
| 2026-07-30 | 根级 `.github/` 作为 vNext provider deployment projection | GitHub provider 约束 + Day 0 隔离复核 | 应用实现仍只在 `vnext/`；projection 由 vNext policy exact-hash 绑定，并随 clean-copy 删除独立性验证 |
| 2026-07-30 | 在 formal readiness 仍为 `deny_g3_1` 时先完成 G3.1 local implementation | 用户明确 development override | 允许 epoch-3 合同、存储、迁移与测试落地；gold promotion、protected admission 和 production Evidence 权限保持 fail closed |
