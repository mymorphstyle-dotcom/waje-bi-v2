from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import yaml

from bi_agent.runtime.analysis_contracts import DIMENSION_PRESENCE_POLICIES
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.exploration_budget_policy import ExplorationBudgetPolicy
from bi_agent.runtime.temporal_comparison import ROLLING_WINDOW_PARAMETER_FIELDS


CANONICAL_RUNTIME_BINDINGS_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "runtime"
    / "clickhouse-analysis-bindings.yaml"
)

_MAPPING_SECTIONS = (
    "datasets",
    "metrics",
    "dimensions",
    "capability_inputs",
    "analysis_axis_catalog",
    "goal_obligations",
)
_OPTIONAL_MAPPING_SECTIONS = ("query_shapes",)
_SEQUENCE_SECTIONS = ("launch_question_families",)
_REQUIRED_SECTIONS = (
    "contract_version",
    "artifact",
    "business_timezone",
    "public_scope_types",
    "exploration_budget_policy",
    "restricted_output_policy",
    "claim_strength_taxonomy",
    "claim_publication_policy",
    "metric_display_policies",
    "metric_business_labels",
    *_MAPPING_SECTIONS,
    *_SEQUENCE_SECTIONS,
)
_REQUIRED_CLAIM_STRENGTHS = frozenset(
    {
        "insufficient",
        "context_only",
        "observed",
        "medium",
        "high",
        "strong",
    }
)
_REQUIRED_MAXIMUM_STRENGTHS = frozenset(
    {
        "insufficient",
        "directional",
        "candidate_driver",
        "candidate_mechanism",
        "anomaly_candidate",
        "recurring_pattern",
        "quantified_contribution",
        "trust_boundary",
        "verifier_only",
        "reducer_only",
    }
)
_ANALYSIS_AXIS_FIELDS = frozenset(
    {
        "business_name",
        "semantics",
        "axis_kind",
        "target_metric_refs",
        "metric_refs",
        "dimension_refs",
        "context_source_refs",
        "capability_refs",
        "reconciliation_group",
        "selection_policy",
        "source_refs",
    }
)
_ANALYSIS_AXIS_KINDS = frozenset(
    {
        "change_validation",
        "formula_tree",
        "dimension_localization",
        "time_context",
        "cross_source_context",
        "market_context",
        "business_context",
        "data_quality",
        "anomaly_detection",
    }
)
_ANALYSIS_AXIS_ROLES = frozenset({"required", "disclosure", "auxiliary", "conditional"})
_ANALYSIS_AXIS_SELECTION_POLICIES = frozenset(
    {
        "primary_baseline_required",
        "all_contract_backed_formula_members",
        "all_automatic_screening_dimensions",
        "periodic_context_when_available",
        "association_context_when_available",
        "user_or_evidence_triggered",
        "always",
    }
)
_ANALYSIS_AXIS_POLICY_BY_KIND = {
    "change_validation": "primary_baseline_required",
    "formula_tree": "all_contract_backed_formula_members",
    "dimension_localization": "all_automatic_screening_dimensions",
    "time_context": "periodic_context_when_available",
    "cross_source_context": "association_context_when_available",
    "market_context": "association_context_when_available",
    "business_context": "user_or_evidence_triggered",
    "data_quality": "always",
    "anomaly_detection": "user_or_evidence_triggered",
}
_ANALYSIS_GOAL_FIELDS = frozenset(
    {
        "business_name",
        "semantics",
        "question_family_ref",
        "target_metric_refs",
        "required_outcomes",
        "outcome_claim_types",
        "analysis_axes",
        "completion_policy",
    }
)
_CLAIM_PUBLICATION_POLICY_FIELDS = frozenset(
    {
        "version",
        "minimum_strength_by_claim_kind",
        "composite_support_by_claim_kind",
    }
)
_CLAIM_COMPOSITE_SUPPORT_POLICY_FIELDS = frozenset(
    {
        "policy",
        "claim_class",
        "publication_strength",
        "causal_interpretation_allowed",
        "identity_fields",
        "required_supports",
    }
)
_CLAIM_COMPOSITE_SUPPORT_REQUIREMENT_FIELDS = frozenset(
    {
        "source_claim_kind",
        "evidence_kind",
        "maximum_claim_strength",
        "evidence_contract",
    }
)
_ANALYSIS_GOAL_COMPLETION_POLICY = {
    "obligation_success": "verified_or_explicit_boundary",
    "required_axis_completion": "all_required_and_disclosure_axes_terminal",
    "publication_authority": "verifier_passed",
}
_ANALYSIS_GOAL_COMPLETION_FIELDS = frozenset(
    {
        "obligation_success",
        "required_axis_completion",
        "publication_authority",
    }
)
_ANALYSIS_GOAL_ROLES = frozenset({"primary", "supporting"})
_ANALYSIS_EXPLICIT_FOCUS_FIELDS = frozenset(
    {"component_ids", "dimension_ids", "context_source_ids"}
)
_ANALYSIS_AXIS_ROLE_PRIORITY = {
    "conditional": 0,
    "auxiliary": 1,
    "disclosure": 2,
    "required": 3,
}
_CONTEXT_WINDOW_POLICY_FIELDS = frozenset(
    {
        "relation",
        "allowed_units",
        "count_bounds",
        "aggregation",
        "execution_default",
    }
)
_CONTEXT_WINDOW_RELATIONS = frozenset({"trailing_complete_periods"})
_CONTEXT_WINDOW_UNITS = frozenset({"day", "week", "month", "quarter"})
_CONTEXT_WINDOW_AGGREGATIONS = frozenset(
    {"mean_of_complete_days", "daily_observations"}
)
_TEMPORAL_COMPATIBILITY_FIELDS = frozenset(
    {
        "modes",
        "window_roles",
        "consumption_semantics",
        "calendar_partition_fields",
    }
)
_TEMPORAL_COMPATIBILITY_MODES = frozenset(
    {
        "target_only",
        "single_day_window_pair",
        "aggregate_window_pair",
        "calendar_partition",
        "event_relative",
    }
)
_TEMPORAL_WINDOW_ROLES = frozenset({"target", "baseline", "reference"})
_TEMPORAL_RESULT_GRAIN_SEMANTICS = frozenset(
    {
        "source_window_aggregate",
        "complete_day_sum_or_mean",
        "daily_series",
        "evaluation_window",
    }
)
_TEMPORAL_WINDOW_SELECTION_SEMANTICS = frozenset(
    {"partition_members", "capability_context"}
)
_TEMPORAL_CONSUMPTION_SEMANTICS = (
    _TEMPORAL_RESULT_GRAIN_SEMANTICS | _TEMPORAL_WINDOW_SELECTION_SEMANTICS
)
_TEMPORAL_CALENDAR_PARTITION_FIELDS = frozenset(
    {"quarter_of_year", "month_of_year", "month_phase", "iso_weekday"}
)


