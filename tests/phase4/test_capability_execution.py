import unittest

from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    CompletenessReport,
    QueryResultEnvelope,
)
from bi_agent.runtime.capability_execution import (
    bind_capability_inputs,
    capability_plan_has_executable_query_contracts,
    validate_bound_capability_input,
)


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


def plan(
    *,
    required_slots=(),
    optional_slots=(),
    required_mode=None,
    degradation_policy=None,
):
    return CapabilityExecutionPlan(
        capability_id="joint_attribution",
        capability_contract_ref="capability:joint@1",
        required_input_slots=tuple(required_slots),
        optional_input_slots=tuple(optional_slots),
        merge_strategy="by_query_family",
        minimum_readiness={
            "required_slots": (
                required_mode
                if required_mode is not None
                else "all" if required_slots else "none"
            ),
            "accepted_completeness": ("complete",),
        },
        degradation_policy=(
            dict(degradation_policy)
            if degradation_policy is not None
            else {
                "missing_optional_input": "omit_optional_component",
                "incomplete_input": "degrade_claim",
            }
        ),
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
    def test_at_least_one_plan_is_executable_with_one_complete_required_slot(self):
        grouped = plan(
            required_slots=(
                slot("first_profile", "query:first:1"),
                slot("second_profile", "query:second:1"),
            ),
            required_mode="at_least_one",
            degradation_policy={
                "missing_required_input": "degrade_claim",
                "incomplete_input": "degrade_claim",
            },
        )

        self.assertTrue(
            capability_plan_has_executable_query_contracts(
                grouped,
                {"query:first:1"},
            )
        )
        self.assertFalse(
            capability_plan_has_executable_query_contracts(grouped, set())
        )
        self.assertFalse(
            capability_plan_has_executable_query_contracts(
                plan(required_slots=grouped.required_input_slots),
                {"query:first:1"},
            )
        )

    def test_production_binder_rejects_caller_signed_maps_without_authority(self):
        bound = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": result("query:joint:1")},
            reports={"query:joint:1": report("query:joint:1")},
        )

        self.assertEqual(bound.status, "blocked")
        self.assertEqual(bound.reasons, ("runtime_evidence_authority_missing",))
        self.assertEqual(bound.binding_manifest_ref, "")

    def test_blocked_bound_exposes_original_typed_reason_without_authority_record(self):
        bound = bind_capability_inputs(
            plan(required_slots=(slot(),)),
            results={"query:joint:1": result("query:joint:1")},
            reports={"query:joint:1": report("query:joint:1")},
        )

        self.assertEqual(
            validate_bound_capability_input(bound),
            "runtime_evidence_authority_missing",
        )

if __name__ == "__main__":
    unittest.main()
