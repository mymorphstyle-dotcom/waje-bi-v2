# General Agent Runtime P0：连续对话与状态权威

## 当前完成范围

本阶段建立了 Agents SDK Runner 外围的持久化权威纵向闭环：

- `conversation_messages` 原位扩展为唯一 `ThreadItemLedger`，没有新增第二份消息历史；
- `investigation_threads` 保存原子 `ThreadHead`：state version、活跃 task/topic、pending
  action、最新 item sequence 和唯一 customer state；
- `PostgresAgentSession` 以 SDK Session 四方法协议直接读写 ledger；
- `AgentContextAssembler` 从持久化 recent items、当前 head、活跃 task、accepted decisions
  和 customer-safe publication artifact index 组装版本化 snapshot；
- `AgentTurnRuntime` 幂等接受 user item，调用显式 Agents SDK adapter，原子写入普通
  assistant response、terminal item 和最终 ThreadHead；
- Provider 或 SDK 故障写入真实失败终局，技术错误只留在服务端 item payload 和 WAJE
  trace，客户投影只得到可操作说明；
- Gateway 的新 user item 带 operation key、canonical digest 和 customer visibility，客户
  消息查询只读取 `customer_visible=true` 的同一 ledger；
- 每个 operation 由进程生命周期 advisory lock 单飞；原执行进程退出后锁由 PostgreSQL
  自动释放，常驻 recovery worker 从持久化 user item、ThreadHead 和 owner authority 重建
  精确命令继续执行；
- 已删除总为空的 `prior_topic_material_context` 运行合同。

现有 BI LangGraph、IntentRevision、PlanRevision、query/evidence、claim、publication 和
delivery 权威没有改写。

## 唯一历史与原子 Head

`conversation_messages` 新增通用 item 字段：

- `item_sequence`：thread 内单调序号；
- `item_type`：user、assistant、tool、progress、action、artifact 和 terminal 类型；
- `operation_key`：同一应用操作的稳定幂等身份；
- `item_digest`：重放时检查同身份内容一致性；
- `customer_visible`：Gateway 客户投影边界；
- `payload`：SDK replay item、tool identity 和服务端终局详情。

普通 Gateway insert 由数据库 trigger 在锁住 thread row 后分配 sequence 并推进 state
version。`PostgresThreadItemLedger.append_items()` 在同一事务中校验 expected version、
检查 operation replay、追加一个或多个 item，并 CAS 更新 ThreadHead。部分重放、同 key
异内容和过期 state version 都明确失败。

## Session 与工具持久化

SDK 每轮通过 `PostgresAgentSession.get_items()` 读取最近 replay-ready item。当前 user item
在 Runner 启动前已落账，Session 读取截止到它之前的 sequence，并在 SDK 保存输入时识别
同一 user item，避免重复注入。

function handler 进入前写入稳定 tool call ID、tool name 和参数；handler 返回或抛错后写入
对应 tool result。SDK 在终局再次保存整轮 item 时，Session 用 call ID 命中原记录，不重复
执行工具或追加第二份结果。隐藏 reasoning 只进入 WAJE trace，不进入业务 ledger。

SDK Session 的 `pop_item()` 和 `clear_session()` 明确拒绝，因为 ThreadItemLedger 是
append-only 权威。

## AgentTurnRuntime 终局

每轮最终输出使用强类型 envelope：

```json
{
  "answerMarkdown": "自由组织的业务回答",
  "materialRefs": [],
  "limitationRefs": []
}
```

Runtime 会验证输出引用属于 context artifact 或本轮 tool result 的引用闭包。成功时同一
CAS 追加 customer-visible assistant item 和 server-only task terminal；有 limitation 时
ThreadHead 进入 `completed_with_limits`。Provider、SDK、Session、工具或引用合同失败时，
写入 `failed` assistant/terminal/head，原始错误不进入客户 projection。

重复提交同一 operation 会直接返回已持久化 assistant 与 terminal，不再次调用模型。执行中
的同 operation 返回 retryable `agent_turn_operation_in_progress`，不会启动第二个模型—工具
循环。

## Gateway 边界

Gateway 从 `conversation_messages` 读取 user/assistant 历史，使用
`customer_visible=true` 过滤并按 `item_sequence` 排序。General Agent process 先把 user
operation 写入 ledger 并把 ThreadHead 置为 working，随后才通过专用 startup control pipe
确认接受；HTTP 连接不承担后台执行生命周期。

## 进程监督与常驻恢复

常驻 `tools.runtime.recover_run_dispatches` worker 每个周期处理三类持久化工作：

1. 已提交的 BI `run_dispatches`；
2. `customer_state=working`、active task 为 General Agent run、已有 user item 且没有
   checkpoint/terminal 的应用轮次；
3. 已终局 BI task 对应的 `agent_task_resume_outbox`。

General Agent 恢复命令只从 thread owner、稳定 operation key、user message 和已持久化 typed
pending-action resolution 重建，并重新验证派生 run/item identity。活跃原进程仍持有 operation
lock 时 worker 只记录 `in_progress`；进程消失后数据库释放锁，后续周期接管。恢复会复用
ThreadItemLedger 与 SDK Session 中已有的 user/tool items，终局仍由 `AgentTurnRuntime` 原子
提交。

`npm run worker` 启动连续模式；`--once` 用于部署探测。连续模式隔离每个数据库周期、记录
稳定周期状态、在临时数据库错误后继续轮询，并在 SIGTERM/SIGINT 后于周期边界停止。

## 后续边界

已有材料解释见 [P0 已有材料解释](./p0-existing-material-explanation.md)，BI 分析工具提交见
[P1 BI 分析工具提交边界](./p1-bi-analysis-tools.md)。durable long-tool checkpoint、typed
clarification/approval interruption、SSE cursor 与浏览器恢复已经接入当前运行链；它们仍需
遵守各自的审批绑定、重试终局和资源预算加固合同。
