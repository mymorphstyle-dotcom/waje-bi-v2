from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Callable, Mapping, Sequence

from psycopg import Error as PsycopgError

from bi_agent.runtime.claim_authority import AuthorityBundle, RecommendationRecord
from bi_agent.runtime.claim_settlement import AuthorityBundleInputs, ClaimSettlement
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.durable_call_journal import DurableCallJournal
from bi_agent.runtime.narrative_authority import (
    PublicationFieldVisibilityPolicy,
    RestrictedProviderResponse,
)
from bi_agent.runtime.narrative_workflow import (
    NarrativeQualityAuditResult,
    NarrativeWorkflowResult,
)
from bi_agent.runtime.narrative_material_projection import (
    NarrativeMaterialProjection,
)
from bi_agent.runtime.publication_authority import (
    DeliveryAttempt,
    DeliveryOutboxRecord,
    PublicationProjection,
    PublicationRevision,
    validate_publication_lifecycle,
)
from bi_agent.runtime.publication_flow import (
    PublicationFlowResult,
    ValidatedPublicationFlowContext,
    validate_publication_flow_context,
    validate_publication_flow_in_context,
)
from bi_agent.runtime.single_authority import (
    DurableTransition,
    LifecycleState,
)


class PublicationPersistenceError(ValueError):
    pass


class PublicationPersistenceOperationalError(RuntimeError):
    """The durable publication write could not complete for an external reason."""

    def __init__(self, *, technical_detail_ref: str) -> None:
        if (
            not isinstance(technical_detail_ref, str)
            or not technical_detail_ref.startswith("technical-detail:sha256:")
            or len(technical_detail_ref.removeprefix("technical-detail:sha256:")) != 64
        ):
            raise PublicationPersistenceError(
                "publication_operational_detail_ref_invalid"
            )
        super().__init__("publication_persistence_unavailable")
        self.technical_detail_ref = technical_detail_ref


class PublicationPersistenceBackendError(RuntimeError):
    """Typed adapter signal for a durable storage operation failure."""

    pass


@dataclass(frozen=True)
class PublicationPersistenceResult:
    narrative_workflow_ref: str
    narrative_workflow_digest: str
    transition_id: str
    publication_ref: str | None
    outbox_ref: str | None
    customer_payload_ref: str | None
    publication_state: str
    status: str
    lifecycle_state_digest: str


@dataclass(frozen=True)
class NarrativeQualityAuditPersistenceResult:
    audit_result_ref: str
    verifier_report_ref: str
    audit_status: str
    status: str


@dataclass(frozen=True)
class DeliveryMessage:
    outbox_ref: str
    destination_ref: str
    channel: str
    idempotency_key: str
    customer_payload: Mapping[str, Any]


@dataclass(frozen=True)
class DeliveryTransportResult:
    status: str
    transport_receipt_ref: str | None
    failure_code: str | None

    @classmethod
    def published(cls, transport_receipt_ref: str) -> "DeliveryTransportResult":
        return cls._create(
            status="published",
            transport_receipt_ref=transport_receipt_ref,
            failure_code=None,
        )

    @classmethod
    def failed(
        cls,
        *,
        retryable: bool,
        failure_code: str,
    ) -> "DeliveryTransportResult":
        return cls._create(
            status="retryable_failed" if retryable else "permanently_failed",
            transport_receipt_ref=None,
            failure_code=failure_code,
        )

    @classmethod
    def _create(
        cls,
        *,
        status: str,
        transport_receipt_ref: str | None,
        failure_code: str | None,
    ) -> "DeliveryTransportResult":
        if status not in {"published", "retryable_failed", "permanently_failed"}:
            raise PublicationPersistenceError("delivery_transport_status_invalid")
        receipt = _optional_string(
            transport_receipt_ref,
            "delivery_transport_receipt_invalid",
        )
        failure = _optional_string(failure_code, "delivery_transport_failure_invalid")
        if (status == "published" and (receipt is None or failure is not None)) or (
            status != "published" and (receipt is not None or failure is None)
        ):
            raise PublicationPersistenceError("delivery_transport_result_invalid")
        return cls(
            status=status,
            transport_receipt_ref=receipt,
            failure_code=failure,
        )


@dataclass(frozen=True)
class DeliveryPersistenceResult:
    outbox_ref: str
    attempt_ref: str
    status: str
    lifecycle_state_digest: str
    customer_publication_ref: str | None
    replayed: bool


