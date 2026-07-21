from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from bi_agent.runtime.claim_authority import AuthorityBundle
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.single_authority import (
    DurableTransition,
    FailureRecord,
    LifecycleState,
)


class PostSealFailurePersistenceError(ValueError):
    pass


POST_SEAL_FAILURE_STATUSES = frozenset({"narrative_failed", "publication_failed"})

_STATUS_LAYER = {
    "narrative_failed": "narrative",
    "publication_failed": "persistence",
}


@dataclass(frozen=True)
class PostSealFailureTerminal:
    terminal_ref: str
    attempt_number: int
    supersedes_terminal_ref: str | None
    status: str
    run_attempt_id: str
    authority_bundle_ref: str
    authority_bundle_digest: str
    authority_transition_id: str
    failure_record: FailureRecord
    lifecycle_state: LifecycleState
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        attempt_number: int,
        supersedes_terminal_ref: str | None,
        status: str,
        authority_bundle: AuthorityBundle,
        authority_transition: DurableTransition,
        failure_record: FailureRecord,
        lifecycle_state: LifecycleState,
    ) -> "PostSealFailureTerminal":
        if status not in POST_SEAL_FAILURE_STATUSES:
            raise PostSealFailurePersistenceError("post_seal_failure_status_invalid")
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
            or (attempt_number == 1) != (supersedes_terminal_ref is None)
            or (
                supersedes_terminal_ref is not None
                and (
                    not isinstance(supersedes_terminal_ref, str)
                    or not supersedes_terminal_ref.startswith(
                        "post-seal-failure:sha256:"
                    )
                    or len(
                        supersedes_terminal_ref.removeprefix(
                            "post-seal-failure:sha256:"
                        )
                    )
                    != 64
                )
            )
        ):
            raise PostSealFailurePersistenceError(
                "post_seal_failure_attempt_chain_invalid"
            )
        if type(authority_bundle) is not AuthorityBundle:
            raise PostSealFailurePersistenceError(
                "post_seal_failure_authority_bundle_invalid"
            )
        if type(authority_transition) is not DurableTransition:
            raise PostSealFailurePersistenceError(
                "post_seal_failure_authority_transition_invalid"
            )
        if type(failure_record) is not FailureRecord:
            raise PostSealFailurePersistenceError("post_seal_failure_record_invalid")
        if type(lifecycle_state) is not LifecycleState:
            raise PostSealFailurePersistenceError("post_seal_failure_lifecycle_invalid")
        if (
            authority_transition.node_name != "settle_claim_authority"
            or authority_transition.run_attempt_id != authority_bundle.run_attempt_id
            or authority_transition.intent_revision_id
            != authority_bundle.intent_revision_id
            or authority_transition.status != "succeeded"
            or authority_transition.acceptance_state != "accepted"
            or failure_record.layer != _STATUS_LAYER[status]
            or failure_record.scope != "run"
            or failure_record.integrity_level != "local"
            or authority_bundle.bundle_ref not in failure_record.affected_refs
            or authority_transition.transition_id not in failure_record.affected_refs
            or lifecycle_state.run_attempt_id != authority_bundle.run_attempt_id
            or lifecycle_state.execution_state != "complete"
            or lifecycle_state.evidence_state not in {"complete", "boundary_only"}
            or lifecycle_state.publication_state != "not_ready"
            or lifecycle_state.delivery_state != "pending"
            or lifecycle_state.retry_state != "exhausted"
            or lifecycle_state.cancellation_state != "active"
            or lifecycle_state.supersession_state != "active"
        ):
            raise PostSealFailurePersistenceError(
                "post_seal_failure_authority_closure_invalid"
            )
        body = {
            "attempt_number": attempt_number,
            "supersedes_terminal_ref": supersedes_terminal_ref,
            "status": status,
            "run_attempt_id": authority_bundle.run_attempt_id,
            "authority_bundle_ref": authority_bundle.bundle_ref,
            "authority_bundle_digest": authority_bundle.bundle_digest,
            "authority_transition_id": authority_transition.transition_id,
            "failure_record": failure_record.to_dict(),
            "lifecycle_state": lifecycle_state.to_dict(),
        }
        digest = canonical_digest(body)
        return cls(
            terminal_ref="post-seal-failure:sha256:" + digest,
            content_digest=digest,
            failure_record=failure_record,
            lifecycle_state=lifecycle_state,
            **{
                key: value
                for key, value in body.items()
                if key
                not in {
                    "failure_record",
                    "lifecycle_state",
                }
            },
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        authority_bundle: AuthorityBundle,
        authority_transition: DurableTransition,
    ) -> "PostSealFailureTerminal":
        expected = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != expected:
            raise PostSealFailurePersistenceError(
                "post_seal_failure_terminal_shape_invalid"
            )
        try:
            failure = FailureRecord.from_dict(payload["failure_record"])
            lifecycle = LifecycleState.from_dict(payload["lifecycle_state"])
            rebuilt = cls.create(
                attempt_number=payload["attempt_number"],
                supersedes_terminal_ref=payload["supersedes_terminal_ref"],
                status=payload["status"],
                authority_bundle=authority_bundle,
                authority_transition=authority_transition,
                failure_record=failure,
                lifecycle_state=lifecycle,
            )
        except (TypeError, ValueError) as exc:
            raise PostSealFailurePersistenceError(
                "post_seal_failure_terminal_invalid"
            ) from exc
        if rebuilt.to_dict() != canonical_value(payload):
            raise PostSealFailurePersistenceError(
                "post_seal_failure_terminal_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return {
            "terminal_ref": self.terminal_ref,
            "attempt_number": self.attempt_number,
            "supersedes_terminal_ref": self.supersedes_terminal_ref,
            "status": self.status,
            "run_attempt_id": self.run_attempt_id,
            "authority_bundle_ref": self.authority_bundle_ref,
            "authority_bundle_digest": self.authority_bundle_digest,
            "authority_transition_id": self.authority_transition_id,
            "failure_record": self.failure_record.to_dict(),
            "lifecycle_state": self.lifecycle_state.to_dict(),
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True)
class PostSealFailurePersistenceResult:
    terminal: PostSealFailureTerminal
    status: str

    def __post_init__(self) -> None:
        if type(self.terminal) is not PostSealFailureTerminal or self.status not in {
            "inserted",
            "replayed",
        }:
            raise PostSealFailurePersistenceError(
                "post_seal_failure_persistence_result_invalid"
            )


