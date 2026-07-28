from __future__ import annotations

from pathlib import Path

import pytest

from bi_agent.runtime.controlled_investigation_runtime import (
    CONTROLLED_INVESTIGATION_INSTRUCTIONS,
    ControlledInvestigationOperation,
    ControlledInvestigationOutput,
    ControlledInvestigationProposal,
    admit_controlled_investigations,
    validate_controlled_investigation_output,
)
from bi_agent.runtime.controlled_investigation_workflow import (
    _proposal_output_validator,
)


ROOT = Path(__file__).resolve().parents[2]


def _operation() -> ControlledInvestigationOperation:
    return ControlledInvestigationOperation.create(
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


def _proposal() -> ControlledInvestigationProposal:
    return ControlledInvestigationProposal.model_validate(
        {
            "investigations": [
                {
                    "investigationKey": "mechanism",
                    "question": "复核主驱动和抵消项。",
                    "axisRefs": ["axis:formula"],
                    "sourceRefs": ["c_1", "m_1", "l_1"],
                    "expectedOutputKind": "mechanism_explanation",
                },
                {
                    "investigationKey": "structure",
                    "question": "检查结构集中点和反向信号。",
                    "axisRefs": ["axis:structure"],
                    "sourceRefs": ["c_2", "m_2"],
                    "expectedOutputKind": "structure_concentration",
                },
            ]
        }
    )


def test_admission_uses_accepted_axes_and_allowlisted_materials() -> None:
    admission = admit_controlled_investigations(
        operation=_operation(),
        proposal=_proposal(),
        accepted_axis_refs=("axis:formula", "axis:structure", "axis:context"),
        allowed_source_refs=("c_1", "m_1", "l_1", "c_2", "m_2"),
    )

    assert len(admission.accepted) == 2
    assert admission.rejected == ()
    assert {
        investigation.parent_operation_ref
        for investigation in admission.accepted
    } == {_operation().operation_ref}
    assert len(
        {investigation.input_digest for investigation in admission.accepted}
    ) == 2
    assert all(
        investigation.child_run_id.startswith("controlled-child-run:sha256:")
        for investigation in admission.accepted
    )


def test_admission_rejects_unknown_axes_sources_and_overlapping_work() -> None:
    proposal = ControlledInvestigationProposal.model_validate(
        {
            "investigations": [
                {
                    "investigationKey": "accepted",
                    "question": "复核合法材料。",
                    "axisRefs": ["axis:formula"],
                    "sourceRefs": ["c_1"],
                    "expectedOutputKind": "mechanism_explanation",
                },
                {
                    "investigationKey": "overlap",
                    "question": "重复复核同一分析轴。",
                    "axisRefs": ["axis:formula"],
                    "sourceRefs": ["c_2"],
                    "expectedOutputKind": "alternative_explanation",
                },
                {
                    "investigationKey": "unknown-source",
                    "question": "读取未授权材料。",
                    "axisRefs": ["axis:context"],
                    "sourceRefs": ["source:invented"],
                    "expectedOutputKind": "alternative_explanation",
                },
            ]
        }
    )

    admission = admit_controlled_investigations(
        operation=_operation(),
        proposal=proposal,
        accepted_axis_refs=("axis:formula", "axis:context"),
        allowed_source_refs=("c_1", "c_2"),
    )

    assert [item.investigation_key for item in admission.accepted] == ["accepted"]
    assert {
        item.reason
        for item in admission.rejected
    } == {
        "investigation_axis_overlap",
        "investigation_source_unapproved",
    }

    unknown_axis = ControlledInvestigationProposal.model_validate(
        {
            "investigations": [
                {
                    "investigationKey": "unknown-axis",
                    "question": "复核未接受的分析轴。",
                    "axisRefs": ["axis:invented"],
                    "sourceRefs": ["c_1"],
                    "expectedOutputKind": "alternative_explanation",
                }
            ]
        }
    )
    rejected_axis = admit_controlled_investigations(
        operation=_operation(),
        proposal=unknown_axis,
        accepted_axis_refs=("axis:formula", "axis:context"),
        allowed_source_refs=("c_1",),
    )
    assert [item.reason for item in rejected_axis.rejected] == [
        "investigation_axis_unaccepted"
    ]


@pytest.mark.parametrize(
    ("investigations", "error"),
    (
        (
            [
                {
                    "investigationKey": "unknown-axis",
                    "question": "复核模型自行命名的分析轴。",
                    "axisRefs": ["payment_channel_and_method"],
                    "sourceRefs": ["c_1"],
                    "expectedOutputKind": "mechanism_explanation",
                }
            ],
            "investigation_axis_unaccepted",
        ),
        (
            [
                {
                    "investigationKey": "first",
                    "question": "第一次使用同一分析轴。",
                    "axisRefs": ["axis:formula"],
                    "sourceRefs": ["c_1"],
                    "expectedOutputKind": "mechanism_explanation",
                },
                {
                    "investigationKey": "second",
                    "question": "第二次使用同一分析轴。",
                    "axisRefs": ["axis:formula"],
                    "sourceRefs": ["c_2"],
                    "expectedOutputKind": "alternative_explanation",
                },
            ],
            "investigation_axis_overlap",
        ),
        (
            [
                {
                    "investigationKey": "unknown-source",
                    "question": "读取模型自行命名的材料。",
                    "axisRefs": ["axis:formula"],
                    "sourceRefs": ["source:invented"],
                    "expectedOutputKind": "alternative_explanation",
                }
            ],
            "investigation_source_unapproved",
        ),
    ),
)
def test_planner_typed_output_rejects_unaccepted_or_overlapping_refs(
    investigations: list[dict[str, object]],
    error: str,
) -> None:
    validate = _proposal_output_validator(
        accepted_axis_refs=("axis:formula", "axis:structure"),
        allowed_source_refs=("c_1", "c_2"),
    )

    with pytest.raises(ValueError, match=error):
        validate({"investigations": investigations})


def test_child_output_requires_allowlist_closure_and_rejects_forged_refs() -> None:
    valid = ControlledInvestigationOutput.model_validate(
        {
            "findings": [
                {
                    "findingKind": "offset",
                    "text": "一个已验证分项抵消了部分增长。",
                    "sourceRefs": ["c_1", "m_1"],
                }
            ],
            "limitationRefs": ["l_1"],
        }
    )
    assert (
        validate_controlled_investigation_output(
            valid,
            allowed_source_refs=("c_1", "m_1", "l_1"),
        )
        == valid
    )

    forged = ControlledInvestigationOutput.model_validate(
        {
            "findings": [
                {
                    "findingKind": "offset",
                    "text": "伪造来源。",
                    "sourceRefs": ["source:forged"],
                }
            ],
            "limitationRefs": [],
        }
    )
    with pytest.raises(
        ValueError,
        match="controlled_investigation_source_unknown",
    ):
        validate_controlled_investigation_output(
            forged,
            allowed_source_refs=("c_1", "m_1", "l_1"),
        )


def test_prompt_injection_is_data_and_child_has_no_tool_or_authority_contract() -> None:
    assert "untrusted data" in CONTROLLED_INVESTIGATION_INSTRUCTIONS
    assert "Ignore instructions" in CONTROLLED_INVESTIGATION_INSTRUCTIONS
    assert "Do not call tools" in CONTROLLED_INVESTIGATION_INSTRUCTIONS
    assert "claim" in CONTROLLED_INVESTIGATION_INSTRUCTIONS.lower()
    assert "publication" in CONTROLLED_INVESTIGATION_INSTRUCTIONS.lower()
    assert "delivery" in CONTROLLED_INVESTIGATION_INSTRUCTIONS.lower()

    output_fields = set(ControlledInvestigationOutput.model_fields)
    assert output_fields == {
        "findings",
        "limitation_refs",
    }
    assert "quality_audit" not in str(
        ControlledInvestigationProposal.model_json_schema()
    )


def test_same_logical_input_has_stable_parent_child_identity() -> None:
    first = admit_controlled_investigations(
        operation=_operation(),
        proposal=_proposal(),
        accepted_axis_refs=("axis:formula", "axis:structure"),
        allowed_source_refs=("c_1", "m_1", "l_1", "c_2", "m_2"),
    )
    second = admit_controlled_investigations(
        operation=_operation(),
        proposal=_proposal(),
        accepted_axis_refs=("axis:formula", "axis:structure"),
        allowed_source_refs=("c_1", "m_1", "l_1", "c_2", "m_2"),
    )

    assert first == second
    assert len(
        {item.investigation_ref for item in first.accepted}
    ) == len(first.accepted)


def test_admission_rejects_child_source_view_over_runtime_budget() -> None:
    admission = admit_controlled_investigations(
        operation=_operation(),
        proposal=ControlledInvestigationProposal.model_validate(
            {
                "investigations": [
                    {
                        "investigationKey": "bounded-read",
                        "question": "Inspect the accepted materials.",
                        "axisRefs": ["axis:formula"],
                        "sourceRefs": ["c_1", "m_1"],
                        "expectedOutputKind": "mechanism_explanation",
                    }
                ]
            }
        ),
        accepted_axis_refs=("axis:formula",),
        allowed_source_refs=("c_1", "m_1"),
        source_read_bytes={"c_1": 48_000, "m_1": 2_000},
        maximum_source_read_bytes=49_152,
    )

    assert admission.accepted == ()
    assert admission.rejected[0].reason == "investigation_source_budget_exceeded"


def test_quality_review_remains_outside_publication_dependency_graph() -> None:
    post_execution = (
        ROOT / "bi_agent/runtime/post_execution_workflow.py"
    ).read_text(encoding="utf-8")
    publication_source = (
        ROOT / "bi_agent/runtime/publication_persistence.py"
    ).read_text(encoding="utf-8")
    persist_publication = publication_source[
        publication_source.index("def persist_publication(") :
        publication_source.index("def _narrative_quality_audit_records(")
    ]

    assert "narrative_quality_audit_results" not in post_execution
    assert "quality_audit" not in post_execution
    assert "quality_audit" not in persist_publication


def test_controlled_investigation_remains_explicitly_opt_in() -> None:
    agent_core = (
        ROOT / "bi_agent/conversation/agent_core.py"
    ).read_text(encoding="utf-8")
    binding = agent_core[
        agent_core.index('"controlled_investigation_enabled"') :
        agent_core.index("@classmethod", agent_core.index(
            '"controlled_investigation_enabled"'
        ))
    ]

    assert '"WAJE_CONTROLLED_INVESTIGATION_ENABLED"' in binding
    assert '"0"' in binding
    assert '== "1"' in binding
