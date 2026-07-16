from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import unittest

import yaml

from bi_agent.conversation import models as conversation_models
from bi_agent.conversation.agent_core import _build_clarification_source_envelope
from bi_agent.conversation.runtime import (
    ConversationRuntime,
    _can_read_scope,
    _classify_intent,
    _looks_like_clarification_answer,
    _metric_business_label_vocabulary,
    _mentioned_metrics,
    _needs_clarification,
    _should_use_llm_orchestrator,
)
from bi_agent.conversation.runtime import _build_clarification
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


ROOT = Path(__file__).resolve().parents[2]
CASE_FILE = ROOT / "evals" / "phase7" / "conversation_scenarios.yaml"


def _cases():
    return yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))["cases"]


def _persist_clarification_source_envelope(
    runtime: ConversationRuntime,
    result,
    *,
    question: str,
    analysis_context=None,
):
    state = runtime.store.get_open_clarification(result.context_manifest.thread_id)
    thread = runtime.store.get_thread(result.context_manifest.thread_id)
    runtime.store.upsert_run(
        state.run_id,
        thread_id=thread.thread_id,
        topic_id=state.topic_id,
        status="waiting_for_clarification",
        request={
            "clarification_source_envelope": _build_clarification_source_envelope(
                source_run_id=state.run_id,
                source_thread_id=thread.thread_id,
                source_topic_id=state.topic_id,
                source_owner_id=thread.owner_id,
                question=question,
                analysis_context=analysis_context or {},
                clarification=result.clarification.to_dict(),
            )
        },
    )


