# WAJE BI Agent vNext Gate 3 Review

## 1. Verdict

Gate 3 accepted。

本 Gate 证明了开放业务问题可以沿同一条权威链完成：

```text
user question
→ Primary Agent measurement design
→ AnalysisFrameRevision
→ requirement-complete WorkPlanRevision
→ governed probe/capability effects
→ immutable EvidenceRecord
→ interpretation and reversal
→ provisional/settled AnswerVersion
→ read-only Workflow projection
```

证明切片没有变成日期分段特例。`vnext/services/` 中不存在月初、1–7、1–10 或该问题的
本地业务规则；具体分段只出现在 Agent proposal、query-spec acceptance fixture 和测试数据。
launch 范围继续由 Gate 7 的全问题家族矩阵定义。

## 2. Gate entry interview record

入口先核查了 factor SSOT、付费金额与 source contract、历史失败 expectation、旧 runbook、
本地 ClickHouse 和全量日汇总数据。

仓库材料中存在两套历史分段：

- 1–10 / 11–20 / 21–月底；
- 1–5 / 6–24 / 25–月底。

真实数据同时证明阶段总额会被观察天数显著影响。最初曾准备让用户确认一套固定主
estimand；用户指出日期段、主 estimator、exposure 校正和 sensitivity 应由 LLM 从业务问题
和证据自主判断。本 Gate 接受该顶层决定：

- Primary Agent 自主生成 comparison groups、`EstimatorSpec`、`ExposureDesign`、
  alternatives、falsification、reversal、success 和 stop conditions。
- 确定性系统校验 typed contract、requirement closure、SQL/source/snapshot 边界和证据绑定。
- 可由 probe 或 sensitivity 检验的合理测量分歧进入 provisional Frame，不触发
  `ask_user`。
- 缺失的企业政策、目标或业务定义无法从合同和数据检验时，`ask_user` 才可阻塞 run。

本 Gate 无其他用户决策。

## 3. Authority and measurement contract

### 3.1 Single measurement authority

`AnalysisFrameRevision` 现在显式包含：

- estimand、population、time scope 和 observation unit；
- `EstimatorSpec`：quantity、aggregation、numerator、denominator、exposure adjustment；
- `ComparisonDesign` 与 Agent 定义的 focal/reference groups；
- `ExposureDesign`：exposure variable/unit、balance assumption、normalization strategy、
  diagnostic requirement 和 sensitivity adjustments；
- material alternatives 与其 requirement binding；
- falsification、reversal、success 和 stop conditions；
- semantic contract refs。

当 exposure 为 `unknown` 或 `expected_unequal`，且 primary estimator 选择 `none`
adjustment 时，Frame 必须声明至少一个非 `none` 的 sensitivity adjustment。系统不指定
`per_exposure_unit`、`model_adjusted`、`stratified` 或 `design_equalized` 中哪一种是业务
主估计量。

### 3.2 Frame to Plan closure

每个 `FrameRequirement` 使用稳定 ID 和 typed kind。accepted WorkPlan 的 tasks 必须精确覆盖
全部 Frame requirements；缺失或多余 requirement ref 会被 admission 拒绝。所有 material
alternative 必须引用 `alternative` requirement。

测量合同错误使用稳定 reason code 返回 Primary Agent，包括：

- `frame_exposure_diagnostic_requirement_invalid`；
- `frame_exposure_sensitivity_requirement_invalid`；
- `frame_adjusted_sensitivity_required`；
- `frame_alternative_requirement_invalid`；
- `frame_alternatives_required`；
- 其他 Frame completeness codes。

controller 不用本地模板修复 Frame。

### 3.3 Provider repair boundary

真实 provider 曾返回错误的外层 `{"action": ...}`。provider adapter 通过统一 typed-output
repair turn 要求重新发送 exact `kind/payload`，decoder 继续拒绝 alias 和额外字段。格式修复
只存在于 provider 层，不进入业务 controller 或兼容解析器。

## 4. Context and evidence loop

effect success 可以返回一个或多个 `EvidenceDraft`。controller 负责：

1. 校验 task、capability、accepted Frame/Plan 和 semantic contract bindings；
2. 分配稳定 EvidenceRecord ID；
3. 固化 payload hash、snapshot/release、grain、provenance、strength 和 limitation；
4. 将 aggregate inline payload 或 result handle 放入 ContextPacket；
5. customer projection 只展示业务摘要和 EvidenceRecord IDs。

