from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from bi_agent.runtime.agent_sdk_contracts import (
    WajeAgentRunRequest,
    WajeAgentRunResult,
)
from bi_agent.runtime.analysis_artifacts import ArtifactDescriptor
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.thread_context_summary import (
    ThreadSummaryContent,
    ThreadSummarySourceItem,
    ThreadSummaryStore,
    VersionedThreadSummary,
)
from bi_agent.runtime.thread_item_ledger import ThreadItem, ThreadItemLedger


THREAD_SUMMARY_INSTRUCTIONS = """\
Create a compact WAJE thread summary from only the supplied customer-safe sources.
Return typed statements. Every statement must cite exact sourceRefs from the supplied item,
artifact, material, or previous-summary refs. A business_fact must cite at least one supplied
artifact or material ref listed in allowedAuthorityRefs. User and assistant messages can support
user_goal, accepted_decision, limitation, or open_question only when their content explicitly
establishes that class; they never establish a business_fact by themselves. Preserve a prior
statement only when it remains relevant; keep its original authority ref and cite the previous
summary ref. Return an empty statements list when no material statement is supported. Do not add
facts, calculations, decisions, limitations, identities, or business meaning absent from the supplied sources.
Do not include hidden reasoning, provider details, raw rows, SQL, credentials, or technical errors.
"""


class ThreadSummaryAdapter(Protocol):
    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult: ...


@dataclass(frozen=True)
class ThreadSummaryGenerationInput:
    thread_id: str
    previous_summary: VersionedThreadSummary | None
    source_items: tuple[ThreadItem, ...]
    artifacts: tuple[ArtifactDescriptor, ...]

    @property
    def authority_refs(self) -> tuple[str, ...]:
        refs: set[str] = set()
        for artifact in self.artifacts:
            refs.add(artifact.artifact_ref)
            refs.update(artifact.source_refs)
        return tuple(sorted(refs))


class WajeThreadSummaryGenerator:
    """Typed summary binding through the same explicit mainland SDK adapter."""

    def __init__(self, adapter: ThreadSummaryAdapter) -> None:
        self._adapter = adapter

    async def generate(
        self,
        value: ThreadSummaryGenerationInput,
    ) -> ThreadSummaryContent:
        payload = {
            "threadId": value.thread_id,
            "previousSummary": (
                value.previous_summary.to_contract()
                if value.previous_summary is not None
                else None
            ),
            "sourceItems": [
                item.to_dict(include_server_payload=False)
                for item in value.source_items
            ],
            "artifactAuthority": [item.to_dict() for item in value.artifacts],
            "allowedAuthorityRefs": list(value.authority_refs),
        }
        input_digest = canonical_digest(payload)
        run_id = f"thread-summary-run-{input_digest[:24]}"
        result = await self._adapter.run(
            WajeAgentRunRequest(
                run_id=run_id,
                agent_name="WAJE Thread Context Compactor",
                instructions=THREAD_SUMMARY_INSTRUCTIONS,
                input_text=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                output_type=ThreadSummaryContent,
                max_turns=1,
                trace_metadata={
                    "waje_thread_id": value.thread_id,
                    "waje_compaction_input_digest": input_digest,
                    "waje_previous_summary_ref": (
                        value.previous_summary.summary_ref
                        if value.previous_summary is not None
                        else ""
                    ),
                },
            )
        )
        return ThreadSummaryContent.model_validate(result.final_output)


class ThreadSummaryGenerator(Protocol):
    async def generate(
        self,
        value: ThreadSummaryGenerationInput,
    ) -> ThreadSummaryContent: ...


class ThreadSummaryArtifactIndex(Protocol):
    def list_artifacts(
        self,
        thread_id: str,
        *,
        limit: int,
    ) -> tuple[ArtifactDescriptor, ...]: ...


