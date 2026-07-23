# WAJE Standard Pack v1

`cases.jsonl` 是 General Agent Runtime 的单一版本化评测目录，遵循
`case.schema.json`。当前目录固定 48 个 canonical scenario：24 个业务分析、12 个 runtime、
4 个安全、8 个浏览器体验 case。

统一 runner `run_local.py` 调度三种 adapter：

- `agent_live`：真实 `GeneralAgentTurnCommand -> AgentTurnRuntime -> DeepSeek`；
- `pytest`：确定性合同、故障注入、Provider 和持久化测试；
- `playwright`：真实浏览器状态、交互、客户安全投影与可读性测试。

完整合同见 `docs/specs/general-agent-runtime/standard-pack-v1.md`。人工回答质量审核只生成
`pending_human_review`，不加入 hard pass/fail，不触发 writer retry、自动改写、撤回或发布
阻断。决策导向 case 要求人工作审核可执行建议；概念、能力和纯证据追问不强制建议。

## 只校验目录

无需数据库和模型密钥：

```bash
.venv/bin/python -m evals.general_agent_runtime.run_local \
  --profile release \
  --validate-only
```

## 执行 deterministic smoke

```bash
.venv/bin/python -m evals.general_agent_runtime.run_local \
  --profile smoke \
  --adapter pytest \
  --output evals/general_agent_runtime/results/latest-deterministic.json
```

## P4 全因子调查包

P4 使用独立标准包，不占用或重跑 Case B：

```bash
.venv/bin/python -m evals.general_agent_runtime.run_local \
  --cases evals/general_agent_runtime/p4-cases.jsonl \
  --profile smoke \
  --validate-only
```

确定性 P4 门禁：

```bash
unset OPENAI_API_KEY
.venv/bin/python -m evals.general_agent_runtime.run_local \
  --cases evals/general_agent_runtime/p4-cases.jsonl \
  --profile smoke \
  --adapter pytest \
  --output evals/general_agent_runtime/results/p4-latest-deterministic.json
```

该包覆盖全因素变化解释、拉新注册首充漏斗、充值档位、事件边界、分支失败恢复、受控
调查 Agent 并行与无发布权威、越界建议局部拒绝、Workbench/客户投影隔离和大陆 Provider
出站。业务质量继续进入人工 advisory review，不会自动改写或撤回首次交付。

## P6 支付终态与完整链路验收包

P6 使用独立标准包，覆盖真实支付终态调查、连续追问、证据挑战、最终状态权威、
大陆 Provider 唯一出站、SDK 类型隔离和前端可读性：

```bash
unset OPENAI_API_KEY
.venv/bin/python -m evals.general_agent_runtime.run_local \
  --cases evals/general_agent_runtime/p6-cases.jsonl \
  --profile smoke \
  --validate-only
```

真实 DeepSeek case 使用同一命令并增加 `--adapter agent_live`、数据库 URL 和结果输出；
回答完整度、深度、可读性及行动性继续进入 `human_advisory`，不改变首次 publication。

## P7 回答完整性补全包

P7 使用独立标准包验证 accepted obligation 的三层闭环：证据缺口走现有 `PlanPatch`，已封存材料
的漏写走一次 additive narrative completion revision，增补失败继续交付首稿并附客户安全边界。
追问还验证工具结果已持久化后的摘要恢复，以及工具选择模型暂时不可用时的完整 publication
保全。质量 verifier 的主观发现继续只进入人工审阅：

```bash
unset OPENAI_API_KEY
.venv/bin/python -m evals.general_agent_runtime.run_local \
  --cases evals/general_agent_runtime/p7-cases.jsonl \
  --profile smoke \
  --validate-only
```

确定性 P7 门禁：

```bash
unset OPENAI_API_KEY
.venv/bin/python -m evals.general_agent_runtime.run_local \
  --cases evals/general_agent_runtime/p7-cases.jsonl \
  --profile smoke \
  --adapter pytest \
  --output evals/general_agent_runtime/results/p7-latest-deterministic.json
```

真实 case 复用 P6 支付终态历史失败措辞，检查首答的 obligation coverage、正文外已发布材料的追问
读取和支付过程证据边界。洞察深度、行动性和可读性仍为 `human_advisory`。

## 执行真实 DeepSeek case

```bash
set -a
source .env
set +a
unset OPENAI_API_KEY
.venv/bin/python -m evals.general_agent_runtime.run_local \
  --profile smoke \
  --adapter agent_live \
  --database-url "$WAJE_RUNTIME_DATABASE_URL" \
  --fixture-map evals/general_agent_runtime/fixture-map.local.json \
  --output evals/general_agent_runtime/results/latest-live.json
```

`fixture-map.local.json` 是本地文件，键为 case 中的 `completedThreadKey`，值为该 case 独占的
完成态 thread ID。不同 key 不得映射到同一个 thread。新分析 case 由 runner 创建隔离 thread。

`release` profile 中，`riskTier=critical` 的真实 DeepSeek case 固定执行三次；任何一次硬断言
失败都会令该 case 失败。缺少 DeepSeek 配置、访问 `api.openai.com`、fixture 不完整、动作或
工具不匹配、publication 未收敛、证据/数字越界都会令命令以非零状态退出。

## 执行浏览器 case

```bash
.venv/bin/python -m evals.general_agent_runtime.run_local \
  --profile smoke \
  --adapter playwright \
  --artifact-dir output/playwright/standard-pack/smoke \
  --output evals/general_agent_runtime/results/latest-browser.json
```

显式 `--case-id` 会越过 profile 过滤，便于单 case 重跑；可以重复传入。runner 报告 schema
固定为 `waje-standard-pack-run.v1`，不会记录 API key、原始 Provider payload、隐藏推理或
原始数据行。Playwright case 会把 `WAJE_VISUAL_EVIDENCE_DIR` 绑定到 case 独立目录，并在
报告的 `artifactRefs` 中登记截图；live turn 记录 `durationSeconds` 和
`recoveryCycleCount`，用于把 correctness、stability 和 latency 分开评价。
