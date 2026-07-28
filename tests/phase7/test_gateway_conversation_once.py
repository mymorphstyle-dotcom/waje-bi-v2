from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.phase7 import run_gateway_conversation_once as gateway_once


def _status_event(status: str) -> dict:
    return {
        "event": "run_status",
        "runId": "run-checkpoint",
        "payload": {"status": status},
    }


def _customer_publication() -> dict:
    return {
        "blocks": [
            {
                "role": "executive_answer",
                "text": "付费金额上升。",
                "statement_role": "conclusion",
                "claim_refs": ["claim-one"],
                "recommendation_refs": [],
                "limitation_refs": [],
                "material_fact_bindings": [],
            }
        ],
        "claim_refs": ["claim-one"],
        "field_visibility_policy_ref": "policy-customer",
        "limitation_refs": [],
        "recommendation_refs": [],
        "visualization_refs": [],
        "warnings": [],
    }


def _publication(delivery_status: str = "published") -> dict:
    return {
        "authority_bundle_ref": "bundle-one",
        "authority_bundle_digest": "a" * 64,
        "publication_ref": "publication-one",
        "publication_digest": "b" * 64,
        "projection_id": "projection-one",
        "projection_digest": "c" * 64,
        "outbox_ref": "outbox-one",
        "delivery_status": delivery_status,
    }


def _post_execution_refs() -> dict:
    return {
        "post_execution_result_ref": "post-execution-one",
        "post_execution_result_digest": "d" * 64,
        "semantic_authority_result_ref": "semantic-authority-one",
        "semantic_authority_result_digest": "e" * 64,
        "authority_bundle_ref": "bundle-one",
        "authority_bundle_digest": "a" * 64,
        "authority_transition_id": "transition-authority-one",
        "claim_coverage_checkpoint_ref": "claim-coverage-checkpoint-one",
        "claim_coverage_checkpoint_digest": "1" * 64,
        "claim_coverage_transition_id": "transition-claim-coverage-one",
        "post_seal_failure_terminal_ref": None,
        "failure_record_ref": None,
        "failure_lifecycle_state_digest": None,
        "narrative_workflow_ref": "narrative-one",
        "narrative_workflow_digest": "f" * 64,
        "compose_transition_id": "transition-compose-one",
        "publication_ref": "publication-one",
        "outbox_ref": "outbox-one",
        "customer_payload_ref": "customer-payload-one",
        "delivery_attempt_ref": "delivery-attempt-one",
        "customer_publication_ref": "customer-publication-one",
    }


def _post_execution_state(status: str = "completed") -> dict:
    publication_status, delivery_status = gateway_once.POST_EXECUTION_STATE_MATRIX[
        status
    ]
    refs = _post_execution_refs()
    state = {
        "post_execution_status": status,
        "analysis_status": "complete",
        "publication_status": publication_status,
        "delivery_status": delivery_status,
        "publication_refs": refs,
    }
    if status in {
        "delivery_retryable_failed",
        "delivery_permanently_failed",
    }:
        refs["customer_publication_ref"] = None
    elif status in gateway_once.POST_EXECUTION_FAILURE_STATUSES:
        refs.update(
            {
                "post_seal_failure_terminal_ref": "post-seal-failure-one",
                "failure_record_ref": "failure-one",
                "failure_lifecycle_state_digest": "1" * 64,
                "narrative_workflow_ref": None,
                "narrative_workflow_digest": None,
                "compose_transition_id": None,
                "publication_ref": None,
                "outbox_ref": None,
                "customer_payload_ref": None,
                "delivery_attempt_ref": None,
                "customer_publication_ref": None,
            }
        )
        state["operational_failure"] = {
            "failure_ref": "failure-one",
            "layer": "narrative" if status == "narrative_failed" else "persistence",
            "kind": "provider_failure",
            "retryability": "retryable",
            "business_boundary": "Accepted analysis remains authoritative.",
        }
    return state


