from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from bi_agent.capabilities import make_evidence_envelope
from bi_agent.runtime.evidence_authority import canonical_digest


EVENT_PRESENCE_EVIDENCE_CONTRACT = "event-presence.v1"
_CLAIM_MATERIAL_EVENT_LIMIT = 20


def event_evidence(
    events: Iterable[Mapping[str, Any]] = (),
    *,
    event_ref: str | None = None,
    temporal_authority_ref: str | None = None,
    event_window_set: Mapping[str, Any] | None = None,
    result_refs: tuple[str, ...] = (),
):
    if event_window_set is not None and (
        event_ref is not None or temporal_authority_ref is not None
    ):
        raise ValueError("event_evidence_temporal_identity_conflict")
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
    if event_window_set is not None:
        dynamic_event_ref = event_window_set.get("event_ref")
        dynamic_authority_ref = event_window_set.get("temporal_authority_ref")
        source_authority_ref = event_window_set.get(
            "source_temporal_authority_ref"
        )
        occurrences = event_window_set.get("occurrences")
        if (
            not isinstance(dynamic_event_ref, str)
            or not dynamic_event_ref
            or not isinstance(dynamic_authority_ref, str)
            or not dynamic_authority_ref
            or not isinstance(source_authority_ref, str)
            or not source_authority_ref
            or not isinstance(occurrences, (list, tuple))
            or any(not isinstance(item, Mapping) for item in occurrences)
        ):
            raise ValueError("event_evidence_dynamic_identity_invalid")
        authority_payload = {
            "evidence_contract": EVENT_PRESENCE_EVIDENCE_CONTRACT,
            "event_ref": dynamic_event_ref,
            "temporal_authority_ref": dynamic_authority_ref,
            "source_temporal_authority_ref": source_authority_ref,
            "event_occurrence_summary": tuple(
                {
                    key: item[key]
                    for key in (
                        "occurrence_ref",
                        "source_family",
                        "event_type",
                        "event_start_date",
                        "event_end_date",
                        "affected_scope",
                        "authority",
                        "evidence_level",
                        "wording_limit",
                    )
                    if key in item
                }
                for item in occurrences
            ),
            "causal_interpretation_allowed": False,
        }
    event_summary = tuple(_public_event(item) for item in source_events)
    occurrence_summary = tuple(
        authority_payload.get("event_occurrence_summary") or ()
    )
    claim_material_summary = {
        "projection_kind": "claim_material_summary",
        **{
            key: authority_payload[key]
            for key in (
                "evidence_contract",
                "event_ref",
                "temporal_authority_ref",
                "source_temporal_authority_ref",
                "causal_interpretation_allowed",
            )
            if key in authority_payload
        },
        "event_count": len(event_summary),
        "displayed_event_count": min(
            len(event_summary), _CLAIM_MATERIAL_EVENT_LIMIT
        ),
        "omitted_event_count": max(
            0, len(event_summary) - _CLAIM_MATERIAL_EVENT_LIMIT
        ),
        "event_record_limit": _CLAIM_MATERIAL_EVENT_LIMIT,
        "event_selection_policy": "canonical_source_order_first",
        "event_summary": event_summary[:_CLAIM_MATERIAL_EVENT_LIMIT],
        "event_occurrence_count": len(occurrence_summary),
        "displayed_event_occurrence_count": min(
            len(occurrence_summary), _CLAIM_MATERIAL_EVENT_LIMIT
        ),
        "omitted_event_occurrence_count": max(
            0, len(occurrence_summary) - _CLAIM_MATERIAL_EVENT_LIMIT
        ),
        "event_occurrence_summary": occurrence_summary[
            :_CLAIM_MATERIAL_EVENT_LIMIT
        ],
        "business_readout": "活动窗口证据仅作为候选机制检查。",
        "claim_boundary": "活动窗口重合只能作为候选机制，不能直接写成因果结论。",
    }
    return make_evidence_envelope(
        "event_evidence",
        evidence_type="candidate_mechanism" if events else "insufficient_evidence",
        strength="low",
        wording_limit="candidate" if events else "insufficient",
        typed_payload={
            **authority_payload,
            "events": events,
            "event_summary": event_summary,
            "claim_material_observations": (claim_material_summary,),
            "business_readout": "活动窗口证据仅作为候选机制检查。",
            "claim_boundary": "活动窗口重合只能作为候选机制，不能直接写成因果结论。",
            "synthesis_contract": {
                "schema_version": "public-fact-projection.v1",
                "public_fact_paths": (
                    "business_readout",
                    "claim_boundary",
                    "event_summary",
                    *(
                        ("event_occurrence_summary",)
                        if event_window_set is not None
                        else ()
                    ),
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
