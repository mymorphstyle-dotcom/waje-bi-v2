import unittest

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.analysis_assets import build_analysis_assets
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult


class AnalysisAssetsTest(unittest.TestCase):
    def test_builds_assets_from_answer_package(self):
        assets = build_analysis_assets(
            {
                "admin_audit": {
                    "compiler_runtime_plan": {
                        "query_intents": ("dimension_scan",),
                        "contract_gaps": ("payment_status_contract_missing",),
                    },
                    "contract_gap_diagnostics": (
                        {
                            "gap_id": "payment_status_contract_missing",
                            "status": "contract_absent",
                        },
                    ),
                },
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {
                            "claim_groups": [
                                {
                                    "text": "当前只能支持渠道贡献候选判断。",
                                    "evidence_refs": ["segment_contribution:inline"],
                                    "strength": "medium",
                                }
                            ]
                        },
                    }
                ],
            }
        )

        asset_types = {asset["asset_type"] for asset in assets}
        self.assertIn("compiler_runtime_plan", asset_types)
        self.assertIn("contract_gap_diagnostic", asset_types)
        self.assertIn("verified_claim_slot", asset_types)

    def test_store_round_trips_topic_assets(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-assets", owner_id="analyst-1")
        topic = store.create_topic("thread-assets", title="收入分析")
        store.save_analysis_assets(
            "thread-assets",
            topic.topic_id,
            ({"asset_type": "dimension_scan", "status": "usable", "query_ref": "query:1"},),
        )

        assets = store.list_analysis_assets("thread-assets", topic.topic_id)
        self.assertEqual(assets[0]["query_ref"], "query:1")

    def test_runtime_reuses_topic_assets_in_manifest_and_run_request(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-assets-runtime", owner_id="analyst-1")
        topic = store.create_topic("thread-assets-runtime", title="收入分析", summary="收入分析")
        store.set_current_topic("thread-assets-runtime", topic.topic_id)
        store.save_analysis_assets(
            "thread-assets-runtime",
            topic.topic_id,
            (
                {
                    "asset_type": "dimension_scan",
                    "dimension": "channel",
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                },
            ),
        )

        result = runtime.handle_message(
            "thread-assets-runtime",
            "继续看哪个渠道影响最大",
        )

        self.assertIsNotNone(result.run_request)
        self.assertEqual(
            result.run_request.prior_analysis_assets,
            (
                {
                    "asset_type": "dimension_scan",
                    "dimension": "channel",
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                },
            ),
        )
        self.assertEqual(
            result.run_request.context_manifest["analysis_assets"][0]["query_ref"],
            "query:channel-scan",
        )

    def test_agent_core_persists_assets_after_completed_answer_package(self):
        def workflow(request):
            return WorkflowRunResult(
                status="draft",
                run_id=request["run_id"],
                answer_package={
                    "run_id": request["run_id"],
                    "status": "draft",
                    "snapshot_id": "2026H1",
                    "permission_scope": "analyst",
                    "sections": [
                        {
                            "section_id": "summary",
                            "payload": {
                                "answer_text": "渠道贡献主要来自直营。",
                                "claim_groups": [
                                    {
                                        "text": "直营渠道贡献较高。",
                                        "evidence_refs": ["segment_contribution:inline"],
                                        "strength": "medium",
                                    }
                                ],
                            },
                        }
                    ],
                    "admin_audit": {
                        "compiler_runtime_plan": {
                            "query_intents": ("dimension_scan_reuse",),
                            "asset_inputs_used": ("query:channel-scan",),
                        },
                        "contract_gap_diagnostics": (
                            {
                                "gap_id": "payment_status_contract_missing",
                                "status": "contract_absent",
                            },
                        ),
                        "verifier": {"status": "passed"},
                    },
                },
                artifact_path="artifacts/phase-7/run-analysis-assets/answer_package.json",
                checkpoint_events=(),
            )

        store = InMemoryConversationStore()
        store.create_thread("thread-agent-assets", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=workflow)

        result = core.run_message(
            thread_id="thread-agent-assets",
            run_id="run-agent-assets",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "completed")
        assets = store.list_analysis_assets("thread-agent-assets", result["topic_id"])
        asset_types = {asset["asset_type"] for asset in assets}
        self.assertIn("compiler_runtime_plan", asset_types)
        self.assertIn("contract_gap_diagnostic", asset_types)
        self.assertIn("verified_claim_slot", asset_types)


if __name__ == "__main__":
    unittest.main()
