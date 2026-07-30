# WAJE BI Agent vNext

`vnext/` 是 WAJE BI Agent 新系统的独立实现根目录。Gate 0 隔离边界、Gate 1
权威/存储合同和 Gate 2 单主 Agent runtime 已建立。

## Day 0 边界

- 生产代码、合同、迁移、测试、eval、ops 和发布入口都在本目录内。
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
```

verifier 会：

1. 扫描可执行 source、SQL 和 dependency manifests；
2. 检查 symlink、Python import 与 path dependency；
3. 只复制本目录到临时空 workspace；
4. 在临时 workspace 中重新创建 Python 3.12.13 virtualenv 并执行 `npm ci`；
5. 校验 JSON Schema 生成的 TypeScript bindings；
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
Gate 1 提供五类权威对象、typed actions、ContextPacket、event journal、runtime
persistence envelopes、PostgreSQL adapter 与 migration。Gate 2 提供 WAJE-owned
controller、typed Primary Agent provider、checkpoint/resume、lease、outbox、effect retry
和 `ask_user` 中断。Workbench 从 Gate 6 完成产品验收。

真实 provider smoke 只读取 `WAJE_VNEXT_LLM_` 前缀配置：

```bash
npm run test:provider:gate2
```
