from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Mapping

import pytest
from pydantic import BaseModel

from bi_agent.runtime.agent_context import AgentContextAssembler, InMemoryArtifactIndex
from bi_agent.runtime.agent_interaction_tools import agent_interaction_tools
from bi_agent.runtime.agent_sdk_contracts import (
    AgentToolResult,
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentTool,
    WajeAgentToolCall,
)
from bi_agent.runtime.agent_task_recovery import (
    AgentTaskRecoveryError,
    AuthoritativeAgentTaskCompletionLoader,
)
from bi_agent.runtime.agent_tool_discovery import AgentTurnActionBinding
from bi_agent.runtime.agent_turn_runtime import (
    AgentTaskCompletion,
    AgentTaskResumeRequest,
    AgentTurnError,
    AgentTurnRequest,
    AgentTurnRuntime,
)
from bi_agent.runtime.analysis_artifacts import (
    ArtifactDescriptor,
    InMemoryAnalysisArtifactRegistry,
)
from bi_agent.runtime.durable_tool_bridge import (
    AgentCheckpoint,
    PendingActionResolution,
)
from bi_agent.runtime.postgres_agent_session import PostgresAgentSession
from bi_agent.runtime.thread_item_ledger import (
    InMemoryThreadItemLedger,
    NewThreadItem,
    ThreadHeadTarget,
)


@dataclass(frozen=True)
class AdapterStep:
    final_output: Mapping[str, Any]
    tool_name: str | None = None
    call_id: str | None = None
    arguments: Mapping[str, Any] | None = None
    tool_result: Mapping[str, Any] | None = None
    raises_after_tool: bool = False


class DurableAdapter:
    def __init__(self, *steps: AdapterStep) -> None:
        self.steps = list(steps)
        self.calls: list[WajeAgentRunRequest] = []

    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
        self.calls.append(request)
        if not self.steps:
            raise AssertionError("unexpected_adapter_call")
        step = self.steps.pop(0)
        if step.tool_name is not None:
            assert request.event_sink is not None
            assert step.call_id is not None
            assert step.arguments is not None
            assert step.tool_result is not None
            await request.event_sink.record_tool_call(
                tool_name=step.tool_name,
                call_id=step.call_id,
                arguments=step.arguments,
            )
            await request.event_sink.record_tool_result(
                tool_name=step.tool_name,
                call_id=step.call_id,
                result=step.tool_result,
                succeeded=step.tool_result.get("status") != "failed",
            )
            if step.raises_after_tool:
                raise RuntimeError("sdk_tail_failed_after_durable_tool_result")
        return WajeAgentRunResult(
            run_id=request.run_id,
            final_output=dict(step.final_output),
            usage={"input_tokens": 5, "output_tokens": 3},
            model_turns=2 if step.tool_name is not None else 1,
            tool_calls=(
                (WajeAgentToolCall(tool_name=step.tool_name, call_id=step.call_id),)
                if step.tool_name is not None and step.call_id is not None
                else ()
            ),
        )


class TaskStateStore:
    def __init__(self, state: Mapping[str, Any] | None) -> None:
        self.state = state

    def get_run_state(self, run_id: str) -> Mapping[str, Any] | None:
        if self.state is None:
            return None
        return dict(self.state)


class StaticCompletionLoader:
    def __init__(self, completion: AgentTaskCompletion | None) -> None:
        self.completion = completion
        self.calls: list[dict[str, str]] = []

    def load_task_completion(
        self,
        *,
        thread_id: str,
        task_ref: str,
    ) -> AgentTaskCompletion | None:
        self.calls.append({"thread_id": thread_id, "task_ref": task_ref})
        return self.completion


def _runtime(
    adapter: DurableAdapter,
) -> tuple[InMemoryThreadItemLedger, AgentTurnRuntime]:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-durable")
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=AgentContextAssembler(
            ledger=ledger,
            artifact_index=InMemoryArtifactIndex(),
        ),
        adapter=adapter,
    )
    return ledger, runtime


