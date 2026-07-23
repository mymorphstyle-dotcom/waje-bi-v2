from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


THREAD_SUMMARY_SCHEMA_VERSION = "thread-summary.v1"


class ThreadSummaryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ThreadSummarySourceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    item_ref: str = Field(alias="itemRef", min_length=1)
    sequence: int = Field(ge=1)
    item_digest: str = Field(alias="itemDigest", min_length=1)

    @field_validator("item_ref", "item_digest")
    @classmethod
    def validate_exact_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("thread_summary_source_text_invalid")
        return value

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ThreadSummaryStatement(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    statement_id: str = Field(alias="statementId", min_length=1)
    kind: Literal[
        "user_goal",
        "accepted_decision",
        "business_fact",
        "limitation",
        "open_question",
    ] = Field(
        description=(
            "Evidence class: user_goal is a requested outcome; accepted_decision is "
            "an explicit settled choice; business_fact is an externally evidenced fact; "
            "limitation is an explicit evidence boundary; open_question is unresolved."
        )
    )
    text: str = Field(min_length=1)
    source_refs: list[str] = Field(
        alias="sourceRefs",
        min_length=1,
        description=(
            "Exact supplied refs supporting this statement. A business_fact must include "
            "an artifact or material ref from allowedAuthorityRefs; a user or assistant "
            "message alone cannot support a business_fact."
        ),
    )

    @field_validator("statement_id", "text")
    @classmethod
    def validate_exact_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("thread_summary_statement_text_invalid")
        return value

    @field_validator("source_refs")
    @classmethod
    def validate_source_refs(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("thread_summary_statement_source_ref_invalid")
        if len(values) != len(set(values)):
            raise ValueError("thread_summary_statement_source_ref_duplicate")
        return values


class ThreadSummaryContent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    statements: list[ThreadSummaryStatement] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_statement_identity(self) -> "ThreadSummaryContent":
        statement_ids = [item.statement_id for item in self.statements]
        if len(statement_ids) != len(set(statement_ids)):
            raise ValueError("thread_summary_statement_id_duplicate")
        return self

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class VersionedThreadSummary(BaseModel):
    """Append-only semantic compression with explicit source closure."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["thread-summary.v1"] = Field(
        alias="schemaVersion",
        default=THREAD_SUMMARY_SCHEMA_VERSION,
    )
    summary_ref: str = Field(alias="summaryRef", min_length=1)
    thread_id: str = Field(alias="threadId", min_length=1)
    summary_version: int = Field(alias="summaryVersion", ge=1)
    covers_from_sequence: int = Field(alias="coversFromSequence", ge=1)
    covers_through_sequence: int = Field(alias="coversThroughSequence", ge=1)
    source_from_sequence: int = Field(alias="sourceFromSequence", ge=1)
    source_through_sequence: int = Field(alias="sourceThroughSequence", ge=1)
    previous_summary_ref: str | None = Field(
        alias="previousSummaryRef",
        default=None,
    )
    previous_summary_digest: str | None = Field(
        alias="previousSummaryDigest",
        default=None,
    )
    source_items: list[ThreadSummarySourceItem] = Field(
        alias="sourceItems",
        min_length=1,
    )
    authority_refs: list[str] = Field(alias="authorityRefs", default_factory=list)
    content: ThreadSummaryContent
    source_digest: str = Field(alias="sourceDigest", min_length=1)
    content_digest: str = Field(alias="contentDigest", min_length=1)
    summary_digest: str = Field(alias="summaryDigest", min_length=1)

    @field_validator(
        "summary_ref",
        "thread_id",
        "previous_summary_ref",
        "previous_summary_digest",
        "source_digest",
        "content_digest",
        "summary_digest",
    )
    @classmethod
    def validate_optional_exact_text(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("thread_summary_identity_invalid")
        return value

    @field_validator("authority_refs")
    @classmethod
    def validate_authority_refs(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("thread_summary_authority_ref_invalid")
        if values != sorted(set(values)):
            raise ValueError("thread_summary_authority_refs_not_canonical")
        return values

    @model_validator(mode="after")
    def validate_integrity(self) -> "VersionedThreadSummary":
        if self.covers_from_sequence != 1:
            raise ValueError("thread_summary_coverage_start_invalid")
        if self.covers_through_sequence < self.covers_from_sequence:
            raise ValueError("thread_summary_coverage_invalid")
        if self.source_through_sequence != self.covers_through_sequence:
            raise ValueError("thread_summary_source_end_invalid")
        sequences = [item.sequence for item in self.source_items]
        item_refs = [item.item_ref for item in self.source_items]
        if len(item_refs) != len(set(item_refs)):
            raise ValueError("thread_summary_source_item_ref_duplicate")
        expected = list(
            range(self.source_from_sequence, self.source_through_sequence + 1)
        )
        if sequences != expected:
            raise ValueError("thread_summary_source_range_not_contiguous")
        if self.summary_version == 1:
            if (
                self.previous_summary_ref is not None
                or self.previous_summary_digest is not None
                or self.source_from_sequence != 1
            ):
                raise ValueError("thread_summary_initial_source_invalid")
        elif (
            self.previous_summary_ref is None
            or self.previous_summary_digest is None
            or self.source_from_sequence <= 1
        ):
            raise ValueError("thread_summary_previous_source_missing")

        allowed_refs = set(item_refs) | set(self.authority_refs)
        if self.previous_summary_ref is not None:
            allowed_refs.add(self.previous_summary_ref)
        for statement in self.content.statements:
            refs = set(statement.source_refs)
            if not refs.issubset(allowed_refs):
                raise ValueError("thread_summary_statement_source_unknown")
            if statement.kind == "business_fact" and not (
                refs & set(self.authority_refs)
            ):
                raise ValueError("thread_summary_business_fact_authority_missing")

        expected_source_digest = _source_digest(
            previous_summary_ref=self.previous_summary_ref,
            previous_summary_digest=self.previous_summary_digest,
            source_items=self.source_items,
            authority_refs=self.authority_refs,
        )
        expected_content_digest = canonical_digest(self.content.to_contract())
        expected_summary_digest = _summary_digest(
            thread_id=self.thread_id,
            summary_version=self.summary_version,
            covers_through_sequence=self.covers_through_sequence,
            source_digest=expected_source_digest,
            content_digest=expected_content_digest,
        )
        if self.source_digest != expected_source_digest:
            raise ValueError("thread_summary_source_digest_invalid")
        if self.content_digest != expected_content_digest:
            raise ValueError("thread_summary_content_digest_invalid")
        if self.summary_digest != expected_summary_digest:
            raise ValueError("thread_summary_digest_invalid")
        if self.summary_ref != f"thread-summary:sha256:{expected_summary_digest}":
            raise ValueError("thread_summary_ref_invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        thread_id: str,
        summary_version: int,
        source_items: Sequence[ThreadSummarySourceItem],
        authority_refs: Sequence[str],
        content: ThreadSummaryContent,
        previous_summary: "VersionedThreadSummary | None" = None,
    ) -> "VersionedThreadSummary":
        normalized_sources = list(source_items)
        if not normalized_sources:
            raise ValueError("thread_summary_source_items_empty")
        source_from_sequence = normalized_sources[0].sequence
        source_through_sequence = normalized_sources[-1].sequence
        previous_ref = (
            previous_summary.summary_ref if previous_summary is not None else None
        )
        previous_digest = (
            previous_summary.summary_digest if previous_summary is not None else None
        )
        covers_through = source_through_sequence
        source_digest = _source_digest(
            previous_summary_ref=previous_ref,
            previous_summary_digest=previous_digest,
            source_items=normalized_sources,
            authority_refs=authority_refs,
        )
        content_digest = canonical_digest(content.to_contract())
        summary_digest = _summary_digest(
            thread_id=thread_id,
            summary_version=summary_version,
            covers_through_sequence=covers_through,
            source_digest=source_digest,
            content_digest=content_digest,
        )
        return cls(
            summaryRef=f"thread-summary:sha256:{summary_digest}",
            threadId=thread_id,
            summaryVersion=summary_version,
            coversFromSequence=1,
            coversThroughSequence=covers_through,
            sourceFromSequence=source_from_sequence,
            sourceThroughSequence=source_through_sequence,
            previousSummaryRef=previous_ref,
            previousSummaryDigest=previous_digest,
            sourceItems=normalized_sources,
            authorityRefs=sorted(set(authority_refs)),
            content=content,
            sourceDigest=source_digest,
            contentDigest=content_digest,
            summaryDigest=summary_digest,
        )

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ThreadSummaryStore(Protocol):
    def latest(self, thread_id: str) -> VersionedThreadSummary | None: ...

    def append(
        self,
        summary: VersionedThreadSummary,
    ) -> VersionedThreadSummary: ...


class InMemoryThreadSummaryStore:
    def __init__(self) -> None:
        self._by_thread: dict[str, list[VersionedThreadSummary]] = {}

    def latest(self, thread_id: str) -> VersionedThreadSummary | None:
        values = self._by_thread.get(thread_id, ())
        return deepcopy(values[-1]) if values else None

    def append(
        self,
        summary: VersionedThreadSummary,
    ) -> VersionedThreadSummary:
        values = self._by_thread.setdefault(summary.thread_id, [])
        if values and values[-1].summary_ref == summary.summary_ref:
            return deepcopy(values[-1])
        _validate_append(summary, values[-1] if values else None)
        values.append(summary)
        return deepcopy(summary)


class PostgresThreadSummaryStore:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def latest(self, thread_id: str) -> VersionedThreadSummary | None:
        row = self.connection.execute(
            """
            SELECT summary_payload
            FROM waje_runtime.agent_thread_summaries
            WHERE thread_id = %(thread_id)s
            ORDER BY summary_version DESC
            LIMIT 1
            """,
            {"thread_id": thread_id},
        ).fetchone()
        if row is None:
            return None
        return _summary_from_payload(_field(row, "summary_payload", 0))

    def append(
        self,
        summary: VersionedThreadSummary,
    ) -> VersionedThreadSummary:
        try:
            self.connection.execute("BEGIN")
            head_row = self.connection.execute(
                """
                SELECT latest_item_sequence
                FROM waje_runtime.investigation_threads
                WHERE thread_id = %(thread_id)s
                FOR UPDATE
                """,
                {"thread_id": summary.thread_id},
            ).fetchone()
            if head_row is None:
                raise ThreadSummaryError("thread_summary_thread_missing")
            if summary.covers_through_sequence > int(
                _field(head_row, "latest_item_sequence", 0)
            ):
                raise ThreadSummaryError("thread_summary_ahead_of_thread")
            row = self.connection.execute(
                """
                SELECT summary_payload
                FROM waje_runtime.agent_thread_summaries
                WHERE thread_id = %(thread_id)s
                ORDER BY summary_version DESC
                LIMIT 1
                FOR UPDATE
                """,
                {"thread_id": summary.thread_id},
            ).fetchone()
            previous = (
                _summary_from_payload(_field(row, "summary_payload", 0))
                if row is not None
                else None
            )
            if previous is not None and previous.summary_ref == summary.summary_ref:
                self.connection.commit()
                return previous
            _validate_append(summary, previous)
            source_rows = self.connection.execute(
                """
                SELECT message_id, item_sequence, item_digest
                FROM waje_runtime.conversation_messages
                WHERE thread_id = %(thread_id)s
                  AND item_sequence BETWEEN %(source_from_sequence)s
                                        AND %(source_through_sequence)s
                ORDER BY item_sequence
                """,
                {
                    "thread_id": summary.thread_id,
                    "source_from_sequence": summary.source_from_sequence,
                    "source_through_sequence": summary.source_through_sequence,
                },
            ).fetchall()
            persisted_sources = [
                ThreadSummarySourceItem(
                    itemRef=str(_field(item, "message_id", 0)),
                    sequence=int(_field(item, "item_sequence", 1)),
                    itemDigest=str(_field(item, "item_digest", 2)),
                )
                for item in source_rows
            ]
            if persisted_sources != summary.source_items:
                raise ThreadSummaryError("thread_summary_source_items_conflict")
            inserted = self.connection.execute(
                """
                INSERT INTO waje_runtime.agent_thread_summaries(
                  summary_ref, thread_id, summary_version,
                  covers_from_sequence, covers_through_sequence,
                  previous_summary_ref, source_digest, content_digest,
                  summary_digest, summary_payload
                ) VALUES (
                  %(summary_ref)s, %(thread_id)s, %(summary_version)s,
                  %(covers_from_sequence)s, %(covers_through_sequence)s,
                  %(previous_summary_ref)s, %(source_digest)s, %(content_digest)s,
                  %(summary_digest)s, %(summary_payload)s::jsonb
                )
                ON CONFLICT (summary_ref) DO NOTHING
                RETURNING summary_payload
                """,
                {
                    "summary_ref": summary.summary_ref,
                    "thread_id": summary.thread_id,
                    "summary_version": summary.summary_version,
                    "covers_from_sequence": summary.covers_from_sequence,
                    "covers_through_sequence": summary.covers_through_sequence,
                    "previous_summary_ref": summary.previous_summary_ref,
                    "source_digest": summary.source_digest,
                    "content_digest": summary.content_digest,
                    "summary_digest": summary.summary_digest,
                    "summary_payload": json.dumps(
                        summary.to_contract(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ).fetchone()
            if inserted is None:
                replay = self.connection.execute(
                    """
                    SELECT summary_payload
                    FROM waje_runtime.agent_thread_summaries
                    WHERE summary_ref = %(summary_ref)s
                    """,
                    {"summary_ref": summary.summary_ref},
                ).fetchone()
                if replay is None:
                    raise ThreadSummaryError("thread_summary_insert_conflict")
                persisted = _summary_from_payload(
                    _field(replay, "summary_payload", 0)
                )
                if persisted != summary:
                    raise ThreadSummaryError("thread_summary_replay_conflict")
                self.connection.commit()
                return persisted
            persisted = _summary_from_payload(
                _field(inserted, "summary_payload", 0)
            )
            self.connection.commit()
            return persisted
        except Exception:
            self.connection.rollback()
            raise


def _validate_append(
    summary: VersionedThreadSummary,
    previous: VersionedThreadSummary | None,
) -> None:
    expected_version = 1 if previous is None else previous.summary_version + 1
    expected_source_from = (
        1 if previous is None else previous.covers_through_sequence + 1
    )
    if summary.summary_version != expected_version:
        raise ThreadSummaryError("thread_summary_version_conflict")
    if summary.source_from_sequence != expected_source_from:
        raise ThreadSummaryError("thread_summary_coverage_gap")
    if previous is None:
        if summary.previous_summary_ref is not None:
            raise ThreadSummaryError("thread_summary_previous_conflict")
        return
    if (
        summary.previous_summary_ref != previous.summary_ref
        or summary.previous_summary_digest != previous.summary_digest
    ):
        raise ThreadSummaryError("thread_summary_previous_conflict")


def _source_digest(
    *,
    previous_summary_ref: str | None,
    previous_summary_digest: str | None,
    source_items: Sequence[ThreadSummarySourceItem],
    authority_refs: Sequence[str],
) -> str:
    return canonical_digest(
        {
            "previous_summary_ref": previous_summary_ref,
            "previous_summary_digest": previous_summary_digest,
            "source_items": [item.to_contract() for item in source_items],
            "authority_refs": sorted(set(authority_refs)),
        }
    )


def _summary_digest(
    *,
    thread_id: str,
    summary_version: int,
    covers_through_sequence: int,
    source_digest: str,
    content_digest: str,
) -> str:
    return canonical_digest(
        {
            "schema_version": THREAD_SUMMARY_SCHEMA_VERSION,
            "thread_id": thread_id,
            "summary_version": summary_version,
            "covers_from_sequence": 1,
            "covers_through_sequence": covers_through_sequence,
            "source_digest": source_digest,
            "content_digest": content_digest,
        }
    )


def _summary_from_payload(value: Any) -> VersionedThreadSummary:
    normalized = value
    if isinstance(value, str):
        try:
            normalized = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ThreadSummaryError("thread_summary_payload_malformed") from exc
    try:
        return VersionedThreadSummary.model_validate(canonical_value(normalized))
    except Exception as exc:
        raise ThreadSummaryError("thread_summary_payload_invalid") from exc


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


__all__ = (
    "InMemoryThreadSummaryStore",
    "PostgresThreadSummaryStore",
    "THREAD_SUMMARY_SCHEMA_VERSION",
    "ThreadSummaryContent",
    "ThreadSummaryError",
    "ThreadSummarySourceItem",
    "ThreadSummaryStatement",
    "ThreadSummaryStore",
    "VersionedThreadSummary",
)
