from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.agent_context import (
    AgentContextAssembler,
    PostgresContextAuthorityReader,
)
from bi_agent.runtime.agent_runtime_admission import (
    PostgresAgentRuntimeAdmissionLease,
)
from bi_agent.runtime.agent_context_compactor import (
    ThreadContextCompactor,
    WajeThreadSummaryGenerator,
)
from bi_agent.runtime.agent_interaction_tools import agent_interaction_tools
from bi_agent.runtime.agent_sdk_contracts import WajeAgentTool
from bi_agent.runtime.agent_turn_runtime import (
    AgentTurnRequest,
    AgentTurnResult,
    AgentTurnRuntime,
)
from bi_agent.runtime.agent_task_recovery import (
    AuthoritativeAgentTaskCompletionLoader,
)
from bi_agent.runtime.agent_tool_discovery import (
    DynamicAgentToolResolver,
    WajeToolSelectionGenerator,
)
from bi_agent.runtime.agents_sdk_adapter import WajeAgentsSdkAdapter
from bi_agent.runtime.agents_sdk_trace import PostgresAgentTraceSink
from bi_agent.runtime.analysis_artifacts import (
    PostgresAnalysisArtifactRegistry,
    analysis_artifact_tools,
)
from bi_agent.runtime.bi_analysis_tools import (
    PostgresBiAnalysisTaskGateway,
    bi_analysis_tools,
)
from bi_agent.runtime.capability_catalog_tool import capability_catalog_tool
from bi_agent.runtime.controlled_subagent_tools import (
    PostgresGeneratedArtifactWriter,
    controlled_subagent_tool,
)
from bi_agent.runtime.durable_tool_bridge import PendingActionResolution
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.mainland_model_provider import MainlandModelProvider
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.thread_context_summary import (
    PostgresThreadSummaryStore,
    VersionedThreadSummary,
)


GENERAL_AGENT_INSTRUCTIONS = """\
You are the WAJE General Agent for one durable customer conversation.
Use only the customer-safe context and tools supplied for this turn.
Answer directly when the request can be handled from conversation context or general reasoning.
Inspect persisted analysis artifacts before explaining an existing result or claim.
Use list_available_capabilities when the user asks what WAJE can analyze or query.
Delegate only mutually independent, read-only investigations over persisted customer-safe
artifacts. The main Agent retains final synthesis and all customer-facing authority.
Start run_bi_analysis only when the request requires new business-data evidence, and use
continue_bi_analysis only for a material revision of a published analysis task.
Do not invent data, artifact references, evidence, query results, or completed tool work.
For an explanation of persisted evidence, copy numeric literals only from the tool's published
customer summary. Do not expose lower-level precise fact values, recalculate, round, convert
units, or introduce a new numeric rendering. Keep opaque artifact, claim, evidence, publication,
and material references only in the typed materialRefs field; never print them in answerMarkdown.
Use ask_user only when a material ambiguity can change the business conclusion, evidence use,
fixed sensitive-output or data-access boundary, claim strength, or execution cost.
Use request_approval before an external write, irreversible action, permission increase, or
material cost. Keep technical provider payloads and hidden reasoning out of customer answers.
Write customer-facing answers, questions, and option text in the language used by the latest user
message unless the user explicitly asks for another language. Do not invent business metrics or
data availability when proposing clarification options.
"""

GENERAL_AGENT_COMMAND_MAX_BYTES = 64 * 1024
GENERAL_AGENT_MESSAGE_MAX_BYTES = 16 * 1024


