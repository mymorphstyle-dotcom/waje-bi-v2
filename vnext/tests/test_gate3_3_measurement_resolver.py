from __future__ import annotations

import calendar
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from gate1_fixtures import make_frame, make_measurement_design
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.identity import (
    compute_resolution_id,
    compute_resolution_outcome_id,
    compute_typed_boundary_derivation_proof,
    validate_resolution_against_frame,
    validate_resolution_identities,
)
from waje_vnext.domain.measurement import (
    AggregationOrder,
    AmbiguousLocalTimePolicy,
    CalendarUnit,
    ClaimTargetKind,
    ClaimStrengthCeiling,
    CompletenessPolicy,
    ExposureBasis,
    ExposureFactSourceKind,
    ExposureNormalization,
    IntervalBoundary,
    ObligationExecutionDisposition,
    MissingExposurePolicy,
    RequirementBoundaryPolicy,
    ResolutionContext,
    ResolutionOutcomeKind,
    WindowRuleKind,
    WindowSelectionKind,
)
from waje_vnext.domain.measurement_resolver import (
    BusinessCalendarReceipt,
    CalendarCoverageReceipt,
    CalendarResolutionRequest,
    ExposureCoverageFact,
    ResolutionBoundaryCode,
    TrustedMeasurementResolver,
    TrustedResolutionInputRegistry,
    TrustedResolutionInputSigner,
    TrustedResolutionInputVerifier,
    UnitExpression,
    UnitPower,
    assert_contrast_comparable,
    claim_target_validation_contracts,
    comparable_estimate,
    validate_executable_design,
)


