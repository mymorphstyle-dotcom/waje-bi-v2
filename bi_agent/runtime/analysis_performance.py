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
class ProviderCallObservation:
    stage: str
    operation: str
    provider: str
    model: str
    model_tier: str
    thinking: str | None
    status: str
    attempt_count: int
    retry_count: int
    total_duration_ms: float
    successful_attempt_duration_ms: float
    retry_duration_ms: float
    input_bytes_per_attempt: int
    total_input_bytes: int
    total_output_bytes: int
    prompt_tokens: int
    completion_tokens: int
    failure_codes: tuple[str, ...]
    reasoning_content_present: bool


@dataclass(frozen=True)
class ProviderCallTotals:
    call_count: int
    attempt_count: int
    retry_count: int
    total_duration_ms: float
    retry_duration_ms: float
    total_input_bytes: int
    total_output_bytes: int
    prompt_tokens: int
    completion_tokens: int


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
    provider_calls: tuple[ProviderCallObservation, ...]
    provider_totals: ProviderCallTotals
    unclassified_nodes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_analysis_performance_profile(
    *,
    run_id: str,
    checkpoint_events: Sequence[Mapping[str, Any]],
    policy: AnalysisPerformancePolicy,
    capability_substages: Sequence[Mapping[str, Any]] = (),
    provider_call_audits: Sequence[Mapping[str, Any]] = (),
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
    provider_calls = tuple(
        _provider_call_observation(item) for item in provider_call_audits
    )
    provider_totals = _provider_call_totals(provider_calls)
    total = _stable_number(sum(item.duration_ms for item in observations))
    budget_status = (
        "breached"
        if total > policy.full_factor_p95_target_ms
        else "target_met"
    )
    profile_payload = {
        "schema_version": "analysis-performance-profile.v2",
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
        "provider_calls": [asdict(item) for item in provider_calls],
        "provider_totals": asdict(provider_totals),
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
        provider_calls=provider_calls,
        provider_totals=provider_totals,
        unclassified_nodes=tuple(sorted(set(unclassified))),
        **{
            key: value
            for key, value in profile_payload.items()
            if key
            not in {
                "stage_observations",
                "bottlenecks",
                "capability_substages",
                "provider_calls",
                "provider_totals",
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


def _provider_call_observation(
    value: Mapping[str, Any],
) -> ProviderCallObservation:
    if not isinstance(value, Mapping) or set(value) != {"stage", "audit"}:
        raise AnalysisPerformanceContractError("provider_call_audit_invalid")
    stage = value.get("stage")
    audit = value.get("audit")
    if (
        not isinstance(stage, str)
        or not stage.strip()
        or not isinstance(audit, Mapping)
    ):
        raise AnalysisPerformanceContractError("provider_call_audit_invalid")
    operation = audit.get("task")
    provider = audit.get("provider")
    model = audit.get("model")
    model_tier = audit.get("model_tier")
    attempt_count = audit.get("attempt_count")
    duration = audit.get("duration_ms")
    input_bytes = audit.get("input_bytes")
    if (
        not isinstance(operation, str)
        or not operation.strip()
        or not isinstance(provider, str)
        or not provider.strip()
        or not isinstance(model, str)
        or not model.strip()
        or not isinstance(model_tier, str)
        or not model_tier.strip()
        or type(attempt_count) is not int
        or attempt_count < 1
        or not _nonnegative_number(duration)
        or type(input_bytes) is not int
        or input_bytes < 0
    ):
        raise AnalysisPerformanceContractError("provider_call_audit_invalid")
    thinking = audit.get("thinking")
    if thinking not in {None, "enabled", "disabled"}:
        raise AnalysisPerformanceContractError("provider_call_audit_invalid")
    failures = audit.get("attempt_failures") or ()
    if isinstance(failures, (str, bytes)) or not isinstance(failures, Sequence):
        raise AnalysisPerformanceContractError("provider_call_audit_invalid")
    normalized_failures: list[Mapping[str, Any]] = []
    for failure in failures:
        if not isinstance(failure, Mapping):
            raise AnalysisPerformanceContractError("provider_call_audit_invalid")
        failure_duration = failure.get("duration_ms", 0)
        if not _nonnegative_number(failure_duration):
            raise AnalysisPerformanceContractError("provider_call_audit_invalid")
        normalized_failures.append(failure)
    if len(normalized_failures) != max(0, attempt_count - 1):
        raise AnalysisPerformanceContractError("provider_call_audit_invalid")
    status = audit.get("status")
    if status is None:
        status = "succeeded_after_retry" if normalized_failures else "succeeded"
    if status not in {"succeeded", "succeeded_after_retry", "failed"}:
        raise AnalysisPerformanceContractError("provider_call_audit_invalid")
    output_bytes = audit.get("output_bytes", 0)
    if type(output_bytes) is not int or output_bytes < 0:
        raise AnalysisPerformanceContractError("provider_call_audit_invalid")
    final_usage = _provider_usage(audit.get("usage"))
    retry_duration = sum(
        float(item.get("duration_ms", 0)) for item in normalized_failures
    )
    failure_usage = tuple(
        _provider_usage(item.get("usage")) for item in normalized_failures
    )
    failure_output_bytes = sum(
        _nonnegative_integer(
            item.get("raw_response_bytes", item.get("structured_output_bytes", 0)),
            "provider_call_audit_invalid",
        )
        for item in normalized_failures
    )
    failure_codes = tuple(
        str(item["failure_code"])
        for item in normalized_failures
        if isinstance(item.get("failure_code"), str)
        and str(item["failure_code"]).strip()
    )
    return ProviderCallObservation(
        stage=stage.strip(),
        operation=operation.strip(),
        provider=provider.strip(),
        model=model.strip(),
        model_tier=model_tier.strip(),
        thinking=thinking,
        status=status,
        attempt_count=attempt_count,
        retry_count=len(normalized_failures),
        total_duration_ms=_stable_number(float(duration) + retry_duration),
        successful_attempt_duration_ms=(
            0 if status == "failed" else _stable_number(float(duration))
        ),
        retry_duration_ms=_stable_number(retry_duration),
        input_bytes_per_attempt=input_bytes,
        total_input_bytes=input_bytes * attempt_count,
        total_output_bytes=output_bytes + failure_output_bytes,
        prompt_tokens=final_usage["prompt_tokens"]
        + sum(item["prompt_tokens"] for item in failure_usage),
        completion_tokens=final_usage["completion_tokens"]
        + sum(item["completion_tokens"] for item in failure_usage),
        failure_codes=failure_codes,
        reasoning_content_present=bool(
            audit.get("reasoning_content_present", False)
            or any(
                item.get("reasoning_content_present", False)
                for item in normalized_failures
            )
        ),
    )


def _provider_usage(value: Any) -> dict[str, int]:
    if value is None:
        return {"prompt_tokens": 0, "completion_tokens": 0}
    if not isinstance(value, Mapping):
        raise AnalysisPerformanceContractError("provider_call_audit_invalid")
    return {
        "prompt_tokens": _nonnegative_integer(
            value.get("prompt_tokens", 0),
            "provider_call_audit_invalid",
        ),
        "completion_tokens": _nonnegative_integer(
            value.get("completion_tokens", 0),
            "provider_call_audit_invalid",
        ),
    }


def _provider_call_totals(
    observations: Sequence[ProviderCallObservation],
) -> ProviderCallTotals:
    return ProviderCallTotals(
        call_count=len(observations),
        attempt_count=sum(item.attempt_count for item in observations),
        retry_count=sum(item.retry_count for item in observations),
        total_duration_ms=_stable_number(
            sum(float(item.total_duration_ms) for item in observations)
        ),
        retry_duration_ms=_stable_number(
            sum(float(item.retry_duration_ms) for item in observations)
        ),
        total_input_bytes=sum(item.total_input_bytes for item in observations),
        total_output_bytes=sum(item.total_output_bytes for item in observations),
        prompt_tokens=sum(item.prompt_tokens for item in observations),
        completion_tokens=sum(item.completion_tokens for item in observations),
    )


def _nonnegative_integer(value: Any, error: str) -> int:
    if type(value) is not int or value < 0:
        raise AnalysisPerformanceContractError(error)
    return value


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
