# WAJE BI Agent vNext G3.4：Plan 与逻辑查询连续性实施计划

> 日期：2026-07-31
> 状态：Local implementation complete；三路对抗审查 Blocking=0、Major=0
> 基线：`origin/main@28c65b03`
> 实现根：`vnext/`

## 1. Gate entry

### 1.1 已查明事实

- G3.2/G3.3 已合并，durable async runtime、measurement resolver、可信
  resolution admission 与 immutable obligation 已通过本地和 GitHub clean-copy
  验证。
- G3.E0 formal admission 继续为 `deny_g3_1`；development override 允许继续完成
  local implementation，但不得据此生成 production Evidence、settled Answer、
  completed Workflow 或 protected held-out 结果。
- 当前 `WorkTask` 未绑定 obligation，`WorkPlanRevision` 未声明采用哪些
  `MeasurementResolutionOutcome`，`accept_plan` 只检查当前 Frame 与 revision
  序列。
- 当前 `call_capability` 和 `run_sensitivity` 仍接受自由 `parameters`，可以在
  accepted Frame/Plan 之外形成平行业务口径。
- 当前没有 `QueryBindingEnvelope`、`ConformanceExecutionSpec`、Plan adoption
  proof 或 capability/tool logical retry identity。

### 1.2 访谈判断

本 Gate 无需用户决策。

G3.4 的业务边界、权威方向和 exit criteria 已在已批准的 Gate 3 计划中确定。本 Gate
只关闭已接受 Frame、可信 resolution、obligation、Plan、logical execution 之间的
连续性，不选择 Gate 4 capability 分类、不生成物理 QuerySpec/SQL、不进入
Evidence/Answer/Workflow。

若实施中发现下列情况，暂停对应分支并按 `$grill-me` 一次只问一个问题：

- 需要改变 Frame 中的 estimand、population、window、exposure、comparison、
  estimator、completion 或 claim ceiling；
- 需要让 Plan 丢弃或降低某个 Frame requirement；
- 需要提前确定 Gate 4 capability registry、SQL escape hatch 或生产数据源策略；
- 需要允许无 obligation 的业务任务成为 accepted WorkPlan authority。

## 2. 目标与边界

G3.4 建立以下单向、可重放、可持久化证明链：

```text
accepted QuestionRevision
  -> accepted AnalysisFrameRevision
  -> admitted MeasurementResolutionOutcome
  -> immutable ResolvedEvidenceObligation
  -> accepted WorkPlanRevision + PlanAdoptionRecord
  -> immutable QueryBindingEnvelope
  -> conformance-only ConformanceExecutionSpec
  -> LogicalExecutionAttempt
```

目标：

- Plan 为当前 Frame 的每个 estimand 显式采用一个 admitted resolution outcome，并只采用
  该 outcome 派生的 obligations；
- 每个 obligation 有且只有一个 accepted WorkTask closure owner；
- 每个 executable obligation 有且只有一个 logical query binding；
- boundary obligation 保留为业务边界，不伪造查询；
- Gate 4 physical compiler 消费 QueryBindingEnvelope，并按 envelope 中的 Frame
  identity/hash 加载 immutable Frame 节点；不能重新解释业务语义；
- conformance technical retry 重用同一 logical identity；通用 runtime effect retry
  重用同一 admitted Action 与 immutable outbox identity；
- 任何业务测量变化先创建 FrameRevision，再重新 resolution 和 Plan；
- Plan acceptance、adoption、query bindings、head CAS 与 journal append 在同一短事务
  内完成；conformance spec 只在该 Plan 已 accepted 且 authority snapshot 仍当前时单独
  记录。

不在本 Gate：

- capability registry 和具体 capability 实现；
- physical QuerySpec、SQL、ClickHouse 执行、cost/row/privacy guard；
- production capability result 或 EvidenceRecord；
- Answer、settlement、Reviewer claim review 或 Workflow projection；
- G3.E0 外部 admission、人工双审、calibration、held-out promotion。

