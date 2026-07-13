from pathlib import Path
import json
import unittest

from bi_agent.runtime.analysis_runtime import AnalysisRuntimeRequest


ROOT = Path(__file__).resolve().parents[2]
ROUTE = ROOT / "app" / "api" / "runs" / "[runId]" / "clarifications" / "route.ts"
HELPER = ROOT / "app" / "api" / "_agentCore.ts"


class GatewayClarificationsTest(unittest.TestCase):
    def test_clarification_route_records_answer_and_resumes_same_run(self):
        route = ROUTE.read_text(encoding="utf-8")
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("runAgentCore", route)
        self.assertIn("recordClarificationOutcome", route)
        self.assertIn("addUserMessage", route)
        self.assertIn("agentCore", route)
        self.assertNotIn("createRun", route)
        self.assertIn("clarification_answer_recorded", store)

    def test_clarification_route_forwards_full_payload_and_waits_for_resumed_result(self):
        route = ROUTE.read_text(encoding="utf-8")
        helper = HELPER.read_text(encoding="utf-8")
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertRegex(
            route,
            r"const clarificationPayload = \{\s*runId,\s*answer,\s*selectedOptionId: body\.selectedOptionId \?\? null,\s*source: \"user\"(?: as const)?,\s*\}",
        )
        self.assertRegex(
            route,
            r"runAgentCore\([^;]*clarification:\s*clarificationPayload[^;]*forceInline:\s*true[^;]*\)",
        )
        self.assertIn("resumedRunId: visibleResult.run_id ?? runId", route)
        self.assertIn("topicId: visibleResult.topic_id ?? null", route)
        self.assertIn("status: visibleAgentCore.status", route)
        self.assertNotIn("status: resumed.status ?? agentCore.status", route)
        self.assertIn("export function filterAnswerPackageForRole", store)
        self.assertIn("filterAgentCoreForRole", route)
        self.assertIn("export function filterAgentCoreForRole", store)
        self.assertIn("resolveGatewayRole", route)
        self.assertIn("export function resolveGatewayRole", store)
        self.assertIn(
            'return { displayRole: "business_reader", runtimePermissionScope: "viewer" }',
            store,
        )
        self.assertIn(
            'return { displayRole: "analyst", runtimePermissionScope: "analyst" }',
            store,
        )
        self.assertIn(
            'return { displayRole: "data_owner_admin", runtimePermissionScope: "admin" }',
            store,
        )
        self.assertIn(
            'role === "data_owner_admin" && nodeEnv !== "production"',
            store,
        )
        self.assertRegex(
            route,
            r"const roleDecision = resolveGatewayRole\(\s*process\.env\.WAJE_GATEWAY_ROLE,\s*process\.env\.NODE_ENV,?\s*\)",
        )
        self.assertNotIn('process.env.WAJE_GATEWAY_ROLE || "analyst"', route)
        self.assertIn(
            "filterAnswerPackageForRole(resumed.answer_package, role)",
            store,
        )
        self.assertIn("answerPackagePreview = visibleResult.answer_package ?? null", route)
        self.assertIn("agentCore: visibleAgentCore", route)
        self.assertRegex(
            route,
            r"filterAgentCoreForRole\(\s*agentCore[^;]*roleDecision\.displayRole,?\s*\)",
        )
        self.assertRegex(
            route,
            r"runAgentCore\([^;]*roleDecision\.displayRole[^;]*runtimePermissionScope:\s*roleDecision\.runtimePermissionScope[^;]*clarification:",
        )
        self.assertNotIn("body.role", route)
        self.assertNotIn("...agentCore", route)
        self.assertNotIn("...resumed", route)

        self.assertRegex(helper, r"clarification\?:\s*\{")
        self.assertIn("selectedOptionId?: string | null", helper)
        self.assertIn('source?: "user"', helper)
        self.assertIn('"--runtime-permission-scope"', helper)
        self.assertIn("options.runtimePermissionScope", helper)
        self.assertIn('"--clarification"', helper)
        self.assertIn("JSON.stringify(options.clarification)", helper)
        self.assertRegex(helper, r"options\.forceInline\s*\|\|\s*process\.env\.WAJE_AGENT_CORE_INLINE === \"1\"")

    def test_clarification_route_behavior_with_stubbed_dependencies(self):
        self.assertEqual(_typescript_route_test_runtime(), [])
        route = ROUTE.read_text(encoding="utf-8")
        self.assertIn("forceInline: true", route)
        self.assertIn("clarification: clarificationPayload", route)

        calls = {}

        def require_run(run_id):
            calls["requireRun"] = run_id
            return {"id": run_id, "threadId": "thread-123"}

        def add_user_message(thread_id, answer):
            calls["addUserMessage"] = {"threadId": thread_id, "answer": answer}
            return {"id": "message-1", "threadId": thread_id, "content": answer}

        def record_clarification_outcome(payload):
            calls["recordClarificationOutcome"] = dict(payload)
            return {**payload, "status": "accepted"}

        def run_agent_core(thread_id, run_id, answer, role, options):
            calls["runAgentCore"] = {
                "threadId": thread_id,
                "runId": run_id,
                "answer": answer,
                "role": role,
                "options": options,
            }
            return {
                "status": "completed",
                "result": {
                    "run_id": "run-resumed",
                    "topic_id": "topic-456",
                    "status": "completed",
                    "answer_package": {"preview": "answer-package-preview"},
                },
            }

        response = _simulate_clarification_route_post(
            run_id="run-open",
            body={"answer": "按推荐继续", "selectedOptionId": "daily_remove_top_positive_day"},
            role="data_owner_admin",
            require_run=require_run,
            add_user_message=add_user_message,
            record_clarification_outcome=record_clarification_outcome,
            run_agent_core=run_agent_core,
        )

        expected_payload = {
            "runId": "run-open",
            "answer": "按推荐继续",
            "selectedOptionId": "daily_remove_top_positive_day",
            "source": "user",
        }
        self.assertEqual(calls["recordClarificationOutcome"], expected_payload)
        self.assertEqual(calls["runAgentCore"]["threadId"], "thread-123")
        self.assertEqual(calls["runAgentCore"]["runId"], "run-open")
        self.assertEqual(calls["runAgentCore"]["answer"], "按推荐继续")
        self.assertEqual(calls["runAgentCore"]["role"], "data_owner_admin")
        self.assertEqual(
            calls["runAgentCore"]["options"],
            {
                "clarification": expected_payload,
                "forceInline": True,
                "runtimePermissionScope": "admin",
            },
        )
        self.assertEqual(response["runId"], "run-open")
        self.assertEqual(response["resumedRunId"], "run-resumed")
        self.assertEqual(response["topicId"], "topic-456")
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["answerPackagePreview"], {"preview": "answer-package-preview"})
        self.assertEqual(response["eventsUrl"], "/api/runs/run-open/events")

    def test_clarification_route_filters_inline_answer_package_for_non_admin_roles(self):
        answer_package = {
            "run_id": "run-resumed",
            "status": "completed",
            "package_type": "analysis",
            "admin_audit": {"secret": "internal-authority"},
            "sections": [
                {
                    "section_id": "summary",
                    "visibility": "business_summary",
                    "payload": {"text": "visible"},
                },
                {
                    "section_id": "diagnostics",
                    "visibility": "diagnostic_detail",
                    "payload": {"text": "analyst-only"},
                },
                {
                    "section_id": "admin_audit",
                    "visibility": "admin_only",
                    "payload": {"secret": "internal-authority"},
                },
            ],
        }

        for role in ("analyst", "business_reader"):
            with self.subTest(role=role):
                response = _simulate_clarification_route_post(
                    run_id="run-open",
                    body={"answer": "按推荐继续"},
                    role=role,
                    require_run=lambda run_id: {"id": run_id, "threadId": "thread-123"},
                    add_user_message=lambda thread_id, answer: {
                        "id": "message-1",
                        "threadId": thread_id,
                        "content": answer,
                    },
                    record_clarification_outcome=lambda payload: {
                        **payload,
                        "status": "accepted",
                    },
                    run_agent_core=lambda *_args: {
                        "status": "completed",
                        "output": json.dumps(
                            {"answer_package": answer_package}, ensure_ascii=False
                        ),
                        "result": {
                            "run_id": "run-resumed",
                            "status": "completed",
                            "answer_package": answer_package,
                        },
                    },
                )

                self.assertNotIn("admin_audit", response["answerPackagePreview"])
                self.assertNotIn(
                    "admin_audit",
                    response["agentCore"]["result"]["answer_package"],
                )
                visible_sections = {
                    section["section_id"]
                    for section in response["answerPackagePreview"]["sections"]
                }
                self.assertNotIn("admin_audit", visible_sections)
                self.assertNotIn(
                    "admin_audit",
                    json.dumps(response, ensure_ascii=False),
                )
                if role == "business_reader":
                    self.assertNotIn("diagnostics", visible_sections)

    def test_clarification_route_allowlists_non_admin_agent_core_and_result_fields(self):
        answer_package = {
            "run_id": "run-resumed",
            "status": "completed",
            "package_type": "analysis",
            "admin_audit": {"authority": "private-audit"},
            "sections": [
                {
                    "section_id": "summary",
                    "visibility": "business_summary",
                    "payload": {"text": "visible"},
                }
            ],
        }
        raw_agent_core = {
            "status": "completed",
            "command": "private-command",
            "output": "private-stdout",
            "error": "private-error",
            "stderr": "private-stderr",
            "future_internal_payload": {"authority": "private-agent-sibling"},
            "result": {
                "run_id": "run-resumed",
                "topic_id": "topic-456",
                "status": "completed",
                "answer_package": answer_package,
                "llm_calls": [
                    {"raw_response_content": "private-provider-response"}
                ],
                "context_manifest": {"authority": "private-context"},
                "quality_review": {"authority": "private-quality-review"},
                "future_internal_result": {"authority": "private-result-sibling"},
            },
        }

        for role in ("analyst", "business_reader"):
            with self.subTest(role=role):
                response = _simulate_clarification_route_post(
                    run_id="run-open",
                    body={"answer": "按推荐继续"},
                    role=role,
                    require_run=lambda run_id: {"id": run_id, "threadId": "thread-123"},
                    add_user_message=lambda thread_id, answer: {
                        "id": "message-1",
                        "threadId": thread_id,
                        "content": answer,
                    },
                    record_clarification_outcome=lambda payload: {
                        **payload,
                        "status": "accepted",
                    },
                    run_agent_core=lambda *_args: raw_agent_core,
                )

                self.assertEqual(
                    set(response["agentCore"]),
                    {"status", "result"},
                )
                self.assertEqual(
                    set(response["agentCore"]["result"]),
                    {"run_id", "topic_id", "status", "answer_package"},
                )
                self.assertEqual(
                    response["agentCore"]["result"]["answer_package"],
                    response["answerPackagePreview"],
                )
                serialized = json.dumps(response, ensure_ascii=False)
                for private_value in (
                    "private-command",
                    "private-stdout",
                    "private-error",
                    "private-stderr",
                    "private-agent-sibling",
                    "private-provider-response",
                    "private-context",
                    "private-quality-review",
                    "private-result-sibling",
                    "private-audit",
                ):
                    self.assertNotIn(private_value, serialized)

    def test_gateway_role_resolver_fails_closed_without_authenticated_principal(self):
        self.assertEqual(
            _resolve_gateway_role("business_reader", "production"),
            {"displayRole": "business_reader", "runtimePermissionScope": "viewer"},
        )
        self.assertEqual(
            _resolve_gateway_role("analyst", "production"),
            {"displayRole": "analyst", "runtimePermissionScope": "analyst"},
        )
        least_privilege = {
            "displayRole": "business_reader",
            "runtimePermissionScope": "viewer",
        }
        self.assertEqual(_resolve_gateway_role(None, "production"), least_privilege)
        self.assertEqual(_resolve_gateway_role("", "production"), least_privilege)
        self.assertEqual(_resolve_gateway_role("unknown_role", "production"), least_privilege)
        self.assertEqual(
            _resolve_gateway_role("data_owner_admin", "production"),
            least_privilege,
        )
        self.assertEqual(
            _resolve_gateway_role("data_owner_admin", "development"),
            {"displayRole": "data_owner_admin", "runtimePermissionScope": "admin"},
        )
        self.assertEqual(
            _resolve_gateway_role("data_owner_admin", "test"),
            {"displayRole": "data_owner_admin", "runtimePermissionScope": "admin"},
        )

    def test_gateway_runtime_role_mapping_is_accepted_by_analysis_runtime_request(self):
        cases = (
            ("business_reader", "production", "viewer"),
            ("analyst", "production", "analyst"),
            ("data_owner_admin", "test", "admin"),
            ("unknown_role", "production", "viewer"),
        )
        for configured_role, node_env, expected_runtime_role in cases:
            with self.subTest(configured_role=configured_role, node_env=node_env):
                decision = _resolve_gateway_role(configured_role, node_env)
                request = AnalysisRuntimeRequest.create(
                    run_id="run-role-mapping",
                    proposal={},
                    accepted_graph=(),
                    as_of="2026-06-03T12:00:00+01:00",
                    permission_scope=decision["runtimePermissionScope"],
                    run_mode="fixture",
                )
                self.assertEqual(request.permission_scope, expected_runtime_role)

        for display_role in ("business_reader", "data_owner_admin"):
            with self.subTest(invalid_direct_display_role=display_role):
                with self.assertRaisesRegex(
                    PermissionError,
                    "analysis_runtime_permission_scope_invalid",
                ):
                    AnalysisRuntimeRequest.create(
                        run_id="run-invalid-display-role",
                        proposal={},
                        accepted_graph=(),
                        as_of="2026-06-03T12:00:00+01:00",
                        permission_scope=display_role,
                        run_mode="fixture",
                    )

    def test_clarification_route_uses_same_effective_role_for_core_and_output_filtering(self):
        observed_roles = []
        answer_package = {
            "run_id": "run-resumed",
            "status": "completed",
            "sections": [
                {
                    "section_id": "admin",
                    "visibility": "admin_only",
                    "payload": {"private": True},
                },
                {
                    "section_id": "summary",
                    "visibility": "business_summary",
                    "payload": {"text": "visible"},
                },
            ],
        }

        for configured_role, node_env in (
            (None, "production"),
            ("unknown_role", "production"),
            ("data_owner_admin", "production"),
        ):
            with self.subTest(configured_role=configured_role, node_env=node_env):
                def run_agent_core(_thread_id, _run_id, _answer, role, options):
                    observed_roles.append((role, options["runtimePermissionScope"]))
                    return {
                        "status": "completed",
                        "output": "private-output",
                        "result": {
                            "run_id": "run-resumed",
                            "status": "completed",
                            "answer_package": answer_package,
                        },
                    }

                response = _simulate_clarification_route_post(
                    run_id="run-open",
                    body={"answer": "按推荐继续"},
                    role=configured_role,
                    node_env=node_env,
                    require_run=lambda run_id: {"id": run_id, "threadId": "thread-123"},
                    add_user_message=lambda thread_id, answer: {
                        "id": "message-1",
                        "threadId": thread_id,
                        "content": answer,
                    },
                    record_clarification_outcome=lambda payload: {
                        **payload,
                        "status": "accepted",
                    },
                    run_agent_core=run_agent_core,
                )

                self.assertEqual(observed_roles[-1], ("business_reader", "viewer"))
                self.assertNotIn("private-output", json.dumps(response, ensure_ascii=False))
                self.assertEqual(
                    [
                        section["section_id"]
                        for section in response["answerPackagePreview"]["sections"]
                    ],
                    ["summary"],
                )

    def test_client_cannot_override_gateway_role_decision(self):
        observed_roles = []
        answer_package = {
            "run_id": "run-resumed",
            "status": "completed",
            "sections": [
                {
                    "section_id": "admin",
                    "visibility": "admin_only",
                    "payload": {"private": True},
                },
                {
                    "section_id": "summary",
                    "visibility": "business_summary",
                    "payload": {"text": "visible"},
                },
            ],
        }

        def run_agent_core(_thread_id, _run_id, _answer, role, options):
            observed_roles.append((role, options["runtimePermissionScope"]))
            return {
                "status": "completed",
                "result": {
                    "run_id": "run-resumed",
                    "status": "completed",
                    "answer_package": answer_package,
                },
            }

        response = _simulate_clarification_route_post(
            run_id="run-open",
            body={
                "answer": "按推荐继续",
                "role": "data_owner_admin",
                "permission_scope": "admin",
            },
            role="business_reader",
            node_env="test",
            require_run=lambda run_id: {"id": run_id, "threadId": "thread-123"},
            add_user_message=lambda thread_id, answer: {
                "id": "message-1",
                "threadId": thread_id,
                "content": answer,
            },
            record_clarification_outcome=lambda payload: {**payload, "status": "accepted"},
            run_agent_core=run_agent_core,
        )

        self.assertEqual(observed_roles, [("business_reader", "viewer")])
        self.assertEqual(
            [
                section["section_id"]
                for section in response["answerPackagePreview"]["sections"]
            ],
            ["summary"],
        )

    def test_clarification_route_preserves_inline_answer_package_for_admin(self):
        answer_package = {
            "run_id": "run-resumed",
            "status": "completed",
            "admin_audit": {"secret": "internal-authority"},
            "sections": [],
        }
        raw_agent_core = {
            "status": "completed",
            "command": "bi_agent.conversation.agent_core",
            "output": json.dumps(
                {"answer_package": answer_package}, ensure_ascii=False
            ),
            "error": "private-admin-stderr",
            "future_internal_payload": {"private": True},
            "result": {
                "run_id": "run-resumed",
                "status": "completed",
                "answer_package": answer_package,
                "llm_calls": [{"raw_response_content": "private-admin-response"}],
                "context_manifest": {"private": True},
                "quality_review": {"private": True},
            },
        }
        response = _simulate_clarification_route_post(
            run_id="run-open",
            body={"answer": "按推荐继续"},
            role="data_owner_admin",
            require_run=lambda run_id: {"id": run_id, "threadId": "thread-123"},
            add_user_message=lambda thread_id, answer: {
                "id": "message-1",
                "threadId": thread_id,
                "content": answer,
            },
            record_clarification_outcome=lambda payload: {**payload, "status": "accepted"},
            run_agent_core=lambda *_args: raw_agent_core,
        )

        self.assertEqual(response["answerPackagePreview"], answer_package)
        self.assertEqual(
            response["agentCore"]["result"]["answer_package"],
            answer_package,
        )
        self.assertEqual(response["agentCore"], raw_agent_core)
        self.assertIn("admin_audit", response["agentCore"]["output"])


