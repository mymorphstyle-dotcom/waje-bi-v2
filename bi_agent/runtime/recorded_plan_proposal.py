from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_value
from bi_agent.runtime.llm_client import (
    LLMConfigurationError,
    LLMOutputError,
    LLMResult,
    _hash_json,
    _json_byte_count,
)


_PLAN_TASK = "single_authority_plan_proposal"
_INPUT_OPEN = "<input_json>\n"
_INPUT_CLOSE = "\n</input_json>"
_INTENT_SEMANTIC_FIELDS = (
    "schema_version",
    "original_user_text",
    "goal_bindings",
    "target_metric_refs",
    "scope",
    "time_spec",
    "comparison_spec",
    "direction_premise",
    "requested_analysis_axes",
    "requested_factor_refs",
    "desired_decisions",
    "source_spans",
)
_DECISION_SEMANTIC_FIELDS = (
    "slot_id",
    "status",
    "source",
    "materiality",
    "option_id",
    "value",
    "affected_plan_fields",
)
_AUTHORITY_SEMANTIC_FIELDS = (
    "contract_versions",
    "dataset_coverage",
    "release_refs",
    "snapshot_refs",
)


class RecordedPlanProposalClient:
    """Replay one accepted DeepSeek plan proposal for a controlled A/B run."""

    supports_output_validator = True

    def __init__(
        self,
        provider_client: Any,
        *,
        connection: Any,
        source_run_id: str,
    ) -> None:
        if not callable(getattr(provider_client, "invoke_json", None)):
            raise LLMConfigurationError("recorded_plan_provider_invalid")
        if not callable(getattr(connection, "execute", None)):
            raise LLMConfigurationError("recorded_plan_connection_invalid")
        if (
            not isinstance(source_run_id, str)
            or not source_run_id.startswith("run-")
            or source_run_id != source_run_id.strip()
        ):
            raise LLMConfigurationError("recorded_plan_source_run_invalid")
        self._provider_client = provider_client
        self._connection = connection
        self._source_run_id = source_run_id
        self.durable_max_attempts = getattr(
            provider_client,
            "durable_max_attempts",
            1,
        )
        self.supports_model_tier = bool(
            getattr(provider_client, "supports_model_tier", False)
        )
        self.supports_thinking_mode = bool(
            getattr(provider_client, "supports_thinking_mode", False)
        )

    def invoke_json(
        self,
        *,
        task: str,
        prompt_version: str,
        messages: Sequence[Mapping[str, str]],
        required_keys: Sequence[str],
        output_validator: Callable[[Mapping[str, Any]], None] | None = None,
        model_tier: str = "default",
        thinking: str | None = None,
    ) -> Any:
        if task != _PLAN_TASK:
            return self._provider_client.invoke_json(
                task=task,
                prompt_version=prompt_version,
                messages=messages,
                required_keys=required_keys,
                output_validator=output_validator,
                model_tier=model_tier,
                thinking=thinking,
            )
        attempt_ref, source_payload = self._load_source()
        output = source_payload.get("output")
        source_audit = source_payload.get("audit")
        if not isinstance(output, Mapping) or not isinstance(source_audit, Mapping):
            raise LLMConfigurationError("recorded_plan_source_payload_invalid")
        if (
            source_audit.get("task") != task
            or source_audit.get("prompt_version") != prompt_version
            or source_audit.get("model_tier", "default") != model_tier
            or source_audit.get("thinking") != thinking
        ):
            raise LLMConfigurationError("recorded_plan_source_profile_mismatch")
        source_messages = source_audit.get("messages")
        if (
            isinstance(source_messages, (str, bytes))
            or not isinstance(source_messages, Sequence)
            or _plan_input_projection(source_messages)
            != _plan_input_projection(messages)
        ):
            raise LLMConfigurationError("recorded_plan_input_mismatch")
        missing = [key for key in required_keys if key not in output]
        if missing:
            raise LLMOutputError(
                f"missing_llm_output_keys:{','.join(missing)}",
                retryable=False,
            )
        if output_validator is not None:
            try:
                output_validator(output)
            except (ValueError, LLMOutputError) as exc:
                raise LLMOutputError(
                    str(exc).strip() or "recorded_plan_output_contract_invalid",
                    retryable=False,
                ) from exc
        raw_response = source_audit.get("raw_response_content")
        provider = source_audit.get("provider")
        model = source_audit.get("model")
        if (
            not isinstance(raw_response, str)
            or not raw_response
            or not isinstance(provider, str)
            or not provider
            or not isinstance(model, str)
            or not model
        ):
            raise LLMConfigurationError("recorded_plan_source_audit_invalid")
        current_provider = getattr(self._provider_client, "provider", provider)
        current_model = (
            getattr(self._provider_client, "critical_model", None)
            if model_tier == "critical"
            else getattr(self._provider_client, "model", None)
        )
        if current_provider != provider or (
            isinstance(current_model, str) and current_model != model
        ):
            raise LLMConfigurationError("recorded_plan_source_model_mismatch")
        timestamp = datetime.now(timezone.utc).isoformat()
        normalized_messages = [dict(message) for message in messages]
        normalized_output = dict(canonical_value(output))
        return LLMResult(
            output=normalized_output,
            audit={
                "task": task,
                "provider": provider,
                "model": model,
                "model_tier": model_tier,
                "thinking": thinking,
                "reasoning_content_present": bool(
                    source_audit.get("reasoning_content_present", False)
                ),
                "prompt_version": prompt_version,
                "response_id": "recorded-plan-proposal:" + attempt_ref,
                "messages": normalized_messages,
                "required_keys": list(required_keys),
                "raw_response_content": raw_response,
                "started_at": timestamp,
                "finished_at": timestamp,
                "duration_ms": 0.0,
                "attempt_count": 1,
                "input_hash": _hash_json(normalized_messages),
                "output_hash": _hash_json(normalized_output),
                "base_url_hash": source_audit.get("base_url_hash", ""),
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                "finish_reason": "recorded_plan_replay",
                "input_bytes": _json_byte_count(normalized_messages),
                "output_bytes": len(raw_response.encode("utf-8")),
                "structured_output": normalized_output,
                "recorded_plan_replay": {
                    "source_run_id": self._source_run_id,
                    "source_attempt_ref": attempt_ref,
                    "source_output_hash": source_audit.get("output_hash", ""),
                },
            },
        )

    def _load_source(self) -> tuple[str, Mapping[str, Any]]:
        row = self._connection.execute(
            """
            SELECT acceptance.accepted_attempt_ref, acceptance.output_payload
            FROM waje_runtime.durable_call_acceptances acceptance
            JOIN waje_runtime.durable_call_attempts attempt
              ON attempt.attempt_ref = acceptance.accepted_attempt_ref
            WHERE acceptance.run_attempt_id = %(run_attempt_id)s
              AND attempt.operation_name = 'single_authority_plan_proposal'
              AND attempt.call_kind = 'planner_provider'
              AND attempt.stage_name = 'compile_authoritative_plan'
            ORDER BY acceptance.created_at DESC
            LIMIT 2
            """,
            {"run_attempt_id": self._source_run_id},
        ).fetchall()
        if len(row) != 1:
            raise LLMConfigurationError("recorded_plan_source_acceptance_invalid")
        record = row[0]
        attempt_ref = (
            record.get("accepted_attempt_ref")
            if isinstance(record, Mapping)
            else record[0]
        )
        output_payload = (
            record.get("output_payload")
            if isinstance(record, Mapping)
            else record[1]
        )
        if (
            not isinstance(attempt_ref, str)
            or not attempt_ref
            or not isinstance(output_payload, Mapping)
        ):
            raise LLMConfigurationError("recorded_plan_source_acceptance_invalid")
        return attempt_ref, output_payload


