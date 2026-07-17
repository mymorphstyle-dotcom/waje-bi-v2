from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from bi_agent.runtime.analysis_contracts import DIMENSION_PRESENCE_POLICIES
from bi_agent.runtime.capability_registry import get_capability_card, public_capability_ids
from bi_agent.runtime.contracts import load_contract


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
_OBLIGATION_SECTIONS = ("question_family_obligations", "diagnostic_obligations")
_REQUIRED_SECTIONS = (
    "contract_version",
    "artifact",
    "business_timezone",
    "public_scope_types",
    "restricted_output_policy",
    "claim_strength_taxonomy",
    "metric_display_policies",
    "metric_business_labels",
    *_MAPPING_SECTIONS,
    *_OBLIGATION_SECTIONS,
)
_OBLIGATION_CONDITIONS = frozenset(
    {
        "baselines_present",
        "dimensions_present",
        "multiple_dimensions_present",
        "components_present",
        "event_context_requested",
        "anomaly_review_requested",
        "trust_review_requested",
    }
)
_PUBLISHABLE_EVIDENCE = frozenset(
    {"verified_observation", "verified_trust_boundary"}
)
_DEGRADATION_POLICY_FIELDS = frozenset(
    {"missing_required_input", "missing_optional_input"}
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
        "business_context",
        "data_quality",
    }
)
_ANALYSIS_AXIS_ROLES = frozenset(
    {"required", "disclosure", "auxiliary", "conditional"}
)
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
    "business_context": "user_or_evidence_triggered",
    "data_quality": "always",
}
_ANALYSIS_GOAL_FIELDS = frozenset(
    {
        "business_name",
        "semantics",
        "target_metric_refs",
        "required_outcomes",
        "outcome_claim_types",
        "analysis_axes",
    }
)
_ANALYSIS_REQUIRED_OUTCOMES = frozenset(
    {
        "direction_and_magnitude",
        "ranked_drivers",
        "quantified_contributions",
        "evidence_boundaries",
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
    {"relation", "allowed_units", "count_bounds", "aggregation"}
)
_CONTEXT_WINDOW_RELATIONS = frozenset({"trailing_complete_periods"})
_CONTEXT_WINDOW_UNITS = frozenset({"day", "week", "month", "quarter"})
_CONTEXT_WINDOW_AGGREGATIONS = frozenset(
    {"mean_of_complete_days", "daily_observations"}
)


class RuntimeContractRegistry:
    def __init__(self, payload: Mapping[str, Any], *, source_ref: str = "") -> None:
        missing = tuple(section for section in _REQUIRED_SECTIONS if section not in payload)
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
                    raise ValueError(f"runtime_contract_entry_must_be_mapping:{section}:{item_id}")
        _validate_claim_strength_taxonomy(payload["claim_strength_taxonomy"])
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
        _validate_analysis_axis_catalog(payload)
        _validate_goal_obligations(payload)
        _validate_obligations(payload)
        maximum_ranks = payload["claim_strength_taxonomy"]["maximum_strength_ranks"]
        for capability_id, contract in payload["capability_inputs"].items():
            _validate_context_window_policy(capability_id, contract)
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
                        "runtime_capability_context_datasets_invalid:"
                        f"{capability_id}"
                    )
            elif context_datasets is not None:
                raise ValueError(
                    "runtime_capability_context_datasets_unexpected:"
                    f"{capability_id}"
                )
            maximum = str(contract.get("maximum_claim_strength") or "")
            if maximum not in maximum_ranks:
                raise ValueError(
                    "runtime_capability_maximum_claim_strength_unknown:"
                    f"{capability_id}:{maximum or 'missing'}"
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
    def question_family_ids(self) -> tuple[str, ...]:
        return tuple(self._payload["question_family_obligations"])

    @property
    def diagnostic_obligation_ids(self) -> tuple[str, ...]:
        return tuple(self._payload["diagnostic_obligations"])

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(self._payload["capability_inputs"])

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
        for goal_binding in normalized_goals:
            goal_id = goal_binding["goal_id"]
            goal_role = goal_binding["role"]
            obligation = self.analysis_goal_obligation(goal_id)
            if target_metric not in set(obligation["target_metric_refs"]):
                raise ValueError(
                    f"analysis_goal_target_metric_unsupported:{goal_id}:{target_metric}"
                )
            required_outcomes.extend(obligation["required_outcomes"])
            for outcome_id, claim_types in obligation[
                "outcome_claim_types"
            ].items():
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
                    if _ANALYSIS_AXIS_ROLE_PRIORITY[effective_role] > (
                        _ANALYSIS_AXIS_ROLE_PRIORITY[record["role"]]
                    ):
                        record["role"] = effective_role
                    if goal_id not in record["goal_refs"]:
                        record["goal_refs"].append(goal_id)

        focus_to_axis_field = {
            "component_ids": "metric_refs",
            "dimension_ids": "dimension_refs",
            "context_source_ids": "context_source_refs",
        }
        unmatched_focus = {
            key: set(values) for key, values in normalized_focus.items()
        }
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
            "schema_version": "analysis_goal_plan.v1",
            "goal_bindings": normalized_goals,
            "target_metric": target_metric,
            "required_outcomes": list(dict.fromkeys(required_outcomes)),
            "outcome_claim_types": outcome_claim_types,
            "analysis_axes": list(axis_records.values()),
            "explicit_focus": normalized_focus,
        }

    def question_family_obligation(self, question_family: str) -> dict[str, Any]:
        return self._entry(
            "question_family_obligations", question_family, "question_family_obligation"
        )

    def diagnostic_obligation(self, tag: str) -> dict[str, Any]:
        return self._entry("diagnostic_obligations", tag, "diagnostic_obligation")

    def obligation_condition_context_sources(
        self,
        condition: str,
    ) -> tuple[str, ...]:
        capabilities: set[str] = set()
        for contract in self._payload["question_family_obligations"].values():
            for rule in contract["conditional_rules"]:
                if rule["condition"] == condition:
                    capabilities.update(str(item) for item in rule["add"])
        for contract in self._payload["diagnostic_obligations"].values():
            if contract["condition"] == condition:
                capabilities.update(
                    str(item) for item in contract["required_capabilities"]
                )
        return tuple(
            sorted(
                {
                    str(dataset_id)
                    for capability_id in capabilities
                    for dataset_id in self._payload["capability_inputs"]
                    .get(capability_id, {})
                    .get("allowed_context_datasets", ())
                }
            )
        )

    def order_capabilities(self, capabilities: Any) -> tuple[str, ...]:
        requested = set(str(item) for item in capabilities)
        unknown = requested - set(public_capability_ids())
        if unknown:
            raise ValueError(
                "runtime_obligation_unknown_capability:order:"
                f"{','.join(sorted(unknown))}"
            )
        return tuple(item for item in public_capability_ids() if item in requested)

    @property
    def business_timezone(self) -> str:
        return str(self._payload["business_timezone"])

    @property
    def public_scope_types(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self._payload["public_scope_types"])

    @property
    def restricted_output_fields(self) -> tuple[str, ...]:
        return tuple(
            str(item)
            for item in self._payload["restricted_output_policy"]["blocked_raw_fields"]
        )

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
            raise ValueError(f"runtime_contract_source_dataset_missing:{kind}:{item_id}")
        entries = {dataset_id: base}
        if not isinstance(adapters, Mapping):
            raise ValueError(f"runtime_contract_source_adapters_invalid:{kind}:{item_id}")
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
    if any(
        claim_ranks[strength] != 0
        for strength in ("insufficient", "context_only")
    ):
        raise ValueError("runtime_claim_strength_taxonomy_zero_layer_invalid")
    ordered = tuple(
        claim_ranks[strength]
        for strength in ("observed", "medium", "high", "strong")
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
            raise ValueError(
                f"runtime_query_shape_source_fields:{query_family}"
            )
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
            raise ValueError(
                f"runtime_dataset_intent_roles_invalid:{dataset_id}"
            )


def _validate_context_window_policy(
    capability_id: str,
    contract: Mapping[str, Any],
) -> None:
    policy = contract.get("context_window_policy")
    if policy is None:
        return
    if not isinstance(policy, Mapping) or set(policy) != _CONTEXT_WINDOW_POLICY_FIELDS:
        raise ValueError(
            f"runtime_context_window_policy_invalid:{capability_id}:shape"
        )
    relation = policy.get("relation")
    units = policy.get("allowed_units")
    bounds = policy.get("count_bounds")
    aggregation = policy.get("aggregation")
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
            or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
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


def _validate_analysis_axis_catalog(payload: Mapping[str, Any]) -> None:
    catalog = payload["analysis_axis_catalog"]
    metrics = payload["metrics"]
    dimensions = payload["dimensions"]
    datasets = payload["datasets"]
    capabilities = payload["capability_inputs"]
    public_capabilities = set(public_capability_ids())
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
            allowed=set(capabilities).intersection(public_capabilities),
        )
        if set(target_metric_refs).intersection(metric_refs):
            raise ValueError(
                f"runtime_analysis_axis_target_member_overlap:{axis_id}"
            )
        expected_member_kinds = {
            "change_validation": (False, False, False),
            "formula_tree": (True, False, False),
            "dimension_localization": (False, True, False),
            "time_context": (False, False, False),
            "cross_source_context": (True, True, True),
            "business_context": (False, False, True),
            "data_quality": (False, False, False),
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
                    "runtime_analysis_axis_automatic_dimensions_mismatch:"
                    f"{axis_id}"
                )


