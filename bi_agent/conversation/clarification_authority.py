from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    analysis_contract_from_dict,
    analysis_contract_signature,
    stable_contract_signature,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_value


_MATERIAL_AUTHORITY_KEYS = frozenset(
    {
        "schema_version",
        "source_run_id",
        "thread_id",
        "topic_id",
        "intent_material",
        "route_material_slots",
        "route_control",
        "material_authority_signature",
    }
)
_ROUTE_CONTROL_KEYS = frozenset({"obligation_rejection_history"})
_OBLIGATION_REJECTION_KEYS = frozenset({"action", "capability", "reason"})
_LOCAL_OBLIGATION_REJECTION_REASONS = frozenset(
    {
        "diagnostic_question_family_incompatible",
        "unknown_diagnostic_rejected",
    }
)
_INTENT_MATERIAL_KEYS = frozenset(
    {
        "primary_question_family",
        "question_families",
        "primary_target_metric",
        "target_metrics",
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
        "scope",
    }
)
_ROUTE_MATERIAL_KEYS = frozenset(
    {
        "target_metrics",
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
        "diagnostic_tags",
        "scope",
    }
)
_MATERIAL_LIST_AXES = (
    "target_metrics",
    "requested_components",
    "requested_dimensions",
    "baselines",
    "context_sources",
    "claim_intents",
)


def build_material_authority(
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    original_intent: Mapping[str, Any],
    material_slots: Mapping[str, Any],
    obligation_rejection_history: Any = (),
) -> dict[str, Any]:
    if not isinstance(original_intent, Mapping):
        raise EvidenceIntegrityError("material_authority_original_intent_invalid")
    if not isinstance(material_slots, Mapping):
        raise EvidenceIntegrityError("material_authority_route_invalid")
    route_material = _route_material_projection(material_slots)
    intent_material = _intent_material_projection(
        original_intent,
        route_material=route_material,
    )
    body = {
        "schema_version": "1",
        "source_run_id": _required(source_run_id, "source_run_id"),
        "thread_id": _required(thread_id, "thread_id"),
        "topic_id": _required(topic_id, "topic_id"),
        "intent_material": intent_material,
        "route_material_slots": route_material,
        "route_control": _route_control_projection(
            obligation_rejection_history
        ),
    }
    return {
        **body,
        "material_authority_signature": stable_contract_signature(body),
    }


