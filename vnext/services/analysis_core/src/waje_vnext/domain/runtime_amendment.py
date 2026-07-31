"""Gate 3 durable-async amendment records.

These records keep semantic binding, measurement review, worker disposition,
and model trace state replayable.  They carry no mutable accepted head; the
``InvestigationCase`` CAS row remains the only acceptance authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .async_runtime import (
    AsyncJobKind,
    AuthoritySnapshot,
    OperationIdentity,
)
from .authority import (
    AnalysisFrameRevision,
    DecisionOption,
    QuestionRevision,
)
from .canonical import (
    FrozenJson,
    content_sha256,
    freeze_json,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
    to_jsonable,
)
from .events import JournalEventType


class MessageBindingDisposition(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_USER_DECISION = "needs_user_decision"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class MessageImpactKind(StrEnum):
    CONTEXT_ONLY = "context_only"
    QUESTION_REVISION = "question_revision"
    FRAME_REVISION = "frame_revision"
    CHALLENGE = "challenge"
    STOP_REQUEST = "stop_request"


class FrameReviewDisposition(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"
    BLOCK = "block"


class MeasurementObjectionSeverity(StrEnum):
    ADVISORY = "advisory"
    MATERIAL = "material"
    BLOCKING = "blocking"


class JobDisposition(StrEnum):
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    TERMINAL_FAILURE = "terminal_failure"


def derive_changed_measurement_node_ids(
    prior_design,
    replacement_design,
) -> tuple[str, ...]:
    changed: list[str] = []

    def visit(prior, replacement, path: str) -> None:
        if type(prior) is not type(replacement):
            changed.append(path)
            return
        if isinstance(prior, dict):
            for key in sorted(set(prior) | set(replacement)):
                child_path = "{}.{}".format(path, key)
                if key not in prior or key not in replacement:
                    changed.append(child_path)
                else:
                    visit(prior[key], replacement[key], child_path)
            return
        if isinstance(prior, list):
            for index in range(max(len(prior), len(replacement))):
                child_path = "{}.{}".format(path, index)
                if index >= len(prior) or index >= len(replacement):
                    changed.append(child_path)
                else:
                    visit(
                        prior[index],
                        replacement[index],
                        child_path,
                    )
            return
        if prior != replacement:
            changed.append(path)

    visit(
        to_jsonable(prior_design),
        to_jsonable(replacement_design),
        "measurement_design",
    )
    return tuple(sorted(set(changed)))


def measurement_paths_overlap(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(right + ".")
        or right.startswith(left + ".")
    )


class ProviderAttemptDisposition(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    REFUSAL = "refusal"
    INCOMPLETE = "incomplete"
    MULTIPLE_TOOL_CALLS = "multiple_tool_calls"
    SUPERSEDED = "superseded"


class RunTraceProfile(StrEnum):
    CASE_AUTHORITY_LANE = "case_authority_lane"


class ModelExecutionRole(StrEnum):
    PRIMARY_BUSINESS_ANALYSIS_AGENT = "primary_business_analysis_agent"
    RUNTIME_REVIEWER = "runtime_reviewer"
    EVALUATION_REVIEWER = "evaluation_reviewer"


class ModelInputViewKind(StrEnum):
    MESSAGE_BINDING_VIEW = "message_binding_view"
    AGENT_WORLD_VIEW = "agent_world_view"
    MEASUREMENT_REVIEW_VIEW = "measurement_review_view"
    EVALUATION_REVIEW_VIEW = "evaluation_review_view"


@dataclass(frozen=True, slots=True)
class ModelConfigurationIdentity:
    execution_role: ModelExecutionRole
    provider_ref: str
    endpoint_ref: str
    protocol_ref: str
    adapter_release_ref: str
    adapter_release_sha256: str
    model_ref: str
    thinking: str
    stable_parameters: Mapping[str, FrozenJson]
    delivery_policy_ref: str
    max_attempts: int
    timeout_seconds: float | None
    configuration_sha256: str

    @classmethod
    def build(
        cls,
        *,
        execution_role: ModelExecutionRole,
        provider_ref: str,
        endpoint_ref: str,
        protocol_ref: str,
        adapter_release_ref: str,
        adapter_release_sha256: str,
        model_ref: str,
        thinking: str,
        stable_parameters: Mapping[str, FrozenJson],
        delivery_policy_ref: str,
        max_attempts: int,
        timeout_seconds: float | None,
    ) -> "ModelConfigurationIdentity":
        content = {
            "execution_role": execution_role.value,
            "provider_ref": provider_ref,
            "endpoint_ref": endpoint_ref,
            "protocol_ref": protocol_ref,
            "adapter_release_ref": adapter_release_ref,
            "adapter_release_sha256": adapter_release_sha256,
            "model_ref": model_ref,
            "thinking": thinking,
            "stable_parameters": stable_parameters,
            "delivery_policy_ref": delivery_policy_ref,
            "max_attempts": max_attempts,
            "timeout_seconds": timeout_seconds,
        }
        return cls(
            execution_role=execution_role,
            provider_ref=provider_ref,
            endpoint_ref=endpoint_ref,
            protocol_ref=protocol_ref,
            adapter_release_ref=adapter_release_ref,
            adapter_release_sha256=adapter_release_sha256,
            model_ref=model_ref,
            thinking=thinking,
            stable_parameters=stable_parameters,
            delivery_policy_ref=delivery_policy_ref,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            configuration_sha256=content_sha256(content),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.execution_role, ModelExecutionRole):
            raise TypeError("execution_role must be ModelExecutionRole")
        for field_name in (
            "provider_ref",
            "endpoint_ref",
            "protocol_ref",
            "adapter_release_ref",
            "model_ref",
            "thinking",
            "delivery_policy_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        require_sha256(
            self.adapter_release_sha256,
            "adapter_release_sha256",
        )
        if self.thinking not in {"enabled", "disabled"}:
            raise ValueError("thinking must be enabled or disabled")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError(
                "timeout_seconds must be positive when configured"
            )
        stable = _freeze_object(
            self.stable_parameters,
            "stable_parameters",
        )
        object.__setattr__(self, "stable_parameters", stable)
        require_sha256(
            self.configuration_sha256,
            "configuration_sha256",
        )
        if self.configuration_sha256 != content_sha256(
            _model_configuration_content(self)
        ):
            raise ValueError("configuration_sha256 is stale")

    @property
    def operational_configuration_sha256(self) -> str:
        return content_sha256(
            _model_configuration_operational_content(self)
        )


@dataclass(frozen=True, slots=True)
class ModelRequestArtifact:
    model_request_artifact_id: str
    logical_model_job_id: str
    execution_role: ModelExecutionRole
    logical_job_kind: str
    input_view_kind: ModelInputViewKind
    input_view_ref: str
    input_view_sha256: str
    typed_request_contract_ref: str
    typed_request_sha256: str
    prompt_bundle_ref: str
    prompt_bundle_sha256: str
    tool_bundle_ref: str
    tool_bundle_sha256: str
    output_contract_ref: str
    output_contract_sha256: str
    decoder_release_ref: str
    decoder_release_sha256: str
    provider_request_body: Mapping[str, FrozenJson]
    provider_request_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.execution_role, ModelExecutionRole):
            raise TypeError("execution_role must be ModelExecutionRole")
        if not isinstance(self.input_view_kind, ModelInputViewKind):
            raise TypeError("input_view_kind must be ModelInputViewKind")
        for field_name in (
            "model_request_artifact_id",
            "logical_model_job_id",
            "logical_job_kind",
            "input_view_ref",
            "typed_request_contract_ref",
            "prompt_bundle_ref",
            "tool_bundle_ref",
            "output_contract_ref",
            "decoder_release_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        for field_name in (
            "input_view_sha256",
            "typed_request_sha256",
            "prompt_bundle_sha256",
            "tool_bundle_sha256",
            "output_contract_sha256",
            "decoder_release_sha256",
            "provider_request_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        body = _freeze_object(
            self.provider_request_body,
            "provider_request_body",
        )
        object.__setattr__(self, "provider_request_body", body)
        if content_sha256(body) != self.provider_request_sha256:
            raise ValueError("provider request body hash is stale")
        require_aware_datetime(self.created_at, "created_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


class SemanticAssertionKind(StrEnum):
    METRIC = "metric"
    POPULATION = "population"
    OBSERVATION_UNIT = "observation_unit"
    TIME_SEMANTIC = "time_semantic"
    WINDOW = "window"
    COMPARISON = "comparison"
    EXPOSURE = "exposure"
    BUSINESS_CONSTRAINT = "business_constraint"
    CLAIM_STRENGTH = "claim_strength"


@dataclass(frozen=True, slots=True)
class ProposedSemanticAssertion:
    kind: SemanticAssertionKind
    value_json: str
    source_start_codepoint: int
    source_end_codepoint: int
    material: bool

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SemanticAssertionKind):
            raise TypeError("kind must be SemanticAssertionKind")
        require_nonempty(self.value_json, "value_json")
        if self.source_start_codepoint < 0:
            raise ValueError("semantic source start must be non-negative")
        if self.source_end_codepoint <= self.source_start_codepoint:
            raise ValueError("semantic source end must follow start")


@dataclass(frozen=True, slots=True)
class ProposedSemanticAmbiguity:
    question: str
    recommended_interpretation_json: str
    source_start_codepoint: int
    source_end_codepoint: int
    material: bool

    def __post_init__(self) -> None:
        for field_name in (
            "question",
            "recommended_interpretation_json",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if self.source_start_codepoint < 0:
            raise ValueError("ambiguity source start must be non-negative")
        if self.source_end_codepoint <= self.source_start_codepoint:
            raise ValueError("ambiguity source end must follow start")


@dataclass(frozen=True, slots=True)
class MessageImpactProposal:
    impact_kind: MessageImpactKind
    disposition: MessageBindingDisposition
    assertions: tuple[ProposedSemanticAssertion, ...]
    ambiguities: tuple[ProposedSemanticAmbiguity, ...]
    clarification_options: tuple[DecisionOption, ...]
    recommended_option_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.impact_kind, MessageImpactKind):
            raise TypeError("impact_kind must be MessageImpactKind")
        if not isinstance(
            self.disposition,
            MessageBindingDisposition,
        ):
            raise TypeError(
                "disposition must be MessageBindingDisposition"
            )
        if not isinstance(self.assertions, tuple) or any(
            not isinstance(item, ProposedSemanticAssertion)
            for item in self.assertions
        ):
            raise TypeError(
                "assertions must contain ProposedSemanticAssertion"
            )
        if not self.assertions:
            raise ValueError("message impact proposal requires assertions")
        if not isinstance(self.ambiguities, tuple) or any(
            not isinstance(item, ProposedSemanticAmbiguity)
            for item in self.ambiguities
        ):
            raise TypeError(
                "ambiguities must contain ProposedSemanticAmbiguity"
            )
        if not isinstance(self.clarification_options, tuple) or any(
            not isinstance(item, DecisionOption)
            for item in self.clarification_options
        ):
            raise TypeError(
                "clarification_options must contain DecisionOption"
            )
        if (
            self.disposition
            is MessageBindingDisposition.NEEDS_USER_DECISION
        ):
            if not 2 <= len(self.clarification_options) <= 3:
                raise ValueError(
                    "material ambiguity requires two or three options"
                )
            option_ids = {
                option.option_id
                for option in self.clarification_options
            }
            if self.recommended_option_id not in option_ids:
                raise ValueError(
                    "recommended clarification option is missing"
                )
            if not any(item.material for item in self.ambiguities):
                raise ValueError(
                    "user decision requires a material ambiguity"
                )
        elif (
            self.disposition is MessageBindingDisposition.ACCEPTED
            and any(item.material for item in self.ambiguities)
        ):
            raise ValueError(
                "accepted binding cannot carry unresolved material ambiguity"
            )
        elif self.clarification_options or (
            self.recommended_option_id is not None
        ):
            raise ValueError(
                "accepted binding cannot carry clarification options"
            )

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class MessageBindingRequest:
    logical_model_job_id: str
    case_id: str
    message_id: str
    message_content: str
    prior_question_text: str | None
    has_accepted_frame: bool
    binding_contract_ref: str
    requested_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "logical_model_job_id",
            "case_id",
            "message_id",
            "message_content",
            "binding_contract_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if self.prior_question_text is not None:
            require_nonempty(
                self.prior_question_text,
                "prior_question_text",
            )
        require_aware_datetime(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class MessageIngressRecord:
    ingress_record_id: str
    case_id: str
    run_id: str
    message_id: str
    mailbox_sequence: int
    authority_epoch: int
    operation: OperationIdentity
    message_payload_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "ingress_record_id",
            "case_id",
            "run_id",
            "message_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.mailbox_sequence < 1:
            raise ValueError("mailbox_sequence must be positive")
        if self.authority_epoch < 1:
            raise ValueError("authority_epoch must be positive")
        if self.operation.authority_revision not in {
            0,
            self.authority_epoch,
        }:
            raise ValueError("ingress operation authority epoch is inconsistent")
        require_sha256(
            self.message_payload_sha256,
            "message_payload_sha256",
        )
        if self.operation.payload_sha256 != self.message_payload_sha256:
            raise ValueError("ingress payload hash must match operation")
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class PendingUserMessage:
    pending_message_id: str
    ingress_record_id: str
    case_id: str
    message_id: str
    binding_job_id: str
    authority_epoch: int
    source_operation_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "pending_message_id",
            "ingress_record_id",
            "case_id",
            "message_id",
            "binding_job_id",
            "source_operation_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.authority_epoch < 1:
            raise ValueError("authority_epoch must be positive")
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class SemanticSourceSpan:
    span_id: str
    message_id: str
    start_codepoint: int
    end_codepoint: int
    selected_text_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("span_id", "message_id"):
            require_nonempty(getattr(self, field_name), field_name)
        if self.start_codepoint < 0:
            raise ValueError("source span start must be non-negative")
        if self.end_codepoint <= self.start_codepoint:
            raise ValueError("source span end must follow start")
        require_sha256(
            self.selected_text_sha256,
            "selected_text_sha256",
        )


@dataclass(frozen=True, slots=True)
class SemanticAssertion:
    assertion_id: str
    kind: SemanticAssertionKind
    value: Mapping[str, FrozenJson]
    source_span_ids: tuple[str, ...]
    decision_record_ids: tuple[str, ...]
    material: bool

    def __post_init__(self) -> None:
        require_nonempty(self.assertion_id, "assertion_id")
        if not isinstance(self.kind, SemanticAssertionKind):
            raise TypeError("kind must be SemanticAssertionKind")
        frozen = _freeze_object(self.value, "value")
        if not frozen:
            raise ValueError("semantic assertion value cannot be empty")
        object.__setattr__(self, "value", frozen)
        _require_string_tuple(self.source_span_ids, "source_span_ids")
        _require_string_tuple(
            self.decision_record_ids,
            "decision_record_ids",
        )
        if not self.source_span_ids and not self.decision_record_ids:
            raise ValueError(
                "semantic assertion requires source or decision grounding"
            )


@dataclass(frozen=True, slots=True)
class SemanticAmbiguity:
    ambiguity_id: str
    question: str
    material: bool
    recommended_interpretation: Mapping[str, FrozenJson]
    source_span_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("ambiguity_id", "question"):
            require_nonempty(getattr(self, field_name), field_name)
        frozen = _freeze_object(
            self.recommended_interpretation,
            "recommended_interpretation",
        )
        if not frozen:
            raise ValueError(
                "ambiguity requires a recommended interpretation"
            )
        object.__setattr__(
            self,
            "recommended_interpretation",
            frozen,
        )
        _require_string_tuple(self.source_span_ids, "source_span_ids")
        if not self.source_span_ids:
            raise ValueError("ambiguity requires source grounding")


@dataclass(frozen=True, slots=True)
class TypedSemanticBinding:
    binding_contract_version: str
    source_spans: tuple[SemanticSourceSpan, ...]
    assertions: tuple[SemanticAssertion, ...]
    ambiguities: tuple[SemanticAmbiguity, ...]
    decision_ledger_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(
            self.binding_contract_version,
            "binding_contract_version",
        )
        if not isinstance(self.source_spans, tuple) or any(
            not isinstance(item, SemanticSourceSpan)
            for item in self.source_spans
        ):
            raise TypeError("source_spans must contain SemanticSourceSpan")
        if not isinstance(self.assertions, tuple) or any(
            not isinstance(item, SemanticAssertion)
            for item in self.assertions
        ):
            raise TypeError("assertions must contain SemanticAssertion")
        if not isinstance(self.ambiguities, tuple) or any(
            not isinstance(item, SemanticAmbiguity)
            for item in self.ambiguities
        ):
            raise TypeError("ambiguities must contain SemanticAmbiguity")
        _require_string_tuple(
            self.decision_ledger_refs,
            "decision_ledger_refs",
        )
        span_ids = tuple(item.span_id for item in self.source_spans)
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("semantic source span IDs must be unique")
        assertion_ids = tuple(
            item.assertion_id for item in self.assertions
        )
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError("semantic assertion IDs must be unique")
        ambiguity_ids = tuple(
            item.ambiguity_id for item in self.ambiguities
        )
        if len(ambiguity_ids) != len(set(ambiguity_ids)):
            raise ValueError("semantic ambiguity IDs must be unique")
        referenced_span_ids = {
            span_id
            for item in self.assertions
            for span_id in item.source_span_ids
        } | {
            span_id
            for item in self.ambiguities
            for span_id in item.source_span_ids
        }
        if not referenced_span_ids <= set(span_ids):
            raise ValueError("semantic binding references unknown source spans")
        if not self.assertions:
            raise ValueError("semantic binding requires assertions")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class MessageImpactBinding:
    binding_id: str
    pending_message_id: str
    case_id: str
    message_id: str
    authority_epoch: int
    source_payload_sha256: str
    impact_kind: MessageImpactKind
    disposition: MessageBindingDisposition
    bound_question_revision_id: str | None
    prior_frame_revision_id: str | None
    decision_record_ids: tuple[str, ...]
    semantic_binding: TypedSemanticBinding
    semantic_binding_sha256: str
    logical_model_job_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "binding_id",
            "pending_message_id",
            "case_id",
            "message_id",
            "logical_model_job_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.authority_epoch < 1:
            raise ValueError("authority_epoch must be positive")
        require_sha256(
            self.source_payload_sha256,
            "source_payload_sha256",
        )
        if not isinstance(self.impact_kind, MessageImpactKind):
            raise TypeError("impact_kind must be MessageImpactKind")
        if not isinstance(self.disposition, MessageBindingDisposition):
            raise TypeError(
                "disposition must be MessageBindingDisposition"
            )
        for field_name in (
            "bound_question_revision_id",
            "prior_frame_revision_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                require_nonempty(value, field_name)
        _require_string_tuple(self.decision_record_ids, "decision_record_ids")
        if not isinstance(self.semantic_binding, TypedSemanticBinding):
            raise TypeError(
                "semantic_binding must be TypedSemanticBinding"
            )
        require_sha256(
            self.semantic_binding_sha256,
            "semantic_binding_sha256",
        )
        if (
            self.semantic_binding.content_sha256
            != self.semantic_binding_sha256
        ):
            raise ValueError("semantic binding hash does not match payload")
        if (
            self.disposition is MessageBindingDisposition.ACCEPTED
            and self.impact_kind
            in {
                MessageImpactKind.QUESTION_REVISION,
                MessageImpactKind.FRAME_REVISION,
            }
            and self.bound_question_revision_id is None
        ):
            raise ValueError(
                "accepted authority-changing binding requires question revision"
            )
        if (
            self.disposition
            is MessageBindingDisposition.NEEDS_USER_DECISION
            and self.decision_record_ids
        ):
            raise ValueError(
                "pending user decision cannot claim accepted decisions"
            )
        require_aware_datetime(self.created_at, "created_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class FrameCandidateRecord:
    frame_candidate_id: str
    case_id: str
    message_binding_id: str
    question_revision_id: str
    proposed_frame_revision_id: str
    proposed_frame_content_sha256: str
    proposed_frame: AnalysisFrameRevision
    candidate_generation: int
    prior_frame_candidate_id: str | None
    addressed_objection_ids: tuple[str, ...]
    authority_epoch: int
    source_action_id: str
    source_operation_id: str
    review_job_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "frame_candidate_id",
            "case_id",
            "message_binding_id",
            "question_revision_id",
            "proposed_frame_revision_id",
            "source_action_id",
            "source_operation_id",
            "review_job_id",
        ):
            require_nonempty(getattr(self, name), name)
        require_sha256(
            self.proposed_frame_content_sha256,
            "proposed_frame_content_sha256",
        )
        if not isinstance(self.proposed_frame, AnalysisFrameRevision):
            raise TypeError(
                "proposed_frame must be AnalysisFrameRevision"
            )
        if (
            self.proposed_frame.frame_revision_id
            != self.proposed_frame_revision_id
            or self.proposed_frame.content_sha256
            != self.proposed_frame_content_sha256
            or self.proposed_frame.case_id != self.case_id
        ):
            raise ValueError(
                "candidate Frame identity does not match its envelope"
            )
        if self.candidate_generation < 1:
            raise ValueError("candidate_generation must be positive")
        if self.candidate_generation == 1:
            if self.prior_frame_candidate_id is not None:
                raise ValueError("first candidate cannot have a prior candidate")
            if self.addressed_objection_ids:
                raise ValueError(
                    "first candidate cannot close prior objections"
                )
        elif self.prior_frame_candidate_id is None:
            raise ValueError("later candidate requires prior candidate")
        if self.prior_frame_candidate_id is not None:
            require_nonempty(
                self.prior_frame_candidate_id,
                "prior_frame_candidate_id",
            )
        _require_string_tuple(
            self.addressed_objection_ids,
            "addressed_objection_ids",
        )
        if self.authority_epoch < 1:
            raise ValueError("authority_epoch must be positive")
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class FrameCandidateSupersessionRecord:
    supersession_record_id: str
    case_id: str
    frame_candidate_id: str
    superseded_by_question_revision_id: str
    source_operation_id: str
    authority_epoch: int
    reason_code: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "supersession_record_id",
            "case_id",
            "frame_candidate_id",
            "superseded_by_question_revision_id",
            "source_operation_id",
            "reason_code",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if self.authority_epoch < 1:
            raise ValueError("authority_epoch must be positive")
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class MeasurementReviewObjection:
    objection_id: str
    code: str
    severity: MeasurementObjectionSeverity
    affected_node_ids: tuple[str, ...]
    explanation: str

    def __post_init__(self) -> None:
        for name in ("objection_id", "code", "explanation"):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.severity, MeasurementObjectionSeverity):
            raise TypeError(
                "severity must be MeasurementObjectionSeverity"
            )
        _require_string_tuple(
            self.affected_node_ids,
            "affected_node_ids",
        )

    @property
    def blocking(self) -> bool:
        return self.severity is MeasurementObjectionSeverity.BLOCKING


@dataclass(frozen=True, slots=True)
class ProposedMeasurementObjection:
    code: str
    severity: MeasurementObjectionSeverity
    affected_node_refs: tuple[str, ...]
    explanation: str

    def __post_init__(self) -> None:
        for field_name in ("code", "explanation"):
            require_nonempty(getattr(self, field_name), field_name)
        if not isinstance(self.severity, MeasurementObjectionSeverity):
            raise TypeError(
                "severity must be MeasurementObjectionSeverity"
            )
        _require_string_tuple(
            self.affected_node_refs,
            "affected_node_refs",
        )
        if (
            self.severity is MeasurementObjectionSeverity.BLOCKING
            and not self.affected_node_refs
        ):
            raise ValueError(
                "blocking objection requires affected measurement nodes"
            )


@dataclass(frozen=True, slots=True)
class FrameReviewProposal:
    disposition: FrameReviewDisposition
    objections: tuple[ProposedMeasurementObjection, ...]
    review_summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, FrameReviewDisposition):
            raise TypeError("disposition must be FrameReviewDisposition")
        if not isinstance(self.objections, tuple) or any(
            not isinstance(item, ProposedMeasurementObjection)
            for item in self.objections
        ):
            raise TypeError(
                "objections must contain ProposedMeasurementObjection"
            )
        require_nonempty(self.review_summary, "review_summary")
        blocking = any(
            item.severity is MeasurementObjectionSeverity.BLOCKING
            for item in self.objections
        )
        if self.disposition is FrameReviewDisposition.ACCEPT and blocking:
            raise ValueError(
                "Reviewer proposal cannot accept its own blocking objection"
            )
        if self.disposition is FrameReviewDisposition.BLOCK and not blocking:
            raise ValueError(
                "blocking proposal requires a blocking objection"
            )
        if (
            self.disposition is FrameReviewDisposition.REVISE
            and not self.objections
        ):
            raise ValueError("revision proposal requires an objection")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class DeterministicFrameValidationFinding:
    code: str
    node_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.code, "code")
        _require_string_tuple(self.node_refs, "node_refs")


@dataclass(frozen=True, slots=True)
class FrameReviewRequest:
    logical_model_job_id: str
    case_id: str
    frame_candidate: FrameCandidateRecord
    accepted_question: QuestionRevision
    accepted_message_bindings: tuple[MessageImpactBinding, ...]
    prior_frame_review: FrameReviewRecord | None
    objection_closures: tuple[ObjectionClosureRecord, ...]
    deterministic_validation_findings: tuple[
        DeterministicFrameValidationFinding,
        ...,
    ]
    review_contract_ref: str
    reviewer_configuration_ref: str
    independence_policy_ref: str
    requested_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "logical_model_job_id",
            "case_id",
            "review_contract_ref",
            "reviewer_configuration_ref",
            "independence_policy_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if not isinstance(self.frame_candidate, FrameCandidateRecord):
            raise TypeError(
                "frame_candidate must be FrameCandidateRecord"
            )
        if self.frame_candidate.case_id != self.case_id:
            raise ValueError("review request candidate belongs to another case")
        if (
            not isinstance(self.accepted_question, QuestionRevision)
            or self.accepted_question.case_id != self.case_id
            or self.accepted_question.question_revision_id
            != self.frame_candidate.question_revision_id
        ):
            raise ValueError(
                "review request must carry the candidate question authority"
            )
        if (
            not isinstance(self.accepted_message_bindings, tuple)
            or not self.accepted_message_bindings
            or any(
                not isinstance(item, MessageImpactBinding)
                for item in self.accepted_message_bindings
            )
        ):
            raise TypeError(
                "review request requires typed message bindings"
            )
        if any(
            item.case_id != self.case_id
            for item in self.accepted_message_bindings
        ):
            raise ValueError("review message binding crosses cases")
        expected_binding_ids = set(
            self.accepted_question.explicit_constraint_refs
        )
        if {
            item.binding_id
            for item in self.accepted_message_bindings
        } != expected_binding_ids:
            raise ValueError(
                "review request message bindings are incomplete"
            )
        if self.prior_frame_review is not None and not isinstance(
            self.prior_frame_review,
            FrameReviewRecord,
        ):
            raise TypeError(
                "prior_frame_review must be FrameReviewRecord"
            )
        if not isinstance(self.objection_closures, tuple) or any(
            not isinstance(item, ObjectionClosureRecord)
            for item in self.objection_closures
        ):
            raise TypeError(
                "objection_closures must contain ObjectionClosureRecord"
            )
        if {
            item.replacement_frame_candidate_id
            for item in self.objection_closures
        } - {self.frame_candidate.frame_candidate_id}:
            raise ValueError(
                "review objection closure targets another candidate"
            )
        if not isinstance(
            self.deterministic_validation_findings,
            tuple,
        ) or any(
            not isinstance(
                item,
                DeterministicFrameValidationFinding,
            )
            for item in self.deterministic_validation_findings
        ):
            raise TypeError(
                "deterministic findings use an invalid contract"
            )
        require_aware_datetime(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class ObjectionClosureRecord:
    objection_closure_id: str
    objection_id: str
    source_frame_review_id: str
    source_frame_candidate_id: str
    replacement_frame_candidate_id: str
    objection_content_sha256: str
    changed_node_ids: tuple[str, ...]
    closure_explanation: str
    derivation_proof_sha256: str
    created_by_action_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "objection_closure_id",
            "objection_id",
            "source_frame_review_id",
            "source_frame_candidate_id",
            "replacement_frame_candidate_id",
            "closure_explanation",
            "created_by_action_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if (
            self.source_frame_candidate_id
            == self.replacement_frame_candidate_id
        ):
            raise ValueError(
                "objection closure requires a replacement candidate"
            )
        require_sha256(
            self.objection_content_sha256,
            "objection_content_sha256",
        )
        _require_string_tuple(
            self.changed_node_ids,
            "changed_node_ids",
        )
        if not self.changed_node_ids:
            raise ValueError(
                "objection closure requires a measured design change"
            )
        require_sha256(
            self.derivation_proof_sha256,
            "derivation_proof_sha256",
        )
        expected_proof_sha256 = content_sha256(
            {
                "kind": "objection-closure-derivation.v1",
                "objection_id": self.objection_id,
                "source_frame_review_id": self.source_frame_review_id,
                "source_frame_candidate_id": (
                    self.source_frame_candidate_id
                ),
                "replacement_frame_candidate_id": (
                    self.replacement_frame_candidate_id
                ),
                "objection_content_sha256": (
                    self.objection_content_sha256
                ),
                "changed_node_ids": self.changed_node_ids,
                "closure_explanation": self.closure_explanation,
                "created_by_action_id": self.created_by_action_id,
            }
        )
        if self.derivation_proof_sha256 != expected_proof_sha256:
            raise ValueError("objection closure derivation hash is stale")
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class FrameReviewRecord:
    frame_review_id: str
    frame_candidate_id: str
    reviewer_job_id: str
    authority_epoch: int
    disposition: FrameReviewDisposition
    objections: tuple[MeasurementReviewObjection, ...]
    closure_proof_refs: tuple[str, ...]
    reviewed_frame_content_sha256: str
    logical_model_job_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "frame_review_id",
            "frame_candidate_id",
            "reviewer_job_id",
            "logical_model_job_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.authority_epoch < 1:
            raise ValueError("authority_epoch must be positive")
        if not isinstance(self.disposition, FrameReviewDisposition):
            raise TypeError("disposition must be FrameReviewDisposition")
        if not isinstance(self.objections, tuple) or any(
            not isinstance(item, MeasurementReviewObjection)
            for item in self.objections
        ):
            raise TypeError(
                "objections must contain MeasurementReviewObjection"
            )
        objection_ids = tuple(item.objection_id for item in self.objections)
        if len(objection_ids) != len(set(objection_ids)):
            raise ValueError("review objection IDs must be unique")
        _require_string_tuple(
            self.closure_proof_refs,
            "closure_proof_refs",
        )
        require_sha256(
            self.reviewed_frame_content_sha256,
            "reviewed_frame_content_sha256",
        )
        blocking_ids = {
            item.objection_id for item in self.objections if item.blocking
        }
        if (
            self.disposition is FrameReviewDisposition.ACCEPT
            and blocking_ids
        ):
            raise ValueError(
                "a review cannot accept a candidate it currently blocks"
            )
        if (
            self.disposition is FrameReviewDisposition.BLOCK
            and not blocking_ids
        ):
            raise ValueError("blocking review requires a blocking objection")
        if (
            self.disposition is FrameReviewDisposition.REVISE
            and not self.objections
        ):
            raise ValueError("revision review requires an objection")
        require_aware_datetime(self.created_at, "created_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class FrameAdmissionProof:
    frame_admission_proof_id: str
    case_id: str
    frame_candidate_id: str
    candidate_generation: int
    frame_revision_id: str
    frame_content_sha256: str
    frame_review_id: str
    frame_review_content_sha256: str
    objection_closure_record_ids: tuple[str, ...]
    authority_snapshot: AuthoritySnapshot
    authority_snapshot_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "frame_admission_proof_id",
            "case_id",
            "frame_candidate_id",
            "frame_revision_id",
            "frame_review_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if self.candidate_generation < 1:
            raise ValueError("candidate_generation must be positive")
        for field_name in (
            "frame_content_sha256",
            "frame_review_content_sha256",
            "authority_snapshot_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        _require_string_tuple(
            self.objection_closure_record_ids,
            "objection_closure_record_ids",
        )
        if not isinstance(self.authority_snapshot, AuthoritySnapshot):
            raise TypeError(
                "authority_snapshot must be AuthoritySnapshot"
            )
        if (
            self.authority_snapshot.content_sha256
            != self.authority_snapshot_sha256
        ):
            raise ValueError("authority snapshot hash is stale or forged")
        if self.authority_snapshot.case_id != self.case_id:
            raise ValueError("authority snapshot belongs to another case")
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class JobDispositionRecord:
    job_disposition_record_id: str
    outbox_message_id: str
    case_id: str
    job_kind: AsyncJobKind
    disposition: JobDisposition
    owner_id: str
    fencing_token: int | None
    expected_authority_epoch: int
    observed_authority_epoch: int
    result_sha256: str | None
    reason_code: str
    operation: OperationIdentity
    completed_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "job_disposition_record_id",
            "outbox_message_id",
            "case_id",
            "owner_id",
            "reason_code",
        ):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.job_kind, AsyncJobKind):
            raise TypeError("job_kind must be AsyncJobKind")
        if not isinstance(self.disposition, JobDisposition):
            raise TypeError("disposition must be JobDisposition")
        if self.fencing_token is not None and self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if (
            self.disposition
            in {
                JobDisposition.COMPLETED,
                JobDisposition.TERMINAL_FAILURE,
            }
            and self.fencing_token is None
            and self.job_kind is not AsyncJobKind.CONTROLLER_WAKE
        ):
            raise ValueError(
                "worker completion or failure requires a fencing token"
            )
        if self.expected_authority_epoch < 1:
            raise ValueError("expected_authority_epoch must be positive")
        if self.observed_authority_epoch < 1:
            raise ValueError("observed_authority_epoch must be positive")
        if self.result_sha256 is not None:
            require_sha256(self.result_sha256, "result_sha256")
        if self.disposition is JobDisposition.COMPLETED:
            if self.result_sha256 is None:
                raise ValueError("completed job requires result hash")
            if (
                self.expected_authority_epoch
                != self.observed_authority_epoch
            ):
                raise ValueError(
                    "completed job cannot cross an authority epoch"
                )
        if (
            self.disposition is JobDisposition.SUPERSEDED
            and self.expected_authority_epoch
            == self.observed_authority_epoch
            and self.reason_code == "authority_epoch_changed"
        ):
            raise ValueError("superseded epoch reason requires epoch drift")
        require_aware_datetime(self.completed_at, "completed_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class DispatcherRecoveryCursor:
    """Durable high-water mark for an outbox dispatcher scan.

    The cursor records discovery progress only. Pending-job recovery must still
    query terminal disposition state so an older undisposed message is never
    hidden by a newer scan position.
    """

    dispatcher_id: str
    last_outbox_created_at: datetime | None
    last_source_event_cursor: int | None
    last_outbox_message_id: str | None
    updated_at: datetime

    def __post_init__(self) -> None:
        require_nonempty(self.dispatcher_id, "dispatcher_id")
        unset_count = sum(
            value is None
            for value in (
                self.last_outbox_created_at,
                self.last_source_event_cursor,
                self.last_outbox_message_id,
            )
        )
        if unset_count not in {0, 3}:
            raise ValueError(
                "dispatcher cursor position fields must all be set or all be null"
            )
        if self.last_outbox_created_at is not None:
            require_aware_datetime(
                self.last_outbox_created_at,
                "last_outbox_created_at",
            )
            require_nonempty(
                self.last_outbox_message_id,
                "last_outbox_message_id",
            )
            if (
                self.last_source_event_cursor is None
                or self.last_source_event_cursor < 1
            ):
                raise ValueError(
                    "last_source_event_cursor must be positive"
                )
        require_aware_datetime(self.updated_at, "updated_at")

    @property
    def position(self) -> tuple[datetime, int, str] | None:
        if self.last_outbox_created_at is None:
            return None
        if self.last_outbox_message_id is None:
            raise AssertionError("validated cursor has a complete position")
        if self.last_source_event_cursor is None:
            raise AssertionError("validated cursor has a complete position")
        return (
            self.last_outbox_created_at,
            self.last_source_event_cursor,
            self.last_outbox_message_id,
        )


@dataclass(frozen=True, slots=True)
class LogicalModelJob:
    logical_model_job_id: str
    case_id: str
    job_id: str
    operation_id: str
    role: str
    provider_ref: str
    model_ref: str
    prompt_contract_ref: str
    input_sha256: str
    configuration_identity: ModelConfigurationIdentity
    configuration_sha256: str
    model_request_artifact: ModelRequestArtifact
    model_request_artifact_sha256: str
    authority_snapshot_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "logical_model_job_id",
            "case_id",
            "job_id",
            "operation_id",
            "role",
            "provider_ref",
            "model_ref",
            "prompt_contract_ref",
        ):
            require_nonempty(getattr(self, name), name)
        require_sha256(self.input_sha256, "input_sha256")
        if not isinstance(
            self.configuration_identity,
            ModelConfigurationIdentity,
        ):
            raise TypeError(
                "configuration_identity must be ModelConfigurationIdentity"
            )
        if not isinstance(
            self.model_request_artifact,
            ModelRequestArtifact,
        ):
            raise TypeError(
                "model_request_artifact must be ModelRequestArtifact"
            )
        require_sha256(
            self.configuration_sha256,
            "configuration_sha256",
        )
        require_sha256(
            self.model_request_artifact_sha256,
            "model_request_artifact_sha256",
        )
        if (
            self.configuration_identity.configuration_sha256
            != self.configuration_sha256
        ):
            raise ValueError("logical job configuration identity drifted")
        artifact = self.model_request_artifact
        if (
            artifact.logical_model_job_id != self.logical_model_job_id
            or artifact.logical_job_kind != self.role
            or artifact.typed_request_sha256 != self.input_sha256
            or artifact.prompt_bundle_ref != self.prompt_contract_ref
            or artifact.content_sha256
            != self.model_request_artifact_sha256
        ):
            raise ValueError("logical job request artifact identity drifted")
        if (
            self.provider_ref != self.configuration_identity.provider_ref
            or self.model_ref != self.configuration_identity.model_ref
            or artifact.execution_role
            is not self.configuration_identity.execution_role
        ):
            raise ValueError("logical job provider configuration drifted")
        require_sha256(
            self.authority_snapshot_sha256,
            "authority_snapshot_sha256",
        )
        require_aware_datetime(self.created_at, "created_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class ProviderAttemptRequest:
    provider_attempt_id: str
    logical_model_job_id: str
    attempt_number: int
    prior_provider_attempt_id: str | None
    provider_idempotency_key: str
    request_sha256: str
    model_request_artifact_sha256: str
    configuration_sha256: str
    requested_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "provider_attempt_id",
            "logical_model_job_id",
            "provider_idempotency_key",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if self.attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        if self.attempt_number == 1:
            if self.prior_provider_attempt_id is not None:
                raise ValueError("first attempt cannot have a prior attempt")
        elif self.prior_provider_attempt_id is None:
            raise ValueError("later attempt requires a prior attempt")
        require_sha256(self.request_sha256, "request_sha256")
        require_sha256(
            self.model_request_artifact_sha256,
            "model_request_artifact_sha256",
        )
        require_sha256(
            self.configuration_sha256,
            "configuration_sha256",
        )
        require_aware_datetime(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class ProviderAttemptReceipt:
    provider_attempt_receipt_id: str
    provider_attempt_id: str
    logical_model_job_id: str
    disposition: ProviderAttemptDisposition
    provider_response_id: str | None
    output_sha256: str | None
    finish_reason: str | None
    usage_payload: Mapping[str, FrozenJson]
    completed_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "provider_attempt_receipt_id",
            "provider_attempt_id",
            "logical_model_job_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if not isinstance(self.disposition, ProviderAttemptDisposition):
            raise TypeError(
                "disposition must be ProviderAttemptDisposition"
            )
        for field_name in ("provider_response_id", "finish_reason"):
            value = getattr(self, field_name)
            if value is not None:
                require_nonempty(value, field_name)
        if self.output_sha256 is not None:
            require_sha256(self.output_sha256, "output_sha256")
        if (
            self.disposition is ProviderAttemptDisposition.SUCCEEDED
            and (
                self.output_sha256 is None
                or self.provider_response_id is None
            )
        ):
            raise ValueError(
                "successful provider attempt requires response identity and output"
            )
        frozen = _freeze_object(self.usage_payload, "usage_payload")
        object.__setattr__(self, "usage_payload", frozen)
        require_aware_datetime(self.completed_at, "completed_at")


@dataclass(frozen=True, slots=True)
class DurableModelResult:
    durable_model_result_id: str
    logical_model_job_id: str
    provider_attempt_id: str
    provider_attempt_receipt_id: str
    result_kind: str
    result_contract_ref: str
    result_payload: Mapping[str, FrozenJson]
    output_sha256: str
    model_request_artifact_sha256: str
    configuration_sha256: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "durable_model_result_id",
            "logical_model_job_id",
            "provider_attempt_id",
            "provider_attempt_receipt_id",
            "result_kind",
            "result_contract_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        frozen = _freeze_object(
            self.result_payload,
            "result_payload",
        )
        object.__setattr__(self, "result_payload", frozen)
        require_sha256(self.output_sha256, "output_sha256")
        require_sha256(
            self.model_request_artifact_sha256,
            "model_request_artifact_sha256",
        )
        require_sha256(
            self.configuration_sha256,
            "configuration_sha256",
        )
        if content_sha256(self.result_payload) != self.output_sha256:
            raise ValueError("durable model result hash is stale")
        require_aware_datetime(self.recorded_at, "recorded_at")


@dataclass(frozen=True, slots=True)
class RunTraceEventLink:
    cursor: int
    event_id: str
    event_type: JournalEventType
    recorded_at: datetime
    operation_id: str
    causation_id: str
    correlation_id: str
    authority_revision: int
    action_id: str | None
    authority_ref: str | None
    payload_sha256: str
    event_content_sha256: str

    def __post_init__(self) -> None:
        if self.cursor < 1:
            raise ValueError("run trace event cursor must be positive")
        for field_name in (
            "event_id",
            "operation_id",
            "causation_id",
            "correlation_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if not isinstance(self.event_type, JournalEventType):
            raise TypeError("run trace event_type must be JournalEventType")
        require_aware_datetime(self.recorded_at, "recorded_at")
        for field_name in ("action_id", "authority_ref"):
            value = getattr(self, field_name)
            if value is not None:
                require_nonempty(value, field_name)
        if self.authority_revision < 0:
            raise ValueError(
                "run trace authority revision cannot be negative"
            )
        require_sha256(self.payload_sha256, "payload_sha256")
        require_sha256(
            self.event_content_sha256,
            "event_content_sha256",
        )


@dataclass(frozen=True, slots=True)
class RunTraceManifest:
    trace_manifest_id: str
    case_id: str
    run_id: str
    trace_profile: RunTraceProfile
    start_event_cursor: int
    terminal_event_cursor: int
    event_operation_lineage: tuple[RunTraceEventLink, ...]
    ingress_record_ids: tuple[str, ...]
    message_binding_ids: tuple[str, ...]
    frame_candidate_ids: tuple[str, ...]
    frame_candidate_supersession_ids: tuple[str, ...]
    frame_review_ids: tuple[str, ...]
    job_disposition_record_ids: tuple[str, ...]
    logical_model_job_ids: tuple[str, ...]
    provider_attempt_request_ids: tuple[str, ...]
    provider_attempt_receipt_ids: tuple[str, ...]
    durable_model_result_ids: tuple[str, ...]
    plan_revision_ids: tuple[str, ...]
    resolution_outcome_ids: tuple[str, ...]
    obligation_ids: tuple[str, ...]
    effect_attempt_ids: tuple[str, ...]
    evidence_record_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    provisional_answer_version_ids: tuple[str, ...]
    lineage_sha256: str
    built_at: datetime

    def __post_init__(self) -> None:
        for name in ("trace_manifest_id", "case_id", "run_id"):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.trace_profile, RunTraceProfile):
            raise TypeError("trace_profile must be RunTraceProfile")
        if (
            self.start_event_cursor < 1
            or self.terminal_event_cursor < self.start_event_cursor
        ):
            raise ValueError("run trace event cursor bounds are invalid")
        if (
            not isinstance(self.event_operation_lineage, tuple)
            or not self.event_operation_lineage
            or any(
                not isinstance(item, RunTraceEventLink)
                for item in self.event_operation_lineage
            )
        ):
            raise TypeError(
                "event_operation_lineage must contain RunTraceEventLink"
            )
        cursors = tuple(
            item.cursor for item in self.event_operation_lineage
        )
        if cursors != tuple(
            range(
                self.start_event_cursor,
                self.terminal_event_cursor + 1,
            )
        ):
            raise ValueError(
                "run trace event lineage must be complete and contiguous"
            )
        if any(
            item.correlation_id != self.run_id
            for item in self.event_operation_lineage
        ):
            raise ValueError(
                "run trace event lineage crosses correlation IDs"
            )
        for field_name in (
            "ingress_record_ids",
            "message_binding_ids",
            "frame_candidate_ids",
            "frame_candidate_supersession_ids",
            "frame_review_ids",
            "job_disposition_record_ids",
            "logical_model_job_ids",
            "provider_attempt_request_ids",
            "provider_attempt_receipt_ids",
            "durable_model_result_ids",
            "plan_revision_ids",
            "resolution_outcome_ids",
            "obligation_ids",
            "effect_attempt_ids",
            "evidence_record_ids",
            "claim_ids",
            "provisional_answer_version_ids",
        ):
            _require_string_tuple(getattr(self, field_name), field_name)
        if not self.ingress_record_ids:
            raise ValueError("trace manifest requires ingress lineage")
        require_sha256(self.lineage_sha256, "lineage_sha256")
        if self.lineage_sha256 != compute_run_trace_lineage_sha256(self):
            raise ValueError("run trace lineage hash is stale or forged")
        require_aware_datetime(self.built_at, "built_at")
        if any(
            event.recorded_at > self.built_at
            for event in self.event_operation_lineage
        ):
            raise ValueError("run trace event postdates manifest build")


def run_trace_lineage_material(
    record: RunTraceManifest,
) -> Mapping[str, object]:
    return {
        "case_id": record.case_id,
        "run_id": record.run_id,
        "trace_profile": record.trace_profile.value,
        "start_event_cursor": record.start_event_cursor,
        "terminal_event_cursor": record.terminal_event_cursor,
        "event_operation_lineage": record.event_operation_lineage,
        "ingress_record_ids": record.ingress_record_ids,
        "message_binding_ids": record.message_binding_ids,
        "frame_candidate_ids": record.frame_candidate_ids,
        "frame_candidate_supersession_ids": (
            record.frame_candidate_supersession_ids
        ),
        "frame_review_ids": record.frame_review_ids,
        "job_disposition_record_ids": (
            record.job_disposition_record_ids
        ),
        "logical_model_job_ids": record.logical_model_job_ids,
        "provider_attempt_request_ids": (
            record.provider_attempt_request_ids
        ),
        "provider_attempt_receipt_ids": (
            record.provider_attempt_receipt_ids
        ),
        "durable_model_result_ids": record.durable_model_result_ids,
        "plan_revision_ids": record.plan_revision_ids,
        "resolution_outcome_ids": record.resolution_outcome_ids,
        "obligation_ids": record.obligation_ids,
        "effect_attempt_ids": record.effect_attempt_ids,
        "evidence_record_ids": record.evidence_record_ids,
        "claim_ids": record.claim_ids,
        "provisional_answer_version_ids": (
            record.provisional_answer_version_ids
        ),
    }


def compute_run_trace_lineage_sha256(
    record: RunTraceManifest,
) -> str:
    return content_sha256(run_trace_lineage_material(record))


def _model_configuration_content(
    identity: ModelConfigurationIdentity,
) -> Mapping[str, object]:
    return {
        "execution_role": identity.execution_role.value,
        **_model_configuration_operational_content(identity),
    }


def _model_configuration_operational_content(
    identity: ModelConfigurationIdentity,
) -> Mapping[str, object]:
    return {
        "provider_ref": identity.provider_ref,
        "endpoint_ref": identity.endpoint_ref,
        "protocol_ref": identity.protocol_ref,
        "adapter_release_ref": identity.adapter_release_ref,
        "adapter_release_sha256": identity.adapter_release_sha256,
        "model_ref": identity.model_ref,
        "thinking": identity.thinking,
        "stable_parameters": identity.stable_parameters,
        "delivery_policy_ref": identity.delivery_policy_ref,
        "max_attempts": identity.max_attempts,
        "timeout_seconds": identity.timeout_seconds,
    }


def _freeze_object(
    value: Mapping[str, FrozenJson],
    field_name: str,
) -> Mapping[str, FrozenJson]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{field_name} must be a JSON object")
    return frozen


def _require_string_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field_name} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must be unique")
