from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WorkbenchProcessRenderingTest(unittest.TestCase):
    def test_workbench_maps_only_current_single_authority_nodes(self):
        workbench = (
            ROOT / "app" / "agent-run-workbench" / "AgentRunWorkbench.tsx"
        ).read_text(encoding="utf-8")
        canvas = (
            ROOT / "app" / "agent-run-workbench" / "WorkflowCanvasModal.tsx"
        ).read_text(encoding="utf-8")

        for node in (
            "conversation_entry",
            "bind_intent",
            "generate_clarification",
            "persist_waiting_for_decision",
            "accept_material_decision",
            "compile_authoritative_plan",
            "execute_capability_dag",
            "evaluate_claim_coverage",
            "compile_plan_patch",
            "settle_claim_authority",
            "seal_authority_bundle",
            "compose_claim_aware_narrative",
            "publish_customer_projection",
            "deliver_publication",
        ):
            with self.subTest(node=node):
                self.assertIn(f'"{node}"', workbench + canvas)
        for legacy in (
            "question_tool",
            "accept_analysis_route",
            "execute_capabilities",
            "reduce_evidence",
            "hard_verify_answer",
            "repair_answer",
            "answer_verify",
            "persist_clarification",
            "clarification_policy_gate",
            "understand_business_intent",
            "decide_question_boundary",
        ):
            with self.subTest(legacy=legacy):
                self.assertNotIn(f'"{legacy}"', workbench + canvas)
        self.assertNotIn("addBypassBranch", canvas)
        self.assertNotIn("本轮未触发", canvas)
        self.assertIn("chronologicalTraceNodes", canvas)
        self.assertIn("traceNode.route", canvas)
        self.assertIn("bindClaimsByEvidenceRefs", canvas)
        self.assertIn("claim.evidenceRefs", canvas)
        self.assertNotIn("evidenceRef 未记录", canvas)
        self.assertNotIn("evidence.evidenceRef ||", canvas)
        self.assertNotIn("run.traceClaims.slice", canvas)
        self.assertIn("结论权威", canvas)
        self.assertIn("evidenceExecutionSummary(evidenceItems)", canvas)
        self.assertIn("evidenceBindingSummary(evidenceItems)", canvas)
        self.assertIn("evidence.executionState", canvas)
        self.assertIn("计划内能力记录", canvas)
        self.assertIn("item.taskId === task.taskId", canvas)
        self.assertIn("item.planRevisionId === task.planRevisionId", canvas)
        self.assertIn("acceptedTaskIdentity(task)", canvas)
        self.assertIn("`${task.planRevisionId}:${task.taskId}`", canvas)
        self.assertIn("traceNode.evidenceCompleteness", canvas)
        self.assertNotIn("item.capability === capability", canvas)

    def test_capability_cards_render_authoritative_task_outcomes(self):
        workbench = (
            ROOT / "app" / "agent-run-workbench" / "AgentRunWorkbench.tsx"
        ).read_text(encoding="utf-8")
        canvas = (
            ROOT / "app" / "agent-run-workbench" / "WorkflowCanvasModal.tsx"
        ).read_text(encoding="utf-8")
        styles = (
            ROOT / "app" / "phase4-replay" / "replay.module.css"
        ).read_text(encoding="utf-8")

        for status in (
            "not_started",
            "unsettled",
            "succeeded",
            "unavailable",
            "integrity_failed",
            "technical_failed",
            "skipped",
            "superseded",
        ):
            with self.subTest(status=status):
                self.assertIn(f'{status}: "', workbench + canvas)
                self.assertIn(f'data-task-status="{status}"', styles)
        for retryability in ("never", "same_input", "replan_required"):
            with self.subTest(retryability=retryability):
                self.assertIn(f'{retryability}: "', canvas)

        self.assertIn("acceptedTask.execution.failure?.businessBoundary", canvas)
        self.assertIn("acceptedTask.execution.limitationRefs", canvas)
        self.assertIn('task.execution.state === "not_started"', canvas)
        self.assertIn('task.execution.state === "unsettled"', canvas)
        self.assertIn('task.execution.status !== "succeeded"', canvas)
        self.assertIn("该任务终态不形成可发布结论绑定", canvas)
        self.assertNotIn("证据详情未记录", canvas)
        self.assertNotIn("technicalDetailRef", canvas)
        self.assertNotIn("acceptedTask.execution.failure?.kind", canvas)
        self.assertNotIn("acceptedTask.execution.failure?.layer", canvas)

    def test_canvas_keeps_playback_visibility_separate_from_execution_outcome(self):
        canvas = (
            ROOT / "app" / "agent-run-workbench" / "WorkflowCanvasModal.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("const revealed = revealedNodeIds.has(traceNode.id)", canvas)
        self.assertIn("revealed,", canvas)
        self.assertIn("outcome = nodeOutcome(traceNode)", canvas)
        self.assertIn("return node.outcome", canvas)
        self.assertIn("run.traceCompleteness[key]", canvas)
        self.assertNotIn("as TraceRun &", canvas)
        self.assertNotIn("completed: traceNode.index <= visibleCount", canvas)

    def test_canvas_dialog_traps_focus_and_restores_the_opener(self):
        canvas = (
            ROOT / "app" / "agent-run-workbench" / "WorkflowCanvasModal.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('event.key !== "Tab"', canvas)
        self.assertIn("previouslyFocusedRef.current?.focus()", canvas)
        self.assertIn("aria-labelledby={titleId}", canvas)
        self.assertIn("closeButtonRef.current?.focus()", canvas)


if __name__ == "__main__":
    unittest.main()
