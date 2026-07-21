# General Agent Runtime P2：上下文压缩与受控委派

## 状态

P2 已完成。生产 Agent 每轮从 PostgreSQL 权威读取版本化摘要、未压缩 ledger items、任务、
decision 和客户安全 artifact。上下文超过 item 或 Provider 输入预算时，Runtime 先生成新摘要
并重新组装；任何无法在预算内闭合的上下文都会形成 typed terminal error。

## 版本化摘要

`VersionedThreadSummary` 是 append-only 压缩产物，保存：

- 连续覆盖的 item sequence 区间与每个 item digest；
- 前一版 summary ref 与 digest；
- artifact、material authority refs；
- typed statement、source digest、content digest 和 summary digest。

每条 statement 必须引用本次 source item、前序摘要或显式 authority ref。`business_fact`
还必须引用 artifact/material authority。摘要不修改原始 `ThreadItemLedger`，不授予事实、权限、
任务或发布权威。`PostgresAgentSession` 从摘要覆盖终点之后回放原始模型历史，artifact 仍可按
稳定 ref 重新读取。

摘要生成与主 Agent 共用显式 `MainlandModelProvider` 和 WAJE trace sink。模型失败、来源断链、
摘要持久化冲突和压缩后仍超预算均映射为 WAJE typed error；运行链没有本地高价值摘要模板。

## 动态工具发现

Runtime 将当前候选工具的名称、用途、执行模式和输入 schema 交给 typed tool selector。
selector 只返回当前请求所需的最小可选工具集；`ask_user`、`request_approval` 等运行安全工具
作为 mandatory tools 保留。选择结果包含 catalog/input/selection digest，并写入
`ThreadItemLedger` 的 `tool_selection` item。

同一 operation 恢复时严格读取并校验原选择。未知工具、超出数量、catalog 漂移或 digest
冲突明确失败。能力目录由 reviewed `RuntimeContractRegistry` active binding 生成，不用自由文本
关键词判断业务能力。

## 受控子 Agent

`delegate_independent_investigations` 只接收一至三个相互独立的调查：证据复核、竞争假设复核、
独立报告章节或质量审计。每个调查最多引用五个已注册 customer-safe artifacts，并通过同一
大陆模型 adapter 以无工具、单轮、强类型方式执行。独立调查可并行。

子结果的 finding 与 limitation refs 必须落在输入 artifact 及其客户安全 material/source refs
闭包内。校验通过后保存为 `controlled-subagent-result.v1` artifact，包含版本、digest、source
refs、visibility policy、客户摘要和结构化 detail。主 Agent读取这些 artifact 完成最终综合。

子 Agent 没有 ThreadHead、客户消息、BI task、SQL、审批或外部副作用写权限。主 Agent 持有
唯一 thread 和最终客户回答权威；现有 LangGraph 单权威 BI 工作流保持在 BI tools 内部。

## 持久化与迁移

`single-authority-workflow.v12` 增加 `agent_thread_summaries` 和
`agent_generated_artifacts`，并将 `tool_selection` 纳入 conversation item 合同。迁移保留原
thread、task、checkpoint、artifact、publication 和 trace identity，不建立第二套消息历史。

## 验收边界

- item 数量与 Provider token 预算都会触发显式 compaction；
- 摘要覆盖后的 Session replay 不重复旧 item；
- 摘要与子结果都验证 source closure；
- 工具发现可恢复且 catalog 漂移明确失败；
- 子调查并发执行，结果只进入 artifact registry；
- Gateway 和客户 DOM 不依赖 Agents SDK 类型；
- Provider、timeout、retry、熔断与 WAJE-only trace 边界保持 P0 合同。
