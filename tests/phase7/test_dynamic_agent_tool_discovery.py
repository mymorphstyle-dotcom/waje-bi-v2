from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from bi_agent.runtime.agent_context import AgentContextAssembler, InMemoryArtifactIndex
from bi_agent.runtime.agent_sdk_contracts import (
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentTool,
    WajeAgentToolCall,
)
from bi_agent.runtime.agent_tool_discovery import (
    AgentToolDiscoveryError,
    DynamicAgentToolResolver,
    DynamicToolSelectionOutput,
    TOOL_DISCOVERY_INSTRUCTIONS,
    WajeToolSelectionGenerator,
)
from bi_agent.runtime.agent_turn_runtime import (
    AgentTurnRequest,
    AgentTurnRuntime,
    _action_context,
)
from bi_agent.runtime.capability_catalog_tool import capability_catalog_tool
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.thread_item_ledger import (
    InMemoryThreadItemLedger,
    NewThreadItem,
)


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _tool(name: str) -> WajeAgentTool:
    return WajeAgentTool(
        name=name,
        description=f"Execute {name} through a typed WAJE boundary.",
        input_model=NoArguments,
        handler=lambda _arguments: {"status": "ok"},
        execution_mode=(
            "suspend_turn"
            if name in {"ask_user", "request_approval", "run_bi_analysis"}
            else "continue"
        ),
    )


class SelectionGenerator:
    def __init__(
        self,
        selected: list[str],
        *,
        initial_action: str | None = None,
        required_tool_name: str | None = None,
        material_decision_topics: list[str] | None = None,
    ) -> None:
        self.selected = selected
        self.initial_action = initial_action or ("call_tool" if selected else "respond")
        self.required_tool_name = (
            required_tool_name
            if required_tool_name is not None
            else selected[0]
            if selected
            else None
        )
        self.material_decision_topics = material_decision_topics or []
        self.calls = 0
        self.catalog_names: list[str] = []
        self.action_contexts: list[object] = []

    async def select(
        self,
        *,
        user_message: str,
        tool_catalog: object,
        permission_scope: object,
        action_context: object,
    ) -> DynamicToolSelectionOutput:
        assert user_message == "解释已经发布的材料"
        assert permission_scope == {"analysis_access": "single"}
        self.action_contexts.append(action_context)
        self.calls += 1
        self.catalog_names = [item["name"] for item in tool_catalog]  # type: ignore[index]
        return DynamicToolSelectionOutput(
            selectedTools=self.selected,
            initialAction=self.initial_action,
            requiredToolName=self.required_tool_name,
            materialDecisionTopics=self.material_decision_topics,
        )


def _candidates() -> tuple[WajeAgentTool, ...]:
    return (
        _tool("inspect_analysis_artifact"),
        _tool("run_bi_analysis"),
        _tool("ask_user"),
        _tool("request_approval"),
    )


def test_dynamic_resolver_adds_mandatory_tools_and_replays_exact_selection() -> None:
    generator = SelectionGenerator(["inspect_analysis_artifact"])
    resolver = DynamicAgentToolResolver(
        generator=generator,
        mandatory_tool_names=["ask_user", "request_approval"],
    )
    resolved = asyncio.run(
        resolver.resolve(
            user_message="解释已经发布的材料",
            candidate_tools=_candidates(),
            permission_scope={"analysis_access": "single"},
        )
    )

    assert [tool.name for tool in resolved.tools] == [
        "inspect_analysis_artifact",
        "ask_user",
        "request_approval",
    ]
    assert generator.catalog_names == [
        "inspect_analysis_artifact",
        "run_bi_analysis",
    ]
    replayed = resolver.replay(
        user_message="解释已经发布的材料",
        candidate_tools=_candidates(),
        permission_scope={"analysis_access": "single"},
        selection_payload=resolved.selection.to_contract(),
    )
    assert replayed.selection == resolved.selection
    assert [tool.name for tool in replayed.tools] == [
        tool.name for tool in resolved.tools
    ]


def test_model_selected_required_optional_tool_is_canonicalized_into_selection() -> None:
    class Adapter:
        async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
            return WajeAgentRunResult(
                run_id=request.run_id,
                final_output={
                    "selectedTools": [],
                    "initialAction": "call_tool",
                    "requiredToolName": "inspect_analysis_artifact",
                    "materialDecisionTopics": [],
                },
                usage={},
                model_turns=1,
            )

    output = asyncio.run(
        WajeToolSelectionGenerator(Adapter()).select(
            user_message="解释材料",
            tool_catalog=[
                {
                    "name": "inspect_analysis_artifact",
                    "description": "Inspect one artifact.",
                }
            ],
            permission_scope={"analysis_access": "single"},
        )
    )

    assert output.selected_tools == ["inspect_analysis_artifact"]
    assert output.required_tool_name == "inspect_analysis_artifact"


