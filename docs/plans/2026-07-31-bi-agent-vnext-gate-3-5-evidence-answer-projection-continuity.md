# WAJE BI Agent vNext G3.5：Evidence、Answer 与 Workflow 连续性实施计划

> 日期：2026-07-31
> 状态：Local implementation complete；G3.E0 formal admission 仍为 `deny_g3_1`
> 基线：`origin/main@15f61c51`
> 分支：`codex/gate3-5-evidence-answer-continuity`
> 实现根：`vnext/`

## 1. Gate entry

### 1.1 已查明事实

- G3.4 已在 `main` 合并，accepted Question → Frame → resolution outcome →
  obligation → Plan → QueryBinding → conformance logical execution 的链路已关闭。
- G3.E0 formal admission 仍为 `deny_g3_1`。当前 development override 允许继续完成
  local implementation；它不允许生产 Evidence、settled Answer、delivered Workflow 或
  protected held-out 结果。
- 当前 `EvidenceRecord` 只绑定 Frame、Plan、task 和自由 provenance，缺少
  Question/Estimand/requirement/obligation、分层 identity、实际 scope/window/exposure、
  tagged execution provenance 与 realm。
- 当前没有 `CapabilityResultEnvelope`、`EvidenceAdmissionRecord` 或
  `EvidenceUseBinding`。
- 当前 `record_evidence` 只验证 accepted Frame/Plan 和 task membership；它无法证明
  Evidence 回答 accepted estimand，也无法拒绝 window、exposure、scope、strength 或 realm
  漂移。
- 当前 `ObligationCompletionRecord` 只保存 result hash。执行成功后没有 durable typed result
  receipt，无法从持久化事实恢复 Evidence disposition。
- 当前 `propose_answer` 直接把 LLM 提供的 claim ID 与文本 applicability 写进
  `AnswerVersion`，随后把 controller phase 设为 `COMPLETED`。它没有逐 claim
  Evidence-use compatibility、obligation closure、typed scope 或 strength ceiling precheck。
- 当前 `EvidenceValidityRecord`、`ObligationSatisfactionRecord` 和
  `SettlementPreconditionReport` 只有 Gate 1/G3.1 schema 骨架；repository 接受调用方构造
  的内容，没有从当前 authority exact replay。
- 当前 journal 的 `customer_projection` 是事件级片段，没有 accepted WorkPlan + journal 的
  可重建四轴 Workflow contract。

### 1.2 访谈判断

**本 Gate 无需用户决策。**

用户已经确认：

- capability 原生返回 immutable EvidenceRecord；
- Answer 逐 claim 绑定 Evidence 与适用边界；
- provisional 与 settled 分离；
- Reviewer 输出结构化异议，settlement 前执行风险触发审查；
- Workflow 是 accepted WorkPlan 与真实 journal 的只读业务投影；
- Gate 3 不拥有 Gate 4 物理查询和 Gate 5 settled publication。

G3.5 只实现这些已批准边界的通用连续性，不选择业务指标、日期窗口、分析路线、
capability 分类或 Reviewer 内容评分规则。

若实施中出现下列情况，暂停对应分支并按 `$grill-me` 一次只问一个问题：

- 需要改变 accepted Frame 的 estimand、scope、window、exposure、comparison、
  estimator、identification 或 strength ceiling；
- 需要让 Evidence admission 推断新的开放业务语义；
- 需要提前决定 Gate 4 的物理 QuerySpec、SQL、数据源或 credential 策略；
- 需要允许 Gate 3 发布 settled Answer、completed/delivered Workflow；
- 需要定义会改变用户可见结论的 Reviewer 内容评分标准。

## 2. Gate 目标

G3.5 关闭以下单向链路：

```text
accepted QuestionRevision
  -> accepted AnalysisFrameRevision
  -> admitted MeasurementResolutionOutcome
  -> immutable ResolvedEvidenceObligation
  -> accepted WorkPlanRevision + PlanAdoptionRecord
  -> immutable QueryBindingEnvelope
  -> tagged logical execution
  -> immutable CapabilityResultEnvelope
  -> immutable EvidenceRecord
  -> system-derived EvidenceAdmissionRecord
  -> append-only EvidenceValidityRecord
  -> system-derived ObligationSatisfactionRecord
  -> immutable EvidenceUseBinding
  -> provisional AnswerVersion + claim precheck
  -> system-derived SettlementPreconditionReport
  -> read-only four-axis WorkflowProjection
```

G3.5 建立以下硬边界：

1. capability 执行成功与 Evidence admitted 分开表达；
2. capability result、Evidence、admission 和 disposition 在 crash/retry 后可重放；
3. Evidence 创建时的 authority provenance 永久不变；
4. Evidence 是否可用于当前 claim 由新的 EvidenceUseBinding 证明；
5. accepted heads、measurement identity、window、exposure、scope、strength 或 realm
   漂移时 fail closed；
