from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


_BINDING_SCHEMA_VERSION = "final-narrative-publication-binding.v4"
_AUTHORITY_SCHEMA_VERSION = "narrative-authority-record.v3"
_STATEMENT_CLASSES = frozenset(
    {
        "verified_claim",
        "factor_contribution",
        "factor_observation",
        "data_boundary",
        "analysis_scope",
        "next_check",
    }
)
_FULL_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<iso>\d{4}-\d{1,2}-\d{1,2})(?!\d)"
    r"|(?<!\d)(?P<cn>\d{4}年\d{1,2}月\d{1,2}日)(?!\d)"
)
_PARTIAL_DATE_PATTERN = re.compile(r"(?<!\d)(?P<month>\d{1,2})月(?P<day>\d{1,2})日(?!\d)")
_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[+\-−－＋]?(?:\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.\d+)?[ \t]*(?:%|万|亿|[kKmM])?"
)
_RATIO_FIELD_MARKERS = (
    "ratio",
    "rate",
    "share",
    "percent",
    "relative",
    "change_rate",
    "changerate",
    "coverage",
    "贡献占比",
    "变化率",
)
_MATERIAL_REVIEW_CODES = frozenset(
    {
        "unsupported_material_claim",
        "claim_paraphrase_drift",
        "claim_paraphrase_unclear",
        "internal_visible_token",
        "missing_primary_claim",
        "missing_driver_claim",
    }
)
_INTERNAL_VISIBLE_TOKENS = (
    "paid_amount",
    "payment_amount",
    "pattern_status",
    "pattern_established",
    "wording_limit",
    "pattern_scan",
    "data_quality_check",
    "evidence_ref",
    "custom_baseline",
    "intra_period",
    "event_evidence",
    "segment_bridge",
    "outlier_scan",
    "data_engineering_owner",
    "business_analysis_owner",
    "all_users",
    "monthly_daily_avg",
)
_MECHANISM_CAUSAL_PATTERN = re.compile(
    r"导致|造成|归因于|根本原因|唯一原因|因为|由于|使得|"
    r"业务机制|作用机制|已证明|证明了|必然|源于|来自于|带来"
)
_ACCOUNTING_LANGUAGE_PATTERN = re.compile(
    r"贡献|主要贡献|最大贡献|正向|负向|抵消|核算驱动|驱动"
)
_DIRECTION_LANGUAGE_PATTERN = re.compile(
    r"上涨|下跌|上升|下降|增加|减少|增长|回落|提升|降低|正向|负向|抵消"
)
_POSITIVE_DIRECTION_PATTERN = re.compile(r"上涨|上升|增加|增长|提升|正向")
_NEGATIVE_DIRECTION_PATTERN = re.compile(r"下跌|下降|减少|回落|降低|负向")
_STRENGTH_LANGUAGE_PATTERN = re.compile(
    r"高强度证据|中等强度证据|低强度证据|高置信度|确定可靠|确定性结论|已证实"
)
_QUESTION_MARKERS = ("是否", "能否", "有没有", "是不是", "为什么", "哪些", "如何")
_NEGATIVE_CAUSAL_BOUNDARY = re.compile(
    r"(?:无法|不能|尚未|未能|没有证据|缺少证据|证据不足|"
    r"不说明|不能说明|不代表|不等于).{0,20}"
    r"(?:确认|证明|支持|判断|说明|表明|业务机制|原因|因果)?"
)
_BOUNDARY_LANGUAGE_PATTERN = re.compile(
    r"仅适用|不推断|不重复|不可相加|缺少|未知|按不变处理|"
    r"尚未|未观察|无法|不能|证据不足|需进一步验证|限制"
)
_TARGET_ROLE_MARKERS = (
    "目标窗口",
    "目标日期",
    "目标期",
    "目标日",
    "目标",
)
_BASELINE_ROLE_MARKERS = (
    "基线窗口",
    "基准日期",
    "基线日期",
    "基线期",
    "基准日",
    "基线日",
    "基准",
    "基线",
    "前一天",
)
_STRENGTH_MARKER_RANKS = (
    ("高强度证据", 3),
    ("中等强度证据", 2),
    ("低强度证据", 1),
    ("高置信度", 4),
    ("确定可靠", 4),
    ("确定性结论", 4),
    ("已证实", 4),
)
_AUTHORITY_STRENGTH_RANKS = {
    "causal": 4,
    "high": 3,
    "medium": 2,
    "observed": 1,
    "low": 1,
    "degraded": 0,
    "insufficient": 0,
    "blocked": 0,
}
_PUBLISHABLE_INSIGHT_STATES = frozenset({"verified", "derived"})
_DIAGNOSTIC_INSIGHT_COLLECTIONS = (
    "insights",
    "counterfactuals",
    "growth_quality_signals",
    "dimension_findings",
    "cross_source_findings",
)
_INSIGHT_NUMERIC_FIELD_MARKERS = (
    "value",
    "amount",
    "change",
    "rate",
    "ratio",
    "share",
    "contribution",
    "count",
    "frequency",
    "total",
    "coefficient",
    "pearson",
    "spearman",
    "coverage",
    "sample_size",
    "lag",
)
_FACTOR_FIELD_LABELS = {
    "paid_users": "付费人数",
    "paid_orders": "付费订单数",
    "paid_frequency": "付费频次",
    "avg_order_amount": "单笔付费金额",
    "first_paid_users": "首充人数",
    "first_paid_user_ratio": "首充用户占比",
    "payment_success_rate": "支付成功率",
}


def build_narrative_publication_review_record(
    quality_gate: Mapping[str, Any],
) -> dict[str, Any]:
    return _publication_review_projection(quality_gate)


