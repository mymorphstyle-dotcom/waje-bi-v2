from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import wraps
from threading import RLock
from typing import Any, Mapping, Protocol, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


THREAD_ITEM_TYPES = frozenset(
    {
        "message",
        "user_message",
        "assistant_message",
        "progress",
        "tool_call",
        "tool_result",
        "tool_selection",
        "clarification",
        "approval_request",
        "approval_decision",
        "artifact_reference",
        "task_terminal",
    }
)
CUSTOMER_STATES = frozenset(
    {
        "idle",
        "working",
        "needs_input",
        "completed",
        "completed_with_limits",
        "failed",
    }
)


class ThreadLedgerError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ThreadStateVersionConflict(ThreadLedgerError):
    def __init__(self) -> None:
        super().__init__("thread_state_version_conflict")


@dataclass(frozen=True)
class ThreadHead:
    thread_id: str
    state_version: int
    active_task_id: str | None
    active_topic_ref: str | None
    pending_action_ref: str | None
    latest_item_sequence: int
    customer_state: str

    def __post_init__(self) -> None:
        if not self.thread_id.strip():
            raise ValueError("thread_head_thread_id_missing")
        if self.state_version < 0 or self.latest_item_sequence < 0:
            raise ValueError("thread_head_version_invalid")
        if self.customer_state not in CUSTOMER_STATES:
            raise ValueError("thread_head_customer_state_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "state_version": str(self.state_version),
        }


@dataclass(frozen=True)
class ThreadHeadTarget:
    active_task_id: str | None
    active_topic_ref: str | None
    pending_action_ref: str | None
    customer_state: str

    def __post_init__(self) -> None:
        if self.customer_state not in CUSTOMER_STATES:
            raise ValueError("thread_head_customer_state_invalid")

    @classmethod
    def from_head(cls, head: ThreadHead) -> "ThreadHeadTarget":
        return cls(
            active_task_id=head.active_task_id,
            active_topic_ref=head.active_topic_ref,
            pending_action_ref=head.pending_action_ref,
            customer_state=head.customer_state,
        )


@dataclass(frozen=True)
class NewThreadItem:
    item_id: str
    item_type: str
    role: str
    text: str
    operation_key: str | None = None
    customer_visible: bool = False
    payload: Mapping[str, Any] = field(default_factory=dict)
    turn_id: str | None = None

    def __post_init__(self) -> None:
        if not self.item_id.strip():
            raise ValueError("thread_item_id_missing")
        if self.item_type not in THREAD_ITEM_TYPES:
            raise ValueError("thread_item_type_invalid")
        if not self.role.strip():
            raise ValueError("thread_item_role_missing")
        if not isinstance(self.text, str):
            raise ValueError("thread_item_text_invalid")
        if self.operation_key is not None and not self.operation_key.strip():
            raise ValueError("thread_item_operation_key_invalid")
        normalized = canonical_value(self.payload)
        if not isinstance(normalized, dict):
            raise ValueError("thread_item_payload_invalid")
        object.__setattr__(self, "payload", normalized)

    @property
    def item_digest(self) -> str:
        return canonical_digest(
            {
                "item_id": self.item_id,
                "item_type": self.item_type,
                "role": self.role,
                "text": self.text,
                "operation_key": self.operation_key,
                "customer_visible": self.customer_visible,
                "payload": dict(self.payload),
                "turn_id": self.turn_id,
            }
        )


@dataclass(frozen=True)
class ThreadItem:
    item_id: str
    thread_id: str
    sequence: int
    item_type: str
    role: str
    text: str
    operation_key: str | None
    item_digest: str
    customer_visible: bool
    payload: Mapping[str, Any]
    turn_id: str | None
    created_at: str

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("thread_item_sequence_invalid")
        if self.item_type not in THREAD_ITEM_TYPES:
            raise ValueError("thread_item_type_invalid")
        normalized = canonical_value(self.payload)
        if not isinstance(normalized, dict):
            raise ValueError("thread_item_payload_invalid")
        object.__setattr__(self, "payload", normalized)

    def to_dict(self, *, include_server_payload: bool = False) -> dict[str, Any]:
        data = {
            "item_id": self.item_id,
            "thread_id": self.thread_id,
            "sequence": self.sequence,
            "item_type": self.item_type,
            "role": self.role,
            "text": self.text,
            "operation_key": self.operation_key,
            "customer_visible": self.customer_visible,
            "turn_id": self.turn_id,
            "created_at": self.created_at,
        }
        if include_server_payload:
            data["item_digest"] = self.item_digest
            data["payload"] = deepcopy(dict(self.payload))
        return data


