from __future__ import annotations

from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from bi_agent.runtime.capability_authority import EvidenceLedgerEntry
from bi_agent.runtime.claim_authority import (
    ClaimKey,
    ClaimPublicationCeiling,
    ClaimRevision,
    ObligationCoverage,
)
from bi_agent.runtime.claim_settlement import (
    ClaimSettlement,
    ObligationSettlementBasis,
    publication_ceiling_satisfies,
    validate_typed_claim_settlement,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.narrative_authority import (
    PublicClaim,
    PublicClaimPalette,
    PublicFactDescriptor,
    PublicLimitation,
    PublicRecommendation,
)


class NarrativeMaterialProjectionContractError(ValueError):
    pass


_MATERIALIZER_SOURCE_PREFIX = re.compile(r"^source_([1-9][0-9]*)\.(.+)$")
_COVERAGE_SEMANTICS_BY_STATUS = {
    "satisfied": "supported",
    "mixed": "supported_with_limitations",
    "contradicted": "contradicted",
    "unavailable": "unavailable",
}
_ASSERTION_SCOPE_FIELDS = frozenset(
    {
        "scope_effect",
        "metric_refs",
        "target_window_refs",
        "baseline_window_refs",
        "scope_refs",
        "grains",
        "dimension_paths",
    }
)


def _plain(value: Any) -> Any:
    return canonical_value(value)


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise NarrativeMaterialProjectionContractError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise NarrativeMaterialProjectionContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise NarrativeMaterialProjectionContractError(error)
    if len(normalized) != len(set(normalized)):
        raise NarrativeMaterialProjectionContractError(error)
    return tuple(sorted(normalized)) if sort else normalized


def _strict_shape(payload: Any, record_type: type, error: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != set(
        record_type.__dataclass_fields__
    ):
        raise NarrativeMaterialProjectionContractError(error)
    return payload


def _freeze(value: Any, error: str) -> Any:
    try:
        normalized = canonical_value(value)
    except ValueError as exc:
        raise NarrativeMaterialProjectionContractError(error) from exc
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item, error) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_freeze(item, error) for item in normalized)
    return normalized


def _opaque_handle(prefix: str, identity: Any) -> str:
    return f"{prefix}_{canonical_digest(identity)[:20]}"


def _content_ref(prefix: str, body: Mapping[str, Any]) -> tuple[str, str]:
    digest = canonical_digest(body)
    return prefix + digest, digest


def _assertion_scope_from_claim_keys(
    claim_keys: Sequence[ClaimKey],
) -> Mapping[str, Any]:
    keys = tuple(claim_keys)
    return _freeze(
        {
            "scope_effect": "local_claim_family",
            "metric_refs": tuple(
                sorted(
                    {item.metric_ref for item in keys if item.metric_ref is not None}
                )
            ),
            "target_window_refs": tuple(
                sorted(
                    {
                        item.target_window_ref
                        for item in keys
                        if item.target_window_ref is not None
                    }
                )
            ),
            "baseline_window_refs": tuple(
                sorted(
                    {
                        item.baseline_window_ref
                        for item in keys
                        if item.baseline_window_ref is not None
                    }
                )
            ),
            "scope_refs": tuple(sorted({item.scope for item in keys})),
            "grains": tuple(sorted({item.grain for item in keys})),
            "dimension_paths": tuple(sorted({item.dimension_path for item in keys})),
        },
        "narrative_material_projection_requirement_assertion_scope_invalid",
    )


def _validated_assertion_scope(value: Mapping[str, Any]) -> Mapping[str, Any]:
    error = "narrative_material_projection_requirement_assertion_scope_invalid"
    if not isinstance(value, Mapping) or set(value) != _ASSERTION_SCOPE_FIELDS:
        raise NarrativeMaterialProjectionContractError(error)
    if value.get("scope_effect") != "local_claim_family":
        raise NarrativeMaterialProjectionContractError(error)
    normalized = {
        "scope_effect": "local_claim_family",
        "metric_refs": _string_tuple(value.get("metric_refs"), error),
        "target_window_refs": _string_tuple(value.get("target_window_refs"), error),
        "baseline_window_refs": _string_tuple(value.get("baseline_window_refs"), error),
        "scope_refs": _string_tuple(value.get("scope_refs"), error),
        "grains": _string_tuple(value.get("grains"), error),
    }
    raw_paths = value.get("dimension_paths")
    if isinstance(raw_paths, (str, bytes)) or not isinstance(raw_paths, Sequence):
        raise NarrativeMaterialProjectionContractError(error)
    paths = tuple(_string_tuple(path, error, sort=False) for path in raw_paths)
    if len(paths) != len(set(paths)):
        raise NarrativeMaterialProjectionContractError(error)
    normalized["dimension_paths"] = tuple(sorted(paths))
    return _freeze(normalized, error)


def _normalized_public_name(
    fact: PublicFactDescriptor,
    *,
    expected_source_index: int,
) -> str:
    match = _MATERIALIZER_SOURCE_PREFIX.match(fact.public_name)
    if match is None:
        return fact.public_name
    if int(match.group(1)) != expected_source_index:
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_source_prefix_mismatch"
        )
    return match.group(2)


def _interpretation_contract_from_entry(
    entry: EvidenceLedgerEntry,
) -> Mapping[str, Any]:
    contracts_by_digest: dict[str, Mapping[str, Any]] = {}
    for observation in entry.observation_facts:
        if "interpretation_contract" not in observation:
            continue
        raw_contract = observation["interpretation_contract"]
        if not isinstance(raw_contract, Mapping):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_interpretation_contract_invalid"
            )
        contract = _freeze(
            raw_contract,
            "narrative_material_projection_interpretation_contract_invalid",
        )
        contracts_by_digest[canonical_digest(contract)] = contract
    if len(contracts_by_digest) > 1:
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_interpretation_contract_conflict"
        )
    if contracts_by_digest:
        return next(iter(contracts_by_digest.values()))
    return _freeze(
        {},
        "narrative_material_projection_interpretation_contract_invalid",
    )