## 3. 权威对象

### 3.1 ProposedWorkTask

LLM 的 `revise_plan` 输出使用非权威 `proposal_task_key` 表达任务和依赖。当前合同允许
LLM 提出业务目的、受治理 capability intent、obligation 分组和 task dependency；不能
签发 `task_id`、`query_binding_id`、`plan_revision_id` 或 logical execution ID。

### 3.2 WorkTask / WorkPlanRevision

系统 compiler：

- 根据 controller-issued `plan_revision_id` 与 `proposal_task_key` 生成稳定 `task_id`；
- 把 proposal dependency key 转成系统 task ID；
- 把 accepted obligation 分配写入 `obligation_ids`；
- 把 executable obligation 对应的 system-issued query binding ID 写入
  `query_binding_ids`；
- 在 `WorkPlanRevision` 中记录完整 `resolution_outcome_ids`。

每个 accepted WorkTask 至少关闭一个 obligation。探查、分页、SQL 分片和 provider
调用属于 Gate 4 execution graph，不成为无 obligation 的平行业务任务。

### 3.2.1 MeasurementDerivationAuthority

每个 `MeasurementResolutionOutcome` 和 `ResolvedEvidenceObligation` 持久化其推导时的
业务权威：

- `case_id`；
- `mailbox_authority_epoch`；
- accepted QuestionRevision；
- accepted AnalysisFrameRevision。

该权威刻意不包含 `head_version`、Plan 或 Answer head。Plan-only revision、Evidence
admission 或 Answer 更新不会让同一 Frame 的 measurement derivation 失效；用户
correction、QuestionRevision 或 FrameRevision 会让旧 outcome/obligation 进入 stale，
即使调用方没有提交 `OperationIdentity`，repository 仍必须拒绝持久化和采用。

### 3.3 PlanAdoptionRecord

system-owned immutable record，至少绑定：

- case/question/frame/plan identity 与 plan content hash；
- acceptance 前 authority snapshot 与 expected head version；
- adopted resolution outcome IDs、outcome hashes；
- adopted obligation IDs、obligation hashes；
- task-to-obligation 与 obligation-to-query-binding closure；
- adoption policy/version 与完整 derivation proof。

repository 从持久化 Frame、outcomes、obligations、Plan 和 QueryBindingEnvelope 重新计算
合法 adoption；调用方提供的摘要不能替代 repository validation。

### 3.4 QueryBindingEnvelope

每个 executable obligation 一个 immutable envelope，完整绑定：

- case/question/frame/plan/task/estimand/requirement/obligation/outcome；
- semantic measurement ID、authority binding ID、resolution ID；
- accepted Frame identity/hash、target kind、measurement node refs、scope、population、
  observation unit、grain、
  metric variables、event、time semantic、estimator、exposure、comparison/cohort/
  sequence/relationship/identification refs；
- actual resolved windows、timezone/business-day boundary 与 exposure facts；
- data contract、snapshot/release、watermark、late-arrival 与 resolver input identity；
- requirement composition、minimum strength、contradiction/boundary/falsification/
  reversal refs；
- closure definition 与 field derivation proof；
- Gate 4 physical compiler contract version。

Envelope 不携带自由业务参数、SQL、表名、列名、physical filter 或 capability 私有
配置。Gate 4 从 immutable Frame exact hash 解引用 node definitions，并可在安全边界内
增加物理执行信息，不能改写 envelope 的业务字段。

### 3.5 ConformanceExecutionSpec

G3.4 只生成 `conformance` realm 的执行规范：

- 绑定一个 QueryBindingEnvelope 与 logical execution ID；
- 绑定 fixture/artifact digest、预期 result contract 和 conformance policy；
- 禁止 SQL、生产 data source、credentials 和 production realm；
- 作为 G3.5 conformance Evidence provenance 的唯一执行入口。

### 3.6 LogicalExecutionAttempt

每次 attempt 绑定同一 logical execution ID、query binding、execution spec 与
authority snapshot：

