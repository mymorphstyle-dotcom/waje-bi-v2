from __future__ import annotations

import json
from pathlib import Path
import textwrap

import pytest

from tests.phase7.test_authoritative_execution_result import _result
from tests.phase7.test_gateway_route_runtime import _compiled_gateway_run
from tests.phase7.test_gateway_typescript_contract import _run_typescript


ROOT = Path(__file__).resolve().parents[2]


def _customer_publication() -> dict:
    return {
        "blocks": [
            {
                "role": "executive_answer",
                "text": "昨天付费金额上升，主要贡献来自付费用户数。",
                "statement_role": "conclusion",
                "claim_refs": ["claim-accepted"],
                "recommendation_refs": [],
                "limitation_refs": ["limitation-window"],
                "material_fact_bindings": [],
            }
        ],
        "claim_refs": ["claim-accepted"],
        "field_visibility_policy_ref": "policy-customer",
        "limitation_refs": ["limitation-window"],
        "recommendation_refs": [],
        "visualization_refs": [],
        "warnings": [],
    }


def _publication_refs() -> dict:
    return {
        "authority_bundle_ref": "authority-bundle-published",
        "authority_bundle_digest": "a" * 64,
        "authority_sealed_at": "2026-07-18T07:59:58.000Z",
        "publication_ref": "publication-published",
        "publication_digest": "b" * 64,
        "published_at": "2026-07-18T08:00:02.000Z",
        "projection_id": "projection-published",
        "projection_digest": "c" * 64,
        "outbox_ref": "outbox-published",
        "delivery_status": "published",
        "delivery_attempted_at": "2026-07-18T08:00:03.000Z",
    }


def _post_execution_refs() -> dict:
    return {
        "post_execution_result_ref": "post-execution:published",
        "post_execution_result_digest": "d" * 64,
        "semantic_authority_result_ref": "semantic-authority:published",
        "semantic_authority_result_digest": "e" * 64,
        "authority_bundle_ref": "authority-bundle-published",
        "authority_bundle_digest": "a" * 64,
        "authority_transition_id": "transition-authority-published",
        "claim_coverage_checkpoint_ref": (
            "claim-coverage-checkpoint:sha256:" + "1" * 64
        ),
        "claim_coverage_checkpoint_digest": "1" * 64,
        "claim_coverage_transition_id": "transition-claim-coverage",
        "post_seal_failure_terminal_ref": None,
        "failure_record_ref": None,
        "failure_lifecycle_state_digest": None,
        "narrative_workflow_ref": "narrative-workflow:published",
        "narrative_workflow_digest": "f" * 64,
        "compose_transition_id": "transition-compose-published",
        "publication_ref": "publication-published",
        "outbox_ref": "outbox-published",
        "customer_payload_ref": "customer-payload-published",
        "delivery_attempt_ref": "delivery-attempt-published",
        "customer_publication_ref": "customer-publication-published",
    }


def _post_execution_state() -> dict:
    return {
        "post_execution_status": "completed",
        "analysis_status": "complete",
        "publication_status": "published",
        "delivery_status": "published",
        "publication_refs": _post_execution_refs(),
    }


def _intermediate_post_execution_state(status: str) -> dict:
    state = _post_execution_state()
    state["post_execution_status"] = status
    if status == "authority_sealed":
        state["publication_status"] = "not_ready"
        state["delivery_status"] = "pending"
        for field in (
            "narrative_workflow_ref",
            "narrative_workflow_digest",
            "compose_transition_id",
            "publication_ref",
            "outbox_ref",
            "customer_payload_ref",
            "delivery_attempt_ref",
            "customer_publication_ref",
        ):
            state["publication_refs"][field] = None
    elif status == "narrative_ready":
        state["publication_status"] = "ready"
        state["delivery_status"] = "persisted"
        state["publication_refs"]["delivery_attempt_ref"] = None
        state["publication_refs"]["customer_publication_ref"] = None
    else:
        raise ValueError(f"unsupported_intermediate_status:{status}")
    return state


