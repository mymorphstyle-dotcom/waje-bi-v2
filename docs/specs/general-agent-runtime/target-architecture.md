# WAJE BI v2 通用 Agent Runtime 目标架构

## 状态

框架决策已接受。2026-07-21 完成结构修复后，真实 DeepSeek 应用链评测 7/7 通过，覆盖直答、
能力目录、明确 BI 分析、描述性比较澄清、因果基线澄清及恢复、异常敏感性分析和已发布证据
追问。隔离 PostgreSQL v9→v12 升级及当前目标 v12→v13 升级、`REPEATABLE READ READ ONLY`
审计和完整部署门禁通过。
所有验收均显式清除 `OPENAI_API_KEY`，模型出站目标为 `https://api.deepseek.com`，OpenAI
托管请求与默认 trace exporter 使用计数均为 0。P0 框架与 Provider 边界达到当前交付合同。

2026-07-23 起，`WAJE Standard Pack v1` 接管通用 Agent Runtime 的评测合同，旧 10-case
shape 和 `general-agent-runtime-live-eval.v2` runner 已退出。当前 catalog 含 48 个 scenario：
business 24、runtime 12、security 4、experience 8；30 个 deterministic case 和 8 个浏览器
case 已完整通过。真实 DeepSeek 已通过 direct 两轮连续回答的 release 3/3、能力目录工具调用
1/1、公式分析 publication 1/1。公式分析历时 1067.5 秒，作为 latency observation 保留；
hard correctness 与人工答案质量审核继续分别记录。所有 live 运行均清除 `OPENAI_API_KEY`，
出站 origin 为 `https://api.deepseek.com`。完整合同见
[WAJE Standard Pack v1](./standard-pack-v1.md)。

本文定义 WAJE BI v2 下一阶段的产品与运行时目标。完成实施与验收后，本文将接管
对话入口、连续追问、长任务恢复和客户对话投影的架构权威。现有
[单权威 BI 工作流 ADR](../../adr/2026-07-17-single-authority-agent-workflow.md)
继续约束真实 BI 分析内部的 IntentRevision、PlanRevision、查询、证据、claim、
publication 和 delivery；
[业务参考持续交付与人工审计 ADR](../../adr/2026-07-20-advisory-publication-human-review.md)
继续约束质量核验、人工复核与发布关系。

## 一句话目标

把 WAJE 从“每条用户消息预分类后启动一条分析工作流”升级为“一个持久化 thread
中持续运行的通用 Agent loop”：Python OpenAI Agents SDK 驱动模型与工具循环，大陆
模型提供推理算力；WAJE 确定性控制权限、数据安全、幂等、持久化、证据、恢复和客户
投影。

## 框架定版

| 层 | 正式选型 | 边界 |
|---|---|---|
| 顶层通用 Agent loop | OpenAI Agents SDK（Python） | 负责单轮内的模型调用、function tool loop、handoff、HITL interruption 和流式事件 |
| 模型算力 | 大陆模型 Provider | 当前通过 OpenAI-compatible Chat Completions 接入；不调用 GPT 算力 |
| Agent 运行监督 | WAJE `AgentTurnRuntime` | 负责上下文、SDK RunConfig、持久化 hook、幂等、长工具桥接、恢复和终局提交 |
| BI 分析编排 | 现有 LangGraph 单权威工作流 | 只在 `run_bi_analysis`、`continue_bi_analysis` 等 BI 工具内部运行 |
| 持久化 | WAJE PostgreSQL | 保存 thread、session item、task、checkpoint、artifact、pending action、outbox 和 trace identity |
| 分析查询 | ClickHouse | 执行经过合同、安全和 release/snapshot 校验的分析查询 |
| 前端与 Gateway | Next.js、Vercel AI SDK、TypeScript Gateway | 负责单对话流、鉴权、幂等接入、SSE 和 customer-safe projection，不拥有 Agent 状态 |
| 技术审计 | WAJE Workbench | 保存 SDK、模型、工具、LangGraph、claim、evidence、错误和人工审核 trace |

OpenAI Agents SDK 在本架构中是一项可替换的开源运行库。WAJE 对外合同只依赖
`AgentTurnRuntime`、`AgentToolResult`、`ThreadItem` 和 projection，不向 Gateway、数据层
或客户 UI 暴露 SDK 类型。SDK 升级或替换不能改变 thread、task、artifact、证据和发布
权威。

正式运行链不使用 OpenAI Responses API、OpenAI Conversations、OpenAI 托管工具、
OpenAI Hosted Multi-Agent 或 OpenAI Trace 后端。后续大陆 Provider 若提供经过验收的
Responses-compatible 能力，可以在 `MainlandModelProvider` 内升级传输方式，不能绕过
WAJE 状态与审计合同。

## 背景与已确认问题

2026-07-21 的真实连续追问测试暴露了结构性缺口。

原分析已经给出设备型号维度 0.70、支付方式维度 0.59 的诊断优先级。用户在同一
thread 中追问公式、组成项、权重和归一化方式。系统正确绑定为 `follow_up` 和
`inherit_current`，随后仍然启动了完整分析 run。新 run 将追问误绑定为
`data_quality_or_evidence_review`，最终回答数据覆盖和质量限制，未解释已有得分。

运行证据：

- 原分析 run：`run-3e737248-ce73-45e5-bc0c-7f3565c37df4`；
- 追问 run：`run-ebd6d7dd-aa34-4bd5-a5bc-57314e7752d7`；
- conversation orchestrator 的输入中 `recent_turns` 为空；
- `prior_topic_material_context` 被构造为空对象；
- 下游 intent binder 无法看到原 publication、artifact、得分合同和证据；
- 页面刷新后可以恢复追问及错误回答，说明消息持久化和客户状态恢复在本次测试中有效；
- 原 capability output 实际保存了公式和组成值，失败源于上下文与材料断链。

对应代码：

- `bi_agent/conversation/runtime.py`：从 `thread.turns` 组装 `recent_turns`，并为
  `prior_topic_material_context` 写入空对象；
- `bi_agent/conversation/entry_authority.py`：当前权威校验要求 prior material 为空；
- `bi_agent/conversation/runtime.py::_should_run`：普通 follow-up 进入完整 run；
- `bi_agent/capabilities/candidate_dimension_screen.py`：保存诊断优先级公式和组成项。

这类失败说明现有入口围绕“分析工作流路由”组织，缺少持续任务、artifact 复用和
Agent 原生工具循环。

## 产品第一性原理

### 1. 用户面对 thread 和任务

