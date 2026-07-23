from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import asyncio
import json

import pytest

from bi_agent.runtime.agent_context import (
    AgentContextAssembler,
    AgentContextBudgetExceeded,
    AgentContextCompactionRequired,
    InMemoryArtifactIndex,
)
from bi_agent.runtime.agent_context_compactor import (
    ThreadContextCompactor,
    ThreadSummaryGenerationInput,
    WajeThreadSummaryGenerator,
)
from bi_agent.runtime.agent_sdk_contracts import (
    AgentSdkAdapterError,
    WajeAgentRunRequest,
    WajeAgentRunResult,
)
from bi_agent.runtime.agent_turn_runtime import (
    AgentTurnRequest,
    AgentTurnRuntime,
)
from bi_agent.runtime.postgres_agent_session import PostgresAgentSession
from bi_agent.runtime.thread_context_summary import (
    InMemoryThreadSummaryStore,
    PostgresThreadSummaryStore,
    ThreadSummaryContent,
    ThreadSummaryError,
    ThreadSummarySourceItem,
    ThreadSummaryStatement,
    VersionedThreadSummary,
)
from bi_agent.runtime.thread_item_ledger import (
    InMemoryThreadItemLedger,
    NewThreadItem,
)


ROOT = Path(__file__).resolve().parents[2]


class _Rows:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def fetchone(self) -> object | None:
        return self._values[0] if self._values else None

    def fetchall(self) -> list[object]:
        return list(self._values)


class _SummaryConnection:
    def __init__(self, *, source_ref_override: str | None = None) -> None:
        self.persisted_payload: dict[str, object] | None = None
        self.source_ref_override = source_ref_override
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(
        self,
        statement: str,
        params: dict[str, object] | None = None,
    ) -> _Rows:
        self.statements.append(statement)
        params = params or {}
        if statement == "BEGIN":
            return _Rows([])
        if "FROM waje_runtime.investigation_threads" in statement:
            return _Rows([(4,)])
        if "FROM waje_runtime.conversation_messages" in statement:
            start = int(params["source_from_sequence"])
            through = int(params["source_through_sequence"])
            return _Rows(
                [
                    (
                        self.source_ref_override or f"message-{sequence}",
                        sequence,
                        f"{sequence:064x}",
                    )
                    for sequence in range(start, through + 1)
                ]
            )
        if "INSERT INTO waje_runtime.agent_thread_summaries" in statement:
            self.persisted_payload = json.loads(str(params["summary_payload"]))
            return _Rows([(self.persisted_payload,)])
        if "FROM waje_runtime.agent_thread_summaries" in statement:
            return _Rows(
                [(self.persisted_payload,)]
                if self.persisted_payload is not None
                else []
            )
        raise AssertionError(statement)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _source(sequence: int) -> ThreadSummarySourceItem:
    return ThreadSummarySourceItem(
        itemRef=f"message-{sequence}",
        sequence=sequence,
        itemDigest=f"{sequence:064x}",
    )


def _content(
    *,
    item_ref: str,
    fact_ref: str = "artifact:publication-1",
) -> ThreadSummaryContent:
    return ThreadSummaryContent(
        statements=[
            ThreadSummaryStatement(
                statementId="goal-1",
                kind="user_goal",
                text="解释当前已发布分析的变化原因。",
                sourceRefs=[item_ref],
            ),
            ThreadSummaryStatement(
                statementId="fact-1",
                kind="business_fact",
                text="当前线程存在一份已发布分析材料。",
                sourceRefs=[fact_ref],
            ),
        ]
    )


def test_versioned_thread_summary_has_content_addressed_source_closure() -> None:
    summary = VersionedThreadSummary.create(
        thread_id="thread-summary",
        summary_version=1,
        source_items=[_source(1), _source(2)],
        authority_refs=["artifact:publication-1", "claim:1"],
        content=_content(item_ref="message-1"),
    )

    assert summary.covers_from_sequence == 1
    assert summary.covers_through_sequence == 2
    assert summary.source_from_sequence == 1
    assert summary.summary_ref == f"thread-summary:sha256:{summary.summary_digest}"
    assert VersionedThreadSummary.model_validate(summary.to_contract()) == summary

    tampered = deepcopy(summary.to_contract())
    tampered["content"]["statements"][0]["text"] = "被篡改的目标"
    with pytest.raises(ValueError, match="thread_summary_content_digest_invalid"):
        VersionedThreadSummary.model_validate(tampered)


