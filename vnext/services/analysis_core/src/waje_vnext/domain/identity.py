"""Canonical identity and closed scope relations for measurement authority."""

from __future__ import annotations

import hashlib
import json
import calendar
import unicodedata
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .canonical import content_sha256, require_nonempty, require_sha256
from .measurement import (
    IDENTITY_ALGORITHM_VERSION,
    AnalysisFrameRevision,
    AmbiguousLocalTimePolicy,
    ClaimStrengthCeiling,
    EstimandSpec,
    MeasurementDesign,
    MeasurementResolutionOutcome,
    QuestionRevision,
    RequirementResolutionBoundary,
    ResolvedMeasurementInstance,
    ResolutionOutcomeKind,
    ScopeExpression,
    TypedResolutionBoundary,
    WindowRuleKind,
    WindowSelectionKind,
)


class IdentityKind(StrEnum):
    SEMANTIC_MEASUREMENT = "semantic_measurement"
    AUTHORITY_BINDING = "authority_binding"
    RESOLUTION_OUTCOME = "resolution_outcome"
    RESOLUTION = "resolution"
    LOGICAL_EXECUTION = "logical_execution"


class ScopeRelationKind(StrEnum):
    EXACT = "exact"
    SUBSET = "subset"
    SUPERSET = "superset"
    LAWFUL_PROJECTION = "lawful_projection"
    LAWFUL_AGGREGATION = "lawful_aggregation"
    DISJOINT = "disjoint"
    UNKNOWN = "unknown"


TRUSTED_RESOLVER_CONTRACT_REFS = frozenset(
    {"waje-vnext://measurement-resolver/gregorian.v1"}
)
TRUSTED_BOUNDARY_POLICY_REFS = frozenset(
    {"waje-vnext://measurement-boundary-policy/registry.v1"}
)
TRUSTED_RESOLUTION_BOUNDARY_CODES = frozenset(
    {
        "missing_anchor",
        "unsupported_calendar",
        "missing_calendar_contract",
        "invalid_window_rule",
        "snapshot_out_of_range",
        "incomplete_period",
        "insufficient_valid_exposure",
        "incompatible_unit",
        "incomparable_exposure",
    }
)