用户关心业务目标、已经得到的结果、当前进展、需要提供的决定和下一步行动。run、
node、provider、dispatch 和内部 UUID 只服务运行与审计。

一个 thread 可以承载多轮连续任务：

- 提出新的业务问题；
- 解释已有结论；
- 修改时间、基线、范围或口径；
- 下钻或补充分析；
- 质疑已有结论；
- 询问数据、能力和计算机制；
- 暂停、恢复或取消长任务。

### 2. 每轮统一进入 Agent loop

入口不先把消息切成“即时交互”和“完整分析”两条产品链。每轮都由同一个 Agent
根据权威上下文选择下一步动作。

稳定的执行动作只有：

```text
respond
call_tool
ask_user
request_approval
delegate
finish
```

业务目标、引用对象、约束和预期结果保持开放语义，由 typed LLM binding 产生。固定
枚举只控制执行动作、权限、状态机、幂等和安全边界。

### 3. 语言自由与事实权威分离

LLM 可以自由组织解释、重点、比较、洞察和建议。数值、公式、数据范围、证据强度、
限制和工具执行结果来自权威 artifact 与 typed tool result。

本地代码不通过关键词字典解释开放业务语义，不生成本地高价值答案模板，也不使用
无证据 fallback 填补业务结论。

Writer 的业务语义输入来自 accepted `IntentRevision` 与 `PlanRevision` 的类型化投影，
包括目标指标、时间和比较关系、用户指定因子、分析轴、能力路线及用户要求的结论义务。
原始客户文本与固定敏感标识不进入模型 payload。公式类材料同时携带指标合同声明的
层级分组；叶子因子和复合因子的贡献只能在同一分组层级比较，分组贡献由声明式
grouped Shapley 重新计算，不能跨层相加。

这些输入完整性合同用于让正常执行产生完整业务参考。它们不把表达质量转成发布门禁。
表达完整性、解释深度和潜在幻觉发现继续在交付后进入 Workbench 与人工审核，不自动
重试 writer，也不修改首次交付。

### 4. 每个已接受请求都有可见终局

每条用户消息立即形成持久化 user item。需要后台执行时，系统写入已接受和真实进展；
任务最终必须产生以下一种 assistant item：

- 完整业务参考；
- 带明确限制的业务参考；
- 需要用户输入；
- 需要用户批准；
- 可操作的真实失败说明；
- 用户主动取消后的终止说明。

质量核验发现进入后台审计、人工标注和学习链，不阻断安全的业务参考交付。权限、固定
敏感输出、SQL 安全、数据合同、证据来源和持久化完整性继续作为硬边界。

### 5. 历史只能提供上下文

历史 item、event、run 和 clarification 用于回放与检索。当前操作权限只来自最新
`ThreadHead`、活跃 `TaskSnapshot` 和待处理 action。

## 目标系统分层

```text
客户对话 UI
  ↓ customer-safe ThreadProjection
TypeScript Gateway
  ↓ 幂等消息、SSE、鉴权、版本校验
WAJE AgentTurnRuntime
  ├─ AgentContextAssembler
  ├─ PostgresAgentSession → ThreadItemLedger
  ├─ OpenAI Agents SDK Runner
  │    ↓ MainlandModelProvider / Chat Completions
  │  大陆模型服务
  └─ DurableToolBridge → Tool Runtime
       ├─ BI 分析工具 → LangGraph 单权威 BI 工作流
       ├─ artifact / claim 解释工具
       ├─ 数据与能力目录工具
       ├─ 审批与人工输入工具
       └─ 可选子 Agent 工具
Durable Runtime
  ↓ task、checkpoint、tool call、artifact、event、outbox
PostgreSQL / ClickHouse / LLM Provider

同一份持久化运行记录
  ↓ technical TraceProjection
Agent Run Workbench
```

### 层级职责

| 层 | 权威职责 |
|---|---|
| 客户 UI | 展示 thread items、一个主状态、输入控件和 customer-safe artifact |
| Gateway | 身份、线程归属、幂等接入、SSE cursor、客户投影 |
| AgentTurnRuntime | 组装上下文和 SDK RunConfig，将 Runner 事件映射为持久化动作，监督长工具、恢复与终局 |
| Agents SDK Runner | 执行一个应用轮次内的模型、function tool、handoff 和 interruption 循环 |
| MainlandModelProvider | 显式选择大陆模型 endpoint、模型、传输协议和能力，不允许回落到 SDK 默认模型 |
| Tool Runtime | 参数校验、权限、工具分发、结果合同、幂等执行 |
| BI Analysis Runtime / LangGraph | IntentRevision、PlanRevision、查询、capability、证据与 publication |
| Durable Runtime | checkpoint、lease、恢复、tool call、outbox 和状态原子更新 |
| Workbench | 完整 trace、调用、错误、证据、claim、审核和修订 |

## 核心持久化对象

### Thread

用户可见的持续对话容器。Thread 本身不等于某个 run。

### ThreadItem

append-only 的对话与执行记录：

```ts
type ThreadItem =
  | UserMessageItem
  | AssistantMessageItem
  | ProgressItem
  | ToolCallItem
  | ToolResultItem
  | ClarificationItem
  | ApprovalRequestItem
  | ApprovalDecisionItem
  | ArtifactReferenceItem
  | TaskTerminalItem;
```

客户投影只公开 user、assistant、业务进展、可操作澄清、审批、客户安全 artifact 和
业务错误。完整 tool call、provider payload、内部错误和 trace 留在 Workbench。

### ThreadHead

当前 thread 的唯一可操作状态：

```ts
type ThreadHead = {
  threadId: string;
  stateVersion: string;
  activeTaskId: string | null;
  activeTopicRef: string | null;
  pendingActionRef: string | null;
  latestItemSequence: number;
  customerState:
    | "idle"
    | "working"
    | "needs_input"
    | "completed"
    | "completed_with_limits"
    | "failed";
};
```

任意时刻只有一个主客户状态。历史事件不能改变 `ThreadHead`。

### Task 与 RunAttempt

`Task` 表示一个持续目标，`RunAttempt` 表示完成该目标的一次执行尝试。解释已有结果
可以创建 turn，但通常不创建新的 BI analysis task。修改范围或补充证据时，可以在
原 task 下建立 revision 或新的 run attempt。

### Artifact

Artifact 是模型可检索、工具可引用、用户可解释的持久化工作成果，包括：

- BI publication；
- claim 与 evidence bundle；
- 查询和聚合结果；
- 公式、得分和归一化解释；
- 限制、假设和决策；
- 导出的报告或图表；
- 子 Agent 的结构化结果。

Artifact 需要稳定类型、版本、digest、source refs、visibility policy 和 customer-safe
projection。

