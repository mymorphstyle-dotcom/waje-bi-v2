from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from tests.phase7.test_single_authority_phase03_workflow_core import (
    _compile_execute_and_evaluate,
)
from tools.phase7 import run_single_authority_phase03_acceptance as acceptance
from tools.phase7 import run_gateway_conversation_once as gateway_runner


def _execution(monkeypatch: pytest.MonkeyPatch):
    wired = _compile_execute_and_evaluate(monkeypatch)
    execution = AuthoritativeExecutionResult.from_dict(wired.state["execution_result"])
    refs = {
        "schema_version": execution.schema_version,
        "authoritative_execution_result_ref": (
            execution.authoritative_execution_result_ref
        ),
        "intent_revision_id": execution.intent_revision_id,
        "authority_context_ref": execution.authority_context_ref,
        "plan_revision_id": execution.plan_revision_id,
        "execution_snapshot_ref": execution.execution_snapshot_ref,
        "stop_ref": execution.stop_ref,
        "accepted_transition_id": execution.transition_id,
    }
    persisted_request = wired.store.get_run_state(execution.run_attempt_id)["request"]
    wired.store.get_run_state = lambda run_id: {
        "run_id": run_id,
        "status": "completed",
        "request": {
            **persisted_request,
            "execution_result_refs": refs,
        },
    }
    wired.store._fetchone = lambda *_args, **_kwargs: (1, 1, 1)
    wired.store.apply_schema = lambda: None
    wired.store.connection = SimpleNamespace(close=lambda: None)
    return wired, execution


def _execution_observation(execution: AuthoritativeExecutionResult):
    customer_publication = {
        "blocks": [],
        "claim_refs": [],
        "field_visibility_policy_ref": "visibility-policy:customer-safe",
        "limitation_refs": [],
        "recommendation_refs": [],
        "visualization_refs": [],
        "warnings": [],
    }
    publication = {
        "authority_bundle_ref": "authority-bundle:test",
        "authority_bundle_digest": "a" * 64,
        "publication_ref": "publication:test",
        "publication_digest": "b" * 64,
        "projection_id": "projection:test",
        "projection_digest": "c" * 64,
        "outbox_ref": "outbox:test",
        "delivery_status": "published",
    }
    return {
        "checkpoint_reached": True,
        "terminal_status": "completed",
        "publication_state": "published",
        "delivery_state": "published",
        "business_acceptance": "passed",
        "customer_publication": customer_publication,
        "publication": publication,
        "run_id": execution.run_attempt_id,
        "events": [
            {
                "event": "execution_result_ready",
                "payload": {
                    "status": "evidence_ready",
                    "execution_result": execution.public_projection(),
                },
            },
            {
                "event": "customer_publication_ready",
                "payload": {
                    "status": "completed",
                    "customer_publication": customer_publication,
                    "publication": publication,
                },
            },
        ],
    }


def _terminal_observation(run_id: str, status: str) -> dict:
    publication_state, delivery_state = gateway_runner.POST_EXECUTION_STATE_MATRIX[
        status
    ]
    refs = {
        "post_execution_result_ref": "post-execution-one",
        "post_execution_result_digest": "d" * 64,
        "semantic_authority_result_ref": "semantic-one",
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
        "customer_publication_ref": None,
    }
    post_execution = {
        "post_execution_status": status,
        "analysis_status": "complete",
        "publication_status": publication_state,
        "delivery_status": delivery_state,
        "publication_refs": refs,
    }
    if status in gateway_runner.POST_EXECUTION_FAILURE_STATUSES:
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
            }
        )
        post_execution["operational_failure"] = {
            "failure_ref": "failure-one",
            "layer": "narrative" if status == "narrative_failed" else "persistence",
            "kind": "provider_failure",
            "retryability": "retryable",
            "business_boundary": "Accepted analysis remains authoritative.",
        }
    business_acceptance = (
        "delivery_failed"
        if status in {"delivery_retryable_failed", "delivery_permanently_failed"}
        else "failed"
    )
    return {
        "checkpoint_reached": True,
        "run_status": "completed",
        "terminal_status": status,
        "post_execution_status": status,
        "publication_state": publication_state,
        "delivery_state": delivery_state,
        "business_acceptance": business_acceptance,
        "post_execution": post_execution,
        "customer_publication": None,
        "publication": None,
        "timed_out": False,
        "run_id": run_id,
        "events": [],
    }


