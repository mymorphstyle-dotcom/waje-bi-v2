from dataclasses import replace
import unittest
from unittest.mock import patch

from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    CompletenessReport,
    QueryResultEnvelope,
)
from bi_agent.runtime.capability_execution import (
    BoundCapabilityInput,
    bind_capability_inputs as _bind_capability_inputs,
    validate_bound_capability_input,
)


def bind_capability_inputs(*args, **kwargs):
    kwargs.setdefault("run_mode", "fixture")
    with patch.dict(
        "os.environ",
        {
            "WAJE_ALLOW_LEGACY_FIXTURES": "1",
            "WAJE_RUNTIME_ENV": "test",
        },
    ):
        return _bind_capability_inputs(*args, **kwargs)


def slot(
    slot_id="joint_candidates",
    query_ref="query:joint:1",
    *,
    required=True,
    validation_refs=(),
):
    return CapabilityInputSlot(
        slot_id=slot_id,
        query_contract_refs=(query_ref,),
        required=required,
        accepted_completeness=("complete",),
        required_fields=("period", "amount"),
        required_window_ids=("target_day",),
        validation_query_contract_refs=tuple(validation_refs),
    )


def plan(*, required_slots=(), optional_slots=()):
    return CapabilityExecutionPlan(
        capability_id="joint_attribution",
        capability_contract_ref="capability:joint@1",
        required_input_slots=tuple(required_slots),
        optional_input_slots=tuple(optional_slots),
        merge_strategy="by_query_family",
        minimum_readiness={
            "required_slots": "all" if required_slots else "none",
            "accepted_completeness": ("complete",),
        },
        degradation_policy={
            "missing_optional_input": "omit_optional_component",
            "incomplete_input": "degrade_claim",
        },
        supported_evidence_types=("accounting_contribution",),
        maximum_claim_strength="high",
    )


def result(query_ref, *, result_ref=None, snapshots=("snapshot:paid:1",)):
    suffix = query_ref.rsplit(":", 1)[-1]
    return QueryResultEnvelope(
        query_contract_ref=query_ref,
        query_id=f"provider:{suffix}",
        query_hash=f"hash:{suffix}",
        result_ref=result_ref or f"result:{suffix}",
        execution_status="succeeded",
        rows_ref=f"rows:{suffix}",
        row_count=1,
        completeness_report_ref=f"complete:{suffix}",
        rows=({"period": "2026-06-02", "amount": 10.0},),
        observed_schema={"period": "Date", "amount": "Float64"},
        observed_windows=("target_day",),
        observed_grain=("target_day",),
        source_snapshot_refs=tuple(snapshots),
        execution_attempt_ref=f"attempt:{suffix}",
    )


def report(
    query_ref,
    *,
    result_ref=None,
    report_ref=None,
    coverage=None,
    assertions=None,
):
    suffix = query_ref.rsplit(":", 1)[-1]
    return CompletenessReport(
        report_ref=report_ref or f"complete:{suffix}",
        query_contract_ref=query_ref,
        result_ref=result_ref or f"result:{suffix}",
        completeness_status="complete",
        analysis_readiness="ready",
        assertion_results=tuple(
            assertions
            if assertions is not None
            else (
                {
                    "assertion": "execution_succeeded",
                    "passed": True,
                    "failure_reasons": (),
                    "details": {},
                },
            )
        ),
        failure_reasons=(),
        coverage_summary=dict(
            coverage
            or {
                "row_count": 1,
                "required_windows": ("target_day",),
                "observed_windows": ("target_day",),
                "snapshot_ref": "snapshot:paid:1",
            }
        ),
    )


