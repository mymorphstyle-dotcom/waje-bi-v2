from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from bi_agent.runtime.agent_context import AgentContextAssembler, InMemoryArtifactIndex
from bi_agent.runtime.agent_sdk_contracts import (
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentTool,
)
from bi_agent.runtime.agent_tool_discovery import (
    AgentToolDiscoveryError,
    DynamicAgentToolResolver,
    DynamicToolSelectionOutput,
)
from bi_agent.runtime.agent_turn_runtime import AgentTurnRequest, AgentTurnRuntime
from bi_agent.runtime.capability_catalog_tool import capability_catalog_tool
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.thread_item_ledger import InMemoryThreadItemLedger


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
    def __init__(self, selected: list[str]) -> None:
        self.selected = selected
        self.calls = 0
        self.catalog_names: list[str] = []

    async def select(
        self,
        *,
        user_message: str,
        tool_catalog: object,
        permission_scope: object,
    ) -> DynamicToolSelectionOutput:
        assert user_message == "解释已经发布的材料"
        assert permission_scope == {"analysis_access": "single"}
        self.calls += 1
        self.catalog_names = [item["name"] for item in tool_catalog]  # type: ignore[index]
        return DynamicToolSelectionOutput(selectedTools=self.selected)


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
        return WajeAgentRunResult(
            run_id=request.run_id,
            final_output={
                "answerMarkdown": "已按动态工具集完成回答。",
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
    assert replay.replayed is True
    assert selector.calls == 1
    assert [[tool.name for tool in call.tools] for call in adapter.calls] == [[
        "inspect_analysis_artifact",
        "ask_user",
        "request_approval",
    ]]
    persisted = ledger.get_item_by_operation_key(
        "thread-tools", "tool-selection:operation-tools"
    )
    assert persisted is not None
    assert persisted.item_type == "tool_selection"
    assert persisted.customer_visible is False
    assert persisted.payload["sdk_replay"] is False


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
