import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[2]


def test_general_agent_trace_projects_model_tool_error_and_full_technical_records() -> None:
    result = _run_typescript(
        """
        import { traceRunFromRuntimeState } from "./app/api/_customerRunProjection.ts";

        const records = [{
          event_type: "span_finished",
          span_data: { type: "generation", model: "deepseek-chat" },
          error: null,
        }, {
          event_type: "span_finished",
          span_data: { type: "function", name: "run_bi_analysis" },
          error: { message: "tool failed" },
        }];
        const run = traceRunFromRuntimeState({
          id: "run:agent-run-test",
          label: "General Agent test",
          runId: "agent-run-test",
          runStatus: "completed_with_limits",
          question: "分析付费金额变化",
          request: {
            runtime_kind: "general_agent",
            interaction_result: { response_text: "分析已完成。" },
          },
          runNodes: [{
            node_name: "general_agent_model_turn",
            label: "大陆模型推理",
            summary: "模型 deepseek-chat 完成一次 Runner 推理。",
            owner: "LLM",
            status: "completed",
          }, {
            node_name: "general_agent_tool_call",
            label: "工具调用 · run_bi_analysis",
            summary: "Runner 调用工具失败。",
            owner: "本地系统",
            status: "failed",
          }],
          llmCallCount: 1,
          technicalTrace: {
            runtime: "general_agent",
            provider: "deepseek",
            model: "deepseek-chat",
            transport: "chat_completions",
            records,
          },
          createdAt: "2026-07-22T00:00:00.000Z",
          updatedAt: "2026-07-22T00:00:01.000Z",
        });
        console.log(JSON.stringify(run));
        """
    )

    assert result["runOutcome"] == "completed"
    assert result["processSummary"]["llmCallCount"] == 1
    assert result["traceCompleteness"]["llmCalls"] == "known"
    assert result["processSummary"]["nodes"][0]["owner"] == "LLM"
    assert result["processSummary"]["nodes"][1]["outcome"] == "failed"
    assert result["technicalTrace"]["provider"] == "deepseek"
    assert result["technicalTrace"]["records"][1]["error"]["message"] == "tool failed"