6. provisional Answer 只能进入 `publication_state=provisional|blocked`；
7. Gate 3 所有入口拒绝 settled 和 delivered；
8. Workflow 只投影业务状态，不拥有编排 authority。

## 3. 非目标与 Gate 所有权

### 3.1 G3.5 不实现

- production QuerySpec compiler、SQL、ClickHouse/PostgreSQL analytics execution；
- capability 业务族与 SQL escape hatch；
- production credential/source registry；
- 数字与文字方向 verifier；
- Reviewer 内容评分、风险触发策略和 objection resolution；
- settled Answer transition、publication outbox 或用户 delivery；
- UI 双栏工作台；
- G3.6 behavior-first real-provider eval。

### 3.2 Gate 4 / Gate 5 边界

G3.5 定义并验证两类 tagged provenance 的封闭结构：

- `ConformanceExecutionProvenance`：G3.4 logical execution attempt +
  ConformanceExecutionSpec；
- `PhysicalQueryExecutionProvenance`：Gate 4 logical execution + QuerySpec +
  capability invocation。

当前 runtime 只允许 conformance profile 写入隔离的 conformance Evidence registry。
production branch 的数据结构进入 schema，但没有 Gate 4 trusted registry 时 admission
固定拒绝。拒绝结果保留服务端审计，不进入 production Evidence registry，也不能关闭
production obligation。

G3.5 测试不得用本地 fixture 模拟“可信 production Evidence”。production positive path
等 Gate 4 提供真实 QuerySpec、compiler 和 source registry 后再开放。

Gate 5 消费 G3.5 的 provisional Answer、claim precheck、EvidenceUseBinding 和
SettlementPreconditionReport。Gate 5 才能增加内容 verifier、Reviewer disposition、
settled transition、publication 和 delivery。

## 4. 权威对象

### 4.1 `CapabilityResultEnvelope`

capability-native immutable envelope 至少包含：

- result envelope、invocation、result、case、run、schedule、dispatch identity；
- realm 与 tagged execution provenance；
- QueryBinding、logical execution、attempt/spec identity 与 content hash；
- capability contract/version；
- bounded payload 或稳定 `ResultHandle`；
- capability-native immutable `EvidenceRecord`；
- result receipt hash、创建时间和 schema epoch。

系统从 dispatch、QueryBinding、execution spec/attempt 和 Evidence preimage 重算
envelope/result/Evidence ID。调用方提供的 forged ID、hash 或跨执行拼接一律拒绝。

### 4.2 tagged execution provenance

`ConformanceExecutionProvenance` exact 绑定：

- trusted conformance realm；
- logical execution、attempt、ConformanceExecutionSpec ID/hash；
- fixture、result contract、execution policy；
- query binding ID/hash。

`PhysicalQueryExecutionProvenance` exact 绑定：

- production realm；
- logical execution；
- QuerySpec ID/hash；
- capability invocation ID/hash；
- compiler、source registry 与 credential lineage refs。

union tag 与字段集合必须 exact。conformance 不能携带 QuerySpec；production 不能携带
fixture/spec 占位。

### 4.3 `EvidenceRecord`

EvidenceRecord 是 capability output 的不可变业务证据，至少绑定：

- case、run、realm；
- QuestionRevision、FrameRevision、PlanRevision、task；
- EstimandSpec、EvidenceRequirementSpec、ResolvedEvidenceObligation；
- semantic measurement、authority binding、resolution outcome/resolution；
- QueryBinding 与 logical execution；
- tagged execution provenance；
- comparison/operand/window identity；
- actual resolved time ranges、timezone、business-day cutoff；
- data contract、snapshot/release、watermark 与 late-arrival boundary；
- actual population、typed scope、grain、unit、aggregation path；
- expected/observed/valid/invalid/missing/at-risk exposure；
- estimate、uncertainty、Evidence type、strength、limitations；
- bounded payload 或稳定 result handle；
- capability/compiler/provenance refs 与 immutable content hash。

EvidenceRecord 不保存 accepted/rejected、current/superseded 或 obligation fulfillment。

### 4.4 `CapabilityResultReceipt`

system-owned receipt 证明 envelope 已持久化：

- envelope/result/Evidence identity 与 hash；
- dispatch、attempt 和 result hash；
- received operation/idempotency/correlation；
- received timestamp；

receipt 永久不可变，不保存 mutable admission status。是否已经 dispositioned 从
EvidenceAdmissionRecord、job disposition 和 admission checkpoint 推导。receipt 是 effect
success 与 Evidence disposition 之间的 durable recovery anchor。

### 4.5 `EvidenceAdmissionRecord`

system-owned immutable disposition：