class GeneralAgentTurnCommand(BaseModel):
    """SDK-neutral process contract used by the TypeScript Gateway."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    thread_id: str = Field(alias="threadId", min_length=1)
    actor_id: str = Field(alias="actorId", min_length=1)
    operation_id: str = Field(alias="operationId", min_length=1)
    message: str = Field(min_length=1)
    pending_action_resolution: PendingActionResolution | None = Field(
        alias="pendingActionResolution",
        default=None,
    )

    @field_validator("thread_id", "actor_id", "operation_id", "message")
    @classmethod
    def validate_exact_text(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("general_agent_command_text_invalid")
        return value

    @field_validator("message")
    @classmethod
    def validate_message_budget(cls, value: str) -> str:
        if len(value.encode("utf-8")) > GENERAL_AGENT_MESSAGE_MAX_BYTES:
            raise ValueError("general_agent_message_too_large")
        return value

    @model_validator(mode="after")
    def validate_pending_action_message(self) -> "GeneralAgentTurnCommand":
        if (
            self.pending_action_resolution is not None
            and self.pending_action_resolution.answer_text != self.message
        ):
            raise ValueError("general_agent_resolution_message_mismatch")
        return self

    @property
    def agent_run_id(self) -> str:
        return "agent-run-" + canonical_digest(
            {
                "schema_version": "general-agent-turn.v1",
                "thread_id": self.thread_id,
                "operation_id": self.operation_id,
            }
        )[:24]

    @property
    def user_item_id(self) -> str:
        return "agent-message-" + canonical_digest(
            {
                "schema_version": "general-agent-user-item.v1",
                "thread_id": self.thread_id,
                "operation_id": self.operation_id,
            }
        )[:24]


@dataclass
class GeneralAgentRuntimeBindings:
    store: PostgresConversationStore
    provider: MainlandModelProvider
    runtime: AgentTurnRuntime
    tools: Sequence[WajeAgentTool]
    trace_store: PostgresConversationStore | None = None
    admission_lease: PostgresAgentRuntimeAdmissionLease | None = None

    async def close(self) -> None:
        try:
            await self.provider.close()
        finally:
            try:
                if self.trace_store is not None:
                    self.trace_store.connection.close()
            finally:
                try:
                    if self.admission_lease is not None:
                        self.admission_lease.release()
                finally:
                    self.store.connection.close()


def build_general_agent_runtime(
    command: GeneralAgentTurnCommand,
    *,
    environ: Mapping[str, str] | None = None,
) -> GeneralAgentRuntimeBindings:
    env = os.environ if environ is None else environ
    store = PostgresConversationStore.from_env()
    trace_store: PostgresConversationStore | None = None
    admission_lease: PostgresAgentRuntimeAdmissionLease | None = None
    store.set_actor_id(command.actor_id)
    try:
        trace_store = PostgresConversationStore.from_env()
        trace_store.set_actor_id(command.actor_id)
        _require_thread_owner(store, command)
        admission_lease = PostgresAgentRuntimeAdmissionLease.acquire(
            connection=store.connection,
            actor_id=command.actor_id,
            environ=env,
        )
        ledger = store.thread_item_ledger
        artifact_registry = PostgresAnalysisArtifactRegistry(store.connection)
        summary_store = PostgresThreadSummaryStore(store.connection)
        provider = MainlandModelProvider.deepseek_from_env(
            env,
            circuit_connection=store.connection,
        )
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        adapter = WajeAgentsSdkAdapter(
            provider=provider,
            trace_sink=PostgresAgentTraceSink(trace_store),
        )
        context_assembler = AgentContextAssembler(
            ledger=ledger,
            artifact_index=artifact_registry,
            authority_reader=PostgresContextAuthorityReader(store.connection),
            summary_store=summary_store,
            context_token_budget=_context_token_budget(provider),
        )
        application_tools = (
            capability_catalog_tool(
                registry
            ),
            *analysis_artifact_tools(
                registry=artifact_registry,
                thread_id=command.thread_id,
            ),
            controlled_subagent_tool(
                adapter=adapter,
                registry=artifact_registry,
                writer=PostgresGeneratedArtifactWriter(store.connection),
                thread_id=command.thread_id,
                operation_id=command.operation_id,
            ),
            *bi_analysis_tools(
                gateway=PostgresBiAnalysisTaskGateway(store.connection),
                thread_id=command.thread_id,
                source_message_id=command.user_item_id,
                operation_id=command.operation_id,
            ),
        )
        tools = (
            *application_tools,
            *agent_interaction_tools(
                thread_id=command.thread_id,
                operation_id=command.operation_id,
                customer_language=_customer_language_contract(command.message),
                approvable_tools=application_tools,
            ),
        )
        return GeneralAgentRuntimeBindings(
            store=store,
            provider=provider,
            runtime=AgentTurnRuntime(
                ledger=ledger,
                context_assembler=context_assembler,
                adapter=adapter,
                tool_resolver=DynamicAgentToolResolver(
                    generator=WajeToolSelectionGenerator(
                        adapter,
                        trace_metadata={
                            "waje_thread_id": command.thread_id,
                            "waje_parent_run_id": command.agent_run_id,
                        },
                    ),
                    mandatory_tool_names=("ask_user", "request_approval"),
                ),
                business_clock=_business_clock(registry),
            ),
            tools=tools,
            trace_store=trace_store,
            admission_lease=admission_lease,
        )
    except Exception:
        try:
            if admission_lease is not None:
                admission_lease.release()
        finally:
            try:
                if trace_store is not None:
                    trace_store.connection.close()
            finally:
                store.connection.close()
        raise


def _business_clock(registry: RuntimeContractRegistry) -> dict[str, str]:
    timezone_name = registry.business_timezone
    current = datetime.now(ZoneInfo(timezone_name))
    return {
        "currentDate": current.date().isoformat(),
        "timeZone": timezone_name,
    }


async def run_general_agent_turn(
    command: GeneralAgentTurnCommand,
    *,
    bindings: GeneralAgentRuntimeBindings | None = None,
) -> AgentTurnResult:
    owned_bindings = bindings is None
    runtime_bindings = bindings or build_general_agent_runtime(command)
    try:
        head = runtime_bindings.store.thread_item_ledger.get_head(command.thread_id)
        return await runtime_bindings.runtime.run(
            AgentTurnRequest(
                thread_id=command.thread_id,
                run_id=command.agent_run_id,
                operation_id=command.operation_id,
                user_item_id=command.user_item_id,
                user_message=command.message,
                expected_state_version=head.state_version,
                instructions=GENERAL_AGENT_INSTRUCTIONS,
                tools=runtime_bindings.tools,
                active_topic_ref=head.active_topic_ref,
                permission_scope={
                    "analysis_access": "single_customer_analysis_access",
                },
                pending_action_resolution=command.pending_action_resolution,
            )
        )
    finally:
        if owned_bindings:
            await runtime_bindings.close()


async def resume_general_agent_task(
    *,
    thread_id: str,
    task_ref: str,
    environ: Mapping[str, str] | None = None,
) -> AgentTurnResult | None:
    env = os.environ if environ is None else environ
    store = PostgresConversationStore.from_env()
    trace_store: PostgresConversationStore | None = None
    provider: MainlandModelProvider | None = None
    admission_lease: PostgresAgentRuntimeAdmissionLease | None = None
    try:
        row = store.connection.execute(
            """
            SELECT owner_id
            FROM waje_runtime.investigation_threads
            WHERE thread_id = %(thread_id)s
            """,
            {"thread_id": thread_id},
        ).fetchone()
        if row is None:
            raise RuntimeError("thread_not_found")
        actor_id = str(row.get("owner_id") if isinstance(row, Mapping) else row[0])
        store.set_actor_id(actor_id)
        admission_lease = PostgresAgentRuntimeAdmissionLease.acquire(
            connection=store.connection,
            actor_id=actor_id,
            environ=env,
        )
        trace_store = PostgresConversationStore.from_env()
        trace_store.set_actor_id(actor_id)
        ledger = store.thread_item_ledger
        artifact_registry = PostgresAnalysisArtifactRegistry(store.connection)
        summary_store = PostgresThreadSummaryStore(store.connection)
        provider = MainlandModelProvider.deepseek_from_env(
            env,
            circuit_connection=store.connection,
        )
        adapter = WajeAgentsSdkAdapter(
            provider=provider,
            trace_sink=PostgresAgentTraceSink(trace_store),
        )
        runtime = AgentTurnRuntime(
            ledger=ledger,
            context_assembler=AgentContextAssembler(
                ledger=ledger,
                artifact_index=artifact_registry,
                authority_reader=PostgresContextAuthorityReader(store.connection),
                summary_store=summary_store,
                context_token_budget=_context_token_budget(provider),
            ),
            adapter=adapter,
        )
        return await runtime.resume_ready_task(
            thread_id=thread_id,
            task_ref=task_ref,
            completion_loader=AuthoritativeAgentTaskCompletionLoader(
                store=store,
                artifact_registry=artifact_registry,
            ),
            instructions=GENERAL_AGENT_INSTRUCTIONS,
            tools=analysis_artifact_tools(
                registry=artifact_registry,
                thread_id=thread_id,
            ),
            permission_scope={
                "analysis_access": "single_customer_analysis_access",
            },
        )
    finally:
        try:
            if provider is not None:
                await provider.close()
        finally:
            try:
                if trace_store is not None:
                    trace_store.connection.close()
            finally:
                try:
                    if admission_lease is not None:
                        admission_lease.release()
                finally:
                    store.connection.close()


async def refresh_general_agent_thread_summary(
    *,
    thread_id: str,
    compact_through_sequence: int,
    environ: Mapping[str, str] | None = None,
) -> VersionedThreadSummary | None:
    """Refresh one stale summary outside the customer turn critical path."""

    if (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id != thread_id.strip()
        or isinstance(compact_through_sequence, bool)
        or not isinstance(compact_through_sequence, int)
        or compact_through_sequence < 1
    ):
        raise ValueError("thread_summary_refresh_request_invalid")
    env = os.environ if environ is None else environ
    store = PostgresConversationStore.from_env()
    trace_store: PostgresConversationStore | None = None
    provider: MainlandModelProvider | None = None
    try:
        row = store.connection.execute(
            """
            SELECT owner_id
            FROM waje_runtime.investigation_threads
            WHERE thread_id = %(thread_id)s
            """,
            {"thread_id": thread_id},
        ).fetchone()
        if row is None:
            raise RuntimeError("thread_not_found")
        actor_id = str(row.get("owner_id") if isinstance(row, Mapping) else row[0])
        store.set_actor_id(actor_id)
        ledger = store.thread_item_ledger
        summary_store = PostgresThreadSummaryStore(store.connection)
        latest = summary_store.latest(thread_id)
        if (
            latest is not None
            and latest.covers_through_sequence >= compact_through_sequence
        ):
            return latest
        head = ledger.get_head(thread_id)
        if compact_through_sequence >= head.latest_item_sequence:
            raise RuntimeError("thread_summary_refresh_retention_conflict")
        trace_store = PostgresConversationStore.from_env()
        trace_store.set_actor_id(actor_id)
        artifact_registry = PostgresAnalysisArtifactRegistry(store.connection)
        provider = MainlandModelProvider.deepseek_from_env(
            env,
            circuit_connection=store.connection,
        )
        adapter = WajeAgentsSdkAdapter(
            provider=provider,
            trace_sink=PostgresAgentTraceSink(trace_store),
        )
        compactor = ThreadContextCompactor(
            ledger=ledger,
            summary_store=summary_store,
            artifact_index=artifact_registry,
            generator=WajeThreadSummaryGenerator(adapter),
        )
        return await compactor.compact(
            thread_id=thread_id,
            compact_from_sequence=(
                1 if latest is None else latest.covers_through_sequence + 1
            ),
            compact_through_sequence=compact_through_sequence,
        )
    finally:
        try:
            if provider is not None:
                await provider.close()
        finally:
            try:
                if trace_store is not None:
                    trace_store.connection.close()
            finally:
                store.connection.close()


def _require_thread_owner(
    store: PostgresConversationStore,
    command: GeneralAgentTurnCommand,
) -> None:
    row = store.connection.execute(
        """
        SELECT owner_id
        FROM waje_runtime.investigation_threads
        WHERE thread_id = %(thread_id)s
        """,
        {"thread_id": command.thread_id},
    ).fetchone()
    if row is None:
        raise RuntimeError("thread_not_found")
    owner_id = str(row.get("owner_id") if isinstance(row, Mapping) else row[0])
    if owner_id != command.actor_id:
        raise RuntimeError("thread_owner_mismatch")


def _context_token_budget(provider: MainlandModelProvider) -> int:
    input_capacity = (
        provider.config.capabilities.context_window_tokens
        - provider.config.model_settings.max_output_tokens
    )
    if input_capacity < 512:
        raise RuntimeError("mainland_provider_context_budget_invalid")
    return max(256, int(input_capacity * 0.8))


_HAN_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_TEXT = re.compile(r"[A-Za-z]")


def _customer_language_contract(message: str) -> str:
    if _HAN_TEXT.search(message):
        return "zh-Hans"
    if _LATIN_TEXT.search(message):
        return "en"
    return "match-input-script"


def _emit_startup_control(payload: Mapping[str, Any]) -> None:
    raw_fd = os.getenv("WAJE_GENERAL_AGENT_STARTUP_ACK_FD", "").strip()
    if not raw_fd:
        return
    try:
        fd = int(raw_fd)
        encoded = (
            json.dumps(
                dict(payload),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        os.write(fd, encoded)
        os.close(fd)
    except (OSError, ValueError) as exc:
        raise RuntimeError("general_agent_startup_ack_failed") from exc


def _emit_startup_ack(command: GeneralAgentTurnCommand) -> None:
    _emit_startup_control(
        {
            "schemaVersion": "general-agent-startup-control.v1",
            "status": "running",
            "runId": command.agent_run_id,
        }
    )


def _emit_startup_failure(
    error: Exception,
    command: GeneralAgentTurnCommand | None,
) -> None:
    error_code = _startup_error_code(error)
    run_id = command.agent_run_id if command is not None else "unbound"
    technical_detail_ref = "general-agent-startup-" + canonical_digest(
        {
            "schema_version": "general-agent-startup-failure.v1",
            "run_id": run_id,
            "error_code": error_code,
            "error_type": type(error).__name__,
        }
    )[:24]
    _emit_startup_control(
        {
            "schemaVersion": "general-agent-startup-control.v1",
            "status": "failed",
            "errorCode": error_code,
            "technicalDetailRef": technical_detail_ref,
        }
    )


def _startup_error_code(error: Exception) -> str:
    candidate = getattr(error, "code", None)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    message = str(error).strip()
    if message and all(
        character.islower()
        or character.isdigit()
        or character in {"_", ":", ",", "-"}
        for character in message
    ):
        return message
    return "general_agent_startup_failed"


def _command_from_json(raw: str) -> GeneralAgentTurnCommand:
    if len(raw.encode("utf-8")) > GENERAL_AGENT_COMMAND_MAX_BYTES:
        raise ValueError("general_agent_command_too_large")
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("general_agent_command_malformed_json") from exc
    return GeneralAgentTurnCommand.model_validate(value)


async def _run_cli_turn(
    command: GeneralAgentTurnCommand,
    bindings: GeneralAgentRuntimeBindings,
) -> AgentTurnResult:
    turn_task = asyncio.create_task(
        run_general_agent_turn(command, bindings=bindings)
    )
    try:
        operation_key = f"user:{command.operation_id}"
        while bindings.store.thread_item_ledger.get_item_by_operation_key(
            command.thread_id,
            operation_key,
        ) is None:
            if turn_task.done():
                return await turn_task
            await asyncio.sleep(0.01)
        _emit_startup_ack(command)
        return await turn_task
    finally:
        if not turn_task.done():
            turn_task.cancel()
            with suppress(asyncio.CancelledError):
                await turn_task
        await bindings.close()


def main(argv: list[str] | None = None) -> int:
    resolved_argv = sys.argv[1:] if argv is None else argv
    command: GeneralAgentTurnCommand | None = None
    try:
        if resolved_argv:
            raise ValueError("general_agent_cli_arguments_forbidden")
        raw = sys.stdin.buffer.read(GENERAL_AGENT_COMMAND_MAX_BYTES + 1)
        if len(raw) > GENERAL_AGENT_COMMAND_MAX_BYTES:
            raise ValueError("general_agent_command_too_large")
        command = _command_from_json(raw.decode("utf-8"))
        bindings = build_general_agent_runtime(command)
    except Exception as exc:
        _emit_startup_failure(exc, command)
        return 1
    result = asyncio.run(_run_cli_turn(command, bindings))
    json.dump(result.customer_projection(), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if result.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "GENERAL_AGENT_INSTRUCTIONS",
    "GeneralAgentRuntimeBindings",
    "GeneralAgentTurnCommand",
    "build_general_agent_runtime",
    "resume_general_agent_task",
    "run_general_agent_turn",
)
