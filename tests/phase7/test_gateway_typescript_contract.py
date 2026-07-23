from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[2]


def test_gateway_python_runtime_uses_prebuilt_immutable_environment() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            process.env.WAJE_PYTHON_EXECUTABLE = process.execPath;
            const { wajePythonInvocation } = await import(
              "./app/api/_pythonRuntime.ts"
            );
            console.log(JSON.stringify(wajePythonInvocation(["-c", "pass"])));
            """
        )
    )

    assert Path(result["command"]).is_absolute()
    assert Path(result["command"]).exists()
    assert result["args"] == ["-c", "pass"]


def test_production_customer_actor_requires_fresh_request_bound_signature() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            import { createHmac } from "node:crypto";

            process.env.NODE_ENV = "production";
            process.env.WAJE_AUTH_HEADER_SECRET = "s".repeat(64);
            const { resolveCustomerActor } = await import(
              "./app/api/_customerActor.ts"
            );
            const actorId = "customer-17";
            const issuedAt = String(Math.floor(Date.now() / 1000));
            const requestUrl = "https://waje.example/api/threads/thread-1/messages?view=current";
            const canonical = [
              "waje-auth-v1",
              "POST",
              "/api/threads/thread-1/messages?view=current",
              issuedAt,
              actorId,
            ].join("\\n");
            const signature = createHmac(
              "sha256",
              process.env.WAJE_AUTH_HEADER_SECRET,
            ).update(canonical).digest("hex");
            const headers = {
              "x-waje-authenticated-user-id": actorId,
              "x-waje-authenticated-user-issued-at": issuedAt,
              "x-waje-authentication-signature": signature,
            };
            const valid = resolveCustomerActor(new Request(requestUrl, {
              method: "POST",
              headers,
            }));
            const failure = (request) => {
              try {
                resolveCustomerActor(request);
                return "accepted";
              } catch (error) {
                return error instanceof Error ? error.message : "unknown";
              }
            };
            const unsigned = failure(new Request(requestUrl, {
              method: "POST",
              headers: { "x-waje-authenticated-user-id": actorId },
            }));
            const wrongPath = failure(new Request(
              "https://waje.example/api/threads/thread-2/messages?view=current",
              { method: "POST", headers },
            ));
            const expiredAt = String(Number(issuedAt) - 3600);
            const expiredCanonical = [
              "waje-auth-v1",
              "POST",
              "/api/threads/thread-1/messages?view=current",
              expiredAt,
              actorId,
            ].join("\\n");
            const expiredSignature = createHmac(
              "sha256",
              process.env.WAJE_AUTH_HEADER_SECRET,
            ).update(expiredCanonical).digest("hex");
            const expired = failure(new Request(requestUrl, {
              method: "POST",
              headers: {
                ...headers,
                "x-waje-authenticated-user-issued-at": expiredAt,
                "x-waje-authentication-signature": expiredSignature,
              },
            }));
            console.log(JSON.stringify({ valid, unsigned, wrongPath, expired }));
            """
        )
    )

    assert result == {
        "valid": "customer-17",
        "unsigned": "customer_identity_untrusted",
        "wrongPath": "customer_identity_untrusted",
        "expired": "customer_identity_expired",
    }


