# WAJE BI Agent vNext Gate 0–2 Realignment Audit

## 0. 文档控制

| 项目 | 内容 |
|---|---|
| 日期 | 2026-07-30 |
| 审计对象 | Gate 0 commit `93464d83`、Gate 1 commit `4525189c`、Gate 2 commit `01c28dbf` |
| 触发原因 | 原 Gate 3 暴露 question → Frame → Evidence → Answer 的 authority drift |
| Entry interview | 本次复核无需用户决策；用户已经明确要求通用顶层修复、撤销旧 Gate 3 |
| 结论 | Gate 0 保持；Gate 1、Gate 2 的历史验收保持；G3.1、G3.2 是新 Gate 3 业务闭环实现的硬前置 |

本审计不改写 Gate 0–2 当时已经成立的验收事实。它回答一个更窄的问题：新 Gate 3 若要成为
可适配当前及未来 BI 问题的测量权威层，Gate 0–2 留下的接口是否足够。

## 1. 审计方法

审计沿两条路径执行：

1. 从已接受 Gate 2 commit `01c28dbf795e2d6b0b2272c1c46b4cfa96aab453` 读取 domain、
   JSON Schema、migration、controller、provider、ContextPacket 与 admission；
2. 用原问题发生漂移的失败链反向攻击每一层，检查系统能否证明：
   - Primary Agent 看到的就是被接受的用户问题；
   - Frame 能完整表达其测量语义；
   - downstream 只能执行 Frame 已接受的同一测量；
   - Evidence、Answer 和 Workflow 无法只靠 ID 对齐伪装成语义对齐。

复核命令：

```bash
cd vnext
git diff --exit-code 01c28dbf795e2d6b0b2272c1c46b4cfa96aab453 -- .
npm run check:contracts
npm run test:bootstrap
```

结果：

- `vnext/` 与 Gate 2 commit 无差异；
- generated contract drift check 通过；
- 52 tests 运行，48 passed，4 个 PostgreSQL environment tests 按合同 skipped。

## 2. Gate 0

### 2.1 结论

Gate 0 无需调整。

Day 0 隔离、Python 3.12+、独立 package/database/environment namespace、clean-copy build 和
legacy dependency scan 都与新 Gate 3 的通用测量设计无冲突。旧 Gate 3 的撤销也证明隔离
边界有效：整个实现可作为一个提交逆向移除，Gate 0–2 仍能测试和运行。

### 2.2 保持的不变量

- 所有新生产实现继续只位于 `vnext/`。
- 不读取或 import 旧合同、旧 runtime、旧测试和旧前端。
- 新 Gate 3 的 schema 变更直接形成当前 vNext 合同，不保留错误 Gate 3 兼容分支。
- Python baseline 保持 `>=3.12`，本地与 clean-copy 使用项目 virtualenv。

### 2.3 验收

| 检查 | 结果 |
|---|---|
| 逆向移除 Gate 3 后 Gate 0–2 可运行 | Pass |
| `vnext/` 与 Gate 2 commit 精确一致 | Pass |
| contract drift check | Pass |
| Gate 0–2 unit/schema baseline | Pass |

## 3. Gate 1

### 3.1 保持有效的核心设计

- `InvestigationCase` 是稳定 case root，业务内容存放在 immutable revision/version。
- `AnalysisFrameRevision` 是测量设计唯一权威。
- `WorkPlanRevision` 表示业务调查任务，技术 retry 不创建 revision。
- `EvidenceRecord` 与 `AnswerVersion` 保持不可变。
- accepted heads 通过 CAS 移动，event journal 只记录事实。
- JSON Schema 是跨语言合同源，Python domain type 执行不变量。

这些边界继续作为新 Gate 3 的基础。

### 3.2 必须补强的合同

