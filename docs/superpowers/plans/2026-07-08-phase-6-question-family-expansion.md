# Phase 6 Question Family Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand the Phase 5 agent workflow from the first pattern slice to representative end-to-end coverage across the launch question-family matrix.

**Architecture:** Keep LangGraph as the visible workflow runner and keep WAJE-owned contracts, compiler, capability execution, evidence envelopes, verifier, and Answer Package as the truth boundary. LLM nodes make the analyst-style business judgment: intent, ambiguity, analysis route, evidence interpretation, causal wording audit, and final business answer. Local code checks schema, SQL safety, contracts, evidence refs, numeric facts, permissions, and claim boundaries.

**Tech Stack:** Python BI Agent Core, LangGraph, ClickHouse runtime, DeepSeek/OpenAI-compatible LLM adapter, YAML eval manifests, existing node debug runner, Next.js Agent Run Workbench.

## Global Constraints

- Use real ClickHouse rows and real LLM calls for Phase 6 acceptance; fixture tests only protect local code behavior.
- Keep raw SQL execution behind local runtime and validators; LLM never executes raw SQL.
- Do not hard-code eval case wording, WajeSpecial, Q2/Q1, or month-start behavior into runtime rules.
- Treat ask-question as an optional clarification branch when ambiguity can change baseline, scope, route, claim strength, or cost.
- Do not promote route drift into compiler guardrails without human review and a reusable failure pattern.
- Keep user-facing answers in simplified Chinese business language.
- Keep production observability, permission-filtered sharing, rerun comparability, and release operations in Phase 8.

---

## Current Entry State

- Phase 5 closeout commit: `09bc9e4b Close Phase 5 agent workflow`.
- Latest real node-by-node artifact: `artifacts/phase-5/live-node-system/20260707-v31-prompt-audit-r2/`.
- Existing live runner: `tools/phase5/run_live_node_system_test.py` accepts custom `--case-file` and `--artifact-root`, so Phase 6 should reuse it before adding another runner.
- Existing recipes cover all launch families, but most non-pattern entries still have `default_degraded=True`.
- Existing capability execution already runs `data_quality_check`, pattern comparisons, `formula_decompose`, `driver_decomposition`, `segment_contribution`, `outlier_contribution`, `event_evidence`, `segment_bridge`, `outlier_scan`, and `joint_attribution` inside `bi_agent/runtime/langgraph_workflow.py`.

## Phase 6 Test Scope

Phase 6 starts with 12 representative live cases:

| Case | Question family | Business question | Main capabilities expected |
|---|---|---|---|
| phase6_pattern_month_start_negative | pattern_explanation | 全量样本下月初付费金额是否持续高于月中和月末？ | compare_period_phases, pattern_scan |
| phase6_pattern_channel_monthly | pattern_explanation | WajeSpecial 月度日均付费金额是否稳定高于其他渠道合计？ | compare_periods, outlier_scan |
| phase6_paid_amount_q2_driver | paid_amount_change_explanation | Q2 付费金额提升主要来自付费用户数还是单付费用户金额？ | driver_decomposition |
| phase6_paid_amount_channel_mix | paid_amount_change_explanation | Q2 增长里不同渠道分别贡献了多少？ | segment_contribution, driver_decomposition |
| phase6_business_object_campaign | business_object_impact_review | 某活动后付费金额有没有改善？ | event_evidence, compare_periods |
| phase6_segment_channel_attribution | segment_or_factor_attribution | 哪些渠道解释了 Q2 相比 Q1 的变化？ | segment_contribution |
| phase6_segment_joint_candidate | segment_or_factor_attribution | 渠道和时间段组合是否解释了主要变化？ | segment_contribution, joint_attribution |
| phase6_revenue_health_h1 | revenue_health_review | 2026 上半年付费金额健康度如何，风险在哪里？ | data_quality_check, formula_decompose, outlier_scan |
| phase6_anomaly_single_period | anomaly_or_black_swan_review | Q2 的增长是否被少数异常日期撑起来？ | outlier_contribution, outlier_scan |
| phase6_custom_baseline_release | custom_baseline_comparison | 某业务窗口相比发布前基线是否改善？ | compare_periods, driver_decomposition |
| phase6_data_quality_trust | data_quality_or_evidence_review | 当前付费数据能不能支撑渠道对比结论？ | data_quality_check |
| phase6_composite_change_and_reason | paid_amount_change_explanation + segment_or_factor_attribution | Q2 为什么增长，主要渠道和用户数/客单价分别怎么贡献？ | driver_decomposition, segment_contribution |

