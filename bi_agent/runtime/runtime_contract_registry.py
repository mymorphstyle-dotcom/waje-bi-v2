from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import yaml

from bi_agent.runtime.contracts import load_contract


_MAPPING_SECTIONS = ("datasets", "metrics", "dimensions", "capability_inputs")
_OPTIONAL_MAPPING_SECTIONS = ("query_shapes",)
_REQUIRED_SECTIONS = (
    "contract_version",
    "artifact",
    "business_timezone",
    *_MAPPING_SECTIONS,
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
        self._payload = deepcopy(dict(payload))
        self._source_ref = source_ref

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
    def business_timezone(self) -> str:
        return str(self._payload["business_timezone"])

    @property
    def source_ref(self) -> str:
        return self._source_ref

    def metric(self, metric_id: str) -> dict[str, Any]:
        return self._entry("metrics", metric_id, "metric")

    def dimension(self, dimension_id: str) -> dict[str, Any]:
        return self._entry("dimensions", dimension_id, "dimension")

    def capability_inputs(self, capability_id: str) -> dict[str, Any]:
        return self._entry("capability_inputs", capability_id, "capability")

    def dataset(self, dataset_id: str) -> dict[str, Any]:
        return self._entry("datasets", dataset_id, "dataset")

    def query_shape(self, query_family: str) -> dict[str, Any]:
        return self._entry("query_shapes", query_family, "query_shape")

    def _entry(self, section: str, item_id: str, kind: str) -> dict[str, Any]:
        try:
            item = self._payload[section][item_id]
        except KeyError as exc:
            raise KeyError(f"unknown_{kind}:{item_id}") from exc
        return deepcopy(dict(item))


def _reject_duplicate_ids(path: Path) -> None:
    root = yaml.compose(path.read_text(encoding="utf-8"))
    if root is None:
        return
    _walk_mapping_nodes(root, path=())


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
