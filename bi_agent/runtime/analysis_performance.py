from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Sequence


class AnalysisPerformanceContractError(ValueError):
    pass


_STAGE_ORDER = (
    "intent",
    "plan",
    "evidence",
    "coverage",
    "claim_authority",
    "narrative",
    "delivery",
)

_NODE_STAGES = {
    "understand_business_intent": "intent",
    "decide_question_boundary": "intent",
    "generate_clarification": "intent",
    "clarification_policy_gate": "intent",
    "compile_authoritative_plan": "plan",
    "repair_authoritative_plan": "plan",
    "execute_capability_dag": "evidence",
    "evaluate_claim_coverage": "coverage",
    "settle_claim_authority": "claim_authority",
    "compose_claim_aware_narrative": "narrative",
    "deliver_publication": "delivery",
}


@dataclass(frozen=True)
class AnalysisPerformancePolicy:
    schema_version: str
    enforcement: str
    breach_action: str
    depth_protection: str
    full_factor_p50_target_ms: int
    full_factor_p95_target_ms: int
    follow_up_target_ms: int
    response_ack_target_ms: int
    first_progress_target_ms: int
    stage_targets_ms: Mapping[str, int]

    @classmethod
    def from_contract(
        cls,
        value: Mapping[str, Any],
    ) -> "AnalysisPerformancePolicy":
        if not isinstance(value, Mapping):
            raise AnalysisPerformanceContractError("analysis_performance_policy_invalid")
        required = {
            "schema_version",
            "enforcement",
            "breach_action",
            "depth_protection",
            "full_factor_targets_ms",
            "follow_up_target_ms",
            "response_ack_target_ms",
            "first_progress_target_ms",
            "stage_targets_ms",
        }
        if set(value) != required:
            raise AnalysisPerformanceContractError(
                "analysis_performance_policy_invalid:shape"
            )
        if value.get("schema_version") != "analysis-performance-policy.v1":
            raise AnalysisPerformanceContractError(
                "analysis_performance_policy_invalid:schema_version"
            )
        if value.get("enforcement") != "audit_only":
            raise AnalysisPerformanceContractError(
                "analysis_performance_policy_invalid:enforcement"
            )
        if value.get("breach_action") != "record_and_continue":
            raise AnalysisPerformanceContractError(
                "analysis_performance_policy_invalid:breach_action"
            )
        if (
            value.get("depth_protection")
            != "preserve_required_coverage_and_verification"
        ):
            raise AnalysisPerformanceContractError(
                "analysis_performance_policy_invalid:depth_protection"
            )
        full_targets = value.get("full_factor_targets_ms")
        if not isinstance(full_targets, Mapping) or set(full_targets) != {
            "p50",
            "p95",
        }:
            raise AnalysisPerformanceContractError(
                "analysis_performance_policy_invalid:full_factor_targets"
            )
        p50 = _positive_int(full_targets.get("p50"), "full_factor_p50")
        p95 = _positive_int(full_targets.get("p95"), "full_factor_p95")
        if p95 < p50:
            raise AnalysisPerformanceContractError(
                "analysis_performance_policy_invalid:target_order"
            )
        stage_targets = value.get("stage_targets_ms")
        if not isinstance(stage_targets, Mapping) or set(stage_targets) != set(
            _STAGE_ORDER
        ):
            raise AnalysisPerformanceContractError(
                "analysis_performance_policy_invalid:stage_targets"
            )
        normalized_stages = {
            stage: _positive_int(stage_targets[stage], f"stage_target:{stage}")
            for stage in _STAGE_ORDER
        }
        return cls(
            schema_version="analysis-performance-policy.v1",
            enforcement="audit_only",
            breach_action="record_and_continue",
            depth_protection="preserve_required_coverage_and_verification",
            full_factor_p50_target_ms=p50,
            full_factor_p95_target_ms=p95,
            follow_up_target_ms=_positive_int(
                value.get("follow_up_target_ms"), "follow_up_target"
            ),
            response_ack_target_ms=_positive_int(
                value.get("response_ack_target_ms"), "response_ack_target"
            ),
            first_progress_target_ms=_positive_int(
                value.get("first_progress_target_ms"), "first_progress_target"
            ),
            stage_targets_ms=normalized_stages,
        )

    def contract_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "enforcement": self.enforcement,
            "breach_action": self.breach_action,
            "depth_protection": self.depth_protection,
            "full_factor_targets_ms": {
                "p50": self.full_factor_p50_target_ms,
                "p95": self.full_factor_p95_target_ms,
            },
            "follow_up_target_ms": self.follow_up_target_ms,
            "response_ack_target_ms": self.response_ack_target_ms,
            "first_progress_target_ms": self.first_progress_target_ms,
            "stage_targets_ms": dict(self.stage_targets_ms),
        }


@dataclass(frozen=True)
class AnalysisPerformanceStageObservation:
    stage: str
    duration_ms: float
    target_ms: int
    budget_status: str
    node_count: int
    nodes: tuple[str, ...]


@dataclass(frozen=True)
class CapabilitySubstageObservation:
    stage: str
    operation: str
    duration_ms: float
    input_bytes: int