## Task 1: Phase 6 Live Case Manifest

**Files:**
- Create: `evals/phase6/live_question_family_cases.yaml`
- Create: `tests/phase6/test_live_question_family_cases.py`

**Interfaces:**
- Consumes: existing Phase 4/5 case fields accepted by `tools/phase5/run_live_node_system_test.py`.
- Produces: a Phase 6 manifest that can run through the existing node-by-node runner.

- [ ] **Step 1: Write the manifest test**

```python
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
CASE_FILE = ROOT / "evals" / "phase6" / "live_question_family_cases.yaml"


class Phase6LiveQuestionFamilyCasesTest(unittest.TestCase):
    def test_manifest_covers_launch_families_and_composite_intent(self):
        data = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
        cases = data["cases"]
        families = {case["primary_question_family"] for case in cases}
        self.assertGreaterEqual(len(cases), 12)
        self.assertTrue(
            {
                "pattern_explanation",
                "paid_amount_change_explanation",
                "business_object_impact_review",
                "segment_or_factor_attribution",
                "revenue_health_review",
                "anomaly_or_black_swan_review",
                "custom_baseline_comparison",
                "data_quality_or_evidence_review",
            }.issubset(families)
        )
        self.assertTrue(any(len(case.get("secondary_question_families", ())) > 0 for case in cases))

    def test_cases_are_live_only_and_reviewable(self):
        data = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["artifact_policy"], "real_clickhouse_real_llm_node_debug_only")
        for case in data["cases"]:
            with self.subTest(case=case["case_id"]):
                self.assertNotIn("fixture_rows", case)
                self.assertTrue(case.get("real_sql") or case.get("source_case_id"))
                self.assertTrue(case["question"])
                self.assertTrue(case["review_focus"])
                self.assertTrue(case["required_accepted_capabilities"])
                self.assertTrue(case["allowed_final_statuses"])
```

- [ ] **Step 2: Run the failing test**

Run: `python3 -m unittest tests.phase6.test_live_question_family_cases`

Expected: FAIL because the Phase 6 manifest does not exist.

- [ ] **Step 3: Create the manifest**

Create `evals/phase6/live_question_family_cases.yaml` with:

