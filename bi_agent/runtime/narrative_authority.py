from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    ClaimKey,
    ClaimPublicationCeiling,
    ClaimRevision,
    RECOMMENDATION_ACTION_DOMAINS,
    RECOMMENDATION_ACTION_STAGES,
    RECOMMENDATION_COMMITMENT_CONTRACT_VERSION,
    RECOMMENDATION_COMMITMENT_KINDS,
    RECOMMENDATION_DIAGNOSTIC_MODES,
    RECOMMENDATION_EXPECTED_VALUE_KINDS,
    RECOMMENDATION_EXPECTED_VALUE_MODES,
    RecommendationCommitment,
    RecommendationRecord,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value

if TYPE_CHECKING:
    from bi_agent.runtime.narrative_material_projection import (
        NarrativeMaterialProjection,
    )


class NarrativeAuthorityContractError(ValueError):
    pass


PUBLIC_FACT_KINDS = frozenset({"number", "date", "date_range", "scope", "label"})
PUBLICATION_VISIBLE_FIELDS = (
    "blocks",
    "claim_refs",
    "field_visibility_policy_ref",
    "limitation_refs",
    "recommendation_refs",
    "visualization_refs",
    "warnings",
)
PUBLICATION_BLOCK_VISIBLE_FIELDS = (
    "claim_refs",
    "limitation_refs",
    "material_fact_bindings",
    "recommendation_refs",
    "role",
    "statement_role",
    "text",
)
PUBLICATION_FACT_BINDING_VISIBLE_FIELDS = (
    "fact_kind",
    "name",
    "range_end",
    "unit",
    "value",
)
PUBLICATION_FORBIDDEN_FIELDS = (
    "authority_owner_ref",
    "content_digest",
    "debug",
    "internal_debug",
    "internal_record_ref",
    "internal_record_refs",
    "owner_id",
    "owner_ref",
    "palette_ref",
    "player_id",
    "raw_provider_response_ref",
    "raw_rows",
    "raw_sql",
    "secrets",
    "sql",
    "technical_detail",
    "technical_detail_ref",
    "writer_attempt_id",
)
NARRATIVE_BLOCK_ROLES = frozenset(
    {
        "executive_answer",
        "direction",
        "accounting_drivers",
        "dimension_localization",
        "contextual_pattern",
        "boundary",
        "next_action",
    }
)
LOCAL_BLOCK_ISSUE_CODES = frozenset(
    {
        "unknown_claim_handle",
        "unknown_requirement_handle",
        "question_requirement_scope_mismatch",
        "unknown_limitation_handle",
        "limitation_claim_scope_mismatch",
        "unknown_fact_handle",
        "material_fact_binding_mismatch",
        "ranking_position_binding_incomplete",
        "internal_fact_name_exposed",
        "unknown_recommendation_handle",
        "sensitive_output_policy_violation",
    }
)
RESTRICTED_PROVIDER_RESPONSE_PURPOSES = frozenset(
    {
        "candidate_claim_proposal",
        "claim_verification",
        "recommendation_proposal",
        "recommendation_verification",
        "narrative_writer",
        "block_verification",
    }
)


def narrative_block_authority_handles_are_valid(
    *,
    role: str,
    claim_handles: Sequence[str],
    recommendation_handles: Sequence[str],
    limitation_handles: Sequence[str],
) -> bool:
    """Return whether a block has the minimum authority for its semantic role."""

    if role not in NARRATIVE_BLOCK_ROLES:
        return False
    if role == "boundary":
        return bool(limitation_handles or claim_handles)
    if role == "next_action":
        return bool(recommendation_handles)
    return bool(claim_handles or recommendation_handles)


def _plain(value: Any) -> Any:
    return canonical_value(value)


def _immutable(value: Any, error: str) -> Any:
    try:
        normalized = canonical_value(value)
    except ValueError as exc:
        raise NarrativeAuthorityContractError(error) from exc
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _immutable(item, error) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_immutable(item, error) for item in normalized)
    return normalized


