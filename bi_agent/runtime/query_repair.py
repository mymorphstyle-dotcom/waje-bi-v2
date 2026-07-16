from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from bi_agent.runtime.analysis_contracts import CompletenessReport, QueryContract


@dataclass(frozen=True)
class QueryRepairDecision:
    action: str
    reason: str
    failed_query_contract_ref: str
    failed_signature: str
    requires_llm: bool
    requires_clarification: bool
    report_ref: str
    failure_reasons: tuple[str, ...]


def plan_query_repair(
    contract: QueryContract,
    report: CompletenessReport,
    attempted_signatures: Iterable[str],
) -> QueryRepairDecision:
    reasons = tuple(report.failure_reasons)
    reason_set = set(reasons)

    transient_reasons = tuple(
        reason
        for reason in reasons
        if reason.startswith("transient_clickhouse:")
    )
    execution_metadata = tuple(
        reason for reason in reasons if reason.startswith("execution_status:")
    )
    if transient_reasons and len(reasons) == len(
        (*transient_reasons, *execution_metadata)
    ):
        return _decision(
            contract,
            report,
            action="retry_same",
            reason="transient_clickhouse",
            requires_llm=False,
            requires_clarification=False,
        )

    if contract.contract_signature in set(attempted_signatures):
        return _decision(
            contract,
            report,
            action="degrade",
            reason="repeated_query_contract_signature",
            requires_llm=False,
            requires_clarification=False,
        )

    if any(
        reason.startswith(("missing_field:", "invalid_type:", "duplicate_key:"))
        for reason in reason_set
    ):
        return _decision(
            contract,
            report,
            action="recompile",
            reason="query_shape_mismatch",
            requires_llm=True,
            requires_clarification=False,
        )

    if any(
        reason.startswith(
            (
                "snapshot_stale:",
                "missing_required_window:",
                "incomplete_window:",
            )
        )
        for reason in reason_set
    ):
        return _decision(
            contract,
            report,
            action="block",
            reason="window_coverage_failure",
            requires_llm=False,
            requires_clarification=False,
        )

    return _decision(
        contract,
        report,
        action="degrade",
        reason="insufficient_complete_evidence",
        requires_llm=True,
        requires_clarification=False,
    )


def _decision(
    contract: QueryContract,
    report: CompletenessReport,
    *,
    action: str,
    reason: str,
    requires_llm: bool,
    requires_clarification: bool,
) -> QueryRepairDecision:
    return QueryRepairDecision(
        action=action,
        reason=reason,
        failed_query_contract_ref=contract.query_contract_id,
        failed_signature=contract.contract_signature,
        requires_llm=requires_llm,
        requires_clarification=requires_clarification,
        report_ref=report.report_ref,
        failure_reasons=tuple(report.failure_reasons),
    )
