from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
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

_MAPPING_SECTIONS = ("datasets", "metrics", "dimensions", "capability_inputs")
_OPTIONAL_MAPPING_SECTIONS = ("query_shapes",)
_OBLIGATION_SECTIONS = ("question_family_obligations", "diagnostic_obligations")
_REQUIRED_SECTIONS = (
    "contract_version",
    "artifact",
    "business_timezone",
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
        "dry_run_context",
        "observed",
        "medium",
        "high",
        "strong",
    }
)
_REQUIRED_MAXIMUM_STRENGTHS = frozenset(
    {
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
        _validate_metric_display_policies(payload["metric_display_policies"])
        _validate_metric_business_labels(
            payload["metric_business_labels"],
            tuple(str(item) for item in payload["metrics"]),
        )
        _validate_query_shapes(payload.get("query_shapes") or {})
        _validate_obligations(payload)
        maximum_ranks = payload["claim_strength_taxonomy"]["maximum_strength_ranks"]
        for capability_id, contract in payload["capability_inputs"].items():
            maximum = str(contract.get("maximum_claim_strength") or "")
            if maximum not in maximum_ranks:
                raise ValueError(
                    "runtime_capability_maximum_claim_strength_unknown:"
                    f"{capability_id}:{maximum or 'missing'}"
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
    def metric_ids(self) -> tuple[str, ...]:
        return tuple(sorted(str(item) for item in self._payload["metrics"]))

    @property
    def dataset_ids(self) -> tuple[str, ...]:
        return tuple(sorted(str(item) for item in self._payload["datasets"]))

    @property
    def question_family_ids(self) -> tuple[str, ...]:
        return tuple(self._payload["question_family_obligations"])

    def question_family_obligation(self, question_family: str) -> dict[str, Any]:
        return self._entry(
            "question_family_obligations", question_family, "question_family_obligation"
        )

    def diagnostic_obligation(self, tag: str) -> dict[str, Any]:
        return self._entry("diagnostic_obligations", tag, "diagnostic_obligation")

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
        for strength in ("insufficient", "context_only", "dry_run_context")
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


def _validate_query_shapes(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("runtime_query_shapes_invalid")
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
    if referenced != public:
        missing = public - referenced
        extra = referenced - public
        raise ValueError(
            "runtime_obligation_capability_coverage:"
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