def test_gateway_observation_keeps_typed_clarification_state() -> None:
    clarification = {
        "run_id": "run-phase03-clarification",
        "question": "目标日期要跟哪个基准比较？",
        "status": "waiting",
        "options": [
            {
                "option_id": "comparison_baseline.previous_day",
                "label": "跟前一天比较（推荐）",
                "description": "用于日变化解释。",
                "recommended": True,
            }
        ],
    }

    projected = gateway_runner._project_event(
        {
            "event": "clarification_state_saved",
            "runId": "run-phase03-clarification",
            "payload": clarification,
        }
    )

    assert projected["payload"] == clarification


def test_phase03_review_requires_all_planned_tasks_and_exact_record_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, execution = _execution(monkeypatch)

    review = acceptance._review_execution_result(execution)
    planned_task_count = len(execution.plan_revision.capability_tasks)

    assert review["passed"] is True
    assert review["task_count"] == planned_task_count
    assert review["outcome_count"] == planned_task_count
    assert review["missing_adapter_capability_ids"] == []
    assert review["outcome_closure"] is True
    assert review["evidence_closure"] is True
    assert review["failure_closure"] is True
    assert review["stop_reason"] == "plan_exhausted"
    assert review["required_obligations"]["passed"] is True
    assert {item["status"] for item in review["required_obligations"]["states"]} == {
        "satisfied"
    }


def test_phase03_review_rejects_unresolved_user_required_obligation() -> None:
    obligation = SimpleNamespace(
        obligation_id="obligation:required",
        claim_kind="formula_component_contribution",
        role="user_required",
    )
    execution = SimpleNamespace(
        plan_revision=SimpleNamespace(claim_obligations=(obligation,)),
        capability_outcome_bundles=(),
    )

    review = acceptance._review_required_obligation_coverage(execution)

    assert review == {
        "passed": False,
        "states": [
            {
                "obligation_id": "obligation:required",
                "claim_kind": "formula_component_contribution",
                "status": "unresolved",
                "evidence_refs": (),
            }
        ],
    }


def test_phase03_persistence_review_requires_unique_settlement_and_latest_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired, execution = _execution(monkeypatch)

    loaded = acceptance._load_persisted_execution(
        wired.store,
        execution.run_attempt_id,
    )
    review = acceptance._review_execution_persistence(wired.store, loaded)

    assert loaded == execution
    assert review["passed"] is True
    assert review["stop_record_count"] == 1
    assert review["execution_snapshot_count"] == 1
    assert review["accepted_execute_transition_count"] == 1
    assert review["latest_accepted_transition_id"] == (
        wired.store.claim_coverage_checkpoint.transition_id
    )

    wired.store._fetchone = lambda *_args, **_kwargs: (1, 1, 2)
    assert (
        acceptance._review_execution_persistence(
            wired.store,
            execution,
        )["passed"]
        is False
    )

    wired.store._fetchone = lambda *_args, **_kwargs: (1, 1, 1)
    wired.store.latest_transition_override = "transition:post-execution-complete"
    assert (
        acceptance._review_execution_persistence(
            wired.store,
            execution,
        )["passed"]
        is True
    )


def test_gateway_projection_must_equal_persisted_public_authority_and_stay_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, execution = _execution(monkeypatch)
    observation = _execution_observation(execution)
    projection = acceptance._execution_projection_from_events(observation)

    review = acceptance._review_customer_projection(projection, execution)

    assert review == {
        "passed": True,
        "field_names": sorted(acceptance.PUBLIC_EXECUTION_FIELDS),
        "matches_persisted_authority": True,
        "private_paths": [],
        "private_markers": [],
    }

    leaked = dict(projection)
    leaked["provider_ref"] = "private-provider"
    leaked_review = acceptance._review_customer_projection(leaked, execution)
    assert leaked_review["passed"] is False
    assert "$.provider_ref" in leaked_review["private_paths"]
    assert "provider_ref" in leaked_review["private_markers"]

    mismatched = json.loads(json.dumps(projection))
    mismatched["stop"]["reason"] = "hard_budget_reached"
    mismatch_review = acceptance._review_customer_projection(
        mismatched,
        execution,
    )
    assert mismatch_review["passed"] is False
    assert mismatch_review["matches_persisted_authority"] is False


