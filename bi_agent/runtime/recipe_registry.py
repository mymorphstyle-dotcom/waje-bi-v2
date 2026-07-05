from bi_agent.runtime.models import RecipeEntry


def load_recipe_registry() -> dict[str, RecipeEntry]:
    return {
        "pattern_explanation": RecipeEntry(
            recipe_id="pattern_explanation",
            question_family="pattern_explanation",
            subgraph_nodes=(
                "data_quality_check",
                "pattern_scan",
                "formula_decompose",
                "event_evidence",
                "segment_bridge",
                "outlier_scan",
                "answer_verify",
            ),
        ),
        "paid_amount_change_explanation": RecipeEntry(
            recipe_id="paid_amount_change_explanation",
            question_family="paid_amount_change_explanation",
            subgraph_nodes=(
                "data_quality_check",
                "formula_decompose",
                "segment_bridge",
                "answer_verify",
            ),
            default_degraded=True,
        ),
        "business_object_impact_review": RecipeEntry(
            recipe_id="business_object_impact_review",
            question_family="business_object_impact_review",
            subgraph_nodes=("data_quality_check", "segment_bridge", "answer_verify"),
            default_degraded=True,
        ),
        "revenue_health_review": RecipeEntry(
            recipe_id="revenue_health_review",
            question_family="revenue_health_review",
            subgraph_nodes=(
                "data_quality_check",
                "formula_decompose",
                "outlier_scan",
                "answer_verify",
            ),
            default_degraded=True,
        ),
        "segment_or_factor_attribution": RecipeEntry(
            recipe_id="segment_or_factor_attribution",
            question_family="segment_or_factor_attribution",
            subgraph_nodes=(
                "data_quality_check",
                "segment_bridge",
                "formula_decompose",
                "answer_verify",
            ),
            default_degraded=True,
        ),
        "anomaly_or_black_swan_review": RecipeEntry(
            recipe_id="anomaly_or_black_swan_review",
            question_family="anomaly_or_black_swan_review",
            subgraph_nodes=(
                "data_quality_check",
                "outlier_scan",
                "event_evidence",
                "answer_verify",
            ),
            default_degraded=True,
        ),
        "custom_baseline_comparison": RecipeEntry(
            recipe_id="custom_baseline_comparison",
            question_family="custom_baseline_comparison",
            subgraph_nodes=("data_quality_check", "pattern_scan", "answer_verify"),
            default_degraded=True,
        ),
        "data_quality_or_evidence_review": RecipeEntry(
            recipe_id="data_quality_or_evidence_review",
            question_family="data_quality_or_evidence_review",
            subgraph_nodes=("data_quality_check", "answer_verify"),
            default_degraded=True,
        ),
    }
