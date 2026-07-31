"""Deterministic measurement validation, calendar resolution, and algebra.

The Primary Agent owns the open business interpretation expressed by an
``AnalysisFrameRevision``.  This module only resolves that accepted design
against explicit calendar and data-coverage facts.  It never invents a window,
changes an offset, or silently substitutes an exposure basis.
"""

from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import (
    content_sha256,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
    to_jsonable,
)
from .identity import (
    canonical_decimal_string,
    canonical_identity_json_bytes,
    compute_resolution_id,
    compute_resolution_outcome_id,
    validate_resolution_against_frame,
    validate_resolution_identities,
)
from .measurement import (
    AggregationOrder,
    AccountingDecompositionTargetSpec,
    AmbiguousLocalTimePolicy,
    AnalysisFrameRevision,
    AssociationTargetSpec,
    CalendarUnit,
    ClaimStrengthCeiling,
    ClaimTargetKind,
    CompletenessPolicy,
    CompositionTargetSpec,
    ContrastSpec,
    ContrastTargetSpec,
    CausalEffectTargetSpec,
    CohortOutcomeTargetSpec,
    DiagnosticSetTargetSpec,
    EvidenceRequirementSpec,
    EstimandSpec,
    EstimatorFamily,
    ExposureBasis,
    ExposureFactSourceKind,
    ExposureNormalization,
    ExposureSpec,
    FunnelTransitionTargetSpec,
    MeasurementDesign,
    MeasurementDerivationAuthority,
    MeasurementResolutionOutcome,
    MissingExposurePolicy,
    ObligationExecutionDisposition,
    RequirementBoundaryPolicy,
    RequirementResolutionBoundary,
    ResolvedEvidenceObligation,
    ResolvedExposureFact,
    ResolvedMeasurementInstance,
    ResolvedWindow,
    ResolutionContext,
    ResolutionOutcomeKind,
    TypedResolutionBoundary,
    WindowRuleKind,
    WindowRuleSpec,
    WindowSelectionKind,
)

RESOLVER_CONTRACT_REF = "waje-vnext://measurement-resolver/gregorian.v1"
BOUNDARY_POLICY_REGISTRY_REF = (
    "waje-vnext://measurement-boundary-policy/registry.v1"
)


class ResolutionBoundaryCode(StrEnum):
    INVALID_MEASUREMENT_GRAPH = "invalid_measurement_graph"
    MISSING_ANCHOR = "missing_anchor"
    UNSUPPORTED_CALENDAR = "unsupported_calendar"
    MISSING_CALENDAR_CONTRACT = "missing_calendar_contract"
    INVALID_WINDOW_RULE = "invalid_window_rule"
    SNAPSHOT_OUT_OF_RANGE = "snapshot_out_of_range"
    INCOMPLETE_PERIOD = "incomplete_period"
    INSUFFICIENT_VALID_EXPOSURE = "insufficient_valid_exposure"
    INCOMPATIBLE_UNIT = "incompatible_unit"
    INCOMPARABLE_EXPOSURE = "incomparable_exposure"


@dataclass(frozen=True, slots=True)
class BoundaryPolicyRule:
    code: ResolutionBoundaryCode
    maximum_claim_ceiling: ClaimStrengthCeiling
    required_proof_kinds: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.code, ResolutionBoundaryCode):
            raise TypeError("code must be ResolutionBoundaryCode")
        if not isinstance(
            self.maximum_claim_ceiling,
            ClaimStrengthCeiling,
        ):
            raise TypeError(
                "maximum_claim_ceiling must be ClaimStrengthCeiling"
            )
        if not isinstance(self.required_proof_kinds, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.required_proof_kinds
        ):
            raise ValueError(
                "required_proof_kinds must contain non-empty strings"
            )
        if len(self.required_proof_kinds) != len(
            set(self.required_proof_kinds)
        ):
            raise ValueError("required_proof_kinds must be unique")
        if not self.required_proof_kinds:
            raise ValueError("boundary policy requires proof kinds")


BOUNDARY_POLICY_REGISTRY: Mapping[
    ResolutionBoundaryCode,
    BoundaryPolicyRule,
] = {
    code: BoundaryPolicyRule(
        code=code,
        maximum_claim_ceiling=ClaimStrengthCeiling.BOUNDARY_ONLY,
        required_proof_kinds=(
            "failed_contract_ref",
            "inspection_evidence_ref",
        ),
    )
    for code in ResolutionBoundaryCode
    if code is not ResolutionBoundaryCode.INVALID_MEASUREMENT_GRAPH
}


@dataclass(frozen=True, slots=True)
class MeasurementValidationFinding:
    estimand_id: str
    code: str
    node_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.estimand_id, "estimand_id")
        require_nonempty(self.code, "code")
        _require_string_tuple(self.node_refs, "node_refs")


@dataclass(frozen=True, slots=True)
class ClaimTargetValidationContract:
    claim_target_kind: ClaimTargetKind
    required_estimand_fields: tuple[str, ...]
    allowed_estimator_families: tuple[EstimatorFamily, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.claim_target_kind, ClaimTargetKind):
            raise TypeError("claim_target_kind must be ClaimTargetKind")
        if not isinstance(self.required_estimand_fields, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.required_estimand_fields
        ):
            raise ValueError(
                "required_estimand_fields must contain strings"
            )
        if len(self.required_estimand_fields) != len(
            set(self.required_estimand_fields)
        ):
            raise ValueError("required_estimand_fields must be unique")
        if not isinstance(self.allowed_estimator_families, tuple) or any(
            not isinstance(item, EstimatorFamily)
            for item in self.allowed_estimator_families
        ):
            raise TypeError(
                "allowed_estimator_families must contain EstimatorFamily"
            )
        if not self.allowed_estimator_families:
            raise ValueError(
                "claim target requires at least one estimator family"
            )


def claim_target_validation_contracts(
) -> tuple[ClaimTargetValidationContract, ...]:
    """Expose the complete deterministic validation routing table."""

    return tuple(
        ClaimTargetValidationContract(
            claim_target_kind=kind,
            required_estimand_fields=_required_fields(kind),
            allowed_estimator_families=tuple(
                sorted(
                    _allowed_estimator_families(kind),
                    key=lambda item: item.value,
                )
            ),
        )
        for kind in ClaimTargetKind
    )


@dataclass(frozen=True, slots=True)
class CalendarCoverageReceipt:
    """Trusted snapshot-catalog facts for one Frame-owned calendar window.

    This receipt describes release coverage and date availability only. Metric
    or entity exposure belongs to a separate ``ExposureCoverageFact``.
    """

    window_rule_id: str
    released_start: date
    released_end: date
    released_at_instant: datetime
    coverage_complete_through: date
    late_arrival_cutoff_instant: datetime
    observed_dates: tuple[date, ...]
    valid_dates: tuple[date, ...]
    snapshot_release_ref: str
    coverage_watermark_ref: str
    source_receipt_sha256: str
    inspection_evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.window_rule_id, "window_rule_id")
        if self.released_end < self.released_start:
            raise ValueError("released coverage end precedes start")
        require_aware_datetime(
            self.released_at_instant,
            "released_at_instant",
        )
        require_aware_datetime(
            self.late_arrival_cutoff_instant,
            "late_arrival_cutoff_instant",
        )
        if not (
            self.released_start
            <= self.coverage_complete_through
            <= self.released_end
        ):
            raise ValueError(
                "coverage completeness watermark must fit release range"
            )
        if (
            self.late_arrival_cutoff_instant
            < self.released_at_instant
        ):
            raise ValueError(
                "late-arrival cutoff cannot precede release"
            )
        for field_name in (
            "observed_dates",
            "valid_dates",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple):
                raise TypeError(f"{field_name} must be a tuple")
            if tuple(sorted(values)) != values:
                raise ValueError(f"{field_name} must be sorted")
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
        if not set(self.valid_dates) <= set(self.observed_dates):
            raise ValueError("valid dates must be observed")
        if any(
            value < self.released_start or value > self.released_end
            for value in self.observed_dates
        ):
            raise ValueError(
                "observed dates must fit released coverage"
            )
        for field_name in (
            "snapshot_release_ref",
            "coverage_watermark_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        require_sha256(
            self.source_receipt_sha256,
            "source_receipt_sha256",
        )
        _require_string_tuple(
            self.inspection_evidence_refs,
            "inspection_evidence_refs",
        )
        if not self.inspection_evidence_refs:
            raise ValueError("calendar coverage requires inspection evidence")


@dataclass(frozen=True, slots=True)
class ExposureCoverageFact:
    window_rule_id: str
    exposure_id: str
    basis: ExposureBasis
    unit_ref: str
    expected_exposure_decimal: str
    observed_exposure_decimal: str
    valid_exposure_decimal: str
    invalid_exposure_decimal: str
    missing_exposure_decimal: str
    at_risk_exposure_decimal: str | None
    source_kind: ExposureFactSourceKind
    source_receipt_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("window_rule_id", "exposure_id", "unit_ref"):
            require_nonempty(getattr(self, field_name), field_name)
        if self.source_kind not in {
            ExposureFactSourceKind.CONTRACT_CATALOG,
            ExposureFactSourceKind.SNAPSHOT_CATALOG,
            ExposureFactSourceKind.CALENDAR_DERIVATION,
        }:
            raise ValueError("exposure fact source is not resolver-trusted")
        require_sha256(
            self.source_receipt_sha256,
            "source_receipt_sha256",
        )
        ResolvedExposureFact(
            exposure_id=self.exposure_id,
            basis=self.basis,
            unit_ref=self.unit_ref,
            expected_exposure_decimal=self.expected_exposure_decimal,
            observed_exposure_decimal=self.observed_exposure_decimal,
            valid_exposure_decimal=self.valid_exposure_decimal,
            invalid_exposure_decimal=self.invalid_exposure_decimal,
            missing_exposure_decimal=self.missing_exposure_decimal,
            coverage_ratio_decimal=_coverage_ratio(
                self.valid_exposure_decimal,
                self.expected_exposure_decimal,
            ),
            at_risk_exposure_decimal=self.at_risk_exposure_decimal,
            source_kind=self.source_kind,
            source_receipt_sha256=self.source_receipt_sha256,
        )


@dataclass(frozen=True, slots=True)
class BusinessCalendarReceipt:
    calendar_version_ref: str
    holiday_version_ref: str | None
    fiscal_version_ref: str | None
    valid_dates: tuple[date, ...]
    source_receipt_sha256: str
    inspection_evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(
            self.calendar_version_ref,
            "calendar_version_ref",
        )
        for field_name in ("holiday_version_ref", "fiscal_version_ref"):
            value = getattr(self, field_name)
            if value is not None:
                require_nonempty(value, field_name)
        if not isinstance(self.valid_dates, tuple):
            raise TypeError("valid_dates must be a tuple")
        if tuple(sorted(self.valid_dates)) != self.valid_dates:
            raise ValueError("valid_dates must be sorted")
        if len(self.valid_dates) != len(set(self.valid_dates)):
            raise ValueError("valid_dates must be unique")
        require_sha256(
            self.source_receipt_sha256,
            "source_receipt_sha256",
        )
        _require_string_tuple(
            self.inspection_evidence_refs,
            "inspection_evidence_refs",
        )
        if not self.inspection_evidence_refs:
            raise ValueError(
                "business calendar requires inspection evidence"
            )


@dataclass(frozen=True, slots=True)
class CalendarResolutionRequest:
    target_period_ref: str
    target_period_start: date
    target_period_end: date
    target_anchor_ref: str
    anchor_dates: Mapping[str, date]
    calendar_coverage_by_window_rule: Mapping[
        str,
        CalendarCoverageReceipt,
    ]
    exposure_facts: tuple[ExposureCoverageFact, ...]
    business_calendar_receipts: Mapping[str, BusinessCalendarReceipt]
    unit_registry: Mapping[str, UnitExpression]
    unit_registry_contract_ref: str
    unit_registry_receipt_sha256: str
    input_bundle_sha256: str

    def __post_init__(self) -> None:
        require_nonempty(self.target_period_ref, "target_period_ref")
        require_nonempty(self.target_anchor_ref, "target_anchor_ref")
        if self.target_period_end < self.target_period_start:
            raise ValueError("target period end precedes start")
        if not isinstance(self.anchor_dates, Mapping):
            raise TypeError("anchor_dates must be a mapping")
        if not isinstance(self.calendar_coverage_by_window_rule, Mapping):
            raise TypeError(
                "calendar_coverage_by_window_rule must be a mapping"
            )
        if not isinstance(self.exposure_facts, tuple) or any(
            not isinstance(item, ExposureCoverageFact)
            for item in self.exposure_facts
        ):
            raise TypeError(
                "exposure_facts must contain ExposureCoverageFact"
            )
        if not isinstance(self.business_calendar_receipts, Mapping):
            raise TypeError(
                "business_calendar_receipts must be a mapping"
            )
        for anchor_ref, anchor in self.anchor_dates.items():
            require_nonempty(anchor_ref, "anchor_ref")
            if not isinstance(anchor, date):
                raise TypeError("anchor date must be a date")
        target_anchor = self.anchor_dates.get(self.target_anchor_ref)
        if target_anchor is None:
            raise ValueError("target anchor must be present in anchor dates")
        if not (
            self.target_period_start
            <= target_anchor
            <= self.target_period_end
        ):
            raise ValueError(
                "target anchor must fall inside the target period"
            )
        for rule_id, coverage in (
            self.calendar_coverage_by_window_rule.items()
        ):
            if rule_id != coverage.window_rule_id:
                raise ValueError("coverage key must match window_rule_id")
        fact_keys = tuple(
            (fact.window_rule_id, fact.exposure_id)
            for fact in self.exposure_facts
        )
        if len(fact_keys) != len(set(fact_keys)):
            raise ValueError("exposure facts must be unique per window")
        for calendar_ref, receipt in (
            self.business_calendar_receipts.items()
        ):
            require_nonempty(calendar_ref, "calendar_ref")
            if not isinstance(receipt, BusinessCalendarReceipt):
                raise TypeError(
                    "business calendar registry must contain receipts"
                )
            if receipt.calendar_version_ref != calendar_ref:
                raise ValueError(
                    "business calendar key must match receipt"
                )
        if not isinstance(self.unit_registry, Mapping):
            raise TypeError("unit_registry must be a mapping")
        for unit_ref, expression in self.unit_registry.items():
            if not isinstance(expression, UnitExpression):
                raise TypeError(
                    "unit_registry must contain UnitExpression"
                )
            if unit_ref != expression.unit_ref:
                raise ValueError(
                    "unit registry key must match unit_ref"
                )
        require_nonempty(
            self.unit_registry_contract_ref,
            "unit_registry_contract_ref",
        )
        require_sha256(
            self.unit_registry_receipt_sha256,
            "unit_registry_receipt_sha256",
        )
        expected_unit_receipt_sha = content_sha256(
            {
                "unit_registry": self.unit_registry,
                "unit_registry_contract_ref": (
                    self.unit_registry_contract_ref
                ),
            }
        )
        if (
            self.unit_registry_receipt_sha256
            != expected_unit_receipt_sha
        ):
            raise ValueError(
                "unit registry receipt hash is stale or forged"
            )
        require_sha256(
            self.input_bundle_sha256,
            "input_bundle_sha256",
        )
        expected_bundle_sha = content_sha256(
            {
                "target_period_ref": self.target_period_ref,
                "target_period_start": self.target_period_start,
                "target_period_end": self.target_period_end,
                "target_anchor_ref": self.target_anchor_ref,
                "anchor_dates": self.anchor_dates,
                "calendar_coverage_by_window_rule": (
                    self.calendar_coverage_by_window_rule
                ),
                "exposure_facts": self.exposure_facts,
                "business_calendar_receipts": (
                    self.business_calendar_receipts
                ),
                "unit_registry": self.unit_registry,
                "unit_registry_contract_ref": (
                    self.unit_registry_contract_ref
                ),
                "unit_registry_receipt_sha256": (
                    self.unit_registry_receipt_sha256
                ),
            }
        )
        if self.input_bundle_sha256 != expected_bundle_sha:
            raise ValueError("resolution input bundle hash is stale or forged")


