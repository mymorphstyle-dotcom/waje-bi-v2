# WAJE BI v2 P4 全因子调查交付报告

状态：`complete`

P4 把付费金额调查从单一公式与常见维度扩展为十个 reviewed 因素域，并保持既有
`IntentRevision -> PlanRevision -> capability -> evidence -> claim -> publication -> delivery`
单权威链。本文只记录当前合同、真实环境证据和仍需数据 owner 补齐的来源。

## 交付边界

| 边界 | 当前实现 |
| --- | --- |
| 因素 SSOT | runtime contract v17 声明支付公式、拉新注册首充、充值档位、支付渠道、增长运营、玩法、日历、内部事件、外部事件、数据质量十个域。 |
| 调查闭环 | `FactorCoveragePlan`、`FactorCoverageOutcome`、`InvestigationBranch` 和 `InvestigationSynthesis` 内容寻址并绑定 accepted plan、snapshot/release、task、ledger evidence、claim 与 limitation refs。 |
| 宽进深出 | capability scheduler 按依赖波次并行执行 reviewed capabilities；分支失败局部结算，恢复严格重放 persisted plan/outcome/branch refs。 |
| 多 Agent | General Agent 可并行运行 1-3 个只读受控调查 Agent；输入只允许 customer-safe artifact，输出必须引用 allowlist，结果保存为结构化 artifact。子 Agent 没有 ThreadHead、BI 查询、claim、publication 或 delivery 权限。 |
| 漏斗 | 新增、注册、首充、新增首日付费及窗口转化率进入 `funnel_decompose`；daily dashboard 与 lifetime first-payment 保持明确边界。 |
| 充值档位 | `amount_bucket` 进入 active dimension catalog；金额、人数、订单、频次、单笔金额、份额与 mix 偏移在档位 reconciliation group 内对账。 |
| 事件 | 内部与外部事件执行 reviewed overlap/window capabilities；无匹配、来源未绑定、来源过期和合同缺失分别结算，任何缺口都不能推出“无影响”。 |
| 推荐 | claim ceiling 继续硬校验。越界可选建议被拒绝并写入 Provider/Workbench audit，已验证事实与 claim 继续发布。 |
| 投影 | 完整 coverage topology 只进入 Workbench；客户路由只读取持久化 customer-safe publication/material refs。 |

P4 沿用现有 append-only audit、run request refs、capability ledger 和 artifact 身份，因此没有新增
PostgreSQL 表或迁移。runtime contract 升至 v17，release manifest 升至 v45。

## 真实数据覆盖

全因素真实 run `run-888cbea1e6f4c2828362185d` 结算 10 个因素域：

- `analyzed`：充值档位、日历周期、数据质量、玩法背景、支付渠道与方式、支付订单公式链；
- `unavailable_data`：外部事件、投放与增长、内部运营事件、拉新注册首充漏斗。

这四个 unavailable 结果来自当前 release 的来源缺口。能力路径和 typed boundary 已经存在；要形成
业务结论仍需 data owner 发布可复用 snapshot/release 与 join contract。玩法数据只支持背景关联，
窗口覆盖、映射、固定效应收敛和序列相关限制继续约束 claim strength。

## 真实 DeepSeek 验收

所有命令显式清除 `OPENAI_API_KEY`，模型出口固定为
`https://api.deepseek.com/chat/completions`。报告中的 `openAiHostedRequestCount` 均为 0，SDK trace
只进入 WAJE sink。

| 用例 | 结果 | 运行观测 |
| --- | --- | --- |
| 全因素变化调查 + 两轮追问 | passed | 首轮 741.375 秒，`completed_with_limits`，38 个 authority refs，publication integrity=true；两轮追问分别 14.515/34.236 秒，均复用已发布材料且没有重跑 BI。 |
| 拉新注册首充漏斗 | passed | 790.156 秒，`completed_with_limits`，36 个 authority refs；明确结算当前 release 没有漏斗数据，人工语言与内容质量审核仍为 pending。 |
| 充值档位结构 | passed | 934.801 秒，`completed_with_limits`，36 个 authority refs，publication integrity=true；持久化排序为支付渠道与方式、支付订单指标链、充值档位与用户价值、玩法投注、日历周期、数据质量。 |
| 双受控调查 Agent | passed | 两个 DeepSeek Agent 并行完成，4.353 秒，生成 2 个引用闭合 artifact，12 条 WAJE trace record，OpenAI key 缺席。 |

真实验收暴露并修复了三类通用终局缺陷：

1. 已完成分析在 narrative/publication 局部失败时，resume loader 曾重复等待不存在的 publication；
   现在读取持久化 post-execution terminal 并返回 typed failure。
2. coverage outcome 曾保存 capability `evidence_ref`，claim edge 使用 ledger `entry_ref`，导致已验证
   claim 无法进入因素排序；现在统一使用持久化 evidence-ledger identity。
3. 可选建议越过 claim ceiling 时曾让事实分析整体失败；现在保留 policy rejection 审计并丢弃
   越界建议，已验证分析继续进入 narrative。

失败尝试保留在 Workbench 作为真实审计记录，没有删除或改写历史。

## 自动化结果

- P4 标准包：13 个场景目录通过校验，10 个 deterministic 场景全部通过；
- P4/能力/权威链重点回归：240 passed；
- 恢复与 resume outbox：54 passed；
- Gateway、Workbench 与客户投影：69 passed；
- Provider/SDK 功能回归：75 passed；release manifest 与 deployment 门禁：23 passed；
- Phase 7 全量（排除 Case B）：1548 passed、40 skipped、13 deselected、0 failed；
- Phase 8：16 passed；TypeScript `tsc --noEmit`、25 个 YAML/合同验证、`git diff --check` 通过。

Case B 没有新建或重跑。人工回答质量审核继续采用 post-delivery advisory 流程，不参与上述硬门禁。

## 本轮文件范围

- 因素与数据合同：`contracts/runtime/clickhouse-analysis-bindings.yaml`、
  `contracts/metrics/paid-amount.metric.yaml` 及事件、玩法、市场 source contracts；
- 能力与运行时：`bi_agent/runtime/factor_coverage.py`、`runtime_contract_registry.py`、
  `formula_graph.py`、`authoritative_task_inputs.py`、`capability_task_adapter.py`、
  `clickhouse_query_compiler.py`、`langgraph_workflow.py`、`post_execution_workflow.py`、
  `semantic_authority_workflow.py`、`agent_task_recovery.py`、`agent_core.py`；
- Workbench/persistence：`bi_agent/conversation/postgres_store.py`、
  `app/api/_conversationStore.ts`、`app/api/agent-runs/route.ts`、
  `app/agent-run-workbench/contracts.ts`、`app/agent-run-workbench/AgentRunWorkbench.tsx`；
- 评测与版本：`evals/general_agent_runtime/p4-cases.jsonl`、P4 result reports、
  `bi_agent/runtime/release_manifest.json` 与相关 Phase 4/7/8 tests；
- 文档：P4 执行计划、target architecture、本文和文档索引。

## 后续阶段

P4 之后优先进入数据覆盖与性能阶段：发布市场漏斗、内部运营事件、外部事件和支付尝试的真实
snapshot/release；把 12-13 分钟级完整调查拆成可观测的查询、claim 和 narrative 延迟预算；完成
本轮真实回答的人工内容评审。Case B、开放网络搜索、更多前端功能和拥有发布权威的多 Agent
仍未进入 P4。
