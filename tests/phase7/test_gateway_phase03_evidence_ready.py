from __future__ import annotations

import json
from pathlib import Path
import textwrap

from tests.phase7.test_authoritative_execution_result import _result
from tests.phase7.test_gateway_route_runtime import _compiled_gateway_run
from tests.phase7.test_gateway_typescript_contract import _run_typescript


ROOT = Path(__file__).resolve().parents[2]


def _execution_result_payload() -> dict:
    return _result().to_dict()


def test_gateway_validates_execution_authority_and_projects_a_fixed_customer_shape():
    payload = json.dumps(_execution_result_payload(), ensure_ascii=False)
    result = _run_typescript(
        textwrap.dedent(
            f"""
            const {{
              isValidAuthoritativeExecutionResult,
              parseAgentCoreOutput,
            }} = await import("./app/api/_agentCore.ts");
            const {{ projectAgentCoreForCustomer }} = await import(
              "./app/api/_conversationStore.ts"
            );
            const executionResult = {payload};
            const coverageDigest = "a".repeat(64);
            const claimCoverage = {{
              schema_version: "claim-coverage-checkpoint.v1",
              source_plan_revision_id: executionResult.plan_revision_id,
              source_execution_result_ref:
                executionResult.authoritative_execution_result_ref,
              claim_coverage_checkpoint_ref:
                `claim-coverage-checkpoint:sha256:${{coverageDigest}}`,
              claim_coverage_checkpoint_digest: coverageDigest,
              claim_coverage_evaluation_ref: "claim-coverage-evaluation:test",
              plan_expansion_decision_ref: "plan-expansion-decision:test",
              decision: "seal",
              plan_patch_ref: null,
              accepted_transition_id: "transition-claim-coverage",
            }};
            const coreResult = {{
              status: "evidence_ready",
              run_id: executionResult.run_attempt_id,
              turn_id: "turn-phase03-gateway",
              topic_id: null,
              context_manifest: {{}},
              execution_result: executionResult,
              claim_coverage: claimCoverage,
            }};
            const parsed = parseAgentCoreOutput(JSON.stringify(coreResult));
            const visible = projectAgentCoreForCustomer({{
              status: "evidence_ready",
              command: "private-command",
              output: "private-stdout",
              result: coreResult,
            }});
            const extra = structuredClone(executionResult);
            extra.future_internal_field = true;
            const brokenClosure = structuredClone(executionResult);
            brokenClosure.execution_snapshot.outcome_refs = [];
            console.log(JSON.stringify({{
              accepted: isValidAuthoritativeExecutionResult(
                executionResult,
                executionResult.run_attempt_id,
              ),
              parsedStatus: parsed.status,
              visible,
              extraAccepted: isValidAuthoritativeExecutionResult(
                extra,
                executionResult.run_attempt_id,
              ),
              brokenClosureAccepted: isValidAuthoritativeExecutionResult(
                brokenClosure,
                executionResult.run_attempt_id,
              ),
            }}));
            """
        )
    )

    assert result["accepted"] is True
    assert result["parsedStatus"] == "evidence_ready"
    assert result["extraAccepted"] is False
    assert result["brokenClosureAccepted"] is False
    execution = result["visible"]["result"]["execution_result"]
    assert set(execution) == {
        "schema_version",
        "status",
        "result_ref",
        "plan_revision_id",
        "execution_snapshot_ref",
        "tasks",
        "outcomes",
        "obligations",
        "evidence",
        "failures",
        "limitations",
        "stop",
    }
    assert execution["status"] == "evidence_ready"
    assert len(execution["tasks"]) == 2
    assert len(execution["outcomes"]) == 2
    assert len(execution["evidence"]) == 1
    assert len(execution["failures"]) == 1
    assert set(execution["evidence"][0]) == {
        "evidence_entry_ref",
        "evidence_ref",
        "task_id",
        "outcome_ref",
        "status",
        "evidence_kind",
        "data_contract_state",
        "supported_claim_kinds",
        "evidence_strength",
        "maximum_claim_strength",
        "scope",
        "window_refs",
        "result_refs",
        "completeness_report_refs",
        "dimension_path",
        "hierarchy_qualified",
        "limitation_refs",
    }
    assert set(execution["failures"][0]) == {
        "failure_ref",
        "task_id",
        "scope",
        "integrity_level",
        "retryability",
        "user_actionable",
        "business_boundary",
    }
    serialized = json.dumps(result["visible"], ensure_ascii=False)
    for private_field_or_value in (
        "observation_facts",
        "output_payload",
        "raw_rows",
        "internal-row",
        "provider_ref",
        "model_ref",
        "technical_detail_ref",
        "provider-debug-owner-42",
        "content_digest",
        "bundle_set_digest",
        "owner_id",
        "internal-owner",
        "debug",
        "private-command",
        "private-stdout",
    ):
        assert private_field_or_value not in serialized


