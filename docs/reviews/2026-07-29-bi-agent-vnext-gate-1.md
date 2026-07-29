# WAJE BI Agent vNext Gate 1：权威对象与存储合同

## 状态

- Gate：1
- 日期：2026-07-29
- 状态：Complete
- Entry interview：用户确认 `InvestigationCase` 为第五类权威对象
- 其余用户决策：本 Gate 无需第二项用户决策
- Source branch：`codex/bi-agent-vnext`
- Gate 0 commit：`93464d83`

## Entry shared understanding

`InvestigationCase` 只保存稳定 identity、lifecycle 和 accepted
`AnalysisFrameRevision` / `WorkPlanRevision` / `AnswerVersion` heads 的 CAS 指针。业务内容
只存在于 immutable revision/version。`EvidenceRecord` 按 ID、payload hash、Frame、Plan 和
task binding 保持不可变。

低风险业务推断可以写入 `DecisionRecord` 并进入 accepted revision。会改变业务结论、
baseline、时间语义、固定敏感输出、数据访问安全、claim 强度或显著执行成本的歧义进入
`ask_user`，等待用户选择。

## 已交付

### 权威与从属对象

- 五类权威对象：
  - `InvestigationCase`
  - `AnalysisFrameRevision`
  - `WorkPlanRevision`
  - `EvidenceRecord`
  - `AnswerVersion`
- 从属记录：
  - `InterpretationRecord`
  - `DecisionRecord`
  - append-only `ReviewerObjection` revision chain
- `AnalysisFrameRevision` 集中定义 estimand、观察单位、分子、分母、exposure、
  comparison、assumptions、alternatives、falsification、reversal、success 与 stop
  conditions。
- `WorkPlanRevision` 只承载业务调查任务；任务依赖要求引用已知 task 且保持无环。
- `AnswerVersion` 支持 provisional/settled；settled fingerprint 精确绑定 Frame、Plan、
  claims 和 verifier policy。

### Agent 与 runtime contracts

- 十类 Primary Agent typed actions：
  `revise_frame`、`revise_plan`、`inspect_semantics`、`run_probe`、
  `call_capability`、`run_sensitivity`、`record_interpretation`、`ask_user`、
  `propose_answer`、`stop`。
- admission 先检查 case/head/lifecycle，再检查 accepted Frame/Plan 与 task binding。
- capability call 与 sensitivity retry 不移动 Frame/Plan head。
- `ContextPacket` 从 accepted heads、event cursor、evidence index、Reviewer objection 和
  Decision refs 确定性构建；构造器会重算内容 hash。
- event journal 使用 case 内单调 cursor、全局 event ID 和 immutable
  customer-safe projection。
- runtime persistence envelopes：
  `ActionReceipt`、`CheckpointRecord`、`OutboxMessage`。三者都要求 versioned schema ref、
  content hash 和 immutable payload。

### 存储与跨语言合同

- `waje_vnext` PostgreSQL schema v1 与独立 migration ledger。
- authority repository port、in-memory conformance adapter、psycopg PostgreSQL adapter。
- accepted head 使用 `head_version` CAS；跨进程并发写由 PostgreSQL row lock 串行化。
- Frame 变更会失效 accepted Plan/Answer heads，同时 Plan/Answer 的 case-wide 历史版本链
  继续递增并保留 prior pointer。
- authority、evidence、subordinate record、context、receipt、checkpoint、outbox 和 journal
  都有 append-only 数据库约束。
- versioned JSON Schemas：
  authority、actions、ContextPacket、runtime state、journal entry。
- Python runtime domain types执行业务 invariants；TypeScript bindings 从 JSON Schema
  生成。`npm run check:contracts` 阻止生成物漂移。

## 对抗式自审

