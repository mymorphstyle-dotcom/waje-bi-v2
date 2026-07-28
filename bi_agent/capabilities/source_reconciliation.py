from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from bi_agent.capabilities import make_evidence_envelope


class SourceReconciliationContractError(ValueError):
    pass


_SOURCE_FIELDS = frozenset(
    {
        "source_id",
        "result_ref",
        "metric_contract_ref",
        "reconciliation_tolerance",
        "reconciliation_strategy",
        "rows",
    }
)
_POLICY_FIELDS = frozenset(
    {
        "contract_id",
        "authoritative_source_id",
        "partition_source_id",
        "window_id_key",
        "window_role_key",
        "bounded_window_relative_tolerance",
        "bounded_change_residual_share",
        "hard_observation_relative_limit",
    }
)
_COMPARABLE_STRATEGIES = frozenset(
    {"additive_sum", "exact_additive_count", "ratio_from_components"}
)
_RECONCILIATION_CONTRACT = "bounded-window-source-reconciliation.v1"
_CLAIM_MATERIAL_WINDOW_LIMIT = 12


def source_reconciliation(
    sources: Sequence[Mapping[str, Any]],
    *,
    join_keys: Sequence[str],
    value_key: str,
    reconciliation_tolerance: float,
    reconciliation_strategy: str,
    reconciliation_policy: Mapping[str, Any],
    result_refs: tuple[str, ...] = (),
    evidence_ref: str | None = None,
):
    """Reconcile an authoritative total with a partition total in the active windows."""

    join_keys = _string_tuple(
        join_keys,
        "source_reconciliation_join_keys_invalid",
        allow_empty=False,
    )
    value_key = _required_name(value_key, "source_reconciliation_value_key_invalid")
    exact_tolerance = _non_negative_decimal(
        reconciliation_tolerance,
        "source_reconciliation_tolerance_invalid",
    )
    reconciliation_strategy = _required_name(
        reconciliation_strategy,
        "source_reconciliation_strategy_invalid",
    )
    policy = _policy(reconciliation_policy, join_keys=join_keys)
    normalized_sources = _sources(
        sources,
        join_keys=join_keys,
        value_key=value_key,
        window_id_key=policy["window_id_key"],
        window_role_key=policy["window_role_key"],
        expected_tolerance=exact_tolerance,
        expected_strategy=reconciliation_strategy,
    )
    sources_by_id = {source["source_id"]: source for source in normalized_sources}
    if set(sources_by_id) != {
        policy["authoritative_source_id"],
        policy["partition_source_id"],
    }:
        raise SourceReconciliationContractError(
            "source_reconciliation_policy_source_identity_mismatch"
        )
    authoritative = sources_by_id[policy["authoritative_source_id"]]
    partition = sources_by_id[policy["partition_source_id"]]
    contract_refs = {source["metric_contract_ref"] for source in normalized_sources}
    if len(contract_refs) != 1:
        raise SourceReconciliationContractError(
            "source_reconciliation_metric_contract_inconsistent"
        )

    base_payload = {
        "evidence_contract": policy["contract_id"],
        "source_ids": tuple(source["source_id"] for source in normalized_sources),
        "source_roles": {
            "authoritative_total": authoritative["source_id"],
            "partition_total": partition["source_id"],
        },
        "source_result_refs": {
            source["source_id"]: source["result_ref"] for source in normalized_sources
        },
        "metric_contract_ref": next(iter(contract_refs)),
        "join_keys": join_keys,
        "value_key": value_key,
        "reconciliation_tolerance": exact_tolerance,
        "reconciliation_strategy": reconciliation_strategy,
        "reconciliation_policy": policy,
    }
    if reconciliation_strategy not in _COMPARABLE_STRATEGIES:
        return _unavailable_envelope(
            base_payload,
            reconciliation_state="incomplete",
            numeric_facts=_count_facts(),
            limitations=(
                f"reconciliation_strategy_unsupported:{reconciliation_strategy}",
            ),
            result_refs=result_refs,
            evidence_ref=evidence_ref,
        )

    authoritative_rows = authoritative["rows_by_key"]
    partition_rows = partition["rows_by_key"]
    common_keys = set(authoritative_rows).intersection(partition_rows)
    observations = []
    for identity in sorted(common_keys, key=repr):
        authoritative_row = authoritative_rows[identity]
        partition_row = partition_rows[identity]
        if (
            authoritative_row["window_id"] != partition_row["window_id"]
            or authoritative_row["window_role"] != partition_row["window_role"]
        ):
            raise SourceReconciliationContractError(
                "source_reconciliation_window_identity_mismatch"
            )
        authoritative_value = authoritative_row["value"]
        partition_value = partition_row["value"]
        residual = authoritative_value - partition_value
        residual_ratio = _relative_residual(residual, authoritative_value)
        observation_state = (
            "exact_match"
            if abs(residual) <= exact_tolerance
            else "bounded_match"
            if residual_ratio is not None
            and residual_ratio <= policy["hard_observation_relative_limit"]
            else "failed"
        )
        observations.append(
            {
                "join_key": authoritative_row["join_key"],
                "window_id": authoritative_row["window_id"],
                "window_role": authoritative_row["window_role"],
                "authoritative_value": authoritative_value,
                "partition_value": partition_value,
                "residual": residual,
                "absolute_residual": abs(residual),
                "residual_ratio": residual_ratio,
                "state": observation_state,
            }
        )

    missing_by_source = {
        authoritative["source_id"]: tuple(
            partition_rows[identity]["join_key"]
            for identity in sorted(
                set(partition_rows) - set(authoritative_rows), key=repr
            )
        ),
        partition["source_id"]: tuple(
            authoritative_rows[identity]["join_key"]
            for identity in sorted(
                set(authoritative_rows) - set(partition_rows), key=repr
            )
        ),
    }
    missing_count = sum(len(items) for items in missing_by_source.values())
    windows = _window_reconciliations(
        observations,
        exact_tolerance=exact_tolerance,
        bounded_window_relative_tolerance=policy[
            "bounded_window_relative_tolerance"
        ],
    )
    change = _change_reconciliation(
        windows,
        bounded_change_residual_share=policy[
            "bounded_change_residual_share"
        ],
    )
    exact_count = sum(item["state"] == "exact_match" for item in observations)
    bounded_count = sum(item["state"] == "bounded_match" for item in observations)
    failed_count = sum(item["state"] == "failed" for item in observations)
    numeric_facts = _count_facts(
        exact=exact_count,
        bounded=bounded_count,
        failed=failed_count,
        missing=missing_count,
    )
    numeric_facts.update(
        {
            "window_count": len(windows),
            "max_observation_residual_ratio": _max_ratio(
                item["residual_ratio"] for item in observations
            ),
            "max_window_residual_ratio": _max_ratio(
                item["residual_ratio"] for item in windows
            ),
        }
    )
    if change is not None:
        numeric_facts.update(
            {
                "authoritative_change": change["authoritative_change"],
                "partition_change": change["partition_change"],
                "residual_change": change["residual_change"],
                "residual_change_share": change["residual_change_share"],
            }
        )

    incomplete = missing_count > 0 or not common_keys
    failed = (
        failed_count > 0
        or any(item["state"] == "failed" for item in windows)
        or change is not None
        and change["state"] == "failed"
    )
    reconciliation_state = (
        "incomplete"
        if incomplete
        else "failed"
        if failed
        else "exact_match"
        if exact_count == len(observations)
        else "bounded_match"
    )
    residual_bucket = _residual_bucket(windows, change)
    payload = {
        **base_payload,
        "reconciliation_state": reconciliation_state,
        "observation_reconciliations": tuple(observations),
        "window_reconciliations": tuple(windows),
        "change_reconciliation": change,
        "missing_by_source": missing_by_source,
        "residual_bucket": residual_bucket,
        "claim_ceiling": (
            "quantified_contribution"
            if reconciliation_state in {"exact_match", "bounded_match"}
            else "insufficient"
        ),
        "metric_claim_allowed": reconciliation_state
        in {"exact_match", "bounded_match"},
        "interpretation_contract": _interpretation_contract(
            reconciliation_state
        ),
    }
    payload["claim_material_observations"] = (
        _claim_material_summary(payload, numeric_facts=numeric_facts),
    )
    if reconciliation_state == "incomplete":
        limitations = ["window_reconciliation_incomplete"]
        for source_id, missing in missing_by_source.items():
            if missing:
                limitations.append(f"source_rows_missing:{source_id}")
        if not common_keys:
            limitations.append("no_reconciled_pairs")
        return _unavailable_envelope(
            payload,
            reconciliation_state=reconciliation_state,
            numeric_facts=numeric_facts,
            limitations=tuple(limitations),
            result_refs=result_refs,
            evidence_ref=evidence_ref,
        )
    if reconciliation_state == "failed":
        return _unavailable_envelope(
            payload,
            reconciliation_state=reconciliation_state,
            numeric_facts=numeric_facts,
            limitations=("window_reconciliation_threshold_exceeded",),
            result_refs=result_refs,
            evidence_ref=evidence_ref,
        )
    return make_evidence_envelope(
        "source_reconciliation",
        evidence_ref=evidence_ref,
        evidence_type="accounting_contribution",
        strength="quantified_contribution",
        wording_limit=(
            "exact_accounting_contribution"
            if reconciliation_state == "exact_match"
            else "bounded_accounting_contribution_with_residual"
        ),
        numeric_facts=numeric_facts,
        typed_payload=payload,
        limitations=(),
        result_refs=result_refs,
    )