def validate_material_authority(
    value: Mapping[str, Any],
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _MATERIAL_AUTHORITY_KEYS:
        raise EvidenceIntegrityError("material_authority_shape_invalid")
    if str(value.get("schema_version") or "") != "1":
        raise EvidenceIntegrityError("material_authority_version_invalid")
    if (
        str(value.get("source_run_id") or "") != source_run_id
        or str(value.get("thread_id") or "") != thread_id
        or str(value.get("topic_id") or "") != topic_id
    ):
        raise EvidenceIntegrityError("material_authority_owner_mismatch")
    intent_material = value.get("intent_material")
    if (
        not isinstance(intent_material, Mapping)
        or set(intent_material) != _INTENT_MATERIAL_KEYS
    ):
        raise EvidenceIntegrityError("material_authority_intent_shape_invalid")
    route_material = value.get("route_material_slots")
    if (
        not isinstance(route_material, Mapping)
        or set(route_material) != _ROUTE_MATERIAL_KEYS
    ):
        raise EvidenceIntegrityError("material_authority_route_shape_invalid")
    route_control = value.get("route_control")
    if (
        not isinstance(route_control, Mapping)
        or set(route_control) != _ROUTE_CONTROL_KEYS
    ):
        raise EvidenceIntegrityError(
            "material_authority_route_control_shape_invalid"
        )
    _validate_intent_material(intent_material)
    _validate_route_material(route_material)
    _route_control_projection(
        route_control.get("obligation_rejection_history")
    )
    signature = str(value.get("material_authority_signature") or "")
    body = {
        key: canonical_value(item)
        for key, item in value.items()
        if key != "material_authority_signature"
    }
    if not signature or signature != stable_contract_signature(body):
        raise EvidenceIntegrityError("material_authority_signature_invalid")
    return {**body, "material_authority_signature": signature}


def validate_material_authority_contract_overlap(
    material_authority: Mapping[str, Any],
    analysis_contract: AnalysisContract,
) -> None:
    intent_material = material_authority.get("intent_material")
    route_material = material_authority.get("route_material_slots")
    if not isinstance(intent_material, Mapping) or not isinstance(
        route_material, Mapping
    ):
        raise EvidenceIntegrityError("material_authority_shape_invalid")
    if tuple(intent_material.get("question_families") or ()) != tuple(
        analysis_contract.question_families
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_question_families_mismatch"
        )
    contract_target_metrics = _contract_target_metric_ids(analysis_contract)
    if any(
        tuple(targets or ()) != contract_target_metrics
        for targets in (
            intent_material.get("target_metrics"),
            route_material.get("target_metrics"),
        )
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_target_metrics_mismatch"
        )
    contract_scope = _contract_material_scope(analysis_contract.scope)
    if any(
        _material_scope(scope) != contract_scope
        for scope in (
            intent_material.get("scope"),
            route_material.get("scope"),
        )
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_scope_mismatch"
        )


def validate_terminal_resume_proposal_overlap(
    material_authority: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> None:
    intent_material = material_authority.get("intent_material")
    if not isinstance(intent_material, Mapping):
        raise EvidenceIntegrityError("material_authority_shape_invalid")
    raw_families = proposal.get("question_families")
    if raw_families is None and proposal.get("question_family"):
        raw_families = proposal.get("question_family")
    if _proposal_axis_values(raw_families) != tuple(
        intent_material.get("question_families") or ()
    ):
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_question_families_mismatch"
        )
    raw_targets = proposal.get("target_metrics")
    if raw_targets is None and proposal.get("target_metric"):
        raw_targets = proposal.get("target_metric")
    if _proposal_axis_values(raw_targets) != tuple(
        intent_material.get("target_metrics") or ()
    ):
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_target_metrics_mismatch"
        )
    if _material_scope(proposal.get("scope")) != _material_scope(
        intent_material.get("scope")
    ):
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_scope_mismatch"
        )


def validate_terminal_clarification_choice_overlap(
    material_authority: Mapping[str, Any],
    choice: Mapping[str, Any],
) -> None:
    intent_material = material_authority.get("intent_material")
    if not isinstance(intent_material, Mapping):
        raise EvidenceIntegrityError("material_authority_shape_invalid")
    signed_families = tuple(intent_material.get("question_families") or ())
    signed_primary_family = str(
        intent_material.get("primary_question_family") or ""
    )
    family_mismatch = (
        "terminal_resume_proposal_question_families_mismatch"
    )
    if (
        "question_families" in choice
        and _proposal_axis_values(choice.get("question_families"))
        != signed_families
    ):
        raise EvidenceIntegrityError(family_mismatch)
    for alias in ("question_family", "primary_question_family"):
        if alias in choice and choice.get(alias) != signed_primary_family:
            raise EvidenceIntegrityError(family_mismatch)
    if (
        "secondary_question_families" in choice
        and _proposal_axis_values(choice.get("secondary_question_families"))
        != signed_families[1:]
    ):
        raise EvidenceIntegrityError(family_mismatch)

    signed_targets = tuple(intent_material.get("target_metrics") or ())
    signed_primary_target = str(
        intent_material.get("primary_target_metric") or ""
    )
    target_mismatch = "terminal_resume_proposal_target_metrics_mismatch"
    if (
        "target_metrics" in choice
        and _proposal_axis_values(choice.get("target_metrics"))
        != signed_targets
    ):
        raise EvidenceIntegrityError(target_mismatch)
    if (
        "target_metric" in choice
        and choice.get("target_metric") != signed_primary_target
    ):
        raise EvidenceIntegrityError(target_mismatch)

    if "scope" in choice:
        raw_scope = choice.get("scope")
        if raw_scope in (None, "", (), [], {}):
            raise EvidenceIntegrityError(
                "terminal_resume_proposal_scope_mismatch"
            )
        try:
            scope_matches = _material_scope(raw_scope) == _material_scope(
                intent_material.get("scope")
            )
        except EvidenceIntegrityError as exc:
            raise EvidenceIntegrityError(
                "terminal_resume_proposal_scope_mismatch"
            ) from exc
        if not scope_matches:
            raise EvidenceIntegrityError(
                "terminal_resume_proposal_scope_mismatch"
            )