@dataclass(frozen=True)
class _CustomerPayloadRecord:
    customer_payload_ref: str
    run_attempt_id: str
    outbox_ref: str
    publication_ref: str
    publication_digest: str
    projection_id: str
    projection_digest: str
    field_visibility_policy_ref: str
    field_visibility_policy_digest: str
    customer_payload_digest: str
    customer_payload: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        outbox: DeliveryOutboxRecord,
        projection: PublicationProjection,
        visibility_policy: PublicationFieldVisibilityPolicy,
        customer_payload: Mapping[str, Any],
    ) -> "_CustomerPayloadRecord":
        normalized_payload = canonical_value(customer_payload)
        visibility_policy.validate_customer_payload(normalized_payload)
        if (
            outbox.projection_id != projection.projection_id
            or outbox.projection_digest != projection.projection_digest
            or projection.field_visibility_policy_ref != visibility_policy.policy_ref
            or projection.field_visibility_policy_digest
            != visibility_policy.content_digest
        ):
            raise PublicationPersistenceError("customer_payload_source_conflict")
        payload_digest = canonical_digest(normalized_payload)
        body = {
            "run_attempt_id": outbox.run_attempt_id,
            "outbox_ref": outbox.outbox_ref,
            "publication_ref": outbox.publication_ref,
            "publication_digest": outbox.publication_digest,
            "projection_id": outbox.projection_id,
            "projection_digest": outbox.projection_digest,
            "field_visibility_policy_ref": visibility_policy.policy_ref,
            "field_visibility_policy_digest": visibility_policy.content_digest,
            "customer_payload_digest": payload_digest,
            "customer_payload": normalized_payload,
        }
        digest = canonical_digest(body)
        return cls(
            customer_payload_ref="customer-payload:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "_CustomerPayloadRecord":
        if not isinstance(payload, Mapping) or set(payload) != {
            "customer_payload_ref",
            "run_attempt_id",
            "outbox_ref",
            "publication_ref",
            "publication_digest",
            "projection_id",
            "projection_digest",
            "field_visibility_policy_ref",
            "field_visibility_policy_digest",
            "customer_payload_digest",
            "customer_payload",
            "content_digest",
        }:
            raise PublicationPersistenceError("customer_payload_record_shape_invalid")
        body = {
            key: canonical_value(value)
            for key, value in payload.items()
            if key not in {"customer_payload_ref", "content_digest"}
        }
        digest = canonical_digest(body)
        if (
            payload["content_digest"] != digest
            or payload["customer_payload_ref"] != "customer-payload:sha256:" + digest
            or payload["customer_payload_digest"]
            != canonical_digest(payload["customer_payload"])
        ):
            raise PublicationPersistenceError(
                "customer_payload_record_integrity_invalid"
            )
        return cls(
            customer_payload_ref=str(payload["customer_payload_ref"]),
            run_attempt_id=str(payload["run_attempt_id"]),
            outbox_ref=str(payload["outbox_ref"]),
            publication_ref=str(payload["publication_ref"]),
            publication_digest=str(payload["publication_digest"]),
            projection_id=str(payload["projection_id"]),
            projection_digest=str(payload["projection_digest"]),
            field_visibility_policy_ref=str(payload["field_visibility_policy_ref"]),
            field_visibility_policy_digest=str(
                payload["field_visibility_policy_digest"]
            ),
            customer_payload_digest=str(payload["customer_payload_digest"]),
            customer_payload=canonical_value(payload["customer_payload"]),
            content_digest=str(payload["content_digest"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class _CustomerPublicationRecord:
    customer_publication_ref: str
    run_attempt_id: str
    outbox_ref: str
    delivery_attempt_ref: str
    publication_ref: str
    projection_id: str
    destination_ref: str
    channel: str
    transport_receipt_ref: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        outbox: DeliveryOutboxRecord,
        attempt: DeliveryAttempt,
    ) -> "_CustomerPublicationRecord":
        if (
            attempt.status != "published"
            or attempt.transport_receipt_ref is None
            or attempt.outbox_ref != outbox.outbox_ref
        ):
            raise PublicationPersistenceError("customer_publication_attempt_invalid")
        body = {
            "run_attempt_id": outbox.run_attempt_id,
            "outbox_ref": outbox.outbox_ref,
            "delivery_attempt_ref": attempt.attempt_ref,
            "publication_ref": outbox.publication_ref,
            "projection_id": outbox.projection_id,
            "destination_ref": outbox.destination_ref,
            "channel": outbox.channel,
            "transport_receipt_ref": attempt.transport_receipt_ref,
        }
        digest = canonical_digest(body)
        return cls(
            customer_publication_ref="customer-publication:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "_CustomerPublicationRecord":
        expected = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != expected:
            raise PublicationPersistenceError("customer_publication_shape_invalid")
        body = {
            key: value
            for key, value in payload.items()
            if key not in {"customer_publication_ref", "content_digest"}
        }
        digest = canonical_digest(body)
        if (
            payload["content_digest"] != digest
            or payload["customer_publication_ref"]
            != "customer-publication:sha256:" + digest
        ):
            raise PublicationPersistenceError("customer_publication_integrity_invalid")
        return cls(
            customer_publication_ref=_required_string(
                payload["customer_publication_ref"],
                "customer_publication_integrity_invalid",
            ),
            run_attempt_id=_required_string(
                payload["run_attempt_id"], "customer_publication_integrity_invalid"
            ),
            outbox_ref=_required_string(
                payload["outbox_ref"], "customer_publication_integrity_invalid"
            ),
            delivery_attempt_ref=_required_string(
                payload["delivery_attempt_ref"],
                "customer_publication_integrity_invalid",
            ),
            publication_ref=_required_string(
                payload["publication_ref"], "customer_publication_integrity_invalid"
            ),
            projection_id=_required_string(
                payload["projection_id"], "customer_publication_integrity_invalid"
            ),
            destination_ref=_required_string(
                payload["destination_ref"], "customer_publication_integrity_invalid"
            ),
            channel=_required_string(
                payload["channel"], "customer_publication_integrity_invalid"
            ),
            transport_receipt_ref=_required_string(
                payload["transport_receipt_ref"],
                "customer_publication_integrity_invalid",
            ),
            content_digest=_required_string(
                payload["content_digest"], "customer_publication_integrity_invalid"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class _InsertRecord:
    table: str
    identity_column: str
    columns: Mapping[str, Any]
    json_columns: frozenset[str] = frozenset({"payload"})
    conflict_columns: tuple[str, ...] = ()


_PUBLICATION_PREFLIGHT_SQL = """
/* publication_persistence_preflight */
SELECT
  thread.owner_id AS owner_ref,
  run.thread_id AS thread_ref,
  bundle.payload AS authority_bundle_payload,
  bundle.bundle_digest AS authority_bundle_digest,
  to_jsonb(parent_transition)
    - 'input_payload'
    - 'output_payload'
    - 'failure_ref'
    - 'created_at' AS parent_transition_payload,
  parent_transition.input_payload AS parent_transition_input_payload,
  parent_transition.output_payload AS parent_transition_output_payload,
  lifecycle.payload AS lifecycle_payload,
  to_jsonb(existing_transition)
    - 'input_payload'
    - 'output_payload'
    - 'failure_ref'
    - 'created_at' AS existing_transition_payload,
  existing_transition.input_payload AS existing_transition_input_payload,
  existing_transition.output_payload AS existing_transition_output_payload,
  expected_publication.payload AS expected_publication_payload,
  latest_publication.payload AS latest_publication_payload,
  expected_outbox.payload AS expected_outbox_payload,
  expected_customer_payload.payload AS expected_customer_payload,
  material_projection.payload AS material_projection_payload,
  material_projection.content_digest AS material_projection_digest,
  material_projection.palette_ref AS material_projection_palette_ref,
  material_projection.palette_digest AS material_projection_palette_digest,
  material_projection.claim_settlement_ref AS material_projection_settlement_ref,
  material_projection.claim_settlement_digest AS material_projection_settlement_digest
FROM waje_runtime.analysis_runs run
JOIN waje_runtime.investigation_threads thread
  ON thread.thread_id = run.thread_id
JOIN waje_runtime.authority_bundles bundle
  ON bundle.bundle_ref = %(authority_bundle_ref)s
 AND bundle.run_attempt_id = run.run_id
 AND bundle.seal_state = 'sealed'
JOIN waje_runtime.workflow_transition_attempts parent_transition
  ON parent_transition.transition_id = %(parent_transition_id)s
 AND parent_transition.run_attempt_id = run.run_id
 AND parent_transition.node_name = 'settle_claim_authority'
 AND parent_transition.status = 'succeeded'
 AND parent_transition.acceptance_state = 'accepted'
JOIN waje_runtime.narrative_material_projections material_projection
  ON material_projection.projection_ref = %(material_projection_ref)s
 AND material_projection.owner_ref = thread.owner_id
 AND material_projection.run_attempt_id = run.run_id
JOIN LATERAL (
  SELECT state.payload
  FROM waje_runtime.run_lifecycle_state_revisions state
  WHERE state.run_attempt_id = run.run_id
  ORDER BY state.state_revision DESC
  LIMIT 1
) lifecycle ON TRUE
LEFT JOIN waje_runtime.workflow_transition_attempts existing_transition
  ON existing_transition.attempt_id = %(compose_attempt_id)s
 AND existing_transition.run_attempt_id = run.run_id
LEFT JOIN waje_runtime.publication_revisions expected_publication
  ON expected_publication.publication_ref = %(publication_ref)s
 AND expected_publication.run_attempt_id = run.run_id
LEFT JOIN LATERAL (
  SELECT publication.payload
  FROM waje_runtime.publication_revisions publication
  WHERE publication.run_attempt_id = run.run_id
  ORDER BY publication.revision DESC
  LIMIT 1
) latest_publication ON TRUE
LEFT JOIN waje_runtime.delivery_outbox_records expected_outbox
  ON expected_outbox.outbox_ref = %(outbox_ref)s
 AND expected_outbox.run_attempt_id = run.run_id
LEFT JOIN waje_runtime.publication_customer_payloads expected_customer_payload
  ON expected_customer_payload.outbox_ref = %(outbox_ref)s
 AND expected_customer_payload.run_attempt_id = run.run_id
WHERE run.run_id = %(run_attempt_id)s
  AND run.run_attempt_id = %(run_attempt_id)s
FOR UPDATE OF run
"""


def narrative_publication_transition_payloads(
    *,
    authority_inputs: AuthorityBundleInputs,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
    narrative_workflow: NarrativeWorkflowResult,
    publication_flow: PublicationFlowResult,
    supersedes_publication: PublicationRevision | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    context = _validated_publication_context(
        authority_inputs=authority_inputs,
        authority_bundle=authority_bundle,
        claim_settlement=claim_settlement,
        recommendations=recommendations,
        narrative_workflow=narrative_workflow,
    )
    flow = _validated_flow_for_context(
        context=context,
        publication_flow=publication_flow,
        supersedes_publication=supersedes_publication,
    )
    return _transition_payloads_from_validated(
        context=context,
        publication_flow=flow,
    )


def _validated_publication_context(
    *,
    authority_inputs: AuthorityBundleInputs,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
    narrative_workflow: NarrativeWorkflowResult,
) -> ValidatedPublicationFlowContext:
    try:
        return validate_publication_flow_context(
            authority_inputs=authority_inputs,
            authority_bundle=authority_bundle,
            claim_settlement=claim_settlement,
            recommendations=recommendations,
            narrative_workflow=narrative_workflow,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicationPersistenceError(
            "publication_validation_context_invalid"
        ) from exc


def _validated_flow_for_context(
    *,
    context: ValidatedPublicationFlowContext,
    publication_flow: PublicationFlowResult,
    supersedes_publication: PublicationRevision | None,
) -> PublicationFlowResult:
    if type(publication_flow) is not PublicationFlowResult:
        raise PublicationPersistenceError("publication_flow_missing")
    try:
        flow = validate_publication_flow_in_context(
            publication_flow,
            context=context,
            supersedes_publication=supersedes_publication,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicationPersistenceError("publication_flow_invalid") from exc
    if flow != publication_flow:
        raise PublicationPersistenceError("publication_flow_invalid")
    return flow


def _transition_payloads_from_validated(
    *,
    context: ValidatedPublicationFlowContext,
    publication_flow: PublicationFlowResult,
) -> tuple[dict[str, Any], dict[str, Any]]:
    authority_bundle = context.authority_bundle
    claim_settlement = context.claim_settlement
    workflow = context.narrative_workflow
    input_payload = {
        "authority_bundle_ref": authority_bundle.bundle_ref,
        "authority_bundle_digest": authority_bundle.bundle_digest,
        "claim_settlement_ref": claim_settlement.settlement_ref,
        "claim_settlement_digest": claim_settlement.content_digest,
        "recommendation_refs": tuple(
            item.recommendation_ref for item in context.recommendations
        ),
        "narrative_material_projection_ref": (
            workflow.material_projection.projection_ref
        ),
        "narrative_material_projection_digest": (
            workflow.material_projection.content_digest
        ),
        "visibility_policy_ref": workflow.visibility_policy.policy_ref,
        "visibility_policy_digest": workflow.visibility_policy.content_digest,
        "answer_context_ref": workflow.answer_context.context_ref,
        "answer_context_digest": workflow.answer_context.content_digest,
    }
    output_payload = {
        "narrative_workflow_result": workflow.to_dict(),
        "publication_flow": publication_flow.to_dict(),
        "publication_state": "ready",
    }
    return canonical_value(input_payload), canonical_value(output_payload)


def persist_publication(
    connection: Any,
    *,
    owner_ref: str,
    thread_ref: str,
    authority_inputs: AuthorityBundleInputs,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
    narrative_workflow: NarrativeWorkflowResult,
    publication_flow: PublicationFlowResult,
    supersedes_publication: PublicationRevision | None,
    compose_transition: DurableTransition,
    attempt_journal: DurableCallJournal,
    accepted_attempt_refs: Sequence[str],
) -> PublicationPersistenceResult:
    return _persist_narrative_chain(
        connection,
        owner_ref=owner_ref,
        thread_ref=thread_ref,
        authority_inputs=authority_inputs,
        authority_bundle=authority_bundle,
        claim_settlement=claim_settlement,
        recommendations=recommendations,
        narrative_workflow=narrative_workflow,
        publication_flow=publication_flow,
        supersedes_publication=supersedes_publication,
        compose_transition=compose_transition,
        attempt_journal=attempt_journal,
        accepted_attempt_refs=accepted_attempt_refs,
    )


def _persist_narrative_chain(
    connection: Any,
    *,
    owner_ref: str,
    thread_ref: str,
    authority_inputs: AuthorityBundleInputs,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
    narrative_workflow: NarrativeWorkflowResult,
    publication_flow: PublicationFlowResult,
    supersedes_publication: PublicationRevision | None,
    compose_transition: DurableTransition,
    attempt_journal: DurableCallJournal,
    accepted_attempt_refs: Sequence[str],
) -> PublicationPersistenceResult:
    owner = _required_string(owner_ref, "publication_owner_ref_invalid")
    thread = _required_string(thread_ref, "publication_thread_ref_invalid")
    if not isinstance(attempt_journal, DurableCallJournal):
        raise PublicationPersistenceError("publication_attempt_journal_invalid")
    if isinstance(accepted_attempt_refs, (str, bytes)):
        raise PublicationPersistenceError("publication_attempt_refs_invalid")
    normalized_attempt_refs = tuple(accepted_attempt_refs)
    workflow, flow, transition, transition_input, transition_output = (
        _validated_persistence_inputs(
            authority_inputs=authority_inputs,
            authority_bundle=authority_bundle,
            claim_settlement=claim_settlement,
            recommendations=recommendations,
            narrative_workflow=narrative_workflow,
            publication_flow=publication_flow,
            supersedes_publication=supersedes_publication,
            compose_transition=compose_transition,
        )
    )
    run_attempt_id = authority_bundle.run_attempt_id
    customer_record = _CustomerPayloadRecord.create(
        outbox=flow.outbox,
        projection=flow.projection,
        visibility_policy=workflow.visibility_policy,
        customer_payload=flow.customer_payload,
    )
    publication_ref = flow.publication.publication_ref
    outbox_ref = flow.outbox.outbox_ref
    try:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": f"single_authority:{run_attempt_id}"},
        )
        row = connection.execute(
            _PUBLICATION_PREFLIGHT_SQL,
            {
                "run_attempt_id": run_attempt_id,
                "authority_bundle_ref": authority_bundle.bundle_ref,
                "material_projection_ref": (
                    workflow.material_projection.projection_ref
                ),
                "parent_transition_id": transition.parent_transition_id,
                "compose_attempt_id": transition.attempt_id,
                "publication_ref": publication_ref,
                "outbox_ref": outbox_ref,
            },
        ).fetchone()
        if row is None:
            raise PublicationPersistenceError("publication_active_chain_missing")
        lifecycle, replaying = _validate_publication_preflight(
            row,
            owner_ref=owner,
            thread_ref=thread,
            authority_bundle=authority_bundle,
            material_projection=workflow.material_projection,
            compose_transition=transition,
            transition_input=transition_input,
            transition_output=transition_output,
            publication_flow=flow,
            supersedes_publication=supersedes_publication,
            customer_record=customer_record,
        )
        for record in _publication_records(
            owner_ref=owner,
            authority_bundle=authority_bundle,
            narrative_workflow=workflow,
            publication_flow=flow,
            customer_record=customer_record,
        ):
            _insert_exact(connection, record)
        _insert_exact(
            connection,
            _compose_transition_record(
                transition=transition,
                input_payload=transition_input,
                output_payload=transition_output,
            ),
        )
        final_lifecycle = lifecycle
        if not replaying:
            final_lifecycle = _persist_publication_lifecycle(
                connection,
                lifecycle=lifecycle,
                authority_bundle=authority_bundle,
                publication=flow.publication,
                outbox=flow.outbox,
            )
        attempt_journal.bind_stage(
            run_attempt_id=run_attempt_id,
            transition_attempt_id=transition.attempt_id,
            stage_name="compose_claim_aware_narrative",
            attempt_refs=normalized_attempt_refs,
            commit=False,
        )
        connection.commit()
        workflow_ref = "narrative-workflow-result:sha256:" + workflow.content_digest
        return PublicationPersistenceResult(
            narrative_workflow_ref=workflow_ref,
            narrative_workflow_digest=workflow.content_digest,
            transition_id=transition.transition_id,
            publication_ref=publication_ref,
            outbox_ref=outbox_ref,
            customer_payload_ref=customer_record.customer_payload_ref,
            publication_state="ready",
            status="replayed" if replaying else "inserted",
            lifecycle_state_digest=final_lifecycle.content_digest,
        )
    except PublicationPersistenceError:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        if not isinstance(
            exc,
            (PublicationPersistenceBackendError, PsycopgError),
        ):
            raise
        detail_digest = canonical_digest(
            {
                "run_attempt_id": run_attempt_id,
                "authority_bundle_ref": authority_bundle.bundle_ref,
                "compose_transition_id": transition.transition_id,
                "exception_type": type(exc).__name__,
            }
        )
        raise PublicationPersistenceOperationalError(
            technical_detail_ref="technical-detail:sha256:" + detail_digest
        ) from exc


def _validated_persistence_inputs(
    *,
    authority_inputs: AuthorityBundleInputs,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
    narrative_workflow: NarrativeWorkflowResult,
    publication_flow: PublicationFlowResult,
    supersedes_publication: PublicationRevision | None,
    compose_transition: DurableTransition,
) -> tuple[
    NarrativeWorkflowResult,
    PublicationFlowResult,
    DurableTransition,
    Mapping[str, Any],
    Mapping[str, Any],
]:
    context = _validated_publication_context(
        authority_inputs=authority_inputs,
        authority_bundle=authority_bundle,
        claim_settlement=claim_settlement,
        recommendations=recommendations,
        narrative_workflow=narrative_workflow,
    )
    flow = _validated_flow_for_context(
        context=context,
        publication_flow=publication_flow,
        supersedes_publication=supersedes_publication,
    )
    transition_input, transition_output = _transition_payloads_from_validated(
        context=context,
        publication_flow=flow,
    )
    if type(compose_transition) is not DurableTransition:
        raise PublicationPersistenceError("publication_compose_transition_invalid")
    try:
        transition = DurableTransition.from_dict(compose_transition.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicationPersistenceError(
            "publication_compose_transition_invalid"
        ) from exc
    if (
        transition != compose_transition
        or transition.node_name != "compose_claim_aware_narrative"
        or transition.parent_transition_id is None
        or transition.run_attempt_id != authority_bundle.run_attempt_id
        or transition.intent_revision_id != authority_bundle.intent_revision_id
        or transition.input_digest != canonical_digest(transition_input)
        or transition.output_digest != canonical_digest(transition_output)
        or transition.status != "succeeded"
        or transition.acceptance_state != "accepted"
        or transition.next_transition != "deliver_publication"
    ):
        raise PublicationPersistenceError("publication_compose_transition_invalid")
    return (
        context.narrative_workflow,
        flow,
        transition,
        transition_input,
        transition_output,
    )


def _validate_publication_preflight(
    row: Any,
    *,
    owner_ref: str,
    thread_ref: str,
    authority_bundle: AuthorityBundle,
    material_projection: NarrativeMaterialProjection,
    compose_transition: DurableTransition,
    transition_input: Mapping[str, Any],
    transition_output: Mapping[str, Any],
    publication_flow: PublicationFlowResult,
    supersedes_publication: PublicationRevision | None,
    customer_record: _CustomerPayloadRecord,
) -> tuple[LifecycleState, bool]:
    if (
        str(_field(row, "owner_ref", 0) or "") != owner_ref
        or str(_field(row, "thread_ref", 1) or "") != thread_ref
    ):
        raise PublicationPersistenceError("publication_owner_scope_conflict")
    stored_bundle = _payload(_field(row, "authority_bundle_payload", 2))
    if (
        canonical_value(stored_bundle) != canonical_value(authority_bundle.to_dict())
        or str(_field(row, "authority_bundle_digest", 3) or "")
        != authority_bundle.bundle_digest
    ):
        raise PublicationPersistenceError("publication_authority_bundle_conflict")
    if type(material_projection) is not NarrativeMaterialProjection:
        raise PublicationPersistenceError("publication_material_projection_conflict")
    try:
        material_projection.assert_integrity()
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicationPersistenceError(
            "publication_material_projection_conflict"
        ) from exc
    stored_projection = _payload(_field(row, "material_projection_payload", 15))
    if (
        canonical_value(stored_projection)
        != canonical_value(material_projection.to_dict())
        or str(_field(row, "material_projection_digest", 16) or "")
        != material_projection.content_digest
        or str(_field(row, "material_projection_palette_ref", 17) or "")
        != material_projection.palette_ref
        or str(_field(row, "material_projection_palette_digest", 18) or "")
        != material_projection.palette_digest
        or str(_field(row, "material_projection_settlement_ref", 19) or "")
        != material_projection.claim_settlement_ref
        or str(_field(row, "material_projection_settlement_digest", 20) or "")
        != material_projection.claim_settlement_digest
    ):
        raise PublicationPersistenceError("publication_material_projection_conflict")
    parent_payload = _payload(_field(row, "parent_transition_payload", 4))
    parent_input = _payload(_field(row, "parent_transition_input_payload", 5))
    parent_output = _payload(_field(row, "parent_transition_output_payload", 6))
    try:
        parent = DurableTransition.from_dict(parent_payload)
    except (TypeError, ValueError) as exc:
        raise PublicationPersistenceError(
            "publication_parent_transition_invalid"
        ) from exc
    if (
        parent.transition_id != compose_transition.parent_transition_id
        or parent.node_name != "settle_claim_authority"
        or parent.run_attempt_id != authority_bundle.run_attempt_id
        or parent.intent_revision_id != authority_bundle.intent_revision_id
        or parent.decision_ledger_position
        != compose_transition.decision_ledger_position
        or parent.input_digest != canonical_digest(parent_input)
        or parent.output_digest != canonical_digest(parent_output)
        or parent.status != "succeeded"
        or parent.acceptance_state != "accepted"
        or parent.next_transition != "compose_claim_aware_narrative"
        or canonical_value(parent_output.get("authority_bundle"))
        != canonical_value(authority_bundle.to_dict())
    ):
        raise PublicationPersistenceError("publication_parent_transition_invalid")
    lifecycle = LifecycleState.from_dict(_payload(_field(row, "lifecycle_payload", 7)))
    if (
        lifecycle.run_attempt_id != authority_bundle.run_attempt_id
        or lifecycle.execution_state != "complete"
        or lifecycle.evidence_state not in {"complete", "boundary_only"}
        or lifecycle.cancellation_state != "active"
        or lifecycle.supersession_state != "active"
    ):
        raise PublicationPersistenceError("publication_lifecycle_not_active")
    existing_payload = _optional_payload(_field(row, "existing_transition_payload", 8))
    existing_input = _optional_payload(
        _field(row, "existing_transition_input_payload", 9)
    )
    existing_output = _optional_payload(
        _field(row, "existing_transition_output_payload", 10)
    )
    replaying = existing_payload is not None
    if replaying:
        try:
            existing = DurableTransition.from_dict(existing_payload)
        except (TypeError, ValueError) as exc:
            raise PublicationPersistenceError(
                "publication_transition_replay_conflict"
            ) from exc
        if (
            existing != compose_transition
            or canonical_value(existing_input) != canonical_value(transition_input)
            or canonical_value(existing_output) != canonical_value(transition_output)
        ):
            raise PublicationPersistenceError("publication_transition_replay_conflict")
    elif existing_input is not None or existing_output is not None:
        raise PublicationPersistenceError("publication_transition_partial_conflict")

    expected_publication = _optional_payload(
        _field(row, "expected_publication_payload", 11)
    )
    latest_publication = _optional_payload(
        _field(row, "latest_publication_payload", 12)
    )
    expected_outbox = _optional_payload(_field(row, "expected_outbox_payload", 13))
    expected_customer = _optional_payload(_field(row, "expected_customer_payload", 14))
    publication = publication_flow.publication
    outbox = publication_flow.outbox
    exact_existing = (
        canonical_value(expected_publication) == canonical_value(publication.to_dict())
        and canonical_value(expected_outbox) == canonical_value(outbox.to_dict())
        and canonical_value(expected_customer)
        == canonical_value(customer_record.to_dict())
    )
    if replaying and not exact_existing:
        raise PublicationPersistenceError("publication_transition_artifact_conflict")
    if not replaying and any(
        item is not None
        for item in (expected_publication, expected_outbox, expected_customer)
    ):
        raise PublicationPersistenceError("publication_partial_replay_conflict")
    if expected_publication is not None and not exact_existing:
        raise PublicationPersistenceError("publication_revision_conflict")
    if replaying:
        if lifecycle.publication_state not in {"ready", "published"} or (
            lifecycle.delivery_state
            not in {
                "persisted",
                "retryable_failed",
                "permanently_failed",
                "published",
            }
        ):
            raise PublicationPersistenceError("publication_replay_lifecycle_conflict")
        return lifecycle, True
    if (
        lifecycle.publication_state not in {"not_ready", "composing"}
        or lifecycle.delivery_state != "pending"
    ):
        raise PublicationPersistenceError("publication_lifecycle_not_composable")
    expected_predecessor = (
        None if supersedes_publication is None else supersedes_publication.to_dict()
    )
    if canonical_value(latest_publication) != canonical_value(expected_predecessor):
        raise PublicationPersistenceError("publication_revision_cas_conflict")
    return lifecycle, False


def _publication_records(
    *,
    owner_ref: str,
    authority_bundle: AuthorityBundle,
    narrative_workflow: NarrativeWorkflowResult,
    publication_flow: PublicationFlowResult,
    customer_record: _CustomerPayloadRecord,
) -> tuple[_InsertRecord, ...]:
    run = authority_bundle.run_attempt_id
    policy = narrative_workflow.visibility_policy
    records: list[_InsertRecord] = []

    def record(
        table: str,
        identity_column: str,
        identity: str,
        payload: Mapping[str, Any],
        **columns: Any,
    ) -> None:
        records.append(
            _InsertRecord(
                table=table,
                identity_column=identity_column,
                columns={
                    identity_column: identity,
                    "owner_ref": owner_ref,
                    "run_attempt_id": run,
                    **columns,
                    "payload": canonical_value(payload),
                },
                json_columns=frozenset(
                    {"payload", *(key for key in columns if key.endswith("_refs"))}
                ),
            )
        )

    def scoped_conflict(*columns: str) -> None:
        current = records[-1]
        records[-1] = _InsertRecord(
            table=current.table,
            identity_column=current.identity_column,
            columns=current.columns,
            json_columns=current.json_columns,
            conflict_columns=("owner_ref", "run_attempt_id", *columns),
        )

    for response in narrative_workflow.provider_responses:
        replayed = RestrictedProviderResponse.from_dict(response.to_dict())
        record(
            "restricted_provider_responses",
            "provider_response_ref",
            replayed.response_ref,
            replayed.to_dict(),
            attempt_id=replayed.attempt_id,
            purpose=replayed.purpose,
            provider_ref=replayed.provider_ref,
            model_ref=replayed.model_ref,
            input_ref=replayed.input_ref,
            input_digest=replayed.input_digest,
            attempt_number=replayed.attempt_number,
            raw_response_content=replayed.content,
            content_digest=replayed.content_digest,
        )

    for writer, narrative, local_report in zip(
        narrative_workflow.writer_attempts,
        narrative_workflow.narratives,
        narrative_workflow.local_reports,
        strict=True,
    ):
        record(
            "narrative_writer_attempts",
            "writer_attempt_ref",
            writer.writer_attempt_ref,
            writer.to_dict(),
            attempt_id=writer.attempt_id,
            authority_bundle_ref=writer.authority_bundle_ref,
            material_projection_ref=writer.material_projection_ref,
            input_ref=writer.input_ref,
            input_digest=writer.input_digest,
            attempt_number=writer.attempt_number,
            provider_ref=writer.provider_ref,
            model_ref=writer.model_ref,
            provider_response_ref=writer.provider_response_ref,
            provider_response_digest=writer.provider_response_digest,
            content_digest=writer.content_digest,
        )
        record(
            "narrative_documents",
            "narrative_id",
            narrative.narrative_id,
            narrative.to_dict(),
            authority_bundle_ref=narrative.authority_bundle_ref,
            material_projection_ref=narrative.material_projection_ref,
            writer_attempt_ref=writer.writer_attempt_ref,
            parent_narrative_id=narrative.parent_narrative_id,
            content_digest=narrative.content_digest,
        )
        for block in narrative.blocks:
            record(
                "narrative_blocks",
                "block_id",
                block.block_id,
                block.to_dict(),
                narrative_id=narrative.narrative_id,
                writer_attempt_id=block.writer_attempt_id,
                role=block.role,
                required=block.required,
                content_digest=block.content_digest,
            )
            scoped_conflict("narrative_id", "block_id")
            for binding in block.material_fact_bindings:
                record(
                    "narrative_fact_bindings",
                    "binding_ref",
                    binding.binding_ref,
                    binding.to_dict(),
                    narrative_id=narrative.narrative_id,
                    block_id=block.block_id,
                    claim_handle=binding.claim_handle,
                    fact_handle=binding.fact_handle,
                    content_digest=binding.content_digest,
                )
                scoped_conflict("narrative_id", "block_id", "binding_ref")
        for finding in local_report.sensitive_output_findings:
            record(
                "sensitive_output_findings",
                "finding_ref",
                finding.finding_ref,
                finding.to_dict(),
                narrative_id=narrative.narrative_id,
                block_id=finding.block_id,
                field_visibility_policy_ref=finding.field_visibility_policy_ref,
                policy_rule_ref=finding.policy_rule_ref,
                material_ref=finding.material_ref,
                content_digest=finding.content_digest,
            )
            scoped_conflict("narrative_id", "finding_ref")
        record(
            "block_local_validation_reports",
            "local_report_ref",
            local_report.local_report_ref,
            local_report.to_dict(),
            narrative_id=local_report.narrative_id,
            narrative_digest=local_report.narrative_digest,
            material_projection_ref=local_report.material_projection_ref,
            material_projection_digest=local_report.material_projection_digest,
            field_visibility_policy_ref=policy.policy_ref,
            finding_refs=tuple(
                finding.finding_ref
                for finding in local_report.sensitive_output_findings
            ),
            content_digest=local_report.content_digest,
        )
        for issue in local_report.issues:
            record(
                "block_local_issues",
                "issue_ref",
                issue.issue_ref,
                issue.to_dict(),
                local_report_ref=local_report.local_report_ref,
                narrative_id=local_report.narrative_id,
                block_id=issue.block_id,
                issue_code=issue.code,
                content_digest=issue.content_digest,
            )
            scoped_conflict("local_report_ref", "issue_ref")
    if publication_flow is not None:
        projection = publication_flow.projection
        publication = publication_flow.publication
        outbox = publication_flow.outbox
        record(
            "publication_projections",
            "projection_id",
            projection.projection_id,
            projection.to_dict(),
            authority_bundle_ref=projection.authority_bundle_ref,
            authority_bundle_digest=projection.authority_bundle_digest,
            material_projection_ref=projection.material_projection_ref,
            material_projection_digest=projection.material_projection_digest,
            narrative_id=projection.narrative_id,
            narrative_digest=projection.narrative_digest,
            local_report_ref=projection.local_report_ref,
            local_report_digest=projection.local_report_digest,
            field_visibility_policy_ref=projection.field_visibility_policy_ref,
            field_visibility_policy_digest=(projection.field_visibility_policy_digest),
            recommendation_refs=projection.recommendation_refs,
            projection_digest=projection.projection_digest,
            content_digest=projection.projection_digest,
        )
        record(
            "publication_revisions",
            "publication_ref",
            publication.publication_ref,
            publication.to_dict(),
            revision=publication.revision,
            supersedes_publication_ref=publication.supersedes_publication_ref,
            authority_bundle_ref=publication.authority_bundle_ref,
            authority_bundle_digest=publication.authority_bundle_digest,
            narrative_id=publication.narrative_id,
            narrative_digest=publication.narrative_digest,
            narrative_attempt_id=publication.narrative_attempt_id,
            local_report_ref=publication.local_report_ref,
            local_report_digest=publication.local_report_digest,
            projection_id=publication.projection_id,
            projection_digest=publication.projection_digest,
            publication_digest=publication.publication_digest,
            published_at=publication.published_at,
            content_digest=publication.publication_digest,
        )
        record(
            "delivery_outbox_records",
            "outbox_ref",
            outbox.outbox_ref,
            outbox.to_dict(),
            publication_ref=outbox.publication_ref,
            publication_digest=outbox.publication_digest,
            authority_bundle_ref=outbox.authority_bundle_ref,
            authority_bundle_digest=outbox.authority_bundle_digest,
            projection_id=outbox.projection_id,
            projection_digest=outbox.projection_digest,
            destination_ref=outbox.destination_ref,
            channel=outbox.channel,
            idempotency_key=outbox.idempotency_key,
            content_digest=outbox.content_digest,
        )
        record(
            "publication_customer_payloads",
            "customer_payload_ref",
            customer_record.customer_payload_ref,
            customer_record.to_dict(),
            outbox_ref=customer_record.outbox_ref,
            publication_ref=customer_record.publication_ref,
            publication_digest=customer_record.publication_digest,
            projection_id=customer_record.projection_id,
            projection_digest=customer_record.projection_digest,
            field_visibility_policy_ref=(customer_record.field_visibility_policy_ref),
            customer_payload=customer_record.customer_payload,
            content_digest=customer_record.content_digest,
        )
        current = records[-1]
        records[-1] = _InsertRecord(
            table=current.table,
            identity_column=current.identity_column,
            columns=current.columns,
            json_columns=frozenset({"payload", "customer_payload"}),
            conflict_columns=current.conflict_columns,
        )
    elif customer_record is not None:
        raise PublicationPersistenceError("withheld_customer_payload_forbidden")
    return tuple(records)


def _narrative_quality_audit_records(
    *,
    owner_ref: str,
    run_attempt_id: str,
    narrative_workflow: NarrativeWorkflowResult,
    quality_audit: NarrativeQualityAuditResult,
) -> tuple[_InsertRecord, ...]:
    records: list[_InsertRecord] = []

    def record(
        table: str,
        identity_column: str,
        identity: str,
        payload: Mapping[str, Any],
        **columns: Any,
    ) -> None:
        records.append(
            _InsertRecord(
                table=table,
                identity_column=identity_column,
                columns={
                    identity_column: identity,
                    "owner_ref": owner_ref,
                    "run_attempt_id": run_attempt_id,
                    **columns,
                    "payload": canonical_value(payload),
                },
                json_columns=frozenset({"payload"}),
            )
        )

    for response in quality_audit.provider_responses:
        replayed = RestrictedProviderResponse.from_dict(response.to_dict())
        record(
            "restricted_provider_responses",
            "provider_response_ref",
            replayed.response_ref,
            replayed.to_dict(),
            attempt_id=replayed.attempt_id,
            purpose=replayed.purpose,
            provider_ref=replayed.provider_ref,
            model_ref=replayed.model_ref,
            input_ref=replayed.input_ref,
            input_digest=replayed.input_digest,
            attempt_number=replayed.attempt_number,
            raw_response_content=replayed.content,
            content_digest=replayed.content_digest,
        )
    attempt = quality_audit.verification_attempt
    if attempt is not None:
        record(
            "block_verification_attempts",
            "verification_attempt_ref",
            attempt.verification_attempt_ref,
            attempt.to_dict(),
            narrative_id=attempt.narrative_id,
            narrative_digest=attempt.narrative_digest,
            local_report_ref=attempt.local_report_ref,
            local_report_digest=attempt.local_report_digest,
            attempt_id=attempt.attempt_id,
            input_ref=attempt.input_ref,
            input_digest=attempt.input_digest,
            provider_ref=attempt.provider_ref,
            model_ref=attempt.model_ref,
            attempt_number=attempt.attempt_number,
            provider_response_ref=attempt.provider_response_ref,
            provider_response_digest=attempt.provider_response_digest,
            content_digest=attempt.content_digest,
        )
    report = quality_audit.verifier_report
    for veto in report.vetoes:
        if report.verification_attempt_ref is None:
            raise PublicationPersistenceError(
                "block_verifier_veto_without_attempt"
            )
        record(
            "block_vetoes",
            "veto_ref",
            veto.veto_ref,
            veto.to_dict(),
            verification_attempt_ref=report.verification_attempt_ref,
            narrative_id=veto.narrative_id,
            block_id=veto.block_id,
            reason_code=veto.reason_code,
            content_digest=veto.content_digest,
        )
    record(
        "block_verification_reports",
        "verifier_report_ref",
        report.verifier_report_ref,
        report.to_dict(),
        audit_status=report.audit_status,
        verification_attempt_ref=report.verification_attempt_ref,
        verification_attempt_digest=report.verification_attempt_digest,
        narrative_id=report.narrative_id,
        narrative_digest=report.narrative_digest,
        local_report_ref=report.local_report_ref,
        local_report_digest=report.local_report_digest,
        failure_kind=report.failure_kind,
        retryability=report.retryability,
        technical_detail_ref=report.technical_detail_ref,
        content_digest=report.content_digest,
    )
    audit_result_ref = (
        "narrative-quality-audit-result:sha256:" + quality_audit.content_digest
    )
    record(
        "narrative_quality_audit_results",
        "audit_result_ref",
        audit_result_ref,
        quality_audit.to_dict(),
        source_customer_publication_ref=(
            quality_audit.source_customer_publication_ref
        ),
        narrative_workflow_ref=quality_audit.narrative_workflow_ref,
        narrative_workflow_digest=quality_audit.narrative_workflow_digest,
        call_input_ref=quality_audit.call_input_ref,
        call_input_digest=quality_audit.call_input_digest,
        verifier_report_ref=report.verifier_report_ref,
        verifier_report_digest=report.content_digest,
        audit_status=report.audit_status,
        content_digest=quality_audit.content_digest,
    )
    if (
        quality_audit.narrative_workflow_ref
        != "narrative-workflow-result:sha256:" + narrative_workflow.content_digest
        or quality_audit.narrative_workflow_digest
        != narrative_workflow.content_digest
    ):
        raise PublicationPersistenceError(
            "narrative_quality_audit_workflow_conflict"
        )
    return tuple(records)


def persist_narrative_quality_audit(
    connection: Any,
    *,
    owner_ref: str,
    run_attempt_id: str,
    narrative_workflow: NarrativeWorkflowResult,
    quality_audit: NarrativeQualityAuditResult,
) -> NarrativeQualityAuditPersistenceResult:
    owner = _required_string(owner_ref, "narrative_quality_audit_owner_invalid")
    run = _required_string(
        run_attempt_id,
        "narrative_quality_audit_run_attempt_invalid",
    )
    if type(narrative_workflow) is not NarrativeWorkflowResult:
        raise PublicationPersistenceError(
            "narrative_quality_audit_workflow_invalid"
        )
    try:
        audit = NarrativeQualityAuditResult.from_dict(
            quality_audit.to_dict(),
            narrative_workflow=narrative_workflow,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicationPersistenceError(
            "narrative_quality_audit_result_invalid"
        ) from exc
    if audit != quality_audit:
        raise PublicationPersistenceError(
            "narrative_quality_audit_result_invalid"
        )
    records = _narrative_quality_audit_records(
        owner_ref=owner,
        run_attempt_id=run,
        narrative_workflow=narrative_workflow,
        quality_audit=audit,
    )
    try:
        for record in records:
            _insert_exact(connection, record)
        connection.commit()
    except PublicationPersistenceError:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        if not isinstance(exc, (PublicationPersistenceBackendError, PsycopgError)):
            raise
        detail_digest = canonical_digest(
            {
                "run_attempt_id": run,
                "narrative_workflow_ref": audit.narrative_workflow_ref,
                "audit_result_digest": audit.content_digest,
                "exception_type": type(exc).__name__,
            }
        )
        raise PublicationPersistenceOperationalError(
            technical_detail_ref="technical-detail:sha256:" + detail_digest
        ) from exc
    return NarrativeQualityAuditPersistenceResult(
        audit_result_ref=(
            "narrative-quality-audit-result:sha256:" + audit.content_digest
        ),
        verifier_report_ref=audit.verifier_report.verifier_report_ref,
        audit_status=audit.verifier_report.audit_status,
        status="persisted",
    )


def _compose_transition_record(
    *,
    transition: DurableTransition,
    input_payload: Mapping[str, Any],
    output_payload: Mapping[str, Any],
) -> _InsertRecord:
    return _InsertRecord(
        table="workflow_transition_attempts",
        identity_column="attempt_id",
        columns={
            "attempt_id": transition.attempt_id,
            "transition_id": transition.transition_id,
            "node_name": transition.node_name,
            "parent_transition_id": transition.parent_transition_id,
            "run_attempt_id": transition.run_attempt_id,
            "intent_revision_id": transition.intent_revision_id,
            "decision_ledger_position": transition.decision_ledger_position,
            "input_digest": transition.input_digest,
            "output_digest": transition.output_digest,
            "execution_attempt": transition.execution_attempt,
            "provider_ref": transition.provider_ref,
            "model_ref": transition.model_ref,
            "status": transition.status,
            "acceptance_state": transition.acceptance_state,
            "next_transition": transition.next_transition,
            "input_payload": canonical_value(input_payload),
            "output_payload": canonical_value(output_payload),
            "started_at": transition.started_at,
            "finished_at": transition.finished_at,
        },
        json_columns=frozenset({"input_payload", "output_payload"}),
    )


def _persist_publication_lifecycle(
    connection: Any,
    *,
    lifecycle: LifecycleState,
    authority_bundle: AuthorityBundle,
    publication: PublicationRevision,
    outbox: DeliveryOutboxRecord,
) -> LifecycleState:
    state = lifecycle
    if state.publication_state == "not_ready":
        state = state.transition(
            publication_state="composing",
            retry_state=(
                "succeeded" if state.retry_state == "exhausted" else state.retry_state
            ),
        )
        _insert_lifecycle(connection, state)
    ready = state.transition(
        publication_state="ready",
        retry_state=(
            "succeeded" if state.retry_state == "exhausted" else state.retry_state
        ),
    )
    _insert_lifecycle(connection, ready)
    persisted = ready.transition(delivery_state="persisted")
    validate_publication_lifecycle(
        lifecycle=persisted,
        authority_bundle=authority_bundle,
        publication=publication,
        outbox=outbox,
    )
    _insert_lifecycle(connection, persisted)
    return persisted


def _insert_exact(connection: Any, record: _InsertRecord) -> None:
    names = tuple(record.columns)
    values = tuple(
        f"%({name})s::jsonb" if name in record.json_columns else f"%({name})s"
        for name in names
    )
    params = {
        name: (
            json.dumps(canonical_value(value), sort_keys=True, separators=(",", ":"))
            if name in record.json_columns
            else value
        )
        for name, value in record.columns.items()
    }
    conflict_columns = record.conflict_columns or (record.identity_column,)
    inserted = connection.execute(
        f"""
        INSERT INTO waje_runtime.{record.table} ({", ".join(names)})
        VALUES ({", ".join(values)})
        ON CONFLICT ({", ".join(conflict_columns)}) DO NOTHING
        RETURNING {record.identity_column}
        """,
        params,
    ).fetchone()
    if inserted is not None:
        if str(_field(inserted, record.identity_column, 0)) != str(
            record.columns[record.identity_column]
        ):
            raise PublicationPersistenceError(
                f"publication_insert_identity_conflict:{record.table}"
            )
        return
    replay_where = " AND ".join(f"{name} = %({name})s" for name in conflict_columns)
    row = connection.execute(
        f"""
        /* publication_exact_replay:{record.table} */
        SELECT {", ".join(names)}
        FROM waje_runtime.{record.table}
        WHERE {replay_where}
        """,
        {name: record.columns[name] for name in conflict_columns},
    ).fetchone()
    if row is None:
        raise PublicationPersistenceError(
            f"publication_exact_replay_missing:{record.table}"
        )
    for index, name in enumerate(names):
        stored = _field(row, name, index)
        expected = record.columns[name]
        if name in record.json_columns:
            stored = _json_value(stored)
        if _comparable(stored) != _comparable(expected):
            raise PublicationPersistenceError(
                f"publication_exact_replay_conflict:{record.table}:{name}"
            )


def _insert_lifecycle(connection: Any, lifecycle: LifecycleState) -> None:
    payload = lifecycle.to_dict()
    columns = {key: value for key, value in payload.items() if key != "content_digest"}
    columns["content_digest"] = lifecycle.content_digest
    columns["payload"] = payload
    record = _InsertRecord(
        table="run_lifecycle_state_revisions",
        identity_column="state_revision",
        columns=columns,
        json_columns=frozenset({"payload"}),
        conflict_columns=("run_attempt_id", "state_revision"),
    )
    _insert_exact(connection, record)


_DELIVERY_PREFLIGHT_SQL = """
/* delivery_persistence_preflight */
SELECT
  outbox.owner_ref,
  outbox.run_attempt_id,
  outbox.payload AS outbox_payload,
  customer.payload AS customer_payload_record,
  lifecycle.payload AS lifecycle_payload,
  publication.payload AS publication_payload,
  bundle.payload AS authority_bundle_payload,
  customer_publication.payload AS customer_publication_payload
FROM waje_runtime.delivery_outbox_records outbox
JOIN waje_runtime.publication_customer_payloads customer
  ON customer.owner_ref = outbox.owner_ref
 AND customer.run_attempt_id = outbox.run_attempt_id
 AND customer.outbox_ref = outbox.outbox_ref
JOIN waje_runtime.publication_revisions publication
  ON publication.owner_ref = outbox.owner_ref
 AND publication.run_attempt_id = outbox.run_attempt_id
 AND publication.publication_ref = outbox.publication_ref
JOIN waje_runtime.authority_bundles bundle
  ON bundle.owner_ref = outbox.owner_ref
 AND bundle.run_attempt_id = outbox.run_attempt_id
 AND bundle.bundle_ref = outbox.authority_bundle_ref
JOIN LATERAL (
  SELECT state.payload
  FROM waje_runtime.run_lifecycle_state_revisions state
  WHERE state.run_attempt_id = outbox.run_attempt_id
  ORDER BY state.state_revision DESC
  LIMIT 1
) lifecycle ON TRUE
LEFT JOIN waje_runtime.customer_publications customer_publication
  ON customer_publication.owner_ref = outbox.owner_ref
 AND customer_publication.run_attempt_id = outbox.run_attempt_id
 AND customer_publication.outbox_ref = outbox.outbox_ref
WHERE outbox.outbox_ref = %(outbox_ref)s
FOR UPDATE OF outbox
"""


_DELIVERY_SCOPE_SQL = """
/* delivery_persistence_scope */
SELECT run_attempt_id
FROM waje_runtime.delivery_outbox_records
WHERE outbox_ref = %(outbox_ref)s
"""


_DELIVERY_ATTEMPTS_SQL = """
/* delivery_attempt_history */
SELECT payload
FROM waje_runtime.delivery_attempts
WHERE owner_ref = %(owner_ref)s
  AND run_attempt_id = %(run_attempt_id)s
  AND outbox_ref = %(outbox_ref)s
ORDER BY attempt_number
"""


def deliver_persisted_outbox(
    connection: Any,
    *,
    outbox_ref: str,
    transport: Callable[[DeliveryMessage], DeliveryTransportResult],
) -> DeliveryPersistenceResult:
    return _deliver(connection, outbox_ref=outbox_ref, transport=transport)


def _deliver(
    connection: Any,
    *,
    outbox_ref: str,
    transport: Callable[[DeliveryMessage], DeliveryTransportResult],
) -> DeliveryPersistenceResult:
    ref = _required_string(outbox_ref, "delivery_outbox_ref_invalid")
    if not callable(transport):
        raise PublicationPersistenceError("delivery_transport_invalid")
    try:
        scope_row = connection.execute(
            _DELIVERY_SCOPE_SQL,
            {"outbox_ref": ref},
        ).fetchone()
        if scope_row is None:
            raise PublicationPersistenceError("delivery_outbox_missing")
        scoped_run_attempt_id = _required_string(
            _field(scope_row, "run_attempt_id", 0),
            "delivery_run_attempt_id_invalid",
        )
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": f"single_authority:{scoped_run_attempt_id}"},
        )
        row = connection.execute(
            _DELIVERY_PREFLIGHT_SQL, {"outbox_ref": ref}
        ).fetchone()
        if row is None:
            raise PublicationPersistenceError("delivery_outbox_missing")
        owner_ref = _required_string(
            _field(row, "owner_ref", 0), "delivery_owner_ref_invalid"
        )
        run_attempt_id = _required_string(
            _field(row, "run_attempt_id", 1), "delivery_run_attempt_id_invalid"
        )
        if run_attempt_id != scoped_run_attempt_id:
            raise PublicationPersistenceError("delivery_run_scope_conflict")
        outbox = _outbox_from_payload(_payload(_field(row, "outbox_payload", 2)))
        customer = _CustomerPayloadRecord.from_dict(
            _payload(_field(row, "customer_payload_record", 3))
        )
        lifecycle = LifecycleState.from_dict(
            _payload(_field(row, "lifecycle_payload", 4))
        )
        publication = _publication_from_payload(
            _payload(_field(row, "publication_payload", 5))
        )
        authority_bundle = _authority_bundle_from_payload(
            _payload(_field(row, "authority_bundle_payload", 6))
        )
        if (
            outbox.outbox_ref != ref
            or outbox.run_attempt_id != run_attempt_id
            or customer.run_attempt_id != run_attempt_id
            or customer.outbox_ref != ref
            or customer.publication_ref != outbox.publication_ref
            or customer.publication_digest != outbox.publication_digest
            or customer.projection_id != outbox.projection_id
            or customer.projection_digest != outbox.projection_digest
            or publication.publication_ref != outbox.publication_ref
            or publication.publication_digest != outbox.publication_digest
            or authority_bundle.bundle_ref != outbox.authority_bundle_ref
            or authority_bundle.bundle_digest != outbox.authority_bundle_digest
            or lifecycle.run_attempt_id != run_attempt_id
            or lifecycle.execution_state != "complete"
            or lifecycle.evidence_state not in {"complete", "boundary_only"}
            or lifecycle.publication_state not in {"ready", "published"}
            or lifecycle.cancellation_state != "active"
            or lifecycle.supersession_state != "active"
        ):
            raise PublicationPersistenceError("delivery_authority_closure_conflict")
        history = _load_delivery_history(
            connection,
            owner_ref=owner_ref,
            run_attempt_id=run_attempt_id,
            outbox=outbox,
        )
        existing_customer = _optional_payload(
            _field(row, "customer_publication_payload", 7)
        )
        if history and history[-1].status in {"published", "permanently_failed"}:
            latest = history[-1]
            if (latest.status == "published") != (existing_customer is not None):
                raise PublicationPersistenceError("delivery_terminal_replay_conflict")
            validate_publication_lifecycle(
                lifecycle=lifecycle,
                authority_bundle=authority_bundle,
                publication=publication,
                outbox=outbox,
            )
            customer_publication_ref = None
            if existing_customer is not None:
                customer_publication = _CustomerPublicationRecord.from_dict(
                    existing_customer
                )
                expected_customer_publication = _CustomerPublicationRecord.create(
                    outbox=outbox,
                    attempt=latest,
                )
                if customer_publication != expected_customer_publication:
                    raise PublicationPersistenceError(
                        "delivery_customer_publication_conflict"
                    )
                customer_publication_ref = customer_publication.customer_publication_ref
            connection.commit()
            return DeliveryPersistenceResult(
                outbox_ref=outbox.outbox_ref,
                attempt_ref=latest.attempt_ref,
                status=latest.status,
                lifecycle_state_digest=lifecycle.content_digest,
                customer_publication_ref=customer_publication_ref,
                replayed=True,
            )
        if lifecycle.delivery_state not in {"persisted", "retryable_failed"}:
            raise PublicationPersistenceError("delivery_lifecycle_not_dispatchable")
        previous = history[-1] if history else None
        if previous is not None and previous.status != "retryable_failed":
            raise PublicationPersistenceError("delivery_retry_state_conflict")
        if (previous is None and lifecycle.delivery_state != "persisted") or (
            previous is not None and lifecycle.delivery_state != "retryable_failed"
        ):
            raise PublicationPersistenceError("delivery_retry_state_conflict")
        result = transport(
            DeliveryMessage(
                outbox_ref=outbox.outbox_ref,
                destination_ref=outbox.destination_ref,
                channel=outbox.channel,
                idempotency_key=outbox.idempotency_key,
                customer_payload=canonical_value(customer.customer_payload),
            )
        )
        if type(result) is not DeliveryTransportResult:
            raise PublicationPersistenceError("delivery_transport_result_invalid")
        result = DeliveryTransportResult._create(
            status=result.status,
            transport_receipt_ref=result.transport_receipt_ref,
            failure_code=result.failure_code,
        )
        attempt = DeliveryAttempt.record(
            outbox=outbox,
            attempt_number=len(history) + 1,
            previous_attempt=previous,
            status=result.status,
            transport_receipt_ref=result.transport_receipt_ref,
            failure_code=result.failure_code,
            attempted_at=_utc_now(),
        )
        _insert_delivery_attempt(
            connection,
            owner_ref=owner_ref,
            attempt=attempt,
        )
        customer_publication_ref: str | None = None
        if result.status == "published":
            customer_publication = _CustomerPublicationRecord.create(
                outbox=outbox,
                attempt=attempt,
            )
            _insert_customer_publication(
                connection,
                owner_ref=owner_ref,
                record=customer_publication,
            )
            customer_publication_ref = customer_publication.customer_publication_ref
            next_lifecycle = lifecycle.transition(
                publication_state="published",
                delivery_state="published",
            )
        else:
            next_lifecycle = lifecycle.transition(delivery_state=result.status)
        validate_publication_lifecycle(
            lifecycle=next_lifecycle,
            authority_bundle=authority_bundle,
            publication=publication,
            outbox=outbox,
        )
        _insert_lifecycle(connection, next_lifecycle)
        connection.commit()
        return DeliveryPersistenceResult(
            outbox_ref=outbox.outbox_ref,
            attempt_ref=attempt.attempt_ref,
            status=attempt.status,
            lifecycle_state_digest=next_lifecycle.content_digest,
            customer_publication_ref=customer_publication_ref,
            replayed=False,
        )
    except Exception:
        connection.rollback()
        raise


def _load_delivery_history(
    connection: Any,
    *,
    owner_ref: str,
    run_attempt_id: str,
    outbox: DeliveryOutboxRecord,
) -> tuple[DeliveryAttempt, ...]:
    rows = connection.execute(
        _DELIVERY_ATTEMPTS_SQL,
        {
            "owner_ref": owner_ref,
            "run_attempt_id": run_attempt_id,
            "outbox_ref": outbox.outbox_ref,
        },
    ).fetchall()
    history: list[DeliveryAttempt] = []
    for row in rows:
        attempt = DeliveryAttempt.from_dict(
            _payload(_field(row, "payload", 0)),
            outbox=outbox,
            previous_attempt=history[-1] if history else None,
        )
        history.append(attempt)
    return tuple(history)


def _insert_delivery_attempt(
    connection: Any,
    *,
    owner_ref: str,
    attempt: DeliveryAttempt,
) -> None:
    _insert_exact(
        connection,
        _InsertRecord(
            table="delivery_attempts",
            identity_column="attempt_ref",
            columns={
                "attempt_ref": attempt.attempt_ref,
                "owner_ref": owner_ref,
                "run_attempt_id": attempt.run_attempt_id,
                "outbox_ref": attempt.outbox_ref,
                "publication_ref": attempt.publication_ref,
                "publication_digest": attempt.publication_digest,
                "projection_id": attempt.projection_id,
                "projection_digest": attempt.projection_digest,
                "destination_ref": attempt.destination_ref,
                "channel": attempt.channel,
                "idempotency_key": attempt.idempotency_key,
                "attempt_number": attempt.attempt_number,
                "previous_attempt_ref": attempt.previous_attempt_ref,
                "status": attempt.status,
                "transport_receipt_ref": attempt.transport_receipt_ref,
                "failure_code": attempt.failure_code,
                "attempted_at": attempt.attempted_at,
                "content_digest": attempt.content_digest,
                "payload": attempt.to_dict(),
            },
        ),
    )


def _insert_customer_publication(
    connection: Any,
    *,
    owner_ref: str,
    record: _CustomerPublicationRecord,
) -> None:
    _insert_exact(
        connection,
        _InsertRecord(
            table="customer_publications",
            identity_column="customer_publication_ref",
            columns={
                "customer_publication_ref": record.customer_publication_ref,
                "owner_ref": owner_ref,
                "run_attempt_id": record.run_attempt_id,
                "outbox_ref": record.outbox_ref,
                "delivery_attempt_ref": record.delivery_attempt_ref,
                "publication_ref": record.publication_ref,
                "projection_id": record.projection_id,
                "destination_ref": record.destination_ref,
                "channel": record.channel,
                "transport_receipt_ref": record.transport_receipt_ref,
                "content_digest": record.content_digest,
                "payload": record.to_dict(),
            },
        ),
    )


def _outbox_from_payload(payload: Mapping[str, Any]) -> DeliveryOutboxRecord:
    expected = set(DeliveryOutboxRecord.__dataclass_fields__)
    if set(payload) != expected:
        raise PublicationPersistenceError("delivery_outbox_shape_invalid")
    outbox = DeliveryOutboxRecord(**payload)
    body = {
        key: value
        for key, value in outbox.to_dict().items()
        if key not in {"outbox_ref", "content_digest"}
    }
    digest = canonical_digest(body)
    if (
        outbox.content_digest != digest
        or outbox.outbox_ref != "delivery-outbox:sha256:" + digest
    ):
        raise PublicationPersistenceError("delivery_outbox_integrity_invalid")
    return outbox


def _publication_from_payload(payload: Mapping[str, Any]) -> PublicationRevision:
    expected = set(PublicationRevision.__dataclass_fields__)
    if set(payload) != expected:
        raise PublicationPersistenceError("delivery_publication_shape_invalid")
    publication = PublicationRevision(**payload)
    manifest = {
        key: value
        for key, value in publication.to_dict().items()
        if key not in {"publication_ref", "publication_digest", "published_at"}
    }
    digest = canonical_digest(manifest)
    if (
        publication.publication_digest != digest
        or publication.publication_ref != "publication-revision:sha256:" + digest
    ):
        raise PublicationPersistenceError("delivery_publication_integrity_invalid")
    return publication


def _authority_bundle_from_payload(payload: Mapping[str, Any]) -> AuthorityBundle:
    expected = set(AuthorityBundle.__dataclass_fields__)
    if set(payload) != expected:
        raise PublicationPersistenceError("delivery_authority_bundle_shape_invalid")
    bundle = AuthorityBundle(**payload)
    manifest = {
        "bundle_revision": bundle.bundle_revision,
        "supersedes_bundle_ref": bundle.supersedes_bundle_ref,
        "run_attempt_id": bundle.run_attempt_id,
        "intent_revision_id": bundle.intent_revision_id,
        "decision_refs": bundle.decision_refs,
        "plan_revision_id": bundle.plan_revision_id,
        "authority_context_ref": bundle.authority_context_ref,
        "execution_result_ref": bundle.execution_result_ref,
        "execution_result_digest": bundle.execution_result_digest,
        "claim_settlement_ref": bundle.claim_settlement_ref,
        "claim_settlement_digest": bundle.claim_settlement_digest,
        "claim_graph_ref": bundle.claim_graph_ref,
        "claim_graph_digest": bundle.claim_graph_digest,
        "authority_mode": bundle.authority_mode,
        "required_obligation_ids": bundle.required_obligation_ids,
        "obligation_coverage_refs": bundle.obligation_coverage_refs,
        "evidence_refs": bundle.evidence_refs,
        "verified_claim_refs": bundle.verified_claim_refs,
        "recommendation_refs": bundle.recommendation_refs,
        "assumption_refs": bundle.assumption_refs,
        "limitation_refs": bundle.limitation_refs,
        "claim_verifier_report_ref": bundle.claim_verifier_report_ref,
    }
    digest = canonical_digest(manifest)
    namespace_token = bundle.authority_namespace_ref.removeprefix(
        "claim-authority-namespace:sha256:"
    )[:24]
    if (
        bundle.bundle_digest != digest
        or bundle.content_digest != digest
        or bundle.bundle_ref != f"authority-bundle:{namespace_token}:sha256:{digest}"
        or bundle.seal_state != "sealed"
    ):
        raise PublicationPersistenceError("delivery_authority_bundle_integrity_invalid")
    return bundle


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _payload(value: Any) -> Mapping[str, Any]:
    value = _json_value(value)
    if not isinstance(value, Mapping):
        raise PublicationPersistenceError("publication_payload_invalid")
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PublicationPersistenceError("publication_payload_invalid") from exc
    if not isinstance(value, (Mapping, list)):
        raise PublicationPersistenceError("publication_payload_invalid")
    return value


def _optional_payload(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return _payload(value)


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PublicationPersistenceError(error)
    return value


def _optional_string(value: Any, error: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, error)


def _comparable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return canonical_value(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
