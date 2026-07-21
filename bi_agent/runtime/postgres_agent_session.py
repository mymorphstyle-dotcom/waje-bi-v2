from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from bi_agent.runtime.agent_sdk_contracts import AgentSessionError
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.thread_item_ledger import (
    NewThreadItem,
    ThreadItem,
    ThreadItemLedger,
)


class PostgresAgentSession:
    """Agents SDK Session protocol backed directly by the WAJE ThreadItemLedger.

    The class is intentionally SDK-neutral and relies on Python structural typing.
    `pop_item` and `clear_session` are rejected because the underlying ledger is
    append-only.
    """

    session_settings = None

    def __init__(
        self,
        *,
        ledger: ThreadItemLedger,
        thread_id: str,
        operation_id: str,
        input_item_id: str,
        input_text: str,
        replay_through_sequence: int,
        history_limit: int = 40,
    ) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (thread_id, operation_id, input_item_id, input_text)
        ):
            raise ValueError("agent_session_identity_invalid")
        if replay_through_sequence < 0:
            raise ValueError("agent_session_replay_sequence_invalid")
        if isinstance(history_limit, bool) or history_limit < 1:
            raise ValueError("agent_session_history_limit_invalid")
        self._ledger = ledger
        self.session_id = thread_id
        self._operation_id = operation_id
        self._input_item_id = input_item_id
        self._input_text = input_text
        self._replay_through_sequence = replay_through_sequence
        self._history_limit = history_limit

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        resolved_limit = self._history_limit if limit is None else limit
        if isinstance(resolved_limit, bool) or resolved_limit < 1:
            raise AgentSessionError("agent_session_limit_invalid")
        ledger_items = await asyncio.to_thread(
            self._ledger.list_items,
            self.session_id,
            limit=resolved_limit,
            through_sequence=self._replay_through_sequence,
        )
        replay_items: list[dict[str, Any]] = []
        for item in ledger_items:
            sdk_item = _sdk_item_from_thread_item(item)
            if sdk_item is not None:
                replay_items.append(sdk_item)
        return replay_items

    async def add_items(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        projected: list[NewThreadItem] = []
        for index, raw_item in enumerate(items):
            normalized = canonical_value(raw_item)
            if not isinstance(normalized, dict):
                raise AgentSessionError("agent_session_item_invalid")
            if _is_current_input(normalized, self._input_text):
                continue
            projected_item = _thread_item_from_sdk_item(
                normalized,
                operation_id=self._operation_id,
                index=index,
            )
            if (
                projected_item is not None
                and projected_item.operation_key is not None
                and await asyncio.to_thread(
                    self._ledger.get_item_by_operation_key,
                    self.session_id,
                    projected_item.operation_key,
                )
                is not None
            ):
                continue
            if projected_item is not None:
                projected.append(projected_item)
        if not projected:
            return
        await asyncio.to_thread(
            self._ledger.append_items,
            self.session_id,
            projected,
        )

    async def pop_item(self) -> dict[str, Any] | None:
        raise AgentSessionError("agent_session_append_only")

    async def clear_session(self) -> None:
        raise AgentSessionError("agent_session_append_only")

    async def record_tool_call(
        self,
        *,
        tool_name: str,
        call_id: str,
        arguments: Mapping[str, Any],
    ) -> None:
        sdk_item = {
            "type": "function_call",
            "name": tool_name,
            "call_id": call_id,
            "arguments": json.dumps(
                canonical_value(arguments),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        await self._append_tool_event(
            sdk_item,
            item_type="tool_call",
            operation_key=f"tool-call:{self._operation_id}:{call_id}",
        )

    async def record_tool_result(
        self,
        *,
        tool_name: str,
        call_id: str,
        result: Any,
        succeeded: bool,
    ) -> None:
        normalized_result = canonical_value(result)
        sdk_item = {
            "type": "function_call_output",
            "name": tool_name,
            "call_id": call_id,
            "output": json.dumps(
                normalized_result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        await self._append_tool_event(
            sdk_item,
            item_type="tool_result",
            operation_key=f"tool-result:{self._operation_id}:{call_id}",
            extra_payload={"succeeded": succeeded},
        )

    async def _append_tool_event(
        self,
        sdk_item: Mapping[str, Any],
        *,
        item_type: str,
        operation_key: str,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> None:
        existing = await asyncio.to_thread(
            self._ledger.get_item_by_operation_key,
            self.session_id,
            operation_key,
        )
        if existing is not None:
            persisted = existing.payload.get("sdk_item")
            if not isinstance(persisted, Mapping) or (
                str(persisted.get("call_id") or "")
                != str(sdk_item.get("call_id") or "")
            ):
                raise AgentSessionError("agent_tool_event_replay_conflict")
            return
        digest = canonical_digest(
            {
                "operation_id": self._operation_id,
                "operation_key": operation_key,
                "sdk_item": dict(sdk_item),
            }
        )
        await asyncio.to_thread(
            self._ledger.append_items,
            self.session_id,
            [
                NewThreadItem(
                    item_id=f"agent-item-{digest[:24]}",
                    item_type=item_type,
                    role="tool",
                    text="",
                    operation_key=operation_key,
                    customer_visible=False,
                    payload={
                        "sdk_item": dict(sdk_item),
                        "sdk_replay": True,
                        **dict(extra_payload or {}),
                    },
                )
            ],
        )


def _thread_item_from_sdk_item(
    item: Mapping[str, Any],
    *,
    operation_id: str,
    index: int,
) -> NewThreadItem | None:
    item_type = str(item.get("type") or "message")
    role = str(item.get("role") or "")
    if item_type == "reasoning":
        # Hidden reasoning is trace material and never becomes business authority.
        return None
    if item_type == "message" and role in {"user", "assistant"}:
        projected_type = f"{role}_message"
        text = _message_text(item)
        customer_visible = role == "user"
        operation_key = (
            f"agent:{operation_id}:model-assistant"
            if role == "assistant"
            else f"agent:{operation_id}:user:{index}"
        )
        projected_role = role
    elif item_type in {
        "function_call",
        "custom_tool_call",
        "computer_call",
        "shell_call",
        "apply_patch_call",
    }:
        projected_type = "tool_call"
        text = ""
        customer_visible = False
        call_id = str(item.get("call_id") or item.get("id") or "")
        if not call_id:
            raise AgentSessionError("agent_tool_call_id_missing")
        operation_key = f"tool-call:{operation_id}:{call_id}"
        projected_role = "tool"
    elif item_type in {
        "function_call_output",
        "custom_tool_call_output",
        "computer_call_output",
        "shell_call_output",
        "apply_patch_call_output",
    }:
        projected_type = "tool_result"
        text = ""
        customer_visible = False
        call_id = str(item.get("call_id") or "")
        if not call_id:
            raise AgentSessionError("agent_tool_result_call_id_missing")
        operation_key = f"tool-result:{operation_id}:{call_id}"
        projected_role = "tool"
    else:
        raise AgentSessionError("agent_session_item_type_unsupported")

    fingerprint = canonical_digest(
        {"operation_id": operation_id, "index": index, "sdk_item": dict(item)}
    )
    return NewThreadItem(
        item_id=f"agent-item-{fingerprint[:24]}",
        item_type=projected_type,
        role=projected_role,
        text=text,
        operation_key=operation_key,
        customer_visible=customer_visible,
        payload={"sdk_item": dict(item), "sdk_replay": True},
    )


def _sdk_item_from_thread_item(item: ThreadItem) -> dict[str, Any] | None:
    if item.payload.get("sdk_replay") is False:
        return None
    sdk_item = item.payload.get("sdk_item")
    if isinstance(sdk_item, Mapping):
        return dict(sdk_item)
    if item.item_type in {"message", "user_message", "assistant_message"} and (
        item.role in {"user", "assistant"}
    ):
        return {"role": item.role, "content": item.text}
    return None


def _is_current_input(item: Mapping[str, Any], input_text: str) -> bool:
    return (
        str(item.get("type") or "message") == "message"
        and item.get("role") == "user"
        and _message_text(item) == input_text
    )


def _message_text(item: Mapping[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return _customer_answer_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, Mapping):
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        return _customer_answer_text("".join(parts))
    return ""


def _customer_answer_text(value: str) -> str:
    stripped = value.strip()
    if not stripped.startswith("{"):
        return value
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, Mapping) and isinstance(parsed.get("answerMarkdown"), str):
        return str(parsed["answerMarkdown"])
    return value