def _proposal_axis_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        values = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, Mapping):
        values = value
    else:
        values = ()
    return tuple(
        dict.fromkeys(str(item).strip() for item in values if str(item).strip())
    )


def _contract_target_metric_ids(
    analysis_contract: AnalysisContract,
) -> tuple[str, ...]:
    metric_ids: list[str] = []
    for contract_ref in analysis_contract.target_metric_refs:
        matches = tuple(
            dict.fromkeys(
                binding.metric_id
                for binding in analysis_contract.metric_bindings
                if binding.contract_ref == contract_ref
            )
        )
        if len(matches) != 1:
            raise EvidenceIntegrityError(
                "material_authority_contract_target_metrics_unresolvable"
            )
        if matches[0] not in metric_ids:
            metric_ids.append(matches[0])
    if not metric_ids:
        raise EvidenceIntegrityError(
            "material_authority_contract_target_metrics_unresolvable"
        )
    return tuple(metric_ids)


def _contract_material_scope(scope: Mapping[str, Any]) -> Any:
    return _material_scope(
        {
            str(key): value
            for key, value in scope.items()
            if key
            not in {"requested_metric_ids", "requested_dimension_ids"}
            and value not in (None, "", {}, [])
        }
    )


def _material_scope(scope: Any) -> Any:
    if scope in (None, "", {}, []):
        return {"type": "full_sample"}
    if isinstance(scope, str):
        return {"type": scope}
    if isinstance(scope, Mapping):
        return canonical_value(scope)
    raise EvidenceIntegrityError("material_authority_contract_scope_mismatch")


def _route_control_projection(value: Any) -> dict[str, Any]:
    raw = [] if value is None else value
    if not isinstance(raw, (list, tuple)):
        raise EvidenceIntegrityError(
            "material_authority_rejection_history_invalid"
        )
    records: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != _OBLIGATION_REJECTION_KEYS:
            raise EvidenceIntegrityError(
                "material_authority_rejection_history_invalid"
            )
        action = item.get("action")
        capability = item.get("capability")
        reason = item.get("reason")
        if (
            not all(
                isinstance(field, str)
                and field
                and field == field.strip()
                for field in (action, capability, reason)
            )
            or action != "rejected"
            or reason not in _LOCAL_OBLIGATION_REJECTION_REASONS
        ):
            raise EvidenceIntegrityError(
                "material_authority_rejection_history_invalid"
            )
        record_key = (action, capability, reason)
        if record_key in seen:
            raise EvidenceIntegrityError(
                "material_authority_rejection_history_invalid"
            )
        seen.add(record_key)
        records.append(
            {
                "action": action,
                "capability": capability,
                "reason": reason,
            }
        )
    return {"obligation_rejection_history": records}


def _intent_material_projection(
    original: Mapping[str, Any],
    *,
    route_material: Mapping[str, Any],
) -> dict[str, Any]:
    raw_families = original.get("question_families")
    families = _string_sequence(
        raw_families,
        reason="material_authority_question_families_invalid",
        required=True,
    )
    primary_family = str(original.get("primary_question_family") or "")
    question_family = str(original.get("question_family") or "")
    secondary = _string_sequence(
        original.get("secondary_question_families"),
        reason="material_authority_question_families_invalid",
    )
    if (
        not primary_family
        or primary_family != question_family
        or primary_family != families[0]
        or secondary != families[1:]
    ):
        raise EvidenceIntegrityError("material_authority_question_families_invalid")
    target_metrics = list(route_material["target_metrics"])
    primary_target = str(original.get("target_metric") or "")
    if not primary_target or primary_target != target_metrics[0]:
        raise EvidenceIntegrityError("material_authority_target_metrics_invalid")
    return {
        "primary_question_family": primary_family,
        "question_families": families,
        "primary_target_metric": primary_target,
        "target_metrics": target_metrics,
        "requested_components": _string_sequence(
            original.get("requested_components"),
            reason="material_authority_requested_components_invalid",
        ),
        "requested_dimensions": _string_sequence(
            original.get("requested_dimensions"),
            reason="material_authority_requested_dimensions_invalid",
        ),
        "baselines": _baseline_sequence(original.get("baseline_candidates")),
        "context_sources": _string_sequence(
            original.get("context_sources"),
            reason="material_authority_context_sources_invalid",
        ),
        "claim_intents": _string_sequence(
            original.get("claim_intents"),
            reason="material_authority_claim_intents_invalid",
        ),
        "scope": _canonical_scope(original.get("scope")),
    }


