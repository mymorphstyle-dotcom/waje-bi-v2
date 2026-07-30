from __future__ import annotations

import unittest
from dataclasses import replace

from tests.test_gate2_controller import (
    NOW,
    frame_proposal,
    plan_proposal,
)
from waje_vnext.controller import (
    EffectExecutionResult,
    EvidenceDraft,
    ScriptedEffectExecutor,
    WAJEController,
)
from waje_vnext.domain.actions import (
    ActionKind,
    AgentActionProposal,
    RevisePlanPayload,
    RunProbePayload,
)
from waje_vnext.domain.authority import (
    ComparisonGroup,
    ComparisonGroupRole,
    EvidenceStrength,
    EvidenceType,
    ExposureAdjustmentMode,
    ExposureBalance,
)
from waje_vnext.domain.controller import ControllerPhase
from waje_vnext.domain.events import JournalEventType
from waje_vnext.providers import ScriptedPrimaryAgentProvider
from waje_vnext.storage import InMemoryAuthorityStore


def probe_proposal() -> AgentActionProposal:
    return AgentActionProposal(
        kind=ActionKind.RUN_PROBE,
        payload=RunProbePayload(
            task_id="task-pattern",
            probe_kind="period_comparison",
            parameters={
                "comparison_spec_ref": "query-spec:agent-proposed:v1",
            },
        ),
    )


