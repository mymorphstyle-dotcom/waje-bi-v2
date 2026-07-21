from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WorkbenchReplayStateSemanticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workbench = (
            ROOT / "app" / "agent-run-workbench" / "AgentRunWorkbench.tsx"
        ).read_text(encoding="utf-8")

    def test_static_snapshot_cannot_start_a_synthetic_timer_replay(self):
        self.assertIn('run.runMode !== "event_replay"', self.workbench)
        self.assertIn('run.traceCompleteness.chronology !== "known"', self.workbench)
        self.assertIn('setPlaybackState(run.runMode === "event_replay" ? "ready" : "snapshot")', self.workbench)
        self.assertNotIn("nodes.map((_, index) => index + 1)", self.workbench)

    def test_playback_cursor_and_business_outcome_are_independent(self):
        self.assertIn('type PlaybackState = "snapshot" | "ready" | "playing" | "completed"', self.workbench)
        self.assertIn('const playbackComplete = playbackState === "completed"', self.workbench)
        self.assertIn("runOutcomeLabel(active.runOutcome)", self.workbench)
        self.assertIn("run.lifecycle.verifier.outcome", self.workbench)
        self.assertNotIn('active.processSummary.verifierStatus === "passed"', self.workbench)
        self.assertNotIn('answer.status === "passed"', self.workbench)
        self.assertIn('delivery_pending: "发布等待交付"', self.workbench)
        self.assertIn('delivery_failed: "分析完成，交付失败"', self.workbench)

    def test_workbench_has_no_fake_follow_up_composer_or_bottom_autoscroll(self):
        self.assertNotIn("function Composer", self.workbench)
        self.assertNotIn("继续追问这次分析", self.workbench)
        self.assertIn('scrollTo({ top: 0, behavior: "auto" })', self.workbench)
        self.assertNotIn("messageListRef.current.scrollHeight", self.workbench)
        self.assertIn(
            "normalizeMessageText(message.text) === answerText", self.workbench
        )

    def test_run_selector_is_auditable_and_refreshable(self):
        self.assertIn("runOptionLabel(run)", self.workbench)
        self.assertIn("formatRunTime(run.generatedAt)", self.workbench)
        self.assertIn('return run.runId.replace(/^run-/, "").slice(-8)', self.workbench)
        self.assertIn("void loadRuns(activeId)", self.workbench)
        self.assertIn(
            "disabled={loading || refreshing || !runs.length}", self.workbench
        )

    def test_answer_uses_only_persisted_projection_fields(self):
        self.assertIn("answer.limitations.map", self.workbench)
        self.assertIn("key={item.evidenceRef}", self.workbench)
        self.assertIn("evidenceExecutionLabel(item.executionState)", self.workbench)
        self.assertIn("evidenceBindingLabel(item.bindingState)", self.workbench)
        self.assertIn("能力执行与证据边界", self.workbench)
        self.assertIn('item.planState === "superseded"', self.workbench)
        self.assertIn('run.lifecycle.verifier.status === "findings"', self.workbench)
        self.assertIn('run.lifecycle.publication.outcome === "complete"', self.workbench)
        self.assertNotIn('const accepted = runOutcome ===', self.workbench)
        self.assertNotIn("answer.visualBlocks", self.workbench)
        self.assertNotIn("item.evidenceRef ||", self.workbench)

    def test_current_transition_names_drive_todo_stages(self):
        for node in (
            "conversation_entry",
            "bind_intent",
            "generate_clarification",
            "persist_waiting_for_decision",
            "accept_material_decision",
            "compile_authoritative_plan",
            "execute_capability_dag",
            "evaluate_claim_coverage",
            "settle_claim_authority",
            "seal_authority_bundle",
            "compose_claim_aware_narrative",
            "publish_customer_projection",
            "deliver_publication",
        ):
            with self.subTest(node=node):
                self.assertIn(f'"{node}"', self.workbench)
        self.assertIn("return undefined", self.workbench)


if __name__ == "__main__":
    unittest.main()
