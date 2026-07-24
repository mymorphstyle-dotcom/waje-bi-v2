# P8 完整首答性能验收

日期：2026-07-24
状态：真实链硬验收通过；回答质量进入人工 advisory review

## 交付结论

P8 将支付终态完整首答从 P7 验收的 1240.792 秒收敛到 318.835 秒，降幅约
74.3%，达到现行 480 秒合同。两轮基于已发布材料的追问分别为 11.175 秒和
9.518 秒，均达到 20 秒合同。

性能收敛没有减少 accepted Plan 覆盖。最终 run 保留：

- 1 个 `IntentRevision` 和 1 个 `PlanRevision`；
- 13 条 claim obligation、13 个 analysis axis、21 个 capability task；
- 21 个 capability outcome，其中 14 个 `succeeded`、7 个 `unavailable`；
- 23 条 evidence ledger entry；
- 22 个 proposed claim、22 个 verified claim、22 个 accepted verifier decision；
- 1 个 sealed `AuthorityBundle`、7 个 narrative block；
- 1 个 publication、1 个 published delivery attempt、1 个 customer publication。
- 首答交付链上的 `narrative_quality_audit_results` 为 0；人工 advisory review 独立记录，
  没有进入 publication 或 delivery 依赖。

## 当前运行链

```text
Gateway / GeneralAgentTurnCommand
→ WAJE AgentTurnRuntime
→ OpenAI Agents SDK Runner
→ run_bi_analysis
→ existing single-authority LangGraph workflow
→ accepted Plan / capability / evidence / claim settlement
→ one narrative writer call
→ publication / outbox / customer-safe projection
```

Agents SDK 只运行单个应用轮次内的模型—工具循环。线程、任务、恢复、publication
和 delivery 身份继续由 PostgreSQL 与 WAJE runtime 管理。

所有模型请求经 `MainlandModelProvider` 发往显式配置的 DeepSeek
OpenAI-compatible Chat Completions endpoint。最终报告记录：

- Provider：`deepseek`；
- 模型：`deepseek-v4-flash` / `deepseek-v4-pro`；
- 唯一模型出站：`https://api.deepseek.com`；
- `OPENAI_API_KEY`：不存在；
- OpenAI hosted request count：0；
- OpenAI 默认 trace exporter：未启用，trace 只进入 WAJE 审计。

## 通用性能修复

### 1. Provider 投影压缩

Intent、semantic authority 和 narrative 只接收完成业务判断所需的 typed
projection。大型 public facts 使用无损列式传输；claim、fact、material、limitation
等引用在 Provider wire 上使用可逆短 alias。完整权威对象和原始 Provider payload 留在
Workbench。

### 2. 高价值节点调用画像

Plan proposal、semantic verifier、recommendation 和 narrative 使用显式 purpose
profile。已验证证据的合同检查与叙事渲染关闭额外 thinking；开放意图绑定保留 thinking。
timeout、retry 和 circuit breaker 仍只由 Provider 层管理。

### 3. 质量审计移出客户关键路径

首次 publication 只生成一个 narrative。完整度、深度、可读性、行动性和潜在幻觉风险
在 customer publication 完成后进入独立人工审计。它们不触发 writer retry、自动补写、
删段、客户 warning、状态降级、撤回或 publication veto。

### 4. Authority 引用确定性装配

模型选中已知 fact、但返回的 claim owner 不属于该 material 时，runtime 只使用
accepted authority 中的合法 owner 集合，按 authority 顺序确定引用并保留模型原文。
重复 claim-fact pair 同样由 runtime 去重。这些操作只装配 opaque metadata，不读取或改写
开放业务语义。

未知 claim/fact、伪造 alias 和无法闭合的 provenance 仍形成明确、非重试的 typed
Provider output error。一次确定性输出错误不会用相同输入重复请求模型。

### 5. 已发布材料追问

