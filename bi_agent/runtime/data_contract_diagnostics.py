from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def diagnose_contract_gaps(
    *,
    contract_gaps: Iterable[str | Mapping[str, Any]],
    available_fields: Iterable[str],
    contract_fields: Iterable[str],
    restricted_output_fields: Iterable[str],
    unsupported_grains: Iterable[str],
) -> tuple[dict[str, Any], ...]:
    available = {str(field) for field in available_fields if field}
    contracted = {str(field) for field in contract_fields if field}
    restricted = {str(field) for field in restricted_output_fields if field}
    unsupported = {str(field) for field in unsupported_grains if field}
    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for gap in contract_gaps:
        gap_id, field_mode, fields = _normalize_gap_descriptor(gap)
        key = (gap_id, field_mode, fields)
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(
            _diagnose_gap(
                gap_id=gap_id,
                field_mode=field_mode,
                fields=fields,
                available=available,
                contracted=contracted,
                restricted=restricted,
                unsupported=unsupported,
            )
        )
    return tuple(diagnostics)


def contract_fields_from_records(records: Any) -> tuple[str, ...]:
    if isinstance(records, Mapping):
        records = records.values()
    if not isinstance(records, Iterable) or isinstance(records, (str, bytes)):
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
        if isinstance(source_fields, Iterable) and not isinstance(
            source_fields, (str, bytes)
        ):
            for value in source_fields:
                if isinstance(value, str) and value and value not in names:
                    names.append(value)
    return tuple(names)


def _diagnose_gap(
    *,
    gap_id: str,
    field_mode: str,
    fields: tuple[str, ...],
    available: set[str],
    contracted: set[str],
    restricted: set[str],
    unsupported: set[str],
) -> dict[str, Any]:
    if not fields:
        return _item(
            gap_id=gap_id,
            status="unknown",
            data_presence="field_unknown",
            contract_presence="unknown",
            owner="运行时 owner",
            repair_path="补充 gap 字段元数据，或检查 schema probe、合同注册和固定输出策略是否完整。",
            claim_effect="degrade_claim_strength",
        )

    present = tuple(field for field in fields if field in available)
    covered = tuple(field for field in fields if field in contracted)
    restricted_fields = tuple(field for field in fields if field in restricted)
    present_restricted_fields = tuple(
        field for field in restricted_fields if field in available
    )
    unsupported_fields = tuple(field for field in fields if field in unsupported)
    if field_mode == "any":
        data_presence = "field_present" if present else "field_missing"
        contract_presence = "present" if covered else "missing"

        if present_restricted_fields:
            return _item(
                gap_id=gap_id,
                status="restricted_output_blocked",
                data_presence=data_presence,
                contract_presence=contract_presence,
                owner="数据输出安全策略 owner",
                repair_path="仅使用合同允许的聚合字段；原始标识符或受限明细不进入业务答案。",
                claim_effect="block_sensitive_detail_claim",
            )
        if unsupported_fields:
            return _item(
                gap_id=gap_id,
                status="unsupported_grain",
                data_presence=data_presence,
                contract_presence=contract_presence,
                owner="语义合同 owner",
                repair_path="补充该字段支持的聚合粒度、稀疏阈值和可展示范围。",
                claim_effect="degrade_to_supported_grain",
            )
        if not present:
            return _item(
                gap_id=gap_id,
                status="data_absent",
                data_presence="field_missing",
                contract_presence=contract_presence,
                owner="数据工程 owner",
                repair_path="补数据字段或接入对应事实表，再补语义合同绑定。",
                claim_effect="block_dependent_claim",
            )
        if not covered:
            return _item(
                gap_id=gap_id,
                status="contract_absent",
                data_presence="field_present",
                contract_presence="missing",
                owner="语义合同 owner",
                repair_path="补语义合同，声明口径、粒度、刷新规则和可支持 claim。",
                claim_effect="degrade_claim_strength",
            )
        return _item(
            gap_id=gap_id,
            status="unknown",
            data_presence="field_present",
            contract_presence="present",
            owner="运行时 owner",
            repair_path="检查 gap 元数据、schema probe、合同注册和固定输出策略是否一致。",
            claim_effect="degrade_claim_strength",
        )

    data_presence = "field_present" if len(present) == len(fields) else "field_missing"
    contract_presence = (
        "present"
        if len(covered) == len(fields)
        else "partial" if covered else "missing"
    )

    if present_restricted_fields:
        return _item(
            gap_id=gap_id,
            status="restricted_output_blocked",
            data_presence=data_presence,
            contract_presence=contract_presence,
            owner="数据输出安全策略 owner",
            repair_path="仅使用合同允许的聚合字段；原始标识符或受限明细不进入业务答案。",
            claim_effect="block_sensitive_detail_claim",
        )
    if unsupported_fields:
        return _item(
            gap_id=gap_id,
            status="unsupported_grain",
            data_presence=data_presence,
            contract_presence=contract_presence,
            owner="语义合同 owner",
            repair_path="补充该字段支持的聚合粒度、稀疏阈值和可展示范围。",
            claim_effect="degrade_to_supported_grain",
        )
    if len(present) < len(fields):
        return _item(
            gap_id=gap_id,
            status="data_absent",
            data_presence="field_missing",
            contract_presence=contract_presence,
            owner="数据工程 owner",
            repair_path="补数据字段或接入对应事实表，再补语义合同绑定。",
            claim_effect="block_dependent_claim",
        )
    if not covered:
        return _item(
            gap_id=gap_id,
            status="contract_absent",
            data_presence="field_present",
            contract_presence="missing",
            owner="语义合同 owner",
            repair_path="补语义合同，声明口径、粒度、刷新规则和可支持 claim。",
            claim_effect="degrade_claim_strength",
        )
    if len(covered) < len(fields):
        return _item(
            gap_id=gap_id,
            status="contract_partial",
            data_presence="field_present",
            contract_presence="partial",
            owner="语义合同 owner",
            repair_path="补齐缺少的合同字段或降级到已覆盖字段。",
            claim_effect="degrade_claim_strength",
        )
    return _item(
        gap_id=gap_id,
        status="unknown",
        data_presence="field_present",
        contract_presence="present",
        owner="运行时 owner",
        repair_path="检查 gap 元数据、schema probe、合同注册和固定输出策略是否一致。",
        claim_effect="degrade_claim_strength",
    )


def _normalize_gap_descriptor(
    gap: str | Mapping[str, Any],
) -> tuple[str, str, tuple[str, ...]]:
    if isinstance(gap, Mapping):
        gap_id = str(gap.get("gap_id") or "").strip() or str(gap)
        required_fields = _normalize_fields(gap.get("required_fields"))
        if required_fields:
            return gap_id, "all", required_fields
        fields = _normalize_fields(gap.get("fields"))
        if fields:
            return gap_id, "any", fields
        return gap_id, "unknown", ()
    return str(gap), "unknown", ()


def _normalize_fields(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    fields: list[str] = []
    for item in value:
        field = str(item).strip()
        if field and field not in fields:
            fields.append(field)
    return tuple(fields)


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
