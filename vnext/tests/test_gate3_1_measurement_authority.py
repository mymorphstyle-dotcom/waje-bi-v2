from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from gate1_fixtures import (
    NOW,
    QUESTION_TEXT,
    accept_initial_question,
    make_frame,
    make_measurement_design,
    make_operation,
    make_question,
    record_reviewed_frame,
)
from gate3_plan_fixtures import record_plan_bundle
from test_gate3_3_measurement_resolver import (
    make_derivation_authority,
    make_trusted_signer,
    make_trusted_verifier,
)
from waje_vnext.domain.async_runtime import MailboxMessageKind
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.identity import (
    ScopeRelationKind,
    canonical_identity_json_bytes,
    canonical_decimal_string,
    compute_resolution_id,
    compute_resolution_outcome_id,
    scope_relation,
    semantic_measurement_id,
)
from waje_vnext.domain.measurement import (
    AmbiguousLocalTimePolicy,
    ExposureBasis,
    ExposureFactSourceKind,
    MeasurementResolutionOutcome,
    ObligationExecutionDisposition,
    ResolvedEvidenceObligation,
    ResolvedExposureFact,
    ResolvedMeasurementInstance,
    ResolvedWindow,
    ResolutionContext,
    ResolutionOutcomeKind,
    ScopeExpression,
)
from waje_vnext.storage import (
    InMemoryAuthorityStore,
    InvalidAuthorityTransition,
)
from waje_vnext.storage.codec import decode_frame, decode_question


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
ROOT = Path(__file__).resolve().parents[1]


def make_resolution_admission(outcome):
    return make_trusted_signer()._issue_resolution_admission(
        outcome=outcome,
        registry_content_sha256=content_sha256("registry"),
        resolver_input_bundle_sha256=content_sha256("input-bundle"),
        resolution_context_sha256=content_sha256("context"),
    )


