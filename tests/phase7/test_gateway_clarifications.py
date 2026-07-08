from pathlib import Path
import json
import unittest


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

        self.assertRegex(
            route,
            r"const clarificationPayload = \{\s*runId,\s*answer,\s*selectedOptionId: body\.selectedOptionId \?\? null,\s*source: \"user\"(?: as const)?,\s*\}",
        )
        self.assertRegex(
            route,
            r"runAgentCore\([^;]*clarification:\s*clarificationPayload[^;]*forceInline:\s*true[^;]*\)",
        )
        self.assertIn("resumedRunId: resumed.run_id ?? runId", route)
        self.assertIn("topicId: resumed.topic_id ?? null", route)
        self.assertIn("status: resumed.status ?? agentCore.status", route)
        self.assertIn("answerPackagePreview: resumed.answer_package ?? null", route)

        self.assertRegex(helper, r"clarification\?:\s*\{")
        self.assertIn("selectedOptionId?: string | null", helper)
        self.assertIn('source?: "user"', helper)
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
            {"clarification": expected_payload, "forceInline": True},
        )
        self.assertEqual(response["runId"], "run-open")
        self.assertEqual(response["resumedRunId"], "run-resumed")
        self.assertEqual(response["topicId"], "topic-456")
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["answerPackagePreview"], {"preview": "answer-package-preview"})
        self.assertEqual(response["eventsUrl"], "/api/runs/run-open/events")


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
    agent_core = run_agent_core(
        run["threadId"],
        run_id,
        answer,
        role,
        {
            "clarification": clarification_payload,
            "forceInline": True,
        },
    )
    resumed = agent_core.get("result") if isinstance(agent_core.get("result"), dict) else {}
    return {
        "runId": run_id,
        "resumedRunId": resumed.get("run_id") or run_id,
        "topicId": resumed.get("topic_id"),
        "status": resumed.get("status") or agent_core.get("status"),
        "answerPackagePreview": resumed.get("answer_package"),
        "message": message,
        "clarification": clarification,
        "agentCore": agent_core,
        "eventsUrl": f"/api/runs/{run['id']}/events",
    }


if __name__ == "__main__":
    unittest.main()