def _plan_input_projection(
    messages: Sequence[Mapping[str, str]],
) -> Mapping[str, Any]:
    if len(messages) != 2:
        raise LLMConfigurationError("recorded_plan_messages_invalid")
    user_content = messages[1].get("content")
    if not isinstance(user_content, str):
        raise LLMConfigurationError("recorded_plan_messages_invalid")
    try:
        raw_payload = user_content.split(_INPUT_OPEN, 1)[1].split(
            _INPUT_CLOSE,
            1,
        )[0]
        payload = json.loads(raw_payload)
    except (IndexError, json.JSONDecodeError) as exc:
        raise LLMConfigurationError("recorded_plan_messages_invalid") from exc
    if not isinstance(payload, Mapping):
        raise LLMConfigurationError("recorded_plan_messages_invalid")
    intent = payload.get("intent_revision")
    decisions = payload.get("active_decisions")
    authority = payload.get("authority_context")
    ambiguity_slots = intent.get("ambiguity_slots") if isinstance(intent, Mapping) else None
    if (
        not isinstance(intent, Mapping)
        or isinstance(decisions, (str, bytes))
        or not isinstance(decisions, Sequence)
        or not isinstance(authority, Mapping)
        or isinstance(ambiguity_slots, (str, bytes))
        or not isinstance(ambiguity_slots, Sequence)
    ):
        raise LLMConfigurationError("recorded_plan_messages_invalid")
    normalized_slots = []
    for slot in ambiguity_slots:
        if not isinstance(slot, Mapping):
            raise LLMConfigurationError("recorded_plan_messages_invalid")
        normalized_slots.append(
            {
                key: slot.get(key)
                for key in (
                    "slot_id",
                    "slot_kind",
                    "materiality",
                    "allowed_value_refs",
                    "status",
                )
            }
        )
    normalized_decisions = []
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise LLMConfigurationError("recorded_plan_messages_invalid")
        normalized_decisions.append(
            {
                key: decision.get(key)
                for key in _DECISION_SEMANTIC_FIELDS
            }
        )
    return canonical_value(
        {
            "intent_revision": {
                **{
                    key: intent.get(key)
                    for key in _INTENT_SEMANTIC_FIELDS
                },
                "ambiguity_slots": normalized_slots,
            },
            "active_decisions": normalized_decisions,
            "authority_context": {
                key: authority.get(key)
                for key in _AUTHORITY_SEMANTIC_FIELDS
            },
            "goal_contracts": payload.get("goal_contracts"),
            "analysis_axis_catalog": payload.get("analysis_axis_catalog"),
            "capability_summaries": payload.get("capability_summaries"),
        }
    )