def test_customer_publication_projects_authoritative_replay_fields() -> None:
    result = _run_typescript(
        """
        import { traceRunFromCustomerPublication } from "./app/api/_customerRunProjection.ts";

        const digest = "a".repeat(64);
        const claimRef = `claim:authority:sha256:${digest}`;
        const evidenceRef = "evidence:task:main";
        const run = traceRunFromCustomerPublication({
          blocks: [{
            claim_refs: [claimRef],
            limitation_refs: [],
            material_fact_bindings: [],
            recommendation_refs: [],
            role: "summary",
            statement_role: "main_conclusion",
            text: "付费金额上升。",
          }],
          claim_refs: [claimRef],
          field_visibility_policy_ref: "visibility-policy:test",
          limitation_refs: [],
          recommendation_refs: [],
          visualization_refs: [],
          warnings: [],
        }, {
          authority_bundle_ref: "authority:test",
          authority_bundle_digest: digest,
          authority_sealed_at: "2026-07-20T00:00:00.950Z",
          publication_ref: "publication:test",
          publication_digest: digest,
          published_at: "2026-07-20T00:00:01.500Z",
          projection_id: "projection:test",
          projection_digest: digest,
          outbox_ref: "outbox:test",
          delivery_status: "published",
          delivery_attempted_at: "2026-07-20T00:00:01.600Z",
        }, {
          id: "persisted:run-test",
          label: "测试运行",
          runId: "run-test",
          runStatus: "completed",
          question: "为什么上涨？",
          request: {
            analysis_status: "complete",
            post_execution_status: "completed",
            publication_status: "published",
            delivery_status: "published",
          },
          workflowTransitions: [
            {
              attempt_id: "attempt-bind",
              node_name: "bind_intent",
              provider_ref: "openai",
              status: "succeeded",
              acceptance_state: "accepted",
              next_transition: "execute_capability_dag",
            },
            {
              attempt_id: "attempt-execute",
              node_name: "execute_capability_dag",
              provider_ref: "waje-capability-runtime",
              status: "succeeded",
              acceptance_state: "accepted",
              next_transition: "settle_claim_authority",
              execution_snapshot_ref: "snapshot:active",
              execution_plan_revision_id: "plan:active",
              execution_evidence_entry_refs: ["entry:main"],
            },
            {
              attempt_id: "attempt-settle",
              node_name: "settle_claim_authority",
              provider_ref: "waje-semantic-authority",
              status: "succeeded",
              acceptance_state: "accepted",
              next_transition: "compose_claim_aware_narrative",
            },
            {
              attempt_id: "attempt-compose",
              node_name: "compose_claim_aware_narrative",
              provider_ref: "waje-narrative-authority",
              status: "succeeded",
              acceptance_state: "accepted",
              next_transition: "publish_customer_projection",
            },
          ],
          stageTimings: [
            {
              transition_attempt_id: "attempt-bind",
              stage_name: "bind_intent",
              started_at: "2026-07-20T00:00:00.100Z",
              finished_at: "2026-07-20T00:00:00.400Z",
              accepted_call_count: 1,
              llm_call_count: 1,
              control_call_count: 0,
              query_call_count: 0,
              capability_call_count: 0,
            },
            {
              transition_attempt_id: "attempt-execute",
              stage_name: "execute_capability_dag",
              started_at: "2026-07-20T00:00:00.410Z",
              finished_at: "2026-07-20T00:00:00.490Z",
              accepted_call_count: 2,
              llm_call_count: 0,
              control_call_count: 0,
              query_call_count: 1,
              capability_call_count: 1,
            },
            {
              transition_attempt_id: "attempt-settle",
              stage_name: "settle_claim_authority",
              started_at: "2026-07-20T00:00:00.500Z",
              finished_at: "2026-07-20T00:00:00.900Z",
              accepted_call_count: 2,
              llm_call_count: 2,
              control_call_count: 0,
              query_call_count: 0,
              capability_call_count: 0,
            },
            {
              transition_attempt_id: "attempt-compose",
              stage_name: "compose_claim_aware_narrative",
              started_at: "2026-07-20T00:00:01.000Z",
              finished_at: "2026-07-20T00:00:01.400Z",
              accepted_call_count: 1,
              llm_call_count: 1,
              control_call_count: 0,
              query_call_count: 0,
              capability_call_count: 0,
            },
          ],
          evidenceRefs: [{
            entry_ref: "entry:main",
            evidence_ref: evidenceRef,
            plan_revision_id: "plan:active",
            plan_state: "active",
            task_id: "task:main",
            binding_state: "bound",
            execution_transition_attempt_id: "attempt-execute",
            capability_id: "metric_timeseries",
            evidence_kind: "observed",
            execution_state: "available",
            data_contract_state: "complete",
            maximum_claim_strength: "directional",
            limitation_refs: [],
          }],
          claimEvidenceLinks: [{
            claim_ref: claimRef,
            claim_class: "comparative_change",
            claim_status: "verified",
            evidence_refs: [evidenceRef],
          }],
          acceptedGraph: [{
            task_id: "task:main",
            plan_revision_id: "plan:active",
            capability_id: "metric_timeseries",
            task_key: "metric_timeseries:main",
            execution_state: "settled",
            outcome_ref: "outcome:main",
            status: "succeeded",
            retryability: "never",
            limitation_refs: [],
          }],
          verifierStatus: {
            acceptedClaimCount: 1,
            vetoedClaimCount: 0,
            acceptedBlockCount: 1,
            rejectedBlockCount: 1,
            vetoedBlockCount: 1,
            claimReportRefs: ["claim-report:test"],
            blockReportRefs: ["block-report:test"],
            verifiedAt: "2026-07-20T00:00:00.940Z",
          },
          humanReview: {
            status: "revision_requested",
            evaluationCount: 2,
            latest: {
              reviewerRef: "reviewer:finance-ops",
              scores: {
                explanation_value: 4,
                novelty: 3,
                decision_usefulness: 5,
                competing_hypotheses: 2,
                uncertainty_handling: 4,
                actionability: 5,
              },
              humanReasons: {
                explanation_value: "解释清楚。",
                novelty: "有增量信息。",
                decision_usefulness: "可支持决策。",
                competing_hypotheses: "需要补充竞争解释。",
                uncertainty_handling: "边界明确。",
                actionability: "行动建议具体。",
              },
              result: "request_independent_narrative_attempt",
              reviewedAt: "2026-07-20T00:00:01.900Z",
            },
          },
          createdAt: "2026-07-20T00:00:00.000Z",
          updatedAt: "2026-07-20T00:00:02.000Z",
        });
        console.log(JSON.stringify(run));
        """
    )

    assert result["runOutcome"] == "published"
    assert result["runMode"] == "event_replay"
    assert result["lifecycle"]["verifier"] == {
        "outcome": "complete",
        "status": "findings",
        "completeness": "known",
        "detail": "客户投影回答块 1 · 核验通过回答块 1 · 核验发现回答块 1 · 语义风险记录 1 · 有证据支持结论 1 · 结论风险记录 0 · 后台报告 2",
        "at": "2026-07-20T00:00:00.940Z",
    }
    assert result["lifecycle"]["publication"]["status"] == "published"
    assert result["lifecycle"]["delivery"]["status"] == "published"
    assert result["humanReview"]["status"] == "revision_requested"
    assert result["humanReview"]["evaluationCount"] == 2
    assert result["humanReview"]["latest"]["scores"]["decision_usefulness"] == 5
    assert result["timing"] == {
        "actualDurationMs": 2000,
        "completeness": "known",
    }
    assert result["processSummary"]["llmCallCount"] == 4
    assert result["processSummary"]["acceptedGraph"] == [{
        "taskId": "task:main",
        "planRevisionId": "plan:active",
        "capabilityId": "metric_timeseries",
        "taskKey": "metric_timeseries:main",
        "execution": {
            "state": "settled",
            "outcomeRef": "outcome:main",
            "status": "succeeded",
            "retryability": "never",
            "limitationRefs": [],
        },
    }]
    owners = {
        node["node"]: node["owner"] for node in result["processSummary"]["nodes"]
    }
    assert owners["bind_intent"] == "LLM"
    assert owners["execute_capability_dag"] == "本地系统"
    assert owners["settle_claim_authority"] == "混合"
    assert owners["compose_claim_aware_narrative"] == "混合"
    assert result["processSummary"]["nodes"][0]["route"] == "execute_capability_dag"
    assert all("audit" not in node for node in result["processSummary"]["nodes"])
    assert result["traceClaims"] == [
        {
            "claimRef": f"claim:authority:sha256:{'a' * 64}",
            "claimClass": "comparative_change",
            "status": "verified",
            "text": "付费金额上升。",
            "evidenceRefs": ["evidence:task:main"],
        }
    ]
    assert result["traceEvidence"][0]["executionState"] == "available"
    assert result["traceEvidence"][0]["taskId"] == "task:main"
    assert result["traceEvidence"][0]["limitationsCompleteness"] == "known"
    assert set(result["traceCompleteness"].values()) == {"known"}
    assert not {
        "sourceRef",
        "debugStage",
        "debugAudit",
    }.intersection(result["processSummary"])