def _turn_request(
    *,
    operation_id: str = "operation-durable",
    run_id: str = "agent-run-durable",
    expected_state_version: int = 0,
    user_message: str = "分析付费金额变化。",
    pending_action_resolution: PendingActionResolution | None = None,
    action_binding: AgentTurnActionBinding | None = None,
    tools: tuple[WajeAgentTool, ...] = (),
) -> AgentTurnRequest:
    return AgentTurnRequest(
        thread_id="thread-durable",
        run_id=run_id,
        operation_id=operation_id,
        user_item_id=f"message-{operation_id}",
        user_message=user_message,
        expected_state_version=expected_state_version,
        instructions="依据 WAJE 权威材料调用工具并回答。",
        pending_action_resolution=pending_action_resolution,
        action_binding=action_binding,
        tools=tools,
    )


def _analysis_action_binding() -> AgentTurnActionBinding:
    return AgentTurnActionBinding.create(
        catalog_digest="catalog-digest",
        input_digest="input-digest",
        action_context_digest="context-digest",
        selected_tools=("run_bi_analysis",),
        initial_action="call_tool",
        required_tool_name="run_bi_analysis",
        material_decision_topics=(),
    )


class _AnalysisToolInput(BaseModel):
    business_question: str


def _analysis_tool() -> WajeAgentTool:
    return WajeAgentTool(
        name="run_bi_analysis",
        description="提交 BI 分析任务。",
        input_model=_AnalysisToolInput,
        handler=lambda _arguments: _queued_tool_result(),
        execution_mode="suspend_turn",
    )


def _queued_tool_result(task_ref: str = "run-bi-background") -> dict[str, Any]:
    return AgentToolResult(
        status="succeeded",
        output={
            "operation": "run_bi_analysis",
            "taskRef": task_ref,
            "taskState": "queued",
            "sourceTaskRef": None,
            "replayed": False,
        },
        artifactRefs=[],
        materialRefs=[],
        limitationRefs=[],
        retryability="never",
        customerSummary="BI 分析任务已进入持久化执行队列。",
        technicalDetailRef=None,
    ).model_dump(mode="json", by_alias=True)


def test_deferred_bi_tool_suspends_agent_turn_without_overwriting_active_task() -> None:
    adapter = DurableAdapter(
        AdapterStep(
            final_output={
                "answerMarkdown": "模型临时确认文本不会成为任务终局。",
                "materialRefs": [],
                "limitationRefs": [],
            },
            tool_name="run_bi_analysis",
            call_id="call-bi-background",
            arguments={"business_question": "分析付费金额变化。"},
            tool_result=_queued_tool_result(),
        )
    )
    ledger, runtime = _runtime(adapter)
    request = _turn_request()

    result = asyncio.run(runtime.run(request))

    assert result.status == "working"
    assert result.final_output is None
    assert result.terminal_item is None
    assert result.checkpoint_item is not None
    assert result.assistant_item.item_type == "progress"
    assert result.assistant_item.text == "BI 分析任务已进入持久化执行队列。"
    assert result.thread_head.active_task_id == "run-bi-background"
    assert result.thread_head.customer_state == "working"
    assert not any(
        item.item_type == "task_terminal"
        for item in ledger.list_items("thread-durable")
    )
    projection = result.customer_projection()
    assert "run-bi-background" not in json.dumps(projection, ensure_ascii=False)
    assert "checkpoint" not in json.dumps(projection, ensure_ascii=False)

    replay = asyncio.run(runtime.run(request))
    assert replay.replayed is True
    assert replay.status == "working"
    assert len(adapter.calls) == 1