_PREFLIGHT_SQL = """
/* post_seal_failure_preflight */
SELECT
  run.thread_id,
  bundle.owner_ref,
  bundle.payload AS authority_bundle_payload,
  bundle.bundle_digest,
  to_jsonb(transition)
    - 'input_payload'
    - 'output_payload'
    - 'failure_ref'
    - 'created_at' AS authority_transition_payload,
  lifecycle.payload AS lifecycle_payload,
  terminal.payload AS existing_terminal_payload
FROM waje_runtime.authority_bundles bundle
JOIN waje_runtime.analysis_runs run
  ON run.run_id = bundle.run_attempt_id
JOIN waje_runtime.workflow_transition_attempts transition
  ON transition.run_attempt_id = bundle.run_attempt_id
 AND transition.transition_id = %(authority_transition_id)s
 AND transition.acceptance_state = 'accepted'
JOIN LATERAL (
  SELECT state.payload
  FROM waje_runtime.run_lifecycle_state_revisions state
  WHERE state.run_attempt_id = bundle.run_attempt_id
  ORDER BY state.state_revision DESC
  LIMIT 1
) lifecycle ON TRUE
LEFT JOIN waje_runtime.post_seal_failure_terminals terminal
  ON terminal.terminal_ref = (
    SELECT latest.terminal_ref
    FROM waje_runtime.post_seal_failure_terminals latest
    WHERE latest.run_attempt_id = bundle.run_attempt_id
    ORDER BY latest.attempt_number DESC
    LIMIT 1
  )
WHERE bundle.run_attempt_id = %(run_attempt_id)s
  AND bundle.bundle_ref = %(authority_bundle_ref)s
FOR UPDATE OF bundle
"""


def load_post_seal_failure_terminal(
    connection: Any,
    *,
    authority_bundle: AuthorityBundle,
    authority_transition: DurableTransition,
) -> PostSealFailureTerminal | None:
    row = connection.execute(
        """
        /* load_post_seal_failure_terminal */
        SELECT payload
        FROM waje_runtime.post_seal_failure_terminals
        WHERE run_attempt_id = %(run_attempt_id)s
        ORDER BY attempt_number DESC
        LIMIT 1
        """,
        {"run_attempt_id": authority_bundle.run_attempt_id},
    ).fetchone()
    if row is None:
        return None
    return PostSealFailureTerminal.from_dict(
        _payload(_field(row, "payload", 0)),
        authority_bundle=authority_bundle,
        authority_transition=authority_transition,
    )


