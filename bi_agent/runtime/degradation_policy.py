from __future__ import annotations

from collections.abc import Mapping
from typing import Any


NON_BLOCKING_DEGRADATION_ACTIONS = frozenset(
    {
        "context_only",
        "degrade_claim",
        "omit_optional_component",
        "omit_path",
        "report_contract_gap",
        "report_limitation",
        "sensitivity_only",
    }
)
BLOCKING_DEGRADATION_ACTIONS = frozenset(
    {
        "block_candidate_impact",
        "block_claim",
        "block_reduced_claim",
        "block_unverified_claim",
    }
)
KNOWN_DEGRADATION_ACTIONS = (
    NON_BLOCKING_DEGRADATION_ACTIONS | BLOCKING_DEGRADATION_ACTIONS
)

_DEGRADABLE_REQUIRED_SLOT_MATCH_FAILURES = frozenset(
    {
        "primary_provenance_mismatch",
        "completeness_not_accepted",
        "primary_report_not_ready",
        "primary_snapshot_provenance_mismatch",
        "empty_primary_result",
        "primary_row_count_mismatch",
        "required_fields_missing",
        "required_windows_missing",
        "required_window_provenance_mismatch",
        "required_window_rows_mismatch",
        "missing_validation_query",
        "missing_validation_report",
        "validation_report_not_ready",
        "validation_provenance_mismatch",
    }
)
_REQUIRED_SLOT_MATCH_FAILURES_WITH_DETAILS = frozenset(
    {
        "required_fields_missing",
        "required_windows_missing",
    }
)


def degradation_action_is_non_blocking(action: Any) -> bool:
    return str(action or "") in NON_BLOCKING_DEGRADATION_ACTIONS


def degraded_binding_projection_is_authorized(
    plan: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> bool:
    """Validate an at-least-one binding without reclassifying data readiness."""

    if str(binding.get("status") or "") != "degraded":
        return False
    required_slots = tuple(plan.get("required_input_slots") or ())
    optional_slots = tuple(plan.get("optional_input_slots") or ())
    if not required_slots or any(
        not isinstance(item, Mapping) for item in required_slots
    ):
        return False
    if any(not isinstance(item, Mapping) for item in optional_slots):
        return False

    available_query_refs = {
        str(ref) for ref in binding.get("query_contract_refs") or () if ref
    }
    available_validation_refs = {
        str(ref)
        for ref in binding.get("validation_query_contract_refs") or ()
        if ref
    }

    def slot_ready(slot: Mapping[str, Any]) -> bool:
        query_refs = tuple(
            str(ref) for ref in slot.get("query_contract_refs") or () if ref
        )
        validation_refs = tuple(
            str(ref)
            for ref in slot.get("validation_query_contract_refs") or ()
            if ref
        )
        return bool(
            len(query_refs) == 1
            and query_refs[0] in available_query_refs
            and all(ref in available_validation_refs for ref in validation_refs)
        )

    required_mode = str(
        (plan.get("minimum_readiness") or {}).get("required_slots") or ""
    )
    required_readiness = tuple(slot_ready(slot) for slot in required_slots)
    minimum_ready = (
        all(required_readiness)
        if required_mode == "all"
        else any(required_readiness)
        if required_mode == "at_least_one"
        else False
    )
    if not minimum_ready:
        return False

    required_slot_ids = {
        str(slot.get("slot_id") or "")
        for slot in required_slots
        if slot.get("slot_id")
    }
    optional_slot_ids = {
        str(slot.get("slot_id") or "")
        for slot in optional_slots
        if slot.get("slot_id")
    }
    reasons = tuple(str(item) for item in binding.get("reasons") or () if item)
    if not reasons:
        return False
    degradation_policy = plan.get("degradation_policy") or {}
    for reason in reasons:
        if reason.startswith("missing_optional_slot:"):
            slot_id = reason.split(":", 1)[1]
            if (
                slot_id not in optional_slot_ids
                or not degradation_action_is_non_blocking(
                    degradation_policy.get("missing_optional_input")
                )
            ):
                return False
            continue
        if reason.startswith("missing_required_slot:"):
            slot_id = reason.split(":", 1)[1]
            if (
                required_mode != "at_least_one"
                or slot_id not in required_slot_ids
                or not degradation_action_is_non_blocking(
                    degradation_policy.get("missing_required_input")
                )
            ):
                return False
            continue
        slot_id = _declared_required_slot_for_match_failure(
            reason,
            required_slot_ids,
        )
        if (
            not slot_id
            or required_mode != "at_least_one"
            or not degradation_action_is_non_blocking(
                degradation_policy.get("incomplete_input")
            )
        ):
            return False
    return True


def _declared_required_slot_for_match_failure(
    reason: str,
    required_slot_ids: set[str],
) -> str:
    failure_type, separator, payload = reason.partition(":")
    if (
        not separator
        or failure_type not in _DEGRADABLE_REQUIRED_SLOT_MATCH_FAILURES
    ):
        return ""
    if failure_type not in _REQUIRED_SLOT_MATCH_FAILURES_WITH_DETAILS:
        return payload if payload in required_slot_ids else ""
    return next(
        (
            slot_id
            for slot_id in sorted(required_slot_ids, key=len, reverse=True)
            if payload.startswith(f"{slot_id}:")
        ),
        "",
    )
