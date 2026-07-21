from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from bi_agent.runtime.evidence_authority import canonical_value


CLARIFICATION_ESCAPE_OPTION = "tell the agent to do differently"


def canonical_run_checkpoint_events(
    run_id: str,
    checkpoint_events: tuple[Mapping[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    if (
        not isinstance(run_id, str)
        or not run_id.strip()
        or run_id != run_id.strip()
        or not isinstance(checkpoint_events, tuple)
    ):
        raise ValueError("run_checkpoint_event_invalid")
    normalized_events: list[dict[str, Any]] = []
    for event in checkpoint_events:
        normalized = canonical_value(event)
        if (
            not isinstance(normalized, dict)
            or "name" in normalized
            or not isinstance(normalized.get("node"), str)
            or not normalized["node"].strip()
            or normalized["node"] != normalized["node"].strip()
            or not isinstance(normalized.get("status"), str)
            or not normalized["status"].strip()
            or normalized["status"] != normalized["status"].strip()
        ):
            raise ValueError("run_checkpoint_event_invalid")
        normalized_events.append(normalized)
    return tuple(normalized_events)


@dataclass(frozen=True)
class TurnIntent:
    intent: str
    confidence: float
    topic_relation: str
    decision_source: str
    business_summary: str

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.intent == other
        if isinstance(other, TurnIntent):
            return self.to_dict() == other.to_dict()
        return NotImplemented

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextItem:
    source_type: str
    source_ref: str
    summary: str
    can_support_claims: bool
    reason: str = ""
    source_version: str = ""
    expired: bool = False
    claim_use: str = "context_only"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, init=False)
class ContextManifest:
    manifest_id: str
    thread_id: str
    turn_id: str
    topic_id: str | None
    run_id: str | None
    items: tuple[ContextItem, ...]
    sources: list[dict[str, Any]]
    claim_use_policy: dict[str, Any]
    snapshot_version: str | None
    accepted_assumptions: list[dict[str, Any]]
    contract_versions: dict[str, str]
    schema_fingerprint: str
    created_at: str
    can_support_claims: bool

    def __init__(
        self,
        manifest_id: str,
        thread_id: str,
        turn_id: str,
        items: tuple[ContextItem, ...] | list[ContextItem] | None = None,
        can_support_claims: bool | None = None,
        *,
        topic_id: str | None = None,
        run_id: str | None = None,
        sources: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        claim_use_policy: Mapping[str, Any] | None = None,
        snapshot_version: str | None = None,
        accepted_assumptions: list[dict[str, Any]]
        | tuple[dict[str, Any], ...]
        | None = None,
        contract_versions: Mapping[str, Any] | None = None,
        schema_fingerprint: str | None = None,
        created_at: str | None = None,
    ) -> None:
        normalized_items = tuple(items or ())
        normalized_sources = (
            list(sources)
            if sources is not None
            else [
                {
                    "type": item.source_type,
                    "ref": item.source_ref,
                    "can_support_claim": item.can_support_claims,
                    **item.to_dict(),
                }
                for item in normalized_items
            ]
        )
        if can_support_claims is None:
            can_support_claims = any(
                bool(
                    source.get("can_support_claim") or source.get("can_support_claims")
                )
                for source in normalized_sources
            )
        object.__setattr__(self, "manifest_id", manifest_id)
        object.__setattr__(self, "thread_id", thread_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "topic_id", topic_id)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "items", normalized_items)
        object.__setattr__(self, "sources", normalized_sources)
        default_claim_use_policy = {
            "requires_evidence_ref": True,
            "can_support_bi_claim": bool(can_support_claims),
        }
        object.__setattr__(
            self,
            "claim_use_policy",
            {**default_claim_use_policy, **dict(claim_use_policy or {})},
        )
        object.__setattr__(self, "snapshot_version", snapshot_version)
        object.__setattr__(
            self,
            "accepted_assumptions",
            [dict(item) for item in accepted_assumptions or ()],
        )
        object.__setattr__(
            self,
            "contract_versions",
            {
                str(key): str(value)
                for key, value in dict(contract_versions or {}).items()
                if key not in ("", None) and value not in ("", None)
            },
        )
        object.__setattr__(self, "schema_fingerprint", str(schema_fingerprint or ""))
        object.__setattr__(
            self,
            "created_at",
            created_at or datetime.now(timezone.utc).isoformat(),
        )
        object.__setattr__(self, "can_support_claims", bool(can_support_claims))

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["items"] = [item.to_dict() for item in self.items]
        data["sources"] = list(self.sources)
        data["accepted_assumptions"] = [
            dict(item) for item in self.accepted_assumptions
        ]
        return data


@dataclass(frozen=True)
class ClarificationOption:
    option_id: str
    label: str
    description: str
    recommended: bool = False

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in (self.option_id, self.label, self.description)
        ) or not isinstance(self.recommended, bool):
            raise ValueError("clarification_option_invalid")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClarificationState:
    run_id: str
    topic_id: str
    question: str
    options: list[ClarificationOption]
    status: str = "waiting"
    answer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = [option.to_dict() for option in self.options]
        return data


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    owner_id: str
    text: str
    source_ref: str
    status: str
    ttl: str = "until_revoked"
    confidence: str = "user_confirmed"
    refresh_rule: str = "refresh_on_contract_or_owner_change"
    revocation_path: str = "memory_proposal_revoke_or_admin_action"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryProposal:
    proposal_id: str
    thread_id: str
    text: str
    source_ref: str
    owner_id: str
    status: str = "proposed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConversationRunRequest:
    thread_id: str
    turn_id: str
    topic_id: Optional[str]
    user_message: str
    context_manifest: Mapping[str, Any]
    analysis_context: Mapping[str, Any] = field(default_factory=dict)
    prior_topic_material_context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["prior_topic_material_context"] = canonical_value(
            self.prior_topic_material_context
        )
        return data


