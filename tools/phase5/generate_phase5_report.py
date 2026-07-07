#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_NODE_AUDIT = (
    ROOT
    / "artifacts"
    / "phase-4"
    / "ten-case-node-audit-20260707-postfix"
    / "node_audit_summary.json"
)
DEFAULT_CLARIFICATION_CASES = ROOT / "evals" / "phase5" / "implicit_clarification_cases.yaml"
DEFAULT_OUTPUT = ROOT / "docs" / "reviews" / "phase5-eval-audit-20260707.md"

EXPECTED_ROUTE_CAPABILITIES = {
    "full_wajespecial_vs_other_by_month": {"compare_periods"},
    "full_q2_vs_q1_by_year": {"compare_periods"},
}


def build_report_model(
    cases: Sequence[Mapping[str, Any]],
    clarification_suite: Mapping[str, Any],
) -> dict[str, Any]:
    weak_evidence_case_ids = [
        case["case_id"]
        for case in cases
        if _is_weak_or_degraded(case)
    ]
    route_drift_case_ids = [
        case["case_id"]
        for case in cases
        if _route_drift_observed(case)
    ]
    clarification_cases = list(clarification_suite.get("cases", ()))

    return {
        "case_count": len(cases),
        "status_counts": dict(Counter(case.get("eval_status") for case in cases)),
        "published_counts": dict(
            Counter(
                "published"
                if case.get("business_conclusion_published")
                else "blocked_or_degraded"
                for case in cases
            )
        ),
        "weak_evidence_case_ids": weak_evidence_case_ids,
        "route_drift_case_ids": route_drift_case_ids,
        "clarification_case_count": len(clarification_cases),
        "clarification_expected_counts": dict(
            Counter(case.get("expected_boundary_status") for case in clarification_cases)
        ),
        "clarification_latent_choices": [
            case.get("latent_choice", "") for case in clarification_cases
        ],
    }


def render_report(
    *,
    cases: Sequence[Mapping[str, Any]],
    clarification_suite: Mapping[str, Any],
    model: Mapping[str, Any],
) -> str:
    status = model["status_counts"]
    published = model["published_counts"]
    clarification_expected = model["clarification_expected_counts"]
    blocked_or_failed = status.get("blocked", 0) + status.get("failed", 0)
    lines = [
        "# Phase 5 Eval Audit Report",
        "",
        "生成日期：2026-07-07",
        "",
        "## 现状是什么",
        "",
        f"- Phase 4 全周期 10 case 已可作为 Phase 5 输入：{status.get('passed', 0)} 个通过，{status.get('degraded', 0)} 个降级，{blocked_or_failed} 个阻断或失败。",
        f"- 业务主结论发布状态：{published.get('published', 0)} 个已发布，{published.get('blocked_or_degraded', 0)} 个因证据边界未发布主结论。",
        f"- 隐性澄清套件已有 {model['clarification_case_count']} 个用例，期望状态为 {_format_counts(clarification_expected)}。",
        "- 当前没有发现全量隐性澄清套件的 live run 汇总产物；已有单测只验证 `needs_question` 路径可被评估辅助函数读取。",
        "",
        "## 10 Case 结果",
        "",
        "| Case | 状态 | 主证据 | 强度 | 结论发布 | 主要限制 |",
        "|---|---|---|---|---|---|",
    ]
    for case in cases:
        evidence = case.get("primary_evidence", {}) or {}
        limitations = ", ".join(evidence.get("limitations", ()) or ()) or "-"
        lines.append(
            "| {case_id} | {status} | {capability} | {strength}/{wording} | {published} | {limitations} |".format(
                case_id=case.get("case_id", ""),
                status=case.get("eval_status", ""),
                capability=evidence.get("capability", ""),
                strength=evidence.get("strength", ""),
                wording=evidence.get("wording_limit", ""),
                published="是" if case.get("business_conclusion_published") else "否",
                limitations=limitations,
            )
        )

    lines.extend(
        [
            "",
            "## 问题在哪",
            "",
            f"- 弱证据或降级仍是主问题：{len(model['weak_evidence_case_ids'])} 个 case 不能支撑强结论，主要集中在方向不稳定、低于重要性阈值、可比周期不足。",
            f"- route drift 已有可见风险：{', '.join(model['route_drift_case_ids']) or '当前汇总未识别'}。这些 case 证据数字可用，但主能力选择可能影响 replay 信任和证据形态。",
            "- unsupported claim 风险目前被 verifier 和降级状态压住：降级 case 没有发布主结论。后续风险点是摘要文字如果把 tendency 或 insufficient 写成稳定结论，就会越过证据边界。",
            "- ask-question 仍处于潜在歧义验证阶段：用例覆盖了总金额/日均、合并基线/分渠道基线、严格每期/多数倾向、日历窗口/业务事件窗口，但还缺一轮全套运行产物。",
            "",
            "## 应该怎么改",
            "",
            "- Phase 5 先补全报告和 eval gate：每次 10 case 运行后生成同类审计报告，避免人工翻 JSON 判断证据边界。",
            "- 对 route drift 先记录影响范围和答案影响；只有错配能改变结论或明显损害 replay 信任时，再进入人工审核后的 guardrail 候选。",
            "- 对降级摘要继续压实表达：必须写清证据支持的倾向、没有支持的主结论、缺的业务事件或可比周期。",
            "- 对隐性澄清套件跑完整 harness，结果要记录 `needs_question`、推荐假设和用户可改写出口。",
            "",
            "## 进入 Phase 6",
            "",
            "- 扩展到更多业务问题族和组合意图。",
            "- 建立更完整的 capability-support ledger，把事件/机制证据缺口从报告风险变成可执行补数路线。",
            "- 在 Phase 5 gate 稳定后，再决定哪些 route drift 模式需要 compiler 固化。",
            "",
            "## Phase 5 保留项",
            "",
            "- 当前报告只基于已有 10 case 汇总和隐性澄清 YAML；没有重跑 ClickHouse live eval。",
            "- 隐性澄清套件还缺全量执行后的 artifact summary。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    cases = json.loads(DEFAULT_NODE_AUDIT.read_text(encoding="utf-8"))
    clarification_suite = yaml.safe_load(DEFAULT_CLARIFICATION_CASES.read_text(encoding="utf-8"))
    model = build_report_model(cases, clarification_suite)
    DEFAULT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT.write_text(
        render_report(cases=cases, clarification_suite=clarification_suite, model=model),
        encoding="utf-8",
    )
    print(DEFAULT_OUTPUT.relative_to(ROOT))
    return 0


def _is_weak_or_degraded(case: Mapping[str, Any]) -> bool:
    evidence = case.get("primary_evidence", {}) or {}
    return (
        case.get("eval_status") != "passed"
        or evidence.get("strength") == "low"
        or evidence.get("wording_limit") in {"insufficient", "tendency"}
    )


def _route_drift_observed(case: Mapping[str, Any]) -> bool:
    expected = EXPECTED_ROUTE_CAPABILITIES.get(str(case.get("case_id")))
    if not expected:
        return False
    evidence = case.get("primary_evidence", {}) or {}
    return evidence.get("capability") not in expected


def _format_counts(counts: Mapping[str, Any]) -> str:
    return "，".join(f"{key} {value} 个" for key, value in counts.items()) or "无"


if __name__ == "__main__":
    sys.exit(main())
