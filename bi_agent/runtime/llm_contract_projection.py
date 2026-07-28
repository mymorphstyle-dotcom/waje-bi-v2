from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, MutableSequence, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


class ContractProjectionError(ValueError):
    pass


def _path(parent: str, field: str) -> str:
    return f"{parent}.{field}" if parent else field


def projection_mutation(
    *,
    path: str,
    action: str,
    reason: str,
) -> dict[str, str]:
    if not all(
        isinstance(value, str) and value.strip()
        for value in (path, action, reason)
    ):
        raise ContractProjectionError("llm_contract_projection_mutation_invalid")
    return {
        "path": path.strip(),
        "action": action.strip(),
        "reason": reason.strip(),
    }


def project_mapping_fields(
    value: Any,
    *,
    allowed_fields: Sequence[str],
    path: str,
    mutations: MutableSequence[Mapping[str, str]],
) -> Any:
    """Project one mapping onto declared consumer fields.

    Non-mapping values are preserved so the strict consumer validator can report
    the authoritative type error. Surplus fields are removed and recorded.
    """

    if not isinstance(value, Mapping):
        return value
    normalized_allowed = tuple(str(field) for field in allowed_fields)
    allowed = frozenset(normalized_allowed)
    projected = {
        field: canonical_value(value[field])
        for field in normalized_allowed
        if field in value
    }
    for field in value:
        if field in allowed:
            continue
        mutations.append(
            projection_mutation(
                path=_path(path, str(field)),
                action="discard_surplus_field",
                reason="outside_consumer_contract",
            )
        )
    return projected


@dataclass(frozen=True)
class ContractProjection:
    output: Mapping[str, Any]
    mutations: tuple[Mapping[str, str], ...]

    @classmethod
    def create(
        cls,
        *,
        output: Mapping[str, Any],
        mutations: Sequence[Mapping[str, str]] = (),
    ) -> "ContractProjection":
        if not isinstance(output, Mapping):
            raise ContractProjectionError("llm_contract_projection_output_invalid")
        normalized_output = canonical_value(dict(output))
        if not isinstance(normalized_output, dict):
            raise ContractProjectionError("llm_contract_projection_output_invalid")
        normalized_mutations: list[Mapping[str, str]] = []
        for mutation in mutations:
            if not isinstance(mutation, Mapping) or set(mutation) != {
                "path",
                "action",
                "reason",
            }:
                raise ContractProjectionError(
                    "llm_contract_projection_mutation_invalid"
                )
            normalized_mutations.append(
                MappingProxyType(
                    projection_mutation(
                        path=mutation["path"],
                        action=mutation["action"],
                        reason=mutation["reason"],
                    )
                )
            )
        return cls(
            output=MappingProxyType(normalized_output),
            mutations=tuple(normalized_mutations),
        )

    @property
    def disposition(self) -> str:
        return "accepted_normalized" if self.mutations else "accepted_exact"

    def audit_record(
        self,
        *,
        raw_output: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(raw_output, Mapping):
            raise ContractProjectionError("llm_contract_projection_input_invalid")
        raw = canonical_value(dict(raw_output))
        projected = canonical_value(dict(self.output))
        if self.disposition == "accepted_exact" and raw != projected:
            raise ContractProjectionError(
                "llm_contract_projection_mutation_record_missing"
            )
        if self.disposition == "accepted_normalized" and raw == projected:
            raise ContractProjectionError(
                "llm_contract_projection_mutation_record_surplus"
            )
        return {
            "schema_version": "llm-contract-projection.v1",
            "disposition": self.disposition,
            "raw_output": raw,
            "raw_output_digest": canonical_digest(raw),
            "canonical_output_digest": canonical_digest(projected),
            "mutation_count": len(self.mutations),
            "mutations": [dict(item) for item in self.mutations],
        }


def exact_contract_projection(
    output: Mapping[str, Any],
) -> ContractProjection:
    return ContractProjection.create(output=output)


def project_required_output_fields(
    output: Mapping[str, Any],
    *,
    required_fields: Sequence[str],
) -> ContractProjection:
    if any(field not in output for field in required_fields):
        return exact_contract_projection(output)
    mutations: list[Mapping[str, str]] = []
    projected = project_mapping_fields(
        output,
        allowed_fields=required_fields,
        path="",
        mutations=mutations,
    )
    if not isinstance(projected, Mapping):
        raise ContractProjectionError("llm_contract_projection_output_invalid")
    return ContractProjection.create(
        output=projected,
        mutations=mutations,
    )


__all__ = (
    "ContractProjection",
    "ContractProjectionError",
    "exact_contract_projection",
    "project_mapping_fields",
    "project_required_output_fields",
    "projection_mutation",
)
