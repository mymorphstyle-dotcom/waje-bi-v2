# WAJE BI Agent vNext Gate 3 Behavior Eval 对抗式审查

> 2026-07-30 closure update：本文保留首次攻击证据和原始计数。E0 trust infrastructure
> 的当前实现、45 个 Episode 状态与剩余 external-authority blockers 以
> `docs/reviews/2026-07-30-bi-agent-vnext-gate-3-e0-trust-implementation.md` 和
> `vnext/evals/gate3/gate3-e0-readiness.json` 为准。

## 1. 结论

| 项 | 结论 |
|---|---|
| 审查对象 | G3.E0 schema、policy、rubric、validator、37 个 authoring candidates、123 个 counterfactual siblings、coverage ledger 与 Gate 3 第 21 节 |
| Authoring content | 已形成可继续编辑的候选池 |
| G3.E0 readiness | **Blocked** |
| G3.1 entry | **Denied** |
| 用户决策 | 本轮无需用户决策；findings 都属于已同意范围内的测试权威加固 |

三路独立审查分别覆盖 measurement design、eval trust、authority/runtime decoupling。审查者
只读检查合并 catalog，并通过临时篡改验证 validator 的攻击面。

当前候选集没有 action 顺序、工具顺序、SQL 形状或唯一 Frame 的直接 golden answer，
业务题面覆盖也较广。现有机器合同仍允许测试作者自证来源、评审、coverage 和 grader，
并存在 evaluator truth 泄漏、时间漂移、非原子反事实等问题。它可以作为 authoring draft，
不能作为 development-ready 或 launch-ready 测试集。

## 2. 可复现状态

- 37 个 base Episode，全部 `candidate`、全部 `authoring`；
- 21 个多轮 Episode；
- 33 个 high/critical Episode；
- 123 个 counterfactual siblings；
- 六个已声明 coverage 维度当前无 tag 空白；
- `real_user_language = 0`；
- business/measurement 双审记录为 0；
- `policy_ready = false`；
- `--require-policy-ready` 返回非零。

最终 hash 以 `vnext/evals/gate3/coverage-ledger.json` 为准；每次修订后必须重新验证。

## 3. Blocking findings

### B1. Episode 缺少可判定的 baseline support expectation

Schema 只有 `allowed_dispositions`。很多案例同时允许 executable design、clarification 和
typed boundary。runner 无法区分诚实边界与逃避支持能力。

关闭条件：

- 增加 baseline `contract_supported`、required disposition 和 conditional alternatives；
- 每个 boundary code 绑定可核验证据前提；
- supported baseline 无前提地退到 boundary 时 deterministic fail。

### B2. 时间业务世界不可稳定 replay

37 例中只有少量 `business_world.as_of`。大量案例使用“最近、昨天、本周、本月、Q2
未成熟”等相对时间。

关闭条件：

- 引入 required `EvaluationClock`：as-of instant、business timezone、calendar version、
  release cutoff；
- relative、partial-period、cross-period、timezone/business-day Episode 必须绑定 clock；
- sibling 明确 clock 保持或变更。

### B3. Agent view 与 evaluator oracle 没有可执行隔离

同一 JSON 包含未来用户消息、诊断性 title、hidden truth、acceptable/forbidden outcomes 和
grader。policy boolean 与 README 文字无法阻止 runner 把完整对象传给 Agent。

关闭条件：

- signed Episode 编译为白名单 `AgentEpisodeView` 与 `EvaluatorOracleView`；
- 逐 interaction trigger 注入用户消息；
- discoverable condition 只能经相应 inspection/probe surface 暴露；
- prompt、tool input 和 trace 运行 hidden-field canary/negative leak tests；
- 任一泄漏使 run 无效。

### B4. 来源和评审可由 catalog 作者自行认证

`source_trace_ref`、`reviewer_ref` 和 `review_record_ref` 是普通字符串。攻击者可以计算
content hash、虚构两个 reviewer，并把生成文案提升为真实用户或 fully reviewed。

关闭条件：

- Source Registry 与 Review Registry 置于 catalog 之外的受控 append-only authority；
- validator 解析 source record、reviewer identity/role、review record、权限域和内容 hash；
- promotion 状态由 registry 派生，Episode 无权自行声明；
- `expert_business_case` 需要 expert author record，`historical_failure` 需要 durable
  incident/eval record，`real_user_language` 需要 interview/trace record；
- 同一人员跨两个 review role 被拒绝。

### B5. Coverage 与风险仍依赖自报标签

一个 Episode 可以声明全部 tag，risk 可以自行改成 critical，coverage ledger 只计算 union。
文档列出十组 coverage 维度，policy 只实现六组，`missing_coverage={}` 无法证明文档目标。