def _validate_goal_obligations(payload: Mapping[str, Any]) -> None:
    goals = payload["goal_obligations"]
    catalog = payload["analysis_axis_catalog"]
    if not goals:
        raise ValueError("runtime_analysis_goal_obligations_empty")
    referenced_axes: set[str] = set()
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
        unknown_outcomes = set(required_outcomes) - _ANALYSIS_REQUIRED_OUTCOMES
        if unknown_outcomes:
            raise ValueError(
                f"runtime_analysis_goal_outcome_unknown:{goal_id}:"
                f"{','.join(sorted(unknown_outcomes))}"
            )
        outcome_claim_types = contract["outcome_claim_types"]
        if (
            not isinstance(outcome_claim_types, Mapping)
            or set(outcome_claim_types) != set(required_outcomes)
        ):
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
                    f"runtime_analysis_goal_axis_binding_invalid:{goal_id}:"
                    f"{axis_id}"
                )
            axis_targets = set(catalog[axis_id]["target_metric_refs"])
            if not set(target_metric_refs).issubset(axis_targets):
                raise ValueError(
                    f"runtime_analysis_goal_axis_target_mismatch:{goal_id}:"
                    f"{axis_id}"
                )
            seen_axes.add(axis_id)
            referenced_axes.add(axis_id)
            goal_capability_refs.update(catalog[axis_id]["capability_refs"])
            required_axis_seen = required_axis_seen or role in {
                "required",
                "disclosure",
            }
        if not required_axis_seen:
            raise ValueError(
                f"runtime_analysis_goal_required_axis_missing:{goal_id}"
            )
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
            f"{prefix}_reference_unknown:{axis_id}:{field}:"
            f"{','.join(sorted(unknown))}"
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
                f"runtime_dimension_automatic_screening_contract_invalid:"
                f"{dimension_id}"
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


