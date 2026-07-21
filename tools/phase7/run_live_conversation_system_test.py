#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

import yaml


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.phase7 import run_gateway_conversation_once as gateway_once  # noqa: E402


DEFAULT_CASES_PATH = ROOT / "evals" / "phase7" / "business_question_expectations.yaml"
DEFAULT_ARTIFACT_DIR = ROOT / "artifacts" / "phase7" / "customer-publication-acceptance"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
ACCEPTANCE_SUMMARY_VERSION = "phase7-customer-publication-acceptance-summary.v2"
PAIR_MATERIAL_SNAPSHOT_VERSION = "phase7-pair-material-snapshot.v1"
ACCEPTANCE_SOURCE = "persisted_customer_publication"
QUESTION_FAMILIES = (
    "pattern_explanation",
    "paid_amount_change_explanation",
    "business_object_impact_review",
    "revenue_health_review",
    "segment_or_factor_attribution",
    "anomaly_or_black_swan_review",
    "custom_baseline_comparison",
    "data_quality_or_evidence_review",
)
PAIR_FIELDS = frozenset({"original_case_id", "paraphrase_case_id"})
CASE_FIELDS = frozenset({"case_id", "user_message", "review_focus"})
EXECUTION_CONTRACT = {
    "entrypoint": "gateway",
    "required_dependencies": ["postgres", "clickhouse", "deepseek"],
    "data_authority": "active_release",
    "prebound_sql": "forbidden",
    "injected_rows": "forbidden",
    "prebound_capabilities": "forbidden",
    "acceptance_source": ACCEPTANCE_SOURCE,
}
RUN_CHECKPOINTS = frozenset(
    {
        "interaction_completed",
        "waiting_for_clarification",
        "planned",
        "evidence_ready",
        "authority_sealed",
        "narrative_ready",
        "failed",
    }
)
FINAL_DELIVERY_STATES = frozenset(
    {"published", "retryable_failed", "permanently_failed"}
)
POST_EXECUTION_FAILURE_STATES = {
    "narrative_failed": ("not_ready", "pending"),
    "publication_failed": ("failed", "pending"),
}
SUMMARY_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "acceptance_source",
        "case",
        "dependency_health",
        "execution",
        "active_release_refs",
        "authority_refs",
        "pair_material_snapshot",
        "publication",
        "delivery",
        "llm_call_audits",
        "human_decisions",
        "terminal_state",
    }
)
LLM_AUDIT_FIELDS = frozenset(
    {
        "audit_kind",
        "run_id",
        "audit_ref",
        "task",
        "provider_ref",
        "model_ref",
        "status",
        "acceptance_state",
        "attempt_number",
        "input_ref",
        "input_digest",
        "output_digest",
        "started_at",
        "finished_at",
    }
)
HUMAN_DECISION_FIELDS = frozenset(
    {
        "decision_ref",
        "run_id",
        "decision_kind",
        "slot_id",
        "selected_option_id",
        "source",
        "status",
        "materiality",
        "content_digest",
        "recorded_at",
    }
)
FORBIDDEN_SUMMARY_KEYS = frozenset(
    {
        "rows",
        "raw_rows",
        "raw_response",
        "raw_response_content",
        "customer_publication",
        "customer_payload",
        "password",
        "secret",
        "api_key",
        "access_token",
        "refresh_token",
    }
)

PAIR_MATERIAL_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "intent_revision_id",
        "plan_revision_id",
        "target_metric_refs",
        "metric_refs",
        "scope",
        "time_semantics",
        "active_material_decisions",
        "user_required_obligation_coverage",
        "content_digest",
    }
)
PAIR_TIME_SEMANTICS_FIELDS = frozenset(
    {"intent_time_spec", "resolved_window_refs", "context_window_specs"}
)
PAIR_MATERIAL_DECISION_FIELDS = frozenset(
    {
        "slot_id",
        "option_id",
        "source",
        "status",
        "materiality",
        "value",
        "affected_plan_fields",
    }
)
PAIR_REQUIRED_OBLIGATION_FIELDS = frozenset(
    {
        "obligation_id",
        "claim_kind",
        "target_metric_ref",
        "scope",
        "outcome_refs",
        "minimum_claim_strength",
        "coverage_state",
        "coverage_claim_refs",
        "coverage_limitation_refs",
        "unavailable_limitation_refs",
    }
)
PAIR_DECISION_SOURCES = frozenset(
    {"user", "accepted_recommendation", "safe_inference", "inherited", "system"}
)


PHASE7_ACCEPTANCE_SUMMARY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": ACCEPTANCE_SUMMARY_VERSION,
    "title": "WAJE Phase 7 persisted customer publication acceptance summary",
    "type": "object",
    "additionalProperties": False,
    "required": sorted(SUMMARY_TOP_LEVEL_FIELDS),
    "properties": {
        "schema_version": {"const": ACCEPTANCE_SUMMARY_VERSION},
        "acceptance_source": {"const": ACCEPTANCE_SOURCE},
        "case": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "case_id",
                "question_family",
                "variant",
                "user_message",
                "review_focus",
            ],
            "properties": {
                "case_id": {"type": "string", "minLength": 1},
                "question_family": {"type": ["string", "null"]},
                "variant": {"enum": ["original", "paraphrase", "additional"]},
                "user_message": {"type": "string", "minLength": 1},
                "review_focus": {"type": "string", "minLength": 1},
            },
        },
        "dependency_health": {
            "type": "object",
            "additionalProperties": False,
            "required": ["checked_at", "overall_status", "checks"],
            "properties": {
                "checked_at": {"type": "string", "format": "date-time"},
                "overall_status": {"enum": ["ok", "degraded"]},
                "checks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "dependency",
                            "gateway_check",
                            "status",
                            "detail",
                        ],
                        "properties": {
                            "dependency": {
                                "enum": [
                                    "gateway",
                                    "postgres",
                                    "clickhouse",
                                    "deepseek",
                                ]
                            },
                            "gateway_check": {"type": "string"},
                            "status": {"enum": ["ok", "failed"]},
                            "detail": {"type": "string"},
                        },
                    },
                },
            },
        },
        "execution": {
            "type": "object",
            "additionalProperties": False,
            "required": ["thread_id", "run_ids", "final_run_id"],
            "properties": {
                "thread_id": {"type": "string", "minLength": 1},
                "run_ids": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "final_run_id": {"type": "string", "minLength": 1},
            },
        },
        "active_release_refs": {
            "type": "object",
            "additionalProperties": False,
            "required": ["actual_as_of", "release_refs", "snapshot_refs"],
            "properties": {
                "actual_as_of": {"type": ["string", "null"]},
                "release_refs": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
                "snapshot_refs": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
            },
        },
        "authority_refs": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "intent_revision_id",
                "authority_context_ref",
                "authority_context_digest",
                "plan_revision_id",
                "execution_result_ref",
                "authority_bundle_ref",
                "authority_bundle_digest",
            ],
            "properties": {
                field: {"type": ["string", "null"]}
                for field in (
                    "intent_revision_id",
                    "authority_context_ref",
                    "authority_context_digest",
                    "plan_revision_id",
                    "execution_result_ref",
                    "authority_bundle_ref",
                    "authority_bundle_digest",
                )
            },
        },
        "pair_material_snapshot": {
            "oneOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(PAIR_MATERIAL_SNAPSHOT_FIELDS),
                    "properties": {
                        "schema_version": {"const": PAIR_MATERIAL_SNAPSHOT_VERSION},
                        "intent_revision_id": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "plan_revision_id": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "target_metric_refs": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "metric_refs": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "scope": {"type": "object"},
                        "time_semantics": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": sorted(PAIR_TIME_SEMANTICS_FIELDS),
                        },
                        "active_material_decisions": {"type": "array"},
                        "user_required_obligation_coverage": {
                            "type": "array",
                            "minItems": 1,
                        },
                        "content_digest": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{64}$",
                        },
                    },
                },
            ]
        },
        "publication": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "state",
                "customer_payload_ref",
                "customer_payload_digest",
                "publication_ref",
                "publication_digest",
                "projection_id",
                "projection_digest",
                "customer_publication_event_observed",
            ],
            "properties": {
                "state": {"type": "string"},
                **{
                    field: {"type": ["string", "null"]}
                    for field in (
                        "customer_payload_ref",
                        "customer_payload_digest",
                        "publication_ref",
                        "publication_digest",
                        "projection_id",
                        "projection_digest",
                    )
                },
                "customer_publication_event_observed": {"type": "boolean"},
            },
        },
        "delivery": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "state",
                "outbox_ref",
                "attempt_ref",
                "customer_publication_ref",
                "failure_code",
            ],
            "properties": {
                "state": {"type": "string"},
                **{
                    field: {"type": ["string", "null"]}
                    for field in (
                        "outbox_ref",
                        "attempt_ref",
                        "customer_publication_ref",
                        "failure_code",
                    )
                },
            },
        },
        "llm_call_audits": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(LLM_AUDIT_FIELDS),
                "properties": {
                    **{
                        field: {"type": "string", "minLength": 1}
                        for field in (
                            "audit_kind",
                            "run_id",
                            "audit_ref",
                            "task",
                            "provider_ref",
                            "model_ref",
                            "status",
                            "acceptance_state",
                            "input_digest",
                        )
                    },
                    "attempt_number": {"type": "integer", "minimum": 1},
                    **{
                        field: {"type": ["string", "null"]}
                        for field in (
                            "input_ref",
                            "output_digest",
                            "started_at",
                            "finished_at",
                        )
                    },
                },
            },
        },
        "human_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(HUMAN_DECISION_FIELDS),
                "properties": {
                    **{
                        field: {"type": "string", "minLength": 1}
                        for field in (
                            "decision_ref",
                            "run_id",
                            "decision_kind",
                            "source",
                            "status",
                            "content_digest",
                            "recorded_at",
                        )
                    },
                    **{
                        field: {"type": ["string", "null"]}
                        for field in (
                            "slot_id",
                            "selected_option_id",
                            "materiality",
                        )
                    },
                },
            },
        },
        "terminal_state": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "run_status",
                "publication_state",
                "delivery_state",
                "acceptance_status",
                "reason",
            ],
            "properties": {
                "run_status": {"type": "string"},
                "publication_state": {"type": "string"},
                "delivery_state": {"type": "string"},
                "acceptance_status": {
                    "enum": [
                        "passed",
                        "waiting_for_human",
                        "not_evaluated",
                        "run_failed",
                        "delivery_failed",
                        "dependency_failed",
                        "contract_failed",
                    ]
                },
                "reason": {"type": "string", "minLength": 1},
            },
        },
    },
}


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(error)
    return value


