"""Gate 3.5 provisional Answer authority and settlement readiness.

The Primary Agent owns open business language.  This module owns the typed
admission boundary around that language: claim identities, evidence-use
closure, scope and strength ceilings, obligation closure, and exact authority
continuity are all system-derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .async_runtime import AuthoritySnapshot
from .canonical import (
    content_sha256,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
)
from .evidence import (
    ConformanceExecutionProvenance,
    EvidenceAdmissionRecord,
    EvidenceAdmissionStatus,
    EvidenceRecord,
    EvidenceUseBinding,
    EvidenceValidityRecord,
    EvidenceValidityStatus,
    ObligationSatisfactionRecord,
    ObligationSatisfactionStatus,
    PhysicalQueryExecutionProvenance,
    validate_evidence_use_binding,
)
from .measurement import ClaimStrengthCeiling, ScopeExpression
from .planning import (
    PlanAdoptionRecord,
    QueryBindingEnvelope,
)


SCHEMA_EPOCH = 3
ANSWER_IDENTITY_VERSION = "answer-identity.g3.5.v1"
CLAIM_PRECHECK_POLICY_VERSION = "claim-precheck.g3.5.v1"
SETTLEMENT_POLICY_VERSION = "settlement-precondition.g3.5.v1"
ANALYSIS_CHECK_POLICY_VERSION = "analysis-check.g3.5.v1"


class AnswerStatus(StrEnum):
    PROVISIONAL = "provisional"
    SETTLED = "settled"


class ClaimPrecheckStatus(StrEnum):
    ADMISSIBLE_SUPPORTED = "admissible_supported"
    ADMISSIBLE_BOUNDED = "admissible_bounded"
    ADMISSIBLE_BOUNDARY = "admissible_boundary"
    REJECTED = "rejected"


class AnswerCandidateStatus(StrEnum):
    ACCEPTED_PROVISIONAL = "accepted_provisional"
    REJECTED = "rejected"


class SettlementPreconditionStatus(StrEnum):
    ELIGIBLE_FOR_FUTURE_SETTLEMENT = "eligible_for_future_settlement"
    BLOCKED = "blocked"


class AnalysisCheckKind(StrEnum):
    CONTRADICTION = "contradiction"
    FALSIFICATION = "falsification"
    REVERSAL = "reversal"


class AnalysisCheckStatus(StrEnum):
    SATISFIED = "satisfied"
    TRIGGERED = "triggered"
    RESOLVED_WITH_LIMITATION = "resolved_with_limitation"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    evidence_record_id: str
    role_ref: str

    def __post_init__(self) -> None:
        require_sha256(self.evidence_record_id, "evidence_record_id")
        require_nonempty(self.role_ref, "role_ref")


@dataclass(frozen=True, slots=True)
class NarrativeBlockProposal:
    block_key: str
    markdown: str
    proposal_claim_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.block_key, "block_key")
        require_nonempty(self.markdown, "markdown")
        _require_unique_nonempty_tuple(
            self.proposal_claim_keys,
            "proposal_claim_keys",
        )
        if not self.proposal_claim_keys:
            raise ValueError("every narrative block must bind a claim")


@dataclass(frozen=True, slots=True)
class AnalysisCheckDisposition:
    check_disposition_id: str
    check_id: str
    kind: AnalysisCheckKind
    status: AnalysisCheckStatus
    source_authority_ref: str
    source_authority_content_sha256: str
    limitation_ref: str | None
    policy_version: str = ANALYSIS_CHECK_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in (
            "check_disposition_id",
            "source_authority_ref",
            "source_authority_content_sha256",
        ):
            require_sha256(getattr(self, name), name)
        require_nonempty(self.check_id, "check_id")
        if not isinstance(self.kind, AnalysisCheckKind):
            raise TypeError("analysis check kind has unsupported type")
        if not isinstance(self.status, AnalysisCheckStatus):
            raise TypeError("analysis check status has unsupported type")
        require_nonempty(
            self.source_authority_ref,
            "source_authority_ref",
        )
        if self.limitation_ref is not None:
            require_nonempty(self.limitation_ref, "limitation_ref")
        if (
            self.status
            is AnalysisCheckStatus.RESOLVED_WITH_LIMITATION
            and self.limitation_ref is None
        ):
            raise ValueError(
                "resolved check requires an explicit limitation"
            )
        if self.policy_version != ANALYSIS_CHECK_POLICY_VERSION:
            raise ValueError("analysis check policy is unsupported")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class ProposedClaim:
    """Open business claim proposed by the LLM.

    ``proposal_claim_key`` is local to one answer proposal.  It is a matching
    key, never an authority identity.
    """

    proposal_claim_key: str
    statement: str
    target_estimand_id: str
    obligation_ids: tuple[str, ...]
    evidence_selections: tuple[EvidenceSelection, ...]
    applicability_scope: ScopeExpression
    requested_strength: ClaimStrengthCeiling
    boundary_satisfaction_record_ids: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    contradiction_refs: tuple[str, ...]
    falsification_refs: tuple[str, ...]
    reversal_refs: tuple[str, ...]
    depends_on_proposal_claim_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "proposal_claim_key",
            "statement",
            "target_estimand_id",
        ):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.applicability_scope, ScopeExpression):
            raise TypeError("applicability_scope must be ScopeExpression")
        if not isinstance(self.requested_strength, ClaimStrengthCeiling):
            raise TypeError("requested_strength has unsupported type")
        _require_typed_tuple(
            self.evidence_selections,
            EvidenceSelection,
            "evidence_selections",
        )
        evidence_ids = tuple(
            item.evidence_record_id for item in self.evidence_selections
        )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence selections must be unique by Evidence")
        for name in (
            "obligation_ids",
            "boundary_satisfaction_record_ids",
            "limitation_refs",
            "contradiction_refs",
            "falsification_refs",
            "reversal_refs",
            "depends_on_proposal_claim_keys",
        ):
            _require_unique_nonempty_tuple(getattr(self, name), name)
        if not self.obligation_ids:
            raise ValueError("proposed claim requires obligation authority")
        if not self.evidence_selections and not (
            self.boundary_satisfaction_record_ids
        ):
            raise ValueError(
                "proposed claim requires evidence or typed boundary closure"
            )
        if self.evidence_selections and (
            self.boundary_satisfaction_record_ids
        ):
            raise ValueError(
                "one claim cannot mix evidence and boundary-only closure"
            )
        if (
            self.boundary_satisfaction_record_ids
            and self.requested_strength
            is not ClaimStrengthCeiling.BOUNDARY_ONLY
        ):
            raise ValueError(
                "boundary-only claim must request boundary-only strength"
            )

    @property
    def evidence_record_ids(self) -> tuple[str, ...]:
        return tuple(
            item.evidence_record_id for item in self.evidence_selections
        )


@dataclass(frozen=True, slots=True)
class ProvisionalAnswerCandidate:
    answer_candidate_id: str
    case_id: str
    question_revision_id: str
    frame_revision_id: str
    plan_revision_id: str
    plan_adoption_id: str
    plan_adoption_content_sha256: str
    authority_snapshot: AuthoritySnapshot
    authority_snapshot_content_sha256: str
    accepted_head_version: int
    version_number: int
    prior_answer_version_id: str | None
    claims: tuple[ProposedClaim, ...]
    narrative_blocks: tuple[NarrativeBlockProposal, ...]
    created_by_action_id: str
    created_at: datetime
    identity_version: str = ANSWER_IDENTITY_VERSION
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "answer_candidate_id",
            "plan_adoption_id",
            "plan_adoption_content_sha256",
            "authority_snapshot_content_sha256",
        ):
            require_sha256(getattr(self, name), name)
        for name in (
            "case_id",
            "question_revision_id",
            "frame_revision_id",
            "plan_revision_id",
            "created_by_action_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.accepted_head_version < 0:
            raise ValueError("accepted_head_version must be non-negative")
        if not isinstance(self.authority_snapshot, AuthoritySnapshot):
            raise TypeError("authority_snapshot must be AuthoritySnapshot")
        if (
            self.authority_snapshot.content_sha256
            != self.authority_snapshot_content_sha256
        ):
            raise ValueError("candidate authority snapshot hash is stale")
        if self.version_number < 1:
            raise ValueError("version_number must be positive")
        if (self.version_number == 1) != (
            self.prior_answer_version_id is None
        ):
            raise ValueError("answer candidate prior/version chain is invalid")
        if self.prior_answer_version_id is not None:
            require_sha256(
                self.prior_answer_version_id,
                "prior_answer_version_id",
            )
        _require_typed_tuple(self.claims, ProposedClaim, "claims")
        if not self.claims:
            raise ValueError("answer candidate requires claims")
        keys = tuple(item.proposal_claim_key for item in self.claims)
        if len(keys) != len(set(keys)):
            raise ValueError("proposal claim keys must be unique")
        for claim in self.claims:
            unknown_dependencies = (
                set(claim.depends_on_proposal_claim_keys) - set(keys)
            )
            if unknown_dependencies:
                raise ValueError(
                    "claim dependency references unknown proposal claim"
                )
            if claim.proposal_claim_key in (
                claim.depends_on_proposal_claim_keys
            ):
                raise ValueError("claim cannot depend on itself")
        _require_acyclic_claim_dependencies(self.claims)
        _require_typed_tuple(
            self.narrative_blocks,
            NarrativeBlockProposal,
            "narrative_blocks",
        )
        if not self.narrative_blocks:
            raise ValueError("answer candidate requires narrative blocks")
        block_keys = tuple(
            item.block_key for item in self.narrative_blocks
        )
        if len(block_keys) != len(set(block_keys)):
            raise ValueError("narrative block keys must be unique")
        for block in self.narrative_blocks:
            if set(block.proposal_claim_keys) - set(keys):
                raise ValueError(
                    "narrative block references unknown proposal claim"
                )
        require_aware_datetime(self.created_at, "created_at")
        if self.identity_version != ANSWER_IDENTITY_VERSION:
            raise ValueError("answer identity version is unsupported")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("answer candidate requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)

    @property
    def narrative_markdown(self) -> str:
        return "\n\n".join(
            item.markdown for item in self.narrative_blocks
        )


@dataclass(frozen=True, slots=True)
class ClaimEvidenceSupport:
    evidence: EvidenceRecord
    admission: EvidenceAdmissionRecord
    validity: EvidenceValidityRecord
    query_binding: QueryBindingEnvelope
    use_binding: EvidenceUseBinding

    def __post_init__(self) -> None:
        for value, expected, name in (
            (self.evidence, EvidenceRecord, "evidence"),
            (self.admission, EvidenceAdmissionRecord, "admission"),
            (self.validity, EvidenceValidityRecord, "validity"),
            (self.query_binding, QueryBindingEnvelope, "query_binding"),
            (self.use_binding, EvidenceUseBinding, "use_binding"),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{name} has unsupported type")


@dataclass(frozen=True, slots=True)
class ClaimPrecheckRecord:
    claim_precheck_id: str
    claim_id: str
    answer_candidate_id: str
    answer_candidate_content_sha256: str
    proposal_claim_key: str
    case_id: str
    question_revision_id: str
    frame_revision_id: str
    plan_revision_id: str
    plan_adoption_id: str
    authority_snapshot: AuthoritySnapshot
    authority_snapshot_content_sha256: str
    target_estimand_id: str
    obligation_ids: tuple[str, ...]
    evidence_use_binding_ids: tuple[str, ...]
    evidence_use_binding_content_sha256s: tuple[str, ...]
    latest_evidence_validity_ids: tuple[str, ...]
    latest_evidence_validity_content_sha256s: tuple[str, ...]
    obligation_satisfaction_record_ids: tuple[str, ...]
    obligation_satisfaction_content_sha256s: tuple[str, ...]
    scope_proof_ids: tuple[str, ...]
    window_exposure_proof_sha256s: tuple[str, ...]
    data_version_proof_sha256s: tuple[str, ...]
    analysis_check_disposition_ids: tuple[str, ...]
    analysis_check_disposition_content_sha256s: tuple[str, ...]
    dependency_claim_ids: tuple[str, ...]
    applicability_scope: ScopeExpression
    requested_strength: ClaimStrengthCeiling
    effective_strength: ClaimStrengthCeiling
    status: ClaimPrecheckStatus
    reason_codes: tuple[str, ...]
    required_limitation_refs: tuple[str, ...]
    derived_input_sha256: str
    policy_version: str
    checked_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "claim_precheck_id",
            "claim_id",
            "answer_candidate_id",
            "answer_candidate_content_sha256",
            "plan_adoption_id",
            "authority_snapshot_content_sha256",
            "derived_input_sha256",
        ):
            require_sha256(getattr(self, name), name)
        for name in (
            "proposal_claim_key",
            "case_id",
            "question_revision_id",
            "frame_revision_id",
            "plan_revision_id",
            "target_estimand_id",
        ):
            require_nonempty(getattr(self, name), name)
        for name in (
            "obligation_ids",
            "evidence_use_binding_ids",
            "evidence_use_binding_content_sha256s",
            "latest_evidence_validity_ids",
            "latest_evidence_validity_content_sha256s",
            "obligation_satisfaction_record_ids",
            "obligation_satisfaction_content_sha256s",
            "scope_proof_ids",
            "window_exposure_proof_sha256s",
            "data_version_proof_sha256s",
            "analysis_check_disposition_ids",
            "analysis_check_disposition_content_sha256s",
            "dependency_claim_ids",
            "reason_codes",
            "required_limitation_refs",
        ):
            _require_unique_nonempty_tuple(getattr(self, name), name)
        if len(self.evidence_use_binding_ids) != len(
            self.evidence_use_binding_content_sha256s
        ):
            raise ValueError("use binding identity/hash tuples must align")
        if len(self.latest_evidence_validity_ids) != len(
            self.latest_evidence_validity_content_sha256s
        ):
            raise ValueError("validity identity/hash tuples must align")
        if len(self.obligation_satisfaction_record_ids) != len(
            self.obligation_satisfaction_content_sha256s
        ):
            raise ValueError("satisfaction identity/hash tuples must align")
        if len(self.analysis_check_disposition_ids) != len(
            self.analysis_check_disposition_content_sha256s
        ):
            raise ValueError("check disposition identity/hash tuples must align")
        for value in (
            *self.evidence_use_binding_ids,
            *self.evidence_use_binding_content_sha256s,
            *self.latest_evidence_validity_ids,
            *self.latest_evidence_validity_content_sha256s,
            *self.obligation_satisfaction_record_ids,
            *self.obligation_satisfaction_content_sha256s,
            *self.scope_proof_ids,
            *self.window_exposure_proof_sha256s,
            *self.data_version_proof_sha256s,
            *self.analysis_check_disposition_ids,
            *self.analysis_check_disposition_content_sha256s,
            *self.dependency_claim_ids,
        ):
            require_sha256(value, "claim precheck reference")
        if not isinstance(self.authority_snapshot, AuthoritySnapshot):
            raise TypeError("authority_snapshot must be AuthoritySnapshot")
        if (
            self.authority_snapshot.content_sha256
            != self.authority_snapshot_content_sha256
        ):
            raise ValueError("precheck authority snapshot hash is stale")
        if not isinstance(self.applicability_scope, ScopeExpression):
            raise TypeError("applicability_scope must be ScopeExpression")
        if not isinstance(self.requested_strength, ClaimStrengthCeiling):
            raise TypeError("requested_strength has unsupported type")
        if not isinstance(self.effective_strength, ClaimStrengthCeiling):
            raise TypeError("effective_strength has unsupported type")
        if not isinstance(self.status, ClaimPrecheckStatus):
            raise TypeError("claim precheck status has unsupported type")
        if not self.reason_codes:
            raise ValueError("claim precheck requires reason codes")
        if self.policy_version != CLAIM_PRECHECK_POLICY_VERSION:
            raise ValueError("claim precheck policy is unsupported")
        require_aware_datetime(self.checked_at, "checked_at")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("claim precheck requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    claim_id: str
    proposal_claim_key: str
    statement: str
    target_estimand_id: str
    obligation_ids: tuple[str, ...]
    evidence_use_binding_ids: tuple[str, ...]
    boundary_satisfaction_record_ids: tuple[str, ...]
    applicability_scope: ScopeExpression
    claim_strength: ClaimStrengthCeiling
    limitation_refs: tuple[str, ...]
    analysis_check_disposition_ids: tuple[str, ...]
    dependency_claim_ids: tuple[str, ...]
    claim_precheck_id: str
    claim_precheck_content_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "claim_id",
            "claim_precheck_id",
            "claim_precheck_content_sha256",
        ):
            require_sha256(getattr(self, name), name)
        for name in (
            "proposal_claim_key",
            "statement",
            "target_estimand_id",
        ):
            require_nonempty(getattr(self, name), name)
        for name in (
            "obligation_ids",
            "evidence_use_binding_ids",
            "boundary_satisfaction_record_ids",
            "limitation_refs",
            "analysis_check_disposition_ids",
            "dependency_claim_ids",
        ):
            _require_unique_nonempty_tuple(getattr(self, name), name)
        for value in (
            *self.evidence_use_binding_ids,
            *self.boundary_satisfaction_record_ids,
            *self.analysis_check_disposition_ids,
            *self.dependency_claim_ids,
        ):
            require_sha256(value, "answer claim authority reference")
        if not isinstance(self.applicability_scope, ScopeExpression):
            raise TypeError("applicability_scope must be ScopeExpression")
        if not isinstance(self.claim_strength, ClaimStrengthCeiling):
            raise TypeError("claim_strength has unsupported type")
        if (not self.evidence_use_binding_ids) == (
            not self.boundary_satisfaction_record_ids
        ):
            raise ValueError(
                "claim requires exactly one evidence or boundary authority path"
            )

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class AnswerNarrativeBlock:
    block_id: str
    block_key: str
    markdown: str
    claim_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_sha256(self.block_id, "block_id")
        require_nonempty(self.block_key, "block_key")
        require_nonempty(self.markdown, "markdown")
        _require_unique_nonempty_tuple(self.claim_ids, "claim_ids")
        if not self.claim_ids:
            raise ValueError("answer narrative block requires claim authority")
        for value in self.claim_ids:
            require_sha256(value, "claim_ids")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class AnswerVersion:
    answer_version_id: str
    answer_candidate_id: str
    case_id: str
    question_revision_id: str
    frame_revision_id: str
    plan_revision_id: str
    plan_adoption_id: str
    accepted_head_version: int
    version_number: int
    prior_answer_version_id: str | None
    status: AnswerStatus
    claims: tuple[AnswerClaim, ...]
    claim_precheck_ids: tuple[str, ...]
    claim_precheck_content_sha256s: tuple[str, ...]
    narrative_blocks: tuple[AnswerNarrativeBlock, ...]
    created_by_action_id: str
    created_at: datetime
    identity_version: str = ANSWER_IDENTITY_VERSION
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "answer_version_id",
            "answer_candidate_id",
            "plan_adoption_id",
        ):
            require_sha256(getattr(self, name), name)
        for name in (
            "case_id",
            "question_revision_id",
            "frame_revision_id",
            "plan_revision_id",
            "created_by_action_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.accepted_head_version < 0:
            raise ValueError("accepted_head_version must be non-negative")
        if self.version_number < 1:
            raise ValueError("version_number must be positive")
        if (self.version_number == 1) != (
            self.prior_answer_version_id is None
        ):
            raise ValueError("answer prior/version chain is invalid")
        if self.prior_answer_version_id is not None:
            require_sha256(
                self.prior_answer_version_id,
                "prior_answer_version_id",
            )
        if self.status is not AnswerStatus.PROVISIONAL:
            raise ValueError("Gate 3 can only create provisional answers")
        _require_typed_tuple(self.claims, AnswerClaim, "claims")
        if not self.claims:
            raise ValueError("answer requires claims")
        for name in (
            "claim_precheck_ids",
            "claim_precheck_content_sha256s",
        ):
            _require_unique_nonempty_tuple(getattr(self, name), name)
            for value in getattr(self, name):
                require_sha256(value, name)
        if len(self.claim_precheck_ids) != len(
            self.claim_precheck_content_sha256s
        ) or len(self.claims) != len(self.claim_precheck_ids):
            raise ValueError("answer claim/precheck tuples must align")
        if tuple(item.claim_precheck_id for item in self.claims) != (
            self.claim_precheck_ids
        ):
            raise ValueError("answer claims must preserve precheck order")
        _require_typed_tuple(
            self.narrative_blocks,
            AnswerNarrativeBlock,
            "narrative_blocks",
        )
        if not self.narrative_blocks:
            raise ValueError("answer requires narrative blocks")
        claim_ids = {item.claim_id for item in self.claims}
        if any(
            set(block.claim_ids) - claim_ids
            for block in self.narrative_blocks
        ):
            raise ValueError("narrative block references unknown claim")
        require_aware_datetime(self.created_at, "created_at")
        if self.identity_version != ANSWER_IDENTITY_VERSION:
            raise ValueError("answer identity version is unsupported")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("answer requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)

    @property
    def narrative_markdown(self) -> str:
        return "\n\n".join(
            item.markdown for item in self.narrative_blocks
        )


@dataclass(frozen=True, slots=True)
class ProvisionalAnswerBundle:
    candidate: ProvisionalAnswerCandidate
    prechecks: tuple[ClaimPrecheckRecord, ...]
    status: AnswerCandidateStatus
    answer: AnswerVersion | None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ProvisionalAnswerCandidate):
            raise TypeError("candidate has unsupported type")
        _require_typed_tuple(
            self.prechecks,
            ClaimPrecheckRecord,
            "prechecks",
        )
        if len(self.prechecks) != len(self.candidate.claims):
            raise ValueError("candidate requires one precheck per claim")
        if not isinstance(self.status, AnswerCandidateStatus):
            raise TypeError("candidate status has unsupported type")
        if (
            self.status is AnswerCandidateStatus.ACCEPTED_PROVISIONAL
        ) != (self.answer is not None):
            raise ValueError("accepted candidate requires provisional answer")


@dataclass(frozen=True, slots=True)
class SettlementPreconditionReport:
    settlement_precondition_report_id: str
    case_id: str
    question_revision_id: str
    frame_revision_id: str
    plan_revision_id: str
    plan_adoption_id: str
    plan_adoption_content_sha256: str
    accepted_head_version: int
    answer_version_id: str
    answer_version_content_sha256: str
    claim_ids: tuple[str, ...]
    claim_precheck_ids: tuple[str, ...]
    evidence_use_binding_ids: tuple[str, ...]
    evidence_validity_ids: tuple[str, ...]
    obligation_satisfaction_record_ids: tuple[str, ...]
    objection_disposition_refs: tuple[str, ...]
    trace_manifest_id: str
    trace_manifest_content_sha256: str
    status: SettlementPreconditionStatus
    fail_reason_codes: tuple[str, ...]
    derived_input_sha256: str
    policy_version: str
    created_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for name in (
            "settlement_precondition_report_id",
            "plan_adoption_id",
            "plan_adoption_content_sha256",
            "answer_version_id",
            "answer_version_content_sha256",
            "trace_manifest_content_sha256",
            "derived_input_sha256",
        ):
            require_sha256(getattr(self, name), name)
        for name in (
            "case_id",
            "question_revision_id",
            "frame_revision_id",
            "plan_revision_id",
            "trace_manifest_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.accepted_head_version < 0:
            raise ValueError("accepted_head_version must be non-negative")
        for name in (
            "claim_ids",
            "claim_precheck_ids",
            "evidence_use_binding_ids",
            "evidence_validity_ids",
            "obligation_satisfaction_record_ids",
            "objection_disposition_refs",
            "fail_reason_codes",
        ):
            _require_unique_nonempty_tuple(getattr(self, name), name)
        for value in (
            *self.claim_ids,
            *self.claim_precheck_ids,
            *self.evidence_use_binding_ids,
            *self.evidence_validity_ids,
            *self.obligation_satisfaction_record_ids,
        ):
            require_sha256(value, "settlement authority reference")
        if not isinstance(self.status, SettlementPreconditionStatus):
            raise TypeError("settlement status has unsupported type")
        if (
            self.status
            is SettlementPreconditionStatus.ELIGIBLE_FOR_FUTURE_SETTLEMENT
            and self.fail_reason_codes
        ):
            raise ValueError("eligible report cannot carry failure reasons")
        if (
            self.status is SettlementPreconditionStatus.BLOCKED
            and not self.fail_reason_codes
        ):
            raise ValueError("blocked report requires failure reasons")
        if self.policy_version != SETTLEMENT_POLICY_VERSION:
            raise ValueError("settlement policy is unsupported")
        require_aware_datetime(self.created_at, "created_at")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("settlement report requires schema epoch 3")

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


def _id(kind: str, payload: object) -> str:
    return content_sha256(
        {
            "identity_version": ANSWER_IDENTITY_VERSION,
            "kind": kind,
            "payload": payload,
        }
    )


def build_analysis_check_disposition(
    *,
    check_id: str,
    kind: AnalysisCheckKind,
    status: AnalysisCheckStatus,
    source_authority_ref: str,
    source_authority_content_sha256: str,
    limitation_ref: str | None,
) -> AnalysisCheckDisposition:
    material = {
        "check_id": check_id,
        "kind": kind,
        "status": status,
        "source_authority_ref": source_authority_ref,
        "source_authority_content_sha256": (
            source_authority_content_sha256
        ),
        "limitation_ref": limitation_ref,
        "policy_version": ANALYSIS_CHECK_POLICY_VERSION,
    }
    return AnalysisCheckDisposition(
        check_disposition_id=_id(
            "analysis-check-disposition",
            material,
        ),
        **material,
    )


def validate_analysis_check_disposition(
    disposition: AnalysisCheckDisposition,
) -> None:
    material = {
        name: getattr(disposition, name)
        for name in disposition.__dataclass_fields__
        if name != "check_disposition_id"
    }
    if disposition.check_disposition_id != _id(
        "analysis-check-disposition",
        material,
    ):
        raise ValueError("analysis check disposition identity is forged")


def build_provisional_answer_candidate(
    *,
    case_id: str,
    current_authority: AuthoritySnapshot,
    plan_adoption: PlanAdoptionRecord,
    version_number: int,
    prior_answer_version_id: str | None,
    claims: tuple[ProposedClaim, ...],
    narrative_blocks: tuple[NarrativeBlockProposal, ...],
    created_by_action_id: str,
    created_at: datetime,
) -> ProvisionalAnswerCandidate:
    if (
        current_authority.case_id != case_id
        or current_authority.accepted_question_revision_id
        != plan_adoption.question_revision_id
        or current_authority.accepted_frame_revision_id
        != plan_adoption.frame_revision_id
        or current_authority.accepted_plan_revision_id
        != plan_adoption.plan_revision_id
        or plan_adoption.case_id != case_id
    ):
        raise ValueError("answer candidate authority is stale")
    material = {
        "case_id": case_id,
        "question_revision_id": plan_adoption.question_revision_id,
        "frame_revision_id": plan_adoption.frame_revision_id,
        "plan_revision_id": plan_adoption.plan_revision_id,
        "plan_adoption_id": plan_adoption.plan_adoption_id,
        "plan_adoption_content_sha256": plan_adoption.content_sha256,
        "authority_snapshot": current_authority,
        "authority_snapshot_content_sha256": (
            current_authority.content_sha256
        ),
        "accepted_head_version": current_authority.head_version,
        "version_number": version_number,
        "prior_answer_version_id": prior_answer_version_id,
        "claims": claims,
        "narrative_blocks": narrative_blocks,
        "created_by_action_id": created_by_action_id,
        "identity_version": ANSWER_IDENTITY_VERSION,
    }
    return ProvisionalAnswerCandidate(
        answer_candidate_id=_id("answer-candidate", material),
        created_at=created_at,
        schema_epoch=SCHEMA_EPOCH,
        **material,
    )


def _precheck_claim(
    *,
    candidate: ProvisionalAnswerCandidate,
    proposal: ProposedClaim,
    supports: tuple[ClaimEvidenceSupport, ...],
    satisfactions: tuple[ObligationSatisfactionRecord, ...],
    check_dispositions: tuple[AnalysisCheckDisposition, ...],
    dependency_prechecks: tuple[ClaimPrecheckRecord, ...],
    checked_at: datetime,
) -> ClaimPrecheckRecord:
    reasons: list[str] = []
    support_by_evidence = {
        item.evidence.evidence_record_id: item for item in supports
    }
    if len(support_by_evidence) != len(supports):
        raise ValueError("claim support evidence identities must be unique")
    if set(support_by_evidence) != set(proposal.evidence_record_ids):
        reasons.append("evidence_support_set_mismatch")
    satisfaction_by_id = {
        item.obligation_satisfaction_id: item
        for item in satisfactions
    }
    if len(satisfaction_by_id) != len(satisfactions):
        raise ValueError("claim satisfaction identities must be unique")
    referenced_satisfaction_ids = (
        proposal.boundary_satisfaction_record_ids
        if proposal.boundary_satisfaction_record_ids
        else tuple(
            item.obligation_satisfaction_id for item in satisfactions
        )
    )
    if set(referenced_satisfaction_ids) != set(satisfaction_by_id):
        reasons.append("satisfaction_set_mismatch")

    required_limitations: set[str] = set()
    expected_falsification_ids: set[str] = set()
    expected_reversal_ids: set[str] = set()
    effective_ranks: list[int] = []
    use_pairs: list[tuple[str, str]] = []
    validity_pairs: list[tuple[str, str]] = []
    scope_proof_ids: list[str] = []
    window_exposure_proof_ids: list[str] = []
    data_version_proof_ids: list[str] = []
    for support in supports:
        try:
            validate_evidence_use_binding(
                use=support.use_binding,
                evidence=support.evidence,
                admission=support.admission,
                validity=support.validity,
                binding=support.query_binding,
            )
        except (TypeError, ValueError):
            reasons.append("evidence_use_invalid")
            continue
        use = support.use_binding
        binding = support.query_binding
        if support.admission.status is not EvidenceAdmissionStatus.ACCEPTED:
            reasons.append("evidence_not_admitted")
        if support.validity.status is not (
            EvidenceValidityStatus.ADMITTED_VALID
        ):
            reasons.append("evidence_not_currently_valid")
        if (
            use.case_id != candidate.case_id
            or use.question_revision_id != candidate.question_revision_id
            or use.frame_revision_id != candidate.frame_revision_id
            or use.plan_revision_id != candidate.plan_revision_id
        ):
            reasons.append("evidence_use_authority_mismatch")
        if (
            use.answer_candidate_id != candidate.answer_candidate_id
            or use.proposal_claim_key != proposal.proposal_claim_key
        ):
            reasons.append("evidence_use_claim_mismatch")
        if use.estimand_id != proposal.target_estimand_id:
            reasons.append("estimand_mismatch")
        if use.obligation_id not in proposal.obligation_ids:
            reasons.append("obligation_mismatch")
        if use.claim_scope != proposal.applicability_scope:
            reasons.append("claim_scope_mismatch")
        if use.requested_claim_strength is not proposal.requested_strength:
            reasons.append("claim_strength_request_mismatch")
        required_limitations.update(use.limitation_refs)
        expected_falsification_ids.update(
            binding.requirement_binding.linked_falsification_ids
        )
        expected_reversal_ids.update(
            binding.requirement_binding.linked_reversal_ids
        )
        effective_ranks.append(
            _STRENGTH_RANK[use.effective_claim_strength]
        )
        use_pairs.append(
            (use.evidence_use_binding_id, use.content_sha256)
        )
        validity_pairs.append(
            (
                support.validity.evidence_validity_id,
                support.validity.content_sha256,
            )
        )
        scope_proof_ids.append(
            content_sha256(use.scope_relation_proof)
        )
        window_exposure_proof_ids.append(
            content_sha256(
                {
                    "window": support.admission.window_proof_sha256,
                    "exposure": (
                        support.admission.exposure_proof_sha256
                    ),
                    "unit": support.admission.unit_proof_sha256,
                    "grain": support.admission.grain_proof_sha256,
                }
            )
        )
        data_version_proof_ids.append(
            support.admission.data_version_proof_sha256
        )

    if required_limitations - set(proposal.limitation_refs):
        reasons.append("required_limitation_omitted")
    if expected_falsification_ids - set(proposal.falsification_refs):
        reasons.append("falsification_reference_omitted")
    if expected_reversal_ids - set(proposal.reversal_refs):
        reasons.append("reversal_reference_omitted")

    check_by_key = {
        (item.kind, item.check_id): item
        for item in check_dispositions
    }
    if len(check_by_key) != len(check_dispositions):
        raise ValueError("analysis check dispositions must be unique")
    referenced_checks = {
        *(
            (AnalysisCheckKind.CONTRADICTION, item)
            for item in proposal.contradiction_refs
        ),
        *(
            (AnalysisCheckKind.FALSIFICATION, item)
            for item in proposal.falsification_refs
        ),
        *(
            (AnalysisCheckKind.REVERSAL, item)
            for item in proposal.reversal_refs
        ),
    }
    expected_checks = {
        *(
            (AnalysisCheckKind.FALSIFICATION, item)
            for item in expected_falsification_ids
        ),
        *(
            (AnalysisCheckKind.REVERSAL, item)
            for item in expected_reversal_ids
        ),
    }
    if referenced_checks - set(check_by_key):
        reasons.append("analysis_check_disposition_missing")
    if set(check_by_key) - referenced_checks:
        reasons.append("analysis_check_disposition_unreferenced")
    if expected_checks - set(check_by_key):
        reasons.append("required_analysis_check_unresolved")
    support_authority = {
        item.evidence.evidence_record_id: item.evidence.content_sha256
        for item in supports
    }
    bounded = False
    for disposition in check_dispositions:
        try:
            validate_analysis_check_disposition(disposition)
        except (TypeError, ValueError):
            reasons.append("analysis_check_disposition_forged")
            continue
        if support_authority.get(
            disposition.source_authority_ref
        ) != disposition.source_authority_content_sha256:
            reasons.append("analysis_check_source_not_admitted")
        if disposition.status is AnalysisCheckStatus.UNRESOLVED:
            reasons.append("analysis_check_unresolved")
        elif (
            disposition.kind is AnalysisCheckKind.FALSIFICATION
            and disposition.status is AnalysisCheckStatus.TRIGGERED
        ):
            reasons.append("claim_falsified")
        elif (
            disposition.kind is AnalysisCheckKind.CONTRADICTION
            and disposition.status is AnalysisCheckStatus.TRIGGERED
        ):
            reasons.append("contradiction_unresolved")
        elif disposition.status is (
            AnalysisCheckStatus.RESOLVED_WITH_LIMITATION
        ):
            bounded = True
            if disposition.limitation_ref not in proposal.limitation_refs:
                reasons.append("check_limitation_omitted")
        elif (
            disposition.kind is AnalysisCheckKind.REVERSAL
            and disposition.status is AnalysisCheckStatus.TRIGGERED
        ):
            bounded = True
            if not proposal.limitation_refs:
                reasons.append("reversal_limitation_omitted")

    dependency_by_key = {
        item.proposal_claim_key: item for item in dependency_prechecks
    }
    if set(dependency_by_key) != set(
        proposal.depends_on_proposal_claim_keys
    ):
        reasons.append("dependency_precheck_set_mismatch")
    dependency_claim_ids: list[str] = []
    for dependency in dependency_prechecks:
        dependency_claim_ids.append(dependency.claim_id)
        if dependency.status is ClaimPrecheckStatus.REJECTED:
            reasons.append("dependency_claim_rejected")
        elif dependency.status in {
            ClaimPrecheckStatus.ADMISSIBLE_BOUNDED,
            ClaimPrecheckStatus.ADMISSIBLE_BOUNDARY,
        }:
            bounded = True

    satisfaction_pairs: list[tuple[str, str]] = []
    satisfied_obligations: set[str] = set()
    for satisfaction in satisfactions:
        if satisfaction.obligation_id not in proposal.obligation_ids:
            reasons.append("satisfaction_obligation_mismatch")
        if proposal.boundary_satisfaction_record_ids:
            if satisfaction.status is not (
                ObligationSatisfactionStatus.BOUNDARY
            ):
                reasons.append("boundary_satisfaction_required")
        elif satisfaction.status is not (
            ObligationSatisfactionStatus.SATISFIED
        ):
            reasons.append("obligation_not_satisfied")
        satisfied_obligations.add(satisfaction.obligation_id)
        satisfaction_pairs.append(
            (
                satisfaction.obligation_satisfaction_id,
                satisfaction.content_sha256,
            )
        )
    if satisfied_obligations != set(proposal.obligation_ids):
        reasons.append("obligation_closure_incomplete")

    if proposal.boundary_satisfaction_record_ids:
        effective_strength = ClaimStrengthCeiling.BOUNDARY_ONLY
        status = (
            ClaimPrecheckStatus.ADMISSIBLE_BOUNDARY
            if not reasons
            else ClaimPrecheckStatus.REJECTED
        )
    else:
        if not effective_ranks:
            reasons.append("accepted_evidence_use_required")
            effective_rank = 0
        else:
            effective_rank = min(effective_ranks)
        effective_strength = next(
            item
            for item, rank in _STRENGTH_RANK.items()
            if rank == effective_rank
        )
        if (
            _STRENGTH_RANK[proposal.requested_strength]
            > effective_rank
        ):
            reasons.append("claim_strength_exceeds_support")
        status = (
            ClaimPrecheckStatus.REJECTED
            if reasons
            else (
                ClaimPrecheckStatus.ADMISSIBLE_BOUNDED
                if bounded
                else ClaimPrecheckStatus.ADMISSIBLE_SUPPORTED
            )
        )
    canonical_reasons = tuple(
        sorted(
            set(
                reasons
                or [
                    (
                        "admissible_bounded"
                        if bounded
                        else "admissible"
                    )
                ]
            )
        )
    )
    use_pairs.sort()
    validity_pairs.sort()
    satisfaction_pairs.sort()
    check_pairs = sorted(
        (
            item.check_disposition_id,
            item.content_sha256,
        )
        for item in check_dispositions
    )
    claim_identity_material = {
        "answer_candidate_id": candidate.answer_candidate_id,
        "proposal": proposal,
        "use_pairs": use_pairs,
        "validity_pairs": validity_pairs,
        "satisfaction_pairs": satisfaction_pairs,
        "check_pairs": check_pairs,
        "dependency_claim_ids": tuple(sorted(dependency_claim_ids)),
    }
    claim_id = _id("answer-claim", claim_identity_material)
    derived_input_sha256 = content_sha256(
        {
            "candidate": candidate,
            "proposal": proposal,
            "supports": supports,
            "satisfactions": satisfactions,
            "check_dispositions": check_dispositions,
            "dependency_prechecks": dependency_prechecks,
        }
    )
    material = {
        "claim_id": claim_id,
        "answer_candidate_id": candidate.answer_candidate_id,
        "answer_candidate_content_sha256": candidate.content_sha256,
        "proposal_claim_key": proposal.proposal_claim_key,
        "case_id": candidate.case_id,
        "question_revision_id": candidate.question_revision_id,
        "frame_revision_id": candidate.frame_revision_id,
        "plan_revision_id": candidate.plan_revision_id,
        "plan_adoption_id": candidate.plan_adoption_id,
        "authority_snapshot": candidate.authority_snapshot,
        "authority_snapshot_content_sha256": (
            candidate.authority_snapshot_content_sha256
        ),
        "target_estimand_id": proposal.target_estimand_id,
        "obligation_ids": tuple(sorted(proposal.obligation_ids)),
        "evidence_use_binding_ids": tuple(
            item[0] for item in use_pairs
        ),
        "evidence_use_binding_content_sha256s": tuple(
            item[1] for item in use_pairs
        ),
        "latest_evidence_validity_ids": tuple(
            item[0] for item in validity_pairs
        ),
        "latest_evidence_validity_content_sha256s": tuple(
            item[1] for item in validity_pairs
        ),
        "obligation_satisfaction_record_ids": tuple(
            item[0] for item in satisfaction_pairs
        ),
        "obligation_satisfaction_content_sha256s": tuple(
            item[1] for item in satisfaction_pairs
        ),
        "scope_proof_ids": tuple(sorted(scope_proof_ids)),
        "window_exposure_proof_sha256s": tuple(
            sorted(window_exposure_proof_ids)
        ),
        "data_version_proof_sha256s": tuple(
            sorted(data_version_proof_ids)
        ),
        "analysis_check_disposition_ids": tuple(
            item[0] for item in check_pairs
        ),
        "analysis_check_disposition_content_sha256s": tuple(
            item[1] for item in check_pairs
        ),
        "dependency_claim_ids": tuple(sorted(dependency_claim_ids)),
        "applicability_scope": proposal.applicability_scope,
        "requested_strength": proposal.requested_strength,
        "effective_strength": effective_strength,
        "status": status,
        "reason_codes": canonical_reasons,
        "required_limitation_refs": tuple(sorted(required_limitations)),
        "derived_input_sha256": derived_input_sha256,
        "policy_version": CLAIM_PRECHECK_POLICY_VERSION,
    }
    return ClaimPrecheckRecord(
        claim_precheck_id=_id("claim-precheck", material),
        checked_at=checked_at,
        **material,
    )


def compile_provisional_answer_bundle(
    *,
    candidate: ProvisionalAnswerCandidate,
    current_authority: AuthoritySnapshot,
    plan_adoption: PlanAdoptionRecord,
    supports_by_claim_key: dict[
        str, tuple[ClaimEvidenceSupport, ...]
    ],
    satisfactions_by_claim_key: dict[
        str, tuple[ObligationSatisfactionRecord, ...]
    ],
    check_dispositions_by_claim_key: dict[
        str, tuple[AnalysisCheckDisposition, ...]
    ],
    checked_at: datetime,
) -> ProvisionalAnswerBundle:
    validate_provisional_answer_candidate(
        candidate=candidate,
        current_authority=current_authority,
        plan_adoption=plan_adoption,
    )
    known_keys = {
        item.proposal_claim_key for item in candidate.claims
    }
    if set(supports_by_claim_key) - known_keys:
        raise ValueError("supports reference unknown proposal claim")
    if set(satisfactions_by_claim_key) - known_keys:
        raise ValueError("satisfactions reference unknown proposal claim")
    if set(check_dispositions_by_claim_key) - known_keys:
        raise ValueError(
            "analysis checks reference unknown proposal claim"
        )
    proposal_by_key = {
        item.proposal_claim_key: item for item in candidate.claims
    }
    precheck_by_key: dict[str, ClaimPrecheckRecord] = {}
    visiting: set[str] = set()

    def build_precheck(claim_key: str) -> ClaimPrecheckRecord:
        if claim_key in precheck_by_key:
            return precheck_by_key[claim_key]
        if claim_key in visiting:
            raise ValueError("claim dependency graph is cyclic")
        visiting.add(claim_key)
        proposal = proposal_by_key[claim_key]
        dependencies = tuple(
            build_precheck(dependency_key)
            for dependency_key in (
                proposal.depends_on_proposal_claim_keys
            )
        )
        result = _precheck_claim(
            candidate=candidate,
            proposal=proposal,
            supports=supports_by_claim_key.get(
                claim_key, ()
            ),
            satisfactions=satisfactions_by_claim_key.get(
                claim_key, ()
            ),
            check_dispositions=check_dispositions_by_claim_key.get(
                claim_key, ()
            ),
            dependency_prechecks=dependencies,
            checked_at=checked_at,
        )
        visiting.remove(claim_key)
        precheck_by_key[claim_key] = result
        return result

    prechecks = tuple(
        build_precheck(proposal.proposal_claim_key)
        for proposal in candidate.claims
    )
    if any(
        item.status is ClaimPrecheckStatus.REJECTED
        for item in prechecks
    ):
        return ProvisionalAnswerBundle(
            candidate=candidate,
            prechecks=prechecks,
            status=AnswerCandidateStatus.REJECTED,
            answer=None,
        )
    answer_claims = tuple(
        _build_answer_claim(
            candidate=candidate,
            proposal=proposal,
            precheck=precheck,
        )
        for proposal, precheck in zip(
            candidate.claims, prechecks, strict=True
        )
    )
    claim_id_by_key = {
        item.proposal_claim_key: item.claim_id
        for item in answer_claims
    }
    narrative_blocks = tuple(
        _build_answer_narrative_block(
            candidate=candidate,
            proposal=block,
            claim_id_by_key=claim_id_by_key,
        )
        for block in candidate.narrative_blocks
    )
    material = {
        "answer_candidate_id": candidate.answer_candidate_id,
        "case_id": candidate.case_id,
        "question_revision_id": candidate.question_revision_id,
        "frame_revision_id": candidate.frame_revision_id,
        "plan_revision_id": candidate.plan_revision_id,
        "plan_adoption_id": candidate.plan_adoption_id,
        "accepted_head_version": candidate.accepted_head_version,
        "version_number": candidate.version_number,
        "prior_answer_version_id": candidate.prior_answer_version_id,
        "status": AnswerStatus.PROVISIONAL,
        "claims": answer_claims,
        "claim_precheck_ids": tuple(
            item.claim_precheck_id for item in prechecks
        ),
        "claim_precheck_content_sha256s": tuple(
            item.content_sha256 for item in prechecks
        ),
        "narrative_blocks": narrative_blocks,
        "created_by_action_id": candidate.created_by_action_id,
        "identity_version": ANSWER_IDENTITY_VERSION,
    }
    answer = AnswerVersion(
        answer_version_id=_id("answer-version", material),
        created_at=candidate.created_at,
        **material,
    )
    return ProvisionalAnswerBundle(
        candidate=candidate,
        prechecks=prechecks,
        status=AnswerCandidateStatus.ACCEPTED_PROVISIONAL,
        answer=answer,
    )


def validate_provisional_answer_candidate(
    *,
    candidate: ProvisionalAnswerCandidate,
    current_authority: AuthoritySnapshot,
    plan_adoption: PlanAdoptionRecord,
) -> None:
    if (
        candidate.case_id != current_authority.case_id
        or candidate.accepted_head_version != current_authority.head_version
        or candidate.question_revision_id
        != current_authority.accepted_question_revision_id
        or candidate.frame_revision_id
        != current_authority.accepted_frame_revision_id
        or candidate.plan_revision_id
        != current_authority.accepted_plan_revision_id
        or candidate.plan_adoption_id
        != plan_adoption.plan_adoption_id
        or candidate.plan_adoption_content_sha256
        != plan_adoption.content_sha256
        or candidate.authority_snapshot != current_authority
        or candidate.authority_snapshot_content_sha256
        != current_authority.content_sha256
    ):
        raise ValueError("answer candidate authority is stale")
    material = {
        name: getattr(candidate, name)
        for name in candidate.__dataclass_fields__
        if name
        not in {
            "answer_candidate_id",
            "created_at",
            "schema_epoch",
        }
    }
    if candidate.answer_candidate_id != _id(
        "answer-candidate", material
    ):
        raise ValueError("answer candidate identity is forged")


def _build_answer_claim(
    *,
    candidate: ProvisionalAnswerCandidate,
    proposal: ProposedClaim,
    precheck: ClaimPrecheckRecord,
) -> AnswerClaim:
    if precheck.status is ClaimPrecheckStatus.REJECTED:
        raise ValueError("rejected claim cannot enter AnswerVersion")
    material = {
        "proposal_claim_key": proposal.proposal_claim_key,
        "statement": proposal.statement,
        "target_estimand_id": proposal.target_estimand_id,
        "obligation_ids": tuple(sorted(proposal.obligation_ids)),
        "evidence_use_binding_ids": (
            precheck.evidence_use_binding_ids
        ),
        "boundary_satisfaction_record_ids": (
            tuple(sorted(proposal.boundary_satisfaction_record_ids))
        ),
        "applicability_scope": proposal.applicability_scope,
        "claim_strength": precheck.effective_strength,
        "limitation_refs": tuple(sorted(proposal.limitation_refs)),
        "analysis_check_disposition_ids": (
            precheck.analysis_check_disposition_ids
        ),
        "dependency_claim_ids": precheck.dependency_claim_ids,
        "claim_precheck_id": precheck.claim_precheck_id,
        "claim_precheck_content_sha256": precheck.content_sha256,
    }
    return AnswerClaim(
        claim_id=precheck.claim_id,
        **material,
    )


def _build_answer_narrative_block(
    *,
    candidate: ProvisionalAnswerCandidate,
    proposal: NarrativeBlockProposal,
    claim_id_by_key: dict[str, str],
) -> AnswerNarrativeBlock:
    claim_ids = tuple(
        claim_id_by_key[key]
        for key in proposal.proposal_claim_keys
    )
    material = {
        "answer_candidate_id": candidate.answer_candidate_id,
        "block_key": proposal.block_key,
        "markdown": proposal.markdown,
        "claim_ids": claim_ids,
    }
    return AnswerNarrativeBlock(
        block_id=_id("answer-narrative-block", material),
        block_key=proposal.block_key,
        markdown=proposal.markdown,
        claim_ids=claim_ids,
    )


def derive_settlement_precondition_report(
    *,
    answer: AnswerVersion,
    candidate: ProvisionalAnswerCandidate,
    prechecks: tuple[ClaimPrecheckRecord, ...],
    supports: tuple[ClaimEvidenceSupport, ...],
    satisfactions: tuple[ObligationSatisfactionRecord, ...],
    current_authority: AuthoritySnapshot,
    plan_adoption: PlanAdoptionRecord,
    objection_disposition_refs: tuple[str, ...],
    unresolved_blocking_objection_refs: tuple[str, ...],
    trace_manifest_id: str,
    trace_manifest_content_sha256: str,
    trace_complete: bool,
    created_at: datetime,
) -> SettlementPreconditionReport:
    reasons: set[str] = set()
    if (
        answer.case_id != current_authority.case_id
        or answer.question_revision_id
        != current_authority.accepted_question_revision_id
        or answer.frame_revision_id
        != current_authority.accepted_frame_revision_id
        or answer.plan_revision_id
        != current_authority.accepted_plan_revision_id
        or any(
            getattr(current_authority, field_name)
            != getattr(candidate.authority_snapshot, field_name)
            for field_name in (
                "case_id",
                "mailbox_authority_epoch",
                "accepted_question_revision_id",
                "accepted_frame_revision_id",
                "accepted_plan_revision_id",
                "active_frame_candidate_generation",
                "active_frame_candidate_sha256",
            )
        )
    ):
        reasons.add("stale_answer_authority")
    if (
        answer.answer_candidate_id != candidate.answer_candidate_id
        or answer.plan_adoption_id != plan_adoption.plan_adoption_id
        or candidate.plan_adoption_content_sha256
        != plan_adoption.content_sha256
    ):
        reasons.add("answer_candidate_or_adoption_mismatch")
    if (
        tuple(item.claim_precheck_id for item in prechecks)
        != answer.claim_precheck_ids
        or tuple(item.content_sha256 for item in prechecks)
        != answer.claim_precheck_content_sha256s
        or any(
            item.status is ClaimPrecheckStatus.REJECTED
            for item in prechecks
        )
    ):
        reasons.add("claim_precheck_closure_invalid")
    for support in supports:
        if support.validity.status is not (
            EvidenceValidityStatus.ADMITTED_VALID
        ):
            reasons.add("evidence_not_currently_valid")
        if (
            support.use_binding.evidence_validity_id
            != support.validity.evidence_validity_id
            or support.use_binding.evidence_validity_content_sha256
            != support.validity.content_sha256
        ):
            reasons.add("evidence_use_not_latest")
        if isinstance(
            support.evidence.execution_provenance,
            ConformanceExecutionProvenance,
        ):
            reasons.add("production_evidence_unavailable")
        elif not isinstance(
            support.evidence.execution_provenance,
            PhysicalQueryExecutionProvenance,
        ):
            reasons.add("execution_provenance_unknown")
    if any(
        item.status
        not in {
            ObligationSatisfactionStatus.SATISFIED,
            ObligationSatisfactionStatus.BOUNDARY,
        }
        for item in satisfactions
    ):
        reasons.add("obligation_closure_incomplete")
    precheck_satisfaction_pairs = {
        pair
        for precheck in prechecks
        for pair in zip(
            precheck.obligation_satisfaction_record_ids,
            precheck.obligation_satisfaction_content_sha256s,
            strict=True,
        )
    }
    current_satisfaction_pairs = {
        (
            item.obligation_satisfaction_id,
            item.content_sha256,
        )
        for item in satisfactions
    }
    if current_satisfaction_pairs != precheck_satisfaction_pairs:
        reasons.add("obligation_closure_changed")
    if unresolved_blocking_objection_refs:
        reasons.add("blocking_objection_open")
    if not trace_complete:
        reasons.add("trace_incomplete")
    status = (
        SettlementPreconditionStatus.BLOCKED
        if reasons
        else SettlementPreconditionStatus.ELIGIBLE_FOR_FUTURE_SETTLEMENT
    )
    claim_ids = tuple(item.claim_id for item in answer.claims)
    use_ids = tuple(
        sorted(
            {
                support.use_binding.evidence_use_binding_id
                for support in supports
            }
        )
    )
    validity_ids = tuple(
        sorted(
            {
                support.validity.evidence_validity_id
                for support in supports
            }
        )
    )
    satisfaction_ids = tuple(
        sorted(
            {
                item.obligation_satisfaction_id
                for item in satisfactions
            }
        )
    )
    derived_input_sha256 = content_sha256(
        {
            "answer": answer,
            "candidate": candidate,
            "prechecks": prechecks,
            "supports": supports,
            "satisfactions": satisfactions,
            "current_authority": current_authority,
            "plan_adoption": plan_adoption,
            "objection_disposition_refs": (
                tuple(sorted(objection_disposition_refs))
            ),
            "unresolved_blocking_objection_refs": (
                tuple(sorted(unresolved_blocking_objection_refs))
            ),
            "trace_manifest_id": trace_manifest_id,
            "trace_manifest_content_sha256": (
                trace_manifest_content_sha256
            ),
            "trace_complete": trace_complete,
        }
    )
    material = {
        "case_id": answer.case_id,
        "question_revision_id": answer.question_revision_id,
        "frame_revision_id": answer.frame_revision_id,
        "plan_revision_id": answer.plan_revision_id,
        "plan_adoption_id": plan_adoption.plan_adoption_id,
        "plan_adoption_content_sha256": plan_adoption.content_sha256,
        "accepted_head_version": current_authority.head_version,
        "answer_version_id": answer.answer_version_id,
        "answer_version_content_sha256": answer.content_sha256,
        "claim_ids": claim_ids,
        "claim_precheck_ids": answer.claim_precheck_ids,
        "evidence_use_binding_ids": use_ids,
        "evidence_validity_ids": validity_ids,
        "obligation_satisfaction_record_ids": satisfaction_ids,
        "objection_disposition_refs": tuple(
            sorted(objection_disposition_refs)
        ),
        "trace_manifest_id": trace_manifest_id,
        "trace_manifest_content_sha256": trace_manifest_content_sha256,
        "status": status,
        "fail_reason_codes": tuple(sorted(reasons)),
        "derived_input_sha256": derived_input_sha256,
        "policy_version": SETTLEMENT_POLICY_VERSION,
    }
    return SettlementPreconditionReport(
        settlement_precondition_report_id=_id(
            "settlement-precondition", material
        ),
        created_at=created_at,
        **material,
    )


def _require_unique_nonempty_tuple(
    values: tuple[str, ...],
    name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")
    for value in values:
        require_nonempty(value, name)


def _require_typed_tuple(
    values: tuple[object, ...],
    expected: type[object],
    name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    if not all(isinstance(value, expected) for value in values):
        raise TypeError(f"{name} must contain {expected.__name__}")


def _require_acyclic_claim_dependencies(
    claims: tuple[ProposedClaim, ...],
) -> None:
    dependencies = {
        item.proposal_claim_key: item.depends_on_proposal_claim_keys
        for item in claims
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(claim_key: str) -> None:
        if claim_key in visited:
            return
        if claim_key in visiting:
            raise ValueError("claim dependency graph must be acyclic")
        visiting.add(claim_key)
        for dependency in dependencies[claim_key]:
            visit(dependency)
        visiting.remove(claim_key)
        visited.add(claim_key)

    for key in dependencies:
        visit(key)
