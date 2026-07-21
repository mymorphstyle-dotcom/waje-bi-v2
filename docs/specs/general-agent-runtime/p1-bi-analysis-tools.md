# General Agent Runtime P1：BI 分析工具提交边界

## 状态

`run_bi_analysis` 与 `continue_bi_analysis` 的 Agents SDK-neutral adapter boundary
已经实现。两个工具只负责接受强类型参数并提交持久化 BI 任务；完整分析仍由现有
Conversation worker 调用 LangGraph 单权威工作流完成。

长工具 checkpoint、SDK run 恢复和 clarification / approval interruption 的监督边界已完成，
详见 [P1 长工具监督与恢复边界](./p1-durable-tool-supervision.md)。生产 worker 终局自动触发、
正常入口与 thread SSE cursor 已在 P1 transport cutover 接入。

## 当前纵向链路

```text
OpenAI Agents SDK Runner
  -> WAJE WajeAgentTool
  -> PostgresBiAnalysisTaskGateway
  -> analysis_runs + run_dispatches
  -> DurableToolBridge + AgentCheckpoint
  -> recover_run_dispatches worker
  -> ConversationAgentCore
  -> IntentRevision + PlanRevision
  -> LangGraph 单权威 BI 工作流
  -> evidence + claim + publication + delivery
```

Runner 在长工具返回统一 `AgentToolResult` 后立即停止当前进程内循环。十分钟级任务的
执行状态、恢复身份和最终 publication 继续由 PostgreSQL 持有。

## 工具合同

### `run_bi_analysis`

输入只有开放业务问题：

```json
{
  "businessQuestion": "分析付费金额变化，并说明证据强度。"
}
```

工具不在本地猜测指标、窗口、baseline、分析路线或业务语义。原始文本进入现有 typed
Intent binding 和 accepted PlanRevision 链。

### `continue_bi_analysis`

输入引用一个已有 customer publication，并显式声明本次替换的当前 PlanRevision 字段：

```json
{
  "sourceTaskRef": "run-ref",
  "revisionRequest": "把窗口改成最近七个完整自然日。",
  "supersededPlanFields": ["time_spec", "resolved_window_refs"]
}
```

Provider adapter 只接受 `MaterialRevisionContinuation` 合同已有的可修订字段。源任务必须
属于当前 thread，必须存在 customer-safe publication、唯一 active IntentRevision 和最新
accepted workflow transition。新任务通过 `intentRevisionContext` 进入既有 revision 权威，
不会修改或复制源 publication。

## 持久化与幂等

- 每个工具 operation 生成稳定 `request_identity`、run ref 和 dispatch ref；
- request digest 覆盖 producer、thread、scope 和完整 request payload；
- 相同 operation 与相同 payload 返回原 task；
- 相同 operation 携带不同 payload 明确返回冲突；
- dispatch 复用 AgentTurnRuntime 已写入的 user item，不建立第二套对话历史；
- 提交事务只写 queued `analysis_runs`、`run_dispatches`、ThreadHead 投影和服务端 audit；
- IntentRevision、PlanRevision、SQL、证据、claim 和 publication 仍由 worker 内的单权威
  工作流写入。

## ToolResult 与错误边界

所有 Agent 工具共用 `bi_agent.runtime.agent_sdk_contracts.AgentToolResult`。BI 工具提交成功
时只返回 task ref、queued 状态、源 task ref 和 replay 标记；此时没有凭空生成 artifact、
material 或 limitation ref。

数据库、来源或幂等冲突映射为 typed failed result。客户摘要不包含 SQL、Provider payload、
数据库信息或内部异常文本；完整工具调用与结果继续进入 WAJE trace / Workbench。

## 已验证边界

- 开放中文业务文本原样进入 recoverable command；
- `continue_bi_analysis` 绑定源 active IntentRevision 与 accepted parent transition；
- 未发布材料不能成为 continuation 来源；
- 同一 operation 幂等重放，变更 payload 明确失败；
- 工具提交不写第二条 conversation message，也不提前写 BI 权威表；
- 无 `OPENAI_API_KEY` 时，Agents SDK Runner 通过显式大陆 Provider 的
  `/v1/chat/completions` 选择长工具，并在一次模型请求后交还 WAJE Runtime；
- 测试断言所有模型请求 host 均不为 `api.openai.com`。

## 后续阶段

- 生产 worker 终局回调与恢复 outbox；
- P1 transport 浏览器验收已完成；长对话与多 Agent 能力进入 P2。
