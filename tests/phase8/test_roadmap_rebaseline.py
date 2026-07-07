from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RoadmapRebaselineTest(unittest.TestCase):
    def test_roadmap_has_no_open_legacy_checkboxes_before_phase5(self):
        roadmap = (ROOT / "docs" / "implementation-roadmap.md").read_text(encoding="utf-8")
        legacy_section = roadmap.split("## Phases 0-4:", 1)[1].split("## Phase 5:", 1)[0]

        self.assertIn("## Phases 0-4: Historical Baseline", roadmap)
        self.assertIn("superseded by the 2026-07-07 Post-Phase 4 Rebaseline", legacy_section)
        self.assertNotIn("- [ ]", legacy_section)


if __name__ == "__main__":
    unittest.main()
