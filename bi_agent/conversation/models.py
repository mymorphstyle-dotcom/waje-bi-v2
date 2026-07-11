from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


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
    visibility: str = "analyst"
    reason: str = ""
    permission_scope: str = ""
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
    items: tuple[ContextItem, ...]
    sources: list[dict[str, Any]]
    claim_use_policy: dict[str, Any]
    snapshot_version: str | None
    permission_context: dict[str, Any]
    analysis_assets: list[dict[str, Any]]
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
        sources: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        claim_use_policy: Mapping[str, Any] | None = None,
        snapshot_version: str | None = None,
        permission_context: Mapping[str, Any] | None = None,
        analysis_assets: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        accepted_assumptions: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        contract_versions: Mapping[str, Any] | None = None,
        schema_fingerprint: str | None = None,
        created_at: str | None = None,
    ) -> None:
        normalized_items = tuple(items or ())
        normalized_sources = list(sources) if sources is not None else [
            {
                "type": item.source_type,
                "ref": item.source_ref,
                "can_support_claim": item.can_support_claims,
                **item.to_dict(),
            }
            for item in normalized_items
        ]
        if can_support_claims is None:
            can_support_claims = any(
                bool(source.get("can_support_claim") or source.get("can_support_claims"))
                for source in normalized_sources
            )
        object.__setattr__(self, "manifest_id", manifest_id)
        object.__setattr__(self, "thread_id", thread_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "topic_id", topic_id)
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
        object.__setattr__(self, "permission_context", dict(permission_context or {}))
        object.__setattr__(self, "analysis_assets", [dict(item) for item in analysis_assets or ()])
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
        data["analysis_assets"] = [dict(item) for item in self.analysis_assets]
        data["accepted_assumptions"] = [dict(item) for item in self.accepted_assumptions]
        return data


@dataclass(frozen=True, init=False)
class ReuseDecision:
    source_ref: str
    decision: str
    result_ref: str
    reason: str
    can_support_claim: bool
    requires_rerun: bool

    def __init__(
        self,
        decision: str,
        result_ref: str = "",
        reason: str = "",
        can_support_claim: bool | None = None,
        requires_rerun: bool | None = None,
        *,
        source_ref: str | None = None,
    ) -> None:
        known_decisions = {"reuse", "rerun", "context_only", "blocked", "none"}
        if decision not in known_decisions and result_ref in known_decisions:
            source_ref = decision
            decision = result_ref
            result_ref = source_ref
        ref = source_ref if source_ref is not None else result_ref
        if can_support_claim is None:
            can_support_claim = decision == "reuse"
        if requires_rerun is None:
            requires_rerun = decision in {"blocked", "context_only", "rerun"}
        object.__setattr__(self, "source_ref", ref)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "result_ref", result_ref or ref)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "can_support_claim", bool(can_support_claim))
        object.__setattr__(self, "requires_rerun", bool(requires_rerun))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, init=False)
class ClarificationOption:
    option_id: str
    label: str
    description: str
    recommended: bool = False

    def __init__(
        self,
        option_id: str | None = None,
        label: str = "",
        description: str | None = None,
        recommended: bool = False,
        *,
        id: str | None = None,
        business_meaning: str | None = None,
    ) -> None:
        object.__setattr__(self, "option_id", option_id or id or "")
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "description", description or business_meaning or "")
        object.__setattr__(self, "recommended", recommended)

    @property
    def id(self) -> str:
        return self.option_id

    @property
    def business_meaning(self) -> str:
        return self.description

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["id"] = self.option_id
        data["business_meaning"] = self.description
        return data


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
    analysis_context: Mapping[str, Any] = field(default_factory=dict)
    clarification_resume_context: Mapping[str, Any] = field(default_factory=dict)
    prior_analysis_assets: tuple[Mapping[str, Any], ...] = ()
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

    @property
    def status(self) -> str:
        if self.needs_clarification:
            return "waiting_for_clarification"
        if self.run_request:
            return "running"
        return "completed"

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
