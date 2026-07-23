from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.temporal_comparison import (
    TemporalComparisonContractError,
    validate_comparison_slot_binding,
    validate_comparison_spec,
    validate_time_spec,
)


class SingleAuthorityContractError(ValueError):
    pass


DIRECTION_PREMISES = frozenset(
    {
        "user_hypothesis_positive",
        "user_hypothesis_negative",
        "unknown",
        "no_direction_requested",
    }
)
DECISION_SOURCES = frozenset(
    {"user", "accepted_recommendation", "safe_inference", "inherited", "system"}
)
DECISION_STATUSES = frozenset(
    {"unresolved", "inferred", "user_confirmed", "invalidated"}
)
DECISION_MATERIALITIES = frozenset({"material", "non_material"})
FAILURE_LAYERS = frozenset(
    {
        "intent",
        "plan",
        "query",
        "capability",
        "evidence",
        "claim",
        "narrative",
        "persistence",
        "delivery",
    }
)
FAILURE_SCOPES = frozenset(
    {"run", "plan_revision", "task", "claim", "narrative_block", "delivery"}
)
FAILURE_INTEGRITY_LEVELS = frozenset({"none", "local", "shared", "critical"})
FAILURE_RETRYABILITY = frozenset(
    {"not_retryable", "retryable", "retry_after_user_action"}
)
EXECUTION_STATES = frozenset(
    {"pending", "running", "waiting", "complete", "cancelled", "failed", "superseded"}
)
INTERACTION_STATES = frozenset({"active", "waiting_for_user", "closed", "superseded"})
EVIDENCE_STATES = frozenset(
    {"not_started", "partial", "complete", "boundary_only", "integrity_failed"}
)
PUBLICATION_STATES = frozenset(
    {"not_ready", "composing", "ready", "published"}
)
DELIVERY_STATES = frozenset(
    {"pending", "persisted", "published", "retryable_failed", "permanently_failed"}
)
RETRY_STATES = frozenset({"idle", "scheduled", "running", "exhausted", "succeeded"})
CANCELLATION_STATES = frozenset({"active", "requested", "cancelled"})
SUPERSESSION_STATES = frozenset({"active", "superseded"})
TRANSITION_STATUSES = frozenset({"running", "succeeded", "failed"})
TRANSITION_ACCEPTANCE_STATES = frozenset(
    {"pending", "accepted", "rejected", "orphaned"}
)
INTERACTION_DIRECTIVE_KINDS = frozenset(
    {"material_intent_change", "cancel", "challenge"}
)
SCOPE_FILTER_OPERATORS = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "not_in",
        "between",
        "is_null",
        "is_not_null",
    }
)

