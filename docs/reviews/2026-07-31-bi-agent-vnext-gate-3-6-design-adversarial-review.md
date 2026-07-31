# WAJE BI Agent vNext G3.6 设计对抗审查

> 日期：2026-07-31
> 状态：Plan reviewed；implementation findings open

## 结论

G3.6 的 behavior-first、Reviewer-centric、两条真实 provider lane 和三层 strict-AND 方向
成立。当前可以实施运行权威合同，真实模型 full matrix 需等待评测权威和 runtime 前置缺口
关闭。

本轮由三条独立审查线检查：

- eval authority、formal/dev 状态、grader 自证和 held-out；
- corpus、ClaimTargetKind、开放结果空间、relation 和 Reviewer calibration；
- durable async、PostgreSQL、provider recovery、trace 和 Lane A/B 可达性。

## Blocking findings

| ID | Finding | Disposition |
|---|---|---|
| G36-AR-01 | per-cell hash index 可由 runner 自证，缺受保护执行回执 | G3.6.0 增加 external execution receipt；formal 本地永远 blocked |
| G36-AR-02 | run manifest 缺 lane/paraphrase/repeat/seed/role/trace 坐标 | G3.6.0 增加 execution manifest 与 policy compiler |
| G36-AR-03 | 同一 cell 可丢弃失败 attempt 后重跑 | append-only attempt journal + first terminal selection |
| G36-AR-04 | protected held-out 未进入 executable universe | protected expansion receipt + opaque cells |
| G36-AR-05 | 144 claim targets 无 typed kind，COMPOSITION 真实覆盖为 0 | typed epoch 已关闭分类与独立世界计数；复合 target 语义拆分仍 open |
| G36-AR-06 | mutation/property/schedule 无 relation authority | operator registry + relation group/result + exact suite set |
| G36-AR-07 | SuiteResult 只数 cell | 增加 relation、trace、critical、historical、coverage 分母 |
| G36-RT-01 | Agent 看不到 calendar/release/data-contract world | AgentWorldView authority 进入 binding/ContextPacket |
| G36-RT-02 | low-risk inference 无法进入 DecisionLedger | typed inference → immutable DecisionRecord → Frame refs |
| G36-RT-03 | accepted Frame 后无 production resolution/obligation stage | 建立 durable resolution worker，harness 只供 source authority |
| G36-RT-04 | provisional Answer Reviewer outbox 路由到 Frame Reviewer | typed AnswerReview worker 和独立 dispatch route |
| G36-RT-05 | provider config/thinking/prompt 不在 durable job identity | ModelConfigurationIdentity 进入 job/outbox/attempt/result/trace |
| G36-RT-06 | success receipt 与 typed result 分事务产生 crash window | 原子 provider success commit 或可取回幂等 response |
| G36-RT-07 | test realm 由 caller DSN/字符串自报 | registry-issued RunRealmContext + store attestation |
| G36-RT-08 | parallel obligation/projector/sensitivity worker 不完整 | 独立 workers、selected sensitivity identity、safety+liveness |
| G36-TR-01 | trace_complete 和任意 hash 可自证 | storage-backed artifact/trace/invocation exact verifier |

## Major findings

- 当前来源池缺 expert business cases，8 个 real-user cases 只来自一个 source task。
- 36/36 全部为 multi-estimand，缺单 estimand、低上下文和简单开放问题。
- authored valid-design examples 仍可能被误当 whitelist。
- coverage ledger 汇总作者标签，部分 conversation tag 与结构事实矛盾。
- deterministic hard-check verdict 若提前给 Evaluation Reviewer，会造成锚定和双计分。
- A/B dossier 只覆盖跨月 world，固定七天 reference 本身带偏，无法推广。
- model invocation schema 需无损表达 retry/refusal/incomplete/superseded。
- live Gate 2 runner 和 direct-urllib quality probe 都不能作为 G3.6 lane evidence。

## 已落地的第一批修复

- G3.6 持久化计划与 Gate entry；
- provider thinking 进入 request 和 configuration identity；
- execution manifest、attempt journal、trace、model invocation、cell/suite result、relation
  result 与 protected execution receipt 初始合同；
- authority/implementation hard-check result 按 grader registry exact set 派生，cell 无法自报
  两层 pass；
- mutation operator registry；
- Reviewer 五维机械 verdict 映射；
- local execution 与 formal admission 双状态；
- smoke/slice/full run mode、coverage admission 与 `runner_self_attested` 本地证据标记；
- canonical attempt policy、first-terminal disposition、runner executable release hash；
- counterfactual materialization 后重新编译 Agent/Evaluator views；
- invocation 的 case/correlation/authority snapshot/typed output 与 trace artifact 绑定；
- 同一角色允许多个独立 logical jobs，单个 logical job 仍只允许一个成功输出；
- terminal attempt 与 cell 的 artifact-set identity 改由完整 TraceArtifactIndex 规范 hash 重算；
- 144 个 base 与 12 个 replacement claim targets 由独立 registry 绑定 13 类
  `ClaimTargetKind`，候选 Episode 自行改类型会被拒绝；