def test_phase03_gateway_routes_and_ui_declare_evidence_ready_terminal_delivery():
    sources = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "app/api/_conversationStore.ts",
            "app/api/threads/[threadId]/messages/route.ts",
            "app/api/runs/[runId]/clarifications/route.ts",
            "app/page.tsx",
        )
    }

    store = sources["app/api/_conversationStore.ts"]
    assert 'event: "execution_result_ready"' in store
    for route in ("app/api/threads/[threadId]/messages/route.ts",):
        assert 'terminalStatus === "evidence_ready"' in sources[route]
    clarification_route = sources["app/api/runs/[runId]/clarifications/route.ts"]
    assert 'agentCore.status !== "started"' in clarification_route
    assert "loadCustomerAnalysisSnapshot" in clarification_route
    assert "rawResult" not in clarification_route
    page = sources["app/page.tsx"]
    assert "snapshot.state.status" in page
    assert 'snapshot?.state.status === "working"' in page
    assert "executionTextFromResult" not in page


def test_waiting_run_events_project_typed_clarification_options_for_resume():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            globalThis.__wajeConversationPool = {
              async query(statement) {
                if (statement.includes(
                  "JOIN waje_runtime.investigation_threads"
                )) {
                  return { rows: [{
                    run_id: "run-phase03-clarification",
                    thread_id: "thread-phase03-clarification",
                    status: "waiting_for_clarification",
                    created_at: "2026-07-18T08:00:00.000Z",
                    owner_id: "local-user",
                  }] };
                }
                if (statement.includes(
                  "FROM waje_runtime.audit_events"
                )) {
                  return { rows: [{
                    event_type: "clarification_state_saved",
                    payload: {
                      run_id: "run-phase03-clarification",
                      topic_id: "topic-phase03-clarification",
                      question: "目标日期要跟哪个基准比较？",
                      status: "waiting",
                      answer: "private-previous-answer",
                      internal_owner: "private-clarification-owner",
                      debug: "private-clarification-debug",
                      options: [{
                        option_id: "comparison_baseline.previous_day",
                        label: "跟前一天比较（推荐）",
                        description: "用于日变化解释。",
                        recommended: true,
                      }, {
                        option_id:
                          "comparison_baseline.rolling_7_day_baseline",
                        label: "跟过去七天比较",
                        description: "用于平滑短期波动。",
                        recommended: false,
                      }, {
                        option_id: "tell_agent_differently",
                        label: "告诉分析助手采用其他方式",
                        description: "自由说明业务选择。",
                        recommended: false,
                      }],
                    },
                    created_at: "2026-07-18T08:00:01.000Z",
                  }] };
                }
                if (statement.includes("FROM waje_runtime.run_nodes")) {
                  return { rows: [] };
                }
                throw new Error(`unexpected_statement:${statement}`);
              },
            };
            const { runEvents } = require(
              out + "/app/api/_conversationStore.js"
            );
            (async () => {
              const events = await runEvents(
                "run-phase03-clarification",
                "local-user",
              );
              console.log(JSON.stringify(events));
            })().catch((error) => {
              console.error(error);
              process.exit(1);
            });
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://phase03-contract"},
    )

    clarification = next(
        event for event in result if event["event"] == "clarification_state_saved"
    )
    assert clarification["payload"] == {
        "run_id": "run-phase03-clarification",
        "topic_id": "topic-phase03-clarification",
        "question": "目标日期要跟哪个基准比较？",
        "status": "waiting",
        "options": [
            {
                "option_id": "comparison_baseline.previous_day",
                "label": "跟前一天比较（推荐）",
                "description": "用于日变化解释。",
                "recommended": True,
            },
            {
                "option_id": ("comparison_baseline.rolling_7_day_baseline"),
                "label": "跟过去七天比较",
                "description": "用于平滑短期波动。",
                "recommended": False,
            },
            {
                "option_id": "tell_agent_differently",
                "label": "告诉分析助手采用其他方式",
                "description": "自由说明业务选择。",
                "recommended": False,
            },
        ],
    }
    serialized = json.dumps(clarification, ensure_ascii=False)
    for private_field_or_value in (
        "answer",
        "private-previous-answer",
        "internal_owner",
        "private-clarification-owner",
        "debug",
        "private-clarification-debug",
        "provider_ref",
        "private-option-provider",
    ):
        assert private_field_or_value not in serialized


