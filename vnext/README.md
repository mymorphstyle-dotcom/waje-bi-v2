# WAJE BI Agent vNext

`vnext/` 是 WAJE BI Agent 新系统的独立应用实现根目录。Gate 0 隔离边界、Gate 1
权威/存储合同和 Gate 2 单主 Agent runtime 已建立。

## Day 0 边界

- 应用生产代码、合同、迁移、测试、eval、ops 和运行入口都在本目录内。
- GitHub provider 强制位于仓库根 `.github/` 的文件仅作为最小 deployment projection；
  允许集合与精确 hash 由 `ops/github/workflow-authority-policy.json` 拥有。
- 历史 `bi_agent/`、前端、contracts、runtime SQL、tests、fixtures 和 build manifests 只供
  调查参考。
- 本目录不 import、调用、读取或发布历史实现。
- PostgreSQL schema 为 `waje_vnext`，环境变量前缀为 `WAJE_VNEXT_`，Python namespace 为
  `waje_vnext`。
- `tools/isolation-policy.json` 与 `tools/verify_isolation.py` 机械验证边界。

## 本地命令

从本目录执行：

```bash
uv sync --frozen --no-install-project --python 3.12.13
npm ci
npm run check
npm run test:postgres
npm run test:postgres:gate2
npm run test:postgres:gate3.4
npm run test:postgres:gate3.5
```

`test:postgres:*` 使用一次性 PostgreSQL 17 容器验证 migration、事务回滚和
PostgreSQL adapter；执行机器需要 Docker。G3.5 的标准入口为
`npm run test:postgres:gate3.5`。

verifier 会：

1. 扫描可执行 source、SQL 和 dependency manifests；
2. 检查 symlink、Python import 与 path dependency；
3. 只复制本目录与 policy 列出的根级 `.github/` projection 到临时空 workspace；
4. 在临时 workspace 中重新创建 Python 3.12.13 virtualenv 并执行 `npm ci`；
5. 校验 JSON Schema 生成的 TypeScript bindings 与 GitHub deployment projection；
6. 在该 virtualenv 中 build wheel、compile、运行 unit tests 和 health smoke。

单独运行 bootstrap：

```bash
PYTHONPATH=services/analysis_core/src .venv/bin/python -m waje_vnext health
```

## 服务边界

- `apps/workbench/`：TypeScript Chat + Analysis + Workflow UI 与 customer-safe gateway。
- `services/analysis_core/`：Python Primary Business Analysis Agent、controller、capability、
  trust 与 projection。
- `contracts/`：TypeScript/Python 共享的版本化 domain/API/event/semantic schemas。
- `storage/migrations/`：独立 PostgreSQL migration ledger。
- `tests/`、`evals/`：只验证 vNext 当前合同。
- `ops/`、`tools/`：独立运行、构建、验证与发布入口。

Python baseline 为 3.12.13 toolchain、`requires-python >=3.12` 和项目内 `.venv`。
Gate 1 提供五类权威对象、typed actions、ContextPacket、event journal、durable case
mailbox、operation identity、runtime persistence envelopes、PostgreSQL adapter 与
migration。Gate 2 提供 WAJE-owned durable async controller、typed Primary Agent job、
checkpoint/resume、authority epoch fence、job lease/outbox、effect retry 和 `ask_user`
中断。command ingress 在短事务内持久化 mailbox + journal + controller wake，耗时 LLM
与 effect 在事务外运行，authority admission 回到 case-scoped 串行提交通道。

Gate 3 G3.1–G3.5 当前已实现 measurement authority、durable model/effect saga、
obligation scheduler、Plan/QueryBinding 连续性、capability result T1/T2 Evidence
admission、provisional Answer precheck、settlement precondition 与 journal-driven
Workflow 四轴投影。该状态只代表 local implementation complete；G3.E0 formal admission
继续派生 `deny_g3_1`。当前 conformance realm 可用于合同验收；production Evidence、
settled publication 和 delivered Workflow 继续 fail closed，分别等待 Gate 4 与 Gate 5。
`run_sensitivity` 在 selected sensitivity identity 进入 sealed dispatch/result 合同前也
保持 fail closed。Workflow projector 直接消费 durable journal，通过 cursor receipt 与
head CAS 恢复，不建立第二套 projection outbox authority。

G3.6.0 已建立 execution/attempt/trace/model/relation/hard-check/cell/suite 运行权威，并完成
typed/open-world corpus epoch：144 个 base 与 12 个 replacement claim targets 绑定 13 类
`ClaimTargetKind`；business-world independence 从 outcome authority refs 派生；authored
design 只作非穷尽示例。execution-universe compiler 当前从 36 个 Episodes 派生 156 个 case
variants、1,172 个 exact coordinates 和 2,011 个 Episode relation groups；201 个 paraphrase
authority slots 与 38 个 operator scenarios（19 类 × 至少 2 个独立业务世界）仍待 author/review，所以真实 provider lanes、
protected execution 和 full matrix 尚未开放。
完整逻辑部署边界见 `services/README.md`。Workbench 从 Gate 6 完成产品验收。

真实 provider smoke 只读取 `WAJE_VNEXT_LLM_` 前缀配置：

```bash
npm run test:provider:gate2
```
