from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.narrative_authority import (
    NarrativeDocument,
    PublicationFieldVisibilityPolicy,
    SensitiveOutputFinding,
)


class PublicationSafetyContractError(ValueError):
    pass


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PublicationSafetyContractError(error)
    return value


@dataclass(frozen=True)
class RestrictedLiteral:
    literal_ref: str
    policy_rule_ref: str
    value: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        policy_rule_ref: str,
        value: str,
    ) -> "RestrictedLiteral":
        body = {
            "policy_rule_ref": _required_string(
                policy_rule_ref,
                "restricted_literal_policy_rule_ref_invalid",
            ),
            "value": _required_string(value, "restricted_literal_value_invalid"),
        }
        digest = canonical_digest(body)
        return cls(
            literal_ref="restricted-literal:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RestrictedLiteral":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise PublicationSafetyContractError("restricted_literal_shape_invalid")
        rebuilt = cls.create(
            policy_rule_ref=payload["policy_rule_ref"],
            value=payload["value"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise PublicationSafetyContractError("restricted_literal_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class FixedSensitiveOutputInspector:
    restricted_literals: tuple[RestrictedLiteral, ...]
    registry_digest: str
    visibility_policy_ref: str
    input_mode: str

    @classmethod
    def create(
        cls,
        restricted_literals: Sequence[RestrictedLiteral],
        *,
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> "FixedSensitiveOutputInspector":
        if isinstance(restricted_literals, (str, bytes)) or not isinstance(
            restricted_literals, Sequence
        ):
            raise PublicationSafetyContractError("restricted_literal_registry_invalid")
        normalized = tuple(
            RestrictedLiteral.from_dict(item.to_dict())
            if type(item) is RestrictedLiteral
            else (_raise_registry_invalid())
            for item in restricted_literals
        )
        if not normalized:
            raise PublicationSafetyContractError("restricted_literal_registry_empty")
        if type(visibility_policy) is not PublicationFieldVisibilityPolicy:
            raise PublicationSafetyContractError(
                "restricted_literal_visibility_policy_invalid"
            )
        policy = PublicationFieldVisibilityPolicy.from_dict(visibility_policy.to_dict())
        by_ref = {item.literal_ref: item for item in normalized}
        if len(by_ref) != len(normalized):
            raise PublicationSafetyContractError("restricted_literal_registry_invalid")
        ordered = tuple(by_ref[ref] for ref in sorted(by_ref))
        return cls(
            restricted_literals=ordered,
            registry_digest=canonical_digest(
                {
                    "input_mode": "restricted_literals",
                    "visibility_policy_ref": policy.policy_ref,
                    "restricted_literals": [item.to_dict() for item in ordered],
                }
            ),
            visibility_policy_ref=policy.policy_ref,
            input_mode="restricted_literals",
        )

    @classmethod
    def from_visibility_policy(
        cls,
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> "FixedSensitiveOutputInspector":
        if type(visibility_policy) is not PublicationFieldVisibilityPolicy:
            raise PublicationSafetyContractError(
                "restricted_field_visibility_policy_invalid"
            )
        policy = PublicationFieldVisibilityPolicy.from_dict(visibility_policy.to_dict())
        restricted_literals = tuple(
            RestrictedLiteral.create(
                policy_rule_ref=policy.restricted_output_policy_ref,
                value=field,
            )
            for field in policy.restricted_output_fields
        )
        body = {
            "input_mode": "policy_restricted_field_names",
            "visibility_policy_ref": policy.policy_ref,
            "restricted_literals": [item.to_dict() for item in restricted_literals],
        }
        return cls(
            restricted_literals=restricted_literals,
            registry_digest=canonical_digest(body),
            visibility_policy_ref=policy.policy_ref,
            input_mode="policy_restricted_field_names",
        )

    def __call__(
        self,
        *,
        narrative: NarrativeDocument,
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> tuple[SensitiveOutputFinding, ...]:
        if type(narrative) is not NarrativeDocument:
            raise PublicationSafetyContractError("sensitive_output_narrative_invalid")
        replayed_narrative = NarrativeDocument.from_dict(narrative.to_dict())
        if type(visibility_policy) is not PublicationFieldVisibilityPolicy:
            raise PublicationSafetyContractError(
                "sensitive_output_visibility_policy_invalid"
            )
        policy = PublicationFieldVisibilityPolicy.from_dict(visibility_policy.to_dict())
        if self.visibility_policy_ref != policy.policy_ref or self.input_mode not in {
            "restricted_literals",
            "policy_restricted_field_names",
        }:
            raise PublicationSafetyContractError(
                "sensitive_output_inspector_policy_mismatch"
            )
        findings = tuple(
            SensitiveOutputFinding.create(
                block_id=block.block_id,
                field_visibility_policy_ref=policy.policy_ref,
                policy_rule_ref=literal.policy_rule_ref,
                material_ref=literal.literal_ref,
            )
            for block in replayed_narrative.blocks
            for literal in self.restricted_literals
            if literal.value in block.text
        )
        return tuple(sorted(findings, key=lambda item: item.finding_ref))


def _raise_registry_invalid() -> RestrictedLiteral:
    raise PublicationSafetyContractError("restricted_literal_registry_invalid")


__all__ = (
    "FixedSensitiveOutputInspector",
    "PublicationSafetyContractError",
    "RestrictedLiteral",
)
