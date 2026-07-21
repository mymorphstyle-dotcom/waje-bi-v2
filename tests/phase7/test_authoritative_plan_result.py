from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any

import pytest

from bi_agent.runtime.authoritative_plan_result import (
    AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION,
    parse_authoritative_plan_result,
    planner_raw_response_ref,
    validate_planner_provider_audit_closure,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    AuthorityContext,
    ClaimObligation,
    EvidenceRequirement,
    PlannerProposal,
    PlanRevision,
    ProposalAdmissionRecord,
)
from bi_agent.runtime.single_authority import DurableTransition
from tests.support.temporal_authority import resolved_test_temporal_authority


RUN_ID = "run-phase02-plan-result"
INTENT_ID = "intent-phase02-plan-result"
DECISION_REFS = ("decision-phase02-plan-result",)
CONTRACT_VERSIONS = {
    "clickhouse_analysis_bindings": "v7",
    "analysis_goal_registry": "v1",
}
AXIS_PROPOSAL_ID = "proposal-axis-change-validation"
HYPOTHESIS_PROPOSAL_ID = "proposal-hypothesis-change"
PRIORITY_PROPOSAL_ID = "proposal-priority-change-validation"
ASSUMPTION_PROPOSAL_ID = "proposal-assumption-channel-stability"