- accepted/rejected；
- Evidence、result receipt、obligation、QueryBinding、Plan adoption；
- accepted authority snapshot；
- admission profile/realm；
- identity/scope/window/exposure/unit/grain/data-version/strength compatibility proof；
- required Evidence type 与 composition slot；
- reason codes、policy version、derived input hash；
- created timestamp。

accepted admission 才能创建 admitted-valid validity root、EvidenceUseBinding 和 satisfied
obligation projection。rejected admission 创建 `never_admitted` validity root。

### 4.6 `EvidenceValidityRecord`

append-only chain 表达 Evidence 的系统 disposition：

- root：`admitted_valid | never_admitted`；
- successor：`superseded | revoked`；
- prior ID/hash CAS；
- source authority、reason、policy 和输入 hash。

accepted head 改变不会改写历史 Evidence。新 authority 对旧 Evidence 的使用通过
EvidenceUseBinding 重新验证。source release 被撤销、contract 被废止或确定性错误发现时，
系统追加 successor。

### 4.7 `EvidenceUseBinding`

system-derived immutable compatibility proof，绑定：

- target AnswerVersion/claim；
- target Question/Frame/Plan/Estimand/requirement/obligation；
- Evidence、accepted admission 与 latest admitted-valid record；
- semantic/resolution/data-version compatibility；
- typed scope relation；
- unit、grain、aggregation、window 与 exposure relation；
- available strength、claim requested/effective strength；
- limitations、contradiction/reversal applicability；
- compatibility status、reason codes、policy、derived input hash。

无法证明的关系返回 `unknown/rejected`，不会靠文本相似度放行。

### 4.8 `ObligationSatisfactionRecord`

从 obligation definition 和当前 accepted admissions/use bindings/boundaries/contradiction
dispositions重算：

- `open | satisfied | boundary | blocked | superseded`；
- 完整 input set IDs/hashes；
- composition (`ALL | ANY | AT_LEAST`) 与 minimum count；
- strength、scope、exposure 和 boundary closure；
- policy、reason、content hash。

记录形成 append-only current-head chain：

- revision number、prior satisfaction ID/hash；
- 每个 prior 只能有一个 successor；
- 同一 obligation + input-set hash 只产生一个 canonical record；
- repository 从 persisted inputs exact replay status 和 ID。

新 Evidence 或 validity 变化产生 successor；definition 永久不变。调用方不能提交
`satisfied` 字符串或自由 admission/use ID 集合。

### 4.9 provisional claim 与 `ClaimPrecheckRecord`

LLM `ProposedClaim` 提供开放业务表述，并引用 accepted typed authority：

- proposal claim key；
- target estimand；
- obligation refs；
- Evidence refs；
- typed applicability `ScopeExpression` 或 accepted scope ref；
- requested claim strength；
- boundary/limitation；
- contradiction/reversal refs；
- statement。

controller 规范化 typed applicability，生成 scope、AnswerVersion 和 claim ID。LLM 无权注入
系统 claim identity。

system-owned precheck 逐 claim 验证：

- target estimand/requirement/obligation closure；
- EvidenceUseBinding 全部 accepted 且 latest；
- applicability 是 Evidence supported scope 的可证明子集或 lawful projection；
- requested/effective strength 不超过 Frame ceiling、Evidence strength 和 requirement；
- window、exposure、unit、grain、data version 与 identification level兼容；
- contradiction/reversal 与 limitation 没有被省略；
- boundary claim 绑定允许的 typed boundary closure。

precheck 只验证 authority/identity/scope/strength 边界。statement 数字、文字方向和内容正确性
留给 Gate 5 verifier/Reviewer。

provisional Answer 采用 candidate/bundle admission：

1. 先持久化 system-issued candidate identity 与逐 claim precheck；
2. supported、bounded 或显式 typed boundary claim 可进入 accepted AnswerVersion；
3. 任一 unsupported strong claim 会拒绝该 candidate，并返回逐 claim failure；
4. Primary Agent 可以只修订失败 claim 后重提，已通过 claim 的证据事实不需要重跑；
5. deterministic system 不删除 claim，也不重写 narrative。

AnswerClaim 只引用 EvidenceUseBinding 或 typed boundary satisfaction，不能直接引用裸
EvidenceRecord ID。

### 4.10 `SettlementPreconditionReport`

repository 从当前持久化状态 exact derive：

- accepted Question/Frame/Plan 与 provisional Answer；
- Estimand、requirement、outcome、obligation、logical execution；
- tagged provenance realm；
- admissions、latest validity、use bindings、satisfactions、claim prechecks；
- objection dispositions 与 trace completeness refs；
- policy、fail reasons、derived input hash。

Gate 3 runtime 的报告只可能：

- structurally eligible for a future Gate 5 settlement；或
- blocked。

当前 conformance-only runtime 必须包含 `production_evidence_unavailable`，不能形成可供
production settlement 消费的 eligible report。Agent、capability、fixture 和直接
repository caller无权自报 eligible。

