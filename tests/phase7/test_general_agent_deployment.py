from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from bi_agent.runtime.agent_sdk_contracts import (
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentStreamEvent,
)
from bi_agent.runtime.agents_sdk_trace import InMemoryAgentTraceSink
from bi_agent.runtime.general_agent_deployment import (
    DEPLOYMENT_CONTRACT_VERSION,
    DeploymentDatabaseAuditor,
    DeploymentValidationError,
    GeneralAgentLiveDeploymentProbe,
    REQUIRED_DEPENDENCY_VERSIONS,
    repository_deployment_checks,
    validate_general_agent_deployment,
    write_deployment_report,
)
from bi_agent.runtime.mainland_model_provider import (
    MainlandModelCapabilities,
    MainlandModelSettings,
    MainlandProviderConfig,
)
from tools.runtime.cutover_single_authority_schema import (
    SINGLE_AUTHORITY_MIGRATION_DIGEST,
    SINGLE_AUTHORITY_MIGRATION_ID,
)
from tools.runtime.validate_general_agent_deployment import main


class _Rows:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def fetchall(self) -> list[object]:
        return list(self._values)

    def fetchone(self) -> object | None:
        return self._values[0] if self._values else None


class _DatabaseConnection:
    def __init__(
        self,
        *,
        omit_trigger: bool = False,
        migration_rows: list[object] | None = None,
    ) -> None:
        self.omit_trigger = omit_trigger
        self.migration_rows = migration_rows
        self.statements: list[str] = []
        self.rolled_back = False

    def execute(
        self,
        statement: str,
        _params: Mapping[str, Any] | None = None,
    ) -> _Rows:
        self.statements.append(statement)
        if statement.startswith("BEGIN TRANSACTION"):
            return _Rows([])
        if "FROM waje_runtime.schema_migrations" in statement:
            if self.migration_rows is not None:
                return _Rows(self.migration_rows)
            return _Rows(
                [(SINGLE_AUTHORITY_MIGRATION_ID, SINGLE_AUTHORITY_MIGRATION_DIGEST)]
            )
        if "FROM information_schema.tables" in statement:
            return _Rows(
                [
                    ("agent_generated_artifacts",),
                    ("agent_thread_summaries",),
                    ("conversation_messages",),
                    ("investigation_threads",),
                    ("schema_migrations",),
                ]
            )
        if "FROM pg_trigger" in statement:
            rows: list[object] = [
                (
                    "agent_thread_summaries",
                    "agent_thread_summaries_append_only",
                    "O",
                )
            ]
            if not self.omit_trigger:
                rows.append(
                    (
                        "agent_generated_artifacts",
                        "agent_generated_artifacts_append_only",
                        "O",
                    )
                )
            return _Rows(rows)
        if "FROM pg_constraint" in statement:
            return _Rows(
                [
                    (
                        "CHECK ((item_type = ANY (ARRAY['message'::text, "
                        "'tool_selection'::text])))",
                    )
                ]
            )
        raise AssertionError(statement)

    def rollback(self) -> None:
        self.rolled_back = True


class _FakeProvider:
    transport = "openai_compatible_chat_completions"

    def __init__(self) -> None:
        self.config = MainlandProviderConfig(
            provider="test-mainland",
            base_url="https://model.provider.example.cn/v1",
            api_key="deployment-test-key",
            model="deployment-test-model",
            model_settings=MainlandModelSettings(
                max_output_tokens=512,
                thinking="enabled",
            ),
            capabilities=MainlandModelCapabilities(
                text_generation=True,
                function_calling=True,
                structured_output=True,
                streaming_text=True,
                streaming_tool_calls=True,
                typed_error_mapping=True,
                context_window_tokens=32_768,
                max_output_tokens=4_096,
                thinking=True,
            ),
        )
        self.thinking_observed = True

    def reset_probe_observations(self) -> None:
        self.thinking_observed = True


class _FakeLiveAdapter:
    def __init__(self, sink: InMemoryAgentTraceSink) -> None:
        self.sink = sink

    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
        self._trace(request.run_id)
        if request.run_id == "provider-probe:text":
            output: str | Mapping[str, Any] = "WAJE_TEXT_PROBE_OK"
        elif request.run_id == "provider-probe:structured":
            output = {"marker": "WAJE_STRUCTURED_PROBE_OK", "ok": True}
        elif request.agent_name == "WAJE Thread Context Compactor":
            payload = json.loads(request.input_text)
            output = {
                "statements": [
                    {
                        "statementId": "deployment-goal",
                        "kind": "user_goal",
                        "text": "保留部署验收目标。",
                        "sourceRefs": [payload["sourceItems"][0]["item_id"]],
                    }
                ]
            }
        elif request.agent_name == "WAJE Dynamic Tool Discovery":
            output = {"selectedTools": ["list_available_capabilities"]}
        elif request.agent_name == "WAJE Controlled Investigation Agent":
            payload = json.loads(request.input_text)
            output = {
                "title": "引用闭包复核",
                "summary": "受控子任务引用闭包有效。",
                "findings": [
                    {
                        "text": "结论只覆盖给定材料。",
                        "sourceRefs": [payload["allowedSourceRefs"][0]],
                    }
                ],
                "limitationRefs": [],
            }
        else:
            raise AssertionError(request.run_id)
        return WajeAgentRunResult(
            run_id=request.run_id,
            final_output=output,
            usage={"input_tokens": 5, "output_tokens": 3},
            model_turns=1,
        )

    async def run_streamed(
        self,
        request: WajeAgentRunRequest,
    ) -> WajeAgentRunResult:
        self._trace(request.run_id)
        if request.run_id == "provider-probe:stream-tool":
            result = request.tools[0].handler({"marker": "WAJE_TOOL_PROBE_OK"})
            if asyncio.iscoroutine(result):
                await result
            events = (
                WajeAgentStreamEvent(kind="tool_call_delta", delta="{}"),
                WajeAgentStreamEvent(kind="tool_called", tool_name="probe_echo"),
            )
            output = "WAJE_TOOL_PROBE_OK"
        elif request.run_id == "provider-probe:stream-text":
            events = (
                WajeAgentStreamEvent(
                    kind="model_text_delta",
                    delta="WAJE_STREAM_PROBE_OK",
                ),
            )
            output = "WAJE_STREAM_PROBE_OK"
        else:
            raise AssertionError(request.run_id)
        return WajeAgentRunResult(
            run_id=request.run_id,
            final_output=output,
            usage={"input_tokens": 5, "output_tokens": 3},
            model_turns=1,
            stream_events=events,
        )

    def _trace(self, run_id: str) -> None:
        trace_id = "trace_" + run_id.replace(":", "_")
        for event_type in ("trace_started", "trace_finished"):
            self.sink.write_trace_record(
                {
                    "schema_version": "waje-agent-trace.v1",
                    "event_type": event_type,
                    "id": trace_id,
                    "waje_trace_metadata": {"waje_run_id": run_id},
                }
            )


