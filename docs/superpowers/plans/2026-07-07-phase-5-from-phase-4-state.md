# Phase 5 From Phase 4 State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current Phase 4 pattern workflow into an auditable Answer Package, verifier, and eval gate before expanding to more question families.

**Architecture:** Phase 5 hardens the existing Python BI Agent Core path. It does not add broad new analytical capabilities. LangGraph still runs the visible workflow; WAJE-owned contracts, evidence envelopes, answer package, verifier, and eval harness decide claim validity and acceptance.

**Tech Stack:** Python BI Agent Core, LangGraph, ClickHouse runtime, YAML eval packages, existing unittest suite, existing local artifact format.

## Global Constraints

- Base all Phase 5 work on the Phase 4 live retest state in `docs/reviews/phase4-ten-case-node-audit-20260707.md`.
- Do not add deterministic route compiler rules for `compare_periods` / `rolling_window_compare` drift until Phase 5 measures impact across open tests.
- Add implicit clarification tests for latent ambiguity; do not limit ask-question tests to obvious missing-field prompts.
- Eval failures do not become runtime guardrails automatically; promotion requires human validation, dual ownership, and a generalizable pattern.
- User-visible text must stay in Simplified Chinese business language; internal ids and machine enums stay in audit material.
- Do not expand all eight question families in Phase 5. That starts in Phase 6 after Phase 5 gates are reliable.

---

## Current Phase 4 Entry State

Roadmap source: `docs/implementation-roadmap.md` section `Post-Phase 4 Rebaseline`.

Phase 4 has a runnable generalized pattern slice:

- 10 live full-period cases retested: 3 passed, 7 degraded, 0 blocked, 0 failed.
- Degraded summaries now include primary evidence numbers.
- `compare_periods`, `compare_period_phases`, `weekday_calendar_compare`, `rolling_window_compare`, and `event_window_compare` can all count as primary pattern evidence.
- Coverage LLM cannot block without local data/validator evidence.
- Remaining issue: route choice can drift while evidence numbers remain correct.

Phase 5 should therefore focus on answer safety, eval attribution, implicit clarification, and measured route drift.

## Files

- Modify: `bi_agent/runtime/answer_package.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `tools/phase4/validate_phase4.py`
- Create: `evals/phase5/implicit_clarification_cases.yaml`
- Create: `tools/phase5/run_phase5_eval.py`
- Create: `tests/phase5/test_answer_package_claim_groups.py`
- Create: `tests/phase5/test_implicit_clarification_eval.py`
- Create: `tests/phase5/test_phase5_eval_harness.py`
- Modify: `docs/implementation-roadmap.md`
- Modify: `docs/reviews/phase4-ten-case-node-audit-20260707.md` only if Phase 5 retest changes the baseline facts.

## Task 1: Phase 5 Claim Group Contract

**Files:**
- Modify: `bi_agent/runtime/answer_package.py`
- Test: `tests/phase5/test_answer_package_claim_groups.py`

**Interfaces:**
- Consumes: existing `build_answer_package(...)`, `verify_answer_package(...)`, evidence envelopes, and draft claims.
- Produces: package summary payload field `claim_groups: list[dict[str, Any]]`.

- [ ] **Step 1: Write failing test for claim group shape**

```python
from bi_agent.runtime.answer_package import build_answer_package


