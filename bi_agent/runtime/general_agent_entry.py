from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.agent_context import (
    AgentContextAssembler,
    PostgresContextAuthorityReader,
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
from bi_agent.runtime.durable_tool_bridge import PendingActionResolution
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.mainland_model_provider import MainlandModelProvider


GENERAL_AGENT_INSTRUCTIONS = """\
You are the WAJE General Agent for one durable customer conversation.
Use only the customer-safe context and tools supplied for this turn.
Answer directly when the request can be handled from conversation context or general reasoning.
Inspect persisted analysis artifacts before explaining an existing result or claim.
Start run_bi_analysis only when the request requires new business-data evidence, and use
continue_bi_analysis only for a material revision of a published analysis task.
Do not invent data, artifact references, evidence, query results, or completed tool work.
Use ask_user only when a material ambiguity can change the business conclusion, evidence use,
fixed sensitive-output or data-access boundary, claim strength, or execution cost.
Use request_approval before an external write, irreversible action, permission increase, or
material cost. Keep technical provider payloads and hidden reasoning out of customer answers.
"""


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

    async def close(self) -> None:
        await self.provider.close()
        self.store.connection.close()


def build_general_agent_runtime(
    command: GeneralAgentTurnCommand,
    *,
    environ: Mapping[str, str] | None = None,
) -> GeneralAgentRuntimeBindings:
    env = os.environ if environ is None else environ
    store = PostgresConversationStore.from_env()
    store.set_actor_id(command.actor_id)
    try:
        _require_thread_owner(store, command)
        ledger = store.thread_item_ledger
        artifact_registry = PostgresAnalysisArtifactRegistry(store.connection)
        context_assembler = AgentContextAssembler(
            ledger=ledger,
            artifact_index=artifact_registry,
            authority_reader=PostgresContextAuthorityReader(store.connection),
        )
        provider = MainlandModelProvider.deepseek_from_env(env)
        adapter = WajeAgentsSdkAdapter(
            provider=provider,
            trace_sink=PostgresAgentTraceSink(store),
        )
        tools = (
            *analysis_artifact_tools(
                registry=artifact_registry,
                thread_id=command.thread_id,
            ),
            *bi_analysis_tools(
                gateway=PostgresBiAnalysisTaskGateway(store.connection),
                thread_id=command.thread_id,
                source_message_id=command.user_item_id,
                operation_id=command.operation_id,
            ),
            *agent_interaction_tools(
                thread_id=command.thread_id,
                operation_id=command.operation_id,
            ),
        )
        return GeneralAgentRuntimeBindings(
            store=store,
            provider=provider,
            runtime=AgentTurnRuntime(
                ledger=ledger,
                context_assembler=context_assembler,
                adapter=adapter,
            ),
            tools=tools,
        )
    except Exception:
        store.connection.close()
        raise


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
                    "actor_ref": command.actor_id,
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
        artifact_registry = PostgresAnalysisArtifactRegistry(store.connection)
        provider = MainlandModelProvider.deepseek_from_env(env)
        runtime = AgentTurnRuntime(
            ledger=ledger,
            context_assembler=AgentContextAssembler(
                ledger=ledger,
                artifact_index=artifact_registry,
                authority_reader=PostgresContextAuthorityReader(store.connection),
            ),
            adapter=WajeAgentsSdkAdapter(
                provider=provider,
                trace_sink=PostgresAgentTraceSink(store),
            ),
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
                "actor_ref": actor_id,
                "analysis_access": "single_customer_analysis_access",
            },
        )
    finally:
        if provider is not None:
            await provider.close()
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


def _emit_startup_ack() -> None:
    raw_fd = os.getenv("WAJE_GENERAL_AGENT_STARTUP_ACK_FD", "").strip()
    if not raw_fd:
        return
    try:
        fd = int(raw_fd)
        os.write(fd, b"WAJE_GENERAL_AGENT_RUNNING\n")
        os.close(fd)
    except (OSError, ValueError) as exc:
        raise RuntimeError("general_agent_startup_ack_failed") from exc


def _command_from_json(raw: str) -> GeneralAgentTurnCommand:
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
        _emit_startup_ack()
        return await turn_task
    finally:
        if not turn_task.done():
            turn_task.cancel()
            with suppress(asyncio.CancelledError):
                await turn_task
        await bindings.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-json", required=True)
    args = parser.parse_args(argv)
    command = _command_from_json(args.command_json)
    bindings = build_general_agent_runtime(command)
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