def _completed_stage_authority_script() -> str:
    execution = json.dumps(_result().to_dict(), ensure_ascii=False)
    return textwrap.dedent(
        f"""
        const execution = {execution};
        const bundles = execution.capability_outcome_bundles;
        const planTransitionId =
          execution.durable_transition.parent_transition_id;
        const planResultRefs = {{
          schema_version: "single-authority-phase02.v2",
          plan_patch_ref: null,
          intent_revision_id: execution.intent_revision_id,
          authority_context_ref: execution.authority_context_ref,
          planner_proposal_id: execution.plan_revision.planner_proposal_ref,
          proposal_admission_id:
            execution.plan_revision.proposal_admission_ref,
          plan_revision_id: execution.plan_revision_id,
          accepted_transition_id: planTransitionId,
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
        const coverageDigest = "1".repeat(64);
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
        const withStageRefs = (postExecution) => ({{
          ...postExecution,
          plan_result_refs: planResultRefs,
          execution_result_refs: executionResultRefs,
          claim_coverage_refs: claimCoverageRefs,
        }});
        const transitionInputPayload = {{
          plan_revision_id: execution.plan_revision_id,
          plan_digest: execution.plan_revision.content_digest,
          authority_context_ref: execution.authority_context_ref,
          budget_policy_ref: execution.plan_revision.budget_policy_ref,
          hard_budget_limit:
            execution.exploration_stop_record.hard_budget_limit,
          capability_tasks: [...execution.plan_revision.capability_tasks]
            .sort((left, right) => left.task_id.localeCompare(right.task_id))
            .map((task) => ({{
              task_id: task.task_id,
              idempotency_key: task.idempotency_key,
            }})),
        }};
        let authorityRunStatus = "completed";
        function stageAuthorityQuery(statement, params = []) {{
          const runId = String(params[0] || execution.run_attempt_id);
          if (statement.includes(
            "LEFT JOIN waje_runtime.capability_execution_snapshots"
          )) {{
            return {{ rows: [{{
              run_id: runId,
              run_attempt_id: runId,
              run_status: authorityRunStatus,
              execution_result_refs: executionResultRefs,
              claim_coverage_refs: claimCoverageRefs,
              plan_revision: execution.plan_revision,
              superseded_plan_revision_id: null,
              execution_snapshot: execution.execution_snapshot,
              exploration_stop_record: execution.exploration_stop_record,
              accepted_transition_id: execution.transition_id,
              transition_attempt_id: execution.durable_transition.attempt_id,
              transition_node_name: "execute_capability_dag",
              transition_parent_transition_id: planTransitionId,
              transition_run_attempt_id: runId,
              transition_intent_revision_id: execution.intent_revision_id,
              transition_decision_ledger_position: 1,
              transition_input_digest:
                execution.durable_transition.input_digest,
              transition_output_digest:
                execution.durable_transition.output_digest,
              transition_input_payload: transitionInputPayload,
              transition_output_payload: {{
                execution_snapshot: execution.execution_snapshot,
                exploration_stop_record:
                  execution.exploration_stop_record,
              }},
              transition_execution_attempt: 1,
              transition_status: "succeeded",
              transition_acceptance_state: "accepted",
              transition_next_transition: "phase03_evidence_bound",
              latest_accepted_transition_id:
                "transition:later-terminal-head",
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
          return null;
        }}
        """
    )