### Checkpoint

每次模型动作、工具结果、用户中断和审批决定后持久化可恢复 checkpoint。checkpoint
必须包含恢复执行所需的公开模型 item、工具状态、pending actions 和上下文版本；隐藏
chain-of-thought 不进入 WAJE 业务权威。

### PostgresAgentSession

`PostgresAgentSession` 是 Agents SDK `Session` 接口对 `ThreadItemLedger` 的适配视图，
不建立第二套消息历史。SDK 从该 Session 读取 replay-ready 模型 item，并将新增模型、
工具和 interruption item 交给同一 ledger 原子写入。

Session 只承载上下文历史。当前操作权限继续来自 `ThreadHead` 和 pending action。
Provider 侧 `conversation_id`、`previous_response_id` 和服务端会话状态不进入本架构。

## AgentContextSnapshot

每轮模型调用前，由服务端从持久化记录组装：

```ts
type AgentContextSnapshot = {
  threadSummary: VersionedSummary | null;
  recentItems: ThreadItem[];
  activeTask: TaskSnapshot | null;
  acceptedDecisions: DecisionReference[];
  pendingActions: PendingAction[];
  artifactIndex: ArtifactDescriptor[];
  relevantMaterials: MaterialExcerpt[];
  availableTools: ToolDescriptor[];
  permissionScope: PermissionScope;
  contextVersion: string;
};
```

### 上下文组装规则

1. 从 PostgreSQL 读取持久化 thread items，不能依赖进程内 `thread.turns`。
2. 最近消息提供语言连续性。
3. 活跃任务和 accepted decisions 提供目标连续性。
4. artifact index 提供可检索的世界状态。
5. 根据当前消息与 target refs 检索相关材料，避免把全部运行审计塞入模型上下文。
6. 摘要携带覆盖的 item range、source refs 和 digest，只负责压缩，不授予事实权威。
7. compaction 后仍可按 artifact ref 重新读取原始材料。
8. provider reasoning continuation 可以优化模型连续性，不能替代 WAJE 的 thread、task
   和 artifact SSOT。

## Agent loop 合同

### 职责切分

- Agents SDK `Runner` 负责一个应用轮次内重复调用模型、执行短 function tool、处理
  handoff，并在最终输出或 interruption 时停止。
- `AgentTurnRuntime` 负责 Runner 外围的状态权威、持久化 hook、工具幂等、长任务桥接、
  失败恢复和 customer terminal item。
- LangGraph 负责一次真实 BI 分析工具内部的单权威工作流，不接管普通对话追问。

### 每个应用轮次

```text
1. 幂等接收 user item
2. 读取 ThreadHead 并校验 expected state version
3. 组装 AgentContextSnapshot
4. 从 ThreadItemLedger 创建 PostgresAgentSession 视图
5. 使用显式 MainlandModelProvider 和 WAJE tool registry 启动 SDK Runner
6. Runner 产生 function tool call 时，先持久化 call ID 与 idempotency identity
7. 短工具返回后持久化 tool result，Runner 在同一轮继续
8. 长工具通过 DurableToolBridge 持久化 checkpoint 并交给后台 task
9. 长工具完成后，由 AgentTurnRuntime 从 Session 和 checkpoint 恢复 Runner 上下文
10. Runner 最终输出、澄清或审批 interruption 映射为 typed AgentAction
11. 同一事务写入 assistant / pending action / terminal item、ThreadHead 和 outbox
```

SDK Runner 的内存调用栈不承担长任务恢复。任何已经产生外部副作用或需要十分钟级执行
的工具，都必须先进入 DurableToolBridge。worker 重启后依据稳定 tool call ID 恢复；已完成
工具结果直接回放，不重复执行。

### AgentAction

```ts
type AgentAction =
  | {
      type: "respond";
      goal: string;
      targetRefs: string[];
      materialRefs: string[];
    }
  | {
      type: "call_tool";
      toolName: string;
      arguments: unknown;
      purpose: string;
      expectedEvidence: string[];
    }
  | {
      type: "ask_user";
      materialDecision: string;
      options: BusinessOption[];
    }
  | {
      type: "request_approval";
      actionSummary: string;
      sideEffectScope: string;
    }
  | {
      type: "delegate";
      task: string;
      inputRefs: string[];
      expectedOutput: string;
    }
  | {
      type: "finish";
      outcome: "completed" | "completed_with_limits" | "failed" | "cancelled";
      materialRefs: string[];
    };
```

`respond` 表示已有材料足以回答。系统仍需检查引用闭包和客户可见性，但不创建新的
BI analysis run。

### SDK 事件映射

| SDK 行为 | WAJE 持久化动作 |
|---|---|
| 模型最终输出 | `respond`，随后写入 `finish` 终局 |
| function tool call | `call_tool`，调用前保存稳定 call ID |
| `ask_user` 工具调用 | 保存 `ask_user` 与 pending action，序列化可恢复 interruption |
| 工具需要批准 | 保存 `request_approval` 与 SDK run state |
| agent-as-tool 或受控 handoff | 保存 `delegate`，子结果只作为 artifact 返回主 Agent |
| SDK、Provider 或工具终止故障 | 保存真实错误与 `finish`，由客户投影生成可操作说明 |

最终回答采用强类型输出 envelope，至少包含自由组织的 `answerMarkdown`、引用的
`materialRefs` 和 `limitationRefs`。类型合同约束来源闭包，不能将客户表达压成固定答案
模板。

## 领域工具模型

现有 BI 能力收敛为通用 Agent 可调用的工具：

| 工具 | 用途 |
|---|---|
| `run_bi_analysis` | 新问题或需要新增数据的分析 |
| `continue_bi_analysis` | 修改原任务范围、基线、时间或分析深度 |
| `inspect_analysis_artifact` | 读取已有 publication、组成项和限制 |
| `explain_claim` | 解释 claim 的计算、证据、边界和来源 |
| `challenge_claim` | 基于原证据检查质疑并决定是否需要补充分析 |
| `inspect_metric_contract` | 解释指标、公式、粒度和数据覆盖 |
| `list_available_capabilities` | 回答系统可分析的数据和能力 |
| `get_task_status` | 读取长任务当前权威状态 |
| `annotate_analysis` | 后台人工标注、评分和清洗决定 |

`run_bi_analysis` 内部继续使用单权威 BI 工作流。AgentTurnRuntime 不复制 SQL、证据、
claim 或 publication 逻辑。

### 工具结果共同字段

