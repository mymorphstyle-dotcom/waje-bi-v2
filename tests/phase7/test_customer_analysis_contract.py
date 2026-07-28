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
  questions: [{
    slot_id: "comparison_baseline",
    question: "请选择比较基线",
    recommendation_reason: "前一天最接近目标日。",
    options: [
      { option_id: "previous", label: "前一天", description: "前一个完整自然日", recommended: true },
      { option_id: "rolling", label: "近 7 日均值", description: "此前七个完整自然日", recommended: false },
      { option_id: "tell_agent_differently", label: "其他", description: "自行说明", recommended: false },
    ],
  }],
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


def test_business_understanding_requires_an_accepted_intent_revision_binding() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const unbound = projectCustomerAnalysisSnapshot({
              ...base,
              run: {
                id: "run-unbound", status: "running_workflow",
                request: {
                  business_understanding: "这仍是会话入口的临时理解。",
                },
                createdAt: base.confirmedAt, updatedAt: base.confirmedAt,
              },
            });
            const bound = projectCustomerAnalysisSnapshot({
              ...base,
              run: {
                id: "run-bound", status: "running_workflow",
                request: {
                  business_understanding: "你希望分析全量样本中月初付费金额是否稳定高于月中和月末。",
                  business_understanding_intent_revision_id: "intent-revision-accepted",
                },
                createdAt: base.confirmedAt, updatedAt: base.confirmedAt,
              },
            });
            console.log(JSON.stringify({
              unbound: unbound.businessUnderstanding,
              bound: bound.businessUnderstanding,
              restored: parseCustomerAnalysisSnapshot(bound).businessUnderstanding,
            }));
            """
        )
    )
    assert result == {
        "unbound": None,
        "bound": "你希望分析全量样本中月初付费金额是否稳定高于月中和月末。",
        "restored": "你希望分析全量样本中月初付费金额是否稳定高于月中和月末。",
    }


def test_planner_issues_require_the_accepted_plan_binding() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const request = {
              plan_result_refs: {
                plan_revision_id: "plan-revision-accepted",
                planner_proposal_id: "planner-proposal-accepted",
              },
              planner_problem_projection: {
                schema_version: "planner-problem-projection.v1",
                plan_revision_id: "plan-revision-accepted",
                planner_proposal_id: "planner-proposal-accepted",
                issues: [
                  { issue_id: "root", parent_issue_id: null, question: "完成本轮分析" },
                  { issue_id: "pattern", parent_issue_id: "root", question: "验证月初是否稳定更高" },
                  { issue_id: "quality", parent_issue_id: "root", question: "检查数据质量边界" },
                ],
              },
              query_bundle_projection: {
                schema_version: "query-bundle-projection.v1",
                query_bundle_ref: "query-bundle-accepted",
                stage: "compiled",
                plan_revision_id: "plan-revision-accepted",
                planner_proposal_id: "planner-proposal-accepted",
                issues: [
                  {
                    issue_id: "root", question: "完成本轮分析",
                    status: "querying", status_message: "查询中",
                    query_ir_ref: "query-ir-root", repair_actions: [],
                  },
                  {
                    issue_id: "pattern", question: "验证月初是否稳定更高",
                    status: "evidenced", status_message: "已有证据",
                    query_ir_ref: "query-ir-pattern", repair_actions: [],
                  },
                  {
                    issue_id: "quality", question: "检查数据质量边界",
                    status: "limited", status_message: "有边界",
                    query_ir_ref: "query-ir-quality",
                    repair_actions: ["select_available_capability_route"],
                  },
                ],
              },
            };
            const accepted = projectCustomerAnalysisSnapshot({
              ...base,
              run: {
                id: "run-accepted", status: "running_workflow", request,
                createdAt: base.confirmedAt, updatedAt: base.confirmedAt,
              },
            });
            const stale = projectCustomerAnalysisSnapshot({
              ...base,
              run: {
                id: "run-stale", status: "running_workflow",
                request: {
                  ...request,
                  plan_result_refs: {
                    ...request.plan_result_refs,
                    plan_revision_id: "plan-revision-new",
                  },
                },
                createdAt: base.confirmedAt, updatedAt: base.confirmedAt,
              },
            });
            console.log(JSON.stringify({
              accepted: accepted.plannerIssues,
              acceptedStates: accepted.plannerIssueStates,
              stale: stale.plannerIssues,
              staleStates: stale.plannerIssueStates,
              restored: parseCustomerAnalysisSnapshot(accepted).plannerIssues,
              restoredStates: parseCustomerAnalysisSnapshot(accepted).plannerIssueStates,
            }));
            """
        )
    )
    assert result == {
        "accepted": [
            "完成本轮分析",
            "验证月初是否稳定更高",
            "检查数据质量边界",
        ],
        "acceptedStates": [
            {
                "question": "完成本轮分析",
                "status": "querying",
                "statusLabel": "查询中",
            },
            {
                "question": "验证月初是否稳定更高",
                "status": "evidenced",
                "statusLabel": "已有证据",
            },
            {
                "question": "检查数据质量边界",
                "status": "limited",
                "statusLabel": "有边界",
            },
        ],
        "stale": [],
        "staleStates": [],
        "restored": [
            "完成本轮分析",
            "验证月初是否稳定更高",
            "检查数据质量边界",
        ],
        "restoredStates": [
            {
                "question": "完成本轮分析",
                "status": "querying",
                "statusLabel": "查询中",
            },
            {
                "question": "验证月初是否稳定更高",
                "status": "evidenced",
                "statusLabel": "已有证据",
            },
            {
                "question": "检查数据质量边界",
                "status": "limited",
                "statusLabel": "有边界",
            },
        ],
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


def test_explicit_phase_stop_projects_a_terminal_checkpoint() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const stopped = projectCustomerAnalysisSnapshot({
              ...base,
              runNodes: [{
                nodeName: "compile_authoritative_plan",
                status: "completed",
                confirmedAt: base.confirmedAt,
              }],
              run: {
                id: "run-stopped",
                status: "planned",
                request: { stop_after_phase: "phase02" },
                createdAt: base.confirmedAt,
                updatedAt: base.confirmedAt,
              },
            });
            const continuing = projectCustomerAnalysisSnapshot({
              ...base,
              run: {
                id: "run-continuing",
                status: "planned",
                request: {},
                createdAt: base.confirmedAt,
                updatedAt: base.confirmedAt,
              },
            });
            console.log(JSON.stringify({
              stopped: {
                status: stopped.state.status,
                phase: stopped.state.phase,
                title: stopped.state.title,
                safeToClose: stopped.state.safeToClose,
                updateStatuses: stopped.state.updates.map((item) => item.status),
                parsedStatus: parseCustomerAnalysisSnapshot(stopped).state.status,
              },
              continuing: {
                status: continuing.state.status,
                phase: continuing.state.phase,
              },
            }));
            """
        )
    )
    assert result == {
        "stopped": {
            "status": "checkpoint",
            "phase": "planning",
            "title": "分析计划已确认",
            "safeToClose": True,
            "updateStatuses": ["completed", "completed"],
            "parsedStatus": "checkpoint",
        },
        "continuing": {
            "status": "working",
            "phase": "planning",
        },
    }


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
              headings: restored.state.answer.blocks.map((block) => block.heading),
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
        "headings": ["核心结论", "证据边界"],
        "hasFacts": False,
        "evidenceCount": 1,
        "limitationCount": 1,
    }


def test_direct_agent_response_uses_thread_terminal_without_bi_run() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              messages: [
                ...base.messages,
                {
                  key: "assistant-1", role: "assistant", text: "这是直接回复。",
                  createdAt: base.confirmedAt, itemType: "assistant_message",
                  operationKey: "assistant:operation-direct",
                },
              ],
              run: null,
              agentHead: { status: "completed", activeTaskRef: null, pendingActionRef: null },
              agentTerminal: {
                status: "completed",
                finalOutput: { answerMarkdown: "这是直接回复。", materialRefs: [], limitationRefs: [] },
                errorCode: null,
                completionKind: "direct_response",
                durableTaskRef: null,
                operationId: "operation-direct",
              },
              eventCursor: "2100",
              latestItemSequence: 3,
            });
            const parsed = parseCustomerAnalysisSnapshot(snapshot);
            console.log(JSON.stringify({
              status: parsed.state.status,
              answer: parsed.state.answer.blocks[0].text,
              heading: parsed.state.answer.blocks[0].heading,
              runHandle: parsed.transport.runHandle,
              eventsUrl: parsed.transport.eventsUrl,
              eventCursor: parsed.transport.eventCursor,
              latestItemSequence: parsed.transport.latestItemSequence,
            }));
            """
        )
    )
    assert result == {
        "status": "completed",
        "answer": "这是直接回复。",
        "heading": None,
        "runHandle": None,
        "eventsUrl": "/api/threads/thread-handle/events",
        "eventCursor": "2100",
        "latestItemSequence": 3,
    }


