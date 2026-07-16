import json


class FakeLLMClient:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []
        self.audit_calls = []

    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        self.calls.append(task)
        override = self.overrides.get(task, {})
        if task == "analysis_route_plan":
            output = _default_analysis_route_plan(messages, override)
        elif task == "final_route_narrative":
            output = _default_final_route_narrative(messages, override)
        elif task == "final_business_summary":
            output = (
                dict(override)
                if task in self.overrides
                else _default_final_business_summary(messages)
            )
        else:
            output = (
                _default_query_gap_clarification(messages)
                if task == "query_gap_clarification" and task not in self.overrides
                else _default_confirm_understanding(messages)
                if task == "confirm_understanding"
                else dict(DEFAULT_OUTPUTS.get(task, {}))
            )
        if task != "final_business_summary":
            output.update(override)
        audit = {
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
            }
        self.audit_calls.append(audit)
        return FakeLLMResult(output, audit)


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
        "display_summary": "需要确认业务口径后继续。",
    }


def _default_confirm_understanding(messages):
    payload = _input_payload(messages)
    required_machine_intent = payload.get("required_machine_intent")
    if isinstance(required_machine_intent, dict):
        machine_intent = dict(required_machine_intent)
    else:
        machine_intent = None
    intent = payload.get("intent")
    if not isinstance(intent, dict):
        intent = {}
    material_fields = (
        "question_family",
        "target_metric",
        "pattern_family",
        "scope",
        "time_window",
        "target_claim",
        "baseline",
        "target",
        "pattern_params",
    )
    if machine_intent is None:
        machine_intent = {
            field: intent[field]
            for field in material_fields
            if field in intent
        }
    return {
        "confirmed_intent": {
            "business_summary": "已确认本次分析的业务问题、时间口径和证据边界。",
            "machine_intent": machine_intent,
        },
        "accepted_assumptions": [],
        "status_message": "已确认本次业务理解。",
        "display_summary": "已确认分析边界，继续设计分析路线。",
    }


def _default_analysis_route_plan(messages, override):
    output = dict(DEFAULT_OUTPUTS["analysis_route_plan"])
    output["analysis_requirements"] = dict(output["analysis_requirements"])
    output.update(override if isinstance(override, dict) else {})
    payload = _input_payload(messages)
    intent = payload.get("intent") or {}
    baseline_binding = intent.get("baseline_binding") or {}
    if not bool(baseline_binding.get("confirmed")):
        output["analysis_requirements"]["baselines"] = []
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
    selection_normalized = bool(
        selected
        and any(capability_id not in cards for capability_id in selected)
    )
    if selection_normalized:
        selected = [
            str(item)
            for item in payload.get("required_capability_ids") or ()
            if isinstance(item, str) and item in cards
        ]
        output["requested_nodes"] = selected
    claims = []
    for capability_id in selected:
        card = cards.get(capability_id) or {}
        for claim in card.get("allowed_claim_types") or ():
            if isinstance(claim, str) and claim and claim not in claims:
                claims.append(claim)
    output["analysis_requirements"]["claim_intents"] = claims
    return output


def _default_final_route_narrative(messages, override):
    payload = _input_payload(messages)
    context = payload.get("route_context") or {}
    steps = list(context.get("route_steps") or ())
    output = {
        "route_summary": "先核对目标指标的真实变化，再按已确认路线检查贡献因素。",
        "sections": [
            {
                "step_ref": str(step.get("step_ref") or ""),
                "route_step": f"执行{step.get('business_name') or '该项业务分析'}。",
                "expected_evidence": "获得该步骤对应的业务证据与限制说明。",
            }
            for step in steps
        ],
        "decision_summary": "这条路线先验证方向，再形成有证据约束的因素判断。",
        "display_summary": "分析路线已经确定，下一步核验数据与因素贡献。",
    }
    output.update(override if isinstance(override, dict) else {})
    return output


