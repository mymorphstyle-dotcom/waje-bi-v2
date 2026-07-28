from __future__ import annotations

from bi_agent.runtime.controlled_investigation_runtime import (
    ControlledInvestigationOperation,
    ControlledInvestigationProposal,
    InMemoryControlledInvestigationStore,
    admit_controlled_investigations,
)


def _records():
    operation = ControlledInvestigationOperation.create(
        owner_ref="owner-1",
        thread_ref="thread-1",
        run_attempt_id="run-1",
        intent_revision_id="intent-1",
        plan_revision_id="plan-1",
        authority_context_ref="authority-context:one",
        authority_bundle_ref="authority-bundle:one",
        parent_transition_id="transition:authority",
        source_material_projection_ref="narrative-material-projection:one",
        source_material_projection_digest="a" * 64,
    )
    proposal = ControlledInvestigationProposal.model_validate(
        {
            "investigations": [
                {
                    "investigationKey": "one",
                    "question": "独立复核第一个分析轴。",
                    "axisRefs": ["axis:one"],
                    "sourceRefs": ["c_1"],
                    "expectedOutputKind": "mechanism_explanation",
                },
                {
                    "investigationKey": "two",
                    "question": "独立复核第二个分析轴。",
                    "axisRefs": ["axis:two"],
                    "sourceRefs": ["c_2"],
                    "expectedOutputKind": "structure_concentration",
                },
                {
                    "investigationKey": "three",
                    "question": "独立复核第三个分析轴。",
                    "axisRefs": ["axis:three"],
                    "sourceRefs": ["c_3"],
                    "expectedOutputKind": "alternative_explanation",
                },
            ]
        }
    )
    admission = admit_controlled_investigations(
        operation=operation,
        proposal=proposal,
        accepted_axis_refs=("axis:one", "axis:two", "axis:three"),
        allowed_source_refs=("c_1", "c_2", "c_3"),
    )
    return operation, admission.accepted


def test_restart_replays_completed_child_and_reclaims_expired_lease() -> None:
    operation, investigations = _records()
    store = InMemoryControlledInvestigationStore()
    store.ensure_operation(operation, investigations)

    first = store.claim_next(
        operation.operation_ref,
        worker_id="worker-a",
        now="2026-07-24T00:00:00+00:00",
        lease_expires_at="2026-07-24T00:01:00+00:00",
    )
    second = store.claim_next(
        operation.operation_ref,
        worker_id="worker-a",
        now="2026-07-24T00:00:00+00:00",
        lease_expires_at="2026-07-24T00:01:00+00:00",
    )
    assert first is not None and second is not None

    store.complete(
        first.investigation_ref,
        worker_id="worker-a",
        artifact_ref="controlled-investigation-artifact:one",
        output_digest="a" * 64,
    )

    recovered = store.claim_next(
        operation.operation_ref,
        worker_id="worker-b",
        now="2026-07-24T00:02:00+00:00",
        lease_expires_at="2026-07-24T00:03:00+00:00",
    )
    assert recovered is not None
    assert recovered.investigation_ref == second.investigation_ref
    assert recovered.attempt_number == 2
    assert (
        store.snapshot(first.investigation_ref).accepted_artifact_ref
        == "controlled-investigation-artifact:one"
    )


def test_duplicate_dispatch_and_child_input_accept_one_logical_result() -> None:
    operation, investigations = _records()
    store = InMemoryControlledInvestigationStore()
    first = store.ensure_operation(operation, investigations)
    replay = store.ensure_operation(operation, investigations)

    assert first == replay
    claimed = store.claim_next(
        operation.operation_ref,
        worker_id="worker-a",
        now="2026-07-24T00:00:00+00:00",
        lease_expires_at="2026-07-24T00:01:00+00:00",
    )
    assert claimed is not None
    completed = store.complete(
        claimed.investigation_ref,
        worker_id="worker-a",
        artifact_ref="controlled-investigation-artifact:one",
        output_digest="a" * 64,
    )
    replayed = store.complete(
        claimed.investigation_ref,
        worker_id="worker-a",
        artifact_ref="controlled-investigation-artifact:one",
        output_digest="a" * 64,
    )

    assert completed == replayed
    assert completed.attempt_number == 1


def test_one_child_failure_is_local_and_other_results_settle() -> None:
    operation, investigations = _records()
    store = InMemoryControlledInvestigationStore()
    store.ensure_operation(operation, investigations)
    claimed = [
        store.claim_next(
            operation.operation_ref,
            worker_id="worker-a",
            now="2026-07-24T00:00:00+00:00",
            lease_expires_at="2026-07-24T00:01:00+00:00",
        )
        for _ in investigations
    ]
    assert all(item is not None for item in claimed)

    store.complete(
        claimed[0].investigation_ref,
        worker_id="worker-a",
        artifact_ref="controlled-investigation-artifact:one",
        output_digest="a" * 64,
    )
    store.fail(
        claimed[1].investigation_ref,
        worker_id="worker-a",
        failure_code="provider_unavailable",
        retryability="not_retryable",
        technical_detail_ref="technical-detail:server-only",
    )
    store.complete(
        claimed[2].investigation_ref,
        worker_id="worker-a",
        artifact_ref="controlled-investigation-artifact:three",
        output_digest="c" * 64,
    )

    settled = store.settle_operation(operation.operation_ref)
    assert settled.status == "completed_with_limits"
    assert len(settled.accepted_artifact_refs) == 2
    assert settled.failed_investigation_count == 1
    assert "technical-detail:server-only" not in str(settled.customer_projection())


def test_cancellation_only_stops_unfinished_children() -> None:
    operation, investigations = _records()
    store = InMemoryControlledInvestigationStore()
    store.ensure_operation(operation, investigations)
    claimed = store.claim_next(
        operation.operation_ref,
        worker_id="worker-a",
        now="2026-07-24T00:00:00+00:00",
        lease_expires_at="2026-07-24T00:01:00+00:00",
    )
    assert claimed is not None
    store.complete(
        claimed.investigation_ref,
        worker_id="worker-a",
        artifact_ref="controlled-investigation-artifact:one",
        output_digest="a" * 64,
    )

    cancelled = store.cancel_unfinished(operation.operation_ref)
    assert cancelled == 2
    assert store.snapshot(claimed.investigation_ref).status == "completed"
    assert store.settle_operation(operation.operation_ref).status == (
        "completed_with_limits"
    )
