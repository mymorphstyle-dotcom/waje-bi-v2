from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from bi_agent.runtime.capability_authority import (
    ExecutionSnapshot,
    ExplorationStopRecord,
)
from bi_agent.runtime.capability_scheduler import (
    capability_execution_transition_payloads,
)
from bi_agent.runtime.claim_authority import AuthorityBundle
from bi_agent.runtime.claim_coverage import (
    ClaimCoverageCheckpoint,
    ClaimCoverageContractError,
    claim_coverage_transition_payloads,
)
from bi_agent.runtime.claim_settlement import AuthorityBundleInputs
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.durable_call_journal import DurableCallJournal
from bi_agent.runtime.narrative_authority import RestrictedProviderResponse
from bi_agent.runtime.plan_authority import AuthorityContext, PlanRevision
from bi_agent.runtime.semantic_authority_workflow import SemanticAuthorityResult
from bi_agent.runtime.single_authority import (
    DecisionRecord,
    DurableTransition,
    IntentRevision,
    LifecycleState,
)


class AuthoritySealPersistenceError(ValueError):
    pass


@dataclass(frozen=True)
class AuthoritySealResult:
    bundle_ref: str
    bundle_digest: str
    status: str
    lifecycle_state_digest: str


_PREFLIGHT_SQL = """
/* authority_seal_preflight */
SELECT
  thread.owner_id AS owner_ref,
  run.thread_id AS thread_ref,
  intent.payload AS intent_payload,
  intent.content_digest AS intent_content_digest,
  context.payload AS authority_context_payload,
  context.content_digest AS authority_context_content_digest,
  plan.payload AS plan_payload,
  plan.content_digest AS plan_content_digest,
  snapshot.payload AS execution_snapshot_payload,
  snapshot.content_digest AS execution_snapshot_content_digest,
  stop.payload AS stop_payload,
  stop.content_digest AS stop_content_digest,
  to_jsonb(transition)
    - 'input_payload'
    - 'output_payload'
    - 'failure_ref'
    - 'created_at' AS transition_payload,
  transition.input_payload AS transition_input_payload,
  transition.output_payload AS transition_output_payload,
  lifecycle.payload AS lifecycle_payload,
  existing_bundle.payload AS existing_bundle_payload
FROM waje_runtime.analysis_runs run
JOIN waje_runtime.investigation_threads thread
  ON thread.thread_id = run.thread_id
JOIN waje_runtime.intent_revisions intent
  ON intent.intent_revision_id = %(intent_revision_id)s
 AND intent.run_attempt_id = run.run_id
 AND NOT EXISTS (
   SELECT 1
   FROM waje_runtime.intent_revision_supersessions supersession
   WHERE supersession.superseded_intent_revision_id = intent.intent_revision_id
 )
 AND EXISTS (
   SELECT 1
   FROM waje_runtime.workflow_transition_attempts accepted_intent
   WHERE accepted_intent.run_attempt_id = run.run_id
     AND accepted_intent.intent_revision_id = intent.intent_revision_id
     AND accepted_intent.status = 'succeeded'
     AND accepted_intent.acceptance_state = 'accepted'
     AND accepted_intent.output_payload
           #>> '{intent_revision,intent_revision_id}'
         = intent.intent_revision_id
 )
JOIN waje_runtime.authority_contexts context
  ON context.authority_context_ref = %(authority_context_ref)s
 AND context.run_attempt_id = run.run_id
JOIN waje_runtime.plan_revisions plan
  ON plan.plan_revision_id = %(plan_revision_id)s
 AND plan.run_attempt_id = run.run_id
 AND plan.intent_revision_id = intent.intent_revision_id
 AND plan.authority_context_ref = context.authority_context_ref
 AND NOT EXISTS (
   SELECT 1
   FROM waje_runtime.plan_revision_supersessions supersession
   WHERE supersession.superseded_plan_revision_id = plan.plan_revision_id
 )
 AND EXISTS (
   SELECT 1
   FROM waje_runtime.workflow_transition_attempts accepted_plan
   WHERE accepted_plan.run_attempt_id = run.run_id
     AND accepted_plan.intent_revision_id = intent.intent_revision_id
     AND accepted_plan.node_name = CASE
       WHEN plan.supersedes_plan_revision_id IS NULL
         THEN 'compile_authoritative_plan'
       ELSE 'compile_plan_patch'
     END
     AND accepted_plan.status = 'succeeded'
     AND accepted_plan.acceptance_state = 'accepted'
     AND accepted_plan.output_payload #>> '{plan_revision,plan_revision_id}'
         = plan.plan_revision_id
 )
JOIN waje_runtime.capability_execution_snapshots snapshot
  ON snapshot.execution_snapshot_ref = %(execution_snapshot_ref)s
 AND snapshot.run_attempt_id = run.run_id
 AND snapshot.plan_revision_id = plan.plan_revision_id
 AND snapshot.authority_context_ref = context.authority_context_ref
JOIN waje_runtime.exploration_stop_records stop
  ON stop.stop_ref = %(stop_ref)s
 AND stop.run_attempt_id = run.run_id
 AND stop.plan_revision_id = plan.plan_revision_id
JOIN waje_runtime.workflow_transition_attempts transition
  ON transition.transition_id = %(transition_id)s
 AND transition.run_attempt_id = run.run_id
 AND transition.intent_revision_id = intent.intent_revision_id
 AND transition.node_name = 'execute_capability_dag'
 AND transition.status = 'succeeded'
 AND transition.acceptance_state = 'accepted'
JOIN LATERAL (
  SELECT state.payload
  FROM waje_runtime.run_lifecycle_state_revisions state
  WHERE state.run_attempt_id = run.run_id
  ORDER BY state.state_revision DESC
  LIMIT 1
) lifecycle ON TRUE
LEFT JOIN waje_runtime.authority_bundles existing_bundle
  ON existing_bundle.run_attempt_id = run.run_id
 AND existing_bundle.seal_state = 'sealed'
WHERE run.run_id = %(run_attempt_id)s
  AND run.run_attempt_id = %(run_attempt_id)s
FOR UPDATE OF run
"""


