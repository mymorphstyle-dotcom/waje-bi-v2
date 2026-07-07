class FakeLLMClient:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []

    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        self.calls.append(task)
        output = dict(DEFAULT_OUTPUTS.get(task, {}))
        output.update(self.overrides.get(task, {}))
        for key in required_keys:
            output.setdefault(key, None)
        return FakeLLMResult(
            output,
            {
                "task": task,
                "provider": "fake",
                "model": "fake-model",
                "prompt_version": prompt_version,
                "response_id": f"fake-{task}",
                "messages": [dict(message) for message in messages],
                "required_keys": list(required_keys),
                "raw_response_content": "{}",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:00+00:00",
                "duration_ms": 0.0,
                "input_hash": f"input-{task}",
                "output_hash": f"output-{task}",
                "usage": {},
                "structured_output": output,
            },
        )


class FakeLLMResult:
    def __init__(self, output, audit):
        self.output = output
        self.audit = audit


DEFAULT_OUTPUTS = {
    "business_intent": {
        "question_family": "pattern_explanation",
        "target_metric": "paid_amount",
        "pattern_family": "intra_period",
        "scope": "full_sample",
        "time_window": "2024-01..2026-05",
        "target_claim": "recurring_pattern_existence",
        "baseline_candidates": ["same_period_phase_baseline"],
        "status_message": "正在识别问题意图",
    },
    "boundary_decision": {
        "boundary_status": "clear",
        "recommended_assumption": {},
        "clarification_questions": [],
        "decision_summary": "问题边界足够明确，可以继续。",
    },
    "clarification_question": {
        "questions": [],
        "recommended_assumption": {},
        "status_message": "需要用户确认业务边界。",
    },
    "confirm_understanding": {
        "confirmed_intent": {"question_family": "pattern_explanation"},
        "accepted_assumptions": [],
        "status_message": "已确认本次业务理解。",
    },
    "analysis_route": {
        "requested_nodes": ["pattern_scan"],
        "route_summary": "先验证 pattern，再补充必要证据路径。",
        "expected_evidence": ["pattern_scan"],
        "decision_summary": "使用 pattern_explanation 路线。",
    },
    "route_repair": {
        "requested_nodes": ["pattern_scan"],
        "repair_summary": "移除不支持节点。",
        "decision_summary": "修正为可执行路线。",
    },
    "data_coverage_interpretation": {
        "coverage_status": "sufficient",
        "business_impact": "当前聚合数据可支持本轮 pattern 评估。",
        "decision_summary": "数据覆盖足够。",
    },
    "next_action": {
        "next_action": "synthesize_answer",
        "decision_summary": "证据足够进入答案合成。",
    },
    "promotion_direction": {
        "requested_nodes": ["joint_attribution"],
        "decision_summary": "残差值得测试组合归因。",
    },
    "evidence_interpretation": {
        "interpretation": "pattern evidence supports a draft association claim.",
        "decision_summary": "证据可支持候选业务解释。",
        "evidence_boundary": "No causal claim.",
    },
    "answer_synthesis": {
        "answer_text": "基于已验证证据引用生成答案草稿。",
        "claims": None,
    },
    "semantic_audit": {
        "audit_status": "passed",
        "extracted_claims": [],
        "issues": [],
    },
    "causal_audit": {
        "causal_assessment": "candidate_hypothesis",
        "publishable_wording": "可以作为候选解释，不能写成已证明原因。",
        "supporting_reasons": ["当前证据显示可观察现象，但缺少对照或机制验证。"],
        "main_risks": ["替代解释仍然可能成立。"],
        "alternative_explanations": [],
        "missing_checks": ["补充分群一致性、事件重合和对照证据。"],
        "recommended_next_analysis": ["继续检查候选机制是否在不同分群中一致。"],
        "answer_guidance": "最终答案应分开写已验证事实、候选解释和后续观察。",
    },
    "answer_repair": {
        "answer_text": "已按校验反馈修正答案草稿。",
        "claims": None,
    },
    "final_business_summary": {
        "summary_text": (
            "我对问题的理解是：用户要确认当前付费金额模式是否成立。\n"
            "分析脉络：系统完成了业务意图绑定、分析路径验收、数据覆盖检查、证据执行和答案校验。\n"
            "关键发现：证据支持有边界的业务结论。\n"
            "最终结论：保留通过 verifier 的结论。\n"
            "需要注意：后续仍要观察限制项和新周期表现。"
        ),
    },
    "degraded_explanation": {
        "status": "degraded",
        "explanation": "当前证据有限。",
        "owner": "data_engineering_owner",
        "repair_path": "补充更强证据。",
    },
    "blocked_explanation": {
        "status": "blocked",
        "explanation": "当前存在硬边界，无法继续执行。",
        "owner": "data_engineering_owner",
        "repair_path": "修复阻断边界。",
    },
}
