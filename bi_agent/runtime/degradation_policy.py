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

_DEGRADABLE_FAILURE_CLASSES = frozenset({"availability", "boundary", "technical"})
_BINDING_ISSUE_FIELDS = frozenset(
    {
        "code",
        "failure_class",
        "input_state",
        "slot_id",
        "slot_role",
        "diagnostic",
    }
)
_DEGRADABLE_ISSUE_CONTRACTS = {
    "slot_input_missing": frozenset({("availability", "missing")}),
    "query_execution_failed": frozenset({("technical", "incomplete")}),
    "completeness_not_accepted": frozenset(
        {
            ("availability", "incomplete"),
            ("boundary", "incomplete"),
            ("technical", "incomplete"),
        }
    ),
    "primary_report_not_ready": frozenset(
        {
            ("availability", "incomplete"),
            ("boundary", "incomplete"),
            ("technical", "incomplete"),
        }
    ),
    "empty_primary_result": frozenset({("availability", "incomplete")}),
    "required_windows_missing": frozenset({("availability", "incomplete")}),
    "validation_report_not_ready": frozenset(
        {
            ("availability", "incomplete"),
            ("boundary", "incomplete"),
            ("technical", "incomplete"),
        }
    ),
    "accepted_incomplete_input": frozenset({("boundary", "incomplete")}),
}


def degradation_action_is_non_blocking(action: Any) -> bool:
    return str(action or "") in NON_BLOCKING_DEGRADATION_ACTIONS


