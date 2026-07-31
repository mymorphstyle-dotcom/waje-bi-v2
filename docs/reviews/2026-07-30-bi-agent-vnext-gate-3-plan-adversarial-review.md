# Gate 3 Universal Measurement Authority：组合对抗式审查

## 0. 审查控制

| 项目 | 内容 |
|---|---|
| 日期 | 2026-07-30 |
| 审查对象 | `docs/plans/2026-07-30-bi-agent-vnext-gate-3-universal-measurement-authority.md` |
| 方法 | 主审 + 三个独立子智能体：measurement design、authority/runtime、eval/trust |
| 基线验证 | `vnext/` 与 Gate 2 commit `01c28dbf795e2d6b0b2272c1c46b4cfa96aab453` 无差异 |
| 当前 disposition | 两轮 findings 已回写；最终三路 closure pass：0 open Blocking、0 个不可实现 Major |
| Gate 状态 | Planned |

## 1. 审查纪律

审查采用 hostile producer、hostile consumer 和 crash/concurrency 三种视角：

- Primary Agent 可能误解 source、漏报歧义或自评通过；
- Reviewer 可能与 Primary Agent 共享偏差；
- compiler/resolver 可能形成隐藏业务权威；
- capability 可能返回成功状态和漂移后的实际 scope；
- Answer 可能只凭 Evidence ID 存在就扩大 claim；
- Workflow 可能把多个生命周期压成一个 completed；
- provider/harness 可能用 scripted 或 test-realm 伪造全链路证据；
- crash 可能发生在任意 external effect 与持久化边界；
- correction 可能与 review、effect、Evidence admission 并发；
- hash、trace ref、environment class 和 settlement status 都可能被伪造。

审查只接受可机械验证的修复。自然语言承诺、单例 fixture、关键词规则和“模型通常会做对”
均不构成 finding closure。

## 2. 第一次审查结论被重新打开

第一次文档自审记录了 23 项并过早写成“0 blocking open”。三个独立审查从不同攻击面证明：

- 多数 identity 仍只保证对象相连，无法证明 estimand 等价；
- comparison 之外的测量形状缺少可执行代数；
- Reviewer、correction、resolution adoption 和 Evidence admission 没有完整恢复协议；
- real-provider、trace、test realm 与 settlement 仍可被测试脚本伪造；
- Gate 3 和 Gate 4 对 QuerySpec compiler 的所有权冲突；
- eval boundary 可以掩盖 supported case 的系统失败。

因此第一次 disposition 整体重新打开。下表是去重后的组合审查 ledger，也是当前设计的验收
依据。

## 3. Blocking findings

