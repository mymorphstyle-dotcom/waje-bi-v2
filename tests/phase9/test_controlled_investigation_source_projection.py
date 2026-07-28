from pathlib import Path

from bi_agent.runtime.controlled_investigation_runtime import (
    CONTROLLED_INVESTIGATION_INSTRUCTIONS,
)
from bi_agent.runtime.controlled_investigation_workflow import (
    _child_source_record,
    _source_catalog_record,
)


def _evidence_record(fact_count: int) -> dict[str, object]:
    return {
        "sourceKind": "customer_safe_evidence",
        "sourceRef": "evidence:test",
        "payload": {
            "scope": "scope:test",
            "dimension_path": ["region"],
            "evidence_kind": "observed",
            "evidence_strength": "medium",
            "maximum_claim_strength": "directional",
            "material_handle": "m_test",
            "interpretation_contract": {
                "contract_id": "observed.v1",
                "analysis_role": "diagnostic",
                "causal_inference_allowed": False,
            },
            "facts": [
                {
                    "name": (
                        f"observation_{index}.summary"
                        if index < 4
                        else f"observation_5.members[{index}].value"
                    ),
                    "fact_kind": "number",
                    "value": str(index),
                    "range_end": None,
                    "unit": "count",
                    "fact_handle": f"f_{index}",
                    "content_digest": "a" * 64,
                    "source_fact_refs": [f"source-fact:{index}"],
                }
                for index in range(fact_count)
            ],
        },
    }


def test_large_evidence_uses_lossless_columnar_projection() -> None:
    projected = _child_source_record(_evidence_record(160))
    payload = projected["payload"]

    assert payload["factSelection"] == {
        "mode": "complete_columnar_projection",
        "sourceFactCount": 160,
        "selectedFactCount": 160,
        "omittedFactCount": 0,
    }
    assert payload["factColumns"] == [
        "name",
        "fact_kind",
        "value",
        "range_end",
        "unit",
    ]
    assert len(payload["facts"]) == 160
    assert any("[" in row[0] for row in payload["facts"])


def test_source_catalog_exposes_structure_and_read_cost_without_fact_dump() -> None:
    record = _evidence_record(160)
    catalog = _source_catalog_record(
        "evidence:test",
        record,
        read_bytes=2048,
    )

    assert catalog["factCount"] == 160
    assert catalog["factKinds"] == ["number"]
    assert catalog["contractId"] == "observed.v1"
    assert catalog["analysisRole"] == "diagnostic"
    assert catalog["childReadBytes"] == 2048
    assert "factNames" not in catalog


def test_parent_planner_uses_provider_compatible_purpose_profile() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "bi_agent/runtime/controlled_investigation_workflow.py"
    ).read_text(encoding="utf-8")
    planner_call = source[
        source.index('task="plan_controlled_investigations"') :
        source.index("proposal = ControlledInvestigationProposal")
    ]

    assert 'thinking="disabled"' in planner_call
    assert "Return one JSON object" in planner_call
    assert "Return one JSON object" in CONTROLLED_INVESTIGATION_INSTRUCTIONS
