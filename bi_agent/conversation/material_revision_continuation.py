from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.single_authority import InteractionDirective


MATERIAL_REVISION_CONTINUATION_SCHEMA_VERSION = "material-revision-continuation.v1"
MATERIAL_REVISION_PLAN_FIELDS = frozenset(
    {
        "goal_bindings",
        "desired_decisions",
        "analysis_axes",
        "target_metric_refs",
        "baseline_refs",
        "resolved_window_refs",
        "time_spec",
        "scope",
        "filters",
        "direction_premise",
    }
)


class MaterialRevisionContinuationError(ValueError):
    pass


@dataclass(frozen=True)
class MaterialRevisionContinuation:
    schema_version: str
    directive_id: str
    source_run_id: str
    source_intent_revision_id: str
    thread_id: str
    successor_user_text: str
    superseded_plan_fields: tuple[str, ...]
    parent_transition_id: str
    producer_kind: str
    scope_ref: str
    request_identity: str
    successor_run_id: str
    successor_message_id: str
    successor_dispatch_id: str
    continuation_ref: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        directive: InteractionDirective,
        thread_id: str,
        successor_user_text: str,
        superseded_plan_fields: Sequence[str],
        parent_transition_id: str,
    ) -> "MaterialRevisionContinuation":
        if type(directive) is not InteractionDirective:
            raise MaterialRevisionContinuationError(
                "material_revision_directive_invalid"
            )
        try:
            rebuilt_directive = InteractionDirective.from_dict(directive.to_dict())
        except ValueError as exc:
            raise MaterialRevisionContinuationError(
                "material_revision_directive_invalid"
            ) from exc
        if rebuilt_directive != directive:
            raise MaterialRevisionContinuationError(
                "material_revision_directive_invalid"
            )
        if directive.kind != "material_intent_change":
            raise MaterialRevisionContinuationError(
                "material_revision_directive_kind_invalid"
            )
        if directive.target_refs != (directive.intent_revision_id,):
            raise MaterialRevisionContinuationError(
                "material_revision_directive_target_invalid"
            )
        thread_id = _exact_string(
            thread_id,
            "material_revision_thread_id_invalid",
        )
        successor_user_text = _exact_string(
            successor_user_text,
            "material_revision_successor_text_invalid",
        )
        parent_transition_id = _exact_string(
            parent_transition_id,
            "material_revision_parent_transition_invalid",
        )
        normalized_plan_fields = _plan_fields(superseded_plan_fields)
        identity_digest = _identity_digest(directive.directive_id)
        request_identity = f"material-revision:{directive.directive_id}"
        body = {
            "schema_version": MATERIAL_REVISION_CONTINUATION_SCHEMA_VERSION,
            "directive_id": directive.directive_id,
            "source_run_id": directive.run_attempt_id,
            "source_intent_revision_id": directive.intent_revision_id,
            "thread_id": thread_id,
            "successor_user_text": successor_user_text,
            "superseded_plan_fields": normalized_plan_fields,
            "parent_transition_id": parent_transition_id,
            "producer_kind": "thread_message",
            "scope_ref": thread_id,
            "request_identity": request_identity,
            "successor_run_id": f"run-{identity_digest[:24]}",
            "successor_message_id": f"message-{identity_digest[:24]}",
            "successor_dispatch_id": f"dispatch-{identity_digest[:24]}",
            "continuation_ref": (
                "material-revision-continuation:sha256:" + identity_digest
            ),
        }
        provisional = cls(content_digest="", **body)
        return cls(
            content_digest=canonical_digest(provisional._content_body()),
            **body,
        ).validate()

    @property
    def request_payload(self) -> dict[str, Any]:
        return canonical_value(
            {
                "message": self.successor_user_text,
                "intentRevisionContext": {
                    "supersedes_intent_revision_id": (self.source_intent_revision_id),
                    "superseded_plan_fields": list(self.superseded_plan_fields),
                    "intent_revision_reason_ref": self.directive_id,
                    "parent_transition_id": self.parent_transition_id,
                },
            }
        )

    @property
    def request_digest(self) -> str:
        return canonical_digest(
            {
                "producer_kind": self.producer_kind,
                "scope_ref": self.scope_ref,
                "thread_id": self.thread_id,
                "request_payload": self.request_payload,
            }
        )

    @property
    def source_link_payload(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "directive_id": self.directive_id,
            "source_run_id": self.source_run_id,
            "successor_run_id": self.successor_run_id,
            "successor_dispatch_id": self.successor_dispatch_id,
            "continuation_ref": self.continuation_ref,
        }

    def validate(self) -> "MaterialRevisionContinuation":
        identity_digest = _identity_digest(self.directive_id)
        expected_identity = {
            "schema_version": MATERIAL_REVISION_CONTINUATION_SCHEMA_VERSION,
            "producer_kind": "thread_message",
            "scope_ref": self.thread_id,
            "request_identity": f"material-revision:{self.directive_id}",
            "successor_run_id": f"run-{identity_digest[:24]}",
            "successor_message_id": f"message-{identity_digest[:24]}",
            "successor_dispatch_id": f"dispatch-{identity_digest[:24]}",
            "continuation_ref": (
                "material-revision-continuation:sha256:" + identity_digest
            ),
        }
        actual_identity = {field: getattr(self, field) for field in expected_identity}
        if actual_identity != expected_identity:
            raise MaterialRevisionContinuationError(
                "material_revision_continuation_identity_invalid"
            )
        for value, error in (
            (self.source_run_id, "material_revision_source_run_invalid"),
            (
                self.source_intent_revision_id,
                "material_revision_source_intent_invalid",
            ),
            (self.thread_id, "material_revision_thread_id_invalid"),
            (
                self.successor_user_text,
                "material_revision_successor_text_invalid",
            ),
            (
                self.parent_transition_id,
                "material_revision_parent_transition_invalid",
            ),
        ):
            _exact_string(value, error)
        normalized_plan_fields = _plan_fields(self.superseded_plan_fields)
        if normalized_plan_fields != self.superseded_plan_fields:
            raise MaterialRevisionContinuationError(
                "material_revision_plan_fields_invalid"
            )
        if canonical_digest(self._content_body()) != self.content_digest:
            raise MaterialRevisionContinuationError(
                "material_revision_continuation_digest_invalid"
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(
            {
                **asdict(self),
                "request_payload": self.request_payload,
                "request_digest": self.request_digest,
            }
        )

    def _content_body(self) -> dict[str, Any]:
        body = asdict(self)
        body.pop("content_digest")
        return {
            **body,
            "request_payload": self.request_payload,
            "request_digest": self.request_digest,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "MaterialRevisionContinuation":
        expected_fields = {
            *cls.__dataclass_fields__,
            "request_payload",
            "request_digest",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected_fields:
            raise MaterialRevisionContinuationError(
                "material_revision_continuation_shape_invalid"
            )
        raw_fields = payload.get("superseded_plan_fields")
        if isinstance(raw_fields, (str, bytes)) or not isinstance(
            raw_fields,
            Sequence,
        ):
            raise MaterialRevisionContinuationError(
                "material_revision_plan_fields_invalid"
            )
        raw_payload = payload.get("request_payload")
        if not isinstance(raw_payload, Mapping):
            raise MaterialRevisionContinuationError(
                "material_revision_request_payload_invalid"
            )
        stored_payload = canonical_value(raw_payload)
        stored_request_digest = payload.get("request_digest")
        constructor = {field: payload[field] for field in cls.__dataclass_fields__}
        continuation = cls(
            **{
                **constructor,
                "superseded_plan_fields": tuple(raw_fields),
            }
        ).validate()
        if stored_payload != continuation.request_payload:
            raise MaterialRevisionContinuationError(
                "material_revision_request_payload_invalid"
            )
        if stored_request_digest != continuation.request_digest:
            raise MaterialRevisionContinuationError(
                "material_revision_request_digest_invalid"
            )
        return continuation


def _identity_digest(directive_id: str) -> str:
    directive_id = _exact_string(
        directive_id,
        "material_revision_directive_id_invalid",
    )
    return sha256(
        (MATERIAL_REVISION_CONTINUATION_SCHEMA_VERSION + "\x00" + directive_id).encode(
            "utf-8"
        )
    ).hexdigest()


def _plan_fields(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MaterialRevisionContinuationError("material_revision_plan_fields_invalid")
    normalized = tuple(values)
    if (
        not normalized
        or any(
            not isinstance(value, str) or value not in MATERIAL_REVISION_PLAN_FIELDS
            for value in normalized
        )
        or len(set(normalized)) != len(normalized)
    ):
        raise MaterialRevisionContinuationError("material_revision_plan_fields_invalid")
    return normalized


def _exact_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MaterialRevisionContinuationError(error)
    return value
