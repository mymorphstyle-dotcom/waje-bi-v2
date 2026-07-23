# WAJE Standard Pack v1

## 1. 目的

Standard Pack v1 是 General Agent Runtime 的统一验收合同。它覆盖用户问题进入系统以后，从
intent/plan、能力选择、查询与证据、claim、publication、customer-safe projection、持久化、
Provider 到浏览器呈现的完整链路。

该合同同时服务四类执行环境：

- `agent_live`：通过真实 `GeneralAgentTurnCommand -> AgentTurnRuntime` 路径调用大陆模型；
- `pytest`：验证确定性合同、故障注入、Provider 边界和持久化不变量；
- `playwright`：验证客户页面的状态、交互、排版和安全投影；
- `catalog`：在无数据库、无模型密钥时校验 48 个 canonical scenario 的结构完整性。

所有 adapter 由一个 runner 调度，生成同一种报告。OpenAI hosted Evals、Hosted Grader、
Responses、Conversations 和远程 Trace 后端不参与运行。

## 2. 权威边界

- 业务 truth 继续由已接受的 `IntentRevision`、`PlanRevision`、query/result、evidence、claim、
  publication 和 verifier 合同约束。
- `AgentTurnRuntime` 和 PostgreSQL ledger 继续承担 thread、task、checkpoint、artifact、trace
  identity、幂等、恢复和终局权威。
- Agents SDK Runner 只承担单个应用轮次内的模型—工具循环。
- `agent_live` 只能使用显式配置的 Mainland Model Provider。缺少配置、能力合同不满足或出站
  目标为 `api.openai.com` 时直接失败。
- 原始 Provider payload、技术错误和完整 trace 只进入 Workbench/服务端审计。评测报告只保留
  必需的安全摘要和持久化引用。
- 人工答案质量审核为 advisory observation，不改变 publication/delivery 状态，不触发 writer
  retry、自动改写、撤回或首次交付阻断。

## 3. Canonical scenario 矩阵

v1 固定 48 个 scenario。每个 case 只属于一个主类，同时可以带多个能力与风险标签。

| 主类 | 数量 | 覆盖重点 |
| --- | ---: | --- |
| `business` | 24 | 直答、能力说明、公式分解、澄清、异常与活动边界、证据追问、挑战、修订、时间窗口、层级诊断、claim 与叙事完整性 |
| `runtime` | 12 | direct/tool/multi-tool/typed output、streaming、错误映射、retry/circuit、capability probe、ledger、恢复与 resume |
| `security` | 4 | 禁止 OpenAI 默认出口、租户隔离、customer-safe projection、浏览器安全头 |
| `experience` | 8 | 澄清、pending action、多标签页、过期事件、长任务恢复、失败态、唯一终局回答、长答案可读性 |

case 必须声明来源池：

- `real_user`：真实运营问题措辞；
- `historical_failure`：已确认的历史失败类型；
- `matrix_generated`：按能力、状态、证据边界和故障组合生成的边界 case。

## 4. Case schema

每行 case 遵循 `evals/general_agent_runtime/case.schema.json`：

- `schemaVersion`：固定为 `waje-standard-eval-case.v1`；
- `caseId`：稳定、唯一、可用于单 case 重跑；
- `category`、`questionFamily`、`samplePool`、`riskTier`、`tags`：用于覆盖率和责任归因；
- `profiles`：至少属于 `smoke`、`nightly`、`release` 之一；
- `execution.adapter`：`agent_live`、`pytest` 或 `playwright`；
- `execution.target`：确定性或浏览器 case 的唯一测试目标；
- `execution.awaitTerminal`：真实分析 case 是否等待 durable runtime 收敛到终态；
- `execution.releaseRepeats`：发布 profile 中的重复次数；
- `fixture`：线程模式、独立 completed-thread fixture key、dataset release 与合同版本；
- `turns`：一到多个业务 turn，每个 turn 带 typed expectation，可含推荐澄清选项选择；
- `advisoryReview`：人工评价维度、适用 turn 和决策导向建议要求；
- `failureAttribution`：失败类型与第一责任点，不把所有失败归入模型质量。

自由文本回答不做整句或关键词字典匹配。数字、日期、单位、scope、authority identity、工具、
状态、checkpoint、evidence provenance、claim provenance 和安全字段采用确定性断言。

## 5. Profiles 与重复规则

| Profile | 用途 | 执行范围 |
| --- | --- | --- |
| `smoke` | 提交前快速回归 | 关键 deterministic、少量 live、关键 UI |
| `nightly` | 每日完整覆盖 | 48 case 各执行一次 |
| `release` | 发布验收 | 48 case；`riskTier=critical` 的 `agent_live` case 固定执行 3 次 |

`releaseRepeats=3` 是关键真实 DeepSeek case 的合同下限。runner 拒绝 critical live case 使用更小
值。三次结果分别保留，任何一次硬断言失败都会令该 case 的 hard status 失败。人工评分可以
跨三次观察稳定性，仍保持 advisory。

## 6. 硬断言与人工审核

硬断言覆盖：

- action binding、实际工具调用和禁用工具；
- clarification 选项数量、唯一推荐项和 material decision topic；
- customer state、completion kind、checkpoint schema 和 durable terminal；
- 新任务数量、source task/publication/revision identity；
- publication 完整性、material refs、数字与单位的来源一致性；
- 权限、租户、SQL/数据合同、claim provenance、customer-safe projection；
- Provider capability、typed error、retry/circuit 和禁止 OpenAI 出站；
- 浏览器唯一终局回答、状态恢复、交互与安全头。

人工审核按六个通用维度记录：结论直接性、分析完整性、机制与抵消项、运营含义、证据边界、
阅读体验。决策导向 case 额外要求建议可执行；概念、能力说明和纯证据追问不强制建议。审核
结果为 `pending_human_review`、`reviewed_pass` 或 `reviewed_attention`，不并入 hard pass/fail。

## 7. 报告与留存

统一 runner 生成 `waje-standard-pack-run.v1`：

- catalog 版本、profile、选择条件和运行环境摘要；
- 每个 case 的 adapter、repeat、hard status、失败码、耗时和安全引用；
- 每个 live turn 的 route/tool/state/checkpoint/authority/fidelity 观察；
- 每个 live turn 的总耗时与 scoped recovery cycle 数量；
- advisory review package；
- pytest/Playwright 的退出码、受限输出摘要和可复核截图引用；
- live provider origin 和 `OPENAI_API_KEY` 缐除证明。

报告不得包含 API key、原始行、隐藏推理或未脱敏 Provider payload。真实执行使用隔离数据库或
明确的 fixture namespace；不同 completed-analysis case 必须绑定不同 thread key。

## 8. 演进规则

- 新失败先归因到业务合同、数据/证据、runtime/provider、projection/UI 或评测设施。
- 只有经人工确认、可泛化且具备明确 owner 的模式才进入 runtime guardrail。
- 新需求替代旧合同时直接迁移 schema、case 和 runner，不保留双轨兼容实现。
- v1 case 数量或含义变化需要升级 catalog 版本，并在文档中记录 coverage delta。
