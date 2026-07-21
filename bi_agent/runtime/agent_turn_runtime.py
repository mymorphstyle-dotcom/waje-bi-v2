from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bi_agent.runtime.agent_context import AgentContextAssembler, AgentContextSnapshot
from bi_agent.runtime.agent_sdk_contracts import (
    AgentSdkAdapterError,
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentTool,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.durable_tool_bridge import (
    AgentCheckpoint,
    AgentPendingAction,
    DurableToolBridge,
    DurableToolBridgeError,
    DurableToolSuspension,
    PendingActionResolution,
    checkpoint_operation_key,
)
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


class AgentTaskCompletionLoader(Protocol):
    def load_task_completion(
        self,
        *,
        thread_id: str,
        task_ref: str,
    ) -> "AgentTaskCompletion | None": ...


class AgentTurnError(RuntimeError):
    def __init__(self, code: str, *, retryability: str = "not_retryable") -> None:
        super().__init__(code)
        self.code = code
        self.retryability = retryability


class AgentTaskCompletion(BaseModel):
    """Customer-safe terminal projection loaded from durable task authority."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_ref: str = Field(alias="taskRef", min_length=1)
    status: str
    customer_summary: str = Field(alias="customerSummary", min_length=1)
    artifact_refs: list[str] = Field(alias="artifactRefs", default_factory=list)
    material_refs: list[str] = Field(alias="materialRefs", default_factory=list)
    limitation_refs: list[str] = Field(alias="limitationRefs", default_factory=list)
    relevant_materials: list[dict[str, Any]] = Field(
        alias="relevantMaterials",
        default_factory=list,
    )

    @field_validator("task_ref", "customer_summary")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("agent_task_completion_text_invalid")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"completed", "completed_with_limits", "failed"}:
            raise ValueError("agent_task_completion_status_invalid")
        return value

    @field_validator("artifact_refs", "material_refs", "limitation_refs")
    @classmethod
    def validate_refs(cls, values: list[str]) -> list[str]:
        if any(
            not isinstance(value, str) or not value or value != value.strip()
            for value in values
        ) or len(values) != len(set(values)):
            raise ValueError("agent_task_completion_refs_invalid")
        return values

    @model_validator(mode="after")
    def validate_completion_shape(self) -> "AgentTaskCompletion":
        normalized = canonical_value(self.relevant_materials)
        if not isinstance(normalized, list) or any(
            not isinstance(item, dict) for item in normalized
        ):
            raise ValueError("agent_task_completion_materials_invalid")
        self.relevant_materials[:] = normalized
        if self.status == "completed_with_limits" and not self.limitation_refs:
            raise ValueError("agent_task_completion_limit_missing")
        if self.status == "failed" and (
            self.artifact_refs or self.material_refs or self.limitation_refs
        ):
            raise ValueError("agent_task_failure_refs_invalid")
        if self.status != "failed" and not (
            self.artifact_refs or self.material_refs
        ):
            raise ValueError("agent_task_completion_refs_missing")
        return self

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def context_materials(self) -> tuple[Mapping[str, Any], ...]:
        values = [dict(item) for item in self.relevant_materials]
        represented = {
            str(item.get("material_ref") or item.get("ref") or "")
            for item in values
        }
        for ref in (*self.artifact_refs, *self.material_refs, *self.limitation_refs):
            if ref not in represented:
                values.append(
                    {
                        "material_ref": ref,
                        "source": "durable_task_completion",
                    }
                )
        return tuple(values)


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
    pending_action_resolution: PendingActionResolution | None = None
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
        if self.pending_action_resolution is not None and type(
            self.pending_action_resolution
        ) is not PendingActionResolution:
            raise TypeError("agent_turn_pending_action_resolution_invalid")


@dataclass(frozen=True)
class AgentTaskResumeRequest:
    thread_id: str
    run_id: str
    operation_id: str
    instructions: str
    completion: AgentTaskCompletion
    tools: Sequence[WajeAgentTool] = ()
    agent_name: str = "WAJE General Agent"
    permission_scope: Mapping[str, Any] = field(default_factory=dict)
    max_turns: int = 10

    def __post_init__(self) -> None:
        for value, code in (
            (self.thread_id, "agent_resume_thread_id_missing"),
            (self.run_id, "agent_resume_run_id_missing"),
            (self.operation_id, "agent_resume_operation_id_missing"),
            (self.instructions, "agent_resume_instructions_missing"),
            (self.agent_name, "agent_resume_agent_name_missing"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(code)
        if type(self.completion) is not AgentTaskCompletion:
            raise TypeError("agent_resume_completion_invalid")
        if isinstance(self.max_turns, bool) or self.max_turns < 1:
            raise ValueError("agent_resume_max_turns_invalid")
        if any(tool.execution_mode == "suspend_turn" for tool in self.tools):
            raise ValueError("agent_resume_nested_suspension_unsupported")


@dataclass(frozen=True)
class AgentTurnResult:
    thread_id: str
    run_id: str
    operation_id: str
    status: str
    final_output: Mapping[str, Any] | None
    assistant_item: ThreadItem
    terminal_item: ThreadItem | None
    checkpoint_item: ThreadItem | None
    thread_head: ThreadHead
    context_version: str
    model_turns: int
    replayed: bool
    error_code: str | None = None

    def customer_projection(self) -> dict[str, Any]:
        projection: dict[str, Any] = {
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
        pending_action = self.assistant_item.payload.get("pending_action")
        if isinstance(pending_action, Mapping):
            projection["pendingAction"] = canonical_value(pending_action)
        return projection


class AgentTurnRuntime:
    """Supervises one durable WAJE application turn around the SDK Runner."""

    def __init__(
        self,
        *,
        ledger: ThreadItemLedger,
        context_assembler: AgentContextAssembler,
        adapter: AgentLoopAdapter,
        durable_tool_bridge: DurableToolBridge | None = None,
        session_history_limit: int = 40,
    ) -> None:
        if session_history_limit < 1:
            raise ValueError("agent_turn_session_limit_invalid")
        self._ledger = ledger
        self._context_assembler = context_assembler
        self._adapter = adapter
        self._durable_tool_bridge = durable_tool_bridge or DurableToolBridge(ledger)
        self._session_history_limit = session_history_limit

    async def run(self, request: AgentTurnRequest) -> AgentTurnResult:
        replayed_terminal = self._ledger.get_item_by_operation_key(
            request.thread_id,
            _terminal_operation_key(request.operation_id),
        )
        if replayed_terminal is not None:
            return self._replayed_result(request, replayed_terminal)
        replayed_checkpoint = self._durable_tool_bridge.checkpoint_for_operation(
            thread_id=request.thread_id,
            operation_id=request.operation_id,
        )
        if replayed_checkpoint is not None:
            return self._replayed_suspension(request, *replayed_checkpoint)
        recovered_suspension = self._durable_tool_bridge.suspension_for_operation(
            thread_id=request.thread_id,
            operation_id=request.operation_id,
        )
        if recovered_suspension is not None:
            recovered_snapshot = self._context_assembler.assemble(
                request.thread_id,
                available_tools=_tool_descriptors(request.tools),
                permission_scope=request.permission_scope,
                relevant_materials=request.relevant_materials,
            )
            return self._commit_suspension(
                request,
                snapshot=recovered_snapshot,
                suspension=recovered_suspension,
                model_turns=0,
            )

        starting_head = self._ledger.get_head(request.thread_id)
        existing_user = self._ledger.get_item_by_operation_key(
            request.thread_id,
            _user_operation_key(request.operation_id),
        )
        pending_action = (
            _replayed_pending_action(existing_user, request)
            if existing_user is not None
            else _resolved_pending_action(
                bridge=self._durable_tool_bridge,
                thread_id=request.thread_id,
                customer_state=starting_head.customer_state,
                pending_action_ref=starting_head.pending_action_ref,
                resolution=request.pending_action_resolution,
            )
        )
        if (
            starting_head.customer_state == "working"
            and starting_head.active_task_id not in {None, request.run_id}
        ):
            raise AgentTurnError(
                "thread_active_task_conflict", retryability="retryable"
            )
        user_payload: dict[str, Any] = {
            "sdk_item": {"role": "user", "content": request.user_message},
            "sdk_replay": True,
            "run_id": request.run_id,
        }
        if request.pending_action_resolution is not None:
            if pending_action is None:
                raise AgentTurnError("pending_action_resolution_missing")
            user_payload.update(
                {
                    "pending_action_resolution": (
                        request.pending_action_resolution.model_dump(
                            mode="json",
                            by_alias=True,
                        )
                    ),
                    "resolved_pending_action": pending_action.to_contract(),
                }
            )
        user_item = NewThreadItem(
            item_id=request.user_item_id,
            item_type="user_message",
            role="user",
            text=request.user_message,
            operation_key=_user_operation_key(request.operation_id),
            customer_visible=True,
            payload=user_payload,
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
        except Exception as exc:
            suspension = self._durable_tool_bridge.suspension_for_operation(
                thread_id=request.thread_id,
                operation_id=request.operation_id,
            )
            if suspension is not None:
                refreshed = self._context_assembler.assemble(
                    request.thread_id,
                    available_tools=_tool_descriptors(request.tools),
                    permission_scope=request.permission_scope,
                    relevant_materials=request.relevant_materials,
                )
                return self._commit_suspension(
                    request,
                    snapshot=refreshed,
                    suspension=suspension,
                    model_turns=0,
                )
            return self._commit_failure(request, snapshot=snapshot, error=exc)
        refreshed = self._context_assembler.assemble(
            request.thread_id,
            available_tools=_tool_descriptors(request.tools),
            permission_scope=request.permission_scope,
            relevant_materials=request.relevant_materials,
        )
        suspension = self._durable_tool_bridge.suspension_for_operation(
            thread_id=request.thread_id,
            operation_id=request.operation_id,
        )
        if suspension is not None:
            return self._commit_suspension(
                request,
                snapshot=refreshed,
                suspension=suspension,
                model_turns=sdk_result.model_turns,
            )
        try:
            final_output = AgentFinalOutput.model_validate(sdk_result.final_output)
            _validate_source_closure(final_output, refreshed)
        except Exception as exc:
            return self._commit_failure(request, snapshot=snapshot, error=exc)
        return self._commit_success(
            request,
            snapshot=snapshot,
            final_output=final_output,
            sdk_result=sdk_result,
        )

    async def resume_task(
        self,
        request: AgentTaskResumeRequest,
    ) -> AgentTurnResult:
        replayed_terminal = self._ledger.get_item_by_operation_key(
            request.thread_id,
            _terminal_operation_key(request.operation_id),
        )
        if replayed_terminal is not None:
            return self._replayed_result(request, replayed_terminal)
        persisted = self._durable_tool_bridge.checkpoint_for_operation(
            thread_id=request.thread_id,
            operation_id=request.operation_id,
        )
        if persisted is None:
            raise AgentTurnError("agent_task_checkpoint_missing")
        checkpoint_item, checkpoint = persisted
        if (
            checkpoint.agent_run_id != request.run_id
            or checkpoint.checkpoint_kind != "waiting_for_task"
            or checkpoint.awaited_task_ref != request.completion.task_ref
        ):
            raise AgentTurnError("agent_task_checkpoint_mismatch")
        current = self._ledger.get_head(request.thread_id)
        if current.active_task_id != request.completion.task_ref:
            raise AgentTurnError(
                "agent_task_completion_head_conflict",
                retryability="retryable",
            )
        if request.completion.status == "failed":
            return self._commit_task_failure(
                request,
                checkpoint=checkpoint,
            )

        completion_materials = request.completion.context_materials()
        snapshot = self._context_assembler.assemble(
            request.thread_id,
            available_tools=_tool_descriptors(request.tools),
            permission_scope=request.permission_scope,
            relevant_materials=completion_materials,
        )
        input_text = _task_completion_input(request.completion)
        session = PostgresAgentSession(
            ledger=self._ledger,
            thread_id=request.thread_id,
            operation_id=request.operation_id,
            input_item_id=checkpoint_item.item_id,
            input_text=input_text,
            replay_through_sequence=current.latest_item_sequence,
            history_limit=self._session_history_limit,
        )
        run_request = WajeAgentRunRequest(
            run_id=request.run_id,
            agent_name=request.agent_name,
            instructions=_runtime_instructions(request.instructions, snapshot),
            input_text=input_text,
            tools=request.tools,
            output_type=AgentFinalOutput,
            max_turns=request.max_turns,
            trace_metadata={
                "waje_thread_id": request.thread_id,
                "waje_run_id": request.run_id,
                "waje_task_id": request.completion.task_ref,
                "waje_topic_id": snapshot.thread_head.active_topic_ref or "",
                "waje_context_version": snapshot.context_version,
                "waje_resume_checkpoint_ref": checkpoint.checkpoint_ref,
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
                relevant_materials=completion_materials,
            )
            _validate_source_closure(final_output, refreshed)
            _validate_completion_closure(request.completion, final_output)
        except Exception as exc:
            return self._commit_failure(request, snapshot=snapshot, error=exc)
        return self._commit_success(
            request,
            snapshot=refreshed,
            final_output=final_output,
            sdk_result=sdk_result,
        )

    async def resume_ready_task(
        self,
        *,
        thread_id: str,
        task_ref: str,
        completion_loader: AgentTaskCompletionLoader,
        instructions: str,
        tools: Sequence[WajeAgentTool] = (),
        agent_name: str = "WAJE General Agent",
        permission_scope: Mapping[str, Any] | None = None,
        max_turns: int = 10,
    ) -> AgentTurnResult | None:
        persisted = self._durable_tool_bridge.checkpoint_for_task(
            thread_id=thread_id,
            task_ref=task_ref,
        )
        if persisted is None:
            raise AgentTurnError("agent_task_checkpoint_missing")
        _, checkpoint = persisted
        completion = completion_loader.load_task_completion(
            thread_id=thread_id,
            task_ref=task_ref,
        )
        if completion is None:
            return None
        return await self.resume_task(
            AgentTaskResumeRequest(
                thread_id=thread_id,
                run_id=checkpoint.agent_run_id,
                operation_id=checkpoint.operation_id,
                instructions=instructions,
                completion=completion,
                tools=tools,
                agent_name=agent_name,
                permission_scope=dict(permission_scope or {}),
                max_turns=max_turns,
            )
        )

    def _commit_task_failure(
        self,
        request: AgentTaskResumeRequest,
        *,
        checkpoint: AgentCheckpoint,
    ) -> AgentTurnResult:
        assistant, terminal = _terminal_items(
            request,
            answer_text=request.completion.customer_summary,
            status="failed",
            final_output=None,
            context_version=checkpoint.context_version,
            model_turns=0,
            usage={},
            error_code="agent_deferred_task_failed",
        )
        current = self._ledger.get_head(request.thread_id)
        if current.active_task_id != request.completion.task_ref:
            raise AgentTurnError("agent_task_completion_head_conflict")
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
            checkpoint_item=None,
            thread_head=committed.head,
            context_version=checkpoint.context_version,
            model_turns=0,
            replayed=False,
            error_code="agent_deferred_task_failed",
        )

    def _commit_suspension(
        self,
        request: AgentTurnRequest,
        *,
        snapshot: AgentContextSnapshot,
        suspension: DurableToolSuspension,
        model_turns: int,
    ) -> AgentTurnResult:
        current = self._ledger.get_head(request.thread_id)
        task_ref = suspension.awaited_task_ref
        pending_action = suspension.pending_action
        if suspension.checkpoint_kind == "waiting_for_task":
            if current.active_task_id not in {request.run_id, task_ref}:
                raise AgentTurnError(
                    "agent_deferred_task_head_conflict",
                    retryability="retryable",
                )
            active_task_id = task_ref
            pending_action_ref = None
            customer_state = "working"
            item_type = "progress"
            answer_text = suspension.customer_summary
        else:
            if current.active_task_id not in {None, request.run_id}:
                raise AgentTurnError(
                    "agent_pending_action_head_conflict",
                    retryability="retryable",
                )
            if pending_action is None:
                raise AgentTurnError("agent_pending_action_missing")
            active_task_id = request.run_id
            pending_action_ref = pending_action.action_ref
            customer_state = "needs_input"
            item_type = (
                "clarification"
                if pending_action.action_type == "ask_user"
                else "approval_request"
            )
            answer_text = pending_action.prompt
        checkpoint = AgentCheckpoint.create(
            thread_id=request.thread_id,
            agent_run_id=request.run_id,
            operation_id=request.operation_id,
            suspension=suspension,
            context_version=snapshot.context_version,
            session_through_sequence=current.latest_item_sequence,
        )
        assistant, checkpoint_item = _suspension_items(
            request,
            checkpoint=checkpoint,
            item_type=item_type,
            answer_text=answer_text,
            task_ref=task_ref,
            pending_action=pending_action,
        )
        committed = self._ledger.append_items(
            request.thread_id,
            [assistant, checkpoint_item],
            expected_state_version=current.state_version,
            head_target=ThreadHeadTarget(
                active_task_id=active_task_id,
                active_topic_ref=current.active_topic_ref,
                pending_action_ref=pending_action_ref,
                customer_state=customer_state,
            ),
        )
        return AgentTurnResult(
            thread_id=request.thread_id,
            run_id=request.run_id,
            operation_id=request.operation_id,
            status=customer_state,
            final_output=None,
            assistant_item=committed.items[0],
            terminal_item=None,
            checkpoint_item=committed.items[1],
            thread_head=committed.head,
            context_version=snapshot.context_version,
            model_turns=model_turns,
            replayed=False,
        )

    def _commit_success(
        self,
        request: AgentTurnRequest | AgentTaskResumeRequest,
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
            checkpoint_item=None,
            thread_head=committed.head,
            context_version=snapshot.context_version,
            model_turns=sdk_result.model_turns,
            replayed=False,
        )

    def _commit_failure(
        self,
        request: AgentTurnRequest | AgentTaskResumeRequest,
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
            checkpoint_item=None,
            thread_head=committed.head,
            context_version=snapshot.context_version,
            model_turns=0,
            replayed=False,
            error_code=error_code,
        )

    def _replayed_result(
        self,
        request: AgentTurnRequest | AgentTaskResumeRequest,
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
            checkpoint_item=None,
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

    def _replayed_suspension(
        self,
        request: AgentTurnRequest,
        checkpoint_item: ThreadItem,
        checkpoint: AgentCheckpoint,
    ) -> AgentTurnResult:
        if checkpoint.agent_run_id != request.run_id:
            raise AgentTurnError("agent_turn_replay_run_conflict")
        head = self._ledger.get_head(request.thread_id)
        if checkpoint.checkpoint_kind == "waiting_for_task":
            checkpoint_is_current = (
                head.customer_state == "working"
                and head.active_task_id == checkpoint.awaited_task_ref
            )
        else:
            checkpoint_is_current = (
                head.customer_state == "needs_input"
                and checkpoint.pending_action is not None
                and head.pending_action_ref == checkpoint.pending_action.action_ref
            )
        if not checkpoint_is_current:
            raise AgentTurnError("agent_checkpoint_stale")
        assistant = self._ledger.get_item_by_operation_key(
            request.thread_id,
            _suspension_operation_key(request.operation_id),
        )
        if assistant is None:
            raise AgentTurnError("agent_turn_replay_assistant_missing")
        status = (
            "working"
            if checkpoint.checkpoint_kind == "waiting_for_task"
            else "needs_input"
        )
        return AgentTurnResult(
            thread_id=request.thread_id,
            run_id=request.run_id,
            operation_id=request.operation_id,
            status=status,
            final_output=None,
            assistant_item=assistant,
            terminal_item=None,
            checkpoint_item=checkpoint_item,
            thread_head=head,
            context_version=checkpoint.context_version,
            model_turns=0,
            replayed=True,
        )


def _terminal_items(
    request: AgentTurnRequest | AgentTaskResumeRequest,
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


def _suspension_items(
    request: AgentTurnRequest,
    *,
    checkpoint: AgentCheckpoint,
    item_type: str,
    answer_text: str,
    task_ref: str | None,
    pending_action: AgentPendingAction | None,
) -> tuple[NewThreadItem, NewThreadItem]:
    assistant_payload = canonical_value(
        {
            "sdk_replay": False,
            "run_id": request.run_id,
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "task_ref": task_ref,
            "pending_action": (
                pending_action.to_contract() if pending_action is not None else None
            ),
        }
    )
    assistant_digest = canonical_digest(
        {
            "operation_id": request.operation_id,
            "kind": item_type,
            "payload": assistant_payload,
            "text": answer_text,
        }
    )
    checkpoint_payload = {
        "sdk_replay": False,
        "run_id": request.run_id,
        "checkpoint": checkpoint.to_contract(),
    }
    checkpoint_digest = canonical_digest(
        {
            "operation_id": request.operation_id,
            "kind": "agent_checkpoint",
            "payload": checkpoint_payload,
        }
    )
    return (
        NewThreadItem(
            item_id=f"assistant-{assistant_digest[:24]}",
            item_type=item_type,
            role="assistant",
            text=answer_text,
            operation_key=_suspension_operation_key(request.operation_id),
            customer_visible=True,
            payload=assistant_payload,
        ),
        NewThreadItem(
            item_id=f"checkpoint-{checkpoint_digest[:24]}",
            item_type="progress",
            role="system",
            text="",
            operation_key=checkpoint_operation_key(request.operation_id),
            customer_visible=False,
            payload=checkpoint_payload,
        ),
    )


def _resolved_pending_action(
    *,
    bridge: DurableToolBridge,
    thread_id: str,
    customer_state: str,
    pending_action_ref: str | None,
    resolution: PendingActionResolution | None,
) -> AgentPendingAction | None:
    if pending_action_ref is None:
        if resolution is not None:
            raise AgentTurnError("pending_action_resolution_stale")
        return None
    if customer_state != "needs_input":
        raise AgentTurnError("pending_action_head_state_invalid")
    if resolution is None:
        raise AgentTurnError("pending_action_resolution_required")
    if resolution.action_ref != pending_action_ref:
        raise AgentTurnError("pending_action_resolution_ref_mismatch")
    action = bridge.pending_action(
        thread_id=thread_id,
        action_ref=pending_action_ref,
    )
    if action is None:
        raise AgentTurnError("pending_action_missing")
    _validate_pending_action_resolution(action, resolution)
    return action


def _replayed_pending_action(
    user_item: ThreadItem,
    request: AgentTurnRequest,
) -> AgentPendingAction | None:
    stored_resolution = user_item.payload.get("pending_action_resolution")
    requested_resolution = (
        request.pending_action_resolution.model_dump(mode="json", by_alias=True)
        if request.pending_action_resolution is not None
        else None
    )
    if canonical_value(stored_resolution) != canonical_value(requested_resolution):
        raise AgentTurnError("pending_action_resolution_replay_conflict")
    raw_action = user_item.payload.get("resolved_pending_action")
    if raw_action is None:
        return None
    if not isinstance(raw_action, Mapping) or request.pending_action_resolution is None:
        raise AgentTurnError("pending_action_resolution_replay_conflict")
    try:
        action = AgentPendingAction.model_validate(raw_action)
    except ValueError as exc:
        raise AgentTurnError("pending_action_resolution_replay_conflict") from exc
    _validate_pending_action_resolution(action, request.pending_action_resolution)
    return action


def _validate_pending_action_resolution(
    action: AgentPendingAction,
    resolution: PendingActionResolution,
) -> None:
    if action.action_ref != resolution.action_ref:
        raise AgentTurnError("pending_action_resolution_ref_mismatch")
    if action.action_type == "ask_user":
        if resolution.decision != "answered":
            raise AgentTurnError("pending_action_resolution_kind_invalid")
        if resolution.selected_option_id is not None and (
            resolution.selected_option_id
            not in {option.option_id for option in action.options}
        ):
            raise AgentTurnError("pending_action_resolution_option_invalid")
    elif (
        resolution.decision not in {"approved", "rejected"}
        or resolution.selected_option_id is not None
    ):
        raise AgentTurnError("pending_action_resolution_kind_invalid")


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


def _validate_completion_closure(
    completion: AgentTaskCompletion,
    output: AgentFinalOutput,
) -> None:
    authoritative = set(completion.artifact_refs) | set(completion.material_refs)
    if authoritative and not authoritative.intersection(output.material_refs):
        raise AgentTurnError("agent_task_completion_material_ref_missing")
    if not set(completion.limitation_refs).issubset(output.limitation_refs):
        raise AgentTurnError("agent_task_completion_limitation_ref_missing")


def _task_completion_input(completion: AgentTaskCompletion) -> str:
    payload = completion.to_contract()
    payload.pop("relevantMaterials")
    return "WAJE_DURABLE_TASK_COMPLETION=" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


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
    if isinstance(error, DurableToolBridgeError):
        return error.code, "not_retryable"
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
            "execution_mode": tool.execution_mode,
        }
        for tool in tools
    )


def _user_operation_key(operation_id: str) -> str:
    return f"user:{operation_id}"


def _assistant_operation_key(operation_id: str) -> str:
    return f"assistant:{operation_id}"


def _suspension_operation_key(operation_id: str) -> str:
    return f"assistant-suspension:{operation_id}"


def _terminal_operation_key(operation_id: str) -> str:
    return f"terminal:{operation_id}"
