from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from bi_agent.runtime.capability_authority import EvidenceLedgerEntry
from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    ClaimAuthorityNamespace,
    ClaimKey,
    ClaimRevision,
    SupportEdge,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.narrative_authority import (
    NarrativeAuthorityContractError,
    PublicationFieldVisibilityPolicy,
    PublicFactDescriptor,
)


class PublicFactMaterializationContractError(ValueError):
    pass


MATERIALIZATION_STATES = frozenset({"ready", "incomplete", "boundary_only"})
MATERIALIZATION_ISSUE_CODES = frozenset(
    {
        "empty_structure",
        "field_visibility_blocked",
        "named_fact_shape_ambiguous",
        "null_not_public_fact",
        "boolean_not_public_fact",
        "datetime_not_public_fact",
        "public_fact_contract_rejected",
        "public_name_collision",
        "unsupported_scalar_type",
    }
)
_TYPED_FACT_FIELDS = frozenset({"name", "fact_kind", "value", "range_end", "unit"})
_MATERIAL_METADATA_FIELDS = frozenset({"interpretation_contract", "synthesis_contract"})


def _plain(value: Any) -> Any:
    return canonical_value(value)


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PublicFactMaterializationContractError(error)
    return value


def _digest(value: Any, error: str) -> str:
    value = _required_string(value, error)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise PublicFactMaterializationContractError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = True,
    unique: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PublicFactMaterializationContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise PublicFactMaterializationContractError(error)
    if unique and len(normalized) != len(set(normalized)):
        raise PublicFactMaterializationContractError(error)
    return tuple(sorted(normalized)) if sort else normalized


def _strict_shape(
    payload: Any,
    record_type: type,
    error: str,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != set(
        record_type.__dataclass_fields__
    ):
        raise PublicFactMaterializationContractError(error)
    return payload


def _path_name(path: Sequence[str]) -> str:
    if not path:
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_path_empty"
        )
    rendered = ""
    for segment in path:
        if segment.startswith("["):
            rendered += segment
        else:
            rendered += ("." if rendered else "") + segment
    return rendered


def _bundle_manifest(bundle: AuthorityBundle) -> Mapping[str, Any]:
    return {
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


def _validate_bundle(
    bundle: AuthorityBundle,
    namespace: ClaimAuthorityNamespace,
) -> AuthorityBundle:
    if type(bundle) is not AuthorityBundle:
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_bundle_invalid"
        )
    if type(namespace) is not ClaimAuthorityNamespace:
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_namespace_invalid"
        )
    try:
        replayed_namespace = ClaimAuthorityNamespace.from_dict(namespace.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_namespace_invalid"
        ) from exc
    manifest = _bundle_manifest(bundle)
    digest = canonical_digest(manifest)
    namespace_prefix = "claim-authority-namespace:sha256:"
    if not namespace.authority_namespace_ref.startswith(namespace_prefix):
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_namespace_invalid"
        )
    namespace_token = namespace.authority_namespace_ref.removeprefix(namespace_prefix)[
        :24
    ]
    if (
        replayed_namespace != namespace
        or bundle.authority_namespace_ref != namespace.authority_namespace_ref
        or bundle.run_attempt_id != namespace.run_attempt_id
        or bundle.bundle_digest != digest
        or bundle.content_digest != digest
        or bundle.bundle_ref != f"authority-bundle:{namespace_token}:sha256:{digest}"
        or bundle.seal_state != "sealed"
        or _normalized_utc_timestamp(bundle.sealed_at) != bundle.sealed_at
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
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_bundle_integrity_invalid"
        )
    return bundle


