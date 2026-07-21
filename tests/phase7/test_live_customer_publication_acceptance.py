from __future__ import annotations

from copy import deepcopy
import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.phase7 import run_live_conversation_system_test as live_acceptance


ROOT = Path(__file__).resolve().parents[2]
_EVENT_UNSET = object()


def _case() -> dict:
    return live_acceptance.load_cases(
        str(ROOT / "evals" / "phase7" / "business_question_expectations.yaml")
    )[0]


def _health() -> dict:
    return {
        "checked_at": "2026-07-18T00:00:00Z",
        "overall_status": "ok",
        "checks": [
            {
                "dependency": dependency,
                "gateway_check": gateway_check,
                "status": "ok",
                "detail": "healthy",
            }
            for dependency, gateway_check in (
                ("gateway", "frontend_gateway"),
                ("postgres", "postgres_runtime_store"),
                ("clickhouse", "clickhouse_access"),
                ("deepseek", "llm_access"),
            )
        ],
    }


def _authority() -> dict:
    obligation_closure = [
        {
            "obligation_id": "obligation-one",
            "proposed_claim_refs": ["proposed-claim-one"],
            "unavailable_limitation_refs": [],
            "coverage_claim_refs": ["claim-one"],
            "coverage_limitation_refs": [],
            "coverage_state": "satisfied",
        }
    ]
    return {
        "active_release_refs": {
            "actual_as_of": "2026-07-18T00:00:00Z",
            "release_refs": ["release:paid-order:active"],
            "snapshot_refs": ["snapshot:paid-order:active"],
        },
        "authority_refs": {
            "intent_revision_id": "intent-one",
            "authority_context_ref": "context-one",
            "authority_context_digest": "a" * 64,
            "plan_revision_id": "plan-one",
            "execution_result_ref": "execution-one",
            "authority_bundle_ref": "bundle-one",
            "authority_bundle_digest": "b" * 64,
        },
        "pair_material_snapshot": _pair_material_snapshot(obligation_closure),
        "required_obligation_publication_closure": {
            "authority_mode": "claim_bearing",
            "verified_claim_refs": ["claim-one"],
            "obligations": obligation_closure,
        },
    }


def _pair_material_snapshot(obligation_closure: list[dict]) -> dict:
    return live_acceptance.build_pair_material_snapshot(
        intent_revision_id="intent-one",
        plan_revision_id="plan-one",
        target_metric_refs=["paid_amount"],
        analysis_axes=[
            {
                "axis_id": "change_validation",
                "target_metric_refs": ["paid_amount"],
                "metric_refs": ["paid_amount", "paid_user_count"],
            }
        ],
        scope={"type": "full_sample"},
        intent_time_spec={
            "kind": "date_range",
            "start": "2026-01-01",
            "end": "2026-06-30",
        },
        resolved_window_refs=["window:h1-2026"],
        context_window_specs=[],
        plan_decision_refs=["decision:baseline"],
        active_decisions=[
            {
                "decision_ref": "decision:baseline",
                "slot_id": "comparison_baseline",
                "option_id": "previous_period",
                "source": "system",
                "status": "inferred",
                "materiality": "material",
                "value": {"baseline": "previous_period"},
                "affected_plan_fields": ["resolved_window_refs"],
            }
        ],
        user_required_obligations=[
            {
                "obligation_id": "obligation-one",
                "role": "user_required",
                "claim_kind": "comparative_change",
                "subject": {
                    "target_metric_ref": "paid_amount",
                    "scope": {"type": "full_sample"},
                    "outcome_refs": ["direction_and_magnitude"],
                },
                "success_policy": {"minimum_claim_strength": "directional"},
            }
        ],
        obligation_closure=obligation_closure,
    )


