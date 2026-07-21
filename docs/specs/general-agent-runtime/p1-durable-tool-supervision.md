# General Agent Runtime P1：长工具监督与恢复边界

## 状态

AgentTurnRuntime 已具备可运行的 durable tool supervision 纵向闭环：Agents SDK
Runner 选择长工具后立即交回 WAJE Runtime；Runtime 从同一 ThreadItemLedger 读取工具结果，
保存 SDK-neutral AgentCheckpoint，并让 ThreadHead 持有后台 task 或 pending action 的唯一
客户状态。

现有 `run_dispatches` worker 继续承担 BI 任务 lease、heartbeat、回收和终局写入。
Runtime 新增权威 completion loader，可从 `analysis_runs` 状态和 customer-safe publication
恢复原 agent turn。生产 worker 已通过 `agent_task_resume_outbox` 自动触发恢复；正常消息入口
已切换到 Agent Runtime，thread SSE 已接入独立 cursor。浏览器多标签页、断网、关闭页面与
stale cursor 验收已经通过。

resume outbox 使用可过期 lease 与单调 fencing epoch。claim 后崩溃的 worker 会被重领，持有
旧 epoch 的迟到 worker 无权提交 completed 或 failed，确保恢复投递不会永久停在 processing，
也不会覆盖新 owner 的结果。

## 运行链

```text
Mainland Chat Completions
  -> OpenAI Agents SDK Runner
  -> WajeAgentTool(execution_mode=suspend_turn)
  -> PostgresAgentSession / ThreadItemLedger
  -> DurableToolBridge
  -> AgentCheckpoint
  -> ThreadHead(active task | pending action)

run_dispatches worker
  -> existing ConversationAgentCore + LangGraph authority workflow
  -> analysis_runs terminal state + customer-safe publication
  -> AuthoritativeAgentTaskCompletionLoader
  -> AgentTurnRuntime.resume_ready_task
  -> Mainland Chat Completions
  -> assistant message + task_terminal
```

SDK `StopAtTools` 只存在于 `WajeAgentsSdkAdapter`。Gateway、客户投影、BI 工具合同和
持久化模型只看到 WAJE 类型。

## 长工具控制合同

`WajeAgentTool.execution_mode` 当前有两个值：

- `continue`：Runner 在工具结果后继续模型—工具循环；
- `suspend_turn`：Runner 在目标工具完成后结束进程内循环，WAJE Runtime 接管后续状态。

`run_bi_analysis`、`continue_bi_analysis`、`ask_user` 和 `request_approval` 使用
`suspend_turn`。普通读取与计算工具保持 `continue`，因此 direct response、一次 function
call 和多轮 tool loop 的现有能力不受影响。只要一个轮次包含 suspending tool，adapter
会显式设置 `parallel_tool_calls=false`，避免一次模型响应创建多个后台副作用或多个 pending
action。

## AgentCheckpoint

`AgentCheckpoint` 是冻结的 WAJE 合同，包含：

- schema version、稳定 checkpoint ref 和 content digest；
- thread、agent run、operation、源 tool call 身份；
- `waiting_for_task`、`waiting_for_user` 或 `waiting_for_approval`；
- 后台 task ref 或强类型 pending action；
- context version、原始 action binding digest 和持久化 session sequence。

checkpoint 作为 customer-invisible ThreadItem 保存。checkpoint ref 和内容摘要都会在读取时
重算；owner、operation、task 或摘要不一致会明确失败。崩溃发生在 tool result 已写入、
checkpoint 尚未写入的窗口时，DurableToolBridge 会从原 tool result 重建同一 suspension，
无需再次调用模型或重复提交任务。

暂停消息、恢复消息和 terminal 分别使用独立幂等 operation key。重复请求只重放对应阶段，
不会出现部分重放冲突。

## 后台任务恢复

`AuthoritativeAgentTaskCompletionLoader` 只接受当前 thread 下同一 task 的权威状态：

- 中间状态返回等待，不触发 Runner；
- `failed` 返回固定客户安全摘要，不读取原始异常；
- `completed` 必须找到 source refs 包含该 task 的 `bi_publication`；
- publication 缺失、跨 thread、身份冲突或限制合同错误都明确失败；
- artifact、material 和 limitation refs 作为 relevant materials 注入新的 context version。

`resume_ready_task` 按 task ref 找回原 checkpoint，再构造 SDK-neutral
`AgentTaskResumeRequest`。恢复后的模型输出必须引用至少一个权威 completion material，且
覆盖全部 limitation refs，随后 Runtime 原子提交 assistant message、task terminal 和终局
ThreadHead。terminal admission 继续引用发起长任务的 action binding digest，使动作选择、
后台任务和最终发布保持同一条可验证链。失败终局无需再次调用模型。

## 澄清与审批

`ask_user` 接受一个会实质改变业务判断的决定和 2–3 个业务选项，恰好一个选项标记推荐。
`request_approval` 接受 action summary 与 side-effect scope。两个工具都生成稳定 action ref，
并进入 `needs_input` ThreadHead。

恢复请求必须携带 `PendingActionResolution`：

- ask_user 只接受 `answered` 与合法 option id；
- request_approval 只接受 `approved` 或 `rejected`；
- stale ref、跨类型决定、重复 operation 携带不同 payload 都明确失败。

客户投影只包含可读 prompt、选项或副作用范围。task ref、checkpoint ref、Provider payload、
模型名和技术错误继续留在服务端 ledger 与 Workbench trace。

## 已验证边界

- 无 `OPENAI_API_KEY` 时，真实 Agents SDK 经显式大陆 Provider 调用
  `/v1/chat/completions`，一次请求提交 BI 长工具后立即暂停；
- 出站 host 仅为显式配置的大陆 Provider，测试断言不会访问 `api.openai.com`；
- direct response 和普通多轮 function loop 继续通过；
- queued BI task 不会被 AgentTurnRuntime 清空或提前写成 completed；
- tool result 已持久化后的 crash window 能无模型重放恢复 checkpoint；
- 后台 completed、failed、重复恢复、checkpoint 篡改、跨 task publication 和缺失
  publication 都有测试；
- ask_user 与 request_approval 的 typed resolution 均有测试；
- 客户投影不包含 task/checkpoint/技术错误身份。

## 后续阶段

长对话 compaction、summary 版本化、source closure 压力测试、动态工具发现与受控多 Agent
进入 P2，不再属于 P1 transport 范围。