def _post_execution_event(status: str) -> dict:
    return {
        "event": "post_execution_state",
        "runId": "run-checkpoint",
        "payload": _post_execution_state(status),
    }


def _publication_event() -> dict:
    return {
        "event": "customer_publication_ready",
        "runId": "run-checkpoint",
        "payload": {
            "status": "completed",
            "customer_publication": _customer_publication(),
            "publication": _publication(),
            "post_execution": _post_execution_state(),
        },
    }


def _customer_snapshot_response(
    *,
    thread_id: str = "thread-checkpoint",
    run_id: str | None = "run-checkpoint",
    status: str = "working",
) -> dict:
    return {
        "snapshot": {
            "stateVersion": "state-one",
            "state": {"status": status},
            "transport": {
                "threadHandle": thread_id,
                "runHandle": run_id,
                "actionHandle": run_id,
                "eventsUrl": (
                    f"/api/runs/{run_id}/events" if run_id is not None else None
                ),
                "acceptedOperationIds": [],
            },
        }
    }


def _clarification_admission(
    selected_option_id: str | None = "baseline.previous_quarter",
) -> dict:
    del selected_option_id
    return {
        "snapshot": {
            "stateVersion": "state-clarification-admitted",
            "state": {"status": "needs_input"},
            "transport": {
                "threadHandle": "thread-checkpoint",
                "runHandle": "run-checkpoint",
                "actionHandle": "run-checkpoint",
                "eventsUrl": "/api/runs/run-checkpoint/events",
                "acceptedOperationIds": [
                    "clarification-cli",
                    "clarification-free-text-one",
                    "clarification-option-one",
                    "clarification-option-budget",
                    "clarification-option-terminal",
                    "clarification-contract",
                ],
            },
        }
    }


def _dispatch_event(dispatch_id: str, status: str | None = None) -> dict:
    return {
        "event": ("run_dispatch_completed" if status else "run_dispatch_claimed"),
        "runId": "run-checkpoint",
        "payload": {
            "dispatch_id": dispatch_id,
            "state": "terminal" if status else "running",
            **({"terminal_status": status} if status else {}),
        },
    }