def test_nonproduction_customer_actor_keeps_local_development_identity() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            const { resolveCustomerActor } = await import(
              "./app/api/_customerActor.ts"
            );
            console.log(JSON.stringify({
              actor: resolveCustomerActor(new Request("http://localhost/api/threads")),
            }));
            """
        )
    )

    assert result == {"actor": "local-user"}


def test_inline_bridge_preserves_typed_failed_terminal_on_exit_one() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            const {
              finalizeAgentCoreInlineResult,
            } = await import("./app/api/_agentCore.ts");
            const failed = {
              status: "failed",
              run_id: "run-failed",
              turn_id: "turn-failed",
              topic_id: "topic-failed",
              failure_reason: "post_execution_public_fact_materialization_incomplete",
            };
            const validFailure = finalizeAgentCoreInlineResult(
              JSON.stringify(failed),
              1,
            );
            const invalidSuccessExit = finalizeAgentCoreInlineResult(
              JSON.stringify({ ...failed, status: "run_cancelled", directive: {}, durable_checkpoint: {}, intent_revision_id: "intent" }),
              1,
            );
            const malformed = finalizeAgentCoreInlineResult("not-json", 1);
            console.log(JSON.stringify({
              validFailure,
              invalidSuccessExit,
              malformed,
            }));
            """
        )
    )

    assert result["validFailure"]["status"] == "failed"
    assert result["validFailure"]["result"]["failure_reason"] == (
        "post_execution_public_fact_materialization_incomplete"
    )
    assert "error" not in result["validFailure"]
    assert result["invalidSuccessExit"]["error"] == "agent_core_process_failed"
    assert result["malformed"]["error"] == "agent_core_output_malformed_json"


def test_typed_interaction_contract_is_exact_and_customer_visible() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            const { parseAgentCoreOutput } = await import("./app/api/_agentCore.ts");
            const { projectAgentCoreForCustomer } = await import(
              "./app/api/_conversationStore.ts"
            );
            const terminal = {
              status: "interaction_completed",
              run_id: "run-interaction",
              turn_id: "turn-interaction",
              topic_id: null,
              intent: "capability_question",
              topic_relation: "inherit_current",
              context_manifest: { internal: "hidden" },
              interaction_result: {
                schema_version: "typed-interaction.v1",
                intent: "capability_question",
                response_text: "可以分析付费金额及其已签约影响因子。",
              },
            };
            const parsed = parseAgentCoreOutput(JSON.stringify(terminal));
            const visible = projectAgentCoreForCustomer({
              status: terminal.status,
              command: "hidden-command",
              output: "hidden-output",
              result: terminal,
            });
            const missingText = structuredClone(terminal);
            delete missingText.interaction_result.response_text;
            const extraField = structuredClone(terminal);
            extraField.interaction_result.local_fallback = "forbidden";
            const invalidRelation = structuredClone(terminal);
            invalidRelation.topic_relation = "same_topic";
            console.log(JSON.stringify({
              parsedStatus: parsed.status,
              visible,
              missingStatus: parseAgentCoreOutput(JSON.stringify(missingText)).status,
              extraStatus: parseAgentCoreOutput(JSON.stringify(extraField)).status,
              relationStatus: parseAgentCoreOutput(
                JSON.stringify(invalidRelation)
              ).status,
            }));
            """
        )
    )

    assert result["parsedStatus"] == "interaction_completed"
    assert result["visible"] == {
        "status": "interaction_completed",
        "result": {
            "run_id": "run-interaction",
            "turn_id": "turn-interaction",
            "topic_id": None,
            "status": "interaction_completed",
            "intent": "capability_question",
            "topic_relation": "inherit_current",
            "interaction_result": {
                "schema_version": "typed-interaction.v1",
                "intent": "capability_question",
                "response_text": "可以分析付费金额及其已签约影响因子。",
            },
        },
    }
    assert result["missingStatus"] == "failed"
    assert result["extraStatus"] == "failed"
    assert result["relationStatus"] == "failed"


def test_analysis_cancellation_is_a_typed_safe_interaction_terminal() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            const { parseAgentCoreOutput } = await import("./app/api/_agentCore.ts");
            const { projectAgentCoreForCustomer } = await import(
              "./app/api/_conversationStore.ts"
            );
            const terminal = {
              status: "interaction_completed",
              run_id: "run-cancelled",
              turn_id: "turn-cancelled",
              topic_id: "topic-cancelled",
              intent: "analysis_cancellation",
              topic_relation: "analysis_cancellation",
              context_manifest: { manifest_id: "private-manifest" },
              directive: { original_user_text: "private-control-text" },
              durable_checkpoint: { provider_ref: "private-provider" },
              interaction_result: {
                schema_version: "typed-interaction.v1",
                intent: "analysis_cancellation",
                response_text: "已取消当前分析。",
              },
            };
            const parsed = parseAgentCoreOutput(JSON.stringify(terminal));
            const visible = projectAgentCoreForCustomer({
              status: terminal.status,
              command: "private-command",
              output: "private-output",
              result: terminal,
            });
            const wrongRelation = structuredClone(terminal);
            wrongRelation.topic_relation = "inherit_current";
            console.log(JSON.stringify({
              parsedStatus: parsed.status,
              visible,
              wrongRelationStatus: parseAgentCoreOutput(
                JSON.stringify(wrongRelation)
              ).status,
            }));
            """
        )
    )

    assert result == {
        "parsedStatus": "interaction_completed",
        "visible": {
            "status": "interaction_completed",
            "result": {
                "run_id": "run-cancelled",
                "turn_id": "turn-cancelled",
                "topic_id": "topic-cancelled",
                "status": "interaction_completed",
                "intent": "analysis_cancellation",
                "topic_relation": "analysis_cancellation",
                "interaction_result": {
                    "schema_version": "typed-interaction.v1",
                    "intent": "analysis_cancellation",
                    "response_text": "已取消当前分析。",
                },
            },
        },
        "wrongRelationStatus": "failed",
    }