class Gate3MeasurementLoopTest(unittest.TestCase):
    def test_controller_preserves_agent_selected_measurement_designs(self) -> None:
        first = frame_proposal()
        second = replace(
            first,
            payload=replace(
                first.payload,
                comparison=replace(
                    first.payload.comparison,
                    groups=(
                        ComparisonGroup(
                            group_id="early-window",
                            label="Agent proposal A focal group",
                            role=ComparisonGroupRole.FOCAL,
                            membership_rule="ordinal days 1 through 8",
                        ),
                        ComparisonGroup(
                            group_id="later-window",
                            label="Agent proposal A reference group",
                            role=ComparisonGroupRole.REFERENCE,
                            membership_rule="ordinal days 9 through period end",
                        ),
                    ),
                ),
                exposure=replace(
                    first.payload.exposure,
                    balance_assumption=ExposureBalance.EXPECTED_UNEQUAL,
                    normalization_strategy=(
                        "Model amount per observed eligible day"
                    ),
                ),
            ),
        )

        stored = []
        for suffix, proposal in (("a", first), ("b", second)):
            store = InMemoryAuthorityStore()
            controller = WAJEController(
                store=store,
                provider=ScriptedPrimaryAgentProvider((proposal,)),
                effect_executor=ScriptedEffectExecutor(()),
                owner_id="worker-{}".format(suffix),
                clock=lambda: NOW,
            )
            controller.start(
                case_id="case-{}".format(suffix),
                thread_id="thread-{}".format(suffix),
                run_id="run-{}".format(suffix),
                user_message="Compare an agent-defined business period",
            )
            controller.advance("case-{}".format(suffix))
            case = store.get_case("case-{}".format(suffix))
            stored.append(store.get_frame(case.accepted_frame_revision_id or ""))

        self.assertEqual(
            stored[0].comparison.groups,
            first.payload.comparison.groups,
        )
        self.assertEqual(
            stored[1].comparison.groups,
            second.payload.comparison.groups,
        )
        self.assertNotEqual(
            stored[0].exposure.normalization_strategy,
            stored[1].exposure.normalization_strategy,
        )

    def test_plan_must_cover_every_agent_declared_frame_requirement(self) -> None:
        incomplete_task = replace(
            plan_proposal().payload.tasks[0],
            requirement_ids=("req-exposure", "req-sensitivity"),
        )
        incomplete_plan = AgentActionProposal(
            kind=ActionKind.REVISE_PLAN,
            payload=RevisePlanPayload(
                revision_reason="Incomplete investigation",
                tasks=(incomplete_task,),
            ),
        )
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(
                (frame_proposal(), incomplete_plan)
            ),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="coverage-worker",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-coverage",
            thread_id="thread-coverage",
            run_id="run-coverage",
            user_message="Investigate the accepted frame",
        )
        controller.advance("case-coverage")
        rejected = controller.advance("case-coverage")

        self.assertEqual(rejected.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertIsNone(
            store.get_case("case-coverage").accepted_plan_revision_id
        )
        event = next(
            entry
            for entry in reversed(store.list_events("case-coverage"))
            if entry.event_type is JournalEventType.ACTION_REJECTED
        )
        self.assertEqual(
            event.payload["reason_code"],
            "plan_requirement_coverage_mismatch",
        )

    def test_invalid_frame_links_are_rejected_and_returned_to_agent(
        self,
    ) -> None:
        valid = frame_proposal()
        invalid = replace(
            valid,
            payload=replace(
                valid.payload,
                exposure=replace(
                    valid.payload.exposure,
                    diagnostic_requirement_id="req-missing",
                ),
            ),
        )
        store = InMemoryAuthorityStore()
        provider = ScriptedPrimaryAgentProvider((invalid, valid))
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="repair-worker",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-frame-repair",
            thread_id="thread-frame-repair",
            run_id="run-frame-repair",
            user_message="Define a measurement design",
        )

        rejected = controller.advance("case-frame-repair")
        self.assertEqual(rejected.consecutive_rejections, 1)
        self.assertIsNone(
            store.get_case("case-frame-repair").accepted_frame_revision_id
        )
        accepted = controller.advance("case-frame-repair")
        self.assertEqual(accepted.consecutive_rejections, 0)
        rejection_feedback = next(
            event
            for event in provider.requests[-1].context_packet.recent_events
            if event.event_type == JournalEventType.ACTION_REJECTED.value
        )
        self.assertEqual(
            rejection_feedback.agent_result["admission"]["reason_code"],
            "frame_exposure_diagnostic_requirement_invalid",
        )

    def test_unbalanced_unadjusted_frame_requires_adjusted_sensitivity(
        self,
    ) -> None:
        valid = frame_proposal()
        invalid = replace(
            valid,
            payload=replace(
                valid.payload,
                primary_estimator=replace(
                    valid.payload.primary_estimator,
                    exposure_adjustment=ExposureAdjustmentMode.NONE,
                ),
                exposure=replace(
                    valid.payload.exposure,
                    balance_assumption=ExposureBalance.EXPECTED_UNEQUAL,
                    sensitivity_adjustments=(ExposureAdjustmentMode.NONE,),
                ),
            ),
        )
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider((invalid,)),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="exposure-contract-worker",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-exposure-contract",
            thread_id="thread-exposure-contract",
            run_id="run-exposure-contract",
            user_message="Define an exposure-aware measurement",
        )

        state = controller.advance("case-exposure-contract")

        self.assertEqual(state.consecutive_rejections, 1)
        self.assertIsNone(
            store.get_case(
                "case-exposure-contract"
            ).accepted_frame_revision_id
        )

    def test_frame_requires_material_alternative_and_reversal_contract(
        self,
    ) -> None:
        valid = frame_proposal()
        invalid = replace(
            valid,
            payload=replace(valid.payload, alternatives=()),
        )
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider((invalid,)),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="frame-completeness-worker",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-frame-completeness",
            thread_id="thread-frame-completeness",
            run_id="run-frame-completeness",
            user_message="Define the full measurement contract",
        )

        controller.advance("case-frame-completeness")
        rejection = next(
            event
            for event in store.list_events("case-frame-completeness")
            if event.event_type is JournalEventType.ACTION_REJECTED
        )
        self.assertEqual(
            rejection.payload["reason_code"],
            "frame_alternatives_required",
        )

    def test_probe_materializes_evidence_for_agent_driven_frame_revision(
        self,
    ) -> None:
        revised = replace(
            frame_proposal(),
            payload=replace(
                frame_proposal().payload,
                revision_reason=(
                    "Observed exposure imbalance changes the estimator"
                ),
                exposure=replace(
                    frame_proposal().payload.exposure,
                    balance_assumption=ExposureBalance.EXPECTED_UNEQUAL,
                    normalization_strategy=(
                        "Use observed exposure units in every comparison"
                    ),
                ),
            ),
        )
        store = InMemoryAuthorityStore()
        provider = ScriptedPrimaryAgentProvider(
            (
                frame_proposal(),
                plan_proposal(),
                probe_proposal(),
                revised,
            )
        )
        effects = ScriptedEffectExecutor(
            (
                EffectExecutionResult(
                    payload={
                        "group_exposure_units": {
                            "focal": 10,
                            "reference": 19,
                        }
                    },
                    business_summary=(
                        "Observed exposure differs across comparison groups"
                    ),
                    evidence=(
                        EvidenceDraft(
                            task_id="task-pattern",
                            capability_name="period_comparison",
                            query_spec_ref="query-spec:agent-proposed:v1",
                            semantic_contract_refs=(
                                "metric:paid_amount:v1",
                            ),
                            snapshot_release_ref="release:test:v1",
                            grain="comparison_group_by_calendar_month",
                            evidence_type=EvidenceType.DATA_QUALITY,
                            strength=EvidenceStrength.QUANTIFIED,
                            business_summary=(
                                "The proposed groups have unequal observed "
                                "exposure units"
                            ),
                            limitations=(),
                            provenance={
                                "comparison_spec_ref": (
                                    "query-spec:agent-proposed:v1"
                                )
                            },
                            inline_payload={
                                "group_exposure_units": {
                                    "focal": 10,
                                    "reference": 19,
                                }
                            },
                        ),
                    ),
                ),
            )
        )
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=effects,
            owner_id="evidence-worker",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-evidence-loop",
            thread_id="thread-evidence-loop",
            run_id="run-evidence-loop",
            user_message="Test the business premise and its exposure",
        )
        controller.advance("case-evidence-loop")
        controller.advance("case-evidence-loop")
        waiting = controller.advance("case-evidence-loop")
        self.assertEqual(waiting.phase, ControllerPhase.WAITING_FOR_EFFECT)
        ready = controller.deliver_pending_effect("case-evidence-loop")
        self.assertEqual(ready.phase, ControllerPhase.READY_FOR_AGENT)
        evidence = store.list_evidence("case-evidence-loop")
        self.assertEqual(len(evidence), 1)
        self.assertEqual(
            evidence[0].inline_payload["group_exposure_units"]["reference"],
            19,
        )

        controller.advance("case-evidence-loop")
        revision_request = provider.requests[-1].context_packet
        self.assertEqual(
            revision_request.evidence_index[0].inline_payload[
                "group_exposure_units"
            ]["reference"],
            19,
        )
        completed_event = next(
            event
            for event in revision_request.recent_events
            if event.event_type
            == JournalEventType.EFFECT_COMPLETED.value
        )
        self.assertEqual(
            completed_event.agent_result["payload"][
                "group_exposure_units"
            ]["focal"],
            10,
        )
        case = store.get_case("case-evidence-loop")
        frame = store.get_frame(case.accepted_frame_revision_id or "")
        self.assertEqual(frame.revision_number, 2)
        self.assertEqual(
            frame.exposure.balance_assumption,
            ExposureBalance.EXPECTED_UNEQUAL,
        )
        self.assertIsNone(case.accepted_plan_revision_id)


if __name__ == "__main__":
    unittest.main()
