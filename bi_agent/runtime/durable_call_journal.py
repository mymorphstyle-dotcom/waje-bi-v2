from __future__ import annotations

from dataclasses import dataclass
import json
from threading import Condition, RLock
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.llm_client import (
    LLMOutputError,
    llm_failure_code,
    llm_failure_is_retryable,
)


CALL_SCOPE_REQUIREMENTS = {
    "conversation_provider": (False, False, False),
    "topic_selection": (False, False, False),
    "intent_provider": (False, False, False),
    "clarification_provider": (True, False, False),
    "planner_provider": (True, False, False),
    "plan_patch_provider": (True, True, False),
    "query": (True, True, True),
    "capability": (True, True, True),
    "semantic_provider": (True, True, False),
    "narrative_provider": (True, True, False),
}
CALL_KINDS = frozenset(CALL_SCOPE_REQUIREMENTS)
PROVIDER_CALL_KINDS = frozenset(
    {
        "conversation_provider",
        "intent_provider",
        "clarification_provider",
        "planner_provider",
        "plan_patch_provider",
        "semantic_provider",
        "narrative_provider",
    }
)
ATTEMPT_EVENT_STATUSES = frozenset({"claimed", "started", "succeeded", "failed"})
SUCCESS_DISPOSITIONS = frozenset({"accepted", "orphaned"})
RETRY_REASONS = frozenset(
    {"initial", "previous_attempt_incomplete", "previous_attempt_failed"}
)


class DurableCallJournalError(ValueError):
    pass


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DurableCallJournalError(error)
    return value.strip()


def _tagged_scope_ref(
    value: Any,
    *,
    required: bool,
    error: str,
) -> str | None:
    if required:
        return _required_string(value, error)
    if value is not None:
        raise DurableCallJournalError(error)
    return None


def _digest(value: Any, error: str) -> str:
    value = _required_string(value, error)
    if len(value) != 64:
        raise DurableCallJournalError(error)
    return value