@dataclass(frozen=True)
class ProjectedEvidenceFact:
    projected_fact_ref: str
    fact_handle: str
    evidence_entry_ref: str
    source_fact_refs: tuple[str, ...]
    name: str
    fact_kind: str
    value: str
    range_end: str | None
    unit: str | None
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        evidence_entry_ref: str,
        source_fact_refs: Sequence[str],
        name: str,
        fact_kind: str,
        value: str,
        range_end: str | None,
        unit: str | None,
    ) -> "ProjectedEvidenceFact":
        evidence_ref = _required_string(
            evidence_entry_ref,
            "narrative_material_projection_fact_evidence_ref_invalid",
        )
        normalized_name = _required_string(
            name, "narrative_material_projection_fact_name_invalid"
        )
        normalized_kind = _required_string(
            fact_kind, "narrative_material_projection_fact_kind_invalid"
        )
        normalized_value = _required_string(
            value, "narrative_material_projection_fact_value_invalid"
        )
        normalized_end = (
            None
            if range_end is None
            else _required_string(
                range_end,
                "narrative_material_projection_fact_range_end_invalid",
            )
        )
        normalized_unit = (
            None
            if unit is None
            else _required_string(
                unit, "narrative_material_projection_fact_unit_invalid"
            )
        )
        sources = _string_tuple(
            source_fact_refs,
            "narrative_material_projection_fact_source_refs_invalid",
            allow_empty=False,
        )
        identity = {
            "evidence_entry_ref": evidence_ref,
            "name": normalized_name,
            "fact_kind": normalized_kind,
            "value": normalized_value,
            "range_end": normalized_end,
            "unit": normalized_unit,
        }
        body = {**identity, "source_fact_refs": sources}
        fact_ref, digest = _content_ref("narrative-projected-fact:sha256:", body)
        return cls(
            projected_fact_ref=fact_ref,
            fact_handle=_opaque_handle("f", identity),
            content_digest=digest,
            **body,
        )

    def assert_integrity(self) -> None:
        rebuilt = ProjectedEvidenceFact.create(
            evidence_entry_ref=self.evidence_entry_ref,
            source_fact_refs=self.source_fact_refs,
            name=self.name,
            fact_kind=self.fact_kind,
            value=self.value,
            range_end=self.range_end,
            unit=self.unit,
        )
        if rebuilt != self:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def to_writer_payload(self) -> dict[str, Any]:
        return {
            "fact_handle": self.fact_handle,
            "name": self.name,
            "fact_kind": self.fact_kind,
            "value": self.value,
            "range_end": self.range_end,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class ProjectedEvidenceMaterial:
    evidence_material_ref: str
    material_handle: str
    evidence_entry_ref: str
    evidence_entry_digest: str
    evidence_edge_refs: tuple[str, ...]
    evidence_kind: str
    evidence_strength: str
    maximum_claim_strength: str
    scope: str
    dimension_path: tuple[str, ...]
    facts: tuple[ProjectedEvidenceFact, ...]
    content_digest: str
    interpretation_contract: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @classmethod
    def create(
        cls,
        *,
        evidence_entry: EvidenceLedgerEntry,
        evidence_edge_refs: Sequence[str],
        facts: Sequence[ProjectedEvidenceFact],
    ) -> "ProjectedEvidenceMaterial":
        try:
            entry = EvidenceLedgerEntry.from_dict(evidence_entry.to_dict())
        except (AttributeError, TypeError, ValueError) as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_evidence_entry_invalid"
            ) from exc
        if entry != evidence_entry:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_evidence_entry_invalid"
            )
        interpretation_contract = _interpretation_contract_from_entry(entry)
        edges = _string_tuple(
            evidence_edge_refs,
            "narrative_material_projection_material_edges_invalid",
            allow_empty=False,
        )
        if isinstance(facts, (str, bytes)) or not isinstance(facts, Sequence):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_material_facts_invalid"
            )
        normalized_facts = tuple(
            sorted(facts, key=lambda item: (item.name, item.fact_handle))
        )
        if any(type(item) is not ProjectedEvidenceFact for item in normalized_facts):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_material_facts_invalid"
            )
        for fact in normalized_facts:
            fact.assert_integrity()
        if (
            any(fact.evidence_entry_ref != entry.entry_ref for fact in normalized_facts)
            or len({fact.name for fact in normalized_facts}) != len(normalized_facts)
            or len({fact.fact_handle for fact in normalized_facts})
            != len(normalized_facts)
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_material_fact_closure_invalid"
            )
        body = {
            "evidence_entry_ref": entry.entry_ref,
            "evidence_entry_digest": entry.content_digest,
            "evidence_edge_refs": edges,
            "evidence_kind": entry.evidence_kind,
            "evidence_strength": entry.evidence_strength,
            "maximum_claim_strength": entry.maximum_claim_strength,
            "scope": entry.scope,
            "dimension_path": entry.dimension_path,
            "facts": normalized_facts,
        }
        digest_body = dict(body)
        if interpretation_contract:
            digest_body["interpretation_contract"] = interpretation_contract
        material_ref, digest = _content_ref(
            "narrative-evidence-material:sha256:", digest_body
        )
        return cls(
            evidence_material_ref=material_ref,
            material_handle=_opaque_handle("m", entry.entry_ref),
            content_digest=digest,
            interpretation_contract=interpretation_contract,
            **body,
        )

    def assert_integrity(self) -> None:
        for fact in self.facts:
            fact.assert_integrity()
        if not isinstance(self.interpretation_contract, Mapping):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )
        interpretation_contract = _freeze(
            self.interpretation_contract,
            "narrative_material_projection_integrity_invalid",
        )
        body = {
            "evidence_entry_ref": self.evidence_entry_ref,
            "evidence_entry_digest": self.evidence_entry_digest,
            "evidence_edge_refs": self.evidence_edge_refs,
            "evidence_kind": self.evidence_kind,
            "evidence_strength": self.evidence_strength,
            "maximum_claim_strength": self.maximum_claim_strength,
            "scope": self.scope,
            "dimension_path": self.dimension_path,
            "facts": self.facts,
        }
        if interpretation_contract:
            body["interpretation_contract"] = interpretation_contract
        expected_ref, digest = _content_ref("narrative-evidence-material:sha256:", body)
        if (
            self.evidence_material_ref != expected_ref
            or self.content_digest != digest
            or self.material_handle != _opaque_handle("m", self.evidence_entry_ref)
            or interpretation_contract != self.interpretation_contract
            or len({item.name for item in self.facts}) != len(self.facts)
            or any(
                item.evidence_entry_ref != self.evidence_entry_ref
                for item in self.facts
            )
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def to_writer_payload(self) -> dict[str, Any]:
        return {
            "material_handle": self.material_handle,
            "evidence_kind": self.evidence_kind,
            "evidence_strength": self.evidence_strength,
            "maximum_claim_strength": self.maximum_claim_strength,
            "scope": self.scope,
            "dimension_path": list(self.dimension_path),
            "interpretation_contract": canonical_value(self.interpretation_contract),
            "facts": [item.to_writer_payload() for item in self.facts],
        }


@dataclass(frozen=True)
class ProjectedNarrativeClaim:
    projected_claim_ref: str
    claim_ref: str
    claim_digest: str
    claim_handle: str
    claim_class: str
    publication_ceiling: ClaimPublicationCeiling
    subject: str
    scope: str
    grain: str
    dimension_path: tuple[str, ...]
    evidence_entry_refs: tuple[str, ...]
    material_handles: tuple[str, ...]
    limitation_handles: tuple[str, ...]
    verified_claim_payload: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        public_claim: PublicClaim,
        evidence_entry_refs: Sequence[str],
        material_handle_by_evidence_ref: Mapping[str, str],
        verified_claim_payload: Mapping[str, Any],
    ) -> "ProjectedNarrativeClaim":
        public_claim.assert_integrity()
        if not isinstance(verified_claim_payload, Mapping) or not verified_claim_payload:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_claim_payload_invalid"
            )
        payload = _freeze(
            verified_claim_payload,
            "narrative_material_projection_claim_payload_invalid",
        )
        refs = _string_tuple(
            evidence_entry_refs,
            "narrative_material_projection_claim_material_refs_invalid",
        )
        try:
            handles = tuple(material_handle_by_evidence_ref[ref] for ref in refs)
        except KeyError as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_claim_material_closure_invalid"
            ) from exc
        if len(handles) != len(set(handles)):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_claim_material_closure_invalid"
            )
        body = {
            "claim_ref": public_claim.claim_ref,
            "claim_digest": public_claim.facts[0].claim_digest,
            "claim_handle": public_claim.claim_handle,
            "claim_class": public_claim.claim_class,
            "publication_ceiling": public_claim.publication_ceiling,
            "subject": public_claim.subject,
            "scope": public_claim.scope,
            "grain": public_claim.grain,
            "dimension_path": public_claim.dimension_path,
            "evidence_entry_refs": refs,
            "material_handles": handles,
            "limitation_handles": public_claim.limitation_handles,
            "verified_claim_payload": payload,
        }
        projected_ref, digest = _content_ref("narrative-projected-claim:sha256:", body)
        return cls(
            projected_claim_ref=projected_ref,
            content_digest=digest,
            **body,
        )

    def assert_integrity(self) -> None:
        try:
            ceiling = ClaimPublicationCeiling.from_dict(
                self.publication_ceiling.to_dict()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            ) from exc
        body = {
            "claim_ref": self.claim_ref,
            "claim_digest": self.claim_digest,
            "claim_handle": self.claim_handle,
            "claim_class": self.claim_class,
            "publication_ceiling": self.publication_ceiling,
            "subject": self.subject,
            "scope": self.scope,
            "grain": self.grain,
            "dimension_path": self.dimension_path,
            "evidence_entry_refs": self.evidence_entry_refs,
            "material_handles": self.material_handles,
            "limitation_handles": self.limitation_handles,
            "verified_claim_payload": self.verified_claim_payload,
        }
        expected_ref, digest = _content_ref("narrative-projected-claim:sha256:", body)
        if (
            self.projected_claim_ref != expected_ref
            or self.content_digest != digest
            or ceiling != self.publication_ceiling
            or ceiling.claim_class != self.claim_class
            or len(self.evidence_entry_refs) != len(self.material_handles)
            or len(set(self.material_handles)) != len(self.material_handles)
            or not isinstance(self.verified_claim_payload, Mapping)
            or not self.verified_claim_payload
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def to_writer_payload(self) -> dict[str, Any]:
        return {
            "claim_handle": self.claim_handle,
            "claim_class": self.claim_class,
            "publication_ceiling": self.publication_ceiling.to_dict(),
            "subject": self.subject,
            "scope": self.scope,
            "grain": self.grain,
            "dimension_path": list(self.dimension_path),
            "material_handles": list(self.material_handles),
            "limitation_handles": list(self.limitation_handles),
            "verified_claim_payload": canonical_value(self.verified_claim_payload),
        }


@dataclass(frozen=True)
class BoundaryFacet:
    boundary_facet_ref: str
    boundary_facet_handle: str
    facet_kind: str
    context: Mapping[str, Any]
    source_limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        facet_kind: str,
        context: Mapping[str, Any],
        source_limitation_refs: Sequence[str],
    ) -> "BoundaryFacet":
        kind = _required_string(
            facet_kind, "narrative_material_projection_facet_kind_invalid"
        )
        if not isinstance(context, Mapping) or not context:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_facet_context_invalid"
            )
        frozen_context = _freeze(
            context, "narrative_material_projection_facet_context_invalid"
        )
        refs = _string_tuple(
            source_limitation_refs,
            "narrative_material_projection_facet_limitation_refs_invalid",
            allow_empty=False,
        )
        identity = {"facet_kind": kind, "context": frozen_context}
        body = {**identity, "source_limitation_refs": refs}
        facet_ref, digest = _content_ref("narrative-boundary-facet:sha256:", body)
        return cls(
            boundary_facet_ref=facet_ref,
            boundary_facet_handle=_opaque_handle("bf", identity),
            content_digest=digest,
            **body,
        )

    def assert_integrity(self) -> None:
        rebuilt = BoundaryFacet.create(
            facet_kind=self.facet_kind,
            context=self.context,
            source_limitation_refs=self.source_limitation_refs,
        )
        if rebuilt != self:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def to_writer_payload(self) -> dict[str, Any]:
        return {
            "boundary_facet_handle": self.boundary_facet_handle,
            "facet_kind": self.facet_kind,
            "context": canonical_value(self.context),
        }