```yaml
version: "0.1"
source_case_files:
  - evals/phase4/full_period_pattern_cases.yaml
  - evals/phase5/live_node_system_cases.yaml
artifact_policy: real_clickhouse_real_llm_node_debug_only
cases:
  - case_id: phase6_pattern_month_start_negative
    source_case_id: full_month_start_vs_mid_end
    primary_question_family: pattern_explanation
    secondary_question_families: []
    question: "全量样本下，月初1-10号付费金额是否持续高于月中和月末？"
    review_focus: "数据充分但假设不被支持时，输出负向业务答案。"
    required_accepted_capabilities: [compare_period_phases]
    allowed_final_statuses: [passed, degraded]

  - case_id: phase6_pattern_channel_monthly
    source_case_id: full_wajespecial_vs_other_by_month
    primary_question_family: pattern_explanation
    secondary_question_families: []
    question: "WajeSpecial 渠道的月度日均付费金额是否稳定高于其他渠道合计？"
    review_focus: "渠道月度对比保持可解释降级，不把 route drift 固化成硬规则。"
    required_accepted_capabilities: [compare_periods]
    allowed_final_statuses: [passed, degraded]

  - case_id: phase6_paid_amount_q2_driver
    source_case_id: driver_q2_vs_q1_paid_users
    primary_question_family: paid_amount_change_explanation
    secondary_question_families: []
    question: "2026年Q2相比Q1付费金额提升，主要是付费用户数增加还是单付费用户金额提升带来的？"
    review_focus: "驱动拆解给出贡献型答案，不能写成因果定论。"
    required_accepted_capabilities: [driver_decomposition]
    allowed_final_statuses: [passed]

  - case_id: phase6_paid_amount_channel_mix
    primary_question_family: paid_amount_change_explanation
    secondary_question_families: [segment_or_factor_attribution]
    question: "2026年Q2相比Q1付费金额变化里，不同渠道分别贡献了多少？"
    review_focus: "LLM 选择渠道贡献和驱动拆解，Answer Package 保留各自证据 ref。"
    required_accepted_capabilities: [segment_contribution]
    allowed_final_statuses: [passed, degraded]
    pattern_family: custom_baseline
    time_window: "2026-01-01..2026-06-30"
    scope: all_users
    baseline: {label: "2026年Q1", start: "2026-01-01", end: "2026-04-01"}
    target: {label: "2026年Q2", start: "2026-04-01", end: "2026-07-01"}
    pattern_params:
      period_key: channel
      group_key: group
      target_group: target
      baseline_group: baseline
    required_fields: [channel, group, amount]
    real_sql: |
      SELECT
        channel,
        if(business_date_lagos < toDate('2026-04-01'), 'baseline', 'target') AS `group`,
        sum(paid_amount_ngn) AS amount
      FROM paid_order_success_clean_20240101_20260704
      WHERE business_date_lagos >= toDate('2026-01-01')
        AND business_date_lagos < toDate('2026-07-01')
      GROUP BY channel, `group`
      ORDER BY channel, `group`
```

Add the remaining 8 case entries from the Phase 6 test scope table in the same shape. Each new entry must contain `primary_question_family`, optional `secondary_question_families`, `question`, `review_focus`, `required_accepted_capabilities`, `allowed_final_statuses`, and either `source_case_id` or `real_sql`.

- [ ] **Step 4: Run the test**

Run: `python3 -m unittest tests.phase6.test_live_question_family_cases`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add evals/phase6/live_question_family_cases.yaml tests/phase6/test_live_question_family_cases.py
git commit -m "Add Phase 6 live question family cases"
```

## Task 2: Multi-Intent Binding And Clarification

**Files:**
- Modify: `bi_agent/runtime/llm_prompts.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Test: `tests/phase4/test_llm_workflow.py`

**Interfaces:**
- Consumes: existing intent output from `understand_business_intent`.
- Produces: intent payload fields `question_families: list[str]`, `primary_question_family: str`, `secondary_question_families: list[str]`, and existing `question_family` as the primary family for backward compatibility.

- [ ] **Step 1: Write failing tests**

Add tests that use fake LLM outputs:

```python
def test_intent_binding_preserves_composite_question_families(self):
    result = run_workflow_with_fake_llm(
        question="Q2为什么增长，主要渠道和用户数客单价分别怎么贡献？",
        llm_outputs={
            "business_intent": {
                "question_family": "paid_amount_change_explanation",
                "question_families": [
                    "paid_amount_change_explanation",
                    "segment_or_factor_attribution",
                ],
                "target_metric": "paid_amount",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
                "pattern_family": "custom_baseline",
                "target_claim": "formula_component_contribution",
            },
            "boundary_decision": {"boundary_status": "clear", "decision_summary": "问题边界清楚。"},
        },
    )
    self.assertEqual(result.state["intent"]["question_family"], "paid_amount_change_explanation")
    self.assertEqual(
        result.state["intent"]["secondary_question_families"],
        ["segment_or_factor_attribution"],
    )


def test_boundary_decision_asks_when_composite_scope_changes_answer_quality(self):
    result = run_workflow_with_fake_llm(
        question="WajeSpecial 最近是不是比其他渠道好，也帮我看主要原因？",
        llm_outputs={
            "business_intent": {
                "question_family": "paid_amount_change_explanation",
                "question_families": [
                    "paid_amount_change_explanation",
                    "segment_or_factor_attribution",
                ],
                "target_metric": "paid_amount",
                "scope": "WajeSpecial_vs_other_channels",
                "time_window": "2026-01-01..2026-06-30",
                "pattern_family": "custom_baseline",
                "target_claim": "comparative_change",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "decision_summary": "需要确认按总额、日均还是贡献拆解优先。",
                "recommended_assumption": "先按日均付费金额比较，再拆主要贡献项。",
                "options": [
                    {"label": "按推荐继续", "business_impact": "先回答可比性最高的问题。"},
                    {"label": "只看总额", "business_impact": "总额可能受天数和样本量影响。"},
                ],
            },
        },
    )
    self.assertEqual(result.state["boundary_decision"]["boundary_status"], "needs_question")
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m unittest tests.phase4.test_llm_workflow`