def test_business_fact_must_close_to_artifact_or_material_authority() -> None:
    with pytest.raises(
        ValueError,
        match="thread_summary_business_fact_authority_missing",
    ):
        VersionedThreadSummary.create(
            thread_id="thread-summary",
            summary_version=1,
            source_items=[_source(1)],
            authority_refs=["artifact:publication-1"],
            content=ThreadSummaryContent(
                statements=[
                    ThreadSummaryStatement(
                        statementId="fact-1",
                        kind="business_fact",
                        text="付费金额增长。",
                        sourceRefs=["message-1"],
                    )
                ]
            ),
        )

    with pytest.raises(ValueError, match="thread_summary_statement_source_unknown"):
        VersionedThreadSummary.create(
            thread_id="thread-summary",
            summary_version=1,
            source_items=[_source(1)],
            authority_refs=[],
            content=ThreadSummaryContent(
                statements=[
                    ThreadSummaryStatement(
                        statementId="goal-1",
                        kind="user_goal",
                        text="解释分析。",
                        sourceRefs=["message-unknown"],
                    )
                ]
            ),
        )


def test_empty_summary_is_valid_when_sources_have_no_material_statement() -> None:
    summary = VersionedThreadSummary.create(
        thread_id="thread-summary",
        summary_version=1,
        source_items=[_source(1)],
        authority_refs=[],
        content=ThreadSummaryContent(statements=[]),
    )

    assert summary.content.statements == []
    assert VersionedThreadSummary.model_validate(summary.to_contract()) == summary


def test_summary_store_requires_contiguous_append_only_versions() -> None:
    store = InMemoryThreadSummaryStore()
    first = VersionedThreadSummary.create(
        thread_id="thread-summary",
        summary_version=1,
        source_items=[_source(1), _source(2)],
        authority_refs=["artifact:publication-1"],
        content=_content(item_ref="message-1"),
    )
    assert store.append(first) == first
    assert store.append(first) == first

    second = VersionedThreadSummary.create(
        thread_id="thread-summary",
        summary_version=2,
        previous_summary=first,
        source_items=[_source(3), _source(4)],
        authority_refs=["artifact:publication-1"],
        content=_content(item_ref="message-3"),
    )
    assert store.append(second) == second
    assert store.latest("thread-summary") == second

    gap = VersionedThreadSummary.create(
        thread_id="thread-summary",
        summary_version=3,
        previous_summary=second,
        source_items=[_source(6)],
        authority_refs=["artifact:publication-1"],
        content=_content(item_ref="message-6"),
    )
    with pytest.raises(ThreadSummaryError, match="thread_summary_coverage_gap"):
        store.append(gap)


def test_postgres_summary_store_validates_ledger_sources_and_replays_exactly() -> None:
    connection = _SummaryConnection()
    store = PostgresThreadSummaryStore(connection)
    summary = VersionedThreadSummary.create(
        thread_id="thread-summary",
        summary_version=1,
        source_items=[_source(1), _source(2)],
        authority_refs=["artifact:publication-1"],
        content=_content(item_ref="message-1"),
    )

    assert store.append(summary) == summary
    assert store.append(summary) == summary
    assert store.latest("thread-summary") == summary
    assert connection.commits == 2
    assert connection.rollbacks == 0
    assert any(
        "FROM waje_runtime.investigation_threads" in statement
        and "FOR UPDATE" in statement
        for statement in connection.statements
    )

    conflicted = _SummaryConnection(source_ref_override="message-conflict")
    with pytest.raises(ThreadSummaryError, match="thread_summary_source_items_conflict"):
        PostgresThreadSummaryStore(conflicted).append(summary)
    assert conflicted.commits == 0
    assert conflicted.rollbacks == 1