def _string_tuple(value: Any, error: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(error)
    return normalized


def _canonical_json(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _content_digest(value: Any) -> str:
    return sha256(
        json.dumps(
            _canonical_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _sorted_unique_strings(value: Any, error: str) -> list[str]:
    return sorted(_string_tuple(value, error))


def _sorted_canonical_items(value: Any, error: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(error)
    items = [_canonical_json(item) for item in value]
    if any(not isinstance(item, Mapping) for item in items):
        raise ValueError(error)
    return sorted(
        items,
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def build_pair_material_snapshot(
    *,
    intent_revision_id: str,
    plan_revision_id: str,
    target_metric_refs: Sequence[str],
    analysis_axes: Sequence[Mapping[str, Any]],
    scope: Mapping[str, Any],
    intent_time_spec: Mapping[str, Any],
    resolved_window_refs: Sequence[str],
    context_window_specs: Sequence[Mapping[str, Any]],
    plan_decision_refs: Sequence[str],
    active_decisions: Sequence[Mapping[str, Any]],
    user_required_obligations: Sequence[Mapping[str, Any]],
    obligation_closure: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    intent_revision_id = _required_string(
        intent_revision_id, "pair_material_intent_revision_invalid"
    )
    plan_revision_id = _required_string(
        plan_revision_id, "pair_material_plan_revision_invalid"
    )
    targets = _sorted_unique_strings(
        target_metric_refs, "pair_material_metric_refs_invalid"
    )
    if not targets:
        raise ValueError("pair_material_metric_refs_invalid")
    if not isinstance(scope, Mapping) or not scope:
        raise ValueError("pair_material_scope_invalid")
    if not isinstance(intent_time_spec, Mapping) or not intent_time_spec:
        raise ValueError("pair_material_time_semantics_invalid")

    metric_refs = set(targets)
    normalized_axes = _sorted_canonical_items(
        analysis_axes, "pair_material_analysis_axes_invalid"
    )
    for axis in normalized_axes:
        metric_refs.update(
            _string_tuple(
                axis.get("target_metric_refs") or (),
                "pair_material_analysis_axes_invalid",
            )
        )
        metric_refs.update(
            _string_tuple(
                axis.get("metric_refs") or (),
                "pair_material_analysis_axes_invalid",
            )
        )
    if not metric_refs:
        raise ValueError("pair_material_metric_refs_invalid")

    expected_decision_refs = _string_tuple(
        plan_decision_refs, "pair_material_decision_refs_invalid"
    )
    decisions: list[dict[str, Any]] = []
    observed_decision_refs: list[str] = []
    for raw in active_decisions:
        if not isinstance(raw, Mapping):
            raise ValueError("pair_material_decisions_invalid")
        decision_ref = _required_string(
            raw.get("decision_ref"), "pair_material_decisions_invalid"
        )
        observed_decision_refs.append(decision_ref)
        if raw.get("materiality") not in {"material", "non_material"} or (
            raw.get("status") not in {"inferred", "user_confirmed"}
            or raw.get("value") is None
        ):
            raise ValueError("pair_material_decisions_invalid")
        option_id = raw.get("option_id")
        if option_id is not None:
            _required_string(option_id, "pair_material_decisions_invalid")
        source = _required_string(raw.get("source"), "pair_material_decisions_invalid")
        if source not in PAIR_DECISION_SOURCES:
            raise ValueError("pair_material_decisions_invalid")
        normalized = {
            "slot_id": _required_string(
                raw.get("slot_id"), "pair_material_decisions_invalid"
            ),
            "option_id": option_id,
            "source": source,
            "status": str(raw["status"]),
            "materiality": str(raw["materiality"]),
            "value": _canonical_json(raw["value"]),
            "affected_plan_fields": _sorted_unique_strings(
                raw.get("affected_plan_fields") or (),
                "pair_material_decisions_invalid",
            ),
        }
        if normalized["materiality"] == "material":
            decisions.append(normalized)
    if tuple(observed_decision_refs) != expected_decision_refs:
        raise ValueError("pair_material_decision_refs_invalid")
    decisions.sort(
        key=lambda item: (
            item["slot_id"],
            item["option_id"] or "",
            json.dumps(item["value"], ensure_ascii=False, sort_keys=True),
        )
    )

    closure_by_id: dict[str, Mapping[str, Any]] = {}
    for item in obligation_closure:
        if not isinstance(item, Mapping):
            raise ValueError("pair_material_obligation_coverage_invalid")
        obligation_id = _required_string(
            item.get("obligation_id"),
            "pair_material_obligation_coverage_invalid",
        )
        if obligation_id in closure_by_id:
            raise ValueError("pair_material_obligation_coverage_invalid")
        closure_by_id[obligation_id] = item

    coverage: list[dict[str, Any]] = []
    for obligation in user_required_obligations:
        if (
            not isinstance(obligation, Mapping)
            or obligation.get("role") != "user_required"
        ):
            raise ValueError("pair_material_obligation_coverage_invalid")
        obligation_id = _required_string(
            obligation.get("obligation_id"),
            "pair_material_obligation_coverage_invalid",
        )
        closure = closure_by_id.get(obligation_id)
        subject = obligation.get("subject")
        success_policy = obligation.get("success_policy")
        if (
            closure is None
            or not isinstance(subject, Mapping)
            or not isinstance(subject.get("scope"), Mapping)
            or not isinstance(success_policy, Mapping)
        ):
            raise ValueError("pair_material_obligation_coverage_invalid")
        coverage.append(
            {
                "obligation_id": obligation_id,
                "claim_kind": _required_string(
                    obligation.get("claim_kind"),
                    "pair_material_obligation_coverage_invalid",
                ),
                "target_metric_ref": _required_string(
                    subject.get("target_metric_ref"),
                    "pair_material_obligation_coverage_invalid",
                ),
                "scope": _canonical_json(subject["scope"]),
                "outcome_refs": _sorted_unique_strings(
                    subject.get("outcome_refs") or (),
                    "pair_material_obligation_coverage_invalid",
                ),
                "minimum_claim_strength": _required_string(
                    success_policy.get("minimum_claim_strength"),
                    "pair_material_obligation_coverage_invalid",
                ),
                "coverage_state": _required_string(
                    closure.get("coverage_state"),
                    "pair_material_obligation_coverage_invalid",
                ),
                "coverage_claim_refs": _sorted_unique_strings(
                    closure.get("coverage_claim_refs") or (),
                    "pair_material_obligation_coverage_invalid",
                ),
                "coverage_limitation_refs": _sorted_unique_strings(
                    closure.get("coverage_limitation_refs") or (),
                    "pair_material_obligation_coverage_invalid",
                ),
                "unavailable_limitation_refs": _sorted_unique_strings(
                    closure.get("unavailable_limitation_refs") or (),
                    "pair_material_obligation_coverage_invalid",
                ),
            }
        )
    if not coverage or {item["obligation_id"] for item in coverage} != set(
        closure_by_id
    ):
        raise ValueError("pair_material_obligation_coverage_invalid")
    coverage.sort(key=lambda item: item["obligation_id"])

    body = {
        "schema_version": PAIR_MATERIAL_SNAPSHOT_VERSION,
        "intent_revision_id": intent_revision_id,
        "plan_revision_id": plan_revision_id,
        "target_metric_refs": targets,
        "metric_refs": sorted(metric_refs),
        "scope": _canonical_json(scope),
        "time_semantics": {
            "intent_time_spec": _canonical_json(intent_time_spec),
            "resolved_window_refs": list(
                _string_tuple(
                    resolved_window_refs,
                    "pair_material_time_semantics_invalid",
                )
            ),
            "context_window_specs": _sorted_canonical_items(
                context_window_specs,
                "pair_material_time_semantics_invalid",
            ),
        },
        "active_material_decisions": decisions,
        "user_required_obligation_coverage": coverage,
    }
    snapshot = {**body, "content_digest": _content_digest(body)}
    validate_pair_material_snapshot(snapshot)
    return snapshot


def validate_pair_material_snapshot(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != PAIR_MATERIAL_SNAPSHOT_FIELDS:
        raise ValueError("pair_material_snapshot_shape_invalid")
    if value.get("schema_version") != PAIR_MATERIAL_SNAPSHOT_VERSION:
        raise ValueError("pair_material_snapshot_version_invalid")
    _required_string(
        value.get("intent_revision_id"), "pair_material_snapshot_authority_invalid"
    )
    _required_string(
        value.get("plan_revision_id"), "pair_material_snapshot_authority_invalid"
    )
    targets = _string_tuple(
        value.get("target_metric_refs"), "pair_material_snapshot_metrics_invalid"
    )
    metrics = _string_tuple(
        value.get("metric_refs"), "pair_material_snapshot_metrics_invalid"
    )
    if (
        not targets
        or not metrics
        or not isinstance(value.get("target_metric_refs"), list)
        or not isinstance(value.get("metric_refs"), list)
        or list(targets) != sorted(targets)
        or list(metrics) != sorted(metrics)
        or not set(targets) <= set(metrics)
    ):
        raise ValueError("pair_material_snapshot_metrics_invalid")
    if not isinstance(value.get("scope"), Mapping) or not value["scope"]:
        raise ValueError("pair_material_snapshot_scope_invalid")
    time_semantics = value.get("time_semantics")
    if (
        not isinstance(time_semantics, Mapping)
        or set(time_semantics) != PAIR_TIME_SEMANTICS_FIELDS
        or not isinstance(time_semantics.get("intent_time_spec"), Mapping)
        or not time_semantics["intent_time_spec"]
    ):
        raise ValueError("pair_material_snapshot_time_invalid")
    resolved_window_refs = _string_tuple(
        time_semantics.get("resolved_window_refs"),
        "pair_material_snapshot_time_invalid",
    )
    if not isinstance(time_semantics.get("resolved_window_refs"), list) or not (
        resolved_window_refs
    ):
        raise ValueError("pair_material_snapshot_time_invalid")
    context_specs = time_semantics.get("context_window_specs")
    if not isinstance(context_specs, list) or any(
        not isinstance(item, Mapping) for item in context_specs
    ):
        raise ValueError("pair_material_snapshot_time_invalid")
    if context_specs != _sorted_canonical_items(
        context_specs, "pair_material_snapshot_time_invalid"
    ):
        raise ValueError("pair_material_snapshot_time_invalid")
    decisions = value.get("active_material_decisions")
    if not isinstance(decisions, list):
        raise ValueError("pair_material_snapshot_decisions_invalid")
    for item in decisions:
        if (
            not isinstance(item, Mapping)
            or set(item) != PAIR_MATERIAL_DECISION_FIELDS
            or item.get("materiality") != "material"
            or item.get("status") not in {"inferred", "user_confirmed"}
            or item.get("source") not in PAIR_DECISION_SOURCES
            or item.get("value") is None
        ):
            raise ValueError("pair_material_snapshot_decisions_invalid")
        _required_string(
            item.get("slot_id"), "pair_material_snapshot_decisions_invalid"
        )
        if item.get("option_id") is not None:
            _required_string(
                item.get("option_id"),
                "pair_material_snapshot_decisions_invalid",
            )
        affected = item.get("affected_plan_fields")
        affected_refs = _string_tuple(
            affected, "pair_material_snapshot_decisions_invalid"
        )
        if not isinstance(affected, list) or list(affected_refs) != sorted(
            affected_refs
        ):
            raise ValueError("pair_material_snapshot_decisions_invalid")
    if len({item["slot_id"] for item in decisions}) != len(
        decisions
    ) or decisions != sorted(
        decisions,
        key=lambda item: (
            item["slot_id"],
            item["option_id"] or "",
            json.dumps(item["value"], ensure_ascii=False, sort_keys=True),
        ),
    ):
        raise ValueError("pair_material_snapshot_decisions_invalid")
    obligations = value.get("user_required_obligation_coverage")
    if not isinstance(obligations, list) or not obligations:
        raise ValueError("pair_material_snapshot_obligations_invalid")
    obligation_ids: list[str] = []
    for item in obligations:
        if (
            not isinstance(item, Mapping)
            or set(item) != PAIR_REQUIRED_OBLIGATION_FIELDS
        ):
            raise ValueError("pair_material_snapshot_obligations_invalid")
        obligation_ids.append(
            _required_string(
                item.get("obligation_id"),
                "pair_material_snapshot_obligations_invalid",
            )
        )
        _required_string(
            item.get("claim_kind"),
            "pair_material_snapshot_obligations_invalid",
        )
        target_metric_ref = _required_string(
            item.get("target_metric_ref"),
            "pair_material_snapshot_obligations_invalid",
        )
        if (
            target_metric_ref not in metrics
            or not isinstance(item.get("scope"), Mapping)
            or not item["scope"]
        ):
            raise ValueError("pair_material_snapshot_obligations_invalid")
        _required_string(
            item.get("minimum_claim_strength"),
            "pair_material_snapshot_obligations_invalid",
        )
        ref_fields = (
            "outcome_refs",
            "coverage_claim_refs",
            "coverage_limitation_refs",
            "unavailable_limitation_refs",
        )
        refs: dict[str, tuple[str, ...]] = {}
        for field in ref_fields:
            raw_refs = item.get(field)
            normalized_refs = _string_tuple(
                raw_refs, "pair_material_snapshot_obligations_invalid"
            )
            if not isinstance(raw_refs, list) or list(normalized_refs) != sorted(
                normalized_refs
            ):
                raise ValueError("pair_material_snapshot_obligations_invalid")
            refs[field] = normalized_refs
        if not refs["outcome_refs"]:
            raise ValueError("pair_material_snapshot_obligations_invalid")
        state = item.get("coverage_state")
        if state not in {"satisfied", "mixed", "contradicted", "unavailable"}:
            raise ValueError("pair_material_snapshot_obligations_invalid")
        if not set(refs["unavailable_limitation_refs"]) <= set(
            refs["coverage_limitation_refs"]
        ):
            raise ValueError("pair_material_snapshot_obligations_invalid")
        if state == "unavailable":
            if refs["coverage_claim_refs"] or not refs["coverage_limitation_refs"]:
                raise ValueError("pair_material_snapshot_obligations_invalid")
        elif not refs["coverage_claim_refs"]:
            raise ValueError("pair_material_snapshot_obligations_invalid")
    if obligation_ids != sorted(obligation_ids) or len(obligation_ids) != len(
        set(obligation_ids)
    ):
        raise ValueError("pair_material_snapshot_obligations_invalid")
    body = {
        key: _canonical_json(item)
        for key, item in value.items()
        if key != "content_digest"
    }
    digest = value.get("content_digest")
    if not isinstance(digest, str) or digest != _content_digest(body):
        raise ValueError("pair_material_snapshot_digest_invalid")


def validate_manifest(raw: Mapping[str, Any]) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "artifact",
        "execution_contract",
        "question_family_pairs",
        "cases",
    }:
        raise ValueError("business_expectation_document_invalid")
    if raw.get("version") != "1" or raw.get("artifact") != (
        "real_gateway_business_question_expectations"
    ):
        raise ValueError("business_expectation_document_invalid")
    if raw.get("execution_contract") != EXECUTION_CONTRACT:
        raise ValueError("business_expectation_execution_contract_invalid")
    pairs = raw.get("question_family_pairs")
    if not isinstance(pairs, Mapping) or set(pairs) != set(QUESTION_FAMILIES):
        raise ValueError("business_expectation_family_pairs_invalid")
    cases = raw.get("cases")
    if not isinstance(cases, list):
        raise ValueError("business_expectation_cases_invalid")

    case_ids: set[str] = set()
    for raw_case in cases:
        if not isinstance(raw_case, Mapping) or set(raw_case) != CASE_FIELDS:
            raise ValueError("business_expectation_case_shape_invalid")
        case_id = _required_string(
            raw_case.get("case_id"), "business_expectation_case_value_invalid"
        )
        _required_string(
            raw_case.get("user_message"), "business_expectation_case_value_invalid"
        )
        _required_string(
            raw_case.get("review_focus"), "business_expectation_case_value_invalid"
        )
        if case_id in case_ids:
            raise ValueError("business_expectation_case_id_duplicate")
        case_ids.add(case_id)

    mapped_case_ids: set[str] = set()
    for family in QUESTION_FAMILIES:
        pair = pairs[family]
        if not isinstance(pair, Mapping) or set(pair) != PAIR_FIELDS:
            raise ValueError("business_expectation_family_pair_shape_invalid")
        original = _required_string(
            pair.get("original_case_id"),
            "business_expectation_family_pair_value_invalid",
        )
        paraphrase = _required_string(
            pair.get("paraphrase_case_id"),
            "business_expectation_family_pair_value_invalid",
        )
        if original == paraphrase or {original, paraphrase} - case_ids:
            raise ValueError("business_expectation_family_pair_value_invalid")
        if original in mapped_case_ids or paraphrase in mapped_case_ids:
            raise ValueError("business_expectation_family_case_reused")
        mapped_case_ids.update((original, paraphrase))


def load_manifest(path: str) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    validate_manifest(raw)
    return raw


def load_cases(path: str) -> list[dict[str, Any]]:
    manifest = load_manifest(path)
    variants: dict[str, tuple[str, str]] = {}
    for family, pair in manifest["question_family_pairs"].items():
        variants[pair["original_case_id"]] = (family, "original")
        variants[pair["paraphrase_case_id"]] = (family, "paraphrase")
    return [
        {
            "id": raw_case["case_id"],
            "question_family": variants.get(raw_case["case_id"], (None, "additional"))[
                0
            ],
            "variant": variants.get(raw_case["case_id"], (None, "additional"))[1],
            "turns": [
                {
                    "user": raw_case["user_message"],
                    "review_focus": raw_case["review_focus"],
                }
            ],
        }
        for raw_case in manifest["cases"]
    ]


def select_cases(
    cases: list[dict[str, Any]], case_id: str | None
) -> list[dict[str, Any]]:
    if not case_id:
        return cases
    return [case for case in cases if case["id"] == case_id]


def resolve_cli_cases(
    cases_path: str | None,
    case_id: str | None,
) -> list[dict[str, Any]]:
    if not cases_path:
        raise ValueError("eval_case_source_required")
    if not case_id:
        raise ValueError("eval_case_id_required")
    selected = select_cases(load_cases(cases_path), case_id)
    if not selected:
        raise ValueError("eval_case_unknown")
    return selected


def load_env_file(path: str = ".env") -> list[str]:
    env_path = Path(path)
    if not env_path.exists():
        return []
    loaded: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_env_value(value.strip())
        loaded.append(key)
    return loaded


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if " #" in value:
        return value.split(" #", 1)[0].strip()
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _dependency_health(base_url: str, user_id: str) -> dict[str, Any]:
    payload = gateway_once._json_request(
        base_url,
        "/api/health",
        user_id=user_id,
    )
    raw_checks = payload.get("checks")
    if (
        payload.get("status") not in {"ok", "degraded"}
        or not isinstance(raw_checks, list)
        or any(not isinstance(item, Mapping) for item in raw_checks)
    ):
        raise RuntimeError("gateway_health_contract_invalid")
    by_name = {str(item.get("name") or ""): item for item in raw_checks}
    required = (
        ("gateway", "frontend_gateway"),
        ("postgres", "postgres_runtime_store"),
        ("clickhouse", "clickhouse_access"),
        ("deepseek", "llm_access"),
    )
    checks: list[dict[str, str]] = []
    for dependency, check_name in required:
        check = by_name.get(check_name)
        if not isinstance(check, Mapping) or check.get("status") not in {
            "ok",
            "failed",
        }:
            raise RuntimeError(f"gateway_health_check_missing:{check_name}")
        checks.append(
            {
                "dependency": dependency,
                "gateway_check": check_name,
                "status": str(check["status"]),
                "detail": str(check.get("detail") or ""),
            }
        )
    health = {
        "checked_at": _utc_now(),
        "overall_status": str(payload["status"]),
        "checks": checks,
    }
    if health["overall_status"] != "ok" or any(
        check["status"] != "ok" for check in checks
    ):
        raise RuntimeError("acceptance_dependency_health_failed")
    return health


def _connect_runtime_database() -> Any:
    database_url = os.environ.get("WAJE_RUNTIME_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )
    if not database_url:
        raise RuntimeError("runtime_database_url_required")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("psycopg_required") from exc
    return psycopg.connect(database_url, autocommit=True, row_factory=dict_row)


def _fetch_one(
    connection: Any, statement: str, parameters: tuple[Any, ...]
) -> dict[str, Any] | None:
    row = connection.execute(statement, parameters).fetchone()
    return dict(row) if row is not None else None


def _fetch_all(
    connection: Any, statement: str, parameters: tuple[Any, ...]
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(statement, parameters).fetchall()]


def _read_run_state(connection: Any, run_id: str) -> dict[str, Any] | None:
    return _fetch_one(
        connection,
        """
        SELECT
          run.run_id,
          run.thread_id,
          run.status AS run_status,
          run.request ->> 'post_execution_status' AS post_execution_status,
          lifecycle.execution_state,
          lifecycle.interaction_state,
          lifecycle.evidence_state,
          COALESCE(
            NULLIF(run.request ->> 'publication_status', ''),
            lifecycle.publication_state
          ) AS publication_state,
          COALESCE(
            NULLIF(run.request ->> 'delivery_status', ''),
            lifecycle.delivery_state
          ) AS delivery_state,
          lifecycle.retry_state,
          lifecycle.cancellation_state,
          lifecycle.supersession_state,
          lifecycle.state_revision,
          failure.status AS post_seal_failure_status,
          failure.terminal_ref AS post_seal_failure_terminal_ref
        FROM waje_runtime.analysis_runs run
        LEFT JOIN LATERAL (
          SELECT *
          FROM waje_runtime.run_lifecycle_state_revisions revision
          WHERE revision.run_attempt_id = run.run_attempt_id
          ORDER BY revision.state_revision DESC
          LIMIT 1
        ) lifecycle ON true
        LEFT JOIN LATERAL (
          SELECT terminal.status, terminal.terminal_ref
          FROM waje_runtime.post_seal_failure_terminals terminal
          WHERE terminal.run_attempt_id = run.run_id
          ORDER BY terminal.attempt_number DESC
          LIMIT 1
        ) failure ON true
        WHERE run.run_id = %s
        """,
        (run_id,),
    )


def _is_terminal_snapshot(snapshot: Mapping[str, Any]) -> bool:
    status = str(snapshot.get("run_status") or "")
    if status in RUN_CHECKPOINTS:
        return True
    if status != "completed":
        return False
    if snapshot.get("post_execution_status") in POST_EXECUTION_FAILURE_STATES:
        return True
    delivery_state = str(snapshot.get("delivery_state") or "")
    return delivery_state in FINAL_DELIVERY_STATES


def _unavailable_authority_records() -> dict[str, Any]:
    return {
        "active_release_refs": {
            "actual_as_of": None,
            "release_refs": [],
            "snapshot_refs": [],
        },
        "authority_refs": {
            "intent_revision_id": None,
            "authority_context_ref": None,
            "authority_context_digest": None,
            "plan_revision_id": None,
            "execution_result_ref": None,
            "authority_bundle_ref": None,
            "authority_bundle_digest": None,
        },
        "pair_material_snapshot": None,
        "required_obligation_publication_closure": {
            "authority_mode": None,
            "verified_claim_refs": [],
            "obligations": [],
        },
    }


def _wait_for_terminal_snapshot(
    connection: Any,
    run_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    deadline = monotonic() + timeout_seconds
    last_snapshot: dict[str, Any] | None = None
    while True:
        last_snapshot = _read_run_state(connection, run_id)
        if last_snapshot is not None and _is_terminal_snapshot(last_snapshot):
            return last_snapshot
        remaining = deadline - monotonic()
        if remaining <= 0:
            status = str((last_snapshot or {}).get("run_status") or "missing")
            raise RuntimeError(f"acceptance_terminal_timeout:{status}")
        sleep(min(poll_interval_seconds, remaining))


def _authority_records(connection: Any, run_id: str) -> dict[str, Any]:
    row = _fetch_one(
        connection,
        """
        SELECT
          bundle.intent_revision_id,
          intent.payload -> 'target_metric_refs' AS target_metric_refs,
          intent.payload -> 'scope' AS intent_scope,
          intent.payload -> 'time_spec' AS intent_time_spec,
          context.authority_context_ref,
          context.actual_as_of,
          context.content_digest AS authority_context_digest,
          context.payload -> 'release_refs' AS release_refs,
          context.payload -> 'snapshot_refs' AS snapshot_refs,
          plan.plan_revision_id,
          plan.payload -> 'decision_refs' AS decision_refs,
          plan.payload -> 'resolved_window_refs' AS resolved_window_refs,
          plan.payload -> 'context_window_specs' AS context_window_specs,
          plan.payload -> 'analysis_axes' AS analysis_axes,
          plan.payload -> 'claim_obligations' AS claim_obligations,
          bundle.execution_result_ref,
          bundle.bundle_ref AS authority_bundle_ref,
          bundle.bundle_digest AS authority_bundle_digest,
          bundle.authority_mode,
          bundle.payload -> 'verified_claim_refs' AS verified_claim_refs
        FROM waje_runtime.analysis_runs run
        LEFT JOIN waje_runtime.authority_bundles bundle
          ON bundle.run_attempt_id = run.run_attempt_id
         AND bundle.seal_state = 'sealed'
        LEFT JOIN waje_runtime.intent_revisions intent
          ON intent.run_attempt_id = run.run_attempt_id
         AND intent.intent_revision_id = bundle.intent_revision_id
        LEFT JOIN waje_runtime.authority_contexts context
          ON context.run_attempt_id = run.run_attempt_id
         AND context.authority_context_ref = bundle.authority_context_ref
        LEFT JOIN waje_runtime.plan_revisions plan
          ON plan.run_attempt_id = run.run_attempt_id
         AND plan.plan_revision_id = bundle.plan_revision_id
        WHERE run.run_id = %s
        """,
        (run_id,),
    )
    if row is None:
        raise RuntimeError("acceptance_run_missing")
    release_refs = _string_tuple(
        row.get("release_refs") or (), "active_release_refs_invalid"
    )
    snapshot_refs = _string_tuple(
        row.get("snapshot_refs") or (), "active_snapshot_refs_invalid"
    )
    raw_obligations = row.get("claim_obligations")
    if not isinstance(raw_obligations, list):
        raise RuntimeError("required_obligation_records_invalid")
    user_required = {
        str(item.get("obligation_id") or ""): item
        for item in raw_obligations
        if isinstance(item, Mapping) and item.get("role") == "user_required"
    }
    if not user_required or "" in user_required:
        raise RuntimeError("required_obligation_records_invalid")
    basis_rows = _fetch_all(
        connection,
        """
        SELECT
          obligation_id,
          payload -> 'proposed_claim_refs' AS proposed_claim_refs,
          payload -> 'unavailable_limitation_refs' AS unavailable_limitation_refs
        FROM waje_runtime.claim_obligation_settlement_bases
        WHERE run_attempt_id = %s
        """,
        (run_id,),
    )
    coverage_rows = _fetch_all(
        connection,
        """
        SELECT
          obligation_id,
          payload -> 'claim_refs' AS claim_refs,
          payload -> 'limitation_refs' AS limitation_refs,
          coverage_state
        FROM waje_runtime.claim_obligation_coverages
        WHERE run_attempt_id = %s
        """,
        (run_id,),
    )
    basis_by_obligation = {str(item["obligation_id"]): item for item in basis_rows}
    coverage_by_obligation = {
        str(item["obligation_id"]): item for item in coverage_rows
    }
    if len(basis_by_obligation) != len(basis_rows) or len(
        coverage_by_obligation
    ) != len(coverage_rows):
        raise RuntimeError("required_obligation_records_invalid")
    obligation_closure = []
    for obligation_id in sorted(user_required):
        basis = basis_by_obligation.get(obligation_id)
        coverage = coverage_by_obligation.get(obligation_id)
        if basis is None or coverage is None:
            raise RuntimeError("required_obligation_records_invalid")
        obligation_closure.append(
            {
                "obligation_id": obligation_id,
                "proposed_claim_refs": list(
                    _string_tuple(
                        basis.get("proposed_claim_refs") or (),
                        "required_obligation_records_invalid",
                    )
                ),
                "unavailable_limitation_refs": list(
                    _string_tuple(
                        basis.get("unavailable_limitation_refs") or (),
                        "required_obligation_records_invalid",
                    )
                ),
                "coverage_claim_refs": list(
                    _string_tuple(
                        coverage.get("claim_refs") or (),
                        "required_obligation_records_invalid",
                    )
                ),
                "coverage_limitation_refs": list(
                    _string_tuple(
                        coverage.get("limitation_refs") or (),
                        "required_obligation_records_invalid",
                    )
                ),
                "coverage_state": str(coverage.get("coverage_state") or ""),
            }
        )
    active_decisions = _fetch_all(
        connection,
        """
        SELECT
          decision_id AS decision_ref,
          slot_id,
          option_id,
          source,
          status,
          materiality,
          payload -> 'value' AS value,
          payload -> 'affected_plan_fields' AS affected_plan_fields
        FROM waje_runtime.decision_records
        WHERE run_attempt_id = %s
          AND intent_revision_id = %s
          AND invalidated_by_revision_id IS NULL
          AND status IN ('inferred', 'user_confirmed')
        ORDER BY ledger_position
        """,
        (run_id, str(row.get("intent_revision_id") or "")),
    )
    pair_material_snapshot = build_pair_material_snapshot(
        intent_revision_id=str(row.get("intent_revision_id") or ""),
        plan_revision_id=str(row.get("plan_revision_id") or ""),
        target_metric_refs=_string_tuple(
            row.get("target_metric_refs") or (),
            "pair_material_metric_refs_invalid",
        ),
        analysis_axes=row.get("analysis_axes") or (),
        scope=row.get("intent_scope") or {},
        intent_time_spec=row.get("intent_time_spec") or {},
        resolved_window_refs=_string_tuple(
            row.get("resolved_window_refs") or (),
            "pair_material_time_semantics_invalid",
        ),
        context_window_specs=row.get("context_window_specs") or (),
        plan_decision_refs=_string_tuple(
            row.get("decision_refs") or (),
            "pair_material_decision_refs_invalid",
        ),
        active_decisions=active_decisions,
        user_required_obligations=[
            user_required[obligation_id] for obligation_id in sorted(user_required)
        ],
        obligation_closure=obligation_closure,
    )
    return {
        "active_release_refs": {
            "actual_as_of": _iso(row.get("actual_as_of")),
            "release_refs": list(release_refs),
            "snapshot_refs": list(snapshot_refs),
        },
        "authority_refs": {
            "intent_revision_id": row.get("intent_revision_id"),
            "authority_context_ref": row.get("authority_context_ref"),
            "authority_context_digest": row.get("authority_context_digest"),
            "plan_revision_id": row.get("plan_revision_id"),
            "execution_result_ref": row.get("execution_result_ref"),
            "authority_bundle_ref": row.get("authority_bundle_ref"),
            "authority_bundle_digest": row.get("authority_bundle_digest"),
        },
        "pair_material_snapshot": pair_material_snapshot,
        "required_obligation_publication_closure": {
            "authority_mode": row.get("authority_mode"),
            "verified_claim_refs": list(
                _string_tuple(
                    row.get("verified_claim_refs") or (),
                    "required_obligation_records_invalid",
                )
            ),
            "obligations": obligation_closure,
        },
    }


def _required_obligation_publication_closed(
    authority_records: Mapping[str, Any],
    persisted_publication: Mapping[str, Any],
) -> bool:
    closure = authority_records.get("required_obligation_publication_closure")
    if not isinstance(closure, Mapping):
        return False
    obligations = closure.get("obligations")
    if not isinstance(obligations, list) or not obligations:
        return False
    verified_claim_refs = set(closure.get("verified_claim_refs") or ())
    customer_publication = persisted_publication.get("customer_publication")
    if not isinstance(customer_publication, Mapping):
        return False
    published_claim_refs = set(customer_publication.get("claim_refs") or ())
    published_limitation_refs = set(customer_publication.get("limitation_refs") or ())
    for obligation in obligations:
        if not isinstance(obligation, Mapping):
            return False
        coverage_claim_refs = set(obligation.get("coverage_claim_refs") or ())
        unavailable_limitation_refs = set(
            obligation.get("unavailable_limitation_refs") or ()
        )
        coverage_limitation_refs = set(obligation.get("coverage_limitation_refs") or ())
        if not unavailable_limitation_refs <= coverage_limitation_refs:
            return False
        coverage_state = obligation.get("coverage_state")
        if coverage_state in {"satisfied", "mixed", "contradicted"}:
            if not (coverage_claim_refs & verified_claim_refs & published_claim_refs):
                return False
            if not coverage_limitation_refs <= published_limitation_refs:
                return False
            continue
        if coverage_state == "unavailable":
            if (
                coverage_claim_refs
                or not coverage_limitation_refs
                or not coverage_limitation_refs <= published_limitation_refs
            ):
                return False
            continue
        return False
    return True


def _persisted_publication(connection: Any, run_id: str) -> dict[str, Any] | None:
    row = _fetch_one(
        connection,
        """
        SELECT
          customer.customer_payload_ref,
          customer.customer_payload,
          bundle.bundle_ref AS authority_bundle_ref,
          bundle.bundle_digest AS authority_bundle_digest,
          publication.publication_ref,
          publication.publication_digest,
          projection.projection_id,
          projection.projection_digest,
          outbox.outbox_ref,
          delivery.attempt_ref,
          delivery.status AS delivery_status,
          delivery.failure_code,
          delivered.customer_publication_ref
        FROM waje_runtime.publication_customer_payloads customer
        JOIN waje_runtime.delivery_outbox_records outbox
          ON outbox.owner_ref = customer.owner_ref
         AND outbox.run_attempt_id = customer.run_attempt_id
         AND outbox.outbox_ref = customer.outbox_ref
        JOIN waje_runtime.publication_revisions publication
          ON publication.owner_ref = customer.owner_ref
         AND publication.run_attempt_id = customer.run_attempt_id
         AND publication.publication_ref = customer.publication_ref
         AND publication.publication_digest = customer.publication_digest
         AND publication.publication_digest = outbox.publication_digest
        JOIN waje_runtime.publication_projections projection
          ON projection.owner_ref = customer.owner_ref
         AND projection.run_attempt_id = customer.run_attempt_id
         AND projection.projection_id = customer.projection_id
         AND projection.projection_digest = customer.projection_digest
         AND projection.projection_digest = outbox.projection_digest
        JOIN waje_runtime.authority_bundles bundle
          ON bundle.owner_ref = customer.owner_ref
         AND bundle.run_attempt_id = customer.run_attempt_id
         AND bundle.bundle_ref = outbox.authority_bundle_ref
         AND bundle.bundle_digest = outbox.authority_bundle_digest
        JOIN LATERAL (
          SELECT attempt.attempt_ref, attempt.status, attempt.failure_code
          FROM waje_runtime.delivery_attempts attempt
          WHERE attempt.owner_ref = customer.owner_ref
            AND attempt.run_attempt_id = customer.run_attempt_id
            AND attempt.outbox_ref = customer.outbox_ref
          ORDER BY attempt.attempt_number DESC
          LIMIT 1
        ) delivery ON true
        LEFT JOIN waje_runtime.customer_publications delivered
          ON delivered.owner_ref = customer.owner_ref
         AND delivered.run_attempt_id = customer.run_attempt_id
         AND delivered.outbox_ref = customer.outbox_ref
        WHERE customer.run_attempt_id = %s
        ORDER BY publication.revision DESC, customer.created_at DESC
        LIMIT 1
        """,
        (run_id,),
    )
    if row is None:
        return None
    customer_publication = gateway_once._require_customer_publication(
        row.get("customer_payload")
    )
    safe_publication = gateway_once._require_safe_publication(
        {
            "authority_bundle_ref": row.get("authority_bundle_ref"),
            "authority_bundle_digest": row.get("authority_bundle_digest"),
            "publication_ref": row.get("publication_ref"),
            "publication_digest": row.get("publication_digest"),
            "projection_id": row.get("projection_id"),
            "projection_digest": row.get("projection_digest"),
            "outbox_ref": row.get("outbox_ref"),
            "delivery_status": row.get("delivery_status"),
        }
    )
    customer_payload_digest = sha256(
        json.dumps(
            customer_publication,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "customer_publication": customer_publication,
        "safe_publication": safe_publication,
        "customer_payload_ref": row.get("customer_payload_ref"),
        "customer_payload_digest": customer_payload_digest,
        "attempt_ref": row.get("attempt_ref"),
        "failure_code": row.get("failure_code"),
        "customer_publication_ref": row.get("customer_publication_ref"),
    }


def _llm_call_audits(connection: Any, run_ids: Sequence[str]) -> list[dict[str, Any]]:
    audits: list[dict[str, Any]] = []
    for run_id in run_ids:
        transitions = _fetch_all(
            connection,
            """
            SELECT
              attempt_id,
              node_name,
              provider_ref,
              model_ref,
              status,
              acceptance_state,
              input_digest,
              output_digest,
              execution_attempt,
              started_at,
              finished_at
            FROM waje_runtime.workflow_transition_attempts
            WHERE run_attempt_id = %s
            ORDER BY created_at, attempt_id
            """,
            (run_id,),
        )
        audits.extend(
            {
                "audit_kind": "workflow_transition_attempt",
                "run_id": run_id,
                "audit_ref": row["attempt_id"],
                "task": row["node_name"],
                "provider_ref": row["provider_ref"],
                "model_ref": row["model_ref"],
                "status": row["status"],
                "acceptance_state": row["acceptance_state"],
                "attempt_number": row["execution_attempt"],
                "input_ref": None,
                "input_digest": row["input_digest"],
                "output_digest": row.get("output_digest"),
                "started_at": _iso(row.get("started_at")),
                "finished_at": _iso(row.get("finished_at")),
            }
            for row in transitions
        )
        responses = _fetch_all(
            connection,
            """
            SELECT
              provider_response_ref,
              purpose,
              provider_ref,
              model_ref,
              input_ref,
              input_digest,
              attempt_number,
              content_digest,
              created_at
            FROM waje_runtime.restricted_provider_responses
            WHERE run_attempt_id = %s
            ORDER BY created_at, provider_response_ref
            """,
            (run_id,),
        )
        audits.extend(
            {
                "audit_kind": "restricted_provider_response",
                "run_id": run_id,
                "audit_ref": row["provider_response_ref"],
                "task": row["purpose"],
                "provider_ref": row["provider_ref"],
                "model_ref": row["model_ref"],
                "status": "persisted",
                "acceptance_state": "accepted",
                "attempt_number": row["attempt_number"],
                "input_ref": row["input_ref"],
                "input_digest": row["input_digest"],
                "output_digest": row["content_digest"],
                "started_at": _iso(row.get("created_at")),
                "finished_at": _iso(row.get("created_at")),
            }
            for row in responses
        )
    return audits


def _human_decisions(
    connection: Any,
    run_ids: Sequence[str],
    submitted_decision: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for run_id in run_ids:
        rows = _fetch_all(
            connection,
            """
            SELECT
              decision_id,
              slot_id,
              option_id,
              source,
              status,
              materiality,
              content_digest,
              created_at
            FROM waje_runtime.decision_records
            WHERE run_attempt_id = %s
              AND source IN ('user', 'accepted_recommendation')
            ORDER BY ledger_position, decision_id
            """,
            (run_id,),
        )
        decisions.extend(
            {
                "decision_ref": row["decision_id"],
                "run_id": run_id,
                "decision_kind": (
                    "selected_option" if row.get("option_id") else "free_text"
                ),
                "slot_id": row.get("slot_id"),
                "selected_option_id": row.get("option_id"),
                "source": row.get("source"),
                "status": row.get("status"),
                "materiality": row.get("materiality"),
                "content_digest": row.get("content_digest"),
                "recorded_at": _iso(row.get("created_at")),
            }
            for row in rows
        )
    if submitted_decision is not None:
        decisions.append(dict(submitted_decision))
    return decisions


def _deepseek_audit_observed(audits: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        audit.get("audit_kind") == "workflow_transition_attempt"
        and audit.get("status") == "succeeded"
        and audit.get("acceptance_state") == "accepted"
        and "deepseek"
        in (f"{audit.get('provider_ref') or ''} {audit.get('model_ref') or ''}").lower()
        for audit in audits
    )


def _event_publication(
    base_url: str,
    user_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    events = gateway_once._events(
        base_url,
        f"/api/runs/{run_id}/events",
        user_id=user_id,
    )
    return gateway_once._customer_publication_ready(events)


def _acceptance_status(
    *,
    snapshot: Mapping[str, Any],
    dependency_health: Mapping[str, Any],
    authority_records: Mapping[str, Any],
    persisted_publication: Mapping[str, Any] | None,
    event_publication: Mapping[str, Any] | None,
    llm_call_audits: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    run_status = str(snapshot.get("run_status") or "")
    publication_state = str(snapshot.get("publication_state") or "not_ready")
    delivery_state = str(snapshot.get("delivery_state") or "pending")
    if run_status == "waiting_for_clarification":
        return "waiting_for_human", "human_clarification_required"
    if run_status == "interaction_completed":
        return "not_evaluated", "interaction_completed_without_analysis"
    if run_status == "failed":
        return "run_failed", "analysis_run_failed"
    if run_status != "completed":
        return "not_evaluated", f"phase_checkpoint:{run_status}"
    post_execution_status = str(snapshot.get("post_execution_status") or "")
    if post_execution_status in POST_EXECUTION_FAILURE_STATES:
        expected_publication, expected_delivery = POST_EXECUTION_FAILURE_STATES[
            post_execution_status
        ]
        if (
            snapshot.get("post_seal_failure_status") != post_execution_status
            or not snapshot.get("post_seal_failure_terminal_ref")
            or snapshot.get("execution_state") != "complete"
            or snapshot.get("interaction_state") != "active"
            or snapshot.get("evidence_state") not in {"complete", "boundary_only"}
            or publication_state != expected_publication
            or delivery_state != expected_delivery
            or snapshot.get("retry_state") != "exhausted"
            or snapshot.get("cancellation_state") != "active"
            or snapshot.get("supersession_state") != "active"
        ):
            return "contract_failed", "post_execution_failure_terminal_invalid"
        if persisted_publication is not None or event_publication is not None:
            return "contract_failed", "failed_customer_publication_forbidden"
        return "run_failed", f"post_execution_{post_execution_status}"
    if (
        snapshot.get("execution_state") != "complete"
        or snapshot.get("interaction_state") != "active"
        or snapshot.get("evidence_state") not in {"complete", "boundary_only"}
        or snapshot.get("retry_state") != "idle"
        or snapshot.get("cancellation_state") != "active"
        or snapshot.get("supersession_state") != "active"
    ):
        return "contract_failed", "terminal_lifecycle_state_invalid"
    if delivery_state in {"retryable_failed", "permanently_failed"}:
        if publication_state != "ready":
            return "contract_failed", "publication_delivery_state_mismatch"
        if persisted_publication is None:
            return "contract_failed", "failed_delivery_persistence_missing"
        safe_publication = persisted_publication["safe_publication"]
        if safe_publication.get("delivery_status") != delivery_state:
            return "contract_failed", "delivery_state_ref_mismatch"
        if not persisted_publication.get("attempt_ref"):
            return "contract_failed", "delivery_attempt_ref_missing"
        if (
            not persisted_publication.get("failure_code")
            or persisted_publication.get("customer_publication_ref") is not None
        ):
            return "contract_failed", "failed_delivery_ref_closure_invalid"
        if event_publication is not None and (
            persisted_publication.get("customer_publication")
            != event_publication.get("customer_publication")
            or persisted_publication.get("safe_publication")
            != event_publication.get("publication")
        ):
            return (
                "contract_failed",
                "customer_publication_event_persistence_mismatch",
            )
        return "delivery_failed", f"delivery_{delivery_state}"
    if delivery_state != "published":
        return "contract_failed", "delivery_terminal_state_invalid"
    if publication_state != "published":
        return "contract_failed", "publication_delivery_state_mismatch"
    if persisted_publication is None or event_publication is None:
        return "contract_failed", "customer_publication_ready_missing"
    if persisted_publication.get("customer_publication") != event_publication.get(
        "customer_publication"
    ) or persisted_publication.get("safe_publication") != event_publication.get(
        "publication"
    ):
        return "contract_failed", "customer_publication_event_persistence_mismatch"
    safe_publication = persisted_publication["safe_publication"]
    if safe_publication.get("delivery_status") != delivery_state:
        return "contract_failed", "delivery_state_ref_mismatch"
    if not persisted_publication.get("attempt_ref"):
        return "contract_failed", "delivery_attempt_ref_missing"
    if (
        not persisted_publication.get("customer_publication_ref")
        or persisted_publication.get("failure_code") is not None
    ):
        return "contract_failed", "published_delivery_ref_closure_invalid"
    if dependency_health.get("overall_status") != "ok":
        return "dependency_failed", "dependency_health_not_ok"
    active_release_refs = authority_records["active_release_refs"]["release_refs"]
    authority_refs = authority_records["authority_refs"]
    if not active_release_refs:
        return "contract_failed", "active_release_refs_missing"
    if any(
        not authority_refs.get(field)
        for field in (
            "authority_context_ref",
            "plan_revision_id",
            "execution_result_ref",
            "authority_bundle_ref",
            "authority_bundle_digest",
        )
    ):
        return "contract_failed", "authority_ref_closure_missing"
    if not _required_obligation_publication_closed(
        authority_records,
        persisted_publication,
    ):
        return (
            "contract_failed",
            "required_obligation_publication_closure_missing",
        )
    if not llm_call_audits:
        return "contract_failed", "llm_call_audits_missing"
    if not _deepseek_audit_observed(llm_call_audits):
        return "contract_failed", "deepseek_call_audit_missing"
    return "passed", "persisted_customer_publication_verified"


def build_acceptance_summary(
    *,
    case: Mapping[str, Any],
    dependency_health: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    run_ids: Sequence[str],
    authority_records: Mapping[str, Any],
    persisted_publication: Mapping[str, Any] | None,
    event_publication: Mapping[str, Any] | None,
    llm_call_audits: Sequence[Mapping[str, Any]],
    human_decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    turn = case["turns"][0]
    acceptance_status, reason = _acceptance_status(
        snapshot=snapshot,
        dependency_health=dependency_health,
        authority_records=authority_records,
        persisted_publication=persisted_publication,
        event_publication=event_publication,
        llm_call_audits=llm_call_audits,
    )
    safe_publication = (
        persisted_publication.get("safe_publication")
        if persisted_publication is not None
        else {}
    )
    summary = {
        "schema_version": ACCEPTANCE_SUMMARY_VERSION,
        "acceptance_source": ACCEPTANCE_SOURCE,
        "case": {
            "case_id": case["id"],
            "question_family": case.get("question_family"),
            "variant": case["variant"],
            "user_message": turn["user"],
            "review_focus": turn["review_focus"],
        },
        "dependency_health": dict(dependency_health),
        "execution": {
            "thread_id": snapshot["thread_id"],
            "run_ids": list(dict.fromkeys(run_ids)),
            "final_run_id": snapshot["run_id"],
        },
        "active_release_refs": dict(authority_records["active_release_refs"]),
        "authority_refs": dict(authority_records["authority_refs"]),
        "pair_material_snapshot": (
            dict(authority_records["pair_material_snapshot"])
            if authority_records.get("pair_material_snapshot") is not None
            else None
        ),
        "publication": {
            "state": str(snapshot.get("publication_state") or "not_ready"),
            "customer_payload_ref": (
                persisted_publication.get("customer_payload_ref")
                if persisted_publication is not None
                else None
            ),
            "customer_payload_digest": (
                persisted_publication.get("customer_payload_digest")
                if persisted_publication is not None
                else None
            ),
            "publication_ref": safe_publication.get("publication_ref"),
            "publication_digest": safe_publication.get("publication_digest"),
            "projection_id": safe_publication.get("projection_id"),
            "projection_digest": safe_publication.get("projection_digest"),
            "customer_publication_event_observed": event_publication is not None,
        },
        "delivery": {
            "state": str(snapshot.get("delivery_state") or "pending"),
            "outbox_ref": safe_publication.get("outbox_ref"),
            "attempt_ref": (
                persisted_publication.get("attempt_ref")
                if persisted_publication is not None
                else None
            ),
            "customer_publication_ref": (
                persisted_publication.get("customer_publication_ref")
                if persisted_publication is not None
                else None
            ),
            "failure_code": (
                persisted_publication.get("failure_code")
                if persisted_publication is not None
                else None
            ),
        },
        "llm_call_audits": [dict(audit) for audit in llm_call_audits],
        "human_decisions": [dict(decision) for decision in human_decisions],
        "terminal_state": {
            "run_status": str(snapshot.get("run_status") or "unknown"),
            "publication_state": str(snapshot.get("publication_state") or "not_ready"),
            "delivery_state": str(snapshot.get("delivery_state") or "pending"),
            "acceptance_status": acceptance_status,
            "reason": reason,
        },
    }
    validate_acceptance_summary(summary)
    return summary


def _walk_summary(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = set(value) & FORBIDDEN_SUMMARY_KEYS
        if forbidden:
            raise ValueError(
                "acceptance_summary_sensitive_field_forbidden:"
                + ",".join(sorted(forbidden))
            )
        for item in value.values():
            _walk_summary(item)
    elif isinstance(value, list):
        for item in value:
            _walk_summary(item)


def validate_acceptance_summary(summary: Mapping[str, Any]) -> None:
    if not isinstance(summary, Mapping) or set(summary) != SUMMARY_TOP_LEVEL_FIELDS:
        raise ValueError("acceptance_summary_shape_invalid")
    if summary.get("schema_version") != ACCEPTANCE_SUMMARY_VERSION:
        raise ValueError("acceptance_summary_version_invalid")
    if summary.get("acceptance_source") != ACCEPTANCE_SOURCE:
        raise ValueError("acceptance_summary_source_invalid")
    case = summary.get("case")
    if not isinstance(case, Mapping) or set(case) != {
        "case_id",
        "question_family",
        "variant",
        "user_message",
        "review_focus",
    }:
        raise ValueError("acceptance_summary_case_invalid")
    if case.get("variant") not in {"original", "paraphrase", "additional"}:
        raise ValueError("acceptance_summary_case_invalid")
    execution = summary.get("execution")
    if not isinstance(execution, Mapping) or set(execution) != {
        "thread_id",
        "run_ids",
        "final_run_id",
    }:
        raise ValueError("acceptance_summary_execution_invalid")
    run_ids = _string_tuple(
        execution.get("run_ids"), "acceptance_summary_execution_invalid"
    )
    if execution.get("final_run_id") not in run_ids:
        raise ValueError("acceptance_summary_execution_invalid")
    pair_material_snapshot = summary.get("pair_material_snapshot")
    if pair_material_snapshot is not None:
        validate_pair_material_snapshot(pair_material_snapshot)
        authority_refs = summary.get("authority_refs")
        if not isinstance(authority_refs, Mapping) or (
            pair_material_snapshot.get("intent_revision_id")
            != authority_refs.get("intent_revision_id")
            or pair_material_snapshot.get("plan_revision_id")
            != authority_refs.get("plan_revision_id")
        ):
            raise ValueError("acceptance_summary_pair_material_authority_invalid")
    terminal = summary.get("terminal_state")
    if not isinstance(terminal, Mapping) or set(terminal) != {
        "run_status",
        "publication_state",
        "delivery_state",
        "acceptance_status",
        "reason",
    }:
        raise ValueError("acceptance_summary_terminal_invalid")
    if terminal.get("acceptance_status") == "passed" and (
        terminal.get("run_status") != "completed"
        or terminal.get("publication_state") != "published"
        or terminal.get("delivery_state") != "published"
        or summary.get("publication", {}).get("customer_publication_event_observed")
        is not True
        or pair_material_snapshot is None
    ):
        raise ValueError("acceptance_summary_false_pass_forbidden")
    audits = summary.get("llm_call_audits")
    if not isinstance(audits, list) or any(
        not isinstance(audit, Mapping) or set(audit) != LLM_AUDIT_FIELDS
        for audit in audits
    ):
        raise ValueError("acceptance_summary_llm_audits_invalid")
    decisions = summary.get("human_decisions")
    if not isinstance(decisions, list) or any(
        not isinstance(decision, Mapping) or set(decision) != HUMAN_DECISION_FIELDS
        for decision in decisions
    ):
        raise ValueError("acceptance_summary_human_decisions_invalid")
    _walk_summary(summary)


def load_acceptance_summary(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_acceptance_summary(payload)
    return payload


def _submitted_decision(
    *,
    source_run_id: str,
    selected_option_id: str | None,
    free_text: str | None,
) -> dict[str, Any]:
    submitted_text = selected_option_id or free_text
    if submitted_text is None:
        raise ValueError("human_clarification_input_required")
    digest = sha256(submitted_text.encode("utf-8")).hexdigest()
    return {
        "decision_ref": f"cli-human-decision:sha256:{digest}",
        "run_id": source_run_id,
        "decision_kind": "selected_option" if selected_option_id else "free_text",
        "slot_id": None,
        "selected_option_id": selected_option_id,
        "source": "user",
        "status": "submitted",
        "materiality": None,
        "content_digest": digest,
        "recorded_at": _utc_now(),
    }


def _submit_gateway_operation(
    *,
    base_url: str,
    user_id: str,
    case: Mapping[str, Any],
    thread_id: str | None,
    source_run_id: str | None,
    selected_option_id: str | None,
    free_text: str | None,
    request_identity: str,
    request_timeout_seconds: float = (
        gateway_once.CLARIFICATION_ADMISSION_TIMEOUT_SECONDS
    ),
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    if source_run_id is None:
        if selected_option_id or free_text:
            raise ValueError("clarification_source_run_required")
        resolved_thread_id = thread_id or gateway_once._create_thread(
            base_url,
            user_id,
            request_identity,
        )
        turn = case["turns"][0]
        response = gateway_once._json_request(
            base_url,
            f"/api/threads/{resolved_thread_id}/messages",
            method="POST",
            payload={
                "message": turn["user"],
                "requestIdentity": request_identity,
            },
            user_id=user_id,
            request_identity=request_identity,
            request_timeout_seconds=request_timeout_seconds,
            expected_status=202,
        )
        return response, None, None
    if bool(selected_option_id) == bool(free_text):
        raise ValueError("human_clarification_input_mode_invalid")
    answer = selected_option_id or free_text
    if answer is None:
        raise ValueError("human_clarification_input_required")
    payload: dict[str, Any] = {
        "answer": answer,
        "selectedOptionId": selected_option_id,
        "requestIdentity": request_identity,
    }
    response = gateway_once._json_request(
        base_url,
        f"/api/runs/{source_run_id}/clarifications",
        method="POST",
        payload=payload,
        user_id=user_id,
        request_identity=request_identity,
        request_timeout_seconds=request_timeout_seconds,
        expected_status=202,
    )
    return (
        response,
        source_run_id,
        _submitted_decision(
            source_run_id=source_run_id,
            selected_option_id=selected_option_id,
            free_text=free_text,
        ),
    )


def _write_summary(summary: Mapping[str, Any], artifact_dir: Path) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / (
        f"{summary['case']['case_id']}-{summary['execution']['final_run_id']}.json"
    )
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if load_acceptance_summary(path) != _canonical_json(summary):
        raise ValueError("acceptance_summary_readback_mismatch")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one natural-language Phase 7 acceptance case through the real "
            "Gateway and summarize only persisted customer-publication authority."
        )
    )
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--case", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:3107")
    parser.add_argument("--user-id", default="human-led-test")
    parser.add_argument("--thread-id")
    parser.add_argument("--run-id")
    clarification = parser.add_mutually_exclusive_group()
    clarification.add_argument("--selected-option-id")
    clarification.add_argument("--clarification-free-text")
    parser.add_argument("--request-identity")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0 or args.poll_interval_seconds <= 0:
        parser.error("poll intervals must be positive")
    if args.selected_option_id is not None and not args.selected_option_id.strip():
        parser.error("--selected-option-id must be non-empty")
    if (
        args.clarification_free_text is not None
        and not args.clarification_free_text.strip()
    ):
        parser.error("--clarification-free-text must be non-empty")
    if args.selected_option_id is not None:
        args.selected_option_id = args.selected_option_id.strip()
    if args.clarification_free_text is not None:
        args.clarification_free_text = args.clarification_free_text.strip()
    if args.run_id and not (args.selected_option_id or args.clarification_free_text):
        parser.error(
            "--run-id requires explicit --selected-option-id or "
            "--clarification-free-text"
        )
    if not args.run_id and (args.selected_option_id or args.clarification_free_text):
        parser.error("clarification input requires --run-id")

    load_env_file(args.env_file)
    case = resolve_cli_cases(args.cases, args.case)[0]
    dependency_health = _dependency_health(args.base_url, args.user_id)
    deadline = monotonic() + args.timeout_seconds
    response, source_run_id, submitted_decision = _submit_gateway_operation(
        base_url=args.base_url,
        user_id=args.user_id,
        case=case,
        thread_id=args.thread_id,
        source_run_id=args.run_id,
        selected_option_id=args.selected_option_id,
        free_text=args.clarification_free_text,
        request_identity=args.request_identity or f"phase7-acceptance-{uuid4()}",
        request_timeout_seconds=min(
            gateway_once.CLARIFICATION_ADMISSION_TIMEOUT_SECONDS,
            args.timeout_seconds,
        ),
    )
    final_run_id = gateway_once._gateway_run_id(response)
    run_ids = tuple(
        dict.fromkeys(
            run_id
            for run_id in (source_run_id, final_run_id)
            if isinstance(run_id, str) and run_id
        )
    )
    remaining_seconds = deadline - monotonic()
    if remaining_seconds <= 0:
        raise TimeoutError("gateway_acceptance_observation_budget_exhausted")

    with _connect_runtime_database() as connection:
        snapshot = _wait_for_terminal_snapshot(
            connection,
            final_run_id,
            timeout_seconds=remaining_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
        authority_records = (
            _authority_records(connection, final_run_id)
            if snapshot.get("run_status") == "completed"
            else _unavailable_authority_records()
        )
        persisted_publication = _persisted_publication(connection, final_run_id)
        event_publication = None
        if (
            snapshot.get("run_status") == "completed"
            and snapshot.get("post_execution_status")
            not in POST_EXECUTION_FAILURE_STATES
        ):
            event_publication = _event_publication(
                args.base_url,
                args.user_id,
                final_run_id,
            )
        llm_call_audits = _llm_call_audits(connection, run_ids)
        human_decisions = _human_decisions(
            connection,
            run_ids,
            submitted_decision,
        )

    summary = build_acceptance_summary(
        case=case,
        dependency_health=dependency_health,
        snapshot=snapshot,
        run_ids=run_ids,
        authority_records=authority_records,
        persisted_publication=persisted_publication,
        event_publication=event_publication,
        llm_call_audits=llm_call_audits,
        human_decisions=human_decisions,
    )
    artifact_path = _write_summary(summary, Path(args.artifact_dir))
    print(
        json.dumps(
            {
                "case_id": summary["case"]["case_id"],
                "run_id": summary["execution"]["final_run_id"],
                "acceptance_status": summary["terminal_state"]["acceptance_status"],
                "artifact_path": str(artifact_path),
            },
            ensure_ascii=False,
        )
    )
    status = summary["terminal_state"]["acceptance_status"]
    if status == "passed":
        return 0
    if status == "waiting_for_human":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