def test_completed_parser_requires_post_execution_state_and_hides_inline_payload():
    customer = json.dumps(_customer_publication(), ensure_ascii=False)
    post_execution = json.dumps(_post_execution_state())
    result = _run_typescript(
        textwrap.dedent(
            f"""
            const {{ parseAgentCoreOutput }} = await import(
              "./app/api/_agentCore.ts"
            );
            const {{ projectAgentCoreForCustomer }} = await import(
              "./app/api/_conversationStore.ts"
            );
            const customer = {customer};
            const completed = {{
              status: "completed",
              run_id: "run-published",
              turn_id: "turn-published",
              topic_id: "topic-published",
              context_manifest: {{ private: "context-private" }},
              ...{post_execution},
              customer_publication: customer,
              provider_audits: ["private-provider-audit"],
            }};
            const parsed = parseAgentCoreOutput(JSON.stringify(completed));
            const visible = projectAgentCoreForCustomer({{
              status: "completed",
              command: "private-command",
              output: "private-output",
              result: completed,
            }});
            const missingCustomerPublication = structuredClone(completed);
            delete missingCustomerPublication.customer_publication;
            const mismatchedState = structuredClone(completed);
            mismatchedState.delivery_status = "retryable_failed";
            console.log(JSON.stringify({{
              parsedStatus: parsed.status,
              visible,
              missingStatus: parseAgentCoreOutput(
                JSON.stringify(missingCustomerPublication),
              ).status,
              mismatchStatus: parseAgentCoreOutput(
                JSON.stringify(mismatchedState),
              ).status,
            }}));
            """
        )
    )

    assert result["parsedStatus"] == "completed"
    assert result["visible"]["result"] == {
        "run_id": "run-published",
        "turn_id": "turn-published",
        "topic_id": "topic-published",
        "status": "completed",
        **_post_execution_state(),
    }
    assert result["missingStatus"] == "failed"
    assert result["mismatchStatus"] == "failed"
    serialized = json.dumps(result["visible"], ensure_ascii=False)
    for private_value in (
        "context-private",
        "private-provider-audit",
        "private-command",
        "private-output",
        "昨天付费金额上升",
    ):
        assert private_value not in serialized


def test_post_seal_failure_projection_requires_typed_failure_and_exact_refs() -> None:
    state = {
        **_post_execution_state(),
        "post_execution_status": "narrative_failed",
        "publication_status": "not_ready",
        "delivery_status": "pending",
        "operational_failure": {
            "failure_ref": "failure:narrative-provider",
            "layer": "narrative",
            "kind": "provider_rate_limited",
            "retryability": "retryable",
            "business_boundary": (
                "Accepted analysis remains authoritative; publication is pending."
            ),
        },
    }
    state["publication_refs"] = {
        **state["publication_refs"],
        "post_seal_failure_terminal_ref": "post-seal-failure:typed",
        "failure_record_ref": "failure:narrative-provider",
        "failure_lifecycle_state_digest": "1" * 64,
        "narrative_workflow_ref": None,
        "narrative_workflow_digest": None,
        "compose_transition_id": None,
        "publication_ref": None,
        "outbox_ref": None,
        "customer_payload_ref": None,
        "delivery_attempt_ref": None,
        "customer_publication_ref": None,
    }
    state_json = json.dumps(state)
    result = _run_typescript(
        textwrap.dedent(
            f"""
            const {{ parseAgentCoreOutput }} = await import(
              "./app/api/_agentCore.ts"
            );
            const {{ projectAgentCoreForCustomer }} = await import(
              "./app/api/_conversationStore.ts"
            );
            const terminal = {{
              status: "completed",
              run_id: "run-narrative-failed",
              turn_id: "turn-narrative-failed",
              topic_id: "topic-narrative-failed",
              context_manifest: {{ internal: "private-context" }},
              ...{state_json},
            }};
            const parsed = parseAgentCoreOutput(JSON.stringify(terminal));
            const visible = projectAgentCoreForCustomer({{
              status: "completed",
              result: terminal,
            }});
            const missingFailureRef = structuredClone(terminal);
            missingFailureRef.publication_refs.failure_record_ref = null;
            const leakedTechnicalDetail = structuredClone(terminal);
            leakedTechnicalDetail.operational_failure.technical_detail_ref =
              "technical-detail:private";
            console.log(JSON.stringify({{
              parsedStatus: parsed.status,
              visible,
              missingFailureRefStatus: parseAgentCoreOutput(
                JSON.stringify(missingFailureRef),
              ).status,
              leakedTechnicalDetailStatus: parseAgentCoreOutput(
                JSON.stringify(leakedTechnicalDetail),
              ).status,
            }}));
            """
        )
    )

    assert result["parsedStatus"] == "completed"
    assert result["visible"]["result"] == {
        "run_id": "run-narrative-failed",
        "turn_id": "turn-narrative-failed",
        "topic_id": "topic-narrative-failed",
        "status": "completed",
        **state,
    }
    assert result["missingFailureRefStatus"] == "failed"
    assert result["leakedTechnicalDetailStatus"] == "failed"
    assert "private-context" not in json.dumps(result["visible"])