def test_schema_persists_summary_versions_without_a_second_message_ledger() -> None:
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS waje_runtime.agent_thread_summaries" in schema
    assert "UNIQUE(thread_id, summary_version)" in schema
    assert "UNIQUE(thread_id, covers_through_sequence)" in schema
    assert "previous_summary_ref text" in schema
    assert "summary_payload jsonb NOT NULL" in schema
    assert "'agent_thread_summaries'" in schema
    assert "CREATE TABLE IF NOT EXISTS waje_runtime.thread_items" not in schema


def _ledger_with_messages(count: int) -> InMemoryThreadItemLedger:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-summary")
    ledger.append_items(
        "thread-summary",
        [
            NewThreadItem(
                item_id=f"message-{sequence}",
                item_type="user_message",
                role="user",
                text=f"消息 {sequence}",
                operation_key=f"user:operation-{sequence}",
                customer_visible=True,
                payload={
                    "sdk_item": {"role": "user", "content": f"消息 {sequence}"},
                    "sdk_replay": True,
                },
            )
            for sequence in range(1, count + 1)
        ],
    )
    return ledger


def _summary_from_ledger(
    ledger: InMemoryThreadItemLedger,
    *,
    through_sequence: int,
) -> VersionedThreadSummary:
    items = ledger.list_items(
        "thread-summary",
        through_sequence=through_sequence,
    )
    return VersionedThreadSummary.create(
        thread_id="thread-summary",
        summary_version=1,
        source_items=[
            ThreadSummarySourceItem(
                itemRef=item.item_id,
                sequence=item.sequence,
                itemDigest=item.item_digest,
            )
            for item in items
        ],
        authority_refs=[],
        content=ThreadSummaryContent(
            statements=[
                ThreadSummaryStatement(
                    statementId="goal-1",
                    kind="user_goal",
                    text="继续处理当前线程中的历史请求。",
                    sourceRefs=[items[0].item_id],
                )
            ]
        ),
    )


def test_context_assembler_uses_summary_and_only_uncompacted_items() -> None:
    ledger = _ledger_with_messages(6)
    store = InMemoryThreadSummaryStore()
    summary = store.append(_summary_from_ledger(ledger, through_sequence=4))
    assembler = AgentContextAssembler(
        ledger=ledger,
        artifact_index=InMemoryArtifactIndex(),
        summary_store=store,
        recent_item_limit=4,
        compaction_retention=2,
    )

    snapshot = assembler.assemble("thread-summary")

    assert snapshot.thread_summary == summary.to_contract()
    assert snapshot.compacted_through_sequence == 4
    assert [item.sequence for item in snapshot.recent_items] == [5, 6]
    assert summary.summary_digest in AgentContextAssembler.model_context(snapshot)


def test_context_assembler_fails_explicitly_before_history_is_truncated() -> None:
    ledger = _ledger_with_messages(5)
    assembler = AgentContextAssembler(
        ledger=ledger,
        artifact_index=InMemoryArtifactIndex(),
        recent_item_limit=4,
        compaction_retention=2,
    )

    with pytest.raises(AgentContextCompactionRequired) as raised:
        assembler.assemble("thread-summary")

    assert raised.value.compact_from_sequence == 1
    assert raised.value.compact_through_sequence == 3
    assert raised.value.latest_item_sequence == 5


def test_agent_session_replays_only_items_after_summary_coverage() -> None:
    ledger = _ledger_with_messages(6)
    session = PostgresAgentSession(
        ledger=ledger,
        thread_id="thread-summary",
        operation_id="operation-current",
        input_item_id="message-current",
        input_text="当前问题",
        replay_after_sequence=4,
        replay_through_sequence=6,
        history_limit=4,
    )

    assert asyncio.run(session.get_items()) == [
        {"role": "user", "content": "消息 5"},
        {"role": "user", "content": "消息 6"},
    ]