class RuntimeContractRegistry:
    def __init__(self, payload: Mapping[str, Any], *, source_ref: str = "") -> None:
        missing = tuple(
            section for section in _REQUIRED_SECTIONS if section not in payload
        )
        if missing:
            raise ValueError(f"runtime_contract_missing_sections:{','.join(missing)}")
        for section in (*_MAPPING_SECTIONS, *_OPTIONAL_MAPPING_SECTIONS):
            if section not in payload:
                continue
            if not isinstance(payload[section], Mapping):
                raise ValueError(f"runtime_contract_section_must_be_mapping:{section}")
            for item_id, item in payload[section].items():
                if not isinstance(item_id, str) or not item_id.strip():
                    raise ValueError(f"runtime_contract_invalid_id:{section}:{item_id}")
                if not isinstance(item, Mapping):
                    raise ValueError(
                        f"runtime_contract_entry_must_be_mapping:{section}:{item_id}"
                    )
        for section in _SEQUENCE_SECTIONS:
            values = payload[section]
            if (
                not isinstance(values, list)
                or not values
                or any(type(value) is not str or not value.strip() for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"runtime_contract_sequence_invalid:{section}")
        _validate_claim_strength_taxonomy(payload["claim_strength_taxonomy"])
        ExplorationBudgetPolicy.from_contract(payload["exploration_budget_policy"])
        _validate_public_scope_types(payload["public_scope_types"])
        _validate_metric_display_policies(payload["metric_display_policies"])
        _validate_metric_business_labels(
            payload["metric_business_labels"],
            tuple(str(item) for item in payload["metrics"]),
        )
        _validate_query_shapes(
            payload.get("query_shapes") or {},
            payload["capability_inputs"],
        )
        _validate_dataset_intent_roles(payload["datasets"])
        _validate_restricted_output_policy(
            payload["restricted_output_policy"],
            payload["datasets"],
        )
        _validate_customer_safe_filter_fields(
            payload["datasets"],
            blocked_raw_fields=payload["restricted_output_policy"][
                "blocked_raw_fields"
            ],
        )
        _validate_analysis_axis_catalog(payload)
        maximum_ranks = payload["claim_strength_taxonomy"]["maximum_strength_ranks"]
        for capability_id, contract in payload["capability_inputs"].items():
            _validate_context_window_policy(capability_id, contract)
            _validate_temporal_compatibility(
                capability_id,
                contract,
                query_shapes=payload.get("query_shapes") or {},
            )
            source_mode = str(contract.get("source_mode") or "")
            context_datasets = contract.get("allowed_context_datasets")
            if source_mode == "requested_context_sources":
                if (
                    not isinstance(context_datasets, (list, tuple))
                    or not context_datasets
                    or len(context_datasets) != len(set(context_datasets))
                    or any(item not in payload["datasets"] for item in context_datasets)
                    or any(
                        "business_context"
                        not in payload["datasets"][item].get("intent_roles", ())
                        for item in context_datasets
                    )
                    or contract.get("allowed_datasets") is not None
                ):
                    raise ValueError(
                        f"runtime_capability_context_datasets_invalid:{capability_id}"
                    )
            elif context_datasets is not None:
                raise ValueError(
                    f"runtime_capability_context_datasets_unexpected:{capability_id}"
                )
            maximum = str(contract.get("maximum_claim_strength") or "")
            if maximum not in maximum_ranks:
                raise ValueError(
                    "runtime_capability_maximum_claim_strength_unknown:"
                    f"{capability_id}:{maximum or 'missing'}"
                )
            evidence_contract = contract.get("evidence_contract")
            if evidence_contract is not None and (
                type(evidence_contract) is not str
                or not re.fullmatch(
                    r"[a-z][a-z0-9-]*\.v[1-9][0-9]*",
                    evidence_contract,
                )
            ):
                raise ValueError(
                    f"runtime_capability_evidence_contract_invalid:{capability_id}"
                )
            _validate_capability_publication_compatibility(
                capability_id,
                contract,
            )
            accepted_completeness = tuple(
                (contract.get("minimum_readiness") or {}).get("accepted_completeness")
                or ()
            )
            if "partial" in accepted_completeness and (
                maximum != "trust_boundary"
                or "trust_boundary"
                not in set(contract.get("supported_evidence_types") or ())
            ):
                raise ValueError(
                    "runtime_capability_partial_completeness_boundary_invalid:"
                    f"{capability_id}"
                )
            required_slots = (contract.get("minimum_readiness") or {}).get(
                "required_slots"
            )
            completion_authority = contract.get("completion_authority")
            if required_slots == "none":
                if not (
                    completion_authority == "verifier_passed"
                    or isinstance(completion_authority, str)
                    and re.fullmatch(
                        r"checkpoint_completed:[a-z][a-z0-9_]*",
                        completion_authority,
                    )
                ):
                    raise ValueError(
                        "runtime_capability_completion_authority_invalid:"
                        f"{capability_id}:{completion_authority or 'missing'}"
                    )
            elif completion_authority is not None:
                raise ValueError(
                    "runtime_capability_completion_authority_unexpected:"
                    f"{capability_id}"
                )
        _validate_window_reconciliation_contracts(payload["capability_inputs"])
        _validate_capability_task_dependencies(
            payload["capability_inputs"],
            payload["analysis_axis_catalog"],
        )
        _validate_claim_publication_policy(payload)
        _validate_goal_obligations(payload)
        self._payload = deepcopy(dict(payload))
        self._source_ref = source_ref
        self._source_payload_digest = (
            _runtime_contract_payload_digest(payload) if source_ref else ""
        )

    @classmethod
    def from_path(cls, path: str | Path) -> "RuntimeContractRegistry":
        contract_path = Path(path)
        payload = load_contract(contract_path)
        _reject_duplicate_ids(contract_path)
        return cls(payload, source_ref=str(contract_path))

    @property
    def contract_version(self) -> str:
        return str(self._payload["contract_version"])

    @property
    def source_payload_digest(self) -> str:
        if not self._source_payload_digest:
            raise ValueError("runtime_contract_registry_digest_unavailable")
        return self._source_payload_digest

    @property
    def metric_ids(self) -> tuple[str, ...]:
        return tuple(sorted(str(item) for item in self._payload["metrics"]))

    @property
    def dataset_ids(self) -> tuple[str, ...]:
        return tuple(sorted(str(item) for item in self._payload["datasets"]))

    @property
    def context_source_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                str(dataset_id)
                for dataset_id, contract in self._payload["datasets"].items()
                if "business_context" in contract.get("intent_roles", ())
            )
        )

    @property
    def dimension_ids(self) -> tuple[str, ...]:
        return tuple(sorted(str(item) for item in self._payload["dimensions"]))

    @property
    def launch_question_family_ids(self) -> tuple[str, ...]:
        return tuple(self._payload["launch_question_families"])

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(self._payload["capability_inputs"])

    @property
    def public_capability_ids(self) -> tuple[str, ...]:
        referenced = {
            str(capability_id)
            for axis in self._payload["analysis_axis_catalog"].values()
            for capability_id in axis["capability_refs"]
        }
        return tuple(
            capability_id
            for capability_id in self.capability_ids
            if capability_id in referenced
        )

    @property
    def analysis_axis_ids(self) -> tuple[str, ...]:
        return tuple(self._payload["analysis_axis_catalog"])

    @property
    def analysis_goal_ids(self) -> tuple[str, ...]:
        return tuple(self._payload["goal_obligations"])

    @property
    def analysis_goal_semantics(self) -> dict[str, str]:
        return {
            str(goal_id): str(contract["semantics"])
            for goal_id, contract in self._payload["goal_obligations"].items()
        }

    def analysis_axis(self, axis_id: str) -> dict[str, Any]:
        return self._entry("analysis_axis_catalog", axis_id, "analysis_axis")

    def analysis_goal_obligation(self, goal_id: str) -> dict[str, Any]:
        return self._entry("goal_obligations", goal_id, "analysis_goal")

    def analysis_goal_question_family_ref(self, goal_id: str) -> str:
        return str(self.analysis_goal_obligation(goal_id)["question_family_ref"])

    @property
    def claim_publication_requirements(self) -> dict[str, str]:
        return deepcopy(
            dict(
                self._payload["claim_publication_policy"][
                    "minimum_strength_by_claim_kind"
                ]
            )
        )

    @property
    def claim_composite_support_policies(self) -> dict[str, dict[str, Any]]:
        return deepcopy(
            dict(
                self._payload["claim_publication_policy"][
                    "composite_support_by_claim_kind"
                ]
            )
        )

    def claim_composite_support_policy(
        self,
        claim_kind: str,
    ) -> dict[str, Any] | None:
        if (
            not isinstance(claim_kind, str)
            or not claim_kind
            or claim_kind != claim_kind.strip()
        ):
            raise ValueError("runtime_claim_composite_support_scope_invalid")
        policy = self.claim_composite_support_policies.get(claim_kind)
        return deepcopy(policy) if policy is not None else None

    def claim_required_publication_strength(
        self,
        claim_kind: str,
        *,
        goal_ids: Sequence[str],
        axis_ids: Sequence[str] = (),
    ) -> str:
        if (
            not isinstance(claim_kind, str)
            or not claim_kind
            or claim_kind != claim_kind.strip()
            or isinstance(goal_ids, (str, bytes))
            or not isinstance(goal_ids, Sequence)
            or isinstance(axis_ids, (str, bytes))
            or not isinstance(axis_ids, Sequence)
            or any(
                not isinstance(item, str) or not item for item in (*goal_ids, *axis_ids)
            )
        ):
            raise ValueError("runtime_claim_publication_scope_invalid")
        normalized_goals = tuple(dict.fromkeys(str(item) for item in goal_ids))
        normalized_axes = tuple(dict.fromkeys(str(item) for item in axis_ids))
        goal_claims = {
            str(item)
            for goal_id in normalized_goals
            for claim_types in self.analysis_goal_obligation(goal_id)[
                "outcome_claim_types"
            ].values()
            for item in claim_types
        }
        axis_claims = {
            str(item)
            for axis_id in normalized_axes
            for capability_id in self.analysis_axis(axis_id)["capability_refs"]
            for capability in (self.capability_inputs(str(capability_id)),)
            if not capability.get("completion_authority")
            for item in capability.get("supported_claim_types", ())
        }
        if claim_kind not in goal_claims | axis_claims:
            raise KeyError(f"active_claim_publication_strength_missing:{claim_kind}")
        try:
            return str(self.claim_publication_requirements[claim_kind])
        except KeyError as exc:
            raise KeyError(
                f"claim_publication_requirement_missing:{claim_kind}"
            ) from exc

    def compile_goal_analysis_plan(
        self,
        *,
        goal_bindings: Any,
        target_metric: str,
        explicit_focus: Any,
    ) -> dict[str, Any]:
        normalized_goals = _validated_goal_bindings(
            goal_bindings,
            known_goal_ids=set(self.analysis_goal_ids),
        )
        if type(target_metric) is not str or target_metric not in set(self.metric_ids):
            raise ValueError(
                f"analysis_goal_target_metric_invalid:{target_metric or 'missing'}"
            )
        normalized_focus = _validated_analysis_explicit_focus(
            explicit_focus,
            metric_ids=set(self.metric_ids),
            dimension_ids=set(self.dimension_ids),
            context_source_ids=set(self.context_source_ids),
        )
        axis_records: dict[str, dict[str, Any]] = {}
        required_outcomes: list[str] = []
        outcome_claim_types: dict[str, list[str]] = {}
        question_family_refs: list[str] = []
        goal_claim_publication_requirements: dict[str, dict[str, str]] = {}
        goal_completion_policies: dict[str, dict[str, str]] = {}
        for goal_binding in normalized_goals:
            goal_id = goal_binding["goal_id"]
            goal_role = goal_binding["role"]
            obligation = self.analysis_goal_obligation(goal_id)
            if target_metric not in set(obligation["target_metric_refs"]):
                raise ValueError(
                    f"analysis_goal_target_metric_unsupported:{goal_id}:{target_metric}"
                )
            required_outcomes.extend(obligation["required_outcomes"])
            question_family_refs.append(str(obligation["question_family_ref"]))
            goal_claim_kinds = {
                str(claim_kind)
                for claim_types in obligation["outcome_claim_types"].values()
                for claim_kind in claim_types
            }
            goal_claim_publication_requirements[goal_id] = {
                claim_kind: self.claim_publication_requirements[claim_kind]
                for claim_kind in sorted(goal_claim_kinds)
            }
            goal_completion_policies[goal_id] = deepcopy(
                dict(obligation["completion_policy"])
            )
            for outcome_id, claim_types in obligation["outcome_claim_types"].items():
                outcome_claim_types.setdefault(str(outcome_id), [])
                outcome_claim_types[str(outcome_id)].extend(
                    claim_type
                    for claim_type in claim_types
                    if claim_type not in outcome_claim_types[str(outcome_id)]
                )
            for raw_binding in obligation["analysis_axes"]:
                axis_id = str(raw_binding["axis_id"])
                declared_role = str(raw_binding["role"])
                effective_role = (
                    "auxiliary"
                    if goal_role == "supporting" and declared_role == "required"
                    else declared_role
                )
                if axis_id not in axis_records:
                    axis = self.analysis_axis(axis_id)
                    axis_records[axis_id] = {
                        "axis_id": axis_id,
                        "business_name": axis["business_name"],
                        "semantics": axis["semantics"],
                        "axis_kind": axis["axis_kind"],
                        "role": effective_role,
                        "metric_refs": list(axis["metric_refs"]),
                        "dimension_refs": list(axis["dimension_refs"]),
                        "context_source_refs": list(axis["context_source_refs"]),
                        "capability_refs": list(axis["capability_refs"]),
                        "reconciliation_group": axis["reconciliation_group"],
                        "selection_policy": axis["selection_policy"],
                        "source_refs": list(axis["source_refs"]),
                        "explicit_focus_refs": {
                            "component_ids": [],
                            "dimension_ids": [],
                            "context_source_ids": [],
                        },
                        "goal_refs": [goal_id],
                    }
                else:
                    record = axis_records[axis_id]
                    if (
                        _ANALYSIS_AXIS_ROLE_PRIORITY[effective_role]
                        > (_ANALYSIS_AXIS_ROLE_PRIORITY[record["role"]])
                    ):
                        record["role"] = effective_role
                    if goal_id not in record["goal_refs"]:
                        record["goal_refs"].append(goal_id)

        focus_to_axis_field = {
            "component_ids": "metric_refs",
            "dimension_ids": "dimension_refs",
            "context_source_ids": "context_source_refs",
        }
        unmatched_focus = {key: set(values) for key, values in normalized_focus.items()}
        for record in axis_records.values():
            for focus_key, axis_field in focus_to_axis_field.items():
                matches = [
                    value
                    for value in normalized_focus[focus_key]
                    if value in set(record[axis_field])
                ]
                if matches:
                    record["explicit_focus_refs"][focus_key] = matches
                    record["role"] = "required"
                    unmatched_focus[focus_key].difference_update(matches)
        unbound = [
            f"{key}:{value}"
            for key in _ANALYSIS_EXPLICIT_FOCUS_FIELDS
            for value in sorted(unmatched_focus[key])
        ]
        if unbound:
            raise ValueError(
                "analysis_goal_explicit_focus_unbound:" + ",".join(unbound)
            )
        return {
            "schema_version": "analysis_goal_plan.v2",
            "goal_bindings": normalized_goals,
            "question_family_refs": list(dict.fromkeys(question_family_refs)),
            "target_metric": target_metric,
            "required_outcomes": list(dict.fromkeys(required_outcomes)),
            "outcome_claim_types": outcome_claim_types,
            "goal_claim_publication_requirements": (
                goal_claim_publication_requirements
            ),
            "goal_completion_policies": goal_completion_policies,
            "analysis_axes": list(axis_records.values()),
            "explicit_focus": normalized_focus,
        }

    def order_capabilities(self, capabilities: Any) -> tuple[str, ...]:
        requested = set(str(item) for item in capabilities)
        authority_order = self.public_capability_ids
        unknown = requested - set(authority_order)
        if unknown:
            raise ValueError(
                "runtime_obligation_unknown_capability:order:"
                f"{','.join(sorted(unknown))}"
            )
        return tuple(item for item in authority_order if item in requested)

    @property
    def business_timezone(self) -> str:
        return str(self._payload["business_timezone"])

    @property
    def public_scope_types(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self._payload["public_scope_types"])

    @property
    def exploration_budget_policy(self) -> ExplorationBudgetPolicy:
        return ExplorationBudgetPolicy.from_contract(
            self._payload["exploration_budget_policy"]
        )

    @property
    def restricted_output_fields(self) -> tuple[str, ...]:
        return tuple(
            str(item)
            for item in self._payload["restricted_output_policy"]["blocked_raw_fields"]
        )

    @property
    def restricted_output_policy_ref(self) -> str:
        return str(self._payload["restricted_output_policy"]["contract_ref"])

    @property
    def restricted_output_policy_version(self) -> str:
        return str(self._payload["restricted_output_policy"]["version"])

    @property
    def source_ref(self) -> str:
        return self._source_ref

    def source_is_current(self, expected_path: str | Path) -> bool:
        """Return whether this registry still matches its canonical source file."""
        if not self._source_ref or not self._source_payload_digest:
            return False
        source_path = Path(self._source_ref)
        expected = Path(expected_path)
        try:
            if source_path.resolve() != expected.resolve():
                return False
            _reject_duplicate_ids(source_path)
            source_payload = load_contract(source_path)
        except (OSError, TypeError, ValueError):
            return False
        return (
            _runtime_contract_payload_digest(self._payload)
            == self._source_payload_digest
            == _runtime_contract_payload_digest(source_payload)
        )

    def metric(self, metric_id: str, *, dataset_id: str = "") -> dict[str, Any]:
        return self._source_entry("metrics", metric_id, "metric", dataset_id)

    def dimension(self, dimension_id: str, *, dataset_id: str = "") -> dict[str, Any]:
        return self._source_entry("dimensions", dimension_id, "dimension", dataset_id)

    def metric_sources(self, metric_id: str) -> dict[str, dict[str, Any]]:
        return self._source_entries("metrics", metric_id, "metric")

    def metric_ids_for_contract_ref(self, contract_ref: str) -> tuple[str, ...]:
        if not isinstance(contract_ref, str) or not contract_ref:
            return ()
        return tuple(
            metric_id
            for metric_id in self.metric_ids
            if any(
                str(source.get("contract_ref") or "") == contract_ref
                for source in self.metric_sources(metric_id).values()
            )
        )

    def metric_business_labels(self, metric_id: str) -> tuple[str, ...]:
        try:
            labels = self._payload["metric_business_labels"]["labels"][metric_id]
        except KeyError as exc:
            raise KeyError(f"unknown_metric:{metric_id}") from exc
        return tuple(str(item) for item in labels)

    def dimension_sources(self, dimension_id: str) -> dict[str, dict[str, Any]]:
        return self._source_entries("dimensions", dimension_id, "dimension")

    def capability_inputs(self, capability_id: str) -> dict[str, Any]:
        return self._entry("capability_inputs", capability_id, "capability")

    def capability_contract_signature(self, capability_id: str) -> str:
        payload = {
            "registry_contract_version": self.contract_version,
            "capability_id": capability_id,
            "capability_contract": self.capability_inputs(capability_id),
            "claim_strength_taxonomy": self.claim_strength_taxonomy,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def capability_contract_ref(self, capability_id: str) -> str:
        contract = self.capability_inputs(capability_id)
        base = str(
            contract.get("contract_ref")
            or f"contracts/runtime/clickhouse-analysis-bindings.yaml#capability_inputs.{capability_id}"
        )
        return (
            f"{base}|runtime_version={self.contract_version}"
            f"|sha256={self.capability_contract_signature(capability_id)}"
        )

    @property
    def claim_strength_taxonomy(self) -> dict[str, Any]:
        return deepcopy(dict(self._payload["claim_strength_taxonomy"]))

    @property
    def claim_strength_taxonomy_version(self) -> str:
        return str(self._payload["claim_strength_taxonomy"]["version"])

    def claim_strength_rank(self, strength: str) -> int:
        return self._strength_rank("claim_strength_ranks", strength)

    def maximum_claim_strength_rank(self, strength: str) -> int:
        return self._strength_rank("maximum_strength_ranks", strength)

    def _strength_rank(self, section: str, strength: str) -> int:
        try:
            return int(self._payload["claim_strength_taxonomy"][section][strength])
        except (KeyError, TypeError, ValueError) as exc:
            raise KeyError(f"unknown_claim_strength:{strength}") from exc

    def dataset(self, dataset_id: str) -> dict[str, Any]:
        return self._entry("datasets", dataset_id, "dataset")

    def customer_safe_filter_fields(self, dataset_id: str) -> tuple[str, ...]:
        dataset = self.dataset(dataset_id)
        return tuple(str(item) for item in dataset["customer_safe_filter_fields"])

    @property
    def all_customer_safe_filter_fields(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    field
                    for dataset_id in self.dataset_ids
                    for field in self.customer_safe_filter_fields(dataset_id)
                }
            )
        )

    def query_shape(self, query_family: str) -> dict[str, Any]:
        return self._entry("query_shapes", query_family, "query_shape")

    def metric_display_policy_allowed(
        self,
        value_semantics: str,
        display_format: str,
    ) -> bool:
        policies = self._payload["metric_display_policies"]["allowed"]
        return any(
            str(item.get("value_semantics") or "") == value_semantics
            and str(item.get("display_format") or "") == display_format
            for item in policies
        )

    def _entry(self, section: str, item_id: str, kind: str) -> dict[str, Any]:
        try:
            item = self._payload[section][item_id]
        except KeyError as exc:
            raise KeyError(f"unknown_{kind}:{item_id}") from exc
        return deepcopy(dict(item))

    def _source_entry(
        self,
        section: str,
        item_id: str,
        kind: str,
        dataset_id: str,
    ) -> dict[str, Any]:
        base = self._entry(section, item_id, kind)
        adapters = base.pop("source_adapters", {})
        if not dataset_id or dataset_id == str(base.get("dataset_id") or ""):
            return base
        if not isinstance(adapters, Mapping) or dataset_id not in adapters:
            raise KeyError(f"unknown_{kind}_source_adapter:{item_id}:{dataset_id}")
        adapter = adapters[dataset_id]
        if not isinstance(adapter, Mapping):
            raise ValueError(
                f"runtime_contract_source_adapter_invalid:{kind}:{item_id}:{dataset_id}"
            )
        return {**base, **deepcopy(dict(adapter)), "dataset_id": dataset_id}

    def _source_entries(
        self,
        section: str,
        item_id: str,
        kind: str,
    ) -> dict[str, dict[str, Any]]:
        base = self._entry(section, item_id, kind)
        adapters = base.pop("source_adapters", {})
        dataset_id = str(base.get("dataset_id") or "")
        if not dataset_id:
            raise ValueError(
                f"runtime_contract_source_dataset_missing:{kind}:{item_id}"
            )
        entries = {dataset_id: base}
        if not isinstance(adapters, Mapping):
            raise ValueError(
                f"runtime_contract_source_adapters_invalid:{kind}:{item_id}"
            )
        for adapter_dataset, adapter in adapters.items():
            if not isinstance(adapter, Mapping):
                raise ValueError(
                    f"runtime_contract_source_adapter_invalid:{kind}:{item_id}:{adapter_dataset}"
                )
            entries[str(adapter_dataset)] = {
                **base,
                **deepcopy(dict(adapter)),
                "dataset_id": str(adapter_dataset),
            }
        return entries