def test_completed_sse_loads_exact_customer_payload_from_publication_chain():
    customer = json.dumps(_customer_publication(), ensure_ascii=False)
    publication = _publication_refs()
    post_execution = json.dumps(_post_execution_state())
    stage_authority = _completed_stage_authority_script()
    result = _compiled_gateway_run(
        textwrap.dedent(
            f"""
            const out = process.env.GATEWAY_OUT;
            {stage_authority}
            const customer = {customer};
            globalThis.__wajeConversationPool = {{
              async query(statement, params = []) {{
                if (statement.includes(
                  "JOIN waje_runtime.investigation_threads"
                )) {{
                  return {{ rows: [{{
                    run_id: execution.run_attempt_id,
                    thread_id: "thread-published",
                    status: "completed",
                    request: withStageRefs({post_execution}),
                    created_at: "2026-07-18T08:00:00.000Z",
                    owner_id: "local-user",
                  }}] }};
                }}
                if (statement.includes("FROM waje_runtime.audit_events")) {{
                  return {{ rows: [] }};
                }}
                if (statement.includes("FROM waje_runtime.run_nodes")) {{
                  return {{ rows: [] }};
                }}
                const stageRows = stageAuthorityQuery(statement, params);
                if (stageRows) return stageRows;
                if (statement.includes(
                  "FROM waje_runtime.publication_customer_payloads customer"
                )) {{
                  return {{ rows: [{{
                    customer_payload: customer,
                        authority_bundle_ref: "{publication["authority_bundle_ref"]}",
                        authority_bundle_digest: "{publication["authority_bundle_digest"]}",
                        authority_sealed_at: "2026-07-18T07:59:58.000Z",
                        publication_ref: "{publication["publication_ref"]}",
                        publication_digest: "{publication["publication_digest"]}",
                        published_at: "2026-07-18T08:00:02.000Z",
                        projection_id: "{publication["projection_id"]}",
                        projection_digest: "{publication["projection_digest"]}",
                        outbox_ref: "{publication["outbox_ref"]}",
                        delivery_status: "published",
                        delivery_attempted_at: "2026-07-18T08:00:03.000Z",
                  }}] }};
                }}
                throw new Error(`unexpected_statement:${{statement}}`);
              }},
            }};
            const {{ runEvents }} = require(
              out + "/app/api/_conversationStore.js"
            );
            (async () => {{
              const events = await runEvents(
                execution.run_attempt_id,
                "local-user",
              );
              console.log(JSON.stringify(events));
            }})().catch((error) => {{
              console.error(error);
              process.exit(1);
            }});
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://publication-contract"},
    )

    ready = next(
        event for event in result if event["event"] == "customer_publication_ready"
    )
    plan_ready = next(
        event for event in result if event["event"] == "plan_result_ready"
    )
    execution_ready = next(
        event for event in result if event["event"] == "execution_result_ready"
    )
    assert plan_ready["payload"]["status"] == "planned"
    assert (
        plan_ready["payload"]["plan_result"]["authority_refs"]["plan_revision_id"]
        == _result().plan_revision_id
    )
    assert execution_ready["payload"]["status"] == "evidence_ready"
    assert (
        execution_ready["payload"]["execution_result"]["result_ref"]
        == _result().authoritative_execution_result_ref
    )
    assert ready["payload"] == {
        "status": "completed",
        "customer_publication": _customer_publication(),
        "publication": _publication_refs(),
        "post_execution": _post_execution_state(),
    }
    assert ready["process"] == {
        "stage": "publication",
        "label": "权威分析已发布",
        "summary": "已发布经过证据、claim 和叙事校验的客户结果。",
        "status": "completed",
    }


def test_completed_sse_requires_persisted_plan_and_execution_authority():
    post_execution = json.dumps(_post_execution_state())
    stage_authority = _completed_stage_authority_script()
    result = _compiled_gateway_run(
        textwrap.dedent(
            f"""
            const out = process.env.GATEWAY_OUT;
            {stage_authority}
            let missingStage = "plan";
            globalThis.__wajeConversationPool = {{
              async query(statement, params = []) {{
                if (statement.includes(
                  "JOIN waje_runtime.investigation_threads"
                )) {{
                  return {{ rows: [{{
                    run_id: execution.run_attempt_id,
                    thread_id: "thread-missing-stage-authority",
                    status: "completed",
                    request: withStageRefs({post_execution}),
                    created_at: "2026-07-18T08:00:00.000Z",
                    owner_id: "local-user",
                  }}] }};
                }}
                if (statement.includes("FROM waje_runtime.audit_events")) {{
                  return {{ rows: [] }};
                }}
                if (statement.includes("FROM waje_runtime.run_nodes")) {{
                  return {{ rows: [] }};
                }}
                if (
                  missingStage === "plan"
                  && statement.includes("r.request -> 'plan_result_refs'")
                ) {{
                  return {{ rows: [] }};
                }}
                if (
                  missingStage === "execution"
                  && statement.includes(
                    "LEFT JOIN waje_runtime.capability_execution_snapshots"
                  )
                ) {{
                  return {{ rows: [] }};
                }}
                const stageRows = stageAuthorityQuery(statement, params);
                if (stageRows) return stageRows;
                throw new Error(`unexpected_statement:${{statement}}`);
              }},
            }};
            const {{ runEvents }} = require(
              out + "/app/api/_conversationStore.js"
            );
            (async () => {{
              const errors = {{}};
              for (const stage of ["plan", "execution"]) {{
                missingStage = stage;
                try {{
                  await runEvents(execution.run_attempt_id, "local-user");
                }} catch (error) {{
                  errors[stage] = error instanceof Error
                    ? error.message
                    : String(error);
                }}
              }}
              console.log(JSON.stringify(errors));
            }})().catch((error) => {{
              console.error(error);
              process.exit(1);
            }});
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://stage-authority-contract"},
    )

    assert result == {
        "plan": "planned_result_authority_refs_invalid",
        "execution": "execution_result_authority_refs_invalid",
    }


def test_post_execution_stage_sse_keeps_plan_and_execution_history_visible():
    states = {
        status: _intermediate_post_execution_state(status)
        for status in ("authority_sealed", "narrative_ready")
    }
    states_json = json.dumps(states)
    stage_authority = _completed_stage_authority_script()
    result = _compiled_gateway_run(
        textwrap.dedent(
            f"""
            const out = process.env.GATEWAY_OUT;
            {stage_authority}
            const states = {states_json};
            globalThis.__wajeConversationPool = {{
              async query(statement, params = []) {{
                if (statement.includes(
                  "JOIN waje_runtime.investigation_threads"
                )) {{
                  return {{ rows: [{{
                    run_id: execution.run_attempt_id,
                    thread_id: "thread-post-execution-stage",
                    status: authorityRunStatus,
                    request: withStageRefs(states[authorityRunStatus]),
                    created_at: "2026-07-18T08:00:00.000Z",
                    owner_id: "local-user",
                  }}] }};
                }}
                if (statement.includes("FROM waje_runtime.audit_events")) {{
                  return {{ rows: [] }};
                }}
                if (statement.includes("FROM waje_runtime.run_nodes")) {{
                  return {{ rows: [] }};
                }}
                const stageRows = stageAuthorityQuery(statement, params);
                if (stageRows) return stageRows;
                throw new Error(`unexpected_statement:${{statement}}`);
              }},
            }};
            const {{ runEvents }} = require(
              out + "/app/api/_conversationStore.js"
            );
            (async () => {{
              const observed = {{}};
              for (const status of ["authority_sealed", "narrative_ready"]) {{
                authorityRunStatus = status;
                const events = await runEvents(
                  execution.run_attempt_id,
                  "local-user",
                );
                observed[status] = events.map((event) => event.event);
              }}
              console.log(JSON.stringify(observed));
            }})().catch((error) => {{
              console.error(error);
              process.exit(1);
            }});
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://stage-history-contract"},
    )

    for status in ("authority_sealed", "narrative_ready"):
        assert {
            "plan_result_ready",
            "execution_result_ready",
            "post_execution_state",
        }.issubset(result[status])


def test_failed_sse_keeps_only_authority_stages_persisted_before_failure():
    stage_authority = _completed_stage_authority_script()
    result = _compiled_gateway_run(
        textwrap.dedent(
            f"""
            const out = process.env.GATEWAY_OUT;
            {stage_authority}
            authorityRunStatus = "failed";
            let authorityRefsPersisted = false;
            globalThis.__wajeConversationPool = {{
              async query(statement, params = []) {{
                if (statement.includes(
                  "JOIN waje_runtime.investigation_threads"
                )) {{
                  return {{ rows: [{{
                    run_id: execution.run_attempt_id,
                    thread_id: "thread-failed-stage-history",
                    status: "failed",
                    request: authorityRefsPersisted
                      ? withStageRefs({{}})
                      : {{}},
                    created_at: "2026-07-18T08:00:00.000Z",
                    owner_id: "local-user",
                  }}] }};
                }}
                if (statement.includes("FROM waje_runtime.audit_events")) {{
                  return {{ rows: [] }};
                }}
                if (statement.includes("FROM waje_runtime.run_nodes")) {{
                  return {{ rows: [] }};
                }}
                const stageRows = stageAuthorityQuery(statement, params);
                if (stageRows) return stageRows;
                throw new Error(`unexpected_statement:${{statement}}`);
              }},
            }};
            const {{ runEvents }} = require(
              out + "/app/api/_conversationStore.js"
            );
            (async () => {{
              const observed = {{}};
              for (const persisted of [false, true]) {{
                authorityRefsPersisted = persisted;
                const events = await runEvents(
                  execution.run_attempt_id,
                  "local-user",
                );
                observed[String(persisted)] = events.map(
                  (event) => event.event
                );
              }}
              console.log(JSON.stringify(observed));
            }})().catch((error) => {{
              console.error(error);
              process.exit(1);
            }});
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://failed-stage-history"},
    )

    assert "plan_result_ready" not in result["false"]
    assert "execution_result_ready" not in result["false"]
    assert {
        "plan_result_ready",
        "execution_result_ready",
    }.issubset(result["true"])


@pytest.mark.parametrize(
    ("post_execution_status", "publication_status", "delivery_status"),
    (
        ("delivery_retryable_failed", "ready", "retryable_failed"),
        ("delivery_permanently_failed", "ready", "permanently_failed"),
        ("narrative_failed", "not_ready", "pending"),
        ("publication_failed", "failed", "pending"),
    ),
)
def test_completed_without_persisted_customer_payload_emits_post_execution_state(
    post_execution_status: str,
    publication_status: str,
    delivery_status: str,
) -> None:
    state = {
        **_post_execution_state(),
        "post_execution_status": post_execution_status,
        "publication_status": publication_status,
        "delivery_status": delivery_status,
    }
    if post_execution_status in {"narrative_failed", "publication_failed"}:
        failure_layer = (
            "narrative"
            if post_execution_status == "narrative_failed"
            else "persistence"
        )
        failure_ref = f"failure:{post_execution_status}"
        state["operational_failure"] = {
            "failure_ref": failure_ref,
            "layer": failure_layer,
            "kind": f"{failure_layer}_contract_failed",
            "retryability": "not_retryable",
            "business_boundary": (
                "Accepted analysis remains authoritative; publication is pending."
            ),
        }
        state["publication_refs"].update(
            {
                "post_seal_failure_terminal_ref": (
                    f"post-seal-failure:{post_execution_status}"
                ),
                "failure_record_ref": failure_ref,
                "failure_lifecycle_state_digest": "1" * 64,
            }
        )
        for field in (
            "narrative_workflow_ref",
            "narrative_workflow_digest",
            "compose_transition_id",
            "publication_ref",
            "outbox_ref",
            "customer_payload_ref",
            "delivery_attempt_ref",
            "customer_publication_ref",
        ):
            state["publication_refs"][field] = None
    else:
        state["publication_refs"]["customer_publication_ref"] = None
    state_json = json.dumps(state)
    stage_authority = _completed_stage_authority_script()
    result = _compiled_gateway_run(
        textwrap.dedent(
            f"""
            const out = process.env.GATEWAY_OUT;
            {stage_authority}
            globalThis.__wajeConversationPool = {{
              async query(statement, params = []) {{
                if (statement.includes("JOIN waje_runtime.investigation_threads")) {{
                  return {{ rows: [{{
                    run_id: execution.run_attempt_id,
                    thread_id: "thread-post-state",
                    status: "completed",
                    request: withStageRefs({state_json}),
                    created_at: "2026-07-18T08:00:00.000Z",
                    owner_id: "local-user",
                  }}] }};
                }}
                if (statement.includes("FROM waje_runtime.audit_events")) {{
                  return {{ rows: [] }};
                }}
                if (statement.includes("FROM waje_runtime.run_nodes")) {{
                  return {{ rows: [] }};
                }}
                const stageRows = stageAuthorityQuery(statement, params);
                if (stageRows) return stageRows;
                if (statement.includes(
                  "FROM waje_runtime.publication_customer_payloads customer"
                )) {{
                  return {{ rows: [] }};
                }}
                throw new Error(`unexpected_statement:${{statement}}`);
              }},
            }};
            const {{ runEvents }} = require(out + "/app/api/_conversationStore.js");
            (async () => {{
              console.log(JSON.stringify(
                await runEvents(execution.run_attempt_id, "local-user")
              ));
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://post-state-contract"},
    )

    assert "customer_publication_ready" not in {event["event"] for event in result}
    assert {
        "plan_result_ready",
        "execution_result_ready",
        "post_execution_state",
    }.issubset({event["event"] for event in result})
    state_event = next(
        event for event in result if event["event"] == "post_execution_state"
    )
    assert state_event["payload"] == state
    assert "technical_detail_ref" not in json.dumps(state_event["payload"])


def test_gateway_sources_have_one_completed_publication_contract():
    sources = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "app/api/_agentCore.ts",
            "app/api/_conversationStore.ts",
            "app/api/_customerRunProjection.ts",
            "app/api/agent-runs/route.ts",
            "app/page.tsx",
        )
    }
    combined = "\n".join(sources.values())
    assert "customer_publication_ready" in combined
    assert "publication_customer_payloads" in combined
    assert "delivery_outbox_records" in combined
    assert "delivery_attempts" in combined
    assert "traceRunFromCustomerPublication" in combined
    assert "answer_package" not in combined
    assert "answerPackage" not in combined
    assert "answer_package_ready" not in combined
    assert "completed_without_workflow" not in combined
    assert "post_execution_state" in combined


def test_interaction_completed_sse_emits_only_typed_interaction_result() -> None:
    interaction = {
        "schema_version": "typed-interaction.v1",
        "intent": "capability_question",
        "response_text": "可以分析已签约指标、因子与证据边界。",
    }
    interaction_json = json.dumps(interaction, ensure_ascii=False)
    result = _compiled_gateway_run(
        textwrap.dedent(
            f"""
            const out = process.env.GATEWAY_OUT;
            globalThis.__wajeConversationPool = {{
              async query(statement) {{
                if (statement.includes("JOIN waje_runtime.investigation_threads")) {{
                  return {{ rows: [{{
                    run_id: "run-interaction",
                    thread_id: "thread-interaction",
                    status: "interaction_completed",
                    request: {{ interaction_result: {interaction_json} }},
                    created_at: "2026-07-18T08:00:00.000Z",
                    owner_id: "local-user",
                  }}] }};
                }}
                if (statement.includes("FROM waje_runtime.audit_events")) {{
                  return {{ rows: [] }};
                }}
                if (statement.includes("FROM waje_runtime.run_nodes")) {{
                  return {{ rows: [] }};
                }}
                throw new Error(`unexpected_statement:${{statement}}`);
              }},
            }};
            const {{ runEvents }} = require(out + "/app/api/_conversationStore.js");
            (async () => {{
              console.log(JSON.stringify(
                await runEvents("run-interaction", "local-user")
              ));
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://interaction-contract"},
    )

    interaction_event = next(
        event for event in result if event["event"] == "interaction_result_ready"
    )
    assert interaction_event["payload"] == {
        "status": "interaction_completed",
        "interaction_result": interaction,
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert "customer_publication_ready" not in serialized
    assert "completed_without_workflow" not in serialized


def test_analysis_cancellation_sse_exposes_only_the_typed_safe_terminal() -> None:
    interaction = {
        "schema_version": "typed-interaction.v1",
        "intent": "analysis_cancellation",
        "response_text": "已取消当前分析。",
    }
    interaction_json = json.dumps(interaction, ensure_ascii=False)
    result = _compiled_gateway_run(
        textwrap.dedent(
            f"""
            const out = process.env.GATEWAY_OUT;
            globalThis.__wajeConversationPool = {{
              async query(statement) {{
                if (statement.includes("JOIN waje_runtime.investigation_threads")) {{
                  return {{ rows: [{{
                    run_id: "run-cancelled",
                    thread_id: "thread-cancelled",
                    status: "interaction_completed",
                    request: {{
                      interaction_result: {interaction_json},
                      directive: {{ original_user_text: "private-control-text" }},
                    }},
                    created_at: "2026-07-18T08:00:00.000Z",
                    owner_id: "local-user",
                  }}] }};
                }}
                if (statement.includes("FROM waje_runtime.audit_events")) {{
                  return {{ rows: [] }};
                }}
                if (statement.includes("FROM waje_runtime.run_nodes")) {{
                  return {{ rows: [] }};
                }}
                throw new Error(`unexpected_statement:${{statement}}`);
              }},
            }};
            const {{ runEvents }} = require(out + "/app/api/_conversationStore.js");
            (async () => {{
              console.log(JSON.stringify(
                await runEvents("run-cancelled", "local-user")
              ));
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://cancellation-contract"},
    )

    interaction_event = next(
        event for event in result if event["event"] == "interaction_result_ready"
    )
    assert interaction_event["payload"] == {
        "status": "interaction_completed",
        "interaction_result": interaction,
    }
    assert "private-control-text" not in json.dumps(result, ensure_ascii=False)


def test_run_dispatch_sse_exposes_exact_command_progress_without_lease_fields() -> None:
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const out = process.env.GATEWAY_OUT;
            globalThis.__wajeConversationPool = {
              async query(statement) {
                if (statement.includes("JOIN waje_runtime.investigation_threads")) {
                  return { rows: [{
                    run_id: "run-dispatch",
                    thread_id: "thread-dispatch",
                    status: "running_workflow",
                    request: {},
                    created_at: "2026-07-19T08:00:00.000Z",
                    owner_id: "local-user",
                  }] };
                }
                if (statement.includes("FROM waje_runtime.audit_events")) {
                  return { rows: [
                    {
                      event_type: "run_dispatch_claimed",
                      payload: {
                        dispatch_id: "dispatch-one",
                        producer_kind: "clarification_resolution",
                        dispatch_owner_id: "private-owner",
                        lease_epoch: 7,
                      },
                      created_at: "2026-07-19T08:00:01.000Z",
                    },
                    {
                      event_type: "run_dispatch_completed",
                      payload: {
                        dispatchId: "dispatch-one",
                        status: "completed",
                        failureReason: "private-failure",
                        leaseEpoch: 7,
                      },
                      created_at: "2026-07-19T08:00:02.000Z",
                    },
                  ] };
                }
                if (statement.includes("FROM waje_runtime.run_nodes")) {
                  return { rows: [] };
                }
                throw new Error(`unexpected_statement:${statement}`);
              },
            };
            const { runEvents } = require(out + "/app/api/_conversationStore.js");
            (async () => {
              console.log(JSON.stringify(
                await runEvents("run-dispatch", "local-user")
              ));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        env={"WAJE_RUNTIME_DATABASE_URL": "postgres://dispatch-contract"},
    )

    dispatch_events = [
        event for event in result if event["event"].startswith("run_dispatch_")
    ]
    assert [event["payload"] for event in dispatch_events] == [
        {
            "dispatch_id": "dispatch-one",
            "producer_kind": "clarification_resolution",
            "state": "running",
        },
        {
            "dispatch_id": "dispatch-one",
            "state": "terminal",
            "terminal_status": "completed",
        },
    ]
    serialized = json.dumps(dispatch_events)
    assert "private-owner" not in serialized
    assert "private-failure" not in serialized
    assert "lease_epoch" not in serialized