def test_case_sized_projection_closes_graph_claim_evidence_and_real_timing() -> None:
    result = _run_typescript(
        """
        import { traceRunFromCustomerPublication } from "./app/api/_customerRunProjection.ts";

        const digest = "b".repeat(64);
        const claimRefs = Array.from({ length: 5 }, (_, index) => `claim:test:${index}`);
        const capabilities = Array.from({ length: 12 }, (_, index) => `capability_${index}`);
        const evidenceRefs = Array.from({ length: 15 }, (_, index) => `evidence:test:${index}`);
        const evidenceEntryRefs = Array.from({ length: 15 }, (_, index) => `entry:test:${index}`);
        const transitions = [
          ["attempt-entry", "conversation_entry", "system", "2026-07-20T00:00:00.000Z"],
          ["attempt-bind", "bind_intent", "system", "2026-07-20T00:00:01.000Z"],
          ["attempt-clarify", "generate_clarification", "system", "2026-07-20T00:00:02.000Z"],
          ["attempt-wait", "persist_waiting_for_decision", "system", "2026-07-20T00:00:01.600Z"],
          ["attempt-decide", "accept_material_decision", "user_protocol", "2026-07-20T00:00:01.700Z"],
          ["attempt-plan", "compile_authoritative_plan", "system", "2026-07-20T00:00:05.000Z"],
          ["attempt-execute", "execute_capability_dag", "system", "2026-07-20T00:00:06.000Z"],
          ["attempt-coverage", "evaluate_claim_coverage", "local_deterministic", "2026-07-20T00:00:03.600Z"],
          ["attempt-settle", "settle_claim_authority", "system", "2026-07-20T00:00:08.000Z"],
          ["attempt-compose", "compose_claim_aware_narrative", "system", "2026-07-20T00:00:09.000Z"],
        ].map(([attemptId, nodeName, providerRef, at], index, values) => ({
          attempt_id: attemptId,
          node_name: nodeName,
          provider_ref: providerRef,
          status: "succeeded",
          acceptance_state: "accepted",
          next_transition: values[index + 1]?.[1] ?? "publish_customer_projection",
          started_at: [3, 4, 7].includes(index) ? at : undefined,
          finished_at: [3, 4, 7].includes(index) ? at : undefined,
          ...(nodeName === "execute_capability_dag" ? {
            execution_snapshot_ref: "snapshot:active",
            execution_plan_revision_id: "plan:active",
            execution_evidence_entry_refs: evidenceEntryRefs,
          } : {}),
        }));
        const timing = (attemptId, stageName, startedAt, finishedAt, counts) => ({
          transition_attempt_id: attemptId,
          stage_name: stageName,
          started_at: startedAt,
          finished_at: finishedAt,
          accepted_call_count: counts.llm + counts.query + counts.capability,
          llm_call_count: counts.llm,
          control_call_count: 0,
          query_call_count: counts.query,
          capability_call_count: counts.capability,
        });
        const run = traceRunFromCustomerPublication({
          blocks: claimRefs.map((claimRef, index) => ({
            claim_refs: [claimRef],
            limitation_refs: [],
            material_fact_bindings: [],
            recommendation_refs: [],
            role: "analysis",
            statement_role: "conclusion",
            text: `客户可见结论 ${index + 1}`,
          })),
          claim_refs: claimRefs,
          field_visibility_policy_ref: "visibility-policy:test",
          limitation_refs: [],
          recommendation_refs: [],
          visualization_refs: [],
          warnings: [],
        }, {
          authority_bundle_ref: "authority:test",
          authority_bundle_digest: digest,
          authority_sealed_at: "2026-07-20T00:00:05.100Z",
          publication_ref: "publication:test",
          publication_digest: digest,
          published_at: "2026-07-20T00:00:07.100Z",
          projection_id: "projection:test",
          projection_digest: digest,
          outbox_ref: "outbox:test",
          delivery_status: "published",
          delivery_attempted_at: "2026-07-20T00:00:07.200Z",
        }, {
          id: "persisted:run-test",
          label: "测试运行",
          runId: "run-test",
          runStatus: "completed",
          question: "为什么上涨？",
          request: {
            analysis_status: "complete",
            post_execution_status: "completed",
            publication_status: "published",
            delivery_status: "published",
          },
          workflowTransitions: transitions,
          stageTimings: [
            timing("attempt-entry", "conversation_entry", "2026-07-20T00:00:00.100Z", "2026-07-20T00:00:00.500Z", { llm: 1, query: 0, capability: 0 }),
            timing("attempt-bind", "bind_intent", "2026-07-20T00:00:00.600Z", "2026-07-20T00:00:01.000Z", { llm: 1, query: 0, capability: 0 }),
            timing("attempt-clarify", "generate_clarification", "2026-07-20T00:00:01.100Z", "2026-07-20T00:00:01.500Z", { llm: 1, query: 0, capability: 0 }),
            timing("attempt-plan", "compile_authoritative_plan", "2026-07-20T00:00:01.800Z", "2026-07-20T00:00:02.500Z", { llm: 1, query: 0, capability: 0 }),
            timing("attempt-execute", "execute_capability_dag", "2026-07-20T00:00:02.600Z", "2026-07-20T00:00:03.500Z", { llm: 0, query: 2, capability: 1 }),
            timing("attempt-settle", "settle_claim_authority", "2026-07-20T00:00:03.700Z", "2026-07-20T00:00:05.000Z", { llm: 4, query: 0, capability: 0 }),
            timing("attempt-compose", "compose_claim_aware_narrative", "2026-07-20T00:00:05.200Z", "2026-07-20T00:00:07.000Z", { llm: 2, query: 0, capability: 0 }),
          ],
          evidenceRefs: evidenceRefs.map((evidenceRef, index) => ({
            entry_ref: evidenceEntryRefs[index],
            evidence_ref: evidenceRef,
            plan_revision_id: "plan:active",
            plan_state: "active",
            task_id: `task:${index % capabilities.length}`,
            binding_state: "bound",
            execution_transition_attempt_id: "attempt-execute",
            capability_id: capabilities[index % capabilities.length],
            evidence_kind: "observed",
            execution_state: "available",
            data_contract_state: "complete",
            maximum_claim_strength: "directional",
            limitation_refs: [],
          })),
          claimEvidenceLinks: claimRefs.map((claimRef, index) => ({
            claim_ref: claimRef,
            claim_class: "comparative_change",
            claim_status: "verified",
            evidence_refs: [evidenceRefs[index]],
          })),
          acceptedGraph: capabilities.map((capabilityId, index) => ({
            task_id: `task:${index}`,
            plan_revision_id: "plan:active",
            capability_id: capabilityId,
            task_key: `task_key_${index}`,
            execution_state: "settled",
            outcome_ref: `outcome:${index}`,
            status: index === 4 || index === 6 ? "unavailable" : index === 8 ? "integrity_failed" : "succeeded",
            retryability: index === 4 || index === 6 || index === 8 ? "replan_required" : "never",
            limitation_refs: index === 4 || index === 6 || index === 8 ? [`limitation:${index}`] : [],
            ...(index === 8 ? { failure: {
              layer: "query",
              kind: "query_result_contract_invalid",
              integrity_level: "task",
              business_boundary: "market_channel_context_evidence_unpublishable",
            } } : {}),
          })),
          verifierStatus: {
            acceptedClaimCount: 19,
            vetoedClaimCount: 2,
            acceptedBlockCount: 5,
            rejectedBlockCount: 0,
            vetoedBlockCount: 0,
            claimReportRefs: ["claim-report:test"],
            blockReportRefs: ["block-report:test"],
            verifiedAt: "2026-07-20T00:00:07.050Z",
          },
          createdAt: "2026-07-20T00:00:00.000Z",
          updatedAt: "2026-07-20T00:00:12.000Z",
        });
        console.log(JSON.stringify(run));
        """
    )

    assert result["runOutcome"] == "published"
    assert result["runMode"] == "event_replay"
    assert result["lifecycle"]["verifier"]["status"] == "recorded"
    assert result["lifecycle"]["verifier"]["at"] == "2026-07-20T00:00:07.050Z"
    assert result["processSummary"]["llmCallCount"] == 10
    nodes = result["processSummary"]["nodes"]
    assert [node["node"] for node in nodes] == [
        "conversation_entry",
        "bind_intent",
        "generate_clarification",
        "persist_waiting_for_decision",
        "accept_material_decision",
        "compile_authoritative_plan",
        "execute_capability_dag",
        "evaluate_claim_coverage",
        "settle_claim_authority",
        "seal_authority_bundle",
        "compose_claim_aware_narrative",
        "publish_customer_projection",
        "deliver_publication",
    ]
    by_name = {node["node"]: node for node in nodes}
    assert by_name["compose_claim_aware_narrative"]["finishedAt"] == "2026-07-20T00:00:07.000Z"
    assert by_name["publish_customer_projection"]["startedAt"] == "2026-07-20T00:00:07.100Z"
    assert by_name["settle_claim_authority"]["owner"] == "混合"
    assert by_name["compose_claim_aware_narrative"]["owner"] == "混合"
    assert by_name["execute_capability_dag"]["owner"] == "本地系统"
    assert by_name["accept_material_decision"]["owner"] == "用户"
    assert len(result["processSummary"]["acceptedGraph"]) == 12
    task_executions = [
        task["execution"] for task in result["processSummary"]["acceptedGraph"]
    ]
    assert {execution["state"] for execution in task_executions} == {"settled"}
    assert [execution["status"] for execution in task_executions].count(
        "unavailable"
    ) == 2
    integrity_failure = next(
        execution for execution in task_executions
        if execution["status"] == "integrity_failed"
    )
    assert integrity_failure["failure"] == {
        "layer": "query",
        "kind": "query_result_contract_invalid",
        "integrityLevel": "task",
        "businessBoundary": "market_channel_context_evidence_unpublishable",
    }
    assert len(result["traceClaims"]) == 5
    assert len(result["traceEvidence"]) == 15
    assert {entry["planState"] for entry in result["traceEvidence"]} == {"active"}
    evidence_refs = {evidence["evidenceRef"] for evidence in result["traceEvidence"]}
    assert all(
        set(claim["evidenceRefs"]).issubset(evidence_refs)
        for claim in result["traceClaims"]
    )
    assert result["timing"] == {
        "actualDurationMs": 12000,
        "completeness": "known",
    }


