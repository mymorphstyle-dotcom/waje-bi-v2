from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.dataset_catalog import (
    canonical_dataset_release_members,
    validate_dataset_snapshot_release_payloads,
)
from tests.phase7.test_conversation_persistence import FakeConnection, FakeCursor

from tools.data.register_existing_paid_success_snapshot import (
    ExistingPaidSuccessInspection,
    PaidSuccessRegistrationError,
    build_paid_success_snapshot_payload,
    inspect_existing_paid_success,
    main,
    register_existing_paid_success_snapshot,
)


PHYSICAL_TABLE = "paid_order_success_clean_20240101_20260704"
SCHEMA = (
    ("order_id", "String"),
    ("user_id", "String"),
    ("business_date_lagos", "Date"),
    ("paid_amount_ngn", "Decimal(18, 2)"),
)


class _QueryResult:
    def __init__(self, rows):
        self._rows = tuple(rows)

    def named_results(self):
        return iter(self._rows)


class FakeClickHouseClient:
    def __init__(
        self,
        *,
        aggregate=None,
        schema=SCHEMA,
        configured_database="waje_bi",
    ):
        self.schema = tuple(schema)
        self.configured_database = configured_database
        self.aggregate = {
            "row_count": 41_234_677,
            "min_business_date": "2024-01-01",
            "max_business_date": "2026-07-04",
            "null_critical_fields": 0,
            "invalid_amount_rows": 0,
            "duplicate_key_rows": 0,
            "source_first_payment_rows": 100,
            "source_first_payment_users": 99,
            "canonical_first_payment_rows": 99,
            "canonical_first_payment_users": 99,
            "canonical_first_payment_duplicate_rows": 0,
            "canonical_first_payment_missing_timestamp_rows": 0,
            "noncanonical_first_payment_timestamp_rows": 0,
            "valid_first_payment_lag_rows": 97,
            "missing_registered_at_first_payment_rows": 1,
            "negative_first_payment_lag_rows": 1,
            "content_hash_a": 123456789,
            "content_hash_b": 987654321,
            **(aggregate or {}),
        }
        self.queries = []
        self.query_parameters = []

    def query(self, query, parameters=None, settings=None):
        self.queries.append(query)
        self.query_parameters.append(parameters or {})
        if "currentDatabase() AS configured_database" in query:
            return _QueryResult(({"configured_database": self.configured_database},))
        if "system.columns" in query:
            return _QueryResult(
                {"name": name, "type": data_type} for name, data_type in self.schema
            )
        return _QueryResult((self.aggregate,))


class PaidSuccessSnapshotRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.archive = Path(self.temp_dir.name) / "paid.zip"
        self.archive.write_bytes(b"reviewed-paid-source")
        self.archive_sha256 = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_inspector_requires_reviewed_schema_count_range_and_success_semantics(self):
        client = FakeClickHouseClient()
        inspection = inspect_existing_paid_success(
            client,
            archive_path=self.archive,
            physical_table=PHYSICAL_TABLE,
            source_contract=self._source_contract(),
        )

        self.assertEqual(inspection.watermark, "2026-07-04")
        self.assertEqual(inspection.row_count, 41_234_677)
        self.assertIn("paid_amount_ngn", inspection.schema_fields)
        self.assertTrue(inspection.ready_to_publish)
        self.assertEqual(len(client.queries), 3)
        self.assertIn("currentDatabase()", client.queries[0])
        self.assertIn("system.columns", client.queries[1])
        self.assertEqual(client.query_parameters[1]["analytical_database"], "waje_bi")
        self.assertIn("database = {analytical_database:String}", client.queries[1])
        self.assertIn("count()", client.queries[2])
        self.assertIn("groupBitXor", client.queries[2])
        self.assertIn(
            "FROM `waje_bi`.`paid_order_success_clean_20240101_20260704`",
            client.queries[2],
        )

    def test_wrong_configured_database_fails_before_fact_query(self):
        client = FakeClickHouseClient(configured_database="default")

        with self.assertRaisesRegex(
            PaidSuccessRegistrationError, "analytical_database:mismatch"
        ):
            inspect_existing_paid_success(
                client,
                archive_path=self.archive,
                physical_table=PHYSICAL_TABLE,
                source_contract=self._source_contract(),
            )

        self.assertEqual(len(client.queries), 1)
        self.assertNotIn("count()", client.queries[0])

    def test_unsafe_reviewed_database_identifier_fails_before_any_query(self):
        contract = self._source_contract()
        contract["storage_boundary"]["analytical_database"] = (
            "waje_bi`; DROP DATABASE waje_bi; --"
        )
        client = FakeClickHouseClient()

        with self.assertRaisesRegex(
            PaidSuccessRegistrationError, "analytical_database:invalid_identifier"
        ):
            inspect_existing_paid_success(
                client,
                archive_path=self.archive,
                physical_table=PHYSICAL_TABLE,
                source_contract=contract,
            )

        self.assertEqual(client.queries, [])

    def test_aggregate_fingerprint_canonicalizes_nullable_columns(self):
        nullable_schema = (
            *SCHEMA,
            ("channel", "Nullable(String)"),
            ("optional_amount", "Nullable(UInt64)"),
        )
        contract = self._source_contract()
        contract["storage_boundary"]["clean_schema"] = [
            {"name": name, "type": data_type} for name, data_type in nullable_schema
        ]
        client = FakeClickHouseClient(schema=nullable_schema)

        inspect_existing_paid_success(
            client,
            archive_path=self.archive,
            physical_table=PHYSICAL_TABLE,
            source_contract=contract,
        )

        self.assertIn(
            "tuple(isNull(`channel`), ifNull(`channel`, ''))",
            client.queries[2],
        )
        self.assertIn(
            "tuple(isNull(`optional_amount`), ifNull(`optional_amount`, 0))",
            client.queries[2],
        )

    def test_aggregate_fingerprint_preserves_datetime64_timezone(self):
        typed_schema = (
            *SCHEMA,
            ("registered_at", "Nullable(DateTime64(3, 'Africa/Lagos'))"),
        )
        contract = self._source_contract()
        contract["storage_boundary"]["clean_schema"] = [
            {"name": name, "type": data_type} for name, data_type in typed_schema
        ]
        client = FakeClickHouseClient(schema=typed_schema)

        inspect_existing_paid_success(
            client,
            archive_path=self.archive,
            physical_table=PHYSICAL_TABLE,
            source_contract=contract,
        )

        self.assertIn(
            "ifNull(`registered_at`, toDateTime64(0, 3, 'Africa/Lagos'))",
            client.queries[2],
        )

    def test_schema_mismatch_fails_before_fact_aggregate_query(self):
        client = FakeClickHouseClient(schema=SCHEMA[:-1])

        with self.assertRaisesRegex(PaidSuccessRegistrationError, "schema:mismatch"):
            inspect_existing_paid_success(
                client,
                archive_path=self.archive,
                physical_table=PHYSICAL_TABLE,
                source_contract=self._source_contract(),
            )

        self.assertEqual(len(client.queries), 2)
        self.assertIn("system.columns", client.queries[1])

    def test_unreviewed_malicious_physical_column_never_reaches_fact_query(self):
        client = FakeClickHouseClient(
            schema=(*SCHEMA[:-1], ("paid_amount_ngn`); DROP TABLE facts; --", "String"))
        )

        with self.assertRaisesRegex(PaidSuccessRegistrationError, "schema:mismatch"):
            inspect_existing_paid_success(
                client,
                archive_path=self.archive,
                physical_table=PHYSICAL_TABLE,
                source_contract=self._source_contract(),
            )

        self.assertEqual(len(client.queries), 2)
        self.assertNotIn("DROP TABLE", "\n".join(client.queries))

    def test_inspector_fails_closed_for_each_immutable_source_fact(self):
        mutations = {
            "archive_checksum": {"contract_archive_sha256": "0" * 64},
            "schema": {"schema": SCHEMA[:-1]},
            "row_count": {"aggregate": {"row_count": 41_234_676}},
            "date_range": {"aggregate": {"max_business_date": "2026-07-03"}},
            "duplicate_key": {"aggregate": {"duplicate_key_rows": 1}},
            "data_quality": {
                "aggregate": {"canonical_first_payment_duplicate_rows": 1}
            },
        }
        for failure_type, mutation in mutations.items():
            with self.subTest(failure_type=failure_type):
                contract = self._source_contract()
                if "contract_archive_sha256" in mutation:
                    contract["source_file"]["sha256"] = mutation[
                        "contract_archive_sha256"
                    ]
                client = FakeClickHouseClient(
                    aggregate=mutation.get("aggregate"),
                    schema=mutation.get("schema", SCHEMA),
                )
                with self.assertRaisesRegex(PaidSuccessRegistrationError, failure_type):
                    inspect_existing_paid_success(
                        client,
                        archive_path=self.archive,
                        physical_table=PHYSICAL_TABLE,
                        source_contract=contract,
                    )

    def test_payload_contains_complete_immutable_source_evidence(self):
        payload = build_paid_success_snapshot_payload(
            self._valid_inspection(),
            snapshot_id="paid-order-detail-20240101-20260704",
            load_revision="accepted-20260705",
            loaded_at="2026-07-05T00:00:00+00:00",
        )

        self.assertEqual(payload["dataset_id"], "paid_order_success")
        self.assertEqual(
            payload["contract_ref"],
            "contracts/sources/paid-order-detail.source.yaml@0.4",
        )
        self.assertEqual(payload["row_count"], 41_234_677)
        self.assertEqual(payload["reconciliation_ref"], "")
        self.assertEqual(payload["date_range"], ["2024-01-01", "2026-07-04"])
        self.assertEqual(
            payload["source_checksums"]["archive_sha256"], self.archive_sha256
        )
        self.assertEqual(len(payload["rows_content_hash"]), 64)

    def test_registration_keeps_final_outcome_in_its_own_atomic_release(self):
        store = _LockRecordingStore()
        result = register_existing_paid_success_snapshot(
            store,
            self._valid_inspection(),
            snapshot_id="paid-order-detail-20240101-20260704",
            load_revision="accepted-20260705",
            loaded_at="2026-07-05T00:00:00+00:00",
        )

        self.assertEqual(result.dataset_ids, ("paid_order_success",))
        self.assertEqual(store.locked_ids, ["paid-order-detail-20240101-20260704"])
        self.assertEqual(store.publish_calls, 1)
        self.assertEqual(len(store.dataset_snapshots), 1)
        self.assertNotIn(
            "payment_final_outcome",
            tuple(item["dataset_id"] for item in store.dataset_snapshots.values()),
        )
        self.assertEqual(
            result.authority_record_ref,
            store.resolve_dataset_release(result.release_ref).authority_record_ref,
        )

        repeated = register_existing_paid_success_snapshot(
            store,
            self._valid_inspection(),
            snapshot_id="paid-order-detail-20240101-20260704",
            load_revision="accepted-20260705",
            loaded_at="2026-07-05T00:00:00+00:00",
        )
        self.assertEqual(repeated, result)

    def test_registration_builds_and_validates_authority_inside_release_lock(self):
        store = _OrderingStore()
        events = store.events

        from tools.data import register_existing_paid_success_snapshot as module

        original_build = module.build_paid_success_snapshot_payload
        original_validate = module.validate_dataset_snapshot_release_payloads
        original_authority = module.build_dataset_release_authority_record

        def record(name, function):
            def wrapped(*args, **kwargs):
                events.append(name)
                return function(*args, **kwargs)

            return wrapped

        with (
            patch.object(
                module,
                "build_paid_success_snapshot_payload",
                record("build", original_build),
            ),
            patch.object(
                module,
                "validate_dataset_snapshot_release_payloads",
                record("validate", original_validate),
            ),
            patch.object(
                module,
                "build_dataset_release_authority_record",
                record("authority", original_authority),
            ),
        ):
            register_existing_paid_success_snapshot(
                store,
                self._valid_inspection(),
                snapshot_id="paid-order-detail-20240101-20260704",
                load_revision="accepted-20260705",
                loaded_at="2026-07-05T00:00:00+00:00",
            )

        self.assertEqual(
            events[:6],
            ["lock_enter", "build", "validate", "authority", "publish", "lock_exit"],
        )

    def test_paid_success_release_membership_is_exactly_one_canonical_member(self):
        self.assertEqual(
            canonical_dataset_release_members("paid_order_success"),
            ("paid_order_success",),
        )
        payload = build_paid_success_snapshot_payload(
            self._valid_inspection(),
            snapshot_id="paid-order-detail-20240101-20260704",
            load_revision="accepted-20260705",
            loaded_at="2026-07-05T00:00:00+00:00",
        )
        with self.assertRaisesRegex(ValueError, "dataset_snapshot_release_dataset_set"):
            validate_dataset_snapshot_release_payloads(
                (
                    payload,
                    {
                        **payload,
                        "dataset_id": "payment_final_outcome",
                        "snapshot_ref": "payment-final",
                    },
                )
            )

    def test_registration_rejects_unready_inspection_before_lock_or_write(self):
        store = _LockRecordingStore()
        invalid = ExistingPaidSuccessInspection(
            **{
                **self._valid_inspection().__dict__,
                "validation_errors": ("row_count:mismatch",),
            }
        )

        with self.assertRaisesRegex(PaidSuccessRegistrationError, "row_count"):
            register_existing_paid_success_snapshot(
                store,
                invalid,
                snapshot_id="paid-order-detail-20240101-20260704",
                load_revision="accepted-20260705",
                loaded_at="2026-07-05T00:00:00+00:00",
            )
        self.assertEqual(store.locked_ids, ["paid-order-detail-20240101-20260704"])
        self.assertEqual(store.publish_calls, 0)

    def test_registration_rejects_immutable_drift_for_same_release(self):
        store = _LockRecordingStore()
        kwargs = {
            "snapshot_id": "paid-order-detail-20240101-20260704",
            "load_revision": "accepted-20260705",
            "loaded_at": "2026-07-05T00:00:00+00:00",
        }
        register_existing_paid_success_snapshot(
            store, self._valid_inspection(), **kwargs
        )
        drifted = ExistingPaidSuccessInspection(
            **{
                **self._valid_inspection().__dict__,
                "rows_content_hash": "f" * 64,
            }
        )

        with self.assertRaisesRegex(ValueError, "dataset_snapshot_published_immutable"):
            register_existing_paid_success_snapshot(store, drifted, **kwargs)

    def test_postgres_store_round_trip_is_idempotent_and_rolls_back_drift(self):
        connection = _PaidSuccessPostgresConnection()
        store = PostgresConversationStore(connection)
        kwargs = {
            "snapshot_id": "paid-order-detail-20240101-20260704",
            "load_revision": "accepted-20260705",
            "loaded_at": "2026-07-05T00:00:00+00:00",
        }

        first = register_existing_paid_success_snapshot(
            store, self._valid_inspection(), **kwargs
        )
        repeated = register_existing_paid_success_snapshot(
            store, self._valid_inspection(), **kwargs
        )
        self.assertEqual(repeated, first)

        drifted = ExistingPaidSuccessInspection(
            **{**self._valid_inspection().__dict__, "rows_content_hash": "f" * 64}
        )
        with self.assertRaisesRegex(
            RuntimeError, "dataset_snapshot_release_validation_failed"
        ):
            register_existing_paid_success_snapshot(store, drifted, **kwargs)
        self.assertGreaterEqual(connection.rollbacks, 1)
        self.assertEqual(
            store.resolve_dataset_release(first.release_ref).authority_record_ref,
            first.authority_record_ref,
        )

    def test_cli_redacts_unexpected_runtime_exception_details(self):
        secret = "postgresql://admin:password@secret-host/waje"
        output = io.StringIO()
        argv = [
            "--archive",
            str(self.archive),
            "--physical-table",
            PHYSICAL_TABLE,
            "--snapshot-id",
            "paid-order-detail-20240101-20260704",
            "--load-revision",
            "accepted-20260705",
            "--dry-run",
        ]

        with (
            patch(
                "tools.data.register_existing_paid_success_snapshot.ClickHouseRuntime.from_env",
                side_effect=ValueError(secret),
            ),
            redirect_stdout(output),
        ):
            exit_code = main(argv)

        rendered = output.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("secret-host", rendered)
        self.assertEqual(
            json.loads(rendered)["error_code"],
            "registration_runtime_failed",
        )

    def _source_contract(self):
        return {
            "contract_version": "0.4",
            "source_file": {
                "sha256": self.archive_sha256,
                "date_range": {"start": "2024-01-01", "end": "2026-07-04"},
            },
            "storage_boundary": {
                "analytical_database": "waje_bi",
                "clean_table": PHYSICAL_TABLE,
                "clean_schema": [
                    {"name": name, "type": data_type} for name, data_type in SCHEMA
                ],
            },
            "paid_amount_boundary": {
                "final_success_status": "pay_success",
                "business_date_basis": "支付完成时间 converted to Africa/Lagos",
                "dedup_key": "订单id",
                "duplicate_success_rule": "keep latest 支付完成时间",
                "cleaned_profile": {"paid_records": 41_234_677},
            },
            "first_payment_authority": {
                "source_first_payment_rows": 100,
                "canonical_first_payment_rows": 99,
                "canonical_first_payment_users": 99,
            },
            "timestamp_authority": {
                "registered_to_first_paid_lag": {
                    "canonical_rows_valid": 97,
                    "canonical_rows_missing_registered_at": 1,
                    "canonical_rows_negative_source_anomaly": 1,
                }
            },
        }

    def _valid_inspection(self):
        return inspect_existing_paid_success(
            FakeClickHouseClient(),
            archive_path=self.archive,
            physical_table=PHYSICAL_TABLE,
            source_contract=self._source_contract(),
        )