- attempt 1 为 initial；
- attempt 2+ 只能是 technical retry，并引用 prior attempt；
- retry 只允许改变 attempt metadata、provider routing、lease 和技术错误恢复字段；
- query binding、execution spec input、Frame/Plan/outcome/obligation identity 任一变化，
  拒绝作为 retry。

## 4. Deterministic compiler 与 acceptance

### 4.1 Plan bundle compiler

compiler 输入为 accepted Frame、persisted admitted outcomes、persisted obligations、
LLM `ProposedWorkTask` 和 controller-issued revision metadata。它必须：

1. 验证每个 Frame estimand 恰有一个被采用的 current resolution outcome；
2. 验证每个 `(estimand, evidence requirement, required evidence type)` 恰有一个
   obligation；
3. 验证 outcome/obligation 的 semantic、authority、resolution、Frame identity；
4. 验证所有 obligations 被 task closure 完整且不重复覆盖；
5. 为 executable obligation 生成唯一 envelope；
6. 为 boundary obligation 禁止生成 envelope；
7. 验证 task DAG、system-issued IDs 和 query binding ownership；
8. 生成 WorkPlanRevision、QueryBindingEnvelope、PlanAdoptionRecord；
9. 对相同输入重放得到相同 identity 和内容。

### 4.2 Atomic acceptance

新增 `accept_plan_bundle`，一个事务内：

- lock InvestigationCase 并检查 expected head/version/authority epoch；
- 读取当前 accepted Question/Frame；
- 读取并验证 measurement resolution admission；
- 读取 persisted obligations；
- exact replay plan bundle；
- immutable insert Plan、query bindings 与 adoption；
- move accepted plan head；
- append accepted journal event；
- commit。

任一步失败全部回滚。原 `accept_plan` 直接入口删除，避免绕过 adoption。
`ConformanceExecutionSpec` 通过独立 current-authority CAS 记录；它没有 Plan acceptance
权限，也不能进入 production realm。

所有普通 outbox commit 也使用同一 authority commit fence。用户 correction 与 worker
enqueue 并发时，提交顺序由 mailbox authority row lock 决定；correction 先提交则旧
outbox fail closed，worker 先提交则 correction 在其后推进 epoch。stale check 只用于
提前终止，不能代替提交点围栏。

## 5. Revision 与 retry 规则

| 变化 | 必需动作 |
|---|---|
| metric、population、observation unit、window、exposure、comparison、estimand、assumption、completion 改变 | 新 AnalysisFrameRevision |
| resolution context、snapshot/release 或 actual resolved boundary 改变且影响业务测量 | 新 resolution outcome；若 accepted Frame 含义也变则先新 FrameRevision |
| obligation 分组、任务依赖、调查顺序、capability intent 改变，但业务测量不变 | 新 WorkPlanRevision |
| provider timeout、连接失败、worker crash、lease takeover | 同一 logical execution ID 的 technical retry |
| query binding、execution input 或业务权威改变 | 禁止作为 retry；重新 adoption 或 revision |
| 仅 case head、Plan/Answer 等同业务权威内的 sibling state 推进 | 允许 technical retry；仍复用原 logical identity |

## 6. 代码与存储边界

已新增：

- `domain/planning.py`：proposal-to-plan compiler、PlanAdoptionRecord、
  QueryBindingEnvelope、ConformanceExecutionSpec、LogicalExecutionAttempt；
- `controller/runtime.py`：accepted heads 下的 plan bundle compile/admit；
- storage port/in-memory/PostgreSQL 的 plan bundle 与 logical attempt API；
- migration `005_gate3_4_plan_query_continuity.sql`；
- authority/actions/context/runtime schema 与 TypeScript bindings；
- `test_gate3_4_plan_query_continuity.py` 和 PostgreSQL conformance coverage。

已修改：