def test_waiting_projection_rejects_legacy_clarification_option_aliases():
    result = _run_typescript(
        textwrap.dedent(
            """
            const { projectAgentCoreForCustomer } = await import(
              "./app/api/_conversationStore.ts"
            );
            const clarification = {
              status: "question_tool_opened",
              question: "目标日期要跟哪个基准比较？",
              options: [{
                option_id: "comparison_baseline.previous_day",
                label: "跟前一天比较（推荐）",
                description: "用于日变化解释。",
                recommended: true,
                typed_value: { baseline_id: "previous_day" },
              }, {
                option_id: "tell_agent_differently",
                label: "告诉分析助手采用其他方式",
                description: "自由说明业务选择。",
                recommended: false,
              }],
              recommendation_reason: "上一日最适合解释日变化。",
            };
            const wrapper = (value) => ({
              status: "waiting_for_clarification",
              result: {
                run_id: "run-strict-clarification",
                turn_id: "turn-strict-clarification",
                topic_id: "topic-strict-clarification",
                status: "waiting_for_clarification",
                clarification: value,
              },
            });
            const visible = projectAgentCoreForCustomer(wrapper(clarification));
            const legacy = structuredClone(clarification);
            legacy.options[0].id = legacy.options[0].option_id;
            let legacyError = null;
            try {
              projectAgentCoreForCustomer(wrapper(legacy));
            } catch (error) {
              legacyError = error.code;
            }
            console.log(JSON.stringify({ visible, legacyError }));
            """
        )
    )

    assert result["visible"]["result"]["clarification"] == {
        "question": "目标日期要跟哪个基准比较？",
        "status": "question_tool_opened",
        "allow_freeform": True,
        "options": [
            {
                "option_id": "comparison_baseline.previous_day",
                "label": "跟前一天比较（推荐）",
                "description": "用于日变化解释。",
                "recommended": True,
            },
            {
                "option_id": "tell_agent_differently",
                "label": "告诉分析助手采用其他方式",
                "description": "自由说明业务选择。",
                "recommended": False,
            },
        ],
        "recommendation_reason": "上一日最适合解释日变化。",
    }
    assert result["legacyError"] == "clarification_payload_invalid"


def test_evidence_ready_terminalizes_owned_gateway_dispatch():
    result = _run_typescript(
        textwrap.dedent(
            """
            const store = await import("./app/api/_conversationStore.ts");
            const thread = await store.createThread("phase03-owner");
            const claim = await store.claimRunDispatchRequest({
              producerKind: "thread_message",
              scopeRef: thread.id,
              requestIdentity: "phase03-request",
              threadId: thread.id,
              text: "执行当前权威计划",
              actorId: "phase03-owner",
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
              runStatus: "evidence_ready",
            });
            const persisted = await store.requireRun(
              claim.run.id,
              "phase03-owner",
            );
            console.log(JSON.stringify({ completed, persisted }));
            """
        )
    )

    assert result["completed"]["status"] == "evidence_ready"
    assert result["persisted"]["status"] == "evidence_ready"