def _reject_duplicate_ids(path: Path) -> None:
    root = yaml.compose(path.read_text(encoding="utf-8"))
    if root is None:
        return
    _walk_mapping_nodes(root, path=())


def _runtime_contract_payload_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_registry_integrity_error(value: Any) -> str:
    if type(value) is not RuntimeContractRegistry:
        return "runtime_contract_registry_type_invalid"
    if not value.source_is_current(CANONICAL_RUNTIME_BINDINGS_PATH):
        return "runtime_contract_registry_integrity"
    return ""


def _validate_public_scope_types(value: Any) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(type(item) is not str or not item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError("runtime_contract_public_scope_types_invalid")


def _validate_restricted_output_policy(
    value: Any,
    datasets: Mapping[str, Any],
) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"version", "contract_ref", "blocked_raw_fields"}
        or not str(value.get("version") or "")
        or not str(value.get("contract_ref") or "")
    ):
        raise ValueError("runtime_restricted_output_policy_invalid")
    fields = value.get("blocked_raw_fields")
    if (
        not isinstance(fields, list)
        or not fields
        or any(type(field) is not str or not field.strip() for field in fields)
        or len(fields) != len(set(fields))
    ):
        raise ValueError("runtime_restricted_output_policy_fields_invalid")
    dataset_fields = {
        str(field)
        for dataset in datasets.values()
        if isinstance(dataset, Mapping)
        for field in dataset.get("schema_fields", ())
    }
    unknown = set(fields) - dataset_fields
    if unknown:
        raise ValueError(
            "runtime_restricted_output_policy_unknown_fields:"
            f"{','.join(sorted(unknown))}"
        )


