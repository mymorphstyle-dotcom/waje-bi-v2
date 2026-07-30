from __future__ import annotations

import unittest
from datetime import date

from tests.gate1_fixtures import NOW
from waje_vnext.capabilities import (
    OrdinalGroupSpec,
    PeriodComparisonEffectExecutor,
    PeriodComparisonQuerySpec,
    PeriodUnit,
    SourceBinding,
    build_period_comparison_evidence,
    compile_period_comparison_sql,
    parse_period_comparison_tsv,
    summarize_period_comparison,
)
from waje_vnext.domain.authority import ComparisonGroupRole
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.runtime_state import OutboxMessage


def source_binding() -> SourceBinding:
    return SourceBinding(
        source_ref="source:paid_order_daily:v1",
        metric_ref="metric:paid_amount:v1",
        table="waje_bi.paid_order_success_daily_20240101_20260704_v2",
        date_column="business_date_lagos",
        value_column="paid_amount_ngn",
        snapshot_release_ref="release:test:v1",
        business_timezone="Africa/Lagos",
        available_from=date(2024, 1, 1),
        available_through=date(2026, 7, 4),
    )


def query_spec() -> PeriodComparisonQuerySpec:
    return PeriodComparisonQuerySpec(
        query_spec_id="query-spec:test:v1",
        metric_ref="metric:paid_amount:v1",
        source_ref="source:paid_order_daily:v1",
        period_unit=PeriodUnit.CALENDAR_MONTH,
        range_start=date(2024, 1, 1),
        range_end=date(2024, 2, 29),
        groups=(
            OrdinalGroupSpec(
                group_id="focal",
                role=ComparisonGroupRole.FOCAL,
                lower_inclusive=1,
                upper_inclusive=5,
            ),
            OrdinalGroupSpec(
                group_id="reference",
                role=ComparisonGroupRole.REFERENCE,
                lower_inclusive=6,
                upper_inclusive=24,
            ),
        ),
    )


TSV = """period_start\tgroup_id\ttotal_value\tobserved_exposure_units\tvalue_per_exposure_unit
2024-01-01\tfocal\t500\t5\t100
2024-01-01\treference\t1140\t19\t60
2024-02-01\tfocal\t450\t5\t90
2024-02-01\treference\t1330\t19\t70
"""


class Gate3PeriodComparisonTest(unittest.TestCase):
    def test_compiler_uses_agent_supplied_groups_and_governed_binding(
        self,
    ) -> None:
        sql = compile_period_comparison_sql(query_spec(), source_binding())

        self.assertIn("BETWEEN 1 AND 5, 'focal'", sql)
        self.assertIn("BETWEEN 6 AND 24, 'reference'", sql)
        self.assertIn(source_binding().table, sql)
        self.assertIn("uniqExact(business_date)", sql)
        self.assertNotIn("month_start", sql)

    def test_summary_keeps_raw_and_exposure_normalized_results(self) -> None:
        rows = parse_period_comparison_tsv(TSV)
        summary = summarize_period_comparison(query_spec(), rows)
        contrast = summary["contrasts"][0]

        self.assertEqual(contrast["raw_direction_hits"], 0)
        self.assertEqual(contrast["normalized_direction_hits"], 2)
        self.assertEqual(
            summary["exposure_by_group"]["reference"]["median"],
            19.0,
        )

    def test_evidence_contains_sufficient_statistics_without_selecting_estimand(
        self,
    ) -> None:
        evidence = build_period_comparison_evidence(
            spec=query_spec(),
            binding=source_binding(),
            task_id="task-pattern",
            rows=parse_period_comparison_tsv(TSV),
        )

        self.assertEqual(len(evidence), 2)
        self.assertIn(
            "exposure_by_group",
            evidence[0].inline_payload,
        )
        self.assertIn("contrasts", evidence[1].inline_payload)
        self.assertEqual(
            evidence[1].inline_payload["contrasts"][0][
                "normalized_direction_hits"
            ],
            2,
        )

    def test_contract_rejects_overlapping_agent_groups(self) -> None:
        with self.assertRaisesRegex(ValueError, "overlap"):
            PeriodComparisonQuerySpec(
                query_spec_id="query-spec:invalid",
                metric_ref="metric:paid_amount:v1",
                source_ref="source:paid_order_daily:v1",
                period_unit=PeriodUnit.CALENDAR_MONTH,
                range_start=date(2024, 1, 1),
                range_end=date(2024, 1, 31),
                groups=(
                    OrdinalGroupSpec(
                        group_id="focal",
                        role=ComparisonGroupRole.FOCAL,
                        lower_inclusive=1,
                        upper_inclusive=10,
                    ),
                    OrdinalGroupSpec(
                        group_id="reference",
                        role=ComparisonGroupRole.REFERENCE,
                        lower_inclusive=10,
                        upper_inclusive=20,
                    ),
                ),
            )

    def test_effect_executor_turns_typed_agent_spec_into_evidence(self) -> None:
        class Runner:
            def __init__(self) -> None:
                self.sql = None

            def run(self, sql: str) -> str:
                self.sql = sql
                return TSV

        runner = Runner()
        executor = PeriodComparisonEffectExecutor(
            source_bindings={
                source_binding().source_ref: source_binding(),
            },
            query_runner=runner,
        )
        payload = {
            "action_kind": "run_probe",
            "request": {
                "task_id": "task-pattern",
                "probe_kind": "period_comparison",
                "parameters": {
                    "query_spec": to_jsonable(query_spec()),
                },
            },
            "expected_head_version": 2,
            "frame_revision_id": "frame-1",
            "plan_revision_id": "plan-1",
        }
        result = executor.execute(
            OutboxMessage(
                outbox_message_id="outbox-1",
                case_id="case-1",
                source_event_cursor=1,
                action_id="action-probe",
                idempotency_key="effect-key",
                destination="analysis_probe",
                contract_ref="waje-vnext://runtime/effect-request.v1",
                payload=payload,
                payload_sha256=content_sha256(payload),
                created_at=NOW,
            )
        )

        self.assertIsNotNone(runner.sql)
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(
            result.payload["contrasts"][0]["normalized_direction_hits"],
            2,
        )


if __name__ == "__main__":
    unittest.main()
