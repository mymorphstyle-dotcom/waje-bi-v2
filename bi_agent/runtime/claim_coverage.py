from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
    validate_typed_authoritative_execution_result,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.evidence_taxonomy import (
    NON_PUBLISHABLE_EVIDENCE_TYPES,
    publication_evidence_kind,
)
from bi_agent.runtime.claim_authority import ClaimPublicationCeiling
from bi_agent.runtime.claim_settlement import (
    admissible_evidence_publication_ceiling,
    evidence_publication_ceiling,
    publication_ceiling_satisfies,
)
from bi_agent.runtime.llm_client import (
    LLMOutputError,
    parse_llm_structured_response_content,
)
from bi_agent.runtime.plan_authority import (
    CAPABILITY_TASK_DECLARED_BUDGET_UNITS,
    AuthorityContext,
    ClaimObligation,
    PlanRevision,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.single_authority import DurableTransition


CLAIM_COVERAGE_SCHEMA_VERSION = "claim-coverage-evaluation.v2"
PLAN_EXPANSION_DECISION_SCHEMA_VERSION = "plan-expansion-decision.v2"
PLAN_PATCH_SCHEMA_VERSION = "plan-patch.v2"
CLAIM_COVERAGE_CHECKPOINT_SCHEMA_VERSION = "claim-coverage-checkpoint.v1"
PLAN_EXPANSION_PROVIDER_TASK = "claim_coverage_expansion_decision"

_COVERAGE_STATES = frozenset({"uncovered", "evidence_present", "explicit_boundary"})
_DECISIONS = frozenset({"seal", "patch"})
_DECISION_AUTHORITIES = frozenset({"provider", "deterministic_no_admissible_route"})
_PLAN_AXIS_ROLE_PRIORITY = MappingProxyType(
    {"conditional": 0, "auxiliary": 1, "disclosure": 2, "required": 3}
)
_PROVIDER_OUTPUT_FIELDS = frozenset({"decision", "selected_axis_ids"})
_PROVIDER_AUDIT_FIELDS = frozenset(
    {
        "task",
        "provider",
        "model",
        "prompt_version",
        "raw_response_content",
        "structured_output",
    }
)
_ROUTE_EXPECTED_VALUE_VALUES = MappingProxyType(
    {
        "expected_information_gain": frozenset(
            {"obligation_closing", "hypothesis_testing"}
        ),
        "materiality": frozenset({"user_required", "analyst_auxiliary"}),
        "actionability": frozenset({"decision_supporting", "explanation_supporting"}),
        "statistical_risk": frozenset({"contract_bounded", "multiplicity_sensitive"}),
    }
)


class ClaimCoverageContractError(ValueError):
    pass


@dataclass(frozen=True)
class ClaimEvidenceCoverageAssessment:
    assessment_ref: str
    evidence_entry_ref: str
    settlement_outcome_ref: str
    binding_record_ref: str | None
    evidence_kind: str
    evidence_strength: str
    maximum_claim_strength: str
    publication_ceiling: Mapping[str, str]
    data_contract_state: str
    supported_claim_kinds: tuple[str, ...]
    observation_facts: tuple[Mapping[str, Any], ...]
    scope: str
    window_refs: tuple[str, ...]
    dimension_path: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    result_refs: tuple[str, ...]
    completeness_report_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        evidence_entry_ref: str,
        settlement_outcome_ref: str,
        binding_record_ref: str | None,
        evidence_kind: str,
        evidence_strength: str,
        maximum_claim_strength: str,
        publication_ceiling: Mapping[str, Any],
        data_contract_state: str,
        supported_claim_kinds: Sequence[str],
        observation_facts: Sequence[Mapping[str, Any]],
        scope: str,
        window_refs: Sequence[str],
        dimension_path: Sequence[str],
        limitation_refs: Sequence[str],
        result_refs: Sequence[str],
        completeness_report_refs: Sequence[str],
    ) -> "ClaimEvidenceCoverageAssessment":
        ceiling = ClaimPublicationCeiling.from_dict(publication_ceiling)
        body = {
            "evidence_entry_ref": _required_string(
                evidence_entry_ref,
                "claim_coverage_assessment_evidence_ref_invalid",
            ),
            "settlement_outcome_ref": _required_string(
                settlement_outcome_ref,
                "claim_coverage_assessment_outcome_ref_invalid",
            ),
            "binding_record_ref": _optional_string(
                binding_record_ref,
                "claim_coverage_assessment_binding_ref_invalid",
            ),
            "evidence_kind": _required_string(
                evidence_kind,
                "claim_coverage_assessment_evidence_kind_invalid",
            ),
            "evidence_strength": _required_string(
                evidence_strength,
                "claim_coverage_assessment_evidence_strength_invalid",
            ),
            "maximum_claim_strength": _required_string(
                maximum_claim_strength,
                "claim_coverage_assessment_claim_strength_invalid",
            ),
            "publication_ceiling": _freeze(ceiling.to_dict()),
            "data_contract_state": _required_string(
                data_contract_state,
                "claim_coverage_assessment_data_contract_state_invalid",
            ),
            "supported_claim_kinds": _string_tuple(
                supported_claim_kinds,
                "claim_coverage_assessment_claim_kinds_invalid",
                allow_empty=False,
            ),
            "observation_facts": _mapping_tuple(
                observation_facts,
                "claim_coverage_assessment_observation_facts_invalid",
                allow_empty=False,
            ),
            "scope": _required_string(scope, "claim_coverage_assessment_scope_invalid"),
            "window_refs": _string_tuple(
                window_refs,
                "claim_coverage_assessment_window_refs_invalid",
                allow_empty=False,
            ),
            "dimension_path": _ordered_string_tuple(
                dimension_path,
                "claim_coverage_assessment_dimension_path_invalid",
            ),
            "limitation_refs": _string_tuple(
                limitation_refs,
                "claim_coverage_assessment_limitation_refs_invalid",
            ),
            "result_refs": _string_tuple(
                result_refs,
                "claim_coverage_assessment_result_refs_invalid",
                allow_empty=False,
            ),
            "completeness_report_refs": _string_tuple(
                completeness_report_refs,
                "claim_coverage_assessment_completeness_refs_invalid",
                allow_empty=False,
            ),
        }
        digest = canonical_digest(body)
        return cls(
            assessment_ref="claim-evidence-assessment:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClaimEvidenceCoverageAssessment":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise ClaimCoverageContractError("claim_coverage_assessment_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in payload
                if key not in {"assessment_ref", "content_digest"}
            }
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimCoverageContractError(
                "claim_coverage_assessment_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class ClaimObligationCoverage:
    obligation_coverage_ref: str
    obligation_id: str
    claim_kind: str
    role: str
    subject: Mapping[str, Any]
    success_policy: Mapping[str, Any]
    required_claim_strength: str
    status: str
    evidence_assessments: tuple[ClaimEvidenceCoverageAssessment, ...]
    evidence_entry_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        obligation_id: str,
        claim_kind: str,
        role: str,
        subject: Mapping[str, Any],
        success_policy: Mapping[str, Any],
        status: str,
        evidence_assessments: Sequence[ClaimEvidenceCoverageAssessment],
    ) -> "ClaimObligationCoverage":
        if status not in _COVERAGE_STATES:
            raise ClaimCoverageContractError("claim_coverage_status_invalid")
        if not isinstance(subject, Mapping) or not subject:
            raise ClaimCoverageContractError("claim_coverage_subject_invalid")
        if not isinstance(success_policy, Mapping):
            raise ClaimCoverageContractError("claim_coverage_success_policy_invalid")
        policy = _freeze(success_policy)
        if policy.get("policy") != "verified_or_explicit_boundary":
            raise ClaimCoverageContractError(
                "claim_coverage_success_policy_unsupported"
            )
        required_strength = _required_string(
            policy.get("minimum_claim_strength"),
            "claim_coverage_required_claim_strength_invalid",
        )
        assessments = tuple(
            sorted(
                (_replay_assessment(item) for item in evidence_assessments),
                key=lambda item: item.evidence_entry_ref,
            )
        )
        evidence_refs = tuple(item.evidence_entry_ref for item in assessments)
        if len(evidence_refs) != len(set(evidence_refs)):
            raise ClaimCoverageContractError("claim_coverage_evidence_refs_invalid")
        if any(
            claim_kind not in assessment.supported_claim_kinds
            for assessment in assessments
        ):
            raise ClaimCoverageContractError(
                "claim_coverage_assessment_claim_kind_mismatch"
            )
        boundary_satisfied = _explicit_boundary_satisfied(
            assessments,
            required_strength=required_strength,
        )
        if status == "uncovered" and assessments:
            raise ClaimCoverageContractError(
                "claim_coverage_uncovered_evidence_present"
            )
        if status == "evidence_present" and (not assessments or boundary_satisfied):
            raise ClaimCoverageContractError(
                "claim_coverage_evidence_present_state_invalid"
            )
        if status == "explicit_boundary" and not boundary_satisfied:
            raise ClaimCoverageContractError("claim_coverage_explicit_boundary_invalid")
        body = {
            "obligation_id": _required_string(
                obligation_id, "claim_coverage_obligation_id_invalid"
            ),
            "claim_kind": _required_string(
                claim_kind, "claim_coverage_claim_kind_invalid"
            ),
            "role": _required_string(role, "claim_coverage_role_invalid"),
            "subject": _freeze(subject),
            "success_policy": policy,
            "required_claim_strength": required_strength,
            "status": status,
            "evidence_assessments": assessments,
            "evidence_entry_refs": evidence_refs,
        }
        digest = canonical_digest(body)
        return cls(
            obligation_coverage_ref=("claim-obligation-coverage:sha256:" + digest),
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClaimObligationCoverage":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise ClaimCoverageContractError("claim_coverage_shape_invalid")
        rebuilt = cls.create(
            obligation_id=payload["obligation_id"],
            claim_kind=payload["claim_kind"],
            role=payload["role"],
            subject=payload["subject"],
            success_policy=payload["success_policy"],
            status=payload["status"],
            evidence_assessments=tuple(
                ClaimEvidenceCoverageAssessment.from_dict(item)
                for item in payload["evidence_assessments"]
            ),
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimCoverageContractError("claim_coverage_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class AdmissibleEvidenceRoute:
    evidence_route_ref: str
    obligation_id: str
    claim_kind: str
    capability_id: str
    evidence_kind: str
    maximum_claim_strength: str
    publication_ceiling: Mapping[str, str]
    required_claim_strength: str
    capability_contract_ref: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        obligation_id: str,
        claim_kind: str,
        capability_id: str,
        evidence_kind: str,
        maximum_claim_strength: str,
        publication_ceiling: Mapping[str, Any],
        required_claim_strength: str,
        capability_contract_ref: str,
    ) -> "AdmissibleEvidenceRoute":
        ceiling = ClaimPublicationCeiling.from_dict(publication_ceiling)
        required = _required_string(
            required_claim_strength,
            "claim_coverage_route_required_strength_invalid",
        )
        if not publication_ceiling_satisfies(ceiling, required_strength=required):
            raise ClaimCoverageContractError(
                "claim_coverage_route_ceiling_insufficient"
            )
        body = {
            "obligation_id": _required_string(
                obligation_id,
                "claim_coverage_route_obligation_id_invalid",
            ),
            "claim_kind": _required_string(
                claim_kind, "claim_coverage_route_claim_kind_invalid"
            ),
            "capability_id": _required_string(
                capability_id,
                "claim_coverage_route_capability_id_invalid",
            ),
            "evidence_kind": _required_string(
                evidence_kind,
                "claim_coverage_route_evidence_kind_invalid",
            ),
            "maximum_claim_strength": _required_string(
                maximum_claim_strength,
                "claim_coverage_route_maximum_strength_invalid",
            ),
            "publication_ceiling": _freeze(ceiling.to_dict()),
            "required_claim_strength": required,
            "capability_contract_ref": _required_string(
                capability_contract_ref,
                "claim_coverage_route_contract_ref_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            evidence_route_ref="admissible-evidence-route:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AdmissibleEvidenceRoute":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise ClaimCoverageContractError(
                "claim_coverage_evidence_route_shape_invalid"
            )
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in payload
                if key not in {"evidence_route_ref", "content_digest"}
            }
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimCoverageContractError(
                "claim_coverage_evidence_route_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class AdmissibleAxisRoute:
    route_ref: str
    axis_id: str
    business_name: str
    semantics: str
    selection_policy: str
    supported_obligation_ids: tuple[str, ...]
    supported_claim_kinds: tuple[str, ...]
    capability_ids: tuple[str, ...]
    incremental_capability_ids: tuple[str, ...]
    protected_incremental_capability_ids: tuple[str, ...]
    auxiliary_incremental_capability_ids: tuple[str, ...]
    evidence_kinds: tuple[str, ...]
    evidence_routes: tuple[AdmissibleEvidenceRoute, ...]
    maximum_claim_strength_by_obligation: Mapping[str, str]
    expected_value_projection: Mapping[str, str]
    estimated_budget_units: int
    estimated_auxiliary_budget_units: int
    remaining_auxiliary_budget_units: int | None
    contract_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        axis_id: str,
        business_name: str,
        semantics: str,
        selection_policy: str,
        supported_obligation_ids: Sequence[str],
        supported_claim_kinds: Sequence[str],
        capability_ids: Sequence[str],
        incremental_capability_ids: Sequence[str],
        protected_incremental_capability_ids: Sequence[str],
        auxiliary_incremental_capability_ids: Sequence[str],
        evidence_kinds: Sequence[str],
        evidence_routes: Sequence[AdmissibleEvidenceRoute],
        maximum_claim_strength_by_obligation: Mapping[str, str],
        expected_value_projection: Mapping[str, str],
        estimated_budget_units: int,
        estimated_auxiliary_budget_units: int,
        remaining_auxiliary_budget_units: int | None,
        contract_refs: Sequence[str],
    ) -> "AdmissibleAxisRoute":
        routes = tuple(
            sorted(
                (_replay_evidence_route(item) for item in evidence_routes),
                key=lambda item: (
                    item.obligation_id,
                    item.capability_id,
                    item.evidence_kind,
                ),
            )
        )
        if not routes or len({item.evidence_route_ref for item in routes}) != len(
            routes
        ):
            raise ClaimCoverageContractError("claim_coverage_evidence_routes_invalid")
        obligation_ids = _string_tuple(
            supported_obligation_ids,
            "claim_coverage_route_obligation_ids_invalid",
            allow_empty=False,
        )
        claim_kinds = _string_tuple(
            supported_claim_kinds,
            "claim_coverage_route_claim_kinds_invalid",
            allow_empty=False,
        )
        capabilities = _string_tuple(
            capability_ids,
            "claim_coverage_route_capability_ids_invalid",
            allow_empty=False,
        )
        incremental_capabilities = _string_tuple(
            incremental_capability_ids,
            "claim_coverage_route_incremental_capabilities_invalid",
            allow_empty=False,
        )
        protected_incremental_capabilities = _string_tuple(
            protected_incremental_capability_ids,
            "claim_coverage_route_protected_incremental_capabilities_invalid",
        )
        auxiliary_incremental_capabilities = _string_tuple(
            auxiliary_incremental_capability_ids,
            "claim_coverage_route_auxiliary_incremental_capabilities_invalid",
        )
        evidence = _string_tuple(
            evidence_kinds,
            "claim_coverage_route_evidence_kinds_invalid",
            allow_empty=False,
        )
        if (
            set(obligation_ids) != {item.obligation_id for item in routes}
            or set(claim_kinds) != {item.claim_kind for item in routes}
            or set(capabilities) != {item.capability_id for item in routes}
            or set(evidence) != {item.evidence_kind for item in routes}
            or not set(capabilities).issubset(incremental_capabilities)
            or set(protected_incremental_capabilities).intersection(
                auxiliary_incremental_capabilities
            )
            or set(incremental_capabilities)
            != set(protected_incremental_capabilities).union(
                auxiliary_incremental_capabilities
            )
        ):
            raise ClaimCoverageContractError(
                "claim_coverage_evidence_route_closure_invalid"
            )
        maximum_strengths = _string_mapping(
            maximum_claim_strength_by_obligation,
            "claim_coverage_route_strengths_invalid",
        )
        if set(maximum_strengths) != set(obligation_ids):
            raise ClaimCoverageContractError("claim_coverage_route_strengths_invalid")
        expected_value = _route_expected_value_projection(expected_value_projection)
        if type(estimated_budget_units) is not int or estimated_budget_units < 1:
            raise ClaimCoverageContractError("claim_coverage_route_budget_invalid")
        if estimated_budget_units != (
            len(incremental_capabilities) * CAPABILITY_TASK_DECLARED_BUDGET_UNITS
        ):
            raise ClaimCoverageContractError("claim_coverage_route_budget_invalid")
        if (
            type(estimated_auxiliary_budget_units) is not int
            or estimated_auxiliary_budget_units < 0
            or estimated_auxiliary_budget_units
            != len(auxiliary_incremental_capabilities)
            * CAPABILITY_TASK_DECLARED_BUDGET_UNITS
        ):
            raise ClaimCoverageContractError(
                "claim_coverage_route_auxiliary_budget_invalid"
            )
        if remaining_auxiliary_budget_units is not None and (
            type(remaining_auxiliary_budget_units) is not int
            or remaining_auxiliary_budget_units < estimated_auxiliary_budget_units
        ):
            raise ClaimCoverageContractError(
                "claim_coverage_route_auxiliary_budget_invalid"
            )
        body = {
            "axis_id": _required_string(
                axis_id, "claim_coverage_route_axis_id_invalid"
            ),
            "business_name": _required_string(
                business_name,
                "claim_coverage_route_business_name_invalid",
            ),
            "semantics": _required_string(
                semantics, "claim_coverage_route_semantics_invalid"
            ),
            "selection_policy": _required_string(
                selection_policy,
                "claim_coverage_route_selection_policy_invalid",
            ),
            "supported_obligation_ids": obligation_ids,
            "supported_claim_kinds": claim_kinds,
            "capability_ids": capabilities,
            "incremental_capability_ids": incremental_capabilities,
            "protected_incremental_capability_ids": (
                protected_incremental_capabilities
            ),
            "auxiliary_incremental_capability_ids": (
                auxiliary_incremental_capabilities
            ),
            "evidence_kinds": evidence,
            "evidence_routes": routes,
            "maximum_claim_strength_by_obligation": maximum_strengths,
            "expected_value_projection": expected_value,
            "estimated_budget_units": estimated_budget_units,
            "estimated_auxiliary_budget_units": (estimated_auxiliary_budget_units),
            "remaining_auxiliary_budget_units": (remaining_auxiliary_budget_units),
            "contract_refs": _string_tuple(
                contract_refs,
                "claim_coverage_route_contract_refs_invalid",
                allow_empty=False,
            ),
        }
        digest = canonical_digest(body)
        return cls(
            route_ref="admissible-axis-route:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AdmissibleAxisRoute":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise ClaimCoverageContractError("claim_coverage_route_shape_invalid")
        rebuilt = cls.create(
            axis_id=payload["axis_id"],
            business_name=payload["business_name"],
            semantics=payload["semantics"],
            selection_policy=payload["selection_policy"],
            supported_obligation_ids=payload["supported_obligation_ids"],
            supported_claim_kinds=payload["supported_claim_kinds"],
            capability_ids=payload["capability_ids"],
            incremental_capability_ids=payload["incremental_capability_ids"],
            protected_incremental_capability_ids=payload[
                "protected_incremental_capability_ids"
            ],
            auxiliary_incremental_capability_ids=payload[
                "auxiliary_incremental_capability_ids"
            ],
            evidence_kinds=payload["evidence_kinds"],
            evidence_routes=tuple(
                AdmissibleEvidenceRoute.from_dict(item)
                for item in payload["evidence_routes"]
            ),
            maximum_claim_strength_by_obligation=payload[
                "maximum_claim_strength_by_obligation"
            ],
            expected_value_projection=payload["expected_value_projection"],
            estimated_budget_units=payload["estimated_budget_units"],
            estimated_auxiliary_budget_units=payload[
                "estimated_auxiliary_budget_units"
            ],
            remaining_auxiliary_budget_units=payload[
                "remaining_auxiliary_budget_units"
            ],
            contract_refs=payload["contract_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimCoverageContractError("claim_coverage_route_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class ClaimCoverageEvaluation:
    evaluation_ref: str
    schema_version: str
    status: str
    run_attempt_id: str
    intent_revision_id: str
    authority_context_ref: str
    source_plan_revision_id: str
    source_plan_digest: str
    source_execution_result_ref: str
    source_execution_result_digest: str
    exploration_stop_ref: str
    exploration_stop_reason: str
    exploration_stop_policy: Mapping[str, Any]
    used_budget_units: int
    hard_budget_limit: int | None
    obligation_coverages: tuple[ClaimObligationCoverage, ...]
    unresolved_obligation_ids: tuple[str, ...]
    scheduled_axis_ids: tuple[str, ...]
    admissible_routes: tuple[AdmissibleAxisRoute, ...]
    route_catalog_digest: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        plan_revision: PlanRevision,
        execution_result: AuthoritativeExecutionResult,
        obligation_coverages: Sequence[ClaimObligationCoverage],
        admissible_routes: Sequence[AdmissibleAxisRoute],
    ) -> "ClaimCoverageEvaluation":
        plan = _validated_plan(plan_revision)
        execution = _validated_execution(execution_result, plan=plan)
        coverage = tuple(
            sorted(
                (_replay_coverage(item) for item in obligation_coverages),
                key=lambda item: item.obligation_id,
            )
        )
        obligations = {item.obligation_id: item for item in plan.claim_obligations}
        if (
            len(coverage) != len(obligations)
            or len({item.obligation_id for item in coverage}) != len(coverage)
            or set(item.obligation_id for item in coverage) != set(obligations)
            or any(
                item.claim_kind != obligations[item.obligation_id].claim_kind
                or item.role != obligations[item.obligation_id].role
                or item.subject != obligations[item.obligation_id].subject
                or item.success_policy != obligations[item.obligation_id].success_policy
                for item in coverage
            )
        ):
            raise ClaimCoverageContractError(
                "claim_coverage_obligation_closure_invalid"
            )
        unresolved_ids = tuple(
            item.obligation_id
            for item in coverage
            if item.status in {"uncovered", "evidence_present"}
        )
        routes = tuple(
            sorted(
                (_replay_route(item) for item in admissible_routes),
                key=lambda item: item.axis_id,
            )
        )
        scheduled_axis_ids = tuple(sorted(item.axis_id for item in plan.analysis_axes))
        unresolved = set(unresolved_ids)
        scheduled = set(scheduled_axis_ids)
        if (
            len({item.axis_id for item in routes}) != len(routes)
            or any(item.axis_id in scheduled for item in routes)
            or any(
                not set(item.supported_obligation_ids).issubset(unresolved)
                for item in routes
            )
            or any(
                set(item.supported_claim_kinds)
                != {
                    obligations[obligation_id].claim_kind
                    for obligation_id in item.supported_obligation_ids
                }
                for item in routes
            )
            or len({item.remaining_auxiliary_budget_units for item in routes}) > 1
        ):
            raise ClaimCoverageContractError("claim_coverage_route_closure_invalid")
        route_catalog_digest = canonical_digest(
            tuple(item.to_dict() for item in routes)
        )
        body = {
            "schema_version": CLAIM_COVERAGE_SCHEMA_VERSION,
            "status": "evaluated",
            "run_attempt_id": plan.run_attempt_id,
            "intent_revision_id": plan.intent_revision_id,
            "authority_context_ref": plan.authority_context_ref,
            "source_plan_revision_id": plan.plan_revision_id,
            "source_plan_digest": plan.content_digest,
            "source_execution_result_ref": (
                execution.authoritative_execution_result_ref
            ),
            "source_execution_result_digest": execution.content_digest,
            "exploration_stop_ref": (execution.exploration_stop_record.stop_ref),
            "exploration_stop_reason": (execution.exploration_stop_record.reason),
            "exploration_stop_policy": _freeze(
                execution.exploration_stop_record.policy_decision
            ),
            "used_budget_units": (execution.exploration_stop_record.used_budget_units),
            "hard_budget_limit": (execution.exploration_stop_record.hard_budget_limit),
            "obligation_coverages": coverage,
            "unresolved_obligation_ids": unresolved_ids,
            "scheduled_axis_ids": scheduled_axis_ids,
            "admissible_routes": routes,
            "route_catalog_digest": route_catalog_digest,
        }
        digest = canonical_digest(body)
        return cls(
            evaluation_ref="claim-coverage-evaluation:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_context: AuthorityContext,
        plan_revision: PlanRevision,
        execution_result: AuthoritativeExecutionResult,
        route_catalog: RuntimeContractRegistry,
    ) -> "ClaimCoverageEvaluation":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise ClaimCoverageContractError("claim_coverage_evaluation_shape_invalid")
        rebuilt = evaluate_claim_coverage(
            authority_context=authority_context,
            plan_revision=plan_revision,
            execution_result=execution_result,
            route_catalog=route_catalog,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimCoverageContractError(
                "claim_coverage_evaluation_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class PlanExpansionDecision:
    decision_ref: str
    schema_version: str
    evaluation_ref: str
    decision: str
    decision_authority: str
    selected_axis_ids: tuple[str, ...]
    selected_obligation_ids: tuple[str, ...]
    selected_auxiliary_budget_units: int
    provider_audit_ref: str | None
    provider_ref: str | None
    model_ref: str | None
    prompt_version: str | None
    raw_response_ref: str | None
    raw_response_content: str | None
    structured_output: Mapping[str, Any] | None
    content_digest: str

    @classmethod
    def from_provider_audit(
        cls,
        *,
        evaluation: ClaimCoverageEvaluation,
        provider_audit: Mapping[str, Any],
    ) -> "PlanExpansionDecision":
        evaluation = _replay_evaluation(evaluation)
        if (
            not isinstance(provider_audit, Mapping)
            or set(provider_audit) != _PROVIDER_AUDIT_FIELDS
        ):
            raise ClaimCoverageContractError("plan_expansion_provider_audit_invalid")
        task = provider_audit.get("task")
        provider = provider_audit.get("provider")
        model = provider_audit.get("model")
        prompt_version = provider_audit.get("prompt_version")
        raw_response = provider_audit.get("raw_response_content")
        structured_output = provider_audit.get("structured_output")
        if (
            task != PLAN_EXPANSION_PROVIDER_TASK
            or not _is_required_string(provider)
            or not _is_required_string(model)
            or not _is_required_string(prompt_version)
            or not _is_required_string(raw_response)
            or not isinstance(structured_output, Mapping)
        ):
            raise ClaimCoverageContractError("plan_expansion_provider_audit_invalid")
        try:
            parsed_raw = parse_llm_structured_response_content(raw_response)
        except LLMOutputError as exc:
            raise ClaimCoverageContractError(
                "plan_expansion_provider_audit_invalid"
            ) from exc
        if canonical_value(parsed_raw) != canonical_value(structured_output):
            raise ClaimCoverageContractError("plan_expansion_provider_audit_invalid")
        decision, selected_axis_ids = _validated_provider_output(structured_output)
        routes_by_axis = {
            route.axis_id: route for route in evaluation.admissible_routes
        }
        if decision == "patch":
            if not selected_axis_ids:
                raise ClaimCoverageContractError("plan_expansion_patch_selection_empty")
            if any(axis_id not in routes_by_axis for axis_id in selected_axis_ids):
                raise ClaimCoverageContractError(
                    "plan_expansion_patch_route_not_admissible"
                )
            normalized_axes = tuple(
                route.axis_id
                for route in evaluation.admissible_routes
                if route.axis_id in set(selected_axis_ids)
            )
            selected_obligation_ids = tuple(
                sorted(
                    {
                        obligation_id
                        for axis_id in normalized_axes
                        for obligation_id in routes_by_axis[
                            axis_id
                        ].supported_obligation_ids
                    }
                )
            )
            if not selected_obligation_ids:
                raise ClaimCoverageContractError("plan_expansion_patch_selection_empty")
        else:
            if selected_axis_ids:
                raise ClaimCoverageContractError(
                    "plan_expansion_seal_selection_present"
                )
            normalized_axes = ()
            selected_obligation_ids = ()
        raw_response_ref = (
            "restricted-provider-response:sha256:"
            + sha256(raw_response.encode("utf-8")).hexdigest()
        )
        audit_body = {
            "task": task,
            "evaluation_ref": evaluation.evaluation_ref,
            "provider_ref": provider,
            "model_ref": model,
            "prompt_version": prompt_version,
            "raw_response_ref": raw_response_ref,
            "structured_output": canonical_value(structured_output),
        }
        provider_audit_ref = "plan-expansion-provider-audit:sha256:" + canonical_digest(
            audit_body
        )
        return cls._create(
            evaluation=evaluation,
            decision=decision,
            decision_authority="provider",
            selected_axis_ids=normalized_axes,
            selected_obligation_ids=selected_obligation_ids,
            provider_audit_ref=provider_audit_ref,
            provider_ref=provider,
            model_ref=model,
            prompt_version=prompt_version,
            raw_response_ref=raw_response_ref,
            raw_response_content=raw_response,
            structured_output=structured_output,
        )

    @classmethod
    def deterministic_seal(
        cls, evaluation: ClaimCoverageEvaluation
    ) -> "PlanExpansionDecision":
        evaluation = _replay_evaluation(evaluation)
        if evaluation.admissible_routes:
            raise ClaimCoverageContractError(
                "plan_expansion_deterministic_seal_forbidden"
            )
        return cls._create(
            evaluation=evaluation,
            decision="seal",
            decision_authority="deterministic_no_admissible_route",
            selected_axis_ids=(),
            selected_obligation_ids=(),
            provider_audit_ref=None,
            provider_ref=None,
            model_ref=None,
            prompt_version=None,
            raw_response_ref=None,
            raw_response_content=None,
            structured_output=None,
        )

    @classmethod
    def _create(
        cls,
        *,
        evaluation: ClaimCoverageEvaluation,
        decision: str,
        decision_authority: str,
        selected_axis_ids: Sequence[str],
        selected_obligation_ids: Sequence[str],
        provider_audit_ref: str | None,
        provider_ref: str | None,
        model_ref: str | None,
        prompt_version: str | None,
        raw_response_ref: str | None,
        raw_response_content: str | None,
        structured_output: Mapping[str, Any] | None,
    ) -> "PlanExpansionDecision":
        if decision not in _DECISIONS:
            raise ClaimCoverageContractError("plan_expansion_decision_invalid")
        if decision_authority not in _DECISION_AUTHORITIES:
            raise ClaimCoverageContractError(
                "plan_expansion_decision_authority_invalid"
            )
        axes = _string_tuple(
            selected_axis_ids,
            "plan_expansion_selected_axes_invalid",
        )
        obligations = _string_tuple(
            selected_obligation_ids,
            "plan_expansion_selected_obligations_invalid",
        )
        if decision == "patch" and (not axes or not obligations):
            raise ClaimCoverageContractError("plan_expansion_patch_selection_empty")
        if decision == "seal" and (axes or obligations):
            raise ClaimCoverageContractError("plan_expansion_seal_selection_present")
        selected_auxiliary_budget_units = _selected_route_auxiliary_budget_units(
            evaluation=evaluation,
            selected_axis_ids=axes,
        )
        provider_fields = (
            provider_audit_ref,
            provider_ref,
            model_ref,
            prompt_version,
            raw_response_ref,
            raw_response_content,
            structured_output,
        )
        if decision_authority == "provider":
            if any(value is None for value in provider_fields):
                raise ClaimCoverageContractError(
                    "plan_expansion_provider_audit_invalid"
                )
        elif any(value is not None for value in provider_fields):
            raise ClaimCoverageContractError(
                "plan_expansion_deterministic_audit_present"
            )
        body = {
            "schema_version": PLAN_EXPANSION_DECISION_SCHEMA_VERSION,
            "evaluation_ref": evaluation.evaluation_ref,
            "decision": decision,
            "decision_authority": decision_authority,
            "selected_axis_ids": axes,
            "selected_obligation_ids": obligations,
            "selected_auxiliary_budget_units": (selected_auxiliary_budget_units),
            "provider_audit_ref": provider_audit_ref,
            "provider_ref": provider_ref,
            "model_ref": model_ref,
            "prompt_version": prompt_version,
            "raw_response_ref": raw_response_ref,
            "raw_response_content": raw_response_content,
            "structured_output": (
                _freeze(structured_output) if structured_output is not None else None
            ),
        }
        digest = canonical_digest(body)
        return cls(
            decision_ref="plan-expansion-decision:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        evaluation: ClaimCoverageEvaluation,
    ) -> "PlanExpansionDecision":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise ClaimCoverageContractError("plan_expansion_decision_shape_invalid")
        if payload.get("decision_authority") == "provider":
            rebuilt = cls.from_provider_audit(
                evaluation=evaluation,
                provider_audit={
                    "task": PLAN_EXPANSION_PROVIDER_TASK,
                    "provider": payload["provider_ref"],
                    "model": payload["model_ref"],
                    "prompt_version": payload["prompt_version"],
                    "raw_response_content": payload["raw_response_content"],
                    "structured_output": payload["structured_output"],
                },
            )
        else:
            rebuilt = cls.deterministic_seal(evaluation)
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimCoverageContractError(
                "plan_expansion_decision_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class PlanPatch:
    plan_patch_ref: str
    schema_version: str
    run_attempt_id: str
    intent_revision_id: str
    authority_context_ref: str
    source_plan_revision_id: str
    source_plan_digest: str
    source_execution_result_ref: str
    source_execution_result_digest: str
    claim_coverage_evaluation_ref: str
    source_unresolved_obligation_ids: tuple[str, ...]
    selected_axis_ids: tuple[str, ...]
    selected_obligation_ids: tuple[str, ...]
    selected_auxiliary_budget_units: int
    plan_expansion_decision_ref: str
    provider_audit_ref: str
    provider_ref: str
    model_ref: str
    prompt_version: str
    raw_response_ref: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        plan_revision: PlanRevision,
        execution_result: AuthoritativeExecutionResult,
        evaluation: ClaimCoverageEvaluation,
        decision: PlanExpansionDecision,
    ) -> "PlanPatch":
        plan = _validated_plan(plan_revision)
        execution = _validated_execution(execution_result, plan=plan)
        evaluation = _replay_evaluation(evaluation)
        decision = _replay_decision(decision, evaluation=evaluation)
        if (
            evaluation.run_attempt_id != plan.run_attempt_id
            or evaluation.intent_revision_id != plan.intent_revision_id
            or evaluation.authority_context_ref != plan.authority_context_ref
            or evaluation.source_plan_revision_id != plan.plan_revision_id
            or evaluation.source_plan_digest != plan.content_digest
            or evaluation.source_execution_result_ref
            != execution.authoritative_execution_result_ref
            or evaluation.source_execution_result_digest != execution.content_digest
        ):
            raise ClaimCoverageContractError("plan_patch_source_closure_invalid")
        if (
            decision.decision != "patch"
            or decision.decision_authority != "provider"
            or decision.evaluation_ref != evaluation.evaluation_ref
            or not decision.selected_axis_ids
            or not decision.selected_obligation_ids
            or decision.provider_audit_ref is None
            or decision.provider_ref is None
            or decision.model_ref is None
            or decision.prompt_version is None
            or decision.raw_response_ref is None
        ):
            raise ClaimCoverageContractError("plan_patch_decision_invalid")
        routes_by_axis = {item.axis_id: item for item in evaluation.admissible_routes}
        if any(axis_id not in routes_by_axis for axis_id in decision.selected_axis_ids):
            raise ClaimCoverageContractError("plan_patch_route_closure_invalid")
        selected_obligation_ids = tuple(
            sorted(
                {
                    obligation_id
                    for axis_id in decision.selected_axis_ids
                    for obligation_id in routes_by_axis[
                        axis_id
                    ].supported_obligation_ids
                }
            )
        )
        if selected_obligation_ids != decision.selected_obligation_ids:
            raise ClaimCoverageContractError("plan_patch_obligation_closure_invalid")
        selected_auxiliary_budget_units = _selected_route_auxiliary_budget_units(
            evaluation=evaluation,
            selected_axis_ids=decision.selected_axis_ids,
        )
        if decision.selected_auxiliary_budget_units != selected_auxiliary_budget_units:
            raise ClaimCoverageContractError("plan_patch_budget_closure_invalid")
        body = {
            "schema_version": PLAN_PATCH_SCHEMA_VERSION,
            "run_attempt_id": plan.run_attempt_id,
            "intent_revision_id": plan.intent_revision_id,
            "authority_context_ref": plan.authority_context_ref,
            "source_plan_revision_id": plan.plan_revision_id,
            "source_plan_digest": plan.content_digest,
            "source_execution_result_ref": (
                execution.authoritative_execution_result_ref
            ),
            "source_execution_result_digest": execution.content_digest,
            "claim_coverage_evaluation_ref": evaluation.evaluation_ref,
            "source_unresolved_obligation_ids": (evaluation.unresolved_obligation_ids),
            "selected_axis_ids": decision.selected_axis_ids,
            "selected_obligation_ids": decision.selected_obligation_ids,
            "selected_auxiliary_budget_units": (selected_auxiliary_budget_units),
            "plan_expansion_decision_ref": decision.decision_ref,
            "provider_audit_ref": decision.provider_audit_ref,
            "provider_ref": decision.provider_ref,
            "model_ref": decision.model_ref,
            "prompt_version": decision.prompt_version,
            "raw_response_ref": decision.raw_response_ref,
        }
        digest = canonical_digest(body)
        return cls(
            plan_patch_ref="plan-patch:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        plan_revision: PlanRevision,
        execution_result: AuthoritativeExecutionResult,
        evaluation: ClaimCoverageEvaluation,
        decision: PlanExpansionDecision,
    ) -> "PlanPatch":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise ClaimCoverageContractError("plan_patch_shape_invalid")
        rebuilt = cls.create(
            plan_revision=plan_revision,
            execution_result=execution_result,
            evaluation=evaluation,
            decision=decision,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimCoverageContractError("plan_patch_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


def claim_coverage_transition_payloads(
    *,
    evaluation: ClaimCoverageEvaluation,
    decision: PlanExpansionDecision,
    plan_patch: PlanPatch | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evaluation = _replay_evaluation(evaluation)
    decision = _replay_decision(decision, evaluation=evaluation)
    if decision.decision == "patch":
        if type(plan_patch) is not PlanPatch:
            raise ClaimCoverageContractError("claim_coverage_checkpoint_patch_missing")
        patch = _replay_patch(
            plan_patch,
            evaluation=evaluation,
            decision=decision,
        )
    else:
        if plan_patch is not None:
            raise ClaimCoverageContractError(
                "claim_coverage_checkpoint_patch_unexpected"
            )
        patch = None
    return (
        {
            "source_plan_revision_id": evaluation.source_plan_revision_id,
            "source_plan_digest": evaluation.source_plan_digest,
            "source_execution_result_ref": (evaluation.source_execution_result_ref),
            "source_execution_result_digest": (
                evaluation.source_execution_result_digest
            ),
            "claim_coverage_evaluation_ref": evaluation.evaluation_ref,
            "claim_coverage_evaluation_digest": evaluation.content_digest,
        },
        {
            "claim_coverage_evaluation": evaluation.to_dict(),
            "plan_expansion_decision": decision.to_dict(),
            "plan_patch": None if patch is None else patch.to_dict(),
        },
    )


@dataclass(frozen=True)
class ClaimCoverageCheckpoint:
    checkpoint_ref: str
    schema_version: str
    run_attempt_id: str
    intent_revision_id: str
    authority_context_ref: str
    source_plan_revision_id: str
    source_execution_result_ref: str
    evaluation_ref: str
    evaluation_digest: str
    decision_ref: str
    decision_digest: str
    plan_patch_ref: str | None
    plan_patch_digest: str | None
    transition_id: str
    transition_output_digest: str
    evaluation: ClaimCoverageEvaluation
    decision: PlanExpansionDecision
    plan_patch: PlanPatch | None
    transition: DurableTransition
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        plan_revision: PlanRevision,
        execution_result: AuthoritativeExecutionResult,
        evaluation: ClaimCoverageEvaluation,
        decision: PlanExpansionDecision,
        plan_patch: PlanPatch | None,
        transition: DurableTransition,
    ) -> "ClaimCoverageCheckpoint":
        plan = _validated_plan(plan_revision)
        execution = _validated_execution(execution_result, plan=plan)
        evaluation = _replay_evaluation(evaluation)
        decision = _replay_decision(decision, evaluation=evaluation)
        if (
            evaluation.run_attempt_id != plan.run_attempt_id
            or evaluation.intent_revision_id != plan.intent_revision_id
            or evaluation.authority_context_ref != plan.authority_context_ref
            or evaluation.source_plan_revision_id != plan.plan_revision_id
            or evaluation.source_plan_digest != plan.content_digest
            or evaluation.source_execution_result_ref
            != execution.authoritative_execution_result_ref
            or evaluation.source_execution_result_digest != execution.content_digest
        ):
            raise ClaimCoverageContractError(
                "claim_coverage_checkpoint_source_closure_invalid"
            )
        if decision.decision == "patch":
            if type(plan_patch) is not PlanPatch:
                raise ClaimCoverageContractError(
                    "claim_coverage_checkpoint_patch_missing"
                )
            patch = PlanPatch.from_dict(
                plan_patch.to_dict(),
                plan_revision=plan,
                execution_result=execution,
                evaluation=evaluation,
                decision=decision,
            )
        else:
            if plan_patch is not None:
                raise ClaimCoverageContractError(
                    "claim_coverage_checkpoint_patch_unexpected"
                )
            patch = None
        if type(transition) is not DurableTransition:
            raise ClaimCoverageContractError(
                "claim_coverage_checkpoint_transition_invalid"
            )
        try:
            replayed_transition = DurableTransition.from_dict(transition.to_dict())
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimCoverageContractError(
                "claim_coverage_checkpoint_transition_invalid"
            ) from exc
        transition_input, transition_output = claim_coverage_transition_payloads(
            evaluation=evaluation,
            decision=decision,
            plan_patch=patch,
        )
        expected_next = (
            "compile_plan_patch"
            if decision.decision == "patch"
            else "seal_authority_bundle"
        )
        expected_provider = (
            decision.provider_ref
            if decision.decision_authority == "provider"
            else "local_deterministic"
        )
        expected_model = (
            decision.model_ref
            if decision.decision_authority == "provider"
            else "claim-coverage-contract.v1"
        )
        if (
            replayed_transition != transition
            or transition.node_name != "evaluate_claim_coverage"
            or transition.parent_transition_id != execution.transition_id
            or transition.run_attempt_id != plan.run_attempt_id
            or transition.intent_revision_id != plan.intent_revision_id
            or transition.decision_ledger_position
            != execution.durable_transition.decision_ledger_position
            or transition.input_digest != canonical_digest(transition_input)
            or transition.output_digest != canonical_digest(transition_output)
            or transition.status != "succeeded"
            or transition.acceptance_state != "accepted"
            or transition.next_transition != expected_next
            or transition.provider_ref != expected_provider
            or transition.model_ref != expected_model
        ):
            raise ClaimCoverageContractError(
                "claim_coverage_checkpoint_transition_invalid"
            )
        body = {
            "schema_version": CLAIM_COVERAGE_CHECKPOINT_SCHEMA_VERSION,
            "run_attempt_id": plan.run_attempt_id,
            "intent_revision_id": plan.intent_revision_id,
            "authority_context_ref": plan.authority_context_ref,
            "source_plan_revision_id": plan.plan_revision_id,
            "source_execution_result_ref": (
                execution.authoritative_execution_result_ref
            ),
            "evaluation_ref": evaluation.evaluation_ref,
            "evaluation_digest": evaluation.content_digest,
            "decision_ref": decision.decision_ref,
            "decision_digest": decision.content_digest,
            "plan_patch_ref": None if patch is None else patch.plan_patch_ref,
            "plan_patch_digest": None if patch is None else patch.content_digest,
            "transition_id": transition.transition_id,
            "transition_output_digest": transition.output_digest,
            "evaluation": evaluation,
            "decision": decision,
            "plan_patch": patch,
            "transition": transition,
        }
        digest = canonical_digest(body)
        return cls(
            checkpoint_ref="claim-coverage-checkpoint:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_context: AuthorityContext,
        plan_revision: PlanRevision,
        execution_result: AuthoritativeExecutionResult,
        route_catalog: RuntimeContractRegistry,
    ) -> "ClaimCoverageCheckpoint":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise ClaimCoverageContractError("claim_coverage_checkpoint_shape_invalid")
        evaluation = ClaimCoverageEvaluation.from_dict(
            payload["evaluation"],
            authority_context=authority_context,
            plan_revision=plan_revision,
            execution_result=execution_result,
            route_catalog=route_catalog,
        )
        decision = PlanExpansionDecision.from_dict(
            payload["decision"], evaluation=evaluation
        )
        raw_patch = payload["plan_patch"]
        patch = (
            None
            if raw_patch is None
            else PlanPatch.from_dict(
                raw_patch,
                plan_revision=plan_revision,
                execution_result=execution_result,
                evaluation=evaluation,
                decision=decision,
            )
        )
        try:
            transition = DurableTransition.from_dict(payload["transition"])
        except (TypeError, ValueError) as exc:
            raise ClaimCoverageContractError(
                "claim_coverage_checkpoint_transition_invalid"
            ) from exc
        rebuilt = cls.create(
            plan_revision=plan_revision,
            execution_result=execution_result,
            evaluation=evaluation,
            decision=decision,
            plan_patch=patch,
            transition=transition,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimCoverageContractError(
                "claim_coverage_checkpoint_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


def evaluate_claim_coverage(
    *,
    authority_context: AuthorityContext,
    plan_revision: PlanRevision,
    execution_result: AuthoritativeExecutionResult,
    route_catalog: RuntimeContractRegistry,
) -> ClaimCoverageEvaluation:
    context = _validated_authority_context(authority_context)
    plan = _validated_plan(plan_revision)
    execution = _validated_execution(execution_result, plan=plan)
    if not isinstance(route_catalog, RuntimeContractRegistry):
        raise ClaimCoverageContractError("claim_coverage_route_catalog_invalid")
    if (
        context.run_attempt_id != plan.run_attempt_id
        or context.authority_context_ref != plan.authority_context_ref
    ):
        raise ClaimCoverageContractError("claim_coverage_authority_context_invalid")
    obligations = {item.obligation_id: item for item in plan.claim_obligations}
    for obligation in obligations.values():
        required_strength = obligation.success_policy.get("minimum_claim_strength")
        goal_ids = _string_tuple(
            obligation.subject.get("goal_refs"),
            "claim_coverage_obligation_goal_refs_invalid",
            allow_empty=False,
        )
        axis_ids = (
            ()
            if obligation.role == "user_required"
            else _string_tuple(
                obligation.success_policy.get("requested_axis_ids"),
                "claim_coverage_requested_axis_ids_invalid",
                allow_empty=False,
            )
        )
        try:
            contract_strength = route_catalog.claim_required_publication_strength(
                obligation.claim_kind,
                goal_ids=goal_ids,
                axis_ids=axis_ids,
            )
        except (KeyError, ValueError) as exc:
            raise ClaimCoverageContractError(
                f"claim_coverage_claim_requirement_invalid:{obligation.claim_kind}"
            ) from exc
        if (
            obligation.success_policy.get("policy") != "verified_or_explicit_boundary"
            or required_strength != contract_strength
        ):
            raise ClaimCoverageContractError(
                "claim_coverage_success_policy_contract_mismatch:"
                f"{obligation.claim_kind}"
            )
    assessments_by_obligation: dict[str, dict[str, ClaimEvidenceCoverageAssessment]] = {
        obligation_id: {} for obligation_id in obligations
    }
    tasks = {item.task_id: item for item in plan.capability_tasks}
    for _, outcome, entries, _ in execution.capability_outcome_bundles:
        task = tasks[outcome.task_id]
        capability = route_catalog.capability_inputs(task.capability_id)
        capability_claim_kinds = set(
            str(item) for item in capability.get("supported_claim_types", ())
        )
        capability_evidence_kinds = set(_capability_evidence_kinds(capability))
        task_obligation_ids = set(task.supports_obligation_ids)
        if not task_obligation_ids.issubset(obligations):
            raise ClaimCoverageContractError(
                "claim_coverage_task_obligation_closure_invalid"
            )
        for entry in entries:
            _validate_evidence_entry(
                entry=entry,
                outcome=outcome,
                task=task,
                plan=plan,
                capability_claim_kinds=capability_claim_kinds,
                capability_evidence_kinds=capability_evidence_kinds,
            )
            relevant_obligation_ids = tuple(
                obligation_id
                for obligation_id in task.supports_obligation_ids
                if obligations[obligation_id].claim_kind
                in set(entry.supported_claim_kinds)
            )
            if task.supports_obligation_ids and not relevant_obligation_ids:
                raise ClaimCoverageContractError(
                    "claim_coverage_evidence_obligation_membership_missing"
                )
            for obligation_id in relevant_obligation_ids:
                if obligation_id not in set(outcome.affected_obligation_ids):
                    raise ClaimCoverageContractError(
                        "claim_coverage_outcome_obligation_membership_missing"
                    )
                obligation = obligations[obligation_id]
                if entry.evidence_kind not in set(
                    obligation.evidence_requirement.evidence_kinds
                ):
                    continue
                limitation_refs = tuple(
                    sorted(
                        {
                            *entry.limitation_refs,
                            *outcome.limitation_refs,
                        }
                    )
                )
                _validate_data_contract_state(
                    entry,
                    limitation_refs=limitation_refs,
                )
                ceiling = evidence_publication_ceiling(
                    evidence_kind=entry.evidence_kind,
                    source_claim_kind=obligation.claim_kind,
                    maximum_claim_strength=entry.maximum_claim_strength,
                )
                assessment = ClaimEvidenceCoverageAssessment.create(
                    evidence_entry_ref=entry.entry_ref,
                    settlement_outcome_ref=outcome.outcome_ref,
                    binding_record_ref=entry.binding_record_ref,
                    evidence_kind=entry.evidence_kind,
                    evidence_strength=entry.evidence_strength,
                    maximum_claim_strength=(entry.maximum_claim_strength),
                    publication_ceiling=ceiling.to_dict(),
                    data_contract_state=entry.data_contract_state,
                    supported_claim_kinds=entry.supported_claim_kinds,
                    observation_facts=entry.observation_facts,
                    scope=entry.scope,
                    window_refs=entry.window_refs,
                    dimension_path=entry.dimension_path,
                    limitation_refs=limitation_refs,
                    result_refs=entry.result_refs,
                    completeness_report_refs=(entry.completeness_report_refs),
                )
                existing = assessments_by_obligation[obligation_id].setdefault(
                    assessment.evidence_entry_ref,
                    assessment,
                )
                if existing != assessment:
                    raise ClaimCoverageContractError(
                        "claim_coverage_evidence_assessment_conflict"
                    )
    coverage = tuple(
        ClaimObligationCoverage.create(
            obligation_id=obligation.obligation_id,
            claim_kind=obligation.claim_kind,
            role=obligation.role,
            subject=obligation.subject,
            success_policy=obligation.success_policy,
            status=_coverage_status(
                tuple(assessments_by_obligation[obligation.obligation_id].values()),
                required_strength=str(
                    obligation.success_policy["minimum_claim_strength"]
                ),
            ),
            evidence_assessments=tuple(
                assessments_by_obligation[obligation.obligation_id].values()
            ),
        )
        for obligation in sorted(
            plan.claim_obligations, key=lambda item: item.obligation_id
        )
    )
    unresolved = tuple(
        obligations[item.obligation_id]
        for item in coverage
        if item.status in {"uncovered", "evidence_present"}
    )
    budget_policy = route_catalog.exploration_budget_policy
    try:
        protected_task_ids = budget_policy.protected_task_ids(plan)
    except ValueError as exc:
        raise ClaimCoverageContractError(
            "claim_coverage_budget_policy_invalid"
        ) from exc
    used_auxiliary_budget_units = sum(
        outcome.budget_units
        for _, outcome, _, _ in execution.capability_outcome_bundles
        if outcome.task_id not in protected_task_ids
    )
    remaining_auxiliary_budget_units = (
        None
        if budget_policy.auxiliary_budget_limit is None
        else max(
            budget_policy.auxiliary_budget_limit - used_auxiliary_budget_units,
            0,
        )
    )
    routes = _admissible_axis_routes(
        authority_context=context,
        plan=plan,
        unresolved_obligations=unresolved,
        route_catalog=route_catalog,
        remaining_auxiliary_budget_units=(remaining_auxiliary_budget_units),
    )
    return ClaimCoverageEvaluation.create(
        plan_revision=plan,
        execution_result=execution,
        obligation_coverages=coverage,
        admissible_routes=routes,
    )


def _admissible_axis_routes(
    *,
    authority_context: AuthorityContext,
    plan: PlanRevision,
    unresolved_obligations: Sequence[ClaimObligation],
    route_catalog: RuntimeContractRegistry,
    remaining_auxiliary_budget_units: int | None,
) -> tuple[AdmissibleAxisRoute, ...]:
    scheduled = {item.axis_id for item in plan.analysis_axes}
    authority_dataset_ids = {
        str(item["dataset_id"]) for item in authority_context.dataset_coverage
    }
    allowed_axes_by_obligation = {
        obligation.obligation_id: _allowed_axis_ids(
            obligation, route_catalog=route_catalog
        )
        for obligation in unresolved_obligations
    }
    routes = []
    for axis_id in route_catalog.analysis_axis_ids:
        if axis_id in scheduled:
            continue
        axis = route_catalog.analysis_axis(axis_id)
        axis_target_metrics = set(str(item) for item in axis["target_metric_refs"])
        supported_obligation_ids: set[str] = set()
        supported_claim_kinds: set[str] = set()
        supporting_capability_ids: set[str] = set()
        supporting_evidence_kinds: set[str] = set()
        evidence_routes: list[AdmissibleEvidenceRoute] = []
        feasible_capability_ids: set[str] = set()
        claim_kinds_by_capability: dict[str, set[str]] = {}
        for capability_id in axis["capability_refs"]:
            capability = route_catalog.capability_inputs(str(capability_id))
            if capability.get("completion_authority"):
                continue
            if not _capability_has_authority_coverage(
                capability=capability,
                axis=axis,
                authority_dataset_ids=authority_dataset_ids,
            ):
                continue
            feasible_capability_ids.add(str(capability_id))
            claim_kinds = set(
                str(item) for item in capability.get("supported_claim_types", ())
            )
            claim_kinds_by_capability[str(capability_id)] = claim_kinds
            evidence_kinds = set(_capability_evidence_kinds(capability))
            for obligation in unresolved_obligations:
                if axis_id not in allowed_axes_by_obligation[obligation.obligation_id]:
                    continue
                if not set(_obligation_target_metric_refs(obligation)).issubset(
                    axis_target_metrics
                ):
                    continue
                matching_evidence = evidence_kinds.intersection(
                    obligation.evidence_requirement.evidence_kinds
                )
                if obligation.claim_kind not in claim_kinds or not matching_evidence:
                    continue
                required_strength = str(
                    obligation.success_policy["minimum_claim_strength"]
                )
                capability_contract_ref = route_catalog.capability_contract_ref(
                    str(capability_id)
                )
                for evidence_kind in sorted(matching_evidence):
                    ceiling = admissible_evidence_publication_ceiling(
                        evidence_kind=evidence_kind,
                        source_claim_kind=obligation.claim_kind,
                        maximum_claim_strength=str(
                            capability["maximum_claim_strength"]
                        ),
                    )
                    if ceiling is None or not publication_ceiling_satisfies(
                        ceiling,
                        required_strength=required_strength,
                    ):
                        continue
                    evidence_routes.append(
                        AdmissibleEvidenceRoute.create(
                            obligation_id=obligation.obligation_id,
                            claim_kind=obligation.claim_kind,
                            capability_id=str(capability_id),
                            evidence_kind=evidence_kind,
                            maximum_claim_strength=str(
                                capability["maximum_claim_strength"]
                            ),
                            publication_ceiling=ceiling.to_dict(),
                            required_claim_strength=required_strength,
                            capability_contract_ref=(capability_contract_ref),
                        )
                    )
                    supported_obligation_ids.add(obligation.obligation_id)
                    supported_claim_kinds.add(obligation.claim_kind)
                    supporting_capability_ids.add(str(capability_id))
                    supporting_evidence_kinds.add(evidence_kind)
        if not supported_obligation_ids:
            continue
        required_claim_kinds = {
            obligation.claim_kind
            for obligation in plan.claim_obligations
            if obligation.role == "user_required"
        }
        successor_axis_role = _successor_axis_role(
            plan=plan,
            axis_id=axis_id,
            route_catalog=route_catalog,
        )
        if successor_axis_role in set(
            route_catalog.exploration_budget_policy.protected_axis_roles
        ):
            protected_incremental_capability_ids = set(feasible_capability_ids)
        else:
            protected_incremental_capability_ids = {
                capability_id
                for capability_id in feasible_capability_ids
                if claim_kinds_by_capability[capability_id].intersection(
                    required_claim_kinds
                )
            }
        auxiliary_incremental_capability_ids = (
            feasible_capability_ids - protected_incremental_capability_ids
        )
        estimated_budget_units = (
            len(feasible_capability_ids) * CAPABILITY_TASK_DECLARED_BUDGET_UNITS
        )
        estimated_auxiliary_budget_units = (
            len(auxiliary_incremental_capability_ids)
            * CAPABILITY_TASK_DECLARED_BUDGET_UNITS
        )
        if (
            remaining_auxiliary_budget_units is not None
            and estimated_auxiliary_budget_units > remaining_auxiliary_budget_units
        ):
            continue
        maximum_strength_by_obligation = {
            obligation_id: _strongest_route_strength(
                tuple(
                    item.maximum_claim_strength
                    for item in evidence_routes
                    if item.obligation_id == obligation_id
                ),
                route_catalog=route_catalog,
            )
            for obligation_id in supported_obligation_ids
        }
        supported_by_id = {
            obligation.obligation_id: obligation
            for obligation in unresolved_obligations
            if obligation.obligation_id in supported_obligation_ids
        }
        has_required_obligation = any(
            obligation.role == "user_required"
            for obligation in supported_by_id.values()
        )
        expected_value_projection = {
            "expected_information_gain": (
                "obligation_closing"
                if has_required_obligation
                else "hypothesis_testing"
            ),
            "materiality": (
                "user_required" if has_required_obligation else "analyst_auxiliary"
            ),
            "actionability": (
                "decision_supporting"
                if has_required_obligation
                else "explanation_supporting"
            ),
            "statistical_risk": (
                "multiplicity_sensitive"
                if "statistical_association" in supporting_evidence_kinds
                else "contract_bounded"
            ),
        }
        contract_refs = tuple(
            dict.fromkeys(
                (
                    *(str(item) for item in axis["source_refs"]),
                    *(
                        route_catalog.capability_contract_ref(capability_id)
                        for capability_id in sorted(supporting_capability_ids)
                    ),
                )
            )
        )
        routes.append(
            AdmissibleAxisRoute.create(
                axis_id=axis_id,
                business_name=str(axis["business_name"]),
                semantics=str(axis["semantics"]),
                selection_policy=str(axis["selection_policy"]),
                supported_obligation_ids=tuple(sorted(supported_obligation_ids)),
                supported_claim_kinds=tuple(sorted(supported_claim_kinds)),
                capability_ids=tuple(sorted(supporting_capability_ids)),
                incremental_capability_ids=tuple(sorted(feasible_capability_ids)),
                protected_incremental_capability_ids=tuple(
                    sorted(protected_incremental_capability_ids)
                ),
                auxiliary_incremental_capability_ids=tuple(
                    sorted(auxiliary_incremental_capability_ids)
                ),
                evidence_kinds=tuple(sorted(supporting_evidence_kinds)),
                evidence_routes=tuple(evidence_routes),
                maximum_claim_strength_by_obligation=(maximum_strength_by_obligation),
                expected_value_projection=expected_value_projection,
                estimated_budget_units=estimated_budget_units,
                estimated_auxiliary_budget_units=(estimated_auxiliary_budget_units),
                remaining_auxiliary_budget_units=(remaining_auxiliary_budget_units),
                contract_refs=contract_refs,
            )
        )
    return tuple(routes)


def _successor_axis_role(
    *,
    plan: PlanRevision,
    axis_id: str,
    route_catalog: RuntimeContractRegistry,
) -> str:
    active_goal_refs = tuple(
        dict.fromkeys(
            goal_ref
            for obligation in plan.claim_obligations
            if obligation.role == "user_required"
            for goal_ref in _string_tuple(
                obligation.subject.get("goal_refs"),
                "claim_coverage_obligation_goal_refs_invalid",
                allow_empty=False,
            )
        )
    )
    roles = tuple(
        str(binding["role"])
        for goal_ref in active_goal_refs
        for binding in route_catalog.analysis_goal_obligation(goal_ref)["analysis_axes"]
        if str(binding["axis_id"]) == axis_id
    )
    if not roles:
        return "auxiliary"
    if any(role not in _PLAN_AXIS_ROLE_PRIORITY for role in roles):
        raise ClaimCoverageContractError("claim_coverage_route_axis_role_invalid")
    return max(roles, key=_PLAN_AXIS_ROLE_PRIORITY.__getitem__)


def _allowed_axis_ids(
    obligation: ClaimObligation,
    *,
    route_catalog: RuntimeContractRegistry,
) -> frozenset[str]:
    if obligation.role == "user_required":
        goal_refs = _string_tuple(
            obligation.subject.get("goal_refs"),
            "claim_coverage_obligation_goal_refs_invalid",
            allow_empty=False,
        )
        return frozenset(
            str(binding["axis_id"])
            for goal_ref in goal_refs
            for binding in route_catalog.analysis_goal_obligation(goal_ref)[
                "analysis_axes"
            ]
        )
    requested_axis_ids = _string_tuple(
        obligation.success_policy.get("requested_axis_ids"),
        "claim_coverage_requested_axis_ids_invalid",
        allow_empty=False,
    )
    unknown = set(requested_axis_ids) - set(route_catalog.analysis_axis_ids)
    if unknown:
        raise ClaimCoverageContractError("claim_coverage_requested_axis_ids_invalid")
    return frozenset(requested_axis_ids)


def _obligation_target_metric_refs(
    obligation: ClaimObligation,
) -> tuple[str, ...]:
    if obligation.role == "user_required":
        return (
            _required_string(
                obligation.subject["target_metric_ref"],
                "claim_coverage_target_metric_refs_invalid",
            ),
        )
    if obligation.role == "analyst_auxiliary":
        return _string_tuple(
            obligation.subject["target_metric_refs"],
            "claim_coverage_target_metric_refs_invalid",
            allow_empty=False,
        )
    raise ClaimCoverageContractError("claim_coverage_target_metric_refs_invalid")


def _validate_evidence_entry(
    *,
    entry: Any,
    outcome: Any,
    task: Any,
    plan: PlanRevision,
    capability_claim_kinds: set[str],
    capability_evidence_kinds: set[str],
) -> None:
    if (
        outcome.status != "succeeded"
        or entry.execution_state != "available"
        or entry.task_id != task.task_id
        or entry.outcome_ref != outcome.outcome_ref
        or not entry.result_refs
        or not entry.completeness_report_refs
        or not entry.observation_facts
        or tuple(sorted(entry.window_refs)) != tuple(sorted(plan.resolved_window_refs))
        or entry.hierarchy_qualified != bool(entry.dimension_path)
    ):
        raise ClaimCoverageContractError("claim_coverage_evidence_authority_invalid")
    entry_claim_kinds = set(entry.supported_claim_kinds)
    if (
        not entry_claim_kinds.issubset(capability_claim_kinds)
        or entry.evidence_kind not in capability_evidence_kinds
    ):
        raise ClaimCoverageContractError("claim_coverage_evidence_contract_invalid")


def _validate_data_contract_state(
    entry: Any,
    *,
    limitation_refs: Sequence[str],
) -> None:
    if entry.data_contract_state == "complete":
        return
    if entry.data_contract_state == "partial":
        if not limitation_refs:
            raise ClaimCoverageContractError(
                "claim_coverage_partial_evidence_limitation_missing"
            )
        return
    raise ClaimCoverageContractError("claim_coverage_data_contract_state_invalid")


def _capability_evidence_kinds(
    capability: Mapping[str, Any],
) -> tuple[str, ...]:
    kinds = []
    for evidence_type in capability.get("supported_evidence_types", ()):
        if evidence_type in NON_PUBLISHABLE_EVIDENCE_TYPES:
            continue
        kind = publication_evidence_kind(str(evidence_type))
        if kind not in kinds:
            kinds.append(kind)
    return tuple(kinds)


def _capability_has_authority_coverage(
    *,
    capability: Mapping[str, Any],
    axis: Mapping[str, Any],
    authority_dataset_ids: set[str],
) -> bool:
    if capability.get("source_mode") == "requested_context_sources":
        allowed_context = {
            str(item) for item in capability.get("allowed_context_datasets", ())
        }
        required_dataset_ids = {
            str(item)
            for item in axis.get("context_source_refs", ())
            if str(item) in allowed_context
        }
    else:
        required_dataset_ids = {
            str(item) for item in capability.get("allowed_datasets", ())
        }
    return required_dataset_ids.issubset(authority_dataset_ids)


def _validated_provider_output(
    output: Mapping[str, Any],
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(output, Mapping) or set(output) != _PROVIDER_OUTPUT_FIELDS:
        raise ClaimCoverageContractError("plan_expansion_provider_output_invalid")
    decision = output.get("decision")
    if decision not in _DECISIONS:
        raise ClaimCoverageContractError("plan_expansion_provider_output_invalid")
    raw_axis_ids = output.get("selected_axis_ids")
    if (
        isinstance(raw_axis_ids, (str, bytes))
        or not isinstance(raw_axis_ids, Sequence)
        or any(not _is_required_string(item) for item in raw_axis_ids)
        or len(raw_axis_ids) != len(set(raw_axis_ids))
    ):
        raise ClaimCoverageContractError("plan_expansion_provider_output_invalid")
    return str(decision), tuple(str(item) for item in raw_axis_ids)


def _selected_route_auxiliary_budget_units(
    *,
    evaluation: ClaimCoverageEvaluation,
    selected_axis_ids: Sequence[str],
) -> int:
    routes_by_axis = {route.axis_id: route for route in evaluation.admissible_routes}
    selected_routes = tuple(
        routes_by_axis[axis_id]
        for axis_id in selected_axis_ids
        if axis_id in routes_by_axis
    )
    if len(selected_routes) != len(selected_axis_ids):
        raise ClaimCoverageContractError("plan_expansion_patch_route_not_admissible")
    incremental_task_keys = {
        (route.axis_id, capability_id)
        for route in selected_routes
        for capability_id in route.auxiliary_incremental_capability_ids
    }
    selected_budget_units = (
        len(incremental_task_keys) * CAPABILITY_TASK_DECLARED_BUDGET_UNITS
    )
    remaining_values = {
        route.remaining_auxiliary_budget_units for route in selected_routes
    }
    if len(remaining_values) > 1:
        raise ClaimCoverageContractError("plan_expansion_route_budget_inconsistent")
    remaining_budget_units = next(iter(remaining_values)) if remaining_values else None
    if (
        remaining_budget_units is not None
        and selected_budget_units > remaining_budget_units
    ):
        raise ClaimCoverageContractError("plan_expansion_patch_budget_exceeded")
    return selected_budget_units


def _validated_plan(value: Any) -> PlanRevision:
    if type(value) is not PlanRevision:
        raise ClaimCoverageContractError("claim_coverage_plan_invalid")
    try:
        replayed = PlanRevision.from_dict(value.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ClaimCoverageContractError("claim_coverage_plan_invalid") from exc
    if replayed != value:
        raise ClaimCoverageContractError("claim_coverage_plan_invalid")
    return replayed


def _validated_authority_context(value: Any) -> AuthorityContext:
    if type(value) is not AuthorityContext:
        raise ClaimCoverageContractError("claim_coverage_authority_context_invalid")
    try:
        replayed = AuthorityContext.from_dict(value.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ClaimCoverageContractError(
            "claim_coverage_authority_context_invalid"
        ) from exc
    if replayed != value:
        raise ClaimCoverageContractError("claim_coverage_authority_context_invalid")
    return replayed


def _validated_execution(
    value: Any, *, plan: PlanRevision
) -> AuthoritativeExecutionResult:
    try:
        execution = validate_typed_authoritative_execution_result(value)
    except (TypeError, ValueError) as exc:
        raise ClaimCoverageContractError("claim_coverage_execution_invalid") from exc
    if (
        execution.plan_revision != plan
        or execution.run_attempt_id != plan.run_attempt_id
        or execution.intent_revision_id != plan.intent_revision_id
        or execution.authority_context_ref != plan.authority_context_ref
        or execution.plan_revision_id != plan.plan_revision_id
    ):
        raise ClaimCoverageContractError("claim_coverage_source_closure_invalid")
    return execution


def _replay_coverage(value: Any) -> ClaimObligationCoverage:
    if type(value) is not ClaimObligationCoverage:
        raise ClaimCoverageContractError("claim_coverage_record_invalid")
    return ClaimObligationCoverage.from_dict(value.to_dict())


def _replay_assessment(value: Any) -> ClaimEvidenceCoverageAssessment:
    if type(value) is not ClaimEvidenceCoverageAssessment:
        raise ClaimCoverageContractError("claim_coverage_assessment_invalid")
    return ClaimEvidenceCoverageAssessment.from_dict(value.to_dict())


def _replay_route(value: Any) -> AdmissibleAxisRoute:
    if type(value) is not AdmissibleAxisRoute:
        raise ClaimCoverageContractError("claim_coverage_route_invalid")
    return AdmissibleAxisRoute.from_dict(value.to_dict())


def _replay_evidence_route(value: Any) -> AdmissibleEvidenceRoute:
    if type(value) is not AdmissibleEvidenceRoute:
        raise ClaimCoverageContractError("claim_coverage_evidence_route_invalid")
    return AdmissibleEvidenceRoute.from_dict(value.to_dict())


def _replay_evaluation(value: Any) -> ClaimCoverageEvaluation:
    if type(value) is not ClaimCoverageEvaluation:
        raise ClaimCoverageContractError("claim_coverage_evaluation_invalid")
    coverage = tuple(
        sorted(
            (_replay_coverage(item) for item in value.obligation_coverages),
            key=lambda item: item.obligation_id,
        )
    )
    routes = tuple(
        sorted(
            (_replay_route(item) for item in value.admissible_routes),
            key=lambda item: item.axis_id,
        )
    )
    unresolved_ids = tuple(
        item.obligation_id
        for item in coverage
        if item.status in {"uncovered", "evidence_present"}
    )
    scheduled_axis_ids = _string_tuple(
        value.scheduled_axis_ids,
        "claim_coverage_evaluation_invalid",
    )
    coverage_by_id = {item.obligation_id: item for item in coverage}
    if (
        coverage != value.obligation_coverages
        or routes != value.admissible_routes
        or scheduled_axis_ids != value.scheduled_axis_ids
        or len(coverage_by_id) != len(coverage)
        or unresolved_ids != value.unresolved_obligation_ids
        or len({item.axis_id for item in routes}) != len(routes)
        or len({item.remaining_auxiliary_budget_units for item in routes}) > 1
        or any(item.axis_id in set(scheduled_axis_ids) for item in routes)
        or any(
            not set(item.supported_obligation_ids).issubset(unresolved_ids)
            or set(item.supported_claim_kinds)
            != {
                coverage_by_id[obligation_id].claim_kind
                for obligation_id in item.supported_obligation_ids
            }
            for item in routes
        )
    ):
        raise ClaimCoverageContractError("claim_coverage_evaluation_invalid")
    route_catalog_digest = canonical_digest(tuple(item.to_dict() for item in routes))
    body = {
        "schema_version": value.schema_version,
        "status": value.status,
        "run_attempt_id": value.run_attempt_id,
        "intent_revision_id": value.intent_revision_id,
        "authority_context_ref": value.authority_context_ref,
        "source_plan_revision_id": value.source_plan_revision_id,
        "source_plan_digest": value.source_plan_digest,
        "source_execution_result_ref": value.source_execution_result_ref,
        "source_execution_result_digest": value.source_execution_result_digest,
        "exploration_stop_ref": value.exploration_stop_ref,
        "exploration_stop_reason": value.exploration_stop_reason,
        "exploration_stop_policy": _freeze(value.exploration_stop_policy),
        "used_budget_units": value.used_budget_units,
        "hard_budget_limit": value.hard_budget_limit,
        "obligation_coverages": coverage,
        "unresolved_obligation_ids": unresolved_ids,
        "scheduled_axis_ids": scheduled_axis_ids,
        "admissible_routes": routes,
        "route_catalog_digest": route_catalog_digest,
    }
    digest = canonical_digest(body)
    if (
        value.schema_version != CLAIM_COVERAGE_SCHEMA_VERSION
        or value.status != "evaluated"
        or value.content_digest != digest
        or value.evaluation_ref != "claim-coverage-evaluation:sha256:" + digest
        or value.route_catalog_digest != route_catalog_digest
    ):
        raise ClaimCoverageContractError("claim_coverage_evaluation_invalid")
    return value


def _replay_decision(
    value: Any, *, evaluation: ClaimCoverageEvaluation
) -> PlanExpansionDecision:
    if type(value) is not PlanExpansionDecision:
        raise ClaimCoverageContractError("plan_expansion_decision_invalid")
    return PlanExpansionDecision.from_dict(value.to_dict(), evaluation=evaluation)


def _replay_patch(
    value: Any,
    *,
    evaluation: ClaimCoverageEvaluation,
    decision: PlanExpansionDecision,
) -> PlanPatch:
    if type(value) is not PlanPatch:
        raise ClaimCoverageContractError("plan_patch_invalid")
    if (
        value.claim_coverage_evaluation_ref != evaluation.evaluation_ref
        or value.plan_expansion_decision_ref != decision.decision_ref
        or value.source_plan_revision_id != evaluation.source_plan_revision_id
        or value.source_execution_result_ref != evaluation.source_execution_result_ref
    ):
        raise ClaimCoverageContractError("plan_patch_source_closure_invalid")
    return value


def _required_string(value: Any, error: str) -> str:
    if not _is_required_string(value):
        raise ClaimCoverageContractError(error)
    return str(value)


def _optional_string(value: Any, error: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, error)


def _is_required_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or any(not _is_required_string(item) for item in value)
    ):
        raise ClaimCoverageContractError(error)
    normalized = tuple(sorted(str(item) for item in value))
    if len(normalized) != len(set(normalized)) or (not allow_empty and not normalized):
        raise ClaimCoverageContractError(error)
    return normalized


def _ordered_string_tuple(value: Any, error: str) -> tuple[str, ...]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or any(not _is_required_string(item) for item in value)
    ):
        raise ClaimCoverageContractError(error)
    normalized = tuple(str(item) for item in value)
    if len(normalized) != len(set(normalized)):
        raise ClaimCoverageContractError(error)
    return normalized


def _mapping_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
) -> tuple[Mapping[str, Any], ...]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or any(not isinstance(item, Mapping) for item in value)
    ):
        raise ClaimCoverageContractError(error)
    normalized = tuple(_freeze(item) for item in value)
    if not allow_empty and not normalized:
        raise ClaimCoverageContractError(error)
    return normalized


def _string_mapping(value: Any, error: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ClaimCoverageContractError(error)
    normalized = {
        _required_string(key, error): _required_string(item, error)
        for key, item in value.items()
    }
    return _freeze(dict(sorted(normalized.items())))


def _route_expected_value_projection(value: Any) -> Mapping[str, str]:
    projection = _string_mapping(
        value,
        "claim_coverage_route_expected_value_invalid",
    )
    if set(projection) != set(_ROUTE_EXPECTED_VALUE_VALUES) or any(
        projection[key] not in allowed
        for key, allowed in _ROUTE_EXPECTED_VALUE_VALUES.items()
    ):
        raise ClaimCoverageContractError("claim_coverage_route_expected_value_invalid")
    return projection


def _strongest_route_strength(
    strengths: Sequence[str],
    *,
    route_catalog: RuntimeContractRegistry,
) -> str:
    normalized = _string_tuple(
        tuple(dict.fromkeys(strengths)),
        "claim_coverage_route_strengths_invalid",
        allow_empty=False,
    )
    highest_rank = max(
        route_catalog.maximum_claim_strength_rank(strength) for strength in normalized
    )
    strongest = tuple(
        strength
        for strength in normalized
        if route_catalog.maximum_claim_strength_rank(strength) == highest_rank
    )
    if len(strongest) != 1:
        raise ClaimCoverageContractError("claim_coverage_route_strengths_ambiguous")
    return strongest[0]


def _coverage_status(
    assessments: Sequence[ClaimEvidenceCoverageAssessment],
    *,
    required_strength: str,
) -> str:
    if not assessments:
        return "uncovered"
    if _explicit_boundary_satisfied(
        assessments,
        required_strength=required_strength,
    ):
        return "explicit_boundary"
    return "evidence_present"


def _explicit_boundary_satisfied(
    assessments: Sequence[ClaimEvidenceCoverageAssessment],
    *,
    required_strength: str,
) -> bool:
    if not assessments or any(item.evidence_kind != "boundary" for item in assessments):
        return False
    return any(
        bool(item.limitation_refs)
        and publication_ceiling_satisfies(
            ClaimPublicationCeiling.from_dict(
                canonical_value(item.publication_ceiling)
            ),
            required_strength=required_strength,
        )
        for item in assessments
    )


def _freeze(value: Any) -> Any:
    normalized = canonical_value(value)
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_freeze(item) for item in normalized)
    return normalized


__all__ = (
    "AdmissibleEvidenceRoute",
    "AdmissibleAxisRoute",
    "CLAIM_COVERAGE_CHECKPOINT_SCHEMA_VERSION",
    "CLAIM_COVERAGE_SCHEMA_VERSION",
    "ClaimCoverageCheckpoint",
    "ClaimCoverageContractError",
    "ClaimCoverageEvaluation",
    "ClaimEvidenceCoverageAssessment",
    "ClaimObligationCoverage",
    "PLAN_EXPANSION_DECISION_SCHEMA_VERSION",
    "PLAN_EXPANSION_PROVIDER_TASK",
    "PLAN_PATCH_SCHEMA_VERSION",
    "PlanExpansionDecision",
    "PlanPatch",
    "claim_coverage_transition_payloads",
    "evaluate_claim_coverage",
)