```ts
type AgentToolResult<T> = {
  status: "succeeded" | "limited" | "failed" | "needs_input";
  output: T | null;
  artifactRefs: string[];
  materialRefs: string[];
  limitationRefs: string[];
  retryability: "never" | "same_input" | "replan_required";
  customerSummary: string;
  technicalDetailRef: string | null;
};
```

`technicalDetailRef` 只进入 Workbench 和服务端审计。

## 连续追问处理

### 解释已有答案

```text
用户：这个得分怎么算？
→ 解析“这个得分”对应的 claim / artifact ref
→ inspect_analysis_artifact 或 explain_claim
→ 返回公式、组成项、权重、数值、主体和限制
→ LLM 自由组织客户回答
→ 不启动新 SQL，不创建完整 BI plan
```

若同一上下文存在多个可能引用，且选择会显著改变回答，Agent 使用 `ask_user`。材料
缺失时如实说明缺少字段，不推测计算过程。

### 修改范围或时间

```text
用户：换成按周看呢？
→ 继承当前指标、范围和业务目标
→ 将时间粒度变化绑定为 material revision
→ continue_bi_analysis
→ 新 run attempt 查询所需数据
→ 在同一 thread 返回结果
```

### 询问系统机制

得分公式、指标口径、可用数据、工具能力、证据含义和限制应通过公开版本化合同回答。
隐藏推理、密钥、内部提示词、原始敏感行和安全防护细节继续受客户安全边界保护。

### 质疑结论

Agent 先读取原 claim、证据和限制。已有材料可以回答时直接解释；需要竞争假设或新数据
时调用 `challenge_claim` 或 `continue_bi_analysis`。质疑文本不通过关键词字典判断。

## 诊断优先级得分的目标解释合同

评分 artifact 至少保存：

```ts
type ScoreExplanation = {
  formulaId: string;
  formulaVersion: string;
  subject: {
    type: "dimension" | "member" | "claim";
    dimensionRef: string | null;
    memberRef: string | null;
    representativeMemberRef: string | null;
  };
  components: Array<{
    componentId: string;
    status: "measured" | "not_applicable" | "unavailable";
    rawValue: number | null;
    normalizedValue: number | null;
    weight: number | null;
    contribution: number | null;
    normalization: string;
    materialRefs: string[];
  }>;
  finalScore: number | null;
  rankingScope: string;
  comparisonAllowed: boolean;
  limitationRefs: string[];
};
```

当前实现中的诊断优先级属于维度级排序。TECNO AC8、OPAY 等名称是代表性成员，
客户文案需要写成“设备型号维度优先级 0.70，代表性变化切面为 TECNO AC8”。

没有可信 `global_primary_factor` 时，主因子对齐项标记为 `not_applicable` 或
`unavailable`，不能默认满分。若该组成项参与评分，必须传入已验证主因子和实际对齐
材料。

## 长任务、断网与恢复

1. 用户连接只负责提交和观察，后台 task 独立生存。
2. 每个 tool call 在执行前写入稳定 call ID 和 idempotency key。
3. 每个模型动作、工具结果和中断后写入 checkpoint。
4. worker 使用 lease、heartbeat 和 recovery claim 防止重复所有权。
5. SSE 使用 `stateVersion + eventCursor`；重连先取权威 snapshot，再补增量 item。
6. 浏览器刷新、关闭、断网和多标签页不改变任务执行状态。
7. 同一 operation 重试返回原接受结果，不能重复创建 user item、tool call 或 task。
8. 任务终态通过 outbox 幂等写入 assistant item。

## Human-in-the-loop

### 前链路澄清

只在选择会显著改变结论、基线、范围、证据强度、安全边界或成本时打开
`ask_user`。历史澄清 item 只用于回放；最新 pending action 才能渲染控件。

### 动作审批

外部写入、不可逆动作、权限提升和显著成本需要 `request_approval`。审批状态保存到
checkpoint，并按 action ID 恢复。

### 后链路人工审计

业务参考先按固定客户安全边界交付。核验发现、人工评分、标注、清洗和学习保存在
Workbench 审计链。修订形成新 revision，首次交付保持不可变。

## 多 Agent

多 Agent 是 accepted Plan 之后、唯一 narrative 之前的受控调查策略。父 Agent 始终拥有
IntentRevision、PlanRevision、Evidence、Claim、ThreadHead、Publication、Delivery 和客户状态
权威。子 Agent 只生成带 source allowlist 的候选调查 artifact。

适合委派：

- accepted Plan 中相互独立的机制轴；
- 结构集中点和抵消因素复核；
- 竞争解释、反向证据和交叉信号；
- 基于现有材料的行动优先级整理。

简单追问、单个计算解释和共享上下文密集的同一推理链由主 Agent 直接处理。子 Agent
返回结构化 artifact，不直接修改 ThreadHead 或客户对话。

父级 operation 绑定 owner、thread、run、IntentRevision、PlanRevision、AuthorityContext、
sealed AuthorityBundle、父 transition 和完整 `NarrativeMaterialProjection` digest。最多三个
独立 child dispatch 通过 deterministic admission 进入执行；admission 校验 accepted axis、
axis 不重叠、source allowlist 和读取预算。子任务没有工具入口，无法发起 BI query 或写入
Evidence、Claim、Publication、Delivery。

operation 和 dispatch 保存在 PostgreSQL。dispatch 记录稳定 child identity、input digest、
idempotency key、lease epoch、accepted Provider attempt、候选 artifact 和 typed failure。
过期 lease 可由其他 worker 领取；已经接受的 attempt 和 artifact 只重放，不重复生成逻辑
结果。单个或全部子任务失败只形成局部 settlement，父级仍可用 sealed authority 材料生成唯一
客户答案。

Provider 规划目录使用列式 source catalog。子读取投影对 evidence facts 使用无损列式编码；
任何单任务超过预算的 source 组合在 admission 阶段拒绝。完整权威对象、原始 Provider
payload 和技术错误只进入 Workbench。客户页面只显示最终业务参考和既有客户安全投影。

子任务 artifact 保留完整审计记录，进入唯一 narrative 的材料只包含 typed narrative delta：
调查轴、finding 类型、推荐落位、增量文本和 source refs。父级不再把子任务标题、摘要、完整
限制清单或小报告正文再次交给 writer。相同 finding 文本与来源闭包按 canonical digest 精确
去重；语义判断不使用本地关键词或模糊相似度。writer 对每条 delta 最多使用一次，可以在
sealed authority 已覆盖同一决策点时省略，所有事实仍需 accepted handle，质量评价继续留在
交付后的 advisory 链。

## 客户投影与前端

主页面继续采用 Codex 风格的单对话流：

