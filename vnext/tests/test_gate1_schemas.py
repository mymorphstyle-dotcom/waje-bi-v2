from __future__ import annotations

import json
import unittest
from pathlib import Path

from gate1_fixtures import (
    NOW,
    make_frame,
    make_plan,
    make_question,
)
import test_gate3_5_answer_contracts as answer_fixtures
import test_gate3_5_evidence_contracts as evidence_fixtures
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from waje_vnext.domain.actions import (
    ActionEnvelope,
    ActionKind,
    AgentActionProposal,
    CallCapabilityPayload,
)
from waje_vnext.domain.async_runtime import (
    AsyncJobKind,
    AuthoritySnapshot,
    OperationIdentity,
)
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.context import (
    ContextEvidenceItem,
    ContextEventItem,
    ContextUserMessageItem,
    build_context_packet,
)
from waje_vnext.domain.controller import ControllerPhase, ControllerState
from waje_vnext.domain.events import EventJournalEntry, JournalEventType
from waje_vnext.domain.runtime_state import (
    ActionReceipt,
    CheckpointRecord,
    OutboxMessage,
)
from waje_vnext.storage import InMemoryAuthorityStore


ROOT = Path(__file__).resolve().parents[1]


def load_schema(relative_path: str) -> dict[str, object]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class JsonSchemaContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.format_checker = FormatChecker()

    def test_all_gate1_schemas_are_valid_draft_2020_12(self) -> None:
        paths = (
            "contracts/domain/authority.v3.schema.json",
            "contracts/domain/actions.v3.schema.json",
            "contracts/domain/context-packet.v3.schema.json",
            "contracts/domain/runtime-state.v1.schema.json",
            "contracts/domain/controller-state.v1.schema.json",
            "contracts/domain/async-runtime.v1.schema.json",
            "contracts/domain/runtime-amendment.v1.schema.json",
            "contracts/domain/planning.v1.schema.json",
            "contracts/domain/evidence.v1.schema.json",
            "contracts/domain/answering.v1.schema.json",
            "contracts/domain/workflow.v1.schema.json",
            "contracts/events/journal-entry.v1.schema.json",
        )
        for path in paths:
            with self.subTest(path=path):
                Draft202012Validator.check_schema(load_schema(path))

    def test_five_authority_objects_match_language_neutral_schema(self) -> None:
        schema = load_schema("contracts/domain/authority.v3.schema.json")
        validator = Draft202012Validator(
            schema,
            format_checker=self.format_checker,
        )
        store = InMemoryAuthorityStore()
        case = store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )
        evidence_fixture = evidence_fixtures.Gate35EvidenceContractsTest()
        evidence_fixture.setUp()
        answer_fixture = answer_fixtures.Gate35AnswerContractsTest()
        answer_fixture.setUp()
        answer = answer_fixture._compile().answer
        assert answer is not None
        authorities = (
            case,
            make_question(),
            make_frame(),
            make_plan(),
            evidence_fixture.evidence,
            answer,
        )

        for authority in authorities:
            with self.subTest(authority=type(authority).__name__):
                validator.validate(to_jsonable(authority))

    def test_epoch3_schema_rejects_settlement_and_epoch_drift(self) -> None:
        schema = load_schema("contracts/domain/authority.v3.schema.json")
        validator = Draft202012Validator(
            schema,
            format_checker=self.format_checker,
        )
        answer_fixture = answer_fixtures.Gate35AnswerContractsTest()
        answer_fixture.setUp()
        answer = answer_fixture._compile().answer
        assert answer is not None
        settled = to_jsonable(answer)
        settled["status"] = "settled"
        frame_epoch_drift = to_jsonable(make_frame())
        frame_epoch_drift["schema_epoch"] = 2
        invalid_question_head = to_jsonable(make_question())
        invalid_question_head["accepted_head_version"] = 0

        for payload in (
            settled,
            frame_epoch_drift,
            invalid_question_head,
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validator.validate(payload)

    def test_action_context_and_event_match_schemas(self) -> None:
        store = InMemoryAuthorityStore()
        case = store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )
        action = ActionEnvelope(
            action_id="action-1",
            case_id="case-1",
            kind=ActionKind.CALL_CAPABILITY,
            expected_head_version=0,
            idempotency_key="key-1",
            issued_at=NOW,
            payload=CallCapabilityPayload(
                task_id="task-pattern",
                query_binding_id="a" * 64,
            ),
        )
        packet = build_context_packet(
            packet_id="packet-1",
            case=case,
            user_messages=(
                ContextUserMessageItem(
                    message_id="message-1",
                    sequence=1,
                    authority_epoch=1,
                    kind="user_message",
                    content="Investigate the pattern",
                ),
            ),
            relevant_event_cursor_start=1,
            relevant_event_cursor_end=1,
            accepted_question=None,
            accepted_frame=None,
            accepted_plan=None,
            accepted_answer=None,
            recent_events=(
                ContextEventItem.from_event(store.list_events("case-1")[0]),
            ),
            evidence_index=(
                ContextEvidenceItem(
                    evidence_record_id="evidence-1",
                    evidence_type="descriptive",
                    strength="quantified",
                    business_summary="Measured pattern",
                    limitation_count=1,
                    frame_revision_id="frame-1",
                    plan_revision_id="plan-1",
                    task_id="task-pattern",
                    snapshot_release_ref="release-1",
                ),
            ),
            decision_index=(),
            reviewer_objection_index=(),
            built_at=NOW,
        )
        event = EventJournalEntry(
            event_id="event-2",
            case_id="case-1",
            cursor=2,
            event_type=JournalEventType.ACTION_ADMITTED,
            recorded_at=NOW,
            operation=OperationIdentity(
                operation_id="operation-event-2",
                idempotency_key="event-key-2",
                causation_id="action-1",
                correlation_id="case-1",
                authority_revision=0,
                payload_sha256=content_sha256(
                    {"kind": "call_capability"}
                ),
            ),
            action_id="action-1",
            authority_ref=None,
            payload={"kind": "call_capability"},
            customer_projection={"state": "investigating"},
        )
        cases = (
            (
                "contracts/domain/actions.v3.schema.json",
                AgentActionProposal(
                    kind=action.kind,
                    payload=action.payload,
                ),
            ),
            (
                "contracts/domain/context-packet.v3.schema.json",
                packet,
            ),
            (
                "contracts/events/journal-entry.v1.schema.json",
                event,
            ),
        )
        for path, value in cases:
            with self.subTest(path=path):
                Draft202012Validator(
                    load_schema(path),
                    format_checker=self.format_checker,
                ).validate(to_jsonable(value))

        proposal = AgentActionProposal(
            kind=action.kind,
            payload=action.payload,
        )
        action_validator = Draft202012Validator(
            load_schema("contracts/domain/actions.v3.schema.json"),
            format_checker=self.format_checker,
        )
        action_validator.validate(to_jsonable(proposal))
        partial_envelope = to_jsonable(proposal)
        partial_envelope["action_id"] = "unbound-action"
        with self.assertRaises(ValidationError):
            action_validator.validate(partial_envelope)

    def test_controller_state_matches_language_neutral_schema(self) -> None:
        state = ControllerState(
            run_id="run-1",
            case_id="case-1",
            phase=ControllerPhase.READY_FOR_AGENT,
            step_number=2,
            head_version=2,
            authority_epoch=1,
            mailbox_cursor=1,
            last_event_cursor=8,
            context_packet_id="packet-1",
            latest_user_message="Investigate the pattern",
            pending_action_id=None,
            pending_job_ids=(),
            pending_decision_request_id=None,
            accepted_answer_version_id=None,
            consecutive_rejections=0,
            updated_at=NOW,
        )
        Draft202012Validator(
            load_schema("contracts/domain/controller-state.v1.schema.json"),
            format_checker=self.format_checker,
        ).validate(to_jsonable(state))

    def test_runtime_persistence_envelopes_match_schema(self) -> None:
        result_payload = {"admission": "accepted", "head_version": 2}
        state_payload = {
            "controller_contract": "controller-state.v1",
            "next_action": "call_capability",
        }
        outbox_payload = {
            "capability": "periodic_pattern_compare",
            "task_id": "task-pattern",
        }
        envelopes = (
            ActionReceipt(
                case_id="case-1",
                idempotency_key="action-key-1",
                action_id="action-1",
                request_sha256="1" * 64,
                result_schema_ref="action-admission-result.v1",
                result_payload=result_payload,
                result_sha256=content_sha256(result_payload),
                event_cursor=3,
                recorded_at=NOW,
            ),
            CheckpointRecord(
                checkpoint_id="checkpoint-1",
                case_id="case-1",
                head_version=2,
                event_cursor=4,
                context_packet_id="packet-1",
                context_sha256="2" * 64,
                state_schema_ref="controller-state.v1",
                state_payload=state_payload,
                state_sha256=content_sha256(state_payload),
                created_at=NOW,
            ),
            OutboxMessage(
                outbox_message_id="outbox-1",
                case_id="case-1",
                source_event_cursor=5,
                action_id="action-1",
                job_kind=AsyncJobKind.CAPABILITY,
                operation=OperationIdentity(
                    operation_id="operation-outbox-1",
                    idempotency_key="effect-key-1",
                    causation_id="action-1",
                    correlation_id="case-1",
                    authority_revision=1,
                    payload_sha256=content_sha256(outbox_payload),
                ),
                expected_head_version=2,
                expected_authority_epoch=1,
                authority_snapshot=AuthoritySnapshot(
                    case_id="case-1",
                    head_version=2,
                    mailbox_authority_epoch=1,
                    accepted_question_revision_id="question-1",
                    accepted_frame_revision_id="frame-1",
                    accepted_plan_revision_id="plan-1",
                    active_frame_candidate_generation=0,
                    active_frame_candidate_sha256=None,
                    obligation_state_version=0,
                    evidence_admission_state_version=0,
                    contradiction_state_version=0,
                ),
                authority_snapshot_sha256=content_sha256(
                    AuthoritySnapshot(
                        case_id="case-1",
                        head_version=2,
                        mailbox_authority_epoch=1,
                        accepted_question_revision_id="question-1",
                        accepted_frame_revision_id="frame-1",
                        accepted_plan_revision_id="plan-1",
                        active_frame_candidate_generation=0,
                        active_frame_candidate_sha256=None,
                        obligation_state_version=0,
                        evidence_admission_state_version=0,
                        contradiction_state_version=0,
                    )
                ),
                idempotency_key="effect-key-1",
                destination="capability-fabric",
                contract_ref="capability-request.v1",
                payload=outbox_payload,
                payload_sha256=content_sha256(outbox_payload),
                created_at=NOW,
            ),
        )
        validator = Draft202012Validator(
            load_schema("contracts/domain/runtime-state.v1.schema.json"),
            format_checker=self.format_checker,
        )

        for envelope in envelopes:
            with self.subTest(envelope=type(envelope).__name__):
                validator.validate(to_jsonable(envelope))

    def test_action_schema_rejects_kind_payload_mismatch(self) -> None:
        schema = load_schema("contracts/domain/actions.v3.schema.json")
        action = ActionEnvelope(
            action_id="action-1",
            case_id="case-1",
            kind=ActionKind.CALL_CAPABILITY,
            expected_head_version=0,
            idempotency_key="key-1",
            issued_at=NOW,
            payload=CallCapabilityPayload(
                task_id="task-pattern",
                query_binding_id="a" * 64,
            ),
        )
        invalid = to_jsonable(
            AgentActionProposal(kind=action.kind, payload=action.payload)
        )
        invalid["kind"] = "revise_frame"

        with self.assertRaises(ValidationError):
            Draft202012Validator(
                schema,
                format_checker=self.format_checker,
            ).validate(invalid)