- `WorkTask`、`WorkPlanRevision`；
- `RevisePlanPayload`、`CallCapabilityPayload`、`RunSensitivityPayload`；
- controller revise-plan 与 effect dispatch；
- `accept_plan` 调用点和当前 fixture，直接切换到当前合同，不保留兼容入口。

## 7. 测试矩阵

### 7.1 合同与编译

- G3.3 已验证 13 类 ClaimTargetKind 各自有显式 measurement validation contract；G3.4
  compiler 对 target kind 无业务分支，验证通用 identity/closure，并用 executable、
  all-boundary、mixed、multi-estimand 和 multi-evidence-slot topology 证明；
- 13 类真实业务行为与多种合法 measurement design 的端到端评分继续由 G3.6
  behavior-first Episode matrix 验收；
- 多 estimand、多 requirement、shared requirement、`AT_LEAST` composition；
- executable 与 typed-boundary 混合；
- task 合并、拆分、dependency DAG 和乱序 proposal；
- system-issued task/query/logical IDs 可重放，LLM 不能注入；
- missing、duplicate、unknown、cross-case、cross-frame obligation 全部拒绝；
- Plan 无法降低 strength、scope、exposure、composition 或 completion requirement；
- envelope 不出现 open parameters、SQL、table/column 或 physical filter 字段。

### 7.2 日期与 exposure 连续性

- 跨月、跨年、闰年、28/29/30/31 天；
- 不等长 window、partial coverage、valid/observed/expected exposure；
- timezone、business cutoff、DST 23/25 小时；
- envelope 中 actual window/exposure 与 admitted outcome byte-for-byte 一致；
- window、month offset、snapshot/release 或 exposure mutation 导致 adoption 失败。

### 7.3 Revision / retry / 并发

- conformance technical retry 保持 logical identity；
- runtime effect retry 保持 accepted Action/outbox identity，task/query/sensitivity
  request 不能改变；
- retry 修改 query binding 或 execution input 被拒绝；
- technical retry 比较 mailbox epoch 与 accepted question/frame/plan；不能因
  obligation、Evidence、contradiction、active candidate 或普通 head sibling state
  推进被误判为业务变化；
- Plan-only sequencing change 创建 PlanRevision；
- measurement change 必须创建 FrameRevision；
- stale head、correction-vs-adoption、并发 Plan acceptance 只有一个成功；
- crash at Plan/query/adoption/journal boundary 后全部回滚；
- duplicate delivery idempotent，payload divergence 冲突；
- 旧 epoch/outcome/obligation 在 correction 后只能 superseded；
- correction 与普通 outbox enqueue 真实并发时，旧 epoch outbox 无法越过 commit
  fence。

### 7.4 Realm 与跨层边界

- G3.4 只能生成 conformance execution；
- conformance spec 不能携带生产数据源或 SQL；
- Gate 4 compiler input 必须包含 accepted QueryBindingEnvelope 与其 exact-hash
  immutable Frame authority；
- direct repository bypass、伪造 adoption、伪造 system IDs 被拒绝；
- Python/JSON Schema/TypeScript canonical round-trip；
- clean-copy Python 3.12 build/test/run。
- Gate 1、Gate 2、Gate 3.4 PostgreSQL acceptance 均按 001→005 完整 migration
  bootstrap；可分别运行，也可在同一临时数据库连续和组合运行；
- PostgreSQL 测试每例清空业务表并保留 migration ledger，固定 fixture ID 不依赖
  前序测试残留；所有 migration 重复应用必须保持 checksum 一致；reset 同时要求
  `WAJE_VNEXT_ALLOW_TEST_DATABASE_RESET=1`、runner 生成的随机 token，以及目标数据库
  `public` schema 中与当前数据库名和 token exact match 的 disposable marker。三个
  Docker acceptance 入口只在新建的 tmpfs 数据库内创建 marker；项目 `.env` 或单独
  设置环境开关无法通过清理授权。

测试从业务问题家族、测量形状和 mutation 维度生成组合，不把某道题的预期窗口或
capability 路线写成固定答案。