def test_typed_topic_choice_contract_is_exact_and_relation_bound() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            const { parseAgentCoreOutput } = await import("./app/api/_agentCore.ts");
            const { projectAgentCoreForCustomer } = await import(
              "./app/api/_conversationStore.ts"
            );
            const terminal = {
              status: "interaction_completed",
              run_id: "run-topic-choice",
              turn_id: "turn-topic-choice",
              topic_id: "topic-current",
              intent: "follow_up",
              topic_relation: "ask_topic_choice",
              context_manifest: { internal: "hidden" },
              interaction_result: {
                schema_version: "typed-topic-choice.v1",
                intent: "follow_up",
                response_text: "请选择要继续分析的主题。",
                options: [
                  {
                    topic_id: "topic-revenue",
                    label: "收入变化",
                    description: "继续查看收入变化主题。",
                  },
                  {
                    topic_id: "topic-retention",
                    label: "留存变化",
                    description: "继续查看留存变化主题。",
                  },
                ],
                recommended_topic_id: "topic-revenue",
                allow_free_text: true,
              },
            };
            const visible = projectAgentCoreForCustomer({
              status: terminal.status,
              command: "hidden-command",
              result: terminal,
            });
            const duplicate = structuredClone(terminal);
            duplicate.interaction_result.options[1].topic_id = "topic-revenue";
            const badRecommendation = structuredClone(terminal);
            badRecommendation.interaction_result.recommended_topic_id = "topic-missing";
            const extraOptionField = structuredClone(terminal);
            extraOptionField.interaction_result.options[0].local_hint = "forbidden";
            const plainForChoice = structuredClone(terminal);
            plainForChoice.interaction_result = {
              schema_version: "typed-interaction.v1",
              intent: "capability_question",
              response_text: "普通交互。",
            };
            plainForChoice.intent = "capability_question";
            console.log(JSON.stringify({
              parsedStatus: parseAgentCoreOutput(JSON.stringify(terminal)).status,
              visible,
              duplicateStatus: parseAgentCoreOutput(JSON.stringify(duplicate)).status,
              recommendationStatus: parseAgentCoreOutput(
                JSON.stringify(badRecommendation)
              ).status,
              optionFieldStatus: parseAgentCoreOutput(
                JSON.stringify(extraOptionField)
              ).status,
              plainForChoiceStatus: parseAgentCoreOutput(
                JSON.stringify(plainForChoice)
              ).status,
            }));
            """
        )
    )

    assert result["parsedStatus"] == "interaction_completed"
    assert result["visible"]["result"]["interaction_result"] == {
        "schema_version": "typed-topic-choice.v1",
        "intent": "follow_up",
        "response_text": "请选择要继续分析的主题。",
        "options": [
            {
                "topic_id": "topic-revenue",
                "label": "收入变化",
                "description": "继续查看收入变化主题。",
            },
            {
                "topic_id": "topic-retention",
                "label": "留存变化",
                "description": "继续查看留存变化主题。",
            },
        ],
        "recommended_topic_id": "topic-revenue",
        "allow_free_text": True,
    }
    assert result["duplicateStatus"] == "failed"
    assert result["recommendationStatus"] == "failed"
    assert result["optionFieldStatus"] == "failed"
    assert result["plainForChoiceStatus"] == "failed"


def test_phase45_terminals_require_orthogonal_post_execution_state() -> None:
    refs = {
        "post_execution_result_ref": "post:phase45",
        "post_execution_result_digest": "a" * 64,
        "semantic_authority_result_ref": "semantic:phase45",
        "semantic_authority_result_digest": "b" * 64,
        "authority_bundle_ref": "bundle:phase45",
        "authority_bundle_digest": "c" * 64,
        "authority_transition_id": "transition:phase45",
        "claim_coverage_checkpoint_ref": (
            "claim-coverage-checkpoint:sha256:" + "d" * 64
        ),
        "claim_coverage_checkpoint_digest": "d" * 64,
        "claim_coverage_transition_id": "transition:claim-coverage",
        "post_seal_failure_terminal_ref": None,
        "failure_record_ref": None,
        "failure_lifecycle_state_digest": None,
        "narrative_workflow_ref": None,
        "narrative_workflow_digest": None,
        "compose_transition_id": None,
        "publication_ref": None,
        "outbox_ref": None,
        "customer_payload_ref": None,
        "delivery_attempt_ref": None,
        "customer_publication_ref": None,
    }
    result = _run_typescript(
        textwrap.dedent(
            f"""
            const {{ parseAgentCoreOutput }} = await import("./app/api/_agentCore.ts");
            const terminal = {{
              status: "authority_sealed",
              run_id: "run-phase45",
              turn_id: "turn-phase45",
              topic_id: "topic-phase45",
              context_manifest: {{}},
              post_execution_status: "authority_sealed",
              analysis_status: "complete",
              publication_status: "not_ready",
              delivery_status: "pending",
              publication_refs: {json.dumps(refs)},
            }};
            const accepted = parseAgentCoreOutput(JSON.stringify(terminal));
            terminal.publication_status = "published";
            const rejected = parseAgentCoreOutput(JSON.stringify(terminal));
            console.log(JSON.stringify({{ accepted: accepted.status, rejected: rejected.status }}));
            """
        )
    )

    assert result == {"accepted": "authority_sealed", "rejected": "failed"}


def test_gateway_app_has_no_legacy_completion_or_artifact_authority() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "app").rglob("*.ts*")
    )

    for legacy in (
        "completed_without_workflow",
        "answer_package_ready",
        "artifact_continue",
        "WAJE_REPLAY_ARTIFACT_ROOT",
        "plan_result.json",
    ):
        assert legacy not in sources


def _run_typescript(source: str, *, env=None, unit_test_store=True):
    completed = _run_typescript_process(
        source,
        env=env,
        unit_test_store=unit_test_store,
    )
    completed.check_returncode()
    return json.loads(completed.stdout)


def _run_typescript_process(source: str, *, env=None, unit_test_store=True):
    node = shutil.which("node")
    if not node:
        raise RuntimeError(
            "node executable is required for Gateway TypeScript contract tests"
        )
    process_env = {**os.environ, **(env or {})}
    if unit_test_store:
        process_env.pop("WAJE_RUNTIME_DATABASE_URL", None)
        process_env.pop("DATABASE_URL", None)
        process_env["NODE_ENV"] = "test"
        process_env["WAJE_GATEWAY_UNIT_TEST_STORE"] = "memory"
    else:
        process_env.pop("WAJE_RUNTIME_DATABASE_URL", None)
        process_env.pop("DATABASE_URL", None)
        process_env.pop("WAJE_GATEWAY_UNIT_TEST_STORE", None)
        process_env["NODE_ENV"] = "development"
    return subprocess.run(
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
        env=process_env,
        capture_output=True,
        text=True,
    )