class MigrationContractTest(unittest.TestCase):
    def test_migration_uses_independent_schema_and_append_only_tables(self) -> None:
        migration = (
            ROOT / "storage/migrations/001_gate1_authority.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("CREATE SCHEMA IF NOT EXISTS waje_vnext", migration)
        self.assertIn("analysis_frame_revisions", migration)
        self.assertIn("work_plan_revisions", migration)
        self.assertIn("evidence_records", migration)
        self.assertIn("answer_versions", migration)
        self.assertIn("event_journal", migration)
        self.assertIn("action_receipts", migration)
        self.assertIn("checkpoint_records", migration)
        self.assertIn("outbox_messages", migration)
        self.assertIn("case_mailbox_messages", migration)
        self.assertIn("authority_epoch", migration)
        self.assertIn("reject_immutable_change", migration)

    def test_gate2_migration_adds_controller_runtime_storage(self) -> None:
        migration = (
            ROOT / "storage/migrations/002_gate2_controller.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("action_records", migration)
        self.assertIn("user_decision_requests", migration)
        self.assertIn("effect_attempts", migration)
        self.assertIn("controller_leases", migration)
        self.assertIn("outbox_delivery_leases", migration)
        self.assertIn("heartbeat_at", migration)

    def test_gate3_1_migration_is_epoch3_and_fail_closed(self) -> None:
        migration = (
            ROOT
            / "storage/migrations/003_gate3_1_measurement_authority.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("requires a clean waje_vnext authority schema", migration)
        self.assertIn("question_revisions", migration)
        self.assertIn("measurement_resolution_outcomes", migration)
        self.assertIn("resolved_evidence_obligations", migration)
        self.assertIn("evidence_validity_records", migration)
        self.assertIn("settlement_precondition_reports", migration)
        self.assertIn("status = 'provisional'", migration)


if __name__ == "__main__":
    unittest.main()