def make_resolved_outcome(frame):
    design = frame.measurement_design
    estimand = design.estimands[0]
    left_rule, right_rule = design.window_rules
    context = ResolutionContext(
        as_of_instant=datetime(2026, 7, 29, 8, tzinfo=UTC),
        timezone="Asia/Shanghai",
        business_day_cutoff="04:00:00",
        ambiguous_local_time_policy=(
            AmbiguousLocalTimePolicy.EARLIEST_FOLD
        ),
        calendar_version_ref="calendar:gregorian:v1",
        holiday_version_ref="holiday:cn:v2026",
        fiscal_version_ref=None,
        data_contract_version_ref="data-contract:payments:v3",
        snapshot_release_ref="release:2026-07-29",
        coverage_watermark_ref="watermark:2026-07-28T235959+08",
        late_arrival_policy_ref="late-arrival:t-plus-1:v1",
    )
    instance = ResolvedMeasurementInstance(
        resolution_id=SHA_A,
        semantic_measurement_id=frame.semantic_measurement_ids[0],
        authority_binding_id=frame.authority_binding_ids[0],
        frame_revision_id=frame.frame_revision_id,
        estimand_id=estimand.estimand_id,
        context=context,
        target_period_ref="target-period:2026-01",
        windows=(
            ResolvedWindow(
                operand_id=design.contrasts[0].operands[0].operand_id,
                window_rule_id=left_rule.window_rule_id,
                anchor_date=date(2026, 1, 1),
                period_offset=0,
                actual_start=date(2026, 1, 1),
                actual_end=date(2026, 1, 7),
                start_instant=datetime(
                    2025, 12, 31, 20, tzinfo=UTC
                ),
                end_instant=datetime(
                    2026, 1, 7, 20, tzinfo=UTC
                ),
                elapsed_seconds=7 * 86400,
                actual_calendar_days=7,
                selected_calendar_dates_count=7,
                observed_calendar_dates_count=7,
                valid_calendar_dates_count=7,
                selected_calendar_dates_sha256=content_sha256(
                    tuple(date(2026, 1, day) for day in range(1, 8))
                ),
                calendar_coverage_receipt_sha256=SHA_A,
                exposure_facts=(
                    ResolvedExposureFact(
                        exposure_id=design.exposures[0].exposure_id,
                        basis=ExposureBasis.VALID,
                        unit_ref=design.exposures[0].unit_ref,
                        expected_exposure_decimal="7",
                        observed_exposure_decimal="7",
                        valid_exposure_decimal="6",
                        invalid_exposure_decimal="1",
                        missing_exposure_decimal="0",
                        coverage_ratio_decimal=str(
                            Decimal(6) / Decimal(7)
                        ),
                        at_risk_exposure_decimal=None,
                        source_kind=(
                            ExposureFactSourceKind.SNAPSHOT_CATALOG
                        ),
                        source_receipt_sha256=SHA_A,
                    ),
                ),
            ),
            ResolvedWindow(
                operand_id=design.contrasts[0].operands[1].operand_id,
                window_rule_id=right_rule.window_rule_id,
                anchor_date=date(2026, 1, 1),
                period_offset=-1,
                actual_start=date(2025, 12, 25),
                actual_end=date(2025, 12, 31),
                start_instant=datetime(
                    2025, 12, 24, 20, tzinfo=UTC
                ),
                end_instant=datetime(
                    2025, 12, 31, 20, tzinfo=UTC
                ),
                elapsed_seconds=7 * 86400,
                actual_calendar_days=7,
                selected_calendar_dates_count=7,
                observed_calendar_dates_count=7,
                valid_calendar_dates_count=7,
                selected_calendar_dates_sha256=content_sha256(
                    tuple(date(2025, 12, day) for day in range(25, 32))
                ),
                calendar_coverage_receipt_sha256=SHA_A,
                exposure_facts=(
                    ResolvedExposureFact(
                        exposure_id=design.exposures[0].exposure_id,
                        basis=ExposureBasis.VALID,
                        unit_ref=design.exposures[0].unit_ref,
                        expected_exposure_decimal="7",
                        observed_exposure_decimal="7",
                        valid_exposure_decimal="7",
                        invalid_exposure_decimal="0",
                        missing_exposure_decimal="0",
                        coverage_ratio_decimal="1",
                        at_risk_exposure_decimal=None,
                        source_kind=(
                            ExposureFactSourceKind.SNAPSHOT_CATALOG
                        ),
                        source_receipt_sha256=SHA_B,
                    ),
                ),
            ),
        ),
        expected_scope_id=design.scopes[0].scope_id,
        expected_grain_ref=design.scopes[0].grain_ref,
        expected_unit_ref=design.scopes[0].unit_ref,
        expected_exposure_id=design.exposures[0].exposure_id,
        eligibility_id=design.eligibilities[0].eligibility_id,
        resolver_contract_ref=(
            "waje-vnext://measurement-resolver/gregorian.v1"
        ),
        resolver_input_bundle_sha256=SHA_B,
        field_derivation_proof_sha256=SHA_B,
    )
    instance = replace(
        instance,
        field_derivation_proof_sha256=hashlib.sha256(
            canonical_identity_json_bytes(
                {
                    "frame_revision_id": frame.frame_revision_id,
                    "estimand_id": estimand.estimand_id,
                    "semantic_measurement_id": (
                        frame.semantic_measurement_ids[0]
                    ),
                    "authority_binding_id": (
                        frame.authority_binding_ids[0]
                    ),
                    "context": context,
                    "target_period_ref": "target-period:2026-01",
                    "resolver_contract_ref": (
                        instance.resolver_contract_ref
                    ),
                    "resolver_input_bundle_sha256": (
                        instance.resolver_input_bundle_sha256
                    ),
                    "windows": instance.windows,
                    "scope": design.scopes[0],
                    "exposure": design.exposures[0],
                    "eligibility": design.eligibilities[0],
                }
            )
        ).hexdigest(),
    )
    instance = replace(
        instance,
        resolution_id=compute_resolution_id(instance),
    )
    outcome = MeasurementResolutionOutcome(
        resolution_outcome_id=SHA_C,
        case_id=frame.case_id,
        question_revision_id=frame.question_revision_id,
        frame_revision_id=frame.frame_revision_id,
        estimand_id=estimand.estimand_id,
        semantic_measurement_id=frame.semantic_measurement_ids[0],
        authority_binding_id=frame.authority_binding_ids[0],
        derivation_authority=make_derivation_authority(
            frame,
            mailbox_authority_epoch=1,
        ),
        kind=ResolutionOutcomeKind.RESOLVED_INSTANCE,
        resolved_instance=instance,
        boundary=None,
        requirement_boundaries=(),
        created_at=NOW,
    )
    return replace(
        outcome,
        resolution_outcome_id=compute_resolution_outcome_id(outcome),
    )