@dataclass(frozen=True)
class ProjectedLimitation:
    projected_limitation_ref: str
    limitation_ref: str
    limitation_digest: str
    limitation_handle: str
    boundary_facet_refs: tuple[str, ...]
    boundary_facet_handles: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        limitation: PublicLimitation,
        facets: Sequence[BoundaryFacet],
    ) -> "ProjectedLimitation":
        limitation.assert_integrity()
        if isinstance(facets, (str, bytes)) or not isinstance(facets, Sequence):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_limitation_facets_invalid"
            )
        normalized_facets = tuple(facets)
        if (
            not normalized_facets
            or any(type(item) is not BoundaryFacet for item in normalized_facets)
            or len({item.boundary_facet_ref for item in normalized_facets})
            != len(normalized_facets)
            or any(
                limitation.limitation_ref not in set(item.source_limitation_refs)
                for item in normalized_facets
            )
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_limitation_facets_invalid"
            )
        body = {
            "limitation_ref": limitation.limitation_ref,
            "limitation_digest": limitation.content_digest,
            "limitation_handle": limitation.limitation_handle,
            "boundary_facet_refs": tuple(
                item.boundary_facet_ref for item in normalized_facets
            ),
            "boundary_facet_handles": tuple(
                item.boundary_facet_handle for item in normalized_facets
            ),
        }
        projected_ref, digest = _content_ref(
            "narrative-projected-limitation:sha256:", body
        )
        return cls(
            projected_limitation_ref=projected_ref,
            content_digest=digest,
            **body,
        )

    def assert_integrity(self) -> None:
        body = {
            "limitation_ref": self.limitation_ref,
            "limitation_digest": self.limitation_digest,
            "limitation_handle": self.limitation_handle,
            "boundary_facet_refs": self.boundary_facet_refs,
            "boundary_facet_handles": self.boundary_facet_handles,
        }
        expected_ref, digest = _content_ref(
            "narrative-projected-limitation:sha256:", body
        )
        if (
            self.projected_limitation_ref != expected_ref
            or self.content_digest != digest
            or not self.boundary_facet_refs
            or len(self.boundary_facet_refs) != len(self.boundary_facet_handles)
            or len(set(self.boundary_facet_refs)) != len(self.boundary_facet_refs)
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def to_writer_payload(self) -> dict[str, Any]:
        return {
            "limitation_handle": self.limitation_handle,
            "boundary_facet_handles": list(self.boundary_facet_handles),
        }


def _requirement_claim_keys(
    *,
    basis: ObligationSettlementBasis,
    coverage: ObligationCoverage,
    settlement: ClaimSettlement,
) -> tuple[ClaimKey, ...]:
    claim_by_ref = {
        item.claim_ref: item
        for item in (
            *settlement.checkpoint.proposed_claims,
            *settlement.accepted_claims,
        )
    }
    key_by_ref = {
        item.claim_key: item
        for item in (
            *settlement.checkpoint.proposed_claim_keys,
            *settlement.accepted_claim_keys,
        )
    }
    claim_refs = coverage.claim_refs or basis.proposed_claim_refs
    try:
        keys = tuple(key_by_ref[claim_by_ref[ref].claim_key] for ref in claim_refs)
    except KeyError as exc:
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_requirement_claim_scope_invalid"
        ) from exc
    return tuple(
        sorted(
            {item.claim_key: item for item in keys}.values(),
            key=lambda item: item.claim_key,
        )
    )


def _limitation_obligation_claim_kinds(
    *,
    palette: PublicClaimPalette,
    obligation_id: str,
    limitation_refs: Sequence[str],
) -> set[str]:
    limitation_by_ref = {item.limitation_ref: item for item in palette.limitations}
    kinds: set[str] = set()
    for limitation_ref in limitation_refs:
        limitation = limitation_by_ref.get(limitation_ref)
        if limitation is None:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_requirement_limitation_closure_invalid"
            )
        records = limitation.public_context.get("obligations", ())
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_requirement_claim_kind_invalid"
            )
        for record in records:
            if not isinstance(record, Mapping):
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_requirement_claim_kind_invalid"
                )
            if record.get("obligation_id") == obligation_id:
                kinds.add(
                    _required_string(
                        record.get("claim_kind"),
                        "narrative_material_projection_requirement_claim_kind_invalid",
                    )
                )
    return kinds


