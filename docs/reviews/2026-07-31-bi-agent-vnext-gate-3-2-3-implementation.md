# WAJE BI Agent vNext G3.2 / G3.3 实施与验收

## 0. 结论

G3.2 durable runtime amendment 与 G3.3 measurement algebra/resolver 的本地实现已完成。
本包把开放业务语义留给 typed LLM，把 authority admission、并发 fencing、日历解析、
exposure、unit、boundary 和 obligation identity 放在可重放的确定性合同内。

本结论只覆盖 G3.2/G3.3 local implementation：

- G3.4 的 accepted WorkPlan adoption 与 logical QueryBindingEnvelope 尚未开始；
- G3.5 的 capability-native Evidence admission、claim continuity 与四轴 Workflow 尚未开始；
- G3.E0 的 external admission、人工独立双审、calibration、held-out、promotion 和 frozen
  run 继续由 readiness verifier 判定为 `deny_g3_1`；
- 本包没有创建 production EvidenceRecord、settled Answer 或 completed Workflow。

## 1. Entry interview

记录：**本 Gate 无需用户决策**。

用户已确认整体异步、authority commit 局部同步的 runtime 方向，也已确认开放日期与测量
语义由 LLM 自主提出。仓库可以确定本包的合同、并发与验收边界，无需补问产品判断。

## 2. G3.2：durable runtime amendment

### 2.1 Message authority

所有用户输入先在同一个短事务内形成：

1. durable mailbox message；
2. `MessageIngressRecord`；
3. `PendingUserMessage`；
4. message-binding outbox job；
5. journal event 与 checkpoint。

`MessageImpactBinding` 保存 typed source spans、material assertions、ambiguities、recommended
clarification 与 source lineage。开放文本分类只在 provider 的 strict tool contract中产生；
controller 校验结构和 source span，并决定是否创建新的 `QuestionRevision`。

correction 先推进 mailbox authority epoch。已经在途的 Primary、Reviewer 或 effect result
可以保留审计轨迹，但 authority snapshot 不一致时只能形成 superseded disposition。

### 2.2 Frame candidate / review / closure

`revise_frame` 只创建 `FrameCandidateRecord`，不能直接接受 Frame。candidate 绑定：

- accepted QuestionRevision 与 MessageImpactBinding；
- candidate generation 与完整 Frame hash；
- 创建时的 `AuthoritySnapshot`；
- 已处理 objection ID 与修订说明。

Reviewer 在独立 durable job 中返回 `FrameReviewRecord`。同一次 review 不能一边提出 blocking
objection 一边接受 candidate。blocking review 后，Primary Agent 必须创建新的 candidate，
逐项引用旧 review 的精确 objection。system 重算两个 Frame 的 measurement-node diff，
确认 Reviewer 指出的节点确实变化，并生成 content-addressed closure derivation；新的
Reviewer review 通过后，system 生成
`FrameAdmissionProof`，repository 才允许 Frame CAS。

### 2.3 Worker lifecycle 与 fencing

每个 outbox job 绑定：

- `caseId`、operation/causation/correlation；
- accepted question/frame/plan heads；
- mailbox authority epoch；
- active candidate generation/hash；
- obligation/evidence/contradiction state versions；
- immutable payload hash。

worker 使用 DB-backed lease、heartbeat 和单调 fencing token。authority commit 的同一事务会
锁定并校验 owner、token、active、DB clock 和 expiry；旧 worker 在任何业务写入前失败。

每个 job 最终只有一种 `JobDispositionRecord`：

- `completed`；
- `superseded`；
- `terminal_failure`。

provider 永久失败或 provider 层统一重试耗尽时，attempt receipts、terminal disposition、
`JOB_TERMINALLY_FAILED` event 与 `blocked` checkpoint 原子提交。dispatcher recovery cursor
只记录扫描高水位并只能单调前进；pending recovery 始终根据 terminal disposition 查询，
不会因高水位跳过未终结的旧 job。

mailbox 按 sequence 逐条 binding。新 correction 可以立即推进 authority epoch 并 fence
在途业务结果，同时不会覆盖尚未完成 semantic binding 的中间消息。上一 run 到达
`COMPLETED` 后，同一 case 可以创建新 analysis cycle；每轮 correlation 保持独立 trace。

