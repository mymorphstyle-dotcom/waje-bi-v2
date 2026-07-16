from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping
from typing import Any

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.permission_roles import PRODUCT_ROLE_PERMISSION_SCOPES


DispatchRunner = Callable[[Mapping[str, Any]], Mapping[str, Any]]
PRODUCT_ROLE_BY_PERMISSION_SCOPE = {
    scope: role for role, scope in PRODUCT_ROLE_PERMISSION_SCOPES.items()
}


def run_agent_core_dispatch(dispatch: Mapping[str, Any]) -> Mapping[str, Any]:
    run_id = _required_string(dispatch, "run_id")
    thread_id = _required_string(dispatch, "thread_id")
    producer_kind = _required_string(dispatch, "producer_kind")
    scope_ref = _required_string(dispatch, "scope_ref")
    owner_id = _required_string(dispatch, "dispatch_owner_id")
    lease_epoch = dispatch.get("lease_epoch")
    payload = dispatch.get("request_payload")
    if (
        producer_kind not in {
            "thread_message",
            "artifact_continue",
            "clarification_resume",
        }
        or not isinstance(lease_epoch, int)
        or isinstance(lease_epoch, bool)
        or lease_epoch <= 0
        or not isinstance(payload, Mapping)
    ):
        raise ValueError("run_dispatch_recovery_payload_invalid")
    permission_scope = _required_string(payload, "runtimePermissionScope")
    role = PRODUCT_ROLE_BY_PERMISSION_SCOPE.get(permission_scope)
    if role is None:
        raise ValueError("run_dispatch_recovery_permission_scope_invalid")
    clarification: dict[str, Any] | None = None
    if producer_kind == "clarification_resume":
        source_run_id = _required_string(payload, "runId")
        if source_run_id != scope_ref:
            raise ValueError("run_dispatch_recovery_scope_mismatch")
        answer = _required_string(payload, "answer")
        source = _required_string(payload, "source")
        selected_option_id = payload.get("selectedOptionId")
        if selected_option_id is not None and not isinstance(
            selected_option_id, str
        ):
            raise ValueError("run_dispatch_recovery_payload_invalid")
        user_message = answer
        clarification = {
            "runId": source_run_id,
            "answer": answer,
            "selectedOptionId": selected_option_id,
            "source": source,
        }
    else:
        user_message = _required_string(payload, "message")
        if producer_kind == "thread_message" and scope_ref != thread_id:
            raise ValueError("run_dispatch_recovery_scope_mismatch")
        if producer_kind == "artifact_continue":
            artifact_id = _required_string(payload, "artifactId")
            if artifact_id != scope_ref:
                raise ValueError("run_dispatch_recovery_scope_mismatch")

    core = ConversationAgentCore.from_environment()
    try:
        result = core.run_message(
            thread_id=thread_id,
            run_id=run_id,
            user_message=user_message,
            role=role,
            runtime_permission_scope=permission_scope,
            clarification=clarification,
            run_dispatch={
                "dispatch_owner_id": owner_id,
                "lease_epoch": lease_epoch,
            },
        )
        if not isinstance(result, Mapping):
            raise ValueError("run_dispatch_recovery_result_invalid")
        result_run_id = result.get("run_id")
        status = result.get("status")
        if (
            result_run_id not in (None, run_id)
            or status
            not in {
                "waiting_for_clarification",
                "completed",
                "completed_without_workflow",
                "failed",
            }
        ):
            raise ValueError("run_dispatch_recovery_result_invalid")
        return result
    finally:
        connection = getattr(getattr(core, "store", None), "connection", None)
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def recover_pending_run_dispatches(
    *,
    store: Any,
    dispatch_runner: DispatchRunner = run_agent_core_dispatch,
    limit: int = 100,
) -> dict[str, list[Any]]:
    swept = list(store.sweep_expired_run_dispatches(limit=limit))
    leases = tuple(store.lease_recoverable_run_dispatches(limit=limit))
    dispatched: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for lease in leases:
        run_id = _required_string(lease, "run_id")
        thread_id = _required_string(lease, "thread_id")
        owner_id = _required_string(lease, "dispatch_owner_id")
        lease_epoch = lease.get("lease_epoch")
        if (
            not isinstance(lease_epoch, int)
            or isinstance(lease_epoch, bool)
            or lease_epoch <= 0
        ):
            raise ValueError("run_dispatch_recovery_lease_invalid")
        try:
            result = dispatch_runner(lease)
            if not isinstance(result, Mapping):
                raise ValueError("run_dispatch_recovery_result_invalid")
            status = result.get("status")
            if not isinstance(status, str) or not status:
                raise ValueError("run_dispatch_recovery_result_invalid")
            dispatched.append({"run_id": run_id, "status": status})
        except Exception as exc:
            failure_reason = "run_dispatch_recovery_worker_failed"
            try:
                durable = store.fail_owned_run_dispatch(
                    run_id=run_id,
                    thread_id=thread_id,
                    dispatch_owner_id=owner_id,
                    lease_epoch=lease_epoch,
                    failure_reason=failure_reason,
                )
            except Exception as finalization_error:
                raise RuntimeError(
                    "run_dispatch_recovery_failure_finalization_failed"
                ) from finalization_error
            durable_status = (
                durable.get("status") if isinstance(durable, Mapping) else None
            )
            if durable_status == "failed":
                durable_reason = durable.get("failure_reason")
                failed.append(
                    {
                        "run_id": run_id,
                        "failure_reason": (
                            durable_reason
                            if isinstance(durable_reason, str)
                            and durable_reason
                            else failure_reason
                        ),
                        "error_type": type(exc).__name__,
                    }
                )
            elif durable_status in {
                "waiting_for_clarification",
                "completed",
                "completed_without_workflow",
            }:
                dispatched.append({"run_id": run_id, "status": durable_status})
            else:
                raise RuntimeError(
                    "run_dispatch_recovery_failure_finalization_invalid"
                )
    return {
        "swept": swept,
        "leased": [_required_string(lease, "run_id") for lease in leases],
        "dispatched": dispatched,
        "failed": failed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recover and execute committed WAJE run dispatches using "
            "database-time owner leases."
        ),
    )
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    store = PostgresConversationStore.from_env()
    try:
        summary = recover_pending_run_dispatches(
            store=store,
            limit=args.limit,
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    finally:
        store.connection.close()
    return 0


def _required_string(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_dispatch_recovery_payload_invalid")
    return value.strip()


if __name__ == "__main__":
    raise SystemExit(main())
