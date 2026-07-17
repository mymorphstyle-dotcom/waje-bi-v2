from datetime import date, timedelta
from math import sin
from unittest.mock import patch

from bi_agent.runtime.capability_execution import BoundCapabilityInput
from bi_agent.runtime.capability_harness import (
    _align_cross_source_rows,
    execute_capability,
)
from bi_agent.runtime.capability_models import BudgetState, CapabilityRequest
from bi_agent.runtime.capability_registry import (
    get_capability_card,
    public_capability_ids,
)


def _bound_input(
    rows_by_slot,
    *,
    capability_id="cross_source_association",
    status="ready",
    reasons=(),
):
    bound = object.__new__(BoundCapabilityInput)
    for field in BoundCapabilityInput.__annotations__:
        if field in {"rows_by_slot", "binding_manifest"}:
            value = {}
        elif field == "maximum_claim_strength_rank":
            value = 2
        elif field.endswith("s") or field.endswith("refs"):
            value = ()
        else:
            value = ""
        object.__setattr__(bound, field, value)
    object.__setattr__(bound, "capability_id", capability_id)
    object.__setattr__(bound, "capability_contract_ref", "capability:association:v1")
    object.__setattr__(bound, "analysis_contract_ref", "analysis:case-b")
    object.__setattr__(bound, "status", status)
    object.__setattr__(bound, "rows_by_slot", rows_by_slot)
    object.__setattr__(bound, "reasons", tuple(reasons))
    object.__setattr__(bound, "result_refs", ("result:paid", "result:gameplay"))
    object.__setattr__(
        bound,
        "query_contract_refs",
        ("query:paid", "query:gameplay"),
    )
    object.__setattr__(
        bound,
        "supported_evidence_types",
        ("statistical_association",),
    )
    object.__setattr__(
        bound,
        "supported_claim_types",
        ("cross_source_statistical_association",),
    )
    object.__setattr__(bound, "maximum_claim_strength", "candidate_driver")
    object.__setattr__(bound, "binding_manifest_ref", "binding:association")
    object.__setattr__(bound, "binding_manifest_digest", "digest:association")
    return bound


def _request(
    bound,
    *,
    capability_id="cross_source_association",
    **param_overrides,
):
    return CapabilityRequest(
        run_id="run-case-b-association",
        accepted_graph_id="graph:case-b",
        graph_version=1,
        capability_id=capability_id,
        question_family="paid_amount_change_explanation",
        target_claim="Identify stable cross-source candidate drivers.",
        claim_type="cross_source_statistical_association",
        metric="paid_amount",
        scope="all_successful_paid_orders",
        time_window="2026-01-01..2026-06-01",
        baseline={"label": "2026-05-31"},
        target={"label": "2026-06-01"},
        grain="day",
        filters={},
        dimensions=(),
        contract_versions={},
        budget_state=BudgetState(
            mode="research",
            used_capability_calls=0,
            soft_limit=50,
            hard_limit=100,
        ),
        llm_business_reason="Check cross-source associations and their stability.",
        params={
            "rows": ({"observation_key": "untrusted", "paid_amount": -1},),
            "methods": ("pearson", "spearman"),
            "transforms": ("level",),
            "lags": (0,),
            "min_samples": 30,
            "rolling_window": 45,
            "rolling_step": 20,
            **param_overrides,
        },
        bound_input=bound,
    )


def _series_rows(size=140):
    outcome_rows = []
    candidate_rows = []
    for index in range(size):
        observation_key = (date(2026, 1, 1) + timedelta(days=index)).isoformat()
        common = 100.0 + index * 1.7 + sin(index / 5.0)
        outcome = {
            "window_id": "trailing_context",
            "window_role": "context",
            "observation_key": observation_key,
            "paid_amount": common * 10.0,
            "paid_users": common * 0.8,
            "paid_orders": common * 1.4,
            "paid_frequency": 1.5 + index * 0.001,
            "avg_order_amount": 7.0 + common * 0.03,
        }
        candidate = {
            "window_id": "trailing_context",
            "window_role": "context",
            "observation_key": observation_key,
            "player_bet_amount": common * 4.0,
            "player_bet_count": common * 1.2,
        }
        outcome_rows.append(outcome)
        candidate_rows.append(candidate)

    # The target and primary-baseline windows overlap the trailing context.
    # Equal observations must be deduplicated, independent of window labels.
    outcome_rows.append(
        {
            **outcome_rows[-1],
            "window_id": "target_day",
            "window_role": "target",
        }
    )
    candidate_rows.append(
        {
            **candidate_rows[-1],
            "window_id": "target_day",
            "window_role": "target",
        }
    )
    return outcome_rows, candidate_rows