class MeasurementIdentityTest(unittest.TestCase):
    def test_question_lineage_does_not_become_semantic_measurement(self) -> None:
        first = make_measurement_design(question_id="question-a")
        second = replace(
            make_measurement_design(question_id="question-b"),
            question_grounding=replace(
                first.question_grounding,
                question_revision_id="question-b",
            ),
        )

        self.assertEqual(
            semantic_measurement_id(first, first.estimands[0].estimand_id),
            semantic_measurement_id(second, second.estimands[0].estimand_id),
        )

    def test_node_renaming_is_identity_stable(self) -> None:
        first = make_measurement_design()
        renamed = make_measurement_design(node_prefix="renamed-")

        self.assertEqual(
            semantic_measurement_id(first, first.estimands[0].estimand_id),
            semantic_measurement_id(
                renamed,
                renamed.estimands[0].estimand_id,
            ),
        )

    def test_set_like_graph_order_is_not_identity_material(self) -> None:
        first = make_measurement_design()
        contrast = first.contrasts[0]
        reordered = replace(
            first,
            contrasts=(
                replace(
                    contrast,
                    operands=tuple(reversed(contrast.operands)),
                ),
            ),
            scopes=(
                replace(
                    first.scopes[0],
                    time_window_rule_ids=tuple(
                        reversed(first.scopes[0].time_window_rule_ids)
                    ),
                ),
            ),
        )

        self.assertEqual(
            semantic_measurement_id(first, first.estimands[0].estimand_id),
            semantic_measurement_id(
                reordered,
                reordered.estimands[0].estimand_id,
            ),
        )

    def test_month_offset_and_exposure_are_identity_material(self) -> None:
        cross_month = make_measurement_design(right_period_offset=-1)
        same_month = make_measurement_design(right_period_offset=0)
        raw_total = replace(
            cross_month,
            exposures=(
                replace(
                    cross_month.exposures[0],
                    normalization=(
                        cross_month.exposures[0].normalization.NONE
                    ),
                ),
            ),
        )

        identities = {
            semantic_measurement_id(
                design,
                design.estimands[0].estimand_id,
            )
            for design in (cross_month, same_month, raw_total)
        }
        self.assertEqual(len(identities), 3)

    def test_canonical_decimal_is_exponent_free(self) -> None:
        self.assertEqual(canonical_decimal_string(Decimal("1000")), "1000")
        self.assertEqual(canonical_decimal_string(Decimal("0.900")), "0.9")
        self.assertEqual(canonical_decimal_string(Decimal("-0")), "0")

    def test_epoch3_codec_round_trip_is_exact(self) -> None:
        question = make_question()
        frame = make_frame(question=question)

        self.assertEqual(
            decode_question(to_jsonable(question)),
            question,
        )
        self.assertEqual(decode_frame(to_jsonable(frame)), frame)

    def test_python_matches_cross_language_identity_vectors(self) -> None:
        vectors = json.loads(
            (
                ROOT
                / "contracts/test-vectors/measurement-identity.v1.json"
            ).read_text(encoding="utf-8")
        )
        for vector in vectors["golden_vectors"]:
            with self.subTest(vector=vector["name"]):
                payload = canonical_identity_json_bytes(
                    _revive_identity_vector(vector["value"])
                )
                self.assertEqual(
                    payload.decode(),
                    vector["expected_canonical_json"],
                )
                self.assertEqual(
                    hashlib.sha256(payload).hexdigest(),
                    vector["expected_sha256"],
                )
        for vector in vectors["mutation_vectors"]:
            with self.subTest(vector=vector["name"]):
                left = canonical_identity_json_bytes(
                    _revive_identity_vector(vector["left"])
                )
                right = canonical_identity_json_bytes(
                    _revive_identity_vector(vector["right"])
                )
                relation = "same" if left == right else "different"
                self.assertEqual(
                    relation,
                    vector["expected_identity_relation"],
                )


