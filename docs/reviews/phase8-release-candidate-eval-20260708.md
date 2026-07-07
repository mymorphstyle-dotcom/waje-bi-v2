# Phase 8 Release Candidate Eval

Date: 2026-07-08

Command:

```bash
ruby tools/evals/validate-launch-evals.rb
```

Result:

```text
Launch eval validation passed.
Expectation packages: 8
Question families: anomaly_or_black_swan_review, business_object_impact_review, custom_baseline_comparison, data_quality_or_evidence_review, paid_amount_change_explanation, pattern_explanation, revenue_health_review, segment_or_factor_attribution
Compiler outcomes: accept, auto_repair, block, degrade, targeted_repair
Source pools: historical_failure_cases, matrix_generated_boundary_cases, real_user_questions
```

Scope:

- Full launch expectation-package validation.
- Covers all launch question families.
- Covers accepted, repaired, degraded, and blocked compiler outcomes.
- Covers real user questions, historical failure cases, and matrix-generated boundary cases.
