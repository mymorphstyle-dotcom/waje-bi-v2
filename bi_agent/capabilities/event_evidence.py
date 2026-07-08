from __future__ import annotations

from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def event_evidence(
    events: Iterable[dict[str, Any]] = (),
    *,
    event_window_policy: dict[str, Any] | None = None,
    low_risk_default: bool = True,
    result_refs: tuple[str, ...] = (),
):
    events = tuple(events)
    policy = event_window_policy or {"window": "business_default", "aggregate_only": True}
    return make_evidence_envelope(
        "event_evidence",
        evidence_type="candidate_mechanism" if events else "insufficient_evidence",
        strength="low",
        wording_limit="candidate" if events else "insufficient",
        typed_payload={
            "events": events,
            "event_window_policy": policy,
            "low_risk_default": low_risk_default,
            "business_readout": "活动窗口证据仅作为候选机制检查。",
            "claim_boundary": "活动窗口重合只能作为候选机制，不能直接写成因果结论。",
        },
        limitations=() if events else ("no_event_contract_or_matches",),
        result_refs=result_refs,
    )


collect_event_evidence = event_evidence
