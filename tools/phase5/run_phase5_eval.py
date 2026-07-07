from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Mapping

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
            "artifact_root": case.get(
                "artifact_root", str(ROOT / "artifacts" / "phase-5")
            ),
        }
    )
    package = result.answer_package or {}
    actual = _boundary_status(package)
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


def _boundary_status(package: Mapping[str, Any]) -> Any:
    audit = package.get("admin_audit", {})
    outcome = audit.get("clarification_outcome", {})
    if outcome.get("boundary_status"):
        return outcome["boundary_status"]
    for call in audit.get("llm_calls", ()):
        if call.get("task") == "boundary_decision":
            return call.get("output", {}).get("boundary_status")
    return None
