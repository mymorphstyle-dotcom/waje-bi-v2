import json


class FakeLLMClient:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []

    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        self.calls.append(task)
        override = self.overrides.get(task, {})
        if task == "analysis_route":
            output = _default_analysis_route(messages, override)
        else:
            output = (
                _default_query_gap_clarification(messages)
                if task == "query_gap_clarification" and task not in self.overrides
                else _default_final_business_summary(messages)
                if task == "final_business_summary" and task not in self.overrides
                else dict(DEFAULT_OUTPUTS.get(task, {}))
            )
            output.update(override)
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


def _default_query_gap_clarification(messages):
    payload = _input_payload(messages)
    options = [
        str(option)
        for option in payload.get("allowed_business_options") or ()
        if str(option)
    ]
    recommended = str(payload.get("recommended_business_option") or "")
    if not recommended and options:
        recommended = options[0]
    return {
        "questions": [{
            "question": "需要确认按哪个业务口径继续？",
            "options": [*options, "tell the agent to do differently"],
        }],
        "recommended_assumption": {"option": recommended},
        "recommendation_reason": "该处理方式符合当前业务证据边界。",
        "decision_summary": "该选择会影响业务结论。",
    }


def _default_analysis_route(messages, override):
    output = dict(DEFAULT_OUTPUTS["analysis_route"])
    output["analysis_requirements"] = dict(output["analysis_requirements"])
    output.update(override if isinstance(override, dict) else {})
    if isinstance(override, dict) and "analysis_requirements" in override:
        return output
    payload = _input_payload(messages)
    cards = {
        str(card.get("capability_id") or ""): card
        for card in payload.get("known_capabilities") or ()
        if isinstance(card, dict) and str(card.get("capability_id") or "")
    }
    selected = []
    for item in output.get("requested_nodes") or ():
        if isinstance(item, str):
            selected.append(item)
            continue
        if not isinstance(item, dict):
            continue
        selected.append(
            next(
                (
                    str(item[key])
                    for key in (
                        "capability_id",
                        "capability",
                        "node_id",
                        "node",
                        "id",
                    )
                    if isinstance(item.get(key), str) and item.get(key)
                ),
                "",
            )
        )
    claims = []
    for capability_id in selected:
        card = cards.get(capability_id) or {}
        for claim in card.get("allowed_claim_types") or ():
            if isinstance(claim, str) and claim and claim not in claims:
                claims.append(claim)
    output["analysis_requirements"]["claim_intents"] = claims
    return output


DEFAULT_OUTPUTS = {
    "business_intent": {
        "question_family": "pattern_explanation",
        "target_metric": "paid_amount",
        "pattern_family": "intra_period",
        "pattern_params": {"target_phase": "start"},
        "scope": "full_sample",
        "time_window": "2024-01..2026-05",
        "target_claim": "recurring_pattern_existence",
        "baseline_candidates": [],
        "analysis_requirements": {
            "context_sources": [],
            "claim_intents": [],
            "requested_dimensions": [],
            "requested_components": [],
        },
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
        "analysis_requirements": {
            "target_metrics": ["paid_amount"],
            "requested_components": [],
            "requested_dimensions": [],
            "baselines": ["previous_day"],
            "context_sources": [],
            "claim_intents": [],
            "scope": {"type": "full_sample"},
        },
        "decision_summary": "使用 pattern_explanation 路线。",
    },
    "query_gap_clarification": {
        "questions": [
            {
                "question": "目标日数据尚未完整时，按哪个业务窗口继续？",
                "options": [
                    "等待相关业务数据可用后继续",
                    "tell the agent to do differently",
                ],
            }
        ],
        "recommended_assumption": {
            "option": "等待相关业务数据可用后继续",
        },
        "decision_summary": "目标窗口会改变结论，需要用户确认。",
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
        "summary_text": "最终结论：当前证据能把排查方向收敛到已验证业务结论。",
    },
    "final_answer_audit": {
        "display_status": "ready",
        "hard_blockers": [],
        "repairable_warnings": [],
        "retry_instruction": "",
        "business_audit_summary": "答案满足当前展示边界。",
    },
    "degraded_explanation": {
        "status": "degraded",
        "explanation": "当前证据有限。",
        "owner": "业务分析负责人",
        "repair_path": "补充业务证据后重跑。",
    },
    "blocked_explanation": {
        "status": "blocked",
        "explanation": "当前存在硬边界，无法继续执行。",
        "owner": "业务分析负责人",
        "repair_path": "先解除阻断边界后重跑。",
    },
}


def _default_final_business_summary(messages):
    payload = _input_payload(messages)
    intent = payload.get("intent") if isinstance(payload, dict) else {}
    metric = _business_label(str((intent or {}).get("target_metric") or "付费金额"))
    scope = _business_label(str((intent or {}).get("scope") or "full_sample"))
    claims = payload.get("claims") if isinstance(payload, dict) else []
    claim_text = ""
    if isinstance(claims, list) and claims and isinstance(claims[0], dict):
        claim_text = str(claims[0].get("text") or "").strip()
        number_text = _claim_number_text(claims[0].get("numbers"))
    else:
        number_text = ""
    if not claim_text:
        final = payload.get("final_explanation") if isinstance(payload, dict) else {}
        claim_text = str((final or {}).get("explanation") or "当前证据不足以发布主业务结论。")
    limitations = []
    if isinstance(payload, dict):
        limitations = list((payload.get("evidence_brief") or {}).get("limitations") or [])
    attention = "还不能直接说这是唯一原因或已被因果证明。"
    if "insufficient_comparable_periods" in limitations or "no_comparable_periods" in limitations:
        attention = "可比周期不足，结论只能按当前证据边界使用。"
    elif "weak_direction" in limitations:
        attention = "方向一致性不足，结论只能作为排查线索。"
    elif "below_materiality_floor" in limitations:
        attention = "变化幅度低于当前重要性阈值，不能写成强结论。"
    return {
        "summary_text": (
            f"我对问题的理解是：用户要在{scope}口径下确认当前{metric}相关业务问题。\n"
            "分析脉络：我检查了已接受分析路径、证据引用和答案校验结果。\n"
            f"关键发现：当前证据能把排查方向收敛到已验证结论，{claim_text} {number_text}\n"
            f"最终结论：已验证结论是：{claim_text} {number_text}当前证据能把排查方向收敛到这个方向。\n"
            f"需要注意：{attention}"
        )
    }


def _input_payload(messages):
    for message in messages:
        content = message.get("content", "") if isinstance(message, dict) else ""
        if "<input_json>" not in content:
            continue
        start = content.index("<input_json>") + len("<input_json>")
        end = content.index("</input_json>")
        return json.loads(content[start:end].strip())
    return {}


def _business_label(value):
    return {
        "paid_amount": "付费金额",
        "daily_paid_amount": "日均付费金额",
        "full_sample": "全样本",
        "all_users": "全体用户",
    }.get(value, value)


def _claim_number_text(numbers):
    if not isinstance(numbers, dict):
        return ""
    parts = []
    for value in numbers.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if abs(value) <= 1:
                parts.append(f"{abs(value) * 100:.1f}%")
            else:
                parts.append(str(value))
    return " ".join(parts)