def build_narrative_question_scope(
    analysis_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    contract = analysis_contract if isinstance(analysis_contract, Mapping) else {}
    windows = [
        dict(item)
        for item in contract.get("resolved_windows") or ()
        if isinstance(item, Mapping)
    ]
    by_role = {
        str(item.get("role") or ""): item
        for item in windows
        if str(item.get("role") or "")
    }

    def window(role: str) -> dict[str, Any]:
        source = by_role.get(role) or {}
        return {
            key: source[key]
            for key in (
                "window_id",
                "role",
                "label",
                "start_inclusive",
                "end_exclusive",
                "timezone",
            )
            if source.get(key) not in (None, "")
        }

    target = window("target")
    baseline = window("baseline")
    return canonical_value(
        {
            "scope": contract.get("scope") or {},
            "target": target,
            "baseline": baseline,
            "time_window": str(target.get("label") or ""),
        }
    )


def build_narrative_authority_record(
    *,
    verified_claims: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    visible_limitations: Sequence[Any],
    accepted_assumptions: Sequence[Mapping[str, Any]],
    question_scope: Mapping[str, Any] | None = None,
    diagnostic_insights: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence_items = tuple(
        dict(item) for item in evidence if isinstance(item, Mapping)
    )
    evidence_by_ref = {
        str(item.get("evidence_ref") or ""): item
        for item in evidence_items
        if str(item.get("evidence_ref") or "")
    }
    claims: list[dict[str, Any]] = []
    for index, raw_claim in enumerate(verified_claims, start=1):
        claim = dict(raw_claim)
        claim_ref = str(claim.get("claim_ref") or "")
        if not claim_ref:
            raise ValueError("narrative_authority_claim_ref_missing")
        evidence_types = sorted(
            {
                str(evidence_by_ref.get(str(ref), {}).get("evidence_type") or "")
                for ref in claim.get("evidence_refs") or ()
                if str(evidence_by_ref.get(str(ref), {}).get("evidence_type") or "")
            }
        )
        claims.append(
            canonical_value(
                {
                    "authority_key": f"结论{index}",
                    "claim_ref": claim_ref,
                    "claim_type": str(claim.get("claim_type") or ""),
                    "claim_strength": str(claim.get("claim_strength") or ""),
                    "statement": str(claim.get("text") or ""),
                    "target_metric": str(claim.get("target_metric") or ""),
                    "target": claim.get("target") or {},
                    "baseline": claim.get("baseline") or {},
                    "comparison_direction": str(
                        claim.get("comparison_direction") or ""
                    ),
                    "dimensions": claim.get("dimensions") or {},
                    "numbers": claim.get("numbers") or {},
                    "evidence_refs": list(claim.get("evidence_refs") or ()),
                    "evidence_types": evidence_types,
                    "causal_authority": "causal_evidence" in evidence_types,
                }
            )
        )

    factor_states = _factor_states_from_evidence(evidence_items)
    factor_states = _merge_claim_factor_states(claims, factor_states)
    insight_authorities = _diagnostic_insight_authorities(
        diagnostic_insights,
        evidence_by_ref=evidence_by_ref,
    )
    limitations = sorted(
        (canonical_value(item) for item in visible_limitations),
        key=canonical_digest,
    )
    assumptions = sorted(
        (
            canonical_value(item)
            for item in accepted_assumptions
            if isinstance(item, Mapping)
        ),
        key=canonical_digest,
    )
    return canonical_value(
        {
            "schema_version": _AUTHORITY_SCHEMA_VERSION,
            "claims": claims,
            "factor_states": factor_states,
            "diagnostic_insights": insight_authorities,
            "limitations": limitations,
            "accepted_assumptions": assumptions,
            "question_scope": canonical_value(question_scope or {}),
            "causal_boundary": (
                "当前证据无法确认业务机制或更深层原因；"
                "已对账的组成贡献只支持主要贡献项和抵消关系。"
            ),
        }
    )


def build_final_narrative_publication_binding(
    *,
    narrative: str,
    statement_bindings: Sequence[Mapping[str, Any]],
    authority_record: Mapping[str, Any],
    quality_gate: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    text = str(narrative or "")
    normalized_statements, shape_errors = _normalize_statement_bindings(
        statement_bindings,
        narrative=text,
    )
    record = canonical_value(authority_record)
    normalized_statements, class_audits = _normalize_statement_classes(
        normalized_statements,
        authority_record=record,
    )
    errors = list(shape_errors)
    publication_errors, statement_reviews = _narrative_publication_errors(
        narrative=text,
        statement_bindings=normalized_statements,
        authority_record=record,
        quality_gate=quality_gate,
    )
    errors.extend(publication_errors)
    errors = list(dict.fromkeys(errors))
    accepted_statement_indexes = [
        int(review["statement_index"])
        for review in statement_reviews
        if review.get("status") == "bound"
    ]
    rejected_statement_indexes = [
        int(review["statement_index"])
        for review in statement_reviews
        if review.get("status") == "rejected"
    ]
    authority_digest = canonical_digest(record)
    binding = {
        "schema_version": _BINDING_SCHEMA_VERSION,
        "status": "bound" if not errors else "rejected",
        "narrative_digest": _narrative_digest(text),
        "statement_bindings_digest": canonical_digest(normalized_statements),
        "claim_authority_refs": [
            str(claim.get("claim_ref") or "")
            for claim in record.get("claims") or ()
            if isinstance(claim, Mapping)
        ],
        "narrative_authority_ref": (
            f"narrative-authority:sha256:{authority_digest}"
        ),
        "authority_record_digest": authority_digest,
        "publication_review_digest": canonical_digest(
            _publication_review_projection(quality_gate)
        ),
        "statement_class_audits": list(class_audits),
        "statement_reviews": list(statement_reviews),
        "accepted_statement_indexes": accepted_statement_indexes,
        "rejected_statement_indexes": rejected_statement_indexes,
        "validation_errors": errors,
    }
    return binding, tuple(errors)


def build_authority_safe_narrative(
    authority_record: Mapping[str, Any],
    *,
    required_claim_types: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Project a publishable business summary from already accepted authority.

    The projection is used only when a prose writer or the separate authority
    binder cannot produce a locally valid publication.  It never repairs facts
    by inference: every retained sentence is either an accepted claim, a
    publishable diagnostic statement, an explicit factor state, or a visible
    evidence boundary.  Candidates are admitted one at a time through the same
    final publication validator used for model-authored prose.
    """

    record = canonical_value(authority_record)
    candidates = _authority_narrative_candidates(record)
    accepted: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    final_binding: dict[str, Any] = {}

    for candidate in candidates:
        proposed = [*accepted, candidate]
        narrative, statement_bindings = _render_authority_narrative(proposed)
        binding, errors = build_final_narrative_publication_binding(
            narrative=narrative,
            statement_bindings=statement_bindings,
            authority_record=record,
            quality_gate={},
        )
        if errors:
            omitted.append(
                {
                    "authority_key": candidate["authority_key"],
                    "validation_errors": list(errors),
                }
            )
            continue
        accepted.append(candidate)
        final_binding = binding

    narrative, statement_bindings = _render_authority_narrative(accepted)
    claim_by_authority_key = {
        str(item.get("authority_key") or ""): item
        for item in record.get("claims") or ()
        if isinstance(item, Mapping) and str(item.get("authority_key") or "")
    }
    required_authority_keys = (
        {
            authority_key
            for authority_key, item in claim_by_authority_key.items()
            if str(item.get("statement") or "").strip()
        }
        if required_claim_types is None
        else set()
    )
    required_claim_type_set = {
        str(value) for value in required_claim_types or () if str(value)
    }
    accepted_authority_keys = {
        str(item.get("authority_key") or "") for item in accepted
    }
    missing_required_authorities = sorted(
        required_authority_keys - accepted_authority_keys
    )
    accepted_claim_types = {
        str(claim_by_authority_key[authority_key].get("claim_type") or "")
        for authority_key in accepted_authority_keys
        if authority_key in claim_by_authority_key
    }
    missing_required_claim_types = sorted(
        required_claim_type_set - accepted_claim_types
    )
    has_substantive_authority = any(
        item.get("section") in {"management", "diagnostic", "factor"}
        for item in accepted
    )
    if (
        accepted
        and has_substantive_authority
        and not missing_required_authorities
        and not missing_required_claim_types
    ):
        final_binding, final_errors = build_final_narrative_publication_binding(
            narrative=narrative,
            statement_bindings=statement_bindings,
            authority_record=record,
            quality_gate={},
        )
    elif missing_required_authorities:
        final_errors = tuple(
            f"required_authority_not_publishable:{authority_key}"
            for authority_key in missing_required_authorities
        )
    elif missing_required_claim_types:
        final_errors = tuple(
            f"required_claim_type_not_publishable:{claim_type}"
            for claim_type in missing_required_claim_types
        )
    else:
        final_errors = ("authority_safe_narrative_has_no_business_conclusion",)

    return canonical_value(
        {
            "status": (
                "bound"
                if (
                    accepted
                    and has_substantive_authority
                    and not missing_required_authorities
                    and not missing_required_claim_types
                    and not final_errors
                )
                else "rejected"
            ),
            "narrative": narrative,
            "statement_bindings": list(statement_bindings),
            "publication_binding": final_binding,
            "accepted_authority_keys": [
                item["authority_key"] for item in accepted
            ],
            "required_authority_keys": sorted(required_authority_keys),
            "missing_required_authority_keys": missing_required_authorities,
            "required_claim_types": sorted(required_claim_type_set),
            "missing_required_claim_types": missing_required_claim_types,
            "omitted_authorities": omitted,
            "validation_errors": list(final_errors),
        }
    )


def _authority_narrative_candidates(
    authority_record: Mapping[str, Any],
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    claim_text = "\n".join(
        str(item.get("statement") or "")
        for item in authority_record.get("claims") or ()
        if isinstance(item, Mapping)
    )

    for claim in authority_record.get("claims") or ():
        if not isinstance(claim, Mapping):
            continue
        statement = str(claim.get("statement") or "").strip()
        authority_key = str(claim.get("authority_key") or "").strip()
        if not statement or not authority_key:
            continue
        candidates.append(
            {
                "section": "management",
                "statement": statement,
                "statement_class": _claim_statement_class(claim),
                "authority_key": authority_key,
            }
        )

    for insight in authority_record.get("diagnostic_insights") or ():
        if not isinstance(insight, Mapping):
            continue
        statement = str(insight.get("statement") or "").strip()
        authority_key = str(insight.get("authority_key") or "").strip()
        if not statement or not authority_key:
            continue
        candidates.append(
            {
                "section": "diagnostic",
                "statement": statement,
                "statement_class": "verified_claim",
                "authority_key": authority_key,
            }
        )

    for factor in authority_record.get("factor_states") or ():
        if not isinstance(factor, Mapping):
            continue
        factor_name = str(factor.get("factor") or "").strip()
        if not factor_name or _claim_text_covers_factor_state(
            claim_text,
            factor_name=factor_name,
            factor=factor,
        ):
            continue
        statement = _factor_authority_statement(factor)
        if not statement:
            continue
        state = str(factor.get("state") or "")
        if state == "已量化贡献":
            statement_class = "factor_contribution"
        elif state == "已观察变化，贡献尚未量化":
            statement_class = "factor_observation"
        else:
            statement_class = "data_boundary"
        candidates.append(
            {
                "section": "factor",
                "statement": statement,
                "statement_class": statement_class,
                "authority_key": factor_name,
            }
        )

    for index, limitation in enumerate(
        authority_record.get("limitations") or (), start=1
    ):
        statement = _limitation_statement(limitation)
        if not statement:
            continue
        candidates.append(
            {
                "section": "boundary",
                "statement": statement,
                "statement_class": "data_boundary",
                "authority_key": f"数据边界{index}",
            }
        )

    causal_boundary = str(authority_record.get("causal_boundary") or "").strip()
    if causal_boundary:
        candidates.append(
            {
                "section": "boundary",
                "statement": causal_boundary,
                "statement_class": "data_boundary",
                "authority_key": "原因边界",
            }
        )

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        token = (candidate["statement"], candidate["authority_key"])
        if token in seen:
            continue
        seen.add(token)
        deduped.append(candidate)
    return deduped


def _claim_statement_class(claim: Mapping[str, Any]) -> str:
    claim_type = str(claim.get("claim_type") or "")
    if claim_type == "formula_component_contribution":
        return "factor_contribution"
    if claim_type == "contract_coverage_and_trust_boundary":
        return "data_boundary"
    if claim_type in {"observed_factor_change", "observed_activity"}:
        return "factor_observation"
    return "verified_claim"


def _claim_text_covers_factor_state(
    claim_text: str,
    *,
    factor_name: str,
    factor: Mapping[str, Any],
) -> bool:
    clauses = [
        clause
        for clause in re.split(r"[。；;!?？\n]+", claim_text)
        if factor_name in clause
    ]
    if not clauses:
        return False
    numeric_fields = (
        "baseline",
        "target",
        "change",
        "changeRate",
        "contribution",
        "contributionShare",
    )
    for field in numeric_fields:
        if factor.get(field) is None:
            continue
        if not _authority_fact_is_mentioned(
            " ".join(clauses),
            factor[field],
            ratio=field in {"changeRate", "contributionShare"},
        ):
            return False
    state = str(factor.get("state") or "")
    if state == "已量化贡献":
        return any("贡献" in clause for clause in clauses)
    if state == "已观察变化，贡献尚未量化":
        return any(
            marker in clause
            for clause in clauses
            for marker in ("观察", "变化", "升至", "降至", "增至", "减至")
        )
    return any(
        marker in clause
        for clause in clauses
        for marker in ("缺少", "没有", "无独立观测", "按不变处理")
    )


def _authority_fact_is_mentioned(
    text: str,
    value: Any,
    *,
    ratio: bool,
) -> bool:
    number = _decimal(value)
    if number is None:
        return str(value) in text
    candidates = {
        str(value),
        _authority_display_number(value, ratio=ratio),
    }
    if not ratio:
        candidates.add(f"{number:,.2f}")
        candidates.add(f"{number:,.0f}")
    return any(candidate and candidate in text for candidate in candidates)


def _factor_authority_statement(factor: Mapping[str, Any]) -> str:
    factor_name = str(factor.get("factor") or "").strip()
    state = str(factor.get("state") or "").strip()
    if not factor_name or not state:
        return ""
    numeric_fields = (
        ("baseline", "基线期"),
        ("target", "目标期"),
        ("change", "变化"),
        ("changeRate", "变化率"),
        ("contribution", "贡献"),
        ("contributionShare", "贡献份额"),
    )
    parts: list[str] = []
    for field, label in numeric_fields:
        value = factor.get(field)
        if value is None:
            continue
        parts.append(f"{label}{_authority_display_number(value, ratio=field in {'changeRate', 'contributionShare'})}")
    if not parts:
        return f"{factor_name}：{state}。"
    return f"{factor_name}：{state}，{'，'.join(parts)}。"


def _authority_display_number(value: Any, *, ratio: bool) -> str:
    number = _decimal(value)
    if number is None:
        return str(value)
    if ratio:
        number *= Decimal("100")
        rendered = format(number.quantize(Decimal("0.01")), "f")
        return f"{rendered}%"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _limitation_statement(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return ""
    for key in ("business_impact", "statement", "message", "summary"):
        statement = str(value.get(key) or "").strip()
        if statement:
            return statement
    return ""


def _render_authority_narrative(
    candidates: Sequence[Mapping[str, str]],
) -> tuple[str, tuple[dict[str, Any], ...]]:
    section_labels = {
        "management": "管理结论",
        "diagnostic": "补充诊断",
        "factor": "指标状态",
        "boundary": "证据边界",
    }
    section_order = tuple(section_labels)
    paragraphs: list[str] = []
    bindings: list[dict[str, Any]] = []
    for section in section_order:
        section_items = [
            item for item in candidates if item.get("section") == section
        ]
        if not section_items:
            continue
        paragraphs.append(
            section_labels[section]
            + "：\n"
            + "\n".join(f"- {item['statement']}" for item in section_items)
        )
        bindings.extend(
            {
                "excerpt": str(item["statement"]),
                "statement_class": str(item["statement_class"]),
                "authority_keys": [str(item["authority_key"])],
            }
            for item in section_items
        )
    return "\n\n".join(paragraphs), tuple(bindings)


def _diagnostic_insight_authorities(
    diagnostic_insights: Mapping[str, Any] | None,
    *,
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    portfolio = (
        diagnostic_insights
        if isinstance(diagnostic_insights, Mapping)
        else {}
    )
    eligible: list[dict[str, Any]] = []
    for collection in _DIAGNOSTIC_INSIGHT_COLLECTIONS:
        raw_items = portfolio.get(collection) or ()
        if isinstance(raw_items, (str, bytes, bytearray)) or not isinstance(
            raw_items,
            Sequence,
        ):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                continue
            item = canonical_value(raw_item)
            evidence_state = str(item.get("evidence_state") or "").lower()
            if evidence_state not in _PUBLISHABLE_INSIGHT_STATES:
                continue
            source_refs = _unique_string_sequence(item.get("source_evidence_refs"))
            if not source_refs or any(
                ref not in evidence_by_ref for ref in source_refs
            ):
                continue
            source_result_refs = _unique_string_sequence(
                item.get("source_result_refs")
            )
            owned_result_refs = {
                str(result_ref)
                for ref in source_refs
                for result_ref in evidence_by_ref[ref].get("result_refs") or ()
                if str(result_ref)
            }
            if any(
                result_ref not in owned_result_refs
                for result_ref in source_result_refs
            ):
                continue
            numbers = {
                str(field): value
                for field, value in item.items()
                if _insight_numeric_field(str(field), value)
            }
            source_types = sorted(
                {
                    str(evidence_by_ref[ref].get("evidence_type") or "")
                    for ref in source_refs
                    if str(evidence_by_ref[ref].get("evidence_type") or "")
                }
            )
            eligible.append(
                canonical_value(
                    {
                        "source_collection": collection,
                        "insight_type": str(
                            item.get("insight_type")
                            or item.get("counterfactual_type")
                            or item.get("signal_type")
                            or ""
                        ),
                        "evidence_state": evidence_state,
                        "claim_strength": "observed",
                        "statement": str(
                            item.get("statement")
                            or item.get("summary")
                            or ""
                        ),
                        "numbers": numbers,
                        "comparison_direction": str(
                            item.get("direction_without_factor")
                            or item.get("direction")
                            or ""
                        ),
                        "business_labels": _insight_business_labels(item),
                        "derivation": str(item.get("derivation") or ""),
                        "source_evidence_refs": source_refs,
                        **(
                            {"source_result_refs": source_result_refs}
                            if source_result_refs
                            else {}
                        ),
                        "evidence_types": source_types,
                        "causal_authority": bool(
                            evidence_state == "verified"
                            and "causal_evidence" in source_types
                        ),
                    }
                )
            )
    eligible.sort(key=canonical_digest)
    return [
        canonical_value({"authority_key": f"洞察{index}", **item})
        for index, item in enumerate(eligible, start=1)
    ]


def _unique_string_sequence(value: Any) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item)))


def _insight_numeric_field(field: str, value: Any) -> bool:
    normalized = str(field or "").lower()
    if normalized.endswith(("_id", "_ref")) or normalized in {
        "id",
        "window_id",
    }:
        return False
    if not any(marker in normalized for marker in _INSIGHT_NUMERIC_FIELD_MARKERS):
        return False
    if isinstance(value, bool) or isinstance(value, Mapping):
        return False
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return False
    return _decimal(value) is not None


def _insight_business_labels(item: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    for field, value in item.items():
        normalized = str(field or "").lower()
        if normalized.endswith("_id") or normalized.endswith("_ref"):
            continue
        if not (
            normalized
            in {
                "factor",
                "region",
                "city",
                "segment",
                "dimension",
                "outcome_metric",
                "candidate_metric",
            }
            or (
                normalized.endswith("_factor")
                and not normalized.startswith(("direction_", "change_"))
            )
            or normalized.endswith("_label")
        ):
            continue
        if isinstance(value, str) and value.strip():
            labels.append(value.strip())
    return list(dict.fromkeys(labels))


def final_narrative_binding_errors(
    *,
    binding: Mapping[str, Any] | None,
    narrative: str,
    statement_bindings: Sequence[Mapping[str, Any]],
    authority_record: Mapping[str, Any],
    quality_gate: Mapping[str, Any],
) -> tuple[str, ...]:
    if not isinstance(binding, Mapping):
        return ("binding_missing",)
    expected, content_errors = build_final_narrative_publication_binding(
        narrative=narrative,
        statement_bindings=statement_bindings,
        authority_record=authority_record,
        quality_gate=quality_gate,
    )
    errors = list(content_errors)
    if binding.get("schema_version") != _BINDING_SCHEMA_VERSION:
        errors.append("binding_schema_invalid")
    if binding.get("status") != "bound":
        errors.append("binding_not_bound")
    if list(binding.get("validation_errors") or ()):
        errors.append("binding_has_validation_errors")
    for field, code in (
        ("narrative_digest", "narrative_digest_mismatch"),
        ("statement_bindings_digest", "statement_bindings_digest_mismatch"),
        ("publication_review_digest", "publication_review_digest_mismatch"),
    ):
        if str(binding.get(field) or "") != str(expected.get(field) or ""):
            errors.append(code)
    if (
        str(binding.get("authority_record_digest") or "")
        != str(expected.get("authority_record_digest") or "")
        or str(binding.get("narrative_authority_ref") or "")
        != str(expected.get("narrative_authority_ref") or "")
    ):
        errors.append("authority_record_mismatch")
    if tuple(str(value) for value in binding.get("claim_authority_refs") or ()) != tuple(
        str(value) for value in expected.get("claim_authority_refs") or ()
    ):
        errors.append("claim_authority_refs_mismatch")
    return tuple(dict.fromkeys(errors))


def _narrative_publication_errors(
    *,
    narrative: str,
    statement_bindings: Sequence[Mapping[str, Any]],
    authority_record: Mapping[str, Any],
    quality_gate: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    errors: list[str] = []
    statement_reviews: list[dict[str, Any]] = []
    if not narrative.strip():
        return ("narrative_missing",), ()
    if narrative != narrative.strip():
        errors.append("narrative_not_trimmed")
    if authority_record.get("schema_version") != _AUTHORITY_SCHEMA_VERSION:
        errors.append("authority_record_schema_invalid")
    if quality_gate.get("blocks_display") is True:
        errors.append("quality_gate_blocks_display")
    if tuple(quality_gate.get("hard_blockers") or ()):
        errors.append("quality_gate_hard_blockers")
    material_warnings = {
        str(value)
        for value in quality_gate.get("repairable_warnings") or ()
        if str(value)
    }.intersection(_MATERIAL_REVIEW_CODES)
    if material_warnings:
        errors.append("material_expression_review_unresolved")
    if any(token in narrative for token in _INTERNAL_VISIBLE_TOKENS):
        errors.append("internal_visible_token")
    if not statement_bindings:
        material_conclusions = _material_conclusion_text(narrative)
        if (
            _FULL_DATE_PATTERN.search(material_conclusions)
            or _PARTIAL_DATE_PATTERN.search(material_conclusions)
            or _NUMBER_PATTERN.search(_mask_dates(material_conclusions))
            or _MECHANISM_CAUSAL_PATTERN.search(material_conclusions)
            or _ACCOUNTING_LANGUAGE_PATTERN.search(material_conclusions)
        ):
            errors.append("statement_bindings_missing")
        return tuple(dict.fromkeys(errors)), ()

    authority_by_key = _authority_by_key(authority_record)
    for statement_index, statement in enumerate(statement_bindings):
        excerpt = str(statement.get("excerpt") or "")
        statement_class = str(statement.get("statement_class") or "")
        keys = tuple(str(key) for key in statement.get("authority_keys") or ())
        selected = [authority_by_key[key] for key in keys if key in authority_by_key]
        statement_errors: list[str] = []
        if len(selected) != len(keys):
            statement_errors.append("statement_authority_key_unknown")
            errors.extend(statement_errors)
            statement_reviews.append(
                _statement_review(
                    statement_index=statement_index,
                    statement=statement,
                    validation_errors=statement_errors,
                )
            )
            continue
        statement_errors.extend(
            _date_binding_errors(
                excerpt,
                selected,
                authority_record=authority_record,
            )
        )
        statement_errors.extend(_number_binding_errors(excerpt, selected))
        statement_errors.extend(_comparison_direction_errors(excerpt, selected))
        statement_errors.extend(_claim_strength_errors(excerpt, selected))
        if _has_unsupported_causal_statement(
            excerpt,
            statement_class=statement_class,
            selected_authority=selected,
            authority_record=authority_record,
        ):
            statement_errors.append("unsupported_causal_statement")
        statement_errors = list(dict.fromkeys(statement_errors))
        errors.extend(statement_errors)
        statement_reviews.append(
            _statement_review(
                statement_index=statement_index,
                statement=statement,
                validation_errors=statement_errors,
            )
        )

    material_conclusions = _material_conclusion_text(narrative)
    if _unbound_material_mentions(
        material_conclusions,
        statement_bindings,
        authority_record,
    ):
        errors.append("material_statement_unbound")
    return tuple(dict.fromkeys(errors)), tuple(statement_reviews)


def _statement_review(
    *,
    statement_index: int,
    statement: Mapping[str, Any],
    validation_errors: Sequence[str],
) -> dict[str, Any]:
    errors = list(
        dict.fromkeys(str(value) for value in validation_errors if str(value))
    )
    return {
        "statement_index": statement_index,
        "status": "rejected" if errors else "bound",
        "excerpt": str(statement.get("excerpt") or ""),
        "statement_class": str(statement.get("statement_class") or ""),
        "authority_keys": list(statement.get("authority_keys") or ()),
        "validation_errors": errors,
    }


def _normalize_statement_bindings(
    value: Sequence[Mapping[str, Any]],
    *,
    narrative: str,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return (), ("statement_bindings_shape_invalid",)
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    expected_fields = {"excerpt", "statement_class", "authority_keys"}
    for item in value:
        if not isinstance(item, Mapping) or set(item) != expected_fields:
            errors.append("statement_binding_shape_invalid")
            continue
        excerpt = str(item.get("excerpt") or "")
        statement_class = str(item.get("statement_class") or "")
        raw_keys = item.get("authority_keys")
        keys = (
            [str(key) for key in raw_keys if str(key)]
            if isinstance(raw_keys, Sequence)
            and not isinstance(raw_keys, (str, bytes, bytearray))
            else []
        )
        if not excerpt.strip() or excerpt != excerpt.strip() or excerpt not in narrative:
            errors.append("statement_binding_excerpt_invalid")
        if statement_class not in _STATEMENT_CLASSES:
            errors.append("statement_binding_class_invalid")
        if not keys:
            errors.append("statement_binding_authority_missing")
        normalized.append(
            {
                "excerpt": excerpt,
                "statement_class": statement_class,
                "authority_keys": list(dict.fromkeys(keys)),
            }
        )
    return tuple(normalized), tuple(dict.fromkeys(errors))


def _normalize_statement_classes(
    statement_bindings: Sequence[Mapping[str, Any]],
    *,
    authority_record: Mapping[str, Any],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    authority_by_key = _authority_by_key(authority_record)
    normalized: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    for statement_index, raw_statement in enumerate(statement_bindings):
        statement = dict(raw_statement)
        submitted_class = str(statement.get("statement_class") or "")
        keys = tuple(str(key) for key in statement.get("authority_keys") or ())
        selected = [authority_by_key[key] for key in keys if key in authority_by_key]
        if len(selected) != len(keys):
            normalized.append(statement)
            continue
        excerpt = str(statement.get("excerpt") or "")
        normalized_class = submitted_class
        if not _statement_class_matches_authority(
            submitted_class,
            selected,
            excerpt=excerpt,
        ):
            normalized_class = _preferred_statement_class(
                excerpt,
                selected,
                fallback=submitted_class,
            )
        if normalized_class != submitted_class:
            statement["statement_class"] = normalized_class
            audits.append(
                {
                    "statement_index": statement_index,
                    "submitted_class": submitted_class,
                    "normalized_class": normalized_class,
                }
            )
        normalized.append(statement)
    return tuple(normalized), tuple(audits)


def _preferred_statement_class(
    excerpt: str,
    selected: Sequence[Mapping[str, Any]],
    *,
    fallback: str,
) -> str:
    kinds = {str(item.get("kind") or "") for item in selected}
    if any(marker in excerpt for marker in _QUESTION_MARKERS):
        return "next_check"
    if (
        "boundary" in kinds
        or _NEGATIVE_CAUSAL_BOUNDARY.search(excerpt)
        or _BOUNDARY_LANGUAGE_PATTERN.search(excerpt)
        or any(
            item.get("kind") == "factor"
            and str(item.get("state") or "")
            == "缺少独立观测，本轮按不变处理"
            for item in selected
        )
    ):
        return "data_boundary"
    if _ACCOUNTING_LANGUAGE_PATTERN.search(excerpt) and any(
        (
            item.get("kind") == "factor"
            and str(item.get("state") or "") == "已量化贡献"
        )
        or (
            item.get("kind") == "claim"
            and str(item.get("claim_type") or "")
            == "formula_component_contribution"
        )
        for item in selected
    ):
        return "factor_contribution"
    if any(
        (
            item.get("kind") == "factor"
            and str(item.get("state") or "")
            in {"已观察变化，贡献尚未量化", "已量化贡献"}
        )
        or (
            item.get("kind") == "claim"
            and str(item.get("claim_type") or "")
            in {
                "segment_contribution_or_mix_shift",
                "observed_factor_change",
                "observed_activity",
            }
        )
        for item in selected
    ):
        return "factor_observation"
    if "scope" in kinds:
        return "analysis_scope"
    if kinds.intersection({"claim", "factor", "insight"}):
        return "verified_claim"
    return fallback


def _authority_by_key(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for claim in record.get("claims") or ():
        if isinstance(claim, Mapping) and claim.get("authority_key"):
            result[str(claim["authority_key"])] = {
                "kind": "claim",
                **dict(claim),
            }
    for factor in record.get("factor_states") or ():
        if isinstance(factor, Mapping) and factor.get("factor"):
            result[str(factor["factor"])] = {
                "kind": "factor",
                **dict(factor),
            }
    for insight in record.get("diagnostic_insights") or ():
        if isinstance(insight, Mapping) and insight.get("authority_key"):
            result[str(insight["authority_key"])] = {
                "kind": "insight",
                **dict(insight),
            }
    for index, limitation in enumerate(record.get("limitations") or (), start=1):
        result[f"数据边界{index}"] = {
            "kind": "boundary",
            "statement": limitation,
        }
    result["原因边界"] = {
        "kind": "boundary",
        "statement": str(record.get("causal_boundary") or ""),
    }
    if record.get("claims") or record.get("diagnostic_insights"):
        result["问题范围"] = {
            "kind": "scope",
            "claims": list(record.get("claims") or ()),
            "accepted_assumptions": list(record.get("accepted_assumptions") or ()),
            "question_scope": dict(record.get("question_scope") or {}),
        }
    return result


def _statement_class_matches_authority(
    statement_class: str,
    selected: Sequence[Mapping[str, Any]],
    *,
    excerpt: str,
) -> bool:
    kinds = {str(item.get("kind") or "") for item in selected}
    if statement_class == "verified_claim":
        return bool(kinds.intersection({"claim", "factor", "insight"}))
    if statement_class == "factor_contribution":
        factors = [item for item in selected if item.get("kind") == "factor"]
        claims = [item for item in selected if item.get("kind") == "claim"]
        return bool(
            any(str(item.get("state") or "") == "已量化贡献" for item in factors)
            or any(
                str(item.get("claim_type") or "")
                == "formula_component_contribution"
                for item in claims
            )
        )
    if statement_class == "factor_observation":
        return bool(
            any(
                item.get("kind") == "factor"
                and str(item.get("state") or "")
                in {"已观察变化，贡献尚未量化", "已量化贡献"}
                for item in selected
            )
            or any(
                item.get("kind") == "claim"
                and str(item.get("claim_type") or "")
                in {
                    "segment_contribution_or_mix_shift",
                    "observed_factor_change",
                    "observed_activity",
                }
                for item in selected
            )
        ) and not re.search(r"主要贡献|最大贡献|驱动|导致|归因", excerpt)
    if statement_class == "data_boundary":
        return bool(
            kinds.intersection({"boundary", "factor"})
            or (
                kinds.intersection({"claim", "scope"})
                and (
                    _NEGATIVE_CAUSAL_BOUNDARY.search(excerpt)
                    or _BOUNDARY_LANGUAGE_PATTERN.search(excerpt)
                )
            )
        )
    if statement_class == "analysis_scope":
        return bool(kinds.intersection({"scope", "claim"}))
    if statement_class == "next_check":
        return any(marker in excerpt for marker in _QUESTION_MARKERS)
    return False


def _date_binding_errors(
    excerpt: str,
    selected: Sequence[Mapping[str, Any]],
    *,
    authority_record: Mapping[str, Any],
) -> tuple[str, ...]:
    question_scope = authority_record.get("question_scope") or {}
    scoped_dates = _canonical_dates(question_scope)
    allowed = scoped_dates or _canonical_dates(selected)
    role_dates = _question_scope_role_dates(authority_record)
    errors: list[str] = []
    for match in _FULL_DATE_PATTERN.finditer(excerpt):
        parsed = _canonical_date(match.group(0))
        if parsed and parsed not in allowed:
            errors.append("unsupported_narrative_date")
            continue
        explicit_role = _explicit_date_role(excerpt, match.start(), match.end())
        if (
            parsed
            and explicit_role
            and role_dates.get(explicit_role)
            and parsed not in role_dates[explicit_role]
        ):
            errors.append("authority_date_role_mismatch")
    for match in _PARTIAL_DATE_PATTERN.finditer(excerpt):
        if _FULL_DATE_PATTERN.fullmatch(match.group(0)):
            continue
        month_day = (int(match.group("month")), int(match.group("day")))
        matching = {
            value for value in allowed if _date_month_day(value) == month_day
        }
        if len(matching) != 1:
            errors.append("unsupported_narrative_date")
            continue
        parsed = next(iter(matching))
        explicit_role = _explicit_date_role(excerpt, match.start(), match.end())
        if (
            explicit_role
            and role_dates.get(explicit_role)
            and parsed not in role_dates[explicit_role]
        ):
            errors.append("authority_date_role_mismatch")
    return tuple(dict.fromkeys(errors))


def _question_scope_role_dates(
    authority_record: Mapping[str, Any],
) -> dict[str, set[str]]:
    question_scope = authority_record.get("question_scope") or {}
    result: dict[str, set[str]] = {"target": set(), "baseline": set()}
    if isinstance(question_scope, Mapping):
        for role in result:
            window = question_scope.get(role) or {}
            if not isinstance(window, Mapping):
                continue
            role_value = {
                key: window.get(key)
                for key in ("label", "start_inclusive")
                if window.get(key) not in (None, "")
            }
            result[role].update(_canonical_dates(role_value))
    for claim in authority_record.get("claims") or ():
        if not isinstance(claim, Mapping):
            continue
        for role in result:
            if result[role]:
                continue
            value = claim.get(role) or {}
            if isinstance(value, Mapping):
                result[role].update(_canonical_dates(value))
    return result


def _explicit_date_role(text: str, start: int, end: int) -> str:
    clause_start = max(
        text.rfind(mark, 0, start)
        for mark in ("。", "；", ";", "，", ",", "\n")
    ) + 1
    clause_end_candidates = [
        position
        for mark in ("。", "；", ";", "，", ",", "\n")
        if (position := text.find(mark, end)) >= 0
    ]
    clause_end = min(clause_end_candidates) if clause_end_candidates else len(text)
    clause = text[clause_start:clause_end]
    local_start = start - clause_start
    local_end = end - clause_start
    candidates: list[tuple[int, str]] = []
    for role, markers in (
        ("target", _TARGET_ROLE_MARKERS),
        ("baseline", _BASELINE_ROLE_MARKERS),
    ):
        for marker in markers:
            cursor = 0
            while (position := clause.find(marker, cursor)) >= 0:
                marker_end = position + len(marker)
                if marker_end <= local_start:
                    distance = local_start - marker_end
                elif position >= local_end:
                    distance = position - local_end
                else:
                    distance = 0
                if distance <= 12:
                    candidates.append((distance, role))
                cursor = marker_end
    comparison_prefix = clause[max(0, local_start - 4) : local_start]
    if re.search(r"(?:相比|相较|较)\s*$", comparison_prefix):
        candidates.append((0, "baseline"))
    if not candidates:
        return ""
    nearest_distance = min(distance for distance, _ in candidates)
    nearest_roles = {
        role for distance, role in candidates if distance == nearest_distance
    }
    return next(iter(nearest_roles)) if len(nearest_roles) == 1 else ""


def _number_binding_errors(
    excerpt: str,
    selected: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    facts = _numeric_facts(selected)
    masked = _mask_dates(excerpt)
    errors: list[str] = []
    for match in _NUMBER_PATTERN.finditer(masked):
        if _number_match_is_list_ordinal(excerpt, match):
            continue
        mention = _numeric_mention(match.group(0), start=match.start())
        if mention is None:
            continue
        value_matches = [fact for fact in facts if _mention_matches_fact(mention, fact)]
        if not value_matches:
            errors.append("unsupported_narrative_number")
            continue
        if not any(
            _fact_role_matches_context(excerpt, mention, fact)
            for fact in value_matches
        ):
            errors.append("authority_role_mismatch")
    return tuple(dict.fromkeys(errors))


def _number_match_is_list_ordinal(excerpt: str, match: re.Match[str]) -> bool:
    raw = match.group(0).strip()
    if not re.fullmatch(r"\d+", raw):
        return False
    suffix = excerpt[match.end() : match.end() + 1]
    if suffix not in {".", "、", ")", "）"}:
        return False
    prefix = excerpt[: match.start()]
    item_prefix = re.split(r"[。；;!?？\n：:]", prefix)[-1]
    return not item_prefix.strip()


def _comparison_direction_errors(
    excerpt: str,
    selected: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    expected = {
        str(item.get("comparison_direction") or "").lower()
        for item in selected
        if (
            (
                item.get("kind") == "claim"
                and str(item.get("claim_type") or "") == "comparative_change"
            )
            or item.get("kind") == "insight"
        )
        and str(item.get("comparison_direction") or "")
    }
    if not expected:
        return ()
    text = str(excerpt or "")
    negated_positive = bool(
        re.search(r"(?:没有|并未|未曾|并无|不再)(?:上涨|上升|增加|增长|提升)", text)
    )
    negated_negative = bool(
        re.search(r"(?:没有|并未|未曾|并无|不再)(?:下跌|下降|减少|回落|降低)", text)
    )
    positive_text = re.sub(
        r"(?:没有|并未|未曾|并无|不再)(?:下跌|下降|减少|回落|降低)",
        "",
        text,
    )
    negative_text = re.sub(
        r"(?:没有|并未|未曾|并无|不再)(?:上涨|上升|增加|增长|提升)",
        "",
        text,
    )
    mentions_positive = bool(_POSITIVE_DIRECTION_PATTERN.search(positive_text))
    mentions_negative = bool(_NEGATIVE_DIRECTION_PATTERN.search(negative_text))
    expects_positive = bool(expected.intersection({"positive", "increase", "up"}))
    expects_negative = bool(expected.intersection({"negative", "decrease", "down"}))
    if expects_positive and (mentions_negative or negated_positive):
        return ("comparison_direction_mismatch",)
    if expects_negative and (mentions_positive or negated_negative):
        return ("comparison_direction_mismatch",)
    return ()


def _claim_strength_errors(
    excerpt: str,
    selected: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    for marker, requested_rank in _STRENGTH_MARKER_RANKS:
        cursor = 0
        while (position := excerpt.find(marker, cursor)) >= 0:
            clause = _strength_clause(excerpt, position)
            owners = [
                item
                for item in selected
                if _authority_owns_strength_clause(item, clause)
            ]
            candidates = owners or [
                item
                for item in selected
                if item.get("kind") in {"claim", "factor", "insight"}
            ]
            authority_ranks = [
                _AUTHORITY_STRENGTH_RANKS.get(
                    str(
                        item.get("claim_strength")
                        or item.get("claimStrength")
                        or ""
                    ).lower(),
                    0,
                )
                for item in candidates
                if item.get("claim_strength") or item.get("claimStrength")
            ]
            if authority_ranks and requested_rank > min(authority_ranks):
                return ("claim_strength_escalation",)
            cursor = position + len(marker)
    return ()


def _strength_clause(text: str, position: int) -> str:
    start = max(
        text.rfind(mark, 0, position)
        for mark in ("。", "；", ";", "!", "?", "？", "\n")
    ) + 1
    end_candidates = [
        found
        for mark in ("。", "；", ";", "!", "?", "？", "\n")
        if (found := text.find(mark, position)) >= 0
    ]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end]


def _authority_owns_strength_clause(
    authority: Mapping[str, Any],
    clause: str,
) -> bool:
    kind = str(authority.get("kind") or "")
    if kind == "factor":
        factor = str(authority.get("factor") or "")
        return bool(factor and factor in clause)
    if kind == "insight":
        labels = [
            str(value)
            for value in authority.get("business_labels") or ()
            if str(value)
        ]
        if any(label in clause for label in labels):
            return True
        masked = _mask_dates(clause)
        return any(
            (mention := _numeric_mention(match.group(0), start=match.start()))
            is not None
            and any(
                _mention_matches_fact(mention, fact)
                for fact in _numeric_facts((authority,))
            )
            for match in _NUMBER_PATTERN.finditer(masked)
        )
    if kind != "claim":
        return False
    dimensions = authority.get("dimensions") or {}
    if isinstance(dimensions, Mapping) and any(
        str(value) and str(value) in clause for value in dimensions.values()
    ):
        return True
    numbers = authority.get("numbers") or {}
    if isinstance(numbers, Mapping):
        factor_labels = {
            label
            for field in numbers
            if (label := _factor_label_for_field(str(field)))
        }
        if any(label in clause for label in factor_labels):
            return True
    masked = _mask_dates(clause)
    facts = _numeric_facts((authority,))
    for match in _NUMBER_PATTERN.finditer(masked):
        mention = _numeric_mention(match.group(0), start=match.start())
        if mention is not None and any(
            _mention_matches_fact(mention, fact) for fact in facts
        ):
            return True
    return False


def _numeric_facts(selected: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for item in selected:
        kind = str(item.get("kind") or "")
        if kind == "scope":
            facts.extend(_numeric_facts(item.get("claims") or ()))
            continue
        if kind == "claim":
            numbers = item.get("numbers") or {}
            if not isinstance(numbers, Mapping):
                continue
            target = item.get("target") or {}
            baseline = item.get("baseline") or {}
            dimensions = item.get("dimensions") or {}
            dimension_labels = [
                str(value) for value in dimensions.values() if str(value)
            ] if isinstance(dimensions, Mapping) else []
            for field, value in numbers.items():
                factor = _factor_label_for_field(str(field))
                facts.append(
                    {
                        "value": value,
                        "field": str(field),
                        "role": _numeric_role(str(field)),
                        "ratio": _field_is_ratio(str(field)),
                        "factor": factor,
                        "target_label": str(target.get("label") or "")
                        if isinstance(target, Mapping)
                        else "",
                        "baseline_label": str(baseline.get("label") or "")
                        if isinstance(baseline, Mapping)
                        else "",
                        "dimension_labels": dimension_labels,
                    }
                )
            continue
        if kind == "insight":
            numbers = item.get("numbers") or {}
            if not isinstance(numbers, Mapping):
                continue
            business_labels = [
                str(value)
                for value in item.get("business_labels") or ()
                if str(value)
            ]
            for field, value in numbers.items():
                facts.append(
                    {
                        "value": value,
                        "field": str(field),
                        "role": _numeric_role(str(field)),
                        "ratio": _field_is_ratio(str(field)),
                        "factor": "",
                        "target_label": "",
                        "baseline_label": "",
                        "dimension_labels": business_labels,
                        "signed_magnitude_allowed": bool(
                            str(item.get("comparison_direction") or "")
                            and any(
                                marker in str(field).lower()
                                for marker in ("change", "contribution")
                            )
                        ),
                    }
                )
            continue
        if kind == "factor":
            factor = str(item.get("factor") or "")
            field_map = {
                "baseline": "baseline_value",
                "target": "target_value",
                "change": "absolute_change",
                "changeRate": "relative_change",
                "contribution": "contribution",
                "contributionShare": "contribution_share",
            }
            for source_field, fact_field in field_map.items():
                if item.get(source_field) is None:
                    continue
                facts.append(
                    {
                        "value": item[source_field],
                        "field": fact_field,
                        "role": _numeric_role(fact_field),
                        "ratio": _field_is_ratio(fact_field),
                        "factor": factor,
                        "target_label": "",
                        "baseline_label": "",
                        "dimension_labels": [],
                    }
                )
    return facts


def _numeric_mention(raw: str, *, start: int) -> dict[str, Any] | None:
    text = re.sub(r"[ \t]", "", str(raw or "").strip())
    text = (
        text.replace("−", "-")
        .replace("－", "-")
        .replace("＋", "+")
    )
    suffix = ""
    for candidate in ("%", "万", "亿", "k", "K", "m", "M"):
        if text.endswith(candidate):
            suffix = candidate
            text = text[: -len(candidate)]
            break
    try:
        number = Decimal(text.replace(",", "").replace("+", ""))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    multiplier = {
        "万": Decimal(10_000),
        "亿": Decimal(100_000_000),
        "k": Decimal(1_000),
        "K": Decimal(1_000),
        "m": Decimal(1_000_000),
        "M": Decimal(1_000_000),
    }.get(suffix, Decimal(1))
    decimals = max(0, -number.as_tuple().exponent)
    display_value = number * multiplier
    resolution = multiplier * (Decimal(10) ** (-decimals))
    return {
        "raw": raw,
        "value": display_value,
        "percent": suffix == "%",
        "resolution": resolution,
        "start": start,
        "end": start + len(raw),
    }


def _mention_matches_fact(mention: Mapping[str, Any], fact: Mapping[str, Any]) -> bool:
    expected = _decimal(fact.get("value"))
    if expected is None:
        return False
    if mention.get("percent"):
        candidates = [expected * Decimal(100)] if fact.get("ratio") else [expected]
    else:
        candidates = [expected]
    if fact.get("signed_magnitude_allowed") and expected < 0:
        candidates.append(abs(expected))
    actual = _decimal(mention.get("value"))
    resolution = _decimal(mention.get("resolution")) or Decimal(0)
    if actual is None:
        return False
    tolerance = abs(resolution) / Decimal(2)
    tolerance = max(tolerance, Decimal("0.000000001"))
    return any(abs(actual - candidate) <= tolerance for candidate in candidates)


def _fact_role_matches_context(
    excerpt: str,
    mention: Mapping[str, Any],
    fact: Mapping[str, Any],
) -> bool:
    start = int(mention.get("start") or 0)
    sentence_start = max(
        excerpt.rfind(mark, 0, start) for mark in ("。", "；", ";", "\n")
    ) + 1
    sentence_end_candidates = [
        position
        for mark in ("。", "；", ";", "\n")
        if (position := excerpt.find(mark, start)) >= 0
    ]
    sentence_end = (
        min(sentence_end_candidates) if sentence_end_candidates else len(excerpt)
    )
    sentence = excerpt[sentence_start:sentence_end]
    clause_start = max(
        excerpt.rfind(mark, 0, start) for mark in ("。", "；", ";", "\n", "，", ",")
    ) + 1
    clause_end_candidates = [
        position
        for mark in ("。", "；", ";", "\n")
        if (position := excerpt.find(mark, start)) >= 0
    ]
    clause_end = min(clause_end_candidates) if clause_end_candidates else len(excerpt)
    clause = excerpt[clause_start:clause_end]
    local_start = start - clause_start
    role = str(fact.get("role") or "")
    factor = str(fact.get("factor") or "")
    if factor and factor not in sentence:
        return False
    dimension_labels = [str(value) for value in fact.get("dimension_labels") or ()]
    if dimension_labels and not any(label in sentence for label in dimension_labels):
        return False
    if role in {"target", "baseline"}:
        target_markers = tuple(
            marker
            for marker in (
                str(fact.get("target_label") or ""),
                "目标期",
                "目标日",
                "目标窗口",
            )
            if marker
        )
        baseline_markers = tuple(
            marker
            for marker in (
                str(fact.get("baseline_label") or ""),
                "基线期",
                "基准日",
                "基线日",
                "前一天",
            )
            if marker
        )
        nearest = _nearest_role_before(
            clause,
            local_start,
            target_markers=target_markers,
            baseline_markers=baseline_markers,
        )
        if nearest:
            return nearest == role
    if role in {"contribution", "contribution_share"}:
        return bool(_ACCOUNTING_LANGUAGE_PATTERN.search(clause))
    if role == "absolute_change":
        return bool(
            re.search(
                r"增加|减少|变化|变动|上涨|下跌|提升|下降|增长|回落|降低",
                clause,
            )
        )
    if role == "relative_change":
        return bool(mention.get("percent")) or bool(
            re.search(r"变化率|涨幅|跌幅|占比", clause)
        )
    return True


def _nearest_role_before(
    clause: str,
    position: int,
    *,
    target_markers: Sequence[str],
    baseline_markers: Sequence[str],
) -> str:
    candidates: list[tuple[int, str]] = []
    for marker in target_markers:
        found = clause.rfind(marker, 0, position)
        if found >= 0:
            candidates.append((found, "target"))
    for marker in baseline_markers:
        found = clause.rfind(marker, 0, position)
        if found >= 0:
            candidates.append((found, "baseline"))
    return max(candidates)[1] if candidates else ""


def _has_unsupported_causal_statement(
    excerpt: str,
    *,
    statement_class: str,
    selected_authority: Sequence[Mapping[str, Any]],
    authority_record: Mapping[str, Any],
) -> bool:
    selected_has_causal_claim = any(
        item.get("kind") in {"claim", "insight"}
        and item.get("causal_authority") is True
        for item in selected_authority
    )
    for clause in _causal_clauses(excerpt):
        has_mechanism_language = bool(_MECHANISM_CAUSAL_PATTERN.search(clause))
        has_driver_language = "驱动" in clause or bool(
            re.search(r"主要由.{0,20}(?:贡献|提升|下降|变化)", clause)
        )
        if not has_mechanism_language and not has_driver_language:
            continue
        if any(marker in clause for marker in _QUESTION_MARKERS):
            continue
        if _NEGATIVE_CAUSAL_BOUNDARY.search(clause):
            continue
        if selected_has_causal_claim:
            continue
        if _accounting_statement_authorized(
            clause,
            statement_class=statement_class,
            selected_authority=selected_authority,
            authority_record=authority_record,
        ):
            continue
        return True
    return False


def _causal_clauses(text: str) -> tuple[str, ...]:
    return tuple(
        clause.strip()
        for clause in re.split(
            r"[。；;!?？\n]+|(?<!\d)\.(?!\d)|(?:但是|但|不过|然而)",
            text,
        )
        if clause.strip()
    )


def _accounting_statement_authorized(
    clause: str,
    *,
    statement_class: str,
    selected_authority: Sequence[Mapping[str, Any]],
    authority_record: Mapping[str, Any],
) -> bool:
    if statement_class not in {"verified_claim", "factor_contribution"}:
        return False
    has_formula_claim = any(
        item.get("kind") == "claim"
        and str(item.get("claim_type") or "")
        == "formula_component_contribution"
        for item in selected_authority
    )
    selected_factors = {
        str(item.get("factor") or "")
        for item in selected_authority
        if item.get("kind") == "factor"
        and str(item.get("state") or "") == "已量化贡献"
    }
    all_factors = {
        str(item.get("factor") or ""): item
        for item in authority_record.get("factor_states") or ()
        if isinstance(item, Mapping)
        and str(item.get("state") or "") == "已量化贡献"
    }
    mentioned = {factor for factor in all_factors if factor and factor in clause}
    if not mentioned:
        return False
    if not has_formula_claim and not mentioned.intersection(selected_factors):
        return False
    if re.search(r"主要|最大|最高|首要", clause):
        ranked = [
            (
                _decimal(item.get("contributionShare"))
                if item.get("contributionShare") is not None
                else _decimal(item.get("contribution"))
            , factor)
            for factor, item in all_factors.items()
        ]
        ranked = [(value, factor) for value, factor in ranked if value is not None]
        if ranked:
            largest_factor = max(ranked, key=lambda pair: pair[0])[1]
            if largest_factor not in mentioned:
                return False
    for factor in mentioned:
        contribution = _decimal(all_factors[factor].get("contribution"))
        if contribution is None:
            continue
        factor_fragments = [
            fragment
            for fragment in re.split(r"[，,；;。]", clause)
            if factor in fragment
        ]
        factor_context = "，".join(factor_fragments) or clause
        if re.search(r"负向|抵消|下降|减少|降低|回落", factor_context) and contribution >= 0:
            return False
        if re.search(r"正向|提升|增加|上升|上涨|增长", factor_context) and contribution <= 0:
            return False
    return True


def _unbound_material_mentions(
    narrative: str,
    statement_bindings: Sequence[Mapping[str, Any]],
    authority_record: Mapping[str, Any],
) -> bool:
    coverage: list[tuple[int, int]] = []
    for statement in statement_bindings:
        excerpt = str(statement.get("excerpt") or "")
        if not excerpt:
            continue
        cursor = 0
        while (start := narrative.find(excerpt, cursor)) >= 0:
            coverage.append((start, start + len(excerpt)))
            cursor = start + max(1, len(excerpt))

    for start, end in _material_mention_spans(narrative, authority_record):
        if not any(bound_start <= start and end <= bound_end for bound_start, bound_end in coverage):
            return True
    return False


def _material_mention_spans(
    narrative: str,
    authority_record: Mapping[str, Any],
) -> tuple[tuple[int, int], ...]:
    spans: set[tuple[int, int]] = set()
    for pattern in (
        _FULL_DATE_PATTERN,
        _PARTIAL_DATE_PATTERN,
        _MECHANISM_CAUSAL_PATTERN,
        _ACCOUNTING_LANGUAGE_PATTERN,
        _DIRECTION_LANGUAGE_PATTERN,
        _STRENGTH_LANGUAGE_PATTERN,
        re.compile(r"全样本"),
    ):
        spans.update(
            (match.start(), match.end())
            for match in pattern.finditer(narrative)
            if not _span_is_editorial_heading(
                narrative,
                match.start(),
                match.end(),
            )
        )

    masked = _mask_dates(narrative)
    for match in _NUMBER_PATTERN.finditer(masked):
        if not _number_match_is_list_ordinal(narrative, match):
            spans.add((match.start(), match.end()))

    labels = {
        str(item.get("factor") or "")
        for item in authority_record.get("factor_states") or ()
        if isinstance(item, Mapping) and str(item.get("factor") or "")
    }
    for claim in authority_record.get("claims") or ():
        if not isinstance(claim, Mapping):
            continue
        dimensions = claim.get("dimensions") or {}
        if isinstance(dimensions, Mapping):
            labels.update(str(value) for value in dimensions.values() if str(value))
    for insight in authority_record.get("diagnostic_insights") or ():
        if isinstance(insight, Mapping):
            labels.update(
                str(value)
                for value in insight.get("business_labels") or ()
                if str(value)
            )
    for label in labels:
        cursor = 0
        while (start := narrative.find(label, cursor)) >= 0:
            spans.add((start, start + len(label)))
            cursor = start + len(label)
    return tuple(sorted(spans))


def _span_is_editorial_heading(
    narrative: str,
    start: int,
    end: int,
) -> bool:
    line_start = narrative.rfind("\n", 0, start) + 1
    line_end = narrative.find("\n", end)
    if line_end < 0:
        line_end = len(narrative)
    line = narrative[line_start:line_end].strip()
    if not line:
        return False
    markdown_heading = bool(re.fullmatch(r"#{1,6}\s+.{1,60}", line))
    emphasized_heading = bool(
        re.fullmatch(r"(?:\*\*|__)[^\n]{1,60}(?:\*\*|__)", line)
    )
    if not (markdown_heading or emphasized_heading):
        return False
    return not (
        _FULL_DATE_PATTERN.search(line)
        or _PARTIAL_DATE_PATTERN.search(line)
        or _NUMBER_PATTERN.search(line)
    )


def _material_conclusion_text(narrative: str) -> str:
    key_start = narrative.find("关键发现：")
    if key_start < 0:
        key_start = narrative.find("关键发现")
    final_start = narrative.find("最终结论：")
    if final_start < 0:
        final_start = narrative.find("最终结论")
    starts = [value for value in (key_start, final_start) if value >= 0]
    if not starts:
        return narrative
    start = min(starts)
    attention_start = narrative.find("需要注意：", start)
    if attention_start < 0:
        attention_start = narrative.find("需要注意", start)
    end = attention_start if attention_start >= 0 else len(narrative)
    return narrative[start:end]


def _factor_states_from_evidence(
    evidence: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    factors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence:
        payload = item.get("typed_payload") or {}
        if not isinstance(payload, Mapping):
            continue
        for decomposition in payload.get("decompositions") or ():
            if not isinstance(decomposition, Mapping):
                continue
            contributions = {
                str(value.get("component_id") or ""): value
                for value in decomposition.get("core_factor_contributions") or ()
                if isinstance(value, Mapping)
            }
            for change in decomposition.get("component_changes") or ():
                if not isinstance(change, Mapping):
                    continue
                factor = str(change.get("business_name") or "").strip()
                if not factor:
                    factor = _FACTOR_FIELD_LABELS.get(
                        str(change.get("component_id") or ""),
                        "",
                    )
                if not factor:
                    continue
                contribution = contributions.get(
                    str(change.get("component_id") or "")
                )
                if change.get("observed") is False:
                    state = "缺少独立观测，本轮按不变处理"
                elif contribution is not None:
                    state = "已量化贡献"
                else:
                    state = "已观察变化，贡献尚未量化"
                factor_state: dict[str, Any] = {
                    "factor": factor,
                    "state": state,
                    "source_evidence_ref": str(item.get("evidence_ref") or ""),
                    "source_binding_digest": str(
                        item.get("binding_manifest_digest") or ""
                    ),
                }
                if change.get("observed") is not False:
                    factor_state.update(
                        {
                            "baseline": change.get("baseline_value"),
                            "target": change.get("target_value"),
                            "change": change.get("delta"),
                            "changeRate": change.get("delta_ratio"),
                        }
                    )
                if contribution is not None and change.get("observed") is not False:
                    factor_state.update(
                        {
                            "contribution": contribution.get("contribution"),
                            "contributionShare": contribution.get(
                                "contribution_share"
                            ),
                        }
                    )
                identity = canonical_digest(factor_state)
                if identity not in seen:
                    seen.add(identity)
                    factors.append(canonical_value(factor_state))
    return factors


def _merge_claim_factor_states(
    claims: Sequence[Mapping[str, Any]],
    evidence_factors: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_factor = {
        str(item.get("factor") or ""): dict(item)
        for item in evidence_factors
        if str(item.get("factor") or "")
    }
    for claim in claims:
        if str(claim.get("claim_type") or "") != "formula_component_contribution":
            continue
        numbers = claim.get("numbers") or {}
        if not isinstance(numbers, Mapping):
            continue
        for field, value in numbers.items():
            factor = _factor_label_for_field(str(field))
            if not factor:
                continue
            state = by_factor.setdefault(
                factor,
                {"factor": factor, "state": "已量化贡献"},
            )
            if claim.get("claim_strength"):
                state["claimStrength"] = str(claim.get("claim_strength") or "")
            field_name = str(field)
            if "contribution_share" in field_name:
                state["contributionShare"] = value
            elif "contribution" in field_name:
                state["contribution"] = value
    return sorted(
        (canonical_value(value) for value in by_factor.values()),
        key=lambda item: (str(item.get("factor") or ""), canonical_digest(item)),
    )


def _factor_label_for_field(field: str) -> str:
    normalized = str(field or "").lower()
    for prefix, label in _FACTOR_FIELD_LABELS.items():
        if normalized.startswith(prefix):
            return label
    return ""


def _numeric_role(field: str) -> str:
    value = str(field or "").replace("-", "_").lower()
    if value in {"target", "target_value"} or value.endswith("_target_value"):
        return "target"
    if value in {"baseline", "baseline_value"} or value.endswith("_baseline_value"):
        return "baseline"
    if "contribution_share" in value:
        return "contribution_share"
    if "contribution" in value:
        return "contribution"
    if value in {"absolute_change", "delta", "change"} or value.endswith("_delta"):
        return "absolute_change"
    if _field_is_ratio(value):
        return "relative_change"
    return "value"


def _canonical_dates(value: Any) -> set[str]:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return {
        parsed
        for match in _FULL_DATE_PATTERN.finditer(text)
        if (parsed := _canonical_date(match.group(0)))
    }


def _canonical_date(value: str) -> str:
    raw = str(value or "")
    if "年" in raw:
        match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", raw)
    else:
        match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _date_month_day(value: str) -> tuple[int, int]:
    _, month, day = value.split("-")
    return int(month), int(day)


def _mask_dates(text: str) -> str:
    masked = list(text)
    for pattern in (_FULL_DATE_PATTERN, _PARTIAL_DATE_PATTERN):
        for match in pattern.finditer(text):
            masked[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(masked)


def _field_is_ratio(field: str) -> bool:
    value = str(field or "").replace("-", "_").lower()
    return any(marker in value for marker in _RATIO_FIELD_MARKERS)


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _narrative_digest(narrative: str) -> str:
    return canonical_digest({"final_business_summary": narrative})


def _publication_review_projection(
    quality_gate: Mapping[str, Any],
) -> dict[str, Any]:
    return canonical_value(
        {
            key: quality_gate.get(key)
            for key in (
                "status",
                "display_status",
                "blocks_display",
                "hard_blockers",
                "repairable_warnings",
                "risk_flags",
            )
            if key in quality_gate
        }
    )
