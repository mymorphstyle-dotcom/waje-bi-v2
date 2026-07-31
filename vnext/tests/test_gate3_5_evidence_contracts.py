from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date

import gate1_fixtures
import test_gate3_4_plan_query_continuity as gate34_fixtures
import test_gate3_3_measurement_resolver as resolver_fixtures
from gate1_fixtures import NOW
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.async_runtime import OperationIdentity
from waje_vnext.domain.evidence import (
    EstimatePayload,
    EvidenceAdmissionProfile,
    EvidenceAdmissionStatus,
    EvidenceValidityStatus,
    ExecutionProvenanceKind,
    InlineResultMaterial,
    ObligationSatisfactionStatus,
    ResultMaterialKind,
    build_capability_result_envelope,
    build_capability_result_receipt,
    build_conformance_execution_provenance,
    build_evidence_admission,
    build_evidence_record,
    build_evidence_use_binding,
    build_evidence_validity_successor,
    build_initial_evidence_validity,
    build_obligation_satisfaction,
    build_physical_query_execution_provenance,
    capability_result_receipt_payload_sha256,
    validate_capability_result_envelope,
    validate_capability_result_receipt,
    validate_evidence_admission,
    validate_evidence_record_authority,
    validate_evidence_use_binding,
    validate_evidence_validity,
    validate_obligation_satisfaction,
)
from waje_vnext.domain.measurement import (
    ClaimStrengthCeiling,
)
from waje_vnext.domain.planning import (
    build_conformance_execution_spec,
    build_logical_execution_attempt,
)


