from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class TurnIntent:
    intent: str
    confidence: float
    topic_relation: str
    decision_source: str
    business_summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextItem:
    source_type: str
    source_ref: str
    summary: str
    can_support_claims: bool
    visibility: str = "analyst"
    reason: str = ""
    permission_scope: str = ""
    source_version: str = ""
    expired: bool = False
    claim_use: str = "context_only"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextManifest:
    manifest_id: str
    thread_id: str
    turn_id: str
    items: tuple[ContextItem, ...]
    can_support_claims: bool

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["items"] = [item.to_dict() for item in self.items]
        return data


@dataclass(frozen=True)
class ReuseDecision:
    decision: str
    result_ref: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClarificationOption:
    option_id: str
    label: str
    description: str
    recommended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClarificationQuestion:
    question_id: str
    question: str
    options: tuple[ClarificationOption, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = [option.to_dict() for option in self.options]
        return data


@dataclass(frozen=True)
class ClarificationRequest:
    clarification_id: str
    reason: str
    questions: tuple[ClarificationQuestion, ...]
    allow_freeform: bool = True
    status: str = "waiting_for_user"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["questions"] = [question.to_dict() for question in self.questions]
        return data


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    owner_scope: str
    text: str
    source_ref: str
    visibility: str
    status: str
    ttl: str = "until_revoked"
    confidence: str = "user_confirmed"
    refresh_rule: str = "refresh_on_contract_or_scope_change"
    revocation_path: str = "memory_proposal_revoke_or_admin_action"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryProposal:
    proposal_id: str
    thread_id: str
    text: str
    source_ref: str
    owner_scope: str
    visibility: str
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
    permission_context: Mapping[str, Any]
    runtime_budget: Mapping[str, Any]
    requested_nodes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
class ConversationTurnResult:
    thread_id: str
    turn_id: str
    topic_id: Optional[str]
    turn_intent: TurnIntent
    topic_relation: str
    context_manifest: ContextManifest
    reuse_decisions: tuple[ReuseDecision, ...]
    memory_proposals: tuple[MemoryProposal, ...] = ()
    audit_events: tuple[dict[str, Any], ...] = ()
    run_request: Optional[ConversationRunRequest] = None
    needs_clarification: bool = False
    clarification: Optional[ClarificationRequest] = None
    response_boundary: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["turn_intent"] = self.turn_intent.to_dict()
        data["context_manifest"] = self.context_manifest.to_dict()
        data["reuse_decisions"] = [decision.to_dict() for decision in self.reuse_decisions]
        data["memory_proposals"] = [proposal.to_dict() for proposal in self.memory_proposals]
        data["run_request"] = self.run_request.to_dict() if self.run_request else None
        data["clarification"] = self.clarification.to_dict() if self.clarification else None
        return data


@dataclass
class ThreadState:
    thread_id: str
    owner_id: str
    current_topic_id: Optional[str] = None
    pending_clarification_topic_id: Optional[str] = None
    pending_clarification_id: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ResultRefRecord:
    topic_id: str
    result_ref: str
    snapshot_id: str
    contract_version: str
    permission_scope: str
    semantic_scope: str


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    topic_id: str
    follow_up_context: str
    snapshot_id: str
    permission_scope: str
