from pathlib import Path
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
        route = (
            ROOT / "app" / "api" / "artifacts" / "[artifactId]" / "continue" / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("WAJE_GATEWAY_ROLE", route)
        self.assertNotIn("body.role", route)


if __name__ == "__main__":
    unittest.main()