class SummaryGenerator:
    def __init__(self) -> None:
        self.inputs: list[ThreadSummaryGenerationInput] = []

    async def generate(
        self,
        value: ThreadSummaryGenerationInput,
    ) -> ThreadSummaryContent:
        self.inputs.append(value)
        return ThreadSummaryContent(
            statements=[
                ThreadSummaryStatement(
                    statementId=f"goal-{len(self.inputs)}",
                    kind="user_goal",
                    text="延续当前线程的历史目标。",
                    sourceRefs=[value.source_items[0].item_id],
                )
            ]
        )


def test_compactor_generates_and_appends_one_contiguous_summary_version() -> None:
    ledger = _ledger_with_messages(6)
    store = InMemoryThreadSummaryStore()
    generator = SummaryGenerator()
    compactor = ThreadContextCompactor(
        ledger=ledger,
        summary_store=store,
        artifact_index=InMemoryArtifactIndex(),
        generator=generator,
    )

    summary = asyncio.run(
        compactor.compact(
            thread_id="thread-summary",
            compact_from_sequence=1,
            compact_through_sequence=4,
        )
    )

    assert summary.covers_through_sequence == 4
    assert [item.sequence for item in summary.source_items] == [1, 2, 3, 4]
    assert generator.inputs[0].source_items[0].payload["sdk_replay"] is True
    assert store.latest("thread-summary") == summary


def test_compactor_admits_only_source_closed_generated_statements() -> None:
    class SourceDriftGenerator:
        async def generate(
            self,
            value: ThreadSummaryGenerationInput,
        ) -> ThreadSummaryContent:
            source_ref = value.source_items[0].item_id
            return ThreadSummaryContent(
                statements=[
                    ThreadSummaryStatement(
                        statementId="goal-with-extra-ref",
                        kind="user_goal",
                        text="延续当前线程的用户目标。",
                        sourceRefs=[source_ref, "message:outside-compaction-window"],
                    ),
                    ThreadSummaryStatement(
                        statementId="unsupported-business-fact",
                        kind="business_fact",
                        text="缺少权威材料的业务事实。",
                        sourceRefs=[source_ref],
                    ),
                    ThreadSummaryStatement(
                        statementId="unknown-only",
                        kind="open_question",
                        text="没有闭合来源的问题。",
                        sourceRefs=["message:unknown"],
                    ),
                ]
            )

    ledger = _ledger_with_messages(6)
    store = InMemoryThreadSummaryStore()
    compactor = ThreadContextCompactor(
        ledger=ledger,
        summary_store=store,
        artifact_index=InMemoryArtifactIndex(),
        generator=SourceDriftGenerator(),
    )

    summary = asyncio.run(
        compactor.compact(
            thread_id="thread-summary",
            compact_from_sequence=1,
            compact_through_sequence=4,
        )
    )

    assert [
        statement.model_dump(mode="json", by_alias=True)
        for statement in summary.content.statements
    ] == [
        {
            "statementId": "goal-with-extra-ref",
            "kind": "user_goal",
            "text": "延续当前线程的用户目标。",
            "sourceRefs": [summary.source_items[0].item_ref],
        }
    ]


class CombinedAdapter:
    def __init__(self) -> None:
        self.calls: list[WajeAgentRunRequest] = []

    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
        self.calls.append(request)
        if request.output_type is ThreadSummaryContent:
            payload = json.loads(request.input_text)
            source_ref = payload["sourceItems"][0]["item_id"]
            output = {
                "statements": [
                    {
                        "statementId": "goal-compacted",
                        "kind": "user_goal",
                        "text": "延续压缩前的历史目标。",
                        "sourceRefs": [source_ref],
                    }
                ]
            }
        else:
            history = await request.session.get_items() if request.session else []
            assert history == [{"role": "user", "content": "消息 4"}]
            output = {
                "answerMarkdown": "已在版本化摘要后继续回答。",
                "materialRefs": [],
                "limitationRefs": [],
            }
        return WajeAgentRunResult(
            run_id=request.run_id,
            final_output=output,
            usage={},
            model_turns=1,
        )


