import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayTypeScriptContractTest(unittest.TestCase):
    def test_gateway_store_fails_closed_without_postgres_or_unit_test_injection(self):
        result = _run_typescript(
            textwrap.dedent(
                """
                const { conversationStoreMode } = await import(
                  "./app/api/_conversationStore.ts"
                );
                let error = "";
                try {
                  conversationStoreMode();
                } catch (caught) {
                  error = caught instanceof Error ? caught.message : String(caught);
                }
                console.log(JSON.stringify({ error }));
                """
            ),
            unit_test_store=False,
        )

        self.assertEqual(
            result["error"],
            "WAJE_RUNTIME_DATABASE_URL or DATABASE_URL is required",
        )

    def test_gateway_spawn_failure_terminalizes_queued_run_idempotently(self):
        result = _run_typescript(
            textwrap.dedent(
                """
                const {
                  createRun,
                  createThread,
                  failQueuedRunDispatch,
                  requireRun,
                } = await import("./app/api/_conversationStore.ts");
                const thread = await createThread("dispatch-owner");
                const run = await createRun(thread.id);
                const failed = await failQueuedRunDispatch(
                  run.id,
                  "agent_core_spawn_failed",
                );
                const replay = await failQueuedRunDispatch(
                  run.id,
                  "agent_core_spawn_failed",
                );
                const persisted = await requireRun(run.id);
                console.log(JSON.stringify({ failed, replay, persisted }));
                """
            )
        )

        self.assertEqual(result["failed"]["status"], "failed")
        self.assertEqual(result["replay"]["status"], "failed")
        self.assertEqual(result["persisted"]["status"], "failed")

    def test_inline_spawn_error_is_typed_failure(self):
        completed = _run_typescript_process(
            textwrap.dedent(
                """
                const { runAgentCore } = await import("./app/api/_agentCore.ts");
                const result = await runAgentCore(
                  "thread-inline-spawn",
                  "run-inline-spawn",
                  "message",
                  "business_reader",
                  { forceInline: true, runtimePermissionScope: "viewer" },
                );
                console.log(JSON.stringify(result));
                """
            ),
            env={**os.environ, "PATH": "/nonexistent"},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["result"])
        self.assertEqual(result["error"], "agent_core_spawn_failed")

    def test_detached_spawn_error_is_typed_failure(self):
        completed = _run_typescript_process(
            textwrap.dedent(
                """
                const { runAgentCore } = await import("./app/api/_agentCore.ts");
                delete process.env.WAJE_AGENT_CORE_INLINE;
                const result = await runAgentCore(
                  "thread-detached-spawn",
                  "run-detached-spawn",
                  "message",
                  "business_reader",
                  { runtimePermissionScope: "viewer" },
                );
                console.log(JSON.stringify(result));
                """
            ),
            env={**os.environ, "PATH": "/nonexistent"},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["result"])
        self.assertEqual(result["error"], "agent_core_spawn_failed")

    def test_inline_exit_zero_with_malformed_json_is_typed_failure(self):
        result = _run_agent_core_inline("not-json")

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["result"])
        self.assertEqual(result["error"], "agent_core_output_malformed_json")

    def test_inline_exit_zero_with_unknown_status_is_typed_failure(self):
        result = _run_agent_core_inline(json.dumps({"status": "unexpected"}))

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["result"])
        self.assertEqual(result["error"], "agent_core_output_status_invalid")

    def test_parser_rejects_truncated_success_contracts(self):
        result = _run_typescript(
            textwrap.dedent(
                """
                const { parseAgentCoreOutput } = await import("./app/api/_agentCore.ts");
                const common = {
                  run_id: "run-parser-contract",
                  turn_id: "turn-parser-contract",
                  topic_id: "topic-parser-contract",
                  context_manifest: {
                    manifest_id: "context-parser-contract",
                    thread_id: "thread-parser-contract",
                    turn_id: "turn-parser-contract",
                    topic_id: "topic-parser-contract",
                  },
                };
                const parsed = [
                  { status: "completed" },
                  { status: "completed", ...common },
                  {
                    status: "completed_without_workflow",
                    ...common,
                    context_manifest: [],
                  },
                  {
                    status: "waiting_for_clarification",
                    ...common,
                    topic_id: 42,
                    clarification: {},
                  },
                  {
                    status: "waiting_for_clarification",
                    ...common,
                    clarification: null,
                  },
                  {
                    status: "failed",
                    run_id: "run-parser-contract",
                    turn_id: "turn-parser-contract",
                    topic_id: null,
                  },
                ].map((payload) => parseAgentCoreOutput(JSON.stringify(payload)));
                console.log(JSON.stringify(parsed));
                """
            )
        )

        self.assertEqual(
            result,
            [
                {
                    "status": "failed",
                    "result": None,
                    "error": "agent_core_output_shape_invalid",
                }
            ]
            * 6,
        )

    def test_parser_accepts_complete_status_specific_contracts(self):
        result = _run_typescript(
            textwrap.dedent(
                """
                const { parseAgentCoreOutput } = await import("./app/api/_agentCore.ts");
                const common = {
                  run_id: "run-parser-contract",
                  turn_id: "turn-parser-contract",
                  topic_id: "topic-parser-contract",
                  context_manifest: {
                    manifest_id: "context-parser-contract",
                    thread_id: "thread-parser-contract",
                    turn_id: "turn-parser-contract",
                    topic_id: "topic-parser-contract",
                  },
                };
                const parsed = [
                  {
                    status: "completed",
                    ...common,
                    answer_package: { run_id: "run-parser-contract" },
                  },
                  {
                    status: "completed_without_workflow",
                    run_id: "run-parser-contract",
                    turn_id: "turn-parser-contract",
                    topic_id: null,
                    context_manifest: {},
                  },
                  {
                    status: "waiting_for_clarification",
                    ...common,
                    clarification: {},
                  },
                  {
                    status: "failed",
                    run_id: "run-parser-contract",
                    turn_id: "turn-parser-contract",
                    topic_id: null,
                    failure_reason: "workflow_runtime_failed",
                  },
                ].map((payload) => parseAgentCoreOutput(JSON.stringify(payload)).status);
                console.log(JSON.stringify(parsed));
                """
            )
        )

        self.assertEqual(
            result,
            [
                "completed",
                "completed_without_workflow",
                "waiting_for_clarification",
                "failed",
            ],
        )

    def test_agent_core_omitted_role_uses_least_privilege(self):
        result = _run_agent_core_inline(
            None,
            node_source=textwrap.dedent(
                """
                const { runAgentCore } = await import("./app/api/_agentCore.ts");
                const result = await runAgentCore(
                  "thread-runtime-role",
                  "run-runtime-role",
                  "message",
                  undefined,
                  { forceInline: true },
                );
                console.log(JSON.stringify(result));
                """
            ),
            fake_python_source=textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import sys

                role_index = sys.argv.index("--role") + 1
                scope_index = sys.argv.index("--runtime-permission-scope") + 1
                print(json.dumps({
                    "status": "completed",
                    "run_id": "run-runtime-role",
                    "turn_id": "turn-runtime-role",
                    "topic_id": "topic-runtime-role",
                    "context_manifest": {
                        "manifest_id": "context-runtime-role",
                        "thread_id": "thread-runtime-role",
                        "turn_id": "turn-runtime-role",
                        "topic_id": "topic-runtime-role",
                    },
                    "answer_package": {"run_id": "run-runtime-role"},
                    "received_role": sys.argv[role_index],
                    "received_scope": sys.argv[scope_index],
                }))
                """
            ),
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["received_role"], "business_reader")
        self.assertEqual(result["result"]["received_scope"], "viewer")

    def test_role_resolver_and_business_reader_filter_execute_typescript(self):
        result = _run_typescript(
            textwrap.dedent(
                """
                const {
                  filterAgentCoreForRole,
                  resolveGatewayRole,
                } = await import("./app/api/_conversationStore.ts");
                const decisions = {
                  missing: resolveGatewayRole(undefined, "production"),
                  unknown: resolveGatewayRole("unknown_role", "production"),
                  productionAdmin: resolveGatewayRole("data_owner_admin", "production"),
                  nonProductionAdmin: resolveGatewayRole("data_owner_admin", "test"),
                };
                const visible = filterAgentCoreForRole({
                  status: "completed",
                  command: "private-command",
                  output: "private-output",
                  error: "private-error",
                  future_internal_payload: { secret: "private-agent-sibling" },
                  result: {
                    run_id: "run-visible",
                    topic_id: "topic-visible",
                    status: "completed",
                    quality_review: { secret: "private-quality-review" },
                    answer_package: {
                      run_id: "run-visible",
                      status: "completed",
                      package_type: "analysis",
                      admin_audit: { secret: "private-audit" },
                      sections: [
                        {
                          section_id: "summary",
                          visibility: "business_summary",
                          payload: { text: "visible-summary" },
                        },
                        {
                          section_id: "diagnostics",
                          visibility: "diagnostic_detail",
                          payload: { text: "private-diagnostics" },
                        },
                        {
                          section_id: "admin",
                          visibility: "admin_only",
                          payload: { text: "private-admin" },
                        },
                      ],
                    },
                  },
                }, "business_reader");
                console.log(JSON.stringify({ decisions, visible }));
                """
            )
        )

        least_privilege = {
            "displayRole": "business_reader",
            "runtimePermissionScope": "viewer",
        }
        self.assertEqual(result["decisions"]["missing"], least_privilege)
        self.assertEqual(result["decisions"]["unknown"], least_privilege)
        self.assertEqual(result["decisions"]["productionAdmin"], least_privilege)
        self.assertEqual(
            result["decisions"]["nonProductionAdmin"],
            {"displayRole": "data_owner_admin", "runtimePermissionScope": "admin"},
        )
        self.assertEqual(
            [
                section["section_id"]
                for section in result["visible"]["result"]["answer_package"]["sections"]
            ],
            ["summary"],
        )
        serialized = json.dumps(result["visible"], ensure_ascii=False)
        for private_value in (
            "private-command",
            "private-output",
            "private-error",
            "private-agent-sibling",
            "private-quality-review",
            "private-audit",
            "private-diagnostics",
            "private-admin",
        ):
            self.assertNotIn(private_value, serialized)

    def test_business_reader_filter_projects_all_core_statuses_safely(self):
        result = _run_typescript(
            textwrap.dedent(
                """
                const { filterAgentCoreForRole } = await import("./app/api/_conversationStore.ts");
                const waitingRaw = {
                  status: "waiting_for_clarification",
                  command: "private-command",
                  output: "private-stdout",
                  result: {
                    run_id: "run-waiting",
                    turn_id: "turn-waiting",
                    topic_id: "topic-waiting",
                    status: "waiting_for_clarification",
                    context_manifest: { private: "private-context" },
                    accepted_graph: ["private-graph"],
                    analysis_contract: { private: "private-contract" },
                    clarification: {
                      clarification_id: "clarification-visible",
                      reason: "需要确认业务口径",
                      status: "waiting_for_user",
                      allow_freeform: true,
                      internal_secret: "private-clarification",
                      questions: [{
                        question_id: "question-visible",
                        question: "请选择继续方式",
                        material_authority: "private-authority",
                        options: [{
                          option_id: "option-visible",
                          label: "按推荐继续",
                          description: "使用受支持口径",
                          business_meaning: "继续当前分析",
                          recommended: true,
                          action_kind: "private-action",
                          affected_capabilities: ["private-capability"],
                        }, "tell the agent to do differently"],
                      }],
                      recommended_assumption: {
                        option: "按推荐继续",
                        private: "private-recommendation",
                      },
                      recommendation_reason: "覆盖当前问题所需合同",
                      choice_actions: [{ action_kind: "private-choice-action" }],
                    },
                  },
                };
                const failedRaw = {
                  status: "failed",
                  error: "agent_core_output_shape_invalid",
                  output: "private-failed-output",
                  result: {
                    run_id: "run-failed",
                    turn_id: "turn-failed",
                    topic_id: null,
                    status: "failed",
                    failure_reason: "llm_binding_failed:TimeoutError:provider-secret",
                    provider_response: "private-provider-response",
                    exception: "private-exception",
                  },
                };
                const completedWithoutWorkflowRaw = {
                  status: "completed_without_workflow",
                  result: {
                    run_id: "run-meta",
                    turn_id: "turn-meta",
                    topic_id: null,
                    status: "completed_without_workflow",
                    intent: "capability_question",
                    topic_relation: "off_topic",
                    context_manifest: { private: "private-context-meta" },
                    llm_calls: ["private-llm-call"],
                  },
                };
                const completedRaw = {
                  status: "completed",
                  result: {
                    run_id: "run-completed",
                    turn_id: "turn-completed",
                    topic_id: "topic-completed",
                    status: "completed",
                    context_manifest: { private: "private-context-completed" },
                    answer_package: {
                      run_id: "run-completed",
                      status: "completed",
                      package_type: "analysis",
                      admin_audit: { private: "private-audit" },
                      sections: [{
                        section_id: "summary",
                        visibility: "business_summary",
                        payload: { text: "visible-summary" },
                      }, {
                        section_id: "admin",
                        visibility: "admin_only",
                        payload: { text: "private-admin" },
                      }],
                    },
                  },
                };
                console.log(JSON.stringify({
                  waiting: filterAgentCoreForRole(waitingRaw, "business_reader"),
                  failed: filterAgentCoreForRole(failedRaw, "business_reader"),
                  completedWithoutWorkflow: filterAgentCoreForRole(
                    completedWithoutWorkflowRaw,
                    "business_reader",
                  ),
                  completed: filterAgentCoreForRole(completedRaw, "business_reader"),
                  adminPreserved: filterAgentCoreForRole(waitingRaw, "data_owner_admin") === waitingRaw,
                }));
                """
            )
        )

        self.assertEqual(
            result["waiting"],
            {
                "status": "waiting_for_clarification",
                "result": {
                    "run_id": "run-waiting",
                    "turn_id": "turn-waiting",
                    "topic_id": "topic-waiting",
                    "status": "waiting_for_clarification",
                    "clarification": {
                        "clarification_id": "clarification-visible",
                        "reason": "需要确认业务口径",
                        "status": "waiting_for_user",
                        "allow_freeform": True,
                        "questions": [
                            {
                                "question_id": "question-visible",
                                "question": "请选择继续方式",
                                "options": [
                                    {
                                        "id": "option-visible",
                                        "label": "按推荐继续",
                                        "description": "使用受支持口径",
                                        "recommended": True,
                                        "business_meaning": "继续当前分析",
                                    },
                                    {
                                        "label": "tell the agent to do differently",
                                        "description": "tell the agent to do differently",
                                        "business_meaning": "tell the agent to do differently",
                                    },
                                ],
                            }
                        ],
                        "recommended_assumption": {"option": "按推荐继续"},
                        "recommendation_reason": "覆盖当前问题所需合同",
                    },
                },
            },
        )
        self.assertEqual(
            result["failed"],
            {
                "status": "failed",
                "error": "agent_core_output_shape_invalid",
                "result": {
                    "run_id": "run-failed",
                    "turn_id": "turn-failed",
                    "topic_id": None,
                    "status": "failed",
                    "failure_reason": "agent_core_run_failed",
                },
            },
        )
        self.assertEqual(
            result["completedWithoutWorkflow"],
            {
                "status": "completed_without_workflow",
                "result": {
                    "run_id": "run-meta",
                    "turn_id": "turn-meta",
                    "topic_id": None,
                    "status": "completed_without_workflow",
                    "intent": "capability_question",
                    "topic_relation": "off_topic",
                },
            },
        )
        self.assertEqual(
            result["completed"]["result"]["answer_package"]["sections"],
            [
                {
                    "section_id": "summary",
                    "visibility": "business_summary",
                    "payload": {"text": "visible-summary"},
                }
            ],
        )
        self.assertTrue(result["adminPreserved"])
        serialized = json.dumps(result, ensure_ascii=False)
        for private_value in (
            "private-command",
            "private-stdout",
            "private-context",
            "private-graph",
            "private-contract",
            "private-clarification",
            "private-authority",
            "private-action",
            "private-capability",
            "private-recommendation",
            "private-choice-action",
            "provider-secret",
            "private-provider-response",
            "private-exception",
            "private-context-meta",
            "private-llm-call",
            "private-context-completed",
            "private-audit",
            "private-admin",
        ):
            self.assertNotIn(private_value, serialized)

    def test_nonzero_exit_with_complete_stdout_cannot_publish_answer_package(self):
        result = _run_agent_core_inline(
            None,
            node_source=textwrap.dedent(
                """
                const { runAgentCore } = await import("./app/api/_agentCore.ts");
                const { filterAgentCoreForRole } = await import("./app/api/_conversationStore.ts");
                const raw = await runAgentCore(
                  "thread-process-failure",
                  "run-process-failure",
                  "message",
                  "business_reader",
                  { forceInline: true, runtimePermissionScope: "viewer" },
                );
                const visible = filterAgentCoreForRole(raw, "business_reader");
                console.log(JSON.stringify({ raw, visible }));
                """
            ),
            fake_python_source=textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json

                print(json.dumps({
                    "status": "completed",
                    "run_id": "run-process-failure",
                    "turn_id": "turn-process-failure",
                    "topic_id": "topic-process-failure",
                    "context_manifest": {},
                    "answer_package": {
                        "run_id": "run-process-failure",
                        "status": "completed",
                        "package_type": "analysis",
                        "sections": [{
                            "section_id": "summary",
                            "visibility": "business_summary",
                            "payload": {"text": "must-not-publish"},
                        }],
                    },
                }))
                raise SystemExit(1)
                """
            ),
        )

        self.assertEqual(result["raw"]["status"], "failed")
        self.assertEqual(result["raw"]["result"]["status"], "completed")
        self.assertEqual(
            result["visible"],
            {
                "status": "failed",
                "error": "agent_core_process_failed",
                "result": {
                    "run_id": "run-process-failure",
                    "turn_id": "turn-process-failure",
                    "topic_id": "topic-process-failure",
                    "status": "failed",
                    "failure_reason": "agent_core_run_failed",
                },
            },
        )
        self.assertNotIn("must-not-publish", json.dumps(result["visible"]))

    def test_wrapper_status_is_authoritative_for_all_inner_status_mismatches(self):
        result = _run_typescript(
            textwrap.dedent(
                """
                const { filterAgentCoreForRole } = await import("./app/api/_conversationStore.ts");
                const common = {
                  run_id: "run-mismatch",
                  turn_id: "turn-mismatch",
                  topic_id: "topic-mismatch",
                };
                const inner = {
                  completed: {
                    ...common,
                    status: "completed",
                    answer_package: {
                      run_id: "run-mismatch",
                      sections: [{
                        section_id: "summary",
                        visibility: "business_summary",
                        payload: { text: "private-mismatch-package" },
                      }],
                    },
                  },
                  completed_without_workflow: {
                    ...common,
                    status: "completed_without_workflow",
                    intent: "private-mismatch-intent",
                  },
                  waiting_for_clarification: {
                    ...common,
                    status: "waiting_for_clarification",
                    clarification: { reason: "private-mismatch-clarification" },
                  },
                  failed: {
                    ...common,
                    status: "failed",
                    failure_reason: "private-provider-exception",
                  },
                };
                const cases = [
                  {
                    status: "failed",
                    error: "agent_core_process_failed",
                    result: inner.completed,
                  },
                  { status: "completed", result: inner.waiting_for_clarification },
                  { status: "completed_without_workflow", result: inner.failed },
                  { status: "waiting_for_clarification", result: inner.completed_without_workflow },
                ];
                console.log(JSON.stringify(cases.map((item) =>
                  filterAgentCoreForRole(item, "business_reader")
                )));
                """
            )
        )

        self.assertEqual(len(result), 4)
        for index, visible in enumerate(result):
            with self.subTest(index=index):
                self.assertEqual(visible["status"], "failed")
                self.assertEqual(visible["result"]["status"], "failed")
                self.assertEqual(
                    visible["result"]["failure_reason"],
                    "agent_core_run_failed",
                )
                self.assertNotIn("answer_package", visible["result"])
                self.assertNotIn("clarification", visible["result"])
                self.assertNotIn("intent", visible["result"])
        self.assertEqual(result[0]["error"], "agent_core_process_failed")
        for visible in result[1:]:
            self.assertEqual(visible["error"], "agent_core_run_failed")
        serialized = json.dumps(result)
        for private_value in (
            "private-mismatch-package",
            "private-mismatch-intent",
            "private-mismatch-clarification",
            "private-provider-exception",
        ):
            self.assertNotIn(private_value, serialized)

    def test_admin_status_mismatch_fails_closed_before_raw_bypass(self):
        result = _run_typescript(
            textwrap.dedent(
                """
                const { filterAgentCoreForRole } = await import("./app/api/_conversationStore.ts");
                const raw = {
                  status: "failed",
                  error: "agent_core_process_failed",
                  result: {
                    run_id: "run-admin-mismatch",
                    turn_id: "turn-admin-mismatch",
                    topic_id: "topic-admin-mismatch",
                    status: "completed",
                    answer_package: {
                      sections: [{ payload: { text: "private-admin-mismatch-package" } }],
                    },
                  },
                };
                const visible = filterAgentCoreForRole(raw, "data_owner_admin");
                console.log(JSON.stringify({ same: visible === raw, visible }));
                """
            )
        )

        self.assertFalse(result["same"])
        self.assertEqual(
            result["visible"],
            {
                "status": "failed",
                "error": "agent_core_process_failed",
                "result": {
                    "run_id": "run-admin-mismatch",
                    "turn_id": "turn-admin-mismatch",
                    "topic_id": "topic-admin-mismatch",
                    "status": "failed",
                    "failure_reason": "agent_core_run_failed",
                },
            },
        )
        self.assertNotIn("private-admin-mismatch-package", json.dumps(result))

    def test_gateway_preserves_reviewed_runtime_publication_stage_codes(self):
        reasons = _run_typescript(
            textwrap.dedent(
                """
                const { filterAgentCoreForRole } = await import("./app/api/_conversationStore.ts");
                const reasons = [
                  "material_authority_projection_failed",
                  "analysis_runtime_bundle_validation_failed",
                  "analysis_runtime_artifact_sync_failed",
                  "analysis_runtime_store_commit_failed",
                ];
                console.log(JSON.stringify(reasons.map((failure_reason, index) =>
                  filterAgentCoreForRole({
                    status: "failed",
                    result: {
                      run_id: `run-stage-${index}`,
                      turn_id: `turn-stage-${index}`,
                      topic_id: `topic-stage-${index}`,
                      status: "failed",
                      failure_reason,
                    },
                  }, "business_reader").result.failure_reason
                )));
                """
            )
        )

        self.assertEqual(
            reasons,
            [
                "material_authority_projection_failed",
                "analysis_runtime_bundle_validation_failed",
                "analysis_runtime_artifact_sync_failed",
                "analysis_runtime_store_commit_failed",
            ],
        )


def _run_agent_core_inline(
    stdout: str | None,
    *,
    node_source: str | None = None,
    fake_python_source: str | None = None,
):
    source = node_source or textwrap.dedent(
        """
        const { runAgentCore } = await import("./app/api/_agentCore.ts");
        const result = await runAgentCore(
          "thread-parser",
          "run-parser",
          "message",
          "business_reader",
          { forceInline: true, runtimePermissionScope: "viewer" },
        );
        console.log(JSON.stringify(result));
        """
    )
    python_source = fake_python_source or textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import os
        import sys

        sys.stdout.write(os.environ["FAKE_AGENT_CORE_STDOUT"])
        """
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        fake_python = Path(temporary_directory) / "python3"
        python_source = python_source.replace(
            "#!/usr/bin/env python3",
            f"#!{sys.executable}",
            1,
        )
        fake_python.write_text(python_source, encoding="utf-8")
        fake_python.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{temporary_directory}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_AGENT_CORE_STDOUT": stdout or "",
        }
        return _run_typescript(source, env=env)


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
        raise RuntimeError("node executable is required for Gateway TypeScript contract tests")
    process_env = {
        **os.environ,
        **(env or {}),
    }
    if unit_test_store:
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


if __name__ == "__main__":
    unittest.main()
