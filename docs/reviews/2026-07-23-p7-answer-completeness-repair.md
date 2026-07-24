# P7 回答完整性补全闭环验收

日期：2026-07-23
状态：历史验收记录；其中 narrative 自动补全、客户 warning 和同步 verifier 路径已被
`docs/adr/2026-07-20-advisory-publication-human-review.md` 的当前合同明确取代

> 当前有效合同：回答深度、完整性、可读性和行动性只进入交付后质量审计与人工复核。质量发现
> 不触发 writer retry、自动补写、状态降级、客户 warning、撤回或 publication veto，也不占用
> 首答交付关键路径。本页保留为 P7 当时的实现与测试证据，不能作为当前运行时合同。

## 交付边界

P7 在既有单权威链上增加有界的回答补全能力，没有改变 2026-07-20 的人工质量审核决议：

- execution 中 accepted obligation 缺证据时，复用现有 `ClaimCoverageCheckpoint -> PlanPatch`
  增量补查；
- claim settlement 继续把可用证据收敛为 verified claim，无法满足时形成局部 limitation；
- narrative 初稿遗漏必答 claim、显式请求 factor 的必要 public facts 或 limitation 时，只增加一次
  additive completion revision；
- completion Provider 失败或耗尽时保留首稿，客户得到 `completed_with_limits`、安全 warning 和
  已知边界；
- block verifier 的深度、文风、可读性、行动性与主观质量发现继续进入人工审核，不触发自动撤回、
  重写或 publication veto。

## 通用修复

### 1. 内容寻址的完整性 assessment

`AnswerCompletenessAssessment` 逐个读取
`NarrativeMaterialProjection.publication_requirements`，只检查 required blocks：

- 满足、混合或矛盾状态至少绑定一个允许的 claim handle；
- 显式请求 factor 的每个 required fact handle 必须通过该 requirement 的 claim 绑定；
- 所有 requirement limitation handles 必须进入 required block。

assessment 与 source narrative、material projection、required block identities 一起做内容寻址，
回放时重新计算。它不读取用户自由文本，也不对答案打主观分。

### 2. 有界 additive completion

初稿不完整时，编译器保留原有全部 blocks，只为开放 requirement 创建 scoped target。补全 writer
只能使用投影后的 claim/fact/limitation handles；成功后形成同一 AuthorityBundle 下的第二版
narrative，并对新增块执行本地安全检查和 advisory verifier。自动补全最多一次。

### 3. 显式请求 factor 的事实合同

`PlanCompiler` 为每个 typed `requested_factor_ref` 生成独立 user-required obligation，并保留对应的
contract axis 与 dimension refs。`NarrativeMaterialProjection` 通过 metric ref 精确解析：

- 全样本 baseline/target facts；
- contract-declared dimension 的同成员 baseline/target facts；
- 一个稳定的 dimension summary anchor。

`payment_outcome_compare` 现在先跨 payment method/channel 对账全样本 totals，再为每个维度选择
目标期终态订单量最大的可比成员，公开同一成员两个窗口的终态订单、成功订单、截至快照未支付
订单和成功率。选择策略为规模代表，明确标注为代表性、非穷举。

### 4. 证据解释边界

payment final-outcome evidence 带有 typed interpretation contract。writer 和 advisory verifier 都
被要求保留 final-status-as-of-snapshot 语义；观察到的终态变化不能自动扩写为支付流程效率、失败
环节、延迟、重试、事故或因果机制。

### 5. 追问材料读取

publication artifact 暴露客户安全的 claim inventory 与有条数/字节上限的 public fact 摘要。
后续 Agent 可以精确读取已发布但正文未使用的材料，无需重跑完整 BI；raw rows、SQL、Provider
payload、内部 owner/debug 字段和技术错误不会进入客户投影。

### 6. 追问选择阶段的发布保全

动态工具选择的模型步骤发生 Provider 或可重试 SDK runtime 故障时，runtime 只在上下文中存在
客户安全 `bi_publication` 的条件下保全最近一次完整发布。终局为 `context_response` 与
`completed_with_limits`，原 authority refs 保持闭合；客户文本明确本轮新增解释尚未完成。
无 publication、selection 合同错误、上下文错误、未知 refs 或包含内部标识的摘要继续失败关闭。