def _claim_material_summary(
    payload: Mapping[str, Any],
    *,
    numeric_facts: Mapping[str, Any],
) -> dict[str, Any]:
    windows = tuple(payload.get("window_reconciliations") or ())
    ranked_windows = tuple(
        sorted(
            windows,
            key=lambda item: (
                item.get("residual_ratio") is not None,
                item.get("residual_ratio") or Decimal(0),
                str(item.get("window_id") or ""),
            ),
            reverse=True,
        )
    )
    displayed_windows = ranked_windows[:_CLAIM_MATERIAL_WINDOW_LIMIT]
    missing_by_source = payload.get("missing_by_source")
    missing_counts = (
        {
            str(source_id): len(tuple(items))
            for source_id, items in missing_by_source.items()
        }
        if isinstance(missing_by_source, Mapping)
        else {}
    )
    return {
        "projection_kind": "claim_material_summary",
        "evidence_contract": payload["evidence_contract"],
        "reconciliation_state": payload["reconciliation_state"],
        "source_ids": payload["source_ids"],
        "source_roles": payload["source_roles"],
        "metric_contract_ref": payload["metric_contract_ref"],
        "reconciliation_tolerance": payload["reconciliation_tolerance"],
        "reconciliation_strategy": payload["reconciliation_strategy"],
        "reconciliation_policy": payload["reconciliation_policy"],
        "observation_count": (
            int(numeric_facts.get("exact_pair_count") or 0)
            + int(numeric_facts.get("bounded_pair_count") or 0)
            + int(numeric_facts.get("failed_pair_count") or 0)
        ),
        "exact_pair_count": int(numeric_facts.get("exact_pair_count") or 0),
        "bounded_pair_count": int(numeric_facts.get("bounded_pair_count") or 0),
        "failed_pair_count": int(numeric_facts.get("failed_pair_count") or 0),
        "missing_pair_count": int(numeric_facts.get("missing_pair_count") or 0),
        "missing_pair_count_by_source": missing_counts,
        "window_count": len(windows),
        "displayed_window_count": len(displayed_windows),
        "omitted_window_count": len(windows) - len(displayed_windows),
        "window_selection_policy": "largest_residual_ratio_first",
        "window_record_limit": _CLAIM_MATERIAL_WINDOW_LIMIT,
        "worst_window_reconciliations": displayed_windows,
        "max_observation_residual_ratio": numeric_facts.get(
            "max_observation_residual_ratio"
        ),
        "max_window_residual_ratio": numeric_facts.get(
            "max_window_residual_ratio"
        ),
        "change_reconciliation": payload.get("change_reconciliation"),
        "residual_bucket": payload.get("residual_bucket"),
        "claim_ceiling": payload["claim_ceiling"],
        "metric_claim_allowed": payload["metric_claim_allowed"],
        "interpretation_contract": payload["interpretation_contract"],
    }