def _strict_shape(
    payload: Any,
    record_type: type,
    error: str,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != set(
        record_type.__dataclass_fields__
    ):
        raise NarrativeAuthorityContractError(error)
    return payload


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise NarrativeAuthorityContractError(error)
    return value


def _optional_string(value: Any, error: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, error)


def _raw_text(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NarrativeAuthorityContractError(error)
    return value


def _boolean(value: Any, error: str) -> bool:
    if type(value) is not bool:
        raise NarrativeAuthorityContractError(error)
    return value


def _positive_integer(value: Any, error: str) -> int:
    if type(value) is not int or value < 1:
        raise NarrativeAuthorityContractError(error)
    return value


def _digest_string(value: Any, error: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise NarrativeAuthorityContractError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise NarrativeAuthorityContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise NarrativeAuthorityContractError(error)
    if len(normalized) != len(set(normalized)):
        raise NarrativeAuthorityContractError(error)
    return tuple(sorted(normalized)) if sort else normalized


def _typed_records(
    value: Any,
    record_type: type,
    identity_field: str,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = True,
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise NarrativeAuthorityContractError(error)
    records = tuple(value)
    if not allow_empty and not records:
        raise NarrativeAuthorityContractError(error)
    if any(type(item) is not record_type for item in records):
        raise NarrativeAuthorityContractError(error)
    identities = tuple(str(getattr(item, identity_field)) for item in records)
    if len(identities) != len(set(identities)):
        raise NarrativeAuthorityContractError(error)
    if not sort:
        return records
    return tuple(sorted(records, key=lambda item: str(getattr(item, identity_field))))


def _stable_handle(prefix: str, authority_ref: str) -> str:
    return f"{prefix}_{canonical_digest({'authority_ref': authority_ref})[:20]}"


def _canonical_number(value: Any, error: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise NarrativeAuthorityContractError(error)
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise NarrativeAuthorityContractError(error) from exc
    if not number.is_finite():
        raise NarrativeAuthorityContractError(error)
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized in {"-0", ""}:
        return "0"
    return normalized


def _canonical_date(value: Any, error: str) -> str:
    if not isinstance(value, str):
        raise NarrativeAuthorityContractError(error)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise NarrativeAuthorityContractError(error) from exc
    if parsed.isoformat() != value:
        raise NarrativeAuthorityContractError(error)
    return value


def _assert_content_addressed_record(
    record: Any,
    *,
    ref_field: str,
    digest_field: str,
    prefix: str,
    excluded_fields: Sequence[str] = (),
    error: str,
) -> None:
    if not hasattr(record, "to_dict"):
        raise NarrativeAuthorityContractError(error)
    payload = record.to_dict()
    excluded = {ref_field, digest_field, *excluded_fields}
    body = {key: value for key, value in payload.items() if key not in excluded}
    digest = canonical_digest(body)
    ref = payload.get(ref_field)
    namespaced_prefix = prefix.removesuffix("sha256:")
    ref_valid = ref == prefix + digest or (
        isinstance(ref, str)
        and ref.startswith(namespaced_prefix)
        and ref.endswith(":sha256:" + digest)
    )
    if payload.get(digest_field) != digest or not ref_valid:
        raise NarrativeAuthorityContractError(error)


def _assert_authority_bundle_integrity(bundle: AuthorityBundle) -> None:
    if type(bundle) is not AuthorityBundle:
        raise NarrativeAuthorityContractError("public_palette_bundle_invalid")
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
    namespace_prefix = "claim-authority-namespace:sha256:"
    if not bundle.authority_namespace_ref.startswith(namespace_prefix):
        raise NarrativeAuthorityContractError("public_palette_bundle_integrity_invalid")
    if (
        _string_tuple(
            bundle.required_obligation_ids,
            "public_palette_bundle_integrity_invalid",
        )
        != bundle.required_obligation_ids
    ):
        raise NarrativeAuthorityContractError("public_palette_bundle_integrity_invalid")
    namespace_token = bundle.authority_namespace_ref.removeprefix(namespace_prefix)[:24]
    if (
        bundle.bundle_digest != digest
        or bundle.content_digest != digest
        or bundle.seal_state != "sealed"
        or bundle.bundle_ref != f"authority-bundle:{namespace_token}:sha256:{digest}"
        or bundle.authority_mode not in {"claim_bearing", "boundary_only"}
        or (bundle.authority_mode == "claim_bearing" and not bundle.verified_claim_refs)
        or (
            bundle.authority_mode == "boundary_only"
            and (
                bundle.verified_claim_refs
                or bundle.recommendation_refs
                or not bundle.obligation_coverage_refs
                or not bundle.limitation_refs
            )
        )
    ):
        raise NarrativeAuthorityContractError("public_palette_bundle_integrity_invalid")


def _assert_claim_integrity(claim: ClaimRevision) -> None:
    if type(claim) is not ClaimRevision:
        raise NarrativeAuthorityContractError("public_fact_claim_invalid")
    _assert_content_addressed_record(
        claim,
        ref_field="claim_ref",
        digest_field="content_digest",
        prefix="claim:sha256:",
        excluded_fields=("authority_namespace_ref",),
        error="public_fact_claim_integrity_invalid",
    )


def _assert_public_fact_claim_closure(
    fact: PublicFactDescriptor,
    *,
    claim: ClaimRevision,
) -> None:
    if type(fact) is not PublicFactDescriptor:
        raise NarrativeAuthorityContractError("public_claim_facts_invalid")
    fact.assert_integrity()
    if (
        fact.claim_ref != claim.claim_ref
        or fact.claim_digest != claim.content_digest
        or fact.source_material_ref not in claim.support_edge_refs
    ):
        raise NarrativeAuthorityContractError(
            "public_fact_source_material_closure_invalid"
        )


def _assert_claim_key_integrity(claim_key: ClaimKey) -> None:
    if type(claim_key) is not ClaimKey:
        raise NarrativeAuthorityContractError("public_claim_key_closure_invalid")
    _assert_content_addressed_record(
        claim_key,
        ref_field="claim_key",
        digest_field="content_digest",
        prefix="claim-key:sha256:",
        excluded_fields=("authority_namespace_ref",),
        error="public_claim_key_integrity_invalid",
    )


def _assert_namespaced_content_addressed_record(
    record: Any,
    *,
    ref_field: str,
    digest_field: str,
    kind: str,
    authority_namespace_ref: str,
    error: str,
) -> None:
    _assert_content_addressed_record(
        record,
        ref_field=ref_field,
        digest_field=digest_field,
        prefix=f"{kind}:sha256:",
        excluded_fields=("authority_namespace_ref",),
        error=error,
    )
    namespace_prefix = "claim-authority-namespace:sha256:"
    if not authority_namespace_ref.startswith(namespace_prefix):
        raise NarrativeAuthorityContractError(error)
    namespace_token = authority_namespace_ref.removeprefix(namespace_prefix)[:24]
    if getattr(record, "authority_namespace_ref", None) != authority_namespace_ref or (
        getattr(record, ref_field, None)
        != f"{kind}:{namespace_token}:sha256:{getattr(record, digest_field)}"
    ):
        raise NarrativeAuthorityContractError(error)


def _assert_recommendation_commitment_semantics(
    commitment: Any,
    *,
    error: str,
) -> None:
    kind = getattr(commitment, "commitment_kind", None)
    if kind not in RECOMMENDATION_COMMITMENT_KINDS:
        raise NarrativeAuthorityContractError(error)
    _required_string(getattr(commitment, "text", None), error)
    _string_tuple(
        getattr(commitment, "supporting_claim_refs", None),
        error,
        allow_empty=False,
    )
    diagnostic_mode = getattr(commitment, "diagnostic_mode", None)
    action_domain = getattr(commitment, "action_domain", None)
    action_stage = getattr(commitment, "action_stage", None)
    expected_value_kind = getattr(commitment, "expected_value_kind", None)
    expected_value_mode = getattr(commitment, "expected_value_mode", None)
    if (
        diagnostic_mode is not None
        and diagnostic_mode not in RECOMMENDATION_DIAGNOSTIC_MODES
    ):
        raise NarrativeAuthorityContractError(error)
    if action_domain is not None and action_domain not in RECOMMENDATION_ACTION_DOMAINS:
        raise NarrativeAuthorityContractError(error)
    if action_stage is not None and action_stage not in RECOMMENDATION_ACTION_STAGES:
        raise NarrativeAuthorityContractError(error)
    if (
        expected_value_kind is not None
        and expected_value_kind not in RECOMMENDATION_EXPECTED_VALUE_KINDS
    ):
        raise NarrativeAuthorityContractError(error)
    if (
        expected_value_mode is not None
        and expected_value_mode not in RECOMMENDATION_EXPECTED_VALUE_MODES
    ):
        raise NarrativeAuthorityContractError(error)
    expected_fields = {
        "diagnostic_premise": (
            diagnostic_mode is not None
            and action_domain is None
            and action_stage is None
            and expected_value_kind is None
            and expected_value_mode is None
        ),
        "action": (
            diagnostic_mode is None
            and action_domain is not None
            and action_stage is not None
            and expected_value_kind is None
            and expected_value_mode is None
        ),
        "expected_outcome": (
            diagnostic_mode is None
            and action_domain is None
            and action_stage is None
            and expected_value_kind is not None
            and expected_value_mode is not None
        ),
    }
    if not expected_fields[kind]:
        raise NarrativeAuthorityContractError(error)


def _assert_recommendation_integrity(recommendation: RecommendationRecord) -> None:
    if type(recommendation) is not RecommendationRecord:
        raise NarrativeAuthorityContractError("public_recommendation_authority_invalid")
    namespace_ref = recommendation.authority_namespace_ref
    _assert_namespaced_content_addressed_record(
        recommendation,
        ref_field="recommendation_ref",
        digest_field="content_digest",
        kind="recommendation",
        authority_namespace_ref=namespace_ref,
        error="public_recommendation_integrity_invalid",
    )
    proposal = recommendation.proposal
    attempt = recommendation.verification_attempt
    decision = recommendation.verification_decision
    commitments = recommendation.commitments
    _assert_namespaced_content_addressed_record(
        proposal,
        ref_field="recommendation_proposal_ref",
        digest_field="content_digest",
        kind="recommendation-proposal",
        authority_namespace_ref=namespace_ref,
        error="public_recommendation_integrity_invalid",
    )
    _assert_namespaced_content_addressed_record(
        attempt,
        ref_field="verification_attempt_ref",
        digest_field="content_digest",
        kind="semantic-verification-attempt",
        authority_namespace_ref=namespace_ref,
        error="public_recommendation_integrity_invalid",
    )
    _assert_namespaced_content_addressed_record(
        decision,
        ref_field="verification_decision_ref",
        digest_field="content_digest",
        kind="semantic-verification-decision",
        authority_namespace_ref=namespace_ref,
        error="public_recommendation_integrity_invalid",
    )
    if any(type(item) is not RecommendationCommitment for item in commitments):
        raise NarrativeAuthorityContractError("public_recommendation_integrity_invalid")
    for commitment in commitments:
        _assert_recommendation_commitment_semantics(
            commitment,
            error="public_recommendation_integrity_invalid",
        )
        _assert_namespaced_content_addressed_record(
            commitment,
            ref_field="recommendation_commitment_ref",
            digest_field="content_digest",
            kind="recommendation-commitment",
            authority_namespace_ref=namespace_ref,
            error="public_recommendation_integrity_invalid",
        )
    commitment_refs = tuple(item.recommendation_commitment_ref for item in commitments)
    action_commitments = tuple(
        item for item in commitments if item.commitment_kind == "action"
    )
    outcome_commitments = tuple(
        item for item in commitments if item.commitment_kind == "expected_outcome"
    )
    if (
        recommendation.recommendation_proposal_ref
        != proposal.recommendation_proposal_ref
        or recommendation.claim_graph_ref != proposal.claim_graph_ref
        or recommendation.claim_graph_digest != proposal.claim_graph_digest
        or recommendation.commitment_contract_version
        != RECOMMENDATION_COMMITMENT_CONTRACT_VERSION
        or recommendation.commitment_contract_version
        != proposal.commitment_contract_version
        or recommendation.recommendation_commitment_refs != commitment_refs
        or proposal.recommendation_commitment_refs != commitment_refs
        or recommendation.commitments != proposal.commitments
        or len(commitment_refs) != len(set(commitment_refs))
        or len(action_commitments) != 1
        or len(outcome_commitments) != 1
        or action_commitments[0].text != recommendation.action
        or outcome_commitments[0].text != recommendation.expected_decision_value
        or {
            claim_ref
            for commitment in commitments
            for claim_ref in commitment.supporting_claim_refs
        }
        != set(recommendation.supporting_claim_refs)
        or recommendation.supporting_claim_refs != proposal.supporting_claim_refs
        or recommendation.assumption_refs != proposal.assumption_refs
        or recommendation.risk_refs != proposal.risk_refs
        or recommendation.action != proposal.action
        or recommendation.applicable_conditions != proposal.applicable_conditions
        or recommendation.expected_decision_value != proposal.expected_decision_value
        or recommendation.verification_attempt_ref != attempt.verification_attempt_ref
        or recommendation.verification_decision_ref
        != decision.verification_decision_ref
        or attempt.purpose != "recommendation"
        or attempt.authority_input_ref != recommendation.claim_graph_ref
        or attempt.authority_input_digest != recommendation.claim_graph_digest
        or attempt.subject_refs != (proposal.recommendation_proposal_ref,)
        or decision.verification_attempt_ref != attempt.verification_attempt_ref
        or decision.subject_ref != proposal.recommendation_proposal_ref
        or decision.disposition != "accepted"
        or decision.veto_basis is not None
        or decision.reason_code is not None
        or decision.limitation_refs
    ):
        raise NarrativeAuthorityContractError("public_recommendation_integrity_invalid")


def _assert_no_forbidden_fields(
    value: Any,
    *,
    forbidden_fields: frozenset[str],
    error: str,
) -> None:
    if isinstance(value, Mapping):
        folded_forbidden = {item.casefold() for item in forbidden_fields}
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in folded_forbidden:
                raise NarrativeAuthorityContractError(error)
            _assert_no_forbidden_fields(
                item,
                forbidden_fields=forbidden_fields,
                error=error,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _assert_no_forbidden_fields(
                item,
                forbidden_fields=forbidden_fields,
                error=error,
            )


def _assert_no_restricted_text_literals(
    value: Any,
    *,
    restricted_literals: frozenset[str],
    error: str,
) -> None:
    if isinstance(value, str):
        if any(literal in value for literal in restricted_literals):
            raise NarrativeAuthorityContractError(error)
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_no_restricted_text_literals(
                item,
                restricted_literals=restricted_literals,
                error=error,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _assert_no_restricted_text_literals(
                item,
                restricted_literals=restricted_literals,
                error=error,
            )
        return


@dataclass(frozen=True)
class PublicationFieldVisibilityPolicy:
    policy_ref: str
    policy_id: str
    revision: int
    visible_fields: tuple[str, ...]
    block_visible_fields: tuple[str, ...]
    fact_binding_visible_fields: tuple[str, ...]
    restricted_output_policy_ref: str
    restricted_output_policy_version: str
    restricted_output_fields: tuple[str, ...]
    forbidden_fields: tuple[str, ...]
    content_digest: str

    @classmethod
    def fixed(
        cls,
        *,
        policy_id: str,
        revision: int,
        restricted_output_policy_ref: str,
        restricted_output_policy_version: str,
        restricted_output_fields: Sequence[str],
    ) -> "PublicationFieldVisibilityPolicy":
        if type(revision) is not int or revision < 1:
            raise NarrativeAuthorityContractError(
                "publication_visibility_policy_revision_invalid"
            )
        if isinstance(restricted_output_fields, (str, bytes)) or not isinstance(
            restricted_output_fields, Sequence
        ):
            raise NarrativeAuthorityContractError(
                "publication_restricted_output_fields_invalid"
            )
        normalized_restricted_fields = tuple(
            sorted(
                _required_string(field, "publication_restricted_output_field_invalid")
                for field in restricted_output_fields
            )
        )
        if not normalized_restricted_fields or len(
            set(normalized_restricted_fields)
        ) != len(normalized_restricted_fields):
            raise NarrativeAuthorityContractError(
                "publication_restricted_output_fields_invalid"
            )
        body = {
            "policy_id": _required_string(
                policy_id, "publication_visibility_policy_id_invalid"
            ),
            "revision": revision,
            "visible_fields": PUBLICATION_VISIBLE_FIELDS,
            "block_visible_fields": PUBLICATION_BLOCK_VISIBLE_FIELDS,
            "fact_binding_visible_fields": PUBLICATION_FACT_BINDING_VISIBLE_FIELDS,
            "restricted_output_policy_ref": _required_string(
                restricted_output_policy_ref,
                "publication_restricted_output_policy_ref_invalid",
            ),
            "restricted_output_policy_version": _required_string(
                restricted_output_policy_version,
                "publication_restricted_output_policy_version_invalid",
            ),
            "restricted_output_fields": normalized_restricted_fields,
            "forbidden_fields": tuple(
                sorted(
                    set(PUBLICATION_FORBIDDEN_FIELDS)
                    | set(normalized_restricted_fields)
                )
            ),
        }
        digest = canonical_digest(body)
        return cls(
            policy_ref="field-visibility-policy:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "PublicationFieldVisibilityPolicy":
        payload = _strict_shape(
            payload,
            cls,
            "publication_visibility_policy_shape_invalid",
        )
        rebuilt = cls.fixed(
            policy_id=payload["policy_id"],
            revision=payload["revision"],
            restricted_output_policy_ref=payload["restricted_output_policy_ref"],
            restricted_output_policy_version=payload[
                "restricted_output_policy_version"
            ],
            restricted_output_fields=payload["restricted_output_fields"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError(
                "publication_visibility_policy_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def assert_public_name(self, public_name: str) -> None:
        PublicationFieldVisibilityPolicy.from_dict(self.to_dict())
        if public_name in set(self.forbidden_fields):
            raise NarrativeAuthorityContractError("public_fact_name_forbidden")

    def validate_customer_payload(self, payload: Mapping[str, Any]) -> None:
        PublicationFieldVisibilityPolicy.from_dict(self.to_dict())
        _assert_no_forbidden_fields(
            payload,
            forbidden_fields=frozenset(self.forbidden_fields),
            error="publication_customer_payload_forbidden_field",
        )
        _assert_no_restricted_text_literals(
            payload,
            restricted_literals=frozenset(self.restricted_output_fields),
            error="publication_customer_payload_restricted_literal",
        )
        if not isinstance(payload, Mapping) or set(payload) != set(self.visible_fields):
            raise NarrativeAuthorityContractError(
                "publication_customer_payload_shape_invalid"
            )
        blocks = payload.get("blocks")
        if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence):
            raise NarrativeAuthorityContractError(
                "publication_customer_payload_shape_invalid"
            )
        for block in blocks:
            if not isinstance(block, Mapping) or set(block) != set(
                self.block_visible_fields
            ):
                raise NarrativeAuthorityContractError(
                    "publication_customer_payload_shape_invalid"
                )
            bindings = block.get("material_fact_bindings")
            if isinstance(bindings, (str, bytes)) or not isinstance(bindings, Sequence):
                raise NarrativeAuthorityContractError(
                    "publication_customer_payload_shape_invalid"
                )
            if any(
                not isinstance(binding, Mapping)
                or set(binding) != set(self.fact_binding_visible_fields)
                for binding in bindings
            ):
                raise NarrativeAuthorityContractError(
                    "publication_customer_payload_shape_invalid"
                )


def _normalize_fact_values(
    *,
    fact_kind: str,
    value: Any,
    range_end: Any,
    unit: Any,
    error: str,
) -> tuple[str, str | None, str | None]:
    if fact_kind not in PUBLIC_FACT_KINDS:
        raise NarrativeAuthorityContractError(f"{error}_kind_invalid")
    normalized_unit = _optional_string(unit, f"{error}_unit_invalid")
    if fact_kind == "number":
        if range_end is not None:
            raise NarrativeAuthorityContractError(f"{error}_range_invalid")
        return _canonical_number(value, f"{error}_value_invalid"), None, normalized_unit
    if normalized_unit is not None:
        raise NarrativeAuthorityContractError(f"{error}_unit_invalid")
    if fact_kind == "date":
        if range_end is not None:
            raise NarrativeAuthorityContractError(f"{error}_range_invalid")
        return _canonical_date(value, f"{error}_value_invalid"), None, None
    if fact_kind == "date_range":
        start = _canonical_date(value, f"{error}_value_invalid")
        end = _canonical_date(range_end, f"{error}_range_invalid")
        if end < start:
            raise NarrativeAuthorityContractError(f"{error}_range_invalid")
        return start, end, None
    if range_end is not None:
        raise NarrativeAuthorityContractError(f"{error}_range_invalid")
    return _required_string(value, f"{error}_value_invalid"), None, None


@dataclass(frozen=True)
class PublicFactDescriptor:
    fact_ref: str
    fact_handle: str
    claim_ref: str
    claim_digest: str
    source_material_ref: str
    public_name: str
    fact_kind: str
    value: str
    range_end: str | None
    unit: str | None
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        claim: ClaimRevision,
        public_name: str,
        fact_kind: str,
        value: Any,
        range_end: Any,
        unit: str | None,
        source_material_ref: str,
    ) -> "PublicFactDescriptor":
        _assert_claim_integrity(claim)
        return cls._create_for_validated_claim(
            claim=claim,
            public_name=public_name,
            fact_kind=fact_kind,
            value=value,
            range_end=range_end,
            unit=unit,
            source_material_ref=source_material_ref,
        )

    @classmethod
    def _create_for_validated_claim(
        cls,
        *,
        claim: ClaimRevision,
        public_name: str,
        fact_kind: str,
        value: Any,
        range_end: Any,
        unit: str | None,
        source_material_ref: str,
    ) -> "PublicFactDescriptor":
        if claim.status != "verified":
            raise NarrativeAuthorityContractError("public_fact_claim_invalid")
        normalized_value, normalized_end, normalized_unit = _normalize_fact_values(
            fact_kind=fact_kind,
            value=value,
            range_end=range_end,
            unit=unit,
            error="public_fact",
        )
        material_ref = _required_string(
            source_material_ref,
            "public_fact_source_material_ref_invalid",
        )
        if material_ref not in set(claim.support_edge_refs):
            raise NarrativeAuthorityContractError(
                "public_fact_source_material_closure_invalid"
            )
        body = {
            "claim_ref": claim.claim_ref,
            "claim_digest": claim.content_digest,
            "source_material_ref": material_ref,
            "public_name": _required_string(public_name, "public_fact_name_invalid"),
            "fact_kind": fact_kind,
            "value": normalized_value,
            "range_end": normalized_end,
            "unit": normalized_unit,
        }
        digest = canonical_digest(body)
        fact_ref = "public-fact-descriptor:sha256:" + digest
        return cls(
            fact_ref=fact_ref,
            fact_handle=_stable_handle("f", fact_ref),
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        claim: ClaimRevision,
    ) -> "PublicFactDescriptor":
        _assert_claim_integrity(claim)
        return cls._from_dict_for_validated_claim(payload, claim=claim)

    @classmethod
    def _from_dict_for_validated_claim(
        cls,
        payload: Mapping[str, Any],
        *,
        claim: ClaimRevision,
    ) -> "PublicFactDescriptor":
        payload = _strict_shape(payload, cls, "public_fact_shape_invalid")
        rebuilt = cls._create_for_validated_claim(
            claim=claim,
            public_name=payload["public_name"],
            fact_kind=payload["fact_kind"],
            value=payload["value"],
            range_end=payload["range_end"],
            unit=payload["unit"],
            source_material_ref=payload["source_material_ref"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError("public_fact_integrity_invalid")
        return rebuilt

    def assert_integrity(self) -> None:
        normalized = _normalize_fact_values(
            fact_kind=self.fact_kind,
            value=self.value,
            range_end=self.range_end,
            unit=self.unit,
            error="public_fact",
        )
        if normalized != (self.value, self.range_end, self.unit):
            raise NarrativeAuthorityContractError("public_fact_integrity_invalid")
        _required_string(self.claim_ref, "public_fact_integrity_invalid")
        _required_string(self.claim_digest, "public_fact_integrity_invalid")
        _required_string(self.source_material_ref, "public_fact_integrity_invalid")
        _required_string(self.public_name, "public_fact_integrity_invalid")
        _assert_content_addressed_record(
            self,
            ref_field="fact_ref",
            digest_field="content_digest",
            prefix="public-fact-descriptor:sha256:",
            excluded_fields=("fact_handle",),
            error="public_fact_integrity_invalid",
        )
        if self.fact_handle != _stable_handle("f", self.fact_ref):
            raise NarrativeAuthorityContractError("public_fact_integrity_invalid")

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class PublicLimitation:
    limitation_ref: str
    limitation_handle: str
    public_context: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        limitation_ref: str,
        public_context: Mapping[str, Any],
    ) -> "PublicLimitation":
        authority_ref = _required_string(
            limitation_ref, "public_limitation_ref_invalid"
        )
        if not isinstance(public_context, Mapping) or not public_context:
            raise NarrativeAuthorityContractError("public_limitation_context_invalid")
        context = _immutable(
            public_context,
            "public_limitation_context_invalid",
        )
        _assert_no_forbidden_fields(
            context,
            forbidden_fields=frozenset(PUBLICATION_FORBIDDEN_FIELDS),
            error="public_limitation_context_forbidden_field",
        )
        body = {
            "limitation_ref": authority_ref,
            "public_context": context,
        }
        digest = canonical_digest(body)
        return cls(
            limitation_handle=_stable_handle("l", authority_ref),
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PublicLimitation":
        payload = _strict_shape(payload, cls, "public_limitation_shape_invalid")
        rebuilt = cls.create(
            limitation_ref=payload["limitation_ref"],
            public_context=payload["public_context"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError("public_limitation_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def assert_integrity(self) -> None:
        rebuilt = PublicLimitation.from_dict(self.to_dict())
        if rebuilt != self:
            raise NarrativeAuthorityContractError("public_limitation_integrity_invalid")

    def to_writer_payload(self) -> dict[str, Any]:
        self.assert_integrity()
        return {
            "limitation_handle": self.limitation_handle,
            "context": canonical_value(self.public_context),
        }


@dataclass(frozen=True)
class PublicRecommendationCommitment:
    public_recommendation_commitment_ref: str
    commitment_handle: str
    recommendation_commitment_ref: str
    recommendation_commitment_digest: str
    commitment_kind: str
    text: str
    supporting_claim_refs: tuple[str, ...]
    supporting_claim_handles: tuple[str, ...]
    diagnostic_mode: str | None
    action_domain: str | None
    action_stage: str | None
    expected_value_kind: str | None
    expected_value_mode: str | None
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        commitment: RecommendationCommitment,
        public_claims_by_ref: Mapping[str, "PublicClaim"],
    ) -> "PublicRecommendationCommitment":
        if type(commitment) is not RecommendationCommitment:
            raise NarrativeAuthorityContractError(
                "public_recommendation_commitment_authority_invalid"
            )
        _assert_recommendation_commitment_semantics(
            commitment,
            error="public_recommendation_commitment_integrity_invalid",
        )
        _assert_namespaced_content_addressed_record(
            commitment,
            ref_field="recommendation_commitment_ref",
            digest_field="content_digest",
            kind="recommendation-commitment",
            authority_namespace_ref=commitment.authority_namespace_ref,
            error="public_recommendation_commitment_integrity_invalid",
        )
        try:
            supporting_claims = tuple(
                public_claims_by_ref[claim_ref]
                for claim_ref in commitment.supporting_claim_refs
            )
        except (KeyError, TypeError) as exc:
            raise NarrativeAuthorityContractError(
                "public_recommendation_commitment_claim_closure_invalid"
            ) from exc
        if not supporting_claims or any(
            type(claim) is not PublicClaim for claim in supporting_claims
        ):
            raise NarrativeAuthorityContractError(
                "public_recommendation_commitment_claim_closure_invalid"
            )
        _assert_no_restricted_text_literals(
            commitment.text,
            restricted_literals=frozenset(public_claims_by_ref),
            error="public_recommendation_commitment_internal_ref_forbidden",
        )
        body = {
            "recommendation_commitment_ref": (commitment.recommendation_commitment_ref),
            "recommendation_commitment_digest": commitment.content_digest,
            "commitment_kind": commitment.commitment_kind,
            "text": commitment.text,
            "supporting_claim_refs": commitment.supporting_claim_refs,
            "supporting_claim_handles": tuple(
                claim.claim_handle for claim in supporting_claims
            ),
            "diagnostic_mode": commitment.diagnostic_mode,
            "action_domain": commitment.action_domain,
            "action_stage": commitment.action_stage,
            "expected_value_kind": commitment.expected_value_kind,
            "expected_value_mode": commitment.expected_value_mode,
        }
        digest = canonical_digest(body)
        return cls(
            public_recommendation_commitment_ref=(
                "public-recommendation-commitment:sha256:" + digest
            ),
            commitment_handle=_stable_handle(
                "rc", commitment.recommendation_commitment_ref
            ),
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        commitment: RecommendationCommitment,
        public_claims_by_ref: Mapping[str, "PublicClaim"],
    ) -> "PublicRecommendationCommitment":
        payload = _strict_shape(
            payload,
            cls,
            "public_recommendation_commitment_shape_invalid",
        )
        rebuilt = cls.create(
            commitment=commitment,
            public_claims_by_ref=public_claims_by_ref,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError(
                "public_recommendation_commitment_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def assert_integrity(self) -> None:
        _assert_recommendation_commitment_semantics(
            self,
            error="public_recommendation_commitment_integrity_invalid",
        )
        if len(self.supporting_claim_refs) != len(self.supporting_claim_handles) or len(
            set(self.supporting_claim_handles)
        ) != len(self.supporting_claim_handles):
            raise NarrativeAuthorityContractError(
                "public_recommendation_commitment_integrity_invalid"
            )
        _assert_content_addressed_record(
            self,
            ref_field="public_recommendation_commitment_ref",
            digest_field="content_digest",
            prefix="public-recommendation-commitment:sha256:",
            excluded_fields=("commitment_handle",),
            error="public_recommendation_commitment_integrity_invalid",
        )
        if self.commitment_handle != _stable_handle(
            "rc", self.recommendation_commitment_ref
        ):
            raise NarrativeAuthorityContractError(
                "public_recommendation_commitment_integrity_invalid"
            )

    def to_writer_payload(self) -> dict[str, Any]:
        self.assert_integrity()
        return {
            "commitment_handle": self.commitment_handle,
            "commitment_kind": self.commitment_kind,
            "text": self.text,
            "supporting_claim_handles": list(self.supporting_claim_handles),
            "diagnostic_mode": self.diagnostic_mode,
            "action_domain": self.action_domain,
            "action_stage": self.action_stage,
            "expected_value_kind": self.expected_value_kind,
            "expected_value_mode": self.expected_value_mode,
        }


@dataclass(frozen=True)
class PublicRecommendation:
    public_recommendation_ref: str
    recommendation_handle: str
    recommendation_ref: str
    recommendation_digest: str
    commitment_contract_version: str
    recommendation_commitment_refs: tuple[str, ...]
    commitments: tuple[PublicRecommendationCommitment, ...]
    supporting_claim_refs: tuple[str, ...]
    supporting_claim_handles: tuple[str, ...]
    assumption_refs: tuple[str, ...]
    risk_refs: tuple[str, ...]
    risk_handles: tuple[str, ...]
    action: str
    applicable_conditions: tuple[str, ...]
    expected_decision_value: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        recommendation: RecommendationRecord,
        public_claims_by_ref: Mapping[str, "PublicClaim"],
        public_limitations_by_ref: Mapping[str, PublicLimitation],
    ) -> "PublicRecommendation":
        _assert_recommendation_integrity(recommendation)
        try:
            supporting_claims = tuple(
                public_claims_by_ref[claim_ref]
                for claim_ref in recommendation.supporting_claim_refs
            )
        except (KeyError, TypeError) as exc:
            raise NarrativeAuthorityContractError(
                "public_recommendation_claim_closure_invalid"
            ) from exc
        if not supporting_claims or any(
            type(claim) is not PublicClaim for claim in supporting_claims
        ):
            raise NarrativeAuthorityContractError(
                "public_recommendation_claim_closure_invalid"
            )
        try:
            risks = tuple(
                public_limitations_by_ref[risk_ref]
                for risk_ref in recommendation.risk_refs
            )
        except (KeyError, TypeError) as exc:
            raise NarrativeAuthorityContractError(
                "public_recommendation_risk_closure_invalid"
            ) from exc
        if any(type(item) is not PublicLimitation for item in risks):
            raise NarrativeAuthorityContractError(
                "public_recommendation_risk_closure_invalid"
            )
        public_commitments = tuple(
            PublicRecommendationCommitment.create(
                commitment=commitment,
                public_claims_by_ref=public_claims_by_ref,
            )
            for commitment in recommendation.commitments
        )
        _assert_no_restricted_text_literals(
            {
                "action": recommendation.action,
                "applicable_conditions": recommendation.applicable_conditions,
                "expected_decision_value": recommendation.expected_decision_value,
            },
            restricted_literals=frozenset(public_claims_by_ref),
            error="public_recommendation_internal_ref_forbidden",
        )
        body = {
            "recommendation_ref": recommendation.recommendation_ref,
            "recommendation_digest": recommendation.content_digest,
            "commitment_contract_version": (recommendation.commitment_contract_version),
            "recommendation_commitment_refs": (
                recommendation.recommendation_commitment_refs
            ),
            "commitments": public_commitments,
            "supporting_claim_refs": recommendation.supporting_claim_refs,
            "supporting_claim_handles": tuple(
                claim.claim_handle for claim in supporting_claims
            ),
            "assumption_refs": recommendation.assumption_refs,
            "risk_refs": recommendation.risk_refs,
            "risk_handles": tuple(item.limitation_handle for item in risks),
            "action": recommendation.action,
            "applicable_conditions": recommendation.applicable_conditions,
            "expected_decision_value": recommendation.expected_decision_value,
        }
        digest = canonical_digest(body)
        return cls(
            public_recommendation_ref="public-recommendation:sha256:" + digest,
            recommendation_handle=_stable_handle(
                "r", recommendation.recommendation_ref
            ),
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        recommendation: RecommendationRecord,
        public_claims_by_ref: Mapping[str, "PublicClaim"],
        public_limitations_by_ref: Mapping[str, PublicLimitation],
    ) -> "PublicRecommendation":
        payload = _strict_shape(
            payload,
            cls,
            "public_recommendation_shape_invalid",
        )
        raw_commitments = payload["commitments"]
        if isinstance(raw_commitments, (str, bytes)) or not isinstance(
            raw_commitments, Sequence
        ):
            raise NarrativeAuthorityContractError(
                "public_recommendation_commitments_invalid"
            )
        commitment_by_ref = {
            item.recommendation_commitment_ref: item
            for item in recommendation.commitments
        }
        try:
            replayed_commitments = tuple(
                PublicRecommendationCommitment.from_dict(
                    item,
                    commitment=commitment_by_ref[item["recommendation_commitment_ref"]],
                    public_claims_by_ref=public_claims_by_ref,
                )
                for item in raw_commitments
            )
        except (KeyError, TypeError) as exc:
            raise NarrativeAuthorityContractError(
                "public_recommendation_commitments_invalid"
            ) from exc
        rebuilt = cls.create(
            recommendation=recommendation,
            public_claims_by_ref=public_claims_by_ref,
            public_limitations_by_ref=public_limitations_by_ref,
        )
        if (
            replayed_commitments != rebuilt.commitments
            or rebuilt.to_dict() != canonical_value(payload)
        ):
            raise NarrativeAuthorityContractError(
                "public_recommendation_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def assert_integrity(self) -> None:
        for commitment in self.commitments:
            if type(commitment) is not PublicRecommendationCommitment:
                raise NarrativeAuthorityContractError(
                    "public_recommendation_integrity_invalid"
                )
            commitment.assert_integrity()
        commitment_refs = tuple(
            item.recommendation_commitment_ref for item in self.commitments
        )
        action_commitments = tuple(
            item for item in self.commitments if item.commitment_kind == "action"
        )
        outcome_commitments = tuple(
            item
            for item in self.commitments
            if item.commitment_kind == "expected_outcome"
        )
        if (
            self.commitment_contract_version
            != RECOMMENDATION_COMMITMENT_CONTRACT_VERSION
            or self.recommendation_commitment_refs != commitment_refs
            or len(commitment_refs) != len(set(commitment_refs))
            or len(action_commitments) != 1
            or len(outcome_commitments) != 1
            or action_commitments[0].text != self.action
            or outcome_commitments[0].text != self.expected_decision_value
            or {
                claim_ref
                for commitment in self.commitments
                for claim_ref in commitment.supporting_claim_refs
            }
            != set(self.supporting_claim_refs)
            or not self.supporting_claim_refs
            or len(self.supporting_claim_refs) != len(self.supporting_claim_handles)
            or len(set(self.supporting_claim_refs)) != len(self.supporting_claim_refs)
            or len(set(self.supporting_claim_handles))
            != len(self.supporting_claim_handles)
            or len(self.risk_refs) != len(self.risk_handles)
            or len(set(self.risk_handles)) != len(self.risk_handles)
            or len({commitment.commitment_handle for commitment in self.commitments})
            != len(self.commitments)
        ):
            raise NarrativeAuthorityContractError(
                "public_recommendation_integrity_invalid"
            )
        _assert_content_addressed_record(
            self,
            ref_field="public_recommendation_ref",
            digest_field="content_digest",
            prefix="public-recommendation:sha256:",
            excluded_fields=("recommendation_handle",),
            error="public_recommendation_integrity_invalid",
        )
        if self.recommendation_handle != _stable_handle("r", self.recommendation_ref):
            raise NarrativeAuthorityContractError(
                "public_recommendation_integrity_invalid"
            )

    def to_writer_payload(self) -> dict[str, Any]:
        self.assert_integrity()
        return {
            "recommendation_handle": self.recommendation_handle,
            "commitment_contract_version": self.commitment_contract_version,
            "commitments": [item.to_writer_payload() for item in self.commitments],
            "supporting_claim_handles": list(self.supporting_claim_handles),
            "risk_handles": list(self.risk_handles),
            "action": self.action,
            "applicable_conditions": list(self.applicable_conditions),
            "expected_decision_value": self.expected_decision_value,
        }


@dataclass(frozen=True)
class PublicClaim:
    public_claim_ref: str
    claim_handle: str
    claim_ref: str
    claim_key_ref: str
    claim_class: str
    publication_ceiling: ClaimPublicationCeiling
    subject: str
    metric_ref: str | None
    target_window_ref: str | None
    baseline_window_ref: str | None
    scope: str
    grain: str
    dimension_path: tuple[str, ...]
    facts: tuple[PublicFactDescriptor, ...]
    limitation_refs: tuple[str, ...]
    limitation_handles: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        claim: ClaimRevision,
        claim_key: ClaimKey,
        facts: Sequence[PublicFactDescriptor],
        limitations: Sequence[PublicLimitation],
    ) -> "PublicClaim":
        _assert_claim_integrity(claim)
        _assert_claim_key_integrity(claim_key)
        if claim.status != "verified":
            raise NarrativeAuthorityContractError("public_claim_authority_invalid")
        if type(claim_key) is not ClaimKey or claim.claim_key != claim_key.claim_key:
            raise NarrativeAuthorityContractError("public_claim_key_closure_invalid")
        normalized_facts = _typed_records(
            facts,
            PublicFactDescriptor,
            "fact_ref",
            "public_claim_facts_invalid",
            allow_empty=False,
        )
        if any(fact.claim_ref != claim.claim_ref for fact in normalized_facts):
            raise NarrativeAuthorityContractError("public_claim_fact_closure_invalid")
        for fact in normalized_facts:
            _assert_public_fact_claim_closure(fact, claim=claim)
        normalized_limitations = _typed_records(
            limitations,
            PublicLimitation,
            "limitation_ref",
            "public_claim_limitations_invalid",
        )
        normalized_limitations = tuple(
            PublicLimitation.from_dict(item.to_dict())
            for item in normalized_limitations
        )
        limitation_refs = tuple(item.limitation_ref for item in normalized_limitations)
        if limitation_refs != tuple(sorted(claim.limitation_refs)):
            raise NarrativeAuthorityContractError(
                "public_claim_limitation_closure_invalid"
            )
        body = {
            "claim_ref": claim.claim_ref,
            "claim_key_ref": claim_key.claim_key,
            "claim_class": claim.claim_class,
            "publication_ceiling": claim.publication_ceiling,
            "subject": claim_key.subject,
            "metric_ref": claim_key.metric_ref,
            "target_window_ref": claim_key.target_window_ref,
            "baseline_window_ref": claim_key.baseline_window_ref,
            "scope": claim_key.scope,
            "grain": claim_key.grain,
            "dimension_path": claim_key.dimension_path,
            "facts": normalized_facts,
            "limitation_refs": limitation_refs,
            "limitation_handles": tuple(
                item.limitation_handle for item in normalized_limitations
            ),
        }
        digest = canonical_digest(body)
        return cls(
            public_claim_ref="public-claim:sha256:" + digest,
            claim_handle=_stable_handle("c", claim.claim_ref),
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        claim: ClaimRevision,
        claim_key: ClaimKey,
        limitations_by_ref: Mapping[str, PublicLimitation],
    ) -> "PublicClaim":
        payload = _strict_shape(payload, cls, "public_claim_shape_invalid")
        raw_facts = payload.get("facts")
        if isinstance(raw_facts, (str, bytes)) or not isinstance(raw_facts, Sequence):
            raise NarrativeAuthorityContractError("public_claim_facts_invalid")
        _assert_claim_integrity(claim)
        facts = tuple(
            PublicFactDescriptor._from_dict_for_validated_claim(item, claim=claim)
            for item in raw_facts
        )
        try:
            limitations = tuple(
                limitations_by_ref[ref] for ref in payload["limitation_refs"]
            )
        except (KeyError, TypeError) as exc:
            raise NarrativeAuthorityContractError(
                "public_claim_limitation_closure_invalid"
            ) from exc
        rebuilt = cls.create(
            claim=claim,
            claim_key=claim_key,
            facts=facts,
            limitations=limitations,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError("public_claim_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def assert_integrity(self) -> None:
        try:
            ceiling = ClaimPublicationCeiling.from_dict(
                self.publication_ceiling.to_dict()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise NarrativeAuthorityContractError(
                "public_claim_integrity_invalid"
            ) from exc
        if (
            ceiling != self.publication_ceiling
            or ceiling.claim_class != self.claim_class
        ):
            raise NarrativeAuthorityContractError("public_claim_integrity_invalid")
        for fact in self.facts:
            fact.assert_integrity()
        _assert_content_addressed_record(
            self,
            ref_field="public_claim_ref",
            digest_field="content_digest",
            prefix="public-claim:sha256:",
            excluded_fields=("claim_handle",),
            error="public_claim_integrity_invalid",
        )
        if self.claim_handle != _stable_handle("c", self.claim_ref):
            raise NarrativeAuthorityContractError("public_claim_integrity_invalid")


@dataclass(frozen=True)
class PublicClaimPalette:
    palette_ref: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    authority_mode: str
    required_obligation_ids: tuple[str, ...]
    field_visibility_policy_ref: str
    field_visibility_policy_digest: str
    claims: tuple[PublicClaim, ...]
    recommendations: tuple[PublicRecommendation, ...]
    limitations: tuple[PublicLimitation, ...]
    content_digest: str

    @classmethod
    def derive(
        cls,
        *,
        authority_bundle: AuthorityBundle,
        claims: Sequence[ClaimRevision],
        claim_keys: Sequence[ClaimKey],
        recommendations: Sequence[RecommendationRecord],
        public_facts: Sequence[PublicFactDescriptor],
        public_limitations: Sequence[PublicLimitation],
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> "PublicClaimPalette":
        _assert_authority_bundle_integrity(authority_bundle)
        if type(visibility_policy) is not PublicationFieldVisibilityPolicy:
            raise NarrativeAuthorityContractError(
                "public_palette_visibility_policy_invalid"
            )
        visibility_policy = PublicationFieldVisibilityPolicy.from_dict(
            visibility_policy.to_dict()
        )
        authority_mode = authority_bundle.authority_mode
        if authority_mode not in {"claim_bearing", "boundary_only"}:
            raise NarrativeAuthorityContractError(
                "public_palette_authority_mode_invalid"
            )
        boundary_only = authority_mode == "boundary_only"
        normalized_claims = _typed_records(
            claims,
            ClaimRevision,
            "claim_ref",
            "public_palette_claims_invalid",
            allow_empty=boundary_only,
        )
        if tuple(item.claim_ref for item in normalized_claims) != tuple(
            sorted(authority_bundle.verified_claim_refs)
        ):
            raise NarrativeAuthorityContractError(
                "public_palette_claim_closure_invalid"
            )
        if any(item.status != "verified" for item in normalized_claims):
            raise NarrativeAuthorityContractError("public_palette_claim_status_invalid")
        for claim in normalized_claims:
            _assert_claim_integrity(claim)
        normalized_keys = _typed_records(
            claim_keys,
            ClaimKey,
            "claim_key",
            "public_palette_claim_keys_invalid",
            allow_empty=boundary_only,
        )
        if {item.claim_key for item in normalized_keys} != {
            item.claim_key for item in normalized_claims
        }:
            raise NarrativeAuthorityContractError(
                "public_palette_claim_key_closure_invalid"
            )
        for claim_key in normalized_keys:
            _assert_claim_key_integrity(claim_key)
        key_by_ref = {item.claim_key: item for item in normalized_keys}
        normalized_facts = _typed_records(
            public_facts,
            PublicFactDescriptor,
            "fact_ref",
            "public_palette_facts_invalid",
            allow_empty=boundary_only,
        )
        claim_ref_set = {item.claim_ref for item in normalized_claims}
        if {item.claim_ref for item in normalized_facts} != claim_ref_set:
            raise NarrativeAuthorityContractError("public_palette_fact_closure_invalid")
        claims_by_ref = {item.claim_ref: item for item in normalized_claims}
        forbidden_public_names = frozenset(visibility_policy.forbidden_fields)
        for fact in normalized_facts:
            _assert_public_fact_claim_closure(
                fact,
                claim=claims_by_ref[fact.claim_ref],
            )
            if fact.public_name in forbidden_public_names:
                raise NarrativeAuthorityContractError("public_fact_name_forbidden")
        normalized_limitations = _typed_records(
            public_limitations,
            PublicLimitation,
            "limitation_ref",
            "public_palette_limitations_invalid",
        )
        expected_limitation_refs = set(authority_bundle.limitation_refs)
        if {item.limitation_ref for item in normalized_limitations} != (
            expected_limitation_refs
        ):
            raise NarrativeAuthorityContractError(
                "public_palette_limitation_closure_invalid"
            )
        normalized_limitations = tuple(
            PublicLimitation.from_dict(item.to_dict())
            for item in normalized_limitations
        )
        facts_by_claim = {
            claim.claim_ref: tuple(
                fact for fact in normalized_facts if fact.claim_ref == claim.claim_ref
            )
            for claim in normalized_claims
        }
        limitations_by_ref = {
            item.limitation_ref: item for item in normalized_limitations
        }
        public_claims = tuple(
            PublicClaim.create(
                claim=claim,
                claim_key=key_by_ref[claim.claim_key],
                facts=facts_by_claim[claim.claim_ref],
                limitations=tuple(
                    limitations_by_ref[ref] for ref in claim.limitation_refs
                ),
            )
            for claim in normalized_claims
        )
        normalized_recommendations = _typed_records(
            recommendations,
            RecommendationRecord,
            "recommendation_ref",
            "public_palette_recommendations_invalid",
        )
        if tuple(
            item.recommendation_ref for item in normalized_recommendations
        ) != tuple(sorted(authority_bundle.recommendation_refs)):
            raise NarrativeAuthorityContractError(
                "public_palette_recommendation_closure_invalid"
            )
        public_claims_by_ref = {item.claim_ref: item for item in public_claims}
        for recommendation in normalized_recommendations:
            _assert_recommendation_integrity(recommendation)
            if (
                recommendation.authority_namespace_ref
                != authority_bundle.authority_namespace_ref
                or not set(recommendation.supporting_claim_refs).issubset(
                    public_claims_by_ref
                )
                or not set(recommendation.assumption_refs).issubset(
                    set(authority_bundle.assumption_refs)
                )
            ):
                raise NarrativeAuthorityContractError(
                    "public_palette_recommendation_authority_invalid"
                )
        public_recommendations = tuple(
            PublicRecommendation.create(
                recommendation=recommendation,
                public_claims_by_ref=public_claims_by_ref,
                public_limitations_by_ref=limitations_by_ref,
            )
            for recommendation in normalized_recommendations
        )
        handles = [item.claim_handle for item in public_claims]
        handles.extend(item.recommendation_handle for item in public_recommendations)
        handles.extend(
            commitment.commitment_handle
            for recommendation in public_recommendations
            for commitment in recommendation.commitments
        )
        handles.extend(item.fact_handle for item in normalized_facts)
        handles.extend(item.limitation_handle for item in normalized_limitations)
        if len(handles) != len(set(handles)):
            raise NarrativeAuthorityContractError("public_palette_handle_collision")
        body = {
            "authority_bundle_ref": authority_bundle.bundle_ref,
            "authority_bundle_digest": authority_bundle.bundle_digest,
            "authority_mode": authority_mode,
            "required_obligation_ids": authority_bundle.required_obligation_ids,
            "field_visibility_policy_ref": visibility_policy.policy_ref,
            "field_visibility_policy_digest": visibility_policy.content_digest,
            "claims": public_claims,
            "recommendations": public_recommendations,
            "limitations": normalized_limitations,
        }
        digest = canonical_digest(body)
        return cls(
            palette_ref="public-claim-palette:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_bundle: AuthorityBundle,
        claims: Sequence[ClaimRevision],
        claim_keys: Sequence[ClaimKey],
        recommendations: Sequence[RecommendationRecord],
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> "PublicClaimPalette":
        payload = _strict_shape(payload, cls, "public_palette_shape_invalid")
        required_obligation_ids = _string_tuple(
            payload["required_obligation_ids"],
            "public_palette_bundle_closure_invalid",
        )
        if (
            payload["authority_bundle_ref"] != authority_bundle.bundle_ref
            or payload["authority_bundle_digest"] != authority_bundle.bundle_digest
            or payload["authority_mode"] != authority_bundle.authority_mode
            or required_obligation_ids != authority_bundle.required_obligation_ids
        ):
            raise NarrativeAuthorityContractError(
                "public_palette_bundle_closure_invalid"
            )
        if (
            payload["field_visibility_policy_ref"] != visibility_policy.policy_ref
            or payload["field_visibility_policy_digest"]
            != visibility_policy.content_digest
        ):
            raise NarrativeAuthorityContractError(
                "public_palette_visibility_policy_closure_invalid"
            )
        raw_limitations = payload.get("limitations")
        raw_claims = payload.get("claims")
        raw_recommendations = payload.get("recommendations")
        if isinstance(raw_limitations, (str, bytes)) or not isinstance(
            raw_limitations, Sequence
        ):
            raise NarrativeAuthorityContractError("public_palette_limitations_invalid")
        if isinstance(raw_claims, (str, bytes)) or not isinstance(raw_claims, Sequence):
            raise NarrativeAuthorityContractError("public_palette_claims_invalid")
        if isinstance(raw_recommendations, (str, bytes)) or not isinstance(
            raw_recommendations, Sequence
        ):
            raise NarrativeAuthorityContractError(
                "public_palette_recommendations_invalid"
            )
        limitations = tuple(
            PublicLimitation.from_dict(item) for item in raw_limitations
        )
        limitations_by_ref = {item.limitation_ref: item for item in limitations}
        claims_by_ref = {item.claim_ref: item for item in claims}
        keys_by_ref = {item.claim_key: item for item in claim_keys}
        try:
            public_claims = tuple(
                PublicClaim.from_dict(
                    item,
                    claim=claims_by_ref[item["claim_ref"]],
                    claim_key=keys_by_ref[item["claim_key_ref"]],
                    limitations_by_ref=limitations_by_ref,
                )
                for item in raw_claims
            )
        except (KeyError, TypeError) as exc:
            raise NarrativeAuthorityContractError(
                "public_palette_claim_closure_invalid"
            ) from exc
        public_claims_by_ref = {item.claim_ref: item for item in public_claims}
        recommendations_by_ref = {
            item.recommendation_ref: item for item in recommendations
        }
        try:
            tuple(
                PublicRecommendation.from_dict(
                    item,
                    recommendation=recommendations_by_ref[item["recommendation_ref"]],
                    public_claims_by_ref=public_claims_by_ref,
                    public_limitations_by_ref=limitations_by_ref,
                )
                for item in raw_recommendations
            )
        except (KeyError, TypeError) as exc:
            raise NarrativeAuthorityContractError(
                "public_palette_recommendation_closure_invalid"
            ) from exc
        rebuilt = cls.derive(
            authority_bundle=authority_bundle,
            claims=claims,
            claim_keys=claim_keys,
            recommendations=recommendations,
            public_facts=tuple(
                fact for public_claim in public_claims for fact in public_claim.facts
            ),
            public_limitations=limitations,
            visibility_policy=visibility_policy,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError("public_palette_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def assert_integrity(
        self,
        *,
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> None:
        policy = PublicationFieldVisibilityPolicy.from_dict(visibility_policy.to_dict())
        required_obligation_ids = _string_tuple(
            self.required_obligation_ids,
            "public_palette_integrity_invalid",
        )
        if (
            self.field_visibility_policy_ref != policy.policy_ref
            or self.field_visibility_policy_digest != policy.content_digest
            or required_obligation_ids != self.required_obligation_ids
        ):
            raise NarrativeAuthorityContractError(
                "public_palette_visibility_policy_closure_invalid"
            )
        for limitation in self.limitations:
            limitation.assert_integrity()
        for public_claim in self.claims:
            public_claim.assert_integrity()
            for fact in public_claim.facts:
                policy.assert_public_name(fact.public_name)
        for recommendation in self.recommendations:
            recommendation.assert_integrity()
        claim_refs = tuple(claim.claim_ref for claim in self.claims)
        claim_handle_by_ref = {
            claim.claim_ref: claim.claim_handle for claim in self.claims
        }
        limitation_handle_by_ref = {
            limitation.limitation_ref: limitation.limitation_handle
            for limitation in self.limitations
        }
        handles = [claim.claim_handle for claim in self.claims]
        handles.extend(
            recommendation.recommendation_handle
            for recommendation in self.recommendations
        )
        handles.extend(
            commitment.commitment_handle
            for recommendation in self.recommendations
            for commitment in recommendation.commitments
        )
        handles.extend(
            fact.fact_handle for claim in self.claims for fact in claim.facts
        )
        handles.extend(item.limitation_handle for item in self.limitations)
        if (
            len(claim_refs) != len(set(claim_refs))
            or len(handles) != len(set(handles))
            or any(
                fact.claim_ref != claim.claim_ref
                for claim in self.claims
                for fact in claim.facts
            )
            or any(
                tuple(
                    claim_handle_by_ref.get(claim_ref)
                    for claim_ref in recommendation.supporting_claim_refs
                )
                != recommendation.supporting_claim_handles
                for recommendation in self.recommendations
            )
            or any(
                tuple(
                    claim_handle_by_ref.get(claim_ref)
                    for claim_ref in commitment.supporting_claim_refs
                )
                != commitment.supporting_claim_handles
                for recommendation in self.recommendations
                for commitment in recommendation.commitments
            )
            or any(
                tuple(
                    limitation_handle_by_ref.get(risk_ref)
                    for risk_ref in recommendation.risk_refs
                )
                != recommendation.risk_handles
                for recommendation in self.recommendations
            )
            or (self.authority_mode == "claim_bearing" and not self.claims)
            or (
                self.authority_mode == "boundary_only"
                and (self.claims or self.recommendations or not self.limitations)
            )
            or self.authority_mode not in {"claim_bearing", "boundary_only"}
        ):
            raise NarrativeAuthorityContractError("public_palette_integrity_invalid")
        _assert_content_addressed_record(
            self,
            ref_field="palette_ref",
            digest_field="content_digest",
            prefix="public-claim-palette:sha256:",
            error="public_palette_integrity_invalid",
        )


@dataclass(frozen=True)
class RestrictedProviderResponse:
    response_ref: str
    attempt_id: str
    purpose: str
    provider_ref: str
    model_ref: str
    input_ref: str
    input_digest: str
    attempt_number: int
    content: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        attempt_id: str,
        purpose: str,
        provider_ref: str,
        model_ref: str,
        input_ref: str,
        input_digest: str,
        attempt_number: int,
        content: str,
    ) -> "RestrictedProviderResponse":
        if purpose not in RESTRICTED_PROVIDER_RESPONSE_PURPOSES:
            raise NarrativeAuthorityContractError(
                "restricted_provider_response_purpose_invalid"
            )
        body = {
            "attempt_id": _required_string(
                attempt_id, "restricted_provider_response_attempt_id_invalid"
            ),
            "purpose": purpose,
            "provider_ref": _required_string(
                provider_ref, "restricted_provider_response_provider_ref_invalid"
            ),
            "model_ref": _required_string(
                model_ref, "restricted_provider_response_model_ref_invalid"
            ),
            "input_ref": _required_string(
                input_ref, "restricted_provider_response_input_ref_invalid"
            ),
            "input_digest": _digest_string(
                input_digest, "restricted_provider_response_input_digest_invalid"
            ),
            "attempt_number": _positive_integer(
                attempt_number,
                "restricted_provider_response_attempt_number_invalid",
            ),
            "content": _raw_text(
                content, "restricted_provider_response_content_invalid"
            ),
        }
        digest = canonical_digest(body)
        return cls(
            response_ref="restricted-provider-response:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "RestrictedProviderResponse":
        payload = _strict_shape(
            payload,
            cls,
            "restricted_provider_response_shape_invalid",
        )
        rebuilt = cls.create(
            attempt_id=payload["attempt_id"],
            purpose=payload["purpose"],
            provider_ref=payload["provider_ref"],
            model_ref=payload["model_ref"],
            input_ref=payload["input_ref"],
            input_digest=payload["input_digest"],
            attempt_number=payload["attempt_number"],
            content=payload["content"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError(
                "restricted_provider_response_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class NarrativeWriterAttempt:
    writer_attempt_ref: str
    attempt_id: str
    authority_bundle_ref: str
    material_projection_ref: str
    material_projection_digest: str
    input_ref: str
    input_digest: str
    attempt_number: int
    provider_ref: str
    model_ref: str
    provider_response_ref: str
    provider_response_digest: str
    provider_response: RestrictedProviderResponse
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_bundle_ref: str,
        material_projection_ref: str,
        material_projection_digest: str,
        input_ref: str,
        input_digest: str,
        attempt_number: int,
        provider_response: RestrictedProviderResponse,
    ) -> "NarrativeWriterAttempt":
        if type(provider_response) is not RestrictedProviderResponse:
            raise NarrativeAuthorityContractError(
                "narrative_writer_attempt_provider_response_invalid"
            )
        response = RestrictedProviderResponse.from_dict(provider_response.to_dict())
        if response.purpose != "narrative_writer":
            raise NarrativeAuthorityContractError(
                "narrative_writer_attempt_provider_response_purpose_invalid"
            )
        normalized_input_ref = _required_string(
            input_ref, "narrative_writer_attempt_input_ref_invalid"
        )
        normalized_input_digest = _digest_string(
            input_digest, "narrative_writer_attempt_input_digest_invalid"
        )
        normalized_attempt_number = _positive_integer(
            attempt_number, "narrative_writer_attempt_number_invalid"
        )
        if (
            response.input_ref != normalized_input_ref
            or response.input_digest != normalized_input_digest
            or response.attempt_number != normalized_attempt_number
        ):
            raise NarrativeAuthorityContractError(
                "narrative_writer_attempt_input_closure_invalid"
            )
        body = {
            "attempt_id": response.attempt_id,
            "authority_bundle_ref": _required_string(
                authority_bundle_ref,
                "narrative_writer_attempt_bundle_ref_invalid",
            ),
            "material_projection_ref": _required_string(
                material_projection_ref,
                "narrative_writer_attempt_material_projection_ref_invalid",
            ),
            "material_projection_digest": _digest_string(
                material_projection_digest,
                "narrative_writer_attempt_material_projection_digest_invalid",
            ),
            "input_ref": normalized_input_ref,
            "input_digest": normalized_input_digest,
            "attempt_number": normalized_attempt_number,
            "provider_ref": response.provider_ref,
            "model_ref": response.model_ref,
            "provider_response_ref": response.response_ref,
            "provider_response_digest": response.content_digest,
            "provider_response": response,
        }
        digest = canonical_digest(body)
        return cls(
            writer_attempt_ref="narrative-writer-attempt:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "NarrativeWriterAttempt":
        payload = _strict_shape(
            payload,
            cls,
            "narrative_writer_attempt_shape_invalid",
        )
        raw_response = payload.get("provider_response")
        if not isinstance(raw_response, Mapping):
            raise NarrativeAuthorityContractError(
                "narrative_writer_attempt_provider_response_invalid"
            )
        rebuilt = cls.create(
            authority_bundle_ref=payload["authority_bundle_ref"],
            material_projection_ref=payload["material_projection_ref"],
            material_projection_digest=payload["material_projection_digest"],
            input_ref=payload["input_ref"],
            input_digest=payload["input_digest"],
            attempt_number=payload["attempt_number"],
            provider_response=RestrictedProviderResponse.from_dict(raw_response),
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError(
                "narrative_writer_attempt_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class NarrativeFactBinding:
    binding_ref: str
    claim_handle: str
    fact_handle: str
    fact_kind: str
    value: str
    range_end: str | None
    unit: str | None
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        claim_handle: str,
        fact_handle: str,
        fact_kind: str,
        value: Any,
        range_end: Any,
        unit: str | None,
    ) -> "NarrativeFactBinding":
        normalized_value, normalized_end, normalized_unit = _normalize_fact_values(
            fact_kind=fact_kind,
            value=value,
            range_end=range_end,
            unit=unit,
            error="narrative_fact_binding",
        )
        body = {
            "claim_handle": _required_string(
                claim_handle, "narrative_fact_binding_claim_handle_invalid"
            ),
            "fact_handle": _required_string(
                fact_handle, "narrative_fact_binding_fact_handle_invalid"
            ),
            "fact_kind": fact_kind,
            "value": normalized_value,
            "range_end": normalized_end,
            "unit": normalized_unit,
        }
        digest = canonical_digest(body)
        return cls(
            binding_ref="narrative-fact-binding:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NarrativeFactBinding":
        payload = _strict_shape(payload, cls, "narrative_fact_binding_shape_invalid")
        rebuilt = cls.create(
            claim_handle=payload["claim_handle"],
            fact_handle=payload["fact_handle"],
            fact_kind=payload["fact_kind"],
            value=payload["value"],
            range_end=payload["range_end"],
            unit=payload["unit"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError(
                "narrative_fact_binding_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class NarrativeBlock:
    block_id: str
    writer_attempt_id: str
    role: str
    text: str
    requirement_handles: tuple[str, ...]
    claim_handles: tuple[str, ...]
    recommendation_handles: tuple[str, ...]
    limitation_handles: tuple[str, ...]
    material_fact_bindings: tuple[NarrativeFactBinding, ...]
    statement_role: str
    required: bool
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        writer_attempt_id: str,
        role: str,
        text: str,
        claim_handles: Sequence[str],
        recommendation_handles: Sequence[str],
        limitation_handles: Sequence[str],
        material_fact_bindings: Sequence[NarrativeFactBinding],
        statement_role: str,
        required: bool,
        requirement_handles: Sequence[str] = (),
    ) -> "NarrativeBlock":
        if role not in NARRATIVE_BLOCK_ROLES:
            raise NarrativeAuthorityContractError("narrative_block_role_invalid")
        claims = _string_tuple(
            claim_handles,
            "narrative_block_claim_handles_invalid",
        )
        recommendations = _string_tuple(
            recommendation_handles,
            "narrative_block_recommendation_handles_invalid",
        )
        requirements = _string_tuple(
            requirement_handles,
            "narrative_block_requirement_handles_invalid",
        )
        limitations = _string_tuple(
            limitation_handles, "narrative_block_limitation_handles_invalid"
        )
        if not narrative_block_authority_handles_are_valid(
            role=role,
            claim_handles=claims,
            recommendation_handles=recommendations,
            limitation_handles=limitations,
        ):
            raise NarrativeAuthorityContractError(
                "narrative_block_authority_handles_invalid"
            )
        bindings = _typed_records(
            material_fact_bindings,
            NarrativeFactBinding,
            "binding_ref",
            "narrative_block_fact_bindings_invalid",
        )
        bindings = tuple(
            NarrativeFactBinding.from_dict(binding.to_dict()) for binding in bindings
        )
        if any(binding.claim_handle not in claims for binding in bindings):
            raise NarrativeAuthorityContractError(
                "narrative_block_fact_claim_closure_invalid"
            )
        body = {
            "writer_attempt_id": _required_string(
                writer_attempt_id, "narrative_block_attempt_id_invalid"
            ),
            "role": role,
            "text": _raw_text(text, "narrative_block_text_invalid"),
            "requirement_handles": requirements,
            "claim_handles": claims,
            "recommendation_handles": recommendations,
            "limitation_handles": limitations,
            "material_fact_bindings": bindings,
            "statement_role": _required_string(
                statement_role, "narrative_block_statement_role_invalid"
            ),
            "required": _boolean(required, "narrative_block_required_invalid"),
        }
        digest = canonical_digest(body)
        return cls(
            block_id="narrative-block:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NarrativeBlock":
        payload = _strict_shape(payload, cls, "narrative_block_shape_invalid")
        raw_bindings = payload.get("material_fact_bindings")
        if isinstance(raw_bindings, (str, bytes)) or not isinstance(
            raw_bindings, Sequence
        ):
            raise NarrativeAuthorityContractError(
                "narrative_block_fact_bindings_invalid"
            )
        rebuilt = cls.create(
            writer_attempt_id=payload["writer_attempt_id"],
            role=payload["role"],
            text=payload["text"],
            requirement_handles=payload["requirement_handles"],
            claim_handles=payload["claim_handles"],
            recommendation_handles=payload["recommendation_handles"],
            limitation_handles=payload["limitation_handles"],
            material_fact_bindings=tuple(
                NarrativeFactBinding.from_dict(item) for item in raw_bindings
            ),
            statement_role=payload["statement_role"],
            required=payload["required"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError("narrative_block_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class NarrativeDocument:
    narrative_id: str
    authority_bundle_ref: str
    material_projection_ref: str
    material_projection_digest: str
    writer_attempt: NarrativeWriterAttempt
    parent_narrative_id: str | None
    blocks: tuple[NarrativeBlock, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_bundle_ref: str,
        material_projection_ref: str,
        material_projection_digest: str,
        writer_attempt: NarrativeWriterAttempt,
        parent_narrative_id: str | None,
        blocks: Sequence[NarrativeBlock],
    ) -> "NarrativeDocument":
        if type(writer_attempt) is not NarrativeWriterAttempt:
            raise NarrativeAuthorityContractError(
                "narrative_document_writer_attempt_invalid"
            )
        attempt = NarrativeWriterAttempt.from_dict(writer_attempt.to_dict())
        normalized_bundle_ref = _required_string(
            authority_bundle_ref, "narrative_document_bundle_ref_invalid"
        )
        normalized_projection_ref = _required_string(
            material_projection_ref,
            "narrative_document_material_projection_ref_invalid",
        )
        normalized_projection_digest = _digest_string(
            material_projection_digest,
            "narrative_document_material_projection_digest_invalid",
        )
        if (
            attempt.authority_bundle_ref != normalized_bundle_ref
            or attempt.material_projection_ref != normalized_projection_ref
            or attempt.material_projection_digest != normalized_projection_digest
        ):
            raise NarrativeAuthorityContractError(
                "narrative_document_writer_attempt_closure_invalid"
            )
        normalized_blocks = _typed_records(
            blocks,
            NarrativeBlock,
            "block_id",
            "narrative_document_blocks_invalid",
            allow_empty=False,
            sort=False,
        )
        if any(
            NarrativeBlock.from_dict(block.to_dict()) != block
            for block in normalized_blocks
        ):
            raise NarrativeAuthorityContractError("narrative_document_blocks_invalid")
        parent_id = _optional_string(
            parent_narrative_id, "narrative_document_parent_id_invalid"
        )
        current_attempt_block_count = sum(
            block.writer_attempt_id == attempt.attempt_id for block in normalized_blocks
        )
        if parent_id is None and current_attempt_block_count != len(normalized_blocks):
            raise NarrativeAuthorityContractError(
                "narrative_document_initial_attempt_closure_invalid"
            )
        if parent_id is not None and current_attempt_block_count == 0:
            raise NarrativeAuthorityContractError(
                "narrative_document_revision_attempt_closure_invalid"
            )
        body = {
            "authority_bundle_ref": normalized_bundle_ref,
            "material_projection_ref": normalized_projection_ref,
            "material_projection_digest": normalized_projection_digest,
            "writer_attempt": attempt,
            "parent_narrative_id": parent_id,
            "blocks": normalized_blocks,
        }
        digest = canonical_digest(body)
        return cls(
            narrative_id="narrative-document:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NarrativeDocument":
        payload = _strict_shape(payload, cls, "narrative_document_shape_invalid")
        raw_blocks = payload.get("blocks")
        raw_writer_attempt = payload.get("writer_attempt")
        if not isinstance(raw_writer_attempt, Mapping):
            raise NarrativeAuthorityContractError(
                "narrative_document_writer_attempt_invalid"
            )
        if isinstance(raw_blocks, (str, bytes)) or not isinstance(raw_blocks, Sequence):
            raise NarrativeAuthorityContractError("narrative_document_blocks_invalid")
        rebuilt = cls.create(
            authority_bundle_ref=payload["authority_bundle_ref"],
            material_projection_ref=payload["material_projection_ref"],
            material_projection_digest=payload["material_projection_digest"],
            writer_attempt=NarrativeWriterAttempt.from_dict(raw_writer_attempt),
            parent_narrative_id=payload["parent_narrative_id"],
            blocks=tuple(NarrativeBlock.from_dict(item) for item in raw_blocks),
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError(
                "narrative_document_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @property
    def writer_attempt_id(self) -> str:
        return self.writer_attempt.attempt_id

    @property
    def writer_input_ref(self) -> str:
        return self.writer_attempt.input_ref

    @property
    def writer_input_digest(self) -> str:
        return self.writer_attempt.input_digest

    @property
    def raw_provider_response_ref(self) -> str:
        return self.writer_attempt.provider_response_ref


@dataclass(frozen=True)
class SensitiveOutputFinding:
    finding_ref: str
    block_id: str
    field_visibility_policy_ref: str
    policy_rule_ref: str
    material_ref: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        block_id: str,
        field_visibility_policy_ref: str,
        policy_rule_ref: str,
        material_ref: str,
    ) -> "SensitiveOutputFinding":
        body = {
            "block_id": _required_string(
                block_id, "sensitive_output_finding_block_id_invalid"
            ),
            "field_visibility_policy_ref": _required_string(
                field_visibility_policy_ref,
                "sensitive_output_finding_visibility_policy_ref_invalid",
            ),
            "policy_rule_ref": _required_string(
                policy_rule_ref, "sensitive_output_finding_policy_rule_ref_invalid"
            ),
            "material_ref": _required_string(
                material_ref, "sensitive_output_finding_material_ref_invalid"
            ),
        }
        digest = canonical_digest(body)
        return cls(
            finding_ref="sensitive-output-finding:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SensitiveOutputFinding":
        payload = _strict_shape(payload, cls, "sensitive_output_finding_shape_invalid")
        rebuilt = cls.create(
            block_id=payload["block_id"],
            field_visibility_policy_ref=payload["field_visibility_policy_ref"],
            policy_rule_ref=payload["policy_rule_ref"],
            material_ref=payload["material_ref"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError(
                "sensitive_output_finding_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def assert_integrity(self) -> None:
        SensitiveOutputFinding.from_dict(self.to_dict())


@dataclass(frozen=True)
class BlockLocalIssue:
    issue_ref: str
    block_id: str
    code: str
    affected_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        block_id: str,
        code: str,
        affected_refs: Sequence[str],
    ) -> "BlockLocalIssue":
        if code not in LOCAL_BLOCK_ISSUE_CODES:
            raise NarrativeAuthorityContractError("block_local_issue_code_invalid")
        body = {
            "block_id": _required_string(
                block_id, "block_local_issue_block_id_invalid"
            ),
            "code": code,
            "affected_refs": _string_tuple(
                affected_refs,
                "block_local_issue_affected_refs_invalid",
                allow_empty=False,
            ),
        }
        digest = canonical_digest(body)
        return cls(
            issue_ref="block-local-issue:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BlockLocalIssue":
        payload = _strict_shape(payload, cls, "block_local_issue_shape_invalid")
        rebuilt = cls.create(
            block_id=payload["block_id"],
            code=payload["code"],
            affected_refs=payload["affected_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError("block_local_issue_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def _ranking_position_binding_gaps(
    *,
    block: NarrativeBlock,
    claims_by_handle: Mapping[str, Any],
    materials_by_handle: Mapping[str, Any],
    facts_by_handle: Mapping[str, tuple[str, Any]],
) -> tuple[tuple[str, ...], ...]:
    """Require typed positions whenever one block compares multiple ranked items."""

    binding_pairs = {
        (binding.claim_handle, binding.fact_handle)
        for binding in block.material_fact_bindings
    }
    ranking_groups: dict[
        tuple[str, str, str, str, str], set[tuple[str, str]]
    ] = {}
    for binding in block.material_fact_bindings:
        fact_record = facts_by_handle.get(binding.fact_handle)
        claim = claims_by_handle.get(binding.claim_handle)
        if fact_record is None or claim is None:
            continue
        material_handle, fact = fact_record
        if material_handle not in set(claim.material_handles):
            continue
        material = materials_by_handle.get(material_handle)
        if material is None or not isinstance(material.interpretation_contract, Mapping):
            continue
        contract = material.interpretation_contract
        fields = tuple(
            contract.get(name)
            for name in (
                "ranking_scope",
                "ranking_measure",
                "ranking_order",
                "ranking_position_measure",
                "priority_rank_order",
            )
        )
        if any(not isinstance(value, str) or not value for value in fields):
            continue
        if fact.name != fields[1]:
            continue
        ranking_groups.setdefault(fields, set()).add(
            (binding.claim_handle, material_handle)
        )

    gaps: list[tuple[str, ...]] = []
    for group, ranked_pairs in ranking_groups.items():
        if len(ranked_pairs) < 2:
            continue
        position_measure = group[3]
        for claim_handle, material_handle in sorted(ranked_pairs):
            material = materials_by_handle[material_handle]
            position_facts = tuple(
                fact for fact in material.facts if fact.name == position_measure
            )
            if len(position_facts) != 1:
                gaps.append((claim_handle, material_handle))
                continue
            position_fact = position_facts[0]
            if (claim_handle, position_fact.fact_handle) not in binding_pairs:
                gaps.append(
                    (claim_handle, material_handle, position_fact.fact_handle)
                )
    return tuple(dict.fromkeys(gaps))


def _internal_fact_name_exposure_refs(
    *,
    block: NarrativeBlock,
    refs_by_name: Mapping[str, tuple[str, ...]],
    name_pattern: re.Pattern[str] | None,
) -> tuple[str, ...]:
    """Reject exact machine field names copied into customer prose."""

    if name_pattern is None:
        return ()
    exposed = {
        ref
        for match in name_pattern.finditer(block.text)
        for ref in refs_by_name.get(match.group(0), ())
    }
    return tuple(sorted(exposed))


def _internal_fact_name_index(
    materials_by_handle: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, ...]], re.Pattern[str] | None]:
    refs: dict[str, list[str]] = {}
    for material in materials_by_handle.values():
        for fact in material.facts:
            if "_" in fact.name:
                refs.setdefault(fact.name, []).append(fact.projected_fact_ref)
    normalized = {
        name: tuple(sorted(set(values))) for name, values in refs.items()
    }
    if not normalized:
        return normalized, None
    alternatives = "|".join(
        re.escape(name)
        for name in sorted(normalized, key=lambda item: (-len(item), item))
    )
    return (
        normalized,
        re.compile(
            rf"(?<![A-Za-z0-9_])(?:{alternatives})(?![A-Za-z0-9_])"
        ),
    )


@dataclass(frozen=True)
class BlockLocalValidationReport:
    local_report_ref: str
    narrative_id: str
    narrative_digest: str
    material_projection_ref: str
    material_projection_digest: str
    evaluated_block_ids: tuple[str, ...]
    accepted_block_ids: tuple[str, ...]
    rejected_block_ids: tuple[str, ...]
    sensitive_output_findings: tuple[SensitiveOutputFinding, ...]
    issues: tuple[BlockLocalIssue, ...]
    content_digest: str

    @classmethod
    def validate(
        cls,
        *,
        narrative: NarrativeDocument,
        material_projection: "NarrativeMaterialProjection",
        visibility_policy: PublicationFieldVisibilityPolicy,
        sensitive_output_findings: Sequence[SensitiveOutputFinding],
    ) -> "BlockLocalValidationReport":
        from bi_agent.runtime.narrative_material_projection import (
            NarrativeMaterialProjection,
        )

        if type(narrative) is not NarrativeDocument:
            raise NarrativeAuthorityContractError("block_local_narrative_invalid")
        if type(material_projection) is not NarrativeMaterialProjection:
            raise NarrativeAuthorityContractError(
                "block_local_material_projection_invalid"
            )
        narrative = NarrativeDocument.from_dict(narrative.to_dict())
        material_projection.assert_integrity()
        if (
            narrative.material_projection_ref != material_projection.projection_ref
            or narrative.material_projection_digest
            != material_projection.content_digest
        ):
            raise NarrativeAuthorityContractError(
                "block_local_material_projection_closure_invalid"
            )
        findings = _typed_records(
            sensitive_output_findings,
            SensitiveOutputFinding,
            "finding_ref",
            "block_local_sensitive_findings_invalid",
        )
        findings = tuple(
            SensitiveOutputFinding.from_dict(finding.to_dict()) for finding in findings
        )
        block_by_id = {item.block_id: item for item in narrative.blocks}
        if any(item.block_id not in block_by_id for item in findings):
            raise NarrativeAuthorityContractError(
                "block_local_sensitive_finding_closure_invalid"
            )
        if any(
            item.field_visibility_policy_ref != visibility_policy.policy_ref
            for item in findings
        ):
            raise NarrativeAuthorityContractError(
                "block_local_sensitive_finding_policy_closure_invalid"
            )
        findings_by_block: dict[str, list[SensitiveOutputFinding]] = {}
        for finding in findings:
            findings_by_block.setdefault(finding.block_id, []).append(finding)

        claims_by_handle = {
            item.claim_handle: item for item in material_projection.claims
        }
        recommendations_by_handle = {
            item.recommendation_handle: item
            for item in material_projection.recommendations
        }
        limitations_by_handle = {
            item.limitation_handle: item for item in material_projection.limitations
        }
        requirements_by_handle = {
            item.requirement_handle: item
            for item in material_projection.publication_requirements
        }
        facts_by_handle: dict[str, tuple[str, Any]] = {}
        materials_by_handle = {
            item.material_handle: item
            for item in material_projection.evidence_materials
        }
        internal_fact_refs_by_name, internal_fact_name_pattern = (
            _internal_fact_name_index(materials_by_handle)
        )
        for material in material_projection.evidence_materials:
            for fact in material.facts:
                facts_by_handle[fact.fact_handle] = (
                    material.material_handle,
                    fact,
                )

        issues: list[BlockLocalIssue] = []
        accepted: list[str] = []
        rejected: list[str] = []
        for block in narrative.blocks:
            block_issues: list[BlockLocalIssue] = []
            known_claims = tuple(
                claims_by_handle[handle]
                for handle in block.claim_handles
                if handle in claims_by_handle
            )
            known_recommendations = tuple(
                recommendations_by_handle[handle]
                for handle in block.recommendation_handles
                if handle in recommendations_by_handle
            )
            known_requirements = tuple(
                requirements_by_handle[handle]
                for handle in block.requirement_handles
                if handle in requirements_by_handle
            )
            for handle in block.requirement_handles:
                if handle not in requirements_by_handle:
                    block_issues.append(
                        BlockLocalIssue.create(
                            block_id=block.block_id,
                            code="unknown_requirement_handle",
                            affected_refs=(handle,),
                        )
                    )
            for handle in block.claim_handles:
                if handle not in claims_by_handle:
                    block_issues.append(
                        BlockLocalIssue.create(
                            block_id=block.block_id,
                            code="unknown_claim_handle",
                            affected_refs=(handle,),
                        )
                    )
            for handle in block.recommendation_handles:
                if handle not in recommendations_by_handle:
                    block_issues.append(
                        BlockLocalIssue.create(
                            block_id=block.block_id,
                            code="unknown_recommendation_handle",
                            affected_refs=(handle,),
                        )
                    )
            if block.requirement_handles:
                issue_refs = {
                    requirement.issue_ref for requirement in known_requirements
                }
                allowed_claim_handles = {
                    handle
                    for requirement in known_requirements
                    for handle in requirement.claim_handles
                }
                allowed_limitation_handles = {
                    handle
                    for requirement in known_requirements
                    for handle in requirement.limitation_handles
                }
                allowed_limitation_handles.update(
                    limitation_handle
                    for claim_handle in allowed_claim_handles
                    if claim_handle in claims_by_handle
                    for limitation_handle in claims_by_handle[
                        claim_handle
                    ].limitation_handles
                )
                scope_violations = tuple(
                    sorted(
                        set(block.claim_handles) - allowed_claim_handles
                        | set(block.recommendation_handles)
                        | (
                            set(block.limitation_handles)
                            - allowed_limitation_handles
                        )
                    )
                )
                if (
                    not known_requirements
                    or None in issue_refs
                    or len(issue_refs) != 1
                    or scope_violations
                ):
                    block_issues.append(
                        BlockLocalIssue.create(
                            block_id=block.block_id,
                            code="question_requirement_scope_mismatch",
                            affected_refs=(
                                scope_violations or block.requirement_handles
                            ),
                        )
                    )
            else:
                allowed_limitation_handles = {
                    handle
                    for claim in known_claims
                    for handle in claim.limitation_handles
                }
            if block.role == "boundary" and not block.requirement_handles:
                allowed_limitation_handles = {
                    item.limitation_handle for item in material_projection.limitations
                }
            elif not block.requirement_handles:
                allowed_limitation_handles.update(
                    handle
                    for recommendation in known_recommendations
                    for handle in recommendation.risk_handles
                )
            for handle in block.limitation_handles:
                if handle not in limitations_by_handle:
                    code = "unknown_limitation_handle"
                elif handle not in allowed_limitation_handles:
                    code = "limitation_claim_scope_mismatch"
                else:
                    continue
                block_issues.append(
                    BlockLocalIssue.create(
                        block_id=block.block_id,
                        code=code,
                        affected_refs=(handle,),
                    )
                )
            for binding in block.material_fact_bindings:
                expected = facts_by_handle.get(binding.fact_handle)
                if expected is None:
                    block_issues.append(
                        BlockLocalIssue.create(
                            block_id=block.block_id,
                            code="unknown_fact_handle",
                            affected_refs=(binding.fact_handle,),
                        )
                    )
                    continue
                expected_material_handle, expected_fact = expected
                expected_claim = claims_by_handle.get(binding.claim_handle)
                if (
                    expected_claim is None
                    or expected_material_handle
                    not in set(expected_claim.material_handles)
                    or binding.fact_kind != expected_fact.fact_kind
                    or binding.value != expected_fact.value
                    or binding.range_end != expected_fact.range_end
                    or binding.unit != expected_fact.unit
                ):
                    block_issues.append(
                        BlockLocalIssue.create(
                            block_id=block.block_id,
                            code="material_fact_binding_mismatch",
                            affected_refs=(
                                binding.binding_ref,
                                expected_fact.projected_fact_ref,
                                expected_material_handle,
                            ),
                        )
                    )
            for affected_refs in _ranking_position_binding_gaps(
                block=block,
                claims_by_handle=claims_by_handle,
                materials_by_handle=materials_by_handle,
                facts_by_handle=facts_by_handle,
            ):
                block_issues.append(
                    BlockLocalIssue.create(
                        block_id=block.block_id,
                        code="ranking_position_binding_incomplete",
                        affected_refs=affected_refs,
                    )
                )
            exposed_fact_refs = _internal_fact_name_exposure_refs(
                block=block,
                refs_by_name=internal_fact_refs_by_name,
                name_pattern=internal_fact_name_pattern,
            )
            if exposed_fact_refs:
                block_issues.append(
                    BlockLocalIssue.create(
                        block_id=block.block_id,
                        code="internal_fact_name_exposed",
                        affected_refs=exposed_fact_refs,
                    )
                )
            for finding in findings_by_block.get(block.block_id, ()):
                block_issues.append(
                    BlockLocalIssue.create(
                        block_id=block.block_id,
                        code="sensitive_output_policy_violation",
                        affected_refs=(
                            finding.field_visibility_policy_ref,
                            finding.policy_rule_ref,
                            finding.material_ref,
                        ),
                    )
                )
            if block_issues:
                rejected.append(block.block_id)
                issues.extend(block_issues)
            else:
                accepted.append(block.block_id)
        normalized_issues = _typed_records(
            issues,
            BlockLocalIssue,
            "issue_ref",
            "block_local_issues_invalid",
        )
        normalized_issues = tuple(
            BlockLocalIssue.from_dict(issue.to_dict()) for issue in normalized_issues
        )
        body = {
            "narrative_id": narrative.narrative_id,
            "narrative_digest": narrative.content_digest,
            "material_projection_ref": material_projection.projection_ref,
            "material_projection_digest": material_projection.content_digest,
            "evaluated_block_ids": tuple(sorted(block_by_id)),
            "accepted_block_ids": tuple(sorted(accepted)),
            "rejected_block_ids": tuple(sorted(rejected)),
            "sensitive_output_findings": findings,
            "issues": normalized_issues,
        }
        digest = canonical_digest(body)
        return cls(
            local_report_ref="block-local-report:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        narrative: NarrativeDocument,
        material_projection: "NarrativeMaterialProjection",
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> "BlockLocalValidationReport":
        payload = _strict_shape(payload, cls, "block_local_report_shape_invalid")
        raw_findings = payload.get("sensitive_output_findings")
        if isinstance(raw_findings, (str, bytes)) or not isinstance(
            raw_findings, Sequence
        ):
            raise NarrativeAuthorityContractError(
                "block_local_sensitive_findings_invalid"
            )
        rebuilt = cls.validate(
            narrative=narrative,
            material_projection=material_projection,
            visibility_policy=visibility_policy,
            sensitive_output_findings=tuple(
                SensitiveOutputFinding.from_dict(item) for item in raw_findings
            ),
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError(
                "block_local_report_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class BlockVerificationAttempt:
    verification_attempt_ref: str
    attempt_id: str
    narrative_id: str
    narrative_digest: str
    local_report_ref: str
    local_report_digest: str
    input_ref: str
    input_digest: str
    attempt_number: int
    provider_ref: str
    model_ref: str
    provider_response_ref: str
    provider_response_digest: str
    provider_response: RestrictedProviderResponse
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        narrative: NarrativeDocument,
        local_report: BlockLocalValidationReport,
        input_ref: str,
        input_digest: str,
        attempt_number: int,
        provider_response: RestrictedProviderResponse,
    ) -> "BlockVerificationAttempt":
        if type(narrative) is not NarrativeDocument:
            raise NarrativeAuthorityContractError(
                "block_verification_attempt_narrative_invalid"
            )
        replayed_narrative = NarrativeDocument.from_dict(narrative.to_dict())
        if type(local_report) is not BlockLocalValidationReport:
            raise NarrativeAuthorityContractError(
                "block_verification_attempt_local_report_invalid"
            )
        _assert_content_addressed_record(
            local_report,
            ref_field="local_report_ref",
            digest_field="content_digest",
            prefix="block-local-report:sha256:",
            error="block_verification_attempt_local_report_integrity_invalid",
        )
        if (
            local_report.narrative_id != replayed_narrative.narrative_id
            or local_report.narrative_digest != replayed_narrative.content_digest
        ):
            raise NarrativeAuthorityContractError(
                "block_verification_attempt_local_report_closure_invalid"
            )
        if type(provider_response) is not RestrictedProviderResponse:
            raise NarrativeAuthorityContractError(
                "block_verification_attempt_provider_response_invalid"
            )
        response = RestrictedProviderResponse.from_dict(provider_response.to_dict())
        if response.purpose != "block_verification":
            raise NarrativeAuthorityContractError(
                "block_verification_attempt_provider_response_purpose_invalid"
            )
        normalized_input_ref = _required_string(
            input_ref, "block_verification_attempt_input_ref_invalid"
        )
        normalized_input_digest = _digest_string(
            input_digest, "block_verification_attempt_input_digest_invalid"
        )
        normalized_attempt_number = _positive_integer(
            attempt_number, "block_verification_attempt_number_invalid"
        )
        if (
            response.input_ref != normalized_input_ref
            or response.input_digest != normalized_input_digest
            or response.attempt_number != normalized_attempt_number
        ):
            raise NarrativeAuthorityContractError(
                "block_verification_attempt_input_closure_invalid"
            )
        if (
            response.attempt_id == replayed_narrative.writer_attempt_id
            or response.input_ref == replayed_narrative.writer_input_ref
            or response.response_ref == replayed_narrative.raw_provider_response_ref
        ):
            raise NarrativeAuthorityContractError(
                "block_verification_attempt_independence_invalid"
            )
        body = {
            "attempt_id": response.attempt_id,
            "narrative_id": replayed_narrative.narrative_id,
            "narrative_digest": replayed_narrative.content_digest,
            "local_report_ref": local_report.local_report_ref,
            "local_report_digest": local_report.content_digest,
            "input_ref": normalized_input_ref,
            "input_digest": normalized_input_digest,
            "attempt_number": normalized_attempt_number,
            "provider_ref": response.provider_ref,
            "model_ref": response.model_ref,
            "provider_response_ref": response.response_ref,
            "provider_response_digest": response.content_digest,
            "provider_response": response,
        }
        digest = canonical_digest(body)
        return cls(
            verification_attempt_ref="block-verification-attempt:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        narrative: NarrativeDocument,
        local_report: BlockLocalValidationReport,
    ) -> "BlockVerificationAttempt":
        payload = _strict_shape(
            payload,
            cls,
            "block_verification_attempt_shape_invalid",
        )
        raw_response = payload.get("provider_response")
        if not isinstance(raw_response, Mapping):
            raise NarrativeAuthorityContractError(
                "block_verification_attempt_provider_response_invalid"
            )
        rebuilt = cls.create(
            narrative=narrative,
            local_report=local_report,
            input_ref=payload["input_ref"],
            input_digest=payload["input_digest"],
            attempt_number=payload["attempt_number"],
            provider_response=RestrictedProviderResponse.from_dict(raw_response),
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError(
                "block_verification_attempt_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class BlockVeto:
    veto_ref: str
    narrative_id: str
    block_id: str
    reason_code: str
    affected_claim_handles: tuple[str, ...]
    affected_recommendation_handles: tuple[str, ...]
    limitation_handles: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        narrative_id: str,
        block_id: str,
        reason_code: str,
        affected_claim_handles: Sequence[str],
        affected_recommendation_handles: Sequence[str],
        limitation_handles: Sequence[str],
    ) -> "BlockVeto":
        claims = _string_tuple(
            affected_claim_handles,
            "block_veto_claim_handles_invalid",
        )
        recommendations = _string_tuple(
            affected_recommendation_handles,
            "block_veto_recommendation_handles_invalid",
        )
        limitations = _string_tuple(
            limitation_handles, "block_veto_limitation_handles_invalid"
        )
        if not claims and not recommendations and not limitations:
            raise NarrativeAuthorityContractError("block_veto_handles_invalid")
        body = {
            "narrative_id": _required_string(
                narrative_id, "block_veto_narrative_id_invalid"
            ),
            "block_id": _required_string(block_id, "block_veto_block_id_invalid"),
            "reason_code": _required_string(
                reason_code, "block_veto_reason_code_invalid"
            ),
            "affected_claim_handles": claims,
            "affected_recommendation_handles": recommendations,
            "limitation_handles": limitations,
        }
        digest = canonical_digest(body)
        return cls(
            veto_ref="block-veto:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BlockVeto":
        payload = _strict_shape(payload, cls, "block_veto_shape_invalid")
        rebuilt = cls.create(
            narrative_id=payload["narrative_id"],
            block_id=payload["block_id"],
            reason_code=payload["reason_code"],
            affected_claim_handles=payload["affected_claim_handles"],
            affected_recommendation_handles=payload["affected_recommendation_handles"],
            limitation_handles=payload["limitation_handles"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError("block_veto_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class BlockVerifierReport:
    verifier_report_ref: str
    audit_status: str
    verification_attempt_ref: str | None
    verification_attempt_digest: str | None
    verification_attempt: BlockVerificationAttempt | None
    narrative_id: str
    narrative_digest: str
    local_report_ref: str
    local_report_digest: str
    input_ref: str
    input_digest: str
    evaluated_block_ids: tuple[str, ...]
    accepted_block_ids: tuple[str, ...]
    rejected_block_ids: tuple[str, ...]
    vetoes: tuple[BlockVeto, ...]
    failure_kind: str | None
    retryability: str | None
    technical_detail_ref: str | None
    content_digest: str

    @staticmethod
    def _replay_sources(
        *,
        narrative: NarrativeDocument,
        material_projection: "NarrativeMaterialProjection",
        visibility_policy: PublicationFieldVisibilityPolicy,
        local_report: BlockLocalValidationReport,
    ) -> tuple[NarrativeDocument, BlockLocalValidationReport]:
        if type(narrative) is not NarrativeDocument:
            raise NarrativeAuthorityContractError("block_verifier_narrative_invalid")
        replayed_narrative = NarrativeDocument.from_dict(narrative.to_dict())
        if type(local_report) is not BlockLocalValidationReport:
            raise NarrativeAuthorityContractError("block_verifier_local_report_invalid")
        replayed_local = BlockLocalValidationReport.from_dict(
            local_report.to_dict(),
            narrative=replayed_narrative,
            material_projection=material_projection,
            visibility_policy=visibility_policy,
        )
        if replayed_local.narrative_id != replayed_narrative.narrative_id:
            raise NarrativeAuthorityContractError("block_verifier_local_report_invalid")
        return replayed_narrative, replayed_local

    @classmethod
    def create(
        cls,
        *,
        narrative: NarrativeDocument,
        material_projection: "NarrativeMaterialProjection",
        visibility_policy: PublicationFieldVisibilityPolicy,
        local_report: BlockLocalValidationReport,
        verification_attempt: BlockVerificationAttempt,
        accepted_block_ids: Sequence[str],
        vetoes: Sequence[BlockVeto],
    ) -> "BlockVerifierReport":
        narrative, local_report = cls._replay_sources(
            narrative=narrative,
            material_projection=material_projection,
            visibility_policy=visibility_policy,
            local_report=local_report,
        )
        if type(verification_attempt) is not BlockVerificationAttempt:
            raise NarrativeAuthorityContractError(
                "block_verifier_verification_attempt_invalid"
            )
        attempt = BlockVerificationAttempt.from_dict(
            verification_attempt.to_dict(),
            narrative=narrative,
            local_report=local_report,
        )
        evaluated = tuple(sorted(block.block_id for block in narrative.blocks))
        if evaluated != local_report.evaluated_block_ids:
            raise NarrativeAuthorityContractError(
                "block_verifier_evaluated_blocks_invalid"
            )
        accepted = _string_tuple(
            accepted_block_ids, "block_verifier_accepted_blocks_invalid"
        )
        if not set(accepted).issubset(set(local_report.accepted_block_ids)):
            raise NarrativeAuthorityContractError(
                "block_verifier_accepted_blocks_invalid"
            )
        normalized_vetoes = _typed_records(
            vetoes,
            BlockVeto,
            "veto_ref",
            "block_verifier_vetoes_invalid",
        )
        normalized_vetoes = tuple(
            BlockVeto.from_dict(veto.to_dict()) for veto in normalized_vetoes
        )
        veto_block_ids = tuple(sorted(item.block_id for item in normalized_vetoes))
        if len(veto_block_ids) != len(set(veto_block_ids)):
            raise NarrativeAuthorityContractError("block_verifier_vetoes_invalid")
        semantic_rejected = tuple(
            sorted(set(local_report.accepted_block_ids) - set(accepted))
        )
        if veto_block_ids != semantic_rejected:
            raise NarrativeAuthorityContractError("block_verifier_partition_invalid")
        rejected = tuple(sorted(set(evaluated) - set(accepted)))
        block_by_id = {item.block_id: item for item in narrative.blocks}
        for veto in normalized_vetoes:
            block = block_by_id.get(veto.block_id)
            if veto.narrative_id != narrative.narrative_id or block is None:
                raise NarrativeAuthorityContractError(
                    "block_verifier_veto_block_closure_invalid"
                )
            if (
                not set(veto.affected_claim_handles).issubset(set(block.claim_handles))
                or not set(veto.affected_recommendation_handles).issubset(
                    set(block.recommendation_handles)
                )
                or not set(veto.limitation_handles).issubset(
                    set(block.limitation_handles)
                )
            ):
                raise NarrativeAuthorityContractError(
                    "block_verifier_veto_handle_closure_invalid"
                )
        body = {
            "audit_status": "completed",
            "verification_attempt_ref": attempt.verification_attempt_ref,
            "verification_attempt_digest": attempt.content_digest,
            "verification_attempt": attempt,
            "narrative_id": narrative.narrative_id,
            "narrative_digest": narrative.content_digest,
            "local_report_ref": local_report.local_report_ref,
            "local_report_digest": local_report.content_digest,
            "input_ref": attempt.input_ref,
            "input_digest": attempt.input_digest,
            "evaluated_block_ids": evaluated,
            "accepted_block_ids": accepted,
            "rejected_block_ids": rejected,
            "vetoes": normalized_vetoes,
            "failure_kind": None,
            "retryability": None,
            "technical_detail_ref": None,
        }
        digest = canonical_digest(body)
        return cls(
            verifier_report_ref="block-verifier-report:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        narrative: NarrativeDocument,
        material_projection: "NarrativeMaterialProjection",
        visibility_policy: PublicationFieldVisibilityPolicy,
        local_report: BlockLocalValidationReport,
        input_ref: str,
        input_digest: str,
        failure_kind: str,
        retryability: str,
        technical_detail_ref: str,
    ) -> "BlockVerifierReport":
        narrative, local_report = cls._replay_sources(
            narrative=narrative,
            material_projection=material_projection,
            visibility_policy=visibility_policy,
            local_report=local_report,
        )
        normalized_input_ref = _required_string(
            input_ref,
            "block_verifier_input_ref_invalid",
        )
        if not normalized_input_ref.startswith("narrative-provider-input:sha256:"):
            raise NarrativeAuthorityContractError("block_verifier_input_ref_invalid")
        normalized_input_digest = _digest_string(
            input_digest,
            "block_verifier_input_digest_invalid",
        )
        normalized_failure_kind = _required_string(
            failure_kind,
            "block_verifier_failure_kind_invalid",
        )
        normalized_retryability = _required_string(
            retryability,
            "block_verifier_retryability_invalid",
        )
        if normalized_retryability not in {"retryable", "not_retryable"}:
            raise NarrativeAuthorityContractError(
                "block_verifier_retryability_invalid"
            )
        normalized_detail_ref = _required_string(
            technical_detail_ref,
            "block_verifier_technical_detail_ref_invalid",
        )
        detail_prefix = "technical-detail:sha256:"
        if (
            not normalized_detail_ref.startswith(detail_prefix)
            or len(normalized_detail_ref.removeprefix(detail_prefix)) != 64
        ):
            raise NarrativeAuthorityContractError(
                "block_verifier_technical_detail_ref_invalid"
            )
        body = {
            "audit_status": "unavailable",
            "verification_attempt_ref": None,
            "verification_attempt_digest": None,
            "verification_attempt": None,
            "narrative_id": narrative.narrative_id,
            "narrative_digest": narrative.content_digest,
            "local_report_ref": local_report.local_report_ref,
            "local_report_digest": local_report.content_digest,
            "input_ref": normalized_input_ref,
            "input_digest": normalized_input_digest,
            "evaluated_block_ids": (),
            "accepted_block_ids": (),
            "rejected_block_ids": (),
            "vetoes": (),
            "failure_kind": normalized_failure_kind,
            "retryability": normalized_retryability,
            "technical_detail_ref": normalized_detail_ref,
        }
        digest = canonical_digest(body)
        return cls(
            verifier_report_ref="block-verifier-report:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        narrative: NarrativeDocument,
        material_projection: "NarrativeMaterialProjection",
        visibility_policy: PublicationFieldVisibilityPolicy,
        local_report: BlockLocalValidationReport,
    ) -> "BlockVerifierReport":
        payload = _strict_shape(payload, cls, "block_verifier_report_shape_invalid")
        raw_attempt = payload.get("verification_attempt")
        raw_vetoes = payload.get("vetoes")
        if isinstance(raw_vetoes, (str, bytes)) or not isinstance(raw_vetoes, Sequence):
            raise NarrativeAuthorityContractError("block_verifier_vetoes_invalid")
        if payload["audit_status"] == "completed":
            if not isinstance(raw_attempt, Mapping):
                raise NarrativeAuthorityContractError(
                    "block_verifier_verification_attempt_invalid"
                )
            rebuilt = cls.create(
                narrative=narrative,
                material_projection=material_projection,
                visibility_policy=visibility_policy,
                local_report=local_report,
                verification_attempt=BlockVerificationAttempt.from_dict(
                    raw_attempt,
                    narrative=narrative,
                    local_report=local_report,
                ),
                accepted_block_ids=payload["accepted_block_ids"],
                vetoes=tuple(BlockVeto.from_dict(item) for item in raw_vetoes),
            )
        elif payload["audit_status"] == "unavailable":
            if raw_attempt is not None or raw_vetoes:
                raise NarrativeAuthorityContractError(
                    "block_verifier_unavailable_shape_invalid"
                )
            rebuilt = cls.unavailable(
                narrative=narrative,
                material_projection=material_projection,
                visibility_policy=visibility_policy,
                local_report=local_report,
                input_ref=payload["input_ref"],
                input_digest=payload["input_digest"],
                failure_kind=payload["failure_kind"],
                retryability=payload["retryability"],
                technical_detail_ref=payload["technical_detail_ref"],
            )
        else:
            raise NarrativeAuthorityContractError(
                "block_verifier_audit_status_invalid"
            )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeAuthorityContractError(
                "block_verifier_report_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @property
    def verifier_attempt_id(self) -> str | None:
        if self.verification_attempt is None:
            return None
        return self.verification_attempt.attempt_id

    @property
    def verifier_input_ref(self) -> str:
        return self.input_ref

    @property
    def verifier_input_digest(self) -> str:
        return self.input_digest

    @property
    def raw_provider_response_ref(self) -> str | None:
        if self.verification_attempt is None:
            return None
        return self.verification_attempt.provider_response_ref
