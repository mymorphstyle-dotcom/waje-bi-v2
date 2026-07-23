# General Agent Runtime P0：框架与大陆 Provider

## 状态与范围

本阶段已经建立可运行、可测试的 Agents SDK adapter boundary。2026-07-21 的
`waje-standard-pack-run.v1` 在真实 DeepSeek、隔离 PostgreSQL 和无
`OPENAI_API_KEY` 环境中 7/7 通过；必需工具执行、强类型终局、澄清恢复、持久化 checkpoint、
客户语言和出站安全均进入应用级验收。连续对话、Session 和应用轮次持久化边界见
[`p0-conversation-state-authority.md`](./p0-conversation-state-authority.md)。

现有 LangGraph 单权威工作流继续由 `run_bi_analysis`、`continue_bi_analysis` 等后续 BI
工具封装承接。本阶段没有改变 IntentRevision、PlanRevision、SQL 安全、数据合同、证据、
claim、publication 或 delivery 权威。

## 依赖与边界

运行依赖锁定为：

- `openai-agents==0.8.4`；
- `openai==2.44.0`；
- `eval-type-backport==0.4.0`，用于 Python 3.9 工具环境解析 SDK 类型；生产 Gateway
  继续使用 Python 3.12。

`openai==2.44.0` 是当前 SDK 组合的兼容上界。2.45 及之后的 response usage 类型新增
必填字段，而 `openai-agents==0.8.4` 的 usage 默认构造尚未提供该字段，会在 Runner 发出
模型请求前失败。

SDK 类型只允许出现在以下 Python 边界：

- `agents_sdk_adapter.py`：将 WAJE run、tool、stream 和 terminal 合同转换为 SDK 类型；
- `mainland_model_provider.py`：实现显式 `ModelProvider` 和 Chat Completions model；
- `agents_sdk_trace.py`：将 SDK trace/span 转换为 WAJE 审计记录。

Gateway、客户投影、BI 合同和 DOM 只消费 `agent_sdk_contracts.py` 中的 WAJE 类型。

## 模型请求路径

```text
General Agent: WajeAgentRunRequest
  -> WajeAgentsSdkAdapter -> Agents SDK Runner
  -> MainlandModelProvider -> explicit AsyncOpenAI client

BI LangGraph typed nodes
  -> MainlandModelProvider.structured_client(...)
  -> OpenAICompatibleLLMClient

Both transports
  -> exact configured HTTPS origin
  -> <WAJE_LLM_BASE_URL>/chat/completions
```

以下边界由代码强制执行：

- provider、base URL、API key、model、model settings 全部显式传入；
- Provider 只创建 `OpenAIChatCompletionsModel`；
- SDK `model=None` 和未配置 model 被拒绝；
- `previous_response_id`、`conversation_id` 和 hosted prompt 被拒绝；
- `OPENAI_API_KEY` 不参与新旧两条 WAJE Provider 配置；
- BI typed client 与 Agents SDK client 共享同一 `MainlandProviderConfig` 配置工厂，业务层和
  工具脚本不再单独解析 Provider 环境变量；
- base URL 必须使用 HTTPS，`openai.com` 及其子域在构造时拒绝，两条传输链的每个 HTTP
  请求都再次校验目标 origin；
- HTTP redirect 关闭；
- timeout、HTTP retry 和 circuit breaker 位于 Provider 层。

首个真实配置入口为 `MainlandModelProvider.deepseek_from_env()`。必需环境变量为：

```text
WAJE_LLM_PROVIDER=deepseek
WAJE_LLM_BASE_URL=https://api.deepseek.com/v1
WAJE_LLM_MODEL=<accepted DeepSeek model>
WAJE_LLM_API_KEY=<key>              # 或 DEEPSEEK_API_KEY
```

已验收的 `deepseek-v4-flash` 和 `deepseek-v4-pro` 使用 Provider 内的显式能力档案：1M
context、8192 max output、thinking enabled。以下环境变量可以收紧或覆盖该档案；未知模型
必须全部提供，Provider 不推断未知能力：

```text
WAJE_LLM_CONTEXT_WINDOW_TOKENS=<declared provider limit>
WAJE_LLM_MAX_OUTPUT_TOKENS=<declared and requested output limit>
WAJE_LLM_THINKING=enabled|disabled
```

`WAJE_LLM_TIMEOUT_SECONDS` 只有在配置正数时启用。`WAJE_LLM_MAX_ATTEMPTS` 缺省为 3；
SDK Runner 和业务工具不得再包重试循环。