def _policy(
    value: Mapping[str, Any], *, join_keys: tuple[str, ...]
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _POLICY_FIELDS:
        raise SourceReconciliationContractError(
            "source_reconciliation_policy_shape_invalid"
        )
    contract_id = _required_name(
        value["contract_id"], "source_reconciliation_policy_contract_invalid"
    )
    if contract_id != _RECONCILIATION_CONTRACT:
        raise SourceReconciliationContractError(
            "source_reconciliation_policy_contract_unsupported"
        )
    authoritative_source_id = _required_name(
        value["authoritative_source_id"],
        "source_reconciliation_authoritative_source_invalid",
    )
    partition_source_id = _required_name(
        value["partition_source_id"],
        "source_reconciliation_partition_source_invalid",
    )
    if authoritative_source_id == partition_source_id:
        raise SourceReconciliationContractError(
            "source_reconciliation_policy_source_identity_invalid"
        )
    window_id_key = _required_name(
        value["window_id_key"], "source_reconciliation_window_id_key_invalid"
    )
    window_role_key = _required_name(
        value["window_role_key"], "source_reconciliation_window_role_key_invalid"
    )
    if window_id_key not in join_keys:
        raise SourceReconciliationContractError(
            "source_reconciliation_window_id_not_join_key"
        )
    bounded_window = _bounded_ratio(
        value["bounded_window_relative_tolerance"],
        "source_reconciliation_window_tolerance_invalid",
    )
    bounded_change = _bounded_ratio(
        value["bounded_change_residual_share"],
        "source_reconciliation_change_tolerance_invalid",
    )
    hard_observation = _bounded_ratio(
        value["hard_observation_relative_limit"],
        "source_reconciliation_observation_limit_invalid",
    )
    if bounded_window > hard_observation:
        raise SourceReconciliationContractError(
            "source_reconciliation_threshold_order_invalid"
        )
    return {
        "contract_id": contract_id,
        "authoritative_source_id": authoritative_source_id,
        "partition_source_id": partition_source_id,
        "window_id_key": window_id_key,
        "window_role_key": window_role_key,
        "bounded_window_relative_tolerance": bounded_window,
        "bounded_change_residual_share": bounded_change,
        "hard_observation_relative_limit": hard_observation,
    }


def _sources(
    sources: Sequence[Mapping[str, Any]],
    *,
    join_keys: tuple[str, ...],
    value_key: str,
    window_id_key: str,
    window_role_key: str,
    expected_tolerance: Decimal,
    expected_strategy: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        isinstance(sources, (str, bytes))
        or not isinstance(sources, Sequence)
        or len(sources) != 2
    ):
        raise SourceReconciliationContractError(
            "source_reconciliation_requires_two_sources"
        )
    normalized = []
    source_ids: set[str] = set()
    result_refs: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != _SOURCE_FIELDS:
            raise SourceReconciliationContractError(
                "source_reconciliation_source_shape_invalid"
            )
        source_id = _required_name(
            source["source_id"], "source_reconciliation_source_id_invalid"
        )
        result_ref = _required_name(
            source["result_ref"], "source_reconciliation_result_ref_invalid"
        )
        metric_contract_ref = _required_name(
            source["metric_contract_ref"],
            "source_reconciliation_metric_contract_ref_invalid",
        )
        tolerance = _non_negative_decimal(
            source["reconciliation_tolerance"],
            "source_reconciliation_source_tolerance_invalid",
        )
        strategy = _required_name(
            source["reconciliation_strategy"],
            "source_reconciliation_source_strategy_invalid",
        )
        if tolerance != expected_tolerance or strategy != expected_strategy:
            raise SourceReconciliationContractError(
                "source_reconciliation_metric_contract_inconsistent"
            )
        if source_id in source_ids or result_ref in result_refs:
            raise SourceReconciliationContractError(
                "source_reconciliation_source_identity_duplicate"
            )
        rows_by_key = _source_rows(
            source["rows"],
            join_keys=join_keys,
            value_key=value_key,
            window_id_key=window_id_key,
            window_role_key=window_role_key,
        )
        normalized.append(
            {
                "source_id": source_id,
                "result_ref": result_ref,
                "metric_contract_ref": metric_contract_ref,
                "reconciliation_tolerance": tolerance,
                "reconciliation_strategy": strategy,
                "rows_by_key": rows_by_key,
            }
        )
        source_ids.add(source_id)
        result_refs.add(result_ref)
    return normalized[0], normalized[1]


def _source_rows(
    rows: Any,
    *,
    join_keys: tuple[str, ...],
    value_key: str,
    window_id_key: str,
    window_role_key: str,
) -> dict[tuple[tuple[str, str], ...], dict[str, Any]]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise SourceReconciliationContractError("source_reconciliation_rows_invalid")
    by_key = {}
    role_by_window: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise SourceReconciliationContractError("source_reconciliation_row_invalid")
        if any(
            key not in row
            for key in (*join_keys, value_key, window_id_key, window_role_key)
        ):
            raise SourceReconciliationContractError(
                "source_reconciliation_row_field_missing"
            )
        join_key = {key: row[key] for key in join_keys}
        identity = tuple(_join_identity(row[key]) for key in join_keys)
        if identity in by_key:
            raise SourceReconciliationContractError(
                "source_reconciliation_join_key_duplicate"
            )
        window_id = _required_name(
            row[window_id_key], "source_reconciliation_window_id_invalid"
        )
        window_role = _required_name(
            row[window_role_key], "source_reconciliation_window_role_invalid"
        )
        existing_role = role_by_window.setdefault(window_id, window_role)
        if existing_role != window_role:
            raise SourceReconciliationContractError(
                "source_reconciliation_window_role_inconsistent"
            )
        by_key[identity] = {
            "join_key": join_key,
            "window_id": window_id,
            "window_role": window_role,
            "value": _finite_decimal(
                row[value_key], "source_reconciliation_value_invalid"
            ),
        }
    return by_key


def _window_reconciliations(
    observations: Sequence[Mapping[str, Any]],
    *,
    exact_tolerance: Decimal,
    bounded_window_relative_tolerance: Decimal,
) -> tuple[dict[str, Any], ...]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for observation in observations:
        grouped.setdefault(str(observation["window_id"]), []).append(observation)
    windows = []
    for window_id, items in sorted(grouped.items()):
        roles = {str(item["window_role"]) for item in items}
        if len(roles) != 1:
            raise SourceReconciliationContractError(
                "source_reconciliation_window_role_inconsistent"
            )
        authoritative_total = sum(
            (item["authoritative_value"] for item in items), Decimal(0)
        )
        partition_total = sum(
            (item["partition_value"] for item in items), Decimal(0)
        )
        residual = authoritative_total - partition_total
        residual_ratio = _relative_residual(residual, authoritative_total)
        state = (
            "exact_match"
            if abs(residual) <= exact_tolerance
            and all(item["state"] == "exact_match" for item in items)
            else "bounded_match"
            if residual_ratio is not None
            and residual_ratio <= bounded_window_relative_tolerance
            and all(item["state"] != "failed" for item in items)
            else "failed"
        )
        windows.append(
            {
                "window_id": window_id,
                "window_role": next(iter(roles)),
                "observation_count": len(items),
                "authoritative_total": authoritative_total,
                "partition_total": partition_total,
                "residual": residual,
                "absolute_residual": abs(residual),
                "residual_ratio": residual_ratio,
                "state": state,
            }
        )
    return tuple(windows)


def _change_reconciliation(
    windows: Sequence[Mapping[str, Any]],
    *,
    bounded_change_residual_share: Decimal,
) -> dict[str, Any] | None:
    by_role: dict[str, list[Mapping[str, Any]]] = {}
    for window in windows:
        by_role.setdefault(str(window["window_role"]), []).append(window)
    if len(by_role.get("target", ())) != 1 or len(by_role.get("baseline", ())) != 1:
        return None
    target = by_role["target"][0]
    baseline = by_role["baseline"][0]
    authoritative_change = (
        target["authoritative_total"] - baseline["authoritative_total"]
    )
    partition_change = target["partition_total"] - baseline["partition_total"]
    residual_change = target["residual"] - baseline["residual"]
    residual_change_share = _relative_residual(
        residual_change, authoritative_change
    )
    state = (
        "exact_match"
        if residual_change == 0
        else "bounded_match"
        if residual_change_share is not None
        and residual_change_share <= bounded_change_residual_share
        else "failed"
    )
    return {
        "target_window_id": target["window_id"],
        "baseline_window_id": baseline["window_id"],
        "authoritative_change": authoritative_change,
        "partition_change": partition_change,
        "residual_change": residual_change,
        "residual_change_share": residual_change_share,
        "closure_identity": {
            "left": authoritative_change,
            "partition_component": partition_change,
            "residual_component": residual_change,
            "closed": authoritative_change == partition_change + residual_change,
        },
        "state": state,
    }


def _residual_bucket(
    windows: Sequence[Mapping[str, Any]], change: Mapping[str, Any] | None
) -> dict[str, Any]:
    return {
        "bucket_id": "unreconciled_residual",
        "business_label": "未调和部分",
        "participates_in_closure": True,
        "ranking_eligible": False,
        "disclosure_required": True,
        "window_values": tuple(
            {
                "window_id": item["window_id"],
                "window_role": item["window_role"],
                "value": item["residual"],
                "relative_to_authoritative": item["residual_ratio"],
            }
            for item in windows
        ),
        "change_value": change["residual_change"] if change is not None else None,
        "change_share": (
            change["residual_change_share"] if change is not None else None
        ),
    }


def _interpretation_contract(reconciliation_state: str) -> dict[str, Any]:
    allowed_uses = (
        (
            "exact_accounting_identity",
            "partition_total_change_contribution",
        )
        if reconciliation_state == "exact_match"
        else (
            "bounded_accounting_identity_with_residual",
            "partition_total_change_contribution_with_residual",
        )
        if reconciliation_state == "bounded_match"
        else ()
    )
    return {
        "evidence_contract": _RECONCILIATION_CONTRACT,
        "reconciliation_state": reconciliation_state,
        "allowed_uses": allowed_uses,
        "blocked_uses": (
            "causal_conclusion",
            "partition_member_ranking_without_member_evidence",
            "ranking_the_residual_bucket",
        ),
        "residual_disclosure": "required",
        "unknown_internal_state_policy": "contract_violation",
    }


def _unavailable_envelope(
    payload: Mapping[str, Any],
    *,
    reconciliation_state: str,
    numeric_facts: Mapping[str, Any],
    limitations: tuple[str, ...],
    result_refs: tuple[str, ...],
    evidence_ref: str | None,
):
    typed_payload = dict(payload)
    typed_payload.setdefault("reconciliation_state", reconciliation_state)
    typed_payload.setdefault(
        "claim_ceiling",
        "insufficient",
    )
    typed_payload.setdefault("metric_claim_allowed", False)
    typed_payload.setdefault(
        "interpretation_contract", _interpretation_contract(reconciliation_state)
    )
    return make_evidence_envelope(
        "source_reconciliation",
        evidence_ref=evidence_ref,
        evidence_type="insufficient_evidence",
        strength="insufficient",
        wording_limit="insufficient",
        numeric_facts=dict(numeric_facts),
        typed_payload=typed_payload,
        limitations=limitations,
        result_refs=result_refs,
    )


def _count_facts(
    *, exact: int = 0, bounded: int = 0, failed: int = 0, missing: int = 0
) -> dict[str, int]:
    return {
        "exact_pair_count": exact,
        "bounded_pair_count": bounded,
        "failed_pair_count": failed,
        "missing_pair_count": missing,
    }


def _relative_residual(residual: Decimal, authoritative: Decimal) -> Decimal | None:
    if authoritative == 0:
        return Decimal(0) if residual == 0 else None
    return abs(residual) / abs(authoritative)


def _max_ratio(values: Sequence[Decimal | None] | Any) -> Decimal | None:
    finite = tuple(value for value in values if value is not None)
    return max(finite) if finite else None


def _join_identity(value: Any) -> tuple[str, str]:
    if value is None or isinstance(value, bool):
        raise SourceReconciliationContractError(
            "source_reconciliation_join_key_value_invalid"
        )
    if isinstance(value, (date, datetime)):
        return type(value).__name__, value.isoformat()
    if isinstance(value, str):
        if not value or value != value.strip():
            raise SourceReconciliationContractError(
                "source_reconciliation_join_key_value_invalid"
            )
        return "str", value
    if isinstance(value, (int, float, Decimal)):
        normalized = _finite_decimal(
            value, "source_reconciliation_join_key_value_invalid"
        )
        canonical = Decimal(0) if normalized == 0 else normalized.normalize()
        return "number", str(canonical)
    raise SourceReconciliationContractError(
        "source_reconciliation_join_key_value_invalid"
    )


def _required_name(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SourceReconciliationContractError(error)
    return value


def _string_tuple(value: Any, error: str, *, allow_empty: bool) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SourceReconciliationContractError(error)
    normalized = tuple(_required_name(item, error) for item in value)
    if (not allow_empty and not normalized) or len(normalized) != len(set(normalized)):
        raise SourceReconciliationContractError(error)
    return normalized


def _finite_decimal(value: Any, error: str) -> Decimal:
    if isinstance(value, bool):
        raise SourceReconciliationContractError(error)
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SourceReconciliationContractError(error) from exc
    if not normalized.is_finite():
        raise SourceReconciliationContractError(error)
    return normalized


def _non_negative_decimal(value: Any, error: str) -> Decimal:
    normalized = _finite_decimal(value, error)
    if normalized < 0:
        raise SourceReconciliationContractError(error)
    return normalized


def _bounded_ratio(value: Any, error: str) -> Decimal:
    normalized = _non_negative_decimal(value, error)
    if normalized > 1:
        raise SourceReconciliationContractError(error)
    return normalized


__all__ = (
    "SourceReconciliationContractError",
    "source_reconciliation",
)