def test_repeated_stage_timings_bind_to_exact_transition_attempts() -> None:
    result = _run_typescript(
        """
        import { traceRunFromRuntimeState } from "./app/api/_customerRunProjection.ts";
        const timing = (attemptId, startedAt, finishedAt) => ({
          transition_attempt_id: attemptId,
          stage_name: "compile_plan_patch",
          started_at: startedAt,
          finished_at: finishedAt,
          accepted_call_count: 1,
          llm_call_count: 1,
          control_call_count: 0,
          query_call_count: 0,
          capability_call_count: 0,
        });
        const run = traceRunFromRuntimeState({
          id: "runtime:run-test",
          label: "测试运行",
          runId: "run-test",
          runStatus: "planned",
          question: "继续扩展证据路径",
          workflowTransitions: [
            { attempt_id: "patch-1", node_name: "compile_plan_patch", status: "succeeded", acceptance_state: "accepted" },
            { attempt_id: "patch-2", node_name: "compile_plan_patch", status: "succeeded", acceptance_state: "accepted" },
          ],
          stageTimings: [
            timing("patch-1", "2026-07-20T00:00:00.100Z", "2026-07-20T00:00:00.500Z"),
            timing("patch-2", "2026-07-20T00:00:00.600Z", "2026-07-20T00:00:01.100Z"),
          ],
          acceptedGraph: [{
            task_id: "task:planned",
            plan_revision_id: "plan:planned",
            capability_id: "metric_timeseries",
            task_key: "planned_metric",
            execution_state: "not_started",
          }],
        });
        console.log(JSON.stringify(run));
        """
    )

    nodes = result["processSummary"]["nodes"]
    assert result["runMode"] == "event_replay"
    assert result["processSummary"]["llmCallCount"] == 2
    assert [node["id"] for node in nodes] == ["patch-1", "patch-2"]
    assert [node["startedAt"] for node in nodes] == [
        "2026-07-20T00:00:00.100Z",
        "2026-07-20T00:00:00.600Z",
    ]
    assert [node["durationMs"] for node in nodes] == [400, 500]
    assert result["processSummary"]["acceptedGraph"] == [{
        "taskId": "task:planned",
        "planRevisionId": "plan:planned",
        "capabilityId": "metric_timeseries",
        "taskKey": "planned_metric",
        "execution": {"state": "not_started"},
    }]
    assert result["traceCompleteness"]["acceptedGraph"] == "known"


