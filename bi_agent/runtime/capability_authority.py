from __future__ import annotations

from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence, runtime_checkable

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.plan_authority import CapabilityTask, PlanRevision
from bi_agent.runtime.single_authority import DurableTransition

if TYPE_CHECKING:
    from bi_agent.runtime.runtime_persistence import CapabilitySettlementAuthority


class CapabilityAuthorityContractError(ValueError):
    pass


OUTCOME_STATUSES = frozenset(
    {
        "succeeded",
        "unavailable",
        "integrity_failed",
        "technical_failed",
        "skipped",
        "superseded",
    }
)
FAILURE_LAYERS = frozenset({"query", "capability", "evidence", "persistence"})
FAILURE_SCOPES = frozenset(
    {"run", "plan_revision", "task", "claim", "narrative_block", "delivery"}
)
INTEGRITY_LEVELS = frozenset({"expected_boundary", "task", "shared_authority"})
RETRYABILITY_STATES = frozenset({"never", "same_input", "replan_required"})
EXECUTION_STATES = frozenset(
    {"available", "unavailable", "integrity_failed", "technical_failed"}
)
EVIDENCE_KINDS = frozenset(
    {"boundary", "observed", "derived", "scenario", "statistical_association"}
)
CAPABILITY_EVIDENCE_OBSERVATION_BYTE_LIMIT = 64 * 1024
STOP_REASONS = frozenset(
    {
        "plan_exhausted",
        "hard_budget_reached",
        "no_ready_tasks",
        "shared_authority_failure",
    }
)
_STOP_POLICY_STATES = {
    "required_obligations": frozenset(
        {
            "all_tasks_evaluated",
            "unevaluated_required_tasks_remain",
            "blocked_by_shared_failure",
        }
    ),
    "remaining_materiality": frozenset(
        {
            "no_unevaluated_tasks",
            "unevaluated_tasks_remain",
            "not_evaluated_after_failure",
        }
    ),
    "next_information_gain": frozenset(
        {
            "no_eligible_task",
            "eligible_but_budget_blocked",
            "blocked_by_dependency",
            "blocked_by_shared_failure",
        }
    ),
    "actionability": frozenset(
        {"no_remaining_task", "not_evaluated", "blocked_by_shared_failure"}
    ),
    "statistical_risk": frozenset(
        {
            "contract_bounded",
            "multiplicity_sensitive_tasks_remain",
            "not_evaluated_after_failure",
        }
    ),
    "budget": frozenset({"not_limited", "available", "exhausted"}),
}


def _plain(value: Any) -> Any:
    return canonical_value(value)


def _freeze(value: Any) -> Any:
    normalized = canonical_value(value)
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_freeze(item) for item in normalized)
    return normalized


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CapabilityAuthorityContractError(error)
    return value


def _optional_string(value: Any, error: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, error)


def _digest(value: Any, error: str) -> str:
    value = _required_string(value, error)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise CapabilityAuthorityContractError(error)
    return value


