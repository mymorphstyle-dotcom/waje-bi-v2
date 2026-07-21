# General Agent Runtime live eval

`cases.jsonl` 保存真实用户措辞和结构化 expectation package。当前评测只约束 action
binding、实际工具调用、持久化状态、checkpoint、authority refs、客户语言和出站安全；不按
某次模型回答的具体措辞打分。

评测命令使用 Gateway 调用的同一个 `GeneralAgentTurnCommand → AgentTurnRuntime` 进程合同。
除 `evidence_follow_up` 外，每个 case 创建隔离 thread。证据追问需要一个已经完成并发布的
thread，通过 `--completed-thread-id` 显式提供，评测只允许读取它，不允许创建新 BI task。

```bash
set -a
source .env
set +a
unset OPENAI_API_KEY
.venv/bin/python -m evals.general_agent_runtime.run_local \
  --database-url "$WAJE_RUNTIME_DATABASE_URL" \
  --completed-thread-id <completed-analysis-thread> \
  --output evals/general_agent_runtime/results/latest.json
```

缺少 DeepSeek 配置、缺少完成态 thread、访问 `api.openai.com`、动作或工具不匹配、客户语言
漂移、checkpoint 不闭合，都会令命令以非零状态退出。`results/baseline-20260721.json` 保留
修复前基线，供对照使用。
