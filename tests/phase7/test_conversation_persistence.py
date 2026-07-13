import json
import hashlib
from copy import deepcopy
from pathlib import Path
import unittest

from bi_agent.conversation.models import ContextManifest, MemoryProposal
from bi_agent.conversation.postgres_store import CONVERSATION_SCHEMA_SQL, PostgresConversationStore
from bi_agent.conversation.runtime import evaluate_reuse_candidate
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.dataset_catalog import build_dataset_release_authority_record
from bi_agent.runtime.analysis_contracts import analysis_contract_signature
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_digest


ROOT = Path(__file__).resolve().parents[2]


class ConversationPersistenceTest(unittest.TestCase):
    def test_in_memory_result_candidate_payload_is_immutable_and_newest_first(self):
        store = InMemoryConversationStore()
        older = _result_candidate_payload("result:older", source_run_id="run-older")
        newer = _result_candidate_payload("result:newer", source_run_id="run-newer")

        _add_result_candidate(store, older)
        _add_result_candidate(store, older)
        _add_result_candidate(store, newer)

        candidates = store.results_for_topic("topic-candidate")
        self.assertEqual(
            tuple(candidate.result_ref for candidate in candidates),
            ("result:newer", "result:older"),
        )
        self.assertEqual(candidates[0].payload, newer)
        self.assertEqual(len(candidates), 2)

        candidates[0].payload["source_snapshot_refs"].append("snapshot:mutated")
        self.assertEqual(
            store.results_for_topic("topic-candidate")[0].payload,
            newer,
        )

        changed = _result_candidate_payload(
            "result:newer",
            source_run_id="run-collision",
        )
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "result_ref_payload_conflict",
        ):
            _add_result_candidate(store, changed)

    def test_result_candidate_payload_rejects_unknown_or_forged_shape(self):
        store = InMemoryConversationStore()
        payload = _result_candidate_payload("result:shape")

        unknown = {**payload, "unexpected": "value"}
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "result_candidate_payload_shape_invalid",
        ):
            _add_result_candidate(store, unknown)

        forged = {**payload, "source_run_id": "run-forged"}
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "result_candidate_signature_invalid",
        ):
            _add_result_candidate(store, forged)

    def test_postgres_result_candidates_round_trip_exact_payload(self):
        payload = _result_candidate_payload("result:pg")
        row = {
            "topic_id": "topic-candidate",
            "result_ref": "result:pg",
            "snapshot_id": "2026H1",
            "contract_version": "contracts-v1",
            "permission_scope": "analyst",
            "semantic_scope": payload["semantic_scope_signature"],
            "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        }
        store = PostgresConversationStore(FakeConnection(rows=[row]))

        candidates = store.results_for_topic("topic-candidate")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].payload, payload)

    def test_postgres_result_candidate_write_is_exact_replay_only(self):
        payload = _result_candidate_payload("result:pg-write")
        connection = FakeConnection()
        store = PostgresConversationStore(connection)

        _add_result_candidate(store, payload)

        statement, params = next(
            (statement, params)
            for statement, params in connection.statements
            if "result_ref_immutable_write" in statement
        )
        self.assertIn("current.payload = EXCLUDED.payload", statement)
        self.assertEqual(json.loads(params["payload"]), payload)

        collision = PostgresConversationStore(
            FakeConnection(result_ref_collision=True)
        )
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "result_ref_payload_conflict",
        ):
            _add_result_candidate(collision, payload)

    def test_result_candidate_authority_resolves_source_run_and_contract(self):
        payload = _result_candidate_payload("result:resolve")
        contract = {
            "analysis_contract_id": payload["analysis_contract_ref"],
            "metric": "paid_amount",
        }
        contract["contract_signature"] = analysis_contract_signature(contract)
        payload["analysis_contract_signature"] = contract["contract_signature"]
        payload["semantic_scope_signature"] = (
            "analysis-contract:sha256:" + contract["contract_signature"]
        )
        payload.pop("candidate_signature")
        payload["candidate_signature"] = canonical_digest(payload)
        store = InMemoryConversationStore()
        store.upsert_run(
            payload["source_run_id"],
            thread_id="thread-candidate",
            topic_id="topic-candidate",
            status="completed",
            request={
                "context_manifest": {
                    "snapshot_version": "2026H1",
                    "contract_versions": {"runtime": "contracts-v1"},
                }
            },
        )
        store.analysis_runtime_authority["analysis_contract"][
            contract["analysis_contract_id"]
        ] = contract
        store.analysis_runtime_records[payload["source_run_id"]] = {
            "digest": "test-owned-publication",
            "payload": {"analysis_contract": contract},
        }
        _add_result_candidate(store, payload)

        authority = store.resolve_result_candidate_authority(
            result_ref=payload["result_ref"],
            topic_id="topic-candidate",
        )

        self.assertEqual(authority["source_run_id"], payload["source_run_id"])
        self.assertEqual(authority["run_topic_id"], "topic-candidate")
        self.assertEqual(authority["analysis_contract"], contract)
        self.assertEqual(
            authority["stored_analysis_contract_signature"],
            payload["analysis_contract_signature"],
        )

    def test_run_request_owner_comes_from_authoritative_run_columns(self):
        spoofed = {"thread_id": "thread-spoofed", "topic_id": "topic-spoofed"}
        memory = InMemoryConversationStore()
        memory.upsert_run(
            "run-owner",
            thread_id="thread-owner",
            topic_id="topic-owner",
            status="needs_question",
            request=spoofed,
        )
        self.assertEqual(memory.get_run_request("run-owner")["thread_id"], "thread-owner")
        self.assertEqual(memory.get_run_request("run-owner")["topic_id"], "topic-owner")

        postgres = PostgresConversationStore(
            FakeConnection(rows=[(spoofed, "thread-owner", "topic-owner")])
        )
        request = postgres.get_run_request("run-owner")
        self.assertEqual(request["thread_id"], "thread-owner")
        self.assertEqual(request["topic_id"], "topic-owner")

    def test_in_memory_single_save_checks_release_membership_before_dataset_policy(self):
        store = InMemoryConversationStore()
        payloads = (
            _release_snapshot_payload("snapshot:published-overall", "market_dashboard"),
            _release_snapshot_payload(
                "snapshot:published-channel", "market_dashboard_channel"
            ),
        )
        release_ref = _release_ref(payloads)
        for payload in payloads:
            payload["release_ref"] = release_ref
        store.publish_dataset_snapshot_release(
            release_ref=release_ref,
            logical_snapshot_id="dashboard-logical",
            payloads=payloads,
        )

        for dataset_id in ("market_dashboard", "paid_order_success"):
            with self.subTest(dataset_id=dataset_id):
                changed = {**payloads[0], "dataset_id": dataset_id}
                with self.assertRaisesRegex(
                    ValueError,
                    "dataset_snapshot_published_immutable",
                ):
                    store.save_dataset_snapshot(changed)

    def test_postgres_single_save_rejects_published_ref_with_spoofed_dataset(self):
        connection = FakeConnection(rows=[{"published": 1}])
        payload = _dataset_snapshot_payload(
            "snapshot:published-dashboard",
            "paid_order_success",
        )

        with self.assertRaisesRegex(ValueError, "dataset_snapshot_published_immutable"):
            PostgresConversationStore(connection).save_dataset_snapshot(payload)

        sql = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("dataset_snapshot_releases", sql)
        self.assertLess(sql.index("pg_advisory_xact_lock"), sql.index("dataset_snapshot_releases"))
        self.assertNotIn("INSERT INTO waje_runtime.dataset_snapshots", sql)

    def test_postgres_batch_locks_sorted_members_before_logical_release(self):
        connection = FakeConnection()
        payloads = (
            _release_snapshot_payload("snapshot:z-overall", "market_dashboard"),
            _release_snapshot_payload("snapshot:a-channel", "market_dashboard_channel"),
        )
        release_ref = _release_ref(payloads)
        for payload in payloads:
            payload["release_ref"] = release_ref

        PostgresConversationStore(connection).publish_dataset_snapshot_release(
            release_ref=release_ref,
            logical_snapshot_id="dashboard-logical",
            payloads=payloads,
        )

        lock_params = [
            params["lock_key"]
            for statement, params in connection.statements
            if "pg_advisory_xact_lock" in statement
        ]
        self.assertEqual(
            lock_params,
            [
                "dataset_snapshot_member:snapshot:a-channel",
                "dataset_snapshot_member:snapshot:z-overall",
                "dataset_snapshot_release:dashboard-logical",
            ],
        )

    def test_release_required_snapshot_cannot_be_published_by_single_save(self):
        connection = FakeConnection()
        payload = _dataset_snapshot_payload("snapshot:dashboard", "market_dashboard")

        with self.assertRaisesRegex(ValueError, "dataset_snapshot_release_required"):
            PostgresConversationStore(connection).save_dataset_snapshot(payload)

        sql = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("dataset_snapshot_releases", sql)
        self.assertNotIn("INSERT INTO waje_runtime.dataset_snapshots", sql)

    def test_postgres_resolver_builds_authority_only_from_exact_join_membership(self):
        payloads = (
            _release_snapshot_payload("snapshot:overall:v2", "market_dashboard"),
            _release_snapshot_payload("snapshot:channel:v2", "market_dashboard_channel"),
        )
        release_ref = _release_ref(payloads)
        for payload in payloads:
            payload["release_ref"] = release_ref
        authority = build_dataset_release_authority_record(payloads)
        member_payloads = sorted(
            (
                {**payload, "authority_record_ref": authority.authority_record_ref}
                for payload in payloads
            ),
            key=lambda item: item["snapshot_ref"],
        )
        connection = FakeConnection(rows=[{
            "release_payload": json.dumps(authority.to_dict()),
            "logical_snapshot_id": authority.logical_snapshot_id,
            "load_revision": authority.load_revision,
            "snapshot_refs": json.dumps(list(authority.snapshot_refs)),
            "member_count": 2,
            "member_payloads": json.dumps(member_payloads),
            "member_columns": json.dumps(member_payloads),
        }])

        resolved = PostgresConversationStore(connection).resolve_dataset_release(
            release_ref
        )

        self.assertEqual(resolved, authority)
        sql = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("dataset_snapshot_releases", sql)
        self.assertIn("jsonb_agg(s.payload ORDER BY s.snapshot_ref)", sql)
        self.assertIn("count(s.snapshot_ref)", sql)

    def test_postgres_resolver_rejects_release_column_drift(self):
        payloads = (
            _release_snapshot_payload("snapshot:overall:v2", "market_dashboard"),
            _release_snapshot_payload("snapshot:channel:v2", "market_dashboard_channel"),
        )
        release_ref = _release_ref(payloads)
        for payload in payloads:
            payload["release_ref"] = release_ref
        authority = build_dataset_release_authority_record(payloads)
        member_payloads = sorted(
            (
                {**payload, "authority_record_ref": authority.authority_record_ref}
                for payload in payloads
            ),
            key=lambda item: item["snapshot_ref"],
        )
        connection = FakeConnection(rows=[{
            "release_payload": json.dumps(authority.to_dict()),
            "logical_snapshot_id": authority.logical_snapshot_id,
            "load_revision": "dashboard-load:sha256:drifted",
            "snapshot_refs": json.dumps(list(reversed(authority.snapshot_refs))),
            "member_count": 2,
            "member_payloads": json.dumps(member_payloads),
            "member_columns": json.dumps(member_payloads),
        }])

        with self.assertRaisesRegex(
            ValueError,
            "dataset_release_authority_(membership|record_mismatch)",
        ):
            PostgresConversationStore(connection).resolve_dataset_release(release_ref)

    def test_postgres_batch_requires_exact_two_sided_release(self):
        connection = FakeConnection()
        payload = _release_snapshot_payload(
            "snapshot:overall:v2", "market_dashboard"
        )

        with self.assertRaisesRegex(ValueError, "dataset_snapshot_release_dataset_set"):
            PostgresConversationStore(connection).publish_dataset_snapshot_release(
                release_ref=_release_ref((payload,)),
                logical_snapshot_id="dashboard-logical",
                payloads=(payload,),
            )

        self.assertEqual(connection.statements, [])

    def test_postgres_release_membership_validation_failure_rolls_back(self):
        payloads = (
            _release_snapshot_payload("snapshot:overall:v2", "market_dashboard"),
            _release_snapshot_payload(
                "snapshot:channel:v2", "market_dashboard_channel"
            ),
        )
        release_ref = _release_ref(payloads)
        for payload in payloads:
            payload["release_ref"] = release_ref
        connection = FakeConnection(rows=[{"validated_count": 0}])

        with self.assertRaisesRegex(RuntimeError, "dataset_snapshot_release_validation_failed"):
            PostgresConversationStore(connection).publish_dataset_snapshot_release(
                release_ref=release_ref,
                logical_snapshot_id="dashboard-logical",
                payloads=payloads,
            )

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

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

    def test_context_manifest_all_fields_round_trip_in_memory_and_postgres(self):
        manifest = ContextManifest(
            manifest_id="manifest-roundtrip-all",
            thread_id="thread-roundtrip",
            turn_id="turn-roundtrip",
            topic_id="topic-roundtrip",
            sources=[{"type": "policy", "ref": "source-1", "can_support_claim": False}],
            claim_use_policy={"requires_evidence_ref": True},
            snapshot_version="snapshot-v2",
            permission_context={"role": "analyst"},
            analysis_assets=[{"asset_id": "asset-1"}],
            accepted_assumptions=[{"action_kind": "omit_unavailable_context"}],
            contract_versions={"runtime": "v2", "semantic": "v3"},
            schema_fingerprint="schema:sha256:abc",
            created_at="2026-07-12T00:00:00+00:00",
        )
        expected = manifest.to_dict()

        memory = InMemoryConversationStore()
        memory.save_context_manifest(manifest)
        self.assertEqual(
            memory.list_context_manifests("thread-roundtrip")[0].to_dict(),
            expected,
        )

        row = {
            "manifest_id": manifest.manifest_id,
            "thread_id": manifest.thread_id,
            "turn_id": manifest.turn_id,
            "can_support_claims": manifest.can_support_claims,
            "items": expected,
        }
        postgres = PostgresConversationStore(FakeConnection(rows=[row]))
        self.assertEqual(
            postgres.list_context_manifests("thread-roundtrip")[0].to_dict(),
            expected,
        )

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
        self.assertEqual(reusable.decision, "candidate")
        self.assertIs(reusable.can_support_claim, False)
        self.assertEqual(reusable.reason, "candidate_same_thread_scope")

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
        self.assertIn(
            "idx_dataset_snapshot_releases_identity",
            CONVERSATION_SCHEMA_SQL,
        )

    def test_schema_and_store_persist_dataset_snapshots(self):
        self.assertIn("waje_runtime.dataset_snapshots", CONVERSATION_SCHEMA_SQL)
        connection = FakeConnection()
        store = PostgresConversationStore(connection)
        store.save_dataset_snapshot(
            {
                "snapshot_ref": "snapshot:paid_order:1",
                "dataset_id": "legacy_paid_order",
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
        first = _dataset_snapshot_payload("snapshot:paid_order:1", "legacy_paid_order")
        second = _dataset_snapshot_payload("snapshot:attempt:1", "payment_attempt")

        store.save_dataset_snapshot(first)
        store.save_dataset_snapshot(second)

        self.assertEqual(store.list_dataset_snapshots("legacy_paid_order"), (first,))
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
                _dataset_snapshot_payload("snapshot:paid_order:1", "legacy_paid_order")
            )

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_postgres_snapshot_save_rolls_back_when_commit_fails(self):
        connection = FakeConnection(fail_commit=True)
        store = PostgresConversationStore(connection)

        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            store.save_dataset_snapshot(
                _dataset_snapshot_payload("snapshot:paid_order:1", "legacy_paid_order")
            )

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_postgres_snapshot_upsert_replaces_all_mirrored_columns(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)
        original = _dataset_snapshot_payload("snapshot:paid_order:1", "legacy_paid_order")
        store.save_dataset_snapshot(original)
        payload = _dataset_snapshot_payload("snapshot:paid_order:1", "legacy_paid_order_v2")
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
        replacement = _dataset_snapshot_payload("snapshot:paid_order:1", "legacy_paid_order_v2")
        replacement["schema_fingerprint"] = "replacement-schema"

        store.save_dataset_snapshot(original)
        store.save_dataset_snapshot(replacement)

        self.assertEqual(store.list_dataset_snapshots(), (replacement,))
        self.assertEqual(store.list_dataset_snapshots("legacy_paid_order"), ())

    def test_postgres_batch_publishes_snapshot_release_in_one_transaction(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)
        overall = _release_snapshot_payload("snapshot:dashboard:overall:v2", "market_dashboard")
        channel = _release_snapshot_payload(
            "snapshot:dashboard:channel:v2", "market_dashboard_channel"
        )
        release_ref = _release_ref((overall, channel))
        for payload in (overall, channel):
            payload["release_ref"] = release_ref

        store.publish_dataset_snapshot_release(
            release_ref=release_ref,
            logical_snapshot_id="dashboard-logical",
            payloads=(overall, channel),
        )

        sql = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("dataset_snapshot_releases", sql)
        self.assertIn("ON CONFLICT (snapshot_ref) DO UPDATE", sql)
        self.assertIn("payload - 'authority_record_ref'", sql)
        self.assertIn("validated_count", sql)
        release_params = next(
            params
            for statement, params in connection.statements
            if "INSERT INTO waje_runtime.dataset_snapshot_releases" in statement
        )
        self.assertEqual(
            json.loads(release_params["snapshot_refs"]),
            sorted((overall["snapshot_ref"], channel["snapshot_ref"])),
        )
        self.assertTrue(
            any(
                params.get("event_type") == "dataset_snapshot_release_published"
                for _, params in connection.statements
            )
        )
        self.assertIn("evidence_state", sql)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)

    def test_postgres_release_session_lock_wraps_clickhouse_publish_window(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)

        with store.dataset_snapshot_release_lock("dashboard-logical"):
            sql_inside = "\n".join(statement for statement, _ in connection.statements)
            self.assertIn("pg_advisory_lock", sql_inside)
            self.assertNotIn("pg_advisory_unlock", sql_inside)

        sql = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("pg_advisory_unlock", sql)
        self.assertEqual(connection.commits, 1)

    def test_postgres_batch_release_failure_rolls_back_and_never_commits(self):
        connection = FakeConnection(fail_execute_at=3)
        store = PostgresConversationStore(connection)
        payloads = (
            _release_snapshot_payload("snapshot:overall:v2", "market_dashboard"),
            _release_snapshot_payload("snapshot:channel:v2", "market_dashboard_channel"),
        )
        release_ref = _release_ref(payloads)
        for payload in payloads:
            payload["release_ref"] = release_ref

        with self.assertRaisesRegex(RuntimeError, "execute failed"):
            store.publish_dataset_snapshot_release(
                release_ref=release_ref,
                logical_snapshot_id="dashboard-logical",
                payloads=payloads,
            )

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_in_memory_batch_release_is_atomic_on_injected_failure(self):
        store = InMemoryConversationStore()
        old_payloads = (
            _release_snapshot_payload(
                "snapshot:old:overall", "market_dashboard", revision="load:old"
            ),
            _release_snapshot_payload(
                "snapshot:old:channel", "market_dashboard_channel", revision="load:old"
            ),
        )
        old_release_ref = _release_ref(old_payloads)
        for payload in old_payloads:
            payload["release_ref"] = old_release_ref
        store.publish_dataset_snapshot_release(
            release_ref=old_release_ref,
            logical_snapshot_id="dashboard-logical",
            payloads=old_payloads,
        )
        new_payloads = (
            _release_snapshot_payload(
                "snapshot:new:overall", "market_dashboard", revision="load:new"
            ),
            _release_snapshot_payload(
                "snapshot:new:channel", "market_dashboard_channel", revision="load:new"
            ),
        )
        new_release_ref = _release_ref(new_payloads)
        for payload in new_payloads:
            payload["release_ref"] = new_release_ref

        with self.assertRaisesRegex(RuntimeError, "injected_release_failure"):
            store.publish_dataset_snapshot_release(
                release_ref=new_release_ref,
                logical_snapshot_id="dashboard-logical",
                payloads=new_payloads,
                fail_after_writes=1,
            )

        self.assertEqual(
            {item["snapshot_ref"] for item in store.list_dataset_snapshots()},
            {item["snapshot_ref"] for item in old_payloads},
        )

    def test_in_memory_snapshot_payloads_are_isolated_at_every_boundary(self):
        store = InMemoryConversationStore()
        payload = _dataset_snapshot_payload("snapshot:paid_order:1", "legacy_paid_order")

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

    def test_postgres_store_preserves_failed_llm_audit_contract(self):
        audit = {
            "task": "business_intent",
            "provider": "contract-test-provider",
            "model": "contract-test-model",
            "prompt_version": "contract-test-v1",
            "response_id": "response-3",
            "structured_output": {
                "question_family": "data_quality_or_evidence_review",
                "analysis_requirements": {
                    "context_sources": ["gameplay"]
                },
            },
            "raw_response_content": json.dumps(
                {"question_family": "data_quality_or_evidence_review"},
                ensure_ascii=False,
            ),
        }
        connection = FakeConnection()
        store = PostgresConversationStore(connection)

        store.add_audit_event(
            "workflow_failure_llm_call_recorded",
            thread_id="thread-failed-audit",
            topic_id="topic-failed-audit",
            run_id="run-failed-audit",
            ref=audit["response_id"],
            payload=audit,
        )

        statement, params = connection.statements[-1]
        self.assertIn("waje_runtime.audit_events", statement)
        self.assertEqual(params["event_type"], "workflow_failure_llm_call_recorded")
        self.assertEqual(params["ref"], "response-3")
        self.assertEqual(json.loads(params["payload"]), audit)