class _LockRecordingStore(InMemoryConversationStore):
    def __init__(self):
        super().__init__()
        self.locked_ids = []
        self.publish_calls = 0

    def dataset_snapshot_release_lock(self, logical_snapshot_id):
        self.locked_ids.append(logical_snapshot_id)

        class _Lock:
            def __enter__(inner_self):
                return None

            def __exit__(inner_self, exc_type, exc, traceback):
                return False

        return _Lock()

    def publish_dataset_snapshot_release(self, **kwargs):
        self.publish_calls += 1
        return super().publish_dataset_snapshot_release(**kwargs)


class _OrderingStore(_LockRecordingStore):
    def __init__(self):
        super().__init__()
        self.events = []

    def dataset_snapshot_release_lock(self, logical_snapshot_id):
        parent_lock = super().dataset_snapshot_release_lock(logical_snapshot_id)
        events = self.events

        class _Lock:
            def __enter__(inner_self):
                parent_lock.__enter__()
                events.append("lock_enter")

            def __exit__(inner_self, exc_type, exc, traceback):
                events.append("lock_exit")
                return parent_lock.__exit__(exc_type, exc, traceback)

        return _Lock()

    def publish_dataset_snapshot_release(self, **kwargs):
        self.events.append("publish")
        return super().publish_dataset_snapshot_release(**kwargs)