def test_harness_aligns_authenticated_slots_and_evaluates_every_outcome_metric():
    outcome_rows, candidate_rows = _series_rows()
    bound = _bound_input(
        {
            "association_outcome_timeseries": tuple(outcome_rows),
            "association_candidate_timeseries": tuple(candidate_rows),
        }
    )

    with patch(
        "bi_agent.runtime.capability_harness.validate_bound_capability_input",
        return_value="",
    ):
        envelope = execute_capability(_request(bound))

    assert envelope.evidence_type == "statistical_association"
    assert envelope.typed_payload["primary_outcome"] == "paid_amount"
    assert set(envelope.typed_payload["associations_by_outcome"]) == {
        "paid_amount",
        "paid_users",
        "paid_orders",
        "paid_frequency",
        "avg_order_amount",
    }
    assert envelope.typed_payload["candidate_metrics"] == (
        "player_bet_amount",
        "player_bet_count",
    )
    assert envelope.typed_payload["alignment"]["aligned_observation_count"] == 140
    assert envelope.typed_payload["alignment"]["duplicate_window_rows_deduplicated"] == 2
    assert "rows" not in envelope.typed_payload["alignment"]
    assert envelope.typed_payload["correlation_coefficient_is_contribution"] is False
    assert envelope.typed_payload["causal_claim_allowed"] is False
    assert envelope.result_refs == ("result:paid", "result:gameplay")
    assert envelope.query_contract_refs == ("query:paid", "query:gameplay")
    assert envelope.analysis_contract_ref == "analysis:case-b"


def test_alignment_rejects_conflicting_duplicate_metric_values():
    outcome_rows, candidate_rows = _series_rows(size=40)
    outcome_rows.append(
        {
            **outcome_rows[0],
            "window_id": "primary_baseline",
            "paid_amount": outcome_rows[0]["paid_amount"] + 1,
        }
    )
    bound = _bound_input(
        {
            "association_outcome_timeseries": tuple(outcome_rows),
            "association_candidate_timeseries": tuple(candidate_rows),
        }
    )

    aligned, limitation = _align_cross_source_rows(bound)

    assert aligned["aligned_observation_count"] == 0
    assert limitation == (
        "association_duplicate_value_conflict:"
        "association_outcome_timeseries:paid_amount"
    )


def test_missing_candidate_slot_returns_insufficient_envelope_without_exception():
    outcome_rows, _ = _series_rows(size=40)
    bound = _bound_input(
        {
            "association_outcome_timeseries": tuple(outcome_rows),
        },
        status="degraded",
        reasons=("missing_optional_slot:association_candidate_timeseries",),
    )

    with patch(
        "bi_agent.runtime.capability_harness.validate_bound_capability_input",
        return_value="",
    ):
        envelope = execute_capability(_request(bound))

    assert envelope.evidence_type == "insufficient"
    assert envelope.wording_limit == "insufficient"
    assert envelope.typed_payload["status"] == "insufficient"
    assert envelope.limitations == ("association_candidate_rows_missing",)
    assert envelope.analysis_contract_ref == "analysis:case-b"


def _panel_series_rows(periods=18, panels=5):
    outcome_rows = []
    candidate_rows = []
    for time_index in range(periods):
        observation_key = f"2026-05-{time_index + 1:02d}"
        for panel_index in range(panels):
            interaction = ((time_index + 1) * (panel_index + 2) % 13) - 6
            gameplay_value = (
                40.0
                + 7.0 * panel_index
                + 3.0 * time_index
                + interaction
            )
            paid_value = (
                100.0
                - 5.0 * panel_index
                + 8.0 * time_index
                + 1.9 * interaction
                + ((time_index + panel_index) % 3 - 1) * 0.03
            )
            outcome_rows.append(
                {
                    "window_id": "trailing_context",
                    "window_role": "context",
                    "observation_key": observation_key,
                    "channel": f"PA_Channel-{panel_index}",
                    "paid_amount": paid_value,
                    "paid_users": paid_value * 0.2,
                    "paid_orders": paid_value * 0.35,
                    "paid_frequency": 1.5 + interaction * 0.01,
                    "avg_order_amount": 7.0 + interaction * 0.03,
                }
            )
            candidate_rows.append(
                {
                    "window_id": "trailing_context",
                    "window_role": "context",
                    "observation_key": observation_key,
                    "channel": f"channel{panel_index}",
                    "player_bet_amount": gameplay_value,
                    "player_bet_count": gameplay_value * 0.4,
                }
            )
    outcome_rows.append(
        {
            **outcome_rows[-1],
            "window_id": "target_day",
            "window_role": "target",
        }
    )
    candidate_rows.append(
        {
            **candidate_rows[-1],
            "window_id": "target_day",
            "window_role": "target",
        }
    )
    return outcome_rows, candidate_rows