def _validate_obligations(payload: Mapping[str, Any]) -> None:
    public = set(public_capability_ids())
    configured = set(payload["capability_inputs"])
    referenced: set[str] = set()
    family_fields = {
        "required_capabilities",
        "conditional_rules",
        "independent_capabilities",
        "minimum_publishable_evidence",
        "missing_contract_owner",
        "degradation_policy",
    }
    diagnostic_fields = {
        "required_capabilities",
        "condition",
        "supported_question_families",
    }
    for family, contract in payload["question_family_obligations"].items():
        _validate_obligation_mapping(contract, family_fields, str(family))
        required = _validate_capability_list(
            contract["required_capabilities"], str(family), required=True
        )
        independent = _validate_capability_list(
            contract["independent_capabilities"], str(family)
        )
        if required & independent:
            _raise_classification_conflict(str(family), required & independent)
        _validate_capability_references(
            (*contract["required_capabilities"], *contract["independent_capabilities"]),
            public,
            configured,
            str(family),
        )
        family_capabilities = required | independent
        rules = contract["conditional_rules"]
        if not isinstance(rules, list):
            raise ValueError(f"runtime_obligation_conditional_rules_invalid:{family}")
        conditional: set[str] = set()
        for rule in rules:
            if not isinstance(rule, Mapping) or set(rule) != {"condition", "add"}:
                raise ValueError(f"runtime_obligation_conditional_rule_invalid:{family}")
            _validate_obligation_condition(rule["condition"], str(family))
            added = _validate_capability_list(rule["add"], str(family), required=True)
            conflict = added & (required | independent | conditional)
            if conflict:
                _raise_classification_conflict(str(family), conflict)
            conditional.update(added)
            family_capabilities.update(added)
            _validate_capability_references(rule["add"], public, configured, str(family))
        for capability in family_capabilities:
            supported_families = get_capability_card(
                capability
            ).supported_question_families
            if str(family) not in supported_families:
                raise ValueError(
                    f"runtime_obligation_unsupported_family:{family}:{capability}"
                )
        referenced.update(family_capabilities)
        _validate_publishability_contract(contract, str(family))
    for tag, contract in payload["diagnostic_obligations"].items():
        _validate_obligation_mapping(contract, diagnostic_fields, str(tag))
        _validate_obligation_condition(contract["condition"], str(tag))
        _validate_capability_list(
            contract["required_capabilities"], str(tag), required=True
        )
        _validate_capability_references(
            contract["required_capabilities"], public, configured, str(tag)
        )
        supported_families = _validate_nonempty_string_list(
            contract["supported_question_families"],
            str(tag),
            error_prefix="runtime_diagnostic_question_families",
        )
        unknown_families = supported_families - set(
            payload["question_family_obligations"]
        )
        if unknown_families:
            raise ValueError(
                f"runtime_diagnostic_unknown_family:{tag}:"
                f"{','.join(sorted(unknown_families))}"
            )
        for capability in contract["required_capabilities"]:
            card_families = set(
                get_capability_card(capability).supported_question_families
            )
            incompatible = supported_families - card_families
            if incompatible:
                raise ValueError(
                    f"runtime_diagnostic_unsupported_family:{tag}:{capability}:"
                    f"{','.join(sorted(incompatible))}"
                )
        referenced.update(contract["required_capabilities"])
    axis_candidates = {
        str(capability_id)
        for axis in payload["analysis_axis_catalog"].values()
        for capability_id in axis.get("capability_refs") or ()
    }
    covered = referenced | axis_candidates
    if covered != public:
        missing = public - covered
        extra = covered - public
        raise ValueError(
            "runtime_analysis_capability_coverage:"
            f"missing={','.join(sorted(missing))}:extra={','.join(sorted(extra))}"
        )


