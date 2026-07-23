from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from bi_agent.capabilities import make_evidence_envelope
from bi_agent.runtime.evidence_authority import canonical_digest


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
    source_events = _real_events(events, event_ref=event_ref)
    events = tuple(_audited_event(item) for item in source_events)
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
            "event_summary": tuple(_public_event(item) for item in source_events),
            "business_readout": "活动窗口证据仅作为候选机制检查。",
            "claim_boundary": "活动窗口重合只能作为候选机制，不能直接写成因果结论。",
            "synthesis_contract": {
                "schema_version": "public-fact-projection.v1",
                "public_fact_paths": (
                    "business_readout",
                    "claim_boundary",
                    "event_summary",
                ),
            },
        },
        limitations=() if events else ("no_event_matches",),
        result_refs=result_refs,
    )


collect_event_evidence = event_evidence


def _audited_event(event: Mapping[str, Any]) -> dict[str, Any]:
    audited = {
        key: event[key]
        for key in (
            "event_id",
            "source_family",
            "window_role",
            "event_type",
            "event_start_date",
            "event_end_date",
            "affected_scope",
            "authority",
            "evidence_level",
            "wording_limit",
            "event_count",
        )
        if key in event
    }
    audited["source_event_digest"] = canonical_digest(event)
    return audited


def _public_event(event: Mapping[str, Any]) -> dict[str, Any]:
    summary = {
        key: event[key]
        for key in (
            "source_family",
            "window_role",
            "event_type",
            "event_start_date",
            "event_end_date",
            "affected_scope",
            "authority",
            "evidence_level",
            "wording_limit",
            "event_count",
        )
        if key in event
    }
    raw_payload = event.get("payload")
    if isinstance(raw_payload, str):
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping):
            for key in ("business_use", "description"):
                value = payload.get(key)
                if isinstance(value, str) and value and value == value.strip():
                    summary[key] = value
    return summary


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
