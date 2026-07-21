from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bi_agent.runtime.authority_context_resolver import (
    resolve_latest_authority_context,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


class _ReleaseResolver:
    def __init__(self, releases: dict[str, object]) -> None:
        self.releases = releases

    def resolve_dataset_release(self, release_ref: str) -> object:
        return self.releases[release_ref]


def _member(
    snapshot_ref: str,
    *,
    dataset_id: str = "paid_order_success",
    evidence_state: str = "claim_ready",
) -> object:
    return SimpleNamespace(
        snapshot_ref=snapshot_ref,
        dataset_id=dataset_id,
        evidence_state=evidence_state,
    )


def _release(release_ref: str, *members: object) -> object:
    return SimpleNamespace(
        release_ref=release_ref,
        integrity_errors=(),
        member_projections=members,
    )


def _snapshot(
    snapshot_ref: str,
    release_ref: str,
    loaded_at: str,
    *,
    dataset_id: str = "paid_order_success",
    evidence_state: str = "claim_ready",
    status: str = "active",
) -> dict[str, object]:
    return {
        "snapshot_ref": snapshot_ref,
        "dataset_id": dataset_id,
        "release_ref": release_ref,
        "loaded_at": loaded_at,
        "status": status,
        "evidence_state": evidence_state,
    }


def _registry() -> RuntimeContractRegistry:
    return RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)


def test_latest_active_release_is_pinned_and_all_datasets_are_covered() -> None:
    snapshots = (
        _snapshot(
            "snapshot:paid-order:r1",
            "release:paid-order:r1",
            "2026-07-17T08:00:00Z",
        ),
        _snapshot(
            "snapshot:paid-order:r2",
            "release:paid-order:r2",
            "2026-07-18T08:00:00Z",
        ),
    )
    resolver = _ReleaseResolver(
        {
            "release:paid-order:r1": _release(
                "release:paid-order:r1", _member("snapshot:paid-order:r1")
            ),
            "release:paid-order:r2": _release(
                "release:paid-order:r2", _member("snapshot:paid-order:r2")
            ),
        }
    )

    context = resolve_latest_authority_context(
        run_attempt_id="run-phase02-authority",
        actual_as_of=datetime(2026, 7, 18, 9, tzinfo=timezone.utc),
        runtime_registry=_registry(),
        snapshot_records=snapshots,
        release_resolver=resolver,
    )

    assert context.release_refs == ("release:paid-order:r2",)
    assert context.snapshot_refs == ("snapshot:paid-order:r2",)
    assert {item["dataset_id"] for item in context.dataset_coverage} == set(
        _registry().dataset_ids
    )
    paid_order = next(
        item
        for item in context.dataset_coverage
        if item["dataset_id"] == "paid_order_success"
    )
    assert paid_order["availability"] == "claim_ready"
    payment_attempt = next(
        item
        for item in context.dataset_coverage
        if item["dataset_id"] == "payment_attempt"
    )
    assert payment_attempt["availability"] == "missing_contract"


def test_release_membership_must_be_active_as_one_authority_set() -> None:
    resolver = _ReleaseResolver(
        {
            "release:market:r1": _release(
                "release:market:r1",
                _member("snapshot:market:r1", dataset_id="market_dashboard"),
                _member(
                    "snapshot:market-channel:r1",
                    dataset_id="market_dashboard_channel",
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="release_not_active"):
        resolve_latest_authority_context(
            run_attempt_id="run-phase02-incomplete-release",
            actual_as_of=datetime(2026, 7, 18, 9, tzinfo=timezone.utc),
            runtime_registry=_registry(),
            snapshot_records=(
                _snapshot(
                    "snapshot:market:r1",
                    "release:market:r1",
                    "2026-07-18T08:00:00Z",
                    dataset_id="market_dashboard",
                ),
            ),
            release_resolver=resolver,
        )
