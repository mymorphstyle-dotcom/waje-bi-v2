from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bi_agent.runtime.agent_sdk_contracts import (
    AgentToolResult,
    WajeAgentRunRequest,
    WajeAgentRunResult,
)
from bi_agent.runtime.analysis_artifacts import (
    ArtifactDescriptor,
    InMemoryAnalysisArtifactRegistry,
)
from bi_agent.runtime.controlled_subagent_tools import (
    CONTROLLED_SUBAGENT_ARTIFACT_VERSION,
    ControlledSubAgentOutput,
    InMemoryGeneratedArtifactWriter,
    PostgresGeneratedArtifactWriter,
    controlled_subagent_tool,
)


ROOT = Path(__file__).resolve().parents[2]


class _ArtifactRows:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def fetchone(self) -> object | None:
        return self._value


class _GeneratedArtifactConnection:
    def __init__(self) -> None:
        self.persisted: tuple[object, ...] | None = None
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement: str, params: dict[str, object]) -> _ArtifactRows:
        self.statements.append(statement)
        if "INSERT INTO waje_runtime.agent_generated_artifacts" in statement:
            detail = json.loads(str(params["detail"]))
            source_refs = json.loads(str(params["source_refs"]))
            self.persisted = (
                params["artifact_ref"],
                params["artifact_type"],
                params["artifact_version"],
                params["content_digest"],
                source_refs,
                params["visibility_policy_ref"],
                params["customer_summary"],
                detail,
                datetime(2026, 7, 21, tzinfo=timezone.utc),
            )
            return _ArtifactRows(self.persisted)
        if "FROM waje_runtime.agent_generated_artifacts" in statement:
            return _ArtifactRows(self.persisted)
        raise AssertionError(statement)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class ConcurrentSubAgentAdapter:
    def __init__(self, *, invented_source: bool = False) -> None:
        self.calls: list[WajeAgentRunRequest] = []
        self.active = 0
        self.max_active = 0
        self._release = asyncio.Event()
        self._invented_source = invented_source

    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
        self.calls.append(request)
        assert request.output_type is ControlledSubAgentOutput
        assert request.tools == ()
        assert request.max_turns == 1
        payload = json.loads(request.input_text)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == 2:
            self._release.set()
        await asyncio.wait_for(self._release.wait(), timeout=1)
        await asyncio.sleep(0)
        self.active -= 1
        source_ref = (
            "invented:source"
            if self._invented_source
            else payload["allowedSourceRefs"][0]
        )
        investigation = payload["investigation"]
        return WajeAgentRunResult(
            run_id=request.run_id,
            final_output={
                "title": investigation["investigationId"],
                "summary": f"已完成 {investigation['outputKind']}。",
                "findings": [
                    {
                        "text": "该判断仅覆盖给定材料。",
                        "sourceRefs": [source_ref],
                    }
                ],
                "limitationRefs": [],
            },
            usage={"input_tokens": 10, "output_tokens": 5},
            model_turns=1,
        )


def test_controlled_subagents_run_concurrently_and_persist_closed_artifacts() -> None:
    async def scenario() -> tuple[
        AgentToolResult,
        ConcurrentSubAgentAdapter,
        InMemoryAnalysisArtifactRegistry,
    ]:
        registry = _registry()
        adapter = ConcurrentSubAgentAdapter()
        tool = controlled_subagent_tool(
            adapter=adapter,
            registry=registry,
            writer=InMemoryGeneratedArtifactWriter(registry),
            thread_id="thread-1",
            operation_id="operation-1",
        )
        result = await tool.handler(
            {
                "investigations": [
                    {
                        "investigationId": "review-evidence",
                        "task": "独立复核证据边界。",
                        "outputKind": "evidence_review",
                        "sourceArtifactRefs": ["artifact:one"],
                    },
                    {
                        "investigationId": "audit-quality",
                        "task": "独立检查材料质量。",
                        "outputKind": "quality_audit",
                        "sourceArtifactRefs": ["artifact:two"],
                    },
                ]
            }
        )
        assert isinstance(result, AgentToolResult)
        return result, adapter, registry

    result, adapter, registry = asyncio.run(scenario())

    assert adapter.max_active == 2
    assert result.status == "succeeded"
    assert len(result.artifact_refs) == 2
    assert set(result.material_refs) == {
        "artifact:one",
        "artifact:two",
        *result.artifact_refs,
    }
    for artifact_ref in result.artifact_refs:
        persisted = registry.inspect("thread-1", artifact_ref)
        assert persisted is not None
        assert persisted.descriptor.artifact_type == "controlled_subagent_result"
        assert persisted.descriptor.version == CONTROLLED_SUBAGENT_ARTIFACT_VERSION
        assert persisted.descriptor.visibility_policy_ref == "visibility:customer-safe"
        allowed = {
            ref
            for source_ref in persisted.descriptor.source_refs
            for ref in (
                source_ref,
                *(_registry().inspect("thread-1", source_ref).descriptor.source_refs),
            )
        }
        finding_refs = {
            ref
            for finding in persisted.detail["findings"]
            for ref in finding["sourceRefs"]
        }
        assert finding_refs <= allowed


