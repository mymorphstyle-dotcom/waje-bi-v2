from __future__ import annotations

from collections.abc import Mapping, Sequence
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

_PREVIOUS_DAY_ALIASES = ("前日", "前一日", "前一天")
_CONTROLLED_METRIC_SUFFIXES = frozenset(
    {
        "付费金额",
        "收入",
        "付费人数",
        "付费用户数",
        "付费订单数",
        "首充人数",
        "活跃用户",
        "日活",
        "新增用户",
        "注册数",
        "投放成本",
        "利润",
        "玩家下注金额",
        "下注金额",
        "paidamount",
        "revenue",
        "paidusers",
        "paidorders",
        "firstpaidusers",
        "activeusers",
        "newusers",
        "registrations",
        "aggregatemarketingcost",
        "profit",
        "playerbetamount",
    }
)
_ROLLING_TYPES = frozenset({"rollingaverage", "rollingmean", "rollingavg"})
_SAME_WEEKDAY_TYPES = frozenset({"sameweekday", "samedaylastweek"})

_ALIASES = {
    "previousday": "previous_day",
    "priorday": "previous_day",
    "前日": "previous_day",
    "前一日": "previous_day",
    "前一天": "previous_day",
    "rolling7daybaseline": "rolling_7_day_baseline",
    "rolling7davg": "rolling_7_day_baseline",
    "rolling7dayavg": "rolling_7_day_baseline",
    "rolling7daymean": "rolling_7_day_baseline",
    "rolling7dayaverage": "rolling_7_day_baseline",
    "past7daysavg": "rolling_7_day_baseline",
    "past7daysmean": "rolling_7_day_baseline",
    "past7daysaverage": "rolling_7_day_baseline",
    "last7daysavg": "rolling_7_day_baseline",
    "last7daysmean": "rolling_7_day_baseline",
    "last7daysaverage": "rolling_7_day_baseline",
    "近7日均值": "rolling_7_day_baseline",
    "近7天均值": "rolling_7_day_baseline",
    "近7日平均": "rolling_7_day_baseline",
    "近7天平均": "rolling_7_day_baseline",
    "sameweekdaylastweek": "same_weekday_last_week",
    "samedaylastweek": "same_weekday_last_week",
    "sameweekdaypreviousweek": "same_weekday_last_week",
    "lastweeksameday": "same_weekday_last_week",
    "上周同日": "same_weekday_last_week",
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
        baseline_id
        for baseline_id in CANONICAL_BASELINE_IDS
        if baseline_id in found
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
        baseline_id = _canonical_text(value)
        if not baseline_id:
            raise BaselineSemanticError("baseline_semantics_unknown")
        return {baseline_id}
    if not isinstance(value, Mapping):
        raise BaselineSemanticError("baseline_semantics_unknown")

    found: set[str] = set()
    has_type = "type" in value
    baseline_type = _compact(str(value.get("type") or ""))
    if baseline_type in _ROLLING_TYPES:
        if (
            str(value.get("window") or "").strip() != "7"
            or "lag_weeks" in value
        ):
            raise BaselineSemanticError("baseline_semantics_conflict")
        found.add("rolling_7_day_baseline")
    elif baseline_type in _SAME_WEEKDAY_TYPES:
        lag_weeks = value.get("lag_weeks", 1)
        if (
            str(lag_weeks).strip() != "1"
            or "window" in value
        ):
            raise BaselineSemanticError("baseline_semantics_conflict")
        found.add("same_weekday_last_week")
    elif has_type or "window" in value or "lag_weeks" in value:
        raise BaselineSemanticError("baseline_semantics_unknown")

    for key in (
        "baseline_id",
        "id",
        "value",
        "ref",
        "description",
        "label",
        "name",
        "baseline",
    ):
        nested = value.get(key)
        if nested in (None, "", {}, [], ()):
            continue
        try:
            found.update(_candidate_ids(nested))
        except BaselineSemanticError as exc:
            if str(exc) == "baseline_semantics_conflict":
                raise
            continue
    if len(found) != 1:
        reason = (
            "baseline_semantics_conflict"
            if found
            else "baseline_semantics_unknown"
        )
        raise BaselineSemanticError(reason)
    return found


def _canonical_text(value: str) -> str:
    direct = value.strip()
    if direct in CANONICAL_BASELINE_IDS:
        return direct
    compact = _compact(direct)
    for alias in _PREVIOUS_DAY_ALIASES:
        if compact.startswith(alias):
            suffix = compact[len(alias) :]
            if suffix in _CONTROLLED_METRIC_SUFFIXES:
                return "previous_day"
    return _ALIASES.get(compact, "")


def _compact(value: str) -> str:
    return "".join(
        character
        for character in value.strip().lower()
        if character not in {" ", "_", "-"}
    )