@dataclass(frozen=True)
class TopicState:
    topic_id: str
    thread_id: str
    title: str
    summary: str
    status: str = "active"
    assumptions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InteractionResponse:
    schema_version: str
    intent: str
    response_text: str

    def __post_init__(self) -> None:
        if self.schema_version != "typed-interaction.v1":
            raise ValueError("interaction_response_schema_invalid")
        for value in (self.intent, self.response_text):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError("interaction_response_value_invalid")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TopicChoiceOption:
    topic_id: str
    label: str
    description: str

    def __post_init__(self) -> None:
        for value in (self.topic_id, self.label, self.description):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError("topic_choice_option_value_invalid")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TopicChoiceOption":
        if not isinstance(payload, Mapping) or set(payload) != {
            "topic_id",
            "label",
            "description",
        }:
            raise ValueError("topic_choice_option_shape_invalid")
        return cls(
            topic_id=payload["topic_id"],
            label=payload["label"],
            description=payload["description"],
        )


@dataclass(frozen=True)
class TopicChoiceInteractionResponse:
    schema_version: str
    intent: str
    response_text: str
    options: tuple[TopicChoiceOption, ...]
    recommended_topic_id: str
    allow_free_text: bool

    def __post_init__(self) -> None:
        if self.schema_version != "typed-topic-choice.v1":
            raise ValueError("topic_choice_response_schema_invalid")
        for value in (self.intent, self.response_text, self.recommended_topic_id):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError("topic_choice_response_value_invalid")
        if not 2 <= len(self.options) <= 3:
            raise ValueError("topic_choice_response_options_invalid")
        if any(type(option) is not TopicChoiceOption for option in self.options):
            raise ValueError("topic_choice_response_options_invalid")
        topic_ids = tuple(option.topic_id for option in self.options)
        if len(set(topic_ids)) != len(topic_ids):
            raise ValueError("topic_choice_response_options_invalid")
        if self.recommended_topic_id not in set(topic_ids):
            raise ValueError("topic_choice_response_recommendation_invalid")
        if self.allow_free_text is not True:
            raise ValueError("topic_choice_response_free_text_required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent": self.intent,
            "response_text": self.response_text,
            "options": [option.to_dict() for option in self.options],
            "recommended_topic_id": self.recommended_topic_id,
            "allow_free_text": self.allow_free_text,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "TopicChoiceInteractionResponse":
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema_version",
            "intent",
            "response_text",
            "options",
            "recommended_topic_id",
            "allow_free_text",
        }:
            raise ValueError("topic_choice_response_shape_invalid")
        options = payload["options"]
        if not isinstance(options, list):
            raise ValueError("topic_choice_response_options_invalid")
        return cls(
            schema_version=payload["schema_version"],
            intent=payload["intent"],
            response_text=payload["response_text"],
            options=tuple(TopicChoiceOption.from_dict(item) for item in options),
            recommended_topic_id=payload["recommended_topic_id"],
            allow_free_text=payload["allow_free_text"],
        )


@dataclass(frozen=True)
class ConversationTurnResult:
    thread_id: str
    turn_id: str
    topic_id: Optional[str]
    turn_intent: TurnIntent
    topic_relation: str
    context_manifest: ContextManifest
    entry_command: Mapping[str, Any]
    memory_proposals: tuple[MemoryProposal, ...] = ()
    audit_events: tuple[dict[str, Any], ...] = ()
    run_request: Optional[ConversationRunRequest] = None
    interaction_response: Optional[
        InteractionResponse | TopicChoiceInteractionResponse
    ] = None

    @property
    def status(self) -> str:
        if self.run_request:
            return "running"
        return "completed"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["turn_intent"] = self.turn_intent.to_dict()
        data["context_manifest"] = self.context_manifest.to_dict()
        data["memory_proposals"] = [
            proposal.to_dict() for proposal in self.memory_proposals
        ]
        data["run_request"] = self.run_request.to_dict() if self.run_request else None
        data["interaction_response"] = (
            self.interaction_response.to_dict() if self.interaction_response else None
        )
        return data


@dataclass
class ThreadState:
    thread_id: str
    owner_id: str
    current_topic_id: Optional[str] = None
    pending_clarification_topic_id: Optional[str] = None
    pending_clarification_id: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)
