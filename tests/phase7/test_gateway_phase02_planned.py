from __future__ import annotations

import json
from pathlib import Path
import textwrap

from tests.phase7.test_gateway_route_runtime import _compiled_gateway_run
from tests.phase7.test_gateway_typescript_contract import _run_typescript


ROOT = Path(__file__).resolve().parents[2]


def test_planned_agent_core_contract_is_strict_and_customer_projection_is_fixed():
    result = _run_typescript(
        textwrap.dedent(
            """
            const { parseAgentCoreOutput } = await import("./app/api/_agentCore.ts");
            const { projectAgentCoreForCustomer } = await import(
              "./app/api/_conversationStore.ts"
            );
            const authorityRefs = {
              intent_revision_id: "intent-phase02",
              authority_context_ref: "authority-context:phase02",
              planner_proposal_id: "planner-proposal-phase02",
              proposal_admission_id: "proposal-admission-phase02",
              plan_revision_id: "plan-revision-phase02",
              accepted_transition_id: "transition-phase02",
            };
            const planResult = {
              schema_version: "single-authority-phase02.v2",
              run_id: "run-phase02",
              run_attempt_id: "run-phase02",
              status: "planned",
              intent_revision_id: "intent-phase02",
              plan_patch_ref: null,
              decision_ledger_position: 1,
              decision_refs: ["decision-phase02"],
              authority_context: {
                authority_context_ref: "authority-context:phase02",
                run_attempt_id: "run-phase02",
                actual_as_of: "2026-07-18T00:00:00Z",
                release_refs: ["release:phase02"],
                snapshot_refs: ["snapshot:phase02"],
                dataset_coverage: [{
                  dataset_id: "paid_order_success",
                  availability: "claim_ready",
                  release_ref: "release:phase02",
                  snapshot_refs: ["snapshot:phase02"],
                  limitation_ref: null,
                  raw_provider_response_ref: "private-nested-context",
                }],
                contract_versions: {
                  runtime_bindings: "v1",
                  raw_provider_response_ref: "private-contract-version",
                },
                content_digest: "context-digest",
              },
              planner_proposal: {
                planner_proposal_id: "planner-proposal-phase02",
                run_attempt_id: "run-phase02",
                intent_revision_id: "intent-phase02",
                decision_refs: ["decision-phase02"],
                authority_context_ref: "authority-context:phase02",
                issue_tree: [{
                  issue_id: "issue-root",
                  parent_issue_id: null,
                  question: "付费金额为何变化？",
                  target_claim_kind: "comparative_change",
                  private_chain_of_thought: "private-reasoning",
                }],
                auxiliary_axes: [],
                hypotheses: [],
                priority_proposals: [],
                assumption_proposals: [{ statement: "private-assumption" }],
                raw_provider_response_ref: "private-provider-ref",
                raw_provider_response: "private-provider-content",
                schema_version: "planner-proposal.v1",
                prompt_version: "single-authority-plan-proposal.v1",
                model_version: "model-v1",
                content_digest: "proposal-digest",
              },
              proposal_admission_record: {
                proposal_admission_id: "proposal-admission-phase02",
                planner_proposal_ref: "planner-proposal-phase02",
                intent_revision_id: "intent-phase02",
                decision_refs: ["decision-phase02"],
                authority_context_ref: "authority-context:phase02",
                admission_entries: [],
                compiler_version: "compiler-v1",
                contract_versions: { runtime_bindings: "v1" },
                content_digest: "admission-digest",
              },
              plan_revision: {
                plan_revision_id: "plan-revision-phase02",
                run_attempt_id: "run-phase02",
                supersedes_plan_revision_id: null,
                intent_revision_id: "intent-phase02",
                decision_refs: ["decision-phase02"],
                authority_context_ref: "authority-context:phase02",
                planner_proposal_ref: "planner-proposal-phase02",
                proposal_admission_ref: "proposal-admission-phase02",
                resolved_window_refs: ["window:target"],
                context_window_specs: [{
                  capability_id: "change_point_scan",
                  relation: "trailing_complete_periods",
                  unit: "day",
                  count: 8,
                  internal_owner: "private-context-window-owner",
                }],
                claim_obligations: [],
                analysis_axes: [],
                capability_tasks: [],
                assumption_refs: [],
                budget_policy_ref: "budget:default",
                contract_versions: { runtime_bindings: "v1" },
                content_digest: "plan-digest",
              },
              durable_checkpoint: { private: "private-checkpoint" },
              authority_refs: authorityRefs,
              llm_calls: [{ response: "private-llm-call" }],
              checkpoint_events: [{ payload: "private-checkpoint-event" }],
            };
            const output = {
              status: "planned",
              run_id: "run-phase02",
              turn_id: "turn-phase02",
              topic_id: "topic-phase02",
              context_manifest: {},
              plan_result: planResult,
            };
            const parsed = parseAgentCoreOutput(JSON.stringify(output));
            const truncated = structuredClone(output);
            delete truncated.plan_result.plan_revision;
            const rejected = parseAgentCoreOutput(JSON.stringify(truncated));
            const legacyDecision = parseAgentCoreOutput(JSON.stringify({
              status: "decision_recorded",
              run_id: "run-phase02",
              turn_id: "turn-phase02",
              topic_id: "topic-phase02",
              intent_revision_id: "intent-phase02",
              decision: {},
              decision_ledger: {},
              durable_checkpoint: {},
            }));
            const legacyVisible = projectAgentCoreForCustomer({
              status: "decision_recorded",
              result: {
                status: "decision_recorded",
                run_id: "run-phase02",
                turn_id: "turn-phase02",
                topic_id: "topic-phase02",
                decision: { private: "legacy-decision" },
              },
            });
            const visible = projectAgentCoreForCustomer({
              status: "planned",
              command: "private-command",
              output: "private-stdout",
              result: output,
            });
            console.log(JSON.stringify({
              parsed,
              rejected,
              legacyDecision,
              legacyVisible,
              visible,
            }));
            """
        )
    )

    assert result["parsed"]["status"] == "planned"
    assert result["rejected"] == {
        "status": "failed",
        "result": None,
        "error": "agent_core_output_shape_invalid",
    }
    assert result["legacyDecision"] == {
        "status": "failed",
        "result": None,
        "error": "agent_core_output_status_invalid",
    }
    assert result["legacyVisible"] == {
        "status": "failed",
        "error": "agent_core_run_failed",
        "result": {
            "run_id": "run-phase02",
            "turn_id": "turn-phase02",
            "topic_id": "topic-phase02",
            "status": "failed",
            "failure_reason": "agent_core_run_failed",
        },
    }
    assert "legacy-decision" not in json.dumps(result["legacyVisible"])
    visible = result["visible"]
    assert visible["status"] == "planned"
    assert visible["result"]["plan_result"]["status"] == "planned"
    assert visible["result"]["plan_result"]["plan_revision"][
        "context_window_specs"
    ] == [
        {
            "capability_id": "change_point_scan",
            "relation": "trailing_complete_periods",
            "unit": "day",
            "count": 8,
        }
    ]
    assert (
        visible["result"]["plan_result"]["planner_proposal"]["issue_tree"][0][
            "question"
        ]
        == "付费金额为何变化？"
    )
    serialized = json.dumps(visible, ensure_ascii=False)
    for private_value in (
        "private-command",
        "private-stdout",
        "private-provider-ref",
        "private-provider-content",
        "private-llm-call",
        "private-checkpoint",
        "private-checkpoint-event",
        "private-reasoning",
        "private-assumption",
        "private-contract-version",
        "private-nested-context",
        "private-context-window-owner",
    ):
        assert private_value not in serialized
    for private_field in (
        "raw_provider_response_ref",
        "raw_provider_response",
        "llm_calls",
        "durable_checkpoint",
        "checkpoint_events",
        "assumption_proposals",
    ):
        assert private_field not in serialized


