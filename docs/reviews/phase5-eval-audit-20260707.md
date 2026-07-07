# Phase 5 Eval Audit Report

生成日期：2026-07-07

## 现状是什么

- Phase 4 全周期 10 case 已可作为 Phase 5 输入：3 个通过，7 个降级，0 个阻断或失败。
- 业务主结论发布状态：3 个已发布，7 个因证据边界未发布主结论。
- 隐性澄清套件已有 4 个用例，期望状态为 needs_question 4 个。
- 当前没有发现全量隐性澄清套件的 live run 汇总产物；已有单测只验证 `needs_question` 路径可被评估辅助函数读取。

## 10 Case 结果

| Case | 状态 | 主证据 | 强度 | 结论发布 | 主要限制 |
|---|---|---|---|---|---|
| full_month_start_vs_mid_end | degraded | compare_period_phases | low/insufficient | 否 | weak_direction, below_materiality_floor |
| full_month_boundary_vs_mid | degraded | compare_period_phases | low/insufficient | 否 | weak_direction, below_materiality_floor |
| full_thu_fri_vs_mon_sun | degraded | weekday_calendar_compare | low/tendency | 否 | weak_direction |
| full_rolling_28_day_growth | passed | rolling_window_compare | medium/supported | 是 | - |
| full_2026_q2_vs_q1 | passed | compare_periods | high/supported | 是 | - |
| full_month_end_vs_mid | degraded | compare_period_phases | low/insufficient | 否 | weak_direction |
| full_wajespecial_vs_other_by_month | degraded | rolling_window_compare | low/insufficient | 否 | insufficient_comparable_periods |
| full_weekend_vs_workday | degraded | weekday_calendar_compare | low/insufficient | 否 | weak_direction, below_materiality_floor |
| full_december_vs_november | passed | compare_periods | high/supported | 是 | - |
| full_q2_vs_q1_by_year | degraded | compare_period_phases | low/tendency | 否 | weak_direction |

## 问题在哪

- 弱证据或降级仍是主问题：7 个 case 不能支撑强结论，主要集中在方向不稳定、低于重要性阈值、可比周期不足。
- route drift 已有可见风险：full_wajespecial_vs_other_by_month, full_q2_vs_q1_by_year。这些 case 证据数字可用，但主能力选择可能影响 replay 信任和证据形态。
- unsupported claim 风险目前被 verifier 和降级状态压住：降级 case 没有发布主结论。后续风险点是摘要文字如果把 tendency 或 insufficient 写成稳定结论，就会越过证据边界。
- ask-question 仍处于潜在歧义验证阶段：用例覆盖了总金额/日均、合并基线/分渠道基线、严格每期/多数倾向、日历窗口/业务事件窗口，但还缺一轮全套运行产物。

## 应该怎么改

- Phase 5 先补全报告和 eval gate：每次 10 case 运行后生成同类审计报告，避免人工翻 JSON 判断证据边界。
- 对 route drift 先记录影响范围和答案影响；只有错配能改变结论或明显损害 replay 信任时，再进入人工审核后的 guardrail 候选。
- 对降级摘要继续压实表达：必须写清证据支持的倾向、没有支持的主结论、缺的业务事件或可比周期。
- 对隐性澄清套件跑完整 harness，结果要记录 `needs_question`、推荐假设和用户可改写出口。

## 进入 Phase 6

- 扩展到更多业务问题族和组合意图。
- 建立更完整的 capability-support ledger，把事件/机制证据缺口从报告风险变成可执行补数路线。
- 在 Phase 5 gate 稳定后，再决定哪些 route drift 模式需要 compiler 固化。

## Phase 5 保留项

- 当前报告只基于已有 10 case 汇总和隐性澄清 YAML；没有重跑 ClickHouse live eval。
- 隐性澄清套件还缺全量执行后的 artifact summary。

## 主线程 Live Rerun 记录

运行时间：2026-07-07

本轮用当前代码重新跑 `evals/phase4/full_period_pattern_cases.yaml`，产物写入 `artifacts/phase-5/live-full-period-20260707/`。结果未达到完整 10 case：前 6 个 case 已写出 Answer Package，第 7 个 `full_wajespecial_vs_other_by_month` 卡在 `design_analysis_route` 的 DeepSeek 响应读取，已人工中断。

已完成 6 个 case 的状态：

| Case | 状态 | 主证据 | 强度 | claim groups |
|---|---|---|---|---|
| full_month_start_vs_mid_end | degraded | compare_period_phases | low/insufficient | 0 |
| full_month_boundary_vs_mid | degraded | compare_period_phases | low/insufficient | 0 |
| full_thu_fri_vs_mon_sun | degraded | weekday_calendar_compare | low/tendency | 0 |
| full_rolling_28_day_growth | passed | rolling_window_compare | medium/supported | 1 |
| full_2026_q2_vs_q1 | passed | compare_periods | high/supported | 1 |
| full_month_end_vs_mid | degraded | compare_period_phases | low/insufficient | 0 |

业务判断：

- 当前代码的 Answer Package claim group 在已通过 case 上可用；降级 case 没有发布主 claim，符合证据边界。
- 当前 live rerun 的主要问题是可观测性：长 LLM 调用期间没有逐 case 进度和当前节点输出，出问题时只能靠中断堆栈定位。
- `WajeSpecial vs 其他渠道` 仍是 Phase 5 route/clarification 重点样本。它的问题不应直接变成 compiler 硬规则，先进入逐 case 调试和 route drift 影响评估。

下一步调试要求：

- live runner 改成逐 case 输出：开始 case、完成 ClickHouse、进入每个 LLM 节点、写出 artifact、失败节点。
- 对卡住 case 单独跑 `business_intent -> boundary_decision -> analysis_route` 三段，记录 prompt、raw response、耗时和是否需要 ask question。
- 完整 10 case live rerun 通过后再更新本报告的 10 case live summary。
