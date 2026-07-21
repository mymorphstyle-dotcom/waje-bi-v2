from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bi_agent.runtime.agent_sdk_contracts import (
    AgentToolResult,
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentTool,
)
from bi_agent.runtime.analysis_artifacts import (
    AnalysisArtifactRegistry,
    ArtifactDescriptor,
    RegisteredAnalysisArtifact,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


CONTROLLED_SUBAGENT_ARTIFACT_VERSION = "controlled-subagent-result.v1"

CONTROLLED_SUBAGENT_INSTRUCTIONS = """\
Perform one bounded independent investigation using only the supplied customer-safe artifacts.
Return a concise structured result. Every finding must cite exact sourceRefs from the supplied
allowedSourceRefs. Limitation refs must also come from allowedSourceRefs. Do not call tools, query
data, create business facts, infer missing evidence, change thread state, address the customer,
or include hidden reasoning, raw rows, SQL, credentials, provider payloads, or technical errors.
"""


class ControlledInvestigation(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    investigation_id: str = Field(alias="investigationId", min_length=1)
    task: str = Field(min_length=1)
    output_kind: Literal[
        "evidence_review",
        "hypothesis_review",
        "report_section",
        "quality_audit",
    ] = Field(alias="outputKind")
    source_artifact_refs: list[str] = Field(
        alias="sourceArtifactRefs",
        min_length=1,
        max_length=5,
    )

    @field_validator("investigation_id", "task")
    @classmethod
    def validate_exact_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("controlled_subagent_text_invalid")
        return value

    @field_validator("source_artifact_refs")
    @classmethod
    def validate_source_refs(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("controlled_subagent_source_ref_invalid")
        if len(values) != len(set(values)):
            raise ValueError("controlled_subagent_source_ref_duplicate")
        return values


class DelegateIndependentInvestigationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    investigations: list[ControlledInvestigation] = Field(
        min_length=1,
        max_length=3,
    )

    @model_validator(mode="after")
    def validate_investigation_ids(self) -> "DelegateIndependentInvestigationsInput":
        values = [item.investigation_id for item in self.investigations]
        if len(values) != len(set(values)):
            raise ValueError("controlled_subagent_investigation_id_duplicate")
        return self


class ControlledSubAgentFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    text: str = Field(min_length=1)
    source_refs: list[str] = Field(alias="sourceRefs", min_length=1)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("controlled_subagent_finding_text_invalid")
        return value

    @field_validator("source_refs")
    @classmethod
    def validate_source_refs(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("controlled_subagent_finding_source_invalid")
        if len(values) != len(set(values)):
            raise ValueError("controlled_subagent_finding_source_duplicate")
        return values


class ControlledSubAgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    findings: list[ControlledSubAgentFinding] = Field(min_length=1)
    limitation_refs: list[str] = Field(
        alias="limitationRefs",
        default_factory=list,
    )

    @field_validator("title", "summary")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("controlled_subagent_output_text_invalid")
        return value

    @field_validator("limitation_refs")
    @classmethod
    def validate_limitation_refs(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("controlled_subagent_limitation_ref_invalid")
        if len(values) != len(set(values)):
            raise ValueError("controlled_subagent_limitation_ref_duplicate")
        return values

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ControlledSubAgentAdapter(Protocol):
    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult: ...


class GeneratedArtifactWriter(Protocol):
    def register(
        self,
        *,
        thread_id: str,
        operation_id: str,
        descriptor: ArtifactDescriptor,
        detail: Mapping[str, Any],
    ) -> RegisteredAnalysisArtifact: ...


class InMemoryGeneratedArtifactWriter:
    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def register(
        self,
        *,
        thread_id: str,
        operation_id: str,
        descriptor: ArtifactDescriptor,
        detail: Mapping[str, Any],
    ) -> RegisteredAnalysisArtifact:
        del operation_id
        existing = self._registry.inspect(thread_id, descriptor.artifact_ref)
        if existing is not None:
            if existing.descriptor.digest != descriptor.digest:
                raise ValueError("generated_artifact_replay_conflict")
            return existing
        self._registry.add(thread_id, descriptor, detail)
        persisted = self._registry.inspect(thread_id, descriptor.artifact_ref)
        if persisted is None:
            raise RuntimeError("generated_artifact_persistence_failed")
        return persisted


class PostgresGeneratedArtifactWriter:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def register(
        self,
        *,
        thread_id: str,
        operation_id: str,
        descriptor: ArtifactDescriptor,
        detail: Mapping[str, Any],
    ) -> RegisteredAnalysisArtifact:
        normalized_detail = canonical_value(detail)
        if not isinstance(normalized_detail, dict):
            raise ValueError("generated_artifact_detail_invalid")
        try:
            row = self.connection.execute(
                """
                INSERT INTO waje_runtime.agent_generated_artifacts(
                  artifact_ref, thread_id, operation_id, artifact_type,
                  artifact_version, content_digest, source_refs,
                  visibility_policy_ref, customer_summary, detail
                ) VALUES (
                  %(artifact_ref)s, %(thread_id)s, %(operation_id)s,
                  %(artifact_type)s, %(artifact_version)s, %(content_digest)s,
                  %(source_refs)s::jsonb, %(visibility_policy_ref)s,
                  %(customer_summary)s, %(detail)s::jsonb
                )
                ON CONFLICT (thread_id, artifact_ref) DO NOTHING
                RETURNING artifact_ref, artifact_type, artifact_version,
                          content_digest, source_refs, visibility_policy_ref,
                          customer_summary, detail, created_at
                """,
                {
                    "artifact_ref": descriptor.artifact_ref,
                    "thread_id": thread_id,
                    "operation_id": operation_id,
                    "artifact_type": descriptor.artifact_type,
                    "artifact_version": descriptor.version,
                    "content_digest": descriptor.digest,
                    "source_refs": json.dumps(list(descriptor.source_refs)),
                    "visibility_policy_ref": descriptor.visibility_policy_ref,
                    "customer_summary": descriptor.customer_summary,
                    "detail": json.dumps(
                        normalized_detail,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    """
                    SELECT artifact_ref, artifact_type, artifact_version,
                           content_digest, source_refs, visibility_policy_ref,
                           customer_summary, detail, created_at
                    FROM waje_runtime.agent_generated_artifacts
                    WHERE artifact_ref = %(artifact_ref)s
                      AND thread_id = %(thread_id)s
                    """,
                    {
                        "artifact_ref": descriptor.artifact_ref,
                        "thread_id": thread_id,
                    },
                ).fetchone()
            if row is None:
                raise RuntimeError("generated_artifact_persistence_failed")
            persisted = _registered_from_row(row)
            if (
                persisted.descriptor.digest != descriptor.digest
                or persisted.descriptor.source_refs != descriptor.source_refs
                or persisted.detail != normalized_detail
            ):
                raise ValueError("generated_artifact_replay_conflict")
            self.connection.commit()
            return persisted
        except Exception:
            self.connection.rollback()
            raise


@dataclass(frozen=True)
class _InvestigationMaterials:
    investigation: ControlledInvestigation
    artifacts: tuple[RegisteredAnalysisArtifact, ...]
    allowed_source_refs: tuple[str, ...]


def controlled_subagent_tool(
    *,
    adapter: ControlledSubAgentAdapter,
    registry: AnalysisArtifactRegistry,
    writer: GeneratedArtifactWriter,
    thread_id: str,
    operation_id: str,
) -> WajeAgentTool:
    if not thread_id.strip() or not operation_id.strip():
        raise ValueError("controlled_subagent_identity_invalid")

    async def delegate(arguments: Mapping[str, Any]) -> AgentToolResult:
        request = DelegateIndependentInvestigationsInput.model_validate(arguments)
        prepared = tuple(
            _load_materials(registry, thread_id, investigation)
            for investigation in request.investigations
        )
        outputs = await asyncio.gather(
            *(
                _run_investigation(
                    adapter=adapter,
                    thread_id=thread_id,
                    operation_id=operation_id,
                    materials=materials,
                )
                for materials in prepared
            )
        )
        registered: list[RegisteredAnalysisArtifact] = []
        for materials, output in zip(prepared, outputs):
            _validate_output_closure(output, materials.allowed_source_refs)
            detail = {
                "schemaVersion": CONTROLLED_SUBAGENT_ARTIFACT_VERSION,
                "investigationId": materials.investigation.investigation_id,
                "outputKind": materials.investigation.output_kind,
                **output.to_contract(),
            }
            digest = canonical_digest(detail)
            descriptor = ArtifactDescriptor(
                artifact_ref=f"subagent-artifact:sha256:{digest}",
                artifact_type="controlled_subagent_result",
                version=CONTROLLED_SUBAGENT_ARTIFACT_VERSION,
                digest=digest,
                source_refs=tuple(materials.investigation.source_artifact_refs),
                visibility_policy_ref="visibility:customer-safe",
                customer_summary=output.summary,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            registered.append(
                writer.register(
                    thread_id=thread_id,
                    operation_id=operation_id,
                    descriptor=descriptor,
                    detail=detail,
                )
            )
        artifact_refs = [item.descriptor.artifact_ref for item in registered]
        limitation_refs = sorted(
            {
                ref
                for output in outputs
                for ref in output.limitation_refs
            }
        )
        material_refs = list(
            dict.fromkeys(
                [
                    *artifact_refs,
                    *(
                        ref
                        for materials in prepared
                        for ref in materials.investigation.source_artifact_refs
                    ),
                ]
            )
        )
        return AgentToolResult(
            status="limited" if limitation_refs else "succeeded",
            output={
                "results": [
                    {
                        "investigationId": materials.investigation.investigation_id,
                        "artifactRef": item.descriptor.artifact_ref,
                        "summary": item.descriptor.customer_summary,
                    }
                    for materials, item in zip(prepared, registered)
                ]
            },
            artifactRefs=artifact_refs,
            materialRefs=material_refs,
            limitationRefs=limitation_refs,
            retryability="never",
            customerSummary="独立调查已完成并保存为可追溯材料。",
            technicalDetailRef=None,
        )

    return WajeAgentTool(
        name="delegate_independent_investigations",
        description=(
            "Run one to three mutually independent, read-only investigations over "
            "explicit customer-safe artifacts. Use for competing hypotheses, independent "
            "report sections, evidence review, or quality audit. Each result is persisted "
            "as a structured artifact; sub-agents cannot modify thread state or call BI."
        ),
        input_model=DelegateIndependentInvestigationsInput,
        handler=delegate,
    )


def _load_materials(
    registry: AnalysisArtifactRegistry,
    thread_id: str,
    investigation: ControlledInvestigation,
) -> _InvestigationMaterials:
    artifacts: list[RegisteredAnalysisArtifact] = []
    allowed: set[str] = set()
    for artifact_ref in investigation.source_artifact_refs:
        artifact = registry.inspect(thread_id, artifact_ref)
        if artifact is None:
            raise ValueError("controlled_subagent_source_artifact_missing")
        artifacts.append(artifact)
        allowed.add(artifact.descriptor.artifact_ref)
        allowed.update(artifact.descriptor.source_refs)
        _collect_contract_refs(artifact.detail, allowed)
    return _InvestigationMaterials(
        investigation=investigation,
        artifacts=tuple(artifacts),
        allowed_source_refs=tuple(sorted(allowed)),
    )


async def _run_investigation(
    *,
    adapter: ControlledSubAgentAdapter,
    thread_id: str,
    operation_id: str,
    materials: _InvestigationMaterials,
) -> ControlledSubAgentOutput:
    payload = {
        "investigation": materials.investigation.model_dump(
            mode="json",
            by_alias=True,
        ),
        "allowedSourceRefs": list(materials.allowed_source_refs),
        "artifacts": [
            {
                "descriptor": item.descriptor.to_dict(),
                "detail": canonical_value(item.detail),
            }
            for item in materials.artifacts
        ],
    }
    input_digest = canonical_digest(payload)
    result = await adapter.run(
        WajeAgentRunRequest(
            run_id=f"subagent-run-{input_digest[:24]}",
            agent_name="WAJE Controlled Investigation Agent",
            instructions=CONTROLLED_SUBAGENT_INSTRUCTIONS,
            input_text=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            output_type=ControlledSubAgentOutput,
            max_turns=1,
            trace_metadata={
                "waje_thread_id": thread_id,
                "waje_parent_operation_id": operation_id,
                "waje_investigation_id": materials.investigation.investigation_id,
                "waje_subagent_input_digest": input_digest,
            },
        )
    )
    return ControlledSubAgentOutput.model_validate(result.final_output)


def _validate_output_closure(
    output: ControlledSubAgentOutput,
    allowed_source_refs: Sequence[str],
) -> None:
    allowed = set(allowed_source_refs)
    for finding in output.findings:
        if not set(finding.source_refs).issubset(allowed):
            raise ValueError("controlled_subagent_finding_source_unknown")
    if not set(output.limitation_refs).issubset(allowed):
        raise ValueError("controlled_subagent_limitation_source_unknown")


def _collect_contract_refs(value: Any, refs: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {
                "artifactRefs",
                "materialRefs",
                "limitationRefs",
                "sourceRefs",
            } and isinstance(child, list):
                refs.update(item for item in child if isinstance(item, str) and item)
            else:
                _collect_contract_refs(child, refs)
    elif isinstance(value, list):
        for child in value:
            _collect_contract_refs(child, refs)


def _registered_from_row(row: Any) -> RegisteredAnalysisArtifact:
    source_refs = _json_value(_field(row, "source_refs", 4))
    detail = _json_value(_field(row, "detail", 7))
    created_at = _field(row, "created_at", 8)
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()
    if not isinstance(source_refs, list) or not isinstance(detail, Mapping):
        raise ValueError("generated_artifact_payload_invalid")
    return RegisteredAnalysisArtifact(
        descriptor=ArtifactDescriptor(
            artifact_ref=str(_field(row, "artifact_ref", 0)),
            artifact_type=str(_field(row, "artifact_type", 1)),
            version=str(_field(row, "artifact_version", 2)),
            digest=str(_field(row, "content_digest", 3)),
            source_refs=tuple(str(item) for item in source_refs),
            visibility_policy_ref=str(_field(row, "visibility_policy_ref", 5)),
            customer_summary=str(_field(row, "customer_summary", 6)),
            created_at=str(created_at),
        ),
        detail=dict(detail),
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


__all__ = (
    "CONTROLLED_SUBAGENT_ARTIFACT_VERSION",
    "CONTROLLED_SUBAGENT_INSTRUCTIONS",
    "ControlledInvestigation",
    "ControlledSubAgentFinding",
    "ControlledSubAgentOutput",
    "DelegateIndependentInvestigationsInput",
    "GeneratedArtifactWriter",
    "InMemoryGeneratedArtifactWriter",
    "PostgresGeneratedArtifactWriter",
    "controlled_subagent_tool",
)