def test_recovery_commits_checkpoint_from_persisted_tool_result_without_model_replay() -> (
    None
):
    adapter = DurableAdapter()
    ledger, runtime = _runtime(adapter)
    request = _turn_request()
    user = ledger.append_items(
        request.thread_id,
        [
            NewThreadItem(
                item_id=request.user_item_id,
                item_type="user_message",
                role="user",
                text=request.user_message,
                operation_key=f"user:{request.operation_id}",
                customer_visible=True,
                payload={
                    "sdk_item": {"role": "user", "content": request.user_message},
                    "sdk_replay": True,
                    "run_id": request.run_id,
                },
            )
        ],
        expected_state_version=0,
        head_target=ThreadHeadTarget(
            active_task_id=request.run_id,
            active_topic_ref=None,
            pending_action_ref=None,
            customer_state="working",
        ),
    ).items[0]
    session = PostgresAgentSession(
        ledger=ledger,
        thread_id=request.thread_id,
        operation_id=request.operation_id,
        input_item_id=user.item_id,
        input_text=request.user_message,
        replay_through_sequence=0,
    )
    asyncio.run(
        session.record_tool_call(
            tool_name="run_bi_analysis",
            call_id="call-crash-window",
            arguments={"business_question": request.user_message},
        )
    )
    asyncio.run(
        session.record_tool_result(
            tool_name="run_bi_analysis",
            call_id="call-crash-window",
            result=_queued_tool_result(),
            succeeded=True,
        )
    )

    recovered = asyncio.run(runtime.run(request))

    assert recovered.status == "working"
    assert recovered.thread_head.active_task_id == "run-bi-background"
    assert recovered.checkpoint_item is not None
    assert adapter.calls == []


def test_durable_tool_result_wins_when_sdk_fails_after_tool_persistence() -> None:
    adapter = DurableAdapter(
        AdapterStep(
            final_output={
                "answerMarkdown": "不会成为终局。",
                "materialRefs": [],
                "limitationRefs": [],
            },
            tool_name="run_bi_analysis",
            call_id="call-sdk-tail-failure",
            arguments={"business_question": "分析付费金额变化。"},
            tool_result=_queued_tool_result(),
            raises_after_tool=True,
        )
    )
    ledger, runtime = _runtime(adapter)

    result = asyncio.run(runtime.run(_turn_request()))

    assert result.status == "working"
    assert result.terminal_item is None
    assert result.checkpoint_item is not None
    assert result.thread_head.active_task_id == "run-bi-background"
    assert not any(
        item.item_type == "task_terminal"
        for item in ledger.list_items("thread-durable")
    )