def test_plan_patch_execute_nodes_bind_only_their_exact_evidence() -> None:
    result = _run_typescript(
        """
        import { traceRunFromRuntimeState } from "./app/api/_customerRunProjection.ts";
        const timing = (attemptId, stageName, startedAt, finishedAt, llm, capability) => ({
          transition_attempt_id: attemptId, stage_name: stageName,
          started_at: startedAt, finished_at: finishedAt,
          accepted_call_count: llm + capability, llm_call_count: llm,
          control_call_count: 0, query_call_count: 0, capability_call_count: capability,
        });
        const run = traceRunFromRuntimeState({
          id: "runtime:patch", label: "patch", runId: "patch",
          runStatus: "evidence_ready", question: "扩展后证据是什么？",
          workflowTransitions: [
            {
              attempt_id: "execute-old", node_name: "execute_capability_dag",
              status: "succeeded", acceptance_state: "accepted",
              execution_snapshot_ref: "snapshot:old",
              execution_plan_revision_id: "plan:old",
              execution_evidence_entry_refs: ["entry:old"],
            },
            {
              attempt_id: "patch", node_name: "compile_plan_patch",
              status: "succeeded", acceptance_state: "accepted",
            },
            {
              attempt_id: "execute-active", node_name: "execute_capability_dag",
              status: "succeeded", acceptance_state: "accepted",
              execution_snapshot_ref: "snapshot:active",
              execution_plan_revision_id: "plan:active",
              execution_evidence_entry_refs: ["entry:active"],
            },
          ],
          stageTimings: [
            timing("execute-old", "execute_capability_dag", "2026-07-20T00:00:00.100Z", "2026-07-20T00:00:00.400Z", 0, 1),
            timing("patch", "compile_plan_patch", "2026-07-20T00:00:00.500Z", "2026-07-20T00:00:00.800Z", 1, 0),
            timing("execute-active", "execute_capability_dag", "2026-07-20T00:00:00.900Z", "2026-07-20T00:00:01.200Z", 0, 1),
          ],
          evidenceRefs: [
            {
              entry_ref: "entry:old", evidence_ref: "evidence:old",
              plan_revision_id: "plan:old", plan_state: "superseded", task_id: "task:old",
              capability_id: "metric_timeseries", binding_state: "bound",
              execution_transition_attempt_id: "execute-old", execution_state: "available",
              evidence_kind: "observed", data_contract_state: "complete",
              maximum_claim_strength: "directional", limitation_refs: [],
            },
            {
              entry_ref: "entry:active", evidence_ref: "evidence:active",
              plan_revision_id: "plan:active", plan_state: "active", task_id: "task:active",
              capability_id: "metric_timeseries", binding_state: "bound",
              execution_transition_attempt_id: "execute-active", execution_state: "available",
              evidence_kind: "observed", data_contract_state: "complete",
              maximum_claim_strength: "directional", limitation_refs: [],
            },
          ],
          acceptedGraph: [{
            task_id: "task:active", plan_revision_id: "plan:active",
            capability_id: "metric_timeseries", task_key: "active_metric",
            execution_state: "settled", outcome_ref: "outcome:active",
            status: "succeeded", retryability: "never", limitation_refs: [],
          }],
        });
        const empty = traceRunFromRuntimeState({
          id: "runtime:empty", label: "empty", runId: "empty",
          runStatus: "evidence_ready", question: "能力不可用时有证据吗？",
          workflowTransitions: [{
            attempt_id: "execute-empty", node_name: "execute_capability_dag",
            status: "succeeded", acceptance_state: "accepted",
            execution_snapshot_ref: "snapshot:empty",
            execution_plan_revision_id: "plan:empty",
            execution_evidence_entry_refs: [],
          }],
          stageTimings: [
            timing("execute-empty", "execute_capability_dag", "2026-07-20T00:00:00.100Z", "2026-07-20T00:00:00.400Z", 0, 1),
          ],
          evidenceRefs: [],
          acceptedGraph: [{
            task_id: "task:empty", plan_revision_id: "plan:empty",
            capability_id: "metric_timeseries", task_key: "empty_metric",
            execution_state: "settled", outcome_ref: "outcome:empty",
            status: "unavailable", retryability: "replan_required",
            limitation_refs: ["no_comparable_periods"],
          }],
        });
        console.log(JSON.stringify({ run, empty }));
        """
    )

    nodes = {
        node["id"]: node for node in result["run"]["processSummary"]["nodes"]
    }
    assert nodes["execute-old"]["evidenceRefs"] == ["evidence:old"]
    assert nodes["execute-active"]["evidenceRefs"] == ["evidence:active"]
    assert nodes["execute-old"]["evidenceCompleteness"] == "known"
    assert nodes["execute-active"]["evidenceCompleteness"] == "known"
    assert result["run"]["processSummary"]["acceptedGraph"] == [{
        "taskId": "task:active",
        "planRevisionId": "plan:active",
        "capabilityId": "metric_timeseries",
        "taskKey": "active_metric",
        "execution": {
            "state": "settled",
            "outcomeRef": "outcome:active",
            "status": "succeeded",
            "retryability": "never",
            "limitationRefs": [],
        },
    }]
    assert [entry["planState"] for entry in result["run"]["traceEvidence"]] == [
        "superseded",
        "active",
    ]
    empty_node = result["empty"]["processSummary"]["nodes"][0]
    assert empty_node["evidenceRefs"] == []
    assert empty_node["evidenceCompleteness"] == "known"
    assert result["empty"]["processSummary"]["acceptedGraph"][0]["execution"] == {
        "state": "settled",
        "outcomeRef": "outcome:empty",
        "status": "unavailable",
        "retryability": "replan_required",
        "limitationRefs": ["no_comparable_periods"],
    }


def test_control_calls_and_missing_journal_coverage_do_not_inflate_llm_truth() -> None:
    result = _run_typescript(
        """
        import { traceRunFromRuntimeState } from "./app/api/_customerRunProjection.ts";
        const entry = {
          attempt_id: "entry-control",
          node_name: "conversation_entry",
          status: "succeeded",
          acceptance_state: "accepted",
        };
        const controlled = traceRunFromRuntimeState({
          id: "runtime:control",
          label: "control",
          runId: "control",
          runStatus: "interaction_completed",
          question: "选择话题",
          workflowTransitions: [entry],
          stageTimings: [{
            transition_attempt_id: "entry-control",
            stage_name: "conversation_entry",
            started_at: "2026-07-20T00:00:00.100Z",
            finished_at: "2026-07-20T00:00:00.500Z",
            accepted_call_count: 1,
            llm_call_count: 0,
            control_call_count: 1,
            query_call_count: 0,
            capability_call_count: 0,
          }],
          acceptedGraph: [],
        });
        const missing = traceRunFromRuntimeState({
          id: "runtime:missing",
          label: "missing",
          runId: "missing",
          runStatus: "running_workflow",
          question: "继续分析",
          workflowTransitions: [{ ...entry, attempt_id: "entry-missing" }],
          stageTimings: [],
          acceptedGraph: [],
        });
        console.log(JSON.stringify({ controlled, missing }));
        """
    )

    controlled = result["controlled"]
    assert controlled["processSummary"]["llmCallCount"] == 0
    assert controlled["traceCompleteness"]["llmCalls"] == "known"
    assert controlled["processSummary"]["nodes"][0]["owner"] == "本地系统"
    missing = result["missing"]
    assert missing["processSummary"]["llmCallCount"] == 0
    assert missing["traceCompleteness"]["llmCalls"] == "incomplete"
    assert missing["runMode"] == "static_snapshot"


