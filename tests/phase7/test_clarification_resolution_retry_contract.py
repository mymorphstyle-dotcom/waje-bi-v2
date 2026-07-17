from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap

from bi_agent.conversation.agent_core import (
    ConversationAgentCore,
    _build_clarification_source_envelope,
)
from bi_agent.conversation.models import ClarificationOption, ClarificationState
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult


ROOT = Path(__file__).resolve().parents[2]
GATEWAY_SOURCES = (
    "app/api/runs/[runId]/clarifications/route.ts",
    "app/api/runs/[runId]/retry/route.ts",
    "app/api/_agentCore.ts",
    "app/api/_conversationStore.ts",
    "app/api/_customerActor.ts",
)


def test_failed_clarification_attempt_retries_same_resolution_without_new_message():
    result = _compiled_gateway_run(
        textwrap.dedent(
            """
            const { NextRequest } = require("next/server");
            const fs = require("fs");
            const path = require("path");
            const out = process.env.GATEWAY_OUT;
            const source = {
              id: "run-source",
              threadId: "thread-source",
              topicId: "topic-source",
              status: "waiting_for_clarification",
              createdAt: "2026-07-17T00:00:00.000Z",
              request: {
                clarification_source_envelope: {
                  schema_version: "clarification-source-envelope.v1",
                  source_run_id: "run-source",
                  source_thread_id: "thread-source",
                  source_topic_id: "topic-source",
                  source_owner_id: "local-user",
                  question: "2026年6月1日付费金额为什么上涨？",
                  analysis_context: {},
                  source_material: {
                    accepted_graph: [],
                    analysis_contract: {},
                    analysis_route: {},
                    original_intent: {
                      target_metric: "paid_amount",
                      baseline_candidates: [],
                    },
                    material_slots: {
                      target_metrics: ["paid_amount"],
                      baselines: [],
                    },
                  },
                  clarification: {
                    choice_actions: [{
                      choice_id: "material-baseline-previous-day",
                      action_kind: "bind_material_choice",
                      business_label: "跟前一天比较",
                      material_patch: { baseline_candidates: ["previous_day"] },
                    }],
                  },
                  source_digest: "gateway-fixture-source-digest",
                },
              },
            };
            const thread = {
              id: source.threadId,
              ownerId: "local-user",
              topicIds: [source.topicId],
              messages: [],
              createdAt: "2026-07-17T00:00:00.000Z",
            };
            globalThis.__wajeConversationMemoryStore = {
              threads: new Map([[thread.id, thread]]),
              runs: new Map([[source.id, source]]),
              artifacts: new Map(),
              memoryProposals: new Map(),
              clarificationResolutions: new Map(),
              clarificationExecutionAttempts: new Map(),
              runDispatches: new Map(),
              auditEvents: [],
            };
            const clarificationRoute = require(
              out + "/app/api/runs/[runId]/clarifications/route.js"
            );
            const retryRoute = require(
              out + "/app/api/runs/[runId]/retry/route.js"
            );
            const storeApi = require(out + "/app/api/_conversationStore.js");
            const request = (url, identity, body = {}, actor = "local-user") =>
              new NextRequest(url, {
                method: "POST",
                headers: {
                  "content-type": "application/json",
                  "idempotency-key": identity,
                  "x-waje-authenticated-user-id": actor,
                },
                body: JSON.stringify(body),
              });
            const context = (runId) => ({
              params: Promise.resolve({ runId }),
            });
            const errorCode = async (operation) => {
              try {
                await operation();
                return "";
              } catch (error) {
                return error.code ?? error.message;
              }
            };
            (async () => {
              const initialResponse = await clarificationRoute.POST(
                request(
                  "http://localhost/api/runs/run-source/clarifications",
                  "resolution-1-attempt-1",
                  {
                    answer: "跟前一天比较",
                    selectedOptionId: "material-baseline-previous-day",
                  },
                ),
                context(source.id),
              );
              const initialBody = await initialResponse.json();
              const firstAttemptRunId = initialBody.attemptRunId;
              const memory = globalThis.__wajeConversationMemoryStore;

              const invalidBodyResponse = await retryRoute.POST(
                request(
                  "http://localhost/api/runs/" + firstAttemptRunId + "/retry",
                  "resolution-1-invalid-body",
                  { answer: "再次确认前一天" },
                ),
                context(firstAttemptRunId),
              );
              const ownerResponse = await retryRoute.POST(
                request(
                  "http://localhost/api/runs/" + firstAttemptRunId + "/retry",
                  "resolution-1-owner-drift",
                  {},
                  "user-other",
                ),
                context(firstAttemptRunId),
              );

              const [resolutionKey, originalResolution] =
                [...memory.clarificationResolutions.entries()][0];
              const cleanResolution = structuredClone(originalResolution);
              originalResolution.acceptedChoice = {
                ...originalResolution.acceptedChoice,
                forged: true,
              };
              const choiceTamperCode = await errorCode(() =>
                storeApi.claimClarificationRetryAttempt({
                  failedRunId: firstAttemptRunId,
                  requestIdentity: "resolution-1-choice-tamper",
                  actorId: "local-user",
                })
              );
              memory.clarificationResolutions.set(resolutionKey, cleanResolution);

              const failedAttempt = memory.runs.get(firstAttemptRunId);
              const cleanTopicId = failedAttempt.topicId;
              failedAttempt.topicId = "topic-forged";
              const topicTamperCode = await errorCode(() =>
                storeApi.claimClarificationRetryAttempt({
                  failedRunId: firstAttemptRunId,
                  requestIdentity: "resolution-1-topic-tamper",
                  actorId: "local-user",
                })
              );
              failedAttempt.topicId = cleanTopicId;

              const cleanThreadId = failedAttempt.threadId;
              failedAttempt.threadId = "thread-forged";
              const threadTamperCode = await errorCode(() =>
                storeApi.claimClarificationRetryAttempt({
                  failedRunId: firstAttemptRunId,
                  requestIdentity: "resolution-1-thread-tamper",
                  actorId: "local-user",
                })
              );
              failedAttempt.threadId = cleanThreadId;

              const retryRequest = () => request(
                "http://localhost/api/runs/" + firstAttemptRunId + "/retry",
                "resolution-1-attempt-2",
              );
              const retryResponse = await retryRoute.POST(
                retryRequest(),
                context(firstAttemptRunId),
              );
              const retryBody = await retryResponse.json();
              const replayResponse = await retryRoute.POST(
                retryRequest(),
                context(firstAttemptRunId),
              );
              const replayBody = await replayResponse.json();
              const conflictResponse = await retryRoute.POST(
                request(
                  "http://localhost/api/runs/" + firstAttemptRunId + "/retry",
                  "resolution-1-different-successor",
                ),
                context(firstAttemptRunId),
              );

              const invocationPath = path.join(
                process.env.WAJE_GATEWAY_TEST_TMP,
                "agent-invocations.jsonl",
              );
              const invocations = fs.existsSync(invocationPath)
                ? fs.readFileSync(invocationPath, "utf8").trim().split("\\n")
                    .filter(Boolean).map((line) => JSON.parse(line))
                : [];
              const attempts = [...memory.clarificationExecutionAttempts.values()]
                .sort((left, right) => left.attemptNumber - right.attemptNumber);
              const runs = [...memory.runs.values()];
              console.log(JSON.stringify({
                initialStatus: initialResponse.status,
                initialBody,
                invalidBodyStatus: invalidBodyResponse.status,
                ownerStatus: ownerResponse.status,
                choiceTamperCode,
                topicTamperCode,
                threadTamperCode,
                retryStatus: retryResponse.status,
                retryBody,
                replayStatus: replayResponse.status,
                replayBody,
                conflictStatus: conflictResponse.status,
                resolutionCount: memory.clarificationResolutions.size,
                resolutions: [...memory.clarificationResolutions.values()],
                attempts,
                messageCount: thread.messages.length,
                runStatuses: Object.fromEntries(runs.map((run) => [run.id, run.status])),
                waitingRunIds: runs.filter(
                  (run) => run.status === "waiting_for_clarification"
                ).map((run) => run.id),
                clarificationAnswerAuditCount: memory.auditEvents.filter(
                  (event) => event.eventType === "clarification_answer_recorded"
                ).length,
                invocations,
              }));
            })().catch((error) => { console.error(error); process.exit(1); });
            """
        ),
        fake_python=_fail_once_then_complete_agent_core(),
    )

    first_attempt = result["initialBody"]["attemptRunId"]
    second_attempt = result["retryBody"]["attemptRunId"]
    assert result["initialStatus"] == 200
    assert result["initialBody"]["attemptNumber"] == 1
    assert "resumedRunId" not in result["initialBody"]
    assert result["invalidBodyStatus"] == 400
    assert result["ownerStatus"] == 403
    assert result["choiceTamperCode"]
    assert result["topicTamperCode"]
    assert result["threadTamperCode"]
    assert result["retryStatus"] == 200
    assert result["retryBody"]["attemptNumber"] == 2
    assert result["retryBody"]["previousAttemptRunId"] == first_attempt
    assert result["retryBody"]["message"] is None
    assert "resumedRunId" not in result["retryBody"]
    assert second_attempt != first_attempt
    assert result["replayStatus"] == 200
    assert result["replayBody"]["attemptRunId"] == second_attempt
    assert result["conflictStatus"] == 409
    assert result["resolutionCount"] == 1
    assert result["resolutions"][0]["status"] == "accepted"
    assert result["resolutions"][0]["acceptedAt"]
    assert [item["attemptNumber"] for item in result["attempts"]] == [1, 2]
    assert result["attempts"][0]["previousAttemptRunId"] is None
    assert result["attempts"][1]["previousAttemptRunId"] == first_attempt
    assert result["messageCount"] == 1
    assert result["runStatuses"][first_attempt] == "failed"
    assert result["runStatuses"][second_attempt] == "completed"
    assert result["waitingRunIds"] == ["run-source"]
    assert result["clarificationAnswerAuditCount"] == 1
    assert len(result["invocations"]) == 2
    clarifications = [item["clarification"] for item in result["invocations"]]
    assert {item["sourceRunId"] for item in clarifications} == {"run-source"}
    assert {item["resolutionId"] for item in clarifications} == {
        result["initialBody"]["resolutionId"]
    }
    assert [item["attemptRunId"] for item in clarifications] == [
        first_attempt,
        second_attempt,
    ]
    assert {item["selectedOptionId"] for item in clarifications} == {
        "material-baseline-previous-day"
    }


