# General Agent Runtime P3：部署与真实环境验收

## 状态

2026-07-22 已在仓库 `.env` 指向的目标环境完成 P3 交付。目标 PostgreSQL 先生成快照一致、
可独立校验的 v9 备份，再原位升级到 v12；该次交付证据保存在
`artifacts/deployment-reports/general-agent-deployment-v12-20260722.json`。随后按当前合同把同一目标
从 v12 原位升级到 v13，增加并校验已发布客户 payload 的持久化 digest。两次升级都保持全部
既有业务表行数不变。当前 repository、PostgreSQL 只读审计、真实 DeepSeek
capability/application smoke、WAJE trace 和出站目标检查全部通过，v13 机器报告保存在
`artifacts/deployment-reports/general-agent-deployment-v13-20260722.json`。真实客户链另由
`waje-standard-pack-run.v1`、回答完整性实测和浏览器截图验收。每个后续部署目标仍需
独立执行本页流程并保存自己的机器报告。

P3 不改变 Agent、BI、publication 或客户投影权威。它把 P0-P2 已接受合同组合成部署前的
可执行 release gate。

## Production customer identity boundary

Production Gateway requests must arrive through an authenticating ingress that
removes every client-supplied `x-waje-authenticated-*` header and writes these
three headers after authentication:

- `x-waje-authenticated-user-id`;
- `x-waje-authenticated-user-issued-at`, as Unix seconds;
- `x-waje-authentication-signature`, as lowercase HMAC-SHA256 hex.

The signature input is the newline-joined sequence
`waje-auth-v1`, uppercase HTTP method, exact path plus query, issued-at value,
and actor ID. The Gateway verifies it with `WAJE_AUTH_HEADER_SECRET`, which must
contain at least 32 UTF-8 bytes. `WAJE_AUTH_HEADER_MAX_AGE_SECONDS` defaults to
60 and may be set from 1 through 300. Missing configuration, unsigned identity,
request replay outside the allowed window, path changes, method changes, and
actor changes all fail closed before thread ownership is evaluated. The
application service must not be exposed around the authenticating ingress.

## 统一门禁

所有生产门禁和常驻进程使用同一个预构建、已审计的 Python 3.12 环境。Gateway 的
`WAJE_PYTHON_EXECUTABLE` 必须配置为该环境中 Python 的绝对路径；部署期间不执行依赖解析。
以下示例用 `$WAJE_PYTHON_EXECUTABLE` 表示这一路径。

Repository-only：

```bash
"$WAJE_PYTHON_EXECUTABLE" -m tools.runtime.validate_general_agent_deployment
```

数据库只读审计：

```bash
"$WAJE_PYTHON_EXECUTABLE" -m tools.runtime.validate_general_agent_deployment --database
```

真实大陆 Provider 与 P2 smoke：

```bash
env -u OPENAI_API_KEY \
  "$WAJE_PYTHON_EXECUTABLE" -m tools.runtime.validate_general_agent_deployment --live-provider
```

完整门禁及机器报告：

```bash
env -u OPENAI_API_KEY \
  "$WAJE_PYTHON_EXECUTABLE" -m tools.runtime.validate_general_agent_deployment \
  --all --json-output /tmp/waje-general-agent-deployment.json
```

报告合同为 `general-agent-deployment.v1`。报告只保存检查状态、稳定 error code、Provider/model
身份、声明限制和 content-addressed probe refs；不保存 API key、原始 Provider payload 或客户
业务内容。

## v9/v10/v11/v12→v13 schema upgrade

先按现有运行规范完成数据库备份，再执行：

```bash
"$WAJE_PYTHON_EXECUTABLE" \
  -m tools.runtime.cutover_single_authority_schema --in-place-upgrade
```

in-place upgrade 接受经过 migration ID 与 digest 双重校验的 v9、v10、v11 或 v12 source。v9/v10
允许新增：

- `agent_task_resume_outbox`；
- `agent_thread_summaries`；
- `agent_generated_artifacts`。

v11 允许新增：

- `agent_thread_summaries`；
- `agent_generated_artifacts`。

v12 不新增表。所有 source 都会建立并验证
`publication_customer_payloads.customer_payload_digest`，使持久化客户 payload 与其封存 digest
可以由数据库只读门禁直接核对。其余声明表必须完整存在且不能出现未知表；升级前后所有既有
业务表行数必须完全一致，source 允许新增的表必须为空。DDL、验证和 migration identity 更新
位于同一事务，失败会 rollback。升级完成后立即执行 `--database` gate。

## Database gate

数据库检查使用 `REPEATABLE READ READ ONLY` 事务，只验证：

- v13 migration ID 与 digest；
- `publication_customer_payloads.customer_payload_digest` 必需列；
- summary、generated artifact、conversation、thread 和 migration 表；
- summary/generated artifact append-only triggers；
- `conversation_messages` 接受 `tool_selection` 的当前 item-type constraint；
- `audit_events.thread_id` 的新写入外键和 thread 删除级联。该约束以 `NOT VALID` 安装，保留
  历史上已失去 thread 的审计记录；报告会给出历史 orphan 数量和 constraint validation 状态。

