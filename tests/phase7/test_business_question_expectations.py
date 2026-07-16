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
        "acceptance_source": "persisted_answer_package",
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


def test_live_runner_maps_business_expectation_to_one_natural_language_turn():
    from tools.phase7.run_live_conversation_system_test import load_cases

    cases = load_cases(str(EXPECTATIONS))

    first = cases[0]
    assert first == {
        "id": "pattern_month_start_vs_mid_end",
        "turns": [{
            "user": "全量样本看，2024-01到2026-06每个月月初1-10号付费金额是否高于月中和月末？",
            "review_focus": "先验证模式是否真实成立，再解释稳定性和例外月份。",
        }],
    }
    assert all(set(case) == {"id", "turns"} for case in cases)
    assert all(
        len(case["turns"]) == 1
        and set(case["turns"][0]) == {"user", "review_focus"}
        for case in cases
    )


def test_live_runner_rejects_prebound_business_case_fields(tmp_path):
    from tools.phase7.run_live_conversation_system_test import load_cases

    path = tmp_path / "expectations.yaml"
    path.write_text(
        yaml.safe_dump({
            "cases": [{
                "case_id": "prebound-case",
                "user_message": "昨天付费金额为什么变化？",
                "review_focus": "验证真实方向。",
                "scenario": {"expected_dataset_states": {"paid_order_success": "executable"}},
            }],
        }, allow_unicode=True),
        encoding="utf-8",
    )

    try:
        load_cases(str(path))
    except ValueError as exc:
        assert str(exc) == "business_expectation_case_shape_invalid"
    else:
        raise AssertionError("prebound business expectation was accepted")