_CLAIM_COVERAGE_CHECKPOINT_SQL = """
/* authority_seal_claim_coverage_checkpoint */
SELECT
  to_jsonb(checkpoint)
    - 'input_payload'
    - 'output_payload'
    - 'failure_ref'
    - 'created_at' AS checkpoint_transition_payload,
  checkpoint.input_payload AS checkpoint_input_payload,
  checkpoint.output_payload AS checkpoint_output_payload,
  CASE
    WHEN settlement.attempt_id IS NULL THEN NULL
    ELSE to_jsonb(settlement)
      - 'input_payload'
      - 'output_payload'
      - 'failure_ref'
      - 'created_at'
  END AS settlement_transition_payload,
  settlement.input_payload AS settlement_input_payload,
  settlement.output_payload AS settlement_output_payload,
  head.transition_id AS head_transition_id
FROM waje_runtime.workflow_transition_attempts checkpoint
LEFT JOIN waje_runtime.workflow_transition_attempts settlement
  ON settlement.transition_id = %(settlement_transition_id)s
 AND settlement.run_attempt_id = checkpoint.run_attempt_id
 AND settlement.intent_revision_id = checkpoint.intent_revision_id
 AND settlement.node_name = 'settle_claim_authority'
 AND settlement.parent_transition_id = checkpoint.transition_id
 AND settlement.status = 'succeeded'
 AND settlement.acceptance_state = 'accepted'
JOIN waje_runtime.workflow_transition_attempts head
  ON head.transition_id = %(head_transition_id)s
 AND head.run_attempt_id = checkpoint.run_attempt_id
 AND head.intent_revision_id = checkpoint.intent_revision_id
 AND head.status = 'succeeded'
 AND head.acceptance_state = 'accepted'
WHERE checkpoint.transition_id = %(checkpoint_transition_id)s
  AND checkpoint.run_attempt_id = %(run_attempt_id)s
  AND checkpoint.intent_revision_id = %(intent_revision_id)s
  AND checkpoint.node_name = 'evaluate_claim_coverage'
  AND checkpoint.parent_transition_id = %(execution_transition_id)s
  AND checkpoint.status = 'succeeded'
  AND checkpoint.acceptance_state = 'accepted'
  AND checkpoint.next_transition = 'seal_authority_bundle'
  AND NOT EXISTS (
    SELECT 1
    FROM waje_runtime.workflow_transition_attempts child
    WHERE child.run_attempt_id = checkpoint.run_attempt_id
      AND child.parent_transition_id = head.transition_id
      AND child.status = 'succeeded'
      AND child.acceptance_state = 'accepted'
  )
"""


_ACTIVE_DECISIONS_SQL = """
/* authority_seal_active_decisions */
SELECT decision.payload
FROM waje_runtime.decision_records decision
WHERE decision.run_attempt_id = %(run_attempt_id)s
  AND decision.status <> 'invalidated'
  AND NOT EXISTS (
    SELECT 1
    FROM waje_runtime.decision_records successor
    WHERE successor.run_attempt_id = decision.run_attempt_id
      AND successor.supersedes_decision_id = decision.decision_id
  )
ORDER BY decision.ledger_position
"""


_PROVIDER_RESPONSES_SQL = """
/* authority_seal_provider_response_closure */
SELECT
  response.provider_response_ref,
  response.owner_ref,
  response.run_attempt_id,
  response.attempt_id,
  response.purpose,
  response.provider_ref,
  response.model_ref,
  response.input_ref,
  response.input_digest,
  response.attempt_number,
  response.raw_response_content,
  response.content_digest,
  response.payload
FROM waje_runtime.restricted_provider_responses response
WHERE response.owner_ref = %(owner_ref)s
  AND response.run_attempt_id = %(run_attempt_id)s
  AND response.purpose = ANY(%(allowed_purposes)s)
ORDER BY response.provider_response_ref
"""


_PHASE4_PROVIDER_RESPONSE_PURPOSES = frozenset(
    {
        "candidate_claim_proposal",
        "claim_verification",
        "recommendation_proposal",
        "recommendation_verification",
    }
)

_SETTLEMENT_TRANSITION_PROVIDER_REF = "waje-semantic-authority"
_SETTLEMENT_TRANSITION_MODEL_REF = "single-authority-phase04.v1"


_EXECUTION_CLOSURE_TABLES = {
    "attempts": "capability_task_attempts",
    "outcomes": "capability_outcomes",
    "evidence": "capability_evidence_ledger_entries",
    "failures": "capability_failure_records",
}

_JSON_COLUMNS = frozenset(
    {"payload", "obligation_coverage_refs", "input_payload", "output_payload"}
)


def semantic_authority_transition_payloads(
    semantic_authority_result: SemanticAuthorityResult,
    authority_bundle: AuthorityBundle,
    *,
    claim_coverage_checkpoint_ref: str,
    claim_coverage_checkpoint_digest: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if type(semantic_authority_result) is not SemanticAuthorityResult:
        raise AuthoritySealPersistenceError("authority_seal_semantic_result_invalid")
    result = semantic_authority_result
    inputs = result.authority_bundle_inputs
    _, bundle = _validated_seal_input(inputs, authority_bundle)
    checkpoint_ref = _required_checkpoint_ref(
        claim_coverage_checkpoint_ref,
        claim_coverage_checkpoint_digest,
    )
    return (
        {
            "authoritative_execution_result_ref": inputs.execution_result_ref,
            "authoritative_execution_result_digest": inputs.execution_result_digest,
            "authority_namespace_ref": inputs.authority_namespace_ref,
            "claim_coverage_checkpoint_ref": checkpoint_ref,
            "claim_coverage_checkpoint_digest": (claim_coverage_checkpoint_digest),
        },
        {
            "semantic_authority_result": result.to_dict(),
            "authority_bundle": bundle.to_dict(),
        },
    )


def seal_authority_bundle(
    connection: Any,
    *,
    owner_ref: str,
    thread_ref: str,
    authority_inputs: AuthorityBundleInputs,
    authority_bundle: AuthorityBundle,
    provider_responses: Sequence[RestrictedProviderResponse],
    semantic_authority_result: SemanticAuthorityResult,
    claim_coverage_checkpoint: ClaimCoverageCheckpoint,
    settlement_transition: DurableTransition,
    attempt_journal: DurableCallJournal,
    accepted_attempt_refs: Sequence[str],
) -> AuthoritySealResult:
    inputs, bundle = _validated_seal_input(authority_inputs, authority_bundle)
    coverage_checkpoint = _validated_claim_coverage_checkpoint(
        claim_coverage_checkpoint,
        inputs=inputs,
    )
    if (
        not isinstance(owner_ref, str)
        or not owner_ref.strip()
        or owner_ref != owner_ref.strip()
        or not isinstance(thread_ref, str)
        or not thread_ref.strip()
        or thread_ref != thread_ref.strip()
    ):
        raise AuthoritySealPersistenceError("authority_seal_owner_scope_invalid")
    execution = inputs.execution_result
    run_attempt_id = inputs.run_attempt_id
    if not isinstance(attempt_journal, DurableCallJournal):
        raise AuthoritySealPersistenceError("authority_seal_attempt_journal_invalid")
    if isinstance(accepted_attempt_refs, (str, bytes)):
        raise AuthoritySealPersistenceError("authority_seal_attempt_refs_invalid")
    normalized_attempt_refs = tuple(accepted_attempt_refs)

    try:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": f"single_authority:{run_attempt_id}"},
        )
        row = connection.execute(
            _PREFLIGHT_SQL,
            {
                "run_attempt_id": run_attempt_id,
                "intent_revision_id": inputs.intent_revision_id,
                "authority_context_ref": inputs.authority_context_ref,
                "plan_revision_id": inputs.plan_revision_id,
                "execution_snapshot_ref": execution.execution_snapshot_ref,
                "stop_ref": execution.stop_ref,
                "transition_id": execution.transition_id,
            },
        ).fetchone()
        if row is None:
            raise AuthoritySealPersistenceError("authority_seal_active_chain_missing")
        lifecycle, replaying = _validate_active_chain(
            row,
            inputs=inputs,
            bundle=bundle,
            owner_ref=owner_ref,
            thread_ref=thread_ref,
        )
        _validate_active_decisions(connection, inputs)
        _, responses, transition, transition_input, transition_output = (
            _validated_semantic_checkpoint(
                inputs=inputs,
                bundle=bundle,
                provider_responses=provider_responses,
                semantic_authority_result=semantic_authority_result,
                claim_coverage_checkpoint=coverage_checkpoint,
                settlement_transition=settlement_transition,
            )
        )
        _validate_persisted_claim_coverage_checkpoint(
            connection,
            checkpoint=coverage_checkpoint,
            settlement_transition=transition,
            settlement_input=transition_input,
            settlement_output=transition_output,
            replaying=replaying,
        )
        _persist_provider_responses(
            connection,
            inputs,
            responses,
            owner_ref=owner_ref,
            replay_only=replaying,
        )
        _validate_provider_response_set(
            connection,
            inputs,
            responses,
            owner_ref=owner_ref,
        )
        _validate_provider_verification_closure(inputs, responses)
        _validate_execution_closure(connection, inputs)
        _persist_authority_closure(
            connection,
            inputs,
            bundle,
            owner_ref=owner_ref,
            thread_ref=thread_ref,
        )
        _persist_settlement_transition(
            connection,
            transition=transition,
            input_payload=transition_input,
            output_payload=transition_output,
            replay_only=replaying,
        )
        _persist_authority_lifecycle(
            connection,
            lifecycle=lifecycle,
            replay_only=replaying,
        )
        attempt_journal.bind_stage(
            run_attempt_id=run_attempt_id,
            transition_attempt_id=transition.attempt_id,
            stage_name="settle_claim_authority",
            attempt_refs=normalized_attempt_refs,
            commit=False,
        )
        connection.commit()
        return AuthoritySealResult(
            bundle_ref=bundle.bundle_ref,
            bundle_digest=bundle.bundle_digest,
            status="replayed" if replaying else "inserted",
            lifecycle_state_digest=lifecycle.content_digest,
        )
    except Exception:
        connection.rollback()
        raise