def test_answer_package_emits_claim_groups_with_evidence_boundary():
    package = build_answer_package(
        run_id="phase5-claim-group",
        draft_claims=[
            {
                "text": "Q2 相比 Q1 日均付费金额提升 15.0%。",
                "evidence_refs": ["compare_periods:run"],
                "numbers": {"median_uplift": 0.1504749251624582},
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
                "target_metric": "paid_amount",
            }
        ],
        evidence=[
            {
                "evidence_ref": "compare_periods:run",
                "capability_id": "compare_periods",
                "evidence_type": "statistical_association",
                "strength": "high",
                "wording_limit": "supported",
                "limitations": [],
                "typed_payload": {
                    "median_uplift": 0.1504749251624582,
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                },
            }
        ],
        checkpoint_events=[],
        proposed_graph=[],
        accepted_graph=["compare_periods", "answer_verify"],
        rejected_or_degraded_mutations=[],
        validator_results=[],
        sql_text="SELECT 1",
        sql_hash="hash",
        artifact_audit={},
    )

    claim_groups = package["sections"][0]["payload"]["claim_groups"]
    assert claim_groups == [
        {
            "text": "Q2 相比 Q1 日均付费金额提升 15.0%。",
            "scope": "full_sample",
            "baseline": {"label": "Q1"},
            "target": {"label": "Q2"},
            "target_metric": "paid_amount",
            "time_window": "2026-01-01..2026-06-30",
            "evidence_refs": ["compare_periods:run"],
            "evidence_type": "statistical_association",
            "strength": "high",
            "wording_limit": "supported",
            "limitations": [],
            "verifier_status": "passed",
        }
    ]
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m unittest tests.phase5.test_answer_package_claim_groups`

Expected: FAIL because `claim_groups` is absent.

- [ ] **Step 3: Add minimal claim group builder**

Add a private helper in `bi_agent/runtime/answer_package.py`:

```python
def build_claim_groups(
    *,
    draft_claims: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    verifier: Mapping[str, Any],
) -> list[dict[str, Any]]:
    evidence_by_ref = {item.get("evidence_ref"): item for item in evidence}
    groups = []
    for claim in draft_claims:
        refs = list(claim.get("evidence_refs", ()))
        ref_items = [evidence_by_ref[ref] for ref in refs if ref in evidence_by_ref]
        limitations = []
        for item in ref_items:
            for limitation in item.get("limitations", ()):
                if limitation not in limitations:
                    limitations.append(limitation)
        first = ref_items[0] if ref_items else {}
        groups.append(
            {
                "text": claim.get("text", ""),
                "scope": claim.get("scope"),
                "baseline": claim.get("baseline", {}),
                "target": claim.get("target", {}),
                "target_metric": claim.get("target_metric"),
                "time_window": claim.get("time_window"),
                "evidence_refs": refs,
                "evidence_type": first.get("evidence_type"),
                "strength": first.get("strength"),
                "wording_limit": first.get("wording_limit"),
                "limitations": limitations,
                "verifier_status": verifier.get("status"),
            }
        )
    return groups
```

In `build_answer_package(...)`, after `verifier = verify_answer_package(...)`, set:

```python
claim_groups = build_claim_groups(
    draft_claims=draft_claims,
    evidence=evidence,
    verifier=verifier,
)
```

Then add `"claim_groups": claim_groups` to the summary payload.

- [ ] **Step 4: Run test**

Run: `python3 -m unittest tests.phase5.test_answer_package_claim_groups`

Expected: PASS.

## Task 2: Verifier Blocks Strong Claims With Failed Evidence

**Files:**
- Modify: `bi_agent/runtime/answer_package.py`
- Test: `tests/phase5/test_answer_package_claim_groups.py`

**Interfaces:**
- Consumes: `verify_answer_package(...)`.
- Produces: verifier error `strong_claim_with_failed_verifier`.

- [ ] **Step 1: Write failing test**

```python
from bi_agent.runtime.answer_package import verify_answer_package


def test_strong_claim_fails_when_evidence_ref_is_missing():
    verifier = verify_answer_package(
        draft_claims=[
            {
                "text": "该模式稳定成立。",
                "claim_strength": "strong",
                "evidence_refs": ["missing:evidence"],
                "numbers": {"median_uplift": 0.2},
            }
        ],
        evidence=[],
        visible_limitations=[],
    )

    assert verifier["status"] == "failed"
    assert any(error["code"] == "missing_evidence_ref" for error in verifier["errors"])