def _positive_integer(value: Any, error: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DurableCallJournalError(error)
    return value


def _mapping(value: Any, error: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DurableCallJournalError(error)
    normalized = canonical_value(dict(value))
    if not isinstance(normalized, dict):
        raise DurableCallJournalError(error)
    return normalized


@dataclass(frozen=True)
class DurableCallSpec:
    spec_ref: str
    run_attempt_id: str
    intent_revision_id: str | None
    plan_revision_id: str | None
    task_id: str | None
    stage_name: str
    call_kind: str
    operation_name: str
    input_ref: str
    input_digest: str
    input_payload: Mapping[str, Any]
    idempotency_key: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_attempt_id: str,
        intent_revision_id: str | None,
        plan_revision_id: str | None,
        task_id: str | None,
        stage_name: str,
        call_kind: str,
        operation_name: str,
        input_ref: str,
        input_payload: Mapping[str, Any],
    ) -> "DurableCallSpec":
        if call_kind not in CALL_KINDS:
            raise DurableCallJournalError("call_spec_kind_invalid")
        requires_intent, requires_plan, requires_task = CALL_SCOPE_REQUIREMENTS[
            call_kind
        ]
        normalized_intent = _tagged_scope_ref(
            intent_revision_id,
            required=requires_intent,
            error="call_spec_intent_revision_id_invalid",
        )
        normalized_plan = _tagged_scope_ref(
            plan_revision_id,
            required=requires_plan,
            error="call_spec_plan_revision_id_invalid",
        )
        normalized_task = _tagged_scope_ref(
            task_id,
            required=requires_task,
            error="call_spec_task_id_invalid",
        )
        normalized_input = _mapping(input_payload, "call_spec_input_payload_invalid")
        input_digest = canonical_digest(normalized_input)
        body = {
            "run_attempt_id": _required_string(
                run_attempt_id, "call_spec_run_attempt_id_invalid"
            ),
            "intent_revision_id": normalized_intent,
            "plan_revision_id": normalized_plan,
            "task_id": normalized_task,
            "stage_name": _required_string(stage_name, "call_spec_stage_name_invalid"),
            "call_kind": call_kind,
            "operation_name": _required_string(
                operation_name, "call_spec_operation_name_invalid"
            ),
            "input_ref": _required_string(input_ref, "call_spec_input_ref_invalid"),
            "input_digest": input_digest,
            "input_payload": normalized_input,
        }
        idempotency_key = canonical_digest(
            {
                key: body[key]
                for key in (
                    "run_attempt_id",
                    "intent_revision_id",
                    "plan_revision_id",
                    "task_id",
                    "stage_name",
                    "call_kind",
                    "operation_name",
                    "input_ref",
                    "input_digest",
                )
            }
        )
        body["idempotency_key"] = idempotency_key
        digest = canonical_digest(body)
        return cls(
            spec_ref="durable-call-spec:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DurableCallSpec":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise DurableCallJournalError("call_spec_shape_invalid")
        rebuilt = cls.create(
            run_attempt_id=payload["run_attempt_id"],
            intent_revision_id=payload["intent_revision_id"],
            plan_revision_id=payload["plan_revision_id"],
            task_id=payload["task_id"],
            stage_name=payload["stage_name"],
            call_kind=payload["call_kind"],
            operation_name=payload["operation_name"],
            input_ref=payload["input_ref"],
            input_payload=payload["input_payload"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise DurableCallJournalError("call_spec_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class DurableCallAttempt:
    attempt_ref: str
    spec: DurableCallSpec
    attempt_number: int
    retry_reason: str
    content_digest: str

    @classmethod
    def create(
        cls,
        spec: DurableCallSpec,
        *,
        attempt_number: int,
        retry_reason: str,
    ) -> "DurableCallAttempt":
        if type(spec) is not DurableCallSpec:
            raise DurableCallJournalError("call_attempt_spec_invalid")
        spec = DurableCallSpec.from_dict(spec.to_dict())
        number = _positive_integer(attempt_number, "call_attempt_number_invalid")
        if (
            retry_reason not in RETRY_REASONS
            or (number == 1 and retry_reason != "initial")
            or (number > 1 and retry_reason == "initial")
        ):
            raise DurableCallJournalError("call_attempt_retry_reason_invalid")
        body = {
            "spec": spec.to_dict(),
            "attempt_number": number,
            "retry_reason": retry_reason,
        }
        if spec.call_kind == "capability":
            attempt_ref = (
                "capability-attempt-"
                + canonical_digest(
                    {
                        "input_digest": spec.input_digest,
                        "execution_attempt": number,
                    }
                )[:24]
            )
        elif spec.call_kind == "topic_selection":
            attempt_ref = "control-call-attempt:sha256:" + canonical_digest(
                {
                    "idempotency_key": spec.idempotency_key,
                    "attempt_number": number,
                }
            )
        else:
            attempt_ref = "provider-call-attempt:sha256:" + canonical_digest(
                {
                    "idempotency_key": spec.idempotency_key,
                    "attempt_number": number,
                }
            )
        return cls(
            attempt_ref=attempt_ref,
            spec=spec,
            attempt_number=number,
            retry_reason=retry_reason,
            content_digest=canonical_digest(body),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DurableCallAttempt":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise DurableCallJournalError("call_attempt_shape_invalid")
        rebuilt = cls.create(
            DurableCallSpec.from_dict(payload["spec"]),
            attempt_number=payload["attempt_number"],
            retry_reason=payload["retry_reason"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise DurableCallJournalError("call_attempt_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class DurableCallAttemptEvent:
    event_ref: str
    attempt_ref: str
    event_sequence: int
    status: str
    success_disposition: str | None
    output_digest: str | None
    output_payload: Mapping[str, Any] | None
    failure_code: str | None
    failure_payload: Mapping[str, Any] | None
    content_digest: str

    @classmethod
    def create(
        cls,
        attempt: DurableCallAttempt,
        *,
        status: str,
        success_disposition: str | None = None,
        output_payload: Mapping[str, Any] | None = None,
        failure_code: str | None = None,
        failure_payload: Mapping[str, Any] | None = None,
    ) -> "DurableCallAttemptEvent":
        if type(attempt) is not DurableCallAttempt:
            raise DurableCallJournalError("call_event_attempt_invalid")
        if status not in ATTEMPT_EVENT_STATUSES:
            raise DurableCallJournalError("call_event_status_invalid")
        sequence = {
            "claimed": 1,
            "started": 2,
            "succeeded": 3,
            "failed": 3,
        }[status]
        normalized_output = (
            None
            if output_payload is None
            else _mapping(output_payload, "call_event_output_invalid")
        )
        normalized_failure = (
            None
            if failure_payload is None
            else _mapping(failure_payload, "call_event_failure_payload_invalid")
        )
        normalized_failure_code = (
            None
            if failure_code is None
            else _required_string(failure_code, "call_event_failure_code_invalid")
        )
        if status == "succeeded":
            if (
                success_disposition not in SUCCESS_DISPOSITIONS
                or normalized_output is None
                or normalized_failure_code is not None
            ):
                raise DurableCallJournalError("call_event_terminal_shape_invalid")
        elif status == "failed":
            if (
                success_disposition is not None
                or normalized_output is not None
                or normalized_failure_code is None
            ):
                raise DurableCallJournalError("call_event_terminal_shape_invalid")
        elif success_disposition is not None or any(
            value is not None
            for value in (
                normalized_output,
                normalized_failure_code,
                normalized_failure,
            )
        ):
            raise DurableCallJournalError("call_event_nonterminal_shape_invalid")
        output_digest = (
            None if normalized_output is None else canonical_digest(normalized_output)
        )
        body = {
            "attempt_ref": attempt.attempt_ref,
            "event_sequence": sequence,
            "status": status,
            "success_disposition": success_disposition,
            "output_digest": output_digest,
            "output_payload": normalized_output,
            "failure_code": normalized_failure_code,
            "failure_payload": normalized_failure,
        }
        digest = canonical_digest(body)
        return cls(
            event_ref="durable-call-event:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DurableCallAttemptEvent":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise DurableCallJournalError("call_event_shape_invalid")
        body = {
            key: canonical_value(value)
            for key, value in payload.items()
            if key not in {"event_ref", "content_digest"}
        }
        digest = canonical_digest(body)
        if (
            payload["content_digest"] != digest
            or payload["event_ref"] != "durable-call-event:sha256:" + digest
            or payload["status"] not in ATTEMPT_EVENT_STATUSES
            or payload["event_sequence"]
            != {"claimed": 1, "started": 2, "succeeded": 3, "failed": 3}[
                payload["status"]
            ]
        ):
            raise DurableCallJournalError("call_event_integrity_invalid")
        if payload["status"] == "succeeded":
            if (
                payload["success_disposition"] not in SUCCESS_DISPOSITIONS
                or not isinstance(payload["output_payload"], Mapping)
                or payload["output_digest"]
                != canonical_digest(payload["output_payload"])
                or payload["failure_code"] is not None
            ):
                raise DurableCallJournalError("call_event_integrity_invalid")
        elif payload["status"] == "failed":
            if (
                payload["success_disposition"] is not None
                or payload["output_payload"] is not None
                or not isinstance(payload["failure_code"], str)
            ):
                raise DurableCallJournalError("call_event_integrity_invalid")
        elif payload["success_disposition"] is not None or any(
            payload[key] is not None
            for key in (
                "output_digest",
                "output_payload",
                "failure_code",
                "failure_payload",
            )
        ):
            raise DurableCallJournalError("call_event_integrity_invalid")
        return cls(**canonical_value(payload))

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class DurableCallAcceptance:
    acceptance_ref: str
    run_attempt_id: str
    idempotency_key: str
    accepted_attempt_ref: str
    output_digest: str
    output_payload: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        attempt: DurableCallAttempt,
        succeeded: DurableCallAttemptEvent,
    ) -> "DurableCallAcceptance":
        if (
            type(attempt) is not DurableCallAttempt
            or type(succeeded) is not DurableCallAttemptEvent
            or succeeded.attempt_ref != attempt.attempt_ref
            or succeeded.status != "succeeded"
            or succeeded.success_disposition != "accepted"
            or succeeded.output_digest is None
            or succeeded.output_payload is None
        ):
            raise DurableCallJournalError("call_acceptance_input_invalid")
        body = {
            "run_attempt_id": attempt.spec.run_attempt_id,
            "idempotency_key": attempt.spec.idempotency_key,
            "accepted_attempt_ref": attempt.attempt_ref,
            "output_digest": succeeded.output_digest,
            "output_payload": canonical_value(succeeded.output_payload),
        }
        digest = canonical_digest(body)
        return cls(
            acceptance_ref="durable-call-acceptance:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DurableCallAcceptance":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise DurableCallJournalError("call_acceptance_shape_invalid")
        body = {
            key: canonical_value(value)
            for key, value in payload.items()
            if key not in {"acceptance_ref", "content_digest"}
        }
        digest = canonical_digest(body)
        if (
            payload["content_digest"] != digest
            or payload["acceptance_ref"] != "durable-call-acceptance:sha256:" + digest
            or payload["output_digest"] != canonical_digest(payload["output_payload"])
        ):
            raise DurableCallJournalError("call_acceptance_integrity_invalid")
        return cls(**canonical_value(payload))

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)


@dataclass(frozen=True)
class DurableCallClaim:
    attempt: DurableCallAttempt
    replayed: bool
    acceptance: DurableCallAcceptance | None
    output_payload: Mapping[str, Any] | None


@dataclass(frozen=True)
class DurableCallCompletion:
    attempt: DurableCallAttempt
    accepted_attempt: DurableCallAttempt | None
    terminal_event: DurableCallAttemptEvent
    disposition: str
    acceptance: DurableCallAcceptance | None
    output_payload: Mapping[str, Any]


@dataclass(frozen=True)
class DurableAcceptedCallClosure:
    attempt: DurableCallAttempt
    acceptance: DurableCallAcceptance
    terminal_event: DurableCallAttemptEvent


def _validated_accepted_call_closure(
    *,
    spec: DurableCallSpec,
    attempt_ref: str,
    attempt: DurableCallAttempt,
    acceptance: DurableCallAcceptance,
    events: Sequence[DurableCallAttemptEvent],
) -> DurableAcceptedCallClosure:
    if (
        type(attempt) is not DurableCallAttempt
        or type(acceptance) is not DurableCallAcceptance
        or attempt.spec != spec
        or attempt.attempt_ref != attempt_ref
        or acceptance.run_attempt_id != spec.run_attempt_id
        or acceptance.idempotency_key != spec.idempotency_key
        or acceptance.accepted_attempt_ref != attempt_ref
        or len(events) != 3
        or tuple(event.status for event in events)
        != ("claimed", "started", "succeeded")
        or tuple(event.event_sequence for event in events) != (1, 2, 3)
        or any(event.attempt_ref != attempt_ref for event in events)
    ):
        raise DurableCallJournalError("accepted_call_integrity_invalid")
    terminal = events[-1]
    if (
        terminal.success_disposition != "accepted"
        or terminal.output_payload is None
        or terminal.output_digest != acceptance.output_digest
        or canonical_value(terminal.output_payload)
        != canonical_value(acceptance.output_payload)
    ):
        raise DurableCallJournalError("accepted_call_integrity_invalid")
    return DurableAcceptedCallClosure(
        attempt=attempt,
        acceptance=acceptance,
        terminal_event=terminal,
    )


@runtime_checkable
class DurableCallJournal(Protocol):
    def claim(self, spec: DurableCallSpec) -> DurableCallClaim: ...

    def succeed(
        self,
        attempt: DurableCallAttempt,
        output_payload: Mapping[str, Any],
    ) -> DurableCallCompletion: ...

    def fail(
        self,
        attempt: DurableCallAttempt,
        *,
        failure_code: str,
        failure_payload: Mapping[str, Any] | None = None,
    ) -> DurableCallAttemptEvent: ...

    def load_accepted_call(
        self,
        *,
        call_spec: DurableCallSpec,
        accepted_attempt_ref: str,
    ) -> DurableAcceptedCallClosure: ...

    def bind_stage(
        self,
        *,
        run_attempt_id: str,
        transition_attempt_id: str,
        stage_name: str,
        attempt_refs: Sequence[str],
        commit: bool = True,
    ) -> tuple[str, ...]: ...

    def load_stage_attempt_refs(
        self,
        *,
        run_attempt_id: str,
        transition_attempt_id: str,
        stage_name: str,
    ) -> tuple[str, ...]: ...


class InMemoryDurableCallJournal:
    def __init__(
        self,
        *,
        active_scope_validator: Callable[[DurableCallSpec], bool] | None = None,
    ) -> None:
        if active_scope_validator is not None and not callable(active_scope_validator):
            raise DurableCallJournalError("active_scope_validator_invalid")
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._specs: dict[str, DurableCallSpec] = {}
        self._attempts: dict[str, DurableCallAttempt] = {}
        self._attempt_refs_by_idempotency: dict[str, list[str]] = {}
        self._events: dict[str, list[DurableCallAttemptEvent]] = {}
        self._acceptances: dict[str, DurableCallAcceptance] = {}
        self._stage_bindings: dict[tuple[str, str, str], tuple[str, ...]] = {}
        self._active_attempt_by_idempotency: dict[str, str] = {}
        self._active_scope_validator = active_scope_validator

    def claim(self, spec: DurableCallSpec) -> DurableCallClaim:
        if type(spec) is not DurableCallSpec:
            raise DurableCallJournalError("call_claim_spec_invalid")
        spec = DurableCallSpec.from_dict(spec.to_dict())
        with self._condition:
            self._validate_active_scope(spec)
            stored_spec = self._specs.setdefault(spec.idempotency_key, spec)
            if stored_spec != spec:
                raise DurableCallJournalError("call_spec_idempotency_conflict")
            while spec.idempotency_key in self._active_attempt_by_idempotency:
                self._condition.wait()
            self._validate_active_scope(spec)
            acceptance = self._acceptances.get(spec.idempotency_key)
            if acceptance is not None:
                return DurableCallClaim(
                    attempt=self._attempts[acceptance.accepted_attempt_ref],
                    replayed=True,
                    acceptance=acceptance,
                    output_payload=canonical_value(acceptance.output_payload),
                )
            refs = self._attempt_refs_by_idempotency.setdefault(
                spec.idempotency_key, []
            )
            attempt_number = len(refs) + 1
            if not refs:
                retry_reason = "initial"
            else:
                last_events = self._events[refs[-1]]
                if (
                    last_events[-1].status == "succeeded"
                    and last_events[-1].success_disposition == "orphaned"
                ):
                    raise DurableCallJournalError("call_success_orphaned")
                retry_reason = (
                    "previous_attempt_failed"
                    if last_events[-1].status == "failed"
                    else "previous_attempt_incomplete"
                )
            attempt = DurableCallAttempt.create(
                spec,
                attempt_number=attempt_number,
                retry_reason=retry_reason,
            )
            if attempt.attempt_ref in self._attempts:
                raise DurableCallJournalError("call_attempt_identity_conflict")
            refs.append(attempt.attempt_ref)
            self._attempts[attempt.attempt_ref] = attempt
            self._events[attempt.attempt_ref] = [
                DurableCallAttemptEvent.create(attempt, status="claimed"),
                DurableCallAttemptEvent.create(attempt, status="started"),
            ]
            self._active_attempt_by_idempotency[spec.idempotency_key] = (
                attempt.attempt_ref
            )
            return DurableCallClaim(
                attempt=attempt,
                replayed=False,
                acceptance=None,
                output_payload=None,
            )

    def succeed(
        self,
        attempt: DurableCallAttempt,
        output_payload: Mapping[str, Any],
    ) -> DurableCallCompletion:
        with self._condition:
            stored = self._stored_attempt(attempt)
            try:
                events = self._events[stored.attempt_ref]
                terminal = tuple(
                    event for event in events if event.status in {"succeeded", "failed"}
                )
                if terminal:
                    if terminal[0].status != "succeeded" or canonical_value(
                        terminal[0].output_payload
                    ) != canonical_value(output_payload):
                        raise DurableCallJournalError("attempt_terminal_conflict")
                    success = terminal[0]
                else:
                    disposition = (
                        "accepted" if self._scope_is_active(stored.spec) else "orphaned"
                    )
                    success = DurableCallAttemptEvent.create(
                        stored,
                        status="succeeded",
                        success_disposition=disposition,
                        output_payload=output_payload,
                    )
                    events.append(success)
                if success.success_disposition == "accepted":
                    candidate = DurableCallAcceptance.create(stored, success)
                    acceptance = self._acceptances.setdefault(
                        stored.spec.idempotency_key, candidate
                    )
                    accepted_attempt = self._attempts[acceptance.accepted_attempt_ref]
                    output = canonical_value(acceptance.output_payload)
                else:
                    acceptance = None
                    accepted_attempt = None
                    output = canonical_value(success.output_payload)
                return DurableCallCompletion(
                    attempt=stored,
                    accepted_attempt=accepted_attempt,
                    terminal_event=success,
                    disposition=success.success_disposition,
                    acceptance=acceptance,
                    output_payload=output,
                )
            finally:
                self._release_active_attempt(stored)

    def fail(
        self,
        attempt: DurableCallAttempt,
        *,
        failure_code: str,
        failure_payload: Mapping[str, Any] | None = None,
    ) -> DurableCallAttemptEvent:
        with self._condition:
            stored = self._stored_attempt(attempt)
            try:
                events = self._events[stored.attempt_ref]
                if any(event.status in {"succeeded", "failed"} for event in events):
                    raise DurableCallJournalError("attempt_terminal_conflict")
                event = DurableCallAttemptEvent.create(
                    stored,
                    status="failed",
                    failure_code=failure_code,
                    failure_payload=failure_payload,
                )
                events.append(event)
                return event
            finally:
                self._release_active_attempt(stored)

    def load_accepted_call(
        self,
        *,
        call_spec: DurableCallSpec,
        accepted_attempt_ref: str,
    ) -> DurableAcceptedCallClosure:
        if type(call_spec) is not DurableCallSpec:
            raise DurableCallJournalError("accepted_call_spec_invalid")
        spec = DurableCallSpec.from_dict(call_spec.to_dict())
        attempt_ref = _required_string(
            accepted_attempt_ref,
            "accepted_call_attempt_ref_invalid",
        )
        with self._lock:
            acceptance_payload = self._acceptances.get(spec.idempotency_key)
            if acceptance_payload is None:
                raise DurableCallJournalError("accepted_call_missing")
            acceptance = DurableCallAcceptance.from_dict(acceptance_payload.to_dict())
            attempt_payload = self._attempts.get(attempt_ref)
            if attempt_payload is None:
                raise DurableCallJournalError("accepted_call_missing")
            attempt = DurableCallAttempt.from_dict(attempt_payload.to_dict())
            event_payloads = self._events.get(attempt_ref)
            if event_payloads is None:
                raise DurableCallJournalError("accepted_call_event_chain_invalid")
            events = tuple(
                DurableCallAttemptEvent.from_dict(event.to_dict())
                for event in event_payloads
            )
            return _validated_accepted_call_closure(
                spec=spec,
                attempt_ref=attempt_ref,
                attempt=attempt,
                acceptance=acceptance,
                events=events,
            )

    def abandon(self, attempt: DurableCallAttempt) -> None:
        with self._condition:
            stored = self._stored_attempt(attempt)
            if any(
                event.status in {"succeeded", "failed"}
                for event in self._events[stored.attempt_ref]
            ):
                raise DurableCallJournalError("attempt_terminal_conflict")
            self._release_active_attempt(stored)

    def bind_stage(
        self,
        *,
        run_attempt_id: str,
        transition_attempt_id: str,
        stage_name: str,
        attempt_refs: Sequence[str],
        commit: bool = True,
    ) -> tuple[str, ...]:
        del commit
        run_id = _required_string(run_attempt_id, "stage_run_attempt_id_invalid")
        transition_id = _required_string(
            transition_attempt_id, "stage_transition_attempt_id_invalid"
        )
        stage = _required_string(stage_name, "stage_name_invalid")
        refs = tuple(sorted(set(attempt_refs)))
        if len(refs) != len(tuple(attempt_refs)):
            raise DurableCallJournalError("stage_attempt_refs_duplicated")
        with self._lock:
            accepted_refs = {
                acceptance.accepted_attempt_ref
                for acceptance in self._acceptances.values()
                if acceptance.run_attempt_id == run_id
            }
            if set(refs) - accepted_refs:
                raise DurableCallJournalError("stage_attempt_not_accepted")
            for ref in refs:
                self._validate_active_scope(self._attempts[ref].spec)
            if any(self._attempts[ref].spec.stage_name != stage for ref in refs):
                raise DurableCallJournalError("stage_attempt_scope_invalid")
            key = (run_id, transition_id, stage)
            existing = self._stage_bindings.setdefault(key, refs)
            if existing != refs:
                raise DurableCallJournalError("stage_binding_conflict")
            return existing

    def load_stage_attempt_refs(
        self,
        *,
        run_attempt_id: str,
        transition_attempt_id: str,
        stage_name: str,
    ) -> tuple[str, ...]:
        key = (
            _required_string(run_attempt_id, "stage_run_attempt_id_invalid"),
            _required_string(
                transition_attempt_id, "stage_transition_attempt_id_invalid"
            ),
            _required_string(stage_name, "stage_name_invalid"),
        )
        with self._lock:
            try:
                return self._stage_bindings[key]
            except KeyError as exc:
                raise DurableCallJournalError("stage_seal_missing") from exc

    def events_for_attempt(
        self, attempt: DurableCallAttempt
    ) -> tuple[DurableCallAttemptEvent, ...]:
        with self._lock:
            stored = self._stored_attempt(attempt)
            return tuple(self._events[stored.attempt_ref])

    def attempts_for_idempotency(
        self, idempotency_key: str
    ) -> tuple[DurableCallAttempt, ...]:
        key = _digest(idempotency_key, "call_idempotency_key_invalid")
        with self._lock:
            return tuple(
                self._attempts[ref]
                for ref in self._attempt_refs_by_idempotency.get(key, ())
            )

    def _stored_attempt(self, attempt: DurableCallAttempt) -> DurableCallAttempt:
        if type(attempt) is not DurableCallAttempt:
            raise DurableCallJournalError("call_attempt_invalid")
        stored = self._attempts.get(attempt.attempt_ref)
        if stored != attempt:
            raise DurableCallJournalError("call_attempt_unknown")
        return stored

    def _release_active_attempt(self, attempt: DurableCallAttempt) -> None:
        active_ref = self._active_attempt_by_idempotency.get(
            attempt.spec.idempotency_key
        )
        if active_ref == attempt.attempt_ref:
            del self._active_attempt_by_idempotency[attempt.spec.idempotency_key]
            self._condition.notify_all()

    def _scope_is_active(self, spec: DurableCallSpec) -> bool:
        if self._active_scope_validator is None:
            return True
        result = self._active_scope_validator(spec)
        if type(result) is not bool:
            raise DurableCallJournalError("active_scope_validator_result_invalid")
        return result

    def _validate_active_scope(self, spec: DurableCallSpec) -> None:
        if not self._scope_is_active(spec):
            raise DurableCallJournalError("call_scope_not_active")


class PostgresDurableCallJournal:
    def __init__(self, connection: Any) -> None:
        if connection is None or not callable(getattr(connection, "execute", None)):
            raise DurableCallJournalError("postgres_call_journal_connection_invalid")
        self.connection = connection
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._held_call_locks: dict[str, str] = {}

    def claim(self, spec: DurableCallSpec) -> DurableCallClaim:
        if type(spec) is not DurableCallSpec:
            raise DurableCallJournalError("call_claim_spec_invalid")
        spec = DurableCallSpec.from_dict(spec.to_dict())
        with self._condition:
            while spec.idempotency_key in self._held_call_locks:
                self._condition.wait()
            self._acquire_session_call_lock(spec.idempotency_key)
            self._held_call_locks[spec.idempotency_key] = ""
            try:
                self._validate_active_scope(spec)
                accepted = self._load_acceptance(spec)
                if accepted is not None:
                    acceptance, attempt = accepted
                    self.connection.commit()
                    self._release_session_call_lock(spec.idempotency_key)
                    return DurableCallClaim(
                        attempt=attempt,
                        replayed=True,
                        acceptance=acceptance,
                        output_payload=canonical_value(acceptance.output_payload),
                    )
                previous = self._latest_attempt(spec)
                if previous is None:
                    number = 1
                    retry_reason = "initial"
                else:
                    number = previous.attempt_number + 1
                    terminal = self._load_terminal_event(previous.attempt_ref)
                    if (
                        terminal is not None
                        and terminal.status == "succeeded"
                        and terminal.success_disposition == "orphaned"
                    ):
                        raise DurableCallJournalError("call_success_orphaned")
                    if terminal is not None and terminal.status == "succeeded":
                        raise DurableCallJournalError(
                            "call_acceptance_integrity_invalid"
                        )
                    retry_reason = (
                        "previous_attempt_failed"
                        if terminal is not None and terminal.status == "failed"
                        else "previous_attempt_incomplete"
                    )
                attempt = DurableCallAttempt.create(
                    spec,
                    attempt_number=number,
                    retry_reason=retry_reason,
                )
                self._insert_attempt(attempt)
                self._insert_event(
                    attempt,
                    DurableCallAttemptEvent.create(attempt, status="claimed"),
                )
                self._insert_event(
                    attempt,
                    DurableCallAttemptEvent.create(attempt, status="started"),
                )
                self._held_call_locks[spec.idempotency_key] = attempt.attempt_ref
                self.connection.commit()
                return DurableCallClaim(
                    attempt=attempt,
                    replayed=False,
                    acceptance=None,
                    output_payload=None,
                )
            except Exception:
                self.connection.rollback()
                self._release_session_call_lock(spec.idempotency_key)
                raise

    def succeed(
        self,
        attempt: DurableCallAttempt,
        output_payload: Mapping[str, Any],
    ) -> DurableCallCompletion:
        if type(attempt) is not DurableCallAttempt:
            raise DurableCallJournalError("call_attempt_invalid")
        attempt = DurableCallAttempt.from_dict(attempt.to_dict())
        with self._condition:
            try:
                self._require_owned_call_lock(attempt)
                self._require_attempt(attempt)
                disposition = (
                    "accepted" if self._scope_is_active(attempt.spec) else "orphaned"
                )
                terminal = self._load_terminal_event(attempt.attempt_ref)
                if terminal is None:
                    terminal = DurableCallAttemptEvent.create(
                        attempt,
                        status="succeeded",
                        success_disposition=disposition,
                        output_payload=output_payload,
                    )
                    self._insert_event(attempt, terminal)
                elif (
                    terminal.status != "succeeded"
                    or terminal.success_disposition != disposition
                    or canonical_value(terminal.output_payload)
                    != canonical_value(output_payload)
                ):
                    raise DurableCallJournalError("attempt_terminal_conflict")
                if disposition == "orphaned":
                    self.connection.commit()
                    self._release_session_call_lock(attempt.spec.idempotency_key)
                    return DurableCallCompletion(
                        attempt=attempt,
                        accepted_attempt=None,
                        terminal_event=terminal,
                        disposition="orphaned",
                        acceptance=None,
                        output_payload=canonical_value(terminal.output_payload),
                    )
                candidate = DurableCallAcceptance.create(attempt, terminal)
                payload = candidate.to_dict()
                self.connection.execute(
                    """
                    INSERT INTO waje_runtime.durable_call_acceptances(
                      acceptance_ref, run_attempt_id, idempotency_key,
                      accepted_attempt_ref, output_digest, output_payload,
                      content_digest, payload
                    ) VALUES (
                      %(acceptance_ref)s, %(run_attempt_id)s,
                      %(idempotency_key)s, %(accepted_attempt_ref)s,
                      %(output_digest)s, %(output_payload)s::jsonb,
                      %(content_digest)s, %(payload)s::jsonb
                    )
                    ON CONFLICT (run_attempt_id, idempotency_key) DO NOTHING
                    """,
                    {
                        **payload,
                        "output_payload": _json(payload["output_payload"]),
                        "payload": _json(payload),
                    },
                )
                accepted = self._load_acceptance(attempt.spec)
                if accepted is None:
                    raise DurableCallJournalError("call_acceptance_missing")
                acceptance, accepted_attempt = accepted
                self.connection.commit()
                self._release_session_call_lock(attempt.spec.idempotency_key)
                return DurableCallCompletion(
                    attempt=attempt,
                    accepted_attempt=accepted_attempt,
                    terminal_event=terminal,
                    disposition="accepted",
                    acceptance=acceptance,
                    output_payload=canonical_value(acceptance.output_payload),
                )
            except Exception:
                self.connection.rollback()
                self._release_session_call_lock(attempt.spec.idempotency_key)
                raise

    def fail(
        self,
        attempt: DurableCallAttempt,
        *,
        failure_code: str,
        failure_payload: Mapping[str, Any] | None = None,
    ) -> DurableCallAttemptEvent:
        if type(attempt) is not DurableCallAttempt:
            raise DurableCallJournalError("call_attempt_invalid")
        attempt = DurableCallAttempt.from_dict(attempt.to_dict())
        with self._condition:
            try:
                self._require_owned_call_lock(attempt)
                self._require_attempt(attempt)
                if self._load_terminal_event(attempt.attempt_ref) is not None:
                    raise DurableCallJournalError("attempt_terminal_conflict")
                event = DurableCallAttemptEvent.create(
                    attempt,
                    status="failed",
                    failure_code=failure_code,
                    failure_payload=failure_payload,
                )
                self._insert_event(attempt, event)
                self.connection.commit()
                self._release_session_call_lock(attempt.spec.idempotency_key)
                return event
            except Exception:
                self.connection.rollback()
                self._release_session_call_lock(attempt.spec.idempotency_key)
                raise

    def load_accepted_call(
        self,
        *,
        call_spec: DurableCallSpec,
        accepted_attempt_ref: str,
    ) -> DurableAcceptedCallClosure:
        if type(call_spec) is not DurableCallSpec:
            raise DurableCallJournalError("accepted_call_spec_invalid")
        spec = DurableCallSpec.from_dict(call_spec.to_dict())
        attempt_ref = _required_string(
            accepted_attempt_ref,
            "accepted_call_attempt_ref_invalid",
        )
        row = self.connection.execute(
            """
            SELECT attempt.payload AS attempt_payload,
                   acceptance.payload AS acceptance_payload
            FROM waje_runtime.durable_call_acceptances acceptance
            JOIN waje_runtime.durable_call_attempts attempt
              ON attempt.run_attempt_id = acceptance.run_attempt_id
             AND attempt.attempt_ref = acceptance.accepted_attempt_ref
            WHERE acceptance.run_attempt_id = %(run_attempt_id)s
              AND acceptance.idempotency_key = %(idempotency_key)s
              AND acceptance.accepted_attempt_ref = %(accepted_attempt_ref)s
            """,
            {
                "run_attempt_id": spec.run_attempt_id,
                "idempotency_key": spec.idempotency_key,
                "accepted_attempt_ref": attempt_ref,
            },
        ).fetchone()
        if row is None:
            raise DurableCallJournalError("accepted_call_missing")
        try:
            attempt = DurableCallAttempt.from_dict(
                _json_value(_field(row, "attempt_payload", 0))
            )
            acceptance = DurableCallAcceptance.from_dict(
                _json_value(_field(row, "acceptance_payload", 1))
            )
            event_rows = self.connection.execute(
                """
                SELECT payload
                FROM waje_runtime.durable_call_attempt_events
                WHERE run_attempt_id = %(run_attempt_id)s
                  AND attempt_ref = %(accepted_attempt_ref)s
                ORDER BY event_sequence
                """,
                {
                    "run_attempt_id": spec.run_attempt_id,
                    "accepted_attempt_ref": attempt_ref,
                },
            ).fetchall()
            events = tuple(
                DurableCallAttemptEvent.from_dict(
                    _json_value(_field(event_row, "payload", 0))
                )
                for event_row in event_rows
            )
        except (TypeError, ValueError) as exc:
            raise DurableCallJournalError("accepted_call_integrity_invalid") from exc
        return _validated_accepted_call_closure(
            spec=spec,
            attempt_ref=attempt_ref,
            attempt=attempt,
            acceptance=acceptance,
            events=events,
        )

    def bind_stage(
        self,
        *,
        run_attempt_id: str,
        transition_attempt_id: str,
        stage_name: str,
        attempt_refs: Sequence[str],
        commit: bool = True,
    ) -> tuple[str, ...]:
        run_id = _required_string(run_attempt_id, "stage_run_attempt_id_invalid")
        transition_id = _required_string(
            transition_attempt_id, "stage_transition_attempt_id_invalid"
        )
        stage = _required_string(stage_name, "stage_name_invalid")
        refs = tuple(sorted(set(attempt_refs)))
        if len(refs) != len(tuple(attempt_refs)):
            raise DurableCallJournalError("stage_attempt_refs_duplicated")
        with self._lock:
            try:
                row = self.connection.execute(
                    """
                    SELECT node_name
                    FROM waje_runtime.workflow_transition_attempts
                    WHERE attempt_id = %(transition_attempt_id)s
                      AND run_attempt_id = %(run_attempt_id)s
                      AND status = 'succeeded'
                      AND acceptance_state = 'accepted'
                    """,
                    {
                        "transition_attempt_id": transition_id,
                        "run_attempt_id": run_id,
                    },
                ).fetchone()
                if row is None or str(_field(row, "node_name", 0) or "") != stage:
                    raise DurableCallJournalError("stage_transition_invalid")
                accepted_rows = (
                    ()
                    if not refs
                    else self.connection.execute(
                        """
                        SELECT acceptance.accepted_attempt_ref,
                               attempt.stage_name,
                               attempt.payload AS attempt_payload
                        FROM waje_runtime.durable_call_acceptances acceptance
                        JOIN waje_runtime.durable_call_attempts attempt
                          ON attempt.run_attempt_id = acceptance.run_attempt_id
                         AND attempt.attempt_ref = acceptance.accepted_attempt_ref
                        WHERE acceptance.run_attempt_id = %(run_attempt_id)s
                          AND acceptance.accepted_attempt_ref = ANY(%(attempt_refs)s)
                        """,
                        {"run_attempt_id": run_id, "attempt_refs": list(refs)},
                    ).fetchall()
                )
                if {
                    str(_field(item, "accepted_attempt_ref", 0) or "")
                    for item in accepted_rows
                } != set(refs):
                    raise DurableCallJournalError("stage_attempt_not_accepted")
                if any(
                    str(_field(item, "stage_name", 1) or "") != stage
                    for item in accepted_rows
                ):
                    raise DurableCallJournalError("stage_attempt_scope_invalid")
                for item in accepted_rows:
                    accepted_attempt = DurableCallAttempt.from_dict(
                        _json_value(_field(item, "attempt_payload", 2))
                    )
                    if accepted_attempt.attempt_ref != str(
                        _field(item, "accepted_attempt_ref", 0) or ""
                    ):
                        raise DurableCallJournalError(
                            "call_acceptance_integrity_invalid"
                        )
                    self._validate_active_scope(accepted_attempt.spec)
                attempt_set_digest = canonical_digest(refs)
                seal_body = {
                    "run_attempt_id": run_id,
                    "transition_attempt_id": transition_id,
                    "stage_name": stage,
                    "attempt_refs": refs,
                    "attempt_set_digest": attempt_set_digest,
                }
                seal_digest = canonical_digest(seal_body)
                seal_ref = "durable-stage-attempt-seal:sha256:" + seal_digest
                existing = self.connection.execute(
                    """
                    SELECT payload
                    FROM waje_runtime.durable_stage_attempt_seals
                    WHERE run_attempt_id = %(run_attempt_id)s
                      AND transition_attempt_id = %(transition_attempt_id)s
                      AND stage_name = %(stage_name)s
                    """,
                    {
                        "run_attempt_id": run_id,
                        "transition_attempt_id": transition_id,
                        "stage_name": stage,
                    },
                ).fetchone()
                if existing is not None:
                    stored = _json_value(_field(existing, "payload", 0))
                    if canonical_value(stored) != canonical_value(
                        {
                            **seal_body,
                            "stage_seal_ref": seal_ref,
                            "content_digest": seal_digest,
                        }
                    ):
                        raise DurableCallJournalError("stage_binding_conflict")
                    stored_refs = self.load_stage_attempt_refs(
                        run_attempt_id=run_id,
                        transition_attempt_id=transition_id,
                        stage_name=stage,
                    )
                    if stored_refs != refs:
                        raise DurableCallJournalError("stage_binding_conflict")
                    if commit:
                        self.connection.commit()
                    return stored_refs
                seal_payload = {
                    **seal_body,
                    "stage_seal_ref": seal_ref,
                    "content_digest": seal_digest,
                }
                self.connection.execute(
                    """
                    INSERT INTO waje_runtime.durable_stage_attempt_seals(
                      stage_seal_ref, run_attempt_id, transition_attempt_id,
                      stage_name, attempt_set_digest, content_digest, payload
                    ) VALUES (
                      %(stage_seal_ref)s, %(run_attempt_id)s,
                      %(transition_attempt_id)s, %(stage_name)s,
                      %(attempt_set_digest)s, %(content_digest)s,
                      %(payload)s::jsonb
                    )
                    """,
                    {**seal_payload, "payload": _json(seal_payload)},
                )
                for attempt_ref in refs:
                    binding_body = {
                        "run_attempt_id": run_id,
                        "stage_seal_ref": seal_ref,
                        "transition_attempt_id": transition_id,
                        "stage_name": stage,
                        "accepted_attempt_ref": attempt_ref,
                    }
                    binding_digest = canonical_digest(binding_body)
                    binding_payload = {
                        **binding_body,
                        "binding_ref": (
                            "durable-stage-attempt-binding:sha256:" + binding_digest
                        ),
                        "content_digest": binding_digest,
                    }
                    self.connection.execute(
                        """
                        INSERT INTO waje_runtime.durable_stage_attempt_bindings(
                          binding_ref, run_attempt_id, stage_seal_ref,
                          transition_attempt_id, stage_name,
                          accepted_attempt_ref, content_digest, payload
                        ) VALUES (
                          %(binding_ref)s, %(run_attempt_id)s,
                          %(stage_seal_ref)s, %(transition_attempt_id)s,
                          %(stage_name)s, %(accepted_attempt_ref)s,
                          %(content_digest)s, %(payload)s::jsonb
                        )
                        """,
                        {**binding_payload, "payload": _json(binding_payload)},
                    )
                if commit:
                    self.connection.commit()
                return refs
            except Exception:
                self.connection.rollback()
                raise

    def load_stage_attempt_refs(
        self,
        *,
        run_attempt_id: str,
        transition_attempt_id: str,
        stage_name: str,
    ) -> tuple[str, ...]:
        run_id = _required_string(run_attempt_id, "stage_run_attempt_id_invalid")
        transition_id = _required_string(
            transition_attempt_id, "stage_transition_attempt_id_invalid"
        )
        stage = _required_string(stage_name, "stage_name_invalid")
        seal = self.connection.execute(
            """
            SELECT payload
            FROM waje_runtime.durable_stage_attempt_seals
            WHERE run_attempt_id = %(run_attempt_id)s
              AND transition_attempt_id = %(transition_attempt_id)s
              AND stage_name = %(stage_name)s
            """,
            {
                "run_attempt_id": run_id,
                "transition_attempt_id": transition_id,
                "stage_name": stage,
            },
        ).fetchone()
        if seal is None:
            raise DurableCallJournalError("stage_seal_missing")
        payload = _json_value(_field(seal, "payload", 0))
        if not isinstance(payload, Mapping):
            raise DurableCallJournalError("stage_seal_invalid")
        refs = tuple(payload.get("attempt_refs") or ())
        rows = self.connection.execute(
            """
            SELECT accepted_attempt_ref
            FROM waje_runtime.durable_stage_attempt_bindings
            WHERE run_attempt_id = %(run_attempt_id)s
              AND transition_attempt_id = %(transition_attempt_id)s
              AND stage_name = %(stage_name)s
            ORDER BY accepted_attempt_ref
            """,
            {
                "run_attempt_id": run_id,
                "transition_attempt_id": transition_id,
                "stage_name": stage,
            },
        ).fetchall()
        stored_refs = tuple(
            str(_field(row, "accepted_attempt_ref", 0) or "") for row in rows
        )
        body = {
            "run_attempt_id": run_id,
            "transition_attempt_id": transition_id,
            "stage_name": stage,
            "attempt_refs": refs,
            "attempt_set_digest": canonical_digest(refs),
        }
        digest = canonical_digest(body)
        if (
            refs != stored_refs
            or payload.get("attempt_set_digest") != canonical_digest(refs)
            or payload.get("content_digest") != digest
            or payload.get("stage_seal_ref")
            != "durable-stage-attempt-seal:sha256:" + digest
        ):
            raise DurableCallJournalError("stage_seal_invalid")
        return refs

    def _acquire_session_call_lock(self, idempotency_key: str) -> None:
        self.connection.execute(
            "SELECT pg_advisory_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": "durable-call:" + idempotency_key},
        )

    def _require_owned_call_lock(
        self,
        attempt: DurableCallAttempt,
    ) -> None:
        if (
            self._held_call_locks.get(attempt.spec.idempotency_key)
            != attempt.attempt_ref
        ):
            raise DurableCallJournalError("call_attempt_owner_missing")

    def _release_session_call_lock(self, idempotency_key: str) -> None:
        if idempotency_key not in self._held_call_locks:
            return
        row = self.connection.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": "durable-call:" + idempotency_key},
        ).fetchone()
        if row is None or _field(row, "pg_advisory_unlock", 0) is not True:
            raise DurableCallJournalError("call_session_lock_release_failed")
        self.connection.commit()
        del self._held_call_locks[idempotency_key]
        self._condition.notify_all()

    def _scope_is_active(self, spec: DurableCallSpec) -> bool:
        self.connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": "single_authority:" + spec.run_attempt_id},
        )
        requires_intent, requires_plan, requires_task = CALL_SCOPE_REQUIREMENTS[
            spec.call_kind
        ]
        parameters = {
            "run_attempt_id": spec.run_attempt_id,
            "intent_revision_id": spec.intent_revision_id,
            "plan_revision_id": spec.plan_revision_id,
            "task_id": spec.task_id,
        }
        if not requires_intent:
            statement = """
                SELECT run.run_id
                FROM waje_runtime.analysis_runs run
                JOIN LATERAL (
                  SELECT state.cancellation_state, state.supersession_state
                  FROM waje_runtime.run_lifecycle_state_revisions state
                  WHERE state.run_attempt_id = run.run_id
                  ORDER BY state.state_revision DESC
                  LIMIT 1
                ) lifecycle ON TRUE
                WHERE run.run_id = %(run_attempt_id)s
                  AND run.run_attempt_id = %(run_attempt_id)s
                  AND lifecycle.cancellation_state = 'active'
                  AND lifecycle.supersession_state = 'active'
                FOR UPDATE OF run
            """
        elif not requires_plan:
            statement = """
                SELECT run.run_id
                FROM waje_runtime.analysis_runs run
                JOIN waje_runtime.intent_revisions intent
                  ON intent.run_attempt_id = run.run_id
                 AND intent.intent_revision_id = %(intent_revision_id)s
                JOIN LATERAL (
                  SELECT state.cancellation_state, state.supersession_state
                  FROM waje_runtime.run_lifecycle_state_revisions state
                  WHERE state.run_attempt_id = run.run_id
                  ORDER BY state.state_revision DESC
                  LIMIT 1
                ) lifecycle ON TRUE
                WHERE run.run_id = %(run_attempt_id)s
                  AND run.run_attempt_id = %(run_attempt_id)s
                  AND lifecycle.cancellation_state = 'active'
                  AND lifecycle.supersession_state = 'active'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM waje_runtime.intent_revision_supersessions supersession
                    WHERE supersession.superseded_intent_revision_id
                        = intent.intent_revision_id
                  )
                FOR UPDATE OF run, intent
            """
        else:
            task_clause = (
                """
                  AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                      plan.payload->'capability_tasks'
                    ) AS task(payload)
                    WHERE task.payload->>'task_id' = %(task_id)s
                  )
                """
                if requires_task
                else ""
            )
            statement = (
                """
                SELECT run.run_id
                FROM waje_runtime.analysis_runs run
                JOIN waje_runtime.intent_revisions intent
                  ON intent.run_attempt_id = run.run_id
                 AND intent.intent_revision_id = %(intent_revision_id)s
                JOIN waje_runtime.plan_revisions plan
                  ON plan.run_attempt_id = run.run_id
                 AND plan.intent_revision_id = intent.intent_revision_id
                 AND plan.plan_revision_id = %(plan_revision_id)s
                JOIN LATERAL (
                  SELECT state.cancellation_state, state.supersession_state
                  FROM waje_runtime.run_lifecycle_state_revisions state
                  WHERE state.run_attempt_id = run.run_id
                  ORDER BY state.state_revision DESC
                  LIMIT 1
                ) lifecycle ON TRUE
                WHERE run.run_id = %(run_attempt_id)s
                  AND run.run_attempt_id = %(run_attempt_id)s
                  AND lifecycle.cancellation_state = 'active'
                  AND lifecycle.supersession_state = 'active'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM waje_runtime.intent_revision_supersessions supersession
                    WHERE supersession.superseded_intent_revision_id
                        = intent.intent_revision_id
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM waje_runtime.plan_revision_supersessions supersession
                    WHERE supersession.superseded_plan_revision_id
                        = plan.plan_revision_id
                  )
                """
                + task_clause
                + " FOR UPDATE OF run, intent, plan"
            )
        row = self.connection.execute(statement, parameters).fetchone()
        return row is not None

    def _validate_active_scope(self, spec: DurableCallSpec) -> None:
        if not self._scope_is_active(spec):
            raise DurableCallJournalError("call_scope_not_active")

    def _latest_attempt(self, spec: DurableCallSpec) -> DurableCallAttempt | None:
        row = self.connection.execute(
            """
            SELECT payload
            FROM waje_runtime.durable_call_attempts
            WHERE run_attempt_id = %(run_attempt_id)s
              AND idempotency_key = %(idempotency_key)s
            ORDER BY attempt_number DESC
            LIMIT 1
            """,
            {
                "run_attempt_id": spec.run_attempt_id,
                "idempotency_key": spec.idempotency_key,
            },
        ).fetchone()
        if row is None:
            return None
        attempt = DurableCallAttempt.from_dict(_json_value(_field(row, "payload", 0)))
        if attempt.spec != spec:
            raise DurableCallJournalError("call_spec_idempotency_conflict")
        return attempt

    def _load_acceptance(
        self, spec: DurableCallSpec
    ) -> tuple[DurableCallAcceptance, DurableCallAttempt] | None:
        row = self.connection.execute(
            """
            SELECT acceptance.payload AS acceptance_payload,
                   attempt.payload AS attempt_payload
            FROM waje_runtime.durable_call_acceptances acceptance
            JOIN waje_runtime.durable_call_attempts attempt
              ON attempt.run_attempt_id = acceptance.run_attempt_id
             AND attempt.attempt_ref = acceptance.accepted_attempt_ref
            WHERE acceptance.run_attempt_id = %(run_attempt_id)s
              AND acceptance.idempotency_key = %(idempotency_key)s
            """,
            {
                "run_attempt_id": spec.run_attempt_id,
                "idempotency_key": spec.idempotency_key,
            },
        ).fetchone()
        if row is None:
            return None
        acceptance = DurableCallAcceptance.from_dict(
            _json_value(_field(row, "acceptance_payload", 0))
        )
        attempt = DurableCallAttempt.from_dict(
            _json_value(_field(row, "attempt_payload", 1))
        )
        terminal = self._load_terminal_event(attempt.attempt_ref)
        if (
            attempt.spec != spec
            or acceptance.run_attempt_id != spec.run_attempt_id
            or acceptance.idempotency_key != spec.idempotency_key
            or acceptance.accepted_attempt_ref != attempt.attempt_ref
            or terminal is None
            or terminal.status != "succeeded"
            or terminal.success_disposition != "accepted"
            or terminal.output_digest != acceptance.output_digest
            or canonical_value(terminal.output_payload)
            != canonical_value(acceptance.output_payload)
        ):
            raise DurableCallJournalError("call_acceptance_integrity_invalid")
        return acceptance, attempt

    def _load_terminal_event(self, attempt_ref: str) -> DurableCallAttemptEvent | None:
        row = self.connection.execute(
            """
            SELECT payload
            FROM waje_runtime.durable_call_attempt_events
            WHERE attempt_ref = %(attempt_ref)s
              AND event_sequence = 3
            """,
            {"attempt_ref": attempt_ref},
        ).fetchone()
        if row is None:
            return None
        return DurableCallAttemptEvent.from_dict(_json_value(_field(row, "payload", 0)))

    def _require_attempt(self, attempt: DurableCallAttempt) -> None:
        row = self.connection.execute(
            """
            SELECT payload
            FROM waje_runtime.durable_call_attempts
            WHERE attempt_ref = %(attempt_ref)s
              AND run_attempt_id = %(run_attempt_id)s
            """,
            {
                "attempt_ref": attempt.attempt_ref,
                "run_attempt_id": attempt.spec.run_attempt_id,
            },
        ).fetchone()
        if (
            row is None
            or DurableCallAttempt.from_dict(_json_value(_field(row, "payload", 0)))
            != attempt
        ):
            raise DurableCallJournalError("call_attempt_unknown")

    def _insert_attempt(self, attempt: DurableCallAttempt) -> None:
        payload = attempt.to_dict()
        spec = attempt.spec
        self.connection.execute(
            """
            INSERT INTO waje_runtime.durable_call_attempts(
              attempt_ref, run_attempt_id, intent_revision_id,
              plan_revision_id, task_id, stage_name, call_kind, operation_name,
              input_ref, input_digest, idempotency_key, attempt_number,
              retry_reason, content_digest, payload
            ) VALUES (
              %(attempt_ref)s, %(run_attempt_id)s, %(intent_revision_id)s,
              %(plan_revision_id)s, %(task_id)s, %(stage_name)s, %(call_kind)s,
              %(operation_name)s, %(input_ref)s, %(input_digest)s,
              %(idempotency_key)s, %(attempt_number)s, %(retry_reason)s,
              %(content_digest)s, %(payload)s::jsonb
            )
            """,
            {
                "attempt_ref": attempt.attempt_ref,
                "run_attempt_id": spec.run_attempt_id,
                "intent_revision_id": spec.intent_revision_id,
                "plan_revision_id": spec.plan_revision_id,
                "task_id": spec.task_id,
                "stage_name": spec.stage_name,
                "call_kind": spec.call_kind,
                "operation_name": spec.operation_name,
                "input_ref": spec.input_ref,
                "input_digest": spec.input_digest,
                "idempotency_key": spec.idempotency_key,
                "attempt_number": attempt.attempt_number,
                "retry_reason": attempt.retry_reason,
                "content_digest": attempt.content_digest,
                "payload": _json(payload),
            },
        )

    def _insert_event(
        self,
        attempt: DurableCallAttempt,
        event: DurableCallAttemptEvent,
    ) -> None:
        if event.attempt_ref != attempt.attempt_ref:
            raise DurableCallJournalError("call_event_attempt_invalid")
        payload = event.to_dict()
        self.connection.execute(
            """
            INSERT INTO waje_runtime.durable_call_attempt_events(
              event_ref, run_attempt_id, attempt_ref, event_sequence,
              status, success_disposition, output_digest, output_payload, failure_code,
              failure_payload, content_digest, payload
            ) VALUES (
              %(event_ref)s, %(run_attempt_id)s, %(attempt_ref)s,
              %(event_sequence)s, %(status)s, %(success_disposition)s,
              %(output_digest)s,
              %(output_payload)s::jsonb, %(failure_code)s,
              %(failure_payload)s::jsonb, %(content_digest)s,
              %(payload)s::jsonb
            )
            """,
            {
                **payload,
                "run_attempt_id": attempt.spec.run_attempt_id,
                "output_payload": (
                    None
                    if payload["output_payload"] is None
                    else _json(payload["output_payload"])
                ),
                "failure_payload": (
                    None
                    if payload["failure_payload"] is None
                    else _json(payload["failure_payload"])
                ),
                "payload": _json(payload),
            },
        )


@dataclass(frozen=True)
class _JournaledProviderResult:
    output: Mapping[str, Any]
    audit: Mapping[str, Any]


class DurableProviderClient:
    def __init__(
        self,
        provider_client: Any,
        *,
        journal: DurableCallJournal,
        run_attempt_id: str,
        intent_revision_id: str | None,
        plan_revision_id: str | None,
        call_kind: str,
        task_id: str | None,
        stage_name: str,
    ) -> None:
        if not callable(getattr(provider_client, "invoke_json", None)):
            raise DurableCallJournalError("provider_client_invalid")
        if not isinstance(journal, DurableCallJournal):
            raise DurableCallJournalError("provider_journal_invalid")
        self._provider_client = provider_client
        self._provider_supports_output_validator = bool(
            getattr(provider_client, "supports_output_validator", False)
        )
        self.supports_model_tier = bool(
            getattr(provider_client, "supports_model_tier", False)
        )
        self.supports_thinking_mode = bool(
            getattr(provider_client, "supports_thinking_mode", False)
        )
        self._journal = journal
        self._run_attempt_id = _required_string(
            run_attempt_id, "provider_run_attempt_id_invalid"
        )
        if call_kind not in PROVIDER_CALL_KINDS:
            raise DurableCallJournalError("provider_call_kind_invalid")
        self._call_kind = call_kind
        requirements = CALL_SCOPE_REQUIREMENTS[call_kind]
        self._intent_revision_id = _tagged_scope_ref(
            intent_revision_id,
            required=requirements[0],
            error="provider_intent_revision_id_invalid",
        )
        self._plan_revision_id = _tagged_scope_ref(
            plan_revision_id,
            required=requirements[1],
            error="provider_plan_revision_id_invalid",
        )
        self._task_id = _tagged_scope_ref(
            task_id,
            required=requirements[2],
            error="provider_task_id_invalid",
        )
        self._stage_name = _required_string(stage_name, "provider_stage_name_invalid")
        self._accepted_attempt_refs: list[str] = []
        self._accepted_call_specs: list[DurableCallSpec] = []

    @property
    def accepted_attempt_refs(self) -> tuple[str, ...]:
        return tuple(self._accepted_attempt_refs)

    @property
    def accepted_call_specs(self) -> tuple[DurableCallSpec, ...]:
        return tuple(self._accepted_call_specs)

    def invoke_json(
        self,
        *,
        task: str,
        prompt_version: str,
        messages: Sequence[Mapping[str, str]],
        required_keys: Sequence[str],
        output_validator: Callable[[Mapping[str, Any]], None] | None = None,
        model_tier: str = "default",
        thinking: str | None = None,
    ) -> _JournaledProviderResult:
        validator_ref = (
            None
            if output_validator is None
            else (
                f"{getattr(output_validator, '__module__', '')}:"
                f"{getattr(output_validator, '__qualname__', '')}"
            )
        )
        input_payload = canonical_value(
            {
                "task": task,
                "prompt_version": prompt_version,
                "messages": tuple(dict(item) for item in messages),
                "required_keys": tuple(required_keys),
                "output_validator_ref": validator_ref,
                "model_tier": model_tier,
                "thinking": thinking,
            }
        )
        input_digest = canonical_digest(input_payload)
        spec = DurableCallSpec.create(
            run_attempt_id=self._run_attempt_id,
            intent_revision_id=self._intent_revision_id,
            plan_revision_id=self._plan_revision_id,
            task_id=self._task_id,
            stage_name=self._stage_name,
            call_kind=self._call_kind,
            operation_name=task,
            input_ref="provider-call-input:sha256:" + input_digest,
            input_payload=input_payload,
        )
        max_attempts = getattr(
            self._provider_client,
            "durable_max_attempts",
            1,
        )
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise DurableCallJournalError("provider_max_attempts_invalid")
        prior_failures: list[dict[str, Any]] = []
        while True:
            claim = self._journal.claim(spec)
            if claim.replayed:
                return self._provider_result(
                    claim.output_payload,
                    accepted_attempt_ref=claim.attempt.attempt_ref,
                    call_spec=claim.attempt.spec,
                )
            try:
                provider_kwargs: dict[str, Any] = {
                    "task": task,
                    "prompt_version": prompt_version,
                    "messages": messages,
                    "required_keys": required_keys,
                }
                if (
                    output_validator is not None
                    and self._provider_supports_output_validator
                ):
                    provider_kwargs["output_validator"] = output_validator
                if model_tier != "default":
                    provider_kwargs["model_tier"] = model_tier
                if thinking is not None:
                    provider_kwargs["thinking"] = thinking
                result = self._provider_client.invoke_json(
                    **provider_kwargs,
                )
                output = getattr(result, "output", None)
                audit = getattr(result, "audit", None)
                if not isinstance(output, Mapping) or not isinstance(audit, Mapping):
                    raise DurableCallJournalError("provider_result_invalid")
                if output_validator is not None:
                    try:
                        output_validator(output)
                    except (ValueError, LLMOutputError) as exc:
                        failure_code = str(exc).strip() or "llm_output_contract_invalid"
                        raise LLMOutputError(
                            failure_code,
                            audit=_provider_failure_journal_audit(
                                audit,
                                failure_code=failure_code,
                                attempt_number=claim.attempt.attempt_number,
                                call_spec=claim.attempt.spec,
                            ),
                        ) from exc
                provider_payload = {
                    "output": canonical_value(output),
                    "audit": _provider_success_audit(
                        audit,
                        attempt_number=claim.attempt.attempt_number,
                        prior_failures=prior_failures,
                    ),
                }
            except Exception as exc:
                raw_audit = getattr(exc, "audit", None)
                failure_code = llm_failure_code(exc)
                self._journal.fail(
                    claim.attempt,
                    failure_code=failure_code,
                    failure_payload={
                        "audit": _provider_failure_journal_audit(
                            raw_audit,
                            failure_code=failure_code,
                            attempt_number=claim.attempt.attempt_number,
                            call_spec=claim.attempt.spec,
                            provider_error=getattr(exc, "provider_error", None),
                        )
                    },
                )
                if (
                    llm_failure_is_retryable(exc)
                    and claim.attempt.attempt_number < max_attempts
                ):
                    prior_failures.append(
                        _provider_failure_audit(
                            exc,
                            attempt_number=claim.attempt.attempt_number,
                        )
                    )
                    continue
                raise
            completion = self._journal.succeed(
                claim.attempt,
                provider_payload,
            )
            if (
                completion.disposition != "accepted"
                or completion.acceptance is None
                or completion.accepted_attempt is None
            ):
                raise DurableCallJournalError("call_success_orphaned")
            return self._provider_result(
                completion.output_payload,
                accepted_attempt_ref=(completion.acceptance.accepted_attempt_ref),
                call_spec=completion.accepted_attempt.spec,
            )

    def _provider_result(
        self,
        payload: Mapping[str, Any] | None,
        *,
        accepted_attempt_ref: str,
        call_spec: DurableCallSpec,
    ) -> _JournaledProviderResult:
        if not isinstance(payload, Mapping) or set(payload) != {"output", "audit"}:
            raise DurableCallJournalError("provider_journal_output_invalid")
        output = payload["output"]
        audit = payload["audit"]
        if not isinstance(output, Mapping) or not isinstance(audit, Mapping):
            raise DurableCallJournalError("provider_journal_output_invalid")
        if accepted_attempt_ref not in self._accepted_attempt_refs:
            self._accepted_attempt_refs.append(accepted_attempt_ref)
            self._accepted_call_specs.append(call_spec)
        return _JournaledProviderResult(
            output=MappingProxyType(dict(canonical_value(output))),
            audit=MappingProxyType(dict(canonical_value(audit))),
        )


def _provider_failure_audit(
    exc: Exception,
    *,
    attempt_number: int,
) -> dict[str, Any]:
    raw_audit = getattr(exc, "audit", None)
    normalized_audit = _compact_provider_failure_audit(raw_audit)
    provider_error = _compact_provider_error(getattr(exc, "provider_error", None))
    root_diagnostics = {
        key: normalized_audit[key]
        for key in (
            "task",
            "provider",
            "model",
            "model_tier",
            "thinking",
            "prompt_version",
            "started_at",
            "finished_at",
            "duration_ms",
            "usage",
        )
        if key in normalized_audit
    }
    if isinstance(raw_audit, Mapping):
        failures = normalized_audit.get("attempt_failures")
        if (
            isinstance(failures, Sequence)
            and not isinstance(failures, (str, bytes))
            and failures
            and isinstance(failures[-1], Mapping)
        ):
            failure = {
                **root_diagnostics,
                **canonical_value(dict(failures[-1])),
                "attempt": attempt_number,
            }
            if provider_error:
                failure["provider_error"] = provider_error
            return failure
    failure = {
        **root_diagnostics,
        "attempt": attempt_number,
        "failure_code": llm_failure_code(exc),
        "response_id": "",
        "reasoning_content_present": False,
    }
    if provider_error:
        failure["provider_error"] = provider_error
    return failure


def _provider_failure_journal_audit(
    raw_audit: Any,
    *,
    failure_code: str,
    attempt_number: int,
    call_spec: DurableCallSpec,
    provider_error: Any = None,
) -> dict[str, Any]:
    normalized = _compact_provider_failure_audit(raw_audit)
    normalized.update(
        {
            "status": "failed",
            "failure_code": failure_code,
            "attempt_number": attempt_number,
            "call_input_ref": call_spec.input_ref,
            "call_input_digest": call_spec.input_digest,
            "call_input_bytes": len(_json(call_spec.input_payload).encode("utf-8")),
        }
    )
    diagnostics = _compact_provider_error(provider_error)
    if diagnostics:
        normalized["provider_error"] = diagnostics
    failures = normalized.get("attempt_failures")
    if not isinstance(failures, list) or not failures:
        failure = {
            "attempt": attempt_number,
            "failure_code": failure_code,
            "response_id": str(normalized.get("response_id") or ""),
            "reasoning_content_present": bool(
                normalized.get("reasoning_content_present", False)
            ),
        }
        if diagnostics:
            failure["provider_error"] = diagnostics
        normalized["attempt_failures"] = [failure]
    return normalized


def _compact_provider_failure_audit(raw_audit: Any) -> dict[str, Any]:
    if not isinstance(raw_audit, Mapping):
        return {}
    normalized: dict[str, Any] = {}
    for key in (
        "task",
        "provider",
        "model",
        "model_tier",
        "thinking",
        "reasoning_content_present",
        "prompt_version",
        "response_id",
        "required_keys",
        "started_at",
        "finished_at",
        "duration_ms",
        "attempt_count",
        "input_hash",
        "input_bytes",
        "input_message_count",
        "base_url_hash",
        "usage",
        "status",
        "failure_code",
        "output_hash",
        "raw_response_digest",
        "raw_response_bytes",
        "structured_output_digest",
        "structured_output_bytes",
        "call_input_ref",
        "call_input_digest",
        "call_input_bytes",
        "attempt_number",
    ):
        if key in raw_audit:
            normalized[key] = canonical_value(raw_audit[key])

    raw_response = raw_audit.get("raw_response_content")
    if isinstance(raw_response, str):
        normalized.setdefault("raw_response_digest", canonical_digest(raw_response))
        normalized.setdefault("raw_response_bytes", len(raw_response.encode("utf-8")))
    structured_output = raw_audit.get("structured_output")
    if isinstance(structured_output, Mapping):
        normalized.setdefault(
            "structured_output_digest", canonical_digest(structured_output)
        )
        normalized.setdefault(
            "structured_output_bytes",
            len(_json(structured_output).encode("utf-8")),
        )

    diagnostics = _compact_provider_error(raw_audit.get("provider_error"))
    if diagnostics:
        normalized["provider_error"] = diagnostics
    failures = raw_audit.get("attempt_failures")
    if isinstance(failures, Sequence) and not isinstance(failures, (str, bytes)):
        normalized_failures = [
            compact
            for item in failures
            if isinstance(item, Mapping)
            for compact in (_compact_provider_attempt_failure(item),)
        ]
        if normalized_failures:
            normalized["attempt_failures"] = normalized_failures
    return normalized


def _compact_provider_attempt_failure(
    failure: Mapping[str, Any],
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in (
        "attempt",
        "failure_code",
        "response_id",
        "reasoning_content_present",
        "raw_response_digest",
        "raw_response_bytes",
        "structured_output_digest",
        "structured_output_bytes",
        "started_at",
        "finished_at",
        "duration_ms",
        "usage",
    ):
        if key in failure:
            normalized[key] = canonical_value(failure[key])
    raw_response = failure.get("raw_response_content")
    if isinstance(raw_response, str):
        normalized.setdefault("raw_response_digest", canonical_digest(raw_response))
        normalized.setdefault("raw_response_bytes", len(raw_response.encode("utf-8")))
    structured_output = failure.get("structured_output")
    if isinstance(structured_output, Mapping):
        normalized.setdefault(
            "structured_output_digest", canonical_digest(structured_output)
        )
        normalized.setdefault(
            "structured_output_bytes",
            len(_json(structured_output).encode("utf-8")),
        )
    diagnostics = _compact_provider_error(failure.get("provider_error"))
    if diagnostics:
        normalized["provider_error"] = diagnostics
    return normalized


def _compact_provider_error(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, Any] = {}
    status_code = value.get("status_code")
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        normalized["status_code"] = status_code
    for key in ("code", "type", "param"):
        item = value.get(key)
        if isinstance(item, str) and item:
            normalized[key] = item
    return normalized


def _provider_success_audit(
    audit: Mapping[str, Any],
    *,
    attempt_number: int,
    prior_failures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    normalized = dict(canonical_value(audit))
    if attempt_number > 1:
        normalized["attempt_count"] = attempt_number
        normalized["attempt_failures"] = canonical_value(prior_failures)
    return normalized


__all__ = (
    "DurableCallAcceptance",
    "DurableCallAttempt",
    "DurableCallAttemptEvent",
    "DurableCallClaim",
    "DurableCallCompletion",
    "DurableCallJournal",
    "DurableCallJournalError",
    "DurableCallSpec",
    "DurableProviderClient",
    "InMemoryDurableCallJournal",
    "PostgresDurableCallJournal",
)


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _json(value: Any) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value