### 2.4 Strict provider boundary

Primary、message binding 与 Reviewer 都使用：

- strict typed tools；
- exactly one tool call；
- `parallel_tool_calls=false`；
- refusal/incomplete/multiple-call/terminal/retryable attempt 分类；
- logical model job 与 provider attempts 分离持久化；
- provider 调用在数据库事务外。

G3.2-owned case/run/operation/revision/candidate/binding/review/job ID 均由 controller 生成。
accepted question ID 从 `revise_frame` provider schema 中移除，controller 在 typed output
通过 source/schema 校验后注入。task 与 claim identity 分别属于 G3.4/G3.5。

production provider 需要安装 durable attempt observer：每次 outbound 前写
`ProviderAttemptRequest`，每次返回后写 receipt，成功的 typed output 先写
`DurableModelResult`。authority transaction 崩溃时，同一 logical job 直接复用该结果。
Primary 与 Reviewer 必须使用不同的可审计配置；同 vendor 的独立配置仍符合当前 independence
policy。

### 2.5 Obligation scheduler 与 trace

纯函数 scheduler 从 immutable obligation DAG 与 authority snapshot 选择全部可运行节点；
durable coordinator 把 schedule、dispatch、completion、checkpoint、journal 与 outbox
落入同一 authority store：

- independent obligations 同时 runnable；
- schedule 与首批 dispatch/outbox 原子提交，completion 与后继 fan-out 原子提交；
- completion 顺序不改变业务 authority；
- duplicate completion 幂等，冲突 duplicate 拒绝；
- correction、Frame/Plan revision 或 candidate generation 变化使旧 completion 失效；
- correlation 从 active durable run 派生，调用方不能另造 run lineage；
- typed boundary、failed/superseded prerequisite 与 executable obligation 分开处理，无法
  满足的下游会形成持久化 terminal completion。

`RunTraceManifest` 从持久化记录重建 ingress、binding、candidate、review、model attempts、
durable typed result、candidate supersession、plan、resolution、obligation、effect attempt、
Evidence、claim 与 provisional Answer lineage，并把每轮真实 journal operation lineage
纳入完整性 hash。引用在写入时与 storage 逐项核对，不能提交只自洽但不对应事实表的 manifest。

## 3. G3.3：measurement algebra 与 resolver

### 3.1 输入与确定性边界

resolver 只消费 content-addressed `CalendarResolutionRequest`：

- immutable calendar coverage receipt；
- snapshot/release/watermark refs；
- inspection evidence refs；
- per-window `ExposureCoverageFact`；
- timezone、business-day cutoff 与 resolution anchor；
- 完整 input bundle hash。

request 需要先由 `TrustedResolutionInputRegistry` 接纳。resolver 会重新核对 target-period
范围、anchor、as-of、release instant、coverage watermark、late-arrival cutoff、
calendar/business-day receipt 与 unit-registry receipt。registry 由配置在 resolver composition
root 的 Ed25519 signer 签发、verify-only public key 验签，`ResolutionContext` hash 也必须
被同一 registry 接纳；
caller 自签的 bundle、registry 或替换后的 timezone/cutoff/as-of 无法执行。
resolution outcome 进入 authority store 前，还必须携带 composition root 签发的
`MeasurementResolutionAdmission`。签发前 resolver 对 Frame、request、context 与 registry
执行一次精确重放；store 只持有公钥，核对 outcome/frame/estimand/input/context/registry
identity 和 Ed25519 receipt。outcome 与 receipt 在同一事务中不可变持久化。普通内容 hash
全部被重新计算，也无法把伪造 boundary 写成受信结果。

同一输入产生同一 resolution/proof identity。Frame、coverage、timezone、calendar version、
snapshot 或 exposure 任一实质变化都会改变 resolution identity；伪造已有 hash 的修改会被
拒绝。

### 3.2 ClaimTarget validator

13 个 `ClaimTargetKind` 都有独立 validation contract：

- definition；
- data quality；
- point quantity；
- distribution；
- temporal pattern；
- contrast；
- composition；
- accounting decomposition；
- cohort outcome；
- funnel transition；
- association；
- causal effect；
- diagnostic set。