class FakeConnection:
    def __init__(
        self,
        rows=None,
        *,
        fail_execute_at=None,
        fail_commit=False,
        result_ref_collision=False,
    ):
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.rows = rows
        self.fail_execute_at = fail_execute_at
        self.fail_commit = fail_commit
        self.result_ref_collision = result_ref_collision
        self.runtime_publications = {}
        self.pending_runtime_publications = {}

    def execute(self, statement, params=None):
        self.statements.append((statement, params or {}))
        if len(self.statements) == self.fail_execute_at:
            raise RuntimeError("execute failed")
        rows = self.rows
        if "result_ref_immutable_write" in statement and rows is None:
            rows = (
                []
                if self.result_ref_collision
                else [{"result_ref": (params or {})["result_ref"]}]
            )
        if "runtime_publication_preflight" in statement and rows is None:
            run_id = (params or {})["run_id"]
            rows = [{
                "run_id": run_id,
                "thread_id": (params or {}).get("expected_thread_id", "thread-task9"),
                "topic_id": (params or {}).get("expected_topic_id", "topic-task9"),
                "bundle_digest": self.runtime_publications.get(run_id, ""),
            }]
        if "INSERT INTO waje_runtime.analysis_runtime_publications" in statement:
            self.pending_runtime_publications[(params or {})["run_id"]] = (
                params or {}
            )["bundle_digest"]
        if "runtime_publication_postcheck" in statement and self.rows is None:
            rows = [{
                "bundle_digest": (params or {})["expected_bundle_digest"],
                "query_contract_count": (params or {})["expected_query_contract_count"],
                "query_run_count": (params or {})["expected_query_run_count"],
                "query_authority_count": (params or {})["expected_query_authority_count"],
                "completeness_count": (params or {})["expected_completeness_count"],
                "binding_count": (params or {})["expected_binding_count"],
                "evidence_count": (params or {})["expected_evidence_count"],
                "verified_claim_count": (params or {})["expected_verified_claim_count"],
                "claim_link_count": (params or {})["expected_claim_link_count"],
            }]
        if rows is None and "validated_count" in statement:
            expected = json.loads((params or {})["expected_payloads"])
            rows = [{"validated_count": len(expected)}]
        return FakeCursor(rows or [])

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.runtime_publications.update(self.pending_runtime_publications)
        self.pending_runtime_publications.clear()
        self.commits += 1

    def rollback(self):
        self.pending_runtime_publications.clear()
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


