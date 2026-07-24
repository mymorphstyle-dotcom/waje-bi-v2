from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from bi_agent.runtime.capability_authority import (
    CapabilityOutcome,
    EvidenceLedgerEntry,
)
from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    ClaimAuthorityNamespace,
    ClaimGraph,
    ClaimKey,
    ClaimPublicationCeiling,
    ClaimRevision,
    ClaimVerifierReport,
    ClaimVeto,
    LocalBoundaryAuthority,
    ObligationCoverage,
    RecommendationRecord,
    SemanticVerificationAttempt,
    SemanticVerificationDecision,
    SupportEdge,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.plan_authority import ClaimObligation


class ClaimSettlementContractError(ValueError):
    pass


_CANDIDATE_CLAIM_KINDS = frozenset(
    {"business_object_candidate_impact", "candidate_mechanism"}
)
_SOURCE_CLASSES_ALLOWED_FOR_CANDIDATE = frozenset(
    {
        "observed_fact",
        "accounting_identity_contribution",
        "dimension_localization",
        "statistical_association",
        "candidate_mechanism",
    }
)
_CLASS_STRENGTH_ORDER = MappingProxyType(
    {
        "observed_fact": ("descriptive", "directional"),
        "accounting_identity_contribution": (
            "accounting_contribution",
            "quantified_contribution",
        ),
        "dimension_localization": (
            "dimension_localization",
            "directional",
            "candidate_driver",
        ),
        "statistical_association": (
            "directional",
            "anomaly_candidate",
            "statistical_association",
            "candidate_driver",
            "recurring_pattern",
        ),
        "candidate_mechanism": ("candidate_mechanism",),
        "candidate_impact": ("candidate_driver",),
        "scenario": ("scenario",),
        "boundary": ("boundary", "trust_boundary"),
    }
)


@dataclass(frozen=True)
class _EvidenceCompatibility:
    source_claim_class: str
    publication_ceiling: ClaimPublicationCeiling
    evidence_contract_type: str


def _compatibility(
    *,
    evidence_kind: str,
    source_claim_kind: str,
    maximum_claim_strength: str,
) -> _EvidenceCompatibility:
    claim_class: str | None = None
    evidence_contract_type: str | None = None
    if source_claim_kind in {"baseline_stability", "comparative_change"}:
        if evidence_kind == "observed" and maximum_claim_strength in {
            "descriptive",
            "directional",
        }:
            claim_class = "observed_fact"
            evidence_contract_type = "observed"
        elif evidence_kind == "statistical_association" and maximum_claim_strength in {
            "directional",
            "anomaly_candidate",
            "statistical_association",
            "candidate_driver",
            "recurring_pattern",
        }:
            claim_class = "statistical_association"
            evidence_contract_type = "statistical_association"
        elif evidence_kind == "derived" and maximum_claim_strength in {
            "dimension_localization",
            "directional",
            "candidate_driver",
        }:
            claim_class = "dimension_localization"
            evidence_contract_type = "derived"
    elif source_claim_kind == "external_shock_candidate_or_anomaly":
        if (
            evidence_kind == "statistical_association"
            and maximum_claim_strength == "anomaly_candidate"
        ):
            claim_class = "statistical_association"
            evidence_contract_type = "statistical_association"
        elif (
            evidence_kind == "derived" and maximum_claim_strength == "candidate_driver"
        ):
            claim_class = "dimension_localization"
            evidence_contract_type = "derived"
    elif source_claim_kind == "formula_component_contribution":
        if evidence_kind == "derived" and maximum_claim_strength in {
            "accounting_contribution",
            "quantified_contribution",
        }:
            claim_class = "accounting_identity_contribution"
            evidence_contract_type = "derived"
    elif source_claim_kind == "segment_contribution_or_mix_shift":
        if evidence_kind == "derived" and maximum_claim_strength in {
            "dimension_localization",
            "directional",
            "candidate_driver",
        }:
            claim_class = "dimension_localization"
            evidence_contract_type = "derived"
        elif evidence_kind == "derived" and maximum_claim_strength in {
            "accounting_contribution",
            "quantified_contribution",
        }:
            claim_class = "accounting_identity_contribution"
            evidence_contract_type = "derived"
        elif evidence_kind == "statistical_association" and maximum_claim_strength in {
            "directional",
            "anomaly_candidate",
            "statistical_association",
            "candidate_driver",
            "recurring_pattern",
        }:
            claim_class = "statistical_association"
            evidence_contract_type = "statistical_association"
        elif evidence_kind == "observed" and maximum_claim_strength in {
            "descriptive",
            "directional",
        }:
            claim_class = "observed_fact"
            evidence_contract_type = "observed"
    elif source_claim_kind in {
        "cross_source_statistical_association",
        "recurring_pattern_existence",
    }:
        allowed = (
            {"statistical_association", "candidate_driver"}
            if source_claim_kind == "cross_source_statistical_association"
            else {"statistical_association", "recurring_pattern"}
        )
        if (
            evidence_kind == "statistical_association"
            and maximum_claim_strength in allowed
        ):
            claim_class = "statistical_association"
            evidence_contract_type = "statistical_association"
        elif (
            source_claim_kind == "recurring_pattern_existence"
            and evidence_kind == "observed"
            and maximum_claim_strength in {"descriptive", "directional"}
        ):
            claim_class = "observed_fact"
            evidence_contract_type = "observed"
    elif (
        source_claim_kind == "business_object_candidate_impact"
        and evidence_kind == "observed"
        and maximum_claim_strength == "directional"
    ):
        claim_class = "observed_fact"
        evidence_contract_type = "observed"
    elif source_claim_kind in _CANDIDATE_CLAIM_KINDS:
        if (
            evidence_kind == "observed"
            and maximum_claim_strength == "candidate_mechanism"
        ):
            claim_class = "candidate_mechanism"
            evidence_contract_type = "observed"
        elif evidence_kind == "statistical_association" and maximum_claim_strength in {
            "directional",
            "anomaly_candidate",
            "statistical_association",
            "candidate_driver",
            "recurring_pattern",
        }:
            claim_class = "statistical_association"
            evidence_contract_type = "statistical_association"
    elif source_claim_kind == "source_reconciliation":
        if evidence_kind == "derived" and maximum_claim_strength in {
            "accounting_contribution",
            "quantified_contribution",
        }:
            claim_class = "accounting_identity_contribution"
            evidence_contract_type = "derived"
    elif source_claim_kind == "contract_coverage_and_trust_boundary":
        if evidence_kind == "boundary" and maximum_claim_strength in {
            "boundary",
            "trust_boundary",
        }:
            claim_class = "boundary"
            evidence_contract_type = "boundary"
    elif source_claim_kind == "scenario":
        if evidence_kind == "scenario" and maximum_claim_strength == "scenario":
            claim_class = "scenario"
            evidence_contract_type = "scenario"
    if claim_class is None or evidence_contract_type is None:
        raise ClaimSettlementContractError(
            "claim_settlement_ceiling_compatibility_missing:"
            f"{evidence_kind}:{source_claim_kind}:{maximum_claim_strength}"
        )
    return _EvidenceCompatibility(
        source_claim_class=claim_class,
        publication_ceiling=ClaimPublicationCeiling.create(
            claim_class=claim_class,
            strength=maximum_claim_strength,
        ),
        evidence_contract_type=evidence_contract_type,
    )


def evidence_publication_ceiling(
    *,
    evidence_kind: str,
    source_claim_kind: str,
    maximum_claim_strength: str,
) -> ClaimPublicationCeiling:
    """Return the publication ceiling admitted by claim settlement authority."""

    return _compatibility(
        evidence_kind=evidence_kind,
        source_claim_kind=source_claim_kind,
        maximum_claim_strength=maximum_claim_strength,
    ).publication_ceiling


def admissible_evidence_publication_ceiling(
    *,
    evidence_kind: str,
    source_claim_kind: str,
    maximum_claim_strength: str,
) -> ClaimPublicationCeiling | None:
    """Return a ceiling only when the evidence/claim pairing is admissible."""

    try:
        return evidence_publication_ceiling(
            evidence_kind=evidence_kind,
            source_claim_kind=source_claim_kind,
            maximum_claim_strength=maximum_claim_strength,
        )
    except ClaimSettlementContractError as exc:
        if not str(exc).startswith("claim_settlement_ceiling_compatibility_missing:"):
            raise
        return None


def publication_ceiling_satisfies(
    ceiling: ClaimPublicationCeiling,
    *,
    required_strength: str,
) -> bool:
    """Check a claim-class-local strength requirement without global rank guesses."""

    if type(ceiling) is not ClaimPublicationCeiling:
        raise ClaimSettlementContractError(
            "claim_settlement_publication_ceiling_invalid"
        )
    replayed = ClaimPublicationCeiling.from_dict(ceiling.to_dict())
    allowed = _CLASS_STRENGTH_ORDER[replayed.claim_class]
    if required_strength not in allowed:
        return False
    return allowed.index(replayed.strength) >= allowed.index(required_strength)


def _plain(value: Any) -> Any:
    return canonical_value(value)


def _freeze(value: Any, error: str) -> Any:
    try:
        normalized = canonical_value(value)
    except ValueError as exc:
        raise ClaimSettlementContractError(error) from exc
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item, error) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_freeze(item, error) for item in normalized)
    return normalized


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ClaimSettlementContractError(error)
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
        raise ClaimSettlementContractError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ClaimSettlementContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise ClaimSettlementContractError(error)
    if len(normalized) != len(set(normalized)):
        raise ClaimSettlementContractError(error)
    return tuple(sorted(normalized)) if sort else normalized