def _normalized_utc_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PublicFactMaterializationIssue:
    issue_ref: str
    claim_ref: str
    source_material_ref: str
    evidence_entry_ref: str
    observation_path: tuple[str, ...]
    issue_code: str
    source_value_digest: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        claim_ref: str,
        source_material_ref: str,
        evidence_entry_ref: str,
        observation_path: Sequence[str],
        issue_code: str,
        source_value: Any,
    ) -> "PublicFactMaterializationIssue":
        if issue_code not in MATERIALIZATION_ISSUE_CODES:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_issue_code_invalid"
            )
        body = {
            "claim_ref": _required_string(
                claim_ref, "public_fact_materialization_issue_claim_ref_invalid"
            ),
            "source_material_ref": _required_string(
                source_material_ref,
                "public_fact_materialization_issue_material_ref_invalid",
            ),
            "evidence_entry_ref": _required_string(
                evidence_entry_ref,
                "public_fact_materialization_issue_evidence_ref_invalid",
            ),
            "observation_path": _string_tuple(
                observation_path,
                "public_fact_materialization_issue_path_invalid",
                allow_empty=False,
                sort=False,
                unique=False,
            ),
            "issue_code": issue_code,
            "source_value_digest": canonical_digest(source_value),
        }
        digest = canonical_digest(body)
        return cls(
            issue_ref="public-fact-materialization-issue:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "PublicFactMaterializationIssue":
        payload = _strict_shape(
            payload,
            cls,
            "public_fact_materialization_issue_shape_invalid",
        )
        issue_code = payload["issue_code"]
        if issue_code not in MATERIALIZATION_ISSUE_CODES:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_issue_code_invalid"
            )
        source_value_digest = _digest(
            payload["source_value_digest"],
            "public_fact_materialization_issue_value_digest_invalid",
        )
        body = {
            "claim_ref": _required_string(
                payload["claim_ref"],
                "public_fact_materialization_issue_claim_ref_invalid",
            ),
            "source_material_ref": _required_string(
                payload["source_material_ref"],
                "public_fact_materialization_issue_material_ref_invalid",
            ),
            "evidence_entry_ref": _required_string(
                payload["evidence_entry_ref"],
                "public_fact_materialization_issue_evidence_ref_invalid",
            ),
            "observation_path": _string_tuple(
                payload["observation_path"],
                "public_fact_materialization_issue_path_invalid",
                allow_empty=False,
                sort=False,
                unique=False,
            ),
            "issue_code": issue_code,
            "source_value_digest": source_value_digest,
        }
        digest = canonical_digest(body)
        expected = cls(
            issue_ref="public-fact-materialization-issue:sha256:" + digest,
            content_digest=digest,
            **body,
        )
        if expected.to_dict() != canonical_value(payload):
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_issue_integrity_invalid"
            )
        return expected

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class PublicFactMaterialization:
    materialization_ref: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    field_visibility_policy_ref: str
    field_visibility_policy_digest: str
    materialization_state: str
    public_facts: tuple[PublicFactDescriptor, ...]
    issues: tuple[PublicFactMaterializationIssue, ...]
    claims_without_public_facts: tuple[str, ...]
    content_digest: str

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_bundle: AuthorityBundle,
        authority_namespace: ClaimAuthorityNamespace,
        claims: Sequence[ClaimRevision],
        claim_keys: Sequence[ClaimKey],
        support_edges: Sequence[SupportEdge],
        evidence_entries: Sequence[EvidenceLedgerEntry],
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> "PublicFactMaterialization":
        _strict_shape(
            payload,
            cls,
            "public_fact_materialization_shape_invalid",
        )
        rebuilt = materialize_public_facts(
            authority_bundle=authority_bundle,
            authority_namespace=authority_namespace,
            claims=claims,
            claim_keys=claim_keys,
            support_edges=support_edges,
            evidence_entries=evidence_entries,
            visibility_policy=visibility_policy,
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_integrity_invalid"
            )
        return rebuilt

    def replay(
        self,
        *,
        authority_bundle: AuthorityBundle,
        authority_namespace: ClaimAuthorityNamespace,
        claims: Sequence[ClaimRevision],
        claim_keys: Sequence[ClaimKey],
        support_edges: Sequence[SupportEdge],
        evidence_entries: Sequence[EvidenceLedgerEntry],
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> "PublicFactMaterialization":
        return PublicFactMaterialization.from_dict(
            self.to_dict(),
            authority_bundle=authority_bundle,
            authority_namespace=authority_namespace,
            claims=claims,
            claim_keys=claim_keys,
            support_edges=support_edges,
            evidence_entries=evidence_entries,
            visibility_policy=visibility_policy,
        )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class _FactCandidate:
    claim: ClaimRevision
    source_material_ref: str
    evidence_entry_ref: str
    observation_path: tuple[str, ...]
    public_name: str
    fact_kind: str
    value: Any
    range_end: Any
    unit: str | None


@dataclass(frozen=True)
class _Extraction:
    candidates: tuple[_FactCandidate, ...]
    issues: tuple[PublicFactMaterializationIssue, ...]


def _issue(
    *,
    claim: ClaimRevision,
    edge: SupportEdge,
    entry: EvidenceLedgerEntry,
    path: Sequence[str],
    issue_code: str,
    value: Any,
) -> PublicFactMaterializationIssue:
    return PublicFactMaterializationIssue.create(
        claim_ref=claim.claim_ref,
        source_material_ref=edge.support_edge_ref,
        evidence_entry_ref=entry.entry_ref,
        observation_path=path,
        issue_code=issue_code,
        source_value=value,
    )


def _typed_scalar(value: Any) -> tuple[str, Any, Any, str | None] | None:
    if isinstance(value, Mapping):
        if set(value) == {"$decimal"}:
            raw = value["$decimal"]
            if not isinstance(raw, str):
                return None
            try:
                number = Decimal(raw)
            except InvalidOperation:
                return None
            if not number.is_finite():
                return None
            return "number", number, None, None
        if set(value) == {"$date"}:
            raw = value["$date"]
            if not isinstance(raw, str):
                return None
            try:
                if date.fromisoformat(raw).isoformat() != raw:
                    return None
            except ValueError:
                return None
            return "date", raw, None, None
        return None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return "number", value, None, None
    if isinstance(value, date):
        return "date", value.isoformat(), None, None
    if isinstance(value, str):
        return "label", value, None, None
    return None


def _declared_fact(
    value: Mapping[str, Any],
) -> tuple[str, str, Any, Any, str | None] | None:
    fields = set(value)
    if not {"name", "value"}.issubset(fields) or not fields.issubset(
        _TYPED_FACT_FIELDS
    ):
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name or name != name.strip():
        return None
    declared_kind = value.get("fact_kind")
    if declared_kind is None:
        scalar = _typed_scalar(value.get("value"))
        if scalar is None:
            return None
        kind, normalized, range_end, unit = scalar
        return name, kind, normalized, range_end, unit
    if declared_kind not in {"number", "date", "date_range", "scope", "label"}:
        return None
    normalized_value = _unwrap_declared_value(
        value.get("value"),
        declared_kind="date" if declared_kind == "date_range" else declared_kind,
    )
    normalized_end = (
        _unwrap_declared_value(value.get("range_end"), declared_kind="date")
        if declared_kind == "date_range"
        else value.get("range_end")
    )
    unit = value.get("unit")
    if unit is not None and (
        not isinstance(unit, str) or not unit or unit != unit.strip()
    ):
        return None
    return (
        name,
        declared_kind,
        normalized_value,
        normalized_end,
        unit,
    )


def _unwrap_declared_value(value: Any, *, declared_kind: str) -> Any:
    if declared_kind == "number" and isinstance(value, Mapping):
        scalar = _typed_scalar(value)
        if scalar is not None and scalar[0] == "number":
            return scalar[1]
    if declared_kind == "date" and isinstance(value, Mapping):
        scalar = _typed_scalar(value)
        if scalar is not None and scalar[0] == "date":
            return scalar[1]
    return value


def _extract_value(
    *,
    claim: ClaimRevision,
    edge: SupportEdge,
    entry: EvidenceLedgerEntry,
    visibility_policy: PublicationFieldVisibilityPolicy,
    value: Any,
    path: tuple[str, ...],
    public_path: tuple[str, ...],
) -> _Extraction:
    if isinstance(value, Mapping):
        if set(value) == {"$datetime"}:
            return _Extraction(
                (),
                (
                    _issue(
                        claim=claim,
                        edge=edge,
                        entry=entry,
                        path=path,
                        issue_code="datetime_not_public_fact",
                        value=value,
                    ),
                ),
            )
        scalar = _typed_scalar(value)
        if scalar is not None:
            kind, normalized, range_end, unit = scalar
            return _Extraction(
                (
                    _FactCandidate(
                        claim=claim,
                        source_material_ref=edge.support_edge_ref,
                        evidence_entry_ref=entry.entry_ref,
                        observation_path=path,
                        public_name=_path_name(public_path),
                        fact_kind=kind,
                        value=normalized,
                        range_end=range_end,
                        unit=unit,
                    ),
                ),
                (),
            )
        fields = set(value)
        if {"name", "value"}.issubset(fields) and fields.issubset(_TYPED_FACT_FIELDS):
            declared = _declared_fact(value)
            if declared is None:
                return _Extraction(
                    (),
                    (
                        _issue(
                            claim=claim,
                            edge=edge,
                            entry=entry,
                            path=path,
                            issue_code="named_fact_shape_ambiguous",
                            value=value,
                        ),
                    ),
                )
            name, kind, normalized, range_end, unit = declared
            if name in set(visibility_policy.forbidden_fields):
                return _Extraction(
                    (),
                    (
                        _issue(
                            claim=claim,
                            edge=edge,
                            entry=entry,
                            path=(*path, name),
                            issue_code="field_visibility_blocked",
                            value=value,
                        ),
                    ),
                )
            return _Extraction(
                (
                    _FactCandidate(
                        claim=claim,
                        source_material_ref=edge.support_edge_ref,
                        evidence_entry_ref=entry.entry_ref,
                        observation_path=(*path, name),
                        public_name=_path_name((*public_path, name)),
                        fact_kind=kind,
                        value=normalized,
                        range_end=range_end,
                        unit=unit,
                    ),
                ),
                (),
            )
        if not value:
            return _Extraction(
                (),
                (
                    _issue(
                        claim=claim,
                        edge=edge,
                        entry=entry,
                        path=path,
                        issue_code="empty_structure",
                        value=value,
                    ),
                ),
            )
        candidates: list[_FactCandidate] = []
        issues: list[PublicFactMaterializationIssue] = []
        for key in sorted(value):
            if key in _MATERIAL_METADATA_FIELDS:
                continue
            if key in set(visibility_policy.forbidden_fields):
                issues.append(
                    _issue(
                        claim=claim,
                        edge=edge,
                        entry=entry,
                        path=(*path, key),
                        issue_code="field_visibility_blocked",
                        value=value[key],
                    )
                )
                continue
            extracted = _extract_value(
                claim=claim,
                edge=edge,
                entry=entry,
                visibility_policy=visibility_policy,
                value=value[key],
                path=(*path, key),
                public_path=(*public_path, key),
            )
            candidates.extend(extracted.candidates)
            issues.extend(extracted.issues)
        return _Extraction(tuple(candidates), tuple(issues))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            return _Extraction(
                (),
                (
                    _issue(
                        claim=claim,
                        edge=edge,
                        entry=entry,
                        path=path,
                        issue_code="empty_structure",
                        value=value,
                    ),
                ),
            )
        candidates = []
        issues = []
        for index, item in enumerate(value):
            marker = f"[{index}]"
            extracted = _extract_value(
                claim=claim,
                edge=edge,
                entry=entry,
                visibility_policy=visibility_policy,
                value=item,
                path=(*path, marker),
                public_path=(*public_path, marker),
            )
            candidates.extend(extracted.candidates)
            issues.extend(extracted.issues)
        return _Extraction(tuple(candidates), tuple(issues))
    if value is None:
        code = "null_not_public_fact"
    elif isinstance(value, bool):
        code = "boolean_not_public_fact"
    else:
        scalar = _typed_scalar(value)
        if scalar is not None:
            kind, normalized, range_end, unit = scalar
            return _Extraction(
                (
                    _FactCandidate(
                        claim=claim,
                        source_material_ref=edge.support_edge_ref,
                        evidence_entry_ref=entry.entry_ref,
                        observation_path=path,
                        public_name=_path_name(public_path),
                        fact_kind=kind,
                        value=normalized,
                        range_end=range_end,
                        unit=unit,
                    ),
                ),
                (),
            )
        code = "unsupported_scalar_type"
    return _Extraction(
        (),
        (
            _issue(
                claim=claim,
                edge=edge,
                entry=entry,
                path=path,
                issue_code=code,
                value=value,
            ),
        ),
    )


def _replay_inputs(
    *,
    authority_bundle: AuthorityBundle,
    authority_namespace: ClaimAuthorityNamespace,
    claims: Sequence[ClaimRevision],
    claim_keys: Sequence[ClaimKey],
    support_edges: Sequence[SupportEdge],
    evidence_entries: Sequence[EvidenceLedgerEntry],
    visibility_policy: PublicationFieldVisibilityPolicy,
) -> tuple[
    tuple[ClaimRevision, ...],
    tuple[SupportEdge, ...],
    tuple[EvidenceLedgerEntry, ...],
    PublicationFieldVisibilityPolicy,
]:
    _validate_bundle(authority_bundle, authority_namespace)
    try:
        policy = PublicationFieldVisibilityPolicy.from_dict(visibility_policy.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_visibility_policy_invalid"
        ) from exc
    if isinstance(claim_keys, (str, bytes)) or not isinstance(claim_keys, Sequence):
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_claim_keys_invalid"
        )
    keys: list[ClaimKey] = []
    for item in claim_keys:
        try:
            replayed = ClaimKey.from_dict(
                item.to_dict(), authority_namespace=authority_namespace
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_claim_keys_invalid"
            ) from exc
        if replayed != item:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_claim_keys_invalid"
            )
        keys.append(replayed)
    key_by_ref = {item.claim_key: item for item in keys}
    if len(key_by_ref) != len(keys):
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_claim_keys_duplicated"
        )
    if isinstance(support_edges, (str, bytes)) or not isinstance(
        support_edges, Sequence
    ):
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_support_edges_invalid"
        )
    edges: list[SupportEdge] = []
    for item in support_edges:
        try:
            replayed = SupportEdge.from_dict(
                item.to_dict(), authority_namespace=authority_namespace
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_support_edges_invalid"
            ) from exc
        if replayed != item:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_support_edges_invalid"
            )
        edges.append(replayed)
    edge_by_ref = {item.support_edge_ref: item for item in edges}
    if len(edge_by_ref) != len(edges):
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_support_edges_duplicated"
        )
    if isinstance(claims, (str, bytes)) or not isinstance(claims, Sequence):
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_claims_invalid"
        )
    replayed_claims: list[ClaimRevision] = []
    for item in claims:
        if type(item) is not ClaimRevision:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_claims_invalid"
            )
        try:
            key = key_by_ref[item.claim_key]
            claim_edges = tuple(edge_by_ref[ref] for ref in item.support_edge_refs)
            replayed = ClaimRevision.from_dict(
                item.to_dict(),
                authority_namespace=authority_namespace,
                claim_key=key,
                support_edges=claim_edges,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_claims_invalid"
            ) from exc
        if replayed != item or replayed.status != "verified":
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_claims_invalid"
            )
        replayed_claims.append(replayed)
    normalized_claims = tuple(sorted(replayed_claims, key=lambda item: item.claim_ref))
    if tuple(item.claim_ref for item in normalized_claims) != tuple(
        sorted(authority_bundle.verified_claim_refs)
    ):
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_bundle_claim_closure_invalid"
        )
    expected_keys = {item.claim_key for item in normalized_claims}
    if set(key_by_ref) != expected_keys:
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_claim_key_closure_invalid"
        )
    expected_edges = {
        ref for item in normalized_claims for ref in item.support_edge_refs
    }
    if set(edge_by_ref) != expected_edges:
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_support_edge_closure_invalid"
        )
    if isinstance(evidence_entries, (str, bytes)) or not isinstance(
        evidence_entries, Sequence
    ):
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_evidence_invalid"
        )
    entries: list[EvidenceLedgerEntry] = []
    for item in evidence_entries:
        try:
            replayed = EvidenceLedgerEntry.from_dict(item.to_dict())
        except (AttributeError, TypeError, ValueError) as exc:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_evidence_invalid"
            ) from exc
        if replayed != item:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_evidence_invalid"
            )
        entries.append(replayed)
    entry_by_ref = {item.entry_ref: item for item in entries}
    if len(entry_by_ref) != len(entries):
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_evidence_duplicated"
        )
    expected_evidence_refs = {
        edge.source_ref
        for edge in edges
        if edge.kind == "supports" and edge.source_type == "evidence"
    }
    if set(entry_by_ref) != expected_evidence_refs:
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_evidence_closure_invalid"
        )
    if not expected_evidence_refs.issubset(set(authority_bundle.evidence_refs)):
        raise PublicFactMaterializationContractError(
            "public_fact_materialization_bundle_evidence_closure_invalid"
        )
    for entry in entries:
        if (
            entry.run_attempt_id != authority_bundle.run_attempt_id
            or entry.authority_context_ref != authority_bundle.authority_context_ref
            or entry.plan_revision_id != authority_bundle.plan_revision_id
            or entry.execution_state != "available"
        ):
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_evidence_authority_invalid"
            )
    _reject_embedded_evidence_copies(normalized_claims)
    return (
        normalized_claims,
        tuple(sorted(edges, key=lambda item: item.support_edge_ref)),
        tuple(sorted(entries, key=lambda item: item.entry_ref)),
        policy,
    )


