from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bi_agent.conversation.material_revision_continuation import (
    MATERIAL_REVISION_PLAN_FIELDS,
)
from bi_agent.runtime.agent_sdk_contracts import AgentToolResult, WajeAgentTool
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


BI_ANALYSIS_TOOL_SCHEMA_VERSION = "bi-analysis-tool-submission.v1"


class RunBiAnalysisInput(BaseModel):
    """Open business question submitted to the existing BI authority workflow."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    business_question: str = Field(alias="businessQuestion", min_length=1)

    @field_validator("business_question")
    @classmethod
    def validate_business_question(cls, value: str) -> str:
        return _exact_text(value, "bi_analysis_business_question_invalid")


class ContinueBiAnalysisInput(BaseModel):
    """Material revision of one published BI analysis task."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source_task_ref: str = Field(alias="sourceTaskRef", min_length=1)
    revision_request: str = Field(alias="revisionRequest", min_length=1)
    superseded_plan_fields: list[str] = Field(
        alias="supersededPlanFields",
        min_length=1,
    )

    @field_validator("source_task_ref")
    @classmethod
    def validate_source_task_ref(cls, value: str) -> str:
        return _exact_text(value, "bi_analysis_source_task_ref_invalid")

    @field_validator("revision_request")
    @classmethod
    def validate_revision_request(cls, value: str) -> str:
        return _exact_text(value, "bi_analysis_revision_request_invalid")

    @field_validator("superseded_plan_fields")
    @classmethod
    def validate_superseded_plan_fields(cls, values: list[str]) -> list[str]:
        if (
            not values
            or any(value not in MATERIAL_REVISION_PLAN_FIELDS for value in values)
            or len(values) != len(set(values))
        ):
            raise ValueError("bi_analysis_superseded_plan_fields_invalid")
        return values


@dataclass(frozen=True)
class BiAnalysisTaskSubmission:
    task_ref: str
    task_state: Literal["queued"]
    replayed: bool
    source_task_ref: str | None = None

    def __post_init__(self) -> None:
        _exact_text(self.task_ref, "bi_analysis_task_ref_invalid")
        if self.source_task_ref is not None:
            _exact_text(
                self.source_task_ref,
                "bi_analysis_source_task_ref_invalid",
            )

    def to_contract(self, *, operation: str) -> dict[str, Any]:
        return {
            "operation": operation,
            "taskRef": self.task_ref,
            "taskState": self.task_state,
            "sourceTaskRef": self.source_task_ref,
            "replayed": self.replayed,
        }


class BiAnalysisTaskGateway(Protocol):
    """Durable task submission boundary; it does not execute LangGraph inline."""

    def start_analysis(
        self,
        *,
        thread_id: str,
        source_message_id: str,
        operation_id: str,
        business_question: str,
    ) -> BiAnalysisTaskSubmission: ...

    def continue_analysis(
        self,
        *,
        thread_id: str,
        source_message_id: str,
        operation_id: str,
        source_task_ref: str,
        revision_request: str,
        superseded_plan_fields: Sequence[str],
    ) -> BiAnalysisTaskSubmission: ...


class BiAnalysisToolError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryability: Literal["never", "same_input", "replan_required"],
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryability = retryability