@dataclass(frozen=True, slots=True)
class ComparableEstimate:
    numerator_decimal: str
    exposure_decimal: str
    value_decimal: str
    output_unit_ref: str
    normalized: bool
    normalization: ExposureNormalization
    aggregation_order: AggregationOrder
    contributing_component_count: int
    degraded: bool
    limitation_codes: tuple[str, ...]
    unit_proof_sha256: str

    def __post_init__(self) -> None:
        _require_decimal(self.numerator_decimal, "numerator_decimal")
        _require_nonnegative_decimal(
            self.exposure_decimal,
            "exposure_decimal",
        )
        _require_decimal(self.value_decimal, "value_decimal")
        require_nonempty(self.output_unit_ref, "output_unit_ref")
        if not isinstance(self.normalization, ExposureNormalization):
            raise TypeError(
                "normalization must be ExposureNormalization"
            )
        if not isinstance(self.aggregation_order, AggregationOrder):
            raise TypeError(
                "aggregation_order must be AggregationOrder"
            )
        if self.contributing_component_count < 1:
            raise ValueError(
                "comparable estimate requires contributing components"
            )
        _require_string_tuple(
            self.limitation_codes,
            "limitation_codes",
        )
        if len(self.limitation_codes) != len(
            set(self.limitation_codes)
        ):
            raise ValueError("limitation codes must be unique")
        if self.degraded != bool(self.limitation_codes):
            raise ValueError(
                "degraded status must match limitation codes"
            )
        require_sha256(self.unit_proof_sha256, "unit_proof_sha256")


@dataclass(frozen=True, slots=True)
class UnitPower:
    dimension: str
    exponent: int

    def __post_init__(self) -> None:
        require_nonempty(self.dimension, "dimension")
        if self.exponent == 0:
            raise ValueError("unit dimension exponent cannot be zero")


@dataclass(frozen=True, slots=True)
class UnitExpression:
    unit_ref: str
    powers: tuple[UnitPower, ...]
    currency_code: str | None
    scale_decimal: str
    conversion_version_ref: str

    def __post_init__(self) -> None:
        require_nonempty(self.unit_ref, "unit_ref")
        if not isinstance(self.powers, tuple) or any(
            not isinstance(item, UnitPower) for item in self.powers
        ):
            raise TypeError("powers must contain UnitPower")
        dimensions = tuple(item.dimension for item in self.powers)
        if tuple(sorted(dimensions)) != dimensions:
            raise ValueError("unit dimensions must be sorted")
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("unit dimensions must be unique")
        if self.currency_code is not None:
            require_nonempty(self.currency_code, "currency_code")
        if _decimal(self.scale_decimal, "scale_decimal") <= 0:
            raise ValueError("unit scale must be positive")
        require_nonempty(
            self.conversion_version_ref,
            "conversion_version_ref",
        )


