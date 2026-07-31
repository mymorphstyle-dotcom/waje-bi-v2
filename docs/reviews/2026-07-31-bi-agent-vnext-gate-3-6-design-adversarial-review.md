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
| G36-RT-05 | provider config/thinking/prompt 不在 durable job identity | runtime identity 与 local eval bridge 已关闭；protected source proof 仍 open |
| G36-RT-06 | success receipt 与 typed result 分事务产生 crash window | 本地事务已关闭；provider 端 outcome unknown 仍需幂等/response lookup 证明 |
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
- runtime model execution 必须携带完整 attempt history；仅成功 result 可生产 trace stage。
- live Gate 2 runner 和 direct-urllib quality probe 都不能作为 G3.6 lane evidence。

## 已落地的第一批修复

- G3.6 持久化计划与 Gate entry；
- provider thinking 进入 request 和 configuration identity；
- execution manifest、attempt journal、trace、runtime model execution、cell/suite result、relation
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
- runtime logical job 已绑定 execution role、provider endpoint/protocol、model/thinking、稳定参数、
  adapter release、input view、typed request、prompt、tool、output contract、decoder 和实际
  provider request bytes；controller 会从 typed state 重算整套 identity 并在 transport 前拒绝
  drift 或隐藏字段；
- provider attempt 使用稳定幂等键，InMemory/PostgreSQL 会在同一事务提交 success receipt 与
  typed result，并拒绝跨 job prior、request/config drift、第二个成功结果和单边 success；
- retryable receipt 后恢复从 durable `N+1` 继续，总预算不随进程重启增加；terminal receipt
  直接完成失败，unreceipted request 进入 `outcome_unknown` 且不会自动重发；
- prompt/tool/input/output/decoder/provider body 由 controller 侧受信 compiler 重算；stable
  parameter exact set、role-neutral operational independence 和 durable endpoint/timeout dispatch
  已纳入攻击测试；
- OpenAI-compatible configuration 必须与 sealed adapter settings 完全相等；伪造 endpoint、
  timeout、model 或 adapter identity 都会在 transport 前失败，未登记 adapter 不进入 job；
- 三角色 provider factory 已固化用户确认的三组临时配置，三个 configuration hash 独立；
- migration 007、495 个 Python tests（36 skipped）、41 个 G3.6 runtime/eval authority tests、
  contract checks、27 个加载本地配置的 disposable PostgreSQL migration/storage/race tests 和
  Python 3.12.13 clean-copy build 已通过。

复审后已关闭的 local Blocking：

- eval 已采用 runtime configuration identity 与 per-job request artifact，quality-probe prompt
  hash 不再充当 production invocation identity；
- `RuntimeModelExecution` 携带完整 job/request/receipt/result set，重算所有内部 hash 与 retry
  连续性；
- TraceArtifactIndex v2 通过 durable result source 和 stage producer registry 阻止 wrong-stage
  artifact；
- runtime-implemented typed request 必须由 production decoder/compiler 原样重放；简化占位输入、
  prompt/tool/body 复制规则和隐藏字段已从正向 fixture 删除；
- persisted RunTraceManifest exact 绑定 model job、attempt request、receipt、result 与 journal lineage；
  跨 case/run、漏 job/request、额外未拥有 artifact、跨 cell 重复 id、错误时间顺序、历史残缺
  manifest 与 malformed schema 均被拒绝；
- lane stage graph 与六个 model stage 的完整 15-field producer capability tuple 使用独立代码基线，
  registry 无法通过删除 Reviewer、改 predecessor、缩减 producer 字段或自报 implemented 来降低
  测试要求；test-double 永远不能进入 execution admission；
- results、relation results、findings、authority 和 per-cell artifact/runtime/check maps 的畸形输入
  均安全派生 invalid/blocked；ghost/missing cell keys 无法绕过 exact-set closure；
- event-sourced stage 精确绑定 journal event type、cursor、authority/action 与 event bytes；Primary
  Frame proposal、Reviewer candidate 和 acceptance event 必须指向同一 Frame authority。

复审确认仍 open 的 Blocking：

- 本地 success pair 已原子提交，稳定幂等键会保留给后续 provider 对账；当前 unreceipted
  request 会停在 `outcome_unknown`。provider 幂等承诺或按键查询能力仍缺真实证明；
- 内置 request compiler 和 durable dispatch 参数已封闭应用层 drift；transport release、实际
  sent-bytes receipt、签名代码加载与 oracle-store 权限仍依赖 protected executor。任意同进程
  恶意代码可以修改 Python 模块状态，该威胁不能靠同一解释器内的对象校验封闭；
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
- Primary runtime invocation 已绑定可重算 request artifact 与 AgentWorldView 类型，并会拒绝
  clean-view label 下的隐藏 oracle；protected principal 和 eval-side execution proof 仍未形成；
- Frame proposal、review、candidate 与 frame-accepted event 已做同一 Frame identity 核验；
  admission proof → accepted frame/head 的完整 byte-level replay 仍待接入；
- canonical `runtime_review` 和 `evaluation_review` producer 明确标记为 `unprovisioned`，因此
  full-authority manifest 会在入口 fail closed；test-double 只能验证合同，不能生成 readiness；
- Evaluation Reviewer 仍能看到 illustrative valid-design examples，存在锚定风险；正式输入收敛
  取决于预注册 A/B 探针，当前不能宣称 Reviewer 对替代设计无偏；
- Lane A/B 尚未接入 production durable runtime，因此 full matrix 禁止启动。

后续 G3.6.1 修订已删除自由填写的 eval `request_sha256`/job/attempt projection。local eval 直接
消费完整 runtime record set，重算 request/config/result，验证完整 retry chain，并通过
TraceArtifactIndex v2 与 stage producer registry 阻止 wrong-stage 引用。PostgreSQL projector
使用一个顶层 `REPEATABLE READ READ ONLY` 事务读取 job、全部 request/receipt、result 和
RunTraceManifest；manifest materialization 同样使用一致读，admission 在独立顶层事务重新核验
引用。嵌套事务会被明确拒绝，历史残缺 manifest 也不能投影新结果。

本地 runner 仍可先编造整套数据库事实，再生成内部一致的 dossier；hard-check/relation
observation、actual artifact bytes、provider gateway raw response 与 protected assignment 尚未由
独立 principal 对账。以上限制由
`local_evidence_trust=runner_self_attested` 和 formal fail-closed 明示，不得把本地合同测试
解读为真实模型运行证据。

这些实现仍属于 G3.6.0 foundation。上表 open Blocking 未全部关闭，不能开始 full matrix，也
不能声明 local G3.6 complete。

## 审查决定

本 Gate 无需新增用户决策。所有 open finding 都属于已确认目标下的通用工程闭环。实施顺序
采用：corpus/manifest authority → AgentWorldView/DecisionLedger/resolution → provider invocation
authority → eval/runtime artifact bridge → Answer/obligation/projector/sensitivity workers →
trusted realm/trace → Lane A/B →
Reviewer calibration/full matrix。
