from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_analysis_assets(answer_package: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    admin = _admin_audit(answer_package)
    assets: list[dict[str, Any]] = []
    plan = admin.get("compiler_runtime_plan")
    if isinstance(plan, Mapping) and plan:
        assets.append(
            {
                "asset_type": "compiler_runtime_plan",
                "status": "usable",
                "payload": dict(plan),
            }
        )
    for item in admin.get("contract_gap_diagnostics") or ():
        if isinstance(item, Mapping):
            assets.append(
                {
                    "asset_type": "contract_gap_diagnostic",
                    "status": str(item.get("status") or "unknown"),
                    "payload": dict(item),
                }
            )
    for claim in _summary_claim_groups(answer_package):
        assets.append(
            {
                "asset_type": "verified_claim_slot",
                "status": "usable",
                "text": str(claim.get("text") or ""),
                "evidence_refs": tuple(str(ref) for ref in claim.get("evidence_refs") or ()),
                "strength": claim.get("strength"),
            }
        )
    return tuple(assets)


def _admin_audit(answer_package: Mapping[str, Any]) -> Mapping[str, Any]:
    admin = answer_package.get("admin_audit")
    if isinstance(admin, Mapping):
        return admin
    return _section_payload(answer_package, "admin_audit")


def _summary_claim_groups(answer_package: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    payload = _section_payload(answer_package, "summary")
    groups = payload.get("claim_groups") or ()
    return tuple(item for item in groups if isinstance(item, Mapping))


def _section_payload(answer_package: Mapping[str, Any], section_id: str) -> Mapping[str, Any]:
    for section in answer_package.get("sections") or ():
        if not isinstance(section, Mapping):
            continue
        current_id = section.get("section_id") or section.get("id")
        if current_id == section_id:
            payload = section.get("payload")
            if isinstance(payload, Mapping):
                return payload
            return {}
    return {}