报告必须额外绑定 provisional Answer ID/hash、claim IDs、PlanAdoptionRecord 和完整
EvidenceUseBinding 集合。repository 接收 derive request，不接收 caller 构造的 report。

## 5. 原子 admission 与 crash recovery

### 5.1 T1：result landing

capability completion 必须提交 typed envelope，不能只提交 result hash。一个短事务：

1. 验证 delivery lease/fencing token；
2. exact 验证 dispatch 时封存的 authority、schedule、dispatch、QueryBinding、
   execution spec/attempt；
3. 持久化 immutable CapabilityResultEnvelope、EvidenceRecord 和 CapabilityResultReceipt；
4. 记录 execution succeeded fact，但不写 terminal JobDisposition；
5. checkpoint 进入 `WAITING_FOR_EVIDENCE_ADMISSION`；
6. append result-landed journal；
7. commit 后才确认 result landing。

T1 不要求 dispatch 时的 Question/Frame/Plan 仍是 current head。correction 后迟到的合法
result 仍按 sealed dispatch authority 保存为审计事实；T2 再根据 current authority 将其
rejected/superseded。这样不会丢失“旧调查实际返回了什么”。

重复 worker 先查 immutable receipt：

- 同 invocation + 同 result hash：跳过外部 capability，继续 T2；
- 同 invocation + 不同 result hash：authority conflict；
- 无 receipt：复用同一 provider idempotency key 执行。

### 5.2 T2：Evidence disposition

1. lock receipt、case authority 和 obligation；
2. 重检 current accepted heads/epoch；
3. exact replay result/Evidence identity；
4. 运行 realm、identity、scope、window、exposure、data-version、strength admission；
5. 写 EvidenceAdmissionRecord；
6. 写 EvidenceValidity root；
7. derive ObligationSatisfactionRecord；
8. 写 terminal JobDisposition；
9. append journal 和 checkpoint；journal-driven projector 通过 durable cursor/CAS 消费；
10. commit。

duplicate delivery 返回同一 canonical disposition。相同 identity 不同 payload 冲突。
correction 先提交时旧 result 可保留审计，但 admission rejected/superseded；旧 Evidence
不能进入新 Answer。

receipt 不被修改。dispositioned 状态从 immutable admission、checkpoint 和 job disposition
的存在性推导。

### 5.3 故障注入点

- capability 成功后、T1 前；
- result envelope insert 前后；
- Evidence insert 后、receipt 前；
- T1 commit 后、T2 前；
- admission 后、validity 前；
- validity 后、satisfaction 前；
- admission/validity/satisfaction、completion、terminal disposition、
  journal/checkpoint 各持久化写入点；
- journal-driven projection cursor CAS 前；
- T2 commit acknowledgment 前后。

每个故障点恢复后只能得到一个 result、Evidence、admission、validity root 和当前
satisfaction；不能重复执行 capability。Workflow 不创建第二套 projection outbox
authority，跨进程 projector 直接消费 durable journal，使用 cursor application receipt 与
head CAS 实现 ACK 丢失恢复。

## 6. Realm 与 registry

### 6.1 conformance profile

- 只接受 G3.4 trusted ConformanceExecutionSpec 和 exact logical attempt；
- fixture/result contract/policy 必须来自 frozen conformance registry；
- Evidence 写入 conformance registry；
- projection 明确标记 conformance；
- settlement report固定被 production realm precondition 阻断。

### 6.2 production profile

- schema 表达 PhysicalQueryExecutionProvenance；
- 需要 Gate 4 QuerySpec、capability invocation、source/credential registry；
- 任一 registry 缺失或不可信时 admission rejected；
- conformance Evidence ID 无法写入 production registry；
- test/conformance caller不能声明 production realm。

## 7. Workflow 四轴只读投影

新增 immutable/versioned `WorkflowProjection`：

- source accepted Plan ID/hash；
- source journal cursor；
- projection policy version/hash；
- task execution states；
- obligation states；
- publication state；
- case delivery state；
- conformance/production realm；
- source record IDs/hashes；
- projection content hash。

四轴：

| 轴 | 状态 | 权威来源 |
|---|---|---|
| execution | pending/running/succeeded/failed/superseded | accepted Plan、dispatch、execution receipts |
| obligation | open/satisfied/boundary/blocked/superseded | canonical satisfaction head |
| publication | not_ready/provisional/settled/blocked | provisional Answer、settlement/publication facts |
| case delivery | not_delivered/delivered/superseded | Gate 5 delivery facts |

Gate 3 projection compiler：

- 禁止生成 generic `completed`；
- 禁止 `settled` 和 `delivered`；
- execution success 不自动改变 obligation；
- provisional Answer 不自动改变 delivery；
- old Plan 在 current Plan 变化后投影 superseded；
- technical retry、prompt、model node、SQL retry、provider attempt 和 verifier internals
  不进入 customer projection；
