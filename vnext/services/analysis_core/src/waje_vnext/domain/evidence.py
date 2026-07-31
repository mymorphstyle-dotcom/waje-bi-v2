"""Immutable evidence authority derived from sealed measurement execution.

This module deliberately owns no persistence and performs no provider calls.
Every authoritative identifier is derived from canonical typed input, and each
validator recomputes that derivation instead of trusting caller summaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .async_runtime import AuthoritySnapshot, OperationIdentity
from .canonical import (
    content_sha256,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
)
from .identity import (
    ScopeRelationKind,
    ScopeRelationProof,
    scope_relation,
)
from .measurement import (
    ClaimStrengthCeiling,
    MeasurementResolutionOutcome,
    ObligationExecutionDisposition,
    ResolvedEvidenceObligation,
    ResolvedExposureFact,
    ResolvedWindow,
    ResolutionOutcomeKind,
    ScopeExpression,
)
from .planning import (
    ConformanceExecutionSpec,
    LogicalExecutionAttempt,
    PlanAdoptionRecord,
    QueryBindingEnvelope,
    validate_conformance_execution_spec_authority,
    validate_logical_execution_attempt_authority,
)


SCHEMA_EPOCH = 3
EVIDENCE_IDENTITY_VERSION = "evidence-identity.g3.5.v1"
EVIDENCE_ADMISSION_POLICY_VERSION = "evidence-admission.g3.5.v1"
EVIDENCE_VALIDITY_POLICY_VERSION = "evidence-validity.g3.5.v1"
EVIDENCE_USE_POLICY_VERSION = "evidence-use.g3.5.v1"
OBLIGATION_SATISFACTION_POLICY_VERSION = (
    "obligation-satisfaction.g3.5.v1"
)
SCOPE_PROOF_POLICY_VERSION = "evidence-scope.g3.5.v1"


class ExecutionProvenanceKind(StrEnum):
    CONFORMANCE = "conformance"
    PHYSICAL_QUERY = "physical_query"


class ResultMaterialKind(StrEnum):
    INLINE = "inline"
    STABLE_HANDLE = "stable_handle"


class EvidenceAdmissionProfile(StrEnum):
    CONFORMANCE = "conformance"
    PRODUCTION = "production"


class EvidenceAdmissionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class EvidenceValidityStatus(StrEnum):
    ADMITTED_VALID = "admitted_valid"
    NEVER_ADMITTED = "never_admitted"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class ObligationSatisfactionStatus(StrEnum):
    OPEN = "open"
    SATISFIED = "satisfied"
    BOUNDARY = "boundary"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class EvidenceDataContext:
    resolution_context_content_sha256: str
    data_contract_version_ref: str
    snapshot_release_ref: str
    coverage_watermark_ref: str
    late_arrival_policy_ref: str
    timezone: str
    business_day_cutoff: str
    calendar_version_ref: str
    holiday_version_ref: str | None
    fiscal_version_ref: str | None

    def __post_init__(self) -> None:
        require_sha256(
            self.resolution_context_content_sha256,
            "resolution_context_content_sha256",
        )
        for name in (
            "data_contract_version_ref",
            "snapshot_release_ref",
            "coverage_watermark_ref",
            "late_arrival_policy_ref",
            "timezone",
            "business_day_cutoff",
            "calendar_version_ref",
        ):
            require_nonempty(getattr(self, name), name)
        for name in ("holiday_version_ref", "fiscal_version_ref"):
            value = getattr(self, name)
            if value is not None:
                require_nonempty(value, name)


@dataclass(frozen=True, slots=True)
class AdmissionAuthorityFence:
    case_id: str
    mailbox_authority_epoch: int
    accepted_question_revision_id: str
    accepted_frame_revision_id: str
    accepted_plan_revision_id: str
    active_frame_candidate_generation: int
    active_frame_candidate_sha256: str | None

    def __post_init__(self) -> None:
        for name in (
            "case_id",
            "accepted_question_revision_id",
            "accepted_frame_revision_id",
            "accepted_plan_revision_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.mailbox_authority_epoch < 0:
            raise ValueError("mailbox_authority_epoch must be non-negative")
        if self.active_frame_candidate_generation < 0:
            raise ValueError(
                "active_frame_candidate_generation must be non-negative"
            )
        if self.active_frame_candidate_sha256 is not None:
            require_sha256(
                self.active_frame_candidate_sha256,
                "active_frame_candidate_sha256",
            )

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class ConformanceExecutionProvenance:
    kind: ExecutionProvenanceKind
    logical_execution_id: str
    query_binding_id: str
    query_binding_content_sha256: str
    execution_spec_id: str
    execution_spec_content_sha256: str
    logical_execution_attempt_id: str
    logical_execution_attempt_content_sha256: str
    fixture_ref: str
    fixture_content_sha256: str
    result_contract_ref: str
    execution_policy_ref: str

    def __post_init__(self) -> None:
        if self.kind is not ExecutionProvenanceKind.CONFORMANCE:
            raise ValueError("conformance provenance has the wrong tag")
        for name in (
            "logical_execution_id",
            "query_binding_id",
            "query_binding_content_sha256",
            "execution_spec_id",
            "execution_spec_content_sha256",
            "logical_execution_attempt_id",
            "logical_execution_attempt_content_sha256",
            "fixture_content_sha256",
        ):
            require_sha256(getattr(self, name), name)
        for name in (
            "fixture_ref",
            "result_contract_ref",
            "execution_policy_ref",
        ):
            require_nonempty(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class PhysicalQueryExecutionProvenance:
    kind: ExecutionProvenanceKind
    logical_execution_id: str
    query_binding_id: str
    query_binding_content_sha256: str
    query_spec_id: str
    query_spec_content_sha256: str
    capability_invocation_id: str
    capability_invocation_content_sha256: str
    provider_receipt_id: str
    provider_receipt_content_sha256: str
    compiler_contract_ref: str

    def __post_init__(self) -> None:
        if self.kind is not ExecutionProvenanceKind.PHYSICAL_QUERY:
            raise ValueError("physical provenance has the wrong tag")
        for name in (
            "logical_execution_id",
            "query_binding_id",
            "query_binding_content_sha256",
            "query_spec_id",
            "query_spec_content_sha256",
            "capability_invocation_id",
            "capability_invocation_content_sha256",
            "provider_receipt_id",
            "provider_receipt_content_sha256",
        ):
            require_sha256(getattr(self, name), name)
        require_nonempty(self.compiler_contract_ref, "compiler_contract_ref")


type ExecutionProvenance = (
    ConformanceExecutionProvenance | PhysicalQueryExecutionProvenance
)


@dataclass(frozen=True, slots=True)
class InlineResultMaterial:
    kind: ResultMaterialKind
    payload_content_sha256: str
    schema_ref: str
    row_count: int
    byte_count: int

    def __post_init__(self) -> None:
        if self.kind is not ResultMaterialKind.INLINE:
            raise ValueError("inline result material has the wrong tag")
        require_sha256(
            self.payload_content_sha256,
            "payload_content_sha256",
        )
        require_nonempty(self.schema_ref, "schema_ref")
        if self.row_count < 0 or self.byte_count < 0:
            raise ValueError("result sizes must be non-negative")


@dataclass(frozen=True, slots=True)
class StableResultHandle:
    kind: ResultMaterialKind
    result_handle_id: str
    result_content_sha256: str
    schema_ref: str
    row_count: int
    storage_contract_ref: str
    retention_class_ref: str

    def __post_init__(self) -> None:
        if self.kind is not ResultMaterialKind.STABLE_HANDLE:
            raise ValueError("stable result handle has the wrong tag")
        require_sha256(self.result_handle_id, "result_handle_id")
        require_sha256(
            self.result_content_sha256,
            "result_content_sha256",
        )
        for name in (
            "schema_ref",
            "storage_contract_ref",
            "retention_class_ref",
        ):
            require_nonempty(getattr(self, name), name)
        if self.row_count < 0:
            raise ValueError("row_count must be non-negative")


type ResultMaterial = InlineResultMaterial | StableResultHandle


@dataclass(frozen=True, slots=True)
class EstimatePayload:
    estimate_schema_ref: str
    estimate_content_sha256: str
    uncertainty_schema_ref: str | None
    uncertainty_content_sha256: str | None

    def __post_init__(self) -> None:
        require_nonempty(self.estimate_schema_ref, "estimate_schema_ref")
        require_sha256(
            self.estimate_content_sha256,
            "estimate_content_sha256",
        )
        if (self.uncertainty_schema_ref is None) != (
            self.uncertainty_content_sha256 is None
        ):
            raise ValueError(
                "uncertainty schema and content digest must be paired"
            )
        if self.uncertainty_schema_ref is not None:
            require_nonempty(
                self.uncertainty_schema_ref,
                "uncertainty_schema_ref",
            )
            require_sha256(
                self.uncertainty_content_sha256,
                "uncertainty_content_sha256",
            )


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_record_id: str
    run_id: str
    profile: EvidenceAdmissionProfile
    case_id: str
    question_revision_id: str
    frame_revision_id: str
    plan_revision_id: str
    task_id: str
    estimand_id: str
    evidence_requirement_id: str
    obligation_id: str
    resolution_outcome_id: str
    resolution_outcome_content_sha256: str
    resolution_id: str
    semantic_measurement_id: str
    authority_binding_id: str
    query_binding_id: str
    query_binding_content_sha256: str
    logical_execution_id: str
    execution_provenance: ExecutionProvenance
    data_context: EvidenceDataContext
    evidence_type_ref: str
    evidence_strength: ClaimStrengthCeiling
    actual_scope: ScopeExpression
    actual_windows: tuple[ResolvedWindow, ...]
    actual_exposure_facts: tuple[ResolvedExposureFact, ...]
    actual_grain_ref: str
    actual_unit_ref: str
    actual_aggregation_path_ref: str
    estimate: EstimatePayload
    result_material: ResultMaterial
    business_summary: str
    limitation_refs: tuple[str, ...]
    produced_at: datetime
    identity_version: str = EVIDENCE_IDENTITY_VERSION
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "evidence_record_id",
            "resolution_outcome_content_sha256",
            "semantic_measurement_id",
            "authority_binding_id",
            "query_binding_id",
            "query_binding_content_sha256",
            "logical_execution_id",
        ):
            require_sha256(getattr(self, name), name)
        for name in (
            "run_id",
            "case_id",
            "question_revision_id",
            "frame_revision_id",
            "plan_revision_id",
            "task_id",
            "estimand_id",
            "evidence_requirement_id",
            "resolution_id",
            "evidence_type_ref",
            "actual_grain_ref",
            "actual_unit_ref",
            "actual_aggregation_path_ref",
            "business_summary",
        ):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.profile, EvidenceAdmissionProfile):
            raise TypeError("profile has unsupported type")
        require_sha256(self.obligation_id, "obligation_id")
        require_nonempty(
            self.resolution_outcome_id,
            "resolution_outcome_id",
        )
        if not isinstance(
            self.execution_provenance,
            (ConformanceExecutionProvenance, PhysicalQueryExecutionProvenance),
        ):
            raise TypeError("execution_provenance has unsupported type")
        if not isinstance(self.data_context, EvidenceDataContext):
            raise TypeError("data_context must be EvidenceDataContext")
        if not isinstance(self.evidence_strength, ClaimStrengthCeiling):
            raise TypeError("evidence_strength must be ClaimStrengthCeiling")
        if not isinstance(self.actual_scope, ScopeExpression):
            raise TypeError("actual_scope must be ScopeExpression")
        if not isinstance(self.actual_windows, tuple) or not all(
            isinstance(item, ResolvedWindow) for item in self.actual_windows
        ):
            raise TypeError("actual_windows must be a typed tuple")
        if not isinstance(self.actual_exposure_facts, tuple) or not all(
            isinstance(item, ResolvedExposureFact)
            for item in self.actual_exposure_facts
        ):
            raise TypeError("actual_exposure_facts must be a typed tuple")
        if len(self.limitation_refs) != len(set(self.limitation_refs)):
            raise ValueError("limitation_refs must be unique")
        for item in self.limitation_refs:
            require_nonempty(item, "limitation_refs")
        require_aware_datetime(self.produced_at, "produced_at")
        if self.identity_version != EVIDENCE_IDENTITY_VERSION:
            raise ValueError("evidence identity version is unsupported")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("EvidenceRecord requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class CapabilityResultEnvelope:
    capability_result_envelope_id: str
    run_id: str
    schedule_id: str
    dispatch_record_id: str
    outbox_message_id: str
    logical_execution_attempt_id: str
    logical_execution_attempt_content_sha256: str
    capability_invocation_id: str
    case_id: str
    frame_revision_id: str
    plan_revision_id: str
    task_id: str
    obligation_id: str
    query_binding_id: str
    query_binding_content_sha256: str
    execution_provenance: ExecutionProvenance
    result_material: ResultMaterial
    evidence_record: EvidenceRecord
    produced_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "capability_result_envelope_id",
            "logical_execution_attempt_id",
            "logical_execution_attempt_content_sha256",
            "capability_invocation_id",
            "query_binding_id",
            "query_binding_content_sha256",
        ):
            require_sha256(getattr(self, name), name)
        for name in (
            "run_id",
            "schedule_id",
            "dispatch_record_id",
            "outbox_message_id",
            "case_id",
            "frame_revision_id",
            "plan_revision_id",
            "task_id",
        ):
            require_nonempty(getattr(self, name), name)
        require_sha256(self.obligation_id, "obligation_id")
        require_aware_datetime(self.produced_at, "produced_at")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("result envelope requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class CapabilityResultReceipt:
    capability_result_receipt_id: str
    run_id: str
    schedule_id: str
    dispatch_record_id: str
    outbox_message_id: str
    delivery_owner_id: str
    delivery_fencing_token: int
    logical_execution_attempt_id: str
    logical_execution_attempt_content_sha256: str
    capability_result_envelope_id: str
    capability_result_envelope_content_sha256: str
    capability_invocation_id: str
    query_binding_id: str
    execution_provenance_content_sha256: str
    result_material_content_sha256: str
    operation_identity: OperationIdentity
    idempotency_key: str
    correlation_id: str
    received_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "capability_result_receipt_id",
            "logical_execution_attempt_id",
            "logical_execution_attempt_content_sha256",
            "capability_result_envelope_id",
            "capability_result_envelope_content_sha256",
            "capability_invocation_id",
            "query_binding_id",
            "execution_provenance_content_sha256",
            "result_material_content_sha256",
        ):
            require_sha256(getattr(self, name), name)
        for name in (
            "run_id",
            "schedule_id",
            "dispatch_record_id",
            "outbox_message_id",
            "delivery_owner_id",
            "idempotency_key",
            "correlation_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.delivery_fencing_token < 1:
            raise ValueError("delivery_fencing_token must be positive")
        if not isinstance(self.operation_identity, OperationIdentity):
            raise TypeError(
                "operation_identity must be OperationIdentity"
            )
        if (
            self.idempotency_key
            != self.operation_identity.idempotency_key
            or self.correlation_id
            != self.operation_identity.correlation_id
            or self.correlation_id != self.run_id
            or self.operation_identity.causation_id
            != self.outbox_message_id
        ):
            raise ValueError(
                "receipt operation identity does not bind run"
            )
        require_aware_datetime(self.received_at, "received_at")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("result receipt requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class EvidenceAdmissionRecord:
    evidence_admission_id: str
    profile: EvidenceAdmissionProfile
    status: EvidenceAdmissionStatus
    evidence_record_id: str
    evidence_record_content_sha256: str
    capability_result_envelope_id: str
    capability_result_envelope_content_sha256: str
    capability_result_receipt_id: str
    capability_result_receipt_content_sha256: str
    obligation_id: str
    obligation_content_sha256: str
    query_binding_id: str
    query_binding_content_sha256: str
    plan_adoption_id: str
    plan_adoption_content_sha256: str
    authority_fence: AdmissionAuthorityFence
    authority_fence_content_sha256: str
    authority_snapshot: AuthoritySnapshot
    authority_snapshot_content_sha256: str
    expected_scope: ScopeExpression
    scope_relation_proof: ScopeRelationProof
    window_proof_sha256: str
    exposure_proof_sha256: str
    unit_proof_sha256: str
    grain_proof_sha256: str
    data_version_proof_sha256: str
    effective_strength: ClaimStrengthCeiling
    reason_codes: tuple[str, ...]
    derived_input_sha256: str
    policy_version: str
    admitted_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "evidence_admission_id",
            "evidence_record_id",
            "evidence_record_content_sha256",
            "capability_result_envelope_id",
            "capability_result_envelope_content_sha256",
            "capability_result_receipt_id",
            "capability_result_receipt_content_sha256",
            "obligation_id",
            "obligation_content_sha256",
            "query_binding_id",
            "query_binding_content_sha256",
            "plan_adoption_id",
            "plan_adoption_content_sha256",
            "authority_fence_content_sha256",
            "authority_snapshot_content_sha256",
            "window_proof_sha256",
            "exposure_proof_sha256",
            "unit_proof_sha256",
            "grain_proof_sha256",
            "data_version_proof_sha256",
            "derived_input_sha256",
        ):
            require_sha256(getattr(self, name), name)
        if not isinstance(self.profile, EvidenceAdmissionProfile):
            raise TypeError("profile has unsupported type")
        if not isinstance(self.status, EvidenceAdmissionStatus):
            raise TypeError("status has unsupported type")
        if self.authority_snapshot.content_sha256 != (
            self.authority_snapshot_content_sha256
        ):
            raise ValueError("admission authority snapshot hash is stale")
        if not isinstance(
            self.authority_fence,
            AdmissionAuthorityFence,
        ):
            raise TypeError(
                "authority_fence must be AdmissionAuthorityFence"
            )
        if (
            self.authority_fence.content_sha256
            != self.authority_fence_content_sha256
        ):
            raise ValueError("admission authority fence hash is stale")
        if not isinstance(self.expected_scope, ScopeExpression):
            raise TypeError("expected_scope must be ScopeExpression")
        if not isinstance(self.scope_relation_proof, ScopeRelationProof):
            raise TypeError("scope relation proof is required")
        if not isinstance(self.effective_strength, ClaimStrengthCeiling):
            raise TypeError("effective_strength has unsupported type")
        if not self.reason_codes:
            raise ValueError("admission requires at least one reason code")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("admission reason codes must be unique")
        for item in self.reason_codes:
            require_nonempty(item, "reason_codes")
        if self.policy_version != EVIDENCE_ADMISSION_POLICY_VERSION:
            raise ValueError("admission policy version is unsupported")
        require_aware_datetime(self.admitted_at, "admitted_at")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("admission requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class EvidenceValidityRecord:
    evidence_validity_id: str
    evidence_record_id: str
    evidence_admission_id: str
    evidence_admission_content_sha256: str
    prior_evidence_validity_id: str | None
    prior_evidence_validity_content_sha256: str | None
    status: EvidenceValidityStatus
    reason_code: str
    policy_version: str
    recorded_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "evidence_validity_id",
            "evidence_record_id",
            "evidence_admission_id",
            "evidence_admission_content_sha256",
        ):
            require_sha256(getattr(self, name), name)
        if (self.prior_evidence_validity_id is None) != (
            self.prior_evidence_validity_content_sha256 is None
        ):
            raise ValueError("validity prior identity and hash must be paired")
        if self.prior_evidence_validity_id is not None:
            require_sha256(
                self.prior_evidence_validity_id,
                "prior_evidence_validity_id",
            )
            require_sha256(
                self.prior_evidence_validity_content_sha256,
                "prior_evidence_validity_content_sha256",
            )
        if not isinstance(self.status, EvidenceValidityStatus):
            raise TypeError("validity status has unsupported type")
        require_nonempty(self.reason_code, "reason_code")
        if self.policy_version != EVIDENCE_VALIDITY_POLICY_VERSION:
            raise ValueError("validity policy version is unsupported")
        require_aware_datetime(self.recorded_at, "recorded_at")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("validity record requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class EvidenceUseBinding:
    evidence_use_binding_id: str
    evidence_record_id: str
    evidence_record_content_sha256: str
    evidence_admission_id: str
    evidence_admission_content_sha256: str
    evidence_validity_id: str
    evidence_validity_content_sha256: str
    case_id: str
    question_revision_id: str
    frame_revision_id: str
    plan_revision_id: str
    estimand_id: str
    evidence_requirement_id: str
    obligation_id: str
    resolution_outcome_id: str
    answer_candidate_id: str
    proposal_claim_key: str
    claim_scope: ScopeExpression
    scope_relation_proof: ScopeRelationProof
    requested_claim_strength: ClaimStrengthCeiling
    effective_claim_strength: ClaimStrengthCeiling
    limitation_refs: tuple[str, ...]
    policy_version: str
    bound_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "evidence_use_binding_id",
            "evidence_record_id",
            "evidence_record_content_sha256",
            "evidence_admission_id",
            "evidence_admission_content_sha256",
            "evidence_validity_id",
            "evidence_validity_content_sha256",
            "obligation_id",
            "answer_candidate_id",
        ):
            require_sha256(getattr(self, name), name)
        for name in (
            "case_id",
            "question_revision_id",
            "frame_revision_id",
            "plan_revision_id",
            "estimand_id",
            "evidence_requirement_id",
            "resolution_outcome_id",
            "proposal_claim_key",
        ):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.claim_scope, ScopeExpression):
            raise TypeError("claim_scope must be ScopeExpression")
        if not isinstance(self.scope_relation_proof, ScopeRelationProof):
            raise TypeError("scope relation proof is required")
        if not isinstance(
            self.requested_claim_strength,
            ClaimStrengthCeiling,
        ) or not isinstance(
            self.effective_claim_strength,
            ClaimStrengthCeiling,
        ):
            raise TypeError("claim strength has unsupported type")
        if len(self.limitation_refs) != len(set(self.limitation_refs)):
            raise ValueError("limitation_refs must be unique")
        if self.policy_version != EVIDENCE_USE_POLICY_VERSION:
            raise ValueError("evidence use policy is unsupported")
        require_aware_datetime(self.bound_at, "bound_at")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("evidence use binding requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class ObligationSatisfactionRecord:
    obligation_satisfaction_id: str
    obligation_id: str
    obligation_content_sha256: str
    prior_obligation_satisfaction_id: str | None
    prior_obligation_satisfaction_content_sha256: str | None
    status: ObligationSatisfactionStatus
    evidence_admission_ids: tuple[str, ...]
    evidence_admission_content_sha256s: tuple[str, ...]
    evidence_validity_ids: tuple[str, ...]
    evidence_validity_content_sha256s: tuple[str, ...]
    boundary_resolution_outcome_id: str | None
    input_set_sha256: str
    reason_code: str
    policy_version: str
    recorded_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "obligation_satisfaction_id",
            "obligation_id",
            "obligation_content_sha256",
            "input_set_sha256",
        ):
            require_sha256(getattr(self, name), name)
        if (self.prior_obligation_satisfaction_id is None) != (
            self.prior_obligation_satisfaction_content_sha256 is None
        ):
            raise ValueError("satisfaction prior identity and hash must be paired")
        for values, name in (
            (self.evidence_admission_ids, "evidence_admission_ids"),
            (
                self.evidence_admission_content_sha256s,
                "evidence_admission_content_sha256s",
            ),
            (self.evidence_validity_ids, "evidence_validity_ids"),
            (
                self.evidence_validity_content_sha256s,
                "evidence_validity_content_sha256s",
            ),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must be unique")
            for item in values:
                require_sha256(item, name)
        if len(self.evidence_admission_ids) != len(
            self.evidence_admission_content_sha256s
        ) or len(self.evidence_validity_ids) != len(
            self.evidence_validity_content_sha256s
        ):
            raise ValueError("satisfaction identity/hash tuples must align")
        if not isinstance(self.status, ObligationSatisfactionStatus):
            raise TypeError("satisfaction status has unsupported type")
        require_nonempty(self.reason_code, "reason_code")
        if self.policy_version != OBLIGATION_SATISFACTION_POLICY_VERSION:
            raise ValueError("satisfaction policy is unsupported")
        require_aware_datetime(self.recorded_at, "recorded_at")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("satisfaction record requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


_STRENGTH_RANK = {
    ClaimStrengthCeiling.BOUNDARY_ONLY: 0,
    ClaimStrengthCeiling.DESCRIPTIVE: 1,
    ClaimStrengthCeiling.ACCOUNTING: 2,
    ClaimStrengthCeiling.ASSOCIATIONAL: 3,
    ClaimStrengthCeiling.CAUSAL: 4,
}
_COVERING_SCOPE_RELATIONS = {
    ScopeRelationKind.EXACT,
    ScopeRelationKind.SUPERSET,
    ScopeRelationKind.LAWFUL_PROJECTION,
    ScopeRelationKind.LAWFUL_AGGREGATION,
}


def _id(kind: str, payload: object) -> str:
    return content_sha256(
        {
            "identity_version": EVIDENCE_IDENTITY_VERSION,
            "kind": kind,
            "payload": payload,
        }
    )


def _data_context(binding: QueryBindingEnvelope) -> EvidenceDataContext:
    context = binding.resolved_measurement_instance.context
    return EvidenceDataContext(
        resolution_context_content_sha256=content_sha256(context),
        data_contract_version_ref=context.data_contract_version_ref,
        snapshot_release_ref=context.snapshot_release_ref,
        coverage_watermark_ref=context.coverage_watermark_ref,
        late_arrival_policy_ref=context.late_arrival_policy_ref,
        timezone=context.timezone,
        business_day_cutoff=context.business_day_cutoff,
        calendar_version_ref=context.calendar_version_ref,
        holiday_version_ref=context.holiday_version_ref,
        fiscal_version_ref=context.fiscal_version_ref,
    )


def _admission_fence(
    snapshot: AuthoritySnapshot,
) -> AdmissionAuthorityFence:
    if (
        snapshot.accepted_question_revision_id is None
        or snapshot.accepted_frame_revision_id is None
        or snapshot.accepted_plan_revision_id is None
    ):
        raise ValueError(
            "evidence admission requires accepted question, Frame, and Plan"
        )
    return AdmissionAuthorityFence(
        case_id=snapshot.case_id,
        mailbox_authority_epoch=snapshot.mailbox_authority_epoch,
        accepted_question_revision_id=(
            snapshot.accepted_question_revision_id
        ),
        accepted_frame_revision_id=snapshot.accepted_frame_revision_id,
        accepted_plan_revision_id=snapshot.accepted_plan_revision_id,
        active_frame_candidate_generation=(
            snapshot.active_frame_candidate_generation
        ),
        active_frame_candidate_sha256=(
            snapshot.active_frame_candidate_sha256
        ),
    )


def _validate_plan_adoption(
    *,
    adoption: PlanAdoptionRecord,
    binding: QueryBindingEnvelope,
    obligation: ResolvedEvidenceObligation,
) -> None:
    adoption_material = {
        name: getattr(adoption, name)
        for name in adoption.__dataclass_fields__
        if name
        not in {
            "plan_adoption_id",
            "authority_snapshot",
            "derivation_proof_sha256",
            "created_at",
            "schema_epoch",
        }
    }
    expected_proof = content_sha256(adoption_material)
    expected_id = content_sha256(
        {
            "kind": "plan-adoption-record.v1",
            "derivation_proof_sha256": expected_proof,
        }
    )
    if (
        adoption.derivation_proof_sha256 != expected_proof
        or adoption.plan_adoption_id != expected_id
    ):
        raise ValueError("Plan adoption identity is not system-derived")
    try:
        query_index = adoption.query_binding_ids.index(
            binding.query_binding_id
        )
        obligation_index = adoption.obligation_ids.index(
            obligation.obligation_id
        )
    except ValueError as error:
        raise ValueError(
            "Plan adoption does not own evidence authority"
        ) from error
    if (
        adoption.case_id != binding.case_id
        or adoption.question_revision_id != binding.question_revision_id
        or adoption.frame_revision_id != binding.frame_revision_id
        or adoption.plan_revision_id != binding.plan_revision_id
        or adoption.query_binding_content_sha256s[query_index]
        != binding.content_sha256
        or adoption.obligation_content_sha256s[obligation_index]
        != obligation.content_sha256
    ):
        raise ValueError("Plan adoption changes evidence authority")


def _admission_proofs(
    *,
    evidence: EvidenceRecord,
    binding: QueryBindingEnvelope,
) -> tuple[str, str, str, str, str]:
    instance = binding.resolved_measurement_instance
    return (
        content_sha256(
            {
                "actual_windows": evidence.actual_windows,
                "resolved_windows": instance.windows,
            }
        ),
        content_sha256(
            {
                "actual_exposure": evidence.actual_exposure_facts,
                "expected_exposure_id": instance.expected_exposure_id,
                "resolved_windows": instance.windows,
            }
        ),
        content_sha256(
            {
                "actual_unit_ref": evidence.actual_unit_ref,
                "expected_unit_ref": instance.expected_unit_ref,
            }
        ),
        content_sha256(
            {
                "actual_grain_ref": evidence.actual_grain_ref,
                "expected_grain_ref": instance.expected_grain_ref,
            }
        ),
        content_sha256(
            {
                "evidence_data_context": evidence.data_context,
                "resolved_context": instance.context,
                "scope_data_version_boundary_ref": (
                    evidence.actual_scope.data_version_boundary_ref
                ),
            }
        ),
    )


def _flatten_exposure(
    windows: tuple[ResolvedWindow, ...],
) -> tuple[ResolvedExposureFact, ...]:
    return tuple(
        fact for window in windows for fact in window.exposure_facts
    )


def build_conformance_execution_provenance(
    *,
    binding: QueryBindingEnvelope,
    spec: ConformanceExecutionSpec,
    attempt: LogicalExecutionAttempt,
    current_authority: AuthoritySnapshot,
    prior_attempt: LogicalExecutionAttempt | None = None,
) -> ConformanceExecutionProvenance:
    validate_conformance_execution_spec_authority(
        spec=spec,
        binding=binding,
        current_authority=current_authority,
    )
    validate_logical_execution_attempt_authority(
        attempt=attempt,
        spec=spec,
        binding=binding,
        current_authority=current_authority,
        prior_attempt=prior_attempt,
    )
    return ConformanceExecutionProvenance(
        kind=ExecutionProvenanceKind.CONFORMANCE,
        logical_execution_id=attempt.logical_execution_id,
        query_binding_id=binding.query_binding_id,
        query_binding_content_sha256=binding.content_sha256,
        execution_spec_id=spec.conformance_execution_spec_id,
        execution_spec_content_sha256=spec.content_sha256,
        logical_execution_attempt_id=attempt.logical_execution_attempt_id,
        logical_execution_attempt_content_sha256=attempt.content_sha256,
        fixture_ref=spec.fixture_ref,
        fixture_content_sha256=spec.fixture_content_sha256,
        result_contract_ref=spec.result_contract_ref,
        execution_policy_ref=spec.execution_policy_ref,
    )


def build_physical_query_execution_provenance(
    *,
    logical_execution_id: str,
    binding: QueryBindingEnvelope,
    query_spec_id: str,
    query_spec_content_sha256: str,
    capability_invocation_id: str,
    capability_invocation_content_sha256: str,
    provider_receipt_id: str,
    provider_receipt_content_sha256: str,
    compiler_contract_ref: str,
    production_profile_enabled: bool = False,
) -> PhysicalQueryExecutionProvenance:
    if not production_profile_enabled:
        raise ValueError("production evidence provenance is disabled")
    return PhysicalQueryExecutionProvenance(
        kind=ExecutionProvenanceKind.PHYSICAL_QUERY,
        logical_execution_id=logical_execution_id,
        query_binding_id=binding.query_binding_id,
        query_binding_content_sha256=binding.content_sha256,
        query_spec_id=query_spec_id,
        query_spec_content_sha256=query_spec_content_sha256,
        capability_invocation_id=capability_invocation_id,
        capability_invocation_content_sha256=(
            capability_invocation_content_sha256
        ),
        provider_receipt_id=provider_receipt_id,
        provider_receipt_content_sha256=(
            provider_receipt_content_sha256
        ),
        compiler_contract_ref=compiler_contract_ref,
    )


def _validate_provenance(
    *,
    provenance: ExecutionProvenance,
    binding: QueryBindingEnvelope,
    production_profile_enabled: bool,
) -> None:
    if (
        provenance.query_binding_id != binding.query_binding_id
        or provenance.query_binding_content_sha256
        != binding.content_sha256
    ):
        raise ValueError("execution provenance changes QueryBinding")
    if isinstance(provenance, PhysicalQueryExecutionProvenance):
        if not production_profile_enabled:
            raise ValueError("production evidence provenance is disabled")
    elif not isinstance(provenance, ConformanceExecutionProvenance):
        raise TypeError("unsupported execution provenance")


def _validate_binding_chain(
    *,
    binding: QueryBindingEnvelope,
    obligation: ResolvedEvidenceObligation,
    outcome: MeasurementResolutionOutcome,
) -> None:
    if (
        obligation.execution_disposition
        is not ObligationExecutionDisposition.EXECUTABLE
    ):
        raise ValueError("EvidenceRecord requires executable obligation")
    if (
        outcome.kind is not ResolutionOutcomeKind.RESOLVED_INSTANCE
        or outcome.resolved_instance is None
    ):
        raise ValueError("EvidenceRecord requires resolved measurement")
    if (
        binding.case_id != obligation.case_id
        or binding.frame_revision_id != obligation.frame_revision_id
        or binding.estimand_id != obligation.estimand_id
        or binding.evidence_requirement_id
        != obligation.evidence_requirement_id
        or binding.obligation_id != obligation.obligation_id
        or binding.resolution_outcome_id
        != obligation.resolution_outcome_id
        or binding.resolution_outcome_id
        != outcome.resolution_outcome_id
        or binding.resolution_outcome_content_sha256
        != outcome.content_sha256
        or binding.obligation_content_sha256
        != obligation.content_sha256
        or binding.resolved_measurement_instance
        != outcome.resolved_instance
    ):
        raise ValueError("Evidence chain changes sealed authority")


def _evidence_preimage(record: EvidenceRecord) -> object:
    return {
        name: getattr(record, name)
        for name in record.__dataclass_fields__
        if name
        not in {
            "evidence_record_id",
            "produced_at",
            "schema_epoch",
        }
    }


def build_evidence_record(
    *,
    run_id: str,
    profile: EvidenceAdmissionProfile,
    binding: QueryBindingEnvelope,
    obligation: ResolvedEvidenceObligation,
    outcome: MeasurementResolutionOutcome,
    execution_provenance: ExecutionProvenance,
    actual_scope: ScopeExpression,
    actual_windows: tuple[ResolvedWindow, ...],
    actual_exposure_facts: tuple[ResolvedExposureFact, ...],
    evidence_type_ref: str,
    evidence_strength: ClaimStrengthCeiling,
    estimate: EstimatePayload,
    result_material: ResultMaterial,
    business_summary: str,
    limitation_refs: tuple[str, ...],
    produced_at: datetime,
    production_profile_enabled: bool = False,
) -> EvidenceRecord:
    _validate_binding_chain(
        binding=binding,
        obligation=obligation,
        outcome=outcome,
    )
    _validate_provenance(
        provenance=execution_provenance,
        binding=binding,
        production_profile_enabled=production_profile_enabled,
    )
    instance = binding.resolved_measurement_instance
    material = {
        "run_id": run_id,
        "profile": profile,
        "case_id": binding.case_id,
        "question_revision_id": binding.question_revision_id,
        "frame_revision_id": binding.frame_revision_id,
        "plan_revision_id": binding.plan_revision_id,
        "task_id": binding.task_id,
        "estimand_id": binding.estimand_id,
        "evidence_requirement_id": binding.evidence_requirement_id,
        "obligation_id": binding.obligation_id,
        "resolution_outcome_id": outcome.resolution_outcome_id,
        "resolution_outcome_content_sha256": outcome.content_sha256,
        "resolution_id": instance.resolution_id,
        "semantic_measurement_id": binding.semantic_measurement_id,
        "authority_binding_id": binding.authority_binding_id,
        "query_binding_id": binding.query_binding_id,
        "query_binding_content_sha256": binding.content_sha256,
        "logical_execution_id": execution_provenance.logical_execution_id,
        "execution_provenance": execution_provenance,
        "data_context": _data_context(binding),
        "evidence_type_ref": evidence_type_ref,
        "evidence_strength": evidence_strength,
        "actual_scope": actual_scope,
        "actual_windows": actual_windows,
        "actual_exposure_facts": actual_exposure_facts,
        "actual_grain_ref": actual_scope.grain_ref,
        "actual_unit_ref": actual_scope.unit_ref,
        "actual_aggregation_path_ref": actual_scope.aggregation_path_ref,
        "estimate": estimate,
        "result_material": result_material,
        "business_summary": business_summary,
        "limitation_refs": limitation_refs,
        "identity_version": EVIDENCE_IDENTITY_VERSION,
    }
    record = EvidenceRecord(
        evidence_record_id=_id("evidence-record", material),
        produced_at=produced_at,
        schema_epoch=SCHEMA_EPOCH,
        **material,
    )
    validate_evidence_record_authority(
        record=record,
        binding=binding,
        obligation=obligation,
        outcome=outcome,
        production_profile_enabled=production_profile_enabled,
    )
    return record


def validate_evidence_record_authority(
    *,
    record: EvidenceRecord,
    binding: QueryBindingEnvelope,
    obligation: ResolvedEvidenceObligation,
    outcome: MeasurementResolutionOutcome,
    production_profile_enabled: bool = False,
) -> None:
    _validate_binding_chain(
        binding=binding,
        obligation=obligation,
        outcome=outcome,
    )
    _validate_provenance(
        provenance=record.execution_provenance,
        binding=binding,
        production_profile_enabled=production_profile_enabled,
    )
    expected_kind = (
        ExecutionProvenanceKind.CONFORMANCE
        if record.profile is EvidenceAdmissionProfile.CONFORMANCE
        else ExecutionProvenanceKind.PHYSICAL_QUERY
    )
    if record.execution_provenance.kind is not expected_kind:
        raise ValueError("EvidenceRecord profile changes execution realm")
    expected = binding.resolved_measurement_instance
    expected_pairs = {
        "case_id": binding.case_id,
        "question_revision_id": binding.question_revision_id,
        "frame_revision_id": binding.frame_revision_id,
        "plan_revision_id": binding.plan_revision_id,
        "task_id": binding.task_id,
        "estimand_id": binding.estimand_id,
        "evidence_requirement_id": binding.evidence_requirement_id,
        "obligation_id": binding.obligation_id,
        "resolution_outcome_id": outcome.resolution_outcome_id,
        "resolution_outcome_content_sha256": outcome.content_sha256,
        "resolution_id": expected.resolution_id,
        "semantic_measurement_id": binding.semantic_measurement_id,
        "authority_binding_id": binding.authority_binding_id,
        "query_binding_id": binding.query_binding_id,
        "query_binding_content_sha256": binding.content_sha256,
        "logical_execution_id": (
            record.execution_provenance.logical_execution_id
        ),
    }
    if any(
        getattr(record, name) != value
        for name, value in expected_pairs.items()
    ):
        raise ValueError("EvidenceRecord changes sealed authority identity")
    if record.evidence_type_ref not in obligation.evidence_type_refs:
        raise ValueError("EvidenceRecord uses unowned evidence type")
    if record.actual_windows != expected.windows:
        raise ValueError("EvidenceRecord changes resolved windows")
    if record.actual_exposure_facts != _flatten_exposure(expected.windows):
        raise ValueError("EvidenceRecord changes resolved exposure")
    if record.data_context != _data_context(binding):
        raise ValueError("EvidenceRecord changes resolved data context")
    if (
        record.actual_grain_ref != record.actual_scope.grain_ref
        or record.actual_unit_ref != record.actual_scope.unit_ref
        or record.actual_aggregation_path_ref
        != record.actual_scope.aggregation_path_ref
    ):
        raise ValueError("EvidenceRecord scope facts are internally inconsistent")
    if record.evidence_record_id != _id(
        "evidence-record",
        _evidence_preimage(record),
    ):
        raise ValueError("EvidenceRecord identity is not system-derived")


def _envelope_preimage(envelope: CapabilityResultEnvelope) -> object:
    return {
        name: getattr(envelope, name)
        for name in envelope.__dataclass_fields__
        if name
        not in {
            "capability_result_envelope_id",
            "produced_at",
            "schema_epoch",
        }
    }


def build_capability_result_envelope(
    *,
    evidence_record: EvidenceRecord,
    run_id: str,
    schedule_id: str,
    dispatch_record_id: str,
    outbox_message_id: str,
    logical_execution_attempt_id: str,
    logical_execution_attempt_content_sha256: str,
    produced_at: datetime,
) -> CapabilityResultEnvelope:
    if run_id != evidence_record.run_id:
        raise ValueError("result envelope changes evidence run")
    if isinstance(
        evidence_record.execution_provenance,
        ConformanceExecutionProvenance,
    ) and (
        logical_execution_attempt_id
        != evidence_record.execution_provenance.logical_execution_attempt_id
        or logical_execution_attempt_content_sha256
        != evidence_record.execution_provenance
        .logical_execution_attempt_content_sha256
    ):
        raise ValueError(
            "result envelope changes conformance attempt identity"
        )
    invocation_id = _id(
        "capability-invocation",
        {
            "logical_execution_id": evidence_record.logical_execution_id,
            "query_binding_id": evidence_record.query_binding_id,
            "execution_provenance": evidence_record.execution_provenance,
        },
    )
    material = {
        "run_id": run_id,
        "schedule_id": schedule_id,
        "dispatch_record_id": dispatch_record_id,
        "outbox_message_id": outbox_message_id,
        "logical_execution_attempt_id": logical_execution_attempt_id,
        "logical_execution_attempt_content_sha256": (
            logical_execution_attempt_content_sha256
        ),
        "capability_invocation_id": invocation_id,
        "case_id": evidence_record.case_id,
        "frame_revision_id": evidence_record.frame_revision_id,
        "plan_revision_id": evidence_record.plan_revision_id,
        "task_id": evidence_record.task_id,
        "obligation_id": evidence_record.obligation_id,
        "query_binding_id": evidence_record.query_binding_id,
        "query_binding_content_sha256": (
            evidence_record.query_binding_content_sha256
        ),
        "execution_provenance": evidence_record.execution_provenance,
        "result_material": evidence_record.result_material,
        "evidence_record": evidence_record,
    }
    envelope = CapabilityResultEnvelope(
        capability_result_envelope_id=_id(
            "capability-result-envelope",
            material,
        ),
        produced_at=produced_at,
        **material,
    )
    validate_capability_result_envelope(envelope)
    return envelope


def validate_capability_result_envelope(
    envelope: CapabilityResultEnvelope,
) -> None:
    evidence = envelope.evidence_record
    if any(
        (
            envelope.run_id != evidence.run_id,
            envelope.case_id != evidence.case_id,
            envelope.frame_revision_id != evidence.frame_revision_id,
            envelope.plan_revision_id != evidence.plan_revision_id,
            envelope.task_id != evidence.task_id,
            envelope.obligation_id != evidence.obligation_id,
            envelope.query_binding_id != evidence.query_binding_id,
            envelope.query_binding_content_sha256
            != evidence.query_binding_content_sha256,
            envelope.execution_provenance
            != evidence.execution_provenance,
            envelope.result_material != evidence.result_material,
        )
    ):
        raise ValueError("result envelope changes EvidenceRecord identity")
    if isinstance(
        evidence.execution_provenance,
        ConformanceExecutionProvenance,
    ) and (
        envelope.logical_execution_attempt_id
        != evidence.execution_provenance.logical_execution_attempt_id
        or envelope.logical_execution_attempt_content_sha256
        != evidence.execution_provenance
        .logical_execution_attempt_content_sha256
    ):
        raise ValueError("result envelope changes logical attempt")
    expected_invocation = _id(
        "capability-invocation",
        {
            "logical_execution_id": evidence.logical_execution_id,
            "query_binding_id": evidence.query_binding_id,
            "execution_provenance": evidence.execution_provenance,
        },
    )
    if envelope.capability_invocation_id != expected_invocation:
        raise ValueError("capability invocation identity is forged")
    if envelope.capability_result_envelope_id != _id(
        "capability-result-envelope",
        _envelope_preimage(envelope),
    ):
        raise ValueError("result envelope identity is not system-derived")


def build_capability_result_receipt(
    *,
    envelope: CapabilityResultEnvelope,
    operation_identity: OperationIdentity,
    delivery_owner_id: str,
    delivery_fencing_token: int,
    received_at: datetime,
) -> CapabilityResultReceipt:
    validate_capability_result_envelope(envelope)
    if operation_identity.correlation_id != envelope.run_id:
        raise ValueError("receipt operation does not correlate to run")
    if operation_identity.payload_sha256 != (
        capability_result_receipt_payload_sha256(envelope)
    ):
        raise ValueError(
            "receipt operation does not bind capability result payload"
        )
    material = {
        "run_id": envelope.run_id,
        "schedule_id": envelope.schedule_id,
        "dispatch_record_id": envelope.dispatch_record_id,
        "outbox_message_id": envelope.outbox_message_id,
        "delivery_owner_id": delivery_owner_id,
        "delivery_fencing_token": delivery_fencing_token,
        "logical_execution_attempt_id": (
            envelope.logical_execution_attempt_id
        ),
        "logical_execution_attempt_content_sha256": (
            envelope.logical_execution_attempt_content_sha256
        ),
        "capability_result_envelope_id": (
            envelope.capability_result_envelope_id
        ),
        "capability_result_envelope_content_sha256": (
            envelope.content_sha256
        ),
        "capability_invocation_id": envelope.capability_invocation_id,
        "query_binding_id": envelope.query_binding_id,
        "execution_provenance_content_sha256": content_sha256(
            envelope.execution_provenance
        ),
        "result_material_content_sha256": content_sha256(
            envelope.result_material
        ),
        "operation_identity": operation_identity,
        "idempotency_key": operation_identity.idempotency_key,
        "correlation_id": operation_identity.correlation_id,
    }
    return CapabilityResultReceipt(
        capability_result_receipt_id=_id(
            "capability-result-receipt",
            material,
        ),
        received_at=received_at,
        **material,
    )


def capability_result_receipt_payload_sha256(
    envelope: CapabilityResultEnvelope,
) -> str:
    validate_capability_result_envelope(envelope)
    return content_sha256(
        {
            "capability_result_envelope_id": (
                envelope.capability_result_envelope_id
            ),
            "capability_result_envelope_content_sha256": (
                envelope.content_sha256
            ),
            "dispatch_record_id": envelope.dispatch_record_id,
            "outbox_message_id": envelope.outbox_message_id,
        }
    )


def validate_capability_result_receipt(
    *,
    receipt: CapabilityResultReceipt,
    envelope: CapabilityResultEnvelope,
) -> None:
    expected = build_capability_result_receipt(
        envelope=envelope,
        operation_identity=receipt.operation_identity,
        delivery_owner_id=receipt.delivery_owner_id,
        delivery_fencing_token=receipt.delivery_fencing_token,
        received_at=receipt.received_at,
    )
    if receipt != expected:
        raise ValueError("result receipt is not system-derived")


def _authority_matches(
    binding: QueryBindingEnvelope,
    snapshot: AuthoritySnapshot,
    *,
    receipt: CapabilityResultReceipt,
    plan_adoption: PlanAdoptionRecord,
) -> bool:
    return (
        snapshot.case_id == binding.case_id
        and snapshot.accepted_question_revision_id
        == binding.question_revision_id
        and snapshot.accepted_frame_revision_id
        == binding.frame_revision_id
        and snapshot.accepted_plan_revision_id
        == binding.plan_revision_id
        and receipt.operation_identity.authority_revision
        == snapshot.mailbox_authority_epoch
        and plan_adoption.authority_snapshot.mailbox_authority_epoch
        == snapshot.mailbox_authority_epoch
        and plan_adoption.authority_snapshot
        .active_frame_candidate_generation
        == snapshot.active_frame_candidate_generation
        and plan_adoption.authority_snapshot
        .active_frame_candidate_sha256
        == snapshot.active_frame_candidate_sha256
    )


def build_evidence_admission(
    *,
    binding: QueryBindingEnvelope,
    obligation: ResolvedEvidenceObligation,
    outcome: MeasurementResolutionOutcome,
    envelope: CapabilityResultEnvelope,
    receipt: CapabilityResultReceipt,
    plan_adoption: PlanAdoptionRecord,
    expected_scope: ScopeExpression,
    current_authority: AuthoritySnapshot,
    profile: EvidenceAdmissionProfile,
    admitted_at: datetime,
    relation_contracts: Mapping[
        tuple[str, str], tuple[ScopeRelationKind, str]
    ]
    | None = None,
    production_profile_enabled: bool = False,
) -> EvidenceAdmissionRecord:
    evidence = envelope.evidence_record
    _validate_plan_adoption(
        adoption=plan_adoption,
        binding=binding,
        obligation=obligation,
    )
    validate_capability_result_envelope(envelope)
    validate_capability_result_receipt(receipt=receipt, envelope=envelope)
    validate_evidence_record_authority(
        record=evidence,
        binding=binding,
        obligation=obligation,
        outcome=outcome,
        production_profile_enabled=production_profile_enabled,
    )
    if expected_scope.scope_id != binding.requirement_binding.scope_id:
        raise ValueError("expected scope is outside QueryBinding")
    if evidence.profile is not profile:
        raise ValueError("admission profile changes EvidenceRecord profile")
    proof = scope_relation(
        evidence.actual_scope,
        expected_scope,
        proof_policy_version=SCOPE_PROOF_POLICY_VERSION,
        relation_contracts=relation_contracts,
    )
    reasons: list[str] = []
    expected_kind = (
        ExecutionProvenanceKind.CONFORMANCE
        if profile is EvidenceAdmissionProfile.CONFORMANCE
        else ExecutionProvenanceKind.PHYSICAL_QUERY
    )
    if evidence.execution_provenance.kind is not expected_kind:
        reasons.append("execution_profile_mismatch")
    if (
        profile is EvidenceAdmissionProfile.PRODUCTION
        and not production_profile_enabled
    ):
        reasons.append("production_profile_disabled")
    if not _authority_matches(
        binding,
        current_authority,
        receipt=receipt,
        plan_adoption=plan_adoption,
    ):
        reasons.append("stale_authority")
    if proof.relation not in _COVERING_SCOPE_RELATIONS:
        reasons.append("scope_not_covered")
    if evidence.evidence_type_ref not in obligation.evidence_type_refs:
        reasons.append("evidence_type_mismatch")
    required_strength = binding.requirement_binding.minimum_strength
    effective_rank = min(
        _STRENGTH_RANK[evidence.evidence_strength],
        _STRENGTH_RANK[binding.measurement_binding.claim_strength_ceiling],
    )
    effective_strength = next(
        item
        for item, rank in _STRENGTH_RANK.items()
        if rank == effective_rank
    )
    if effective_rank < _STRENGTH_RANK[required_strength]:
        reasons.append("insufficient_strength")
    status = (
        EvidenceAdmissionStatus.ACCEPTED
        if not reasons
        else EvidenceAdmissionStatus.REJECTED
    )
    if not reasons:
        reasons.append("accepted")
    authority_fence = _admission_fence(current_authority)
    (
        window_proof_sha256,
        exposure_proof_sha256,
        unit_proof_sha256,
        grain_proof_sha256,
        data_version_proof_sha256,
    ) = _admission_proofs(evidence=evidence, binding=binding)
    material = {
        "profile": profile,
        "status": status,
        "evidence_record_id": evidence.evidence_record_id,
        "evidence_record_content_sha256": evidence.content_sha256,
        "capability_result_envelope_id": (
            envelope.capability_result_envelope_id
        ),
        "capability_result_envelope_content_sha256": (
            envelope.content_sha256
        ),
        "capability_result_receipt_id": (
            receipt.capability_result_receipt_id
        ),
        "capability_result_receipt_content_sha256": (
            receipt.content_sha256
        ),
        "obligation_id": obligation.obligation_id,
        "obligation_content_sha256": obligation.content_sha256,
        "query_binding_id": binding.query_binding_id,
        "query_binding_content_sha256": binding.content_sha256,
        "plan_adoption_id": plan_adoption.plan_adoption_id,
        "plan_adoption_content_sha256": plan_adoption.content_sha256,
        "authority_fence": authority_fence,
        "authority_fence_content_sha256": (
            authority_fence.content_sha256
        ),
        "authority_snapshot": current_authority,
        "authority_snapshot_content_sha256": (
            current_authority.content_sha256
        ),
        "expected_scope": expected_scope,
        "scope_relation_proof": proof,
        "window_proof_sha256": window_proof_sha256,
        "exposure_proof_sha256": exposure_proof_sha256,
        "unit_proof_sha256": unit_proof_sha256,
        "grain_proof_sha256": grain_proof_sha256,
        "data_version_proof_sha256": data_version_proof_sha256,
        "effective_strength": effective_strength,
        "reason_codes": tuple(sorted(reasons)),
        "policy_version": EVIDENCE_ADMISSION_POLICY_VERSION,
    }
    material["derived_input_sha256"] = content_sha256(
        _admission_derived_input_material(material)
    )
    record = EvidenceAdmissionRecord(
        evidence_admission_id=_id(
            "evidence-admission",
            _admission_identity_material_from_values(material),
        ),
        admitted_at=admitted_at,
        **material,
    )
    validate_evidence_admission(
        admission=record,
        binding=binding,
        obligation=obligation,
        outcome=outcome,
        envelope=envelope,
        receipt=receipt,
        plan_adoption=plan_adoption,
        expected_scope=expected_scope,
        current_authority=current_authority,
        relation_contracts=relation_contracts,
        production_profile_enabled=production_profile_enabled,
    )
    return record


def validate_evidence_admission(
    *,
    admission: EvidenceAdmissionRecord,
    binding: QueryBindingEnvelope,
    obligation: ResolvedEvidenceObligation,
    outcome: MeasurementResolutionOutcome,
    envelope: CapabilityResultEnvelope,
    receipt: CapabilityResultReceipt,
    plan_adoption: PlanAdoptionRecord,
    expected_scope: ScopeExpression,
    current_authority: AuthoritySnapshot,
    relation_contracts: Mapping[
        tuple[str, str], tuple[ScopeRelationKind, str]
    ]
    | None = None,
    production_profile_enabled: bool = False,
) -> None:
    _validate_plan_adoption(
        adoption=plan_adoption,
        binding=binding,
        obligation=obligation,
    )
    _validate_binding_chain(
        binding=binding,
        obligation=obligation,
        outcome=outcome,
    )
    validate_capability_result_envelope(envelope)
    validate_capability_result_receipt(
        receipt=receipt,
        envelope=envelope,
    )
    validate_evidence_record_authority(
        record=envelope.evidence_record,
        binding=binding,
        obligation=obligation,
        outcome=outcome,
        production_profile_enabled=production_profile_enabled,
    )
    if expected_scope.scope_id != binding.requirement_binding.scope_id:
        raise ValueError("expected scope is outside QueryBinding")
    if envelope.evidence_record.profile is not admission.profile:
        raise ValueError("admission profile changes EvidenceRecord profile")
    proof = scope_relation(
        envelope.evidence_record.actual_scope,
        expected_scope,
        proof_policy_version=SCOPE_PROOF_POLICY_VERSION,
        relation_contracts=relation_contracts,
    )
    reasons: list[str] = []
    expected_kind = (
        ExecutionProvenanceKind.CONFORMANCE
        if admission.profile is EvidenceAdmissionProfile.CONFORMANCE
        else ExecutionProvenanceKind.PHYSICAL_QUERY
    )
    if envelope.evidence_record.execution_provenance.kind is not expected_kind:
        reasons.append("execution_profile_mismatch")
    if (
        admission.profile is EvidenceAdmissionProfile.PRODUCTION
        and not production_profile_enabled
    ):
        reasons.append("production_profile_disabled")
    if not _authority_matches(
        binding,
        current_authority,
        receipt=receipt,
        plan_adoption=plan_adoption,
    ):
        reasons.append("stale_authority")
    if proof.relation not in _COVERING_SCOPE_RELATIONS:
        reasons.append("scope_not_covered")
    if (
        envelope.evidence_record.evidence_type_ref
        not in obligation.evidence_type_refs
    ):
        reasons.append("evidence_type_mismatch")
    effective_rank = min(
        _STRENGTH_RANK[envelope.evidence_record.evidence_strength],
        _STRENGTH_RANK[binding.measurement_binding.claim_strength_ceiling],
    )
    effective_strength = next(
        item
        for item, rank in _STRENGTH_RANK.items()
        if rank == effective_rank
    )
    if effective_rank < _STRENGTH_RANK[
        binding.requirement_binding.minimum_strength
    ]:
        reasons.append("insufficient_strength")
    status = (
        EvidenceAdmissionStatus.ACCEPTED
        if not reasons
        else EvidenceAdmissionStatus.REJECTED
    )
    expected_reasons = tuple(sorted(reasons or ["accepted"]))
    expected_fence = _admission_fence(current_authority)
    (
        window_proof_sha256,
        exposure_proof_sha256,
        unit_proof_sha256,
        grain_proof_sha256,
        data_version_proof_sha256,
    ) = _admission_proofs(
        evidence=envelope.evidence_record,
        binding=binding,
    )
    if any(
        (
            admission.status is not status,
            admission.evidence_record_id
            != envelope.evidence_record.evidence_record_id,
            admission.evidence_record_content_sha256
            != envelope.evidence_record.content_sha256,
            admission.capability_result_envelope_id
            != envelope.capability_result_envelope_id,
            admission.capability_result_envelope_content_sha256
            != envelope.content_sha256,
            admission.capability_result_receipt_id
            != receipt.capability_result_receipt_id,
            admission.capability_result_receipt_content_sha256
            != receipt.content_sha256,
            admission.obligation_id != obligation.obligation_id,
            admission.obligation_content_sha256
            != obligation.content_sha256,
            admission.query_binding_id != binding.query_binding_id,
            admission.query_binding_content_sha256
            != binding.content_sha256,
            admission.plan_adoption_id
            != plan_adoption.plan_adoption_id,
            admission.plan_adoption_content_sha256
            != plan_adoption.content_sha256,
            admission.authority_fence != expected_fence,
            admission.authority_fence_content_sha256
            != expected_fence.content_sha256,
            _admission_fence(admission.authority_snapshot)
            != admission.authority_fence,
            admission.expected_scope != expected_scope,
            admission.scope_relation_proof != proof,
            admission.window_proof_sha256 != window_proof_sha256,
            admission.exposure_proof_sha256
            != exposure_proof_sha256,
            admission.unit_proof_sha256 != unit_proof_sha256,
            admission.grain_proof_sha256 != grain_proof_sha256,
            admission.data_version_proof_sha256
            != data_version_proof_sha256,
            admission.effective_strength is not effective_strength,
            admission.reason_codes != expected_reasons,
            admission.derived_input_sha256
            != content_sha256(
                _admission_derived_input_material(
                    {
                        name: getattr(admission, name)
                        for name in admission.__dataclass_fields__
                        if name
                        not in {
                            "evidence_admission_id",
                            "admitted_at",
                            "schema_epoch",
                        }
                    }
                )
            ),
        )
    ):
        raise ValueError("evidence admission is not policy-derived")
    _validate_admission_record_identity(admission)


def _validate_admission_record_identity(
    admission: EvidenceAdmissionRecord,
) -> None:
    material = {
        name: getattr(admission, name)
        for name in admission.__dataclass_fields__
        if name
        not in {
            "evidence_admission_id",
            "admitted_at",
            "schema_epoch",
        }
    }
    material = _admission_identity_material_from_values(material)
    if admission.evidence_admission_id != _id(
        "evidence-admission",
        material,
    ):
        raise ValueError("evidence admission identity is forged")


def _admission_identity_material_from_values(
    values: Mapping[str, object],
) -> dict[str, object]:
    return {
        name: value
        for name, value in values.items()
        if name
        not in {
            "authority_snapshot",
            "authority_snapshot_content_sha256",
        }
    }


def _admission_derived_input_material(
    values: Mapping[str, object],
) -> dict[str, object]:
    return {
        name: value
        for name, value in values.items()
        if name
        not in {
            "authority_snapshot",
            "authority_snapshot_content_sha256",
            "derived_input_sha256",
        }
    }


def build_initial_evidence_validity(
    *,
    admission: EvidenceAdmissionRecord,
    recorded_at: datetime,
) -> EvidenceValidityRecord:
    _validate_admission_record_identity(admission)
    status = (
        EvidenceValidityStatus.ADMITTED_VALID
        if admission.status is EvidenceAdmissionStatus.ACCEPTED
        else EvidenceValidityStatus.NEVER_ADMITTED
    )
    reason = (
        "admission_accepted"
        if status is EvidenceValidityStatus.ADMITTED_VALID
        else "admission_rejected"
    )
    material = {
        "evidence_record_id": admission.evidence_record_id,
        "evidence_admission_id": admission.evidence_admission_id,
        "evidence_admission_content_sha256": admission.content_sha256,
        "prior_evidence_validity_id": None,
        "prior_evidence_validity_content_sha256": None,
        "status": status,
        "reason_code": reason,
        "policy_version": EVIDENCE_VALIDITY_POLICY_VERSION,
    }
    return EvidenceValidityRecord(
        evidence_validity_id=_id("evidence-validity", material),
        recorded_at=recorded_at,
        **material,
    )


def build_evidence_validity_successor(
    *,
    prior: EvidenceValidityRecord,
    status: EvidenceValidityStatus,
    reason_code: str,
    recorded_at: datetime,
) -> EvidenceValidityRecord:
    if prior.status is not EvidenceValidityStatus.ADMITTED_VALID:
        raise ValueError("terminal evidence validity cannot have successor")
    validate_evidence_validity(prior)
    if status not in {
        EvidenceValidityStatus.SUPERSEDED,
        EvidenceValidityStatus.REVOKED,
    }:
        raise ValueError("validity successor must close prior validity")
    material = {
        "evidence_record_id": prior.evidence_record_id,
        "evidence_admission_id": prior.evidence_admission_id,
        "evidence_admission_content_sha256": (
            prior.evidence_admission_content_sha256
        ),
        "prior_evidence_validity_id": prior.evidence_validity_id,
        "prior_evidence_validity_content_sha256": prior.content_sha256,
        "status": status,
        "reason_code": reason_code,
        "policy_version": EVIDENCE_VALIDITY_POLICY_VERSION,
    }
    record = EvidenceValidityRecord(
        evidence_validity_id=_id("evidence-validity", material),
        recorded_at=recorded_at,
        **material,
    )
    validate_evidence_validity(record, prior=prior)
    return record


def validate_evidence_validity(
    record: EvidenceValidityRecord,
    *,
    prior: EvidenceValidityRecord | None = None,
) -> None:
    if prior is None:
        expected_statuses = {
            EvidenceValidityStatus.ADMITTED_VALID,
            EvidenceValidityStatus.NEVER_ADMITTED,
        }
        if (
            record.prior_evidence_validity_id is not None
            or record.status not in expected_statuses
        ):
            raise ValueError("invalid initial evidence validity")
    else:
        if (
            prior.status is not EvidenceValidityStatus.ADMITTED_VALID
            or record.status
            not in {
                EvidenceValidityStatus.SUPERSEDED,
                EvidenceValidityStatus.REVOKED,
            }
            or record.evidence_record_id != prior.evidence_record_id
            or record.evidence_admission_id
            != prior.evidence_admission_id
            or record.evidence_admission_content_sha256
            != prior.evidence_admission_content_sha256
            or record.prior_evidence_validity_id
            != prior.evidence_validity_id
            or record.prior_evidence_validity_content_sha256
            != prior.content_sha256
        ):
            raise ValueError("invalid evidence validity successor")
    _validate_validity_record_identity(record)


def _validate_validity_record_identity(
    record: EvidenceValidityRecord,
) -> None:
    material = {
        name: getattr(record, name)
        for name in record.__dataclass_fields__
        if name
        not in {
            "evidence_validity_id",
            "recorded_at",
            "schema_epoch",
        }
    }
    if record.evidence_validity_id != _id(
        "evidence-validity", material
    ):
        raise ValueError("evidence validity identity is forged")


def build_evidence_use_binding(
    *,
    evidence: EvidenceRecord,
    admission: EvidenceAdmissionRecord,
    validity: EvidenceValidityRecord,
    binding: QueryBindingEnvelope,
    answer_candidate_id: str,
    proposal_claim_key: str,
    claim_scope: ScopeExpression,
    requested_claim_strength: ClaimStrengthCeiling,
    bound_at: datetime,
    relation_contracts: Mapping[
        tuple[str, str], tuple[ScopeRelationKind, str]
    ]
    | None = None,
) -> EvidenceUseBinding:
    _validate_admission_record_identity(admission)
    require_sha256(answer_candidate_id, "answer_candidate_id")
    require_nonempty(proposal_claim_key, "proposal_claim_key")
    if admission.status is not EvidenceAdmissionStatus.ACCEPTED:
        raise ValueError("rejected evidence cannot bind a claim")
    validate_evidence_validity(validity)
    if validity.status is not EvidenceValidityStatus.ADMITTED_VALID:
        raise ValueError("closed evidence validity cannot bind a claim")
    if (
        admission.evidence_record_id != evidence.evidence_record_id
        or validity.evidence_record_id != evidence.evidence_record_id
        or validity.evidence_admission_id != admission.evidence_admission_id
        or validity.evidence_admission_content_sha256
        != admission.content_sha256
        or admission.query_binding_id != binding.query_binding_id
        or admission.query_binding_content_sha256
        != binding.content_sha256
        or admission.obligation_id != binding.obligation_id
    ):
        raise ValueError("evidence use changes admission identity")
    if claim_scope.scope_id != binding.requirement_binding.scope_id:
        raise ValueError("claim scope is outside evidence requirement authority")
    proof = scope_relation(
        evidence.actual_scope,
        claim_scope,
        proof_policy_version=SCOPE_PROOF_POLICY_VERSION,
        relation_contracts=relation_contracts,
    )
    if proof.relation not in _COVERING_SCOPE_RELATIONS:
        raise ValueError("evidence scope cannot support claim scope")
    ceiling_rank = min(
        _STRENGTH_RANK[evidence.evidence_strength],
        _STRENGTH_RANK[admission.effective_strength],
        _STRENGTH_RANK[binding.measurement_binding.claim_strength_ceiling],
    )
    if _STRENGTH_RANK[requested_claim_strength] > ceiling_rank:
        raise ValueError("requested claim exceeds evidence strength")
    effective = next(
        item
        for item, rank in _STRENGTH_RANK.items()
        if rank == ceiling_rank
    )
    material = {
        "evidence_record_id": evidence.evidence_record_id,
        "evidence_record_content_sha256": evidence.content_sha256,
        "evidence_admission_id": admission.evidence_admission_id,
        "evidence_admission_content_sha256": admission.content_sha256,
        "evidence_validity_id": validity.evidence_validity_id,
        "evidence_validity_content_sha256": validity.content_sha256,
        "case_id": binding.case_id,
        "question_revision_id": binding.question_revision_id,
        "frame_revision_id": binding.frame_revision_id,
        "plan_revision_id": binding.plan_revision_id,
        "estimand_id": binding.estimand_id,
        "evidence_requirement_id": binding.evidence_requirement_id,
        "obligation_id": binding.obligation_id,
        "resolution_outcome_id": binding.resolution_outcome_id,
        "answer_candidate_id": answer_candidate_id,
        "proposal_claim_key": proposal_claim_key,
        "claim_scope": claim_scope,
        "scope_relation_proof": proof,
        "requested_claim_strength": requested_claim_strength,
        "effective_claim_strength": effective,
        "limitation_refs": evidence.limitation_refs,
        "policy_version": EVIDENCE_USE_POLICY_VERSION,
    }
    use = EvidenceUseBinding(
        evidence_use_binding_id=_id("evidence-use-binding", material),
        bound_at=bound_at,
        **material,
    )
    _validate_use_record_identity(use)
    return use


def _validate_use_record_identity(use: EvidenceUseBinding) -> None:
    material = {
        name: getattr(use, name)
        for name in use.__dataclass_fields__
        if name
        not in {
            "evidence_use_binding_id",
            "bound_at",
            "schema_epoch",
        }
    }
    if use.evidence_use_binding_id != _id(
        "evidence-use-binding",
        material,
    ):
        raise ValueError("evidence use binding identity is forged")


def validate_evidence_use_binding(
    *,
    use: EvidenceUseBinding,
    evidence: EvidenceRecord,
    admission: EvidenceAdmissionRecord,
    validity: EvidenceValidityRecord,
    binding: QueryBindingEnvelope,
    relation_contracts: Mapping[
        tuple[str, str], tuple[ScopeRelationKind, str]
    ]
    | None = None,
) -> None:
    expected = build_evidence_use_binding(
        evidence=evidence,
        admission=admission,
        validity=validity,
        binding=binding,
        answer_candidate_id=use.answer_candidate_id,
        proposal_claim_key=use.proposal_claim_key,
        claim_scope=use.claim_scope,
        requested_claim_strength=use.requested_claim_strength,
        bound_at=use.bound_at,
        relation_contracts=relation_contracts,
    )
    if use != expected:
        raise ValueError("evidence use binding is not system-derived")


def build_obligation_satisfaction(
    *,
    obligation: ResolvedEvidenceObligation,
    admissions: tuple[EvidenceAdmissionRecord, ...],
    validities: tuple[EvidenceValidityRecord, ...],
    boundary_outcome: MeasurementResolutionOutcome | None,
    prior: ObligationSatisfactionRecord | None,
    recorded_at: datetime,
    supersede: bool = False,
) -> ObligationSatisfactionRecord:
    for admission in admissions:
        _validate_admission_record_identity(admission)
        if admission.obligation_id != obligation.obligation_id:
            raise ValueError("admission belongs to another obligation")
    validity_by_admission = {
        item.evidence_admission_id: item for item in validities
    }
    if len(validity_by_admission) != len(validities):
        raise ValueError("validity heads must be unique by admission")
    for validity in validities:
        _validate_validity_record_identity(validity)
        admission = next(
            (
                item
                for item in admissions
                if item.evidence_admission_id
                == validity.evidence_admission_id
            ),
            None,
        )
        if (
            admission is None
            or validity.evidence_record_id
            != admission.evidence_record_id
            or validity.evidence_admission_content_sha256
            != admission.content_sha256
        ):
            raise ValueError(
                "validity head lacks its admission authority"
            )
    accepted = tuple(
        item
        for item in admissions
        if item.status is EvidenceAdmissionStatus.ACCEPTED
        and (
            validity_by_admission.get(item.evidence_admission_id)
            is not None
        )
        and validity_by_admission[
            item.evidence_admission_id
        ].status
        is EvidenceValidityStatus.ADMITTED_VALID
    )
    if supersede:
        if prior is None:
            raise ValueError("superseding satisfaction requires prior record")
        _validate_satisfaction_record_identity(prior)
        if prior.obligation_id != obligation.obligation_id:
            raise ValueError("satisfaction prior belongs to another obligation")
        status = ObligationSatisfactionStatus.SUPERSEDED
        reason = "authority_superseded"
    elif (
        obligation.execution_disposition
        is ObligationExecutionDisposition.EXECUTABLE
    ):
        if boundary_outcome is not None:
            raise ValueError("executable obligation cannot use boundary outcome")
        required = 1
        if (
            # One resolved obligation owns one evidence slot. Composition and
            # minimum count are resolved before this layer.
            len(accepted) >= required
        ):
            status = ObligationSatisfactionStatus.SATISFIED
            reason = "accepted_evidence_present"
        elif admissions:
            status = ObligationSatisfactionStatus.BLOCKED
            reason = "all_evidence_rejected"
        else:
            status = ObligationSatisfactionStatus.OPEN
            reason = "awaiting_evidence"
    else:
        if (
            boundary_outcome is None
            or boundary_outcome.resolution_outcome_id
            != obligation.resolution_outcome_id
        ):
            raise ValueError("boundary obligation requires matching outcome")
        status = (
            ObligationSatisfactionStatus.BOUNDARY
            if obligation.execution_disposition
            is ObligationExecutionDisposition.TYPED_BOUNDARY
            else ObligationSatisfactionStatus.BLOCKED
        )
        reason = obligation.boundary_code or "measurement_boundary"
    admission_pairs = sorted(
        (
            item.evidence_admission_id,
            item.content_sha256,
        )
        for item in admissions
    )
    validity_pairs = sorted(
        (
            item.evidence_validity_id,
            item.content_sha256,
        )
        for item in validities
    )
    input_set_sha256 = content_sha256(
        {
            "admissions": admission_pairs,
            "validities": validity_pairs,
            "boundary_outcome": boundary_outcome,
        }
    )
    if (
        prior is not None
        and not supersede
        and prior.input_set_sha256 == input_set_sha256
    ):
        raise ValueError(
            "unchanged obligation satisfaction input cannot create successor"
        )
    material = {
        "obligation_id": obligation.obligation_id,
        "obligation_content_sha256": obligation.content_sha256,
        "prior_obligation_satisfaction_id": (
            prior.obligation_satisfaction_id if prior else None
        ),
        "prior_obligation_satisfaction_content_sha256": (
            prior.content_sha256 if prior else None
        ),
        "status": status,
        "evidence_admission_ids": tuple(
            item[0] for item in admission_pairs
        ),
        "evidence_admission_content_sha256s": tuple(
            item[1] for item in admission_pairs
        ),
        "evidence_validity_ids": tuple(
            item[0] for item in validity_pairs
        ),
        "evidence_validity_content_sha256s": tuple(
            item[1] for item in validity_pairs
        ),
        "boundary_resolution_outcome_id": (
            boundary_outcome.resolution_outcome_id
            if boundary_outcome
            else None
        ),
        "input_set_sha256": input_set_sha256,
        "reason_code": reason,
        "policy_version": OBLIGATION_SATISFACTION_POLICY_VERSION,
    }
    record = ObligationSatisfactionRecord(
        obligation_satisfaction_id=_id(
            "obligation-satisfaction",
            material,
        ),
        recorded_at=recorded_at,
        **material,
    )
    validate_obligation_satisfaction(record, prior=prior)
    return record


def validate_obligation_satisfaction(
    record: ObligationSatisfactionRecord,
    *,
    prior: ObligationSatisfactionRecord | None,
) -> None:
    if prior is None:
        if record.prior_obligation_satisfaction_id is not None:
            raise ValueError("initial satisfaction cannot reference prior")
        if record.status is ObligationSatisfactionStatus.SUPERSEDED:
            raise ValueError("initial satisfaction cannot be superseded")
    else:
        if (
            record.prior_obligation_satisfaction_id
            != prior.obligation_satisfaction_id
            or record.prior_obligation_satisfaction_content_sha256
            != prior.content_sha256
            or record.obligation_id != prior.obligation_id
        ):
            raise ValueError("satisfaction successor changes append-only head")
    _validate_satisfaction_record_identity(record)


def _validate_satisfaction_record_identity(
    record: ObligationSatisfactionRecord,
) -> None:
    material = {
        name: getattr(record, name)
        for name in record.__dataclass_fields__
        if name
        not in {
            "obligation_satisfaction_id",
            "recorded_at",
            "schema_epoch",
        }
    }
    if record.obligation_satisfaction_id != _id(
        "obligation-satisfaction",
        material,
    ):
        raise ValueError("obligation satisfaction identity is forged")
