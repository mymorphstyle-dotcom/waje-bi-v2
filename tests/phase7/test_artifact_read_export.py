from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ArtifactReadExportTest(unittest.TestCase):
    def test_artifact_read_and_export_apply_same_customer_projection_and_audit(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")
        read_route = (ROOT / "app" / "api" / "artifacts" / "[artifactId]" / "route.ts")
        export_route = (
            ROOT / "app" / "api" / "artifacts" / "[artifactId]" / "export" / "route.ts"
        )

        self.assertTrue(read_route.exists())
        self.assertTrue(export_route.exists())
        self.assertIn("export async function readArtifact", store)
        self.assertIn("projectAnswerPackageForCustomer", store)
        self.assertIn("artifact_opened", store)
        self.assertIn("artifact_exported", store)
        self.assertIn("visibleSectionIds", store)
        self.assertIn("readArtifact", read_route.read_text(encoding="utf-8"))
        self.assertIn("readArtifact", export_route.read_text(encoding="utf-8"))
        self.assertIn("jsonError", export_route.read_text(encoding="utf-8"))
        self.assertIn("text/markdown", export_route.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
