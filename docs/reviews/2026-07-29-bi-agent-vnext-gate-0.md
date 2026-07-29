# WAJE BI Agent vNext Gate 0：现状解剖与 Day 0 隔离

## 状态

- Gate：0
- 日期：2026-07-29
- 状态：Complete
- Entry interview：本 Gate 无需用户决策
- 基线 commit：`7c7009a5efa6f7be14695bda81999d08dcdadd8c`
- 工作分支：`codex/bi-agent-vnext`

## 1. 现状解剖

### 1.1 历史实现规模

| 区域 | 调查结果 |
|---|---|
| Python production | `bi_agent/` 下 133 个 `.py` 文件 |
| Python tests | `tests/` 下 171 个 `test_*.py` 文件 |
| Frontend | `app/`、`components/`、`lib/` 共 46 个文件 |
| Contracts | `contracts/` 共 33 个文件 |
| Tools | `tools/` 共 38 个文件 |
| 总参考面 | production、frontend、contracts、tests、tools、evals 共 454 个文件 |
| 旧 runtime schema | `tools/runtime/conversation-runtime.sql` 共 3,576 行，schema 为 `waje_runtime` |

这些数字只用于确定隔离面，不形成 vNext 工作量估算或复用承诺。

### 1.2 历史入口

| 类型 | 历史入口 |
|---|---|
| Web | 根级 Next.js `package.json`：`dev`、`build`、`start` |
| Worker | `python -m tools.runtime.recover_run_dispatches` |
| Release | `python -m tools.runtime.build_production_release` |
| UI tests | 根级 Playwright |
| Python runtime | 根级 `requirements.txt`、`bi_agent/` |
| PostgreSQL | `tools/runtime/conversation-runtime.sql`、`waje_runtime` |
| ClickHouse | 根级 compose、`ops/clickhouse/`、`tools/data/` |
| Contracts | 根级 `contracts/`，包括 factor SSOT |

### 1.3 可保留的参考价值

- 业务 metric、dimension、factor 与数据源线索。
- 历史失败问题和真实用户措辞。
- SQL 安全、snapshot/release、evidence、claim、recovery 的事故经验。
- 双栏 Workbench 与 customer-safe projection 的交互经验。
- ClickHouse/PostgreSQL 本地运行经验。

所有价值都需要在 vNext 当前合同下重新表达。旧 import、旧 schema、旧 assertion、旧 API 和
旧 UI component 不进入新系统。

## 2. Day 0 新边界

| 项目 | vNext 值 |
|---|---|
| 实现根目录 | `vnext/` |
| Python namespace | `waje_vnext` |
| Python toolchain | 3.12.13 virtualenv；project minimum `>=3.12` |
| Python service | `waje-bi-agent-vnext-analysis-core` |
| Node workspace | `waje-bi-agent-vnext` |
| PostgreSQL schema | `waje_vnext` |
| 环境变量前缀 | `WAJE_VNEXT_` |
| Workbench package | `@waje-vnext/workbench` |
| 隔离策略 | `vnext/tools/isolation-policy.json` |
| 隔离 verifier | `vnext/tools/verify_isolation.py` |

## 3. Gate 0 交付物

- [x] 持久化开发计划。
- [x] 对抗式计划自审。
- [x] 当前实现与入口清单。
- [x] 独立 Python/Node manifests。
- [x] Python 3.12.13 toolchain 与项目 virtualenv 合同。
- [x] 最小 Python compile/test/run skeleton。
- [x] machine-readable forbidden dependency policy。
- [x] clean-copy deletion-independence verifier。
- [x] verifier 执行证据。
- [x] Gate 0 exit criteria 回写与状态关闭。

## 4. 验收证据

### 4.1 Evidence manifest

| 字段 | 值 |
|---|---|
| Source baseline | `7c7009a5efa6f7be14695bda81999d08dcdadd8c` |
| Source branch | `codex/bi-agent-vnext` |
| vNext tree SHA-256 | `09af89931e6b476ffae8b4d01ac37f3faeb6d890c4f72abbec6da5ed6c1381f5` |
| Toolchain | Python 3.12.13；uv 0.11.14；Node 26.0.0；npm 11.12.1 |
| Isolation policy | version 1 |
| Findings | 0 |
| Reviewer disposition | Gate 0 blocking findings cleared |

### 4.2 Positive commands

| Command | Result |
|---|---|
| `uv lock --python 3.12.13` | exit 0；6 packages resolved |
| `uv sync --frozen --no-install-project --python 3.12.13` | exit 0；project `.venv` created |
| `.venv/bin/python tools/verify_isolation.py` | exit 0；7 checks passed |
| clean-copy `uv sync --frozen --no-install-project --python 3.12.13` | exit 0；临时 `.venv` created |
| clean-copy `.venv/bin/python -m build --wheel --no-isolation` | exit 0 |
| clean-copy `.venv/bin/python -m compileall` | exit 0 |
| clean-copy `.venv/bin/python -m unittest discover` | exit 0；3 tests passed |
| clean-copy `.venv/bin/python --version` | exit 0；`Python 3.12.13` |
| clean-copy `.venv/bin/python -m waje_vnext health` | exit 0；status `ok` |

### 4.3 Negative command

使用宿主 Python 3.9.6 运行 `python3 tools/verify_isolation.py`，命令按预期 exit 1，
`python_toolchain` 为 failed，错误明确要求 Python 3.12+。clean-copy 阶段没有执行。

### 4.4 Artifact

| Artifact | 验证 |
|---|---|
| `waje_bi_agent_vnext_analysis_core-0.0.0-py3-none-any.whl` | SHA-256 `c9ef50bd4e555525ab9c0c37b68be4a956a23d32e7a0b8b83ed40078b3dfb4f0` |
| Wheel metadata | `Requires-Python: >=3.12` |
| Wheel top-level content | 只有 `waje_vnext` 与当前 dist-info |
| Wheel legacy scan | 0 findings |

临时 wheel 用于 clean-copy 验收，命令结束后随 temporary workspace 清理；文件名、hash 和
metadata 保留在 verifier 输出与本报告中。

## 5. Exit criteria

| Criteria | 结果 |
|---|---|
| vNext executable surface 无旧 import/path/schema/runtime dependency | Pass |
| machine-readable policy 覆盖 source、manifest、SQL、symlink、dynamic Python literal 与 wheel | Pass |
| 独立 Python/Node package manifests 和 lockfiles | Pass |
| Python 3.12 virtualenv build/test/run | Pass |
| clean-copy deletion independence | Pass |
| 低版本 fail closed | Pass |
| 旧系统删除顺序可执行 | Pass |
| Gate 0 adversarial blocking findings | 0 open |

## 6. 后续边界

Gate 0 只证明 Day 0 隔离、toolchain 与最小 build/test/run。五类权威对象、存储 schema、
typed actions、ContextPacket 和 event journal 在 Gate 1 实现。Gate 1 入口需先完成访谈判断。