def _validate_customer_safe_filter_fields(
    datasets: Mapping[str, Any],
    *,
    blocked_raw_fields: Any,
) -> None:
    blocked = set(str(field) for field in blocked_raw_fields)
    for dataset_id, dataset in datasets.items():
        fields = dataset.get("customer_safe_filter_fields")
        if (
            not isinstance(fields, list)
            or any(type(field) is not str or not field.strip() for field in fields)
            or len(fields) != len(set(fields))
        ):
            raise ValueError(
                f"runtime_customer_safe_filter_fields_invalid:{dataset_id}"
            )
        schema_fields = set(str(field) for field in dataset.get("schema_fields", ()))
        unknown = set(fields) - schema_fields
        if unknown:
            raise ValueError(
                "runtime_customer_safe_filter_fields_unknown:"
                f"{dataset_id}:{','.join(sorted(unknown))}"
            )
        restricted = set(fields).intersection(blocked)
        if restricted:
            raise ValueError(
                "runtime_customer_safe_filter_fields_restricted:"
                f"{dataset_id}:{','.join(sorted(restricted))}"
            )


def _validate_capability_publication_compatibility(
    capability_id: str,
    contract: Mapping[str, Any],
) -> None:
    if contract.get("completion_authority"):
        return
    from bi_agent.runtime.claim_settlement import (
        admissible_evidence_publication_ceiling,
    )
    from bi_agent.runtime.evidence_taxonomy import (
        NON_PUBLISHABLE_EVIDENCE_TYPES,
        EvidenceTaxonomyContractError,
        publication_evidence_kind,
    )

    claim_kinds = tuple(str(item) for item in contract.get("supported_claim_types", ()))
    evidence_types = tuple(
        str(item)
        for item in contract.get("supported_evidence_types", ())
        if item not in NON_PUBLISHABLE_EVIDENCE_TYPES
    )
    if not claim_kinds or not evidence_types:
        raise ValueError(
            f"runtime_capability_publication_contract_missing:{capability_id}"
        )
    for claim_kind in claim_kinds:
        for evidence_type in evidence_types:
            try:
                evidence_kind = publication_evidence_kind(evidence_type)
            except EvidenceTaxonomyContractError as exc:
                raise ValueError(
                    "runtime_capability_evidence_type_invalid:"
                    f"{capability_id}:{evidence_type}"
                ) from exc
            ceiling = admissible_evidence_publication_ceiling(
                evidence_kind=evidence_kind,
                source_claim_kind=claim_kind,
                maximum_claim_strength=str(
                    contract.get("maximum_claim_strength") or ""
                ),
            )
            if ceiling is None:
                raise ValueError(
                    "runtime_capability_publication_compatibility_invalid:"
                    f"{capability_id}:{claim_kind}:{evidence_type}:"
                    f"{contract.get('maximum_claim_strength') or 'missing'}"
                )


def _validate_claim_strength_taxonomy(value: Any) -> None:
    if not isinstance(value, Mapping) or not str(value.get("version") or ""):
        raise ValueError("runtime_claim_strength_taxonomy_invalid")
    for field, required in (
        ("claim_strength_ranks", _REQUIRED_CLAIM_STRENGTHS),
        ("maximum_strength_ranks", _REQUIRED_MAXIMUM_STRENGTHS),
    ):
        ranks = value.get(field)
        if not isinstance(ranks, Mapping) or set(ranks) != set(required):
            raise ValueError(f"runtime_claim_strength_taxonomy_incomplete:{field}")
        if any(type(rank) is not int or rank < 0 for rank in ranks.values()):
            raise ValueError(f"runtime_claim_strength_rank_invalid:{field}")
    claim_ranks = value["claim_strength_ranks"]
    if any(claim_ranks[strength] != 0 for strength in ("insufficient", "context_only")):
        raise ValueError("runtime_claim_strength_taxonomy_zero_layer_invalid")
    ordered = tuple(
        claim_ranks[strength] for strength in ("observed", "medium", "high", "strong")
    )
    if not all(left < right for left, right in zip(ordered, ordered[1:])):
        raise ValueError("runtime_claim_strength_taxonomy_order_invalid")
    maximum_ranks = value["maximum_strength_ranks"]
    expected_maximums = {
        "insufficient": 0,
        "directional": claim_ranks["observed"],
        "candidate_driver": claim_ranks["medium"],
        "candidate_mechanism": claim_ranks["medium"],
        "anomaly_candidate": claim_ranks["medium"],
        "recurring_pattern": claim_ranks["high"],
        "quantified_contribution": claim_ranks["high"],
        "trust_boundary": claim_ranks["observed"],
        "verifier_only": 0,
        "reducer_only": 0,
    }
    if dict(maximum_ranks) != expected_maximums:
        raise ValueError("runtime_claim_strength_taxonomy_maximum_ceiling_invalid")


def _validate_metric_display_policies(value: Any) -> None:
    if not isinstance(value, Mapping) or not str(value.get("version") or ""):
        raise ValueError("runtime_metric_display_policies_invalid")
    allowed = value.get("allowed")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("runtime_metric_display_policies_invalid")
    pairs: set[tuple[str, str]] = set()
    for item in allowed:
        if not isinstance(item, Mapping):
            raise ValueError("runtime_metric_display_policy_invalid")
        pair = (
            str(item.get("value_semantics") or ""),
            str(item.get("display_format") or ""),
        )
        if not all(pair) or pair in pairs:
            raise ValueError("runtime_metric_display_policy_invalid")
        pairs.add(pair)


def _validate_metric_business_labels(
    value: Any,
    metric_ids: tuple[str, ...],
) -> None:
    if not isinstance(value, Mapping) or not str(value.get("version") or ""):
        raise ValueError("runtime_metric_business_labels_invalid")
    labels = value.get("labels")
    if not isinstance(labels, Mapping) or set(labels) != set(metric_ids):
        raise ValueError("runtime_metric_business_labels_incomplete")
    for metric_id, values in labels.items():
        if (
            not isinstance(values, list)
            or not values
            or any(type(item) is not str or not item.strip() for item in values)
            or len(values) != len(set(values))
        ):
            raise ValueError(f"runtime_metric_business_labels_invalid:{metric_id}")