def _integer(value: Any, error: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CapabilityAuthorityContractError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CapabilityAuthorityContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise CapabilityAuthorityContractError(error)
    if len(normalized) != len(set(normalized)):
        raise CapabilityAuthorityContractError(error)
    return tuple(sorted(normalized)) if sort else normalized


def _mapping_tuple(value: Any, error: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CapabilityAuthorityContractError(error)
    if any(not isinstance(item, Mapping) for item in value):
        raise CapabilityAuthorityContractError(error)
    normalized = tuple(_freeze(item) for item in value)
    identities = tuple(canonical_digest(item) for item in normalized)
    if len(identities) != len(set(identities)):
        raise CapabilityAuthorityContractError(error)
    return tuple(sorted(normalized, key=canonical_digest))


def _strict_shape(
    payload: Any,
    record_type: type,
    error: str,
) -> Mapping[str, Any]:
    fields = set(record_type.__dataclass_fields__)
    if not isinstance(payload, Mapping) or set(payload) != fields:
        raise CapabilityAuthorityContractError(error)
    return payload


@dataclass(frozen=True)
class CapabilityFailure:
    layer: str
    kind: str
    scope: str
    affected_refs: tuple[str, ...]
    integrity_level: str
    retryability: str
    user_actionable: bool
    business_boundary: str
    technical_detail_ref: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        layer: str,
        kind: str,
        scope: str,
        affected_refs: Sequence[str],
        integrity_level: str,
        retryability: str,
        user_actionable: bool,
        business_boundary: str,
        technical_detail_ref: str,
    ) -> "CapabilityFailure":
        if layer not in FAILURE_LAYERS:
            raise CapabilityAuthorityContractError("capability_failure_layer_invalid")
        if scope not in FAILURE_SCOPES:
            raise CapabilityAuthorityContractError("capability_failure_scope_invalid")
        if integrity_level not in INTEGRITY_LEVELS:
            raise CapabilityAuthorityContractError(
                "capability_failure_integrity_level_invalid"
            )
        if retryability not in RETRYABILITY_STATES:
            raise CapabilityAuthorityContractError(
                "capability_failure_retryability_invalid"
            )
        if type(user_actionable) is not bool:
            raise CapabilityAuthorityContractError(
                "capability_failure_user_actionable_invalid"
            )
        body = {
            "layer": layer,
            "kind": _required_string(kind, "capability_failure_kind_invalid"),
            "scope": scope,
            "affected_refs": _string_tuple(
                affected_refs,
                "capability_failure_affected_refs_invalid",
                allow_empty=False,
            ),
            "integrity_level": integrity_level,
            "retryability": retryability,
            "user_actionable": user_actionable,
            "business_boundary": _required_string(
                business_boundary, "capability_failure_business_boundary_invalid"
            ),
            "technical_detail_ref": _required_string(
                technical_detail_ref,
                "capability_failure_technical_detail_ref_invalid",
            ),
        }
        return cls(content_digest=canonical_digest(body), **body)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CapabilityFailure":
        payload = _strict_shape(payload, cls, "capability_failure_shape_invalid")
        rebuilt = cls.create(
            **{key: payload[key] for key in payload if key != "content_digest"}
        )
        if rebuilt.content_digest != payload.get("content_digest"):
            raise CapabilityAuthorityContractError("capability_failure_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class CapabilityEvidence:
    evidence_ref: str
    binding_record_ref: str | None
    execution_state: str
    evidence_kind: str
    data_contract_state: str
    supported_claim_kinds: tuple[str, ...]
    evidence_strength: str
    maximum_claim_strength: str
    observation_facts: tuple[Mapping[str, Any], ...]
    scope: str
    window_refs: tuple[str, ...]
    dimension_path: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    result_refs: tuple[str, ...]
    completeness_report_refs: tuple[str, ...]
    hierarchy_qualified: bool
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        evidence_ref: str,
        binding_record_ref: str | None,
        execution_state: str,
        evidence_kind: str,
        data_contract_state: str,
        supported_claim_kinds: Sequence[str],
        evidence_strength: str,
        maximum_claim_strength: str,
        observation_facts: Sequence[Mapping[str, Any]],
        scope: str,
        window_refs: Sequence[str],
        dimension_path: Sequence[str],
        limitation_refs: Sequence[str],
        result_refs: Sequence[str],
        completeness_report_refs: Sequence[str],
        hierarchy_qualified: bool,
    ) -> "CapabilityEvidence":
        if execution_state not in EXECUTION_STATES:
            raise CapabilityAuthorityContractError(
                "capability_evidence_execution_state_invalid"
            )
        if evidence_kind not in EVIDENCE_KINDS:
            raise CapabilityAuthorityContractError("capability_evidence_kind_invalid")
        if type(hierarchy_qualified) is not bool:
            raise CapabilityAuthorityContractError(
                "capability_evidence_hierarchy_qualified_invalid"
            )
        dimensions = _string_tuple(
            dimension_path,
            "capability_evidence_dimension_path_invalid",
            sort=False,
        )
        if hierarchy_qualified and not dimensions:
            raise CapabilityAuthorityContractError(
                "capability_evidence_qualified_hierarchy_missing"
            )
        observations = _mapping_tuple(
            observation_facts, "capability_evidence_observation_facts_invalid"
        )
        observation_bytes = len(
            json.dumps(
                canonical_value(observations),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if observation_bytes > CAPABILITY_EVIDENCE_OBSERVATION_BYTE_LIMIT:
            raise CapabilityAuthorityContractError(
                "capability_evidence_observation_budget_exceeded"
            )
        body = {
            "evidence_ref": _required_string(
                evidence_ref, "capability_evidence_ref_invalid"
            ),
            "binding_record_ref": _optional_string(
                binding_record_ref, "capability_evidence_binding_ref_invalid"
            ),
            "execution_state": execution_state,
            "evidence_kind": evidence_kind,
            "data_contract_state": _required_string(
                data_contract_state,
                "capability_evidence_data_contract_state_invalid",
            ),
            "supported_claim_kinds": _string_tuple(
                supported_claim_kinds,
                "capability_evidence_claim_kinds_invalid",
            ),
            "evidence_strength": _required_string(
                evidence_strength,
                "capability_evidence_strength_invalid",
            ),
            "maximum_claim_strength": _required_string(
                maximum_claim_strength,
                "capability_evidence_claim_strength_invalid",
            ),
            "observation_facts": observations,
            "scope": _required_string(scope, "capability_evidence_scope_invalid"),
            "window_refs": _string_tuple(
                window_refs, "capability_evidence_window_refs_invalid"
            ),
            "dimension_path": dimensions,
            "limitation_refs": _string_tuple(
                limitation_refs, "capability_evidence_limitation_refs_invalid"
            ),
            "result_refs": _string_tuple(
                result_refs, "capability_evidence_result_refs_invalid"
            ),
            "completeness_report_refs": _string_tuple(
                completeness_report_refs,
                "capability_evidence_completeness_refs_invalid",
            ),
            "hierarchy_qualified": hierarchy_qualified,
        }
        return cls(content_digest=canonical_digest(body), **body)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CapabilityEvidence":
        payload = _strict_shape(payload, cls, "capability_evidence_shape_invalid")
        rebuilt = cls.create(
            **{key: payload[key] for key in payload if key != "content_digest"}
        )
        if rebuilt.content_digest != payload.get("content_digest"):
            raise CapabilityAuthorityContractError("capability_evidence_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class CapabilityAdapterOutput:
    status: str
    output_payload: Mapping[str, Any]
    output_digest: str
    evidence: tuple[CapabilityEvidence, ...]
    affected_obligation_ids: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    retryability: str
    failure: CapabilityFailure | None
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        status: str,
        output_payload: Mapping[str, Any],
        evidence: Sequence[CapabilityEvidence | Mapping[str, Any]],
        affected_obligation_ids: Sequence[str],
        limitation_refs: Sequence[str],
        retryability: str,
        failure: CapabilityFailure | Mapping[str, Any] | None = None,
    ) -> "CapabilityAdapterOutput":
        if status not in OUTCOME_STATUSES:
            raise CapabilityAuthorityContractError(
                "capability_adapter_output_status_invalid"
            )
        if not isinstance(output_payload, Mapping):
            raise CapabilityAuthorityContractError(
                "capability_adapter_output_payload_invalid"
            )
        normalized_evidence = tuple(
            item
            if isinstance(item, CapabilityEvidence)
            else CapabilityEvidence.from_dict(item)
            for item in evidence
        )
        evidence_refs = tuple(item.evidence_ref for item in normalized_evidence)
        if len(evidence_refs) != len(set(evidence_refs)):
            raise CapabilityAuthorityContractError(
                "capability_adapter_output_evidence_duplicated"
            )
        normalized_evidence = tuple(
            sorted(normalized_evidence, key=lambda item: item.evidence_ref)
        )
        if retryability not in RETRYABILITY_STATES:
            raise CapabilityAuthorityContractError(
                "capability_adapter_output_retryability_invalid"
            )
        if failure is not None and not isinstance(failure, CapabilityFailure):
            failure = CapabilityFailure.from_dict(failure)
        if status == "succeeded":
            if not normalized_evidence or failure is not None:
                raise CapabilityAuthorityContractError(
                    "capability_adapter_output_success_invalid"
                )
            if any(item.execution_state != "available" for item in normalized_evidence):
                raise CapabilityAuthorityContractError(
                    "capability_adapter_output_success_evidence_invalid"
                )
        if status in {"integrity_failed", "technical_failed"} and failure is None:
            raise CapabilityAuthorityContractError(
                "capability_adapter_output_failure_required"
            )
        if failure is not None and failure.retryability != retryability:
            raise CapabilityAuthorityContractError(
                "capability_adapter_output_failure_retryability_mismatch"
            )
        frozen_payload = _freeze(output_payload)
        output_digest = canonical_digest(frozen_payload)
        body = {
            "status": status,
            "output_payload": frozen_payload,
            "output_digest": output_digest,
            "evidence": normalized_evidence,
            "affected_obligation_ids": _string_tuple(
                affected_obligation_ids,
                "capability_adapter_output_obligation_refs_invalid",
            ),
            "limitation_refs": _string_tuple(
                limitation_refs,
                "capability_adapter_output_limitation_refs_invalid",
            ),
            "retryability": retryability,
            "failure": failure,
        }
        return cls(content_digest=canonical_digest(body), **body)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CapabilityAdapterOutput":
        payload = _strict_shape(payload, cls, "capability_adapter_output_shape_invalid")
        rebuilt = cls.create(
            status=payload["status"],
            output_payload=payload["output_payload"],
            evidence=payload["evidence"],
            affected_obligation_ids=payload["affected_obligation_ids"],
            limitation_refs=payload["limitation_refs"],
            retryability=payload["retryability"],
            failure=payload["failure"],
        )
        if rebuilt.output_digest != payload.get("output_digest"):
            raise CapabilityAuthorityContractError(
                "capability_adapter_output_output_digest_invalid"
            )
        if rebuilt.content_digest != payload.get("content_digest"):
            raise CapabilityAuthorityContractError(
                "capability_adapter_output_digest_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class CapabilityAttempt:
    attempt_id: str
    run_attempt_id: str
    intent_revision_id: str
    plan_revision_id: str
    task_id: str
    task_idempotency_key: str
    execution_attempt: int
    normalized_input_digest: str
    release_set_digest: str
    contract_versions_digest: str
    input_digest: str
    content_digest: str

    @classmethod
    def create(
        cls,
        plan_revision: PlanRevision,
        task: CapabilityTask,
        *,
        execution_attempt: int = 1,
    ) -> "CapabilityAttempt":
        _validate_plan_task(plan_revision, task)
        return cls._from_components(
            run_attempt_id=plan_revision.run_attempt_id,
            intent_revision_id=plan_revision.intent_revision_id,
            plan_revision_id=plan_revision.plan_revision_id,
            task_id=task.task_id,
            task_idempotency_key=task.idempotency_key,
            execution_attempt=execution_attempt,
            normalized_input_digest=canonical_digest(task.normalized_input_refs),
            release_set_digest=canonical_digest(
                {"authority_context_ref": plan_revision.authority_context_ref}
            ),
            contract_versions_digest=canonical_digest(plan_revision.contract_versions),
        )

    @classmethod
    def _from_components(
        cls,
        *,
        run_attempt_id: str,
        intent_revision_id: str,
        plan_revision_id: str,
        task_id: str,
        task_idempotency_key: str,
        execution_attempt: int,
        normalized_input_digest: str,
        release_set_digest: str,
        contract_versions_digest: str,
    ) -> "CapabilityAttempt":
        body = {
            "run_attempt_id": _required_string(
                run_attempt_id, "capability_attempt_run_id_invalid"
            ),
            "intent_revision_id": _required_string(
                intent_revision_id, "capability_attempt_intent_revision_id_invalid"
            ),
            "plan_revision_id": _required_string(
                plan_revision_id, "capability_attempt_plan_revision_id_invalid"
            ),
            "task_id": _required_string(task_id, "capability_attempt_task_id_invalid"),
            "task_idempotency_key": _digest(
                task_idempotency_key,
                "capability_attempt_task_idempotency_key_invalid",
            ),
            "execution_attempt": _integer(
                execution_attempt,
                "capability_attempt_execution_attempt_invalid",
                minimum=1,
            ),
            "normalized_input_digest": _digest(
                normalized_input_digest,
                "capability_attempt_normalized_input_digest_invalid",
            ),
            "release_set_digest": _digest(
                release_set_digest, "capability_attempt_release_set_digest_invalid"
            ),
            "contract_versions_digest": _digest(
                contract_versions_digest,
                "capability_attempt_contract_versions_digest_invalid",
            ),
        }
        input_digest = canonical_digest(
            {
                key: body[key]
                for key in (
                    "plan_revision_id",
                    "task_id",
                    "task_idempotency_key",
                    "normalized_input_digest",
                    "release_set_digest",
                    "contract_versions_digest",
                )
            }
        )
        body["input_digest"] = input_digest
        attempt_seed = {
            "input_digest": input_digest,
            "execution_attempt": body["execution_attempt"],
        }
        return cls(
            attempt_id="capability-attempt-" + canonical_digest(attempt_seed)[:24],
            content_digest=canonical_digest(body),
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CapabilityAttempt":
        payload = _strict_shape(payload, cls, "capability_attempt_shape_invalid")
        rebuilt = cls._from_components(
            **{
                key: payload[key]
                for key in payload
                if key not in {"attempt_id", "input_digest", "content_digest"}
            }
        )
        if rebuilt.attempt_id != payload.get("attempt_id"):
            raise CapabilityAuthorityContractError("capability_attempt_id_invalid")
        if rebuilt.input_digest != payload.get("input_digest"):
            raise CapabilityAuthorityContractError(
                "capability_attempt_input_digest_invalid"
            )
        if rebuilt.content_digest != payload.get("content_digest"):
            raise CapabilityAuthorityContractError("capability_attempt_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class FailureRecord:
    failure_ref: str
    run_attempt_id: str
    plan_revision_id: str
    task_id: str
    attempt_id: str
    layer: str
    kind: str
    scope: str
    affected_refs: tuple[str, ...]
    integrity_level: str
    retryability: str
    user_actionable: bool
    business_boundary: str
    technical_detail_ref: str
    content_digest: str

    @classmethod
    def create(
        cls,
        attempt: CapabilityAttempt,
        failure: CapabilityFailure,
    ) -> "FailureRecord":
        if not isinstance(attempt, CapabilityAttempt) or not isinstance(
            failure, CapabilityFailure
        ):
            raise CapabilityAuthorityContractError("failure_record_input_invalid")
        return cls._from_components(
            run_attempt_id=attempt.run_attempt_id,
            plan_revision_id=attempt.plan_revision_id,
            task_id=attempt.task_id,
            attempt_id=attempt.attempt_id,
            layer=failure.layer,
            kind=failure.kind,
            scope=failure.scope,
            affected_refs=failure.affected_refs,
            integrity_level=failure.integrity_level,
            retryability=failure.retryability,
            user_actionable=failure.user_actionable,
            business_boundary=failure.business_boundary,
            technical_detail_ref=failure.technical_detail_ref,
        )

    @classmethod
    def _from_components(cls, **values: Any) -> "FailureRecord":
        layer = values.get("layer")
        scope = values.get("scope")
        integrity_level = values.get("integrity_level")
        retryability = values.get("retryability")
        if layer not in FAILURE_LAYERS:
            raise CapabilityAuthorityContractError("failure_record_layer_invalid")
        if scope not in FAILURE_SCOPES:
            raise CapabilityAuthorityContractError("failure_record_scope_invalid")
        if integrity_level not in INTEGRITY_LEVELS:
            raise CapabilityAuthorityContractError(
                "failure_record_integrity_level_invalid"
            )
        if retryability not in RETRYABILITY_STATES:
            raise CapabilityAuthorityContractError(
                "failure_record_retryability_invalid"
            )
        user_actionable = values.get("user_actionable")
        if type(user_actionable) is not bool:
            raise CapabilityAuthorityContractError(
                "failure_record_user_actionable_invalid"
            )
        body = {
            "run_attempt_id": _required_string(
                values.get("run_attempt_id"), "failure_record_run_id_invalid"
            ),
            "plan_revision_id": _required_string(
                values.get("plan_revision_id"),
                "failure_record_plan_revision_id_invalid",
            ),
            "task_id": _required_string(
                values.get("task_id"), "failure_record_task_id_invalid"
            ),
            "attempt_id": _required_string(
                values.get("attempt_id"), "failure_record_attempt_id_invalid"
            ),
            "layer": layer,
            "kind": _required_string(values.get("kind"), "failure_record_kind_invalid"),
            "scope": scope,
            "affected_refs": _string_tuple(
                values.get("affected_refs"),
                "failure_record_affected_refs_invalid",
                allow_empty=False,
            ),
            "integrity_level": integrity_level,
            "retryability": retryability,
            "user_actionable": user_actionable,
            "business_boundary": _required_string(
                values.get("business_boundary"),
                "failure_record_business_boundary_invalid",
            ),
            "technical_detail_ref": _required_string(
                values.get("technical_detail_ref"),
                "failure_record_technical_detail_ref_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            failure_ref="capability-failure-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FailureRecord":
        payload = _strict_shape(payload, cls, "failure_record_shape_invalid")
        rebuilt = cls._from_components(
            **{
                key: payload[key]
                for key in payload
                if key not in {"failure_ref", "content_digest"}
            }
        )
        if rebuilt.failure_ref != payload.get("failure_ref"):
            raise CapabilityAuthorityContractError("failure_record_ref_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise CapabilityAuthorityContractError("failure_record_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class CapabilityOutcome:
    outcome_ref: str
    run_attempt_id: str
    plan_revision_id: str
    task_id: str
    attempt_id: str
    status: str
    evidence_refs: tuple[str, ...]
    affected_obligation_ids: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    retryability: str
    failure_ref: str | None
    input_digest: str
    output_digest: str
    budget_units: int
    content_digest: str

    @classmethod
    def create(
        cls,
        attempt: CapabilityAttempt,
        task: CapabilityTask,
        adapter_output: CapabilityAdapterOutput,
        *,
        failure_ref: str | None,
        budget_units: int,
    ) -> "CapabilityOutcome":
        if not isinstance(attempt, CapabilityAttempt) or not isinstance(
            adapter_output, CapabilityAdapterOutput
        ):
            raise CapabilityAuthorityContractError("capability_outcome_input_invalid")
        if attempt.task_id != task.task_id:
            raise CapabilityAuthorityContractError("capability_outcome_task_mismatch")
        unsupported = set(adapter_output.affected_obligation_ids) - set(
            task.supports_obligation_ids
        )
        if unsupported:
            raise CapabilityAuthorityContractError(
                "capability_outcome_obligation_ref_invalid"
            )
        expected_failure = adapter_output.failure is not None
        if expected_failure != (failure_ref is not None):
            raise CapabilityAuthorityContractError(
                "capability_outcome_failure_ref_invalid"
            )
        return cls._from_components(
            run_attempt_id=attempt.run_attempt_id,
            plan_revision_id=attempt.plan_revision_id,
            task_id=attempt.task_id,
            attempt_id=attempt.attempt_id,
            status=adapter_output.status,
            evidence_refs=tuple(item.evidence_ref for item in adapter_output.evidence),
            affected_obligation_ids=adapter_output.affected_obligation_ids,
            # Outcome boundaries apply to the task as a whole. Evidence-local
            # boundaries remain on their exact evidence entry so claim settlement
            # and customer projection cannot widen them to sibling dimensions.
            limitation_refs=adapter_output.limitation_refs,
            retryability=adapter_output.retryability,
            failure_ref=failure_ref,
            input_digest=attempt.input_digest,
            output_digest=adapter_output.output_digest,
            budget_units=budget_units,
        )

    @classmethod
    def _from_components(cls, **values: Any) -> "CapabilityOutcome":
        status = values.get("status")
        retryability = values.get("retryability")
        if status not in OUTCOME_STATUSES:
            raise CapabilityAuthorityContractError("capability_outcome_status_invalid")
        if retryability not in RETRYABILITY_STATES:
            raise CapabilityAuthorityContractError(
                "capability_outcome_retryability_invalid"
            )
        failure_ref = _optional_string(
            values.get("failure_ref"), "capability_outcome_failure_ref_invalid"
        )
        if status in {"integrity_failed", "technical_failed"} and failure_ref is None:
            raise CapabilityAuthorityContractError(
                "capability_outcome_failure_ref_required"
            )
        evidence_refs = _string_tuple(
            values.get("evidence_refs"), "capability_outcome_evidence_refs_invalid"
        )
        if status == "succeeded" and not evidence_refs:
            raise CapabilityAuthorityContractError(
                "capability_outcome_success_evidence_required"
            )
        body = {
            "run_attempt_id": _required_string(
                values.get("run_attempt_id"), "capability_outcome_run_id_invalid"
            ),
            "plan_revision_id": _required_string(
                values.get("plan_revision_id"),
                "capability_outcome_plan_revision_id_invalid",
            ),
            "task_id": _required_string(
                values.get("task_id"), "capability_outcome_task_id_invalid"
            ),
            "attempt_id": _required_string(
                values.get("attempt_id"), "capability_outcome_attempt_id_invalid"
            ),
            "status": status,
            "evidence_refs": evidence_refs,
            "affected_obligation_ids": _string_tuple(
                values.get("affected_obligation_ids"),
                "capability_outcome_obligation_refs_invalid",
            ),
            "limitation_refs": _string_tuple(
                values.get("limitation_refs"),
                "capability_outcome_limitation_refs_invalid",
            ),
            "retryability": retryability,
            "failure_ref": failure_ref,
            "input_digest": _digest(
                values.get("input_digest"), "capability_outcome_input_digest_invalid"
            ),
            "output_digest": _digest(
                values.get("output_digest"),
                "capability_outcome_output_digest_invalid",
            ),
            "budget_units": _integer(
                values.get("budget_units"),
                "capability_outcome_budget_units_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            outcome_ref="capability-outcome-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CapabilityOutcome":
        payload = _strict_shape(payload, cls, "capability_outcome_shape_invalid")
        rebuilt = cls._from_components(
            **{
                key: payload[key]
                for key in payload
                if key not in {"outcome_ref", "content_digest"}
            }
        )
        if rebuilt.outcome_ref != payload.get("outcome_ref"):
            raise CapabilityAuthorityContractError("capability_outcome_ref_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise CapabilityAuthorityContractError("capability_outcome_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class EvidenceLedgerEntry:
    entry_ref: str
    run_attempt_id: str
    authority_context_ref: str
    plan_revision_id: str
    task_id: str
    outcome_ref: str
    evidence_ref: str
    binding_record_ref: str | None
    execution_state: str
    evidence_kind: str
    data_contract_state: str
    supported_claim_kinds: tuple[str, ...]
    evidence_strength: str
    maximum_claim_strength: str
    observation_facts: tuple[Mapping[str, Any], ...]
    scope: str
    window_refs: tuple[str, ...]
    dimension_path: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    result_refs: tuple[str, ...]
    completeness_report_refs: tuple[str, ...]
    hierarchy_qualified: bool
    result_membership_digest: str
    completeness_membership_digest: str
    content_digest: str

    @classmethod
    def create(
        cls,
        plan_revision: PlanRevision,
        task: CapabilityTask,
        outcome: CapabilityOutcome,
        evidence: CapabilityEvidence,
    ) -> "EvidenceLedgerEntry":
        _validate_plan_task(plan_revision, task)
        if outcome.plan_revision_id != plan_revision.plan_revision_id:
            raise CapabilityAuthorityContractError(
                "evidence_ledger_outcome_plan_mismatch"
            )
        if (
            outcome.task_id != task.task_id
            or evidence.evidence_ref not in outcome.evidence_refs
        ):
            raise CapabilityAuthorityContractError(
                "evidence_ledger_outcome_evidence_mismatch"
            )
        return cls._from_components(
            run_attempt_id=plan_revision.run_attempt_id,
            authority_context_ref=plan_revision.authority_context_ref,
            plan_revision_id=plan_revision.plan_revision_id,
            task_id=task.task_id,
            outcome_ref=outcome.outcome_ref,
            evidence_ref=evidence.evidence_ref,
            binding_record_ref=evidence.binding_record_ref,
            execution_state=evidence.execution_state,
            evidence_kind=evidence.evidence_kind,
            data_contract_state=evidence.data_contract_state,
            supported_claim_kinds=evidence.supported_claim_kinds,
            evidence_strength=evidence.evidence_strength,
            maximum_claim_strength=evidence.maximum_claim_strength,
            observation_facts=evidence.observation_facts,
            scope=evidence.scope,
            window_refs=evidence.window_refs,
            dimension_path=evidence.dimension_path,
            limitation_refs=evidence.limitation_refs,
            result_refs=evidence.result_refs,
            completeness_report_refs=evidence.completeness_report_refs,
            hierarchy_qualified=evidence.hierarchy_qualified,
        )

    @classmethod
    def _from_components(cls, **values: Any) -> "EvidenceLedgerEntry":
        execution_state = values.get("execution_state")
        evidence_kind = values.get("evidence_kind")
        hierarchy_qualified = values.get("hierarchy_qualified")
        if execution_state not in EXECUTION_STATES:
            raise CapabilityAuthorityContractError(
                "evidence_ledger_execution_state_invalid"
            )
        if evidence_kind not in EVIDENCE_KINDS:
            raise CapabilityAuthorityContractError("evidence_ledger_kind_invalid")
        if type(hierarchy_qualified) is not bool:
            raise CapabilityAuthorityContractError(
                "evidence_ledger_hierarchy_qualified_invalid"
            )
        dimensions = _string_tuple(
            values.get("dimension_path"),
            "evidence_ledger_dimension_path_invalid",
            sort=False,
        )
        if hierarchy_qualified and not dimensions:
            raise CapabilityAuthorityContractError(
                "evidence_ledger_qualified_hierarchy_missing"
            )
        result_refs = _string_tuple(
            values.get("result_refs"), "evidence_ledger_result_refs_invalid"
        )
        completeness_refs = _string_tuple(
            values.get("completeness_report_refs"),
            "evidence_ledger_completeness_refs_invalid",
        )
        body = {
            "run_attempt_id": _required_string(
                values.get("run_attempt_id"), "evidence_ledger_run_id_invalid"
            ),
            "authority_context_ref": _required_string(
                values.get("authority_context_ref"),
                "evidence_ledger_authority_context_ref_invalid",
            ),
            "plan_revision_id": _required_string(
                values.get("plan_revision_id"),
                "evidence_ledger_plan_revision_id_invalid",
            ),
            "task_id": _required_string(
                values.get("task_id"), "evidence_ledger_task_id_invalid"
            ),
            "outcome_ref": _required_string(
                values.get("outcome_ref"), "evidence_ledger_outcome_ref_invalid"
            ),
            "evidence_ref": _required_string(
                values.get("evidence_ref"), "evidence_ledger_evidence_ref_invalid"
            ),
            "binding_record_ref": _optional_string(
                values.get("binding_record_ref"),
                "evidence_ledger_binding_ref_invalid",
            ),
            "execution_state": execution_state,
            "evidence_kind": evidence_kind,
            "data_contract_state": _required_string(
                values.get("data_contract_state"),
                "evidence_ledger_data_contract_state_invalid",
            ),
            "supported_claim_kinds": _string_tuple(
                values.get("supported_claim_kinds"),
                "evidence_ledger_claim_kinds_invalid",
            ),
            "evidence_strength": _required_string(
                values.get("evidence_strength"),
                "evidence_ledger_evidence_strength_invalid",
            ),
            "maximum_claim_strength": _required_string(
                values.get("maximum_claim_strength"),
                "evidence_ledger_claim_strength_invalid",
            ),
            "observation_facts": _mapping_tuple(
                values.get("observation_facts"),
                "evidence_ledger_observation_facts_invalid",
            ),
            "scope": _required_string(
                values.get("scope"), "evidence_ledger_scope_invalid"
            ),
            "window_refs": _string_tuple(
                values.get("window_refs"), "evidence_ledger_window_refs_invalid"
            ),
            "dimension_path": dimensions,
            "limitation_refs": _string_tuple(
                values.get("limitation_refs"),
                "evidence_ledger_limitation_refs_invalid",
            ),
            "result_refs": result_refs,
            "completeness_report_refs": completeness_refs,
            "hierarchy_qualified": hierarchy_qualified,
            "result_membership_digest": canonical_digest(result_refs),
            "completeness_membership_digest": canonical_digest(completeness_refs),
        }
        digest = canonical_digest(body)
        return cls(
            entry_ref="evidence-ledger-entry-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceLedgerEntry":
        payload = _strict_shape(payload, cls, "evidence_ledger_shape_invalid")
        rebuilt = cls._from_components(
            **{
                key: payload[key]
                for key in payload
                if key
                not in {
                    "entry_ref",
                    "result_membership_digest",
                    "completeness_membership_digest",
                    "content_digest",
                }
            }
        )
        for field, error in (
            ("entry_ref", "evidence_ledger_entry_ref_invalid"),
            ("result_membership_digest", "evidence_ledger_result_digest_invalid"),
            (
                "completeness_membership_digest",
                "evidence_ledger_completeness_digest_invalid",
            ),
            ("content_digest", "evidence_ledger_digest_invalid"),
        ):
            if getattr(rebuilt, field) != payload.get(field):
                raise CapabilityAuthorityContractError(error)
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def _stop_policy_decision(
    *,
    plan_revision: PlanRevision,
    outcomes: Sequence[CapabilityOutcome],
    reason: str,
    hard_budget_limit: int | None,
) -> Mapping[str, Any]:
    evaluated_task_ids = {item.task_id for item in outcomes}
    remaining = tuple(
        sorted(
            (
                task
                for task in plan_revision.capability_tasks
                if task.task_id not in evaluated_task_ids
            ),
            key=lambda task: (task.execution_rank, task.task_id),
        )
    )
    used_budget = sum(item.budget_units for item in outcomes)
    budget_state = (
        "not_limited"
        if hard_budget_limit is None
        else "exhausted"
        if used_budget >= hard_budget_limit
        else "available"
    )
    if reason == "plan_exhausted":
        decision = {
            "required_obligations": "all_tasks_evaluated",
            "remaining_materiality": "no_unevaluated_tasks",
            "next_information_gain": "no_eligible_task",
            "actionability": "no_remaining_task",
            "statistical_risk": "contract_bounded",
            "budget": budget_state,
            "next_task_id": None,
        }
    elif reason == "hard_budget_reached":
        decision = {
            "required_obligations": (
                "unevaluated_required_tasks_remain"
                if any(
                    any(bool(edge["required"]) for edge in task.obligation_edges)
                    for task in remaining
                )
                else "all_tasks_evaluated"
            ),
            "remaining_materiality": "unevaluated_tasks_remain",
            "next_information_gain": "eligible_but_budget_blocked",
            "actionability": "not_evaluated",
            "statistical_risk": (
                "multiplicity_sensitive_tasks_remain"
                if any(
                    task.governor_inputs["statistical_risk"] == "multiplicity_sensitive"
                    for task in remaining
                )
                else "contract_bounded"
            ),
            "budget": "exhausted",
            "next_task_id": remaining[0].task_id if remaining else None,
        }
    elif reason == "no_ready_tasks":
        decision = {
            "required_obligations": (
                "unevaluated_required_tasks_remain"
                if any(
                    any(bool(edge["required"]) for edge in task.obligation_edges)
                    for task in remaining
                )
                else "all_tasks_evaluated"
            ),
            "remaining_materiality": "unevaluated_tasks_remain",
            "next_information_gain": "blocked_by_dependency",
            "actionability": "not_evaluated",
            "statistical_risk": (
                "multiplicity_sensitive_tasks_remain"
                if any(
                    task.governor_inputs["statistical_risk"] == "multiplicity_sensitive"
                    for task in remaining
                )
                else "contract_bounded"
            ),
            "budget": budget_state,
            "next_task_id": remaining[0].task_id if remaining else None,
        }
    elif reason == "shared_authority_failure":
        decision = {
            "required_obligations": "blocked_by_shared_failure",
            "remaining_materiality": "not_evaluated_after_failure",
            "next_information_gain": "blocked_by_shared_failure",
            "actionability": "blocked_by_shared_failure",
            "statistical_risk": "not_evaluated_after_failure",
            "budget": budget_state,
            "next_task_id": None,
        }
    else:
        raise CapabilityAuthorityContractError("exploration_stop_reason_invalid")
    return _validated_stop_policy_decision(decision)


def _validated_stop_policy_decision(value: Any) -> Mapping[str, Any]:
    expected = {*_STOP_POLICY_STATES, "next_task_id"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise CapabilityAuthorityContractError(
            "exploration_stop_policy_decision_invalid"
        )
    normalized = {
        key: _required_string(
            value.get(key), "exploration_stop_policy_decision_invalid"
        )
        for key in _STOP_POLICY_STATES
    }
    if any(
        normalized[key] not in allowed for key, allowed in _STOP_POLICY_STATES.items()
    ):
        raise CapabilityAuthorityContractError(
            "exploration_stop_policy_decision_invalid"
        )
    normalized["next_task_id"] = _optional_string(
        value.get("next_task_id"),
        "exploration_stop_policy_decision_invalid",
    )
    return _freeze(normalized)


@dataclass(frozen=True)
class ExplorationStopRecord:
    stop_ref: str
    run_attempt_id: str
    plan_revision_id: str
    evaluated_outcome_refs: tuple[str, ...]
    evaluated_outcome_set_digest: str
    budget_policy_ref: str
    reason: str
    used_budget_units: int
    hard_budget_limit: int | None
    policy_decision: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        plan_revision: PlanRevision,
        outcomes: Sequence[CapabilityOutcome],
        *,
        reason: str,
        hard_budget_limit: int | None,
    ) -> "ExplorationStopRecord":
        if not isinstance(plan_revision, PlanRevision):
            raise CapabilityAuthorityContractError("exploration_stop_plan_invalid")
        normalized_outcomes = _validated_plan_outcomes(plan_revision, outcomes)
        task_ids = {item.task_id for item in normalized_outcomes}
        all_task_ids = {item.task_id for item in plan_revision.capability_tasks}
        used = sum(item.budget_units for item in normalized_outcomes)
        if reason == "plan_exhausted" and task_ids != all_task_ids:
            raise CapabilityAuthorityContractError(
                "exploration_stop_plan_not_exhausted"
            )
        if reason == "hard_budget_reached" and (
            hard_budget_limit is None or used < hard_budget_limit
        ):
            raise CapabilityAuthorityContractError(
                "exploration_stop_budget_not_reached"
            )
        return cls._from_components(
            run_attempt_id=plan_revision.run_attempt_id,
            plan_revision_id=plan_revision.plan_revision_id,
            evaluated_outcome_refs=tuple(
                item.outcome_ref for item in normalized_outcomes
            ),
            budget_policy_ref=plan_revision.budget_policy_ref,
            reason=reason,
            used_budget_units=used,
            hard_budget_limit=hard_budget_limit,
            policy_decision=_stop_policy_decision(
                plan_revision=plan_revision,
                outcomes=normalized_outcomes,
                reason=reason,
                hard_budget_limit=hard_budget_limit,
            ),
        )

    @classmethod
    def _from_components(cls, **values: Any) -> "ExplorationStopRecord":
        reason = values.get("reason")
        if reason not in STOP_REASONS:
            raise CapabilityAuthorityContractError("exploration_stop_reason_invalid")
        hard_budget_limit = values.get("hard_budget_limit")
        if hard_budget_limit is not None:
            hard_budget_limit = _integer(
                hard_budget_limit, "exploration_stop_budget_limit_invalid"
            )
        used = _integer(
            values.get("used_budget_units"),
            "exploration_stop_budget_used_invalid",
        )
        if reason == "hard_budget_reached" and (
            hard_budget_limit is None or used < hard_budget_limit
        ):
            raise CapabilityAuthorityContractError(
                "exploration_stop_budget_not_reached"
            )
        outcome_refs = _string_tuple(
            values.get("evaluated_outcome_refs"),
            "exploration_stop_outcome_refs_invalid",
        )
        policy_decision = _validated_stop_policy_decision(values.get("policy_decision"))
        body = {
            "run_attempt_id": _required_string(
                values.get("run_attempt_id"), "exploration_stop_run_id_invalid"
            ),
            "plan_revision_id": _required_string(
                values.get("plan_revision_id"),
                "exploration_stop_plan_revision_id_invalid",
            ),
            "evaluated_outcome_refs": outcome_refs,
            "evaluated_outcome_set_digest": canonical_digest(outcome_refs),
            "budget_policy_ref": _required_string(
                values.get("budget_policy_ref"),
                "exploration_stop_budget_policy_ref_invalid",
            ),
            "reason": reason,
            "used_budget_units": used,
            "hard_budget_limit": hard_budget_limit,
            "policy_decision": policy_decision,
        }
        digest = canonical_digest(body)
        return cls(
            stop_ref="exploration-stop-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExplorationStopRecord":
        payload = _strict_shape(payload, cls, "exploration_stop_shape_invalid")
        rebuilt = cls._from_components(
            **{
                key: payload[key]
                for key in payload
                if key
                not in {
                    "stop_ref",
                    "evaluated_outcome_set_digest",
                    "content_digest",
                }
            }
        )
        for field, error in (
            ("stop_ref", "exploration_stop_ref_invalid"),
            (
                "evaluated_outcome_set_digest",
                "exploration_stop_outcome_digest_invalid",
            ),
            ("content_digest", "exploration_stop_digest_invalid"),
        ):
            if getattr(rebuilt, field) != payload.get(field):
                raise CapabilityAuthorityContractError(error)
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ExecutionSnapshot:
    execution_snapshot_ref: str
    run_attempt_id: str
    authority_context_ref: str
    plan_revision_id: str
    stop_ref: str
    outcome_refs: tuple[str, ...]
    failure_refs: tuple[str, ...]
    evidence_entry_refs: tuple[str, ...]
    outcome_set_digest: str
    failure_set_digest: str
    evidence_ledger_digest: str
    content_digest: str

    @classmethod
    def create(
        cls,
        plan_revision: PlanRevision,
        stop_record: ExplorationStopRecord,
        outcomes: Sequence[CapabilityOutcome],
        evidence_entries: Sequence[EvidenceLedgerEntry],
        failures: Sequence[FailureRecord],
    ) -> "ExecutionSnapshot":
        normalized_outcomes = _validated_plan_outcomes(plan_revision, outcomes)
        if stop_record.plan_revision_id != plan_revision.plan_revision_id:
            raise CapabilityAuthorityContractError(
                "execution_snapshot_stop_plan_mismatch"
            )
        outcome_refs = tuple(sorted(item.outcome_ref for item in normalized_outcomes))
        if outcome_refs != stop_record.evaluated_outcome_refs:
            raise CapabilityAuthorityContractError(
                "execution_snapshot_stop_outcome_mismatch"
            )
        normalized_evidence = tuple(
            sorted(evidence_entries, key=lambda item: item.entry_ref)
        )
        normalized_failures = tuple(sorted(failures, key=lambda item: item.failure_ref))
        if len({item.entry_ref for item in normalized_evidence}) != len(
            normalized_evidence
        ):
            raise CapabilityAuthorityContractError(
                "execution_snapshot_evidence_duplicated"
            )
        if len({item.failure_ref for item in normalized_failures}) != len(
            normalized_failures
        ):
            raise CapabilityAuthorityContractError(
                "execution_snapshot_failure_duplicated"
            )
        outcome_by_ref = {item.outcome_ref: item for item in normalized_outcomes}
        if any(
            item.plan_revision_id != plan_revision.plan_revision_id
            or item.outcome_ref not in outcome_by_ref
            for item in normalized_evidence
        ):
            raise CapabilityAuthorityContractError(
                "execution_snapshot_evidence_closure_invalid"
            )
        failure_by_ref = {item.failure_ref: item for item in normalized_failures}
        if any(
            item.plan_revision_id != plan_revision.plan_revision_id
            for item in normalized_failures
        ):
            raise CapabilityAuthorityContractError(
                "execution_snapshot_failure_closure_invalid"
            )
        for outcome in normalized_outcomes:
            ledger_refs = {
                item.evidence_ref
                for item in normalized_evidence
                if item.outcome_ref == outcome.outcome_ref
            }
            if ledger_refs != set(outcome.evidence_refs):
                raise CapabilityAuthorityContractError(
                    "execution_snapshot_evidence_membership_invalid"
                )
            if (
                outcome.failure_ref is not None
                and outcome.failure_ref not in failure_by_ref
            ):
                raise CapabilityAuthorityContractError(
                    "execution_snapshot_failure_membership_invalid"
                )
        return cls._from_components(
            run_attempt_id=plan_revision.run_attempt_id,
            authority_context_ref=plan_revision.authority_context_ref,
            plan_revision_id=plan_revision.plan_revision_id,
            stop_ref=stop_record.stop_ref,
            outcome_refs=outcome_refs,
            failure_refs=tuple(item.failure_ref for item in normalized_failures),
            evidence_entry_refs=tuple(item.entry_ref for item in normalized_evidence),
        )

    @classmethod
    def _from_components(cls, **values: Any) -> "ExecutionSnapshot":
        outcome_refs = _string_tuple(
            values.get("outcome_refs"), "execution_snapshot_outcome_refs_invalid"
        )
        failure_refs = _string_tuple(
            values.get("failure_refs"), "execution_snapshot_failure_refs_invalid"
        )
        evidence_refs = _string_tuple(
            values.get("evidence_entry_refs"),
            "execution_snapshot_evidence_refs_invalid",
        )
        body = {
            "run_attempt_id": _required_string(
                values.get("run_attempt_id"), "execution_snapshot_run_id_invalid"
            ),
            "authority_context_ref": _required_string(
                values.get("authority_context_ref"),
                "execution_snapshot_authority_context_ref_invalid",
            ),
            "plan_revision_id": _required_string(
                values.get("plan_revision_id"),
                "execution_snapshot_plan_revision_id_invalid",
            ),
            "stop_ref": _required_string(
                values.get("stop_ref"), "execution_snapshot_stop_ref_invalid"
            ),
            "outcome_refs": outcome_refs,
            "failure_refs": failure_refs,
            "evidence_entry_refs": evidence_refs,
            "outcome_set_digest": canonical_digest(outcome_refs),
            "failure_set_digest": canonical_digest(failure_refs),
            "evidence_ledger_digest": canonical_digest(evidence_refs),
        }
        digest = canonical_digest(body)
        return cls(
            execution_snapshot_ref="capability-execution-snapshot-" + digest[:24],
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionSnapshot":
        payload = _strict_shape(payload, cls, "execution_snapshot_shape_invalid")
        rebuilt = cls._from_components(
            **{
                key: payload[key]
                for key in payload
                if key
                not in {
                    "execution_snapshot_ref",
                    "outcome_set_digest",
                    "failure_set_digest",
                    "evidence_ledger_digest",
                    "content_digest",
                }
            }
        )
        for field, error in (
            ("execution_snapshot_ref", "execution_snapshot_ref_invalid"),
            ("outcome_set_digest", "execution_snapshot_outcome_digest_invalid"),
            ("failure_set_digest", "execution_snapshot_failure_digest_invalid"),
            (
                "evidence_ledger_digest",
                "execution_snapshot_evidence_digest_invalid",
            ),
            ("content_digest", "execution_snapshot_digest_invalid"),
        ):
            if getattr(rebuilt, field) != payload.get(field):
                raise CapabilityAuthorityContractError(error)
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


CapabilityOutcomeBundle = tuple[
    CapabilityAttempt,
    CapabilityOutcome,
    tuple[EvidenceLedgerEntry, ...],
    tuple[FailureRecord, ...],
]


@runtime_checkable
class CapabilityExecutionStore(Protocol):
    def load_capability_outcome(
        self,
        plan_revision_id: str,
        task_id: str,
    ) -> CapabilityOutcomeBundle | None: ...

    def accept_capability_outcome(
        self,
        attempt: CapabilityAttempt,
        outcome: CapabilityOutcome,
        evidence_entries: Sequence[EvidenceLedgerEntry],
        failures: Sequence[FailureRecord],
        settlement_authority: "CapabilitySettlementAuthority",
    ) -> CapabilityOutcomeBundle: ...

    def load_execution_snapshot(
        self,
        plan_revision_id: str,
    ) -> ExecutionSnapshot | None: ...

    def accept_execution_settlement(
        self,
        snapshot: ExecutionSnapshot,
        stop_record: ExplorationStopRecord,
        transition: DurableTransition,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        accepted_attempt_refs: Sequence[str],
    ) -> ExecutionSnapshot: ...


def _validate_plan_task(plan_revision: PlanRevision, task: CapabilityTask) -> None:
    if not isinstance(plan_revision, PlanRevision) or not isinstance(
        task, CapabilityTask
    ):
        raise CapabilityAuthorityContractError("capability_plan_task_type_invalid")
    matching = tuple(
        item for item in plan_revision.capability_tasks if item.task_id == task.task_id
    )
    if len(matching) != 1 or matching[0] != task:
        raise CapabilityAuthorityContractError("capability_plan_task_ref_invalid")
    if task.plan_revision_id != plan_revision.plan_revision_id:
        raise CapabilityAuthorityContractError("capability_plan_task_plan_mismatch")


def _validated_plan_outcomes(
    plan_revision: PlanRevision,
    outcomes: Sequence[CapabilityOutcome],
) -> tuple[CapabilityOutcome, ...]:
    if isinstance(outcomes, (str, bytes)) or not isinstance(outcomes, Sequence):
        raise CapabilityAuthorityContractError("capability_plan_outcomes_invalid")
    normalized = tuple(sorted(outcomes, key=lambda item: item.task_id))
    if any(not isinstance(item, CapabilityOutcome) for item in normalized):
        raise CapabilityAuthorityContractError("capability_plan_outcomes_invalid")
    if len({item.task_id for item in normalized}) != len(normalized):
        raise CapabilityAuthorityContractError("capability_plan_outcome_duplicated")
    known_tasks = {item.task_id for item in plan_revision.capability_tasks}
    if any(
        item.plan_revision_id != plan_revision.plan_revision_id
        or item.run_attempt_id != plan_revision.run_attempt_id
        or item.task_id not in known_tasks
        for item in normalized
    ):
        raise CapabilityAuthorityContractError(
            "capability_plan_outcome_closure_invalid"
        )
    return normalized
