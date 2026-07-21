# General Agent Runtime P1：正常入口与恢复 transport

## 状态

正常客户消息已切换到 `AgentTurnRuntime`。Gateway 只验证身份、thread 归属、请求幂等身份和
typed pending-action resolution，再启动 SDK-neutral Python process contract。普通消息不会预建
`analysis_runs`；只有模型调用 `run_bi_analysis` 或 `continue_bi_analysis` 后，BI tool gateway
才创建可恢复 task 与 dispatch。

## 正常消息链

```text
POST /api/threads/:threadId/messages
  -> requireThread + stable operationId
  -> bi_agent.runtime.general_agent_entry
  -> AgentTurnRuntime
  -> explicit MainlandModelProvider
  -> OpenAI Agents SDK Runner
  -> direct assistant | artifact tool | durable BI tool | pending action
  -> ThreadItemLedger + ThreadHead
```

Gateway 与客户投影只消费 WAJE command、snapshot 和 pending-action 合同。Agents SDK 的
`RunConfig`、`FunctionTool`、model item 和异常类型停留在 Python adapter 内。

## 长任务终态恢复

`agent_task_resume_outbox` 是 BI task 终态到原 Agent turn 的持久化投递边界。recovery worker
扫描同时满足以下条件的 task：

- `analysis_runs` 已进入 `completed` 或 `failed`；
- ThreadHead 仍以该 task 为 active task；
- 同一 thread 存在 `waiting_for_task` checkpoint，且 `awaitedTaskRef` 匹配 task。

outbox 以 `(thread_id, task_ref)` 唯一，claim 使用 `FOR UPDATE SKIP LOCKED`，并保存 attempt
count、typed error code、可过期 lease 和 fencing epoch。worker 在 claim 后退出时，后续 worker
可以重领；旧 lease 即使迟到完成也不能覆盖新 owner 的结果。成功恢复后，
`AgentTurnRuntime.resume_ready_task` 从 customer-safe publication/material 恢复模型轮次并写入
assistant、task terminal 与终局 ThreadHead。进程在 BI 终态和 outbox 插入之间退出时，下次
扫描会补建同一 outbox record。

## 客户 transport

客户 snapshot 同时携带三类单调身份：

- `stateVersion`：ThreadHead 权威版本；
- `latestItemSequence`：同一 thread 的最新 ledger item sequence；
- `eventCursor`：覆盖 thread item、task、audit 与 publication 变化的 SSE watermark。

SSE endpoint 为 `/api/threads/:threadId/events`。`Last-Event-ID` 只解释为 `eventCursor`；重连先
加载完整权威 snapshot，再按 cursor 推送后续 snapshot。客户端先比较 cursor，再比较
state version，多个标签页不会把较旧 snapshot 覆盖到较新状态。

Gateway detached process 只在 user item 和 `working` ThreadHead 已原子提交后发送 startup ACK。
POST 返回 202 后，首次 snapshot 和 SSE 因而不会被上一轮 terminal 状态提前关闭。

## 当前验收

- Gateway 静态与进程测试证明正常消息不会预建 BI run 或 dispatch；
- direct response 与 typed pending action 可投影为 customer-safe snapshot；
- 独立 event cursor、ThreadHead version 和 item sequence 均通过合同测试；
- terminal BI task 的 outbox enqueue、租约重领、fencing、成功与可重试失败均通过测试；
- TypeScript 类型检查与 Python Provider、AgentTurnRuntime、durable recovery 测试通过。
- 刷新、断网、关闭页面、typed pending action、stale cursor 与多标签页操作身份均通过
  Playwright 真实浏览器测试。

P1 transport 已关闭。后续 P2 长对话压缩、动态工具发现与受控子 Agent 已在
[P2 上下文与委派合同](./p2-context-and-delegation.md)完成。