def _reject_embedded_evidence_copies(
    claims: Sequence[ClaimRevision],
) -> None:
    for claim in claims:
        if "evidence_observations" in claim.factual_payload:
            raise PublicFactMaterializationContractError(
                "public_fact_materialization_embedded_evidence_forbidden"
            )


def materialize_public_facts(
    *,
    authority_bundle: AuthorityBundle,
    authority_namespace: ClaimAuthorityNamespace,
    claims: Sequence[ClaimRevision],
    claim_keys: Sequence[ClaimKey],
    support_edges: Sequence[SupportEdge],
    evidence_entries: Sequence[EvidenceLedgerEntry],
    visibility_policy: PublicationFieldVisibilityPolicy,
) -> PublicFactMaterialization:
    normalized_claims, normalized_edges, normalized_entries, policy = _replay_inputs(
        authority_bundle=authority_bundle,
        authority_namespace=authority_namespace,
        claims=claims,
        claim_keys=claim_keys,
        support_edges=support_edges,
        evidence_entries=evidence_entries,
        visibility_policy=visibility_policy,
    )
    if authority_bundle.authority_mode == "boundary_only":
        body = {
            "authority_bundle_ref": authority_bundle.bundle_ref,
            "authority_bundle_digest": authority_bundle.bundle_digest,
            "field_visibility_policy_ref": policy.policy_ref,
            "field_visibility_policy_digest": policy.content_digest,
            "materialization_state": "boundary_only",
            "public_facts": (),
            "issues": (),
            "claims_without_public_facts": (),
        }
        digest = canonical_digest(body)
        return PublicFactMaterialization(
            materialization_ref="public-fact-materialization:sha256:" + digest,
            content_digest=digest,
            **body,
        )
    entry_by_ref = {item.entry_ref: item for item in normalized_entries}
    edges_by_claim: dict[str, list[SupportEdge]] = {
        claim.claim_key: [] for claim in normalized_claims
    }
    for edge in normalized_edges:
        if edge.kind == "supports" and edge.source_type == "evidence":
            edges_by_claim[edge.target_claim_key].append(edge)
    all_candidates: list[_FactCandidate] = []
    issues: list[PublicFactMaterializationIssue] = []
    for claim in normalized_claims:
        claim_edges = tuple(
            sorted(
                edges_by_claim[claim.claim_key],
                key=lambda item: item.support_edge_ref,
            )
        )
        for source_index, edge in enumerate(claim_edges, start=1):
            entry = entry_by_ref[edge.source_ref]
            source_path = (f"source_{source_index}",)
            all_candidates.append(
                _FactCandidate(
                    claim=claim,
                    source_material_ref=edge.support_edge_ref,
                    evidence_entry_ref=entry.entry_ref,
                    observation_path=("scope",),
                    public_name=_path_name((*source_path, "evidence_scope")),
                    fact_kind="scope",
                    value=entry.scope,
                    range_end=None,
                    unit=None,
                )
            )
            observation_count = len(entry.observation_facts)
            for observation_index, observation in enumerate(
                entry.observation_facts,
                start=1,
            ):
                observation_marker = f"observation_{observation_index}"
                public_path = (
                    (*source_path, observation_marker)
                    if observation_count > 1
                    else source_path
                )
                extracted = _extract_value(
                    claim=claim,
                    edge=edge,
                    entry=entry,
                    visibility_policy=policy,
                    value=observation,
                    path=(observation_marker,),
                    public_path=public_path,
                )
                all_candidates.extend(extracted.candidates)
                issues.extend(extracted.issues)
    descriptors: list[PublicFactDescriptor] = []
    claims_with_observation_facts: set[str] = set()
    candidates_by_name: dict[tuple[str, str], list[_FactCandidate]] = {}
    for candidate in all_candidates:
        candidates_by_name.setdefault(
            (candidate.claim.claim_ref, candidate.public_name), []
        ).append(candidate)
    for _, candidates in sorted(candidates_by_name.items()):
        candidate_identities = {
            canonical_digest(
                {
                    "source_material_ref": item.source_material_ref,
                    "fact_kind": item.fact_kind,
                    "value": item.value,
                    "range_end": item.range_end,
                    "unit": item.unit,
                }
            )
            for item in candidates
        }
        if len(candidate_identities) > 1:
            for item in candidates:
                edge = next(
                    edge
                    for edge in normalized_edges
                    if edge.support_edge_ref == item.source_material_ref
                )
                issues.append(
                    _issue(
                        claim=item.claim,
                        edge=edge,
                        entry=entry_by_ref[item.evidence_entry_ref],
                        path=item.observation_path,
                        issue_code="public_name_collision",
                        value={
                            "public_name": item.public_name,
                            "candidate_identity": canonical_digest(
                                {
                                    "fact_kind": item.fact_kind,
                                    "value": item.value,
                                    "range_end": item.range_end,
                                    "unit": item.unit,
                                }
                            ),
                        },
                    )
                )
            continue
        item = candidates[0]
        try:
            policy.assert_public_name(item.public_name)
            descriptor = PublicFactDescriptor.create(
                claim=item.claim,
                public_name=item.public_name,
                fact_kind=item.fact_kind,
                value=item.value,
                range_end=item.range_end,
                unit=item.unit,
                source_material_ref=item.source_material_ref,
            )
        except (NarrativeAuthorityContractError, TypeError, ValueError):
            edge = next(
                edge
                for edge in normalized_edges
                if edge.support_edge_ref == item.source_material_ref
            )
            issues.append(
                _issue(
                    claim=item.claim,
                    edge=edge,
                    entry=entry_by_ref[item.evidence_entry_ref],
                    path=item.observation_path,
                    issue_code="public_fact_contract_rejected",
                    value={
                        "public_name": item.public_name,
                        "fact_kind": item.fact_kind,
                        "value": item.value,
                        "range_end": item.range_end,
                        "unit": item.unit,
                    },
                )
            )
            continue
        descriptors.append(descriptor)
        if item.observation_path != ("scope",):
            claims_with_observation_facts.add(item.claim.claim_ref)
    normalized_descriptors = tuple(sorted(descriptors, key=lambda item: item.fact_ref))
    issue_by_ref = {item.issue_ref: item for item in issues}
    normalized_issues = tuple(issue_by_ref[ref] for ref in sorted(issue_by_ref))
    claims_without_facts = tuple(
        claim.claim_ref
        for claim in normalized_claims
        if claim.claim_ref not in claims_with_observation_facts
    )
    state = "incomplete" if claims_without_facts else "ready"
    body = {
        "authority_bundle_ref": authority_bundle.bundle_ref,
        "authority_bundle_digest": authority_bundle.bundle_digest,
        "field_visibility_policy_ref": policy.policy_ref,
        "field_visibility_policy_digest": policy.content_digest,
        "materialization_state": state,
        "public_facts": normalized_descriptors,
        "issues": normalized_issues,
        "claims_without_public_facts": claims_without_facts,
    }
    digest = canonical_digest(body)
    return PublicFactMaterialization(
        materialization_ref="public-fact-materialization:sha256:" + digest,
        content_digest=digest,
        **body,
    )


__all__ = (
    "MATERIALIZATION_ISSUE_CODES",
    "MATERIALIZATION_STATES",
    "PublicFactMaterialization",
    "PublicFactMaterializationContractError",
    "PublicFactMaterializationIssue",
    "materialize_public_facts",
)
