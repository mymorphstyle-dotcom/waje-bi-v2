from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[2]


def _run_projection(source: str) -> dict:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node executable is required")
    env = {**os.environ, "NODE_ENV": "test", "WAJE_GATEWAY_UNIT_TEST_STORE": "memory"}
    env.pop("WAJE_RUNTIME_DATABASE_URL", None)
    env.pop("DATABASE_URL", None)
    completed = subprocess.run(
        [
            node,
            "--no-warnings",
            "--experimental-loader=./tests/support/typescript-extension-loader.mjs",
            "--experimental-strip-types",
            "--input-type=module",
            "-e",
            source,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


BASE_SOURCE = """
const { projectCustomerAnalysisSnapshot, parseCustomerAnalysisSnapshot } = await import(
  "./app/api/_customerAnalysisContract.ts"
);
const clarification = {
  status: "waiting",
  question: "请选择比较基线",
  options: [
    { option_id: "previous", label: "前一天", description: "前一个完整自然日", recommended: true },
    { option_id: "rolling", label: "近 7 日均值", description: "此前七个完整自然日", recommended: false },
    { option_id: "tell_agent_differently", label: "其他", description: "自行说明", recommended: false },
  ],
};
const publication = {
  blocks: [
    { claim_refs: ["claim-1"], limitation_refs: [], material_fact_bindings: [
      { fact_kind: "number", name: "diagnostic_priority_score", range_end: null, unit: null, value: "0.7" },
    ], recommendation_refs: [], role: "executive_answer", statement_role: "executive_summary", text: "主要业务结论。" },
    { claim_refs: [], limitation_refs: ["limit-1"], material_fact_bindings: [], recommendation_refs: [], role: "boundary", statement_role: "limitations", text: "结论仅适用于当前数据范围。" },
  ],
  claim_refs: ["claim-1"], field_visibility_policy_ref: "safe", limitation_refs: ["limit-1"], recommendation_refs: [], visualization_refs: [], warnings: [],
};
const base = {
  thread: { id: "thread-handle", createdAt: "2026-07-20T00:00:00.000Z" },
  messages: [{ key: "message-key", role: "user", text: "为什么变化？", createdAt: "2026-07-20T00:00:01.000Z" }],
  runNodes: [], currentClarification: clarification, interactionResult: null,
  customerPublication: null, acceptedOperationIds: ["operation-1"],
  confirmedAt: "2026-07-20T00:00:02.000Z", stateVersion: "2000",
};
"""


def test_historical_clarification_cannot_regain_action_authority() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              run: { id: "run-handle", status: "completed", request: {
                post_execution_status: "completed", analysis_status: "complete",
                publication_status: "published", delivery_status: "published",
              }, createdAt: base.confirmedAt, updatedAt: base.confirmedAt },
              customerPublication: publication,
            });
            console.log(JSON.stringify({
              status: snapshot.state.status,
              hasInput: "input" in snapshot.state,
              actionHandle: snapshot.transport.actionHandle,
              parsedStatus: parseCustomerAnalysisSnapshot(snapshot).state.status,
            }));
            """
        )
    )
    assert result == {
        "status": "completed_with_limits",
        "hasInput": False,
        "actionHandle": None,
        "parsedStatus": "completed_with_limits",
    }


def test_only_current_waiting_state_creates_one_actionable_input() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              run: { id: "run-handle", status: "waiting_for_clarification", request: {}, createdAt: base.confirmedAt, updatedAt: base.confirmedAt },
            });
            console.log(JSON.stringify({
              status: snapshot.state.status,
              optionCount: snapshot.state.input.options.length,
              actionHandle: snapshot.transport.actionHandle,
              activeUpdates: snapshot.state.updates.filter((item) => item.status === "active").length,
            }));
            """
        )
    )
    assert result == {
        "status": "needs_input",
        "optionCount": 2,
        "actionHandle": "run-handle",
        "activeUpdates": 1,
    }


def test_replayed_old_clarification_does_not_stop_working_run() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              runNodes: [{ nodeName: "execute_capability_dag", status: "completed", confirmedAt: base.confirmedAt }],
              run: { id: "run-handle", status: "running_workflow", request: {}, createdAt: base.confirmedAt, updatedAt: base.confirmedAt },
            });
            console.log(JSON.stringify({ status: snapshot.state.status, phase: snapshot.state.phase, hasInput: "input" in snapshot.state }));
            """
        )
    )
    assert result == {"status": "working", "phase": "querying", "hasInput": False}


def test_persisted_provider_stage_advances_progress_past_stale_run_nodes() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              progressPhase: "synthesizing",
              runNodes: [{ nodeName: "understand_business_intent", status: "completed", confirmedAt: base.confirmedAt }],
              run: { id: "run-handle", status: "running_workflow", request: {}, createdAt: base.confirmedAt, updatedAt: base.confirmedAt },
            });
            console.log(JSON.stringify({
              status: snapshot.state.status,
              phase: snapshot.state.phase,
              updateLabels: snapshot.state.updates.map((item) => item.text),
              activeUpdates: snapshot.state.updates.filter((item) => item.status === "active").length,
            }));
            """
        )
    )
    assert result == {
        "status": "working",
        "phase": "synthesizing",
        "updateLabels": [
            "理解业务问题",
            "整理分析路径",
            "查询并分析数据",
            "汇总结论与边界",
        ],
        "activeUpdates": 1,
    }


def test_real_runtime_failure_is_not_projected_as_success() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              run: { id: "run-handle", status: "completed", request: {
                post_execution_status: "narrative_failed", analysis_status: "complete",
                publication_status: "not_ready", delivery_status: "pending",
              }, createdAt: base.confirmedAt, updatedAt: base.confirmedAt },
            });
            console.log(JSON.stringify({
              status: snapshot.state.status,
              title: snapshot.state.title,
              hasAnswer: "answer" in snapshot.state,
              failedUpdates: snapshot.state.updates.filter((item) => item.status === "failed").length,
              activeUpdates: snapshot.state.updates.filter((item) => item.status === "active").length,
            }));
            """
        )
    )
    assert result["status"] == "failed"
    assert result["title"] == "本次分析未完成"
    assert result["hasAnswer"] is False
    assert result["failedUpdates"] == 1
    assert result["activeUpdates"] == 0


def test_completed_answer_round_trips_with_customer_safe_blocks() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              run: { id: "run-handle", status: "completed", request: {
                post_execution_status: "completed", analysis_status: "complete",
                publication_status: "published", delivery_status: "published",
              }, createdAt: base.confirmedAt, updatedAt: base.confirmedAt },
              customerPublication: publication,
            });
            const restored = parseCustomerAnalysisSnapshot(JSON.parse(JSON.stringify(snapshot)));
            console.log(JSON.stringify({
              status: restored.state.status,
              kinds: restored.state.answer.blocks.map((block) => block.kind),
              hasFacts: "facts" in restored.state.answer,
              evidenceCount: restored.state.answer.evidenceCount,
              limitationCount: restored.state.answer.limitationCount,
            }));
            """
        )
    )
    assert result == {
        "status": "completed_with_limits",
        "kinds": ["summary", "limitation"],
        "hasFacts": False,
        "evidenceCount": 1,
        "limitationCount": 1,
    }