检查不会创建 thread、task、artifact、trace 或业务数据。

## Live Provider gate

live gate 从显式 DeepSeek 环境构造 `MainlandModelProvider`，并从传入环境副本删除
`OPENAI_API_KEY`。它顺序验证：

1. 文本、function calling、结构化输出、流式文本、流式 tool call、上下文/输出限制、thinking
   和 typed error mapping；
2. 真实 typed thread summary 及 source closure；
3. 动态工具最小选择和 digest replay；
4. 受控子任务及 generated artifact source closure；
5. `waje-agent-trace.v1` 起止事件只进入 WAJE sink；
6. report 与 trace 中没有 `api.openai.com`。

live gate 不访问 PostgreSQL，不修改 ThreadHead，不创建 BI run。完整业务服务验收仍由正常
Gateway、AgentTurnRuntime 和既有 BI acceptance suite 承担。

## Production runtime configuration

生产环境必须显式配置 Provider、数据库、身份和进程路径。容量参数有受边界约束的默认值，部署
负责人仍需根据副本数和 PostgreSQL 连接预算明确确认：

| Configuration | Contract |
| --- | --- |
| `WAJE_LLM_PROVIDER`, `WAJE_LLM_BASE_URL`, `WAJE_LLM_API_KEY`, `WAJE_LLM_MODEL` | 唯一大陆模型出口；base URL 必须是已验收 HTTPS origin，运行环境删除 `OPENAI_API_KEY`。 |
| `WAJE_RUNTIME_DATABASE_URL` | Gateway、worker、General Agent 和 BI runtime 共享的 PostgreSQL 权威库。 |
| `WAJE_PYTHON_EXECUTABLE` | 预构建 Python 3.12 环境的绝对路径；Gateway 启动时校验。 |
| `WAJE_AUTH_HEADER_SECRET` | ingress 与 Gateway 共用的至少 32 字节 HMAC secret。 |
| `WAJE_HEALTH_READINESS_TOKEN` | 至少 32 字节；只通过内部探针 header `x-waje-readiness-token` 传入。公开 liveness 不需要 token。 |
| `WAJE_AGENT_MAX_PROCESSES` | PostgreSQL advisory admission 的全局槽位，默认 16，范围 1–256。 |
| `WAJE_AGENT_MAX_PROCESSES_PER_ACTOR` | 单 actor 槽位，默认 2，范围 1–16。 |
| `WAJE_SSE_MAX_CONNECTIONS` / `WAJE_SSE_MAX_CONNECTIONS_PER_ACTOR` | 单 Gateway 进程的 SSE 上限，默认 128 / 4。全局跨副本上限由 ingress 同时约束。 |
| `WAJE_SSE_CONNECTION_TTL_MS` / `WAJE_SSE_POLL_INTERVAL_MS` | 默认 300000 / 2000；允许范围 10000–1800000 / 500–30000。 |
| `WAJE_RUNTIME_WORKER_POLL_SECONDS` | recovery worker 周期间隔，默认 2，必须为正数。 |
| `WAJE_RUN_DISPATCH_LEASE_MS` | BI dispatch lease，默认 30000，必须为 1–86400000 的规范十进制整数。 |

`npm run worker` 和 `npm run prune:agent-traces` 读取同一
`WAJE_PYTHON_EXECUTABLE`。SDK trace 的当前固定合同为单 record 512 KiB、单 run 256 records、
保留 30 天；prune 作为每日作业运行，结果进入运维审计。

## 每个生产目标仍需执行

- 配置至少 32 字节的 `WAJE_AUTH_HEADER_SECRET`，并验证 ingress 删除外部同名 header 后按
  上述请求绑定合同重新签名；
- 对目标 PostgreSQL 执行备份与 v13 in-place upgrade；
- 使用目标数据库和目标 DeepSeek 配置运行 `--all`，保存并附加
  `general-agent-deployment.v1` 通过报告；
- 确认目标网络策略仍只允许已验收的大陆 Provider origin；
- 将 `npm run worker` 作为独立常驻进程部署，并与 Gateway 使用相同的 PostgreSQL、Provider
  和密钥配置。worker 每个周期处理 BI `run_dispatches`、从持久化 user item 恢复未终局的
  General Agent turn、处理 BI task resume outbox；SIGTERM/SIGINT 会在当前周期边界优雅停止。
  运维探测可以运行 `python -m tools.runtime.recover_run_dispatches --once`；它只执行一个周期并
  返回周期状态。生产部署必须保证至少一个 worker 实例持续运行，允许多实例并发，数据库租约、
  fencing 与 operation advisory lock 负责单飞；
- 继续遵守当前要求，不新建或重跑 Case B。
