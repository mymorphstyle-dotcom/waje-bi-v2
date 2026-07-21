from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from math import isfinite
from typing import Any

from bi_agent.capabilities import make_evidence_envelope


class MarketChannelContextContractError(ValueError):
    pass


_COMPLETENESS_FIELDS = frozenset(
    {
        "result_ref",
        "completeness_report_ref",
        "completeness_status",
        "analysis_readiness",
        "reconciliation_status",
    }
)
_COMPLETENESS_STATES = frozenset(
    {"complete", "partial", "empty", "invalid", "truncated", "stale"}
)
_READINESS_STATES = frozenset({"ready", "degraded", "blocked"})
_RECONCILIATION_STATES = frozenset({"passed", "failed", "pending"})
_WINDOW_PRESENCE_POLICIES = frozenset({"all", "reconciled_zero_fill"})


def market_channel_context(
    rows: Iterable[Mapping[str, Any]],
    *,
    metric_id: str,
    value_key: str,
    channel_key: str,
    window_id_key: str,
    observation_key: str,
    required_window_ids: Sequence[str],
    required_window_presence: str,
    completeness_records: Sequence[Mapping[str, Any]],
    result_refs: tuple[str, ...] = (),
    evidence_ref: str | None = None,
):
    """Report channel coverage and comparability under a trust boundary."""

    metric_id = _required_name(metric_id, "market_channel_metric_id_invalid")
    value_key = _required_name(value_key, "market_channel_value_key_invalid")
    channel_key = _required_name(channel_key, "market_channel_channel_key_invalid")
    window_id_key = _required_name(
        window_id_key, "market_channel_window_id_key_invalid"
    )
    observation_key = _required_name(
        observation_key, "market_channel_observation_key_invalid"
    )
    required_windows = _string_tuple(
        required_window_ids,
        "market_channel_required_windows_invalid",
        allow_empty=False,
    )
    if required_window_presence not in _WINDOW_PRESENCE_POLICIES:
        raise MarketChannelContextContractError(
            "market_channel_window_presence_policy_invalid"
        )
    completeness = _completeness_records(completeness_records)
    prepared = _rows(
        rows,
        value_key=value_key,
        channel_key=channel_key,
        window_id_key=window_id_key,
        observation_key=observation_key,
        required_windows=frozenset(required_windows),
    )
    if prepared and not completeness:
        raise MarketChannelContextContractError(
            "market_channel_completeness_records_missing"
        )

    reconciliation_state = _reconciliation_state(completeness)
    completeness_ready = bool(completeness) and all(
        record["completeness_status"] == "complete"
        and record["analysis_readiness"] == "ready"
        for record in completeness
    )
    comparison_authorized = completeness_ready and reconciliation_state == "passed"
    zero_fill_authorized = (
        required_window_presence == "reconciled_zero_fill"
        and comparison_authorized
    )
    rows_by_channel: dict[str, list[Mapping[str, Any]]] = {}
    for row in prepared:
        rows_by_channel.setdefault(str(row[channel_key]), []).append(row)

    channels = []
    limitations = []
    for channel, channel_rows in sorted(rows_by_channel.items()):
        observed_windows = tuple(
            window_id
            for window_id in required_windows
            if any(row[window_id_key] == window_id for row in channel_rows)
        )
        structurally_absent_windows = tuple(
            window_id
            for window_id in required_windows
            if window_id not in observed_windows
        )
        zero_filled_windows = (
            structurally_absent_windows if zero_fill_authorized else ()
        )
        missing_windows = (
            () if zero_fill_authorized else structurally_absent_windows
        )
        non_null_values = sum(1 for row in channel_rows if row[value_key] is not None)
        comparable = (
            comparison_authorized
            and not missing_windows
            and non_null_values == len(channel_rows)
        )
        if missing_windows:
            limitations.append(f"channel_window_coverage_incomplete:{channel}")
        if non_null_values != len(channel_rows):
            limitations.append(f"channel_metric_coverage_incomplete:{channel}")
        channels.append(
            {
                "channel": channel,
                "observed_window_ids": observed_windows,
                "zero_filled_window_ids": zero_filled_windows,
                "missing_window_ids": missing_windows,
                "observation_count": len(channel_rows),
                "metric_non_null_count": non_null_values,
                "comparable": comparable,
            }
        )

    if reconciliation_state != "passed":
        limitations.append("overall_channel_reconciliation_" + reconciliation_state)
    incomplete_reports = tuple(
        record["completeness_report_ref"]
        for record in completeness
        if record["completeness_status"] != "complete"
        or record["analysis_readiness"] != "ready"
    )
    if incomplete_reports:
        limitations.append("channel_context_completeness_limited")
    if not channels:
        limitations.append("no_channel_context_rows")

    evidence_available = bool(channels)
    comparable_channel_count = sum(
        1 for channel in channels if channel["comparable"]
    )
    incomplete_channel_count = len(channels) - comparable_channel_count
    return make_evidence_envelope(
        "market_channel_context",
        evidence_ref=evidence_ref,
        evidence_type=(
            "trust_boundary" if evidence_available else "insufficient_evidence"
        ),
        strength=("trust_boundary" if evidence_available else "insufficient"),
        wording_limit=("context_only" if evidence_available else "insufficient"),
        numeric_facts={
            "channel_count": len(channels),
            "comparable_channel_count": comparable_channel_count,
            "incomplete_channel_count": incomplete_channel_count,
            "zero_filled_channel_count": sum(
                1 for channel in channels if channel["zero_filled_window_ids"]
            ),
        },
        typed_payload={
            "metric_id": metric_id,
            "required_window_ids": required_windows,
            "required_window_presence": required_window_presence,
            "reconciliation_state": reconciliation_state,
            "comparison_authorized": comparison_authorized,
            "zero_fill_authorized": zero_fill_authorized,
            "completeness": completeness,
            "channels": tuple(channels),
            "interpretation_contract": {
                "contract_id": "market-channel-coverage-interpretation.v1",
                "analysis_role": "coverage_and_trust_boundary",
                "source_availability": (
                    "available" if evidence_available else "unavailable"
                ),
                "evidence_role": (
                    "background_context" if evidence_available else "unavailable"
                ),
                "allowed_use": "background_and_candidate_localization",
                "blocked_use": "direct_attribution_or_causal_conclusion",
                "customer_wording_policy": (
                    "describe_role_limit_not_missing_data"
                ),
                "channel_count_partition": {
                    "whole": "channel_count",
                    "parts": (
                        "comparable_channel_count",
                        "incomplete_channel_count",
                    ),
                    "relationship": "whole_equals_sum_of_parts",
                },
                "zero_fill_policy": required_window_presence,
                "zero_fill_authority": (
                    "complete_query_and_passed_overall_reconciliation"
                ),
                "structurally_absent_group_value": "zero_when_authorized",
                "attribution_claim_allowed": False,
            },
            "claim_ceiling": "trust_boundary",
            "attribution_claim_allowed": False,
            "causal_claim_allowed": False,
        },
        limitations=tuple(dict.fromkeys(limitations)),
        result_refs=result_refs,
    )