def _result_candidate_payload(
    result_ref: str,
    *,
    source_run_id: str = "run-candidate",
) -> dict:
    payload = {
        "schema_version": "result-reuse-candidate.v1",
        "source_run_id": source_run_id,
        "result_ref": result_ref,
        "query_contract_ref": "query-contract:candidate",
        "query_contract_signature": "query-signature",
        "query_execution_record_ref": "query-execution-record:candidate",
        "query_execution_record_digest": "query-execution-digest",
        "analysis_contract_ref": f"analysis:{source_run_id}:1",
        "analysis_contract_signature": "analysis-signature",
        "runtime_snapshot_id": "2026H1",
        "runtime_contract_version": "contracts-v1",
        "source_snapshot_refs": ["snapshot:paid-success"],
        "source_snapshot_record_refs": ["snapshot-record:paid-success"],
        "source_snapshot_record_digests": ["snapshot-record-digest"],
        "source_release_refs": ["release:paid-success"],
        "source_release_authority_refs": ["release-authority:paid-success"],
        "source_schema_fingerprints": ["schema:paid-success"],
        "permission_scope": "analyst",
        "semantic_scope_signature": "analysis-contract:sha256:analysis-signature",
        "rows_ref": "rows:candidate",
        "rows_record_ref": "rows-record:candidate",
        "rows_record_digest": "rows-record-digest",
        "rows_content_hash": "rows-content-hash",
        "completeness_report_ref": "completeness:candidate",
        "completeness_record_refs": ["completeness-record:candidate"],
        "completeness_record_digests": ["completeness-record-digest"],
        "binding_record_refs": ["binding-record:candidate"],
        "binding_record_digests": ["binding-record-digest"],
    }
    payload["candidate_signature"] = canonical_digest(payload)
    return payload


