from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
    validate_typed_authoritative_execution_result,
)
from bi_agent.runtime.authority_seal_persistence import (
    AuthoritySealResult,
    seal_authority_bundle,
    semantic_authority_transition_payloads,
)
from bi_agent.runtime.capability_authority import EvidenceLedgerEntry
from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    ClaimAuthorityNamespace,
)
from bi_agent.runtime.claim_coverage import (
    ClaimCoverageCheckpoint,
    ClaimCoverageContractError,
)
from bi_agent.runtime.controlled_investigation_workflow import (
    run_controlled_investigation_workflow,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.factor_coverage import (
    FactorCoveragePlan,
    FactorCoverageResult,
    narrative_factor_coverage_context,
    synthesize_factor_coverage,
)
from bi_agent.runtime.durable_call_journal import (
    DurableCallJournal,
    DurableCallJournalError,
    DurableProviderClient,
)
from bi_agent.runtime.narrative_authority import (
    PublicationFieldVisibilityPolicy,
)
from bi_agent.runtime.narrative_context import build_narrative_answer_context
from bi_agent.runtime.narrative_material_persistence import (
    NarrativeMaterialPersistenceOperationalError,
    NarrativeMaterialPersistenceResult,
    persist_narrative_material_projection,
)
from bi_agent.runtime.narrative_material_projection import (
    NarrativeMaterialProjection,
)
from bi_agent.runtime.narrative_materialization import (
    build_public_limitation_contexts,
    build_reviewed_public_materialization,
)
from bi_agent.runtime.narrative_workflow import (
    NarrativeAnswerContext,
    NarrativeProviderCallError,
    NarrativeWorkflowResult,
    SensitiveOutputInspector,
    TypedNarrativeLLM,
    prepare_narrative_material_projection,
    run_narrative_workflow,
    validate_typed_narrative_workflow_result,
)
from bi_agent.runtime.post_seal_failure_persistence import (
    POST_SEAL_FAILURE_STATUSES,
    PostSealFailurePersistenceResult,
    PostSealFailureTerminal,
)
from bi_agent.runtime.public_fact_materialization import (
    PublicFactMaterialization,
    materialize_public_facts,
)
from bi_agent.runtime.publication_flow import (
    PublicationFlowResult,
    validate_typed_publication_flow,
)
from bi_agent.runtime.publication_persistence import (
    DeliveryMessage,
    DeliveryPersistenceResult,
    DeliveryTransportResult,
    PublicationPersistenceResult,
    PublicationPersistenceOperationalError,
    deliver_persisted_outbox,
    narrative_publication_transition_payloads,
    persist_publication,
)
from bi_agent.runtime.semantic_authority_workflow import (
    SemanticAuthorityResult,
    TypedSemanticAuthorityLLM,
    run_semantic_authority_workflow,
)
from bi_agent.runtime.single_authority import (
    DurableTransition,
    FailureRecord,
    IntentRevision,
)
from bi_agent.runtime.llm_client import (
    LLMOutputError,
    LLMProviderError,
    LLMTimeoutError,
)


class PostExecutionWorkflowError(ValueError):
    pass


POST_EXECUTION_STATUSES = frozenset(
    {
        "authority_sealed",
        "narrative_ready",
        "completed",
        "delivery_retryable_failed",
        "delivery_permanently_failed",
        *POST_SEAL_FAILURE_STATUSES,
    }
)
STOP_BOUNDARIES = frozenset({"phase04", "phase05"})
_PERSISTENCE_STATES = frozenset({"not_started", "inserted", "replayed"})

_SEMANTIC_PROVIDER_REF = "waje-semantic-authority"
_SEMANTIC_MODEL_REF = "single-authority-phase04.v1"
_NARRATIVE_PROVIDER_REF = "waje-narrative-authority"
_NARRATIVE_MODEL_REF = "single-authority-phase05.v21"


class AcceptedTransitionStore(Protocol):
    def load_accepted_transition(
        self,
        *,
        run_attempt_id: str,
        node_name: str,
        input_digest: str,
    ) -> Mapping[str, Any] | None: ...

    def load_post_seal_failure_terminal(
        self,
        *,
        authority_bundle: AuthorityBundle,
        authority_transition: DurableTransition,
    ) -> PostSealFailureTerminal | None: ...

    def record_post_seal_failure(
        self,
        *,
        owner_ref: str,
        thread_ref: str,
        authority_bundle: AuthorityBundle,
        authority_transition: DurableTransition,
        status: str,
        failure_record: FailureRecord,
        supersedes_terminal_ref: str | None,
    ) -> PostSealFailurePersistenceResult: ...


class PostExecutionLLM(TypedSemanticAuthorityLLM, TypedNarrativeLLM, Protocol):
    pass


@dataclass(frozen=True)
class PostExecutionWorkflowResult:
    result_ref: str
    status: str
    run_attempt_id: str
    intent_revision_id: str
    claim_coverage_checkpoint_ref: str
    claim_coverage_checkpoint_digest: str
    claim_coverage_transition_id: str
    semantic_authority_result_ref: str
    semantic_authority_result_digest: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    authority_transition_id: str
    authority_persistence_status: str
    post_seal_failure_terminal_ref: str | None
    post_seal_failure_persistence_status: str
    semantic_authority_result: SemanticAuthorityResult
    authority_bundle: AuthorityBundle
    authority_transition: DurableTransition
    post_seal_failure_terminal: PostSealFailureTerminal | None
    narrative_material_projection_ref: str | None
    narrative_material_projection_digest: str | None
    narrative_material_persistence_status: str
    narrative_workflow_ref: str | None
    narrative_workflow_digest: str | None
    compose_transition_id: str | None
    narrative_persistence_status: str
    narrative_workflow: NarrativeWorkflowResult | None
    publication_flow: PublicationFlowResult | None
    compose_transition: DurableTransition | None
    publication_ref: str | None
    outbox_ref: str | None
    customer_payload_ref: str | None
    delivery_attempt_ref: str | None
    delivery_status: str | None
    delivery_replayed: bool | None
    customer_publication_ref: str | None
    customer_payload: Mapping[str, Any] | None
    content_digest: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PostExecutionWorkflowResult":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise PostExecutionWorkflowError("post_execution_result_shape_invalid")
        raw_semantic = payload["semantic_authority_result"]
        if not isinstance(raw_semantic, Mapping):
            raise PostExecutionWorkflowError(
                "post_execution_result_semantic_authority_invalid"
            )
        try:
            semantic = SemanticAuthorityResult.from_dict(raw_semantic)
        except (TypeError, ValueError) as exc:
            raise PostExecutionWorkflowError(
                "post_execution_result_semantic_authority_invalid"
            ) from exc
        raw_bundle = payload["authority_bundle"]
        if not isinstance(raw_bundle, Mapping):
            raise PostExecutionWorkflowError(
                "post_execution_result_authority_bundle_invalid"
            )
        try:
            bundle = AuthorityBundle.from_dict(
                raw_bundle,
                authority_inputs=semantic.authority_bundle_inputs,
            )
            authority_transition = DurableTransition.from_dict(
                payload["authority_transition"]
            )
        except (TypeError, ValueError) as exc:
            raise PostExecutionWorkflowError(
                "post_execution_result_authority_bundle_invalid"
            ) from exc

        raw_failure_terminal = payload["post_seal_failure_terminal"]
        if raw_failure_terminal is None:
            failure_terminal = None
        elif isinstance(raw_failure_terminal, Mapping):
            try:
                failure_terminal = PostSealFailureTerminal.from_dict(
                    raw_failure_terminal,
                    authority_bundle=bundle,
                    authority_transition=authority_transition,
                )
            except (TypeError, ValueError) as exc:
                raise PostExecutionWorkflowError(
                    "post_execution_result_failure_terminal_invalid"
                ) from exc
        else:
            raise PostExecutionWorkflowError(
                "post_execution_result_failure_terminal_invalid"
            )

        evidence_entries = _accepted_evidence_entries(
            semantic.authority_bundle_inputs.execution_result,
            semantic,
        )
        raw_narrative = payload["narrative_workflow"]
        raw_flow = payload["publication_flow"]
        raw_compose = payload["compose_transition"]
        if raw_narrative is None:
            if raw_flow is not None or raw_compose is not None:
                raise PostExecutionWorkflowError(
                    "post_execution_result_narrative_closure_invalid"
                )
            narrative = None
            flow = None
            compose = None
        else:
            if not isinstance(raw_narrative, Mapping) or not isinstance(
                raw_compose, Mapping
            ):
                raise PostExecutionWorkflowError(
                    "post_execution_result_narrative_closure_invalid"
                )
            try:
                narrative = NarrativeWorkflowResult.from_dict(
                    raw_narrative,
                    authority_bundle=bundle,
                    claim_settlement=semantic.settlement,
                    evidence_entries=evidence_entries,
                    recommendations=semantic.recommendations,
                )
                compose = DurableTransition.from_dict(raw_compose)
                flow = (
                    None
                    if raw_flow is None
                    else PublicationFlowResult.from_dict(
                        raw_flow,
                        authority_inputs=semantic.authority_bundle_inputs,
                        authority_bundle=bundle,
                        claim_settlement=semantic.settlement,
                        recommendations=semantic.recommendations,
                        narrative_workflow=narrative,
                        supersedes_publication=None,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise PostExecutionWorkflowError(
                    "post_execution_result_narrative_closure_invalid"
                ) from exc

        raw_customer_payload = payload["customer_payload"]
        if raw_customer_payload is not None and not isinstance(
            raw_customer_payload, Mapping
        ):
            raise PostExecutionWorkflowError(
                "post_execution_result_customer_payload_invalid"
            )
        expected_authority_input, expected_authority_output = (
            semantic_authority_transition_payloads(
                semantic,
                bundle,
                claim_coverage_checkpoint_ref=payload["claim_coverage_checkpoint_ref"],
                claim_coverage_checkpoint_digest=payload[
                    "claim_coverage_checkpoint_digest"
                ],
            )
        )
        if authority_transition.input_digest != canonical_digest(
            expected_authority_input
        ) or authority_transition.output_digest != canonical_digest(
            expected_authority_output
        ):
            raise PostExecutionWorkflowError(
                "post_execution_result_authority_transition_invalid"
            )
        if narrative is not None and compose is not None:
            expected_compose_input, expected_compose_output = (
                narrative_publication_transition_payloads(
                    authority_inputs=semantic.authority_bundle_inputs,
                    authority_bundle=bundle,
                    claim_settlement=semantic.settlement,
                    recommendations=semantic.recommendations,
                    narrative_workflow=narrative,
                    publication_flow=flow,
                    supersedes_publication=None,
                )
            )
            if compose.input_digest != canonical_digest(
                expected_compose_input
            ) or compose.output_digest != canonical_digest(expected_compose_output):
                raise PostExecutionWorkflowError(
                    "post_execution_result_narrative_closure_invalid"
                )
        rebuilt = _build_result(
            status=payload["status"],
            semantic=semantic,
            bundle=bundle,
            authority_transition=authority_transition,
            claim_coverage_checkpoint_ref=payload["claim_coverage_checkpoint_ref"],
            claim_coverage_checkpoint_digest=payload[
                "claim_coverage_checkpoint_digest"
            ],
            claim_coverage_transition_id=payload["claim_coverage_transition_id"],
            authority_persistence_status=payload["authority_persistence_status"],
            failure_terminal=failure_terminal,
            failure_persistence_status=payload["post_seal_failure_persistence_status"],
            material_projection_ref=payload["narrative_material_projection_ref"],
            material_projection_digest=payload["narrative_material_projection_digest"],
            material_persistence_status=payload[
                "narrative_material_persistence_status"
            ],
            narrative=narrative,
            flow=flow,
            compose_transition=compose,
            narrative_persistence_status=payload["narrative_persistence_status"],
            customer_payload_ref=payload["customer_payload_ref"],
            delivery_attempt_ref=payload["delivery_attempt_ref"],
            delivery_status=payload["delivery_status"],
            delivery_replayed=payload["delivery_replayed"],
            customer_publication_ref=payload["customer_publication_ref"],
            customer_payload=raw_customer_payload,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise PostExecutionWorkflowError("post_execution_result_integrity_invalid")
        return rebuilt

    def replay(self) -> "PostExecutionWorkflowResult":
        return self.from_dict(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_ref": self.result_ref,
            "status": self.status,
            "run_attempt_id": self.run_attempt_id,
            "intent_revision_id": self.intent_revision_id,
            "claim_coverage_checkpoint_ref": (self.claim_coverage_checkpoint_ref),
            "claim_coverage_checkpoint_digest": (self.claim_coverage_checkpoint_digest),
            "claim_coverage_transition_id": self.claim_coverage_transition_id,
            "semantic_authority_result_ref": self.semantic_authority_result_ref,
            "semantic_authority_result_digest": (self.semantic_authority_result_digest),
            "authority_bundle_ref": self.authority_bundle_ref,
            "authority_bundle_digest": self.authority_bundle_digest,
            "authority_transition_id": self.authority_transition_id,
            "authority_persistence_status": self.authority_persistence_status,
            "post_seal_failure_terminal_ref": (self.post_seal_failure_terminal_ref),
            "post_seal_failure_persistence_status": (
                self.post_seal_failure_persistence_status
            ),
            "semantic_authority_result": self.semantic_authority_result.to_dict(),
            "authority_bundle": self.authority_bundle.to_dict(),
            "authority_transition": self.authority_transition.to_dict(),
            "post_seal_failure_terminal": (
                None
                if self.post_seal_failure_terminal is None
                else self.post_seal_failure_terminal.to_dict()
            ),
            "narrative_material_projection_ref": (
                self.narrative_material_projection_ref
            ),
            "narrative_material_projection_digest": (
                self.narrative_material_projection_digest
            ),
            "narrative_material_persistence_status": (
                self.narrative_material_persistence_status
            ),
            "narrative_workflow_ref": self.narrative_workflow_ref,
            "narrative_workflow_digest": self.narrative_workflow_digest,
            "compose_transition_id": self.compose_transition_id,
            "narrative_persistence_status": self.narrative_persistence_status,
            "narrative_workflow": (
                None
                if self.narrative_workflow is None
                else self.narrative_workflow.to_dict()
            ),
            "publication_flow": (
                None
                if self.publication_flow is None
                else self.publication_flow.to_dict()
            ),
            "compose_transition": (
                None
                if self.compose_transition is None
                else self.compose_transition.to_dict()
            ),
            "publication_ref": self.publication_ref,
            "outbox_ref": self.outbox_ref,
            "customer_payload_ref": self.customer_payload_ref,
            "delivery_attempt_ref": self.delivery_attempt_ref,
            "delivery_status": self.delivery_status,
            "delivery_replayed": self.delivery_replayed,
            "customer_publication_ref": self.customer_publication_ref,
            "customer_payload": (
                None
                if self.customer_payload is None
                else canonical_value(self.customer_payload)
            ),
            "content_digest": self.content_digest,
        }


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PostExecutionWorkflowError(error)
    return value


def _optional_string(value: Any, error: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, error)


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _post_execution_dependency_manifest(
    *,
    status: str,
    run_attempt_id: str,
    intent_revision_id: str,
    claim_coverage_checkpoint_ref: str,
    claim_coverage_checkpoint_digest: str,
    claim_coverage_transition_id: str,
    semantic: SemanticAuthorityResult,
    bundle: AuthorityBundle,
    authority_transition: DurableTransition,
    authority_persistence_status: str,
    failure_terminal: PostSealFailureTerminal | None,
    failure_persistence_status: str,
    material_projection_ref: str | None,
    material_projection_digest: str | None,
    material_persistence_status: str,
    narrative: NarrativeWorkflowResult | None,
    flow: PublicationFlowResult | None,
    compose_transition: DurableTransition | None,
    narrative_persistence_status: str,
    customer_payload_ref: str | None,
    delivery_attempt_ref: str | None,
    delivery_status: str | None,
    delivery_replayed: bool | None,
    customer_publication_ref: str | None,
    customer_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "run_attempt_id": run_attempt_id,
        "intent_revision_id": intent_revision_id,
        "claim_coverage_checkpoint_ref": claim_coverage_checkpoint_ref,
        "claim_coverage_checkpoint_digest": claim_coverage_checkpoint_digest,
        "claim_coverage_transition_id": claim_coverage_transition_id,
        "semantic_authority_result_ref": semantic.result_ref,
        "semantic_authority_result_digest": semantic.content_digest,
        "authority_bundle_ref": bundle.bundle_ref,
        "authority_bundle_digest": bundle.bundle_digest,
        "authority_transition": authority_transition.to_dict(),
        "authority_persistence_status": authority_persistence_status,
        "post_seal_failure_terminal_ref": (
            None if failure_terminal is None else failure_terminal.terminal_ref
        ),
        "post_seal_failure_terminal_digest": (
            None if failure_terminal is None else failure_terminal.content_digest
        ),
        "post_seal_failure_persistence_status": failure_persistence_status,
        "narrative_material_projection_ref": material_projection_ref,
        "narrative_material_projection_digest": material_projection_digest,
        "narrative_material_persistence_status": material_persistence_status,
        "narrative_workflow_ref": (
            None
            if narrative is None
            else "narrative-workflow-result:sha256:" + narrative.content_digest
        ),
        "narrative_workflow_digest": (
            None if narrative is None else narrative.content_digest
        ),
        "compose_transition": (
            None if compose_transition is None else compose_transition.to_dict()
        ),
        "narrative_persistence_status": narrative_persistence_status,
        "publication_flow_ref": None if flow is None else flow.flow_ref,
        "publication_flow_digest": None if flow is None else flow.content_digest,
        "publication_ref": (None if flow is None else flow.publication.publication_ref),
        "publication_digest": (
            None if flow is None else flow.publication.publication_digest
        ),
        "outbox_ref": None if flow is None else flow.outbox.outbox_ref,
        "outbox_digest": None if flow is None else flow.outbox.content_digest,
        "customer_payload_ref": customer_payload_ref,
        "delivery_attempt_ref": delivery_attempt_ref,
        "delivery_status": delivery_status,
        "delivery_replayed": delivery_replayed,
        "customer_publication_ref": customer_publication_ref,
        "customer_payload_digest": (
            None if customer_payload is None else canonical_digest(customer_payload)
        ),
    }


def _validated_execution(
    execution_result: AuthoritativeExecutionResult,
) -> AuthoritativeExecutionResult:
    try:
        return validate_typed_authoritative_execution_result(execution_result)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PostExecutionWorkflowError(
            "post_execution_authority_result_invalid"
        ) from exc


def _validated_claim_coverage_checkpoint(
    value: ClaimCoverageCheckpoint,
    *,
    execution: AuthoritativeExecutionResult,
) -> ClaimCoverageCheckpoint:
    if type(value) is not ClaimCoverageCheckpoint:
        raise PostExecutionWorkflowError(
            "post_execution_claim_coverage_checkpoint_invalid"
        )
    try:
        checkpoint = ClaimCoverageCheckpoint.create(
            plan_revision=execution.plan_revision,
            execution_result=execution,
            evaluation=value.evaluation,
            decision=value.decision,
            plan_patch=value.plan_patch,
            transition=value.transition,
        )
    except (AttributeError, TypeError, ValueError, ClaimCoverageContractError) as exc:
        raise PostExecutionWorkflowError(
            "post_execution_claim_coverage_checkpoint_invalid"
        ) from exc
    if (
        checkpoint != value
        or checkpoint.decision.decision != "seal"
        or checkpoint.plan_patch is not None
        or checkpoint.source_plan_revision_id != execution.plan_revision_id
        or checkpoint.source_execution_result_ref
        != execution.authoritative_execution_result_ref
        or checkpoint.transition.next_transition != "seal_authority_bundle"
        or checkpoint.checkpoint_ref
        != "claim-coverage-checkpoint:sha256:" + checkpoint.content_digest
    ):
        raise PostExecutionWorkflowError(
            "post_execution_claim_coverage_checkpoint_invalid"
        )
    return checkpoint


def _validated_intent(
    intent_revision: IntentRevision,
    *,
    execution: AuthoritativeExecutionResult,
) -> IntentRevision:
    if type(intent_revision) is not IntentRevision:
        raise PostExecutionWorkflowError("post_execution_intent_revision_invalid")
    try:
        replayed = IntentRevision.from_dict(intent_revision.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise PostExecutionWorkflowError(
            "post_execution_intent_revision_invalid"
        ) from exc
    if (
        replayed != intent_revision
        or replayed.run_attempt_id != execution.run_attempt_id
        or replayed.intent_revision_id != execution.intent_revision_id
    ):
        raise PostExecutionWorkflowError("post_execution_intent_revision_invalid")
    return replayed


def _validated_policy(
    visibility_policy: PublicationFieldVisibilityPolicy,
) -> PublicationFieldVisibilityPolicy:
    if type(visibility_policy) is not PublicationFieldVisibilityPolicy:
        raise PostExecutionWorkflowError("post_execution_visibility_policy_invalid")
    try:
        replayed = PublicationFieldVisibilityPolicy.from_dict(
            visibility_policy.to_dict()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PostExecutionWorkflowError(
            "post_execution_visibility_policy_invalid"
        ) from exc
    if replayed != visibility_policy:
        raise PostExecutionWorkflowError("post_execution_visibility_policy_invalid")
    return replayed


def _semantic_transition_input(
    execution: AuthoritativeExecutionResult,
    namespace: ClaimAuthorityNamespace,
    *,
    claim_coverage_checkpoint_ref: str,
    claim_coverage_checkpoint_digest: str,
) -> dict[str, Any]:
    checkpoint_ref = _required_string(
        claim_coverage_checkpoint_ref,
        "post_execution_claim_coverage_checkpoint_ref_invalid",
    )
    if (
        not _valid_digest(claim_coverage_checkpoint_digest)
        or checkpoint_ref
        != "claim-coverage-checkpoint:sha256:" + claim_coverage_checkpoint_digest
    ):
        raise PostExecutionWorkflowError(
            "post_execution_claim_coverage_checkpoint_ref_invalid"
        )
    return {
        "authoritative_execution_result_ref": (
            execution.authoritative_execution_result_ref
        ),
        "authoritative_execution_result_digest": execution.content_digest,
        "authority_namespace_ref": namespace.authority_namespace_ref,
        "claim_coverage_checkpoint_ref": checkpoint_ref,
        "claim_coverage_checkpoint_digest": claim_coverage_checkpoint_digest,
    }


def _narrative_transition_input(
    *,
    bundle: AuthorityBundle,
    semantic: SemanticAuthorityResult,
    material_projection_ref: str,
    material_projection_digest: str,
    visibility_policy: PublicationFieldVisibilityPolicy,
    answer_context: NarrativeAnswerContext,
) -> dict[str, Any]:
    return canonical_value(
        {
            "authority_bundle_ref": bundle.bundle_ref,
            "authority_bundle_digest": bundle.bundle_digest,
            "claim_settlement_ref": semantic.settlement.settlement_ref,
            "claim_settlement_digest": semantic.settlement.content_digest,
            "recommendation_refs": tuple(
                item.recommendation_ref for item in semantic.recommendations
            ),
            "narrative_material_projection_ref": material_projection_ref,
            "narrative_material_projection_digest": material_projection_digest,
            "visibility_policy_ref": visibility_policy.policy_ref,
            "visibility_policy_digest": visibility_policy.content_digest,
            "answer_context_ref": answer_context.context_ref,
            "answer_context_digest": answer_context.content_digest,
        }
    )


def _load_accepted_authority(
    *,
    authority_store: AcceptedTransitionStore,
    attempt_journal: DurableCallJournal,
    execution: AuthoritativeExecutionResult,
    namespace: ClaimAuthorityNamespace,
    claim_coverage_checkpoint: ClaimCoverageCheckpoint,
) -> tuple[SemanticAuthorityResult, AuthorityBundle, DurableTransition] | None:
    checkpoint = _validated_claim_coverage_checkpoint(
        claim_coverage_checkpoint,
        execution=execution,
    )
    expected_input = _semantic_transition_input(
        execution,
        namespace,
        claim_coverage_checkpoint_ref=checkpoint.checkpoint_ref,
        claim_coverage_checkpoint_digest=checkpoint.content_digest,
    )
    accepted = authority_store.load_accepted_transition(
        run_attempt_id=execution.run_attempt_id,
        node_name="settle_claim_authority",
        input_digest=canonical_digest(expected_input),
    )
    if accepted is None:
        return None
    if not isinstance(accepted, Mapping) or set(accepted) != {
        "transition",
        "input_payload",
        "output_payload",
    }:
        raise PostExecutionWorkflowError("post_execution_authority_transition_invalid")
    transition = accepted["transition"]
    output = accepted["output_payload"]
    if not isinstance(transition, DurableTransition) or not isinstance(output, Mapping):
        raise PostExecutionWorkflowError("post_execution_authority_transition_invalid")
    try:
        semantic = SemanticAuthorityResult.from_dict(
            output["semantic_authority_result"]
        )
        bundle = AuthorityBundle.from_dict(
            output["authority_bundle"],
            authority_inputs=semantic.authority_bundle_inputs,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PostExecutionWorkflowError(
            "post_execution_authority_transition_invalid"
        ) from exc
    expected_input_replayed, expected_output = semantic_authority_transition_payloads(
        semantic,
        bundle,
        claim_coverage_checkpoint_ref=checkpoint.checkpoint_ref,
        claim_coverage_checkpoint_digest=checkpoint.content_digest,
    )
    if (
        set(output) != {"semantic_authority_result", "authority_bundle"}
        or canonical_value(accepted["input_payload"]) != canonical_value(expected_input)
        or canonical_value(expected_input_replayed) != canonical_value(expected_input)
        or canonical_value(output) != canonical_value(expected_output)
        or transition.node_name != "settle_claim_authority"
        or transition.parent_transition_id != checkpoint.transition_id
        or transition.run_attempt_id != execution.run_attempt_id
        or transition.intent_revision_id != execution.intent_revision_id
        or transition.decision_ledger_position
        != checkpoint.transition.decision_ledger_position
        or transition.input_digest != canonical_digest(expected_input)
        or transition.output_digest != canonical_digest(expected_output)
        or transition.status != "succeeded"
        or transition.acceptance_state != "accepted"
        or transition.next_transition != "compose_claim_aware_narrative"
        or semantic.authority_bundle_inputs.execution_result != execution
        or semantic.authority_bundle_inputs.authority_namespace != namespace
    ):
        raise PostExecutionWorkflowError("post_execution_authority_transition_invalid")
    try:
        attempt_journal.load_stage_attempt_refs(
            run_attempt_id=transition.run_attempt_id,
            transition_attempt_id=transition.attempt_id,
            stage_name="settle_claim_authority",
        )
    except DurableCallJournalError as exc:
        raise PostExecutionWorkflowError(
            "post_execution_authority_stage_seal_invalid"
        ) from exc
    return semantic, bundle, transition


def _load_accepted_narrative(
    *,
    authority_store: AcceptedTransitionStore,
    attempt_journal: DurableCallJournal,
    bundle: AuthorityBundle,
    semantic: SemanticAuthorityResult,
    evidence_entries: tuple[EvidenceLedgerEntry, ...],
    parent_transition: DurableTransition,
    transition_input: Mapping[str, Any],
    destination_ref: str,
    channel: str,
) -> (
    tuple[
        NarrativeWorkflowResult,
        PublicationFlowResult | None,
        DurableTransition,
    ]
    | None
):
    accepted = authority_store.load_accepted_transition(
        run_attempt_id=bundle.run_attempt_id,
        node_name="compose_claim_aware_narrative",
        input_digest=canonical_digest(transition_input),
    )
    if accepted is None:
        return None
    if not isinstance(accepted, Mapping) or set(accepted) != {
        "transition",
        "input_payload",
        "output_payload",
    }:
        raise PostExecutionWorkflowError("post_execution_narrative_transition_invalid")
    transition = accepted["transition"]
    output = accepted["output_payload"]
    if not isinstance(transition, DurableTransition) or not isinstance(output, Mapping):
        raise PostExecutionWorkflowError("post_execution_narrative_transition_invalid")
    try:
        narrative = NarrativeWorkflowResult.from_dict(
            output["narrative_workflow_result"],
            authority_bundle=bundle,
            claim_settlement=semantic.settlement,
            evidence_entries=evidence_entries,
            recommendations=semantic.recommendations,
        )
        raw_flow = output["publication_flow"]
        if not isinstance(raw_flow, Mapping):
            raise PostExecutionWorkflowError(
                "post_execution_narrative_transition_invalid"
            )
        flow = PublicationFlowResult.from_dict(
            raw_flow,
            authority_inputs=semantic.authority_bundle_inputs,
            authority_bundle=bundle,
            claim_settlement=semantic.settlement,
            recommendations=semantic.recommendations,
            narrative_workflow=narrative,
            supersedes_publication=None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PostExecutionWorkflowError(
            "post_execution_narrative_transition_invalid"
        ) from exc
    expected_input, expected_output = narrative_publication_transition_payloads(
        authority_inputs=semantic.authority_bundle_inputs,
        authority_bundle=bundle,
        claim_settlement=semantic.settlement,
        recommendations=semantic.recommendations,
        narrative_workflow=narrative,
        publication_flow=flow,
        supersedes_publication=None,
    )
    if (
        set(output)
        != {"narrative_workflow_result", "publication_flow", "publication_state"}
        or canonical_value(accepted["input_payload"])
        != canonical_value(transition_input)
        or canonical_value(expected_input) != canonical_value(transition_input)
        or canonical_value(output) != canonical_value(expected_output)
        or transition.node_name != "compose_claim_aware_narrative"
        or transition.parent_transition_id != parent_transition.transition_id
        or transition.run_attempt_id != bundle.run_attempt_id
        or transition.intent_revision_id != bundle.intent_revision_id
        or transition.decision_ledger_position
        != parent_transition.decision_ledger_position
        or transition.input_digest != canonical_digest(expected_input)
        or transition.output_digest != canonical_digest(expected_output)
        or transition.status != "succeeded"
        or transition.acceptance_state != "accepted"
        or transition.next_transition != "deliver_publication"
        or flow.outbox.destination_ref != destination_ref
        or flow.outbox.channel != channel
    ):
        raise PostExecutionWorkflowError("post_execution_narrative_transition_invalid")
    try:
        attempt_journal.load_stage_attempt_refs(
            run_attempt_id=transition.run_attempt_id,
            transition_attempt_id=transition.attempt_id,
            stage_name="compose_claim_aware_narrative",
        )
    except DurableCallJournalError as exc:
        raise PostExecutionWorkflowError(
            "post_execution_narrative_stage_seal_invalid"
        ) from exc
    return narrative, flow, transition


def _accepted_evidence_entries(
    execution: AuthoritativeExecutionResult,
    semantic: SemanticAuthorityResult,
) -> tuple[EvidenceLedgerEntry, ...]:
    authority_inputs = semantic.authority_bundle_inputs
    if authority_inputs.execution_result != execution:
        raise PostExecutionWorkflowError(
            "post_execution_public_fact_evidence_incomplete"
        )
    try:
        return authority_inputs.material_projection_evidence_entries()
    except (AttributeError, TypeError, ValueError) as exc:
        raise PostExecutionWorkflowError(
            "post_execution_public_fact_evidence_incomplete"
        ) from exc


def _customer_payload_ref(
    *,
    flow: PublicationFlowResult,
    narrative: NarrativeWorkflowResult,
) -> str:
    payload = canonical_value(flow.customer_payload)
    body = {
        "run_attempt_id": flow.outbox.run_attempt_id,
        "outbox_ref": flow.outbox.outbox_ref,
        "publication_ref": flow.outbox.publication_ref,
        "publication_digest": flow.outbox.publication_digest,
        "projection_id": flow.outbox.projection_id,
        "projection_digest": flow.outbox.projection_digest,
        "field_visibility_policy_ref": narrative.visibility_policy.policy_ref,
        "field_visibility_policy_digest": (narrative.visibility_policy.content_digest),
        "customer_payload_digest": canonical_digest(payload),
        "customer_payload": payload,
    }
    return "customer-payload:sha256:" + canonical_digest(body)


_PERSISTED_CUSTOMER_PAYLOAD_SQL = """
/* post_execution_persisted_customer_payload */
SELECT
  customer.customer_payload_ref,
  customer.customer_payload,
  customer.payload AS customer_payload_record,
  publication.customer_publication_ref,
  publication.payload AS customer_publication_record
FROM waje_runtime.publication_customer_payloads customer
JOIN waje_runtime.customer_publications publication
  ON publication.owner_ref = customer.owner_ref
 AND publication.run_attempt_id = customer.run_attempt_id
 AND publication.outbox_ref = customer.outbox_ref
WHERE customer.owner_ref = %(owner_ref)s
  AND customer.run_attempt_id = %(run_attempt_id)s
  AND customer.outbox_ref = %(outbox_ref)s
  AND customer.customer_payload_ref = %(customer_payload_ref)s
  AND publication.customer_publication_ref = %(customer_publication_ref)s
"""


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _persisted_customer_payload(
    connection: Any,
    *,
    owner_ref: str,
    bundle: AuthorityBundle,
    narrative: NarrativeWorkflowResult,
    flow: PublicationFlowResult,
    customer_payload_ref: str,
    delivery: DeliveryPersistenceResult,
) -> Mapping[str, Any]:
    if delivery.status != "published" or delivery.customer_publication_ref is None:
        raise PostExecutionWorkflowError(
            "post_execution_customer_publication_not_published"
        )
    try:
        row = connection.execute(
            _PERSISTED_CUSTOMER_PAYLOAD_SQL,
            {
                "owner_ref": owner_ref,
                "run_attempt_id": bundle.run_attempt_id,
                "outbox_ref": flow.outbox.outbox_ref,
                "customer_payload_ref": customer_payload_ref,
                "customer_publication_ref": delivery.customer_publication_ref,
            },
        ).fetchone()
        if row is None:
            raise PostExecutionWorkflowError(
                "post_execution_customer_publication_missing"
            )
        persisted_ref = _required_string(
            _field(row, "customer_payload_ref", 0),
            "post_execution_customer_payload_ref_invalid",
        )
        customer_payload = _field(row, "customer_payload", 1)
        customer_record = _field(row, "customer_payload_record", 2)
        customer_publication_ref = _required_string(
            _field(row, "customer_publication_ref", 3),
            "post_execution_customer_publication_ref_invalid",
        )
        publication_record = _field(row, "customer_publication_record", 4)
        if not all(
            isinstance(item, Mapping)
            for item in (customer_payload, customer_record, publication_record)
        ):
            raise PostExecutionWorkflowError(
                "post_execution_customer_publication_invalid"
            )
        normalized_payload = canonical_value(customer_payload)
        normalized_customer_record = canonical_value(customer_record)
        normalized_publication_record = canonical_value(publication_record)
        customer_fields = {
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
        }
        publication_fields = {
            "customer_publication_ref",
            "run_attempt_id",
            "outbox_ref",
            "delivery_attempt_ref",
            "publication_ref",
            "projection_id",
            "destination_ref",
            "channel",
            "transport_receipt_ref",
            "content_digest",
        }
        if (
            set(normalized_customer_record) != customer_fields
            or set(normalized_publication_record) != publication_fields
        ):
            raise PostExecutionWorkflowError(
                "post_execution_customer_publication_invalid"
            )
        customer_body = {
            key: value
            for key, value in normalized_customer_record.items()
            if key not in {"customer_payload_ref", "content_digest"}
        }
        customer_digest = canonical_digest(customer_body)
        publication_body = {
            key: value
            for key, value in normalized_publication_record.items()
            if key not in {"customer_publication_ref", "content_digest"}
        }
        publication_digest = canonical_digest(publication_body)
        if (
            persisted_ref != customer_payload_ref
            or customer_publication_ref != delivery.customer_publication_ref
            or normalized_customer_record["customer_payload_ref"] != persisted_ref
            or normalized_customer_record["content_digest"] != customer_digest
            or persisted_ref != "customer-payload:sha256:" + customer_digest
            or normalized_customer_record["customer_payload_digest"]
            != canonical_digest(normalized_payload)
            or normalized_customer_record["customer_payload"] != normalized_payload
            or normalized_customer_record["run_attempt_id"] != bundle.run_attempt_id
            or normalized_customer_record["outbox_ref"] != flow.outbox.outbox_ref
            or normalized_customer_record["publication_ref"]
            != flow.publication.publication_ref
            or normalized_customer_record["publication_digest"]
            != flow.publication.publication_digest
            or normalized_customer_record["projection_id"]
            != flow.projection.projection_id
            or normalized_customer_record["projection_digest"]
            != flow.projection.projection_digest
            or normalized_customer_record["field_visibility_policy_ref"]
            != narrative.visibility_policy.policy_ref
            or normalized_customer_record["field_visibility_policy_digest"]
            != narrative.visibility_policy.content_digest
            or normalized_publication_record["customer_publication_ref"]
            != customer_publication_ref
            or normalized_publication_record["content_digest"] != publication_digest
            or customer_publication_ref
            != "customer-publication:sha256:" + publication_digest
            or normalized_publication_record["run_attempt_id"] != bundle.run_attempt_id
            or normalized_publication_record["outbox_ref"] != flow.outbox.outbox_ref
            or normalized_publication_record["delivery_attempt_ref"]
            != delivery.attempt_ref
            or normalized_publication_record["publication_ref"]
            != flow.publication.publication_ref
            or normalized_publication_record["projection_id"]
            != flow.projection.projection_id
            or normalized_publication_record["destination_ref"]
            != flow.outbox.destination_ref
            or normalized_publication_record["channel"] != flow.outbox.channel
            or normalized_payload != canonical_value(flow.customer_payload)
        ):
            raise PostExecutionWorkflowError(
                "post_execution_customer_publication_invalid"
            )
        narrative.visibility_policy.validate_customer_payload(normalized_payload)
        connection.commit()
        return normalized_payload
    except Exception:
        connection.rollback()
        raise


def _build_result(
    *,
    status: str,
    semantic: SemanticAuthorityResult,
    bundle: AuthorityBundle,
    authority_transition: DurableTransition,
    claim_coverage_checkpoint_ref: str,
    claim_coverage_checkpoint_digest: str,
    claim_coverage_transition_id: str,
    authority_persistence_status: str,
    material_projection_ref: str | None,
    material_projection_digest: str | None,
    material_persistence_status: str,
    narrative: NarrativeWorkflowResult | None,
    flow: PublicationFlowResult | None,
    compose_transition: DurableTransition | None,
    narrative_persistence_status: str,
    customer_payload_ref: str | None,
    delivery_attempt_ref: str | None,
    delivery_status: str | None,
    delivery_replayed: bool | None,
    customer_publication_ref: str | None,
    customer_payload: Mapping[str, Any] | None,
    failure_terminal: PostSealFailureTerminal | None = None,
    failure_persistence_status: str = "not_started",
    replay_nested_authorities: bool = True,
) -> PostExecutionWorkflowResult:
    if status not in POST_EXECUTION_STATUSES:
        raise PostExecutionWorkflowError("post_execution_status_invalid")
    if authority_persistence_status not in _PERSISTENCE_STATES - {"not_started"}:
        raise PostExecutionWorkflowError(
            "post_execution_authority_persistence_status_invalid"
        )
    if narrative_persistence_status not in _PERSISTENCE_STATES:
        raise PostExecutionWorkflowError(
            "post_execution_narrative_persistence_status_invalid"
        )
    if material_persistence_status not in _PERSISTENCE_STATES:
        raise PostExecutionWorkflowError(
            "post_execution_material_persistence_status_invalid"
        )
    if failure_persistence_status not in _PERSISTENCE_STATES:
        raise PostExecutionWorkflowError(
            "post_execution_failure_persistence_status_invalid"
        )
    if (
        type(semantic) is not SemanticAuthorityResult
        or type(bundle) is not AuthorityBundle
        or type(authority_transition) is not DurableTransition
    ):
        raise PostExecutionWorkflowError(
            "post_execution_result_authority_closure_invalid"
        )
    try:
        replayed_authority_transition = DurableTransition.from_dict(
            authority_transition.to_dict()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PostExecutionWorkflowError(
            "post_execution_result_authority_closure_invalid"
        ) from exc
    if replayed_authority_transition != authority_transition:
        raise PostExecutionWorkflowError(
            "post_execution_result_authority_closure_invalid"
        )
    authority_inputs = semantic.authority_bundle_inputs
    checkpoint_ref = _required_string(
        claim_coverage_checkpoint_ref,
        "post_execution_result_claim_coverage_checkpoint_invalid",
    )
    checkpoint_transition_id = _required_string(
        claim_coverage_transition_id,
        "post_execution_result_claim_coverage_checkpoint_invalid",
    )
    if (
        not _valid_digest(claim_coverage_checkpoint_digest)
        or checkpoint_ref
        != "claim-coverage-checkpoint:sha256:" + claim_coverage_checkpoint_digest
    ):
        raise PostExecutionWorkflowError(
            "post_execution_result_claim_coverage_checkpoint_invalid"
        )
    expected_authority_input = _semantic_transition_input(
        authority_inputs.execution_result,
        authority_inputs.authority_namespace,
        claim_coverage_checkpoint_ref=checkpoint_ref,
        claim_coverage_checkpoint_digest=claim_coverage_checkpoint_digest,
    )
    if (
        not _valid_digest(semantic.content_digest)
        or semantic.result_ref
        != "semantic-authority-result:sha256:" + semantic.content_digest
        or not _valid_digest(bundle.bundle_digest)
        or bundle.content_digest != bundle.bundle_digest
        or not bundle.bundle_ref.endswith(":sha256:" + bundle.bundle_digest)
        or bundle.seal_state != "sealed"
        or bundle.run_attempt_id != authority_inputs.run_attempt_id
        or bundle.intent_revision_id != authority_inputs.intent_revision_id
        or bundle.plan_revision_id != authority_inputs.plan_revision_id
        or bundle.authority_namespace_ref != authority_inputs.authority_namespace_ref
        or bundle.execution_result_ref != authority_inputs.execution_result_ref
        or bundle.execution_result_digest != authority_inputs.execution_result_digest
        or bundle.claim_settlement_ref != semantic.settlement.settlement_ref
        or bundle.claim_settlement_digest != semantic.settlement.content_digest
        or tuple(bundle.recommendation_refs)
        != tuple(item.recommendation_ref for item in semantic.recommendations)
        or authority_transition.node_name != "settle_claim_authority"
        or authority_transition.parent_transition_id != checkpoint_transition_id
        or authority_transition.run_attempt_id != bundle.run_attempt_id
        or authority_transition.intent_revision_id != bundle.intent_revision_id
        or authority_transition.decision_ledger_position
        != authority_inputs.execution_result.durable_transition.decision_ledger_position
        or authority_transition.input_digest
        != canonical_digest(expected_authority_input)
        or not _valid_digest(authority_transition.output_digest)
        or authority_transition.status != "succeeded"
        or authority_transition.acceptance_state != "accepted"
        or authority_transition.next_transition != "compose_claim_aware_narrative"
    ):
        raise PostExecutionWorkflowError(
            "post_execution_result_authority_transition_invalid"
        )

    projection_ref = _optional_string(
        material_projection_ref,
        "post_execution_result_material_projection_invalid",
    )
    projection_digest = _optional_string(
        material_projection_digest,
        "post_execution_result_material_projection_invalid",
    )
    if (projection_ref is None) != (projection_digest is None) or (
        projection_ref is not None
        and (
            not _valid_digest(projection_digest)
            or projection_ref
            != "narrative-material-projection:sha256:" + projection_digest
        )
    ):
        raise PostExecutionWorkflowError(
            "post_execution_result_material_projection_invalid"
        )
    if status == "authority_sealed":
        if projection_ref is not None or material_persistence_status != "not_started":
            raise PostExecutionWorkflowError(
                "post_execution_result_material_projection_invalid"
            )
    elif projection_ref is None:
        raise PostExecutionWorkflowError(
            "post_execution_result_material_projection_invalid"
        )

    replayed_failure_terminal: PostSealFailureTerminal | None = None
    if failure_terminal is None:
        if (
            status in POST_SEAL_FAILURE_STATUSES
            or failure_persistence_status != "not_started"
        ):
            raise PostExecutionWorkflowError(
                "post_execution_result_failure_terminal_invalid"
            )
    else:
        if (
            status not in POST_SEAL_FAILURE_STATUSES
            or failure_persistence_status not in {"inserted", "replayed"}
        ):
            raise PostExecutionWorkflowError(
                "post_execution_result_failure_terminal_invalid"
            )
        try:
            replayed_failure_terminal = PostSealFailureTerminal.from_dict(
                failure_terminal.to_dict(),
                authority_bundle=bundle,
                authority_transition=authority_transition,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise PostExecutionWorkflowError(
                "post_execution_result_failure_terminal_invalid"
            ) from exc
        if (
            replayed_failure_terminal != failure_terminal
            or replayed_failure_terminal.status != status
            or projection_ref
            not in replayed_failure_terminal.failure_record.affected_refs
        ):
            raise PostExecutionWorkflowError(
                "post_execution_result_failure_terminal_invalid"
            )

    try:
        evidence_entries = authority_inputs.material_projection_evidence_entries()
    except (AttributeError, TypeError, ValueError) as exc:
        raise PostExecutionWorkflowError(
            "post_execution_public_fact_evidence_incomplete"
        ) from exc
    replayed_narrative: NarrativeWorkflowResult | None = None
    replayed_flow: PublicationFlowResult | None = None
    replayed_compose: DurableTransition | None = None
    if narrative is None:
        if (
            flow is not None
            or compose_transition is not None
            or narrative_persistence_status != "not_started"
            or status not in {"authority_sealed", *POST_SEAL_FAILURE_STATUSES}
            or (
                status == "narrative_failed"
                and material_persistence_status not in {"inserted", "replayed"}
            )
        ):
            raise PostExecutionWorkflowError(
                "post_execution_result_narrative_closure_invalid"
            )
    else:
        if (
            compose_transition is None
            or narrative_persistence_status == "not_started"
            or material_persistence_status not in {"inserted", "replayed"}
            or projection_ref != narrative.material_projection.projection_ref
            or projection_digest != narrative.material_projection.content_digest
        ):
            raise PostExecutionWorkflowError(
                "post_execution_result_narrative_closure_invalid"
            )
        if replay_nested_authorities:
            try:
                replayed_narrative = validate_typed_narrative_workflow_result(
                    narrative,
                    authority_bundle=bundle,
                    claim_settlement=semantic.settlement,
                    recommendations=semantic.recommendations,
                    evidence_entries=evidence_entries,
                )
                replayed_flow = (
                    None
                    if flow is None
                    else validate_typed_publication_flow(
                        flow,
                        authority_inputs=semantic.authority_bundle_inputs,
                        authority_bundle=bundle,
                        claim_settlement=semantic.settlement,
                        recommendations=semantic.recommendations,
                        narrative_workflow=replayed_narrative,
                        supersedes_publication=None,
                    )
                )
                replayed_compose = DurableTransition.from_dict(
                    compose_transition.to_dict()
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise PostExecutionWorkflowError(
                    "post_execution_result_narrative_closure_invalid"
                ) from exc
        else:
            if (
                type(narrative) is not NarrativeWorkflowResult
                or type(flow) is not PublicationFlowResult
                or type(compose_transition) is not DurableTransition
            ):
                raise PostExecutionWorkflowError(
                    "post_execution_result_narrative_closure_invalid"
                )
            replayed_narrative = narrative
            replayed_flow = flow
            replayed_compose = compose_transition
        expected_input = _narrative_transition_input(
            bundle=bundle,
            semantic=semantic,
            material_projection_ref=(
                replayed_narrative.material_projection.projection_ref
            ),
            material_projection_digest=(
                replayed_narrative.material_projection.content_digest
            ),
            visibility_policy=replayed_narrative.visibility_policy,
            answer_context=replayed_narrative.answer_context,
        )
        if (
            replayed_narrative != narrative
            or replayed_flow != flow
            or replayed_compose != compose_transition
            or compose_transition.node_name != "compose_claim_aware_narrative"
            or compose_transition.parent_transition_id
            != authority_transition.transition_id
            or compose_transition.run_attempt_id != bundle.run_attempt_id
            or compose_transition.intent_revision_id != bundle.intent_revision_id
            or compose_transition.decision_ledger_position
            != authority_transition.decision_ledger_position
            or compose_transition.input_digest != canonical_digest(expected_input)
            or not _valid_digest(compose_transition.output_digest)
            or compose_transition.status != "succeeded"
            or compose_transition.acceptance_state != "accepted"
            or compose_transition.next_transition != "deliver_publication"
        ):
            raise PostExecutionWorkflowError(
                "post_execution_result_narrative_closure_invalid"
            )

    normalized_customer_payload = (
        None if customer_payload is None else canonical_value(customer_payload)
    )
    expected_customer_payload_ref = (
        None
        if replayed_flow is None or replayed_narrative is None
        else _customer_payload_ref(flow=replayed_flow, narrative=replayed_narrative)
    )
    customer_ref = _optional_string(
        customer_payload_ref, "post_execution_result_customer_payload_ref_invalid"
    )
    delivery_attempt = _optional_string(
        delivery_attempt_ref, "post_execution_result_delivery_attempt_ref_invalid"
    )
    customer_publication = _optional_string(
        customer_publication_ref,
        "post_execution_result_customer_publication_ref_invalid",
    )
    if replayed_flow is None:
        if (
            customer_ref is not None
            or delivery_attempt is not None
            or delivery_status is not None
            or delivery_replayed is not None
            or customer_publication is not None
            or normalized_customer_payload is not None
            or (
                replayed_narrative is None
                and status not in {"authority_sealed", *POST_SEAL_FAILURE_STATUSES}
            )
            or replayed_narrative is not None
        ):
            raise PostExecutionWorkflowError(
                "post_execution_result_publication_closure_invalid"
            )
    else:
        if customer_ref != expected_customer_payload_ref:
            raise PostExecutionWorkflowError(
                "post_execution_result_customer_payload_ref_invalid"
            )
        if status == "narrative_ready":
            if any(
                item is not None
                for item in (
                    delivery_attempt,
                    delivery_status,
                    delivery_replayed,
                    customer_publication,
                    normalized_customer_payload,
                )
            ):
                raise PostExecutionWorkflowError(
                    "post_execution_result_delivery_closure_invalid"
                )
        elif status == "completed":
            if (
                delivery_status != "published"
                or delivery_attempt is None
                or type(delivery_replayed) is not bool
                or customer_publication is None
                or normalized_customer_payload is None
                or normalized_customer_payload
                != canonical_value(replayed_flow.customer_payload)
            ):
                raise PostExecutionWorkflowError(
                    "post_execution_result_delivery_closure_invalid"
                )
            replayed_narrative.visibility_policy.validate_customer_payload(
                normalized_customer_payload
            )
        elif status in {
            "delivery_retryable_failed",
            "delivery_permanently_failed",
        }:
            expected_delivery_status = (
                "retryable_failed"
                if status == "delivery_retryable_failed"
                else "permanently_failed"
            )
            if (
                delivery_status != expected_delivery_status
                or delivery_attempt is None
                or type(delivery_replayed) is not bool
                or customer_publication is not None
                or normalized_customer_payload is not None
            ):
                raise PostExecutionWorkflowError(
                    "post_execution_result_delivery_closure_invalid"
                )
        else:
            raise PostExecutionWorkflowError(
                "post_execution_result_publication_closure_invalid"
            )

    narrative_ref = (
        None
        if replayed_narrative is None
        else "narrative-workflow-result:sha256:" + replayed_narrative.content_digest
    )
    record_fields = {
        "status": status,
        "run_attempt_id": bundle.run_attempt_id,
        "intent_revision_id": bundle.intent_revision_id,
        "claim_coverage_checkpoint_ref": checkpoint_ref,
        "claim_coverage_checkpoint_digest": claim_coverage_checkpoint_digest,
        "claim_coverage_transition_id": checkpoint_transition_id,
        "semantic_authority_result_ref": semantic.result_ref,
        "semantic_authority_result_digest": semantic.content_digest,
        "authority_bundle_ref": bundle.bundle_ref,
        "authority_bundle_digest": bundle.bundle_digest,
        "authority_transition_id": authority_transition.transition_id,
        "authority_persistence_status": authority_persistence_status,
        "post_seal_failure_terminal_ref": (
            None
            if replayed_failure_terminal is None
            else replayed_failure_terminal.terminal_ref
        ),
        "post_seal_failure_persistence_status": failure_persistence_status,
        "narrative_material_projection_ref": projection_ref,
        "narrative_material_projection_digest": projection_digest,
        "narrative_material_persistence_status": material_persistence_status,
        "semantic_authority_result": semantic,
        "authority_bundle": bundle,
        "authority_transition": authority_transition,
        "post_seal_failure_terminal": (
            None if replayed_failure_terminal is None else replayed_failure_terminal
        ),
        "narrative_workflow_ref": narrative_ref,
        "narrative_workflow_digest": (
            None if replayed_narrative is None else replayed_narrative.content_digest
        ),
        "compose_transition_id": (
            None if replayed_compose is None else replayed_compose.transition_id
        ),
        "narrative_persistence_status": narrative_persistence_status,
        "narrative_workflow": (
            None if replayed_narrative is None else replayed_narrative
        ),
        "publication_flow": (None if replayed_flow is None else replayed_flow),
        "compose_transition": (None if replayed_compose is None else replayed_compose),
        "publication_ref": (
            None if replayed_flow is None else replayed_flow.publication.publication_ref
        ),
        "outbox_ref": (
            None if replayed_flow is None else replayed_flow.outbox.outbox_ref
        ),
        "customer_payload_ref": customer_ref,
        "delivery_attempt_ref": delivery_attempt,
        "delivery_status": delivery_status,
        "delivery_replayed": delivery_replayed,
        "customer_publication_ref": customer_publication,
        "customer_payload": normalized_customer_payload,
    }
    digest = canonical_digest(
        _post_execution_dependency_manifest(
            status=status,
            run_attempt_id=bundle.run_attempt_id,
            intent_revision_id=bundle.intent_revision_id,
            claim_coverage_checkpoint_ref=checkpoint_ref,
            claim_coverage_checkpoint_digest=(claim_coverage_checkpoint_digest),
            claim_coverage_transition_id=checkpoint_transition_id,
            semantic=semantic,
            bundle=bundle,
            authority_transition=authority_transition,
            authority_persistence_status=authority_persistence_status,
            failure_terminal=replayed_failure_terminal,
            failure_persistence_status=failure_persistence_status,
            material_projection_ref=projection_ref,
            material_projection_digest=projection_digest,
            material_persistence_status=material_persistence_status,
            narrative=replayed_narrative,
            flow=replayed_flow,
            compose_transition=replayed_compose,
            narrative_persistence_status=narrative_persistence_status,
            customer_payload_ref=customer_ref,
            delivery_attempt_ref=delivery_attempt,
            delivery_status=delivery_status,
            delivery_replayed=delivery_replayed,
            customer_publication_ref=customer_publication,
            customer_payload=normalized_customer_payload,
        )
    )
    return PostExecutionWorkflowResult(
        result_ref="post-execution-workflow-result:sha256:" + digest,
        content_digest=digest,
        **record_fields,
    )


_POST_EXECUTION_SCALAR_FIELDS = tuple(
    field
    for field in PostExecutionWorkflowResult.__dataclass_fields__
    if field
    not in {
        "semantic_authority_result",
        "authority_bundle",
        "authority_transition",
        "post_seal_failure_terminal",
        "narrative_workflow",
        "publication_flow",
        "compose_transition",
        "customer_payload",
    }
)


def _post_execution_scalar_state(
    result: PostExecutionWorkflowResult,
) -> dict[str, Any]:
    return {
        **{field: getattr(result, field) for field in _POST_EXECUTION_SCALAR_FIELDS},
        "customer_payload_digest": (
            None
            if result.customer_payload is None
            else canonical_digest(result.customer_payload)
        ),
    }


def validate_typed_post_execution_workflow_result(
    result: PostExecutionWorkflowResult,
) -> PostExecutionWorkflowResult:
    if type(result) is not PostExecutionWorkflowResult:
        raise PostExecutionWorkflowError("post_execution_result_shape_invalid")
    rebuilt = _build_result(
        status=result.status,
        semantic=result.semantic_authority_result,
        bundle=result.authority_bundle,
        authority_transition=result.authority_transition,
        claim_coverage_checkpoint_ref=result.claim_coverage_checkpoint_ref,
        claim_coverage_checkpoint_digest=(result.claim_coverage_checkpoint_digest),
        claim_coverage_transition_id=result.claim_coverage_transition_id,
        authority_persistence_status=result.authority_persistence_status,
        failure_terminal=result.post_seal_failure_terminal,
        failure_persistence_status=result.post_seal_failure_persistence_status,
        material_projection_ref=result.narrative_material_projection_ref,
        material_projection_digest=result.narrative_material_projection_digest,
        material_persistence_status=result.narrative_material_persistence_status,
        narrative=result.narrative_workflow,
        flow=result.publication_flow,
        compose_transition=result.compose_transition,
        narrative_persistence_status=result.narrative_persistence_status,
        customer_payload_ref=result.customer_payload_ref,
        delivery_attempt_ref=result.delivery_attempt_ref,
        delivery_status=result.delivery_status,
        delivery_replayed=result.delivery_replayed,
        customer_publication_ref=result.customer_publication_ref,
        customer_payload=result.customer_payload,
    )
    if _post_execution_scalar_state(rebuilt) != _post_execution_scalar_state(result):
        raise PostExecutionWorkflowError("post_execution_result_integrity_invalid")
    return result


def validate_in_process_post_execution_workflow_result(
    result: PostExecutionWorkflowResult,
) -> PostExecutionWorkflowResult:
    """Validate the frozen workflow handoff without replaying sealed children.

    Serialized values still use ``from_dict`` or the full typed validator.
    This boundary accepts only the exact in-process type produced by
    ``_build_result``. Its nested authorities were replay-validated before
    persistence, so terminal bookkeeping verifies their immutable identities
    and the parent dependency digest without replaying the full claim,
    evidence, narrative, and publication graphs after delivery.
    """

    if type(result) is not PostExecutionWorkflowResult:
        raise PostExecutionWorkflowError("post_execution_result_shape_invalid")
    semantic = result.semantic_authority_result
    bundle = result.authority_bundle
    authority_transition = result.authority_transition
    failure_terminal = result.post_seal_failure_terminal
    narrative = result.narrative_workflow
    flow = result.publication_flow
    compose_transition = result.compose_transition
    if (
        type(semantic) is not SemanticAuthorityResult
        or type(bundle) is not AuthorityBundle
        or type(authority_transition) is not DurableTransition
        or (
            failure_terminal is not None
            and type(failure_terminal) is not PostSealFailureTerminal
        )
        or (
            narrative is not None
            and type(narrative) is not NarrativeWorkflowResult
        )
        or (flow is not None and type(flow) is not PublicationFlowResult)
        or (
            compose_transition is not None
            and type(compose_transition) is not DurableTransition
        )
    ):
        raise PostExecutionWorkflowError("post_execution_result_shape_invalid")

    expected_narrative_ref = (
        None
        if narrative is None
        else "narrative-workflow-result:sha256:" + narrative.content_digest
    )
    if (
        result.status not in POST_EXECUTION_STATUSES
        or result.run_attempt_id != bundle.run_attempt_id
        or result.intent_revision_id != bundle.intent_revision_id
        or result.semantic_authority_result_ref != semantic.result_ref
        or result.semantic_authority_result_digest != semantic.content_digest
        or result.authority_bundle_ref != bundle.bundle_ref
        or result.authority_bundle_digest != bundle.bundle_digest
        or result.authority_transition_id != authority_transition.transition_id
        or result.post_seal_failure_terminal_ref
        != (
            None if failure_terminal is None else failure_terminal.terminal_ref
        )
        or result.narrative_workflow_ref != expected_narrative_ref
        or result.narrative_workflow_digest
        != (None if narrative is None else narrative.content_digest)
        or result.compose_transition_id
        != (
            None
            if compose_transition is None
            else compose_transition.transition_id
        )
        or result.publication_ref
        != (None if flow is None else flow.publication.publication_ref)
        or result.outbox_ref != (None if flow is None else flow.outbox.outbox_ref)
        or (narrative is None) != (flow is None)
        or (narrative is None) != (compose_transition is None)
    ):
        raise PostExecutionWorkflowError("post_execution_result_integrity_invalid")

    expected_digest = canonical_digest(
        _post_execution_dependency_manifest(
            status=result.status,
            run_attempt_id=result.run_attempt_id,
            intent_revision_id=result.intent_revision_id,
            claim_coverage_checkpoint_ref=result.claim_coverage_checkpoint_ref,
            claim_coverage_checkpoint_digest=(
                result.claim_coverage_checkpoint_digest
            ),
            claim_coverage_transition_id=result.claim_coverage_transition_id,
            semantic=semantic,
            bundle=bundle,
            authority_transition=authority_transition,
            authority_persistence_status=result.authority_persistence_status,
            failure_terminal=failure_terminal,
            failure_persistence_status=(
                result.post_seal_failure_persistence_status
            ),
            material_projection_ref=result.narrative_material_projection_ref,
            material_projection_digest=(
                result.narrative_material_projection_digest
            ),
            material_persistence_status=(
                result.narrative_material_persistence_status
            ),
            narrative=narrative,
            flow=flow,
            compose_transition=compose_transition,
            narrative_persistence_status=result.narrative_persistence_status,
            customer_payload_ref=result.customer_payload_ref,
            delivery_attempt_ref=result.delivery_attempt_ref,
            delivery_status=result.delivery_status,
            delivery_replayed=result.delivery_replayed,
            customer_publication_ref=result.customer_publication_ref,
            customer_payload=result.customer_payload,
        )
    )
    if (
        result.content_digest != expected_digest
        or result.result_ref
        != "post-execution-workflow-result:sha256:" + expected_digest
    ):
        raise PostExecutionWorkflowError("post_execution_result_integrity_invalid")
    return result


def _validated_prior_result(
    prior_result: PostExecutionWorkflowResult,
    *,
    execution: AuthoritativeExecutionResult,
    claim_coverage_checkpoint: ClaimCoverageCheckpoint,
    stop_after: str | None,
) -> PostExecutionWorkflowResult:
    try:
        prior = validate_typed_post_execution_workflow_result(prior_result)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PostExecutionWorkflowError("post_execution_prior_result_invalid") from exc
    expected_status = (
        "authority_sealed" if stop_after == "phase05" else "narrative_ready"
    )
    authority_execution = (
        prior.semantic_authority_result.authority_bundle_inputs.execution_result
    )
    if (
        stop_after == "phase04"
        or prior.status != expected_status
        or prior.run_attempt_id != execution.run_attempt_id
        or prior.intent_revision_id != execution.intent_revision_id
        or authority_execution.authoritative_execution_result_ref
        != execution.authoritative_execution_result_ref
        or authority_execution.content_digest != execution.content_digest
        or authority_execution.plan_revision_id != execution.plan_revision_id
        or prior.claim_coverage_checkpoint_ref
        != claim_coverage_checkpoint.checkpoint_ref
        or prior.claim_coverage_checkpoint_digest
        != claim_coverage_checkpoint.content_digest
        or prior.claim_coverage_transition_id != claim_coverage_checkpoint.transition_id
        or prior.authority_transition.parent_transition_id
        != claim_coverage_checkpoint.transition_id
    ):
        raise PostExecutionWorkflowError("post_execution_prior_stage_invalid")
    return prior


def _deliver_publication_result(
    *,
    connection: Any,
    owner_ref: str,
    semantic: SemanticAuthorityResult,
    bundle: AuthorityBundle,
    authority_transition: DurableTransition,
    claim_coverage_checkpoint_ref: str,
    claim_coverage_checkpoint_digest: str,
    claim_coverage_transition_id: str,
    authority_persistence_status: str,
    material_projection: NarrativeMaterialProjection,
    material_persistence_status: str,
    narrative: NarrativeWorkflowResult,
    flow: PublicationFlowResult,
    compose_transition: DurableTransition,
    narrative_persistence_status: str,
    customer_payload_ref: str,
    transport: Callable[[DeliveryMessage], DeliveryTransportResult] | None,
) -> PostExecutionWorkflowResult:
    if not callable(transport):
        raise PostExecutionWorkflowError("post_execution_delivery_transport_required")
    delivery = deliver_persisted_outbox(
        connection,
        outbox_ref=flow.outbox.outbox_ref,
        transport=transport,
    )
    if type(delivery) is not DeliveryPersistenceResult:
        raise PostExecutionWorkflowError("post_execution_delivery_result_invalid")
    if delivery.status == "published":
        safe_payload = _persisted_customer_payload(
            connection,
            owner_ref=owner_ref,
            bundle=bundle,
            narrative=narrative,
            flow=flow,
            customer_payload_ref=customer_payload_ref,
            delivery=delivery,
        )
        status = "completed"
    elif delivery.status == "retryable_failed":
        safe_payload = None
        status = "delivery_retryable_failed"
    elif delivery.status == "permanently_failed":
        safe_payload = None
        status = "delivery_permanently_failed"
    else:
        raise PostExecutionWorkflowError("post_execution_delivery_result_invalid")
    return _build_result(
        status=status,
        semantic=semantic,
        bundle=bundle,
        authority_transition=authority_transition,
        claim_coverage_checkpoint_ref=claim_coverage_checkpoint_ref,
        claim_coverage_checkpoint_digest=claim_coverage_checkpoint_digest,
        claim_coverage_transition_id=claim_coverage_transition_id,
        authority_persistence_status=authority_persistence_status,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        material_persistence_status=material_persistence_status,
        narrative=narrative,
        flow=flow,
        compose_transition=compose_transition,
        narrative_persistence_status=narrative_persistence_status,
        customer_payload_ref=customer_payload_ref,
        delivery_attempt_ref=delivery.attempt_ref,
        delivery_status=delivery.status,
        delivery_replayed=delivery.replayed,
        customer_publication_ref=delivery.customer_publication_ref,
        customer_payload=safe_payload,
        replay_nested_authorities=False,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _post_seal_failure_result(
    *,
    authority_store: AcceptedTransitionStore,
    owner_ref: str,
    thread_ref: str,
    semantic: SemanticAuthorityResult,
    bundle: AuthorityBundle,
    authority_transition: DurableTransition,
    claim_coverage_checkpoint_ref: str,
    claim_coverage_checkpoint_digest: str,
    claim_coverage_transition_id: str,
    authority_persistence_status: str,
    material_projection: NarrativeMaterialProjection,
    material_persistence_status: str,
    status: str,
    kind: str,
    retryability: str,
    technical_detail_ref: str,
    affected_refs: tuple[str, ...],
    supersedes_terminal_ref: str | None,
) -> PostExecutionWorkflowResult:
    failure = FailureRecord.create(
        layer=("narrative" if status == "narrative_failed" else "persistence"),
        kind=kind,
        scope="run",
        affected_refs=(
            bundle.bundle_ref,
            authority_transition.transition_id,
            material_projection.projection_ref,
            *affected_refs,
        ),
        integrity_level="local",
        retryability=retryability,
        user_actionable=False,
        business_boundary=(
            "The accepted analysis remains authoritative; customer publication "
            "is unavailable for this run attempt."
        ),
        technical_detail_ref=technical_detail_ref,
    )
    persisted = authority_store.record_post_seal_failure(
        owner_ref=owner_ref,
        thread_ref=thread_ref,
        authority_bundle=bundle,
        authority_transition=authority_transition,
        status=status,
        failure_record=failure,
        supersedes_terminal_ref=supersedes_terminal_ref,
    )
    if (
        type(persisted) is not PostSealFailurePersistenceResult
        or persisted.status not in {"inserted", "replayed"}
        or persisted.terminal.status != status
        or persisted.terminal.failure_record != failure
        or persisted.terminal.authority_bundle_ref != bundle.bundle_ref
        or persisted.terminal.authority_bundle_digest != bundle.bundle_digest
        or persisted.terminal.authority_transition_id
        != authority_transition.transition_id
    ):
        raise PostExecutionWorkflowError("post_execution_failure_persistence_invalid")
    return _build_result(
        status=status,
        semantic=semantic,
        bundle=bundle,
        authority_transition=authority_transition,
        claim_coverage_checkpoint_ref=claim_coverage_checkpoint_ref,
        claim_coverage_checkpoint_digest=claim_coverage_checkpoint_digest,
        claim_coverage_transition_id=claim_coverage_transition_id,
        authority_persistence_status=authority_persistence_status,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        material_persistence_status=material_persistence_status,
        narrative=None,
        flow=None,
        compose_transition=None,
        narrative_persistence_status="not_started",
        customer_payload_ref=None,
        delivery_attempt_ref=None,
        delivery_status=None,
        delivery_replayed=None,
        customer_publication_ref=None,
        customer_payload=None,
        failure_terminal=persisted.terminal,
        failure_persistence_status=persisted.status,
    )


def _load_post_seal_failure_terminal(
    *,
    authority_store: AcceptedTransitionStore,
    bundle: AuthorityBundle,
    authority_transition: DurableTransition,
) -> PostSealFailureTerminal | None:
    terminal = authority_store.load_post_seal_failure_terminal(
        authority_bundle=bundle,
        authority_transition=authority_transition,
    )
    if terminal is None:
        return None
    if (
        type(terminal) is not PostSealFailureTerminal
        or terminal.run_attempt_id != bundle.run_attempt_id
        or terminal.authority_bundle_ref != bundle.bundle_ref
        or terminal.authority_bundle_digest != bundle.bundle_digest
        or terminal.authority_transition_id != authority_transition.transition_id
    ):
        raise PostExecutionWorkflowError(
            "post_execution_failure_terminal_replay_invalid"
        )
    return terminal


def _replay_post_seal_failure_result(
    *,
    terminal: PostSealFailureTerminal,
    semantic: SemanticAuthorityResult,
    bundle: AuthorityBundle,
    authority_transition: DurableTransition,
    claim_coverage_checkpoint_ref: str,
    claim_coverage_checkpoint_digest: str,
    claim_coverage_transition_id: str,
    authority_persistence_status: str,
    material_projection: NarrativeMaterialProjection,
    material_persistence_status: str,
) -> PostExecutionWorkflowResult:
    return _build_result(
        status=terminal.status,
        semantic=semantic,
        bundle=bundle,
        authority_transition=authority_transition,
        claim_coverage_checkpoint_ref=claim_coverage_checkpoint_ref,
        claim_coverage_checkpoint_digest=claim_coverage_checkpoint_digest,
        claim_coverage_transition_id=claim_coverage_transition_id,
        authority_persistence_status=authority_persistence_status,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        material_persistence_status=material_persistence_status,
        narrative=None,
        flow=None,
        compose_transition=None,
        narrative_persistence_status="not_started",
        customer_payload_ref=None,
        delivery_attempt_ref=None,
        delivery_status=None,
        delivery_replayed=None,
        customer_publication_ref=None,
        customer_payload=None,
        failure_terminal=terminal,
        failure_persistence_status="replayed",
    )


def run_post_execution_workflow(
    execution_result: AuthoritativeExecutionResult,
    *,
    claim_coverage_checkpoint: ClaimCoverageCheckpoint,
    intent_revision: IntentRevision,
    owner_ref: str,
    thread_ref: str,
    authority_store: AcceptedTransitionStore,
    connection: Any,
    llm_client: PostExecutionLLM,
    visibility_policy: PublicationFieldVisibilityPolicy,
    sensitive_output_inspector: SensitiveOutputInspector,
    locale: str,
    destination_ref: str,
    channel: str,
    transport: Callable[[DeliveryMessage], DeliveryTransportResult] | None,
    customer_term_labels: Mapping[str, str] | None = None,
    stop_after: str | None = None,
    prior_result: PostExecutionWorkflowResult | None = None,
    factor_coverage_plan: FactorCoveragePlan | None = None,
    factor_coverage_result: FactorCoverageResult | None = None,
    controlled_investigation_enabled: bool = False,
) -> PostExecutionWorkflowResult:
    execution = _validated_execution(execution_result)
    coverage_checkpoint = _validated_claim_coverage_checkpoint(
        claim_coverage_checkpoint,
        execution=execution,
    )
    intent = _validated_intent(intent_revision, execution=execution)
    coverage_plan: FactorCoveragePlan | None = None
    coverage_result: FactorCoverageResult | None = None
    if factor_coverage_plan is not None or factor_coverage_result is not None:
        try:
            if type(factor_coverage_plan) is not FactorCoveragePlan or type(
                factor_coverage_result
            ) is not FactorCoverageResult:
                raise TypeError("factor_coverage")
            coverage_plan = FactorCoveragePlan.from_dict(
                factor_coverage_plan.to_dict()
            )
            coverage_result = FactorCoverageResult.from_dict(
                factor_coverage_result.to_dict(),
                plan=coverage_plan,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise PostExecutionWorkflowError(
                "post_execution_factor_coverage_invalid"
            ) from exc
        if (
            coverage_plan.run_attempt_id != execution.run_attempt_id
            or coverage_plan.intent_revision_id != execution.intent_revision_id
            or coverage_plan.plan_revision_id != execution.plan_revision_id
            or coverage_result.execution_result_ref
            != execution.authoritative_execution_result_ref
        ):
            raise PostExecutionWorkflowError(
                "post_execution_factor_coverage_invalid"
            )
    owner = _required_string(owner_ref, "post_execution_owner_ref_invalid")
    thread = _required_string(thread_ref, "post_execution_thread_ref_invalid")
    if not callable(getattr(authority_store, "load_accepted_transition", None)):
        raise PostExecutionWorkflowError("post_execution_authority_store_invalid")
    attempt_journal = getattr(authority_store, "attempt_journal", None)
    if not isinstance(attempt_journal, DurableCallJournal):
        raise PostExecutionWorkflowError("post_execution_attempt_journal_invalid")
    if not callable(
        getattr(authority_store, "load_post_seal_failure_terminal", None)
    ) or not callable(getattr(authority_store, "record_post_seal_failure", None)):
        raise PostExecutionWorkflowError("post_execution_authority_store_invalid")
    if connection is None or not callable(getattr(connection, "execute", None)):
        raise PostExecutionWorkflowError("post_execution_connection_invalid")
    if not callable(getattr(llm_client, "invoke_json", None)):
        raise PostExecutionWorkflowError("post_execution_llm_client_invalid")
    if type(controlled_investigation_enabled) is not bool:
        raise PostExecutionWorkflowError(
            "post_execution_controlled_investigation_flag_invalid"
        )
    if not callable(sensitive_output_inspector):
        raise PostExecutionWorkflowError(
            "post_execution_sensitive_output_inspector_invalid"
        )
    if stop_after is not None and stop_after not in STOP_BOUNDARIES:
        raise PostExecutionWorkflowError("post_execution_stop_boundary_invalid")
    policy = _validated_policy(visibility_policy)
    namespace = ClaimAuthorityNamespace.create(
        run_attempt_id=execution.run_attempt_id,
        intent_revision_id=execution.intent_revision_id,
        plan_revision_id=execution.plan_revision_id,
    )
    prior = (
        None
        if prior_result is None
        else _validated_prior_result(
            prior_result,
            execution=execution,
            claim_coverage_checkpoint=coverage_checkpoint,
            stop_after=stop_after,
        )
    )
    accepted_authority = (
        None
        if prior is not None
        else _load_accepted_authority(
            authority_store=authority_store,
            attempt_journal=attempt_journal,
            execution=execution,
            namespace=namespace,
            claim_coverage_checkpoint=coverage_checkpoint,
        )
    )
    if prior is not None:
        semantic = prior.semantic_authority_result
        bundle = prior.authority_bundle
        authority_transition = prior.authority_transition
        authority_persistence_status = prior.authority_persistence_status
    elif accepted_authority is None:
        semantic_llm = DurableProviderClient(
            llm_client,
            journal=attempt_journal,
            run_attempt_id=execution.run_attempt_id,
            intent_revision_id=execution.intent_revision_id,
            plan_revision_id=execution.plan_revision_id,
            call_kind="semantic_provider",
            task_id=None,
            stage_name="settle_claim_authority",
        )
        semantic = run_semantic_authority_workflow(
            execution,
            authority_namespace=namespace,
            claim_coverage_checkpoint=coverage_checkpoint,
            llm_client=semantic_llm,
        )
        bundle = semantic.authority_bundle_inputs.seal(
            bundle_revision=1,
            supersedes_bundle_ref=None,
            sealed_at=_utc_now(),
        )
        transition_input, transition_output = semantic_authority_transition_payloads(
            semantic,
            bundle,
            claim_coverage_checkpoint_ref=coverage_checkpoint.checkpoint_ref,
            claim_coverage_checkpoint_digest=coverage_checkpoint.content_digest,
        )
        now = _utc_now()
        authority_transition = DurableTransition.create(
            node_name="settle_claim_authority",
            parent_transition_id=coverage_checkpoint.transition_id,
            run_attempt_id=execution.run_attempt_id,
            intent_revision_id=execution.intent_revision_id,
            decision_ledger_position=(
                coverage_checkpoint.transition.decision_ledger_position
            ),
            input_digest=canonical_digest(transition_input),
            output_digest=canonical_digest(transition_output),
            execution_attempt=1,
            provider_ref=_SEMANTIC_PROVIDER_REF,
            model_ref=_SEMANTIC_MODEL_REF,
            status="succeeded",
            acceptance_state="accepted",
            next_transition="compose_claim_aware_narrative",
            started_at=now,
            finished_at=now,
        )
        seal_result = seal_authority_bundle(
            connection,
            owner_ref=owner,
            thread_ref=thread,
            authority_inputs=semantic.authority_bundle_inputs,
            authority_bundle=bundle,
            provider_responses=semantic.provider_responses,
            semantic_authority_result=semantic,
            claim_coverage_checkpoint=coverage_checkpoint,
            settlement_transition=authority_transition,
            attempt_journal=attempt_journal,
            accepted_attempt_refs=semantic_llm.accepted_attempt_refs,
        )
        if (
            type(seal_result) is not AuthoritySealResult
            or seal_result.bundle_ref != bundle.bundle_ref
            or seal_result.bundle_digest != bundle.bundle_digest
            or seal_result.status not in {"inserted", "replayed"}
        ):
            raise PostExecutionWorkflowError(
                "post_execution_authority_persistence_invalid"
            )
        authority_persistence_status = seal_result.status
    else:
        semantic, bundle, authority_transition = accepted_authority
        authority_persistence_status = "replayed"

    if (
        semantic.projection.claim_coverage_evaluation_ref
        != coverage_checkpoint.evaluation_ref
        or semantic.projection.claim_coverage_evaluation_digest
        != coverage_checkpoint.evaluation.content_digest
    ):
        raise PostExecutionWorkflowError(
            "post_execution_claim_coverage_semantic_closure_invalid"
        )

    if stop_after == "phase04":
        return _build_result(
            status="authority_sealed",
            semantic=semantic,
            bundle=bundle,
            authority_transition=authority_transition,
            claim_coverage_checkpoint_ref=coverage_checkpoint.checkpoint_ref,
            claim_coverage_checkpoint_digest=coverage_checkpoint.content_digest,
            claim_coverage_transition_id=coverage_checkpoint.transition_id,
            authority_persistence_status=authority_persistence_status,
            material_projection_ref=None,
            material_projection_digest=None,
            material_persistence_status="not_started",
            narrative=None,
            flow=None,
            compose_transition=None,
            narrative_persistence_status="not_started",
            customer_payload_ref=None,
            delivery_attempt_ref=None,
            delivery_status=None,
            delivery_replayed=None,
            customer_publication_ref=None,
            customer_payload=None,
            replay_nested_authorities=False,
        )

    if prior is not None and prior.status == "narrative_ready":
        narrative = prior.narrative_workflow
        flow = prior.publication_flow
        compose_transition = prior.compose_transition
        customer_payload_ref = prior.customer_payload_ref
        if (
            narrative is None
            or flow is None
            or compose_transition is None
            or customer_payload_ref is None
            or prior.narrative_material_projection_ref
            != narrative.material_projection.projection_ref
            or prior.narrative_material_projection_digest
            != narrative.material_projection.content_digest
            or prior.narrative_material_persistence_status
            not in {"inserted", "replayed"}
            or narrative.visibility_policy.policy_ref != policy.policy_ref
            or narrative.visibility_policy.content_digest != policy.content_digest
            or narrative.answer_context.locale
            != _required_string(locale, "post_execution_locale_invalid")
            or flow.outbox.destination_ref
            != _required_string(
                destination_ref, "post_execution_destination_ref_invalid"
            )
            or flow.outbox.channel
            != _required_string(channel, "post_execution_channel_invalid")
        ):
            raise PostExecutionWorkflowError(
                "post_execution_prior_delivery_closure_invalid"
            )
        return _deliver_publication_result(
            connection=connection,
            owner_ref=owner,
            semantic=semantic,
            bundle=bundle,
            authority_transition=authority_transition,
            claim_coverage_checkpoint_ref=coverage_checkpoint.checkpoint_ref,
            claim_coverage_checkpoint_digest=coverage_checkpoint.content_digest,
            claim_coverage_transition_id=coverage_checkpoint.transition_id,
            authority_persistence_status=authority_persistence_status,
            material_projection=narrative.material_projection,
            material_persistence_status=(prior.narrative_material_persistence_status),
            narrative=narrative,
            flow=flow,
            compose_transition=compose_transition,
            narrative_persistence_status=prior.narrative_persistence_status,
            customer_payload_ref=customer_payload_ref,
            transport=transport,
        )

    evidence_entries = _accepted_evidence_entries(execution, semantic)
    public_facts: PublicFactMaterialization = materialize_public_facts(
        authority_bundle=bundle,
        authority_namespace=namespace,
        claims=semantic.settlement.accepted_claims,
        claim_keys=semantic.settlement.accepted_claim_keys,
        support_edges=semantic.settlement.accepted_support_edges,
        evidence_entries=evidence_entries,
        visibility_policy=policy,
    )
    if public_facts.materialization_state not in {"ready", "boundary_only"} or (
        public_facts.claims_without_public_facts
    ):
        raise PostExecutionWorkflowError(
            "post_execution_public_fact_materialization_incomplete"
        )
    limitation_contexts = build_public_limitation_contexts(
        execution,
        bundle,
        semantic.settlement,
        semantic.recommendations,
    )
    reviewed_materialization = build_reviewed_public_materialization(
        authority_bundle=bundle,
        claim_settlement=semantic.settlement,
        public_fact_materialization=public_facts,
        public_limitation_context_by_ref=limitation_contexts,
    )
    coverage_business_context: tuple[str, ...] = ()
    if coverage_plan is not None and coverage_result is not None:
        final_synthesis = synthesize_factor_coverage(
            plan=coverage_plan,
            coverage_result=coverage_result,
            claim_settlement=semantic.settlement,
        )
        coverage_business_context = (
            narrative_factor_coverage_context(
                plan=coverage_plan,
                coverage_result=coverage_result,
                synthesis=final_synthesis,
            ),
        )
    palette, material_projection = prepare_narrative_material_projection(
        authority_bundle=bundle,
        claim_settlement=semantic.settlement,
        evidence_entries=evidence_entries,
        recommendations=semantic.recommendations,
        public_materialization=reviewed_materialization,
        visibility_policy=policy,
    )
    prior_failure = _load_post_seal_failure_terminal(
        authority_store=authority_store,
        bundle=bundle,
        authority_transition=authority_transition,
    )
    try:
        material_persistence: NarrativeMaterialPersistenceResult = (
            persist_narrative_material_projection(
                connection,
                owner_ref=owner,
                thread_ref=thread,
                authority_bundle=bundle,
                claim_settlement=semantic.settlement,
                visibility_policy=policy,
                palette=palette,
                projection=material_projection,
                evidence_entries=evidence_entries,
            )
        )
    except NarrativeMaterialPersistenceOperationalError as exc:
        return _post_seal_failure_result(
            authority_store=authority_store,
            owner_ref=owner,
            thread_ref=thread,
            semantic=semantic,
            bundle=bundle,
            authority_transition=authority_transition,
            claim_coverage_checkpoint_ref=coverage_checkpoint.checkpoint_ref,
            claim_coverage_checkpoint_digest=coverage_checkpoint.content_digest,
            claim_coverage_transition_id=coverage_checkpoint.transition_id,
            authority_persistence_status=authority_persistence_status,
            material_projection=material_projection,
            material_persistence_status="not_started",
            status="publication_failed",
            kind="narrative_material_persistence_unavailable",
            retryability="retryable",
            technical_detail_ref=exc.technical_detail_ref,
            affected_refs=(),
            supersedes_terminal_ref=(
                None if prior_failure is None else prior_failure.terminal_ref
            ),
        )
    if (
        type(material_persistence) is not NarrativeMaterialPersistenceResult
        or material_persistence.projection_ref != material_projection.projection_ref
        or material_persistence.projection_digest != material_projection.content_digest
        or material_persistence.palette_ref != palette.palette_ref
        or material_persistence.run_attempt_id != bundle.run_attempt_id
        or material_persistence.status not in {"inserted", "replayed"}
    ):
        raise PostExecutionWorkflowError("post_execution_material_persistence_invalid")
    material_persistence_status = material_persistence.status
    controlled_business_context: tuple[str, ...] = ()
    if controlled_investigation_enabled and coverage_plan is not None:
        try:
            controlled_result = run_controlled_investigation_workflow(
                owner_ref=owner,
                thread_ref=thread,
                run_attempt_id=bundle.run_attempt_id,
                intent_revision_id=bundle.intent_revision_id,
                plan_revision_id=bundle.plan_revision_id,
                authority_context_ref=execution.authority_context_ref,
                authority_bundle_ref=bundle.bundle_ref,
                parent_transition_id=authority_transition.attempt_id,
                material_projection=material_projection,
                factor_coverage_plan=coverage_plan,
                llm_client=llm_client,
                attempt_journal=attempt_journal,
                connection=connection,
                worker_id="controlled-investigation-worker:" + bundle.run_attempt_id,
            )
            controlled_business_context = (
                controlled_result.narrative_context_record(),
            )
        except (LLMOutputError, LLMProviderError, LLMTimeoutError):
            controlled_business_context = ()
    answer_context = build_narrative_answer_context(
        authority_bundle=bundle,
        authority_inputs=semantic.authority_bundle_inputs,
        intent_revision=intent,
        recommendations=semantic.recommendations,
        locale=_required_string(locale, "post_execution_locale_invalid"),
        customer_term_labels=customer_term_labels,
        additional_business_context=(
            *coverage_business_context,
            *controlled_business_context,
        ),
    )
    destination = _required_string(
        destination_ref, "post_execution_destination_ref_invalid"
    )
    delivery_channel = _required_string(channel, "post_execution_channel_invalid")
    narrative_input = _narrative_transition_input(
        bundle=bundle,
        semantic=semantic,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        visibility_policy=policy,
        answer_context=answer_context,
    )
    accepted_narrative = (
        None
        if prior is not None
        else _load_accepted_narrative(
            authority_store=authority_store,
            attempt_journal=attempt_journal,
            bundle=bundle,
            semantic=semantic,
            evidence_entries=evidence_entries,
            parent_transition=authority_transition,
            transition_input=narrative_input,
            destination_ref=destination,
            channel=delivery_channel,
        )
    )
    if accepted_narrative is None:
        if (
            prior_failure is not None
            and prior_failure.failure_record.retryability != "retryable"
        ):
            return _replay_post_seal_failure_result(
                terminal=prior_failure,
                semantic=semantic,
                bundle=bundle,
                authority_transition=authority_transition,
                claim_coverage_checkpoint_ref=(coverage_checkpoint.checkpoint_ref),
                claim_coverage_checkpoint_digest=(coverage_checkpoint.content_digest),
                claim_coverage_transition_id=(coverage_checkpoint.transition_id),
                authority_persistence_status=authority_persistence_status,
                material_projection=material_projection,
                material_persistence_status=material_persistence_status,
            )
        try:
            narrative_llm = DurableProviderClient(
                llm_client,
                journal=attempt_journal,
                run_attempt_id=bundle.run_attempt_id,
                intent_revision_id=bundle.intent_revision_id,
                plan_revision_id=bundle.plan_revision_id,
                call_kind="narrative_provider",
                task_id=None,
                stage_name="compose_claim_aware_narrative",
            )
            narrative = run_narrative_workflow(
                authority_bundle=bundle,
                claim_settlement=semantic.settlement,
                evidence_entries=evidence_entries,
                recommendations=semantic.recommendations,
                public_materialization=reviewed_materialization,
                visibility_policy=policy,
                material_projection=material_projection,
                answer_context=answer_context,
                llm_client=narrative_llm,
                sensitive_output_inspector=sensitive_output_inspector,
            )
        except NarrativeProviderCallError as exc:
            return _post_seal_failure_result(
                authority_store=authority_store,
                owner_ref=owner,
                thread_ref=thread,
                semantic=semantic,
                bundle=bundle,
                authority_transition=authority_transition,
                claim_coverage_checkpoint_ref=(coverage_checkpoint.checkpoint_ref),
                claim_coverage_checkpoint_digest=(coverage_checkpoint.content_digest),
                claim_coverage_transition_id=(coverage_checkpoint.transition_id),
                authority_persistence_status=authority_persistence_status,
                material_projection=material_projection,
                material_persistence_status=material_persistence_status,
                status="narrative_failed",
                kind=exc.kind,
                retryability=exc.retryability,
                technical_detail_ref=exc.technical_detail_ref,
                affected_refs=(exc.call_input_ref,),
                supersedes_terminal_ref=(
                    None if prior_failure is None else prior_failure.terminal_ref
                ),
            )
        flow = PublicationFlowResult.create(
            authority_inputs=semantic.authority_bundle_inputs,
            authority_bundle=bundle,
            claim_settlement=semantic.settlement,
            recommendations=semantic.recommendations,
            narrative_workflow=narrative,
            supersedes_publication=None,
            destination_ref=destination,
            channel=delivery_channel,
            published_at=_utc_now(),
            customer_term_labels=customer_term_labels,
        )
        transition_input, transition_output = narrative_publication_transition_payloads(
            authority_inputs=semantic.authority_bundle_inputs,
            authority_bundle=bundle,
            claim_settlement=semantic.settlement,
            recommendations=semantic.recommendations,
            narrative_workflow=narrative,
            publication_flow=flow,
            supersedes_publication=None,
        )
        now = _utc_now()
        compose_transition = DurableTransition.create(
            node_name="compose_claim_aware_narrative",
            parent_transition_id=authority_transition.transition_id,
            run_attempt_id=bundle.run_attempt_id,
            intent_revision_id=bundle.intent_revision_id,
            decision_ledger_position=authority_transition.decision_ledger_position,
            input_digest=canonical_digest(transition_input),
            output_digest=canonical_digest(transition_output),
            execution_attempt=1,
            provider_ref=_NARRATIVE_PROVIDER_REF,
            model_ref=_NARRATIVE_MODEL_REF,
            status="succeeded",
            acceptance_state="accepted",
            next_transition="deliver_publication",
            started_at=now,
            finished_at=now,
        )
        persistence_result: PublicationPersistenceResult
        try:
            persistence_result = persist_publication(
                connection,
                owner_ref=owner,
                thread_ref=thread,
                authority_inputs=semantic.authority_bundle_inputs,
                authority_bundle=bundle,
                claim_settlement=semantic.settlement,
                recommendations=semantic.recommendations,
                narrative_workflow=narrative,
                publication_flow=flow,
                supersedes_publication=None,
                compose_transition=compose_transition,
                attempt_journal=attempt_journal,
                accepted_attempt_refs=narrative_llm.accepted_attempt_refs,
            )
        except PublicationPersistenceOperationalError as exc:
            return _post_seal_failure_result(
                authority_store=authority_store,
                owner_ref=owner,
                thread_ref=thread,
                semantic=semantic,
                bundle=bundle,
                authority_transition=authority_transition,
                claim_coverage_checkpoint_ref=(coverage_checkpoint.checkpoint_ref),
                claim_coverage_checkpoint_digest=(coverage_checkpoint.content_digest),
                claim_coverage_transition_id=(coverage_checkpoint.transition_id),
                authority_persistence_status=authority_persistence_status,
                material_projection=material_projection,
                material_persistence_status=material_persistence_status,
                status="publication_failed",
                kind="publication_persistence_unavailable",
                retryability="retryable",
                technical_detail_ref=exc.technical_detail_ref,
                affected_refs=(
                    "narrative-workflow-result:sha256:" + narrative.content_digest,
                ),
                supersedes_terminal_ref=(
                    None if prior_failure is None else prior_failure.terminal_ref
                ),
            )
        if (
            type(persistence_result) is not PublicationPersistenceResult
            or persistence_result.transition_id != compose_transition.transition_id
            or persistence_result.publication_ref != flow.publication.publication_ref
            or persistence_result.outbox_ref != flow.outbox.outbox_ref
            or persistence_result.status not in {"inserted", "replayed"}
        ):
            raise PostExecutionWorkflowError(
                "post_execution_narrative_persistence_invalid"
            )
        customer_payload_ref = persistence_result.customer_payload_ref
        narrative_persistence_status = persistence_result.status
    else:
        narrative, flow, compose_transition = accepted_narrative
        narrative_persistence_status = "replayed"
        customer_payload_ref = _customer_payload_ref(
            flow=flow,
            narrative=narrative,
        )
    expected_customer_payload_ref = _customer_payload_ref(
        flow=flow,
        narrative=narrative,
    )
    if customer_payload_ref != expected_customer_payload_ref:
        raise PostExecutionWorkflowError(
            "post_execution_customer_payload_persistence_invalid"
        )
    if stop_after == "phase05":
        return _build_result(
            status="narrative_ready",
            semantic=semantic,
            bundle=bundle,
            authority_transition=authority_transition,
            claim_coverage_checkpoint_ref=coverage_checkpoint.checkpoint_ref,
            claim_coverage_checkpoint_digest=coverage_checkpoint.content_digest,
            claim_coverage_transition_id=coverage_checkpoint.transition_id,
            authority_persistence_status=authority_persistence_status,
            material_projection_ref=material_projection.projection_ref,
            material_projection_digest=material_projection.content_digest,
            material_persistence_status=material_persistence_status,
            narrative=narrative,
            flow=flow,
            compose_transition=compose_transition,
            narrative_persistence_status=narrative_persistence_status,
            customer_payload_ref=customer_payload_ref,
            delivery_attempt_ref=None,
            delivery_status=None,
            delivery_replayed=None,
            customer_publication_ref=None,
            customer_payload=None,
            replay_nested_authorities=False,
        )
    return _deliver_publication_result(
        connection=connection,
        owner_ref=owner,
        semantic=semantic,
        bundle=bundle,
        authority_transition=authority_transition,
        claim_coverage_checkpoint_ref=coverage_checkpoint.checkpoint_ref,
        claim_coverage_checkpoint_digest=coverage_checkpoint.content_digest,
        claim_coverage_transition_id=coverage_checkpoint.transition_id,
        authority_persistence_status=authority_persistence_status,
        material_projection=material_projection,
        material_persistence_status=material_persistence_status,
        narrative=narrative,
        flow=flow,
        compose_transition=compose_transition,
        narrative_persistence_status=narrative_persistence_status,
        customer_payload_ref=customer_payload_ref,
        transport=transport,
    )


__all__ = (
    "AcceptedTransitionStore",
    "POST_EXECUTION_STATUSES",
    "PostExecutionLLM",
    "PostExecutionWorkflowError",
    "PostExecutionWorkflowResult",
    "STOP_BOUNDARIES",
    "run_post_execution_workflow",
    "validate_in_process_post_execution_workflow_result",
    "validate_typed_post_execution_workflow_result",
)
