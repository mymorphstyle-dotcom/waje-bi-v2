# WAJE BI Agent vNext

`vnext/` 是 WAJE BI Agent 新系统的独立实现根目录。当前为 Gate 0 bootstrap。

## Day 0 边界

- 生产代码、合同、迁移、测试、eval、ops 和发布入口都在本目录内。
- 历史 `bi_agent/`、前端、contracts、runtime SQL、tests、fixtures 和 build manifests 只供
  调查参考。
- 本目录不 import、调用、读取或发布历史实现。
- PostgreSQL schema 为 `waje_vnext`，环境变量前缀为 `WAJE_VNEXT_`，Python namespace 为
  `waje_vnext`。
- `tools/isolation-policy.json` 与 `tools/verify_isolation.py` 机械验证边界。

## Gate 0 命令

从本目录执行：

```bash
uv sync --frozen --no-install-project --python 3.12.13
.venv/bin/python tools/verify_isolation.py
```

verifier 会：

1. 扫描可执行 source、SQL 和 dependency manifests；
2. 检查 symlink、Python import 与 path dependency；
3. 只复制本目录到临时空 workspace；
4. 在临时 workspace 中重新创建 Python 3.12 virtualenv；
5. 在该 virtualenv 中 build wheel、compile、运行 unit tests 和 health smoke。

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

Gate 0 的 Python baseline 为 3.12.13 toolchain、`requires-python >=3.12` 和项目内
`.venv`。业务权威对象从 Gate 1 开始实现，Workbench 从 Gate 6 完成产品验收。
