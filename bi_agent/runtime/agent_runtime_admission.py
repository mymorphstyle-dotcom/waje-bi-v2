from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


_GLOBAL_LOCK_NAMESPACE = 1_464_024_389
_DEFAULT_GLOBAL_LIMIT = 16
_DEFAULT_ACTOR_LIMIT = 2


class AgentRuntimeAdmissionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.retryability = "retryable"


@dataclass
class PostgresAgentRuntimeAdmissionLease:
    connection: Any
    global_slot: int
    actor_key: int
    actor_slot: int
    _released: bool = False

    @classmethod
    def acquire(
        cls,
        *,
        connection: Any,
        actor_id: str,
        environ: Mapping[str, str],
    ) -> "PostgresAgentRuntimeAdmissionLease":
        if not actor_id or actor_id != actor_id.strip():
            raise AgentRuntimeAdmissionError("agent_runtime_actor_invalid")
        global_limit = _bounded_limit(
            environ,
            "WAJE_AGENT_MAX_PROCESSES",
            _DEFAULT_GLOBAL_LIMIT,
            maximum=256,
        )
        actor_limit = _bounded_limit(
            environ,
            "WAJE_AGENT_MAX_PROCESSES_PER_ACTOR",
            _DEFAULT_ACTOR_LIMIT,
            maximum=16,
        )
        global_slot = _acquire_slot(
            connection,
            namespace=_GLOBAL_LOCK_NAMESPACE,
            limit=global_limit,
        )
        if global_slot is None:
            raise AgentRuntimeAdmissionError("agent_runtime_global_capacity_exceeded")
        actor_key = _actor_lock_key(actor_id)
        actor_slot = _acquire_slot(
            connection,
            namespace=actor_key,
            limit=actor_limit,
        )
        if actor_slot is None:
            _unlock(connection, _GLOBAL_LOCK_NAMESPACE, global_slot)
            raise AgentRuntimeAdmissionError("agent_runtime_actor_capacity_exceeded")
        return cls(
            connection=connection,
            global_slot=global_slot,
            actor_key=actor_key,
            actor_slot=actor_slot,
        )

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        _unlock(self.connection, self.actor_key, self.actor_slot)
        _unlock(self.connection, _GLOBAL_LOCK_NAMESPACE, self.global_slot)


def _acquire_slot(connection: Any, *, namespace: int, limit: int) -> int | None:
    for slot in range(1, limit + 1):
        row = connection.execute(
            "SELECT pg_try_advisory_lock(%(namespace)s, %(slot)s) AS acquired",
            {"namespace": namespace, "slot": slot},
        ).fetchone()
        acquired = (
            row.get("acquired") if isinstance(row, Mapping) else row[0]
            if row is not None
            else False
        )
        if acquired is True:
            return slot
    return None


def _unlock(connection: Any, namespace: int, slot: int) -> None:
    connection.execute(
        "SELECT pg_advisory_unlock(%(namespace)s, %(slot)s)",
        {"namespace": namespace, "slot": slot},
    )


def _actor_lock_key(actor_id: str) -> int:
    raw = int.from_bytes(
        hashlib.sha256(actor_id.encode("utf-8")).digest()[:4],
        byteorder="big",
        signed=True,
    )
    return raw if raw != _GLOBAL_LOCK_NAMESPACE else raw - 1


def _bounded_limit(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    maximum: int,
) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    if not raw.isdigit():
        raise AgentRuntimeAdmissionError("agent_runtime_admission_config_invalid")
    value = int(raw)
    if value < 1 or value > maximum:
        raise AgentRuntimeAdmissionError("agent_runtime_admission_config_invalid")
    return value


__all__ = (
    "AgentRuntimeAdmissionError",
    "PostgresAgentRuntimeAdmissionLease",
)
