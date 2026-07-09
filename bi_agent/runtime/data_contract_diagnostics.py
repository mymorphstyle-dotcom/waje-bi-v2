from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


GAP_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "payment_status_contract_missing": ("payment_status", "pay_status", "status"),
    "duplicate_order_contract_missing": ("order_id", "payment_order_id"),
    "high_value_user_contract_missing": (
        "user_id",
        "high_value_amount",
        "high_value_paid_users",
        "value_percentile",
    ),
    "gameplay_contract_missing": ("gameplay_id", "gameplay", "play_mode"),
    "event_context_contract_missing": ("event_id", "event_time", "campaign_id"),
}


def diagnose_contract_gaps(
    *,
    contract_gaps: Iterable[str],
    available_fields: Iterable[str],
    contract_fields: Iterable[str],
    permission_denied_fields: Iterable[str],
    unsupported_grains: Iterable[str],
) -> tuple[dict[str, Any], ...]:
    available = {str(field) for field in available_fields if field}
    contracted = {str(field) for field in contract_fields if field}
    denied = {str(field) for field in permission_denied_fields if field}
    unsupported = {str(field) for field in unsupported_grains if field}
    return tuple(
        _diagnose_gap(
            gap_id=str(gap_id),
            available=available,
            contracted=contracted,
            denied=denied,
            unsupported=unsupported,
        )
        for gap_id in contract_gaps
    )


def contract_fields_from_records(records: Any) -> tuple[str, ...]:
    if isinstance(records, Mapping):
        records = records.values()
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return ()

    names: list[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        for key in ("field", "field_id", "name"):
            value = record.get(key)
            if isinstance(value, str) and value and value not in names:
                names.append(value)
        source_fields = record.get("fields")
        if isinstance(source_fields, Sequence) and not isinstance(
            source_fields, (str, bytes)
        ):
            for value in source_fields:
                if isinstance(value, str) and value and value not in names:
                    names.append(value)
    return tuple(names)


def _diagnose_gap(
    *,
    gap_id: str,
    available: set[str],
    contracted: set[str],
    denied: set[str],
    unsupported: set[str],
) -> dict[str, Any]:
    fields = GAP_FIELD_HINTS.get(gap_id, ())
    present = tuple(field for field in fields if field in available)
    covered = tuple(field for field in fields if field in contracted)
    denied_fields = tuple(field for field in fields if field in denied)
    unsupported_fields = tuple(field for field in fields if field in unsupported)

    if denied_fields:
        return _item(
            gap_id=gap_id,
            status="permission_blocked",
            data_presence="field_present" if present else "field_unknown",
            contract_presence="present" if covered else "missing",
            owner="权限或安全策略 owner",
            repair_path="使用允许的聚合粒度，或由权限 owner 开放对应聚合输出。",
            claim_effect="block_sensitive_detail_claim",
        )
    if unsupported_fields:
        return _item(
            gap_id=gap_id,
            status="unsupported_grain",
            data_presence="field_present" if present else "field_unknown",
            contract_presence="present" if covered else "partial",
            owner="语义合同 owner",
            repair_path="补充该字段支持的聚合粒度、稀疏阈值和可展示范围。",
            claim_effect="degrade_to_supported_grain",
        )
    if present and not covered:
        return _item(
            gap_id=gap_id,
            status="contract_absent",
            data_presence="field_present",
            contract_presence="missing",
            owner="语义合同 owner",
            repair_path="补语义合同，声明口径、粒度、刷新规则和可支持 claim。",
            claim_effect="degrade_claim_strength",
        )
    if present and covered and len(covered) < len(fields):
        return _item(
            gap_id=gap_id,
            status="contract_partial",
            data_presence="field_present",
            contract_presence="partial",
            owner="语义合同 owner",
            repair_path="补齐缺少的合同字段或降级到已覆盖字段。",
            claim_effect="degrade_claim_strength",
        )
    if not present:
        return _item(
            gap_id=gap_id,
            status="data_absent",
            data_presence="field_missing",
            contract_presence="present" if covered else "missing",
            owner="数据工程 owner",
            repair_path="补数据字段或接入对应事实表，再补语义合同绑定。",
            claim_effect="block_dependent_claim",
        )
    return _item(
        gap_id=gap_id,
        status="contract_partial",
        data_presence="field_unknown",
        contract_presence="partial" if covered else "missing",
        owner="运行时 owner",
        repair_path="检查 schema probe、合同注册和权限绑定是否完整。",
        claim_effect="degrade_claim_strength",
    )


def _item(
    *,
    gap_id: str,
    status: str,
    data_presence: str,
    contract_presence: str,
    owner: str,
    repair_path: str,
    claim_effect: str,
) -> dict[str, Any]:
    return {
        "gap_id": gap_id,
        "status": status,
        "data_presence": data_presence,
        "contract_presence": contract_presence,
        "owner": owner,
        "repair_path": repair_path,
        "claim_effect": claim_effect,
    }
