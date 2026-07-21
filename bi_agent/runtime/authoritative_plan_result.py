from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.llm_client import (
    LLMOutputError,
    parse_llm_structured_response_content,
)
from bi_agent.runtime.plan_authority import (
    AuthorityContext,
    PlannerProposal,
    PlanRevision,
    ProposalAdmissionRecord,
)
from bi_agent.runtime.single_authority import DurableTransition


AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION = "single-authority-phase02.v2"

_PLAN_RESULT_FIELDS = {
    "schema_version",
    "run_id",
    "run_attempt_id",
    "status",
    "intent_revision_id",
    "plan_patch_ref",
    "decision_ledger_position",
    "decision_refs",
    "authority_context",
    "planner_proposal",
    "proposal_admission_record",
    "plan_revision",
    "durable_checkpoint",
    "authority_refs",
    "llm_calls",
    "checkpoint_events",
}

_AUTHORITY_REF_FIELDS = {
    "intent_revision_id",
    "authority_context_ref",
    "planner_proposal_id",
    "proposal_admission_id",
    "plan_revision_id",
    "accepted_transition_id",
}

_PLANNER_OUTPUT_FIELDS = {
    "issue_tree",
    "auxiliary_axes",
    "hypotheses",
    "priority_proposals",
    "assumption_proposals",
}


@dataclass(frozen=True)
class ParsedAuthoritativePlanResult:
    run_id: str
    plan_patch_ref: str | None
    decision_ledger_position: int
    decision_refs: tuple[str, ...]
    authority_context: AuthorityContext
    planner_proposal: PlannerProposal
    proposal_admission: ProposalAdmissionRecord
    plan_revision: PlanRevision
    transition: DurableTransition
    authority_refs: Mapping[str, str]
    llm_calls: tuple[Mapping[str, Any], ...]
    checkpoint_events: tuple[Mapping[str, Any], ...]
    planner_llm_audit: Mapping[str, Any]
    transition_input: Mapping[str, Any]
    transition_output: Mapping[str, Any]


def planner_raw_response_ref(raw_response: str) -> str:
    if not _is_required_string(raw_response):
        raise EvidenceIntegrityError("planner_provider_audit_invalid")
    return (
        "restricted-provider-response:sha256:"
        + sha256(raw_response.encode("utf-8")).hexdigest()
    )


def validate_planner_provider_audit_closure(
    *,
    planner_audit: Mapping[str, Any],
    planner_proposal: PlannerProposal,
    transition: DurableTransition,
    error_code: str = "planner_provider_audit_invalid",
) -> Mapping[str, Any]:
    expected_task = _planner_task_for_transition(transition)
    raw_response = (
        planner_audit.get("raw_response_content")
        if isinstance(planner_audit, Mapping)
        else None
    )
    structured_output = (
        planner_audit.get("structured_output")
        if isinstance(planner_audit, Mapping)
        else None
    )
    provider = (
        planner_audit.get("provider") if isinstance(planner_audit, Mapping) else None
    )
    model = planner_audit.get("model") if isinstance(planner_audit, Mapping) else None
    if (
        not isinstance(planner_audit, Mapping)
        or planner_audit.get("task") != expected_task
        or not _is_required_string(raw_response)
        or not isinstance(structured_output, Mapping)
        or not _is_required_string(provider)
        or not _is_required_string(model)
        or planner_proposal.prompt_version != planner_audit.get("prompt_version")
        or planner_proposal.model_version != model
        or transition.provider_ref != provider
        or transition.model_ref != model
        or planner_proposal.raw_provider_response_ref
        != planner_raw_response_ref(raw_response)
    ):
        raise EvidenceIntegrityError(error_code)
    try:
        parsed_raw_output = parse_llm_structured_response_content(raw_response)
    except LLMOutputError as exc:
        raise EvidenceIntegrityError(error_code) from exc
    expected_structured_output = {
        field: canonical_value(getattr(planner_proposal, field))
        for field in _PLANNER_OUTPUT_FIELDS
    }
    if (
        set(structured_output) != _PLANNER_OUTPUT_FIELDS
        or canonical_value(parsed_raw_output) != canonical_value(structured_output)
        or canonical_value(structured_output)
        != canonical_value(expected_structured_output)
    ):
        raise EvidenceIntegrityError(error_code)
    return canonical_value(planner_audit)