| 缺口 | 当前证据 | 风险 | 新 Gate 3 修正 |
|---|---|---|---|
| 原问题没有 immutable revision | `InvestigationCase` 只有 Frame、Plan、Answer heads；`ContextPacket` 只有瞬时 `user_message` | 规划、恢复或 follow-up 可把另一个相似问题当成当前问题 | 在 `InvestigationCase` authority family 内增加 immutable `QuestionRevision` lineage 与 accepted question head |
| Frame 的关键语义是自由字符串 | `AnalysisFrameRevision.estimand`、`exposure`、`comparison` 等均为 `str` | schema 合法仍无法表达或验证相对时间、多个 estimand、分母、识别假设和边界 | 将 Frame 升级为可组合的 typed measurement algebra；每个 estimand 使用显式 `EstimandSpec` |
| Frame 缺少 source grounding | Frame 只引用 `DecisionRecord` 和 semantic contracts | 模型可以产出流畅且脱离原问题的测量设计 | 每个 material semantic binding 引用用户消息 span、DecisionRecord、contract 或显式 Agent inference |
| Frame 与 compiler 可能双重定义 Evidence closure | 当前没有 requirement/obligation 所有权区分 | compiler 可以降低或改写 Frame 所需证据 | Frame 拥有 `EvidenceRequirementSpec`；compiler 单向派生 `ResolvedEvidenceObligation` |
| Plan task 没有 evidence obligation | `WorkTask` 只有 capability intent、target claim 和依赖 | task 无法证明它执行的是 Frame 中哪个测量 | task 绑定 estimand、authority binding、resolution 与 resolved obligation |
| Evidence 缺少测量身份 | Evidence 只绑定 Frame/Plan/task 与 query ref | 同一 Frame 下的窗口、单位或 estimator 漂移无法机械检测 | Evidence 绑定 semantic/authority/resolution-outcome 与 tagged conformance/production execution provenance、实际范围与 observed exposure |
| Claim scope 是文字 | `AnswerClaim` 只绑定 Evidence IDs 和文字 applicability | claim 可以扩大 Evidence 的 population、时间、grain 或 strength | 增加 typed scope algebra 与 `EvidenceUseBinding`，只有可证明的 subset/projection/aggregation 才能通过 |
| identity preimage 未分层 | Frame、instance、query 和 transport schema 可能进入同一个 hash | harmless revision、snapshot refresh、跨语言 codec 或 schema 增量会错误改变 identity | 分离 semantic、authority、resolution、logical、physical identity；定义 versioned preimage 和 Python/TS golden vectors |
| settlement fingerprint 只证明对象 ID 集合 | fingerprint 由 Frame ID、Plan ID、claims、policy 构成 | 内部对象语义不完整时仍可得到稳定 hash | 增加 system-derived `SettlementPreconditionReport`，绑定 requirement/obligation、compatibility、objection 与 trace completeness |
| storage 无 question/resolution/schema epoch 表达 | migration v1 没有 QuestionRevision 或 ResolvedMeasurementInstance | restart 后无法重建完整权威链；旧 payload 可能在 runtime 才 decode 失败 | 使用 schema epoch 3；新 bootstrap 只接受当前 schema，含旧 authority rows 的数据库受控拒绝/reset |
| resolution failure 没有权威对象 | 当前只有可执行 instance 概念 | missing contract/calendar/data version 时会伪造 instance 或让 boundary 脱链 | 增加 `MeasurementResolutionOutcome` tagged union；obligation 绑定 outcome ID |
| Evidence/obligation validity 容易落成可变字段 | Evidence immutable，但 reuse 需要 supersede/revoke；obligation 需要 fulfillment | 原地改状态会破坏 append-only authority | system-owned EvidenceValidityRecord、ObligationSatisfactionRecord；definition 保持 immutable |

### 3.3 Gate 1 状态处理

Gate 1 保持 `Complete`，因为 CAS、immutability、五类 authority family、storage port 和 schema
generation 的目标已经完成。以上项目记录为 **Gate 1 contract amendment required by
Gate 3**：

- G3.1 必须在任何新 capability、Evidence、Answer 或 Workflow 证明之前完成；
- amendment 仍归 `InvestigationCase`、`AnalysisFrameRevision`、`WorkPlanRevision`、
  `EvidenceRecord`、`AnswerVersion` 五类 authority family，不引入平行测量权威；
- 当前没有线上数据，采用显式 schema epoch cutover；不读取错误 Gate 3 artifact，也不解码
  Gate 1/2 的旧 authority payload。

## 4. Gate 2

### 4.1 保持有效的核心设计

- 一个 Primary Business Analysis Agent 持有开放业务语义。
- WAJE-owned controller 是唯一 authoritative action loop。
- provider 只能提出 typed proposal，不能写 accepted head。
- lease、fencing、checkpoint、outbox、idempotency 和 retry/resume 保持系统边界。
- 高价值模型调用默认等待；timeout/retry 集中在 provider 层。
- `ask_user` 可暂停并恢复同一 run。

### 4.2 必须补强的 runtime

