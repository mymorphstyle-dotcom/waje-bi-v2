# Phase 6 Live Question-Family Audit

Date: 2026-07-08

## Scope

Phase 6 expands the agent workflow from the pattern slice to representative launch question families. Acceptance used real ClickHouse aggregate rows and real DeepSeek `deepseek-v4-flash` LLM calls through the OpenAI-compatible adapter. Fixture tests only cover local behavior.

Latest passing run:

- Artifact root: `artifacts/phase-6/live-question-family/20260708-r9/`
- Summary: `artifacts/phase-6/live-question-family/20260708-r9/live_node_system_summary.json`
- Result: 12 / 12 cases passed
- Workflow volume: 231 completed nodes, 136 LLM calls

## Coverage

| Case | Family | Status | Accepted graph highlights |
|---|---|---:|---|
| `phase6_pattern_month_start_negative` | pattern explanation | passed | `compare_period_phases`, `pattern_scan`, `formula_decompose`, `event_evidence`, `segment_bridge`, `outlier_scan` |
| `phase6_pattern_channel_monthly` | pattern explanation | passed | `compare_periods`, degraded answer when comparable periods were insufficient |
| `phase6_paid_amount_q2_driver` | paid amount change | passed | `driver_decomposition`, `event_evidence` |
| `phase6_paid_amount_channel_mix` | paid amount + segment | passed | `segment_contribution` |
| `phase6_business_object_campaign` | business object impact | passed | `compare_periods`, `event_evidence` |
| `phase6_segment_channel_attribution` | segment attribution | passed | `segment_contribution` |
| `phase6_segment_joint_candidate` | segment / joint candidate | passed | `segment_contribution` |
| `phase6_revenue_health_h1` | revenue health | passed | `data_quality_profile`, `driver_decomposition`, degraded answer |
| `phase6_anomaly_single_period` | anomaly review | passed | `outlier_scan`, `outlier_contribution` |
| `phase6_custom_baseline_release` | custom baseline | passed | `compare_periods`, `driver_decomposition`, `segment_contribution`, `event_evidence` |
| `phase6_data_quality_trust` | data quality / evidence | passed | `data_quality_profile`, degraded answer |
| `phase6_composite_change_and_reason` | composite change + reason | passed | `compare_periods`, `driver_decomposition`, `segment_contribution`, `event_evidence` |

## Issues Found And Fixed

1. `next_action` over-asked when evidence had already run.
   - Symptom: `revenue_health_h1` became blocked because missing driver components were treated as a user question.
   - Fix: post-evidence terminal limitations now route to bounded answer or degraded answer, not ask-question, unless a real unresolved business boundary remains.

2. Hard verifier treated normal business rounding as numeric mismatch.
   - Symptom: 65.4% / 34.6% contribution shares were degraded after being rounded in claims.
   - Fix: numeric verification now accepts small rounding tolerance while still rejecting material drift.

3. Capability execution was not null-safe for real channel rows.
   - Symptom: driver decomposition crashed when a grouping key contained `null`.
   - Fix: driver grouping skips empty period keys and normalizes group keys to strings.

4. Route normalization missed segment capability when LLM classified the question as custom baseline.
   - Symptom: questions like “哪些渠道解释变化” sometimes requested baseline comparison without `segment_contribution`.
   - Fix: original question text is preserved in intent, segment wording can add `segment_contribution`, and requested capabilities infer secondary question families before compiler validation.

5. Revenue-health clarification was too conservative.
   - Symptom: “健康度如何，风险在哪里” sometimes blocked on baseline preference.
   - Fix: revenue-health baseline or segmentation preference can proceed as a low-risk default when metric, scope, and time window are already bound.

## Quality Review

Current state is ready to close Phase 6 as an engineering milestone:

- All eight launch question families have at least one real end-to-end representative case.
- Composite questions run through accepted multi-capability graphs.
- `answer_verify` and evidence refs are present on representative answer paths.
- Negative evidence produces bounded negative business answers instead of “无法回答”.
- Ask-question remains available, but low-risk preference gaps can continue with explicit assumptions.

Residual polish should stay out of Phase 6 closeout:

- Some degraded final summaries are still terse, especially revenue-health and data-quality trust.
- A few LLM summaries repeat the negative conclusion sentence. This is content polish for Phase 7 answer UX, not a Phase 6 graph legality blocker.
- Live LLM latency is variable; one node took 161 seconds. Production timeout, retry, and cost policy belong to Phase 8.

## Verification

Primary live command:

```bash
python3 tools/phase5/run_live_node_system_test.py \
  --case-file evals/phase6/live_question_family_cases.yaml \
  --artifact-root artifacts/phase-6/live-question-family/20260708-r9 \
  --fail-fast
```

Local regression commands are recorded in the final closeout response.