def test_ask_user_checkpoint_is_customer_safe_and_typed_resolution_resumes() -> None:
    ask_user, _ = agent_interaction_tools(
        thread_id="thread-durable",
        operation_id="operation-durable",
    )
    arguments = {
        "materialDecision": "请选择本次比较基线。",
        "options": [
            {
                "optionId": "previous_period",
                "label": "上一周期",
                "description": "与紧邻的上一完整周期比较。",
                "recommended": True,
            },
            {
                "optionId": "same_period_last_month",
                "label": "上月同期",
                "description": "与上月相同日期范围比较。",
                "recommended": False,
            },
        ],
    }
    tool_result = ask_user.handler(arguments)
    assert isinstance(tool_result, AgentToolResult)
    pending = tool_result.output["pendingAction"]
    adapter = DurableAdapter(
        AdapterStep(
            final_output={
                "answerMarkdown": "等待用户决定。",
                "materialRefs": [],
                "limitationRefs": [],
            },
            tool_name="ask_user",
            call_id="call-ask-user",
            arguments=arguments,
            tool_result=tool_result.model_dump(mode="json", by_alias=True),
        ),
        AdapterStep(
            final_output={
                "answerMarkdown": "已采用上一周期作为比较基线。",
                "materialRefs": [],
                "limitationRefs": [],
            }
        ),
    )
    ledger, runtime = _runtime(adapter)

    waiting = asyncio.run(runtime.run(_turn_request()))

    assert waiting.status == "needs_input"
    assert waiting.assistant_item.item_type == "clarification"
    assert waiting.thread_head.pending_action_ref == pending["actionRef"]
    assert waiting.customer_projection()["pendingAction"] == pending

    resolution = PendingActionResolution(
        actionRef=pending["actionRef"],
        decision="answered",
        selectedOptionId="previous_period",
        answerText="采用上一周期。",
    )
    resumed = asyncio.run(
        runtime.run(
            _turn_request(
                operation_id="operation-resolution",
                run_id="agent-run-resolution",
                expected_state_version=waiting.thread_head.state_version,
                user_message="采用上一周期。",
                pending_action_resolution=resolution,
            )
        )
    )

    assert resumed.status == "completed"
    assert resumed.thread_head.pending_action_ref is None
    resolution_item = ledger.get_item_by_operation_key(
        "thread-durable",
        "user:operation-resolution",
    )
    assert resolution_item is not None
    assert resolution_item.payload["pending_action_resolution"] == (
        resolution.model_dump(mode="json", by_alias=True)
    )
    with pytest.raises(AgentTurnError, match="agent_checkpoint_stale"):
        asyncio.run(runtime.run(_turn_request()))


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_approval_checkpoint_accepts_only_typed_approval_resolution(
    decision: str,
) -> None:
    _, request_approval = agent_interaction_tools(
        thread_id="thread-durable",
        operation_id="operation-durable",
    )
    arguments = {
        "actionSummary": "提交经过校验的分析发布。",
        "sideEffectScope": "写入当前 thread 的 publication 与 delivery 状态。",
    }
    tool_result = request_approval.handler(arguments)
    assert isinstance(tool_result, AgentToolResult)
    pending = tool_result.output["pendingAction"]
    adapter = DurableAdapter(
        AdapterStep(
            final_output={
                "answerMarkdown": "等待审批。",
                "materialRefs": [],
                "limitationRefs": [],
            },
            tool_name="request_approval",
            call_id="call-request-approval",
            arguments=arguments,
            tool_result=tool_result.model_dump(mode="json", by_alias=True),
        ),
        AdapterStep(
            final_output={
                "answerMarkdown": f"审批决定已记录：{decision}。",
                "materialRefs": [],
                "limitationRefs": [],
            }
        ),
    )
    _, runtime = _runtime(adapter)

    waiting = asyncio.run(runtime.run(_turn_request()))

    assert waiting.status == "needs_input"
    assert waiting.assistant_item.item_type == "approval_request"
    assert waiting.customer_projection()["pendingAction"] == pending
    resolution = PendingActionResolution(
        actionRef=pending["actionRef"],
        decision=decision,
        answerText="批准执行。" if decision == "approved" else "拒绝执行。",
    )
    resumed = asyncio.run(
        runtime.run(
            _turn_request(
                operation_id=f"operation-approval-{decision}",
                run_id=f"agent-run-approval-{decision}",
                expected_state_version=waiting.thread_head.state_version,
                user_message=resolution.answer_text,
                pending_action_resolution=resolution,
            )
        )
    )

    assert resumed.status == "completed"
    assert resumed.thread_head.pending_action_ref is None
    assert adapter.calls[1].input_text == resolution.answer_text


def test_pending_action_resolution_rejects_stale_or_wrong_typed_decision() -> None:
    adapter = DurableAdapter()
    ledger, runtime = _runtime(adapter)
    ledger.append_items(
        "thread-durable",
        [
            NewThreadItem(
                item_id="clarification-existing",
                item_type="clarification",
                role="assistant",
                text="请选择基线。",
                operation_key="assistant:old-operation",
                customer_visible=True,
                payload={
                    "pending_action": {
                        "actionRef": "pending-action:one",
                        "actionType": "ask_user",
                        "prompt": "请选择基线。",
                        "options": [
                            {
                                "optionId": "a",
                                "label": "A",
                                "description": "选择 A。",
                                "recommended": True,
                            },
                            {
                                "optionId": "b",
                                "label": "B",
                                "description": "选择 B。",
                                "recommended": False,
                            },
                        ],
                        "actionSummary": None,
                        "sideEffectScope": None,
                    }
                },
            )
        ],
        head_target=ThreadHeadTarget(
            active_task_id="agent-run-old",
            active_topic_ref=None,
            pending_action_ref="pending-action:one",
            customer_state="needs_input",
        ),
    )
    head = ledger.get_head("thread-durable")

    with pytest.raises(AgentTurnError, match="pending_action_resolution_kind_invalid"):
        asyncio.run(
            runtime.run(
                _turn_request(
                    operation_id="operation-invalid-resolution",
                    run_id="agent-run-invalid-resolution",
                    expected_state_version=head.state_version,
                    pending_action_resolution=PendingActionResolution(
                        actionRef="pending-action:one",
                        decision="approved",
                        answerText="批准。",
                    ),
                )
            )
        )