| ID | 攻击路径 | 根因 | 写入计划的通用修复 | 设计 disposition |
|---|---|---|---|---|
| G3-CR-01 | 多 estimand Frame 只引用整个 graph，某个 claim 无法定位 | 缺少显式 `EstimandSpec` | 每个 estimand 独立 ID、ClaimTargetKind、requirements 与 downstream refs | Resolved in plan §8 |
| G3-CR-02 | cohort/funnel/decomposition/association/causal 用同一个通用模板通过 | 只列问题形状，没有专属代数 | 增加 Event、Sequence、RiskSet、Reconciliation、Relationship 等 typed nodes 与条件 validator | Resolved in plan §8 |
| G3-CR-03 | compiler 生成的 obligation 反过来改变 Frame evidence 要求 | Frame 与 compiler 双重拥有 `EvidenceObligation` | Frame 拥有 `EvidenceRequirementSpec`；compiler 单向派生 `ResolvedEvidenceObligation` | Resolved in plan §12 |
| G3-CR-04 | 同义改写不能复用，harmless Frame 改动使 Evidence 全失效，transport 版本改变 semantic ID | identity preimage 混合 source、authority、resolution、execution 与 transport | 拆分 semantic、authority、resolution、logical、physical identity；加入 EvidenceUseBinding | Resolved in plan §10 |
| G3-CR-05 | claim applicability 写一句自由文本即可扩大 Evidence scope | 无可判定 scope relation | 封闭 ScopeExpression 与 equality/subset/projection/aggregation proof；unknown fail closed | Resolved in plan §11 |
| G3-CR-06 | 模型引用真实 span，却给出相反日期方向 | span/hash validator 不验证语义关系 | 所有 material assertion 运行独立 semantic consistency pass；风险只控制审查深度 | Resolved in plan §7 |
| G3-CR-07 | event time、ingestion time、snapshot time 被混用，DST/holiday/version 丢失 | calendar 与 data version 只有一个 TimeDomain | TemporalSemanticSpec、WindowRuleSpec、ResolutionContext、DataVersionSpec 四层 | Resolved in plan §9 |
| G3-CR-08 | rate 比较混用 ratio-of-sums 与 mean-of-ratios，raw total 改变方向 | exposure 缺 unit、risk set 与 aggregation algebra | 区分 calendar/eligible/observed/valid/at-risk exposure，显式 weights 与 aggregation order | Resolved in plan §9 |
| G3-CR-09 | supported case 被返回 `typed_boundary`，runner 仍通过 | “design/clarification/boundary 三选一”是全局逃逸口 | case 固定 required/allowed disposition；contract-supported case 必须 executable | Resolved in plan §1/§21 |
| G3-CR-10 | strict JSON mode 产生形状近似对象，fallback 继续运行 | provider API/schema 合同未封闭 | structured outputs + 每个 action 独立 strict tool + single call + no fallback + failure taxonomy | Resolved in plan §7 |
| G3-CR-11 | artifact 填一个 trace ID 就声称真实模型参与 | trace ref 没有系统拥有的完整性记录 | ModelInvocationRecord、RunTraceManifest、required spans 与 completeness verifier | Resolved in plan §18 |
| G3-CR-12 | Gate 3 conformance compiler 被当成真实 QuerySpec/data accuracy | Gate 3 与 Gate 4 同时拥有 QuerySpec compiler | Gate 3 只拥有 QueryBindingEnvelope；Gate 4 独占 physical QuerySpec compiler | Resolved in plan §13 |
| G3-CR-13 | caller 自报 `environment_class=test/production`，测试 Evidence 污染生产 | realm class 来自不可信 payload | trusted registry/context/storage realm + 独立 DSN/credential + production 二次验证 | Resolved in plan §21.5 |
| G3-CR-14 | agent 或脚本写 accepted verifier status / settlement fingerprint | settlement precondition 没有系统对象和多层拒绝 | system-derived SettlementPreconditionReport；controller/repo/DB/projection hard deny | Resolved in plan §15 |
| G3-CR-15 | Reviewer 在 Frame CAS 事务内调用 LLM，或在事务外崩溃后换了一次 review | 缺少 durable candidate review state | FrameCandidateBundle、review outbox、candidate hash、typed wait state、closure proof 与最终 CAS | Resolved in plan §7.4 |
| G3-CR-16 | READY/WAITING/COMPLETED 阶段的新 correction 被忽略，旧 effect 进入新问题 | 只有 clarification decision API，没有跨阶段 message transaction | MessageIngress/MessageImpactBinding LLM saga、question-head CAS、head invalidation、effect fencing 与新 cycle | Resolved in plan §6 |
| G3-CR-17 | resolver 可追加新 instance 并悄悄替换 snapshot/date | ResolvedMeasurementInstance 有隐藏 acceptance path | content-addressed deterministic record，无独立 head；Plan CAS 是唯一 adoption point | Resolved in plan §9.3/§16 |
| G3-CR-18 | capability 成功后 crash，outbox 已清除，Evidence 永久缺失 | effect completion 与 Evidence admission 分成无恢复的两步 | CapabilityResultEnvelope、immutable EvidenceRecord、system-owned admission、result receipt 与 WAITING_FOR_EVIDENCE_ADMISSION | Resolved in plan §14 |
| G3-CR-19 | real provider 只做 binding，后续对象仍由脚本预制 | full-chain proof 没有生产 controller/provider path | semantic/frame lane + full-authority conformance lane + independent provider Reviewer | Resolved in plan §21.5 |
| G3-CR-20 | Frame graph hash 包含自身 ID，Python/TS 对 decimal/time/Unicode 算出不同 ID | preimage、计算顺序与跨语言 codec 未规范 | versioned preimage、排除 derived fields、canonical codec、golden/mutation vectors | Resolved in plan §10 |

## 4. Major findings