- 给定相同 accepted heads + journal prefix + subordinate records，重放输出逐字节一致。

projection 是可删除重建的 read model。journal 与 authority records 是事实来源：

- 每个 immutable snapshot 使用 canonical ID
  `(case, plan, source_cursor, event_digest, policy_hash)`；
- mutable `WorkflowProjectionHead` 只保存 latest snapshot ref、last applied cursor 和
  CAS version；
- 每个 cursor 有 immutable application receipt；
- exact duplicate 为 no-op；
- cursor gap、逆序或同 cursor 不同 event hash 进入 blocked/rebuild；
- 从 cursor 0 全量 replay 的 snapshot/hash 必须等于 incremental head。

event `customer_projection` 中的自由展示字典不参与 reducer authority。G3.5 reducer 只消费
typed journal facts和 accepted records。

## 8. Controller 与状态机

新增/修改状态：

- `waiting_for_evidence_admission`；
- `waiting_for_review` 继续保留给 Reviewer；
- provisional Answer 接受后进入 `waiting_for_review | waiting_for_settlement`，不进入
  generic completed；
- Gate 3 settlement boundary 可进入 `blocked` 或显式 `stopped_at_gate_boundary`。

typed action 边界保持：

- `call_capability` 只引用 accepted task + QueryBinding，并解析到唯一 sealed obligation
  dispatch；
- `run_sensitivity` 必须额外绑定 selected sensitivity identity；当前 dispatch/result
  合同尚未封存该 identity，因此 G3.5 明确 fail closed，不能借普通 capability identity
  执行；
- `record_interpretation` 只引用 admitted、currently usable Evidence；
- `propose_answer` 使用 proposal claim key 与 typed refs；
- controller/system 生成 claim/use/precheck/report/projection identities。

## 9. 代码与存储边界

计划新增：

- `domain/evidence.py`：result/provenance/Evidence/admission/validity/use/satisfaction compiler；
- `domain/answering.py`：claim proposal compiler、precheck、settlement report；
- `domain/workflow.py`：四轴 projection；
- migration `006_gate3_5_evidence_answer_projection.sql`；
- G3.5 schema 与 TypeScript bindings；
- G3.5 InMemory/PostgreSQL/runtime tests 和 disposable PostgreSQL runner。

计划修改：

- `domain/authority.py`：当前 Evidence/Answer contract；
- `domain/measurement.py`：validity/satisfaction/report 从骨架升级为可重放合同；
- `domain/actions.py`、codec/provider schema：system-owned claim identity；
- `domain/events.py`、runtime state：result/admission/precheck/report/projection events 与 wait state；
- `controller/obligation_runtime.py`、`controller/runtime.py`：typed result completion、
  admission recovery、provisional answer bundle；
- storage ports/InMemory/PostgreSQL/codec；
- migration 001/003 的当前 epoch bootstrap 合同通过 006 直接切换，不保留旧 G3.5 写入口；
- tests/fixtures 直接重写当前合同。

### 9.1 存储约束

- result、Evidence、admission、use binding、precheck、report、projection 全部 immutable；
- canonical derived ID 由 repository 重算；
- result ↔ Evidence 一对一；
- Evidence ↔ admission 在同 profile/obligation 下唯一；
- admission accepted/rejected exactly one disposition；
- validity root一份，successor 一条；
- claim ↔ use binding集合与 precheck exact closure；
- satisfaction 按 obligation + input-set hash 唯一；
- projection 按 case + Plan + source cursor + policy唯一；
- production/conformance registry物理隔离或带不可绕过的 realm constraint；
- append、authority mutation、checkpoint、journal/outbox 使用同一短事务。

`ContextPacket` 和 `InterpretationRecord` 同步切换到 admission-aware、
latest-validity-aware 读取：

- rejected、audit-only 或 revoked Evidence 不进入 Agent 的可引用 Evidence index；
- 服务端审计仍能按 result receipt 查到原始事实；
- Interpretation 绑定 EvidenceUseBinding 或 accepted admission，不消费裸 Evidence ID。

`ResultHandle` 在 G3.5 只验证 bounded conformance locator、content hash、schema 和 row
count。production retention、expiry、读取授权和 locator proof 归 Gate 4。

## 10. 测试设计

### 10.1 合同与 mutation

- 每个 Evidence material field 独立 mutation：Question/Frame/Plan/task、estimand、
  requirement、obligation、semantic/binding/resolution/logical、window、scope、grain、
  unit、exposure、snapshot/release、realm、provenance；
- forged result/Evidence/admission/use/precheck/report/projection ID；
- same ID different payload；
- cross-case、cross-plan、cross-obligation、cross-attempt 拼接；
- conformance/production tag-field 混用；
- inline payload/result handle exactly-one 与 hash；
- unsupported/opaque scope 返回 unknown 并拒绝 claim。

