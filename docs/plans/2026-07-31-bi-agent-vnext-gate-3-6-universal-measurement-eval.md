# WAJE BI Agent vNext G3.6 通用测量行为评测计划

> 日期：2026-07-31
> 分支：`codex/gate3-6-universal-measurement-eval`
> 基线：`origin/main@ff9f3278`
> 状态：G3.6.0 in progress；real-provider full matrix 尚未开放

## 1. Gate entry

### 1.1 已查明事实

- G3.1–G3.5 已建立 Question、typed binding、Frame candidate review、accepted
  AnalysisFrame、Plan、effect、Evidence admission、provisional Answer、settlement precondition
  和 Workflow projection 的持久化权威链。
- G3.5 local closeout 为 438 个 Python tests，包含 disposable PostgreSQL、fault/race 和
  clean-copy 验收；G3.E0 仍派生 `deny_g3_1`。
- Gate 3 corpus 已有 36 个 WAJEgame Required Episodes、120 个可执行 counterfactual、
  41 个 case-file authorities 和三层 verdict 合同。
- 三个临时模型配置已经过质量探针并由用户确认：
  - Primary Agent：`deepseek-v4-pro`，thinking enabled；
  - Runtime Reviewer：`deepseek-v4-pro`，thinking disabled；
  - Evaluation Reviewer：`deepseek-v4-flash`，thinking enabled。
- 三个配置都保持 `quality_probe_only`。当前团队只有一个可执行审查账号，无法完成正式
  双人独立校准、protected held-out 和外部 admission。
- 现有 per-cell result validator 能阻止三层互相抵消，也能绑定 frozen run cell 与 runner
  artifact index。现有 run manifest 没有表达 lane、paraphrase、repeat、seed、角色配置、
  trace profile、每轮输入 view 和整套完成证明。
- 现有真实 provider adapter 使用同一生产 typed-action 路径，支持 durable provider attempt
  observer 和 Primary/Binding/Reviewer 独立配置。G3.6.0 已让 `thinking` 进入通用 provider
  settings、request 与 configuration identity；durable logical job 仍未绑定 exact prompt/tool/
  request identity。
- G3.6.0 execution-universe compiler 已从当前 policy 与 36 个 Episodes 派生 156 个
  base/counterfactual case variants、1,172 个必跑坐标和 2,011 个 Episode relation groups。
  当前缺 201 个 reviewed paraphrase slots 与 38 个专项 operator scenarios（19 类 × 至少 2 个
  独立业务世界），因此 development
  full universe 明确为 `blocked`，smoke/slice 仍可做合同开发。

### 1.2 访谈判断

**本 Gate 无需用户决策。**

模型角色、Reviewer-centric 评分、八类 WAJEgame 业务问题、合理设计空间、评测来源池、
三层 verdict 和正式校准边界均已有明确决定。开发运行可以继续；任何本地结果只能形成
development evidence，不能解除 G3.E0 的人工与外部控制 blocker。

若实现期间出现以下情况，暂停受影响分支并一次只问一个问题：

- 需要改变八类业务问题或 launch acceptance floor；
- 需要把某个业务定义、唯一窗口或唯一调查路线提升为产品权威；
- 需要改变 Reviewer veto、claim ceiling 或 settled publication 边界；
- 需要把当前单账号审查声明为正式独立校准；
- 需要读取、重建或写入 production 数据与 production Evidence。

## 2. G3.6 的目标

G3.6 验证同一 Primary Business Analysis Agent 面对开放业务问题时，能否：

1. 保留用户的决策目标和业务语义；
2. 形成专业、显式、可执行的测量设计；
3. 根据证据条件动态调查、局部修订和局部降级；
4. 让 Frame identity 原样贯穿 Plan、effect、Evidence、claim 与 Workflow；
5. 在歧义、缺合同、部分覆盖、纠正、并发和恢复条件下保持诚实；
6. 由独立配置的 Reviewer 对测量质量和业务有用性评分；
7. 由确定性 validator 对权限、结构、身份、完整性、可恢复性和发布硬边界判定。

G3.6 不新增 capability 业务范围，不提前开放 production Evidence、settled Answer 或 Gate 5
发布能力。Gate 4/5 尚未实现的环节以 typed boundary 验证，不能用脚本制造已上线事实。

## 3. 第一性原理

### 3.1 评测对象是行为关系

Episode 固定业务世界、用户目标、可观察事实、可接受结果空间和禁止结果。它不固定：

- action 顺序；
- 工具顺序；
- SQL 形状；
- 唯一 Frame；
- 唯一窗口；
- 唯一结论措辞；
- 唯一调查深度。