def test_interaction_directive_statuses_use_a_fixed_customer_projection():
    result = _run_typescript(
        textwrap.dedent(
            """
            const { projectAgentCoreForCustomer } = await import(
              "./app/api/_conversationStore.ts"
            );
            const statuses = [
              "material_revision_required",
              "run_cancelled",
              "challenge_recorded",
            ];
            const projected = statuses.map((status, index) =>
              projectAgentCoreForCustomer({
                status,
                command: "private-command",
                result: {
                  run_id: `run-directive-${index}`,
                  turn_id: `turn-directive-${index}`,
                  topic_id: `topic-directive-${index}`,
                  status,
                  intent_revision_id: `intent-directive-${index}`,
                  directive: {
                    directive_id: `directive-${index}`,
                    kind: "private-kind",
                    target_refs: ["private-target"],
                    original_user_text: "private-original-user-text",
                    source: "private-source",
                    content_digest: "private-directive-digest",
                  },
                  durable_checkpoint: {
                    transition_id: "private-transition",
                    provider_ref: "private-provider",
                    model_ref: "private-model",
                  },
                  replacement_user_text: "private-replacement-user-text",
                  superseded_plan_fields: ["private-plan-field"],
                  raw_decision_binding: { private: "private-binding" },
                  replayed: true,
                },
              })
            );
            console.log(JSON.stringify(projected));
            """
        )
    )

    assert result == [
        {
            "status": "material_revision_required",
            "result": {
                "run_id": "run-directive-0",
                "turn_id": "turn-directive-0",
                "topic_id": "topic-directive-0",
                "status": "material_revision_required",
                "intent_revision_id": "intent-directive-0",
                "directive_id": "directive-0",
            },
        },
        {
            "status": "run_cancelled",
            "result": {
                "run_id": "run-directive-1",
                "turn_id": "turn-directive-1",
                "topic_id": "topic-directive-1",
                "status": "run_cancelled",
                "intent_revision_id": "intent-directive-1",
                "directive_id": "directive-1",
            },
        },
        {
            "status": "challenge_recorded",
            "result": {
                "run_id": "run-directive-2",
                "turn_id": "turn-directive-2",
                "topic_id": "topic-directive-2",
                "status": "challenge_recorded",
                "intent_revision_id": "intent-directive-2",
                "directive_id": "directive-2",
            },
        },
    ]
    serialized = json.dumps(result, ensure_ascii=False)
    for private_value in (
        "private-command",
        "private-kind",
        "private-target",
        "private-original-user-text",
        "private-source",
        "private-directive-digest",
        "private-transition",
        "private-provider",
        "private-model",
        "private-replacement-user-text",
        "private-plan-field",
        "private-binding",
    ):
        assert private_value not in serialized
    for private_field in (
        "directive",
        "durable_checkpoint",
        "replacement_user_text",
        "superseded_plan_fields",
        "raw_decision_binding",
        "replayed",
    ):
        assert all(private_field not in item["result"] for item in result)