| ID | 攻击路径 | 根因 | 写入计划的通用修复 | 设计 disposition |
|---|---|---|---|---|
| G3-CR-21 | Plan 用成本 stop condition 覆盖 Frame 的 evidence minimum | Frame/Plan 都叫 success/stop | Frame 只管 epistemic completion；Plan 只管 execution budget/scheduling | Resolved in plan §8.5 |
| G3-CR-22 | execution succeeded 被 UI 显示为 completed/delivered | Workflow 状态轴不足 | execution、obligation、publication、delivery 四轴；Gate 3 无 generic completed | Resolved in plan §15.3 |
| G3-CR-23 | risk matrix 根据模型自报 confidence 跳过 Reviewer | material/risk 由同一模型自评 | 所有 material assertion 都做独立 consistency pass；system policy 决定深度 | Resolved in plan §7.3 |
| G3-CR-24 | eval 失败后减少 paraphrase、repeat 或 higher-order cases | coverage 只有文档列表，没有机器 floor | 独立 Gate3EvalPolicy、expectation catalog、frozen manifest、denominator/pass-policy validator | Resolved in plan §21.1 |
| G3-CR-25 | Gate 2 数据库升级后 runtime 才出现旧 payload decode error | no-backcompat 没定义 schema cutover | schema epoch 3；含旧 rows 的 DB 受控拒绝/reset；无 dual-read | Resolved in plan §17 |
| G3-CR-26 | OpenAI hosted eval 产品变化使项目失去历史 authority | hosted eval 被当成长期 SSOT | WAJE-owned dataset/runner/grader/manifest；hosted service 只作辅助 | Resolved in plan §19 |
| G3-CR-27 | Evidence requirement 只支持单个 minimum，无法表达 AND/OR、矛盾和 coverage | closure contract 过弱 | requirement 支持 composition、strength、coverage、contradiction、boundary policy | Resolved in plan §12 |
| G3-CR-28 | Gate 0–2 被标记整体 complete，Gate 3 在缺少 amendment 时继续写 Evidence | 历史验收与当前入口条件混在一起 | 保留历史验收事实；G3.1/G3.2 成为新业务闭环实现的硬前置 | Resolved in plan §0/§20 |

## 5. Closure verification 重新打开的 findings

三个子智能体在修订后重新读取全文，发现第一次回写仍留下 6 个 Blocking 和 6 个 Major
closure gap。主审再次保持 Gate 打开并完成以下 disposition：

| ID | Severity | Closure gap | 二次修复 | 设计 disposition |
|---|---|---|---|---|
| G3-CV-01 | Blocking | Gate 3 Evidence 强制引用 Gate 4 QuerySpec | ConformanceExecutionProvenance / PhysicalQueryExecutionProvenance tagged union；按 realm 验证 | Resolved in plan §10/§14 |
| G3-CV-02 | Blocking | correction 需要开放语义分类，却没有 durable LLM 边界 | MessageIngressRecord、PendingUserMessage、MessageImpactBinding、两次短事务与外部 provider saga | Resolved in plan §6 |
| G3-CV-03 | Blocking | Primary 可以单方把 blocking objection 写成 resolved | 新 candidate hash、DecisionRecord/deterministic proof、independent recheck 与 closure record | Resolved in plan §7.4 |
| G3-CV-04 | Blocking | manifest 可以在运行前把 floor 冻结为 0/1 | 独立 Gate3EvalPolicy 固定 case、pairwise、higher-order、paraphrase/repeat 与 zero-tolerance floor | Resolved in plan §21.1 |
| G3-CV-05 | Blocking | resolution failure 无权威链位置 | MeasurementResolutionOutcome 的 resolved/boundary union；obligation 绑定 outcome | Resolved in plan §9/§12 |
| G3-CV-06 | Blocking | ResolvedEvidenceObligation 同时承担 immutable definition 与 mutable fulfillment | definition immutable；SatisfactionRecord 从 admission/use/boundary 确定性投影 | Resolved in plan §12 |
| G3-CV-07 | Major | Evidence supersede/revoke 没有 append-only 权威记录 | system-owned EvidenceValidityRecord；EvidenceUseBinding 消费最新 validity | Resolved in plan §10.3 |
| G3-CV-08 | Major | focused plan 暗示 journal/checkpoint 可重建 accepted heads | heads 只读 InvestigationCase CAS row；journal 只做验证/projection | Resolved in plan §5 |
| G3-CV-09 | Major | Lane A 与 Lane B 共用一套 required spans | checked-in TraceProfile 按 lane/outcome 固定必需与允许缺失 spans | Resolved in plan §18 |
| G3-CV-10 | Major | G3.2 前向依赖 G3.3–G3.5 对象 | G3.2 收敛到 correction/provider/review/trace/state skeleton；Evidence 原子闭环移到 G3.5 | Resolved in plan §20 |
| G3-CV-11 | Major | 主计划 expectation package 缺 disposition/trace/floor 字段 | 主计划 §8.2 继承 Gate3EvalPolicy 与同一机器合同 | Resolved in main plan §8.2 |
| G3-CV-12 | Major | 主计划澄清条件会让高影响问题自动询问用户 | 只有无法从 source/contract/data/低成本调查查明时 ask_user | Resolved in main plan §4 |