## 8. Exit criteria

- [x] Plan 无法降低、遗漏或重复覆盖 Frame requirement；
- [x] accepted Plan 对每个 estimand 采用唯一 admitted resolution outcome；
- [x] 每个 obligation 有唯一 WorkTask closure owner；
- [x] executable obligation 有唯一 QueryBindingEnvelope，boundary obligation 无查询；
- [x] envelope 无开放业务参数、SQL 或物理数据源字段；
- [x] LLM 不能签发 task/query/logical identity；
- [x] technical retry 保持 logical identity；
- [x] 业务语义变化创建 FrameRevision；
- [x] Plan bundle acceptance 在 in-memory/PostgreSQL 均为单事务 CAS；
- [x] correction、并发、duplicate、crash/resume 和 stale input fail closed；
- [x] ConformanceExecutionSpec 无法进入 production realm；
- [x] Gate 4 physical compiler 所有权边界明确；
- [x] 全量 Python 3.12 clean-copy 验证通过；
- [x] 组合对抗审查 Blocking/Major 为 0；
- [x] G3.E0 readiness 状态未被 local implementation 篡改。

## 9. 提交与审查

- 单独分支：`codex/gate3-4-plan-query-continuity`；
- implementation commit 只包含 G3.4 当前合同，不保留旧 `accept_plan` 或自由
  capability parameter 兼容分支；
- 提交前运行 focused、全量、schema、eval-readiness 与 clean-copy；
- 子智能体分别审合同权威、存储/CAS、真实问题测试覆盖；
- 修复后再做主智能体逐项 exit audit；
- PR 明确列出本地 PostgreSQL 是否因缺少 `WAJE_VNEXT_DATABASE_URL` 跳过。

## 10. 实施前对抗自审

### 10.1 已发现并关闭

1. **调用时重新选择 capability**

   若 `call_capability` 保留自由 `capability_name`，同一 accepted Plan 可以在执行时换
   分析路线。修订：capability intent 进入 accepted WorkTask；调用 payload 只引用
   `task_id` 与 `query_binding_id`，Gate 4 根据 accepted task 解析实际 capability。

2. **system ID 循环依赖**

   若 query binding ID 同时依赖 plan content hash，而 plan 又保存 query binding ID，
   无法得到稳定 identity。修订：采用两阶段 derivation：

   - controller 先签发 plan revision ID；
   - task ID 从 plan ID + proposal key 派生；
   - query binding ID 从 plan/task/obligation/outcome identity 派生；
   - Plan 写入 binding IDs 后得到 content hash；
   - envelope 与 adoption 再绑定最终 plan hash。

3. **跨 resolution 拼接**

   只校验 outcome 各自合法仍可能把同一 estimand 的两个 outcome 或互相冲突的数据版本
   拼进一个 Plan。修订：每个 estimand 只能采用一个 outcome；adoption 保存每个 outcome
   的 resolution admission、context hash、input bundle hash 和 registry hash。compiler
   按 versioned coherence policy 检查可共同执行的 context axes，差异必须显式 boundary
   或新 Plan/Frame。

4. **boundary 被伪装成空查询**

   给 boundary obligation 生成空 envelope 会让下游把“无法测量”当成成功执行。修订：
   boundary obligation 必须进入 WorkTask closure，但 query binding 数量严格为零。

5. **conformance fixture 冒充 production**

   单靠 `realm="conformance"` 标签不足以保护。修订：fixture ref 只允许
   `waje-vnext://conformance-fixture/` 命名空间并绑定内容 digest；spec schema 明确禁止
   SQL、data source、credential、table、column 和 production locator 字段。

6. **PlanAdoptionRecord 由调用方自证**

   若 repository 只核对 adoption hash，调用方仍可伪造完整但错误的 closure。修订：
   repository 从持久化 Frame、admitted outcomes、obligations、Plan 和 envelopes exact
   replay，且不提供绕过 replay 的独立 adoption insert API。