General Agent 与 BI typed client 在生产接线时都使用 `PostgresProviderCircuit`。熔断事件按
Provider、origin、model 和 transport 形成稳定 circuit identity，写入 WAJE audit ledger；
不同 detached process 读取同一连续失败窗口。达到阈值后请求在出站前失败，恢复窗口结束只
允许一个 probe claim 进入；Provider 成功或可重试失败继续写入对应状态事件。单元测试与无
PostgreSQL 的隔离 capability probe 可以显式使用进程内 circuit。

## 结构化输出、工具和流

DeepSeek Chat Completions 的 JSON Object mode 负责生成合法 JSON，Agents SDK 的
Pydantic output schema 负责最终强类型验证。WAJE function tool 同样通过 Pydantic input
model 验证参数；无效参数产生明确终局错误。Provider 支持原生增量 tool call 时投影
`tool_call_delta`，不稳定 Provider 可以在 capability 中声明 `buffered`，完整缓冲后再交给
Runner。

reasoning/thinking 内容保留在服务端技术 trace，不能进入 WAJE stream event、客户合同或
业务权威。

## Trace 与审计

创建 adapter 时必须传入 WAJE `AgentTraceSink`。初始化会用 `WajeTraceProcessor` 替换
SDK 的全部默认 trace processors，因此 run、model turn、tool call 和错误只写入 WAJE
sink。生产接线使用 `PostgresAgentTraceSink` 复用现有 `audit_events`；单元测试使用隔离的
`InMemoryAgentTraceSink`。客户投影没有 trace sink 或 SDK payload 字段。

生产 trace 使用独立 PostgreSQL connection，不能和 SDK Session/ThreadItemLedger 并发复用
同一 connection。单条记录上限 512 KiB、单个 Agent run 上限 256 条；超限会写入不含原始
payload 的 `agents_sdk_trace_record_rejected` 并使当前 turn 以 typed trace persistence failure
结束。完整记录保留 30 天，部署通过 `npm run prune:agent-traces` 清理过期和孤儿记录；删除
thread 时数据库 trigger 同步删除该 thread 的 SDK trace。`/api/agent-runs` 是受内部访问控制
保护的唯一完整技术 trace 读取面，客户 thread/message 投影不读取这些 payload。

## Capability probe

`ProviderCapabilityProbe` 在同一个显式 Provider 和 SDK Runner 上执行：

1. 非流式文本；
2. function calling 和工具结果回传；
3. WAJE schema 强类型最终输出；
4. 流式文本；
5. 流式 tool call；
6. 上下文预算拒绝边界，以及真实请求中观测到的输出限制；
7. thinking 配置；
8. typed error mapping 合同，覆盖认证、权限、限流、超时、连接、服务端和请求拒绝。

probe 结果分别保存能力声明与请求观测。origin、path、model、`max_tokens` 和 thinking 必须
来自真实 HTTP/模型事件；context limit 记录 WAJE 入站预算边界的正反例；typed error mapping
运行当前 mapper 的分类矩阵，不能用配置布尔值代替。任一必需 capability 缺失或 live probe 不通过都会抛出
`ProviderCapabilityError`。HTTP 认证、权限、限流、请求、timeout 和不可用错误映射为
`LLMProviderError`，不触发模型或本地答案降级。部署使用 P3 gate 运行 probe，成功后才接受
Agent turn；普通消息进程不会为每条请求重复执行完整 capability probe。

## 当前验收状态

协议级测试通过 `httpx.MockTransport` 驱动真实 `AsyncOpenAI`、
`OpenAIChatCompletionsModel` 和 Agents SDK Runner，覆盖 direct response、一次 tool call、
多轮 tool loop、强类型终局、流式文本、流式 tool call、retry、circuit breaker、typed
error、trace replacement、无 `OPENAI_API_KEY` 和出站 origin 断言。真实 DeepSeek 凭据
没有进入仓库；部署环境使用同一 `deepseek_from_env()` 路径执行 live probe。

2026-07-21 在显式清除 `OPENAI_API_KEY` 的本地部署配置上，真实 DeepSeek
`deepseek-v4-flash` 的 capability 与应用级门禁通过。最终模型请求只到
`https://api.deepseek.com` 的 Chat Completions 路径；WAJE 审计记录了 9 个 trace、62 个
trace/span 事件，OpenAI exporter 未启用。

真实应用链评测 7/7 通过：direct response、能力目录 function tool、BI 长工具提交、
`comparison_scope` 澄清、`baseline_or_counterfactual` 澄清后恢复、多轮 tool loop、描述性
异常敏感性分析和已发布证据追问均符合当前合同。澄清工具参数不满足客户语言或推荐选项合同
时，Runner 在同一应用轮次内要求模型修正；技术错误和原始 Provider payload 只进入 WAJE
审计。评测报告见 `evals/general_agent_runtime/results/repair-20260721.json`。