def _route_material_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(value) - _ROUTE_MATERIAL_KEYS
    if unknown:
        raise EvidenceIntegrityError("material_authority_route_shape_invalid")
    projected = {
        axis: _string_sequence(
            value.get(axis),
            reason=f"material_authority_{axis}_invalid",
            required=axis == "target_metrics",
        )
        for axis in _MATERIAL_LIST_AXES
    }
    projected["diagnostic_tags"] = _string_sequence(
        value.get("diagnostic_tags"),
        reason="material_authority_diagnostic_tags_invalid",
    )
    projected["scope"] = _canonical_scope(value.get("scope"))
    return projected


def _validate_intent_material(value: Mapping[str, Any]) -> None:
    families = _string_sequence(
        value.get("question_families"),
        reason="material_authority_question_families_invalid",
        required=True,
    )
    primary_family = str(value.get("primary_question_family") or "")
    if not primary_family or primary_family != families[0]:
        raise EvidenceIntegrityError("material_authority_question_families_invalid")
    targets = _string_sequence(
        value.get("target_metrics"),
        reason="material_authority_target_metrics_invalid",
        required=True,
    )
    if str(value.get("primary_target_metric") or "") != targets[0]:
        raise EvidenceIntegrityError("material_authority_target_metrics_invalid")
    for axis in (
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
    ):
        _string_sequence(
            value.get(axis),
            reason=f"material_authority_{axis}_invalid",
        )
    _canonical_scope(value.get("scope"))


def _validate_route_material(value: Mapping[str, Any]) -> None:
    for axis in _MATERIAL_LIST_AXES:
        _string_sequence(
            value.get(axis),
            reason=f"material_authority_{axis}_invalid",
            required=axis == "target_metrics",
        )
    _string_sequence(
        value.get("diagnostic_tags"),
        reason="material_authority_diagnostic_tags_invalid",
    )
    _canonical_scope(value.get("scope"))


def _string_sequence(
    value: Any,
    *,
    reason: str,
    required: bool = False,
) -> list[str]:
    raw = [] if value is None else value
    if (
        not isinstance(raw, (list, tuple))
        or any(not isinstance(item, str) or not item for item in raw)
        or len(raw) != len(set(raw))
        or (required and not raw)
    ):
        raise EvidenceIntegrityError(reason)
    return list(raw)


def _baseline_sequence(value: Any) -> list[str]:
    raw = [] if value is None else value
    if not isinstance(raw, (list, tuple)):
        raise EvidenceIntegrityError("material_authority_baselines_invalid")
    baselines: list[str] = []
    for item in raw:
        baseline = item if isinstance(item, str) else next(
            (
                item.get(key)
                for key in ("baseline_id", "id", "value")
                if isinstance(item, Mapping)
                and isinstance(item.get(key), str)
                and item.get(key)
            ),
            "",
        )
        if not baseline or baseline in baselines:
            if baseline in baselines:
                continue
            raise EvidenceIntegrityError("material_authority_baselines_invalid")
        baselines.append(baseline)
    return baselines


def _canonical_scope(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, (str, Mapping)):
        raise EvidenceIntegrityError("material_authority_scope_invalid")
    return canonical_value(value)


def build_clarification_outcome(
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    choice: Mapping[str, Any],
) -> dict[str, Any]:
    body = {
        "source_run_id": _required(source_run_id, "source_run_id"),
        "thread_id": _required(thread_id, "thread_id"),
        "topic_id": _required(topic_id, "topic_id"),
        "choice": canonical_value(dict(choice)),
    }
    digest = stable_contract_signature(body)
    payload = {
        "outcome_ref": f"clarification-outcome:{digest}",
        **body,
    }
    payload["outcome_signature"] = stable_contract_signature(payload)
    return payload