def _unavailable_authority() -> dict:
    return {
        "active_release_refs": {
            "actual_as_of": None,
            "release_refs": [],
            "snapshot_refs": [],
        },
        "authority_refs": {
            "intent_revision_id": None,
            "authority_context_ref": None,
            "authority_context_digest": None,
            "plan_revision_id": None,
            "execution_result_ref": None,
            "authority_bundle_ref": None,
            "authority_bundle_digest": None,
        },
        "pair_material_snapshot": None,
        "required_obligation_publication_closure": {
            "authority_mode": None,
            "verified_claim_refs": [],
            "obligations": [],
        },
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


def _safe_publication(delivery_status: str = "published") -> dict:
    return {
        "authority_bundle_ref": "bundle-one",
        "authority_bundle_digest": "b" * 64,
        "publication_ref": "publication-one",
        "publication_digest": "c" * 64,
        "projection_id": "projection-one",
        "projection_digest": "d" * 64,
        "outbox_ref": "outbox-one",
        "delivery_status": delivery_status,
    }


def _persisted(delivery_status: str = "published") -> dict:
    return {
        "customer_publication": _customer_publication(),
        "safe_publication": _safe_publication(delivery_status),
        "customer_payload_ref": "customer-payload-one",
        "customer_payload_digest": "e" * 64,
        "attempt_ref": "delivery-attempt-one",
        "failure_code": (
            None if delivery_status == "published" else "transport_unavailable"
        ),
        "customer_publication_ref": (
            "customer-publication-one" if delivery_status == "published" else None
        ),
    }


def _snapshot(
    *,
    publication_state: str = "published",
    delivery_state: str = "published",
) -> dict:
    return {
        "run_id": "run-one",
        "thread_id": "thread-one",
        "run_status": "completed",
        "execution_state": "complete",
        "interaction_state": "active",
        "evidence_state": "complete",
        "publication_state": publication_state,
        "delivery_state": delivery_state,
        "retry_state": "idle",
        "cancellation_state": "active",
        "supersession_state": "active",
    }


def _audits() -> list[dict]:
    return [
        {
            "audit_kind": "workflow_transition_attempt",
            "run_id": "run-one",
            "audit_ref": "response-one",
            "task": "narrative_writer",
            "provider_ref": "deepseek",
            "model_ref": "deepseek-chat",
            "status": "succeeded",
            "acceptance_state": "accepted",
            "attempt_number": 1,
            "input_ref": "input-one",
            "input_digest": "f" * 64,
            "output_digest": "1" * 64,
            "started_at": "2026-07-18T00:00:00Z",
            "finished_at": "2026-07-18T00:00:01Z",
        }
    ]


def _build(
    *,
    snapshot: dict | None = None,
    persisted: dict | None = None,
    event: dict | None | object = _EVENT_UNSET,
) -> dict:
    persisted = _persisted() if persisted is None else persisted
    if event is _EVENT_UNSET:
        event = {
            "customer_publication": persisted["customer_publication"],
            "publication": persisted["safe_publication"],
        }
    return live_acceptance.build_acceptance_summary(
        case=_case(),
        dependency_health=_health(),
        snapshot=snapshot or _snapshot(),
        run_ids=("run-one",),
        authority_records=_authority(),
        persisted_publication=persisted,
        event_publication=event,
        llm_call_audits=_audits(),
        human_decisions=[],
    )


def test_acceptance_summary_passes_only_on_exact_persisted_customer_publication():
    summary = _build()

    assert summary["schema_version"] == live_acceptance.ACCEPTANCE_SUMMARY_VERSION
    assert summary["acceptance_source"] == "persisted_customer_publication"
    assert summary["case"]["variant"] == "original"
    assert summary["publication"]["customer_publication_event_observed"] is True
    assert summary["pair_material_snapshot"]["metric_refs"] == [
        "paid_amount",
        "paid_user_count",
    ]
    assert summary["pair_material_snapshot"]["scope"] == {"type": "full_sample"}
    assert (
        summary["pair_material_snapshot"]["active_material_decisions"][0]["slot_id"]
        == "comparison_baseline"
    )
    assert (
        summary["pair_material_snapshot"]["user_required_obligation_coverage"][0][
            "coverage_state"
        ]
        == "satisfied"
    )
    assert summary["terminal_state"] == {
        "run_status": "completed",
        "publication_state": "published",
        "delivery_state": "published",
        "acceptance_status": "passed",
        "reason": "persisted_customer_publication_verified",
    }
    serialized = json.dumps(summary, ensure_ascii=False)
    for forbidden in ("raw_response_content", "api_key", "password"):
        assert forbidden not in serialized
    assert '"customer_publication":' not in serialized
    assert '"customer_payload":' not in serialized


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("metric_refs",), ["gross_revenue"]),
        (("scope", "type"), "filtered_sample"),
        (("time_semantics", "intent_time_spec", "start"), "2025-01-01"),
        (
            ("active_material_decisions", 0, "option_id"),
            "custom_baseline",
        ),
        (
            (
                "user_required_obligation_coverage",
                0,
                "minimum_claim_strength",
            ),
            "causal",
        ),
    ],
)
def test_pair_material_snapshot_rejects_nested_tampering(
    path: tuple[object, ...],
    replacement: object,
) -> None:
    snapshot = deepcopy(_authority()["pair_material_snapshot"])
    target: object = snapshot
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = replacement  # type: ignore[index]

    with pytest.raises(ValueError):
        live_acceptance.validate_pair_material_snapshot(snapshot)


