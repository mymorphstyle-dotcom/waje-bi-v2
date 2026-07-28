from __future__ import annotations

from copy import deepcopy

from tools.phase9.build_case_b_ab_report import _hard_contracts


def _record(*, controlled: bool, child_count: int) -> dict[str, object]:
    children = [
        {
            "dispatchState": "terminal",
            "terminalStatus": "limited",
            "acceptedAttemptRef": f"attempt:{index}",
            "acceptedArtifactRef": f"artifact:{index}",
            "outputDigest": f"digest:{index}",
        }
        for index in range(child_count)
    ]
    return {
        "controlledInvestigationEnabled": controlled,
        "counts": {
            "queries": 33,
            "acceptedTasks": 21,
            "evidenceEntries": 22,
            "verifiedClaims": 25,
            "narratives": 1,
            "publications": 1,
            "customerPayloads": 1,
            "children": child_count,
        },
        "providerCalls": [
            {
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
            }
        ],
        "children": children,
    }


def test_hard_contracts_accept_true_single_and_controlled_multi() -> None:
    contracts = _hard_contracts(
        _record(controlled=False, child_count=0),
        _record(controlled=True, child_count=2),
        same_accepted_plan=True,
        same_executable_plan=True,
    )

    assert all(contracts.values())


def test_hard_contracts_reject_single_run_with_controlled_children() -> None:
    single = _record(controlled=True, child_count=2)
    contracts = _hard_contracts(
        single,
        _record(controlled=True, child_count=2),
        same_accepted_plan=True,
        same_executable_plan=True,
    )

    assert contracts["single_mode_disabled"] is False
    assert contracts["single_has_no_children"] is False


def test_hard_contracts_reject_query_or_provider_drift() -> None:
    multi = _record(controlled=True, child_count=2)
    multi["counts"]["queries"] = 34  # type: ignore[index]
    multi["providerCalls"] = [
        {"provider": "openai", "model": "gpt-5"}
    ]
    contracts = _hard_contracts(
        _record(controlled=False, child_count=0),
        multi,
        same_accepted_plan=True,
        same_executable_plan=True,
    )

    assert contracts["same_query_count"] is False
    assert contracts["deepseek_only"] is False


def test_hard_contracts_reject_unclosed_child_source() -> None:
    multi = _record(controlled=True, child_count=2)
    children = deepcopy(multi["children"])
    children[0]["acceptedArtifactRef"] = None
    multi["children"] = children
    contracts = _hard_contracts(
        _record(controlled=False, child_count=0),
        multi,
        same_accepted_plan=True,
        same_executable_plan=True,
    )

    assert contracts["multi_children_source_closed"] is False
