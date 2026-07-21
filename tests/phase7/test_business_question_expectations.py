from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
EXPECTATIONS = ROOT / "evals" / "phase7" / "business_question_expectations.yaml"


def test_business_expectations_require_the_real_gateway_chain():
    payload = yaml.safe_load(EXPECTATIONS.read_text(encoding="utf-8"))
    execution = payload["execution_contract"]

    assert execution == {
        "entrypoint": "gateway",
        "required_dependencies": ["postgres", "clickhouse", "deepseek"],
        "data_authority": "active_release",
        "prebound_sql": "forbidden",
        "injected_rows": "forbidden",
        "prebound_capabilities": "forbidden",
        "acceptance_source": "persisted_customer_publication",
    }


def test_business_expectations_contain_only_questions_and_review_focus():
    payload = yaml.safe_load(EXPECTATIONS.read_text(encoding="utf-8"))
    cases = payload["cases"]

    assert cases
    assert len({case["case_id"] for case in cases}) == len(cases)
    for case in cases:
        assert set(case) == {"case_id", "user_message", "review_focus"}
        assert case["user_message"].strip()
        assert case["review_focus"].strip()


def test_all_eight_question_families_have_original_and_natural_paraphrase():
    payload = yaml.safe_load(EXPECTATIONS.read_text(encoding="utf-8"))
    pairs = payload["question_family_pairs"]
    cases = {case["case_id"]: case for case in payload["cases"]}

    assert set(pairs) == {
        "pattern_explanation",
        "paid_amount_change_explanation",
        "business_object_impact_review",
        "revenue_health_review",
        "segment_or_factor_attribution",
        "anomaly_or_black_swan_review",
        "custom_baseline_comparison",
        "data_quality_or_evidence_review",
    }
    for pair in pairs.values():
        assert set(pair) == {"original_case_id", "paraphrase_case_id"}
        assert pair["original_case_id"] in cases
        assert pair["paraphrase_case_id"] in cases
        assert pair["original_case_id"] != pair["paraphrase_case_id"]
        assert (
            cases[pair["original_case_id"]]["user_message"]
            != cases[pair["paraphrase_case_id"]]["user_message"]
        )


def test_case_b_stability_slice_is_a_natural_language_additional_case():
    payload = yaml.safe_load(EXPECTATIONS.read_text(encoding="utf-8"))
    cases = {case["case_id"]: case for case in payload["cases"]}

    assert cases["daily_paid_amount_change_2026_06_01"] == {
        "case_id": "daily_paid_amount_change_2026_06_01",
        "user_message": "全量样本看，2026年6月1日付费金额为什么上涨？",
        "review_focus": (
            "Case B 稳定性切片：先确认上涨及比较基线，再用完整合同轴解释；"
            "连续运行和进程重启后必须保持同一权威语义。"
        ),
    }


def test_live_runner_maps_business_expectation_to_one_natural_language_turn():
    from tools.phase7.run_live_conversation_system_test import load_cases

    cases = load_cases(str(EXPECTATIONS))

    first = cases[0]
    assert first == {
        "id": "pattern_month_start_vs_mid_end",
        "question_family": "pattern_explanation",
        "variant": "original",
        "turns": [
            {
                "user": "全量样本看，2024-01到2026-06每个月月初1-10号付费金额是否高于月中和月末？",
                "review_focus": "先验证模式是否真实成立，再解释稳定性和例外月份。",
            }
        ],
    }
    assert all(
        set(case) == {"id", "question_family", "variant", "turns"} for case in cases
    )
    assert sum(case["variant"] == "original" for case in cases) == 8
    assert sum(case["variant"] == "paraphrase" for case in cases) == 8
    assert all(
        len(case["turns"]) == 1 and set(case["turns"][0]) == {"user", "review_focus"}
        for case in cases
    )


def test_live_runner_rejects_prebound_business_case_fields(tmp_path):
    from tools.phase7.run_live_conversation_system_test import load_cases

    path = tmp_path / "expectations.yaml"
    payload = yaml.safe_load(EXPECTATIONS.read_text(encoding="utf-8"))
    payload["cases"][0]["scenario"] = {
        "expected_dataset_states": {"paid_order_success": "executable"}
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    try:
        load_cases(str(path))
    except ValueError as exc:
        assert str(exc) == "business_expectation_case_shape_invalid"
    else:
        raise AssertionError("prebound business expectation was accepted")