def _plan_result(
    *,
    context_versions: dict[str, str] | None = None,
    admitted_versions: dict[str, str] | None = None,
    admission_entries: list[dict[str, Any]] | None = None,
    axis_proposal_refs: tuple[str, ...] | None = None,
    assumption_refs: tuple[str, ...] | None = None,
    transition_input_digest: str | None = None,
    supersedes_plan_revision_id: str | None = None,
    plan_patch_ref: str | None = None,
    transition_node_name: str | None = None,
    next_transition: str | None = None,
) -> dict[str, Any]:
    context_versions = context_versions or dict(CONTRACT_VERSIONS)
    admitted_versions = admitted_versions or dict(context_versions)
    context = AuthorityContext.create(
        run_attempt_id=RUN_ID,
        actual_as_of="2026-07-18T00:00:00Z",
        release_refs=(),
        snapshot_refs=(),
        dataset_coverage=(),
        contract_versions=context_versions,
    )
    structured_output = {
        "issue_tree": [
            {
                "issue_id": "issue-root",
                "parent_issue_id": None,
                "question": "目标日期相比前一日发生了什么？",
                "target_claim_kind": "comparative_change",
            }
        ],
        "auxiliary_axes": [
            {
                "proposal_item_id": AXIS_PROPOSAL_ID,
                "axis_id": "change_validation",
                "rationale": "Validate the requested comparison.",
                "supports_claim_kinds": ["comparative_change"],
            }
        ],
        "hypotheses": [
            {
                "proposal_item_id": HYPOTHESIS_PROPOSAL_ID,
                "statement": "Channel mix may explain the change.",
                "target_claim_kind": "comparative_change",
                "requested_axis_ids": ["change_validation"],
                "assumption_refs": [ASSUMPTION_PROPOSAL_ID],
            }
        ],
        "priority_proposals": [
            {
                "proposal_item_id": PRIORITY_PROPOSAL_ID,
                "target_ref": "change_validation",
                "rationale": "Resolve the primary comparison first.",
            }
        ],
        "assumption_proposals": [
            {
                "proposal_item_id": ASSUMPTION_PROPOSAL_ID,
                "statement": "Channel definitions are stable.",
                "affected_refs": ["change_validation"],
            }
        ],
    }
    raw_response = json.dumps(
        structured_output,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    proposal = PlannerProposal.create(
        run_attempt_id=RUN_ID,
        intent_revision_id=INTENT_ID,
        decision_refs=DECISION_REFS,
        authority_context_ref=context.authority_context_ref,
        issue_tree=structured_output["issue_tree"],
        auxiliary_axes=structured_output["auxiliary_axes"],
        hypotheses=structured_output["hypotheses"],
        priority_proposals=structured_output["priority_proposals"],
        assumption_proposals=structured_output["assumption_proposals"],
        raw_provider_response_ref=(
            "restricted-provider-response:sha256:"
            + sha256(raw_response.encode("utf-8")).hexdigest()
        ),
        schema_version="planner-proposal.v1",
        prompt_version=(
            "single-authority-plan-patch-proposal.v1"
            if supersedes_plan_revision_id is not None
            else "single-authority-plan-proposal.v1"
        ),
        model_version="phase02-plan-model",
    )
    default_admission_entries = [
        {
            "proposal_item_ref": AXIS_PROPOSAL_ID,
            "item_kind": "analysis_axis",
            "status": "admitted",
            "reason_code": "supported_auxiliary_axis",
            "contract_refs": ["contract:change-validation"],
            "normalized_execution_ref": "change_validation",
        },
        {
            "proposal_item_ref": HYPOTHESIS_PROPOSAL_ID,
            "item_kind": "hypothesis",
            "status": "admitted",
            "reason_code": "hypothesis_contract_bound",
            "contract_refs": ["contract:change-validation"],
            "normalized_execution_ref": (f"hypothesis:{HYPOTHESIS_PROPOSAL_ID}"),
        },
        {
            "proposal_item_ref": PRIORITY_PROPOSAL_ID,
            "item_kind": "priority",
            "status": "admitted",
            "reason_code": "priority_target_scheduled",
            "contract_refs": ["contract:change-validation"],
            "normalized_execution_ref": "change_validation",
        },
        {
            "proposal_item_ref": ASSUMPTION_PROPOSAL_ID,
            "item_kind": "assumption",
            "status": "admitted",
            "reason_code": "assumption_refs_contract_bound",
            "contract_refs": ["change_validation"],
            "normalized_execution_ref": (f"assumption:{ASSUMPTION_PROPOSAL_ID}"),
        },
    ]
    admission = ProposalAdmissionRecord.create(
        planner_proposal_ref=proposal.planner_proposal_id,
        intent_revision_id=INTENT_ID,
        decision_refs=DECISION_REFS,
        authority_context_ref=context.authority_context_ref,
        admission_entries=(
            default_admission_entries
            if admission_entries is None
            else admission_entries
        ),
        compiler_version="single-authority-plan-compiler.v1",
        contract_versions=admitted_versions,
    )
    obligation = ClaimObligation.create(
        claim_kind="comparative_change",
        role="user_required",
        subject={
            "target_metric_ref": "metric:paid_amount",
            "scope": {"scope_type": "full_sample", "filters": []},
            "outcome_refs": ("outcome:comparative_change",),
            "goal_refs": ("explain_change",),
        },
        evidence_requirement=EvidenceRequirement.create(
            operator="any_of",
            evidence_kinds=("verified_observation",),
        ),
        success_policy={
            "policy": "verified_or_explicit_boundary",
            "minimum_claim_strength": "directional",
        },
    )
    axis = AnalysisAxis.create(
        axis_id="change_validation",
        role="required",
        axis_kind="comparison",
        target_metric_refs=("metric:paid_amount",),
        metric_refs=("metric:paid_amount",),
        dimension_refs=(),
        context_source_refs=(),
        capability_refs=("compare_periods",),
        reconciliation_group="paid_amount",
        selection_policy="required",
        source_refs=("source:paid_order_success",),
        goal_refs=("explain_change",),
        supports_obligation_ids=(obligation.obligation_id,),
        proposal_refs=(
            (AXIS_PROPOSAL_ID, HYPOTHESIS_PROPOSAL_ID)
            if axis_proposal_refs is None
            else axis_proposal_refs
        ),
    )
    temporal_authority = resolved_test_temporal_authority(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-06-18",
            "baseline_end": "2026-06-18",
            "aggregation": "sum_of_complete_days",
        },
        require_physical_baseline=True,
    )
    plan = PlanRevision.create(
        run_attempt_id=RUN_ID,
        supersedes_plan_revision_id=supersedes_plan_revision_id,
        intent_revision_id=INTENT_ID,
        decision_refs=DECISION_REFS,
        authority_context_ref=context.authority_context_ref,
        planner_proposal_ref=proposal.planner_proposal_id,
        proposal_admission_ref=admission.proposal_admission_id,
        temporal_authority=temporal_authority,
        resolved_window_refs=temporal_authority.resolved_window_refs,
        context_window_specs=(),
        claim_obligations=(obligation,),
        analysis_axes=(axis,),
        capability_task_specs=(
            {
                "task_key": "compare-periods",
                "capability_id": "compare_periods",
                "normalized_input_refs": (
                    *temporal_authority.resolved_window_refs,
                    "metric:paid_amount",
                ),
                "dependency_task_keys": (),
                "obligation_edges": (
                    {
                        "obligation_id": obligation.obligation_id,
                        "required": True,
                    },
                ),
                "execution_rank": 1,
                "declared_budget_units": 1,
                "governor_inputs": {
                    "expected_information_gain": "obligation_closing",
                    "materiality": "user_required",
                    "actionability": "decision_supporting",
                    "statistical_risk": "contract_bounded",
                },
                "execution_policy": {
                    "degradation_policy": {"missing_required_input": "block_claim"},
                    "integrity_failure": "fail_closed",
                    "input_states": (),
                },
            },
        ),
        assumption_refs=(
            (f"assumption:{ASSUMPTION_PROPOSAL_ID}",)
            if assumption_refs is None
            else assumption_refs
        ),
        budget_policy_ref="budget-policy:default",
        contract_versions=admitted_versions,
    )
    planner_audit = {
        "task": (
            "single_authority_plan_patch_proposal"
            if supersedes_plan_revision_id is not None
            else "single_authority_plan_proposal"
        ),
        "provider": "phase02-plan-provider",
        "model": "phase02-plan-model",
        "prompt_version": proposal.prompt_version,
        "response_id": "response-phase02-plan-result",
        "raw_response_content": raw_response,
        "structured_output": structured_output,
        "usage": {},
    }
    transition_input = {
        "intent_revision_id": INTENT_ID,
        "decision_refs": list(DECISION_REFS),
        "authority_context_ref": context.authority_context_ref,
        "planner_proposal_ref": proposal.planner_proposal_id,
        "proposal_admission_ref": admission.proposal_admission_id,
        "supersedes_plan_revision_id": None,
        "plan_patch_ref": plan_patch_ref,
    }
    transition_input["supersedes_plan_revision_id"] = supersedes_plan_revision_id
    transition_output = {
        "authority_context": context.to_dict(),
        "planner_proposal": proposal.to_dict(),
        "proposal_admission_record": admission.to_dict(),
        "plan_revision": plan.to_dict(),
        "planner_llm_audit": canonical_value(planner_audit),
    }
    transition = DurableTransition.create(
        node_name=(
            transition_node_name
            or (
                "compile_plan_patch"
                if supersedes_plan_revision_id is not None
                else "compile_authoritative_plan"
            )
        ),
        parent_transition_id=None,
        run_attempt_id=RUN_ID,
        intent_revision_id=INTENT_ID,
        decision_ledger_position=1,
        input_digest=(transition_input_digest or canonical_digest(transition_input)),
        output_digest=canonical_digest(transition_output),
        execution_attempt=1,
        provider_ref="phase02-plan-provider",
        model_ref="phase02-plan-model",
        status="succeeded",
        acceptance_state="accepted",
        next_transition=(
            next_transition
            or (
                "phase03_plan_patch_bound"
                if supersedes_plan_revision_id is not None
                else "phase02_plan_bound"
            )
        ),
        started_at="2026-07-18T00:00:01+00:00",
        finished_at="2026-07-18T00:00:02+00:00",
    )
    return {
        "schema_version": AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION,
        "run_id": RUN_ID,
        "run_attempt_id": RUN_ID,
        "status": "planned",
        "intent_revision_id": INTENT_ID,
        "plan_patch_ref": plan_patch_ref,
        "decision_ledger_position": 1,
        "decision_refs": list(DECISION_REFS),
        "authority_context": context.to_dict(),
        "planner_proposal": proposal.to_dict(),
        "proposal_admission_record": admission.to_dict(),
        "plan_revision": plan.to_dict(),
        "durable_checkpoint": transition.to_dict(),
        "authority_refs": {
            "intent_revision_id": INTENT_ID,
            "authority_context_ref": context.authority_context_ref,
            "planner_proposal_id": proposal.planner_proposal_id,
            "proposal_admission_id": admission.proposal_admission_id,
            "plan_revision_id": plan.plan_revision_id,
            "accepted_transition_id": transition.transition_id,
        },
        "llm_calls": [planner_audit],
        "checkpoint_events": [],
    }


