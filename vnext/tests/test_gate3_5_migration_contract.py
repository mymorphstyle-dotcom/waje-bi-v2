from __future__ import annotations

import os
from pathlib import Path
import unittest

import psycopg


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "storage"
    / "migrations"
    / "006_gate3_5_evidence_answer_projection.sql"
)
DSN = os.environ.get("WAJE_VNEXT_GATE35_MIGRATION_TEST_DSN")


class Gate35MigrationStaticContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_fail_closed_replacement_lists_every_superseded_authority(self) -> None:
        for table_name in (
            "evidence_records",
            "evidence_validity_records",
            "obligation_satisfaction_records",
            "answer_versions",
            "settlement_precondition_reports",
            "reviewer_objections",
        ):
            self.assertIn(
                f"FROM waje_vnext.{table_name}",
                self.sql,
            )
        self.assertIn("SQLSTATE `55000`", (
            ROOT / "storage" / "migrations" / "README.md"
        ).read_text(encoding="utf-8"))
        self.assertIn(
            "reset the disposable development database",
            self.sql,
        )

    def test_all_gate35_authority_tables_are_present(self) -> None:
        for table_name in (
            "capability_result_envelopes",
            "capability_result_receipts",
            "evidence_records",
            "evidence_admission_records",
            "evidence_validity_records",
            "evidence_use_bindings",
            "obligation_satisfaction_records",
            "provisional_answer_candidates",
            "claim_precheck_records",
            "answer_versions",
            "answer_claim_records",
            "settlement_precondition_reports",
            "workflow_projection_snapshots",
            "workflow_application_receipts",
            "workflow_projection_heads",
        ):
            self.assertIn(
                f"CREATE TABLE waje_vnext.{table_name}",
                self.sql,
            )

    def test_realm_and_production_admission_are_fail_closed(self) -> None:
        self.assertIn(
            "profile IN ('conformance', 'production')",
            self.sql,
        )
        self.assertIn(
            "provenance_kind IN ('conformance', 'physical_query')",
            self.sql,
        )
        self.assertIn(
            "profile <> 'production'\n        OR admission_status = 'rejected'",
            self.sql,
        )
        self.assertIn(
            "profile = 'conformance'\n"
            "            AND provenance_kind = 'conformance'",
            self.sql,
        )

    def test_ids_hashes_and_authority_continuity_are_constrained(self) -> None:
        for token in (
            "plan_adoption_g35_authority_key",
            "query_binding_g35_authority_key",
            "resolution_outcome_g35_authority_key",
            "evidence_obligation_g35_authority_key",
            "evidence_record_content_sha256",
            "capability_result_envelope_content_sha256",
            "authority_fence_content_sha256",
            "derived_input_sha256",
            "prior_evidence_validity_content_sha256",
            "prior_obligation_satisfaction_content_sha256",
        ):
            self.assertIn(token, self.sql)
        self.assertGreaterEqual(
            self.sql.count("~ '^[0-9a-f]{64}$'"),
            45,
        )

    def test_append_only_chains_and_canonical_uniqueness_are_constrained(self) -> None:
        for token in (
            "evidence_validity_one_root",
            "evidence_validity_one_successor",
            "obligation_satisfaction_one_successor",
            "UNIQUE (obligation_id, input_set_sha256)",
            "NOT (payload ? 'evidence_use_binding_ids')",
            "NOT (payload ? 'evidence_use_binding_content_sha256s')",
            "UNIQUE (answer_candidate_id, proposal_claim_key)",
            "'workflow_application_receipts'",
            "waje_vnext.reject_immutable_change()",
        ):
            self.assertIn(token, self.sql)

    def test_gate3_publication_and_delivery_states_are_hard_denied(self) -> None:
        self.assertIn(
            "status text NOT NULL CHECK (status = 'provisional')",
            self.sql,
        )
        self.assertIn(
            "publication_state IN (\n"
            "            'not_ready',\n"
            "            'provisional',\n"
            "            'blocked'",
            self.sql,
        )
        self.assertIn(
            "delivery_state IN ('not_delivered', 'superseded')",
            self.sql,
        )
        self.assertIn("<> 'settled'", self.sql)
        self.assertIn("<> 'delivered'", self.sql)
        self.assertIn("<> 'completed'", self.sql)

    def test_workflow_head_has_monotonic_cas_and_receipt_chain(self) -> None:
        for token in (
            "guard_workflow_projection_head_cas",
            "NEW.version <> OLD.version + 1",
            "NEW.last_applied_cursor <> OLD.last_applied_cursor + 1",
            "USING ERRCODE = '40001'",
            "UNIQUE (case_id, cursor)",
            "UNIQUE (case_id, source_event_id)",
            "UNIQUE (case_id, source_event_sha256)",
            "UNIQUE (prior_receipt_id)",
        ):
            self.assertIn(token, self.sql)


@unittest.skipUnless(
    DSN,
    "WAJE_VNEXT_GATE35_MIGRATION_TEST_DSN is not configured",
)
class Gate35MigrationDatabaseContractTest(unittest.TestCase):
    def test_version6_exists_and_all_expected_tables_are_catalogued(self) -> None:
        assert DSN is not None
        with psycopg.connect(DSN) as connection:
            ledger = connection.execute(
                """
                SELECT name
                FROM waje_vnext.schema_migrations
                WHERE version = 6
                """
            ).fetchone()
            self.assertEqual(
                ledger,
                ("gate3_5_evidence_answer_projection",),
            )
            rows = connection.execute(
                """
                SELECT tablename
                FROM pg_catalog.pg_tables
                WHERE schemaname = 'waje_vnext'
                """
            ).fetchall()
        table_names = {row[0] for row in rows}
        self.assertTrue(
            {
                "capability_result_envelopes",
                "capability_result_receipts",
                "evidence_admission_records",
                "evidence_use_bindings",
                "claim_precheck_records",
                "answer_claim_records",
                "workflow_projection_snapshots",
                "workflow_application_receipts",
                "workflow_projection_heads",
            }.issubset(table_names)
        )

    def test_catalog_contains_db_hard_denies_and_immutable_triggers(self) -> None:
        assert DSN is not None
        with psycopg.connect(DSN) as connection:
            constraints = connection.execute(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_catalog.pg_constraint
                WHERE connamespace = 'waje_vnext'::regnamespace
                  AND conrelid IN (
                      'waje_vnext.answer_versions'::regclass,
                      'waje_vnext.evidence_admission_records'::regclass,
                      'waje_vnext.obligation_satisfaction_records'::regclass,
                      'waje_vnext.workflow_projection_snapshots'::regclass
                  )
                """
            ).fetchall()
            triggers = connection.execute(
                """
                SELECT c.relname, t.tgname
                FROM pg_catalog.pg_trigger AS t
                JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = 'waje_vnext'
                  AND NOT t.tgisinternal
                """
            ).fetchall()
        rendered = "\n".join(row[0] for row in constraints)
        self.assertIn("status = 'provisional'::text", rendered)
        self.assertIn("admission_status = 'rejected'::text", rendered)
        self.assertIn("evidence_use_binding_ids", rendered)
        self.assertNotIn("'settled'::text = ANY", rendered)
        self.assertNotIn("'delivered'::text = ANY", rendered)
        trigger_names = {row[1] for row in triggers}
        self.assertIn("evidence_records_immutable", trigger_names)
        self.assertIn("answer_versions_immutable", trigger_names)
        self.assertIn("workflow_projection_head_cas", trigger_names)


if __name__ == "__main__":
    unittest.main()
