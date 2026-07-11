from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.dataset_catalog import (
    canonical_dataset_release_members,
    validate_dataset_snapshot_release_payloads,
)

from tools.data.register_existing_paid_success_snapshot import (
    ExistingPaidSuccessInspection,
    PaidSuccessRegistrationError,
    build_paid_success_snapshot_payload,
    inspect_existing_paid_success,
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
    def __init__(self, *, aggregate=None, schema=SCHEMA):
        self.schema = tuple(schema)
        self.aggregate = {
            "row_count": 41_234_677,
            "min_business_date": "2024-01-01",
            "max_business_date": "2026-07-04",
            "null_critical_fields": 0,
            "invalid_amount_rows": 0,
            "duplicate_key_rows": 0,
            "content_hash_a": 123456789,
            "content_hash_b": 987654321,
            **(aggregate or {}),
        }
        self.queries = []

    def query(self, query, parameters=None, settings=None):
        self.queries.append(query)
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
        self.assertEqual(len(client.queries), 2)
        self.assertIn("system.columns", client.queries[0])
        self.assertIn("count()", client.queries[1])
        self.assertIn("groupBitXor", client.queries[1])

    def test_aggregate_fingerprint_canonicalizes_nullable_columns(self):
        nullable_schema = (*SCHEMA, ("channel", "Nullable(String)"))
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

        self.assertIn("ifNull(`channel`, '')", client.queries[1])

    def test_inspector_fails_closed_for_each_immutable_source_fact(self):
        mutations = {
            "archive_checksum": {"contract_archive_sha256": "0" * 64},
            "schema": {"schema": SCHEMA[:-1]},
            "row_count": {"aggregate": {"row_count": 41_234_676}},
            "date_range": {"aggregate": {"max_business_date": "2026-07-03"}},
            "duplicate_key": {"aggregate": {"duplicate_key_rows": 1}},
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
                with self.assertRaisesRegex(
                    PaidSuccessRegistrationError, failure_type
                ):
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
            "contracts/sources/paid-order-detail.source.yaml@0.3",
        )
        self.assertEqual(payload["row_count"], 41_234_677)
        self.assertEqual(payload["reconciliation_ref"], "")
        self.assertEqual(payload["date_range"], ["2024-01-01", "2026-07-04"])
        self.assertEqual(payload["source_checksums"]["archive_sha256"], self.archive_sha256)
        self.assertEqual(len(payload["rows_content_hash"]), 64)

    def test_registration_publishes_one_atomic_release_without_payment_attempt(self):
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
            "payment_attempt",
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
                (payload, {**payload, "dataset_id": "payment_attempt", "snapshot_ref": "attempt"})
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
        self.assertEqual(store.locked_ids, [])
        self.assertEqual(store.publish_calls, 0)

    def test_registration_rejects_immutable_drift_for_same_release(self):
        store = _LockRecordingStore()
        kwargs = {
            "snapshot_id": "paid-order-detail-20240101-20260704",
            "load_revision": "accepted-20260705",
            "loaded_at": "2026-07-05T00:00:00+00:00",
        }
        register_existing_paid_success_snapshot(store, self._valid_inspection(), **kwargs)
        drifted = ExistingPaidSuccessInspection(
            **{
                **self._valid_inspection().__dict__,
                "rows_content_hash": "f" * 64,
            }
        )

        with self.assertRaisesRegex(ValueError, "dataset_snapshot_published_immutable"):
            register_existing_paid_success_snapshot(store, drifted, **kwargs)

    def _source_contract(self):
        return {
            "contract_version": "0.3",
            "source_file": {
                "sha256": self.archive_sha256,
                "date_range": {"start": "2024-01-01", "end": "2026-07-04"},
            },
            "storage_boundary": {
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


if __name__ == "__main__":
    unittest.main()
