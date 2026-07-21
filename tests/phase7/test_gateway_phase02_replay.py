from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_filesystem_plan_replay_route_is_removed() -> None:
    assert not (ROOT / "app/api/replays/route.ts").exists()

    app_source = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "app").rglob("*.ts*")
    )
    assert "WAJE_REPLAY_ARTIFACT_ROOT" not in app_source
    assert "plan_result.json" not in app_source
    assert 'from "fs/promises"' not in app_source


def test_workbench_uses_persisted_gateway_runs_only() -> None:
    route = (ROOT / "app/api/agent-runs/route.ts").read_text(encoding="utf-8")
    projection = (ROOT / "app/api/_customerRunProjection.ts").read_text(
        encoding="utf-8"
    )

    assert "listPersistedAgentRunCandidates" in route
    assert "Promise.all" not in route
    assert "traceRunFromCustomerPublication" in route
    assert "traceRunFromCustomerPublication" in projection
