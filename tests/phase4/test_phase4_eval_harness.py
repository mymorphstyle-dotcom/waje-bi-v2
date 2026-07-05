import tempfile
import unittest
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from tools.phase4.validate_phase4 import run_fixture_eval, run_real_eval
from tools.phase4.run_phase4_pattern_slice import exit_code_for_result
from bi_agent.runtime.sql_safety import validate_select_only


class FakeRealRuntime:
    binding = SimpleNamespace(reason="")

    def configured(self):
        return True

    def aggregate(self, sql, query_id):
        return SimpleNamespace(
            ok=True,
            reason="",
            rows=_month_start_rows(),
            query_hash=validate_select_only(sql, aggregate=True).query_hash,
            query_id=query_id,
        )


def _month_start_rows():
    rows = []
    year = 2024
    month = 1
    while (year, month) <= (2026, 5):
        month_key = f"{year}-{month:02d}"
        rows.extend(
            (
                {"month": month_key, "phase": "start", "amount": 120},
                {"month": month_key, "phase": "mid", "amount": 100},
                {"month": month_key, "phase": "end", "amount": 96},
            )
        )
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(rows)


class Phase4EvalHarnessTest(unittest.TestCase):
    def test_phase4_fixture_eval_requires_month_start_and_two_siblings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_fixture_eval(artifact_root=tmpdir)
            for case in result.cases:
                with open(case.artifact_path, encoding="utf-8") as handle:
                    artifact = json.load(handle)
                self.assertTrue(artifact["non_real_data"])
                self.assertEqual(artifact["eval_mode"], "fixture")
                evidence = artifact["sections"][1]["payload"]["evidence"]
                data_quality = [
                    item for item in evidence if item["capability"] == "data_quality_check"
                ][0]
                self.assertFalse(data_quality["limitations"])
            self.assertTrue(
                all(not case.business_conclusion_published for case in result.cases)
            )

        self.assertTrue(result.engineering_fixture_passed)
        self.assertEqual(result.month_start_case.status, "passed")
        self.assertGreaterEqual(result.sibling_summary.passed_count, 2)
        self.assertTrue(
            all(case.reason for case in result.sibling_summary.degraded_or_blocked)
        )

    def test_real_eval_reports_external_dependency_block_when_clickhouse_env_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_real_eval(artifact_root=tmpdir, environ={})

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "external_dependency_blocked")
        self.assertEqual(result.owner, "data_engineering_owner")
        self.assertIn("ClickHouse", result.repair_path)
        self.assertFalse(result.business_conclusion_published)

    def test_invalid_real_sql_binding_reports_validator_without_sql_text(self):
        env = {
            "WAJE_CLICKHOUSE_HOST": "localhost",
            "WAJE_CLICKHOUSE_PORT": "8123",
            "WAJE_CLICKHOUSE_USER": "reader",
            "WAJE_CLICKHOUSE_PASSWORD": "secret",
            "WAJE_CLICKHOUSE_DATABASE": "waje_bi",
            "WAJE_CLICKHOUSE_SECURE": "false",
            "WAJE_PHASE4_PATTERN_SQL": "DROP TABLE paid_success",
        }
        result = run_real_eval(environ=env)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "external_dependency_blocked")
        self.assertEqual(
            result.diagnostics["validator_results"][0]["reason"],
            "select_only",
        )
        serialized = json.dumps(asdict(result), ensure_ascii=False)
        self.assertNotIn("DROP TABLE", serialized)
        self.assertNotIn("secret", serialized)

    def test_invalid_clickhouse_runtime_binding_is_external_dependency_block(self):
        env = {
            "WAJE_CLICKHOUSE_HOST": "localhost",
            "WAJE_CLICKHOUSE_PORT": "not-a-port",
            "WAJE_CLICKHOUSE_USER": "reader",
            "WAJE_CLICKHOUSE_PASSWORD": "secret",
            "WAJE_CLICKHOUSE_DATABASE": "waje_bi",
            "WAJE_CLICKHOUSE_SECURE": "false",
            "WAJE_PHASE4_PATTERN_SQL": "SELECT count(*) FROM paid_success",
        }

        result = run_real_eval(environ=env)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "external_dependency_blocked")
        self.assertIn("invalid ClickHouse binding", result.repair_path)
        self.assertFalse(result.business_conclusion_published)

    def test_real_eval_artifact_records_executed_sql_hash(self):
        env = {
            "WAJE_CLICKHOUSE_HOST": "localhost",
            "WAJE_CLICKHOUSE_PORT": "8123",
            "WAJE_CLICKHOUSE_USER": "reader",
            "WAJE_CLICKHOUSE_PASSWORD": "secret",
            "WAJE_CLICKHOUSE_DATABASE": "waje_bi",
            "WAJE_CLICKHOUSE_SECURE": "false",
            "WAJE_PHASE4_PATTERN_SQL": "SELECT month, phase, sum(amount) AS amount FROM accepted_paid_success GROUP BY month, phase",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "tools.phase4.validate_phase4.ClickHouseRuntime.from_env",
                return_value=FakeRealRuntime(),
            ):
                result = run_real_eval(artifact_root=tmpdir, environ=env)
            with open(result.artifact_path, encoding="utf-8") as handle:
                artifact = json.load(handle)

        expected_hash = validate_select_only(
            env["WAJE_PHASE4_PATTERN_SQL"],
            aggregate=True,
        ).query_hash
        self.assertEqual(result.status, "passed")
        self.assertTrue(result.business_conclusion_published)
        self.assertEqual(artifact["admin_audit"]["sql_hash"], expected_hash)

    def test_fixture_cli_exit_code_fails_on_degraded_or_blocked(self):
        self.assertEqual(exit_code_for_result("fixture", "passed"), 0)
        self.assertEqual(exit_code_for_result("fixture", "degraded"), 1)
        self.assertEqual(exit_code_for_result("fixture", "blocked"), 1)
        self.assertEqual(exit_code_for_result("real", "blocked"), 0)


if __name__ == "__main__":
    unittest.main()