`ContextPacket` 同时携带内部 `agent_result` 和 aggregate evidence payload。Primary Agent
可以读取实际 exposure 数、raw/normalized contrast 和 boundary，再生成 FrameRevision、
interpretation 或 Answer。prompt、密钥、SQL 和 verifier internals 不进入 customer
projection。

## 5. Generic period-comparison proof capability

`period_comparison` 接受 Agent 提交的 typed query spec：

- governed metric/source refs；
- complete date range；
- period unit；
- 任意非重叠 ordinal groups 及 focal/reference roles。

compiler 只使用 accepted physical source binding，返回每个 period/group 的：

- total value；
- observed exposure units；
- value per exposure unit。

同一批 sufficient statistics 同时支持 raw total 与 exposure-normalized sensitivity。能力
本身不选择主 estimand，也不输出机制或因果结论。

vNext 自有 semantic contracts 固化了：

- successful paid amount、NGN、order dedup 和 Africa/Lagos business date；
- daily non-cumulative source grain；
- accepted source table、availability 和 immutable snapshot/release ref。

## 6. Real-data proof

### 6.1 Multiple Agent candidates

`npm run test:data:gate3` 对两个 Agent 候选分段运行同一 generic compiler。两者均覆盖
2024-01 到 2026-05 的 29 个完整月份。

候选 A（1–10 / 11–20 / 21–月底）：

- focal vs 11–20：raw 与 normalized 均为 10/29 同方向，中位比 0.9681；
- focal vs 21–月底：raw 6/29、中位比 0.8675；normalized 9/29、中位比 0.9419。

候选 B（1–5 / 6–24 / 25–月底）：

- focal vs 6–24：raw 0/29、中位比 0.2559；normalized 12/29、中位比 0.9725；
- focal vs 25–月底：raw 5/29、中位比 0.6778；normalized 7/29、中位比 0.9183。

数据证明 exposure 处理会实质改变数字和方向命中率，系统因此不能静默预选窗口或 estimator。

artifact：
`vnext/artifacts/gate3-real-data/period-comparison.json`

- content SHA-256：`ddb3b3533eb31325d95ff07c8e88394b36680e76379e7ad15b2b6ebe4641fcfe`
- file SHA-256：`d65d69b4b751de64cb9529f7f2e7392e281476c5a8edb307ae5baa08375c7c2d`

### 6.2 Live Primary Agent Frame

真实 DeepSeek provider 通过：

```text
inspect_semantics → revise_frame
```

自主产生 2 个 comparison groups、`expected_unequal` exposure、typed primary estimator、
adjusted sensitivities、material alternatives、falsification、reversal、success/stop
conditions，并通过当前 authority JSON Schema。

artifact：
`vnext/artifacts/gate3-live-provider/analysis-frame.json`

- artifact content SHA-256：`a9a3e3a4fb6c5b1b6b8f9d0fd756f2395f2fc50f2e0c8ecf18131318b7012554`
- file SHA-256：`d0d7d7dd4af74836d7c47fb3188f69e0068259b6874f00c4beb61dd7ebd8baf7`
- provider/model：`deepseek` / `deepseek-v4-flash`
- `OPENAI_API_KEY` 在验收进程中显式清除。
- 旧 `.env` 的已配置值只在该验收进程显式映射为 `WAJE_VNEXT_LLM_*`；vNext runtime
  继续只读取 vNext 前缀。

### 6.3 Full authority-loop slice

`npm run test:slice:gate3` 使用 Agent-selected 1–7 / 8–月底 query spec 运行真实数据：

- 29 个 comparable months；
- raw focal > reference：0/29；
- normalized focal > reference：9/29；
- median raw ratio：0.2820；
- median normalized ratio：0.9535。

原始“每个月月初更高”前提触发 reversal。payday 与 composition alternatives 分别生成
`missing_contract` boundary EvidenceRecord，没有发布机制结论。

同一 store 最终包含：

- 1 accepted FrameRevision；
- 1 accepted WorkPlanRevision；
- 4 immutable EvidenceRecords；
- 1 InterpretationRecord；
- provisional AnswerVersion；
- settled AnswerVersion；
- replay-mode Workflow projection，3 个业务 task 均为 completed。

artifact：
`vnext/artifacts/gate3-slice/authority-loop.json`