def test_panel_harness_crosswalks_channels_and_caps_results_at_sensitivity():
    outcome_rows, candidate_rows = _panel_series_rows()
    bound = _bound_input(
        {
            "association_outcome_timeseries": tuple(outcome_rows),
            "association_candidate_timeseries": tuple(candidate_rows),
        },
        capability_id="cross_source_panel_association",
    )

    with patch(
        "bi_agent.runtime.capability_harness.validate_bound_capability_input",
        return_value="",
    ):
        envelope = execute_capability(
            _request(
                bound,
                capability_id="cross_source_panel_association",
                hypotheses=(
                    {
                        "hypothesis_id": "paid-vs-bet-level-lag0",
                        "outcome_metric": "paid_amount",
                        "candidate_metric": "player_bet_amount",
                        "transform": "level",
                        "lag": 0,
                    },
                ),
                mapping_authority_status="contracted",
                min_samples=30,
                min_panels=3,
                min_panel_samples=6,
            )
        )

    assert envelope.evidence_type == "statistical_association"
    assert envelope.strength == "low"
    assert envelope.wording_limit == "sensitivity_only"
    assert envelope.typed_payload["claim_ceiling"] == "sensitivity_only"
    assert envelope.typed_payload["specific_channel_claim_allowed"] is False
    assert envelope.typed_payload["causal_claim_allowed"] is False
    assert envelope.typed_payload["contribution_claim_allowed"] is False
    assert envelope.typed_payload["mapping"]["authority_status"] == (
        "candidate_mechanical_crosswalk"
    )
    assert envelope.typed_payload["mapping"]["authority_established"] is False
    assert envelope.typed_payload["mapping"]["candidate_rules"] == (
        "unicode_casefold",
        "remove_non_alphanumeric",
        "strip_paid_source_prefix_pa",
    )
    assert envelope.typed_payload["mapping"]["pair_count"] == 5
    assert envelope.typed_payload["mapping"]["specific_mapping_pairs_included"] is False
    assert set(envelope.typed_payload["associations_by_hypothesis"]) == {
        "paid-vs-bet-level-lag0"
    }
    assert envelope.numeric_facts["requested_hypothesis_count"] == 1
    assert envelope.numeric_facts["evaluated_hypothesis_count"] == 1
    assert envelope.numeric_facts["statistical_hypothesis_count"] == 1
    assert "PA_Channel" not in repr(envelope.typed_payload)
    assert "mapping_pairs" not in envelope.typed_payload["mapping"]
    assert envelope.analysis_contract_ref == "analysis:case-b"
    assert envelope.result_refs == ("result:paid", "result:gameplay")


def test_panel_harness_missing_candidate_data_is_local_insufficient():
    outcome_rows, _ = _panel_series_rows(periods=8, panels=3)
    bound = _bound_input(
        {
            "association_outcome_timeseries": tuple(outcome_rows),
        },
        capability_id="cross_source_panel_association",
        status="degraded",
        reasons=("missing_optional_slot:association_candidate_timeseries",),
    )

    with patch(
        "bi_agent.runtime.capability_harness.validate_bound_capability_input",
        return_value="",
    ):
        envelope = execute_capability(
            _request(
                bound,
                capability_id="cross_source_panel_association",
            )
        )

    assert envelope.evidence_type == "insufficient"
    assert envelope.wording_limit == "sensitivity_only"
    assert envelope.limitations == ("panel_association_candidate_rows_missing",)
    assert envelope.typed_payload["specific_channel_claim_allowed"] is False


def test_registry_replaces_activity_context_with_cross_source_capabilities():
    capability_ids = public_capability_ids()

    assert "gameplay_activity_context" not in capability_ids
    for capability_id in (
        "cross_source_association",
        "cross_source_panel_association",
    ):
        card = get_capability_card(capability_id)
        assert card.allowed_claim_types == (
            "cross_source_statistical_association",
        )
        assert card.default_evidence_type == "statistical_association"
        assert set(card.supported_question_families) == {
            "paid_amount_change_explanation",
            "segment_or_factor_attribution",
            "business_object_impact_review",
        }