@dataclass(frozen=True, slots=True)
class TrustedResolutionInputRegistry:
    """WAJE-owned admission view supplied separately from model/data payloads."""

    registry_ref: str
    issuer_ref: str
    admitted_input_bundle_sha256s: tuple[str, ...]
    admitted_resolution_context_sha256s: tuple[str, ...]
    admitted_source_receipt_sha256s: tuple[str, ...]
    registry_content_sha256: str
    issuer_signature_hex: str

    def __post_init__(self) -> None:
        require_nonempty(self.registry_ref, "registry_ref")
        require_nonempty(self.issuer_ref, "issuer_ref")
        for field_name in (
            "admitted_input_bundle_sha256s",
            "admitted_resolution_context_sha256s",
            "admitted_source_receipt_sha256s",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or not values:
                raise ValueError(f"{field_name} must be a non-empty tuple")
            if tuple(sorted(values)) != values:
                raise ValueError(f"{field_name} must be sorted")
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
            for value in values:
                require_sha256(value, field_name)
        require_sha256(
            self.registry_content_sha256,
            "registry_content_sha256",
        )
        expected = content_sha256(
            {
                "registry_ref": self.registry_ref,
                "issuer_ref": self.issuer_ref,
                "admitted_input_bundle_sha256s": (
                    self.admitted_input_bundle_sha256s
                ),
                "admitted_resolution_context_sha256s": (
                    self.admitted_resolution_context_sha256s
                ),
                "admitted_source_receipt_sha256s": (
                    self.admitted_source_receipt_sha256s
                ),
            }
        )
        if self.registry_content_sha256 != expected:
            raise ValueError(
                "trusted resolution input registry hash is stale or forged"
            )
        _require_ed25519_signature(
            self.issuer_signature_hex,
            "issuer_signature_hex",
        )


@dataclass(frozen=True, slots=True)
class MeasurementResolutionAdmission:
    """Trust-root signature required before a resolution can be persisted."""

    resolution_outcome_id: str
    frame_revision_id: str
    estimand_id: str
    registry_content_sha256: str
    resolver_input_bundle_sha256: str
    resolution_context_sha256: str
    issuer_ref: str
    issuer_signature_hex: str

    def __post_init__(self) -> None:
        require_sha256(
            self.resolution_outcome_id,
            "resolution_outcome_id",
        )
        require_nonempty(self.frame_revision_id, "frame_revision_id")
        require_nonempty(self.estimand_id, "estimand_id")
        for field_name in (
            "registry_content_sha256",
            "resolver_input_bundle_sha256",
            "resolution_context_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        require_nonempty(self.issuer_ref, "issuer_ref")
        _require_ed25519_signature(
            self.issuer_signature_hex,
            "issuer_signature_hex",
        )

    @property
    def signed_content_sha256(self) -> str:
        return content_sha256(
            {
                "resolution_outcome_id": self.resolution_outcome_id,
                "frame_revision_id": self.frame_revision_id,
                "estimand_id": self.estimand_id,
                "registry_content_sha256": (
                    self.registry_content_sha256
                ),
                "resolver_input_bundle_sha256": (
                    self.resolver_input_bundle_sha256
                ),
                "resolution_context_sha256": (
                    self.resolution_context_sha256
                ),
                "issuer_ref": self.issuer_ref,
            }
        )


@dataclass(frozen=True, slots=True)
class TrustedResolutionInputVerifier:
    """Configured trust root used to authenticate resolver admission proofs."""

    issuer_ref: str
    public_key_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        require_nonempty(self.issuer_ref, "issuer_ref")
        if (
            not isinstance(self.public_key_bytes, bytes)
            or len(self.public_key_bytes) != 32
        ):
            raise ValueError(
                "resolution input Ed25519 public key must be 32 bytes"
            )
        Ed25519PublicKey.from_public_bytes(self.public_key_bytes)

    def verify(self, registry: TrustedResolutionInputRegistry) -> None:
        if registry.issuer_ref != self.issuer_ref:
            raise ValueError(
                "resolution input registry issuer is not trusted"
            )
        try:
            Ed25519PublicKey.from_public_bytes(
                self.public_key_bytes
            ).verify(
                bytes.fromhex(registry.issuer_signature_hex),
                registry.registry_content_sha256.encode("ascii"),
            )
        except (InvalidSignature, ValueError):
            raise ValueError(
                "resolution input registry signature is invalid"
            ) from None

    def verify_resolution_admission(
        self,
        *,
        admission: MeasurementResolutionAdmission,
        outcome: MeasurementResolutionOutcome,
    ) -> None:
        if (
            admission.issuer_ref != self.issuer_ref
            or admission.resolution_outcome_id
            != outcome.resolution_outcome_id
            or admission.frame_revision_id != outcome.frame_revision_id
            or admission.estimand_id != outcome.estimand_id
        ):
            raise ValueError(
                "measurement resolution admission identity is stale"
            )
        try:
            Ed25519PublicKey.from_public_bytes(
                self.public_key_bytes
            ).verify(
                bytes.fromhex(admission.issuer_signature_hex),
                admission.signed_content_sha256.encode("ascii"),
            )
        except (InvalidSignature, ValueError):
            raise ValueError(
                "measurement resolution admission signature is invalid"
            ) from None


@dataclass(frozen=True, slots=True)
class TrustedResolutionInputSigner:
    """Private signing capability kept outside authority stores."""

    issuer_ref: str
    private_key_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        require_nonempty(self.issuer_ref, "issuer_ref")
        if (
            not isinstance(self.private_key_bytes, bytes)
            or len(self.private_key_bytes) != 32
        ):
            raise ValueError(
                "resolution input Ed25519 private key must be 32 bytes"
            )
        Ed25519PrivateKey.from_private_bytes(self.private_key_bytes)

    @property
    def public_key_bytes(self) -> bytes:
        return (
            Ed25519PrivateKey.from_private_bytes(
                self.private_key_bytes
            )
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )

    def sign_registry_content(
        self,
        registry_content_sha256: str,
    ) -> str:
        require_sha256(
            registry_content_sha256,
            "registry_content_sha256",
        )
        return self._sign_content(
            registry_content_sha256.encode("ascii")
        )

    def _issue_resolution_admission(
        self,
        *,
        outcome: MeasurementResolutionOutcome,
        registry_content_sha256: str,
        resolver_input_bundle_sha256: str,
        resolution_context_sha256: str,
    ) -> MeasurementResolutionAdmission:
        unsigned = MeasurementResolutionAdmission(
            resolution_outcome_id=outcome.resolution_outcome_id,
            frame_revision_id=outcome.frame_revision_id,
            estimand_id=outcome.estimand_id,
            registry_content_sha256=registry_content_sha256,
            resolver_input_bundle_sha256=resolver_input_bundle_sha256,
            resolution_context_sha256=resolution_context_sha256,
            issuer_ref=self.issuer_ref,
            issuer_signature_hex="0" * 128,
        )
        return replace(
            unsigned,
            issuer_signature_hex=self._sign_content(
                unsigned.signed_content_sha256.encode("ascii")
            ),
        )

    def _sign_content(self, content: bytes) -> str:
        return Ed25519PrivateKey.from_private_bytes(
            self.private_key_bytes
        ).sign(content).hex()


class TrustedMeasurementResolver:
    """Resolver with verify-only input trust and an isolated admission signer."""

    __slots__ = (
        "__trusted_input_verifier",
        "__resolution_admission_signer",
    )

    def __init__(
        self,
        trusted_input_verifier: TrustedResolutionInputVerifier,
        resolution_admission_signer: TrustedResolutionInputSigner,
    ) -> None:
        if not isinstance(
            trusted_input_verifier,
            TrustedResolutionInputVerifier,
        ):
            raise TypeError(
                "trusted_input_verifier must be configured at composition root"
            )
        if not isinstance(
            resolution_admission_signer,
            TrustedResolutionInputSigner,
        ):
            raise TypeError(
                "resolution_admission_signer must be configured at "
                "composition root"
            )
        if (
            resolution_admission_signer.issuer_ref
            != trusted_input_verifier.issuer_ref
            or resolution_admission_signer.public_key_bytes
            != trusted_input_verifier.public_key_bytes
        ):
            raise ValueError(
                "resolution signer and verifier trust roots do not match"
            )
        self.__trusted_input_verifier = trusted_input_verifier
        self.__resolution_admission_signer = (
            resolution_admission_signer
        )

    def resolve_measurement(
        self,
        *,
        frame: AnalysisFrameRevision,
        derivation_authority: MeasurementDerivationAuthority,
        estimand_id: str,
        context: ResolutionContext,
        request: CalendarResolutionRequest,
        trusted_input_registry: TrustedResolutionInputRegistry,
        created_at,
    ) -> MeasurementResolutionOutcome:
        return _resolve_measurement(
            frame=frame,
            derivation_authority=derivation_authority,
            estimand_id=estimand_id,
            context=context,
            request=request,
            trusted_input_registry=trusted_input_registry,
            trusted_input_verifier=self.__trusted_input_verifier,
            created_at=created_at,
        )

    def compile_evidence_obligations(
        self,
        *,
        frame: AnalysisFrameRevision,
        outcome: MeasurementResolutionOutcome,
        context: ResolutionContext,
        resolution_request: CalendarResolutionRequest,
        trusted_input_registry: TrustedResolutionInputRegistry,
        created_at,
    ) -> tuple[ResolvedEvidenceObligation, ...]:
        return _compile_evidence_obligations(
            frame=frame,
            outcome=outcome,
            context=context,
            resolution_request=resolution_request,
            trusted_input_registry=trusted_input_registry,
            trusted_input_verifier=self.__trusted_input_verifier,
            created_at=created_at,
        )

    def admit_resolution(
        self,
        *,
        frame: AnalysisFrameRevision,
        outcome: MeasurementResolutionOutcome,
        context: ResolutionContext,
        request: CalendarResolutionRequest,
        trusted_input_registry: TrustedResolutionInputRegistry,
    ) -> MeasurementResolutionAdmission:
        expected = _resolve_measurement(
            frame=frame,
            derivation_authority=outcome.derivation_authority,
            estimand_id=outcome.estimand_id,
            context=context,
            request=request,
            trusted_input_registry=trusted_input_registry,
            trusted_input_verifier=self.__trusted_input_verifier,
            created_at=outcome.created_at,
        )
        if expected != outcome:
            raise ValueError(
                "resolution admission requires exact deterministic replay"
            )
        self.__trusted_input_verifier.verify(trusted_input_registry)
        _validate_trusted_resolution_inputs(
            context=context,
            request=request,
            registry=trusted_input_registry,
            verifier=self.__trusted_input_verifier,
        )
        return self.__resolution_admission_signer._issue_resolution_admission(
            outcome=outcome,
            registry_content_sha256=(
                trusted_input_registry.registry_content_sha256
            ),
            resolver_input_bundle_sha256=request.input_bundle_sha256,
            resolution_context_sha256=content_sha256(context),
        )


def validate_executable_design(
    design: MeasurementDesign,
    *,
    unit_registry: Mapping[str, UnitExpression] | None = None,
) -> tuple[MeasurementValidationFinding, ...]:
    """Return conditional graph findings for every supported claim shape."""

    findings: list[MeasurementValidationFinding] = []
    indexes = _design_indexes(design)
    for estimand in design.estimands:
        findings.extend(
            _validate_target_contract(
                estimand=estimand,
                design=design,
            )
        )
        required_fields = _required_fields(estimand.claim_target_kind)
        missing = tuple(
            field_name
            for field_name in required_fields
            if getattr(estimand, field_name) is None
        )
        if missing:
            findings.append(
                MeasurementValidationFinding(
                    estimand_id=estimand.estimand_id,
                    code="missing_conditional_nodes",
                    node_refs=missing,
                )
            )
            continue
        if (
            estimand.claim_target_kind is not ClaimTargetKind.DEFINITION
            and not estimand.variable_ids
        ):
            findings.append(
                MeasurementValidationFinding(
                    estimand_id=estimand.estimand_id,
                    code="missing_target_variables",
                    node_refs=(),
                )
            )

        for requirement_id in estimand.evidence_requirement_ids:
            requirement = indexes["evidence_requirements"].get(
                requirement_id
            )
            if requirement is None:
                continue
            if (
                requirement.scope_id != estimand.scope_ceiling_id
                or requirement.exposure_id != estimand.exposure_id
            ):
                findings.append(
                    MeasurementValidationFinding(
                        estimand_id=estimand.estimand_id,
                        code=(
                            "evidence_requirement_measurement_mismatch"
                        ),
                        node_refs=(
                            requirement_id,
                            requirement.scope_id,
                            requirement.exposure_id or "exposure:none",
                        ),
                    )
                )

        estimator = indexes["estimators"].get(estimand.estimator_id)
        if estimator is not None:
            allowed_families = _allowed_estimator_families(
                estimand.claim_target_kind
            )
            if estimator.family not in allowed_families:
                findings.append(
                    MeasurementValidationFinding(
                        estimand_id=estimand.estimand_id,
                        code="incompatible_estimator_family",
                        node_refs=(
                            estimator.estimator_id,
                            estimator.family.value,
                        ),
                    )
                )
            if estimator.exposure_id != estimand.exposure_id:
                findings.append(
                    MeasurementValidationFinding(
                        estimand_id=estimand.estimand_id,
                        code="estimator_exposure_mismatch",
                        node_refs=tuple(
                            value
                            for value in (
                                estimator.estimator_id,
                                estimator.exposure_id,
                                estimand.exposure_id,
                            )
                            if value is not None
                        ),
                    )
                )

        for rule in design.window_rules:
            if (
                rule.rule_kind is WindowRuleKind.ROLLING_INTERVAL
                and rule.selection_kind
                is not WindowSelectionKind.ROLLING_LENGTH
            ):
                findings.append(
                    MeasurementValidationFinding(
                        estimand_id=estimand.estimand_id,
                        code="rolling_rule_selection_mismatch",
                        node_refs=(rule.window_rule_id,),
                    )
                )
            if (
                rule.rule_kind is WindowRuleKind.BUSINESS_CALENDAR
                and rule.selection_kind
                not in {
                    WindowSelectionKind.FIRST_N_VALID_BUSINESS_DAYS,
                    WindowSelectionKind.LAST_N_VALID_BUSINESS_DAYS,
                }
            ):
                findings.append(
                    MeasurementValidationFinding(
                        estimand_id=estimand.estimand_id,
                        code="business_calendar_selection_mismatch",
                        node_refs=(rule.window_rule_id,),
                    )
                )

        variable_units = {
            item.variable_id: item.unit_ref for item in design.variables
        }
        metric_index = {
            item.metric_expression_id: item
            for item in design.metric_expressions
        }
        if estimator is not None:
            metric = metric_index.get(estimator.metric_expression_id)
            if metric is not None:
                output_variable_unit = variable_units.get(
                    metric.output_variable_id
                )
                if output_variable_unit != metric.output_unit_ref:
                    findings.append(
                        MeasurementValidationFinding(
                            estimand_id=estimand.estimand_id,
                            code="metric_output_unit_mismatch",
                            node_refs=(
                                metric.metric_expression_id,
                                metric.output_variable_id,
                            ),
                        )
                    )

        if estimand.claim_target_kind is ClaimTargetKind.CONTRAST:
            contrast = indexes["contrasts"].get(estimand.contrast_id)
            if contrast is not None:
                findings.extend(
                    _validate_contrast_windows(estimand, contrast, indexes)
                )

        if estimand.claim_target_kind is ClaimTargetKind.TEMPORAL_PATTERN:
            scope = indexes["scopes"].get(estimand.scope_ceiling_id)
            if scope is None or not scope.time_window_rule_ids:
                findings.append(
                    MeasurementValidationFinding(
                        estimand_id=estimand.estimand_id,
                        code="temporal_target_has_no_window",
                        node_refs=(estimand.scope_ceiling_id,),
                    )
                )

        if estimand.claim_target_kind is ClaimTargetKind.CAUSAL_EFFECT:
            identification = indexes["identifications"].get(
                estimand.identification_id
            )
            if (
                identification is None
                or identification.level.value != "causal"
            ):
                findings.append(
                    MeasurementValidationFinding(
                        estimand_id=estimand.estimand_id,
                        code="causal_target_lacks_causal_identification",
                        node_refs=(estimand.identification_id or "missing",),
                    )
                )
        if unit_registry is not None:
            findings.extend(
                _validate_metric_unit_contract(
                    estimand=estimand,
                    design=design,
                    unit_registry=unit_registry,
                )
            )
            findings.extend(
                _validate_estimand_unit_contract(
                    estimand=estimand,
                    design=design,
                    unit_registry=unit_registry,
                )
            )
    return tuple(findings)


def _validate_target_contract(
    *,
    estimand: EstimandSpec,
    design: MeasurementDesign,
) -> tuple[MeasurementValidationFinding, ...]:
    target = estimand.claim_target_spec
    mismatches: list[str] = []
    if isinstance(target, ContrastTargetSpec):
        if target.contrast_id != estimand.contrast_id:
            mismatches.append("contrast_id")
    elif isinstance(target, AccountingDecompositionTargetSpec):
        if target.reconciliation_id != estimand.reconciliation_id:
            mismatches.append("reconciliation_id")
    elif isinstance(target, CohortOutcomeTargetSpec):
        if target.cohort_risk_set_id != estimand.cohort_risk_set_id:
            mismatches.append("cohort_risk_set_id")
    elif isinstance(target, FunnelTransitionTargetSpec):
        if target.sequence_id != estimand.sequence_id:
            mismatches.append("sequence_id")
    elif isinstance(target, AssociationTargetSpec):
        if target.relationship_id != estimand.relationship_id:
            mismatches.append("relationship_id")
    elif isinstance(target, CausalEffectTargetSpec):
        if target.relationship_id != estimand.relationship_id:
            mismatches.append("relationship_id")
        if target.identification_id != estimand.identification_id:
            mismatches.append("identification_id")
    elif isinstance(target, CompositionTargetSpec):
        variable_ids = {item.variable_id for item in design.variables}
        if target.whole_variable_id not in variable_ids:
            mismatches.append("whole_variable_id")
        if not set(target.component_variable_ids) <= variable_ids:
            mismatches.append("component_variable_ids")
    elif isinstance(target, DiagnosticSetTargetSpec):
        known_estimands = {
            item.estimand_id for item in design.estimands
        }
        if estimand.estimand_id in target.member_estimand_ids:
            mismatches.append("diagnostic_self_reference")
        if not set(target.member_estimand_ids) <= known_estimands:
            mismatches.append("member_estimand_ids")
    if not mismatches:
        return ()
    return (
        MeasurementValidationFinding(
            estimand_id=estimand.estimand_id,
            code="claim_target_contract_mismatch",
            node_refs=tuple(sorted(mismatches)),
        ),
    )


def _validate_metric_unit_contract(
    *,
    estimand: EstimandSpec,
    design: MeasurementDesign,
    unit_registry: Mapping[str, UnitExpression],
) -> tuple[MeasurementValidationFinding, ...]:
    if estimand.estimator_id is None:
        return ()
    estimator = next(
        item
        for item in design.estimators
        if item.estimator_id == estimand.estimator_id
    )
    metric = next(
        item
        for item in design.metric_expressions
        if item.metric_expression_id == estimator.metric_expression_id
    )
    variables = {
        item.variable_id: item for item in design.variables
    }
    try:
        numerator_units = tuple(
            _unit(unit_registry, variables[item].unit_ref)
            for item in metric.numerator_variable_ids
        )
        numerator_unit = numerator_units[0]
        for component in numerator_units[1:]:
            _assert_unit_equivalent(component, numerator_unit)
        denominator_units = tuple(
            _unit(unit_registry, variables[item].unit_ref)
            for item in metric.denominator_variable_ids
        )
        output_unit = _unit(unit_registry, metric.output_unit_ref)
        if not denominator_units:
            _assert_unit_equivalent(numerator_unit, output_unit)
        else:
            denominator_unit = denominator_units[0]
            for component in denominator_units[1:]:
                _assert_unit_equivalent(component, denominator_unit)
            derived = _divide_units(
                numerator_unit,
                denominator_unit,
                output_unit_ref=metric.output_unit_ref,
            )
            _assert_unit_equivalent(derived, output_unit)
    except (KeyError, IndexError, ValueError):
        return (
            MeasurementValidationFinding(
                estimand_id=estimand.estimand_id,
                code="metric_variable_unit_mismatch",
                node_refs=(
                    metric.metric_expression_id,
                    *metric.numerator_variable_ids,
                    *metric.denominator_variable_ids,
                    metric.output_unit_ref,
                ),
            ),
        )
    return ()


def _validate_estimand_unit_contract(
    *,
    estimand: EstimandSpec,
    design: MeasurementDesign,
    unit_registry: Mapping[str, UnitExpression],
) -> tuple[MeasurementValidationFinding, ...]:
    if estimand.estimator_id is None:
        return ()
    estimator = next(
        item
        for item in design.estimators
        if item.estimator_id == estimand.estimator_id
    )
    metric = next(
        item
        for item in design.metric_expressions
        if item.metric_expression_id == estimator.metric_expression_id
    )
    scope = next(
        item
        for item in design.scopes
        if item.scope_id == estimand.scope_ceiling_id
    )
    exposure = next(
        (
            item
            for item in design.exposures
            if item.exposure_id == estimand.exposure_id
        ),
        None,
    )
    try:
        numerator_unit = _unit(unit_registry, metric.output_unit_ref)
        output_unit = _unit(unit_registry, scope.unit_ref)
        if exposure is None or (
            exposure.normalization is ExposureNormalization.NONE
        ):
            _assert_unit_equivalent(numerator_unit, output_unit)
        else:
            exposure_unit = _unit(unit_registry, exposure.unit_ref)
            derived = _divide_units(
                numerator_unit,
                exposure_unit,
                output_unit_ref=scope.unit_ref,
            )
            _assert_unit_equivalent(derived, output_unit)
            if estimator.aggregation_order is not exposure.aggregation_order:
                raise ValueError(
                    "estimator and exposure aggregation order differ"
                )
            if (
                exposure.normalization
                is ExposureNormalization.WEIGHTED_BY_EXPOSURE
                and estimator.weight_variable_id is None
            ):
                raise ValueError(
                    "weighted exposure requires a weight variable"
                )
    except (KeyError, StopIteration, ValueError):
        return (
            MeasurementValidationFinding(
                estimand_id=estimand.estimand_id,
                code="incompatible_unit_algebra",
                node_refs=tuple(
                    dict.fromkeys(
                        value
                        for value in (
                            metric.metric_expression_id,
                            metric.output_unit_ref,
                            None
                            if exposure is None
                            else exposure.unit_ref,
                            scope.unit_ref,
                        )
                        if value is not None
                    )
                ),
            ),
        )
    return ()


def _validate_trusted_resolution_inputs(
    *,
    context: ResolutionContext,
    request: CalendarResolutionRequest,
    registry: TrustedResolutionInputRegistry,
    verifier: TrustedResolutionInputVerifier,
) -> None:
    verifier.verify(registry)
    if (
        request.input_bundle_sha256
        not in registry.admitted_input_bundle_sha256s
    ):
        raise ValueError(
            "resolution input bundle is not admitted by the trusted registry"
        )
    if (
        content_sha256(context)
        not in registry.admitted_resolution_context_sha256s
    ):
        raise ValueError(
            "resolution context is not admitted by the trusted registry"
        )
    source_receipts = {
        request.unit_registry_receipt_sha256,
        *(
            item.source_receipt_sha256
            for item in request.calendar_coverage_by_window_rule.values()
        ),
        *(
            item.source_receipt_sha256
            for item in request.exposure_facts
        ),
        *(
            item.source_receipt_sha256
            for item in request.business_calendar_receipts.values()
        ),
    }
    if not source_receipts <= set(
        registry.admitted_source_receipt_sha256s
    ):
        raise ValueError(
            "resolution source receipt is not admitted by the trusted registry"
        )


def _resolve_measurement(
    *,
    frame: AnalysisFrameRevision,
    derivation_authority: MeasurementDerivationAuthority,
    estimand_id: str,
    context: ResolutionContext,
    request: CalendarResolutionRequest,
    trusted_input_registry: TrustedResolutionInputRegistry,
    trusted_input_verifier: TrustedResolutionInputVerifier,
    created_at,
) -> MeasurementResolutionOutcome:
    """Resolve one accepted estimand or return a claim-scoped boundary."""

    if (
        derivation_authority.case_id != frame.case_id
        or derivation_authority.accepted_question_revision_id
        != frame.question_revision_id
        or derivation_authority.accepted_frame_revision_id
        != frame.frame_revision_id
    ):
        raise ValueError(
            "measurement derivation authority does not bind the Frame"
        )
    design = frame.measurement_design
    _validate_trusted_resolution_inputs(
        context=context,
        request=request,
        registry=trusted_input_registry,
        verifier=trusted_input_verifier,
    )
    estimand, semantic_id, binding_id = _frame_estimand_identity(
        frame,
        estimand_id,
    )
    findings = tuple(
        finding
        for finding in validate_executable_design(
            design,
            unit_registry=request.unit_registry,
        )
        if finding.estimand_id == estimand_id
    )
    if findings:
        raise ValueError(
            "invalid accepted measurement graph: {}".format(
                ",".join(sorted({item.code for item in findings}))
            )
        )

    context_error = _validate_resolution_context(context)
    if context_error is not None:
        return _boundary_outcome(
            frame=frame,
            derivation_authority=derivation_authority,
            estimand=estimand,
            semantic_id=semantic_id,
            binding_id=binding_id,
            code=context_error,
            failed_contract_refs=(context.calendar_version_ref,),
            inspection_refs=(
                request.input_bundle_sha256,
                content_sha256(context),
            ),
            context=context,
            request=request,
            trusted_input_registry=trusted_input_registry,
            created_at=created_at,
        )

    indexes = _design_indexes(design)
    scope = indexes["scopes"][estimand.scope_ceiling_id]
    operand_by_rule = _operand_by_window_rule(estimand, indexes)
    resolved: list[ResolvedWindow] = []
    for rule_id in scope.time_window_rule_ids:
        rule = indexes["window_rules"].get(rule_id)
        if rule is None:
            return _boundary_outcome(
                frame=frame,
                derivation_authority=derivation_authority,
                estimand=estimand,
                semantic_id=semantic_id,
                binding_id=binding_id,
                code=ResolutionBoundaryCode.INVALID_MEASUREMENT_GRAPH,
                failed_contract_refs=(f"window-rule:{rule_id}",),
                inspection_refs=(f"scope:{scope.scope_id}",),
                context=context,
                request=request,
                trusted_input_registry=trusted_input_registry,
                created_at=created_at,
            )
        result = _resolve_window_rule(
            rule=rule,
            operand_id=operand_by_rule.get(
                rule_id,
                f"scope-window:{rule_id}",
            ),
            context=context,
            request=request,
        )
        if isinstance(result, _WindowBoundary):
            return _boundary_outcome(
                frame=frame,
                derivation_authority=derivation_authority,
                estimand=estimand,
                semantic_id=semantic_id,
                binding_id=binding_id,
                code=result.code,
                failed_contract_refs=result.failed_contract_refs,
                inspection_refs=result.inspection_refs,
                context=context,
                request=request,
                trusted_input_registry=trusted_input_registry,
                created_at=created_at,
            )
        resolved.append(result)

    exposure = indexes["exposures"].get(estimand.exposure_id)
    eligibility = indexes["eligibilities"].get(estimand.eligibility_id)
    coverage_boundary = _validate_resolved_coverage(
        windows=tuple(resolved),
        exposure=exposure,
        eligibility=eligibility,
    )
    requirement_boundaries = ()
    if coverage_boundary is not None:
        failed_contract_refs = (
            eligibility.missingness_contract_ref
            if eligibility is not None
            else context.data_contract_version_ref,
        )
        inspection_refs = tuple(
            sorted(
                {
                    ref
                    for coverage
                    in request.calendar_coverage_by_window_rule.values()
                    for ref in coverage.inspection_evidence_refs
                }
            )
        )
        impacted_requirement_ids = tuple(
            requirement.evidence_requirement_id
            for requirement in design.evidence_requirements
            if (
                requirement.evidence_requirement_id
                in estimand.evidence_requirement_ids
                and requirement.exposure_id == estimand.exposure_id
            )
        )
        if set(impacted_requirement_ids) != set(
            estimand.evidence_requirement_ids
        ):
            requirement_boundaries = tuple(
                _requirement_boundary(
                    frame=frame,
                    estimand=estimand,
                    requirement_id=requirement_id,
                    code=coverage_boundary,
                    failed_contract_refs=failed_contract_refs,
                    inspection_refs=inspection_refs,
                    context=context,
                    request=request,
                    trusted_input_registry=trusted_input_registry,
                )
                for requirement_id in impacted_requirement_ids
            )
        else:
            return _boundary_outcome(
                frame=frame,
                derivation_authority=derivation_authority,
                estimand=estimand,
                semantic_id=semantic_id,
                binding_id=binding_id,
                code=coverage_boundary,
                failed_contract_refs=failed_contract_refs,
                inspection_refs=inspection_refs,
                context=context,
                request=request,
                trusted_input_registry=trusted_input_registry,
                created_at=created_at,
            )

    if (
        estimand.claim_target_kind is ClaimTargetKind.CONTRAST
        and exposure is not None
        and exposure.normalization is ExposureNormalization.NONE
        and len(resolved) >= 2
        and len(
            {
                _window_exposure(window, exposure)
                for window in resolved
            }
        )
        > 1
    ):
        return _boundary_outcome(
            frame=frame,
            derivation_authority=derivation_authority,
            estimand=estimand,
            semantic_id=semantic_id,
            binding_id=binding_id,
            code=ResolutionBoundaryCode.INCOMPARABLE_EXPOSURE,
            failed_contract_refs=(exposure.comparability_rule_ref,),
            inspection_refs=tuple(
                sorted(
                    {
                        fact.source_receipt_sha256
                        for window in resolved
                        for fact in window.exposure_facts
                        if fact.exposure_id == exposure.exposure_id
                    }
                )
            ),
            context=context,
            request=request,
            trusted_input_registry=trusted_input_registry,
            created_at=created_at,
        )

    proof_payload = {
        "frame_revision_id": frame.frame_revision_id,
        "estimand_id": estimand.estimand_id,
        "semantic_measurement_id": semantic_id,
        "authority_binding_id": binding_id,
        "context": context,
        "target_period_ref": request.target_period_ref,
        "resolver_contract_ref": RESOLVER_CONTRACT_REF,
        "resolver_input_bundle_sha256": request.input_bundle_sha256,
        "windows": tuple(resolved),
        "scope": scope,
        "exposure": exposure,
        "eligibility": eligibility,
    }
    proof_sha = hashlib.sha256(
        canonical_identity_json_bytes(proof_payload)
    ).hexdigest()
    instance = ResolvedMeasurementInstance(
        resolution_id="0" * 64,
        semantic_measurement_id=semantic_id,
        authority_binding_id=binding_id,
        frame_revision_id=frame.frame_revision_id,
        estimand_id=estimand.estimand_id,
        context=context,
        target_period_ref=request.target_period_ref,
        windows=tuple(resolved),
        expected_scope_id=scope.scope_id,
        expected_grain_ref=scope.grain_ref,
        expected_unit_ref=scope.unit_ref,
        expected_exposure_id=estimand.exposure_id,
        eligibility_id=estimand.eligibility_id,
        resolver_contract_ref=RESOLVER_CONTRACT_REF,
        resolver_input_bundle_sha256=request.input_bundle_sha256,
        field_derivation_proof_sha256=proof_sha,
    )
    instance = replace(
        instance,
        resolution_id=compute_resolution_id(instance),
    )
    outcome = MeasurementResolutionOutcome(
        resolution_outcome_id="0" * 64,
        case_id=frame.case_id,
        question_revision_id=frame.question_revision_id,
        frame_revision_id=frame.frame_revision_id,
        estimand_id=estimand.estimand_id,
        semantic_measurement_id=semantic_id,
        authority_binding_id=binding_id,
        derivation_authority=derivation_authority,
        kind=ResolutionOutcomeKind.RESOLVED_INSTANCE,
        resolved_instance=instance,
        boundary=None,
        requirement_boundaries=requirement_boundaries,
        created_at=created_at,
    )
    return replace(
        outcome,
        resolution_outcome_id=compute_resolution_outcome_id(outcome),
    )


def _compile_evidence_obligations(
    *,
    frame: AnalysisFrameRevision,
    outcome: MeasurementResolutionOutcome,
    context: ResolutionContext,
    resolution_request: CalendarResolutionRequest,
    trusted_input_registry: TrustedResolutionInputRegistry,
    trusted_input_verifier: TrustedResolutionInputVerifier,
    created_at,
) -> tuple[ResolvedEvidenceObligation, ...]:
    """Compile immutable obligation definitions from Frame requirements."""

    expected_outcome = _resolve_measurement(
        frame=frame,
        derivation_authority=outcome.derivation_authority,
        estimand_id=outcome.estimand_id,
        context=context,
        request=resolution_request,
        trusted_input_registry=trusted_input_registry,
        trusted_input_verifier=trusted_input_verifier,
        created_at=outcome.created_at,
    )
    if expected_outcome != outcome:
        raise ValueError(
            "resolution outcome cannot be reproduced from admitted inputs"
        )
    validate_resolution_identities(outcome)
    validate_resolution_against_frame(frame, outcome)
    if (
        outcome.case_id != frame.case_id
        or outcome.question_revision_id != frame.question_revision_id
        or outcome.frame_revision_id != frame.frame_revision_id
    ):
        raise ValueError("resolution outcome does not bind Frame authority")
    estimand, semantic_id, binding_id = _frame_estimand_identity(
        frame,
        outcome.estimand_id,
    )
    if (
        outcome.semantic_measurement_id != semantic_id
        or outcome.authority_binding_id != binding_id
    ):
        raise ValueError("resolution outcome changes Frame identity")
    requirements = {
        item.evidence_requirement_id: item
        for item in frame.measurement_design.evidence_requirements
    }
    requirement_boundaries = {
        item.evidence_requirement_id: item
        for item in outcome.requirement_boundaries
    }
    if outcome.boundary is not None:
        requirement_boundaries.update(
            {
                requirement_id: outcome.boundary
                for requirement_id
                in outcome.boundary.failed_requirement_ids
            }
        )
    obligations: list[ResolvedEvidenceObligation] = []
    for requirement_id in estimand.evidence_requirement_ids:
        requirement = requirements[requirement_id]
        requirement_scope = next(
            item
            for item in frame.measurement_design.scopes
            if item.scope_id == requirement.scope_id
        )
        if outcome.resolved_instance is not None:
            resolved_rule_ids = {
                item.window_rule_id
                for item in outcome.resolved_instance.windows
            }
            if set(requirement_scope.time_window_rule_ids) != (
                resolved_rule_ids
            ):
                raise ValueError(
                    "requirement scope requires its own resolution outcome"
                )
        if requirement.exposure_id != estimand.exposure_id:
            raise ValueError(
                "requirement exposure does not bind estimand exposure"
            )
        requirement_boundary = requirement_boundaries.get(requirement_id)
        obligations.extend(
            _build_resolved_evidence_obligation(
                frame=frame,
                estimand=estimand,
                semantic_measurement_id=semantic_id,
                authority_binding_id=binding_id,
                outcome=outcome,
                requirement=requirement,
                evidence_type_refs=(evidence_type_ref,),
                requirement_boundary=requirement_boundary,
                created_at=created_at,
            )
            for evidence_type_ref
            in requirement.required_evidence_type_refs
        )
    return tuple(obligations)


def validate_evidence_obligation_derivation(
    *,
    frame: AnalysisFrameRevision,
    outcome: MeasurementResolutionOutcome,
    obligation: ResolvedEvidenceObligation,
) -> None:
    """Rebuild one immutable obligation from admitted measurement authority."""

    validate_resolution_identities(outcome)
    validate_resolution_against_frame(frame, outcome)
    if any(
        (
            obligation.case_id != frame.case_id,
            obligation.frame_revision_id != frame.frame_revision_id,
            obligation.resolution_outcome_id
            != outcome.resolution_outcome_id,
            obligation.estimand_id != outcome.estimand_id,
        )
    ):
        raise ValueError("obligation does not bind its Frame and outcome")
    estimand, semantic_id, binding_id = _frame_estimand_identity(
        frame,
        outcome.estimand_id,
    )
    if (
        outcome.semantic_measurement_id != semantic_id
        or outcome.authority_binding_id != binding_id
    ):
        raise ValueError("obligation outcome changes Frame identity")
    if (
        obligation.evidence_requirement_id
        not in estimand.evidence_requirement_ids
    ):
        raise ValueError("obligation requirement is outside estimand")
    requirement = next(
        item
        for item in frame.measurement_design.evidence_requirements
        if (
            item.evidence_requirement_id
            == obligation.evidence_requirement_id
        )
    )
    scope = next(
        item
        for item in frame.measurement_design.scopes
        if item.scope_id == requirement.scope_id
    )
    if outcome.resolved_instance is not None:
        resolved_rule_ids = {
            item.window_rule_id
            for item in outcome.resolved_instance.windows
        }
        if set(scope.time_window_rule_ids) != resolved_rule_ids:
            raise ValueError(
                "obligation requirement scope changes resolved windows"
            )
    if requirement.exposure_id != estimand.exposure_id:
        raise ValueError(
            "obligation requirement changes estimand exposure"
        )
    requirement_boundary = next(
        (
            item
            for item in outcome.requirement_boundaries
            if (
                item.evidence_requirement_id
                == requirement.evidence_requirement_id
            )
        ),
        None,
    )
    if (
        requirement_boundary is None
        and outcome.boundary is not None
        and requirement.evidence_requirement_id
        in outcome.boundary.failed_requirement_ids
    ):
        requirement_boundary = outcome.boundary
    if (
        not set(obligation.evidence_type_refs)
        <= set(requirement.required_evidence_type_refs)
    ):
        raise ValueError(
            "obligation evidence types are outside Frame requirement"
        )
    expected = _build_resolved_evidence_obligation(
        frame=frame,
        estimand=estimand,
        semantic_measurement_id=semantic_id,
        authority_binding_id=binding_id,
        outcome=outcome,
        requirement=requirement,
        evidence_type_refs=obligation.evidence_type_refs,
        requirement_boundary=requirement_boundary,
        created_at=obligation.created_at,
    )
    if expected != obligation:
        raise ValueError(
            "evidence obligation exact derivation replay failed"
        )


def _build_resolved_evidence_obligation(
    *,
    frame: AnalysisFrameRevision,
    estimand: EstimandSpec,
    semantic_measurement_id: str,
    authority_binding_id: str,
    outcome: MeasurementResolutionOutcome,
    requirement: EvidenceRequirementSpec,
    evidence_type_refs: tuple[str, ...],
    requirement_boundary: TypedResolutionBoundary | None,
    created_at,
) -> ResolvedEvidenceObligation:
    definition = {
        "frame_revision_id": frame.frame_revision_id,
        "estimand_id": estimand.estimand_id,
        "semantic_measurement_id": semantic_measurement_id,
        "authority_binding_id": authority_binding_id,
        "resolution_outcome_id": outcome.resolution_outcome_id,
        "requirement": requirement,
        "evidence_type_refs": evidence_type_refs,
    }
    closure_sha = content_sha256(definition)
    requirement_sha = content_sha256(requirement)
    return ResolvedEvidenceObligation(
        obligation_id=content_sha256(
            {
                "kind": "resolved-evidence-obligation.v1",
                "definition_sha256": closure_sha,
            }
        ),
        case_id=frame.case_id,
        frame_revision_id=frame.frame_revision_id,
        estimand_id=estimand.estimand_id,
        evidence_requirement_id=requirement.evidence_requirement_id,
        evidence_requirement_sha256=requirement_sha,
        evidence_type_refs=evidence_type_refs,
        resolution_outcome_id=outcome.resolution_outcome_id,
        derivation_authority=outcome.derivation_authority,
        execution_disposition=(
            ObligationExecutionDisposition.EXECUTABLE
            if requirement_boundary is None
            else (
                ObligationExecutionDisposition.TYPED_BOUNDARY
                if (
                    requirement.boundary_policy
                    is RequirementBoundaryPolicy.ALLOW_TYPED_BOUNDARY
                    and requirement_boundary.boundary_code
                    in requirement.allowed_boundary_codes
                )
                else ObligationExecutionDisposition.BLOCKED
            )
        ),
        boundary_code=(
            None
            if requirement_boundary is None
            else requirement_boundary.boundary_code
        ),
        closure_definition_sha256=closure_sha,
        field_derivation_proof_sha256=content_sha256(
            {
                "resolution_outcome_id": outcome.resolution_outcome_id,
                "requirement_sha256": requirement_sha,
                "evidence_type_refs": evidence_type_refs,
            }
        ),
        created_at=created_at,
    )


def comparable_estimate(
    *,
    numerator_decimal: str,
    window: ResolvedWindow,
    exposure: ExposureSpec,
    numerator_unit_ref: str,
    output_unit_ref: str,
    unit_registry: Mapping[str, UnitExpression],
    numerator_components_decimal: tuple[str, ...] | None = None,
    exposure_components_decimal: tuple[str, ...] | None = None,
    weight_components_decimal: tuple[str, ...] | None = None,
) -> ComparableEstimate:
    """Apply the Frame-owned exposure algebra to one resolved window."""

    require_nonempty(numerator_unit_ref, "numerator_unit_ref")
    require_nonempty(output_unit_ref, "output_unit_ref")
    numerator = _decimal(numerator_decimal, "numerator_decimal")
    numerator_unit = _unit(unit_registry, numerator_unit_ref)
    exposure_unit = _unit(unit_registry, exposure.unit_ref)
    output_unit = _unit(unit_registry, output_unit_ref)
    denominator = _window_exposure(window, exposure)
    numerator_components = _decimal_components(
        numerator_components_decimal,
        "numerator_components_decimal",
    )
    exposure_components = _decimal_components(
        exposure_components_decimal,
        "exposure_components_decimal",
        nonnegative=True,
    )
    weight_components = _decimal_components(
        weight_components_decimal,
        "weight_components_decimal",
        nonnegative=True,
    )
    _validate_component_alignment(
        numerator_components=numerator_components,
        exposure_components=exposure_components,
        weight_components=weight_components,
    )
    limitations: list[str] = []
    exposure_fact = _resolved_exposure_fact(window, exposure)
    if exposure_fact is None:
        raise ValueError("resolved exposure fact is unavailable")
    missing_exposure = (
        Decimal(exposure_fact.missing_exposure_decimal)
        + Decimal(exposure_fact.invalid_exposure_decimal)
    )
    if missing_exposure > 0:
        if exposure.missing_policy is MissingExposurePolicy.BLOCK:
            raise ValueError("missing exposure is blocked by Frame policy")
        limitations.append(
            "missing_exposure:{}".format(
                exposure.missing_policy.value
            )
        )
        if (
            exposure.missing_policy
            is MissingExposurePolicy.TREAT_AS_ZERO
        ):
            denominator = Decimal(
                exposure_fact.expected_exposure_decimal
            )
    if exposure.normalization is ExposureNormalization.NONE:
        _assert_unit_equivalent(numerator_unit, output_unit)
        scale_factor = (
            Decimal(numerator_unit.scale_decimal)
            / Decimal(output_unit.scale_decimal)
        )
        value = numerator * scale_factor
        normalized = False
        denominator = Decimal("1")
        component_count = (
            len(numerator_components)
            if numerator_components is not None
            else 1
        )
    else:
        derived_unit = _divide_units(
            numerator_unit,
            exposure_unit,
            output_unit_ref=output_unit_ref,
        )
        _assert_unit_equivalent(derived_unit, output_unit)
        scale_factor = (
            Decimal(derived_unit.scale_decimal)
            / Decimal(output_unit.scale_decimal)
        )
        (
            value,
            denominator,
            component_count,
            zero_limitations,
        ) = _normalized_value(
            numerator=numerator,
            denominator=denominator,
            normalization=exposure.normalization,
            aggregation_order=exposure.aggregation_order,
            zero_policy=exposure.zero_policy,
            numerator_components=numerator_components,
            exposure_components=exposure_components,
            weight_components=weight_components,
        )
        limitations.extend(zero_limitations)
        value *= scale_factor
        normalized = True
    proof_sha = content_sha256(
        {
            "numerator_unit": numerator_unit,
            "exposure_unit": (
                None
                if exposure.normalization is ExposureNormalization.NONE
                else exposure_unit
            ),
            "output_unit": output_unit,
            "normalization": exposure.normalization.value,
            "aggregation_order": exposure.aggregation_order.value,
            "zero_policy": exposure.zero_policy.value,
            "missing_policy": exposure.missing_policy.value,
            "numerator_components_decimal": (
                numerator_components_decimal
            ),
            "exposure_components_decimal": (
                exposure_components_decimal
            ),
            "weight_components_decimal": weight_components_decimal,
            "limitation_codes": tuple(sorted(set(limitations))),
            "scale_factor": canonical_decimal_string(scale_factor),
        }
    )
    limitation_codes = tuple(sorted(set(limitations)))
    return ComparableEstimate(
        numerator_decimal=canonical_decimal_string(numerator),
        exposure_decimal=canonical_decimal_string(denominator),
        value_decimal=canonical_decimal_string(value),
        output_unit_ref=output_unit_ref,
        normalized=normalized,
        normalization=exposure.normalization,
        aggregation_order=exposure.aggregation_order,
        contributing_component_count=component_count,
        degraded=bool(limitation_codes),
        limitation_codes=limitation_codes,
        unit_proof_sha256=proof_sha,
    )


def _decimal_components(
    values: tuple[str, ...] | None,
    field_name: str,
    *,
    nonnegative: bool = False,
) -> tuple[Decimal, ...] | None:
    if values is None:
        return None
    if not isinstance(values, tuple) or not values:
        raise ValueError(f"{field_name} must be a non-empty tuple")
    parsed = tuple(
        _decimal(value, f"{field_name}[{index}]")
        for index, value in enumerate(values)
    )
    if nonnegative and any(value < 0 for value in parsed):
        raise ValueError(f"{field_name} cannot contain negatives")
    return parsed


def _validate_component_alignment(
    *,
    numerator_components: tuple[Decimal, ...] | None,
    exposure_components: tuple[Decimal, ...] | None,
    weight_components: tuple[Decimal, ...] | None,
) -> None:
    supplied = tuple(
        len(values)
        for values in (
            numerator_components,
            exposure_components,
            weight_components,
        )
        if values is not None
    )
    if supplied and len(set(supplied)) != 1:
        raise ValueError("estimate component arrays must align")
    if (
        exposure_components is not None
        and numerator_components is None
    ):
        raise ValueError(
            "exposure components require numerator components"
        )
    if weight_components is not None and (
        numerator_components is None
        or exposure_components is None
    ):
        raise ValueError(
            "weight components require numerator and exposure components"
        )


def _normalized_value(
    *,
    numerator: Decimal,
    denominator: Decimal,
    normalization: ExposureNormalization,
    aggregation_order: AggregationOrder,
    zero_policy: MissingExposurePolicy,
    numerator_components: tuple[Decimal, ...] | None,
    exposure_components: tuple[Decimal, ...] | None,
    weight_components: tuple[Decimal, ...] | None,
) -> tuple[Decimal, Decimal, int, tuple[str, ...]]:
    limitations: list[str] = []
    if (
        numerator_components is not None
        and sum(numerator_components, Decimal("0")) != numerator
    ):
        raise ValueError(
            "numerator components do not reconcile to numerator"
        )
    if (
        exposure_components is not None
        and sum(exposure_components, Decimal("0")) != denominator
    ):
        raise ValueError(
            "exposure components do not reconcile to exposure"
        )
    if aggregation_order is AggregationOrder.RATIO_OF_SUMS:
        value, zero_limitation = _divide_with_zero_policy(
            numerator=numerator,
            denominator=denominator,
            zero_policy=zero_policy,
        )
        if zero_limitation is not None:
            limitations.append(zero_limitation)
        return (
            value,
            denominator,
            len(numerator_components or (numerator,)),
            tuple(limitations),
        )

    if aggregation_order not in {
        AggregationOrder.MEAN_OF_RATIOS,
        AggregationOrder.WEIGHTED_MEAN,
    }:
        raise ValueError(
            "normalized exposure requires an explicit ratio aggregation"
        )
    if (
        numerator_components is None
        or exposure_components is None
    ):
        raise ValueError(
            "{} requires component-level numerator and exposure".format(
                aggregation_order.value
            )
        )
    components: list[tuple[Decimal, Decimal, Decimal]] = []
    raw_weights = (
        weight_components
        if weight_components is not None
        else exposure_components
    )
    for component_numerator, component_exposure, component_weight in zip(
        numerator_components,
        exposure_components,
        raw_weights,
        strict=True,
    ):
        if component_exposure == 0:
            if zero_policy is MissingExposurePolicy.BLOCK:
                raise ValueError(
                    "zero component exposure is blocked by Frame policy"
                )
            limitations.append(
                "zero_exposure:{}".format(zero_policy.value)
            )
            if zero_policy is MissingExposurePolicy.EXCLUDE:
                continue
            components.append(
                (
                    Decimal("0"),
                    component_exposure,
                    component_weight,
                )
            )
            continue
        components.append(
            (
                component_numerator / component_exposure,
                component_exposure,
                component_weight,
            )
        )
    if not components:
        raise ValueError("no comparable components remain")
    if aggregation_order is AggregationOrder.MEAN_OF_RATIOS:
        value = sum(
            (item[0] for item in components),
            Decimal("0"),
        ) / Decimal(len(components))
    else:
        if normalization is not ExposureNormalization.WEIGHTED_BY_EXPOSURE:
            raise ValueError(
                "weighted mean requires weighted exposure normalization"
            )
        total_weight = sum(
            (item[2] for item in components),
            Decimal("0"),
        )
        value, zero_limitation = _divide_with_zero_policy(
            numerator=sum(
                (item[0] * item[2] for item in components),
                Decimal("0"),
            ),
            denominator=total_weight,
            zero_policy=zero_policy,
        )
        if zero_limitation is not None:
            limitations.append(zero_limitation)
    return (
        value,
        denominator,
        len(components),
        tuple(limitations),
    )


def _divide_with_zero_policy(
    *,
    numerator: Decimal,
    denominator: Decimal,
    zero_policy: MissingExposurePolicy,
) -> tuple[Decimal, str | None]:
    if denominator > 0:
        return numerator / denominator, None
    if zero_policy is MissingExposurePolicy.BLOCK:
        raise ValueError(
            "zero exposure is blocked by Frame policy"
        )
    if zero_policy is MissingExposurePolicy.EXCLUDE:
        raise ValueError(
            "aggregate zero exposure cannot be excluded without components"
        )
    return Decimal("0"), "zero_exposure:{}".format(zero_policy.value)


def assert_contrast_comparable(
    *,
    estimates: tuple[ComparableEstimate, ...],
    windows: tuple[ResolvedWindow, ...],
    exposure: ExposureSpec,
) -> None:
    """Block raw-total direction claims when realized exposure differs."""

    if len(estimates) != len(windows) or len(estimates) < 2:
        raise ValueError("contrast comparability requires aligned operands")
    if exposure.normalization is not ExposureNormalization.NONE:
        return
    realized = tuple(_window_exposure(window, exposure) for window in windows)
    if len(set(realized)) != 1:
        raise ValueError(
            ResolutionBoundaryCode.INCOMPARABLE_EXPOSURE.value
        )


@dataclass(frozen=True, slots=True)
class _WindowBoundary:
    code: ResolutionBoundaryCode
    failed_contract_refs: tuple[str, ...]
    inspection_refs: tuple[str, ...]


def _resolve_window_rule(
    *,
    rule: WindowRuleSpec,
    operand_id: str,
    context: ResolutionContext,
    request: CalendarResolutionRequest,
) -> ResolvedWindow | _WindowBoundary:
    anchor = request.anchor_dates.get(rule.anchor_ref)
    if rule.rule_kind is not WindowRuleKind.ABSOLUTE_INTERVAL and anchor is None:
        return _WindowBoundary(
            code=ResolutionBoundaryCode.MISSING_ANCHOR,
            failed_contract_refs=(rule.anchor_ref,),
            inspection_refs=(request.target_period_ref,),
        )

    try:
        selected_dates, anchor_date = _calendar_interval(
            rule=rule,
            anchor=anchor,
            context=context,
            request=request,
        )
    except _ResolutionFailure as exc:
        return _WindowBoundary(
            code=exc.code,
            failed_contract_refs=exc.failed_contract_refs,
            inspection_refs=exc.inspection_refs,
        )

    start = selected_dates[0]
    end = selected_dates[-1]
    coverage = request.calendar_coverage_by_window_rule.get(
        rule.window_rule_id
    )
    if coverage is None:
        return _WindowBoundary(
            code=ResolutionBoundaryCode.SNAPSHOT_OUT_OF_RANGE,
            failed_contract_refs=(context.data_contract_version_ref,),
            inspection_refs=(context.snapshot_release_ref,),
        )
    if start < coverage.released_start or end > coverage.released_end:
        return _WindowBoundary(
            code=ResolutionBoundaryCode.SNAPSHOT_OUT_OF_RANGE,
            failed_contract_refs=(context.snapshot_release_ref,),
            inspection_refs=coverage.inspection_evidence_refs
            or (context.coverage_watermark_ref,),
        )
    if (
        coverage.released_at_instant > context.as_of_instant
        or end > coverage.coverage_complete_through
        or coverage.late_arrival_cutoff_instant
        > context.as_of_instant
    ):
        return _WindowBoundary(
            code=ResolutionBoundaryCode.SNAPSHOT_OUT_OF_RANGE,
            failed_contract_refs=(
                context.snapshot_release_ref,
                context.coverage_watermark_ref,
                context.late_arrival_policy_ref,
            ),
            inspection_refs=coverage.inspection_evidence_refs,
        )
    if (
        coverage.snapshot_release_ref != context.snapshot_release_ref
        or coverage.coverage_watermark_ref
        != context.coverage_watermark_ref
    ):
        return _WindowBoundary(
            code=ResolutionBoundaryCode.SNAPSHOT_OUT_OF_RANGE,
            failed_contract_refs=(context.snapshot_release_ref,),
            inspection_refs=coverage.inspection_evidence_refs,
        )
    selected_set = set(selected_dates)
    observed_count = len(selected_set.intersection(coverage.observed_dates))
    valid_count = len(selected_set.intersection(coverage.valid_dates))

    exposure_facts: list[ResolvedExposureFact] = []
    for fact in request.exposure_facts:
        if fact.window_rule_id != rule.window_rule_id:
            continue
        if (
            fact.source_kind
            is ExposureFactSourceKind.CALENDAR_DERIVATION
            and (
                fact.basis is not ExposureBasis.CALENDAR
                or Decimal(fact.expected_exposure_decimal)
                != len(selected_dates)
            )
        ):
            return _WindowBoundary(
                code=ResolutionBoundaryCode.INCOMPATIBLE_UNIT,
                failed_contract_refs=(f"exposure:{fact.exposure_id}",),
                inspection_refs=(fact.source_receipt_sha256,),
            )
        exposure_facts.append(
            ResolvedExposureFact(
                exposure_id=fact.exposure_id,
                basis=fact.basis,
                unit_ref=fact.unit_ref,
                expected_exposure_decimal=fact.expected_exposure_decimal,
                observed_exposure_decimal=fact.observed_exposure_decimal,
                valid_exposure_decimal=fact.valid_exposure_decimal,
                invalid_exposure_decimal=fact.invalid_exposure_decimal,
                missing_exposure_decimal=fact.missing_exposure_decimal,
                coverage_ratio_decimal=_coverage_ratio(
                    fact.valid_exposure_decimal,
                    fact.expected_exposure_decimal,
                ),
                at_risk_exposure_decimal=fact.at_risk_exposure_decimal,
                source_kind=fact.source_kind,
                source_receipt_sha256=fact.source_receipt_sha256,
            )
        )
    start_instant = _resolve_local_instant(
        local_date=start,
        context=context,
    )
    end_instant = _resolve_local_instant(
        local_date=end + timedelta(days=1),
        context=context,
    )
    if start_instant is None or end_instant is None:
        return _WindowBoundary(
            code=ResolutionBoundaryCode.UNSUPPORTED_CALENDAR,
            failed_contract_refs=(context.calendar_version_ref,),
            inspection_refs=(context.timezone,),
        )
    if end_instant > context.as_of_instant:
        return _WindowBoundary(
            code=ResolutionBoundaryCode.SNAPSHOT_OUT_OF_RANGE,
            failed_contract_refs=(context.snapshot_release_ref,),
            inspection_refs=(
                "as-of:{}".format(context.as_of_instant.isoformat()),
            ),
        )
    return ResolvedWindow(
        operand_id=operand_id,
        window_rule_id=rule.window_rule_id,
        anchor_date=anchor_date,
        period_offset=rule.period_offset,
        actual_start=start,
        actual_end=end,
        start_instant=start_instant,
        end_instant=end_instant,
        elapsed_seconds=int(
            (
                end_instant.astimezone(UTC)
                - start_instant.astimezone(UTC)
            ).total_seconds()
        ),
        actual_calendar_days=(end - start).days + 1,
        selected_calendar_dates_count=len(selected_dates),
        observed_calendar_dates_count=observed_count,
        valid_calendar_dates_count=valid_count,
        selected_calendar_dates_sha256=content_sha256(selected_dates),
        calendar_coverage_receipt_sha256=(
            coverage.source_receipt_sha256
        ),
        exposure_facts=tuple(exposure_facts),
    )


def _calendar_interval(
    *,
    rule: WindowRuleSpec,
    anchor: date | None,
    context: ResolutionContext,
    request: CalendarResolutionRequest,
) -> tuple[tuple[date, ...], date]:
    if rule.rule_kind is WindowRuleKind.ABSOLUTE_INTERVAL:
        assert rule.absolute_start is not None
        assert rule.absolute_end is not None
        selected = _apply_interval_boundaries(
            _date_range(rule.absolute_start, rule.absolute_end),
            rule,
        )
        return selected, rule.absolute_start
    assert anchor is not None

    if rule.rule_kind is WindowRuleKind.ROLLING_INTERVAL:
        if (
            rule.selection_kind is not WindowSelectionKind.ROLLING_LENGTH
            or rule.selection_count is None
        ):
            raise _ResolutionFailure(
                ResolutionBoundaryCode.INVALID_WINDOW_RULE,
                (f"window-rule:{rule.window_rule_id}",),
                (request.target_period_ref,),
            )
        rolling_end = _shift_anchor(
            anchor,
            rule.calendar_unit,
            rule.period_offset,
        )
        rolling_start = rolling_end - timedelta(
            days=rule.selection_count - 1
        )
        return (
            _apply_interval_boundaries(
                _date_range(rolling_start, rolling_end),
                rule,
            ),
            anchor,
        )

    if rule.rule_kind is WindowRuleKind.BUSINESS_CALENDAR:
        if rule.selection_kind not in {
            WindowSelectionKind.FIRST_N_VALID_BUSINESS_DAYS,
            WindowSelectionKind.LAST_N_VALID_BUSINESS_DAYS,
        }:
            raise _ResolutionFailure(
                ResolutionBoundaryCode.INVALID_WINDOW_RULE,
                (f"window-rule:{rule.window_rule_id}",),
                (context.calendar_version_ref,),
            )
    elif rule.rule_kind is not WindowRuleKind.RELATIVE_CALENDAR:
        raise _ResolutionFailure(
            ResolutionBoundaryCode.UNSUPPORTED_CALENDAR,
            (f"window-rule-kind:{rule.rule_kind.value}",),
            (context.calendar_version_ref,),
        )

    if rule.calendar_unit is CalendarUnit.FISCAL_PERIOD:
        raise _ResolutionFailure(
            ResolutionBoundaryCode.UNSUPPORTED_CALENDAR,
            (context.fiscal_version_ref or "fiscal-calendar:missing",),
            (context.calendar_version_ref,),
        )
    period_start, period_end = _shifted_period(
        anchor,
        rule.calendar_unit,
        rule.period_offset,
    )
    selection = rule.selection_kind
    if selection is WindowSelectionKind.COMPLETE_PERIOD:
        return (
            _apply_interval_boundaries(
                _date_range(period_start, period_end),
                rule,
            ),
            anchor,
        )
    if selection in {
        WindowSelectionKind.FIRST_N_CALENDAR_DAYS,
        WindowSelectionKind.LAST_N_CALENDAR_DAYS,
    }:
        assert rule.selection_count is not None
        if rule.selection_count > (period_end - period_start).days + 1:
            raise _ResolutionFailure(
                ResolutionBoundaryCode.INVALID_WINDOW_RULE,
                (f"window-rule:{rule.window_rule_id}",),
                (request.target_period_ref,),
            )
        if selection is WindowSelectionKind.FIRST_N_CALENDAR_DAYS:
            start = period_start
            end = start + timedelta(days=rule.selection_count - 1)
        else:
            end = period_end
            start = end - timedelta(days=rule.selection_count - 1)
        return (
            _apply_interval_boundaries(_date_range(start, end), rule),
            anchor,
        )
    if selection is WindowSelectionKind.ORDINAL_RANGE:
        assert rule.ordinal_start is not None
        assert rule.ordinal_end is not None
        start = period_start + timedelta(days=rule.ordinal_start - 1)
        end = period_start + timedelta(days=rule.ordinal_end - 1)
        if end > period_end:
            raise _ResolutionFailure(
                ResolutionBoundaryCode.INVALID_WINDOW_RULE,
                (f"window-rule:{rule.window_rule_id}",),
                (request.target_period_ref,),
            )
        return (
            _apply_interval_boundaries(_date_range(start, end), rule),
            anchor,
        )
    if selection in {
        WindowSelectionKind.FIRST_N_VALID_BUSINESS_DAYS,
        WindowSelectionKind.LAST_N_VALID_BUSINESS_DAYS,
    }:
        calendar_receipt = request.business_calendar_receipts.get(
            context.calendar_version_ref
        )
        if calendar_receipt is None:
            raise _ResolutionFailure(
                ResolutionBoundaryCode.MISSING_CALENDAR_CONTRACT,
                (context.calendar_version_ref,),
                (context.holiday_version_ref or "holiday-calendar:missing",),
            )
        if (
            calendar_receipt.holiday_version_ref
            != context.holiday_version_ref
            or calendar_receipt.fiscal_version_ref
            != context.fiscal_version_ref
        ):
            raise _ResolutionFailure(
                ResolutionBoundaryCode.MISSING_CALENDAR_CONTRACT,
                (
                    context.holiday_version_ref
                    or context.fiscal_version_ref
                    or "calendar-subversion:missing",
                ),
                calendar_receipt.inspection_evidence_refs,
            )
        assert rule.selection_count is not None
        candidates = tuple(
            value
            for value in calendar_receipt.valid_dates
            if period_start <= value <= period_end
        )
        if len(candidates) < rule.selection_count:
            raise _ResolutionFailure(
                ResolutionBoundaryCode.INVALID_WINDOW_RULE,
                (context.calendar_version_ref,),
                (f"window-rule:{rule.window_rule_id}",),
            )
        chosen = (
            candidates[: rule.selection_count]
            if selection
            is WindowSelectionKind.FIRST_N_VALID_BUSINESS_DAYS
            else candidates[-rule.selection_count :]
        )
        return _apply_interval_boundaries(chosen, rule), anchor
    raise _ResolutionFailure(
        ResolutionBoundaryCode.INVALID_WINDOW_RULE,
        (f"window-rule:{rule.window_rule_id}",),
        (request.target_period_ref,),
    )


class _ResolutionFailure(RuntimeError):
    def __init__(
        self,
        code: ResolutionBoundaryCode,
        failed_contract_refs: tuple[str, ...],
        inspection_refs: tuple[str, ...],
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.failed_contract_refs = failed_contract_refs
        self.inspection_refs = inspection_refs


def _shifted_period(
    anchor: date,
    unit: CalendarUnit,
    offset: int,
) -> tuple[date, date]:
    if unit is CalendarUnit.DAY:
        shifted = anchor + timedelta(days=offset)
        return shifted, shifted
    if unit is CalendarUnit.WEEK:
        start = anchor - timedelta(days=anchor.weekday())
        start += timedelta(weeks=offset)
        return start, start + timedelta(days=6)
    if unit is CalendarUnit.MONTH:
        year, month = _shift_month(anchor.year, anchor.month, offset)
        start = date(year, month, 1)
        return start, date(year, month, calendar.monthrange(year, month)[1])
    if unit is CalendarUnit.QUARTER:
        anchor_quarter = (anchor.month - 1) // 3
        absolute_quarter = anchor.year * 4 + anchor_quarter + offset
        year, quarter = divmod(absolute_quarter, 4)
        month = quarter * 3 + 1
        start = date(year, month, 1)
        end_year, end_month = _shift_month(year, month, 2)
        end = date(
            end_year,
            end_month,
            calendar.monthrange(end_year, end_month)[1],
        )
        return start, end
    if unit is CalendarUnit.YEAR:
        year = anchor.year + offset
        return date(year, 1, 1), date(year, 12, 31)
    raise _ResolutionFailure(
        ResolutionBoundaryCode.UNSUPPORTED_CALENDAR,
        (f"calendar-unit:{unit.value}",),
        ("calendar-resolver:gregorian.v1",),
    )


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + offset
    shifted_year, shifted_month = divmod(absolute, 12)
    return shifted_year, shifted_month + 1


def _shift_anchor(
    anchor: date,
    unit: CalendarUnit,
    offset: int,
) -> date:
    if unit is CalendarUnit.DAY:
        return anchor + timedelta(days=offset)
    if unit is CalendarUnit.WEEK:
        return anchor + timedelta(weeks=offset)
    if unit is CalendarUnit.MONTH:
        year, month = _shift_month(anchor.year, anchor.month, offset)
        day = min(anchor.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    if unit is CalendarUnit.QUARTER:
        year, month = _shift_month(anchor.year, anchor.month, offset * 3)
        day = min(anchor.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    if unit is CalendarUnit.YEAR:
        year = anchor.year + offset
        day = min(anchor.day, calendar.monthrange(year, anchor.month)[1])
        return date(year, anchor.month, day)
    raise _ResolutionFailure(
        ResolutionBoundaryCode.UNSUPPORTED_CALENDAR,
        (f"calendar-unit:{unit.value}",),
        ("calendar-resolver:rolling-anchor.v1",),
    )


def _validate_resolution_context(
    context: ResolutionContext,
) -> ResolutionBoundaryCode | None:
    try:
        ZoneInfo(context.timezone)
    except ZoneInfoNotFoundError:
        return ResolutionBoundaryCode.UNSUPPORTED_CALENDAR
    parts = context.business_day_cutoff.split(":")
    if len(parts) != 3:
        return ResolutionBoundaryCode.UNSUPPORTED_CALENDAR
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return ResolutionBoundaryCode.UNSUPPORTED_CALENDAR
    if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59):
        return ResolutionBoundaryCode.UNSUPPORTED_CALENDAR
    return None


def _resolve_local_instant(
    *,
    local_date: date,
    context: ResolutionContext,
) -> datetime | None:
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
    ordered = tuple(
        unique[key] for key in sorted(unique)
    )
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


def _validate_resolved_coverage(
    *,
    windows: tuple[ResolvedWindow, ...],
    exposure: ExposureSpec | None,
    eligibility,
) -> ResolutionBoundaryCode | None:
    eligibility_threshold = (
        Decimal("0")
        if eligibility is None
        else Decimal(eligibility.minimum_coverage_ratio)
    )
    exposure_threshold = (
        Decimal("0")
        if exposure is None
        else Decimal(exposure.minimum_coverage_ratio)
    )
    for window in windows:
        calendar_ratio = (
            Decimal(window.valid_calendar_dates_count)
            / Decimal(window.selected_calendar_dates_count)
        )
        calendar_incomplete = (
            calendar_ratio < eligibility_threshold
            or window.observed_calendar_dates_count
            < window.selected_calendar_dates_count
        )
        if calendar_incomplete:
            completeness_policy = (
                None
                if eligibility is None
                else eligibility.completeness_policy
            )
            if completeness_policy in {
                CompletenessPolicy.REQUIRE_COMPLETE,
                CompletenessPolicy.EXCLUDE_INCOMPLETE,
            }:
                return ResolutionBoundaryCode.INCOMPLETE_PERIOD
            if (
                completeness_policy
                is not CompletenessPolicy.ALLOW_PARTIAL_WITH_EXPOSURE
                or exposure is None
            ):
                return ResolutionBoundaryCode.INSUFFICIENT_VALID_EXPOSURE
        if exposure is None:
            continue
        fact = _resolved_exposure_fact(window, exposure)
        if fact is None:
            return ResolutionBoundaryCode.INSUFFICIENT_VALID_EXPOSURE
        expected = Decimal(fact.expected_exposure_decimal)
        valid = Decimal(fact.valid_exposure_decimal)
        ratio = valid / expected if expected else Decimal("0")
        if ratio >= max(eligibility_threshold, exposure_threshold):
            continue
        if eligibility is not None and eligibility.completeness_policy in {
            CompletenessPolicy.REQUIRE_COMPLETE,
            CompletenessPolicy.EXCLUDE_INCOMPLETE,
        }:
            return ResolutionBoundaryCode.INCOMPLETE_PERIOD
        if (
            exposure is not None
            and exposure.missing_policy.value == "block"
        ):
            return ResolutionBoundaryCode.INSUFFICIENT_VALID_EXPOSURE
        if (
            eligibility is not None
            and eligibility.completeness_policy
            is CompletenessPolicy.DEGRADE_INCOMPLETE
        ):
            return ResolutionBoundaryCode.INSUFFICIENT_VALID_EXPOSURE
        return ResolutionBoundaryCode.INSUFFICIENT_VALID_EXPOSURE
    return None


def _validate_boundary_proof_refs(
    *,
    policy_rule: BoundaryPolicyRule,
    frame: AnalysisFrameRevision,
    context: ResolutionContext,
    request: CalendarResolutionRequest,
    registry: TrustedResolutionInputRegistry,
    failed_contract_refs: tuple[str, ...],
    inspection_refs: tuple[str, ...],
) -> None:
    required_kinds = set(policy_rule.required_proof_kinds)
    if required_kinds != {
        "failed_contract_ref",
        "inspection_evidence_ref",
    }:
        raise ValueError("boundary policy declares unsupported proof kinds")
    if not failed_contract_refs:
        raise ValueError("boundary proof is missing failed contract refs")
    replayable_refs = _string_proof_refs(
        to_jsonable(
            {
                "frame": frame,
                "context": context,
                "request": request,
                "registry": registry,
            }
        )
    )
    replayable_refs.update(
        {
            request.input_bundle_sha256,
            content_sha256(context),
            "as-of:{}".format(context.as_of_instant.isoformat()),
            "calendar-resolver:gregorian.v1",
            "calendar-resolver:rolling-anchor.v1",
            "calendar-resolver:business.v1",
            "calendar-resolver:empty-window",
            "calendar-subversion:missing",
            "fiscal-calendar:missing",
            "holiday-calendar:missing",
        }
    )
    for rule in frame.measurement_design.window_rules:
        replayable_refs.add(f"window-rule:{rule.window_rule_id}")
        replayable_refs.add(f"window-rule-kind:{rule.rule_kind.value}")
        replayable_refs.add(f"calendar-unit:{rule.calendar_unit.value}")
    for scope in frame.measurement_design.scopes:
        replayable_refs.add(f"scope:{scope.scope_id}")
    for exposure in frame.measurement_design.exposures:
        replayable_refs.add(f"exposure:{exposure.exposure_id}")
    unknown_failed = set(failed_contract_refs) - replayable_refs
    if unknown_failed:
        raise ValueError(
            "boundary failed contract proof is not bound to admitted "
            "inputs: "
            + ",".join(sorted(unknown_failed))
        )
    unknown_inspection = set(inspection_refs) - replayable_refs
    if unknown_inspection:
        raise ValueError(
            "boundary inspection proof is not bound to admitted inputs: "
            + ",".join(sorted(unknown_inspection))
        )


def _string_proof_refs(value) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        result: set[str] = set()
        for key, item in value.items():
            if isinstance(key, str):
                result.add(key)
            result.update(_string_proof_refs(item))
        return result
    if isinstance(value, (list, tuple)):
        result: set[str] = set()
        for item in value:
            result.update(_string_proof_refs(item))
        return result
    return set()


def _boundary_outcome(
    *,
    frame: AnalysisFrameRevision,
    derivation_authority: MeasurementDerivationAuthority,
    estimand: EstimandSpec,
    semantic_id: str,
    binding_id: str,
    code: ResolutionBoundaryCode,
    failed_contract_refs: tuple[str, ...],
    inspection_refs: tuple[str, ...],
    context: ResolutionContext,
    request: CalendarResolutionRequest,
    trusted_input_registry: TrustedResolutionInputRegistry,
    created_at,
) -> MeasurementResolutionOutcome:
    policy_rule = BOUNDARY_POLICY_REGISTRY.get(code)
    if policy_rule is None:
        raise ValueError(
            "system or measurement-graph failures cannot become boundaries"
        )
    if not failed_contract_refs or not inspection_refs:
        raise ValueError(
            "typed boundary requires replayable failure proof"
        )
    _validate_boundary_proof_refs(
        policy_rule=policy_rule,
        frame=frame,
        context=context,
        request=request,
        registry=trusted_input_registry,
        failed_contract_refs=failed_contract_refs,
        inspection_refs=inspection_refs,
    )
    requirement_index = {
        item.evidence_requirement_id: item
        for item in frame.measurement_design.evidence_requirements
    }
    applicable = tuple(
        requirement_index[requirement_id]
        for requirement_id in estimand.evidence_requirement_ids
    )
    allowed = [
        requirement
        for requirement in applicable
        if (
            requirement.boundary_policy
            is RequirementBoundaryPolicy.ALLOW_TYPED_BOUNDARY
            and code.value in requirement.allowed_boundary_codes
        )
    ]
    requested_ceiling = (
        ClaimStrengthCeiling.BOUNDARY_ONLY
        if len(allowed) != len(applicable)
        else min(
            (requirement.minimum_strength for requirement in allowed),
            key=_claim_strength_rank,
            default=ClaimStrengthCeiling.BOUNDARY_ONLY,
        )
    )
    ceiling = min(
        (requested_ceiling, policy_rule.maximum_claim_ceiling),
        key=_claim_strength_rank,
    )
    normalized_contract_refs = tuple(
        sorted(set(failed_contract_refs))
    )
    normalized_inspection_refs = tuple(
        sorted(set(inspection_refs))
    )
    proof_sha = content_sha256(
        {
            "frame_revision_id": frame.frame_revision_id,
            "estimand_id": estimand.estimand_id,
            "semantic_measurement_id": semantic_id,
            "authority_binding_id": binding_id,
            "boundary_code": code.value,
            "boundary_policy_ref": BOUNDARY_POLICY_REGISTRY_REF,
            "failed_requirement_ids": estimand.evidence_requirement_ids,
            "failed_contract_refs": normalized_contract_refs,
            "inspection_evidence_refs": normalized_inspection_refs,
            "allowed_claim_ceiling": ceiling.value,
        }
    )
    boundary = TypedResolutionBoundary(
        boundary_code=code.value,
        boundary_policy_ref=BOUNDARY_POLICY_REGISTRY_REF,
        failed_requirement_ids=estimand.evidence_requirement_ids,
        failed_contract_refs=normalized_contract_refs,
        inspection_evidence_refs=normalized_inspection_refs,
        allowed_claim_ceiling=ceiling,
        derivation_proof_sha256=proof_sha,
    )
    outcome = MeasurementResolutionOutcome(
        resolution_outcome_id="0" * 64,
        case_id=frame.case_id,
        question_revision_id=frame.question_revision_id,
        frame_revision_id=frame.frame_revision_id,
        estimand_id=estimand.estimand_id,
        semantic_measurement_id=semantic_id,
        authority_binding_id=binding_id,
        derivation_authority=derivation_authority,
        kind=ResolutionOutcomeKind.TYPED_RESOLUTION_BOUNDARY,
        resolved_instance=None,
        boundary=boundary,
        requirement_boundaries=(),
        created_at=created_at,
    )
    return replace(
        outcome,
        resolution_outcome_id=compute_resolution_outcome_id(outcome),
    )


def _requirement_boundary(
    *,
    frame: AnalysisFrameRevision,
    estimand: EstimandSpec,
    requirement_id: str,
    code: ResolutionBoundaryCode,
    failed_contract_refs: tuple[str, ...],
    inspection_refs: tuple[str, ...],
    context: ResolutionContext,
    request: CalendarResolutionRequest,
    trusted_input_registry: TrustedResolutionInputRegistry,
) -> RequirementResolutionBoundary:
    policy_rule = BOUNDARY_POLICY_REGISTRY.get(code)
    if policy_rule is None:
        raise ValueError("untrusted code cannot become a requirement boundary")
    if not failed_contract_refs or not inspection_refs:
        raise ValueError(
            "requirement boundary requires replayable failure proof"
        )
    _validate_boundary_proof_refs(
        policy_rule=policy_rule,
        frame=frame,
        context=context,
        request=request,
        registry=trusted_input_registry,
        failed_contract_refs=failed_contract_refs,
        inspection_refs=inspection_refs,
    )
    requirement = next(
        item
        for item in frame.measurement_design.evidence_requirements
        if item.evidence_requirement_id == requirement_id
    )
    requested_ceiling = (
        requirement.minimum_strength
        if (
            requirement.boundary_policy
            is RequirementBoundaryPolicy.ALLOW_TYPED_BOUNDARY
            and code.value in requirement.allowed_boundary_codes
        )
        else ClaimStrengthCeiling.BOUNDARY_ONLY
    )
    ceiling = min(
        (requested_ceiling, policy_rule.maximum_claim_ceiling),
        key=_claim_strength_rank,
    )
    proof_sha = content_sha256(
        {
            "frame_revision_id": frame.frame_revision_id,
            "estimand_id": estimand.estimand_id,
            "evidence_requirement_id": requirement_id,
            "boundary_code": code.value,
            "boundary_policy_ref": BOUNDARY_POLICY_REGISTRY_REF,
            "failed_contract_refs": tuple(sorted(set(failed_contract_refs))),
            "inspection_evidence_refs": tuple(
                sorted(set(inspection_refs))
            ),
            "allowed_claim_ceiling": ceiling.value,
        }
    )
    return RequirementResolutionBoundary(
        evidence_requirement_id=requirement_id,
        boundary_code=code.value,
        boundary_policy_ref=BOUNDARY_POLICY_REGISTRY_REF,
        failed_contract_refs=tuple(sorted(set(failed_contract_refs))),
        inspection_evidence_refs=tuple(sorted(set(inspection_refs))),
        allowed_claim_ceiling=ceiling,
        derivation_proof_sha256=proof_sha,
    )


def _frame_estimand_identity(
    frame: AnalysisFrameRevision,
    estimand_id: str,
) -> tuple[EstimandSpec, str, str]:
    for index, estimand in enumerate(frame.measurement_design.estimands):
        if estimand.estimand_id == estimand_id:
            return (
                estimand,
                frame.semantic_measurement_ids[index],
                frame.authority_binding_ids[index],
            )
    raise ValueError("unknown Frame estimand")


def _design_indexes(
    design: MeasurementDesign,
) -> dict[str, dict[str | None, object]]:
    return {
        "estimators": {
            item.estimator_id: item for item in design.estimators
        },
        "contrasts": {
            item.contrast_id: item for item in design.contrasts
        },
        "scopes": {item.scope_id: item for item in design.scopes},
        "window_rules": {
            item.window_rule_id: item for item in design.window_rules
        },
        "exposures": {
            item.exposure_id: item for item in design.exposures
        },
        "eligibilities": {
            item.eligibility_id: item for item in design.eligibilities
        },
        "identifications": {
            item.identification_id: item
            for item in design.identifications
        },
        "evidence_requirements": {
            item.evidence_requirement_id: item
            for item in design.evidence_requirements
        },
    }


def _operand_by_window_rule(
    estimand: EstimandSpec,
    indexes: Mapping[str, Mapping[str | None, object]],
) -> dict[str, str]:
    if estimand.contrast_id is None:
        return {}
    contrast = indexes["contrasts"].get(estimand.contrast_id)
    if not isinstance(contrast, ContrastSpec):
        return {}
    return {
        operand.window_rule_id: operand.operand_id
        for operand in contrast.operands
    }


def _validate_contrast_windows(
    estimand: EstimandSpec,
    contrast: ContrastSpec,
    indexes: Mapping[str, Mapping[str | None, object]],
) -> tuple[MeasurementValidationFinding, ...]:
    scope = indexes["scopes"].get(estimand.scope_ceiling_id)
    if scope is None:
        return (
            MeasurementValidationFinding(
                estimand_id=estimand.estimand_id,
                code="missing_scope",
                node_refs=(estimand.scope_ceiling_id,),
            ),
        )
    operand_rules = tuple(
        operand.window_rule_id for operand in contrast.operands
    )
    if set(operand_rules) != set(scope.time_window_rule_ids):
        return (
            MeasurementValidationFinding(
                estimand_id=estimand.estimand_id,
                code="contrast_scope_window_mismatch",
                node_refs=tuple(
                    sorted(set(operand_rules + scope.time_window_rule_ids))
                ),
            ),
        )
    if any(
        rule_id not in indexes["window_rules"] for rule_id in operand_rules
    ):
        return (
            MeasurementValidationFinding(
                estimand_id=estimand.estimand_id,
                code="contrast_window_rule_missing",
                node_refs=operand_rules,
            ),
        )
    return ()


def _required_fields(kind: ClaimTargetKind) -> tuple[str, ...]:
    base = (
        "population_id",
        "observation_unit_id",
        "estimator_id",
        "eligibility_id",
        "identification_id",
    )
    conditional = {
        ClaimTargetKind.DEFINITION: (),
        ClaimTargetKind.DATA_QUALITY_STATE: (),
        ClaimTargetKind.POINT_QUANTITY: base,
        ClaimTargetKind.DISTRIBUTION: base,
        ClaimTargetKind.TEMPORAL_PATTERN: base + ("temporal_semantic_id",),
        ClaimTargetKind.CONTRAST: base
        + ("temporal_semantic_id", "contrast_id"),
        ClaimTargetKind.COMPOSITION: base,
        ClaimTargetKind.ACCOUNTING_DECOMPOSITION: base
        + ("reconciliation_id",),
        ClaimTargetKind.COHORT_OUTCOME: base
        + ("temporal_semantic_id", "cohort_risk_set_id"),
        ClaimTargetKind.FUNNEL_TRANSITION: base
        + ("temporal_semantic_id", "sequence_id"),
        ClaimTargetKind.ASSOCIATION: base + ("relationship_id",),
        ClaimTargetKind.CAUSAL_EFFECT: base
        + ("temporal_semantic_id", "relationship_id"),
        ClaimTargetKind.DIAGNOSTIC_SET: base,
    }
    return conditional[kind]


def _allowed_estimator_families(
    kind: ClaimTargetKind,
) -> frozenset[EstimatorFamily]:
    general = frozenset(
        {
            EstimatorFamily.TOTAL,
            EstimatorFamily.MEAN,
            EstimatorFamily.RATE,
            EstimatorFamily.RATIO,
        }
    )
    mapping = {
        ClaimTargetKind.DEFINITION: frozenset(EstimatorFamily),
        ClaimTargetKind.DATA_QUALITY_STATE: frozenset(EstimatorFamily),
        ClaimTargetKind.POINT_QUANTITY: general,
        ClaimTargetKind.DISTRIBUTION: frozenset(
            {EstimatorFamily.DISTRIBUTION, EstimatorFamily.QUANTILE}
        ),
        ClaimTargetKind.TEMPORAL_PATTERN: general,
        ClaimTargetKind.CONTRAST: general,
        ClaimTargetKind.COMPOSITION: frozenset(
            {EstimatorFamily.RATIO, EstimatorFamily.DISTRIBUTION}
        ),
        ClaimTargetKind.ACCOUNTING_DECOMPOSITION: frozenset(
            {EstimatorFamily.ACCOUNTING_BRIDGE}
        ),
        ClaimTargetKind.COHORT_OUTCOME: general,
        ClaimTargetKind.FUNNEL_TRANSITION: frozenset(
            {EstimatorFamily.RATE, EstimatorFamily.RATIO}
        ),
        ClaimTargetKind.ASSOCIATION: frozenset(
            {EstimatorFamily.ASSOCIATION}
        ),
        ClaimTargetKind.CAUSAL_EFFECT: frozenset({EstimatorFamily.EFFECT}),
        ClaimTargetKind.DIAGNOSTIC_SET: frozenset(EstimatorFamily),
    }
    return mapping[kind]


def _window_exposure(
    window: ResolvedWindow,
    exposure: ExposureSpec,
) -> Decimal:
    fact = _resolved_exposure_fact(window, exposure)
    if fact is None:
        raise ValueError("resolved exposure fact is unavailable")
    if exposure.basis is ExposureBasis.CALENDAR:
        return Decimal(fact.expected_exposure_decimal)
    if exposure.basis is ExposureBasis.ELIGIBLE:
        return Decimal(fact.expected_exposure_decimal)
    if exposure.basis is ExposureBasis.OBSERVED:
        return Decimal(fact.observed_exposure_decimal)
    if exposure.basis is ExposureBasis.VALID:
        return Decimal(fact.valid_exposure_decimal)
    if exposure.basis is ExposureBasis.MISSING_INVALID:
        return Decimal(fact.missing_exposure_decimal) + Decimal(
            fact.invalid_exposure_decimal
        )
    if exposure.basis is ExposureBasis.AT_RISK:
        if fact.at_risk_exposure_decimal is None:
            raise ValueError("at-risk exposure is unavailable")
        return Decimal(fact.at_risk_exposure_decimal)
    raise ValueError("unsupported exposure basis")


def _resolved_exposure_fact(
    window: ResolvedWindow,
    exposure: ExposureSpec,
) -> ResolvedExposureFact | None:
    for fact in window.exposure_facts:
        if fact.exposure_id == exposure.exposure_id:
            if (
                fact.basis is not exposure.basis
                or fact.unit_ref != exposure.unit_ref
            ):
                raise ValueError("resolved exposure changes Frame contract")
            return fact
    return None


def _claim_strength_rank(value: ClaimStrengthCeiling) -> int:
    order = (
        ClaimStrengthCeiling.BOUNDARY_ONLY,
        ClaimStrengthCeiling.DESCRIPTIVE,
        ClaimStrengthCeiling.ACCOUNTING,
        ClaimStrengthCeiling.ASSOCIATIONAL,
        ClaimStrengthCeiling.CAUSAL,
    )
    return order.index(value)


def _unit(
    registry: Mapping[str, UnitExpression],
    unit_ref: str,
) -> UnitExpression:
    value = registry.get(unit_ref)
    if value is None:
        raise ValueError(f"unit contract is unavailable: {unit_ref}")
    if value.unit_ref != unit_ref:
        raise ValueError("unit registry key does not match its expression")
    return value


def _divide_units(
    numerator: UnitExpression,
    denominator: UnitExpression,
    *,
    output_unit_ref: str,
) -> UnitExpression:
    powers: dict[str, int] = {
        item.dimension: item.exponent for item in numerator.powers
    }
    for item in denominator.powers:
        powers[item.dimension] = powers.get(item.dimension, 0) - item.exponent
        if powers[item.dimension] == 0:
            del powers[item.dimension]
    if (
        numerator.conversion_version_ref
        != denominator.conversion_version_ref
    ):
        raise ValueError("unit conversion versions are incompatible")
    return UnitExpression(
        unit_ref=output_unit_ref,
        powers=tuple(
            UnitPower(dimension=dimension, exponent=exponent)
            for dimension, exponent in sorted(powers.items())
        ),
        currency_code=numerator.currency_code,
        scale_decimal=canonical_decimal_string(
            Decimal(numerator.scale_decimal)
            / Decimal(denominator.scale_decimal)
        ),
        conversion_version_ref=numerator.conversion_version_ref,
    )


def _assert_unit_equivalent(
    actual: UnitExpression,
    expected: UnitExpression,
) -> None:
    if (
        actual.powers != expected.powers
        or actual.currency_code != expected.currency_code
        or actual.conversion_version_ref
        != expected.conversion_version_ref
    ):
        raise ValueError(ResolutionBoundaryCode.INCOMPATIBLE_UNIT.value)


def _date_range(start: date, end: date) -> tuple[date, ...]:
    return tuple(
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    )


def _apply_interval_boundaries(
    dates: tuple[date, ...],
    rule: WindowRuleSpec,
) -> tuple[date, ...]:
    selected = dates
    if rule.start_boundary.value == "exclusive":
        selected = selected[1:]
    if rule.end_boundary.value == "exclusive":
        selected = selected[:-1]
    if not selected:
        raise _ResolutionFailure(
            ResolutionBoundaryCode.INVALID_WINDOW_RULE,
            (f"window-rule:{rule.window_rule_id}",),
            ("calendar-resolver:empty-window",),
        )
    return selected


def _decimal(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


def _require_ed25519_signature(
    value: str,
    field_name: str,
) -> None:
    if not isinstance(value, str) or len(value) != 128:
        raise ValueError(
            f"{field_name} must be a 64-byte Ed25519 signature in hex"
        )
    try:
        decoded = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError(
            f"{field_name} must be hexadecimal"
        ) from error
    if len(decoded) != 64:
        raise ValueError(
            f"{field_name} must be a 64-byte Ed25519 signature in hex"
        )


def _require_decimal(value: str, field_name: str) -> None:
    _decimal(value, field_name)


def _require_nonnegative_decimal(value: str, field_name: str) -> None:
    if _decimal(value, field_name) < 0:
        raise ValueError(f"{field_name} must be non-negative")


def _coverage_ratio(valid: str, expected: str) -> str:
    expected_value = _decimal(expected, "expected_exposure_decimal")
    valid_value = _decimal(valid, "valid_exposure_decimal")
    if expected_value < 0 or valid_value < 0:
        raise ValueError("exposure values must be non-negative")
    return canonical_decimal_string(
        valid_value / expected_value
        if expected_value
        else Decimal("0")
    )


def _require_string_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field_name} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must be unique")