关闭条件：

- 建立一个 machine-readable taxonomy，schema enum、policy 和文档从同一来源生成；
- coverage 从结构化 expectation、world/profile 和已审核关系派生；
- 每个值设置独立 Episode、独立 world、来源多样性、pairwise 和 higher-order floor；
- tag 不能单独满足 coverage；
- ledger 同时报告 raw tag count、verified world count 和 reviewed runnable count。

### B6. Episode-owned grader 与 runtime 合同耦合，且可被作者关闭

当前 `grading` 内含自由文本 deterministic checks 和 trace obligations。部分 Episode 直接引用
`MessageImpactBinding`、Frame、Evidence、accepted revision、claim precheck、settled 等
Gate/runtime 概念。作者可以清空 checks、写 `none`、关闭 human review 或避开关键 rubric。

关闭条件：

- immutable Episode core 只保存业务 behavior predicates；
- Gate-specific AuthorityConformanceProfile 独立绑定 authority hard boundaries；
- implementation suite 再绑定具体 repository/controller/provider；
- Episode 引用受保护、版本化的 GraderProfile；
- risk、claim、data condition 和 sibling relation 决定 mandatory graders；
- 未注册 predicate、缺 artifact 或不可判定结果全部 fail closed。

### B7. 三层 verdict 没有机器合同

文档声明 product behavior、authority conformance、implementation 三层独立，当前没有
run-result schema、独立 denominator 或 strict aggregation。

关闭条件：

```text
EvaluationRunResult
├── product_behavior_verdict
├── authority_conformance_verdict
├── implementation_verdict
└── release_verdict = strict AND + critical veto
```

- 每层有独立 denominator、skip policy、artifact 和 failure owner；
- blocking outcome、authority drift、trace 缺失分别 veto；
- aggregate score 只用于诊断，不能覆盖 veto。

### B8. 多个 counterfactual 不是单因素，部分期望无法由 mutation 推导

审查确认多例同时改变关账和结果方向、完整性和 exposure、随机化和成熟度、合同可用性和
业务 truth。`G3-GF-003-CF02` 规定了 mutation 无法推出的方向；`G3-GF-011-CF04` 会用邻近
Growth GMV 替代管理层财务语义。

关闭条件：

- sibling 使用 typed delta 或完整 variant；
- meaning-preserving / measurement-changing / boundary-changing 只允许一个 material
  mutation；
- interaction-changing 明确多个 mutation，并配套对应单因素 siblings；
- expected changes 必须由 frozen world 与 mutation 推导；
- 自动结构 diff + measurement reviewer 双重验证。

### B9. G3.E0 readiness 缺少唯一 fail-closed artifact

计划要求 protected held-out manifest、grader calibration、run manifest 和冻结 partitions，
当前均未形成权威包。caller 可以选择任意 catalog；默认检查还能在 `policy_ready=false` 时
退出 0 并覆盖 ledger。

关闭条件：

- 唯一 `Gate3E0ReadinessManifest` 固定 catalog/schema/policy/taxonomy/rubric/grader
  registry/source registry/review registry/partition/held-out/calibration hashes；
- ledger generation 与 ledger verification 分离；
- Gate CI 只调用 read-only policy-ready check；
- G3.1 bootstrap 消费 readiness verdict，任一 artifact 缺失、过期、不可解析或未双审
  都拒绝启动。

### B10. 同一 Episode 无法完整延伸到 Gate 4–7

当前 business world 主要是文字合同与隐藏 truth，缺少 frozen world package、semantic
contract bundle、snapshot/generator、capability profile、result oracle、privacy policy 和
UI observation contract。后续 Gate 另写 fixture 会造成 world drift。

关闭条件：

```text
Immutable EvaluationEpisodeCore
├── Gate3SemanticProfile
├── Gate4DataEvidenceProfile
├── Gate5ClaimPublicationProfile
├── Gate6WorkbenchProfile
└── Gate7ReleaseProfile
```

所有 profile 绑定同一个 world package hash，只增加 gate-specific observables 和 graders，
不能改写 decision stakes、acceptable outcome 或 forbidden outcome。

## 4. Major findings