def _validate_query_shapes(
    value: Any,
    capability_inputs: Mapping[str, Any],
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("runtime_query_shapes_invalid")
    dimension_families = {
        str(query_family)
        for capability in capability_inputs.values()
        if isinstance(capability, Mapping)
        and capability.get("dimension_mode") == "requested"
        for query_family in capability.get("query_families", ())
    }
    for query_family, shape in value.items():
        policy = (
            shape.get("dimension_presence_policy")
            if isinstance(shape, Mapping)
            else None
        )
        if type(policy) is not str or policy not in DIMENSION_PRESENCE_POLICIES:
            raise ValueError(
                "runtime_query_shape_dimension_presence_policy:"
                f"{query_family}:{policy or 'missing'}"
            )
        source_fields = shape.get("source_fields")
        if source_fields is not None and (
            not isinstance(source_fields, list)
            or not source_fields
            or any(type(field) is not str or not field for field in source_fields)
            or len(source_fields) != len(set(source_fields))
        ):
            raise ValueError(f"runtime_query_shape_source_fields:{query_family}")
        source_field_policy = shape.get("source_field_policy")
        if source_field_policy is not None and source_field_policy not in {
            "metric_bindings",
        }:
            raise ValueError(
                "runtime_query_shape_source_field_policy:"
                f"{query_family}:{source_field_policy}"
            )
        if query_family in dimension_families:
            topology = shape.get("dimension_topology")
            if topology not in {"independent", "joint"}:
                raise ValueError(
                    "runtime_query_shape_dimension_topology:"
                    f"{query_family}:{topology or 'missing'}"
                )


def _validate_dataset_intent_roles(datasets: Mapping[str, Any]) -> None:
    allowed = {"metric_source", "business_context"}
    for dataset_id, contract in datasets.items():
        roles = contract.get("intent_roles") if isinstance(contract, Mapping) else None
        if (
            not isinstance(roles, list)
            or not roles
            or any(type(role) is not str or role not in allowed for role in roles)
            or len(roles) != len(set(roles))
        ):
            raise ValueError(f"runtime_dataset_intent_roles_invalid:{dataset_id}")


def _validate_context_window_policy(
    capability_id: str,
    contract: Mapping[str, Any],
) -> None:
    policy = contract.get("context_window_policy")
    if policy is None:
        return
    if not isinstance(policy, Mapping) or set(policy) != _CONTEXT_WINDOW_POLICY_FIELDS:
        raise ValueError(f"runtime_context_window_policy_invalid:{capability_id}:shape")
    relation = policy.get("relation")
    units = policy.get("allowed_units")
    bounds = policy.get("count_bounds")
    aggregation = policy.get("aggregation")
    execution_default = policy.get("execution_default")
    if relation not in _CONTEXT_WINDOW_RELATIONS:
        raise ValueError(
            f"runtime_context_window_policy_invalid:{capability_id}:relation"
        )
    if (
        not isinstance(units, list)
        or not units
        or any(unit not in _CONTEXT_WINDOW_UNITS for unit in units)
        or len(units) != len(set(units))
    ):
        raise ValueError(
            f"runtime_context_window_policy_invalid:{capability_id}:allowed_units"
        )
    if not isinstance(bounds, Mapping) or set(bounds) != set(units):
        raise ValueError(
            f"runtime_context_window_policy_invalid:{capability_id}:count_bounds"
        )
    for unit, value in bounds.items():
        if (
            not isinstance(value, list)
            or len(value) != 2
            or any(
                isinstance(item, bool) or not isinstance(item, int) for item in value
            )
            or value[0] <= 0
            or value[1] < value[0]
        ):
            raise ValueError(
                f"runtime_context_window_policy_invalid:{capability_id}:"
                f"count_bounds:{unit}"
            )
    if aggregation not in _CONTEXT_WINDOW_AGGREGATIONS:
        raise ValueError(
            f"runtime_context_window_policy_invalid:{capability_id}:aggregation"
        )
    if not isinstance(execution_default, Mapping) or set(execution_default) != {
        "unit",
        "count",
    }:
        raise ValueError(
            "runtime_context_window_policy_invalid:"
            f"{capability_id}:execution_default:shape"
        )
    default_unit = execution_default.get("unit")
    default_count = execution_default.get("count")
    default_bounds = (
        bounds.get(default_unit)
        if isinstance(default_unit, str) and isinstance(bounds, Mapping)
        else None
    )
    if (
        not isinstance(default_unit, str)
        or default_unit not in set(units)
        or isinstance(default_count, bool)
        or not isinstance(default_count, int)
        or not isinstance(default_bounds, list)
        or len(default_bounds) != 2
        or not default_bounds[0] <= default_count <= default_bounds[1]
    ):
        raise ValueError(
            "runtime_context_window_policy_invalid:"
            f"{capability_id}:execution_default:policy"
        )
    task_binding = contract.get("task_input_binding")
    if (
        isinstance(task_binding, Mapping)
        and task_binding.get("pattern_mode") == "rolling"
    ):
        parameters = task_binding.get("parameters")
        if (
            not isinstance(parameters, Mapping)
            or set(parameters) != ROLLING_WINDOW_PARAMETER_FIELDS
        ):
            raise ValueError(
                f"runtime_context_window_policy_invalid:{capability_id}:"
                "rolling_parameters"
            )
        materiality_floor = parameters.get("materiality_floor")
        minimum_span = parameters.get("minimum_span_days")
        minimum = parameters.get("min_periods")
        if (
            isinstance(materiality_floor, bool)
            or not isinstance(materiality_floor, (int, float))
            or not math.isfinite(float(materiality_floor))
            or isinstance(minimum_span, bool)
            or not isinstance(minimum_span, int)
            or minimum_span <= 0
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or minimum < 2
            or parameters.get("rolling_span_policy")
            != "target_window_duration_with_minimum"
            or parameters.get("rolling_step_policy") != "target_window_duration"
        ):
            raise ValueError(
                f"runtime_context_window_policy_invalid:{capability_id}:"
                "rolling_parameters"
            )
        required_context_days = max(1, minimum_span) + (minimum - 1)
        if (
            units != ["day"]
            or aggregation != "daily_observations"
            or default_unit != "day"
            or default_count != required_context_days
        ):
            raise ValueError(
                f"runtime_context_window_policy_invalid:{capability_id}:rolling_history"
            )


def _validate_temporal_compatibility(
    capability_id: str,
    contract: Mapping[str, Any],
    *,
    query_shapes: Mapping[str, Any],
) -> None:
    compatibility = contract.get("temporal_compatibility")
    if compatibility is None:
        return
    if (
        not isinstance(compatibility, Mapping)
        or set(compatibility) != _TEMPORAL_COMPATIBILITY_FIELDS
    ):
        raise ValueError(
            f"runtime_capability_temporal_compatibility_shape_invalid:{capability_id}"
        )
    if (
        not isinstance(contract.get("task_input_binding"), Mapping)
        or contract.get("completion_authority") is not None
    ):
        raise ValueError(f"runtime_capability_temporal_binding_invalid:{capability_id}")

    modes = compatibility["modes"]
    window_roles = compatibility["window_roles"]
    consumption_semantics = compatibility["consumption_semantics"]
    calendar_fields = compatibility["calendar_partition_fields"]
    _validate_temporal_values(
        capability_id,
        "modes",
        modes,
        allowed=_TEMPORAL_COMPATIBILITY_MODES,
    )
    _validate_temporal_values(
        capability_id,
        "window_roles",
        window_roles,
        allowed=_TEMPORAL_WINDOW_ROLES,
    )
    _validate_temporal_values(
        capability_id,
        "consumption_semantics",
        consumption_semantics,
        allowed=_TEMPORAL_CONSUMPTION_SEMANTICS,
    )
    _validate_temporal_values(
        capability_id,
        "calendar_partition_fields",
        calendar_fields,
        allowed=_TEMPORAL_CALENDAR_PARTITION_FIELDS,
        allow_empty=True,
    )

    result_semantics = set(consumption_semantics) & _TEMPORAL_RESULT_GRAIN_SEMANTICS
    if len(result_semantics) != 1:
        raise ValueError(
            "runtime_capability_temporal_result_grain_semantics_invalid:"
            f"{capability_id}"
        )
    selection_semantics = (
        set(consumption_semantics) & _TEMPORAL_WINDOW_SELECTION_SEMANTICS
    )
    if len(selection_semantics) > 1:
        raise ValueError(
            "runtime_capability_temporal_window_selection_semantics_invalid:"
            f"{capability_id}"
        )

    context_roles = set(window_roles)
    task_binding = contract.get("task_input_binding")
    target_with_owned_context = (
        context_roles == {"target", "reference"}
        and isinstance(task_binding, Mapping)
        and (
            task_binding.get("pattern_mode") == "rolling"
            or task_binding.get("context_consumption_mode")
            == "target_against_reference"
        )
    )
    if "capability_context" in selection_semantics and (
        (context_roles != {"reference"} and not target_with_owned_context)
        or not isinstance(contract.get("context_window_policy"), Mapping)
    ):
        raise ValueError(
            f"runtime_capability_temporal_context_ownership_invalid:{capability_id}"
        )

    uses_partition_members = "partition_members" in selection_semantics
    if uses_partition_members and (
        set(modes) != {"calendar_partition"}
        or set(window_roles) != {"target"}
        or not calendar_fields
    ):
        raise ValueError(
            f"runtime_capability_temporal_partition_members_invalid:{capability_id}"
        )
    if calendar_fields and not uses_partition_members:
        raise ValueError(
            f"runtime_capability_temporal_calendar_fields_invalid:{capability_id}"
        )

    if "source_window_aggregate" not in result_semantics:
        return
    query_families = contract.get("query_families")
    if (
        not isinstance(query_families, list)
        or not query_families
        or any(type(item) is not str or not item for item in query_families)
        or len(query_families) != len(set(query_families))
    ):
        raise ValueError(
            "runtime_capability_temporal_aggregate_shape_invalid:"
            f"{capability_id}:missing"
        )
    for query_family in query_families:
        shape = query_shapes.get(query_family)
        if (
            not isinstance(shape, Mapping)
            or shape.get("result_semantics") != "complete_window_aggregate"
        ):
            raise ValueError(
                "runtime_capability_temporal_aggregate_shape_invalid:"
                f"{capability_id}:{query_family}"
            )


def _validate_capability_task_dependencies(
    capabilities: Mapping[str, Any],
    axes: Mapping[str, Any],
) -> None:
    dependency_graph: dict[str, tuple[str, ...]] = {}
    for capability_id, contract in capabilities.items():
        raw_dependencies = contract.get("task_dependencies")
        if raw_dependencies is None:
            dependency_graph[str(capability_id)] = ()
            continue
        if (
            not isinstance(raw_dependencies, list)
            or not raw_dependencies
            or any(
                type(item) is not str
                or not item
                or item not in capabilities
                or item == capability_id
                for item in raw_dependencies
            )
            or len(raw_dependencies) != len(set(raw_dependencies))
        ):
            raise ValueError(
                f"runtime_capability_task_dependencies_invalid:{capability_id}"
            )
        dependency_graph[str(capability_id)] = tuple(raw_dependencies)
        owner_axes = tuple(
            str(axis_id)
            for axis_id, axis in axes.items()
            if capability_id in set(axis.get("capability_refs") or ())
        )
        if not owner_axes or any(
            not set(raw_dependencies).issubset(
                set(axes[axis_id].get("capability_refs") or ())
            )
            for axis_id in owner_axes
        ):
            raise ValueError(
                f"runtime_capability_task_dependency_axis_invalid:{capability_id}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(capability_id: str) -> None:
        if capability_id in visited:
            return
        if capability_id in visiting:
            raise ValueError("runtime_capability_task_dependency_cycle")
        visiting.add(capability_id)
        for dependency_id in dependency_graph[capability_id]:
            visit(dependency_id)
        visiting.remove(capability_id)
        visited.add(capability_id)

    for capability_id in dependency_graph:
        visit(capability_id)


def _validate_window_reconciliation_contracts(
    capabilities: Mapping[str, Any],
) -> None:
    expected_parameter_fields = {
        "authoritative_source_id",
        "bounded_change_residual_share",
        "bounded_window_relative_tolerance",
        "context_only_resolution",
        "hard_observation_relative_limit",
        "partition_source_id",
        "reconciliation_contract",
        "required_source_count",
        "strategy_source",
        "tolerance_source",
    }
    expected_field_fields = {
        "join_keys",
        "value_key",
        "window_id_key",
        "window_role_key",
    }
    for capability_id, contract in capabilities.items():
        binding = contract.get("task_input_binding")
        if not isinstance(binding, Mapping):
            continue
        parameters = binding.get("parameters")
        if not isinstance(parameters, Mapping) or (
            "context_only_resolution" not in parameters
        ):
            continue
        fields = binding.get("fields")
        ratios = tuple(
            parameters.get(field)
            for field in (
                "bounded_window_relative_tolerance",
                "bounded_change_residual_share",
                "hard_observation_relative_limit",
            )
        )
        source_ids = (
            parameters.get("authoritative_source_id"),
            parameters.get("partition_source_id"),
        )
        contract_id = parameters.get("reconciliation_contract")
        if (
            set(parameters) != expected_parameter_fields
            or not isinstance(fields, Mapping)
            or set(fields) != expected_field_fields
            or parameters.get("context_only_resolution")
            != "current_window_reconciliation"
            or parameters.get("required_source_count") != 2
            or parameters.get("tolerance_source") != "metric_contract"
            or parameters.get("strategy_source") != "metric_contract"
            or any(type(item) is not str or not item for item in source_ids)
            or len(set(source_ids)) != 2
            or set(source_ids) != set(contract.get("allowed_datasets") or ())
            or contract.get("source_selection") != "all_required_datasets"
            or type(contract_id) is not str
            or not re.fullmatch(
                r"[a-z][a-z0-9-]*\.v[1-9][0-9]*",
                contract_id,
            )
            or contract.get("evidence_contract") != contract_id
            or contract.get("maximum_claim_strength")
            != "quantified_contribution"
            or "accounting_contribution"
            not in set(contract.get("supported_evidence_types") or ())
            or (contract.get("minimum_readiness") or {}).get(
                "accepted_completeness"
            )
            != ["complete"]
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                or not 0 <= float(item) <= 1
                for item in ratios
            )
            or float(ratios[0]) > float(ratios[2])
            or fields.get("window_id_key") not in set(fields.get("join_keys") or ())
        ):
            raise ValueError(
                f"runtime_window_reconciliation_contract_invalid:{capability_id}"
            )


def _validate_temporal_values(
    capability_id: str,
    field: str,
    value: Any,
    *,
    allowed: frozenset[str],
    allow_empty: bool = False,
) -> None:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(type(item) is not str or not item for item in value)
        or len(value) != len(set(value))
        or any(item not in allowed for item in value)
    ):
        raise ValueError(f"runtime_capability_temporal_{field}_invalid:{capability_id}")


