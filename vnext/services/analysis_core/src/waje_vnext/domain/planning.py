"""Gate 3.4 plan adoption and closed logical query contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .async_runtime import AuthoritySnapshot
from .authority import InvestigationCase, WorkPlanRevision, WorkTask
from .canonical import (
    content_sha256,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
)
from .measurement import (
    AnalysisFrameRevision,
    ClaimStrengthCeiling,
    ClaimTargetKind,
    EvidenceComposition,
    EvidenceRequirementSpec,
    MeasurementDerivationAuthority,
    MeasurementResolutionOutcome,
    ObligationExecutionDisposition,
    RequirementBoundaryPolicy,
    ResolvedEvidenceObligation,
    ResolvedMeasurementInstance,
    ResolutionOutcomeKind,
)
from .measurement_resolver import MeasurementResolutionAdmission


SCHEMA_EPOCH = 3
PLAN_ADOPTION_POLICY_VERSION = "plan-adoption.v1"
QUERY_BINDING_POLICY_VERSION = "logical-query-binding.v1"
GATE4_COMPILER_CONTRACT_REF = (
    "waje-vnext://physical-query-compiler/query-binding.v1"
)
CONFORMANCE_EXECUTION_POLICY_VERSION = "conformance-execution.v1"
CONFORMANCE_FIXTURE_PREFIX = "waje-vnext://conformance-fixture/"
CAPABILITY_INTENT_PREFIX = "waje-vnext://capability-intent/"
CAPABILITY_INTENT_REGISTRY_VERSION = "capability-intent-registry.g3.4.v1"
RESULT_CONTRACT_PREFIX = "waje-vnext://result-contract/"
CONFORMANCE_EXECUTION_POLICY_PREFIX = (
    "waje-vnext://execution-policy/conformance."
)


class ExecutionRealm(StrEnum):
    CONFORMANCE = "conformance"


class LogicalAttemptKind(StrEnum):
    INITIAL = "initial"
    TECHNICAL_RETRY = "technical_retry"


@dataclass(frozen=True, slots=True)
class CapabilityIntentContract:
    capability_intent_ref: str
    allowed_execution_dispositions: tuple[
        ObligationExecutionDisposition,
        ...,
    ]
    allowed_action_kinds: tuple[str, ...]
    allows_any_governed_evidence_type: bool
    allowed_evidence_type_refs: tuple[str, ...]
    required_measurement_authority_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(
            self.capability_intent_ref,
            "capability_intent_ref",
        )
        if not self.capability_intent_ref.startswith(
            CAPABILITY_INTENT_PREFIX
        ):
            raise ValueError("capability intent is outside its namespace")
        if not self.allowed_execution_dispositions:
            raise ValueError(
                "capability intent requires an obligation disposition"
            )
        if len(self.allowed_execution_dispositions) != len(
            set(self.allowed_execution_dispositions)
        ):
            raise ValueError(
                "capability intent dispositions must be unique"
            )
        if any(
            not isinstance(item, ObligationExecutionDisposition)
            for item in self.allowed_execution_dispositions
        ):
            raise TypeError(
                "capability intent dispositions must be typed"
            )
        if not isinstance(self.allowed_action_kinds, tuple):
            raise TypeError("allowed_action_kinds must be a tuple")
        for field_name in (
            "allowed_action_kinds",
            "allowed_evidence_type_refs",
            "required_measurement_authority_fields",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple):
                raise TypeError(f"{field_name} must be a tuple")
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
            for value in values:
                require_nonempty(value, field_name)
        if (
            self.allows_any_governed_evidence_type
            == bool(self.allowed_evidence_type_refs)
        ):
            raise ValueError(
                "capability intent must choose unrestricted or exact "
                "evidence type applicability"
            )
        known_authority_fields = {
            "alternative_ids",
            "sensitivity_ids",
            "falsification_ids",
            "reversal_ids",
        }
        if not set(self.required_measurement_authority_fields) <= (
            known_authority_fields
        ):
            raise ValueError(
                "capability intent requires an unknown measurement "
                "authority field"
            )


@dataclass(frozen=True, slots=True)
class CapabilityIntentRegistry:
    registry_version: str
    contracts: tuple[CapabilityIntentContract, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.registry_version, "registry_version")
        if not self.contracts:
            raise ValueError("capability intent registry cannot be empty")
        refs = tuple(
            item.capability_intent_ref for item in self.contracts
        )
        if len(refs) != len(set(refs)):
            raise ValueError(
                "capability intent registry refs must be unique"
            )

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)

    def get(self, capability_intent_ref: str) -> CapabilityIntentContract:
        for contract in self.contracts:
            if contract.capability_intent_ref == capability_intent_ref:
                return contract
        raise ValueError(
            "capability_intent_ref is absent from the governed registry"
        )


CAPABILITY_INTENT_CONTRACTS = (
    CapabilityIntentContract(
        capability_intent_ref=(
            "{}measurement-evidence.v1".format(CAPABILITY_INTENT_PREFIX)
        ),
        allowed_execution_dispositions=(
            ObligationExecutionDisposition.EXECUTABLE,
        ),
        allowed_action_kinds=("call_capability",),
        allows_any_governed_evidence_type=True,
        allowed_evidence_type_refs=(),
        required_measurement_authority_fields=(),
    ),
    CapabilityIntentContract(
        capability_intent_ref=(
            "{}measurement-sensitivity.v1".format(
                CAPABILITY_INTENT_PREFIX
            )
        ),
        allowed_execution_dispositions=(
            ObligationExecutionDisposition.EXECUTABLE,
        ),
        allowed_action_kinds=("run_sensitivity",),
        allows_any_governed_evidence_type=False,
        allowed_evidence_type_refs=("evidence:sensitivity",),
        required_measurement_authority_fields=("sensitivity_ids",),
    ),
    CapabilityIntentContract(
        capability_intent_ref=(
            "{}boundary-inspection.v1".format(CAPABILITY_INTENT_PREFIX)
        ),
        allowed_execution_dispositions=(
            ObligationExecutionDisposition.TYPED_BOUNDARY,
            ObligationExecutionDisposition.BLOCKED,
        ),
        allowed_action_kinds=(),
        allows_any_governed_evidence_type=True,
        allowed_evidence_type_refs=(),
        required_measurement_authority_fields=(),
    ),
)
CAPABILITY_INTENT_REGISTRY = CapabilityIntentRegistry(
    registry_version=CAPABILITY_INTENT_REGISTRY_VERSION,
    contracts=CAPABILITY_INTENT_CONTRACTS,
)
REGISTERED_CAPABILITY_INTENT_REFS = frozenset(
    item.capability_intent_ref
    for item in CAPABILITY_INTENT_REGISTRY.contracts
)
CAPABILITY_INTENT_REGISTRY_SHA256 = (
    CAPABILITY_INTENT_REGISTRY.content_sha256
)


def get_capability_intent_contract(
    capability_intent_ref: str,
) -> CapabilityIntentContract:
    return CAPABILITY_INTENT_REGISTRY.get(capability_intent_ref)


@dataclass(frozen=True, slots=True)
class ProposedWorkTask:
    proposal_task_key: str
    business_purpose: str
    capability_intent_ref: str
    obligation_ids: tuple[str, ...]
    depends_on_task_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "proposal_task_key",
            "business_purpose",
            "capability_intent_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if (
            self.capability_intent_ref
            not in REGISTERED_CAPABILITY_INTENT_REFS
        ):
            raise ValueError(
                "capability_intent_ref is absent from the governed registry"
            )
        for field_name in (
            "obligation_ids",
            "depends_on_task_keys",
        ):
            _require_string_tuple(
                getattr(self, field_name),
                field_name,
            )
        if not self.obligation_ids:
            raise ValueError(
                "proposed work task must close at least one obligation"
            )
        if self.proposal_task_key in self.depends_on_task_keys:
            raise ValueError("proposed task cannot depend on itself")


@dataclass(frozen=True, slots=True)
class LogicalMeasurementBinding:
    claim_target_kind: ClaimTargetKind
    claim_target_spec_sha256: str
    variable_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    population_id: str | None
    observation_unit_id: str | None
    temporal_semantic_id: str | None
    estimator_id: str | None
    exposure_id: str | None
    contrast_id: str | None
    sequence_id: str | None
    cohort_risk_set_id: str | None
    reconciliation_id: str | None
    relationship_id: str | None
    eligibility_id: str | None
    identification_id: str | None
    alternative_ids: tuple[str, ...]
    sensitivity_ids: tuple[str, ...]
    falsification_ids: tuple[str, ...]
    reversal_ids: tuple[str, ...]
    scope_ceiling_id: str
    claim_strength_ceiling: ClaimStrengthCeiling

    def __post_init__(self) -> None:
        if not isinstance(self.claim_target_kind, ClaimTargetKind):
            raise TypeError(
                "claim_target_kind must be ClaimTargetKind"
            )
        if not isinstance(
            self.claim_strength_ceiling,
            ClaimStrengthCeiling,
        ):
            raise TypeError(
                "claim_strength_ceiling must be ClaimStrengthCeiling"
            )
        require_sha256(
            self.claim_target_spec_sha256,
            "claim_target_spec_sha256",
        )
        require_nonempty(self.scope_ceiling_id, "scope_ceiling_id")
        for field_name in (
            "variable_ids",
            "event_ids",
            "alternative_ids",
            "sensitivity_ids",
            "falsification_ids",
            "reversal_ids",
        ):
            _require_string_tuple(
                getattr(self, field_name),
                field_name,
            )
        for field_name in (
            "population_id",
            "observation_unit_id",
            "temporal_semantic_id",
            "estimator_id",
            "exposure_id",
            "contrast_id",
            "sequence_id",
            "cohort_risk_set_id",
            "reconciliation_id",
            "relationship_id",
            "eligibility_id",
            "identification_id",
        ):
            _require_optional_ref(
                getattr(self, field_name),
                field_name,
            )


@dataclass(frozen=True, slots=True)
class EvidenceRequirementBinding:
    evidence_requirement_id: str
    required_evidence_type_refs: tuple[str, ...]
    obligation_evidence_type_refs: tuple[str, ...]
    composition: EvidenceComposition
    minimum_count: int | None
    minimum_strength: ClaimStrengthCeiling
    scope_id: str
    exposure_id: str | None
    contradiction_policy_ref: str
    boundary_policy: RequirementBoundaryPolicy
    allowed_boundary_codes: tuple[str, ...]
    linked_falsification_ids: tuple[str, ...]
    linked_reversal_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_requirement_id",
            "scope_id",
            "contradiction_policy_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        for field_name in (
            "required_evidence_type_refs",
            "obligation_evidence_type_refs",
            "allowed_boundary_codes",
            "linked_falsification_ids",
            "linked_reversal_ids",
        ):
            _require_string_tuple(
                getattr(self, field_name),
                field_name,
            )
        if not self.required_evidence_type_refs:
            raise ValueError(
                "requirement binding requires evidence types"
            )
        if (
            not self.obligation_evidence_type_refs
            or not set(self.obligation_evidence_type_refs)
            <= set(self.required_evidence_type_refs)
        ):
            raise ValueError(
                "obligation evidence types must belong to requirement"
            )
        if not isinstance(self.composition, EvidenceComposition):
            raise TypeError("composition must be EvidenceComposition")
        if not isinstance(
            self.minimum_strength,
            ClaimStrengthCeiling,
        ):
            raise TypeError(
                "minimum_strength must be ClaimStrengthCeiling"
            )
        if not isinstance(
            self.boundary_policy,
            RequirementBoundaryPolicy,
        ):
            raise TypeError(
                "boundary_policy must be RequirementBoundaryPolicy"
            )
        _require_optional_ref(self.exposure_id, "exposure_id")
        if self.composition is EvidenceComposition.AT_LEAST:
            if (
                type(self.minimum_count) is not int
                or self.minimum_count < 1
            ):
                raise ValueError(
                    "at_least binding requires minimum_count"
                )
            if self.minimum_count > len(
                self.required_evidence_type_refs
            ):
                raise ValueError(
                    "minimum_count cannot exceed required evidence slots"
                )
        elif self.minimum_count is not None:
            raise ValueError(
                "minimum_count only applies to at_least binding"
            )


@dataclass(frozen=True, slots=True)
class QueryBindingEnvelope:
    query_binding_id: str
    case_id: str
    question_revision_id: str
    frame_revision_id: str
    plan_revision_id: str
    task_id: str
    capability_intent_ref: str
    estimand_id: str
    evidence_requirement_id: str
    obligation_id: str
    resolution_outcome_id: str
    semantic_measurement_id: str
    authority_binding_id: str
    frame_content_sha256: str
    plan_content_sha256: str
    resolution_outcome_content_sha256: str
    obligation_content_sha256: str
    measurement_binding: LogicalMeasurementBinding
    requirement_binding: EvidenceRequirementBinding
    resolved_measurement_instance: ResolvedMeasurementInstance
    query_binding_policy_version: str
    physical_compiler_contract_ref: str
    created_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for field_name in (
            "query_binding_id",
            "semantic_measurement_id",
            "authority_binding_id",
            "frame_content_sha256",
            "plan_content_sha256",
            "resolution_outcome_content_sha256",
            "obligation_content_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        for field_name in (
            "case_id",
            "question_revision_id",
            "frame_revision_id",
            "plan_revision_id",
            "task_id",
            "capability_intent_ref",
            "estimand_id",
            "evidence_requirement_id",
            "resolution_outcome_id",
            "query_binding_policy_version",
            "physical_compiler_contract_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        require_sha256(self.obligation_id, "obligation_id")
        if not isinstance(
            self.measurement_binding,
            LogicalMeasurementBinding,
        ):
            raise TypeError(
                "measurement_binding must be LogicalMeasurementBinding"
            )
        if not isinstance(
            self.requirement_binding,
            EvidenceRequirementBinding,
        ):
            raise TypeError(
                "requirement_binding must be EvidenceRequirementBinding"
            )
        if not isinstance(
            self.resolved_measurement_instance,
            ResolvedMeasurementInstance,
        ):
            raise TypeError(
                "resolved_measurement_instance must be "
                "ResolvedMeasurementInstance"
            )
        if (
            self.resolved_measurement_instance.resolution_id
            == self.resolution_outcome_id
        ):
            raise ValueError(
                "resolution instance and outcome require distinct identities"
            )
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("query binding requires schema epoch 3")
        if not self.capability_intent_ref.startswith(
            CAPABILITY_INTENT_PREFIX
        ):
            raise ValueError(
                "query binding capability intent is ungoverned"
            )
        if (
            self.query_binding_policy_version
            != QUERY_BINDING_POLICY_VERSION
            or self.physical_compiler_contract_ref
            != GATE4_COMPILER_CONTRACT_REF
        ):
            raise ValueError(
                "query binding policy contract is unsupported"
            )
        require_aware_datetime(self.created_at, "created_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class PlanAdoptionRecord:
    plan_adoption_id: str
    case_id: str
    question_revision_id: str
    frame_revision_id: str
    plan_revision_id: str
    expected_head_version: int
    authority_snapshot: AuthoritySnapshot
    authority_snapshot_sha256: str
    frame_content_sha256: str
    plan_content_sha256: str
    resolution_outcome_ids: tuple[str, ...]
    resolution_outcome_content_sha256s: tuple[str, ...]
    resolution_admission_content_sha256s: tuple[str, ...]
    resolution_context_sha256s: tuple[str, ...]
    resolver_input_bundle_sha256s: tuple[str, ...]
    resolution_registry_content_sha256s: tuple[str, ...]
    obligation_ids: tuple[str, ...]
    obligation_content_sha256s: tuple[str, ...]
    query_binding_ids: tuple[str, ...]
    query_binding_content_sha256s: tuple[str, ...]
    capability_intent_registry_version: str
    capability_intent_registry_sha256: str
    adoption_policy_version: str
    derivation_proof_sha256: str
    created_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        require_sha256(self.plan_adoption_id, "plan_adoption_id")
        for field_name in (
            "case_id",
            "question_revision_id",
            "frame_revision_id",
            "plan_revision_id",
            "capability_intent_registry_version",
            "adoption_policy_version",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if self.expected_head_version < 0:
            raise ValueError(
                "expected_head_version must be non-negative"
            )
        if not isinstance(self.authority_snapshot, AuthoritySnapshot):
            raise TypeError(
                "authority_snapshot must be AuthoritySnapshot"
            )
        for field_name in (
            "authority_snapshot_sha256",
            "frame_content_sha256",
            "plan_content_sha256",
            "capability_intent_registry_sha256",
            "derivation_proof_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        if (
            self.authority_snapshot.content_sha256
            != self.authority_snapshot_sha256
        ):
            raise ValueError("authority snapshot hash does not match")
        if (
            self.capability_intent_registry_version
            != CAPABILITY_INTENT_REGISTRY_VERSION
            or self.capability_intent_registry_sha256
            != CAPABILITY_INTENT_REGISTRY_SHA256
        ):
            raise ValueError(
                "plan adoption capability intent registry is stale"
            )
        for field_name in (
            "resolution_outcome_ids",
            "obligation_ids",
            "query_binding_ids",
        ):
            values = getattr(self, field_name)
            _require_digest_tuple(values, field_name)
        for field_name in (
            "resolution_outcome_content_sha256s",
            "resolution_admission_content_sha256s",
            "resolution_context_sha256s",
            "resolver_input_bundle_sha256s",
            "resolution_registry_content_sha256s",
            "obligation_content_sha256s",
            "query_binding_content_sha256s",
        ):
            values = getattr(self, field_name)
            _require_sha256_tuple(values, field_name)
        _require_same_length(
            self.resolution_outcome_ids,
            self.resolution_outcome_content_sha256s,
            self.resolution_admission_content_sha256s,
            self.resolution_context_sha256s,
            self.resolver_input_bundle_sha256s,
            self.resolution_registry_content_sha256s,
            label="resolution adoption",
        )
        _require_same_length(
            self.obligation_ids,
            self.obligation_content_sha256s,
            label="obligation adoption",
        )
        _require_same_length(
            self.query_binding_ids,
            self.query_binding_content_sha256s,
            label="query binding adoption",
        )
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("plan adoption requires schema epoch 3")
        require_aware_datetime(self.created_at, "created_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class PlanBundle:
    plan: WorkPlanRevision
    query_bindings: tuple[QueryBindingEnvelope, ...]
    adoption: PlanAdoptionRecord

    def __post_init__(self) -> None:
        if not isinstance(self.plan, WorkPlanRevision):
            raise TypeError("plan must be WorkPlanRevision")
        _require_typed_tuple(
            self.query_bindings,
            QueryBindingEnvelope,
            "query_bindings",
        )
        if not isinstance(self.adoption, PlanAdoptionRecord):
            raise TypeError("adoption must be PlanAdoptionRecord")


@dataclass(frozen=True, slots=True)
class ConformanceExecutionSpec:
    conformance_execution_spec_id: str
    logical_execution_id: str
    case_id: str
    frame_revision_id: str
    plan_revision_id: str
    task_id: str
    obligation_id: str
    query_binding_id: str
    query_binding_content_sha256: str
    realm: ExecutionRealm
    fixture_ref: str
    fixture_content_sha256: str
    result_contract_ref: str
    execution_policy_ref: str
    created_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for field_name in (
            "conformance_execution_spec_id",
            "logical_execution_id",
            "query_binding_id",
            "query_binding_content_sha256",
            "fixture_content_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        require_sha256(self.obligation_id, "obligation_id")
        for field_name in (
            "case_id",
            "frame_revision_id",
            "plan_revision_id",
            "task_id",
            "fixture_ref",
            "result_contract_ref",
            "execution_policy_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if self.realm is not ExecutionRealm.CONFORMANCE:
            raise ValueError(
                "Gate 3.4 execution realm must be conformance"
            )
        if not self.fixture_ref.startswith(
            CONFORMANCE_FIXTURE_PREFIX
        ):
            raise ValueError(
                "conformance fixture ref is outside trusted namespace"
            )
        if not self.result_contract_ref.startswith(
            RESULT_CONTRACT_PREFIX
        ):
            raise ValueError(
                "result contract ref is outside trusted namespace"
            )
        if not self.execution_policy_ref.startswith(
            CONFORMANCE_EXECUTION_POLICY_PREFIX
        ):
            raise ValueError(
                "execution policy ref is outside conformance namespace"
            )
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError(
                "conformance execution requires schema epoch 3"
            )
        require_aware_datetime(self.created_at, "created_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class LogicalExecutionAttempt:
    logical_execution_attempt_id: str
    logical_execution_id: str
    case_id: str
    frame_revision_id: str
    plan_revision_id: str
    task_id: str
    query_binding_id: str
    conformance_execution_spec_id: str
    query_binding_content_sha256: str
    execution_spec_content_sha256: str
    authority_snapshot: AuthoritySnapshot
    authority_snapshot_sha256: str
    attempt_number: int
    prior_attempt_id: str | None
    attempt_kind: LogicalAttemptKind
    retry_reason_code: str | None
    requested_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for field_name in (
            "logical_execution_attempt_id",
            "logical_execution_id",
            "query_binding_id",
            "conformance_execution_spec_id",
            "query_binding_content_sha256",
            "execution_spec_content_sha256",
            "authority_snapshot_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        for field_name in (
            "case_id",
            "frame_revision_id",
            "plan_revision_id",
            "task_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if not isinstance(self.authority_snapshot, AuthoritySnapshot):
            raise TypeError(
                "authority_snapshot must be AuthoritySnapshot"
            )
        if (
            self.authority_snapshot.content_sha256
            != self.authority_snapshot_sha256
        ):
            raise ValueError("attempt authority snapshot hash is stale")
        if not isinstance(self.attempt_kind, LogicalAttemptKind):
            raise TypeError(
                "attempt_kind must be LogicalAttemptKind"
            )
        if self.attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        if self.attempt_number == 1:
            if self.prior_attempt_id is not None:
                raise ValueError(
                    "initial attempt cannot reference prior attempt"
                )
            if self.attempt_kind is not LogicalAttemptKind.INITIAL:
                raise ValueError(
                    "attempt one must use initial attempt kind"
                )
            if self.retry_reason_code is not None:
                raise ValueError(
                    "initial attempt cannot carry retry reason"
                )
        else:
            if self.prior_attempt_id is None:
                raise ValueError(
                    "technical retry requires prior attempt"
                )
            if (
                self.attempt_kind
                is not LogicalAttemptKind.TECHNICAL_RETRY
            ):
                raise ValueError(
                    "later attempts must be technical retries"
                )
            require_nonempty(
                self.retry_reason_code,
                "retry_reason_code",
            )
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError(
                "logical execution attempt requires schema epoch 3"
            )
        require_aware_datetime(self.requested_at, "requested_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


def validate_logical_execution_attempt_authority(
    *,
    attempt: LogicalExecutionAttempt,
    spec: ConformanceExecutionSpec,
    binding: QueryBindingEnvelope,
    current_authority: AuthoritySnapshot,
    prior_attempt: LogicalExecutionAttempt | None = None,
) -> None:
    expected_attempt_id = content_sha256(
        {
            "kind": "logical-execution-attempt.v1",
            "logical_execution_id": attempt.logical_execution_id,
            "attempt_number": attempt.attempt_number,
            "prior_attempt_id": attempt.prior_attempt_id,
            "retry_reason_code": attempt.retry_reason_code,
        }
    )
    if attempt.logical_execution_attempt_id != expected_attempt_id:
        raise ValueError(
            "logical execution attempt is not system-derived"
        )
    if (
        current_authority.accepted_frame_revision_id is None
        or current_authority.accepted_plan_revision_id is None
        or attempt.case_id != current_authority.case_id
        or spec.case_id != current_authority.case_id
        or binding.case_id != current_authority.case_id
        or attempt.frame_revision_id
        != current_authority.accepted_frame_revision_id
        or spec.frame_revision_id
        != current_authority.accepted_frame_revision_id
        or binding.frame_revision_id
        != current_authority.accepted_frame_revision_id
        or attempt.plan_revision_id
        != current_authority.accepted_plan_revision_id
        or spec.plan_revision_id
        != current_authority.accepted_plan_revision_id
        or binding.plan_revision_id
        != current_authority.accepted_plan_revision_id
        or attempt.task_id != spec.task_id
        or attempt.task_id != binding.task_id
        or attempt.query_binding_id != spec.query_binding_id
        or attempt.query_binding_id != binding.query_binding_id
        or attempt.conformance_execution_spec_id
        != spec.conformance_execution_spec_id
        or spec.obligation_id != binding.obligation_id
        or spec.logical_execution_id
        != attempt.logical_execution_id
        or spec.query_binding_content_sha256
        != binding.content_sha256
        or spec.content_sha256
        != attempt.execution_spec_content_sha256
        or binding.content_sha256
        != attempt.query_binding_content_sha256
    ):
        raise ValueError(
            "logical execution attempt changes sealed input"
        )
    if attempt.attempt_number == 1:
        if prior_attempt is not None:
            raise ValueError(
                "initial logical execution cannot extend prior input"
            )
        return
    if (
        prior_attempt is None
        or attempt.prior_attempt_id
        != prior_attempt.logical_execution_attempt_id
        or attempt.attempt_number
        != prior_attempt.attempt_number + 1
        or attempt.logical_execution_id
        != prior_attempt.logical_execution_id
        or attempt.case_id != prior_attempt.case_id
        or attempt.frame_revision_id
        != prior_attempt.frame_revision_id
        or attempt.plan_revision_id != prior_attempt.plan_revision_id
        or attempt.task_id != prior_attempt.task_id
        or attempt.query_binding_id
        != prior_attempt.query_binding_id
        or attempt.conformance_execution_spec_id
        != prior_attempt.conformance_execution_spec_id
        or attempt.query_binding_content_sha256
        != prior_attempt.query_binding_content_sha256
        or attempt.execution_spec_content_sha256
        != prior_attempt.execution_spec_content_sha256
        or not same_business_authority(
            prior_attempt.authority_snapshot,
            attempt.authority_snapshot,
        )
    ):
        raise ValueError(
            "technical retry changes sealed execution input"
        )


def compile_plan_bundle(
    *,
    case: InvestigationCase,
    authority_snapshot: AuthoritySnapshot,
    frame: AnalysisFrameRevision,
    outcomes: tuple[MeasurementResolutionOutcome, ...],
    admissions: tuple[MeasurementResolutionAdmission, ...],
    obligations: tuple[ResolvedEvidenceObligation, ...],
    proposed_tasks: tuple[ProposedWorkTask, ...],
    plan_revision_id: str,
    revision_number: int,
    prior_plan_revision_id: str | None,
    created_by_action_id: str,
    created_at: datetime,
    revision_reason: str,
) -> PlanBundle:
    """Compile a closed Plan bundle from accepted measurement authority."""

    for field_name, value in (
        ("plan_revision_id", plan_revision_id),
        ("created_by_action_id", created_by_action_id),
        ("revision_reason", revision_reason),
    ):
        require_nonempty(value, field_name)
    require_aware_datetime(created_at, "created_at")
    if revision_number == 1:
        if prior_plan_revision_id is not None:
            raise ValueError("first Plan revision cannot have a prior")
    elif prior_plan_revision_id is None:
        raise ValueError("later Plan revisions require a prior")
    if (
        case.accepted_plan_revision_id is not None
        and prior_plan_revision_id
        != case.accepted_plan_revision_id
    ):
        raise ValueError(
            "Plan revision must extend the accepted Plan head"
        )
    _validate_case_and_snapshot(
        case=case,
        frame=frame,
        authority_snapshot=authority_snapshot,
    )
    ordered_outcomes, ordered_admissions, ordered_obligations = (
        _validate_measurement_inputs(
            frame=frame,
            authority_snapshot=authority_snapshot,
            outcomes=outcomes,
            admissions=admissions,
            obligations=obligations,
        )
    )
    _validate_proposed_tasks(
        proposed_tasks=proposed_tasks,
        obligations=ordered_obligations,
        frame=frame,
    )
    outcome_by_id = {
        item.resolution_outcome_id: item
        for item in ordered_outcomes
    }
    obligation_by_id = {
        item.obligation_id: item
        for item in ordered_obligations
    }
    task_id_by_key = {
        task.proposal_task_key: _task_id(
            plan_revision_id=plan_revision_id,
            proposal_task_key=task.proposal_task_key,
        )
        for task in proposed_tasks
    }
    query_binding_id_by_obligation = {}
    for task in proposed_tasks:
        task_id = task_id_by_key[task.proposal_task_key]
        for obligation_id in task.obligation_ids:
            obligation = obligation_by_id[obligation_id]
            if (
                obligation.execution_disposition
                is ObligationExecutionDisposition.EXECUTABLE
            ):
                query_binding_id_by_obligation[obligation_id] = (
                    _query_binding_id(
                        plan_revision_id=plan_revision_id,
                        task_id=task_id,
                        obligation=obligation,
                    )
                )

    compiled_tasks = tuple(
        _build_work_task(
            task=task,
            task_id=task_id_by_key[task.proposal_task_key],
            task_id_by_key=task_id_by_key,
            query_binding_id_by_obligation=(
                query_binding_id_by_obligation
            ),
            obligation_by_id=obligation_by_id,
            frame=frame,
        )
        for task in proposed_tasks
    )
    plan = WorkPlanRevision(
        plan_revision_id=plan_revision_id,
        case_id=case.case_id,
        frame_revision_id=frame.frame_revision_id,
        revision_number=revision_number,
        prior_plan_revision_id=prior_plan_revision_id,
        created_by_action_id=created_by_action_id,
        created_at=created_at,
        revision_reason=revision_reason,
        resolution_outcome_ids=tuple(
            item.resolution_outcome_id
            for item in ordered_outcomes
        ),
        tasks=compiled_tasks,
    )
    task_by_obligation = {
        obligation_id: task
        for task in compiled_tasks
        for obligation_id in task.obligation_ids
    }
    query_bindings = tuple(
        _build_query_binding(
            plan=plan,
            frame=frame,
            task=task_by_obligation[obligation.obligation_id],
            obligation=obligation,
            outcome=outcome_by_id[
                obligation.resolution_outcome_id
            ],
            created_at=created_at,
        )
        for obligation in ordered_obligations
        if (
            obligation.execution_disposition
            is ObligationExecutionDisposition.EXECUTABLE
        )
    )
    adoption = _build_plan_adoption(
        case=case,
        authority_snapshot=authority_snapshot,
        frame=frame,
        plan=plan,
        outcomes=ordered_outcomes,
        admissions=ordered_admissions,
        obligations=ordered_obligations,
        query_bindings=query_bindings,
        created_at=created_at,
    )
    bundle = PlanBundle(
        plan=plan,
        query_bindings=query_bindings,
        adoption=adoption,
    )
    validate_plan_bundle(
        bundle=bundle,
        case=case,
        authority_snapshot=authority_snapshot,
        frame=frame,
        outcomes=ordered_outcomes,
        admissions=ordered_admissions,
        obligations=ordered_obligations,
    )
    return bundle


def validate_plan_bundle(
    *,
    bundle: PlanBundle,
    case: InvestigationCase,
    authority_snapshot: AuthoritySnapshot,
    frame: AnalysisFrameRevision,
    outcomes: tuple[MeasurementResolutionOutcome, ...],
    admissions: tuple[MeasurementResolutionAdmission, ...],
    obligations: tuple[ResolvedEvidenceObligation, ...],
) -> None:
    """Recompute every closed binding without trusting caller summaries."""

    _validate_case_and_snapshot(
        case=case,
        frame=frame,
        authority_snapshot=authority_snapshot,
    )
    ordered_outcomes, ordered_admissions, ordered_obligations = (
        _validate_measurement_inputs(
            frame=frame,
            authority_snapshot=authority_snapshot,
            outcomes=outcomes,
            admissions=admissions,
            obligations=obligations,
        )
    )
    plan = bundle.plan
    if (
        plan.case_id != case.case_id
        or plan.frame_revision_id != frame.frame_revision_id
    ):
        raise ValueError("plan changes accepted case or Frame")
    if plan.resolution_outcome_ids != tuple(
        item.resolution_outcome_id for item in ordered_outcomes
    ):
        raise ValueError("plan resolution adoption is incomplete")
    expected_obligation_ids = tuple(
        item.obligation_id for item in ordered_obligations
    )
    actual_obligation_ids = tuple(
        obligation_id
        for task in plan.tasks
        for obligation_id in task.obligation_ids
    )
    if set(actual_obligation_ids) != set(expected_obligation_ids):
        raise ValueError("plan obligation closure is incomplete")
    if len(actual_obligation_ids) != len(set(actual_obligation_ids)):
        raise ValueError("plan obligation closure is duplicated")

    obligation_by_id = {
        item.obligation_id: item
        for item in ordered_obligations
    }
    outcome_by_id = {
        item.resolution_outcome_id: item
        for item in ordered_outcomes
    }
    binding_by_id = {
        item.query_binding_id: item
        for item in bundle.query_bindings
    }
    if len(binding_by_id) != len(bundle.query_bindings):
        raise ValueError("query binding IDs must be unique")
    expected_binding_ids: list[str] = []
    proposal_key_by_task_id = {
        task.task_id: task.proposal_task_key
        for task in plan.tasks
    }
    task_id_by_key = {
        task.proposal_task_key: task.task_id
        for task in plan.tasks
    }
    query_binding_id_by_obligation = {
        item.obligation_id: item.query_binding_id
        for item in bundle.query_bindings
    }
    for task in plan.tasks:
        if task.task_id != _task_id(
            plan_revision_id=plan.plan_revision_id,
            proposal_task_key=task.proposal_task_key,
        ):
            raise ValueError("task ID was not system-derived")
        expected_task = _build_work_task(
            task=ProposedWorkTask(
                proposal_task_key=task.proposal_task_key,
                business_purpose=task.business_purpose,
                capability_intent_ref=task.capability_intent_ref,
                obligation_ids=task.obligation_ids,
                depends_on_task_keys=tuple(
                    proposal_key_by_task_id[dependency_id]
                    for dependency_id in task.depends_on_task_ids
                ),
            ),
            task_id=task.task_id,
            task_id_by_key=task_id_by_key,
            query_binding_id_by_obligation=(
                query_binding_id_by_obligation
            ),
            obligation_by_id=obligation_by_id,
            frame=frame,
        )
        if expected_task != task:
            raise ValueError(
                "work task changes Frame-derived policy authority"
            )
        expected_task_binding_ids = []
        for obligation_id in task.obligation_ids:
            obligation = obligation_by_id.get(obligation_id)
            if obligation is None:
                raise ValueError("task references unknown obligation")
            if (
                obligation.execution_disposition
                is ObligationExecutionDisposition.EXECUTABLE
            ):
                expected_id = _query_binding_id(
                    plan_revision_id=plan.plan_revision_id,
                    task_id=task.task_id,
                    obligation=obligation,
                )
                expected_binding_ids.append(expected_id)
                expected_task_binding_ids.append(expected_id)
                binding = binding_by_id.get(expected_id)
                if binding is None:
                    raise ValueError(
                        "executable obligation lacks query binding"
                    )
                expected_binding = _build_query_binding(
                    plan=plan,
                    frame=frame,
                    task=task,
                    obligation=obligation,
                    outcome=outcome_by_id[
                        obligation.resolution_outcome_id
                    ],
                    created_at=binding.created_at,
                )
                if expected_binding != binding:
                    raise ValueError(
                        "query binding changes measurement authority"
                    )
            elif any(
                binding.obligation_id == obligation_id
                for binding in bundle.query_bindings
            ):
                raise ValueError(
                    "boundary obligation cannot create query binding"
                )
        if task.query_binding_ids != tuple(
            expected_task_binding_ids
        ):
            raise ValueError(
                "task query binding ownership is inconsistent"
            )
    if set(binding_by_id) != set(expected_binding_ids):
        raise ValueError("plan carries unowned query bindings")

    expected_adoption = _build_plan_adoption(
        case=case,
        authority_snapshot=authority_snapshot,
        frame=frame,
        plan=plan,
        outcomes=ordered_outcomes,
        admissions=ordered_admissions,
        obligations=ordered_obligations,
        query_bindings=bundle.query_bindings,
        created_at=bundle.adoption.created_at,
    )
    if expected_adoption != bundle.adoption:
        raise ValueError("plan adoption exact replay failed")


def build_conformance_execution_spec(
    *,
    query_binding: QueryBindingEnvelope,
    fixture_ref: str,
    fixture_content_sha256: str,
    result_contract_ref: str,
    execution_policy_ref: str,
    created_at: datetime,
) -> ConformanceExecutionSpec:
    require_nonempty(fixture_ref, "fixture_ref")
    require_sha256(
        fixture_content_sha256,
        "fixture_content_sha256",
    )
    require_nonempty(result_contract_ref, "result_contract_ref")
    require_nonempty(execution_policy_ref, "execution_policy_ref")
    logical_execution_id = content_sha256(
        {
            "kind": "conformance-logical-execution.v1",
            "query_binding_id": query_binding.query_binding_id,
            "query_binding_content_sha256": (
                query_binding.content_sha256
            ),
            "fixture_ref": fixture_ref,
            "fixture_content_sha256": fixture_content_sha256,
            "result_contract_ref": result_contract_ref,
            "execution_policy_ref": execution_policy_ref,
        }
    )
    spec_id = content_sha256(
        {
            "kind": "conformance-execution-spec.v1",
            "logical_execution_id": logical_execution_id,
        }
    )
    return ConformanceExecutionSpec(
        conformance_execution_spec_id=spec_id,
        logical_execution_id=logical_execution_id,
        case_id=query_binding.case_id,
        frame_revision_id=query_binding.frame_revision_id,
        plan_revision_id=query_binding.plan_revision_id,
        task_id=query_binding.task_id,
        obligation_id=query_binding.obligation_id,
        query_binding_id=query_binding.query_binding_id,
        query_binding_content_sha256=query_binding.content_sha256,
        realm=ExecutionRealm.CONFORMANCE,
        fixture_ref=fixture_ref,
        fixture_content_sha256=fixture_content_sha256,
        result_contract_ref=result_contract_ref,
        execution_policy_ref=execution_policy_ref,
        created_at=created_at,
    )


def validate_conformance_execution_spec_authority(
    *,
    spec: ConformanceExecutionSpec,
    binding: QueryBindingEnvelope,
    current_authority: AuthoritySnapshot,
) -> None:
    expected = build_conformance_execution_spec(
        query_binding=binding,
        fixture_ref=spec.fixture_ref,
        fixture_content_sha256=spec.fixture_content_sha256,
        result_contract_ref=spec.result_contract_ref,
        execution_policy_ref=spec.execution_policy_ref,
        created_at=spec.created_at,
    )
    if expected != spec:
        raise ValueError(
            "conformance execution spec is not system-derived"
        )
    if (
        current_authority.case_id != spec.case_id
        or current_authority.accepted_frame_revision_id
        != spec.frame_revision_id
        or current_authority.accepted_plan_revision_id
        != spec.plan_revision_id
    ):
        raise ValueError(
            "conformance execution requires accepted Plan"
        )


def build_logical_execution_attempt(
    *,
    spec: ConformanceExecutionSpec,
    authority_snapshot: AuthoritySnapshot,
    attempt_number: int,
    prior_attempt: LogicalExecutionAttempt | None,
    retry_reason_code: str | None,
    requested_at: datetime,
) -> LogicalExecutionAttempt:
    if authority_snapshot.case_id != spec.case_id:
        raise ValueError("attempt authority case does not match spec")
    if (
        authority_snapshot.accepted_frame_revision_id
        != spec.frame_revision_id
        or authority_snapshot.accepted_plan_revision_id
        != spec.plan_revision_id
    ):
        raise ValueError(
            "attempt requires currently accepted Frame and Plan"
        )
    if attempt_number == 1:
        if prior_attempt is not None:
            raise ValueError("initial attempt cannot have prior attempt")
        attempt_kind = LogicalAttemptKind.INITIAL
        prior_attempt_id = None
    else:
        if prior_attempt is None:
            raise ValueError("technical retry requires prior attempt")
        if attempt_number != prior_attempt.attempt_number + 1:
            raise ValueError(
                "technical retry attempt number must be contiguous"
            )
        if any(
            (
                prior_attempt.logical_execution_id
                != spec.logical_execution_id,
                prior_attempt.query_binding_id
                != spec.query_binding_id,
                prior_attempt.conformance_execution_spec_id
                != spec.conformance_execution_spec_id,
                prior_attempt.query_binding_content_sha256
                != spec.query_binding_content_sha256,
                prior_attempt.execution_spec_content_sha256
                != spec.content_sha256,
                not same_business_authority(
                    prior_attempt.authority_snapshot,
                    authority_snapshot,
                ),
            )
        ):
            raise ValueError(
                "technical retry cannot change logical execution identity"
            )
        attempt_kind = LogicalAttemptKind.TECHNICAL_RETRY
        prior_attempt_id = (
            prior_attempt.logical_execution_attempt_id
        )
    attempt_id = content_sha256(
        {
            "kind": "logical-execution-attempt.v1",
            "logical_execution_id": spec.logical_execution_id,
            "attempt_number": attempt_number,
            "prior_attempt_id": prior_attempt_id,
            "retry_reason_code": retry_reason_code,
        }
    )
    return LogicalExecutionAttempt(
        logical_execution_attempt_id=attempt_id,
        logical_execution_id=spec.logical_execution_id,
        case_id=spec.case_id,
        frame_revision_id=spec.frame_revision_id,
        plan_revision_id=spec.plan_revision_id,
        task_id=spec.task_id,
        query_binding_id=spec.query_binding_id,
        conformance_execution_spec_id=(
            spec.conformance_execution_spec_id
        ),
        query_binding_content_sha256=(
            spec.query_binding_content_sha256
        ),
        execution_spec_content_sha256=spec.content_sha256,
        authority_snapshot=authority_snapshot,
        authority_snapshot_sha256=(
            authority_snapshot.content_sha256
        ),
        attempt_number=attempt_number,
        prior_attempt_id=prior_attempt_id,
        attempt_kind=attempt_kind,
        retry_reason_code=retry_reason_code,
        requested_at=requested_at,
    )


def same_business_authority(
    left: AuthoritySnapshot,
    right: AuthoritySnapshot,
) -> bool:
    """Compare only authority that can change one sealed execution.

    Sibling obligation, Evidence, contradiction, Answer, and candidate state
    can advance while an already accepted Plan task is running. Those changes
    do not alter the task's Question/Frame/Plan authority.
    """

    return all(
        (
            left.case_id == right.case_id,
            left.mailbox_authority_epoch
            == right.mailbox_authority_epoch,
            left.accepted_question_revision_id
            == right.accepted_question_revision_id,
            left.accepted_frame_revision_id
            == right.accepted_frame_revision_id,
            left.accepted_plan_revision_id
            == right.accepted_plan_revision_id,
        )
    )


def _validate_case_and_snapshot(
    *,
    case: InvestigationCase,
    frame: AnalysisFrameRevision,
    authority_snapshot: AuthoritySnapshot,
) -> None:
    if (
        frame.case_id != case.case_id
        or authority_snapshot.case_id != case.case_id
    ):
        raise ValueError("plan inputs cross case boundary")
    if (
        case.accepted_question_revision_id
        != frame.question_revision_id
        or case.accepted_frame_revision_id
        != frame.frame_revision_id
    ):
        raise ValueError("plan requires the accepted Question and Frame")
    if (
        authority_snapshot.head_version != case.head_version
        or authority_snapshot.accepted_question_revision_id
        != case.accepted_question_revision_id
        or authority_snapshot.accepted_frame_revision_id
        != case.accepted_frame_revision_id
        or authority_snapshot.accepted_plan_revision_id
        != case.accepted_plan_revision_id
    ):
        raise ValueError("plan authority snapshot is stale")


def _validate_measurement_inputs(
    *,
    frame: AnalysisFrameRevision,
    authority_snapshot: AuthoritySnapshot,
    outcomes: tuple[MeasurementResolutionOutcome, ...],
    admissions: tuple[MeasurementResolutionAdmission, ...],
    obligations: tuple[ResolvedEvidenceObligation, ...],
) -> tuple[
    tuple[MeasurementResolutionOutcome, ...],
    tuple[MeasurementResolutionAdmission, ...],
    tuple[ResolvedEvidenceObligation, ...],
]:
    _require_typed_tuple(
        outcomes,
        MeasurementResolutionOutcome,
        "outcomes",
    )
    _require_typed_tuple(
        admissions,
        MeasurementResolutionAdmission,
        "admissions",
    )
    _require_typed_tuple(
        obligations,
        ResolvedEvidenceObligation,
        "obligations",
    )
    if not outcomes or not obligations:
        raise ValueError(
            "plan requires resolved outcomes and obligations"
        )
    outcome_by_estimand = {}
    for outcome in outcomes:
        if outcome.estimand_id in outcome_by_estimand:
            raise ValueError(
                "plan cannot adopt two outcomes for one estimand"
            )
        outcome_by_estimand[outcome.estimand_id] = outcome
    expected_estimand_ids = tuple(
        item.estimand_id for item in frame.measurement_design.estimands
    )
    if set(outcome_by_estimand) != set(expected_estimand_ids):
        raise ValueError(
            "plan must adopt one outcome for every Frame estimand"
        )
    ordered_outcomes = tuple(
        outcome_by_estimand[estimand_id]
        for estimand_id in expected_estimand_ids
    )
    admission_by_outcome = {}
    for admission in admissions:
        if admission.resolution_outcome_id in admission_by_outcome:
            raise ValueError("resolution admission is duplicated")
        admission_by_outcome[
            admission.resolution_outcome_id
        ] = admission
    if set(admission_by_outcome) != {
        item.resolution_outcome_id for item in ordered_outcomes
    }:
        raise ValueError(
            "every adopted outcome requires one admission"
        )
    ordered_admissions = tuple(
        admission_by_outcome[item.resolution_outcome_id]
        for item in ordered_outcomes
    )

    semantic_by_estimand = dict(
        zip(
            expected_estimand_ids,
            frame.semantic_measurement_ids,
            strict=True,
        )
    )
    authority_by_estimand = dict(
        zip(
            expected_estimand_ids,
            frame.authority_binding_ids,
            strict=True,
        )
    )
    expected_derivation_authority = (
        MeasurementDerivationAuthority.from_authority_snapshot(
            authority_snapshot
        )
    )
    for outcome, admission in zip(
        ordered_outcomes,
        ordered_admissions,
        strict=True,
    ):
        if any(
            (
                outcome.case_id != frame.case_id,
                outcome.question_revision_id
                != frame.question_revision_id,
                outcome.frame_revision_id
                != frame.frame_revision_id,
                outcome.semantic_measurement_id
                != semantic_by_estimand[outcome.estimand_id],
                outcome.authority_binding_id
                != authority_by_estimand[outcome.estimand_id],
                outcome.derivation_authority
                != expected_derivation_authority,
                admission.resolution_outcome_id
                != outcome.resolution_outcome_id,
                admission.frame_revision_id
                != frame.frame_revision_id,
                admission.estimand_id != outcome.estimand_id,
            )
        ):
            raise ValueError(
                "resolution outcome changes Frame authority"
            )

    selected_outcome_ids = {
        item.resolution_outcome_id for item in ordered_outcomes
    }
    selected_obligations = tuple(
        obligation
        for obligation in obligations
        if obligation.resolution_outcome_id in selected_outcome_ids
    )
    requirement_by_id = {
        item.evidence_requirement_id: item
        for item in frame.measurement_design.evidence_requirements
    }
    obligation_by_slot = {}
    for obligation in selected_obligations:
        if len(obligation.evidence_type_refs) != 1:
            raise ValueError(
                "resolved obligation must own one evidence type slot"
            )
        slot = (
            obligation.estimand_id,
            obligation.evidence_requirement_id,
            obligation.evidence_type_refs[0],
        )
        if slot in obligation_by_slot:
            raise ValueError("resolved obligation slot is duplicated")
        obligation_by_slot[slot] = obligation
    expected_slots = tuple(
        (
            estimand.estimand_id,
            requirement_id,
            evidence_type_ref,
        )
        for estimand in frame.measurement_design.estimands
        for requirement_id in estimand.evidence_requirement_ids
        for evidence_type_ref
        in requirement_by_id[
            requirement_id
        ].required_evidence_type_refs
    )
    if set(obligation_by_slot) != set(expected_slots):
        raise ValueError(
            "obligations do not close every Frame requirement"
        )
    ordered_obligations = tuple(
        obligation_by_slot[slot] for slot in expected_slots
    )
    outcome_ids = {
        item.estimand_id: item.resolution_outcome_id
        for item in ordered_outcomes
    }
    for obligation in ordered_obligations:
        requirement = requirement_by_id[
            obligation.evidence_requirement_id
        ]
        if any(
            (
                obligation.case_id != frame.case_id,
                obligation.frame_revision_id
                != frame.frame_revision_id,
                obligation.resolution_outcome_id
                != outcome_ids[obligation.estimand_id],
                obligation.derivation_authority
                != expected_derivation_authority,
                obligation.evidence_requirement_sha256
                != content_sha256(requirement),
                obligation.evidence_type_refs[0]
                not in requirement.required_evidence_type_refs,
            )
        ):
            raise ValueError(
                "obligation changes Frame or resolution authority"
            )
    return ordered_outcomes, ordered_admissions, ordered_obligations


def _validate_proposed_tasks(
    *,
    proposed_tasks: tuple[ProposedWorkTask, ...],
    obligations: tuple[ResolvedEvidenceObligation, ...],
    frame: AnalysisFrameRevision,
) -> None:
    _require_typed_tuple(
        proposed_tasks,
        ProposedWorkTask,
        "proposed_tasks",
    )
    if not proposed_tasks:
        raise ValueError("work plan requires proposed tasks")
    keys = tuple(item.proposal_task_key for item in proposed_tasks)
    if len(keys) != len(set(keys)):
        raise ValueError("proposal task keys must be unique")
    known_keys = set(keys)
    for task in proposed_tasks:
        unknown_dependencies = (
            set(task.depends_on_task_keys) - known_keys
        )
        if unknown_dependencies:
            raise ValueError(
                "proposed task has unknown dependencies"
            )
    _validate_task_dag(proposed_tasks)
    expected_ids = {item.obligation_id for item in obligations}
    actual_ids = tuple(
        obligation_id
        for task in proposed_tasks
        for obligation_id in task.obligation_ids
    )
    if set(actual_ids) != expected_ids:
        raise ValueError(
            "proposed tasks must cover every obligation"
        )
    if len(actual_ids) != len(set(actual_ids)):
        raise ValueError(
            "proposed tasks cannot duplicate obligation ownership"
        )
    obligation_by_id = {
        item.obligation_id: item for item in obligations
    }
    estimand_by_id = {
        item.estimand_id: item
        for item in frame.measurement_design.estimands
    }
    for task in proposed_tasks:
        intent = get_capability_intent_contract(
            task.capability_intent_ref
        )
        for obligation_id in task.obligation_ids:
            obligation = obligation_by_id[obligation_id]
            if (
                obligation.execution_disposition
                not in intent.allowed_execution_dispositions
            ):
                raise ValueError(
                    "capability intent cannot execute obligation "
                    "disposition"
                )
            if (
                not intent.allows_any_governed_evidence_type
                and not set(obligation.evidence_type_refs)
                <= set(intent.allowed_evidence_type_refs)
            ):
                raise ValueError(
                    "capability intent cannot fulfill obligation "
                    "evidence type"
                )
            estimand = estimand_by_id[obligation.estimand_id]
            for field_name in (
                intent.required_measurement_authority_fields
            ):
                value = getattr(estimand, field_name)
                if value is None or value == ():
                    raise ValueError(
                        "capability intent lacks required measurement "
                        "authority"
                    )


def _build_query_binding(
    *,
    plan: WorkPlanRevision,
    frame: AnalysisFrameRevision,
    task: WorkTask,
    obligation: ResolvedEvidenceObligation,
    outcome: MeasurementResolutionOutcome,
    created_at: datetime,
) -> QueryBindingEnvelope:
    if outcome.kind is not ResolutionOutcomeKind.RESOLVED_INSTANCE:
        raise ValueError(
            "executable obligation requires resolved measurement instance"
        )
    instance = outcome.resolved_instance
    if instance is None:
        raise ValueError(
            "executable obligation requires resolved instance"
        )
    estimand = next(
        item
        for item in frame.measurement_design.estimands
        if item.estimand_id == obligation.estimand_id
    )
    requirement = next(
        item
        for item in frame.measurement_design.evidence_requirements
        if (
            item.evidence_requirement_id
            == obligation.evidence_requirement_id
        )
    )
    measurement_binding = LogicalMeasurementBinding(
        claim_target_kind=estimand.claim_target_kind,
        claim_target_spec_sha256=content_sha256(
            estimand.claim_target_spec
        ),
        variable_ids=estimand.variable_ids,
        event_ids=estimand.event_ids,
        population_id=estimand.population_id,
        observation_unit_id=estimand.observation_unit_id,
        temporal_semantic_id=estimand.temporal_semantic_id,
        estimator_id=estimand.estimator_id,
        exposure_id=estimand.exposure_id,
        contrast_id=estimand.contrast_id,
        sequence_id=estimand.sequence_id,
        cohort_risk_set_id=estimand.cohort_risk_set_id,
        reconciliation_id=estimand.reconciliation_id,
        relationship_id=estimand.relationship_id,
        eligibility_id=estimand.eligibility_id,
        identification_id=estimand.identification_id,
        alternative_ids=estimand.alternative_ids,
        sensitivity_ids=estimand.sensitivity_ids,
        falsification_ids=estimand.falsification_ids,
        reversal_ids=estimand.reversal_ids,
        scope_ceiling_id=estimand.scope_ceiling_id,
        claim_strength_ceiling=(
            estimand.claim_strength_ceiling
        ),
    )
    requirement_binding = _requirement_binding(
        requirement,
        obligation,
    )
    return QueryBindingEnvelope(
        query_binding_id=_query_binding_id(
            plan_revision_id=plan.plan_revision_id,
            task_id=task.task_id,
            obligation=obligation,
        ),
        case_id=frame.case_id,
        question_revision_id=frame.question_revision_id,
        frame_revision_id=frame.frame_revision_id,
        plan_revision_id=plan.plan_revision_id,
        task_id=task.task_id,
        capability_intent_ref=task.capability_intent_ref,
        estimand_id=estimand.estimand_id,
        evidence_requirement_id=(
            requirement.evidence_requirement_id
        ),
        obligation_id=obligation.obligation_id,
        resolution_outcome_id=outcome.resolution_outcome_id,
        semantic_measurement_id=outcome.semantic_measurement_id,
        authority_binding_id=outcome.authority_binding_id,
        frame_content_sha256=frame.content_sha256,
        plan_content_sha256=plan.content_sha256,
        resolution_outcome_content_sha256=(
            outcome.content_sha256
        ),
        obligation_content_sha256=obligation.content_sha256,
        measurement_binding=measurement_binding,
        requirement_binding=requirement_binding,
        resolved_measurement_instance=instance,
        query_binding_policy_version=QUERY_BINDING_POLICY_VERSION,
        physical_compiler_contract_ref=(
            GATE4_COMPILER_CONTRACT_REF
        ),
        created_at=created_at,
    )


def _build_work_task(
    *,
    task: ProposedWorkTask,
    task_id: str,
    task_id_by_key: dict[str, str],
    query_binding_id_by_obligation: dict[str, str],
    obligation_by_id: dict[str, ResolvedEvidenceObligation],
    frame: AnalysisFrameRevision,
) -> WorkTask:
    obligation_items = tuple(
        obligation_by_id[obligation_id]
        for obligation_id in task.obligation_ids
    )
    target_estimand_set = {
        item.estimand_id for item in obligation_items
    }
    target_estimand_ids = tuple(
        item.estimand_id
        for item in frame.measurement_design.estimands
        if item.estimand_id in target_estimand_set
    )
    requirement_ids = {
        item.evidence_requirement_id
        for item in obligation_items
    }
    completion_specs = tuple(
        item
        for item in frame.measurement_design.completion_specs
        if (
            set(item.target_estimand_ids) & target_estimand_set
            and set(item.required_evidence_requirement_ids)
            & requirement_ids
        )
    )
    if not completion_specs:
        raise ValueError(
            "task obligations have no Frame completion path"
        )
    return WorkTask(
        task_id=task_id,
        proposal_task_key=task.proposal_task_key,
        business_purpose=task.business_purpose,
        capability_intent_ref=task.capability_intent_ref,
        target_estimand_ids=target_estimand_ids,
        obligation_ids=task.obligation_ids,
        query_binding_ids=tuple(
            query_binding_id_by_obligation[obligation_id]
            for obligation_id in task.obligation_ids
            if obligation_id in query_binding_id_by_obligation
        ),
        completion_spec_ids=tuple(
            item.completion_spec_id for item in completion_specs
        ),
        execution_success_policy_refs=_ordered_unique(
            tuple(item.success_policy_ref for item in completion_specs)
        ),
        execution_degrade_policy_refs=_ordered_unique(
            tuple(item.degrade_policy_ref for item in completion_specs)
        ),
        execution_stop_policy_refs=_ordered_unique(
            tuple(item.stop_policy_ref for item in completion_specs)
        ),
        depends_on_task_ids=tuple(
            task_id_by_key[key]
            for key in task.depends_on_task_keys
        ),
    )


def _requirement_binding(
    requirement: EvidenceRequirementSpec,
    obligation: ResolvedEvidenceObligation,
) -> EvidenceRequirementBinding:
    return EvidenceRequirementBinding(
        evidence_requirement_id=(
            requirement.evidence_requirement_id
        ),
        required_evidence_type_refs=(
            requirement.required_evidence_type_refs
        ),
        obligation_evidence_type_refs=(
            obligation.evidence_type_refs
        ),
        composition=requirement.composition,
        minimum_count=requirement.minimum_count,
        minimum_strength=requirement.minimum_strength,
        scope_id=requirement.scope_id,
        exposure_id=requirement.exposure_id,
        contradiction_policy_ref=(
            requirement.contradiction_policy_ref
        ),
        boundary_policy=requirement.boundary_policy,
        allowed_boundary_codes=requirement.allowed_boundary_codes,
        linked_falsification_ids=(
            requirement.linked_falsification_ids
        ),
        linked_reversal_ids=requirement.linked_reversal_ids,
    )


def _build_plan_adoption(
    *,
    case: InvestigationCase,
    authority_snapshot: AuthoritySnapshot,
    frame: AnalysisFrameRevision,
    plan: WorkPlanRevision,
    outcomes: tuple[MeasurementResolutionOutcome, ...],
    admissions: tuple[MeasurementResolutionAdmission, ...],
    obligations: tuple[ResolvedEvidenceObligation, ...],
    query_bindings: tuple[QueryBindingEnvelope, ...],
    created_at: datetime,
) -> PlanAdoptionRecord:
    material = {
        "case_id": case.case_id,
        "question_revision_id": frame.question_revision_id,
        "frame_revision_id": frame.frame_revision_id,
        "plan_revision_id": plan.plan_revision_id,
        "expected_head_version": case.head_version,
        "authority_snapshot_sha256": (
            authority_snapshot.content_sha256
        ),
        "frame_content_sha256": frame.content_sha256,
        "plan_content_sha256": plan.content_sha256,
        "resolution_outcome_ids": tuple(
            item.resolution_outcome_id for item in outcomes
        ),
        "resolution_outcome_content_sha256s": tuple(
            item.content_sha256 for item in outcomes
        ),
        "resolution_admission_content_sha256s": tuple(
            content_sha256(item) for item in admissions
        ),
        "resolution_context_sha256s": tuple(
            item.resolution_context_sha256
            for item in admissions
        ),
        "resolver_input_bundle_sha256s": tuple(
            item.resolver_input_bundle_sha256
            for item in admissions
        ),
        "resolution_registry_content_sha256s": tuple(
            item.registry_content_sha256 for item in admissions
        ),
        "obligation_ids": tuple(
            item.obligation_id for item in obligations
        ),
        "obligation_content_sha256s": tuple(
            item.content_sha256 for item in obligations
        ),
        "query_binding_ids": tuple(
            item.query_binding_id for item in query_bindings
        ),
        "query_binding_content_sha256s": tuple(
            item.content_sha256 for item in query_bindings
        ),
        "capability_intent_registry_version": (
            CAPABILITY_INTENT_REGISTRY_VERSION
        ),
        "capability_intent_registry_sha256": (
            CAPABILITY_INTENT_REGISTRY_SHA256
        ),
        "adoption_policy_version": (
            PLAN_ADOPTION_POLICY_VERSION
        ),
    }
    proof_sha256 = content_sha256(material)
    adoption_id = content_sha256(
        {
            "kind": "plan-adoption-record.v1",
            "derivation_proof_sha256": proof_sha256,
        }
    )
    return PlanAdoptionRecord(
        plan_adoption_id=adoption_id,
        case_id=case.case_id,
        question_revision_id=frame.question_revision_id,
        frame_revision_id=frame.frame_revision_id,
        plan_revision_id=plan.plan_revision_id,
        expected_head_version=case.head_version,
        authority_snapshot=authority_snapshot,
        authority_snapshot_sha256=(
            authority_snapshot.content_sha256
        ),
        frame_content_sha256=frame.content_sha256,
        plan_content_sha256=plan.content_sha256,
        resolution_outcome_ids=material[
            "resolution_outcome_ids"
        ],
        resolution_outcome_content_sha256s=material[
            "resolution_outcome_content_sha256s"
        ],
        resolution_admission_content_sha256s=material[
            "resolution_admission_content_sha256s"
        ],
        resolution_context_sha256s=material[
            "resolution_context_sha256s"
        ],
        resolver_input_bundle_sha256s=material[
            "resolver_input_bundle_sha256s"
        ],
        resolution_registry_content_sha256s=material[
            "resolution_registry_content_sha256s"
        ],
        obligation_ids=material["obligation_ids"],
        obligation_content_sha256s=material[
            "obligation_content_sha256s"
        ],
        query_binding_ids=material["query_binding_ids"],
        query_binding_content_sha256s=material[
            "query_binding_content_sha256s"
        ],
        capability_intent_registry_version=(
            CAPABILITY_INTENT_REGISTRY_VERSION
        ),
        capability_intent_registry_sha256=(
            CAPABILITY_INTENT_REGISTRY_SHA256
        ),
        adoption_policy_version=(
            PLAN_ADOPTION_POLICY_VERSION
        ),
        derivation_proof_sha256=proof_sha256,
        created_at=created_at,
    )


def _task_id(
    *,
    plan_revision_id: str,
    proposal_task_key: str,
) -> str:
    return content_sha256(
        {
            "kind": "work-task.v1",
            "plan_revision_id": plan_revision_id,
            "proposal_task_key": proposal_task_key,
        }
    )


def _query_binding_id(
    *,
    plan_revision_id: str,
    task_id: str,
    obligation: ResolvedEvidenceObligation,
) -> str:
    return content_sha256(
        {
            "kind": "query-binding-envelope.v1",
            "plan_revision_id": plan_revision_id,
            "task_id": task_id,
            "obligation_id": obligation.obligation_id,
            "resolution_outcome_id": (
                obligation.resolution_outcome_id
            ),
            "closure_definition_sha256": (
                obligation.closure_definition_sha256
            ),
        }
    )


def _validate_task_dag(
    tasks: tuple[ProposedWorkTask, ...],
) -> None:
    dependencies = {
        task.proposal_task_key: task.depends_on_task_keys
        for task in tasks
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_key: str) -> None:
        if task_key in visited:
            return
        if task_key in visiting:
            raise ValueError("proposed task dependencies must be acyclic")
        visiting.add(task_key)
        for dependency in dependencies[task_key]:
            visit(dependency)
        visiting.remove(task_key)
        visited.add(task_key)

    for task_key in dependencies:
        visit(task_key)


def _require_string_tuple(
    values: tuple[str, ...],
    field_name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must be unique")
    for value in values:
        require_nonempty(value, field_name)


def _require_digest_tuple(
    values: tuple[str, ...],
    field_name: str,
) -> None:
    _require_string_tuple(values, field_name)
    for value in values:
        require_sha256(value, field_name)


def _require_sha256_tuple(
    values: tuple[str, ...],
    field_name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for value in values:
        require_sha256(value, field_name)


def _require_optional_ref(
    value: str | None,
    field_name: str,
) -> None:
    if value is not None:
        require_nonempty(value, field_name)


def _require_typed_tuple(
    values: tuple[object, ...],
    expected_type: type[object],
    field_name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for value in values:
        if not isinstance(value, expected_type):
            raise TypeError(
                f"{field_name} must contain {expected_type.__name__}"
            )


def _require_same_length(
    *values: tuple[str, ...],
    label: str,
) -> None:
    if not values or len({len(value) for value in values}) != 1:
        raise ValueError(f"{label} fields must align")


def _ordered_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