def parse_authoritative_plan_result(
    payload: Mapping[str, Any] | None,
    *,
    expected_run_id: str | None = None,
    expected_llm_calls: Sequence[Mapping[str, Any]] | None = None,
) -> ParsedAuthoritativePlanResult:
    if not isinstance(payload, Mapping) or set(payload) != _PLAN_RESULT_FIELDS:
        raise EvidenceIntegrityError("single_authority_plan_result_invalid")

    run_id = payload.get("run_id")
    run_attempt_id = payload.get("run_attempt_id")
    intent_revision_id = payload.get("intent_revision_id")
    plan_patch_ref = payload.get("plan_patch_ref")
    ledger_position = payload.get("decision_ledger_position")
    decision_refs = payload.get("decision_refs")
    llm_calls = payload.get("llm_calls")
    checkpoint_events = payload.get("checkpoint_events")
    if (
        payload.get("schema_version") != AUTHORITATIVE_PLAN_RESULT_SCHEMA_VERSION
        or payload.get("status") != "planned"
        or not _is_required_string(run_id)
        or run_attempt_id != run_id
        or not _is_required_string(intent_revision_id)
        or isinstance(ledger_position, bool)
        or not isinstance(ledger_position, int)
        or ledger_position < 0
        or not isinstance(decision_refs, list)
        or any(not _is_required_string(item) for item in decision_refs)
        or len(decision_refs) != len(set(decision_refs))
        or not isinstance(llm_calls, list)
        or any(not isinstance(item, Mapping) for item in llm_calls)
        or not isinstance(checkpoint_events, list)
        or any(not isinstance(item, Mapping) for item in checkpoint_events)
    ):
        raise EvidenceIntegrityError("single_authority_plan_result_invalid")
    if expected_run_id is not None and (
        not _is_required_string(expected_run_id) or run_id != expected_run_id
    ):
        raise EvidenceIntegrityError("single_authority_plan_result_invalid")
    if expected_llm_calls is not None:
        if (
            isinstance(expected_llm_calls, (str, bytes))
            or not isinstance(expected_llm_calls, Sequence)
            or any(not isinstance(item, Mapping) for item in expected_llm_calls)
            or canonical_value(llm_calls) != canonical_value(expected_llm_calls)
        ):
            raise EvidenceIntegrityError("single_authority_plan_result_invalid")

    try:
        authority_context = AuthorityContext.from_dict(payload["authority_context"])
        planner_proposal = PlannerProposal.from_dict(payload["planner_proposal"])
        proposal_admission = ProposalAdmissionRecord.from_dict(
            payload["proposal_admission_record"]
        )
        plan_revision = PlanRevision.from_dict(payload["plan_revision"])
        transition = DurableTransition.from_dict(payload["durable_checkpoint"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("single_authority_plan_result_invalid") from exc

    is_superseding = plan_revision.supersedes_plan_revision_id is not None
    if (is_superseding and not _is_required_string(plan_patch_ref)) or (
        not is_superseding and plan_patch_ref is not None
    ):
        raise EvidenceIntegrityError("single_authority_plan_result_invalid")
    expected_node_name = (
        "compile_plan_patch" if is_superseding else "compile_authoritative_plan"
    )
    expected_next_transition = (
        "phase03_plan_patch_bound" if is_superseding else "phase02_plan_bound"
    )

    expected_refs = {
        "intent_revision_id": plan_revision.intent_revision_id,
        "authority_context_ref": authority_context.authority_context_ref,
        "planner_proposal_id": planner_proposal.planner_proposal_id,
        "proposal_admission_id": proposal_admission.proposal_admission_id,
        "plan_revision_id": plan_revision.plan_revision_id,
        "accepted_transition_id": transition.transition_id,
    }
    authority_refs = payload.get("authority_refs")
    record_run_attempt_ids = {
        authority_context.run_attempt_id,
        planner_proposal.run_attempt_id,
        plan_revision.run_attempt_id,
        transition.run_attempt_id,
    }
    if (
        not isinstance(authority_refs, Mapping)
        or set(authority_refs) != _AUTHORITY_REF_FIELDS
        or canonical_value(authority_refs) != canonical_value(expected_refs)
        or record_run_attempt_ids != {run_id}
        or payload["intent_revision_id"] != plan_revision.intent_revision_id
        or tuple(decision_refs) != plan_revision.decision_refs
        or planner_proposal.intent_revision_id != plan_revision.intent_revision_id
        or proposal_admission.intent_revision_id != plan_revision.intent_revision_id
        or planner_proposal.decision_refs != plan_revision.decision_refs
        or proposal_admission.decision_refs != plan_revision.decision_refs
        or planner_proposal.authority_context_ref
        != authority_context.authority_context_ref
        or proposal_admission.authority_context_ref
        != authority_context.authority_context_ref
        or plan_revision.authority_context_ref
        != authority_context.authority_context_ref
        or proposal_admission.planner_proposal_ref
        != planner_proposal.planner_proposal_id
        or plan_revision.planner_proposal_ref != planner_proposal.planner_proposal_id
        or plan_revision.proposal_admission_ref
        != proposal_admission.proposal_admission_id
        or transition.intent_revision_id != plan_revision.intent_revision_id
        or transition.decision_ledger_position != ledger_position
        or transition.node_name != expected_node_name
        or transition.status != "succeeded"
        or transition.acceptance_state != "accepted"
        or transition.next_transition != expected_next_transition
    ):
        raise EvidenceIntegrityError("single_authority_plan_authority_mismatch")

    versions = canonical_value(authority_context.contract_versions)
    if (
        canonical_value(proposal_admission.contract_versions) != versions
        or canonical_value(plan_revision.contract_versions) != versions
    ):
        raise EvidenceIntegrityError("single_authority_plan_contract_versions_mismatch")
    validate_proposal_admission_plan_closure(
        planner_proposal=planner_proposal,
        proposal_admission=proposal_admission,
        plan_revision=plan_revision,
    )

    matching_planner_audits = [
        item
        for item in llm_calls
        if _planner_audit_identity_matches(
            item,
            planner_proposal=planner_proposal,
            transition=transition,
        )
    ]
    if len(matching_planner_audits) != 1:
        raise EvidenceIntegrityError("single_authority_planner_provider_audit_invalid")
    planner_audit = validate_planner_provider_audit_closure(
        planner_audit=matching_planner_audits[0],
        planner_proposal=planner_proposal,
        transition=transition,
        error_code="single_authority_planner_provider_audit_invalid",
    )

    transition_input = {
        "intent_revision_id": plan_revision.intent_revision_id,
        "decision_refs": list(plan_revision.decision_refs),
        "authority_context_ref": authority_context.authority_context_ref,
        "planner_proposal_ref": planner_proposal.planner_proposal_id,
        "proposal_admission_ref": proposal_admission.proposal_admission_id,
        "supersedes_plan_revision_id": (plan_revision.supersedes_plan_revision_id),
        "plan_patch_ref": plan_patch_ref,
    }
    transition_output = {
        "authority_context": authority_context.to_dict(),
        "planner_proposal": planner_proposal.to_dict(),
        "proposal_admission_record": proposal_admission.to_dict(),
        "plan_revision": plan_revision.to_dict(),
        "planner_llm_audit": canonical_value(planner_audit),
    }
    if transition.input_digest != canonical_digest(
        transition_input
    ) or transition.output_digest != canonical_digest(transition_output):
        raise EvidenceIntegrityError("single_authority_plan_transition_digest_mismatch")

    return ParsedAuthoritativePlanResult(
        run_id=run_id,
        plan_patch_ref=plan_patch_ref,
        decision_ledger_position=ledger_position,
        decision_refs=tuple(decision_refs),
        authority_context=authority_context,
        planner_proposal=planner_proposal,
        proposal_admission=proposal_admission,
        plan_revision=plan_revision,
        transition=transition,
        authority_refs=canonical_value(expected_refs),
        llm_calls=tuple(canonical_value(llm_calls)),
        checkpoint_events=tuple(canonical_value(checkpoint_events)),
        planner_llm_audit=canonical_value(planner_audit),
        transition_input=canonical_value(transition_input),
        transition_output=canonical_value(transition_output),
    )


def validate_proposal_admission_plan_closure(
    *,
    planner_proposal: PlannerProposal,
    proposal_admission: ProposalAdmissionRecord,
    plan_revision: PlanRevision,
) -> None:
    proposal_items: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for item_kind, items in (
        ("analysis_axis", planner_proposal.auxiliary_axes),
        ("hypothesis", planner_proposal.hypotheses),
        ("priority", planner_proposal.priority_proposals),
        ("assumption", planner_proposal.assumption_proposals),
    ):
        for item in items:
            proposal_items[str(item["proposal_item_id"])] = (
                item_kind,
                item,
            )
    admission_by_ref = {
        str(entry["proposal_item_ref"]): entry
        for entry in proposal_admission.admission_entries
    }
    if set(admission_by_ref) != set(proposal_items):
        raise EvidenceIntegrityError(
            "single_authority_plan_proposal_admission_closure_mismatch"
        )

    plan_axis_ids = {axis.axis_id for axis in plan_revision.analysis_axes}
    axes_by_proposal_ref: dict[str, set[str]] = {}
    for axis in plan_revision.analysis_axes:
        for proposal_ref in axis.proposal_refs:
            axes_by_proposal_ref.setdefault(proposal_ref, set()).add(axis.axis_id)

    admitted_provenance_refs: set[str] = set()
    admitted_assumption_refs: set[str] = set()
    nonadmitted_refs: set[str] = set()
    for item_ref, (item_kind, proposal_item) in proposal_items.items():
        entry = admission_by_ref[item_ref]
        status = str(entry["status"])
        normalized_ref = entry["normalized_execution_ref"]
        if str(entry["item_kind"]) != item_kind:
            raise EvidenceIntegrityError(
                "single_authority_plan_proposal_admission_closure_mismatch"
            )
        expected_execution_ref = _proposal_execution_ref(
            item_kind=item_kind,
            item_ref=item_ref,
            proposal_item=proposal_item,
        )
        if status == "admitted":
            if normalized_ref != expected_execution_ref:
                raise EvidenceIntegrityError(
                    "single_authority_plan_proposal_admission_closure_mismatch"
                )
            if item_kind == "analysis_axis":
                admitted_provenance_refs.add(item_ref)
                expected_axes = {str(proposal_item["axis_id"])}
                if axes_by_proposal_ref.get(item_ref, set()) != expected_axes:
                    raise EvidenceIntegrityError(
                        "single_authority_plan_proposal_admission_closure_mismatch"
                    )
            elif item_kind == "hypothesis":
                admitted_provenance_refs.add(item_ref)
                expected_axes = {
                    str(axis_id) for axis_id in proposal_item["requested_axis_ids"]
                }
                if (
                    not expected_axes
                    or axes_by_proposal_ref.get(item_ref, set()) != expected_axes
                ):
                    raise EvidenceIntegrityError(
                        "single_authority_plan_proposal_admission_closure_mismatch"
                    )
            elif item_kind == "priority":
                if normalized_ref not in plan_axis_ids:
                    raise EvidenceIntegrityError(
                        "single_authority_plan_proposal_admission_closure_mismatch"
                    )
            else:
                admitted_assumption_refs.add(str(normalized_ref))
        else:
            nonadmitted_refs.add(item_ref)
            if item_kind == "assumption":
                nonadmitted_refs.add(f"assumption:{item_ref}")

    plan_proposal_refs = set(axes_by_proposal_ref)
    if (
        plan_proposal_refs != admitted_provenance_refs
        or set(plan_revision.assumption_refs) != admitted_assumption_refs
        or plan_proposal_refs.intersection(nonadmitted_refs)
        or set(plan_revision.assumption_refs).intersection(nonadmitted_refs)
    ):
        raise EvidenceIntegrityError(
            "single_authority_plan_proposal_admission_closure_mismatch"
        )


def _proposal_execution_ref(
    *,
    item_kind: str,
    item_ref: str,
    proposal_item: Mapping[str, Any],
) -> str:
    if item_kind == "analysis_axis":
        return str(proposal_item["axis_id"])
    if item_kind == "hypothesis":
        return f"hypothesis:{item_ref}"
    if item_kind == "priority":
        return str(proposal_item["target_ref"])
    return f"assumption:{item_ref}"


def _is_required_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _planner_task_for_transition(transition: DurableTransition) -> str:
    task_by_node = {
        "compile_authoritative_plan": "single_authority_plan_proposal",
        "compile_plan_patch": "single_authority_plan_patch_proposal",
    }
    task = task_by_node.get(transition.node_name)
    if task is None:
        raise EvidenceIntegrityError("planner_provider_audit_invalid")
    return task


def _planner_audit_identity_matches(
    planner_audit: Mapping[str, Any],
    *,
    planner_proposal: PlannerProposal,
    transition: DurableTransition,
) -> bool:
    raw_response = planner_audit.get("raw_response_content")
    return (
        planner_audit.get("task") == _planner_task_for_transition(transition)
        and planner_audit.get("prompt_version") == planner_proposal.prompt_version
        and planner_audit.get("model") == planner_proposal.model_version
        and planner_audit.get("provider") == transition.provider_ref
        and planner_audit.get("model") == transition.model_ref
        and _is_required_string(raw_response)
        and planner_proposal.raw_provider_response_ref
        == planner_raw_response_ref(raw_response)
    )
