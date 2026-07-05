from typing import Any, Iterable

from bi_agent.capabilities import make_evidence_envelope


def event_evidence(
    events: Iterable[dict[str, Any]] = (),
    *,
    result_refs: tuple[str, ...] = (),
):
    events = tuple(events)
    return make_evidence_envelope(
        "event_evidence",
        evidence_type="candidate_mechanism" if events else "insufficient_evidence",
        strength="low",
        wording_limit="candidate" if events else "insufficient",
        typed_payload={"events": events},
        limitations=() if events else ("no_event_contract_or_matches",),
        result_refs=result_refs,
    )


collect_event_evidence = event_evidence