### 10.2 业务形状

从 WAJEgame 八类问题与通用 claim shape 取样，不固定 LLM 路线：

- 单一变化解释含多个候选 driver claim；
- 周期规律含时间 pattern + 分群 driver；
- 事件复盘含 association/candidate mechanism 与 causal boundary；
- 收入健康含 concentration/distribution/data-quality 混合 claim；
- 多维因子归因含 Top-N、reconciliation 与局部 unsupported dimension；
- 异常复盘含 outlier + 大额用户敏感性；
- 多基准比较含多个 estimand/obligation；
- 数据质量问题含可发布 data-quality claim 和被阻断业务 claim。

每例只断言可接受结果空间：

- claim引用的 estimand/obligation/evidence closure完整；
- supported claim 可 provisional；
- 局部 gap 只阻断受影响 claim；
- strength 和 applicability 不超界；
- boundary/limitation/reversal 不被省略；
- 不规定固定窗口长度、task 数量、claim 数量或 capability 路线。

### 10.3 exposure 与 applicability

- 28/29/30/31 天、跨年、DST；
- 两侧实际天数不同；
- observed/valid/missing exposure 不同；
- raw total 不能支持 normalized rate claim；
- partial coverage 降级 strength；
- claim scope exact/subset/lawful projection/lawful aggregation/disjoint/unknown；
- population、time、grain、unit、data version 任一扩大时拒绝。

### 10.4 composition 与局部降级

- `ALL/ANY/AT_LEAST`；
- 多 Evidence slot、同 obligation 多 Evidence；
- mixed accepted/rejected Evidence；
- validity revoke/supersede 后 satisfaction 重算；
- contradiction unresolved；
- G3.5 pure compiler 验证 falsification/reversal disposition 的结构与伪造拒绝；
- runtime 尚无 trusted persisted check execution authority，因此 sensitivity、
  falsification、reversal 未闭环时，相关 claim 只能 rejected、omitted、unverifiable 或
  revoked；positive check authority 留给 G3.6/Gate 5；
- 一条 claim blocked 不取消独立 claim；
- Answer narrative不能绕过逐 claim状态。

### 10.5 异步、并发与恢复

- effect success 后各故障点 crash/resume；
- duplicate、乱序 result；
- correction-vs-result、correction-vs-admission、correction-vs-answer；
- result-vs-validity revoke；
- parallel obligations；
- stale lease worker；
- 同一业务 authority 下无关 sibling 状态推进；
- technical retry复用 logical identity；
- business identity改变要求新 Frame/Plan；
- InMemory/PostgreSQL parity；
- migration transaction failure injection。

最少逐点覆盖：

- T1 已提交、T2 未开始；
- T2 任一写入点回滚；
- T2 commit 后 ACK 前重投；
- 两个 admission worker 竞争同一 receipt；
- validity revoke 与 claim bundle CAS 并发；
- correction 分别与 T1、T2、Answer bundle、settlement derivation 竞态；
- projection duplicate、gap、逆序、双 worker 和 cursor commit 前 crash。

### 10.6 Workflow replay

- 相同 journal prefix重建同一 projection/hash；
- shuffled storage enumeration不影响结果；
- execution succeeded + Evidence rejected；
- obligation satisfied + publication not_ready；
- provisional Answer + delivery not_delivered；
- old Plan superseded；
- direct forged settled/delivered/completed全部拒绝；
- customer projection不含 prompt、内部 verifier、provider retry、SQL 或敏感 payload。

### 10.7 Reviewer 与后续 eval 边界

确定性测试负责：

- identity、scope、unit、grain、exposure、data version、realm；
- 引用 closure、strength ceiling、composition；
- crash/replay/transaction/immutability；
- settled/delivery hard deny。

G3.6/Gate 5 Reviewer grader负责：

- LLM 是否选择合理 claim；
- statement 是否忠实解释数据；
- 因果措辞是否适当；
- 哪些风险需要独立 Reviewer；
- 多个合理 Answer 中的业务质量。

G3.5 black-box coverage floor：

- 24 个业务 world：8 question family ×
  `{完整支持, 局部缺口, 冲突或反转}`；
- 每类至少一例 multi-claim、mixed strength 和局部 boundary；
- realm、authority drift、settlement hard deny 使用“原案通过/最小 mutation 被拒”的
  sibling pair；
- 以上 world 测试 authority/result-space，不断言唯一窗口、SQL、工具顺序或分析路线。

## 11. Entry criteria

- [x] G3.4 已合并且 CI 通过；
- [x] 本 Gate 无需用户决策；
- [x] G3.E0 `deny_g3_1` 和 development override明确保留；
- [x] Gate 4/5所有权边界明确；
- [x] G3.5计划与实现完成对抗式审查并关闭 Blocking/Major。

## 12. Exit criteria