def _requirement_semantic_authority(
    *,
    basis: ObligationSettlementBasis,
    coverage: ObligationCoverage,
    settlement: ClaimSettlement,
    palette: PublicClaimPalette,
) -> tuple[str, Mapping[str, Any]]:
    claim_keys = _requirement_claim_keys(
        basis=basis,
        coverage=coverage,
        settlement=settlement,
    )
    claim_kinds = {item.claim_kind for item in claim_keys}
    claim_kinds.update(
        _limitation_obligation_claim_kinds(
            palette=palette,
            obligation_id=basis.obligation_id,
            limitation_refs=coverage.limitation_refs,
        )
    )
    if len(claim_kinds) != 1:
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_requirement_claim_kind_invalid"
        )
    return next(iter(claim_kinds)), _assertion_scope_from_claim_keys(claim_keys)


def _requested_factor_fact_handles(
    *,
    basis: ObligationSettlementBasis,
    assertion_scope: Mapping[str, Any],
    claim_refs: Sequence[str],
    projected_claim_by_ref: Mapping[str, ProjectedNarrativeClaim],
    material_by_handle: Mapping[str, ProjectedEvidenceMaterial],
) -> tuple[str, ...]:
    """Resolve exact baseline/target facts for a typed requested-factor obligation."""

    raw_outcome_refs = basis.success_policy.get("outcome_refs", ())
    if isinstance(raw_outcome_refs, (str, bytes)) or not isinstance(
        raw_outcome_refs, Sequence
    ):
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_requirement_outcome_refs_invalid"
        )
    outcome_refs = _string_tuple(
        raw_outcome_refs,
        "narrative_material_projection_requirement_outcome_refs_invalid",
    )
    if "requested_factor_evidence" not in outcome_refs:
        return ()
    metric_refs = _string_tuple(
        assertion_scope.get("metric_refs"),
        "narrative_material_projection_requirement_factor_scope_invalid",
    )
    if len(metric_refs) != 1:
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_requirement_factor_scope_invalid"
        )
    metric_ref = metric_refs[0]
    raw_dimension_refs = basis.success_policy.get("requested_dimension_refs", ())
    if isinstance(raw_dimension_refs, (str, bytes)) or not isinstance(
        raw_dimension_refs, Sequence
    ):
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_requirement_dimension_refs_invalid"
        )
    dimension_refs = _string_tuple(
        raw_dimension_refs,
        "narrative_material_projection_requirement_dimension_refs_invalid",
        sort=False,
    )
    dimension_summary_anchor = basis.success_policy.get(
        "dimension_summary_anchor", False
    )
    if type(dimension_summary_anchor) is not bool:
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_requirement_dimension_anchor_invalid"
        )
    expected_names = [
        f"baseline_{metric_ref}",
        f"target_{metric_ref}",
    ]
    for dimension_ref in dimension_refs:
        prefix = f"dimension_{dimension_ref}"
        if dimension_summary_anchor:
            expected_names.append(f"{prefix}_representative_member")
        expected_names.extend(
            (
                f"{prefix}_baseline_{metric_ref}",
                f"{prefix}_target_{metric_ref}",
            )
        )
    material_handles = tuple(
        dict.fromkeys(
            material_handle
            for claim_ref in claim_refs
            for material_handle in projected_claim_by_ref[claim_ref].material_handles
        )
    )
    resolved: list[str] = []
    for expected_name in expected_names:
        resolved.extend(
            fact.fact_handle
            for material_handle in material_handles
            for fact in material_by_handle[material_handle].facts
            if fact.name == expected_name or fact.name.endswith("." + expected_name)
        )
    return tuple(dict.fromkeys(resolved))


