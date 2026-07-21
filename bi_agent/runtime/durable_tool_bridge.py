from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bi_agent.runtime.agent_sdk_contracts import AgentToolResult
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.thread_item_ledger import ThreadItem, ThreadItemLedger


AGENT_CHECKPOINT_SCHEMA_VERSION = "agent-checkpoint.v2"


class PendingActionOption(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    option_id: str = Field(alias="optionId", min_length=1)
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    recommended: bool = False

    @field_validator("option_id", "label", "description")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _exact_text(value, "pending_action_option_text_invalid")


class AgentPendingAction(BaseModel):
    """Customer-safe pending action owned by the latest ThreadHead only."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    action_ref: str = Field(alias="actionRef", min_length=1)
    action_type: Literal["ask_user", "request_approval"] = Field(
        alias="actionType"
    )
    prompt: str = Field(min_length=1)
    options: list[PendingActionOption] = Field(default_factory=list)
    action_summary: str | None = Field(alias="actionSummary", default=None)
    side_effect_scope: str | None = Field(alias="sideEffectScope", default=None)

    @field_validator("action_ref", "prompt")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        return _exact_text(value, "pending_action_text_invalid")

    @field_validator("action_summary", "side_effect_scope")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _exact_text(value, "pending_action_text_invalid")

    @model_validator(mode="after")
    def validate_action_shape(self) -> "AgentPendingAction":
        if self.action_type == "ask_user":
            if (
                not 2 <= len(self.options) <= 3
                or sum(option.recommended for option in self.options) != 1
                or self.action_summary is not None
                or self.side_effect_scope is not None
            ):
                raise ValueError("pending_action_question_shape_invalid")
        elif (
            self.options
            or self.action_summary is None
            or self.side_effect_scope is None
        ):
            raise ValueError("pending_action_approval_shape_invalid")
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("pending_action_option_duplicate")
        return self

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class PendingActionResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    action_ref: str = Field(alias="actionRef", min_length=1)
    decision: Literal["answered", "approved", "rejected"]
    selected_option_id: str | None = Field(
        alias="selectedOptionId",
        default=None,
    )
    answer_text: str = Field(alias="answerText", min_length=1)

    @field_validator("action_ref", "answer_text")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        return _exact_text(value, "pending_action_resolution_text_invalid")

    @field_validator("selected_option_id")
    @classmethod
    def validate_selected_option(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _exact_text(value, "pending_action_resolution_option_invalid")


class AgentCheckpoint(BaseModel):
    """SDK-neutral durable boundary for task and human interruption recovery."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["agent-checkpoint.v2"] = Field(alias="schemaVersion")
    checkpoint_ref: str = Field(alias="checkpointRef", min_length=1)
    thread_id: str = Field(alias="threadId", min_length=1)
    agent_run_id: str = Field(alias="agentRunId", min_length=1)
    operation_id: str = Field(alias="operationId", min_length=1)
    checkpoint_kind: Literal[
        "waiting_for_task",
        "waiting_for_user",
        "waiting_for_approval",
    ] = Field(alias="checkpointKind")
    source_tool_name: str = Field(alias="sourceToolName", min_length=1)
    source_tool_call_id: str = Field(alias="sourceToolCallId", min_length=1)
    awaited_task_ref: str | None = Field(alias="awaitedTaskRef", default=None)
    pending_action: AgentPendingAction | None = Field(
        alias="pendingAction",
        default=None,
    )
    context_version: str = Field(alias="contextVersion", min_length=1)
    action_binding_digest: str | None = Field(
        alias="actionBindingDigest",
        default=None,
    )
    session_through_sequence: int = Field(
        alias="sessionThroughSequence",
        ge=1,
    )
    content_digest: str = Field(alias="contentDigest", min_length=64, max_length=64)

    @classmethod
    def create(
        cls,
        *,
        thread_id: str,
        agent_run_id: str,
        operation_id: str,
        suspension: "DurableToolSuspension",
        context_version: str,
        session_through_sequence: int,
        action_binding_digest: str | None = None,
    ) -> "AgentCheckpoint":
        identity = {
            "schema_version": AGENT_CHECKPOINT_SCHEMA_VERSION,
            "thread_id": _exact_text(thread_id, "agent_checkpoint_thread_invalid"),
            "agent_run_id": _exact_text(
                agent_run_id,
                "agent_checkpoint_run_invalid",
            ),
            "operation_id": _exact_text(
                operation_id,
                "agent_checkpoint_operation_invalid",
            ),
            "checkpoint_kind": suspension.checkpoint_kind,
            "source_tool_name": suspension.source_tool_name,
            "source_tool_call_id": suspension.source_tool_call_id,
        }
        checkpoint_ref = "agent-checkpoint:sha256:" + canonical_digest(identity)
        body = {
            "schemaVersion": AGENT_CHECKPOINT_SCHEMA_VERSION,
            "checkpointRef": checkpoint_ref,
            "threadId": thread_id,
            "agentRunId": agent_run_id,
            "operationId": operation_id,
            "checkpointKind": suspension.checkpoint_kind,
            "sourceToolName": suspension.source_tool_name,
            "sourceToolCallId": suspension.source_tool_call_id,
            "awaitedTaskRef": suspension.awaited_task_ref,
            "pendingAction": (
                suspension.pending_action.to_contract()
                if suspension.pending_action is not None
                else None
            ),
            "contextVersion": _exact_text(
                context_version,
                "agent_checkpoint_context_invalid",
            ),
            "actionBindingDigest": (
                _exact_text(
                    action_binding_digest,
                    "agent_checkpoint_action_binding_invalid",
                )
                if action_binding_digest is not None
                else None
            ),
            "sessionThroughSequence": session_through_sequence,
        }
        return cls.model_validate(
            {
                **body,
                "contentDigest": canonical_digest(body),
            }
        )

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "AgentCheckpoint":
        for value in (
            self.checkpoint_ref,
            self.thread_id,
            self.agent_run_id,
            self.operation_id,
            self.source_tool_name,
            self.source_tool_call_id,
            self.context_version,
        ):
            _exact_text(value, "agent_checkpoint_text_invalid")
        identity = {
            "schema_version": self.schema_version,
            "thread_id": self.thread_id,
            "agent_run_id": self.agent_run_id,
            "operation_id": self.operation_id,
            "checkpoint_kind": self.checkpoint_kind,
            "source_tool_name": self.source_tool_name,
            "source_tool_call_id": self.source_tool_call_id,
        }
        if self.checkpoint_ref != "agent-checkpoint:sha256:" + canonical_digest(
            identity
        ):
            raise ValueError("agent_checkpoint_ref_invalid")
        if self.checkpoint_kind == "waiting_for_task":
            if self.awaited_task_ref is None or self.pending_action is not None:
                raise ValueError("agent_checkpoint_task_shape_invalid")
        elif self.awaited_task_ref is not None or self.pending_action is None:
            raise ValueError("agent_checkpoint_action_shape_invalid")
        elif (
            self.checkpoint_kind == "waiting_for_user"
            and self.pending_action.action_type != "ask_user"
        ) or (
            self.checkpoint_kind == "waiting_for_approval"
            and self.pending_action.action_type != "request_approval"
        ):
            raise ValueError("agent_checkpoint_action_kind_invalid")
        if canonical_digest(self._content_body()) != self.content_digest:
            raise ValueError("agent_checkpoint_digest_invalid")
        return self

    def _content_body(self) -> dict[str, Any]:
        body = self.model_dump(mode="json", by_alias=True)
        body.pop("contentDigest")
        return body

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


@dataclass(frozen=True)
class DurableToolSuspension:
    checkpoint_kind: Literal[
        "waiting_for_task",
        "waiting_for_user",
        "waiting_for_approval",
    ]
    source_tool_name: str
    source_tool_call_id: str
    customer_summary: str
    awaited_task_ref: str | None = None
    pending_action: AgentPendingAction | None = None

    def __post_init__(self) -> None:
        for value, code in (
            (self.source_tool_name, "durable_tool_name_invalid"),
            (self.source_tool_call_id, "durable_tool_call_id_invalid"),
            (self.customer_summary, "durable_tool_customer_summary_invalid"),
        ):
            _exact_text(value, code)
        if self.checkpoint_kind == "waiting_for_task":
            if self.awaited_task_ref is None or self.pending_action is not None:
                raise ValueError("durable_tool_task_suspension_invalid")
            _exact_text(self.awaited_task_ref, "durable_tool_task_ref_invalid")
        elif self.awaited_task_ref is not None or self.pending_action is None:
            raise ValueError("durable_tool_action_suspension_invalid")


class DurableToolBridgeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DurableToolBridge:
    """Finds durable suspension signals in the append-only tool result ledger."""

    def __init__(self, ledger: ThreadItemLedger) -> None:
        self._ledger = ledger

    def suspension_for_operation(
        self,
        *,
        thread_id: str,
        operation_id: str,
    ) -> DurableToolSuspension | None:
        prefix = f"tool-result:{operation_id}:"
        candidates: list[DurableToolSuspension] = []
        for item in self._ledger.list_items(thread_id):
            if item.item_type != "tool_result" or not str(
                item.operation_key or ""
            ).startswith(prefix):
                continue
            parsed = _agent_tool_result(item)
            if parsed is None:
                continue
            suspension = _suspension_from_result(item, parsed)
            if suspension is not None:
                candidates.append(suspension)
        if not candidates:
            return None
        distinct = {
            canonical_digest(_suspension_contract(candidate))
            for candidate in candidates
        }
        if len(distinct) != 1:
            raise DurableToolBridgeError("durable_tool_suspension_ambiguous")
        return candidates[-1]

    def checkpoint_for_operation(
        self,
        *,
        thread_id: str,
        operation_id: str,
    ) -> tuple[ThreadItem, AgentCheckpoint] | None:
        item = self._ledger.get_item_by_operation_key(
            thread_id,
            checkpoint_operation_key(operation_id),
        )
        if item is None:
            return None
        payload = item.payload.get("checkpoint")
        if not isinstance(payload, Mapping):
            raise DurableToolBridgeError("agent_checkpoint_payload_invalid")
        try:
            checkpoint = AgentCheckpoint.model_validate(payload)
        except ValueError as exc:
            raise DurableToolBridgeError("agent_checkpoint_payload_invalid") from exc
        if (
            checkpoint.thread_id != thread_id
            or checkpoint.operation_id != operation_id
            or item.customer_visible
        ):
            raise DurableToolBridgeError("agent_checkpoint_owner_mismatch")
        return item, checkpoint

    def checkpoint_for_task(
        self,
        *,
        thread_id: str,
        task_ref: str,
    ) -> tuple[ThreadItem, AgentCheckpoint] | None:
        _exact_text(task_ref, "agent_checkpoint_task_ref_invalid")
        matches: list[tuple[ThreadItem, AgentCheckpoint]] = []
        for item in self._ledger.list_items(thread_id):
            if not str(item.operation_key or "").startswith("checkpoint:"):
                continue
            raw = item.payload.get("checkpoint")
            if not isinstance(raw, Mapping):
                raise DurableToolBridgeError("agent_checkpoint_payload_invalid")
            try:
                checkpoint = AgentCheckpoint.model_validate(raw)
            except ValueError as exc:
                raise DurableToolBridgeError(
                    "agent_checkpoint_payload_invalid"
                ) from exc
            if checkpoint.thread_id != thread_id or item.customer_visible:
                raise DurableToolBridgeError("agent_checkpoint_owner_mismatch")
            if checkpoint.awaited_task_ref == task_ref:
                matches.append((item, checkpoint))
        if not matches:
            return None
        if len(matches) != 1:
            raise DurableToolBridgeError("agent_checkpoint_task_ambiguous")
        return matches[0]

    def pending_action(
        self,
        *,
        thread_id: str,
        action_ref: str,
    ) -> AgentPendingAction | None:
        for item in reversed(self._ledger.list_items(thread_id)):
            raw = item.payload.get("pending_action")
            if not isinstance(raw, Mapping):
                continue
            try:
                action = AgentPendingAction.model_validate(raw)
            except ValueError as exc:
                raise DurableToolBridgeError("pending_action_payload_invalid") from exc
            if action.action_ref == action_ref:
                return action
        return None


def checkpoint_operation_key(operation_id: str) -> str:
    return "checkpoint:" + _exact_text(
        operation_id,
        "agent_checkpoint_operation_invalid",
    )


def _agent_tool_result(item: ThreadItem) -> AgentToolResult | None:
    sdk_item = item.payload.get("sdk_item")
    if not isinstance(sdk_item, Mapping):
        return None
    output = sdk_item.get("output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError as exc:
            raise DurableToolBridgeError("durable_tool_result_json_invalid") from exc
    if not isinstance(output, Mapping):
        return None
    try:
        return AgentToolResult.model_validate(output)
    except ValueError:
        return None


def _suspension_from_result(
    item: ThreadItem,
    result: AgentToolResult,
) -> DurableToolSuspension | None:
    sdk_item = item.payload.get("sdk_item")
    if not isinstance(sdk_item, Mapping):
        return None
    tool_name = str(sdk_item.get("name") or "")
    call_id = str(sdk_item.get("call_id") or "")
    output = result.output
    if result.status == "succeeded" and isinstance(output, Mapping):
        task_ref = output.get("taskRef") or output.get("task_ref")
        task_state = output.get("taskState") or output.get("task_state")
        if isinstance(task_ref, str) and task_state == "queued":
            return DurableToolSuspension(
                checkpoint_kind="waiting_for_task",
                source_tool_name=tool_name,
                source_tool_call_id=call_id,
                customer_summary=result.customer_summary,
                awaited_task_ref=task_ref,
            )
    if result.status != "needs_input" or not isinstance(output, Mapping):
        return None
    raw_action = output.get("pendingAction") or output.get("pending_action")
    if not isinstance(raw_action, Mapping):
        raise DurableToolBridgeError("pending_action_result_invalid")
    try:
        pending_action = AgentPendingAction.model_validate(raw_action)
    except ValueError as exc:
        raise DurableToolBridgeError("pending_action_result_invalid") from exc
    return DurableToolSuspension(
        checkpoint_kind=(
            "waiting_for_user"
            if pending_action.action_type == "ask_user"
            else "waiting_for_approval"
        ),
        source_tool_name=tool_name,
        source_tool_call_id=call_id,
        customer_summary=result.customer_summary,
        pending_action=pending_action,
    )


def _suspension_contract(value: DurableToolSuspension) -> dict[str, Any]:
    return canonical_value(
        {
            "checkpoint_kind": value.checkpoint_kind,
            "source_tool_name": value.source_tool_name,
            "source_tool_call_id": value.source_tool_call_id,
            "customer_summary": value.customer_summary,
            "awaited_task_ref": value.awaited_task_ref,
            "pending_action": (
                value.pending_action.to_contract()
                if value.pending_action is not None
                else None
            ),
        }
    )


def _exact_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(code)
    return value