- [x] typed CapabilityResultEnvelope、tagged provenance 与 immutable EvidenceRecord 完成；
- [x] effect success 后 T1/T2 crash/resume 得到唯一 Evidence disposition；
- [x] conformance/production realm fail closed；
- [x] drifted Evidence 被 rejected/superseded，不能关闭新 obligation；
- [x] EvidenceValidity、EvidenceUseBinding、ObligationSatisfaction 可 exact replay；
- [x] claim scope、strength、window、exposure、unit、grain、data version 超界被拒绝；
- [x] claim identity 由 controller/system 生成；
- [x] provisional Answer 逐 claim 绑定 accepted use/precheck；
- [x] provisional Answer 无法触发 settled、completed 或 delivered；
- [x] SettlementPreconditionReport 只能由 repository 从当前事实派生；
- [x] 四轴 Workflow projection 可从 journal + authority 重建；
- [x] conformance/test realm 无法写 production Evidence；
- [x] InMemory/PostgreSQL parity 通过；
- [x] migration atomicity 与 disposable reset guard 通过；
- [x] 全量 contract/schema/TypeScript/eval/bootstrap/clean-copy 验证通过；
- [x] 三路对抗式终审 Blocking=0、Major=0；
- [x] 父计划和 Gate 3 总计划更新；
- [x] G3.E0 派生状态仍为 `deny_g3_1`。

## 13. 删除独立性与 no-backcompat

- 不导入旧 WAJE 实现；
- 不保留当前 placeholder Evidence/Answer写入口的兼容路径；
- 现有测试/fixture直接切换到 G3.5当前合同；
- production Evidence与 settled/delivery入口保持关闭；
- 删除全部历史代码和历史测试后，`vnext/` 仍可构建、测试、运行；
- clean-copy/package-only验收继续使用 Python 3.12.13。

## 14. 风险与访谈触发点

| 风险 | 默认处置 | 是否需访谈 |
|---|---|---|
| LLM claim扩大业务范围 | deterministic precheck拒绝，允许局部修订 | 否 |
| capability result改变window/exposure | admission拒绝；需要新 Frame时另开 revision | 只有业务含义需重选时 |
| conformance结果伪装production | realm/registry/DB约束拒绝 | 否 |
|局部 Evidence缺口 |局部 claim blocked/provisional | 否 |
| claim 内容评分阈值 |留给 Gate 5/G3.6 | 若提前改变发布政策则问 |
| physical QuerySpec设计 |留给 Gate 4 | 若 G3.5必须提前选择则问 |
| settled/delivery | Gate 3硬拒绝 | 若要改变产品阶段则问 |

## 15. 实施与审查顺序

1. 对抗审查本计划，关闭权威、存储和测试 Blocking；
2. 先写 contract/property/mutation tests；
3. 实现 pure compilers/validators；
4. 实现 InMemory atomic bundle；
5. 实现 PostgreSQL migration/repository；
6. 接 controller/effect recovery；
7. 实现 provisional Answer 与 Workflow projection；
8. 跑全量和 disposable PostgreSQL；
9. 三路独立终审；
10. 更新父计划与实施审查；
11. 单一 G3.5 commit、ready PR、CI通过后合并。

提交不混入 G3.6、Gate 4 或 Gate 5 实现。

## 16. 计划对抗式审查

三路独立审查初始发现：

- authority/contract：Blocking 7、Major 5；
- storage/async：result 过早 terminal、迟到结果无法审计、caller 可伪造
  satisfaction/report、projection 无 reducer；
- black-box tests：必须逐 claim、局部降级、八类业务 world、crash/race 和四轴合法状态；
- 主智能体自审：receipt mutable 状态、T1 current-head 误杀迟到结果、satisfaction branch、
  projection cursor/head 和 Answer narrative 局部修订边界不完整。

计划修订后关闭方式：

1. 旧 Evidence/Answer skeleton 直接替换为当前合同；
2. T1 result landing 与 T2 Evidence disposition 强制分离；
3. T1 按 sealed dispatch authority 保存迟到结果，T2 按 current heads fail closed；
4. receipt immutable，disposition 从 admission/checkpoint/job事实派生；
5. validity/satisfaction 使用单 successor append-only current-head chain；
6. EvidenceUseBinding、claim precheck、settlement report全部由 shared pure compiler重算；
7. provisional Answer采用 candidate admission，系统不改写 narrative；
8. Workflow 使用 typed reducer、cursor application receipt、immutable snapshot 和 CAS head；
9. Context/Interpretation 只暴露 currently usable Evidence；
10. conformance positive path与 production disabled path分离；
11. InMemory/PostgreSQL调用同一 validators/compilers；
12. 24 个业务 world只验证可接受结果空间。

修订后设计审查计数：Blocking 0、Major 0。进入实现。

## 17. 实施证据

已完成的当前合同：