7. **technical retry 偷换输入**

   只比较 task ID 无法识别窗口、snapshot 或 exposure 被替换。修订：
   LogicalExecutionAttempt 同时绑定 logical execution ID、query binding content hash、
   execution spec content hash 与 authority snapshot；后续 attempt 必须逐项相等。

8. **Plan revision 与 Frame revision 混淆**

   capability 分组和执行顺序可以改 Plan；任何 measurement node 或 accepted resolution
   的业务边界变化不能借 Plan revision 覆盖。修订：Plan compiler 从 Frame/outcome 派生
   envelope，没有调用方可覆盖字段；若输入变化，identity 改变并触发重新 adoption，
   measurement graph 变化则必须先新 Frame。

9. **correction 与 Plan commit 竞态**

   当前 `accept_plan` 只 CAS `InvestigationCase.head_version`；用户 correction 可以在
   worker 的 stale check 之后推进 mailbox authority epoch，而不推进 case head，导致旧
   Plan 仍被接受。同一窗口也存在于 `accept_frame` 的 proof snapshot check 与 head
   commit 之间。修订：共享 authority commit fence 在同一事务按固定顺序锁
   `case_mailbox_heads` 与 `investigation_cases`，同时校验 expected mailbox authority
   epoch、question/frame head、active candidate generation/content hash 和 case head。
   `accept_frame` 与 `accept_plan_bundle` 共用该 fence；in-memory store 执行同一组
   校验；correction-vs-frame/adoption race 加入确定性回归测试。

10. **rejected action 直接进入 effect outbox**

    只持久化 ActionEnvelope 无法证明 action 已被当前 authority 接受。修订：generic
    effect enqueue 重新执行 admission，并 exact 校验 `ACTION_ADMITTED` journal、
    success receipt、source event、canonical request、current Plan 与 query binding；
    task/query/sensitivity 任一变化均拒绝。

11. **obligation job 绕过 scheduler**

    generic outbox 若能写 `AsyncJobKind.OBLIGATION`，调用方可注入 obligation payload 或
    把未 adopted obligation 发给 worker。修订：generic enqueue 全面拒绝 obligation；
    scheduler 使用受控原子入口同时记录 dispatch event、完整 persisted obligation、
    dispatch record 与 outbox，并在 store 层重放 admission。

12. **同一 requirement 的多证据槽被压成一个 obligation**

    requirement 可以要求 primary estimate 与 independent reconciliation 等多个 evidence
    type。修订：obligation slot identity 为
    `(estimand, requirement, evidence type)`；Plan 可把这些槽合法合并或拆分为不同 task，
    PostgreSQL current schema 允许每个 immutable slot 独立持久化。

13. **同一 Frame 的新 resolution 无法被显式选择**

    snapshot/release、anchor 或 coverage 变化可以在同一 Frame 下形成多个 admitted
    outcome。修订：ProposedWorkTask 引用的 obligation 集合决定本次采用的 outcome；
    compiler 和 repository 只采用每个 estimand 明确选中的一个 outcome，并拒绝拼接旧新
    obligation。

14. **合法并行 outcome 共享 context hash 被误判重复**

    多 estimand 可以合法共享同一 ResolutionContext、input bundle 或 registry。修订：
    adoption 中 identity 数组保持唯一；与 outcome 按 ordinal 对齐的 hash 数组允许重复，
    同时继续执行长度和 exact replay 校验。

15. **execution success 被展示成 evidence complete**

    worker 完成只说明执行结束，Evidence admission 仍属于 G3.5。修订：customer projection
    使用 `execution_succeeded` / `execution_failed` / `typed_boundary_recorded` /
    `execution_superseded`，不提前宣告 evidence obligation 已完成。

16. **无 operation 的 measurement derivation 越过 correction**

    仅在 `OperationIdentity` 存在时核对 authority epoch，会让离线 resolver 或内部
    compiler 生成的旧 outcome/obligation 在 correction 后被写入。修订：derivation
    authority 成为 outcome 与 obligation 自身的不可变内容；in-memory/PostgreSQL 在
    每次 record 与 Plan adoption 时都与当前 Question/Frame/mailbox epoch exact match。