def test_completed_task_resumes_runner_from_checkpoint_and_commits_one_terminal() -> (
    None
):
    adapter = DurableAdapter(
        AdapterStep(
            final_output={
                "answerMarkdown": "等待后台任务。",
                "materialRefs": [],
                "limitationRefs": [],
            },
            tool_name="run_bi_analysis",
            call_id="call-task-resume",
            arguments={"business_question": "分析付费金额变化。"},
            tool_result=_queued_tool_result(),
        ),
        AdapterStep(
            final_output={
                "answerMarkdown": "分析已经完成，并形成可追溯发布材料。",
                "materialRefs": ["publication:completed"],
                "limitationRefs": [],
            }
        ),
    )
    ledger, runtime = _runtime(adapter)
    turn = _turn_request(
        action_binding=_analysis_action_binding(),
        tools=(_analysis_tool(),),
    )
    suspended = asyncio.run(runtime.run(turn))
    completion = AgentTaskCompletion(
        taskRef="run-bi-background",
        status="completed",
        customerSummary="分析已经完成。",
        artifactRefs=["publication:completed"],
        materialRefs=[],
        limitationRefs=[],
        relevantMaterials=[
            {
                "material_ref": "publication:completed",
                "kind": "customer_publication",
                "summary": "已发布业务参考。",
            }
        ],
    )
    completion_loader = StaticCompletionLoader(completion)

    completed = asyncio.run(
        runtime.resume_ready_task(
            thread_id=turn.thread_id,
            task_ref=completion.task_ref,
            completion_loader=completion_loader,
            instructions=turn.instructions,
        )
    )

    assert completed.status == "completed"
    assert completed.terminal_item is not None
    assert completed.thread_head.active_task_id is None
    assert completed.terminal_admission is not None
    assert completed.terminal_admission.completion_kind == "analysis_publication"
    assert completed.terminal_admission.durable_task_ref == "run-bi-background"
    assert completed.terminal_admission.action_binding_digest == (
        turn.action_binding.selection_digest
    )
    assert completed.final_output == {
        "answerMarkdown": "分析已经完成，并形成可追溯发布材料。",
        "materialRefs": ["publication:completed"],
        "limitationRefs": [],
    }
    assert adapter.calls[1].input_text.startswith("WAJE_DURABLE_TASK_COMPLETION=")
    assert "publication:completed" in adapter.calls[1].instructions
    assert suspended.checkpoint_item is not None
    assert suspended.checkpoint_item.payload["checkpoint"][
        "actionBindingDigest"
    ] == turn.action_binding.selection_digest

    replay = asyncio.run(
        runtime.resume_ready_task(
            thread_id=turn.thread_id,
            task_ref=completion.task_ref,
            completion_loader=completion_loader,
            instructions=turn.instructions,
        )
    )
    assert replay.replayed is True
    assert len(adapter.calls) == 2
    assert completion_loader.calls == [
        {"thread_id": "thread-durable", "task_ref": "run-bi-background"},
        {"thread_id": "thread-durable", "task_ref": "run-bi-background"},
    ]
    assert (
        len(
            [
                item
                for item in ledger.list_items("thread-durable")
                if item.item_type == "task_terminal"
            ]
        )
        == 1
    )