NOW = datetime(2026, 7, 31, 8, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
TRUSTED_ISSUER_REF = "trusted-resolution-input-issuer:test:v1"
TRUSTED_PRIVATE_KEY = b"waje-vnext-ed25519-test-key-001!"


def make_context(
    *,
    timezone: str = "Asia/Shanghai",
    cutoff: str = "04:00:00",
    as_of: datetime = NOW,
) -> ResolutionContext:
    return ResolutionContext(
        as_of_instant=as_of,
        timezone=timezone,
        business_day_cutoff=cutoff,
        ambiguous_local_time_policy=(
            AmbiguousLocalTimePolicy.EARLIEST_FOLD
        ),
        calendar_version_ref="calendar:gregorian:v1",
        holiday_version_ref=None,
        fiscal_version_ref=None,
        data_contract_version_ref="data-contract:payments:v3",
        snapshot_release_ref="release:resolver-test",
        coverage_watermark_ref="watermark:resolver-test",
        late_arrival_policy_ref="late-arrival:t-plus-1:v1",
    )


def make_request(
    frame,
    *,
    anchor: date,
    expected: str = "7",
    observed: str = "7",
    valid: str = "7",
    invalid: str = "0",
    missing: str = "0",
) -> CalendarResolutionRequest:
    design = frame.measurement_design
    exposure = design.exposures[0]
    coverage = {
        rule.window_rule_id: CalendarCoverageReceipt(
            window_rule_id=rule.window_rule_id,
            released_start=date(1899, 1, 1),
            released_end=date(2101, 12, 31),
            released_at_instant=datetime(1899, 1, 1, tzinfo=UTC),
            coverage_complete_through=date(2101, 12, 31),
            late_arrival_cutoff_instant=datetime(
                1899,
                1,
                2,
                tzinfo=UTC,
            ),
            observed_dates=_selected_dates_for_test(rule, anchor),
            valid_dates=_selected_dates_for_test(rule, anchor),
            snapshot_release_ref="release:resolver-test",
            coverage_watermark_ref="watermark:resolver-test",
            source_receipt_sha256=content_sha256(
                {
                    "rule": rule.window_rule_id,
                    "release": "release:resolver-test",
                }
            ),
            inspection_evidence_refs=(
                f"inspection:coverage:{rule.window_rule_id}",
            ),
        )
        for rule in design.window_rules
    }
    exposure_facts = tuple(
        ExposureCoverageFact(
            window_rule_id=rule.window_rule_id,
            exposure_id=exposure.exposure_id,
            basis=exposure.basis,
            unit_ref=exposure.unit_ref,
            expected_exposure_decimal=expected,
            observed_exposure_decimal=observed,
            valid_exposure_decimal=valid,
            invalid_exposure_decimal=invalid,
            missing_exposure_decimal=missing,
            at_risk_exposure_decimal=None,
            source_kind=ExposureFactSourceKind.SNAPSHOT_CATALOG,
            source_receipt_sha256=content_sha256(
                {
                    "rule": rule.window_rule_id,
                    "exposure": exposure.exposure_id,
                    "expected": expected,
                    "observed": observed,
                    "valid": valid,
                }
            ),
        )
        for rule in design.window_rules
    )
    target_period_start = anchor.replace(day=1)
    target_period_end = date(
        anchor.year,
        anchor.month,
        calendar.monthrange(anchor.year, anchor.month)[1],
    )
    target_anchor_ref = "anchor:target-month"
    anchors = {target_anchor_ref: anchor}
    business_calendar_receipts = {}
    unit_registry_contract_ref = "unit-registry:test:v1"
    unit_registry = {
        "currency:CNY": UnitExpression(
            unit_ref="currency:CNY",
            powers=(UnitPower(dimension="currency", exponent=1),),
            currency_code="CNY",
            scale_decimal="1",
            conversion_version_ref="unit-conversion:test:v1",
        ),
        "unit:valid-observed-day": UnitExpression(
            unit_ref="unit:valid-observed-day",
            powers=(
                UnitPower(
                    dimension="valid-observed-day",
                    exponent=1,
                ),
            ),
            currency_code=None,
            scale_decimal="1",
            conversion_version_ref="unit-conversion:test:v1",
        ),
        "currency:CNY-per-valid-observed-day": UnitExpression(
            unit_ref="currency:CNY-per-valid-observed-day",
            powers=(
                UnitPower(dimension="currency", exponent=1),
                UnitPower(
                    dimension="valid-observed-day",
                    exponent=-1,
                ),
            ),
            currency_code="CNY",
            scale_decimal="1",
            conversion_version_ref="unit-conversion:test:v1",
        ),
    }
    unit_registry_receipt_sha256 = content_sha256(
        {
            "unit_registry": unit_registry,
            "unit_registry_contract_ref": unit_registry_contract_ref,
        }
    )
    payload = {
        "target_period_ref": f"target-period:{anchor:%Y-%m}",
        "target_period_start": target_period_start,
        "target_period_end": target_period_end,
        "target_anchor_ref": target_anchor_ref,
        "anchor_dates": anchors,
        "calendar_coverage_by_window_rule": coverage,
        "exposure_facts": exposure_facts,
        "business_calendar_receipts": business_calendar_receipts,
        "unit_registry": unit_registry,
        "unit_registry_contract_ref": unit_registry_contract_ref,
        "unit_registry_receipt_sha256": unit_registry_receipt_sha256,
    }
    return CalendarResolutionRequest(
        target_period_ref=payload["target_period_ref"],
        target_period_start=target_period_start,
        target_period_end=target_period_end,
        target_anchor_ref=target_anchor_ref,
        anchor_dates=anchors,
        calendar_coverage_by_window_rule=coverage,
        exposure_facts=exposure_facts,
        business_calendar_receipts=business_calendar_receipts,
        unit_registry=unit_registry,
        unit_registry_contract_ref=unit_registry_contract_ref,
        unit_registry_receipt_sha256=unit_registry_receipt_sha256,
        input_bundle_sha256=content_sha256(payload),
    )


def _selected_dates_for_test(rule, anchor: date) -> tuple[date, ...]:
    if rule.rule_kind is WindowRuleKind.ABSOLUTE_INTERVAL:
        start = rule.absolute_start
        end = rule.absolute_end
        assert start is not None and end is not None
    else:
        absolute_month = (
            anchor.year * 12 + anchor.month - 1 + rule.period_offset
        )
        year, zero_month = divmod(absolute_month, 12)
        month = zero_month + 1
        period_start = date(year, month, 1)
        period_end = date(
            year,
            month,
            calendar.monthrange(year, month)[1],
        )
        count = rule.selection_count
        assert count is not None
        if rule.selection_kind is WindowSelectionKind.FIRST_N_CALENDAR_DAYS:
            start = period_start
            end = start + timedelta(days=count - 1)
        elif rule.selection_kind is WindowSelectionKind.LAST_N_CALENDAR_DAYS:
            end = period_end
            start = end - timedelta(days=count - 1)
        elif rule.selection_kind is WindowSelectionKind.ROLLING_LENGTH:
            if rule.calendar_unit is CalendarUnit.DAY:
                end = anchor + timedelta(days=rule.period_offset)
            elif rule.calendar_unit is CalendarUnit.WEEK:
                end = anchor + timedelta(weeks=rule.period_offset)
            elif rule.calendar_unit is CalendarUnit.MONTH:
                absolute = (
                    anchor.year * 12
                    + anchor.month
                    - 1
                    + rule.period_offset
                )
                year, zero_month = divmod(absolute, 12)
                month = zero_month + 1
                end = date(
                    year,
                    month,
                    min(
                        anchor.day,
                        calendar.monthrange(year, month)[1],
                    ),
                )
            else:
                raise AssertionError(
                    "test helper does not cover rolling unit"
                )
            start = end - timedelta(days=count - 1)
        else:
            start, end = period_start, period_end
    dates = tuple(
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    )
    if rule.start_boundary.value == "exclusive":
        dates = dates[1:]
    if rule.end_boundary.value == "exclusive":
        dates = dates[:-1]
    return dates


def rebuild_request(
    request: CalendarResolutionRequest,
    *,
    coverage=None,
    exposure_facts=None,
    business_calendar_receipts=None,
    unit_registry=None,
    target_period_start=None,
    target_period_end=None,
    target_anchor_ref=None,
    anchor_dates=None,
) -> CalendarResolutionRequest:
    coverage = (
        request.calendar_coverage_by_window_rule
        if coverage is None
        else coverage
    )
    exposure_facts = (
        request.exposure_facts
        if exposure_facts is None
        else exposure_facts
    )
    business_calendar_receipts = (
        request.business_calendar_receipts
        if business_calendar_receipts is None
        else business_calendar_receipts
    )
    unit_registry = (
        request.unit_registry
        if unit_registry is None
        else unit_registry
    )
    target_period_start = (
        request.target_period_start
        if target_period_start is None
        else target_period_start
    )
    target_period_end = (
        request.target_period_end
        if target_period_end is None
        else target_period_end
    )
    target_anchor_ref = (
        request.target_anchor_ref
        if target_anchor_ref is None
        else target_anchor_ref
    )
    anchor_dates = (
        request.anchor_dates
        if anchor_dates is None
        else anchor_dates
    )
    unit_registry_receipt_sha256 = content_sha256(
        {
            "unit_registry": unit_registry,
            "unit_registry_contract_ref": (
                request.unit_registry_contract_ref
            ),
        }
    )
    payload = {
        "target_period_ref": request.target_period_ref,
        "target_period_start": target_period_start,
        "target_period_end": target_period_end,
        "target_anchor_ref": target_anchor_ref,
        "anchor_dates": anchor_dates,
        "calendar_coverage_by_window_rule": coverage,
        "exposure_facts": exposure_facts,
        "business_calendar_receipts": business_calendar_receipts,
        "unit_registry": unit_registry,
        "unit_registry_contract_ref": request.unit_registry_contract_ref,
        "unit_registry_receipt_sha256": unit_registry_receipt_sha256,
    }
    return CalendarResolutionRequest(
        target_period_ref=request.target_period_ref,
        target_period_start=target_period_start,
        target_period_end=target_period_end,
        target_anchor_ref=target_anchor_ref,
        anchor_dates=anchor_dates,
        calendar_coverage_by_window_rule=coverage,
        exposure_facts=exposure_facts,
        business_calendar_receipts=business_calendar_receipts,
        unit_registry=unit_registry,
        unit_registry_contract_ref=request.unit_registry_contract_ref,
        unit_registry_receipt_sha256=unit_registry_receipt_sha256,
        input_bundle_sha256=content_sha256(payload),
    )


def make_trusted_registry(
    request: CalendarResolutionRequest,
    context: ResolutionContext | None = None,
    *,
    private_key_bytes: bytes = TRUSTED_PRIVATE_KEY,
) -> TrustedResolutionInputRegistry:
    context = make_context() if context is None else context
    source_receipts = tuple(
        sorted(
            {
                request.unit_registry_receipt_sha256,
                *(
                    item.source_receipt_sha256
                    for item
                    in request.calendar_coverage_by_window_rule.values()
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
        )
    )
    bundle_hashes = (request.input_bundle_sha256,)
    registry_ref = "trusted-resolution-inputs:test:v1"
    context_hashes = (content_sha256(context),)
    registry_content_sha256 = content_sha256(
        {
            "registry_ref": registry_ref,
            "issuer_ref": TRUSTED_ISSUER_REF,
            "admitted_input_bundle_sha256s": bundle_hashes,
            "admitted_resolution_context_sha256s": context_hashes,
            "admitted_source_receipt_sha256s": source_receipts,
        }
    )
    signer = TrustedResolutionInputSigner(
        issuer_ref=TRUSTED_ISSUER_REF,
        private_key_bytes=private_key_bytes,
    )
    return TrustedResolutionInputRegistry(
        registry_ref=registry_ref,
        issuer_ref=TRUSTED_ISSUER_REF,
        admitted_input_bundle_sha256s=bundle_hashes,
        admitted_resolution_context_sha256s=context_hashes,
        admitted_source_receipt_sha256s=source_receipts,
        registry_content_sha256=registry_content_sha256,
        issuer_signature_hex=signer.sign_registry_content(
            registry_content_sha256
        ),
    )


def make_trusted_signer(
    *,
    private_key_bytes: bytes = TRUSTED_PRIVATE_KEY,
) -> TrustedResolutionInputSigner:
    return TrustedResolutionInputSigner(
        issuer_ref=TRUSTED_ISSUER_REF,
        private_key_bytes=private_key_bytes,
    )


def make_trusted_verifier() -> TrustedResolutionInputVerifier:
    signer = make_trusted_signer()
    return TrustedResolutionInputVerifier(
        issuer_ref=TRUSTED_ISSUER_REF,
        public_key_bytes=signer.public_key_bytes,
    )


def make_trusted_resolver() -> TrustedMeasurementResolver:
    return TrustedMeasurementResolver(
        make_trusted_verifier(),
        make_trusted_signer(),
    )


_RESOLUTION_CASE_FILES: dict[
    str,
    tuple[
        ResolutionContext,
        CalendarResolutionRequest,
        TrustedResolutionInputRegistry,
        TrustedResolutionInputVerifier,
    ],
] = {}


def resolve_measurement(**kwargs):
    request = kwargs["request"]
    context = kwargs["context"]
    registry = kwargs.setdefault(
        "trusted_input_registry",
        make_trusted_registry(request, context),
    )
    verifier = kwargs.setdefault(
        "trusted_input_verifier",
        make_trusted_verifier(),
    )
    kwargs.pop("trusted_input_verifier", None)
    outcome = make_trusted_resolver().resolve_measurement(**kwargs)
    _RESOLUTION_CASE_FILES[outcome.resolution_outcome_id] = (
        context,
        request,
        registry,
        verifier,
    )
    return outcome


def compile_evidence_obligations(**kwargs):
    outcome = kwargs["outcome"]
    context, request, registry, verifier = _RESOLUTION_CASE_FILES[
        outcome.resolution_outcome_id
    ]
    kwargs.setdefault("context", context)
    kwargs.setdefault("resolution_request", request)
    kwargs.setdefault("trusted_input_registry", registry)
    kwargs.setdefault("trusted_input_verifier", verifier)
    kwargs.pop("trusted_input_verifier", None)
    return make_trusted_resolver().compile_evidence_obligations(**kwargs)


class CalendarResolverPropertyTest(unittest.TestCase):
    def test_business_calendar_selects_versioned_valid_dates(self) -> None:
        design = make_measurement_design()
        rules = tuple(
            replace(
                rule,
                rule_kind=WindowRuleKind.BUSINESS_CALENDAR,
                selection_kind=(
                    WindowSelectionKind.FIRST_N_VALID_BUSINESS_DAYS
                    if index == 0
                    else WindowSelectionKind.LAST_N_VALID_BUSINESS_DAYS
                ),
            )
            for index, rule in enumerate(design.window_rules)
        )
        design = replace(design, window_rules=rules)
        frame = make_frame(measurement_design=design)
        request = make_request(frame, anchor=date(2026, 6, 1))
        business_dates = tuple(
            day
            for month in (5, 6)
            for day_number in range(
                1,
                calendar.monthrange(2026, month)[1] + 1,
            )
            if (
                day := date(2026, month, day_number)
            ).weekday() < 5
        )
        request = rebuild_request(
            request,
            business_calendar_receipts={
                "calendar:gregorian:v1": BusinessCalendarReceipt(
                    calendar_version_ref="calendar:gregorian:v1",
                    holiday_version_ref=None,
                    fiscal_version_ref=None,
                    valid_dates=business_dates,
                    source_receipt_sha256=content_sha256(
                        {
                            "calendar": "calendar:gregorian:v1",
                            "valid_dates": business_dates,
                        }
                    ),
                    inspection_evidence_refs=(
                        "inspection:business-calendar:test",
                    ),
                ),
            },
        )
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=design.estimands[0].estimand_id,
            context=make_context(),
            request=request,
            created_at=NOW,
        )
        left, right = outcome.resolved_instance.windows  # type: ignore[union-attr]
        self.assertEqual(left.actual_start, date(2026, 6, 1))
        self.assertEqual(left.actual_end, date(2026, 6, 9))
        self.assertEqual(left.selected_calendar_dates_count, 7)
        self.assertEqual(left.actual_calendar_days, 9)
        self.assertEqual(right.actual_end, date(2026, 5, 29))
        self.assertEqual(right.selected_calendar_dates_count, 7)
        validate_resolution_identities(outcome)

    def test_business_calendar_exclusive_boundary_changes_selection(
        self,
    ) -> None:
        design = make_measurement_design()
        first, second = design.window_rules
        rules = (
            replace(
                first,
                rule_kind=WindowRuleKind.BUSINESS_CALENDAR,
                selection_kind=(
                    WindowSelectionKind.FIRST_N_VALID_BUSINESS_DAYS
                ),
                start_boundary=IntervalBoundary.EXCLUSIVE,
            ),
            replace(
                second,
                rule_kind=WindowRuleKind.BUSINESS_CALENDAR,
                selection_kind=(
                    WindowSelectionKind.LAST_N_VALID_BUSINESS_DAYS
                ),
            ),
        )
        frame = make_frame(
            measurement_design=replace(design, window_rules=rules)
        )
        request = make_request(frame, anchor=date(2026, 6, 1))
        valid_dates = tuple(
            day
            for month in (5, 6)
            for day_number in range(
                1,
                calendar.monthrange(2026, month)[1] + 1,
            )
            if (
                day := date(2026, month, day_number)
            ).weekday() < 5
        )
        request = rebuild_request(
            request,
            business_calendar_receipts={
                "calendar:gregorian:v1": BusinessCalendarReceipt(
                    calendar_version_ref="calendar:gregorian:v1",
                    holiday_version_ref=None,
                    fiscal_version_ref=None,
                    valid_dates=valid_dates,
                    source_receipt_sha256=content_sha256(valid_dates),
                    inspection_evidence_refs=(
                        "inspection:business-calendar:exclusive",
                    ),
                )
            },
        )
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=request,
            created_at=NOW,
        )
        left = outcome.resolved_instance.windows[0]  # type: ignore[union-attr]
        self.assertEqual(left.actual_start, date(2026, 6, 2))
        self.assertEqual(left.selected_calendar_dates_count, 6)
        validate_resolution_identities(outcome)

    def test_unsupported_fiscal_calendar_stays_a_typed_boundary(self) -> None:
        design = make_measurement_design()
        rules = tuple(
            replace(rule, calendar_unit=CalendarUnit.FISCAL_PERIOD)
            for rule in design.window_rules
        )
        design = replace(design, window_rules=rules)
        frame = make_frame(measurement_design=design)
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=design.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(frame, anchor=date(2026, 6, 1)),
            created_at=NOW,
        )
        self.assertIs(
            outcome.kind,
            ResolutionOutcomeKind.TYPED_RESOLUTION_BOUNDARY,
        )
        self.assertEqual(
            outcome.boundary.boundary_code,  # type: ignore[union-attr]
            ResolutionBoundaryCode.UNSUPPORTED_CALENDAR.value,
        )

    def test_missing_snapshot_receipt_cannot_fabricate_dates(self) -> None:
        frame = make_frame()
        request = make_request(frame, anchor=date(2026, 6, 1))
        first_rule_id = (
            frame.measurement_design.window_rules[0].window_rule_id
        )
        request = rebuild_request(
            request,
            coverage={
                rule_id: receipt
                for rule_id, receipt
                in request.calendar_coverage_by_window_rule.items()
                if rule_id != first_rule_id
            },
        )
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=request,
            created_at=NOW,
        )
        self.assertEqual(
            outcome.boundary.boundary_code,  # type: ignore[union-attr]
            ResolutionBoundaryCode.SNAPSHOT_OUT_OF_RANGE.value,
        )

    def test_month_start_and_prior_month_end_across_calendar_shapes(self) -> None:
        anchors = (
            date(2023, 3, 1),
            date(2024, 3, 1),
            date(2024, 5, 1),
            date(2025, 1, 1),
        )
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                frame = make_frame()
                outcome = resolve_measurement(
                    frame=frame,
                    estimand_id=(
                        frame.measurement_design.estimands[0].estimand_id
                    ),
                    context=make_context(
                        as_of=datetime(2102, 1, 1, tzinfo=UTC),
                    ),
                    request=make_request(frame, anchor=anchor),
                    created_at=NOW,
                )
                self.assertIs(
                    outcome.kind,
                    ResolutionOutcomeKind.RESOLVED_INSTANCE,
                )
                instance = outcome.resolved_instance
                assert instance is not None
                left, right = instance.windows
                self.assertEqual(left.actual_start, anchor)
                self.assertEqual(
                    left.actual_end,
                    anchor + timedelta(days=6),
                )
                prior_year = anchor.year
                prior_month = anchor.month - 1
                if prior_month == 0:
                    prior_year -= 1
                    prior_month = 12
                prior_end = date(
                    prior_year,
                    prior_month,
                    calendar.monthrange(prior_year, prior_month)[1],
                )
                self.assertEqual(right.actual_end, prior_end)
                self.assertEqual(
                    right.actual_start,
                    prior_end - timedelta(days=6),
                )
                self.assertEqual(right.period_offset, -1)
                validate_resolution_against_frame(frame, outcome)

    def test_all_month_lengths_and_leap_years_resolve_without_fixed_days(self) -> None:
        observed_lengths = set()
        for year in range(1999, 2033):
            for month in range(1, 13):
                observed_lengths.add(calendar.monthrange(year, month)[1])
        self.assertEqual(observed_lengths, {28, 29, 30, 31})

        frame = make_frame()
        for year in range(1999, 2033):
            for month in range(1, 13):
                anchor = date(year, month, 1)
                outcome = resolve_measurement(
                    frame=frame,
                    estimand_id=(
                        frame.measurement_design.estimands[0].estimand_id
                    ),
                    context=make_context(
                        as_of=datetime(2102, 1, 1, tzinfo=UTC),
                    ),
                    request=make_request(frame, anchor=anchor),
                    created_at=NOW,
                )
                validate_resolution_against_frame(frame, outcome)
                right = outcome.resolved_instance.windows[1]  # type: ignore[union-attr]
                prior = anchor - timedelta(days=1)
                self.assertEqual(right.actual_end, prior)
                self.assertEqual(
                    right.actual_start,
                    prior - timedelta(days=6),
                )

    def test_dst_duration_uses_utc_half_open_instants(self) -> None:
        design = make_measurement_design()
        first = replace(
            design.window_rules[0],
            rule_kind=WindowRuleKind.ABSOLUTE_INTERVAL,
            period_offset=0,
            selection_kind=WindowSelectionKind.COMPLETE_PERIOD,
            selection_count=None,
            absolute_start=date(2025, 3, 9),
            absolute_end=date(2025, 3, 9),
        )
        second = replace(
            design.window_rules[1],
            rule_kind=WindowRuleKind.ABSOLUTE_INTERVAL,
            period_offset=0,
            selection_kind=WindowSelectionKind.COMPLETE_PERIOD,
            selection_count=None,
            absolute_start=date(2025, 11, 2),
            absolute_end=date(2025, 11, 2),
        )
        design = replace(design, window_rules=(first, second))
        frame = make_frame(measurement_design=design)
        request = make_request(
            frame,
            anchor=date(2025, 3, 1),
            expected="1",
            observed="1",
            valid="1",
        )
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=design.estimands[0].estimand_id,
            context=make_context(
                timezone="America/New_York",
                cutoff="00:00:00",
            ),
            request=request,
            created_at=NOW,
        )
        windows = outcome.resolved_instance.windows  # type: ignore[union-attr]
        self.assertEqual(windows[0].elapsed_seconds, 23 * 3600)
        self.assertEqual(windows[1].elapsed_seconds, 25 * 3600)

    def test_missing_calendar_date_and_short_exposure_fail_closed(self) -> None:
        frame = make_frame()
        request = make_request(frame, anchor=date(2026, 7, 1))
        left_rule_id = frame.measurement_design.window_rules[0].window_rule_id
        receipt = request.calendar_coverage_by_window_rule[left_rule_id]
        shortened = replace(
            receipt,
            observed_dates=receipt.observed_dates[:-1],
            valid_dates=receipt.valid_dates[:-1],
            source_receipt_sha256=content_sha256(
                {"receipt": "missing-final-date"}
            ),
        )
        request = rebuild_request(
            request,
            coverage={
                **request.calendar_coverage_by_window_rule,
                left_rule_id: shortened,
            },
        )
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=request,
            created_at=NOW,
        )
        self.assertIs(
            outcome.kind,
            ResolutionOutcomeKind.TYPED_RESOLUTION_BOUNDARY,
        )
        self.assertEqual(
            outcome.boundary.boundary_code,  # type: ignore[union-attr]
            ResolutionBoundaryCode.INSUFFICIENT_VALID_EXPOSURE.value,
        )

        short_exposure = make_request(
            frame,
            anchor=date(2026, 7, 1),
            expected="7",
            observed="6",
            valid="6",
            invalid="0",
            missing="1",
        )
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=short_exposure,
            created_at=NOW,
        )
        self.assertIs(
            outcome.kind,
            ResolutionOutcomeKind.TYPED_RESOLUTION_BOUNDARY,
        )

    def test_input_bundle_tampering_is_rejected(self) -> None:
        frame = make_frame()
        request = make_request(frame, anchor=date(2026, 7, 1))
        with self.assertRaisesRegex(ValueError, "bundle hash"):
            replace(request, target_period_ref="target-period:forged")

    def test_rehashed_request_is_rejected_by_prior_admission(self) -> None:
        frame = make_frame()
        request = make_request(frame, anchor=date(2026, 7, 1))
        trusted_registry = make_trusted_registry(request)
        forged = rebuild_request(
            request,
            anchor_dates={"anchor:target-month": date(2026, 7, 2)},
        )
        with self.assertRaisesRegex(ValueError, "not admitted"):
            make_trusted_resolver().resolve_measurement(
                frame=frame,
                estimand_id=(
                    frame.measurement_design.estimands[0].estimand_id
                ),
                context=make_context(),
                request=forged,
                trusted_input_registry=trusted_registry,
                created_at=NOW,
            )

    def test_registry_signature_and_context_admission_are_required(
        self,
    ) -> None:
        frame = make_frame()
        request = make_request(frame, anchor=date(2026, 7, 1))
        context = make_context()
        attacker_registry = make_trusted_registry(
            request,
            context,
            private_key_bytes=b"attacker-ed25519-private-key-01!",
        )
        with self.assertRaisesRegex(ValueError, "signature is invalid"):
            make_trusted_resolver().resolve_measurement(
                frame=frame,
                estimand_id=(
                    frame.measurement_design.estimands[0].estimand_id
                ),
                context=context,
                request=request,
                trusted_input_registry=attacker_registry,
                created_at=NOW,
            )
        verifier = make_trusted_verifier()
        self.assertFalse(hasattr(verifier, "sign_resolution_admission"))
        self.assertFalse(hasattr(verifier, "issue_resolution_admission"))
        trusted_registry = make_trusted_registry(request, context)
        with self.assertRaisesRegex(ValueError, "context is not admitted"):
            make_trusted_resolver().resolve_measurement(
                frame=frame,
                estimand_id=(
                    frame.measurement_design.estimands[0].estimand_id
                ),
                context=make_context(
                    timezone="UTC",
                    cutoff="07:00:00",
                ),
                request=request,
                trusted_input_registry=trusted_registry,
                created_at=NOW,
            )

    def test_untrusted_signer_cannot_issue_store_verifiable_receipt(
        self,
    ) -> None:
        frame = make_frame()
        context = make_context()
        request = make_request(frame, anchor=date(2026, 7, 1))
        attacker_signer = make_trusted_signer(
            private_key_bytes=b"attacker-ed25519-private-key-01!"
        )
        attacker_verifier = TrustedResolutionInputVerifier(
            issuer_ref=TRUSTED_ISSUER_REF,
            public_key_bytes=attacker_signer.public_key_bytes,
        )
        registry = make_trusted_registry(
            request,
            context,
            private_key_bytes=b"attacker-ed25519-private-key-01!",
        )
        resolver = TrustedMeasurementResolver(
            attacker_verifier,
            attacker_signer,
        )
        outcome = resolver.resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=context,
            request=request,
            trusted_input_registry=registry,
            created_at=NOW,
        )
        admission = resolver.admit_resolution(
            frame=frame,
            outcome=outcome,
            context=context,
            request=request,
            trusted_input_registry=registry,
        )
        with self.assertRaisesRegex(ValueError, "signature is invalid"):
            make_trusted_verifier().verify_resolution_admission(
                admission=admission,
                outcome=outcome,
            )

    def test_rehashed_boundary_cannot_receive_trusted_admission(
        self,
    ) -> None:
        frame = make_frame()
        context = make_context()
        request = make_request(frame, anchor=date(2026, 7, 1))
        request = rebuild_request(request, coverage={})
        registry = make_trusted_registry(request, context)
        resolver = TrustedMeasurementResolver(
            make_trusted_verifier(),
            make_trusted_signer(),
        )
        outcome = resolver.resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=context,
            request=request,
            trusted_input_registry=registry,
            created_at=NOW,
        )
        admission = resolver.admit_resolution(
            frame=frame,
            outcome=outcome,
            context=context,
            request=request,
            trusted_input_registry=registry,
        )
        boundary = outcome.boundary
        assert boundary is not None
        forged = replace(
            outcome,
            boundary=replace(
                boundary,
                failed_contract_refs=("contract:attacker-unissued",),
                inspection_evidence_refs=(
                    "inspection:attacker-unissued",
                ),
                derivation_proof_sha256="0" * 64,
            ),
        )
        forged = replace(
            forged,
            boundary=replace(
                forged.boundary,
                derivation_proof_sha256=(
                    compute_typed_boundary_derivation_proof(forged)
                ),
            ),
        )
        forged = replace(
            forged,
            resolution_outcome_id=compute_resolution_outcome_id(forged),
        )
        validate_resolution_identities(forged)
        validate_resolution_against_frame(frame, forged)
        with self.assertRaisesRegex(
            ValueError,
            "exact deterministic replay",
        ):
            resolver.admit_resolution(
                frame=frame,
                outcome=forged,
                context=context,
                request=request,
                trusted_input_registry=registry,
            )
        with self.assertRaisesRegex(ValueError, "identity is stale"):
            make_trusted_verifier().verify_resolution_admission(
                admission=admission,
                outcome=forged,
            )

    def test_target_anchor_must_fit_declared_target_period(self) -> None:
        frame = make_frame()
        request = make_request(frame, anchor=date(2026, 7, 1))
        with self.assertRaisesRegex(ValueError, "target anchor"):
            rebuild_request(
                request,
                anchor_dates={
                    "anchor:target-month": date(2026, 6, 30),
                },
            )

    def test_future_window_and_unsettled_release_fail_closed(self) -> None:
        frame = make_frame()
        future = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(frame, anchor=date(2027, 1, 1)),
            created_at=NOW,
        )
        self.assertEqual(
            future.boundary.boundary_code,  # type: ignore[union-attr]
            ResolutionBoundaryCode.SNAPSHOT_OUT_OF_RANGE.value,
        )

        request = make_request(frame, anchor=date(2026, 7, 1))
        first_rule = frame.measurement_design.window_rules[0]
        receipt = request.calendar_coverage_by_window_rule[
            first_rule.window_rule_id
        ]
        unsettled = replace(
            receipt,
            coverage_complete_through=date(2026, 7, 6),
            source_receipt_sha256=content_sha256(
                {
                    "coverage_complete_through": date(2026, 7, 6),
                    "rule": first_rule.window_rule_id,
                }
            ),
        )
        request = rebuild_request(
            request,
            coverage={
                **request.calendar_coverage_by_window_rule,
                first_rule.window_rule_id: unsettled,
            },
        )
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=request,
            created_at=NOW,
        )
        self.assertEqual(
            outcome.boundary.boundary_code,  # type: ignore[union-attr]
            ResolutionBoundaryCode.SNAPSHOT_OUT_OF_RANGE.value,
        )

    def test_resolved_instants_cannot_be_rehashed_after_tampering(
        self,
    ) -> None:
        frame = make_frame()
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(frame, anchor=date(2026, 7, 1)),
            created_at=NOW,
        )
        instance = outcome.resolved_instance
        assert instance is not None
        forged_window = replace(
            instance.windows[0],
            start_instant=instance.windows[0].start_instant
            + timedelta(hours=1),
            elapsed_seconds=instance.windows[0].elapsed_seconds - 3600,
        )
        forged_instance = replace(
            instance,
            windows=(forged_window, *instance.windows[1:]),
            resolution_id="0" * 64,
        )
        forged_instance = replace(
            forged_instance,
            resolution_id=compute_resolution_id(forged_instance),
        )
        forged_outcome = replace(
            outcome,
            resolved_instance=forged_instance,
            resolution_outcome_id="0" * 64,
        )
        forged_outcome = replace(
            forged_outcome,
            resolution_outcome_id=compute_resolution_outcome_id(
                forged_outcome
            ),
        )
        validate_resolution_identities(forged_outcome)
        with self.assertRaisesRegex(ValueError, "instants"):
            validate_resolution_against_frame(frame, forged_outcome)

    def test_rolling_and_exclusive_rules_preserve_selected_dates(self) -> None:
        design = make_measurement_design()
        rolling_rules = tuple(
            replace(
                rule,
                rule_kind=WindowRuleKind.ROLLING_INTERVAL,
                selection_kind=WindowSelectionKind.ROLLING_LENGTH,
                start_boundary=IntervalBoundary.INCLUSIVE,
                end_boundary=IntervalBoundary.INCLUSIVE,
            )
            for rule in design.window_rules
        )
        rolling = replace(design, window_rules=rolling_rules)
        frame = make_frame(measurement_design=rolling)
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=rolling.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(
                frame,
                anchor=date(2024, 3, 31),
            ),
            created_at=NOW,
        )
        left, right = outcome.resolved_instance.windows  # type: ignore[union-attr]
        self.assertEqual(left.actual_end, date(2024, 3, 31))
        self.assertEqual(right.actual_end, date(2024, 2, 29))
        self.assertEqual(left.selected_calendar_dates_count, 7)
        self.assertEqual(right.selected_calendar_dates_count, 7)

        exclusive_rules = (
            replace(
                design.window_rules[0],
                start_boundary=IntervalBoundary.EXCLUSIVE,
            ),
            design.window_rules[1],
        )
        exclusive = replace(design, window_rules=exclusive_rules)
        frame = make_frame(measurement_design=exclusive)
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=exclusive.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(frame, anchor=date(2026, 7, 1)),
            created_at=NOW,
        )
        left = outcome.resolved_instance.windows[0]  # type: ignore[union-attr]
        self.assertEqual(left.actual_start, date(2026, 7, 2))
        self.assertEqual(left.selected_calendar_dates_count, 6)

    def test_rehashed_same_month_window_is_rejected_as_authority_drift(
        self,
    ) -> None:
        frame = make_frame()
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(frame, anchor=date(2026, 7, 1)),
            created_at=NOW,
        )
        instance = outcome.resolved_instance
        assert instance is not None
        right = instance.windows[1]
        forged_dates = tuple(
            date(2026, 7, day) for day in range(8, 15)
        )
        forged = replace(
            right,
            actual_start=forged_dates[0],
            actual_end=forged_dates[-1],
            start_instant=datetime(2026, 7, 8, 4, tzinfo=UTC),
            end_instant=datetime(2026, 7, 15, 4, tzinfo=UTC),
            selected_calendar_dates_sha256=content_sha256(forged_dates),
        )
        forged_instance = replace(
            instance,
            windows=(instance.windows[0], forged),
            field_derivation_proof_sha256=SHA_B,
        )
        forged_outcome = replace(
            outcome,
            resolved_instance=forged_instance,
        )
        with self.assertRaisesRegex(ValueError, "Frame window"):
            validate_resolution_against_frame(frame, forged_outcome)