同一问题允许多个专业测量设计。Evaluator 检查设计能否回答决策、是否显式、是否可比、
是否跟随证据边界、是否沿权威链保持一致。

### 3.2 LLM 与确定性系统的职责

LLM 拥有：

- 开放业务语义理解；
- estimand 与备选设计；
- 自适应调查；
- 解释和业务表达；
- Reviewer 对专业质量与业务有用性的判断。

确定性系统拥有：

- schema、类型、权限、隐私和 realm；
- manifest、hash、lineage 和 suite 完整性；
- 时间范围、calendar/exposure 的结构合法性；
- accepted head、CAS、stale result 和 revision identity；
- Evidence/claim compatibility 和 publication hard boundary；
- at-least-once 下的幂等、恢复和 projection replay。

确定性 grader 不用关键词推断开放语义，也不把某个日期窗口、分析路线或业务答案写成
隐藏模板。

### 3.3 三种不同的独立性

运行产物分别记录：

- `configuration_separation`：模型、thinking、prompt、输入合同、输出合同和 invocation
  identity 是否不同；
- `provider_common_mode_risk`：多个角色是否共享 vendor/model family；
- `human_calibration_independence`：是否由满足 policy 的独立人类完成校准。

同一 provider 不自动标记 `reduced_independence`。运行记录如实表达共同模式风险。缺少独立
人工校准时，正式资格保持 blocked。

## 4. 权威对象

### 4.1 `Gate3ExecutionManifest`

manifest 是运行前冻结的工作清单。每个 `RunCellSpec` exact 绑定：

- policy、taxonomy、catalog、Episode core、world profile 和 authority profile hash；
- base/counterfactual variant 与 materialized digest；
- lane：`semantic_frame` 或 `full_authority`；
- wording variant、paraphrase source/hash、repeat index 和 deterministic seed；
- paraphrase authority、operator-scenario authority、execution-universe compiler release、完整
  coordinate set 与 Episode relation-group set hash；
- Primary、Runtime Reviewer、Evaluation Reviewer profile ref/hash；
- AgentWorldView hash、EvaluatorOracleView hash 和 TraceProfile hash；
- required stages、required layer checks 和 artifact types；
- realm、storage epoch 和 runner release identity。

manifest 只扩展 policy floor。运行后删 cell、改 denominator、降低 repeats、换 prompt/model、
换 view 或减少 trace stage 会让 suite invalid。

完整运行的坐标不得由 runner 自己枚举。`compile_gate3_execution_universe.py` 以 policy、
catalog、paraphrase registry、operator registry 和 scenario registry 为输入，先生成 exact
universe readiness；development registry 缺槽位、重复槽位、哈希漂移或非法 stage 时直接
blocked。当前派生矩阵为：

| 风险 | 必跑 lane | paraphrases × repeats |
|---|---|---:|
| medium | semantic/frame | 1 × 1 |
| high | semantic/frame | 1 × 1 |
| high | full authority | 2 × 2 |
| critical | semantic/frame | 3 × 3 |
| critical | full authority | 2 × 2 |

一个 cell 可以同时进入自己的 `episode_outcome` 组、paraphrase 组和 base/sibling mutation
组。relation authority 位于顶层集合，cell 内不保存单值 relation，避免多重关系被最后一次
写入覆盖。

wording paraphrase 与 meaning-preserving case mutation 使用不同 operator。前者保持同一 case
variant 并比较 canonical wording 与 reviewed 改写；后者保持业务 measurement identity 但允许
case variant 改变。base/sibling relation 只在 paraphrase index 0 配对，避免两边独立改写造成
wording confound。所有 relation 还必须保持未被该 operator 改变的 repeat、visible turn 和
paraphrase 坐标轴。

### 4.2 `ModelInvocationRecord`

每次真实调用记录：

- run/cell/case/logical job/attempt/role；
- provider、model、thinking 和 configuration hash；
- prompt bundle、input contract、output contract 与 exact request hash；
- provider response ID、finish disposition、usage、typed output hash；
- causation/correlation、accepted authority snapshot、开始/完成时间；
- durable attempt receipt 与 result reference。

不保存密钥。Evaluator 只能读取允许的审计投影；Agent 不能读取 oracle 或 grader authority。

### 4.3 `Gate3TraceBundle`

TraceProfile 声明每条 lane 的 required stages。stage 至少覆盖：

```text
message ingress
→ typed binding
→ frame proposal
→ frame review
→ frame acceptance / repair
→ plan acceptance
→ effect dispatch / receipt
→ evidence disposition
→ claim proposal
→ runtime review
→ settlement boundary
→ workflow projection
→ evaluation review
```

