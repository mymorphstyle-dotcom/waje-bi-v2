import json
from pathlib import Path
import unittest

from bi_agent.conversation.models import ContextManifest, MemoryProposal
from bi_agent.conversation.postgres_store import CONVERSATION_SCHEMA_SQL, PostgresConversationStore
from bi_agent.conversation.runtime import evaluate_reuse_candidate
from bi_agent.conversation.store import InMemoryConversationStore


ROOT = Path(__file__).resolve().parents[2]


class ConversationPersistenceTest(unittest.TestCase):
    def test_context_manifest_records_claim_usable_sources(self):
        store = InMemoryConversationStore()
        manifest = ContextManifest(
            manifest_id="manifest-1",
            thread_id="t1",
            turn_id="turn-1",
            topic_id="topic-1",
            sources=[{"type": "answer_package", "ref": "artifact-1", "can_support_claim": True}],
            claim_use_policy={"requires_evidence_ref": True},
            snapshot_version="2026-H1",
            permission_context={"role": "analyst"},
            created_at="2026-07-08T00:00:00Z",
        )

        store.save_context_manifest(manifest)

        loaded = store.list_context_manifests("t1")[0]
        self.assertIs(loaded.sources[0]["can_support_claim"], True)
        self.assertIs(loaded.claim_use_policy["requires_evidence_ref"], True)

    def test_context_manifest_partial_claim_policy_keeps_defaults(self):
        manifest = ContextManifest(
            manifest_id="manifest-partial-policy",
            thread_id="t1",
            turn_id="turn-1",
            sources=[{"type": "answer_package", "ref": "artifact-1", "can_support_claim": True}],
            claim_use_policy={"requires_evidence_ref": False},
        )

        self.assertIs(manifest.claim_use_policy["requires_evidence_ref"], False)
        self.assertIs(manifest.claim_use_policy["can_support_bi_claim"], True)

    def test_reuse_decision_blocks_stale_snapshot_claim_support(self):
        decision = evaluate_reuse_candidate(
            source_snapshot="2026-H1",
            current_snapshot="2026-H2",
            permission_match=True,
            semantic_scope_match=True,
        )

        self.assertEqual(decision.decision, "context_only")
        self.assertIs(decision.can_support_claim, False)

    def test_reuse_decision_uses_stable_reason_codes(self):
        stale = evaluate_reuse_candidate(
            source_snapshot="2026-H1",
            current_snapshot="2026-H2",
            permission_match=True,
            semantic_scope_match=True,
        )
        blocked = evaluate_reuse_candidate(
            source_snapshot="2026-H1",
            current_snapshot="2026-H1",
            permission_match=False,
            semantic_scope_match=True,
        )
        scoped = evaluate_reuse_candidate(
            source_snapshot="2026-H1",
            current_snapshot="2026-H1",
            permission_match=True,
            semantic_scope_match=False,
        )
        reusable = evaluate_reuse_candidate(
            source_snapshot="2026-H1",
            current_snapshot="2026-H1",
            permission_match=True,
            semantic_scope_match=True,
        )

        self.assertEqual(stale.reason, "snapshot_mismatch")
        self.assertEqual(blocked.reason, "permission_scope_mismatch")
        self.assertEqual(scoped.reason, "semantic_scope_mismatch")
        self.assertEqual(reusable.reason, "validated_same_thread_scope")

    def test_schema_declares_required_runtime_tables(self):
        required_tables = {
            "investigation_threads",
            "conversation_topics",
            "conversation_turns",
            "conversation_messages",
            "analysis_runs",
            "run_nodes",
            "context_manifests",
            "result_refs",
            "evidence_refs",
            "answer_packages",
            "analysis_assets",
            "investigation_artifacts",
            "memory_items",
            "memory_proposals",
            "audit_events",
        }

        for table in required_tables:
            with self.subTest(table=table):
                self.assertIn(f"waje_runtime.{table}", CONVERSATION_SCHEMA_SQL)
        self.assertIn("refresh_rule", CONVERSATION_SCHEMA_SQL)
        self.assertIn("revocation_path", CONVERSATION_SCHEMA_SQL)

    def test_schema_and_store_persist_dataset_snapshots(self):
        self.assertIn("waje_runtime.dataset_snapshots", CONVERSATION_SCHEMA_SQL)
        connection = FakeConnection()
        store = PostgresConversationStore(connection)
        store.save_dataset_snapshot(
            {
                "snapshot_ref": "snapshot:paid_order:1",
                "dataset_id": "paid_order_success",
                "physical_table": "paid_order_success_clean_20240101_20260704",
                "watermark": "2026-07-04",
                "schema_fingerprint": "schema-1",
                "schema_fields": ["business_date_lagos", "paid_amount_ngn"],
                "contract_ref": "contracts/sources/paid-order-detail.source.yaml@0.2",
                "permission_scopes": ["analyst"],
                "loaded_at": "2026-07-05T00:00:00+00:00",
                "status": "active",
            }
        )
        sql = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("waje_runtime.dataset_snapshots", sql)
        self.assertIn("waje_runtime.audit_events", sql)
        self.assertEqual(connection.commits, 1)

    def test_in_memory_store_lists_dataset_snapshots_by_dataset(self):
        store = InMemoryConversationStore()
        first = _dataset_snapshot_payload("snapshot:paid_order:1", "paid_order_success")
        second = _dataset_snapshot_payload("snapshot:dashboard:1", "market_dashboard")

        store.save_dataset_snapshot(first)
        store.save_dataset_snapshot(second)

        self.assertEqual(store.list_dataset_snapshots("paid_order_success"), (first,))
        self.assertEqual(store.list_dataset_snapshots(), (first, second))

    def test_postgres_store_lists_dataset_snapshot_payloads_by_dataset(self):
        payload = _dataset_snapshot_payload("snapshot:paid_order:1", "paid_order_success")
        connection = FakeConnection(rows=[(payload,)])
        store = PostgresConversationStore(connection)

        snapshots = store.list_dataset_snapshots("paid_order_success")

        self.assertEqual(snapshots, (payload,))
        statement, params = connection.statements[-1]
        self.assertIn("waje_runtime.dataset_snapshots", statement)
        self.assertEqual(params["dataset_id"], "paid_order_success")

    def test_postgres_store_lists_json_encoded_dataset_snapshot_payload(self):
        payload = _dataset_snapshot_payload(
            "snapshot:paid_order:json", "paid_order_success"
        )
        connection = FakeConnection(
            rows=[(json.dumps(payload, ensure_ascii=False, sort_keys=True),)]
        )
        store = PostgresConversationStore(connection)

        self.assertEqual(
            store.list_dataset_snapshots("paid_order_success"),
            (payload,),
        )

    def test_postgres_snapshot_save_rolls_back_when_audit_insert_fails(self):
        connection = FakeConnection(fail_execute_at=2)
        store = PostgresConversationStore(connection)

        with self.assertRaisesRegex(RuntimeError, "execute failed"):
            store.save_dataset_snapshot(
                _dataset_snapshot_payload("snapshot:paid_order:1", "paid_order_success")
            )

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_postgres_snapshot_save_rolls_back_when_commit_fails(self):
        connection = FakeConnection(fail_commit=True)
        store = PostgresConversationStore(connection)

        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            store.save_dataset_snapshot(
                _dataset_snapshot_payload("snapshot:paid_order:1", "paid_order_success")
            )

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_postgres_snapshot_upsert_replaces_all_mirrored_columns(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)
        original = _dataset_snapshot_payload("snapshot:paid_order:1", "legacy_paid_order")
        store.save_dataset_snapshot(original)
        payload = _dataset_snapshot_payload("snapshot:paid_order:1", "paid_order_success")
        payload.update(
            {
                "physical_table": "paid_order_success_clean_20260705",
                "watermark": "2026-07-05",
                "schema_fingerprint": "schema-2",
                "contract_ref": "contracts/sources/paid-order-detail.source.yaml@0.3",
            }
        )

        store.save_dataset_snapshot(payload)

        statement, params = connection.statements[2]
        for column in (
            "dataset_id",
            "physical_table",
            "watermark",
            "schema_fingerprint",
            "schema_fields",
            "contract_ref",
            "permission_scopes",
            "loaded_at",
            "status",
            "payload",
        ):
            with self.subTest(column=column):
                self.assertIn(f"{column} = EXCLUDED.{column}", statement)
        persisted_payload = json.loads(params["payload"])
        for column in (
            "dataset_id",
            "physical_table",
            "watermark",
            "schema_fingerprint",
            "contract_ref",
            "loaded_at",
            "status",
        ):
            with self.subTest(payload_column=column):
                self.assertEqual(params[column], persisted_payload[column])
        self.assertEqual(json.loads(params["schema_fields"]), persisted_payload["schema_fields"])
        self.assertEqual(
            json.loads(params["permission_scopes"]),
            persisted_payload["permission_scopes"],
        )
        self.assertEqual(connection.commits, 2)

    def test_in_memory_store_replaces_the_full_snapshot_for_a_reused_ref(self):
        store = InMemoryConversationStore()
        original = _dataset_snapshot_payload("snapshot:paid_order:1", "legacy_paid_order")
        replacement = _dataset_snapshot_payload("snapshot:paid_order:1", "paid_order_success")
        replacement["schema_fingerprint"] = "replacement-schema"

        store.save_dataset_snapshot(original)
        store.save_dataset_snapshot(replacement)

        self.assertEqual(store.list_dataset_snapshots(), (replacement,))
        self.assertEqual(store.list_dataset_snapshots("legacy_paid_order"), ())

    def test_in_memory_snapshot_payloads_are_isolated_at_every_boundary(self):
        store = InMemoryConversationStore()
        payload = _dataset_snapshot_payload("snapshot:paid_order:1", "paid_order_success")

        store.save_dataset_snapshot(payload)
        payload["schema_fields"].append("input_mutation")
        payload["permission_scopes"].append("admin")

        first_read = store.list_dataset_snapshots()[0]
        self.assertEqual(first_read["schema_fields"], ["business_date_lagos", "paid_amount_ngn"])
        self.assertEqual(first_read["permission_scopes"], ["analyst"])

        first_read["schema_fields"].append("read_mutation")
        first_read["permission_scopes"].append("owner")
        audit_read = store.audit_events[0]
        audit_read["payload"]["schema_fields"].append("audit_read_mutation")

        second_read = store.list_dataset_snapshots()[0]
        second_audit_read = store.audit_events[0]
        self.assertEqual(second_read["schema_fields"], ["business_date_lagos", "paid_amount_ngn"])
        self.assertEqual(second_read["permission_scopes"], ["analyst"])
        self.assertEqual(
            second_audit_read["payload"]["schema_fields"],
            ["business_date_lagos", "paid_amount_ngn"],
        )

    def test_store_writes_audit_events_for_state_changes(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)

        thread = store.create_thread("thread-pg", owner_id="analyst-1")
        topic = store.create_topic(thread.thread_id, title="Q2 vs Q1", summary="Q2 变化")
        store.set_current_topic(thread.thread_id, topic.topic_id)
        store.add_turn(thread.thread_id, {"turn_id": "turn-1", "intent": "new_topic"})
        store.add_result_ref(
            topic.topic_id,
            result_ref="result-1",
            snapshot_id="2026H1",
            contract_version="contracts-v1",
            permission_scope="analyst",
            semantic_scope="q2_vs_q1_paid_amount",
        )
        store.add_memory_proposal(
            MemoryProposal(
                proposal_id="proposal-1",
                thread_id=thread.thread_id,
                text="默认单独看 WajeSpecial",
                source_ref="turn-1",
                owner_scope="org-default",
                visibility="analyst",
            )
        )

        executed_sql = "\n".join(statement for statement, _params in connection.statements)
        self.assertIn("waje_runtime.investigation_threads", executed_sql)
        self.assertIn("waje_runtime.conversation_topics", executed_sql)
        self.assertIn("waje_runtime.conversation_turns", executed_sql)
        self.assertIn("waje_runtime.result_refs", executed_sql)
        self.assertIn("waje_runtime.memory_proposals", executed_sql)
        self.assertGreaterEqual(executed_sql.count("waje_runtime.audit_events"), 5)
        self.assertGreaterEqual(connection.commits, 5)

    def test_postgres_store_records_analysis_assets(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)

        store.save_analysis_assets(
            "thread-pg",
            "topic-pg",
            [{"asset_type": "compiler_runtime_plan", "status": "usable", "payload": {"query_intents": ["dimension_scan_reuse"]}}],
        )

        executed_sql = "\n".join(statement for statement, _params in connection.statements)
        self.assertIn("waje_runtime.analysis_assets", executed_sql)
        self.assertGreaterEqual(executed_sql.count("waje_runtime.audit_events"), 1)
        self.assertEqual(connection.commits, 1)


class FakeConnection:
    def __init__(self, rows=None, *, fail_execute_at=None, fail_commit=False):
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.rows = rows or []
        self.fail_execute_at = fail_execute_at
        self.fail_commit = fail_commit

    def execute(self, statement, params=None):
        self.statements.append((statement, params or {}))
        if len(self.statements) == self.fail_execute_at:
            raise RuntimeError("execute failed")
        return FakeCursor(self.rows)

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def _dataset_snapshot_payload(snapshot_ref, dataset_id):
    return {
        "snapshot_ref": snapshot_ref,
        "dataset_id": dataset_id,
        "physical_table": f"{dataset_id}_clean_20260704",
        "watermark": "2026-07-04",
        "schema_fingerprint": "schema-1",
        "schema_fields": ["business_date_lagos", "paid_amount_ngn"],
        "contract_ref": f"contracts/sources/{dataset_id}.source.yaml@0.2",
        "permission_scopes": ["analyst"],
        "loaded_at": "2026-07-05T00:00:00+00:00",
        "status": "active",
    }


if __name__ == "__main__":
    unittest.main()
