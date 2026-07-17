from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class BudgetState:
    mode: str
    used_capability_calls: int
    soft_limit: int
    hard_limit: int

    def to_llm_summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "used_capability_calls": self.used_capability_calls,
            "soft_limit": self.soft_limit,
            "hard_limit": self.hard_limit,
            "budget_instruction": "do_not_trade_answer_quality_for_cost_during_research",
        }


@dataclass(frozen=True)
class CapabilityCard:
    capability_id: str
    business_name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    supported_question_families: tuple[str, ...]
    supported_grains: tuple[str, ...]
    allowed_claim_types: tuple[str, ...]
    default_evidence_type: str
    cost_tier: str
    runtime_tier: str
    preconditions: tuple[str, ...]
    failure_modes: tuple[str, ...]

    def to_llm_summary(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "business_name": self.business_name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "supported_question_families": list(self.supported_question_families),
            "supported_grains": list(self.supported_grains),
            "allowed_claim_types": list(self.allowed_claim_types),
            "default_evidence_type": self.default_evidence_type,
            "cost_tier": self.cost_tier,
            "runtime_tier": self.runtime_tier,
            "preconditions": list(self.preconditions),
            "failure_modes": list(self.failure_modes),
        }


@dataclass(frozen=True)
class CapabilityRequest:
    run_id: str
    accepted_graph_id: str
    graph_version: int
    capability_id: str
    question_family: str
    target_claim: str
    claim_type: str
    metric: str
    scope: str
    time_window: str
    baseline: Mapping[str, Any]
    target: Mapping[str, Any]
    grain: str
    filters: Mapping[str, Any]
    dimensions: tuple[str, ...]
    contract_versions: Mapping[str, str]
    budget_state: BudgetState
    llm_business_reason: str
    params: Mapping[str, Any]
    bound_input: Any = None
    evidence_resolver: Any = None
    rows_loader: Any = None
    runtime_registry: Any = None
    release_resolver: Any = None


@dataclass(frozen=True)
class CapabilityEvidenceEnvelope:
    evidence_ref: str
    capability_id: str
    question_family: str
    target_claim: str
    claim_type: str
    metric: str
    scope: str
    grain: str
    baseline_label: str
    target_label: str
    time_window: str
    numeric_facts: Mapping[str, Any]
    typed_payload: Mapping[str, Any]
    result_refs: tuple[str, ...]
    sql_hashes: tuple[str, ...]
    evidence_type: str
    strength: str
    wording_limit: str
    limitations: tuple[str, ...]
    disabled_degraded_blocked_path_refs: tuple[str, ...]
    verifier_handoff: Mapping[str, Any]
    admin_audit_ref: str
    analysis_contract_ref: str = ""
    capability_contract_ref: str = ""
    query_contract_refs: tuple[str, ...] = ()
    query_execution_record_refs: tuple[str, ...] = ()
    query_execution_record_digests: tuple[str, ...] = ()
    rows_metadata_record_refs: tuple[str, ...] = ()
    rows_metadata_record_digests: tuple[str, ...] = ()
    completeness_report_refs: tuple[str, ...] = ()
    completeness_record_refs: tuple[str, ...] = ()
    completeness_record_digests: tuple[str, ...] = ()
    source_snapshot_refs: tuple[str, ...] = ()
    supported_evidence_types: tuple[str, ...] = ()
    supported_claim_types: tuple[str, ...] = ()
    maximum_claim_strength: str = ""
    maximum_claim_strength_rank: int = -1
    claim_strength_taxonomy_version: str = ""
    input_status: str = ""
    input_completeness_statuses: tuple[str, ...] = ()
    binding_manifest_ref: str = ""
    binding_manifest_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