每个 stage 绑定 immutable artifact hash、journal cursor、authority heads 和 predecessor。缺
stage、伪造 ref、逆序权威提交或 hash 不匹配直接 invalid；分支合法并行由 causal graph
校验，不要求全局线性顺序。

### 4.4 `EvaluationReviewRecord`

Evaluation Reviewer 接收 raw business context、EvaluatorOracleView、完整业务可见运行产物和
结构化权威 trace。它先独立盲评，不接收 deterministic hard-check verdict 或 reason。盲评
完成后，aggregator 再用 strict AND 合入 hard checks。它输出：

- 五个锚定维度的 0–3 评分；
- `pass | fail | needs_review`；
- critical failure codes；
- claim-local findings、证据 refs 和责任 stage；
- concise reason 与 confidence。

Reviewer 不得到 deterministic 程序生成的“正确业务答案”。日期解析、查询结果、exposure、
引用关系等只以原始权威产物呈现。A/B dossier 探针继续保留为研究证据，不进入正式评分
输入，直到人工校准证明它提升判断且没有引入 fixed-pattern bias。

0–3 分数的产品 verdict 映射固定为：

- 任一 registered critical failure：`fail`；
- Reviewer 明确 `fail`：`fail`；
- 任一维度 0 或 1：`fail`；
- `needs_review`：`blocked`；
- 五维均为 2 或 3 且无 critical failure：`pass`；
- schema、hash、artifact binding 或 grader invocation 损坏：`invalid`。

低质量行为不能写成 `invalid`，避免把真实产品失败从 pass denominator 中移除。

### 4.5 `Gate3CellResult` 与 `Gate3SuiteResult`

CellResult 继续使用 product behavior、authority conformance、implementation 三层严格合取。
SuiteResult 由 runner 从 frozen manifest 和所有 indexed cells 派生：

- exact cell set；
- completed/missing/duplicate/unexpected/invalid cells；
- 每层 pass/fail/blocked/invalid 分母；
- critical veto 和 historical regression 统计；
- paraphrase/repeat relation consistency；
- pairwise/higher-order coverage；
- trace completeness；
- final disposition。

只要存在 missing、duplicate、unexpected、critical fail、silent authority drift、oracle leak、
trace gap 或 policy floor 不满足，suite 无法 pass。平均分不能覆盖任何硬失败。

SuiteResult 强制保留两个正交状态：

- `local_execution_status`：本地运行是否完整、无产品/权威/实现失败；
- `formal_admission_status`：独立校准、protected held-out、受保护执行回执和外部 admission
  是否齐备。

当前允许的最佳状态是 `local_execution_status=pass`、
`formal_admission_status=blocked`。任何文档、PR 或聊天汇报都不能把它缩写成“G3.6 passed”。
`local_execution_status` 只表示 manifest 选定的 smoke/slice/full cells 在本地完整执行；
`local_evidence_trust=runner_self_attested` 明示 observation 与 artifact bytes 尚未得到外部重算。
`coverage_admission_status` 单独证明 full universe、13 类 target × 独立 worlds 和 relation/operator
分母，smoke 或 slice 即使本地通过也保持 coverage blocked。

### 4.6 `ExecutionAttemptJournal`

每个 execution cell 使用 append-only attempt journal：

- retry policy、上限、允许的 retry reason 和 terminal selection 在 manifest 中冻结；
- attempt policy 来自独立 hash-bound canonical policy；manifest 不能自行把业务口径变化加入
  retry reason；
- technical retry 保持同一 cell identity；
- 第一个 terminal attempt 是唯一结果来源；
- terminal 后继续运行直接 invalid；
- 所有失败和 superseded attempts 都进入 SuiteResult 与执行回执；
- 独立 repeat 使用新的 cell identity，不能用 retry 冒充 repeat。

### 4.7 `RelationGroupSpec` 与 `RelationEvaluationResult`

base/sibling、property、mutation 和 schedule 都是一等 relation group。每组绑定：

- versioned operator、seed、anchor 和 subject cells；
- expected identity、boundary、claim-local effect、safety 和 liveness relation；
- deterministic 与专业 judgment 的独立 check；
- relation artifact 与 derived verdict。

Suite 按 relation group exact set 验收。单个 member 各自 pass 仍不能替代 relation
consistency pass。

### 4.8 `Gate3RunExecutionReceipt`