def test_retry_attempt_keeps_business_choice_and_refreshes_execution_authority():
    store = InMemoryConversationStore()
    thread = store.create_thread("thread-retry-authority", owner_id="user-1")
    topic = store.create_topic(
        thread.thread_id,
        title="付费金额变化",
        summary="解释付费金额变化",
    )
    store.set_current_topic(thread.thread_id, topic.topic_id)
    source_run_id = "run-resolution-source"
    first_attempt_run_id = "run-resolution-attempt-1"
    retry_attempt_run_id = "run-resolution-attempt-2"
    resolution_id = "clarification-resolution:previous-day"
    question = "2026年6月1日付费金额为什么上涨？主要由哪些指标变化导致？"
    material_action = {
        "choice_id": "material-baseline-previous-day",
        "action_kind": "bind_material_choice",
        "business_label": "跟前一天比较",
        "material_patch": {"baseline_candidates": ["previous_day"]},
        "affected_material_slots": ["baseline"],
    }
    clarification = {
        "questions": [
            {
                "question": "希望使用哪个比较基线？",
                "options": ["跟前一天比较", "跟上周同日比较"],
            }
        ],
        "recommended_choice_id": material_action["choice_id"],
        "choice_actions": [material_action],
    }
    original_intent = {
        "question_family": "paid_amount_change_explanation",
        "target_metric": "paid_amount",
        "question": question,
        "baseline_candidates": [],
        "ambiguous_slots": ["baseline"],
    }
    material_slots = {
        "target_metrics": ["paid_amount"],
        "baselines": [],
        "component_ids": ["first_paid_users", "paid_frequency"],
    }
    old_context = {
        "as_of": "2026-06-03T12:00:00+01:00",
        "active_release_ref": "release:old",
        "snapshot_refs": ["snapshot:old"],
        "query_refs": ["query:old"],
    }
    fresh_context = {
        "as_of": "2026-07-17T09:30:00+08:00",
    }
    source_envelope = _build_clarification_source_envelope(
        source_run_id=source_run_id,
        source_thread_id=thread.thread_id,
        source_topic_id=topic.topic_id,
        source_owner_id=thread.owner_id,
        question=question,
        analysis_context=old_context,
        accepted_graph=("compare_periods", "driver_decomposition"),
        analysis_contract={
            "analysis_contract_id": "analysis:old",
            "as_of": old_context["as_of"],
            "execution_material_ref": "execution-material:old",
        },
        analysis_route={
            "active_release_ref": old_context["active_release_ref"],
            "snapshot_refs": old_context["snapshot_refs"],
            "query_refs": old_context["query_refs"],
        },
        original_intent=original_intent,
        material_slots=material_slots,
        clarification=clarification,
    )
    store.upsert_run(
        source_run_id,
        thread_id=thread.thread_id,
        topic_id=topic.topic_id,
        status="waiting_for_clarification",
        request={
            "clarification_source_envelope": source_envelope,
            "execution_material": {
                "execution_material_ref": "execution-material:old",
                **old_context,
            },
        },
    )
    store.save_clarification_state(
        ClarificationState(
            run_id=source_run_id,
            topic_id=topic.topic_id,
            question="希望使用哪个比较基线？",
            options=[
                ClarificationOption(
                    option_id=material_action["choice_id"],
                    label=material_action["business_label"],
                    recommended=True,
                )
            ],
            status="answered",
            answer=material_action["business_label"],
        )
    )
    store.upsert_run(
        first_attempt_run_id,
        thread_id=thread.thread_id,
        topic_id=topic.topic_id,
        status="failed",
        request={
            "clarification_resolution_id": resolution_id,
            "execution_material": {
                "execution_material_ref": "execution-material:old",
                **old_context,
            },
        },
    )
    resolver_calls: list[dict] = []

    def resolve_attempt(**kwargs):
        resolver_calls.append(deepcopy(kwargs))
        return {
            "resolution_id": resolution_id,
            "source_run_id": source_run_id,
            "attempt_run_id": retry_attempt_run_id,
            "previous_attempt_run_id": first_attempt_run_id,
            "attempt_number": 2,
            "thread_id": thread.thread_id,
            "topic_id": topic.topic_id,
            "owner_id": thread.owner_id,
            "request_identity": "resolution-attempt-2",
            "answer": material_action["business_label"],
            "selected_option_id": material_action["choice_id"],
            "source": "user",
            "source_request_digest": "source-request-digest",
            "resolution_digest": "resolution-digest",
            "retry_attempt": True,
            "accepted_choice": deepcopy(material_action),
            "material_patch": deepcopy(material_action["material_patch"]),
        }

    store.resolve_clarification_attempt_authority = resolve_attempt
    workflow_requests: list[dict] = []

    def workflow_runner(request):
        workflow_requests.append(deepcopy(request))
        return WorkflowRunResult(
            status="failed",
            run_id=request["run_id"],
            failure_reason="stop_after_retry_contract_capture",
        )

    result = ConversationAgentCore(
        store,
        workflow_runner=workflow_runner,
    ).run_message(
        thread_id=thread.thread_id,
        run_id=retry_attempt_run_id,
        user_id=thread.owner_id,
        user_message=material_action["business_label"],
        clarification={
            "sourceRunId": source_run_id,
            "resolutionId": resolution_id,
            "attemptRunId": retry_attempt_run_id,
            "answer": material_action["business_label"],
            "selectedOptionId": material_action["choice_id"],
            "source": "user",
            "retryAttempt": True,
        },
        analysis_context=fresh_context,
    )

    assert result["status"] == "failed"
    assert "clarification" not in result
    assert resolver_calls == [
        {
            "source_run_id": source_run_id,
            "attempt_run_id": retry_attempt_run_id,
            "thread_id": thread.thread_id,
            "owner_id": thread.owner_id,
            "answer": material_action["business_label"],
            "selected_option_id": material_action["choice_id"],
            "source": "user",
        }
    ]
    assert len(workflow_requests) == 1
    request = workflow_requests[0]
    assert request["question"] == question
    assert request["analysis_context"] == fresh_context
    assert request["clarification_choice"] == {
        "answer_text": material_action["business_label"],
        "baseline_candidates": ["previous_day"],
    }
    resume = request["clarification_attempt_context"]
    assert resume["resolution_id"] == resolution_id
    assert resume["source_run_id"] == source_run_id
    assert resume["attempt_run_id"] == retry_attempt_run_id
    assert resume["previous_attempt_run_id"] == first_attempt_run_id
    assert resume["attempt_number"] == 2
    assert resume["original_intent"] == original_intent
    assert resume["material_slots"] == material_slots
    assert resume["accepted_choice"] == material_action
    assert resume["accepted_graph"] == ()
    assert resume["analysis_contract"] == {}
    assert resume["analysis_route"] == {}

    serialized_request = json.dumps(request, ensure_ascii=False, sort_keys=True)
    for stale_authority in (
        "2026-06-03T12:00:00+01:00",
        "release:old",
        "snapshot:old",
        "query:old",
        "analysis:old",
        "execution-material:old",
    ):
        assert stale_authority not in serialized_request
    assert fresh_context["as_of"] in serialized_request


