from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RoadmapRebaselineTest(unittest.TestCase):
    def test_roadmap_is_rebased_on_the_single_authority_phases(self):
        roadmap = (ROOT / "docs" / "implementation-roadmap.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "Architecture authority: [2026-07-17 single-authority workflow ADR]",
            roadmap,
        )
        for phase in range(8):
            self.assertIn(f"## Phase {phase}:", roadmap)
        self.assertIn("The cutover has no backward-compatibility path", roadmap)
        self.assertNotIn("Post-Phase 4 Rebaseline", roadmap)


if __name__ == "__main__":
    unittest.main()
