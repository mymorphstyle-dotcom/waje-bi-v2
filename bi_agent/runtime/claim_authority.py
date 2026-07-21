from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value

if TYPE_CHECKING:
    from bi_agent.runtime.claim_settlement import AuthorityBundleInputs


class ClaimAuthorityContractError(ValueError):
    pass


SUPPORT_EDGE_KINDS = frozenset(
    {"supports", "qualifies", "depends_on", "contradicts", "contextualizes"}
)
SUPPORT_SOURCE_TYPES = frozenset({"evidence", "claim", "assumption"})
CLAIM_CLASSES = frozenset(
    {
        "observed_fact",
        "accounting_identity_contribution",
        "dimension_localization",
        "statistical_association",
        "candidate_mechanism",
        "candidate_impact",
        "causal_effect",
        "scenario",
        "boundary",
    }
)
CLAIM_STATUSES = frozenset({"proposed", "verified", "withheld"})
OBLIGATION_COVERAGE_STATES = frozenset(
    {
        "satisfied",
        "contradicted",
        "mixed",
        "unavailable",
        "unresolved",
        "not_requested",
    }
)
CLAIM_GRAPH_MODES = frozenset({"claim_bearing", "boundary_only"})
VERIFICATION_PURPOSES = frozenset({"claim_settlement", "recommendation"})
VERIFICATION_DISPOSITIONS = frozenset({"accepted", "vetoed"})
VERIFICATION_VETO_BASES = frozenset(
    {
        "evidence_requirement_unsatisfied",
        "factual_support_invalid",
        "semantic_boundary_exceeded",
        "contract_or_provenance_invalid",
        "assumption_or_limitation_conflict",
        "recommendation_support_invalid",
    }
)
RECOMMENDATION_COMMITMENT_CONTRACT_VERSION = "recommendation-commitments.v1"
RECOMMENDATION_COMMITMENT_KINDS = frozenset(
    {"diagnostic_premise", "action", "expected_outcome"}
)
RECOMMENDATION_DIAGNOSTIC_MODES = frozenset(
    {
        "descriptive",
        "accounting",
        "localization",
        "association",
        "candidate",
        "causal",
        "scenario",
        "boundary",
    }
)
RECOMMENDATION_ACTION_DOMAINS = frozenset(
    {"analysis", "data_quality", "business_operation"}
)
RECOMMENDATION_ACTION_STAGES = frozenset(
    {"investigate", "validate", "experiment", "intervene", "scale"}
)
RECOMMENDATION_EXPECTED_VALUE_KINDS = frozenset(
    {"information_gain", "data_quality_improvement", "business_metric_effect"}
)
RECOMMENDATION_EXPECTED_VALUE_MODES = frozenset(
    {"hypothesis", "conditional_scenario", "expected_effect"}
)

_CLASS_STRENGTH_ORDER = {
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
    "causal_effect": ("causal_effect",),
    "scenario": ("scenario",),
    "boundary": ("boundary", "trust_boundary"),
}
_CANDIDATE_SUPPORT_CLASSES = frozenset(
    {
        "observed_fact",
        "accounting_identity_contribution",
        "dimension_localization",
        "statistical_association",
        "candidate_mechanism",
    }
)

_BASE_RECOMMENDATION_ACTIONS = frozenset(
    (domain, stage)
    for domain in RECOMMENDATION_ACTION_DOMAINS
    for stage in ("investigate", "validate")
)
_EXPERIMENT_RECOMMENDATION_ACTIONS = frozenset(
    {
        ("analysis", "experiment"),
        ("business_operation", "experiment"),
    }
)
_RECOMMENDATION_ACTIONS_BY_CLAIM_CLASS = {
    "observed_fact": _BASE_RECOMMENDATION_ACTIONS | _EXPERIMENT_RECOMMENDATION_ACTIONS,
    "accounting_identity_contribution": _BASE_RECOMMENDATION_ACTIONS
    | _EXPERIMENT_RECOMMENDATION_ACTIONS,
    "dimension_localization": _BASE_RECOMMENDATION_ACTIONS
    | _EXPERIMENT_RECOMMENDATION_ACTIONS,
    "statistical_association": _BASE_RECOMMENDATION_ACTIONS
    | _EXPERIMENT_RECOMMENDATION_ACTIONS,
    "candidate_mechanism": _BASE_RECOMMENDATION_ACTIONS
    | _EXPERIMENT_RECOMMENDATION_ACTIONS,
    "candidate_impact": _BASE_RECOMMENDATION_ACTIONS
    | _EXPERIMENT_RECOMMENDATION_ACTIONS,
    "causal_effect": _BASE_RECOMMENDATION_ACTIONS
    | _EXPERIMENT_RECOMMENDATION_ACTIONS
    | frozenset(
        {
            ("business_operation", "intervene"),
            ("business_operation", "scale"),
        }
    ),
    "scenario": _BASE_RECOMMENDATION_ACTIONS
    | frozenset(
        {
            ("business_operation", "intervene"),
            ("business_operation", "scale"),
        }
    ),
    "boundary": _BASE_RECOMMENDATION_ACTIONS
    | frozenset({("data_quality", "intervene")}),
}
_RECOMMENDATION_EXPECTED_VALUES_BY_CLAIM_CLASS = {
    claim_class: frozenset(
        {
            ("information_gain", "hypothesis"),
            ("information_gain", "expected_effect"),
        }
    )
    for claim_class in CLAIM_CLASSES
}
for _noncausal_class in (
    "observed_fact",
    "accounting_identity_contribution",
    "dimension_localization",
    "statistical_association",
    "candidate_mechanism",
    "candidate_impact",
):
    _RECOMMENDATION_EXPECTED_VALUES_BY_CLAIM_CLASS[_noncausal_class] |= frozenset(
        {("business_metric_effect", "hypothesis")}
    )
_RECOMMENDATION_EXPECTED_VALUES_BY_CLAIM_CLASS["causal_effect"] |= frozenset(
    {
        ("business_metric_effect", "hypothesis"),
        ("business_metric_effect", "conditional_scenario"),
        ("business_metric_effect", "expected_effect"),
    }
)
_RECOMMENDATION_EXPECTED_VALUES_BY_CLAIM_CLASS["scenario"] |= frozenset(
    {("business_metric_effect", "conditional_scenario")}
)
_RECOMMENDATION_EXPECTED_VALUES_BY_CLAIM_CLASS["boundary"] |= frozenset(
    {
        ("data_quality_improvement", "hypothesis"),
        ("data_quality_improvement", "expected_effect"),
    }
)


def _plain(value: Any) -> Any:
    return canonical_value(value)


def _freeze(value: Any, error: str) -> Any:
    try:
        normalized = canonical_value(value)
    except ValueError as exc:
        raise ClaimAuthorityContractError(error) from exc
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item, error) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_freeze(item, error) for item in normalized)
    return normalized


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ClaimAuthorityContractError(error)
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
        raise ClaimAuthorityContractError(error)
    return value