def test_material_decision_topics_bind_the_fixed_clarification_tool() -> None:
    class Adapter:
        async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
            return WajeAgentRunResult(
                run_id=request.run_id,
                final_output={
                    "selectedTools": [],
                    "initialAction": "call_tool",
                    "requiredToolName": "run_bi_analysis",
                    "materialDecisionTopics": [
                        "time_window",
                        "baseline_or_counterfactual",
                    ],
                },
                usage={},
                model_turns=1,
            )

    output = asyncio.run(
        WajeToolSelectionGenerator(Adapter()).select(
            user_message="评估活动影响。",
            tool_catalog=[{"name": "run_bi_analysis", "description": "Run BI."}],
            permission_scope={"analysis_access": "single"},
        )
    )

    assert output.initial_action == "ask_user"
    assert output.required_tool_name == "ask_user"
    assert output.material_decision_topics == [
        "time_window",
        "baseline_or_counterfactual",
    ]


def test_causal_baseline_canonicalizes_overlapping_comparison_topic() -> None:
    class Adapter:
        async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
            return WajeAgentRunResult(
                run_id=request.run_id,
                final_output={
                    "selectedTools": [],
                    "initialAction": "ask_user",
                    "requiredToolName": "ask_user",
                    "materialDecisionTopics": [
                        "baseline_or_counterfactual",
                        "comparison_scope",
                    ],
                },
                usage={},
                model_turns=1,
            )

    output = asyncio.run(
        WajeToolSelectionGenerator(Adapter()).select(
            user_message="评估一次活动对指标的影响。",
            tool_catalog=[{"name": "run_bi_analysis", "description": "Run BI."}],
            permission_scope={"analysis_access": "single"},
        )
    )

    assert output.material_decision_topics == ["baseline_or_counterfactual"]


def test_dynamic_resolver_rejects_unknown_or_tampered_selection() -> None:
    resolver = DynamicAgentToolResolver(
        generator=SelectionGenerator(["unknown_tool"]),
        mandatory_tool_names=["ask_user", "request_approval"],
    )
    with pytest.raises(AgentToolDiscoveryError, match="agent_tool_selection_unknown"):
        asyncio.run(
            resolver.resolve(
                user_message="解释已经发布的材料",
                candidate_tools=_candidates(),
                permission_scope={"analysis_access": "single"},
            )
        )

    valid = asyncio.run(
        DynamicAgentToolResolver(
            generator=SelectionGenerator([]),
            mandatory_tool_names=["ask_user", "request_approval"],
        ).resolve(
            user_message="解释已经发布的材料",
            candidate_tools=_candidates(),
            permission_scope={"analysis_access": "single"},
        )
    )
    tampered = valid.selection.to_contract()
    tampered["selectedTools"] = ["ask_user"]
    with pytest.raises(
        AgentToolDiscoveryError,
        match="agent_tool_selection_payload_invalid",
    ):
        resolver.replay(
            user_message="解释已经发布的材料",
            candidate_tools=_candidates(),
            permission_scope={"analysis_access": "single"},
            selection_payload=tampered,
        )


class MainAdapter:
    def __init__(self) -> None:
        self.calls: list[WajeAgentRunRequest] = []

    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
        self.calls.append(request)
        assert request.event_sink is not None
        assert request.required_tool_name is not None
        await request.event_sink.record_tool_call(
            tool_name=request.required_tool_name,
            call_id="call-required-tool",
            arguments={},
        )
        await request.event_sink.record_tool_result(
            tool_name=request.required_tool_name,
            call_id="call-required-tool",
            result={"status": "ok"},
            succeeded=True,
        )
        return WajeAgentRunResult(
            run_id=request.run_id,
            final_output={
                "answerMarkdown": "已按动态工具集完成回答。",
                "materialRefs": [],
                "limitationRefs": [],
            },
            usage={},
            model_turns=1,
            tool_calls=(
                WajeAgentToolCall(
                    tool_name=request.required_tool_name,
                    call_id="call-required-tool",
                ),
            ),
        )


class PassiveAdapter:
    def __init__(self) -> None:
        self.calls: list[WajeAgentRunRequest] = []

    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
        self.calls.append(request)
        return WajeAgentRunResult(
            run_id=request.run_id,
            final_output={
                "answerMarkdown": "只返回了文本。",
                "materialRefs": [],
                "limitationRefs": [],
            },
            usage={},
            model_turns=1,
        )


