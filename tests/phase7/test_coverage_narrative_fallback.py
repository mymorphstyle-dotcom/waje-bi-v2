from bi_agent.runtime.langgraph_workflow import (
    _deterministic_coverage_interpretation,
)


def test_coverage_narrative_failure_falls_back_without_granting_new_claims():
    output = _deterministic_coverage_interpretation(
        block_reason="",
        answerable_reason="核心比较数据完整，辅助数据存在局部缺口。",
        fallback_reason="llm_narrative_invalid:business_impact",
    )

    assert output["coverage_status"] == "coverage_gap_but_answerable"
    assert output["local_narrative_fallback"] is True
    assert output["fallback_reason"] == "llm_narrative_invalid:business_impact"
    assert "window_id" not in output["business_impact"]
    assert "observation_key" not in output["decision_summary"]


def test_local_hard_boundary_still_blocks_when_coverage_writer_fails():
    output = _deterministic_coverage_interpretation(
        block_reason="no_rows",
        answerable_reason="",
        fallback_reason="provider_unavailable",
    )

    assert output["coverage_status"] == "blocked"
    assert "没有返回可分析数据" in output["business_impact"]