- content SHA-256：`c45ef0d7297659a9b3906a8b81991c109a37b17579c6dd549d21186aa82659c1`
- file SHA-256：`ff4a9be22cd353dee4afee068081a74cf6e30bf63cd40a4b4d580ce0dcdb7926`

## 7. Workflow projection

Workflow 只读取 accepted Frame/Plan、EvidenceRecords 和 event journal customer projections。
它显示业务 task、requirement、dependency、状态、证据 ID 和业务摘要；action IDs、prompt、
SQL、retry internals 和 verifier internals不进入输出。

journal cursor 连续时使用 `replay`；cursor 缺失或矛盾时使用 `static`，只保留 accepted
authority 和 durable evidence 状态，不合成运行进度。

## 8. Verification manifest

环境：

- Python `3.12.13`
- Node `v26.0.0`
- npm `11.12.1`
- local ClickHouse container `waje-bi-clickhouse`
- ephemeral PostgreSQL 17 acceptance containers

执行结果：

| Command | Result |
|---|---|
| `npm run test:bootstrap` | 66 tests；62 passed，4 PostgreSQL-env skips |
| `npm run check:contracts` | passed |
| `npm run test:postgres` | 2/2 passed |
| `npm run test:postgres:gate2` | 2/2 passed |
| `npm run test:data:gate3` | passed；2 designs × 29 months |
| `npm run test:provider:gate3` | passed；live semantic inspection + Frame |
| `npm run test:slice:gate3` | passed；4 evidence + provisional/settled answer + replay workflow |
| `npm run check` | clean-copy passed |

clean-copy evidence：

- Python version：`3.12.13`
- tree SHA-256：`eb71d528d9c94d0372001e441a0dd024024630262d7cbe02cd21a12972440c9b`
- wheel SHA-256：`7922fe62bc59847e3a990d8989ebb14ae1153a14569e869c7a2d48f3860f583d`
- `requires_python`：`>=3.12`
- contract generation、compileall、66 tests、health、wheel build 全部在只复制 `vnext/`
  的临时 workspace 通过。

## 9. Adversarial review

| ID | Severity | Finding | Disposition |
|---|---|---|---|
| G3-AR-01 | Blocking | 初始方案准备固定日期窗口和主 estimator | 已撤销；measurement design 归 Primary Agent |
| G3-AR-02 | Blocking | numerator/denominator 与 exposure adjustment 分散会形成双权威 | 已用单一 `EstimatorSpec` 收敛 |
| G3-AR-03 | Blocking | Agent 只能看到 customer summary，无法按真实 exposure 修 Frame | 已增加 bounded internal `agent_result` 和 aggregate evidence payload |
| G3-AR-04 | Major | invalid Frame 只返回通用错误，模型无法局部修订 | 已拆成稳定 measurement objection codes |
| G3-AR-05 | Major | malformed provider envelope 可能诱导兼容解析 | 已保留 strict decoder；provider 层统一 repair retry |
| G3-AR-06 | Major | 空 alternatives/falsification/reversal 可在 authority 构造阶段崩溃 | 已前移 admission validator，并要求非空合同 |
| G3-AR-07 | Major | 单题分段可能渗入 capability | 服务层只接受 generic ordinal groups；题目常量只在 tools/tests |
| G3-AR-08 | Follow-up | Context inline evidence 尚未设置统一字节/行上限 | Gate 4 增加 bounded payload 与 stable result handle policy；当前 Gate 3 payload 为小型 aggregate |
| G3-AR-09 | Follow-up | Gate 3 settled fixture 只证明 exact binding 和状态转换 | Gate 5 实现数字、单位、分母、方向、文字一致性与 Reviewer settlement gate |
| G3-AR-10 | Follow-up | 当前 proof compiler 只覆盖 calendar-month ordinal comparison | Gate 4 扩展完整 capability fabric；不得据此缩减 launch 问题家族 |

无 unresolved blocking finding。

## 10. Gate 3 exit criteria

- [x] 月内边界来自 Agent typed Frame/QuerySpec，无问题字符串硬编码。
- [x] full sample、complete month、timezone、metric validity、source release 和 exposure 可审计。
- [x] material alternatives 被检验或绑定明确 missing-contract boundary。
- [x] sensitivity 与 reversal 实际改变和限制结论。
- [x] Answer 与 Workflow 来自同一 accepted authority 和 journal。
- [x] 测试验证 generic capability contract，未缩窄 launch 范围。

Gate 4 进入前继续执行 `$grill-me` 入口判断。
