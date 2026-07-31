# WAJE BI Agent vNext G3.1 Implementation Evidence

## 1. 结论

G3.1 local implementation 已完成。它建立 schema epoch 3 的 measurement-authority
合同、accepted QuestionRevision、typed AnalysisFrame、规范身份、scope proof、append-only
派生记录与 PostgreSQL fail-closed 边界。

G3.E0 formal admission 仍由 checked-in trust artifacts 派生为 `deny_g3_1`。本实现依据用户
明确 development override 开展，不构成 gold promotion、protected admission 或 production
Evidence 授权。

本工作包无需新增用户决策。实现期间发现的问题都能从既有顶层原则、Gate 3 文档与代码合同
中确定答案。

## 2. 权威边界

- `QuestionRevision` 只保存 immutable source-message lineage、correction/challenge refs、
  accepted head 与 analysis cycle。
- `AnalysisFrameRevision.measurement_design` 是唯一测量权威。
- Primary Agent 的 `revise_frame` typed action 直接提交 `MeasurementDesign`；旧自由字符串
  frame payload 和本地语义补全路径已删除。
- accepted Frame 绑定唯一 accepted Question、source spans、DecisionLedger refs、
  semantic measurement IDs 与 authority binding IDs。
- correction 接受新的 QuestionRevision 后清空 Frame/Plan/Answer heads，旧对象继续保留为
  append-only audit history。
- G3.1 只提前补入 ContextPacket question payload 与首次消息的 literal QuestionRevision
  bootstrap。MessageImpactBinding saga、candidate review 与 provider job contract 仍由
  G3.2 实现。

## 3. Measurement algebra

epoch-3 `MeasurementDesign` 使用 typed nodes 表达：

- decision objective、source grounding、variable、event、population、observation unit；
- metric expression、time role、window rule、exposure、estimator、contrast、eligibility；
- sequence、cohort/risk set、reconciliation、relationship、identification；
- assumption、alternative、sensitivity、falsification、reversal；
- typed scope、EvidenceRequirement、epistemic completion 与 explicit EstimandSpec。

Frame graph validator检查全局 node ID 唯一、引用闭合、estimand shape、evidence requirement
闭环与 completion path。开放业务语义仍由 typed LLM proposal 提出；本地代码只验证 typed
结构、权威连续性和硬边界。

## 4. Identity、scope 与 resolution continuity

`measurement-identity.v1` 规定：

- rename-stable semantic measurement preimage；
- Question/Frame/grounding-bound authority binding preimage；
- resolution 与 resolution-outcome preimage；
- NFC UTF-8、Unicode code-point key order、explicit null、无 exponent decimal、
  UTC microsecond timestamp、cross-language safe integer；
- set-like graph arrays稳定排序，`ordered_event_ids` 保留业务顺序。

Python 与 JavaScript 共享 golden/mutation vectors。存储层重新计算 Frame、resolution 和
resolution-outcome identities，拒绝 caller-supplied forged digest。

identity hash 之外还有独立 Frame-conformance check。即使 caller 重新计算全部 digest，
以下变化仍无法通过 resolution admission：

- estimand、semantic measurement 或 authority binding 改变；
- scope、grain、unit、exposure 或 eligibility 改变；
- window rule、period offset 或 contrast operand identity 改变；
- boundary 引用未知 requirement、缺少 contract/inspection evidence 或提高 claim ceiling。

typed scope engine只自动证明 exact relation。subset、superset、projection、aggregation 和
disjoint 都要求显式 versioned contract proof；缺少 proof 返回 `unknown`。

实际日历窗口生成、DST/holiday/fiscal/snapshot resolver 和 field derivation proof 由 G3.3
继续实现。G3.1 已保证 resolver 输出必须保留 Frame identity，尚未把未实现的 resolver
声明成可用能力。

## 5. Storage 与 migration

`003_gate3_1_measurement_authority.sql`：