def test_agent_owned_progress_and_terminal_items_have_one_customer_surface() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              messages: [
                ...base.messages,
                {
                  key: "progress-1", role: "assistant",
                  text: "BI 分析任务已进入持久化执行队列。",
                  createdAt: base.confirmedAt, itemType: "progress",
                  operationKey: "assistant-suspension:operation-1",
                },
                {
                  key: "assistant-1", role: "assistant", text: "这是直接回复。",
                  createdAt: base.confirmedAt, itemType: "assistant_message",
                  operationKey: "assistant:operation-2",
                },
              ],
              run: null,
              agentHead: { status: "completed", activeTaskRef: null, pendingActionRef: null },
              agentTerminal: {
                status: "completed",
                finalOutput: { answerMarkdown: "这是直接回复。", materialRefs: [], limitationRefs: [] },
                errorCode: null,
                completionKind: "direct_response",
                durableTaskRef: null,
                operationId: "operation-2",
              },
            });
            console.log(JSON.stringify({
              messageKeys: snapshot.messages.map((message) => message.key),
              answer: snapshot.state.answer.blocks.map((block) => block.text),
              hasInternalItemType: JSON.stringify(snapshot).includes("itemType"),
              hasOperationKey: JSON.stringify(snapshot).includes("operationKey"),
            }));
            """
        )
    )
    assert result == {
        "messageKeys": ["message-key"],
        "answer": ["这是直接回复。"],
        "hasInternalItemType": False,
        "hasOperationKey": False,
    }


def test_analysis_terminal_restores_exact_publication_blocks_and_checks_task_binding() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const source = {
              ...base,
              messages: [
                ...base.messages,
                {
                  key: "assistant-analysis", role: "assistant",
                  text: "被持久化 publication 替代的扁平文本。",
                  createdAt: base.confirmedAt, itemType: "assistant_message",
                  operationKey: "assistant:operation-analysis",
                },
              ],
              run: {
                id: "run-bi", status: "completed", request: {},
                createdAt: base.confirmedAt, updatedAt: base.confirmedAt,
              },
              customerPublication: publication,
              customerPublicationTaskRef: "run-bi",
              agentHead: {
                status: "completed_with_limits",
                activeTaskRef: null,
                pendingActionRef: null,
              },
              agentTerminal: {
                status: "completed_with_limits",
                finalOutput: {
                  answerMarkdown: "被持久化 publication 替代的扁平文本。",
                  materialRefs: ["publication-ref"],
                  limitationRefs: ["limit-1"],
                },
                errorCode: null,
                completionKind: "analysis_publication",
                durableTaskRef: "run-bi",
                operationId: "operation-analysis",
              },
            };
            const snapshot = projectCustomerAnalysisSnapshot(source);
            let mismatch = "accepted";
            try {
              projectCustomerAnalysisSnapshot({
                ...source,
                customerPublicationTaskRef: "run-other",
              });
            } catch (error) {
              mismatch = error instanceof Error ? error.message : "unknown";
            }
            console.log(JSON.stringify({
              status: snapshot.state.status,
              kinds: snapshot.state.answer.blocks.map((block) => block.kind),
              headings: snapshot.state.answer.blocks.map((block) => block.heading),
              texts: snapshot.state.answer.blocks.map((block) => block.text),
              messageKeys: snapshot.messages.map((message) => message.key),
              mismatch,
            }));
            """
        )
    )
    assert result == {
        "status": "completed_with_limits",
        "kinds": ["summary", "limitation"],
        "headings": ["核心结论", "证据边界"],
        "texts": ["主要业务结论。", "结论仅适用于当前数据范围。"],
        "messageKeys": ["message-key"],
        "mismatch": "customer_agent_publication_binding_invalid",
    }


