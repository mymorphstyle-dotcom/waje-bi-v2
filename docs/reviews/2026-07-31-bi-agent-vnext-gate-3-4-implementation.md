# WAJE BI Agent vNext G3.4 实施审查

> 日期：2026-07-31
> 分支：`codex/gate3-4-plan-query-continuity`
> 基线：`origin/main@28c65b03`
> 状态：Local implementation complete；三路对抗审查 Blocking=0、Major=0

## 1. 结论

G3.4 已关闭 accepted AnalysisFrameRevision 到 accepted WorkPlanRevision、immutable
QueryBindingEnvelope 和 conformance logical execution 的权威连续性：

```text
accepted QuestionRevision
  -> accepted AnalysisFrameRevision
  -> admitted MeasurementResolutionOutcome
  -> immutable ResolvedEvidenceObligation
  -> accepted WorkPlanRevision + PlanAdoptionRecord
  -> immutable QueryBindingEnvelope
  -> ConformanceExecutionSpec
  -> LogicalExecutionAttempt
```

当前实现仍受 G3.E0 `deny_g3_1` 约束。它不能生成 production Evidence、settled Answer、
completed Workflow 或 protected held-out 结果。G3.5 尚未开始。

## 2. 已交付合同

- `WorkPlanRevision` 显式采用每个 estimand 唯一的 admitted resolution outcome；
- obligation identity 覆盖
  `(estimand, evidence requirement, required evidence type)`，多证据槽可合并、拆分、
  串行或并行安排；
- executable obligation 必须有一个 QueryBindingEnvelope，typed boundary/blocked
  obligation 禁止生成查询绑定；
- QueryBindingEnvelope exact 绑定 Frame hash、measurement node refs、actual windows、
  timezone/business cutoff、snapshot/release、exposure facts、requirement composition、
  strength、scope、contradiction/boundary/falsification/reversal；
- frozen、versioned `CapabilityIntentContract` registry 约束 obligation disposition、
  Evidence type、所需 measurement authority 和 typed action kind，registry
  version/hash 进入 PlanAdoptionRecord；
- Plan、query bindings、adoption、accepted head 和 journal 在同一短事务内提交；
- conformance technical retry 保持 logical execution identity，并允许无关 sibling
  obligation/Evidence/contradiction 状态独立推进；
- correction、Question、Frame 或 accepted Plan authority 改变会 fence 旧执行；
- obligation worker success 只投影 `execution_succeeded`，Evidence admission 由 G3.5
  继续负责。

## 3. 关键失败模式与关闭方式

| 失败类型 | 关闭方式 |
|---|---|
| Plan 遗漏、重复或跨 outcome 拼接 obligation | compiler 完整 closure、唯一 owner、每 estimand 唯一 outcome，repository exact replay |
| capability intent 换路线 | versioned registry 同时约束 disposition 与 ActionKind |
| boundary 被伪装成空查询 | boundary task 保留 closure，QueryBinding 数量固定为零 |
| capability/outbox 绕过 accepted action | generic effect enqueue 重新 admission，并 exact 绑定 action、receipt、journal、Plan、task、query |
| generic outbox 注入 obligation job | obligation job 只能经 scheduler 的原子 dispatch admission |
| correction 与 outbox/Plan commit 竞态 | mailbox authority commit fence 与 case head CAS |
| 同 Frame 多 resolution 隐式选择 | proposal obligation 集合显式选择 outcome；old/new 同时输入或 crossed pairing 均拒绝 |
| retry 被 sibling state 误杀 | sealed business-authority comparator 排除无关可变状态，保留 mailbox/question/frame/plan fence |
| execution success 提前声称 Evidence 完成 | execution 与 Evidence satisfaction 分离 |
| PostgreSQL Plan 提交局部成功 | 单事务写入；并发 CAS 和中途异常注入证明回滚 |
| migration DDL 与 ledger 分开提交 | migration 文件不含内部事务边界；004/005 失败注入证明 DDL 与 ledger 一起回滚 |
| logical retry 仍要求旧 case head | PostgreSQL commit fence 支持同业务权威的 sibling head 推进，随后重检 mailbox/question/frame/plan |
| duplicate adoption 改写事件因果 | replay 必须与已持久化 journal operation provenance exact 一致 |
| 多 executable estimand 串用 outcome/window | 逐 estimand 独立 resolution、actual windows 与 binding oracle；交叉 mutation 全部拒绝 |
| Evidence cardinality 被 bool/float/零值弱化 | Frame 与 persisted binding 同时执行严格非布尔正整数和 slot 上界校验 |
| spec/attempt 系统 ID 被调用方伪造 | repository 从 canonical input 重算全部派生 ID；retry 与 persisted prior attempt exact 比较 |
| 内存 adapter 接受同 QueryBinding 第二份 spec | 与 PostgreSQL 对齐 logical execution / QueryBinding 唯一索引 |

## 4. 测试结果

### 4.1 当前合同与独立构建

- Python 单元/合同测试：340 个收集并通过，其中 23 个环境型 PostgreSQL 测试在无
  `WAJE_VNEXT_DATABASE_URL` 的普通单元入口按设计 skip；
- contract schema、TypeScript binding 与 measurement identity：通过；
- Gate 3 eval corpus、views、readiness manifest consistency：通过；
- G3.E0 派生状态仍为 `deny_g3_1`，未被本地实现改写；
- clean-copy 隔离：Python 3.12.13，340 个测试通过，wheel build、install、health
  smoke、Node manifest、import、symlink、legacy reference 扫描全部通过；
- clean-copy wheel：
  `waje_bi_agent_vnext_analysis_core-0.0.0-py3-none-any.whl`，
  `requires-python >=3.12`。

### 4.2 临时 PostgreSQL