validator 检查各 target 必需的 metric、population、time、contrast、risk set、sequence、
reconciliation、relationship、identification 与 evidence closure。invalid graph 在 Frame
acceptance 前失败，不能转换成“数据不足”。

### 3.3 Calendar 与实际时间

resolver 分开实现 absolute、relative period、rolling interval 与 business calendar：

- Gregorian day/week/month/quarter/year；
- period offset 与跨年；
- first/last-N、ordinal、rolling、开闭边界；
- IANA timezone 与业务日 cutoff；
- local date set 与 UTC 半开区间；
- DST gap/fold 使用 UTC instant 计算真实 23/25 小时；
- versioned fiscal/business/holiday rule 通过 typed registry 扩展。

resolved window 保存 actual start/end、calendar days、UTC instants、coverage 和 derivation
proof。unsupported calendar contract形成受信 typed boundary，不会静默替换成 Gregorian。

### 3.4 Exposure 与 unit

calendar window 和 exposure fact 分开建模。每个 window 分别保存：

- expected；
- observed；
- valid；
- invalid；
- missing；
- at-risk。

`UnitExpression` 保存 dimension、currency、scale、per-unit 与 conversion version。系统机械
从 metric 的 numerator/denominator variable 推导 output unit，再验证
`metric unit / exposure unit = estimand output unit`。estimator、estimand 与 requirement
必须绑定同一个 exposure。raw total 在 unequal exposure 时只能成为辅助信息；主 contrast
会形成 `incomparable_exposure` boundary，无法编译成 executable obligation。

normalization、aggregation order、zero exposure、missing exposure、minimum coverage 与
component reconciliation 都由 estimator/exposure contract 执行。显式
`ALLOW_PARTIAL_WITH_EXPOSURE` 只有在 calendar 与 exposure thresholds 都满足时才能执行。
ratio-of-sums、
mean-of-ratios 与 weighted mean 产生不同但可重放的 proof，capability 不能自行替换。

### 3.5 Boundary 与 obligation

boundary code 来自 versioned registry，声明适用 claim、required proof、claim ceiling 和
policy。Frame 只能请求 boundary policy：

- source-backed 且合同可用时禁止 boundary escape；
- `BLOCK` policy 生成 blocked obligation；
- known gap 不能生成量化 Evidence；
- boundary 按 requirement 局部生成，不会取消同一 estimand 的其他 executable requirement。

requirement compiler 从 accepted Frame requirement 与 resolution outcome 单向生成 immutable
`ResolvedEvidenceObligation`。obligation definition 不保存 fulfillment state，重复编译产生
相同 identity。

## 4. 竞争与回归矩阵

| 风险 | 验证 |
|---|---|
| correction-vs-Primary/effect/Reviewer | authority snapshot 与 epoch fence；旧结果 superseded |
| lease expiry/takeover | 旧 token 在 commit transaction 内被拒绝 |
| heartbeat failure | provider result不能进入 authority |
| crash before binding/review commit | 复用 durable typed result，模型调用不重复 |
| duplicate delivery | outbox terminal disposition唯一 |
| parallel obligations | 全部独立节点同时 runnable |
| out-of-order completion | 3 个节点的全部 6 种顺序结果一致 |
| Frame review objection | 无新 candidate + 可重算 node diff + fresh review 时拒绝 acceptance |
| provider malformed output | attempt trace + terminal disposition + blocked checkpoint |
| cross-month/year/leap/DST | actual date set、UTC interval 与 exposure proof |
| missing/incomplete coverage | typed boundary、blocked 或降级，禁止 fabricated instance |
| identity drift | Frame/resolution/obligation mismatch fail closed |
| provider/result durability | outbound 前有 request；authority commit crash 后 provider 调用次数保持 1 |
| mailbox burst | 所有 sequence 逐条 binding，无 latest-wins 丢失 |
| case multi-run | 终态后新 analysis cycle 独立 trace，旧 manifest 仍可重放 |
| durable obligation restart | 原子 schedule/dispatch 与 completion/fan-out；重启不重复工作 |
| obligation terminal propagation | failed/boundary/superseded prerequisite 终结全部依赖节点 |
| trusted resolver admission | Ed25519 signer/verify-only 分权；bundle、source receipt、ResolutionContext 共同验签；持久化要求 exact-replay admission receipt |
| storage bypass | repository 重算 worker/system completion；伪造 `system-prerequisite` 被拒绝 |
| late stale completion | worker 回写触发整张旧 schedule 的 superseded completion、disposition 与 checkpoint |