@dataclass(frozen=True)
class AnalysisPerformanceProfile:
    schema_version: str
    profile_ref: str
    run_id: str
    enforcement: str
    breach_action: str
    depth_protection: str
    budget_status: str
    total_observed_duration_ms: float
    p50_target_ms: int
    p95_target_ms: int
    stage_observations: tuple[AnalysisPerformanceStageObservation, ...]
    bottlenecks: tuple[AnalysisPerformanceStageObservation, ...]
    capability_substages: tuple[CapabilitySubstageObservation, ...]
    unclassified_nodes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_analysis_performance_profile(
    *,
    run_id: str,
    checkpoint_events: Sequence[Mapping[str, Any]],
    policy: AnalysisPerformancePolicy,
    capability_substages: Sequence[Mapping[str, Any]] = (),
) -> AnalysisPerformanceProfile:
    if not isinstance(run_id, str) or not run_id.strip():
        raise AnalysisPerformanceContractError("run_id_invalid")
    if type(policy) is not AnalysisPerformancePolicy:
        raise AnalysisPerformanceContractError("analysis_performance_policy_invalid")
    if isinstance(checkpoint_events, (str, bytes)) or not isinstance(
        checkpoint_events, Sequence
    ):
        raise AnalysisPerformanceContractError("checkpoint_event_invalid")

    durations = {stage: 0.0 for stage in _STAGE_ORDER}
    nodes = {stage: [] for stage in _STAGE_ORDER}
    unclassified: list[str] = []
    for raw in checkpoint_events:
        if not isinstance(raw, Mapping):
            raise AnalysisPerformanceContractError("checkpoint_event_invalid")
        node = raw.get("node")
        duration = raw.get("duration_ms")
        if (
            not isinstance(node, str)
            or not node.strip()
            or not _nonnegative_number(duration)
        ):
            raise AnalysisPerformanceContractError("checkpoint_event_invalid")
        if raw.get("status") not in {None, "completed"}:
            continue
        stage = _NODE_STAGES.get(node)
        if stage is None:
            unclassified.append(node)
            continue
        durations[stage] += float(duration)
        nodes[stage].append(node)

    observations = tuple(
        AnalysisPerformanceStageObservation(
            stage=stage,
            duration_ms=_stable_number(durations[stage]),
            target_ms=policy.stage_targets_ms[stage],
            budget_status=(
                "breached"
                if durations[stage] > policy.stage_targets_ms[stage]
                else "target_met"
            ),
            node_count=len(nodes[stage]),
            nodes=tuple(nodes[stage]),
        )
        for stage in _STAGE_ORDER
        if nodes[stage]
    )
    normalized_substages = tuple(
        _capability_substage(item) for item in capability_substages
    )
    total = _stable_number(sum(item.duration_ms for item in observations))
    budget_status = (
        "breached"
        if total > policy.full_factor_p95_target_ms
        else "target_met"
    )
    profile_payload = {
        "schema_version": "analysis-performance-profile.v1",
        "run_id": run_id,
        "enforcement": policy.enforcement,
        "breach_action": policy.breach_action,
        "depth_protection": policy.depth_protection,
        "budget_status": budget_status,
        "total_observed_duration_ms": total,
        "p50_target_ms": policy.full_factor_p50_target_ms,
        "p95_target_ms": policy.full_factor_p95_target_ms,
        "stage_observations": [asdict(item) for item in observations],
        "bottlenecks": [
            asdict(item)
            for item in sorted(
                observations,
                key=lambda item: (-item.duration_ms, _STAGE_ORDER.index(item.stage)),
            )
        ],
        "capability_substages": [asdict(item) for item in normalized_substages],
        "unclassified_nodes": sorted(set(unclassified)),
    }
    profile_ref = "analysis-performance-profile:sha256:" + hashlib.sha256(
        json.dumps(
            profile_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return AnalysisPerformanceProfile(
        profile_ref=profile_ref,
        stage_observations=observations,
        bottlenecks=tuple(
            sorted(
                observations,
                key=lambda item: (-item.duration_ms, _STAGE_ORDER.index(item.stage)),
            )
        ),
        capability_substages=normalized_substages,
        unclassified_nodes=tuple(sorted(set(unclassified))),
        **{
            key: value
            for key, value in profile_payload.items()
            if key
            not in {
                "stage_observations",
                "bottlenecks",
                "capability_substages",
                "unclassified_nodes",
            }
        },
    )


def _capability_substage(value: Mapping[str, Any]) -> CapabilitySubstageObservation:
    if not isinstance(value, Mapping) or set(value) != {
        "stage",
        "operation",
        "duration_ms",
        "input_bytes",
    }:
        raise AnalysisPerformanceContractError("capability_substage_invalid")
    stage = value.get("stage")
    operation = value.get("operation")
    duration = value.get("duration_ms")
    input_bytes = value.get("input_bytes")
    if (
        not isinstance(stage, str)
        or not stage.strip()
        or not isinstance(operation, str)
        or not operation.strip()
        or not _nonnegative_number(duration)
        or type(input_bytes) is not int
        or input_bytes < 0
    ):
        raise AnalysisPerformanceContractError("capability_substage_invalid")
    return CapabilitySubstageObservation(
        stage=stage,
        operation=operation,
        duration_ms=_stable_number(float(duration)),
        input_bytes=input_bytes,
    )


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise AnalysisPerformanceContractError(
            f"analysis_performance_policy_invalid:{field}"
        )
    return value


def _nonnegative_number(value: Any) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _stable_number(value: float | int) -> float | int:
    normalized = float(value)
    return int(normalized) if normalized.is_integer() else round(normalized, 6)
