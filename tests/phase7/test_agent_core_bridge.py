import unittest
from pathlib import Path
from unittest.mock import patch

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult


class AgentCoreBridgeTest(unittest.TestCase):
    def test_agent_core_runs_workflow_and_persists_answer_package(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)

        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-agent-core",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["accepted_graph"], [])
        self.assertEqual(store.runs["run-agent-core"]["status"], "completed")
        self.assertTrue(store.context_manifests)
        self.assertTrue(store.answer_packages)
        package = store.answer_packages["run-agent-core"]
        self.assertEqual(package["run_id"], "run-agent-core")
        self.assertEqual(package["sections"][0]["payload"]["answer_text"], "这是持久化的业务回答。")
        topic = store.current_topic("thread-agent-core")
        self.assertIsNotNone(store.latest_artifact_for_topic(topic.topic_id))
        self.assertTrue(
            any(event["event_type"] == "answer_package_recorded" for event in store.audit_events)
        )

    def test_agent_core_returns_context_manifest_with_current_run_evidence_refs(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core-evidence", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)

        result = core.run_message(
            thread_id="thread-agent-core-evidence",
            run_id="run-agent-core-evidence",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        refs = {
            item["source_ref"]
            for item in result["context_manifest"]["items"]
            if item.get("can_support_claims") is True
        }
        self.assertIn("evidence:fake-workflow", refs)
        self.assertTrue(result["context_manifest"]["can_support_claims"])

    def test_agent_core_creates_thread_before_initial_run_insert(self):
        store = StrictThreadStore()
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)

        result = core.run_message(
            thread_id="thread-agent-core-strict",
            run_id="run-agent-core-strict",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "completed")
        self.assertIn("thread-agent-core-strict", store.threads)

    def test_agent_core_does_not_run_langgraph_for_capability_question(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        calls = []

        def runner(_request):
            calls.append(_request)
            return fake_workflow(_request)

        core = ConversationAgentCore(store, workflow_runner=runner)
        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-capability-question",
            user_message="你现在能看哪些数据？",
            role="analyst",
        )

        self.assertEqual(result["status"], "completed_without_workflow")
        self.assertEqual(calls, [])
        self.assertIsNone(store.runs["run-capability-question"]["answer_package"])

    def test_agent_core_waits_for_structured_clarification_without_running_workflow(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        calls = []

        def runner(_request):
            calls.append(_request)
            return fake_workflow(_request)

        core = ConversationAgentCore(store, workflow_runner=runner)
        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-needs-clarification",
            user_message="这个月是不是变好了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "waiting_for_clarification")
        self.assertEqual(calls, [])
        self.assertIn("clarification", result)
        self.assertEqual(store.runs["run-needs-clarification"]["status"], "waiting_for_clarification")
        self.assertEqual(
            store.runs["run-needs-clarification"]["request"]["clarification"]["clarification_id"],
            result["clarification"]["clarification_id"],
        )
        self.assertTrue(
            any(
                event["event_type"] == "clarification_requested"
                and event["run_id"] == "run-needs-clarification"
                for event in store.audit_events
            )
        )

    def test_agent_core_failed_workflow_still_returns_context_manifest(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=fake_failed_workflow)

        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-failed-workflow",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("context_manifest", result)
        self.assertTrue(result["context_manifest"])

    def test_live_conversation_case_schema_supports_clarification_resume(self):
        from tools.phase7.run_live_conversation_system_test import load_cases

        cases = load_cases("evals/phase7/conversation_scenarios.yaml")
        case = next(item for item in cases if item["id"] == "q2_q1_wajespecial_long_followup")
        self.assertGreaterEqual(len(case["turns"]), 4)
        self.assertIs(case["turns"][3]["expect"]["allow_clarification"], True)
        self.assertIn("clarification_response", case["turns"][3])
        self.assertFalse(any("case_id" in item for item in cases))

    def test_live_conversation_cases_include_long_thread_stress_set(self):
        from tools.phase7.run_live_conversation_system_test import load_cases

        cases = load_cases("evals/phase7/conversation_scenarios.yaml")
        matches = [item for item in cases if item["id"] == "q2_q1_long_thread_stress"]
        self.assertTrue(matches)
        case = matches[0]
        required = {
            capability
            for turn in case["turns"]
            for capability in turn.get("expect", {}).get("required_capabilities", ())
        }

        self.assertEqual(case["group"], "long_thread_stress")
        self.assertGreaterEqual(len(case["turns"]), 8)
        self.assertTrue(any("clarification_response" in turn for turn in case["turns"]))
        self.assertTrue(
            {
                "driver_decomposition",
                "segment_contribution",
                "joint_attribution",
                "outlier_scan",
                "outlier_contribution",
                "answer_verify",
            }.issubset(required)
        )

    def test_live_conversation_harness_runs_clarification_and_resumes_same_topic(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import load_cases, run_case

        case = next(
            item
            for item in load_cases("evals/phase7/conversation_scenarios.yaml")
            if item["id"] == "q2_q1_wajespecial_long_followup"
        )

        with TemporaryDirectory() as tmpdir:
            result = run_case(
                ConversationAgentCore.from_environment(),
                case,
                Path(tmpdir),
            )

        self.assertEqual(result["status"], "passed")
        self.assertIn("run_id", result)
        self.assertIn("topic_id", result)
        self.assertIn("answer_package", result)
        self.assertIn("context_manifest", result)
        self.assertIn("accepted_graph", result)
        self.assertIn("llm_calls", result)
        self.assertIn("quality_review", result)
        clarification_turn = result["turns"][3]
        second_turn = result["turns"][1]
        third_turn = result["turns"][2]
        self.assertEqual(clarification_turn["status"], "waiting_for_clarification")
        self.assertEqual(clarification_turn["resumed_status"], "completed")
        self.assertEqual(clarification_turn["topic_id"], clarification_turn["resumed_topic_id"])
        self.assertEqual(result["turns"][0]["topic_id"], result["turns"][1]["topic_id"])
        self.assertEqual(result["turns"][1]["topic_id"], result["turns"][2]["topic_id"])
        self.assertEqual(second_turn["expectation_review"]["missing_required_capabilities"], [])
        self.assertEqual(third_turn["expectation_review"]["missing_required_capabilities"], [])
        for turn in result["turns"]:
            with self.subTest(turn=turn["index"]):
                self.assertTrue(turn["expectation_review"]["intent_passed"])
                self.assertTrue(turn["expectation_review"]["topic_relation_passed"])
                self.assertTrue(turn["expectation_review"]["context_manifest_present"])
                self.assertTrue(turn["expectation_review"]["context_manifest_can_support_claims"])
                self.assertTrue(turn["expectation_review"]["claim_support_policy_passed"])
                self.assertGreater(turn["expectation_review"]["claim_evidence_review"]["claim_count"], 0)
                self.assertEqual(
                    turn["expectation_review"]["claim_evidence_review"]["unsupported_evidence_refs"],
                    [],
                )
                self.assertEqual(turn["expectation_review"]["missing_final_answer_text"], [])
        self.assertIn("outlier_contribution", clarification_turn["resumed_accepted_graph"])

    def test_agent_core_passes_clarification_answer_as_workflow_choice(self):
        captured: dict[str, object] = {}

        def workflow(request):
            captured.update(request)
            return fake_workflow(request)

        store = InMemoryConversationStore()
        store.create_thread("thread-clarification-choice", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=workflow)

        first = core.run_message(
            thread_id="thread-clarification-choice",
            run_id="run-initial-choice",
            user_message="Q2 相比 Q1 付费金额为什么变了？",
        )
        captured.clear()
        waiting = core.run_message(
            thread_id="thread-clarification-choice",
            run_id="run-waiting-choice",
            user_message="如果去掉异常天还成立吗？",
        )
        resumed = core.run_message(
            thread_id="thread-clarification-choice",
            run_id="run-resumed-choice",
            user_message="按日粒度，移除贡献最大的正向日期后复算，不做订单级明细剔除。",
        )

        self.assertEqual(first["status"], "completed")
        self.assertEqual(waiting["status"], "waiting_for_clarification")
        self.assertEqual(resumed["status"], "completed")
        self.assertIn("clarification_choice", captured)
        self.assertEqual(captured["clarification_choice"]["outlier_removal_strategy"], "daily_remove_top_positive_day")
        self.assertIn("answer_text", captured["clarification_choice"])

    def test_agent_core_passes_row_provider_to_workflow_request(self):
        captured: dict[str, object] = {}
        provider = object()

        def workflow(request):
            captured.update(request)
            return fake_workflow(request)

        store = InMemoryConversationStore()
        store.create_thread("thread-row-provider", owner_id="analyst-1")
        core = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            row_provider=provider,
        )

        result = core.run_message(
            thread_id="thread-row-provider",
            run_id="run-row-provider",
            user_message="昨天付费金额为什么上涨/下跌？",
        )

        self.assertEqual(result["status"], "completed")
        self.assertIs(captured["row_provider"], provider)

    def test_agent_core_from_environment_real_clickhouse_configures_row_provider(self):
        from bi_agent.runtime.clickhouse_revenue_rows import ClickHouseRevenueRows

        with patch(
            "bi_agent.conversation.agent_core.PostgresConversationStore.from_env",
            return_value=InMemoryConversationStore(),
        ):
            core = ConversationAgentCore.from_environment(real_clickhouse=True)

        self.assertIsInstance(core.row_provider, ClickHouseRevenueRows)

    def test_follow_up_hints_route_user_mix_and_high_value_capabilities(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-follow-up-hints", owner_id="analyst-1")
        runtime.handle_message("thread-follow-up-hints", "Q2 相比 Q1 付费金额为什么变了？")

        result = runtime.handle_message(
            "thread-follow-up-hints",
            "新老用户和高价值用户的用户质量分别怎么看？",
        )

        self.assertIn("user_mix_contribution", result.run_request.requested_nodes)
        self.assertIn("high_value_user_contribution", result.run_request.requested_nodes)

    def test_revenue_diagnostic_language_routes_to_general_capabilities(self):
        cases = (
            (
                "最近付费金额是否存在固定规律，比如周末更高、月初更高、晚上更高？"
                "这个规律主要由哪个渠道、地区、用户类型或玩法带动？",
                {"pattern_scan", "segment_contribution", "joint_attribution", "answer_verify"},
            ),
            (
                "当前收入健康吗？是靠正常用户增长带动，还是靠少数大额用户、"
                "短期活动或异常渠道拉动？收入结构里最大的风险点是什么？",
                {
                    "driver_decomposition",
                    "user_mix_contribution",
                    "high_value_user_contribution",
                    "outlier_scan",
                    "data_quality_profile",
                    "event_evidence",
                    "answer_verify",
                },
            ),
            (
                "相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？"
                "哪些指标偏离了正常水平？",
                {
                    "compare_periods",
                    "rolling_window_compare",
                    "driver_decomposition",
                    "answer_verify",
                },
            ),
            (
                "这个结论的数据证据够不够？是否存在数据延迟、渠道归因异常、"
                "支付状态缺失、重复订单或异常用户影响判断？",
                {
                    "data_quality_profile",
                    "segment_contribution",
                    "joint_attribution",
                    "outlier_scan",
                    "answer_verify",
                },
            ),
        )

        for message, expected_nodes in cases:
            with self.subTest(message=message):
                store = InMemoryConversationStore()
                runtime = ConversationRuntime(store)
                store.create_thread("thread-revenue-diagnostics", owner_id="analyst-1")
                runtime.handle_message(
                    "thread-revenue-diagnostics",
                    "昨天付费金额为什么变化？",
                )

                result = runtime.handle_message("thread-revenue-diagnostics", message)

                self.assertIsNotNone(result.run_request)
                self.assertTrue(expected_nodes.issubset(set(result.run_request.requested_nodes)))

    def test_first_business_question_creates_topic_then_time_wording_inherits(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-revenue-topic-reuse", owner_id="analyst-1")

        first = runtime.handle_message(
            "thread-revenue-topic-reuse",
            "昨天付费金额为什么上涨/下跌？",
        )
        second = runtime.handle_message(
            "thread-revenue-topic-reuse",
            "相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？",
        )

        self.assertEqual(first.turn_intent.intent, "new_topic")
        self.assertEqual(first.topic_relation, "new_topic")
        self.assertEqual(second.turn_intent.intent, "follow_up")
        self.assertEqual(second.topic_relation, "inherit_current")
        self.assertEqual(first.topic_id, second.topic_id)

    def test_live_conversation_cases_include_revenue_diagnostic_question_set(self):
        from tools.phase7.run_live_conversation_system_test import load_cases

        expected_questions = [
            "昨天付费金额为什么上涨/下跌？主要是首充人数、付费频次、单笔付费金额，还是支付成功率等因素变化导致的",
            "最近付费金额是否存在固定规律，比如周末更高、月初更高、晚上更高？这个规律主要由哪个渠道、地区、用户类型或玩法带动？",
            "昨天的活动、投放预算、素材更换、版本更新、支付通道、节日或外部事件，是否影响了付费金额？",
            "当前收入健康吗？是靠正常用户增长带动，还是靠少数大额用户、短期活动或异常渠道拉动？收入结构里最大的风险点是什么？",
            "昨天收入变化最大的是哪个一级渠道、地区、设备、包、支付方式或玩法？对收入影响最大的 3 个因子分别是什么？",
            "昨天有没有异常波动？如果有，是哪个渠道、支付通道、地区、设备、玩法或大额用户造成的？",
            "相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？哪些指标偏离了正常水平？",
            "这个结论的数据证据够不够？是否存在数据延迟、渠道归因异常、支付状态缺失、重复订单或异常用户影响判断？",
        ]
        cases = load_cases("evals/phase7/conversation_scenarios.yaml")
        case = next(
            item
            for item in cases
            if item["id"] == "paid_amount_revenue_diagnostics_8_question_set"
        )

        self.assertEqual(case["group"], "production_revenue_diagnostics")
        self.assertEqual([turn["user"] for turn in case["turns"]], expected_questions)
        self.assertEqual(case["turns"][0]["expect"]["topic_relation"], "create")
        self.assertTrue(
            all(turn["expect"]["topic_relation"] == "inherit" for turn in case["turns"][1:])
        )
        self.assertTrue(all(turn["expect"].get("major_nodes") for turn in case["turns"]))

    def test_live_harness_rejects_claim_refs_without_traceable_source(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["artifact:missing"],
                                    }
                                ],
                            }
                        },
                        {
                            "payload": {
                                "evidence": [
                                    {
                                        "evidence_ref": "artifact:missing",
                                        "evidence_type": "artifact",
                                    }
                                ]
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "can_support_claims": True,
                    "items": [],
                },
            },
            [],
        )

        self.assertFalse(review["claim_support_policy_passed"])
        self.assertEqual(
            review["claim_evidence_review"]["unsupported_evidence_refs"],
            ["artifact:missing"],
        )

    def test_live_harness_accepts_current_run_evidence_manifest_refs_without_prefix(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["coverage_block:run-1"],
                                        "context_manifest_ref": "manifest-coverage-block",
                                        "reuse_decisions": [
                                            {
                                                "decision": "rerun",
                                                "result_ref": "",
                                                "reason": "current_run_evidence",
                                            }
                                        ],
                                    }
                                ],
                            }
                        },
                        {
                            "payload": {
                                "evidence": [
                                    {
                                        "evidence_ref": "coverage_block:run-1",
                                        "evidence_type": "insufficient",
                                    }
                                ]
                            }
                        },
                    ]
                },
                "context_manifest": {
                    "manifest_id": "manifest-coverage-block",
                    "can_support_claims": True,
                    "items": [
                        {
                            "source_type": "evidence",
                            "source_ref": "coverage_block:run-1",
                            "can_support_claims": True,
                            "claim_use": "evidence",
                        }
                    ],
                },
            },
            [],
        )

        self.assertTrue(review["claim_support_policy_passed"])
        self.assertEqual(review["claim_evidence_review"]["unsupported_evidence_refs"], [])
        self.assertTrue(review["passed"])

    def test_live_harness_rejects_context_only_manifest_refs_for_claims(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["topic-1"],
                                    }
                                ],
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "can_support_claims": False,
                    "items": [
                        {
                            "source_ref": "topic-1",
                            "can_support_claims": False,
                            "claim_use": "context_only",
                        }
                    ],
                },
            },
            [],
        )

        self.assertFalse(review["claim_support_policy_passed"])
        self.assertEqual(
            review["claim_evidence_review"]["unsupported_evidence_refs"],
            ["topic-1"],
        )

    def test_live_harness_rejects_topic_refs_even_when_marked_claim_supporting(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["topic-1"],
                                    }
                                ],
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "can_support_claims": True,
                    "items": [
                        {
                            "source_type": "topic",
                            "source_ref": "topic-1",
                            "can_support_claims": True,
                            "claim_use": "evidence",
                        }
                    ],
                },
            },
            [],
        )

        self.assertFalse(review["claim_support_policy_passed"])
        self.assertEqual(
            review["claim_evidence_review"]["unsupported_evidence_refs"],
            ["topic-1"],
        )

    def test_live_harness_rejects_claims_missing_manifest_or_reuse_decision(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["evidence:1"],
                                    }
                                ],
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "manifest_id": "context-1",
                    "can_support_claims": True,
                    "items": [
                        {
                            "source_type": "evidence",
                            "source_ref": "evidence:1",
                            "can_support_claims": True,
                            "claim_use": "evidence",
                        }
                    ],
                },
            },
            [],
        )

        self.assertFalse(review["claim_support_policy_passed"])
        self.assertEqual(review["claim_evidence_review"]["missing_context_manifest_ref"], [0])
        self.assertEqual(review["claim_evidence_review"]["missing_reuse_decision_indexes"], [0])

    def test_live_harness_loads_local_env_without_overriding_shell(self):
        import os
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import load_env_file

        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "WAJE_RUNTIME_DATABASE_URL=postgres://local\nWAJE_LLM_MODEL=deepseek-chat\n",
                encoding="utf-8",
            )
            old_database_url = os.environ.pop("WAJE_RUNTIME_DATABASE_URL", None)
            old_model = os.environ.get("WAJE_LLM_MODEL")
            os.environ["WAJE_LLM_MODEL"] = "shell-model"
            try:
                loaded = load_env_file(str(env_path))
                self.assertIn("WAJE_RUNTIME_DATABASE_URL", loaded)
                self.assertNotIn("WAJE_LLM_MODEL", loaded)
                self.assertEqual(os.environ["WAJE_RUNTIME_DATABASE_URL"], "postgres://local")
                self.assertEqual(os.environ["WAJE_LLM_MODEL"], "shell-model")
            finally:
                if old_database_url is None:
                    os.environ.pop("WAJE_RUNTIME_DATABASE_URL", None)
                else:
                    os.environ["WAJE_RUNTIME_DATABASE_URL"] = old_database_url
                if old_model is None:
                    os.environ.pop("WAJE_LLM_MODEL", None)
                else:
                    os.environ["WAJE_LLM_MODEL"] = old_model

    def test_live_harness_reads_quality_gate_for_strict_mode(self):
        from tools.phase7.run_live_conversation_system_test import (
            _strict_quality_failed,
            _quality_review,
        )

        review = _quality_review(
            {
                "quality_gate": {
                    "direct_answer": True,
                    "has_verified_claims": True,
                    "verified_claim_preserved": False,
                    "business_insight_present": True,
                    "followups_one_intent": False,
                    "issues": ["missing_verified_claim"],
                }
            }
        )

        self.assertEqual(
            review,
            {
                "direct_answer": True,
                "has_verified_claims": True,
                "verified_claim_preserved": False,
                "business_insight_present": True,
                "followups_one_intent": False,
            },
        )
        self.assertTrue(_strict_quality_failed({"quality_review": review}))

    def test_live_harness_uses_fresh_thread_for_each_case_run(self):
        from tools.phase7.run_live_conversation_system_test import _case_thread_id

        first = _case_thread_id({"id": "q2_q1_wajespecial_long_followup"})
        second = _case_thread_id({"id": "q2_q1_wajespecial_long_followup"})

        self.assertTrue(first.startswith("live-q2_q1_wajespecial_long_followup-"))
        self.assertTrue(second.startswith("live-q2_q1_wajespecial_long_followup-"))
        self.assertNotEqual(first, second)

    def test_live_harness_separates_real_and_dry_run_artifacts(self):
        from tools.phase7.run_live_conversation_system_test import (
            _default_artifact_dir,
            _run_mode,
        )

        self.assertEqual(
            _default_artifact_dir(real_llm=False, real_clickhouse=False),
            Path("artifacts/phase7/live-conversation-dry-run"),
        )
        self.assertEqual(
            _default_artifact_dir(real_llm=True, real_clickhouse=True),
            Path("artifacts/phase7/live-conversation-real"),
        )
        self.assertEqual(_run_mode(real_llm=False, real_clickhouse=False), "dry_run")
        self.assertEqual(_run_mode(real_llm=True, real_clickhouse=True), "real_llm_real_clickhouse")