- user item；
- assistant item；
- 真实持久化进展；
- 一次性澄清或审批；
- 最终业务参考；
- 可展开的证据和限制；
- 客户可读错误与恢复动作。

主页面不展示工作流画布、node、run、dispatch、UUID、内部组件名、provider、snake_case
错误或核验分数。进展以对话内简洁状态行呈现，不使用阶段卡片堆叠。

客户端可以在内存中保存 thread handle、state version、event cursor 和 operation ID，
这些 transport handles 不进入 DOM 或用户复制内容。

Workbench 展示完整 trace、run、node、tool call、claim、evidence、ref、digest、provider、
错误和人工审核。

## 错误语义

工具失败先作为 typed tool result 返回 Agent。Agent 可以重试允许重试的调用、调整计划、
选择其他已授权工具，或给出真实失败说明。

以下行为禁止：

- 将未知内部状态原样投影给客户；
- 使用本地模板伪造高价值答案；
- 将合同违规吞掉并显示成功；
- 根据一次网络冲突直接声称动作已完成；
- 将质量审计发现伪装成数据缺失或系统失败；
- 将无关能力失败升级为整个任务失败。

只有权威状态证明动作已经接受或完成时，客户错误才可以解释为“已处理”。未知内部状态
生成可观测的 projection contract violation，并在客户侧显示稳定、可操作的通用错误。

## 质量、证据与发布

- 所有已进入分析的用户请求最终交付业务参考或真实失败终局。
- LLM 原始业务表达在固定客户安全投影内交付。
- evidence 和 claim 约束事实强度，不把答案压缩成字段复述。
- verifier、洞察评分和潜在幻觉发现作为后台审计信号。
- 人工复核负责标注、清洗、修订和学习样本治理。
- 单次 eval 失败不自动升级为运行时规则。
- 权限、固定敏感输出、SQL 安全、数据合同、证据 provenance 和持久化完整性继续严格。

## 技术选型边界

### 已接受选型

- Python Agent Core 使用 `openai-agents` 作为顶层通用 Agent loop 框架；
- SDK 只在 WAJE worker 进程内运行，不依赖 OpenAI 托管 Agent runtime；
- WAJE 使用自定义 `PostgresAgentSession`，底层直接复用 `ThreadItemLedger`；
- 模型必须由显式 `MainlandModelProvider` 提供，首个适配器使用 OpenAI-compatible
  Chat Completions；
- 当前 DeepSeek 配置继续作为首个真实 Provider 验收目标，Provider 合同允许后续接入
  其他满足能力门槛的大陆模型；
- 现有 LangGraph 工作流保留在 BI function tools 内部；
- Vercel AI SDK 只服务 Next.js 对话流和 transport，不参与 Agent 规划、状态判断或
  业务权威；
- PostgreSQL、ClickHouse、Gateway、customer projection 和 Workbench 的现有责任边界
  保持不变。

### 大陆模型 Provider 合同

`MainlandModelProvider` 是 General Agent 与 BI LangGraph typed LLM 节点的统一配置和
传输工厂。Agents SDK 路径由它实现 `ModelProvider` 并返回使用显式
`AsyncOpenAI(base_url=...)` 的 `OpenAIChatCompletionsModel`；BI 节点通过同一个
`MainlandProviderConfig` 创建结构化 Chat Completions client。生产代码必须显式传入
provider、HTTPS base URL、API key、model 和 model settings，不能读取 SDK 默认模型作为
回退。两个 HTTP client 都关闭 redirect，并在每次请求前校验目标与配置 origin 完全一致。

首个生产合同要求：

| 能力 | 要求 |
|---|---|
| 文本生成 | 必须支持流式与非流式回答，并保留完整终局 |
| Function calling | 必须支持稳定 tool call ID、强类型参数和多轮工具结果回传 |
| 结构化输出 | 必须通过 WAJE schema 验证；Provider 原生能力不足时判定能力不兼容 |
| 长上下文 | 必须公开最大上下文和输出限制，由 ContextAssembler 主动预算 |
| 思考模式 | 作为 Provider capability 使用，不把 reasoning body 写入客户内容或业务权威 |
| 重试 | timeout、retry 和 provider 熔断集中在 Provider 层 |
| 流式工具调用 | 增量 tool call 不稳定的 Provider 启用完整缓冲后再交给 Runner |
| 错误 | 映射为 WAJE typed provider error，原始 payload 只进入 Workbench |

启动时执行 Provider capability probe。缺失必需能力时服务进入可观测的不可用状态，
不能静默切换 GPT、降低工具合同或改用本地高价值答案模板。

### 禁止的运行时依赖

大陆模型部署环境中：

- 不配置 OpenAI 默认 model/provider；
- 不使用 OpenAI Responses API、Responses WebSocket 和 Conversations API；
- 不使用 `conversation_id`、`previous_response_id` 或 Provider 托管对话作为续接权威；
- 不使用 OpenAI Web Search、File Search、Code Interpreter 等托管工具；
- 不使用 OpenAI Hosted Multi-Agent；
- 不向 OpenAI Trace 后端发送 run、prompt、tool 或业务数据；
- 不允许 SDK 在 Provider 配置错误时回落到 `api.openai.com`。

部署验收在没有 `OPENAI_API_KEY` 的环境运行，并对出站目标做断言。运行时只允许访问
显式配置的大陆模型 endpoint、WAJE 服务和经过授权的数据源。

### Tracing 与审计

Agents SDK 默认 Trace exporter 在启动时关闭或替换为 WAJE 自定义 trace processor。
SDK run、model turn、tool call、handoff、interruption 和异常写入 `AgentTraceProjection`，
再由 Workbench 读取。客户页只接收业务进展和 customer-safe error。

### 版本与替换边界

`openai-agents` 使用锁定版本。SDK 类型只存在于 Python Agent Core 的 adapter 层：

```text
Gateway / Customer Projection
  ↓ WAJE contracts
AgentTurnRuntime
  ↓ SDK adapter boundary
OpenAI Agents SDK Runner
  ↓ ModelProvider boundary
大陆模型服务
```

SDK 升级必须通过 Provider、Session、工具幂等、interruption 恢复、长任务和客户投影
回归测试。SDK 行为变化不能直接修改持久化 schema 或客户 API。

参考：