| ID | Finding | Closure |
|---|---|---|
| M1 | injection point 使用 `after_measurement_proposal`、`concurrent_with_effect` 等固定节点 | 改为用户可观察 trigger predicate，定义 alternative trigger、timeout 和 no-trigger disposition |
| M2 | 多 estimand 只有一个 scalar claim ceiling | claim ceiling 绑定 claim/estimand family |
| M3 | 部分 valid design family 没有 design-conditional oracle | 保存 design-invariant obligations 与各 family 的 conditional expected envelope |
| M4 | meaning-preserving sibling 有时删除确认压力、越界请求或歧义 | 重标 interaction-changing，并补真正同义改写 |
| M5 | 24 个 Episode 复用两套通用 trace 文本 | 用 episode-specific decision points 与 required observations；authority trace 移入 overlay |
| M6 | hidden truth 容易被 grader 当成唯一答案 | 拆分 world fact、agent-observable fact、identifiable conclusion、latent-unidentifiable fact |
| M7 | 独立 business world 数量和作者独立性无法证明 | 加 authoring batch、world lineage、近重复审计和 reviewer independence |
| M8 | grader calibration 没有 agreement、critical false-pass 和过期门槛 | policy 固定分层样本量、agreement、critical false-pass=0 与重新校准条件 |
| M9 | run policy 的 paraphrase/repeat/pairwise/higher-order floor 未执行 | RunManifest validator 计算应运行集合并核对实际完成集合 |
| M10 | merged catalog union test 只比较 ID | deterministic generator + canonical Episode hash equality |
| M11 | policy 同版本可被降级后重生成 ledger | policy schema、版本内容不可变、monotonic comparator 与三方审批 |

## 5. 指定 Episode 修订

- `G3-GF-003-CF02`：删除无法由 mutation 推导的结果方向；
- `G3-GF-011-CF04`：Finance contract 失效后保留财务决策语义，进入 missing-contract/
  clarification boundary；
- `G3-GF-006-CF02`：删除 Gate 3 `settled` 选项；
- `G3-GF-012-CF04`：若 measurement 和 disposition 应保持，改为 robustness/
  interaction relation；
- `G3-EXP-001/002/004/005/006/007/008/009/010/012` 的复合 siblings 拆为单因素或显式
  interaction；
- `G3-ADV-003/008/009/010` 的 CF01 补真正 meaning-preserving 改写；
- `G3-ROOT-001-CF03` 拆分 entity mapping 修复与 bonus-owner 决策。

## 6. 修复顺序

1. E0.1：Episode core、support expectation、EvaluationClock、canonical taxonomy；
2. E0.2：Source/Review Registry、promotion authority 与 provenance reclassification；
3. E0.3：Agent/Evaluator 双视图、Grader/Conformance profiles、RunResult；
4. E0.4：逐 Episode 修复反事实、conditional oracle、claim ceiling 与 business world；
5. E0.5：真实用户/专家来源采集、双审、grader calibration、protected held-out；
6. E0.6：唯一 readiness manifest、read-only verification 和独立 closure audit。

任何一步都不能通过降低 policy floor、隐藏 gap 或把失败改成 runtime 单例规则来关闭。

## 7. 本轮即时 closure

| Finding | Disposition | Evidence |
|---|---|---|
| B2 EvaluationClock missing | **Closed at authoring-contract level** | schema 要求每个 business world 提供 as-of instant、business timezone、calendar contract、business-day contract 与 release cutoff；37 个候选均已冻结 clock。具体 clock 与业务 truth 的匹配仍需 measurement review。 |
| M10 merged catalog union only compares IDs | **Closed** | unit test 比较 candidate 与 merged catalog 的完整 canonical Episode object，任一 hidden truth、outcome 或 grader 漂移都会失败。 |
| M9 default check rewrites ledger | **Closed** | ledger generation 与 read-only freshness check 已拆成两个命令；Gate-ready command 独立 fail closed。 |

## 8. 真实用户问题集追加后的审查增量

2026-07-30 用户提供了八条付费金额分析原始问题。原文已单独保存，并形成
`G3-USER-001` 至 `G3-USER-008` 八个 `real_user_language` candidate。更新后的 authoring
checkpoint 为：

- 45 个 base Episode；
- 8 个有 durable source trace 的真实用户措辞 Episode；
- 21 个多轮 Episode；
- 41 个 high/critical Episode；
- 五个 source pool 的数量 floor 已满足；
- 45 个 Episode 仍全部处于 `candidate/authoring`；
- `policy_ready=false`。

这次追加关闭了“真实用户措辞数量为零”的采集缺口，没有关闭 B4。source registry、
business owner / measurement reviewer 双审、Episode world/expectation 审核与 promotion
authority 仍缺失。八个业务 world 明确标记为测试作者拟合，不能把真实问题措辞的来源证明
扩张成 expectation 已被用户认可。

新增 Episode 覆盖变化解释、规律、事件影响、收入健康、维度/因子归因、异常、多基准和证据
质量。它们形成一个高价值产品 slice，同时仍需与其他行业、指标、定义、cohort、funnel、
因果和无时间问题共同组成 Gate 3 测试集。
