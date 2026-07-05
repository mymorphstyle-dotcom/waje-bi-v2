import json
import tempfile
import unittest

from bi_agent.runtime.langgraph_workflow import run_pattern_workflow
from bi_agent.runtime.artifacts import filter_artifact_for_role


class WorkflowArtifactsAnswerTest(unittest.TestCase):
    def test_langgraph_failure_does_not_publish_business_conclusion(self):
        result = run_pattern_workflow({"force_langgraph_failure": True})
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.answer_package)
        self.assertTrue(result.failure_reason)

    def test_role_visibility_hides_admin_sql_from_business_reader(self):
        artifact = {
            "sections": [
                {
                    "section_id": "summary",
                    "visibility": "business_summary",
                    "payload": {"text": "draft"},
                },
                {
                    "section_id": "sql",
                    "visibility": "admin_audit",
                    "payload": {"sql": "SELECT 1"},
                },
            ]
        }
        filtered = filter_artifact_for_role(artifact, "business_reader")
        self.assertEqual(
            [section["section_id"] for section in filtered["sections"]],
            ["summary"],
        )

    def test_successful_workflow_persists_answer_package_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow({"artifact_root": tmpdir, "run_id": "test-run"})

            self.assertEqual(result.status, "draft")
            self.assertTrue(result.answer_package)
            self.assertTrue(result.artifact_path.endswith("answer_package.json"))
            with open(result.artifact_path, encoding="utf-8") as handle:
                artifact = json.load(handle)

        self.assertEqual(artifact["run_id"], "test-run")
        self.assertEqual(
            [event["node"] for event in artifact["checkpoint_events"]],
            [
                "intent_binding",
                "compile_graph",
                "inspect_schema",
                "validate_runtime_binding",
                "execute_capabilities",
                "synthesize_draft_answer",
                "answer_verify",
                "persist_artifact",
            ],
        )
        self.assertIn("accepted_graph", artifact)
        self.assertIn("proposed_graph", artifact)
        self.assertIn("validator_results", artifact)

    def test_business_artifact_sections_expose_sql_hash_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow({"artifact_root": tmpdir, "run_id": "visibility"})

            business = filter_artifact_for_role(result.answer_package, "business_reader")
            admin = filter_artifact_for_role(result.answer_package, "data_owner_admin")

        self.assertIn("sql_hash", json.dumps(business))
        self.assertNotIn("SELECT", json.dumps(business))
        self.assertIn("SELECT", json.dumps(admin))

    def test_wording_warnings_do_not_block_phase4_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "wording",
                    "draft_claims": [
                        {
                            "text": "Month-start timing caused paid amount uplift.",
                            "evidence_refs": ["pattern_scan:intra_period"],
                            "numbers": {"median_uplift": 0.2},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        }
                    ],
                }
            )

            admin = filter_artifact_for_role(result.answer_package, "data_owner_admin")

        self.assertEqual(result.status, "draft")
        self.assertTrue(
            any(
                warning["code"] == "causal_wording_without_causal_evidence"
                for warning in admin["admin_audit"]["verifier"]["warnings"]
            )
        )

    def test_retry_policy_retries_technical_failure_once_only(self):
        technical = run_pattern_workflow(
            {
                "force_failure": {
                    "node": "execute_capabilities",
                    "failure_type": "technical",
                }
            }
        )
        permission = run_pattern_workflow(
            {
                "force_failure": {
                    "node": "execute_capabilities",
                    "failure_type": "permission",
                }
            }
        )

        self.assertEqual(technical.status, "failed")
        self.assertEqual(
            [
                event["attempt"]
                for event in technical.checkpoint_events
                if event["node"] == "execute_capabilities"
            ],
            [1, 2],
        )
        self.assertEqual(
            [
                event["attempt"]
                for event in permission.checkpoint_events
                if event["node"] == "execute_capabilities"
            ],
            [1],
        )


if __name__ == "__main__":
    unittest.main()