class CalendarAndScopeBoundaryTest(unittest.TestCase):
    def test_actual_calendar_days_cover_month_lengths_and_cross_year(self) -> None:
        intervals = (
            (date(2023, 2, 1), date(2023, 2, 28), 28),
            (date(2024, 2, 1), date(2024, 2, 29), 29),
            (date(2024, 4, 1), date(2024, 4, 30), 30),
            (date(2024, 1, 1), date(2024, 1, 31), 31),
            (date(2025, 12, 25), date(2026, 1, 7), 14),
        )
        for index, (start, end, days) in enumerate(intervals):
            with self.subTest(start=start, end=end):
                window = ResolvedWindow(
                    operand_id=f"operand-{index}",
                    window_rule_id=f"window-{index}",
                    anchor_date=start,
                    period_offset=0,
                    actual_start=start,
                    actual_end=end,
                    start_instant=datetime.combine(
                        start,
                        datetime.min.time(),
                        tzinfo=UTC,
                    ),
                    end_instant=datetime.combine(
                        end + timedelta(days=1),
                        datetime.min.time(),
                        tzinfo=UTC,
                    ),
                    elapsed_seconds=days * 86400,
                    actual_calendar_days=days,
                    selected_calendar_dates_count=days,
                    observed_calendar_dates_count=days,
                    valid_calendar_dates_count=days,
                    selected_calendar_dates_sha256=content_sha256(
                        tuple(
                            start + timedelta(days=offset)
                            for offset in range(days)
                        )
                    ),
                    calendar_coverage_receipt_sha256=SHA_A,
                    exposure_facts=(
                        ResolvedExposureFact(
                            exposure_id=f"exposure-{index}",
                            basis=ExposureBasis.CALENDAR,
                            unit_ref="unit:calendar-day",
                            expected_exposure_decimal=str(days),
                            observed_exposure_decimal=str(days - 1),
                            valid_exposure_decimal=str(days - 2),
                            invalid_exposure_decimal="1",
                            missing_exposure_decimal="1",
                            coverage_ratio_decimal=str(
                                Decimal(days - 2) / Decimal(days)
                            ),
                            at_risk_exposure_decimal=None,
                            source_kind=(
                                ExposureFactSourceKind.SNAPSHOT_CATALOG
                            ),
                            source_receipt_sha256=SHA_A,
                        ),
                    ),
                )
                self.assertLess(
                    Decimal(
                        window.exposure_facts[0].valid_exposure_decimal
                    ),
                    Decimal(
                        window.exposure_facts[0].expected_exposure_decimal
                    ),
                )

    def test_calendar_day_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "calendar days"):
            ResolvedWindow(
                operand_id="operand",
                window_rule_id="window",
                anchor_date=date(2024, 2, 1),
                period_offset=0,
                actual_start=date(2024, 2, 1),
                actual_end=date(2024, 2, 29),
                start_instant=datetime(2024, 2, 1, tzinfo=UTC),
                end_instant=datetime(2024, 3, 1, tzinfo=UTC),
                elapsed_seconds=29 * 86400,
                actual_calendar_days=28,
                selected_calendar_dates_count=29,
                observed_calendar_dates_count=29,
                valid_calendar_dates_count=29,
                selected_calendar_dates_sha256=SHA_A,
                calendar_coverage_receipt_sha256=SHA_A,
                exposure_facts=(),
            )

    def test_scope_relation_fails_closed_without_contract_proof(self) -> None:
        design = make_measurement_design()
        left = design.scopes[0]
        right = replace(
            left,
            scope_id="scope-other",
            dimension_domain_refs=("dimension:one-channel",),
        )

        unknown = scope_relation(
            left,
            right,
            proof_policy_version="scope-proof.v1",
        )
        proven = scope_relation(
            left,
            right,
            proof_policy_version="scope-proof.v1",
            relation_contracts={
                (left.scope_id, right.scope_id): (
                    ScopeRelationKind.SUPERSET,
                    "scope-contract:channel-domain:v1",
                )
            },
        )

        self.assertIs(unknown.relation, ScopeRelationKind.UNKNOWN)
        self.assertIs(proven.relation, ScopeRelationKind.SUPERSET)
        self.assertEqual(
            proven.contract_proof_refs,
            ("scope-contract:channel-domain:v1",),
        )


class MeasurementStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        case = self.store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )
        case, self.question = accept_initial_question(self.store, case)
        self.frame = make_frame(question=self.question)
        frame_proof_id = record_reviewed_frame(self.store, self.frame)
        case = self.store.accept_frame(
            self.frame,
            frame_admission_proof_id=frame_proof_id,
            expected_head_version=case.head_version,
            event_id="event-frame",
            recorded_at=NOW,
        )
        self.case, self.plan_bundle = record_plan_bundle(
            store=self.store,
            case=case,
            frame=self.frame,
            created_at=NOW,
        )
        self.plan = self.plan_bundle.plan

    def test_forged_frame_identity_is_rejected(self) -> None:
        forged = replace(
            make_frame(
                revision_number=2,
                frame_id="frame-2",
                prior_id="frame-1",
                question=self.question,
            ),
            semantic_measurement_ids=(SHA_A,),
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "stale or forged",
        ):
            forged_proof_id = record_reviewed_frame(
                self.store,
                forged,
            )
            self.store.accept_frame(
                forged,
                frame_admission_proof_id=forged_proof_id,
                expected_head_version=self.case.head_version,
                event_id="event-frame-forged",
                recorded_at=NOW,
            )

    def test_resolution_and_obligation_cannot_change_estimand(self) -> None:
        outcome = make_resolved_outcome(self.frame)
        admission = make_resolution_admission(outcome)
        self.store.record_measurement_resolution(
            outcome,
            admission=admission,
            expected_head_version=self.case.head_version,
            event_id="event-resolution",
        )
        self.assertEqual(
            self.store.get_measurement_resolution_admission(
                outcome.resolution_outcome_id
            ),
            admission,
        )
        obligation = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )[0]
        self.assertEqual(
            obligation.estimand_id,
            self.frame.measurement_design.estimands[0].estimand_id,
        )

        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "exact derivation replay",
        ):
            self.store.record_evidence_obligation(
                replace(
                    obligation,
                    obligation_id=SHA_E,
                ),
                expected_head_version=self.case.head_version,
                event_id="event-obligation-forged",
            )

    def test_resolution_identity_rejects_calendar_drift(self) -> None:
        outcome = make_resolved_outcome(self.frame)
        instance = outcome.resolved_instance
        assert instance is not None
        drifted_window = replace(
            instance.windows[1],
            period_offset=0,
        )
        drifted = replace(
            outcome,
            resolved_instance=replace(
                instance,
                windows=(instance.windows[0], drifted_window),
            ),
        )

        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "resolution identity",
        ):
            self.store.record_measurement_resolution(
                drifted,
                admission=make_resolution_admission(drifted),
                expected_head_version=self.case.head_version,
                event_id="event-resolution-calendar-drift",
            )

    def test_rehashed_resolution_cannot_change_frame_window_offset(self) -> None:
        outcome = make_resolved_outcome(self.frame)
        instance = outcome.resolved_instance
        assert instance is not None
        drifted_window = replace(
            instance.windows[1],
            period_offset=0,
        )
        drifted_instance = replace(
            instance,
            windows=(instance.windows[0], drifted_window),
        )
        drifted_instance = replace(
            drifted_instance,
            resolution_id=compute_resolution_id(drifted_instance),
        )
        drifted = replace(
            outcome,
            resolved_instance=drifted_instance,
        )
        drifted = replace(
            drifted,
            resolution_outcome_id=compute_resolution_outcome_id(drifted),
        )

        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "Frame window offset",
        ):
            self.store.record_measurement_resolution(
                drifted,
                admission=make_resolution_admission(drifted),
                expected_head_version=self.case.head_version,
                event_id="event-resolution-rehashed-offset-drift",
            )

    def test_resolution_outcome_identity_rejects_forged_digest(self) -> None:
        outcome = replace(
            make_resolved_outcome(self.frame),
            resolution_outcome_id=SHA_E,
        )

        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "outcome identity",
        ):
            self.store.record_measurement_resolution(
                outcome,
                admission=make_resolution_admission(outcome),
                expected_head_version=self.case.head_version,
                event_id="event-resolution-outcome-forged",
            )

    def test_resolution_persistence_requires_matching_trust_receipt(
        self,
    ) -> None:
        outcome = make_resolved_outcome(self.frame)
        admission = make_resolution_admission(outcome)
        forged = replace(outcome, resolution_outcome_id=SHA_E)
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "admission identity is stale",
        ):
            self.store.record_measurement_resolution(
                forged,
                admission=admission,
                expected_head_version=self.case.head_version,
                event_id="event-resolution-untrusted-rehash",
            )

    def test_question_correction_clears_all_downstream_heads(self) -> None:
        payload = {"message": "改为比较自然周，并重新定义时间口径。"}
        self.store.append_mailbox_message(
            message_id="message-2",
            case_id="case-1",
            kind=MailboxMessageKind.USER_CORRECTION,
            operation=make_operation(
                operation_id="operation-question-2",
                idempotency_key="question-message-key-2",
                payload=payload,
            ),
            payload=payload,
            created_at=NOW,
        )
        revised = make_question(
            revision_number=2,
            question_id="question-2",
            prior_id="question-1",
            accepted_head_version=self.case.head_version + 1,
            event_id="event-question-2",
            message_id="message-2",
            message_sequence=2,
            text=str(payload["message"]),
        )
        case = self.store.accept_question(
            revised,
            expected_head_version=self.case.head_version,
            event_id=revised.acceptance_event_id,
            recorded_at=NOW,
        )

        self.assertEqual(case.accepted_question_revision_id, "question-2")
        self.assertIsNone(case.accepted_frame_revision_id)
        self.assertIsNone(case.accepted_plan_revision_id)
        self.assertIsNone(case.accepted_answer_version_id)


def _revive_identity_vector(value):
    if isinstance(value, list):
        return [_revive_identity_vector(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            return Decimal(value["$decimal"])
        if set(value) == {"$timestamp"}:
            return datetime.fromisoformat(
                value["$timestamp"].replace("Z", "+00:00")
            )
        return {
            key: _revive_identity_vector(item)
            for key, item in value.items()
        }
    return value


if __name__ == "__main__":
    unittest.main()
    ResolvedExposureFact,