def record_post_seal_failure(
    connection: Any,
    *,
    owner_ref: str,
    thread_ref: str,
    authority_bundle: AuthorityBundle,
    authority_transition: DurableTransition,
    status: str,
    failure_record: FailureRecord,
    supersedes_terminal_ref: str | None,
) -> PostSealFailurePersistenceResult:
    owner = _required_string(owner_ref, "post_seal_failure_owner_ref_invalid")
    thread = _required_string(thread_ref, "post_seal_failure_thread_ref_invalid")
    if status not in POST_SEAL_FAILURE_STATUSES:
        raise PostSealFailurePersistenceError("post_seal_failure_status_invalid")
    try:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": f"single_authority:{authority_bundle.run_attempt_id}"},
        )
        row = connection.execute(
            _PREFLIGHT_SQL,
            {
                "run_attempt_id": authority_bundle.run_attempt_id,
                "authority_bundle_ref": authority_bundle.bundle_ref,
                "authority_transition_id": authority_transition.transition_id,
            },
        ).fetchone()
        if row is None:
            raise PostSealFailurePersistenceError(
                "post_seal_failure_active_authority_missing"
            )
        persisted_transition = DurableTransition.from_dict(
            _payload(_field(row, "authority_transition_payload", 4))
        )
        if (
            str(_field(row, "thread_id", 0) or "") != thread
            or str(_field(row, "owner_ref", 1) or "") != owner
            or canonical_value(_payload(_field(row, "authority_bundle_payload", 2)))
            != canonical_value(authority_bundle.to_dict())
            or str(_field(row, "bundle_digest", 3) or "")
            != authority_bundle.bundle_digest
            or persisted_transition != authority_transition
        ):
            raise PostSealFailurePersistenceError(
                "post_seal_failure_authority_conflict"
            )
        existing_payload = _optional_payload(
            _field(row, "existing_terminal_payload", 6)
        )
        prior_terminal_ref = supersedes_terminal_ref
        if prior_terminal_ref is not None and (
            not isinstance(prior_terminal_ref, str)
            or not prior_terminal_ref.startswith("post-seal-failure:sha256:")
            or len(prior_terminal_ref.removeprefix("post-seal-failure:sha256:")) != 64
        ):
            raise PostSealFailurePersistenceError(
                "post_seal_failure_supersedes_ref_invalid"
            )
        existing: PostSealFailureTerminal | None = None
        if existing_payload is not None:
            existing = PostSealFailureTerminal.from_dict(
                existing_payload,
                authority_bundle=authority_bundle,
                authority_transition=authority_transition,
            )
            current = LifecycleState.from_dict(
                _payload(_field(row, "lifecycle_payload", 5))
            )
            if current != existing.lifecycle_state:
                raise PostSealFailurePersistenceError(
                    "post_seal_failure_terminal_replay_conflict"
                )
            if prior_terminal_ref is None:
                expected = PostSealFailureTerminal.create(
                    attempt_number=1,
                    supersedes_terminal_ref=None,
                    status=status,
                    authority_bundle=authority_bundle,
                    authority_transition=authority_transition,
                    failure_record=failure_record,
                    lifecycle_state=existing.lifecycle_state,
                )
                if existing != expected:
                    raise PostSealFailurePersistenceError(
                        "post_seal_failure_terminal_replay_conflict"
                    )
                _validate_single_audit(connection, existing)
                connection.commit()
                return PostSealFailurePersistenceResult(
                    terminal=existing,
                    status="replayed",
                )
            if existing.terminal_ref != prior_terminal_ref:
                if existing.supersedes_terminal_ref != prior_terminal_ref:
                    raise PostSealFailurePersistenceError(
                        "post_seal_failure_retry_cas_conflict"
                    )
                expected = PostSealFailureTerminal.create(
                    attempt_number=existing.attempt_number,
                    supersedes_terminal_ref=prior_terminal_ref,
                    status=status,
                    authority_bundle=authority_bundle,
                    authority_transition=authority_transition,
                    failure_record=failure_record,
                    lifecycle_state=existing.lifecycle_state,
                )
                if existing != expected:
                    raise PostSealFailurePersistenceError(
                        "post_seal_failure_terminal_replay_conflict"
                    )
                _validate_single_audit(connection, existing)
                connection.commit()
                return PostSealFailurePersistenceResult(
                    terminal=existing,
                    status="replayed",
                )
            if existing.failure_record.retryability != "retryable":
                raise PostSealFailurePersistenceError("post_seal_failure_not_retryable")
        elif prior_terminal_ref is not None:
            raise PostSealFailurePersistenceError(
                "post_seal_failure_retry_cas_conflict"
            )

        lifecycle = LifecycleState.from_dict(
            _payload(_field(row, "lifecycle_payload", 5))
        )
        expected_evidence = (
            "boundary_only"
            if authority_bundle.authority_mode == "boundary_only"
            else "complete"
        )
        if (
            lifecycle.run_attempt_id != authority_bundle.run_attempt_id
            or lifecycle.execution_state != "complete"
            or lifecycle.evidence_state != expected_evidence
            or lifecycle.publication_state != "not_ready"
            or lifecycle.delivery_state != "pending"
            or lifecycle.retry_state != ("idle" if existing is None else "exhausted")
            or lifecycle.cancellation_state != "active"
            or lifecycle.supersession_state != "active"
        ):
            raise PostSealFailurePersistenceError(
                "post_seal_failure_lifecycle_not_recordable"
            )
        terminal_lifecycle = lifecycle.transition(retry_state="exhausted")
        terminal = PostSealFailureTerminal.create(
            attempt_number=(1 if existing is None else existing.attempt_number + 1),
            supersedes_terminal_ref=(
                None if existing is None else existing.terminal_ref
            ),
            status=status,
            authority_bundle=authority_bundle,
            authority_transition=authority_transition,
            failure_record=failure_record,
            lifecycle_state=terminal_lifecycle,
        )
        _insert_failure_record(
            connection,
            run_attempt_id=authority_bundle.run_attempt_id,
            failure=failure_record,
        )
        _insert_lifecycle(connection, terminal_lifecycle)
        inserted = connection.execute(
            """
            INSERT INTO waje_runtime.post_seal_failure_terminals(
              terminal_ref, run_attempt_id, attempt_number,
              supersedes_terminal_ref, status, authority_bundle_ref,
              authority_bundle_digest, authority_transition_id, failure_id,
              lifecycle_state_digest, content_digest, payload
            ) VALUES (
              %(terminal_ref)s, %(run_attempt_id)s, %(attempt_number)s,
              %(supersedes_terminal_ref)s, %(status)s,
              %(authority_bundle_ref)s, %(authority_bundle_digest)s,
              %(authority_transition_id)s, %(failure_id)s,
              %(lifecycle_state_digest)s, %(content_digest)s, %(payload)s::jsonb
            )
            ON CONFLICT DO NOTHING
            RETURNING terminal_ref
            """,
            {
                "terminal_ref": terminal.terminal_ref,
                "run_attempt_id": terminal.run_attempt_id,
                "attempt_number": terminal.attempt_number,
                "supersedes_terminal_ref": terminal.supersedes_terminal_ref,
                "status": terminal.status,
                "authority_bundle_ref": terminal.authority_bundle_ref,
                "authority_bundle_digest": terminal.authority_bundle_digest,
                "authority_transition_id": terminal.authority_transition_id,
                "failure_id": terminal.failure_record.failure_id,
                "lifecycle_state_digest": terminal.lifecycle_state.content_digest,
                "content_digest": terminal.content_digest,
                "payload": _json(terminal.to_dict()),
            },
        ).fetchone()
        if inserted is None:
            raise PostSealFailurePersistenceError(
                "post_seal_failure_terminal_concurrent_conflict"
            )
        connection.execute(
            """
            INSERT INTO waje_runtime.audit_events(
              event_type, actor_id, thread_id, run_id, ref, payload
            ) VALUES (
              'post_seal_failure_recorded', %(actor_id)s, %(thread_id)s,
              %(run_id)s, %(ref)s, %(payload)s::jsonb
            )
            """,
            {
                "actor_id": owner,
                "thread_id": thread,
                "run_id": terminal.run_attempt_id,
                "ref": terminal.terminal_ref,
                "payload": _json(terminal.to_dict()),
            },
        )
        connection.commit()
        return PostSealFailurePersistenceResult(
            terminal=terminal,
            status="inserted",
        )
    except Exception:
        connection.rollback()
        raise