def _integer(value: Any, error: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ClaimAuthorityContractError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ClaimAuthorityContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise ClaimAuthorityContractError(error)
    if len(normalized) != len(set(normalized)):
        raise ClaimAuthorityContractError(error)
    return tuple(sorted(normalized)) if sort else normalized


def _strict_shape(payload: Any, record_type: type, error: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != set(
        record_type.__dataclass_fields__
    ):
        raise ClaimAuthorityContractError(error)
    return payload


def _aware_iso(value: str | datetime, error: str) -> str:
    try:
        parsed = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
    except ValueError as exc:
        raise ClaimAuthorityContractError(error) from exc
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ClaimAuthorityContractError(error)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _simple_replay(value: Any, record_type: type, error: str) -> Any:
    if type(value) is not record_type:
        raise ClaimAuthorityContractError(error)
    try:
        replayed = record_type.from_dict(value.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ClaimAuthorityContractError(error) from exc
    if replayed != value:
        raise ClaimAuthorityContractError(error)
    return replayed


def _simple_records(
    value: Any,
    record_type: type,
    identity_field: str,
    error: str,
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ClaimAuthorityContractError(error)
    records = tuple(_simple_replay(item, record_type, error) for item in value)
    identities = tuple(str(getattr(item, identity_field)) for item in records)
    if len(identities) != len(set(identities)):
        raise ClaimAuthorityContractError(error)
    return tuple(sorted(records, key=lambda item: str(getattr(item, identity_field))))


def _namespace_token(authority_namespace_ref: str) -> str:
    prefix = "claim-authority-namespace:sha256:"
    if not authority_namespace_ref.startswith(prefix):
        raise ClaimAuthorityContractError("claim_authority_namespace_ref_invalid")
    return authority_namespace_ref.removeprefix(prefix)[:24]


def _record_ref(kind: str, authority_namespace_ref: str, digest: str) -> str:
    return f"{kind}:{_namespace_token(authority_namespace_ref)}:sha256:{digest}"


def _require_namespace(value: Any, expected_ref: str, error: str) -> None:
    if getattr(value, "authority_namespace_ref", None) != expected_ref:
        raise ClaimAuthorityContractError(error)


def _supports_epistemic_class(source_class: str, target_class: str) -> bool:
    if target_class in {"candidate_mechanism", "candidate_impact"}:
        return source_class in _CANDIDATE_SUPPORT_CLASSES
    return source_class == target_class


def _supports_publication_strength(
    source: "ClaimPublicationCeiling",
    target: "ClaimPublicationCeiling",
) -> bool:
    if source.claim_class != target.claim_class:
        return target.claim_class in {
            "candidate_mechanism",
            "candidate_impact",
        } and (source.claim_class in _CANDIDATE_SUPPORT_CLASSES)
    strengths = _CLASS_STRENGTH_ORDER[target.claim_class]
    return strengths.index(target.strength) <= strengths.index(source.strength)


def _recommendation_diagnostic_modes_for_ceiling(
    ceiling: "ClaimPublicationCeiling",
) -> frozenset[str]:
    claim_class = ceiling.claim_class
    strength = ceiling.strength
    if claim_class == "observed_fact":
        return frozenset({"descriptive"})
    if claim_class == "accounting_identity_contribution":
        return frozenset({"descriptive", "accounting"})
    if claim_class == "dimension_localization":
        modes = {"descriptive", "localization"}
        if strength == "candidate_driver":
            modes.add("candidate")
        return frozenset(modes)
    if claim_class == "statistical_association":
        modes = {"descriptive"}
        if strength in {
            "statistical_association",
            "candidate_driver",
            "recurring_pattern",
        }:
            modes.add("association")
        if strength in {"anomaly_candidate", "candidate_driver"}:
            modes.add("candidate")
        return frozenset(modes)
    if claim_class in {"candidate_mechanism", "candidate_impact"}:
        return frozenset({"candidate"})
    if claim_class == "causal_effect":
        return frozenset({"descriptive", "association", "candidate", "causal"})
    if claim_class == "scenario":
        return frozenset({"scenario"})
    return frozenset({"boundary"})


def recommendation_authorization_for_ceiling(
    publication_ceiling: "ClaimPublicationCeiling",
) -> Mapping[str, Any]:
    ceiling = _simple_replay(
        publication_ceiling,
        ClaimPublicationCeiling,
        "recommendation_publication_ceiling_invalid",
    )
    return MappingProxyType(
        {
            "diagnostic_modes": tuple(
                sorted(_recommendation_diagnostic_modes_for_ceiling(ceiling))
            ),
            "actions": tuple(
                {
                    "action_domain": action_domain,
                    "action_stage": action_stage,
                }
                for action_domain, action_stage in sorted(
                    _RECOMMENDATION_ACTIONS_BY_CLAIM_CLASS[ceiling.claim_class]
                )
            ),
            "expected_values": tuple(
                {
                    "expected_value_kind": expected_value_kind,
                    "expected_value_mode": expected_value_mode,
                }
                for expected_value_kind, expected_value_mode in sorted(
                    _RECOMMENDATION_EXPECTED_VALUES_BY_CLAIM_CLASS[ceiling.claim_class]
                )
            ),
        }
    )


@dataclass(frozen=True)
class ClaimAuthorityNamespace:
    authority_namespace_ref: str
    run_attempt_id: str
    intent_revision_id: str
    plan_revision_id: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_attempt_id: str,
        intent_revision_id: str,
        plan_revision_id: str,
    ) -> "ClaimAuthorityNamespace":
        body = {
            "run_attempt_id": _required_string(
                run_attempt_id, "claim_authority_run_attempt_id_invalid"
            ),
            "intent_revision_id": _required_string(
                intent_revision_id, "claim_authority_intent_revision_id_invalid"
            ),
            "plan_revision_id": _required_string(
                plan_revision_id, "claim_authority_plan_revision_id_invalid"
            ),
        }
        digest = canonical_digest(body)
        return cls(
            authority_namespace_ref="claim-authority-namespace:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClaimAuthorityNamespace":
        payload = _strict_shape(payload, cls, "claim_authority_namespace_shape_invalid")
        rebuilt = cls.create(
            run_attempt_id=payload["run_attempt_id"],
            intent_revision_id=payload["intent_revision_id"],
            plan_revision_id=payload["plan_revision_id"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError(
                "claim_authority_namespace_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ClaimPublicationCeiling:
    claim_class: str
    strength: str

    @classmethod
    def create(cls, *, claim_class: str, strength: str) -> "ClaimPublicationCeiling":
        if claim_class not in CLAIM_CLASSES:
            raise ClaimAuthorityContractError("claim_publication_ceiling_class_invalid")
        strength = _required_string(
            strength, "claim_publication_ceiling_strength_invalid"
        )
        if strength not in _CLASS_STRENGTH_ORDER[claim_class]:
            raise ClaimAuthorityContractError(
                "claim_publication_ceiling_strength_invalid"
            )
        return cls(claim_class=claim_class, strength=strength)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClaimPublicationCeiling":
        payload = _strict_shape(payload, cls, "claim_publication_ceiling_shape_invalid")
        return cls.create(
            claim_class=payload["claim_class"], strength=payload["strength"]
        )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ClaimKey:
    claim_key: str
    authority_namespace_ref: str
    goal_id: str
    claim_kind: str
    subject: str
    metric_ref: str | None
    target_window_ref: str | None
    baseline_window_ref: str | None
    scope: str
    grain: str
    dimension_path: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        goal_id: str,
        claim_kind: str,
        subject: str,
        metric_ref: str | None,
        target_window_ref: str | None,
        baseline_window_ref: str | None,
        scope: str,
        grain: str,
        dimension_path: Sequence[str],
    ) -> "ClaimKey":
        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "claim_key_authority_namespace_invalid",
        )
        body = {
            "goal_id": _required_string(goal_id, "claim_key_goal_id_invalid"),
            "claim_kind": _required_string(claim_kind, "claim_key_claim_kind_invalid"),
            "subject": _required_string(subject, "claim_key_subject_invalid"),
            "metric_ref": _optional_string(metric_ref, "claim_key_metric_ref_invalid"),
            "target_window_ref": _optional_string(
                target_window_ref, "claim_key_target_window_ref_invalid"
            ),
            "baseline_window_ref": _optional_string(
                baseline_window_ref, "claim_key_baseline_window_ref_invalid"
            ),
            "scope": _required_string(scope, "claim_key_scope_invalid"),
            "grain": _required_string(grain, "claim_key_grain_invalid"),
            "dimension_path": _string_tuple(
                dimension_path, "claim_key_dimension_path_invalid", sort=False
            ),
        }
        digest = canonical_digest(body)
        return cls(
            claim_key=_record_ref(
                "claim-key", namespace.authority_namespace_ref, digest
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
    ) -> "ClaimKey":
        payload = _strict_shape(payload, cls, "claim_key_shape_invalid")
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            **{
                key: payload[key]
                for key in payload
                if key not in {"claim_key", "authority_namespace_ref", "content_digest"}
            },
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError("claim_key_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class SupportEdge:
    support_edge_ref: str
    authority_namespace_ref: str
    kind: str
    source_type: str
    source_ref: str
    source_epistemic_class: str
    source_publication_ceiling: ClaimPublicationCeiling
    target_claim_key: str
    limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        kind: str,
        source_type: str,
        source_ref: str,
        source_epistemic_class: str,
        source_publication_ceiling: ClaimPublicationCeiling,
        target_claim_key: str,
        limitation_refs: Sequence[str],
    ) -> "SupportEdge":
        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "support_edge_authority_namespace_invalid",
        )
        ceiling = _simple_replay(
            source_publication_ceiling,
            ClaimPublicationCeiling,
            "support_edge_publication_ceiling_invalid",
        )
        if kind not in SUPPORT_EDGE_KINDS:
            raise ClaimAuthorityContractError("support_edge_kind_invalid")
        if source_type not in SUPPORT_SOURCE_TYPES:
            raise ClaimAuthorityContractError("support_edge_source_type_invalid")
        if source_epistemic_class not in CLAIM_CLASSES:
            raise ClaimAuthorityContractError("support_edge_epistemic_class_invalid")
        if ceiling.claim_class != source_epistemic_class:
            raise ClaimAuthorityContractError(
                "support_edge_publication_ceiling_invalid"
            )
        if kind == "depends_on" and source_type != "claim":
            raise ClaimAuthorityContractError("support_edge_dependency_type_invalid")
        if source_type == "assumption" and source_epistemic_class != "scenario":
            raise ClaimAuthorityContractError("support_edge_assumption_class_invalid")
        target = _required_string(
            target_claim_key, "support_edge_target_claim_key_invalid"
        )
        if f":{_namespace_token(namespace.authority_namespace_ref)}:" not in target:
            raise ClaimAuthorityContractError("support_edge_target_namespace_invalid")
        body = {
            "kind": kind,
            "source_type": source_type,
            "source_ref": _required_string(
                source_ref, "support_edge_source_ref_invalid"
            ),
            "source_epistemic_class": source_epistemic_class,
            "source_publication_ceiling": ceiling,
            "target_claim_key": target,
            "limitation_refs": _string_tuple(
                limitation_refs, "support_edge_limitation_refs_invalid"
            ),
        }
        digest = canonical_digest(body)
        return cls(
            support_edge_ref=_record_ref(
                "claim-support-edge", namespace.authority_namespace_ref, digest
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
    ) -> "SupportEdge":
        payload = _strict_shape(payload, cls, "support_edge_shape_invalid")
        raw_ceiling = payload.get("source_publication_ceiling")
        if not isinstance(raw_ceiling, Mapping):
            raise ClaimAuthorityContractError(
                "support_edge_publication_ceiling_invalid"
            )
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            kind=payload["kind"],
            source_type=payload["source_type"],
            source_ref=payload["source_ref"],
            source_epistemic_class=payload["source_epistemic_class"],
            source_publication_ceiling=ClaimPublicationCeiling.from_dict(raw_ceiling),
            target_claim_key=payload["target_claim_key"],
            limitation_refs=payload["limitation_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError("support_edge_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ClaimRevision:
    claim_ref: str
    authority_namespace_ref: str
    claim_key: str
    factual_payload: Mapping[str, Any]
    claim_class: str
    support_edge_refs: tuple[str, ...]
    dependency_claim_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    status: str
    publication_ceiling: ClaimPublicationCeiling
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        claim_key: ClaimKey,
        factual_payload: Mapping[str, Any],
        claim_class: str,
        support_edges: Sequence[SupportEdge],
        dependency_claim_refs: Sequence[str],
        limitation_refs: Sequence[str],
        status: str,
        publication_ceiling: ClaimPublicationCeiling,
    ) -> "ClaimRevision":
        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "claim_revision_authority_namespace_invalid",
        )
        if type(claim_key) is not ClaimKey:
            raise ClaimAuthorityContractError("claim_revision_key_invalid")
        try:
            key = ClaimKey.from_dict(claim_key.to_dict(), authority_namespace=namespace)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimAuthorityContractError("claim_revision_key_invalid") from exc
        if key != claim_key:
            raise ClaimAuthorityContractError("claim_revision_key_invalid")
        ceiling = _simple_replay(
            publication_ceiling,
            ClaimPublicationCeiling,
            "claim_revision_publication_ceiling_incompatible",
        )
        if claim_class not in CLAIM_CLASSES:
            raise ClaimAuthorityContractError("claim_revision_class_invalid")
        if status not in CLAIM_STATUSES:
            raise ClaimAuthorityContractError("claim_revision_status_invalid")
        if ceiling.claim_class != claim_class:
            raise ClaimAuthorityContractError(
                "claim_revision_publication_ceiling_incompatible"
            )
        if not isinstance(factual_payload, Mapping) or not factual_payload:
            raise ClaimAuthorityContractError("claim_revision_factual_payload_invalid")
        frozen_payload = _freeze(
            factual_payload, "claim_revision_factual_payload_invalid"
        )
        if isinstance(support_edges, (str, bytes)) or not isinstance(
            support_edges, Sequence
        ):
            raise ClaimAuthorityContractError("claim_revision_support_edges_invalid")
        edges: list[SupportEdge] = []
        for raw_edge in support_edges:
            if type(raw_edge) is not SupportEdge:
                raise ClaimAuthorityContractError(
                    "claim_revision_support_edges_invalid"
                )
            try:
                replayed = SupportEdge.from_dict(
                    raw_edge.to_dict(), authority_namespace=namespace
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise ClaimAuthorityContractError(
                    "claim_revision_support_edges_invalid"
                ) from exc
            if replayed != raw_edge:
                raise ClaimAuthorityContractError(
                    "claim_revision_support_edges_invalid"
                )
            edges.append(replayed)
        edges_tuple = tuple(sorted(edges, key=lambda item: item.support_edge_ref))
        if len({item.support_edge_ref for item in edges_tuple}) != len(edges_tuple):
            raise ClaimAuthorityContractError("claim_revision_support_edges_invalid")
        if not edges_tuple or any(
            edge.target_claim_key != key.claim_key for edge in edges_tuple
        ):
            raise ClaimAuthorityContractError("claim_revision_support_edges_invalid")
        authorizing_edges = tuple(
            edge for edge in edges_tuple if edge.kind == "supports"
        )
        if not authorizing_edges or any(
            not _supports_epistemic_class(edge.source_epistemic_class, claim_class)
            for edge in authorizing_edges
        ):
            raise ClaimAuthorityContractError("claim_support_epistemic_class_invalid")
        if claim_class == "candidate_impact":
            authorizing_classes = {
                edge.source_epistemic_class for edge in authorizing_edges
            }
            if (
                not {"observed_fact", "candidate_mechanism"}.issubset(
                    authorizing_classes
                )
                or "causal_effect" in authorizing_classes
            ):
                raise ClaimAuthorityContractError(
                    "claim_candidate_impact_composite_support_invalid"
                )
        if any(
            not _supports_publication_strength(edge.source_publication_ceiling, ceiling)
            for edge in authorizing_edges
        ):
            raise ClaimAuthorityContractError("claim_support_strength_ceiling_exceeded")
        dependencies = _string_tuple(
            dependency_claim_refs, "claim_revision_dependency_refs_invalid"
        )
        expected_dependencies = tuple(
            sorted(edge.source_ref for edge in edges_tuple if edge.kind == "depends_on")
        )
        if dependencies != expected_dependencies:
            raise ClaimAuthorityContractError(
                "claim_revision_dependency_closure_invalid"
            )
        limitations = _string_tuple(
            limitation_refs, "claim_revision_limitation_refs_invalid"
        )
        if not {ref for edge in edges_tuple for ref in edge.limitation_refs}.issubset(
            limitations
        ):
            raise ClaimAuthorityContractError(
                "claim_revision_limitation_closure_invalid"
            )
        body = {
            "claim_key": key.claim_key,
            "factual_payload": frozen_payload,
            "claim_class": claim_class,
            "support_edge_refs": tuple(edge.support_edge_ref for edge in edges_tuple),
            "dependency_claim_refs": dependencies,
            "limitation_refs": limitations,
            "status": status,
            "publication_ceiling": ceiling,
        }
        digest = canonical_digest(body)
        return cls(
            claim_ref=_record_ref("claim", namespace.authority_namespace_ref, digest),
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
        claim_key: ClaimKey,
        support_edges: Sequence[SupportEdge],
    ) -> "ClaimRevision":
        payload = _strict_shape(payload, cls, "claim_revision_shape_invalid")
        raw_ceiling = payload.get("publication_ceiling")
        if not isinstance(raw_ceiling, Mapping):
            raise ClaimAuthorityContractError(
                "claim_revision_publication_ceiling_invalid"
            )
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            claim_key=claim_key,
            factual_payload=payload["factual_payload"],
            claim_class=payload["claim_class"],
            support_edges=support_edges,
            dependency_claim_refs=payload["dependency_claim_refs"],
            limitation_refs=payload["limitation_refs"],
            status=payload["status"],
            publication_ceiling=ClaimPublicationCeiling.from_dict(raw_ceiling),
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError("claim_revision_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ClaimVeto:
    veto_ref: str
    authority_namespace_ref: str
    claim_ref: str
    reason_code: str
    limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        claim_ref: str,
        reason_code: str,
        limitation_refs: Sequence[str],
    ) -> "ClaimVeto":
        namespace = _simple_replay(
            authority_namespace, ClaimAuthorityNamespace, "claim_veto_namespace_invalid"
        )
        body = {
            "claim_ref": _required_string(claim_ref, "claim_veto_claim_ref_invalid"),
            "reason_code": _required_string(
                reason_code, "claim_veto_reason_code_invalid"
            ),
            "limitation_refs": _string_tuple(
                limitation_refs,
                "claim_veto_limitation_refs_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            veto_ref=_record_ref(
                "claim-veto", namespace.authority_namespace_ref, digest
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
    ) -> "ClaimVeto":
        payload = _strict_shape(payload, cls, "claim_veto_shape_invalid")
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            claim_ref=payload["claim_ref"],
            reason_code=payload["reason_code"],
            limitation_refs=payload["limitation_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError("claim_veto_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class SemanticVerificationAttempt:
    verification_attempt_ref: str
    authority_namespace_ref: str
    purpose: str
    authority_input_ref: str
    authority_input_digest: str
    subject_refs: tuple[str, ...]
    provider_ref: str
    model_ref: str
    input_digest: str
    attempt_number: int
    raw_provider_response_ref: str
    raw_provider_response_digest: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        purpose: str,
        authority_input_ref: str,
        authority_input_digest: str,
        subject_refs: Sequence[str],
        provider_ref: str,
        model_ref: str,
        input_digest: str,
        attempt_number: int,
        raw_provider_response_ref: str,
        raw_provider_response_digest: str,
    ) -> "SemanticVerificationAttempt":
        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "semantic_verification_namespace_invalid",
        )
        if purpose not in VERIFICATION_PURPOSES:
            raise ClaimAuthorityContractError("semantic_verification_purpose_invalid")
        body = {
            "purpose": purpose,
            "authority_input_ref": _required_string(
                authority_input_ref, "semantic_verification_input_ref_invalid"
            ),
            "authority_input_digest": _digest(
                authority_input_digest, "semantic_verification_input_digest_invalid"
            ),
            "subject_refs": _string_tuple(
                subject_refs,
                "semantic_verification_subject_refs_invalid",
                allow_empty=False,
            ),
            "provider_ref": _required_string(
                provider_ref, "semantic_verification_provider_ref_invalid"
            ),
            "model_ref": _required_string(
                model_ref, "semantic_verification_model_ref_invalid"
            ),
            "input_digest": _digest(
                input_digest, "semantic_verification_prompt_digest_invalid"
            ),
            "attempt_number": _integer(
                attempt_number,
                "semantic_verification_attempt_number_invalid",
                minimum=1,
            ),
            "raw_provider_response_ref": _required_string(
                raw_provider_response_ref,
                "semantic_verification_raw_response_ref_invalid",
            ),
            "raw_provider_response_digest": _digest(
                raw_provider_response_digest,
                "semantic_verification_raw_response_digest_invalid",
            ),
        }
        digest = canonical_digest(body)
        return cls(
            verification_attempt_ref=_record_ref(
                "semantic-verification-attempt",
                namespace.authority_namespace_ref,
                digest,
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
    ) -> "SemanticVerificationAttempt":
        payload = _strict_shape(
            payload, cls, "semantic_verification_attempt_shape_invalid"
        )
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            purpose=payload["purpose"],
            authority_input_ref=payload["authority_input_ref"],
            authority_input_digest=payload["authority_input_digest"],
            subject_refs=payload["subject_refs"],
            provider_ref=payload["provider_ref"],
            model_ref=payload["model_ref"],
            input_digest=payload["input_digest"],
            attempt_number=payload["attempt_number"],
            raw_provider_response_ref=payload["raw_provider_response_ref"],
            raw_provider_response_digest=payload["raw_provider_response_digest"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError(
                "semantic_verification_attempt_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def _validated_semantic_attempt(
    value: SemanticVerificationAttempt,
    *,
    authority_namespace: ClaimAuthorityNamespace,
    error: str,
) -> SemanticVerificationAttempt:
    if type(value) is not SemanticVerificationAttempt:
        raise ClaimAuthorityContractError(error)
    body = {
        "purpose": value.purpose,
        "authority_input_ref": value.authority_input_ref,
        "authority_input_digest": value.authority_input_digest,
        "subject_refs": value.subject_refs,
        "provider_ref": value.provider_ref,
        "model_ref": value.model_ref,
        "input_digest": value.input_digest,
        "attempt_number": value.attempt_number,
        "raw_provider_response_ref": value.raw_provider_response_ref,
        "raw_provider_response_digest": value.raw_provider_response_digest,
    }
    digest = canonical_digest(body)
    if (
        value.authority_namespace_ref != authority_namespace.authority_namespace_ref
        or value.content_digest != digest
        or value.verification_attempt_ref
        != _record_ref(
            "semantic-verification-attempt",
            authority_namespace.authority_namespace_ref,
            digest,
        )
    ):
        raise ClaimAuthorityContractError(error)
    return value


@dataclass(frozen=True)
class SemanticVerificationDecision:
    verification_decision_ref: str
    authority_namespace_ref: str
    verification_attempt_ref: str
    subject_ref: str
    disposition: str
    veto_basis: str | None
    reason_code: str | None
    limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        verification_attempt: SemanticVerificationAttempt,
        subject_ref: str,
        disposition: str,
        veto_basis: str | None,
        reason_code: str | None,
        limitation_refs: Sequence[str],
    ) -> "SemanticVerificationDecision":
        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "semantic_verification_decision_namespace_invalid",
        )
        attempt = _validated_semantic_attempt(
            verification_attempt,
            authority_namespace=namespace,
            error="semantic_verification_decision_attempt_invalid",
        )
        subject = _required_string(
            subject_ref, "semantic_verification_subject_ref_invalid"
        )
        if subject not in set(attempt.subject_refs):
            raise ClaimAuthorityContractError(
                "semantic_verification_decision_subject_invalid"
            )
        if disposition not in VERIFICATION_DISPOSITIONS:
            raise ClaimAuthorityContractError(
                "semantic_verification_decision_disposition_invalid"
            )
        reason = _optional_string(
            reason_code, "semantic_verification_decision_reason_invalid"
        )
        basis = _optional_string(
            veto_basis, "semantic_verification_decision_veto_basis_invalid"
        )
        if basis is not None and basis not in VERIFICATION_VETO_BASES:
            raise ClaimAuthorityContractError(
                "semantic_verification_decision_veto_basis_invalid"
            )
        limitations = _string_tuple(
            limitation_refs, "semantic_verification_decision_limitations_invalid"
        )
        if disposition == "accepted" and (
            basis is not None or reason is not None or limitations
        ):
            raise ClaimAuthorityContractError(
                "semantic_verification_decision_acceptance_payload_invalid"
            )
        if disposition == "vetoed" and (basis is None or reason is None):
            raise ClaimAuthorityContractError(
                "semantic_verification_decision_veto_payload_invalid"
            )
        body = {
            "verification_attempt_ref": attempt.verification_attempt_ref,
            "subject_ref": subject,
            "disposition": disposition,
            "veto_basis": basis,
            "reason_code": reason,
            "limitation_refs": limitations,
        }
        digest = canonical_digest(body)
        return cls(
            verification_decision_ref=_record_ref(
                "semantic-verification-decision",
                namespace.authority_namespace_ref,
                digest,
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
        verification_attempt: SemanticVerificationAttempt,
    ) -> "SemanticVerificationDecision":
        payload = _strict_shape(
            payload, cls, "semantic_verification_decision_shape_invalid"
        )
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            verification_attempt=verification_attempt,
            subject_ref=payload["subject_ref"],
            disposition=payload["disposition"],
            veto_basis=payload["veto_basis"],
            reason_code=payload["reason_code"],
            limitation_refs=payload["limitation_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError(
                "semantic_verification_decision_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def _verified_mapping(value: Any, error: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ClaimAuthorityContractError(error)
    normalized = {
        _required_string(key, error): _required_string(item, error)
        for key, item in value.items()
    }
    if len(set(normalized.values())) != len(normalized):
        raise ClaimAuthorityContractError(error)
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True)
class LocalBoundaryAuthority:
    local_boundary_authority_ref: str
    authority_namespace_ref: str
    checkpoint_ref: str
    checkpoint_digest: str
    obligation_ids: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        checkpoint_ref: str,
        checkpoint_digest: str,
        obligation_ids: Sequence[str],
        limitation_refs: Sequence[str],
    ) -> "LocalBoundaryAuthority":
        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "local_boundary_authority_namespace_invalid",
        )
        body = {
            "checkpoint_ref": _required_string(
                checkpoint_ref, "local_boundary_authority_checkpoint_ref_invalid"
            ),
            "checkpoint_digest": _digest(
                checkpoint_digest,
                "local_boundary_authority_checkpoint_digest_invalid",
            ),
            "obligation_ids": _string_tuple(
                obligation_ids,
                "local_boundary_authority_obligations_invalid",
                allow_empty=False,
            ),
            "limitation_refs": _string_tuple(
                limitation_refs,
                "local_boundary_authority_limitations_invalid",
                allow_empty=False,
            ),
        }
        digest = canonical_digest(body)
        return cls(
            local_boundary_authority_ref=_record_ref(
                "local-boundary-authority", namespace.authority_namespace_ref, digest
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
    ) -> "LocalBoundaryAuthority":
        payload = _strict_shape(payload, cls, "local_boundary_authority_shape_invalid")
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            checkpoint_ref=payload["checkpoint_ref"],
            checkpoint_digest=payload["checkpoint_digest"],
            obligation_ids=payload["obligation_ids"],
            limitation_refs=payload["limitation_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError(
                "local_boundary_authority_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ClaimVerifierReport:
    verifier_report_ref: str
    authority_namespace_ref: str
    verification_mode: str
    checkpoint_ref: str
    authority_input_ref: str
    authority_input_digest: str
    verification_attempt: SemanticVerificationAttempt | None
    local_boundary_authority: LocalBoundaryAuthority | None
    local_boundary_authority_ref: str | None
    verification_decisions: tuple[SemanticVerificationDecision, ...]
    evaluated_claim_refs: tuple[str, ...]
    proposed_to_verified: Mapping[str, str]
    accepted_claim_refs: tuple[str, ...]
    rejected_claim_refs: tuple[str, ...]
    vetoes: tuple[ClaimVeto, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        verification_attempt: SemanticVerificationAttempt | None,
        local_boundary_authority: LocalBoundaryAuthority | None,
        verification_decisions: Sequence[SemanticVerificationDecision],
        proposed_to_verified: Mapping[str, str],
        vetoes: Sequence[ClaimVeto],
    ) -> "ClaimVerifierReport":
        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "claim_verifier_report_namespace_invalid",
        )
        mapping = _verified_mapping(
            proposed_to_verified, "claim_verifier_report_mapping_invalid"
        )
        if verification_attempt is None:
            if (
                type(local_boundary_authority) is not LocalBoundaryAuthority
                or verification_decisions
                or mapping
                or vetoes
            ):
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_boundary_partition_invalid"
                )
            try:
                boundary_authority = LocalBoundaryAuthority.from_dict(
                    local_boundary_authority.to_dict(),
                    authority_namespace=namespace,
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_boundary_authority_invalid"
                ) from exc
            if boundary_authority != local_boundary_authority:
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_boundary_authority_invalid"
                )
            attempt = None
            verification_mode = "local_boundary_authority"
            checkpoint_ref = boundary_authority.checkpoint_ref
            authority_input_ref = boundary_authority.checkpoint_ref
            authority_input_digest = boundary_authority.checkpoint_digest
            local_boundary_authority_ref = (
                boundary_authority.local_boundary_authority_ref
            )
            decisions: tuple[SemanticVerificationDecision, ...] = ()
            normalized_vetoes: tuple[ClaimVeto, ...] = ()
            evaluated: tuple[str, ...] = ()
        else:
            if local_boundary_authority is not None:
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_boundary_authority_forbidden"
                )
            boundary_authority = None
            if type(verification_attempt) is not SemanticVerificationAttempt:
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_attempt_invalid"
                )
            try:
                attempt = SemanticVerificationAttempt.from_dict(
                    verification_attempt.to_dict(), authority_namespace=namespace
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_attempt_invalid"
                ) from exc
            if attempt != verification_attempt or attempt.purpose != "claim_settlement":
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_attempt_invalid"
                )
            verification_mode = "semantic_verifier"
            checkpoint_ref = attempt.authority_input_ref
            authority_input_ref = attempt.authority_input_ref
            authority_input_digest = attempt.authority_input_digest
            local_boundary_authority_ref = None
            if isinstance(verification_decisions, (str, bytes)) or not isinstance(
                verification_decisions, Sequence
            ):
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_decisions_invalid"
                )
            replayed_decisions = []
            for raw_decision in verification_decisions:
                if type(raw_decision) is not SemanticVerificationDecision:
                    raise ClaimAuthorityContractError(
                        "claim_verifier_report_decisions_invalid"
                    )
                try:
                    replayed = SemanticVerificationDecision.from_dict(
                        raw_decision.to_dict(),
                        authority_namespace=namespace,
                        verification_attempt=attempt,
                    )
                except (AttributeError, TypeError, ValueError) as exc:
                    raise ClaimAuthorityContractError(
                        "claim_verifier_report_decisions_invalid"
                    ) from exc
                if replayed != raw_decision:
                    raise ClaimAuthorityContractError(
                        "claim_verifier_report_decisions_invalid"
                    )
                replayed_decisions.append(replayed)
            decisions = tuple(
                sorted(replayed_decisions, key=lambda item: item.subject_ref)
            )
            if len({item.subject_ref for item in decisions}) != len(decisions):
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_decisions_invalid"
                )
            if {item.subject_ref for item in decisions} != set(attempt.subject_refs):
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_decision_coverage_invalid"
                )
            normalized_vetoes_list = []
            for raw_veto in vetoes:
                if type(raw_veto) is not ClaimVeto:
                    raise ClaimAuthorityContractError(
                        "claim_verifier_report_vetoes_invalid"
                    )
                try:
                    replayed_veto = ClaimVeto.from_dict(
                        raw_veto.to_dict(), authority_namespace=namespace
                    )
                except (AttributeError, TypeError, ValueError) as exc:
                    raise ClaimAuthorityContractError(
                        "claim_verifier_report_vetoes_invalid"
                    ) from exc
                if replayed_veto != raw_veto:
                    raise ClaimAuthorityContractError(
                        "claim_verifier_report_vetoes_invalid"
                    )
                normalized_vetoes_list.append(replayed_veto)
            normalized_vetoes = tuple(
                sorted(normalized_vetoes_list, key=lambda item: item.claim_ref)
            )
            if len({item.claim_ref for item in normalized_vetoes}) != len(
                normalized_vetoes
            ):
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_vetoes_invalid"
                )
            accepted_subjects = {
                item.subject_ref for item in decisions if item.disposition == "accepted"
            }
            vetoed_decisions = {
                item.subject_ref: item
                for item in decisions
                if item.disposition == "vetoed"
            }
            if accepted_subjects != set(mapping):
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_mapping_invalid"
                )
            if set(vetoed_decisions) != {item.claim_ref for item in normalized_vetoes}:
                raise ClaimAuthorityContractError(
                    "claim_verifier_report_veto_partition_invalid"
                )
            for veto in normalized_vetoes:
                decision = vetoed_decisions[veto.claim_ref]
                expected = ClaimVeto.create(
                    authority_namespace=namespace,
                    claim_ref=decision.subject_ref,
                    reason_code=str(decision.reason_code),
                    limitation_refs=decision.limitation_refs,
                )
                if veto != expected:
                    raise ClaimAuthorityContractError(
                        "claim_verifier_report_veto_decision_invalid"
                    )
            evaluated = attempt.subject_refs
        accepted = tuple(sorted(mapping.values()))
        rejected = tuple(item.claim_ref for item in normalized_vetoes)
        body = {
            "verification_mode": verification_mode,
            "checkpoint_ref": checkpoint_ref,
            "authority_input_ref": authority_input_ref,
            "authority_input_digest": authority_input_digest,
            "verification_attempt": attempt,
            "local_boundary_authority": boundary_authority,
            "local_boundary_authority_ref": local_boundary_authority_ref,
            "verification_decisions": decisions,
            "evaluated_claim_refs": evaluated,
            "proposed_to_verified": mapping,
            "accepted_claim_refs": accepted,
            "rejected_claim_refs": rejected,
            "vetoes": normalized_vetoes,
        }
        digest = canonical_digest(body)
        return cls(
            verifier_report_ref=_record_ref(
                "claim-verifier-report", namespace.authority_namespace_ref, digest
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
    ) -> "ClaimVerifierReport":
        payload = _strict_shape(payload, cls, "claim_verifier_report_shape_invalid")
        raw_attempt = payload["verification_attempt"]
        raw_boundary_authority = payload["local_boundary_authority"]
        if raw_attempt is None:
            attempt = None
        elif isinstance(raw_attempt, Mapping):
            attempt = SemanticVerificationAttempt.from_dict(
                raw_attempt, authority_namespace=authority_namespace
            )
        else:
            raise ClaimAuthorityContractError("claim_verifier_report_attempt_invalid")
        if raw_boundary_authority is None:
            boundary_authority = None
        elif isinstance(raw_boundary_authority, Mapping):
            boundary_authority = LocalBoundaryAuthority.from_dict(
                raw_boundary_authority,
                authority_namespace=authority_namespace,
            )
        else:
            raise ClaimAuthorityContractError(
                "claim_verifier_report_boundary_authority_invalid"
            )
        raw_decisions = payload["verification_decisions"]
        raw_vetoes = payload["vetoes"]
        if (
            isinstance(raw_decisions, (str, bytes))
            or not isinstance(raw_decisions, Sequence)
            or isinstance(raw_vetoes, (str, bytes))
            or not isinstance(raw_vetoes, Sequence)
        ):
            raise ClaimAuthorityContractError("claim_verifier_report_children_invalid")
        decisions = (
            tuple(
                SemanticVerificationDecision.from_dict(
                    item,
                    authority_namespace=authority_namespace,
                    verification_attempt=attempt,
                )
                for item in raw_decisions
            )
            if attempt is not None
            else ()
        )
        vetoes = tuple(
            ClaimVeto.from_dict(item, authority_namespace=authority_namespace)
            for item in raw_vetoes
        )
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            verification_attempt=attempt,
            local_boundary_authority=boundary_authority,
            verification_decisions=decisions,
            proposed_to_verified=payload["proposed_to_verified"],
            vetoes=vetoes,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError("claim_verifier_report_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ObligationCoverage:
    coverage_ref: str
    authority_namespace_ref: str
    claim_verifier_report_ref: str
    obligation_id: str
    status: str
    claim_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        verifier_report: ClaimVerifierReport,
        obligation_id: str,
        status: str,
        claim_refs: Sequence[str],
        limitation_refs: Sequence[str],
    ) -> "ObligationCoverage":
        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "obligation_coverage_namespace_invalid",
        )
        if type(verifier_report) is not ClaimVerifierReport:
            raise ClaimAuthorityContractError(
                "obligation_coverage_verifier_report_invalid"
            )
        try:
            report = ClaimVerifierReport.from_dict(
                verifier_report.to_dict(), authority_namespace=namespace
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimAuthorityContractError(
                "obligation_coverage_verifier_report_invalid"
            ) from exc
        if report != verifier_report:
            raise ClaimAuthorityContractError(
                "obligation_coverage_verifier_report_invalid"
            )
        if status not in OBLIGATION_COVERAGE_STATES:
            raise ClaimAuthorityContractError("obligation_coverage_status_invalid")
        claims = _string_tuple(claim_refs, "obligation_coverage_claim_refs_invalid")
        limitations = _string_tuple(
            limitation_refs, "obligation_coverage_limitation_refs_invalid"
        )
        if status in {"satisfied", "contradicted", "mixed"} and not claims:
            raise ClaimAuthorityContractError("obligation_coverage_claims_required")
        if status == "unavailable" and not limitations:
            raise ClaimAuthorityContractError("obligation_coverage_limitation_required")
        if status in {"unresolved", "not_requested"} and claims:
            raise ClaimAuthorityContractError("obligation_coverage_claims_forbidden")
        if status == "not_requested" and limitations:
            raise ClaimAuthorityContractError(
                "obligation_coverage_limitation_forbidden"
            )
        body = {
            "claim_verifier_report_ref": report.verifier_report_ref,
            "obligation_id": _required_string(
                obligation_id, "obligation_coverage_id_invalid"
            ),
            "status": status,
            "claim_refs": claims,
            "limitation_refs": limitations,
        }
        digest = canonical_digest(body)
        return cls(
            coverage_ref=_record_ref(
                "obligation-coverage", namespace.authority_namespace_ref, digest
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
        verifier_report: ClaimVerifierReport,
    ) -> "ObligationCoverage":
        payload = _strict_shape(payload, cls, "obligation_coverage_shape_invalid")
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            verifier_report=verifier_report,
            obligation_id=payload["obligation_id"],
            status=payload["status"],
            claim_refs=payload["claim_refs"],
            limitation_refs=payload["limitation_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError("obligation_coverage_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def _ceiling_mapping(
    value: Any,
    error: str,
) -> Mapping[str, ClaimPublicationCeiling]:
    if not isinstance(value, Mapping):
        raise ClaimAuthorityContractError(error)
    normalized: dict[str, ClaimPublicationCeiling] = {}
    for raw_ref, raw_ceiling in value.items():
        ref = _required_string(raw_ref, error)
        normalized[ref] = _simple_replay(raw_ceiling, ClaimPublicationCeiling, error)
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True)
class ClaimGraph:
    claim_graph_ref: str
    authority_namespace_ref: str
    authority_mode: str
    claim_key_refs: tuple[str, ...]
    claim_refs: tuple[str, ...]
    support_edge_refs: tuple[str, ...]
    evidence_ceiling_by_ref: Mapping[str, ClaimPublicationCeiling]
    assumption_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    obligation_coverage: tuple[ObligationCoverage, ...]
    claim_verifier_report_ref: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        authority_mode: str,
        claim_keys: Sequence[ClaimKey],
        claims: Sequence[ClaimRevision],
        support_edges: Sequence[SupportEdge],
        obligation_coverage: Sequence[ObligationCoverage],
        verifier_report: ClaimVerifierReport,
        evidence_ceiling_by_ref: Mapping[str, ClaimPublicationCeiling],
        assumption_refs: Sequence[str],
        limitation_refs: Sequence[str],
    ) -> "ClaimGraph":
        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "claim_graph_namespace_invalid",
        )
        if authority_mode not in CLAIM_GRAPH_MODES:
            raise ClaimAuthorityContractError("claim_graph_authority_mode_invalid")
        if type(verifier_report) is not ClaimVerifierReport:
            raise ClaimAuthorityContractError("claim_graph_verifier_report_invalid")
        try:
            report = ClaimVerifierReport.from_dict(
                verifier_report.to_dict(), authority_namespace=namespace
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimAuthorityContractError(
                "claim_graph_verifier_report_invalid"
            ) from exc
        if report != verifier_report:
            raise ClaimAuthorityContractError("claim_graph_verifier_report_invalid")

        key_records: list[ClaimKey] = []
        for item in claim_keys:
            if type(item) is not ClaimKey:
                raise ClaimAuthorityContractError("claim_graph_claim_keys_invalid")
            try:
                replayed = ClaimKey.from_dict(
                    item.to_dict(), authority_namespace=namespace
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise ClaimAuthorityContractError(
                    "claim_graph_claim_keys_invalid"
                ) from exc
            if replayed != item:
                raise ClaimAuthorityContractError("claim_graph_claim_keys_invalid")
            key_records.append(replayed)
        keys = tuple(sorted(key_records, key=lambda item: item.claim_key))
        if len({item.claim_key for item in keys}) != len(keys):
            raise ClaimAuthorityContractError("claim_graph_claim_keys_invalid")

        edge_records: list[SupportEdge] = []
        for item in support_edges:
            if type(item) is not SupportEdge:
                raise ClaimAuthorityContractError("claim_graph_support_edges_invalid")
            try:
                replayed = SupportEdge.from_dict(
                    item.to_dict(), authority_namespace=namespace
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise ClaimAuthorityContractError(
                    "claim_graph_support_edges_invalid"
                ) from exc
            if replayed != item:
                raise ClaimAuthorityContractError("claim_graph_support_edges_invalid")
            edge_records.append(replayed)
        edges = tuple(sorted(edge_records, key=lambda item: item.support_edge_ref))
        if len({item.support_edge_ref for item in edges}) != len(edges):
            raise ClaimAuthorityContractError("claim_graph_support_edges_invalid")

        key_by_ref = {item.claim_key: item for item in keys}
        edge_by_ref = {item.support_edge_ref: item for item in edges}
        claim_records: list[ClaimRevision] = []
        for item in claims:
            if type(item) is not ClaimRevision:
                raise ClaimAuthorityContractError("claim_graph_claims_invalid")
            key = key_by_ref.get(item.claim_key)
            referenced_edges = tuple(
                edge_by_ref[ref] for ref in item.support_edge_refs if ref in edge_by_ref
            )
            if key is None or len(referenced_edges) != len(item.support_edge_refs):
                raise ClaimAuthorityContractError("claim_graph_claims_invalid")
            try:
                replayed = ClaimRevision.from_dict(
                    item.to_dict(),
                    authority_namespace=namespace,
                    claim_key=key,
                    support_edges=referenced_edges,
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise ClaimAuthorityContractError("claim_graph_claims_invalid") from exc
            if replayed != item:
                raise ClaimAuthorityContractError("claim_graph_claims_invalid")
            claim_records.append(replayed)
        normalized_claims = tuple(
            sorted(claim_records, key=lambda item: item.claim_ref)
        )
        if len({item.claim_ref for item in normalized_claims}) != len(
            normalized_claims
        ):
            raise ClaimAuthorityContractError("claim_graph_claims_invalid")

        coverage_records: list[ObligationCoverage] = []
        for item in obligation_coverage:
            if type(item) is not ObligationCoverage:
                raise ClaimAuthorityContractError(
                    "claim_graph_obligation_coverage_invalid"
                )
            try:
                replayed = ObligationCoverage.from_dict(
                    item.to_dict(),
                    authority_namespace=namespace,
                    verifier_report=report,
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise ClaimAuthorityContractError(
                    "claim_graph_obligation_coverage_invalid"
                ) from exc
            if replayed != item:
                raise ClaimAuthorityContractError(
                    "claim_graph_obligation_coverage_invalid"
                )
            coverage_records.append(replayed)
        coverage = tuple(sorted(coverage_records, key=lambda item: item.obligation_id))
        if not coverage or len({item.obligation_id for item in coverage}) != len(
            coverage
        ):
            raise ClaimAuthorityContractError("claim_graph_obligation_coverage_invalid")

        ceiling_by_evidence = _ceiling_mapping(
            evidence_ceiling_by_ref, "claim_graph_evidence_ceilings_invalid"
        )
        assumptions = _string_tuple(
            assumption_refs, "claim_graph_assumption_refs_invalid"
        )
        limitations = _string_tuple(
            limitation_refs, "claim_graph_limitation_refs_invalid"
        )
        key_refs = tuple(item.claim_key for item in keys)
        claim_refs = tuple(item.claim_ref for item in normalized_claims)

        if authority_mode == "boundary_only":
            if (
                keys
                or normalized_claims
                or edges
                or ceiling_by_evidence
                or assumptions
                or report.accepted_claim_refs
                or any(item.claim_refs for item in coverage)
            ):
                raise ClaimAuthorityContractError("claim_graph_boundary_only_invalid")
            if report.verification_mode == "local_boundary_authority":
                if report.evaluated_claim_refs or any(
                    item.status != "unavailable" for item in coverage
                ):
                    raise ClaimAuthorityContractError(
                        "claim_graph_boundary_only_invalid"
                    )
            elif (
                not report.evaluated_claim_refs
                or set(report.rejected_claim_refs) != set(report.evaluated_claim_refs)
                or any(
                    item.status not in {"unavailable", "unresolved", "contradicted"}
                    for item in coverage
                )
            ):
                raise ClaimAuthorityContractError("claim_graph_boundary_only_invalid")
            expected_limitations = {
                ref for item in coverage for ref in item.limitation_refs
            }
            if expected_limitations != set(limitations):
                raise ClaimAuthorityContractError(
                    "claim_graph_limitation_closure_invalid"
                )
        else:
            if not keys or not normalized_claims:
                raise ClaimAuthorityContractError("claim_graph_membership_empty")
            if set(key_refs) != {item.claim_key for item in normalized_claims}:
                raise ClaimAuthorityContractError(
                    "claim_graph_claim_key_closure_invalid"
                )
            if len(normalized_claims) != len(keys):
                raise ClaimAuthorityContractError(
                    "claim_graph_current_revision_cardinality_invalid"
                )
            if any(item.status != "verified" for item in normalized_claims):
                raise ClaimAuthorityContractError(
                    "claim_graph_unverified_claim_invalid"
                )
            if set(claim_refs) != set(report.accepted_claim_refs):
                raise ClaimAuthorityContractError(
                    "claim_graph_verifier_membership_invalid"
                )
            expected_edge_refs = {
                ref for claim in normalized_claims for ref in claim.support_edge_refs
            }
            if expected_edge_refs != {item.support_edge_ref for item in edges}:
                raise ClaimAuthorityContractError(
                    "claim_graph_support_edge_closure_invalid"
                )
            if any(edge.target_claim_key not in set(key_refs) for edge in edges):
                raise ClaimAuthorityContractError(
                    "claim_graph_support_edge_target_invalid"
                )
            claim_by_ref = {item.claim_ref: item for item in normalized_claims}
            referenced_evidence: set[str] = set()
            referenced_assumptions: set[str] = set()
            for edge in edges:
                if edge.source_type == "evidence":
                    referenced_evidence.add(edge.source_ref)
                    actual_ceiling = ceiling_by_evidence.get(edge.source_ref)
                    if actual_ceiling is None:
                        continue
                elif edge.source_type == "assumption":
                    referenced_assumptions.add(edge.source_ref)
                    actual_ceiling = ClaimPublicationCeiling.create(
                        claim_class="scenario", strength="scenario"
                    )
                else:
                    source_claim = claim_by_ref.get(edge.source_ref)
                    if source_claim is None:
                        raise ClaimAuthorityContractError(
                            "claim_graph_claim_source_closure_invalid"
                        )
                    target_claim = next(
                        item
                        for item in normalized_claims
                        if item.claim_key == edge.target_claim_key
                    )
                    if source_claim.claim_ref == target_claim.claim_ref:
                        raise ClaimAuthorityContractError(
                            "claim_graph_self_reference_invalid"
                        )
                    actual_ceiling = source_claim.publication_ceiling
                if (
                    actual_ceiling.claim_class != edge.source_epistemic_class
                    or actual_ceiling != edge.source_publication_ceiling
                ):
                    raise ClaimAuthorityContractError(
                        "claim_graph_evidence_class_mismatch"
                    )
            if referenced_evidence != set(ceiling_by_evidence):
                raise ClaimAuthorityContractError(
                    "claim_graph_evidence_closure_invalid"
                )
            if referenced_assumptions != set(assumptions):
                raise ClaimAuthorityContractError(
                    "claim_graph_assumption_closure_invalid"
                )
            _validate_dependency_acyclicity(normalized_claims)
            claim_ref_set = set(claim_refs)
            covered_claims: set[str] = set()
            for item in coverage:
                if not set(item.claim_refs).issubset(claim_ref_set):
                    raise ClaimAuthorityContractError(
                        "claim_graph_obligation_claim_closure_invalid"
                    )
                covered_claims.update(item.claim_refs)
            if covered_claims != claim_ref_set:
                raise ClaimAuthorityContractError(
                    "claim_graph_obligation_membership_invalid"
                )
            expected_limitations = (
                {ref for claim in normalized_claims for ref in claim.limitation_refs}
                | {ref for edge in edges for ref in edge.limitation_refs}
                | {ref for item in coverage for ref in item.limitation_refs}
            )
            if expected_limitations != set(limitations):
                raise ClaimAuthorityContractError(
                    "claim_graph_limitation_closure_invalid"
                )
        body = {
            "authority_mode": authority_mode,
            "claim_key_refs": key_refs,
            "claim_refs": claim_refs,
            "support_edge_refs": tuple(item.support_edge_ref for item in edges),
            "evidence_ceiling_by_ref": ceiling_by_evidence,
            "assumption_refs": assumptions,
            "limitation_refs": limitations,
            "obligation_coverage": coverage,
            "claim_verifier_report_ref": report.verifier_report_ref,
        }
        digest = canonical_digest(body)
        return cls(
            claim_graph_ref=_record_ref(
                "claim-graph", namespace.authority_namespace_ref, digest
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
        claim_keys: Sequence[ClaimKey],
        claims: Sequence[ClaimRevision],
        support_edges: Sequence[SupportEdge],
        verifier_report: ClaimVerifierReport,
    ) -> "ClaimGraph":
        payload = _strict_shape(payload, cls, "claim_graph_shape_invalid")
        raw_coverage = payload["obligation_coverage"]
        raw_ceilings = payload["evidence_ceiling_by_ref"]
        if (
            isinstance(raw_coverage, (str, bytes))
            or not isinstance(raw_coverage, Sequence)
            or not isinstance(raw_ceilings, Mapping)
        ):
            raise ClaimAuthorityContractError("claim_graph_children_invalid")
        coverage = tuple(
            ObligationCoverage.from_dict(
                item,
                authority_namespace=authority_namespace,
                verifier_report=verifier_report,
            )
            for item in raw_coverage
        )
        ceilings = {
            str(ref): ClaimPublicationCeiling.from_dict(raw_ceiling)
            for ref, raw_ceiling in raw_ceilings.items()
        }
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            authority_mode=payload["authority_mode"],
            claim_keys=claim_keys,
            claims=claims,
            support_edges=support_edges,
            obligation_coverage=coverage,
            verifier_report=verifier_report,
            evidence_ceiling_by_ref=ceilings,
            assumption_refs=payload["assumption_refs"],
            limitation_refs=payload["limitation_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError("claim_graph_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def _validate_dependency_acyclicity(claims: Sequence[ClaimRevision]) -> None:
    dependencies = {
        claim.claim_ref: set(claim.dependency_claim_refs) for claim in claims
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(claim_ref: str) -> None:
        if claim_ref in visiting:
            raise ClaimAuthorityContractError("claim_graph_dependency_cycle_invalid")
        if claim_ref in visited:
            return
        visiting.add(claim_ref)
        for dependency_ref in dependencies[claim_ref]:
            if dependency_ref not in dependencies:
                raise ClaimAuthorityContractError(
                    "claim_graph_dependency_closure_invalid"
                )
            visit(dependency_ref)
        visiting.remove(claim_ref)
        visited.add(claim_ref)

    for current_ref in dependencies:
        visit(current_ref)


@dataclass(frozen=True)
class RecommendationCommitment:
    recommendation_commitment_ref: str
    authority_namespace_ref: str
    commitment_kind: str
    text: str
    supporting_claim_refs: tuple[str, ...]
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
        authority_namespace: ClaimAuthorityNamespace,
        commitment_kind: str,
        text: str,
        supporting_claim_refs: Sequence[str],
        diagnostic_mode: str | None,
        action_domain: str | None,
        action_stage: str | None,
        expected_value_kind: str | None,
        expected_value_mode: str | None,
    ) -> "RecommendationCommitment":
        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "recommendation_commitment_namespace_invalid",
        )
        if commitment_kind not in RECOMMENDATION_COMMITMENT_KINDS:
            raise ClaimAuthorityContractError("recommendation_commitment_kind_invalid")
        diagnostic = _optional_string(
            diagnostic_mode, "recommendation_commitment_diagnostic_mode_invalid"
        )
        domain = _optional_string(
            action_domain, "recommendation_commitment_action_domain_invalid"
        )
        stage = _optional_string(
            action_stage, "recommendation_commitment_action_stage_invalid"
        )
        value_kind = _optional_string(
            expected_value_kind,
            "recommendation_commitment_expected_value_kind_invalid",
        )
        value_mode = _optional_string(
            expected_value_mode,
            "recommendation_commitment_expected_value_mode_invalid",
        )
        if diagnostic is not None and diagnostic not in RECOMMENDATION_DIAGNOSTIC_MODES:
            raise ClaimAuthorityContractError(
                "recommendation_commitment_diagnostic_mode_invalid"
            )
        if domain is not None and domain not in RECOMMENDATION_ACTION_DOMAINS:
            raise ClaimAuthorityContractError(
                "recommendation_commitment_action_domain_invalid"
            )
        if stage is not None and stage not in RECOMMENDATION_ACTION_STAGES:
            raise ClaimAuthorityContractError(
                "recommendation_commitment_action_stage_invalid"
            )
        if (
            value_kind is not None
            and value_kind not in RECOMMENDATION_EXPECTED_VALUE_KINDS
        ):
            raise ClaimAuthorityContractError(
                "recommendation_commitment_expected_value_kind_invalid"
            )
        if (
            value_mode is not None
            and value_mode not in RECOMMENDATION_EXPECTED_VALUE_MODES
        ):
            raise ClaimAuthorityContractError(
                "recommendation_commitment_expected_value_mode_invalid"
            )
        typed_fields = {
            "diagnostic_premise": (
                diagnostic is not None
                and domain is None
                and stage is None
                and value_kind is None
                and value_mode is None
            ),
            "action": (
                diagnostic is None
                and domain is not None
                and stage is not None
                and value_kind is None
                and value_mode is None
            ),
            "expected_outcome": (
                diagnostic is None
                and domain is None
                and stage is None
                and value_kind is not None
                and value_mode is not None
            ),
        }
        if not typed_fields[commitment_kind]:
            raise ClaimAuthorityContractError(
                "recommendation_commitment_typed_fields_invalid"
            )
        body = {
            "commitment_kind": commitment_kind,
            "text": _required_string(text, "recommendation_commitment_text_invalid"),
            "supporting_claim_refs": _string_tuple(
                supporting_claim_refs,
                "recommendation_commitment_supporting_claim_refs_invalid",
                allow_empty=False,
            ),
            "diagnostic_mode": diagnostic,
            "action_domain": domain,
            "action_stage": stage,
            "expected_value_kind": value_kind,
            "expected_value_mode": value_mode,
        }
        digest = canonical_digest(body)
        return cls(
            recommendation_commitment_ref=_record_ref(
                "recommendation-commitment",
                namespace.authority_namespace_ref,
                digest,
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
    ) -> "RecommendationCommitment":
        payload = _strict_shape(payload, cls, "recommendation_commitment_shape_invalid")
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            commitment_kind=payload["commitment_kind"],
            text=payload["text"],
            supporting_claim_refs=payload["supporting_claim_refs"],
            diagnostic_mode=payload["diagnostic_mode"],
            action_domain=payload["action_domain"],
            action_stage=payload["action_stage"],
            expected_value_kind=payload["expected_value_kind"],
            expected_value_mode=payload["expected_value_mode"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError(
                "recommendation_commitment_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def _recommendation_commitment_ceiling_allows(
    commitment: RecommendationCommitment,
    publication_ceiling: ClaimPublicationCeiling,
) -> bool:
    authorization = recommendation_authorization_for_ceiling(publication_ceiling)
    if commitment.commitment_kind == "diagnostic_premise":
        return commitment.diagnostic_mode in set(authorization["diagnostic_modes"])
    if commitment.commitment_kind == "action":
        return {
            "action_domain": commitment.action_domain,
            "action_stage": commitment.action_stage,
        } in authorization["actions"]
    return {
        "expected_value_kind": commitment.expected_value_kind,
        "expected_value_mode": commitment.expected_value_mode,
    } in authorization["expected_values"]


def _validated_recommendation_commitments(
    value: Any,
    *,
    authority_namespace: ClaimAuthorityNamespace,
    claim_by_ref: Mapping[str, ClaimRevision],
    supporting_claim_refs: Sequence[str],
    assumption_refs: Sequence[str],
    applicable_conditions: Sequence[str],
) -> tuple[RecommendationCommitment, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ClaimAuthorityContractError("recommendation_commitments_invalid")
    commitments: list[RecommendationCommitment] = []
    for item in value:
        if type(item) is not RecommendationCommitment:
            raise ClaimAuthorityContractError("recommendation_commitments_invalid")
        try:
            replayed = RecommendationCommitment.from_dict(
                item.to_dict(), authority_namespace=authority_namespace
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimAuthorityContractError(
                "recommendation_commitments_invalid"
            ) from exc
        if replayed != item:
            raise ClaimAuthorityContractError("recommendation_commitments_invalid")
        commitments.append(replayed)
    normalized = tuple(
        sorted(commitments, key=lambda item: item.recommendation_commitment_ref)
    )
    refs = tuple(item.recommendation_commitment_ref for item in normalized)
    if not normalized or len(refs) != len(set(refs)):
        raise ClaimAuthorityContractError("recommendation_commitments_invalid")
    kinds = tuple(item.commitment_kind for item in normalized)
    if kinds.count("action") != 1 or kinds.count("expected_outcome") != 1:
        raise ClaimAuthorityContractError("recommendation_commitment_coverage_invalid")
    supporting = set(supporting_claim_refs)
    bound_supporting = {
        claim_ref
        for commitment in normalized
        for claim_ref in commitment.supporting_claim_refs
    }
    if bound_supporting != supporting or any(
        claim_ref not in claim_by_ref for claim_ref in bound_supporting
    ):
        raise ClaimAuthorityContractError(
            "recommendation_commitment_claim_closure_invalid"
        )
    for commitment in normalized:
        if any(
            not _recommendation_commitment_ceiling_allows(
                commitment, claim_by_ref[claim_ref].publication_ceiling
            )
            for claim_ref in commitment.supporting_claim_refs
        ):
            raise ClaimAuthorityContractError(
                "recommendation_commitment_claim_ceiling_exceeded"
            )
    scenario_high_actions = tuple(
        commitment
        for commitment in normalized
        if commitment.commitment_kind == "action"
        and commitment.action_stage in {"intervene", "scale"}
        and any(
            claim_by_ref[claim_ref].claim_class == "scenario"
            for claim_ref in commitment.supporting_claim_refs
        )
    )
    conditional_outcomes = tuple(
        commitment
        for commitment in normalized
        if commitment.commitment_kind == "expected_outcome"
        and commitment.expected_value_mode == "conditional_scenario"
    )
    if scenario_high_actions and (
        not conditional_outcomes or (not assumption_refs and not applicable_conditions)
    ):
        raise ClaimAuthorityContractError("recommendation_scenario_conditions_invalid")
    return normalized


@dataclass(frozen=True)
class RecommendationProposal:
    recommendation_proposal_ref: str
    authority_namespace_ref: str
    claim_graph_ref: str
    claim_graph_digest: str
    commitment_contract_version: str
    recommendation_commitment_refs: tuple[str, ...]
    commitments: tuple[RecommendationCommitment, ...]
    supporting_claim_refs: tuple[str, ...]
    assumption_refs: tuple[str, ...]
    risk_refs: tuple[str, ...]
    action: str
    applicable_conditions: tuple[str, ...]
    expected_decision_value: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        claim_settlement: Any,
        supporting_claim_refs: Sequence[str],
        assumption_refs: Sequence[str],
        risk_refs: Sequence[str],
        commitment_contract_version: str,
        commitments: Sequence[RecommendationCommitment],
        action: str,
        applicable_conditions: Sequence[str],
        expected_decision_value: str,
    ) -> "RecommendationProposal":
        from bi_agent.runtime.claim_settlement import ClaimSettlement

        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "recommendation_proposal_namespace_invalid",
        )
        if type(claim_settlement) is not ClaimSettlement:
            raise ClaimAuthorityContractError(
                "recommendation_proposal_claim_authority_invalid"
            )
        settlement = claim_settlement
        graph = settlement.claim_graph
        report = settlement.verifier_report
        if (
            settlement.authority_namespace_ref != namespace.authority_namespace_ref
            or graph.authority_namespace_ref != namespace.authority_namespace_ref
            or report.authority_namespace_ref != namespace.authority_namespace_ref
            or settlement.claim_graph_ref != graph.claim_graph_ref
            or settlement.claim_graph_digest != graph.content_digest
            or settlement.claim_verifier_report_ref != report.verifier_report_ref
            or graph.claim_verifier_report_ref != report.verifier_report_ref
        ):
            raise ClaimAuthorityContractError(
                "recommendation_proposal_claim_authority_invalid"
            )
        supporting = _string_tuple(
            supporting_claim_refs,
            "recommendation_supporting_claim_refs_invalid",
            allow_empty=False,
        )
        assumptions = _string_tuple(
            assumption_refs, "recommendation_assumption_refs_invalid"
        )
        risks = _string_tuple(risk_refs, "recommendation_risk_refs_invalid")
        if not set(supporting).issubset(set(graph.claim_refs)):
            raise ClaimAuthorityContractError(
                "recommendation_proposal_claim_authority_invalid"
            )
        if not set(assumptions).issubset(set(graph.assumption_refs)) or not set(
            risks
        ).issubset(set(graph.limitation_refs)):
            raise ClaimAuthorityContractError(
                "recommendation_proposal_reference_closure_invalid"
            )
        version = _required_string(
            commitment_contract_version,
            "recommendation_commitment_contract_version_invalid",
        )
        if version != RECOMMENDATION_COMMITMENT_CONTRACT_VERSION:
            raise ClaimAuthorityContractError(
                "recommendation_commitment_contract_version_invalid"
            )
        claim_by_ref = {claim.claim_ref: claim for claim in settlement.accepted_claims}
        normalized_conditions = _string_tuple(
            applicable_conditions,
            "recommendation_conditions_invalid",
            allow_empty=False,
        )
        normalized_commitments = _validated_recommendation_commitments(
            commitments,
            authority_namespace=namespace,
            claim_by_ref=claim_by_ref,
            supporting_claim_refs=supporting,
            assumption_refs=assumptions,
            applicable_conditions=normalized_conditions,
        )
        normalized_action = _required_string(action, "recommendation_action_invalid")
        normalized_value = _required_string(
            expected_decision_value,
            "recommendation_expected_decision_value_invalid",
        )
        action_commitment = next(
            item for item in normalized_commitments if item.commitment_kind == "action"
        )
        outcome_commitment = next(
            item
            for item in normalized_commitments
            if item.commitment_kind == "expected_outcome"
        )
        if (
            action_commitment.text != normalized_action
            or outcome_commitment.text != normalized_value
        ):
            raise ClaimAuthorityContractError(
                "recommendation_commitment_text_binding_invalid"
            )
        body = {
            "claim_graph_ref": graph.claim_graph_ref,
            "claim_graph_digest": graph.content_digest,
            "commitment_contract_version": version,
            "recommendation_commitment_refs": tuple(
                item.recommendation_commitment_ref for item in normalized_commitments
            ),
            "commitments": normalized_commitments,
            "supporting_claim_refs": supporting,
            "assumption_refs": assumptions,
            "risk_refs": risks,
            "action": normalized_action,
            "applicable_conditions": normalized_conditions,
            "expected_decision_value": normalized_value,
        }
        digest = canonical_digest(body)
        return cls(
            recommendation_proposal_ref=_record_ref(
                "recommendation-proposal", namespace.authority_namespace_ref, digest
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
        claim_settlement: Any,
    ) -> "RecommendationProposal":
        payload = _strict_shape(payload, cls, "recommendation_proposal_shape_invalid")
        raw_commitments = payload["commitments"]
        if isinstance(raw_commitments, (str, bytes)) or not isinstance(
            raw_commitments, Sequence
        ):
            raise ClaimAuthorityContractError("recommendation_commitments_invalid")
        commitments = tuple(
            RecommendationCommitment.from_dict(
                item,
                authority_namespace=authority_namespace,
            )
            for item in raw_commitments
        )
        rebuilt = cls.create(
            authority_namespace=authority_namespace,
            claim_settlement=claim_settlement,
            supporting_claim_refs=payload["supporting_claim_refs"],
            assumption_refs=payload["assumption_refs"],
            risk_refs=payload["risk_refs"],
            commitment_contract_version=payload["commitment_contract_version"],
            commitments=commitments,
            action=payload["action"],
            applicable_conditions=payload["applicable_conditions"],
            expected_decision_value=payload["expected_decision_value"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError(
                "recommendation_proposal_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class RecommendationRecord:
    recommendation_ref: str
    authority_namespace_ref: str
    recommendation_proposal_ref: str
    claim_graph_ref: str
    claim_graph_digest: str
    commitment_contract_version: str
    recommendation_commitment_refs: tuple[str, ...]
    commitments: tuple[RecommendationCommitment, ...]
    supporting_claim_refs: tuple[str, ...]
    assumption_refs: tuple[str, ...]
    risk_refs: tuple[str, ...]
    action: str
    applicable_conditions: tuple[str, ...]
    expected_decision_value: str
    claim_verifier_report_ref: str
    verification_attempt_ref: str
    verification_decision_ref: str
    proposal: RecommendationProposal
    verification_attempt: SemanticVerificationAttempt
    verification_decision: SemanticVerificationDecision
    content_digest: str

    @classmethod
    def verify(
        cls,
        *,
        authority_namespace: ClaimAuthorityNamespace,
        proposal: RecommendationProposal,
        verification_attempt: SemanticVerificationAttempt,
        verification_decision: SemanticVerificationDecision,
        claim_settlement: Any,
    ) -> "RecommendationRecord":
        from bi_agent.runtime.claim_settlement import ClaimSettlement

        namespace = _simple_replay(
            authority_namespace,
            ClaimAuthorityNamespace,
            "recommendation_namespace_invalid",
        )
        if type(proposal) is not RecommendationProposal:
            raise ClaimAuthorityContractError("recommendation_proposal_invalid")
        try:
            validated_proposal = RecommendationProposal.create(
                authority_namespace=namespace,
                claim_settlement=claim_settlement,
                supporting_claim_refs=proposal.supporting_claim_refs,
                assumption_refs=proposal.assumption_refs,
                risk_refs=proposal.risk_refs,
                commitment_contract_version=proposal.commitment_contract_version,
                commitments=proposal.commitments,
                action=proposal.action,
                applicable_conditions=proposal.applicable_conditions,
                expected_decision_value=proposal.expected_decision_value,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimAuthorityContractError(
                "recommendation_proposal_invalid"
            ) from exc
        if validated_proposal != proposal:
            raise ClaimAuthorityContractError("recommendation_proposal_invalid")
        if type(claim_settlement) is not ClaimSettlement:
            raise ClaimAuthorityContractError("recommendation_claim_authority_invalid")
        settlement = claim_settlement
        graph = settlement.claim_graph
        report = settlement.verifier_report
        _require_namespace(
            graph,
            namespace.authority_namespace_ref,
            "recommendation_graph_namespace_invalid",
        )
        _require_namespace(
            report,
            namespace.authority_namespace_ref,
            "recommendation_report_namespace_invalid",
        )
        if graph.claim_verifier_report_ref != report.verifier_report_ref:
            raise ClaimAuthorityContractError("recommendation_claim_authority_invalid")
        if (
            proposal.claim_graph_ref != graph.claim_graph_ref
            or proposal.claim_graph_digest != graph.content_digest
        ):
            raise ClaimAuthorityContractError("recommendation_claim_authority_invalid")
        if not set(proposal.supporting_claim_refs).issubset(set(graph.claim_refs)):
            raise ClaimAuthorityContractError("recommendation_claim_closure_invalid")
        try:
            attempt = _validated_semantic_attempt(
                verification_attempt,
                authority_namespace=namespace,
                error="recommendation_verification_attempt_invalid",
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimAuthorityContractError(
                "recommendation_verification_attempt_invalid"
            ) from exc
        if (
            attempt.purpose != "recommendation"
            or attempt.authority_input_ref != graph.claim_graph_ref
            or attempt.authority_input_digest != graph.content_digest
            or attempt.subject_refs != (proposal.recommendation_proposal_ref,)
        ):
            raise ClaimAuthorityContractError(
                "recommendation_verification_attempt_invalid"
            )
        try:
            decision = SemanticVerificationDecision.create(
                authority_namespace=namespace,
                verification_attempt=attempt,
                subject_ref=verification_decision.subject_ref,
                disposition=verification_decision.disposition,
                veto_basis=verification_decision.veto_basis,
                reason_code=verification_decision.reason_code,
                limitation_refs=verification_decision.limitation_refs,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimAuthorityContractError(
                "recommendation_verification_decision_invalid"
            ) from exc
        if decision != verification_decision or decision.disposition != "accepted":
            raise ClaimAuthorityContractError(
                "recommendation_verification_decision_invalid"
            )
        body = {
            "recommendation_proposal_ref": proposal.recommendation_proposal_ref,
            "claim_graph_ref": graph.claim_graph_ref,
            "claim_graph_digest": graph.content_digest,
            "commitment_contract_version": proposal.commitment_contract_version,
            "recommendation_commitment_refs": (proposal.recommendation_commitment_refs),
            "commitments": proposal.commitments,
            "supporting_claim_refs": proposal.supporting_claim_refs,
            "assumption_refs": proposal.assumption_refs,
            "risk_refs": proposal.risk_refs,
            "action": proposal.action,
            "applicable_conditions": proposal.applicable_conditions,
            "expected_decision_value": proposal.expected_decision_value,
            "claim_verifier_report_ref": report.verifier_report_ref,
            "verification_attempt_ref": attempt.verification_attempt_ref,
            "verification_decision_ref": decision.verification_decision_ref,
            "proposal": proposal,
            "verification_attempt": attempt,
            "verification_decision": decision,
        }
        digest = canonical_digest(body)
        return cls(
            recommendation_ref=_record_ref(
                "recommendation", namespace.authority_namespace_ref, digest
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
        claim_settlement: Any,
    ) -> "RecommendationRecord":
        payload = _strict_shape(payload, cls, "recommendation_shape_invalid")
        raw_proposal = payload["proposal"]
        raw_attempt = payload["verification_attempt"]
        raw_decision = payload["verification_decision"]
        if not all(
            isinstance(item, Mapping)
            for item in (raw_proposal, raw_attempt, raw_decision)
        ):
            raise ClaimAuthorityContractError("recommendation_children_invalid")
        proposal = RecommendationProposal.from_dict(
            raw_proposal,
            authority_namespace=authority_namespace,
            claim_settlement=claim_settlement,
        )
        attempt = SemanticVerificationAttempt.from_dict(
            raw_attempt, authority_namespace=authority_namespace
        )
        decision = SemanticVerificationDecision.from_dict(
            raw_decision,
            authority_namespace=authority_namespace,
            verification_attempt=attempt,
        )
        rebuilt = cls.verify(
            authority_namespace=authority_namespace,
            proposal=proposal,
            verification_attempt=attempt,
            verification_decision=decision,
            claim_settlement=claim_settlement,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError("recommendation_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class AuthorityBundle:
    bundle_ref: str
    authority_namespace_ref: str
    bundle_revision: int
    supersedes_bundle_ref: str | None
    run_attempt_id: str
    intent_revision_id: str
    decision_refs: tuple[str, ...]
    plan_revision_id: str
    authority_context_ref: str
    execution_result_ref: str
    execution_result_digest: str
    claim_settlement_ref: str
    claim_settlement_digest: str
    claim_graph_ref: str
    claim_graph_digest: str
    authority_mode: str
    required_obligation_ids: tuple[str, ...]
    obligation_coverage_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    verified_claim_refs: tuple[str, ...]
    recommendation_refs: tuple[str, ...]
    assumption_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    claim_verifier_report_ref: str
    bundle_digest: str
    seal_state: str
    sealed_at: str
    content_digest: str

    @classmethod
    def seal(
        cls,
        *,
        authority_inputs: "AuthorityBundleInputs",
        bundle_revision: int,
        supersedes_bundle_ref: str | None,
        sealed_at: str | datetime,
    ) -> "AuthorityBundle":
        from bi_agent.runtime.claim_settlement import AuthorityBundleInputs

        if type(authority_inputs) is not AuthorityBundleInputs:
            raise ClaimAuthorityContractError("authority_bundle_inputs_invalid")
        try:
            inputs = AuthorityBundleInputs.create(
                execution_result=authority_inputs.execution_result,
                claim_settlement=authority_inputs.claim_settlement,
                recommendations=authority_inputs.recommendations,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaimAuthorityContractError(
                "authority_bundle_inputs_invalid"
            ) from exc
        if inputs != authority_inputs:
            raise ClaimAuthorityContractError("authority_bundle_inputs_invalid")
        revision = _integer(
            bundle_revision, "authority_bundle_revision_invalid", minimum=1
        )
        supersedes = _optional_string(
            supersedes_bundle_ref, "authority_bundle_supersedes_ref_invalid"
        )
        if (revision == 1 and supersedes is not None) or (
            revision > 1 and supersedes is None
        ):
            raise ClaimAuthorityContractError("authority_bundle_supersession_invalid")
        manifest = {
            "bundle_revision": revision,
            "supersedes_bundle_ref": supersedes,
            "run_attempt_id": inputs.run_attempt_id,
            "intent_revision_id": inputs.intent_revision_id,
            "decision_refs": inputs.decision_refs,
            "plan_revision_id": inputs.plan_revision_id,
            "authority_context_ref": inputs.authority_context_ref,
            "execution_result_ref": inputs.execution_result_ref,
            "execution_result_digest": inputs.execution_result_digest,
            "claim_settlement_ref": inputs.claim_settlement_ref,
            "claim_settlement_digest": inputs.claim_settlement_digest,
            "claim_graph_ref": inputs.claim_graph.claim_graph_ref,
            "claim_graph_digest": inputs.claim_graph.content_digest,
            "authority_mode": inputs.authority_mode,
            "required_obligation_ids": tuple(
                sorted(
                    obligation.obligation_id
                    for obligation in inputs.execution_result.plan_revision.claim_obligations
                    if obligation.role == "user_required"
                )
            ),
            "obligation_coverage_refs": inputs.obligation_coverage_refs,
            "evidence_refs": tuple(inputs.claim_graph.evidence_ceiling_by_ref),
            "verified_claim_refs": tuple(item.claim_ref for item in inputs.claims),
            "recommendation_refs": tuple(
                item.recommendation_ref for item in inputs.recommendations
            ),
            "assumption_refs": inputs.assumption_refs,
            "limitation_refs": inputs.limitation_refs,
            "claim_verifier_report_ref": inputs.verifier_report.verifier_report_ref,
        }
        digest = canonical_digest(manifest)
        return cls(
            bundle_ref=_record_ref(
                "authority-bundle", inputs.authority_namespace_ref, digest
            ),
            authority_namespace_ref=inputs.authority_namespace_ref,
            bundle_digest=digest,
            seal_state="sealed",
            sealed_at=_aware_iso(sealed_at, "authority_bundle_sealed_at_invalid"),
            content_digest=digest,
            **manifest,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_inputs: "AuthorityBundleInputs",
    ) -> "AuthorityBundle":
        payload = _strict_shape(payload, cls, "authority_bundle_shape_invalid")
        rebuilt = cls.seal(
            authority_inputs=authority_inputs,
            bundle_revision=payload["bundle_revision"],
            supersedes_bundle_ref=payload["supersedes_bundle_ref"],
            sealed_at=payload["sealed_at"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ClaimAuthorityContractError("authority_bundle_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


__all__ = (
    "AuthorityBundle",
    "ClaimAuthorityContractError",
    "ClaimAuthorityNamespace",
    "ClaimGraph",
    "ClaimKey",
    "ClaimPublicationCeiling",
    "ClaimRevision",
    "ClaimVerifierReport",
    "ClaimVeto",
    "LocalBoundaryAuthority",
    "ObligationCoverage",
    "RecommendationProposal",
    "RecommendationRecord",
    "SemanticVerificationAttempt",
    "SemanticVerificationDecision",
    "SupportEdge",
    "VERIFICATION_VETO_BASES",
)
