from __future__ import annotations

import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bi_agent.runtime.agent_sdk_contracts import AgentToolResult, WajeAgentTool
from bi_agent.runtime.durable_tool_bridge import (
    AgentPendingAction,
    PendingActionOption,
)
from bi_agent.runtime.evidence_authority import canonical_digest


class AskUserInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    material_decision: str = Field(alias="materialDecision", min_length=1)
    options: list[PendingActionOption] = Field(min_length=2, max_length=3)

    @field_validator("material_decision")
    @classmethod
    def validate_material_decision(cls, value: str) -> str:
        return _exact_text(value, "ask_user_material_decision_invalid")


class RequestApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    action_summary: str = Field(alias="actionSummary", min_length=1)
    side_effect_scope: str = Field(alias="sideEffectScope", min_length=1)

    @field_validator("action_summary", "side_effect_scope")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _exact_text(value, "approval_request_text_invalid")


def agent_interaction_tools(
    *,
    thread_id: str,
    operation_id: str,
    customer_language: Literal["zh-Hans", "en", "match-input-script"] = (
        "match-input-script"
    ),
) -> tuple[WajeAgentTool, WajeAgentTool]:
    """Build typed human-interruption tools for one application turn."""

    _exact_text(thread_id, "agent_interaction_thread_id_invalid")
    _exact_text(operation_id, "agent_interaction_operation_id_invalid")
    if customer_language not in {"zh-Hans", "en", "match-input-script"}:
        raise ValueError("agent_interaction_customer_language_invalid")

    def ask_user(arguments: Mapping[str, Any]) -> AgentToolResult:
        request = AskUserInput.model_validate(arguments)
        _validate_customer_language(
            (
                request.material_decision,
                *(
                    text
                    for option in request.options
                    for text in (option.label, option.description)
                ),
            ),
            customer_language=customer_language,
        )
        action = AgentPendingAction(
            actionRef=_action_ref(
                action_type="ask_user",
                thread_id=thread_id,
                operation_id=operation_id,
                arguments=request.model_dump(mode="json", by_alias=True),
            ),
            actionType="ask_user",
            prompt=request.material_decision,
            options=request.options,
        )
        return AgentToolResult(
            status="needs_input",
            output={"pendingAction": action.to_contract()},
            artifactRefs=[],
            materialRefs=[],
            limitationRefs=[],
            retryability="never",
            customerSummary=request.material_decision,
            technicalDetailRef=None,
        )

    def request_approval(arguments: Mapping[str, Any]) -> AgentToolResult:
        request = RequestApprovalInput.model_validate(arguments)
        _validate_customer_language(
            (request.action_summary, request.side_effect_scope),
            customer_language=customer_language,
        )
        action = AgentPendingAction(
            actionRef=_action_ref(
                action_type="request_approval",
                thread_id=thread_id,
                operation_id=operation_id,
                arguments=request.model_dump(mode="json", by_alias=True),
            ),
            actionType="request_approval",
            prompt=request.action_summary,
            options=[],
            actionSummary=request.action_summary,
            sideEffectScope=request.side_effect_scope,
        )
        return AgentToolResult(
            status="needs_input",
            output={"pendingAction": action.to_contract()},
            artifactRefs=[],
            materialRefs=[],
            limitationRefs=[],
            retryability="never",
            customerSummary=request.action_summary,
            technicalDetailRef=None,
        )

    return (
        WajeAgentTool(
            name="ask_user",
            description=(
                "Pause the current turn for one material business decision. "
                "Provide two or three customer-readable options and mark exactly "
                "one recommended option. "
                + _language_instruction(customer_language)
            ),
            input_model=AskUserInput,
            handler=ask_user,
            execution_mode="suspend_turn",
        ),
        WajeAgentTool(
            name="request_approval",
            description=(
                "Pause before an external write, irreversible action, permission "
                "increase, or material cost. Describe the action and side-effect scope. "
                + _language_instruction(customer_language)
            ),
            input_model=RequestApprovalInput,
            handler=request_approval,
            execution_mode="suspend_turn",
        ),
    )


def _action_ref(
    *,
    action_type: str,
    thread_id: str,
    operation_id: str,
    arguments: Mapping[str, Any],
) -> str:
    return "pending-action:sha256:" + canonical_digest(
        {
            "schema_version": "agent-pending-action.v1",
            "action_type": action_type,
            "thread_id": thread_id,
            "operation_id": operation_id,
            "arguments": dict(arguments),
        }
    )


def _exact_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(code)
    return value


_HAN_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _language_instruction(customer_language: str) -> str:
    if customer_language == "zh-Hans":
        return (
            "Every customer-visible prompt, option label, and option description "
            "must be written in Simplified Chinese."
        )
    if customer_language == "en":
        return (
            "Every customer-visible prompt, option label, and option description "
            "must be written in English."
        )
    return "Match every customer-visible string to the latest user's writing language."


def _validate_customer_language(
    values: tuple[str, ...],
    *,
    customer_language: str,
) -> None:
    if customer_language == "zh-Hans" and any(
        _HAN_TEXT.search(value) is None for value in values
    ):
        raise ValueError("agent_interaction_customer_language_mismatch")