def _compiled_gateway_run(
    source: str,
    *,
    fake_python: str,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="waje-resolution-retry-") as tmp:
        out_dir = Path(tmp) / "compiled"
        compile_result = subprocess.run(
            [
                "npx",
                "tsc",
                "--ignoreConfig",
                "--noEmit",
                "false",
                "--module",
                "node16",
                "--moduleResolution",
                "node16",
                "--target",
                "ES2022",
                "--esModuleInterop",
                "--skipLibCheck",
                "--outDir",
                str(out_dir),
                "--rootDir",
                str(ROOT),
                *GATEWAY_SOURCES,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert compile_result.returncode == 0, (
            compile_result.stdout + compile_result.stderr
        )

        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "python3"
        executable.write_text(
            fake_python.replace(
                "#!/usr/bin/env python3",
                f"#!{sys.executable}",
                1,
            ),
            encoding="utf-8",
        )
        executable.chmod(
            executable.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
        process_env = {
            **os.environ,
            "NODE_PATH": str(ROOT / "node_modules"),
            "GATEWAY_OUT": str(out_dir),
            "WAJE_GATEWAY_TEST_TMP": tmp,
            "NODE_ENV": "test",
            "WAJE_GATEWAY_UNIT_TEST_STORE": "memory",
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
        }
        process_env.pop("WAJE_RUNTIME_DATABASE_URL", None)
        process_env.pop("DATABASE_URL", None)
        result = subprocess.run(
            ["node", "-e", source],
            cwd=ROOT,
            env=process_env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])


def _fail_once_then_complete_agent_core() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json
        import os
        import sys

        output_path = os.path.join(
            os.environ["WAJE_GATEWAY_TEST_TMP"],
            "agent-invocations.jsonl",
        )
        clarification = {}
        if "--clarification" in sys.argv:
            clarification = json.loads(
                sys.argv[sys.argv.index("--clarification") + 1]
            )
        invocation = {
            "runId": sys.argv[sys.argv.index("--run-id") + 1],
            "message": sys.argv[sys.argv.index("--message") + 1],
            "clarification": clarification,
        }
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(invocation, ensure_ascii=False) + "\\n")
        with open(output_path, "r", encoding="utf-8") as handle:
            attempt_number = len([line for line in handle if line.strip()])

        run_id = invocation["runId"]
        if attempt_number == 1:
            print(json.dumps({
                "status": "failed",
                "run_id": run_id,
                "turn_id": "turn-attempt-1",
                "topic_id": "topic-source",
                "failure_reason": "workflow_failed_after_clarification",
            }))
        else:
            print(json.dumps({
                "status": "completed",
                "run_id": run_id,
                "turn_id": "turn-attempt-2",
                "topic_id": "topic-source",
                "context_manifest": {"manifest_id": "manifest-attempt-2"},
                "answer_package": {"run_id": run_id, "status": "completed"},
            }))
        """
    )