class ThreadContextCompactor:
    def __init__(
        self,
        *,
        ledger: ThreadItemLedger,
        summary_store: ThreadSummaryStore,
        artifact_index: ThreadSummaryArtifactIndex,
        generator: ThreadSummaryGenerator,
        artifact_limit: int = 50,
    ) -> None:
        if isinstance(artifact_limit, bool) or artifact_limit < 1:
            raise ValueError("thread_compactor_artifact_limit_invalid")
        self._ledger = ledger
        self._summary_store = summary_store
        self._artifact_index = artifact_index
        self._generator = generator
        self._artifact_limit = artifact_limit

    async def compact(
        self,
        *,
        thread_id: str,
        compact_from_sequence: int,
        compact_through_sequence: int,
    ) -> VersionedThreadSummary:
        if (
            not thread_id.strip()
            or compact_from_sequence < 1
            or compact_through_sequence < compact_from_sequence
        ):
            raise ValueError("thread_compaction_request_invalid")
        previous = self._summary_store.latest(thread_id)
        expected_from = (
            1 if previous is None else previous.covers_through_sequence + 1
        )
        if compact_from_sequence != expected_from:
            raise ValueError("thread_compaction_source_start_conflict")
        head = self._ledger.get_head(thread_id)
        if compact_through_sequence >= head.latest_item_sequence:
            raise ValueError("thread_compaction_must_retain_recent_item")
        source_items = self._ledger.list_items(
            thread_id,
            after_sequence=compact_from_sequence - 1,
            through_sequence=compact_through_sequence,
        )
        expected_sequences = tuple(
            range(compact_from_sequence, compact_through_sequence + 1)
        )
        if tuple(item.sequence for item in source_items) != expected_sequences:
            raise ValueError("thread_compaction_source_range_incomplete")
        artifacts = self._artifact_index.list_artifacts(
            thread_id,
            limit=self._artifact_limit,
        )
        generation_input = ThreadSummaryGenerationInput(
            thread_id=thread_id,
            previous_summary=previous,
            source_items=source_items,
            artifacts=tuple(artifacts),
        )
        generated_content = await self._generator.generate(generation_input)
        content = _admit_summary_content(
            generated_content,
            source_item_refs={item.item_id for item in source_items},
            authority_refs=set(generation_input.authority_refs),
            previous_summary_ref=(
                previous.summary_ref if previous is not None else None
            ),
        )
        summary = VersionedThreadSummary.create(
            thread_id=thread_id,
            summary_version=(1 if previous is None else previous.summary_version + 1),
            previous_summary=previous,
            source_items=[
                ThreadSummarySourceItem(
                    itemRef=item.item_id,
                    sequence=item.sequence,
                    itemDigest=item.item_digest,
                )
                for item in source_items
            ],
            authority_refs=generation_input.authority_refs,
            content=content,
        )
        return self._summary_store.append(summary)


def _admit_summary_content(
    content: ThreadSummaryContent,
    *,
    source_item_refs: set[str],
    authority_refs: set[str],
    previous_summary_ref: str | None,
) -> ThreadSummaryContent:
    """Keep only statements whose typed source closure survives admission."""

    allowed_refs = source_item_refs | authority_refs
    if previous_summary_ref is not None:
        allowed_refs.add(previous_summary_ref)
    admitted = []
    for statement in content.statements:
        source_refs = [
            ref for ref in statement.source_refs if ref in allowed_refs
        ]
        if not source_refs:
            continue
        if statement.kind == "business_fact" and not (
            set(source_refs) & authority_refs
        ):
            continue
        admitted.append(statement.model_copy(update={"source_refs": source_refs}))
    return ThreadSummaryContent(statements=admitted)


__all__ = (
    "THREAD_SUMMARY_INSTRUCTIONS",
    "ThreadContextCompactor",
    "ThreadSummaryGenerationInput",
    "ThreadSummaryArtifactIndex",
    "ThreadSummaryGenerator",
    "WajeThreadSummaryGenerator",
)