## 6. 交叉攻击验证

### 6.1 Source → Frame

必须同时满足：

- immutable QuestionRevision；
- durable MessageIngress/MessageImpactBinding；
- span/hash 合法；
- material assertion 有 grounding；
- 独立 consistency pass；
- candidate/review/disposition 可恢复；
- accepted Frame CAS 绑定同一 candidate hash 和 blocking closure proof。

任一项缺失都会阻止 accepted Frame。

### 6.2 Frame → Plan → execution

必须同时满足：

- explicit EstimandSpec；
- semantic/authority/resolution-outcome identities；
- requirement-to-obligation derivation；
- Plan adoption CAS；
- QueryBindingEnvelope 无未绑定语义；
- conformance/production execution provenance tagged union；
- Gate 4 physical compiler 独立所有权。

resolution 可以随 data version 更新，但不能拥有 accepted head。

### 6.3 Result → Evidence → claim

必须同时满足：

- typed CapabilityResultEnvelope；
- immutable EvidenceRecord、EvidenceAdmissionRecord 与 EvidenceValidityRecord；
- result receipt 在 Evidence disposition 前持久化；
- scope/unit/grain/exposure/data version compatibility；
- EvidenceUseBinding；
- claim applicability subset proof；
- SettlementPreconditionReport；
- Gate 3 settlement deny。

capability success 不保证 Evidence accepted，Evidence accepted 也不保证 claim 可 settled。

### 6.4 Evaluation proof

必须同时满足：

- frozen machine manifest；
- 独立 Gate3EvalPolicy、expectation catalog 与 TraceProfile；
- supported case 无 boundary escape；
- real-provider semantic/frame lane；
- real-provider full-authority lane；
- independent Reviewer invocation；
- trusted realm；
- WAJE-owned complete trace；
- runner 在 action rejection、missing head、scripted provenance 或 incomplete trace 时非零退出。

## 7. Gate 0–2 影响

| Gate | 历史验收 | 当前动作 |
|---|---|---|
| Gate 0 | 保留 | 隔离根、删除独立性和 Python 3.12+ 基线无需改动 |
| Gate 1 | 保留 | G3.1 增加 QuestionRevision、measurement algebra、identity/scope、schema epoch |
| Gate 2 | 保留 | G3.2 增加 correction、candidate saga、strict provider、trace 与 wait-state 骨架；G3.5 接入 atomic Evidence admission 与四轴状态 |

G3.1、G3.2 完成前，Gate 3 的业务 Evidence/Answer/Workflow 分支保持关闭。

## 8. 结论

组合审查否定了第一次“文档无阻断项”的判断。第一轮把 20 个 Blocking 和 8 个 Major
finding 转化为设计；closure verification 又重新打开 6 个 Blocking 和 6 个 Major gap，
并完成第二次回写。

当前可以确认的是设计 finding 已有可执行 disposition。Gate 3 仍处于 Planned；只有代码、
迁移、并发恢复、real-provider trace、property/mutation/replay、clean-copy 与数据库 epoch
证据全部通过后，审查状态才能关闭。

最终 closure pass 由 measurement design、authority/runtime、eval/trust 三路独立复核；
三路均返回 0 open Blocking、0 个不可实现 Major。该结论只关闭文档设计 finding，不替代
G3.1–G3.7 的实现验收。
