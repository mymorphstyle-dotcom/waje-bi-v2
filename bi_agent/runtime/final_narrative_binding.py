from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


_BINDING_SCHEMA_VERSION = "final-narrative-publication-binding.v3"
_AUTHORITY_SCHEMA_VERSION = "narrative-authority-record.v2"
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
    r"(?<![A-Za-z0-9_])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.\d+)?\s*(?:%|万|亿|[kKmM])?"
)
_RATIO_FIELD_MARKERS = (
    "ratio",
    "rate",
    "share",
    "percent",
    "relative",
    "change_rate",
    "changerate",
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
        "missing_required_summary_markers",
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
            "limitations": limitations,
            "accepted_assumptions": assumptions,
            "question_scope": canonical_value(question_scope or {}),
            "causal_boundary": (
                "已对账的组成贡献可以说明主要贡献项和抵消关系；"
                "业务机制与更深层原因需要独立证据。"
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
    errors = list(shape_errors)
    errors.extend(
        _narrative_publication_errors(
            narrative=text,
            statement_bindings=normalized_statements,
            authority_record=record,
            quality_gate=quality_gate,
        )
    )
    errors = list(dict.fromkeys(errors))
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
        "validation_errors": errors,
    }
    return binding, tuple(errors)


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
) -> tuple[str, ...]:
    errors: list[str] = []
    if not narrative.strip():
        return ("narrative_missing",)
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
        return tuple(dict.fromkeys(errors))

    authority_by_key = _authority_by_key(authority_record)
    for statement in statement_bindings:
        excerpt = str(statement.get("excerpt") or "")
        statement_class = str(statement.get("statement_class") or "")
        keys = tuple(str(key) for key in statement.get("authority_keys") or ())
        selected = [authority_by_key[key] for key in keys if key in authority_by_key]
        if len(selected) != len(keys):
            errors.append("statement_authority_key_unknown")
            continue
        if not _statement_class_matches_authority(
            statement_class,
            selected,
            excerpt=excerpt,
        ):
            errors.append("statement_class_authority_mismatch")
        errors.extend(_date_binding_errors(excerpt, selected))
        errors.extend(_number_binding_errors(excerpt, selected))
        errors.extend(_comparison_direction_errors(excerpt, selected))
        errors.extend(_claim_strength_errors(excerpt, selected))
        if _has_unsupported_causal_statement(
            excerpt,
            statement_class=statement_class,
            selected_authority=selected,
            authority_record=authority_record,
        ):
            errors.append("unsupported_causal_statement")

    material_conclusions = _material_conclusion_text(narrative)
    if _unbound_material_mentions(
        material_conclusions,
        statement_bindings,
        authority_record,
    ):
        errors.append("material_statement_unbound")
    return tuple(dict.fromkeys(errors))


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
    for index, limitation in enumerate(record.get("limitations") or (), start=1):
        result[f"数据边界{index}"] = {
            "kind": "boundary",
            "statement": limitation,
        }
    result["原因边界"] = {
        "kind": "boundary",
        "statement": str(record.get("causal_boundary") or ""),
    }
    if record.get("claims"):
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
        return bool(kinds.intersection({"claim", "factor"}))
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
        return any(
            item.get("kind") == "factor"
            and str(item.get("state") or "")
            in {"已观察变化，贡献尚未量化", "已量化贡献"}
            for item in selected
        ) and not re.search(r"主要贡献|最大贡献|驱动|导致|归因", excerpt)
    if statement_class == "data_boundary":
        return bool(kinds.intersection({"boundary", "factor"}))
    if statement_class == "analysis_scope":
        return bool(kinds.intersection({"scope", "claim"}))
    if statement_class == "next_check":
        return any(marker in excerpt for marker in _QUESTION_MARKERS)
    return False


def _date_binding_errors(
    excerpt: str,
    selected: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    allowed = _canonical_dates(selected)
    errors: list[str] = []
    for match in _FULL_DATE_PATTERN.finditer(excerpt):
        parsed = _canonical_date(match.group(0))
        if parsed and parsed not in allowed:
            errors.append("unsupported_narrative_date")
    for match in _PARTIAL_DATE_PATTERN.finditer(excerpt):
        if _FULL_DATE_PATTERN.fullmatch(match.group(0)):
            continue
        month_day = (int(match.group("month")), int(match.group("day")))
        matching = {
            value for value in allowed if _date_month_day(value) == month_day
        }
        if len(matching) != 1:
            errors.append("unsupported_narrative_date")
    return tuple(dict.fromkeys(errors))


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
        if item.get("kind") == "claim"
        and str(item.get("claim_type") or "") == "comparative_change"
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
    requested_ranks = [
        rank
        for marker, rank in (
            ("高强度证据", 3),
            ("中等强度证据", 2),
            ("低强度证据", 1),
            ("高置信度", 4),
            ("确定可靠", 4),
            ("确定性结论", 4),
            ("已证实", 4),
        )
        if marker in excerpt
    ]
    if not requested_ranks:
        return ()
    authority_ranks = [
        {
            "causal": 4,
            "high": 3,
            "medium": 2,
            "observed": 1,
            "low": 1,
            "degraded": 0,
            "insufficient": 0,
            "blocked": 0,
        }.get(
            str(
                item.get("claim_strength")
                or item.get("claimStrength")
                or ""
            ).lower(),
            0,
        )
        for item in selected
        if item.get("kind") in {"claim", "factor"}
        and (item.get("claim_strength") or item.get("claimStrength"))
    ]
    if authority_ranks and max(requested_ranks) > min(authority_ranks):
        return ("claim_strength_escalation",)
    return ()


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
    text = str(raw or "").strip().replace(" ", "")
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
        return bool(re.search(r"增加|减少|变化|变动|上涨|下跌|提升|下降", clause))
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
        item.get("kind") == "claim" and item.get("causal_authority") is True
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
        for clause in re.split(r"[。；;.!?？\n]+|(?:但是|但|不过|然而)", text)
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
        spans.update((match.start(), match.end()) for match in pattern.finditer(narrative))

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
    for label in labels:
        cursor = 0
        while (start := narrative.find(label, cursor)) >= 0:
            spans.add((start, start + len(label)))
            cursor = start + len(label)
    return tuple(sorted(spans))


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