def _validated_seal_input(
    authority_inputs: AuthorityBundleInputs,
    authority_bundle: AuthorityBundle,
) -> tuple[AuthorityBundleInputs, AuthorityBundle]:
    if type(authority_inputs) is not AuthorityBundleInputs:
        raise AuthoritySealPersistenceError("authority_seal_inputs_invalid")
    if type(authority_bundle) is not AuthorityBundle:
        raise AuthoritySealPersistenceError("authority_seal_bundle_invalid")
    try:
        expected_bundle = authority_inputs.seal(
            bundle_revision=authority_bundle.bundle_revision,
            supersedes_bundle_ref=authority_bundle.supersedes_bundle_ref,
            sealed_at=authority_bundle.sealed_at,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise AuthoritySealPersistenceError("authority_seal_inputs_invalid") from exc
    if expected_bundle != authority_bundle:
        raise AuthoritySealPersistenceError("authority_seal_bundle_invalid")
    if (
        expected_bundle.bundle_revision != 1
        or expected_bundle.supersedes_bundle_ref is not None
    ):
        raise AuthoritySealPersistenceError("authority_seal_bundle_revision_invalid")
    return authority_inputs, expected_bundle


def _required_checkpoint_ref(value: Any, digest: Any) -> str:
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or value != "claim-coverage-checkpoint:sha256:" + digest
    ):
        raise AuthoritySealPersistenceError(
            "authority_seal_claim_coverage_checkpoint_invalid"
        )
    return value


def _validated_claim_coverage_checkpoint(
    value: ClaimCoverageCheckpoint,
    *,
    inputs: AuthorityBundleInputs,
) -> ClaimCoverageCheckpoint:
    if type(value) is not ClaimCoverageCheckpoint:
        raise AuthoritySealPersistenceError(
            "authority_seal_claim_coverage_checkpoint_invalid"
        )
    execution = inputs.execution_result
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
        raise AuthoritySealPersistenceError(
            "authority_seal_claim_coverage_checkpoint_invalid"
        ) from exc
    if (
        checkpoint != value
        or checkpoint.decision.decision != "seal"
        or checkpoint.plan_patch is not None
        or checkpoint.run_attempt_id != inputs.run_attempt_id
        or checkpoint.intent_revision_id != inputs.intent_revision_id
        or checkpoint.authority_context_ref != inputs.authority_context_ref
        or checkpoint.source_plan_revision_id != inputs.plan_revision_id
        or checkpoint.source_execution_result_ref != inputs.execution_result_ref
        or checkpoint.transition.next_transition != "seal_authority_bundle"
    ):
        raise AuthoritySealPersistenceError(
            "authority_seal_claim_coverage_checkpoint_invalid"
        )
    _required_checkpoint_ref(checkpoint.checkpoint_ref, checkpoint.content_digest)
    return checkpoint