def _validate_analysis_axis_catalog(payload: Mapping[str, Any]) -> None:
    catalog = payload["analysis_axis_catalog"]
    metrics = payload["metrics"]
    dimensions = payload["dimensions"]
    datasets = payload["datasets"]
    capabilities = payload["capability_inputs"]
    registered_capabilities = set(capabilities)
    context_source_ids = {
        str(dataset_id)
        for dataset_id, contract in datasets.items()
        if "business_context" in contract.get("intent_roles", ())
    }
    if not catalog:
        raise ValueError("runtime_analysis_axis_catalog_empty")
    reconciliation_groups: set[str] = set()
    for axis_id, contract in catalog.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(axis_id)):
            raise ValueError(f"runtime_analysis_axis_id_invalid:{axis_id}")
        if not isinstance(contract, Mapping) or set(contract) != _ANALYSIS_AXIS_FIELDS:
            raise ValueError(f"runtime_analysis_axis_shape_invalid:{axis_id}")
        for field in ("business_name", "semantics"):
            if type(contract[field]) is not str or not contract[field].strip():
                raise ValueError(
                    f"runtime_analysis_axis_text_invalid:{axis_id}:{field}"
                )
        axis_kind = contract["axis_kind"]
        if axis_kind not in _ANALYSIS_AXIS_KINDS:
            raise ValueError(
                f"runtime_analysis_axis_kind_invalid:{axis_id}:{axis_kind}"
            )
        target_metric_refs = _analysis_string_sequence(
            contract["target_metric_refs"],
            field="target_metric_refs",
            item_id=str(axis_id),
            required=True,
        )
        metric_refs = _analysis_string_sequence(
            contract["metric_refs"],
            field="metric_refs",
            item_id=str(axis_id),
        )
        dimension_refs = _analysis_string_sequence(
            contract["dimension_refs"],
            field="dimension_refs",
            item_id=str(axis_id),
        )
        context_source_refs = _analysis_string_sequence(
            contract["context_source_refs"],
            field="context_source_refs",
            item_id=str(axis_id),
        )
        capability_refs = _analysis_string_sequence(
            contract["capability_refs"],
            field="capability_refs",
            item_id=str(axis_id),
            required=True,
        )
        _analysis_string_sequence(
            contract["source_refs"],
            field="source_refs",
            item_id=str(axis_id),
            required=True,
        )
        _reject_unknown_analysis_refs(
            axis_id=str(axis_id),
            field="target_metric_refs",
            values=target_metric_refs,
            allowed=set(metrics),
        )
        _reject_unknown_analysis_refs(
            axis_id=str(axis_id),
            field="metric_refs",
            values=metric_refs,
            allowed=set(metrics),
        )
        _reject_unknown_analysis_refs(
            axis_id=str(axis_id),
            field="dimension_refs",
            values=dimension_refs,
            allowed=set(dimensions),
        )
        _reject_unknown_analysis_refs(
            axis_id=str(axis_id),
            field="context_source_refs",
            values=context_source_refs,
            allowed=context_source_ids,
        )
        _reject_unknown_analysis_refs(
            axis_id=str(axis_id),
            field="capability_refs",
            values=capability_refs,
            allowed=registered_capabilities,
        )
        if set(target_metric_refs).intersection(metric_refs):
            raise ValueError(f"runtime_analysis_axis_target_member_overlap:{axis_id}")
        expected_member_kinds = {
            "change_validation": (False, False, False),
            "formula_tree": (True, False, False),
            "dimension_localization": (False, True, False),
            "time_context": (False, False, False),
            "cross_source_context": (True, True, True),
            "market_context": (True, True, False),
            "business_context": (False, False, True),
            "data_quality": (False, False, False),
            "anomaly_detection": (False, False, False),
        }[axis_kind]
        actual_member_kinds = (
            bool(metric_refs),
            bool(dimension_refs),
            bool(context_source_refs),
        )
        if actual_member_kinds != expected_member_kinds:
            raise ValueError(
                f"runtime_analysis_axis_members_invalid:{axis_id}:{axis_kind}"
            )
        selection_policy = contract["selection_policy"]
        if (
            selection_policy not in _ANALYSIS_AXIS_SELECTION_POLICIES
            or selection_policy != _ANALYSIS_AXIS_POLICY_BY_KIND[axis_kind]
        ):
            raise ValueError(
                f"runtime_analysis_axis_selection_policy_invalid:{axis_id}:"
                f"{selection_policy}"
            )
        reconciliation_group = contract["reconciliation_group"]
        if (
            type(reconciliation_group) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_]*", reconciliation_group)
            or reconciliation_group in reconciliation_groups
        ):
            raise ValueError(
                f"runtime_analysis_axis_reconciliation_group_invalid:{axis_id}"
            )
        reconciliation_groups.add(reconciliation_group)
        if selection_policy == "all_automatic_screening_dimensions":
            expected_dimensions = _automatic_screening_dimensions(
                payload,
                target_metric_refs=target_metric_refs,
            )
            if dimension_refs != expected_dimensions:
                raise ValueError(
                    f"runtime_analysis_axis_automatic_dimensions_mismatch:{axis_id}"
                )


