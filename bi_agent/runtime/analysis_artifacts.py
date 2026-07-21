from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bi_agent.runtime.agent_sdk_contracts import AgentToolResult, WajeAgentTool
from bi_agent.runtime.capability_authority import EvidenceLedgerEntry
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


@dataclass(frozen=True)
class ArtifactDescriptor:
    artifact_ref: str
    artifact_type: str
    version: str
    digest: str
    source_refs: tuple[str, ...]
    visibility_policy_ref: str
    customer_summary: str
    created_at: str

    def __post_init__(self) -> None:
        for value, code in (
            (self.artifact_ref, "artifact_ref_missing"),
            (self.artifact_type, "artifact_type_missing"),
            (self.version, "artifact_version_missing"),
            (self.digest, "artifact_digest_missing"),
            (self.visibility_policy_ref, "artifact_visibility_policy_missing"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(code)
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("artifact_source_refs_duplicate")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_refs"] = list(self.source_refs)
        return data


class ScoreSubject(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["dimension", "member", "claim"]
    dimension_ref: str | None = Field(alias="dimensionRef", default=None)
    member_ref: str | None = Field(alias="memberRef", default=None)
    claim_ref: str | None = Field(alias="claimRef", default=None)
    representative_member_ref: str | None = Field(
        alias="representativeMemberRef",
        default=None,
    )

    @model_validator(mode="after")
    def validate_subject_identity(self) -> "ScoreSubject":
        refs = (
            self.dimension_ref,
            self.member_ref,
            self.claim_ref,
            self.representative_member_ref,
        )
        if any(
            ref is not None and (not ref.strip() or ref != ref.strip()) for ref in refs
        ):
            raise ValueError("score_subject_ref_invalid")
        if self.type == "dimension" and self.dimension_ref is None:
            raise ValueError("score_subject_dimension_ref_missing")
        if self.type == "member" and (
            self.dimension_ref is None or self.member_ref is None
        ):
            raise ValueError("score_subject_member_ref_missing")
        if self.type == "claim" and self.claim_ref is None:
            raise ValueError("score_subject_claim_ref_missing")
        return self


class ScoreComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    component_id: str = Field(alias="componentId", min_length=1)
    status: Literal["measured", "not_applicable", "unavailable"]
    raw_value: float | None = Field(alias="rawValue")
    normalized_value: float | None = Field(alias="normalizedValue")
    weight: float | None
    contribution: float | None
    normalization: str = Field(min_length=1)
    material_refs: list[str] = Field(alias="materialRefs", default_factory=list)

    @field_validator("material_refs")
    @classmethod
    def validate_material_refs(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("score_component_material_ref_invalid")
        if len(values) != len(set(values)):
            raise ValueError("score_component_material_ref_duplicate")
        return values

    @model_validator(mode="after")
    def validate_status_values(self) -> "ScoreComponent":
        numeric = (
            self.raw_value,
            self.normalized_value,
            self.weight,
            self.contribution,
        )
        if self.status == "measured":
            if any(value is None for value in numeric):
                raise ValueError("score_component_measured_value_missing")
            expected = float(self.normalized_value or 0.0) * float(self.weight or 0.0)
            if abs(expected - float(self.contribution or 0.0)) > 1e-12:
                raise ValueError("score_component_contribution_invalid")
        elif any(value is not None for value in numeric):
            raise ValueError("score_component_unmeasured_value_present")
        return self


class ScoreExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    formula_id: str = Field(alias="formulaId", min_length=1)
    formula_version: str = Field(alias="formulaVersion", min_length=1)
    subject: ScoreSubject
    components: list[ScoreComponent] = Field(min_length=1)
    final_score: float | None = Field(alias="finalScore")
    ranking_scope: str = Field(alias="rankingScope", min_length=1)
    comparison_allowed: bool = Field(alias="comparisonAllowed")
    limitation_refs: list[str] = Field(alias="limitationRefs", default_factory=list)

    @model_validator(mode="after")
    def validate_score_closure(self) -> "ScoreExplanation":
        component_ids = [item.component_id for item in self.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("score_component_duplicate")
        if len(self.limitation_refs) != len(set(self.limitation_refs)):
            raise ValueError("score_limitation_ref_duplicate")
        measured = [item for item in self.components if item.status == "measured"]
        if not measured:
            if self.final_score is not None:
                raise ValueError("score_final_value_invalid")
            return self
        weight_sum = sum(float(item.weight or 0.0) for item in measured)
        contribution_sum = sum(float(item.contribution or 0.0) for item in measured)
        if abs(weight_sum - 1.0) > 1e-12:
            raise ValueError("score_component_weight_closure_invalid")
        if self.final_score is None or abs(self.final_score - contribution_sum) > 1e-12:
            raise ValueError("score_final_value_invalid")
        return self

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class InspectAnalysisArtifactInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    artifact_ref: str = Field(alias="artifactRef", min_length=1)


class ExplainClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    claim_ref: str = Field(alias="claimRef", min_length=1)


@dataclass(frozen=True)
class RegisteredAnalysisArtifact:
    descriptor: ArtifactDescriptor
    detail: Mapping[str, Any]


class AnalysisArtifactRegistry(Protocol):
    def list_artifacts(
        self,
        thread_id: str,
        *,
        limit: int,
    ) -> tuple[ArtifactDescriptor, ...]: ...

    def inspect(
        self,
        thread_id: str,
        artifact_ref: str,
    ) -> RegisteredAnalysisArtifact | None: ...

    def explain_claim(
        self,
        thread_id: str,
        claim_ref: str,
    ) -> RegisteredAnalysisArtifact | None: ...

    def list_task_artifacts(
        self,
        thread_id: str,
        task_ref: str,
        *,
        limit: int,
    ) -> tuple[RegisteredAnalysisArtifact, ...]: ...


class InMemoryAnalysisArtifactRegistry:
    def __init__(self) -> None:
        self._by_thread: dict[str, dict[str, RegisteredAnalysisArtifact]] = {}

    def add(
        self,
        thread_id: str,
        descriptor: ArtifactDescriptor,
        detail: Mapping[str, Any],
    ) -> None:
        self._by_thread.setdefault(thread_id, {})[descriptor.artifact_ref] = (
            RegisteredAnalysisArtifact(
                descriptor=descriptor,
                detail=_mapping(detail),
            )
        )

    def list_artifacts(
        self,
        thread_id: str,
        *,
        limit: int,
    ) -> tuple[ArtifactDescriptor, ...]:
        _validate_limit(limit)
        values = tuple(self._by_thread.get(thread_id, {}).values())
        return tuple(item.descriptor for item in values[-limit:])

    def inspect(
        self,
        thread_id: str,
        artifact_ref: str,
    ) -> RegisteredAnalysisArtifact | None:
        return deepcopy(self._by_thread.get(thread_id, {}).get(artifact_ref))

    def explain_claim(
        self,
        thread_id: str,
        claim_ref: str,
    ) -> RegisteredAnalysisArtifact | None:
        item = self.inspect(thread_id, claim_ref)
        if item is None or item.descriptor.artifact_type != "bi_claim":
            return None
        return item

    def list_task_artifacts(
        self,
        thread_id: str,
        task_ref: str,
        *,
        limit: int,
    ) -> tuple[RegisteredAnalysisArtifact, ...]:
        _validate_limit(limit)
        if not task_ref.strip():
            raise ValueError("artifact_task_ref_missing")
        values = tuple(self._by_thread.get(thread_id, {}).values())
        reachable_refs = {
            item.descriptor.artifact_ref
            for item in values
            if task_ref in item.descriptor.source_refs
        }
        selected_refs: set[str] = set()
        while True:
            newly_selected = {
                item.descriptor.artifact_ref
                for item in values
                if item.descriptor.artifact_ref in reachable_refs
                and item.descriptor.artifact_ref not in selected_refs
            }
            if not newly_selected:
                break
            selected_refs.update(newly_selected)
            for item in values:
                if item.descriptor.artifact_ref in newly_selected:
                    reachable_refs.update(item.descriptor.source_refs)
        return tuple(
            deepcopy(item)
            for item in values
            if item.descriptor.artifact_ref in selected_refs
        )[:limit]


class PostgresAnalysisArtifactRegistry:
    """Reads published customer-safe materials and typed score explanations."""

    def __init__(self, connection: Any, *, publication_scan_limit: int = 50) -> None:
        _validate_limit(publication_scan_limit)
        self.connection = connection
        self._publication_scan_limit = publication_scan_limit

    def list_artifacts(
        self,
        thread_id: str,
        *,
        limit: int,
    ) -> tuple[ArtifactDescriptor, ...]:
        _validate_limit(limit)
        registered = self._load_registered(thread_id)
        return tuple(item.descriptor for item in registered[:limit])

    def inspect(
        self,
        thread_id: str,
        artifact_ref: str,
    ) -> RegisteredAnalysisArtifact | None:
        if not artifact_ref.strip():
            raise ValueError("artifact_ref_missing")
        return next(
            (
                item
                for item in self._load_registered(thread_id)
                if item.descriptor.artifact_ref == artifact_ref
            ),
            None,
        )

    def explain_claim(
        self,
        thread_id: str,
        claim_ref: str,
    ) -> RegisteredAnalysisArtifact | None:
        item = self.inspect(thread_id, claim_ref)
        if item is None or item.descriptor.artifact_type != "bi_claim":
            return None
        return item

    def list_task_artifacts(
        self,
        thread_id: str,
        task_ref: str,
        *,
        limit: int,
    ) -> tuple[RegisteredAnalysisArtifact, ...]:
        _validate_limit(limit)
        if not task_ref.strip():
            raise ValueError("artifact_task_ref_missing")
        return self._load_registered(thread_id, task_ref=task_ref)[:limit]

    def _load_registered(
        self,
        thread_id: str,
        *,
        task_ref: str | None = None,
    ) -> tuple[RegisteredAnalysisArtifact, ...]:
        generated = self._load_generated(thread_id) if task_ref is None else ()
        publication_rows = self.connection.execute(
            """
            SELECT customer.customer_payload_ref, customer.publication_ref,
                   customer.content_digest, customer.projection_id,
                   customer.field_visibility_policy_ref,
                   customer.customer_payload, customer.run_attempt_id,
                   customer.created_at, material.projection_ref,
                   material.content_digest, material.payload
            FROM waje_runtime.publication_customer_payloads customer
            JOIN waje_runtime.analysis_runs run
              ON run.run_id = customer.run_attempt_id
            JOIN waje_runtime.publication_projections publication_projection
              ON publication_projection.projection_id = customer.projection_id
             AND publication_projection.run_attempt_id = customer.run_attempt_id
             AND publication_projection.owner_ref = customer.owner_ref
            JOIN waje_runtime.narrative_material_projections material
              ON material.projection_ref = publication_projection.material_projection_ref
             AND material.run_attempt_id = customer.run_attempt_id
             AND material.owner_ref = customer.owner_ref
            WHERE run.thread_id = %(thread_id)s
              AND (%(task_ref)s IS NULL OR run.run_id = %(task_ref)s)
            ORDER BY customer.created_at DESC, customer.customer_payload_ref DESC
            LIMIT %(limit)s
            """,
            {
                "thread_id": thread_id,
                "task_ref": task_ref,
                "limit": self._publication_scan_limit,
            },
        ).fetchall()
        evidence_entry_refs = _published_evidence_entry_refs(publication_rows)
        evidence_rows = (
            self.connection.execute(
                """
                SELECT evidence.entry_ref, evidence.content_digest, evidence.payload,
                       evidence.run_attempt_id, evidence.created_at
                FROM waje_runtime.capability_evidence_ledger_entries evidence
                JOIN waje_runtime.analysis_runs run
                  ON run.run_id = evidence.run_attempt_id
                WHERE run.thread_id = %(thread_id)s
                  AND evidence.entry_ref = ANY(%(entry_refs)s)
                ORDER BY evidence.created_at DESC, evidence.entry_ref DESC
                """,
                {"thread_id": thread_id, "entry_refs": evidence_entry_refs},
            ).fetchall()
            if evidence_entry_refs
            else ()
        )
        return (*generated, *_registered_artifacts(publication_rows, evidence_rows))

    def _load_generated(
        self,
        thread_id: str,
    ) -> tuple[RegisteredAnalysisArtifact, ...]:
        rows = self.connection.execute(
            """
            SELECT artifact_ref, artifact_type, artifact_version,
                   content_digest, source_refs, visibility_policy_ref,
                   customer_summary, detail, created_at
            FROM waje_runtime.agent_generated_artifacts
            WHERE thread_id = %(thread_id)s
            ORDER BY created_at DESC, artifact_ref
            LIMIT %(limit)s
            """,
            {
                "thread_id": thread_id,
                "limit": self._publication_scan_limit,
            },
        ).fetchall()
        return tuple(_generated_artifact_from_row(row) for row in rows)


def analysis_artifact_tools(
    *,
    registry: AnalysisArtifactRegistry,
    thread_id: str,
) -> tuple[WajeAgentTool, WajeAgentTool]:
    if not thread_id.strip():
        raise ValueError("artifact_tool_thread_id_missing")

    def inspect(arguments: Mapping[str, Any]) -> AgentToolResult:
        artifact_ref = str(arguments.get("artifact_ref") or "")
        item = registry.inspect(thread_id, artifact_ref)
        return _tool_result(item, requested_ref=artifact_ref, kind="artifact")

    def explain(arguments: Mapping[str, Any]) -> AgentToolResult:
        claim_ref = str(arguments.get("claim_ref") or "")
        item = registry.explain_claim(thread_id, claim_ref)
        return _tool_result(item, requested_ref=claim_ref, kind="claim")

    return (
        WajeAgentTool(
            name="inspect_analysis_artifact",
            description=(
                "Read one persisted customer-safe publication, claim, evidence, "
                "limitation, or score explanation without starting a new BI run."
            ),
            input_model=InspectAnalysisArtifactInput,
            handler=inspect,
        ),
        WajeAgentTool(
            name="explain_claim",
            description=(
                "Read the persisted facts, evidence strength, formula, score "
                "components, and limitations for one published claim."
            ),
            input_model=ExplainClaimInput,
            handler=explain,
        ),
    )


def _tool_result(
    item: RegisteredAnalysisArtifact | None,
    *,
    requested_ref: str,
    kind: str,
) -> AgentToolResult:
    if item is None:
        return AgentToolResult(
            status="failed",
            output=None,
            artifactRefs=[],
            materialRefs=[],
            limitationRefs=[],
            retryability="replan_required",
            customerSummary=(
                "当前线程中没有找到可用于解释的已发布材料。"
                if kind == "artifact"
                else "当前线程中没有找到对应的已发布结论。"
            ),
            technicalDetailRef=None,
        )
    detail = _mapping(item.detail)
    limitation_refs = _string_values(detail.get("limitationRefs"))
    material_refs = tuple(
        dict.fromkeys(
            (
                item.descriptor.artifact_ref,
                *item.descriptor.source_refs,
                *_string_values(detail.get("materialRefs")),
            )
        )
    )
    return AgentToolResult(
        status="limited" if limitation_refs else "succeeded",
        output=detail,
        artifactRefs=[item.descriptor.artifact_ref],
        materialRefs=list(material_refs),
        limitationRefs=list(limitation_refs),
        retryability="never",
        customerSummary=item.descriptor.customer_summary or requested_ref,
        technicalDetailRef=None,
    )


def _registered_artifacts(
    publication_rows: Sequence[Any],
    evidence_rows: Sequence[Any],
) -> tuple[RegisteredAnalysisArtifact, ...]:
    policy_by_run = {
        str(_field(row, "run_attempt_id", 6)): str(
            _field(row, "field_visibility_policy_ref", 4)
        )
        for row in publication_rows
    }
    for run_id in policy_by_run:
        policies = {
            str(_field(row, "field_visibility_policy_ref", 4))
            for row in publication_rows
            if str(_field(row, "run_attempt_id", 6)) == run_id
        }
        if len(policies) != 1:
            raise ValueError("analysis_artifact_visibility_policy_conflict")
    score_by_entry: dict[str, list[RegisteredAnalysisArtifact]] = {}
    for row in evidence_rows:
        run_id = str(_field(row, "run_attempt_id", 3))
        policy_ref = policy_by_run.get(run_id)
        if policy_ref is None:
            continue
        entry = EvidenceLedgerEntry.from_dict(
            _mapping(_json_value(_field(row, "payload", 2)))
        )
        entry_ref = str(_field(row, "entry_ref", 0))
        if (
            entry.entry_ref != entry_ref
            or entry.run_attempt_id != run_id
            or entry.content_digest != str(_field(row, "content_digest", 1))
        ):
            raise ValueError("analysis_artifact_evidence_integrity_invalid")
        for score in _score_explanations(entry.to_dict()):
            contract = score.to_contract()
            digest = canonical_digest(contract)
            artifact_ref = "score-explanation:sha256:" + digest
            registered = RegisteredAnalysisArtifact(
                descriptor=ArtifactDescriptor(
                    artifact_ref=artifact_ref,
                    artifact_type="score_explanation",
                    version=f"{score.formula_id}@{score.formula_version}",
                    digest=digest,
                    source_refs=(run_id, entry_ref),
                    visibility_policy_ref=policy_ref,
                    customer_summary=_score_summary(score),
                    created_at=_isoformat(_field(row, "created_at", 4)),
                ),
                detail={
                    "artifactType": "score_explanation",
                    "artifactRef": artifact_ref,
                    "scoreExplanation": contract,
                    "materialRefs": [entry_ref],
                    "limitationRefs": list(score.limitation_refs),
                },
            )
            score_by_entry.setdefault(entry_ref, []).append(registered)

    registered_by_ref: dict[str, RegisteredAnalysisArtifact] = {}
    for row in publication_rows:
        for item in _publication_artifacts(row, score_by_entry=score_by_entry):
            registered_by_ref.setdefault(item.descriptor.artifact_ref, item)
    return tuple(registered_by_ref.values())


def _generated_artifact_from_row(row: Any) -> RegisteredAnalysisArtifact:
    source_refs = _json_value(_field(row, "source_refs", 4))
    detail = _json_value(_field(row, "detail", 7))
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
            created_at=_isoformat(_field(row, "created_at", 8)),
        ),
        detail=dict(detail),
    )


def _published_evidence_entry_refs(publication_rows: Sequence[Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            entry_ref
            for row in publication_rows
            for material in _mapping_values(
                _mapping(_json_value(_field(row, "payload", 10))).get(
                    "evidence_materials"
                )
            )
            if (entry_ref := str(material.get("evidence_entry_ref") or ""))
        )
    )


def _publication_artifacts(
    row: Any,
    *,
    score_by_entry: Mapping[str, Sequence[RegisteredAnalysisArtifact]],
) -> tuple[RegisteredAnalysisArtifact, ...]:
    customer_payload = _mapping(_json_value(_field(row, "customer_payload", 5)))
    material_payload = _mapping(_json_value(_field(row, "payload", 10)))
    customer_ref = str(_field(row, "customer_payload_ref", 0))
    publication_ref = str(_field(row, "publication_ref", 1))
    customer_payload_digest = str(_field(row, "content_digest", 2))
    projection_id = str(_field(row, "projection_id", 3))
    visibility_policy_ref = str(_field(row, "field_visibility_policy_ref", 4))
    run_id = str(_field(row, "run_attempt_id", 6))
    created_at = _isoformat(_field(row, "created_at", 7))
    material_projection_ref = str(_field(row, "projection_ref", 8))
    material_projection_digest = str(_field(row, "content_digest", 9))
    blocks = _mapping_values(customer_payload.get("blocks"))
    claims = _mapping_values(material_payload.get("claims"))
    evidence = _mapping_values(material_payload.get("evidence_materials"))
    limitations = _mapping_values(material_payload.get("limitations"))
    limitation_ref_by_handle = {
        str(item.get("limitation_handle") or ""): str(item.get("limitation_ref") or "")
        for item in limitations
    }
    facets = {
        str(item.get("boundary_facet_ref") or ""): item
        for item in _mapping_values(material_payload.get("boundary_facets"))
    }
    evidence_by_handle = {
        str(item.get("material_handle") or ""): item for item in evidence
    }

    nested_refs = tuple(
        dict.fromkeys(
            (
                *(str(item.get("claim_ref") or "") for item in claims),
                *(str(item.get("evidence_material_ref") or "") for item in evidence),
                *(str(item.get("limitation_ref") or "") for item in limitations),
            )
        )
    )
    publication_sources = tuple(
        value
        for value in dict.fromkeys(
            (
                run_id,
                publication_ref,
                projection_id,
                material_projection_ref,
                *nested_refs,
            )
        )
        if value
    )
    summary = _publication_summary(blocks)
    publication = RegisteredAnalysisArtifact(
        descriptor=ArtifactDescriptor(
            artifact_ref=customer_ref,
            artifact_type="bi_publication",
            version=publication_ref,
            digest=customer_payload_digest,
            source_refs=publication_sources,
            visibility_policy_ref=visibility_policy_ref,
            customer_summary=summary,
            created_at=created_at,
        ),
        detail={
            "artifactType": "bi_publication",
            "artifactRef": customer_ref,
            "publication": customer_payload,
            "materialRefs": list(publication_sources),
            "limitationRefs": _string_values(customer_payload.get("limitation_refs")),
        },
    )
    nested: list[RegisteredAnalysisArtifact] = [publication]

    for claim in claims:
        claim_ref = str(claim.get("claim_ref") or "")
        if not claim_ref:
            continue
        claim_evidence = tuple(
            evidence_by_handle[handle]
            for handle in _string_values(claim.get("material_handles"))
            if handle in evidence_by_handle
        )
        claim_scores = tuple(
            score
            for evidence_item in claim_evidence
            for score in score_by_entry.get(
                str(evidence_item.get("evidence_entry_ref") or ""), ()
            )
        )
        limitation_refs = tuple(
            limitation_ref_by_handle[handle]
            for handle in _string_values(claim.get("limitation_handles"))
            if limitation_ref_by_handle.get(handle)
        )
        source_refs = tuple(
            value
            for value in dict.fromkeys(
                (
                    customer_ref,
                    publication_ref,
                    material_projection_ref,
                    *(
                        str(item.get("evidence_material_ref") or "")
                        for item in claim_evidence
                    ),
                    *(item.descriptor.artifact_ref for item in claim_scores),
                )
            )
            if value
        )
        nested.append(
            RegisteredAnalysisArtifact(
                descriptor=ArtifactDescriptor(
                    artifact_ref=claim_ref,
                    artifact_type="bi_claim",
                    version=str(claim.get("projected_claim_ref") or claim_ref),
                    digest=str(claim.get("content_digest") or canonical_digest(claim)),
                    source_refs=source_refs,
                    visibility_policy_ref=visibility_policy_ref,
                    customer_summary=_claim_summary(blocks, claim_ref),
                    created_at=created_at,
                ),
                detail={
                    "artifactType": "bi_claim",
                    "artifactRef": claim_ref,
                    "claim": claim,
                    "evidence": list(claim_evidence),
                    "scoreExplanations": [
                        item.detail["scoreExplanation"] for item in claim_scores
                    ],
                    "materialRefs": list(source_refs),
                    "limitationRefs": list(limitation_refs),
                },
            )
        )

    for material in evidence:
        artifact_ref = str(material.get("evidence_material_ref") or "")
        if not artifact_ref:
            continue
        entry_ref = str(material.get("evidence_entry_ref") or "")
        scores = tuple(score_by_entry.get(entry_ref, ()))
        source_refs = tuple(
            value
            for value in dict.fromkeys(
                (
                    customer_ref,
                    material_projection_ref,
                    entry_ref,
                    *(item.descriptor.artifact_ref for item in scores),
                )
            )
            if value
        )
        nested.append(
            RegisteredAnalysisArtifact(
                descriptor=ArtifactDescriptor(
                    artifact_ref=artifact_ref,
                    artifact_type="bi_evidence",
                    version=str(material.get("evidence_entry_ref") or artifact_ref),
                    digest=str(
                        material.get("content_digest") or canonical_digest(material)
                    ),
                    source_refs=source_refs,
                    visibility_policy_ref=visibility_policy_ref,
                    customer_summary=_evidence_summary(material),
                    created_at=created_at,
                ),
                detail={
                    "artifactType": "bi_evidence",
                    "artifactRef": artifact_ref,
                    "evidence": material,
                    "scoreExplanations": [
                        item.detail["scoreExplanation"] for item in scores
                    ],
                    "materialRefs": list(source_refs),
                    "limitationRefs": [],
                },
            )
        )
        nested.extend(scores)

    for limitation in limitations:
        artifact_ref = str(limitation.get("limitation_ref") or "")
        if not artifact_ref:
            continue
        contexts = [
            facets[facet_ref]
            for facet_ref in _string_values(limitation.get("boundary_facet_refs"))
            if facet_ref in facets
        ]
        source_refs = (customer_ref, material_projection_ref)
        nested.append(
            RegisteredAnalysisArtifact(
                descriptor=ArtifactDescriptor(
                    artifact_ref=artifact_ref,
                    artifact_type="bi_limitation",
                    version=str(
                        limitation.get("projected_limitation_ref") or artifact_ref
                    ),
                    digest=str(
                        limitation.get("content_digest") or canonical_digest(limitation)
                    ),
                    source_refs=source_refs,
                    visibility_policy_ref=visibility_policy_ref,
                    customer_summary=_limitation_summary(blocks, artifact_ref),
                    created_at=created_at,
                ),
                detail={
                    "artifactType": "bi_limitation",
                    "artifactRef": artifact_ref,
                    "limitation": limitation,
                    "boundaryFacets": contexts,
                    "materialRefs": list(source_refs),
                    "limitationRefs": [artifact_ref],
                },
            )
        )
    if material_projection_digest != str(material_payload.get("content_digest") or ""):
        raise ValueError("analysis_artifact_material_projection_digest_mismatch")
    return tuple(nested)


def _score_explanations(payload: Any) -> tuple[ScoreExplanation, ...]:
    if not isinstance(payload, Mapping):
        return ()
    observations = payload.get("observation_facts")
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        return ()
    scores: dict[str, ScoreExplanation] = {}
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        raw = observation.get("score_explanation")
        if not isinstance(raw, Mapping):
            continue
        score = ScoreExplanation.model_validate(raw)
        scores[canonical_digest(score.to_contract())] = score
    return tuple(scores.values())


def _score_summary(score: ScoreExplanation) -> str:
    subject = score.subject.dimension_ref or score.subject.member_ref or "当前对象"
    if score.final_score is None:
        return f"{subject} 的评分材料当前不可用。"
    return f"{subject} 的诊断优先级得分为 {score.final_score:.4g}。"


def _publication_summary(blocks: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(
        str(block.get("text")) for block in blocks if isinstance(block.get("text"), str)
    )


def _claim_summary(blocks: Sequence[Mapping[str, Any]], claim_ref: str) -> str:
    texts = [
        str(block.get("text"))
        for block in blocks
        if claim_ref in _string_values(block.get("claim_refs"))
        and isinstance(block.get("text"), str)
    ]
    return "\n\n".join(texts) or "已发布结论及其证据材料。"


def _limitation_summary(
    blocks: Sequence[Mapping[str, Any]],
    limitation_ref: str,
) -> str:
    texts = [
        str(block.get("text"))
        for block in blocks
        if limitation_ref in _string_values(block.get("limitation_refs"))
        and isinstance(block.get("text"), str)
    ]
    return "\n\n".join(texts) or "已发布材料中的适用边界。"


def _evidence_summary(material: Mapping[str, Any]) -> str:
    kind = str(material.get("evidence_kind") or "")
    strength = str(material.get("evidence_strength") or "")
    scope = str(material.get("scope") or "")
    return "；".join(value for value in (kind, strength, scope) if value)


def _mapping(value: Any) -> dict[str, Any]:
    normalized = canonical_value(value)
    if not isinstance(normalized, dict):
        raise ValueError("analysis_artifact_mapping_invalid")
    return normalized


def _mapping_values(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(_mapping(item) for item in value if isinstance(item, Mapping))


def _string_values(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _field(row: Any, key: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _isoformat(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("analysis_artifact_limit_invalid")


__all__ = (
    "AgentToolResult",
    "AnalysisArtifactRegistry",
    "ArtifactDescriptor",
    "ExplainClaimInput",
    "InMemoryAnalysisArtifactRegistry",
    "InspectAnalysisArtifactInput",
    "PostgresAnalysisArtifactRegistry",
    "RegisteredAnalysisArtifact",
    "ScoreExplanation",
    "analysis_artifact_tools",
)