def test_parse_authoritative_plan_result_returns_closed_records() -> None:
    payload = _plan_result()

    parsed = parse_authoritative_plan_result(
        payload,
        expected_run_id=RUN_ID,
        expected_llm_calls=payload["llm_calls"],
    )

    assert parsed.run_id == RUN_ID
    assert parsed.decision_refs == DECISION_REFS
    assert parsed.authority_context.contract_versions == (
        parsed.proposal_admission.contract_versions
    )
    assert parsed.plan_revision.contract_versions == (
        parsed.authority_context.contract_versions
    )
    assert parsed.transition.input_digest == canonical_digest(parsed.transition_input)
    assert parsed.transition.output_digest == canonical_digest(parsed.transition_output)
    assert parsed.planner_llm_audit["raw_response_content"]
    assert parsed.plan_patch_ref is None


def test_superseding_plan_selects_only_its_matching_patch_audit() -> None:
    plan_patch_ref = "plan-patch:sha256:" + "1" * 64
    payload = _plan_result(
        supersedes_plan_revision_id="plan-revision-source",
        plan_patch_ref=plan_patch_ref,
    )
    current_audit = deepcopy(payload["llm_calls"][0])
    prior_patch_audit = deepcopy(current_audit)
    prior_output = deepcopy(prior_patch_audit["structured_output"])
    prior_output["issue_tree"][0]["question"] = "此前补充分析了什么？"
    prior_patch_audit["raw_response_content"] = json.dumps(
        prior_output,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    prior_patch_audit["structured_output"] = prior_output
    prior_patch_audit["response_id"] = "response-prior-plan-patch"
    initial_audit = deepcopy(prior_patch_audit)
    initial_audit["task"] = "single_authority_plan_proposal"
    payload["llm_calls"] = [
        initial_audit,
        prior_patch_audit,
        current_audit,
    ]

    parsed = parse_authoritative_plan_result(payload)

    assert parsed.plan_patch_ref == plan_patch_ref
    assert parsed.transition.node_name == "compile_plan_patch"
    assert parsed.transition.next_transition == "phase03_plan_patch_bound"
    assert parsed.planner_llm_audit["response_id"] == "response-phase02-plan-result"
    assert parsed.transition_input["plan_patch_ref"] == plan_patch_ref


def test_plan_result_rejects_ambiguous_matching_planner_audits() -> None:
    payload = _plan_result()
    payload["llm_calls"].append(deepcopy(payload["llm_calls"][0]))

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_planner_provider_audit_invalid",
    ):
        parse_authoritative_plan_result(payload)