class CapabilityExecutionTest(unittest.TestCase):
    def test_production_binder_rejects_caller_signed_maps_without_authority(self):
        bound = _bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": result("query:joint:1")},
            reports={"query:joint:1": report("query:joint:1")},
        )

        self.assertEqual(bound.status, "blocked")
        self.assertEqual(bound.reasons, ("runtime_evidence_authority_missing",))
        self.assertEqual(bound.binding_manifest_ref, "")

    def test_bound_rows_are_recursively_immutable_and_manifest_verified(self):
        primary = replace(
            result("query:joint:1"),
            rows=(
                {
                    "period": "2026-06-02",
                    "amount": 10.0,
                    "nested": {"items": ["A"]},
                },
            ),
        )
        bound = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": primary},
            reports={"query:joint:1": report("query:joint:1")},
        )

        self.assertEqual(
            validate_bound_capability_input(bound, allow_fixture=True),
            "",
        )
        with self.assertRaises((AttributeError, TypeError)):
            bound.rows_by_slot["joint_candidates"][0]["nested"]["items"].append("B")
        with self.assertRaises(TypeError):
            BoundCapabilityInput(**bound.__dict__)
        self.assertEqual(
            validate_bound_capability_input(object.__new__(BoundCapabilityInput)),
            "bound_capability_input_factory_required",
        )

        object.__setattr__(bound, "binding_manifest_digest", "forged")
        self.assertEqual(
            validate_bound_capability_input(bound),
            "binding_manifest_digest_mismatch",
        )

    def test_primary_result_refs_must_be_unique_across_slots(self):
        first = result("query:first:1", result_ref="result:shared")
        second = result("query:second:1", result_ref="result:shared")
        bound = bind_capability_inputs(
            plan(
                required_slots=(
                    slot("first", "query:first:1"),
                    slot("second", "query:second:1"),
                )
            ),
            results={"query:first:1": first, "query:second:1": second},
            reports={
                "query:first:1": report(
                    "query:first:1", result_ref="result:shared"
                ),
                "query:second:1": report(
                    "query:second:1", result_ref="result:shared"
                ),
            },
        )

        self.assertEqual(bound.status, "blocked")
        self.assertIn("duplicate_primary_result_ref:result:shared", bound.reasons)

    def test_joint_attribution_rejects_unbound_daily_rows(self):
        bound = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:daily:1": result("query:daily:1")},
            reports={"query:daily:1": report("query:daily:1")},
        )

        self.assertEqual(bound.status, "blocked")
        self.assertEqual(bound.reasons, ("missing_required_slot:joint_candidates",))
        self.assertEqual(bound.rows_by_slot, {})

    def test_complete_exact_slot_preserves_all_provenance(self):
        bound = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": result("query:joint:1")},
            reports={"query:joint:1": report("query:joint:1")},
        )

        self.assertEqual(bound.status, "ready")
        self.assertEqual(bound.query_contract_refs, ("query:joint:1",))
        self.assertEqual(bound.result_refs, ("result:1",))
        self.assertEqual(bound.completeness_report_refs, ("complete:1",))
        self.assertEqual(bound.source_snapshot_refs, ("snapshot:paid:1",))

    def test_optional_slot_can_degrade_without_replacing_required_rows(self):
        required = slot("component_drivers", "query:components:1")
        optional = slot(
            "payment_success", "query:success:1", required=False
        )
        bound = bind_capability_inputs(
            plan(required_slots=(required,), optional_slots=(optional,)),
            results={"query:components:1": result("query:components:1")},
            reports={"query:components:1": report("query:components:1")},
        )

        self.assertEqual(bound.status, "degraded")
        self.assertIn("missing_optional_slot:payment_success", bound.reasons)
        self.assertEqual(tuple(bound.rows_by_slot), ("component_drivers",))

    def test_primary_result_report_and_mapping_refs_must_link_exactly(self):
        mismatched = result("query:joint:1")
        bound = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": mismatched},
            reports={
                "query:joint:1": report(
                    "query:joint:1", result_ref="result:other"
                )
            },
        )

        self.assertEqual(bound.status, "blocked")
        self.assertEqual(
            bound.reasons,
            ("primary_provenance_mismatch:joint_candidates",),
        )

    def test_multi_snapshot_report_requires_exact_snapshot_refs(self):
        primary = result(
            "query:joint:1",
            snapshots=("snapshot:paid:1", "snapshot:dashboard:1"),
        )
        legacy_single = report("query:joint:1")
        exact = replace(
            legacy_single,
            coverage_summary={
                **legacy_single.coverage_summary,
                "snapshot_refs": primary.source_snapshot_refs,
            },
        )

        blocked = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": primary},
            reports={"query:joint:1": legacy_single},
        )
        ready = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": primary},
            reports={"query:joint:1": exact},
        )

        self.assertEqual(blocked.status, "blocked")
        self.assertIn("primary_snapshot_provenance_mismatch", blocked.reasons[0])
        self.assertEqual(ready.status, "ready")

    def test_dimension_slot_requires_exact_reconciliation_dependency(self):
        dimension = slot(
            "dimension_contribution_scan:channel",
            "query:channel:1",
            validation_refs=("query:channel-total:1",),
        )
        bound = bind_capability_inputs(
            plan(required_slots=(dimension,)),
            results={"query:channel:1": result("query:channel:1")},
            reports={"query:channel:1": report("query:channel:1")},
        )

        self.assertEqual(bound.status, "blocked")
        self.assertEqual(
            bound.reasons,
            ("missing_validation_query:dimension_contribution_scan:channel",),
        )

    def test_optional_validation_failure_degrades_without_replacing_required_rows(self):
        required = slot("component_drivers", "query:components:1")
        optional = slot(
            "payment_success",
            "query:success:1",
            required=False,
            validation_refs=("query:success-total:1",),
        )
        bound = bind_capability_inputs(
            plan(required_slots=(required,), optional_slots=(optional,)),
            results={
                "query:components:1": result("query:components:1"),
                "query:success:1": result("query:success:1"),
            },
            reports={
                "query:components:1": report("query:components:1"),
                "query:success:1": report("query:success:1"),
            },
        )

        self.assertEqual(bound.status, "degraded")
        self.assertEqual(
            bound.reasons,
            ("missing_validation_query:payment_success",),
        )
        self.assertEqual(tuple(bound.rows_by_slot), ("component_drivers",))

    def test_optional_failure_honors_contract_block_policy(self):
        optional = slot(
            "payment_success", "query:success:1", required=False
        )
        strict_plan = replace(
            plan(optional_slots=(optional,)),
            degradation_policy={"missing_optional_input": "block_claim"},
        )

        bound = bind_capability_inputs(strict_plan, results={}, reports={})

        self.assertEqual(bound.status, "blocked")

    def test_missing_or_unknown_optional_policy_blocks(self):
        optional = slot("payment_success", "query:success:1", required=False)
        for degradation_policy in (
            {},
            {"missing_optional_input": "invented_action"},
        ):
            with self.subTest(degradation_policy=degradation_policy):
                bound = bind_capability_inputs(
                    replace(
                        plan(optional_slots=(optional,)),
                        degradation_policy=degradation_policy,
                    ),
                    results={},
                    reports={},
                )
                self.assertEqual(bound.status, "blocked")

    def test_required_slot_mode_mismatch_blocks_plan(self):
        mismatched = replace(
            plan(required_slots=(slot(),)),
            minimum_readiness={
                "required_slots": "none",
                "accepted_completeness": ("complete",),
            },
        )

        bound = bind_capability_inputs(mismatched, results={}, reports={})

        self.assertEqual(bound.status, "blocked")
        self.assertIn("required_slot_mode_mismatch:none", bound.reasons)

    def test_reconciled_slot_preserves_validation_provenance(self):
        slot_id = "dimension_contribution_scan:channel"
        primary_ref = "query:channel:1"
        validation_ref = "query:channel-total:1"
        primary = result(primary_ref)
        validation = replace(
            result(validation_ref, snapshots=("snapshot:paid:1",)),
            result_ref="result:validation",
            completeness_report_ref="complete:validation",
        )
        provenance = {
            "primary_query_contract_ref": primary_ref,
            "primary_result_ref": primary.result_ref,
            "primary_report_ref": primary.completeness_report_ref,
            "primary_snapshot_refs": primary.source_snapshot_refs,
            "validation_query_contract_ref": validation_ref,
            "validation_result_ref": validation.result_ref,
            "validation_report_ref": validation.completeness_report_ref,
            "validation_snapshot_refs": validation.source_snapshot_refs,
        }
        primary_report = report(
            primary_ref,
            assertions=(
                {
                    "assertion": "dimension_total_reconciliation",
                    "passed": True,
                    "failure_reasons": (),
                    "details": {**provenance, "status": "passed"},
                },
            ),
            coverage={
                "row_count": 1,
                "required_windows": ("target_day",),
                "observed_windows": ("target_day",),
                "snapshot_ref": "snapshot:paid:1",
                "reconciliation_validation": provenance,
            },
        )
        bound = bind_capability_inputs(
            plan(
                required_slots=(
                    slot(
                        slot_id,
                        primary_ref,
                        validation_refs=(validation_ref,),
                    ),
                )
            ),
            results={primary_ref: primary, validation_ref: validation},
            reports={
                primary_ref: primary_report,
                validation_ref: report(
                    validation_ref,
                    result_ref="result:validation",
                    report_ref="complete:validation",
                ),
            },
        )

        self.assertEqual(bound.status, "ready")
        self.assertEqual(bound.validation_query_contract_refs, (validation_ref,))
        self.assertEqual(bound.validation_result_refs, (validation.result_ref,))
        self.assertEqual(
            bound.validation_completeness_report_refs,
            (validation.completeness_report_ref,),
        )
        self.assertEqual(tuple(bound.rows_by_slot), (slot_id,))
        self.assertEqual(bound.rows_by_slot[slot_id], primary.rows)

    def test_two_dimension_slots_can_share_one_immutable_total_dependency(self):
        validation_ref = "query:shared-total:1"
        validation = replace(
            result(validation_ref),
            result_ref="result:shared-total",
            completeness_report_ref="complete:shared-total",
        )
        primary_refs = ("query:channel:1", "query:region:1")
        primaries = {
            ref: replace(
                result(ref),
                result_ref=f"result:{dimension}",
                completeness_report_ref=f"complete:{dimension}",
            )
            for dimension, ref in zip(("channel", "region"), primary_refs)
        }
        reports = {
            validation_ref: report(
                validation_ref,
                result_ref=validation.result_ref,
                report_ref=validation.completeness_report_ref,
            )
        }
        for primary_ref, primary in primaries.items():
            provenance = {
                "primary_query_contract_ref": primary_ref,
                "primary_result_ref": primary.result_ref,
                "primary_report_ref": primary.completeness_report_ref,
                "primary_snapshot_refs": primary.source_snapshot_refs,
                "validation_query_contract_ref": validation_ref,
                "validation_result_ref": validation.result_ref,
                "validation_report_ref": validation.completeness_report_ref,
                "validation_snapshot_refs": validation.source_snapshot_refs,
            }
            reports[primary_ref] = report(
                primary_ref,
                result_ref=primary.result_ref,
                report_ref=primary.completeness_report_ref,
                assertions=(
                    {
                        "assertion": "dimension_total_reconciliation",
                        "passed": True,
                        "failure_reasons": (),
                        "details": {**provenance, "status": "passed"},
                    },
                ),
                coverage={
                    "row_count": 1,
                    "required_windows": ("target_day",),
                    "observed_windows": ("target_day",),
                    "snapshot_ref": "snapshot:paid:1",
                    "reconciliation_validation": provenance,
                },
            )

        bound = bind_capability_inputs(
            plan(
                required_slots=tuple(
                    slot(
                        f"dimension_contribution_scan:{dimension}",
                        primary_ref,
                        validation_refs=(validation_ref,),
                    )
                    for dimension, primary_ref in zip(
                        ("channel", "region"),
                        primary_refs,
                    )
                )
            ),
            results={**primaries, validation_ref: validation},
            reports=reports,
        )

        self.assertEqual(bound.status, "ready")
        self.assertEqual(bound.validation_query_contract_refs, (validation_ref,))
        self.assertEqual(bound.validation_result_refs, (validation.result_ref,))

    def test_required_fields_windows_and_readiness_are_hard_boundaries(self):
        primary = result("query:joint:1")
        invalid = QueryResultEnvelope(
            **{
                **primary.__dict__,
                "rows": ({"period": "2026-06-02"},),
                "observed_windows": ("baseline",),
            }
        )
        bound = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": invalid},
            reports={"query:joint:1": report("query:joint:1")},
        )

        self.assertEqual(bound.status, "blocked")
        self.assertEqual(
            bound.reasons,
            ("required_fields_missing:joint_candidates:amount",),
        )

    def test_complete_label_cannot_override_failed_assertion_or_row_count(self):
        primary = result("query:joint:1")
        forged = replace(
            report("query:joint:1"),
            assertion_results=(
                {
                    "assertion": "required_windows",
                    "passed": False,
                    "failure_reasons": ("missing_window:target_day",),
                    "details": {},
                },
            ),
            failure_reasons=("missing_window:target_day",),
        )
        bound = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": replace(primary, row_count=2)},
            reports={"query:joint:1": forged},
        )

        self.assertEqual(bound.status, "blocked")
        self.assertEqual(bound.reasons, ("primary_report_not_ready:joint_candidates",))

    def test_complete_label_cannot_upgrade_zero_rows(self):
        primary = result("query:joint:1")
        empty = replace(primary, rows=(), row_count=0)
        bound = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": empty},
            reports={
                "query:joint:1": replace(
                    report("query:joint:1"),
                    coverage_summary={
                        "row_count": 0,
                        "required_windows": ("target_day",),
                        "observed_windows": (),
                        "snapshot_ref": "snapshot:paid:1",
                    },
                )
            },
        )

        self.assertEqual(bound.status, "blocked")
        self.assertEqual(bound.reasons, ("empty_primary_result:joint_candidates",))

    def test_duplicate_slot_or_primary_ref_cannot_bind_twice(self):
        duplicate = slot("duplicate", "query:joint:1")
        bound = bind_capability_inputs(
            plan(required_slots=(duplicate, duplicate)),
            results={"query:joint:1": result("query:joint:1")},
            reports={"query:joint:1": report("query:joint:1")},
        )

        self.assertEqual(bound.status, "blocked")
        self.assertIn("duplicate_slot_id:duplicate", bound.reasons)

    def test_plan_and_slot_readiness_contract_must_agree(self):
        incompatible = replace(
            slot(),
            accepted_completeness=("complete", "partial"),
        )
        bound = bind_capability_inputs(
            plan(required_slots=(incompatible,)),
            results={"query:joint:1": result("query:joint:1")},
            reports={"query:joint:1": report("query:joint:1")},
        )

        self.assertEqual(bound.status, "blocked")
        self.assertEqual(
            bound.reasons,
            ("slot_readiness_contract_mismatch:joint_candidates",),
        )

    def test_contract_accepted_partial_report_binds_as_degraded(self):
        partial_slot = replace(
            slot(),
            accepted_completeness=("complete", "partial"),
        )
        partial_plan = replace(
            plan(required_slots=(partial_slot,)),
            minimum_readiness={
                "required_slots": "all",
                "accepted_completeness": ("complete", "partial")
            },
        )
        partial_report = replace(
            report("query:joint:1"),
            completeness_status="partial",
            analysis_readiness="degraded",
            assertion_results=(
                {
                    "assertion": "execution_succeeded",
                    "passed": True,
                    "failure_reasons": (),
                    "details": {},
                },
                {
                    "assertion": "data_quality_warning",
                    "passed": False,
                    "failure_reasons": ("null_bucket_present",),
                    "details": {},
                },
            ),
            failure_reasons=("null_bucket_present",),
        )

        bound = bind_capability_inputs(
            partial_plan,
            results={"query:joint:1": result("query:joint:1")},
            reports={"query:joint:1": partial_report},
        )

        self.assertEqual(bound.status, "degraded")
        self.assertEqual(
            bound.reasons,
            ("accepted_incomplete_input:joint_candidates:partial",),
        )

    def test_plural_validation_dependencies_use_explicit_provenance_list(self):
        primary_ref = "query:channel:primary"
        validation_refs = ("query:channel:total-a", "query:channel:total-b")
        primary = result(primary_ref)
        dependencies = tuple(result(ref) for ref in validation_refs)
        provenance_items = tuple(
            {
                "validation_query_contract_ref": item.query_contract_ref,
                "validation_result_ref": item.result_ref,
                "validation_report_ref": item.completeness_report_ref,
                "validation_snapshot_refs": item.source_snapshot_refs,
            }
            for item in dependencies
        )
        provenance = {
            "primary_query_contract_ref": primary_ref,
            "primary_result_ref": primary.result_ref,
            "primary_report_ref": primary.completeness_report_ref,
            "primary_snapshot_refs": primary.source_snapshot_refs,
            "validation_dependencies": provenance_items,
        }
        primary_report = report(
            primary_ref,
            assertions=(
                {
                    "assertion": "dimension_total_reconciliation",
                    "passed": True,
                    "failure_reasons": (),
                    "details": {**provenance, "status": "passed"},
                },
            ),
            coverage={
                "row_count": 1,
                "required_windows": ("target_day",),
                "observed_windows": ("target_day",),
                "snapshot_ref": "snapshot:paid:1",
                "reconciliation_validation": provenance,
            },
        )
        bound = bind_capability_inputs(
            plan(
                required_slots=(
                    slot(
                        "dimension_contribution_scan:channel",
                        primary_ref,
                        validation_refs=validation_refs,
                    ),
                )
            ),
            results={
                primary_ref: primary,
                **{item.query_contract_ref: item for item in dependencies},
            },
            reports={
                primary_ref: primary_report,
                **{
                    item.query_contract_ref: report(item.query_contract_ref)
                    for item in dependencies
                },
            },
        )

        self.assertEqual(bound.status, "ready")
        self.assertEqual(bound.validation_query_contract_refs, validation_refs)


if __name__ == "__main__":
    unittest.main()
