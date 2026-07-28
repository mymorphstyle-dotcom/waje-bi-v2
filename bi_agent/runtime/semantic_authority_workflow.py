from __future__ import annotations

from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from bi_agent.runtime.capability_authority import EvidenceLedgerEntry
from bi_agent.runtime.claim_authority import (
    ClaimAuthorityNamespace,
    ClaimKey,
    ClaimRevision,
    RECOMMENDATION_ACTION_DOMAINS,
    RECOMMENDATION_ACTION_STAGES,
    RECOMMENDATION_COMMITMENT_CONTRACT_VERSION,
    RECOMMENDATION_COMMITMENT_KINDS,
    RECOMMENDATION_DIAGNOSTIC_MODES,
    RECOMMENDATION_EXPECTED_VALUE_KINDS,
    RECOMMENDATION_EXPECTED_VALUE_MODES,
    RecommendationCommitment,
    RecommendationProposal,
    RecommendationRecord,
    SemanticVerificationAttempt,
    SemanticVerificationDecision,
    SupportEdge,
    VERIFICATION_VETO_BASES,
    recommendation_authorization_for_ceiling,
)
from bi_agent.runtime.claim_coverage import ClaimCoverageCheckpoint
from bi_agent.runtime.claim_settlement import (
    admissible_obligation_evidence_source_claim_kind,
    AuthorityBundleInputs,
    CandidateClaimProposal,
    CandidateEvidenceSupport,
    ClaimSettlement,
    ClaimSettlementCheckpoint,
    prepare_claim_settlement,
    settle_claim_checkpoint,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.narrative_authority import RestrictedProviderResponse
from bi_agent.runtime.plan_authority import EvidenceRequirement


class SemanticAuthorityWorkflowError(ValueError):
    pass


_CANDIDATE_CLAIM_KINDS = frozenset(
    {"business_object_candidate_impact", "candidate_mechanism"}
)
_CALL_PURPOSES = frozenset(
    {
        "candidate_claim_proposal",
        "claim_verification",
        "recommendation_proposal",
        "recommendation_verification",
    }
)
_FORBIDDEN_PROJECTION_FIELDS = frozenset(
    {
        "api_key",
        "authority_owner_ref",
        "debug",
        "internal_debug",
        "owner",
        "owner_id",
        "owner_ref",
        "password",
        "raw_data",
        "raw_rows",
        "secret",
        "secrets",
        "sql",
        "sql_text",
        "technical_detail",
        "technical_detail_ref",
        "thread_owner",
        "token",
    }
)
_SEMANTIC_PROMPT_VERSION = "single-authority-phase04.v13"
_THINKING_MODE_BY_PURPOSE = MappingProxyType(
    {
        "candidate_claim_proposal": "disabled",
        "claim_verification": "disabled",
        "recommendation_proposal": "disabled",
        "recommendation_verification": "disabled",
    }
)
_SEMANTIC_ENCODING_TAG = "__waje_semantic_encoding__"
_SEMANTIC_HOMOGENEOUS_RECORDS = "homogeneous_records.v1"
_SEMANTIC_LITERAL_MAPPING = "literal_mapping.v1"
_SEMANTIC_PROJECTION_FORMAT = "lossless-columnar-json.v1"
_SEMANTIC_ENCODING_GUIDANCE = (
    "Values marked lossless-columnar-json.v1 are complete, model-readable JSON: "
    "field_names defines each homogeneous record field once, and every positional "
    "record_values row aligns to those fields. No record is omitted. "
)
_PUBLICATION_DISPOSITION_GUIDANCE = (
    "Each aggregate evidence item has publication_disposition. direct may support "
    "a claim within its supplied effective claim kinds and ceiling. weakened may "
    "support only the supplied weaker ceiling and must never be upgraded. "
    "observation_only may be described as an aggregate observation but cannot "
    "support a claim or recommendation. coverage_audit_codes explain internal "
    "classification drift and are limitations, never evidence. "
)
_CANDIDATE_OUTPUT_CONTRACT = (
    "Return one JSON object whose only top-level key is "
    "candidate_claim_proposals. Its value is an array. Each item must contain "
    "exactly obligation_id, subject, factual_payload, "
    "assumption_refs, and limitation_refs. "
    "The runtime assigns proposal identity; never create or return a proposal_item_ref. "
    "The runtime also binds every proposal to that obligation's supplied "
    "eligible_candidate_evidence_refs using the obligation claim_kind; never create or "
    "return evidence_support or any evidence reference. "
    "obligation_id and subject are non-empty strings; subject is a concise business "
    "statement. factual_payload is a non-empty JSON object carrying structured facts, "
    "never a string. assumption_refs and limitation_refs are arrays of strings and may "
    "be empty. "
    "Use only top-level assumption_refs and limitation_refs that directly qualify "
    "that proposal; do not attach unrelated references. "
    "Propose only for supplied obligations with a non-empty "
    "eligible_candidate_evidence_refs array; direct and boundary claims are handled "
    'elsewhere. Use {"candidate_claim_proposals":[]} when no candidate is '
    "supported. Do not repeat the same business proposal. Every returned reference "
    "must come from the input. "
)
_CLAIM_VERIFICATION_OUTPUT_CONTRACT = (
    "Return one JSON object whose only top-level key is decisions. decisions is an "
    "object whose keys are exactly every supplied proposed claim_ref. Each value "
    "contains exactly disposition, veto_basis, reason_code, and limitation_refs; "
    "do not repeat "
    "subject_ref inside the value. disposition is the string accepted or vetoed, and "
    "limitation_refs is an array of strings. For accepted, reason_code is null and "
    "veto_basis is null and limitation_refs is empty; qualifiers already attached "
    "to the claim remain attached to the accepted claim. For vetoed, reason_code is "
    "a non-empty string, veto_basis is one of "
    + ", ".join(sorted(VERIFICATION_VETO_BASES))
    + ", and limitation_refs may be empty. Every returned limitation_ref must belong "
    "to the corresponding claim or one of that claim's support_sources. "
)
_RECOMMENDATION_OUTPUT_CONTRACT = (
    "Return one JSON object whose only top-level key is recommendation_proposals. "
    "Its value is an array. Each item contains exactly commitment_contract_version, "
    "commitments, supporting_claim_refs, assumption_refs, risk_refs, action, "
    "applicable_conditions, and expected_decision_value. "
    f"commitment_contract_version must equal {RECOMMENDATION_COMMITMENT_CONTRACT_VERSION}. "
    "commitments is a non-empty array. Each commitment contains exactly "
    "commitment_kind, text, supporting_claim_refs, diagnostic_mode, action_domain, "
    "action_stage, expected_value_kind, and expected_value_mode. commitment_kind is "
    "one of "
    + ", ".join(sorted(RECOMMENDATION_COMMITMENT_KINDS))
    + ". Every commitment has non-empty text and supporting_claim_refs. A "
    "diagnostic_premise sets diagnostic_mode to one of "
    + ", ".join(sorted(RECOMMENDATION_DIAGNOSTIC_MODES))
    + " and sets all action and expected-value fields to null. An action sets "
    "action_domain to one of "
    + ", ".join(sorted(RECOMMENDATION_ACTION_DOMAINS))
    + " and action_stage to one of "
    + ", ".join(sorted(RECOMMENDATION_ACTION_STAGES))
    + " and sets diagnostic and expected-value fields to null. An expected_outcome "
    "sets expected_value_kind to one of "
    + ", ".join(sorted(RECOMMENDATION_EXPECTED_VALUE_KINDS))
    + " and expected_value_mode to one of "
    + ", ".join(sorted(RECOMMENDATION_EXPECTED_VALUE_MODES))
    + " and sets diagnostic and action fields to null. Include exactly one action "
    "and exactly one expected_outcome; action must exactly equal the action commitment "
    "text and expected_decision_value must exactly equal the expected_outcome text. "
    "The item supporting_claim_refs must equal the union of commitment support refs. "
    "Each commitment must stay inside every bound claim's authorization resolved "
    "through recommendation_authorization_ref and the supplied "
    "recommendation_authorization_catalog. supporting_claim_refs is a non-empty array of "
    "claim references from the input. applicable_conditions is a non-empty array "
    "of customer-facing business conditions and must not contain claim refs or "
    "other internal identifiers. action, expected_decision_value, commitment text, "
    "and applicable_conditions must all be readable without internal refs. "
    "assumption_refs and risk_refs are arrays of references from the input and may "
    "be empty. Use "
    '{"recommendation_proposals":[]} when no useful grounded recommendation '
    "exists. "
)
_OPTIONAL_RECOMMENDATION_POLICY_REJECTIONS = frozenset(
    {"recommendation_commitment_claim_ceiling_exceeded"}
)
_RECOMMENDATION_VERIFICATION_OUTPUT_CONTRACT = (
    "Return one JSON object whose only top-level key is decision. decision contains "
    "exactly subject_ref, disposition, veto_basis, reason_code, limitation_refs, and "
    "verified_commitment_refs. subject_ref "
    "must equal the recommendation_proposal_ref. disposition is the string accepted "
    "or vetoed; limitation_refs and verified_commitment_refs are arrays of strings. "
    "For accepted, verified_commitment_refs contains exactly every supplied "
    "recommendation_commitment_ref, reason_code and veto_basis are null, and "
    "limitation_refs is empty. For vetoed, "
    "reason_code is a non-empty string, veto_basis is one of "
    + ", ".join(sorted(VERIFICATION_VETO_BASES))
    + ", verified_commitment_refs may contain only supplied commitment refs, and "
    "limitation_refs may be an empty subset of supplied limitation_refs. "
)
_PROMPTS = MappingProxyType(
    {
        "candidate_claim_proposal": (
            _SEMANTIC_ENCODING_GUIDANCE
            + _PUBLICATION_DISPOSITION_GUIDANCE
            + _CANDIDATE_OUTPUT_CONTRACT
            + "Propose zero or more candidate claims from the supplied aggregate "
            "evidence. Bind every reference to the supplied identifiers. Returning "
            "an empty list is valid when the evidence does not support a useful "
            "candidate."
        ),
        "claim_verification": (
            _SEMANTIC_ENCODING_GUIDANCE
            + _PUBLICATION_DISPOSITION_GUIDANCE
            + _CLAIM_VERIFICATION_OUTPUT_CONTRACT
            + "Evaluate every proposed claim against the supplied aggregate evidence, "
            "obligation, publication ceiling, assumptions, and limitations. "
            "Use each obligation success_policy and required_claim_strength when "
            "judging whether a claim can satisfy that obligation. "
            "The evidence_requirement operator any_of means each claim needs to bind "
            "at least one listed evidence_kind. Treat evidence_kinds as alternatives, "
            "never as a conjunction. Evaluate each claim only against its own "
            "bound_evidence_kinds and support_sources; do not borrow evidence from the "
            "same obligation's other proposed claims. A supplied "
            "evidence_requirement_status of satisfied cannot be vetoed with "
            "evidence_requirement_unsatisfied. "
            "support is reference-bound: evidence facts appear once in the aggregate "
            "evidence catalog and claims point to them by evidence_entry_refs. Return "
            "exactly one accepted or vetoed decision for every supplied claim "
            "reference. Evaluate each claim within its declared claim_class and "
            "publication_ceiling. The absence of stronger or causal evidence does not "
            "veto a claim within its declared boundary. Accepted decision refs are "
            "empty because existing claim and support qualifiers remain attached to "
            "the accepted claim."
        ),
        "recommendation_proposal": (
            _SEMANTIC_ENCODING_GUIDANCE
            + _RECOMMENDATION_OUTPUT_CONTRACT
            + "Propose zero or more decision recommendations grounded only in the "
            "verified claim graph. Each verified claim carries its accepted factual "
            "payload, evidence references, publication ceiling, limitations, and "
            "recommendation authorization. verified_evidence_context carries the business "
            "facts for those references once, without replay-only authority metadata. Claim "
            "verification has already closed those references; do not ask for or infer new "
            "evidence. Returning an empty list is valid. Bind open-language diagnostic "
            "premises, actions, and expected outcomes to typed commitments. Resolve each claim's "
            "recommendation_authorization_ref through the shared "
            "recommendation_authorization_catalog; never upgrade a claim into a "
            "stronger diagnostic mode, action stage, or expected-value mode."
        ),
        "recommendation_verification": (
            _SEMANTIC_ENCODING_GUIDANCE
            + _RECOMMENDATION_VERIFICATION_OUTPUT_CONTRACT
            + "Evaluate the supplied recommendation independently against its verified "
            "claim support, reference-bound aggregate evidence, assumptions, and "
            "risks. Verify every typed commitment separately and return positive "
            "coverage for all commitments when accepting. A semantic support veto can "
            "stand on its reason and veto basis without borrowing an unrelated "
            "limitation. Return one accepted or vetoed decision for the supplied "
            "recommendation proposal reference."
        ),
    }
)
_REQUIRED_OUTPUT_KEY = MappingProxyType(
    {
        "candidate_claim_proposal": "candidate_claim_proposals",
        "claim_verification": "decisions",
        "recommendation_proposal": "recommendation_proposals",
        "recommendation_verification": "decision",
    }
)


class TypedSemanticAuthorityLLM(Protocol):
    def invoke_json(
        self,
        *,
        task: str,
        prompt_version: str,
        messages: Sequence[Mapping[str, str]],
        required_keys: Sequence[str],
        output_validator: Callable[[Mapping[str, Any]], None] | None = None,
        model_tier: str = "default",
        thinking: str | None = None,
    ) -> Any: ...


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SemanticAuthorityWorkflowError(error)
    return value


def _positive_integer(value: Any, error: str) -> int:
    if type(value) is not int or value < 1:
        raise SemanticAuthorityWorkflowError(error)
    return value


def _digest_string(value: Any, error: str) -> str:
    value = _required_string(value, error)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise SemanticAuthorityWorkflowError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SemanticAuthorityWorkflowError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if (not allow_empty and not normalized) or len(normalized) != len(set(normalized)):
        raise SemanticAuthorityWorkflowError(error)
    return tuple(sorted(normalized)) if sort else normalized


def _strict_mapping(
    value: Any,
    fields: frozenset[str],
    error: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SemanticAuthorityWorkflowError(error)
    return value


def _mapping_sequence(value: Any, error: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SemanticAuthorityWorkflowError(error)
    if any(not isinstance(item, Mapping) for item in value):
        raise SemanticAuthorityWorkflowError(error)
    return tuple(value)


def _assert_restricted(value: Any, error: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = _required_string(raw_key, error)
            if key.casefold() in _FORBIDDEN_PROJECTION_FIELDS:
                raise SemanticAuthorityWorkflowError(f"{error}:{key}")
            _assert_restricted(item, error)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _assert_restricted(item, error)


def _frozen(value: Any, error: str) -> Any:
    _assert_restricted(value, error)
    try:
        normalized = canonical_value(value)
    except ValueError as exc:
        raise SemanticAuthorityWorkflowError(error) from exc
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _frozen(item, error) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_frozen(item, error) for item in normalized)
    return normalized


def _immutable(value: Any, error: str) -> Any:
    try:
        normalized = canonical_value(value)
    except ValueError as exc:
        raise SemanticAuthorityWorkflowError(error) from exc
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _immutable(item, error) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_immutable(item, error) for item in normalized)
    return normalized


def _encode_semantic_projection(value: Any) -> Any:
    """Encode repeated records once while keeping the JSON legible to the model."""
    normalized = canonical_value(value)

    def encode(item: Any) -> Any:
        if isinstance(item, Mapping):
            if _SEMANTIC_ENCODING_TAG in item:
                return {
                    _SEMANTIC_ENCODING_TAG: _SEMANTIC_LITERAL_MAPPING,
                    "entries": [[key, encode(item[key])] for key in sorted(item)],
                }
            return {key: encode(item[key]) for key in sorted(item)}
        if isinstance(item, list):
            if len(item) >= 2 and all(isinstance(record, Mapping) for record in item):
                field_names = sorted(item[0])
                if field_names and all(
                    sorted(record) == field_names for record in item[1:]
                ):
                    return {
                        _SEMANTIC_ENCODING_TAG: _SEMANTIC_HOMOGENEOUS_RECORDS,
                        "field_names": field_names,
                        "record_values": [
                            [encode(record[field]) for field in field_names]
                            for record in item
                        ],
                    }
            return [encode(child) for child in item]
        return item

    encoded = encode(normalized)
    if _decode_semantic_projection(encoded) != normalized:
        raise SemanticAuthorityWorkflowError(
            "semantic_projection_lossless_round_trip_invalid"
        )
    return encoded


def _decode_semantic_projection(value: Any) -> Any:
    """Decode the lossless semantic request projection for integrity checks."""

    def decode(item: Any) -> Any:
        if isinstance(item, Mapping):
            encoding = item.get(_SEMANTIC_ENCODING_TAG)
            if encoding is None:
                return {key: decode(item[key]) for key in sorted(item)}
            if encoding == _SEMANTIC_HOMOGENEOUS_RECORDS:
                if set(item) != {
                    _SEMANTIC_ENCODING_TAG,
                    "field_names",
                    "record_values",
                }:
                    raise SemanticAuthorityWorkflowError(
                        "semantic_projection_encoding_shape_invalid"
                    )
                field_names = item["field_names"]
                record_values = item["record_values"]
                if (
                    isinstance(field_names, (str, bytes))
                    or not isinstance(field_names, Sequence)
                    or not field_names
                    or any(
                        not isinstance(field, str) or not field for field in field_names
                    )
                    or list(field_names) != sorted(field_names)
                    or len(set(field_names)) != len(field_names)
                    or isinstance(record_values, (str, bytes))
                    or not isinstance(record_values, Sequence)
                ):
                    raise SemanticAuthorityWorkflowError(
                        "semantic_projection_encoding_shape_invalid"
                    )
                records = []
                for raw_values in record_values:
                    if (
                        isinstance(raw_values, (str, bytes))
                        or not isinstance(raw_values, Sequence)
                        or len(raw_values) != len(field_names)
                    ):
                        raise SemanticAuthorityWorkflowError(
                            "semantic_projection_encoding_shape_invalid"
                        )
                    records.append(
                        {
                            field: decode(raw_values[index])
                            for index, field in enumerate(field_names)
                        }
                    )
                return records
            if encoding == _SEMANTIC_LITERAL_MAPPING:
                if set(item) != {_SEMANTIC_ENCODING_TAG, "entries"}:
                    raise SemanticAuthorityWorkflowError(
                        "semantic_projection_encoding_shape_invalid"
                    )
                entries = item["entries"]
                if isinstance(entries, (str, bytes)) or not isinstance(
                    entries, Sequence
                ):
                    raise SemanticAuthorityWorkflowError(
                        "semantic_projection_encoding_shape_invalid"
                    )
                decoded: dict[str, Any] = {}
                for entry in entries:
                    if (
                        isinstance(entry, (str, bytes))
                        or not isinstance(entry, Sequence)
                        or len(entry) != 2
                        or not isinstance(entry[0], str)
                        or not entry[0]
                        or entry[0] in decoded
                    ):
                        raise SemanticAuthorityWorkflowError(
                            "semantic_projection_encoding_shape_invalid"
                        )
                    decoded[entry[0]] = decode(entry[1])
                return {key: decoded[key] for key in sorted(decoded)}
            raise SemanticAuthorityWorkflowError(
                "semantic_projection_encoding_version_invalid"
            )
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            return [decode(child) for child in item]
        return item

    return canonical_value(decode(value))


def _lossless_semantic_value(value: Any) -> Mapping[str, Any]:
    normalized = canonical_value(value)
    return {
        "encoding": _SEMANTIC_PROJECTION_FORMAT,
        "source_digest": canonical_digest(normalized),
        "value": _encode_semantic_projection(normalized),
    }


def _validated_execution(
    value: AuthoritativeExecutionResult,
) -> AuthoritativeExecutionResult:
    if type(value) is not AuthoritativeExecutionResult:
        raise SemanticAuthorityWorkflowError("semantic_authority_execution_invalid")
    try:
        replayed = AuthoritativeExecutionResult.from_dict(value.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_execution_invalid"
        ) from exc
    if replayed != value:
        raise SemanticAuthorityWorkflowError("semantic_authority_execution_invalid")
    return replayed


def _validated_namespace(
    value: ClaimAuthorityNamespace,
    *,
    execution: AuthoritativeExecutionResult,
) -> ClaimAuthorityNamespace:
    if type(value) is not ClaimAuthorityNamespace:
        raise SemanticAuthorityWorkflowError("semantic_authority_namespace_invalid")
    try:
        replayed = ClaimAuthorityNamespace.from_dict(value.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_namespace_invalid"
        ) from exc
    if replayed != value or replayed.run_attempt_id != execution.run_attempt_id:
        raise SemanticAuthorityWorkflowError("semantic_authority_namespace_invalid")
    return replayed


@dataclass(frozen=True)
class RestrictedAggregateEvidence:
    evidence_entry_ref: str
    evidence_kind: str
    data_contract_state: str
    supported_claim_kinds: tuple[str, ...]
    effective_supported_claim_kinds: tuple[str, ...]
    publication_disposition: str
    effective_maximum_claim_strength_by_claim: Mapping[str, str]
    coverage_audit_codes: tuple[str, ...]
    evidence_strength: str
    maximum_claim_strength: str
    observation_facts: tuple[Mapping[str, Any], ...]
    scope: str
    window_refs: tuple[str, ...]
    dimension_path: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    obligation_ids: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        entry: EvidenceLedgerEntry,
        obligation_ids: Sequence[str],
        publication_policy: Mapping[str, Any],
    ) -> "RestrictedAggregateEvidence":
        if type(entry) is not EvidenceLedgerEntry:
            raise SemanticAuthorityWorkflowError(
                "restricted_aggregate_evidence_invalid"
            )
        try:
            replayed = EvidenceLedgerEntry.from_dict(entry.to_dict())
        except (AttributeError, TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "restricted_aggregate_evidence_invalid"
            ) from exc
        if replayed != entry:
            raise SemanticAuthorityWorkflowError(
                "restricted_aggregate_evidence_invalid"
            )
        if not isinstance(publication_policy, Mapping) or set(
            publication_policy
        ) != {
            "publication_disposition",
            "effective_supported_claim_kinds",
            "effective_maximum_claim_strength_by_claim",
            "coverage_audit_codes",
        }:
            raise SemanticAuthorityWorkflowError(
                "restricted_aggregate_evidence_policy_invalid"
            )
        disposition = publication_policy.get("publication_disposition")
        if disposition not in {"direct", "weakened", "observation_only"}:
            raise SemanticAuthorityWorkflowError(
                "restricted_aggregate_evidence_policy_invalid"
            )
        effective_claim_kinds = _string_tuple(
            publication_policy.get("effective_supported_claim_kinds"),
            "restricted_aggregate_evidence_policy_invalid",
        )
        raw_strengths = publication_policy.get(
            "effective_maximum_claim_strength_by_claim"
        )
        if not isinstance(raw_strengths, Mapping):
            raise SemanticAuthorityWorkflowError(
                "restricted_aggregate_evidence_policy_invalid"
            )
        effective_strengths = {
            _required_string(
                claim_kind,
                "restricted_aggregate_evidence_policy_invalid",
            ): _required_string(
                strength,
                "restricted_aggregate_evidence_policy_invalid",
            )
            for claim_kind, strength in raw_strengths.items()
        }
        if (
            set(effective_strengths) != set(effective_claim_kinds)
            or not set(effective_claim_kinds).issubset(
                entry.supported_claim_kinds
            )
            or (
                disposition == "observation_only"
                and (effective_claim_kinds or effective_strengths)
            )
        ):
            raise SemanticAuthorityWorkflowError(
                "restricted_aggregate_evidence_policy_invalid"
            )
        coverage_audit_codes = _string_tuple(
            publication_policy.get("coverage_audit_codes"),
            "restricted_aggregate_evidence_policy_invalid",
        )
        facts = tuple(
            _frozen(item, "restricted_aggregate_evidence_forbidden_field")
            for item in entry.observation_facts
        )
        body = {
            "evidence_entry_ref": entry.entry_ref,
            "evidence_kind": entry.evidence_kind,
            "data_contract_state": entry.data_contract_state,
            "supported_claim_kinds": entry.supported_claim_kinds,
            "effective_supported_claim_kinds": effective_claim_kinds,
            "publication_disposition": disposition,
            "effective_maximum_claim_strength_by_claim": _frozen(
                effective_strengths,
                "restricted_aggregate_evidence_policy_invalid",
            ),
            "coverage_audit_codes": coverage_audit_codes,
            "evidence_strength": entry.evidence_strength,
            "maximum_claim_strength": entry.maximum_claim_strength,
            "observation_facts": facts,
            "scope": entry.scope,
            "window_refs": entry.window_refs,
            "dimension_path": entry.dimension_path,
            "limitation_refs": entry.limitation_refs,
            "obligation_ids": _string_tuple(
                obligation_ids,
                "restricted_aggregate_evidence_obligations_invalid",
            ),
        }
        return cls(content_digest=canonical_digest(body), **body)

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class RestrictedClaimObligation:
    obligation_id: str
    claim_kind: str
    role: str
    subject: Mapping[str, Any]
    evidence_requirement: EvidenceRequirement
    success_policy: Mapping[str, Any]
    required_claim_strength: str
    eligible_candidate_evidence_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        obligation_id: str,
        claim_kind: str,
        role: str,
        subject: Mapping[str, Any],
        evidence_requirement: EvidenceRequirement,
        success_policy: Mapping[str, Any],
        eligible_candidate_evidence_refs: Sequence[str],
    ) -> "RestrictedClaimObligation":
        policy = _frozen(success_policy, "restricted_obligation_forbidden_field")
        if policy.get("policy") != "verified_or_explicit_boundary":
            raise SemanticAuthorityWorkflowError(
                "restricted_obligation_success_policy_invalid"
            )
        required_strength = _required_string(
            policy.get("minimum_claim_strength"),
            "restricted_obligation_success_policy_invalid",
        )
        if type(evidence_requirement) is not EvidenceRequirement:
            raise SemanticAuthorityWorkflowError(
                "restricted_obligation_evidence_requirement_invalid"
            )
        requirement = EvidenceRequirement.from_dict(evidence_requirement.to_dict())
        if requirement != evidence_requirement:
            raise SemanticAuthorityWorkflowError(
                "restricted_obligation_evidence_requirement_invalid"
            )
        body = {
            "obligation_id": _required_string(
                obligation_id, "restricted_obligation_id_invalid"
            ),
            "claim_kind": _required_string(
                claim_kind, "restricted_obligation_claim_kind_invalid"
            ),
            "role": _required_string(role, "restricted_obligation_role_invalid"),
            "subject": _frozen(subject, "restricted_obligation_forbidden_field"),
            "evidence_requirement": requirement,
            "success_policy": policy,
            "required_claim_strength": required_strength,
            "eligible_candidate_evidence_refs": _string_tuple(
                eligible_candidate_evidence_refs,
                "restricted_obligation_evidence_refs_invalid",
            ),
        }
        return cls(content_digest=canonical_digest(body), **body)

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class RestrictedExecutionProjection:
    projection_ref: str
    execution_result_ref: str
    execution_result_digest: str
    claim_coverage_evaluation_ref: str
    claim_coverage_evaluation_digest: str
    resolved_window_refs: tuple[str, ...]
    obligations: tuple[RestrictedClaimObligation, ...]
    aggregate_evidence: tuple[RestrictedAggregateEvidence, ...]
    assumption_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        execution_result: AuthoritativeExecutionResult,
        *,
        claim_coverage_checkpoint: ClaimCoverageCheckpoint | None = None,
        persisted_claim_coverage_policy: Mapping[str, Any] | None = None,
    ) -> "RestrictedExecutionProjection":
        execution = _validated_execution(execution_result)
        if claim_coverage_checkpoint is not None:
            if (
                type(claim_coverage_checkpoint)
                is not ClaimCoverageCheckpoint
                or persisted_claim_coverage_policy is not None
            ):
                raise SemanticAuthorityWorkflowError(
                    "restricted_execution_claim_coverage_invalid"
                )
            coverage = claim_coverage_checkpoint.evaluation
            if (
                coverage.source_execution_result_ref
                != execution.authoritative_execution_result_ref
                or coverage.source_execution_result_digest
                != execution.content_digest
                or coverage.source_plan_revision_id
                != execution.plan_revision_id
            ):
                raise SemanticAuthorityWorkflowError(
                    "restricted_execution_claim_coverage_invalid"
                )
            review_by_evidence_ref = {
                item.evidence_entry_ref: item
                for item in coverage.evidence_contract_reviews
            }
            direct_refs = set(coverage.direct_publishable_evidence_refs)
            weakened_refs = set(coverage.weakened_evidence_refs)
            observation_refs = set(coverage.observation_only_evidence_refs)
            coverage_evaluation_ref = coverage.evaluation_ref
            coverage_evaluation_digest = coverage.content_digest
            persisted_policy_by_ref: Mapping[str, Any] | None = None
        else:
            policy = persisted_claim_coverage_policy
            if not isinstance(policy, Mapping) or set(policy) != {
                "claim_coverage_evaluation_ref",
                "claim_coverage_evaluation_digest",
                "evidence_policies",
            }:
                raise SemanticAuthorityWorkflowError(
                    "restricted_execution_claim_coverage_invalid"
                )
            coverage_evaluation_ref = _required_string(
                policy["claim_coverage_evaluation_ref"],
                "restricted_execution_claim_coverage_invalid",
            )
            coverage_evaluation_digest = _digest_string(
                policy["claim_coverage_evaluation_digest"],
                "restricted_execution_claim_coverage_invalid",
            )
            raw_policies = policy["evidence_policies"]
            if not isinstance(raw_policies, Mapping):
                raise SemanticAuthorityWorkflowError(
                    "restricted_execution_claim_coverage_invalid"
                )
            persisted_policy_by_ref = raw_policies
            review_by_evidence_ref = {}
            direct_refs = set()
            weakened_refs = set()
            observation_refs = set()
        task_by_id = {
            item.task_id: item for item in execution.plan_revision.capability_tasks
        }
        evidence: list[RestrictedAggregateEvidence] = []
        limitation_refs: set[str] = set()
        for _, outcome, entries, _ in execution.capability_outcome_bundles:
            task = task_by_id[outcome.task_id]
            limitation_refs.update(outcome.limitation_refs)
            for entry in entries:
                limitation_refs.update(entry.limitation_refs)
                if persisted_policy_by_ref is not None:
                    publication_policy = persisted_policy_by_ref.get(
                        entry.entry_ref
                    )
                    if not isinstance(publication_policy, Mapping):
                        raise SemanticAuthorityWorkflowError(
                            "restricted_execution_claim_coverage_invalid"
                        )
                else:
                    review = review_by_evidence_ref.get(entry.entry_ref)
                    if review is None:
                        raise SemanticAuthorityWorkflowError(
                            "restricted_execution_claim_coverage_invalid"
                        )
                    if entry.entry_ref in weakened_refs:
                        disposition = "weakened"
                    elif entry.entry_ref in direct_refs:
                        disposition = "direct"
                    elif entry.entry_ref in observation_refs:
                        disposition = "observation_only"
                    else:
                        raise SemanticAuthorityWorkflowError(
                            "restricted_execution_claim_coverage_invalid"
                        )
                    publication_policy = {
                        "publication_disposition": disposition,
                        "effective_supported_claim_kinds": (
                            review.effective_supported_claim_kinds
                            if disposition != "observation_only"
                            else ()
                        ),
                        "effective_maximum_claim_strength_by_claim": (
                            review.effective_maximum_claim_strength_by_claim
                            if disposition != "observation_only"
                            else {}
                        ),
                        "coverage_audit_codes": review.audit_codes,
                    }
                evidence.append(
                    RestrictedAggregateEvidence.create(
                        entry=entry,
                        obligation_ids=task.supports_obligation_ids,
                        publication_policy=publication_policy,
                    )
                )
        evidence.sort(key=lambda item: item.evidence_entry_ref)
        if len({item.evidence_entry_ref for item in evidence}) != len(evidence):
            raise SemanticAuthorityWorkflowError(
                "restricted_execution_evidence_duplicated"
            )
        if (
            persisted_policy_by_ref is not None
            and set(persisted_policy_by_ref)
            != {item.evidence_entry_ref for item in evidence}
        ):
            raise SemanticAuthorityWorkflowError(
                "restricted_execution_claim_coverage_invalid"
            )
        evidence_by_obligation: dict[str, list[RestrictedAggregateEvidence]] = {}
        for item in evidence:
            for obligation_id in item.obligation_ids:
                evidence_by_obligation.setdefault(obligation_id, []).append(item)
        obligations = []
        for obligation in execution.plan_revision.claim_obligations:
            eligible = ()
            if obligation.claim_kind in _CANDIDATE_CLAIM_KINDS:
                eligible = tuple(
                    item.evidence_entry_ref
                    for item in evidence_by_obligation.get(obligation.obligation_id, ())
                    if item.data_contract_state == "complete"
                    and item.publication_disposition != "observation_only"
                    and admissible_obligation_evidence_source_claim_kind(
                        obligation=obligation,
                        evidence_kind=item.evidence_kind,
                        supported_claim_kinds=(
                            item.effective_supported_claim_kinds
                        ),
                        maximum_claim_strength=(
                            item.effective_maximum_claim_strength_by_claim.get(
                                obligation.claim_kind,
                                item.maximum_claim_strength,
                            )
                        ),
                    )
                    is not None
                )
            obligations.append(
                RestrictedClaimObligation.create(
                    obligation_id=obligation.obligation_id,
                    claim_kind=obligation.claim_kind,
                    role=obligation.role,
                    subject=obligation.subject,
                    evidence_requirement=obligation.evidence_requirement,
                    success_policy=obligation.success_policy,
                    eligible_candidate_evidence_refs=eligible,
                )
            )
        obligations.sort(key=lambda item: item.obligation_id)
        body = {
            "execution_result_ref": execution.authoritative_execution_result_ref,
            "execution_result_digest": execution.content_digest,
            "claim_coverage_evaluation_ref": coverage_evaluation_ref,
            "claim_coverage_evaluation_digest": coverage_evaluation_digest,
            "resolved_window_refs": execution.plan_revision.resolved_window_refs,
            "obligations": tuple(obligations),
            "aggregate_evidence": tuple(evidence),
            "assumption_refs": execution.plan_revision.assumption_refs,
            "limitation_refs": tuple(sorted(limitation_refs)),
        }
        digest = canonical_digest(body)
        return cls(
            projection_ref="restricted-execution-projection:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @property
    def has_candidate_authority(self) -> bool:
        return any(
            item.claim_kind in _CANDIDATE_CLAIM_KINDS
            and item.eligible_candidate_evidence_refs
            for item in self.obligations
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        execution_result: AuthoritativeExecutionResult,
    ) -> "RestrictedExecutionProjection":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise SemanticAuthorityWorkflowError(
                "restricted_execution_projection_shape_invalid"
            )
        raw_evidence = payload.get("aggregate_evidence")
        if isinstance(raw_evidence, (str, bytes)) or not isinstance(
            raw_evidence, Sequence
        ):
            raise SemanticAuthorityWorkflowError(
                "restricted_execution_projection_shape_invalid"
            )
        evidence_policies: dict[str, Any] = {}
        for item in raw_evidence:
            if not isinstance(item, Mapping):
                raise SemanticAuthorityWorkflowError(
                    "restricted_execution_projection_shape_invalid"
                )
            evidence_ref = item.get("evidence_entry_ref")
            if not isinstance(evidence_ref, str) or evidence_ref in evidence_policies:
                raise SemanticAuthorityWorkflowError(
                    "restricted_execution_projection_shape_invalid"
                )
            evidence_policies[evidence_ref] = {
                "publication_disposition": item.get(
                    "publication_disposition"
                ),
                "effective_supported_claim_kinds": item.get(
                    "effective_supported_claim_kinds"
                ),
                "effective_maximum_claim_strength_by_claim": item.get(
                    "effective_maximum_claim_strength_by_claim"
                ),
                "coverage_audit_codes": item.get("coverage_audit_codes"),
            }
        rebuilt = cls.create(
            execution_result,
            persisted_claim_coverage_policy={
                "claim_coverage_evaluation_ref": payload.get(
                    "claim_coverage_evaluation_ref"
                ),
                "claim_coverage_evaluation_digest": payload.get(
                    "claim_coverage_evaluation_digest"
                ),
                "evidence_policies": evidence_policies,
            },
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise SemanticAuthorityWorkflowError(
                "restricted_execution_projection_integrity_invalid"
            )
        return rebuilt


@dataclass(frozen=True)
class SemanticAuthorityCallInput:
    call_input_ref: str
    purpose: str
    authority_input_ref: str
    payload: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        purpose: str,
        authority_input_ref: str,
        payload: Mapping[str, Any],
    ) -> "SemanticAuthorityCallInput":
        if purpose not in _CALL_PURPOSES:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_call_purpose_invalid"
            )
        body = {
            "purpose": purpose,
            "authority_input_ref": _required_string(
                authority_input_ref,
                "semantic_authority_call_authority_ref_invalid",
            ),
            "payload": _frozen(payload, "semantic_authority_call_forbidden_field"),
        }
        digest = canonical_digest(body)
        return cls(
            call_input_ref="semantic-authority-call-input:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


def _provider_attempt_id(
    *,
    purpose: str,
    provider_ref: str,
    model_ref: str,
    input_ref: str,
    input_digest: str,
    attempt_number: int,
    provider_response_id: str,
) -> str:
    digest = canonical_digest(
        {
            "purpose": purpose,
            "provider_ref": provider_ref,
            "model_ref": model_ref,
            "input_ref": input_ref,
            "input_digest": input_digest,
            "attempt_number": attempt_number,
            "provider_response_id": provider_response_id,
        }
    )
    return "semantic-authority-provider-attempt:sha256:" + digest


def _responses_from_audit(
    audit: Mapping[str, Any],
    *,
    purpose: str,
    input_ref: str,
    input_digest: str,
) -> tuple[RestrictedProviderResponse, ...]:
    if purpose not in _CALL_PURPOSES:
        raise SemanticAuthorityWorkflowError("semantic_authority_audit_purpose_invalid")
    input_ref = _required_string(
        input_ref, "semantic_authority_audit_input_ref_invalid"
    )
    input_digest = _digest_string(
        input_digest, "semantic_authority_audit_input_digest_invalid"
    )
    provider_ref = _required_string(
        audit.get("provider"), "semantic_authority_audit_provider_invalid"
    )
    model_ref = _required_string(
        audit.get("model"), "semantic_authority_audit_model_invalid"
    )
    final_attempt = _positive_integer(
        audit.get("attempt_count"),
        "semantic_authority_audit_attempt_count_invalid",
    )
    attempts: list[tuple[int, str, str]] = []
    raw_failures = audit.get("attempt_failures", ())
    if isinstance(raw_failures, (str, bytes)) or not isinstance(raw_failures, Sequence):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_audit_attempt_failures_invalid"
        )
    for failure in raw_failures:
        if not isinstance(failure, Mapping):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_audit_attempt_failures_invalid"
            )
        raw_content = failure.get("raw_response_content")
        if raw_content is None:
            continue
        attempt_number = _positive_integer(
            failure.get("attempt"),
            "semantic_authority_audit_attempt_failure_number_invalid",
        )
        if attempt_number >= final_attempt:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_audit_attempt_failure_number_invalid"
            )
        attempts.append(
            (
                attempt_number,
                _required_string(
                    raw_content,
                    "semantic_authority_audit_raw_response_invalid",
                ),
                str(failure.get("response_id") or ""),
            )
        )
    attempts.append(
        (
            final_attempt,
            _required_string(
                audit.get("raw_response_content"),
                "semantic_authority_audit_raw_response_invalid",
            ),
            str(audit.get("response_id") or ""),
        )
    )
    if len({item[0] for item in attempts}) != len(attempts):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_audit_attempt_identity_duplicated"
        )
    return tuple(
        RestrictedProviderResponse.create(
            attempt_id=_provider_attempt_id(
                purpose=purpose,
                provider_ref=provider_ref,
                model_ref=model_ref,
                input_ref=input_ref,
                input_digest=input_digest,
                attempt_number=attempt_number,
                provider_response_id=response_id,
            ),
            purpose=purpose,
            provider_ref=provider_ref,
            model_ref=model_ref,
            input_ref=input_ref,
            input_digest=input_digest,
            attempt_number=attempt_number,
            content=content,
        )
        for attempt_number, content, response_id in sorted(attempts)
    )