@pytest.mark.parametrize(
    "payload",
    (
        _plan_result(plan_patch_ref="plan-patch:sha256:" + "2" * 64),
        _plan_result(
            supersedes_plan_revision_id="plan-revision-source",
            plan_patch_ref=None,
        ),
    ),
)
def test_plan_patch_ref_closes_initial_and_superseding_shapes(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_result_invalid",
    ):
        parse_authoritative_plan_result(payload)


def test_superseding_plan_requires_patch_task_and_transition_tuple() -> None:
    plan_patch_ref = "plan-patch:sha256:" + "3" * 64
    wrong_task = _plan_result(
        supersedes_plan_revision_id="plan-revision-source",
        plan_patch_ref=plan_patch_ref,
    )
    wrong_task["llm_calls"][0]["task"] = "single_authority_plan_proposal"
    wrong_node = _plan_result(
        supersedes_plan_revision_id="plan-revision-source",
        plan_patch_ref=plan_patch_ref,
        transition_node_name="compile_authoritative_plan",
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_planner_provider_audit_invalid",
    ):
        parse_authoritative_plan_result(wrong_task)
    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_authority_mismatch",
    ):
        parse_authoritative_plan_result(wrong_node)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("schema_version", "single-authority-phase02.v0"),
        ("status", "draft"),
        ("run_id", 7),
        ("run_attempt_id", "another-run"),
        ("decision_ledger_position", True),
        ("decision_refs", (DECISION_REFS[0],)),
        ("llm_calls", ()),
        ("checkpoint_events", ()),
    ),
)
def test_plan_result_rejects_invalid_outer_contract_types(
    field: str,
    invalid_value: Any,
) -> None:
    payload = _plan_result()
    payload[field] = invalid_value

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_result_invalid",
    ):
        parse_authoritative_plan_result(payload)