- 对非空 epoch-1/2 authority schema 受控拒绝，要求显式 development reset；
- 增加 accepted question head、analysis cycle、epoch-3 Frame identity columns；
- 增加 QuestionRevision、MeasurementResolutionOutcome、ResolvedEvidenceObligation、
  EvidenceValidityRecord、ObligationSatisfactionRecord 与 SettlementPreconditionReport
  append-only tables；
- 通过 foreign keys、unique constraints、immutable triggers 和 validity-chain indexes
  维持引用与 CAS；
- Answer row status 与 JSON payload status 都只能为 `provisional`。

InMemory 与 PostgreSQL adapters 使用同一准入规则。migration acceptance 会先建立含旧 case
的 epoch-1 schema，确认 003 明确拒绝，再清理 ephemeral database 并执行当前 schema 测试。
没有 dual-read、payload adapter 或旧 artifact 迁移路径。

## 6. 对抗式审计

| 风险 | 结果 |
|---|---|
| free-text Frame 形成平行业务权威 | 已删除；`revise_frame` 只接收 typed MeasurementDesign |
| node rename 或 set-like 顺序改变 measurement identity | 已修复；rename stable + set normalization |
| month offset/exposure 等实质变化未进入 identity | 已修复并有 mutation tests |
| caller 重算 digest 后改变 accepted Frame 窗口 | 已阻断；独立 Frame-conformance admission |
| scope 根据 domain ref 集合自行猜 subset | 已阻断；非 exact relation 要求 contract proof |
| JavaScript timestamp 截断微秒 | 已修复；ISO offset 到 UTC microsecond 保真 |
| Python/JavaScript Unicode key order 分歧 | 已修复；统一 Unicode code-point order |
| 旧 epoch 数据被静默解释为 epoch 3 | 已阻断；真实 PostgreSQL migration rejection test |
| settled Answer 绕过 controller | schema、domain、repository 与 DB constraint 全部拒绝 |
| local implementation 抬高 trust readiness | 已阻断；readiness 保持 `deny_g3_1` |

## 7. 验收证据

最终收口命令：

```text
npm run check:contracts
npm run test:bootstrap
npm run test:postgres
npm run test:postgres:gate2
npm run check:evals:gate3
npm run check
git diff --check
```

结果：

- `npm run test:bootstrap`：147 tests passed，8 个 environment-gated tests skipped；
- G3.1 focused measurement authority：18 tests passed；
- `npm run test:postgres`：3 tests passed，并先验证含旧 authority row 的 epoch-3 migration
  受控拒绝；
- `npm run test:postgres:gate2`：5 tests passed；
- `npm run check:contracts`：epoch-3 schema freshness、TypeScript bindings 与 Python/JavaScript
  identity vectors 全部通过；
- `npm run check`：clean-copy、Python 3.12.13、wheel build、contract check、147 tests、
  health run 与 legacy reference scan 全部通过；
- `npm run check:evals:gate3`：verifier 无 finding，`derived_status=blocked`、
  `entry_decision=deny_g3_1`；
- `git diff --check`：通过。

其中 `npm run check:evals:gate3` 必须成功验证最新 manifest，同时继续报告
`entry_decision=deny_g3_1`。`npm run gate3:enter:g3.1` 和
`npm run check:evals:gate3:policy-ready` 仍应 fail closed；它们属于未完成的 external trust
promotion，不作为 local implementation 的伪通过项。

## 8. 后续责任

- G3.2：strict message-impact binding、Frame candidate review、Reviewer jobs、durable
  fan-out/fan-in、terminal job disposition、heartbeat takeover 与 operation lineage。
- G3.3：完整 conditional graph validator、calendar/data resolver、unit/exposure algebra、
  deterministic derivation proof 与 requirement-to-obligation compiler。
- G3.4：accepted Plan adoption、QueryBindingEnvelope、logical execution continuity。
- G3.5：capability-native Evidence admission、EvidenceUseBinding、claim precheck、
  system-derived settlement report 与四轴 projection。

当前没有需要用户修复的 G3.1 blocker。
