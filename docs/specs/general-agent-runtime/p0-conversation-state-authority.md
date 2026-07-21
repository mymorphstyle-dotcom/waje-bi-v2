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

重复提交同一 operation 会直接返回已持久化 assistant 与 terminal，不再次调用模型。

## Gateway 边界

Gateway 仍从 `conversation_messages` 读取 user/assistant 历史，增加
`customer_visible=true` 过滤并按 `item_sequence` 排序。新 user operation 在创建 run 和
dispatch 的同一事务写入 ledger，并把 ThreadHead 置为 working。现有 SSE 和 BI run 状态
投影继续服务当前 LangGraph 入口，后续入口切换会让普通 Agent turn 的 state version 和
item cursor 成为统一增量源。

## 本阶段之后

已有材料解释已由
[P0 已有材料解释](./p0-existing-material-explanation.md) 完成，BI 分析工具提交边界已由
[P1 BI 分析工具提交边界](./p1-bi-analysis-tools.md) 完成。以下工作尚未进入：

- 将所有现有 Conversation 请求全面切到 `AgentTurnRuntime`；
- durable long-tool checkpoint、lease 后恢复 SDK run state；
- clarification/approval interruption、完整 SSE cursor 与真实浏览器恢复验收已在 P1
  transport cutover 完成。
