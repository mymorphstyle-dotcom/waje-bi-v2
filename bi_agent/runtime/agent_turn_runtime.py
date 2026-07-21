from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bi_agent.runtime.agent_context import AgentContextAssembler, AgentContextSnapshot
from bi_agent.runtime.agent_sdk_contracts import (
    AgentSdkAdapterError,
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentTool,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.llm_client import LLMProviderError
from bi_agent.runtime.postgres_agent_session import PostgresAgentSession
from bi_agent.runtime.thread_item_ledger import (
    NewThreadItem,
    ThreadHead,
    ThreadHeadTarget,
    ThreadItem,
    ThreadItemLedger,
    ThreadLedgerError,
)


class AgentFinalOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    answer_markdown: str = Field(alias="answerMarkdown", min_length=1)
    material_refs: list[str] = Field(alias="materialRefs", default_factory=list)
    limitation_refs: list[str] = Field(
        alias="limitationRefs",
        default_factory=list,
    )

    @field_validator("answer_markdown")
    @classmethod
    def validate_answer_markdown(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("agent_answer_markdown_invalid")
        return value

    @field_validator("material_refs", "limitation_refs")
    @classmethod
    def validate_refs(cls, values: list[str]) -> list[str]:
        if any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in values
        ) or len(set(values)) != len(values):
            raise ValueError("agent_final_refs_invalid")
        return values

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class AgentLoopAdapter(Protocol):
    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult: ...


class AgentTurnError(RuntimeError):
    def __init__(self, code: str, *, retryability: str = "not_retryable") -> None:
        super().__init__(code)
        self.code = code
        self.retryability = retryability


@dataclass(frozen=True)
class AgentTurnRequest:
    thread_id: str
    run_id: str
    operation_id: str
    user_item_id: str
    user_message: str
    expected_state_version: int
    instructions: str
    tools: Sequence[WajeAgentTool] = ()
    agent_name: str = "WAJE General Agent"
    active_topic_ref: str | None = None
    permission_scope: Mapping[str, Any] = field(default_factory=dict)
    relevant_materials: Sequence[Mapping[str, Any]] = ()
    max_turns: int = 10

    def __post_init__(self) -> None:
        for value, code in (
            (self.thread_id, "agent_turn_thread_id_missing"),
            (self.run_id, "agent_turn_run_id_missing"),
            (self.operation_id, "agent_turn_operation_id_missing"),
            (self.user_item_id, "agent_turn_user_item_id_missing"),
            (self.user_message, "agent_turn_user_message_missing"),
            (self.instructions, "agent_turn_instructions_missing"),
            (self.agent_name, "agent_turn_agent_name_missing"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(code)
        if (
            isinstance(self.expected_state_version, bool)
            or self.expected_state_version < 0
        ):
            raise ValueError("agent_turn_expected_state_version_invalid")
        if isinstance(self.max_turns, bool) or self.max_turns < 1:
            raise ValueError("agent_turn_max_turns_invalid")


@dataclass(frozen=True)
class AgentTurnResult:
    thread_id: str
    run_id: str
    operation_id: str
    status: str
    final_output: Mapping[str, Any] | None
    assistant_item: ThreadItem
    terminal_item: ThreadItem
    thread_head: ThreadHead
    context_version: str
    model_turns: int
    replayed: bool
    error_code: str | None = None

    def customer_projection(self) -> dict[str, Any]:
        return {
            "message": {
                "role": "assistant",
                "text": self.assistant_item.text,
                "createdAt": self.assistant_item.created_at,
            },
            "status": self.status,
            "transport": {
                "stateVersion": str(self.thread_head.state_version),
                "latestItemSequence": self.thread_head.latest_item_sequence,
            },
        }


class AgentTurnRuntime:
    """Supervises one durable WAJE application turn around the SDK Runner."""

    def __init__(
        self,
        *,
        ledger: ThreadItemLedger,
        context_assembler: AgentContextAssembler,
        adapter: AgentLoopAdapter,
        session_history_limit: int = 40,
    ) -> None:
        if session_history_limit < 1:
            raise ValueError("agent_turn_session_limit_invalid")
        self._ledger = ledger
        self._context_assembler = context_assembler
        self._adapter = adapter
        self._session_history_limit = session_history_limit

    async def run(self, request: AgentTurnRequest) -> AgentTurnResult:
        replayed_terminal = self._ledger.get_item_by_operation_key(
            request.thread_id,
            _terminal_operation_key(request.operation_id),
        )
        if replayed_terminal is not None:
            return self._replayed_result(request, replayed_terminal)

        starting_head = self._ledger.get_head(request.thread_id)
        if (
            starting_head.customer_state == "working"
            and starting_head.active_task_id not in {None, request.run_id}
        ):
            raise AgentTurnError(
                "thread_active_task_conflict", retryability="retryable"
            )
        user_item = NewThreadItem(
            item_id=request.user_item_id,
            item_type="user_message",
            role="user",
            text=request.user_message,
            operation_key=_user_operation_key(request.operation_id),
            customer_visible=True,
            payload={
                "sdk_item": {"role": "user", "content": request.user_message},
                "sdk_replay": True,
                "run_id": request.run_id,
            },
        )
        accepted = self._ledger.append_items(
            request.thread_id,
            [user_item],
            expected_state_version=request.expected_state_version,
            head_target=ThreadHeadTarget(
                active_task_id=request.run_id,
                active_topic_ref=(
                    request.active_topic_ref or starting_head.active_topic_ref
                ),
                pending_action_ref=None,
                customer_state="working",
            ),
        )
        persisted_user = accepted.items[0]
        snapshot = self._context_assembler.assemble(
            request.thread_id,
            available_tools=_tool_descriptors(request.tools),
            permission_scope=request.permission_scope,
            relevant_materials=request.relevant_materials,
        )
        session = PostgresAgentSession(
            ledger=self._ledger,
            thread_id=request.thread_id,
            operation_id=request.operation_id,
            input_item_id=persisted_user.item_id,
            input_text=request.user_message,
            replay_through_sequence=persisted_user.sequence - 1,
            history_limit=self._session_history_limit,
        )
        run_request = WajeAgentRunRequest(
            run_id=request.run_id,
            agent_name=request.agent_name,
            instructions=_runtime_instructions(request.instructions, snapshot),
            input_text=request.user_message,
            tools=request.tools,
            output_type=AgentFinalOutput,
            max_turns=request.max_turns,
            trace_metadata={
                "waje_thread_id": request.thread_id,
                "waje_run_id": request.run_id,
                "waje_topic_id": snapshot.thread_head.active_topic_ref or "",
                "waje_context_version": snapshot.context_version,
            },
            session=session,
            event_sink=session,
        )
        try:
            sdk_result = await self._adapter.run(run_request)
            final_output = AgentFinalOutput.model_validate(sdk_result.final_output)
            refreshed = self._context_assembler.assemble(
                request.thread_id,
                available_tools=_tool_descriptors(request.tools),
                permission_scope=request.permission_scope,
                relevant_materials=request.relevant_materials,
            )
            _validate_source_closure(final_output, refreshed)
            return self._commit_success(
                request,
                snapshot=snapshot,
                final_output=final_output,
                sdk_result=sdk_result,
            )
        except Exception as exc:
            return self._commit_failure(request, snapshot=snapshot, error=exc)

    def _commit_success(
        self,
        request: AgentTurnRequest,
        *,
        snapshot: AgentContextSnapshot,
        final_output: AgentFinalOutput,
        sdk_result: WajeAgentRunResult,
    ) -> AgentTurnResult:
        final_contract = final_output.to_contract()
        status = (
            "completed_with_limits" if final_output.limitation_refs else "completed"
        )
        assistant, terminal = _terminal_items(
            request,
            answer_text=final_output.answer_markdown,
            status=status,
            final_output=final_contract,
            context_version=snapshot.context_version,
            model_turns=sdk_result.model_turns,
            usage=sdk_result.usage,
            error_code=None,
        )
        current = self._ledger.get_head(request.thread_id)
        committed = self._ledger.append_items(
            request.thread_id,
            [assistant, terminal],
            expected_state_version=current.state_version,
            head_target=ThreadHeadTarget(
                active_task_id=None,
                active_topic_ref=current.active_topic_ref,
                pending_action_ref=None,
                customer_state=status,
            ),
        )
        return AgentTurnResult(
            thread_id=request.thread_id,
            run_id=request.run_id,
            operation_id=request.operation_id,
            status=status,
            final_output=final_contract,
            assistant_item=committed.items[0],
            terminal_item=committed.items[1],
            thread_head=committed.head,
            context_version=snapshot.context_version,
            model_turns=sdk_result.model_turns,
            replayed=False,
        )

    def _commit_failure(
        self,
        request: AgentTurnRequest,
        *,
        snapshot: AgentContextSnapshot,
        error: Exception,
    ) -> AgentTurnResult:
        error_code, retryability = _typed_error(error)
        answer = (
            "当前请求暂时未能完成，请稍后重试。"
            if retryability == "retryable"
            else "当前请求未能完成，请调整请求后重试；若问题持续，请联系支持。"
        )
        assistant, terminal = _terminal_items(
            request,
            answer_text=answer,
            status="failed",
            final_output=None,
            context_version=snapshot.context_version,
            model_turns=0,
            usage={},
            error_code=error_code,
        )
        current = self._ledger.get_head(request.thread_id)
        committed = self._ledger.append_items(
            request.thread_id,
            [assistant, terminal],
            expected_state_version=current.state_version,
            head_target=ThreadHeadTarget(
                active_task_id=None,
                active_topic_ref=current.active_topic_ref,
                pending_action_ref=None,
                customer_state="failed",
            ),
        )
        return AgentTurnResult(
            thread_id=request.thread_id,
            run_id=request.run_id,
            operation_id=request.operation_id,
            status="failed",
            final_output=None,
            assistant_item=committed.items[0],
            terminal_item=committed.items[1],
            thread_head=committed.head,
            context_version=snapshot.context_version,
            model_turns=0,
            replayed=False,
            error_code=error_code,
        )

    def _replayed_result(
        self,
        request: AgentTurnRequest,
        terminal: ThreadItem,
    ) -> AgentTurnResult:
        payload = terminal.payload
        if str(payload.get("run_id") or "") != request.run_id:
            raise AgentTurnError("agent_turn_replay_run_conflict")
        assistant = self._ledger.get_item_by_operation_key(
            request.thread_id,
            _assistant_operation_key(request.operation_id),
        )
        if assistant is None:
            raise AgentTurnError("agent_turn_replay_assistant_missing")
        status = str(payload.get("status") or "")
        if status not in {"completed", "completed_with_limits", "failed"}:
            raise AgentTurnError("agent_turn_replay_terminal_invalid")
        final_output = payload.get("final_output")
        return AgentTurnResult(
            thread_id=request.thread_id,
            run_id=request.run_id,
            operation_id=request.operation_id,
            status=status,
            final_output=(
                dict(final_output) if isinstance(final_output, Mapping) else None
            ),
            assistant_item=assistant,
            terminal_item=terminal,
            thread_head=self._ledger.get_head(request.thread_id),
            context_version=str(payload.get("context_version") or ""),
            model_turns=int(payload.get("model_turns") or 0),
            replayed=True,
            error_code=(
                str(payload.get("error_code"))
                if isinstance(payload.get("error_code"), str)
                else None
            ),
        )


def _terminal_items(
    request: AgentTurnRequest,
    *,
    answer_text: str,
    status: str,
    final_output: Mapping[str, Any] | None,
    context_version: str,
    model_turns: int,
    usage: Mapping[str, int],
    error_code: str | None,
) -> tuple[NewThreadItem, NewThreadItem]:
    assistant_payload = {
        "sdk_replay": False,
        "run_id": request.run_id,
        "final_output": dict(final_output) if final_output is not None else None,
    }
    assistant_digest = canonical_digest(
        {
            "operation_id": request.operation_id,
            "kind": "assistant",
            "payload": assistant_payload,
            "text": answer_text,
        }
    )
    terminal_payload = canonical_value(
        {
            "run_id": request.run_id,
            "status": status,
            "final_output": dict(final_output) if final_output is not None else None,
            "context_version": context_version,
            "model_turns": model_turns,
            "usage": dict(usage),
            "error_code": error_code,
        }
    )
    terminal_digest = canonical_digest(
        {
            "operation_id": request.operation_id,
            "kind": "terminal",
            "payload": terminal_payload,
        }
    )
    return (
        NewThreadItem(
            item_id=f"assistant-{assistant_digest[:24]}",
            item_type="assistant_message",
            role="assistant",
            text=answer_text,
            operation_key=_assistant_operation_key(request.operation_id),
            customer_visible=True,
            payload=assistant_payload,
        ),
        NewThreadItem(
            item_id=f"terminal-{terminal_digest[:24]}",
            item_type="task_terminal",
            role="system",
            text="",
            operation_key=_terminal_operation_key(request.operation_id),
            customer_visible=False,
            payload=terminal_payload,
        ),
    )


def _runtime_instructions(
    base_instructions: str,
    snapshot: AgentContextSnapshot,
) -> str:
    return (
        f"{base_instructions.rstrip()}\n\n"
        "Use the WAJE context snapshot below as context only. Current authority "
        "comes from thread_head, active_task, accepted_decisions, pending_actions, "
        "and referenced artifacts. Do not expose internal identifiers unless the "
        "customer supplied them. Return every factual material reference in "
        "materialRefs and every material limitation reference in limitationRefs.\n"
        f"WAJE_CONTEXT_JSON={AgentContextAssembler.model_context(snapshot)}"
    )


def _validate_source_closure(
    output: AgentFinalOutput,
    snapshot: AgentContextSnapshot,
) -> None:
    known = set(snapshot.material_refs)
    known.update(_tool_material_refs(snapshot.recent_items))
    unknown = (set(output.material_refs) | set(output.limitation_refs)) - known
    if unknown:
        raise AgentTurnError("agent_final_material_ref_unknown")


def _tool_material_refs(items: Sequence[ThreadItem]) -> frozenset[str]:
    refs: set[str] = set()
    for item in items:
        if item.item_type != "tool_result":
            continue
        sdk_item = item.payload.get("sdk_item")
        if not isinstance(sdk_item, Mapping):
            continue
        output = sdk_item.get("output")
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                continue
        _collect_refs(output, refs)
    return frozenset(refs)


def _collect_refs(value: Any, refs: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {
                "artifactRefs",
                "artifact_refs",
                "materialRefs",
                "material_refs",
                "limitationRefs",
                "limitation_refs",
            } and isinstance(child, list):
                refs.update(item for item in child if isinstance(item, str) and item)
            else:
                _collect_refs(child, refs)
    elif isinstance(value, list):
        for child in value:
            _collect_refs(child, refs)


def _typed_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, LLMProviderError):
        return error.kind, error.retryability
    if isinstance(error, (AgentSdkAdapterError, AgentTurnError)):
        return error.code, error.retryability
    if isinstance(error, ThreadLedgerError):
        return error.code, "retryable"
    return "agent_turn_unexpected_failure", "not_retryable"


def _tool_descriptors(
    tools: Sequence[WajeAgentTool],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_model.model_json_schema(),
        }
        for tool in tools
    )


def _user_operation_key(operation_id: str) -> str:
    return f"user:{operation_id}"


def _assistant_operation_key(operation_id: str) -> str:
    return f"assistant:{operation_id}"


def _terminal_operation_key(operation_id: str) -> str:
    return f"terminal:{operation_id}"
