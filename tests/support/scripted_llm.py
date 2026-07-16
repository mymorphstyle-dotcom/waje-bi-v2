from __future__ import annotations

from collections import deque
from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping, Sequence

from bi_agent.runtime.llm_client import LLMResult


ScriptedLLMResult = LLMResult


class ScriptedLLMClient:
    """LLM test double that consumes only responses named by the test."""

    def __init__(
        self,
        responses: Mapping[
            str,
            Mapping[str, Any] | Sequence[Mapping[str, Any]],
        ],
    ) -> None:
        self.calls: list[str] = []
        self.audit_calls: list[dict[str, Any]] = []
        self._responses = {
            str(task): deque(_response_sequence(task, response))
            for task, response in responses.items()
        }

    def invoke_json(
        self,
        *,
        task: str,
        prompt_version: str,
        messages: Sequence[Mapping[str, str]],
        required_keys: Sequence[str],
    ) -> LLMResult:
        self.calls.append(task)
        scripted = self._responses.get(task)
        if not scripted:
            raise AssertionError(f"scripted_llm_response_missing:{task}")

        output = deepcopy(scripted.popleft())
        messages_payload = [dict(message) for message in messages]
        raw_response = json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True,
            default=repr,
        )
        audit = {
            "task": task,
            "provider": "scripted_test",
            "model": "scripted_test",
            "prompt_version": prompt_version,
            "response_id": f"scripted-{task}-{len(self.calls)}",
            "messages": messages_payload,
            "required_keys": list(required_keys),
            "raw_response_content": raw_response,
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:00+00:00",
            "duration_ms": 0.0,
            "input_hash": _stable_hash(messages_payload),
            "output_hash": _stable_hash(output),
            "usage": {},
            "structured_output": deepcopy(output),
        }
        self.audit_calls.append(audit)
        return LLMResult(output=output, audit=audit)

    def assert_exhausted(self) -> None:
        remaining = {
            task: len(responses)
            for task, responses in self._responses.items()
            if responses
        }
        if remaining:
            raise AssertionError(f"scripted_llm_responses_unused:{remaining}")


def _response_sequence(
    task: str,
    response: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    if isinstance(response, Mapping):
        return (dict(response),)
    if isinstance(response, (str, bytes)):
        raise TypeError(f"scripted_llm_response_invalid:{task}")

    outputs: list[dict[str, Any]] = []
    for item in response:
        if not isinstance(item, Mapping):
            raise TypeError(f"scripted_llm_response_invalid:{task}")
        outputs.append(dict(item))
    if not outputs:
        raise ValueError(f"scripted_llm_response_empty:{task}")
    return tuple(outputs)


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