def _completeness_records(
    value: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MarketChannelContextContractError(
            "market_channel_completeness_records_invalid"
        )
    records = []
    result_refs: set[str] = set()
    report_refs: set[str] = set()
    for record in value:
        if not isinstance(record, Mapping) or set(record) != _COMPLETENESS_FIELDS:
            raise MarketChannelContextContractError(
                "market_channel_completeness_record_shape_invalid"
            )
        result_ref = _required_name(
            record["result_ref"],
            "market_channel_completeness_result_ref_invalid",
        )
        report_ref = _required_name(
            record["completeness_report_ref"],
            "market_channel_completeness_report_ref_invalid",
        )
        if result_ref in result_refs or report_ref in report_refs:
            raise MarketChannelContextContractError(
                "market_channel_completeness_record_duplicate"
            )
        status = record["completeness_status"]
        readiness = record["analysis_readiness"]
        reconciliation = record["reconciliation_status"]
        if status not in _COMPLETENESS_STATES:
            raise MarketChannelContextContractError(
                "market_channel_completeness_status_invalid"
            )
        if readiness not in _READINESS_STATES:
            raise MarketChannelContextContractError(
                "market_channel_analysis_readiness_invalid"
            )
        if reconciliation not in _RECONCILIATION_STATES:
            raise MarketChannelContextContractError(
                "market_channel_reconciliation_status_invalid"
            )
        records.append(
            {
                "result_ref": result_ref,
                "completeness_report_ref": report_ref,
                "completeness_status": status,
                "analysis_readiness": readiness,
                "reconciliation_status": reconciliation,
            }
        )
        result_refs.add(result_ref)
        report_refs.add(report_ref)
    return tuple(sorted(records, key=lambda item: item["result_ref"]))


def _reconciliation_state(records: Sequence[Mapping[str, str]]) -> str:
    states = {record["reconciliation_status"] for record in records}
    if "failed" in states:
        return "failed"
    if states and states == {"passed"}:
        return "passed"
    return "pending"


def _rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    value_key: str,
    channel_key: str,
    window_id_key: str,
    observation_key: str,
    required_windows: frozenset[str],
) -> tuple[Mapping[str, Any], ...]:
    normalized = []
    seen: set[tuple[str, str, tuple[str, str]]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise MarketChannelContextContractError(
                "market_channel_context_row_invalid"
            )
        required = (value_key, channel_key, window_id_key, observation_key)
        if any(key not in row for key in required):
            raise MarketChannelContextContractError(
                "market_channel_context_row_field_missing"
            )
        channel = _required_name(
            row[channel_key], "market_channel_context_channel_invalid"
        )
        window_id = _required_name(
            row[window_id_key], "market_channel_context_window_invalid"
        )
        if window_id not in required_windows:
            raise MarketChannelContextContractError(
                "market_channel_context_window_unbound"
            )
        observation = _observation_identity(row[observation_key])
        identity = (channel, window_id, observation)
        if identity in seen:
            raise MarketChannelContextContractError(
                "market_channel_context_observation_duplicate"
            )
        seen.add(identity)
        if row[value_key] is not None:
            _finite_number(
                row[value_key], "market_channel_context_metric_value_invalid"
            )
        normalized.append(row)
    return tuple(normalized)


def _observation_identity(value: Any) -> tuple[str, str]:
    if value is None or isinstance(value, bool):
        raise MarketChannelContextContractError(
            "market_channel_context_observation_invalid"
        )
    if isinstance(value, str) and (not value or value != value.strip()):
        raise MarketChannelContextContractError(
            "market_channel_context_observation_invalid"
        )
    return type(value).__name__, repr(value)


def _required_name(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MarketChannelContextContractError(error)
    return value


def _string_tuple(value: Any, error: str, *, allow_empty: bool) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MarketChannelContextContractError(error)
    normalized = tuple(_required_name(item, error) for item in value)
    if (not allow_empty and not normalized) or len(normalized) != len(set(normalized)):
        raise MarketChannelContextContractError(error)
    return normalized


def _finite_number(value: Any, error: str) -> float:
    if isinstance(value, bool):
        raise MarketChannelContextContractError(error)
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketChannelContextContractError(error) from exc
    if not isfinite(normalized):
        raise MarketChannelContextContractError(error)
    return normalized


__all__ = (
    "MarketChannelContextContractError",
    "market_channel_context",
)