def test_agent_turn_runtime_compacts_before_starting_the_main_runner() -> None:
    ledger = _ledger_with_messages(4)
    store = InMemoryThreadSummaryStore()
    adapter = CombinedAdapter()
    assembler = AgentContextAssembler(
        ledger=ledger,
        artifact_index=InMemoryArtifactIndex(),
        summary_store=store,
        recent_item_limit=4,
        compaction_retention=2,
    )
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=assembler,
        adapter=adapter,
        context_compactor=ThreadContextCompactor(
            ledger=ledger,
            summary_store=store,
            artifact_index=InMemoryArtifactIndex(),
            generator=WajeThreadSummaryGenerator(adapter),
        ),
        session_history_limit=4,
    )
    request = AgentTurnRequest(
        thread_id="thread-summary",
        run_id="run-after-compaction",
        operation_id="operation-current",
        user_item_id="message-current",
        user_message="当前问题",
        expected_state_version=1,
        instructions="依据权威上下文回答。",
    )

    result = asyncio.run(runtime.run(request))

    assert result.status == "completed"
    assert [call.agent_name for call in adapter.calls] == [
        "WAJE Thread Context Compactor",
        "WAJE General Agent",
    ]
    assert store.latest("thread-summary").covers_through_sequence == 3


def test_provider_context_budget_triggers_compaction_then_explicit_overflow() -> None:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-summary")
    ledger.append_items(
        "thread-summary",
        [
            NewThreadItem(
                item_id=f"message-{sequence}",
                item_type="user_message",
                role="user",
                text="长上下文" * 300,
                operation_key=f"user:long-{sequence}",
                customer_visible=True,
            )
            for sequence in range(1, 6)
        ],
    )
    store = InMemoryThreadSummaryStore()
    assembler = AgentContextAssembler(
        ledger=ledger,
        artifact_index=InMemoryArtifactIndex(),
        summary_store=store,
        recent_item_limit=10,
        compaction_retention=2,
        context_token_budget=1000,
    )

    with pytest.raises(AgentContextCompactionRequired) as required:
        assembler.assemble("thread-summary")
    assert required.value.reason == "token_budget"
    assert required.value.compact_through_sequence == 3

    store.append(_summary_from_ledger(ledger, through_sequence=3))
    with pytest.raises(AgentContextBudgetExceeded) as overflow:
        assembler.assemble("thread-summary")
    assert overflow.value.estimated_tokens > overflow.value.budget_tokens


def test_compaction_model_failure_becomes_a_typed_agent_terminal() -> None:
    class FailingAdapter:
        async def run(self, _request: WajeAgentRunRequest) -> WajeAgentRunResult:
            raise AgentSdkAdapterError("agent_output_contract_invalid")

    ledger = _ledger_with_messages(4)
    store = InMemoryThreadSummaryStore()
    adapter = FailingAdapter()
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=AgentContextAssembler(
            ledger=ledger,
            artifact_index=InMemoryArtifactIndex(),
            summary_store=store,
            recent_item_limit=4,
            compaction_retention=2,
        ),
        adapter=adapter,
        context_compactor=ThreadContextCompactor(
            ledger=ledger,
            summary_store=store,
            artifact_index=InMemoryArtifactIndex(),
            generator=WajeThreadSummaryGenerator(adapter),
        ),
    )
    request = AgentTurnRequest(
        thread_id="thread-summary",
        run_id="run-compaction-failed",
        operation_id="operation-compaction-failed",
        user_item_id="message-current",
        user_message="继续当前问题",
        expected_state_version=1,
        instructions="依据权威上下文回答。",
    )

    result = asyncio.run(runtime.run(request))

    assert result.status == "failed"
    assert result.error_code == "agent_output_contract_invalid"
    assert ledger.get_head("thread-summary").customer_state == "idle"
    assert result.terminal_admission is not None
    assert result.terminal_admission.completion_kind == "failed_turn"
    assert store.latest("thread-summary") is None