所有 PostgreSQL 验收只在 Docker tmpfs 临时数据库执行：

- Gate 1：3/3；
- Gate 2：6/6；
- Gate 3.4：14/14。

G3.4 PostgreSQL 覆盖：

- all-boundary 与 executable/boundary mixed Plan 持久化；
- 多 evidence slots；
- 两个合法 Plan 并发争抢同一 head，只有一个 CAS 成功；
- exact duplicate adoption 幂等、同 identity 不同 payload 冲突；
- Plan/query rows 后、adoption insert 前注入异常，Plan/head/journal 全部回滚；
- correction fence、outbox row-lock race、stale logical attempt；
- terminal case fence；
- 004/005 migration ledger 写入失败时，新增 DDL 和 ledger 均不可见；
- Evidence、Answer 和 obligation sibling state 推进后，同一业务权威下的 logical retry
  仍可提交；Question、Frame、Plan 或 mailbox epoch 改变继续 fail closed；
- duplicate Plan adoption 的 journal operation provenance 必须 exact replay；
- forged conformance logical/spec/attempt ID 被 repository 重算拒绝；
- 同 QueryBinding 第二份 canonical spec 被唯一约束拒绝；
- retry 在 canonical attempt ID 重算后篡改 prior/task/query hash/spec hash，仍被拒绝且
  不留下 attempt row；
- database-owned disposable reset token 拒绝错误 token。

### 4.3 通用测量与计划向量

- 跨月窗口 identity、actual date range、month offset、timezone、business cutoff、
  snapshot/release、calendar coverage 和 exposure values；
- strength、scope、composition、minimum count/strength、boundary policy、
  contradiction、falsification/reversal、kind-specific measurement refs；
- 非默认 Frame 字段逐项投影到 QueryBindingEnvelope 的独立正向 oracle；
- `AT_LEAST` 的 `minimum_count` 不能超过声明的 evidence slots；
- 两个 executable estimand 使用不同月份 anchor，共享 requirement 仍保持独立
  outcome/obligation/binding/actual-window identity；
- Frame requirement 与 QueryBinding binding 都拒绝 bool、float、0、负数和越界
  `minimum_count`；
- grouped、split serial、split parallel、forward-declared dependency、multi-level DAG；
- cycle、unknown dependency、duplicate obligation owner；
- `ALL`、`ANY`、`AT_LEAST`；
- all-boundary 零 dispatch，mixed topology 只选择 executable obligation；
- same Frame 新 resolution 的显式采用、歧义拒绝和 crossed outcome/obligation 拒绝。

这些测试验证可接受结果空间和权威不变量，没有固定 LLM 必须选择某个窗口长度、任务数量、
任务顺序或 capability 路线。

## 5. PostgreSQL 测试清理事故

### 5.1 已确认事实

在 disposable database guard 加入前，一次并行 PostgreSQL 验收误用了项目 `.env`
指向的 `127.0.0.1:15432/waje_bi_runtime`：

- 发生时间：2026-07-31 14:05:01–14:05:07.396（Asia/Shanghai）；
- 操作：动态读取 `waje_vnext` schema 表清单，并对除 `schema_migrations` 外的 52 张表
  执行 `TRUNCATE ... RESTART IDENTITY CASCADE`；
- 按测试调用路径推导，清理最多执行 21 次；
- 执行前后没有采集行数，因此原始行数和实际丢失量无法确认；
- 只读事后检查看到 53 张表，其中 15 张表有一组明显的 G3.4 测试 fixture，38 张为空；
- `archive_mode=off`，未确认存在可用备份、PITR 或 vNext WAL 恢复点。

当前不能承诺原数据可恢复，也不能声称没有数据损失。事故发现后停止了对该数据库的写操作，
后续 PostgreSQL 验收全部改用临时 Docker 数据库。

### 5.2 修复

测试清理现在需要三重条件：

1. `WAJE_VNEXT_ALLOW_TEST_DATABASE_RESET=1`；
2. runner 生成的随机 `WAJE_VNEXT_TEST_DATABASE_RESET_TOKEN`；
3. 当前数据库 `public.waje_vnext_disposable_test_database_marker` 中存在同一 token，并
   exact 绑定 `current_database()`。

三个 acceptance runner 只在新建 Docker tmpfs PostgreSQL 中创建 marker。项目 `.env`、
复制 DSN 或单独设置清理开关无法满足数据库自身的授权证明。错误 token 测试已通过。

## 6. 组合对抗复审

三路子智能体在每轮修复后重新执行只读复审：

- 权威合同与 capability intent；
- PostgreSQL/CAS/事务与清理安全；
- 真实问题导向的测试空间与 Gate 分层。

最终结论：

- 权威合同：Blocking=0，Major=0；
- 存储/CAS/事务：Blocking=0，Major=0；
- 测试设计：Blocking=0，Major=0。

审查实际打开并关闭了 capability registry 可变性、sensitivity applicability、
migration 事务、logical retry sibling state、operation provenance、多 executable
estimand 配对、Evidence cardinality、spec/attempt 派生 ID、prior retry sealed input 和
双 adapter parity 等通用问题。G3.4 达到关闭要求。

## 7. 后续边界

G3.5 承接 CapabilityResultEnvelope、immutable EvidenceRecord、Evidence admission、
EvidenceUseBinding、obligation satisfaction、provisional claim precheck、
SettlementPreconditionReport 和 Workflow 四轴 projection。

G3.6 承接 13 类 ClaimTargetKind 的真实自然语言选择、开放 measurement design、
多种合理 Plan 的 Reviewer 评分和完整 Episode 行为矩阵。G3.4 的 deterministic compiler
保持 target-kind agnostic，不把真实用户问题编译成固定模板。
