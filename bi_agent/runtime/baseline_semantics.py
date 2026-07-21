from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any


CANONICAL_BASELINE_IDS = (
    "previous_day",
    "rolling_7_day_baseline",
    "same_weekday_last_week",
)

_BASELINE_BUSINESS_SEMANTICS = {
    "previous_day": {
        "label": "前一天",
        "semantics": "目标日之前的一个完整自然日",
    },
    "rolling_7_day_baseline": {
        "label": "近7日均值",
        "semantics": "目标日之前连续7个完整自然日的日均值",
    },
    "same_weekday_last_week": {
        "label": "上周同日",
        "semantics": "目标日向前7天对应的完整自然日",
    },
}

_BASELINE_AGGREGATIONS = {
    "previous_day": "sum_of_complete_days",
    "rolling_7_day_baseline": "mean_of_complete_days",
    "same_weekday_last_week": "sum_of_complete_days",
}


class BaselineSemanticError(ValueError):
    pass


def baseline_llm_semantics() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "id": baseline_id,
            **_BASELINE_BUSINESS_SEMANTICS[baseline_id],
        }
        for baseline_id in CANONICAL_BASELINE_IDS
    )


def canonical_baseline_aggregation(baseline_id: str) -> str:
    if baseline_id not in CANONICAL_BASELINE_IDS:
        raise BaselineSemanticError("baseline_semantics_unknown")
    return _BASELINE_AGGREGATIONS[baseline_id]


def canonical_baseline_bounds(
    target: str,
    baseline_id: str,
) -> tuple[str, str]:
    if baseline_id not in CANONICAL_BASELINE_IDS:
        raise BaselineSemanticError("baseline_semantics_unknown")
    try:
        target_date = date.fromisoformat(target)
    except (TypeError, ValueError) as exc:
        raise BaselineSemanticError("baseline_target_date_invalid") from exc
    if target_date.isoformat() != target:
        raise BaselineSemanticError("baseline_target_date_invalid")
    if baseline_id == "previous_day":
        start = end = target_date - timedelta(days=1)
    elif baseline_id == "rolling_7_day_baseline":
        start = target_date - timedelta(days=7)
        end = target_date - timedelta(days=1)
    else:
        start = end = target_date - timedelta(days=7)
    return start.isoformat(), end.isoformat()


def canonical_baseline_ids(value: Any) -> tuple[str, ...]:
    if value in (None, "", (), []):
        return ()
    items = (
        tuple(value)
        if isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        else (value,)
    )
    found: set[str] = set()
    for item in items:
        found.update(_candidate_ids(item))
    return tuple(
        baseline_id for baseline_id in CANONICAL_BASELINE_IDS if baseline_id in found
    )


def _candidate_ids(value: Any) -> set[str]:
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        found: set[str] = set()
        for item in value:
            found.update(_candidate_ids(item))
        if not found:
            raise BaselineSemanticError("baseline_semantics_unknown")
        return found
    if isinstance(value, str):
        baseline_id = value.strip()
        if baseline_id not in CANONICAL_BASELINE_IDS:
            raise BaselineSemanticError("baseline_semantics_unknown")
        return {baseline_id}
    if not isinstance(value, Mapping):
        raise BaselineSemanticError("baseline_semantics_unknown")
    if set(value) != {"baseline_id"}:
        raise BaselineSemanticError("baseline_semantics_unknown")
    baseline_id = value.get("baseline_id")
    if baseline_id not in CANONICAL_BASELINE_IDS:
        raise BaselineSemanticError("baseline_semantics_unknown")
    return {str(baseline_id)}