def _axis_publishable_claim_types(
    payload: Mapping[str, Any],
    *,
    axis_id: str,
) -> set[str]:
    axis = payload["analysis_axis_catalog"][axis_id]
    return {
        str(claim_type)
        for capability_id in axis["capability_refs"]
        for capability in (payload["capability_inputs"][capability_id],)
        if not capability.get("completion_authority")
        for claim_type in capability.get("supported_claim_types", ())
    }


def _axis_claim_publication_ceilings(
    payload: Mapping[str, Any],
    *,
    axis_id: str,
    claim_type: str,
) -> tuple[Any, ...]:
    from bi_agent.runtime.claim_settlement import (
        admissible_evidence_publication_ceiling,
    )
    from bi_agent.runtime.evidence_taxonomy import (
        NON_PUBLISHABLE_EVIDENCE_TYPES,
        publication_evidence_kind,
    )

    axis = payload["analysis_axis_catalog"][axis_id]
    direct = tuple(
        ceiling
        for capability_id in axis["capability_refs"]
        for capability in (payload["capability_inputs"][capability_id],)
        if not capability.get("completion_authority")
        and claim_type in capability.get("supported_claim_types", ())
        for evidence_type in capability.get("supported_evidence_types", ())
        if evidence_type not in NON_PUBLISHABLE_EVIDENCE_TYPES
        for ceiling in (
            admissible_evidence_publication_ceiling(
                evidence_kind=publication_evidence_kind(str(evidence_type)),
                source_claim_kind=claim_type,
                maximum_claim_strength=str(capability["maximum_claim_strength"]),
            ),
        )
        if ceiling is not None
    )
    composite = _axis_composite_publication_ceiling(
        payload,
        axis_id=axis_id,
        claim_type=claim_type,
    )
    return (*direct, *((composite,) if composite is not None else ()))


def _axis_composite_publication_ceiling(
    payload: Mapping[str, Any],
    *,
    axis_id: str,
    claim_type: str,
) -> Any | None:
    from bi_agent.runtime.claim_authority import ClaimPublicationCeiling
    from bi_agent.runtime.evidence_taxonomy import (
        NON_PUBLISHABLE_EVIDENCE_TYPES,
        publication_evidence_kind,
    )

    policy = payload["claim_publication_policy"]["composite_support_by_claim_kind"].get(
        claim_type
    )
    if policy is None:
        return None
    capabilities = tuple(
        payload["capability_inputs"][capability_id]
        for capability_id in payload["analysis_axis_catalog"][axis_id][
            "capability_refs"
        ]
    )
    for requirement in policy["required_supports"]:
        if not any(
            requirement["source_claim_kind"]
            in capability.get("supported_claim_types", ())
            and capability.get("maximum_claim_strength")
            == requirement["maximum_claim_strength"]
            and capability.get("evidence_contract") == requirement["evidence_contract"]
            and any(
                evidence_type not in NON_PUBLISHABLE_EVIDENCE_TYPES
                and publication_evidence_kind(str(evidence_type))
                == requirement["evidence_kind"]
                for evidence_type in capability.get("supported_evidence_types", ())
            )
            for capability in capabilities
        ):
            return None
    return ClaimPublicationCeiling.create(
        claim_class=str(policy["claim_class"]),
        strength=str(policy["publication_strength"]),
    )


def _validate_claim_composite_support_policies(
    payload: Mapping[str, Any],
    *,
    requirements: Mapping[str, Any],
) -> None:
    from bi_agent.runtime.claim_authority import ClaimPublicationCeiling
    from bi_agent.runtime.evidence_taxonomy import (
        PUBLISHABLE_EVIDENCE_KIND_BY_TYPE,
    )

    policies = payload["claim_publication_policy"].get(
        "composite_support_by_claim_kind"
    )
    if not isinstance(policies, Mapping):
        raise ValueError("runtime_claim_composite_support_policies_invalid")
    known_evidence_kinds = set(PUBLISHABLE_EVIDENCE_KIND_BY_TYPE.values())
    known_strengths = set(payload["claim_strength_taxonomy"]["maximum_strength_ranks"])
    for claim_kind, policy in policies.items():
        if (
            claim_kind not in requirements
            or not isinstance(policy, Mapping)
            or set(policy) != _CLAIM_COMPOSITE_SUPPORT_POLICY_FIELDS
            or policy.get("policy") != "all_required_supports_same_authority"
            or policy.get("causal_interpretation_allowed") is not False
            or tuple(policy.get("identity_fields") or ())
            != ("event_ref", "temporal_authority_ref")
            or policy.get("publication_strength") != requirements[claim_kind]
        ):
            raise ValueError(
                f"runtime_claim_composite_support_policy_invalid:{claim_kind}"
            )
        try:
            ClaimPublicationCeiling.create(
                claim_class=str(policy.get("claim_class") or ""),
                strength=str(policy.get("publication_strength") or ""),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"runtime_claim_composite_support_policy_invalid:{claim_kind}"
            ) from exc
        required_supports = policy.get("required_supports")
        if (
            not isinstance(required_supports, list)
            or len(required_supports) < 2
            or any(
                not isinstance(item, Mapping)
                or set(item) != _CLAIM_COMPOSITE_SUPPORT_REQUIREMENT_FIELDS
                or item.get("source_claim_kind") not in requirements
                or item.get("evidence_kind") not in known_evidence_kinds
                or item.get("maximum_claim_strength") not in known_strengths
                or type(item.get("evidence_contract")) is not str
                or not re.fullmatch(
                    r"[a-z][a-z0-9-]*\.v[1-9][0-9]*",
                    str(item.get("evidence_contract") or ""),
                )
                for item in required_supports
            )
            or len(
                {
                    (
                        str(item["source_claim_kind"]),
                        str(item["evidence_contract"]),
                    )
                    for item in required_supports
                }
            )
            != len(required_supports)
        ):
            raise ValueError(
                f"runtime_claim_composite_support_requirements_invalid:{claim_kind}"
            )


def _validate_claim_publication_policy(payload: Mapping[str, Any]) -> None:
    from bi_agent.runtime.claim_settlement import publication_ceiling_satisfies

    policy = payload["claim_publication_policy"]
    if (
        not isinstance(policy, Mapping)
        or set(policy) != _CLAIM_PUBLICATION_POLICY_FIELDS
        or type(policy.get("version")) is not str
        or not policy["version"].strip()
    ):
        raise ValueError("runtime_claim_publication_policy_shape_invalid")
    requirements = policy["minimum_strength_by_claim_kind"]
    if not isinstance(requirements, Mapping) or not requirements:
        raise ValueError("runtime_claim_publication_requirements_invalid")
    _validate_claim_composite_support_policies(
        payload,
        requirements=requirements,
    )
    expected_claim_types = {
        claim_type
        for axis_id in payload["analysis_axis_catalog"]
        for claim_type in _axis_publishable_claim_types(
            payload,
            axis_id=str(axis_id),
        )
    }
    if set(requirements) != expected_claim_types:
        raise ValueError(
            "runtime_claim_publication_requirement_coverage:"
            f"missing={','.join(sorted(expected_claim_types - set(requirements)))}:"
            f"extra={','.join(sorted(set(requirements) - expected_claim_types))}"
        )
    known_strengths = set(payload["claim_strength_taxonomy"]["maximum_strength_ranks"])
    for claim_type, strength in requirements.items():
        if (
            type(claim_type) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_]*", claim_type)
            or type(strength) is not str
            or strength not in known_strengths
        ):
            raise ValueError(
                "runtime_claim_publication_requirement_invalid:"
                f"{claim_type}:{strength or 'missing'}"
            )
    for axis_id in payload["analysis_axis_catalog"]:
        for claim_type in _axis_publishable_claim_types(
            payload,
            axis_id=str(axis_id),
        ):
            strength = str(requirements[claim_type])
            ceilings = _axis_claim_publication_ceilings(
                payload,
                axis_id=str(axis_id),
                claim_type=claim_type,
            )
            if not any(
                publication_ceiling_satisfies(
                    ceiling,
                    required_strength=strength,
                )
                for ceiling in ceilings
            ):
                raise ValueError(
                    "runtime_analysis_axis_claim_requirement_unreachable:"
                    f"{axis_id}:{claim_type}:{strength}"
                )


