import tempfile
import unittest
import json
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.phase4.validate_phase4 import (
    _load_local_env,
    _status_from_answer_package,
    run_eval_case,
    run_fixture_eval,
    run_real_2026h1_eval,
    run_real_eval,
)
from tools.phase4.run_phase4_pattern_slice import exit_code_for_result
from bi_agent.runtime.sql_safety import validate_select_only
from tests.phase4.fake_llm import FakeLLMClient


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


class NoAuditLLMClient:
    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        output = {}
        for key in required_keys:
            output[key] = None
        if task == "business_intent":
            output.update(
                {
                    "question_family": "pattern_explanation",
                    "target_metric": "paid_amount",
                    "pattern_family": "intra_period",
                    "scope": "full_sample",
                    "time_window": "2024-01..2026-05",
                    "target_claim": "recurring_pattern_existence",
                    "baseline_candidates": [],
                    "status_message": "ok",
                }
            )
        elif task == "boundary_decision":
            output.update(
                {
                    "boundary_status": "clear",
                    "recommended_assumption": {},
                    "clarification_questions": [],
                    "decision_summary": "ok",
                }
            )
        elif task == "analysis_route":
            output["requested_nodes"] = ["pattern_scan"]
        elif task == "data_coverage_interpretation":
            output["coverage_status"] = "sufficient"
        elif task == "next_action":
            output["next_action"] = "synthesize_answer"
        elif task == "confirm_understanding":
            output.update(
                {
                    "confirmed_intent": {"question_family": "pattern_explanation"},
                    "accepted_assumptions": [],
                    "status_message": "ok",
                }
            )
        elif task == "evidence_interpretation":
            output.update(
                {
                    "interpretation": "证据支持有边界结论。",
                    "decision_summary": "继续合成答案。",
                    "evidence_boundary": "不能写成原因定论。",
                }
            )
        elif task == "causal_audit":
            output.update(
                {
                    "causal_assessment": "candidate_hypothesis",
                    "publishable_wording": "只能作为候选解释。",
                    "supporting_reasons": [],
                    "main_risks": [],
                    "alternative_explanations": [],
                    "missing_checks": [],
                    "recommended_next_analysis": [],
                    "answer_guidance": "保留证据边界。",
                }
            )
        elif task == "answer_synthesis":
            output.update(
                {
                    "answer_text": "付费金额在当前窗口有可观察变化。",
                    "claims": [
                        {
                            "text": "周期内付费金额模式在 2024-01..2026-05 观察到：月初比其他阶段高 20.0%，方向一致比例 100.0%，可比周期 29 个。",
                            "evidence_refs": ["pattern_scan:intra_period"],
                            "numbers": {
                                "median_uplift": 0.2,
                                "direction_ratio": 1.0,
                                "comparable_periods": 29,
                            },
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        }
                    ],
                }
            )
        elif task == "semantic_audit":
            output.update({"audit_status": "passed", "extracted_claims": [], "issues": []})
        elif task == "final_business_summary":
            output["summary_text"] = (
                "我对问题的理解是：用户要判断全样本付费金额的周期内模式。\n"
                "分析脉络：我检查了数据覆盖、模式证据和答案校验。\n"
                "关键发现：当前证据能把排查方向收敛到月初支付节奏，20.0%、100.0%、29。\n"
                "最终结论：已验证结论是：周期内付费金额模式在 2024-01..2026-05 观察到：月初比其他阶段高 20.0%，方向一致比例 100.0%，可比周期 29 个。 当前证据能把排查方向收敛到这个方向。\n"
                "需要注意：还不能直接说这是唯一原因或已被原因证明。"
            )
        return SimpleNamespace(output=output, audit={})


