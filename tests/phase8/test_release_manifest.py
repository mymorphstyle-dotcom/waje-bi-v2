import unittest

from bi_agent.runtime.release_manifest import (
    REQUIRED_ROLLBACK_COMPONENTS,
    load_release_manifest,
    rollback_plan,
    validate_release_manifest,
)


class ReleaseManifestTest(unittest.TestCase):
    def test_manifest_covers_all_release_rollback_components(self):
        manifest = load_release_manifest()
        problems = validate_release_manifest(manifest)

        self.assertEqual(problems, [])
        components = {item["component"] for item in manifest["components"]}
        self.assertEqual(components, REQUIRED_ROLLBACK_COMPONENTS)

    def test_rollback_plan_names_refs_owner_paths_and_checks(self):
        plan = rollback_plan("prompt_recipe")

        self.assertEqual(plan["component"], "prompt_recipe")
        self.assertTrue(plan["paths"])
        self.assertTrue(plan["active_ref"])
        self.assertTrue(plan["rollback_ref"])
        self.assertTrue(plan["owner"])
        self.assertIn("full_acceptance_eval", plan["required_checks"])


if __name__ == "__main__":
    unittest.main()