formal result 需要受保护执行方签发 receipt，绑定 candidate/runner/grader release、frozen
execution manifest、真实 artifact bytes root、attempt journal、provider receipts、cell/result
集合、SuiteResult 和 protected held-out expansion。仓库内 runner、grader 或 caller 无权签发
formal receipt；独立只读 verifier 从 artifact bytes 重算 hash 和 verdict，再验证 Sigstore
bundle。任意本地 hash index 只能证明引用一致，不能自证真实执行。

## 5. 两条真实 provider lane

### 5.1 Lane A：semantic/frame

输入是自然用户消息与 AgentWorldView。运行生产 `MessageBindingProvider`、Primary Agent、
typed action decoder、Frame candidate saga 和独立 Runtime Reviewer：

```text
QuestionRevision
→ TypedSemanticBinding
→ revise_frame action
→ AnalysisFrameRevision candidate
→ Reviewer objections/acceptance
→ accepted Frame 或 typed blocked/ask_user
```

验收重点：

- 决策目标、metric、population、time、comparison、unit、numerator、denominator、exposure、
  assumptions、alternatives、falsification、reversal、success/stop conditions；
- 低风险推断进入 DecisionLedger 并被 Frame 引用；
- 重大且不可发现的选择才 ask_user；
- 月份长度、闰年、跨年、时区、业务日、partial coverage 和 unequal exposure 进入设计；
- 开放问题不受 question-family router、关键词表或固定窗口驱动。

### 5.2 Lane B：full authority

Lane B 使用同一个 production controller/provider/storage code path：

```text
Question → Binding → Frame review/acceptance → Plan
→ governed effect envelope → Evidence admission
→ provisional Answer → runtime review
→ fail-closed settlement boundary → Workflow projection
```

harness 只实现受控 world 的 capability/effect adapter，返回
`CapabilityResultEnvelope`。它不能构造 Frame、Plan、Evidence、Answer、Reviewer verdict 或
Workflow success。

Gate 4/5 仍封闭时，正确结果可以是：

- conformance Evidence 进入 provisional Answer；
- production Evidence 被明确拒绝；
- settled publication 因对应 Gate 未开放而 blocked；
- unsupported contract 触发局部 omit/degrade；
- 有效 question/Frame/Plan/evidence identity 仍完成全链 trace。

runner 禁止把“尚未开放”统一写成失败；每个 Episode 的 allowed disposition 决定产品层
判断。脚本 provenance、缺 accepted head、incomplete trace 或 action rejection 必须使相应
cell fail/blocked/invalid，不能伪装成功。

## 6. Corpus 与运行矩阵

### 6.1 三个样本池

- 真实用户问题：8 类 WAJEgame 问题家族；
- 历史失败：跨月口径漂移、raw total/exposure、方向反转、错误 settled、stale evidence 等；
- matrix-generated boundary：measurement、time、exposure、evidence、conversation、lifecycle
  的 pairwise 与 critical higher-order 组合。

跨行业 transfer probes 保持 research-only，不参与 launch verdict。

### 6.2 八类 launch 行为

1. 付费金额变化解释；
2. 规律解释；
3. 事件影响复盘；
4. 收入健康度检查；
5. 维度/因子归因；
6. 异常/黑天鹅复盘；
7. 多基准比较；
8. 数据质量/证据检查。

每类至少覆盖一个 supported world、一个 partial/missing world 和一个 correction 或 challenge
关系。八类只作为业务入口；运行合同继续覆盖 definition、distribution、cohort、funnel、
association、causal challenge 和无时间问题。

### 6.3 关系测试

每个 Required Episode 运行 base 与三类 sibling：

- meaning-preserving：业务 measurement identity 保持；
- measurement-changing：受影响的 identity/revision 变化，旧 Evidence 不能复用；
- boundary/interaction-changing：disposition、claim ceiling 或 evidence validity 局部变化。

另运行通用 mutation：

- time offset、contrast order、estimator、denominator、exposure、cohort horizon、funnel order、
  decomposition residual、calendar/release version；
- physical source plan 与 transport-only change；
- technical retry；
- stale result、correction fence、duplicate delivery、crash/resume、lease takeover；
- Evidence/claim scope、strength、unit、grain、window 和 applicability drift；
- forged trace、missing stage、fake settlement 和 oracle leakage。

这些 mutation 使用 versioned `MutationOperatorRegistry`。运行 manifest 不允许用
`case_variant` 自由文本代替 operator/relation/schedule authority。

### 6.4 Corpus remediation

当前 36 Episodes 是完整 candidate set。typed corpus epoch 已完成以下机器权威：

- 144 个 base claim targets 和 12 个 replacement claim targets 全部绑定 13 类
  `ClaimTargetKind`；候选 Episode 不能自行覆盖 registry 中的类型；
