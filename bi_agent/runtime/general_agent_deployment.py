from __future__ import annotations

from datetime import datetime, timezone
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from bi_agent.runtime.agent_context import AgentContextAssembler, InMemoryArtifactIndex
from bi_agent.runtime.agent_context_compactor import (
    ThreadSummaryGenerationInput,
    WajeThreadSummaryGenerator,
)
from bi_agent.runtime.agent_sdk_contracts import (
    AgentToolResult,
    WajeAgentRunResult,
    WajeAgentTool,
)
from bi_agent.runtime.agent_tool_discovery import (
    DynamicAgentToolResolver,
    WajeToolSelectionGenerator,
)
from bi_agent.runtime.agent_turn_runtime import AgentTurnRequest, AgentTurnRuntime
from bi_agent.runtime.agents_sdk_adapter import WajeAgentsSdkAdapter
from bi_agent.runtime.agents_sdk_trace import InMemoryAgentTraceSink
from bi_agent.runtime.analysis_artifacts import (
    ArtifactDescriptor,
    InMemoryAnalysisArtifactRegistry,
)
from bi_agent.runtime.controlled_subagent_tools import (
    InMemoryGeneratedArtifactWriter,
    controlled_subagent_tool,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.llm_client import LLMConfigurationError, LLMProviderError
from bi_agent.runtime.mainland_model_provider import MainlandModelProvider
from bi_agent.runtime.provider_capability_probe import ProviderCapabilityProbe
from bi_agent.runtime.release_manifest import (
    load_release_manifest,
    validate_release_manifest,
)
from bi_agent.runtime.thread_context_summary import (
    ThreadSummarySourceItem,
    VersionedThreadSummary,
)
from bi_agent.runtime.thread_item_ledger import (
    InMemoryThreadItemLedger,
    NewThreadItem,
)
from tools.runtime.cutover_single_authority_schema import (
    SINGLE_AUTHORITY_MIGRATION_DIGEST,
    SINGLE_AUTHORITY_MIGRATION_ID,
    SchemaCutoverError,
    _schema_contract,
)


DEPLOYMENT_CONTRACT_VERSION = "general-agent-deployment.v1"
REQUIRED_DEPENDENCY_VERSIONS = {
    "openai": "2.44.0",
    "openai-agents": "0.8.4",
}
REQUIRED_DEPLOYMENT_TABLES = frozenset(
    {
        "agent_thread_summaries",
        "agent_generated_artifacts",
        "conversation_messages",
        "investigation_threads",
        "schema_migrations",
    }
)
REQUIRED_APPEND_ONLY_TABLES = frozenset(
    {"agent_thread_summaries", "agent_generated_artifacts"}
)


class DeploymentValidationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DeploymentCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    name: str = Field(min_length=1)
    status: Literal["passed", "failed", "skipped"]
    detail: dict[str, Any] = Field(default_factory=dict)


class GeneralAgentDeploymentReport(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["general-agent-deployment.v1"] = Field(
        alias="schemaVersion",
        default=DEPLOYMENT_CONTRACT_VERSION,
    )
    status: Literal["passed", "failed"]
    generated_at: str = Field(alias="generatedAt", min_length=1)
    checks: list[DeploymentCheckResult] = Field(min_length=1)

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _LiveProvider(Protocol):
    config: Any
    transport: str
    thinking_observed: bool

    def reset_probe_observations(self) -> None: ...


class _LiveAdapter(Protocol):
    async def run(self, request: Any) -> WajeAgentRunResult: ...

    async def run_streamed(self, request: Any) -> WajeAgentRunResult: ...


def repository_deployment_checks() -> tuple[DeploymentCheckResult, ...]:
    dependency_versions = {
        name: package_version(name) for name in REQUIRED_DEPENDENCY_VERSIONS
    }
    if dependency_versions != REQUIRED_DEPENDENCY_VERSIONS:
        raise DeploymentValidationError("deployment_dependency_version_conflict")
    _schema_contract()
    manifest_problems = validate_release_manifest(load_release_manifest())
    if manifest_problems:
        raise DeploymentValidationError("deployment_release_manifest_invalid")
    return (
        DeploymentCheckResult(
            name="dependency_lock",
            status="passed",
            detail={"versions": dependency_versions},
        ),
        DeploymentCheckResult(
            name="schema_contract",
            status="passed",
            detail={
                "migrationId": SINGLE_AUTHORITY_MIGRATION_ID,
                "migrationDigest": SINGLE_AUTHORITY_MIGRATION_DIGEST,
            },
        ),
        DeploymentCheckResult(
            name="release_manifest",
            status="passed",
            detail={"manifestVersion": load_release_manifest()["manifest_version"]},
        ),
    )


class DeploymentDatabaseAuditor:
    """Read-only deployment audit over the current PostgreSQL authority."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def run(self) -> DeploymentCheckResult:
        migration_rows = self.connection.execute(
            """
            SELECT migration_id, migration_digest
            FROM waje_runtime.schema_migrations
            WHERE migration_id LIKE 'single-authority-%'
            ORDER BY migration_id
            """,
        ).fetchall()
        expected_migration = [
            (SINGLE_AUTHORITY_MIGRATION_ID, SINGLE_AUTHORITY_MIGRATION_DIGEST)
        ]
        normalized_migrations = [
            (
                str(_field(row, "migration_id", 0)),
                str(_field(row, "migration_digest", 1)),
            )
            for row in migration_rows
        ]
        if normalized_migrations != expected_migration:
            raise DeploymentValidationError("deployment_database_migration_invalid")

        table_rows = self.connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'waje_runtime'
              AND table_name = ANY(%(table_names)s)
            """,
            {"table_names": sorted(REQUIRED_DEPLOYMENT_TABLES)},
        ).fetchall()
        present_tables = {
            str(_field(row, "table_name", 0)) for row in table_rows
        }
        if present_tables != REQUIRED_DEPLOYMENT_TABLES:
            raise DeploymentValidationError("deployment_database_tables_missing")

        trigger_rows = self.connection.execute(
            """
            SELECT relation.relname AS table_name,
                   trigger.tgname AS trigger_name,
                   trigger.tgenabled
            FROM pg_trigger trigger
            JOIN pg_class relation ON relation.oid = trigger.tgrelid
            JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'waje_runtime'
              AND relation.relname = ANY(%(table_names)s)
              AND NOT trigger.tgisinternal
            """,
            {"table_names": sorted(REQUIRED_APPEND_ONLY_TABLES)},
        ).fetchall()
        enabled_triggers = {
            str(_field(row, "table_name", 0)): (
                str(_field(row, "trigger_name", 1)),
                str(_field(row, "tgenabled", 2)),
            )
            for row in trigger_rows
        }
        if any(
            enabled_triggers.get(table)
            != (f"{table}_append_only", "O")
            for table in REQUIRED_APPEND_ONLY_TABLES
        ):
            raise DeploymentValidationError(
                "deployment_database_append_only_trigger_invalid"
            )

        constraint_row = self.connection.execute(
            """
            SELECT pg_get_constraintdef(constraint_record.oid) AS definition
            FROM pg_constraint constraint_record
            WHERE constraint_record.conrelid =
                  'waje_runtime.conversation_messages'::regclass
              AND constraint_record.conname =
                  'conversation_messages_item_type_check'
            """
        ).fetchone()
        definition = (
            str(_field(constraint_row, "definition", 0))
            if constraint_row is not None
            else ""
        )
        if "tool_selection" not in definition:
            raise DeploymentValidationError(
                "deployment_database_tool_selection_contract_invalid"
            )
        return DeploymentCheckResult(
            name="postgres_runtime_authority",
            status="passed",
            detail={
                "migrationId": SINGLE_AUTHORITY_MIGRATION_ID,
                "requiredTables": sorted(REQUIRED_DEPLOYMENT_TABLES),
                "appendOnlyTables": sorted(REQUIRED_APPEND_ONLY_TABLES),
                "transactionMode": "repeatable_read_read_only",
            },
        )


class GeneralAgentLiveDeploymentProbe:
    """Exercises the real mainland Runner path without PostgreSQL mutations."""

    def __init__(
        self,
        *,
        provider: _LiveProvider,
        adapter: _LiveAdapter,
        trace_sink: InMemoryAgentTraceSink,
    ) -> None:
        self._provider = provider
        self._adapter = adapter
        self._trace_sink = trace_sink

    async def run(self) -> tuple[DeploymentCheckResult, ...]:
        capability = await ProviderCapabilityProbe(
            provider=self._provider,  # type: ignore[arg-type]
            adapter=self._adapter,  # type: ignore[arg-type]
        ).run()
        summary = await self._summary_smoke()
        selected_tools = await self._tool_discovery_smoke()
        application_action = await self._application_action_smoke()
        delegated_artifact = await self._controlled_delegation_smoke()
        trace_detail = self._validate_local_trace()
        origin = urlparse(str(self._provider.config.base_url))
        return (
            DeploymentCheckResult(
                name="mainland_provider_capabilities",
                status="passed",
                detail={
                    "provider": capability.provider,
                    "model": capability.model,
                    "transport": capability.transport,
                    "checks": dict(capability.checks),
                    "contextWindowTokens": capability.context_window_tokens,
                    "maxOutputTokens": capability.max_output_tokens,
                    "outboundOrigin": f"{origin.scheme}://{origin.netloc}",
                    "openaiApiKeyUsed": False,
                },
            ),
            DeploymentCheckResult(
                name="p2_live_runtime",
                status="passed",
                detail={
                    "summaryRef": summary.summary_ref,
                    "selectedTools": list(selected_tools),
                    "applicationAction": application_action,
                    "delegatedArtifactRef": delegated_artifact,
                },
            ),
            DeploymentCheckResult(
                name="waje_trace_boundary",
                status="passed",
                detail=trace_detail,
            ),
        )

    async def _summary_smoke(self) -> VersionedThreadSummary:
        ledger = InMemoryThreadItemLedger()
        ledger.create_thread("deployment-probe-thread")
        ledger.append_items(
            "deployment-probe-thread",
            [
                NewThreadItem(
                    item_id="deployment-probe-message-1",
                    item_type="user_message",
                    role="user",
                    text="请保留这个部署验收目标。",
                    customer_visible=True,
                    payload={"sdk_replay": True},
                ),
                NewThreadItem(
                    item_id="deployment-probe-message-2",
                    item_type="assistant_message",
                    role="assistant",
                    text="已记录部署验收目标。",
                    customer_visible=True,
                    payload={"sdk_replay": True},
                ),
            ],
        )
        items = ledger.list_items("deployment-probe-thread")
        artifact = _probe_artifact()
        generation_input = ThreadSummaryGenerationInput(
            thread_id="deployment-probe-thread",
            previous_summary=None,
            source_items=items,
            artifacts=(artifact,),
        )
        content = await WajeThreadSummaryGenerator(self._adapter).generate(
            generation_input
        )
        return VersionedThreadSummary.create(
            thread_id="deployment-probe-thread",
            summary_version=1,
            source_items=[
                ThreadSummarySourceItem(
                    itemRef=item.item_id,
                    sequence=item.sequence,
                    itemDigest=item.item_digest,
                )
                for item in items
            ],
            authority_refs=generation_input.authority_refs,
            content=content,
        )

    async def _tool_discovery_smoke(self) -> tuple[str, ...]:
        tools = (
            _probe_tool("ask_user", "Keep the user-input safety path available."),
            _probe_tool(
                "list_available_capabilities",
                "List reviewed WAJE business analysis capabilities.",
            ),
            _probe_tool(
                "inspect_analysis_artifact",
                "Inspect one existing analysis artifact.",
            ),
        )
        resolver = DynamicAgentToolResolver(
            generator=WajeToolSelectionGenerator(self._adapter),
            mandatory_tool_names=("ask_user",),
            max_optional_tools=1,
        )
        user_message = (
            "For this deployment probe, select only list_available_capabilities "
            "from the optional tools."
        )
        resolved = await resolver.resolve(
            user_message=user_message,
            candidate_tools=tools,
            permission_scope={"probe": "read_only"},
        )
        names = tuple(tool.name for tool in resolved.tools)
        if names != ("ask_user", "list_available_capabilities"):
            raise DeploymentValidationError(
                "deployment_live_tool_discovery_unexpected"
            )
        replayed = resolver.replay(
            user_message=user_message,
            candidate_tools=tools,
            permission_scope={"probe": "read_only"},
            selection_payload=resolved.selection.to_contract(),
        )
        if tuple(tool.name for tool in replayed.tools) != names:
            raise DeploymentValidationError(
                "deployment_live_tool_discovery_replay_conflict"
            )
        return names

    async def _application_action_smoke(self) -> dict[str, Any]:
        ledger = InMemoryThreadItemLedger()
        ledger.create_thread("deployment-action-probe-thread")
        tools = (
            _probe_tool("ask_user", "Ask for material user input."),
            _probe_tool("request_approval", "Request approval for a controlled action."),
            _probe_tool(
                "list_available_capabilities",
                "List reviewed WAJE business analysis capabilities.",
            ),
        )
        runtime = AgentTurnRuntime(
            ledger=ledger,
            context_assembler=AgentContextAssembler(
                ledger=ledger,
                artifact_index=InMemoryArtifactIndex(),
            ),
            adapter=self._adapter,
            tool_resolver=DynamicAgentToolResolver(
                generator=WajeToolSelectionGenerator(self._adapter),
                mandatory_tool_names=("ask_user", "request_approval"),
                max_optional_tools=1,
            ),
        )
        result = await runtime.run(
            AgentTurnRequest(
                thread_id="deployment-action-probe-thread",
                run_id="deployment-action-probe-run",
                operation_id="deployment-action-probe-operation",
                user_item_id="deployment-action-probe-message",
                user_message=(
                    "List the available WAJE analysis capabilities using the capability tool."
                ),
                expected_state_version=0,
                instructions=(
                    "Call list_available_capabilities once, then return a concise typed "
                    "answer. Do not claim any BI analysis was completed."
                ),
                tools=tools,
                permission_scope={"probe": "read_only"},
                agent_name="WAJE Deployment Action Probe",
                max_turns=4,
            )
        )
        admission = result.terminal_admission
        if (
            result.status != "completed"
            or admission is None
            or admission.completion_kind != "tool_response"
            or admission.executed_tool_names != ["list_available_capabilities"]
        ):
            raise DeploymentValidationError(
                "deployment_live_application_action_invalid"
            )
        return {
            "status": result.status,
            "completionKind": admission.completion_kind,
            "executedToolNames": admission.executed_tool_names,
        }

    async def _controlled_delegation_smoke(self) -> str:
        registry = InMemoryAnalysisArtifactRegistry()
        artifact = _probe_artifact()
        registry.add(
            "deployment-probe-thread",
            artifact,
            {
                "answerMarkdown": "该材料只用于验证受控子任务引用闭包。",
                "materialRefs": list(artifact.source_refs),
                "limitationRefs": [],
            },
        )
        tool = controlled_subagent_tool(
            adapter=self._adapter,
            registry=registry,
            writer=InMemoryGeneratedArtifactWriter(registry),
            thread_id="deployment-probe-thread",
            operation_id="deployment-probe-operation",
        )
        result = await tool.handler(
            {
                "investigations": [
                    {
                        "investigationId": "deployment-source-closure",
                        "task": "只复核给定材料的引用边界。",
                        "outputKind": "quality_audit",
                        "sourceArtifactRefs": [artifact.artifact_ref],
                    }
                ]
            }
        )
        if not isinstance(result, AgentToolResult) or len(result.artifact_refs) != 1:
            raise DeploymentValidationError(
                "deployment_live_controlled_delegation_invalid"
            )
        artifact_ref = result.artifact_refs[0]
        if registry.inspect("deployment-probe-thread", artifact_ref) is None:
            raise DeploymentValidationError(
                "deployment_live_controlled_artifact_missing"
            )
        return artifact_ref

    def _validate_local_trace(self) -> dict[str, Any]:
        records = self._trace_sink.records
        if not records:
            raise DeploymentValidationError("deployment_live_trace_missing")
        if any(
            record.get("schema_version") != "waje-agent-trace.v1"
            for record in records
        ):
            raise DeploymentValidationError("deployment_live_trace_schema_invalid")
        serialized = json.dumps(
            canonical_value(records),
            ensure_ascii=True,
            sort_keys=True,
        ).lower()
        if "api.openai.com" in serialized:
            raise DeploymentValidationError("deployment_live_openai_trace_detected")
        event_types = sorted(
            {
                str(record.get("event_type") or "")
                for record in records
                if record.get("event_type")
            }
        )
        if not {"trace_started", "trace_finished"}.issubset(event_types):
            raise DeploymentValidationError("deployment_live_trace_incomplete")
        started_trace_ids = {
            str(record.get("id") or "")
            for record in records
            if record.get("event_type") == "trace_started"
        }
        finished_trace_ids = {
            str(record.get("id") or "")
            for record in records
            if record.get("event_type") == "trace_finished"
        }
        if (
            len(started_trace_ids) < 7
            or started_trace_ids != finished_trace_ids
            or "" in started_trace_ids
        ):
            raise DeploymentValidationError("deployment_live_trace_closure_invalid")
        return {
            "sink": "waje_in_memory_audit",
            "recordCount": len(records),
            "traceCount": len(started_trace_ids),
            "eventTypes": event_types,
            "openaiExporterUsed": False,
        }


async def validate_general_agent_deployment(
    *,
    environ: Mapping[str, str] | None = None,
    include_database: bool = False,
    include_live_provider: bool = False,
    database_connection: Any | None = None,
) -> GeneralAgentDeploymentReport:
    checks: list[DeploymentCheckResult] = []
    try:
        checks.extend(repository_deployment_checks())
    except Exception as exc:
        checks.append(_failed_check("repository_contract", exc))

    if include_database:
        owned_connection = database_connection is None
        connection = database_connection
        try:
            if connection is None:
                connection = _connect_database(environ)
            connection.execute(
                "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            checks.append(DeploymentDatabaseAuditor(connection).run())
        except Exception as exc:
            checks.append(_failed_check("postgres_runtime_authority", exc))
        finally:
            if connection is not None:
                connection.rollback()
                if owned_connection:
                    connection.close()

    if include_live_provider:
        provider: MainlandModelProvider | None = None
        try:
            provider_env = dict(os.environ if environ is None else environ)
            provider_env.pop("OPENAI_API_KEY", None)
            provider = MainlandModelProvider.deepseek_from_env(provider_env)
            trace_sink = InMemoryAgentTraceSink()
            adapter = WajeAgentsSdkAdapter(provider=provider, trace_sink=trace_sink)
            checks.extend(
                await GeneralAgentLiveDeploymentProbe(
                    provider=provider,
                    adapter=adapter,
                    trace_sink=trace_sink,
                ).run()
            )
        except Exception as exc:
            checks.append(_failed_check("live_mainland_runtime", exc))
        finally:
            if provider is not None:
                await provider.close()

    return GeneralAgentDeploymentReport(
        status=(
            "failed" if any(item.status == "failed" for item in checks) else "passed"
        ),
        generatedAt=datetime.now(timezone.utc).isoformat(),
        checks=checks,
    )


def _probe_tool(name: str, description: str) -> WajeAgentTool:
    return WajeAgentTool(
        name=name,
        description=description,
        input_model=_NoArguments,
        handler=lambda _arguments: {"ok": True},
    )


def _probe_artifact() -> ArtifactDescriptor:
    detail = {
        "schemaVersion": "deployment-probe-artifact.v1",
        "summary": "受控部署验收材料。",
    }
    digest = canonical_digest(detail)
    return ArtifactDescriptor(
        artifact_ref=f"deployment-probe-artifact:sha256:{digest}",
        artifact_type="bi_publication",
        version="deployment-probe-artifact.v1",
        digest=digest,
        source_refs=("deployment-probe-evidence:1",),
        visibility_policy_ref="visibility:customer-safe",
        customer_summary="受控部署验收材料。",
        created_at="2026-07-21T00:00:00+00:00",
    )


def _failed_check(name: str, error: Exception) -> DeploymentCheckResult:
    retryability = "not_retryable"
    if isinstance(error, DeploymentValidationError):
        code = error.code
    elif isinstance(error, LLMProviderError):
        code = error.kind
        retryability = error.retryability
    elif isinstance(error, (LLMConfigurationError, SchemaCutoverError)):
        code = str(error)
    else:
        code = "deployment_validation_unexpected_failure"
    return DeploymentCheckResult(
        name=name,
        status="failed",
        detail={"errorCode": code, "retryability": retryability},
    )


def _connect_database(environ: Mapping[str, str] | None) -> Any:
    env = os.environ if environ is None else environ
    database_url = env.get("WAJE_RUNTIME_DATABASE_URL") or env.get("DATABASE_URL")
    if not database_url:
        raise DeploymentValidationError("runtime_database_url_required")
    try:
        import psycopg
    except ImportError as exc:
        raise DeploymentValidationError("psycopg_required") from exc
    return psycopg.connect(database_url)


def write_deployment_report(
    report: GeneralAgentDeploymentReport,
    path: Path,
) -> None:
    if path.exists() and path.is_dir():
        raise DeploymentValidationError("deployment_report_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            report.to_contract(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


__all__ = (
    "DEPLOYMENT_CONTRACT_VERSION",
    "DeploymentCheckResult",
    "DeploymentDatabaseAuditor",
    "DeploymentValidationError",
    "GeneralAgentDeploymentReport",
    "GeneralAgentLiveDeploymentProbe",
    "REQUIRED_APPEND_ONLY_TABLES",
    "REQUIRED_DEPENDENCY_VERSIONS",
    "REQUIRED_DEPLOYMENT_TABLES",
    "repository_deployment_checks",
    "validate_general_agent_deployment",
    "write_deployment_report",
)