@dataclass(frozen=True)
class ProjectedPublicationRequirement:
    projected_requirement_ref: str
    requirement_handle: str
    obligation_id: str
    obligation_basis_ref: str
    obligation_basis_digest: str
    obligation_coverage_ref: str
    obligation_coverage_digest: str
    status: str
    coverage_semantics: str
    claim_kind: str
    assertion_scope: Mapping[str, Any]
    required_claim_strength: str
    claim_refs: tuple[str, ...]
    claim_handles: tuple[str, ...]
    required_fact_handles: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    limitation_handles: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        basis: ObligationSettlementBasis,
        coverage: ObligationCoverage,
        accepted_claims_by_ref: Mapping[str, ClaimRevision],
        claim_handle_by_ref: Mapping[str, str],
        limitation_handle_by_ref: Mapping[str, str],
        claim_kind: str,
        assertion_scope: Mapping[str, Any],
        required_fact_handles: Sequence[str] = (),
    ) -> "ProjectedPublicationRequirement":
        if (
            type(basis) is not ObligationSettlementBasis
            or type(coverage) is not ObligationCoverage
            or basis.obligation_id != coverage.obligation_id
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_requirement_source_closure_invalid"
            )
        try:
            coverage_claims = tuple(
                accepted_claims_by_ref[ref] for ref in coverage.claim_refs
            )
        except KeyError as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_requirement_claim_closure_invalid"
            ) from exc
        if any(
            type(claim) is not ClaimRevision or claim.claim_ref != ref
            for ref, claim in zip(coverage.claim_refs, coverage_claims, strict=True)
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_requirement_claim_closure_invalid"
            )
        if coverage.status == "satisfied":
            if coverage.limitation_refs:
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_requirement_status_closure_invalid"
                )
            claim_refs = tuple(
                ref
                for ref, claim in zip(
                    coverage.claim_refs,
                    coverage_claims,
                    strict=True,
                )
                if publication_ceiling_satisfies(
                    claim.publication_ceiling,
                    required_strength=basis.required_claim_strength,
                )
            )
            limitation_refs: tuple[str, ...] = ()
            if not claim_refs:
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_requirement_status_closure_invalid"
                )
        elif coverage.status == "mixed":
            claim_refs = coverage.claim_refs
            limitation_refs = coverage.limitation_refs
            if not claim_refs or not limitation_refs:
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_requirement_status_closure_invalid"
                )
        elif coverage.status == "contradicted":
            claim_refs = coverage.claim_refs
            limitation_refs = coverage.limitation_refs
            if not claim_refs:
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_requirement_status_closure_invalid"
                )
        elif coverage.status == "unavailable":
            claim_refs = ()
            limitation_refs = coverage.limitation_refs
            if coverage.claim_refs or not limitation_refs:
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_requirement_status_closure_invalid"
                )
        else:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_requirement_status_closure_invalid"
            )
        try:
            claim_handles = tuple(claim_handle_by_ref[ref] for ref in claim_refs)
        except KeyError as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_requirement_claim_closure_invalid"
            ) from exc
        try:
            limitation_handles = tuple(
                limitation_handle_by_ref[ref] for ref in limitation_refs
            )
        except KeyError as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_requirement_limitation_closure_invalid"
            ) from exc
        body = {
            "obligation_id": basis.obligation_id,
            "obligation_basis_ref": basis.basis_ref,
            "obligation_basis_digest": basis.content_digest,
            "obligation_coverage_ref": coverage.coverage_ref,
            "obligation_coverage_digest": coverage.content_digest,
            "status": coverage.status,
            "coverage_semantics": _COVERAGE_SEMANTICS_BY_STATUS[coverage.status],
            "claim_kind": _required_string(
                claim_kind,
                "narrative_material_projection_requirement_claim_kind_invalid",
            ),
            "assertion_scope": _validated_assertion_scope(assertion_scope),
            "required_claim_strength": basis.required_claim_strength,
            "claim_refs": claim_refs,
            "claim_handles": claim_handles,
            "required_fact_handles": _string_tuple(
                required_fact_handles,
                "narrative_material_projection_requirement_fact_handles_invalid",
                sort=False,
            ),
            "limitation_refs": limitation_refs,
            "limitation_handles": limitation_handles,
        }
        projected_ref, digest = _content_ref(
            "narrative-publication-requirement:sha256:", body
        )
        return cls(
            projected_requirement_ref=projected_ref,
            requirement_handle=_opaque_handle("pr", body),
            content_digest=digest,
            **body,
        )

    def assert_integrity(self) -> None:
        claim_kind = _required_string(
            self.claim_kind,
            "narrative_material_projection_requirement_claim_kind_invalid",
        )
        assertion_scope = _validated_assertion_scope(self.assertion_scope)
        _required_string(
            self.required_claim_strength,
            "narrative_material_projection_requirement_integrity_invalid",
        )
        claim_refs = _string_tuple(
            self.claim_refs,
            "narrative_material_projection_requirement_integrity_invalid",
        )
        claim_handles = _string_tuple(
            self.claim_handles,
            "narrative_material_projection_requirement_integrity_invalid",
            sort=False,
        )
        limitation_refs = _string_tuple(
            self.limitation_refs,
            "narrative_material_projection_requirement_integrity_invalid",
        )
        limitation_handles = _string_tuple(
            self.limitation_handles,
            "narrative_material_projection_requirement_integrity_invalid",
            sort=False,
        )
        required_fact_handles = _string_tuple(
            self.required_fact_handles,
            "narrative_material_projection_requirement_integrity_invalid",
            sort=False,
        )
        body = {
            "obligation_id": self.obligation_id,
            "obligation_basis_ref": self.obligation_basis_ref,
            "obligation_basis_digest": self.obligation_basis_digest,
            "obligation_coverage_ref": self.obligation_coverage_ref,
            "obligation_coverage_digest": self.obligation_coverage_digest,
            "status": self.status,
            "coverage_semantics": self.coverage_semantics,
            "claim_kind": self.claim_kind,
            "assertion_scope": self.assertion_scope,
            "required_claim_strength": self.required_claim_strength,
            "claim_refs": self.claim_refs,
            "claim_handles": self.claim_handles,
            "required_fact_handles": self.required_fact_handles,
            "limitation_refs": self.limitation_refs,
            "limitation_handles": self.limitation_handles,
        }
        expected_ref, digest = _content_ref(
            "narrative-publication-requirement:sha256:", body
        )
        status_closure_valid = (
            (
                self.status == "satisfied"
                and bool(self.claim_handles)
                and not self.limitation_handles
            )
            or (
                self.status == "mixed"
                and bool(self.claim_handles)
                and bool(self.limitation_handles)
            )
            or (self.status == "contradicted" and bool(self.claim_handles))
            or (
                self.status == "unavailable"
                and not self.claim_handles
                and bool(self.limitation_handles)
            )
        )
        if self.status == "unavailable" and self.required_fact_handles:
            status_closure_valid = False
        if (
            self.projected_requirement_ref != expected_ref
            or self.requirement_handle != _opaque_handle("pr", body)
            or self.content_digest != digest
            or claim_kind != self.claim_kind
            or assertion_scope != self.assertion_scope
            or self.coverage_semantics != _COVERAGE_SEMANTICS_BY_STATUS.get(self.status)
            or claim_refs != self.claim_refs
            or claim_handles != self.claim_handles
            or required_fact_handles != self.required_fact_handles
            or limitation_refs != self.limitation_refs
            or limitation_handles != self.limitation_handles
            or len(self.claim_refs) != len(self.claim_handles)
            or len(self.limitation_refs) != len(self.limitation_handles)
            or len(set(self.claim_refs)) != len(self.claim_refs)
            or len(set(self.claim_handles)) != len(self.claim_handles)
            or len(set(self.required_fact_handles))
            != len(self.required_fact_handles)
            or len(set(self.limitation_refs)) != len(self.limitation_refs)
            or len(set(self.limitation_handles)) != len(self.limitation_handles)
            or not status_closure_valid
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_requirement_integrity_invalid"
            )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def to_writer_payload(self) -> dict[str, Any]:
        self.assert_integrity()
        return {
            "requirement_handle": self.requirement_handle,
            "obligation_id": self.obligation_id,
            "status": self.status,
            "coverage_semantics": self.coverage_semantics,
            "claim_kind": self.claim_kind,
            "assertion_scope": canonical_value(self.assertion_scope),
            "required_claim_strength": self.required_claim_strength,
            "claim_handles": list(self.claim_handles),
            "required_fact_handles": list(self.required_fact_handles),
            "limitation_handles": list(self.limitation_handles),
        }


def _validate_palette_against_settlement(
    palette: PublicClaimPalette,
    settlement: ClaimSettlement,
) -> tuple[PublicClaim, ...]:
    if type(palette) is not PublicClaimPalette:
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_palette_invalid"
        )
    claims_by_ref = {item.claim_ref: item for item in settlement.accepted_claims}
    keys_by_ref = {item.claim_key: item for item in settlement.accepted_claim_keys}
    limitations_by_ref = {item.limitation_ref: item for item in palette.limitations}
    required_obligation_ids = _string_tuple(
        palette.required_obligation_ids,
        "narrative_material_projection_required_obligations_invalid",
    )
    basis_ids = {item.obligation_id for item in settlement.checkpoint.obligation_basis}
    coverage_ids = {item.obligation_id for item in settlement.obligation_coverage}
    if (
        len(claims_by_ref) != len(settlement.accepted_claims)
        or len(keys_by_ref) != len(settlement.accepted_claim_keys)
        or len(limitations_by_ref) != len(palette.limitations)
        or {item.claim_ref for item in palette.claims} != set(claims_by_ref)
        or palette.authority_mode != settlement.claim_graph.authority_mode
        or required_obligation_ids != palette.required_obligation_ids
        or not set(required_obligation_ids).issubset(basis_ids & coverage_ids)
    ):
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_claim_closure_invalid"
        )
    replayed_claims = []
    for item in palette.claims:
        try:
            claim = claims_by_ref[item.claim_ref]
            key = keys_by_ref[item.claim_key_ref]
            replayed = PublicClaim.from_dict(
                item.to_dict(),
                claim=claim,
                claim_key=key,
                limitations_by_ref=limitations_by_ref,
            )
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_claim_closure_invalid"
            ) from exc
        if replayed != item:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_claim_closure_invalid"
            )
        replayed_claims.append(replayed)
    for limitation in palette.limitations:
        try:
            limitation.assert_integrity()
        except (AttributeError, TypeError, ValueError) as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_limitation_invalid"
            ) from exc
    claim_handle_by_ref = {item.claim_ref: item.claim_handle for item in palette.claims}
    limitation_handle_by_ref = {
        item.limitation_ref: item.limitation_handle for item in palette.limitations
    }
    for recommendation in palette.recommendations:
        try:
            recommendation.assert_integrity()
        except (AttributeError, TypeError, ValueError) as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_recommendation_invalid"
            ) from exc
        if (
            tuple(
                claim_handle_by_ref.get(ref)
                for ref in recommendation.supporting_claim_refs
            )
            != recommendation.supporting_claim_handles
            or tuple(
                limitation_handle_by_ref.get(ref) for ref in recommendation.risk_refs
            )
            != recommendation.risk_handles
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_recommendation_closure_invalid"
            )
    palette_body = {
        "authority_bundle_ref": palette.authority_bundle_ref,
        "authority_bundle_digest": palette.authority_bundle_digest,
        "authority_mode": palette.authority_mode,
        "required_obligation_ids": palette.required_obligation_ids,
        "field_visibility_policy_ref": palette.field_visibility_policy_ref,
        "field_visibility_policy_digest": palette.field_visibility_policy_digest,
        "claims": palette.claims,
        "recommendations": palette.recommendations,
        "limitations": palette.limitations,
    }
    digest = canonical_digest(palette_body)
    if (
        palette.content_digest != digest
        or palette.palette_ref != "public-claim-palette:sha256:" + digest
    ):
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_palette_integrity_invalid"
        )
    return tuple(sorted(replayed_claims, key=lambda item: item.claim_ref))