def _insert_failure_record(
    connection: Any,
    *,
    run_attempt_id: str,
    failure: FailureRecord,
) -> None:
    payload = failure.to_dict()
    inserted = connection.execute(
        """
        INSERT INTO waje_runtime.failure_records(
          failure_id, run_attempt_id, layer, kind, scope, affected_refs,
          integrity_level, retryability, user_actionable, business_boundary,
          technical_detail_ref, content_digest, payload
        ) VALUES (
          %(failure_id)s, %(run_attempt_id)s, %(layer)s, %(kind)s, %(scope)s,
          %(affected_refs)s::jsonb, %(integrity_level)s, %(retryability)s,
          %(user_actionable)s, %(business_boundary)s, %(technical_detail_ref)s,
          %(content_digest)s, %(payload)s::jsonb
        )
        ON CONFLICT DO NOTHING
        RETURNING failure_id
        """,
        {
            **payload,
            "run_attempt_id": run_attempt_id,
            "affected_refs": _json(payload["affected_refs"]),
            "payload": _json(payload),
        },
    ).fetchone()
    if inserted is not None:
        return
    row = connection.execute(
        """
        SELECT payload
        FROM waje_runtime.failure_records
        WHERE run_attempt_id = %(run_attempt_id)s
          AND failure_id = %(failure_id)s
        """,
        {"run_attempt_id": run_attempt_id, "failure_id": failure.failure_id},
    ).fetchone()
    if row is None or canonical_value(_payload(_field(row, "payload", 0))) != payload:
        raise PostSealFailurePersistenceError("post_seal_failure_record_conflict")