- `business_world_independence_key` 从 outcome data binding 的 authority refs 规范派生；36 个
  Episodes 实际归并为 20 个独立 authority sets，换题面、换 `world_id` 或重复使用同一 frozen
  snapshot 都不会增加独立世界数；
- 13 类 claim target 在 base Episodes 和完整 executable variant universe 中均达到至少 3 个
  独立世界；floor 由 eval policy 拥有，coverage ledger 与 SuiteResult 共用：

| ClaimTargetKind | base worlds | executable variant worlds |
|---|---:|---:|
| accounting_decomposition | 10 | 14 |
| association | 8 | 11 |
| causal_effect | 5 | 7 |
| cohort_outcome | 5 | 8 |
| composition | 3 | 4 |
| contrast | 16 | 20 |
| data_quality_state | 5 | 8 |
| definition | 3 | 4 |
| diagnostic_set | 3 | 4 |
| distribution | 4 | 5 |
| funnel_transition | 4 | 6 |
| point_quantity | 3 | 3 |
| temporal_pattern | 3 | 3 |

- 每个 Episode 固定 `open_world_acceptance=true`，authored designs 仅作
  `illustrative_non_exhaustive` 示例；Reviewer 依据 must-preserve、must-investigate、claim
  target、support expectation、forbidden outcomes 和显式 disqualifier 判定；
- independence key、typed targets 和 design-space policy 只进入 evaluator/corpus authority，
  AgentWorldView 不接收这些答案侧字段。

仍有以下 formal coverage 缺口：

- `expert_business_case` 来源为 0；
- 8 个 real-user Episodes 来自同一 source task；
- historical sources 仍待 protected provenance；
- 36/36 全部为 multi-estimand，缺单 estimand、低上下文、简单开放问题；
- 5 个 Episodes 只有一个 authored valid design example，开放验收合同已经避免将示例当作
  whitelist，仍需独立测量审查补充设计多样性；
- candidate world count 尚未获得 source verification、truth review 或独立双审，不能当作
  reviewed coverage；
- `next_experiment_design_claim` 与 repair/republication 类 target 仍需迁入 WorkPlan / next
  investigation expectation，当前 typed registry 只保证分类和覆盖计算没有漂移。

G3.6.0 新 epoch 已完成：

- 为 claim target 增加与 runtime 同源的 `claim_target_kind`；
- 为 Episode 增加 `business_world_independence_key`；
- 每类 ClaimTargetKind 至少 3 个独立 business worlds；
- 将 valid design 改为非穷尽 illustrative examples + required properties + disqualifiers +
  `open_world_acceptance=true`；
- 每次执行按 authority-derived independence key 计算 ClaimTargetKind world coverage。

继续 open：

- 增加 pairwise/higher-order registry 与 derived coverage proof；
- 从业务结构派生 conversation/data/time coverage atom，语义 atom 绑定独立 review；
- 增加 expert、single-estimand、low-context 和 simple-open Episodes。

typed registry 只把显式份额、构成、集中度、分组定位或排名目标纳入 COMPOSITION；机制、
贡献分解、相关关系和诊断目标分别保留自己的类型。后续仍要拆解同时混有 segment pattern、
payer base、per-dimension leader、stable channel payment/GGR 与 paid-silence factor 的复合目标；
repair/republication 与 next experiment design 迁回 WorkPlan/next-investigation expectation。
禁止为了填覆盖表按文案给复合 target 改类型。

## 7. 评分与校准

### 7.1 Product behavior

Evaluation Reviewer 采用五个锚定维度：

- question and measurement；
- investigation；
- evidence and claims；
- authority consistency；
- answer value。

critical failure 直接 fail。专业合理的替代窗口、方法或调查路径可以 pass。Reviewer 的
输出必须引用 artifact/ref，不能只给主观分数。

Reviewer trigger 和 ClaimTargetKind 采用分层 calibration；overall 80% 不能被简单 case
稀释。每类 runtime Reviewer trigger 至少 2 个 base cases × 3 repeats，critical false pass
继续为 0。

### 7.2 Hard checks

以下项目只由确定性 grader 判定：

- schema、manifest、hash、profile、view 与 cell set；
- accepted head、revision lineage、identity compatibility 和 stale fencing；
- artifact completeness、trace stage 和 causal graph；
- realm、permission、privacy、data contract 和 publication hard boundary；
- duplicate/unexpected/missing result；
- suite aggregation与 policy floor。

### 7.3 正式校准

本地开发运行输出 `development_unreviewed` 或 `quality_probe_only`。正式 calibration 仍要求：