Expected: FAIL until workflow normalizes the new list fields.

- [ ] **Step 3: Normalize intent fields**

Add a small helper in `bi_agent/runtime/langgraph_workflow.py`:

```python
def _normalize_question_families(intent: dict[str, Any]) -> dict[str, Any]:
    primary = str(intent.get("primary_question_family") or intent.get("question_family") or "pattern_explanation")
    families = [str(item) for item in intent.get("question_families", ()) if item]
    if primary not in families:
        families.insert(0, primary)
    secondary = [item for item in families if item != primary]
    return {
        **intent,
        "question_family": primary,
        "primary_question_family": primary,
        "question_families": families,
        "secondary_question_families": secondary,
    }
```

Call it immediately after `business_intent` LLM output is parsed.

- [ ] **Step 4: Update prompts**

In `bi_agent/runtime/llm_prompts.py`, make `business_intent` ask for:

```text
如果一个问题同时包含变化解释、分群归因、异常排查、模式判断或数据可信度判断，请输出 question_families。primary_question_family 是当前最影响最终回答的问题族；secondary_question_families 保留需要一起执行或在答案中说明的旁路。
```

Do not accept caller-provided `question_family_hint` as a business input.

- [ ] **Step 5: Run tests**

Run: `python3 -m unittest tests.phase4.test_llm_workflow`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add bi_agent/runtime/llm_prompts.py bi_agent/runtime/langgraph_workflow.py tests/phase4/test_llm_workflow.py
git commit -m "Support composite question family intent"
```

## Task 3: Family Recipe Enablement Gate

**Files:**
- Modify: `bi_agent/runtime/recipe_registry.py`
- Modify: `bi_agent/runtime/compiler.py`
- Test: `tests/phase4/test_recipe_registry_and_compiler.py`

**Interfaces:**
- Consumes: `RecipeEntry.default_degraded`.
- Produces: recipe entries that only stop being degraded after the family has a live Phase 6 case and executable accepted capabilities.

- [ ] **Step 1: Add compiler tests**

```python
def test_paid_amount_change_recipe_can_compile_when_required_capabilities_exist(self):
    compiled = compile_recipe(
        recipe_id="paid_amount_change_explanation",
        requested_nodes=("data_quality_check", "driver_decomposition", "answer_verify"),
        question_family="paid_amount_change_explanation",
    )
    self.assertIn("driver_decomposition", compiled.mutations.accepted_graph)
    self.assertNotIn("paid_amount_change_explanation", compiled.mutations.degraded_paths)