def _insert_lifecycle(connection: Any, lifecycle: LifecycleState) -> None:
    payload = lifecycle.to_dict()
    inserted = connection.execute(
        """
        INSERT INTO waje_runtime.run_lifecycle_state_revisions(
          run_attempt_id, state_revision, execution_state, interaction_state,
          evidence_state, publication_state, delivery_state, retry_state,
          cancellation_state, supersession_state, prior_state_digest,
          content_digest, payload
        ) VALUES (
          %(run_attempt_id)s, %(state_revision)s, %(execution_state)s,
          %(interaction_state)s, %(evidence_state)s, %(publication_state)s,
          %(delivery_state)s, %(retry_state)s, %(cancellation_state)s,
          %(supersession_state)s, %(prior_state_digest)s, %(content_digest)s,
          %(payload)s::jsonb
        )
        ON CONFLICT DO NOTHING
        RETURNING state_revision
        """,
        {**payload, "payload": _json(payload)},
    ).fetchone()
    if inserted is None:
        raise PostSealFailurePersistenceError("post_seal_failure_lifecycle_conflict")


def _validate_single_audit(
    connection: Any,
    terminal: PostSealFailureTerminal,
) -> None:
    rows = connection.execute(
        """
        SELECT payload
        FROM waje_runtime.audit_events
        WHERE event_type = 'post_seal_failure_recorded'
          AND run_id = %(run_attempt_id)s
          AND ref = %(terminal_ref)s
        """,
        {
            "run_attempt_id": terminal.run_attempt_id,
            "terminal_ref": terminal.terminal_ref,
        },
    ).fetchall()
    if len(rows) != 1 or canonical_value(
        _payload(_field(rows[0], "payload", 0))
    ) != canonical_value(terminal.to_dict()):
        raise PostSealFailurePersistenceError("post_seal_failure_audit_conflict")


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _payload(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise PostSealFailurePersistenceError("post_seal_failure_payload_invalid")
    return value


def _optional_payload(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return _payload(value)


def _json(value: Any) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PostSealFailurePersistenceError(error)
    return value


__all__ = (
    "POST_SEAL_FAILURE_STATUSES",
    "PostSealFailurePersistenceError",
    "PostSealFailurePersistenceResult",
    "PostSealFailureTerminal",
    "load_post_seal_failure_terminal",
    "record_post_seal_failure",
)
