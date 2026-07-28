from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_workbench_reads_parent_child_trace_inside_repeatable_read_snapshot() -> None:
    store = (ROOT / "app/api/_conversationStore.ts").read_text(
        encoding="utf-8"
    )
    route = (ROOT / "app/api/agent-runs/route.ts").read_text(encoding="utf-8")
    projection = (ROOT / "app/api/_customerRunProjection.ts").read_text(
        encoding="utf-8"
    )
    workbench = (
        ROOT / "app/agent-run-workbench/AgentRunWorkbench.tsx"
    ).read_text(encoding="utf-8")
    customer_route = (
        ROOT / "app/api/threads/[threadId]/route.ts"
    ).read_text(encoding="utf-8")

    assert "REPEATABLE READ READ ONLY" in store
    assert "controlled_investigation_operations" in store
    assert "controlled_investigation_dispatches" in store
    assert "'parentTransitionId'" in store
    assert "'childRunId'" in store
    assert "'acceptedAttemptRef'" in store
    assert "'acceptedArtifactRef'" in store
    assert "controlledInvestigation: row.controlledInvestigation" in route
    assert "controlledInvestigation" in projection
    assert "受控多 Agent 调查轨迹" in workbench
    assert "子调查只读取父级投影的来源闭包" in workbench
    assert "controlledChildStatusLabel" in workbench
    assert "JSON.stringify(run.controlledInvestigation" not in workbench
    assert "controlled_investigation_dispatches" not in customer_route