def test_gateway_start_review_keeps_human_selection_typed() -> None:
    clarification = {
        "run_id": "run-phase03-acceptance",
        "topic_id": "topic-phase03-acceptance",
        "status": "waiting",
        "question": "目标日期要跟哪个基准比较？",
        "options": [
            {
                "option_id": "comparison_baseline.previous_day",
                "label": "跟前一天比较（推荐）",
                "description": "用于日变化解释。",
                "recommended": True,
            },
            {
                "option_id": "comparison_baseline.rolling_7_day_baseline",
                "label": "跟过去七天比较",
                "description": "用于平滑短期波动。",
                "recommended": False,
            },
            {
                "option_id": "tell_agent_differently",
                "label": "告诉分析助手采用其他方式",
                "description": "自由说明业务选择。",
                "recommended": False,
            },
        ],
    }
    observation = {
        "checkpoint_reached": True,
        "terminal_status": "waiting_for_clarification",
        "run_id": clarification["run_id"],
        "thread_id": "thread-phase03-acceptance",
        "events": [
            {
                "event": "clarification_state_saved",
                "payload": clarification,
            }
        ],
    }

    review = acceptance._review_start_gateway(
        observation,
        thread_id="thread-phase03-acceptance",
    )
    start = {"review": review}

    assert review["passed"] is True
    assert (
        acceptance._selected_option(
            start,
            "comparison_baseline.previous_day",
        )["recommended"]
        is True
    )
    with pytest.raises(ValueError, match="phase03_acceptance_option_id_invalid"):
        acceptance._selected_option(start, "tell_agent_differently")

    placeholder = json.loads(json.dumps(observation))
    placeholder["events"][0]["payload"]["question"] = "待确认的业务澄清问题"
    assert (
        acceptance._review_start_gateway(
            placeholder,
            thread_id="thread-phase03-acceptance",
        )["passed"]
        is False
    )


def test_live_execute_command_uses_gateway_sse_then_postgres_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wired, execution = _execution(monkeypatch)
    output_dir = tmp_path / "single-authority-phase03" / "case-b-test"
    output_dir.mkdir(parents=True)
    clarification = {
        "options": [
            {
                "option_id": "comparison_baseline.previous_day",
                "label": "跟前一天比较（推荐）",
                "description": "用于日变化解释。",
                "recommended": True,
            },
            {
                "option_id": "comparison_baseline.rolling_7_day_baseline",
                "label": "跟过去七天比较",
                "description": "用于平滑短期波动。",
                "recommended": False,
            },
            {
                "option_id": "tell_agent_differently",
                "label": "告诉分析助手采用其他方式",
                "description": "自由说明业务选择。",
                "recommended": False,
            },
        ]
    }
    start = {
        "schema_version": "single-authority-phase03-acceptance-start.v1",
        "acceptance_id": "phase03-focused-acceptance",
        "question": acceptance.QUESTION,
        "owner_id": acceptance.OWNER_ID,
        "thread_id": "thread-phase03-focused",
        "run_id": execution.run_attempt_id,
        "gateway_base_url": "http://127.0.0.1:3000",
        "review": {"clarification": clarification},
        "artifact_directory": str(output_dir.resolve()),
    }
    acceptance._write_new_json(output_dir / "case-b-start.json", start)
    observation = _execution_observation(execution)
    gateway_calls = []

    def submit(**kwargs):
        gateway_calls.append(kwargs)
        return observation

    monkeypatch.setattr(
        acceptance,
        "_submit_clarification_resolution",
        submit,
    )
    monkeypatch.setattr(
        acceptance.PostgresConversationStore,
        "from_env",
        lambda: wired.store,
    )
    monkeypatch.setattr(
        acceptance,
        "_review_terminal_publication",
        lambda **_kwargs: {
            "terminal_state": {
                "run_status": "completed",
                "publication_state": "published",
                "delivery_state": "published",
                "acceptance_status": "passed",
                "reason": "persisted_customer_publication_verified",
            }
        },
        raising=False,
    )

    exit_code = acceptance.run_case_b_execute(
        output_dir=output_dir,
        option_id="comparison_baseline.previous_day",
        gateway_base_url="http://127.0.0.1:3000",
        timeout_seconds=30,
        poll_interval_seconds=0.1,
    )

    assert exit_code == 0
    assert gateway_calls == [
        {
            "base_url": "http://127.0.0.1:3000",
            "user_id": acceptance.OWNER_ID,
            "run_id": execution.run_attempt_id,
            "answer": "跟前一天比较（推荐）",
            "selected_option_id": "comparison_baseline.previous_day",
            "request_identity": "phase03-execute-phase03-focused-acceptance",
            "timeout_seconds": 30,
            "poll_interval_seconds": 0.1,
        }
    ]
    record = json.loads(
        (output_dir / "case-b-execution.json").read_text(encoding="utf-8")
    )
    assert record["review"]["passed"] is True
    assert record["review"]["gateway"]["terminal_status"] == ("completed")
    assert (
        record["review"]["terminal_publication"]["terminal_state"]["acceptance_status"]
        == "passed"
    )