def _add_result_candidate(store, payload: dict) -> None:
    store.add_result_ref(
        "topic-candidate",
        result_ref=payload["result_ref"],
        snapshot_id=payload["runtime_snapshot_id"],
        contract_version=payload["runtime_contract_version"],
        permission_scope=payload["permission_scope"],
        semantic_scope=payload["semantic_scope_signature"],
        payload=deepcopy(payload),
    )


def _release_snapshot_payload(snapshot_ref, dataset_id, *, revision="dashboard-load:sha256:v2"):
    payload = _dataset_snapshot_payload(snapshot_ref, dataset_id)
    payload.update(
        {
            "logical_snapshot_id": "dashboard-logical",
            "load_revision": revision,
            "evidence_state": (
                "claim_ready" if dataset_id == "market_dashboard" else "context_only"
            ),
            "reconciliation_status": "mismatch",
            "reconciliation_ref": "dashboard-reconciliation:sha256:v2",
            "requires_release": True,
            "rows_content_hash": "a" * 64,
        }
    )
    return payload


def _release_ref(payloads):
    logical_ids = {payload["logical_snapshot_id"] for payload in payloads}
    revisions = {payload["load_revision"] for payload in payloads}
    if len(logical_ids) != 1 or len(revisions) != 1:
        raise ValueError("test_release_payload_mismatch")
    canonical = json.dumps(
        {
            "logical_snapshot_id": next(iter(logical_ids)),
            "load_revision": next(iter(revisions)),
            "snapshot_refs": sorted(payload["snapshot_ref"] for payload in payloads),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "dataset-release:sha256:" + hashlib.sha256(canonical).hexdigest()


if __name__ == "__main__":
    unittest.main()