- 至少 12 个 hash-bound Episodes；
- critical/noncritical、base/counterfactual、pass/fail/blocked；
- 至少 4 个人工 non-pass labels；
- grader-human agreement ≥ 80%；
- critical false pass = 0；
- 独立、专职 calibration reviewer；
- prompt/rubric/input/output/runner/model profile 全部冻结进 authority hash。

缺任一项，自动评分可用于研发定位，不能成为 Gate admission 或上线证据。

## 8. 实施阶段

### G3.6.0 运行权威合同

交付：

- versioned execution manifest / model invocation / trace / review / suite result schemas；
- role profile exact binding；
- lane/paraphrase/repeat/seed/realm/TraceProfile 坐标；
- strict suite completeness derivation；
- no-backcompat 删除旧 per-cell-only 假设。
- ExecutionAttemptJournal、MutationOperatorRegistry、RelationGroup/Result；
- protected execution receipt 与 local/formal 双状态；
- typed ClaimTargetKind 和 derived coverage authority。

Exit：

- 漏跑、重复、意外 cell、换 profile/view/prompt、减少 repeat、缺 trace 均不能 pass；
- full manifest 必须逐项等于编译得到的 1,172 个坐标与 2,011 个 Episode relation groups；
  集合数量和规范哈希同时绑定，删除或替换单项均失败；
- product fail 无法由另两层抵消；
- draft/unreviewed corpus 无法产生 formal pass。
- 同一角色可在一个调查循环中完成多个独立 logical jobs；每个 logical job 只能接受一个成功
  输出；
- terminal attempt 的 artifact-set identity 由完整 TraceArtifactIndex 重算，不能由 cell、attempt
  和 hard-check 三份结果同步伪造；
- product Reviewer 必须覆盖 grader registry 的完整 predicate set；critical/historical 分母按
  unique Episode 统计，不被 repeat 数量放大。
- paraphrase authority 逐槽绑定完整多轮消息结构和 wording hash；开放业务语义仍由模型与
  Reviewer 判断，编译器只校验来源、结构、完整性和 meaning-preservation review 状态；
- 19 类无法从 Episode sibling 自动产生的 measurement/authority/physical/transport/runtime/
  schedule/trace operator，必须各覆盖至少 2 个独立业务世界；measurement mutation 同时覆盖
  semantic/frame 与 full-authority lane，其他类型至少覆盖 full-authority lane，才可开放
  development full run。
- scenario metadata 自洽不能证明 mutation 已实际进入模型或 runtime 输入。当前永久保留
  `operator_scenario_executor_unverified` development blocker；后续必须以独立 resolver
  registry、mutation artifact、实际输入 artifact 与 `ScenarioApplicationReceipt` 重算器关闭。
- formal 永久保留 `formal_execution_admission_unverified`，直到 protected held-out expansion、
  external execution receipt 与受保护 verifier 形成正向可满足路径。checked-in review/status/hash
  无权解除该 blocker。

### G3.6.1 Provider profile 与 trace

交付：

- thinking 进入通用 provider settings、request 与 configuration hash；
- 三角色 provider factory；
- provider attempt、typed result 和 trace stage 持久绑定；
- secret-safe invocation artifact。

Exit：

- 三个配置可独立复现；
- 同一模型不同 thinking 配置拥有不同 configuration identity；
- timeout/retry 仍只在 provider 层；
- crash 后复用 durable result，不重复提交业务权威。
- provider success receipt 与 typed result 原子持久；
- configuration/thinking/prompt/tool/request bundle 进入 logical job identity；
- TraceBundle 与 PostgreSQL journal、artifact bytes、RunTraceManifest 和 invocation receipts
  exact 对账。

### G3.6.2 Lane A

交付：

- production semantic binding + Frame candidate + Reviewer runner；
- base/sibling/paraphrase/repeat 调度；
- measurement design 与 authority trace projection；
- model grader input/output contract。

Exit：

- 原跨月历史失败在无关键词规则下通过关系验收；
- 非比较问题可形成可执行 typed algebra；
- material ambiguity/known gap disposition 与 expectation 一致；
- trace 能定位 binding/frame/review 责任点。

Lane A 开始前必须补齐：

- oracle-safe、hash-bound AgentWorldView authority 进入 binding 与 ContextPacket；
- low-risk inference 生成 DecisionRecord，并由 accepted Frame 引用；
- production measurement resolution/obligation stage 能从 accepted Frame 前进；
- 旧 Gate 2 live runner 重写为完整 message-binding → Primary → Reviewer 顺序。

### G3.6.3 Lane B

交付：

- disposable PostgreSQL test realm；
- controlled capability adapter；
- production controller/provider full-chain runner；
- correction、并行 obligation、乱序 completion、retry、crash/resume、stale result schedule。