def test_failed_background_task_commits_safe_terminal_without_model_call() -> None:
    adapter = DurableAdapter(
        AdapterStep(
            final_output={
                "answerMarkdown": "等待后台任务。",
                "materialRefs": [],
                "limitationRefs": [],
            },
            tool_name="run_bi_analysis",
            call_id="call-task-failure",
            arguments={"business_question": "分析付费金额变化。"},
            tool_result=_queued_tool_result(),
        )
    )
    _, runtime = _runtime(adapter)
    turn = _turn_request()
    asyncio.run(runtime.run(turn))

    result = asyncio.run(
        runtime.resume_task(
            AgentTaskResumeRequest(
                thread_id=turn.thread_id,
                run_id=turn.run_id,
                operation_id=turn.operation_id,
                instructions=turn.instructions,
                completion=AgentTaskCompletion(
                    taskRef="run-bi-background",
                    status="failed",
                    customerSummary="本次分析任务未能完成，请稍后重试。",
                    artifactRefs=[],
                    materialRefs=[],
                    limitationRefs=[],
                    relevantMaterials=[],
                ),
            )
        )
    )

    assert result.status == "failed"
    assert result.error_code == "agent_deferred_task_failed"
    assert result.assistant_item.text == "本次分析任务未能完成，请稍后重试。"
    assert len(adapter.calls) == 1
    assert "agent_deferred_task_failed" not in json.dumps(
        result.customer_projection(),
        ensure_ascii=False,
    )


def test_task_resume_rejects_nested_durable_suspension_tools() -> None:
    ask_user, _ = agent_interaction_tools(
        thread_id="thread-durable",
        operation_id="operation-durable",
    )

    with pytest.raises(ValueError, match="agent_resume_nested_suspension_unsupported"):
        AgentTaskResumeRequest(
            thread_id="thread-durable",
            run_id="agent-run-durable",
            operation_id="operation-durable",
            instructions="生成任务终局。",
            completion=AgentTaskCompletion(
                taskRef="run-bi-background",
                status="completed",
                customerSummary="分析已完成。",
                artifactRefs=["publication:completed"],
                materialRefs=[],
                limitationRefs=[],
                relevantMaterials=[],
            ),
            tools=(ask_user,),
        )


def test_checkpoint_rejects_tampered_identity_and_digest() -> None:
    adapter = DurableAdapter(
        AdapterStep(
            final_output={
                "answerMarkdown": "等待后台任务。",
                "materialRefs": [],
                "limitationRefs": [],
            },
            tool_name="run_bi_analysis",
            call_id="call-checkpoint",
            arguments={"business_question": "分析付费金额变化。"},
            tool_result=_queued_tool_result(),
        )
    )
    _, runtime = _runtime(adapter)
    suspended = asyncio.run(runtime.run(_turn_request()))
    assert suspended.checkpoint_item is not None
    payload = deepcopy(suspended.checkpoint_item.payload["checkpoint"])

    payload["agentRunId"] = "agent-run-tampered"
    with pytest.raises(ValueError, match="agent_checkpoint_ref_invalid"):
        AgentCheckpoint.model_validate(payload)


def test_ask_user_rejects_customer_language_drift_before_persistence() -> None:
    ask_user, _ = agent_interaction_tools(
        thread_id="thread-language",
        operation_id="operation-language",
        customer_language="zh-Hans",
    )

    with pytest.raises(
        ValueError,
        match="^agent_interaction_customer_language_mismatch$",
    ):
        ask_user.handler(
            {
                "materialDecision": "Choose a comparison baseline.",
                "options": [
                    {
                        "optionId": "one",
                        "label": "Previous month",
                        "description": "Compare with the previous month.",
                        "recommended": True,
                    },
                    {
                        "optionId": "two",
                        "label": "Previous quarter",
                        "description": "Compare with the previous quarter.",
                        "recommended": False,
                    },
                ],
            }
        )


