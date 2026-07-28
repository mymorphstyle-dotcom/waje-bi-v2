#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from uuid import uuid4


NON_PUBLICATION_CHECKPOINT_STATUSES = frozenset(
    {
        "waiting_for_clarification",
        "planned",
        "evidence_ready",
        "authority_sealed",
        "narrative_ready",
        "failed",
    }
)
CUSTOMER_PUBLICATION_EVENT = "customer_publication_ready"
CUSTOMER_PUBLICATION_FIELDS = frozenset(
    {
        "blocks",
        "claim_refs",
        "field_visibility_policy_ref",
        "limitation_refs",
        "recommendation_refs",
        "visualization_refs",
        "warnings",
    }
)
CUSTOMER_PUBLICATION_BLOCK_FIELDS = frozenset(
    {
        "role",
        "text",
        "statement_role",
        "claim_refs",
        "recommendation_refs",
        "limitation_refs",
        "material_fact_bindings",
    }
)
CUSTOMER_PUBLICATION_FACT_FIELDS = frozenset(
    {"name", "fact_kind", "value", "range_end", "unit"}
)
SAFE_PUBLICATION_FIELDS = frozenset(
    {
        "authority_bundle_ref",
        "authority_bundle_digest",
        "publication_ref",
        "publication_digest",
        "projection_id",
        "projection_digest",
        "outbox_ref",
        "delivery_status",
    }
)
DELIVERY_STATUSES = frozenset({"published", "retryable_failed", "permanently_failed"})
POST_EXECUTION_REF_FIELDS = frozenset(
    {
        "post_execution_result_ref",
        "post_execution_result_digest",
        "semantic_authority_result_ref",
        "semantic_authority_result_digest",
        "authority_bundle_ref",
        "authority_bundle_digest",
        "authority_transition_id",
        "claim_coverage_checkpoint_ref",
        "claim_coverage_checkpoint_digest",
        "claim_coverage_transition_id",
        "post_seal_failure_terminal_ref",
        "failure_record_ref",
        "failure_lifecycle_state_digest",
        "narrative_workflow_ref",
        "narrative_workflow_digest",
        "compose_transition_id",
        "publication_ref",
        "outbox_ref",
        "customer_payload_ref",
        "delivery_attempt_ref",
        "customer_publication_ref",
    }
)
POST_EXECUTION_STATE_MATRIX = {
    "authority_sealed": ("not_ready", "pending"),
    "narrative_ready": ("ready", "persisted"),
    "completed": ("published", "published"),
    "delivery_retryable_failed": ("ready", "retryable_failed"),
    "delivery_permanently_failed": ("ready", "permanently_failed"),
    "narrative_failed": ("not_ready", "pending"),
    "publication_failed": ("failed", "pending"),
}
POST_EXECUTION_FAILURE_STATUSES = frozenset({"narrative_failed", "publication_failed"})
POST_EXECUTION_NON_PUBLICATION_OUTCOMES = {
    "delivery_retryable_failed": (
        "ready",
        "retryable_failed",
        "delivery_failed",
    ),
    "delivery_permanently_failed": (
        "ready",
        "permanently_failed",
        "delivery_failed",
    ),
    "narrative_failed": ("not_ready", "pending", "failed"),
    "publication_failed": ("failed", "pending", "failed"),
}
POST_EXECUTION_NON_PUBLICATION_TERMINALS = frozenset(
    POST_EXECUTION_NON_PUBLICATION_OUTCOMES
)
OPERATIONAL_FAILURE_FIELDS = frozenset(
    {"failure_ref", "layer", "kind", "retryability", "business_boundary"}
)
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
CLARIFICATION_ADMISSION_TIMEOUT_SECONDS = 60.0
EVENT_REQUEST_TIMEOUT_SECONDS = 60.0
AUTHENTICATED_USER_HEADER = "x-waje-authenticated-user-id"


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    user_id: str,
    request_identity: str | None = None,
    request_timeout_seconds: float = 600.0,
    expected_status: int | None = None,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        AUTHENTICATED_USER_HEADER: user_id,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if request_identity:
        headers["Idempotency-Key"] = request_identity
    request = Request(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=request_timeout_seconds) as response:
            response_status = getattr(response, "status", None)
            if not isinstance(response_status, int):
                response_status = response.getcode()
            if expected_status is not None and response_status != expected_status:
                raise RuntimeError(
                    f"gateway_http_status_unexpected:{response_status}:"
                    f"expected_{expected_status}"
                )
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gateway_http_error:{exc.code}:{detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"gateway_unreachable:{exc.reason}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("gateway_response_shape_invalid")
    return parsed


def _events(
    base_url: str,
    events_url: Any,
    *,
    user_id: str,
    request_timeout_seconds: float = EVENT_REQUEST_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    if not isinstance(events_url, str) or not events_url:
        return []
    request = Request(
        urljoin(base_url.rstrip("/") + "/", events_url.lstrip("/")),
        headers={
            "Accept": "text/event-stream",
            AUTHENTICATED_USER_HEADER: user_id,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=request_timeout_seconds) as response:
            while True:
                raw_line = response.readline()
                if not raw_line:
                    return []
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line.startswith("data: "):
                    continue
                try:
                    value = json.loads(line.removeprefix("data: "))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("gateway_event_invalid") from exc
                return [value] if isinstance(value, dict) else []
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"gateway_events_unavailable:{exc}") from exc


def _gateway_run_id(response: dict[str, Any]) -> str:
    snapshot = _customer_snapshot(response)
    run_handle = snapshot["transport"].get("runHandle")
    if not _is_non_empty_string(run_handle):
        raise RuntimeError("gateway_run_identity_invalid")
    return str(run_handle)


def _customer_snapshot(response: dict[str, Any]) -> dict[str, Any]:
    snapshot = response.get("snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("gateway_customer_snapshot_invalid")
    state = snapshot.get("state")
    transport = snapshot.get("transport")
    if (
        not isinstance(state, dict)
        or state.get("status")
        not in {
            "idle",
            "working",
            "needs_input",
            "completed",
            "completed_with_limits",
            "failed",
        }
        or not isinstance(transport, dict)
        or not _is_non_empty_string(transport.get("threadHandle"))
        or not isinstance(transport.get("acceptedOperationIds"), list)
        or not _is_non_empty_string(snapshot.get("stateVersion"))
    ):
        raise RuntimeError("gateway_customer_snapshot_invalid")
    return snapshot


def _require_clarification_admission(
    response: dict[str, Any],
    *,
    source_run_id: str,
    selected_option_id: str | None,
    request_identity: str,
) -> dict[str, Any]:
    del selected_option_id
    snapshot = _customer_snapshot(response)
    transport = snapshot["transport"]
    if (
        transport.get("runHandle") != source_run_id
        or transport.get("actionHandle") not in {None, source_run_id}
        or request_identity not in transport["acceptedOperationIds"]
    ):
        raise RuntimeError("gateway_clarification_admission_invalid")
    return snapshot


CLARIFICATION_REPLAY_STATUSES = frozenset(
    {
        "waiting_for_clarification",
        "running_workflow",
        "planned",
        "evidence_ready",
        "authority_sealed",
        "narrative_ready",
        "completed",
        "interaction_completed",
        "failed",
    }
)


def _latest_run_status(events: list[dict[str, Any]]) -> str:
    statuses: list[str] = []
    for event in events:
        if event.get("event") != "run_status":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        status = payload.get("status")
        if isinstance(status, str) and status.strip():
            statuses.append(status.strip())
    return statuses[-1] if statuses else ""


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(_is_non_empty_string(item) for item in value)


def _require_customer_publication(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != CUSTOMER_PUBLICATION_FIELDS:
        raise RuntimeError("customer_publication_contract_invalid")
    if not isinstance(value["blocks"], list):
        raise RuntimeError("customer_publication_contract_invalid")
    if not _is_non_empty_string(value["field_visibility_policy_ref"]):
        raise RuntimeError("customer_publication_contract_invalid")
    for field in (
        "claim_refs",
        "limitation_refs",
        "recommendation_refs",
        "visualization_refs",
        "warnings",
    ):
        if not _string_list(value[field]):
            raise RuntimeError("customer_publication_contract_invalid")
    for block in value["blocks"]:
        if (
            not isinstance(block, dict)
            or set(block) != CUSTOMER_PUBLICATION_BLOCK_FIELDS
        ):
            raise RuntimeError("customer_publication_contract_invalid")
        if not all(
            _is_non_empty_string(block[field])
            for field in ("role", "text", "statement_role")
        ):
            raise RuntimeError("customer_publication_contract_invalid")
        for field in ("claim_refs", "recommendation_refs", "limitation_refs"):
            if not _string_list(block[field]):
                raise RuntimeError("customer_publication_contract_invalid")
        bindings = block["material_fact_bindings"]
        if not isinstance(bindings, list):
            raise RuntimeError("customer_publication_contract_invalid")
        if any(
            not isinstance(binding, dict)
            or set(binding) != CUSTOMER_PUBLICATION_FACT_FIELDS
            or not _is_non_empty_string(binding.get("name"))
            or not _is_non_empty_string(binding.get("fact_kind"))
            for binding in bindings
        ):
            raise RuntimeError("customer_publication_contract_invalid")
    return value


def _require_safe_publication(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != SAFE_PUBLICATION_FIELDS:
        raise RuntimeError("publication_safe_refs_invalid")
    if not all(
        _is_non_empty_string(value[field])
        for field in (
            "authority_bundle_ref",
            "publication_ref",
            "projection_id",
            "outbox_ref",
        )
    ):
        raise RuntimeError("publication_safe_refs_invalid")
    if not all(
        _is_digest(value[field])
        for field in (
            "authority_bundle_digest",
            "publication_digest",
            "projection_digest",
        )
    ):
        raise RuntimeError("publication_safe_refs_invalid")
    if value["delivery_status"] not in DELIVERY_STATUSES:
        raise RuntimeError("publication_safe_refs_invalid")
    return value


def _require_post_execution_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("post_execution_state_invalid")
    status = value.get("post_execution_status")
    expected = POST_EXECUTION_STATE_MATRIX.get(status)
    expected_fields = {
        "post_execution_status",
        "analysis_status",
        "publication_status",
        "delivery_status",
        "publication_refs",
    }
    if status in POST_EXECUTION_FAILURE_STATUSES:
        expected_fields.add("operational_failure")
    if (
        expected is None
        or set(value) != expected_fields
        or value.get("analysis_status") not in {"complete", "boundary_only"}
        or (value.get("publication_status"), value.get("delivery_status")) != expected
    ):
        raise RuntimeError("post_execution_state_invalid")

    refs = value.get("publication_refs")
    if (
        not isinstance(refs, dict)
        or set(refs) != POST_EXECUTION_REF_FIELDS
        or not all(item is None or _is_non_empty_string(item) for item in refs.values())
        or not all(
            _is_non_empty_string(refs[field])
            for field in (
                "post_execution_result_ref",
                "semantic_authority_result_ref",
                "authority_bundle_ref",
                "authority_transition_id",
                "claim_coverage_checkpoint_ref",
                "claim_coverage_transition_id",
            )
        )
        or not all(
            _is_digest(refs[field])
            for field in (
                "post_execution_result_digest",
                "semantic_authority_result_digest",
                "authority_bundle_digest",
                "claim_coverage_checkpoint_digest",
            )
        )
    ):
        raise RuntimeError("post_execution_state_refs_invalid")

    if status in POST_EXECUTION_FAILURE_STATUSES:
        failure = value.get("operational_failure")
        if (
            not isinstance(failure, dict)
            or set(failure) != OPERATIONAL_FAILURE_FIELDS
            or not all(_is_non_empty_string(failure[field]) for field in failure)
            or failure["layer"] not in {"narrative", "persistence"}
            or failure["retryability"] not in {"retryable", "not_retryable"}
            or failure["failure_ref"] != refs["failure_record_ref"]
            or not _is_non_empty_string(refs["post_seal_failure_terminal_ref"])
            or not _is_digest(refs["failure_lifecycle_state_digest"])
            or any(
                refs[field] is not None
                for field in (
                    "narrative_workflow_ref",
                    "narrative_workflow_digest",
                    "compose_transition_id",
                    "publication_ref",
                    "outbox_ref",
                    "customer_payload_ref",
                    "delivery_attempt_ref",
                    "customer_publication_ref",
                )
            )
        ):
            raise RuntimeError("post_execution_failure_state_invalid")
    elif any(
        refs[field] is not None
        for field in (
            "post_seal_failure_terminal_ref",
            "failure_record_ref",
            "failure_lifecycle_state_digest",
        )
    ):
        raise RuntimeError("post_execution_failure_state_invalid")

    return {
        **value,
        "publication_refs": dict(refs),
        **(
            {"operational_failure": dict(value["operational_failure"])}
            if status in POST_EXECUTION_FAILURE_STATUSES
            else {}
        ),
    }


def _customer_publication_ready(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    ready = [
        event for event in events if event.get("event") == CUSTOMER_PUBLICATION_EVENT
    ]
    if not ready:
        return None
    if len(ready) != 1:
        raise RuntimeError("customer_publication_event_ambiguous")
    payload = ready[0].get("payload")
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"status", "customer_publication", "publication", "post_execution"}
        or payload.get("status") != "completed"
    ):
        raise RuntimeError("customer_publication_event_invalid")
    post_execution = _require_post_execution_state(payload["post_execution"])
    publication = _require_safe_publication(payload["publication"])
    refs = post_execution["publication_refs"]
    if (
        post_execution["post_execution_status"] != "completed"
        or publication["delivery_status"] != "published"
        or publication["authority_bundle_ref"] != refs["authority_bundle_ref"]
        or publication["authority_bundle_digest"] != refs["authority_bundle_digest"]
        or publication["publication_ref"] != refs["publication_ref"]
        or publication["outbox_ref"] != refs["outbox_ref"]
    ):
        raise RuntimeError("customer_publication_post_execution_mismatch")
    return {
        "customer_publication": _require_customer_publication(
            payload["customer_publication"]
        ),
        "publication": publication,
        "post_execution": post_execution,
    }


def _post_execution_non_publication_terminal(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    terminals: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "post_execution_state":
            continue
        state = _require_post_execution_state(event.get("payload"))
        if state["post_execution_status"] in POST_EXECUTION_NON_PUBLICATION_TERMINALS:
            terminals.append(state)
    if not terminals:
        return None
    unique = {
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for item in terminals
    }
    if len(unique) != 1:
        raise RuntimeError("post_execution_terminal_ambiguous")
    return terminals[-1]


def _project_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("event")
    projected: dict[str, Any] = {
        "event": event_type,
        "runId": event.get("runId"),
    }
    if _is_non_empty_string(event.get("threadId")):
        projected["threadId"] = event["threadId"]
    if event_type == "run_status":
        status = event.get("payload")
        projected["payload"] = {
            "status": status.get("status") if isinstance(status, dict) else None
        }
    elif event_type == CUSTOMER_PUBLICATION_EVENT:
        ready = _customer_publication_ready([event])
        if ready is None:
            raise RuntimeError("customer_publication_event_invalid")
        projected["payload"] = {"status": "completed", **ready}
    elif event_type == "post_execution_state":
        projected["payload"] = _require_post_execution_state(event.get("payload"))
    elif event_type in {
        "clarification_requested",
        "clarification_state_saved",
        "interaction_result_ready",
        "plan_result_ready",
        "execution_result_ready",
    }:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("gateway_stage_event_payload_invalid")
        projected["payload"] = payload
    if isinstance(event.get("process"), dict):
        projected["process"] = event["process"]
    return projected


def _event_identity(event: dict[str, Any]) -> str:
    return json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    )


def _poll_run_events(
    *,
    base_url: str,
    user_id: str,
    run_id: str,
    events_url: str,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float,
    deadline: float | None = None,
    await_dispatch_id: str | None = None,
) -> dict[str, Any]:
    if (timeout_seconds is None) == (deadline is None):
        raise ValueError("gateway_observation_deadline_invalid")
    if deadline is None:
        deadline = monotonic() + float(timeout_seconds)
    observed: dict[str, dict[str, Any]] = {}
    terminal_status = ""
    poll_attempts = 0
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {
                "run_id": run_id,
                "events_url": events_url,
                "checkpoint_reached": False,
                "terminal_status": terminal_status or "unknown",
                "publication_state": "unknown",
                "delivery_state": "unknown",
                "business_acceptance": "not_evaluated",
                "customer_publication": None,
                "publication": None,
                "timed_out": True,
                "poll_attempts": poll_attempts,
                "events": [_project_event(event) for event in observed.values()],
            }
        snapshot = _events(
            base_url,
            events_url,
            user_id=user_id,
            request_timeout_seconds=min(EVENT_REQUEST_TIMEOUT_SECONDS, remaining),
        )
        poll_attempts += 1
        for event in snapshot:
            observed.setdefault(_event_identity(event), event)
        current_status = _latest_run_status(snapshot)
        if current_status:
            terminal_status = current_status
        publication_ready = _customer_publication_ready(list(observed.values()))
        if terminal_status == "completed" and publication_ready is not None:
            post_execution = publication_ready["post_execution"]
            return {
                "run_id": run_id,
                "events_url": events_url,
                "checkpoint_reached": True,
                "run_status": terminal_status,
                "terminal_status": post_execution["post_execution_status"],
                "post_execution_status": post_execution["post_execution_status"],
                "publication_state": post_execution["publication_status"],
                "delivery_state": post_execution["delivery_status"],
                "business_acceptance": "passed",
                **publication_ready,
                "timed_out": False,
                "poll_attempts": poll_attempts,
                "events": [_project_event(event) for event in observed.values()],
            }
        post_execution = _post_execution_non_publication_terminal(
            list(observed.values())
        )
        if terminal_status == "completed" and post_execution is not None:
            post_execution_status = post_execution["post_execution_status"]
            outcome = POST_EXECUTION_NON_PUBLICATION_OUTCOMES[post_execution_status]
            return {
                "run_id": run_id,
                "events_url": events_url,
                "checkpoint_reached": True,
                "run_status": terminal_status,
                "terminal_status": post_execution_status,
                "post_execution_status": post_execution_status,
                "publication_state": post_execution["publication_status"],
                "delivery_state": post_execution["delivery_status"],
                "business_acceptance": outcome[2],
                "post_execution": post_execution,
                "customer_publication": None,
                "publication": None,
                "timed_out": False,
                "poll_attempts": poll_attempts,
                "events": [_project_event(event) for event in observed.values()],
            }
        dispatch_terminal_status = _dispatch_terminal_status(
            snapshot,
            await_dispatch_id,
        )
        waiting_command_completed = terminal_status == "waiting_for_clarification" and (
            await_dispatch_id is None
            or dispatch_terminal_status == "waiting_for_clarification"
        )
        if terminal_status in NON_PUBLICATION_CHECKPOINT_STATUSES and (
            terminal_status != "waiting_for_clarification" or waiting_command_completed
        ):
            return {
                "run_id": run_id,
                "events_url": events_url,
                "checkpoint_reached": True,
                "run_status": terminal_status,
                "terminal_status": terminal_status,
                "publication_state": "not_ready",
                "delivery_state": "pending",
                "business_acceptance": (
                    "waiting_for_human"
                    if terminal_status == "waiting_for_clarification"
                    else "failed"
                    if terminal_status == "failed"
                    else "not_evaluated"
                ),
                "customer_publication": None,
                "publication": None,
                "timed_out": False,
                "poll_attempts": poll_attempts,
                "events": [_project_event(event) for event in observed.values()],
            }
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {
                "run_id": run_id,
                "events_url": events_url,
                "checkpoint_reached": False,
                "terminal_status": terminal_status or "unknown",
                "publication_state": "unknown",
                "delivery_state": "unknown",
                "business_acceptance": "not_evaluated",
                "customer_publication": None,
                "publication": None,
                "timed_out": True,
                "poll_attempts": poll_attempts,
                "events": [_project_event(event) for event in observed.values()],
            }
        sleep(min(poll_interval_seconds, remaining))


def _observe_gateway_response(
    *,
    base_url: str,
    user_id: str,
    response: dict[str, Any],
    poll_interval_seconds: float,
    timeout_seconds: float | None = None,
    deadline: float | None = None,
    await_dispatch_id: str | None = None,
    ignore_needs_input_version: str | None = None,
) -> dict[str, Any]:
    del await_dispatch_id
    snapshot = _customer_snapshot(response)
    return _poll_customer_snapshot(
        base_url=base_url,
        user_id=user_id,
        initial_snapshot=snapshot,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        deadline=deadline,
        ignore_needs_input_version=ignore_needs_input_version,
    )


def _poll_customer_snapshot(
    *,
    base_url: str,
    user_id: str,
    initial_snapshot: dict[str, Any],
    poll_interval_seconds: float,
    timeout_seconds: float | None = None,
    deadline: float | None = None,
    ignore_needs_input_version: str | None = None,
) -> dict[str, Any]:
    if (timeout_seconds is None) == (deadline is None):
        raise ValueError("gateway_observation_deadline_invalid")
    if deadline is None:
        deadline = monotonic() + float(timeout_seconds)
    snapshot = initial_snapshot
    poll_attempts = 0
    while True:
        state = snapshot["state"]
        status = state["status"]
        needs_input_ready = (
            status == "needs_input"
            and snapshot["stateVersion"] != ignore_needs_input_version
        )
        if status in {"completed", "completed_with_limits", "failed"} or (
            needs_input_ready
        ):
            return {
                "run_id": snapshot["transport"].get("runHandle"),
                "events_url": snapshot["transport"].get("eventsUrl"),
                "checkpoint_reached": True,
                "run_status": status,
                "terminal_status": status,
                "business_acceptance": {
                    "completed": "passed",
                    "completed_with_limits": "passed_with_limits",
                    "failed": "failed",
                    "needs_input": "waiting_for_human",
                }[status],
                "customer_snapshot": snapshot,
                "answer": state.get("answer"),
                "timed_out": False,
                "poll_attempts": poll_attempts,
            }
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {
                "run_id": snapshot["transport"].get("runHandle"),
                "events_url": snapshot["transport"].get("eventsUrl"),
                "checkpoint_reached": False,
                "run_status": status,
                "terminal_status": status,
                "business_acceptance": "not_evaluated",
                "customer_snapshot": snapshot,
                "answer": None,
                "timed_out": True,
                "poll_attempts": poll_attempts,
            }
        sleep(min(poll_interval_seconds, remaining))
        response = _json_request(
            base_url,
            f"/api/threads/{snapshot['transport']['threadHandle']}",
            user_id=user_id,
            request_timeout_seconds=min(EVENT_REQUEST_TIMEOUT_SECONDS, remaining),
        )
        snapshot = _customer_snapshot(response)
        poll_attempts += 1


def _dispatch_terminal_status(
    events: list[dict[str, Any]],
    dispatch_id: str | None,
) -> str:
    if dispatch_id is None:
        return ""
    terminal_status = ""
    for event in events:
        if event.get("event") not in {
            "run_dispatch_completed",
            "run_dispatch_failed",
        }:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("dispatch_id") != dispatch_id:
            continue
        status = payload.get("terminal_status")
        if isinstance(status, str) and status.strip():
            terminal_status = status.strip()
    return terminal_status


def _create_thread(base_url: str, user_id: str, request_identity: str) -> str:
    response = _json_request(
        base_url,
        "/api/threads",
        method="POST",
        payload={"requestIdentity": request_identity},
        user_id=user_id,
        request_identity=request_identity,
        expected_status=201,
    )
    snapshot = response.get("snapshot")
    transport = snapshot.get("transport") if isinstance(snapshot, dict) else None
    thread_handle = (
        transport.get("threadHandle") if isinstance(transport, dict) else None
    )
    if not isinstance(thread_handle, str) or not thread_handle:
        raise RuntimeError("gateway_thread_response_invalid")
    return thread_handle


def _run_first_turn(
    *,
    base_url: str,
    user_id: str,
    thread_id: str,
    question: str,
    request_identity: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    response = _json_request(
        base_url,
        f"/api/threads/{thread_id}/messages",
        method="POST",
        payload={"message": question, "requestIdentity": request_identity},
        user_id=user_id,
        request_identity=request_identity,
        request_timeout_seconds=timeout_seconds,
        expected_status=202,
    )
    observation = _observe_gateway_response(
        base_url=base_url,
        user_id=user_id,
        response=response,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    return {
        "operation": "first_turn",
        "thread_id": thread_id,
        **observation,
    }


def _submit_clarification_resolution(
    *,
    base_url: str,
    user_id: str,
    run_id: str,
    answer: str,
    selected_option_id: str | None,
    request_identity: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    deadline = monotonic() + timeout_seconds
    payload: dict[str, Any] = {
        "answer": answer,
        "selectedOptionIds": (
            [selected_option_id] if selected_option_id is not None else []
        ),
        "requestIdentity": request_identity,
    }
    response = _json_request(
        base_url,
        f"/api/runs/{run_id}/clarifications",
        method="POST",
        payload=payload,
        user_id=user_id,
        request_identity=request_identity,
        request_timeout_seconds=min(
            CLARIFICATION_ADMISSION_TIMEOUT_SECONDS,
            timeout_seconds,
        ),
        expected_status=202,
    )
    admission = _require_clarification_admission(
        response,
        source_run_id=run_id,
        selected_option_id=selected_option_id,
        request_identity=request_identity,
    )
    observation = _observe_gateway_response(
        base_url=base_url,
        user_id=user_id,
        response=response,
        poll_interval_seconds=poll_interval_seconds,
        deadline=deadline,
        ignore_needs_input_version=admission["stateVersion"],
    )
    return {
        "operation": "clarification_resolution",
        "source_run_id": run_id,
        "operation_identity": request_identity,
        "admission_status": admission["state"]["status"],
        "human_decision": {
            "decision_kind": ("selected_option" if selected_option_id else "free_text"),
            "selected_option_id": selected_option_id,
            "submitted_text_digest": sha256(answer.encode("utf-8")).hexdigest(),
        },
        **observation,
    }


def _poll_existing_run(
    *,
    base_url: str,
    user_id: str,
    run_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    events_url = f"/api/runs/{run_id}/events"
    events = _events(
        base_url=base_url,
        events_url=events_url,
        user_id=user_id,
        request_timeout_seconds=min(EVENT_REQUEST_TIMEOUT_SECONDS, timeout_seconds),
    )
    if len(events) != 1:
        raise RuntimeError("gateway_customer_snapshot_unavailable")
    snapshot = _customer_snapshot(events[0])
    run_handle = snapshot["transport"].get("runHandle")
    state = snapshot.get("state")
    terminal_snapshot = (
        isinstance(state, Mapping)
        and state.get("status") in {"completed", "completed_with_limits"}
        and isinstance(state.get("answer"), Mapping)
    )
    if run_handle != run_id and not (run_handle is None and terminal_snapshot):
        raise RuntimeError("gateway_run_identity_invalid")
    observation = _poll_customer_snapshot(
        base_url=base_url,
        user_id=user_id,
        initial_snapshot=snapshot,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    return {"operation": "events_only", **observation}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run exactly one real WAJE conversation turn through the HTTP Gateway. "
            "The Gateway must already be running with Postgres, ClickHouse, and "
            "DeepSeek configured."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:3107")
    parser.add_argument("--user-id", default="human-led-test")
    parser.add_argument("--thread-id")
    parser.add_argument("--question")
    parser.add_argument("--run-id")
    parser.add_argument("--selected-option-id")
    parser.add_argument("--clarification-free-text")
    parser.add_argument(
        "--events-only",
        "--poll",
        action="store_true",
        dest="events_only",
        help="Poll one existing run without submitting a new user message.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    parser.add_argument("--request-identity")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    if args.question is not None and not args.question.strip():
        parser.error("--question must be non-empty")
    if args.selected_option_id is not None and not args.selected_option_id.strip():
        parser.error("--selected-option-id must be non-empty")
    if (
        args.clarification_free_text is not None
        and not args.clarification_free_text.strip()
    ):
        parser.error("--clarification-free-text must be non-empty")
    if args.selected_option_id is not None:
        args.selected_option_id = args.selected_option_id.strip()
    if args.clarification_free_text is not None:
        args.clarification_free_text = args.clarification_free_text.strip()

    first_turn = bool(
        args.question
        and not args.run_id
        and not args.selected_option_id
        and not args.clarification_free_text
        and not args.events_only
    )
    selected_option_resolution = bool(
        args.run_id
        and args.selected_option_id
        and not args.clarification_free_text
        and not args.events_only
    )
    free_text_resolution = bool(
        args.run_id
        and args.clarification_free_text
        and not args.selected_option_id
        and not args.events_only
    )
    events_only = bool(
        args.run_id
        and args.events_only
        and not args.selected_option_id
        and not args.clarification_free_text
    )
    if (
        sum((first_turn, selected_option_resolution, free_text_resolution, events_only))
        != 1
    ):
        parser.error(
            "provide exactly one operation: --question; "
            "--run-id with --selected-option-id; "
            "--run-id with --clarification-free-text; or "
            "--run-id with --events-only"
        )
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.poll_interval_seconds <= 0:
        parser.error("--poll-interval-seconds must be positive")
    if not args.user_id.strip():
        parser.error("--user-id must be non-empty")

    request_identity = (
        "" if events_only else (args.request_identity or f"gateway-once-{uuid4()}")
    )
    if first_turn:
        thread_id = args.thread_id or _create_thread(
            args.base_url,
            args.user_id,
            request_identity,
        )
        output = _run_first_turn(
            base_url=args.base_url,
            user_id=args.user_id,
            thread_id=thread_id,
            question=args.question,
            request_identity=request_identity,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    elif selected_option_resolution or free_text_resolution:
        answer = args.selected_option_id or args.clarification_free_text
        output = _submit_clarification_resolution(
            base_url=args.base_url,
            user_id=args.user_id,
            run_id=args.run_id,
            answer=answer,
            selected_option_id=(
                args.selected_option_id if selected_option_resolution else None
            ),
            request_identity=request_identity,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    else:
        output = _poll_existing_run(
            base_url=args.base_url,
            user_id=args.user_id,
            run_id=args.run_id,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )

    if request_identity:
        output["request_identity"] = request_identity
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if output.get("checkpoint_reached") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