def test_panel_harness_executes_only_explicit_hypotheses_and_isolates_pair_gaps():
    outcome_rows, candidate_rows = _panel_series_rows(periods=18, panels=5)
    bound = _bound_input(
        {
            "association_outcome_timeseries": tuple(outcome_rows),
            "association_candidate_timeseries": tuple(candidate_rows),
        },
        capability_id="cross_source_panel_association",
    )
    hypotheses = (
        {
            "hypothesis_id": "paid-vs-bet-level-lag0",
            "outcome_metric": "paid_amount",
            "candidate_metric": "player_bet_amount",
            "transform": "level",
            "lag": 0,
        },
        {
            "hypothesis_id": "paid-vs-missing-level-lag0",
            "outcome_metric": "paid_amount",
            "candidate_metric": "missing_gameplay_metric",
            "transform": "level",
            "lag": 0,
        },
    )

    with patch(
        "bi_agent.runtime.capability_harness.validate_bound_capability_input",
        return_value="",
    ):
        envelope = execute_capability(
            _request(
                bound,
                capability_id="cross_source_panel_association",
                hypotheses=hypotheses,
                min_samples=30,
                min_panels=3,
                min_panel_samples=6,
            )
        )

    assert envelope.numeric_facts["requested_hypothesis_count"] == 2
    assert envelope.numeric_facts["evaluated_hypothesis_count"] == 2
    assert envelope.numeric_facts["statistical_hypothesis_count"] == 1
    assert set(envelope.typed_payload["associations_by_hypothesis"]) == {
        "paid-vs-bet-level-lag0",
        "paid-vs-missing-level-lag0",
    }
    supported = envelope.typed_payload["associations_by_hypothesis"][
        "paid-vs-bet-level-lag0"
    ]
    missing = envelope.typed_payload["associations_by_hypothesis"][
        "paid-vs-missing-level-lag0"
    ]
    assert supported["evidence_type"] == "statistical_association"
    assert missing["evidence_type"] == "insufficient"
    assert missing["limitations"] == (
        "panel_hypothesis_candidate_metric_missing:missing_gameplay_metric",
    )
    assert all(
        "missing_gameplay_metric" not in limitation
        for limitation in envelope.limitations
    )
    assert envelope.typed_payload["mapping"]["authority_status"] == (
        "candidate_mechanical_crosswalk"
    )
    assert envelope.typed_payload["mapping"]["authority_established"] is False
    assert "owner" not in repr(envelope.typed_payload).casefold()
    assert "owner" not in repr(envelope.limitations).casefold()


def test_panel_harness_requires_hypotheses_instead_of_scanning_cartesian_product():
    outcome_rows, candidate_rows = _panel_series_rows(periods=18, panels=5)
    bound = _bound_input(
        {
            "association_outcome_timeseries": tuple(outcome_rows),
            "association_candidate_timeseries": tuple(candidate_rows),
        },
        capability_id="cross_source_panel_association",
    )

    with patch(
        "bi_agent.runtime.capability_harness.validate_bound_capability_input",
        return_value="",
    ):
        envelope = execute_capability(
            _request(
                bound,
                capability_id="cross_source_panel_association",
                min_samples=30,
                min_panels=3,
                min_panel_samples=6,
            )
        )

    assert envelope.evidence_type == "insufficient"
    assert envelope.typed_payload["status"] == "insufficient"
    assert envelope.limitations == ("panel_hypotheses_missing",)


def test_panel_harness_records_metric_specific_mapping_coverage_basis():
    outcome_rows, candidate_rows = _panel_series_rows(periods=18, panels=5)
    for row in candidate_rows:
        row["player_avg_bet_amount"] = row["player_bet_amount"] / max(
            row["player_bet_count"], 1e-9
        )
    bound = _bound_input(
        {
            "association_outcome_timeseries": tuple(outcome_rows),
            "association_candidate_timeseries": tuple(candidate_rows),
        },
        capability_id="cross_source_panel_association",
    )

    with patch(
        "bi_agent.runtime.capability_harness.validate_bound_capability_input",
        return_value="",
    ):
        envelope = execute_capability(
            _request(
                bound,
                capability_id="cross_source_panel_association",
                hypotheses=(
                    {
                        "hypothesis_id": "aov-vs-avg-bet-level-lag0",
                        "outcome_metric": "avg_order_amount",
                        "candidate_metric": "player_avg_bet_amount",
                        "transform": "level",
                        "lag": 0,
                    },
                ),
                min_samples=30,
                min_panels=3,
                min_panel_samples=6,
            )
        )

    bundle = envelope.typed_payload["associations_by_hypothesis"][
        "aov-vs-avg-bet-level-lag0"
    ]
    basis = bundle["association"]["mapping"]["coverage_basis"]
    assert basis["combination"] == "minimum_of_source_metric_coverage"
    assert basis["outcome"]["basis"] == "observed_metric_cells"
    assert basis["candidate"]["basis"] == "observed_metric_cells"