def _validated_evidence_entries(
    evidence_entries: Sequence[EvidenceLedgerEntry],
    *,
    expected_refs: set[str],
) -> tuple[EvidenceLedgerEntry, ...]:
    if isinstance(evidence_entries, (str, bytes)) or not isinstance(
        evidence_entries, Sequence
    ):
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_evidence_entries_invalid"
        )
    replayed = []
    for item in evidence_entries:
        try:
            entry = EvidenceLedgerEntry.from_dict(item.to_dict())
        except (AttributeError, TypeError, ValueError) as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_evidence_entries_invalid"
            ) from exc
        if entry != item:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_evidence_entries_invalid"
            )
        replayed.append(entry)
    normalized = tuple(sorted(replayed, key=lambda item: item.entry_ref))
    if (
        len({item.entry_ref for item in normalized}) != len(normalized)
        or {item.entry_ref for item in normalized} != expected_refs
    ):
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_evidence_closure_invalid"
        )
    return normalized


def _fact_signature(
    fact: PublicFactDescriptor,
) -> tuple[str, str, str | None, str | None]:
    return fact.fact_kind, fact.value, fact.range_end, fact.unit


def _project_material_facts(
    *,
    evidence_entry_ref: str,
    candidates: Sequence[tuple[str, PublicFactDescriptor, str]],
) -> tuple[ProjectedEvidenceFact, ...]:
    views: dict[
        str,
        dict[
            str,
            tuple[
                tuple[str, str, str | None, str | None],
                list[PublicFactDescriptor],
            ],
        ],
    ] = {}
    for claim_ref, fact, normalized_name in candidates:
        claim_view = views.setdefault(claim_ref, {})
        signature = _fact_signature(fact)
        existing = claim_view.get(normalized_name)
        if existing is None:
            claim_view[normalized_name] = (signature, [fact])
        elif existing[0] == signature:
            existing[1].append(fact)
        else:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_shared_fact_conflict"
            )
    signatures = [
        {name: signature for name, (signature, _) in view.items()}
        for view in views.values()
    ]
    if signatures and any(item != signatures[0] for item in signatures[1:]):
        raise NarrativeMaterialProjectionContractError(
            "narrative_material_projection_shared_fact_conflict"
        )
    projected = []
    for name in sorted(signatures[0] if signatures else ()):  # all views are exact
        source_facts = tuple(fact for view in views.values() for fact in view[name][1])
        signature = signatures[0][name]
        projected.append(
            ProjectedEvidenceFact.create(
                evidence_entry_ref=evidence_entry_ref,
                source_fact_refs=tuple(item.fact_ref for item in source_facts),
                name=name,
                fact_kind=signature[0],
                value=signature[1],
                range_end=signature[2],
                unit=signature[3],
            )
        )
    return tuple(projected)


def _boundary_projection(
    limitations: Sequence[PublicLimitation],
) -> tuple[tuple[ProjectedLimitation, ...], tuple[BoundaryFacet, ...]]:
    occurrence_keys_by_limitation: dict[str, tuple[str, ...]] = {}
    facet_identity_by_key: dict[str, tuple[str, Mapping[str, Any]]] = {}
    limitation_refs_by_key: dict[str, set[str]] = {}
    for limitation in limitations:
        keys = []
        for raw_kind in sorted(limitation.public_context):
            kind = _required_string(
                raw_kind, "narrative_material_projection_facet_kind_invalid"
            )
            records = limitation.public_context[raw_kind]
            if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_limitation_context_invalid"
                )
            seen_in_section: set[str] = set()
            for record in records:
                if not isinstance(record, Mapping) or not record:
                    raise NarrativeMaterialProjectionContractError(
                        "narrative_material_projection_limitation_context_invalid"
                    )
                frozen = _freeze(
                    record,
                    "narrative_material_projection_limitation_context_invalid",
                )
                identity = {"facet_kind": kind, "context": frozen}
                key = canonical_digest(identity)
                if key in seen_in_section:
                    raise NarrativeMaterialProjectionContractError(
                        "narrative_material_projection_duplicate_boundary_facet"
                    )
                seen_in_section.add(key)
                keys.append(key)
                facet_identity_by_key[key] = (kind, frozen)
                limitation_refs_by_key.setdefault(key, set()).add(
                    limitation.limitation_ref
                )
        if not keys:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_limitation_context_invalid"
            )
        occurrence_keys_by_limitation[limitation.limitation_ref] = tuple(keys)
    facet_by_key = {
        key: BoundaryFacet.create(
            facet_kind=facet_identity_by_key[key][0],
            context=facet_identity_by_key[key][1],
            source_limitation_refs=tuple(limitation_refs_by_key[key]),
        )
        for key in sorted(facet_identity_by_key)
    }
    projected_limitations = tuple(
        ProjectedLimitation.create(
            limitation=limitation,
            facets=tuple(
                facet_by_key[key]
                for key in occurrence_keys_by_limitation[limitation.limitation_ref]
            ),
        )
        for limitation in sorted(limitations, key=lambda item: item.limitation_ref)
    )
    facets = tuple(
        sorted(facet_by_key.values(), key=lambda item: item.boundary_facet_ref)
    )
    return projected_limitations, facets