def test_plan_result_requires_exact_top_level_fields() -> None:
    missing = _plan_result()
    missing.pop("authority_refs")
    extra = _plan_result()
    extra["legacy_route"] = {}

    for payload in (missing, extra):
        with pytest.raises(
            EvidenceIntegrityError,
            match="single_authority_plan_result_invalid",
        ):
            parse_authoritative_plan_result(payload)


@pytest.mark.parametrize(
    "record_field",
    (
        "authority_context",
        "planner_proposal",
        "proposal_admission_record",
        "plan_revision",
    ),
)
def test_plan_result_rebuilds_every_content_addressed_record(
    record_field: str,
) -> None:
    payload = _plan_result()
    payload[record_field]["content_digest"] = "0" * 64

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_result_invalid",
    ):
        parse_authoritative_plan_result(payload)


def test_plan_result_rejects_authority_and_decision_ref_gaps() -> None:
    payloads = []
    intent_gap = _plan_result()
    intent_gap["intent_revision_id"] = "intent-other"
    payloads.append(intent_gap)
    decision_gap = _plan_result()
    decision_gap["decision_refs"] = ["decision-other"]
    payloads.append(decision_gap)
    context_gap = _plan_result()
    context_gap["authority_refs"]["authority_context_ref"] = (
        "authority-context:sha256:" + "0" * 64
    )
    payloads.append(context_gap)
    proposal_gap = _plan_result()
    proposal_gap["authority_refs"]["planner_proposal_id"] = (
        "planner-proposal-000000000000000000000000"
    )
    payloads.append(proposal_gap)
    admission_gap = _plan_result()
    admission_gap["authority_refs"]["proposal_admission_id"] = (
        "proposal-admission-000000000000000000000000"
    )
    payloads.append(admission_gap)
    plan_gap = _plan_result()
    plan_gap["authority_refs"]["plan_revision_id"] = (
        "plan-revision-000000000000000000000000"
    )
    payloads.append(plan_gap)

    for payload in payloads:
        with pytest.raises(
            EvidenceIntegrityError,
            match="single_authority_plan_authority_mismatch",
        ):
            parse_authoritative_plan_result(payload)


def test_plan_result_rejects_contract_version_divergence() -> None:
    payload = _plan_result(
        context_versions=dict(CONTRACT_VERSIONS),
        admitted_versions={
            **CONTRACT_VERSIONS,
            "clickhouse_analysis_bindings": "v8",
        },
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_contract_versions_mismatch",
    ):
        parse_authoritative_plan_result(payload)


def test_plan_result_requires_exact_proposal_admission_item_ledger() -> None:
    original_entries = _plan_result()["proposal_admission_record"]["admission_entries"]
    missing_entry = _plan_result(admission_entries=deepcopy(original_entries[:-1]))
    kind_mismatch_entries = deepcopy(original_entries)
    kind_mismatch_entries[0]["item_kind"] = "hypothesis"
    kind_mismatch = _plan_result(admission_entries=kind_mismatch_entries)

    for payload in (missing_entry, kind_mismatch):
        with pytest.raises(
            EvidenceIntegrityError,
            match=("single_authority_plan_proposal_admission_closure_mismatch"),
        ):
            parse_authoritative_plan_result(payload)