| ID | 攻击面 | 发现 | 修正与验证 | 状态 |
|---|---|---|---|---|
| G1-AR-01 | WorkPlan 可执行性 | 只校验未知依赖时，两个合法 task 可形成环 | 增加全图无环校验和 cyclic plan regression | Resolved |
| G1-AR-02 | settled 可信度 | 任意 64 位值可伪装 settlement fingerprint | fingerprint 由 Frame、Plan、claims、verifier policy 规范化重算并精确比较 | Resolved |
| G1-AR-03 | Context 注入 | 直接构造 `ContextPacket` 可提交伪造 hash | `__post_init__` 对完整 authority projection 重算 hash | Resolved |
| G1-AR-04 | Reviewer 覆盖写 | 单条 objection 状态更新会抹除原始异议 | 改为 objection key + revision number + prior pointer 的 append-only chain | Resolved |
| G1-AR-05 | revision 追溯 | Frame 失效 Plan/Answer head 后，版本推导会错误回到 1 | 从 case 全历史读取 latest Plan/Answer；跨 Frame 继续 revision/version chain | Resolved |
| G1-AR-06 | PostgreSQL 事务 | public read 遗留隐式事务，后续写入停留在 savepoint 并阻塞其他连接 | public read 使用显式 transaction scope；跨连接 runtime envelope 写入验收通过 | Resolved |
| G1-AR-07 | typed action 跨语言一致性 | conditional schema 生成的 TypeScript payload 失去 kind correlation | action schema 改为十分支 discriminated union，重新生成 TypeScript types | Resolved |
| G1-AR-08 | crash boundary | 只有 journal 表，缺少 checkpoint/idempotency/outbox 的持久化 envelope | 增加三个 hash-bound runtime contracts、JSON Schema、表、外键和 append-only trigger | Resolved |
| G1-AR-09 | CAS 并发 | 单线程 stale test 无法证明跨连接串行化 | 两个独立 PostgreSQL connection 同时写 head；结果固定为一次 accepted、一次 stale | Resolved |
| G1-AR-10 | 删除独立性 | clean-copy 只验证 Python，未验证生成合同所需 Node graph | verifier 增加 `npm ci --ignore-scripts` 与 `check:contracts` | Resolved |

Gate 1 blocking finding：0。

## 验收证据

### 确定性与 schema tests

命令：

```bash
cd vnext
npm run check:contracts
npm run test:bootstrap
```

结果：

- TypeScript generated bindings：5 个文件，drift check passed。
- unittest discovery：37 tests，35 passed，2 个 PostgreSQL tests 在无 DSN 的普通入口中
  按合同 skip。
- 覆盖 authority invariants、deep immutability、action/payload binding、admission、
  Context hash、CAS、revision lineage、event idempotency、JSON Schema 与 migration
  structure。

### PostgreSQL 17 integration

命令：

```bash
cd vnext
npm run test:postgres
```

结果：2 tests passed。

- 使用唯一命名的临时 `postgres:17-alpine` 容器和 tmpfs data directory。
- migration 连续 apply 两次，checksum 相同。
- 完整执行 Case → Frame → Plan → Evidence → provisional Answer → Reviewer objection
  revisions → Frame 2 → Plan 2 → Evidence 2 → settled Answer 2。
- 验证跨连接 CAS、stale head、immutable ID、单调 cursor、runtime envelope 外键与
  append-only trigger。
- 测试完成后容器已删除。

Migration SHA-256：
`265186e31a6a03fdd1e1c31ed96f3cf58697065338d4aa97a67a9ed3a509d247`。

### clean-copy 删除独立性

命令：

```bash
cd vnext
npm run check
```

结果：passed。

- clean copy 只复制 `vnext/`。
- 使用锁定的 Python 3.12.13 重建 `.venv`。
- 使用 `npm ci --ignore-scripts` 重建 Node dependency graph。
- generated contract drift、wheel build、compile、unit/schema tests、Python version 和
  health smoke 全部通过。
- wheel：`waje_bi_agent_vnext_analysis_core-0.0.0-py3-none-any.whl`
- wheel `Requires-Python`：`>=3.12`
- wheel SHA-256：
  `fc8cdfc822c154700c9a43a18df1bc8526a9920e7e004de8d8d5d7226a9376dc`
- vNext tree SHA-256：
  `2acb05888ecddeabc610534ff5d4561aedcf44a7365464b24fdeedd218606343`
- legacy reference、legacy import、path dependency、symlink、artifact scan findings：0。

## Exit criteria

- [x] authority head mutation 经过 CAS；typed action admission contract 已固定。
- [x] EvidenceRecord 与 accepted revision 无原地修改路径。
- [x] capability/sensitivity retry 不创建 Frame/Plan revision。
- [x] measurement definition 只能进入 AnalysisFrameRevision；WorkPlan 无平行业务口径字段。
- [x] ContextPacket 可由同一持久化 authority projection 确定性重建并得到相同 hash。
- [x] event journal 支持幂等 append、case 内单调 cursor 和 customer-safe projection。
- [x] checkpoint、action idempotency 与 outbox persistence envelopes 已版本化并具备数据库
  hard boundary。
- [x] 对抗式自审 blocking finding 清零。
- [x] clean-copy 删除独立性验证通过。

## Gate 2 边界

Gate 2 实现 WAJE-owned controller、lease、action loop、checkpoint/resume 和 outbox
delivery。`ActionReceipt`、`CheckpointRecord`、`OutboxMessage` 将在同一个 PostgreSQL
unit of work 中与对应 journal event 原子提交。Reviewer 的风险触发矩阵、逐 claim
disposition 与 settled 发布判定在 Gate 5 完成。