def test_claim_coverage_owner_follows_provider_or_deterministic_route() -> None:
    result = _run_typescript(
        """
        import { traceRunFromRuntimeState } from "./app/api/_customerRunProjection.ts";
        const provider = traceRunFromRuntimeState({
          id: "runtime:provider", label: "provider", runId: "provider",
          runStatus: "evidence_ready", question: "还有证据路径吗？",
          workflowTransitions: [{
            attempt_id: "coverage-provider", node_name: "evaluate_claim_coverage",
            provider_ref: "semantic_provider", status: "succeeded", acceptance_state: "accepted",
          }],
          stageTimings: [{
            transition_attempt_id: "coverage-provider", stage_name: "evaluate_claim_coverage",
            started_at: "2026-07-20T00:00:00.100Z", finished_at: "2026-07-20T00:00:00.500Z",
            accepted_call_count: 1, llm_call_count: 1, control_call_count: 0,
            query_call_count: 0, capability_call_count: 0,
          }],
          acceptedGraph: [],
        });
        const local = traceRunFromRuntimeState({
          id: "runtime:local", label: "local", runId: "local",
          runStatus: "evidence_ready", question: "还有证据路径吗？",
          workflowTransitions: [{
            attempt_id: "coverage-local", node_name: "evaluate_claim_coverage",
            provider_ref: "local_deterministic", status: "succeeded", acceptance_state: "accepted",
            started_at: "2026-07-20T00:00:00.100Z", finished_at: "2026-07-20T00:00:00.500Z",
          }],
          stageTimings: [],
          acceptedGraph: [],
        });
        let conflict;
        try {
          traceRunFromRuntimeState({
            id: "runtime:conflict", label: "conflict", runId: "conflict",
            runStatus: "evidence_ready", question: "还有证据路径吗？",
            workflowTransitions: [{
              attempt_id: "coverage-conflict", node_name: "evaluate_claim_coverage",
              provider_ref: "local_deterministic", status: "succeeded", acceptance_state: "accepted",
            }],
            stageTimings: [{
              transition_attempt_id: "coverage-conflict", stage_name: "evaluate_claim_coverage",
              started_at: "2026-07-20T00:00:00.100Z", finished_at: "2026-07-20T00:00:00.500Z",
              accepted_call_count: 1, llm_call_count: 1, control_call_count: 0,
              query_call_count: 0, capability_call_count: 0,
            }],
            acceptedGraph: [],
          });
        } catch (error) {
          conflict = error.message;
        }
        console.log(JSON.stringify({ provider, local, conflict }));
        """
    )

    assert result["provider"]["processSummary"]["nodes"][0]["owner"] == "混合"
    assert result["provider"]["processSummary"]["llmCallCount"] == 1
    assert result["provider"]["runMode"] == "event_replay"
    assert result["local"]["processSummary"]["nodes"][0]["owner"] == "本地系统"
    assert result["local"]["processSummary"]["llmCallCount"] == 0
    assert result["local"]["runMode"] == "event_replay"
    assert result["conflict"] == "workbench_stage_call_contract_invalid"


def test_rejected_transition_keeps_persisted_snapshot_nodes() -> None:
    result = _run_typescript(
        """
        import { traceRunFromRuntimeState } from "./app/api/_customerRunProjection.ts";
        const run = traceRunFromRuntimeState({
          id: "runtime:run-test",
          label: "测试运行",
          runId: "run-test",
          runStatus: "failed",
          question: "发生了什么？",
          workflowTransitions: [{
            attempt_id: "rejected-attempt",
            node_name: "bind_intent",
            status: "failed",
            acceptance_state: "rejected",
          }],
          stageTimings: [],
          runNodes: [{
            node_name: "run_status",
            status: "failed",
            started_at: "2026-07-20T00:00:00.100Z",
            finished_at: "2026-07-20T00:00:00.200Z",
          }],
          acceptedGraph: [],
        });
        console.log(JSON.stringify(run));
        """
    )

    assert result["runOutcome"] == "failed"
    assert result["runMode"] == "static_snapshot"
    assert result["traceCompleteness"]["chronology"] == "incomplete"
    assert [node["node"] for node in result["processSummary"]["nodes"]] == ["run_status"]


def test_runtime_checkpoint_statuses_are_not_projected_as_terminal_completion() -> None:
    result = _run_typescript(
        """
        import { traceRunFromRuntimeState } from "./app/api/_customerRunProjection.ts";
        const statuses = [
          "queued",
          "running_workflow",
          "waiting_for_clarification",
          "planned",
          "evidence_ready",
          "authority_sealed",
          "narrative_ready",
          "interaction_completed",
          "completed",
          "failed",
        ];
        const projected = statuses.map((status) => {
          const run = traceRunFromRuntimeState({
            id: `runtime:${status}`,
            label: status,
            runId: status,
            runStatus: status,
            question: "状态是什么？",
            request: { analysis_status: "complete" },
          });
          return [status, run.runOutcome, run.lifecycle.execution.outcome];
        });
        console.log(JSON.stringify(projected));
        """
    )

    assert result == [
        ["queued", "running", "running"],
        ["running_workflow", "running", "running"],
        ["waiting_for_clarification", "waiting", "pending"],
        ["planned", "checkpoint", "checkpoint"],
        ["evidence_ready", "checkpoint", "checkpoint"],
        ["authority_sealed", "checkpoint", "checkpoint"],
        ["narrative_ready", "checkpoint", "checkpoint"],
        ["interaction_completed", "interaction_completed", "not_applicable"],
        ["completed", "completed", "complete"],
        ["failed", "failed", "failed"],
    ]