@pytest.mark.parametrize(
    ("entry_index", "invalid_execution_ref"),
    (
        (0, "other_axis"),
        (1, "hypothesis:other"),
        (2, "other_axis"),
        (3, "assumption:other"),
    ),
)
def test_plan_result_binds_admitted_items_to_normalized_execution_refs(
    entry_index: int,
    invalid_execution_ref: str,
) -> None:
    entries = deepcopy(_plan_result()["proposal_admission_record"]["admission_entries"])
    entries[entry_index]["normalized_execution_ref"] = invalid_execution_ref

    with pytest.raises(
        EvidenceIntegrityError,
        match=("single_authority_plan_proposal_admission_closure_mismatch"),
    ):
        parse_authoritative_plan_result(_plan_result(admission_entries=entries))


def test_plan_result_closes_admission_status_into_plan_provenance() -> None:
    admitted_axis_missing = _plan_result(axis_proposal_refs=(HYPOTHESIS_PROPOSAL_ID,))
    admitted_hypothesis_missing = _plan_result(axis_proposal_refs=(AXIS_PROPOSAL_ID,))
    admitted_assumption_missing = _plan_result(assumption_refs=())

    rejected_axis_entries = deepcopy(
        _plan_result()["proposal_admission_record"]["admission_entries"]
    )
    rejected_axis_entries[0]["status"] = "rejected"
    rejected_axis_entries[0]["reason_code"] = "axis_rejected"
    rejected_axis_entries[0]["normalized_execution_ref"] = None
    rejected_axis_leaked = _plan_result(admission_entries=rejected_axis_entries)

    deferred_priority_entries = deepcopy(
        _plan_result()["proposal_admission_record"]["admission_entries"]
    )
    deferred_priority_entries[2]["status"] = "deferred"
    deferred_priority_entries[2]["reason_code"] = "priority_deferred"
    deferred_priority_entries[2]["normalized_execution_ref"] = None
    deferred_priority_leaked = _plan_result(
        admission_entries=deferred_priority_entries,
        axis_proposal_refs=(
            AXIS_PROPOSAL_ID,
            HYPOTHESIS_PROPOSAL_ID,
            PRIORITY_PROPOSAL_ID,
        ),
    )

    rejected_assumption_entries = deepcopy(
        _plan_result()["proposal_admission_record"]["admission_entries"]
    )
    rejected_assumption_entries[3]["status"] = "rejected"
    rejected_assumption_entries[3]["reason_code"] = "assumption_rejected"
    rejected_assumption_entries[3]["normalized_execution_ref"] = None
    rejected_assumption_leaked = _plan_result(
        admission_entries=rejected_assumption_entries
    )

    for payload in (
        admitted_axis_missing,
        admitted_hypothesis_missing,
        admitted_assumption_missing,
        rejected_axis_leaked,
        deferred_priority_leaked,
        rejected_assumption_leaked,
    ):
        with pytest.raises(
            EvidenceIntegrityError,
            match=("single_authority_plan_proposal_admission_closure_mismatch"),
        ):
            parse_authoritative_plan_result(payload)


def test_plan_result_rejects_transition_state_and_digest_divergence() -> None:
    wrong_state = _plan_result(next_transition="execute_capabilities")
    wrong_digest = _plan_result(transition_input_digest="0" * 64)

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_authority_mismatch",
    ):
        parse_authoritative_plan_result(wrong_state)
    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_transition_digest_mismatch",
    ):
        parse_authoritative_plan_result(wrong_digest)