def test_case_b_observation_timeout_does_not_resubmit_or_write_terminal_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "single-authority-phase03" / "case-b-timeout"
    output_dir.mkdir(parents=True)
    start = {
        "schema_version": "single-authority-phase03-acceptance-start.v1",
        "acceptance_id": "phase03-timeout",
        "question": acceptance.QUESTION,
        "owner_id": acceptance.OWNER_ID,
        "thread_id": "thread-phase03-timeout",
        "run_id": "run-phase03-timeout",
        "gateway_base_url": "http://127.0.0.1:3107",
        "review": {
            "clarification": {
                "options": [
                    {
                        "option_id": "comparison_baseline.previous_day",
                        "label": "跟前一天比较（推荐）",
                        "description": "用于日变化解释。",
                        "recommended": True,
                    }
                ]
            }
        },
        "artifact_directory": str(output_dir.resolve()),
    }
    acceptance._write_new_json(output_dir / "case-b-start.json", start)
    submissions = []

    def submit(**kwargs):
        submissions.append(kwargs)
        return {
            "operation": "clarification_resolution",
            "source_run_id": start["run_id"],
            "run_id": start["run_id"],
            "events_url": f"/api/runs/{start['run_id']}/events",
            "checkpoint_reached": False,
            "terminal_status": "running_workflow",
            "business_acceptance": "not_evaluated",
            "timed_out": True,
            "poll_attempts": 4,
            "events": [],
        }

    monkeypatch.setattr(
        acceptance,
        "_submit_clarification_resolution",
        submit,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("incomplete_observation_must_stop_before_authority_read")

    monkeypatch.setattr(
        acceptance.PostgresConversationStore,
        "from_env",
        forbidden,
    )

    with pytest.raises(
        RuntimeError,
        match="phase03_acceptance_gateway_observation_incomplete",
    ):
        acceptance.run_case_b_execute(
            output_dir=output_dir,
            option_id="comparison_baseline.previous_day",
            gateway_base_url="http://127.0.0.1:3107",
            timeout_seconds=30,
            poll_interval_seconds=0.1,
        )

    assert len(submissions) == 1
    assert not (output_dir / "case-b-execution.json").exists()


@pytest.mark.parametrize(
    "terminal_status",
    (
        "delivery_retryable_failed",
        "delivery_permanently_failed",
        "narrative_failed",
        "publication_failed",
    ),
)
def test_case_b_records_typed_nonpublication_terminal_without_final_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    terminal_status: str,
) -> None:
    output_dir = tmp_path / "single-authority-phase03" / f"case-b-{terminal_status}"
    output_dir.mkdir(parents=True)
    start = {
        "schema_version": "single-authority-phase03-acceptance-start.v1",
        "acceptance_id": f"phase03-{terminal_status}",
        "question": acceptance.QUESTION,
        "owner_id": acceptance.OWNER_ID,
        "thread_id": "thread-phase03-terminal",
        "run_id": "run-phase03-terminal",
        "gateway_base_url": "http://127.0.0.1:3107",
        "review": {
            "clarification": {
                "options": [
                    {
                        "option_id": "comparison_baseline.previous_day",
                        "label": "跟前一天比较（推荐）",
                        "description": "用于日变化解释。",
                        "recommended": True,
                    }
                ]
            }
        },
        "artifact_directory": str(output_dir.resolve()),
    }
    acceptance._write_new_json(output_dir / "case-b-start.json", start)
    observation = _terminal_observation(start["run_id"], terminal_status)
    monkeypatch.setattr(
        acceptance,
        "_submit_clarification_resolution",
        lambda **_kwargs: observation,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("nonpublication_terminal_must_stop_before_final_review")

    monkeypatch.setattr(
        acceptance,
        "_execution_projection_from_events",
        forbidden,
    )
    monkeypatch.setattr(
        acceptance.PostgresConversationStore,
        "from_env",
        forbidden,
    )
    monkeypatch.setattr(
        acceptance,
        "_review_terminal_publication",
        forbidden,
    )

    exit_code = acceptance.run_case_b_execute(
        output_dir=output_dir,
        option_id="comparison_baseline.previous_day",
        gateway_base_url="http://127.0.0.1:3107",
        timeout_seconds=30,
        poll_interval_seconds=0.1,
    )

    assert exit_code == 4
    record = json.loads(
        (output_dir / "case-b-execution.json").read_text(encoding="utf-8")
    )
    assert record["schema_version"] == (
        "single-authority-phase03-acceptance-terminal-failure.v1"
    )
    assert record["public_execution_result"] is None
    assert record["review"]["passed"] is False
    assert record["review"]["terminal_failure"]["post_execution_status"] == (
        terminal_status
    )
    assert record["review"]["terminal_publication_review_performed"] is False
    assert "terminal_publication" not in record["review"]


@pytest.mark.parametrize("execution_stage_persisted", (False, True))
def test_case_b_records_failed_run_at_last_persisted_authority_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_stage_persisted: bool,
) -> None:
    output_dir = (
        tmp_path
        / "single-authority-phase03"
        / (
            "case-b-failed-with-evidence"
            if execution_stage_persisted
            else "case-b-failed-before-evidence"
        )
    )
    output_dir.mkdir(parents=True)
    start = {
        "schema_version": "single-authority-phase03-acceptance-start.v1",
        "acceptance_id": "phase03-failed-run",
        "question": acceptance.QUESTION,
        "owner_id": acceptance.OWNER_ID,
        "thread_id": "thread-phase03-failed",
        "run_id": "run-phase03-failed",
        "gateway_base_url": "http://127.0.0.1:3107",
        "review": {
            "clarification": {
                "options": [
                    {
                        "option_id": "comparison_baseline.previous_day",
                        "label": "跟前一天比较（推荐）",
                        "description": "用于日变化解释。",
                        "recommended": True,
                    }
                ]
            }
        },
        "artifact_directory": str(output_dir.resolve()),
    }
    acceptance._write_new_json(output_dir / "case-b-start.json", start)
    public_execution = {
        "schema_version": "single-authority-phase03.v1",
        "status": "evidence_ready",
    }
    events = (
        [
            {
                "event": "execution_result_ready",
                "payload": {
                    "status": "evidence_ready",
                    "execution_result": public_execution,
                },
            }
        ]
        if execution_stage_persisted
        else []
    )
    observation = {
        "checkpoint_reached": True,
        "run_status": "failed",
        "terminal_status": "failed",
        "publication_state": "not_ready",
        "delivery_state": "pending",
        "business_acceptance": "failed",
        "customer_publication": None,
        "publication": None,
        "timed_out": False,
        "run_id": start["run_id"],
        "events": events,
    }
    monkeypatch.setattr(
        acceptance,
        "_submit_clarification_resolution",
        lambda **_kwargs: observation,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("failed_run_must_stop_before_publication_review")

    monkeypatch.setattr(
        acceptance.PostgresConversationStore,
        "from_env",
        forbidden,
    )
    monkeypatch.setattr(
        acceptance,
        "_review_terminal_publication",
        forbidden,
    )

    exit_code = acceptance.run_case_b_execute(
        output_dir=output_dir,
        option_id="comparison_baseline.previous_day",
        gateway_base_url="http://127.0.0.1:3107",
        timeout_seconds=30,
        poll_interval_seconds=0.1,
    )

    assert exit_code == 4
    record = json.loads(
        (output_dir / "case-b-execution.json").read_text(encoding="utf-8")
    )
    assert record["schema_version"] == (
        "single-authority-phase03-acceptance-run-failure.v1"
    )
    assert record["public_execution_result"] == (
        public_execution if execution_stage_persisted else None
    )
    assert record["review"]["terminal_failure"]["last_authoritative_stage"] == (
        "evidence_ready" if execution_stage_persisted else "pre_evidence"
    )
    assert record["review"]["terminal_publication_review_performed"] is False


def test_phase03_artifacts_cannot_overlap_phase01_or_phase02(tmp_path: Path) -> None:
    assert acceptance.DEFAULT_ROOT not in {
        acceptance.PHASE01_ROOT,
        acceptance.PHASE02_ROOT,
    }
    assert acceptance._phase03_artifact_path(tmp_path) == tmp_path.resolve()
    for legacy in (acceptance.PHASE01_ROOT, acceptance.PHASE02_ROOT):
        with pytest.raises(
            ValueError,
            match="phase03_acceptance_artifact_root_reserved",
        ):
            acceptance._phase03_artifact_path(legacy)
        with pytest.raises(
            ValueError,
            match="phase03_acceptance_artifact_root_reserved",
        ):
            acceptance._phase03_artifact_path(legacy / "nested")


def test_runner_live_path_uses_gateway_without_direct_core_invocation() -> None:
    source = Path(acceptance.__file__).read_text(encoding="utf-8")

    assert "_run_first_turn(" in source
    assert "_submit_clarification_resolution(" in source
    assert "ConversationAgentCore" not in source
    assert "stop_after_phase" not in source
