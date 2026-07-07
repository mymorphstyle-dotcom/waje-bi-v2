from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class AgentRunsPersistedAdapterTest(unittest.TestCase):
    def test_agent_runs_api_merges_persisted_answer_packages_into_trace_runs(self):
        agent_runs = (ROOT / "app" / "api" / "agent-runs" / "route.ts").read_text(encoding="utf-8")
        replays = (ROOT / "app" / "api" / "replays" / "route.ts").read_text(encoding="utf-8")
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("listPersistedAnswerPackageRuns", agent_runs)
        self.assertIn("traceRunFromAnswerPackage", agent_runs)
        self.assertIn("waje_runtime.answer_packages", store)
        self.assertIn("waje_runtime.analysis_runs", store)
        self.assertIn("export function traceRunFromAnswerPackage", replays)
        self.assertIn("persistedAnswerPackageRuns", agent_runs)


if __name__ == "__main__":
    unittest.main()
