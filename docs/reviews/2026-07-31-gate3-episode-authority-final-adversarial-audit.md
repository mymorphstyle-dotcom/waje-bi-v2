# Gate 3 Episode Authority 最终组合对抗审查

> 2026-07-31 收口更新：下文记录的机器 authoring 缺口已经关闭。当前证据与仍需真人或
> 外部控制面的门禁以
> `docs/reviews/2026-07-31-gate3-1-formal-closure-audit.md` 为准。

日期：2026-07-31
范围：36 个 Required Episode、8 个 launch case、120 个 counterfactual、
Agent/Evaluator projection、claim/truth/source authority、三层结果、
calibration、USER008 authority repair 与 G3.E0 readiness。
结论：本轮发现的可复现 P0/P1 correctness gap 已修复并加入回归测试；
formal admission 继续为 `deny_g3_1`。

## 1. 审查方法

主审与独立分支从四条路线攻击当前合同：

1. WAJEgame 业务问题、指标口径、可识别性和逐 claim gold；
2. source、contract、truth、counterfactual 和 projection 的跨层 authority
   连续性；
3. frozen run、grader registry、artifact、veto、human review 和 calibration
   的不可变绑定；
4. schema-invalid、stale hash、未来事件、同名 ref、伪 artifact、伪 veto、
   跨 Episode review 与重复投递的 fail-closed 行为。

每个 finding 都要求提供最小复现。修复以通用 authority 模式落地，没有增加题目关键词、
单题分支或业务语义字典。

## 2. 已关闭的主要 correctness gap

| 失败类型 | 可复现风险 | 通用修复 |
|---|---|---|
| counterfactual 只允许单字段替换 | 一个合同释放需要同时更新 contract state、access、source binding 和 materialization；单字段 mutation 会制造自相矛盾世界 | mutation 改为同一语义维度、同一 authority surface 的原子 JSON Pointer patch set；机械重放后重算 sibling hash |
| claim 与 source 状态矛盾 | gap-only claim 可以自报 supported、accepted 或 settled | source mode、support state、applicability、resolution、verifier、settlement 形成硬矩阵；known gap 必须解析到真实 missing-contract backlog |
| 未来信息进入 Agent view | future contract release 可经 public context 提前泄漏；同名 contract/source ref 可让 later-turn binding 提前出现 | contract、condition、source binding 分别按自己的 release/available turn 投影，禁止用扁平 ref set 代替对象级可见性 |
| 未来 truth support 反向授权早期 claim | claim turn 1 可以引用 turn 2 才可识别的 oracle truth | `identifiable_from_world` 的 support 在每个 claim `evaluation_turn` 重新验证；未来 release 不能授权早期 gold |
| launch 业务口径漂移 | “收入”与付费金额/gross successful paid amount 混用；部分 Episode 在未澄清时给出强 gold | USER004/005/006 增加真实第二轮口径确认，claim evaluation 延后；USER001 意义保持 sibling 固定为 gross 成功付费金额 |
| counterfactual affected claim 过宽或过窄 | 一个 baseline/window/coverage/decision-goal 变化会错误取消无关结论，或继续强迫回答已退出用户范围的结论 | 每个 sibling 逐 claim 绑定 base digest、authority effect 和完整 unaffected set；USER003 的支付故障收窄只重算两项支付 claim，其余六项显式 supersede/omit |
| 范围变化被强制整包重算 | 新请求只保留部分旧结论时，旧合同会把已经离开 scope 的 claim 继续当作 gold | claim effect 增加 `not_applicable`、`supersede_or_omit` 与 `clear`；aggregate summary 可为 `mixed`，逐 claim 仍必须落到明确处置 |
| `mixed` 汇总掩盖矛盾的逐 claim 处置 | 同一 claim 可同时声明“证据拒绝、结论删除”和“支持状态/边界需要重算”，利用 aggregate 的混合值通过检查 | 逐 claim 改为完整 authority effect profile 校验；measurement identity、prior evidence、support、disposition、ceiling 和 boundary 必须形成允许的原子组合 |
| stale claim 跳过了仍需保留的边界引用 | materialized sibling 可删除 `expand_or_preserve` 所要求保留的业务条件，同时借 affected claim 的 stale 状态绕过 world-ref 检查 | 仅当该 claim 明确声明 `boundary_codes=recompute/clear` 时允许旧 boundary ref 退出；`preserve/expand_or_preserve` 始终检查引用存在且 Agent 可访问 |
| 同一业务槽位暴露相反 authority | 素材/版本 coverage 反事实曾同时让 Agent 看到完整与不完整两套 assignment 来源，任一选择都有输入依据 | 活动预算与素材版本 authority 物理拆分；case-file 增加 `authority_slot_id`，base 与每个 materialized sibling 在同一 slot 最多暴露一个 authority，冲突直接阻断 readiness |
| 世界描述与 immutable fixture 冲突 | 只改 data condition 文案，却继续绑定声明“覆盖完整”的旧 fixture authority | 覆盖变化同时更新 world、contract 与 source binding；多 authority surface 的单一干预显式标记 `composite_authority` 并机械重放 |
| counterfactual-only authority 绕过准入 | base authority 全部完成审查时，新 sibling replacement fixture 仍可能未物化、未双审 | readiness 物化所有 executable sibling，收集 source identity 发生变化的 binding，把 replacement authority 纳入同一 materialization 与独立双审门槛 |
| 新指标或因果升级缺少证据 | gross 证据被用于净收入，描述性渠道 driver 被要求升级为因果 | 新 metric/claim-strength 请求先查 typed contract gap；退款/冲正与 causal exposure/control 缺失时局部降级或拒绝升级 |
| run manifest 可被当前 grader registry 重新解释 | manifest hash正确，但 registry 在冻结后漂移 | `run_manifest.grader_registry_sha256` 必须等于当前 canonical registry hash；profile、predicate、check 集合继续逐层解析 |
| fail/blocked 结果可引用伪 artifact | 旧校验只在 final pass 时核对 runner index | 所有 verdict 的每层 artifact 都必须与 runner-verified index 精确一致 |
| 调用方可制造任意 critical veto | 任意字符串 veto 可以把全 pass 改成 fail | veto 改为 typed record，必须绑定已注册的 failed child check 和同层 indexed artifact |
| calibration 标签可跨 Episode 借 review | label A 可引用 Episode B 的有效 review，grader result hash也未解析 | 新增专用 human calibration review；label 同时绑定 Episode/core、human verdict、immutable grader result 和 runner artifact index，所有文件重算 hash 并执行完整 result validation |
| invalid 输入导致 verifier traceback | schema-invalid catalog 或 result 在后续直接字段访问时抛异常 | catalog validation 失败立即生成结构化 blocked readiness；invalid calibration result 停止交叉字段读取并返回 finding |
| USER008 repair 可伪造对象或 disposition | payload hash、journal object、claim target、Evidence/Answer scope 缺少完整交叉校验 | 19 个 milestone、19 个 observation、8 个 claim disposition 逐对象解析；加入 payload tamper、ghost object、invented claim、duplicate disposition、invalidated Evidence 等 11 个负例 |