def _strict_shape(payload: Any, record_type: type, error: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != set(
        record_type.__dataclass_fields__
    ):
        raise ClaimSettlementContractError(error)
    return payload


def _namespace_token(authority_namespace_ref: str) -> str:
    prefix = "claim-authority-namespace:sha256:"
    if not authority_namespace_ref.startswith(prefix):
        raise ClaimSettlementContractError("claim_settlement_namespace_ref_invalid")
    return authority_namespace_ref.removeprefix(prefix)[:24]


def _record_ref(kind: str, authority_namespace_ref: str, digest: str) -> str:
    return f"{kind}:{_namespace_token(authority_namespace_ref)}:sha256:{digest}"


def _validated_namespace(value: ClaimAuthorityNamespace) -> ClaimAuthorityNamespace:
    if type(value) is not ClaimAuthorityNamespace:
        raise ClaimSettlementContractError("claim_settlement_namespace_invalid")
    try:
        replayed = ClaimAuthorityNamespace.from_dict(value.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ClaimSettlementContractError(
            "claim_settlement_namespace_invalid"
        ) from exc
    if replayed != value:
        raise ClaimSettlementContractError("claim_settlement_namespace_invalid")
    return replayed


def _validated_execution_result(
    value: AuthoritativeExecutionResult,
) -> AuthoritativeExecutionResult:
    if type(value) is not AuthoritativeExecutionResult:
        raise ClaimSettlementContractError("claim_settlement_execution_result_invalid")
    return value


@dataclass(frozen=True)
class CandidateEvidenceSupport:
    support_ref: str
    authority_namespace_ref: str
    evidence_entry_ref: str
    source_claim_kind: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        evidence_entry_ref: str,
        source_claim_kind: str,
    ) -> "CandidateEvidenceSupport":
        namespace = _validated_namespace(authority_namespace)
        body = {
            "evidence_entry_ref": _required_string(
                evidence_entry_ref,
                "candidate_evidence_support_entry_ref_invalid",
            ),
            "source_claim_kind": _required_string(
                source_claim_kind,
                "candidate_evidence_support_claim_kind_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            support_ref=_record_ref(
                "candidate-evidence-support", namespace.authority_namespace_ref, digest
            ),
            authority_namespace_ref=namespace.authority_namespace_ref,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_namespace: ClaimAuthorityNamespace,
    ) -> "CandidateEvidenceSupport":
        payload = _strict_shape(
            payload, cls, "candidate_evidence_support_shape_invalid"
        )
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            evidence_entry_ref=payload["evidence_entry_ref"],
            source_claim_kind=payload["source_claim_kind"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimSettlementContractError(
                "candidate_evidence_support_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class CandidateClaimProposal:
    candidate_proposal_ref: str
    authority_namespace_ref: str
    proposal_item_ref: str
    obligation_id: str
    subject: str
    factual_payload: Mapping[str, Any]
    evidence_support: tuple[CandidateEvidenceSupport, ...]
    assumption_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        proposal_item_ref: str,
        obligation_id: str,
        subject: str,
        factual_payload: Mapping[str, Any],
        evidence_support: Sequence[CandidateEvidenceSupport],
        limitation_refs: Sequence[str],
        assumption_refs: Sequence[str] = (),
    ) -> "CandidateClaimProposal":
        namespace = _validated_namespace(authority_namespace)
        if not isinstance(factual_payload, Mapping) or not factual_payload:
            raise ClaimSettlementContractError(
                "candidate_claim_proposal_payload_invalid"
            )
        if isinstance(evidence_support, (str, bytes)) or not isinstance(
            evidence_support, Sequence
        ):
            raise ClaimSettlementContractError(
                "candidate_claim_proposal_support_invalid"
            )
        supports: list[CandidateEvidenceSupport] = []
        for raw_support in evidence_support:
            if type(raw_support) is not CandidateEvidenceSupport:
                raise ClaimSettlementContractError(
                    "candidate_claim_proposal_support_invalid"
                )
            try:
                replayed = CandidateEvidenceSupport.from_dict(
                    raw_support.to_dict(), authority_namespace=namespace
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise ClaimSettlementContractError(
                    "candidate_claim_proposal_support_invalid"
                ) from exc
            if replayed != raw_support:
                raise ClaimSettlementContractError(
                    "candidate_claim_proposal_support_invalid"
                )
            supports.append(replayed)
        support = tuple(sorted(supports, key=lambda item: item.support_ref))
        if not support or len({item.support_ref for item in support}) != len(support):
            raise ClaimSettlementContractError(
                "candidate_claim_proposal_support_invalid"
            )
        body = {
            "proposal_item_ref": _required_string(
                proposal_item_ref,
                "candidate_claim_proposal_item_ref_invalid",
            ),
            "obligation_id": _required_string(
                obligation_id,
                "candidate_claim_proposal_obligation_id_invalid",
            ),
            "subject": _required_string(
                subject,
                "candidate_claim_proposal_subject_invalid",
            ),
            "factual_payload": _freeze(
                factual_payload,
                "candidate_claim_proposal_payload_invalid",
            ),
            "evidence_support": support,
            "assumption_refs": _string_tuple(
                assumption_refs,
                "candidate_claim_proposal_assumptions_invalid",
            ),
            "limitation_refs": _string_tuple(
                limitation_refs,
                "candidate_claim_proposal_limitations_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            candidate_proposal_ref=_record_ref(
                "candidate-claim-proposal", namespace.authority_namespace_ref, digest
            ),
            authority_namespace_ref=namespace.authority_namespace_ref,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_namespace: ClaimAuthorityNamespace,
    ) -> "CandidateClaimProposal":
        payload = _strict_shape(payload, cls, "candidate_claim_proposal_shape_invalid")
        raw_support = payload["evidence_support"]
        if isinstance(raw_support, (str, bytes)) or not isinstance(
            raw_support, Sequence
        ):
            raise ClaimSettlementContractError(
                "candidate_claim_proposal_support_invalid"
            )
        support = tuple(
            CandidateEvidenceSupport.from_dict(
                item, authority_namespace=authority_namespace
            )
            for item in raw_support
        )
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            proposal_item_ref=payload["proposal_item_ref"],
            obligation_id=payload["obligation_id"],
            subject=payload["subject"],
            factual_payload=payload["factual_payload"],
            evidence_support=support,
            assumption_refs=payload["assumption_refs"],
            limitation_refs=payload["limitation_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimSettlementContractError(
                "candidate_claim_proposal_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ObligationSettlementBasis:
    basis_ref: str
    authority_namespace_ref: str
    obligation_id: str
    success_policy: Mapping[str, Any]
    required_claim_strength: str
    proposed_claim_refs: tuple[str, ...]
    non_claim_support_evidence_refs: tuple[str, ...]
    unavailable_limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        obligation_id: str,
        success_policy: Mapping[str, Any],
        proposed_claim_refs: Sequence[str],
        non_claim_support_evidence_refs: Sequence[str],
        unavailable_limitation_refs: Sequence[str],
    ) -> "ObligationSettlementBasis":
        namespace = _validated_namespace(authority_namespace)
        policy = _freeze(
            success_policy,
            "obligation_settlement_basis_success_policy_invalid",
        )
        if policy.get("policy") != "verified_or_explicit_boundary":
            raise ClaimSettlementContractError(
                "obligation_settlement_basis_success_policy_invalid"
            )
        required_strength = _required_string(
            policy.get("minimum_claim_strength"),
            "obligation_settlement_basis_success_policy_invalid",
        )
        body = {
            "obligation_id": _required_string(
                obligation_id, "obligation_settlement_basis_id_invalid"
            ),
            "success_policy": policy,
            "required_claim_strength": required_strength,
            "proposed_claim_refs": _string_tuple(
                proposed_claim_refs,
                "obligation_settlement_basis_claim_refs_invalid",
            ),
            "non_claim_support_evidence_refs": _string_tuple(
                non_claim_support_evidence_refs,
                "obligation_settlement_basis_evidence_refs_invalid",
            ),
            "unavailable_limitation_refs": _string_tuple(
                unavailable_limitation_refs,
                "obligation_settlement_basis_limitations_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            basis_ref=_record_ref(
                "obligation-settlement-basis", namespace.authority_namespace_ref, digest
            ),
            authority_namespace_ref=namespace.authority_namespace_ref,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_namespace: ClaimAuthorityNamespace,
    ) -> "ObligationSettlementBasis":
        payload = _strict_shape(
            payload, cls, "obligation_settlement_basis_shape_invalid"
        )
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            obligation_id=payload["obligation_id"],
            success_policy=payload["success_policy"],
            proposed_claim_refs=payload["proposed_claim_refs"],
            non_claim_support_evidence_refs=payload["non_claim_support_evidence_refs"],
            unavailable_limitation_refs=payload["unavailable_limitation_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimSettlementContractError(
                "obligation_settlement_basis_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ClaimSettlementCheckpoint:
    checkpoint_ref: str
    authority_namespace: ClaimAuthorityNamespace
    authority_namespace_ref: str
    execution_result_ref: str
    execution_result_digest: str
    plan_revision_id: str
    proposed_claim_keys: tuple[ClaimKey, ...]
    proposed_claims: tuple[ClaimRevision, ...]
    proposed_support_edges: tuple[SupportEdge, ...]
    obligation_basis: tuple[ObligationSettlementBasis, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        execution_result_ref: str,
        execution_result_digest: str,
        plan_revision_id: str,
        proposed_claim_keys: Sequence[ClaimKey],
        proposed_claims: Sequence[ClaimRevision],
        proposed_support_edges: Sequence[SupportEdge],
        obligation_basis: Sequence[ObligationSettlementBasis],
    ) -> "ClaimSettlementCheckpoint":
        namespace = _validated_namespace(authority_namespace)
        keys, claims, edges = _replay_proposed_authority(
            namespace,
            proposed_claim_keys,
            proposed_claims,
            proposed_support_edges,
        )
        basis = _replay_obligation_basis(namespace, obligation_basis)
        proposed_refs = {item.claim_ref for item in claims}
        if {ref for item in basis for ref in item.proposed_claim_refs} != proposed_refs:
            raise ClaimSettlementContractError(
                "claim_settlement_checkpoint_obligation_closure_invalid"
            )
        if not claims and any(not item.unavailable_limitation_refs for item in basis):
            raise ClaimSettlementContractError(
                "claim_settlement_checkpoint_empty_non_boundary_invalid"
            )
        body = {
            "execution_result_ref": _required_string(
                execution_result_ref,
                "claim_settlement_checkpoint_execution_ref_invalid",
            ),
            "execution_result_digest": _digest(
                execution_result_digest,
                "claim_settlement_checkpoint_execution_digest_invalid",
            ),
            "plan_revision_id": _required_string(
                plan_revision_id,
                "claim_settlement_checkpoint_plan_revision_invalid",
            ),
            "proposed_claim_keys": keys,
            "proposed_claims": claims,
            "proposed_support_edges": edges,
            "obligation_basis": basis,
        }
        digest = canonical_digest(body)
        return cls(
            checkpoint_ref=_record_ref(
                "claim-settlement-checkpoint", namespace.authority_namespace_ref, digest
            ),
            authority_namespace=namespace,
            authority_namespace_ref=namespace.authority_namespace_ref,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClaimSettlementCheckpoint":
        payload = _strict_shape(
            payload, cls, "claim_settlement_checkpoint_shape_invalid"
        )
        raw_namespace = payload["authority_namespace"]
        if not isinstance(raw_namespace, Mapping):
            raise ClaimSettlementContractError(
                "claim_settlement_checkpoint_namespace_invalid"
            )
        namespace = ClaimAuthorityNamespace.from_dict(raw_namespace)
        keys, edges = _claim_keys_and_edges_from_payload(
            namespace,
            payload["proposed_claim_keys"],
            payload["proposed_support_edges"],
        )
        claims = _claims_from_payload(
            namespace,
            payload["proposed_claims"],
            keys,
            edges,
        )
        raw_basis = payload["obligation_basis"]
        if isinstance(raw_basis, (str, bytes)) or not isinstance(raw_basis, Sequence):
            raise ClaimSettlementContractError(
                "claim_settlement_checkpoint_basis_invalid"
            )
        basis = tuple(
            ObligationSettlementBasis.from_dict(item, authority_namespace=namespace)
            for item in raw_basis
        )
        rebuilt = cls.create(
            authority_namespace=namespace,
            execution_result_ref=payload["execution_result_ref"],
            execution_result_digest=payload["execution_result_digest"],
            plan_revision_id=payload["plan_revision_id"],
            proposed_claim_keys=keys,
            proposed_claims=claims,
            proposed_support_edges=edges,
            obligation_basis=basis,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimSettlementContractError(
                "claim_settlement_checkpoint_integrity_invalid"
            )
        return rebuilt

    def verification_attempt(
        self,
        *,
        provider_ref: str,
        model_ref: str,
        input_digest: str,
        attempt_number: int,
        raw_provider_response_ref: str,
        raw_provider_response_digest: str,
    ) -> SemanticVerificationAttempt:
        if not self.proposed_claims:
            raise ClaimSettlementContractError(
                "claim_settlement_boundary_verification_attempt_forbidden"
            )
        return SemanticVerificationAttempt.create(
            authority_namespace=self.authority_namespace,
            purpose="claim_settlement",
            authority_input_ref=self.checkpoint_ref,
            authority_input_digest=self.content_digest,
            subject_refs=tuple(item.claim_ref for item in self.proposed_claims),
            provider_ref=provider_ref,
            model_ref=model_ref,
            input_digest=input_digest,
            attempt_number=attempt_number,
            raw_provider_response_ref=raw_provider_response_ref,
            raw_provider_response_digest=raw_provider_response_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ClaimSettlement:
    settlement_ref: str
    authority_namespace: ClaimAuthorityNamespace
    authority_namespace_ref: str
    checkpoint: ClaimSettlementCheckpoint
    checkpoint_ref: str
    execution_result_ref: str
    execution_result_digest: str
    plan_revision_id: str
    claim_graph_ref: str
    claim_graph_digest: str
    claim_verifier_report_ref: str
    accepted_claim_keys: tuple[ClaimKey, ...]
    accepted_claims: tuple[ClaimRevision, ...]
    accepted_support_edges: tuple[SupportEdge, ...]
    obligation_coverage: tuple[ObligationCoverage, ...]
    verifier_report: ClaimVerifierReport
    claim_graph: ClaimGraph
    content_digest: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClaimSettlement":
        payload = _strict_shape(payload, cls, "claim_settlement_shape_invalid")
        raw_checkpoint = payload["checkpoint"]
        if not isinstance(raw_checkpoint, Mapping):
            raise ClaimSettlementContractError("claim_settlement_checkpoint_invalid")
        checkpoint = ClaimSettlementCheckpoint.from_dict(raw_checkpoint)
        namespace = checkpoint.authority_namespace
        keys, edges = _claim_keys_and_edges_from_payload(
            namespace,
            payload["accepted_claim_keys"],
            payload["accepted_support_edges"],
        )
        claims = _claims_from_payload(
            namespace,
            payload["accepted_claims"],
            keys,
            edges,
        )
        raw_coverage = payload["obligation_coverage"]
        raw_report = payload["verifier_report"]
        raw_graph = payload["claim_graph"]
        if (
            isinstance(raw_coverage, (str, bytes))
            or not isinstance(raw_coverage, Sequence)
            or not isinstance(raw_report, Mapping)
            or not isinstance(raw_graph, Mapping)
        ):
            raise ClaimSettlementContractError("claim_settlement_children_invalid")
        report = ClaimVerifierReport.from_dict(
            raw_report, authority_namespace=namespace
        )
        coverage = tuple(
            ObligationCoverage.from_dict(
                item,
                authority_namespace=namespace,
                verifier_report=report,
            )
            for item in raw_coverage
        )
        graph = ClaimGraph.from_dict(
            raw_graph,
            authority_namespace=namespace,
            claim_keys=keys,
            claims=claims,
            support_edges=edges,
            verifier_report=report,
        )
        rebuilt = _create_settlement(
            checkpoint=checkpoint,
            accepted_claim_keys=keys,
            accepted_claims=claims,
            accepted_support_edges=edges,
            obligation_coverage=coverage,
            verifier_report=report,
            claim_graph=graph,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimSettlementContractError("claim_settlement_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def validate_typed_claim_settlement(value: ClaimSettlement) -> ClaimSettlement:
    if type(value) is not ClaimSettlement:
        raise ClaimSettlementContractError("claim_settlement_invalid")
    namespace = value.authority_namespace
    if type(namespace) is not ClaimAuthorityNamespace:
        raise ClaimSettlementContractError("claim_settlement_namespace_invalid")
    namespace_body = {
        "run_attempt_id": namespace.run_attempt_id,
        "intent_revision_id": namespace.intent_revision_id,
        "plan_revision_id": namespace.plan_revision_id,
    }
    namespace_digest = canonical_digest(namespace_body)
    if (
        namespace.content_digest != namespace_digest
        or namespace.authority_namespace_ref
        != "claim-authority-namespace:sha256:" + namespace_digest
    ):
        raise ClaimSettlementContractError("claim_settlement_namespace_invalid")
    if (
        type(value.checkpoint) is not ClaimSettlementCheckpoint
        or type(value.verifier_report) is not ClaimVerifierReport
        or type(value.claim_graph) is not ClaimGraph
    ):
        raise ClaimSettlementContractError("claim_settlement_children_invalid")
    body = {
        "checkpoint_ref": value.checkpoint.checkpoint_ref,
        "checkpoint_digest": value.checkpoint.content_digest,
        "execution_result_ref": value.checkpoint.execution_result_ref,
        "execution_result_digest": value.checkpoint.execution_result_digest,
        "plan_revision_id": value.checkpoint.plan_revision_id,
        "accepted_claim_keys": value.accepted_claim_keys,
        "accepted_claims": value.accepted_claims,
        "accepted_support_edges": value.accepted_support_edges,
        "obligation_coverage": value.obligation_coverage,
        "verifier_report": value.verifier_report,
        "claim_graph": value.claim_graph,
    }
    digest = canonical_digest(body)
    if (
        value.authority_namespace_ref != namespace.authority_namespace_ref
        or value.checkpoint.authority_namespace_ref != namespace.authority_namespace_ref
        or value.checkpoint_ref != value.checkpoint.checkpoint_ref
        or value.execution_result_ref != value.checkpoint.execution_result_ref
        or value.execution_result_digest != value.checkpoint.execution_result_digest
        or value.plan_revision_id != value.checkpoint.plan_revision_id
        or value.claim_graph_ref != value.claim_graph.claim_graph_ref
        or value.claim_graph_digest != value.claim_graph.content_digest
        or value.claim_verifier_report_ref != value.verifier_report.verifier_report_ref
        or value.claim_graph.claim_verifier_report_ref
        != value.verifier_report.verifier_report_ref
        or value.content_digest != digest
        or value.settlement_ref
        != _record_ref("claim-settlement", namespace.authority_namespace_ref, digest)
    ):
        raise ClaimSettlementContractError("claim_settlement_integrity_invalid")
    return value


@dataclass(frozen=True)
class AuthorityBundleInputs:
    authority_inputs_ref: str
    authority_namespace: ClaimAuthorityNamespace
    authority_namespace_ref: str
    execution_result: AuthoritativeExecutionResult
    execution_result_ref: str
    execution_result_digest: str
    claim_settlement: ClaimSettlement
    claim_settlement_ref: str
    claim_settlement_digest: str
    claim_graph: ClaimGraph
    authority_mode: str
    obligation_coverage_refs: tuple[str, ...]
    claims: tuple[ClaimRevision, ...]
    recommendations: tuple[RecommendationRecord, ...]
    verifier_report: ClaimVerifierReport
    run_attempt_id: str
    intent_revision_id: str
    decision_refs: tuple[str, ...]
    plan_revision_id: str
    authority_context_ref: str
    assumption_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        execution_result: AuthoritativeExecutionResult,
        claim_settlement: ClaimSettlement,
        recommendations: Sequence[RecommendationRecord],
    ) -> "AuthorityBundleInputs":
        execution = _validated_execution_result(execution_result)
        if type(claim_settlement) is not ClaimSettlement:
            raise ClaimSettlementContractError(
                "authority_bundle_inputs_settlement_invalid"
            )
        settlement = claim_settlement
        namespace = settlement.authority_namespace
        if (
            namespace.run_attempt_id != execution.run_attempt_id
            or settlement.execution_result_ref
            != execution.authoritative_execution_result_ref
            or settlement.execution_result_digest != execution.content_digest
            or settlement.plan_revision_id != execution.plan_revision_id
        ):
            raise ClaimSettlementContractError(
                "authority_bundle_inputs_execution_closure_invalid"
            )
        _reject_shared_authority_failure(execution)
        _validate_required_obligation_publication_closure(
            execution=execution,
            settlement=settlement,
        )
        normalized_recommendations = _validated_recommendations(
            namespace,
            recommendations,
            claim_settlement=settlement,
        )
        claim_ref_set = {item.claim_ref for item in settlement.accepted_claims}
        for recommendation in normalized_recommendations:
            if not set(recommendation.supporting_claim_refs).issubset(claim_ref_set):
                raise ClaimSettlementContractError(
                    "authority_bundle_inputs_recommendation_claim_closure_invalid"
                )
            if recommendation.claim_verifier_report_ref != (
                settlement.verifier_report.verifier_report_ref
            ):
                raise ClaimSettlementContractError(
                    "authority_bundle_inputs_recommendation_report_closure_invalid"
                )
        assumption_refs = tuple(
            sorted(
                set(settlement.claim_graph.assumption_refs)
                | {
                    ref
                    for recommendation in normalized_recommendations
                    for ref in recommendation.assumption_refs
                }
            )
        )
        limitation_refs = _public_limitation_refs(
            execution=execution,
            settlement=settlement,
            recommendations=normalized_recommendations,
        )
        body = {
            "execution_result_ref": execution.authoritative_execution_result_ref,
            "execution_result_digest": execution.content_digest,
            "claim_settlement_ref": settlement.settlement_ref,
            "claim_settlement_digest": settlement.content_digest,
            "claim_graph_ref": settlement.claim_graph.claim_graph_ref,
            "claim_graph_digest": settlement.claim_graph.content_digest,
            "authority_mode": settlement.claim_graph.authority_mode,
            "obligation_coverage_refs": tuple(
                item.coverage_ref for item in settlement.claim_graph.obligation_coverage
            ),
            "claim_refs": tuple(item.claim_ref for item in settlement.accepted_claims),
            "recommendation_refs": tuple(
                item.recommendation_ref for item in normalized_recommendations
            ),
            "verifier_report_ref": settlement.verifier_report.verifier_report_ref,
            "run_attempt_id": execution.run_attempt_id,
            "intent_revision_id": execution.intent_revision_id,
            "decision_refs": tuple(sorted(execution.plan_revision.decision_refs)),
            "plan_revision_id": execution.plan_revision_id,
            "authority_context_ref": execution.authority_context_ref,
            "assumption_refs": assumption_refs,
            "limitation_refs": limitation_refs,
        }
        digest = canonical_digest(body)
        return cls(
            authority_inputs_ref=_record_ref(
                "authority-bundle-inputs", namespace.authority_namespace_ref, digest
            ),
            authority_namespace=namespace,
            authority_namespace_ref=namespace.authority_namespace_ref,
            execution_result=execution,
            execution_result_ref=execution.authoritative_execution_result_ref,
            execution_result_digest=execution.content_digest,
            claim_settlement=settlement,
            claim_settlement_ref=settlement.settlement_ref,
            claim_settlement_digest=settlement.content_digest,
            claim_graph=settlement.claim_graph,
            authority_mode=settlement.claim_graph.authority_mode,
            obligation_coverage_refs=tuple(
                item.coverage_ref for item in settlement.claim_graph.obligation_coverage
            ),
            claims=settlement.accepted_claims,
            recommendations=normalized_recommendations,
            verifier_report=settlement.verifier_report,
            run_attempt_id=execution.run_attempt_id,
            intent_revision_id=execution.intent_revision_id,
            decision_refs=tuple(sorted(execution.plan_revision.decision_refs)),
            plan_revision_id=execution.plan_revision_id,
            authority_context_ref=execution.authority_context_ref,
            assumption_refs=assumption_refs,
            limitation_refs=limitation_refs,
            content_digest=digest,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuthorityBundleInputs":
        payload = _strict_shape(payload, cls, "authority_bundle_inputs_shape_invalid")
        raw_execution = payload["execution_result"]
        raw_settlement = payload["claim_settlement"]
        if not isinstance(raw_execution, Mapping) or not isinstance(
            raw_settlement, Mapping
        ):
            raise ClaimSettlementContractError(
                "authority_bundle_inputs_children_invalid"
            )
        execution = AuthoritativeExecutionResult.from_dict(raw_execution)
        settlement = ClaimSettlement.from_dict(raw_settlement)
        recommendations = _recommendations_from_payload(
            settlement.authority_namespace,
            payload["recommendations"],
            claim_settlement=settlement,
        )
        rebuilt = cls.create(
            execution_result=execution,
            claim_settlement=settlement,
            recommendations=recommendations,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimSettlementContractError(
                "authority_bundle_inputs_integrity_invalid"
            )
        return rebuilt

    def replay(self) -> "AuthorityBundleInputs":
        return self.from_dict(self.to_dict())

    def material_projection_evidence_entries(
        self,
    ) -> tuple[EvidenceLedgerEntry, ...]:
        evidence_entries = tuple(
            entry
            for _, _, entries, _ in self.execution_result.capability_outcome_bundles
            for entry in entries
        )
        evidence_by_ref = {entry.entry_ref: entry for entry in evidence_entries}
        required_refs = {
            edge.source_ref
            for edge in self.claim_settlement.accepted_support_edges
            if edge.source_type == "evidence"
        }
        if len(evidence_by_ref) != len(evidence_entries) or not required_refs.issubset(
            evidence_by_ref
        ):
            raise ClaimSettlementContractError(
                "authority_bundle_inputs_projection_evidence_incomplete"
            )
        return tuple(evidence_by_ref[ref] for ref in sorted(required_refs))

    def seal(
        self,
        *,
        bundle_revision: int,
        supersedes_bundle_ref: str | None,
        sealed_at: str | datetime,
    ) -> AuthorityBundle:
        return AuthorityBundle.seal(
            authority_inputs=self,
            bundle_revision=bundle_revision,
            supersedes_bundle_ref=supersedes_bundle_ref,
            sealed_at=sealed_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority_inputs_ref": self.authority_inputs_ref,
            "authority_namespace": self.authority_namespace.to_dict(),
            "authority_namespace_ref": self.authority_namespace_ref,
            "execution_result": self.execution_result.to_dict(),
            "execution_result_ref": self.execution_result_ref,
            "execution_result_digest": self.execution_result_digest,
            "claim_settlement": self.claim_settlement.to_dict(),
            "claim_settlement_ref": self.claim_settlement_ref,
            "claim_settlement_digest": self.claim_settlement_digest,
            "claim_graph": self.claim_graph.to_dict(),
            "authority_mode": self.authority_mode,
            "obligation_coverage_refs": list(self.obligation_coverage_refs),
            "claims": [item.to_dict() for item in self.claims],
            "recommendations": [item.to_dict() for item in self.recommendations],
            "verifier_report": self.verifier_report.to_dict(),
            "run_attempt_id": self.run_attempt_id,
            "intent_revision_id": self.intent_revision_id,
            "decision_refs": list(self.decision_refs),
            "plan_revision_id": self.plan_revision_id,
            "authority_context_ref": self.authority_context_ref,
            "assumption_refs": list(self.assumption_refs),
            "limitation_refs": list(self.limitation_refs),
            "content_digest": self.content_digest,
        }


def _public_limitation_refs(
    *,
    execution: AuthoritativeExecutionResult,
    settlement: ClaimSettlement,
    recommendations: Sequence[RecommendationRecord],
) -> tuple[str, ...]:
    """Project boundaries attached to published claims and required obligations."""
    required_obligation_ids = {
        obligation.obligation_id
        for obligation in execution.plan_revision.claim_obligations
        if obligation.role == "user_required"
    }
    return tuple(
        sorted(
            {
                *(
                    ref
                    for claim in settlement.accepted_claims
                    for ref in claim.limitation_refs
                ),
                *(
                    ref
                    for edge in settlement.accepted_support_edges
                    for ref in edge.limitation_refs
                ),
                *(
                    ref
                    for coverage in settlement.obligation_coverage
                    if coverage.obligation_id in required_obligation_ids
                    for ref in coverage.limitation_refs
                ),
                *(
                    ref
                    for recommendation in recommendations
                    for ref in recommendation.risk_refs
                ),
            }
        )
    )


@dataclass(frozen=True)
class _ProposedClaim:
    obligation_id: str
    claim_key: ClaimKey
    claim: ClaimRevision
    support_edges: tuple[SupportEdge, ...]


@dataclass(frozen=True)
class _EvidenceRecord:
    entry: EvidenceLedgerEntry
    outcome: CapabilityOutcome
    task_obligation_ids: tuple[str, ...]
    required_obligation_ids: tuple[str, ...]
    claim_support_obligation_ids: tuple[str, ...]


def prepare_claim_settlement(
    execution_result: AuthoritativeExecutionResult,
    *,
    authority_namespace: ClaimAuthorityNamespace,
    candidate_proposals: Sequence[CandidateClaimProposal] = (),
) -> ClaimSettlementCheckpoint:
    execution = _validated_execution_result(execution_result)
    namespace = _validated_namespace(authority_namespace)
    if namespace.run_attempt_id != execution.run_attempt_id:
        raise ClaimSettlementContractError("claim_settlement_namespace_run_mismatch")
    _reject_shared_authority_failure(execution)
    obligations = {
        item.obligation_id: item for item in execution.plan_revision.claim_obligations
    }
    evidence_by_ref, outcomes_by_obligation = _evidence_records(
        execution,
        obligations=obligations,
    )
    proposals = _validated_candidate_proposals(namespace, candidate_proposals)
    direct = _direct_claim_proposals(
        execution,
        authority_namespace=namespace,
        obligations=obligations,
        evidence_by_ref=evidence_by_ref,
    )
    candidate = tuple(
        _candidate_claim_proposal(
            proposal,
            authority_namespace=namespace,
            obligations=obligations,
            evidence_by_ref=evidence_by_ref,
            resolved_window_refs=execution.plan_revision.resolved_window_refs,
            temporal_authority=execution.plan_revision.temporal_authority,
        )
        for proposal in proposals
    )
    proposed = tuple(
        sorted((*direct, *candidate), key=lambda item: item.claim_key.claim_key)
    )
    claim_keys = tuple(item.claim_key for item in proposed)
    if len({item.claim_key for item in claim_keys}) != len(claim_keys):
        raise ClaimSettlementContractError(
            "claim_settlement_current_claim_key_duplicated"
        )
    proposed_edges = _unique_support_edges(
        edge for item in proposed for edge in item.support_edges
    )
    basis = _obligation_basis(
        authority_namespace=namespace,
        obligations=obligations,
        proposed=proposed,
        evidence_by_ref=evidence_by_ref,
        outcomes_by_obligation=outcomes_by_obligation,
    )
    if not proposed and any(not item.unavailable_limitation_refs for item in basis):
        raise ClaimSettlementContractError("claim_settlement_proposed_claims_empty")
    return ClaimSettlementCheckpoint.create(
        authority_namespace=namespace,
        execution_result_ref=execution.authoritative_execution_result_ref,
        execution_result_digest=execution.content_digest,
        plan_revision_id=execution.plan_revision_id,
        proposed_claim_keys=claim_keys,
        proposed_claims=tuple(item.claim for item in proposed),
        proposed_support_edges=proposed_edges,
        obligation_basis=basis,
    )


def settle_claim_checkpoint(
    checkpoint: ClaimSettlementCheckpoint,
    *,
    verification_attempt: SemanticVerificationAttempt | None,
    verification_decisions: Sequence[SemanticVerificationDecision],
) -> ClaimSettlement:
    if type(checkpoint) is not ClaimSettlementCheckpoint:
        raise ClaimSettlementContractError("claim_settlement_checkpoint_invalid")
    try:
        replayed_checkpoint = ClaimSettlementCheckpoint.from_dict(checkpoint.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ClaimSettlementContractError(
            "claim_settlement_checkpoint_invalid"
        ) from exc
    if replayed_checkpoint != checkpoint:
        raise ClaimSettlementContractError("claim_settlement_checkpoint_invalid")
    namespace = checkpoint.authority_namespace
    proposed_by_ref = {item.claim_ref: item for item in checkpoint.proposed_claims}
    key_by_ref = {item.claim_key: item for item in checkpoint.proposed_claim_keys}
    edge_by_ref = {
        item.support_edge_ref: item for item in checkpoint.proposed_support_edges
    }
    if not proposed_by_ref:
        if verification_attempt is not None or verification_decisions:
            raise ClaimSettlementContractError(
                "claim_settlement_boundary_verification_forbidden"
            )
        boundary_authority = LocalBoundaryAuthority.create(
            authority_namespace=namespace,
            checkpoint_ref=checkpoint.checkpoint_ref,
            checkpoint_digest=checkpoint.content_digest,
            obligation_ids=tuple(
                item.obligation_id for item in checkpoint.obligation_basis
            ),
            limitation_refs=tuple(
                sorted(
                    {
                        ref
                        for item in checkpoint.obligation_basis
                        for ref in item.unavailable_limitation_refs
                    }
                )
            ),
        )
        report = ClaimVerifierReport.create(
            authority_namespace=namespace,
            verification_attempt=None,
            local_boundary_authority=boundary_authority,
            verification_decisions=(),
            proposed_to_verified={},
            vetoes=(),
        )
        coverage = tuple(
            ObligationCoverage.create(
                authority_namespace=namespace,
                verifier_report=report,
                obligation_id=item.obligation_id,
                status="unavailable",
                claim_refs=(),
                limitation_refs=item.unavailable_limitation_refs,
            )
            for item in checkpoint.obligation_basis
        )
        limitations = tuple(
            sorted({ref for item in coverage for ref in item.limitation_refs})
        )
        graph = ClaimGraph.create(
            authority_namespace=namespace,
            authority_mode="boundary_only",
            claim_keys=(),
            claims=(),
            support_edges=(),
            obligation_coverage=coverage,
            verifier_report=report,
            evidence_ceiling_by_ref={},
            assumption_refs=(),
            limitation_refs=limitations,
        )
        return _create_settlement(
            checkpoint=checkpoint,
            accepted_claim_keys=(),
            accepted_claims=(),
            accepted_support_edges=(),
            obligation_coverage=coverage,
            verifier_report=report,
            claim_graph=graph,
        )

    attempt = _validated_verification_attempt(
        namespace,
        checkpoint,
        verification_attempt,
    )
    decisions = _validated_verification_decisions(
        namespace,
        attempt,
        verification_decisions,
    )
    if {item.subject_ref for item in decisions} != set(proposed_by_ref):
        raise ClaimSettlementContractError(
            "claim_settlement_verification_decision_coverage_invalid"
        )
    accepted_pairs: list[tuple[ClaimRevision, ClaimRevision]] = []
    vetoes: list[ClaimVeto] = []
    for decision in decisions:
        proposed = proposed_by_ref[decision.subject_ref]
        if decision.disposition == "vetoed":
            vetoes.append(
                ClaimVeto.create(
                    authority_namespace=namespace,
                    claim_ref=proposed.claim_ref,
                    reason_code=str(decision.reason_code),
                    limitation_refs=decision.limitation_refs,
                )
            )
            continue
        claim_key = key_by_ref[proposed.claim_key]
        support_edges = tuple(edge_by_ref[ref] for ref in proposed.support_edge_refs)
        verified = ClaimRevision.create(
            authority_namespace=namespace,
            claim_key=claim_key,
            factual_payload=proposed.factual_payload,
            claim_class=proposed.claim_class,
            support_edges=support_edges,
            dependency_claim_refs=proposed.dependency_claim_refs,
            limitation_refs=proposed.limitation_refs,
            status="verified",
            publication_ceiling=proposed.publication_ceiling,
        )
        accepted_pairs.append((proposed, verified))
    proposed_to_verified = {
        proposed.claim_ref: verified.claim_ref for proposed, verified in accepted_pairs
    }
    report = ClaimVerifierReport.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        local_boundary_authority=None,
        verification_decisions=decisions,
        proposed_to_verified=proposed_to_verified,
        vetoes=tuple(vetoes),
    )
    veto_by_claim_ref = {item.claim_ref: item for item in vetoes}
    coverage = _obligation_coverage(
        authority_namespace=namespace,
        verifier_report=report,
        basis=checkpoint.obligation_basis,
        proposed_claims_by_ref=proposed_by_ref,
        proposed_to_verified=proposed_to_verified,
        veto_by_claim_ref=veto_by_claim_ref,
    )
    if not accepted_pairs:
        limitations = tuple(
            sorted({ref for item in coverage for ref in item.limitation_refs})
        )
        graph = ClaimGraph.create(
            authority_namespace=namespace,
            authority_mode="boundary_only",
            claim_keys=(),
            claims=(),
            support_edges=(),
            obligation_coverage=coverage,
            verifier_report=report,
            evidence_ceiling_by_ref={},
            assumption_refs=(),
            limitation_refs=limitations,
        )
        return _create_settlement(
            checkpoint=checkpoint,
            accepted_claim_keys=(),
            accepted_claims=(),
            accepted_support_edges=(),
            obligation_coverage=coverage,
            verifier_report=report,
            claim_graph=graph,
        )
    accepted_claims = tuple(
        sorted((item[1] for item in accepted_pairs), key=lambda item: item.claim_ref)
    )
    accepted_proposed_refs = set(proposed_to_verified)
    accepted_keys = tuple(
        sorted(
            (
                key_by_ref[item.claim_key]
                for item in checkpoint.proposed_claims
                if item.claim_ref in accepted_proposed_refs
            ),
            key=lambda item: item.claim_key,
        )
    )
    accepted_edges = _unique_support_edges(
        edge_by_ref[edge_ref]
        for item in checkpoint.proposed_claims
        if item.claim_ref in accepted_proposed_refs
        for edge_ref in item.support_edge_refs
    )
    evidence_ceilings: dict[str, ClaimPublicationCeiling] = {}
    for edge in accepted_edges:
        if edge.source_type != "evidence":
            continue
        existing = evidence_ceilings.setdefault(
            edge.source_ref, edge.source_publication_ceiling
        )
        if existing != edge.source_publication_ceiling:
            raise ClaimSettlementContractError(
                "claim_settlement_evidence_epistemic_identity_conflict"
            )
    assumption_refs = tuple(
        sorted(
            {
                edge.source_ref
                for edge in accepted_edges
                if edge.source_type == "assumption"
            }
        )
    )
    limitations = tuple(
        sorted(
            {
                *(ref for item in accepted_claims for ref in item.limitation_refs),
                *(ref for edge in accepted_edges for ref in edge.limitation_refs),
                *(ref for item in coverage for ref in item.limitation_refs),
            }
        )
    )
    graph = ClaimGraph.create(
        authority_namespace=namespace,
        authority_mode="claim_bearing",
        claim_keys=accepted_keys,
        claims=accepted_claims,
        support_edges=accepted_edges,
        obligation_coverage=coverage,
        verifier_report=report,
        evidence_ceiling_by_ref=evidence_ceilings,
        assumption_refs=assumption_refs,
        limitation_refs=limitations,
    )
    return _create_settlement(
        checkpoint=checkpoint,
        accepted_claim_keys=accepted_keys,
        accepted_claims=accepted_claims,
        accepted_support_edges=accepted_edges,
        obligation_coverage=coverage,
        verifier_report=report,
        claim_graph=graph,
    )


def _create_settlement(
    *,
    checkpoint: ClaimSettlementCheckpoint,
    accepted_claim_keys: Sequence[ClaimKey],
    accepted_claims: Sequence[ClaimRevision],
    accepted_support_edges: Sequence[SupportEdge],
    obligation_coverage: Sequence[ObligationCoverage],
    verifier_report: ClaimVerifierReport,
    claim_graph: ClaimGraph,
) -> ClaimSettlement:
    namespace = checkpoint.authority_namespace
    keys, claims, edges = _replay_proposed_authority(
        namespace,
        accepted_claim_keys,
        accepted_claims,
        accepted_support_edges,
    )
    coverage = _replay_coverage(
        namespace,
        obligation_coverage,
        verifier_report=verifier_report,
    )
    try:
        report = ClaimVerifierReport.from_dict(
            verifier_report.to_dict(), authority_namespace=namespace
        )
        graph = ClaimGraph.from_dict(
            claim_graph.to_dict(),
            authority_namespace=namespace,
            claim_keys=keys,
            claims=claims,
            support_edges=edges,
            verifier_report=report,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ClaimSettlementContractError("claim_settlement_graph_invalid") from exc
    if report != verifier_report or graph != claim_graph:
        raise ClaimSettlementContractError("claim_settlement_graph_invalid")
    if coverage != graph.obligation_coverage:
        raise ClaimSettlementContractError("claim_settlement_coverage_closure_invalid")
    body = {
        "checkpoint_ref": checkpoint.checkpoint_ref,
        "checkpoint_digest": checkpoint.content_digest,
        "execution_result_ref": checkpoint.execution_result_ref,
        "execution_result_digest": checkpoint.execution_result_digest,
        "plan_revision_id": checkpoint.plan_revision_id,
        "accepted_claim_keys": keys,
        "accepted_claims": claims,
        "accepted_support_edges": edges,
        "obligation_coverage": coverage,
        "verifier_report": report,
        "claim_graph": graph,
    }
    digest = canonical_digest(body)
    return ClaimSettlement(
        settlement_ref=_record_ref(
            "claim-settlement", namespace.authority_namespace_ref, digest
        ),
        authority_namespace=namespace,
        authority_namespace_ref=namespace.authority_namespace_ref,
        checkpoint=checkpoint,
        checkpoint_ref=checkpoint.checkpoint_ref,
        execution_result_ref=checkpoint.execution_result_ref,
        execution_result_digest=checkpoint.execution_result_digest,
        plan_revision_id=checkpoint.plan_revision_id,
        claim_graph_ref=graph.claim_graph_ref,
        claim_graph_digest=graph.content_digest,
        claim_verifier_report_ref=report.verifier_report_ref,
        accepted_claim_keys=keys,
        accepted_claims=claims,
        accepted_support_edges=edges,
        obligation_coverage=coverage,
        verifier_report=report,
        claim_graph=graph,
        content_digest=digest,
    )


def _reject_shared_authority_failure(execution: AuthoritativeExecutionResult) -> None:
    shared_failure_refs = tuple(
        sorted(
            failure.failure_ref
            for _, _, _, failures in execution.capability_outcome_bundles
            for failure in failures
            if failure.integrity_level == "shared_authority"
        )
    )
    if (
        shared_failure_refs
        or execution.exploration_stop_record.reason == "shared_authority_failure"
    ):
        suffix = ",".join(shared_failure_refs)
        raise ClaimSettlementContractError(
            "claim_settlement_shared_authority_failure:" + suffix
        )


def _validate_required_obligation_publication_closure(
    *,
    execution: AuthoritativeExecutionResult,
    settlement: ClaimSettlement,
) -> None:
    coverage_by_obligation_id = {
        item.obligation_id: item for item in settlement.obligation_coverage
    }
    accepted_claim_refs = {item.claim_ref for item in settlement.accepted_claims}
    for obligation in execution.plan_revision.claim_obligations:
        if obligation.role != "user_required":
            continue
        coverage = coverage_by_obligation_id.get(obligation.obligation_id)
        if coverage is None:
            raise ClaimSettlementContractError(
                "authority_bundle_inputs_required_obligation_publication_closure_invalid"
            )
        claim_refs = set(coverage.claim_refs)
        limitation_refs = set(coverage.limitation_refs)
        if coverage.status in {"satisfied", "mixed", "contradicted"}:
            valid = bool(claim_refs) and claim_refs.issubset(accepted_claim_refs)
            if coverage.status == "mixed":
                valid = valid and bool(limitation_refs)
        elif coverage.status == "unavailable":
            valid = not claim_refs and bool(limitation_refs)
        else:
            valid = False
        if not valid:
            raise ClaimSettlementContractError(
                "authority_bundle_inputs_required_obligation_publication_closure_invalid"
            )


def _validated_candidate_proposals(
    namespace: ClaimAuthorityNamespace,
    value: Sequence[CandidateClaimProposal],
) -> tuple[CandidateClaimProposal, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ClaimSettlementContractError(
            "claim_settlement_candidate_proposals_invalid"
        )
    records: list[CandidateClaimProposal] = []
    for item in value:
        if type(item) is not CandidateClaimProposal:
            raise ClaimSettlementContractError(
                "claim_settlement_candidate_proposals_invalid"
            )
        try:
            replayed = CandidateClaimProposal.from_dict(
                item.to_dict(), authority_namespace=namespace
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimSettlementContractError(
                "claim_settlement_candidate_proposals_invalid"
            ) from exc
        if replayed != item:
            raise ClaimSettlementContractError(
                "claim_settlement_candidate_proposals_invalid"
            )
        records.append(replayed)
    normalized = tuple(sorted(records, key=lambda item: item.candidate_proposal_ref))
    if len({item.candidate_proposal_ref for item in normalized}) != len(normalized):
        raise ClaimSettlementContractError(
            "claim_settlement_candidate_proposals_invalid"
        )
    return normalized


def _validated_verification_attempt(
    namespace: ClaimAuthorityNamespace,
    checkpoint: ClaimSettlementCheckpoint,
    value: SemanticVerificationAttempt | None,
) -> SemanticVerificationAttempt:
    if type(value) is not SemanticVerificationAttempt:
        raise ClaimSettlementContractError(
            "claim_settlement_verification_attempt_required"
        )
    try:
        replayed = SemanticVerificationAttempt.from_dict(
            value.to_dict(), authority_namespace=namespace
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ClaimSettlementContractError(
            "claim_settlement_verification_attempt_invalid"
        ) from exc
    if (
        replayed != value
        or replayed.purpose != "claim_settlement"
        or replayed.authority_input_ref != checkpoint.checkpoint_ref
        or replayed.authority_input_digest != checkpoint.content_digest
        or set(replayed.subject_refs)
        != {item.claim_ref for item in checkpoint.proposed_claims}
    ):
        raise ClaimSettlementContractError(
            "claim_settlement_verification_attempt_invalid"
        )
    return replayed


def _validated_verification_decisions(
    namespace: ClaimAuthorityNamespace,
    attempt: SemanticVerificationAttempt,
    value: Sequence[SemanticVerificationDecision],
) -> tuple[SemanticVerificationDecision, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ClaimSettlementContractError(
            "claim_settlement_verification_decisions_invalid"
        )
    records: list[SemanticVerificationDecision] = []
    for item in value:
        if type(item) is not SemanticVerificationDecision:
            raise ClaimSettlementContractError(
                "claim_settlement_verification_decisions_invalid"
            )
        try:
            replayed = SemanticVerificationDecision.from_dict(
                item.to_dict(),
                authority_namespace=namespace,
                verification_attempt=attempt,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimSettlementContractError(
                "claim_settlement_verification_decisions_invalid"
            ) from exc
        if replayed != item:
            raise ClaimSettlementContractError(
                "claim_settlement_verification_decisions_invalid"
            )
        records.append(replayed)
    normalized = tuple(sorted(records, key=lambda item: item.subject_ref))
    if len({item.subject_ref for item in normalized}) != len(normalized):
        raise ClaimSettlementContractError(
            "claim_settlement_verification_decisions_invalid"
        )
    return normalized


def _evidence_records(
    execution: AuthoritativeExecutionResult,
    *,
    obligations: Mapping[str, ClaimObligation],
) -> tuple[
    dict[str, _EvidenceRecord],
    dict[str, tuple[tuple[CapabilityOutcome, bool], ...]],
]:
    task_by_id = {
        item.task_id: item for item in execution.plan_revision.capability_tasks
    }
    evidence_by_ref: dict[str, _EvidenceRecord] = {}
    outcomes_by_obligation: dict[str, list[tuple[CapabilityOutcome, bool]]] = {
        ref: [] for ref in obligations
    }
    for _, outcome, entries, _ in execution.capability_outcome_bundles:
        task = task_by_id[outcome.task_id]
        required_ids = tuple(
            sorted(
                str(edge["obligation_id"])
                for edge in task.obligation_edges
                if bool(edge["required"])
            )
        )
        for obligation_id in task.supports_obligation_ids:
            outcomes_by_obligation[obligation_id].append(
                (outcome, obligation_id in required_ids)
            )
        for entry in entries:
            _validate_evidence_authority(entry, outcome=outcome, execution=execution)
            relevant = tuple(
                obligations[ref]
                for ref in task.supports_obligation_ids
                if obligations[ref].claim_kind in entry.supported_claim_kinds
            )
            if not task.supports_obligation_ids:
                if entry.supported_claim_kinds:
                    raise ClaimSettlementContractError(
                        "claim_settlement_unbound_evidence_claim_support_invalid"
                    )
            elif not relevant:
                raise ClaimSettlementContractError(
                    "claim_settlement_evidence_obligation_membership_missing"
                )
            claim_support_obligation_ids: list[str] = []
            for obligation in relevant:
                compatibility = _compatibility(
                    evidence_kind=entry.evidence_kind,
                    source_claim_kind=obligation.claim_kind,
                    maximum_claim_strength=entry.maximum_claim_strength,
                )
                if _data_contract_allows_claim_support(entry, compatibility):
                    claim_support_obligation_ids.append(obligation.obligation_id)
                if obligation.obligation_id not in outcome.affected_obligation_ids:
                    raise ClaimSettlementContractError(
                        "claim_settlement_outcome_obligation_membership_missing"
                    )
            if entry.entry_ref in evidence_by_ref:
                raise ClaimSettlementContractError(
                    "claim_settlement_evidence_entry_duplicated"
                )
            evidence_by_ref[entry.entry_ref] = _EvidenceRecord(
                entry=entry,
                outcome=outcome,
                task_obligation_ids=task.supports_obligation_ids,
                required_obligation_ids=required_ids,
                claim_support_obligation_ids=tuple(
                    sorted(claim_support_obligation_ids)
                ),
            )
    return evidence_by_ref, {
        ref: tuple(sorted(items, key=lambda item: item[0].outcome_ref))
        for ref, items in outcomes_by_obligation.items()
    }


def _validate_evidence_authority(
    entry: EvidenceLedgerEntry,
    *,
    outcome: CapabilityOutcome,
    execution: AuthoritativeExecutionResult,
) -> None:
    if outcome.status != "succeeded" or entry.execution_state != "available":
        raise ClaimSettlementContractError(
            "claim_settlement_evidence_execution_state_invalid"
        )
    if entry.binding_record_ref is None:
        raise ClaimSettlementContractError("claim_settlement_binding_ref_missing")
    if not entry.result_refs:
        raise ClaimSettlementContractError("claim_settlement_result_membership_missing")
    if not entry.completeness_report_refs:
        raise ClaimSettlementContractError(
            "claim_settlement_completeness_membership_missing"
        )
    if tuple(sorted(entry.window_refs)) != tuple(
        sorted(execution.plan_revision.resolved_window_refs)
    ):
        raise ClaimSettlementContractError(
            "claim_settlement_window_reference_closure_invalid"
        )
    if entry.hierarchy_qualified != bool(entry.dimension_path):
        raise ClaimSettlementContractError(
            "claim_settlement_dimension_membership_invalid"
        )
    if not entry.observation_facts:
        raise ClaimSettlementContractError("claim_settlement_observation_facts_missing")


def _data_contract_allows_claim_support(
    entry: EvidenceLedgerEntry,
    compatibility: _EvidenceCompatibility,
) -> bool:
    if entry.data_contract_state == "complete":
        return True
    if entry.data_contract_state == "partial":
        if compatibility.source_claim_class == "boundary":
            return True
        if not entry.limitation_refs:
            raise ClaimSettlementContractError(
                "claim_settlement_partial_evidence_limitation_missing:"
                f"{entry.entry_ref}"
            )
        return False
    raise ClaimSettlementContractError(
        f"claim_settlement_data_contract_state_unknown:{entry.data_contract_state}"
    )


def _direct_claim_proposals(
    execution: AuthoritativeExecutionResult,
    *,
    authority_namespace: ClaimAuthorityNamespace,
    obligations: Mapping[str, ClaimObligation],
    evidence_by_ref: Mapping[str, _EvidenceRecord],
) -> tuple[_ProposedClaim, ...]:
    grouped: dict[
        tuple[str, str, tuple[str, ...], tuple[str, ...], str],
        list[tuple[_EvidenceRecord, _EvidenceCompatibility]],
    ] = {}
    for record in evidence_by_ref.values():
        entry = record.entry
        for obligation_id in record.task_obligation_ids:
            obligation = obligations[obligation_id]
            if (
                obligation.claim_kind in _CANDIDATE_CLAIM_KINDS
                or obligation.claim_kind not in entry.supported_claim_kinds
                or obligation_id not in record.claim_support_obligation_ids
            ):
                continue
            compatibility = _compatibility(
                evidence_kind=entry.evidence_kind,
                source_claim_kind=obligation.claim_kind,
                maximum_claim_strength=entry.maximum_claim_strength,
            )
            if compatibility.evidence_contract_type not in set(
                obligation.evidence_requirement.evidence_kinds
            ):
                continue
            key = (
                obligation_id,
                entry.scope,
                entry.window_refs,
                entry.dimension_path,
                compatibility.source_claim_class,
            )
            grouped.setdefault(key, []).append((record, compatibility))
    proposed = []
    for key, records in sorted(grouped.items()):
        obligation = obligations[key[0]]
        claim_key = _claim_key(
            obligation,
            authority_namespace=authority_namespace,
            subject_identity={
                "obligation_subject": obligation.subject,
                "epistemic_class": key[4],
            },
            scope=key[1],
            window_refs=execution.plan_revision.resolved_window_refs,
            dimension_path=key[3],
        )
        source_classes = {item[1].source_claim_class for item in records}
        if source_classes != {key[4]}:
            raise ClaimSettlementContractError(
                "claim_settlement_evidence_epistemic_identity_conflict"
            )
        edges = tuple(
            SupportEdge.create(
                authority_namespace=authority_namespace,
                kind="supports",
                source_type="evidence",
                source_ref=record.entry.entry_ref,
                source_epistemic_class=compatibility.source_claim_class,
                source_publication_ceiling=compatibility.publication_ceiling,
                target_claim_key=claim_key.claim_key,
                limitation_refs=record.entry.limitation_refs,
            )
            for record, compatibility in sorted(
                records, key=lambda item: item[0].entry.entry_ref
            )
        )
        ceiling = _weakest_ceiling(
            tuple(item[1].publication_ceiling for item in records)
        )
        limitations = tuple(
            sorted(
                {ref for record, _ in records for ref in record.entry.limitation_refs}
            )
        )
        claim = ClaimRevision.create(
            authority_namespace=authority_namespace,
            claim_key=claim_key,
            factual_payload={
                "obligation_id": obligation.obligation_id,
                "claim_kind": obligation.claim_kind,
            },
            claim_class=ceiling.claim_class,
            support_edges=edges,
            dependency_claim_refs=(),
            limitation_refs=limitations,
            status="proposed",
            publication_ceiling=ceiling,
        )
        proposed.append(
            _ProposedClaim(
                obligation_id=obligation.obligation_id,
                claim_key=claim_key,
                claim=claim,
                support_edges=edges,
            )
        )
    return tuple(proposed)


def _candidate_claim_proposal(
    proposal: CandidateClaimProposal,
    *,
    authority_namespace: ClaimAuthorityNamespace,
    obligations: Mapping[str, ClaimObligation],
    evidence_by_ref: Mapping[str, _EvidenceRecord],
    resolved_window_refs: Sequence[str],
    temporal_authority: Any,
) -> _ProposedClaim:
    obligation = obligations.get(proposal.obligation_id)
    if obligation is None or obligation.claim_kind not in _CANDIDATE_CLAIM_KINDS:
        raise ClaimSettlementContractError(
            "candidate_claim_proposal_obligation_invalid"
        )
    if "evidence_observations" in proposal.factual_payload:
        raise ClaimSettlementContractError(
            "candidate_claim_proposal_embedded_evidence_forbidden"
        )
    bound_support: list[tuple[_EvidenceRecord, _EvidenceCompatibility, str]] = []
    qualifies_minimum = False
    for support in proposal.evidence_support:
        record = evidence_by_ref.get(support.evidence_entry_ref)
        if record is None or support.source_claim_kind not in set(
            record.entry.supported_claim_kinds
        ):
            raise ClaimSettlementContractError(
                "candidate_claim_proposal_evidence_membership_invalid"
            )
        compatibility = _compatibility(
            evidence_kind=record.entry.evidence_kind,
            source_claim_kind=support.source_claim_kind,
            maximum_claim_strength=record.entry.maximum_claim_strength,
        )
        if not _data_contract_allows_claim_support(record.entry, compatibility):
            raise ClaimSettlementContractError(
                "candidate_claim_proposal_data_contract_unpublishable:"
                f"{record.entry.entry_ref}"
            )
        if (
            compatibility.source_claim_class
            not in _SOURCE_CLASSES_ALLOWED_FOR_CANDIDATE
        ):
            raise ClaimSettlementContractError(
                "candidate_claim_proposal_support_class_invalid"
            )
        if (
            obligation.obligation_id in record.task_obligation_ids
            and compatibility.evidence_contract_type
            in set(obligation.evidence_requirement.evidence_kinds)
        ):
            qualifies_minimum = True
        bound_support.append((record, compatibility, support.source_claim_kind))
    if not qualifies_minimum:
        raise ClaimSettlementContractError(
            "candidate_claim_proposal_minimum_evidence_missing"
        )
    scopes = tuple(sorted({item[0].entry.scope for item in bound_support}))
    scope = (
        scopes[0]
        if len(scopes) == 1
        else "scope-set:sha256:" + canonical_digest(scopes)
    )
    claim_key = _claim_key(
        obligation,
        authority_namespace=authority_namespace,
        subject_identity={
            "obligation_subject": obligation.subject,
            "proposal_item_ref": proposal.proposal_item_ref,
            "candidate_subject": proposal.subject,
        },
        scope=scope,
        window_refs=resolved_window_refs,
        dimension_path=(),
    )
    evidence_edges = tuple(
        SupportEdge.create(
            authority_namespace=authority_namespace,
            kind="supports",
            source_type="evidence",
            source_ref=record.entry.entry_ref,
            source_epistemic_class=compatibility.source_claim_class,
            source_publication_ceiling=compatibility.publication_ceiling,
            target_claim_key=claim_key.claim_key,
            limitation_refs=record.entry.limitation_refs,
        )
        for record, compatibility, _source_claim_kind in sorted(
            bound_support, key=lambda item: item[0].entry.entry_ref
        )
    )
    assumption_ceiling = ClaimPublicationCeiling.create(
        claim_class="scenario", strength="scenario"
    )
    assumption_edges = tuple(
        SupportEdge.create(
            authority_namespace=authority_namespace,
            kind="contextualizes",
            source_type="assumption",
            source_ref=ref,
            source_epistemic_class="scenario",
            source_publication_ceiling=assumption_ceiling,
            target_claim_key=claim_key.claim_key,
            limitation_refs=(),
        )
        for ref in proposal.assumption_refs
    )
    edges = tuple((*evidence_edges, *assumption_edges))
    limitations = tuple(
        sorted(
            {
                *proposal.limitation_refs,
                *(
                    ref
                    for record, _, _ in bound_support
                    for ref in record.entry.limitation_refs
                ),
            }
        )
    )
    composite = _validated_candidate_composite_support(
        obligation,
        bound_support=tuple(bound_support),
        temporal_authority=temporal_authority,
    )
    if composite is None:
        claim_class = "candidate_mechanism"
        ceiling = ClaimPublicationCeiling.create(
            claim_class=claim_class,
            strength="candidate_mechanism",
        )
        factual_payload = {
            "candidate_subject": proposal.subject,
            **dict(proposal.factual_payload),
        }
    else:
        claim_class = str(composite["claim_class"])
        ceiling = ClaimPublicationCeiling.create(
            claim_class=claim_class,
            strength=str(composite["publication_strength"]),
        )
        if proposal.factual_payload.get("causal_interpretation_allowed") not in {
            None,
            False,
        }:
            raise ClaimSettlementContractError(
                "candidate_claim_composite_causal_interpretation_forbidden"
            )
        factual_payload = {
            "candidate_subject": proposal.subject,
            **dict(proposal.factual_payload),
            **dict(composite["identity"]),
            "causal_interpretation_allowed": False,
        }
    claim = ClaimRevision.create(
        authority_namespace=authority_namespace,
        claim_key=claim_key,
        factual_payload=factual_payload,
        claim_class=claim_class,
        support_edges=edges,
        dependency_claim_refs=(),
        limitation_refs=limitations,
        status="proposed",
        publication_ceiling=ceiling,
    )
    return _ProposedClaim(
        obligation_id=obligation.obligation_id,
        claim_key=claim_key,
        claim=claim,
        support_edges=edges,
    )


def _validated_candidate_composite_support(
    obligation: ClaimObligation,
    *,
    bound_support: tuple[tuple[_EvidenceRecord, _EvidenceCompatibility, str], ...],
    temporal_authority: Any,
) -> Mapping[str, Any] | None:
    policy = obligation.success_policy.get("composite_support_policy")
    if policy is None:
        return None
    expected_policy_fields = {
        "policy",
        "claim_class",
        "publication_strength",
        "causal_interpretation_allowed",
        "identity_fields",
        "required_supports",
    }
    if (
        not isinstance(policy, Mapping)
        or set(policy) != expected_policy_fields
        or policy.get("policy") != "all_required_supports_same_authority"
        or policy.get("claim_class") != "candidate_impact"
        or policy.get("publication_strength") != "candidate_driver"
        or policy.get("causal_interpretation_allowed") is not False
    ):
        raise ClaimSettlementContractError("candidate_claim_composite_policy_invalid")
    identity_fields = policy.get("identity_fields")
    requirements = policy.get("required_supports")
    if (
        isinstance(identity_fields, (str, bytes))
        or not isinstance(identity_fields, Sequence)
        or tuple(identity_fields) != ("event_ref", "temporal_authority_ref")
        or isinstance(requirements, (str, bytes))
        or not isinstance(requirements, Sequence)
        or not requirements
    ):
        raise ClaimSettlementContractError("candidate_claim_composite_policy_invalid")

    matched: list[tuple[_EvidenceRecord, Mapping[str, Any]]] = []
    for requirement in requirements:
        if not isinstance(requirement, Mapping) or set(requirement) != {
            "source_claim_kind",
            "evidence_kind",
            "maximum_claim_strength",
            "evidence_contract",
        }:
            raise ClaimSettlementContractError(
                "candidate_claim_composite_policy_invalid"
            )
        candidates = []
        for record, _compatibility, source_claim_kind in bound_support:
            fact = _composite_evidence_contract_fact(record.entry)
            if (
                source_claim_kind == requirement["source_claim_kind"]
                and record.entry.evidence_kind == requirement["evidence_kind"]
                and record.entry.maximum_claim_strength
                == requirement["maximum_claim_strength"]
                and fact.get("evidence_contract") == requirement["evidence_contract"]
                and fact.get("causal_interpretation_allowed") is False
            ):
                candidates.append((record, fact))
        if len(candidates) != 1:
            raise ClaimSettlementContractError(
                "candidate_claim_composite_support_missing_or_ambiguous"
            )
        matched.append(candidates[0])

    scopes = {record.entry.scope for record, _ in matched}
    windows = {tuple(sorted(record.entry.window_refs)) for record, _ in matched}
    identities = {
        tuple(fact.get(field) for field in identity_fields) for _, fact in matched
    }
    subject_scope = obligation.subject.get("scope")
    expected_scope = (
        "scope:sha256:" + canonical_digest(subject_scope)
        if isinstance(subject_scope, Mapping)
        else ""
    )
    expected_identity = (
        getattr(temporal_authority, "event_ref", None),
        getattr(temporal_authority, "authority_ref", None),
    )
    if (
        getattr(temporal_authority, "mode", None) != "event_relative"
        or len(scopes) != 1
        or scopes != {expected_scope}
        or len(windows) != 1
        or windows
        != {tuple(sorted(getattr(temporal_authority, "resolved_window_refs", ())))}
        or len(identities) != 1
        or any(not isinstance(item, str) or not item for item in next(iter(identities)))
        or next(iter(identities)) != expected_identity
    ):
        raise ClaimSettlementContractError(
            "candidate_claim_composite_authority_mismatch"
        )
    identity = dict(zip(identity_fields, next(iter(identities))))
    return {
        "claim_class": policy["claim_class"],
        "publication_strength": policy["publication_strength"],
        "identity": identity,
    }


def _composite_evidence_contract_fact(
    entry: EvidenceLedgerEntry,
) -> Mapping[str, Any]:
    facts = tuple(
        fact
        for fact in entry.observation_facts
        if isinstance(fact, Mapping) and "evidence_contract" in fact
    )
    if len(facts) != 1:
        raise ClaimSettlementContractError(
            "candidate_claim_composite_evidence_contract_missing"
        )
    return facts[0]


def _claim_key(
    obligation: ClaimObligation,
    *,
    authority_namespace: ClaimAuthorityNamespace,
    subject_identity: Mapping[str, Any],
    scope: str,
    window_refs: Sequence[str],
    dimension_path: Sequence[str],
) -> ClaimKey:
    subject = obligation.subject
    goal_refs = _string_tuple(
        subject["goal_refs"],
        "claim_settlement_subject_identity_invalid",
        allow_empty=False,
    )
    goal_id = (
        goal_refs[0]
        if len(goal_refs) == 1
        else "goal-set:sha256:" + canonical_digest(goal_refs)
    )
    metric_ref = _obligation_metric_ref(obligation)
    normalized_windows = _string_tuple(
        window_refs,
        "claim_settlement_window_identity_invalid",
        allow_empty=False,
        sort=False,
    )
    target_window_ref = normalized_windows[0]
    baseline_window_ref = None
    if baseline_window_ref is None and len(normalized_windows) == 2:
        baseline_window_ref = normalized_windows[1]
    elif baseline_window_ref is None and len(normalized_windows) > 2:
        baseline_window_ref = "baseline-window-set:sha256:" + canonical_digest(
            normalized_windows[1:]
        )
    return ClaimKey.create(
        authority_namespace=authority_namespace,
        goal_id=goal_id,
        claim_kind=obligation.claim_kind,
        subject="subject:sha256:" + canonical_digest(subject_identity),
        metric_ref=metric_ref,
        target_window_ref=target_window_ref,
        baseline_window_ref=baseline_window_ref,
        scope=scope,
        grain=(
            "aggregate"
            if not dimension_path
            else "dimension-path:sha256:" + canonical_digest(tuple(dimension_path))
        ),
        dimension_path=dimension_path,
    )


def _obligation_metric_ref(obligation: ClaimObligation) -> str:
    if obligation.role == "user_required":
        return _required_string(
            obligation.subject["target_metric_ref"],
            "claim_settlement_metric_identity_invalid",
        )
    metric_refs = _string_tuple(
        obligation.subject["target_metric_refs"],
        "claim_settlement_metric_identity_invalid",
        allow_empty=False,
    )
    if len(metric_refs) == 1:
        return metric_refs[0]
    return "metric-set:sha256:" + canonical_digest(metric_refs)


def _weakest_ceiling(
    ceilings: Sequence[ClaimPublicationCeiling],
) -> ClaimPublicationCeiling:
    if not ceilings:
        raise ClaimSettlementContractError("claim_settlement_ceiling_missing")
    claim_classes = {item.claim_class for item in ceilings}
    if len(claim_classes) != 1:
        raise ClaimSettlementContractError(
            "claim_settlement_evidence_epistemic_identity_conflict"
        )
    claim_class = next(iter(claim_classes))
    order = _CLASS_STRENGTH_ORDER[claim_class]
    return min(ceilings, key=lambda item: order.index(item.strength))


def _obligation_basis(
    *,
    authority_namespace: ClaimAuthorityNamespace,
    obligations: Mapping[str, ClaimObligation],
    proposed: Sequence[_ProposedClaim],
    evidence_by_ref: Mapping[str, _EvidenceRecord],
    outcomes_by_obligation: Mapping[str, Sequence[tuple[CapabilityOutcome, bool]]],
) -> tuple[ObligationSettlementBasis, ...]:
    proposed_by_obligation: dict[str, list[str]] = {ref: [] for ref in obligations}
    used_evidence_by_obligation: dict[str, set[str]] = {
        ref: set() for ref in obligations
    }
    for item in proposed:
        proposed_by_obligation[item.obligation_id].append(item.claim.claim_ref)
        used_evidence_by_obligation[item.obligation_id].update(
            edge.source_ref
            for edge in item.support_edges
            if edge.source_type == "evidence"
        )
    basis = []
    for obligation_id in sorted(obligations):
        unavailable_limitations: set[str] = set()
        non_claim_support_evidence_refs: set[str] = set()
        for outcome, required in outcomes_by_obligation[obligation_id]:
            if not required or outcome.status == "succeeded":
                continue
            unavailable_limitations.update(outcome.limitation_refs)
        obligation = obligations[obligation_id]
        for record in evidence_by_ref.values():
            if (
                obligation_id not in record.task_obligation_ids
                or obligation.claim_kind not in record.entry.supported_claim_kinds
                or record.entry.entry_ref in used_evidence_by_obligation[obligation_id]
            ):
                continue
            non_claim_support_evidence_refs.add(record.entry.entry_ref)
            unavailable_limitations.update(record.entry.limitation_refs)
        if not proposed_by_obligation[obligation_id] and not unavailable_limitations:
            boundary_kind = (
                "semantic-proposal-required"
                if (
                    obligation.claim_kind in _CANDIDATE_CLAIM_KINDS
                    and non_claim_support_evidence_refs
                )
                else "minimum-evidence-unsatisfied"
            )
            unavailable_limitations.add(
                _minimum_evidence_boundary_limitation(
                    boundary_kind=boundary_kind,
                    obligation=obligation,
                    proposed_claim_refs=proposed_by_obligation[obligation_id],
                    non_claim_support_evidence_refs=tuple(
                        non_claim_support_evidence_refs
                    ),
                )
            )
        basis.append(
            ObligationSettlementBasis.create(
                authority_namespace=authority_namespace,
                obligation_id=obligation_id,
                success_policy=obligation.success_policy,
                proposed_claim_refs=proposed_by_obligation[obligation_id],
                non_claim_support_evidence_refs=tuple(non_claim_support_evidence_refs),
                unavailable_limitation_refs=tuple(unavailable_limitations),
            )
        )
    return tuple(basis)


def _obligation_coverage(
    *,
    authority_namespace: ClaimAuthorityNamespace,
    verifier_report: ClaimVerifierReport,
    basis: Sequence[ObligationSettlementBasis],
    proposed_claims_by_ref: Mapping[str, ClaimRevision],
    proposed_to_verified: Mapping[str, str],
    veto_by_claim_ref: Mapping[str, ClaimVeto],
) -> tuple[ObligationCoverage, ...]:
    verification_decision_by_claim_ref = {
        item.subject_ref: item for item in verifier_report.verification_decisions
    }
    coverage = []
    for item in basis:
        accepted_pairs = tuple(
            (
                proposed_claims_by_ref[ref],
                proposed_to_verified[ref],
            )
            for ref in item.proposed_claim_refs
            if ref in proposed_to_verified
        )
        accepted_refs = tuple(
            sorted(verified_ref for _, verified_ref in accepted_pairs)
        )
        sufficient_refs = {
            verified_ref
            for proposed, verified_ref in accepted_pairs
            if publication_ceiling_satisfies(
                proposed.publication_ceiling,
                required_strength=item.required_claim_strength,
            )
        }
        strength_gap_limitations = {
            _claim_strength_gap_limitation(
                obligation_id=item.obligation_id,
                required_claim_strength=item.required_claim_strength,
                accepted_claim_ref=verified_ref,
                publication_ceiling=proposed.publication_ceiling,
            )
            for proposed, verified_ref in accepted_pairs
            if verified_ref not in sufficient_refs
        }
        veto_limitations = {
            limitation
            for ref in item.proposed_claim_refs
            if ref in veto_by_claim_ref
            for limitation in veto_by_claim_ref[ref].limitation_refs
        }
        known_boundary_limitations = (
            veto_limitations
            | set(item.unavailable_limitation_refs)
            | strength_gap_limitations
        )
        if not accepted_refs and not known_boundary_limitations:
            known_boundary_limitations.add(
                _semantic_verifier_boundary_limitation(
                    obligation_id=item.obligation_id,
                    proposed_claim_refs=item.proposed_claim_refs,
                    verification_decision_by_claim_ref=(
                        verification_decision_by_claim_ref
                    ),
                )
            )
        limitations = tuple(sorted(known_boundary_limitations))
        if sufficient_refs:
            status = "mixed" if limitations else "satisfied"
        elif accepted_refs:
            status = "mixed"
        elif limitations:
            status = "unavailable"
        else:
            raise ClaimSettlementContractError(
                "claim_settlement_explicit_boundary_missing"
            )
        coverage.append(
            ObligationCoverage.create(
                authority_namespace=authority_namespace,
                verifier_report=verifier_report,
                obligation_id=item.obligation_id,
                status=status,
                claim_refs=accepted_refs,
                limitation_refs=limitations,
            )
        )
    return tuple(coverage)


def _minimum_evidence_boundary_limitation(
    *,
    boundary_kind: str,
    obligation: ClaimObligation,
    proposed_claim_refs: Sequence[str],
    non_claim_support_evidence_refs: Sequence[str],
) -> str:
    body = {
        "obligation_id": obligation.obligation_id,
        "evidence_requirement": obligation.evidence_requirement.to_dict(),
        "proposed_claim_refs": tuple(sorted(proposed_claim_refs)),
        "non_claim_support_evidence_refs": tuple(
            sorted(non_claim_support_evidence_refs)
        ),
    }
    return f"limitation:{boundary_kind}:sha256:{canonical_digest(body)}"


def _semantic_verifier_boundary_limitation(
    *,
    obligation_id: str,
    proposed_claim_refs: Sequence[str],
    verification_decision_by_claim_ref: Mapping[str, SemanticVerificationDecision],
) -> str:
    proposed_refs = tuple(sorted(proposed_claim_refs))
    decisions = tuple(verification_decision_by_claim_ref[ref] for ref in proposed_refs)
    if not proposed_refs or any(item.disposition != "vetoed" for item in decisions):
        raise ClaimSettlementContractError(
            "claim_settlement_semantic_boundary_source_invalid"
        )
    body = {
        "obligation_id": obligation_id,
        "proposed_claim_refs": proposed_refs,
        "verifier_decisions": tuple(
            {
                "subject_ref": item.subject_ref,
                "verification_decision_ref": item.verification_decision_ref,
                "reason_code": item.reason_code,
            }
            for item in decisions
        ),
    }
    return "limitation:semantic-verifier-boundary:sha256:" + canonical_digest(body)


def _claim_strength_gap_limitation(
    *,
    obligation_id: str,
    required_claim_strength: str,
    accepted_claim_ref: str,
    publication_ceiling: ClaimPublicationCeiling,
) -> str:
    body = {
        "obligation_id": obligation_id,
        "required_claim_strength": required_claim_strength,
        "accepted_claim_ref": accepted_claim_ref,
        "publication_ceiling": publication_ceiling.to_dict(),
    }
    return "limitation:claim-strength-gap:sha256:" + canonical_digest(body)


def _unique_support_edges(edges: Any) -> tuple[SupportEdge, ...]:
    by_ref: dict[str, SupportEdge] = {}
    for edge in edges:
        if type(edge) is not SupportEdge:
            raise ClaimSettlementContractError("claim_settlement_support_edge_invalid")
        existing = by_ref.setdefault(edge.support_edge_ref, edge)
        if existing != edge:
            raise ClaimSettlementContractError(
                "claim_settlement_support_edge_identity_conflict"
            )
    return tuple(sorted(by_ref.values(), key=lambda item: item.support_edge_ref))


def _replay_proposed_authority(
    namespace: ClaimAuthorityNamespace,
    claim_keys: Sequence[ClaimKey],
    claims: Sequence[ClaimRevision],
    support_edges: Sequence[SupportEdge],
) -> tuple[tuple[ClaimKey, ...], tuple[ClaimRevision, ...], tuple[SupportEdge, ...]]:
    if (
        any(type(item) is not ClaimKey for item in claim_keys)
        or any(type(item) is not ClaimRevision for item in claims)
        or any(type(item) is not SupportEdge for item in support_edges)
    ):
        raise ClaimSettlementContractError("claim_settlement_authority_type_invalid")
    key_payloads = tuple(item.to_dict() for item in claim_keys)
    edge_payloads = tuple(item.to_dict() for item in support_edges)
    keys, edges = _claim_keys_and_edges_from_payload(
        namespace, key_payloads, edge_payloads
    )
    replayed_claims = _claims_from_payload(
        namespace,
        tuple(item.to_dict() for item in claims),
        keys,
        edges,
    )
    if keys != tuple(sorted(claim_keys, key=lambda item: item.claim_key)):
        raise ClaimSettlementContractError("claim_settlement_claim_keys_invalid")
    if edges != tuple(sorted(support_edges, key=lambda item: item.support_edge_ref)):
        raise ClaimSettlementContractError("claim_settlement_support_edges_invalid")
    if replayed_claims != tuple(sorted(claims, key=lambda item: item.claim_ref)):
        raise ClaimSettlementContractError("claim_settlement_claims_invalid")
    return keys, replayed_claims, edges


def _claim_keys_and_edges_from_payload(
    namespace: ClaimAuthorityNamespace,
    raw_keys: Any,
    raw_edges: Any,
) -> tuple[tuple[ClaimKey, ...], tuple[SupportEdge, ...]]:
    if (
        isinstance(raw_keys, (str, bytes))
        or not isinstance(raw_keys, Sequence)
        or isinstance(raw_edges, (str, bytes))
        or not isinstance(raw_edges, Sequence)
    ):
        raise ClaimSettlementContractError("claim_settlement_authority_shape_invalid")
    keys = tuple(
        sorted(
            (
                ClaimKey.from_dict(item, authority_namespace=namespace)
                for item in raw_keys
            ),
            key=lambda item: item.claim_key,
        )
    )
    edges = tuple(
        sorted(
            (
                SupportEdge.from_dict(item, authority_namespace=namespace)
                for item in raw_edges
            ),
            key=lambda item: item.support_edge_ref,
        )
    )
    if len({item.claim_key for item in keys}) != len(keys) or len(
        {item.support_edge_ref for item in edges}
    ) != len(edges):
        raise ClaimSettlementContractError("claim_settlement_authority_duplicate")
    return keys, edges


def _claims_from_payload(
    namespace: ClaimAuthorityNamespace,
    raw_claims: Any,
    keys: Sequence[ClaimKey],
    edges: Sequence[SupportEdge],
) -> tuple[ClaimRevision, ...]:
    if isinstance(raw_claims, (str, bytes)) or not isinstance(raw_claims, Sequence):
        raise ClaimSettlementContractError("claim_settlement_claims_invalid")
    key_by_ref = {item.claim_key: item for item in keys}
    edge_by_ref = {item.support_edge_ref: item for item in edges}
    claims: list[ClaimRevision] = []
    for payload in raw_claims:
        if not isinstance(payload, Mapping):
            raise ClaimSettlementContractError("claim_settlement_claims_invalid")
        key = key_by_ref.get(str(payload.get("claim_key")))
        raw_edge_refs = payload.get("support_edge_refs")
        if (
            key is None
            or isinstance(raw_edge_refs, (str, bytes))
            or not isinstance(raw_edge_refs, Sequence)
        ):
            raise ClaimSettlementContractError("claim_settlement_claims_invalid")
        try:
            claim_edges = tuple(edge_by_ref[str(ref)] for ref in raw_edge_refs)
        except KeyError as exc:
            raise ClaimSettlementContractError(
                "claim_settlement_claims_invalid"
            ) from exc
        claims.append(
            ClaimRevision.from_dict(
                payload,
                authority_namespace=namespace,
                claim_key=key,
                support_edges=claim_edges,
            )
        )
    normalized = tuple(sorted(claims, key=lambda item: item.claim_ref))
    if len({item.claim_ref for item in normalized}) != len(normalized):
        raise ClaimSettlementContractError("claim_settlement_claims_invalid")
    return normalized


def _replay_obligation_basis(
    namespace: ClaimAuthorityNamespace,
    value: Sequence[ObligationSettlementBasis],
) -> tuple[ObligationSettlementBasis, ...]:
    if any(type(item) is not ObligationSettlementBasis for item in value):
        raise ClaimSettlementContractError("claim_settlement_obligation_basis_invalid")
    replayed = tuple(
        sorted(
            (
                ObligationSettlementBasis.from_dict(
                    item.to_dict(), authority_namespace=namespace
                )
                for item in value
            ),
            key=lambda item: item.obligation_id,
        )
    )
    if replayed != tuple(sorted(value, key=lambda item: item.obligation_id)) or len(
        {item.obligation_id for item in replayed}
    ) != len(replayed):
        raise ClaimSettlementContractError("claim_settlement_obligation_basis_invalid")
    return replayed


def _replay_coverage(
    namespace: ClaimAuthorityNamespace,
    value: Sequence[ObligationCoverage],
    *,
    verifier_report: ClaimVerifierReport,
) -> tuple[ObligationCoverage, ...]:
    if any(type(item) is not ObligationCoverage for item in value):
        raise ClaimSettlementContractError("claim_settlement_coverage_invalid")
    replayed = tuple(
        sorted(
            (
                ObligationCoverage.from_dict(
                    item.to_dict(),
                    authority_namespace=namespace,
                    verifier_report=verifier_report,
                )
                for item in value
            ),
            key=lambda item: item.obligation_id,
        )
    )
    if replayed != tuple(sorted(value, key=lambda item: item.obligation_id)):
        raise ClaimSettlementContractError("claim_settlement_coverage_invalid")
    return replayed


def _validated_recommendations(
    namespace: ClaimAuthorityNamespace,
    value: Sequence[RecommendationRecord],
    *,
    claim_settlement: ClaimSettlement,
) -> tuple[RecommendationRecord, ...]:
    if any(type(item) is not RecommendationRecord for item in value):
        raise ClaimSettlementContractError(
            "authority_bundle_inputs_recommendations_invalid"
        )
    normalized = tuple(sorted(value, key=lambda item: item.recommendation_ref))
    if len({item.recommendation_ref for item in normalized}) != len(normalized):
        raise ClaimSettlementContractError(
            "authority_bundle_inputs_recommendations_invalid"
        )
    for item in normalized:
        if (
            item.authority_namespace_ref != namespace.authority_namespace_ref
            or item.claim_graph_ref != claim_settlement.claim_graph.claim_graph_ref
            or item.claim_verifier_report_ref
            != claim_settlement.verifier_report.verifier_report_ref
        ):
            raise ClaimSettlementContractError(
                "authority_bundle_inputs_recommendations_invalid"
            )
    return normalized


def _recommendations_from_payload(
    namespace: ClaimAuthorityNamespace,
    raw_value: Any,
    *,
    claim_settlement: ClaimSettlement,
) -> tuple[RecommendationRecord, ...]:
    if isinstance(raw_value, (str, bytes)) or not isinstance(raw_value, Sequence):
        raise ClaimSettlementContractError(
            "authority_bundle_inputs_recommendations_invalid"
        )
    records: list[RecommendationRecord] = []
    for payload in raw_value:
        if not isinstance(payload, Mapping):
            raise ClaimSettlementContractError(
                "authority_bundle_inputs_recommendations_invalid"
            )
        try:
            records.append(
                RecommendationRecord.from_dict(
                    payload,
                    authority_namespace=namespace,
                    claim_settlement=claim_settlement,
                )
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimSettlementContractError(
                "authority_bundle_inputs_recommendations_invalid"
            ) from exc
    normalized = tuple(sorted(records, key=lambda item: item.recommendation_ref))
    if len({item.recommendation_ref for item in normalized}) != len(normalized):
        raise ClaimSettlementContractError(
            "authority_bundle_inputs_recommendations_invalid"
        )
    return normalized


__all__ = (
    "admissible_evidence_publication_ceiling",
    "AuthorityBundleInputs",
    "CandidateClaimProposal",
    "CandidateEvidenceSupport",
    "ClaimSettlement",
    "ClaimSettlementCheckpoint",
    "ClaimSettlementContractError",
    "ObligationSettlementBasis",
    "evidence_publication_ceiling",
    "prepare_claim_settlement",
    "publication_ceiling_satisfies",
    "settle_claim_checkpoint",
    "validate_typed_claim_settlement",
)
