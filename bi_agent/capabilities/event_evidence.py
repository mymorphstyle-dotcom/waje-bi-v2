from __future__ import annotations

from typing import Any, Iterable, Mapping

from bi_agent.capabilities import make_evidence_envelope


EVENT_PRESENCE_EVIDENCE_CONTRACT = "event-presence.v1"


def event_evidence(
    events: Iterable[Mapping[str, Any]] = (),
    *,
    event_ref: str | None = None,
    temporal_authority_ref: str | None = None,
    result_refs: tuple[str, ...] = (),
):
    if (event_ref is None) != (temporal_authority_ref is None):
        raise ValueError("event_evidence_temporal_identity_incomplete")
    if event_ref is not None and (
        not isinstance(event_ref, str)
        or not event_ref
        or event_ref != event_ref.strip()
        or not isinstance(temporal_authority_ref, str)
        or not temporal_authority_ref
        or temporal_authority_ref != temporal_authority_ref.strip()
    ):
        raise ValueError("event_evidence_temporal_identity_invalid")
    events = _real_events(events, event_ref=event_ref)
    authority_payload = (
        {
            "evidence_contract": EVENT_PRESENCE_EVIDENCE_CONTRACT,
            "event_ref": event_ref,
            "temporal_authority_ref": temporal_authority_ref,
            "causal_interpretation_allowed": False,
        }
        if event_ref is not None
        else {}
    )
    return make_evidence_envelope(
        "event_evidence",
        evidence_type="candidate_mechanism" if events else "insufficient_evidence",
        strength="low",
        wording_limit="candidate" if events else "insufficient",
        typed_payload={
            **authority_payload,
            "events": events,
            "business_readout": "活动窗口证据仅作为候选机制检查。",
            "claim_boundary": "活动窗口重合只能作为候选机制，不能直接写成因果结论。",
        },
        limitations=() if events else ("no_event_contract_or_matches",),
        result_refs=result_refs,
    )


collect_event_evidence = event_evidence


def _real_events(
    events: Iterable[Mapping[str, Any]],
    *,
    event_ref: str | None = None,
) -> tuple[dict[str, Any], ...]:
    output = []
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("event_evidence_row_invalid")
        event_id = event.get("event_id")
        event_count = event.get("event_count")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_evidence_id_invalid")
        if isinstance(event_count, bool) or not isinstance(event_count, int):
            raise ValueError("event_evidence_count_invalid")
        is_sentinel = event_id.startswith("__no_event__:")
        if is_sentinel:
            if event_count != 0:
                raise ValueError("event_evidence_sentinel_count_invalid")
            continue
        if event_count <= 0:
            raise ValueError("event_evidence_count_invalid")
        if event_ref is not None and event_id != event_ref:
            continue
        output.append(dict(event))
    return tuple(output)