## 3. 当前测试权威

- Required catalog：36 个 WAJEgame Episode；
- transfer research：4 个物理隔离、不可进入 Gate 的 probe；
- launch case：8 个完整 claim case，24 个 executable counterfactual，
  对应 32 个 base/sibling run variant；
- authority repair：USER008 具备 19 个 milestone、19 个 observation、
  8 个 claim disposition；
- Gate 3 authoring、projection、trust 与 GitHub admission tests：全部通过；
- Python 3.12.13 clean-copy suite：222 个通过，8 个环境依赖测试跳过；
- readiness：`derived_status=blocked`、`entry_decision=deny_g3_1`。

## 4. 仍然开放的正式门禁

以下是已知 authoring/admission 工作，当前没有被本轮本地通过结果掩盖：

- 41 个 case-file authority 已完成 52 条 hash-bound materialization，仍处于
  `authoring` 并等待两位独立 reviewer；
- 36 个 Required Episode 尚未完成独立 business owner 与 measurement reviewer
  双审；
- 54 个 truth fact 尚待 identifiability/support review；
- protected external admission、真实 Sigstore bundle、grader calibration、
  sealed held-out、promotion approval 和 frozen run 尚未完成。

这些条件关闭前，当前 corpus 只能作为可执行 authoring authority，不能授予 production
Evidence、settled Answer、completed Workflow 或正式 G3.1 admission。

## 5. 工作树归属说明

根目录历史文件 `contracts/backlog/missing-contracts.yaml` 在本轮收口前已经处于修改状态。
vNext validator、Episode、readiness 和 clean-copy 构建只读取
`vnext/contracts/backlog/missing-contracts.yaml`，删除整个历史实现树后仍能独立完成
Python 3.12.13 构建和 222 项测试。为避免删除未确认归属的既有工作树内容，本轮没有回退
根目录文件；该文件明确排除在本轮 Gate 3 交付物之外。

## 6. 下一步

下一步继续 case-file 物化和逐 Episode 双审。每个 review 以自然用户问题、业务世界、
estimand/claim、source applicability、boundary、counterfactual 和 reversal 为完整案卷，
不得用当前 runtime 实现反推 gold。完成 launch 后按同一合同补齐 28 个 non-launch
claim case 和 96 个 atomic counterfactual，再进入 calibration、held-out 和 frozen run。