DEFAULT_OUTPUTS = {
    "conversation_orchestrator": {
        "intent": "new_analysis",
        "topic_relation": "new_topic",
        "business_summary": "用户希望开始一项新的业务分析。",
        "confidence": "high",
        "display_summary": "已识别为新的业务分析请求。",
    },
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
            "claim_intent_roles": {},
            "requested_dimensions": [],
            "requested_components": [],
        },
        "status_message": "正在识别问题意图",
        "display_summary": "已识别目标指标、分析范围和时间窗口。",
    },
    "boundary_decision": {
        "boundary_status": "clear",
        "recommended_assumption": {},
        "clarification_questions": [],
        "decision_summary": "问题边界足够明确，可以继续。",
        "display_summary": "问题边界明确，可以继续分析。",
    },
    "clarification_question": {
        "questions": [],
        "recommended_assumption": {},
        "status_message": "需要用户确认业务边界。",
        "display_summary": "需要确认一项会影响结论的业务边界。",
    },
    "confirm_understanding": {
        "confirmed_intent": {
            "business_summary": "已确认本次分析的业务问题、时间口径和证据边界。",
            "machine_intent": {"question_family": "pattern_explanation"},
        },
        "accepted_assumptions": [],
        "status_message": "已确认本次业务理解。",
    },
    "analysis_route_plan": {
        "requested_nodes": ["pattern_scan"],
        "analysis_requirements": {
            "target_metrics": ["paid_amount"],
            "requested_components": [],
            "requested_dimensions": [],
            "baselines": ["previous_day"],
            "context_sources": [],
            "claim_intents": [],
            "scope": {"type": "full_sample"},
        },
        "display_summary": "已规划先验证指标变化，再检查业务因素。",
    },
    "final_route_narrative": {
        "route_summary": "先核对变化，再按路线检查相关因素。",
        "sections": [],
        "decision_summary": "路线保留待验证的数据边界。",
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
        "recommendation_reason": "目标窗口数据可用性会影响业务结论。",
        "decision_summary": "目标窗口会改变结论，需要用户确认。",
        "display_summary": "目标窗口数据尚未就绪，需要确认后续处理。",
    },
    "route_repair": {
        "requested_nodes": ["pattern_scan"],
        "repair_summary": "移除不支持节点。",
        "decision_summary": "修正为可执行路线。",
        "display_summary": "分析路线已调整为当前可执行范围。",
    },
    "data_coverage_interpretation": {
        "coverage_status": "sufficient",
        "business_impact": "当前聚合数据可支持本轮 pattern 评估。",
        "decision_summary": "数据覆盖足够。",
        "display_summary": "当前数据覆盖可支持本轮分析。",
    },
    "next_action": {
        "next_action": "synthesize_answer",
        "decision_summary": "证据足够进入答案合成。",
        "display_summary": "证据已准备好，可以形成业务回答。",
    },
    "promotion_direction": {
        "requested_nodes": ["joint_attribution"],
        "decision_summary": "残差值得测试组合归因。",
        "display_summary": "可以继续检查组合因素的解释力。",
    },
    "evidence_interpretation": {
        "interpretation": "当前证据支持形成有边界的业务观察。",
        "decision_summary": "证据可支持候选业务解释。",
        "evidence_boundary": "当前证据不支持因果结论。",
        "display_summary": "已形成业务观察，并保留因果边界。",
    },
    "answer_synthesis": {
        "answer_text": "基于已验证证据引用生成答案草稿。",
        "display_summary": "已根据可验证证据形成业务答案草稿。",
    },
    "semantic_audit": {
        "audit_status": "passed",
        "issues": [],
    },
    "causal_audit": {
        "causal_assessment": "not_supported",
        "publishable_wording": "会计贡献可保留，深层业务机制尚未获得独立证据。",
        "supporting_reasons": ["当前证据显示可观察现象，但缺少对照或机制验证。"],
        "evidence_limit": "当前缺少独立机制证据。",
        "display_summary": "会计贡献可发布，深层机制仍缺少独立证据。",
    },
    "answer_repair": {
        "answer_text": "已按校验反馈修正答案草稿。",
        "display_summary": "已修正业务答案中的事实和边界表达。",
    },
    "final_answer_audit": {
        "material_findings": [],
    },
    "degraded_explanation": {
        "explanation": "当前证据有限。",
        "repair_path": "补充业务证据后重跑。",
        "display_summary": "当前仅提供可验证的证据边界。",
    },
    "blocked_explanation": {
        "status": "blocked",
        "explanation": "当前存在硬边界，无法继续执行。",
        "repair_path": "先解除阻断边界后重跑。",
        "display_summary": "当前存在会阻止业务结论发布的硬边界。",
    },
}


def _default_final_business_summary(messages):
    payload = _input_payload(messages)
    context = payload.get("businessContext") if isinstance(payload, dict) else {}
    context = context if isinstance(context, dict) else {}
    evidence = context.get("evidence") if isinstance(context, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    claim_slots = [
        item
        for item in evidence.get("claimSlots") or []
        if isinstance(item, dict)
        and str(item.get("claimSlot") or "")
        and str(item.get("statement") or "").strip()
    ]
    understanding = str(
        context.get("questionUnderstanding")
        or "用户希望基于当前可验证数据得到业务结论。"
    ).strip()
    understanding = understanding.removeprefix("我对问题的理解是：").strip()
    analysis_path = str(
        context.get("analysisPath")
        or "先核对数据范围，再检查可发布的业务证据。"
    ).strip()
    analysis_path = analysis_path.removeprefix("分析思路：").strip()
    causal_boundary = str(
        context.get("causalBoundary")
        or "当前结论只保留已验证事实及其证据边界。"
    ).strip()
    if claim_slots:
        primary = claim_slots[0]
        claim_text = str(primary["statement"]).strip()
        statement_bindings = [
            {
                "excerpt": claim_text,
                "statement_class": "verified_claim",
                "authority_keys": [str(primary["claimSlot"])],
            }
        ]
        display_summary = claim_text
    else:
        claim_text = "当前没有可发布的业务事实，本轮保留数据边界。"
        statement_bindings = []
        display_summary = claim_text
    return {
        "summary_text": (
            f"我对问题的理解是：{understanding}\n"
            f"分析脉络：{analysis_path}\n"
            f"关键发现：{claim_text}\n"
            f"最终结论：{claim_text}\n"
            f"需要注意：{causal_boundary}"
        ),
        "statement_bindings": statement_bindings,
        "display_summary": display_summary,
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