def test_repository_deployment_contract_is_current_and_content_addressed() -> None:
    checks = repository_deployment_checks()

    assert REQUIRED_DEPENDENCY_VERSIONS == {
        "openai": "2.44.0",
        "openai-agents": "0.8.4",
    }
    assert {item.name for item in checks} == {
        "dependency_lock",
        "schema_contract",
        "release_manifest",
    }
    assert all(item.status == "passed" for item in checks)


def test_database_deployment_audit_is_read_only_and_closes_v12_contract() -> None:
    connection = _DatabaseConnection()

    check = DeploymentDatabaseAuditor(connection).run()

    assert check.status == "passed"
    assert check.detail["migrationId"] == SINGLE_AUTHORITY_MIGRATION_ID
    assert all(
        token not in statement.upper()
        for statement in connection.statements
        for token in ("INSERT ", "UPDATE ", "DELETE ", "ALTER ", "DROP ")
    )


def test_database_deployment_audit_rejects_missing_append_only_trigger() -> None:
    with pytest.raises(
        DeploymentValidationError,
        match="deployment_database_append_only_trigger_invalid",
    ):
        DeploymentDatabaseAuditor(_DatabaseConnection(omit_trigger=True)).run()


def test_database_deployment_audit_rejects_superseded_migration_rows() -> None:
    connection = _DatabaseConnection(
        migration_rows=[
            ("single-authority-workflow.v11", "a" * 64),
            (SINGLE_AUTHORITY_MIGRATION_ID, SINGLE_AUTHORITY_MIGRATION_DIGEST),
        ]
    )

    with pytest.raises(
        DeploymentValidationError,
        match="deployment_database_migration_invalid",
    ):
        DeploymentDatabaseAuditor(connection).run()


def test_live_deployment_probe_covers_provider_p2_and_waje_trace() -> None:
    sink = InMemoryAgentTraceSink()
    checks = asyncio.run(
        GeneralAgentLiveDeploymentProbe(
            provider=_FakeProvider(),
            adapter=_FakeLiveAdapter(sink),
            trace_sink=sink,
        ).run()
    )

    assert {item.name for item in checks} == {
        "mainland_provider_capabilities",
        "p2_live_runtime",
        "waje_trace_boundary",
    }
    assert all(item.status == "passed" for item in checks)
    trace = next(item for item in checks if item.name == "waje_trace_boundary")
    assert trace.detail["openaiExporterUsed"] is False
    assert trace.detail["recordCount"] == 14
    assert trace.detail["traceCount"] == 7
    p2 = next(item for item in checks if item.name == "p2_live_runtime")
    assert p2.detail["selectedTools"] == [
        "ask_user",
        "list_available_capabilities",
    ]


def test_deployment_report_maps_missing_live_config_without_secret_fallback() -> None:
    report = asyncio.run(
        validate_general_agent_deployment(
            environ={"OPENAI_API_KEY": "must-not-be-used"},
            include_live_provider=True,
        )
    )

    assert report.schema_version == DEPLOYMENT_CONTRACT_VERSION
    assert report.status == "failed"
    failed = next(item for item in report.checks if item.status == "failed")
    assert failed.name == "live_mainland_runtime"
    assert failed.detail["errorCode"] == "missing_llm_provider"
    assert "must-not-be-used" not in json.dumps(report.to_contract())


def test_database_validation_uses_read_only_transaction_and_report_writer(
    tmp_path: Path,
) -> None:
    connection = _DatabaseConnection()
    report = asyncio.run(
        validate_general_agent_deployment(
            include_database=True,
            database_connection=connection,
        )
    )
    output = tmp_path / "deployment-report.json"
    write_deployment_report(report, output)

    assert report.status == "passed"
    assert connection.statements[0].startswith("BEGIN TRANSACTION")
    assert connection.rolled_back is True
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "passed"


def test_repository_only_cli_returns_machine_readable_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemaVersion"] == DEPLOYMENT_CONTRACT_VERSION
    assert payload["status"] == "passed"