## 5. 验证证据

本地 Python：3.12.13。

- `npm run check:contracts`：通过；
- `npm run test:bootstrap`：285 tests passed，9 个 PostgreSQL integration tests 因当前
  worktree 未配置 `WAJE_VNEXT_DATABASE_URL` 而 skip；
- G3.2/G3.3 direct focused suite：48 tests passed；
- G3.1/G3.2/G3.3 authority-focused suite：67 tests passed；
- `npm run check`：clean-copy、Python 3.12.13、wheel build、contract generation、285 tests、
  health 和 legacy isolation 全部通过；
- `npm run run:bootstrap`：通过；
- `npm run check:eval-corpus:gate3`：通过；
- `npm run check:eval-views:gate3`：通过；
- `npm run check:evals:gate3`：verifier 本身通过，derived readiness 仍为 `blocked` /
  `deny_g3_1`。

PostgreSQL adapter、migration 004 和对应 conformance tests 已实现；正式 PR/CI 若配置数据库，
应运行全部 9 项 PostgreSQL integration tests。

## 6. 删除独立性

clean-copy verifier 仅复制 `vnext/` 与受 policy hash 约束的最小 `.github/` projection，
随后重新创建 Python 3.12.13 venv、安装 locked dependencies、生成/核对合同、构建 wheel、
执行 285 tests 并运行 health command。结果通过，证明本包不依赖历史 WAJE runtime、fixture
或测试。

## 7. 对抗式自审

第一轮组合审计打开的主要问题已逐项落在实现：

- rich authority snapshot；
- lease-token commit guard；
- fresh-candidate Reviewer closure；
- typed semantic binding；
- logical model job / provider attempt split；
- trusted content-addressed resolver input；
- calendar/exposure 分离；
- DST UTC interval；
- typed unit algebra；
- versioned boundary registry；
- requirement-local obligation compiler。

fresh 三路只读审查重新核对 runtime、measurement 与跨层组合边界。第二轮发现并关闭：

- caller-self-signed resolver input、target/as-of/release 与 unit registry 漂移；
- exposure threshold、aggregation、zero/missing policy 未进入执行代数；
- mailbox latest-wins、material ambiguity accepted、candidate correction 残留；
- provider result 在 authority commit 前缺少 durable receipt；
- objection closure 依赖模型自报 proof string；
- obligation scheduler 只有纯函数、缺少 durable outbox/checkpoint；
- run correlation 与 trace 引用未覆盖 multi-run 和 durable result。

第三轮反例重放进一步关闭：

- resolver registry 可由 caller 自签、ResolutionContext 不在 admission identity；
- partial-period policy 被 calendar completeness 短路；
- estimator/estimand/requirement exposure 分叉与 metric input unit 伪装；
- raw-total unequal exposure 仍能编译 executable obligation；
- requirement target 与 estimand reference 单向漂移；
- schedule/首批 dispatch 和 completion/后继 fan-out 的 crash gap；
- schedule correlation 可由 caller 注入；
- executable obligation 冒充 typed boundary，以及 failed/superseded prerequisite
  造成永久 pending。

第四轮存储与信任边界重放进一步关闭：

- 旧 authority 的 worker completion 只抛错、没有主动终结 stale schedule；
- caller 绕过 coordinator，直接用伪造的 `system-prerequisite:*` completion 写 repository；
- caller 重算 boundary proof 和 outcome identity 后，尝试替换 failed/inspection refs；
- caller 同时注入自建 registry 与 verifier，绕过 composition-root trust ownership；
- stale result 首次被 supersede 后，同一 at-least-once delivery 重放被误判成冲突；
- 对称签名让 verifier 同时具备签发能力，且验证后的 receipt 没有持久化。

最终边界由 repository graph re-derivation、幂等 late-result supersession、Ed25519
signer/verify-only 分权、exact-replay `MeasurementResolutionAdmission` 与 receipt/outcome
原子持久化共同执行。

G3.4/G3.5 ownership 的工作继续保持关闭，本包没有提前实现 physical QueryBinding、
Evidence admission 或 settled Answer。
