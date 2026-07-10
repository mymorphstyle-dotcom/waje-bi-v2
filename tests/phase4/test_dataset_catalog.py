from datetime import date, datetime
import unittest

from bi_agent.runtime.dataset_catalog import DatasetCatalog, DatasetSnapshot


class DatasetCatalogTest(unittest.TestCase):
    def test_resolves_latest_eligible_snapshot_without_hardcoded_table(self):
        catalog = DatasetCatalog(
            (
                DatasetSnapshot(
                    snapshot_ref="snapshot:paid_order:1",
                    dataset_id="paid_order_success",
                    physical_table="paid_order_success_clean_20240101_20260704",
                    watermark="2026-07-04",
                    schema_fingerprint="schema-1",
                    schema_fields=("business_date_lagos", "paid_amount_ngn"),
                    contract_ref="contracts/sources/paid-order-detail.source.yaml@0.2",
                    permission_scopes=("analyst",),
                    loaded_at="2026-07-05T00:00:00+00:00",
                    status="active",
                ),
            )
        )

        snapshot = catalog.resolve(
            "paid_order_success",
            as_of=datetime.fromisoformat("2026-07-10T00:00:00+00:00"),
            permission_scope="analyst",
        )
        self.assertEqual(snapshot.physical_table, "paid_order_success_clean_20240101_20260704")

    def test_common_watermark_uses_oldest_required_source(self):
        catalog = DatasetCatalog(
            (
                DatasetSnapshot("s1", "paid_order_success", "paid", "2026-07-04", "a", (), "c1", ("analyst",), "2026-07-05T00:00:00Z", "active"),
                DatasetSnapshot("s2", "market_dashboard", "dashboard", "2026-06-02", "b", (), "c2", ("analyst",), "2026-06-03T00:00:00Z", "active"),
            )
        )
        self.assertEqual(
            catalog.common_watermark(("paid_order_success", "market_dashboard")),
            date(2026, 6, 2),
        )

    def test_rejects_naive_as_of(self):
        catalog = DatasetCatalog((_snapshot("s1", loaded_at="2026-07-05T00:00:00Z"),))

        with self.assertRaisesRegex(ValueError, "timezone_aware_required:as_of"):
            catalog.resolve(
                "paid_order_success",
                as_of=datetime(2026, 7, 10),
                permission_scope="analyst",
            )

    def test_rejects_naive_snapshot_loaded_at(self):
        catalog = DatasetCatalog((_snapshot("s1", loaded_at="2026-07-05T00:00:00"),))

        with self.assertRaisesRegex(ValueError, "timezone_aware_required:loaded_at"):
            catalog.resolve(
                "paid_order_success",
                as_of=datetime.fromisoformat("2026-07-10T00:00:00+00:00"),
                permission_scope="analyst",
            )

    def test_compares_equivalent_offsets_as_the_same_instant(self):
        catalog = DatasetCatalog(
            (
                _snapshot("snapshot:paid:1", loaded_at="2026-07-05T01:00:00+01:00"),
                _snapshot("snapshot:paid:2", loaded_at="2026-07-05T00:00:00Z"),
            )
        )

        snapshot = catalog.resolve(
            "paid_order_success",
            as_of=datetime.fromisoformat("2026-07-05T00:00:00+00:00"),
            permission_scope="analyst",
        )

        self.assertEqual(snapshot.snapshot_ref, "snapshot:paid:2")

    def test_resolve_filters_future_inactive_and_permission_blocked_versions(self):
        catalog = DatasetCatalog(
            (
                _snapshot("eligible-old", loaded_at="2026-07-04T00:00:00Z"),
                _snapshot("eligible-latest", loaded_at="2026-07-05T00:00:00Z"),
                _snapshot("future", loaded_at="2026-07-11T00:00:00Z"),
                _snapshot("inactive", loaded_at="2026-07-09T00:00:00Z", status="inactive"),
                _snapshot(
                    "permission-blocked",
                    loaded_at="2026-07-08T00:00:00Z",
                    permission_scopes=("admin",),
                ),
            )
        )

        snapshot = catalog.resolve(
            "paid_order_success",
            as_of=datetime.fromisoformat("2026-07-10T00:00:00+00:00"),
            permission_scope="analyst",
        )

        self.assertEqual(snapshot.snapshot_ref, "eligible-latest")


def _snapshot(
    snapshot_ref,
    *,
    loaded_at,
    status="active",
    permission_scopes=("analyst",),
):
    return DatasetSnapshot(
        snapshot_ref=snapshot_ref,
        dataset_id="paid_order_success",
        physical_table=f"paid_order_success_{snapshot_ref}",
        watermark="2026-07-04",
        schema_fingerprint="schema-1",
        schema_fields=("business_date_lagos", "paid_amount_ngn"),
        contract_ref="contracts/sources/paid-order-detail.source.yaml@0.2",
        permission_scopes=permission_scopes,
        loaded_at=loaded_at,
        status=status,
    )


if __name__ == "__main__":
    unittest.main()