- T1 原子持久化 `CapabilityResultEnvelope`、`EvidenceRecord`、
  `CapabilityResultReceipt` 与对应 journal lineage；T1 不终结 obligation job；
- T2 从持久化 receipt 和当前 accepted authority 重算 admission、validity、
  satisfaction 与 terminal disposition；同一 receipt 重投返回同一 canonical disposition；
- correction 位于 T1/T2 之间时，旧结果保留审计，admission 与 job disposition 进入
  superseded/rejected；
- Answer candidate 必须逐字绑定已持久化的 `propose_answer` typed action；
- accepted provisional Answer 进入 `waiting_for_review`，无法进入 settled、completed 或
  delivered；
- settlement request 逐项核对持久化 RunTraceManifest、当前 Reviewer objection heads、
  latest Evidence validity、obligation satisfaction 与 claim use；
- Workflow adapter 使用封闭 event policy，从 immutable authority record 解析 journal，
  通过 cursor CAS 持久化可重建四轴 read model；
- Evidence validity 被撤销后，系统追加新的 satisfaction fact，Workflow obligation 轴重新
  打开为 blocked；
- migration 006 在数据库层拒绝 production Evidence 冒充、settled/delivered/completed
  状态和 immutable authority 更新。

实施期间额外关闭的跨层问题：

1. dispatch journal 曾引用 logical `dispatch_id`，PostgreSQL resolver 按
   `dispatch_record_id` 查找；现统一引用 immutable dispatch record；
2. PostgreSQL Workflow replay 曾按不存在的 `completed_at` 列排序；现按持久化
   completion record 的 `created_at` 与 ID 确定性排序；
3. `FrozenJson` 的递归 typed decoder 曾无法读回合法嵌套模型结果；现按通用递归 JSON
   合同解码；
4. T1 曾只记录 result-landed event，RunTraceManifest 无法证明 Evidence lineage；现同一
   事务追加独立 `EVIDENCE_RECORDED` fact；
5. settlement 曾接受 caller 自报 trace completeness 与 Reviewer refs；现 repository
   对 persisted manifest 与当前 objection heads exact replay，任何漂移直接拒绝。
6. InMemory 曾允许 forged conformance provenance；现两种 adapter 都从持久化
   execution spec/attempt 重建 provenance 并 exact 比较。
7. falsification/reversal disposition 曾可由调用方自报；现 repository 不接收 positive
   check map，缺少 trusted persisted check execution 时相关 claim 固定 fail closed。
8. ObligationSatisfaction 曾混入 claim-use binding；现 satisfaction 只表达 obligation
   fulfillment，claim consumption 由 EvidenceUseBinding/ClaimPrecheck 独占，settlement
   exact 比较 precheck 封存的 closure。
9. T2 重放曾返回后续 latest validity/satisfaction，形成混合时间点 outcome；现重放返回
   首次 canonical T2 outcome，latest head 由独立读取接口提供。
10. worker 曾可用 backdated `received_at` 绕过 lease expiry；现 InMemory 使用注入的可信
    clock，PostgreSQL 使用事务时钟，审计时间不参与 fencing 判定。
11. repository 曾不重算 schedule ID；现 controller、runtime lookup、InMemory 与
    PostgreSQL 共用 canonical builder，绑定 run、Plan adoption 与业务权威哈希。
12. correction 只推进 mailbox epoch 时，settlement 曾未标记旧 Answer stale；现消息、
    Question/Frame/Plan 与 active Frame candidate continuity 独立校验，Evidence、
    obligation 与 Reviewer 进展分别由各自持久化 closure 校验。

本地验证：

- G3.5 专项：113 tests；普通运行跳过 13 个显式 PostgreSQL 条件测试，随后均由 disposable
  runner 实际执行；
- disposable PostgreSQL：9 个 migration/constraint tests、7 个完整 storage/replay
  tests、10 个 fault/race tests 全部通过；
- fault/race 覆盖 T1/T2 每类 durable write rollback、ACK 丢失、跨连接恢复、双 worker、
  correction 与 Answer/settlement、validity revoke 与 Answer、projector cursor CAS；
- 24 个业务 world 全部执行 typed Frame/Plan → Evidence T1/T2 → claim precheck →
  provisional Answer → Workflow；运行输入与期望评分相互独立；
- 全量 Python：438 tests，35 个显式 provider/PostgreSQL 条件测试跳过；
- contract schema、TypeScript binding、measurement identity 与 Python compile 全部通过。
- deletion-independence clean copy 使用 Python 3.12.13 重建环境、生成 wheel、运行 438
  tests 与 health smoke，全部通过；
- G3.E0 verifier 通过自身完整性检查并继续派生 `deny_g3_1`。

终轮组合对抗审查：

- authority/security：Blocking 0、Major 0；
- async/PostgreSQL：Blocking 0、Major 0；
- tests/docs/CI：Blocking 0、Major 0。