def test_business_object_recipe_stays_degraded_without_event_or_comparison_capability(self):
    compiled = compile_recipe(
        recipe_id="business_object_impact_review",
        requested_nodes=("answer_verify",),
        question_family="business_object_impact_review",
    )
    self.assertIn("business_object_impact_review", compiled.mutations.degraded_paths)
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m unittest tests.phase4.test_recipe_registry_and_compiler`

Expected: FAIL until compiler supports per-family enablement.

- [ ] **Step 3: Implement the smallest enablement check**

Use an allow-list in `compiler.py`:

```python
PHASE6_ENABLED_FAMILY_REQUIREMENTS = {
    "paid_amount_change_explanation": {"driver_decomposition", "answer_verify"},
    "segment_or_factor_attribution": {"segment_contribution", "answer_verify"},
    "anomaly_or_black_swan_review": {"outlier_contribution", "answer_verify"},
    "data_quality_or_evidence_review": {"data_quality_check", "answer_verify"},
}
```

When a recipe is `default_degraded`, clear degradation only if accepted nodes include that family requirement. Add families to this map only after their live case passes.

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.phase4.test_recipe_registry_and_compiler`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bi_agent/runtime/recipe_registry.py bi_agent/runtime/compiler.py tests/phase4/test_recipe_registry_and_compiler.py
git commit -m "Gate Phase 6 recipe enablement by executable capabilities"
```

## Task 4: Capability Evidence For Non-Pattern Families

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `bi_agent/capabilities/driver_decomposition.py`
- Modify: `bi_agent/capabilities/segment_contribution.py`
- Modify: `bi_agent/capabilities/outlier_contribution.py`
- Test: `tests/phase4/test_llm_workflow.py`

**Interfaces:**
- Consumes: existing evidence dictionaries from `_execute_capabilities`.
- Produces: evidence envelopes with `evidence_ref`, `capability`, `capability_id`, `typed_payload`, `numeric_facts`, `strength`, `wording_limit`, and `limitations`.

- [ ] **Step 1: Add envelope tests**

```python
def test_segment_contribution_evidence_has_claim_ready_fields(self):
    result = run_workflow_with_fake_llm(
        question="Q2相比Q1，哪些渠道贡献最大？",
        requested_nodes=("segment_contribution", "answer_verify"),
        rows=[
            {"period": "WajeSpecial", "group": "baseline", "amount": 100},
            {"period": "WajeSpecial", "group": "target", "amount": 160},
            {"period": "Organic", "group": "baseline", "amount": 100},
            {"period": "Organic", "group": "target", "amount": 90},
        ],
    )
    evidence = result.answer_package["evidence"]
    segment = next(item for item in evidence if item["capability"] == "segment_contribution")
    self.assertIn("evidence_ref", segment)
    self.assertIn("typed_payload", segment)
    self.assertIn("numeric_facts", segment)
    self.assertIn(segment["wording_limit"], {"supported", "tendency", "insufficient"})
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m unittest tests.phase4.test_llm_workflow`

Expected: FAIL if any non-pattern evidence lacks claim-ready fields.

- [ ] **Step 3: Patch the shared evidence normalization**

In `_evidence_dict(...)`, preserve existing fields and fill missing fields from `typed_payload`:

```python
def _evidence_dict(result: Any, state: WorkflowState) -> dict[str, Any]:
    item = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    payload = dict(item.get("typed_payload") or {})
    item.setdefault("capability_id", item.get("capability"))
    item.setdefault("capability", item.get("capability_id"))
    item.setdefault("numeric_facts", {k: v for k, v in payload.items() if isinstance(v, (int, float))})
    item.setdefault("result_refs", (state.get("sql_hash"),))
    item.setdefault("sql_hashes", item.get("result_refs", ()))
    return item
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.phase4.test_llm_workflow`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bi_agent/runtime/langgraph_workflow.py bi_agent/capabilities/driver_decomposition.py bi_agent/capabilities/segment_contribution.py bi_agent/capabilities/outlier_contribution.py tests/phase4/test_llm_workflow.py
git commit -m "Normalize Phase 6 capability evidence"
```

## Task 5: Composite Answer Package And Final Summary

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `bi_agent/runtime/llm_prompts.py`
- Test: `tests/phase4/test_workflow_artifacts_answer.py`

**Interfaces:**
- Consumes: `intent.question_families`, `evidence_brief`, `causal_audit`, `semantic_audit`, `verifier`, and `answer_package`.
- Produces: final answer text that explains business understanding, analysis path, evidence findings, conclusion, and watchouts without dumping raw JSON fields.