class ConversationRuntimeTest(unittest.TestCase):
    def test_exact_open_clarification_option_precedes_topic_heuristics(self):
        option = "周末规律（周六/周日与工作日比较）"
        state = conversation_models.ClarificationState(
            run_id="run-pattern-choice",
            topic_id="topic-pattern-choice",
            question="优先分析哪种规律？",
            options=[
                conversation_models.ClarificationOption(
                    option_id="pattern-choice-1",
                    label=option,
                    description=option,
                    recommended=True,
                )
            ],
        )

        self.assertTrue(_looks_like_clarification_answer(option, state))

    def test_historical_write_action_descriptions_remain_business_analysis(self):
        for message in (
            "分析优惠券发放前后付费金额是否变化",
            "复盘奖励下发期间的收入表现",
            "分析消息发送实验对付费金额的影响",
            "给实验组发放优惠券后的付费金额变化如何？",
            "向召回用户发送消息前后，收入表现有什么差异？",
            "对高价值用户下发奖励期间的收入进行复盘。",
            "对比‘再发优惠券’实验组与对照组的收入表现",
            "给实验组的报告里，发放优惠券数量是多少",
            "分析“发送消息”活动的收入变化",
            "发放优惠券之后的付费金额变化如何？",
            "发放过优惠券，分析活动期间收入变化",
        ):
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), "follow_up")

    def test_analysis_constraints_that_challenge_evidence_are_not_corrections(self):
        for message in (
            "不要直接归因，先检查证据是否充分",
            "不要急着说活动导致增长，先看数据能否支撑",
            "不要把相关性改成因果结论，先检查证据是否充分",
            "不要调整为因果结论，先看数据能否支撑",
            "不要把相关性改成因果结论，只报告观察到的差异",
            "不要调整为因果结论，先报告相关性和限制",
        ):
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), "challenge")

    def test_execution_write_requests_remain_unsupported(self):
        for message in (
            "给所有用户发优惠券。",
            "请向实验组发送消息。",
            "现在给高价值用户下发奖励。",
            "请给实验组发优惠券后再分析效果。",
            "现在下发奖励，然后看收入。",
            "此前复盘过收入，现在给高价值用户下发奖励。",
            "分析优惠券发放前后表现，然后向实验组推送消息。",
            "请你给实验组发送消息。",
            "麻烦你向高价值用户下发奖励。",
            "帮忙给召回用户发优惠券。",
            "分析优惠券发放前后表现，并向实验组推送消息。",
            "分析优惠券发放前后表现，给新用户发送消息。",
            "此前复盘过收入，给新用户发放优惠券。",
            "给实验组把优惠券发放出去。",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    _classify_intent(message, False),
                    "unsupported_request",
                )

    def test_current_write_action_clauses_remain_hard_guarded(self):
        for message in (
            "给实验组发放优惠券后直接分析效果。",
            "发放优惠券后直接分析效果。",
            "给实验组发送消息，随后分析付费变化。",
            "向高价值用户下发奖励，同时查看收入表现。",
            "发放优惠券，接着分析收入变化。",
            "先分析收入并发放优惠券。",
            "先分析收入：给实验组发放优惠券。",
            "先看渠道\n请向实验组发送消息。",
            "请向实验组发放一张仅限七天内使用且不能与其他活动叠加的召回优惠券。",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    _classify_intent(message, False),
                    "unsupported_request",
                )

    def test_quoted_execution_payloads_do_not_bypass_write_guard(self):
        for message in (
            "请向实验组发送“召回消息”",
            "请给实验组发放‘七日召回优惠券’",
            "请执行“给实验组发放优惠券”",
            "请执行《向高价值用户下发奖励》",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    _classify_intent(message, False),
                    "unsupported_request",
                )

    def test_analytic_then_write_sequences_remain_hard_guarded(self):
        for message in (
            "分析收入后向实验组发送消息",
            "先分析收入后给实验组发放优惠券",
            "查看趋势后向高价值用户下发奖励",
            "分析完收入就向实验组发送消息",
            "分析完趋势便给实验组发券",
            "统计完渠道表现随即向召回用户推送消息",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    _classify_intent(message, False),
                    "unsupported_request",
                )

    def test_completed_actions_and_nominalized_statistics_remain_analysis(self):
        for message in (
            "昨天给实验组发了优惠券，分析今天收入变化",
            "上周向召回用户发送了一条消息，复盘后续收入",
            "给实验组的优惠券发放数量是多少",
            "向高价值用户的奖励下发覆盖率是多少",
        ):
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), "follow_up")

    def test_write_action_synonyms_follow_the_same_grammar_contract(self):
        for message in (
            "请向实验组发送召回通知",
            "请向实验组发送短信",
            "请给实验组发券",
            "给实验组发提醒",
            "向所有用户发送公告",
            "给召回用户发站内信",
            "给实验组发活动提醒",
            "给所有用户发系统公告",
            "给召回用户发召回站内信",
            "根据分析结果给用户发活动提醒",
            "请给过去三十天完成注册但尚未参与活动的高价值新用户发一张七日召回优惠券",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    _classify_intent(message, False),
                    "unsupported_request",
                )

        for message in (
            "分析召回通知发送前后的收入变化",
            "短信发送数量是多少",
            "发券后的收入表现如何",
            "分析“给实验组发送召回通知”活动的收入表现",
            "分析站内信发送前后的收入变化",
            "上周给用户发了提醒",
            "公告发送数量是多少",
            "上周给用户发了系统公告",
            "分析召回站内信发送前后的收入变化",
            "活动提醒发送数量是多少",
            "研发活动提醒的效果如何",
        ):
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), "follow_up")

    def test_write_capability_boundary_uses_governing_business_grammar(self):
        cases = (
            ("分析收入后向实验组赠送权益", "unsupported_request"),
            ("研究趋势后给高价值用户派发权益", "unsupported_request"),
            ("先复盘收入，再向召回用户投放", "unsupported_request"),
            ("请执行给实验组派送", "unsupported_request"),
            ("向实验组通知", "unsupported_request"),
            ("请执行“向实验组赠送权益”", "unsupported_request"),
            ("请安排‘给高价值用户派发权益’", "unsupported_request"),
            ("请分析“向实验组发送消息”活动效果", "follow_up"),
            ("请统计‘给实验组发放优惠券’的覆盖人数", "follow_up"),
            ("请看一下“向高价值用户下发奖励”的后续表现", "follow_up"),
            ("给我看投资收益", "follow_up"),
            ("给我分析推广效果", "follow_up"),
            ("分析投产后的收入表现", "follow_up"),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), expected)

    def test_completed_and_nominalized_write_events_stay_analytical(self):
        for message in (
            "上周向用户发送了一条消息",
            "给实验组发过优惠券",
            "给实验组的奖励发送比例是多少",
            "给召回用户的消息推送情况如何",
            "向实验组的优惠券发放量是多少",
            "向高价值用户的权益赠送人数是多少",
        ):
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), "follow_up")

        self.assertEqual(
            _classify_intent("现在给实验组发了优惠券", False),
            "unsupported_request",
        )

    def test_negative_causal_ceiling_is_order_independent(self):
        for message in (
            "归因结论不要超过现有证据",
            "因果关系不得直接认定",
            "归因需要避免过度",
            "不能把增长解释为活动导致",
            "结论无需做因果归因",
            "不许直接归因于活动",
        ):
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), "challenge")

    def test_write_governor_scope_stays_bound_to_its_event(self):
        for message in (
            "请根据刚才的分析结果向实验组发送消息",
            "请根据上周的分析结果给实验组发券",
            "请根据昨天的分析结果向高价值用户推送",
            "请根据此前的分析结果向召回用户发送",
            "按“向实验组发送消息”方案执行",
            "“给实验组发券”请立即执行",
            "帮我分析收入后向实验组发送消息",
            "现在回看趋势后向高价值用户推送消息",
            "发一封电子邮件",
            "请根据分析得出的上周数据向实验组发送消息",
            "请根据分析已确认的结论向实验组发送消息",
            "把邮件发给实验组",
            "把优惠券发给召回用户",
            "请发给实验组",
            "把奖励发到账户",
            "发消息给实验组",
            "发邮件给实验组",
            "把消息发往实验组",
            "把邮件发至实验组",
            "发一下消息给实验组",
            "请发一下给实验组",
            "把消息发一下到实验组",
            "把邮件发一下往实验组",
            "把奖励发一下至账户",
            "请根据分析结果向实验组发送消息",
            "结合分析报告给实验组推送消息",
            "按照分析结论向高价值用户下发奖励",
            "基于分析数据给召回用户发券",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    _classify_intent(message, False),
                    "unsupported_request",
                )

        for message in (
            "“给实验组发放优惠券”活动的覆盖人数请统计",
            "“向高价值用户推送消息”方案请分析效果",
            "帮我分析向实验组发送消息的效果",
            "麻烦复盘给实验组发放优惠券后的收入",
            "现在回看向高价值用户推送消息的覆盖情况",
            "研发投入变化如何",
            "开发渠道表现如何",
            "发现收入异常后分析原因",
            "帮我基于上周发放数据分析收入",
            "“向实验组发送消息”的执行效果如何",
            "分析向实验组发送消息能否提升收入",
            "请分析向实验组推送消息与收入变化的关系",
            "研究给实验组发放优惠券是否影响收入",
            "请分析上周活动向实验组发送消息与收入变化的关系",
            "帮我研究活动期间给实验组推送消息的影响",
            "麻烦复盘昨天向召回用户发送消息后的收入表现",
        ):
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), "follow_up")

    def test_explicit_prior_answer_corrections_remain_corrections(self):
        for message in (
            "不要按自然月，按活动前后 14 天。",
            "把统计口径调整为每位付费用户收入。",
            "换成日均再看一遍。",
            "把付费口径由总金额改为日均。",
            "统计粒度调整到按周。",
            "指标由付费总金额到每位付费用户收入。",
            "别用自然月，用活动前后 14 天。",
        ):
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), "correction")

    def test_local_intent_rules_cover_business_paraphrase_families(self):
        cases = (
            ("这个判断经得起换窗口复核吗？", "challenge"),
            ("现有材料足以支撑这项结论吗？", "challenge"),
            ("能把这次增长直接归因于该渠道吗？", "challenge"),
            ("把统计口径调整为每位付费用户收入。", "correction"),
            ("请比较 2025 年第四季度与第三季度的收入走势。", "new_topic"),
            ("请分析全部历史覆盖里的月末收入规律。", "new_topic"),
            ("请拆解渠道贡献并检查异常日期敏感性。", "mixed_question"),
            (
                "相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？",
                "follow_up",
            ),
            ("付费金额和付费人数分别为什么变化？", "mixed_question"),
        )

        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), expected)

    def test_material_analysis_goal_precedes_capability_boundary(self):
        for message in (
            "最近付费人数有没有明显变化，现有数据能支持判断到什么程度？",
            "本季度活跃用户下降了吗，当前数据支持分析到哪一层？",
            "付费金额最近波动明显，现有数据能支持分析到什么程度？",
            "付费订单数是否持续回落，目前数据支持判断到哪一步？",
            "收入上涨是否真实，现有字段可以支持哪些分析？",
            "客单价下降的原因，当前数据支持到哪里？",
            "付费人数持续下降，现有数据支持判断到哪一步？",
            "付费频次最近波动，当前字段能支持哪些分析？",
            "支付成功率明显回落，现有数据支持判断到什么程度？",
            "新增用户持续上涨，当前数据可以支持分析到哪一层？",
            "注册人数下降了吗，现有字段支持判断到哪里？",
            "利润最近波动，当前数据能支持哪些分析？",
            "营销成本持续上涨，现有数据支持判断到哪一步？",
            "昨天玩法活跃变化能判断吗，现有数据支持到什么程度？",
        ):
            with self.subTest(message=message):
                self.assertEqual(_classify_intent(message, False), "new_topic")

        for message in (
            "目前能看哪些数据和字段？",
            "能否按渠道与支付方式组合分析？",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    _classify_intent(message, False),
                    "capability_question",
                )

    def test_metric_vocabulary_matches_canonical_runtime_registry(self):
        registry = RuntimeContractRegistry.from_path(
            ROOT / "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        expected = {
            label: metric_id
            for metric_id in registry.metric_ids
            for label in registry.metric_business_labels(metric_id)
        }

        self.assertEqual(dict(_metric_business_label_vocabulary()), expected)
        for metric_id in (
            "paid_users",
            "paid_frequency",
            "payment_success_rate",
            "new_users",
            "registrations",
            "profit",
            "aggregate_marketing_cost",
        ):
            label = registry.metric_business_labels(metric_id)[0]
            with self.subTest(metric_id=metric_id, label=label):
                self.assertIn(metric_id, _mentioned_metrics(f"查看{label}变化"))

    def test_metric_vocabulary_prefers_longest_overlapping_business_label(self):
        cases = (
            ("首次付费人数持续回落", {"first_paid_users"}),
            ("单笔付费金额最近下降", {"avg_order_amount"}),
        )

        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(_mentioned_metrics(message), expected)
                self.assertEqual(
                    _classify_intent(
                        f"{message}，现有数据能支持判断到什么程度？",
                        False,
                    ),
                    "new_topic",
                )

    def test_metric_health_ambiguity_clarifies_across_time_and_direction_paraphrases(self):
        for message in (
            "最近一个月业绩改善了吗？",
            "本期整体表现更健康了吗？",
            "这段时间经营结果有没有转好？",
        ):
            with self.subTest(message=message):
                self.assertTrue(_needs_clarification(message))

    def test_every_business_challenge_uses_semantic_orchestrator_policy(self):
        for message in (
            "这个结论受到渠道偏差干扰了吗？",
            "现有证据足以支撑结论吗？",
            "能把增长直接归因于活动吗？",
        ):
            with self.subTest(message=message):
                self.assertTrue(
                    _should_use_llm_orchestrator(
                        "challenge",
                        "inherit_current",
                        message,
                        False,
                    )
                )

    def test_artifact_context_binding_uses_artifact_intent_across_paraphrases(self):
        runtime = _seed_runtime()

        result = runtime.handle_message(
            "thread-phase7",
            "沿用上一份报告继续分析支付方式。",
        )

        self.assertEqual(result.turn_intent.intent, "artifact_continue")
        self.assertTrue(
            any(
                item.source_type == "artifact"
                for item in result.context_manifest.items
            )
        )

    def test_every_local_clarification_surface_uses_exact_final_escape_token(self):
        escape = "tell the agent to do differently"
        self.assertEqual(
            getattr(conversation_models, "CLARIFICATION_ESCAPE_OPTION", None),
            escape,
        )
        cases = (
            ("topic", "继续看刚才那个问题", "ask_topic_choice"),
            ("outlier", "如果去掉异常天还成立吗？", "inherit_current"),
            ("metric", "这个月是不是变好了？", "inherit_current"),
        )

        for surface, message, topic_relation in cases:
            with self.subTest(surface=surface):
                clarification = _build_clarification(
                    f"turn-{surface}",
                    message,
                    topic_relation,
                )
                self.assertEqual(len(clarification.questions), 1)
                options = clarification.questions[0].options
                self.assertEqual(options[-1].label, escape)
                self.assertEqual(
                    [option.label for option in options].count(escape),
                    1,
                )
                self.assertTrue(options[-1].description)
                self.assertTrue(
                    any("\u4e00" <= char <= "\u9fff" for char in options[-1].description)
                )

    def test_manifest_has_required_natural_language_coverage(self):
        data = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
        cases = data["cases"]
        counts = Counter(case["group"] for case in cases)

        self.assertGreaterEqual(len(cases), data["minimum_cases"])
        self.assertGreaterEqual(counts["continuous_follow_up"], 20)
        self.assertGreaterEqual(counts["mixed_question"], 10)
        self.assertGreaterEqual(counts["offtopic_capability_unsupported"], 10)
        self.assertGreaterEqual(counts["permission_snapshot_memory"], 10)
        self.assertGreaterEqual(counts["correction_challenge_clarification"], 10)
        for case in cases:
            with self.subTest(case=case["case_id"]):
                self.assertTrue(case["user_message"])
                self.assertIn("expected_intent", case)
                self.assertIn("expected_topic_relation", case)
                self.assertIn("expected_context_use", case)
                self.assertIn("expected_forbidden_context", case)
                self.assertIn("expected_reuse", case)
                self.assertIn("expected_langgraph", case)
                self.assertTrue(case["expected_answer_boundary"])

    def test_runtime_classifies_all_conversation_scenarios(self):
        for case in _cases():
            with self.subTest(case=case["case_id"]):
                runtime = _seed_runtime()
                if case["case_id"] in {"ccc_003", "ccc_004"}:
                    clarification = runtime.handle_message(
                        "thread-phase7",
                        "这个月是不是变好了？",
                    )
                    _persist_clarification_source_envelope(
                        runtime,
                        clarification,
                        question="这个月是不是变好了？",
                    )
                result = runtime.handle_message(
                    "thread-phase7",
                    case["user_message"],
                    role="business_reader" if case["case_id"] == "psm_004" else "analyst",
                    active_run_status="running" if case["case_id"] == "ccc_009" else "idle",
                    current_snapshot="2026H2" if case["case_id"] in {"psm_005", "psm_010"} else "2026H1",
                )

                self.assertEqual(result.turn_intent.intent, case["expected_intent"])
                self.assertEqual(result.topic_relation, case["expected_topic_relation"])
                self.assertTrue(result.context_manifest.items)
                self.assertTrue(result.audit_events)
                expected_reuse = (
                    "candidate"
                    if case["expected_reuse"] == "reuse"
                    else case["expected_reuse"]
                )
                self.assertIn(
                    expected_reuse,
                    [decision.decision for decision in result.reuse_decisions],
                )
                if case["expected_intent"] in {
                    "off_topic",
                    "capability_question",
                    "unsupported_request",
                    "memory_update",
                }:
                    self.assertIsNone(result.run_request)
                elif case["expected_langgraph"].get("ask_question"):
                    self.assertTrue(result.needs_clarification)
                    self.assertIsNone(result.run_request)
                    self.assertIsNotNone(result.clarification)
                else:
                    self.assertIsNotNone(result.run_request)

    def test_context_manifest_and_reuse_are_claim_safe(self):
        runtime = _seed_runtime()

        reusable = runtime.handle_message(
            "thread-phase7",
            "那具体哪些渠道贡献最大？",
        )
        result_items = [
            item for item in reusable.context_manifest.items if item.source_type == "result_ref"
        ]
        self.assertEqual(len(result_items), 1)
        self.assertEqual(result_items[0].source_ref, "result:q2-q1:paid_amount")
        self.assertFalse(result_items[0].can_support_claims)
        self.assertEqual(result_items[0].claim_use, "context_only")
        self.assertEqual(result_items[0].source_version, "contracts-v1:2026H1")
        self.assertFalse(reusable.context_manifest.can_support_claims)
        self.assertEqual(reusable.reuse_decisions[0].decision, "candidate")
        self.assertFalse(reusable.reuse_decisions[0].can_support_claim)
        self.assertEqual(
            reusable.run_request.to_dict()["reuse_candidates"],
            [
                runtime.store.results_for_topic(reusable.topic_id)[0].payload
            ],
        )
        prior_material = reusable.run_request.to_dict()[
            "prior_topic_material_context"
        ]
        self.assertEqual(
            prior_material["source_run_ids"],
            ["run-candidate"],
        )
        material_item = next(
            item
            for item in reusable.context_manifest.items
            if item.source_type == "material_authority"
        )
        self.assertFalse(material_item.can_support_claims)
        self.assertEqual(material_item.claim_use, "context_only")

        blocked = runtime.handle_message(
            "thread-phase7",
            "我现在只有普通权限，继续看刚才的细分。",
            role="business_reader",
        )
        self.assertEqual(blocked.reuse_decisions[0].decision, "blocked")
        self.assertFalse(blocked.context_manifest.can_support_claims)
        self.assertIn("permission_scope_mismatch", blocked.reuse_decisions[0].reason)
        blocked_result_items = [
            item for item in blocked.context_manifest.items if item.source_type == "result_ref"
        ]
        self.assertTrue(blocked_result_items[0].expired)
        self.assertEqual(blocked_result_items[0].claim_use, "blocked")

        stale = runtime.handle_message(
            "thread-phase7",
            "数据更新以后，这个判断现在还成立吗？",
            current_snapshot="2026H2",
        )
        self.assertEqual(stale.reuse_decisions[0].decision, "context_only")
        self.assertFalse(stale.context_manifest.can_support_claims)
        self.assertIn("snapshot_mismatch", stale.reuse_decisions[0].reason)
        stale_result_items = [
            item for item in stale.context_manifest.items if item.source_type == "result_ref"
        ]
        self.assertTrue(stale_result_items[0].expired)
        self.assertEqual(stale_result_items[0].claim_use, "context_only")

    def test_business_reader_can_follow_viewer_artifact_and_reuse_viewer_candidate(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-viewer-alias", owner_id="reader-1")
        topic = store.create_topic(
            "thread-viewer-alias",
            title="付费金额变化",
            summary="已验证的付费金额变化分析。",
        )
        store.set_current_topic("thread-viewer-alias", topic.topic_id)
        candidate = _add_authoritative_result_candidate(
            store,
            topic_id=topic.topic_id,
            result_ref="result:viewer-candidate",
            source_run_id="run-candidate",
            permission_scope="viewer",
        )
        store.add_artifact(
            artifact_id="artifact:viewer-visible",
            topic_id=topic.topic_id,
            follow_up_context="viewer scope 的已验证结果。",
            snapshot_id="2026H1",
            permission_scope="viewer",
        )

        result = runtime.handle_message(
            "thread-viewer-alias",
            "基于这个结果继续分析昨天付费金额变化。",
            role="business_reader",
            current_snapshot="2026H1",
        )

        self.assertEqual(result.reuse_decisions[0].decision, "candidate")
        self.assertEqual(
            result.run_request.to_dict()["reuse_candidates"],
            [candidate],
        )
        artifact = next(
            item
            for item in result.context_manifest.items
            if item.source_ref == "artifact:viewer-visible"
        )
        self.assertTrue(artifact.can_support_claims)
        self.assertFalse(artifact.expired)

    def test_business_reader_cannot_follow_admin_or_unknown_scope(self):
        for protected_scope in ("admin", "unknown_scope"):
            with self.subTest(protected_scope=protected_scope):
                store = InMemoryConversationStore()
                runtime = ConversationRuntime(store)
                thread_id = f"thread-protected-{protected_scope}"
                store.create_thread(thread_id, owner_id="reader-1")
                topic = store.create_topic(
                    thread_id,
                    title="受限分析",
                    summary="受限分析结果。",
                )
                store.set_current_topic(thread_id, topic.topic_id)
                candidate = _result_candidate_payload(
                    f"result:{protected_scope}-candidate"
                )
                candidate.pop("candidate_signature")
                candidate["permission_scope"] = protected_scope
                candidate["candidate_signature"] = canonical_digest(candidate)
                store.add_result_ref(
                    topic.topic_id,
                    result_ref=f"result:{protected_scope}-candidate",
                    snapshot_id="2026H1",
                    contract_version="contracts-v1",
                    permission_scope=protected_scope,
                    semantic_scope="analysis-contract:sha256:analysis-signature",
                    payload=candidate,
                )
                store.add_artifact(
                    artifact_id=f"artifact:{protected_scope}-protected",
                    topic_id=topic.topic_id,
                    follow_up_context="受限结果。",
                    snapshot_id="2026H1",
                    permission_scope=protected_scope,
                )

                result = runtime.handle_message(
                    thread_id,
                    "基于这个结果继续分析昨天付费金额变化。",
                    role="business_reader",
                    current_snapshot="2026H1",
                )

                self.assertEqual(result.reuse_decisions[0].decision, "blocked")
                artifact = next(
                    item
                    for item in result.context_manifest.items
                    if item.source_ref == f"artifact:{protected_scope}-protected"
                )
                self.assertFalse(artifact.can_support_claims)
                self.assertTrue(artifact.expired)

        self.assertFalse(_can_read_scope("data_owner_admin", "unknown_scope"))
        self.assertFalse(_can_read_scope("unknown_role", "business_reader"))

    def test_in_memory_result_candidates_are_newest_first(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-result-order", owner_id="analyst-1")
        topic = store.create_topic(
            "thread-result-order",
            title="付费金额变化",
        )
        for result_ref in ("result:older", "result:newer"):
            store.add_result_ref(
                topic.topic_id,
                result_ref=result_ref,
                snapshot_id="2026H1",
                contract_version="contracts-v1",
                permission_scope="analyst",
                semantic_scope="analysis-contract:sha256:analysis-signature",
                payload=_result_candidate_payload(result_ref),
            )

        self.assertEqual(
            tuple(
                item.result_ref
                for item in store.results_for_topic(topic.topic_id)
            ),
            ("result:newer", "result:older"),
        )

    def test_runtime_mismatch_never_forwards_reuse_candidate(self):
        runtime = _seed_runtime()

        mismatch = runtime.handle_message(
            "thread-phase7",
            "继续看刚才的渠道贡献。",
            contract_version="contracts-v2",
        )

        self.assertEqual(mismatch.reuse_decisions[0].decision, "context_only")
        self.assertEqual(mismatch.run_request.to_dict()["reuse_candidates"], [])

    def test_semantic_rerun_keeps_claim_reuse_closed_and_forwards_signed_intent_material(self):
        messages = (
            "换成按周后继续比较。",
            "按日均口径重新比较。",
            "只看渠道后再分析一次。",
        )
        for index, message in enumerate(messages):
            with self.subTest(message=message):
                thread_id = f"thread-semantic-rerun-{index}"
                store = InMemoryConversationStore()
                store.create_thread(thread_id, owner_id="analyst-1")
                topic = store.create_topic(
                    thread_id,
                    title="付费金额基线比较",
                    summary="已完成付费金额基线比较。",
                )
                store.set_current_topic(thread_id, topic.topic_id)
                candidate = _add_authoritative_result_candidate(
                    store,
                    topic_id=topic.topic_id,
                    result_ref=f"result:semantic-rerun-{index}",
                    source_run_id=f"run-semantic-rerun-{index}",
                    baselines=("previous_day", "rolling_7_day_baseline"),
                )

                turn = ConversationRuntime(store).handle_message(
                    thread_id,
                    message,
                )
                request = turn.run_request.to_dict()

                self.assertEqual(turn.topic_relation, "inherit_current")
                self.assertEqual(turn.reuse_decisions[0].decision, "rerun")
                self.assertEqual(
                    turn.reuse_decisions[0].reason,
                    "semantic_scope_mismatch",
                )
                self.assertEqual(request["reuse_candidates"], [])
                self.assertEqual(
                    request["prior_topic_material_context"][
                        "source_result_refs"
                    ],
                    [candidate["result_ref"]],
                )
                result_item = next(
                    item
                    for item in turn.context_manifest.items
                    if item.source_type == "result_ref"
                )
                self.assertEqual(result_item.claim_use, "rerun")
                self.assertFalse(result_item.can_support_claims)
                material_item = next(
                    item
                    for item in turn.context_manifest.items
                    if item.source_type == "material_authority"
                )
                self.assertEqual(material_item.claim_use, "context_only")
                self.assertFalse(material_item.can_support_claims)

    def test_snapshot_or_contract_mismatch_never_forwards_intent_material(self):
        variants = (
            {"current_snapshot": "2026H2"},
            {"contract_version": "contracts-v2"},
        )
        for index, kwargs in enumerate(variants):
            with self.subTest(kwargs=kwargs):
                thread_id = f"thread-material-mismatch-{index}"
                store = InMemoryConversationStore()
                store.create_thread(thread_id, owner_id="analyst-1")
                topic = store.create_topic(
                    thread_id,
                    title="付费金额分析",
                    summary="已完成付费金额分析。",
                )
                store.set_current_topic(thread_id, topic.topic_id)
                _add_authoritative_result_candidate(
                    store,
                    topic_id=topic.topic_id,
                    result_ref=f"result:material-mismatch-{index}",
                    source_run_id=f"run-material-mismatch-{index}",
                )

                turn = ConversationRuntime(store).handle_message(
                    thread_id,
                    "换成按周后继续比较。",
                    **kwargs,
                )
                request = turn.run_request.to_dict()

                self.assertEqual(request["reuse_candidates"], [])
                self.assertEqual(request["prior_topic_material_context"], {})
                self.assertFalse(
                    any(
                        item.source_type == "material_authority"
                        for item in turn.context_manifest.items
                    )
                )

    def test_runtime_forwards_all_exact_topic_candidates_newest_first(self):
        runtime = _seed_runtime()
        topic = runtime.store.current_topic("thread-phase7")
        newer = _add_authoritative_result_candidate(
            runtime.store,
            topic_id=topic.topic_id,
            result_ref="result:newer-query",
            source_run_id="run-newer-query",
        )

        result = runtime.handle_message(
            "thread-phase7",
            "继续看刚才的渠道贡献。",
        )

        self.assertEqual(
            [item["result_ref"] for item in result.run_request.to_dict()["reuse_candidates"]],
            ["result:newer-query", "result:q2-q1:paid_amount"],
        )
        self.assertTrue(
            all(
                decision.decision == "candidate"
                and not decision.can_support_claim
                for decision in result.reuse_decisions
            )
        )

    def test_prior_topic_material_rejects_indexed_candidate_payload_drift(self):
        class DriftedAuthorityStore(InMemoryConversationStore):
            def resolve_result_candidate_authority(self, **kwargs):
                authority = super().resolve_result_candidate_authority(**kwargs)
                authority["result_ref_record"]["payload"][
                    "rows_content_hash"
                ] = "drifted-indexed-payload"
                return authority

        store = DriftedAuthorityStore()
        store.create_thread("thread-indexed-drift", owner_id="analyst-1")
        topic = store.create_topic(
            "thread-indexed-drift",
            title="付费金额变化",
            summary="已完成付费金额变化分析。",
        )
        store.set_current_topic("thread-indexed-drift", topic.topic_id)
        _add_authoritative_result_candidate(
            store,
            topic_id=topic.topic_id,
            result_ref="result:indexed-drift",
            source_run_id="run-indexed-drift",
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "prior_topic_result_candidate_authority_mismatch",
        ):
            ConversationRuntime(store).handle_message(
                "thread-indexed-drift",
                "继续看刚才的渠道贡献。",
            )

    def test_prior_topic_material_builder_rejects_non_mapping_authority_with_typed_reason(self):
        from bi_agent.conversation.clarification_authority import (
            build_prior_topic_material_context,
        )

        for malformed in (None, "invalid", [], 0):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "^prior_topic_completed_authority_shape_invalid$",
                ):
                    build_prior_topic_material_context(
                        thread_id="thread-authority-shape",
                        topic_id="topic-authority-shape",
                        source_result_refs=("result:authority-shape",),
                        authorities=(malformed,),
                    )

    def test_prior_topic_material_rejects_indexed_column_or_contract_drift(self):
        mutations = {
            "snapshot": lambda authority: authority["result_ref_record"].__setitem__(
                "snapshot_id", "2026H2"
            ),
            "contract_version": lambda authority: authority[
                "result_ref_record"
            ].__setitem__("contract_version", "contracts-v2"),
            "semantic_scope": lambda authority: authority[
                "result_ref_record"
            ].__setitem__("semantic_scope", "analysis-contract:sha256:drift"),
            "analysis_contract": lambda authority: authority[
                "analysis_contract"
            ].__setitem__("business_timezone", "UTC"),
        }
        for axis, mutate in mutations.items():
            with self.subTest(axis=axis):
                class DriftedAuthorityStore(InMemoryConversationStore):
                    def resolve_result_candidate_authority(self, **kwargs):
                        authority = super().resolve_result_candidate_authority(
                            **kwargs
                        )
                        mutate(authority)
                        return authority

                thread_id = f"thread-indexed-{axis}"
                store = DriftedAuthorityStore()
                store.create_thread(thread_id, owner_id="analyst-1")
                topic = store.create_topic(
                    thread_id,
                    title="付费金额变化",
                    summary="已完成付费金额变化分析。",
                )
                store.set_current_topic(thread_id, topic.topic_id)
                _add_authoritative_result_candidate(
                    store,
                    topic_id=topic.topic_id,
                    result_ref=f"result:indexed-{axis}",
                    source_run_id=f"run-indexed-{axis}",
                )

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "prior_topic_result_candidate_(authority|contract)_mismatch",
                ):
                    ConversationRuntime(store).handle_message(
                        thread_id,
                        "继续看刚才的渠道贡献。",
                    )

    def test_prior_topic_material_rejects_one_bad_ref_in_same_run_group(self):
        class OneBadRefStore(InMemoryConversationStore):
            def resolve_result_candidate_authority(self, **kwargs):
                authority = super().resolve_result_candidate_authority(**kwargs)
                if kwargs["result_ref"] == "result:same-run-bad":
                    authority["result_ref_record"]["snapshot_id"] = "2026H2"
                return authority

        store = OneBadRefStore()
        store.create_thread("thread-same-run-bad", owner_id="analyst-1")
        topic = store.create_topic(
            "thread-same-run-bad",
            title="付费金额变化",
            summary="已完成付费金额变化分析。",
        )
        store.set_current_topic("thread-same-run-bad", topic.topic_id)
        good = _add_authoritative_result_candidate(
            store,
            topic_id=topic.topic_id,
            result_ref="result:same-run-good",
            source_run_id="run-same-ref-group",
        )
        bad = _add_authoritative_result_candidate(
            store,
            topic_id=topic.topic_id,
            result_ref="result:same-run-bad",
            source_run_id="run-same-ref-group",
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "prior_topic_result_candidate_authority_mismatch",
        ):
            ConversationRuntime(store).handle_message(
                "thread-same-run-bad",
                "继续看刚才的渠道贡献。",
            )

    def test_prior_topic_material_rejects_contract_or_execution_permission_scope_drift(self):
        for axis in ("contract", "execution"):
            with self.subTest(axis=axis):
                class PermissionDriftStore(InMemoryConversationStore):
                    def resolve_completed_material_authority(self, **kwargs):
                        authority = super().resolve_completed_material_authority(
                            **kwargs
                        )
                        if axis == "contract":
                            authority["analysis_contract"][
                                "permission_scope"
                            ] = "admin"
                        else:
                            authority["material_authority"][
                                "execution_material"
                            ]["permission_scope"] = "admin"
                        return authority

                thread_id = f"thread-permission-drift-{axis}"
                store = PermissionDriftStore()
                store.create_thread(thread_id, owner_id="analyst-1")
                topic = store.create_topic(
                    thread_id,
                    title="付费金额变化",
                    summary="已完成付费金额变化分析。",
                )
                store.set_current_topic(thread_id, topic.topic_id)
                _add_authoritative_result_candidate(
                    store,
                    topic_id=topic.topic_id,
                    result_ref=f"result:permission-{axis}",
                    source_run_id=f"run-permission-{axis}",
                )

                expected_error = (
                    "result_candidate_analysis_contract_mismatch"
                    if axis == "contract"
                    else "prior_topic_permission_scope_mismatch"
                )
                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    f"^{expected_error}$",
                ):
                    ConversationRuntime(store).handle_message(
                        thread_id,
                        "继续看刚才的渠道贡献。",
                    )

    def test_prior_topic_material_conflict_is_order_independent(self):
        orders = (
            (
                ("run-previous", ("previous_day",)),
                ("run-rolling", ("rolling_7_day_baseline",)),
            ),
            (
                ("run-rolling", ("rolling_7_day_baseline",)),
                ("run-previous", ("previous_day",)),
            ),
        )
        for index, order in enumerate(orders):
            with self.subTest(order=index):
                thread_id = f"thread-material-conflict-{index}"
                store = InMemoryConversationStore()
                store.create_thread(thread_id, owner_id="analyst-1")
                topic = store.create_topic(
                    thread_id,
                    title="付费金额变化",
                    summary="已完成付费金额变化分析。",
                )
                store.set_current_topic(thread_id, topic.topic_id)
                for source_run_id, baselines in order:
                    _add_authoritative_result_candidate(
                        store,
                        topic_id=topic.topic_id,
                        result_ref=f"result:{source_run_id}",
                        source_run_id=source_run_id,
                        baselines=baselines,
                    )

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "prior_topic_material_conflict",
                ):
                    ConversationRuntime(store).handle_message(
                        thread_id,
                        "继续看刚才的渠道贡献。",
                    )

    def test_semantic_rerun_validates_all_material_candidates_before_context(self):
        orders = (
            (("run-previous", ("previous_day",)), ("run-rolling", ("rolling_7_day_baseline",))),
            (("run-rolling", ("rolling_7_day_baseline",)), ("run-previous", ("previous_day",))),
        )
        for index, order in enumerate(orders):
            with self.subTest(order=index):
                thread_id = f"thread-semantic-material-conflict-{index}"
                store = InMemoryConversationStore()
                store.create_thread(thread_id, owner_id="analyst-1")
                topic = store.create_topic(
                    thread_id,
                    title="付费金额变化",
                    summary="已完成付费金额变化分析。",
                )
                store.set_current_topic(thread_id, topic.topic_id)
                for source_run_id, baselines in order:
                    _add_authoritative_result_candidate(
                        store,
                        topic_id=topic.topic_id,
                        result_ref=f"result:{source_run_id}",
                        source_run_id=source_run_id,
                        baselines=baselines,
                    )

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "prior_topic_material_conflict",
                ):
                    ConversationRuntime(store).handle_message(
                        thread_id,
                        "换成按周后继续比较。",
                    )

    def test_identical_multi_run_material_is_canonical_across_input_order(self):
        from bi_agent.conversation.clarification_authority import (
            build_prior_topic_material_context,
        )

        store = InMemoryConversationStore()
        store.create_thread("thread-canonical-material", owner_id="analyst-1")
        topic = store.create_topic(
            "thread-canonical-material",
            title="付费金额变化",
            summary="已完成付费金额变化分析。",
        )
        first = _add_authoritative_result_candidate(
            store,
            topic_id=topic.topic_id,
            result_ref="result:canonical-a",
            source_run_id="run-canonical-a",
        )
        second = _add_authoritative_result_candidate(
            store,
            topic_id=topic.topic_id,
            result_ref="result:canonical-b",
            source_run_id="run-canonical-b",
        )
        authorities = tuple(
            store.resolve_completed_material_authority(
                source_run_id=run_id,
                thread_id="thread-canonical-material",
                topic_id=topic.topic_id,
            )
            for run_id in (first["source_run_id"], second["source_run_id"])
        )

        forward = build_prior_topic_material_context(
            thread_id="thread-canonical-material",
            topic_id=topic.topic_id,
            source_result_refs=(first["result_ref"], second["result_ref"]),
            authorities=authorities,
        )
        reverse = build_prior_topic_material_context(
            thread_id="thread-canonical-material",
            topic_id=topic.topic_id,
            source_result_refs=(second["result_ref"], first["result_ref"]),
            authorities=reversed(authorities),
        )

        self.assertEqual(forward, reverse)
        self.assertEqual(
            forward["source_run_ids"],
            ["run-canonical-a", "run-canonical-b"],
        )

    def test_same_run_valid_result_refs_collapse_to_one_authority_and_keep_both_refs(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-same-run-good", owner_id="analyst-1")
        topic = store.create_topic(
            "thread-same-run-good",
            title="付费金额变化",
            summary="已完成付费金额变化分析。",
        )
        store.set_current_topic("thread-same-run-good", topic.topic_id)
        first = _add_authoritative_result_candidate(
            store,
            topic_id=topic.topic_id,
            result_ref="result:same-run-good-a",
            source_run_id="run-same-run-good",
        )
        second = _add_authoritative_result_candidate(
            store,
            topic_id=topic.topic_id,
            result_ref="result:same-run-good-b",
            source_run_id="run-same-run-good",
        )

        turn = ConversationRuntime(store).handle_message(
            "thread-same-run-good",
            "继续看刚才的渠道贡献。",
        )
        context = turn.run_request.to_dict()["prior_topic_material_context"]

        self.assertEqual(context["source_run_ids"], ["run-same-run-good"])
        self.assertEqual(len(context["authorities"]), 1)
        self.assertEqual(
            context["source_result_refs"],
            ["result:same-run-good-a", "result:same-run-good-b"],
        )

    def test_runtime_records_turn_before_context_manifest(self):
        store = StrictTurnStore()
        runtime = ConversationRuntime(store)

        result = runtime.handle_message(
            "thread-runtime-strict",
            "Q2 比 Q1 付费金额为什么变了？",
        )

        self.assertEqual(result.topic_relation, "new_topic")
        self.assertEqual(store.saved_manifest_turn_ids, [result.turn_id])

    def test_memory_update_creates_audited_proposal_without_long_term_write(self):
        runtime = _seed_runtime()
        before = runtime.store.long_term_memory("org-default")

        result = runtime.handle_message(
            "thread-phase7",
            "以后默认把 WajeSpecial 单独看。",
        )

        self.assertEqual(result.turn_intent.intent, "memory_update")
        self.assertEqual(len(result.memory_proposals), 1)
        self.assertEqual(result.memory_proposals[0].status, "proposed")
        self.assertEqual(runtime.store.long_term_memory("org-default"), before)

    def test_memory_items_have_refresh_and_revocation_metadata(self):
        store = InMemoryConversationStore()
        item = store.add_memory_item(
            owner_scope="org-default",
            text="默认把 WajeSpecial 单独观察。",
            source_ref="memory:accepted:wajespecial",
            visibility="analyst",
            status="accepted",
        )

        self.assertEqual(item.ttl, "until_revoked")
        self.assertEqual(item.refresh_rule, "refresh_on_contract_or_scope_change")
        self.assertEqual(item.revocation_path, "memory_proposal_revoke_or_admin_action")

    def test_runtime_carries_prior_assets_into_run_request(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-prior-assets", owner_id="analyst-1")

        result = runtime.handle_message(
            "thread-prior-assets",
            "继续看哪个渠道影响最大",
            prior_analysis_assets=(
                {
                    "asset_type": "dimension_scan",
                    "dimension": "channel",
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                },
            ),
        )

        self.assertIsNotNone(result.run_request)
        self.assertEqual(
            result.run_request.prior_analysis_assets,
            (
                {
                    "asset_type": "dimension_scan",
                    "dimension": "channel",
                    "dimensions": ["channel"],
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                },
            ),
        )

    def test_q_comparison_starts_new_topic_and_outlier_strategy_clarification_resumes_same_topic(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-live-case", owner_id="analyst-1")

        first = runtime.handle_message("thread-live-case", "Q2 相比 Q1 付费金额为什么变了？")
        self.assertEqual(first.turn_intent.intent, "new_topic")
        self.assertEqual(first.topic_relation, "new_topic")
        self.assertIsNotNone(first.topic_id)

        second = runtime.handle_message("thread-live-case", "那具体哪些渠道贡献最大？")
        self.assertIn("candidate_dimension_screen", second.run_request.requested_nodes)
        self.assertNotIn("joint_attribution", second.run_request.requested_nodes)

        third = runtime.handle_message("thread-live-case", "这些渠道里 WajeSpecial 是主要原因吗？")
        self.assertIn("candidate_dimension_screen", third.run_request.requested_nodes)
        self.assertNotIn("joint_attribution", third.run_request.requested_nodes)

        follow_up = runtime.handle_message("thread-live-case", "如果去掉异常天还成立吗？")
        self.assertEqual(follow_up.turn_intent.intent, "challenge")
        self.assertTrue(follow_up.needs_clarification)
        self.assertEqual(follow_up.topic_id, first.topic_id)
        self.assertEqual(
            follow_up.clarification.reason,
            "outlier_removal_strategy_changes_business_answer",
        )
        _persist_clarification_source_envelope(
            runtime,
            follow_up,
            question="如果去掉异常天还成立吗？",
        )

        resumed = runtime.handle_message(
            "thread-live-case",
            "按日粒度，移除贡献最大的正向日期后复算，不做订单级明细剔除。",
        )
        self.assertEqual(resumed.turn_intent.intent, "clarification_answer")
        self.assertEqual(resumed.topic_id, first.topic_id)
        self.assertIsNotNone(resumed.run_request)
        self.assertIn("outlier_contribution", resumed.run_request.requested_nodes)

    def test_clarification_answer_resumes_open_topic_without_creating_new_topic(self):
        runtime = build_test_runtime()

        first = runtime.handle_message("thread-live-case-2", "如果去掉异常天还成立吗？")

        self.assertEqual(first.status, "waiting_for_clarification")
        self.assertEqual(first.turn_intent.intent, "challenge")
        self.assertTrue(first.needs_clarification)
        self.assertEqual(first.topic_relation, "inherit_current")
        self.assertIsNotNone(first.topic_id)
        self.assertTrue(hasattr(runtime.store, "get_open_clarification"))
        open_clarification = runtime.store.get_open_clarification("thread-live-case-2")
        self.assertIsNotNone(open_clarification)
        self.assertEqual(open_clarification.topic_id, first.topic_id)
        self.assertEqual(open_clarification.status, "waiting")
        _persist_clarification_source_envelope(
            runtime,
            first,
            question="如果去掉异常天还成立吗？",
        )

        resumed = runtime.handle_message(
            "thread-live-case-2",
            "按日粒度，移除贡献最大的正向日期后复算。",
        )

        self.assertIn(resumed.status, {"completed", "running"})
        self.assertEqual(resumed.turn_intent, "clarification_answer")
        self.assertEqual(resumed.turn_intent.intent, "clarification_answer")
        self.assertEqual(resumed.topic_id, first.topic_id)
        self.assertEqual(resumed.context_manifest["sources"][0]["type"], "clarification")
        self.assertTrue(
            any(item.source_type == "clarification" for item in resumed.context_manifest.items)
        )
        self.assertEqual(len(runtime.store.topics_for_thread("thread-live-case-2")), 1)
        self.assertIsNotNone(resumed.run_request)
        self.assertIsNone(runtime.store.get_open_clarification("thread-live-case-2"))
        answered = runtime.store.clarification_states[first.clarification.clarification_id]
        self.assertEqual(answered.status, "answered")
        self.assertEqual(answered.answer, "按日粒度，移除贡献最大的正向日期后复算。")

    def test_open_clarification_does_not_coerce_unrelated_business_question(self):
        runtime = build_test_runtime()

        first = runtime.handle_message("thread-live-case-2", "如果去掉异常天还成立吗？")
        self.assertEqual(first.status, "waiting_for_clarification")
        self.assertIsNotNone(runtime.store.get_open_clarification("thread-live-case-2"))

        result = runtime.handle_message(
            "thread-live-case-2",
            "Q3 相比 Q2 按日粒度复算异常日期后，付费金额为什么变了？",
        )

        self.assertEqual(result.turn_intent.intent, "new_topic")
        self.assertEqual(result.topic_relation, "new_topic")
        self.assertNotEqual(result.topic_id, first.topic_id)
        self.assertFalse(
            any(item.source_type == "clarification" for item in result.context_manifest.items)
        )
        self.assertEqual(
            runtime.store.get_open_clarification("thread-live-case-2").status,
            "waiting",
        )

    def test_open_outlier_clarification_rejects_broad_unscoped_answers(self):
        for answer in ("订单级明细", "指定日期", "日期范围"):
            with self.subTest(answer=answer):
                runtime = build_test_runtime()
                first = runtime.handle_message("thread-live-case-2", "如果去掉异常天还成立吗？")

                result = runtime.handle_message("thread-live-case-2", answer)

                self.assertNotEqual(result.turn_intent.intent, "clarification_answer")
                self.assertFalse(
                    any(item.source_type == "clarification" for item in result.context_manifest.items)
                )
                self.assertEqual(
                    runtime.store.get_open_clarification("thread-live-case-2").status,
                    "waiting",
                )
                self.assertEqual(
                    runtime.store.clarification_states[first.clarification.clarification_id].status,
                    "waiting",
                )

    def test_open_topic_choice_clarification_rejects_partial_option_words(self):
        for answer in ("当前", "第二个", "继续"):
            with self.subTest(answer=answer):
                store = InMemoryConversationStore()
                runtime = ConversationRuntime(store)
                store.create_thread("thread-topic-choice-reject", owner_id="analyst-1")
                q2_topic = store.create_topic(
                    "thread-topic-choice-reject",
                    title="Q2 vs Q1",
                    summary="Q2/Q1",
                )
                store.create_topic(
                    "thread-topic-choice-reject",
                    title="1 月月初",
                    summary="1 月月初",
                )
                store.set_current_topic("thread-topic-choice-reject", q2_topic.topic_id)
                first = runtime.handle_message("thread-topic-choice-reject", "刚才那个继续看渠道。")

                result = runtime.handle_message("thread-topic-choice-reject", answer)

                self.assertNotEqual(result.turn_intent.intent, "clarification_answer")
                self.assertFalse(
                    any(item.source_type == "clarification" for item in result.context_manifest.items)
                )
                self.assertEqual(
                    runtime.store.get_open_clarification("thread-topic-choice-reject").status,
                    "waiting",
                )
                self.assertEqual(
                    runtime.store.clarification_states[first.clarification.clarification_id].status,
                    "waiting",
                )

    def test_open_metric_clarification_rejects_question_like_metric_reply(self):
        runtime = build_metric_clarification_runtime()
        first = runtime.handle_message("thread-metric-clarify", "这个月是不是变好了？")
        self.assertEqual(first.status, "waiting_for_clarification")

        result = runtime.handle_message("thread-metric-clarify", "总金额为什么又掉了？")

        self.assertNotEqual(result.turn_intent.intent, "clarification_answer")
        self.assertFalse(
            any(item.source_type == "clarification" for item in result.context_manifest.items)
        )
        self.assertEqual(
            runtime.store.get_open_clarification("thread-metric-clarify").status,
            "waiting",
        )
        self.assertEqual(
            runtime.store.clarification_states[first.clarification.clarification_id].status,
            "waiting",
        )

    def test_open_metric_clarification_accepts_clear_metric_answer(self):
        runtime = build_metric_clarification_runtime()
        first = runtime.handle_message("thread-metric-clarify", "这个月是不是变好了？")
        self.assertEqual(first.status, "waiting_for_clarification")
        _persist_clarification_source_envelope(
            runtime,
            first,
            question="这个月是不是变好了？",
        )

        result = runtime.handle_message("thread-metric-clarify", "按付费总金额")

        self.assertEqual(result.turn_intent.intent, "clarification_answer")
        self.assertTrue(
            any(item.source_type == "clarification" for item in result.context_manifest.items)
        )
        self.assertIsNone(runtime.store.get_open_clarification("thread-metric-clarify"))

    def test_outlier_variant_question_triggers_outlier_strategy_clarification(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-outlier-variant", owner_id="analyst-1")
        first = runtime.handle_message("thread-outlier-variant", "Q2 相比 Q1 付费金额为什么变了？")

        result = runtime.handle_message("thread-outlier-variant", "剔除异常日后还成立吗？")

        self.assertEqual(result.turn_intent.intent, "challenge")
        self.assertTrue(result.needs_clarification)
        self.assertEqual(result.topic_id, first.topic_id)
        self.assertEqual(
            result.clarification.reason,
            "outlier_removal_strategy_changes_business_answer",
        )

    def test_ambiguous_question_creates_structured_clarification_without_starting_run(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-ambiguous", owner_id="analyst-1")

        result = runtime.handle_message("thread-ambiguous", "这个月是不是变好了？")

        self.assertEqual(result.turn_intent.intent, "new_topic")
        self.assertTrue(result.needs_clarification)
        self.assertIsNone(result.run_request)
        self.assertIsNotNone(result.clarification)
        self.assertEqual(
            store.get_thread("thread-ambiguous").pending_clarification_id,
            result.clarification.clarification_id,
        )
        self.assertLessEqual(len(result.clarification.questions), 4)
        question = result.clarification.questions[0]
        self.assertLessEqual(len(question.options), 3)
        self.assertEqual(
            len([option for option in question.options if option.recommended]),
            1,
        )
        self.assertTrue(
            any(option.option_id == "tell_agent_differently" for option in question.options)
        )
        self.assertTrue(
            any(event["event_type"] == "clarification_requested" for event in store.audit_events)
        )

    def test_ambiguous_topic_reference_replaces_stale_pending_clarification(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-topic-choice", owner_id="analyst-1")
        q2_topic = store.create_topic("thread-topic-choice", title="Q2 vs Q1", summary="Q2/Q1")
        store.create_topic("thread-topic-choice", title="1 月月初", summary="1 月月初")
        store.set_current_topic("thread-topic-choice", q2_topic.topic_id)
        store.set_pending_clarification("thread-topic-choice", q2_topic.topic_id, "old-clarification")

        result = runtime.handle_message("thread-topic-choice", "刚才那个继续看渠道。")

        self.assertTrue(result.needs_clarification)
        self.assertIsNotNone(result.clarification)
        self.assertEqual(
            store.get_thread("thread-topic-choice").pending_clarification_id,
            result.clarification.clarification_id,
        )
        self.assertEqual(
            store.get_thread("thread-topic-choice").pending_clarification_topic_id,
            q2_topic.topic_id,
        )

    def test_llm_conversation_orchestrator_can_bind_business_intent(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-llm-route", owner_id="analyst-1")
        topic = store.create_topic(
            "thread-llm-route",
            title="Q2 vs Q1",
            summary="当前 topic 关注 Q2 相比 Q1 的变化。",
        )
        store.set_current_topic("thread-llm-route", topic.topic_id)
        fake = FakeConversationLLM(
            {
                "intent": "challenge",
                "topic_relation": "inherit_current",
                "business_summary": "用户在质疑既有结论是否受到 WajeSpecial 干扰。",
                "confidence": 0.91,
                "display_summary": "已识别为对当前结论的追问。",
            }
        )
        runtime = ConversationRuntime(store, llm_client=fake)

        result = runtime.handle_message(
            "thread-llm-route",
            "这个结论是不是被 WajeSpecial 干扰了？",
        )

        self.assertEqual(result.turn_intent.intent, "challenge")
        self.assertEqual(result.topic_relation, "inherit_current")
        self.assertEqual(result.turn_intent.decision_source, "llm_conversation_orchestrator")
        self.assertEqual(fake.calls[0]["task"], "conversation_orchestrator")
        self.assertTrue(
            any(event["event"] == "conversation_orchestrator_llm_evaluated" for event in result.audit_events)
        )

    def test_llm_cannot_inherit_runnable_turn_without_existing_topic(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-empty-llm-route", owner_id="analyst-1")
        fake = FakeConversationLLM(
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "business_summary": "用户希望执行一项业务分析。",
                "confidence": 0.91,
                "display_summary": "已识别为新的业务分析请求。",
            }
        )
        runtime = ConversationRuntime(store, llm_client=fake)

        result = runtime.handle_message(
            "thread-empty-llm-route",
            "分析收入构成，同时核对可用数据。",
        )

        self.assertEqual(result.turn_intent.intent, "new_topic")
        self.assertEqual(result.topic_relation, "new_topic")
        self.assertIsNotNone(result.topic_id)
        self.assertEqual(result.run_request.topic_id, result.topic_id)
        self.assertEqual(result.context_manifest.topic_id, result.topic_id)
        self.assertEqual(
            store.get_thread("thread-empty-llm-route").current_topic_id,
            result.topic_id,
        )
        self.assertEqual(len(fake.calls), 1)

    def test_clear_followup_uses_local_orchestrator_without_llm_call(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-local-route", owner_id="analyst-1")
        topic = store.create_topic(
            "thread-local-route",
            title="Q2 vs Q1",
            summary="当前 topic 关注 Q2 相比 Q1 的变化。",
        )
        store.set_current_topic("thread-local-route", topic.topic_id)
        fake = FakeConversationLLM(
            {
                "intent": "new_topic",
                "topic_relation": "new_topic",
                "business_summary": "不应调用。",
                "confidence": 0.99,
                "display_summary": "该请求应由本地路由直接处理。",
            }
        )
        runtime = ConversationRuntime(store, llm_client=fake)

        result = runtime.handle_message("thread-local-route", "这些变化在哪些渠道最明显？")

        self.assertEqual(result.turn_intent.intent, "follow_up")
        self.assertEqual(result.topic_relation, "inherit_current")
        self.assertEqual(result.turn_intent.decision_source, "local_conversation_orchestrator")
        self.assertEqual(fake.calls, [])

    def test_local_guard_blocks_unsupported_request_even_when_llm_disagrees(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-llm-guard", owner_id="analyst-1")
        fake = FakeConversationLLM(
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "business_summary": "用户想继续分析。",
                "confidence": 0.93,
                "display_summary": "已识别为继续分析请求。",
            }
        )
        runtime = ConversationRuntime(store, llm_client=fake)

        result = runtime.handle_message("thread-llm-guard", "直接写 SQL 查所有订单。")

        self.assertEqual(result.turn_intent.intent, "unsupported_request")
        self.assertEqual(result.topic_relation, "rejected")
        self.assertEqual(result.turn_intent.decision_source, "local_conversation_orchestrator_guard")
        self.assertIsNone(result.run_request)

    def test_llm_orchestrator_provider_failure_fails_closed_without_local_intent(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-llm-provider-failure", owner_id="analyst-1")
        runtime = ConversationRuntime(store, llm_client=FailingConversationLLM())

        with self.assertRaisesRegex(
            RuntimeError,
            "conversation_orchestrator_provider_failed",
        ):
            runtime.handle_message(
                "thread-llm-provider-failure",
                "刚才那个具体是哪个 topic？",
            )

    def test_invalid_llm_orchestration_fails_closed_without_local_intent(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-llm-fallback", owner_id="analyst-1")
        fake = FakeConversationLLM(
            {
                "intent": "raw_sql",
                "topic_relation": "magic_route",
                "business_summary": "无效输出。",
                "confidence": 0.99,
                "display_summary": "当前路由输出无效。",
            }
        )
        runtime = ConversationRuntime(store, llm_client=fake)

        with self.assertRaisesRegex(
            RuntimeError,
            "conversation_orchestrator_output_invalid",
        ):
            runtime.handle_message(
                "thread-llm-fallback",
                "刚才那个具体是哪个 topic？",
            )

    def test_non_mapping_llm_orchestration_fails_closed_without_local_intent(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-llm-shape", owner_id="analyst-1")
        runtime = ConversationRuntime(
            store,
            llm_client=RawConversationLLM(["not", "an", "intent"]),
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "conversation_orchestrator_output_invalid",
        ):
            runtime.handle_message(
                "thread-llm-shape",
                "刚才那个具体是哪个 topic？",
            )

    def test_llm_orchestration_requires_nonempty_business_summary(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-llm-summary", owner_id="analyst-1")
        fake = FakeConversationLLM(
            {
                "intent": "follow_up",
                "topic_relation": "ask_topic_choice",
                "business_summary": "",
                "confidence": 0.91,
                "display_summary": "当前需要选择关联的业务主题。",
            }
        )
        runtime = ConversationRuntime(store, llm_client=fake)

        with self.assertRaisesRegex(
            RuntimeError,
            "conversation_orchestrator_business_summary_invalid",
        ):
            runtime.handle_message(
                "thread-llm-summary",
                "刚才那个具体是哪个 topic？",
            )


def _seed_runtime() -> ConversationRuntime:
    store = InMemoryConversationStore()
    runtime = ConversationRuntime(store)
    store.create_thread("thread-phase7", owner_id="analyst-1")
    q2_topic = store.create_topic(
        "thread-phase7",
        title="Q2 vs Q1 付费金额变化",
        summary="当前 topic 关注 2026 Q2 相比 Q1 的付费金额变化。",
    )
    month_topic = store.create_topic(
        "thread-phase7",
        title="1 月月初模式",
        summary="第二个 topic 关注 1 月月初付费模式。",
    )
    store.set_current_topic("thread-phase7", q2_topic.topic_id)
    _add_authoritative_result_candidate(
        store,
        topic_id=q2_topic.topic_id,
        result_ref="result:q2-q1:paid_amount",
        source_run_id="run-candidate",
    )
    _add_authoritative_result_candidate(
        store,
        topic_id=month_topic.topic_id,
        result_ref="result:jan-month-start",
        source_run_id="run-jan-month-start",
    )
    store.add_artifact(
        artifact_id="artifact:q2-q1",
        topic_id=q2_topic.topic_id,
        follow_up_context="Q2/Q1 变化的已验证 Answer Package。",
        snapshot_id="2026H1",
        permission_scope="analyst",
    )
    store.add_memory_item(
        owner_scope="org-default",
        text="默认把 WajeSpecial 单独观察。",
        source_ref="memory:accepted:wajespecial",
        visibility="analyst",
        status="accepted",
    )
    return runtime


def _result_candidate_payload(
    result_ref: str,
    *,
    source_run_id: str = "run-candidate",
) -> dict:
    chain_id = canonical_digest({"result_ref": result_ref})[:16]
    payload = {
        "schema_version": "result-reuse-candidate.v1",
        "source_run_id": source_run_id,
        "result_ref": result_ref,
        "query_contract_ref": f"query-contract:{chain_id}",
        "query_contract_signature": f"query-signature:{chain_id}",
        "query_execution_record_ref": f"query-execution-record:{chain_id}",
        "query_execution_record_digest": f"query-execution-digest:{chain_id}",
        "analysis_contract_ref": f"analysis:{source_run_id}:1",
        "analysis_contract_signature": "analysis-signature",
        "runtime_snapshot_id": "2026H1",
        "runtime_contract_version": "contracts-v1",
        "source_snapshot_refs": ["snapshot:paid-success"],
        "source_snapshot_record_refs": ["snapshot-record:paid-success"],
        "source_snapshot_record_digests": ["snapshot-record-digest"],
        "source_release_refs": ["release:paid-success"],
        "source_release_authority_refs": ["release-authority:paid-success"],
        "source_schema_fingerprints": ["schema:paid-success"],
        "permission_scope": "analyst",
        "semantic_scope_signature": "analysis-contract:sha256:analysis-signature",
        "rows_ref": f"rows:{chain_id}",
        "rows_record_ref": f"rows-record:{chain_id}",
        "rows_record_digest": f"rows-record-digest:{chain_id}",
        "rows_content_hash": f"rows-content-hash:{chain_id}",
        "completeness_report_ref": f"completeness:{chain_id}",
        "completeness_record_refs": [f"completeness-record:{chain_id}"],
        "completeness_record_digests": [
            f"completeness-record-digest:{chain_id}"
        ],
        "binding_record_refs": [f"binding-record:{chain_id}"],
        "binding_record_digests": [f"binding-record-digest:{chain_id}"],
    }
    payload["candidate_signature"] = canonical_digest(payload)
    return payload


def _source_publication_payload(candidate, contract):
    query_contract = {
        "query_contract_id": candidate["query_contract_ref"],
        "analysis_contract_ref": candidate["analysis_contract_ref"],
        "contract_signature": candidate["query_contract_signature"],
        "permission_scope": candidate["permission_scope"],
        "dataset_snapshot_refs": candidate["source_snapshot_refs"],
    }
    result_payload = {
        "result_ref": candidate["result_ref"],
        "query_contract_ref": candidate["query_contract_ref"],
        "rows_ref": candidate["rows_ref"],
        "completeness_report_ref": candidate["completeness_report_ref"],
        "source_snapshot_refs": candidate["source_snapshot_refs"],
    }
    snapshot = {
        "snapshot_ref": candidate["source_snapshot_refs"][0],
        "release_ref": candidate["source_release_refs"][0],
        "authority_record_ref": candidate["source_release_authority_refs"][0],
        "schema_fingerprint": candidate["source_schema_fingerprints"][0],
        "permission_scopes": [candidate["permission_scope"]],
    }
    return {
        "analysis_contract": contract,
        "query_contracts": [query_contract],
        "query_execution_records": [
            {
                "result_ref": candidate["result_ref"],
                "record_ref": candidate["query_execution_record_ref"],
                "record_digest": candidate["query_execution_record_digest"],
                "execution_status": "succeeded",
                "query_contract_ref": candidate["query_contract_ref"],
                "contract_signature": candidate["query_contract_signature"],
                "rows_ref": candidate["rows_ref"],
                "rows_content_hash": candidate["rows_content_hash"],
                "row_count": 1,
                "completeness_report_ref": candidate["completeness_report_ref"],
                "source_snapshot_refs": candidate["source_snapshot_refs"],
                "source_snapshot_record_refs": candidate[
                    "source_snapshot_record_refs"
                ],
                "source_snapshot_record_digests": candidate[
                    "source_snapshot_record_digests"
                ],
                "contract": query_contract,
                "query_contract": query_contract,
                "result_payload": result_payload,
            }
        ],
        "rows_records": [
            {
                "rows_ref": candidate["rows_ref"],
                "record_ref": candidate["rows_record_ref"],
                "record_digest": candidate["rows_record_digest"],
                "rows_content_hash": candidate["rows_content_hash"],
                "row_count": 1,
                "metadata_payload": {
                    "rows_ref": candidate["rows_ref"],
                    "rows_content_hash": candidate["rows_content_hash"],
                },
            }
        ],
        "snapshot_records": [
            {
                "snapshot_ref": candidate["source_snapshot_refs"][0],
                "record_ref": candidate["source_snapshot_record_refs"][0],
                "record_digest": candidate["source_snapshot_record_digests"][0],
                "snapshot": snapshot,
                "payload": snapshot,
            }
        ],
        "completeness_records": [
            {
                "result_ref": candidate["result_ref"],
                "query_contract_ref": candidate["query_contract_ref"],
                "report_ref": candidate["completeness_report_ref"],
                "record_ref": candidate["completeness_record_refs"][0],
                "report_digest": candidate["completeness_record_digests"][0],
                "report_payload": {
                    "completeness_status": "complete",
                    "analysis_readiness": "ready",
                },
            }
        ],
        "capability_binding_records": [
            {
                "analysis_contract_ref": candidate["analysis_contract_ref"],
                "record_ref": candidate["binding_record_refs"][0],
                "binding_digest": candidate["binding_record_digests"][0],
                "status": "ready",
                "result_refs": [candidate["result_ref"]],
                "query_contract_refs": [candidate["query_contract_ref"]],
                "query_execution_record_refs": [
                    candidate["query_execution_record_ref"]
                ],
                "query_execution_record_digests": [
                    candidate["query_execution_record_digest"]
                ],
                "rows_refs": [candidate["rows_ref"]],
                "rows_metadata_record_refs": [candidate["rows_record_ref"]],
                "rows_metadata_record_digests": [
                    candidate["rows_record_digest"]
                ],
                "rows_content_hashes": [candidate["rows_content_hash"]],
                "completeness_report_refs": [
                    candidate["completeness_report_ref"]
                ],
                "completeness_record_refs": candidate["completeness_record_refs"],
                "completeness_record_digests": candidate[
                    "completeness_record_digests"
                ],
                "source_snapshot_refs": candidate["source_snapshot_refs"],
            }
        ],
    }


def _add_authoritative_result_candidate(
    store,
    *,
    topic_id,
    result_ref,
    source_run_id,
    permission_scope="analyst",
    baselines=(),
):
    from bi_agent.conversation.clarification_authority import (
        build_material_authority,
    )
    from bi_agent.runtime.analysis_contracts import (
        analysis_contract_signature,
        query_contract_signature,
    )
    from tests.phase7.test_clarification_resume_authority import (
        _runtime_material_for_contract,
        _source_contract,
    )

    topic = store.topic(topic_id)
    contract = _source_contract(source_run_id)
    contract["permission_scope"] = permission_scope
    contract["contract_signature"] = analysis_contract_signature(contract)
    baseline_values = list(baselines)
    original_intent = {
        "question_family": "business_object_impact_review",
        "question_families": ["business_object_impact_review"],
        "primary_question_family": "business_object_impact_review",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "requested_components": [],
        "requested_dimensions": [],
        "baseline_candidates": baseline_values,
        "context_sources": [],
        "claim_intents": [],
        "scope": "full_sample",
        "time_window": {
            "target": "yesterday",
            **(
                {"baseline": baseline_values[0]}
                if baseline_values
                else {}
            ),
        },
    }
    material_slots = {
        "target_metrics": ["paid_amount"],
        "requested_components": [],
        "requested_dimensions": [],
        "baselines": baseline_values,
        "context_sources": [],
        "claim_intents": [],
        "diagnostic_tags": [],
        "scope": "full_sample",
    }
    material_authority = build_material_authority(
        source_run_id=source_run_id,
        thread_id=topic.thread_id,
        topic_id=topic_id,
        original_intent=original_intent,
        material_slots=material_slots,
        runtime_material=_runtime_material_for_contract(contract),
    )
    runtime_context = {
        "context_manifest": {
            "snapshot_version": "2026H1",
            "contract_versions": {"runtime": "contracts-v1"},
        }
    }
    existing_run = store.runs.get(source_run_id)
    if existing_run is None:
        store.upsert_run(
            source_run_id,
            thread_id=topic.thread_id,
            topic_id=topic_id,
            status="running_workflow",
            request=runtime_context,
        )
        store.analysis_runtime_authority["analysis_contract"][
            contract["analysis_contract_id"]
        ] = contract
        store.finalize_completed_material_authority(
            run_id=source_run_id,
            thread_id=topic.thread_id,
            topic_id=topic_id,
            request=runtime_context,
            material_authority=material_authority,
        )
    else:
        contract = store.analysis_runtime_authority["analysis_contract"][
            f"analysis:{source_run_id}:1"
        ]
    candidate = _result_candidate_payload(
        result_ref,
        source_run_id=source_run_id,
    )
    candidate.pop("candidate_signature")
    candidate.update(
        {
            "analysis_contract_ref": contract["analysis_contract_id"],
            "analysis_contract_signature": contract["contract_signature"],
            "permission_scope": permission_scope,
            "semantic_scope_signature": (
                "analysis-contract:sha256:"
                + contract["contract_signature"]
            ),
        }
    )
    query_windows = list(contract["resolved_windows"])
    query_contract = {
        "query_contract_id": candidate["query_contract_ref"],
        "analysis_contract_ref": contract["analysis_contract_id"],
        "query_intent": "business_object_impact_review",
        "dataset_snapshot_refs": list(candidate["source_snapshot_refs"]),
        "metric_bindings": list(contract["metric_bindings"]),
        "dimension_bindings": list(contract["dimension_bindings"]),
        "window_refs": [window["window_id"] for window in query_windows],
        "resolved_windows": query_windows,
        "filters": [],
        "result_shape": {
            "required_fields": ["window_id", "paid_amount"],
            "unique_key": ["window_id"],
            "grain": ["window_id"],
            "required_window_ids": [
                window["window_id"] for window in query_windows
            ],
            "result_semantics": "complete_aggregate",
            "dimension_presence_policy": "paired_required",
        },
        "completeness_assertions": ["all_required_windows_present"],
        "permission_scope": permission_scope,
        "workload_class": "interactive",
        "query_parameters": {},
        "query_role_ref": "",
        "reconciliation_binding": None,
        "join_expectation": None,
    }
    query_contract["contract_signature"] = query_contract_signature(
        query_contract
    )
    candidate["query_contract_signature"] = query_contract[
        "contract_signature"
    ]
    candidate["candidate_signature"] = canonical_digest(candidate)
    publication_payload = _source_publication_payload(candidate, contract)
    publication_payload["query_contracts"] = [query_contract]
    publication_payload["query_execution_records"][0].update(
        {
            "contract_signature": query_contract["contract_signature"],
            "contract": query_contract,
            "query_contract": query_contract,
        }
    )
    existing_publication = store.analysis_runtime_records.get(source_run_id)
    if existing_publication is not None:
        merged = {
            key: (
                dict(value)
                if key == "analysis_contract"
                else list(value)
            )
            for key, value in existing_publication["payload"].items()
        }
        for group, records in publication_payload.items():
            if group == "analysis_contract":
                if merged[group] != records:
                    raise AssertionError("source publication contract drift")
                continue
            for record in records:
                if record not in merged[group]:
                    merged[group].append(record)
        publication_payload = merged
    store.analysis_runtime_records[source_run_id] = {
        "digest": canonical_digest(publication_payload),
        "payload": publication_payload,
    }
    store.add_result_ref(
        topic_id,
        result_ref=result_ref,
        snapshot_id=candidate["runtime_snapshot_id"],
        contract_version=candidate["runtime_contract_version"],
        permission_scope=permission_scope,
        semantic_scope=candidate["semantic_scope_signature"],
        payload=candidate,
    )
    return candidate


def build_test_runtime() -> ConversationRuntime:
    store = InMemoryConversationStore()
    runtime = ConversationRuntime(store)
    store.create_thread("thread-live-case-2", owner_id="analyst-1")
    topic = store.create_topic(
        "thread-live-case-2",
        title="Q2 vs Q1 付费金额变化",
        summary="当前 topic 关注 2026 Q2 相比 Q1 的付费金额变化。",
    )
    store.set_current_topic("thread-live-case-2", topic.topic_id)
    return runtime


def build_metric_clarification_runtime() -> ConversationRuntime:
    store = InMemoryConversationStore()
    runtime = ConversationRuntime(store)
    store.create_thread("thread-metric-clarify", owner_id="analyst-1")
    return runtime


class StrictTurnStore(InMemoryConversationStore):
    def __init__(self) -> None:
        super().__init__()
        self.saved_manifest_turn_ids = []

    def save_context_manifest(self, manifest):
        payload = manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
        thread = self.get_thread(payload["thread_id"])
        turn_exists = any(turn.get("turn_id") == payload["turn_id"] for turn in thread.turns)
        if not turn_exists:
            raise AssertionError("turn must exist before context manifest insert")
        self.saved_manifest_turn_ids.append(payload["turn_id"])
        return super().save_context_manifest(manifest)


class FakeConversationLLM:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        self.calls.append(
            {
                "task": task,
                "prompt_version": prompt_version,
                "messages": [dict(message) for message in messages],
                "required_keys": list(required_keys),
            }
        )
        output = dict(self.output)
        return SimpleNamespace(
            output=output,
            audit={
                "task": task,
                "prompt_version": prompt_version,
                "provider": "fake",
                "model": "fake",
                "structured_output": output,
            },
        )


class FailingConversationLLM:
    def invoke_json(self, **_kwargs):
        raise RuntimeError("provider unavailable")


class RawConversationLLM:
    def __init__(self, output):
        self.output = output

    def invoke_json(self, **_kwargs):
        return SimpleNamespace(output=self.output, audit={"provider": "fake"})


if __name__ == "__main__":
    unittest.main()
