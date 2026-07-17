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

- 4 个代表 case 已完成真实 ClickHouse + 真实 LLM 逐节点闭环；10 case 更大样本和复合意图开放测试进入 Phase 6。
- 隐性澄清套件已有单测和评估字段；完整真实 artifact summary 随 Phase 6 复合意图测试补齐。

## 主线程 Live Rerun 记录（早期）

运行时间：2026-07-07

本轮用当时代码重新跑 `evals/phase4/full_period_pattern_cases.yaml`，产物写入 `artifacts/phase-5/live-full-period-20260707/`。结果未达到完整 10 case：前 6 个 case 已写出 Answer Package，第 7 个 `full_wajespecial_vs_other_by_month` 卡在 `design_analysis_route` 的 DeepSeek 响应读取，已人工中断。

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
- 当前 live rerun 暴露的主要问题是可观测性：长 LLM 调用期间没有逐 case 进度和当前节点输出，出问题时只能靠中断堆栈定位。
- `WajeSpecial vs 其他渠道` 仍是 Phase 5 route/clarification 重点样本。它的问题不应直接变成 compiler 硬规则，先进入逐 case 调试和 route drift 影响评估。

这段记录已被后续逐节点闭环取代，保留它是为了说明 Phase 5 为什么新增 node debug runner 和 Agent Run Workbench。

## Phase 5 逐节点闭环记录

运行时间：2026-07-07

产物目录：`artifacts/phase-5/live-node-system/20260707-v31-prompt-audit-r2/`

本轮按逐节点协议执行真实 ClickHouse + 真实 LLM，覆盖 4 个代表 case：支持型 Q2/Q1 对比、历史负向假设、渠道月度对比、驱动拆解。4 个 case 全部 passed，共 81 个 workflow node、47 次 LLM call。

| Case | 状态 | 节点数 | LLM 调用 | accepted graph | 下一步动作 |
|---|---:|---:|---:|---|---|
| full_2026_q2_vs_q1 | passed | 22 | 13 | data_quality_profile, compare_periods, answer_verify | synthesize_answer |
| full_month_start_vs_mid_end | passed | 21 | 13 | data_quality_profile, compare_period_phases, answer_verify, data_quality_check, pattern_scan, formula_decompose, event_evidence, segment_bridge, outlier_scan | synthesize_answer |
| full_wajespecial_vs_other_by_month | passed | 15 | 8 | data_quality_profile, compare_periods, outlier_scan, answer_verify, data_quality_check, pattern_scan, formula_decompose, event_evidence, segment_bridge | degrade |
| driver_q2_vs_q1_paid_users | passed | 23 | 13 | driver_decomposition, answer_verify | synthesize_answer |

业务判断：

- 数据充分但假设不被支持时，workflow 输出负向业务答案，不再把可回答问题降级成无法回答。
- `WajeSpecial vs 其他渠道` 的早期卡点已通过逐节点 runner 复现并闭环；最终路径保留降级解释，未把 route drift 直接固化成 compiler 硬规则。
- 驱动拆解 case 可由 LLM 自主选择 `driver_decomposition`，并由本地 verifier 保持数字、证据 ref 和 claim 边界。
- Agent Run Workbench 已能把 Answer Package 映射成线性对话、LangGraph path、节点 inspector、accepted graph 侧支和折叠工作流画布。

Phase 5 closeout 验证：

- `python3 tools/phase5/validate_phase5.py`
- `npm run build`
- `git diff --check`

Phase 6 接续项：

- 用更大的 10 case 和随机开放样本继续评估 route drift、复合意图和隐性澄清。
- 扩展八类问题族的 capability coverage。
- 把生产观测、个人归属、固定 customer-safe projection、rerun comparability 和 release gates 留到后续生产门禁。
