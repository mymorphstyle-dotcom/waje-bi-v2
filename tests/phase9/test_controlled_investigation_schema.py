from __future__ import annotations

from pathlib import Path

from bi_agent.runtime.durable_call_journal import (
    DurableCallSpec,
    PROVIDER_CALL_KINDS,
)


ROOT = Path(__file__).resolve().parents[2]


def test_controlled_investigation_provider_is_plan_scoped_and_durable() -> None:
    spec = DurableCallSpec.create(
        run_attempt_id="run-1",
        intent_revision_id="intent-1",
        plan_revision_id="plan-1",
        task_id=None,
        stage_name="compose_claim_aware_narrative",
        call_kind="controlled_investigation_provider",
        operation_name="controlled-investigation:one",
        input_ref="controlled-investigation-input:one",
        input_payload={"allowedSourceRefs": ["c_1"]},
    )

    assert spec.call_kind in PROVIDER_CALL_KINDS
    assert spec.intent_revision_id == "intent-1"
    assert spec.plan_revision_id == "plan-1"
    assert spec.task_id is None


def test_schema_has_parent_operation_and_leased_child_dispatch() -> None:
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
        encoding="utf-8"
    )

    assert (
        "CREATE TABLE IF NOT EXISTS "
        "waje_runtime.controlled_investigation_operations"
    ) in schema
    assert (
        "CREATE TABLE IF NOT EXISTS "
        "waje_runtime.controlled_investigation_dispatches"
    ) in schema
    for field in (
        "operation_ref",
        "run_attempt_id",
        "intent_revision_id",
        "plan_revision_id",
        "authority_context_ref",
        "authority_bundle_ref",
        "source_material_projection_ref",
        "input_digest",
        "child_run_id",
        "investigation_ref",
        "investigation_key",
        "axis_refs",
        "allowed_source_refs",
        "allowed_source_set_digest",
        "dispatch_state",
        "lease_owner_id",
        "lease_epoch",
        "lease_expires_at",
        "accepted_attempt_ref",
        "accepted_artifact_ref",
    ):
        assert field in schema
    assert "'controlled_investigation_provider'" in schema
    assert "idx_controlled_investigation_dispatch_recovery" in schema


def test_schema_keeps_child_dispatch_operational_and_artifacts_customer_safe() -> None:
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
        encoding="utf-8"
    )

    assert "visibility_policy_ref" in schema
    assert "visibility:customer-safe" in schema
    assert "controlled_investigation_dispatch_identity_immutable" in schema
    assert "technical_detail_ref" in schema