class MeasurementAlgebraTest(unittest.TestCase):
    def test_every_claim_target_has_an_explicit_validation_contract(
        self,
    ) -> None:
        contracts = claim_target_validation_contracts()
        self.assertEqual(
            {item.claim_target_kind for item in contracts},
            set(ClaimTargetKind),
        )
        causal = next(
            item
            for item in contracts
            if item.claim_target_kind is ClaimTargetKind.CAUSAL_EFFECT
        )
        self.assertIn("relationship_id", causal.required_estimand_fields)
        self.assertIn(
            "identification_id",
            causal.required_estimand_fields,
        )
        estimand = make_measurement_design().estimands[0]
        with self.assertRaisesRegex(
            TypeError,
            "claim_target_spec",
        ):
            replace(
                estimand,
                claim_target_kind=ClaimTargetKind.POINT_QUANTITY,
            )

    def test_at_risk_exposure_cannot_exceed_expected_population(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "at-risk exposure cannot exceed expected",
        ):
            ExposureCoverageFact(
                window_rule_id="window-risk",
                exposure_id="exposure-risk",
                basis=ExposureBasis.AT_RISK,
                unit_ref="unit:eligible-user",
                expected_exposure_decimal="100",
                observed_exposure_decimal="100",
                valid_exposure_decimal="100",
                invalid_exposure_decimal="0",
                missing_exposure_decimal="0",
                at_risk_exposure_decimal="101",
                source_kind=ExposureFactSourceKind.SNAPSHOT_CATALOG,
                source_receipt_sha256=SHA_A,
            )

    def test_exposure_normalization_and_unit_proof(self) -> None:
        frame = make_frame()
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(frame, anchor=date(2026, 1, 1)),
            created_at=NOW,
        )
        exposure = frame.measurement_design.exposures[0]
        registry = {
            "currency:CNY": UnitExpression(
                unit_ref="currency:CNY",
                powers=(UnitPower("money", 1),),
                currency_code="CNY",
                scale_decimal="1",
                conversion_version_ref="unit-registry:v1",
            ),
            "unit:valid-observed-day": UnitExpression(
                unit_ref="unit:valid-observed-day",
                powers=(UnitPower("day", 1),),
                currency_code=None,
                scale_decimal="1",
                conversion_version_ref="unit-registry:v1",
            ),
            "currency:CNY-per-valid-observed-day": UnitExpression(
                unit_ref="currency:CNY-per-valid-observed-day",
                powers=(
                    UnitPower("day", -1),
                    UnitPower("money", 1),
                ),
                currency_code="CNY",
                scale_decimal="1",
                conversion_version_ref="unit-registry:v1",
            ),
        }
        window = outcome.resolved_instance.windows[0]  # type: ignore[union-attr]
        estimate = comparable_estimate(
            numerator_decimal="700",
            window=window,
            exposure=exposure,
            numerator_unit_ref="currency:CNY",
            output_unit_ref=(
                "currency:CNY-per-valid-observed-day"
            ),
            unit_registry=registry,
        )
        self.assertEqual(estimate.value_decimal, "100")
        self.assertTrue(estimate.normalized)

        with self.assertRaisesRegex(ValueError, "incompatible_unit"):
            comparable_estimate(
                numerator_decimal="700",
                window=window,
                exposure=exposure,
                numerator_unit_ref="unit:valid-observed-day",
                output_unit_ref=(
                    "currency:CNY-per-valid-observed-day"
                ),
                unit_registry=registry,
            )

    def test_exposure_threshold_is_independent_of_eligibility(self) -> None:
        design = make_measurement_design()
        exposure = replace(
            design.exposures[0],
            minimum_coverage_ratio="0.95",
        )
        eligibility = replace(
            design.eligibilities[0],
            completeness_policy=CompletenessPolicy.DEGRADE_INCOMPLETE,
            minimum_coverage_ratio="0.8",
        )
        design = replace(
            design,
            exposures=(exposure,),
            eligibilities=(eligibility,),
        )
        frame = make_frame(measurement_design=design)
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=design.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(
                frame,
                anchor=date(2026, 7, 1),
                expected="10",
                observed="9",
                valid="9",
                invalid="0",
                missing="1",
            ),
            created_at=NOW,
        )
        self.assertEqual(
            outcome.boundary.boundary_code,  # type: ignore[union-attr]
            ResolutionBoundaryCode.INSUFFICIENT_VALID_EXPOSURE.value,
        )

    def test_partial_period_can_execute_only_under_explicit_exposure_policy(
        self,
    ) -> None:
        design = make_measurement_design()
        design = replace(
            design,
            exposures=(
                replace(
                    design.exposures[0],
                    minimum_coverage_ratio="0.5",
                ),
            ),
            eligibilities=(
                replace(
                    design.eligibilities[0],
                    completeness_policy=(
                        CompletenessPolicy.ALLOW_PARTIAL_WITH_EXPOSURE
                    ),
                    minimum_coverage_ratio="0.5",
                ),
            ),
        )
        frame = make_frame(measurement_design=design)
        request = make_request(
            frame,
            anchor=date(2026, 7, 1),
            expected="7",
            observed="6",
            valid="6",
            invalid="0",
            missing="1",
        )
        coverage = {
            rule_id: replace(
                receipt,
                observed_dates=receipt.observed_dates[:-1],
                valid_dates=receipt.valid_dates[:-1],
                source_receipt_sha256=content_sha256(
                    {"partial-window": rule_id}
                ),
            )
            for rule_id, receipt
            in request.calendar_coverage_by_window_rule.items()
        }
        request = rebuild_request(request, coverage=coverage)
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=design.estimands[0].estimand_id,
            context=make_context(),
            request=request,
            created_at=NOW,
        )
        self.assertIs(
            outcome.kind,
            ResolutionOutcomeKind.RESOLVED_INSTANCE,
        )
        self.assertTrue(
            all(
                window.observed_calendar_dates_count == 6
                for window in outcome.resolved_instance.windows
            )
        )

    def test_exposure_identity_is_single_across_estimator_and_estimand(
        self,
    ) -> None:
        design = make_measurement_design()
        alternate = replace(
            design.exposures[0],
            exposure_id="exposure-alternate-calendar",
            basis=ExposureBasis.CALENDAR,
            unit_ref="calendar:business-day",
            comparability_rule_ref="comparability:calendar-day:v1",
        )
        estimator = replace(
            design.estimators[0],
            exposure_id=alternate.exposure_id,
        )
        design = replace(
            design,
            exposures=(*design.exposures, alternate),
            estimators=(estimator,),
        )
        findings = validate_executable_design(design)
        self.assertIn(
            "estimator_exposure_mismatch",
            {item.code for item in findings},
        )

    def test_metric_output_unit_is_derived_from_input_variables(
        self,
    ) -> None:
        design = make_measurement_design()
        metric = replace(
            design.metric_expressions[0],
            numerator_variable_ids=("variable-calendar-day",),
        )
        design = replace(design, metric_expressions=(metric,))
        frame = make_frame(measurement_design=design)
        request = make_request(frame, anchor=date(2026, 7, 1))
        findings = validate_executable_design(
            design,
            unit_registry=request.unit_registry,
        )
        self.assertIn(
            "metric_variable_unit_mismatch",
            {item.code for item in findings},
        )
        with self.assertRaisesRegex(
            ValueError,
            "invalid accepted measurement graph",
        ):
            resolve_measurement(
                frame=frame,
                estimand_id=design.estimands[0].estimand_id,
                context=make_context(),
                request=request,
                created_at=NOW,
            )

    def test_frame_unit_algebra_is_validated_before_resolution(
        self,
    ) -> None:
        design = make_measurement_design()
        scope = replace(
            design.scopes[0],
            unit_ref="currency:CNY",
        )
        design = replace(design, scopes=(scope,))
        frame = make_frame(measurement_design=design)
        request = make_request(frame, anchor=date(2026, 7, 1))
        findings = validate_executable_design(
            design,
            unit_registry=request.unit_registry,
        )
        self.assertIn(
            "incompatible_unit_algebra",
            {item.code for item in findings},
        )
        with self.assertRaisesRegex(
            ValueError,
            "invalid accepted measurement graph",
        ):
            resolve_measurement(
                frame=frame,
                estimand_id=design.estimands[0].estimand_id,
                context=make_context(),
                request=request,
                created_at=NOW,
            )

    def test_aggregation_order_and_exposure_policies_are_executable(
        self,
    ) -> None:
        frame = make_frame()
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(frame, anchor=date(2026, 7, 1)),
            created_at=NOW,
        )
        window = outcome.resolved_instance.windows[0]  # type: ignore[union-attr]
        registry = make_request(
            frame,
            anchor=date(2026, 7, 1),
        ).unit_registry
        base = frame.measurement_design.exposures[0]
        mean_of_ratios = replace(
            base,
            aggregation_order=AggregationOrder.MEAN_OF_RATIOS,
        )
        mean = comparable_estimate(
            numerator_decimal="700",
            window=window,
            exposure=mean_of_ratios,
            numerator_unit_ref="currency:CNY",
            output_unit_ref=(
                "currency:CNY-per-valid-observed-day"
            ),
            unit_registry=registry,
            numerator_components_decimal=("200", "500"),
            exposure_components_decimal=("1", "6"),
        )
        self.assertEqual(
            mean.value_decimal,
            "141.6666666666666666666666666",
        )

        weighted = replace(
            base,
            normalization=ExposureNormalization.WEIGHTED_BY_EXPOSURE,
            aggregation_order=AggregationOrder.WEIGHTED_MEAN,
        )
        weighted_estimate = comparable_estimate(
            numerator_decimal="700",
            window=window,
            exposure=weighted,
            numerator_unit_ref="currency:CNY",
            output_unit_ref=(
                "currency:CNY-per-valid-observed-day"
            ),
            unit_registry=registry,
            numerator_components_decimal=("200", "500"),
            exposure_components_decimal=("1", "6"),
            weight_components_decimal=("1", "3"),
        )
        self.assertEqual(weighted_estimate.value_decimal, "112.5")

        excluding_zero = replace(
            mean_of_ratios,
            zero_policy=MissingExposurePolicy.EXCLUDE,
        )
        excluded = comparable_estimate(
            numerator_decimal="700",
            window=window,
            exposure=excluding_zero,
            numerator_unit_ref="currency:CNY",
            output_unit_ref=(
                "currency:CNY-per-valid-observed-day"
            ),
            unit_registry=registry,
            numerator_components_decimal=("0", "700"),
            exposure_components_decimal=("0", "7"),
        )
        self.assertEqual(excluded.value_decimal, "100")
        self.assertTrue(excluded.degraded)
        self.assertIn("zero_exposure:exclude", excluded.limitation_codes)

        incomplete_fact = replace(
            window.exposure_facts[0],
            observed_exposure_decimal="6",
            valid_exposure_decimal="6",
            invalid_exposure_decimal="0",
            missing_exposure_decimal="1",
            coverage_ratio_decimal="0.8571428571428571428571428571",
        )
        incomplete_window = replace(
            window,
            exposure_facts=(incomplete_fact,),
        )
        degraded = comparable_estimate(
            numerator_decimal="600",
            window=incomplete_window,
            exposure=base,
            numerator_unit_ref="currency:CNY",
            output_unit_ref=(
                "currency:CNY-per-valid-observed-day"
            ),
            unit_registry=registry,
        )
        self.assertTrue(degraded.degraded)
        self.assertIn(
            "missing_exposure:degrade",
            degraded.limitation_codes,
        )
        with self.assertRaisesRegex(ValueError, "missing exposure"):
            comparable_estimate(
                numerator_decimal="600",
                window=incomplete_window,
                exposure=replace(
                    base,
                    missing_policy=MissingExposurePolicy.BLOCK,
                ),
                numerator_unit_ref="currency:CNY",
                output_unit_ref=(
                    "currency:CNY-per-valid-observed-day"
                ),
                unit_registry=registry,
            )

    def test_compiler_rejects_rehashed_outcome_from_wrong_authority(
        self,
    ) -> None:
        frame = make_frame()
        context = make_context()
        request = make_request(frame, anchor=date(2026, 7, 1))
        registry = make_trusted_registry(request)
        outcome = make_trusted_resolver().resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=context,
            request=request,
            trusted_input_registry=registry,
            created_at=NOW,
        )
        forged = replace(
            outcome,
            case_id="case-forged",
            resolution_outcome_id="0" * 64,
        )
        forged = replace(
            forged,
            resolution_outcome_id=compute_resolution_outcome_id(forged),
        )
        with self.assertRaisesRegex(ValueError, "reproduced"):
            make_trusted_resolver().compile_evidence_obligations(
                frame=frame,
                outcome=forged,
                context=context,
                resolution_request=request,
                trusted_input_registry=registry,
                created_at=NOW,
            )

    def test_raw_total_cannot_claim_direction_with_unequal_exposure(self) -> None:
        frame = make_frame()
        left_outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(frame, anchor=date(2026, 1, 1)),
            created_at=NOW,
        )
        windows = left_outcome.resolved_instance.windows  # type: ignore[union-attr]
        unequal_fact = replace(
            windows[1].exposure_facts[0],
            expected_exposure_decimal="6",
            observed_exposure_decimal="6",
            valid_exposure_decimal="6",
            coverage_ratio_decimal="1",
        )
        unequal_window = replace(
            windows[1],
            exposure_facts=(unequal_fact,),
        )
        raw_exposure = replace(
            frame.measurement_design.exposures[0],
            normalization=ExposureNormalization.NONE,
        )
        with self.assertRaisesRegex(
            ValueError,
            ResolutionBoundaryCode.INCOMPARABLE_EXPOSURE.value,
        ):
            assert_contrast_comparable(
                estimates=(
                    _raw_estimate("700"),
                    _raw_estimate("650"),
                ),
                windows=(windows[0], unequal_window),
                exposure=raw_exposure,
            )

    def test_raw_total_unequal_exposure_blocks_executable_obligation(
        self,
    ) -> None:
        design = make_measurement_design()
        exposure = replace(
            design.exposures[0],
            normalization=ExposureNormalization.NONE,
        )
        scope = replace(
            design.scopes[0],
            unit_ref="currency:CNY",
        )
        design = replace(
            design,
            exposures=(exposure,),
            scopes=(scope,),
        )
        frame = make_frame(measurement_design=design)
        request = make_request(frame, anchor=date(2026, 7, 1))
        right_rule_id = design.window_rules[1].window_rule_id
        unequal_facts = tuple(
            replace(
                fact,
                expected_exposure_decimal="6",
                observed_exposure_decimal="6",
                valid_exposure_decimal="6",
                invalid_exposure_decimal="0",
                missing_exposure_decimal="0",
                source_receipt_sha256=content_sha256(
                    {"unequal-exposure": right_rule_id}
                ),
            )
            if fact.window_rule_id == right_rule_id
            else fact
            for fact in request.exposure_facts
        )
        request = rebuild_request(
            request,
            exposure_facts=unequal_facts,
        )
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=design.estimands[0].estimand_id,
            context=make_context(),
            request=request,
            created_at=NOW,
        )
        self.assertIs(
            outcome.kind,
            ResolutionOutcomeKind.TYPED_RESOLUTION_BOUNDARY,
        )
        self.assertEqual(
            outcome.boundary.boundary_code,
            ResolutionBoundaryCode.INCOMPARABLE_EXPOSURE.value,
        )
        obligations = compile_evidence_obligations(
            frame=frame,
            outcome=outcome,
            created_at=NOW,
        )
        self.assertIs(
            obligations[0].execution_disposition,
            ObligationExecutionDisposition.BLOCKED,
        )

    def test_compiler_is_deterministic_and_keeps_fulfillment_out(self) -> None:
        frame = make_frame()
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=frame.measurement_design.estimands[0].estimand_id,
            context=make_context(),
            request=make_request(frame, anchor=date(2026, 1, 1)),
            created_at=NOW,
        )
        first = compile_evidence_obligations(
            frame=frame,
            outcome=outcome,
            created_at=NOW,
        )
        second = compile_evidence_obligations(
            frame=frame,
            outcome=outcome,
            created_at=NOW,
        )
        self.assertEqual(first, second)
        self.assertNotIn(
            "satisfaction",
            first[0].__dataclass_fields__,
        )

    def test_requirement_with_independent_measurement_needs_own_estimand(
        self,
    ) -> None:
        design = make_measurement_design()
        original = design.evidence_requirements[0]
        independent = replace(
            original,
            evidence_requirement_id="requirement-independent-dq",
            exposure_id=None,
            boundary_policy=RequirementBoundaryPolicy.BLOCK,
            allowed_boundary_codes=(),
        )
        estimand = replace(
            design.estimands[0],
            evidence_requirement_ids=(
                original.evidence_requirement_id,
                independent.evidence_requirement_id,
            ),
        )
        completion = replace(
            design.completion_specs[0],
            required_evidence_requirement_ids=(
                original.evidence_requirement_id,
                independent.evidence_requirement_id,
            ),
        )
        design = replace(
            design,
            evidence_requirements=(original, independent),
            estimands=(estimand,),
            completion_specs=(completion,),
        )
        frame = make_frame(measurement_design=design)
        findings = validate_executable_design(design)
        self.assertIn(
            "evidence_requirement_measurement_mismatch",
            {item.code for item in findings},
        )
        with self.assertRaisesRegex(
            ValueError,
            "invalid accepted measurement graph",
        ):
            resolve_measurement(
                frame=frame,
                estimand_id=estimand.estimand_id,
                context=make_context(),
                request=make_request(
                    frame,
                    anchor=date(2026, 7, 1),
                    expected="7",
                    observed="6",
                    valid="6",
                    invalid="0",
                    missing="1",
                ),
                created_at=NOW,
            )

    def test_requirement_target_and_estimand_reference_are_bidirectional(
        self,
    ) -> None:
        design = make_measurement_design()
        original_estimand = design.estimands[0]
        other_estimand = replace(
            original_estimand,
            estimand_id="estimand-other",
        )
        requirement = replace(
            design.evidence_requirements[0],
            target_estimand_ids=(other_estimand.estimand_id,),
        )
        completion = replace(
            design.completion_specs[0],
            target_estimand_ids=(
                original_estimand.estimand_id,
                other_estimand.estimand_id,
            ),
        )
        with self.assertRaisesRegex(ValueError, "must target each other"):
            replace(
                design,
                evidence_requirements=(requirement,),
                estimands=(original_estimand, other_estimand),
                completion_specs=(completion,),
            )

    def test_block_policy_cannot_treat_resolution_gap_as_satisfied(
        self,
    ) -> None:
        design = make_measurement_design()
        requirement = replace(
            design.evidence_requirements[0],
            boundary_policy=RequirementBoundaryPolicy.BLOCK,
            allowed_boundary_codes=(),
        )
        design = replace(
            design,
            evidence_requirements=(requirement,),
        )
        frame = make_frame(measurement_design=design)
        request = make_request(frame, anchor=date(2026, 7, 1))
        request = rebuild_request(request, coverage={})
        outcome = resolve_measurement(
            frame=frame,
            estimand_id=design.estimands[0].estimand_id,
            context=make_context(),
            request=request,
            created_at=NOW,
        )
        compiled = compile_evidence_obligations(
            frame=frame,
            outcome=outcome,
            created_at=NOW,
        )
        self.assertIs(
            compiled[0].execution_disposition,
            ObligationExecutionDisposition.BLOCKED,
        )

    def test_invalid_graph_is_rejected_before_resolution(self) -> None:
        design = make_measurement_design()
        invalid = replace(
            design,
            estimands=(
                replace(design.estimands[0], variable_ids=()),
            ),
        )
        findings = validate_executable_design(invalid)
        self.assertIn(
            "missing_target_variables",
            {item.code for item in findings},
        )


def _raw_estimate(value: str):
    from waje_vnext.domain.measurement_resolver import ComparableEstimate

    return ComparableEstimate(
        numerator_decimal=value,
        exposure_decimal="1",
        value_decimal=value,
        output_unit_ref="currency:CNY",
        normalized=False,
        normalization=ExposureNormalization.NONE,
        aggregation_order=AggregationOrder.SUM,
        contributing_component_count=1,
        degraded=False,
        limitation_codes=(),
        unit_proof_sha256=SHA_A,
    )


if __name__ == "__main__":
    unittest.main()
