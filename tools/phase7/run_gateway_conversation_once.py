#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from uuid import uuid4


TERMINAL_RUN_STATUSES = frozenset(
    {
        "completed",
        "completed_without_workflow",
        "waiting_for_clarification",
        "failed",
    }
)
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    request_identity: str | None = None,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
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
        with urlopen(request, timeout=600) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"gateway_http_error:{exc.code}:{detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"gateway_unreachable:{exc.reason}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("gateway_response_shape_invalid")
    return parsed


def _events(base_url: str, events_url: Any) -> list[dict[str, Any]]:
    if not isinstance(events_url, str) or not events_url:
        return []
    request = Request(
        urljoin(base_url.rstrip("/") + "/", events_url.lstrip("/")),
        headers={"Accept": "text/event-stream"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"gateway_events_unavailable:{exc}") from exc
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            value = json.loads(line.removeprefix("data: "))
        except json.JSONDecodeError as exc:
            raise RuntimeError("gateway_event_invalid") from exc
        if isinstance(value, dict):
            events.append(value)
    return events


def _gateway_run_id(response: dict[str, Any]) -> str:
    candidates: list[Any] = []
    run = response.get("run")
    if isinstance(run, dict):
        candidates.append(run.get("id"))
    candidates.append(response.get("resumedRunId"))
    agent_core = response.get("agentCore")
    if isinstance(agent_core, dict):
        result = agent_core.get("result")
        if isinstance(result, dict):
            candidates.append(result.get("run_id"))
    run_ids = tuple(
        dict.fromkeys(
            str(candidate).strip()
            for candidate in candidates
            if isinstance(candidate, str) and candidate.strip()
        )
    )
    if len(run_ids) != 1:
        raise RuntimeError("gateway_run_identity_invalid")
    return run_ids[0]


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
    run_id: str,
    events_url: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    deadline = monotonic() + timeout_seconds
    observed: dict[str, dict[str, Any]] = {}
    terminal_status = ""
    poll_attempts = 0
    while True:
        snapshot = _events(base_url, events_url)
        poll_attempts += 1
        for event in snapshot:
            observed.setdefault(_event_identity(event), event)
        current_status = _latest_run_status(snapshot)
        if current_status:
            terminal_status = current_status
        if terminal_status in TERMINAL_RUN_STATUSES:
            return {
                "run_id": run_id,
                "events_url": events_url,
                "checkpoint_reached": True,
                "terminal_status": terminal_status,
                "timed_out": False,
                "poll_attempts": poll_attempts,
                "events": list(observed.values()),
            }
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {
                "run_id": run_id,
                "events_url": events_url,
                "checkpoint_reached": False,
                "terminal_status": terminal_status or "unknown",
                "timed_out": True,
                "poll_attempts": poll_attempts,
                "events": list(observed.values()),
            }
        sleep(min(poll_interval_seconds, remaining))


def _observe_gateway_response(
    *,
    base_url: str,
    response: dict[str, Any],
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    run_id = _gateway_run_id(response)
    events_url = response.get("eventsUrl")
    if not isinstance(events_url, str) or not events_url.strip():
        events_url = f"/api/runs/{run_id}/events"
    return _poll_run_events(
        base_url=base_url,
        run_id=run_id,
        events_url=events_url,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def _create_thread(base_url: str, owner_id: str) -> str:
    response = _json_request(
        base_url,
        "/api/threads",
        method="POST",
        payload={"ownerId": owner_id},
    )
    thread = response.get("thread")
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
        raise RuntimeError("gateway_thread_response_invalid")
    return str(thread["id"])


def _run_first_turn(
    *,
    base_url: str,
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
        payload={"message": question},
        request_identity=request_identity,
    )
    observation = _observe_gateway_response(
        base_url=base_url,
        response=response,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    return {
        "operation": "first_turn",
        "thread_id": thread_id,
        "gateway_response": response,
        **observation,
    }


def _resume_clarification(
    *,
    base_url: str,
    run_id: str,
    answer: str,
    selected_option_id: str | None,
    request_identity: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"answer": answer}
    if selected_option_id:
        payload["selectedOptionId"] = selected_option_id
    response = _json_request(
        base_url,
        f"/api/runs/{run_id}/clarifications",
        method="POST",
        payload=payload,
        request_identity=request_identity,
    )
    observation = _observe_gateway_response(
        base_url=base_url,
        response=response,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    return {
        "operation": "clarification_resume",
        "source_run_id": run_id,
        "gateway_response": response,
        **observation,
    }


def _poll_existing_run(
    *,
    base_url: str,
    run_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    observation = _poll_run_events(
        base_url=base_url,
        run_id=run_id,
        events_url=f"/api/runs/{run_id}/events",
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
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--owner-id", default="human-led-test")
    parser.add_argument("--thread-id")
    parser.add_argument("--question")
    parser.add_argument("--run-id")
    parser.add_argument("--clarification-answer")
    parser.add_argument("--selected-option-id")
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

    first_turn = bool(
        args.question
        and not args.run_id
        and not args.clarification_answer
        and not args.events_only
    )
    resume = bool(
        args.run_id
        and args.clarification_answer
        and not args.events_only
    )
    events_only = bool(
        args.run_id
        and args.events_only
        and not args.clarification_answer
    )
    if sum((first_turn, resume, events_only)) != 1:
        parser.error(
            "provide exactly one operation: --question; "
            "--run-id with --clarification-answer; or "
            "--run-id with --events-only"
        )
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.poll_interval_seconds <= 0:
        parser.error("--poll-interval-seconds must be positive")
    if args.selected_option_id and not resume:
        parser.error("--selected-option-id requires clarification resume")

    request_identity = (
        ""
        if events_only
        else (args.request_identity or f"gateway-once-{uuid4()}")
    )
    if first_turn:
        thread_id = args.thread_id or _create_thread(
            args.base_url,
            args.owner_id,
        )
        output = _run_first_turn(
            base_url=args.base_url,
            thread_id=thread_id,
            question=args.question,
            request_identity=request_identity,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    elif resume:
        output = _resume_clarification(
            base_url=args.base_url,
            run_id=args.run_id,
            answer=args.clarification_answer,
            selected_option_id=args.selected_option_id,
            request_identity=request_identity,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    else:
        output = _poll_existing_run(
            base_url=args.base_url,
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