def _typescript_route_test_runtime():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    installed = {
        **package.get("dependencies", {}),
        **package.get("devDependencies", {}),
    }
    return [
        name
        for name in ("vitest", "jest", "tsx", "ts-node")
        if name in installed
    ]


def _simulate_clarification_route_post(
    *,
    run_id,
    body,
    role,
    node_env="test",
    require_run,
    add_user_message,
    record_clarification_outcome,
    run_agent_core,
):
    answer = str(body.get("answer") or body.get("choice") or "").strip()
    if not answer:
        return {"error": "clarification_answer_required", "statusCode": 400}
    run = require_run(run_id)
    message = add_user_message(run["threadId"], answer)
    clarification_payload = {
        "runId": run_id,
        "answer": answer,
        "selectedOptionId": body.get("selectedOptionId") or None,
        "source": "user",
    }
    clarification = record_clarification_outcome(clarification_payload)
    role_decision = _resolve_gateway_role(role, node_env)
    agent_core = run_agent_core(
        run["threadId"],
        run_id,
        answer,
        role_decision["displayRole"],
        {
            "clarification": clarification_payload,
            "forceInline": True,
            "runtimePermissionScope": role_decision["runtimePermissionScope"],
        },
    )
    resumed = agent_core.get("result") if isinstance(agent_core.get("result"), dict) else {}
    visible_agent_core = _filter_agent_core_for_role(
        agent_core,
        role_decision["displayRole"],
    )
    visible_result = (
        visible_agent_core.get("result")
        if isinstance(visible_agent_core.get("result"), dict)
        else {}
    )
    answer_package_preview = visible_result.get("answer_package")
    return {
        "runId": run_id,
        "resumedRunId": resumed.get("run_id") or run_id,
        "topicId": resumed.get("topic_id"),
        "status": resumed.get("status") or agent_core.get("status"),
        "answerPackagePreview": answer_package_preview,
        "message": message,
        "clarification": clarification,
        "agentCore": visible_agent_core,
        "eventsUrl": f"/api/runs/{run['id']}/events",
    }