_INTENT_PROVIDER_FIELDS = frozenset(
    {
        "goal_bindings",
        "target_metric_refs",
        "scope",
        "time_spec",
        "comparison_spec",
        "direction_premise",
        "requested_analysis_axes",
        "requested_factor_refs",
        "desired_decisions",
        "ambiguity_slots",
        "source_spans",
    }
)
_INTENT_FIELDS = (
    "intent_revision_id",
    "run_attempt_id",
    "supersedes_intent_revision_id",
    "original_user_text",
    "goal_bindings",
    "target_metric_refs",
    "scope",
    "time_spec",
    "comparison_spec",
    "direction_premise",
    "requested_analysis_axes",
    "requested_factor_refs",
    "desired_decisions",
    "ambiguity_slots",
    "source_spans",
    "schema_version",
    "prompt_version",
    "model_version",
    "content_digest",
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in canonical_value(value).items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    return canonical_value(value)


def _nonempty_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SingleAuthorityContractError(error)
    return value


def _canonical_utc_timestamp(value: Any, error: str) -> str:
    raw = _nonempty_string(value, error)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SingleAuthorityContractError(error) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SingleAuthorityContractError(error)
    normalized = parsed.astimezone(timezone.utc)
    fraction = (
        f".{normalized.microsecond:06d}".rstrip("0") if normalized.microsecond else ""
    )
    return normalized.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "+00:00"


def _string_tuple(
    value: Any, error: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SingleAuthorityContractError(error)
    normalized = tuple(_nonempty_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise SingleAuthorityContractError(error)
    if len(normalized) != len(set(normalized)):
        raise SingleAuthorityContractError(error)
    return normalized


def _mapping_sequence(value: Any, error: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SingleAuthorityContractError(error)
    if any(not isinstance(item, Mapping) for item in value):
        raise SingleAuthorityContractError(error)
    return tuple(_freeze(item) for item in value)


def _validate_catalog_refs(
    values: Iterable[str], known: set[str] | frozenset[str] | None, error: str
) -> None:
    if known is not None and any(value not in known for value in values):
        raise SingleAuthorityContractError(error)


def _validate_scope_filters(
    value: Any,
    *,
    known_filter_fields: set[str] | frozenset[str] | None,
) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SingleAuthorityContractError("intent_revision_scope_invalid")
    normalized: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise SingleAuthorityContractError("intent_revision_scope_filter_invalid")
        operator = item.get("op")
        if not isinstance(operator, str) or operator not in SCOPE_FILTER_OPERATORS:
            raise SingleAuthorityContractError("intent_revision_scope_filter_invalid")
        expected_fields = (
            {"field", "op"}
            if operator in {"is_null", "is_not_null"}
            else {"field", "op", "value"}
        )
        if set(item) != expected_fields:
            raise SingleAuthorityContractError("intent_revision_scope_filter_invalid")
        field = _nonempty_string(
            item.get("field"), "intent_revision_scope_filter_invalid"
        )
        _validate_catalog_refs(
            (field,),
            known_filter_fields,
            "intent_revision_scope_filter_field_unapproved",
        )
        if operator in {"in", "not_in", "between"}:
            raw_values = item.get("value")
            if (
                isinstance(raw_values, (str, bytes))
                or not isinstance(raw_values, Sequence)
                or not raw_values
                or operator == "between"
                and len(raw_values) != 2
            ):
                raise SingleAuthorityContractError(
                    "intent_revision_scope_filter_value_invalid"
                )
            values = tuple(raw_values)
        elif operator in {"is_null", "is_not_null"}:
            values = ()
        else:
            values = (item.get("value"),)
        if any(
            value is None
            or isinstance(value, (Mapping, list, tuple, set))
            or not isinstance(value, (str, int, float, bool))
            for value in values
        ):
            raise SingleAuthorityContractError(
                "intent_revision_scope_filter_value_invalid"
            )
        normalized.append(_freeze(item))
    return tuple(normalized)


@dataclass(frozen=True)
class IntentRevision:
    intent_revision_id: str
    run_attempt_id: str
    supersedes_intent_revision_id: str | None
    original_user_text: str
    goal_bindings: tuple[Mapping[str, Any], ...]
    target_metric_refs: tuple[str, ...]
    scope: Mapping[str, Any]
    time_spec: Mapping[str, Any]
    comparison_spec: Mapping[str, Any]
    direction_premise: str
    requested_analysis_axes: tuple[str, ...]
    requested_factor_refs: tuple[str, ...]
    desired_decisions: tuple[Mapping[str, Any], ...]
    ambiguity_slots: tuple[Mapping[str, Any], ...]
    source_spans: tuple[Mapping[str, Any], ...]
    schema_version: str
    prompt_version: str
    model_version: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_attempt_id: str,
        supersedes_intent_revision_id: str | None = None,
        original_user_text: str,
        goal_bindings: Sequence[Mapping[str, Any]],
        target_metric_refs: Sequence[str],
        scope: Mapping[str, Any],
        time_spec: Mapping[str, Any],
        comparison_spec: Mapping[str, Any],
        direction_premise: str,
        requested_analysis_axes: Sequence[str],
        requested_factor_refs: Sequence[str],
        desired_decisions: Sequence[Mapping[str, Any]],
        ambiguity_slots: Sequence[Mapping[str, Any]],
        source_spans: Sequence[Mapping[str, Any]],
        schema_version: str,
        prompt_version: str,
        model_version: str,
        known_goal_ids: set[str] | frozenset[str] | None = None,
        known_metric_ids: set[str] | frozenset[str] | None = None,
        known_analysis_axis_ids: set[str] | frozenset[str] | None = None,
        known_scope_types: set[str] | frozenset[str] | None = None,
        known_filter_fields: set[str] | frozenset[str] | None = None,
        known_ambiguity_value_refs: set[str] | frozenset[str] | None = None,
        known_desired_decision_kinds: set[str] | frozenset[str] | None = None,
        known_desired_decision_target_refs: set[str] | frozenset[str] | None = None,
        known_ambiguity_slots: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> "IntentRevision":
        run_attempt_id = _nonempty_string(
            run_attempt_id, "intent_revision_run_attempt_id_invalid"
        )
        if supersedes_intent_revision_id is not None:
            supersedes_intent_revision_id = _nonempty_string(
                supersedes_intent_revision_id,
                "intent_revision_supersedes_invalid",
            )
        original_user_text = _nonempty_string(
            original_user_text, "intent_revision_original_user_text_invalid"
        )
        raw_goals = _mapping_sequence(
            goal_bindings, "intent_revision_goal_bindings_invalid"
        )
        if not raw_goals:
            raise SingleAuthorityContractError("intent_revision_goal_bindings_invalid")
        normalized_goals: list[Mapping[str, Any]] = []
        for binding in raw_goals:
            if set(binding) != {"goal_id", "role"}:
                raise SingleAuthorityContractError(
                    "intent_revision_goal_bindings_invalid"
                )
            _nonempty_string(
                binding.get("goal_id"), "intent_revision_goal_bindings_invalid"
            )
            if binding.get("role") not in {"primary", "supporting"}:
                raise SingleAuthorityContractError(
                    "intent_revision_goal_bindings_invalid"
                )
            normalized_goals.append(_freeze(binding))
        if sum(
            binding["role"] == "primary" for binding in normalized_goals
        ) != 1 or len({binding["goal_id"] for binding in normalized_goals}) != len(
            normalized_goals
        ):
            raise SingleAuthorityContractError("intent_revision_goal_bindings_invalid")
        _validate_catalog_refs(
            (str(binding["goal_id"]) for binding in normalized_goals),
            known_goal_ids,
            "intent_revision_goal_ref_unknown",
        )

        target_metric_refs = _string_tuple(
            target_metric_refs,
            "intent_revision_target_metric_refs_invalid",
            allow_empty=False,
        )
        _validate_catalog_refs(
            target_metric_refs,
            known_metric_ids,
            "intent_revision_metric_ref_unknown",
        )
        if not isinstance(scope, Mapping) or set(scope) != {"scope_type", "filters"}:
            raise SingleAuthorityContractError("intent_revision_scope_invalid")
        scope_type = _nonempty_string(
            scope.get("scope_type"), "intent_revision_scope_invalid"
        )
        _validate_catalog_refs(
            (scope_type,), known_scope_types, "intent_revision_scope_ref_unknown"
        )
        normalized_scope = _freeze(
            {
                "scope_type": scope_type,
                "filters": _validate_scope_filters(
                    scope.get("filters"), known_filter_fields=known_filter_fields
                ),
            }
        )
        if not isinstance(time_spec, Mapping):
            raise SingleAuthorityContractError("intent_revision_time_spec_invalid")
        try:
            normalized_time_spec = _freeze(validate_time_spec(time_spec))
            normalized_comparison_spec = _freeze(
                validate_comparison_spec(
                    comparison_spec,
                    time_spec=normalized_time_spec,
                )
            )
        except TemporalComparisonContractError as exc:
            error = (
                "intent_revision_time_spec_invalid"
                if str(exc) == "temporal_time_spec_invalid"
                else "intent_revision_comparison_spec_invalid"
            )
            raise SingleAuthorityContractError(error) from exc
        if direction_premise not in DIRECTION_PREMISES:
            raise SingleAuthorityContractError(
                "intent_revision_direction_premise_invalid"
            )
        requested_analysis_axes = _string_tuple(
            requested_analysis_axes,
            "intent_revision_analysis_axes_invalid",
        )
        _validate_catalog_refs(
            requested_analysis_axes,
            known_analysis_axis_ids,
            "intent_revision_analysis_axis_ref_unknown",
        )
        requested_factor_refs = _string_tuple(
            requested_factor_refs,
            "intent_revision_factor_refs_invalid",
        )
        _validate_catalog_refs(
            requested_factor_refs,
            known_metric_ids,
            "intent_revision_factor_ref_unknown",
        )
        if set(requested_factor_refs) & set(target_metric_refs):
            raise SingleAuthorityContractError(
                "intent_revision_factor_target_overlap_invalid"
            )
        normalized_desired = _mapping_sequence(
            desired_decisions, "intent_revision_desired_decisions_invalid"
        )
        for desired in normalized_desired:
            if set(desired) != {"decision_kind", "target_ref"}:
                raise SingleAuthorityContractError(
                    "intent_revision_desired_decisions_invalid"
                )
            decision_kind = _nonempty_string(
                desired.get("decision_kind"),
                "intent_revision_desired_decisions_invalid",
            )
            target_ref = _nonempty_string(
                desired.get("target_ref"),
                "intent_revision_desired_decisions_invalid",
            )
            _validate_catalog_refs(
                (decision_kind,),
                known_desired_decision_kinds,
                "intent_revision_desired_decision_ref_unknown",
            )
            _validate_catalog_refs(
                (target_ref,),
                known_desired_decision_target_refs,
                "intent_revision_desired_decision_target_ref_unknown",
            )

        normalized_slots = _mapping_sequence(
            ambiguity_slots, "intent_revision_ambiguity_slots_invalid"
        )
        slot_ids: list[str] = []
        for slot in normalized_slots:
            if set(slot) != {
                "slot_id",
                "slot_kind",
                "materiality",
                "status",
                "question",
                "allowed_value_refs",
            }:
                raise SingleAuthorityContractError(
                    "intent_revision_ambiguity_slots_invalid"
                )
            slot_id = _nonempty_string(
                slot.get("slot_id"), "intent_revision_ambiguity_slots_invalid"
            )
            slot_ids.append(slot_id)
            slot_kind = _nonempty_string(
                slot.get("slot_kind"), "intent_revision_ambiguity_slots_invalid"
            )
            if slot.get("materiality") not in DECISION_MATERIALITIES:
                raise SingleAuthorityContractError(
                    "intent_revision_ambiguity_slots_invalid"
                )
            if slot.get("status") not in {"unresolved", "resolved"}:
                raise SingleAuthorityContractError(
                    "intent_revision_ambiguity_slots_invalid"
                )
            _nonempty_string(
                slot.get("question"), "intent_revision_ambiguity_slots_invalid"
            )
            allowed_value_refs = _string_tuple(
                slot.get("allowed_value_refs"),
                "intent_revision_ambiguity_slots_invalid",
            )
            _validate_catalog_refs(
                allowed_value_refs,
                known_ambiguity_value_refs,
                "intent_revision_ambiguity_value_ref_unknown",
            )
            if known_ambiguity_slots is not None:
                contract = known_ambiguity_slots.get(slot_id)
                if not isinstance(contract, Mapping):
                    raise SingleAuthorityContractError(
                        "intent_revision_ambiguity_slot_ref_unknown"
                    )
                if (
                    slot_kind != contract.get("slot_kind")
                    or slot.get("materiality") != contract.get("materiality")
                    or tuple(allowed_value_refs)
                    != tuple(contract.get("allowed_value_refs") or ())
                ):
                    raise SingleAuthorityContractError(
                        "intent_revision_ambiguity_slot_contract_invalid"
                    )
        if len(slot_ids) != len(set(slot_ids)):
            raise SingleAuthorityContractError(
                "intent_revision_ambiguity_slots_invalid"
            )
        try:
            validate_comparison_slot_binding(
                normalized_comparison_spec,
                ambiguity_slots=normalized_slots,
            )
        except TemporalComparisonContractError as exc:
            raise SingleAuthorityContractError(
                "intent_revision_comparison_authority_invalid"
            ) from exc

        normalized_spans = _mapping_sequence(
            source_spans, "intent_revision_source_span_invalid"
        )
        for span in normalized_spans:
            if set(span) != {"field", "start", "end", "text"}:
                raise SingleAuthorityContractError(
                    "intent_revision_source_span_invalid"
                )
            _nonempty_string(span.get("field"), "intent_revision_source_span_invalid")
            start, end, text = span.get("start"), span.get("end"), span.get("text")
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not isinstance(text, str)
                or start < 0
                or end <= start
                or end > len(original_user_text)
                or original_user_text[start:end] != text
            ):
                raise SingleAuthorityContractError(
                    "intent_revision_source_span_invalid"
                )

        schema_version = _nonempty_string(
            schema_version, "intent_revision_schema_version_invalid"
        )
        prompt_version = _nonempty_string(
            prompt_version, "intent_revision_prompt_version_invalid"
        )
        model_version = _nonempty_string(
            model_version, "intent_revision_model_version_invalid"
        )
        body = {
            "run_attempt_id": run_attempt_id,
            "supersedes_intent_revision_id": supersedes_intent_revision_id,
            "original_user_text": original_user_text,
            "goal_bindings": normalized_goals,
            "target_metric_refs": target_metric_refs,
            "scope": normalized_scope,
            "time_spec": normalized_time_spec,
            "comparison_spec": normalized_comparison_spec,
            "direction_premise": direction_premise,
            "requested_analysis_axes": requested_analysis_axes,
            "requested_factor_refs": requested_factor_refs,
            "desired_decisions": normalized_desired,
            "ambiguity_slots": normalized_slots,
            "source_spans": normalized_spans,
            "schema_version": schema_version,
            "prompt_version": prompt_version,
            "model_version": model_version,
        }
        content_digest = canonical_digest(body)
        intent_revision_id = (
            "intent-revision-"
            + canonical_digest(
                {
                    "run_attempt_id": run_attempt_id,
                    "supersedes_intent_revision_id": supersedes_intent_revision_id,
                    "content_digest": content_digest,
                }
            )[:24]
        )
        if intent_revision_id == supersedes_intent_revision_id:
            raise SingleAuthorityContractError("intent_revision_supersedes_self")
        return cls(
            intent_revision_id=intent_revision_id,
            content_digest=content_digest,
            **body,
        )

    @classmethod
    def from_provider_binding(
        cls,
        output: Mapping[str, Any],
        *,
        run_attempt_id: str,
        original_user_text: str,
        schema_version: str,
        prompt_version: str,
        model_version: str,
        supersedes_intent_revision_id: str | None = None,
        known_goal_ids: set[str] | frozenset[str] | None = None,
        known_metric_ids: set[str] | frozenset[str] | None = None,
        known_analysis_axis_ids: set[str] | frozenset[str] | None = None,
        known_scope_types: set[str] | frozenset[str] | None = None,
        known_filter_fields: set[str] | frozenset[str] | None = None,
        known_ambiguity_value_refs: set[str] | frozenset[str] | None = None,
        known_desired_decision_kinds: set[str] | frozenset[str] | None = None,
        known_desired_decision_target_refs: set[str] | frozenset[str] | None = None,
        known_ambiguity_slots: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> "IntentRevision":
        if not isinstance(output, Mapping) or set(output) != _INTENT_PROVIDER_FIELDS:
            raise SingleAuthorityContractError("intent_binding_provider_shape_invalid")
        return cls.create(
            run_attempt_id=run_attempt_id,
            supersedes_intent_revision_id=supersedes_intent_revision_id,
            original_user_text=original_user_text,
            schema_version=schema_version,
            prompt_version=prompt_version,
            model_version=model_version,
            known_goal_ids=known_goal_ids,
            known_metric_ids=known_metric_ids,
            known_analysis_axis_ids=known_analysis_axis_ids,
            known_scope_types=known_scope_types,
            known_filter_fields=known_filter_fields,
            known_ambiguity_value_refs=known_ambiguity_value_refs,
            known_desired_decision_kinds=known_desired_decision_kinds,
            known_desired_decision_target_refs=known_desired_decision_target_refs,
            known_ambiguity_slots=known_ambiguity_slots,
            **dict(output),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "IntentRevision":
        if not isinstance(payload, Mapping) or set(payload) != set(_INTENT_FIELDS):
            raise SingleAuthorityContractError("intent_revision_shape_invalid")
        expected_id = payload.get("intent_revision_id")
        expected_digest = payload.get("content_digest")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in _INTENT_FIELDS
                if key not in {"intent_revision_id", "content_digest"}
            }
        )
        if rebuilt.content_digest != expected_digest:
            raise SingleAuthorityContractError("intent_revision_content_digest_invalid")
        if rebuilt.intent_revision_id != expected_id:
            raise SingleAuthorityContractError("intent_revision_id_invalid")
        return rebuilt

    @property
    def material_binding_digest(self) -> str:
        return canonical_digest(
            {
                "goal_bindings": self.goal_bindings,
                "target_metric_refs": self.target_metric_refs,
                "scope": self.scope,
                "time_spec": self.time_spec,
                "comparison_spec": self.comparison_spec,
                "direction_premise": self.direction_premise,
                "requested_analysis_axes": self.requested_analysis_axes,
                "requested_factor_refs": self.requested_factor_refs,
                "desired_decisions": self.desired_decisions,
                "ambiguity_slots": [
                    {key: value for key, value in slot.items() if key != "question"}
                    for slot in self.ambiguity_slots
                ],
                "schema_version": self.schema_version,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {field: _plain(getattr(self, field)) for field in _INTENT_FIELDS}


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    intent_revision_id: str
    slot_id: str
    value: Any
    source: str
    status: str
    materiality: str
    affected_plan_fields: tuple[str, ...]
    option_id: str | None
    invalidated_by_revision_id: str | None
    supersedes_decision_id: str | None
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        intent_revision_id: str,
        slot_id: str,
        value: Any,
        source: str,
        status: str,
        materiality: str,
        affected_plan_fields: Sequence[str],
        option_id: str | None = None,
        invalidated_by_revision_id: str | None = None,
        supersedes_decision_id: str | None = None,
    ) -> "DecisionRecord":
        intent_revision_id = _nonempty_string(
            intent_revision_id, "decision_intent_revision_id_invalid"
        )
        slot_id = _nonempty_string(slot_id, "decision_slot_id_invalid")
        if source not in DECISION_SOURCES:
            raise SingleAuthorityContractError("decision_source_invalid")
        if status not in DECISION_STATUSES:
            raise SingleAuthorityContractError("decision_status_invalid")
        if materiality not in DECISION_MATERIALITIES:
            raise SingleAuthorityContractError("decision_materiality_invalid")
        affected = _string_tuple(
            affected_plan_fields, "decision_affected_plan_fields_invalid"
        )
        if option_id is not None:
            option_id = _nonempty_string(option_id, "decision_option_id_invalid")
        if invalidated_by_revision_id is not None:
            invalidated_by_revision_id = _nonempty_string(
                invalidated_by_revision_id,
                "decision_invalidated_by_revision_id_invalid",
            )
        if supersedes_decision_id is not None:
            supersedes_decision_id = _nonempty_string(
                supersedes_decision_id, "decision_supersedes_id_invalid"
            )
        if status == "invalidated" and (
            not invalidated_by_revision_id or not supersedes_decision_id
        ):
            raise SingleAuthorityContractError("decision_invalidation_invalid")
        if status != "invalidated" and invalidated_by_revision_id:
            raise SingleAuthorityContractError("decision_invalidation_invalid")
        normalized_value = _freeze(value)
        body = {
            "intent_revision_id": intent_revision_id,
            "slot_id": slot_id,
            "value": normalized_value,
            "source": source,
            "status": status,
            "materiality": materiality,
            "affected_plan_fields": affected,
            "option_id": option_id,
            "invalidated_by_revision_id": invalidated_by_revision_id,
            "supersedes_decision_id": supersedes_decision_id,
        }
        content_digest = canonical_digest(body)
        decision_id = (
            "decision-"
            + canonical_digest(
                {
                    "intent_revision_id": intent_revision_id,
                    "slot_id": slot_id,
                    "option_id": option_id,
                    "content_digest": content_digest,
                }
            )[:24]
        )
        return cls(decision_id=decision_id, content_digest=content_digest, **body)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DecisionRecord":
        expected_fields = {
            "decision_id",
            "intent_revision_id",
            "slot_id",
            "value",
            "source",
            "status",
            "materiality",
            "affected_plan_fields",
            "option_id",
            "invalidated_by_revision_id",
            "supersedes_decision_id",
            "content_digest",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected_fields:
            raise SingleAuthorityContractError("decision_record_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in expected_fields
                if key not in {"decision_id", "content_digest"}
            }
        )
        if rebuilt.content_digest != payload.get("content_digest"):
            raise SingleAuthorityContractError("decision_content_digest_invalid")
        if rebuilt.decision_id != payload.get("decision_id"):
            raise SingleAuthorityContractError("decision_id_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class DecisionLedger:
    records: tuple[DecisionRecord, ...] = ()

    @property
    def position(self) -> int:
        return len(self.records)

    def append(self, record: DecisionRecord) -> "DecisionLedger":
        if not isinstance(record, DecisionRecord):
            raise SingleAuthorityContractError("decision_record_type_invalid")
        for existing in self.records:
            if existing.decision_id == record.decision_id:
                if existing == record:
                    return self
                raise SingleAuthorityContractError("decision_id_conflict")
            if (
                record.option_id
                and existing.intent_revision_id == record.intent_revision_id
                and existing.slot_id == record.slot_id
                and existing.option_id == record.option_id
            ):
                if existing.content_digest == record.content_digest:
                    return self
                raise SingleAuthorityContractError("decision_option_id_conflict")
        if record.supersedes_decision_id and not any(
            item.decision_id == record.supersedes_decision_id for item in self.records
        ):
            raise SingleAuthorityContractError("decision_supersedes_missing")
        return DecisionLedger(records=(*self.records, record))

    def active_records(self) -> tuple[DecisionRecord, ...]:
        superseded = {
            record.supersedes_decision_id
            for record in self.records
            if record.supersedes_decision_id
        }
        latest_by_slot: dict[str, DecisionRecord] = {}
        for record in self.records:
            if record.decision_id in superseded or record.status == "invalidated":
                continue
            latest_by_slot[record.slot_id] = record
        return tuple(latest_by_slot.values())

    def active_for_slot(self, slot_id: str) -> DecisionRecord | None:
        return next(
            (
                record
                for record in reversed(self.active_records())
                if record.slot_id == slot_id
            ),
            None,
        )

    def supersede_for_revision(
        self,
        new_intent_revision_id: str,
        *,
        affected_plan_fields: set[str] | frozenset[str],
    ) -> "DecisionLedger":
        new_intent_revision_id = _nonempty_string(
            new_intent_revision_id, "decision_intent_revision_id_invalid"
        )
        ledger = self
        for record in self.active_records():
            if set(record.affected_plan_fields).intersection(affected_plan_fields):
                ledger = ledger.append(
                    DecisionRecord.create(
                        intent_revision_id=new_intent_revision_id,
                        slot_id=record.slot_id,
                        value=record.value,
                        source="system",
                        status="invalidated",
                        materiality=record.materiality,
                        affected_plan_fields=record.affected_plan_fields,
                        invalidated_by_revision_id=new_intent_revision_id,
                        supersedes_decision_id=record.decision_id,
                    )
                )
            else:
                ledger = ledger.append(
                    DecisionRecord.create(
                        intent_revision_id=new_intent_revision_id,
                        slot_id=record.slot_id,
                        value=record.value,
                        source="inherited",
                        status=record.status,
                        materiality=record.materiality,
                        affected_plan_fields=record.affected_plan_fields,
                        option_id=record.option_id,
                        supersedes_decision_id=record.decision_id,
                    )
                )
        return ledger


@dataclass(frozen=True)
class InteractionDirective:
    directive_id: str
    run_attempt_id: str
    intent_revision_id: str
    kind: str
    target_refs: tuple[str, ...]
    original_user_text: str
    source: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_attempt_id: str,
        intent_revision_id: str,
        kind: str,
        target_refs: Sequence[str],
        original_user_text: str,
        source: str = "user",
    ) -> "InteractionDirective":
        run_attempt_id = _nonempty_string(
            run_attempt_id, "directive_run_attempt_id_invalid"
        )
        intent_revision_id = _nonempty_string(
            intent_revision_id, "directive_intent_revision_id_invalid"
        )
        if kind not in INTERACTION_DIRECTIVE_KINDS:
            raise SingleAuthorityContractError("directive_kind_invalid")
        refs = _string_tuple(target_refs, "directive_target_refs_invalid")
        original_user_text = _nonempty_string(
            original_user_text, "directive_original_user_text_invalid"
        )
        if source != "user":
            raise SingleAuthorityContractError("directive_source_invalid")
        body = {
            "run_attempt_id": run_attempt_id,
            "intent_revision_id": intent_revision_id,
            "kind": kind,
            "target_refs": refs,
            "original_user_text": original_user_text,
            "source": source,
        }
        content_digest = canonical_digest(body)
        return cls(
            directive_id="directive-" + content_digest[:24],
            content_digest=content_digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InteractionDirective":
        expected_fields = {
            "directive_id",
            "run_attempt_id",
            "intent_revision_id",
            "kind",
            "target_refs",
            "original_user_text",
            "source",
            "content_digest",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected_fields:
            raise SingleAuthorityContractError("directive_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in expected_fields
                if key not in {"directive_id", "content_digest"}
            }
        )
        if rebuilt.directive_id != payload.get("directive_id"):
            raise SingleAuthorityContractError("directive_id_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise SingleAuthorityContractError("directive_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class FailureRecord:
    failure_id: str
    layer: str
    kind: str
    scope: str
    affected_refs: tuple[str, ...]
    integrity_level: str
    retryability: str
    user_actionable: bool
    business_boundary: str
    technical_detail_ref: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        layer: str,
        kind: str,
        scope: str,
        affected_refs: Sequence[str],
        integrity_level: str,
        retryability: str,
        user_actionable: bool,
        business_boundary: str,
        technical_detail_ref: str,
    ) -> "FailureRecord":
        if layer not in FAILURE_LAYERS:
            raise SingleAuthorityContractError("failure_layer_invalid")
        kind = _nonempty_string(kind, "failure_kind_invalid")
        if scope not in FAILURE_SCOPES:
            raise SingleAuthorityContractError("failure_scope_invalid")
        refs = _string_tuple(affected_refs, "failure_affected_refs_invalid")
        if integrity_level not in FAILURE_INTEGRITY_LEVELS:
            raise SingleAuthorityContractError("failure_integrity_level_invalid")
        if retryability not in FAILURE_RETRYABILITY:
            raise SingleAuthorityContractError("failure_retryability_invalid")
        if not isinstance(user_actionable, bool):
            raise SingleAuthorityContractError("failure_user_actionable_invalid")
        business_boundary = _nonempty_string(
            business_boundary, "failure_business_boundary_invalid"
        )
        technical_detail_ref = _nonempty_string(
            technical_detail_ref, "failure_technical_detail_ref_invalid"
        )
        body = {
            "layer": layer,
            "kind": kind,
            "scope": scope,
            "affected_refs": refs,
            "integrity_level": integrity_level,
            "retryability": retryability,
            "user_actionable": user_actionable,
            "business_boundary": business_boundary,
            "technical_detail_ref": technical_detail_ref,
        }
        content_digest = canonical_digest(body)
        return cls(
            failure_id="failure-" + content_digest[:24],
            content_digest=content_digest,
            **body,
        )

    @property
    def policy_scope(self) -> tuple[Any, ...]:
        return (
            self.layer,
            self.kind,
            self.scope,
            self.affected_refs,
            self.integrity_level,
            self.retryability,
            self.user_actionable,
        )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FailureRecord":
        expected_fields = {
            "failure_id",
            "layer",
            "kind",
            "scope",
            "affected_refs",
            "integrity_level",
            "retryability",
            "user_actionable",
            "business_boundary",
            "technical_detail_ref",
            "content_digest",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected_fields:
            raise SingleAuthorityContractError("failure_record_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in expected_fields
                if key not in {"failure_id", "content_digest"}
            }
        )
        if rebuilt.content_digest != payload.get("content_digest"):
            raise SingleAuthorityContractError("failure_content_digest_invalid")
        if rebuilt.failure_id != payload.get("failure_id"):
            raise SingleAuthorityContractError("failure_id_invalid")
        return rebuilt


@dataclass(frozen=True)
class LifecycleState:
    run_attempt_id: str
    state_revision: int
    execution_state: str
    interaction_state: str
    evidence_state: str
    publication_state: str
    delivery_state: str
    retry_state: str
    cancellation_state: str
    supersession_state: str
    prior_state_digest: str | None
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_attempt_id: str,
        state_revision: int = 1,
        execution_state: str = "pending",
        interaction_state: str = "active",
        evidence_state: str = "not_started",
        publication_state: str = "not_ready",
        delivery_state: str = "pending",
        retry_state: str = "idle",
        cancellation_state: str = "active",
        supersession_state: str = "active",
        prior_state_digest: str | None = None,
    ) -> "LifecycleState":
        run_attempt_id = _nonempty_string(
            run_attempt_id, "lifecycle_run_attempt_id_invalid"
        )
        if (
            isinstance(state_revision, bool)
            or not isinstance(state_revision, int)
            or state_revision < 1
        ):
            raise SingleAuthorityContractError("lifecycle_state_revision_invalid")
        values = {
            "execution_state": (execution_state, EXECUTION_STATES),
            "interaction_state": (interaction_state, INTERACTION_STATES),
            "evidence_state": (evidence_state, EVIDENCE_STATES),
            "publication_state": (publication_state, PUBLICATION_STATES),
            "delivery_state": (delivery_state, DELIVERY_STATES),
            "retry_state": (retry_state, RETRY_STATES),
            "cancellation_state": (cancellation_state, CANCELLATION_STATES),
            "supersession_state": (supersession_state, SUPERSESSION_STATES),
        }
        for field_name, (value, allowed) in values.items():
            if value not in allowed:
                raise SingleAuthorityContractError(f"lifecycle_{field_name}_invalid")
        if prior_state_digest is not None and (
            not isinstance(prior_state_digest, str) or len(prior_state_digest) != 64
        ):
            raise SingleAuthorityContractError("lifecycle_prior_state_digest_invalid")
        body = {
            "run_attempt_id": run_attempt_id,
            "state_revision": state_revision,
            **{name: value for name, (value, _) in values.items()},
            "prior_state_digest": prior_state_digest,
        }
        return cls(content_digest=canonical_digest(body), **body)

    def transition(self, **changes: Any) -> "LifecycleState":
        allowed = {
            "execution_state",
            "interaction_state",
            "evidence_state",
            "publication_state",
            "delivery_state",
            "retry_state",
            "cancellation_state",
            "supersession_state",
        }
        if set(changes) - allowed:
            raise SingleAuthorityContractError("lifecycle_transition_field_invalid")
        current = {field: getattr(self, field) for field in allowed}
        current.update(changes)
        return LifecycleState.create(
            run_attempt_id=self.run_attempt_id,
            state_revision=self.state_revision + 1,
            prior_state_digest=self.content_digest,
            **current,
        )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LifecycleState":
        expected_fields = {
            "run_attempt_id",
            "state_revision",
            "execution_state",
            "interaction_state",
            "evidence_state",
            "publication_state",
            "delivery_state",
            "retry_state",
            "cancellation_state",
            "supersession_state",
            "prior_state_digest",
            "content_digest",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected_fields:
            raise SingleAuthorityContractError("lifecycle_state_shape_invalid")
        rebuilt = cls.create(
            **{key: payload[key] for key in expected_fields if key != "content_digest"}
        )
        if rebuilt.content_digest != payload.get("content_digest"):
            raise SingleAuthorityContractError("lifecycle_content_digest_invalid")
        return rebuilt


def result_acceptance_state(
    *,
    lifecycle: LifecycleState,
    result_intent_revision_id: str,
    active_intent_revision_id: str,
) -> str:
    if (
        lifecycle.cancellation_state != "active"
        or lifecycle.supersession_state != "active"
        or result_intent_revision_id != active_intent_revision_id
    ):
        return "orphaned"
    return "accepted"


@dataclass(frozen=True)
class DurableTransition:
    transition_id: str
    attempt_id: str
    node_name: str
    parent_transition_id: str | None
    run_attempt_id: str
    intent_revision_id: str
    decision_ledger_position: int
    input_digest: str
    output_digest: str
    execution_attempt: int
    provider_ref: str
    model_ref: str
    status: str
    acceptance_state: str
    next_transition: str
    started_at: str
    finished_at: str

    @classmethod
    def create(
        cls,
        *,
        node_name: str,
        parent_transition_id: str | None,
        run_attempt_id: str,
        intent_revision_id: str,
        decision_ledger_position: int,
        input_digest: str,
        output_digest: str,
        execution_attempt: int,
        provider_ref: str,
        model_ref: str,
        status: str,
        acceptance_state: str,
        next_transition: str,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> "DurableTransition":
        node_name = _nonempty_string(node_name, "transition_node_name_invalid")
        run_attempt_id = _nonempty_string(
            run_attempt_id, "transition_run_attempt_id_invalid"
        )
        if parent_transition_id is not None:
            parent_transition_id = _nonempty_string(
                parent_transition_id, "transition_parent_id_invalid"
            )
        if not isinstance(intent_revision_id, str):
            raise SingleAuthorityContractError("transition_intent_revision_id_invalid")
        if (
            isinstance(decision_ledger_position, bool)
            or not isinstance(decision_ledger_position, int)
            or decision_ledger_position < 0
        ):
            raise SingleAuthorityContractError("transition_ledger_position_invalid")
        for name, digest in (("input", input_digest), ("output", output_digest)):
            if not isinstance(digest, str) or len(digest) != 64:
                raise SingleAuthorityContractError(f"transition_{name}_digest_invalid")
        if (
            isinstance(execution_attempt, bool)
            or not isinstance(execution_attempt, int)
            or execution_attempt < 1
        ):
            raise SingleAuthorityContractError("transition_execution_attempt_invalid")
        provider_ref = _nonempty_string(provider_ref, "transition_provider_ref_invalid")
        model_ref = _nonempty_string(model_ref, "transition_model_ref_invalid")
        if status not in TRANSITION_STATUSES:
            raise SingleAuthorityContractError("transition_status_invalid")
        if acceptance_state not in TRANSITION_ACCEPTANCE_STATES:
            raise SingleAuthorityContractError("transition_acceptance_state_invalid")
        if acceptance_state == "accepted" and status != "succeeded":
            raise SingleAuthorityContractError("transition_acceptance_status_invalid")
        next_transition = _nonempty_string(
            next_transition, "transition_next_transition_invalid"
        )
        now = datetime.now(timezone.utc).isoformat()
        started_at = _canonical_utc_timestamp(
            started_at or now, "transition_started_at_invalid"
        )
        finished_at = _canonical_utc_timestamp(
            finished_at or now, "transition_finished_at_invalid"
        )
        transition_id = (
            "transition-"
            + canonical_digest(
                {
                    "node_name": node_name,
                    "parent_transition_id": parent_transition_id,
                    "run_attempt_id": run_attempt_id,
                    "intent_revision_id": intent_revision_id,
                    "decision_ledger_position": decision_ledger_position,
                    "input_digest": input_digest,
                }
            )[:24]
        )
        attempt_id = f"{transition_id}:attempt:{execution_attempt}"
        return cls(
            transition_id=transition_id,
            attempt_id=attempt_id,
            node_name=node_name,
            parent_transition_id=parent_transition_id,
            run_attempt_id=run_attempt_id,
            intent_revision_id=intent_revision_id,
            decision_ledger_position=decision_ledger_position,
            input_digest=input_digest,
            output_digest=output_digest,
            execution_attempt=execution_attempt,
            provider_ref=provider_ref,
            model_ref=model_ref,
            status=status,
            acceptance_state=acceptance_state,
            next_transition=next_transition,
            started_at=started_at,
            finished_at=finished_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DurableTransition":
        expected_fields = {
            "transition_id",
            "attempt_id",
            "node_name",
            "parent_transition_id",
            "run_attempt_id",
            "intent_revision_id",
            "decision_ledger_position",
            "input_digest",
            "output_digest",
            "execution_attempt",
            "provider_ref",
            "model_ref",
            "status",
            "acceptance_state",
            "next_transition",
            "started_at",
            "finished_at",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected_fields:
            raise SingleAuthorityContractError("transition_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in expected_fields
                if key not in {"transition_id", "attempt_id"}
            }
        )
        if rebuilt.transition_id != payload.get("transition_id"):
            raise SingleAuthorityContractError("transition_id_invalid")
        if rebuilt.attempt_id != payload.get("attempt_id"):
            raise SingleAuthorityContractError("transition_attempt_id_invalid")
        return rebuilt