Exit：

- harness 无权创建业务权威；
- 同一 run 可跨进程恢复；
- stale/forged/scripted result 无法进入 Evidence/Answer；
- Workflow 由 journal replay 得到同一 projection。

Lane B 开始前必须补齐：

- typed AnswerReview worker，不能把 Answer Reviewer outbox 路由到 Frame Reviewer；
- 独立 obligation/effect/projector workers 与并行 completion；
- selected sensitivity identity 和 Evidence relation；
- registry-issued RunRealmContext 与 PostgreSQL store attestation；
- 真实 OS process crash、successor liveness 和 trace idempotence tests。

### G3.6.4 Reviewer 与校准包

交付：

- Runtime Reviewer 和 Evaluation Reviewer typed contracts；
- raw-artifact Reviewer 输入；
- A/B dossier 研究结果；
- calibration sample builder、label/result binding 和 disagreement queue。

Exit：

- Reviewer 能接受多种合理设计；
- critical drift、unsupported evidence、错误方向和过强 claim 无 false pass；
- 未达到人工校准门槛时 formal result 保持 blocked。

A/B dossier 使用预注册三 arm：

1. raw artifacts；
2. neutral mechanical facts，只给日期、天数、数字、引用；
3. anchored negative control，刻意加入固定 pattern。

每 case 独立调用，至少 12 个 paired cases × 3 repeats，有效非典型设计与 critical fail 各半，
覆盖 temporal、ratio、decomposition、cohort、funnel、event 和 data quality。主指标是有效替代
设计误杀率；guardrail 是 critical false pass = 0。研究结果不能直接晋级正式输入。

### G3.6.5 全矩阵执行

交付：

- smoke、slice、full 三套不可互相冒充的 run mode；
- 36 base + 120 counterfactual 的完整坐标；
- policy-required paraphrase/repeat、property、mutation 和 schedule runs；
- immutable artifact index、cell results 和 suite result。

Exit：

- deterministic checks 100%；
- critical historical regressions 100%；
- silent authority drift 0；
- source-backed critical claim executable design 100%；
- known gap quantitative evidence 0；
- material ambiguity disposition 100%；
- required trace completeness 100%；
- critical higher-order cases全部通过。

### G3.6.6 Closeout

交付：

- 三路对抗式审查与 disposition；
- Python 3.12.13 clean-copy build/test/run；
- disposable PostgreSQL acceptance；
- 文档、契约、测试、eval artifact 和 PR。

Exit：

- Blocking/Major findings 为 0；
- 无关键词字典、固定业务窗口、question-family router、scripted authority 或旧实现依赖；
- local engineering status 与 formal G3.E0 status 分开报告；
- G3.E0 blocked 条件没有被本地实现改写。

## 9. 测试矩阵

| 层 | 必测内容 | 门槛 |
|---|---|---|
| Contract | schema、codec、hash、profile、view、manifest、result | 100% |
| Suite authority | exact cell set、repeat、paraphrase、lane、denominator、artifact index | 100% |
| Universe compiler | policy matrix、156 variants、1,172 coordinates、2,011 relation groups、201 paraphrase slots、38 scenarios | exact set/hash |
| Measurement | 13 ClaimTargetKind、时间四层、exposure、ratio/decomposition/cohort/funnel | critical 100% |
| Relations | base/sibling、metamorphic、mutation、property | critical 100% |
| Async | correction race、duplicate、乱序、crash、lease、resume、replay | 100% |
| Provider | real typed output、profile/think identity、retry、durable receipt | required runs 100% |
| Reviewer | valid alternatives、critical failure recall、pairwise consistency | calibration policy |
| Security | oracle leak、realm forge、trace forge、scripted provenance | 0 tolerated |
| Full suite | 36 base + 120 siblings + policy expansion | exact manifest |

## 10. 数据与环境

- Python 固定为 3.12.13，通过 `uv sync --frozen --no-install-project --python 3.12.13`
  建立 venv，不跟随系统 Python 降级。
- 真实 provider 从本地未提交的环境变量读取 endpoint/key；artifact 只记录 endpoint origin
  的安全 identity、profile 和 hash，不记录 secret。
- G3.6 PostgreSQL 使用 disposable database 或独立 schema/credential。禁止读取项目本地
  runtime database 作为测试存储。
- controlled world adapter 只读 hash-bound case-file materialization。production realm、
  production registry 和 production EvidenceStore 均不可由 caller 选择。
- 真实业务数据只使用已登记的聚合/frozen authority；原始标识和行级数据不进入 evaluator
  artifact。