def _filter_answer_package_for_role(answer_package, role):
    if role == "data_owner_admin":
        return answer_package
    allowed = (
        {"business_summary", "aggregate_evidence", "diagnostic_detail"}
        if role == "analyst"
        else {"business_summary", "aggregate_evidence"}
    )
    return {
        "run_id": answer_package.get("run_id"),
        "status": answer_package.get("status"),
        "package_type": answer_package.get("package_type"),
        "sections": [
            section
            for section in answer_package.get("sections", [])
            if isinstance(section, dict) and section.get("visibility") in allowed
        ],
    }


def _filter_agent_core_for_role(agent_core, role):
    if role == "data_owner_admin":
        return agent_core
    resumed = agent_core.get("result") if isinstance(agent_core.get("result"), dict) else {}
    raw_answer_package = resumed.get("answer_package")
    answer_package = (
        _filter_answer_package_for_role(raw_answer_package, role)
        if isinstance(raw_answer_package, dict)
        else None
    )
    return {
        "status": agent_core.get("status"),
        "result": {
            "run_id": resumed.get("run_id"),
            "topic_id": resumed.get("topic_id"),
            "status": resumed.get("status"),
            "answer_package": answer_package,
        },
    }


def _resolve_gateway_role(configured_role, node_env):
    role = str(configured_role or "").strip()
    if role in {"business_reader", "analyst"}:
        return {
            "displayRole": role,
            "runtimePermissionScope": (
                "viewer" if role == "business_reader" else "analyst"
            ),
        }
    if role == "data_owner_admin" and node_env != "production":
        return {"displayRole": role, "runtimePermissionScope": "admin"}
    return {"displayRole": "business_reader", "runtimePermissionScope": "viewer"}


if __name__ == "__main__":
    unittest.main()
