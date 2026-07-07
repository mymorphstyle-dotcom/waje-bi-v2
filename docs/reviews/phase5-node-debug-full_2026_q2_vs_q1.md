# Phase 5 Node Debug Review

Date: 2026-07-07

## Scope

本轮按逐节点方式调试 3 条真实 case：

1. `full_2026_q2_vs_q1`
2. `full_month_start_vs_mid_end`
3. `full_wajespecial_vs_other_by_month`

所有 state 均保存在 `artifacts/phase-5/node-debug/<case_id>/state.json`。

## Case 1: Q2 vs Q1

问题：`2026年Q2相比Q1，日均付费金额有没有明显抬升？`

状态：完整跑到 `persist_artifact`。

结果：
- accepted graph: `data_quality_profile`, `compare_periods`, `answer_verify`
- verifier: `passed`
- artifact: `artifacts/phase-5/node-debug/phase5-node-debug-full_2026_q2_vs_q1/answer_package.json`
- 结论：Q2 相比 Q1 日均付费金额提升 15.0%，当前窗口内有可观察的明显变化。

修复点：
- route 不再暴露 `evidence_reduce` 作为 LLM 可请求 capability。
- `scope=整体/全量样本` 统一归一为 `full_sample`。
- custom baseline 文案去掉“方向命中率/1 个可比周期”的业务展示，保留在 claim numbers 供 verifier 使用。
- final summary 日期文本不再被误判为“固定未来几个月后”。

## Case 2: 月初 vs 月中/月末

问题：`全量样本看，2024-01到2026-06每个月月初1-10号付费金额是否高于月中和月末？`

状态：完整跑到 `persist_artifact`。

结果：
- accepted graph 包含阶段对比和 Phase 4 pattern 必需路径。
- verifier: `passed`
- artifact: `artifacts/phase-5/node-debug/phase5-node-debug-full_month_start_vs_mid_end/answer_package.json`
- 结论：当前统计证据不支持“月初高于月中和月末”。30 个可比月份中仅 10% 支持该方向，中位变化为 -7.1%。

修复点：
- `next_action=degrade` 在“数据充分但证据不支持假设”场景下会被本地 policy 改为负向答案合成。
- 负向 pattern claim 明确写入“不支持目标声明”，避免 semantic audit 判为 unlisted claim。
- 负向证据解释清理 “目标索赔”“中位提升为-7.1%” 等不合格表达。
- final summary 支持 `10%` 和 `10.0%` 两种百分比展示。

## Case 3: WajeSpecial vs 其他渠道

问题：`全量样本看，WajeSpecial渠道的月度日均付费金额是否稳定高于其他渠道合计？`

状态：逐节点跑到 `accept_analysis_route`。

结果：
- intent: `pattern_explanation + custom_baseline`
- route normalized requested nodes: `data_quality_profile`, `compare_periods`, `outlier_scan`, `answer_verify`
- compiler: `accepted`
- accepted graph 不再包含 `metric_timeseries` 或 `rolling_window_compare`

修复点：
- 中文业务实体里的英文/数字专名按通用规则保留，不再通过单个品牌词白名单绕过 fallback。
- LLM 发明的稳定性百分比被清理为“足够多的周期/月份”，不在用户可见文本暴露未经合同确认的数字阈值。
- route 层过滤当前没有执行 harness 的 public catalog 能力，例如 `metric_timeseries`。
- custom baseline pattern 的 `rolling_window_compare` 会归一为 `compare_periods`。
- route visible output 会按规范化后的 requested nodes 改写，避免 replay 继续显示已移除的候选路径。

## Verification

通过：

```bash
python3 -m unittest tests.phase4.test_llm_workflow
python3 -m unittest tests.phase5.test_node_debug_runner tests.phase5.test_answer_package_claim_groups tests.phase5.test_phase5_eval_harness tests.phase5.test_phase5_report_generator
python3 -m unittest tests.phase4.test_recipe_registry_and_compiler tests.phase4.test_capability_registry tests.phase4.test_capability_harness tests.phase4.test_exploration_budget
python3 tools/phase5/validate_phase5.py
```

`validate_phase5.py` 结果：

- phase4 discover: 140 tests passed
- phase5 discover: 9 tests passed
- `git diff --check`: passed

## Current Judgment

我认为本轮节点调试可以停在这里：

- 支持型答案、负向答案、route drift 三类关键风险都已覆盖。
- 两条完整 case 已到 verifier passed + artifact persisted。
- WajeSpecial route 漂移已在 route/compiler 层闭环，后续再做完整执行即可验证数值输出。
