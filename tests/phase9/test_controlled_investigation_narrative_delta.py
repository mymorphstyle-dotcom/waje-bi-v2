from __future__ import annotations

import json

from bi_agent.runtime.controlled_investigation_runtime import (
    ControlledInvestigationOperation,
    ControlledInvestigationProposal,
    ControlledInvestigationSettlement,
    admit_controlled_investigations,
)
from bi_agent.runtime.controlled_investigation_workflow import (
    ControlledInvestigationWorkflowResult,
)
from bi_agent.runtime.evidence_authority import canonical_digest


def _result() -> ControlledInvestigationWorkflowResult:
    operation = ControlledInvestigationOperation.create(
        owner_ref="owner-1",
        thread_ref="thread-1",
        run_attempt_id="run-1",
        intent_revision_id="intent-1",
        plan_revision_id="plan-1",
        authority_context_ref="authority-context:one",
        authority_bundle_ref="authority-bundle:one",
        parent_transition_id="transition:authority",
        source_material_projection_ref="narrative-material-projection:one",
        source_material_projection_digest="a" * 64,
    )
    proposal = ControlledInvestigationProposal.model_validate(
        {
            "investigations": [
                {
                    "investigationKey": "structure",
                    "question": "检查结构集中点。",
                    "axisRefs": ["axis:structure"],
                    "sourceRefs": ["source:structure"],
                    "expectedOutputKind": "structure_concentration",
                },
                {
                    "investigationKey": "alternative",
                    "question": "检查竞争解释。",
                    "axisRefs": ["axis:context"],
                    "sourceRefs": ["source:context"],
                    "expectedOutputKind": "alternative_explanation",
                },
            ]
        }
    )
    admission = admit_controlled_investigations(
        operation=operation,
        proposal=proposal,
        accepted_axis_refs=("axis:structure", "axis:context"),
        allowed_source_refs=("source:structure", "source:context"),
    )
    settlement_body = {
        "operation_ref": operation.operation_ref,
        "status": "completed_with_limits",
        "accepted_artifact_refs": ("artifact:one", "artifact:two"),
        "completed_investigation_count": 2,
        "limited_investigation_count": 0,
        "failed_investigation_count": 0,
        "cancelled_investigation_count": 0,
    }
    settlement = ControlledInvestigationSettlement(
        **settlement_body,
        content_digest=canonical_digest(settlement_body),
    )
    first, second = admission.accepted
    repeated_finding = {
        "findingKind": "concentration",
        "text": "设备型号出现局部负向集中。",
        "sourceRefs": ["source:structure"],
    }
    return ControlledInvestigationWorkflowResult.create(
        operation=operation,
        admission=admission,
        settlement=settlement,
        planner_attempt_refs=("attempt:planner",),
        child_attempt_refs=("attempt:one", "attempt:two"),
        artifact_details=(
            {
                "investigationRef": first.investigation_ref,
                "axisRefs": list(first.axis_refs),
                "expectedOutputKind": first.expected_output_kind,
                "output": {
                    "findings": [repeated_finding, repeated_finding],
                    "limitationRefs": ["source:structure"],
                },
            },
            {
                "investigationRef": second.investigation_ref,
                "axisRefs": list(second.axis_refs),
                "expectedOutputKind": second.expected_output_kind,
                "output": {
                    "findings": [
                        {
                            "findingKind": "alternative",
                            "text": "日历效应是弱竞争解释。",
                            "sourceRefs": ["source:context"],
                        }
                    ],
                    "limitationRefs": ["source:context"],
                },
            },
        ),
    )


def test_parent_context_contains_only_single_placement_narrative_deltas() -> None:
    context = _result().narrative_context_record()

    assert context.startswith("controlled_investigation_delta_context=")
    payload = json.loads(context.split("=", 1)[1])
    assert payload["schema_version"] == (
        "controlled-investigation-narrative-delta.v1"
    )
    assert payload["omittedExactDuplicateCount"] == 1
    assert len(payload["candidateDeltas"]) == 2
    deltas = [
        dict(zip(payload["candidateDeltaColumns"], row, strict=True))
        for row in payload["candidateDeltas"]
    ]
    assert {
        item["preferredBlockRole"] for item in deltas
    } == {"dimension_localization", "contextual_pattern"}
    assert payload["investigationColumns"] == [
        "investigationId",
        "axisRefs",
        "expectedOutputKind",
    ]
    assert len(payload["investigations"]) == 2
    assert payload["usageContract"] == {
        "baseAuthorityWins": True,
        "candidateMayBeOmitted": True,
        "doNotRestateOverallAnswerOrSourceSummary": True,
        "factualProseRequiresAcceptedHandles": True,
        "maximumPlacementsPerDelta": 1,
        "mode": "delta_only_single_placement",
        "usePreferredBlockRoleWhenMaterial": True,
    }
    assert "candidate_investigations" not in context
    assert '"output"' not in context
    assert '"summary"' not in context
    assert '"title"' not in context
    assert '"limitationRefs"' not in context
    assert context.count("设备型号出现局部负向集中。") == 1