def test_missing_trace_sources_remain_typed_unknown() -> None:
    result = _run_typescript(
        """
        import { traceRunFromRuntimeState } from "./app/api/_customerRunProjection.ts";
        const run = traceRunFromRuntimeState({
          id: "runtime:run-test",
          label: "测试运行",
          runId: "run-test",
          runStatus: "queued",
          question: "发生了什么？",
        });
        console.log(JSON.stringify(run));
        """
    )

    assert result["runOutcome"] == "running"
    assert result["lifecycle"]["execution"]["outcome"] == "running"
    assert result["runMode"] == "static_snapshot"
    assert result["timing"] == {"completeness": "unknown"}
    assert "llmCallCount" not in result["processSummary"]
    assert "acceptedGraph" not in result["processSummary"]
    assert result["traceCompleteness"] == {
        "chronology": "unknown",
        "llmCalls": "unknown",
        "acceptedGraph": "unknown",
        "claims": "unknown",
        "evidence": "unknown",
        "timing": "unknown",
    }


def test_zero_span_chronology_is_a_snapshot_not_a_replay() -> None:
    result = _run_typescript(
        """
        import { traceRunFromRuntimeState } from "./app/api/_customerRunProjection.ts";
        const run = traceRunFromRuntimeState({
          id: "runtime:run-test",
          label: "测试运行",
          runId: "run-test",
          runStatus: "planned",
          question: "发生了什么？",
          workflowTransitions: [{
            attempt_id: "wait",
            node_name: "persist_waiting_for_decision",
            provider_ref: "system",
            status: "succeeded",
            acceptance_state: "accepted",
            started_at: "2026-07-20T00:00:00.100Z",
            finished_at: "2026-07-20T00:00:00.100Z",
          }],
          stageTimings: [],
          acceptedGraph: [],
        });
        console.log(JSON.stringify(run));
        """
    )

    assert result["traceCompleteness"]["chronology"] == "known"
    assert result["runMode"] == "static_snapshot"


def test_pending_publication_is_visible_and_authoritative_projection_wins_duplicate() -> None:
    result = _run_typescript(
        """
        import {
          canonicalizeTraceRuns,
          traceRunFromCustomerPublication,
          traceRunFromRuntimeState,
        } from "./app/api/_customerRunProjection.ts";
        const digest = "d".repeat(64);
        const customer = {
          blocks: [{
            claim_refs: ["claim:main"], limitation_refs: [], material_fact_bindings: [],
            recommendation_refs: [], role: "summary", statement_role: "conclusion", text: "结论",
          }],
          claim_refs: ["claim:main"], field_visibility_policy_ref: "visibility:test",
          limitation_refs: [], recommendation_refs: [], visualization_refs: [], warnings: [],
        };
        const basePublication = {
          authority_bundle_ref: "authority:test", authority_bundle_digest: digest,
          authority_sealed_at: "2026-07-20T00:00:00.500Z",
          publication_ref: "publication:test", publication_digest: digest,
          published_at: "2026-07-20T00:00:01.500Z",
          projection_id: "projection:test", projection_digest: digest, outbox_ref: "outbox:test",
        };
        const baseOptions = {
          label: "publication", runId: "same-run", question: "结论是什么？",
          workflowTransitions: [
            { attempt_id: "settle", node_name: "settle_claim_authority", status: "succeeded", acceptance_state: "accepted" },
            { attempt_id: "compose", node_name: "compose_claim_aware_narrative", status: "succeeded", acceptance_state: "accepted" },
          ],
          stageTimings: [
            { transition_attempt_id: "settle", stage_name: "settle_claim_authority", started_at: "2026-07-20T00:00:00.100Z", finished_at: "2026-07-20T00:00:00.400Z", accepted_call_count: 1, llm_call_count: 1, control_call_count: 0, query_call_count: 0, capability_call_count: 0 },
            { transition_attempt_id: "compose", stage_name: "compose_claim_aware_narrative", started_at: "2026-07-20T00:00:00.600Z", finished_at: "2026-07-20T00:00:01.400Z", accepted_call_count: 1, llm_call_count: 1, control_call_count: 0, query_call_count: 0, capability_call_count: 0 },
          ],
          evidenceRefs: [],
          claimEvidenceLinks: [{ claim_ref: "claim:main", evidence_refs: [] }],
          acceptedGraph: [],
          verifierStatus: {
            acceptedClaimCount: 1, vetoedClaimCount: 0,
            acceptedBlockCount: 1, rejectedBlockCount: 0, vetoedBlockCount: 0,
            claimReportRefs: ["claim-report"], blockReportRefs: ["block-report"],
            verifiedAt: "2026-07-20T00:00:01.450Z",
          },
        };
        const pending = traceRunFromCustomerPublication(customer, {
          ...basePublication, delivery_status: "pending",
        }, {
          ...baseOptions, id: "publication:pending", runStatus: "narrative_ready",
          request: {
            analysis_status: "complete",
            post_execution_status: "narrative_ready",
            publication_status: "ready",
            delivery_status: "persisted",
          },
        });
        const failed = traceRunFromCustomerPublication(customer, {
          ...basePublication, delivery_status: "permanently_failed",
          delivery_attempted_at: "2026-07-20T00:00:01.600Z",
        }, {
          ...baseOptions, id: "publication:failed", runStatus: "completed",
          request: {
            analysis_status: "complete",
            post_execution_status: "delivery_permanently_failed",
            publication_status: "ready",
            delivery_status: "permanently_failed",
          },
        });
        const runtime = traceRunFromRuntimeState({
          id: "runtime:completed", label: "runtime", runId: "same-run",
          runStatus: "completed", question: "结论是什么？",
          request: { analysis_status: "complete" },
        });
        const canonical = canonicalizeTraceRuns([runtime, failed]);
        console.log(JSON.stringify({ pending, failed, canonical }));
        """
    )

    assert result["pending"]["runOutcome"] == "delivery_pending"
    assert result["pending"]["lifecycle"]["publication"]["status"] == "published"
    assert result["pending"]["lifecycle"]["delivery"]["outcome"] == "pending"
    assert result["pending"]["runMode"] == "static_snapshot"
    assert result["failed"]["runOutcome"] == "delivery_failed"
    assert result["canonical"][0]["id"] == "publication:failed"
    assert "answer" in result["canonical"][0]