class GatewayConversationOnceTest(unittest.TestCase):
    def test_json_request_sends_authenticated_user_header(self):
        with patch.object(gateway_once, "urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = b"{}"

            result = gateway_once._json_request(
                "http://gateway.test",
                "/api/threads",
                user_id="user-test",
            )

        request = opener.call_args.args[0]
        self.assertEqual(result, {})
        self.assertEqual(opener.call_args.kwargs["timeout"], 600.0)
        self.assertEqual(
            request.get_header("X-waje-authenticated-user-id"),
            "user-test",
        )

    def test_json_request_requires_clarification_admission_http_status(self):
        with patch.object(gateway_once, "urlopen") as opener:
            response = opener.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b"{}"
            with self.assertRaisesRegex(
                RuntimeError,
                "gateway_http_status_unexpected:200:expected_202",
            ):
                gateway_once._json_request(
                    "http://gateway.test",
                    "/api/runs/run-one/clarifications",
                    method="POST",
                    payload={"answer": "one", "selectedOptionIds": []},
                    user_id="user-test",
                    expected_status=202,
                )

        opener.assert_called_once()

    def test_poll_collects_persisted_events_until_user_checkpoint(self):
        node_event = {
            "event": "node_process",
            "runId": "run-checkpoint",
            "payload": {"node_name": "understand_business_intent"},
        }
        with (
            patch.object(
                gateway_once,
                "_events",
                side_effect=[
                    [_status_event("running"), node_event],
                    [_status_event("waiting_for_clarification"), node_event],
                ],
            ),
            patch.object(gateway_once, "sleep"),
        ):
            result = gateway_once._poll_run_events(
                base_url="http://gateway.test",
                user_id="user-test",
                run_id="run-checkpoint",
                events_url="/api/runs/run-checkpoint/events",
                timeout_seconds=10,
                poll_interval_seconds=0.01,
            )

        self.assertTrue(result["checkpoint_reached"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["terminal_status"], "waiting_for_clarification")
        self.assertEqual(result["business_acceptance"], "waiting_for_human")
        self.assertEqual(result["poll_attempts"], 2)
        self.assertEqual(
            [event["event"] for event in result["events"]].count("node_process"),
            1,
        )

    def test_completed_requires_exact_customer_publication_ready_event(self):
        with (
            patch.object(
                gateway_once,
                "_events",
                side_effect=[
                    [_status_event("completed")],
                    [_status_event("completed"), _publication_event()],
                ],
            ),
            patch.object(gateway_once, "sleep"),
        ):
            result = gateway_once._poll_run_events(
                base_url="http://gateway.test",
                user_id="user-test",
                run_id="run-checkpoint",
                events_url="/api/runs/run-checkpoint/events",
                timeout_seconds=10,
                poll_interval_seconds=0.01,
            )

        self.assertEqual(result["poll_attempts"], 2)
        self.assertEqual(result["terminal_status"], "completed")
        self.assertEqual(result["run_status"], "completed")
        self.assertEqual(result["post_execution_status"], "completed")
        self.assertEqual(result["customer_publication"], _customer_publication())
        self.assertEqual(result["publication"], _publication())
        self.assertEqual(result["post_execution"], _post_execution_state())
        self.assertEqual(result["business_acceptance"], "passed")

    def test_completed_nonpublication_terminals_stop_on_typed_state(self):
        expected = {
            "delivery_retryable_failed": (
                "ready",
                "retryable_failed",
                "delivery_failed",
            ),
            "delivery_permanently_failed": (
                "ready",
                "permanently_failed",
                "delivery_failed",
            ),
            "narrative_failed": ("not_ready", "pending", "failed"),
            "publication_failed": ("failed", "pending", "failed"),
        }
        for status, states in expected.items():
            with self.subTest(status=status):
                with (
                    patch.object(
                        gateway_once,
                        "_events",
                        return_value=[
                            _status_event("completed"),
                            _post_execution_event(status),
                        ],
                    ),
                    patch.object(gateway_once, "sleep") as sleeper,
                ):
                    result = gateway_once._poll_run_events(
                        base_url="http://gateway.test",
                        user_id="user-test",
                        run_id="run-checkpoint",
                        events_url="/api/runs/run-checkpoint/events",
                        timeout_seconds=10,
                        poll_interval_seconds=0.01,
                    )

                self.assertEqual(result["run_status"], "completed")
                self.assertEqual(result["terminal_status"], status)
                self.assertEqual(result["post_execution_status"], status)
                self.assertEqual(
                    (
                        result["publication_state"],
                        result["delivery_state"],
                        result["business_acceptance"],
                    ),
                    states,
                )
                self.assertIsNone(result["customer_publication"])
                self.assertIsNone(result["publication"])
                self.assertEqual(result["poll_attempts"], 1)
                self.assertFalse(result["timed_out"])
                self.assertEqual(
                    result["events"][-1]["payload"],
                    _post_execution_state(status),
                )
                sleeper.assert_not_called()

    def test_nonpublication_terminal_rejects_state_matrix_mismatch(self):
        event = _post_execution_event("delivery_retryable_failed")
        event["payload"]["publication_status"] = "published"

        with (
            patch.object(
                gateway_once,
                "_events",
                return_value=[_status_event("completed"), event],
            ),
            self.assertRaisesRegex(RuntimeError, "post_execution_state_invalid"),
        ):
            gateway_once._poll_run_events(
                base_url="http://gateway.test",
                user_id="user-test",
                run_id="run-checkpoint",
                events_url="/api/runs/run-checkpoint/events",
                timeout_seconds=10,
                poll_interval_seconds=0.01,
            )

    def test_publication_event_rejects_extra_safe_ref(self):
        event = _publication_event()
        event["payload"]["publication"]["internal_owner"] = "private"

        with self.assertRaisesRegex(RuntimeError, "publication_safe_refs_invalid"):
            gateway_once._customer_publication_ready([event])

    def test_post_execution_requires_claim_coverage_authority_refs(self):
        missing = _post_execution_state()
        missing["publication_refs"].pop("claim_coverage_checkpoint_ref")
        with self.assertRaisesRegex(RuntimeError, "post_execution_state_refs_invalid"):
            gateway_once._require_post_execution_state(missing)

        invalid_digest = _post_execution_state()
        invalid_digest["publication_refs"]["claim_coverage_checkpoint_digest"] = (
            "not-a-digest"
        )
        with self.assertRaisesRegex(RuntimeError, "post_execution_state_refs_invalid"):
            gateway_once._require_post_execution_state(invalid_digest)

    def test_failed_checkpoint_keeps_typed_run_status(self):
        with patch.object(
            gateway_once,
            "_events",
            return_value=[_status_event("failed")],
        ):
            result = gateway_once._poll_run_events(
                base_url="http://gateway.test",
                user_id="user-test",
                run_id="run-checkpoint",
                events_url="/api/runs/run-checkpoint/events",
                timeout_seconds=10,
                poll_interval_seconds=0.01,
            )

        self.assertEqual(result["run_status"], "failed")
        self.assertEqual(result["terminal_status"], "failed")
        self.assertEqual(result["business_acceptance"], "failed")

    def test_customer_safe_stage_event_payload_is_preserved(self):
        payload = {
            "status": "evidence_ready",
            "execution_result": {
                "schema_version": "single-authority-phase03.v1",
                "status": "evidence_ready",
            },
        }

        self.assertEqual(
            gateway_once._project_event(
                {
                    "event": "execution_result_ready",
                    "runId": "run-checkpoint",
                    "payload": payload,
                }
            )["payload"],
            payload,
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "gateway_stage_event_payload_invalid",
        ):
            gateway_once._project_event(
                {
                    "event": "execution_result_ready",
                    "runId": "run-checkpoint",
                    "payload": None,
                }
            )

    def test_poll_timeout_keeps_observed_events_and_returns_nonterminal_result(self):
        running = _status_event("running")
        with (
            patch.object(gateway_once, "_events", return_value=[running]),
            patch.object(
                gateway_once,
                "monotonic",
                side_effect=[0.0, 0.0, 2.0],
            ),
            patch.object(gateway_once, "sleep") as sleeper,
        ):
            result = gateway_once._poll_run_events(
                base_url="http://gateway.test",
                user_id="user-test",
                run_id="run-checkpoint",
                events_url="/api/runs/run-checkpoint/events",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertFalse(result["checkpoint_reached"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["terminal_status"], "running")
        self.assertEqual(result["business_acceptance"], "not_evaluated")
        self.assertEqual(
            result["events"],
            [
                {
                    "event": "run_status",
                    "runId": "run-checkpoint",
                    "payload": {"status": "running"},
                }
            ],
        )
        sleeper.assert_not_called()

    def test_gateway_response_requires_one_consistent_run_identity(self):
        self.assertEqual(
            gateway_once._gateway_run_id(
                _customer_snapshot_response(run_id="run-one")
            ),
            "run-one",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "gateway_run_identity_invalid",
        ):
            gateway_once._gateway_run_id(
                _customer_snapshot_response(run_id=None)
            )

    def test_thread_creation_uses_authenticated_user_header_without_owner_body(self):
        with patch.object(
            gateway_once,
            "_json_request",
            return_value=_customer_snapshot_response(
                thread_id="thread-one",
                run_id=None,
            ),
        ) as request:
            thread_id = gateway_once._create_thread(
                "http://gateway.test",
                "user-test",
                "thread-operation-one",
            )

        self.assertEqual(thread_id, "thread-one")
        request.assert_called_once_with(
            "http://gateway.test",
            "/api/threads",
            method="POST",
            payload={"requestIdentity": "thread-operation-one"},
            user_id="user-test",
            request_identity="thread-operation-one",
            expected_status=201,
        )

    def test_customer_snapshot_poll_waits_for_new_clarification_authority(self):
        initial = _clarification_admission()["snapshot"]
        advanced_response = _clarification_admission()
        advanced_response["snapshot"]["stateVersion"] = "state-next-question"
        with (
            patch.object(
                gateway_once,
                "_json_request",
                return_value=advanced_response,
            ) as request,
            patch.object(gateway_once, "monotonic", side_effect=[10.0, 11.0]),
            patch.object(gateway_once, "sleep"),
        ):
            result = gateway_once._poll_customer_snapshot(
                base_url="http://gateway.test",
                user_id="user-test",
                initial_snapshot=initial,
                timeout_seconds=30.0,
                poll_interval_seconds=0.1,
                ignore_needs_input_version=initial["stateVersion"],
            )

        self.assertEqual(result["terminal_status"], "needs_input")
        self.assertEqual(result["business_acceptance"], "waiting_for_human")
        self.assertEqual(result["poll_attempts"], 1)
        request.assert_called_once_with(
            "http://gateway.test",
            "/api/threads/thread-checkpoint",
            user_id="user-test",
            request_timeout_seconds=29.0,
        )

    def test_events_only_writes_checkpoint_artifact_and_uses_no_request_identity(self):
        checkpoint = {
            "operation": "events_only",
            "run_id": "run-checkpoint",
            "events_url": "/api/runs/run-checkpoint/events",
            "checkpoint_reached": True,
            "terminal_status": "completed",
            "timed_out": False,
            "poll_attempts": 1,
            "events": [_status_event("completed")],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "checkpoint.json"
            with (
                patch.object(
                    gateway_once,
                    "_poll_existing_run",
                    return_value=checkpoint,
                ) as poll,
                redirect_stdout(StringIO()),
            ):
                exit_code = gateway_once.main(
                    [
                        "--base-url",
                        "http://gateway.test",
                        "--run-id",
                        "run-checkpoint",
                        "--events-only",
                        "--output",
                        str(output_path),
                    ]
                )

            saved = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(saved, checkpoint)
        self.assertNotIn("request_identity", saved)
        poll.assert_called_once_with(
            base_url="http://gateway.test",
            user_id="human-led-test",
            run_id="run-checkpoint",
            timeout_seconds=gateway_once.DEFAULT_TIMEOUT_SECONDS,
            poll_interval_seconds=gateway_once.DEFAULT_POLL_INTERVAL_SECONDS,
        )

    def test_clarification_cli_requires_explicit_option_or_free_text(self):
        with self.assertRaises(SystemExit):
            gateway_once.main(
                [
                    "--base-url",
                    "http://gateway.test",
                    "--run-id",
                    "run-checkpoint",
                ]
            )

        observation = {
            "run_id": "run-checkpoint",
            "events_url": "/api/runs/run-checkpoint/events",
            "checkpoint_reached": True,
            "terminal_status": "planned",
            "publication_state": "not_ready",
            "delivery_state": "pending",
            "business_acceptance": "not_evaluated",
            "customer_publication": None,
            "publication": None,
            "timed_out": False,
            "poll_attempts": 1,
            "events": [],
        }
        with (
            patch.object(
                gateway_once,
                "_json_request",
                return_value=_clarification_admission(),
            ) as request,
            patch.object(
                gateway_once,
                "_observe_gateway_response",
                return_value=observation,
            ),
            redirect_stdout(StringIO()),
        ):
            exit_code = gateway_once.main(
                [
                    "--base-url",
                    "http://gateway.test",
                    "--run-id",
                    "run-checkpoint",
                    "--selected-option-id",
                    "baseline.previous_quarter",
                    "--request-identity",
                    "clarification-cli",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            request.call_args.kwargs["payload"],
            {
                "answer": "baseline.previous_quarter",
                "selectedOptionIds": ["baseline.previous_quarter"],
                "requestIdentity": "clarification-cli",
            },
        )
        self.assertEqual(
            request.call_args.kwargs["request_timeout_seconds"],
            gateway_once.CLARIFICATION_ADMISSION_TIMEOUT_SECONDS,
        )
        self.assertEqual(request.call_args.kwargs["expected_status"], 202)

    def test_free_text_clarification_sends_explicit_null_option_once(self):
        observation = {
            "run_id": "run-checkpoint",
            "events_url": "/api/runs/run-checkpoint/events",
            "checkpoint_reached": True,
            "terminal_status": "completed",
            "timed_out": False,
            "poll_attempts": 1,
            "events": [],
        }
        with (
            patch.object(
                gateway_once,
                "_json_request",
                return_value=_clarification_admission(None),
            ) as request,
            patch.object(
                gateway_once,
                "_observe_gateway_response",
                return_value=observation,
            ) as observe,
            patch.object(
                gateway_once,
                "monotonic",
                side_effect=[100.0, 102.0],
            ),
        ):
            result = gateway_once._submit_clarification_resolution(
                base_url="http://gateway.test",
                user_id="user-test",
                run_id="run-checkpoint",
                answer="请改用活动上线前七天",
                selected_option_id=None,
                request_identity="clarification-free-text-one",
                timeout_seconds=30.0,
                poll_interval_seconds=0.1,
            )

        request.assert_called_once()
        self.assertEqual(
            request.call_args.kwargs["payload"],
            {
                "answer": "请改用活动上线前七天",
                "selectedOptionIds": [],
                "requestIdentity": "clarification-free-text-one",
            },
        )
        self.assertEqual(request.call_args.kwargs["expected_status"], 202)
        self.assertEqual(
            request.call_args.kwargs["request_timeout_seconds"],
            30.0,
        )
        observe.assert_called_once()
        self.assertEqual(observe.call_args.kwargs["deadline"], 130.0)
        self.assertEqual(
            observe.call_args.kwargs["ignore_needs_input_version"],
            "state-clarification-admitted",
        )
        self.assertEqual(result["source_run_id"], "run-checkpoint")

    def test_clarification_admission_timeout_is_not_retried_or_observed(self):
        with (
            patch.object(
                gateway_once,
                "_json_request",
                side_effect=TimeoutError("admission deadline elapsed"),
            ) as request,
            patch.object(
                gateway_once,
                "_observe_gateway_response",
            ) as observe,
            patch.object(gateway_once, "monotonic", return_value=10.0),
            self.assertRaisesRegex(TimeoutError, "admission deadline elapsed"),
        ):
            gateway_once._submit_clarification_resolution(
                base_url="http://gateway.test",
                user_id="user-test",
                run_id="run-checkpoint",
                answer="baseline.previous_quarter",
                selected_option_id="baseline.previous_quarter",
                request_identity="clarification-option-one",
                timeout_seconds=900.0,
                poll_interval_seconds=0.1,
            )

        request.assert_called_once()
        self.assertEqual(
            request.call_args.kwargs["request_timeout_seconds"],
            gateway_once.CLARIFICATION_ADMISSION_TIMEOUT_SECONDS,
        )
        observe.assert_not_called()

    def test_clarification_admission_contract_requires_started_same_run(self):
        for field, value in (
            ("runHandle", "run-other"),
            ("actionHandle", "run-other"),
            ("acceptedOperationIds", []),
        ):
            with self.subTest(field=field):
                response = _clarification_admission()
                response["snapshot"]["transport"][field] = value
                with self.assertRaisesRegex(
                    RuntimeError,
                    "gateway_clarification_admission_invalid",
                ):
                    gateway_once._require_clarification_admission(
                        response,
                        source_run_id="run-checkpoint",
                        selected_option_id="baseline.previous_quarter",
                        request_identity="clarification-contract",
                    )

    def test_clarification_admission_accepts_active_and_terminal_replays(self):
        active = _clarification_admission()
        self.assertEqual(
            gateway_once._require_clarification_admission(
                active,
                source_run_id="run-checkpoint",
                selected_option_id="baseline.previous_quarter",
                request_identity="clarification-contract",
            ),
            active["snapshot"],
        )
        terminal = _clarification_admission()
        terminal["snapshot"]["state"] = {"status": "completed"}
        self.assertEqual(
            gateway_once._require_clarification_admission(
                terminal,
                source_run_id="run-checkpoint",
                selected_option_id="baseline.previous_quarter",
                request_identity="clarification-contract",
            ),
            terminal["snapshot"],
        )

    def test_clarification_observation_uses_only_remaining_total_budget(self):
        with (
            patch.object(
                gateway_once,
                "_json_request",
                return_value=_clarification_admission(),
            ) as request,
            patch.object(
                gateway_once,
                "_observe_gateway_response",
                return_value={
                    "run_id": "run-checkpoint",
                    "checkpoint_reached": False,
                    "timed_out": True,
                    "events": [],
                },
            ) as observe,
            patch.object(
                gateway_once,
                "monotonic",
                side_effect=[40.0, 55.0],
            ),
        ):
            gateway_once._submit_clarification_resolution(
                base_url="http://gateway.test",
                user_id="user-test",
                run_id="run-checkpoint",
                answer="baseline.previous_quarter",
                selected_option_id="baseline.previous_quarter",
                request_identity="clarification-option-budget",
                timeout_seconds=30.0,
                poll_interval_seconds=0.1,
            )

        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["request_timeout_seconds"], 30.0)
        observe.assert_called_once()
        self.assertEqual(observe.call_args.kwargs["deadline"], 70.0)
        self.assertNotIn("timeout_seconds", observe.call_args.kwargs)

    def test_started_clarification_observes_persisted_stages_until_terminal(self):
        with (
            patch.object(
                gateway_once,
                "_json_request",
                return_value=_clarification_admission(),
            ) as request,
            patch.object(
                gateway_once,
                "_observe_gateway_response",
                return_value={
                    "run_id": "run-checkpoint",
                    "checkpoint_reached": True,
                    "terminal_status": "completed",
                    "business_acceptance": "passed",
                    "poll_attempts": 4,
                },
            ),
        ):
            result = gateway_once._submit_clarification_resolution(
                base_url="http://gateway.test",
                user_id="user-test",
                run_id="run-checkpoint",
                answer="baseline.previous_quarter",
                selected_option_id="baseline.previous_quarter",
                request_identity="clarification-option-terminal",
                timeout_seconds=30.0,
                poll_interval_seconds=0.01,
            )

        request.assert_called_once()
        self.assertEqual(result["terminal_status"], "completed")
        self.assertEqual(result["business_acceptance"], "passed")
        self.assertEqual(result["poll_attempts"], 4)

    def test_clarification_does_not_reaccept_the_pre_command_waiting_checkpoint(self):
        dispatch_id = "dispatch-clarification-one"
        with (
            patch.object(
                gateway_once,
                "_events",
                side_effect=[
                    [
                        _status_event("waiting_for_clarification"),
                        _dispatch_event(dispatch_id),
                    ],
                    [
                        _status_event("waiting_for_clarification"),
                        _dispatch_event(
                            dispatch_id,
                            "waiting_for_clarification",
                        ),
                    ],
                ],
            ),
            patch.object(gateway_once, "sleep"),
        ):
            result = gateway_once._poll_run_events(
                base_url="http://gateway.test",
                user_id="user-test",
                run_id="run-checkpoint",
                events_url="/api/runs/run-checkpoint/events",
                timeout_seconds=10,
                poll_interval_seconds=0.01,
                await_dispatch_id=dispatch_id,
            )

        self.assertEqual(result["poll_attempts"], 2)
        self.assertEqual(result["terminal_status"], "waiting_for_clarification")


if __name__ == "__main__":
    unittest.main()