@dataclass(frozen=True)
class LedgerAppendResult:
    items: tuple[ThreadItem, ...]
    head: ThreadHead
    replayed: bool


class ThreadItemLedger(Protocol):
    def get_head(self, thread_id: str) -> ThreadHead: ...

    def get_item_by_operation_key(
        self,
        thread_id: str,
        operation_key: str,
    ) -> ThreadItem | None: ...

    def list_items(
        self,
        thread_id: str,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
        through_sequence: int | None = None,
    ) -> tuple[ThreadItem, ...]: ...

    def append_items(
        self,
        thread_id: str,
        items: Sequence[NewThreadItem],
        *,
        expected_state_version: int | None = None,
        head_target: ThreadHeadTarget | None = None,
    ) -> LedgerAppendResult: ...


def _serialized_postgres_ledger_call(method: Any) -> Any:
    @wraps(method)
    def serialized(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return serialized


class InMemoryThreadItemLedger:
    def __init__(self) -> None:
        self._lock = RLock()
        self._heads: dict[str, ThreadHead] = {}
        self._items: dict[str, list[ThreadItem]] = {}

    def create_thread(
        self,
        thread_id: str,
        *,
        active_topic_ref: str | None = None,
    ) -> ThreadHead:
        with self._lock:
            if thread_id in self._heads:
                return self._heads[thread_id]
            head = ThreadHead(
                thread_id=thread_id,
                state_version=0,
                active_task_id=None,
                active_topic_ref=active_topic_ref,
                pending_action_ref=None,
                latest_item_sequence=0,
                customer_state="idle",
            )
            self._heads[thread_id] = head
            self._items[thread_id] = []
            return head

    def get_head(self, thread_id: str) -> ThreadHead:
        with self._lock:
            head = self._heads.get(thread_id)
            if head is None:
                raise ThreadLedgerError("thread_head_missing")
            return deepcopy(head)

    def get_item_by_operation_key(
        self,
        thread_id: str,
        operation_key: str,
    ) -> ThreadItem | None:
        with self._lock:
            for item in self._items.get(thread_id, ()):
                if item.operation_key == operation_key:
                    return deepcopy(item)
        return None

    def list_items(
        self,
        thread_id: str,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
        through_sequence: int | None = None,
    ) -> tuple[ThreadItem, ...]:
        _validate_limit(limit)
        _validate_sequence_boundary(after_sequence)
        _validate_sequence_boundary(through_sequence)
        with self._lock:
            values = [
                item
                for item in self._items.get(thread_id, ())
                if (after_sequence is None or item.sequence > after_sequence)
                and (through_sequence is None or item.sequence <= through_sequence)
            ]
            if limit is not None:
                values = values[-limit:]
            return tuple(deepcopy(values))

    def append_items(
        self,
        thread_id: str,
        items: Sequence[NewThreadItem],
        *,
        expected_state_version: int | None = None,
        head_target: ThreadHeadTarget | None = None,
    ) -> LedgerAppendResult:
        normalized = _validated_new_items(items)
        with self._lock:
            head = self._heads.get(thread_id)
            if head is None:
                raise ThreadLedgerError("thread_head_missing")
            replays = _resolve_replays(
                normalized,
                {
                    item.operation_key: item
                    for item in self._items[thread_id]
                    if item.operation_key is not None
                },
            )
            if replays is not None:
                return LedgerAppendResult(replays, deepcopy(head), True)
            if (
                expected_state_version is not None
                and head.state_version != expected_state_version
            ):
                raise ThreadStateVersionConflict()
            created_at = datetime.now(timezone.utc).isoformat()
            appended = tuple(
                ThreadItem(
                    item_id=item.item_id,
                    thread_id=thread_id,
                    sequence=head.latest_item_sequence + index,
                    item_type=item.item_type,
                    role=item.role,
                    text=item.text,
                    operation_key=item.operation_key,
                    item_digest=item.item_digest,
                    customer_visible=item.customer_visible,
                    payload=item.payload,
                    turn_id=item.turn_id,
                    created_at=created_at,
                )
                for index, item in enumerate(normalized, start=1)
            )
            target = head_target or ThreadHeadTarget.from_head(head)
            new_head = ThreadHead(
                thread_id=thread_id,
                state_version=head.state_version + 1,
                active_task_id=target.active_task_id,
                active_topic_ref=target.active_topic_ref,
                pending_action_ref=target.pending_action_ref,
                latest_item_sequence=head.latest_item_sequence + len(appended),
                customer_state=target.customer_state,
            )
            self._items[thread_id].extend(appended)
            self._heads[thread_id] = new_head
            return LedgerAppendResult(deepcopy(appended), deepcopy(new_head), False)


class PostgresThreadItemLedger:
    """Atomic ThreadHead and append-only ThreadItem view over conversation_messages."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._lock = RLock()

    @_serialized_postgres_ledger_call
    def get_head(self, thread_id: str) -> ThreadHead:
        row = _fetchone_with_rollback(
            self.connection,
            """
            SELECT thread_id, state_version, active_task_id, active_topic_ref,
                   pending_action_ref, latest_item_sequence, customer_state
            FROM waje_runtime.investigation_threads
            WHERE thread_id = %(thread_id)s
            """,
            {"thread_id": thread_id},
        )
        if row is None:
            raise ThreadLedgerError("thread_head_missing")
        return _head_from_row(row)

    @_serialized_postgres_ledger_call
    def get_item_by_operation_key(
        self,
        thread_id: str,
        operation_key: str,
    ) -> ThreadItem | None:
        row = _fetchone_with_rollback(
            self.connection,
            f"""
            {_THREAD_ITEM_SELECT}
            WHERE thread_id = %(thread_id)s
              AND operation_key = %(operation_key)s
            """,
            {"thread_id": thread_id, "operation_key": operation_key},
        )
        return _thread_item_from_row(row) if row is not None else None

    @_serialized_postgres_ledger_call
    def list_items(
        self,
        thread_id: str,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
        through_sequence: int | None = None,
    ) -> tuple[ThreadItem, ...]:
        _validate_limit(limit)
        _validate_sequence_boundary(after_sequence)
        _validate_sequence_boundary(through_sequence)
        rows = _fetchall_with_rollback(
            self.connection,
            f"""
            SELECT * FROM (
              {_THREAD_ITEM_SELECT}
              WHERE thread_id = %(thread_id)s
                AND (
                  %(after_sequence)s::bigint IS NULL
                  OR item_sequence > %(after_sequence)s::bigint
                )
                AND (
                  %(through_sequence)s::bigint IS NULL
                  OR item_sequence <= %(through_sequence)s::bigint
                )
              ORDER BY item_sequence DESC
              LIMIT %(limit)s::integer
            ) recent
            ORDER BY item_sequence
            """,
            {
                "thread_id": thread_id,
                "after_sequence": after_sequence,
                "through_sequence": through_sequence,
                "limit": limit,
            },
        )
        return tuple(_thread_item_from_row(row) for row in rows)

    @_serialized_postgres_ledger_call
    def append_items(
        self,
        thread_id: str,
        items: Sequence[NewThreadItem],
        *,
        expected_state_version: int | None = None,
        head_target: ThreadHeadTarget | None = None,
    ) -> LedgerAppendResult:
        normalized = _validated_new_items(items)
        operation_keys = [
            item.operation_key for item in normalized if item.operation_key is not None
        ]
        try:
            head_row = self.connection.execute(
                """
                SELECT thread_id, state_version, active_task_id, active_topic_ref,
                       pending_action_ref, latest_item_sequence, customer_state
                FROM waje_runtime.investigation_threads
                WHERE thread_id = %(thread_id)s
                FOR UPDATE
                """,
                {"thread_id": thread_id},
            ).fetchone()
            if head_row is None:
                raise ThreadLedgerError("thread_head_missing")
            head = _head_from_row(head_row)
            existing: dict[str, ThreadItem] = {}
            if operation_keys:
                rows = self.connection.execute(
                    f"""
                    {_THREAD_ITEM_SELECT}
                    WHERE thread_id = %(thread_id)s
                      AND operation_key = ANY(%(operation_keys)s)
                    """,
                    {
                        "thread_id": thread_id,
                        "operation_keys": operation_keys,
                    },
                ).fetchall()
                existing = {
                    item.operation_key: item
                    for item in (_thread_item_from_row(row) for row in rows)
                    if item.operation_key is not None
                }
            replays = _resolve_replays(normalized, existing)
            if replays is not None:
                self.connection.commit()
                return LedgerAppendResult(replays, head, True)
            if (
                expected_state_version is not None
                and head.state_version != expected_state_version
            ):
                raise ThreadStateVersionConflict()
            appended: list[ThreadItem] = []
            for index, item in enumerate(normalized, start=1):
                row = self.connection.execute(
                    """
                    INSERT INTO waje_runtime.conversation_messages(
                      message_id, thread_id, turn_id, role, text,
                      item_sequence, item_type, operation_key, item_digest,
                      customer_visible, payload
                    ) VALUES (
                      %(item_id)s, %(thread_id)s, %(turn_id)s, %(role)s, %(text)s,
                      %(item_sequence)s, %(item_type)s, %(operation_key)s,
                      %(item_digest)s, %(customer_visible)s, %(payload)s::jsonb
                    )
                    RETURNING message_id, thread_id, item_sequence, item_type,
                              role, text, operation_key, item_digest,
                              customer_visible, payload, turn_id, created_at
                    """,
                    {
                        "item_id": item.item_id,
                        "thread_id": thread_id,
                        "turn_id": item.turn_id,
                        "role": item.role,
                        "text": item.text,
                        "item_sequence": head.latest_item_sequence + index,
                        "item_type": item.item_type,
                        "operation_key": item.operation_key,
                        "item_digest": item.item_digest,
                        "customer_visible": item.customer_visible,
                        "payload": json.dumps(
                            dict(item.payload),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ).fetchone()
                if row is None:
                    raise ThreadLedgerError("thread_item_insert_failed")
                appended.append(_thread_item_from_row(row))
            target = head_target or ThreadHeadTarget.from_head(head)
            updated_row = self.connection.execute(
                """
                UPDATE waje_runtime.investigation_threads
                SET state_version = state_version + 1,
                    active_task_id = %(active_task_id)s,
                    active_topic_ref = %(active_topic_ref)s,
                    pending_action_ref = %(pending_action_ref)s,
                    latest_item_sequence = %(latest_item_sequence)s,
                    customer_state = %(customer_state)s,
                    updated_at = now()
                WHERE thread_id = %(thread_id)s
                  AND state_version = %(state_version)s
                RETURNING thread_id, state_version, active_task_id,
                          active_topic_ref, pending_action_ref,
                          latest_item_sequence, customer_state
                """,
                {
                    "thread_id": thread_id,
                    "state_version": head.state_version,
                    "active_task_id": target.active_task_id,
                    "active_topic_ref": target.active_topic_ref,
                    "pending_action_ref": target.pending_action_ref,
                    "latest_item_sequence": (head.latest_item_sequence + len(appended)),
                    "customer_state": target.customer_state,
                },
            ).fetchone()
            if updated_row is None:
                raise ThreadStateVersionConflict()
            self.connection.commit()
            return LedgerAppendResult(
                tuple(appended),
                _head_from_row(updated_row),
                False,
            )
        except Exception:
            self.connection.rollback()
            raise


_THREAD_ITEM_SELECT = """
SELECT message_id, thread_id, item_sequence, item_type, role, text,
       operation_key, item_digest, customer_visible, payload, turn_id, created_at
FROM waje_runtime.conversation_messages
""".strip()


def _fetchone_with_rollback(
    connection: Any,
    statement: str,
    params: Mapping[str, Any],
) -> Any:
    try:
        return connection.execute(statement, params).fetchone()
    except Exception:
        connection.rollback()
        raise


def _fetchall_with_rollback(
    connection: Any,
    statement: str,
    params: Mapping[str, Any],
) -> list[Any]:
    try:
        return list(connection.execute(statement, params).fetchall())
    except Exception:
        connection.rollback()
        raise


def _validated_new_items(items: Sequence[NewThreadItem]) -> tuple[NewThreadItem, ...]:
    if not items:
        raise ValueError("thread_items_empty")
    normalized = tuple(items)
    if any(type(item) is not NewThreadItem for item in normalized):
        raise TypeError("thread_item_invalid")
    item_ids = [item.item_id for item in normalized]
    operation_keys = [
        item.operation_key for item in normalized if item.operation_key is not None
    ]
    if len(set(item_ids)) != len(item_ids):
        raise ValueError("thread_item_id_duplicate")
    if len(set(operation_keys)) != len(operation_keys):
        raise ValueError("thread_item_operation_key_duplicate")
    return normalized


def _resolve_replays(
    items: Sequence[NewThreadItem],
    existing: Mapping[str, ThreadItem],
) -> tuple[ThreadItem, ...] | None:
    keyed = [item for item in items if item.operation_key is not None]
    matches = [existing.get(item.operation_key or "") for item in keyed]
    if not any(matches):
        return None
    if len(keyed) != len(items) or any(match is None for match in matches):
        raise ThreadLedgerError("thread_item_partial_replay_conflict")
    for candidate, persisted in zip(keyed, matches):
        if persisted is None or persisted.item_digest != candidate.item_digest:
            raise ThreadLedgerError("thread_item_replay_conflict")
    return tuple(item for item in matches if item is not None)


def _validate_limit(limit: int | None) -> None:
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
    ):
        raise ValueError("thread_item_limit_invalid")


def _validate_sequence_boundary(value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValueError("thread_item_sequence_boundary_invalid")


def _head_from_row(row: Any) -> ThreadHead:
    return ThreadHead(
        thread_id=str(_field(row, "thread_id", 0)),
        state_version=int(_field(row, "state_version", 1) or 0),
        active_task_id=_optional_text(_field(row, "active_task_id", 2)),
        active_topic_ref=_optional_text(_field(row, "active_topic_ref", 3)),
        pending_action_ref=_optional_text(_field(row, "pending_action_ref", 4)),
        latest_item_sequence=int(_field(row, "latest_item_sequence", 5) or 0),
        customer_state=str(_field(row, "customer_state", 6) or "idle"),
    )


def _thread_item_from_row(row: Any) -> ThreadItem:
    payload = _field(row, "payload", 9)
    if isinstance(payload, str):
        payload = json.loads(payload)
    created_at = _field(row, "created_at", 11)
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()
    return ThreadItem(
        item_id=str(_field(row, "message_id", 0)),
        thread_id=str(_field(row, "thread_id", 1)),
        sequence=int(_field(row, "item_sequence", 2)),
        item_type=str(_field(row, "item_type", 3) or "message"),
        role=str(_field(row, "role", 4)),
        text=str(_field(row, "text", 5)),
        operation_key=_optional_text(_field(row, "operation_key", 6)),
        item_digest=str(_field(row, "item_digest", 7) or ""),
        customer_visible=bool(_field(row, "customer_visible", 8)),
        payload=payload if isinstance(payload, Mapping) else {},
        turn_id=_optional_text(_field(row, "turn_id", 10)),
        created_at=str(created_at),
    )


def _field(row: Any, key: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