def ready_binding_projection_is_authorized(
    plan: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> bool:
    if str(binding.get("status") or "") != "ready":
        return False
    required_slots = tuple(plan.get("required_input_slots") or ())
    optional_slots = tuple(plan.get("optional_input_slots") or ())
    slots = (*required_slots, *optional_slots)
    if any(not isinstance(item, Mapping) for item in slots):
        return False
    expected_refs = _declared_ref_sets(slots)
    available_query_refs = _binding_ref_set(binding, "query_contract_refs")
    available_validation_refs = _binding_ref_set(
        binding,
        "validation_query_contract_refs",
    )
    if (
        expected_refs is None
        or available_query_refs is None
        or available_validation_refs is None
        or available_query_refs != expected_refs[0]
        or available_validation_refs != expected_refs[1]
    ):
        return False
    if "issues" not in binding or "reasons" not in binding:
        return False
    if binding.get("issues") or binding.get("reasons"):
        return False
    return all(
        _slot_ready(
            slot,
            available_query_refs=available_query_refs,
            available_validation_refs=available_validation_refs,
        )
        for slot in slots
    )


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

    slots = (*required_slots, *optional_slots)
    expected_refs = _declared_ref_sets(slots)
    available_query_refs = _binding_ref_set(binding, "query_contract_refs")
    available_validation_refs = _binding_ref_set(
        binding,
        "validation_query_contract_refs",
    )
    if (
        expected_refs is None
        or available_query_refs is None
        or available_validation_refs is None
        or not available_query_refs.issubset(expected_refs[0])
        or not available_validation_refs.issubset(expected_refs[1])
    ):
        return False
    if "issues" not in binding or "reasons" not in binding:
        return False

    def slot_ready(slot: Mapping[str, Any]) -> bool:
        return _slot_ready(
            slot,
            available_query_refs=available_query_refs,
            available_validation_refs=available_validation_refs,
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
        str(slot.get("slot_id") or "") for slot in required_slots if slot.get("slot_id")
    }
    optional_slot_ids = {
        str(slot.get("slot_id") or "") for slot in optional_slots if slot.get("slot_id")
    }
    issues = tuple(binding.get("issues") or ())
    if not issues or any(not isinstance(item, Mapping) for item in issues):
        return False
    degradation_policy = plan.get("degradation_policy") or {}
    seen_issue_slots: set[str] = set()
    for issue in issues:
        if set(issue) != _BINDING_ISSUE_FIELDS:
            return False
        code = issue.get("code")
        failure_class = issue.get("failure_class")
        input_state = issue.get("input_state")
        slot_id = issue.get("slot_id")
        slot_role = issue.get("slot_role")
        diagnostic = issue.get("diagnostic")
        if any(
            not isinstance(value, str) or not value
            for value in (
                code,
                failure_class,
                input_state,
                slot_id,
                slot_role,
                diagnostic,
            )
        ):
            return False
        if (
            failure_class,
            input_state,
        ) not in _DEGRADABLE_ISSUE_CONTRACTS.get(code, frozenset()):
            return False
        if slot_id in seen_issue_slots:
            return False
        seen_issue_slots.add(slot_id)
        if failure_class not in _DEGRADABLE_FAILURE_CLASSES:
            return False
        if input_state not in {"missing", "incomplete"}:
            return False
        if input_state == "missing" and failure_class != "availability":
            return False
        if failure_class in {"boundary", "technical"} and input_state != "incomplete":
            return False
        if slot_role == "optional":
            if slot_id not in optional_slot_ids:
                return False
            declared_slot = next(
                slot
                for slot in optional_slots
                if str(slot.get("slot_id") or "") == slot_id
            )
            if input_state == "missing":
                if slot_ready(declared_slot):
                    return False
                action = degradation_policy.get("missing_optional_input")
            elif input_state == "incomplete":
                action = degradation_policy.get("incomplete_input")
            else:
                return False
            if not degradation_action_is_non_blocking(action):
                return False
            continue
        if slot_role == "required":
            if slot_id not in required_slot_ids:
                return False
            declared_slot = next(
                slot
                for slot in required_slots
                if str(slot.get("slot_id") or "") == slot_id
            )
            if input_state == "missing":
                if slot_ready(declared_slot):
                    return False
                if required_mode != "at_least_one":
                    return False
                action = degradation_policy.get("missing_required_input")
            elif input_state == "incomplete":
                if (
                    not slot_ready(
                        next(
                            slot
                            for slot in required_slots
                            if str(slot.get("slot_id") or "") == slot_id
                        )
                    )
                    and required_mode != "at_least_one"
                ):
                    return False
                action = degradation_policy.get("incomplete_input")
            else:
                return False
            if not degradation_action_is_non_blocking(action):
                return False
            continue
        return False
    unready_slot_ids = {
        str(slot.get("slot_id") or "")
        for slot in (*required_slots, *optional_slots)
        if not slot_ready(slot)
    }
    if not unready_slot_ids.issubset(seen_issue_slots):
        return False
    return True


def _slot_ready(
    slot: Mapping[str, Any],
    *,
    available_query_refs: set[str],
    available_validation_refs: set[str],
) -> bool:
    query_refs = tuple(str(ref) for ref in slot.get("query_contract_refs") or () if ref)
    validation_refs = tuple(
        str(ref) for ref in slot.get("validation_query_contract_refs") or () if ref
    )
    return bool(
        len(query_refs) == 1
        and query_refs[0] in available_query_refs
        and all(ref in available_validation_refs for ref in validation_refs)
    )


def _declared_ref_sets(
    slots: tuple[Mapping[str, Any], ...],
) -> tuple[set[str], set[str]] | None:
    query_refs: set[str] = set()
    validation_refs: set[str] = set()
    for slot in slots:
        slot_query_refs = _declared_slot_refs(slot, "query_contract_refs")
        slot_validation_refs = _declared_slot_refs(
            slot,
            "validation_query_contract_refs",
        )
        if slot_query_refs is None or slot_validation_refs is None:
            return None
        query_refs.update(slot_query_refs)
        validation_refs.update(slot_validation_refs)
    return query_refs, validation_refs


def _declared_slot_refs(
    slot: Mapping[str, Any],
    key: str,
) -> tuple[str, ...] | None:
    raw = slot.get(key, ())
    if not isinstance(raw, (list, tuple)):
        return None
    refs = tuple(raw)
    if any(not isinstance(ref, str) or not ref for ref in refs):
        return None
    return refs


def _binding_ref_set(
    binding: Mapping[str, Any],
    key: str,
) -> set[str] | None:
    if key not in binding:
        return None
    raw = binding[key]
    if not isinstance(raw, (list, tuple)):
        return None
    refs = tuple(raw)
    if any(not isinstance(ref, str) or not ref for ref in refs) or len(refs) != len(
        set(refs)
    ):
        return None
    return set(refs)
