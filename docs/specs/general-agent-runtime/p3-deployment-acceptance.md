# General Agent Runtime P3：部署与真实环境验收

## 状态

仓库内门禁已完成，repository-only preflight 已通过。当前 shell 没有
`WAJE_RUNTIME_DATABASE_URL`、`WAJE_LLM_BASE_URL` 或 `WAJE_LLM_API_KEY`，所以本轮没有修改
外部数据库，也没有重新发起真实 DeepSeek 请求。数据库和 live Provider gate 会明确失败并
返回 typed error，环境配置就绪后可直接执行同一命令。

P3 不改变 Agent、BI、publication 或客户投影权威。它把 P0-P2 已接受合同组合成部署前的
可执行 release gate。

## 统一门禁

Repository-only：

```bash
uv run --python 3.12 --with-requirements requirements.txt \
  python -m tools.runtime.validate_general_agent_deployment
```

数据库只读审计：

```bash
uv run --python 3.12 --with-requirements requirements.txt \
  python -m tools.runtime.validate_general_agent_deployment --database
```

真实大陆 Provider 与 P2 smoke：

```bash
env -u OPENAI_API_KEY \
  uv run --python 3.12 --with-requirements requirements.txt \
  python -m tools.runtime.validate_general_agent_deployment --live-provider
```

完整门禁及机器报告：

```bash
env -u OPENAI_API_KEY \
  uv run --python 3.12 --with-requirements requirements.txt \
  python -m tools.runtime.validate_general_agent_deployment \
  --all --json-output /tmp/waje-general-agent-deployment.json
```

报告合同为 `general-agent-deployment.v1`。报告只保存检查状态、稳定 error code、Provider/model
身份、声明限制和 content-addressed probe refs；不保存 API key、原始 Provider payload 或客户
业务内容。

## v11→v12 schema upgrade

先按现有运行规范完成数据库备份，再执行：

```bash
uv run --python 3.12 --with-requirements requirements.txt \
  python -m tools.runtime.cutover_single_authority_schema --in-place-upgrade
```

in-place upgrade 只接受精确的 `single-authority-workflow.v11` source migration。升级允许新增：

- `agent_thread_summaries`；
- `agent_generated_artifacts`。

其余声明表必须完整存在且不能出现未知表；升级前后所有既有业务表行数必须完全一致，两个新表
必须为空。DDL、验证和 migration identity 更新位于同一事务，失败会 rollback。升级完成后立即
执行 `--database` gate。

## Database gate

数据库检查使用 `REPEATABLE READ READ ONLY` 事务，只验证：

- v12 migration ID 与 digest；
- summary、generated artifact、conversation、thread 和 migration 表；
- summary/generated artifact append-only triggers；
- `conversation_messages` 接受 `tool_selection` 的当前 item-type constraint。

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

## 尚待部署环境执行

- 对目标 PostgreSQL 执行备份与 v12 in-place upgrade；
- 运行 `--database` 并保存报告；
- 使用目标 DeepSeek 配置运行 `--live-provider`；
- 在服务发布前运行 `--all`，将通过报告附到发布记录；
- 继续遵守当前要求，不新建或重跑 Case B。