- [OpenAI Agents SDK overview](https://developers.openai.com/api/docs/guides/agents)
- [Models and providers](https://openai.github.io/openai-agents-python/models/)
- [Running agents and conversation state](https://openai.github.io/openai-agents-python/running_agents/)
- [Sessions](https://openai.github.io/openai-agents-python/sessions/)
- [Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)
- [Tracing](https://openai.github.io/openai-agents-python/tracing/)
- [OpenAI Agents SDK Python MIT License](https://github.com/openai/openai-agents-python/blob/main/LICENSE)
- [Claude Code CLI continuation and resume](https://docs.anthropic.com/en/docs/claude-code/cli-usage)

## 当前代码的目标收敛

### 删除或重写

- `INTERACTION_INTENTS` 与 `_should_run()` 双轨入口；
- 强制为空的 `prior_topic_material_context`；
- follow-up 自动启动完整分析的路由；
- 只返回分类摘要的 capability-question 即时路径；
- 从进程内 `thread.turns` 恢复持久化对话的假设；
- 顶层消息直接进入 LangGraph 分析路由的入口；
- SDK 默认 OpenAI Provider、Responses continuation 和 Trace exporter；
- 将代表成员名称和维度级分数绑定成同一评分主体的表达；
- 前端从历史 event 推导当前操作权限的逻辑；
- 与新 Agent loop 合同冲突的旧源码断言和 fixture。

### 保留并下沉

- IntentRevision、DecisionLedger 和 PlanRevision；
- SQL、安全、release、snapshot 和数据合同；
- capability execution；
- EvidenceLedger、ClaimGraph 和 AuthorityBundle；
- narrative、publication、delivery outbox；
- durable call journal、lease、heartbeat 和 recovery；
- customer-safe projection 与 Workbench 审计边界。

这些能力进入 `run_bi_analysis`、`continue_bi_analysis`、`explain_claim` 等工具内部。

### 新建或收敛

- `AgentTurnRuntime`；
- `ThreadItemLedger`；
- `ThreadHead`；
- `AgentContextAssembler`；
- `AgentAction` union；
- `AgentCheckpoint`；
- `ArtifactRegistry`；
- `PostgresAgentSession`；
- `CustomerThreadProjection`；
- `AgentTraceProjection`；
- `MainlandModelProvider` 与 Provider capability probe；
- Agents SDK adapter、WAJE trace processor 和 `DurableToolBridge`；
- `inspect_analysis_artifact`；
- `explain_claim`；
- `list_available_capabilities`。

## 实施顺序

### P0：框架与 Provider 落地（已完成）

1. 锁定 `openai-agents` 版本，建立 SDK adapter boundary。
2. 实现 `MainlandModelProvider`，首个真实适配器连接当前 DeepSeek endpoint。
3. 强制 Chat Completions 路径，关闭 Responses、托管状态和 OpenAI Trace exporter。
4. 实现 WAJE trace processor 和出站目标断言。
5. 完成文本、function calling、结构化输出、流式 tool call 和错误映射 capability probe。
6. 在无 `OPENAI_API_KEY` 环境通过 SDK Runner 真实调用验收。

### P0：连续对话与状态权威（已完成）

1. 建立 `ThreadItemLedger` 和原子 `ThreadHead`。
2. 从持久化 conversation messages 恢复 recent items。
3. 建立 `AgentContextAssembler` 和 artifact index。
4. 实现统一 `AgentTurnRuntime`，以 Agents SDK Runner 执行每个应用轮次。
5. 删除 conversation entry 双轨路由和空 prior-material 合同。
6. 实现普通 assistant response 的幂等持久化与 SSE 投影。

### P0：已有材料解释（已完成）

1. 将 publication、claim、evidence 和限制注册为可检索 artifact。
2. 实现 `inspect_analysis_artifact` 和 `explain_claim`。
3. 为评分、公式分解和窗口比较保存客户安全解释合同。
4. 确保已有材料足够时不创建新的 BI run。

### P1：BI 分析工具化（提交边界已完成）

1. 将当前完整分析入口封装为 `run_bi_analysis`。
2. 将 material revision 封装为 `continue_bi_analysis`。
3. 统一 tool result、retryability、artifact refs 和 limitation refs。

### P1：长任务恢复（已完成）

1. 已由 ThreadItemLedger 保存模型动作与工具结果，并在长工具边界保存
   SDK-neutral AgentCheckpoint。
2. 已复用现有 worker lease、heartbeat 和 recovery；publication completion loader、
   `resume_ready_task` 与 `agent_task_resume_outbox` 已接入生产 recovery worker。
3. 已将澄清和审批实现为可恢复 typed Agent interruption。
4. 正常消息入口已切换到 AgentTurnRuntime，Gateway 不再为每条消息预建 BI run。
5. thread SSE 已使用 snapshot、ThreadHead state version、item sequence 和独立 event cursor。
6. 刷新、断网、关闭页面、stale cursor 和多标签页真实浏览器测试已通过。

### P2：上下文压缩与多 Agent（已完成）

1. append-only `VersionedThreadSummary` 保存覆盖区间、源 item digest、前序摘要和
   artifact authority refs；摘要只压缩上下文。
2. `AgentContextAssembler` 在 item 数量或 Provider 输入预算超限前显式触发 compaction，
   `PostgresAgentSession` 只回放摘要覆盖区间之后的原始 ledger items。
3. typed 大陆模型 selector 从候选工具目录选择最小工具集，选择结果以 digest 和
   `tool_selection` item 持久化；恢复严格回放原选择。
4. `delegate_independent_investigations` 最多并行执行三个只读调查，只接收显式
   customer-safe artifacts。子结果通过引用闭包验证后保存为结构化 artifact，不能修改
   ThreadHead、客户对话或 BI 权威。

### P3：部署与真实环境验收（目标环境验收已完成）

1. 修正 v11→v12 in-place upgrade：只允许新增 `agent_thread_summaries` 和
   `agent_generated_artifacts`，保留全部既有业务表行数，新表必须为空。
2. repository gate 校验 SDK 依赖锁、schema migration digest 和 release manifest。
3. PostgreSQL gate 在 `REPEATABLE READ READ ONLY` 事务中校验 v12 migration、必需表、
   append-only trigger 和 `tool_selection` item 合同。
4. live Provider gate 清除 `OPENAI_API_KEY` 后运行十三项 capability check，并真实执行 P2
   summary、动态工具选择、选择重放和受控子任务 artifact 闭包。
5. live trace 只接受 `waje-agent-trace.v1`，验证 trace 起止事件且拒绝
   `api.openai.com` 出站痕迹。
6. 输出 `general-agent-deployment.v1` typed JSON report；任一 gate 失败时退出码非零。
7. v13 在已发布客户 payload 上增加持久化 digest，数据库门禁校验该列，使恢复与读取可以
   验证客户投影没有发生持久化漂移。

### P4：全因子调查与受控编排（已完成）

1. 运行时 registry 以十个业务因素域声明调查覆盖：支付公式链、拉新注册首充、充值档位与
   用户价值、支付渠道与方式、增长运营、玩法与投注、日历发薪、产品运营事件、外部事件、
   数据质量与证据。
2. `FactorCoveragePlan` 从 accepted `PlanRevision`、版本化 registry 和 capability DAG 编译；
   每个因素域均结算为 `analyzed`、`screened_no_signal`、`unavailable_data`、
   `missing_contract`、`unsupported_grain`、`not_applicable`、`deferred_by_budget` 或
   `failed`，能力缺失和数据缺口显式进入限制，不能推断为“没有影响”。
3. 拉新漏斗按分析窗口重算新增、注册、首充和新增首日付费的逐级转化率；充值档位同时结算
   金额、人数、订单、频次、单笔金额与 mix 偏移，并且只允许在同一 reconciliation group
   内对账。
4. 内部活动、外部事件和业务指标通过时间轴与 reviewed join contract 形成候选关联；覆盖状态
   只描述证据可用性，不能直接用于影响排序，也不能升级为因果结论。
5. 调查分支是只读、引用闭包受控的执行单元。单分支失败只降低相关因素域；精确恢复重放
   persisted plan、outcome、evidence 和 branch refs，不重复查询或模型判断。
6. `InvestigationSynthesis` 在 claim settlement 后绑定 verified claim refs，再把业务化覆盖摘要提供
   给 narrative writer。客户投影只包含已发布回答和安全材料；完整因素拓扑、模型、工具、
   LangGraph 与错误 trace 只进入 Workbench。
7. P4 标准包覆盖真实全因素调查、漏斗、充值档位、事件无匹配、分支失败隔离、恢复回放、
   客户安全投影和大陆 Provider 出站边界。回答质量继续采用交付后的人工 advisory review，
   不增加自动拦截、自动重写或撤回。
8. 推荐仍受 claim publication ceiling 约束。可选建议越界时，运行时在 Workbench 审计中记录
   policy rejection 并丢弃该建议；已经闭合的事实、证据和 claim 继续进入 narrative 与发布，
   不把建议拒绝升级成整条分析失败。

P4 的执行与验收细节见
[P4 full-factor investigation and orchestration plan](../../superpowers/plans/2026-07-23-p4-full-factor-investigation-orchestration.md)
和 [P4 full-factor delivery report](../../reviews/2026-07-23-p4-full-factor-delivery.md)。

### P5：数据覆盖与执行性能（已完成）

1. 支付、market dashboard、玩法和关联数据按当前 release/snapshot 权威解析，完整日截止
   2026-06-02 的问题使用同一冻结边界。
2. first-payment 去重与 Lagos/GMT/北京时间基准修复在数据准备层完成，修复前的注册到首充耗时
   结论不进入 publication。
3. query、capability 与 material projection 使用显式行数、字节数和时间预算；大规模 public facts
   采用无损列式传输，不把 raw rows 交给模型。
4. 数据覆盖和性能缺口进入 Workbench 与局部 limitation，不改变单权威链。

### P6：生产链路验收（已完成）

1. 标准包统一调度 pytest、真实 Agent Runtime 与 Playwright，覆盖业务、runtime、安全和浏览器
   体验。
2. 真实支付终态数据只表达最终成功或截至快照未成功；没有失败尝试明细时，不发布失败原因、
   失败阶段、重试或处理耗时结论。
3. 无 `OPENAI_API_KEY` 的验收只允许 DeepSeek Chat Completions 出站，OpenAI hosted request
   count 必须为零。
4. 回答质量由人工 advisory review 评价，不改变硬合同和首次 publication。

### P7：回答完整性审计与持续交付（已实现）

1. typed requested factors 编译为独立 user-required obligations，并保留对应 axis 和
   contract-declared dimensions。
2. claim coverage 的数据缺口继续走有界 `PlanPatch` 补查；无可用 route 时形成局部 explicit
   boundary，可靠结论继续发布。
3. `AnswerCompletenessAssessment` 检查 required blocks 中的 claim handles、显式请求 factor 的
   required public fact handles 和 limitation handles，结果只进入 Workbench 与人工复核。
4. narrative writer 每个首次 publication 只生成一个正文版本。表达遗漏、洞察深度、段落组织
   和行动性不触发自动补写、局部改写、客户 warning、状态降级或撤回。
5. payment final-outcome capability 对账全样本 totals，并为 payment method/channel 生成同成员
   跨窗口的代表性终态 summary。typed interpretation contract 禁止把终态变化扩写成效率、
   失败环节、延迟、重试、事故或因果机制。
6. 逐块质量核验不创建 publication 依赖或 `pending` 发布身份。客户 publication 完成后，独立
   审计调用才可生成 `completed` 或 `unavailable` 的 `NarrativeQualityAuditResult`，并引用已完成
   的 customer publication。该调用不在首答关键路径，主观发现只进入 Workbench 和人工审核。
7. 已发布材料追问使用 typed 工具恢复能力：客户安全只读工具成功持久化后，最终模型失败可以
   直接交付工具摘要并标记 `completed_with_limits`；普通工具、挂起工具、失败结果、来源闭包或
   审计持久化失败不进入恢复路径。
8. 动态工具选择的模型步骤失败时，若线程已有客户安全 `bi_publication`，runtime 保全最近一次
   完整发布并标记 `completed_with_limits`，同时明确本轮新增解释未完成。该路径不生成新业务
   判断；无 publication、selection 合同错误及来源闭包错误继续 fail closed。

P7 的执行与验收细节见
[P7 answer-completeness repair plan](../../superpowers/plans/2026-07-23-p7-answer-completeness-repair.md)
和 [P7 answer-completeness repair report](../../reviews/2026-07-23-p7-answer-completeness-repair.md)。

### P8：完整首答性能收敛（验收完成）

1. 完整首答性能合同保持 480 秒，已发布材料追问保持 20 秒；超限进入 WAJE 性能审计，线上
   已形成的安全 publication 继续交付。
2. plan proposal 使用 critical 模型档位并关闭额外 thinking，确定性 compiler 继续验证 accepted
   obligation、capability route、数据合同和安全边界。
3. candidate claim、semantic verifier、recommendation、narrative writer 均使用显式 purpose
   profile；质量 block verifier 从客户交付关键路径移出。
4. narrative 与 semantic provider payload 使用无损列式材料和短 authority alias，SDK、原始
   Provider payload 与技术错误只进入 Workbench。
5. P8 标准包同时验证首答、两轮基于已发布 artifact 的追问、无 `OPENAI_API_KEY`、大陆
   Provider 唯一出站和质量审计不影响首次 publication。
6. 动态动作绑定升级为 `agent-turn-action-binding.v2`，同时持久化已选只读工具及经 Pydantic
   合同验证的规范参数。对 `prebinding_policy=read_only` 的工具，Agents SDK 先执行这次已绑定
   调用，随后只向 DeepSeek 发起一次不携带工具 schema 的强类型合成请求；普通和多轮工具 loop
   继续使用原 SDK Runner 路径。
7. intent 提示合同明确区分独立业务目标与组成、诊断、健康度指标。模型按 goal catalog 绑定
   `target_metric_refs`，其余用户显式要求的指标进入 `requested_factor_refs`；运行时不使用
   关键词表或本地语义改写。
8. narrative 引用装配由 accepted authority 决定：已知 fact 绑定到多个合法 claim 时，按
   authority 顺序确定 owner 并保留模型原文；未知 claim/fact 继续作为 provenance 合同错误。
   完整度、深度、表达和行动性只进入交付后的 advisory review，不触发删段、补写、模型重试、
   状态降级或 publication veto。
9. 最终真实 DeepSeek 验收中，完整首答为 318.835 秒，两轮 artifact 追问为 11.175 秒和
   9.518 秒。首答保留 21 个 accepted capability task、23 条 evidence、22 个 verified claim、
   7 个 narrative block、claim verifier、settlement、publication 与 delivery 闭包。两轮追问
   各执行一个只读 artifact 工具和一次真实 generation，没有重跑 BI。
10. 首答的 6 次模型调用全部一次成功，Provider retry 为 0；WAJE performance profile 中
    intent、plan、evidence、coverage、claim authority、narrative 和 delivery 均满足各自预算。
    验收环境没有 `OPENAI_API_KEY`，唯一模型出站为 `https://api.deepseek.com`，
    OpenAI hosted request count 为 0。

2026-07-22 已在仓库 `.env` 指向的目标环境完成快照一致备份、v9→v12 原位升级、数据库只读
审计、真实 DeepSeek gate、运行进程验证和完整回归；随后完成 v12→v13 以及 v13→v14 原位
升级。v14→v15 删除 publication 对质量核验的运行时依赖；v15 的质量审计结果只允许
`completed` 或 `unavailable`，并引用已经完成的 customer publication，不重写业务记录。
后续每个部署目标都要重新生成自己的
`general-agent-deployment.v1` 报告；缺少目标环境时不会伪造通过状态。

2026-07-24 P9 先以 `single-authority-workflow.v16` 新增受控调查 operation 与 dispatch
生命周期，再以 `single-authority-workflow.v17` 修复 advisory-quality 表的物理 schema
验证和旧开发库缺列回填。Workbench 在同一个 `REPEATABLE READ READ ONLY` 快照中读取父
transition、child run、lease、accepted attempt 和 artifact；客户 API 不读取这些内部表。P9
保持 DeepSeek Chat Completions 唯一模型出口，OpenAI hosted runtime、trace 和 eval 继续关闭。

## 验收场景

### 连续追问

- “这个得分怎么算？”读取原 score artifact，返回精确公式和组成项，不产生 SQL。
- “为什么 TECNO AC8 高于 OPAY？”正确说明评分主体是维度，并比较实际组成值。
- “为什么说方向证据有限？”解释原 rolling-window evidence，不重新查询。
- “刚才的结论我不认同。”读取原 claim 后回答或只补充必要分析。
- “你能查哪些数据？”从 capability 和 dataset catalog 组织业务回答。

### 任务延续

- “换成按周看。”继承指标和范围，创建时间 revision 并查询新窗口。
- “改成和上个月同期比。”保留目标，修改 baseline 后执行。
- “继续看设备型号下面的变化。”只扩展所需下钻。
- 指代有多个合理目标时打开一次业务澄清。

### 状态与幂等

- 同一 user operation 只写入一个 user item。
- 同一 tool call 网络结果不确定时使用原 idempotency key 恢复。
- 刷新后恢复真实 assistant、progress、pending action 和 terminal item。
- 历史澄清不能重新生成可点击控件。
- 任意时刻只有一个主客户状态。
- 多标签页不能重复启动 task 或提交决定。

### 错误与审计

- 工具真实失败不会显示成功，也不会生成本地高价值答案。
- 无关工具失败只影响相关 claim 或任务分支。
- 客户 DOM 不出现 UUID、内部组件名、provider 和 snake_case 错误。
- Workbench 可以定位完整模型、tool call、run、claim、evidence 和技术故障。
- 后台核验发现不阻断业务参考交付。

### 框架与 Provider

- 仅配置 WAJE 大陆模型凭据时，Agents SDK Runner 可以完成 direct response 和多轮
  function tool loop。
- 环境不存在 `OPENAI_API_KEY` 时运行成功，且没有请求 `api.openai.com`。
- Provider 配置缺失或能力 probe 失败时明确失败，不回落到 SDK 默认模型。
- Chat Completions 不支持的 Responses-only 字段在开发期触发严格合同错误。
- SDK 默认 Trace exporter 已替换，完整 trace 只进入 WAJE Workbench。
- PostgresAgentSession 与 ThreadItemLedger 共享一份历史；刷新和 worker 重启后不会重复
  注入模型消息。
- 长工具完成后从稳定 tool call ID 和 checkpoint 恢复，SDK Runner 内存栈丢失不会重复
  查询或丢失最终回答。

### 长任务

- 页面关闭后后台继续运行。
- 断线重连先恢复 snapshot，再继续增量。
- worker 重启后从 checkpoint 恢复，已完成工具不重复执行。
- 长任务结束后最终 assistant item 可以在桌面和移动端恢复。

## 完成定义

满足以下条件后，通用 Agent Runtime 可以接管正常用户入口：

1. 所有正常消息统一进入 Agent loop；
2. direct response、tool use、澄清、审批、委派和终局使用同一 item/state 合同；
3. 已有材料追问可以稳定回答且不会误开完整分析；
4. 新分析继续使用原有 BI 权威、数据与证据边界；
5. thread、task、checkpoint 和 artifact 在进程重启后可恢复；
6. 主页面只消费 customer-safe ThreadProjection；
7. Workbench 保留完整技术 trace；
8. 连续追问、长任务、恢复、幂等、安全和发布验收全部通过；
9. 大陆模型 Provider 在无 OpenAI 凭据环境通过 direct response、tool loop、流式、
   interruption 和恢复验收；
10. 与目标合同冲突的旧入口、测试和双轨逻辑已经删除。