def test_acceptance_summary_loader_rejects_snapshot_tampering(
    tmp_path: Path,
) -> None:
    summary = _build()
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    assert live_acceptance.load_acceptance_summary(path) == summary

    summary["pair_material_snapshot"]["scope"]["type"] = "filtered_sample"
    path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="pair_material_snapshot_digest_invalid"):
        live_acceptance.load_acceptance_summary(path)


def test_published_acceptance_requires_final_lifecycle_closure():
    snapshot = {**_snapshot(), "evidence_state": "partial"}

    summary = _build(snapshot=snapshot)

    assert summary["terminal_state"]["acceptance_status"] == "contract_failed"
    assert summary["terminal_state"]["reason"] == ("terminal_lifecycle_state_invalid")


def test_deepseek_evidence_must_be_a_succeeded_accepted_transition():
    for status, acceptance_state in (
        ("failed", "rejected"),
        ("succeeded", "rejected"),
        ("failed", "accepted"),
        ("persisted", "accepted"),
    ):
        audits = [
            {
                **_audits()[0],
                "audit_kind": "workflow_transition_attempt",
                "status": status,
                "acceptance_state": acceptance_state,
            }
        ]
        summary = live_acceptance.build_acceptance_summary(
            case=_case(),
            dependency_health=_health(),
            snapshot=_snapshot(),
            run_ids=("run-one",),
            authority_records=_authority(),
            persisted_publication=_persisted(),
            event_publication={
                "customer_publication": _customer_publication(),
                "publication": _safe_publication(),
            },
            llm_call_audits=audits,
            human_decisions=[],
        )

        assert summary["terminal_state"]["acceptance_status"] == "contract_failed"
        assert summary["terminal_state"]["reason"] == "deepseek_call_audit_missing"

    succeeded = [
        {
            **_audits()[0],
            "audit_kind": "workflow_transition_attempt",
            "status": "succeeded",
        }
    ]
    assert live_acceptance._deepseek_audit_observed(succeeded) is True


def test_claimable_required_obligation_must_reach_customer_publication():
    persisted = _persisted()
    persisted["customer_publication"] = {
        **persisted["customer_publication"],
        "blocks": [
            {
                "role": "boundary",
                "text": "当前只能发布证据边界。",
                "statement_role": "limitation",
                "claim_refs": [],
                "recommendation_refs": [],
                "limitation_refs": ["limitation:unresolved"],
                "material_fact_bindings": [],
            }
        ],
        "claim_refs": [],
        "limitation_refs": ["limitation:unresolved"],
    }
    authority = _authority()
    authority["required_obligation_publication_closure"] = {
        "authority_mode": "boundary_only",
        "verified_claim_refs": [],
        "obligations": [
            {
                "obligation_id": "obligation-one",
                "proposed_claim_refs": ["proposed-claim-one"],
                "unavailable_limitation_refs": [],
                "coverage_claim_refs": [],
                "coverage_limitation_refs": ["limitation:unresolved"],
                "coverage_state": "unresolved",
            }
        ],
    }
    event = {
        "customer_publication": persisted["customer_publication"],
        "publication": persisted["safe_publication"],
    }

    summary = live_acceptance.build_acceptance_summary(
        case=_case(),
        dependency_health=_health(),
        snapshot={**_snapshot(), "evidence_state": "boundary_only"},
        run_ids=("run-one",),
        authority_records=authority,
        persisted_publication=persisted,
        event_publication=event,
        llm_call_audits=_audits(),
        human_decisions=[],
    )

    assert summary["terminal_state"]["acceptance_status"] == "contract_failed"
    assert summary["terminal_state"]["reason"] == (
        "required_obligation_publication_closure_missing"
    )


