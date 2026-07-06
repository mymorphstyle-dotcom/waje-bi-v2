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
        "answer_text": "Draft answer based on verified evidence refs.",
        "claims": None,
    },
    "semantic_audit": {
        "audit_status": "passed",
        "extracted_claims": [],
        "issues": [],
    },
    "answer_repair": {
        "answer_text": "Repaired draft answer.",
        "claims": None,
    },
    "degraded_explanation": {
        "status": "degraded",
        "explanation": "Evidence is limited.",
        "owner": "data_engineering_owner",
        "repair_path": "add stronger evidence.",
    },
    "blocked_explanation": {
        "status": "blocked",
        "explanation": "A hard boundary blocks the run.",
        "owner": "data_engineering_owner",
        "repair_path": "fix the blocking boundary.",
    },
}