- [ ] **Step 1: Add final summary test**

```python
def test_composite_final_summary_mentions_each_business_thread_without_raw_labels(self):
    package = build_answer_package_for_state(
        intent={
            "question_family": "paid_amount_change_explanation",
            "question_families": ["paid_amount_change_explanation", "segment_or_factor_attribution"],
        },
        evidence=[
            {"evidence_ref": "driver_decomposition:x", "capability": "driver_decomposition", "strength": "high"},
            {"evidence_ref": "segment_contribution:x", "capability": "segment_contribution", "strength": "medium"},
        ],
        final_business_summary="我先判断整体变化，再拆渠道贡献。结论是增长主要由单付费用户金额和 WajeSpecial 贡献共同解释。需要注意这仍是贡献拆解，不代表因果定论。",
    )
    text = package["sections"][0]["payload"]["final_business_summary"]
    self.assertIn("整体变化", text)
    self.assertIn("渠道贡献", text)
    self.assertNotIn("accepted_graph", text)
    self.assertNotIn("capability_id", text)
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m unittest tests.phase4.test_workflow_artifacts_answer`

Expected: FAIL until final summary carries composite context.

- [ ] **Step 3: Update final-summary prompt**

Require the LLM to write:

```text
请用业务读者能直接理解的中文总结。本次回答必须包含：我如何理解用户问题；我把问题拆成哪些业务判断；每条判断使用了什么证据；哪些结论被支持、哪些没有被支持；最终答案；还需要观察什么。不要输出内部字段名、JSON key、节点 id 或 prompt 术语。
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.phase4.test_workflow_artifacts_answer`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bi_agent/runtime/langgraph_workflow.py bi_agent/runtime/llm_prompts.py tests/phase4/test_workflow_artifacts_answer.py
git commit -m "Improve composite final business summaries"
```

## Task 6: Workbench Audit For Phase 6 Runs

**Files:**
- Modify: `app/api/replays/route.ts`
- Modify: `app/agent-run-workbench/contracts.ts`
- Modify: `app/agent-run-workbench/AgentRunWorkbench.tsx`
- Modify: `app/agent-run-workbench/WorkflowCanvasModal.tsx`

**Interfaces:**
- Consumes: Phase 6 Answer Package artifacts.
- Produces: the existing Agent Run Workbench view with composite families shown as business threads and accepted graph still shown as a side branch.

- [ ] **Step 1: Add a static contract check**

Add a small TypeScript helper test or build-time assertion near the adapter:

```ts
const familyLabel: Record<string, string> = {
  pattern_explanation: "模式判断",
  paid_amount_change_explanation: "付费金额变化解释",
  business_object_impact_review: "业务对象影响评估",
  segment_or_factor_attribution: "分群或因素归因",
  revenue_health_review: "收入健康评估",
  anomaly_or_black_swan_review: "异常或突发因素排查",
  custom_baseline_comparison: "自定义基线对比",
  data_quality_or_evidence_review: "数据质量或证据评估",
};
```

- [ ] **Step 2: Route Phase 6 artifacts through the existing adapter**

Change the API artifact root selection so `/api/agent-runs` can read the latest available root in this order:

```ts
const artifactRoots = [
  "artifacts/phase-6/live-question-family/latest",
  "artifacts/phase-5/live-node-system/20260707-v31-prompt-audit-r2",
];
```

Keep the first screen free of raw LLM logs; keep logs under collapsed node detail.

- [ ] **Step 3: Run frontend build**

Run: `npm run build`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/api/replays/route.ts app/agent-run-workbench/contracts.ts app/agent-run-workbench/AgentRunWorkbench.tsx app/agent-run-workbench/WorkflowCanvasModal.tsx
git commit -m "Show Phase 6 composite runs in workbench"
```

## Task 7: Live Node-By-Node Phase 6 Review

