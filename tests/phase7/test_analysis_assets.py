import unittest

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.analysis_assets import (
    build_analysis_assets,
    build_dimension_scan_reuse_contract,
    merge_analysis_assets,
    reusable_dimension_scan_inputs,
)
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult, run_pattern_workflow
from tests.phase4.fake_llm import FakeLLMClient


class AnalysisAssetsTest(unittest.TestCase):
    def test_builds_assets_from_answer_package(self):
        assets = build_analysis_assets(
            {
                "run_id": "run-assets-build",
                "snapshot_id": "2026H1",
                "permission_scope": "analyst",
                "admin_audit": {
                    "compiler_runtime_plan": {
                        "target_metric": "paid_amount",
                        "scope": "full_sample",
                        "time_window": "2026-07-08",
                        "windows": {"target": "2026-07-08", "baseline": "2026-07-07"},
                        "baselines": ("previous_day",),
                        "query_intents": ("dimension_scan",),
                        "row_shapes": (
                            {
                                "source": "clickhouse",
                                "required_fields": (
                                    "period",
                                    "group",
                                    "amount",
                                    "paid_users",
                                    "orders",
                                    "first_paid_users",
                                ),
                                "dimension_keys": ("channel",),
                            },
                        ),
                        "contract_gaps": ("payment_status_contract_missing",),
                    },
                    "row_query_plan": {
                        "query_intent": "dimension_scan",
                        "query_id": "run-assets-build:dimension_scan",
                        "query_hash": "hash-channel-scan",
                        "result_refs": ["hash-channel-scan"],
                        "dimension_keys": ["channel"],
                        "rows_by_intent": {
                            "dimension_scan": [
                                {
                                    "period": "2026-07-07",
                                    "group": "baseline",
                                    "amount": 100,
                                    "paid_users": 10,
                                    "orders": 12,
                                    "first_paid_users": 3,
                                    "channel": "A",
                                },
                                {
                                    "period": "2026-07-08",
                                    "group": "target",
                                    "amount": 130,
                                    "paid_users": 11,
                                    "orders": 14,
                                    "first_paid_users": 4,
                                    "channel": "A",
                                },
                            ]
                        },
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
                                    "wording_limit": "candidate",
                                    "limitations": ["no_comparable_segments"],
                                    "verifier_status": "passed",
                                }
                            ]
                        },
                    }
                ],
            }
        )

        asset_types = {asset["asset_type"] for asset in assets}
        self.assertIn("compiler_runtime_plan", asset_types)
        self.assertIn("dimension_scan", asset_types)
        self.assertIn("contract_gap_diagnostic", asset_types)
        self.assertIn("claim_context_slot", asset_types)
        dimension_scan = next(asset for asset in assets if asset["asset_type"] == "dimension_scan")
        claim_context = next(asset for asset in assets if asset["asset_type"] == "claim_context_slot")
        self.assertEqual(dimension_scan["query_ref"], "hash-channel-scan")
        self.assertEqual(tuple(dimension_scan["dimensions"]), ("channel",))
        self.assertEqual(dimension_scan["snapshot_version"], "2026H1")
        self.assertEqual(dimension_scan["permission_scope"], "analyst")
        self.assertIn("created_at", dimension_scan)
        self.assertIn("expires_at", dimension_scan)
        self.assertEqual(dimension_scan["result_refs"], ["hash-channel-scan"])
        self.assertEqual(dimension_scan["row_payload"]["row_count"], 2)
        self.assertFalse(dimension_scan["row_payload"]["truncated"])
        self.assertEqual(dimension_scan["row_payload"]["rows"][0]["channel"], "A")
        self.assertEqual(dimension_scan["reuse_contract"]["target_metric"], "paid_amount")
        self.assertEqual(dimension_scan["reuse_contract"]["scope"], "full_sample")
        self.assertEqual(
            dimension_scan["reuse_contract"]["windows"],
            {"target": "2026-07-08", "baseline": "2026-07-07"},
        )
        self.assertTrue(dimension_scan["reuse_contract"]["contract_signature"])
        self.assertEqual(tuple(claim_context["limitations"]), ("no_comparable_segments",))
        self.assertEqual(claim_context["verifier_status"], "passed")
        self.assertEqual(claim_context["wording_limit"], "candidate")
        self.assertFalse(claim_context["can_support_business_truth"])

    def test_dimension_scan_asset_without_rows_is_context_only(self):
        assets = build_analysis_assets(
            {
                "run_id": "run-assets-missing-rows",
                "snapshot_id": "2026H1",
                "permission_scope": "analyst",
                "admin_audit": {
                    "compiler_runtime_plan": {
                        "target_metric": "paid_amount",
                        "scope": "full_sample",
                        "time_window": "2026-07-08",
                        "windows": {"target": "2026-07-08", "baseline": "2026-07-07"},
                        "baselines": ("previous_day",),
                        "query_intents": ("dimension_scan",),
                        "row_shapes": (
                            {
                                "source": "clickhouse",
                                "required_fields": ("period", "group", "amount"),
                                "dimension_keys": ("channel",),
                            },
                        ),
                    },
                    "row_query_plan": {
                        "query_intent": "dimension_scan",
                        "query_id": "run-assets-missing-rows:dimension_scan",
                        "query_hash": "hash-missing-rows",
                        "result_refs": ["hash-missing-rows"],
                        "dimension_keys": ["channel"],
                    },
                },
            }
        )

        dimension_scan = next(asset for asset in assets if asset["asset_type"] == "dimension_scan")
        self.assertEqual(dimension_scan["status"], "context_only")
        self.assertEqual(dimension_scan["row_payload"]["row_count"], 0)
        self.assertEqual(dimension_scan["row_payload"]["rows"], [])

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

    def test_dimension_scan_reuse_rejects_contract_version_or_schema_drift(self):
        base_contract = build_dimension_scan_reuse_contract(
            target_metric="paid_amount",
            scope="full_sample",
            time_window="yesterday",
            windows={"target": "yesterday"},
            baselines=("previous_day",),
            permission_scope="analyst",
            snapshot_version="2026H1",
            dimensions=("channel",),
            required_fields=("period", "group", "amount", "orders"),
            contract_versions={"payment_fact": "v1"},
            schema_fingerprint="schema:v1",
        )
        asset = {
            "asset_type": "dimension_scan",
            "status": "usable",
            "dimensions": ("channel",),
            "query_ref": "query:channel-v1",
            "result_refs": ("result:channel-v1",),
            "created_at": "2026-07-09T00:00:00+00:00",
            "expires_at": "2026-07-10T00:00:00+00:00",
            "reuse_contract": base_contract,
            "row_payload": {
                "row_count": 2,
                "truncated": False,
                "rows": [
                    {"period": "2026-07-07", "group": "previous_day", "amount": 80, "orders": 12, "channel": "ads"},
                    {"period": "2026-07-08", "group": "target", "amount": 120, "orders": 18, "channel": "ads"},
                ],
            },
            "applicable_scans": ("dimension_scan",),
        }

        matched = reusable_dimension_scan_inputs(
            (asset,),
            target_metric="paid_amount",
            scope="full_sample",
            time_window="yesterday",
            windows={"target": "yesterday"},
            baselines=("previous_day",),
            permission_scope="analyst",
            snapshot_version="2026H1",
            required_dimensions=("channel",),
            required_fields=("period", "group", "amount", "orders"),
            contract_versions={"payment_fact": "v1"},
            schema_fingerprint="schema:v1",
        )
        contract_drift = reusable_dimension_scan_inputs(
            (asset,),
            target_metric="paid_amount",
            scope="full_sample",
            time_window="yesterday",
            windows={"target": "yesterday"},
            baselines=("previous_day",),
            permission_scope="analyst",
            snapshot_version="2026H1",
            required_dimensions=("channel",),
            required_fields=("period", "group", "amount", "orders"),
            contract_versions={"payment_fact": "v2"},
            schema_fingerprint="schema:v1",
        )
        schema_drift = reusable_dimension_scan_inputs(
            (asset,),
            target_metric="paid_amount",
            scope="full_sample",
            time_window="yesterday",
            windows={"target": "yesterday"},
            baselines=("previous_day",),
            permission_scope="analyst",
            snapshot_version="2026H1",
            required_dimensions=("channel",),
            required_fields=("period", "group", "amount", "orders"),
            contract_versions={"payment_fact": "v1"},
            schema_fingerprint="schema:v2",
        )

        self.assertEqual(len(matched), 1)
        self.assertEqual(contract_drift, ())
        self.assertEqual(schema_drift, ())

    def test_store_dedupes_reusable_assets_by_content_and_keeps_latest_metadata(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-assets-dedupe", owner_id="analyst-1")
        topic = store.create_topic("thread-assets-dedupe", title="收入分析")

        store.save_analysis_assets(
            "thread-assets-dedupe",
            topic.topic_id,
            (
                {
                    "asset_type": "dimension_scan",
                    "status": "usable",
                    "dimensions": ("channel",),
                    "query_ref": "query:channel-scan",
                    "source_run_id": "run-01",
                },
            ),
        )
        store.save_analysis_assets(
            "thread-assets-dedupe",
            topic.topic_id,
            (
                {
                    "asset_type": "dimension_scan",
                    "status": "usable",
                    "dimensions": ("channel",),
                    "query_ref": "query:channel-scan",
                    "source_run_id": "run-02",
                },
            ),
        )

        assets = store.list_analysis_assets("thread-assets-dedupe", topic.topic_id)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["query_ref"], "query:channel-scan")
        self.assertEqual(assets[0]["source_run_id"], "run-02")

    def test_store_retains_latest_unique_assets_with_bound(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-assets-bound", owner_id="analyst-1")
        topic = store.create_topic("thread-assets-bound", title="收入分析")

        first_batch = tuple(
            {
                "asset_type": "dimension_scan",
                "status": "usable",
                "dimensions": ("channel",),
                "query_ref": f"query:{index:02d}",
                "source_run_id": f"run-{index:02d}",
            }
            for index in range(12)
        )
        second_batch = tuple(
            {
                "asset_type": "dimension_scan",
                "status": "usable",
                "dimensions": ("channel",),
                "query_ref": f"query:{index:02d}",
                "source_run_id": f"run-{index:02d}",
            }
            for index in range(5, 25)
        )

        store.save_analysis_assets("thread-assets-bound", topic.topic_id, first_batch)
        store.save_analysis_assets("thread-assets-bound", topic.topic_id, second_batch)

        assets = store.list_analysis_assets("thread-assets-bound", topic.topic_id)
        self.assertEqual(len(assets), 20)
        self.assertEqual(assets[0]["query_ref"], "query:05")
        self.assertEqual(assets[-1]["query_ref"], "query:24")
        self.assertEqual(
            len({(asset["query_ref"], asset["source_run_id"]) for asset in assets}),
            20,
        )

    def test_runtime_merges_topic_and_request_assets_in_manifest_and_run_request(self):
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
            prior_analysis_assets=(
                {
                    "asset_type": "claim_context_slot",
                    "status": "context_only",
                    "text": "直营渠道贡献偏高。",
                    "evidence_refs": ("segment_contribution:inline",),
                    "source_run_id": "run-claim-context",
                },
            ),
        )

        self.assertIsNotNone(result.run_request)
        self.assertEqual(
            result.run_request.prior_analysis_assets,
            (
                {
                    "asset_type": "dimension_scan",
                    "dimensions": ["channel"],
                    "dimension": "channel",
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                },
                {
                    "asset_type": "claim_context_slot",
                    "status": "context_only",
                    "text": "直营渠道贡献偏高。",
                    "evidence_refs": ["segment_contribution:inline"],
                    "source_run_id": "run-claim-context",
                },
            ),
        )
        manifest_assets = result.run_request.context_manifest["analysis_assets"]
        self.assertEqual(manifest_assets[0]["query_ref"], "query:channel-scan")
        self.assertEqual(manifest_assets[1]["asset_type"], "claim_context_slot")

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
                                        "wording_limit": "candidate",
                                        "limitations": ["needs_segment_sample_review"],
                                        "verifier_status": "passed",
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
        self.assertIn("claim_context_slot", asset_types)
        claim_context = next(asset for asset in assets if asset["asset_type"] == "claim_context_slot")
        self.assertEqual(tuple(claim_context["limitations"]), ("needs_segment_sample_review",))
        self.assertEqual(claim_context["verifier_status"], "passed")
        self.assertEqual(claim_context["wording_limit"], "candidate")
        self.assertFalse(claim_context["can_support_business_truth"])

    def test_claim_asset_requires_strong_wording_limit_for_business_truth_support(self):
        assets = build_analysis_assets(
            {
                "run_id": "run-claim-wording",
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {
                            "claim_groups": [
                                {
                                    "text": "候选判断不能升级成复用真值。",
                                    "evidence_refs": ["segment_contribution:inline"],
                                    "evidence_type": "candidate_mechanism",
                                    "strength": "high",
                                    "wording_limit": "quantified",
                                    "verifier_status": "passed",
                                },
                                {
                                    "text": "上下文证据也不能升级成复用真值。",
                                    "evidence_refs": ["outlier_scan:inline"],
                                    "evidence_type": "contextual_evidence",
                                    "strength": "high",
                                    "wording_limit": "supported",
                                    "verifier_status": "passed",
                                },
                                {
                                    "text": "量化支持可复用。",
                                    "evidence_refs": ["driver_decomposition:inline"],
                                    "evidence_type": "accounting_contribution",
                                    "strength": "high",
                                    "wording_limit": "quantified",
                                    "verifier_status": "passed",
                                },
                            ]
                        },
                    }
                ],
            }
        )

        candidate_claim, contextual_claim, quantified_claim = [
            asset for asset in assets if asset["asset_type"] == "claim_context_slot"
        ]
        self.assertEqual(candidate_claim["status"], "context_only")
        self.assertFalse(candidate_claim["can_support_business_truth"])
        self.assertEqual(candidate_claim["evidence_type"], "candidate_mechanism")
        self.assertEqual(candidate_claim["wording_limit"], "quantified")
        self.assertEqual(contextual_claim["status"], "context_only")
        self.assertFalse(contextual_claim["can_support_business_truth"])
        self.assertEqual(contextual_claim["evidence_type"], "contextual_evidence")
        self.assertEqual(contextual_claim["wording_limit"], "supported")
        self.assertEqual(quantified_claim["status"], "claim_supported")
        self.assertTrue(quantified_claim["can_support_business_truth"])
        self.assertEqual(quantified_claim["evidence_type"], "accounting_contribution")
        self.assertEqual(quantified_claim["wording_limit"], "quantified")

    def test_claim_asset_mixed_evidence_stays_context_only_even_when_first_ref_is_strong(self):
        assets = build_analysis_assets(
            {
                "run_id": "run-mixed-claim-wording",
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {
                            "claim_groups": [
                                {
                                    "text": "第一条 ref 很强，但整组证据里还有候选边界。",
                                    "evidence_refs": [
                                        "driver_decomposition:inline",
                                        "joint_attribution:inline",
                                    ],
                                    "evidence_type": "accounting_contribution",
                                    "evidence_types": [
                                        "accounting_contribution",
                                        "contextual_evidence",
                                    ],
                                    "strength": "high",
                                    "strengths": ["high", "medium"],
                                    "wording_limit": "quantified",
                                    "wording_limits": ["quantified", "contextual"],
                                    "verifier_status": "passed",
                                }
                            ]
                        },
                    }
                ],
            }
        )

        claim_context = next(asset for asset in assets if asset["asset_type"] == "claim_context_slot")
        self.assertEqual(claim_context["status"], "context_only")
        self.assertFalse(claim_context["can_support_business_truth"])
        self.assertEqual(
            tuple(claim_context["evidence_types"]),
            ("accounting_contribution", "contextual_evidence"),
        )
        self.assertEqual(
            tuple(claim_context["wording_limits"]),
            ("quantified", "contextual"),
        )

    def test_claim_asset_missing_wording_limit_stays_context_only(self):
        assets = build_analysis_assets(
            {
                "run_id": "run-missing-wording-limit",
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {
                            "claim_groups": [
                                {
                                    "text": "第二条证据缺 wording_limit 时不能复用成业务真值。",
                                    "evidence_refs": [
                                        "driver_decomposition:inline",
                                        "outlier_scan:inline",
                                    ],
                                    "evidence_type": "accounting_contribution",
                                    "evidence_types": [
                                        "accounting_contribution",
                                        "contextual_evidence",
                                    ],
                                    "strength": "high",
                                    "strengths": ["high", "medium"],
                                    "wording_limit": "quantified",
                                    "wording_limits": ["quantified", "missing"],
                                    "verifier_status": "passed",
                                }
                            ]
                        },
                    }
                ],
            }
        )

        claim_context = next(asset for asset in assets if asset["asset_type"] == "claim_context_slot")
        self.assertEqual(claim_context["status"], "context_only")
        self.assertFalse(claim_context["can_support_business_truth"])
        self.assertEqual(tuple(claim_context["wording_limits"]), ("quantified", "missing"))

    def test_claim_asset_missing_evidence_type_stays_context_only(self):
        assets = build_analysis_assets(
            {
                "run_id": "run-missing-evidence-type",
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {
                            "claim_groups": [
                                {
                                    "text": "第二条证据缺 evidence_type 时不能复用成业务真值。",
                                    "evidence_refs": [
                                        "driver_decomposition:inline",
                                        "outlier_scan:inline",
                                    ],
                                    "evidence_type": "accounting_contribution",
                                    "evidence_types": [
                                        "accounting_contribution",
                                        "missing",
                                    ],
                                    "strength": "high",
                                    "strengths": ["high", "medium"],
                                    "wording_limit": "quantified",
                                    "wording_limits": ["quantified", "supported"],
                                    "verifier_status": "passed",
                                }
                            ]
                        },
                    }
                ],
            }
        )

        claim_context = next(asset for asset in assets if asset["asset_type"] == "claim_context_slot")
        self.assertEqual(claim_context["status"], "context_only")
        self.assertFalse(claim_context["can_support_business_truth"])
        self.assertEqual(
            tuple(claim_context["evidence_types"]),
            ("accounting_contribution", "missing"),
        )

    def test_claim_asset_metadata_only_changes_dedupe_to_latest_payload(self):
        assets = merge_analysis_assets(
            (
                {
                    "asset_type": "claim_context_slot",
                    "status": "context_only",
                    "text": "直营渠道贡献较高。",
                    "evidence_refs": ("segment_contribution:inline",),
                    "target_metric": "paid_amount",
                    "scope": "all_users",
                    "time_window": "2026-07-01..2026-07-07",
                    "verifier_status": "failed",
                    "can_support_business_truth": False,
                    "source_run_id": "run-01",
                },
            ),
            (
                {
                    "asset_type": "claim_context_slot",
                    "status": "claim_supported",
                    "text": "直营渠道贡献较高。",
                    "evidence_refs": ("segment_contribution:inline",),
                    "target_metric": "paid_amount",
                    "scope": "all_users",
                    "time_window": "2026-07-01..2026-07-07",
                    "verifier_status": "passed",
                    "can_support_business_truth": True,
                    "source_run_id": "run-02",
                },
            ),
        )

        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["source_run_id"], "run-02")
        self.assertEqual(assets[0]["status"], "claim_supported")
        self.assertEqual(assets[0]["verifier_status"], "passed")
        self.assertTrue(assets[0]["can_support_business_truth"])

    def test_follow_up_run_reuses_persisted_dimension_scan_assets(self):
        class Provider:
            def configured(self):
                return True

            def binding_reason(self):
                return ""

            def plan(self, request, intent, accepted_graph):
                from bi_agent.runtime.clickhouse_revenue_rows import RevenueRowPlan

                return RevenueRowPlan(
                    sql_text=(
                        "SELECT period, group, channel, sum(amount) AS amount "
                        "FROM t GROUP BY period, group, channel"
                    ),
                    query_id=f"{request['run_id']}:dimension_scan",
                    required_fields=("period", "group", "amount"),
                    dimension_keys=("channel",),
                )

            def fetch(self, plan):
                from bi_agent.runtime.clickhouse_revenue_rows import RevenueRowsResult

                return RevenueRowsResult(
                    ok=True,
                    rows=(
                        {
                            "period": "2026-07-07",
                            "group": "baseline",
                            "amount": 100,
                            "paid_users": 10,
                            "orders": 12,
                            "first_paid_users": 3,
                            "channel": "A",
                        },
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "amount": 130,
                            "paid_users": 11,
                            "orders": 14,
                            "first_paid_users": 4,
                            "channel": "A",
                        },
                    ),
                    query_hash="hash-channel-scan",
                    query_id=plan.query_id,
                    result_refs=("hash-channel-scan",),
                )

        def workflow(request):
            return run_pattern_workflow(
                {
                    **request,
                    "llm_client": FakeLLMClient(
                        {
                            "analysis_route": {
                                "requested_nodes": ["segment_contribution", "answer_verify"]
                            }
                        }
                    ),
                }
            )

        store = InMemoryConversationStore()
        store.create_thread("thread-follow-up-assets", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=workflow, row_provider=Provider())

        first = core.run_message(
            thread_id="thread-follow-up-assets",
            run_id="run-initial-scan",
            user_message="哪个渠道影响最大？",
            role="analyst",
        )
        second = core.run_message(
            thread_id="thread-follow-up-assets",
            run_id="run-follow-up-scan",
            user_message="继续看哪个渠道影响最大",
            role="analyst",
        )

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        assets = store.list_analysis_assets("thread-follow-up-assets", first["topic_id"])
        self.assertTrue(any(asset["asset_type"] == "dimension_scan" for asset in assets))
        prior_assets = store.runs["run-follow-up-scan"]["request"]["prior_analysis_assets"]
        self.assertTrue(any(asset["asset_type"] == "dimension_scan" for asset in prior_assets))
        compiler_plan = second["answer_package"]["admin_audit"]["compiler_runtime_plan"]
        self.assertIn("dimension_scan_reuse", compiler_plan["query_intents"])
        self.assertNotIn("dimension_scan", compiler_plan["query_intents"])
        compiled = compile_graph(
            question_family="segment_or_factor_attribution",
            target_metric="paid_amount",
            requested_nodes=("segment_contribution", "answer_verify"),
            question_text="继续看哪个渠道影响最大",
            bound_context={
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
                "windows": {"time_window": "2024-01..2026-05"},
                "baselines": (),
                "permission_scope": "analyst",
                "snapshot_version": "2026H1",
                "contract_versions": {"runtime": "contracts-v1"},
                "schema_fingerprint": "contracts-v1:2026H1",
            },
            prior_analysis_assets=tuple(prior_assets),
        )
        self.assertIn("dimension_scan_reuse", compiled.runtime_plan["query_intents"])
        self.assertNotIn("dimension_scan", compiled.runtime_plan["query_intents"])


if __name__ == "__main__":
    unittest.main()