def fake_workflow(request):
    manifest = request.get("context_manifest") or {}
    reuse_decisions = list(
        request.get("reuse_decisions")
        or [{"decision": "rerun", "result_ref": "", "reason": "test_default"}]
    )
    return WorkflowRunResult(
        status="draft",
        run_id=request["run_id"],
        answer_package={
            "run_id": request["run_id"],
            "status": "draft",
            "snapshot_id": "2026H1",
            "permission_scope": "analyst",
            "follow_up_context": "这轮回答后可以继续追问渠道、异常和口径变化。",
            "sections": [
                {
                    "id": "summary",
                    "visibility": "business_summary",
                    "payload": {
                        "answer_text": "这是持久化的业务回答。",
                        "claims": [
                            {
                                "text": "这是持久化的业务回答。",
                                "evidence_refs": ["evidence:fake-workflow"],
                                "context_manifest_ref": str(manifest.get("manifest_id") or ""),
                                "reuse_decisions": reuse_decisions,
                            }
                        ],
                    },
                },
                {
                    "id": "evidence",
                    "visibility": "aggregate_evidence",
                    "payload": {
                        "evidence": [
                            {
                                "evidence_ref": "evidence:fake-workflow",
                                "evidence_type": "statistical_association",
                                "strength": "medium",
                            }
                        ]
                    },
                }
            ],
            "admin_audit": {"verifier": {"status": "passed"}},
        },
        artifact_path="artifacts/phase-7/run-agent-core/answer_package.json",
        checkpoint_events=({"node": "persist_artifact", "status": "completed"},),
    )


def fake_failed_workflow(request):
    return WorkflowRunResult(
        status="failed",
        run_id=request["run_id"],
        failure_reason="synthetic_failure",
    )


class StrictThreadStore(InMemoryConversationStore):
    def upsert_run(self, run_id, *, thread_id, turn_id="", topic_id="", status, request=None):
        if thread_id not in self.threads:
            raise AssertionError("thread must exist before run insert")
        return super().upsert_run(
            run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic_id,
            status=status,
            request=request,
        )


if __name__ == "__main__":
    unittest.main()