**Files:**
- Create: `docs/reviews/phase6-live-node-audit-YYYYMMDD.md`
- Use: `tools/phase5/run_live_node_system_test.py`
- Use: `evals/phase6/live_question_family_cases.yaml`

**Interfaces:**
- Consumes: Phase 6 live manifest and current workflow.
- Produces: a business-language audit report with node-level findings, blocking issues, and next fixes.

- [ ] **Step 1: Run one case first**

Run:

```bash
python3 tools/phase5/run_live_node_system_test.py \
  --case-file evals/phase6/live_question_family_cases.yaml \
  --artifact-root artifacts/phase-6/live-question-family/$(date +%Y%m%d)-r1 \
  --case phase6_paid_amount_q2_driver \
  --fail-fast
```

Expected: PASS before wider live runs continue.

- [ ] **Step 2: Run three-family smoke**

Run:

```bash
python3 tools/phase5/run_live_node_system_test.py \
  --case-file evals/phase6/live_question_family_cases.yaml \
  --artifact-root artifacts/phase-6/live-question-family/$(date +%Y%m%d)-r1 \
  --case phase6_paid_amount_q2_driver \
  --case phase6_segment_channel_attribution \
  --case phase6_anomaly_single_period \
  --fail-fast
```

Expected: PASS or degraded with clear evidence boundary. Blocked requires a documented schema, contract, permission, or LLM-output parsing reason.

- [ ] **Step 3: Run full Phase 6 live set**

Run:

```bash
python3 tools/phase5/run_live_node_system_test.py \
  --case-file evals/phase6/live_question_family_cases.yaml \
  --artifact-root artifacts/phase-6/live-question-family/$(date +%Y%m%d)-r1 \
  --fail-fast
```

Expected: every case reaches `persist_artifact`; unsupported hypotheses may end as degraded, but no case may silently publish an unsupported strong claim.

- [ ] **Step 4: Write the review report**

Create `docs/reviews/phase6-live-node-audit-YYYYMMDD.md` with these sections:

```markdown
# Phase 6 Live Node Audit

## 现状是什么

## 每个 case 的业务问题、路径和结果

## 哪些回答可以信

## 哪些问题需要问用户

## 哪些能力还只是弱证据

## 代码层应该怎么改

## Phase 6 是否可以 close
```

- [ ] **Step 5: Commit**

```bash
git add docs/reviews/phase6-live-node-audit-YYYYMMDD.md
git commit -m "Add Phase 6 live node audit"
```

## Acceptance

- At least one live case reaches `persist_artifact` for every launch question family.
- Composite intent is represented explicitly in intent state and final answer context.
- Ask-question triggers for latent ambiguity that can change answer quality.
- Accepted graph contains only executable or explicitly degraded paths; unsupported paths stay visible.
- Final answers can state supported, unsupported, and degraded conclusions in business Chinese.
- Agent Run Workbench can replay Phase 6 artifacts without exposing technical labels on the first screen.

## Validation Commands

Run before Phase 6 closeout:

```bash
python3 -m unittest discover -s tests/phase4
python3 -m unittest discover -s tests/phase5
python3 -m unittest discover -s tests/phase6
npm run build
git diff --check
```

Run for live acceptance:

```bash
python3 tools/phase5/run_live_node_system_test.py \
  --case-file evals/phase6/live_question_family_cases.yaml \
  --artifact-root artifacts/phase-6/live-question-family/$(date +%Y%m%d)-final \
  --fail-fast
```

## Self-Review

- Spec coverage: The plan covers all eight launch question families, composite intent, ask-question, evidence boundaries, final answer quality, workbench replay, and live node-by-node eval.
- Placeholder scan: The plan avoids unresolved placeholder tokens and unspecified implementation steps. The live audit filename uses `YYYYMMDD` as the execution date token.
- Type consistency: `primary_question_family`, `secondary_question_families`, `question_families`, `required_accepted_capabilities`, and `allowed_final_statuses` are used consistently across manifest, tests, workflow, and adapter tasks.