def test_projection_exposes_missing_evidence_and_publication_closure() -> None:
    result = _run_typescript(
        """
        import {
          traceRunFromCustomerPublication,
          traceRunFromRuntimeState,
        } from "./app/api/_customerRunProjection.ts";

        const errors = {};
        try {
          traceRunFromRuntimeState({
            id: "runtime:evidence",
            label: "evidence",
            runId: "evidence",
            runStatus: "evidence_ready",
            question: "证据是什么？",
            evidenceRefs: [{
              evidence_ref: "evidence:test",
              plan_revision_id: "plan:active",
              plan_state: "active",
              task_id: "task:main",
              binding_state: "unsettled",
              capability_id: "metric_timeseries",
              evidence_kind: "observed",
              execution_state: "available",
              data_contract_state: "complete",
              maximum_claim_strength: "directional",
            }],
          });
        } catch (error) {
          errors.evidence = error.message;
        }

        const digest = "c".repeat(64);
        const claim1 = "claim:one";
        const claim2 = "claim:two";
        const customer = {
          blocks: [{
            claim_refs: [claim1], limitation_refs: [], material_fact_bindings: [],
            recommendation_refs: [], role: "summary", statement_role: "conclusion", text: "结论一",
          }],
          claim_refs: [claim1, claim2],
          field_visibility_policy_ref: "visibility-policy:test",
          limitation_refs: [], recommendation_refs: [], visualization_refs: [], warnings: [],
        };
        const publication = {
          authority_bundle_ref: "authority:test", authority_bundle_digest: digest,
          authority_sealed_at: "2026-07-20T00:00:00.500Z",
          publication_ref: "publication:test", publication_digest: digest,
          published_at: "2026-07-20T00:00:01.500Z",
          projection_id: "projection:test", projection_digest: digest,
          outbox_ref: "outbox:test", delivery_status: "published",
          delivery_attempted_at: "2026-07-20T00:00:01.600Z",
        };
        const options = {
          id: "persisted:test", label: "test", runId: "test", runStatus: "completed",
          workflowTransitions: [
            { attempt_id: "settle", node_name: "settle_claim_authority", status: "succeeded", acceptance_state: "accepted" },
            { attempt_id: "compose", node_name: "compose_claim_aware_narrative", status: "succeeded", acceptance_state: "accepted" },
          ],
          stageTimings: [
            { transition_attempt_id: "settle", stage_name: "settle_claim_authority", started_at: "2026-07-20T00:00:00.100Z", finished_at: "2026-07-20T00:00:00.400Z", accepted_call_count: 1, llm_call_count: 1, control_call_count: 0, query_call_count: 0, capability_call_count: 0 },
            { transition_attempt_id: "compose", stage_name: "compose_claim_aware_narrative", started_at: "2026-07-20T00:00:00.600Z", finished_at: "2026-07-20T00:00:01.400Z", accepted_call_count: 1, llm_call_count: 1, control_call_count: 0, query_call_count: 0, capability_call_count: 0 },
          ],
          claimEvidenceLinks: [
            { claim_ref: claim1, evidence_refs: [] },
            { claim_ref: claim2, evidence_refs: [] },
          ],
          evidenceRefs: [], acceptedGraph: [],
          verifierStatus: { acceptedBlockCount: 1, vetoedBlockCount: 0, blockReportRefs: ["report"], verifiedAt: "2026-07-20T00:00:01.450Z" },
        };
        try {
          traceRunFromCustomerPublication(customer, publication, options);
        } catch (error) {
          errors.claim = error.message;
        }
        try {
          traceRunFromCustomerPublication(
            { ...customer, claim_refs: [claim1] },
            publication,
            { ...options, claimEvidenceLinks: [{ claim_ref: claim1, evidence_refs: [] }], request: { publication_status: "ready" } },
          );
        } catch (error) {
          errors.publication = error.message;
        }
        console.log(JSON.stringify(errors));
        """
    )

    assert result == {
        "evidence": "workbench_evidence_projection_invalid",
        "claim": "workbench_published_claim_block_missing",
        "publication": "workbench_publication_status_mismatch",
    }


def test_accepted_task_execution_rejects_unclosed_or_unsafe_outcome_truth() -> None:
    result = _run_typescript(
        """
        import { traceRunFromRuntimeState } from "./app/api/_customerRunProjection.ts";
        const identity = {
          task_id: "task:main",
          plan_revision_id: "plan:active",
          capability_id: "metric_timeseries",
          task_key: "metric_timeseries:main",
        };
        const settled = {
          ...identity,
          execution_state: "settled",
          outcome_ref: "outcome:main",
          status: "integrity_failed",
          retryability: "replan_required",
          limitation_refs: ["limitation:main"],
          failure: {
            layer: "query",
            kind: "query_result_contract_invalid",
            integrity_level: "task",
            business_boundary: "query_evidence_unpublishable",
          },
        };
        const project = (acceptedGraph) => traceRunFromRuntimeState({
          id: "runtime:test", label: "test", runId: "test",
          runStatus: "evidence_ready", question: "执行结果是什么？",
          acceptedGraph,
        });
        const errors = {};
        for (const [name, graph] of Object.entries({
          missingOutcome: [{ ...settled, outcome_ref: undefined }],
          duplicateTask: [settled, { ...settled, outcome_ref: "outcome:other" }],
          partialExecution: [{
            ...identity,
            execution_state: "unsettled",
            outcome_ref: "outcome:partial",
          }],
          unsafeFailure: [{
            ...settled,
            failure: { ...settled.failure, technical_detail_ref: "secret:provider" },
          }],
        })) {
          try {
            project(graph);
          } catch (error) {
            errors[name] = error.message;
          }
        }
        console.log(JSON.stringify(errors));
        """
    )

    assert result == {
        "missingOutcome": "workbench_accepted_graph_invalid",
        "duplicateTask": "workbench_accepted_graph_invalid",
        "partialExecution": "workbench_accepted_graph_invalid",
        "unsafeFailure": "workbench_accepted_graph_invalid",
    }


def _run_typescript(source: str):
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node executable is required")
    completed = subprocess.run(
        [
            node,
            "--no-warnings",
            "--experimental-loader=./tests/support/typescript-extension-loader.mjs",
            "--experimental-strip-types",
            "--input-type=module",
            "-e",
            textwrap.dedent(source),
        ],
        cwd=ROOT,
        env={**os.environ, "NODE_ENV": "test"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)