class _PaidSuccessPostgresConnection(FakeConnection):
    def __init__(self):
        super().__init__()
        self.snapshots = {}
        self.releases = {}
        self.pending_snapshots = {}
        self.pending_releases = {}

    def execute(self, statement, params=None):
        params = params or {}
        self.statements.append((statement, params))
        if "INSERT INTO waje_runtime.dataset_snapshots" in statement:
            payload = json.loads(params["payload"])
            snapshot_ref = payload["snapshot_ref"]
            existing = self.pending_snapshots.get(
                snapshot_ref, self.snapshots.get(snapshot_ref)
            )
            if existing is None or existing == payload:
                self.pending_snapshots[snapshot_ref] = payload
            return FakeCursor([])
        if "INSERT INTO waje_runtime.dataset_snapshot_releases" in statement:
            self.pending_releases[params["release_ref"]] = {
                "payload": json.loads(params["payload"]),
                "logical_snapshot_id": params["logical_snapshot_id"],
                "load_revision": params["load_revision"],
                "snapshot_refs": json.loads(params["snapshot_refs"]),
            }
            return FakeCursor([])
        if "validated_count" in statement:
            expected = json.loads(params["expected_payloads"])
            releases = {**self.releases, **self.pending_releases}
            snapshots = {**self.snapshots, **self.pending_snapshots}
            validated = 0
            for payload in expected:
                release = releases.get(payload["release_ref"])
                if (
                    snapshots.get(payload["snapshot_ref"]) == payload
                    and release is not None
                    and release["logical_snapshot_id"] == payload["logical_snapshot_id"]
                    and release["load_revision"] == payload["load_revision"]
                    and release["snapshot_refs"] == json.loads(params["snapshot_refs"])
                ):
                    validated += 1
            return FakeCursor([{"validated_count": validated}])
        if "SELECT r.payload AS release_payload" in statement:
            release = self.releases.get(params["release_ref"])
            if release is None:
                return FakeCursor([])
            members = [self.snapshots[ref] for ref in release["snapshot_refs"]]
            return FakeCursor(
                [
                    {
                        "release_payload": release["payload"],
                        "logical_snapshot_id": release["logical_snapshot_id"],
                        "load_revision": release["load_revision"],
                        "snapshot_refs": release["snapshot_refs"],
                        "member_count": len(members),
                        "member_payloads": members,
                        "member_columns": members,
                    }
                ]
            )
        return FakeCursor([])

    def commit(self):
        self.snapshots.update(self.pending_snapshots)
        self.releases.update(self.pending_releases)
        self.pending_snapshots.clear()
        self.pending_releases.clear()
        self.commits += 1

    def rollback(self):
        self.pending_snapshots.clear()
        self.pending_releases.clear()
        self.rollbacks += 1


if __name__ == "__main__":
    unittest.main()