def test_agent_turn_persists_tool_selection_in_the_thread_ledger() -> None:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-tools")
    selector = SelectionGenerator(["inspect_analysis_artifact"])
    adapter = MainAdapter()
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=AgentContextAssembler(
            ledger=ledger,
            artifact_index=InMemoryArtifactIndex(),
        ),
        adapter=adapter,
        tool_resolver=DynamicAgentToolResolver(
            generator=selector,
            mandatory_tool_names=["ask_user", "request_approval"],
        ),
        business_clock={
            "currentDate": "2026-07-21",
            "timeZone": "Asia/Shanghai",
        },
    )
    request = AgentTurnRequest(
        thread_id="thread-tools",
        run_id="run-tools",
        operation_id="operation-tools",
        user_item_id="message-tools",
        user_message="解释已经发布的材料",
        expected_state_version=0,
        instructions="使用动态工具回答。",
        tools=_candidates(),
        permission_scope={"analysis_access": "single"},
    )

    first = asyncio.run(runtime.run(request))
    replay = asyncio.run(runtime.run(request))

    assert first.status == "completed"
    assert first.terminal_admission is not None
    assert first.terminal_admission.completion_kind == "tool_response"
    assert first.terminal_admission.executed_tool_names == [
        "inspect_analysis_artifact"
    ]
    assert replay.replayed is True
    assert selector.calls == 1
    assert selector.action_contexts[0]["recentConversation"][-1]["text"] == (
        "解释已经发布的材料"
    )
    assert selector.action_contexts[0]["businessClock"] == {
        "currentDate": "2026-07-21",
        "timeZone": "Asia/Shanghai",
    }
    assert [[tool.name for tool in call.tools] for call in adapter.calls] == [[
        "inspect_analysis_artifact",
        "ask_user",
        "request_approval",
    ]]
    assert adapter.calls[0].initial_tool_choice == "inspect_analysis_artifact"
    assert adapter.calls[0].required_tool_name == "inspect_analysis_artifact"
    persisted = ledger.get_item_by_operation_key(
        "thread-tools", "tool-selection:operation-tools"
    )
    assert persisted is not None
    assert persisted.item_type == "tool_selection"
    assert persisted.customer_visible is False
    assert persisted.payload["sdk_replay"] is False
    assert persisted.payload["tool_selection"]["schemaVersion"] == (
        "agent-turn-action-binding.v1"
    )
    assert persisted.payload["tool_selection"]["initialAction"] == "call_tool"
    assert persisted.payload["tool_selection"]["requiredToolName"] == (
        "inspect_analysis_artifact"
    )


def test_runner_receives_authoritative_clarification_topics() -> None:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-clarification-topics")
    adapter = MainAdapter()
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=AgentContextAssembler(
            ledger=ledger,
            artifact_index=InMemoryArtifactIndex(),
        ),
        adapter=adapter,
        tool_resolver=DynamicAgentToolResolver(
            generator=SelectionGenerator(
                [],
                initial_action="ask_user",
                required_tool_name="ask_user",
                material_decision_topics=["comparison_scope"],
            ),
            mandatory_tool_names=["ask_user", "request_approval"],
        ),
    )

    asyncio.run(
        runtime.run(
            AgentTurnRequest(
                thread_id="thread-clarification-topics",
                run_id="run-clarification-topics",
                operation_id="operation-clarification-topics",
                user_item_id="message-clarification-topics",
                user_message="解释已经发布的材料",
                expected_state_version=0,
                instructions="只澄清权威 action binding 中的未决主题。",
                tools=_candidates(),
                permission_scope={"analysis_access": "single"},
            )
        )
    )

    instructions = adapter.calls[0].instructions
    assert '"materialDecisionTopics":["comparison_scope"]' in instructions
    assert "must resolve only the listed materialDecisionTopics" in instructions


def test_required_action_cannot_complete_without_the_bound_tool_call() -> None:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-required-tool")
    adapter = PassiveAdapter()
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=AgentContextAssembler(
            ledger=ledger,
            artifact_index=InMemoryArtifactIndex(),
        ),
        adapter=adapter,
        tool_resolver=DynamicAgentToolResolver(
            generator=SelectionGenerator(["run_bi_analysis"]),
            mandatory_tool_names=["ask_user", "request_approval"],
        ),
    )

    result = asyncio.run(
        runtime.run(
            AgentTurnRequest(
                thread_id="thread-required-tool",
                run_id="run-required-tool",
                operation_id="operation-required-tool",
                user_item_id="message-required-tool",
                user_message="解释已经发布的材料",
                expected_state_version=0,
                instructions="需要新证据时调用分析工具。",
                tools=_candidates(),
                permission_scope={"analysis_access": "single"},
            )
        )
    )

    assert adapter.calls[0].initial_tool_choice == "run_bi_analysis"
    assert result.status == "failed"
    assert result.error_code == "agent_required_tool_call_missing"
    assert result.thread_head.customer_state == "idle"
    assert result.customer_projection()["completionKind"] == "failed_turn"