def _validated_semantic_checkpoint(
    *,
    inputs: AuthorityBundleInputs,
    bundle: AuthorityBundle,
    provider_responses: Sequence[RestrictedProviderResponse],
    semantic_authority_result: SemanticAuthorityResult,
    claim_coverage_checkpoint: ClaimCoverageCheckpoint,
    settlement_transition: DurableTransition,
) -> tuple[
    SemanticAuthorityResult,
    tuple[RestrictedProviderResponse, ...],
    DurableTransition,
    Mapping[str, Any],
    Mapping[str, Any],
]:
    if type(semantic_authority_result) is not SemanticAuthorityResult:
        raise AuthoritySealPersistenceError("authority_seal_semantic_result_invalid")
    result = semantic_authority_result
    if result.authority_bundle_inputs != inputs:
        raise AuthoritySealPersistenceError("authority_seal_semantic_result_invalid")
    responses = _validated_provider_responses(inputs, provider_responses)
    if responses != result.provider_responses:
        raise AuthoritySealPersistenceError(
            "authority_seal_provider_response_set_conflict"
        )
    checkpoint = _validated_claim_coverage_checkpoint(
        claim_coverage_checkpoint,
        inputs=inputs,
    )
    transition_input, transition_output = semantic_authority_transition_payloads(
        result,
        bundle,
        claim_coverage_checkpoint_ref=checkpoint.checkpoint_ref,
        claim_coverage_checkpoint_digest=checkpoint.content_digest,
    )
    if type(settlement_transition) is not DurableTransition:
        raise AuthoritySealPersistenceError(
            "authority_seal_settlement_transition_invalid"
        )
    try:
        transition = DurableTransition.from_dict(settlement_transition.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise AuthoritySealPersistenceError(
            "authority_seal_settlement_transition_invalid"
        ) from exc
    if (
        transition != settlement_transition
        or transition.node_name != "settle_claim_authority"
        or transition.parent_transition_id != checkpoint.transition_id
        or transition.run_attempt_id != inputs.run_attempt_id
        or transition.intent_revision_id != inputs.intent_revision_id
        or transition.decision_ledger_position
        != checkpoint.transition.decision_ledger_position
        or transition.input_digest != canonical_digest(transition_input)
        or transition.output_digest != canonical_digest(transition_output)
        or transition.status != "succeeded"
        or transition.acceptance_state != "accepted"
        or transition.provider_ref != _SETTLEMENT_TRANSITION_PROVIDER_REF
        or transition.model_ref != _SETTLEMENT_TRANSITION_MODEL_REF
        or transition.next_transition != "compose_claim_aware_narrative"
    ):
        raise AuthoritySealPersistenceError(
            "authority_seal_settlement_transition_invalid"
        )
    return result, responses, transition, transition_input, transition_output


def _validate_persisted_claim_coverage_checkpoint(
    connection: Any,
    *,
    checkpoint: ClaimCoverageCheckpoint,
    settlement_transition: DurableTransition,
    settlement_input: Mapping[str, Any],
    settlement_output: Mapping[str, Any],
    replaying: bool,
) -> None:
    expected_checkpoint_input, expected_checkpoint_output = (
        claim_coverage_transition_payloads(
            evaluation=checkpoint.evaluation,
            decision=checkpoint.decision,
            plan_patch=checkpoint.plan_patch,
        )
    )
    expected_head_transition_id = (
        settlement_transition.transition_id if replaying else checkpoint.transition_id
    )
    row = connection.execute(
        _CLAIM_COVERAGE_CHECKPOINT_SQL,
        {
            "run_attempt_id": checkpoint.run_attempt_id,
            "intent_revision_id": checkpoint.intent_revision_id,
            "execution_transition_id": checkpoint.transition.parent_transition_id,
            "checkpoint_transition_id": checkpoint.transition_id,
            "settlement_transition_id": settlement_transition.transition_id,
            "head_transition_id": expected_head_transition_id,
        },
    ).fetchone()
    if row is None:
        raise AuthoritySealPersistenceError(
            "authority_seal_claim_coverage_checkpoint_missing"
        )
    try:
        persisted_checkpoint = DurableTransition.from_dict(
            _payload(_field(row, "checkpoint_transition_payload", 0))
        )
    except (TypeError, ValueError) as exc:
        raise AuthoritySealPersistenceError(
            "authority_seal_claim_coverage_checkpoint_conflict"
        ) from exc
    if (
        persisted_checkpoint != checkpoint.transition
        or canonical_value(_payload(_field(row, "checkpoint_input_payload", 1)))
        != canonical_value(expected_checkpoint_input)
        or canonical_value(_payload(_field(row, "checkpoint_output_payload", 2)))
        != canonical_value(expected_checkpoint_output)
        or str(_field(row, "head_transition_id", 6) or "")
        != expected_head_transition_id
    ):
        raise AuthoritySealPersistenceError(
            "authority_seal_claim_coverage_checkpoint_conflict"
        )

    persisted_settlement_payload = _optional_payload(
        _field(row, "settlement_transition_payload", 3)
    )
    persisted_settlement_input = _optional_payload(
        _field(row, "settlement_input_payload", 4)
    )
    persisted_settlement_output = _optional_payload(
        _field(row, "settlement_output_payload", 5)
    )
    if not replaying:
        if any(
            value is not None
            for value in (
                persisted_settlement_payload,
                persisted_settlement_input,
                persisted_settlement_output,
            )
        ):
            raise AuthoritySealPersistenceError(
                "authority_seal_claim_coverage_head_conflict"
            )
        return
    try:
        persisted_settlement = DurableTransition.from_dict(
            persisted_settlement_payload or {}
        )
    except (TypeError, ValueError) as exc:
        raise AuthoritySealPersistenceError(
            "authority_seal_claim_coverage_head_conflict"
        ) from exc
    if (
        persisted_settlement != settlement_transition
        or canonical_value(persisted_settlement_input)
        != canonical_value(settlement_input)
        or canonical_value(persisted_settlement_output)
        != canonical_value(settlement_output)
    ):
        raise AuthoritySealPersistenceError(
            "authority_seal_claim_coverage_head_conflict"
        )


def _validate_active_chain(
    row: Any,
    *,
    inputs: AuthorityBundleInputs,
    bundle: AuthorityBundle,
    owner_ref: str,
    thread_ref: str,
) -> tuple[LifecycleState, bool]:
    execution = inputs.execution_result
    persisted_owner_ref = str(_field(row, "owner_ref", 0) or "")
    persisted_thread_ref = str(_field(row, "thread_ref", 1) or "")
    if persisted_owner_ref != owner_ref or persisted_thread_ref != thread_ref:
        raise AuthoritySealPersistenceError("authority_seal_owner_scope_conflict")

    intent = IntentRevision.from_dict(_payload(_field(row, "intent_payload", 2)))
    if (
        intent.intent_revision_id != inputs.intent_revision_id
        or intent.run_attempt_id != inputs.run_attempt_id
        or intent.content_digest != str(_field(row, "intent_content_digest", 3) or "")
    ):
        raise AuthoritySealPersistenceError("authority_seal_intent_conflict")

    context = AuthorityContext.from_dict(
        _payload(_field(row, "authority_context_payload", 4))
    )
    if (
        context.authority_context_ref != inputs.authority_context_ref
        or context.run_attempt_id != inputs.run_attempt_id
        or context.content_digest
        != str(_field(row, "authority_context_content_digest", 5) or "")
    ):
        raise AuthoritySealPersistenceError("authority_seal_authority_context_conflict")

    plan = PlanRevision.from_dict(_payload(_field(row, "plan_payload", 6)))
    if plan != execution.plan_revision or plan.content_digest != str(
        _field(row, "plan_content_digest", 7) or ""
    ):
        raise AuthoritySealPersistenceError("authority_seal_plan_conflict")

    snapshot = ExecutionSnapshot.from_dict(
        _payload(_field(row, "execution_snapshot_payload", 8))
    )
    if snapshot != execution.execution_snapshot or snapshot.content_digest != str(
        _field(row, "execution_snapshot_content_digest", 9) or ""
    ):
        raise AuthoritySealPersistenceError(
            "authority_seal_execution_snapshot_conflict"
        )

    stop = ExplorationStopRecord.from_dict(_payload(_field(row, "stop_payload", 10)))
    if stop != execution.exploration_stop_record or stop.content_digest != str(
        _field(row, "stop_content_digest", 11) or ""
    ):
        raise AuthoritySealPersistenceError("authority_seal_stop_record_conflict")

    transition = DurableTransition.from_dict(
        _payload(_field(row, "transition_payload", 12))
    )
    transition_input, transition_output = capability_execution_transition_payloads(
        plan,
        snapshot,
        stop,
    )
    if (
        transition != execution.durable_transition
        or canonical_value(_payload(_field(row, "transition_input_payload", 13)))
        != canonical_value(transition_input)
        or canonical_value(_payload(_field(row, "transition_output_payload", 14)))
        != canonical_value(transition_output)
    ):
        raise AuthoritySealPersistenceError("authority_seal_transition_conflict")

    lifecycle = LifecycleState.from_dict(_payload(_field(row, "lifecycle_payload", 15)))
    expected_evidence_state = (
        "boundary_only"
        if inputs.claim_graph.authority_mode == "boundary_only"
        else "complete"
    )
    if (
        lifecycle.cancellation_state != "active"
        or lifecycle.supersession_state != "active"
    ):
        raise AuthoritySealPersistenceError("authority_seal_lifecycle_not_active")

    existing_payload = _optional_payload(_field(row, "existing_bundle_payload", 16))
    if existing_payload is not None and canonical_value(
        existing_payload
    ) != canonical_value(bundle.to_dict()):
        raise AuthoritySealPersistenceError("authority_bundle_run_seal_conflict")
    replaying = existing_payload is not None
    if replaying:
        if (
            lifecycle.run_attempt_id != inputs.run_attempt_id
            or lifecycle.execution_state != "complete"
            or lifecycle.interaction_state != "active"
            or lifecycle.evidence_state != expected_evidence_state
            or lifecycle.publication_state != "not_ready"
            or lifecycle.delivery_state != "pending"
            or lifecycle.retry_state != "idle"
        ):
            raise AuthoritySealPersistenceError("authority_seal_lifecycle_not_ready")
        return lifecycle, True
    if (
        lifecycle.run_attempt_id != inputs.run_attempt_id
        or lifecycle.execution_state != "complete"
        or lifecycle.interaction_state != "active"
        or lifecycle.evidence_state != "partial"
        or lifecycle.publication_state != "not_ready"
        or lifecycle.delivery_state != "pending"
        or lifecycle.retry_state != "idle"
    ):
        raise AuthoritySealPersistenceError("authority_seal_lifecycle_not_ready")
    return lifecycle.transition(evidence_state=expected_evidence_state), False


def _validate_active_decisions(
    connection: Any,
    inputs: AuthorityBundleInputs,
) -> None:
    rows = connection.execute(
        _ACTIVE_DECISIONS_SQL,
        {"run_attempt_id": inputs.run_attempt_id},
    ).fetchall()
    records = tuple(
        DecisionRecord.from_dict(_payload(_field(row, "payload", 0))) for row in rows
    )
    decision_ids = tuple(record.decision_id for record in records)
    if (
        len(decision_ids) != len(set(decision_ids))
        or set(decision_ids) != set(inputs.decision_refs)
        or any(
            record.intent_revision_id != inputs.intent_revision_id for record in records
        )
    ):
        raise AuthoritySealPersistenceError("authority_seal_decision_closure_conflict")


def _validate_execution_closure(
    connection: Any,
    inputs: AuthorityBundleInputs,
) -> None:
    execution = inputs.execution_result
    expected = {
        "attempts": tuple(
            bundle[0].to_dict() for bundle in execution.capability_outcome_bundles
        ),
        "outcomes": tuple(
            bundle[1].to_dict() for bundle in execution.capability_outcome_bundles
        ),
        "evidence": tuple(
            entry.to_dict()
            for bundle in execution.capability_outcome_bundles
            for entry in bundle[2]
        ),
        "failures": tuple(
            failure.to_dict()
            for bundle in execution.capability_outcome_bundles
            for failure in bundle[3]
        ),
    }
    for kind, table in _EXECUTION_CLOSURE_TABLES.items():
        rows = connection.execute(
            f"""
            /* authority_seal_execution_closure:{kind} */
            SELECT payload
            FROM waje_runtime.{table}
            WHERE plan_revision_id = %(plan_revision_id)s
            """,
            {"plan_revision_id": inputs.plan_revision_id},
        ).fetchall()
        stored = tuple(_payload(_field(row, "payload", 0)) for row in rows)
        if _canonical_record_set(stored) != _canonical_record_set(expected[kind]):
            raise AuthoritySealPersistenceError(
                f"authority_seal_execution_{kind}_conflict"
            )


def _validated_provider_responses(
    inputs: AuthorityBundleInputs,
    value: Sequence[RestrictedProviderResponse],
) -> tuple[RestrictedProviderResponse, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AuthoritySealPersistenceError("authority_seal_provider_responses_invalid")
    responses: list[RestrictedProviderResponse] = []
    for item in value:
        if type(item) is not RestrictedProviderResponse:
            raise AuthoritySealPersistenceError(
                "authority_seal_provider_responses_invalid"
            )
        try:
            replayed = RestrictedProviderResponse.from_dict(item.to_dict())
        except (AttributeError, TypeError, ValueError) as exc:
            raise AuthoritySealPersistenceError(
                "authority_seal_provider_responses_invalid"
            ) from exc
        if replayed != item or item.purpose not in _PHASE4_PROVIDER_RESPONSE_PURPOSES:
            raise AuthoritySealPersistenceError(
                "authority_seal_provider_responses_invalid"
            )
        responses.append(replayed)
    normalized = tuple(responses)
    for identity in (
        tuple(item.response_ref for item in normalized),
        tuple(item.attempt_id for item in normalized),
        tuple(item.content_digest for item in normalized),
        tuple(
            (
                item.purpose,
                item.input_ref,
                item.input_digest,
                item.attempt_number,
            )
            for item in normalized
        ),
    ):
        if len(identity) != len(set(identity)):
            raise AuthoritySealPersistenceError(
                "authority_seal_provider_response_identity_conflict"
            )
    checkpoint_ref = inputs.claim_settlement.checkpoint_ref
    graph_ref = inputs.claim_graph.claim_graph_ref
    proposal_groups: dict[str, set[tuple[str, str, str]]] = {
        "candidate_claim_proposal": set(),
        "recommendation_proposal": set(),
    }
    for response in normalized:
        if response.purpose == "claim_verification":
            valid_input = response.input_ref == checkpoint_ref
        elif response.purpose in {
            "recommendation_proposal",
            "recommendation_verification",
        }:
            valid_input = response.input_ref == graph_ref
        else:
            valid_input = (
                response.input_ref.startswith("restricted-execution-projection:sha256:")
                and len(
                    response.input_ref.removeprefix(
                        "restricted-execution-projection:sha256:"
                    )
                )
                == 64
            )
        if not valid_input:
            raise AuthoritySealPersistenceError(
                "authority_seal_provider_response_input_scope_conflict"
            )
        if response.purpose in proposal_groups:
            proposal_groups[response.purpose].add(
                (
                    response.provider_ref,
                    response.model_ref,
                    response.input_digest,
                )
            )
    if any(len(groups) > 1 for groups in proposal_groups.values()):
        raise AuthoritySealPersistenceError(
            "authority_seal_provider_response_call_identity_conflict"
        )
    return normalized


def _persist_provider_responses(
    connection: Any,
    inputs: AuthorityBundleInputs,
    responses: Sequence[RestrictedProviderResponse],
    *,
    owner_ref: str,
    replay_only: bool,
) -> None:
    common = {
        "owner_ref": owner_ref,
        "run_attempt_id": inputs.run_attempt_id,
    }
    for response in responses:
        _insert_exact(
            connection,
            table="restricted_provider_responses",
            identity="provider_response_ref",
            collision="restricted_provider_response",
            values={
                "provider_response_ref": response.response_ref,
                **common,
                "attempt_id": response.attempt_id,
                "purpose": response.purpose,
                "provider_ref": response.provider_ref,
                "model_ref": response.model_ref,
                "input_ref": response.input_ref,
                "input_digest": response.input_digest,
                "attempt_number": response.attempt_number,
                "raw_response_content": response.content,
                "content_digest": response.content_digest,
                "payload": response.to_dict(),
            },
            replay_only=replay_only,
        )


def _validate_provider_response_set(
    connection: Any,
    inputs: AuthorityBundleInputs,
    responses: Sequence[RestrictedProviderResponse],
    *,
    owner_ref: str,
) -> None:
    expected = {item.response_ref: item for item in responses}
    rows = connection.execute(
        _PROVIDER_RESPONSES_SQL,
        {
            "owner_ref": owner_ref,
            "run_attempt_id": inputs.run_attempt_id,
            "allowed_purposes": sorted(_PHASE4_PROVIDER_RESPONSE_PURPOSES),
        },
    ).fetchall()
    stored: dict[str, RestrictedProviderResponse] = {}
    for row in rows:
        ref = str(_field(row, "provider_response_ref", 0) or "")
        persisted_owner_ref = str(_field(row, "owner_ref", 1) or "")
        run_attempt_id = str(_field(row, "run_attempt_id", 2) or "")
        try:
            response = RestrictedProviderResponse.from_dict(
                _payload(_field(row, "payload", 12))
            )
        except (TypeError, ValueError) as exc:
            raise AuthoritySealPersistenceError(
                "authority_seal_provider_response_identity_conflict"
            ) from exc
        persisted_columns = (
            ref,
            str(_field(row, "attempt_id", 3) or ""),
            str(_field(row, "purpose", 4) or ""),
            str(_field(row, "provider_ref", 5) or ""),
            str(_field(row, "model_ref", 6) or ""),
            str(_field(row, "input_ref", 7) or ""),
            str(_field(row, "input_digest", 8) or ""),
            int(_field(row, "attempt_number", 9) or 0),
            str(_field(row, "raw_response_content", 10) or ""),
            str(_field(row, "content_digest", 11) or ""),
        )
        typed_columns = (
            response.response_ref,
            response.attempt_id,
            response.purpose,
            response.provider_ref,
            response.model_ref,
            response.input_ref,
            response.input_digest,
            response.attempt_number,
            response.content,
            response.content_digest,
        )
        if (
            persisted_owner_ref != owner_ref
            or run_attempt_id != inputs.run_attempt_id
            or persisted_columns != typed_columns
            or response.purpose not in _PHASE4_PROVIDER_RESPONSE_PURPOSES
            or ref in stored
        ):
            raise AuthoritySealPersistenceError(
                "authority_seal_provider_response_identity_conflict"
            )
        stored[ref] = response
    if stored != expected:
        raise AuthoritySealPersistenceError(
            "authority_seal_provider_response_set_conflict"
        )


def _validate_provider_verification_closure(
    inputs: AuthorityBundleInputs,
    responses: Sequence[RestrictedProviderResponse],
) -> None:
    report_attempt = inputs.verifier_report.verification_attempt
    attempts = tuple(
        item
        for item in (
            report_attempt,
            *(
                recommendation.verification_attempt
                for recommendation in inputs.recommendations
            ),
        )
        if item is not None
    )
    if not attempts:
        if any(
            response.purpose != "candidate_claim_proposal" for response in responses
        ):
            raise AuthoritySealPersistenceError(
                "authority_seal_boundary_provider_response_closure_conflict"
            )
        return
    expected: dict[str, tuple[str, str, str, str, str, int, str]] = {}
    for attempt in attempts:
        purpose = (
            "claim_verification"
            if attempt.purpose == "claim_settlement"
            else "recommendation_verification"
        )
        value = (
            purpose,
            attempt.provider_ref,
            attempt.model_ref,
            attempt.authority_input_ref,
            attempt.input_digest,
            attempt.attempt_number,
            attempt.raw_provider_response_digest,
        )
        existing = expected.setdefault(attempt.raw_provider_response_ref, value)
        if existing != value:
            raise AuthoritySealPersistenceError(
                "authority_seal_provider_response_identity_conflict"
            )
    stored: dict[str, tuple[str, str, str, str, str, int, str]] = {}
    for response in responses:
        ref = response.response_ref
        if ref not in expected:
            continue
        value = (
            response.purpose,
            response.provider_ref,
            response.model_ref,
            response.input_ref,
            response.input_digest,
            response.attempt_number,
            response.content_digest,
        )
        existing = stored.setdefault(ref, value)
        if existing != value:
            raise AuthoritySealPersistenceError(
                "authority_seal_provider_response_identity_conflict"
            )
    if stored != expected:
        raise AuthoritySealPersistenceError(
            "authority_seal_provider_response_closure_conflict"
        )

    for attempt in attempts:
        related = tuple(
            response
            for response in responses
            if response.purpose
            == (
                "claim_verification"
                if attempt.purpose == "claim_settlement"
                else "recommendation_verification"
            )
            and response.provider_ref == attempt.provider_ref
            and response.model_ref == attempt.model_ref
            and response.input_ref == attempt.authority_input_ref
            and response.input_digest == attempt.input_digest
        )
        if not related or any(
            response.attempt_number > attempt.attempt_number for response in related
        ):
            raise AuthoritySealPersistenceError(
                "authority_seal_provider_response_attempt_sequence_conflict"
            )


def _persist_authority_closure(
    connection: Any,
    inputs: AuthorityBundleInputs,
    bundle: AuthorityBundle,
    *,
    owner_ref: str,
    thread_ref: str,
) -> None:
    namespace = inputs.authority_namespace
    settlement = inputs.claim_settlement
    checkpoint = settlement.checkpoint
    graph = settlement.claim_graph
    report = settlement.verifier_report
    common = {
        "owner_ref": owner_ref,
        "run_attempt_id": inputs.run_attempt_id,
    }

    _insert_exact(
        connection,
        table="claim_authority_namespaces",
        identity="authority_namespace_ref",
        collision="claim_authority_namespace",
        values={
            "authority_namespace_ref": namespace.authority_namespace_ref,
            **common,
            "thread_ref": thread_ref,
            "content_digest": namespace.content_digest,
            "payload": namespace.to_dict(),
        },
    )

    claim_keys = _unique_records(
        (*checkpoint.proposed_claim_keys, *settlement.accepted_claim_keys),
        identity="claim_key",
    )
    for record in claim_keys:
        _insert_exact(
            connection,
            table="claim_keys",
            identity="claim_key",
            collision="claim_key",
            values={
                "claim_key": record.claim_key,
                **common,
                "authority_namespace_ref": namespace.authority_namespace_ref,
                "goal_id": record.goal_id,
                "claim_kind": record.claim_kind,
                "content_digest": record.content_digest,
                "payload": record.to_dict(),
            },
        )

    support_edges = _unique_records(
        (*checkpoint.proposed_support_edges, *settlement.accepted_support_edges),
        identity="support_edge_ref",
    )
    for record in support_edges:
        _insert_exact(
            connection,
            table="claim_support_edges",
            identity="support_edge_ref",
            collision="claim_support_edge",
            values={
                "support_edge_ref": record.support_edge_ref,
                **common,
                "authority_namespace_ref": namespace.authority_namespace_ref,
                "target_claim_key": record.target_claim_key,
                "source_type": record.source_type,
                "source_ref": record.source_ref,
                "edge_kind": record.kind,
                "content_digest": record.content_digest,
                "payload": record.to_dict(),
            },
        )

    claims = _unique_records(
        (*checkpoint.proposed_claims, *settlement.accepted_claims),
        identity="claim_ref",
    )
    for record in claims:
        _insert_exact(
            connection,
            table="claim_revisions",
            identity="claim_ref",
            collision="claim_revision",
            values={
                "claim_ref": record.claim_ref,
                **common,
                "authority_namespace_ref": namespace.authority_namespace_ref,
                "claim_key": record.claim_key,
                "claim_class": record.claim_class,
                "claim_status": record.status,
                "content_digest": record.content_digest,
                "payload": record.to_dict(),
            },
        )

    _insert_exact(
        connection,
        table="claim_settlement_checkpoints",
        identity="checkpoint_ref",
        collision="claim_settlement_checkpoint",
        values={
            "checkpoint_ref": checkpoint.checkpoint_ref,
            **common,
            "authority_namespace_ref": namespace.authority_namespace_ref,
            "execution_result_ref": checkpoint.execution_result_ref,
            "execution_result_digest": checkpoint.execution_result_digest,
            "plan_revision_id": checkpoint.plan_revision_id,
            "content_digest": checkpoint.content_digest,
            "payload": checkpoint.to_dict(),
        },
    )
    for basis in checkpoint.obligation_basis:
        _insert_exact(
            connection,
            table="claim_obligation_settlement_bases",
            identity="basis_ref",
            collision="claim_obligation_settlement_basis",
            values={
                "basis_ref": basis.basis_ref,
                **common,
                "authority_namespace_ref": namespace.authority_namespace_ref,
                "checkpoint_ref": checkpoint.checkpoint_ref,
                "obligation_id": basis.obligation_id,
                "content_digest": basis.content_digest,
                "payload": basis.to_dict(),
            },
        )

    claim_attempt = report.verification_attempt
    if claim_attempt is not None:
        _insert_exact(
            connection,
            table="claim_verification_attempts",
            identity="verification_attempt_ref",
            collision="claim_verification_attempt",
            values={
                "verification_attempt_ref": claim_attempt.verification_attempt_ref,
                **common,
                "authority_namespace_ref": namespace.authority_namespace_ref,
                "checkpoint_ref": checkpoint.checkpoint_ref,
                "authority_input_ref": claim_attempt.authority_input_ref,
                "authority_input_digest": claim_attempt.authority_input_digest,
                "provider_ref": claim_attempt.provider_ref,
                "model_ref": claim_attempt.model_ref,
                "input_digest": claim_attempt.input_digest,
                "attempt_number": claim_attempt.attempt_number,
                "raw_provider_response_ref": (claim_attempt.raw_provider_response_ref),
                "raw_provider_response_digest": (
                    claim_attempt.raw_provider_response_digest
                ),
                "content_digest": claim_attempt.content_digest,
                "payload": claim_attempt.to_dict(),
            },
        )
        for decision in report.verification_decisions:
            _insert_exact(
                connection,
                table="claim_verification_decisions",
                identity="verification_decision_ref",
                collision="claim_verification_decision",
                values={
                    "verification_decision_ref": decision.verification_decision_ref,
                    **common,
                    "verification_attempt_ref": claim_attempt.verification_attempt_ref,
                    "subject_ref": decision.subject_ref,
                    "disposition": decision.disposition,
                    "content_digest": decision.content_digest,
                    "payload": decision.to_dict(),
                },
            )

    boundary_authority = report.local_boundary_authority
    if boundary_authority is not None:
        _insert_exact(
            connection,
            table="local_boundary_authorities",
            identity="local_boundary_authority_ref",
            collision="local_boundary_authority",
            values={
                "local_boundary_authority_ref": (
                    boundary_authority.local_boundary_authority_ref
                ),
                **common,
                "authority_namespace_ref": namespace.authority_namespace_ref,
                "checkpoint_ref": checkpoint.checkpoint_ref,
                "checkpoint_digest": boundary_authority.checkpoint_digest,
                "content_digest": boundary_authority.content_digest,
                "payload": boundary_authority.to_dict(),
            },
        )

    _insert_exact(
        connection,
        table="claim_verification_reports",
        identity="verifier_report_ref",
        collision="claim_verification_report",
        values={
            "verifier_report_ref": report.verifier_report_ref,
            **common,
            "verification_mode": report.verification_mode,
            "checkpoint_ref": report.checkpoint_ref,
            "verification_attempt_ref": (
                claim_attempt.verification_attempt_ref
                if claim_attempt is not None
                else None
            ),
            "local_boundary_authority_ref": report.local_boundary_authority_ref,
            "authority_input_ref": report.authority_input_ref,
            "authority_input_digest": report.authority_input_digest,
            "content_digest": report.content_digest,
            "payload": report.to_dict(),
        },
    )
    for coverage in settlement.obligation_coverage:
        _insert_exact(
            connection,
            table="claim_obligation_coverages",
            identity="coverage_ref",
            collision="claim_obligation_coverage",
            values={
                "coverage_ref": coverage.coverage_ref,
                **common,
                "obligation_id": coverage.obligation_id,
                "claim_verifier_report_ref": report.verifier_report_ref,
                "coverage_state": coverage.status,
                "content_digest": coverage.content_digest,
                "payload": coverage.to_dict(),
            },
        )

    _insert_exact(
        connection,
        table="claim_graphs",
        identity="claim_graph_ref",
        collision="claim_graph",
        values={
            "claim_graph_ref": graph.claim_graph_ref,
            **common,
            "authority_namespace_ref": namespace.authority_namespace_ref,
            "authority_mode": graph.authority_mode,
            "claim_verifier_report_ref": report.verifier_report_ref,
            "content_digest": graph.content_digest,
            "payload": graph.to_dict(),
        },
    )
    _insert_exact(
        connection,
        table="claim_settlements",
        identity="settlement_ref",
        collision="claim_settlement",
        values={
            "settlement_ref": settlement.settlement_ref,
            **common,
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "claim_graph_ref": graph.claim_graph_ref,
            "claim_graph_digest": graph.content_digest,
            "execution_result_ref": settlement.execution_result_ref,
            "execution_result_digest": settlement.execution_result_digest,
            "claim_verifier_report_ref": report.verifier_report_ref,
            "content_digest": settlement.content_digest,
            "payload": settlement.to_dict(),
        },
    )

    for recommendation in inputs.recommendations:
        proposal = recommendation.proposal
        attempt = recommendation.verification_attempt
        decision = recommendation.verification_decision
        _insert_exact(
            connection,
            table="recommendation_proposals",
            identity="recommendation_proposal_ref",
            collision="recommendation_proposal",
            values={
                "recommendation_proposal_ref": proposal.recommendation_proposal_ref,
                **common,
                "authority_namespace_ref": namespace.authority_namespace_ref,
                "claim_graph_ref": graph.claim_graph_ref,
                "content_digest": proposal.content_digest,
                "payload": proposal.to_dict(),
            },
        )
        _insert_exact(
            connection,
            table="recommendation_verification_attempts",
            identity="verification_attempt_ref",
            collision="recommendation_verification_attempt",
            values={
                "verification_attempt_ref": attempt.verification_attempt_ref,
                **common,
                "recommendation_proposal_ref": proposal.recommendation_proposal_ref,
                "authority_input_ref": attempt.authority_input_ref,
                "authority_input_digest": attempt.authority_input_digest,
                "provider_ref": attempt.provider_ref,
                "model_ref": attempt.model_ref,
                "input_digest": attempt.input_digest,
                "attempt_number": attempt.attempt_number,
                "raw_provider_response_ref": attempt.raw_provider_response_ref,
                "raw_provider_response_digest": (attempt.raw_provider_response_digest),
                "content_digest": attempt.content_digest,
                "payload": attempt.to_dict(),
            },
        )
        _insert_exact(
            connection,
            table="recommendation_verification_decisions",
            identity="verification_decision_ref",
            collision="recommendation_verification_decision",
            values={
                "verification_decision_ref": decision.verification_decision_ref,
                **common,
                "verification_attempt_ref": attempt.verification_attempt_ref,
                "recommendation_proposal_ref": proposal.recommendation_proposal_ref,
                "disposition": decision.disposition,
                "content_digest": decision.content_digest,
                "payload": decision.to_dict(),
            },
        )
        _insert_exact(
            connection,
            table="recommendation_records",
            identity="recommendation_ref",
            collision="recommendation_record",
            values={
                "recommendation_ref": recommendation.recommendation_ref,
                **common,
                "recommendation_proposal_ref": proposal.recommendation_proposal_ref,
                "verification_attempt_ref": attempt.verification_attempt_ref,
                "verification_decision_ref": decision.verification_decision_ref,
                "claim_graph_ref": graph.claim_graph_ref,
                "claim_verifier_report_ref": report.verifier_report_ref,
                "content_digest": recommendation.content_digest,
                "payload": recommendation.to_dict(),
            },
        )

    _insert_exact(
        connection,
        table="authority_bundles",
        identity="bundle_ref",
        collision="authority_bundle",
        values={
            "bundle_ref": bundle.bundle_ref,
            **common,
            "authority_namespace_ref": namespace.authority_namespace_ref,
            "bundle_revision": bundle.bundle_revision,
            "supersedes_bundle_ref": bundle.supersedes_bundle_ref,
            "intent_revision_id": bundle.intent_revision_id,
            "plan_revision_id": bundle.plan_revision_id,
            "authority_context_ref": bundle.authority_context_ref,
            "execution_result_ref": bundle.execution_result_ref,
            "execution_result_digest": bundle.execution_result_digest,
            "claim_settlement_ref": bundle.claim_settlement_ref,
            "claim_settlement_digest": bundle.claim_settlement_digest,
            "claim_graph_ref": bundle.claim_graph_ref,
            "claim_graph_digest": bundle.claim_graph_digest,
            "authority_mode": bundle.authority_mode,
            "obligation_coverage_refs": list(bundle.obligation_coverage_refs),
            "claim_verifier_report_ref": bundle.claim_verifier_report_ref,
            "bundle_digest": bundle.bundle_digest,
            "seal_state": bundle.seal_state,
            "sealed_at": bundle.sealed_at,
            "content_digest": bundle.content_digest,
            "payload": bundle.to_dict(),
        },
    )


def _persist_settlement_transition(
    connection: Any,
    *,
    transition: DurableTransition,
    input_payload: Mapping[str, Any],
    output_payload: Mapping[str, Any],
    replay_only: bool,
) -> None:
    _insert_exact(
        connection,
        table="workflow_transition_attempts",
        identity="attempt_id",
        collision="settle_claim_authority_transition",
        values={
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
            "input_payload": input_payload,
            "output_payload": output_payload,
            "failure_ref": None,
            "started_at": transition.started_at,
            "finished_at": transition.finished_at,
        },
        replay_only=replay_only,
    )


def _persist_authority_lifecycle(
    connection: Any,
    *,
    lifecycle: LifecycleState,
    replay_only: bool,
) -> None:
    _insert_exact(
        connection,
        table="run_lifecycle_state_revisions",
        identity="state_revision",
        collision="authority_seal_lifecycle",
        values={
            **lifecycle.to_dict(),
            "payload": lifecycle.to_dict(),
        },
        replay_only=replay_only,
    )


def _insert_exact(
    connection: Any,
    *,
    table: str,
    identity: str,
    collision: str,
    values: Mapping[str, Any],
    replay_only: bool = False,
) -> str:
    columns = tuple(values)
    column_sql = ", ".join(columns)
    value_sql = ", ".join(
        f"%({column})s::jsonb" if column in _JSON_COLUMNS else f"%({column})s"
        for column in columns
    )
    replay_equality_sql = "\n          AND ".join(
        (
            f"stored.{column} IS NOT DISTINCT FROM %({column})s::jsonb"
            if column in _JSON_COLUMNS
            else f"stored.{column} IS NOT DISTINCT FROM %({column})s"
        )
        for column in columns
        if column != identity
    )
    params = {
        key: _json_parameter(value) if key in _JSON_COLUMNS else value
        for key, value in values.items()
    }
    if not replay_only:
        inserted = connection.execute(
            f"""
            INSERT INTO waje_runtime.{table} AS current ({column_sql})
            VALUES ({value_sql})
            ON CONFLICT DO NOTHING
            RETURNING {identity}
            """,
            params,
        ).fetchone()
        if inserted is not None:
            return "inserted"
    replayed = connection.execute(
        f"""
        /* authority_seal_exact_replay:{table} */
        SELECT stored.{identity}
        FROM waje_runtime.{table} stored
        WHERE stored.{identity} = %({identity})s
          AND {replay_equality_sql}
        """,
        params,
    ).fetchone()
    if replayed is None:
        raise AuthoritySealPersistenceError(
            f"authority_seal_immutable_conflict:{collision}"
        )
    return "replayed"


def _unique_records(
    records: Sequence[Any],
    *,
    identity: str,
) -> tuple[Any, ...]:
    by_ref: dict[str, Any] = {}
    for record in records:
        ref = str(getattr(record, identity))
        existing = by_ref.setdefault(ref, record)
        if existing != record:
            raise AuthoritySealPersistenceError(
                f"authority_seal_child_identity_conflict:{identity}"
            )
    return tuple(by_ref[ref] for ref in sorted(by_ref))


def _canonical_record_set(
    records: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        sorted(
            (canonical_value(record) for record in records),
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    )


def _json_parameter(value: Any) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _optional_payload(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return _payload(value)


def _payload(value: Any) -> Mapping[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise AuthoritySealPersistenceError("authority_seal_payload_invalid")
    return canonical_value(value)


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


__all__ = (
    "AuthoritySealPersistenceError",
    "AuthoritySealResult",
    "seal_authority_bundle",
    "semantic_authority_transition_payloads",
)
