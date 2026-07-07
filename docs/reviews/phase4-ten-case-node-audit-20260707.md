# Phase 4 10 Case Node Audit

Date: 2026-07-07

Scope: implementation update plus live retest. This report supersedes the older review-only Phase 4 note.

## Current Baseline

- Full-period case file: `evals/phase4/full_period_pattern_cases.yaml`.
- Full payment table: `paid_order_success_clean_20240101_20260704`.
- Accepted complete-month audit window: 2024-01-01 through 2026-06-30.
- Source coverage: raw payment detail covers 2024-01-01 through 2026-07-04; July is excluded from complete-month pattern claims.
- Retest artifact root: `artifacts/phase-4/ten-case-node-audit-20260707-postfix/`.
- Retest summary artifact: `artifacts/phase-4/ten-case-node-audit-20260707-postfix/node_audit_summary.json`.

## Case Set

| Case | Group | Business Question |
|---|---|---|
| `full_month_start_vs_mid_end` | historical_risk_recheck | 全量样本看，2024-01到2026-06每个月月初1-10号付费金额是否高于月中和月末？ |
| `full_month_boundary_vs_mid` | historical_risk_recheck | 全量样本看，2024-01到2026-06每个月月边界窗口（1-8号或26号以后）相比月中是否更高？ |
| `full_thu_fri_vs_mon_sun` | historical_risk_recheck | 全量样本看，周四/周五的付费金额是否稳定高于周一/周日？ |
| `full_rolling_28_day_growth` | historical_risk_recheck | 全量样本看，28日滚动付费金额是否呈持续增长模式？ |
| `full_2026_q2_vs_q1` | historical_risk_recheck | 2026年Q2相比Q1，日均付费金额有没有明显抬升？ |
| `full_month_end_vs_mid` | random_seed_407 | 全量样本看，2024-01到2026-06每个月月末21号以后付费金额是否高于月中？ |
| `full_wajespecial_vs_other_by_month` | random_seed_407 | 全量样本看，WajeSpecial渠道的月度日均付费金额是否稳定高于其他渠道合计？ |
| `full_weekend_vs_workday` | random_seed_407 | 全量样本看，周末付费金额是否稳定高于工作日？ |
| `full_december_vs_november` | random_seed_407 | 全量样本看，12月相比11月的日均付费金额是否更高？ |
| `full_q2_vs_q1_by_year` | random_seed_407 | 全量样本看，每年Q2相比Q1的日均付费金额是否都有抬升？ |

## Retest Results

| Case | Expected | Actual | Primary Evidence | Review |
|---|---:|---:|---|---|
| `full_month_start_vs_mid_end` | degraded | degraded | 中位下降 7.1%，方向命中 10.0%，30/30 周期 | 正确降级。全量数据不支持“月初高于月中和月末”。 |
| `full_month_boundary_vs_mid` | degraded | degraded | 中位提升 1.8%，方向命中 36.7%，30/30 周期 | 正确降级。幅度和方向都不足。 |
| `full_thu_fri_vs_mon_sun` | degraded | degraded | 中位提升 5.9%，方向命中 63.8%，130/120 周期 | 正确降级。存在倾向，但未达到稳定模式阈值。 |
| `full_rolling_28_day_growth` | passed | passed | 中位提升 21.0%，方向命中 82.8%，29/24 周期 | 正确通过。coverage 可回答缺口已继续执行证据路径。 |
| `full_2026_q2_vs_q1` | passed | passed | 日均付费金额提升 15.0%，方向命中 100.0%，1/1 周期 | 正确通过。coverage LLM 不能无本地证据阻断可执行对比。 |
| `full_month_end_vs_mid` | degraded | degraded | 中位提升 3.9%，方向命中 56.7%，30/30 周期 | 正确降级。最终摘要已补核心数字。 |
| `full_wajespecial_vs_other_by_month` | degraded | degraded | 日均付费金额提升 157.3%，方向命中 93.1%，29/30 周期 | 正确降级。29/30 是证据边界，不再归为数据工程外部阻断。 |
| `full_weekend_vs_workday` | degraded | degraded | 中位提升 0.4%，方向命中 33.8%，130/120 周期 | 正确降级。幅度和方向都不足。 |
| `full_december_vs_november` | passed | passed | 日均付费金额提升 52.4%，方向命中 100.0%，2/2 周期 | 正确通过。`compare_periods` 已作为主证据识别。 |
| `full_q2_vs_q1_by_year` | degraded | degraded | 日均付费金额提升 15.0%，方向命中 66.7%，3/3 周期 | 正确降级。并非每年都抬升，只能保留为倾向性观察。 |

## Implemented Changes

1. Eval harness 主证据识别不再绑死 `pattern_scan`；`compare_periods`、`compare_period_phases`、`weekday_calendar_compare`、`rolling_window_compare`、`event_window_compare` 都可作为主模式证据。
2. `insufficient_comparable_periods` 不再自动映射为 `external_dependency_blocked`。只有 ClickHouse env、SQL safety、runtime binding 或 query failure 这类真实外部依赖才归 data engineering owner。
3. coverage LLM 的 `blocked` 必须有本地 validator 失败、无数据行或必需字段缺失；否则改成可审计 warning 并继续执行证据路径。
4. coverage LLM 的 `coverage_gap_but_answerable` 继续执行证据路径，缺口留给结论边界表达，不提前终止。
5. 最终业务摘要的降级 fallback 强制包含主证据数字：中位变化、方向命中率、可比周期、最低周期要求和重要性阈值。
6. scope 和 metric 展示归一化：`{"type":"all"}`、`all`、`full_sample` 显示为全量/全样本；`daily_paid_amount`、`avg_daily_paid_amount` 等机器指标显示为付费金额或日均付费金额。

## Remaining Product Issues

1. Route 选择仍有语义漂移：WajeSpecial 月度渠道对比会走 `rolling_window_compare`，Q2 by year 有时会走 `compare_period_phases`。当前证据数字正确，默认错配率尚未高到需要 Phase 4 追加硬边界；Phase 5 开放测试后再按错配率、答案影响和 replay 审计体验决定是否让 compiler 按 question_family + evidence_shape 固化主能力选择。
2. 降级摘要已经有数字，但部分表达仍偏保守模板。下一轮应让 LLM 更明确说明“证据支持了什么倾向、没有支持什么主结论”。
3. 事件/机制证据仍为空，所以所有机制解释只能写成缺口或候选观察。

## Phase 5 Marker

- Route boundary: measure whether `compare_periods` / `rolling_window_compare` / `compare_period_phases` route drift materially changes evidence shape, answer wording, or replay trust before adding deterministic compiler rules.
- Clarification trigger eval: add a separate implicit-ambiguity suite. Cases should not be obvious missing-field prompts; they should look answerable but contain latent choices that can change conclusion quality, such as total amount vs daily average, combined baseline vs per-segment baseline, strict every-period stability vs majority-period tendency, and calendar window vs business-event window.
- Ask-question policy: if that latent choice can change the main conclusion, claim strength, baseline, or recommended action, the workflow should ask or present a recommended assumption with an explicit user override path.

## Verification

- `python3 -m unittest discover -s tests/phase4` -> 130 tests passed.
- 10 live cases retested against ClickHouse + DeepSeek model. Final status: 3 passed, 7 degraded, 0 blocked, 0 failed.
- Visible text scan: no `daily_paid_amount`, `avg_daily_paid_amount`, `full_sample`, dict repr, `pattern_family`, or `capability_id` leaked in `answer_text` / `final_business_summary`.
- Numeric coverage scan: all 10 summaries include the primary evidence numbers.