def test_planned_customer_projection_rejects_incomplete_authority_bundle():
    errors = _run_typescript(
        textwrap.dedent(
            """
            const { projectPlanResultForCustomer } = await import(
              "./app/api/_conversationStore.ts"
            );
            const complete = {
              schema_version: "single-authority-phase02.v2",
              run_id: "run-incomplete-bundle",
              run_attempt_id: "run-incomplete-bundle",
              status: "planned",
              intent_revision_id: "intent-incomplete-bundle",
              plan_patch_ref: null,
              authority_refs: {
                intent_revision_id: "intent-incomplete-bundle",
                authority_context_ref: "context-incomplete-bundle",
                planner_proposal_id: "proposal-incomplete-bundle",
                proposal_admission_id: "admission-incomplete-bundle",
                plan_revision_id: "plan-incomplete-bundle",
                accepted_transition_id: "transition-incomplete-bundle",
              },
              authority_context: {},
              planner_proposal: {},
              proposal_admission_record: {},
              plan_revision: { supersedes_plan_revision_id: null },
            };
            const bundleFields = [
              "authority_context",
              "planner_proposal",
              "proposal_admission_record",
              "plan_revision",
            ];
            const errors = bundleFields.map((field) => {
              const candidate = structuredClone(complete);
              delete candidate[field];
              try {
                projectPlanResultForCustomer(candidate);
                return null;
              } catch (error) {
                return {
                  field,
                  code: error.code,
                  message: error.message,
                };
              }
            });
            console.log(JSON.stringify(errors));
            """
        )
    )

    assert errors == [
        {
            "field": field,
            "code": "planned_result_authority_bundle_incomplete",
            "message": "planned_result_authority_bundle_incomplete",
        }
        for field in (
            "authority_context",
            "planner_proposal",
            "proposal_admission_record",
            "plan_revision",
        )
    ]