## 11. 提交与审查策略

按可独立验证的顺序提交：

1. G3.6 plan + adversarial design review；
2. execution manifest / invocation / trace / suite contracts；
3. provider role profiles 与 Lane A；
4. Lane B 与 disposable PostgreSQL recovery；
5. Reviewer grader、calibration package 和 full matrix；
6. closeout evidence、clean-copy 和 PR。

policy/expectation 变更与被测 runtime 修复分开提交。一次 eval failure 只能进入 failure ledger；
通过人类确认、双 owner 和可泛化模式审查后，才允许另开 runtime guardrail 变更。

## 12. 对抗式自审

### 12.1 已识别风险

| 风险 | 失败方式 | 计划中的封闭点 |
|---|---|---|
| 单 cell 合法、整套漏跑 | runner 只提交挑选的 pass cell | frozen exact cell set + SuiteResult |
| 模型配置漂移 | manifest 只写 profile 名 | exact profile/prompt/input/output/runner hash |
| 改写次数缩水 | critical case 只跑一次 | paraphrase/repeat 是 RunCell 坐标 |
| 同源 Reviewer 共同偏差 | 三角色共享 provider family | common-mode risk 显式记录 + 人工校准 blocker |
| deterministic dossier 引导 Reviewer | 程序把某种设计包装成事实 | 正式输入只用 raw authority artifacts |
| harness 自己造答案 | fixture 直接写 Frame/Evidence/Answer | harness 只能返回 governed effect envelope |
| Gate 4/5 未开放造成伪失败 | full chain 必须 settled 才算 pass | Episode allowed disposition + typed boundary |
| draft corpus 自我晋级 | checked-in JSON 声称 reviewed | formal result 依赖外部 admission；dev status 封闭 |
| 平均分掩盖硬错 | behavior 低分被 implementation 高分抵消 | 三层 strict AND + critical veto |
| trace 看似完整 | 只有 stage 名，没有权威 lineage | artifact hash + cursor + heads + predecessor graph |
| case-specific 修复 | 为跨月题加关键词/固定七天 | relation/property scoring + open design space |
| 并发测试只测安全 | stale 被拒后新 scope 永远不继续 | 每个 schedule 同时检查 safety 和 liveness |

### 12.2 组合对抗审查新增 findings

三路审查分别覆盖 eval authority、corpus/grader、runtime/async/PostgreSQL。新增 blocking
failure classes 已进入实施顺序：

1. runner/grader 可用任意 hash 自证 artifact；
2. policy 已能编译 Episode 主宇宙、paraphrase slots 和 operator scenario 缺口；201 个
   paraphrase authority 与 38 个 scenario authority 尚待 author/review；
3. typed ClaimTargetKind 和独立 world coverage 缺失；已由 typed registry、authority-derived
   independence key 和 execution coverage 去重关闭；
4. protected held-out 未进入 executable suite；
5. Reviewer 分数/critical code 到 product verdict 缺机械映射；
6. 同一 cell 可丢弃失败 attempt 后重跑到 pass；
7. relation、pairwise/higher-order 与 suite 统计缺一等 authority；
8. AgentWorldView、DecisionLedger inference、resolution worker、Answer Reviewer、trusted realm
   和并行 workers 未闭合；
9. provider configuration identity 与成功响应持久化存在恢复窗口；
10. trace、invocation、artifact index 尚未与 durable PostgreSQL facts exact 对账。

G3.6.0 的冻结快照又验证了两项已关闭的合同漏洞：动态调查不会因同一角色多次成功调用被
误拒绝；terminal artifact root 必须等于 TraceArtifactIndex 的规范 hash。真实 artifact bytes、
provider receipt 和 append-only PostgreSQL journal 仍需 protected executor 对账，因此本地
`pass` 继续标记为 `runner_self_attested`。

这些 finding 都是架构/合同问题，不需要新增用户访谈。execution-universe readiness 已作为
G3.E0 evaluated artifact 纳入 verifier release；其 development/formal blocker 不与 smoke/slice
结果混写。所有项关闭前，full-matrix runner 保持禁用。

### 12.3 自审结论

G3.6 的首个实现提交必须先完成 suite authority 和 provider configuration identity。直接跑
若干真实问题只能形成探针，无法证明 Gate 3 行为覆盖，也无法阻止结果挑选。运行合同闭合后
再接 Lane A/B，可以让后续每次真实调用都有稳定、可追溯、不可缩水的验收坐标。

当前计划没有新增业务答案、固定窗口、关键词规则或测试专用产品分支。正式 G3.E0 blocker
继续保留，开发进度与外部信任状态分别记账。
