from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class AgentRunsPersistedAdapterTest(unittest.TestCase):
    def test_agent_runs_api_merges_sealed_customer_publications_into_trace_runs(self):
        agent_runs = (ROOT / "app" / "api" / "agent-runs" / "route.ts").read_text(
            encoding="utf-8"
        )
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        projection = (ROOT / "app/api/_customerRunProjection.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("listPersistedAgentRunCandidates", agent_runs)
        self.assertIn("listPersistedPublicationRuns", store)
        self.assertIn("traceRunFromCustomerPublication", agent_runs)
        self.assertIn("waje_runtime.publication_customer_payloads", store)
        self.assertIn("waje_runtime.delivery_outbox_records", store)
        self.assertIn("waje_runtime.delivery_attempts", store)
        self.assertIn("waje_runtime.analysis_runs", store)
        self.assertIn("export function traceRunFromCustomerPublication", projection)
        self.assertIn("persistedPublicationRuns", agent_runs)
        self.assertGreaterEqual(agent_runs.count("id: `run:${row.runId}`"), 2)
        self.assertNotIn("waje_runtime.answer_packages", store)

    def test_agent_runs_api_surfaces_waiting_clarification_runs(self):
        agent_runs = (ROOT / "app" / "api" / "agent-runs" / "route.ts").read_text(
            encoding="utf-8"
        )
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        projection = (ROOT / "app/api/_customerRunProjection.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("listPersistedAgentRunCandidates", agent_runs)
        self.assertIn("listPersistedRuntimeRuns", store)
        self.assertIn("traceRunFromRuntimeRun", agent_runs)
        self.assertIn("waiting_for_clarification", agent_runs)
        self.assertIn("persist_waiting_for_decision", projection)
        self.assertIn("clarification", projection)
        self.assertIn("publication_customer_payloads", store)
        self.assertIn("waje_runtime.analysis_runs", store)
        self.assertNotIn(
            "FROM waje_runtime.publication_customer_payloads customer\n"
            "      WHERE customer.run_attempt_id = r.run_attempt_id",
            store,
        )

    def test_persisted_publication_runs_include_safe_recorded_run_nodes(self):
        agent_runs = (ROOT / "app" / "api" / "agent-runs" / "route.ts").read_text(
            encoding="utf-8"
        )
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("runNodes", store)
        self.assertIn("waje_runtime.run_nodes", store)
        self.assertIn("runNodes: row.runNodes", agent_runs)
        self.assertIn("jsonb_build_object", store)
        self.assertNotIn("withRunNodes", agent_runs)

    def test_trace_projection_uses_the_prevalidated_customer_payload(self):
        projection = (ROOT / "app/api/_customerRunProjection.ts").read_text(
            encoding="utf-8"
        )
        contract = (ROOT / "app/api/_customerPublicationContract.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("validateCustomerPublication", projection)
        self.assertIn("parseCustomerPublication", projection)
        self.assertIn("field_visibility_policy_ref", contract)
        self.assertIn("customerPublication", projection)
        self.assertNotIn("filterVisibleItems", projection)

    def test_replay_projection_uses_attempt_bound_timing_and_task_evidence(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        projection = (ROOT / "app/api/_customerRunProjection.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("waje_runtime.durable_stage_attempt_bindings", store)
        self.assertIn("'transition_attempt_id', timing.transition_attempt_id", store)
        self.assertIn("execution_transition_attempt_id", store)
        self.assertIn("entry.plan_revision_id", store)
        self.assertIn("'task_id', task.value ->> 'task_id'", store)
        self.assertIn("projectAcceptedGraph", projection)
        self.assertIn("evidenceForExecutionTransition", projection)

    def test_agent_run_candidates_share_one_repeatable_read_snapshot(self):
        agent_runs = (ROOT / "app" / "api" / "agent-runs" / "route.ts").read_text(
            encoding="utf-8"
        )
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("listPersistedAgentRunCandidates", agent_runs)
        self.assertNotIn("Promise.all", agent_runs)
        self.assertIn(
            '"BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"',
            store,
        )
        publication_read = store.index(
            "listPersistedPublicationRuns(limit, client)"
        )
        runtime_read = store.index("listPersistedRuntimeRuns(limit, client)")
        commit = store.index('client.query("COMMIT")', runtime_read)
        self.assertLess(publication_read, runtime_read)
        self.assertLess(runtime_read, commit)
        self.assertIn('client.query("ROLLBACK")', store)

    def test_publication_candidate_requires_closed_delivery_matrix(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )

        for value in (
            "COALESCE(delivery.status, 'pending') = 'pending'",
            "r.status = 'narrative_ready'",
            "r.request ->> 'post_execution_status' = 'narrative_ready'",
            "delivery.status = 'published'",
            "r.request ->> 'post_execution_status' = 'completed'",
            "delivery.status = 'retryable_failed'",
            "'delivery_retryable_failed'",
            "delivery.status = 'permanently_failed'",
            "'delivery_permanently_failed'",
        ):
            self.assertIn(value, store)

    def test_accepted_tasks_bind_typed_execution_outcomes_to_exact_snapshot(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        projection = (ROOT / "app/api/_customerRunProjection.ts").read_text(
            encoding="utf-8"
        )
        contracts = (ROOT / "app/agent-run-workbench/contracts.ts").read_text(
            encoding="utf-8"
        )

        trace_start = store.index("'execution_state', CASE")
        trace_end = store.index(") plan_trace ON true", trace_start)
        trace_sql = store[trace_start:trace_end]
        for value in (
            "waje_runtime.capability_outcomes",
            "waje_runtime.capability_task_attempts",
            "waje_runtime.durable_stage_attempt_bindings",
            "waje_runtime.workflow_transition_attempts",
            "waje_runtime.capability_execution_snapshots",
            "transition.output_payload -> 'execution_snapshot'",
            "= snapshot.payload",
            "snapshot_payload -> 'outcome_refs'",
            "binding.stage_name = 'execute_capability_dag'",
            "transition.acceptance_state = 'accepted'",
            "WHEN r.status = 'planned'",
            "THEN 'not_started'",
            "ELSE 'unsettled'",
        ):
            self.assertIn(value, trace_sql)
        self.assertNotIn("technical_detail_ref", trace_sql)
        self.assertIn('state: "settled"', contracts)
        self.assertIn('state: "not_started"', contracts)
        self.assertIn('state: "unsettled"', contracts)
        self.assertIn("projectAcceptedTaskExecution", projection)
        self.assertIn("businessBoundary", projection)


if __name__ == "__main__":
    unittest.main()