| 缺口 | 当前证据 | 风险 | 新 Gate 3 修正 |
|---|---|---|---|
| Context 只带当前消息 | `ContextPacket.user_message` 无 accepted QuestionRevision payload | crash/replay 或 follow-up 后缺少问题 authority | ContextPacket 必须携带 accepted QuestionRevision、source messages 与 binding refs |
| provider 只要求 JSON mode | Chat Completions adapter 使用 `response_format={"type":"json_object"}` | 只保证合法 JSON，不保证 schema adherence | 使用 strict Structured Outputs / strict function tools；application 继续执行 domain validation |
| typed binding 没有独立阶段 | provider 直接提出 `revise_frame` | 无法区分“理解了什么”与“选择了什么测量设计” | 增加 source-grounded `SemanticBinding`，Frame 引用被验证 binding |
| 所有 material assertion 没有独立一致性检查 | span validator 只能证明引用存在 | 有效 span 可以支持相反的 binding | 每个 identity-affecting assertion 执行独立 semantic consistency pass；风险只控制深度 |
| Reviewer 无持久化候选状态 | 当前 controller 没有 pending review phase | LLM 调用跨事务，或 crash 后 review 与 candidate 不再对应 | durable FrameCandidateBundle、review outbox、candidate hash、typed wait state 和最终 CAS |
| correction 只在 `WAITING_FOR_USER` 接纳 | READY/effect/completed 阶段的新消息没有统一入口 | 旧 effect 可以进入新问题 authority | MessageIngressRecord/MessageImpactBinding durable LLM saga；事务外 typed binding，CAS 移动 head/fence 旧 work |
| capability payload 可自带开放参数 | `CallCapabilityPayload.parameters` 是通用 JSON object | capability/SQL 层可悄悄重写 window、metric 或 denominator | Gate 3 只创建 `QueryBindingEnvelope`；Gate 4 physical QuerySpec 只能消费该 envelope |
| admission 只检查 head 与 task | 当前 checks 不比较 measurement identity | semantic drift 可以作为合法 effect 进入 outbox | admission 对 semantic/authority/resolution/logical identity 做 exact match |
| effect success 与 Evidence admission 分离 | effect transaction 清除 outbox 后才可能持久化 capability-native Evidence | crash 会留下 succeeded execution 和缺失 Evidence/admission | typed result receipt；immutable EvidenceRecord、system-owned EvidenceAdmission/Validity/Satisfaction records 与 checkpoint 原子提交，或进入专用 wait phase |
| propose answer 后 controller 直接 completed | provisional Answer 被接受后 phase 进入 `COMPLETED` | 未 settlement 的答案和 workflow 看起来已完成 | execution、obligation、publication、delivery 四轴分离；Gate 3 不投影 generic completed |
| trace 缺少语义链与真实性 | journal 有 action/effect hash，缺少 binding/measurement span | 一个任意 trace ref 可以伪装真实 provider proof | system-owned ModelInvocationRecord、RunTraceManifest 与 completeness verifier |
| blocking objection 只有 status 字符串 | Primary 可提交 disposition 后继续 Frame CAS | 独立 review 可被单方越过 | 新 candidate hash、accepted DecisionRecord 或 deterministic proof + independent recheck 才能生成 closure record |

### 4.3 Gate 2 状态处理

Gate 2 保持 `Complete`，因为 controller ownership、durability、retry 和 interruption 已成立。
以上项目记录为 **Gate 2 runtime amendment required by Gate 3**。G3.2 完成前不进入新
Evidence/Answer/Workflow 业务分支。新 Gate 3 扩展当前 controller contract，不另建第二条
agent loop。

## 5. 跨 Gate 决定

### 5.1 Authority family 数量

`QuestionRevision` 属于 `InvestigationCase` authority family：Case 继续保存 identity、
lifecycle 和 accepted head pointers；QuestionRevision 保存用户问题的 immutable input
lineage。它不会定义 estimand，测量设计仍只属于 `AnalysisFrameRevision`。

### 5.2 设计值与观测值

- Frame 定义时间语义、window rule、expected exposure、unit/aggregation algebra、
  eligibility 和 estimator；
- `MeasurementResolutionOutcome` 是 system-derived resolved/boundary union；
  `ResolvedMeasurementInstance` 是内容寻址、确定性派生、无 accepted head 的 resolved
  variant，确定
  actual calendar range、expected calendar days、timezone、business-day boundary、
  calendar/data version 和 snapshot binding；
- accepted WorkPlan CAS 是采用某个 resolution outcome 的唯一入口；
- Evidence 记录实际 observed/valid exposure、missingness 和 capability output。

这一分层避免把查询结果提前写成测量设计，也避免 capability 自行选择日期。

### 5.3 Gate 3 与 Gate 5

新 Gate 3 建立 system-derived `SettlementPreconditionReport` 并证明 drift 时 fail closed。
完整数字/文字一致性、Answer Reviewer 和 claim-level settled publication 仍在 Gate 5。
Gate 3 期间产生的业务 Answer 保持 provisional；controller、repository、database 和
projection 都拒绝 settled。

## 6. 最终结论

| Gate | 审计结论 | 后续动作 |
|---|---|---|
| Gate 0 | 保持，无补强 | 继续执行 deletion independence |
| Gate 1 | 核心架构保持，合同需扩展 | G3.1 完成 authority、measurement、identity/scope 与 schema epoch amendment |
| Gate 2 | controller/runtime 核心保持，恢复边界需扩展 | G3.2 完成 typed binding、message/candidate saga、trace 与状态骨架；G3.5 接入 Evidence admission 与四轴 projection |

新 Gate 3 的入口结论：**本 Gate 无需用户决策**。用户已经明确选择 Primary Agent 自主提出
开放测量设计，确定性系统验证结构、权威连续性、日历/data contract、证据和发布边界。