class PostgresBiAnalysisTaskGateway:
    """Writes BI tool requests into the existing recoverable dispatch ledger."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def start_analysis(
        self,
        *,
        thread_id: str,
        source_message_id: str,
        operation_id: str,
        business_question: str,
    ) -> BiAnalysisTaskSubmission:
        question = _exact_text(
            business_question,
            "bi_analysis_business_question_invalid",
        )
        return self._submit(
            thread_id=thread_id,
            source_message_id=source_message_id,
            operation_id=operation_id,
            tool_name="run_bi_analysis",
            request_payload={"message": question},
            source_task_ref=None,
        )

    def continue_analysis(
        self,
        *,
        thread_id: str,
        source_message_id: str,
        operation_id: str,
        source_task_ref: str,
        revision_request: str,
        superseded_plan_fields: Sequence[str],
    ) -> BiAnalysisTaskSubmission:
        thread_id = _exact_text(thread_id, "bi_analysis_thread_id_invalid")
        source_task_ref = _exact_text(
            source_task_ref,
            "bi_analysis_source_task_ref_invalid",
        )
        revision_request = _exact_text(
            revision_request,
            "bi_analysis_revision_request_invalid",
        )
        plan_fields = _plan_fields(superseded_plan_fields)
        reason_ref = _revision_reason_ref(
            thread_id=thread_id,
            operation_id=operation_id,
            source_task_ref=source_task_ref,
        )
        try:
            self.connection.execute("BEGIN")
            source = self.connection.execute(
                """
                /* bi_analysis_tool_source_publication */
                SELECT run.run_id, run.thread_id, run.status,
                       intent.intent_revision_id,
                       transition.transition_id
                FROM waje_runtime.analysis_runs run
                JOIN waje_runtime.intent_revisions intent
                  ON intent.run_attempt_id = run.run_id
                LEFT JOIN waje_runtime.intent_revision_supersessions supersession
                  ON supersession.superseded_intent_revision_id =
                     intent.intent_revision_id
                JOIN LATERAL (
                  SELECT attempt.transition_id
                  FROM waje_runtime.workflow_transition_attempts attempt
                  WHERE attempt.run_attempt_id = run.run_id
                    AND attempt.acceptance_state = 'accepted'
                  ORDER BY attempt.created_at DESC, attempt.attempt_id DESC
                  LIMIT 1
                ) transition ON true
                WHERE run.run_id = %(source_task_ref)s
                  AND supersession.superseded_intent_revision_id IS NULL
                  AND EXISTS (
                    SELECT 1
                    FROM waje_runtime.publication_customer_payloads publication
                    WHERE publication.run_attempt_id = run.run_id
                  )
                ORDER BY intent.created_at DESC, intent.intent_revision_id DESC
                LIMIT 2
                FOR UPDATE OF run
                """,
                {"source_task_ref": source_task_ref},
            ).fetchall()
            if not source:
                raise BiAnalysisToolError(
                    "bi_analysis_published_source_missing",
                    retryability="replan_required",
                )
            if len(source) != 1:
                raise BiAnalysisToolError(
                    "bi_analysis_source_authority_ambiguous",
                    retryability="never",
                )
            source_row = source[0]
            if str(_field(source_row, "thread_id", 1) or "") != thread_id:
                raise BiAnalysisToolError(
                    "bi_analysis_source_thread_mismatch",
                    retryability="never",
                )
            source_intent_revision_id = str(
                _field(source_row, "intent_revision_id", 3) or ""
            )
            parent_transition_id = str(_field(source_row, "transition_id", 4) or "")
            if not source_intent_revision_id or not parent_transition_id:
                raise BiAnalysisToolError(
                    "bi_analysis_source_authority_incomplete",
                    retryability="never",
                )
        except BiAnalysisToolError:
            self.connection.rollback()
            raise
        except Exception as exc:
            self.connection.rollback()
            raise BiAnalysisToolError(
                "bi_analysis_source_lookup_failed",
                retryability="same_input",
            ) from exc

        try:
            return self._submit(
                thread_id=thread_id,
                source_message_id=source_message_id,
                operation_id=operation_id,
                tool_name="continue_bi_analysis",
                request_payload={
                    "message": revision_request,
                    "intentRevisionContext": {
                        "supersedes_intent_revision_id": (source_intent_revision_id),
                        "superseded_plan_fields": list(plan_fields),
                        "intent_revision_reason_ref": reason_ref,
                        "parent_transition_id": parent_transition_id,
                    },
                },
                source_task_ref=source_task_ref,
                transaction_open=True,
            )
        except Exception:
            # _submit owns rollback and typed mapping after it receives the open
            # transaction. This guard covers validation failures before that point.
            self.connection.rollback()
            raise

    def _submit(
        self,
        *,
        thread_id: str,
        source_message_id: str,
        operation_id: str,
        tool_name: str,
        request_payload: Mapping[str, Any],
        source_task_ref: str | None,
        transaction_open: bool = False,
    ) -> BiAnalysisTaskSubmission:
        thread_id = _exact_text(thread_id, "bi_analysis_thread_id_invalid")
        source_message_id = _exact_text(
            source_message_id,
            "bi_analysis_source_message_id_invalid",
        )
        operation_id = _exact_text(operation_id, "bi_analysis_operation_id_invalid")
        request_payload = canonical_value(request_payload)
        if not isinstance(request_payload, dict):
            raise BiAnalysisToolError(
                "bi_analysis_request_payload_invalid",
                retryability="never",
            )
        request_identity = f"agent-tool:{tool_name}:{operation_id}"
        request_digest = canonical_digest(
            {
                "producer_kind": "thread_message",
                "scope_ref": thread_id,
                "thread_id": thread_id,
                "request_payload": request_payload,
            }
        )
        identity_digest = canonical_digest(
            {
                "schema_version": BI_ANALYSIS_TOOL_SCHEMA_VERSION,
                "thread_id": thread_id,
                "request_identity": request_identity,
            }
        )
        task_ref = f"run-{identity_digest[:24]}"
        dispatch_ref = f"dispatch-{identity_digest[:24]}"
        params = {
            "thread_id": thread_id,
            "source_message_id": source_message_id,
            "operation_key": f"user:{operation_id}",
            "request_identity": request_identity,
            "request_digest": request_digest,
            "request_payload": _json(request_payload),
            "task_ref": task_ref,
            "dispatch_ref": dispatch_ref,
            "tool_name": tool_name,
            "source_task_ref": source_task_ref,
        }
        try:
            if not transaction_open:
                self.connection.execute("BEGIN")
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(request_identity)s, 0))",
                params,
            )
            existing = self.connection.execute(
                """
                /* bi_analysis_tool_dispatch_replay */
                SELECT dispatch.run_id, dispatch.thread_id, dispatch.message_id,
                       dispatch.request_digest, dispatch.request_payload,
                       run.status
                FROM waje_runtime.run_dispatches dispatch
                JOIN waje_runtime.analysis_runs run
                  ON run.run_id = dispatch.run_id
                WHERE dispatch.producer_kind = 'thread_message'
                  AND dispatch.scope_ref = %(thread_id)s
                  AND dispatch.request_identity = %(request_identity)s
                """,
                params,
            ).fetchone()
            if existing is not None:
                if (
                    str(_field(existing, "run_id", 0) or "") != task_ref
                    or str(_field(existing, "thread_id", 1) or "") != thread_id
                    or str(_field(existing, "message_id", 2) or "") != source_message_id
                    or str(_field(existing, "request_digest", 3) or "")
                    != request_digest
                    or canonical_value(
                        _json_value(_field(existing, "request_payload", 4)) or {}
                    )
                    != request_payload
                ):
                    raise BiAnalysisToolError(
                        "bi_analysis_tool_replay_conflict",
                        retryability="never",
                    )
                self.connection.commit()
                return BiAnalysisTaskSubmission(
                    task_ref=task_ref,
                    task_state="queued",
                    replayed=True,
                    source_task_ref=source_task_ref,
                )
            thread = self.connection.execute(
                """
                SELECT thread_id
                FROM waje_runtime.investigation_threads
                WHERE thread_id = %(thread_id)s
                FOR UPDATE
                """,
                params,
            ).fetchone()
            if thread is None:
                raise BiAnalysisToolError(
                    "bi_analysis_thread_missing",
                    retryability="never",
                )
            source_message = self.connection.execute(
                """
                /* bi_analysis_tool_source_message */
                SELECT message_id, thread_id, item_type, operation_key, role
                FROM waje_runtime.conversation_messages
                WHERE message_id = %(source_message_id)s
                FOR SHARE
                """,
                params,
            ).fetchone()
            if source_message is None:
                raise BiAnalysisToolError(
                    "bi_analysis_source_message_missing",
                    retryability="never",
                )
            if (
                str(_field(source_message, "thread_id", 1) or "") != thread_id
                or str(_field(source_message, "item_type", 2) or "") != "user_message"
                or str(_field(source_message, "operation_key", 3) or "")
                != params["operation_key"]
                or str(_field(source_message, "role", 4) or "") != "user"
            ):
                raise BiAnalysisToolError(
                    "bi_analysis_source_message_conflict",
                    retryability="never",
                )
            self.connection.execute(
                """
                INSERT INTO waje_runtime.analysis_runs(
                  run_id, run_attempt_id, thread_id, status
                ) VALUES (%(task_ref)s, %(task_ref)s, %(thread_id)s, 'queued')
                """,
                params,
            )
            self.connection.execute(
                """
                INSERT INTO waje_runtime.run_dispatches(
                  dispatch_id, producer_kind, scope_ref, request_identity,
                  request_digest, request_payload, thread_id, run_id, message_id
                ) VALUES (
                  %(dispatch_ref)s, 'thread_message', %(thread_id)s,
                  %(request_identity)s, %(request_digest)s,
                  %(request_payload)s::jsonb, %(thread_id)s, %(task_ref)s,
                  %(source_message_id)s
                )
                """,
                params,
            )
            self.connection.execute(
                """
                UPDATE waje_runtime.investigation_threads
                SET active_task_id = %(task_ref)s,
                    customer_state = 'working',
                    updated_at = now()
                WHERE thread_id = %(thread_id)s
                """,
                params,
            )
            self.connection.execute(
                """
                INSERT INTO waje_runtime.audit_events(
                  event_type, actor_id, thread_id, run_id, ref, payload
                ) VALUES (
                  'bi_analysis_tool_queued', 'agent-runtime', %(thread_id)s,
                  %(task_ref)s, %(dispatch_ref)s,
                  jsonb_build_object(
                    'toolName', %(tool_name)s,
                    'dispatchId', %(dispatch_ref)s,
                    'sourceTaskRef', %(source_task_ref)s
                  )
                )
                """,
                params,
            )
            self.connection.commit()
        except BiAnalysisToolError:
            self.connection.rollback()
            raise
        except Exception as exc:
            self.connection.rollback()
            raise BiAnalysisToolError(
                "bi_analysis_task_submission_failed",
                retryability="same_input",
            ) from exc
        return BiAnalysisTaskSubmission(
            task_ref=task_ref,
            task_state="queued",
            replayed=False,
            source_task_ref=source_task_ref,
        )


def bi_analysis_tools(
    *,
    gateway: BiAnalysisTaskGateway,
    thread_id: str,
    source_message_id: str,
    operation_id: str,
) -> tuple[WajeAgentTool, WajeAgentTool]:
    """Build SDK-neutral BI tools bound to one durable agent turn."""

    for value, code in (
        (thread_id, "bi_analysis_thread_id_invalid"),
        (source_message_id, "bi_analysis_source_message_id_invalid"),
        (operation_id, "bi_analysis_operation_id_invalid"),
    ):
        _exact_text(value, code)

    def run_analysis(arguments: Mapping[str, Any]) -> AgentToolResult:
        try:
            request = RunBiAnalysisInput.model_validate(arguments)
            submission = gateway.start_analysis(
                thread_id=thread_id,
                source_message_id=source_message_id,
                operation_id=operation_id,
                business_question=request.business_question,
            )
        except Exception as exc:
            return _failure_result(exc)
        return _submission_result(submission, operation="run_bi_analysis")

    def continue_analysis(arguments: Mapping[str, Any]) -> AgentToolResult:
        try:
            request = ContinueBiAnalysisInput.model_validate(arguments)
            submission = gateway.continue_analysis(
                thread_id=thread_id,
                source_message_id=source_message_id,
                operation_id=operation_id,
                source_task_ref=request.source_task_ref,
                revision_request=request.revision_request,
                superseded_plan_fields=request.superseded_plan_fields,
            )
        except Exception as exc:
            return _failure_result(exc)
        return _submission_result(submission, operation="continue_bi_analysis")

    return (
        WajeAgentTool(
            name="run_bi_analysis",
            description=(
                "Start a durable BI investigation for an open business question. "
                "The task runs through WAJE IntentRevision, PlanRevision, SQL safety, "
                "evidence, claim, publication, and delivery authorities."
            ),
            input_model=RunBiAnalysisInput,
            handler=run_analysis,
        ),
        WajeAgentTool(
            name="continue_bi_analysis",
            description=(
                "Create a material revision of one published BI task. Provide the "
                "new business request and the accepted PlanRevision fields it replaces."
            ),
            input_model=ContinueBiAnalysisInput,
            handler=continue_analysis,
        ),
    )


def _submission_result(
    submission: BiAnalysisTaskSubmission,
    *,
    operation: str,
) -> AgentToolResult:
    return AgentToolResult(
        status="succeeded",
        output=submission.to_contract(operation=operation),
        artifactRefs=[],
        materialRefs=[],
        limitationRefs=[],
        retryability="never",
        customerSummary="BI 分析任务已进入持久化执行队列。",
        technicalDetailRef=None,
    )


def _failure_result(error: Exception) -> AgentToolResult:
    retryability: Literal["never", "same_input", "replan_required"] = "same_input"
    if isinstance(error, BiAnalysisToolError):
        retryability = error.retryability
    return AgentToolResult(
        status="failed",
        output=None,
        artifactRefs=[],
        materialRefs=[],
        limitationRefs=[],
        retryability=retryability,
        customerSummary=(
            "当前 BI 分析任务未能提交，请稍后重试。"
            if retryability == "same_input"
            else "当前请求无法基于指定材料继续，请调整任务或修订范围。"
        ),
        technicalDetailRef=None,
    )


def _plan_fields(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise BiAnalysisToolError(
            "bi_analysis_superseded_plan_fields_invalid",
            retryability="never",
        )
    normalized = tuple(values)
    if (
        not normalized
        or any(value not in MATERIAL_REVISION_PLAN_FIELDS for value in normalized)
        or len(normalized) != len(set(normalized))
    ):
        raise BiAnalysisToolError(
            "bi_analysis_superseded_plan_fields_invalid",
            retryability="never",
        )
    return normalized


def _revision_reason_ref(
    *,
    thread_id: str,
    operation_id: str,
    source_task_ref: str,
) -> str:
    digest = canonical_digest(
        {
            "schema_version": BI_ANALYSIS_TOOL_SCHEMA_VERSION,
            "tool_name": "continue_bi_analysis",
            "thread_id": thread_id,
            "operation_id": operation_id,
            "source_task_ref": source_task_ref,
        }
    )
    return f"agent-tool-revision:sha256:{digest}"


def _exact_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(code)
    return value


def _field(row: Any, name: str, index: int) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _json(value: Any) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value