## 对抗式实测发现与修复

| 发现 | 可复用失败类型 | 修复 |
|---|---|---|
| 大型 material projection 超过 512 KiB | 无损事实传输预算失控 | 列式 public fact transport，保留完整 handle/value 闭包 |
| DeepSeek 偶发空白或非法结构化输出 | Provider structured-output 不稳定 | Provider 层统一校验与有界重试，耗尽后映射 typed error |
| intent 有 requested factors，Plan 只保留主指标 obligation | accepted user factor 丢失 | 每个 typed requested factor 生成 scoped user-required obligation |
| factor 被误放进 primary target metrics | 主指标与调查因子身份混淆 | factors 保留在 axis metrics/obligation edges，primary target 不变 |
| payment outcome 只有分组 profiles，没有全样本 totals | 能力证据缺少可发布汇总 | 跨维度 totals 对账并公开四项 baseline/target facts |
| claim 已出现但关键数字未写入 | claim coverage 被误当成 answer completeness | required public fact handles 加入完整性 assessment 与补全 target |
| accepted axis 含 payment method/channel，首答只写付费金额代表项 | 维度证据存在但无必答事实组 | contract-declared dimension summary facts 接入 factor obligations |
| 终态改善被扩写为处理效率改善 | 观察结果越过过程证据边界 | typed interpretation contract 与通用 evidence-role wording 约束 |
| DeepSeek 在追问工具选择前不可用，已有完整 publication 仍显示空泛失败 | 已发布上下文被前置模型故障遮蔽 | 保全完整 publication，明确新增解释未完成，技术错误只进 Workbench |

## 最终真实 DeepSeek 验收

隔离新线程
`thread-eval-p7_payment_final_outcome_completeness_live-20260723111605c0cdf1-r1`
完成三轮真实验收，硬结果为 `passed`：

- 首答运行 1240.792 秒，形成 `analysis_publication` 与 `completed_with_limits`，1107 字、6 段；
- 首答覆盖付费金额、付费频次/人数/单笔金额分解、支付终局订单、成功订单、截至快照未成功订单、
  支付成功率，以及 WajeSpecial/OPAY 的代表性跨窗口事实；
- 第一轮追问运行 18.674 秒，调用 `inspect_analysis_artifact`，形成 479 字、9 段的
  `tool_response`；
- 第二轮边界挑战运行 20.950 秒，同样读取 publication artifact，形成 818 字、8 段的
  `tool_response`；
- 两轮追问都没有重跑 `run_bi_analysis` 或 `continue_bi_analysis`，authority refs 与 publication
  integrity 均闭合；
- Provider 为 `deepseek-v4-flash`，唯一模型出站为 `https://api.deepseek.com`；
- 环境中没有 `OPENAI_API_KEY`，OpenAI hosted request count 为 `0`。

人工 advisory review 仍记录两处语义强度问题：首答把观察到的终态成功率提升写成“支付转化效率
改善”，并把付费频次增长解释成“付费意愿增强”。当前终态数据不能验证支付过程效率，频次也
不能单独证明意愿。这些发现进入人工审核，不撤销 publication，不触发单句规则或自动重写。

## 自动化验证

- P7 catalog：`11 cases`，其中 `10 pytest`、`1 agent_live`；
- P7 deterministic pack：`10/10 passed`；
- P7 聚焦回归：`168 passed`；
- 完整 Phase 7：`1600 passed, 40 skipped`；
- 支付终态、对账、数据覆盖、首充时间修复与查询编译 Phase 4 定向集：`178 passed`；
- Agents SDK Provider、模型路由、typed failure 与无 OpenAI 出站定向集：`55 passed`；
- deployment contract、release manifest 与 health checks：`24 passed`；
- 合同静态校验通过：28 个 YAML、10 张 capability card、25 条 support record；
- `npm run build` 通过，Next.js production build 与 TypeScript 检查完成；
- `git diff --check` 通过；
- release manifest 升级为 `single-authority.final.release.2026-07-23-v53`，所有 active refs
  与当前路径内容一致。