- business world independence 从 outcome authority refs 派生；36 个 Episodes 归并为 20 个
  authority sets，同一 snapshot 的多道题只算一个世界；
- 每类 ClaimTargetKind 当前有 3–16 个 base authority worlds、3–20 个 executable variant
  worlds；coverage floor 由 policy 拥有，ledger 与 suite admission 按 independence key 去重；
- authored design 固定为非穷尽示例，开放设计由 must-preserve、required investigation、claim
  ceiling、evidence boundary 和 forbidden outcomes 判定；Agent view 不接收这些 evaluator 字段；
- product Reviewer 必须声明并覆盖 grader registry 的 exact predicate set；
- critical/historical 分母按 unique Episode 统计，repeat 不再放大业务覆盖；
- 缺 cell、重复 cell、profile drift、trace stage/cycle、attempt terminal selection、hard-check
  缺项与 relation member 漂移的单测。
- risk × lane × paraphrase × repeat policy 已编译成 exact execution universe：36 Episodes、156
  case variants、1,172 个坐标、2,011 个 Episode relation groups；manifest 绑定 compiler、两类
  authoring registry 和两个 exact-set hash；
- relation authority 已从 cell 单值提升为顶层多成员集合，同一 cell 可同时参加 outcome、
  paraphrase 和 mutation 判定；
- execution-universe readiness 已进入 G3.E0 evaluated artifact 与 verifier release，删坐标、删
  relation、改 policy、伪造 paraphrase/operator hash 的攻击测试均 fail closed。
- wording paraphrase 与 meaning-preserving case mutation 已拆成两个 operator；base/sibling
  relation 只在 canonical wording 上配对，避免独立改写引入混杂；
- smoke/slice 同样执行 used-authority readiness：pending paraphrase、pending scenario、不可用
  executor、错配 sibling operator、跨 repeat/turn/paraphrase relation 都会被拒；
- paraphrase review 绑定 source/candidate pair，scenario review 绑定 scenario core；compiler 与
  runner release identity 已纳入实际 import 的 view/counterfactual compilers；
- 手工推导的 high-risk micro-universe 固定 4 个 variants、20 个 coordinates、37 个 relations，
  与全量 compiler/property/shrink tests 分离，避免所有期望都由被测编译器反推；
- 19 类专项 operator 每类至少覆盖 2 个独立业务世界，共 38 个 authoring slots；measurement
  mutation 同时要求 semantic/frame 与 full-authority lane，其他类型要求 full-authority lane。

复审确认仍 open 的 Blocking：

- production durable model job 尚未绑定 thinking、exact prompt/tool/request/config identity；
- provider success receipt 与 typed result 尚未原子持久，crash window 未关闭；
- local hard/relation observations 仍由 runner 产生，需 protected executor 从 artifact bytes 重算；
- scenario subject 仍缺可重算的 `ScenarioApplicationReceipt`；当前
  `operator_scenario_executor_unverified` 永久阻断 development full run，后续只有独立 resolver
  registry、实际输入 artifact 与 receipt verifier 落地后才能移除；
- canonical corpus 已有 typed ClaimTargetKind、independence key 和 open-world contract；
  201 个 paraphrase authority slots 与 38 个专项 operator scenarios（19 类 × 至少 2 个独立
  业务世界）已精确列出但尚未 author/
  review；pairwise/higher-order coverage、expert/single-estimand/low-context/simple Episode、复合
  target 语义拆分仍 open；
- opaque held-out expansion、frozen run-cell 全链和 protected execution receipt verifier 尚未形成
  正向可满足路径；
- Primary invocation 仍未绑定可重算的 request artifact 与 AgentWorldView 类型，oracle isolation
  尚无执行证明；该项留在 G3.6.1；
- Evaluation Reviewer 仍能看到 illustrative valid-design examples，存在锚定风险；正式输入收敛
  取决于预注册 A/B 探针，当前不能宣称 Reviewer 对替代设计无偏；
- Lane A/B 尚未接入 production durable runtime，因此 full matrix 禁止启动。

最终冻结快照继续确认：`request_sha256`、provider response/receipt 和 causation 仍可由同一
synthetic dossier 自报；hard-check/relation observation 仍未从真实 artifact bytes 重算；
append-only attempt 历史尚未与 PostgreSQL journal 对账。以上限制由
`local_evidence_trust=runner_self_attested` 和 formal fail-closed 明示，不得把本地合同测试
解读为真实模型运行证据。

这些实现仍属于 G3.6.0 foundation。上表 open Blocking 未全部关闭，不能开始 full matrix，也
不能声明 local G3.6 complete。

## 审查决定

本 Gate 无需新增用户决策。所有 open finding 都属于已确认目标下的通用工程闭环。实施顺序
采用：corpus/manifest authority → AgentWorldView/DecisionLedger/resolution → provider atomic
recovery → Answer/obligation/projector/sensitivity workers → trusted realm/trace → Lane A/B →
Reviewer calibration/full matrix。
