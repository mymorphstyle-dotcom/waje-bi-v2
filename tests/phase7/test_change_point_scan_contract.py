from __future__ import annotations

from math import isfinite

import pytest

from bi_agent.capabilities.change_point_scan import (
    ChangePointScanContractError,
    change_point_scan,
)


def _scan(rows):
    return change_point_scan(
        rows,
        time_key="observation_key",
        value_key="paid_amount",
        min_total_samples=8,
        min_segment_samples=4,
        min_relative_level_shift=0.2,
        min_standardized_level_shift=2.0,
        max_candidates=5,
    )


@pytest.mark.parametrize(
    ("left_time", "right_time"),
    ((1, 1.0), (0, -0.0)),
)
def test_change_point_scan_rejects_equivalent_numeric_time_keys(
    left_time,
    right_time,
) -> None:
    rows = tuple(
        {
            "observation_key": time,
            "paid_amount": index,
        }
        for index, time in enumerate(
            (left_time, right_time, 2, 3, 4, 5, 6, 7),
            start=1,
        )
    )

    with pytest.raises(
        ChangePointScanContractError,
        match="change_point_scan_time_duplicate",
    ):
        _scan(rows)


def test_change_point_scan_keeps_distinct_large_integer_time_keys() -> None:
    first = 2**53
    result = _scan(
        tuple(
            {
                "observation_key": first + index,
                "paid_amount": value,
            }
            for index, value in enumerate(
                (10.0, 11.0, 9.0, 10.0, 20.0, 19.0, 21.0, 20.0),
            )
        )
    )

    assert result.typed_payload["sample_count"] == 8


def test_change_point_scan_rejects_nonfinite_derived_level_delta() -> None:
    rows = tuple(
        {
            "observation_key": index,
            "paid_amount": value,
        }
        for index, value in enumerate(
            (-1.0e308,) * 4 + (1.0e308,) * 4,
        )
    )

    with pytest.raises(
        ChangePointScanContractError,
        match="change_point_scan_derived_statistic_nonfinite:level_delta",
    ):
        _scan(rows)


def test_change_point_scan_rejects_nonfinite_derived_variance() -> None:
    rows = tuple(
        {
            "observation_key": index,
            "paid_amount": value,
        }
        for index, value in enumerate(
            (-1.0e308, 1.0e308) * 4,
        )
    )

    with pytest.raises(
        ChangePointScanContractError,
        match="change_point_scan_derived_statistic_nonfinite:left_variance",
    ):
        _scan(rows)


def test_change_point_scan_publishes_only_finite_derived_statistics() -> None:
    result = _scan(
        tuple(
            {
                "observation_key": index,
                "paid_amount": value,
            }
            for index, value in enumerate(
                (10.0, 11.0, 9.0, 10.0, 20.0, 19.0, 21.0, 20.0),
            )
        )
    )

    assert result.typed_payload["candidates"]
    for candidate in result.typed_payload["candidates"]:
        for field in (
            "left_mean",
            "right_mean",
            "level_delta",
            "relative_level_shift",
            "standardized_level_shift",
        ):
            value = candidate[field]
            assert value is None or isfinite(value)