`agent-turn-action-binding.v2` 保存精确工具和规范参数。对声明
`prebinding_policy=read_only` 的 artifact 工具，SDK 执行一次已绑定读取，再向 DeepSeek
发起一次不携带工具 schema 的强类型合成请求。普通 function loop 和真正多轮工具任务仍走
SDK Runner。

### 6. 后台维护

线程摘要和质量审计退出客户轮次。恢复、摘要刷新和人工审计由持久化状态驱动，不增加首答
模型调用。

## 真实 DeepSeek 验收

- thread：
  `thread-eval-p8_first_answer_performance_live-202607231944598260ac-r1`
- run：`run-034c3737d56bdcc237ca6e68`
- report：`evals/general_agent_runtime/results/p8-final-live-r8.json`
- performance profile：
  `analysis-performance-profile:sha256:a2c3390f68e0dbceb2ee736edbd4d537883cd7ffd2d43536330ccd549222a86b`

| 阶段 | 耗时 | 预算结果 |
|---|---:|---|
| intent | 23.782 秒 | target met |
| plan | 25.362 秒 | target met |
| evidence | 100.484 秒 | target met |
| coverage | 2.242 秒 | target met |
| claim authority | 73.768 秒 | target met |
| narrative | 77.706 秒 | target met |
| delivery | 6.203 秒 | target met |
| 首答端到端 | 318.835 秒 | 480 秒合同通过 |

首答 6 次模型调用全部一次成功，Provider attempt 为 6、retry 为 0。Provider
总耗时 173.045 秒，prompt tokens 为 172,795，completion tokens 为 16,051。
其中 narrative writer 为一次调用，耗时 54.737 秒，prompt tokens 为 36,338，
completion tokens 为 5,364。

首答生成 2670 个字符、25 个段落，终局为
`completed_with_limits / analysis_publication`。两轮追问各执行一次
`inspect_analysis_artifact`：

| 轮次 | 耗时 | 工具次数 | 终局 |
|---|---:|---:|---|
| 已发布聚合事实追问 | 11.175 秒 | 1 | `completed_with_limits / tool_response` |
| 支付过程证据边界挑战 | 9.518 秒 | 1 | `completed_with_limits / tool_response` |

两轮追问均复用同一 customer payload，没有调用 `run_bi_analysis` 或
`continue_bi_analysis`，publication integrity 与 authority refs 闭合。

## 回答质量人工记录

本轮回答完整覆盖付费金额、公式分解、终态订单、成功订单、截至快照未支付订单、成功率、
支付方式、渠道、维度诊断、跨源关联和数据限制。

人工复核仍需记录一处证据强度风险：回答把终态成功率提升描述为“支付转化效率改善”。
当前材料能支持最终状态和成功率变化，缺少失败原因、失败阶段、重试链和处理耗时，不能验证
支付过程效率或根因。该发现只进入 `pending_human_review`，没有改变本轮 hard pass、
publication、客户状态或首次交付文本。

## 自动化验证

- P8 catalog：13/13 schema validation passed；
- P8 deterministic：12/12 passed；
- P8 real DeepSeek：1/1 hard passed；
- Narrative 边界：67 passed；
- Narrative、发布、持久化与 Provider 聚焦回归：181 passed；
- 完整 Phase 7/8：1676 passed；
- Provider、deployment 和 release manifest 聚焦回归：84 passed；
- TypeScript `tsc --noEmit`：passed；
- Next.js production build：passed；
- 静态合同：28 个 YAML、10 个 capability card、25 个 support record 全部通过；
- Python `compileall` 与 `git diff --check`：passed；
- release manifest：
  `single-authority.final.release.2026-07-24-v55`，当前 source state 校验问题数为 0。

## 后续阶段

Case B 和进一步多 Agent 扩展未进入 P8。支付失败尝试/失败环节事件明细、内部运营和投放
事件继续作为已知数据缺口，不影响本阶段性能验收。
