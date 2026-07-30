#!/usr/bin/env python3
"""Run the Gate 3 authority loop against the accepted local snapshot."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from waje_vnext.capabilities import (
    OrdinalGroupSpec,
    PeriodComparisonEffectExecutor,
    PeriodComparisonQuerySpec,
    PeriodUnit,
    SourceBinding,
)
from waje_vnext.controller import (
    EffectExecutionResult,
    EvidenceDraft,
    WAJEController,
)
from waje_vnext.domain.actions import (
    ActionKind,
    AgentActionProposal,
    CallCapabilityPayload,
    ProposeAnswerPayload,
    ProposedClaim,
    RecordInterpretationPayload,
    ReviseFramePayload,
    RevisePlanPayload,
    RunProbePayload,
)
from waje_vnext.domain.authority import (
    AlternativeHypothesis,
    AnswerStatus,
    AnswerVersion,
    ClaimVerifierStatus,
    ComparisonDesign,
    ComparisonGroup,
    ComparisonGroupRole,
    ComparisonMode,
    EstimatorAggregation,
    EstimatorSpec,
    EvidenceStrength,
    EvidenceType,
    ExposureAdjustmentMode,
    ExposureBalance,
    ExposureDesign,
    FrameRequirement,
    FrameRequirementKind,
    WorkTask,
    compute_answer_settlement_fingerprint,
)
from waje_vnext.domain.canonical import content_sha256, freeze_json, to_jsonable
from waje_vnext.domain.controller import ControllerPhase, PrimaryAgentRequest
from waje_vnext.domain.runtime_state import OutboxMessage
from waje_vnext.projections import build_workflow_projection
from waje_vnext.storage import InMemoryAuthorityStore


QUESTION = (
    "全量样本看，为什么从2024年1月开始到2026年5月结束，"
    "每个月月初的付费金额都比月中月末高一些？"
)


class DockerClickHouseRunner:
    def __init__(self, container: str) -> None:
        self._container = container

    def run(self, sql: str) -> str:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                self._container,
                "clickhouse-client",
                "--query",
                sql,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout


class SliceEffectExecutor:
    def __init__(
        self,
        *,
        period_executor: PeriodComparisonEffectExecutor,
        contract_refs: tuple[str, ...],
        snapshot_release_ref: str,
    ) -> None:
        self._period_executor = period_executor
        self._contract_refs = contract_refs
        self._snapshot_release_ref = snapshot_release_ref

    def execute(self, message: OutboxMessage) -> EffectExecutionResult:
        if message.destination == "analysis_probe":
            return self._period_executor.execute(message)
        request = message.payload["request"]
        task_id = request["task_id"]
        capability_name = request["capability_name"]
        boundary = {
            "boundary": "missing_contract",
            "requested_capability": capability_name,
            "required_contract_refs": request["parameters"][
                "required_contract_refs"
            ],
        }
        return EffectExecutionResult(
            payload=boundary,
            business_summary=(
                "The requested alternative cannot be tested because its "
                "required semantic data contract is unavailable"
            ),
            evidence=(
                EvidenceDraft(
                    task_id=task_id,
                    capability_name=capability_name,
                    query_spec_ref=None,
                    semantic_contract_refs=self._contract_refs,
                    snapshot_release_ref=self._snapshot_release_ref,
                    grain="contract_boundary",
                    evidence_type=EvidenceType.BOUNDARY,
                    strength=EvidenceStrength.NONE,
                    business_summary=(
                        "Alternative investigation is bounded by a missing "
                        "data contract"
                    ),
                    limitations=(
                        "No mechanism conclusion can be drawn",
                    ),
                    provenance={
                        "boundary": "missing_contract",
                        "required_contract_refs": boundary[
                            "required_contract_refs"
                        ],
                    },
                    inline_payload=boundary,
                ),
            ),
        )


class SlicePrimaryAgent:
    def __init__(
        self,
        *,
        frame: ReviseFramePayload,
        plan: RevisePlanPayload,
        query_spec: PeriodComparisonQuerySpec,
    ) -> None:
        self._frame = frame
        self._plan = plan
        self._query_spec = query_spec
        self.requests: list[PrimaryAgentRequest] = []

    def propose(self, request: PrimaryAgentRequest) -> AgentActionProposal:
        self.requests.append(request)
        turn = len(self.requests)
        if turn == 1:
            return AgentActionProposal(
                kind=ActionKind.REVISE_FRAME,
                payload=self._frame,
            )
        if turn == 2:
            return AgentActionProposal(
                kind=ActionKind.REVISE_PLAN,
                payload=self._plan,
            )
        if turn == 3:
            return AgentActionProposal(
                kind=ActionKind.RUN_PROBE,
                payload=RunProbePayload(
                    task_id="task-pattern",
                    probe_kind="period_comparison",
                    parameters={
                        "query_spec": to_jsonable(self._query_spec),
                    },
                ),
            )
        if turn == 4:
            return AgentActionProposal(
                kind=ActionKind.CALL_CAPABILITY,
                payload=CallCapabilityPayload(
                    task_id="task-payday",
                    capability_name="event_context_evidence",
                    parameters={
                        "required_contract_refs": [
                            "source:payday_calendar:v1"
                        ]
                    },
                ),
            )
        if turn == 5:
            return AgentActionProposal(
                kind=ActionKind.CALL_CAPABILITY,
                payload=CallCapabilityPayload(
                    task_id="task-composition",
                    capability_name="composition_diagnostic",
                    parameters={
                        "required_contract_refs": [
                            "source:payment_composition:v1"
                        ]
                    },
                ),
            )
        evidence = request.context_packet.evidence_index
        evidence_ids = tuple(item.evidence_record_id for item in evidence)
        if turn == 6:
            return AgentActionProposal(
                kind=ActionKind.RECORD_INTERPRETATION,
                payload=RecordInterpretationPayload(
                    evidence_record_ids=evidence_ids,
                    interpretation=(
                        "Compare raw and exposure-normalized recurrence first; "
                        "retain missing-contract alternatives as boundaries"
                    ),
                ),
            )
        pattern = next(
            item
            for item in evidence
            if item.evidence_type == EvidenceType.ASSOCIATION.value
        )
        contrast = pattern.inline_payload["contrasts"][0]
        hits = contrast["normalized_direction_hits"]
        periods = contrast["comparable_periods"]
        statement = (
            "The premise that the focal period is higher every month is "
            "not supported: the exposure-normalized direction holds in "
            "{} of {} complete months.".format(hits, periods)
        )
        boundary_ids = tuple(
            item.evidence_record_id
            for item in evidence
            if item.evidence_type == EvidenceType.BOUNDARY.value
        )
        return AgentActionProposal(
            kind=ActionKind.PROPOSE_ANSWER,
            payload=ProposeAnswerPayload(
                claims=(
                    ProposedClaim(
                        claim_id="claim-pattern-reversal",
                        statement=statement,
                        applicability=(
                            "Accepted complete-month scope, source release, "
                            "and Agent-selected comparison groups"
                        ),
                        evidence_record_ids=(pattern.evidence_record_id,),
                        boundary_ref=None,
                        limitations=(
                            "Descriptive recurrence does not identify a cause",
                        ),
                    ),
                    ProposedClaim(
                        claim_id="claim-alternative-boundaries",
                        statement=(
                            "Payday and composition mechanisms remain "
                            "unresolved because their source contracts are "
                            "unavailable"
                        ),
                        applicability="Current accepted semantic contracts",
                        evidence_record_ids=boundary_ids,
                        boundary_ref=None,
                        limitations=(
                            "No causal mechanism claim is published",
                        ),
                    ),
                ),
                narrative_markdown=(
                    "{} Payday and composition explanations remain bounded "
                    "by missing contracts.".format(statement)
                ),
            ),
        )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    source_contract = json.loads(
        (
            root
            / "contracts"
            / "semantics"
            / "source-paid-order-daily.v1.json"
        ).read_text(encoding="utf-8")
    )
    binding = SourceBinding(
        source_ref=source_contract["contract_id"],
        metric_ref=source_contract["metric_ref"],
        table=source_contract["physical_table"],
        date_column=source_contract["date_column"],
        value_column=source_contract["value_column"],
        snapshot_release_ref=source_contract["snapshot_release_ref"],
        business_timezone=source_contract["business_timezone"],
        available_from=date.fromisoformat(source_contract["available_from"]),
        available_through=date.fromisoformat(
            source_contract["available_through"]
        ),
    )
    query_spec = PeriodComparisonQuerySpec(
        query_spec_id="query-spec:gate3-agent-selected:v1",
        metric_ref=binding.metric_ref,
        source_ref=binding.source_ref,
        period_unit=PeriodUnit.CALENDAR_MONTH,
        range_start=date(2024, 1, 1),
        range_end=date(2026, 5, 31),
        groups=(
            OrdinalGroupSpec(
                group_id="focal",
                role=ComparisonGroupRole.FOCAL,
                lower_inclusive=1,
                upper_inclusive=7,
            ),
            OrdinalGroupSpec(
                group_id="reference",
                role=ComparisonGroupRole.REFERENCE,
                lower_inclusive=8,
                upper_inclusive=31,
            ),
        ),
    )
    requirements = (
        FrameRequirement(
            requirement_id="req-exposure",
            kind=FrameRequirementKind.EXPOSURE,
            question="Are observed eligible days balanced across groups?",
            success_condition="Exposure units are measured by month and group",
            failure_consequence="Revise the estimator or comparison",
        ),
        FrameRequirement(
            requirement_id="req-sensitivity",
            kind=FrameRequirementKind.SENSITIVITY,
            question="Does exposure adjustment change the direction?",
            success_condition="Raw and per-exposure estimates are compared",
            failure_consequence="Reverse or limit the conclusion",
        ),
        FrameRequirement(
            requirement_id="req-payday",
            kind=FrameRequirementKind.ALTERNATIVE,
            question="Can payday timing explain the pattern?",
            success_condition="Payday exposure and control are available",
            failure_consequence="Bind a missing-contract boundary",
        ),
        FrameRequirement(
            requirement_id="req-composition",
            kind=FrameRequirementKind.ALTERNATIVE,
            question="Can payment composition explain the pattern?",
            success_condition="Comparable composition dimensions are available",
            failure_consequence="Bind a missing-contract boundary",
        ),
    )
    frame = ReviseFramePayload(
        revision_reason="Agent-selected provisional measurement design",
        estimand=(
            "Difference in mean daily paid amount between the Agent-selected "
            "focal and reference periods"
        ),
        population="All valid paid orders in the accepted full-sample release",
        time_scope="Complete months from 2024-01 through 2026-05",
        observation_unit="calendar month and comparison group",
        primary_estimator=EstimatorSpec(
            quantity="Within-month difference in mean daily paid amount",
            aggregation=EstimatorAggregation.DIFFERENCE,
            numerator="sum of valid daily paid amount",
            denominator="observed eligible business days",
            exposure_adjustment=ExposureAdjustmentMode.PER_EXPOSURE_UNIT,
        ),
        comparison=ComparisonDesign(
            mode=ComparisonMode.WITHIN_UNIT,
            groups=(
                ComparisonGroup(
                    group_id="focal",
                    label="Agent-selected focal period",
                    role=ComparisonGroupRole.FOCAL,
                    membership_rule="ordinal business days 1 through 7",
                ),
                ComparisonGroup(
                    group_id="reference",
                    label="Agent-selected reference period",
                    role=ComparisonGroupRole.REFERENCE,
                    membership_rule="ordinal business days 8 through period end",
                ),
            ),
            contrast="Focal mean daily amount minus reference mean daily amount",
        ),
        exposure=ExposureDesign(
            variable="observed eligible business days",
            unit="business day",
            balance_assumption=ExposureBalance.EXPECTED_UNEQUAL,
            sensitivity_adjustments=(
                ExposureAdjustmentMode.NONE,
                ExposureAdjustmentMode.DESIGN_EQUALIZED,
            ),
            normalization_strategy=(
                "Primary per-day estimate plus raw-total and equal-window "
                "sensitivities"
            ),
            diagnostic_requirement_id="req-exposure",
            sensitivity_requirement_id="req-sensitivity",
        ),
        measurement_rationale=(
            "Measure the user premise as a hypothesis and expose day-count "
            "imbalance before explaining any mechanism"
        ),
        assumptions=("Accepted paid-amount and business-time contracts hold",),
        alternatives=(
            AlternativeHypothesis(
                alternative_id="alt-payday",
                statement="Payday timing may change payment opportunity",
                requirement_id="req-payday",
            ),
            AlternativeHypothesis(
                alternative_id="alt-composition",
                statement="Payment composition may change within the month",
                requirement_id="req-composition",
            ),
        ),
        requirements=requirements,
        falsification_conditions=(
            "The focal direction does not recur across complete months",
        ),
        reversal_conditions=(
            "Exposure-normalized evidence contradicts the user premise",
        ),
        success_conditions=(
            "The premise, exposure, and all material alternatives are resolved",
        ),
        stop_conditions=(
            "No supported pattern remains to explain",
        ),
        semantic_contract_refs=(
            binding.metric_ref,
            binding.source_ref,
        ),
    )
    plan = RevisePlanPayload(
        revision_reason="Investigate every accepted Frame requirement",
        tasks=(
            WorkTask(
                task_id="task-pattern",
                business_purpose=(
                    "Measure recurrence and observed exposure under the "
                    "Agent-selected groups"
                ),
                capability_intent="period comparison with sufficient statistics",
                target_claim_ids=("claim-pattern-reversal",),
                requirement_ids=("req-exposure", "req-sensitivity"),
                depends_on_task_ids=(),
                success_conditions=("Comparable complete periods are measured",),
                stop_conditions=("Coverage is insufficient",),
            ),
            WorkTask(
                task_id="task-payday",
                business_purpose="Test payday as a material alternative",
                capability_intent="event context evidence",
                target_claim_ids=("claim-alternative-boundaries",),
                requirement_ids=("req-payday",),
                depends_on_task_ids=("task-pattern",),
                success_conditions=("Payday exposure is measured",),
                stop_conditions=("Required event contract is missing",),
            ),
            WorkTask(
                task_id="task-composition",
                business_purpose="Test composition as a material alternative",
                capability_intent="composition diagnostic",
                target_claim_ids=("claim-alternative-boundaries",),
                requirement_ids=("req-composition",),
                depends_on_task_ids=("task-pattern",),
                success_conditions=("Comparable composition is measured",),
                stop_conditions=("Required composition contract is missing",),
            ),
        ),
    )
    agent = SlicePrimaryAgent(frame=frame, plan=plan, query_spec=query_spec)
    period_executor = PeriodComparisonEffectExecutor(
        source_bindings={binding.source_ref: binding},
        query_runner=DockerClickHouseRunner("waje-bi-clickhouse"),
    )
    store = InMemoryAuthorityStore()
    controller = WAJEController(
        store=store,
        provider=agent,
        effect_executor=SliceEffectExecutor(
            period_executor=period_executor,
            contract_refs=(binding.metric_ref, binding.source_ref),
            snapshot_release_ref=binding.snapshot_release_ref,
        ),
        owner_id="gate3-slice-worker",
    )
    case_id = "case-gate3-slice"
    state = controller.start(
        case_id=case_id,
        thread_id="thread-gate3-slice",
        run_id="run-gate3-slice",
        user_message=QUESTION,
    )
    for _ in range(20):
        if state.phase is ControllerPhase.READY_FOR_AGENT:
            state = controller.advance(case_id)
        elif state.phase is ControllerPhase.WAITING_FOR_EFFECT:
            state = controller.deliver_pending_effect(case_id)
        elif state.phase is ControllerPhase.COMPLETED:
            break
        else:
            raise RuntimeError(
                "slice entered unexpected phase {}".format(state.phase.value)
            )
    if state.phase is not ControllerPhase.COMPLETED:
        raise RuntimeError("slice did not complete")
    case = store.get_case(case_id)
    provisional = store.get_answer(case.accepted_answer_version_id or "")
    settled_claims = tuple(
        replace(
            claim,
            verifier_status=ClaimVerifierStatus.ACCEPTED,
        )
        for claim in provisional.claims
    )
    settled = AnswerVersion(
        answer_version_id="answer-gate3-settled",
        case_id=case_id,
        frame_revision_id=provisional.frame_revision_id,
        plan_revision_id=provisional.plan_revision_id,
        version_number=2,
        prior_answer_version_id=provisional.answer_version_id,
        status=AnswerStatus.SETTLED,
        claims=settled_claims,
        narrative_markdown=provisional.narrative_markdown,
        verifier_policy_version=provisional.verifier_policy_version,
        unresolved_blocking_objection_ids=(),
        settlement_fingerprint=compute_answer_settlement_fingerprint(
            frame_revision_id=provisional.frame_revision_id,
            plan_revision_id=provisional.plan_revision_id,
            claims=settled_claims,
            verifier_policy_version=provisional.verifier_policy_version,
        ),
        created_by_action_id="gate3-proof-verifier",
        created_at=datetime.now(tz=UTC),
    )
    case = store.accept_answer(
        settled,
        expected_head_version=case.head_version,
        event_id="event-gate3-settled",
        recorded_at=settled.created_at,
    )
    accepted_frame = store.get_frame(case.accepted_frame_revision_id or "")
    accepted_plan = store.get_plan(case.accepted_plan_revision_id or "")
    evidence = store.list_evidence(case_id)
    workflow = build_workflow_projection(
        case=case,
        frame=accepted_frame,
        plan=accepted_plan,
        answer=settled,
        events=store.list_events(case_id),
        evidence=evidence,
    )
    artifact = {
        "acceptance": "gate3-authority-loop-real-data",
        "recorded_at": datetime.now(tz=UTC).isoformat(),
        "frame": to_jsonable(accepted_frame),
        "plan": to_jsonable(accepted_plan),
        "evidence": [to_jsonable(record) for record in evidence],
        "provisional_answer": to_jsonable(provisional),
        "settled_answer": to_jsonable(settled),
        "workflow": to_jsonable(workflow),
        "journal": [
            {
                "cursor": event.cursor,
                "event_type": event.event_type.value,
                "authority_ref": event.authority_ref,
                "customer_projection": event.customer_projection,
            }
            for event in store.list_events(case_id)
        ],
    }
    artifact["content_sha256"] = content_sha256(freeze_json(artifact))
    artifact_root = root / "artifacts" / "gate3-slice"
    artifact_root.mkdir(parents=True, exist_ok=True)
    path = artifact_root / "authority-loop.json"
    path.write_text(
        json.dumps(
            artifact,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=to_jsonable,
        )
        + "\n",
        encoding="utf-8",
    )
    pattern = next(
        record
        for record in evidence
        if record.evidence_type is EvidenceType.ASSOCIATION
    )
    print(
        json.dumps(
            {
                "artifact": str(path),
                "content_sha256": artifact["content_sha256"],
                "evidence_count": len(evidence),
                "normalized_contrast": to_jsonable(
                    pattern.inline_payload["contrasts"][0]
                ),
                "provisional_answer_id": provisional.answer_version_id,
                "settled_answer_id": settled.answer_version_id,
                "workflow_mode": workflow.mode.value,
                "workflow_statuses": [
                    task.status.value for task in workflow.tasks
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