class Gate35EvidenceContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture = gate34_fixtures.Gate34PlanQueryContinuityTest()
        fixture.setUp()
        self.store = fixture.store
        self.case = fixture.case
        self.frame = fixture.frame
        self.bundle = fixture.bundle
        self.binding = self.bundle.query_bindings[0]
        self.run_id = "run:gate3-5"
        self.schedule_id = "schedule:gate3-5"
        self.dispatch_record_id = "dispatch-record:gate3-5"
        self.outbox_message_id = "outbox:gate3-5"
        self.answer_candidate_id = content_sha256(
            {"kind": "answer-candidate", "run_id": self.run_id}
        )
        self.proposal_claim_key = "claim:window-contrast:direction"
        self.obligation = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )[0]
        self.outcome = self.store.get_measurement_resolution(
            self.binding.resolution_outcome_id
        )
        self.scope = next(
            scope
            for scope in self.frame.measurement_design.scopes
            if scope.scope_id == self.binding.requirement_binding.scope_id
        )
        self.snapshot = self.store.get_authority_snapshot(
            self.case.case_id
        )
        self.spec = build_conformance_execution_spec(
            query_binding=self.binding,
            fixture_ref=(
                "waje-vnext://conformance-fixture/"
                "gate3-5-payment-window.v1"
            ),
            fixture_content_sha256=content_sha256(
                {"fixture": "gate3-5-payment-window.v1"}
            ),
            result_contract_ref=(
                "waje-vnext://result-contract/"
                "aggregate-contrast.v1"
            ),
            execution_policy_ref=(
                "waje-vnext://execution-policy/conformance.v1"
            ),
            created_at=NOW,
        )
        self.attempt = build_logical_execution_attempt(
            spec=self.spec,
            authority_snapshot=self.snapshot,
            attempt_number=1,
            prior_attempt=None,
            retry_reason_code=None,
            requested_at=NOW,
        )
        self.provenance = build_conformance_execution_provenance(
            binding=self.binding,
            spec=self.spec,
            attempt=self.attempt,
            current_authority=self.snapshot,
        )
        self.estimate = EstimatePayload(
            estimate_schema_ref=(
                "waje-vnext://estimate-schema/window-contrast.v1"
            ),
            estimate_content_sha256=content_sha256(
                {
                    "left_daily_average": "110.00",
                    "right_daily_average": "100.00",
                    "absolute_change": "10.00",
                }
            ),
            uncertainty_schema_ref=None,
            uncertainty_content_sha256=None,
        )
        self.result = InlineResultMaterial(
            kind=ResultMaterialKind.INLINE,
            payload_content_sha256=content_sha256(
                {"rows": "bounded-conformance-result"}
            ),
            schema_ref=(
                "waje-vnext://result-schema/window-contrast.v1"
            ),
            row_count=2,
            byte_count=256,
        )
        windows = self.binding.resolved_measurement_instance.windows
        exposures = tuple(
            fact for window in windows for fact in window.exposure_facts
        )
        self.evidence = build_evidence_record(
            run_id=self.run_id,
            profile=EvidenceAdmissionProfile.CONFORMANCE,
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
            execution_provenance=self.provenance,
            actual_scope=self.scope,
            actual_windows=windows,
            actual_exposure_facts=exposures,
            evidence_type_ref=self.obligation.evidence_type_refs[0],
            evidence_strength=ClaimStrengthCeiling.DESCRIPTIVE,
            estimate=self.estimate,
            result_material=self.result,
            business_summary=(
                "目标窗口的有效观察日归一化金额高于对照窗口。"
            ),
            limitation_refs=(
                "limitation:conformance-fixture-only",
            ),
            produced_at=NOW,
        )
        self.envelope = build_capability_result_envelope(
            evidence_record=self.evidence,
            run_id=self.run_id,
            schedule_id=self.schedule_id,
            dispatch_record_id=self.dispatch_record_id,
            outbox_message_id=self.outbox_message_id,
            logical_execution_attempt_id=(
                self.attempt.logical_execution_attempt_id
            ),
            logical_execution_attempt_content_sha256=(
                self.attempt.content_sha256
            ),
            produced_at=NOW,
        )
        self.operation = OperationIdentity(
            operation_id="operation:gate3-5-capability-result",
            idempotency_key="idempotency:gate3-5-capability-result",
            causation_id=self.outbox_message_id,
            correlation_id=self.run_id,
            authority_revision=self.snapshot.mailbox_authority_epoch,
            payload_sha256=capability_result_receipt_payload_sha256(
                self.envelope
            ),
        )
        self.receipt = build_capability_result_receipt(
            envelope=self.envelope,
            operation_identity=self.operation,
            delivery_owner_id="gate35-contract-worker",
            delivery_fencing_token=1,
            received_at=NOW,
        )
        self.admission = build_evidence_admission(
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
            envelope=self.envelope,
            receipt=self.receipt,
            plan_adoption=self.bundle.adoption,
            expected_scope=self.scope,
            current_authority=self.snapshot,
            profile=EvidenceAdmissionProfile.CONFORMANCE,
            admitted_at=NOW,
        )
        self.validity = build_initial_evidence_validity(
            admission=self.admission,
            recorded_at=NOW,
        )
        self.use = build_evidence_use_binding(
            evidence=self.evidence,
            admission=self.admission,
            validity=self.validity,
            binding=self.binding,
            answer_candidate_id=self.answer_candidate_id,
            proposal_claim_key=self.proposal_claim_key,
            claim_scope=self.scope,
            requested_claim_strength=ClaimStrengthCeiling.DESCRIPTIVE,
            bound_at=NOW,
        )

    def _build_scope_rejected_admission(self):
        narrower_scope = replace(
            self.scope,
            scope_id="scope:narrow-channel-only",
            predicate_ref="predicate:one-channel-only",
        )
        evidence = build_evidence_record(
            run_id=self.run_id,
            profile=EvidenceAdmissionProfile.CONFORMANCE,
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
            execution_provenance=self.provenance,
            actual_scope=narrower_scope,
            actual_windows=self.evidence.actual_windows,
            actual_exposure_facts=self.evidence.actual_exposure_facts,
            evidence_type_ref=self.evidence.evidence_type_ref,
            evidence_strength=self.evidence.evidence_strength,
            estimate=self.estimate,
            result_material=self.result,
            business_summary=self.evidence.business_summary,
            limitation_refs=self.evidence.limitation_refs,
            produced_at=NOW,
        )
        envelope = build_capability_result_envelope(
            evidence_record=evidence,
            run_id=self.run_id,
            schedule_id=self.schedule_id,
            dispatch_record_id=self.dispatch_record_id,
            outbox_message_id=self.outbox_message_id,
            logical_execution_attempt_id=(
                self.attempt.logical_execution_attempt_id
            ),
            logical_execution_attempt_content_sha256=(
                self.attempt.content_sha256
            ),
            produced_at=NOW,
        )
        receipt = build_capability_result_receipt(
            envelope=envelope,
            operation_identity=replace(
                self.operation,
                payload_sha256=(
                    capability_result_receipt_payload_sha256(envelope)
                ),
            ),
            delivery_owner_id="gate35-contract-worker",
            delivery_fencing_token=1,
            received_at=NOW,
        )
        admission = build_evidence_admission(
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
            envelope=envelope,
            receipt=receipt,
            plan_adoption=self.bundle.adoption,
            expected_scope=self.scope,
            current_authority=self.snapshot,
            profile=EvidenceAdmissionProfile.CONFORMANCE,
            admitted_at=NOW,
        )
        return evidence, admission

    def test_complete_chain_is_canonical_and_replayable(self) -> None:
        self.assertEqual(
            self.provenance.kind,
            ExecutionProvenanceKind.CONFORMANCE,
        )
        self.assertEqual(
            self.admission.status,
            EvidenceAdmissionStatus.ACCEPTED,
        )
        self.assertEqual(
            self.validity.status,
            EvidenceValidityStatus.ADMITTED_VALID,
        )
        validate_evidence_record_authority(
            record=self.evidence,
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
        )
        validate_capability_result_envelope(self.envelope)
        validate_capability_result_receipt(
            receipt=self.receipt,
            envelope=self.envelope,
        )
        validate_evidence_admission(
            admission=self.admission,
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
            envelope=self.envelope,
            receipt=self.receipt,
            plan_adoption=self.bundle.adoption,
            expected_scope=self.scope,
            current_authority=self.snapshot,
        )
        validate_evidence_validity(self.validity)
        validate_evidence_use_binding(
            use=self.use,
            evidence=self.evidence,
            admission=self.admission,
            validity=self.validity,
            binding=self.binding,
        )
        satisfaction = build_obligation_satisfaction(
            obligation=self.obligation,
            admissions=(self.admission,),
            validities=(self.validity,),
            boundary_outcome=None,
            prior=None,
            recorded_at=NOW,
        )
        self.assertEqual(
            satisfaction.status,
            ObligationSatisfactionStatus.SATISFIED,
        )
        validate_obligation_satisfaction(satisfaction, prior=None)

    def test_ids_are_derived_from_material_and_cannot_be_forged(self) -> None:
        forged_evidence = replace(
            self.evidence,
            evidence_record_id="0" * 64,
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_evidence_record_authority(
                record=forged_evidence,
                binding=self.binding,
                obligation=self.obligation,
                outcome=self.outcome,
            )

        forged_envelope = replace(
            self.envelope,
            capability_result_envelope_id="1" * 64,
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_capability_result_envelope(forged_envelope)

        forged_receipt = replace(
            self.receipt,
            capability_result_receipt_id="2" * 64,
        )
        with self.assertRaisesRegex(ValueError, "system-derived"):
            validate_capability_result_receipt(
                receipt=forged_receipt,
                envelope=self.envelope,
            )

        forged_admission = replace(
            self.admission,
            evidence_admission_id="3" * 64,
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_evidence_admission(
                admission=forged_admission,
                binding=self.binding,
                obligation=self.obligation,
                outcome=self.outcome,
                envelope=self.envelope,
                receipt=self.receipt,
                plan_adoption=self.bundle.adoption,
                expected_scope=self.scope,
                current_authority=self.snapshot,
            )

    def test_async_runtime_identity_mutations_are_rejected(self) -> None:
        envelope_mutations = (
            replace(self.envelope, run_id="run:other"),
            replace(self.envelope, schedule_id="schedule:other"),
            replace(
                self.envelope,
                dispatch_record_id="dispatch-record:other",
            ),
            replace(
                self.envelope,
                outbox_message_id="outbox:other",
            ),
            replace(
                self.envelope,
                logical_execution_attempt_id="f" * 64,
            ),
            replace(
                self.envelope,
                logical_execution_attempt_content_sha256="e" * 64,
            ),
        )
        for mutated in envelope_mutations:
            with self.subTest(field=mutated):
                with self.assertRaises(ValueError):
                    validate_capability_result_envelope(mutated)

        with self.assertRaisesRegex(ValueError, "operation identity"):
            replace(
                self.receipt,
                idempotency_key="idempotency:forged",
            )
        with self.assertRaisesRegex(ValueError, "bind run"):
            replace(
                self.receipt,
                operation_identity=replace(
                    self.operation,
                    correlation_id="run:other",
                ),
                correlation_id="run:other",
            )
        with self.assertRaisesRegex(ValueError, "bind run"):
            replace(
                self.receipt,
                operation_identity=replace(
                    self.operation,
                    causation_id="outbox:other",
                ),
            )
        forged_receipt = replace(
            self.receipt,
            schedule_id="schedule:forged",
        )
        with self.assertRaisesRegex(ValueError, "system-derived"):
            validate_capability_result_receipt(
                receipt=forged_receipt,
                envelope=self.envelope,
            )

    def test_data_context_and_admission_proof_mutations_are_rejected(
        self,
    ) -> None:
        changed_context = replace(
            self.evidence.data_context,
            snapshot_release_ref="release:forged",
        )
        with self.assertRaisesRegex(ValueError, "data context"):
            validate_evidence_record_authority(
                record=replace(
                    self.evidence,
                    data_context=changed_context,
                ),
                binding=self.binding,
                obligation=self.obligation,
                outcome=self.outcome,
            )
        for field_name in (
            "window_proof_sha256",
            "exposure_proof_sha256",
            "unit_proof_sha256",
            "grain_proof_sha256",
            "data_version_proof_sha256",
        ):
            with self.subTest(field=field_name):
                mutated = replace(
                    self.admission,
                    **{field_name: "f" * 64},
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "policy-derived",
                ):
                    validate_evidence_admission(
                        admission=mutated,
                        binding=self.binding,
                        obligation=self.obligation,
                        outcome=self.outcome,
                        envelope=self.envelope,
                        receipt=self.receipt,
                        plan_adoption=self.bundle.adoption,
                        expected_scope=self.scope,
                        current_authority=self.snapshot,
                    )
        forged_adoption = replace(
            self.bundle.adoption,
            plan_adoption_id="d" * 64,
        )
        with self.assertRaisesRegex(ValueError, "Plan adoption identity"):
            build_evidence_admission(
                binding=self.binding,
                obligation=self.obligation,
                outcome=self.outcome,
                envelope=self.envelope,
                receipt=self.receipt,
                plan_adoption=forged_adoption,
                expected_scope=self.scope,
                current_authority=self.snapshot,
                profile=EvidenceAdmissionProfile.CONFORMANCE,
                admitted_at=NOW,
            )

    def test_admission_identity_is_stable_across_sibling_completion_order(
        self,
    ) -> None:
        after_sibling_completion = replace(
            self.snapshot,
            head_version=self.snapshot.head_version + 2,
            obligation_state_version=(
                self.snapshot.obligation_state_version + 1
            ),
            evidence_admission_state_version=(
                self.snapshot.evidence_admission_state_version + 1
            ),
            contradiction_state_version=(
                self.snapshot.contradiction_state_version + 1
            ),
        )
        reordered = build_evidence_admission(
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
            envelope=self.envelope,
            receipt=self.receipt,
            plan_adoption=self.bundle.adoption,
            expected_scope=self.scope,
            current_authority=after_sibling_completion,
            profile=EvidenceAdmissionProfile.CONFORMANCE,
            admitted_at=NOW,
        )
        self.assertEqual(
            reordered.evidence_admission_id,
            self.admission.evidence_admission_id,
        )
        self.assertEqual(
            reordered.authority_fence,
            self.admission.authority_fence,
        )
        self.assertNotEqual(
            reordered.authority_snapshot_content_sha256,
            self.admission.authority_snapshot_content_sha256,
        )
        validate_evidence_admission(
            admission=self.admission,
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
            envelope=self.envelope,
            receipt=self.receipt,
            plan_adoption=self.bundle.adoption,
            expected_scope=self.scope,
            current_authority=after_sibling_completion,
        )
        corrected_authority = replace(
            after_sibling_completion,
            mailbox_authority_epoch=(
                after_sibling_completion.mailbox_authority_epoch + 1
            ),
        )
        stale = build_evidence_admission(
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
            envelope=self.envelope,
            receipt=self.receipt,
            plan_adoption=self.bundle.adoption,
            expected_scope=self.scope,
            current_authority=corrected_authority,
            profile=EvidenceAdmissionProfile.CONFORMANCE,
            admitted_at=NOW,
        )
        self.assertEqual(
            stale.status,
            EvidenceAdmissionStatus.REJECTED,
        )
        self.assertIn("stale_authority", stale.reason_codes)
        self.assertNotEqual(
            stale.evidence_admission_id,
            self.admission.evidence_admission_id,
        )

    def test_answer_candidate_and_claim_key_are_sealed(self) -> None:
        with self.assertRaisesRegex(ValueError, "answer_candidate_id"):
            build_evidence_use_binding(
                evidence=self.evidence,
                admission=self.admission,
                validity=self.validity,
                binding=self.binding,
                answer_candidate_id="candidate:caller-invented",
                proposal_claim_key=self.proposal_claim_key,
                claim_scope=self.scope,
                requested_claim_strength=(
                    ClaimStrengthCeiling.DESCRIPTIVE
                ),
                bound_at=NOW,
            )
        mutated = replace(
            self.use,
            proposal_claim_key="claim:other",
        )
        with self.assertRaisesRegex(ValueError, "system-derived"):
            validate_evidence_use_binding(
                use=mutated,
                evidence=self.evidence,
                admission=self.admission,
                validity=self.validity,
                binding=self.binding,
            )

    def test_window_and_exposure_authority_drift_is_rejected(self) -> None:
        missing_window = replace(self.evidence, actual_windows=())
        with self.assertRaisesRegex(ValueError, "windows"):
            validate_evidence_record_authority(
                record=missing_window,
                binding=self.binding,
                obligation=self.obligation,
                outcome=self.outcome,
            )

        shortened_exposure = replace(
            self.evidence,
            actual_exposure_facts=self.evidence.actual_exposure_facts[:1],
        )
        with self.assertRaisesRegex(ValueError, "exposure"):
            validate_evidence_record_authority(
                record=shortened_exposure,
                binding=self.binding,
                obligation=self.obligation,
                outcome=self.outcome,
            )

        changed_resolution = replace(
            self.evidence,
            resolution_id="f" * 64,
        )
        with self.assertRaisesRegex(ValueError, "authority identity"):
            validate_evidence_record_authority(
                record=changed_resolution,
                binding=self.binding,
                obligation=self.obligation,
                outcome=self.outcome,
            )

    def test_identity_type_grain_unit_and_realm_mutations_fail(self) -> None:
        mutations = (
            (
                replace(self.evidence, query_binding_id="a" * 64),
                "authority identity",
            ),
            (
                replace(
                    self.evidence,
                    evidence_type_ref="evidence:unowned-type",
                ),
                "unowned evidence type",
            ),
            (
                replace(
                    self.evidence,
                    actual_grain_ref="grain:order",
                ),
                "internally inconsistent",
            ),
            (
                replace(
                    self.evidence,
                    actual_unit_ref="currency:CNY",
                ),
                "internally inconsistent",
            ),
        )
        for mutated, expected_message in mutations:
            with self.subTest(expected_message=expected_message):
                with self.assertRaisesRegex(
                    ValueError,
                    expected_message,
                ):
                    validate_evidence_record_authority(
                        record=mutated,
                        binding=self.binding,
                        obligation=self.obligation,
                        outcome=self.outcome,
                    )
        with self.assertRaisesRegex(ValueError, "wrong tag"):
            replace(
                self.provenance,
                kind=ExecutionProvenanceKind.PHYSICAL_QUERY,
            )

    def test_strength_below_requirement_is_rejected_at_admission(self) -> None:
        evidence = build_evidence_record(
            run_id=self.run_id,
            profile=EvidenceAdmissionProfile.CONFORMANCE,
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
            execution_provenance=self.provenance,
            actual_scope=self.scope,
            actual_windows=self.evidence.actual_windows,
            actual_exposure_facts=self.evidence.actual_exposure_facts,
            evidence_type_ref=self.evidence.evidence_type_ref,
            evidence_strength=ClaimStrengthCeiling.BOUNDARY_ONLY,
            estimate=self.estimate,
            result_material=self.result,
            business_summary=self.evidence.business_summary,
            limitation_refs=self.evidence.limitation_refs,
            produced_at=NOW,
        )
        envelope = build_capability_result_envelope(
            evidence_record=evidence,
            run_id=self.run_id,
            schedule_id=self.schedule_id,
            dispatch_record_id=self.dispatch_record_id,
            outbox_message_id=self.outbox_message_id,
            logical_execution_attempt_id=(
                self.attempt.logical_execution_attempt_id
            ),
            logical_execution_attempt_content_sha256=(
                self.attempt.content_sha256
            ),
            produced_at=NOW,
        )
        receipt = build_capability_result_receipt(
            envelope=envelope,
            operation_identity=replace(
                self.operation,
                payload_sha256=(
                    capability_result_receipt_payload_sha256(envelope)
                ),
            ),
            delivery_owner_id="gate35-contract-worker",
            delivery_fencing_token=1,
            received_at=NOW,
        )
        admission = build_evidence_admission(
            binding=self.binding,
            obligation=self.obligation,
            outcome=self.outcome,
            envelope=envelope,
            receipt=receipt,
            plan_adoption=self.bundle.adoption,
            expected_scope=self.scope,
            current_authority=self.snapshot,
            profile=EvidenceAdmissionProfile.CONFORMANCE,
            admitted_at=NOW,
        )
        self.assertEqual(
            admission.status,
            EvidenceAdmissionStatus.REJECTED,
        )
        self.assertIn(
            "insufficient_strength",
            admission.reason_codes,
        )

    def test_scope_relation_rejects_unproved_scope_masquerade(self) -> None:
        evidence, admission = self._build_scope_rejected_admission()
        self.assertEqual(
            admission.status,
            EvidenceAdmissionStatus.REJECTED,
        )
        self.assertIn("scope_not_covered", admission.reason_codes)
        validity = build_initial_evidence_validity(
            admission=admission,
            recorded_at=NOW,
        )
        self.assertEqual(
            validity.status,
            EvidenceValidityStatus.NEVER_ADMITTED,
        )
        with self.assertRaisesRegex(ValueError, "rejected evidence"):
            build_evidence_use_binding(
                evidence=evidence,
                admission=admission,
                validity=validity,
                binding=self.binding,
                answer_candidate_id=self.answer_candidate_id,
                proposal_claim_key=self.proposal_claim_key,
                claim_scope=self.scope,
                requested_claim_strength=(
                    ClaimStrengthCeiling.DESCRIPTIVE
                ),
                bound_at=NOW,
            )

    def test_production_provenance_is_fail_closed_by_default(self) -> None:
        digest = content_sha256({"physical": "placeholder"})
        with self.assertRaisesRegex(ValueError, "disabled"):
            build_physical_query_execution_provenance(
                logical_execution_id=digest,
                binding=self.binding,
                query_spec_id=digest,
                query_spec_content_sha256=digest,
                capability_invocation_id=digest,
                capability_invocation_content_sha256=digest,
                provider_receipt_id=digest,
                provider_receipt_content_sha256=digest,
                compiler_contract_ref=(
                    "waje-vnext://query-compiler/clickhouse.v1"
                ),
            )

        production = build_physical_query_execution_provenance(
            logical_execution_id=digest,
            binding=self.binding,
            query_spec_id=digest,
            query_spec_content_sha256=digest,
            capability_invocation_id=digest,
            capability_invocation_content_sha256=digest,
            provider_receipt_id=digest,
            provider_receipt_content_sha256=digest,
            compiler_contract_ref=(
                "waje-vnext://query-compiler/clickhouse.v1"
            ),
            production_profile_enabled=True,
        )
        with self.assertRaisesRegex(ValueError, "disabled"):
            build_evidence_record(
                run_id=self.run_id,
                profile=EvidenceAdmissionProfile.PRODUCTION,
                binding=self.binding,
                obligation=self.obligation,
                outcome=self.outcome,
                execution_provenance=production,
                actual_scope=self.scope,
                actual_windows=self.evidence.actual_windows,
                actual_exposure_facts=self.evidence.actual_exposure_facts,
                evidence_type_ref=self.evidence.evidence_type_ref,
                evidence_strength=self.evidence.evidence_strength,
                estimate=self.estimate,
                result_material=self.result,
                business_summary=self.evidence.business_summary,
                limitation_refs=self.evidence.limitation_refs,
                produced_at=NOW,
            )

    def test_strength_ceiling_blocks_overclaim(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds"):
            build_evidence_use_binding(
                evidence=self.evidence,
                admission=self.admission,
                validity=self.validity,
                binding=self.binding,
                answer_candidate_id=self.answer_candidate_id,
                proposal_claim_key=self.proposal_claim_key,
                claim_scope=self.scope,
                requested_claim_strength=ClaimStrengthCeiling.CAUSAL,
                bound_at=NOW,
            )

    def test_validity_is_append_only_with_closed_terminal_states(self) -> None:
        revoked = build_evidence_validity_successor(
            prior=self.validity,
            status=EvidenceValidityStatus.REVOKED,
            reason_code="source_contract_revoked",
            recorded_at=NOW,
        )
        validate_evidence_validity(revoked, prior=self.validity)
        with self.assertRaisesRegex(ValueError, "terminal"):
            build_evidence_validity_successor(
                prior=revoked,
                status=EvidenceValidityStatus.SUPERSEDED,
                reason_code="later_revision",
                recorded_at=NOW,
            )
        forged = replace(
            revoked,
            prior_evidence_validity_content_sha256="a" * 64,
        )
        with self.assertRaisesRegex(ValueError, "successor"):
            validate_evidence_validity(forged, prior=self.validity)

    def test_satisfaction_cannot_be_faked_from_rejected_evidence(self) -> None:
        _, rejected = self._build_scope_rejected_admission()
        rejected_validity = build_initial_evidence_validity(
            admission=rejected,
            recorded_at=NOW,
        )
        blocked = build_obligation_satisfaction(
            obligation=self.obligation,
            admissions=(rejected,),
            validities=(rejected_validity,),
            boundary_outcome=None,
            prior=None,
            recorded_at=NOW,
        )
        self.assertEqual(
            blocked.status,
            ObligationSatisfactionStatus.BLOCKED,
        )
        forged = replace(
            blocked,
            status=ObligationSatisfactionStatus.SATISFIED,
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_obligation_satisfaction(forged, prior=None)

    def test_satisfaction_successor_binds_exact_prior_head(self) -> None:
        satisfied = build_obligation_satisfaction(
            obligation=self.obligation,
            admissions=(self.admission,),
            validities=(self.validity,),
            boundary_outcome=None,
            prior=None,
            recorded_at=NOW,
        )
        superseded = build_obligation_satisfaction(
            obligation=self.obligation,
            admissions=(self.admission,),
            validities=(self.validity,),
            boundary_outcome=None,
            prior=satisfied,
            recorded_at=NOW,
            supersede=True,
        )
        self.assertEqual(
            superseded.status,
            ObligationSatisfactionStatus.SUPERSEDED,
        )
        forged = replace(
            superseded,
            prior_obligation_satisfaction_content_sha256="b" * 64,
        )
        with self.assertRaisesRegex(ValueError, "append-only"):
            validate_obligation_satisfaction(forged, prior=satisfied)
        self.assertNotIn(
            "evidence_use_binding_ids",
            satisfied.__dataclass_fields__,
        )

    def test_typed_boundary_creates_initial_boundary_satisfaction(
        self,
    ) -> None:
        design = gate1_fixtures.make_measurement_design()
        frame = gate1_fixtures.make_frame(
            measurement_design=replace(
                design,
                window_rules=tuple(
                    replace(
                        rule,
                        calendar_unit=(
                            resolver_fixtures.CalendarUnit.FISCAL_PERIOD
                        ),
                    )
                    for rule in design.window_rules
                ),
                evidence_requirements=tuple(
                    replace(
                        requirement,
                        boundary_policy=(
                            resolver_fixtures.RequirementBoundaryPolicy
                            .ALLOW_TYPED_BOUNDARY
                        ),
                        allowed_boundary_codes=(
                            resolver_fixtures.ResolutionBoundaryCode
                            .UNSUPPORTED_CALENDAR.value,
                        ),
                    )
                    for requirement in design.evidence_requirements
                ),
            )
        )
        context = resolver_fixtures.make_context()
        request = resolver_fixtures.make_request(
            frame,
            anchor=date(2026, 6, 1),
        )
        registry = resolver_fixtures.make_trusted_registry(
            request,
            context,
        )
        resolver = resolver_fixtures.make_trusted_resolver()
        outcome = resolver.resolve_measurement(
            frame=frame,
            derivation_authority=(
                resolver_fixtures.make_derivation_authority(frame)
            ),
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=context,
            request=request,
            trusted_input_registry=registry,
            created_at=NOW,
        )
        obligation = resolver.compile_evidence_obligations(
            frame=frame,
            outcome=outcome,
            context=context,
            resolution_request=request,
            trusted_input_registry=registry,
            created_at=NOW,
        )[0]
        satisfaction = build_obligation_satisfaction(
            obligation=obligation,
            admissions=(),
            validities=(),
            boundary_outcome=outcome,
            prior=None,
            recorded_at=NOW,
        )
        self.assertEqual(
            satisfaction.status,
            ObligationSatisfactionStatus.BOUNDARY,
        )
        self.assertEqual(
            satisfaction.boundary_resolution_outcome_id,
            outcome.resolution_outcome_id,
        )
        validate_obligation_satisfaction(satisfaction, prior=None)


if __name__ == "__main__":
    unittest.main()
