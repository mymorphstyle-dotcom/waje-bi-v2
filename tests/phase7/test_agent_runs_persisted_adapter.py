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

    def test_agent_runs_api_surfaces_waiting_clarification_runs(self):
        agent_runs = (ROOT / "app" / "api" / "agent-runs" / "route.ts").read_text(encoding="utf-8")
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("listPersistedRuntimeRuns", agent_runs)
        self.assertIn("traceRunFromRuntimeRun", agent_runs)
        self.assertIn("waiting_for_clarification", agent_runs)
        self.assertIn("question_tool", agent_runs)
        self.assertIn("clarification", agent_runs)
        self.assertIn("NOT EXISTS", store)
        self.assertIn("waje_runtime.analysis_runs", store)

    def test_persisted_answer_package_runs_include_recorded_run_nodes(self):
        agent_runs = (ROOT / "app" / "api" / "agent-runs" / "route.ts").read_text(encoding="utf-8")
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("runNodes", store)
        self.assertIn("waje_runtime.run_nodes", store)
        self.assertIn("checkpoint_events", agent_runs)
        self.assertIn("withRunNodes", agent_runs)


if __name__ == "__main__":
    unittest.main()
