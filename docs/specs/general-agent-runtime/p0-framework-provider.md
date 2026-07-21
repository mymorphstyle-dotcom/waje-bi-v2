# General Agent Runtime P0：框架与大陆 Provider

## 状态与范围

本阶段已经建立可运行、可测试的 Agents SDK adapter boundary。连续对话、Session 和
应用轮次持久化边界已经进入下一份 P0 合同，见
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
WajeAgentRunRequest
  -> WajeAgentsSdkAdapter
  -> Agents SDK Runner
  -> MainlandModelProvider
  -> explicit AsyncOpenAI client
  -> <WAJE_LLM_BASE_URL>/chat/completions
```

以下边界由代码强制执行：

- provider、base URL、API key、model、model settings 全部显式传入；
- Provider 只创建 `OpenAIChatCompletionsModel`；
- SDK `model=None` 和未配置 model 被拒绝；
- `previous_response_id`、`conversation_id` 和 hosted prompt 被拒绝；
- `OPENAI_API_KEY` 不参与新旧两条 WAJE Provider 配置；
- `openai.com` 及其子域在构造时拒绝，每个 SDK HTTP 请求再次校验目标 origin；
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

## Capability probe

`ProviderCapabilityProbe` 在同一个显式 Provider 和 SDK Runner 上执行：

1. 非流式文本；
2. function calling 和工具结果回传；
3. WAJE schema 强类型最终输出；
4. 流式文本；
5. 流式 tool call；
6. 上下文与输出限制声明及请求限制；
7. thinking 配置；
8. typed error mapping 合同。

任一必需 capability 缺失或 live probe 不通过都会抛出
`ProviderCapabilityError`。HTTP 认证、权限、限流、请求、timeout 和不可用错误映射为
`LLMProviderError`，不触发模型或本地答案降级。部署使用 P3 gate 运行 probe，成功后才接受
Agent turn；普通消息进程不会为每条请求重复执行完整 capability probe。

## 当前验收

协议级测试通过 `httpx.MockTransport` 驱动真实 `AsyncOpenAI`、
`OpenAIChatCompletionsModel` 和 Agents SDK Runner，覆盖 direct response、一次 tool call、
多轮 tool loop、强类型终局、流式文本、流式 tool call、retry、circuit breaker、typed
error、trace replacement、无 `OPENAI_API_KEY` 和出站 origin 断言。真实 DeepSeek 凭据
没有进入仓库；部署环境使用同一 `deepseek_from_env()` 路径执行 live probe。

2026-07-21 已在显式清除 `OPENAI_API_KEY` 的本地部署配置上完成真实 DeepSeek
`deepseek-v4-flash` 验收：全部九项 capability check 通过，thinking 被真实响应观测到，
28 条 trace 只写入 WAJE sink。另一个真实 Runner run 连续执行两轮 function tool，稳定
回传 `current=1`、`current=2`，第三个 model turn 返回强制终局文本；该 run 产生 14 条
WAJE trace 记录。
