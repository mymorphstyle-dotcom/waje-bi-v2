from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayArtifactContinueTest(unittest.TestCase):
    def test_artifact_continue_route_validates_artifact_visibility_before_creating_run(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")
        route = (
            ROOT / "app" / "api" / "artifacts" / "[artifactId]" / "continue" / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("requireArtifactForContinue", store)
        self.assertIn("permission_scope", store)
        self.assertIn("artifact_continue_blocked", store)
        self.assertIn("requireArtifactForContinue", route)
        try_block = route[route.index("try {") :]
        self.assertLess(
            try_block.index("requireArtifactForContinue"),
            try_block.index("addUserMessage"),
        )

    def test_artifact_continue_does_not_trust_client_supplied_role(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        route = (
            ROOT / "app" / "api" / "artifacts" / "[artifactId]" / "continue" / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("WAJE_GATEWAY_ROLE", route)
        self.assertIn("resolveGatewayRole", route)
        self.assertIn("filterAgentCoreForRole", route)
        self.assertIn('viewer: 1', store)
        self.assertIn('admin: 3', store)
        self.assertRegex(
            store,
            r"rank\[role\]\s*!==\s*undefined[^;]*rank\[permissionScope\]\s*!==\s*undefined",
        )
        self.assertIn("roleDecision.displayRole", route)
        self.assertIn(
            "runtimePermissionScope: roleDecision.runtimePermissionScope",
            route,
        )
        self.assertIn("agentCore: visibleAgentCore", route)
        self.assertNotIn("body.role", route)

    def test_artifact_continue_filters_inline_agent_core_for_non_admin(self):
        answer_package = {
            "run_id": "run-continue",
            "status": "completed",
            "admin_audit": {"private": "audit"},
            "sections": [
                {
                    "section_id": "summary",
                    "visibility": "business_summary",
                    "payload": {"text": "visible"},
                },
                {
                    "section_id": "admin",
                    "visibility": "admin_only",
                    "payload": {"private": "admin-section"},
                },
            ],
        }
        raw = {
            "status": "completed",
            "output": "private-output",
            "error": "private-error",
            "future_internal": "private-future",
            "result": {
                "run_id": "run-continue",
                "status": "completed",
                "answer_package": answer_package,
                "llm_calls": [{"raw_response_content": "private-provider"}],
                "context_manifest": {"private": "context"},
            },
        }

        visible = _filter_agent_core_for_role(raw, "business_reader")

        self.assertEqual(set(visible), {"status", "result"})
        self.assertEqual(
            set(visible["result"]),
            {"run_id", "topic_id", "status", "answer_package"},
        )
        serialized = json.dumps(visible, ensure_ascii=False)
        for private_value in (
            "private-output",
            "private-error",
            "private-future",
            "private-provider",
            "context",
            "audit",
            "admin-section",
        ):
            self.assertNotIn(private_value, serialized)

    def test_artifact_continue_preserves_full_inline_agent_core_for_local_admin(self):
        raw = {
            "status": "completed",
            "output": "admin-output",
            "error": "admin-error",
            "future_internal": {"private": True},
            "result": {"answer_package": {"admin_audit": {"private": True}}},
        }

        self.assertIs(_filter_agent_core_for_role(raw, "data_owner_admin"), raw)


def _filter_agent_core_for_role(agent_core, role):
    if role == "data_owner_admin":
        return agent_core
    resumed = agent_core.get("result") if isinstance(agent_core.get("result"), dict) else {}
    raw_package = resumed.get("answer_package")
    answer_package = None
    if isinstance(raw_package, dict):
        answer_package = {
            "run_id": raw_package.get("run_id"),
            "status": raw_package.get("status"),
            "package_type": raw_package.get("package_type"),
            "sections": [
                section
                for section in raw_package.get("sections", [])
                if isinstance(section, dict)
                and section.get("visibility")
                in {"business_summary", "aggregate_evidence"}
            ],
        }
    return {
        "status": agent_core.get("status"),
        "result": {
            "run_id": resumed.get("run_id"),
            "topic_id": resumed.get("topic_id"),
            "status": resumed.get("status"),
            "answer_package": answer_package,
        },
    }


if __name__ == "__main__":
    unittest.main()