def test_limitation_only_obligation_closes_through_coverage_and_customer_payload():
    authority = _authority()
    authority["required_obligation_publication_closure"] = {
        "authority_mode": "boundary_only",
        "verified_claim_refs": [],
        "obligations": [
            {
                "obligation_id": "obligation-one",
                "proposed_claim_refs": [],
                "unavailable_limitation_refs": ["limitation:unavailable"],
                "coverage_claim_refs": [],
                "coverage_limitation_refs": ["limitation:unavailable"],
                "coverage_state": "unavailable",
            }
        ],
    }
    authority["pair_material_snapshot"] = _pair_material_snapshot(
        authority["required_obligation_publication_closure"]["obligations"]
    )
    persisted = _persisted()
    persisted["customer_publication"] = {
        **persisted["customer_publication"],
        "blocks": [
            {
                "role": "boundary",
                "text": "当前证据只支持说明边界。",
                "statement_role": "limitation",
                "claim_refs": [],
                "recommendation_refs": [],
                "limitation_refs": ["limitation:unavailable"],
                "material_fact_bindings": [],
            }
        ],
        "claim_refs": [],
        "limitation_refs": ["limitation:unavailable"],
    }
    event = {
        "customer_publication": persisted["customer_publication"],
        "publication": persisted["safe_publication"],
    }

    summary = live_acceptance.build_acceptance_summary(
        case=_case(),
        dependency_health=_health(),
        snapshot={**_snapshot(), "evidence_state": "boundary_only"},
        run_ids=("run-one",),
        authority_records=authority,
        persisted_publication=persisted,
        event_publication=event,
        llm_call_audits=[
            {
                **_audits()[0],
                "audit_kind": "workflow_transition_attempt",
                "status": "succeeded",
            }
        ],
        human_decisions=[],
    )

    assert summary["terminal_state"]["acceptance_status"] == "passed"

    for missing_layer in ("coverage", "customer_publication"):
        broken_authority = json.loads(json.dumps(authority))
        broken_persisted = json.loads(json.dumps(persisted))
        if missing_layer == "coverage":
            broken_authority["required_obligation_publication_closure"]["obligations"][
                0
            ]["coverage_limitation_refs"] = []
        else:
            broken_persisted["customer_publication"]["limitation_refs"] = []
            broken_persisted["customer_publication"]["blocks"][0][
                "limitation_refs"
            ] = []
        broken_event = {
            "customer_publication": broken_persisted["customer_publication"],
            "publication": broken_persisted["safe_publication"],
        }
        broken = live_acceptance.build_acceptance_summary(
            case=_case(),
            dependency_health=_health(),
            snapshot={**_snapshot(), "evidence_state": "boundary_only"},
            run_ids=("run-one",),
            authority_records=broken_authority,
            persisted_publication=broken_persisted,
            event_publication=broken_event,
            llm_call_audits=[
                {
                    **_audits()[0],
                    "audit_kind": "workflow_transition_attempt",
                    "status": "succeeded",
                }
            ],
            human_decisions=[],
        )

        assert broken["terminal_state"]["acceptance_status"] == "contract_failed"
        assert broken["terminal_state"]["reason"] == (
            "required_obligation_publication_closure_missing"
        )