@dataclass(frozen=True, slots=True)
class IdentityPreimage:
    identity_algorithm_version: str
    identity_kind: IdentityKind
    material_payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.identity_algorithm_version != IDENTITY_ALGORITHM_VERSION:
            raise ValueError("identity algorithm version is unsupported")
        if not isinstance(self.identity_kind, IdentityKind):
            raise TypeError("identity_kind must be IdentityKind")
        if not isinstance(self.material_payload, Mapping):
            raise TypeError("material_payload must be a mapping")

    @property
    def identity_sha256(self) -> str:
        return hashlib.sha256(
            canonical_identity_json_bytes(self)
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ScopeRelationProof:
    left_scope_id: str
    right_scope_id: str
    relation: ScopeRelationKind
    proof_policy_version: str
    contract_proof_refs: tuple[str, ...]
    input_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "left_scope_id",
            "right_scope_id",
            "proof_policy_version",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if not isinstance(self.relation, ScopeRelationKind):
            raise TypeError("relation must be ScopeRelationKind")
        if not isinstance(self.contract_proof_refs, tuple):
            raise TypeError("contract_proof_refs must be a tuple")
        if len(self.contract_proof_refs) != len(
            set(self.contract_proof_refs)
        ):
            raise ValueError("contract proof refs must be unique")
        for reference in self.contract_proof_refs:
            require_nonempty(reference, "contract_proof_refs")
        require_sha256(self.input_sha256, "input_sha256")


def semantic_measurement_preimage(
    design: MeasurementDesign,
    estimand_id: str,
) -> IdentityPreimage:
    """Build a rename-stable, estimand-local measurement graph preimage."""

    require_nonempty(estimand_id, "estimand_id")
    estimand = next(
        (
            candidate
            for candidate in design.estimands
            if candidate.estimand_id == estimand_id
        ),
        None,
    )
    if estimand is None:
        raise ValueError("unknown estimand")
    nodes = _indexed_measurement_nodes(design)
    reachable = _reachable_nodes(estimand, nodes)
    aliases = _refined_node_aliases(reachable)
    normalized_nodes = [
        {
            "node_type": type(node).__name__,
            "material": _normalize_node(node, aliases),
        }
        for node in reachable.values()
    ]
    normalized_nodes.sort(
        key=lambda item: canonical_identity_json_bytes(item)
    )
    return IdentityPreimage(
        identity_algorithm_version=IDENTITY_ALGORITHM_VERSION,
        identity_kind=IdentityKind.SEMANTIC_MEASUREMENT,
        material_payload={
            "estimand_alias": aliases[estimand.estimand_id],
            "nodes": normalized_nodes,
        },
    )


def semantic_measurement_id(
    design: MeasurementDesign,
    estimand_id: str,
) -> str:
    return semantic_measurement_preimage(
        design,
        estimand_id,
    ).identity_sha256


def authority_binding_preimage(
    *,
    question: QuestionRevision,
    frame_revision_id: str,
    frame_content_sha256: str,
    estimand_id: str,
    semantic_measurement_id_value: str,
    grounding_content_sha256: str,
    decision_record_ids: tuple[str, ...],
) -> IdentityPreimage:
    require_nonempty(frame_revision_id, "frame_revision_id")
    require_nonempty(estimand_id, "estimand_id")
    require_sha256(frame_content_sha256, "frame_content_sha256")
    require_sha256(
        semantic_measurement_id_value,
        "semantic_measurement_id",
    )
    require_sha256(
        grounding_content_sha256,
        "grounding_content_sha256",
    )
    return IdentityPreimage(
        identity_algorithm_version=IDENTITY_ALGORITHM_VERSION,
        identity_kind=IdentityKind.AUTHORITY_BINDING,
        material_payload={
            "question_revision_id": question.question_revision_id,
            "question_content_sha256": question.content_sha256,
            "analysis_cycle_id": question.analysis_cycle_id,
            "frame_revision_id": frame_revision_id,
            "frame_content_sha256": frame_content_sha256,
            "estimand_id": estimand_id,
            "semantic_measurement_id": semantic_measurement_id_value,
            "grounding_content_sha256": grounding_content_sha256,
            "decision_record_ids": sorted(decision_record_ids),
        },
    )


def authority_binding_id(**values: Any) -> str:
    return authority_binding_preimage(**values).identity_sha256


def build_analysis_frame_revision(
    *,
    question: QuestionRevision,
    frame_revision_id: str,
    case_id: str,
    revision_number: int,
    prior_frame_revision_id: str | None,
    created_by_action_id: str,
    created_at: datetime,
    revision_reason_ref: str,
    measurement_design: MeasurementDesign,
) -> AnalysisFrameRevision:
    material_payload = {
        "frame_revision_id": frame_revision_id,
        "case_id": case_id,
        "question_revision_id": question.question_revision_id,
        "revision_number": revision_number,
        "prior_frame_revision_id": prior_frame_revision_id,
        "created_by_action_id": created_by_action_id,
        "created_at": created_at,
        "revision_reason_ref": revision_reason_ref,
        "measurement_design": measurement_design,
        "identity_algorithm_version": IDENTITY_ALGORITHM_VERSION,
        "schema_epoch": 3,
    }
    provisional_frame_hash = hashlib.sha256(
        canonical_identity_json_bytes(material_payload)
    ).hexdigest()
    semantic_ids = tuple(
        semantic_measurement_id(
            measurement_design,
            estimand.estimand_id,
        )
        for estimand in measurement_design.estimands
    )
    grounding_hash = hashlib.sha256(
        canonical_identity_json_bytes(
            measurement_design.question_grounding
        )
    ).hexdigest()
    binding_ids = tuple(
        authority_binding_id(
            question=question,
            frame_revision_id=frame_revision_id,
            frame_content_sha256=provisional_frame_hash,
            estimand_id=estimand.estimand_id,
            semantic_measurement_id_value=semantic_id,
            grounding_content_sha256=grounding_hash,
            decision_record_ids=(
                measurement_design.question_grounding.decision_record_ids
            ),
        )
        for estimand, semantic_id in zip(
            measurement_design.estimands,
            semantic_ids,
            strict=True,
        )
    )
    return AnalysisFrameRevision(
        frame_revision_id=frame_revision_id,
        case_id=case_id,
        question_revision_id=question.question_revision_id,
        revision_number=revision_number,
        prior_frame_revision_id=prior_frame_revision_id,
        created_by_action_id=created_by_action_id,
        created_at=created_at,
        revision_reason_ref=revision_reason_ref,
        measurement_design=measurement_design,
        semantic_measurement_ids=semantic_ids,
        authority_binding_ids=binding_ids,
    )


def validate_frame_identities(
    question: QuestionRevision,
    frame: AnalysisFrameRevision,
) -> None:
    expected = build_analysis_frame_revision(
        question=question,
        frame_revision_id=frame.frame_revision_id,
        case_id=frame.case_id,
        revision_number=frame.revision_number,
        prior_frame_revision_id=frame.prior_frame_revision_id,
        created_by_action_id=frame.created_by_action_id,
        created_at=frame.created_at,
        revision_reason_ref=frame.revision_reason_ref,
        measurement_design=frame.measurement_design,
    )
    if (
        frame.semantic_measurement_ids
        != expected.semantic_measurement_ids
        or frame.authority_binding_ids
        != expected.authority_binding_ids
    ):
        raise ValueError("analysis frame identities are stale or forged")


def resolution_preimage(
    instance: ResolvedMeasurementInstance,
) -> IdentityPreimage:
    """Build the identity for one concrete calendar/data resolution."""

    return IdentityPreimage(
        identity_algorithm_version=IDENTITY_ALGORITHM_VERSION,
        identity_kind=IdentityKind.RESOLUTION,
        material_payload=_resolved_instance_material(instance),
    )


def compute_resolution_id(
    instance: ResolvedMeasurementInstance,
) -> str:
    return resolution_preimage(instance).identity_sha256


def resolution_outcome_preimage(
    outcome: MeasurementResolutionOutcome,
) -> IdentityPreimage:
    """Build an outcome identity without trusting supplied derived IDs."""

    material: dict[str, object] = {
        "semantic_measurement_id": outcome.semantic_measurement_id,
        "authority_binding_id": outcome.authority_binding_id,
        "frame_revision_id": outcome.frame_revision_id,
        "estimand_id": outcome.estimand_id,
        "kind": outcome.kind,
        "resolved_instance": None,
        "boundary": None,
        "requirement_boundaries": outcome.requirement_boundaries,
    }
    if outcome.kind is ResolutionOutcomeKind.RESOLVED_INSTANCE:
        instance = outcome.resolved_instance
        if instance is None:
            raise ValueError("resolved outcome is missing its instance")
        material["resolved_instance"] = _resolved_instance_material(instance)
    else:
        boundary = outcome.boundary
        if boundary is None:
            raise ValueError("boundary outcome is missing its boundary")
        material["boundary"] = _resolution_boundary_material(boundary)
    return IdentityPreimage(
        identity_algorithm_version=IDENTITY_ALGORITHM_VERSION,
        identity_kind=IdentityKind.RESOLUTION_OUTCOME,
        material_payload=material,
    )


def compute_resolution_outcome_id(
    outcome: MeasurementResolutionOutcome,
) -> str:
    return resolution_outcome_preimage(outcome).identity_sha256


def compute_typed_boundary_derivation_proof(
    outcome: MeasurementResolutionOutcome,
) -> str:
    """Recompute the resolver-owned proof for a whole-estimand boundary."""

    boundary = outcome.boundary
    if (
        outcome.kind
        is not ResolutionOutcomeKind.TYPED_RESOLUTION_BOUNDARY
        or boundary is None
    ):
        raise ValueError("typed boundary proof requires a boundary outcome")
    return content_sha256(
        {
            "frame_revision_id": outcome.frame_revision_id,
            "estimand_id": outcome.estimand_id,
            "semantic_measurement_id": outcome.semantic_measurement_id,
            "authority_binding_id": outcome.authority_binding_id,
            "boundary_code": boundary.boundary_code,
            "boundary_policy_ref": boundary.boundary_policy_ref,
            "failed_requirement_ids": boundary.failed_requirement_ids,
            "failed_contract_refs": boundary.failed_contract_refs,
            "inspection_evidence_refs": (
                boundary.inspection_evidence_refs
            ),
            "allowed_claim_ceiling": (
                boundary.allowed_claim_ceiling.value
            ),
        }
    )


def compute_requirement_boundary_derivation_proof(
    outcome: MeasurementResolutionOutcome,
    requirement_boundary: RequirementResolutionBoundary,
) -> str:
    """Recompute the proof for one requirement-local typed boundary."""

    return content_sha256(
        {
            "frame_revision_id": outcome.frame_revision_id,
            "estimand_id": outcome.estimand_id,
            "evidence_requirement_id": (
                requirement_boundary.evidence_requirement_id
            ),
            "boundary_code": requirement_boundary.boundary_code,
            "boundary_policy_ref": (
                requirement_boundary.boundary_policy_ref
            ),
            "failed_contract_refs": (
                requirement_boundary.failed_contract_refs
            ),
            "inspection_evidence_refs": (
                requirement_boundary.inspection_evidence_refs
            ),
            "allowed_claim_ceiling": (
                requirement_boundary.allowed_claim_ceiling.value
            ),
        }
    )


def validate_resolution_identities(
    outcome: MeasurementResolutionOutcome,
) -> None:
    for requirement_boundary in outcome.requirement_boundaries:
        if (
            requirement_boundary.boundary_code
            not in TRUSTED_RESOLUTION_BOUNDARY_CODES
        ):
            raise ValueError(
                "requirement boundary code is outside the trusted registry"
            )
        if (
            requirement_boundary.derivation_proof_sha256
            != compute_requirement_boundary_derivation_proof(
                outcome,
                requirement_boundary,
            )
        ):
            raise ValueError(
                "requirement boundary derivation proof is stale or forged"
            )
    if outcome.kind is ResolutionOutcomeKind.TYPED_RESOLUTION_BOUNDARY:
        boundary = outcome.boundary
        if boundary is None:
            raise ValueError("boundary outcome is missing its boundary")
        if boundary.boundary_code not in TRUSTED_RESOLUTION_BOUNDARY_CODES:
            raise ValueError(
                "resolution boundary code is outside the trusted registry"
            )
        if (
            boundary.derivation_proof_sha256
            != compute_typed_boundary_derivation_proof(outcome)
        ):
            raise ValueError(
                "resolution boundary derivation proof is stale or forged"
            )
    if outcome.kind is ResolutionOutcomeKind.RESOLVED_INSTANCE:
        instance = outcome.resolved_instance
        if instance is None:
            raise ValueError("resolved outcome is missing its instance")
        if instance.resolution_id != compute_resolution_id(instance):
            raise ValueError("measurement resolution identity is stale or forged")
    if outcome.resolution_outcome_id != compute_resolution_outcome_id(
        outcome
    ):
        raise ValueError("measurement outcome identity is stale or forged")


def validate_resolution_against_frame(
    frame: AnalysisFrameRevision,
    outcome: MeasurementResolutionOutcome,
) -> None:
    """Reject canonically hashed resolution content that drifts from the Frame."""

    design = frame.measurement_design
    estimand = next(
        (
            candidate
            for candidate in design.estimands
            if candidate.estimand_id == outcome.estimand_id
        ),
        None,
    )
    if estimand is None:
        raise ValueError("resolution targets an unknown frame estimand")
    requirements = {
        item.evidence_requirement_id: item
        for item in design.evidence_requirements
        if item.evidence_requirement_id
        in estimand.evidence_requirement_ids
    }
    for requirement_boundary in outcome.requirement_boundaries:
        if (
            requirement_boundary.boundary_policy_ref
            not in TRUSTED_BOUNDARY_POLICY_REFS
        ):
            raise ValueError(
                "requirement boundary uses an untrusted policy"
            )
        requirement = requirements.get(
            requirement_boundary.evidence_requirement_id
        )
        if requirement is None:
            raise ValueError(
                "requirement boundary changes Frame requirements"
            )
        if (
            requirement.boundary_policy.value != "allow_typed_boundary"
            or requirement_boundary.boundary_code
            not in requirement.allowed_boundary_codes
        ) and (
            requirement_boundary.allowed_claim_ceiling
            is not ClaimStrengthCeiling.BOUNDARY_ONLY
        ):
            raise ValueError(
                "unapproved requirement boundary exceeds boundary-only ceiling"
            )
    if outcome.kind is ResolutionOutcomeKind.TYPED_RESOLUTION_BOUNDARY:
        boundary = outcome.boundary
        if boundary is None:
            raise ValueError("boundary outcome is missing its boundary")
        if (
            boundary.boundary_policy_ref
            not in TRUSTED_BOUNDARY_POLICY_REFS
        ):
            raise ValueError("resolution boundary uses an untrusted policy")
        unknown_requirements = set(boundary.failed_requirement_ids) - set(
            estimand.evidence_requirement_ids
        )
        if unknown_requirements:
            raise ValueError(
                "resolution boundary changes the Frame requirements"
            )
        if not boundary.failed_contract_refs:
            raise ValueError(
                "resolution boundary requires failed contract evidence"
            )
        if not boundary.inspection_evidence_refs:
            raise ValueError(
                "resolution boundary requires inspection evidence"
            )
        if _claim_strength_rank(boundary.allowed_claim_ceiling) > (
            _claim_strength_rank(estimand.claim_strength_ceiling)
        ):
            raise ValueError(
                "resolution boundary exceeds the Frame claim ceiling"
            )
        for requirement_id in boundary.failed_requirement_ids:
            requirement = requirements[requirement_id]
            if (
                requirement.boundary_policy.value
                != "allow_typed_boundary"
                or boundary.boundary_code
                not in requirement.allowed_boundary_codes
            ) and (
                boundary.allowed_claim_ceiling
                is not ClaimStrengthCeiling.BOUNDARY_ONLY
            ):
                raise ValueError(
                    "unapproved boundary exceeds boundary-only ceiling"
                )
        return

    instance = outcome.resolved_instance
    if instance is None:
        raise ValueError("resolved outcome is missing its instance")
    if (
        instance.resolver_contract_ref
        not in TRUSTED_RESOLVER_CONTRACT_REFS
    ):
        raise ValueError("resolution uses an untrusted resolver contract")
    scope = next(
        (
            candidate
            for candidate in design.scopes
            if candidate.scope_id == estimand.scope_ceiling_id
        ),
        None,
    )
    if scope is None:
        raise ValueError("estimand scope ceiling is unavailable")
    expected_refs = {
        "expected_scope_id": estimand.scope_ceiling_id,
        "expected_grain_ref": scope.grain_ref,
        "expected_unit_ref": scope.unit_ref,
        "expected_exposure_id": estimand.exposure_id,
        "eligibility_id": estimand.eligibility_id,
    }
    for field_name, expected in expected_refs.items():
        if getattr(instance, field_name) != expected:
            raise ValueError(
                f"resolution changes the Frame {field_name}"
            )
    rules = {
        candidate.window_rule_id: candidate
        for candidate in design.window_rules
    }
    actual_rule_ids = tuple(
        window.window_rule_id for window in instance.windows
    )
    if len(actual_rule_ids) != len(set(actual_rule_ids)):
        raise ValueError("resolution repeats a Frame window rule")
    if set(actual_rule_ids) != set(scope.time_window_rule_ids):
        raise ValueError("resolution changes the Frame window identity")
    for window in instance.windows:
        rule = rules.get(window.window_rule_id)
        if rule is None:
            raise ValueError("resolution uses an unknown Frame window rule")
        if window.period_offset != rule.period_offset:
            raise ValueError("resolution changes the Frame window offset")
        _validate_resolved_calendar_window(rule, window)
        _validate_resolved_instants(instance.context, window)
    exposure = next(
        (
            candidate
            for candidate in design.exposures
            if candidate.exposure_id == estimand.exposure_id
        ),
        None,
    )
    eligibility = next(
        (
            candidate
            for candidate in design.eligibilities
            if candidate.eligibility_id == estimand.eligibility_id
        ),
        None,
    )
    expected_proof_sha256 = hashlib.sha256(
        canonical_identity_json_bytes(
            {
                "frame_revision_id": frame.frame_revision_id,
                "estimand_id": estimand.estimand_id,
                "semantic_measurement_id": (
                    instance.semantic_measurement_id
                ),
                "authority_binding_id": instance.authority_binding_id,
                "context": instance.context,
                "target_period_ref": instance.target_period_ref,
                "resolver_contract_ref": (
                    instance.resolver_contract_ref
                ),
                "resolver_input_bundle_sha256": (
                    instance.resolver_input_bundle_sha256
                ),
                "windows": instance.windows,
                "scope": scope,
                "exposure": exposure,
                "eligibility": eligibility,
            }
        )
    ).hexdigest()
    if (
        instance.field_derivation_proof_sha256
        != expected_proof_sha256
    ):
        raise ValueError(
            "resolution derivation proof does not match resolved fields"
        )
    if estimand.contrast_id is not None:
        contrast = next(
            (
                candidate
                for candidate in design.contrasts
                if candidate.contrast_id == estimand.contrast_id
            ),
            None,
        )
        if contrast is None:
            raise ValueError("estimand contrast is unavailable")
        expected_operands = {
            operand.operand_id: operand.window_rule_id
            for operand in contrast.operands
        }
        actual_operands = {
            window.operand_id: window.window_rule_id
            for window in instance.windows
        }
        if actual_operands != expected_operands:
            raise ValueError(
                "resolution changes the Frame contrast operands"
            )


def scope_relation(
    left: ScopeExpression,
    right: ScopeExpression,
    *,
    proof_policy_version: str,
    relation_contracts: Mapping[
        tuple[str, str],
        tuple[ScopeRelationKind, str],
    ]
    | None = None,
) -> ScopeRelationProof:
    require_nonempty(proof_policy_version, "proof_policy_version")
    contracts = relation_contracts or {}
    allowed_proven_relations = {
        ScopeRelationKind.SUBSET,
        ScopeRelationKind.SUPERSET,
        ScopeRelationKind.LAWFUL_PROJECTION,
        ScopeRelationKind.LAWFUL_AGGREGATION,
        ScopeRelationKind.DISJOINT,
    }
    normalized_contracts = []
    for (left_id, right_id), (relation, contract_ref) in (
        contracts.items()
    ):
        if relation not in allowed_proven_relations:
            raise ValueError("scope relation contract has invalid relation")
        require_nonempty(left_id, "relation contract left scope")
        require_nonempty(right_id, "relation contract right scope")
        require_nonempty(contract_ref, "relation contract ref")
        normalized_contracts.append(
            (left_id, right_id, relation.value, contract_ref)
        )
    input_payload = {
        "left": left,
        "right": right,
        "proof_policy_version": proof_policy_version,
        "relation_contracts": sorted(normalized_contracts),
    }
    input_sha256 = hashlib.sha256(
        canonical_identity_json_bytes(input_payload)
    ).hexdigest()
    contract_proof_refs: tuple[str, ...] = ()
    if _scope_material(left) == _scope_material(right):
        relation = ScopeRelationKind.EXACT
    else:
        proof = contracts.get((left.scope_id, right.scope_id))
        if proof is None:
            relation = ScopeRelationKind.UNKNOWN
        else:
            relation, contract_ref = proof
            contract_proof_refs = (contract_ref,)
    return ScopeRelationProof(
        left_scope_id=left.scope_id,
        right_scope_id=right.scope_id,
        relation=relation,
        proof_policy_version=proof_policy_version,
        contract_proof_refs=contract_proof_refs,
        input_sha256=input_sha256,
    )


def _validate_resolved_calendar_window(rule, window) -> None:
    if rule.rule_kind is WindowRuleKind.ABSOLUTE_INTERVAL:
        expected_start = rule.absolute_start
        expected_end = rule.absolute_end
    elif rule.rule_kind is WindowRuleKind.RELATIVE_CALENDAR:
        expected_start, expected_end = _identity_shifted_period(
            window.anchor_date,
            rule.calendar_unit.value,
            rule.period_offset,
        )
    elif rule.rule_kind is WindowRuleKind.ROLLING_INTERVAL:
        if (
            rule.selection_kind is not WindowSelectionKind.ROLLING_LENGTH
            or rule.selection_count is None
        ):
            raise ValueError("rolling Frame window rule is invalid")
        rolling_end = _identity_shift_anchor(
            window.anchor_date,
            rule.calendar_unit.value,
            rule.period_offset,
        )
        expected_start = rolling_end - timedelta(
            days=rule.selection_count - 1
        )
        expected_end = rolling_end
    else:
        expected_start, expected_end = _identity_shifted_period(
            window.anchor_date,
            rule.calendar_unit.value,
            rule.period_offset,
        )
        expected_selected_count = rule.selection_count
        if expected_selected_count is not None:
            if rule.start_boundary.value == "exclusive":
                expected_selected_count -= 1
            if rule.end_boundary.value == "exclusive":
                expected_selected_count -= 1
        if (
            rule.selection_count is None
            or window.selected_calendar_dates_count
            != expected_selected_count
            or window.actual_start < expected_start
            or window.actual_end > expected_end
        ):
            raise ValueError(
                "business-calendar resolution violates Frame bounds"
            )
        # Exact membership remains bound by the versioned calendar receipt
        # and resolver input bundle carried by the instance.
        return
    if expected_start is None or expected_end is None:
        raise ValueError("Frame window has no resolvable interval")
    selection = rule.selection_kind
    if selection is WindowSelectionKind.FIRST_N_CALENDAR_DAYS:
        assert rule.selection_count is not None
        expected_end = expected_start + timedelta(
            days=rule.selection_count - 1
        )
    elif selection is WindowSelectionKind.LAST_N_CALENDAR_DAYS:
        assert rule.selection_count is not None
        expected_start = expected_end - timedelta(
            days=rule.selection_count - 1
        )
    elif selection is WindowSelectionKind.ORDINAL_RANGE:
        assert rule.ordinal_start is not None
        assert rule.ordinal_end is not None
        period_start = expected_start
        expected_start = period_start + timedelta(
            days=rule.ordinal_start - 1
        )
        expected_end = period_start + timedelta(
            days=rule.ordinal_end - 1
        )
    if rule.start_boundary.value == "exclusive":
        expected_start += timedelta(days=1)
    if rule.end_boundary.value == "exclusive":
        expected_end -= timedelta(days=1)
    if (
        window.actual_start != expected_start
        or window.actual_end != expected_end
    ):
        raise ValueError(
            "resolution dates do not derive from the Frame window rule"
        )
    expected_dates = tuple(
        expected_start + timedelta(days=offset)
        for offset in range((expected_end - expected_start).days + 1)
    )
    if (
        window.selected_calendar_dates_count != len(expected_dates)
        or window.selected_calendar_dates_sha256
        != content_sha256(expected_dates)
    ):
        raise ValueError(
            "resolution date-set proof does not match the Frame window rule"
        )


def _validate_resolved_instants(context, window) -> None:
    expected_start = _identity_local_instant(
        local_date=window.actual_start,
        context=context,
    )
    expected_end = _identity_local_instant(
        local_date=window.actual_end + timedelta(days=1),
        context=context,
    )
    if expected_start is None or expected_end is None:
        raise ValueError("resolution instant cannot be derived from context")
    if (
        window.start_instant != expected_start
        or window.end_instant != expected_end
    ):
        raise ValueError(
            "resolution instants do not match timezone and business cutoff"
        )
    if window.end_instant > context.as_of_instant:
        raise ValueError("resolution extends beyond the as-of instant")


def _identity_local_instant(*, local_date: date, context) -> datetime | None:
    zone = ZoneInfo(context.timezone)
    cutoff = time.fromisoformat(context.business_day_cutoff)
    naive = datetime.combine(local_date, cutoff)
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            candidates.append(candidate)
    unique = {
        candidate.astimezone(UTC): candidate for candidate in candidates
    }
    if not unique:
        return None
    ordered = tuple(unique[key] for key in sorted(unique))
    if len(ordered) == 1:
        return ordered[0]
    if (
        context.ambiguous_local_time_policy
        is AmbiguousLocalTimePolicy.REJECT
    ):
        return None
    if (
        context.ambiguous_local_time_policy
        is AmbiguousLocalTimePolicy.EARLIEST_FOLD
    ):
        return ordered[0]
    return ordered[-1]


def _identity_shifted_period(
    anchor: date,
    unit: str,
    offset: int,
) -> tuple[date, date]:
    if unit == "day":
        shifted = anchor + timedelta(days=offset)
        return shifted, shifted
    if unit == "week":
        start = anchor - timedelta(days=anchor.weekday())
        start += timedelta(weeks=offset)
        return start, start + timedelta(days=6)
    if unit == "month":
        year, month = _identity_shift_month(
            anchor.year,
            anchor.month,
            offset,
        )
        return (
            date(year, month, 1),
            date(year, month, calendar.monthrange(year, month)[1]),
        )
    if unit == "quarter":
        quarter = (anchor.month - 1) // 3
        absolute = anchor.year * 4 + quarter + offset
        year, shifted_quarter = divmod(absolute, 4)
        month = shifted_quarter * 3 + 1
        end_year, end_month = _identity_shift_month(year, month, 2)
        return (
            date(year, month, 1),
            date(
                end_year,
                end_month,
                calendar.monthrange(end_year, end_month)[1],
            ),
        )
    if unit == "year":
        year = anchor.year + offset
        return date(year, 1, 1), date(year, 12, 31)
    raise ValueError("Frame calendar unit requires a versioned resolver")


def _identity_shift_anchor(
    anchor: date,
    unit: str,
    offset: int,
) -> date:
    if unit == "day":
        return anchor + timedelta(days=offset)
    if unit == "week":
        return anchor + timedelta(weeks=offset)
    if unit == "month":
        year, month = _identity_shift_month(
            anchor.year,
            anchor.month,
            offset,
        )
        return date(
            year,
            month,
            min(anchor.day, calendar.monthrange(year, month)[1]),
        )
    if unit == "quarter":
        year, month = _identity_shift_month(
            anchor.year,
            anchor.month,
            offset * 3,
        )
        return date(
            year,
            month,
            min(anchor.day, calendar.monthrange(year, month)[1]),
        )
    if unit == "year":
        year = anchor.year + offset
        return date(
            year,
            anchor.month,
            min(
                anchor.day,
                calendar.monthrange(year, anchor.month)[1],
            ),
        )
    raise ValueError("rolling calendar unit is unsupported")


def _identity_shift_month(
    year: int,
    month: int,
    offset: int,
) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + offset
    shifted_year, shifted_month = divmod(absolute, 12)
    return shifted_year, shifted_month + 1


def canonical_identity_json_bytes(value: Any) -> bytes:
    """Canonical identity codec: NFC UTF-8, no float, explicit stable forms."""

    normalized = _identity_jsonable(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_decimal_string(value: Decimal) -> str:
    """Return the finite, exponent-free decimal form shared with TypeScript."""

    if not value.is_finite():
        raise ValueError("identity decimal must be finite")
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _identity_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            unicodedata.normalize("NFC", field.name): _identity_jsonable(
                getattr(value, field.name)
            )
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return _identity_jsonable(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("identity timestamp must be timezone-aware")
        normalized = value.astimezone(UTC)
        return normalized.isoformat(timespec="microseconds").replace(
            "+00:00",
            "Z",
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return canonical_decimal_string(value)
    if isinstance(value, float):
        raise TypeError("binary float is forbidden in identity preimages")
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = unicodedata.normalize("NFC", str(key))
            if normalized_key in normalized:
                raise ValueError(
                    "identity object keys collide after normalization"
                )
            normalized[normalized_key] = _identity_jsonable(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_identity_jsonable(item) for item in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise ValueError("identity integer exceeds cross-language safe range")
        return value
    raise TypeError(
        "unsupported identity value {}".format(type(value).__name__)
    )


def _indexed_measurement_nodes(
    design: MeasurementDesign,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for field in fields(design):
        value = getattr(design, field.name)
        if not isinstance(value, tuple):
            continue
        for node in value:
            node_id = _node_identifier(node)
            result[node_id] = node
            operands = getattr(node, "operands", ())
            for operand in operands:
                result[_node_identifier(operand)] = operand
    return result


def _node_identifier(node: object) -> str:
    candidates = [
        field.name
        for field in fields(node)
        if field.name.endswith("_id")
        and not field.name.endswith("_revision_id")
    ]
    if not candidates:
        raise TypeError("measurement node has no identity field")
    return str(getattr(node, candidates[0]))


def _reachable_nodes(
    estimand: EstimandSpec,
    all_nodes: Mapping[str, object],
) -> dict[str, object]:
    reachable: dict[str, object] = {}
    pending = [estimand.estimand_id]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        node = all_nodes.get(node_id)
        if node is None:
            raise ValueError("measurement graph contains an unknown reference")
        reachable[node_id] = node
        for reference in _node_references(node, all_nodes):
            if reference not in reachable:
                pending.append(reference)
    return reachable


def _node_references(
    node: object,
    all_nodes: Mapping[str, object],
) -> tuple[str, ...]:
    references: list[str] = []
    for field in fields(node):
        value = getattr(node, field.name)
        if isinstance(value, str) and value in all_nodes:
            references.append(value)
        elif isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            references.extend(
                item
                for item in value
                if isinstance(item, str) and item in all_nodes
            )
            for item in value:
                if is_dataclass(item):
                    references.extend(
                        _node_references(item, all_nodes)
                    )
    return tuple(references)


def _refined_node_aliases(
    nodes: Mapping[str, object],
) -> dict[str, str]:
    aliases = {
        node_id: hashlib.sha256(
            canonical_identity_json_bytes(
                _node_refinement_payload(
                    node,
                    node_id=node_id,
                    aliases=None,
                    known_ids=set(nodes),
                )
            )
        ).hexdigest()
        for node_id, node in nodes.items()
    }
    for _ in range(len(nodes) + 1):
        aliases = {
            node_id: hashlib.sha256(
                canonical_identity_json_bytes(
                    _node_refinement_payload(
                        node,
                        node_id=node_id,
                        aliases=aliases,
                        known_ids=set(nodes),
                    )
                )
            ).hexdigest()
            for node_id, node in nodes.items()
        }
    if len(set(aliases.values())) != len(aliases):
        raise ValueError(
            "measurement graph contains indistinguishable duplicate nodes"
        )
    return {
        node_id: "{}:{}".format(type(nodes[node_id]).__name__, digest)
        for node_id, digest in aliases.items()
    }


def _node_refinement_payload(
    node: object,
    *,
    node_id: str,
    aliases: Mapping[str, str] | None,
    known_ids: set[str],
) -> object:
    def normalize(value: Any, *, field_name: str | None = None) -> Any:
        if isinstance(value, str) and value in known_ids:
            if value == node_id:
                return {"$self": type(node).__name__}
            return {
                "$ref": (
                    aliases[value]
                    if aliases is not None
                    else type_placeholder(value)
                )
            }
        if is_dataclass(value):
            return {
                field.name: normalize(
                    getattr(value, field.name),
                    field_name=field.name,
                )
                for field in fields(value)
                if getattr(value, field.name) != node_id
            }
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            normalized = [normalize(item) for item in value]
            if field_name != "ordered_event_ids":
                normalized.sort(key=canonical_identity_json_bytes)
            return normalized
        return value

    def type_placeholder(reference: str) -> str:
        return "node:{}".format(reference in known_ids)

    return {
        "node_type": type(node).__name__,
        "material": normalize(node),
    }


def _normalize_node(
    node: object,
    aliases: Mapping[str, str],
) -> object:
    node_id = _node_identifier(node)

    def normalize(value: Any, *, field_name: str | None = None) -> Any:
        if isinstance(value, str) and value in aliases:
            return {"$ref": aliases[value]}
        if is_dataclass(value):
            result = {}
            for field in fields(value):
                member = getattr(value, field.name)
                if member == node_id:
                    continue
                result[field.name] = normalize(
                    member,
                    field_name=field.name,
                )
            return result
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            normalized = [normalize(item) for item in value]
            if field_name != "ordered_event_ids":
                normalized.sort(key=canonical_identity_json_bytes)
            return normalized
        return value

    return normalize(node)


def _resolved_instance_material(
    instance: ResolvedMeasurementInstance,
) -> Mapping[str, object]:
    windows = [
        {
            field.name: getattr(window, field.name)
            for field in fields(window)
        }
        for window in instance.windows
    ]
    windows.sort(key=canonical_identity_json_bytes)
    return {
        "semantic_measurement_id": instance.semantic_measurement_id,
        "authority_binding_id": instance.authority_binding_id,
        "frame_revision_id": instance.frame_revision_id,
        "estimand_id": instance.estimand_id,
        "context": instance.context,
        "target_period_ref": instance.target_period_ref,
        "windows": windows,
        "expected_scope_id": instance.expected_scope_id,
        "expected_grain_ref": instance.expected_grain_ref,
        "expected_unit_ref": instance.expected_unit_ref,
        "expected_exposure_id": instance.expected_exposure_id,
        "eligibility_id": instance.eligibility_id,
    }


def _resolution_boundary_material(
    boundary: TypedResolutionBoundary,
) -> Mapping[str, object]:
    return {
        "boundary_code": boundary.boundary_code,
        "boundary_policy_ref": boundary.boundary_policy_ref,
        "failed_requirement_ids": sorted(
            boundary.failed_requirement_ids
        ),
        "failed_contract_refs": sorted(boundary.failed_contract_refs),
        "inspection_evidence_refs": sorted(
            boundary.inspection_evidence_refs
        ),
        "allowed_claim_ceiling": boundary.allowed_claim_ceiling,
    }


def _claim_strength_rank(value: ClaimStrengthCeiling) -> int:
    return {
        ClaimStrengthCeiling.BOUNDARY_ONLY: 0,
        ClaimStrengthCeiling.DESCRIPTIVE: 1,
        ClaimStrengthCeiling.ACCOUNTING: 2,
        ClaimStrengthCeiling.ASSOCIATIONAL: 3,
        ClaimStrengthCeiling.CAUSAL: 4,
    }[value]


def _scope_material(scope: ScopeExpression) -> Mapping[str, object]:
    return {
        field.name: getattr(scope, field.name)
        for field in fields(scope)
        if field.name != "scope_id"
    }