@dataclass(frozen=True)
class NarrativeMaterialProjection:
    projection_ref: str
    palette_ref: str
    palette_digest: str
    claim_settlement_ref: str
    claim_settlement_digest: str
    authority_mode: str
    claims: tuple[ProjectedNarrativeClaim, ...]
    publication_requirements: tuple[ProjectedPublicationRequirement, ...]
    evidence_materials: tuple[ProjectedEvidenceMaterial, ...]
    recommendations: tuple[PublicRecommendation, ...]
    limitations: tuple[ProjectedLimitation, ...]
    boundary_facets: tuple[BoundaryFacet, ...]
    content_digest: str

    @classmethod
    def derive(
        cls,
        *,
        palette: PublicClaimPalette,
        claim_settlement: ClaimSettlement,
        evidence_entries: Sequence[EvidenceLedgerEntry],
    ) -> "NarrativeMaterialProjection":
        if type(palette) is not PublicClaimPalette:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_palette_invalid"
            )
        if type(claim_settlement) is not ClaimSettlement:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_settlement_invalid"
            )
        raw_edge_by_ref = {
            item.support_edge_ref: item
            for item in claim_settlement.accepted_support_edges
        }
        for public_claim in palette.claims:
            for fact in public_claim.facts:
                if fact.source_material_ref not in raw_edge_by_ref:
                    raise NarrativeMaterialProjectionContractError(
                        "narrative_material_projection_source_edge_missing"
                    )
        try:
            settlement = ClaimSettlement.from_dict(claim_settlement.to_dict())
            validate_typed_claim_settlement(settlement)
        except (AttributeError, TypeError, ValueError) as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_settlement_invalid"
            ) from exc
        if settlement != claim_settlement:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_settlement_invalid"
            )
        public_claims = _validate_palette_against_settlement(palette, settlement)
        edge_by_ref = {
            item.support_edge_ref: item for item in settlement.accepted_support_edges
        }
        expected_evidence_refs = {
            item.source_ref
            for item in settlement.accepted_support_edges
            if item.source_type == "evidence"
        }
        if expected_evidence_refs != set(
            settlement.claim_graph.evidence_ceiling_by_ref
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_evidence_closure_invalid"
            )
        entries = _validated_evidence_entries(
            evidence_entries,
            expected_refs=expected_evidence_refs,
        )
        for entry in entries:
            ceiling = settlement.claim_graph.evidence_ceiling_by_ref[entry.entry_ref]
            if entry.maximum_claim_strength != ceiling.strength:
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_evidence_ceiling_mismatch"
                )

        fact_candidates_by_evidence: dict[
            str, list[tuple[str, PublicFactDescriptor, str]]
        ] = {ref: [] for ref in expected_evidence_refs}
        expected_fact_refs = {
            fact.fact_ref for claim in public_claims for fact in claim.facts
        }
        if len(expected_fact_refs) != sum(len(item.facts) for item in public_claims):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_fact_closure_invalid"
            )
        claim_material_refs: dict[str, tuple[str, ...]] = {}
        for public_claim in public_claims:
            accepted_claim = next(
                item
                for item in settlement.accepted_claims
                if item.claim_ref == public_claim.claim_ref
            )
            claim_edges = tuple(
                edge_by_ref[ref] for ref in accepted_claim.support_edge_refs
            )
            evidence_edges = tuple(
                sorted(
                    (edge for edge in claim_edges if edge.source_type == "evidence"),
                    key=lambda item: item.support_edge_ref,
                )
            )
            claim_material_refs[public_claim.claim_ref] = tuple(
                sorted(
                    {
                        edge.source_ref
                        for edge in evidence_edges
                        if edge.kind == "supports"
                    }
                )
            )
            fact_source_edges = tuple(
                edge for edge in evidence_edges if edge.kind == "supports"
            )
            source_index_by_edge_ref = {
                edge.support_edge_ref: index
                for index, edge in enumerate(fact_source_edges, start=1)
            }
            for fact in public_claim.facts:
                edge = edge_by_ref[fact.source_material_ref]
                if (
                    edge.kind != "supports"
                    or edge.source_type != "evidence"
                    or edge.target_claim_key != accepted_claim.claim_key
                    or edge.support_edge_ref not in source_index_by_edge_ref
                ):
                    raise NarrativeMaterialProjectionContractError(
                        "narrative_material_projection_fact_edge_invalid"
                    )
                normalized_name = _normalized_public_name(
                    fact,
                    expected_source_index=source_index_by_edge_ref[
                        edge.support_edge_ref
                    ],
                )
                fact_candidates_by_evidence[edge.source_ref].append(
                    (public_claim.claim_ref, fact, normalized_name)
                )

        edges_by_evidence = {
            ref: tuple(
                edge.support_edge_ref
                for edge in settlement.accepted_support_edges
                if edge.source_type == "evidence" and edge.source_ref == ref
            )
            for ref in expected_evidence_refs
        }
        materials = tuple(
            ProjectedEvidenceMaterial.create(
                evidence_entry=entry,
                evidence_edge_refs=edges_by_evidence[entry.entry_ref],
                facts=_project_material_facts(
                    evidence_entry_ref=entry.entry_ref,
                    candidates=fact_candidates_by_evidence[entry.entry_ref],
                ),
            )
            for entry in entries
        )
        projected_fact_refs = {
            ref
            for material in materials
            for fact in material.facts
            for ref in fact.source_fact_refs
        }
        if projected_fact_refs != expected_fact_refs or sum(
            len(fact.source_fact_refs)
            for material in materials
            for fact in material.facts
        ) != len(expected_fact_refs):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_fact_loss"
            )
        material_handle_by_ref = {
            item.evidence_entry_ref: item.material_handle for item in materials
        }
        accepted_claims_by_ref = {
            item.claim_ref: item for item in settlement.accepted_claims
        }
        projected_claims = tuple(
            ProjectedNarrativeClaim.create(
                public_claim=public_claim,
                evidence_entry_refs=claim_material_refs[public_claim.claim_ref],
                material_handle_by_evidence_ref=material_handle_by_ref,
                verified_claim_payload=accepted_claims_by_ref[
                    public_claim.claim_ref
                ].factual_payload,
            )
            for public_claim in public_claims
        )
        projected_limitations, facets = _boundary_projection(palette.limitations)
        claim_handle_by_ref = {
            item.claim_ref: item.claim_handle for item in projected_claims
        }
        limitation_handle_by_ref = {
            item.limitation_ref: item.limitation_handle
            for item in projected_limitations
        }
        projected_claim_by_ref = {
            item.claim_ref: item for item in projected_claims
        }
        material_by_handle = {
            item.material_handle: item for item in materials
        }
        basis_by_obligation_id = {
            item.obligation_id: item for item in settlement.checkpoint.obligation_basis
        }
        coverage_by_obligation_id = {
            item.obligation_id: item for item in settlement.obligation_coverage
        }
        try:
            publication_requirements_list = []
            for obligation_id in palette.required_obligation_ids:
                basis = basis_by_obligation_id[obligation_id]
                coverage = coverage_by_obligation_id[obligation_id]
                claim_kind, assertion_scope = _requirement_semantic_authority(
                    basis=basis,
                    coverage=coverage,
                    settlement=settlement,
                    palette=palette,
                )
                publication_requirements_list.append(
                    ProjectedPublicationRequirement.create(
                        basis=basis,
                        coverage=coverage,
                        accepted_claims_by_ref=accepted_claims_by_ref,
                        claim_handle_by_ref=claim_handle_by_ref,
                        limitation_handle_by_ref=limitation_handle_by_ref,
                        claim_kind=claim_kind,
                        assertion_scope=assertion_scope,
                        required_fact_handles=_requested_factor_fact_handles(
                            basis=basis,
                            assertion_scope=assertion_scope,
                            claim_refs=coverage.claim_refs,
                            projected_claim_by_ref=projected_claim_by_ref,
                            material_by_handle=material_by_handle,
                        ),
                    )
                )
            publication_requirements = tuple(publication_requirements_list)
        except KeyError as exc:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_required_obligations_invalid"
            ) from exc
        body = {
            "palette_ref": palette.palette_ref,
            "palette_digest": palette.content_digest,
            "claim_settlement_ref": settlement.settlement_ref,
            "claim_settlement_digest": settlement.content_digest,
            "authority_mode": palette.authority_mode,
            "claims": projected_claims,
            "publication_requirements": publication_requirements,
            "evidence_materials": materials,
            "recommendations": palette.recommendations,
            "limitations": projected_limitations,
            "boundary_facets": facets,
        }
        projection_ref, digest = _content_ref(
            "narrative-material-projection:sha256:", body
        )
        projection = cls(
            projection_ref=projection_ref,
            content_digest=digest,
            **body,
        )
        projection.assert_integrity()
        if projection.reconstruct_limitation_contexts() != {
            item.limitation_ref: canonical_value(item.public_context)
            for item in palette.limitations
        }:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_limitation_loss"
            )
        return projection

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        palette: PublicClaimPalette,
        claim_settlement: ClaimSettlement,
        evidence_entries: Sequence[EvidenceLedgerEntry],
    ) -> "NarrativeMaterialProjection":
        _strict_shape(
            payload,
            cls,
            "narrative_material_projection_shape_invalid",
        )
        rebuilt = cls.derive(
            palette=palette,
            claim_settlement=claim_settlement,
            evidence_entries=evidence_entries,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    def reconstruct_limitation_contexts(self) -> dict[str, Any]:
        facet_by_ref = {item.boundary_facet_ref: item for item in self.boundary_facets}
        reconstructed = {}
        for limitation in self.limitations:
            context: dict[str, list[Any]] = {}
            for facet_ref in limitation.boundary_facet_refs:
                try:
                    facet = facet_by_ref[facet_ref]
                except KeyError as exc:
                    raise NarrativeMaterialProjectionContractError(
                        "narrative_material_projection_integrity_invalid"
                    ) from exc
                context.setdefault(facet.facet_kind, []).append(
                    canonical_value(facet.context)
                )
            reconstructed[limitation.limitation_ref] = context
        return reconstructed

    def assert_integrity(self) -> None:
        for item in self.claims:
            item.assert_integrity()
        for item in self.publication_requirements:
            item.assert_integrity()
        for item in self.evidence_materials:
            item.assert_integrity()
        for item in self.recommendations:
            item.assert_integrity()
        for item in self.limitations:
            item.assert_integrity()
        for item in self.boundary_facets:
            item.assert_integrity()
        material_by_ref = {
            item.evidence_entry_ref: item for item in self.evidence_materials
        }
        material_by_handle = {
            item.material_handle: item for item in self.evidence_materials
        }
        facet_by_ref = {item.boundary_facet_ref: item for item in self.boundary_facets}
        facet_by_handle = {
            item.boundary_facet_handle: item for item in self.boundary_facets
        }
        claim_by_ref = {item.claim_ref: item for item in self.claims}
        claim_handle_by_ref = {
            ref: item.claim_handle for ref, item in claim_by_ref.items()
        }
        fact_handles_by_material = {
            item.material_handle: frozenset(
                fact.fact_handle for fact in item.facts
            )
            for item in self.evidence_materials
        }
        limitation_handle_by_ref = {
            item.limitation_ref: item.limitation_handle for item in self.limitations
        }
        if (
            len(material_by_ref) != len(self.evidence_materials)
            or len(material_by_handle) != len(self.evidence_materials)
            or len(facet_by_ref) != len(self.boundary_facets)
            or len(facet_by_handle) != len(self.boundary_facets)
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )
        for claim in self.claims:
            if (
                tuple(
                    material_by_ref[ref].material_handle
                    if ref in material_by_ref
                    else None
                    for ref in claim.evidence_entry_refs
                )
                != claim.material_handles
            ):
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_integrity_invalid"
                )
        for requirement in self.publication_requirements:
            requirement_fact_handles = frozenset(
                fact_handle
                for ref in requirement.claim_refs
                if ref in claim_by_ref
                for material_handle in claim_by_ref[ref].material_handles
                for fact_handle in fact_handles_by_material.get(
                    material_handle, frozenset()
                )
            )
            if (
                tuple(claim_handle_by_ref.get(ref) for ref in requirement.claim_refs)
                != requirement.claim_handles
                or tuple(
                    limitation_handle_by_ref.get(ref)
                    for ref in requirement.limitation_refs
                )
                != requirement.limitation_handles
                or not set(requirement.required_fact_handles).issubset(
                    requirement_fact_handles
                )
                or (
                    requirement.status == "satisfied"
                    and any(
                        ref not in claim_by_ref
                        or not publication_ceiling_satisfies(
                            claim_by_ref[ref].publication_ceiling,
                            required_strength=requirement.required_claim_strength,
                        )
                        for ref in requirement.claim_refs
                    )
                )
            ):
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_requirement_closure_invalid"
                )
        limitation_refs_by_facet: dict[str, set[str]] = {
            ref: set() for ref in facet_by_ref
        }
        for limitation in self.limitations:
            if (
                tuple(
                    facet_by_ref[ref].boundary_facet_handle
                    if ref in facet_by_ref
                    else None
                    for ref in limitation.boundary_facet_refs
                )
                != limitation.boundary_facet_handles
            ):
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_integrity_invalid"
                )
            for ref in limitation.boundary_facet_refs:
                limitation_refs_by_facet[ref].add(limitation.limitation_ref)
        if any(
            tuple(sorted(limitation_refs_by_facet[ref])) != facet.source_limitation_refs
            for ref, facet in facet_by_ref.items()
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )
        reconstructed = self.reconstruct_limitation_contexts()
        for limitation in self.limitations:
            if (
                canonical_digest(
                    {
                        "limitation_ref": limitation.limitation_ref,
                        "public_context": reconstructed[limitation.limitation_ref],
                    }
                )
                != limitation.limitation_digest
            ):
                raise NarrativeMaterialProjectionContractError(
                    "narrative_material_projection_integrity_invalid"
                )
        source_fact_refs = [
            source_ref
            for material in self.evidence_materials
            for fact in material.facts
            for source_ref in fact.source_fact_refs
        ]
        evidence_edge_refs = [
            evidence_edge_ref
            for material in self.evidence_materials
            for evidence_edge_ref in material.evidence_edge_refs
        ]
        all_handles = [item.claim_handle for item in self.claims]
        all_handles.extend(
            item.requirement_handle for item in self.publication_requirements
        )
        all_handles.extend(item.material_handle for item in self.evidence_materials)
        all_handles.extend(
            fact.fact_handle
            for material in self.evidence_materials
            for fact in material.facts
        )
        all_handles.extend(item.recommendation_handle for item in self.recommendations)
        all_handles.extend(item.limitation_handle for item in self.limitations)
        all_handles.extend(item.boundary_facet_handle for item in self.boundary_facets)
        if (
            len(source_fact_refs) != len(set(source_fact_refs))
            or len(evidence_edge_refs) != len(set(evidence_edge_refs))
            or len(all_handles) != len(set(all_handles))
            or len({item.obligation_id for item in self.publication_requirements})
            != len(self.publication_requirements)
            or (self.authority_mode == "claim_bearing" and not self.claims)
            or (
                self.authority_mode == "boundary_only"
                and (self.claims or self.evidence_materials or self.recommendations)
            )
            or self.authority_mode not in {"claim_bearing", "boundary_only"}
        ):
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )
        body = {
            "palette_ref": self.palette_ref,
            "palette_digest": self.palette_digest,
            "claim_settlement_ref": self.claim_settlement_ref,
            "claim_settlement_digest": self.claim_settlement_digest,
            "authority_mode": self.authority_mode,
            "claims": self.claims,
            "publication_requirements": self.publication_requirements,
            "evidence_materials": self.evidence_materials,
            "recommendations": self.recommendations,
            "limitations": self.limitations,
            "boundary_facets": self.boundary_facets,
        }
        expected_ref, digest = _content_ref(
            "narrative-material-projection:sha256:", body
        )
        if self.projection_ref != expected_ref or self.content_digest != digest:
            raise NarrativeMaterialProjectionContractError(
                "narrative_material_projection_integrity_invalid"
            )

    def to_writer_payload(self) -> dict[str, Any]:
        self.assert_integrity()
        return {
            "authority_mode": self.authority_mode,
            "claims": [item.to_writer_payload() for item in self.claims],
            "publication_requirements": [
                item.to_writer_payload() for item in self.publication_requirements
            ],
            "evidence_materials": [
                item.to_writer_payload() for item in self.evidence_materials
            ],
            "recommendations": [
                item.to_writer_payload() for item in self.recommendations
            ],
            "limitations": [item.to_writer_payload() for item in self.limitations],
            "boundary_facets": [
                item.to_writer_payload() for item in self.boundary_facets
            ],
        }


__all__ = (
    "BoundaryFacet",
    "NarrativeMaterialProjection",
    "NarrativeMaterialProjectionContractError",
    "ProjectedEvidenceFact",
    "ProjectedEvidenceMaterial",
    "ProjectedLimitation",
    "ProjectedNarrativeClaim",
    "ProjectedPublicationRequirement",
)