def test_verifier_vetoed_obligation_closes_with_published_explicit_boundary():
    authority = _authority()
    authority["required_obligation_publication_closure"] = {
        "authority_mode": "boundary_only",
        "verified_claim_refs": [],
        "obligations": [
            {
                "obligation_id": "obligation-one",
                "proposed_claim_refs": ["proposed-claim-one"],
                "unavailable_limitation_refs": [],
                "coverage_claim_refs": [],
                "coverage_limitation_refs": [
                    "limitation:verifier-rejected-required-claim"
                ],
                "coverage_state": "unavailable",
            }
        ],
    }
    persisted = _persisted()
    persisted["customer_publication"] = {
        **persisted["customer_publication"],
        "claim_refs": [],
        "limitation_refs": ["limitation:verifier-rejected-required-claim"],
    }

    assert live_acceptance._required_obligation_publication_closed(
        authority,
        persisted,
    )


@pytest.mark.parametrize(
    ("snapshot", "expected_status", "expected_reason"),
    [
        (
            {
                **_snapshot(publication_state="not_ready", delivery_state="pending"),
                "post_execution_status": "narrative_failed",
                "post_seal_failure_status": "narrative_failed",
                "post_seal_failure_terminal_ref": "post-seal-failure-one",
                "retry_state": "exhausted",
            },
            "run_failed",
            "post_execution_narrative_failed",
        ),
        (
            {
                **_snapshot(publication_state="failed", delivery_state="pending"),
                "post_execution_status": "publication_failed",
                "post_seal_failure_status": "publication_failed",
                "post_seal_failure_terminal_ref": "post-seal-failure-one",
                "retry_state": "exhausted",
            },
            "run_failed",
            "post_execution_publication_failed",
        ),
    ],
)
def test_post_execution_failures_are_immediate_typed_terminals(
    snapshot: dict,
    expected_status: str,
    expected_reason: str,
):
    assert live_acceptance._is_terminal_snapshot(snapshot) is True

    summary = live_acceptance.build_acceptance_summary(
        case=_case(),
        dependency_health=_health(),
        snapshot=snapshot,
        run_ids=("run-one",),
        authority_records=_authority(),
        persisted_publication=None,
        event_publication=None,
        llm_call_audits=[
            {
                **_audits()[0],
                "audit_kind": "workflow_transition_attempt",
                "status": "succeeded",
            }
        ],
        human_decisions=[],
    )

    assert summary["terminal_state"]["acceptance_status"] == expected_status
    assert summary["terminal_state"]["reason"] == expected_reason


def test_interaction_completed_is_an_immediate_nonanalysis_terminal():
    snapshot = {
        **_snapshot(publication_state="not_ready", delivery_state="pending"),
        "run_status": "interaction_completed",
        "execution_state": "not_started",
        "evidence_state": "not_started",
    }

    assert live_acceptance._is_terminal_snapshot(snapshot) is True
    summary = live_acceptance.build_acceptance_summary(
        case=_case(),
        dependency_health=_health(),
        snapshot=snapshot,
        run_ids=("run-one",),
        authority_records=_unavailable_authority(),
        persisted_publication=None,
        event_publication=None,
        llm_call_audits=[],
        human_decisions=[],
    )

    assert summary["terminal_state"]["acceptance_status"] == "not_evaluated"
    assert summary["terminal_state"]["reason"] == (
        "interaction_completed_without_analysis"
    )


def test_waiting_for_clarification_writes_typed_artifact_before_authority(
    tmp_path: Path,
):
    waiting_snapshot = {
        **_snapshot(publication_state="not_ready", delivery_state="pending"),
        "run_status": "waiting_for_clarification",
        "execution_state": "not_started",
        "evidence_state": "not_started",
    }
    with (
        patch.object(live_acceptance, "load_env_file", return_value=[]),
        patch.object(live_acceptance, "resolve_cli_cases", return_value=[_case()]),
        patch.object(live_acceptance, "_dependency_health", return_value=_health()),
        patch.object(
            live_acceptance,
            "_submit_gateway_operation",
            return_value=({"run": {"id": "run-one"}}, None, None),
        ),
        patch.object(
            live_acceptance.gateway_once,
            "_gateway_run_id",
            return_value="run-one",
        ),
        patch.object(
            live_acceptance,
            "_connect_runtime_database",
            return_value=nullcontext(object()),
        ),
        patch.object(
            live_acceptance,
            "_wait_for_terminal_snapshot",
            return_value=waiting_snapshot,
        ),
        patch.object(
            live_acceptance,
            "_authority_records",
            side_effect=AssertionError("plan authority must not be required"),
        ),
        patch.object(live_acceptance, "_persisted_publication", return_value=None),
        patch.object(live_acceptance, "_llm_call_audits", return_value=[]),
        patch.object(live_acceptance, "_human_decisions", return_value=[]),
    ):
        exit_code = live_acceptance.main(
            [
                "--case",
                _case()["id"],
                "--artifact-dir",
                str(tmp_path),
            ]
        )

    assert exit_code == 3
    artifacts = list(tmp_path.glob("*.json"))
    assert len(artifacts) == 1
    summary = json.loads(artifacts[0].read_text(encoding="utf-8"))
    live_acceptance.validate_acceptance_summary(summary)
    assert summary["terminal_state"] == {
        "run_status": "waiting_for_clarification",
        "publication_state": "not_ready",
        "delivery_state": "pending",
        "acceptance_status": "waiting_for_human",
        "reason": "human_clarification_required",
    }