17. **outbox stale check 与提交之间存在竞态**

    worker 在 stale check 后、outbox insert 前可能遇到用户 correction。修订：所有
    ordinary outbox enqueue 在同一事务获取统一 authority commit fence，再校验
    expected head、mailbox epoch、authority snapshot 与 operation authority；真实并发
    测试证明 correction 先提交时旧 outbox 被拒。

18. **technical retry 被无关 sibling state 推进误杀**

    完整比较 `AuthoritySnapshot` 会把同一 accepted Question/Frame/Plan 下的
    obligation、Evidence、contradiction、active candidate 或普通 head 推进当作业务
    口径变化。修订：retry 使用 `same_business_authority`，只比较 case、mailbox epoch、
    accepted Question、accepted Frame 和 accepted Plan；query binding 与 execution
    spec identity 仍逐项一致。Plan head 改变仍会 fence 旧 logical execution。

19. **Gate acceptance 依赖 partial migration 与脏 fixture**

    Gate 1 曾跳过 migration 002 后直接写 receipt，Gate 2 再安装 FK 时会被历史测试行
    阻塞；固定 case ID 也会污染后续结果。修订：统一 PostgreSQL test bootstrap 先清除
    test-owned rows，再按 001→005 完整迁移并验证重复应用；每个 test 重新隔离业务数据，
    receipt fixture 遵守当前 action FK，005 同步接纳当前 `message_binding` job kind。

20. **capability intent 只有字符串 allowlist**

    仅检查 URI 在列表中，仍允许 executable obligation 路由到 boundary inspection，
    或让 `run_sensitivity` 复用普通 measurement task。修订：引入版本化
    `CapabilityIntentContract` registry，逐项声明允许的 obligation disposition、
    Evidence type、required measurement authority 与 typed action kind；registry 是
    frozen tuple-backed value，内容 hash 在构造时校验。registry version/hash 进入
    PlanAdoptionRecord 和 derivation proof，plan compiler、action admission 与
    repository exact replay 都执行同一合同。`run_sensitivity` 只能采用
    `evidence:sensitivity` 且 accepted Frame 已声明 sensitivity authority。

21. **PostgreSQL acceptance 清理授权只靠环境变量**

    单一环境开关可能随项目 `.env` 一起误指向非临时数据库。修订：Docker runner 创建
    tmpfs PostgreSQL 后生成随机 token，并把数据库名/token marker 写入数据库自身；
    reset 必须同时满足环境开关、调用方 token 和数据库 marker。错误 token 的验收测试
    证明清理在读取业务表清单前即 fail closed。

22. **计划拓扑和提交原子性只在 happy path 成立**

    grouped/serial 测试无法证明 parallel、forward-declared dependency、multi-level DAG、
    all-boundary、mixed 或真实 CAS 竞争。修订：补齐合法拓扑与 cycle/unknown/duplicate
    owner 负测；PostgreSQL 验证 all-boundary/mixed 持久化、两个合法 Plan 竞争同一 head
    只有一个成功、exact duplicate 幂等、同 identity 不同 payload 冲突，以及
    Plan/query rows 写入后在 adoption insert 注入异常时全事务回滚。

23. **migration 文件自行提交，破坏 repository 外层事务**

    若 migration 004/005 内含 `BEGIN/COMMIT`，`_apply_migration` 无法把 DDL 与
    `schema_migrations` ledger 放在一个原子事务中；ledger 写入失败时 DDL 仍可能遗留。
    修订：migration 文件不再拥有事务边界，由 repository 唯一管理事务。验收 runner
    分别在 004、005 ledger insert 前注入失败，确认新增表和 ledger version 均不可见，
    再恢复并成功应用。