@dataclass(frozen=True)
class RestrictedProviderAudit:
    audit_ref: str
    purpose: str
    input_ref: str
    input_digest: str
    provider_ref: str
    model_ref: str
    attempt_count: int
    provider_response_refs: tuple[str, ...]
    payload: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        purpose: str,
        input_ref: str,
        input_digest: str,
        payload: Mapping[str, Any],
        provider_responses: Sequence[RestrictedProviderResponse],
    ) -> "RestrictedProviderAudit":
        if purpose not in _CALL_PURPOSES:
            raise SemanticAuthorityWorkflowError(
                "restricted_provider_audit_purpose_invalid"
            )
        normalized_input_ref = _required_string(
            input_ref, "restricted_provider_audit_input_ref_invalid"
        )
        normalized_input_digest = _digest_string(
            input_digest, "restricted_provider_audit_input_digest_invalid"
        )
        if not isinstance(payload, Mapping):
            raise SemanticAuthorityWorkflowError(
                "restricted_provider_audit_payload_invalid"
            )
        immutable_payload = _immutable(
            payload, "restricted_provider_audit_payload_invalid"
        )
        provider_ref = _required_string(
            immutable_payload.get("provider"),
            "restricted_provider_audit_provider_invalid",
        )
        model_ref = _required_string(
            immutable_payload.get("model"),
            "restricted_provider_audit_model_invalid",
        )
        attempt_count = _positive_integer(
            immutable_payload.get("attempt_count"),
            "restricted_provider_audit_attempt_count_invalid",
        )
        if isinstance(provider_responses, (str, bytes)) or not isinstance(
            provider_responses, Sequence
        ):
            raise SemanticAuthorityWorkflowError(
                "restricted_provider_audit_responses_invalid"
            )
        responses = tuple(provider_responses)
        if not responses:
            raise SemanticAuthorityWorkflowError(
                "restricted_provider_audit_responses_invalid"
            )
        for response in responses:
            if type(response) is not RestrictedProviderResponse:
                raise SemanticAuthorityWorkflowError(
                    "restricted_provider_audit_responses_invalid"
                )
            replayed = RestrictedProviderResponse.from_dict(response.to_dict())
            if (
                replayed != response
                or response.purpose != purpose
                or response.provider_ref != provider_ref
                or response.model_ref != model_ref
                or response.input_ref != normalized_input_ref
                or response.input_digest != normalized_input_digest
            ):
                raise SemanticAuthorityWorkflowError(
                    "restricted_provider_audit_response_closure_invalid"
                )
        expected = _responses_from_audit(
            immutable_payload,
            purpose=purpose,
            input_ref=normalized_input_ref,
            input_digest=normalized_input_digest,
        )
        if expected != responses or responses[-1].attempt_number != attempt_count:
            raise SemanticAuthorityWorkflowError(
                "restricted_provider_audit_response_closure_invalid"
            )
        response_refs = tuple(item.response_ref for item in responses)
        body = {
            "purpose": purpose,
            "input_ref": normalized_input_ref,
            "input_digest": normalized_input_digest,
            "provider_ref": provider_ref,
            "model_ref": model_ref,
            "attempt_count": attempt_count,
            "provider_response_refs": response_refs,
            "payload": immutable_payload,
        }
        digest = canonical_digest(body)
        return cls(
            audit_ref="restricted-provider-audit:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        provider_responses: Sequence[RestrictedProviderResponse],
    ) -> "RestrictedProviderAudit":
        payload = _strict_mapping(
            payload,
            frozenset(cls.__dataclass_fields__),
            "restricted_provider_audit_shape_invalid",
        )
        rebuilt = cls.create(
            purpose=payload["purpose"],
            input_ref=payload["input_ref"],
            input_digest=payload["input_digest"],
            payload=payload["payload"],
            provider_responses=provider_responses,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise SemanticAuthorityWorkflowError(
                "restricted_provider_audit_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class _ProviderInvocation:
    output: Mapping[str, Any]
    responses: tuple[RestrictedProviderResponse, ...]
    final_response: RestrictedProviderResponse
    audit: RestrictedProviderAudit


def _invoke(
    llm_client: TypedSemanticAuthorityLLM,
    *,
    call_input: SemanticAuthorityCallInput,
    validator: Callable[[Mapping[str, Any]], None],
) -> _ProviderInvocation:
    required_key = _REQUIRED_OUTPUT_KEY[call_input.purpose]
    invoke_kwargs: dict[str, Any] = {
        "task": "semantic_authority_" + call_input.purpose,
        "prompt_version": _SEMANTIC_PROMPT_VERSION,
        "messages": (
            {"role": "system", "content": _PROMPTS[call_input.purpose]},
            {
                "role": "user",
                "content": json.dumps(
                    call_input.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ),
        "required_keys": (required_key,),
        "output_validator": validator,
        "model_tier": "critical",
    }
    if bool(getattr(llm_client, "supports_thinking_mode", False)):
        invoke_kwargs["thinking"] = _THINKING_MODE_BY_PURPOSE[call_input.purpose]
    result = llm_client.invoke_json(**invoke_kwargs)
    output = getattr(result, "output", None)
    audit = getattr(result, "audit", None)
    if not isinstance(output, Mapping) or not isinstance(audit, Mapping):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_provider_result_invalid"
        )
    validator(output)
    responses = _responses_from_audit(
        audit,
        purpose=call_input.purpose,
        input_ref=call_input.authority_input_ref,
        input_digest=call_input.content_digest,
    )
    provider_audit = RestrictedProviderAudit.create(
        purpose=call_input.purpose,
        input_ref=call_input.authority_input_ref,
        input_digest=call_input.content_digest,
        payload=audit,
        provider_responses=responses,
    )
    return _ProviderInvocation(
        output=MappingProxyType(dict(output)),
        responses=responses,
        final_response=responses[-1],
        audit=provider_audit,
    )


def _candidate_output_validator(
    output: Mapping[str, Any],
    *,
    projection: RestrictedExecutionProjection,
) -> None:
    _strict_mapping(
        output,
        frozenset({"candidate_claim_proposals"}),
        "candidate_claim_output_shape_invalid",
    )
    proposals = _mapping_sequence(
        output["candidate_claim_proposals"],
        "candidate_claim_output_items_invalid",
    )
    obligation_by_id = {
        item.obligation_id: item
        for item in projection.obligations
        if item.claim_kind in _CANDIDATE_CLAIM_KINDS
    }
    known_assumptions = set(projection.assumption_refs)
    known_limitations = set(projection.limitation_refs)
    proposal_identities: set[str] = set()
    for item in proposals:
        _strict_mapping(
            item,
            frozenset(
                {
                    "obligation_id",
                    "subject",
                    "factual_payload",
                    "assumption_refs",
                    "limitation_refs",
                }
            ),
            "candidate_claim_output_item_shape_invalid",
        )
        obligation_id = _required_string(
            item["obligation_id"], "candidate_claim_output_obligation_invalid"
        )
        obligation = obligation_by_id.get(obligation_id)
        if obligation is None or not obligation.eligible_candidate_evidence_refs:
            raise SemanticAuthorityWorkflowError(
                "candidate_claim_output_obligation_unknown"
            )
        _required_string(item["subject"], "candidate_claim_output_subject_invalid")
        factual_payload = item["factual_payload"]
        if not isinstance(factual_payload, Mapping) or not factual_payload:
            raise SemanticAuthorityWorkflowError(
                "candidate_claim_output_factual_payload_invalid"
            )
        _assert_restricted(factual_payload, "candidate_claim_output_forbidden_field")
        assumptions = set(
            _string_tuple(
                item["assumption_refs"],
                "candidate_claim_output_assumptions_invalid",
            )
        )
        limitations = set(
            _string_tuple(
                item["limitation_refs"],
                "candidate_claim_output_limitations_invalid",
            )
        )
        if assumptions - known_assumptions or limitations - known_limitations:
            raise SemanticAuthorityWorkflowError(
                "candidate_claim_output_reference_closure_invalid"
            )
        proposal_identity = canonical_digest(item)
        if proposal_identity in proposal_identities:
            raise SemanticAuthorityWorkflowError(
                "candidate_claim_output_proposal_duplicated"
            )
        proposal_identities.add(proposal_identity)


def _candidate_proposals_from_output(
    output: Mapping[str, Any],
    *,
    projection: RestrictedExecutionProjection,
    authority_namespace: ClaimAuthorityNamespace,
) -> tuple[CandidateClaimProposal, ...]:
    _candidate_output_validator(output, projection=projection)
    proposals = []
    obligation_by_id = {
        item.obligation_id: item
        for item in projection.obligations
        if item.claim_kind in _CANDIDATE_CLAIM_KINDS
        and item.eligible_candidate_evidence_refs
    }
    evidence_by_ref = {
        evidence.evidence_entry_ref: evidence
        for evidence in projection.aggregate_evidence
    }
    for proposal_index, item in enumerate(output["candidate_claim_proposals"]):
        obligation = obligation_by_id[item["obligation_id"]]
        support_items = []
        for evidence_ref in obligation.eligible_candidate_evidence_refs:
            evidence = evidence_by_ref[evidence_ref]
            source_claim_kind = admissible_obligation_evidence_source_claim_kind(
                obligation=obligation,
                evidence_kind=evidence.evidence_kind,
                supported_claim_kinds=evidence.supported_claim_kinds,
                maximum_claim_strength=evidence.maximum_claim_strength,
            )
            if source_claim_kind is None:
                raise SemanticAuthorityWorkflowError(
                    "candidate_claim_evidence_source_claim_missing"
                )
            support_items.append(
                CandidateEvidenceSupport.create(
                    authority_namespace=authority_namespace,
                    evidence_entry_ref=evidence_ref,
                    source_claim_kind=source_claim_kind,
                )
            )
        support = tuple(support_items)
        proposals.append(
            CandidateClaimProposal.create(
                authority_namespace=authority_namespace,
                proposal_item_ref=(
                    "candidate-proposal-item:sha256:"
                    + canonical_digest(
                        {
                            "provider_output_index": proposal_index,
                            "proposal": item,
                        }
                    )
                ),
                obligation_id=item["obligation_id"],
                subject=item["subject"],
                factual_payload=item["factual_payload"],
                evidence_support=support,
                assumption_refs=item["assumption_refs"],
                limitation_refs=item["limitation_refs"],
            )
        )
    normalized = tuple(sorted(proposals, key=lambda item: item.candidate_proposal_ref))
    if len({item.candidate_proposal_ref for item in normalized}) != len(normalized):
        raise SemanticAuthorityWorkflowError(
            "candidate_claim_output_proposal_duplicated"
        )
    return normalized


def _claim_limitation_refs_by_subject(
    checkpoint: ClaimSettlementCheckpoint,
) -> Mapping[str, tuple[str, ...]]:
    edge_by_ref = {
        edge.support_edge_ref: edge for edge in checkpoint.proposed_support_edges
    }
    result: dict[str, tuple[str, ...]] = {}
    for claim in checkpoint.proposed_claims:
        try:
            support_edges = tuple(
                edge_by_ref[edge_ref] for edge_ref in claim.support_edge_refs
            )
        except KeyError as exc:
            raise SemanticAuthorityWorkflowError(
                "claim_verification_support_closure_invalid"
            ) from exc
        result[claim.claim_ref] = tuple(
            sorted(
                {
                    *claim.limitation_refs,
                    *(
                        limitation_ref
                        for edge in support_edges
                        for limitation_ref in edge.limitation_refs
                    ),
                }
            )
        )
    return MappingProxyType(dict(sorted(result.items())))


def _claim_verification_output_validator(
    output: Mapping[str, Any],
    *,
    checkpoint: ClaimSettlementCheckpoint,
    projection: RestrictedExecutionProjection,
) -> None:
    _strict_mapping(
        output,
        frozenset({"decisions"}),
        "claim_verification_output_shape_invalid",
    )
    decisions = output["decisions"]
    if not isinstance(decisions, Mapping):
        raise SemanticAuthorityWorkflowError(
            "claim_verification_output_decisions_invalid"
        )
    expected = {item.claim_ref for item in checkpoint.proposed_claims}
    if set(decisions) != expected:
        raise SemanticAuthorityWorkflowError(
            "claim_verification_output_coverage_invalid"
        )
    projected_claims, _ = _claim_verification_projection(checkpoint, projection)
    requirement_status_by_ref = {
        item["claim_ref"]: item["evidence_requirement_status"]
        for item in projected_claims
    }
    if set(requirement_status_by_ref) != expected:
        raise SemanticAuthorityWorkflowError(
            "claim_verification_output_requirement_closure_invalid"
        )
    claim_limitations = _claim_limitation_refs_by_subject(checkpoint)
    for raw_subject, item in decisions.items():
        _required_string(raw_subject, "claim_verification_output_subject_invalid")
        _strict_mapping(
            item,
            frozenset(
                {
                    "disposition",
                    "veto_basis",
                    "reason_code",
                    "limitation_refs",
                }
            ),
            "claim_verification_output_decision_shape_invalid",
        )
        disposition = item["disposition"]
        if disposition not in {"accepted", "vetoed"}:
            raise SemanticAuthorityWorkflowError(
                "claim_verification_output_disposition_invalid"
            )
        reason = item["reason_code"]
        if reason is not None:
            _required_string(reason, "claim_verification_output_reason_invalid")
        veto_basis = item["veto_basis"]
        if veto_basis is not None and veto_basis not in VERIFICATION_VETO_BASES:
            raise SemanticAuthorityWorkflowError(
                "claim_verification_output_veto_basis_invalid"
            )
        limitations = set(
            _string_tuple(
                item["limitation_refs"],
                "claim_verification_output_limitations_invalid",
            )
        )
        if limitations - set(claim_limitations[raw_subject]):
            raise SemanticAuthorityWorkflowError(
                "claim_verification_output_limitation_closure_invalid"
            )
        if disposition == "accepted" and (
            veto_basis is not None or reason is not None or limitations
        ):
            raise SemanticAuthorityWorkflowError(
                "claim_verification_output_acceptance_invalid"
            )
        if disposition == "vetoed" and (veto_basis is None or reason is None):
            raise SemanticAuthorityWorkflowError(
                "claim_verification_output_veto_invalid"
            )
        if (
            veto_basis == "evidence_requirement_unsatisfied"
            and requirement_status_by_ref[raw_subject] == "satisfied"
        ):
            raise SemanticAuthorityWorkflowError(
                "claim_verification_output_evidence_requirement_veto_invalid"
            )


def _claim_decisions_from_output(
    output: Mapping[str, Any],
    *,
    checkpoint: ClaimSettlementCheckpoint,
    projection: RestrictedExecutionProjection,
    authority_namespace: ClaimAuthorityNamespace,
    verification_attempt: SemanticVerificationAttempt,
) -> tuple[SemanticVerificationDecision, ...]:
    _claim_verification_output_validator(
        output,
        checkpoint=checkpoint,
        projection=projection,
    )
    return tuple(
        sorted(
            (
                SemanticVerificationDecision.create(
                    authority_namespace=authority_namespace,
                    verification_attempt=verification_attempt,
                    subject_ref=subject_ref,
                    disposition=item["disposition"],
                    veto_basis=item["veto_basis"],
                    reason_code=item["reason_code"],
                    limitation_refs=item["limitation_refs"],
                )
                for subject_ref, item in output["decisions"].items()
            ),
            key=lambda item: item.subject_ref,
        )
    )


def _recommendation_output_validator(
    output: Mapping[str, Any],
    *,
    settlement: ClaimSettlement,
) -> None:
    _strict_mapping(
        output,
        frozenset({"recommendation_proposals"}),
        "recommendation_output_shape_invalid",
    )
    items = _mapping_sequence(
        output["recommendation_proposals"],
        "recommendation_output_items_invalid",
    )
    proposals, _policy_rejections = _recommendation_proposals_from_items(
        items,
        settlement=settlement,
    )
    if len({item.recommendation_proposal_ref for item in proposals}) != len(proposals):
        raise SemanticAuthorityWorkflowError(
            "recommendation_output_proposal_duplicated"
        )


def _recommendation_commitment_from_output_item(
    item: Mapping[str, Any],
    *,
    settlement: ClaimSettlement,
) -> RecommendationCommitment:
    _strict_mapping(
        item,
        frozenset(
            {
                "commitment_kind",
                "text",
                "supporting_claim_refs",
                "diagnostic_mode",
                "action_domain",
                "action_stage",
                "expected_value_kind",
                "expected_value_mode",
            }
        ),
        "recommendation_output_commitment_shape_invalid",
    )
    try:
        return RecommendationCommitment.create(
            authority_namespace=settlement.authority_namespace,
            commitment_kind=item["commitment_kind"],
            text=item["text"],
            supporting_claim_refs=item["supporting_claim_refs"],
            diagnostic_mode=item["diagnostic_mode"],
            action_domain=item["action_domain"],
            action_stage=item["action_stage"],
            expected_value_kind=item["expected_value_kind"],
            expected_value_mode=item["expected_value_mode"],
        )
    except (TypeError, ValueError) as exc:
        raise SemanticAuthorityWorkflowError(str(exc)) from exc


def _recommendation_proposal_from_output_item(
    item: Mapping[str, Any],
    *,
    settlement: ClaimSettlement,
) -> RecommendationProposal:
    _strict_mapping(
        item,
        frozenset(
            {
                "commitment_contract_version",
                "commitments",
                "supporting_claim_refs",
                "assumption_refs",
                "risk_refs",
                "action",
                "applicable_conditions",
                "expected_decision_value",
            }
        ),
        "recommendation_output_item_shape_invalid",
    )
    raw_commitments = _mapping_sequence(
        item["commitments"], "recommendation_output_commitments_invalid"
    )
    commitments = tuple(
        _recommendation_commitment_from_output_item(commitment, settlement=settlement)
        for commitment in raw_commitments
    )
    try:
        return RecommendationProposal.create(
            authority_namespace=settlement.authority_namespace,
            claim_settlement=settlement,
            supporting_claim_refs=item["supporting_claim_refs"],
            assumption_refs=item["assumption_refs"],
            risk_refs=item["risk_refs"],
            commitment_contract_version=item["commitment_contract_version"],
            commitments=commitments,
            action=item["action"],
            applicable_conditions=item["applicable_conditions"],
            expected_decision_value=item["expected_decision_value"],
        )
    except (TypeError, ValueError) as exc:
        raise SemanticAuthorityWorkflowError(str(exc)) from exc


def _recommendation_proposals_from_items(
    items: Sequence[Mapping[str, Any]],
    *,
    settlement: ClaimSettlement,
) -> tuple[tuple[RecommendationProposal, ...], tuple[Mapping[str, Any], ...]]:
    proposals: list[RecommendationProposal] = []
    policy_rejections: list[Mapping[str, Any]] = []
    for index, item in enumerate(items):
        try:
            proposals.append(
                _recommendation_proposal_from_output_item(
                    item,
                    settlement=settlement,
                )
            )
        except SemanticAuthorityWorkflowError as exc:
            reason_code = str(exc).strip()
            if reason_code not in _OPTIONAL_RECOMMENDATION_POLICY_REJECTIONS:
                raise
            policy_rejections.append(
                MappingProxyType(
                    {
                        "proposal_index": index,
                        "reason_code": reason_code,
                        "disposition": "rejected",
                    }
                )
            )
    normalized = tuple(
        sorted(proposals, key=lambda item: item.recommendation_proposal_ref)
    )
    return normalized, tuple(policy_rejections)


def _recommendation_proposals_from_output(
    output: Mapping[str, Any],
    *,
    settlement: ClaimSettlement,
) -> tuple[tuple[RecommendationProposal, ...], tuple[Mapping[str, Any], ...]]:
    _recommendation_output_validator(output, settlement=settlement)
    proposals, policy_rejections = _recommendation_proposals_from_items(
        _mapping_sequence(
            output["recommendation_proposals"],
            "recommendation_output_items_invalid",
        ),
        settlement=settlement,
    )
    if len({item.recommendation_proposal_ref for item in proposals}) != len(proposals):
        raise SemanticAuthorityWorkflowError(
            "recommendation_output_proposal_duplicated"
        )
    return proposals, policy_rejections


def _provider_audit_with_policy_rejections(
    audit: RestrictedProviderAudit,
    *,
    provider_responses: Sequence[RestrictedProviderResponse],
    policy_rejections: Sequence[Mapping[str, Any]],
) -> RestrictedProviderAudit:
    if not policy_rejections:
        return audit
    payload = dict(canonical_value(audit.payload))
    payload["policy_rejections"] = canonical_value(policy_rejections)
    return RestrictedProviderAudit.create(
        purpose=audit.purpose,
        input_ref=audit.input_ref,
        input_digest=audit.input_digest,
        payload=payload,
        provider_responses=provider_responses,
    )


def _recommendation_public_text_violation(
    proposal: RecommendationProposal,
    *,
    settlement: ClaimSettlement,
) -> str | None:
    restricted_claim_refs = frozenset(
        claim.claim_ref for claim in settlement.accepted_claims
    )
    public_text = (
        proposal.action,
        *proposal.applicable_conditions,
        proposal.expected_decision_value,
        *(commitment.text for commitment in proposal.commitments),
    )
    if any(
        claim_ref in text
        for text in public_text
        for claim_ref in restricted_claim_refs
    ):
        return "public_recommendation_internal_ref_forbidden"
    return None


def _recommendation_verification_output_validator(
    output: Mapping[str, Any],
    *,
    proposal: RecommendationProposal,
    known_limitation_refs: Sequence[str],
) -> None:
    _strict_mapping(
        output,
        frozenset({"decision"}),
        "recommendation_verification_output_shape_invalid",
    )
    decision = _strict_mapping(
        output["decision"],
        frozenset(
            {
                "subject_ref",
                "disposition",
                "veto_basis",
                "reason_code",
                "limitation_refs",
                "verified_commitment_refs",
            }
        ),
        "recommendation_verification_output_decision_shape_invalid",
    )
    if decision["subject_ref"] != proposal.recommendation_proposal_ref:
        raise SemanticAuthorityWorkflowError(
            "recommendation_verification_output_subject_closure_invalid"
        )
    disposition = decision["disposition"]
    if disposition not in {"accepted", "vetoed"}:
        raise SemanticAuthorityWorkflowError(
            "recommendation_verification_output_disposition_invalid"
        )
    reason = decision["reason_code"]
    if reason is not None:
        _required_string(reason, "recommendation_verification_output_reason_invalid")
    veto_basis = decision["veto_basis"]
    if veto_basis is not None and veto_basis not in VERIFICATION_VETO_BASES:
        raise SemanticAuthorityWorkflowError(
            "recommendation_verification_output_veto_basis_invalid"
        )
    limitations = set(
        _string_tuple(
            decision["limitation_refs"],
            "recommendation_verification_output_limitations_invalid",
        )
    )
    if limitations - set(known_limitation_refs):
        raise SemanticAuthorityWorkflowError(
            "recommendation_verification_output_limitation_closure_invalid"
        )
    verified_commitments = set(
        _string_tuple(
            decision["verified_commitment_refs"],
            "recommendation_verification_output_commitments_invalid",
        )
    )
    known_commitments = set(proposal.recommendation_commitment_refs)
    if verified_commitments - known_commitments:
        raise SemanticAuthorityWorkflowError(
            "recommendation_verification_output_commitment_closure_invalid"
        )
    if disposition == "accepted" and (
        veto_basis is not None
        or reason is not None
        or limitations
        or verified_commitments != known_commitments
    ):
        raise SemanticAuthorityWorkflowError(
            "recommendation_verification_output_acceptance_invalid"
        )
    if disposition == "vetoed" and (veto_basis is None or reason is None):
        raise SemanticAuthorityWorkflowError(
            "recommendation_verification_output_veto_invalid"
        )


def _candidate_call_input(
    projection: RestrictedExecutionProjection,
) -> SemanticAuthorityCallInput:
    obligations = tuple(
        item
        for item in projection.obligations
        if item.claim_kind in _CANDIDATE_CLAIM_KINDS
        and item.eligible_candidate_evidence_refs
    )
    eligible_evidence_refs = tuple(
        sorted(
            {
                evidence_ref
                for obligation in obligations
                for evidence_ref in obligation.eligible_candidate_evidence_refs
            }
        )
    )
    evidence_by_ref = {
        item.evidence_entry_ref: item for item in projection.aggregate_evidence
    }
    if (
        not obligations
        or not eligible_evidence_refs
        or set(eligible_evidence_refs) - set(evidence_by_ref)
    ):
        raise SemanticAuthorityWorkflowError(
            "candidate_claim_provider_projection_invalid"
        )
    relevant_limitation_refs = tuple(
        sorted(
            {
                limitation_ref
                for evidence_ref in eligible_evidence_refs
                for limitation_ref in evidence_by_ref[evidence_ref].limitation_refs
            }
        )
    )
    payload = {
        "obligations": tuple(
            {
                key: value
                for key, value in obligation.to_dict().items()
                if key != "content_digest"
            }
            for obligation in obligations
        ),
        "aggregate_evidence": _aggregate_evidence_projection(
            projection,
            eligible_evidence_refs,
        ),
        "assumption_refs": projection.assumption_refs,
        "limitation_refs": relevant_limitation_refs,
    }
    return SemanticAuthorityCallInput.create(
        purpose="candidate_claim_proposal",
        authority_input_ref=projection.projection_ref,
        payload=payload,
    )


def _aggregate_evidence_projection(
    projection: RestrictedExecutionProjection,
    evidence_refs: Sequence[str] | None = None,
) -> tuple[Mapping[str, Any], ...]:
    selected_refs = (
        {item.evidence_entry_ref for item in projection.aggregate_evidence}
        if evidence_refs is None
        else set(evidence_refs)
    )
    evidence_by_ref = {
        item.evidence_entry_ref: item for item in projection.aggregate_evidence
    }
    if selected_refs - set(evidence_by_ref):
        raise SemanticAuthorityWorkflowError(
            "semantic_projection_evidence_closure_invalid"
        )
    return tuple(
        {
            **item.to_dict(),
            "observation_facts": _lossless_semantic_value(item.observation_facts),
        }
        for item in projection.aggregate_evidence
        if item.evidence_entry_ref in selected_refs
    )


def _claim_key_provider_projection(claim_key: ClaimKey) -> Mapping[str, Any]:
    """Expose business claim identity without replay-only authority metadata."""

    return {
        "goal_id": claim_key.goal_id,
        "claim_kind": claim_key.claim_kind,
        "subject": claim_key.subject,
        "metric_ref": claim_key.metric_ref,
        "target_window_ref": claim_key.target_window_ref,
        "baseline_window_ref": claim_key.baseline_window_ref,
        "scope": claim_key.scope,
        "grain": claim_key.grain,
        "dimension_path": claim_key.dimension_path,
    }


def _support_source_provider_projection(
    edge: SupportEdge,
) -> Mapping[str, Any]:
    """Project only the evidence semantics the verifier must judge."""

    return {
        "kind": edge.kind,
        "source_type": edge.source_type,
        "source_ref": edge.source_ref,
        "source_epistemic_class": edge.source_epistemic_class,
        "source_publication_ceiling": edge.source_publication_ceiling.to_dict(),
        "limitation_refs": edge.limitation_refs,
    }


def _recommendation_evidence_projection(
    aggregate_evidence: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Keep recommendation facts while dropping verifier-only evidence metadata."""

    return tuple(
        {
            "evidence_entry_ref": item["evidence_entry_ref"],
            "evidence_kind": item["evidence_kind"],
            "dimension_path": item["dimension_path"],
            "window_refs": item["window_refs"],
            "limitation_refs": item["limitation_refs"],
            "observation_facts": item["observation_facts"],
        }
        for item in aggregate_evidence
    )


def _claim_authority_projection(
    *,
    claims: Sequence[ClaimRevision],
    claim_keys: Sequence[ClaimKey],
    support_edges: Sequence[SupportEdge],
    obligation_by_claim_ref: Mapping[str, str],
    projection: RestrictedExecutionProjection,
    projection_mode: str = "claim_verification",
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    if projection_mode not in {
        "claim_verification",
        "recommendation_proposal",
        "recommendation_verification",
    }:
        raise SemanticAuthorityWorkflowError(
            "semantic_projection_mode_invalid"
        )
    edge_by_ref = {edge.support_edge_ref: edge for edge in support_edges}
    key_by_ref = {item.claim_key: item for item in claim_keys}
    evidence_by_ref = {
        item.evidence_entry_ref: item for item in projection.aggregate_evidence
    }
    obligation_by_id = {item.obligation_id: item for item in projection.obligations}
    claim_refs = {claim.claim_ref for claim in claims}
    if set(obligation_by_claim_ref) != claim_refs:
        raise SemanticAuthorityWorkflowError(
            "semantic_projection_obligation_membership_invalid"
        )

    projected_claims: list[Mapping[str, Any]] = []
    referenced_evidence_refs: set[str] = set()
    for claim in claims:
        obligation_id = obligation_by_claim_ref.get(claim.claim_ref)
        claim_key = key_by_ref.get(claim.claim_key)
        obligation = obligation_by_id.get(str(obligation_id))
        if obligation_id is None or claim_key is None or obligation is None:
            raise SemanticAuthorityWorkflowError(
                "semantic_projection_claim_authority_closure_invalid"
            )
        try:
            claim_support_edges = tuple(
                edge_by_ref[edge_ref] for edge_ref in claim.support_edge_refs
            )
        except KeyError as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_projection_claim_authority_closure_invalid"
            ) from exc
        evidence_refs = tuple(
            sorted(
                {
                    edge.source_ref
                    for edge in claim_support_edges
                    if edge.source_type == "evidence"
                }
            )
        )
        assumption_refs = tuple(
            sorted(
                {
                    edge.source_ref
                    for edge in claim_support_edges
                    if edge.source_type == "assumption"
                }
            )
        )
        if any(ref not in evidence_by_ref for ref in evidence_refs):
            raise SemanticAuthorityWorkflowError(
                "semantic_projection_evidence_closure_invalid"
            )
        referenced_evidence_refs.update(evidence_refs)
        bound_evidence_kinds = tuple(
            sorted({evidence_by_ref[ref].evidence_kind for ref in evidence_refs})
        )
        requirement = obligation.evidence_requirement
        if requirement.operator != "any_of":
            raise SemanticAuthorityWorkflowError(
                "semantic_projection_evidence_requirement_operator_invalid"
            )
        requirement_status = (
            "satisfied"
            if set(bound_evidence_kinds).intersection(requirement.evidence_kinds)
            else "unsatisfied"
        )
        if requirement_status != "satisfied":
            raise SemanticAuthorityWorkflowError(
                "semantic_projection_evidence_requirement_unsatisfied"
            )

        factual_payload = canonical_value(claim.factual_payload)

        projected_claim = {
            "claim_ref": claim.claim_ref,
            "claim_key": _claim_key_provider_projection(claim_key),
            "claim_class": claim.claim_class,
            "publication_ceiling": claim.publication_ceiling.to_dict(),
            "limitation_refs": claim.limitation_refs,
            "dependency_claim_refs": claim.dependency_claim_refs,
            "obligation_id": obligation_id,
            "evidence_entry_refs": evidence_refs,
            "bound_evidence_kinds": bound_evidence_kinds,
            "assumption_refs": assumption_refs,
            "factual_payload": _lossless_semantic_value(factual_payload),
        }
        if projection_mode != "recommendation_proposal":
            projected_claim = {
                **projected_claim,
                "evidence_requirement_status": requirement_status,
                "support_sources": tuple(
                    _support_source_provider_projection(edge)
                    for edge in claim_support_edges
                ),
            }
        projected_claims.append(projected_claim)

    return tuple(projected_claims), _aggregate_evidence_projection(
        projection, tuple(sorted(referenced_evidence_refs))
    )


def _claim_verification_projection(
    checkpoint: ClaimSettlementCheckpoint,
    projection: RestrictedExecutionProjection,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    obligation_by_claim_ref: dict[str, str] = {}
    for basis in checkpoint.obligation_basis:
        for claim_ref in basis.proposed_claim_refs:
            if claim_ref in obligation_by_claim_ref:
                raise SemanticAuthorityWorkflowError(
                    "claim_verification_obligation_membership_invalid"
                )
            obligation_by_claim_ref[claim_ref] = basis.obligation_id
    return _claim_authority_projection(
        claims=checkpoint.proposed_claims,
        claim_keys=checkpoint.proposed_claim_keys,
        support_edges=checkpoint.proposed_support_edges,
        obligation_by_claim_ref=obligation_by_claim_ref,
        projection=projection,
        projection_mode="claim_verification",
    )


def _claim_call_input(
    checkpoint: ClaimSettlementCheckpoint,
    projection: RestrictedExecutionProjection,
) -> SemanticAuthorityCallInput:
    proposed_claims, aggregate_evidence = _claim_verification_projection(
        checkpoint, projection
    )
    obligation_ids = {item["obligation_id"] for item in proposed_claims}
    obligations = tuple(
        item.to_dict()
        for item in projection.obligations
        if item.obligation_id in obligation_ids
    )
    if {item["obligation_id"] for item in obligations} != obligation_ids:
        raise SemanticAuthorityWorkflowError(
            "claim_verification_obligation_closure_invalid"
        )
    return SemanticAuthorityCallInput.create(
        purpose="claim_verification",
        authority_input_ref=checkpoint.checkpoint_ref,
        payload={
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "checkpoint_digest": checkpoint.content_digest,
            "proposed_claims": proposed_claims,
            "obligations": obligations,
            "aggregate_evidence": aggregate_evidence,
        },
    )


def _accepted_claim_obligation_membership(
    settlement: ClaimSettlement,
) -> Mapping[str, str]:
    obligation_by_claim_ref: dict[str, str] = {}
    for coverage in settlement.obligation_coverage:
        for claim_ref in coverage.claim_refs:
            if claim_ref in obligation_by_claim_ref:
                raise SemanticAuthorityWorkflowError(
                    "recommendation_claim_obligation_membership_invalid"
                )
            obligation_by_claim_ref[claim_ref] = coverage.obligation_id
    if set(obligation_by_claim_ref) != {
        item.claim_ref for item in settlement.accepted_claims
    }:
        raise SemanticAuthorityWorkflowError(
            "recommendation_claim_obligation_membership_invalid"
        )
    return MappingProxyType(dict(sorted(obligation_by_claim_ref.items())))


def _accepted_claim_dependency_closure(
    settlement: ClaimSettlement,
    claim_refs: Sequence[str],
) -> tuple[str, ...]:
    claim_by_ref = {item.claim_ref: item for item in settlement.accepted_claims}
    pending = list(claim_refs)
    closure: set[str] = set()
    while pending:
        claim_ref = pending.pop()
        claim = claim_by_ref.get(claim_ref)
        if claim is None:
            raise SemanticAuthorityWorkflowError(
                "recommendation_supporting_claim_closure_invalid"
            )
        if claim_ref in closure:
            continue
        closure.add(claim_ref)
        pending.extend(claim.dependency_claim_refs)
    return tuple(sorted(closure))


def _accepted_claim_projection(
    settlement: ClaimSettlement,
    projection: RestrictedExecutionProjection,
    *,
    claim_refs: Sequence[str] | None = None,
    projection_mode: str = "recommendation_verification",
) -> tuple[
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
]:
    selected_refs = (
        tuple(item.claim_ref for item in settlement.accepted_claims)
        if claim_refs is None
        else tuple(claim_refs)
    )
    selected = set(selected_refs)
    claims = tuple(
        item for item in settlement.accepted_claims if item.claim_ref in selected
    )
    if (
        len(selected) != len(selected_refs)
        or {item.claim_ref for item in claims} != selected
    ):
        raise SemanticAuthorityWorkflowError(
            "recommendation_supporting_claim_closure_invalid"
        )
    all_membership = _accepted_claim_obligation_membership(settlement)
    projected_claims, aggregate_evidence = _claim_authority_projection(
        claims=claims,
        claim_keys=settlement.accepted_claim_keys,
        support_edges=settlement.accepted_support_edges,
        obligation_by_claim_ref={ref: all_membership[ref] for ref in selected_refs},
        projection=projection,
        projection_mode=projection_mode,
    )
    claim_by_ref = {item.claim_ref: item for item in claims}
    authorization_catalog: dict[str, Mapping[str, Any]] = {}
    provider_claims: list[Mapping[str, Any]] = []
    for item in projected_claims:
        authorization = canonical_value(
            recommendation_authorization_for_ceiling(
                claim_by_ref[str(item["claim_ref"])].publication_ceiling
            )
        )
        authorization_ref = (
            "recommendation-authorization:sha256:"
            + canonical_digest(authorization)
        )
        authorization_catalog[authorization_ref] = authorization
        provider_claims.append(
            {
                **item,
                "recommendation_authorization_ref": authorization_ref,
            }
        )
    return (
        tuple(provider_claims),
        aggregate_evidence,
        tuple(
            {
                "recommendation_authorization_ref": authorization_ref,
                "authorization": authorization_catalog[authorization_ref],
            }
            for authorization_ref in sorted(authorization_catalog)
        ),
    )


def _recommendation_call_input(
    settlement: ClaimSettlement,
    projection: RestrictedExecutionProjection,
) -> SemanticAuthorityCallInput:
    (
        verified_claims,
        aggregate_evidence,
        recommendation_authorization_catalog,
    ) = _accepted_claim_projection(
        settlement,
        projection,
        projection_mode="recommendation_proposal",
    )
    return SemanticAuthorityCallInput.create(
        purpose="recommendation_proposal",
        authority_input_ref=settlement.claim_graph.claim_graph_ref,
        payload={
            "claim_graph_ref": settlement.claim_graph.claim_graph_ref,
            "claim_graph_digest": settlement.claim_graph.content_digest,
            "verified_claims": verified_claims,
            "recommendation_authorization_catalog": (
                recommendation_authorization_catalog
            ),
            "verified_evidence_context": _recommendation_evidence_projection(
                aggregate_evidence
            ),
            "obligation_coverage": tuple(
                item.to_dict() for item in settlement.obligation_coverage
            ),
            "assumption_refs": settlement.claim_graph.assumption_refs,
            "limitation_refs": settlement.claim_graph.limitation_refs,
        },
    )


def _recommendation_verification_call_input(
    settlement: ClaimSettlement,
    proposal: RecommendationProposal,
    projection: RestrictedExecutionProjection,
) -> SemanticAuthorityCallInput:
    supporting_refs = _accepted_claim_dependency_closure(
        settlement, proposal.supporting_claim_refs
    )
    (
        supporting_claims,
        aggregate_evidence,
        recommendation_authorization_catalog,
    ) = _accepted_claim_projection(
        settlement,
        projection,
        claim_refs=supporting_refs,
        projection_mode="recommendation_verification",
    )
    relevant_refs = set(supporting_refs)
    return SemanticAuthorityCallInput.create(
        purpose="recommendation_verification",
        authority_input_ref=settlement.claim_graph.claim_graph_ref,
        payload={
            "claim_graph_ref": settlement.claim_graph.claim_graph_ref,
            "claim_graph_digest": settlement.claim_graph.content_digest,
            "recommendation_proposal": proposal.to_dict(),
            "supporting_claims": supporting_claims,
            "recommendation_authorization_catalog": (
                recommendation_authorization_catalog
            ),
            "aggregate_evidence": aggregate_evidence,
            "obligation_coverage": tuple(
                item.to_dict()
                for item in settlement.obligation_coverage
                if relevant_refs.intersection(item.claim_refs)
            ),
            "assumption_refs": settlement.claim_graph.assumption_refs,
            "limitation_refs": settlement.claim_graph.limitation_refs,
        },
    )


def _call_input_from_audit(
    audit: RestrictedProviderAudit,
) -> SemanticAuthorityCallInput:
    if audit.payload.get("prompt_version") != _SEMANTIC_PROMPT_VERSION:
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_audit_prompt_version_invalid"
        )
    raw_messages = audit.payload.get("messages")
    if (
        isinstance(raw_messages, (str, bytes))
        or not isinstance(raw_messages, Sequence)
        or len(raw_messages) != 2
    ):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_audit_messages_invalid"
        )
    system_message, user_message = raw_messages
    if (
        not isinstance(system_message, Mapping)
        or not isinstance(user_message, Mapping)
        or canonical_value(system_message)
        != {"role": "system", "content": _PROMPTS[audit.purpose]}
        or set(user_message) != {"role", "content"}
        or user_message.get("role") != "user"
        or not isinstance(user_message.get("content"), str)
    ):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_audit_messages_invalid"
        )
    try:
        raw_call_input = json.loads(str(user_message["content"]))
    except json.JSONDecodeError as exc:
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_audit_messages_invalid"
        ) from exc
    if not isinstance(raw_call_input, Mapping):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_audit_messages_invalid"
        )
    raw_call_input = _strict_mapping(
        raw_call_input,
        frozenset(SemanticAuthorityCallInput.__dataclass_fields__),
        "semantic_authority_result_provider_audit_messages_invalid",
    )
    rebuilt = SemanticAuthorityCallInput.create(
        purpose=raw_call_input["purpose"],
        authority_input_ref=raw_call_input["authority_input_ref"],
        payload=raw_call_input["payload"],
    )
    if (
        rebuilt.to_dict() != canonical_value(raw_call_input)
        or rebuilt.purpose != audit.purpose
        or rebuilt.authority_input_ref != audit.input_ref
        or rebuilt.content_digest != audit.input_digest
    ):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_audit_messages_invalid"
        )
    return rebuilt


def _validate_attempt_response(
    attempt: SemanticVerificationAttempt,
    response_by_ref: Mapping[str, RestrictedProviderResponse],
) -> None:
    response = response_by_ref.get(attempt.raw_provider_response_ref)
    expected_purpose = (
        "claim_verification"
        if attempt.purpose == "claim_settlement"
        else "recommendation_verification"
    )
    if (
        response is None
        or response.purpose != expected_purpose
        or response.provider_ref != attempt.provider_ref
        or response.model_ref != attempt.model_ref
        or response.input_ref != attempt.authority_input_ref
        or response.input_digest != attempt.input_digest
        or response.attempt_number != attempt.attempt_number
        or response.content_digest != attempt.raw_provider_response_digest
    ):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_verification_response_closure_invalid"
        )


def _validate_result_provider_closure(
    *,
    projection: RestrictedExecutionProjection,
    checkpoint: ClaimSettlementCheckpoint,
    claim_attempt: SemanticVerificationAttempt | None,
    settlement: ClaimSettlement,
    recommendation_proposals: Sequence[RecommendationProposal],
    recommendation_attempts: Sequence[SemanticVerificationAttempt],
    provider_responses: Sequence[RestrictedProviderResponse],
    provider_audits: Sequence[RestrictedProviderAudit],
) -> None:
    expected_inputs: list[SemanticAuthorityCallInput] = []
    if projection.has_candidate_authority:
        expected_inputs.append(_candidate_call_input(projection))
    if checkpoint.proposed_claims:
        expected_inputs.append(_claim_call_input(checkpoint, projection))
    if settlement.claim_graph.authority_mode == "claim_bearing":
        expected_inputs.append(_recommendation_call_input(settlement, projection))
        expected_inputs.extend(
            _recommendation_verification_call_input(settlement, proposal, projection)
            for proposal in recommendation_proposals
        )
    replayed_inputs = tuple(_call_input_from_audit(item) for item in provider_audits)
    if tuple(expected_inputs) != replayed_inputs:
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_audit_call_closure_invalid"
        )
    response_by_ref = {item.response_ref: item for item in provider_responses}
    if claim_attempt is None:
        if checkpoint.proposed_claims:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_claim_attempt_missing"
            )
    else:
        _validate_attempt_response(claim_attempt, response_by_ref)
    for attempt in recommendation_attempts:
        _validate_attempt_response(attempt, response_by_ref)


def _validate_typed_result_provider_identity(
    *,
    projection: RestrictedExecutionProjection,
    checkpoint: ClaimSettlementCheckpoint,
    claim_attempt: SemanticVerificationAttempt | None,
    settlement: ClaimSettlement,
    recommendation_proposals: Sequence[RecommendationProposal],
    recommendation_attempts: Sequence[SemanticVerificationAttempt],
    provider_responses: Sequence[RestrictedProviderResponse],
    provider_audits: Sequence[RestrictedProviderAudit],
) -> None:
    responses = tuple(provider_responses)
    audits = tuple(provider_audits)
    if any(type(item) is not RestrictedProviderResponse for item in responses):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_responses_invalid"
        )
    response_by_ref = {item.response_ref: item for item in responses}
    if len(response_by_ref) != len(responses):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_responses_invalid"
        )
    if any(type(item) is not RestrictedProviderAudit for item in audits) or len(
        {item.audit_ref for item in audits}
    ) != len(audits):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_audits_invalid"
        )

    expected_calls: list[tuple[str, str]] = []
    if projection.has_candidate_authority:
        expected_calls.append(("candidate_claim_proposal", projection.projection_ref))
    if checkpoint.proposed_claims:
        expected_calls.append(("claim_verification", checkpoint.checkpoint_ref))
    if settlement.claim_graph.authority_mode == "claim_bearing":
        expected_calls.append(
            ("recommendation_proposal", settlement.claim_graph.claim_graph_ref)
        )
        expected_calls.extend(
            (
                "recommendation_verification",
                settlement.claim_graph.claim_graph_ref,
            )
            for _ in recommendation_proposals
        )
    if tuple((item.purpose, item.input_ref) for item in audits) != tuple(
        expected_calls
    ):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_audit_call_closure_invalid"
        )

    owned_response_refs: set[str] = set()
    for audit in audits:
        refs = audit.provider_response_refs
        if not refs or len(set(refs)) != len(refs) or set(refs) & owned_response_refs:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_provider_audit_closure_invalid"
            )
        try:
            audit_responses = tuple(response_by_ref[ref] for ref in refs)
        except KeyError as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_provider_audit_closure_invalid"
            ) from exc
        if (
            any(
                response.purpose != audit.purpose
                or response.provider_ref != audit.provider_ref
                or response.model_ref != audit.model_ref
                or response.input_ref != audit.input_ref
                or response.input_digest != audit.input_digest
                for response in audit_responses
            )
            or audit_responses[-1].attempt_number != audit.attempt_count
        ):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_provider_audit_closure_invalid"
            )
        owned_response_refs.update(refs)
    if owned_response_refs != set(response_by_ref):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_provider_audit_closure_invalid"
        )
    if claim_attempt is None:
        if checkpoint.proposed_claims:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_claim_attempt_missing"
            )
    else:
        _validate_attempt_response(claim_attempt, response_by_ref)
    for attempt in recommendation_attempts:
        _validate_attempt_response(attempt, response_by_ref)


@dataclass(frozen=True)
class SemanticAuthorityResult:
    result_ref: str
    projection: RestrictedExecutionProjection
    candidate_proposals: tuple[CandidateClaimProposal, ...]
    checkpoint: ClaimSettlementCheckpoint
    claim_verification_attempt: SemanticVerificationAttempt | None
    claim_verification_decisions: tuple[SemanticVerificationDecision, ...]
    settlement: ClaimSettlement
    recommendation_proposals: tuple[RecommendationProposal, ...]
    recommendation_verification_attempts: tuple[SemanticVerificationAttempt, ...]
    recommendation_verification_decisions: tuple[SemanticVerificationDecision, ...]
    recommendations: tuple[RecommendationRecord, ...]
    authority_bundle_inputs: AuthorityBundleInputs
    provider_responses: tuple[RestrictedProviderResponse, ...]
    provider_audits: tuple[RestrictedProviderAudit, ...]
    content_digest: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SemanticAuthorityResult":
        payload = _strict_mapping(
            payload,
            frozenset(cls.__dataclass_fields__),
            "semantic_authority_result_shape_invalid",
        )
        raw_inputs = payload["authority_bundle_inputs"]
        if not isinstance(raw_inputs, Mapping):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_inputs_invalid"
            )
        try:
            authority_inputs = AuthorityBundleInputs.from_dict(raw_inputs)
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_inputs_invalid"
            ) from exc
        execution = authority_inputs.execution_result
        namespace = authority_inputs.authority_namespace

        raw_projection = payload["projection"]
        if not isinstance(raw_projection, Mapping):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_projection_invalid"
            )
        try:
            projection = RestrictedExecutionProjection.from_dict(
                raw_projection,
                execution_result=execution,
            )
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_projection_invalid"
            ) from exc

        raw_candidate_proposals = _mapping_sequence(
            payload["candidate_proposals"],
            "semantic_authority_result_candidate_proposals_invalid",
        )
        try:
            candidate_proposals = tuple(
                CandidateClaimProposal.from_dict(item, authority_namespace=namespace)
                for item in raw_candidate_proposals
            )
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_candidate_proposals_invalid"
            ) from exc
        if len({item.candidate_proposal_ref for item in candidate_proposals}) != len(
            candidate_proposals
        ):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_candidate_proposals_invalid"
            )

        raw_checkpoint = payload["checkpoint"]
        if not isinstance(raw_checkpoint, Mapping):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_checkpoint_invalid"
            )
        try:
            checkpoint = ClaimSettlementCheckpoint.from_dict(raw_checkpoint)
            rebuilt_checkpoint = prepare_claim_settlement(
                execution,
                authority_namespace=namespace,
                candidate_proposals=candidate_proposals,
            )
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_checkpoint_invalid"
            ) from exc
        if checkpoint != rebuilt_checkpoint:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_checkpoint_invalid"
            )

        raw_claim_attempt = payload["claim_verification_attempt"]
        if raw_claim_attempt is None:
            claim_attempt = None
        elif isinstance(raw_claim_attempt, Mapping):
            try:
                claim_attempt = SemanticVerificationAttempt.from_dict(
                    raw_claim_attempt, authority_namespace=namespace
                )
            except (TypeError, ValueError) as exc:
                raise SemanticAuthorityWorkflowError(
                    "semantic_authority_result_claim_attempt_invalid"
                ) from exc
        else:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_claim_attempt_invalid"
            )
        raw_claim_decisions = _mapping_sequence(
            payload["claim_verification_decisions"],
            "semantic_authority_result_claim_decisions_invalid",
        )
        if claim_attempt is None and raw_claim_decisions:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_claim_decisions_invalid"
            )
        try:
            claim_decisions = (
                tuple(
                    SemanticVerificationDecision.from_dict(
                        item,
                        authority_namespace=namespace,
                        verification_attempt=claim_attempt,
                    )
                    for item in raw_claim_decisions
                )
                if claim_attempt is not None
                else ()
            )
            rebuilt_settlement = settle_claim_checkpoint(
                checkpoint,
                verification_attempt=claim_attempt,
                verification_decisions=claim_decisions,
            )
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_claim_settlement_invalid"
            ) from exc
        raw_settlement = payload["settlement"]
        if not isinstance(raw_settlement, Mapping):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_claim_settlement_invalid"
            )
        try:
            settlement = ClaimSettlement.from_dict(raw_settlement)
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_claim_settlement_invalid"
            ) from exc
        if (
            settlement != rebuilt_settlement
            or settlement != authority_inputs.claim_settlement
            or checkpoint != settlement.checkpoint
        ):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_claim_settlement_invalid"
            )

        raw_recommendation_proposals = _mapping_sequence(
            payload["recommendation_proposals"],
            "semantic_authority_result_recommendation_proposals_invalid",
        )
        try:
            recommendation_proposals = tuple(
                RecommendationProposal.from_dict(
                    item,
                    authority_namespace=namespace,
                    claim_settlement=settlement,
                )
                for item in raw_recommendation_proposals
            )
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_recommendation_proposals_invalid"
            ) from exc
        proposal_by_ref = {
            item.recommendation_proposal_ref: item for item in recommendation_proposals
        }
        if len(proposal_by_ref) != len(recommendation_proposals):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_recommendation_proposals_invalid"
            )

        raw_recommendation_attempts = _mapping_sequence(
            payload["recommendation_verification_attempts"],
            "semantic_authority_result_recommendation_attempts_invalid",
        )
        try:
            recommendation_attempts = tuple(
                SemanticVerificationAttempt.from_dict(
                    item, authority_namespace=namespace
                )
                for item in raw_recommendation_attempts
            )
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_recommendation_attempts_invalid"
            ) from exc
        attempt_by_ref = {
            item.verification_attempt_ref: item for item in recommendation_attempts
        }
        if len(attempt_by_ref) != len(recommendation_attempts):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_recommendation_attempts_invalid"
            )
        raw_recommendation_decisions = _mapping_sequence(
            payload["recommendation_verification_decisions"],
            "semantic_authority_result_recommendation_decisions_invalid",
        )
        recommendation_decisions_list = []
        try:
            for item in raw_recommendation_decisions:
                attempt = attempt_by_ref.get(str(item.get("verification_attempt_ref")))
                if attempt is None:
                    raise SemanticAuthorityWorkflowError(
                        "semantic_authority_result_recommendation_decisions_invalid"
                    )
                recommendation_decisions_list.append(
                    SemanticVerificationDecision.from_dict(
                        item,
                        authority_namespace=namespace,
                        verification_attempt=attempt,
                    )
                )
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_recommendation_decisions_invalid"
            ) from exc
        recommendation_decisions = tuple(recommendation_decisions_list)
        decision_by_subject = {
            item.subject_ref: item for item in recommendation_decisions
        }
        attempt_by_subject = {
            item.subject_refs[0]: item
            for item in recommendation_attempts
            if item.purpose == "recommendation" and len(item.subject_refs) == 1
        }
        if (
            len(decision_by_subject) != len(recommendation_decisions)
            or len(attempt_by_subject) != len(recommendation_attempts)
            or set(proposal_by_ref) != set(attempt_by_subject)
            or set(proposal_by_ref) != set(decision_by_subject)
        ):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_recommendation_verification_closure_invalid"
            )
        for proposal_ref, attempt in attempt_by_subject.items():
            decision = decision_by_subject[proposal_ref]
            if (
                decision.verification_attempt_ref != attempt.verification_attempt_ref
                or attempt.authority_input_ref != settlement.claim_graph.claim_graph_ref
                or attempt.authority_input_digest
                != settlement.claim_graph.content_digest
            ):
                raise SemanticAuthorityWorkflowError(
                    "semantic_authority_result_recommendation_verification_closure_invalid"
                )

        expected_recommendations = tuple(
            sorted(
                (
                    RecommendationRecord.verify(
                        authority_namespace=namespace,
                        proposal=proposal_by_ref[ref],
                        verification_attempt=attempt_by_subject[ref],
                        verification_decision=decision_by_subject[ref],
                        claim_settlement=settlement,
                    )
                    for ref in sorted(proposal_by_ref)
                    if decision_by_subject[ref].disposition == "accepted"
                ),
                key=lambda item: item.recommendation_ref,
            )
        )
        raw_recommendations = _mapping_sequence(
            payload["recommendations"],
            "semantic_authority_result_recommendations_invalid",
        )
        try:
            recommendations = tuple(
                RecommendationRecord.from_dict(
                    item,
                    authority_namespace=namespace,
                    claim_settlement=settlement,
                )
                for item in raw_recommendations
            )
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_recommendations_invalid"
            ) from exc
        if (
            recommendations != expected_recommendations
            or recommendations != authority_inputs.recommendations
        ):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_recommendations_invalid"
            )

        raw_responses = _mapping_sequence(
            payload["provider_responses"],
            "semantic_authority_result_provider_responses_invalid",
        )
        try:
            provider_responses = tuple(
                RestrictedProviderResponse.from_dict(item) for item in raw_responses
            )
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_provider_responses_invalid"
            ) from exc
        response_by_ref = {item.response_ref: item for item in provider_responses}
        if len(response_by_ref) != len(provider_responses):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_provider_responses_invalid"
            )
        raw_audits = _mapping_sequence(
            payload["provider_audits"],
            "semantic_authority_result_provider_audits_invalid",
        )
        provider_audits_list = []
        owned_response_refs: set[str] = set()
        try:
            for raw_audit in raw_audits:
                refs = _string_tuple(
                    raw_audit.get("provider_response_refs"),
                    "semantic_authority_result_provider_audits_invalid",
                    allow_empty=False,
                    sort=False,
                )
                if set(refs) & owned_response_refs or any(
                    ref not in response_by_ref for ref in refs
                ):
                    raise SemanticAuthorityWorkflowError(
                        "semantic_authority_result_provider_audit_closure_invalid"
                    )
                owned_response_refs.update(refs)
                provider_audits_list.append(
                    RestrictedProviderAudit.from_dict(
                        raw_audit,
                        provider_responses=tuple(response_by_ref[ref] for ref in refs),
                    )
                )
        except (TypeError, ValueError) as exc:
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_provider_audits_invalid"
            ) from exc
        provider_audits = tuple(provider_audits_list)
        if owned_response_refs != set(response_by_ref) or len(
            {item.audit_ref for item in provider_audits}
        ) != len(provider_audits):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_provider_audit_closure_invalid"
            )
        _validate_result_provider_closure(
            projection=projection,
            checkpoint=checkpoint,
            claim_attempt=claim_attempt,
            settlement=settlement,
            recommendation_proposals=recommendation_proposals,
            recommendation_attempts=recommendation_attempts,
            provider_responses=provider_responses,
            provider_audits=provider_audits,
        )

        rebuilt = _result(
            projection=projection,
            candidate_proposals=candidate_proposals,
            checkpoint=checkpoint,
            claim_verification_attempt=claim_attempt,
            claim_verification_decisions=claim_decisions,
            settlement=settlement,
            recommendation_proposals=recommendation_proposals,
            recommendation_verification_attempts=recommendation_attempts,
            recommendation_verification_decisions=recommendation_decisions,
            recommendations=recommendations,
            authority_bundle_inputs=authority_inputs,
            provider_responses=provider_responses,
            provider_audits=provider_audits,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise SemanticAuthorityWorkflowError(
                "semantic_authority_result_integrity_invalid"
            )
        return rebuilt

    def replay(self) -> "SemanticAuthorityResult":
        return self.from_dict(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_ref": self.result_ref,
            "projection": self.projection.to_dict(),
            "candidate_proposals": [
                item.to_dict() for item in self.candidate_proposals
            ],
            "checkpoint": self.checkpoint.to_dict(),
            "claim_verification_attempt": (
                self.claim_verification_attempt.to_dict()
                if self.claim_verification_attempt is not None
                else None
            ),
            "claim_verification_decisions": [
                item.to_dict() for item in self.claim_verification_decisions
            ],
            "settlement": self.settlement.to_dict(),
            "recommendation_proposals": [
                item.to_dict() for item in self.recommendation_proposals
            ],
            "recommendation_verification_attempts": [
                item.to_dict() for item in self.recommendation_verification_attempts
            ],
            "recommendation_verification_decisions": [
                item.to_dict() for item in self.recommendation_verification_decisions
            ],
            "recommendations": [item.to_dict() for item in self.recommendations],
            "authority_bundle_inputs": self.authority_bundle_inputs.to_dict(),
            "provider_responses": [item.to_dict() for item in self.provider_responses],
            "provider_audits": [item.to_dict() for item in self.provider_audits],
            "content_digest": self.content_digest,
        }


def _result(
    *,
    projection: RestrictedExecutionProjection,
    candidate_proposals: Sequence[CandidateClaimProposal],
    checkpoint: ClaimSettlementCheckpoint,
    claim_verification_attempt: SemanticVerificationAttempt | None,
    claim_verification_decisions: Sequence[SemanticVerificationDecision],
    settlement: ClaimSettlement,
    recommendation_proposals: Sequence[RecommendationProposal],
    recommendation_verification_attempts: Sequence[SemanticVerificationAttempt],
    recommendation_verification_decisions: Sequence[SemanticVerificationDecision],
    recommendations: Sequence[RecommendationRecord],
    authority_bundle_inputs: AuthorityBundleInputs,
    provider_responses: Sequence[RestrictedProviderResponse],
    provider_audits: Sequence[RestrictedProviderAudit],
) -> SemanticAuthorityResult:
    normalized_recommendations = authority_bundle_inputs.recommendations
    if (
        tuple(sorted(recommendations, key=lambda item: item.recommendation_ref))
        != normalized_recommendations
    ):
        raise SemanticAuthorityWorkflowError(
            "semantic_authority_result_recommendation_inputs_invalid"
        )
    _validate_typed_result_provider_identity(
        projection=projection,
        checkpoint=checkpoint,
        claim_attempt=claim_verification_attempt,
        settlement=settlement,
        recommendation_proposals=recommendation_proposals,
        recommendation_attempts=recommendation_verification_attempts,
        provider_responses=provider_responses,
        provider_audits=provider_audits,
    )
    body = {
        "projection_ref": projection.projection_ref,
        "candidate_proposal_refs": tuple(
            item.candidate_proposal_ref for item in candidate_proposals
        ),
        "checkpoint_ref": checkpoint.checkpoint_ref,
        "claim_verification_attempt_ref": (
            claim_verification_attempt.verification_attempt_ref
            if claim_verification_attempt is not None
            else None
        ),
        "claim_verification_decision_refs": tuple(
            item.verification_decision_ref for item in claim_verification_decisions
        ),
        "settlement_ref": settlement.settlement_ref,
        "recommendation_proposal_refs": tuple(
            item.recommendation_proposal_ref for item in recommendation_proposals
        ),
        "recommendation_verification_attempt_refs": tuple(
            item.verification_attempt_ref
            for item in recommendation_verification_attempts
        ),
        "recommendation_verification_decision_refs": tuple(
            item.verification_decision_ref
            for item in recommendation_verification_decisions
        ),
        "recommendation_refs": tuple(
            item.recommendation_ref for item in normalized_recommendations
        ),
        "authority_inputs_ref": authority_bundle_inputs.authority_inputs_ref,
        "provider_response_refs": tuple(
            item.response_ref for item in provider_responses
        ),
        "provider_audit_refs": tuple(item.audit_ref for item in provider_audits),
    }
    digest = canonical_digest(body)
    return SemanticAuthorityResult(
        result_ref="semantic-authority-result:sha256:" + digest,
        projection=projection,
        candidate_proposals=tuple(candidate_proposals),
        checkpoint=checkpoint,
        claim_verification_attempt=claim_verification_attempt,
        claim_verification_decisions=tuple(claim_verification_decisions),
        settlement=settlement,
        recommendation_proposals=tuple(recommendation_proposals),
        recommendation_verification_attempts=tuple(
            recommendation_verification_attempts
        ),
        recommendation_verification_decisions=tuple(
            recommendation_verification_decisions
        ),
        recommendations=normalized_recommendations,
        authority_bundle_inputs=authority_bundle_inputs,
        provider_responses=tuple(provider_responses),
        provider_audits=tuple(provider_audits),
        content_digest=digest,
    )


def run_semantic_authority_workflow(
    execution_result: AuthoritativeExecutionResult,
    *,
    authority_namespace: ClaimAuthorityNamespace,
    claim_coverage_checkpoint: ClaimCoverageCheckpoint,
    llm_client: TypedSemanticAuthorityLLM,
) -> SemanticAuthorityResult:
    execution = _validated_execution(execution_result)
    namespace = _validated_namespace(authority_namespace, execution=execution)
    projection = RestrictedExecutionProjection.create(
        execution,
        claim_coverage_checkpoint=claim_coverage_checkpoint,
    )
    provider_responses: list[RestrictedProviderResponse] = []
    provider_audits: list[RestrictedProviderAudit] = []

    candidate_proposals: tuple[CandidateClaimProposal, ...] = ()
    if projection.has_candidate_authority:
        candidate_input = _candidate_call_input(projection)
        candidate_invocation = _invoke(
            llm_client,
            call_input=candidate_input,
            validator=lambda output: _candidate_output_validator(
                output, projection=projection
            ),
        )
        provider_responses.extend(candidate_invocation.responses)
        provider_audits.append(candidate_invocation.audit)
        candidate_proposals = _candidate_proposals_from_output(
            candidate_invocation.output,
            projection=projection,
            authority_namespace=namespace,
        )

    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=namespace,
        candidate_proposals=candidate_proposals,
    )
    claim_attempt: SemanticVerificationAttempt | None = None
    claim_decisions: tuple[SemanticVerificationDecision, ...] = ()
    if checkpoint.proposed_claims:
        claim_input = _claim_call_input(checkpoint, projection)
        claim_invocation = _invoke(
            llm_client,
            call_input=claim_input,
            validator=lambda output: _claim_verification_output_validator(
                output,
                checkpoint=checkpoint,
                projection=projection,
            ),
        )
        provider_responses.extend(claim_invocation.responses)
        provider_audits.append(claim_invocation.audit)
        claim_response = claim_invocation.final_response
        claim_attempt = checkpoint.verification_attempt(
            provider_ref=claim_response.provider_ref,
            model_ref=claim_response.model_ref,
            input_digest=claim_input.content_digest,
            attempt_number=claim_response.attempt_number,
            raw_provider_response_ref=claim_response.response_ref,
            raw_provider_response_digest=claim_response.content_digest,
        )
        claim_decisions = _claim_decisions_from_output(
            claim_invocation.output,
            checkpoint=checkpoint,
            projection=projection,
            authority_namespace=namespace,
            verification_attempt=claim_attempt,
        )

    settlement = settle_claim_checkpoint(
        checkpoint,
        verification_attempt=claim_attempt,
        verification_decisions=claim_decisions,
    )
    recommendation_proposals: tuple[RecommendationProposal, ...] = ()
    recommendation_attempts: list[SemanticVerificationAttempt] = []
    recommendation_decisions: list[SemanticVerificationDecision] = []
    recommendations: list[RecommendationRecord] = []

    if settlement.claim_graph.authority_mode == "claim_bearing":
        recommendation_input = _recommendation_call_input(settlement, projection)
        recommendation_invocation = _invoke(
            llm_client,
            call_input=recommendation_input,
            validator=lambda output: _recommendation_output_validator(
                output, settlement=settlement
            ),
        )
        provider_responses.extend(recommendation_invocation.responses)
        recommendation_proposals, recommendation_policy_rejections = (
            _recommendation_proposals_from_output(
                recommendation_invocation.output,
                settlement=settlement,
            )
        )
        provider_audits.append(
            _provider_audit_with_policy_rejections(
                recommendation_invocation.audit,
                provider_responses=recommendation_invocation.responses,
                policy_rejections=recommendation_policy_rejections,
            )
        )

        known_risks = settlement.claim_graph.limitation_refs
        for proposal in recommendation_proposals:
            verification_input = _recommendation_verification_call_input(
                settlement, proposal, projection
            )
            verification_invocation = _invoke(
                llm_client,
                call_input=verification_input,
                validator=lambda output, current=proposal: (
                    _recommendation_verification_output_validator(
                        output,
                        proposal=current,
                        known_limitation_refs=known_risks,
                    )
                ),
            )
            provider_responses.extend(verification_invocation.responses)
            provider_audits.append(verification_invocation.audit)
            response = verification_invocation.final_response
            attempt = SemanticVerificationAttempt.create(
                authority_namespace=namespace,
                purpose="recommendation",
                authority_input_ref=settlement.claim_graph.claim_graph_ref,
                authority_input_digest=settlement.claim_graph.content_digest,
                subject_refs=(proposal.recommendation_proposal_ref,),
                provider_ref=response.provider_ref,
                model_ref=response.model_ref,
                input_digest=verification_input.content_digest,
                attempt_number=response.attempt_number,
                raw_provider_response_ref=response.response_ref,
                raw_provider_response_digest=response.content_digest,
            )
            raw_decision = verification_invocation.output["decision"]
            public_text_violation = _recommendation_public_text_violation(
                proposal,
                settlement=settlement,
            )
            if (
                raw_decision["disposition"] == "accepted"
                and public_text_violation is not None
            ):
                raw_decision = {
                    **raw_decision,
                    "disposition": "vetoed",
                    "veto_basis": "contract_or_provenance_invalid",
                    "reason_code": public_text_violation,
                    "limitation_refs": [],
                    "verified_commitment_refs": [],
                }
            decision = SemanticVerificationDecision.create(
                authority_namespace=namespace,
                verification_attempt=attempt,
                subject_ref=raw_decision["subject_ref"],
                disposition=raw_decision["disposition"],
                veto_basis=raw_decision["veto_basis"],
                reason_code=raw_decision["reason_code"],
                limitation_refs=raw_decision["limitation_refs"],
            )
            recommendation_attempts.append(attempt)
            recommendation_decisions.append(decision)
            if decision.disposition == "accepted":
                recommendations.append(
                    RecommendationRecord.verify(
                        authority_namespace=namespace,
                        proposal=proposal,
                        verification_attempt=attempt,
                        verification_decision=decision,
                        claim_settlement=settlement,
                    )
                )

    authority_inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement,
        recommendations=tuple(recommendations),
    )
    return _result(
        projection=projection,
        candidate_proposals=candidate_proposals,
        checkpoint=checkpoint,
        claim_verification_attempt=claim_attempt,
        claim_verification_decisions=claim_decisions,
        settlement=settlement,
        recommendation_proposals=recommendation_proposals,
        recommendation_verification_attempts=tuple(recommendation_attempts),
        recommendation_verification_decisions=tuple(recommendation_decisions),
        recommendations=tuple(recommendations),
        authority_bundle_inputs=authority_inputs,
        provider_responses=tuple(provider_responses),
        provider_audits=tuple(provider_audits),
    )


__all__ = (
    "RestrictedAggregateEvidence",
    "RestrictedClaimObligation",
    "RestrictedExecutionProjection",
    "RestrictedProviderAudit",
    "SemanticAuthorityCallInput",
    "SemanticAuthorityResult",
    "SemanticAuthorityWorkflowError",
    "TypedSemanticAuthorityLLM",
    "run_semantic_authority_workflow",
)