class Phase4EvalHarnessTest(unittest.TestCase):
    def test_phase4_fixture_eval_requires_month_start_and_two_siblings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_fixture_eval(
                artifact_root=tmpdir,
                llm_client=FakeLLMClient(),
            )
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
                result = run_real_eval(
                    artifact_root=tmpdir,
                    environ=env,
                    llm_client=FakeLLMClient(),
                )
            with open(result.artifact_path, encoding="utf-8") as handle:
                artifact = json.load(handle)

        expected_hash = validate_select_only(
            env["WAJE_PHASE4_PATTERN_SQL"],
            aggregate=True,
        ).query_hash
        self.assertEqual(result.status, "passed")
        self.assertTrue(result.business_conclusion_published)
        self.assertEqual(artifact["admin_audit"]["sql_hash"], expected_hash)

    def test_real_eval_uses_case_sql_before_env_sql(self):
        env = {
            "WAJE_CLICKHOUSE_HOST": "localhost",
            "WAJE_CLICKHOUSE_PORT": "8123",
            "WAJE_CLICKHOUSE_USER": "reader",
            "WAJE_CLICKHOUSE_PASSWORD": "secret",
            "WAJE_CLICKHOUSE_DATABASE": "waje_bi",
            "WAJE_CLICKHOUSE_SECURE": "false",
            "WAJE_PHASE4_PATTERN_SQL": "SELECT count(*) FROM wrong_binding",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            case_file = Path(tmpdir) / "cases.yaml"
            case_file.write_text(
                """
cases:
  - case_id: case_sql
    pattern_family: intra_period
    real_sql: SELECT month, phase, sum(amount) AS amount FROM accepted_paid_success GROUP BY month, phase
    pattern_params:
      target_phase: start
    required_capabilities: [data_quality_check, pattern_scan]
""",
                encoding="utf-8",
            )
            with patch(
                "tools.phase4.validate_phase4.ClickHouseRuntime.from_env",
                return_value=FakeRealRuntime(),
            ):
                result = run_real_eval(
                    artifact_root=tmpdir,
                    environ=env,
                    case_id="case_sql",
                    case_file=case_file,
                    llm_client=FakeLLMClient(),
                )
            with open(result.artifact_path, encoding="utf-8") as handle:
                artifact = json.load(handle)

        self.assertEqual(
            artifact["admin_audit"]["sql_hash"],
            validate_select_only(
                "SELECT month, phase, sum(amount) AS amount FROM accepted_paid_success GROUP BY month, phase",
                aggregate=True,
            ).query_hash,
        )

    def test_real_eval_keeps_llm_env_available_for_workflow(self):
        env = {
            "WAJE_CLICKHOUSE_HOST": "localhost",
            "WAJE_CLICKHOUSE_PORT": "8123",
            "WAJE_CLICKHOUSE_USER": "reader",
            "WAJE_CLICKHOUSE_PASSWORD": "secret",
            "WAJE_CLICKHOUSE_DATABASE": "waje_bi",
            "WAJE_CLICKHOUSE_SECURE": "false",
            "WAJE_LLM_PROVIDER": "openai_compatible",
            "WAJE_LLM_BASE_URL": "https://api.deepseek.com",
            "WAJE_LLM_MODEL": "deepseek-v4-flash",
            "WAJE_LLM_API_KEY": "test-key",
            "WAJE_PHASE4_PATTERN_SQL": (
                "SELECT month, phase, sum(amount) AS amount "
                "FROM accepted_paid_success GROUP BY month, phase"
            ),
        }
        seen = {}

        def fake_workflow(_request):
            seen["model"] = os.environ.get("WAJE_LLM_MODEL")
            seen["api_key"] = os.environ.get("WAJE_LLM_API_KEY")
            return SimpleNamespace(
                status="failed",
                failure_reason="sentinel",
                answer_package=None,
                artifact_path="",
            )

        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "tools.phase4.validate_phase4.ClickHouseRuntime.from_env",
                return_value=FakeRealRuntime(),
            ), patch(
                "tools.phase4.validate_phase4.run_pattern_workflow",
                side_effect=fake_workflow,
            ):
                run_real_eval(environ=env)

        self.assertEqual(seen["model"], "deepseek-v4-flash")
        self.assertEqual(seen["api_key"], "test-key")

    def test_fixture_cli_exit_code_fails_on_degraded_or_blocked(self):
        self.assertEqual(exit_code_for_result("fixture", "passed"), 0)
        self.assertEqual(exit_code_for_result("fixture", "degraded"), 1)
        self.assertEqual(exit_code_for_result("fixture", "blocked"), 1)
        self.assertEqual(exit_code_for_result("real", "blocked"), 0)

    def test_real_suite_checks_expected_statuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            case_file = Path(tmpdir) / "cases.yaml"
            case_file.write_text(
                """
cases:
  - case_id: missing_env_expected_blocked
    pattern_family: intra_period
    expected_status: blocked
    pattern_params:
      target_phase: start
    required_capabilities: [data_quality_check, pattern_scan]
""",
                encoding="utf-8",
            )

            result = run_real_2026h1_eval(
                artifact_root=tmpdir,
                environ={},
                case_file=case_file,
            )

        self.assertTrue(result.passed)
        self.assertFalse(result.mismatches)

    def test_real_eval_degrades_when_history_is_too_short_but_query_succeeded(self):
        case = {
            "case_id": "short_history",
            "pattern_family": "intra_period",
            "time_window": "2024-01..2026-05",
            "pattern_params": {"target_phase": "start"},
            "required_capabilities": ["data_quality_check", "pattern_scan"],
            "fixture_rows": [
                {"month": "2026-01", "phase": "start", "amount": 120},
                {"month": "2026-01", "phase": "mid", "amount": 100},
                {"month": "2026-01", "phase": "end", "amount": 96},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_eval_case(
                case,
                mode="real",
                artifact_root=tmpdir,
                sql_text="SELECT count(*) FROM paid_success",
                llm_client=FakeLLMClient(),
            )

        self.assertEqual(result.status, "degraded")
        self.assertIn("insufficient_comparable_periods", result.reason)
        self.assertEqual(result.owner, "")
        self.assertFalse(result.business_conclusion_published)

    def test_status_uses_primary_compare_evidence_not_only_pattern_scan(self):
        package = {
            "final_explanation": {"status": "passed"},
            "sections": [
                {
                    "section_id": "evidence",
                    "payload": {
                        "evidence": [
                            {
                                "capability_id": "compare_periods",
                                "typed_payload": {
                                    "pattern_family": "custom_baseline",
                                    "median_uplift": 0.52,
                                    "direction_ratio": 1.0,
                                    "comparable_periods": 2,
                                },
                                "strength": "high",
                                "wording_limit": "supported",
                                "limitations": [],
                            }
                        ]
                    },
                }
            ],
        }

        status, reason = _status_from_answer_package(package, "custom_baseline")

        self.assertEqual(status, "passed")
        self.assertEqual(reason, "pattern_established")

    def test_eval_case_passes_business_question_and_baseline_labels(self):
        case = {
            "case_id": "q2_vs_q1_labels",
            "question": "2026年Q2相比Q1付费金额有没有变化？",
            "pattern_family": "custom_baseline",
            "time_window": "2026-01-01..2026-06-30",
            "baseline": {"label": "Q1"},
            "target": {"label": "Q2"},
            "pattern_params": {
                "period_key": "period",
                "group_key": "group",
                "target_group": "target",
                "baseline_group": "baseline",
                "min_periods": 1,
            },
            "required_capabilities": ["data_quality_check", "pattern_scan"],
            "fixture_rows": [
                {"period": "h1", "group": "baseline", "amount": 100},
                {"period": "h1", "group": "target", "amount": 120},
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_eval_case(
                case,
                mode="fixture",
                artifact_root=tmpdir,
                llm_client=FakeLLMClient(),
            )
            with open(result.artifact_path, encoding="utf-8") as handle:
                artifact = json.load(handle)
        answer_text = artifact["sections"][0]["payload"]["answer_text"]

        self.assertIn("Q2", answer_text)
        self.assertIn("Q1", answer_text)

    def test_eval_case_fails_when_llm_audit_is_missing(self):
        case = {
            "case_id": "missing_llm_audit",
            "pattern_family": "intra_period",
            "pattern_params": {"target_phase": "start"},
            "required_capabilities": ["data_quality_check", "pattern_scan"],
            "fixture_rows": list(_month_start_rows()),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_eval_case(
                case,
                mode="fixture",
                artifact_root=tmpdir,
                llm_client=NoAuditLLMClient(),
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "missing_required_llm_audit")

    def test_local_env_loader_keeps_quoted_sql(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "WAJE_PHASE4_PATTERN_SQL='SELECT count(*) FROM paid_success'\n",
                encoding="utf-8",
            )

            loaded = _load_local_env(env_path)

        self.assertEqual(
            loaded["WAJE_PHASE4_PATTERN_SQL"],
            "SELECT count(*) FROM paid_success",
        )


if __name__ == "__main__":
    unittest.main()