def _validate_obligation_mapping(value: Any, fields: set[str], item_id: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"runtime_obligation_invalid:{item_id}")
    extras = set(value) - fields
    if extras:
        raise ValueError(
            f"runtime_obligation_eval_specific_key:{item_id}:{','.join(sorted(extras))}"
        )
    missing = fields - set(value)
    if missing:
        raise ValueError(f"runtime_obligation_missing_fields:{item_id}:{','.join(sorted(missing))}")


def _validate_capability_references(
    values: Any,
    public: set[str],
    configured: set[str],
    item_id: str,
) -> None:
    if not isinstance(values, list) and not isinstance(values, tuple):
        raise ValueError(f"runtime_obligation_capabilities_invalid:{item_id}")
    for capability in values:
        if capability not in public or capability not in configured:
            raise ValueError(f"runtime_obligation_unknown_capability:{item_id}:{capability}")


def _validate_capability_list(
    values: Any,
    item_id: str,
    *,
    required: bool = False,
) -> set[str]:
    if not isinstance(values, list):
        raise ValueError(f"runtime_obligation_capabilities_invalid_type:{item_id}")
    if any(type(value) is not str or not value.strip() for value in values):
        raise ValueError(f"runtime_obligation_capability_blank:{item_id}")
    if required and not values:
        raise ValueError(f"runtime_obligation_capabilities_empty:{item_id}")
    if len(values) != len(set(values)):
        raise ValueError(f"runtime_obligation_capabilities_duplicate:{item_id}")
    return set(values)


def _validate_nonempty_string_list(
    values: Any,
    item_id: str,
    *,
    error_prefix: str,
) -> set[str]:
    if not isinstance(values, list):
        raise ValueError(f"{error_prefix}_invalid_type:{item_id}")
    if not values:
        raise ValueError(f"{error_prefix}_empty:{item_id}")
    if any(type(value) is not str or not value.strip() for value in values):
        raise ValueError(f"{error_prefix}_blank:{item_id}")
    if len(values) != len(set(values)):
        raise ValueError(f"{error_prefix}_duplicate:{item_id}")
    return set(values)


def _raise_classification_conflict(item_id: str, capabilities: set[str]) -> None:
    raise ValueError(
        f"runtime_obligation_classification_conflict:{item_id}:"
        f"{','.join(sorted(capabilities))}"
    )


def _validate_publishability_contract(contract: Mapping[str, Any], item_id: str) -> None:
    evidence = contract["minimum_publishable_evidence"]
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(
            type(value) is not str or value not in _PUBLISHABLE_EVIDENCE
            for value in evidence
        )
        or len(evidence) != len(set(evidence))
    ):
        raise ValueError(f"runtime_obligation_evidence_invalid:{item_id}")
    owner = contract["missing_contract_owner"]
    if type(owner) is not str or not owner.strip():
        raise ValueError(f"runtime_obligation_owner_invalid:{item_id}")
    degradation = contract["degradation_policy"]
    if (
        not isinstance(degradation, Mapping)
        or set(degradation) != _DEGRADATION_POLICY_FIELDS
        or any(
            type(value) is not str or not value.strip()
            for value in degradation.values()
        )
    ):
        raise ValueError(f"runtime_obligation_degradation_policy_invalid:{item_id}")


def _validate_obligation_condition(value: Any, item_id: str) -> None:
    if value not in _OBLIGATION_CONDITIONS:
        raise ValueError(f"runtime_obligation_unknown_condition:{item_id}:{value}")


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
