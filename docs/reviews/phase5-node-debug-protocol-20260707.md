# Phase 5 Node Debug Protocol

生成日期：2026-07-07

## 测试用例顺序

测试集来源：`evals/phase5/node_debug_cases.yaml`。

| 顺序 | Case | 评审重点 | 停止策略 |
|---|---|---|---|
| 1 | `full_2026_q2_vs_q1` | 支持型自定义基线对比，确认通过路径和 claim group。 | 逐节点跑到 `final_business_summary`。 |
| 2 | `full_month_start_vs_mid_end` | 历史高风险问题，确认全量数据不支持时输出负向业务答案。 | 重点看 `execute_capabilities`、`decide_next_action`、`synthesize_answer`、`semantic_audit`。 |
| 3 | `full_wajespecial_vs_other_by_month` | 上轮 live rerun 卡点，检查 route drift 和潜在澄清需求。 | 先跑到 `accept_analysis_route`，确认 accepted graph 不含未执行或错配能力。 |

## 执行规则

- 每次只跑一个 case 的一个节点。
- 每个节点运行后先看 `node_debug_reviews[-1]`，确认 `changed_keys`、`llm_tasks_added`、checkpoint 状态。
- LLM 节点一次只新增一个 LLM call；如果新增多个，停止评审。
- 卡住或失败时保留当前 state，不继续下一个节点。
- route drift、ask-question、unsupported claim 不自动转 guardrail，只写入评审。

## 命令模板

初始化单个 case，只做 ClickHouse 聚合，不调用 LLM：

```bash
python3 tools/phase5/debug_node_runner.py init-case \
  --case-id full_2026_q2_vs_q1 \
  --state-path artifacts/phase-5/node-debug/full_2026_q2_vs_q1/state.json
```

执行一个节点，并把 state 写回：

```bash
python3 tools/phase5/debug_node_runner.py run-node \
  --state-path artifacts/phase-5/node-debug/full_2026_q2_vs_q1/state.json \
  --node understand_business_intent
```

继续下一个节点：

```bash
python3 tools/phase5/debug_node_runner.py run-node \
  --state-path artifacts/phase-5/node-debug/full_2026_q2_vs_q1/state.json \
  --node decide_question_boundary
```

## 第一轮节点清单

`full_2026_q2_vs_q1`：

1. `understand_business_intent`
2. `decide_question_boundary`
3. `clarification_policy_gate`
4. `confirm_business_understanding`
5. `design_analysis_route`
6. `accept_analysis_route`
7. `inspect_schema`
8. `validate_runtime_binding`
9. `interpret_data_coverage`
10. `execute_capabilities`
11. `reduce_evidence`
12. `decide_next_action`
13. `interpret_evidence`
14. `synthesize_answer`
15. `semantic_audit`
16. `hard_verify_answer`
17. `final_business_summary`
18. `persist_artifact`

## 当前判断

这套调试方式会比完整 live rerun 慢，但每一步都可审计，且只在当前节点花 LLM 成本。上轮卡点发生在 `full_wajespecial_vs_other_by_month` 的 `design_analysis_route`；按本协议重跑时，前置节点通过后才进入该节点。