def test_direct_action_forbids_tools_and_commits_a_direct_terminal() -> None:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-direct-action")
    adapter = PassiveAdapter()
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=AgentContextAssembler(
            ledger=ledger,
            artifact_index=InMemoryArtifactIndex(),
        ),
        adapter=adapter,
        tool_resolver=DynamicAgentToolResolver(
            generator=SelectionGenerator([]),
            mandatory_tool_names=["ask_user", "request_approval"],
        ),
    )

    result = asyncio.run(
        runtime.run(
            AgentTurnRequest(
                thread_id="thread-direct-action",
                run_id="run-direct-action",
                operation_id="operation-direct-action",
                user_item_id="message-direct-action",
                user_message="解释已经发布的材料",
                expected_state_version=0,
                instructions="可以直接回答普通问题。",
                tools=_candidates(),
                permission_scope={"analysis_access": "single"},
            )
        )
    )

    assert adapter.calls[0].initial_tool_choice == "none"
    assert result.status == "completed"
    assert result.customer_projection()["completionKind"] == "direct_response"


def test_capability_catalog_comes_from_reviewed_runtime_binding() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    tool = capability_catalog_tool(registry)

    result = tool.handler({})

    assert result.status == "succeeded"
    assert result.output["contractVersion"] == registry.contract_version
    assert len(result.output["analysisCapabilities"]) == len(
        registry.analysis_axis_ids
    )
    assert all(
        set(item) == {"capabilityRef", "businessName", "semantics"}
        for item in result.output["analysisCapabilities"]
    )
    assert "contracts/" not in str(result.output)


def test_descriptive_outlier_method_is_owned_by_capability_contract() -> None:
    instructions = " ".join(TOOL_DISCOVERY_INSTRUCTIONS.split())
    assert "descriptive outlier, concentration, robustness" in instructions
    assert "Do not ask the customer to choose an evidence type" in instructions


def test_named_reference_population_does_not_reopen_baseline() -> None:
    instructions = " ".join(TOOL_DISCOVERY_INSTRUCTIONS.split())
    assert "reference population as a bound baseline" in instructions
    assert "belongs to comparison_scope only" in instructions
    assert "multi-member reference population" in instructions
    assert "You must choose ask_user for that scope" in instructions


def test_causal_reference_and_business_scope_are_distinct_topics() -> None:
    instructions = " ".join(TOOL_DISCOVERY_INSTRUCTIONS.split())
    assert "causal reference condition" in instructions
    assert "Do not encode one missing causal reference as both topics" in instructions
    assert "mutually exclusive in one action binding" in instructions
    assert "CAUSAL TOPIC EXCLUSIVITY IS MANDATORY" in instructions
    assert "counterfactual involves a comparison" in instructions
    assert "scope of channels, regions, products, or customer segments" in instructions


def test_business_clock_drives_low_risk_calendar_inference() -> None:
    instructions = " ".join(TOOL_DISCOVERY_INSTRUCTIONS.split())
    assert "conversationContext.businessClock" in instructions
    assert "most-recent completed occurrence" in instructions
    assert "downstream decision and plan authority must persist that inference" in instructions


def test_resolved_clarification_topics_are_authoritative_action_context() -> None:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-resolved-topic")
    ledger.append_items(
        "thread-resolved-topic",
        [
            NewThreadItem(
                item_id="selection-resolved-topic",
                item_type="tool_selection",
                role="system",
                text="",
                operation_key="tool-selection:resolved-topic",
                customer_visible=False,
                payload={
                    "tool_selection": {
                        "materialDecisionTopics": ["comparison_scope"],
                    }
                },
            ),
            NewThreadItem(
                item_id="clarification-resolved-topic",
                item_type="clarification",
                role="assistant",
                text="请选择比较渠道。",
                operation_key="suspension:resolved-topic",
                customer_visible=True,
                payload={
                    "pending_action": {
                        "actionRef": "pending-action:resolved-topic",
                    }
                },
            ),
            NewThreadItem(
                item_id="answer-resolved-topic",
                item_type="user_message",
                role="user",
                text="对比所有其他渠道",
                operation_key="user:resolved-topic-answer",
                customer_visible=True,
                payload={
                    "resolved_pending_action": {
                        "actionRef": "pending-action:resolved-topic",
                    },
                    "pending_action_resolution": {
                        "decision": "answered",
                        "answerText": "对比所有其他渠道",
                    },
                },
            ),
        ],
    )
    snapshot = AgentContextAssembler(
        ledger=ledger,
        artifact_index=InMemoryArtifactIndex(),
    ).assemble("thread-resolved-topic")

    context = _action_context(snapshot)

    assert context["resolvedPendingActions"][-1][
        "resolvedMaterialDecisionTopics"
    ] == ["comparison_scope"]
    instructions = " ".join(TOOL_DISCOVERY_INSTRUCTIONS.split())
    assert "resolvedPendingActions as accepted authority" in instructions
    assert "do not ask a second confirmation question" in instructions