def test_evidence_ready_sse_rebuilds_authority_and_allowlists_every_customer_event():
    result_record = _result(evidence_kind="boundary")
    payload = json.dumps(result_record.to_dict(), ensure_ascii=False)
    result = _compiled_gateway_run(
        textwrap.dedent(
            f"""
            const out = process.env.GATEWAY_OUT;
            const execution = {payload};
            const bundles = execution.capability_outcome_bundles;
            const planTransitionId =
              execution.durable_transition.parent_transition_id;
            const planResultRefs = {{
              schema_version: "single-authority-phase02.v2",
              plan_patch_ref: null,
              intent_revision_id: execution.intent_revision_id,
              authority_context_ref: execution.authority_context_ref,
              planner_proposal_id:
                execution.plan_revision.planner_proposal_ref,
              proposal_admission_id:
                execution.plan_revision.proposal_admission_ref,
              plan_revision_id: execution.plan_revision_id,
              accepted_transition_id: planTransitionId,
            }};
            const coverageDigest = "a".repeat(64);
            const claimCoverageRefs = {{
              schema_version: "claim-coverage-checkpoint.v1",
              source_plan_revision_id: execution.plan_revision_id,
              source_execution_result_ref:
                execution.authoritative_execution_result_ref,
              claim_coverage_checkpoint_ref:
                `claim-coverage-checkpoint:sha256:${{coverageDigest}}`,
              claim_coverage_checkpoint_digest: coverageDigest,
              claim_coverage_evaluation_ref: "claim-coverage-evaluation:test",
              plan_expansion_decision_ref: "plan-expansion-decision:test",
              decision: "seal",
              plan_patch_ref: null,
              accepted_transition_id: "transition-claim-coverage",
            }};
            const executionResultRefs = {{
              schema_version: execution.schema_version,
              authoritative_execution_result_ref:
                execution.authoritative_execution_result_ref,
              intent_revision_id: execution.intent_revision_id,
              authority_context_ref: execution.authority_context_ref,
              plan_revision_id: execution.plan_revision_id,
              execution_snapshot_ref: execution.execution_snapshot_ref,
              stop_ref: execution.stop_ref,
              accepted_transition_id: execution.transition_id,
            }};
            const transitionInputPayload = {{
              plan_revision_id: execution.plan_revision_id,
              plan_digest: execution.plan_revision.content_digest,
              authority_context_ref: execution.authority_context_ref,
              budget_policy_ref: execution.plan_revision.budget_policy_ref,
              hard_budget_limit:
                execution.exploration_stop_record.hard_budget_limit,
              capability_tasks: [...execution.plan_revision.capability_tasks]
                .sort((left, right) =>
                  left.task_id.localeCompare(right.task_id)
                )
                .map((task) => ({{
                  task_id: task.task_id,
                  idempotency_key: task.idempotency_key,
                }})),
            }};
            let transitionTamper = "none";
            globalThis.__wajeConversationPool = {{
              async query(statement, params = []) {{
                const runId = String(params[0] || "");
                if (statement.includes("JOIN waje_runtime.investigation_threads")) {{
                  return {{ rows: [{{
                    run_id: runId,
                    thread_id: "thread-phase03-sse",
                    status: "evidence_ready",
                    request: {{
                      plan_result_refs: planResultRefs,
                      execution_result_refs: executionResultRefs,
                      claim_coverage_refs: claimCoverageRefs,
                    }},
                    created_at: "2026-07-18T08:00:00.000Z",
                    owner_id: "local-user",
                  }}] }};
                }}
                if (statement.includes("FROM waje_runtime.audit_events")) {{
                  return {{ rows: [{{
                    event_type: "capability_execution_settled",
                    payload: {{
                      status: "evidence_ready",
                      schema_version: execution.schema_version,
                      authoritative_execution_result_ref:
                        execution.authoritative_execution_result_ref,
                      intent_revision_id: execution.intent_revision_id,
                      authority_context_ref: execution.authority_context_ref,
                      plan_revision_id: execution.plan_revision_id,
                      execution_snapshot_ref: execution.execution_snapshot_ref,
                      stop_ref: execution.stop_ref,
                      accepted_transition_id: execution.transition_id,
                      raw_rows: [{{ secret: "private-audit-row" }}],
                      provider_ref: "private-audit-provider",
                      debug: "private-audit-debug",
                    }},
                    created_at: "2026-07-18T08:00:01.000Z",
                  }}] }};
                }}
                if (statement.includes("FROM waje_runtime.run_nodes")) {{
                  return {{ rows: [{{
                    node_name: "execute_capability_dag",
                    status: "completed",
                    payload: {{
                      observation_facts: [{{ secret: "private-node-fact" }}],
                      output_payload: {{ secret: "private-node-output" }},
                      owner: "private-node-owner",
                    }},
                    started_at: "2026-07-18T08:00:00.000Z",
                    finished_at: "2026-07-18T08:00:01.000Z",
                  }}] }};
                }}
                if (statement.includes("r.request -> 'plan_result_refs'")) {{
                  return {{ rows: [{{
                    run_id: runId,
                    run_attempt_id: runId,
                    plan_result_refs: planResultRefs,
                    authority_context: {{
                      authority_context_ref: execution.authority_context_ref,
                      run_attempt_id: runId,
                    }},
                    planner_proposal: {{
                      planner_proposal_id:
                        execution.plan_revision.planner_proposal_ref,
                      run_attempt_id: runId,
                      intent_revision_id: execution.intent_revision_id,
                      authority_context_ref: execution.authority_context_ref,
                    }},
                    proposal_admission_record: {{
                      proposal_admission_id:
                        execution.plan_revision.proposal_admission_ref,
                      planner_proposal_ref:
                        execution.plan_revision.planner_proposal_ref,
                      intent_revision_id: execution.intent_revision_id,
                      authority_context_ref: execution.authority_context_ref,
                    }},
                    plan_revision: execution.plan_revision,
                    superseded_plan_revision_id: null,
                    accepted_transition_id: planTransitionId,
                    accepted_node_name: "compile_authoritative_plan",
                    decision_ledger_position: 1,
                  }}] }};
                }}
                if (statement.includes(
                  "LEFT JOIN waje_runtime.capability_execution_snapshots"
                )) {{
                  return {{ rows: [{{
                    run_id: runId,
                    run_attempt_id: runId,
                    run_status: "evidence_ready",
                    execution_result_refs: executionResultRefs,
                    claim_coverage_refs: claimCoverageRefs,
                    plan_revision: execution.plan_revision,
                    superseded_plan_revision_id: null,
                    execution_snapshot: execution.execution_snapshot,
                    exploration_stop_record: execution.exploration_stop_record,
                    accepted_transition_id: execution.transition_id,
                    transition_attempt_id: execution.durable_transition.attempt_id,
                    transition_node_name: "execute_capability_dag",
                    transition_parent_transition_id:
                      execution.durable_transition.parent_transition_id,
                    transition_run_attempt_id: runId,
                    transition_intent_revision_id: execution.intent_revision_id,
                    transition_decision_ledger_position: 1,
                    transition_input_digest:
                      execution.durable_transition.input_digest,
                    transition_output_digest:
                      execution.durable_transition.output_digest,
                    transition_input_payload: transitionTamper === "input"
                      ? {{ ...transitionInputPayload, plan_digest: "0".repeat(64) }}
                      : transitionInputPayload,
                    transition_output_payload: transitionTamper === "output"
                      ? {{
                          execution_snapshot: execution.execution_snapshot,
                          exploration_stop_record: {{
                            ...execution.exploration_stop_record,
                            reason: "no_ready_tasks",
                          }},
                        }}
                      : {{
                          execution_snapshot: execution.execution_snapshot,
                          exploration_stop_record:
                            execution.exploration_stop_record,
                        }},
                    transition_execution_attempt: 1,
                    transition_status: "succeeded",
                    transition_acceptance_state: "accepted",
                    transition_next_transition: "phase03_evidence_bound",
                    latest_accepted_transition_id: transitionTamper === "head"
                      ? "transition-newer-accepted-head"
                      : claimCoverageRefs.accepted_transition_id,
                  }}] }};
                }}
                if (statement.includes(
                  "FROM waje_runtime.capability_outcomes outcome"
                )) {{
                  return {{ rows: bundles.map((bundle) => ({{
                    attempt_payload: bundle.attempt,
                    outcome_payload: bundle.outcome,
                  }})) }};
                }}
                if (statement.includes(
                  "FROM waje_runtime.capability_evidence_ledger_entries"
                )) {{
                  return {{ rows: bundles.flatMap((bundle) =>
                    bundle.evidence_entries.map((entry) => ({{ payload: entry }}))
                  ) }};
                }}
                if (statement.includes(
                  "FROM waje_runtime.capability_failure_records"
                )) {{
                  return {{ rows: bundles.flatMap((bundle) =>
                    bundle.failure_records.map((failure) => ({{ payload: failure }}))
                  ) }};
                }}
                throw new Error(`unexpected_statement:${{statement}}`);
              }},
            }};
            const {{ runEvents }} = require(out + "/app/api/_conversationStore.js");
            (async () => {{
              const events = await runEvents(
                execution.run_attempt_id,
                "local-user",
              );
              const tamperErrors = {{}};
              for (const tamper of ["input", "output", "head"]) {{
                transitionTamper = tamper;
                try {{
                  await runEvents(execution.run_attempt_id, "local-user");
                }} catch (error) {{
                  tamperErrors[tamper] = error instanceof Error
                    ? error.message
                    : String(error);
                }}
              }}
              console.log(JSON.stringify({{ events, tamperErrors }}));
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://phase03-contract"},
    )

    events = result["events"]
    assert result["tamperErrors"] == {
        "input": "execution_result_transition_payload_mismatch",
        "output": "execution_result_transition_payload_mismatch",
        "head": "execution_result_authority_mismatch",
    }
    plan_ready = next(
        event for event in events if event["event"] == "plan_result_ready"
    )
    assert plan_ready["payload"]["status"] == "planned"
    assert (
        plan_ready["payload"]["plan_result"]["authority_refs"]["plan_revision_id"]
        == result_record.plan_revision_id
    )
    ready = next(
        event for event in events if event["event"] == "execution_result_ready"
    )
    assert ready["payload"] == {
        "status": "evidence_ready",
        "terminal": False,
        "execution_result": result_record.public_projection(),
    }
    assert ready["process"] == {
        "stage": "evidence_summary",
        "label": "证据执行完成",
        "summary": "证据执行完成，待生成结论。",
        "status": "evidence_ready",
    }
    coverage_ready = next(
        event for event in events if event["event"] == "claim_coverage_ready"
    )
    assert coverage_ready["payload"]["decision"] == "seal"
    assert coverage_ready["payload"]["terminal"] is True
    assert coverage_ready["process"]["stage"] == "claim_coverage"
    node = next(event for event in events if event["event"] == "node_process")
    assert set(node["payload"]) == {
        "node_name",
        "status",
        "started_at",
        "finished_at",
    }
    serialized = json.dumps(events, ensure_ascii=False)
    for private_value in (
        "private-audit-row",
        "private-audit-provider",
        "private-audit-debug",
        "private-node-fact",
        "private-node-output",
        "private-node-owner",
        "observation_facts",
        "output_payload",
        "technical_detail_ref",
        "provider_ref",
        "model_ref",
        "bundle_set_digest",
    ):
        assert private_value not in serialized