def test_controlled_subagent_rejects_missing_source_before_model_call() -> None:
    async def scenario() -> int:
        registry = _registry()
        adapter = ConcurrentSubAgentAdapter()
        tool = controlled_subagent_tool(
            adapter=adapter,
            registry=registry,
            writer=InMemoryGeneratedArtifactWriter(registry),
            thread_id="thread-1",
            operation_id="operation-1",
        )
        with pytest.raises(ValueError, match="controlled_subagent_source_artifact_missing"):
            await tool.handler(
                {
                    "investigations": [
                        {
                            "investigationId": "missing",
                            "task": "复核缺失材料。",
                            "outputKind": "evidence_review",
                            "sourceArtifactRefs": ["artifact:missing"],
                        }
                    ]
                }
            )
        return len(adapter.calls)

    assert asyncio.run(scenario()) == 0


def test_controlled_subagent_rejects_invented_source_without_persisting() -> None:
    async def scenario() -> tuple[int, tuple[ArtifactDescriptor, ...]]:
        registry = _registry()
        adapter = ConcurrentSubAgentAdapter(invented_source=True)
        adapter._release.set()
        tool = controlled_subagent_tool(
            adapter=adapter,
            registry=registry,
            writer=InMemoryGeneratedArtifactWriter(registry),
            thread_id="thread-1",
            operation_id="operation-1",
        )
        with pytest.raises(ValueError, match="controlled_subagent_finding_source_unknown"):
            await tool.handler(
                {
                    "investigations": [
                        {
                            "investigationId": "invented",
                            "task": "检查引用闭包。",
                            "outputKind": "quality_audit",
                            "sourceArtifactRefs": ["artifact:one"],
                        }
                    ]
                }
            )
        return len(adapter.calls), registry.list_artifacts("thread-1", limit=10)

    calls, artifacts = asyncio.run(scenario())
    assert calls == 1
    assert {item.artifact_type for item in artifacts} == {"bi_publication"}


def test_postgres_generated_artifact_writer_persists_customer_safe_contract() -> None:
    connection = _GeneratedArtifactConnection()
    descriptor = ArtifactDescriptor(
        artifact_ref="subagent-artifact:sha256:abc",
        artifact_type="controlled_subagent_result",
        version=CONTROLLED_SUBAGENT_ARTIFACT_VERSION,
        digest="a" * 64,
        source_refs=("artifact:one",),
        visibility_policy_ref="visibility:customer-safe",
        customer_summary="独立复核完成。",
        created_at=datetime(2026, 7, 21, tzinfo=timezone.utc).isoformat(),
    )
    detail = {
        "schemaVersion": CONTROLLED_SUBAGENT_ARTIFACT_VERSION,
        "findings": [
            {"text": "只覆盖给定材料。", "sourceRefs": ["artifact:one"]}
        ],
    }

    persisted = PostgresGeneratedArtifactWriter(connection).register(
        thread_id="thread-1",
        operation_id="operation-1",
        descriptor=descriptor,
        detail=detail,
    )

    assert persisted.descriptor.artifact_ref == descriptor.artifact_ref
    assert persisted.descriptor.source_refs == descriptor.source_refs
    assert persisted.detail == detail
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert "ON CONFLICT (thread_id, artifact_ref)" in connection.statements[0]


def test_controlled_subagent_has_no_thread_authority_and_is_wired_dynamically() -> None:
    source = (ROOT / "bi_agent/runtime/controlled_subagent_tools.py").read_text(
        encoding="utf-8"
    )
    entry = (ROOT / "bi_agent/runtime/general_agent_entry.py").read_text(
        encoding="utf-8"
    )
    ddl = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
        encoding="utf-8"
    )

    assert "ThreadHead" not in source
    assert "conversation_messages" not in source
    assert "controlled_subagent_tool(" in entry
    assert "DynamicAgentToolResolver" in entry
    assert "agent_generated_artifacts" in ddl
    assert "PRIMARY KEY(thread_id, artifact_ref)" in ddl
    assert "'agent_generated_artifacts'" in ddl


def _registry() -> InMemoryAnalysisArtifactRegistry:
    registry = InMemoryAnalysisArtifactRegistry()
    created_at = datetime(2026, 7, 21, tzinfo=timezone.utc).isoformat()
    for suffix in ("one", "two"):
        registry.add(
            "thread-1",
            ArtifactDescriptor(
                artifact_ref=f"artifact:{suffix}",
                artifact_type="bi_publication",
                version="publication.v1",
                digest=suffix * 8,
                source_refs=(f"evidence:{suffix}",),
                visibility_policy_ref="visibility:customer-safe",
                customer_summary=f"材料 {suffix}",
                created_at=created_at,
            ),
            {
                "answerMarkdown": f"材料 {suffix}",
                "materialRefs": [f"evidence:{suffix}"],
                "limitationRefs": [],
            },
        )
    return registry