24. **PostgreSQL logical retry 仍被旧 case head 卡死**

    retry 已使用 `same_business_authority`，但共享 commit fence 仍要求旧
    `head_version` exact match；同一 Question/Frame/Plan 下的 Evidence、Answer 或
    obligation sibling state 推进后仍会误拒绝。修订：logical attempt 的 fence 先锁定
    当前 case/mailbox，再由 sealed comparator 重检 mailbox epoch 与 accepted
    Question/Frame/Plan；query binding 和 execution spec identity 继续 exact。PostgreSQL
    回归先推进 Evidence、provisional Answer 和 obligation state，再从旧 snapshot 提交
    retry。

25. **duplicate Plan adoption 丢失 operation provenance**

    内容相同的 replay 若只比较 Plan/adoption/event payload，可以用不同
    causation/correlation/idempotency 身份“认领”旧 accepted event。修订：repository
    对调用方 operation 生成 expected causal event identity；无 operation 时按既有
    event payload 派生；两者都必须与已持久化 journal operation exact match。

26. **正向测试从 compiler 自身反推，无法发现字段漏投影**

    只比较 compiler 输出 hash 或依赖默认 Frame，会让 strength、scope、composition、
    boundary、contradiction 和 falsification/reversal 的漏投影继续通过。修订：增加独立
    非默认 Frame oracle，逐字段断言 QueryBindingEnvelope；同时验证
    `AT_LEAST.minimum_count` 不得超过声明的 Evidence slots，并覆盖 generic effect 在
    sibling obligation state 推进后的实际 delivery。

27. **多 estimand 的 executable binding 可能串线**

    mixed executable/boundary 只能证明一个查询绑定，无法发现第二个 executable
    estimand 偷用第一个 outcome、estimator 或 resolved windows。修订：增加两个同时
    executable 的 estimand，共用 requirement 仍各自拥有 obligation；分别使用
    2026-06 与 2026-07 anchor，grouped/split Plan 都逐项核对 outcome、semantic/
    authority identity、estimator、actual start/end、period offset、resolver input 与
    selected-date hash。交换 outcome、obligation、完整 instance 或仅 windows 都被拒绝。

28. **Evidence cardinality 在持久化 binding 中可被弱化**

    Frame requirement 的 `minimum_count` 校验无法自动保护调用方直接构造的
    `EvidenceRequirementBinding`；Python 的 `True == 1` 还可能让 exact replay 掩盖
    篡改。修订：Frame 与 binding 两层都只接受非布尔正整数，拒绝 float、0、负数、超过
    slots 的值，并要求 `ALL/ANY` 的 count 为 `None`。

29. **执行规范和 attempt 的系统身份可被调用方伪造**

    只校验 sha256 格式、当前 Plan 和 binding hash，无法证明
    `logical_execution_id`、`conformance_execution_spec_id` 与
    `logical_execution_attempt_id` 来自 canonical compiler；内存 adapter 还可能给同一
    QueryBinding 写第二份 spec。修订：共享 validator 从 QueryBinding、fixture、
    result contract、policy 和 retry lineage 重算全部派生 ID；InMemory 与 PostgreSQL
    都调用同一 validator，InMemory 补齐 logical execution / QueryBinding 唯一索引。
    attempt 2+ 与持久化 prior attempt 逐项核对 Plan/task/query/spec/hash 和 business
    authority。双后端 direct repository 测试拒绝 forged ID、第二份 spec、old-Plan
    current-snapshot 伪装及重算 ID 后的 retry sealed-field 篡改。

### 10.2 自审结论

计划在以下条件下可执行：

- Plan 是 resolution/obligation 的唯一 accepted adoption 点；
- QueryBindingEnvelope 只做逻辑业务绑定，物理执行所有权留给 Gate 4；
- conformance 与 production realm 结构隔离；
- repository exact replay 和 CAS 同时成立；
- test oracle 检查合同不变量与可接受结果空间，不固定某道业务题的 LLM 路线。

当前 Blocking：0。
当前 Major：0。
