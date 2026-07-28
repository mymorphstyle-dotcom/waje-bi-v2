from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from tools.phase7 import run_live_conversation_system_test as live


def test_live_acceptance_uses_split_liveness_and_readiness_contract() -> None:
    responses = {
        "/api/health": {
            "status": "ok",
            "checks": [
                {
                    "name": "frontend_gateway",
                    "status": "ok",
                    "detail": "route_responded",
                }
            ],
        },
        "/api/health?mode=readiness": {
            "status": "ok",
            "checks": [
                {
                    "name": "postgres_runtime_store",
                    "status": "ok",
                    "detail": "connection_verified",
                },
                {
                    "name": "runtime_configuration",
                    "status": "ok",
                    "detail": "required_configuration_present",
                },
            ],
        },
    }

    with (
        patch.object(
            live.gateway_once,
            "_json_request",
            side_effect=lambda _base_url, path, **_kwargs: responses[path],
        ),
        patch.object(
            live.ClickHouseRuntime,
            "from_env",
            return_value=SimpleNamespace(
                show_tables=lambda: SimpleNamespace(ok=True)
            ),
        ),
    ):
        health = live._dependency_health("http://127.0.0.1:3107", "user-one")

    assert health["overall_status"] == "ok"
    assert [item["dependency"] for item in health["checks"]] == [
        "gateway",
        "postgres",
        "clickhouse",
        "deepseek",
    ]


def test_customer_snapshot_is_matched_to_persisted_publication_without_refs() -> None:
    persisted = {
        "customer_publication": {
            "blocks": [
                {"role": "executive_answer", "text": "结论"},
                {"role": "boundary", "text": "边界"},
                ],
                "claim_refs": ["claim-one"],
                "limitation_refs": ["limit-one", "limit-two"],
                "warnings": [],
        }
    }
    projected = {
        "projection_kind": "customer_conversation_snapshot",
        "answer": {
            "blocks": [
                {"kind": "summary", "heading": "核心结论", "text": "结论"},
                {"kind": "limitation", "heading": "证据边界", "text": "边界"},
                ],
                "warnings": [],
                "evidenceCount": 1,
            "limitationCount": 2,
        },
    }

    assert live._customer_snapshot_matches_persisted_publication(
        projected,
        persisted,
    )


def test_customer_snapshot_rejects_changed_business_text() -> None:
    persisted = {
        "customer_publication": {
            "blocks": [{"role": "executive_answer", "text": "权威结论"}],
            "claim_refs": [],
            "limitation_refs": [],
        }
    }
    projected = {
        "projection_kind": "customer_conversation_snapshot",
        "answer": {
            "blocks": [{"kind": "summary", "text": "被改写的结论"}],
            "evidenceCount": 0,
            "limitationCount": 0,
        },
    }

    assert not live._customer_snapshot_matches_persisted_publication(
        projected,
        persisted,
    )