def test_plan_patch_projection_requires_content_addressed_patch_and_superseding_plan():
    result = _run_typescript(
        textwrap.dedent(
            """
            const { projectPlanResultForCustomer } = await import(
              "./app/api/_conversationStore.ts"
            );
            const patchRef = `plan-patch:sha256:${"a".repeat(64)}`;
            const value = {
              schema_version: "single-authority-phase02.v2",
              run_id: "run-plan-patch",
              run_attempt_id: "run-plan-patch",
              status: "planned",
              intent_revision_id: "intent-plan-patch",
              plan_patch_ref: patchRef,
              decision_ledger_position: 1,
              decision_refs: ["decision-plan-patch"],
              authority_refs: {
                intent_revision_id: "intent-plan-patch",
                authority_context_ref: "context-plan-patch",
                planner_proposal_id: "proposal-plan-patch",
                proposal_admission_id: "admission-plan-patch",
                plan_revision_id: "plan-plan-patch-v2",
                accepted_transition_id: "transition-plan-patch",
              },
              authority_context: {},
              planner_proposal: {},
              proposal_admission_record: {},
              plan_revision: {
                supersedes_plan_revision_id: "plan-plan-patch-v1",
              },
            };
            const accepted = projectPlanResultForCustomer(value);
            const missingPatch = structuredClone(value);
            missingPatch.plan_patch_ref = null;
            const initialWithPatch = structuredClone(value);
            initialWithPatch.plan_revision.supersedes_plan_revision_id = null;
            console.log(JSON.stringify({
              accepted,
              missingPatch: projectPlanResultForCustomer(missingPatch),
              initialWithPatch: projectPlanResultForCustomer(initialWithPatch),
            }));
            """
        )
    )

    assert result["accepted"]["plan_patch_ref"].startswith("plan-patch:sha256:")
    assert (
        result["accepted"]["plan_revision"]["supersedes_plan_revision_id"]
        == "plan-plan-patch-v1"
    )
    assert result["missingPatch"] is None
    assert result["initialWithPatch"] is None


def test_planned_agent_core_projection_rejects_null_plan_projection():
    errors = _run_typescript(
        textwrap.dedent(
            """
            const { projectAgentCoreForCustomer } = await import(
              "./app/api/_conversationStore.ts"
            );
            const invalidPlans = [
              null,
              {
                schema_version: "single-authority-phase01.v1",
                status: "planned",
              },
              {
                schema_version: "single-authority-phase02.v2",
                status: "planned",
                run_id: "run-invalid-plan",
                run_attempt_id: "run-invalid-plan",
                intent_revision_id: "intent-invalid-plan",
                plan_patch_ref: null,
                authority_refs: {},
              },
            ];
            const errors = invalidPlans.map((plan_result) => {
              try {
                projectAgentCoreForCustomer({
                  status: "planned",
                  result: {
                    run_id: "run-invalid-plan",
                    turn_id: "turn-invalid-plan",
                    topic_id: "topic-invalid-plan",
                    status: "planned",
                    plan_result,
                  },
                });
                return null;
              } catch (error) {
                return { code: error.code, message: error.message };
              }
            });
            console.log(JSON.stringify(errors));
            """
        )
    )

    assert (
        errors
        == [
            {
                "code": "planned_result_authority_refs_invalid",
                "message": "planned_result_authority_refs_invalid",
            }
        ]
        * 3
    )