def test_removed_withheld_publication_state_is_a_contract_failure():
    summary = live_acceptance.build_acceptance_summary(
        case=_case(),
        dependency_health=_health(),
        snapshot=_snapshot(publication_state="withheld", delivery_state="pending"),
        run_ids=("run-one",),
        authority_records=_authority(),
        persisted_publication=None,
        event_publication=None,
        llm_call_audits=_audits(),
        human_decisions=[],
    )

    assert summary["terminal_state"]["acceptance_status"] == "contract_failed"
    assert summary["terminal_state"]["reason"] == "delivery_terminal_state_invalid"


def test_withheld_state_rejects_any_customer_publication_record():
    summary = _build(
        snapshot=_snapshot(publication_state="withheld", delivery_state="pending")
    )

    assert summary["terminal_state"]["acceptance_status"] == "contract_failed"
    assert summary["terminal_state"]["reason"] == "delivery_terminal_state_invalid"


@pytest.mark.parametrize("delivery_state", ["retryable_failed", "permanently_failed"])
def test_delivery_failure_is_orthogonal_and_never_passes(delivery_state: str):
    persisted = _persisted(delivery_state)
    summary = _build(
        snapshot=_snapshot(
            publication_state="ready",
            delivery_state=delivery_state,
        ),
        persisted=persisted,
        event=None,
    )

    assert summary["terminal_state"]["run_status"] == "completed"
    assert summary["terminal_state"]["acceptance_status"] == "delivery_failed"
    assert summary["publication"]["state"] == "ready"
    assert summary["delivery"]["state"] == delivery_state
    assert summary["publication"]["customer_publication_event_observed"] is False


def test_published_delivery_requires_customer_publication_ready_event():
    summary = _build(event=None)

    assert summary["terminal_state"]["acceptance_status"] == "contract_failed"
    assert summary["terminal_state"]["reason"] == ("customer_publication_ready_missing")


def test_failed_delivery_contract_violation_cannot_pass():
    persisted = {**_persisted("retryable_failed"), "failure_code": None}
    summary = _build(
        snapshot=_snapshot(
            publication_state="ready",
            delivery_state="retryable_failed",
        ),
        persisted=persisted,
        event=None,
    )

    assert summary["terminal_state"]["acceptance_status"] == "contract_failed"
    assert summary["terminal_state"]["reason"] == (
        "failed_delivery_ref_closure_invalid"
    )


def test_event_and_persistence_mismatch_is_a_contract_failure():
    persisted = _persisted()
    event_publication = {
        "customer_publication": {
            **persisted["customer_publication"],
            "warnings": ["changed"],
        },
        "publication": persisted["safe_publication"],
    }

    summary = _build(persisted=persisted, event=event_publication)

    assert summary["terminal_state"]["acceptance_status"] == "contract_failed"
    assert summary["terminal_state"]["reason"] == (
        "customer_publication_event_persistence_mismatch"
    )