def test_plan_result_binds_raw_provider_audit_to_proposal_and_transition() -> None:
    raw_tampered = _plan_result()
    raw_tampered["llm_calls"][0]["raw_response_content"] += " "
    structured_tampered = _plan_result()
    structured_tampered["llm_calls"][0]["structured_output"]["issue_tree"][0][
        "question"
    ] = "Different question"
    provider_tampered = _plan_result()
    provider_tampered["llm_calls"][0]["provider"] = "other-provider"
    model_tampered = _plan_result()
    model_tampered["llm_calls"][0]["model"] = "other-routed-model"

    for payload in (
        raw_tampered,
        structured_tampered,
        provider_tampered,
        model_tampered,
    ):
        with pytest.raises(
            EvidenceIntegrityError,
            match="single_authority_planner_provider_audit_invalid",
        ):
            parse_authoritative_plan_result(payload)


def test_planner_provider_audit_rejects_replaced_raw_even_with_synced_ref() -> None:
    payload = _plan_result()
    original = PlannerProposal.from_dict(payload["planner_proposal"])
    transition = DurableTransition.from_dict(payload["durable_checkpoint"])
    audit = deepcopy(payload["llm_calls"][0])
    replaced_raw_output = deepcopy(audit["structured_output"])
    replaced_raw_output["issue_tree"][0]["question"] = "另一个业务问题是什么？"
    replaced_raw = json.dumps(
        replaced_raw_output,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    audit["raw_response_content"] = replaced_raw
    proposal_with_synced_ref = PlannerProposal.create(
        run_attempt_id=original.run_attempt_id,
        intent_revision_id=original.intent_revision_id,
        decision_refs=original.decision_refs,
        authority_context_ref=original.authority_context_ref,
        issue_tree=original.issue_tree,
        auxiliary_axes=original.auxiliary_axes,
        hypotheses=original.hypotheses,
        priority_proposals=original.priority_proposals,
        assumption_proposals=original.assumption_proposals,
        raw_provider_response_ref=planner_raw_response_ref(replaced_raw),
        schema_version=original.schema_version,
        prompt_version=original.prompt_version,
        model_version=original.model_version,
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_planner_provider_audit_invalid",
    ):
        validate_planner_provider_audit_closure(
            planner_audit=audit,
            planner_proposal=proposal_with_synced_ref,
            transition=transition,
            error_code="single_authority_planner_provider_audit_invalid",
        )


def test_planner_provider_audit_rejects_unsealed_raw_shape() -> None:
    payload = _plan_result()
    original = PlannerProposal.from_dict(payload["planner_proposal"])
    transition = DurableTransition.from_dict(payload["durable_checkpoint"])
    audit = deepcopy(payload["llm_calls"][0])
    raw_output = deepcopy(audit["structured_output"])
    raw_output["issue_tree"][0]["question"] = "目标日期vs前一日发生了什么？"
    raw_response = json.dumps(
        raw_output,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    audit["raw_response_content"] = raw_response
    proposal = PlannerProposal.create(
        run_attempt_id=original.run_attempt_id,
        intent_revision_id=original.intent_revision_id,
        decision_refs=original.decision_refs,
        authority_context_ref=original.authority_context_ref,
        issue_tree=original.issue_tree,
        auxiliary_axes=original.auxiliary_axes,
        hypotheses=original.hypotheses,
        priority_proposals=original.priority_proposals,
        assumption_proposals=original.assumption_proposals,
        raw_provider_response_ref=planner_raw_response_ref(raw_response),
        schema_version=original.schema_version,
        prompt_version=original.prompt_version,
        model_version=original.model_version,
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_planner_provider_audit_invalid",
    ):
        validate_planner_provider_audit_closure(
            planner_audit=audit,
            planner_proposal=proposal,
            transition=transition,
            error_code="single_authority_planner_provider_audit_invalid",
        )


def test_plan_result_checks_caller_run_and_llm_call_envelope() -> None:
    payload = _plan_result()

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_result_invalid",
    ):
        parse_authoritative_plan_result(
            payload,
            expected_run_id="run-other",
        )
    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_result_invalid",
    ):
        parse_authoritative_plan_result(
            payload,
            expected_llm_calls=(),
        )