def test_authoritative_completion_loader_projects_only_task_publication() -> None:
    registry = InMemoryAnalysisArtifactRegistry()
    registry.add(
        "thread-durable",
        ArtifactDescriptor(
            artifact_ref="publication:task",
            artifact_type="bi_publication",
            version="publication:v1",
            digest="digest-task",
            source_refs=("run-bi-background", "claim:one"),
            visibility_policy_ref="visibility:customer-safe",
            customer_summary="付费金额分析已完成。",
            created_at="2026-07-21T00:00:00+00:00",
        ),
        {
            "artifactType": "bi_publication",
            "artifactRef": "publication:task",
            "publication": {"blocks": []},
            "materialRefs": ["claim:one"],
            "limitationRefs": ["limitation:one"],
        },
    )
    registry.add(
        "thread-durable",
        ArtifactDescriptor(
            artifact_ref="publication:task-latest",
            artifact_type="bi_publication",
            version="publication:v2",
            digest="digest-task-latest",
            source_refs=("run-bi-background", "claim:two"),
            visibility_policy_ref="visibility:customer-safe",
            customer_summary="付费金额分析已完成并发布最新修订。",
            created_at="2026-07-21T00:00:02+00:00",
        ),
        {
            "artifactType": "bi_publication",
            "artifactRef": "publication:task-latest",
            "publication": {"blocks": []},
            "materialRefs": ["claim:two"],
            "limitationRefs": ["limitation:one"],
        },
    )
    registry.add(
        "thread-durable",
        ArtifactDescriptor(
            artifact_ref="publication:other-task",
            artifact_type="bi_publication",
            version="publication:v2",
            digest="digest-other",
            source_refs=("run-bi-other",),
            visibility_policy_ref="visibility:customer-safe",
            customer_summary="另一项分析已完成。",
            created_at="2026-07-21T00:00:01+00:00",
        ),
        {"limitationRefs": []},
    )
    loader = AuthoritativeAgentTaskCompletionLoader(
        store=TaskStateStore(
            {
                "run_id": "run-bi-background",
                "thread_id": "thread-durable",
                "status": "completed",
            }
        ),
        artifact_registry=registry,
    )

    completion = loader.load_task_completion(
        thread_id="thread-durable",
        task_ref="run-bi-background",
    )

    assert completion is not None
    assert completion.status == "completed_with_limits"
    assert completion.artifact_refs == ["publication:task-latest"]
    assert completion.limitation_refs == ["limitation:one"]
    serialized = json.dumps(completion.to_contract(), ensure_ascii=False)
    assert "publication:other-task" not in serialized
    assert '"publication:task"' not in serialized
    assert "visibility:customer-safe" not in serialized


def test_authoritative_completion_loader_waits_and_fails_closed() -> None:
    registry = InMemoryAnalysisArtifactRegistry()
    waiting = AuthoritativeAgentTaskCompletionLoader(
        store=TaskStateStore(
            {
                "run_id": "run-bi-background",
                "thread_id": "thread-durable",
                "status": "narrative_ready",
            }
        ),
        artifact_registry=registry,
    )
    assert (
        waiting.load_task_completion(
            thread_id="thread-durable",
            task_ref="run-bi-background",
        )
        is None
    )

    completed_without_publication = AuthoritativeAgentTaskCompletionLoader(
        store=TaskStateStore(
            {
                "run_id": "run-bi-background",
                "thread_id": "thread-durable",
                "status": "completed",
            }
        ),
        artifact_registry=registry,
    )
    with pytest.raises(AgentTaskRecoveryError, match="agent_task_publication_missing"):
        completed_without_publication.load_task_completion(
            thread_id="thread-durable",
            task_ref="run-bi-background",
        )

    wrong_thread = AuthoritativeAgentTaskCompletionLoader(
        store=TaskStateStore(
            {
                "run_id": "run-bi-background",
                "thread_id": "thread-other",
                "status": "failed",
            }
        ),
        artifact_registry=registry,
    )
    with pytest.raises(AgentTaskRecoveryError, match="agent_task_state_owner_mismatch"):
        wrong_thread.load_task_completion(
            thread_id="thread-durable",
            task_ref="run-bi-background",
        )
