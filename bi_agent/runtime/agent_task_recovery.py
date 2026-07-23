from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from bi_agent.runtime.agent_turn_runtime import AgentTaskCompletion
from bi_agent.runtime.analysis_artifacts import AnalysisArtifactRegistry
from bi_agent.runtime.evidence_authority import canonical_value


class AnalysisTaskStateStore(Protocol):
    def get_run_state(self, run_id: str) -> Mapping[str, Any] | None: ...


class AgentTaskRecoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AuthoritativeAgentTaskCompletionLoader:
    """Projects durable BI task state into a customer-safe Agent completion."""

    def __init__(
        self,
        *,
        store: AnalysisTaskStateStore,
        artifact_registry: AnalysisArtifactRegistry,
        artifact_limit: int = 100,
    ) -> None:
        if isinstance(artifact_limit, bool) or artifact_limit < 1:
            raise ValueError("agent_task_artifact_limit_invalid")
        self._store = store
        self._artifact_registry = artifact_registry
        self._artifact_limit = artifact_limit

    def load_task_completion(
        self,
        *,
        thread_id: str,
        task_ref: str,
    ) -> AgentTaskCompletion | None:
        _exact_text(thread_id, "agent_task_thread_id_invalid")
        _exact_text(task_ref, "agent_task_ref_invalid")
        state = self._store.get_run_state(task_ref)
        if state is None:
            raise AgentTaskRecoveryError("agent_task_state_missing")
        if str(state.get("run_id") or "") != task_ref:
            raise AgentTaskRecoveryError("agent_task_state_identity_mismatch")
        if str(state.get("thread_id") or "") != thread_id:
            raise AgentTaskRecoveryError("agent_task_state_owner_mismatch")
        status = str(state.get("status") or "")
        if status == "failed":
            return AgentTaskCompletion(
                taskRef=task_ref,
                status="failed",
                customerSummary="本次分析任务未能完成，请稍后重试。",
                customerAnswerMarkdown=None,
                artifactRefs=[],
                materialRefs=[],
                limitationRefs=[],
                relevantMaterials=[],
            )
        if status != "completed":
            if status in {
                "queued",
                "running",
                "planned",
                "evidence_ready",
                "authority_sealed",
                "narrative_ready",
                "waiting_for_clarification",
            }:
                return None
            raise AgentTaskRecoveryError("agent_task_state_invalid")

        publications = tuple(
            item
            for item in self._artifact_registry.list_task_artifacts(
                thread_id,
                task_ref,
                limit=self._artifact_limit,
            )
            if item.descriptor.artifact_type == "bi_publication"
        )
        if not publications:
            terminal_failure = _terminal_non_publication_failure(state)
            if terminal_failure is not None:
                return AgentTaskCompletion(
                    taskRef=task_ref,
                    status="failed",
                    customerSummary=terminal_failure,
                    customerAnswerMarkdown=None,
                    artifactRefs=[],
                    materialRefs=[],
                    limitationRefs=[],
                    relevantMaterials=[],
                )
            raise AgentTaskRecoveryError("agent_task_publication_missing")
        publications = (max(publications, key=_publication_order),)

        relevant_materials: list[dict[str, Any]] = []
        limitation_refs: list[str] = []
        for registered in publications:
            descriptor = registered.descriptor
            detail = canonical_value(dict(registered.detail))
            if not isinstance(detail, dict):
                raise AgentTaskRecoveryError("agent_task_publication_invalid")
            raw_limitations = detail.get("limitationRefs")
            if raw_limitations is not None:
                if not isinstance(raw_limitations, list) or any(
                    not isinstance(ref, str) or not ref.strip()
                    for ref in raw_limitations
                ):
                    raise AgentTaskRecoveryError(
                        "agent_task_publication_limitations_invalid"
                    )
                limitation_refs.extend(raw_limitations)
            relevant_materials.append(
                {
                    "material_ref": descriptor.artifact_ref,
                    "kind": descriptor.artifact_type,
                    "summary": descriptor.customer_summary,
                    "source_refs": list(descriptor.source_refs),
                    "detail": detail,
                }
            )

        unique_limitations = list(dict.fromkeys(limitation_refs))
        summaries = [
            item.descriptor.customer_summary
            for item in publications
            if item.descriptor.customer_summary
        ]
        customer_answer_markdown = _publication_answer_markdown(
            relevant_materials[0]
        )
        return AgentTaskCompletion(
            taskRef=task_ref,
            status=(
                "completed_with_limits" if unique_limitations else "completed"
            ),
            customerSummary=(
                summaries[0]
                if summaries
                else "BI 分析任务已完成并形成可追溯发布材料。"
            ),
            customerAnswerMarkdown=customer_answer_markdown,
            artifactRefs=[
                item.descriptor.artifact_ref for item in publications
            ],
            materialRefs=[],
            limitationRefs=unique_limitations,
            relevantMaterials=relevant_materials,
        )


def _exact_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(code)
    return value


def _terminal_non_publication_failure(state: Mapping[str, Any]) -> str | None:
    request = state.get("request")
    if not isinstance(request, Mapping):
        return None
    post_execution_status = str(request.get("post_execution_status") or "")
    publication_status = str(request.get("publication_status") or "")
    expected_publication_status = {
        "narrative_failed": "not_ready",
        "publication_failed": "failed",
    }.get(post_execution_status)
    if expected_publication_status is None:
        return None
    if publication_status != expected_publication_status:
        raise AgentTaskRecoveryError("agent_task_terminal_failure_state_invalid")
    return "本次分析未形成可发布回答，请稍后重试。"


def _publication_order(value: Any) -> tuple[float, str]:
    created_at = value.descriptor.created_at
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AgentTaskRecoveryError(
            "agent_task_publication_timestamp_invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise AgentTaskRecoveryError("agent_task_publication_timestamp_invalid")
    return parsed.timestamp(), value.descriptor.artifact_ref


def _publication_answer_markdown(material: Mapping[str, Any]) -> str:
    detail = material.get("detail")
    if not isinstance(detail, Mapping):
        raise AgentTaskRecoveryError("agent_task_publication_invalid")
    publication = detail.get("publication")
    if not isinstance(publication, Mapping):
        raise AgentTaskRecoveryError("agent_task_publication_invalid")
    blocks = publication.get("blocks")
    if (
        isinstance(blocks, (str, bytes))
        or not isinstance(blocks, list)
        or not blocks
    ):
        raise AgentTaskRecoveryError("agent_task_publication_blocks_invalid")
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            raise AgentTaskRecoveryError("agent_task_publication_blocks_invalid")
        text = block.get("text")
        if not isinstance(text, str) or not text or text != text.strip():
            raise AgentTaskRecoveryError("agent_task_publication_blocks_invalid")
        texts.append(text)
    return "\n\n".join(texts)