```

- [ ] **Step 2: Run test**

Run: `python3 -m unittest tests.phase5.test_answer_package_claim_groups`

Expected: FAIL only if strong claim status remains publishable.

- [ ] **Step 3: Keep the smallest implementation**

If the current verifier already fails because of missing evidence, do not add new code. Add only the regression test. If a strong claim can still pass with weak evidence, add this check inside the claim loop:

```python
if claim.get("claim_strength") == "strong":
    if any(evidence_by_ref[ref].get("wording_limit") not in {"supported", "stable_pattern"} for ref in valid_refs):
        errors.append({"code": "strong_claim_without_supported_wording", "claim_index": index})
```

- [ ] **Step 4: Run test**

Run: `python3 -m unittest tests.phase5.test_answer_package_claim_groups`

Expected: PASS.

## Task 3: Implicit Clarification Eval Suite

**Files:**
- Create: `evals/phase5/implicit_clarification_cases.yaml`
- Test: `tests/phase5/test_implicit_clarification_eval.py`

**Interfaces:**
- Consumes: existing `run_pattern_workflow(...)` and `FakeLLMClient`.
- Produces: a YAML suite for latent ambiguity that can change conclusion quality.

- [ ] **Step 1: Create eval case file**

Add `evals/phase5/implicit_clarification_cases.yaml`:

```yaml
version: "0.1"
cases:
  - case_id: channel_total_vs_daily_average
    question: "WajeSpecial 最近几个月是不是比其他渠道好？"
    latent_choice: total_amount_vs_daily_average
    expected_boundary_status: needs_question
    acceptable_recommended_assumption: daily_average_paid_amount

  - case_id: combined_vs_per_segment_baseline
    question: "WajeSpecial 比其他渠道是不是稳定更高？"
    latent_choice: other_channels_combined_vs_each_channel
    expected_boundary_status: needs_question
    acceptable_recommended_assumption: other_channels_combined

  - case_id: strict_every_period_vs_majority_tendency
    question: "Q2 是不是一直比 Q1 好？"
    latent_choice: every_period_strict_vs_majority_tendency
    expected_boundary_status: needs_question
    acceptable_recommended_assumption: every_period_strict

  - case_id: calendar_vs_business_event_window
    question: "活动后付费有没有变好？"
    latent_choice: calendar_window_vs_event_window
    expected_boundary_status: needs_question
    acceptable_recommended_assumption: ask_for_event_window
```

- [ ] **Step 2: Write test that loads the suite**

```python
from pathlib import Path
import yaml


def test_implicit_clarification_cases_are_not_obvious_missing_fields():
    data = yaml.safe_load(Path("evals/phase5/implicit_clarification_cases.yaml").read_text(encoding="utf-8"))
    case_ids = {case["case_id"] for case in data["cases"]}
    assert case_ids == {
        "channel_total_vs_daily_average",
        "combined_vs_per_segment_baseline",
        "strict_every_period_vs_majority_tendency",
        "calendar_vs_business_event_window",
    }
    assert all(case["expected_boundary_status"] == "needs_question" for case in data["cases"])
    assert all("latent_choice" in case for case in data["cases"])
```

- [ ] **Step 3: Run test**

Run: `python3 -m unittest tests.phase5.test_implicit_clarification_eval`

Expected: PASS after file exists.

## Task 4: Boundary LLM Eval Harness For Latent Clarification

**Files:**
- Create: `tools/phase5/run_phase5_eval.py`
- Test: `tests/phase5/test_phase5_eval_harness.py`

**Interfaces:**
- Consumes: `evals/phase5/implicit_clarification_cases.yaml`.
- Produces: case result fields `case_id`, `status`, `actual_boundary_status`, `expected_boundary_status`, `failure_attribution`.

- [ ] **Step 1: Write failing test for harness output**

```python
from tools.phase5.run_phase5_eval import evaluate_boundary_case