def test_planned_sse_rejects_incomplete_persisted_authority_bundle():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            const bundleFields = [
              "authority_context",
              "planner_proposal",
              "proposal_admission_record",
              "plan_revision",
            ];
            globalThis.__wajeConversationPool = {
              async query(statement, params = []) {
                const runId = String(params[0] || "");
                if (statement.includes("JOIN waje_runtime.investigation_threads")) {
                  return { rows: [{
                    run_id: runId,
                    thread_id: "thread-incomplete-bundle",
                    status: "planned",
                    created_at: "2026-07-18T00:00:00.000Z",
                    owner_id: "local-user",
                  }] };
                }
                if (statement.includes("FROM waje_runtime.audit_events")) {
                  return { rows: [] };
                }
                if (statement.includes("FROM waje_runtime.run_nodes")) {
                  return { rows: [] };
                }
                if (statement.includes("LEFT JOIN waje_runtime.plan_revisions")) {
                  const missingField = bundleFields.find((field) =>
                    runId.endsWith(field)
                  );
                  const row = {
                    run_id: runId,
                    run_attempt_id: runId,
                    plan_result_refs: {
                      schema_version: "single-authority-phase02.v2",
                      plan_patch_ref: null,
                      intent_revision_id: "intent-incomplete-bundle",
                      authority_context_ref: "context-incomplete-bundle",
                      planner_proposal_id: "proposal-incomplete-bundle",
                      proposal_admission_id: "admission-incomplete-bundle",
                      plan_revision_id: "plan-incomplete-bundle",
                      accepted_transition_id: "transition-incomplete-bundle",
                    },
                    authority_context: {},
                    planner_proposal: {},
                    proposal_admission_record: {},
                    plan_revision: { supersedes_plan_revision_id: null },
                    superseded_plan_revision_id: null,
                    accepted_transition_id: "transition-incomplete-bundle",
                    accepted_node_name: "compile_authoritative_plan",
                    decision_ledger_position: 1,
                  };
                  row[missingField] = null;
                  return { rows: [row] };
                }
                throw new Error(`unexpected_statement:${statement}`);
              },
            };
            const { runEvents } = require(out + "/app/api/_conversationStore.js");
            (async () => {
              const errors = [];
              for (const field of bundleFields) {
                try {
                  await runEvents(`run-incomplete-${field}`, "local-user");
                  errors.push({ field, code: null });
                } catch (error) {
                  errors.push({ field, code: error.code, message: error.message });
                }
              }
              console.log(JSON.stringify(errors));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://phase02-contract"},
    )

    assert result == [
        {
            "field": field,
            "code": "planned_result_authority_bundle_incomplete",
            "message": "planned_result_authority_bundle_incomplete",
        }
        for field in (
            "authority_context",
            "planner_proposal",
            "proposal_admission_record",
            "plan_revision",
        )
    ]


def test_planned_terminalizes_owned_dispatch_in_memory_store():
    result = _run_typescript(
        textwrap.dedent(
            """
            const store = await import("./app/api/_conversationStore.ts");
            const thread = await store.createThread("phase02-owner");
            const claim = await store.claimRunDispatchRequest({
              producerKind: "thread_message",
              scopeRef: thread.id,
              requestIdentity: "phase02-request",
              threadId: thread.id,
              text: "分析付费金额变化",
              actorId: "phase02-owner",
            });
            const lease = await store.acquireRunDispatchLease({
              dispatchId: claim.dispatch.dispatchId,
              runId: claim.run.id,
            });
            const completed = await store.completeOwnedRunDispatch({
              dispatchId: claim.dispatch.dispatchId,
              runId: claim.run.id,
              ownerId: lease.ownerId,
              leaseEpoch: lease.leaseEpoch,
              runStatus: "planned",
            });
            const persisted = await store.requireRun(claim.run.id, "phase02-owner");
            const dispatch = globalThis.__wajeConversationMemoryStore.runDispatches.get(
              claim.dispatch.dispatchId,
            );
            console.log(JSON.stringify({ completed, persisted, dispatch }));
            """
        )
    )

    assert result["completed"]["status"] == "planned"
    assert result["persisted"]["status"] == "planned"
    assert result["dispatch"]["state"] == "terminal"
    assert result["dispatch"]["terminalStatus"] == "planned"


def test_clarification_route_admits_same_run_dispatch_then_continues_after_response():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const fs = require("fs");
            const path = require("path");
            const out = process.env.GATEWAY_OUT;
            const store = require(out + "/app/api/_conversationStore.js");
            const route = require(
              out + "/app/api/runs/[runId]/clarifications/route.js"
            );
            (async () => {
              const thread = await store.createThread("local-user");
              const run = await store.createRun(thread.id, "local-user");
              globalThis.__wajeConversationMemoryStore.runs.get(run.id).status =
                "waiting_for_clarification";
              store.recordCustomerRunStateFromAgentResult(run.id, {
                clarification: {
                  question: "请选择比较基线",
                  status: "waiting",
                  recommendation_reason: "默认使用上一日。",
                  options: [
                    { option_id: "comparison_baseline.previous_day", label: "上一日", description: "与上一日比较", recommended: true },
                    { option_id: "comparison_baseline.previous_week", label: "上周同日", description: "与上周同日比较", recommended: false },
                    { option_id: "tell_agent_differently", label: "其他方式", description: "告诉分析助手其他比较方式", recommended: false },
                  ],
                },
              });
              const response = await route.POST(
                new NextRequest(
                  `http://localhost/api/runs/${run.id}/clarifications`,
                  {
                    method: "POST",
                    headers: {
                      "content-type": "application/json",
                      "idempotency-key": "phase02-option-request",
                    },
                    body: JSON.stringify({
                      answer: "采用上一日作为比较基线",
                      selectedOptionId: "comparison_baseline.previous_day",
                      requestIdentity: "phase02-option-request",
                    }),
                  },
                ),
                { params: Promise.resolve({ runId: run.id }) },
              );
              const body = await response.json();
              const memory = globalThis.__wajeConversationMemoryStore;
              const dispatch = structuredClone(
                [...memory.runDispatches.values()][0],
              );
              fs.writeFileSync(
                path.join(
                  process.env.WAJE_GATEWAY_TEST_TMP,
                  "phase02-http-response-observed",
                ),
                "observed",
              );
              const invocationPath = path.join(
                process.env.WAJE_GATEWAY_TEST_TMP,
                "phase02-clarification-invocation.json",
              );
              const continuedPath = path.join(
                process.env.WAJE_GATEWAY_TEST_TMP,
                "phase02-worker-continued",
              );
              const deadline = Date.now() + 2000;
              while (
                (!fs.existsSync(invocationPath) || !fs.existsSync(continuedPath))
                && Date.now() < deadline
              ) {
                await new Promise((resolve) => setTimeout(resolve, 20));
              }
              const invocation = JSON.parse(
                fs.readFileSync(invocationPath, "utf8"),
              );
              console.log(JSON.stringify({
                httpStatus: response.status,
                body,
                dispatch,
                invocation,
                workerContinued: fs.existsSync(continuedPath),
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=_planned_clarification_agent_core(),
    )

    assert result["httpStatus"] == 202
    body = result["body"]
    run_id = result["invocation"]["run_id"]
    assert set(body) == {"snapshot"}
    assert body["snapshot"]["state"]["status"] == "needs_input"
    assert body["snapshot"]["transport"]["runHandle"] == run_id
    assert body["snapshot"]["transport"]["actionHandle"] == run_id
    assert "phase02-option-request" in body["snapshot"]["transport"][
        "acceptedOperationIds"
    ]
    assert result["workerContinued"] is True
    assert result["invocation"]["clarification"] == {
        "sourceRunId": run_id,
        "resolutionId": "single-authority:phase02-option-request",
        "attemptRunId": run_id,
        "answer": "采用上一日作为比较基线",
        "selectedOptionId": "comparison_baseline.previous_day",
        "source": "user",
        "retryAttempt": False,
    }
    assert result["invocation"]["dispatch_id"] == result["dispatch"]["dispatchId"]
    assert result["dispatch"]["producerKind"] == "clarification_resolution"
    assert result["dispatch"]["scopeRef"] == run_id
    assert result["dispatch"]["runId"] == run_id
    assert result["dispatch"]["requestPayload"] == {
        "message": "采用上一日作为比较基线",
        "clarification": result["invocation"]["clarification"],
    }


def test_clarification_route_releases_exact_dispatch_when_startup_fails():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const store = require(out + "/app/api/_conversationStore.js");
            const route = require(
              out + "/app/api/runs/[runId]/clarifications/route.js"
            );
            (async () => {
              const thread = await store.createThread("local-user");
              const run = await store.createRun(thread.id, "local-user");
              globalThis.__wajeConversationMemoryStore.runs.get(run.id).status =
                "waiting_for_clarification";
              store.recordCustomerRunStateFromAgentResult(run.id, {
                clarification: {
                  question: "请选择比较基线",
                  status: "waiting",
                  recommendation_reason: "默认使用上一日。",
                  options: [
                    { option_id: "comparison_baseline.previous_day", label: "上一日", description: "与上一日比较", recommended: true },
                    { option_id: "comparison_baseline.previous_week", label: "上周同日", description: "与上周同日比较", recommended: false },
                    { option_id: "tell_agent_differently", label: "其他方式", description: "告诉分析助手其他比较方式", recommended: false },
                  ],
                },
              });
              const response = await route.POST(
                new NextRequest(
                  `http://localhost/api/runs/${run.id}/clarifications`,
                  {
                    method: "POST",
                    headers: {
                      "content-type": "application/json",
                      "idempotency-key": "phase02-startup-failure",
                    },
                    body: JSON.stringify({
                      answer: "采用上一日作为比较基线",
                      selectedOptionId: "comparison_baseline.previous_day",
                      requestIdentity: "phase02-startup-failure",
                    }),
                  },
                ),
                { params: Promise.resolve({ runId: run.id }) },
              );
              const memory = globalThis.__wajeConversationMemoryStore;
              const dispatches = [...memory.runDispatches.values()];
              console.log(JSON.stringify({
                httpStatus: response.status,
                body: await response.json(),
                run: memory.runs.get(run.id),
                dispatches,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=textwrap.dedent(
            """\
            #!/usr/bin/env python3
            raise SystemExit(7)
            """
        ),
    )

    assert result["httpStatus"] == 503
    assert result["body"]["error"] == {
        "code": "analysis_unavailable",
        "title": "分析服务暂时无法完成请求",
        "message": "真实故障已经记录。请稍后重试；若问题持续，可联系支持定位。",
        "recovery": "retry",
    }
    assert result["body"]["transport"]["technicalDetailRef"].startswith(
        "customer-error-"
    )
    assert len(result["dispatches"]) == 1
    dispatch = result["dispatches"][0]
    assert dispatch["runId"] == result["run"]["id"]
    assert result["run"]["status"] == "waiting_for_clarification"
    assert dispatch["state"] == "pending"
    assert dispatch["ownerId"] is None
    assert dispatch["leaseExpiresAt"] is None
    assert dispatch["heartbeatAt"] is None
    assert dispatch["terminalStatus"] is None
    assert dispatch["failureReason"] is None


def test_clarification_route_observes_post_ack_exit_on_exact_dispatch():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const store = require(out + "/app/api/_conversationStore.js");
            const route = require(
              out + "/app/api/runs/[runId]/clarifications/route.js"
            );
            (async () => {
              const thread = await store.createThread("local-user");
              const run = await store.createRun(thread.id, "local-user");
              globalThis.__wajeConversationMemoryStore.runs.get(run.id).status =
                "waiting_for_clarification";
              store.recordCustomerRunStateFromAgentResult(run.id, {
                clarification: {
                  question: "请选择比较基线",
                  status: "waiting",
                  recommendation_reason: "默认使用上一日。",
                  options: [
                    { option_id: "comparison_baseline.previous_day", label: "上一日", description: "与上一日比较", recommended: true },
                    { option_id: "comparison_baseline.previous_week", label: "上周同日", description: "与上周同日比较", recommended: false },
                    { option_id: "tell_agent_differently", label: "其他方式", description: "告诉分析助手其他比较方式", recommended: false },
                  ],
                },
              });
              const response = await route.POST(
                new NextRequest(
                  `http://localhost/api/runs/${run.id}/clarifications`,
                  {
                    method: "POST",
                    headers: {
                      "content-type": "application/json",
                      "idempotency-key": "phase02-post-ack-exit",
                    },
                    body: JSON.stringify({
                      answer: "采用上一日作为比较基线",
                      selectedOptionId: "comparison_baseline.previous_day",
                      requestIdentity: "phase02-post-ack-exit",
                    }),
                  },
                ),
                { params: Promise.resolve({ runId: run.id }) },
              );
              const body = await response.json();
              const memory = globalThis.__wajeConversationMemoryStore;
              const dispatchId = [...memory.runDispatches.keys()][0];
              const deadline = Date.now() + 2000;
              while (
                memory.runDispatches.get(dispatchId)?.state !== "pending"
                && Date.now() < deadline
              ) {
                await new Promise((resolve) => setTimeout(resolve, 20));
              }
              console.log(JSON.stringify({
                httpStatus: response.status,
                body,
                run: memory.runs.get(run.id),
                dispatch: memory.runDispatches.get(dispatchId),
                dispatchIds: [...memory.runDispatches.keys()],
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import time

            os.write(
                int(os.environ["WAJE_AGENT_CORE_STARTUP_ACK_FD"]),
                b"WAJE_AGENT_CORE_RUNNING\\n",
            )
            time.sleep(0.05)
            raise SystemExit(7)
            """
        ),
    )

    assert result["httpStatus"] == 202
    assert result["body"]["snapshot"]["state"]["status"] == "needs_input"
    assert "phase02-post-ack-exit" in result["body"]["snapshot"]["transport"][
        "acceptedOperationIds"
    ]
    assert len(result["dispatchIds"]) == 1
    assert result["run"]["status"] == "waiting_for_clarification"
    assert result["dispatch"]["state"] == "pending"
    assert result["dispatch"]["ownerId"] is None
    assert result["dispatch"]["leaseExpiresAt"] is None
    assert result["dispatch"]["heartbeatAt"] is None
    assert result["dispatch"]["terminalStatus"] is None
    assert result["dispatch"]["failureReason"] is None


def test_clarification_route_rejects_choice_alias_and_inexact_body():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const out = process.env.GATEWAY_OUT;
            const route = require(
              out + "/app/api/runs/[runId]/clarifications/route.js"
            );
            (async () => {
              const legacy = await route.POST(
                new NextRequest(
                  "http://localhost/api/runs/run-legacy/clarifications",
                  {
                    method: "POST",
                    headers: { "content-type": "application/json" },
                    body: JSON.stringify({
                      choice: "跟前一天比较",
                      selectedOptionId: "comparison_baseline.previous_day",
                    }),
                  },
                ),
                { params: Promise.resolve({ runId: "run-legacy" }) },
              );
              const missingField = await route.POST(
                new NextRequest(
                  "http://localhost/api/runs/run-inexact/clarifications",
                  {
                    method: "POST",
                    headers: { "content-type": "application/json" },
                    body: JSON.stringify({ answer: "跟前一天比较" }),
                  },
                ),
                { params: Promise.resolve({ runId: "run-inexact" }) },
              );
              console.log(JSON.stringify({
                legacy: {
                  status: legacy.status,
                  body: await legacy.json(),
                },
                missingField: {
                  status: missingField.status,
                  body: await missingField.json(),
                },
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
    )

    for response in result.values():
        assert response["status"] == 400
        assert response["body"]["error"] == {
            "code": "request_invalid",
            "title": "提交内容无法处理",
            "message": "请检查当前输入后再次提交。",
            "recovery": "retry",
        }
        assert response["body"]["transport"]["technicalDetailRef"].startswith(
            "customer-error-"
        )


def test_phase02_plan_result_reaches_gateway_snapshot_and_workbench():
    helper = (ROOT / "app/api/_agentCore.ts").read_text(encoding="utf-8")
    store = (ROOT / "app/api/_conversationStore.ts").read_text(encoding="utf-8")
    message_route = (ROOT / "app/api/threads/[threadId]/messages/route.ts").read_text(
        encoding="utf-8"
    )
    clarification_route = (
        ROOT / "app/api/runs/[runId]/clarifications/route.ts"
    ).read_text(encoding="utf-8")
    workbench = (ROOT / "app/api/agent-runs/route.ts").read_text(encoding="utf-8")
    page = (ROOT / "app/page.tsx").read_text(encoding="utf-8")

    assert 'value === "planned"' in helper
    assert 'terminalStatus === "planned"' in message_route
    assert 'agentCore.status !== "started"' in clarification_route
    assert "loadCustomerAnalysisSnapshot" in clarification_route
    assert "forceInline" not in clarification_route
    assert "material_revision_required" not in clarification_route
    assert 'event: "plan_result_ready"' in store
    assert "waje_runtime.plan_revisions" in store
    assert "waje_runtime.plan_revision_supersessions" in store
    assert "raw_provider_response_ref" in store
    assert "snapshot.state.status" in page
    assert 'snapshot?.state.status === "working"' in page
    assert "planTextFromResult" not in page
    assert "snapshotFromPayload(await responsePayload(response))" in page
    assert 'status === "planned"' in workbench


def _planned_clarification_agent_core() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json
        import os
        import sys
        import time

        run_id = sys.argv[sys.argv.index("--run-id") + 1]
        dispatch_id = sys.argv[sys.argv.index("--dispatch-id") + 1]
        clarification = json.loads(
            sys.argv[sys.argv.index("--clarification") + 1]
        )
        invocation_path = os.path.join(
            os.environ["WAJE_GATEWAY_TEST_TMP"],
            "phase02-clarification-invocation.json",
        )
        with open(invocation_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "run_id": run_id,
                    "dispatch_id": dispatch_id,
                    "clarification": clarification,
                },
                handle,
                ensure_ascii=False,
            )
        os.write(
            int(os.environ["WAJE_AGENT_CORE_STARTUP_ACK_FD"]),
            b"WAJE_AGENT_CORE_RUNNING\\n",
        )
        response_path = os.path.join(
            os.environ["WAJE_GATEWAY_TEST_TMP"],
            "phase02-http-response-observed",
        )
        deadline = time.monotonic() + 2
        while not os.path.exists(response_path) and time.monotonic() < deadline:
            time.sleep(0.01)
        if os.path.exists(response_path):
            with open(
                os.path.join(
                    os.environ["WAJE_GATEWAY_TEST_TMP"],
                    "phase02-worker-continued",
                ),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("continued")
        time.sleep(0.1)
        """
    )