def test_published_delivery_requires_customer_delivery_receipt_ref():
    persisted = {**_persisted(), "customer_publication_ref": None}

    summary = _build(persisted=persisted)

    assert summary["terminal_state"]["acceptance_status"] == "contract_failed"
    assert summary["terminal_state"]["reason"] == (
        "published_delivery_ref_closure_invalid"
    )


def test_gateway_submission_sends_only_the_natural_language_question():
    case = _case()
    with (
        patch.object(
            live_acceptance.gateway_once,
            "_create_thread",
            return_value="thread-one",
        ),
        patch.object(
            live_acceptance.gateway_once,
            "_json_request",
            return_value={"run": {"id": "run-one"}},
        ) as request,
    ):
        response, source_run_id, decision = live_acceptance._submit_gateway_operation(
            base_url="http://gateway.test",
            user_id="human-test",
            case=case,
            thread_id=None,
            source_run_id=None,
            selected_option_id=None,
            free_text=None,
            request_identity="acceptance-one",
        )

    assert response == {"run": {"id": "run-one"}}
    assert source_run_id is None
    assert decision is None
    assert request.call_args.kwargs["payload"] == {
        "message": case["turns"][0]["user"],
        "requestIdentity": "acceptance-one",
    }
    assert request.call_args.kwargs["expected_status"] == 202
    assert request.call_args.kwargs["request_timeout_seconds"] == (
        live_acceptance.gateway_once.CLARIFICATION_ADMISSION_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize(
    ("selected_option_id", "free_text", "expected_payload"),
    [
        (
            "comparison_baseline.previous_day",
            None,
            {
                "answer": "comparison_baseline.previous_day",
                "selectedOptionId": "comparison_baseline.previous_day",
                "requestIdentity": "acceptance-one",
            },
        ),
        (
            None,
            "改为比较最近七个完整自然日",
            {
                "answer": "改为比较最近七个完整自然日",
                "selectedOptionId": None,
                "requestIdentity": "acceptance-one",
            },
        ),
    ],
)
def test_clarification_submission_uses_the_exact_async_command_contract(
    selected_option_id,
    free_text,
    expected_payload,
):
    with patch.object(
        live_acceptance.gateway_once,
        "_json_request",
        return_value={"attemptRunId": "run-one"},
    ) as request:
        response, source_run_id, decision = live_acceptance._submit_gateway_operation(
            base_url="http://gateway.test",
            user_id="human-test",
            case=_case(),
            thread_id=None,
            source_run_id="run-one",
            selected_option_id=selected_option_id,
            free_text=free_text,
            request_identity="acceptance-one",
        )

    assert response == {"attemptRunId": "run-one"}
    assert source_run_id == "run-one"
    assert decision["decision_kind"] == (
        "selected_option" if selected_option_id else "free_text"
    )
    assert request.call_args.kwargs["payload"] == expected_payload
    assert request.call_args.kwargs["expected_status"] == 202
    assert request.call_args.kwargs["request_timeout_seconds"] == (
        live_acceptance.gateway_once.CLARIFICATION_ADMISSION_TIMEOUT_SECONDS
    )


def test_clarification_continuation_rejects_implicit_or_ambiguous_input():
    kwargs = {
        "base_url": "http://gateway.test",
        "user_id": "human-test",
        "case": _case(),
        "thread_id": None,
        "source_run_id": "run-one",
        "request_identity": "acceptance-one",
    }
    with pytest.raises(ValueError, match="human_clarification_input_mode_invalid"):
        live_acceptance._submit_gateway_operation(
            **kwargs,
            selected_option_id=None,
            free_text=None,
        )
    with pytest.raises(ValueError, match="human_clarification_input_mode_invalid"):
        live_acceptance._submit_gateway_operation(
            **kwargs,
            selected_option_id="option-one",
            free_text="different instruction",
        )


def test_live_harness_has_no_answer_package_or_direct_agent_core_reader():
    source = (
        ROOT / "tools" / "phase7" / "run_live_conversation_system_test.py"
    ).read_text(encoding="utf-8")

    assert "answer_package" not in source
    assert "AnswerPackage" not in source
    assert "ConversationAgentCore" not in source
    assert "runtime_publication_index" not in source
    assert "prebound_sql" in source
    assert "customer_publication_ready" in source