class NeedsQuestionLLM:
    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        output = {key: None for key in required_keys}
        if task == "business_intent":
            output.update(
                {
                    "question_family": "custom_baseline_comparison",
                    "target_metric": "paid_amount",
                    "pattern_family": "custom_baseline",
                    "scope": "full_sample",
                    "time_window": "current_period",
                    "target_claim": "渠道表现对比",
                    "baseline_candidates": [],
                    "status_message": "已识别为渠道对比。",
                }
            )
        if task == "boundary_decision":
            output.update(
                {
                    "boundary_status": "needs_question",
                    "recommended_assumption": {"metric": "日均付费金额"},
                    "clarification_questions": [
                        {"question": "你想看总金额还是日均金额？", "options": ["日均金额", "总金额"]}
                    ],
                    "decision_summary": "指标口径会影响结论。",
                }
            )
        return type("Response", (), {"output": output, "audit": {}})()


def test_boundary_case_reports_expected_needs_question():
    result = evaluate_boundary_case(
        {
            "case_id": "channel_total_vs_daily_average",
            "question": "WajeSpecial 最近几个月是不是比其他渠道好？",
            "expected_boundary_status": "needs_question",
        },
        llm_client=NeedsQuestionLLM(),
    )

    assert result["status"] == "passed"
    assert result["actual_boundary_status"] == "needs_question"
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m unittest tests.phase5.test_phase5_eval_harness`

Expected: FAIL because `tools.phase5.run_phase5_eval` does not exist.

- [ ] **Step 3: Add minimal harness**

Create `tools/phase5/run_phase5_eval.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.runtime.langgraph_workflow import run_pattern_workflow


def evaluate_boundary_case(case: Mapping[str, Any], *, llm_client: Any) -> dict[str, Any]:
    result = run_pattern_workflow(
        {
            "run_id": f"phase5-boundary-{case['case_id']}",
            "question": case["question"],
            "llm_client": llm_client,
            "allow_question_interrupt": True,
        }
    )
    package = result.answer_package or {}
    boundary = _llm_output(package, "boundary_decision")
    actual = boundary.get("boundary_status")
    expected = case.get("expected_boundary_status")
    return {
        "case_id": case["case_id"],
        "status": "passed" if actual == expected else "failed",
        "actual_boundary_status": actual,
        "expected_boundary_status": expected,
        "failure_attribution": ""
        if actual == expected
        else "LLM_reasoner.boundary_decision",
    }


def _llm_output(package: Mapping[str, Any], task: str) -> dict[str, Any]:
    for call in package.get("admin_audit", {}).get("llm_calls", ()):
        if call.get("task") == task:
            return dict(call.get("output", {}))
    return {}
```

- [ ] **Step 4: Run test**

Run: `python3 -m unittest tests.phase5.test_phase5_eval_harness`

Expected: PASS.

## Task 5: Route Drift Measurement, No Guardrail Promotion

**Files:**
- Modify: `tools/phase4/validate_phase4.py`
- Test: `tests/phase5/test_phase5_eval_harness.py`

**Interfaces:**
- Consumes: existing full-period summary artifacts and accepted graph.
- Produces: report field `route_drift_observed: bool` and `route_drift_impact: none | wording | evidence_shape | conclusion`.

- [ ] **Step 1: Write test for drift classification**

```python
from tools.phase4.validate_phase4 import classify_route_drift