def test_agent_pending_action_projects_only_customer_safe_options() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              run: null,
              agentHead: {
                status: "needs_input",
                activeTaskRef: "agent-run-1",
                pendingActionRef: "pending-action:1",
              },
              pendingAction: {
                actionRef: "pending-action:1",
                actionType: "ask_user",
                prompt: "请选择比较口径",
                options: [
                  { optionId: "recommended", label: "采用推荐口径", description: "继续执行", recommended: true },
                  { optionId: "custom", label: "调整口径", description: "说明不同要求", recommended: false },
                ],
              },
            });
            console.log(JSON.stringify({
              status: snapshot.state.status,
              actionHandle: snapshot.transport.actionHandle,
              optionKeys: snapshot.state.input.options.map((item) => item.optionKey),
              hasPayload: "pendingAction" in snapshot,
            }));
            """
        )
    )
    assert result == {
        "status": "needs_input",
        "actionHandle": "pending-action:1",
        "optionKeys": ["recommended", "custom"],
        "hasPayload": False,
    }


def test_new_agent_turn_is_not_masked_by_previous_completed_bi_run() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              run: {
                id: "run-previous-bi",
                status: "completed",
                request: {
                  post_execution_status: "completed",
                  publication_status: "published",
                  delivery_status: "published",
                },
                createdAt: base.confirmedAt,
                updatedAt: base.confirmedAt,
              },
              customerPublication: publication,
              agentHead: {
                status: "working",
                activeTaskRef: "agent-run-current",
                pendingActionRef: null,
              },
            });
            console.log(JSON.stringify({
              status: snapshot.state.status,
              runHandle: snapshot.transport.runHandle,
              title: snapshot.state.title,
            }));
            """
        )
    )
    assert result == {
        "status": "working",
        "runHandle": "agent-run-current",
        "title": "正在处理当前请求",
    }