def _validate_goal_obligations(payload: Mapping[str, Any]) -> None:
    goals = payload["goal_obligations"]
    catalog = payload["analysis_axis_catalog"]
    question_families = set(payload["launch_question_families"])
    if not goals:
        raise ValueError("runtime_analysis_goal_obligations_empty")
    referenced_axes: set[str] = set()
    referenced_question_families: set[str] = set()
    for goal_id, contract in goals.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(goal_id)):
            raise ValueError(f"runtime_analysis_goal_id_invalid:{goal_id}")
        if not isinstance(contract, Mapping) or set(contract) != _ANALYSIS_GOAL_FIELDS:
            raise ValueError(f"runtime_analysis_goal_shape_invalid:{goal_id}")
        for field in ("business_name", "semantics"):
            if type(contract[field]) is not str or not contract[field].strip():
                raise ValueError(
                    f"runtime_analysis_goal_text_invalid:{goal_id}:{field}"
                )
        question_family_ref = contract["question_family_ref"]
        if (
            type(question_family_ref) is not str
            or question_family_ref not in question_families
        ):
            raise ValueError(
                "runtime_analysis_goal_question_family_unknown:"
                f"{goal_id}:{question_family_ref or 'missing'}"
            )
        referenced_question_families.add(question_family_ref)
        target_metric_refs = _analysis_string_sequence(
            contract["target_metric_refs"],
            field="target_metric_refs",
            item_id=str(goal_id),
            required=True,
        )
        _reject_unknown_analysis_refs(
            axis_id=str(goal_id),
            field="target_metric_refs",
            values=target_metric_refs,
            allowed=set(payload["metrics"]),
            prefix="runtime_analysis_goal",
        )
        required_outcomes = _analysis_string_sequence(
            contract["required_outcomes"],
            field="required_outcomes",
            item_id=str(goal_id),
            required=True,
        )
        invalid_outcomes = tuple(
            outcome_id
            for outcome_id in required_outcomes
            if not re.fullmatch(r"[a-z][a-z0-9_]*", outcome_id)
        )
        if invalid_outcomes:
            raise ValueError(
                f"runtime_analysis_goal_outcome_id_invalid:{goal_id}:"
                f"{','.join(invalid_outcomes)}"
            )
        outcome_claim_types = contract["outcome_claim_types"]
        if not isinstance(outcome_claim_types, Mapping) or set(
            outcome_claim_types
        ) != set(required_outcomes):
            raise ValueError(
                f"runtime_analysis_goal_outcome_claim_types_invalid:{goal_id}"
            )
        normalized_outcome_claim_types = {
            str(outcome_id): _analysis_string_sequence(
                claim_types,
                field=f"outcome_claim_types.{outcome_id}",
                item_id=str(goal_id),
                required=True,
            )
            for outcome_id, claim_types in outcome_claim_types.items()
        }
        axis_bindings = contract["analysis_axes"]
        if not isinstance(axis_bindings, list) or not axis_bindings:
            raise ValueError(f"runtime_analysis_goal_axes_invalid:{goal_id}")
        seen_axes: set[str] = set()
        goal_capability_refs: set[str] = set()
        mandatory_goal_capability_refs: set[str] = set()
        required_axis_seen = False
        for binding in axis_bindings:
            if not isinstance(binding, Mapping) or set(binding) != {
                "axis_id",
                "role",
            }:
                raise ValueError(
                    f"runtime_analysis_goal_axis_binding_invalid:{goal_id}"
                )
            axis_id = binding["axis_id"]
            role = binding["role"]
            if (
                type(axis_id) is not str
                or axis_id not in catalog
                or axis_id in seen_axes
                or role not in _ANALYSIS_AXIS_ROLES
            ):
                raise ValueError(
                    f"runtime_analysis_goal_axis_binding_invalid:{goal_id}:{axis_id}"
                )
            axis_targets = set(catalog[axis_id]["target_metric_refs"])
            if not set(target_metric_refs).issubset(axis_targets):
                raise ValueError(
                    f"runtime_analysis_goal_axis_target_mismatch:{goal_id}:{axis_id}"
                )
            seen_axes.add(axis_id)
            referenced_axes.add(axis_id)
            goal_capability_refs.update(catalog[axis_id]["capability_refs"])
            if role in {"required", "disclosure"}:
                mandatory_goal_capability_refs.update(
                    catalog[axis_id]["capability_refs"]
                )
            required_axis_seen = required_axis_seen or role in {
                "required",
                "disclosure",
            }
        if not required_axis_seen:
            raise ValueError(f"runtime_analysis_goal_required_axis_missing:{goal_id}")
        supported_goal_claim_types = {
            str(claim_type)
            for capability_id in goal_capability_refs
            for claim_type in payload["capability_inputs"][capability_id].get(
                "supported_claim_types", ()
            )
        }
        for outcome_id, claim_types in normalized_outcome_claim_types.items():
            unsupported = set(claim_types) - supported_goal_claim_types
            if unsupported:
                raise ValueError(
                    "runtime_analysis_goal_outcome_claim_type_unsupported:"
                    f"{goal_id}:{outcome_id}:{','.join(sorted(unsupported))}"
                )
        required_claim_types = {
            claim_type
            for claim_types in normalized_outcome_claim_types.values()
            for claim_type in claim_types
        }
        for capability_id in mandatory_goal_capability_refs:
            capability = payload["capability_inputs"][capability_id]
            if capability.get("completion_authority") == "verifier_passed":
                continue
            if not set(capability.get("supported_claim_types", ())).intersection(
                required_claim_types
            ):
                raise ValueError(
                    "runtime_analysis_goal_required_capability_unbound:"
                    f"{goal_id}:{capability_id}"
                )
        missing_requirements = required_claim_types - set(
            payload["claim_publication_policy"]["minimum_strength_by_claim_kind"]
        )
        if missing_requirements:
            raise ValueError(
                "runtime_analysis_goal_claim_requirement_missing:"
                f"{goal_id}:{','.join(sorted(missing_requirements))}"
            )
        completion_policy = contract["completion_policy"]
        if (
            not isinstance(completion_policy, Mapping)
            or set(completion_policy) != _ANALYSIS_GOAL_COMPLETION_FIELDS
            or dict(completion_policy) != _ANALYSIS_GOAL_COMPLETION_POLICY
        ):
            raise ValueError(
                f"runtime_analysis_goal_completion_policy_invalid:{goal_id}"
            )
    if referenced_question_families != question_families:
        raise ValueError(
            "runtime_analysis_goal_family_coverage:"
            f"missing={','.join(sorted(question_families - referenced_question_families))}:"
            f"extra={','.join(sorted(referenced_question_families - question_families))}"
        )
    if referenced_axes != set(catalog):
        raise ValueError(
            "runtime_analysis_goal_axis_coverage:"
            f"missing={','.join(sorted(set(catalog) - referenced_axes))}:"
            f"extra={','.join(sorted(referenced_axes - set(catalog)))}"
        )


def _analysis_string_sequence(
    value: Any,
    *,
    field: str,
    item_id: str,
    required: bool = False,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or required
        and not value
        or any(type(item) is not str or not item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(
            f"runtime_analysis_reference_sequence_invalid:{item_id}:{field}"
        )
    return tuple(value)


def _reject_unknown_analysis_refs(
    *,
    axis_id: str,
    field: str,
    values: tuple[str, ...],
    allowed: set[str],
    prefix: str = "runtime_analysis_axis",
) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(
            f"{prefix}_reference_unknown:{axis_id}:{field}:{','.join(sorted(unknown))}"
        )


def _source_dataset_ids(contract: Mapping[str, Any]) -> set[str]:
    dataset_ids = {str(contract.get("dataset_id") or "")}
    adapters = contract.get("source_adapters") or {}
    if isinstance(adapters, Mapping):
        dataset_ids.update(str(dataset_id) for dataset_id in adapters)
    return {dataset_id for dataset_id in dataset_ids if dataset_id}


def _automatic_screening_dimensions(
    payload: Mapping[str, Any],
    *,
    target_metric_refs: tuple[str, ...],
) -> tuple[str, ...]:
    target_datasets = {
        dataset_id
        for metric_id in target_metric_refs
        for dataset_id in _source_dataset_ids(payload["metrics"][metric_id])
    }
    candidates: list[tuple[int, str]] = []
    for dimension_id, contract in payload["dimensions"].items():
        screening = contract.get("automatic_screening")
        if screening is not None and screening not in {"allowed", "blocked"}:
            raise ValueError(
                f"runtime_dimension_automatic_screening_invalid:{dimension_id}"
            )
        if screening != "allowed" or not target_datasets.intersection(
            _source_dataset_ids(contract)
        ):
            continue
        priority = contract.get("screen_priority")
        if (
            type(priority) is not int
            or priority < 0
            or contract.get("output_policy") != "aggregate_only"
        ):
            raise ValueError(
                f"runtime_dimension_automatic_screening_contract_invalid:{dimension_id}"
            )
        candidates.append((priority, str(dimension_id)))
    return tuple(
        dimension_id
        for _, dimension_id in sorted(candidates, key=lambda item: (item[0], item[1]))
    )


def _validated_goal_bindings(
    value: Any,
    *,
    known_goal_ids: set[str],
) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("analysis_goal_bindings_invalid")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    primary_count = 0
    for binding in value:
        if not isinstance(binding, Mapping) or set(binding) != {"goal_id", "role"}:
            raise ValueError("analysis_goal_binding_invalid")
        goal_id = binding["goal_id"]
        role = binding["role"]
        if (
            type(goal_id) is not str
            or goal_id not in known_goal_ids
            or goal_id in seen
            or role not in _ANALYSIS_GOAL_ROLES
        ):
            raise ValueError(f"analysis_goal_binding_invalid:{goal_id}")
        seen.add(goal_id)
        primary_count += int(role == "primary")
        normalized.append({"goal_id": goal_id, "role": str(role)})
    if primary_count != 1:
        raise ValueError("analysis_goal_primary_cardinality_invalid")
    return normalized


def _validated_analysis_explicit_focus(
    value: Any,
    *,
    metric_ids: set[str],
    dimension_ids: set[str],
    context_source_ids: set[str],
) -> dict[str, list[str]]:
    if not isinstance(value, Mapping) or set(value) != _ANALYSIS_EXPLICIT_FOCUS_FIELDS:
        raise ValueError("analysis_goal_explicit_focus_invalid")
    allowed = {
        "component_ids": metric_ids,
        "dimension_ids": dimension_ids,
        "context_source_ids": context_source_ids,
    }
    normalized: dict[str, list[str]] = {}
    for field in ("component_ids", "dimension_ids", "context_source_ids"):
        values = value[field]
        if (
            not isinstance(values, (list, tuple))
            or any(type(item) is not str or not item for item in values)
            or len(values) != len(set(values))
            or any(item not in allowed[field] for item in values)
        ):
            raise ValueError(f"analysis_goal_explicit_focus_invalid:{field}")
        normalized[field] = list(values)
    return normalized


def _walk_mapping_nodes(node: yaml.Node, *, path: tuple[str, ...]) -> None:
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(getattr(key_node, "value", ""))
            if key in seen:
                location = ".".join((*path, key))
                raise ValueError(f"duplicate_runtime_contract_id:{location}")
            seen.add(key)
            _walk_mapping_nodes(value_node, path=(*path, key))
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _walk_mapping_nodes(item, path=(*path, str(index)))