def test_route_drift_is_observed_without_auto_guardrail():
    result = classify_route_drift(
        pattern_family="custom_baseline",
        accepted_graph=["data_quality_profile", "rolling_window_compare", "answer_verify"],
        primary_evidence_capability="rolling_window_compare",
        expected_primary_capabilities=["compare_periods"],
        eval_status="degraded",
    )

    assert result == {
        "route_drift_observed": True,
        "route_drift_impact": "evidence_shape",
        "guardrail_promotion": "requires_human_review",
    }
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m unittest tests.phase5.test_phase5_eval_harness`

Expected: FAIL because `classify_route_drift` is absent.

- [ ] **Step 3: Add minimal pure function**

Add to `tools/phase4/validate_phase4.py`:

```python
def classify_route_drift(
    *,
    pattern_family: str,
    accepted_graph: Sequence[str],
    primary_evidence_capability: str,
    expected_primary_capabilities: Sequence[str],
    eval_status: str,
) -> dict[str, str | bool]:
    observed = primary_evidence_capability not in set(expected_primary_capabilities)
    impact = "none"
    if observed:
        impact = "conclusion" if eval_status in {"failed", "blocked"} else "evidence_shape"
    return {
        "route_drift_observed": observed,
        "route_drift_impact": impact,
        "guardrail_promotion": "requires_human_review" if observed else "not_applicable",
    }
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.phase5.test_phase5_eval_harness`

Expected: PASS.

## Task 6: Phase 5 Roadmap Update

**Files:**
- Modify: `docs/implementation-roadmap.md`
- Test: documentation scan command

**Interfaces:**
- Consumes: Phase 4 audit report and this plan.
- Produces: roadmap text that says Phase 5 starts from Phase 4 state and Phase 6 owns question-family expansion.

- [ ] **Step 1: Update Phase 5 section**

Replace the Phase 5 deliverables with:

```markdown
- [ ] Claim group contract implemented and emitted in Answer Package summary.
- [ ] Answer verifier blocks unsupported strong claims and records visible limitations.
- [ ] Launch eval harness uses real user wording plus structured expectation packages.
- [ ] Failure attribution labels include business failure type and system responsibility point.
- [ ] Implicit clarification eval suite covers latent ambiguity that can change claim quality.
- [ ] Route drift measurement records observed drift and impact without auto-promoting guardrails.
```

- [ ] **Step 2: Update Phase 6 intro**

Add one sentence under Phase 6:

```markdown
Phase 6 starts only after Phase 5 eval gates can explain wrong intent, wrong baseline, weak evidence, route drift, and unsupported claims without manual log reading.
```

- [ ] **Step 3: Run doc scan**

Run:

```bash
rg -n "Implicit clarification|Route drift|Phase 6 starts only after Phase 5" docs/implementation-roadmap.md
git diff --check
```

Expected: both commands exit 0.

## Task 7: Phase 5 Validation Command

**Files:**
- Create: `tools/phase5/validate_phase5.py`
- Test: `tests/phase5/test_phase5_eval_harness.py`

**Interfaces:**
- Consumes: Phase 5 unit tests and Phase 4 full-period eval summary.
- Produces: one command for Phase 5 acceptance.

- [ ] **Step 1: Add validator**

Create `tools/phase5/validate_phase5.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    commands = [
        ("python3", "-m", "unittest", "discover", "-s", "tests/phase4"),
        ("python3", "-m", "unittest", "discover", "-s", "tests/phase5"),
        ("git", "diff", "--check"),
    ]
    failed = []
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, text=True)
        if completed.returncode:
            failed.append(command)
    if failed:
        print({"failed": failed})
        return 1
    print({"status": "passed", "commands": commands})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run validator**

Run: `python3 tools/phase5/validate_phase5.py`

Expected: PASS after Phase 5 tasks are implemented.

## Acceptance

- Phase 5 produces claim groups in Answer Package without changing ordinary business-reader visibility rules.
- Strong claims fail verification when evidence refs, numbers, scope, baseline, or wording boundaries do not support them.
- Implicit clarification cases can catch overconfident answers where a latent choice changes answer quality.
- Route drift is measured and attributed, not promoted to a hard guardrail by default.
- Phase 6 expansion does not begin until Phase 5 eval can classify wrong intent, wrong baseline, route drift, weak evidence, and unsupported claims.

## Self-Review

- Spec coverage: This plan covers Answer Package claim groups, verifier hardening, eval harness, failure attribution, implicit clarification, route drift measurement, roadmap update, and validation command.
- Placeholder scan: no TBD/TODO placeholders; Phase 6 expansion is explicitly out of Phase 5 scope.
- Type consistency: `claim_groups`, `evaluate_boundary_case`, and `classify_route_drift` are named once and reused consistently.