def test_recoverable_agent_failure_is_visible_while_thread_head_accepts_input() -> None:
    result = _run_projection(
        textwrap.dedent(
            BASE_SOURCE
            + """
            const snapshot = projectCustomerAnalysisSnapshot({
              ...base,
              messages: [
                ...base.messages,
                {
                  key: "assistant-failed", role: "assistant",
                  text: "当前请求暂时未能完成，请稍后重试。",
                  createdAt: base.confirmedAt, itemType: "assistant_message",
                  operationKey: "assistant:operation-failed",
                },
              ],
              run: null,
              agentHead: {
                status: "idle",
                activeTaskRef: null,
                pendingActionRef: null,
              },
              agentTerminal: {
                status: "failed",
                finalOutput: null,
                errorCode: "provider_temporary_failure",
                completionKind: "failed_turn",
                durableTaskRef: null,
                operationId: "operation-failed",
              },
            });
            const parsed = parseCustomerAnalysisSnapshot(snapshot);
            console.log(JSON.stringify({
              status: parsed.state.status,
              recovery: parsed.state.recovery,
              description: parsed.state.description,
              runHandle: parsed.transport.runHandle,
              hasErrorCode: JSON.stringify(parsed).includes("provider_temporary_failure"),
            }));
            """
        )
    )
    assert result == {
        "status": "failed",
        "recovery": "retry",
        "description": "当前请求暂时未能完成，请稍后重试。",
        "runHandle": None,
        "hasErrorCode": False,
    }