def validate_clarification_resume_authority(
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    choice: Mapping[str, Any],
    outcome_ref: str,
    analysis_contract: Mapping[str, Any],
    stored_contract_signature: str,
    analysis_run_id: str,
    run_status: str,
    run_thread_id: str,
    run_topic_id: str,
    clarification_outcome: Mapping[str, Any],
    outcome_run_id: str,
    outcome_thread_id: str,
    outcome_topic_id: str,
    material_authority: Mapping[str, Any],
) -> dict[str, Any]:
    if analysis_run_id != source_run_id:
        raise EvidenceIntegrityError("clarification_resume_source_run_mismatch")
    if run_status != "waiting_for_clarification":
        raise EvidenceIntegrityError("clarification_resume_source_run_stale")
    owners = (
        run_thread_id,
        run_topic_id,
        outcome_thread_id,
        outcome_topic_id,
    )
    if owners != (thread_id, topic_id, thread_id, topic_id):
        raise EvidenceIntegrityError("clarification_resume_owner_mismatch")
    if outcome_run_id != source_run_id:
        raise EvidenceIntegrityError("clarification_resume_outcome_run_mismatch")
    if not isinstance(material_authority, Mapping):
        raise EvidenceIntegrityError("material_authority_missing")
    validated_material_authority = validate_material_authority(
        material_authority,
        source_run_id=source_run_id,
        thread_id=thread_id,
        topic_id=topic_id,
    )
    contract_payload = dict(analysis_contract)
    embedded_signature = str(contract_payload.pop("contract_signature", "") or "")
    try:
        typed_contract = analysis_contract_from_dict(contract_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("clarification_resume_contract_payload_invalid") from exc
    expected_signature = analysis_contract_signature(typed_contract)
    if (
        not stored_contract_signature
        or expected_signature != stored_contract_signature
        or (embedded_signature and embedded_signature != stored_contract_signature)
    ):
        raise EvidenceIntegrityError("clarification_resume_contract_signature_invalid")
    if typed_contract.analysis_contract_id != f"analysis:{source_run_id}:1":
        raise EvidenceIntegrityError("clarification_resume_contract_run_mismatch")
    validate_material_authority_contract_overlap(
        validated_material_authority,
        typed_contract,
    )

    outcome = dict(clarification_outcome)
    signature = str(outcome.pop("outcome_signature", "") or "")
    if not signature or stable_contract_signature(outcome) != signature:
        raise EvidenceIntegrityError("clarification_resume_outcome_signature_invalid")
    if str(outcome.get("outcome_ref") or "") != outcome_ref:
        raise EvidenceIntegrityError("clarification_resume_outcome_ref_mismatch")
    expected_outcome_ref = "clarification-outcome:" + stable_contract_signature(
        {
            "source_run_id": source_run_id,
            "thread_id": thread_id,
            "topic_id": topic_id,
            "choice": canonical_value(dict(choice)),
        }
    )
    if outcome_ref != expected_outcome_ref:
        raise EvidenceIntegrityError("clarification_resume_outcome_ref_invalid")
    if (
        str(outcome.get("source_run_id") or "") != source_run_id
        or str(outcome.get("thread_id") or "") != thread_id
        or str(outcome.get("topic_id") or "") != topic_id
    ):
        raise EvidenceIntegrityError("clarification_resume_outcome_owner_mismatch")
    if canonical_value(outcome.get("choice")) != canonical_value(dict(choice)):
        raise EvidenceIntegrityError("clarification_resume_choice_mismatch")
    outcome["outcome_signature"] = signature
    return {
        "source_run_id": source_run_id,
        "thread_id": thread_id,
        "topic_id": topic_id,
        "analysis_contract": typed_contract.to_dict(),
        "analysis_contract_signature": stored_contract_signature,
        "material_authority": validated_material_authority,
        "clarification_outcome": outcome,
    }


def _required(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise EvidenceIntegrityError(f"clarification_outcome_{field}_missing")
    return normalized
